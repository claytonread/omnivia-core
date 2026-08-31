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

**Cold start: `CreateConversation`, then a branchless first `SubmitMessage`.** REF-042
§9.1 gives a new Conversation no branch until its first committed user Message, so
`CreateConversation` writes exactly one active conversation row -- no branch, no
message, no outbox fact -- and invents neither a synthetic root message nor a branch
to hang one on. The first
`SubmitMessage` into that conversation is then the request that has no head to state:
the contract's expected-head group is all-or-none, and a request that omits all three
is served only when the named active conversation genuinely has no default branch and
no branches. That send creates the root user message, the `original` branch at head
version 1, its `branch_created` head event, the actor's view state selecting it, and
the same queued submission, generation job and outbox facts a continuation writes --
one transaction, one executor, one set of rules. A request that omits *part* of the
group is refused as malformed: a caller stating one of the three has a view of a head,
and the two it left out are exactly the halves that would then go unchecked.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from omnivia_core.chat_contract.v1 import (
    ChatContractDecodeError,
    CommandResultEnvelope,
    UnsupportedProtocolVersionError,
    negotiate_protocol_version,
    to_canonical_json,
)
from omnivia_core.contracts.v1 import ERROR_CODE_INVALID_REQUEST
from omnivia_core.contracts.v1.generated import (
    ChatCommandInput,
    ChatCommandResult,
    ChatConversationExpectation,
)
from omnivia_core_runtime.service.chat_command import ChatAggregateConflict, ChatCommand
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.chat import (
    ChatWriter,
    QueuedSubmission,
    StaleVersion,
    read_active_queue_for_actor,
    read_branch,
    read_conversation,
    read_generation_attempt_outcome,
    read_generation_attempts,
    read_generation_events,
    read_generation_job,
    read_generation_job_status_projection,
    read_queue_order_for_conversation,
    read_queue_order_projection,
    read_queued_submission,
)

__all__ = [
    "CANCEL_QUEUED_SUBMISSION_COMMAND",
    "CREATE_CONVERSATION_COMMAND",
    "ENQUEUE_MESSAGE_COMMAND",
    "REORDER_QUEUED_SUBMISSION_COMMAND",
    "RETRY_GENERATION_COMMAND",
    "STOP_GENERATION_COMMAND",
    "SUBMIT_MESSAGE_COMMAND",
    "UPDATE_QUEUED_SUBMISSION_COMMAND",
    "resolve_chat_command",
    "retry_generation_attempt_id",
]

CANCEL_QUEUED_SUBMISSION_COMMAND: Final = "CancelQueuedSubmission"
CREATE_CONVERSATION_COMMAND: Final = "CreateConversation"
ENQUEUE_MESSAGE_COMMAND: Final = "EnqueueMessage"
REORDER_QUEUED_SUBMISSION_COMMAND: Final = "ReorderQueuedSubmission"
RETRY_GENERATION_COMMAND: Final = "RetryGeneration"
STOP_GENERATION_COMMAND: Final = "StopGeneration"
SUBMIT_MESSAGE_COMMAND: Final = "SubmitMessage"
UPDATE_QUEUED_SUBMISSION_COMMAND: Final = "UpdateQueuedSubmission"

#: `queued-submission:<id>` is the one opaque `resultRef` shape this build mints
#: (D07 `CommandResultEnvelope.resultRef`), naming the row `EnqueueMessage` just
#: wrote so a caller can address it without a second read-back operation.
_QUEUED_SUBMISSION_RESULT_REF_PREFIX: Final = "queued-submission:"

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
#: `commands.schema.json#/$defs/CreateConversationRequest.title`. 0029 bounds the
#: stored column at 2048 *bytes*, which 512 characters can never exceed.
_MAX_TITLE_LENGTH: Final = 512
#: The revision a created conversation starts at. One, not zero: the conversation row
#: is itself the first durable structural graph fact (REF-042 §4.19), and every later
#: branch head event this build writes cites the conversation's revision through 0029's
#: non-deferrable foreign key rather than moving it.
_INITIAL_GRAPH_REVISION: Final = 1
#: The append position a created conversation starts at. Zero, because nothing has been
#: appended to it yet: `CreateConversation` writes no branch and no message.
_INITIAL_CONVERSATION_SEQUENCE: Final = 0
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
#: `commands.schema.json#/$defs/SubmitMessageRequest`'s all-or-none expected-head
#: group. Present in full is the continuation this command has always been; absent in
#: full is the first send into a conversation REF-042 §9.1 leaves branchless.
_HEAD_GROUP: Final = ("branchId", "expectedHeadMessageId", "expectedHeadVersion")
_CREATE_CONVERSATION_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "actorId",
        "title",
        "requestedAt",
    }
)
_REORDER_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "actorId",
        "queuedSubmissionId",
        "targetPosition",
        "expectedVersion",
    }
)
_CANCEL_QUEUE_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "actorId",
        "queuedSubmissionId",
    }
)
_STOP_GENERATION_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "actorId",
        "jobId",
    }
)
_RETRY_GENERATION_FIELDS: Final = _STOP_GENERATION_FIELDS
_ENQUEUE_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "branchId",
        "actorId",
        "editableParts",
        "attachmentReferences",
        "contextReferences",
        "targetReference",
    }
)
_UPDATE_QUEUE_FIELDS: Final = frozenset(
    {
        "protocolVersion",
        "commandId",
        "workspaceId",
        "conversationId",
        "actorId",
        "queuedSubmissionId",
        "editableParts",
        "expectedVersion",
    }
)
#: The optional fields whose *presence* names work this build cannot perform. Separate
#: from the unknown-field check: these are contract members, and a request carrying one
#: is unimplemented rather than malformed.
_UNSUPPORTED_FIELDS: Final = ("targetReference",)
_UNSUPPORTED_REFERENCES: Final = ("attachmentReferences", "contextReferences")
#: `EnqueueMessageRequest` carries the same unsupported `targetReference` member as
#: `SubmitMessageRequest`; its non-empty references are refused by the same rule.
_ENQUEUE_UNSUPPORTED_FIELDS: Final = ("targetReference",)
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
_MESSAGE_ALREADY_BRANCHED: Final = (
    "the conversation this command names already has a branch, so a first send into it "
    "would append past a head it did not state"
)
_MESSAGE_CONVERSATION_EXISTS: Final = (
    "the conversation this command would create already exists in this workspace"
)
_MESSAGE_QUEUE_ROW: Final = (
    "the queued submission this command names is not a queued row of that conversation "
    "and actor"
)
_MESSAGE_QUEUE_CONTENT: Final = (
    "the queued submission is not updatable, or the request does not exactly match its "
    "current content, identity or head"
)
_MESSAGE_QUEUE_DRAINED: Final = (
    "the queued submission this command names has already been drained by another "
    "SubmitMessage"
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
    """One decoded `SubmitMessageRequest`, in the variant this build serves.

    The three expected-head members are `None` together or set together, never one
    without the others: :func:`_decode` refuses a partial group before constructing
    this, so `branch_id is None` is the whole first-send test the command needs.
    """

    command_id: str
    workspace_id: str
    conversation_id: str
    branch_id: str | None
    actor_id: str
    expected_head_message_id: str | None
    expected_head_version: int | None
    message_id: str
    parts: tuple[_Part, ...]
    content_hash: str
    from_queued_submission_id: str | None


@dataclass(frozen=True, slots=True)
class _EnqueueMessage:
    """One decoded `EnqueueMessageRequest`, in the variant this build serves.

    The Gate B slice this build serves is always a continuation: `branchId` is
    required by the frozen request schema itself (unlike `SubmitMessage`'s
    all-or-none group), so an offline queue row always names the open branch it
    was composed against and this command never fabricates a branch or a head.
    """

    command_id: str
    workspace_id: str
    conversation_id: str
    branch_id: str
    actor_id: str
    parts: tuple[_Part, ...]


@dataclass(frozen=True, slots=True)
class _UpdateQueuedSubmission:
    command_id: str
    workspace_id: str
    conversation_id: str
    actor_id: str
    queued_submission_id: str
    parts: tuple[_Part, ...]
    expected_version: int


@dataclass(frozen=True, slots=True)
class _CreateConversation:
    """One decoded `CreateConversationRequest`."""

    command_id: str
    workspace_id: str
    actor_id: str
    title: str | None


@dataclass(frozen=True, slots=True)
class _ReorderQueuedSubmission:
    command_id: str
    workspace_id: str
    conversation_id: str
    actor_id: str
    queued_submission_id: str
    target_position: int
    expected_version: int


@dataclass(frozen=True, slots=True)
class _CancelQueuedSubmission:
    command_id: str
    workspace_id: str
    conversation_id: str
    actor_id: str
    queued_submission_id: str


@dataclass(frozen=True, slots=True)
class _StopGeneration:
    command_id: str
    workspace_id: str
    conversation_id: str
    actor_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class _RetryGeneration:
    command_id: str
    workspace_id: str
    conversation_id: str
    actor_id: str
    job_id: str


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


def _positive_integer(
    document: Mapping[str, Any], field: str, *, maximum: int = _MAX_SAFE_INTEGER
) -> int:
    value = document.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise _invalid(field, f"is not an integer from 1 to {maximum}")
    return value


def _array(
    document: Mapping[str, Any], field: str, *, max_items: int
) -> Sequence[Any]:
    value = document.get(field)
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise _invalid(field, f"is not an array of up to {max_items} items")
    if len(value) > max_items:
        raise _invalid(field, f"has more than {max_items} items")
    return value


def _utc_timestamp(document: Mapping[str, Any], field: str) -> None:
    """Assert one governed `Timestamp`: RFC 3339 with an explicit offset.

    Asserted rather than annotated, and asserted rather than kept: this is a required
    member of the command's closed field set, so a document carrying something that is
    not an instant is malformed even though nothing durable is written from it.
    """
    value = document.get(field)
    if not isinstance(value, str):
        raise _invalid(field, "is not an RFC 3339 timestamp")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise _invalid(field, "is not an RFC 3339 timestamp") from error
    if moment.tzinfo is None:
        raise _invalid(field, "is not an RFC 3339 timestamp")


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
    from_queued_submission_id = (
        None
        if "fromQueuedSubmissionId" not in command
        else _identifier(command, "fromQueuedSubmissionId")
    )

    # The expected-head group, all-or-none. A partial group names the *missing*
    # member and the rule, because that is the field the caller has to supply; the
    # ones it did state are not the defect.
    stated = [field for field in _HEAD_GROUP if field in command]
    if stated and len(stated) != len(_HEAD_GROUP):
        missing = next(field for field in _HEAD_GROUP if field not in command)
        raise _invalid(
            missing,
            "is required whenever any of the expected-head group is stated, and "
            "forbidden when none of it is",
        )
    first_send = not stated

    expected_head_version: int | None = None
    if not first_send:
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
        branch_id=None if first_send else _identifier(command, "branchId"),
        actor_id=_identifier(command, "actorId"),
        expected_head_message_id=(
            None if first_send else _identifier(command, "expectedHeadMessageId")
        ),
        expected_head_version=expected_head_version,
        message_id=message_id,
        parts=tuple(part for part, _ in decoded),
        content_hash=_digest(f"[{','.join(canonical for _, canonical in decoded)}]"),
        from_queued_submission_id=from_queued_submission_id,
    )


def _decode_enqueue(command: Mapping[str, Any]) -> _EnqueueMessage:
    """Decode one `EnqueueMessageRequest`, or refuse it as an invalid request.

    Held to `commands.schema.json#/$defs/EnqueueMessageRequest`'s exact closed
    field set, reusing `_part`/`_reference`/`_target_reference` -- the same
    governed per-field rules `SubmitMessage` already validates editable parts and
    references with, so an accepted `EnqueueMessageRequest` and an accepted
    `SubmitMessageRequest` can never disagree about what a valid part is.
    """
    unknown = sorted(set(command) - _ENQUEUE_FIELDS)
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

    parts = command.get("editableParts")
    if (
        not isinstance(parts, Sequence)
        or isinstance(parts, (str, bytes, Mapping))
        or not 1 <= len(parts) <= _MAX_PARTS
    ):
        raise _invalid("editableParts", "is not an array of one to 4096 parts")
    decoded = tuple(_part(item, index) for index, item in enumerate(parts))

    return _EnqueueMessage(
        command_id=_derivable_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        branch_id=_identifier(command, "branchId"),
        actor_id=_identifier(command, "actorId"),
        parts=tuple(part for part, _ in decoded),
    )


def _decode_update_queue(command: Mapping[str, Any]) -> _UpdateQueuedSubmission:
    """Decode one `UpdateQueuedSubmissionRequest`, or refuse it as an invalid request."""
    unknown = sorted(set(command) - _UPDATE_QUEUE_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error

    parts = command.get("editableParts")
    if (
        not isinstance(parts, Sequence)
        or isinstance(parts, (str, bytes, Mapping))
        or not 1 <= len(parts) <= _MAX_PARTS
    ):
        raise _invalid("editableParts", "is not an array of one to 4096 parts")
    decoded = tuple(_part(item, index) for index, item in enumerate(parts))

    return _UpdateQueuedSubmission(
        command_id=_derivable_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        actor_id=_identifier(command, "actorId"),
        queued_submission_id=_identifier(command, "queuedSubmissionId"),
        parts=tuple(part for part, _ in decoded),
        expected_version=_positive_integer(command, "expectedVersion"),
    )


def _decode_create_conversation(command: Mapping[str, Any]) -> _CreateConversation:
    """Decode one `CreateConversationRequest`, or refuse it as an invalid request.

    Held to `commands.schema.json#/$defs/CreateConversationRequest`'s exact closed
    field set. `requestedAt` is validated and then dropped: it is the caller's own
    clock, and every durable instant this command writes is the seam's settlement
    time, so keeping it would be recording a time this service did not observe.
    """
    unknown = sorted(set(command) - _CREATE_CONVERSATION_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error
    _utc_timestamp(command, "requestedAt")
    title = command.get("title")
    if "title" in command and (
        not isinstance(title, str)
        # Zero is the floor the contract actually states: `CreateConversationRequest`
        # carries no `minLength`, and `graph.schema.json#/$defs/Conversation.title`
        # sets it to `0` outright, so an empty title is a title the domain record can
        # hold and not a document this decoder may refuse.
        or len(title) > _MAX_TITLE_LENGTH
        # 0029 refuses a title carrying a NUL outright, and a `CHECK` tripped inside
        # the mutation is an integrity failure where this is a malformed request.
        or "\x00" in title
    ):
        raise _invalid(
            "title", f"is not a string of at most {_MAX_TITLE_LENGTH} characters"
        )
    return _CreateConversation(
        command_id=_derivable_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        actor_id=_identifier(command, "actorId"),
        title=None if "title" not in command else str(title),
    )


def _decode_reorder(command: Mapping[str, Any]) -> _ReorderQueuedSubmission:
    unknown = sorted(set(command) - _REORDER_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error
    return _ReorderQueuedSubmission(
        command_id=_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        actor_id=_identifier(command, "actorId"),
        queued_submission_id=_identifier(command, "queuedSubmissionId"),
        target_position=_positive_integer(command, "targetPosition", maximum=100_000),
        expected_version=_positive_integer(command, "expectedVersion"),
    )


def _decode_cancel_queue(command: Mapping[str, Any]) -> _CancelQueuedSubmission:
    unknown = sorted(set(command) - _CANCEL_QUEUE_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error
    return _CancelQueuedSubmission(
        command_id=_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        actor_id=_identifier(command, "actorId"),
        queued_submission_id=_identifier(command, "queuedSubmissionId"),
    )


def _decode_stop_generation(command: Mapping[str, Any]) -> _StopGeneration:
    unknown = sorted(set(command) - _STOP_GENERATION_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error
    return _StopGeneration(
        command_id=_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        actor_id=_identifier(command, "actorId"),
        job_id=_identifier(command, "jobId"),
    )


def _decode_retry_generation(command: Mapping[str, Any]) -> _RetryGeneration:
    unknown = sorted(set(command) - _RETRY_GENERATION_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this command")
    protocol_version = command.get("protocolVersion")
    if not isinstance(protocol_version, str):
        raise _invalid("protocolVersion", "is not this contract's wire version")
    try:
        negotiate_protocol_version(protocol_version)
    except UnsupportedProtocolVersionError as error:
        raise _invalid("protocolVersion", "is not this contract's wire version") from error
    return _RetryGeneration(
        command_id=_identifier(command, "commandId"),
        workspace_id=_identifier(command, "workspaceId"),
        conversation_id=_identifier(command, "conversationId"),
        actor_id=_identifier(command, "actorId"),
        job_id=_identifier(command, "jobId"),
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


def _conversation_has_branch(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> bool:
    """Whether this conversation holds any branch at all, open or terminal.

    Any branch, not just an open one: the first-send variant states no head, and a
    conversation with an archived branch has a head history a caller could and should
    have stated. 0029's own view-state key foreign-keys a branch, so a conversation
    with no branch also has no view state to disagree with.
    """
    row = connection.execute(
        "SELECT 1 FROM omnivia_chat_message_branches "
        "WHERE workspace_id = ? AND conversation_id = ? LIMIT 1",
        (workspace_id, conversation_id),
    ).fetchone()
    return row is not None


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


def _queued_content_hash(editable_parts: Sequence[Mapping[str, Any]]) -> str:
    """The same digest `_decode`'s `content_hash` is, recomputed from a stored row.

    `editable_parts` is already the exact `{type, visibility, payload}` wire shape
    `_editable_part_wire` writes and `_verified_json_array` re-verifies as canonical
    on read, so hashing it the same way `_decode` hashes a fresh request's parts is
    what makes the two directly comparable: a `SubmitMessage` drain matches this
    queue row's content if and only if the digests are equal, with no field-by-field
    deep comparison to keep in sync with `_part`'s own canonical form.
    """
    canonical_parts = tuple(to_canonical_json(dict(item)) for item in editable_parts)
    return _digest(f"[{','.join(canonical_parts)}]")


def _drained_generation_job_id(queue_idempotency_key: str) -> str:
    """A drain's generation job id, derived from the queue row's own correlation key.

    Not from the draining `SubmitMessage`'s own `commandId`: `EnqueueMessage` and the
    `SubmitMessage` that later drains it are different commands with different
    ids, and this is what keeps the job addressable by the row it serves regardless
    of which drain command opened it. `queue_idempotency_key` is already bounded to
    `_DERIVED_IDENTIFIER_MAX` characters at `EnqueueMessage` decode time, so the
    `.gen` suffix never exceeds 0029's 128-character identifier bound.
    """
    return f"{queue_idempotency_key}.gen"


def _generation_job_exists_for_idempotency_key(
    connection: sqlite3.Connection, *, workspace_id: str, idempotency_key: str
) -> bool:
    """Whether a generation job already carries this idempotency key.

    The same correlation `read_next_executable_queued_submission` joins on
    (`storage/chat.py`), used here the other way around: before opening a drain's
    job, prove no earlier drain of the same queue row already opened one. 0029's own
    `UNIQUE(workspace_id, idempotency_key)` on `omnivia_chat_generation_jobs` is the
    durable backstop if this read and the later insert ever race.
    """
    row = connection.execute(
        "SELECT 1 FROM omnivia_chat_generation_jobs "
        "WHERE workspace_id = ? AND idempotency_key = ?",
        (workspace_id, idempotency_key),
    ).fetchone()
    return row is not None


def _generation_event_id(generation_job_id: str, sequence: int) -> str:
    candidate = f"{generation_job_id}.e{sequence}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"generation-event-{digest[:40]}"


def _generation_cursor(generation_job_id: str, sequence: int) -> str:
    return f"{generation_job_id}:{sequence:06d}"


def retry_generation_attempt_id(command_id: str, generation_job_id: str) -> str:
    document = to_canonical_json(
        {
            "commandId": command_id,
            "generationJobId": generation_job_id,
            "kind": "RetryGeneration",
        }
    )
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return f"retry-attempt-{digest[:40]}"


def _ack(
    command_name: str,
    command_id: str,
    conversation_id: str,
    *,
    conversation_authority: ChatConversationExpectation | None = None,
    result_ref: str | None = None,
) -> Mapping[str, Any]:
    """One settled command's result envelope.

    `conversation_authority` is stated only by a command whose post-settlement counters
    this runtime knows exactly -- it is the conversation aggregate as written, not a
    guess -- and is otherwise absent, which is the shape every existing caller reads.
    `result_ref` is the same D07 `CommandResultEnvelope.resultRef` every other command
    here leaves absent; `EnqueueMessage` is the one caller that states it, opaquely
    naming the queued row this command just wrote.
    """
    return ChatCommandResult(
        command_name=command_name,
        command_result=CommandResultEnvelope(
            command_id=command_id, status="completed", result_ref=result_ref
        ).to_wire(),
        conversation_id=conversation_id,
        conversation_authority=conversation_authority,
    ).to_wire()


def _submit(request: _SubmitMessage) -> ChatCommand:
    """One decoded submission, as the command the mutation seam runs.

    Two variants, one transaction and one write order. The continuation advances an
    existing branch's head; the first send creates the branch that continuation would
    have advanced, at head version 1, and selects it for the actor. Everything after
    the head -- the message, its parts, the queued submission, the generation job and
    the outbox row -- is identical, because it is the same submission either way.
    """

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

        queued_row: QueuedSubmission | None = None
        if request.from_queued_submission_id is not None:
            queued_row = read_queued_submission(
                writer.connection,
                workspace_id=writer.workspace_id,
                queued_submission_id=request.from_queued_submission_id,
            )
            if (
                queued_row is None
                or queued_row.conversation_id != request.conversation_id
                or queued_row.actor_id != request.actor_id
                or queued_row.state != "queued"
            ):
                raise ChatAggregateConflict(_MESSAGE_QUEUE_ROW)
            # `EnqueueMessage` always names an open branch (Gate B admits no
            # branchless offline row), so a drain that omits the head group or
            # names a different branch cannot be this row's drain.
            if request.branch_id is None or request.branch_id != queued_row.branch_id:
                raise ChatAggregateConflict(_MESSAGE_QUEUE_ROW)
            if queued_row.references or _queued_content_hash(
                queued_row.editable_parts
            ) != request.content_hash:
                # A stale renderer's view of this row's content, identity or head is a
                # governed conflict requiring resnapshot, never a silent commit of
                # whichever content happened to arrive.
                raise ChatAggregateConflict(_MESSAGE_QUEUE_CONTENT)
            if _generation_job_exists_for_idempotency_key(
                writer.connection,
                workspace_id=writer.workspace_id,
                idempotency_key=queued_row.idempotency_key,
            ):
                raise ChatAggregateConflict(_MESSAGE_QUEUE_DRAINED)

        stated_branch_id = request.branch_id
        first_send = stated_branch_id is None
        if stated_branch_id is None:
            # A branchless conversation is what the omitted head group asserts, and it
            # is read here rather than trusted from the request: inside the fenced
            # transaction is the only place the answer is still true when it is used.
            if conversation.default_branch_id is not None or _conversation_has_branch(
                writer.connection,
                workspace_id=writer.workspace_id,
                conversation_id=request.conversation_id,
            ):
                raise ChatAggregateConflict(_MESSAGE_ALREADY_BRANCHED)
            branch_id = f"{request.command_id}.br"
            parent_message_id: str | None = None
            head_version = 1
        else:
            branch = read_branch(
                writer.connection,
                workspace_id=writer.workspace_id,
                branch_id=stated_branch_id,
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
            branch_id = branch.branch_id
            parent_message_id = request.expected_head_message_id
            head_version = branch.head_version + 1

        sequence = conversation.latest_conversation_sequence + 1
        if queued_row is None:
            queued_submission_id = f"{request.command_id}.sub"
            generation_job_id = f"{request.command_id}.gen"
            generation_job_idempotency_key = request.command_id
        else:
            # Reused, not re-derived: this drain writes no second queue row, and the
            # job's idempotency key is deliberately the queue row's own -- carried
            # forward from `EnqueueMessage`'s `commandId` rather than this drain's --
            # so `read_next_executable_queued_submission` finds exactly this job for
            # this row, and 0029's `UNIQUE(workspace_id, idempotency_key)` on
            # `omnivia_chat_generation_jobs` makes a second drain of the same row
            # impossible even if the pre-check above raced.
            queued_submission_id = queued_row.queued_submission_id
            generation_job_id = _drained_generation_job_id(queued_row.idempotency_key)
            generation_job_idempotency_key = queued_row.idempotency_key

        writer.update_conversation(
            conversation_id=request.conversation_id,
            expected_graph_revision=conversation.graph_revision,
            graph_revision=conversation.graph_revision,
            latest_conversation_sequence=sequence,
            state="active",
            title=conversation.title,
            title_source=conversation.title_source,
            # The first send is what gives a conversation its default branch; a
            # continuation leaves whatever the conversation already selected alone.
            default_branch_id=branch_id if first_send else conversation.default_branch_id,
            updated_at_us=now,
        )
        writer.append_message(
            message_id=request.message_id,
            conversation_id=request.conversation_id,
            role="user",
            author_type="human",
            author_id=request.actor_id,
            parent_message_id=parent_message_id,
            conversation_sequence=sequence,
            schema_version=1,
            content_hash=request.content_hash,
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=branch_id,
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
        if first_send:
            # After the message and before the head event, because 0029 keys the
            # branch's initial head to a real message row and keys the first head
            # event to the branch's initial head.
            writer.append_branch(
                branch_id=branch_id,
                conversation_id=request.conversation_id,
                origin_kind="original",
                initial_head_message_id=request.message_id,
                created_by_actor_id=request.actor_id,
                created_at_us=now,
                created_conversation_sequence=sequence,
                schema_version=1,
            )
        writer.append_branch_head_event(
            event_id=f"{request.command_id}.head",
            conversation_id=request.conversation_id,
            branch_id=branch_id,
            head_version=head_version,
            previous_head_message_id=parent_message_id,
            new_head_message_id=request.message_id,
            cause="branch_created" if first_send else "user_message_appended",
            # The seam's own claim identity, so the durable head event names the exact
            # mutation claim that settled it.
            command_id=settlement.claim_id,
            graph_revision=conversation.graph_revision,
            conversation_sequence=sequence,
            actor_id=request.actor_id,
            occurred_at_us=now,
            schema_version=1,
        )
        if first_send:
            # `append_branch` already inserted the projection at head version 1 with
            # this exact head, so there is nothing to advance -- only the actor's
            # selection of the branch the send just created.
            writer.insert_view_state(
                conversation_id=request.conversation_id,
                actor_id=request.actor_id,
                active_branch_id=branch_id,
                last_seen_graph_revision=conversation.graph_revision,
                schema_version=1,
                updated_at_us=now,
            )
        else:
            writer.update_branch_head(
                branch_id=branch_id,
                expected_head_version=head_version - 1,
                head_version=head_version,
                current_head_message_id=request.message_id,
                state="open",
            )
        if queued_row is None:
            queue_sequence = _next_queue_sequence(
                writer.connection,
                workspace_id=writer.workspace_id,
                conversation_id=request.conversation_id,
            )
            writer.append_queued_submission(
                queued_submission_id=queued_submission_id,
                conversation_id=request.conversation_id,
                actor_id=request.actor_id,
                queue_sequence=queue_sequence,
                branch_id=branch_id,
                editable_parts=tuple(_editable_part_wire(part) for part in request.parts),
                references=(),
                idempotency_key=request.command_id,
                created_at_us=now,
                updated_at_us=now,
            )
            writer.insert_queue_order_projection(
                queued_submission_id=queued_submission_id,
                conversation_id=request.conversation_id,
                queue_position=queue_sequence,
                updated_by_actor_id=request.actor_id,
                updated_at_us=now,
            )
        # A drain reuses the existing queue and queue-order rows and leaves them
        # `queued`: the generation claimant still walks `queued -> claimed ->
        # submitted` (`service/chat_generation.py`), now against the job this
        # command opens below.
        writer.append_generation_job(
            generation_job_id=generation_job_id,
            conversation_id=request.conversation_id,
            branch_id=branch_id,
            trigger_message_id=request.message_id,
            graph_revision_observed=conversation.graph_revision,
            idempotency_key=generation_job_idempotency_key,
            schema_version=1,
            created_at_us=now,
            updated_at_us=now,
        )
        payload: dict[str, Any] = {
            "conversationId": request.conversation_id,
            "branchId": branch_id,
            "messageId": request.message_id,
            "conversationSequence": sequence,
            "graphRevision": conversation.graph_revision,
            "headVersion": head_version,
            "role": "user",
            "queuedSubmissionId": queued_submission_id,
            "generationJobId": generation_job_id,
        }
        if parent_message_id is not None:
            # A root message has no parent, and `parentMessageId: null` would state
            # that it has one that is nothing.
            payload["parentMessageId"] = parent_message_id
        writer.append_outbox_entry(
            outbox_cursor=_next_outbox_cursor(writer.connection, writer.workspace_id),
            domain_event_id=f"{request.command_id}.evt",
            event_kind=_OUTBOX_EVENT_KIND,
            payload=payload,
            created_at_us=now,
            conversation_id=request.conversation_id,
            generation_job_id=generation_job_id,
        )
        return _ack(SUBMIT_MESSAGE_COMMAND, request.command_id, request.conversation_id)

    return command


def _create_conversation(request: _CreateConversation) -> ChatCommand:
    """One decoded `CreateConversationRequest`, as the command the seam runs.

    One conversation row, and deliberately nothing else -- no branch, no message, no
    view state, no queued submission, no generation job, and no transactional outbox
    row either. REF-042 §9.1 gives a new Conversation no branch until its first
    committed user Message, so a synthetic root message or an empty branch invented
    here would be a durable fabrication -- and the branchless first `SubmitMessage`
    above is what closes the gap honestly. The outbox carries *committed graph* facts
    that a delivery worker republishes to live subscribers, and an empty conversation
    has produced none: the first send is the first thing there is to announce.

    The conversation identity is derived from the caller's own `commandId`, the same
    way every other identity this module writes is, so an honest replay answers from
    the seam's stored outcome and a `commandId` reused under a *different* idempotency
    key finds its conversation already there and refuses.
    """

    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        now = settlement.settled_at_us
        conversation_id = f"{request.command_id}.conv"
        if (
            read_conversation(
                writer.connection,
                workspace_id=writer.workspace_id,
                conversation_id=conversation_id,
            )
            is not None
        ):
            raise ChatAggregateConflict(_MESSAGE_CONVERSATION_EXISTS)
        writer.append_conversation(
            conversation_id=conversation_id,
            state="active",
            graph_revision=_INITIAL_GRAPH_REVISION,
            latest_conversation_sequence=_INITIAL_CONVERSATION_SEQUENCE,
            schema_version=1,
            created_by_actor_id=request.actor_id,
            created_at_us=now,
            updated_at_us=now,
            title=request.title,
            title_source=None if request.title is None else "user",
        )
        return _ack(
            CREATE_CONVERSATION_COMMAND,
            request.command_id,
            conversation_id,
            # The empty authoritative state, stated rather than fabricated: the caller's
            # next command -- the branchless first `SubmitMessage` -- expects exactly the
            # counters this command just wrote, and `chat.snapshot` still answers
            # `not_found` for a conversation that has appended nothing.
            conversation_authority=ChatConversationExpectation(
                conversation_id=conversation_id,
                graph_revision=_INITIAL_GRAPH_REVISION,
                latest_conversation_sequence=_INITIAL_CONVERSATION_SEQUENCE,
            ),
        )

    return command


def _enqueue_message(request: _EnqueueMessage) -> ChatCommand:
    """One decoded `EnqueueMessageRequest`, as the command the seam runs.

    Writes exactly one queued-submission row and its queue-order projection, and
    nothing else: no Message, MessagePart, GenerationJob, GenerationEvent, Attempt,
    provider invocation or Chat outbox generation event (W5 GB-05). A queued row
    from this command is not generation-executable -- `execute_next`
    (`service/chat_generation_executor.py`) only ever selects a row with a matching
    generation job already committed, and this command commits none -- until a
    later `SubmitMessage.fromQueuedSubmissionId` drains it.

    The queued submission id is derived from this command's own `commandId`, the
    same bounded deterministic convention `SubmitMessage` derives one from its own
    id with, so a replay under the same `commandId` answers from the seam's stored
    outcome and a `commandId` reused under a different idempotency key finds its
    row already there and refuses.
    """

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

        queued_submission_id = f"{request.command_id}.sub"
        queue_sequence = _next_queue_sequence(
            writer.connection,
            workspace_id=writer.workspace_id,
            conversation_id=request.conversation_id,
        )
        writer.append_queued_submission(
            queued_submission_id=queued_submission_id,
            conversation_id=request.conversation_id,
            actor_id=request.actor_id,
            queue_sequence=queue_sequence,
            branch_id=request.branch_id,
            editable_parts=tuple(_editable_part_wire(part) for part in request.parts),
            references=(),
            idempotency_key=request.command_id,
            created_at_us=now,
            updated_at_us=now,
        )
        writer.insert_queue_order_projection(
            queued_submission_id=queued_submission_id,
            conversation_id=request.conversation_id,
            queue_position=queue_sequence,
            updated_by_actor_id=request.actor_id,
            updated_at_us=now,
        )
        return _ack(
            ENQUEUE_MESSAGE_COMMAND,
            request.command_id,
            request.conversation_id,
            result_ref=f"{_QUEUED_SUBMISSION_RESULT_REF_PREFIX}{queued_submission_id}",
        )

    return command


def _update_queued_submission(request: _UpdateQueuedSubmission) -> ChatCommand:
    """One decoded `UpdateQueuedSubmissionRequest`, as the command the seam runs.

    CAS-updates only `editableParts` and the submission's `version`; identity,
    order, branch, idempotency key and every other field this row carries are
    untouched (`storage.chat.ChatWriter.update_queued_submission_content`). Only a
    row this same workspace/conversation/actor still holds `queued` -- not yet
    claimed for generation -- is eligible; a claimed, submitted, cancelled or
    missing row, or a stale `expectedVersion`, is the same governed conflict with no
    write, so a caller cannot distinguish "wrong version" from "already claimed"
    and race a claim it cannot see.
    """

    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        submission = read_queued_submission(
            writer.connection,
            workspace_id=writer.workspace_id,
            queued_submission_id=request.queued_submission_id,
        )
        if (
            submission is None
            or submission.conversation_id != request.conversation_id
            or submission.actor_id != request.actor_id
            or submission.state != "queued"
        ):
            raise ChatAggregateConflict(_MESSAGE_QUEUE_CONTENT)
        try:
            writer.update_queued_submission_content(
                queued_submission_id=request.queued_submission_id,
                expected_version=request.expected_version,
                editable_parts=tuple(_editable_part_wire(part) for part in request.parts),
                updated_at_us=settlement.settled_at_us,
            )
        except StaleVersion as error:
            raise ChatAggregateConflict(_MESSAGE_QUEUE_CONTENT) from error
        return _ack(
            UPDATE_QUEUED_SUBMISSION_COMMAND,
            request.command_id,
            request.conversation_id,
        )

    return command


def _reorder_queued_submission(request: _ReorderQueuedSubmission) -> ChatCommand:
    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        submission = read_queued_submission(
            writer.connection,
            workspace_id=writer.workspace_id,
            queued_submission_id=request.queued_submission_id,
        )
        projection = read_queue_order_projection(
            writer.connection,
            workspace_id=writer.workspace_id,
            queued_submission_id=request.queued_submission_id,
        )
        if (
            submission is None
            or projection is None
            or submission.conversation_id != request.conversation_id
            or projection.conversation_id != request.conversation_id
            or submission.actor_id != request.actor_id
            or submission.state != "queued"
        ):
            raise ChatAggregateConflict("the queued submission is not reorderable")
        try:
            _move_queue_projection(
                writer,
                queued_submission_id=request.queued_submission_id,
                expected_version=request.expected_version,
                target_position=request.target_position,
                actor_id=request.actor_id,
                now_us=settlement.settled_at_us,
            )
        except StaleVersion as error:
            raise ChatAggregateConflict("the queued submission order version moved") from error
        return _ack(
            REORDER_QUEUED_SUBMISSION_COMMAND,
            request.command_id,
            request.conversation_id,
        )

    return command


def _move_queue_projection(
    writer: ChatWriter,
    *,
    queued_submission_id: str,
    expected_version: int,
    target_position: int,
    actor_id: str,
    now_us: int,
) -> None:
    """Move one queue row to `target_position`, touching only the caller's own
    active queue.

    `_reorder_queued_submission` has already proved the moving row is
    `actor_id`'s own and `queued`; this function additionally proves that about
    every row it *shifts*. A conversation's `omnivia_chat_queue_order_projection`
    keeps a position for a cancelled, submitted or another actor's row exactly as
    long as that row keeps existing (0029 never deletes it), so reading every
    conversation row and shifting whichever ones are in the way -- the previous
    behaviour -- could move or reveal one of those. Only `actor_id`'s own active
    rows (`read_active_queue_for_actor`) are candidates to shift; a `target_position`
    occupied by anything else is a position outside the caller's mutable queue and
    fails closed rather than moving into it or exposing that it is occupied. The
    same is true of every position strictly *between* the row's own position and
    `target_position`: moving between two of the caller's own positions can still
    cross an outsider-held one in between, and shifting a caller's own row onto
    that position would collide with the outsider's unique position rather than
    fail this governed conflict, so every intermediate occupied position is proved
    the caller's own before any write.
    """
    projection = read_queue_order_projection(
        writer.connection,
        workspace_id=writer.workspace_id,
        queued_submission_id=queued_submission_id,
    )
    if projection is None:
        raise StaleVersion("queue order projection is missing")
    if projection.version != expected_version:
        raise StaleVersion("queue order projection version moved")
    if projection.queue_position == target_position:
        writer.update_queue_order_projection(
            queued_submission_id=queued_submission_id,
            expected_version=expected_version,
            queue_position=target_position,
            updated_by_actor_id=actor_id,
            updated_at_us=now_us,
        )
        return

    all_rows = read_queue_order_for_conversation(
        writer.connection,
        workspace_id=writer.workspace_id,
        conversation_id=projection.conversation_id,
    )
    occupied_by_anyone = {row.queue_position for row in all_rows}
    if target_position not in occupied_by_anyone:
        writer.update_queue_order_projection(
            queued_submission_id=queued_submission_id,
            expected_version=expected_version,
            queue_position=target_position,
            updated_by_actor_id=actor_id,
            updated_at_us=now_us,
        )
        return

    own_entries = read_active_queue_for_actor(
        writer.connection,
        workspace_id=writer.workspace_id,
        conversation_id=projection.conversation_id,
        actor_id=actor_id,
        # The contract's own queue-position ceiling (`ReorderQueuedSubmission
        # .targetPosition`'s maximum, mirrored below in `free_position`'s bound),
        # not the snapshot's 200-row presentation limit: this internal read must
        # see every one of the caller's own active rows, never a page of them, or
        # the interval check below could wrongly fail closed on a real own row it
        # never saw.
        limit=100_000,
    )
    rows = [entry.order for entry in own_entries]
    own_positions = {row.queue_position for row in rows}
    if target_position not in own_positions:
        # Occupied, but not by one of the caller's own active rows: another
        # actor's row or a terminal (cancelled/submitted) one still holding its
        # position. Fail closed rather than revealing or displacing it.
        raise ChatAggregateConflict("the requested queue position is not available")

    old_position = projection.queue_position
    movement_lo, movement_hi = sorted((old_position, target_position))
    for position in occupied_by_anyone:
        # `old_position` and `target_position` are already proved the caller's own
        # above; every OTHER occupied position the shift below would cross must be
        # too, or shifting a caller's own row through it would land that row on
        # another actor's or a terminal row's unique position -- surfacing as a raw
        # storage constraint violation rather than this governed refusal. Checked
        # before any write, so a violation here leaves the queue untouched.
        if movement_lo < position < movement_hi and position not in own_positions:
            raise ChatAggregateConflict(
                "the requested reorder would cross another actor's queue position"
            )

    free_position = max(occupied_by_anyone) + 1
    if free_position > 100_000:
        raise ChatAggregateConflict("the queued submission order has no temporary position")

    moving_expected = expected_version
    writer.update_queue_order_projection(
        queued_submission_id=queued_submission_id,
        expected_version=moving_expected,
        queue_position=free_position,
        updated_by_actor_id=actor_id,
        updated_at_us=now_us,
    )
    moving_expected += 1

    if target_position < old_position:
        shifted = sorted(
            (row for row in rows if target_position <= row.queue_position < old_position),
            key=lambda row: row.queue_position,
            reverse=True,
        )
        delta = 1
    else:
        shifted = sorted(
            (row for row in rows if old_position < row.queue_position <= target_position),
            key=lambda row: row.queue_position,
        )
        delta = -1
    for row in shifted:
        writer.update_queue_order_projection(
            queued_submission_id=row.queued_submission_id,
            expected_version=row.version,
            queue_position=row.queue_position + delta,
            updated_by_actor_id=actor_id,
            updated_at_us=now_us,
        )

    writer.update_queue_order_projection(
        queued_submission_id=queued_submission_id,
        expected_version=moving_expected,
        queue_position=target_position,
        updated_by_actor_id=actor_id,
        updated_at_us=now_us,
    )


def _cancel_queued_submission(request: _CancelQueuedSubmission) -> ChatCommand:
    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        submission = read_queued_submission(
            writer.connection,
            workspace_id=writer.workspace_id,
            queued_submission_id=request.queued_submission_id,
        )
        if (
            submission is None
            or submission.conversation_id != request.conversation_id
            or submission.actor_id != request.actor_id
            or submission.state != "queued"
        ):
            raise ChatAggregateConflict("the queued submission is not cancellable")
        writer.update_queued_submission(
            queued_submission_id=request.queued_submission_id,
            expected_version=submission.version,
            state="cancelled",
            updated_at_us=settlement.settled_at_us,
        )
        return _ack(
            CANCEL_QUEUED_SUBMISSION_COMMAND,
            request.command_id,
            request.conversation_id,
        )

    return command


def _stop_generation(request: _StopGeneration) -> ChatCommand:
    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        job = read_generation_job(
            writer.connection, workspace_id=writer.workspace_id, generation_job_id=request.job_id
        )
        if (
            job is None
            or job.conversation_id != request.conversation_id
            or job.state != "running"
            or job.current_attempt_id is None
        ):
            raise ChatAggregateConflict("the generation job is not running")
        events = read_generation_events(
            writer.connection, workspace_id=writer.workspace_id, generation_job_id=request.job_id
        )
        if any(event.event_type in {"chat.generation.succeeded", "chat.generation.failed", "chat.generation.cancelled"} for event in events):
            raise ChatAggregateConflict("the generation job is already terminal")
        sequence = job.last_event_sequence + 1
        now = settlement.settled_at_us
        writer.append_generation_event(
            event_id=_generation_event_id(request.job_id, sequence),
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=request.job_id,
            generation_attempt_id=job.current_attempt_id,
            event_type="chat.generation.cancelled",
            generation_event_sequence=sequence,
            trigger_message_id=job.trigger_message_id,
            result_message_id=None,
            provider_event_id=None,
            cursor=_generation_cursor(request.job_id, sequence),
            payload={"finishReason": "cancelled", "stoppedByActorId": request.actor_id},
            occurred_at_us=now,
            schema_version=1,
        )
        writer.append_generation_attempt_outcome(
            conversation_id=job.conversation_id,
            generation_job_id=request.job_id,
            generation_attempt_id=job.current_attempt_id,
            terminal_state="cancelled",
            retryable=False,
            sanitized_error_code="cancelled",
            occurred_at_us=now,
        )
        writer.update_generation_job(
            generation_job_id=request.job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state="cancelled",
            lease_epoch=job.lease_epoch,
            current_attempt_id=job.current_attempt_id,
            last_event_sequence=sequence,
            sanitized_error_code="cancelled",
            updated_at_us=now,
            started_at_us=job.started_at_us,
            finished_at_us=now,
        )
        projection = read_generation_job_status_projection(
            writer.connection, workspace_id=writer.workspace_id, generation_job_id=request.job_id
        )
        if projection is None:
            writer.insert_generation_job_status_projection(
                generation_job_id=request.job_id,
                conversation_id=job.conversation_id,
                state="cancelled",
                current_attempt_id=job.current_attempt_id,
                sanitized_error_code="cancelled",
                updated_at_us=now,
                finished_at_us=now,
            )
        else:
            writer.update_generation_job_status_projection(
                generation_job_id=request.job_id,
                expected_version=projection.version,
                state="cancelled",
                current_attempt_id=job.current_attempt_id,
                sanitized_error_code="cancelled",
                updated_at_us=now,
                finished_at_us=now,
            )
        return _ack(STOP_GENERATION_COMMAND, request.command_id, request.conversation_id)

    return command


def _retry_generation(request: _RetryGeneration) -> ChatCommand:
    def command(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        job = read_generation_job(
            writer.connection,
            workspace_id=writer.workspace_id,
            generation_job_id=request.job_id,
        )
        status = read_generation_job_status_projection(
            writer.connection,
            workspace_id=writer.workspace_id,
            generation_job_id=request.job_id,
        )
        if (
            job is None
            or status is None
            or job.conversation_id != request.conversation_id
            or status.conversation_id != request.conversation_id
            or status.state != "retryable"
            or status.current_attempt_id is None
        ):
            raise ChatAggregateConflict("the generation job is not retryable")

        previous = read_generation_attempt_outcome(
            writer.connection,
            workspace_id=writer.workspace_id,
            generation_attempt_id=status.current_attempt_id,
        )
        if (
            previous is None
            or previous.generation_job_id != request.job_id
            or previous.terminal_state != "failed"
            or not previous.retryable
        ):
            raise ChatAggregateConflict("the generation attempt is not retryable")

        attempts = read_generation_attempts(
            writer.connection,
            workspace_id=writer.workspace_id,
            generation_job_id=request.job_id,
        )
        next_attempt_number = max(
            (attempt.attempt_number for attempt in attempts), default=0
        ) + 1
        generation_attempt_id = retry_generation_attempt_id(
            request.command_id, request.job_id
        )
        if any(
            attempt.generation_attempt_id == generation_attempt_id
            for attempt in attempts
        ):
            raise ChatAggregateConflict("the retry command already opened an attempt")
        now = settlement.settled_at_us
        writer.append_generation_attempt(
            generation_attempt_id=generation_attempt_id,
            conversation_id=job.conversation_id,
            generation_job_id=request.job_id,
            attempt_number=next_attempt_number,
            retry_of_attempt_id=status.current_attempt_id,
            state="running",
            schema_version=1,
            started_at_us=now,
        )
        writer.update_generation_job_status_projection(
            generation_job_id=request.job_id,
            expected_version=status.version,
            state="running",
            current_attempt_id=generation_attempt_id,
            updated_at_us=now,
        )
        return _ack(
            RETRY_GENERATION_COMMAND,
            request.command_id,
            request.conversation_id,
        )

    return command


def resolve_chat_command(
    request: ChatCommandInput, context: OperationContext
) -> ChatCommand | None:
    """The production resolver for the Core-owned Chat W5 command subset.

    `None` is a refusal the handler renders as `dependency_unavailable`, and it is
    still the honest answer for commands this build has no implementation of. A
    document for a supported command raises `invalid_request` instead when malformed,
    before a grant is issued and before a transaction is opened, so a malformed request
    writes nothing.

    The workspace the command document names must be the one the request was authorized
    for. It is checked rather than trusted or ignored: the envelope is workspace-scoped
    through the request's selected workspace, and a payload naming a second one is a
    request this seam cannot settle honestly under either.

    `CreateConversation` additionally holds `actorId` to the authenticated principal,
    because that value becomes the conversation's durable `createdBy`: a command may
    not name someone else as the author of a record this request is about to write.
    The other commands here name an actor that is matched against a row this workspace
    already holds, so the check that matters there is the one against the row.
    """
    if request.command_name == CREATE_CONVERSATION_COMMAND:
        create = _decode_create_conversation(request.command)
        if create.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        if create.actor_id != context.principal:
            raise _invalid(
                "actorId", "is not the principal this request was authenticated as"
            )
        return _create_conversation(create)
    if request.command_name == SUBMIT_MESSAGE_COMMAND:
        submit = _decode(request.command)
        if submit.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        if any(field in request.command for field in _UNSUPPORTED_FIELDS) or any(
            request.command.get(field) for field in _UNSUPPORTED_REFERENCES
        ):
            return None
        return _submit(submit)
    if request.command_name == ENQUEUE_MESSAGE_COMMAND:
        enqueue = _decode_enqueue(request.command)
        if enqueue.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        if enqueue.actor_id != context.principal:
            raise _invalid(
                "actorId", "is not the principal this request was authenticated as"
            )
        if any(field in request.command for field in _ENQUEUE_UNSUPPORTED_FIELDS) or any(
            request.command.get(field) for field in _UNSUPPORTED_REFERENCES
        ):
            return None
        return _enqueue_message(enqueue)
    if request.command_name == UPDATE_QUEUED_SUBMISSION_COMMAND:
        update_queue = _decode_update_queue(request.command)
        if update_queue.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        return _update_queued_submission(update_queue)
    if request.command_name == REORDER_QUEUED_SUBMISSION_COMMAND:
        reorder = _decode_reorder(request.command)
        if reorder.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        return _reorder_queued_submission(reorder)
    if request.command_name == CANCEL_QUEUED_SUBMISSION_COMMAND:
        cancel_queue = _decode_cancel_queue(request.command)
        if cancel_queue.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        return _cancel_queued_submission(cancel_queue)
    if request.command_name == STOP_GENERATION_COMMAND:
        stop_generation = _decode_stop_generation(request.command)
        if stop_generation.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        return _stop_generation(stop_generation)
    if request.command_name == RETRY_GENERATION_COMMAND:
        retry_generation = _decode_retry_generation(request.command)
        if retry_generation.workspace_id != context.workspace_id:
            raise _invalid("workspaceId", "is not the workspace this request was authorized for")
        return _retry_generation(retry_generation)
    return None
