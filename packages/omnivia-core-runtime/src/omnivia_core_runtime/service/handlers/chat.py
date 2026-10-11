"""Production handlers for ``chat.command``, ``chat.events`` and ``chat.snapshot``.

Three operations, and none of them owns any Chat rule of its own. `chat.events` is
the wire projection of :func:`~service.chat_generation.replay_generation_events`;
`chat.snapshot` is the wire projection of
:func:`~service.chat_snapshot.resolve_chat_snapshot`; `chat.command` is the
catalogue's front door onto :func:`~service.chat_command.execute_chat_command`,
which is itself the one mutation seam composed over the Chat repository. Everything
that decides a request -- the twelve authorization checks, the server-issued grant,
the fence, the audit, the claim and the proved replay -- already happened by the time
any of these runs.

**The one seam this module adds, and why it is a parameter.** `chat.command` carries a
Chat Contract command name and that command's own request document, both opaque to the
Application Contract. Translating one into the domain writes it performs is Chat domain
policy, and it lives in exactly one place: :class:`ChatHandlers` takes a
:data:`ChatCommandResolver`, and `service/application.py` supplies
`service/chat_submit.py`'s at the wiring site. That resolver serves `SubmitMessage` and
returns `None` for every other command name -- and for any `SubmitMessage` variant
naming work no authority in this build performs -- so an authorized, granted,
well-formed `chat.command` this build has no implementation of still refuses with
`dependency_unavailable`, which is the honest answer and the one that cannot quietly
half-settle a conversation. A build wired with no resolver at all refuses every Chat
command the same way; nothing here changes between the two.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.chat_contract.v1 import CHAT_COMMAND_NAMES, ChatEvent
from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ContractSemanticError,
    idempotency_equivalence,
)
from omnivia_core.contracts.v1.generated import (
    ChatCommandInput,
    ChatCommandResult,
    ChatDraftInput,
    ChatEventsInput,
    ChatEventsResult,
    ChatGenerationEvent,
    ChatSnapshotInput,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.chat_command import (
    ChatAggregateExpectation,
    ChatCommand,
    execute_chat_command,
    is_governed_command_result,
)
from omnivia_core_runtime.service.chat_generation import replay_generation_events
from omnivia_core_runtime.service.chat_snapshot import (
    resolve_chat_snapshot,
    resolve_draft_readback,
)
from omnivia_core_runtime.service.chat_submit import (
    COMMIT_MESSAGE_AMENDMENT_COMMAND,
    REGENERATE_RESPONSE_COMMAND,
    RETRY_GENERATION_COMMAND,
    SUBMIT_MESSAGE_COMMAND,
    retry_generation_attempt_id,
)
from omnivia_core_runtime.service.mutation import issue_mutation_grant
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    application_refusal,
)
from omnivia_core_runtime.storage.chat import (
    Direction,
    GenerationEvent,
    GenerationTextChunk,
    read_directions_for_generation_job,
    read_generation_job,
    read_generation_text_chunks,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator

CHAT_COMMAND_OPERATION: Final = "chat.command"
CHAT_DRAFT_OPERATION: Final = "chat.draft"
CHAT_EVENTS_OPERATION: Final = "chat.events"
CHAT_SNAPSHOT_OPERATION: Final = "chat.snapshot"
CHAT_FAMILY_OPERATIONS: Final = frozenset(
    {
        CHAT_COMMAND_OPERATION,
        CHAT_DRAFT_OPERATION,
        CHAT_EVENTS_OPERATION,
        CHAT_SNAPSHOT_OPERATION,
    }
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this chat operation"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative chat storage"
)
_MESSAGE_NO_COMMAND: Final = (
    "this build serves no implementation of the chat command this request names"
)
_TEXT_APPENDED: Final = "chat.generation.text_appended"
_ITERATION_COMPLETED: Final = "chat.generation.iteration_completed"
_DIRECTION_STATE_CHANGED: Final = "chat.direction.state_changed"
_TOOL_CALL_STATE_CHANGED: Final = "chat.tool_call.state_changed"
#: `ChatEventsResult.events` and `.transport_events` are both `maxItems: 1000`, so
#: one page is the first 1000 of the replayed suffix and the last cursor it returns
#: is what the caller continues from. Not a new pagination field: the cursor the
#: envelope already carries is the continuation it has always been.
_MAX_EVENTS: Final = 1000
#: The two fork commands (C10). Each opens its own `<commandId>.gen` job in its own
#: commit, with no queued submission row and no attempt: the job itself is the whole
#: work. They are woken by the job they just opened, exactly as a submission is woken
#: by the row it just queued -- without a wake the job sits queued and the fork never
#: answers.
_FORK_GENERATION_COMMANDS: Final = frozenset(
    {REGENERATE_RESPONSE_COMMAND, COMMIT_MESSAGE_AMENDMENT_COMMAND}
)
_GENERATION_EXECUTOR_COMMANDS: Final = frozenset(
    {SUBMIT_MESSAGE_COMMAND, RETRY_GENERATION_COMMAND, *_FORK_GENERATION_COMMANDS}
)

#: How one decoded `chat.command` becomes the domain mutation the transaction seam
#: runs. Returning `None` is a refusal rather than a no-op: the seam is never entered,
#: so nothing is claimed, audited or written for a command this build cannot perform.
ChatCommandResolver = Callable[[ChatCommandInput, OperationContext], ChatCommand | None]
ChatGenerationExecution = Callable[..., None]


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _event(event: GenerationEvent) -> ChatGenerationEvent:
    """One durable event as the contract states it.

    Deliberately narrow: the durable row carries the conversation, branch, attempt,
    trigger and result message identities the workspace needs, and none of them is a
    field of `ChatGenerationEvent`. Projecting only the declared six is what keeps this
    from publishing more of the graph than `chat.events` says it answers with.
    """
    return ChatGenerationEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        generation_event_sequence=event.generation_event_sequence,
        cursor=event.cursor,
        occurred_at=_timestamp(event.occurred_at_us),
        payload=dict(event.payload) or None,
    )


def _direction_event_fields(
    event: GenerationEvent, directions: Mapping[str, Direction]
) -> dict[str, Any]:
    """The exact `DirectionStateChangedEvent` fields, identity only.

    The event-time facts (`directionId`, `directionVersion`, `state`) come from the
    durable payload and the actor and job sequence from the durable Direction they name.
    The event carries no attempt, trigger or result message, and no authored content.
    """
    payload = event.payload
    direction = directions.get(str(payload.get("directionId")))
    if (
        direction is None
        or direction.generation_job_id != event.generation_job_id
        or direction.conversation_id != event.conversation_id
        or direction.branch_id != event.branch_id
    ):
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
    return {
        "actorId": direction.actor_id,
        "branchId": event.branch_id,
        "directionId": direction.direction_id,
        "directionVersion": payload.get("directionVersion"),
        "generationEventSequence": event.generation_event_sequence,
        "generationJobId": event.generation_job_id,
        "jobSequence": direction.direction_sequence,
        "state": payload.get("state"),
    }


def _tool_call_event_fields(event: GenerationEvent) -> dict[str, Any]:
    """The exact `ToolCallStateChangedEvent` fields, identity only.

    Every one comes from the durable payload or the event row. Unlike
    `_direction_event_fields` this needs no second read: a Direction's actor and
    job sequence live on the Direction row, while everything a tool-call event
    states was written into its own payload, bounded by migration 0072's CHECK.

    Carries no attempt and no result message. The event's `state` is top-level,
    not nested under a payload, which is the convention `DirectionStateChanged`
    set and what Platform's own decoder reads.
    """
    payload = event.payload
    fields: dict[str, Any] = {
        "branchId": event.branch_id,
        "generationJobId": event.generation_job_id,
        "generationEventSequence": event.generation_event_sequence,
        "toolCallId": payload.get("toolCallId"),
        "turnId": payload.get("turnId"),
        "state": payload.get("state"),
        "lifecycleSequence": payload.get("lifecycleSequence"),
    }
    for optional in ("waitId", "reasonCode"):
        if optional in payload:
            fields[optional] = payload[optional]
    return fields


def _transport_event(
    event: GenerationEvent,
    chunks: Mapping[tuple[str, int], GenerationTextChunk],
    directions: Mapping[str, Direction],
) -> Mapping[str, Any]:
    """One durable event as the *Chat* contract states it, or nothing at all.

    The Application projection above and this one answer for the same position and
    are not the same document: `_event` is the sanitised lifecycle view the envelope
    has always carried, and this is the exact `events.schema.json` transport event.
    Construction goes through :class:`ChatEvent`, which validates the emitted
    document against that event type's own closed branch, so a field the type does
    not define, a required one missing, and a governed field of the wrong type are
    all refused here rather than published.

    Only the identities the event type governs are projected. The durable
    `payload` -- provider event type, provider event id -- is never one of them, so
    no provider metadata, hash, endpoint, model or raw body crosses this seam. The
    generated text is the one exception the contract does define, and it is read
    from the chunk that holds it, never from a payload that has never carried it.
    """
    fields: dict[str, Any]
    if event.event_type == _DIRECTION_STATE_CHANGED:
        fields = _direction_event_fields(event, directions)
    elif event.event_type == _TOOL_CALL_STATE_CHANGED:
        fields = _tool_call_event_fields(event)
    else:
        fields = {
            "branchId": event.branch_id,
            "generationJobId": event.generation_job_id,
            "triggerMessageId": event.trigger_message_id,
            "generationEventSequence": event.generation_event_sequence,
        }
        if event.generation_attempt_id is not None:
            fields["generationAttemptId"] = event.generation_attempt_id
        if event.result_message_id is not None:
            fields["resultMessageId"] = event.result_message_id
    if event.event_type == _TEXT_APPENDED:
        ordinal = event.payload.get("chunkOrdinal")
        if (
            event.generation_attempt_id is None
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
        ):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        chunk = chunks.get((event.generation_attempt_id, ordinal))
        # The chunk is the only place the text is, and the provider event identity is
        # what says this event orders *that* chunk. A missing chunk, or one written
        # under a different provider event, is a torn history rather than a stream
        # this handler may guess the text of.
        if chunk is None or chunk.provider_event_id != event.provider_event_id:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        # The payload states the same identity when it states it at all. Older durable
        # history predates the key, so its absence is history rather than a tear; a
        # key that is present but is not exactly the row's provider event id is a torn
        # history. It stays out of `fields` either way: corroboration, not something
        # this seam publishes.
        if "providerEventId" in event.payload and (
            not isinstance(event.payload["providerEventId"], str)
            or event.payload["providerEventId"] != event.provider_event_id
        ):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        fields["chunkOrdinal"] = ordinal
        fields["textDelta"] = chunk.text_content
    if event.event_type == _ITERATION_COMPLETED:
        fields["generationJobVersion"] = event.payload.get("generationJobVersion")
        fields["iteration"] = event.payload.get("iteration")
    try:
        return ChatEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            workspace_id=event.workspace_id,
            conversation_id=event.conversation_id,
            occurred_at=_timestamp(event.occurred_at_us),
            cursor=event.cursor,
            fields=fields,
        ).to_wire()
    except (TypeError, ValueError):
        # Bounded on purpose: the decode error names the field and rule it refused,
        # and this operation answers a caller that is not entitled to either. The
        # `pass` is load-bearing -- `from None` would leave that text reachable on
        # `__context__`, so the constant refusal is raised once the handler has
        # exited and there is no exception being handled to chain to.
        pass
    raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)


@dataclass(frozen=True)
class ChatHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator
    resolve_command: ChatCommandResolver | None = None
    execute_generation: ChatGenerationExecution | None = None

    def _authority(self) -> tuple[Any, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    def chat_command(self, context: OperationContext) -> AuditedOperationResult:
        request: ChatCommandInput | None = None
        try:
            request = ChatCommandInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None or request.command_name not in CHAT_COMMAND_NAMES:
            # A name outside the Chat Contract's own closed command registry is a
            # malformed request, not an unimplemented one: there is no such command at
            # any version, so it cannot become servable later.
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        if context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        connection, identity, guard = self._authority()
        command = (
            None
            if self.resolve_command is None
            else self.resolve_command(request, context)
        )
        if command is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_COMMAND
            )
        if (
            request.command_name in _GENERATION_EXECUTOR_COMMANDS
            and self.execute_generation is None
        ):
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                "this service instance has no configured chat generation executor",
            )

        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        expectation = (
            None
            if request.expected_conversation is None
            else ChatAggregateExpectation(
                conversation_id=request.expected_conversation.conversation_id,
                graph_revision=request.expected_conversation.graph_revision,
                latest_conversation_sequence=(
                    request.expected_conversation.latest_conversation_sequence
                ),
            )
        )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = ChatCommandResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            # Both halves. The envelope is this operation's result and the command
            # result inside it is the Chat Contract's own governed emission, so a
            # command that answered for some other command name, or with a document
            # the Chat Contract would not publish, is refused before it is stored.
            return result.command_name == request.command_name and (
                is_governed_command_result(result.command_result)
            )

        outcome = execute_chat_command(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            command=command,
            validate_result=valid_result,
            clock=self.clock,
            expected=expectation,
            allocate_identifier=self.allocate_identifier,
        )
        if request.command_name == SUBMIT_MESSAGE_COMMAND and not outcome.replayed:
            command_id = request.command.get("commandId")
            trigger_message_id = request.command.get("newMessageId")
            if not isinstance(command_id, str):  # validated by the resolver
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            if trigger_message_id is None:
                trigger_message_id = f"{command_id}.msg"
            if not isinstance(trigger_message_id, str):  # validated by the resolver
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            executor = self.execute_generation
            if executor is None:  # guarded before the mutation
                raise OperationError(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
                )
            executor(
                queued_submission_id=f"{command_id}.sub",
                generation_job_id=f"{command_id}.gen",
                trigger_message_id=trigger_message_id,
            )
        elif request.command_name == RETRY_GENERATION_COMMAND and not outcome.replayed:
            command_id = request.command.get("commandId")
            generation_job_id = request.command.get("jobId")
            if not isinstance(command_id, str) or not isinstance(generation_job_id, str):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            executor = self.execute_generation
            if executor is None:  # guarded before the mutation
                raise OperationError(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
                )
            executor(
                generation_job_id=generation_job_id,
                generation_attempt_id=retry_generation_attempt_id(
                    command_id, generation_job_id
                ),
            )
        elif request.command_name in _FORK_GENERATION_COMMANDS and not outcome.replayed:
            command_id = request.command.get("commandId")
            if not isinstance(command_id, str):  # validated by the resolver
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            generation_job_id = f"{command_id}.gen"
            # The trigger is NOT a request field for either fork. An amendment's is the
            # new user message it committed and a regeneration's is the user parent it
            # read back from the source message, so the committed job row is the one
            # place both are stated -- and reading it also proves the job this wake
            # names is the job the command actually opened.
            job = read_generation_job(
                connection,
                workspace_id=context.workspace_id,
                generation_job_id=generation_job_id,
            )
            if job is None:
                raise OperationError(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
                )
            executor = self.execute_generation
            if executor is None:  # guarded before the mutation
                raise OperationError(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
                )
            executor(
                generation_job_id=generation_job_id,
                trigger_message_id=job.trigger_message_id,
            )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def chat_events(self, context: OperationContext) -> Mapping[str, Any]:
        request: ChatEventsInput | None = None
        try:
            request = ChatEventsInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        # A generation job this workspace does not hold answers `requires_resnapshot`
        # with `unauthorized_cursor` rather than `not_found`, because that is what the
        # seam already decided and re-deciding it here would publish the existence of a
        # job to a reader the workspace never handed one to.
        replay = replay_generation_events(
            connection,
            workspace_id=context.workspace_id,
            generation_job_id=request.generation_job_id,
            after_cursor=request.after_cursor,
        )
        events = replay.events[:_MAX_EVENTS]
        chunks: dict[tuple[str, int], GenerationTextChunk] = {}
        if any(event.event_type == _TEXT_APPENDED for event in events):
            chunks = {
                (chunk.generation_attempt_id, chunk.chunk_ordinal): chunk
                for chunk in read_generation_text_chunks(
                    connection,
                    workspace_id=context.workspace_id,
                    generation_job_id=request.generation_job_id,
                )
            }
        directions: dict[str, Direction] = {}
        if any(event.event_type == _DIRECTION_STATE_CHANGED for event in events):
            directions = {
                direction.direction_id: direction
                for direction in read_directions_for_generation_job(
                    connection,
                    workspace_id=context.workspace_id,
                    generation_job_id=request.generation_job_id,
                )
            }
        return ChatEventsResult(
            generation_job_id=request.generation_job_id,
            events=tuple(_event(event) for event in events),
            transport_events=tuple(
                _transport_event(event, chunks, directions) for event in events
            ),
            requires_resnapshot=replay.requires_resnapshot,
            resnapshot_reason=replay.reason,
        ).to_wire()

    def chat_snapshot(self, context: OperationContext) -> Mapping[str, Any]:
        request: ChatSnapshotInput | None = None
        try:
            request = ChatSnapshotInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        return resolve_chat_snapshot(connection, request, context)

    def chat_draft(self, context: OperationContext) -> Mapping[str, Any]:
        """One `chat.draft` read-back (C5A).

        The same shape `chat_snapshot` is: a pure read behind the one authority
        check, with the whole domain -- query decode, envelope agreement and the
        found/absent answer -- owned by
        :func:`~service.chat_snapshot.resolve_draft_readback`.
        """
        request: ChatDraftInput | None = None
        try:
            request = ChatDraftInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        return resolve_draft_readback(connection, request, context)


__all__ = [
    "CHAT_COMMAND_OPERATION",
    "CHAT_DRAFT_OPERATION",
    "CHAT_EVENTS_OPERATION",
    "CHAT_FAMILY_OPERATIONS",
    "CHAT_SNAPSHOT_OPERATION",
    "ChatCommandResolver",
    "ChatGenerationExecution",
    "ChatHandlers",
]
