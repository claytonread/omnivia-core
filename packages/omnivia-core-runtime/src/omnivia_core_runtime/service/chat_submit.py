"""The production `SubmitMessage` resolver behind `chat.command` (W5 GB-01).

`handlers/chat.py` takes a :data:`~service.handlers.chat.ChatCommandResolver` and every
build before this one supplied none, so an authorized, granted, well-formed
`chat.command` refused with `dependency_unavailable`. This module is the first real
resolver: it turns one `SubmitMessage` request document into the
:data:`~service.chat_command.ChatCommand` that `execute_chat_command` settles, and
returns `None` for everything else, so every other Chat command keeps exactly the
refusal it has today.

**What one accepted `SubmitMessage` writes**, in the order migration 0029 admits and
all inside the seam's single fenced transaction: the conversation's
`latest_conversation_sequence` moves (a message may not point past it), the user
message and its parts are appended, the durable branch head event is appended, the
branch projection that event backs is advanced, one queued-submission row and its
queued generation job are opened, and one transactional outbox row announces the
whole thing. `graph_revision`
deliberately does not move: `omnivia_chat_branch_head_events.graph_revision`
foreign-keys the conversation's single row through a non-deferrable key, so advancing
it would orphan the head event this command just wrote and every earlier one.

**What it deliberately does not do.** No provider is invoked, contacted or recorded.
0028's `omnivia_provider_invocations` requires a `generation_attempt_id`, and a
submission has no attempt: the job is `queued` and the attempt is opened by whoever
claims it (`service/chat_generation.py`). Writing an invocation row here would be a
record of a call that has not happened. It also opens no network connection, adds no
SDK, and registers no operation of its own.

**The bounded variant this build serves.** `SubmitMessageRequest` admits attachment
and context references, a `targetReference` and a `fromQueuedSubmissionId`; none of
those has an implementation in this repository -- reference revalidation, another
authority's target, and the queued-submission claim walk respectively -- so a request
carrying one is refused as `dependency_unavailable` rather than served with the field
silently dropped. A structurally invalid document is a different refusal
(`invalid_request`) and is decided before any grant is issued.

**Identifiers are derived from the caller's own `commandId`**, not allocated randomly,
so the rows one command wrote are addressable by the caller that wrote them without a
second read-back operation this contract does not have. That is also what makes a
`commandId` replayed under a *different* idempotency key refuse rather than duplicate:
the branch head it expects has already moved.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.chat_contract.v1 import (
    ChatContractDecodeError,
    CommandResultEnvelope,
    UnsupportedProtocolVersionError,
    negotiate_protocol_version,
    to_canonical_json,
)
from omnivia_core.contracts.v1 import ERROR_CODE_INVALID_REQUEST
from omnivia_core.contracts.v1.generated import ChatCommandInput, ChatCommandResult
from omnivia_core_runtime.service.chat_command import ChatAggregateConflict, ChatCommand
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.chat import ChatWriter, read_branch, read_conversation

__all__ = ["SUBMIT_MESSAGE_COMMAND", "resolve_chat_command"]

SUBMIT_MESSAGE_COMMAND: Final = "SubmitMessage"

#: The domain event kind the outbox row carries. A member of the Chat contract's own
#: durable event vocabulary rather than a name invented here, because the delivery
#: worker that reads this row publishes it as that event.
_OUTBOX_EVENT_KIND: Final = "chat.message.committed"

#: `common.schema.json#/$defs/WorkspaceScopedId`, which is also exactly what 0029's own
#: identifier `CHECK`s admit. One pattern rather than two that could drift apart.
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
#: `common.schema.json#/$defs/OpenCode`, the part type's own open registry.
_PART_TYPE: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
_VISIBILITY: Final = frozenset({"standard", "user_only", "internal"})
_TARGET_KINDS: Final = frozenset({"app", "workflow", "agent"})

#: The longest identifier this module may be handed and still derive a durable one from
#: it: 128 minus the longest suffix appended below (`.p4095`).
_DERIVED_IDENTIFIER_MAX: Final = 122
#: 0029's own bound on `omnivia_chat_message_parts.payload_json`.
_MAX_PART_PAYLOAD_BYTES: Final = 262_144
_MAX_REFERENCE_COUNT: Final = 64
_MAX_PARTS: Final = 4096
_MAX_OPEN_CODE: Final = 128
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991

_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "branchId",
        "actorId",
        "expectedHeadMessageId",
        "expectedHeadVersion",
        "newMessageId",
        "editableParts",
        "attachmentReferences",
        "contextReferences",
        "targetReference",
        "fromQueuedSubmissionId",
        "clientCorrelationHint",
    }
)
#: The optional fields whose *presence* names work this build cannot perform. Separate
#: from the unknown-field check: these are contract members, and a request carrying one
#: is unimplemented rather than malformed.
_UNSUPPORTED_FIELDS: Final = ("targetReference", "fromQueuedSubmissionId")
_UNSUPPORTED_REFERENCES: Final = ("attachmentReferences", "contextReferences")
_PART_FIELDS: Final = frozenset({"type", "payload", "visibility"})
_REFERENCE_FIELDS: Final = frozenset({"referenceId", "sourceRevision"})
_TARGET_FIELDS: Final = frozenset({"kind", "referenceId"})

_MESSAGE_CONVERSATION: Final = (
    "the conversation this command names is not an active conversation of this workspace"
)
_MESSAGE_BRANCH: Final = (
    "the branch this command names is not an open branch of that conversation"
)
_MESSAGE_STALE_HEAD: Final = (
    "the branch is not at the head this command expects to append to"
)


def _invalid(field: str, rule: str) -> OperationError:
    """A decode refusal naming the field and the rule, and never the value.

    The refused document is the one most likely to be carrying content this service
    should not echo, so the diagnostic is built from two constants and a field name --
    all three of which are the contract's, not the caller's.
    """
    return OperationError(
        ERROR_CODE_INVALID_REQUEST,
        f"the chat command field {field!r} {rule}",
    )


# --- the request document ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Part:
    part_type: str
    visibility: str
    payload: Mapping[str, Any]
    content_hash: str


@dataclass(frozen=True, slots=True)
class _SubmitMessage:
    """One decoded `SubmitMessageRequest`, in the variant this build serves."""

    command_id: str
    workspace_id: str
    conversation_id: str
    branch_id: str
    actor_id: str
    expected_head_message_id: str
    expected_head_version: int
    message_id: str
    parts: tuple[_Part, ...]
    content_hash: str


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return value
    return [_plain_json(item) for item in value]


def _identifier(document: Mapping[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _invalid(field, "is not a workspace-scoped identifier")
    return value


def _derivable_identifier(document: Mapping[str, Any], field: str) -> str:
    value = _identifier(document, field)
    if len(value) > _DERIVED_IDENTIFIER_MAX:
        # This build derives every durable identifier one submission needs from these
        # two, and 0029 bounds a durable identifier at 128 characters. Refusing here is
        # what keeps the derivation from silently truncating one.
        raise _invalid(
            field, f"is longer than the {_DERIVED_IDENTIFIER_MAX} characters this build "
            "can derive durable identifiers from"
        )
    return value


def _canonical(payload: object, field: str) -> tuple[Mapping[str, Any], str]:
    """One part payload, and the exact canonical JSON its content hash is taken over."""
    if not isinstance(payload, Mapping):
        raise _invalid(field, "is not a JSON object")
    plain = _plain_json(payload)
    if not isinstance(plain, Mapping):  # pragma: no cover - guarded above
        raise _invalid(field, "is not a JSON object")
    try:
        document = to_canonical_json(plain)
    except ChatContractDecodeError as error:
        raise _invalid(field, "is not representable as canonical JSON") from error
    if len(document.encode("utf-8")) > _MAX_PART_PAYLOAD_BYTES:
        raise _invalid(field, "is larger than a durable message part payload may be")
    return plain, document


def _part(item: object, index: int) -> tuple[_Part, str]:
    field = f"editableParts[{index}]"
    if not isinstance(item, Mapping):
        raise _invalid(field, "is not a JSON object")
    unknown = sorted(set(item) - _PART_FIELDS)
    if unknown:
        raise _invalid(f"{field}.{unknown[0]}", "is not a field of this command")
    part_type = item.get("type")
    if (
        not isinstance(part_type, str)
        or len(part_type) > _MAX_OPEN_CODE
        or _PART_TYPE.fullmatch(part_type) is None
    ):
        raise _invalid(f"{field}.type", "is not an open part-type code")
    visibility = item.get("visibility", "standard")
    if visibility not in _VISIBILITY:
        raise _invalid(f"{field}.visibility", "is not a governed message visibility")
    if "payload" not in item:
        raise _invalid(f"{field}.payload", "is required")
    payload, document = _canonical(item["payload"], f"{field}.payload")
    canonical = to_canonical_json(
        {"type": part_type, "visibility": visibility, "payload": dict(payload)}
    )
    return (
        _Part(
            part_type=part_type,
            visibility=str(visibility),
            payload=payload,
            content_hash=_digest(document),
        ),
        canonical,
    )


def _digest(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _array(
    document: Mapping[str, Any], field: str, *, max_items: int
) -> Sequence[Any]:
    value = document.get(field)
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise _invalid(field, f"is not an array of up to {max_items} items")
    if len(value) > max_items:
        raise _invalid(field, f"has more than {max_items} items")
    return value


def _bounded_optional_text(
    item: Mapping[str, Any], name: str, path: str, *, max_length: int
) -> None:
    if name not in item:
        return
    value = item[name]
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise _invalid(path, f"is not a string of one to {max_length} characters")


def _reference(item: object, field: str) -> None:
    if not isinstance(item, Mapping):
        raise _invalid(field, "is not a JSON object")
    unknown = sorted(set(item) - _REFERENCE_FIELDS)
    if unknown:
        raise _invalid(f"{field}.{unknown[0]}", "is not a field of this reference")
    _identifier(item, "referenceId")
    _bounded_optional_text(
        item, "sourceRevision", f"{field}.sourceRevision", max_length=128
    )


def _target_reference(value: object) -> None:
    field = "targetReference"
    if not isinstance(value, Mapping):
        raise _invalid(field, "is not a JSON object")
    unknown = sorted(set(value) - _TARGET_FIELDS)
    if unknown:
        raise _invalid(f"{field}.{unknown[0]}", "is not a field of this reference")
    kind = value.get("kind")
    if kind not in _TARGET_KINDS:
        raise _invalid(f"{field}.kind", "is not a governed target kind")
    _identifier(value, "referenceId")


def _editable_part_wire(part: _Part) -> Mapping[str, Any]:
    return {
        "type": part.part_type,
        "visibility": part.visibility,
        "payload": _plain_json(part.payload),
    }


def _decode(command: Mapping[str, Any]) -> _SubmitMessage:
    """Decode one `SubmitMessageRequest`, or refuse it as an invalid request.

    Held to `commands.schema.json#/$defs/SubmitMessageRequest` field by field, including
    its closed field set: the generated codec exposes no `SubmitMessageRequest` type and
    the generated validator carries no subschema for it, so this is the tight local
    decoder over those frozen fields rather than a second, looser reading of them.
    """
    unknown = sorted(set(command) - _FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error

    attachments = _array(
        command, "attachmentReferences", max_items=_MAX_REFERENCE_COUNT
    )
    contexts = _array(command, "contextReferences", max_items=_MAX_REFERENCE_COUNT)
    for index, item in enumerate(attachments):
        _reference(item, f"attachmentReferences[{index}]")
    for index, item in enumerate(contexts):
        _reference(item, f"contextReferences[{index}]")
    if "targetReference" in command:
        _target_reference(command["targetReference"])
    if "fromQueuedSubmissionId" in command:
        _identifier(command, "fromQueuedSubmissionId")

    expected_head_version = command.get("expectedHeadVersion")
    if (
        not isinstance(expected_head_version, int)
        or isinstance(expected_head_version, bool)
        or expected_head_version < 1
        or expected_head_version > _MAX_SAFE_INTEGER
    ):
        raise _invalid("expectedHeadVersion", "is not a head version")

    parts = command.get("editableParts")
    if (
        not isinstance(parts, Sequence)
        or isinstance(parts, (str, bytes, Mapping))
        or not 1 <= len(parts) <= _MAX_PARTS
    ):
        raise _invalid("editableParts", "is not an array of one to 4096 parts")
    decoded = tuple(_part(item, index) for index, item in enumerate(parts))

    command_id = _derivable_identifier(command, "commandId")
    message_id = (
        f"{command_id}.msg"
        if "newMessageId" not in command
        else _derivable_identifier(command, "newMessageId")
    )
    if "clientCorrelationHint" in command:
        # Validated and then dropped: the hint is inert by contract, and the frozen
        # `CommandResultEnvelope` this command answers with has nowhere to echo it.
        _identifier(command, "clientCorrelationHint")
    return _SubmitMessage(
        command_id=command_id,
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        branch_id=_identifier(command, "branchId"),
        actor_id=_identifier(command, "actorId"),
        expected_head_message_id=_identifier(command, "expectedHeadMessageId"),
        expected_head_version=expected_head_version,
        message_id=message_id,
        parts=tuple(part for part, _ in decoded),
        content_hash=_digest(f"[{','.join(canonical for _, canonical in decoded)}]"),
    )


# --- the command ---------------------------------------------------------------------


def _next_outbox_cursor(connection: sqlite3.Connection, workspace_id: str) -> int:
    """The cursor 0029's own INSERT guard requires the next outbox row to carry."""
    row = connection.execute(
        "SELECT COALESCE(MAX(outbox_cursor), 0) + 1 "
        "FROM omnivia_chat_transactional_outbox WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return int(row[0])


def _next_queue_sequence(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> int:
    """The queue position 0029 requires for the next submission in this conversation."""
    row = connection.execute(
        "SELECT COALESCE(MAX(queue_sequence), 0) + 1 "
        "FROM omnivia_chat_queued_submissions "
        "WHERE workspace_id = ? AND conversation_id = ?",
        (workspace_id, conversation_id),
    ).fetchone()
    return int(row[0])


def _submit(request: _SubmitMessage) -> ChatCommand:
    """One decoded submission, as the command the mutation seam runs."""

    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        now = settlement.settled_at_us
        conversation = read_conversation(
            writer.connection,
            workspace_id=writer.workspace_id,
            conversation_id=request.conversation_id,
        )
        if conversation is None or conversation.state != "active":
            raise ChatAggregateConflict(_MESSAGE_CONVERSATION)
        branch = read_branch(
            writer.connection,
            workspace_id=writer.workspace_id,
            branch_id=request.branch_id,
        )
        if (
            branch is None
            or branch.conversation_id != request.conversation_id
            or branch.state != "open"
        ):
            raise ChatAggregateConflict(_MESSAGE_BRANCH)
        if (
            branch.head_version != request.expected_head_version
            or branch.current_head_message_id != request.expected_head_message_id
        ):
            raise ChatAggregateConflict(_MESSAGE_STALE_HEAD)

        sequence = conversation.latest_conversation_sequence + 1
        head_version = branch.head_version + 1
        queued_submission_id = f"{request.command_id}.sub"
        generation_job_id = f"{request.command_id}.gen"

        writer.update_conversation(
            conversation_id=request.conversation_id,
            expected_graph_revision=conversation.graph_revision,
            graph_revision=conversation.graph_revision,
            latest_conversation_sequence=sequence,
            state="active",
            title=conversation.title,
            title_source=conversation.title_source,
            default_branch_id=conversation.default_branch_id,
            updated_at_us=now,
        )
        writer.append_message(
            message_id=request.message_id,
            conversation_id=request.conversation_id,
            role="user",
            author_type="human",
            author_id=request.actor_id,
            parent_message_id=request.expected_head_message_id,
            conversation_sequence=sequence,
            schema_version=1,
            content_hash=request.content_hash,
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=request.branch_id,
            created_at_us=now,
            committed_at_us=now,
        )
        for index, part in enumerate(request.parts):
            writer.append_message_part(
                part_id=f"{request.message_id}.p{index}",
                message_id=request.message_id,
                conversation_id=request.conversation_id,
                part_index=index,
                part_type=part.part_type,
                schema_version=1,
                visibility=part.visibility,
                payload=part.payload,
                content_hash=part.content_hash,
                created_at_us=now,
                provenance="human",
            )
        writer.append_branch_head_event(
            event_id=f"{request.command_id}.head",
            conversation_id=request.conversation_id,
            branch_id=request.branch_id,
            head_version=head_version,
            previous_head_message_id=request.expected_head_message_id,
            new_head_message_id=request.message_id,
            cause="user_message_appended",
            # The seam's own claim identity, so the durable head event names the exact
            # mutation claim that settled it.
            command_id=settlement.claim_id,
            graph_revision=conversation.graph_revision,
            conversation_sequence=sequence,
            actor_id=request.actor_id,
            occurred_at_us=now,
            schema_version=1,
        )
        writer.update_branch_head(
            branch_id=request.branch_id,
            expected_head_version=branch.head_version,
            head_version=head_version,
            current_head_message_id=request.message_id,
            state="open",
        )
        writer.append_queued_submission(
            queued_submission_id=queued_submission_id,
            conversation_id=request.conversation_id,
            actor_id=request.actor_id,
            queue_sequence=_next_queue_sequence(
                writer.connection,
                workspace_id=writer.workspace_id,
                conversation_id=request.conversation_id,
            ),
            branch_id=request.branch_id,
            editable_parts=tuple(_editable_part_wire(part) for part in request.parts),
            references=(),
            idempotency_key=request.command_id,
            created_at_us=now,
            updated_at_us=now,
        )
        writer.append_generation_job(
            generation_job_id=generation_job_id,
            conversation_id=request.conversation_id,
            branch_id=request.branch_id,
            trigger_message_id=request.message_id,
            graph_revision_observed=conversation.graph_revision,
            idempotency_key=request.command_id,
            schema_version=1,
            created_at_us=now,
            updated_at_us=now,
        )
        writer.append_outbox_entry(
            outbox_cursor=_next_outbox_cursor(writer.connection, writer.workspace_id),
            domain_event_id=f"{request.command_id}.evt",
            event_kind=_OUTBOX_EVENT_KIND,
            payload={
                "conversationId": request.conversation_id,
                "branchId": request.branch_id,
                "messageId": request.message_id,
                "parentMessageId": request.expected_head_message_id,
                "conversationSequence": sequence,
                "graphRevision": conversation.graph_revision,
                "headVersion": head_version,
                "role": "user",
                "queuedSubmissionId": queued_submission_id,
                "generationJobId": generation_job_id,
            },
            created_at_us=now,
            conversation_id=request.conversation_id,
            generation_job_id=generation_job_id,
        )
        return ChatCommandResult(
            command_name=SUBMIT_MESSAGE_COMMAND,
            command_result=CommandResultEnvelope(
                command_id=request.command_id, status="completed"
            ).to_wire(),
            conversation_id=request.conversation_id,
        ).to_wire()

    return command


def resolve_chat_command(
    request: ChatCommandInput, context: OperationContext
) -> ChatCommand | None:
    """The production resolver: `SubmitMessage`, and `None` for every other command.

    `None` is a refusal the handler renders as `dependency_unavailable`, and it is the
    honest answer for a command this build has no implementation of -- including a
    `SubmitMessage` variant that names work no authority here can do. A document that is
    not a `SubmitMessage` at all raises `invalid_request` instead, before a grant is
    issued and before a transaction is opened, so a malformed request writes nothing.

    The workspace the command document names must be the one the request was authorized
    for. It is checked rather than trusted or ignored: the envelope is workspace-scoped
    through the request's selected workspace, and a payload naming a second one is a
    request this seam cannot settle honestly under either.
    """
    if request.command_name != SUBMIT_MESSAGE_COMMAND:
        return None
    decoded = _decode(request.command)
    if decoded.workspace_id != context.workspace_id:
        raise _invalid("workspaceId", "is not the workspace this request was authorized for")
    if any(field in request.command for field in _UNSUPPORTED_FIELDS) or any(
        request.command.get(field) for field in _UNSUPPORTED_REFERENCES
    ):
        return None
    return _submit(decoded)
