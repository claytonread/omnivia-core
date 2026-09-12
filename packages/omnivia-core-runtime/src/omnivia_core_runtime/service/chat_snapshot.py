"""The production `chat.snapshot` resolver: one conversation, at one revision.

`chat.snapshot` is the read a caller takes when `chat.events` tells it a cursor can
no longer be continued. This module is its whole domain: it decodes the governed
`ConversationSnapshotQuery` the envelope carries opaquely, takes exactly one
authoritative read (`storage.chat.read_conversation_snapshot_inputs`), and projects
those rows as the Chat Contract v1 `ConversationSnapshotResult` document the
`ChatSnapshotResult` envelope carries back.

**A pure read.** Nothing here writes, claims, fences, queues, invokes a provider or
allocates an identifier: a snapshot is an observation, and the operation's catalogue
entry says so (`side_effect: none`, `safe_to_retry`). `handlers/chat.py` is what will
call this with a connection; this module holds no connection, no clock and no
service of its own.

**The query is checked against the envelope, never trusted from it.** The Chat
Contract's query document names the workspace, conversation, actor and request the
snapshot is for, and every one of those is a fact the Application Contract envelope
already decided: the authorized workspace, the natively stated `conversation_id`,
the authenticated principal and the request's own `request_id`. A document naming a
second one is refused rather than served under either, and the refusal names the
field and the rule, never the rejected value -- an untrusted document is exactly the
thing this service should not echo.

**A snapshot is complete or it is nothing.** `ConversationSnapshotResult` requires a
conversation, a branch path at a real head, and the actor's view state. A
conversation this workspace does not hold, one with no branch selected, one whose
branch head names no readable message, and one this actor has no view state for all
refuse identically -- one `not_found` with one constant diagnostic -- so the refusal
never becomes an oracle for which of those is the case.

**A branchless conversation is therefore still `not_found`, deliberately.** REF-042
§9.1 leaves a new Conversation with no branch until its first committed user Message,
and `BranchPathResult` requires a `branchId` and a `headMessageId` while
`ConversationViewState` requires an `activeBranchId` -- so the governed result has no
spelling for "exists, empty, no branch yet". Inventing one here would mean either a
fabricated branch identity or a fourth Core-local shape the Chat Contract does not
publish, and both are worse than an honest refusal. A host cold-starting a
conversation does not need this read: the `chat.command` result that created the
conversation already names it, and that *is* the authoritative empty state until the
branchless first `SubmitMessage` (`service/chat_submit.py`) commits the root message
and the `original` branch, after which this snapshot answers completely.

**The selected branch is published as the record, not just as the path.** `path` is
`BranchPathResult`, a disposable projection with no `headVersion`; the optional
`branch` member carries the durable `MessageBranch` the path was taken from, which is
where the optimistic token a later `SubmitMessage` must state actually lives.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)
from omnivia_core.contracts.v1.generated import ChatSnapshotInput, ChatSnapshotResult
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.chat import (
    ActiveQueueEntry,
    Branch,
    Conversation,
    ConversationSnapshotInputs,
    Message,
    MessagePart,
    ViewState,
    read_conversation_snapshot_inputs,
)

__all__ = ["resolve_chat_snapshot"]

#: `common.schema.json#/$defs/WorkspaceScopedId`, the grammar every identifier in this
#: query is spelled in. The same pattern `service/chat_submit.py` holds a command's
#: identifiers to, because it is the same contract rule.
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: `queries.schema.json#/$defs/ConversationSnapshotQuery`, closed field set and all.
_FIELDS: Final = frozenset(
    {"requestId", "workspaceId", "conversationId", "actorId", "deviceId"}
)
_REQUIRED: Final = ("requestId", "workspaceId", "conversationId", "actorId")

#: 0029 stores a digest as `sha256:<64 hex>`; `common.schema.json#/$defs/ContentHash`
#: spells the same digest as the bare hex. One value, two spellings.
_STORED_DIGEST_PREFIX: Final = "sha256:"

_MESSAGE_NOT_FOUND: Final = (
    "no complete conversation snapshot is available for this request"
)
_MESSAGE_QUEUE_OVERFLOW: Final = (
    "the actor's active queue exceeds the bounded snapshot maximum and cannot be "
    "answered completely"
)
_MESSAGE_JOB_OVERFLOW: Final = (
    "the branch's generation jobs exceed the bounded snapshot maximum and cannot be "
    "answered completely"
)


def _invalid(field: str, rule: str) -> OperationError:
    """A decode refusal naming the field and the rule, and never the value."""
    return OperationError(
        ERROR_CODE_INVALID_REQUEST,
        f"the chat snapshot query field {field!r} {rule}",
    )


def _timestamp(value: int) -> str:
    """One durable microsecond instant as the contract's RFC 3339 `Timestamp`."""
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _digest(value: str) -> str:
    return value.removeprefix(_STORED_DIGEST_PREFIX)


# --- the query document --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SnapshotQuery:
    """One decoded `ConversationSnapshotQuery`, already agreed with the envelope."""

    workspace_id: str
    conversation_id: str
    actor_id: str
    device_id: str


def _identifier(query: Mapping[str, Any], field: str) -> str:
    value = query.get(field)
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _invalid(field, "is not a workspace-scoped identifier")
    return value


def _decode(
    query: Mapping[str, Any], request: ChatSnapshotInput, context: OperationContext
) -> _SnapshotQuery:
    """Decode one `ConversationSnapshotQuery`, or refuse it as an invalid request.

    Strict in both directions: a field the frozen query does not define is refused,
    and a required field that is absent, wrongly typed or not an identifier is refused
    the same way. The four the envelope already decided must then *agree* with it.
    """
    unknown = sorted(set(query) - _FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this query")
    for field in _REQUIRED:
        if field not in query:
            raise _invalid(field, "is required")
        _identifier(query, field)
    device_id = "" if "deviceId" not in query else _identifier(query, "deviceId")

    if query["workspaceId"] != context.workspace_id:
        raise _invalid("workspaceId", "is not the workspace this request was authorized for")
    if query["conversationId"] != request.conversation_id:
        raise _invalid("conversationId", "is not the conversation this request addresses")
    if query["actorId"] != context.principal:
        raise _invalid("actorId", "is not the principal this request was authenticated as")
    if query["requestId"] != context.request.metadata.request_id:
        raise _invalid("requestId", "is not the identifier of this request")
    return _SnapshotQuery(
        workspace_id=context.workspace_id,
        conversation_id=request.conversation_id,
        actor_id=context.principal,
        device_id=device_id,
    )


# --- the projection --------------------------------------------------------------------


def _optional(document: dict[str, Any], field: str, value: Any) -> None:
    """State `field` only where the row actually carries it.

    Every optional member of these shapes means *absent*, not null: a conversation
    with no title has no title, and emitting one as `null` would both break the
    governed shape and invent a fact the row does not hold.
    """
    if value is not None:
        document[field] = value


def _part(part: MessagePart) -> Mapping[str, Any]:
    document: dict[str, Any] = {
        "workspaceId": part.workspace_id,
        "conversationId": part.conversation_id,
        "messageId": part.message_id,
        "partId": part.part_id,
        "index": part.part_index,
        "type": part.part_type,
        "schemaVersion": part.schema_version,
        "visibility": part.visibility,
        "payload": dict(part.payload),
        "contentHash": _digest(part.content_hash),
        "createdAt": _timestamp(part.created_at_us),
    }
    _optional(document, "provenance", part.provenance)
    return document


def _message(message: Message, parts: tuple[MessagePart, ...]) -> Mapping[str, Any]:
    document: dict[str, Any] = {
        "workspaceId": message.workspace_id,
        "conversationId": message.conversation_id,
        "messageId": message.message_id,
        "role": message.role,
        "authorType": message.author_type,
        "conversationSequence": message.conversation_sequence,
        "schemaVersion": message.schema_version,
        "contentHash": _digest(message.content_hash),
        "completionStatus": message.completion_status,
        "visibility": message.visibility,
        "parts": [_part(part) for part in parts],
        "createdAt": _timestamp(message.created_at_us),
        "committedAt": _timestamp(message.committed_at_us),
    }
    _optional(document, "parentMessageId", message.parent_message_id)
    _optional(document, "authorId", message.author_id)
    _optional(document, "createdOnBranchId", message.created_on_branch_id)
    if message.tombstoned_at_us is not None:
        document["tombstonedAt"] = _timestamp(message.tombstoned_at_us)
    return document


def _conversation(conversation: Conversation) -> Mapping[str, Any]:
    document: dict[str, Any] = {
        "workspaceId": conversation.workspace_id,
        "conversationId": conversation.conversation_id,
        "createdBy": conversation.created_by_actor_id,
        "createdAt": _timestamp(conversation.created_at_us),
        "state": conversation.state,
        "graphRevision": conversation.graph_revision,
        "latestConversationSequence": conversation.latest_conversation_sequence,
        "schemaVersion": conversation.schema_version,
    }
    _optional(document, "title", conversation.title)
    _optional(document, "defaultBranchId", conversation.default_branch_id)
    if conversation.archived_at_us is not None:
        document["archivedAt"] = _timestamp(conversation.archived_at_us)
    if conversation.tombstoned_at_us is not None:
        document["tombstonedAt"] = _timestamp(conversation.tombstoned_at_us)
    return document


def _path(
    inputs: ConversationSnapshotInputs, branch: Branch, view_state: ViewState
) -> Mapping[str, Any]:
    """The branch path as `BranchPathResult`, at the conversation's own revision.

    `divergenceMetadataByMessageId` is deliberately absent rather than empty: sibling
    position is a graph fact this read does not load, and an empty map would state
    that no message on this path diverges.

    `generationJobIds` is the branch's complete durable job projection, not merely
    the ids the path's result Messages happen to name: a job opened by the
    `SubmitMessage` that committed the head user Message is durable and answerable
    before any assistant Message references it, and a caller proving a candidate job
    against this list has to be able to see it there.
    """
    return {
        "workspaceId": branch.workspace_id,
        "conversationId": branch.conversation_id,
        "branchId": branch.branch_id,
        "headMessageId": branch.current_head_message_id,
        "graphRevision": inputs.conversation.graph_revision,
        "messages": [
            _message(message, inputs.parts_by_message_id.get(message.message_id, ()))
            for message in inputs.path
        ],
        "generationJobIds": list(inputs.generation_job_ids),
        "viewStateVersion": view_state.version,
    }


def _branch(branch: Branch) -> Mapping[str, Any]:
    """The selected branch as the durable `MessageBranch` record, not a second path.

    `BranchPathResult` is a disposable projection and carries no `headVersion`, so a
    caller that has just read a snapshot and wants to append has nowhere to read the
    optimistic token `SubmitMessage` requires. This is that record, exactly as the row
    holds it -- `branchId` is `path.branchId` and `currentHeadMessageId` is
    `path.headMessageId` by construction, because both come from the one branch row
    this read resolved.
    """
    document: dict[str, Any] = {
        "workspaceId": branch.workspace_id,
        "conversationId": branch.conversation_id,
        "branchId": branch.branch_id,
        "originKind": branch.origin_kind,
        "initialHeadMessageId": branch.initial_head_message_id,
        "currentHeadMessageId": branch.current_head_message_id,
        "createdBy": branch.created_by_actor_id,
        "createdAt": _timestamp(branch.created_at_us),
        "createdConversationSequence": branch.created_conversation_sequence,
        "headVersion": branch.head_version,
        "schemaVersion": branch.schema_version,
        "state": branch.state,
    }
    _optional(document, "createdFromBranchId", branch.created_from_branch_id)
    _optional(document, "forkParentMessageId", branch.fork_parent_message_id)
    _optional(document, "forkSourceMessageId", branch.fork_source_message_id)
    if branch.archived_at_us is not None:
        document["archivedAt"] = _timestamp(branch.archived_at_us)
    if branch.tombstoned_at_us is not None:
        document["tombstonedAt"] = _timestamp(branch.tombstoned_at_us)
    return document


def _view_state(view_state: ViewState) -> Mapping[str, Any]:
    document: dict[str, Any] = {
        "workspaceId": view_state.workspace_id,
        "conversationId": view_state.conversation_id,
        "actorId": view_state.actor_id,
        "activeBranchId": view_state.active_branch_id,
        "lastSeenGraphRevision": view_state.last_seen_graph_revision,
        "schemaVersion": view_state.schema_version,
        "updatedAt": _timestamp(view_state.updated_at_us),
        "version": view_state.version,
    }
    # The stored device scope is the empty string for an actor-level row, and the
    # contract has no such spelling: an actor default simply carries no `deviceId`.
    if view_state.device_id:
        document["deviceId"] = view_state.device_id
    _optional(document, "focusedMessageId", view_state.focused_message_id)
    return document


def _queued_submission(entry: ActiveQueueEntry) -> Mapping[str, Any]:
    """One `QueuedSubmissionProjection`: the actor's own row, joined to its order.

    Deliberately narrower than the durable row it is taken from: `claimedBy`,
    `sanitizedErrorCode`/`sanitizedErrorDetail` and `idempotencyKey` are claim and
    execution internals, never actor-facing queue state, and are never read here.
    `attachmentReferences`/`contextReferences` are always empty -- the Gate B slice
    this build serves writes no other reference facts to a queued row -- which is
    the honest answer this projection states rather than omits.

    `generationJobId` is the one exception to that narrowing, and it is identity
    only: a drained row stays `state: queued` until the generation claimant advances
    it, so without it an actor cannot tell a row still waiting to be submitted from
    one already handed to generation, and a restart risks draining it twice. Absent
    until a job exists, and only ever the job `storage.chat` verified for this exact
    row (`read_active_queue_for_actor`) -- never a derived id nothing backs.
    """
    submission, order = entry.submission, entry.order
    document: dict[str, Any] = {
        "workspaceId": submission.workspace_id,
        "conversationId": submission.conversation_id,
        "actorId": submission.actor_id,
        "queuedSubmissionId": submission.queued_submission_id,
        "branchId": submission.branch_id,
        "editableParts": [dict(part) for part in submission.editable_parts],
        "attachmentReferences": [],
        "contextReferences": [],
        "state": "queued",
        "version": submission.version,
        "position": order.queue_position,
        "orderVersion": order.version,
        "createdAt": _timestamp(submission.created_at_us),
        "updatedAt": _timestamp(submission.updated_at_us),
    }
    _optional(document, "generationJobId", entry.generation_job_id)
    return document


def resolve_chat_snapshot(
    connection: sqlite3.Connection,
    request: ChatSnapshotInput,
    context: OperationContext,
) -> Mapping[str, Any]:
    """One `chat.snapshot` request, as the `ChatSnapshotResult` wire mapping.

    Raises `invalid_request` for a query document this contract does not admit or
    that disagrees with the envelope it arrived in, and `not_found` where the
    workspace holds no complete snapshot to answer with.
    """
    query = _decode(request.snapshot_query, request, context)
    inputs = read_conversation_snapshot_inputs(
        connection,
        workspace_id=query.workspace_id,
        conversation_id=query.conversation_id,
        actor_id=query.actor_id,
        device_id=query.device_id,
    )
    if (
        inputs is None
        or inputs.branch is None
        or inputs.view_state is None
        or not inputs.path
        or inputs.path[-1].message_id != inputs.branch.current_head_message_id
    ):
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    if inputs.generation_job_ids_truncated:
        # Same rule as the queue below: a hosted controller proves a candidate job
        # against this list, so a shortened one would let it read "absent" for a job
        # that exists. Refuse the whole snapshot instead.
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_JOB_OVERFLOW)
    if inputs.queued_submissions_truncated:
        # A partial queue would misrepresent completeness to a reconnecting caller
        # relying on it for durable order; fail closed rather than silently
        # answering fewer rows than the actor actually has queued.
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_QUEUE_OVERFLOW)
    snapshot: dict[str, Any] = {
        "conversation": _conversation(inputs.conversation),
        "path": _path(inputs, inputs.branch, inputs.view_state),
        "branch": _branch(inputs.branch),
        "viewState": _view_state(inputs.view_state),
    }
    if inputs.queued_submissions:
        snapshot["queuedSubmissions"] = [
            _queued_submission(entry) for entry in inputs.queued_submissions
        ]
    return ChatSnapshotResult(
        conversation_id=query.conversation_id,
        snapshot=snapshot,
    ).to_wire()
