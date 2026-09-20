"""Production handlers for ``chat.command`` and ``chat.events``.

Two operations, and neither of them owns any Chat rule of its own. `chat.events` is
the wire projection of :func:`~service.chat_generation.replay_generation_events`;
`chat.command` is the catalogue's front door onto
:func:`~service.chat_command.execute_chat_command`, which is itself the one mutation
seam composed over the Chat repository. Everything that decides a request -- the
twelve authorization checks, the server-issued grant, the fence, the audit, the claim
and the proved replay -- already happened by the time either of these runs.

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

from omnivia_core.chat_contract.v1 import CHAT_COMMAND_NAMES
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
    ChatEventsInput,
    ChatEventsResult,
    ChatGenerationEvent,
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
from omnivia_core_runtime.service.chat_submit import SUBMIT_MESSAGE_COMMAND
from omnivia_core_runtime.service.mutation import issue_mutation_grant
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    application_refusal,
)
from omnivia_core_runtime.storage.chat import GenerationEvent
from omnivia_core_runtime.storage.memory import IdentifierAllocator

CHAT_COMMAND_OPERATION: Final = "chat.command"
CHAT_EVENTS_OPERATION: Final = "chat.events"
CHAT_FAMILY_OPERATIONS: Final = frozenset(
    {CHAT_COMMAND_OPERATION, CHAT_EVENTS_OPERATION}
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this chat operation"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative chat storage"
)
_MESSAGE_NO_COMMAND: Final = (
    "this build serves no implementation of the chat command this request names"
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
            request.command_name == SUBMIT_MESSAGE_COMMAND
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
        if request.command_name == SUBMIT_MESSAGE_COMMAND:
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
        return ChatEventsResult(
            generation_job_id=request.generation_job_id,
            events=tuple(_event(event) for event in replay.events),
            requires_resnapshot=replay.requires_resnapshot,
            resnapshot_reason=replay.reason,
        ).to_wire()


__all__ = [
    "CHAT_COMMAND_OPERATION",
    "CHAT_EVENTS_OPERATION",
    "CHAT_FAMILY_OPERATIONS",
    "ChatCommandResolver",
    "ChatGenerationExecution",
    "ChatHandlers",
]
