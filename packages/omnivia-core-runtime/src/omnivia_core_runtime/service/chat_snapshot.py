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

import copy
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ChatContractDecodeError
from omnivia_core.chat_contract.v1.codec import validate_direction
from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)
from omnivia_core.contracts.v1.generated import (
    ChatDraftInput,
    ChatDraftResult,
    ChatSnapshotInput,
    ChatSnapshotResult,
)
from omnivia_core_runtime.service.chat_interactions import ChatWaitConflict
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    MalformedToolProposal,
    validate_action_summary,
)
from omnivia_core_runtime.service.chat_tool_review import (
    authorised_responder_basis,
    replay_tool_call,
    validate_human_input_prompt,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.chat import (
    ActiveQueueEntry,
    Branch,
    Conversation,
    ConversationSnapshotInputs,
    Direction,
    Draft,
    GenerationJob,
    GenerationJobStatusProjection,
    Message,
    MessagePart,
    ViewState,
    read_actor_directions_for_branch,
    read_branch_generation_job_ids,
    read_conversation_snapshot_inputs,
    read_draft,
    read_generation_job,
    read_generation_job_status_projection,
)
from omnivia_core_runtime.storage.chat_attachments import draft_attachment_references
from omnivia_core_runtime.storage.connection import StorageError

__all__ = ["composer_draft", "resolve_chat_snapshot", "resolve_draft_readback"]

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
_MESSAGE_DIRECTION_OVERFLOW: Final = (
    "the actor's directions exceed the bounded snapshot maximum and cannot be "
    "answered completely"
)
#: Every projection defect -- a missing or disagreeing job row, an orphan Direction,
#: tampered stored content or a document the frozen contract rejects -- is this one
#: sentence: it names no identifier, content, digest, SQL or exception.
_MESSAGE_INTERNAL: Final = (
    "the conversation snapshot could not be projected from consistent durable state"
)

#: `queries.schema.json#/$defs/ConversationSnapshotResult.generationJobs` and
#: `.directions`' governed maximum. One row past it is read so a complete set can be
#: told apart from one the bound cannot represent.
_SNAPSHOT_LIMIT: Final = 200

#: The stored job-status vocabulary is 0030's; the public one has no `retryable`,
#: which is a job still failed from the caller's point of view.
_PUBLIC_JOB_STATE: Final = {
    "queued": "queued",
    "running": "running",
    "retryable": "failed",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


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
    inputs: ConversationSnapshotInputs,
    branch: Branch,
    view_state: ViewState,
    job_ids: list[str],
) -> Mapping[str, Any]:
    """The branch path as `BranchPathResult`, at the conversation's own revision.

    `divergenceMetadataByMessageId` is deliberately absent rather than empty: sibling
    position is a graph fact this read does not load, and an empty map would state
    that no message on this path diverges.

    `generationJobIds` is the selected branch's complete durable job set -- exactly
    the jobs `generationJobs` states, not merely the ids the path's result Messages
    happen to name: a job opened by the `SubmitMessage` that committed the head user
    Message is durable and answerable before any assistant Message references it,
    and a caller proving a candidate job against this list has to be able to see it
    there. It is deliberately not the aggregate reader's union with the path's own
    references: an ancestor Message carried over from a forked-from branch names a
    job the selected branch does not own.
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
        "generationJobIds": job_ids,
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
    `attachmentReferences` are the row's tagged attachment entries decoded back, pinned
    and in stored order (C7A); `contextReferences` stay empty because this build refuses
    to store any. An entry that is not a tagged attachment reference is content this
    build did not write and is an internal error, never projected as either kind.

    `generationJobId` is the one exception to that narrowing, and it is identity
    only: a drained row stays `state: queued` until the generation claimant advances
    it, so without it an actor cannot tell a row still waiting to be submitted from
    one already handed to generation, and a restart risks draining it twice. Absent
    until a job exists, and only ever the job `storage.chat` verified for this exact
    row (`read_active_queue_for_actor`) -- never a derived id nothing backs.
    """
    submission, order = entry.submission, entry.order
    try:
        attachments = draft_attachment_references(submission.references)
    except StorageError:
        raise _internal() from None
    document: dict[str, Any] = {
        "workspaceId": submission.workspace_id,
        "conversationId": submission.conversation_id,
        "actorId": submission.actor_id,
        "queuedSubmissionId": submission.queued_submission_id,
        "branchId": submission.branch_id,
        "editableParts": [dict(part) for part in submission.editable_parts],
        "attachmentReferences": [dict(reference) for reference in attachments],
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


def composer_draft(draft: Draft) -> Mapping[str, Any]:
    """One `ComposerDraft`: the actor's own unsent composition state (C5A, C7A).

    Deliberately narrower than nothing: the draft row IS the projection, held to
    `viewstate.schema.json#/$defs/ComposerDraft`'s closed field set. The stored
    `references_json` is one combined array, so every entry is tagged with its kind
    (`storage.chat_attachments`): the attachment references are decoded back exactly as
    they were validated and saved, and `contextReferences` stays empty because this
    build refuses to store any. An entry that is not a tagged attachment reference is
    stored content this build did not write, and is refused as an internal error
    rather than projected as either kind. A discarded draft reads back exactly as it
    was emptied: text `""`, references `[]`.
    """
    try:
        attachments = draft_attachment_references(draft.references)
    except StorageError:
        raise _internal() from None
    document: dict[str, Any] = {
        "workspaceId": draft.workspace_id,
        "conversationId": draft.conversation_id,
        "actorId": draft.actor_id,
        "draftId": draft.draft_id,
        "mode": draft.mode,
        "text": draft.text_content,
        "attachmentReferences": [dict(reference) for reference in attachments],
        "contextReferences": [],
        "schemaVersion": draft.schema_version,
        "updatedAt": _timestamp(draft.updated_at_us),
        "version": draft.version,
    }
    _optional(document, "sourceMessageId", draft.source_message_id)
    _optional(document, "targetReference", draft.target)
    _optional(document, "stashedFromDraftId", draft.stashed_from_draft_id)
    return document


def _internal() -> OperationError:
    return OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INTERNAL)


def _succeeded_consistently(job: GenerationJob, status: GenerationJobStatusProjection) -> bool:
    """Whether a `succeeded` projection is one the foundation row can stand behind.

    Two legitimate histories exist. A first-attempt success leaves the 0029 row
    `succeeded` naming the same result Message. A success after `RetryGeneration`
    leaves it frozen `failed` from the earlier attempt with no result -- 0029 never
    reopens a terminal job -- while the 0030 projection advanced through running to
    succeeded. Attempt identity must back the history: a first-attempt success names
    one attempt on both rows, a retry success names a later attempt than the frozen
    failed one. Any other combination is a defect.
    """
    if status.current_attempt_id is None or job.current_attempt_id is None:
        return False
    if job.state == "succeeded":
        return (
            status.result_message_id is not None
            and job.result_message_id == status.result_message_id
            and job.current_attempt_id == status.current_attempt_id
        )
    return (
        job.state == "failed"
        and job.result_message_id is None
        and status.result_message_id is not None
        and job.current_attempt_id != status.current_attempt_id
    )


def _generation_job(
    job: GenerationJob | None,
    status: GenerationJobStatusProjection | None,
    *,
    workspace_id: str,
    conversation_id: str,
    branch_id: str,
    job_id: str,
) -> Mapping[str, Any]:
    """One `GenerationJob`: the foundation row joined to its 0030 status projection.

    `version` is the status projection's real control token, never inferred. Both
    rows must exist and describe this one job on the selected branch; anything else is
    an authority defect, not something to paper over. The projection's internal
    `retryable` is a failed job to a caller. The projection is the live authority for
    state, result, attempt and version; the foundation row is only frozen history
    (see `_succeeded_consistently`).
    """
    if job is None or status is None:
        raise _internal()
    if (
        (job.workspace_id, job.conversation_id, job.branch_id, job.generation_job_id)
        != (workspace_id, conversation_id, branch_id, job_id)
        or (status.workspace_id, status.conversation_id, status.generation_job_id)
        != (workspace_id, conversation_id, job_id)
        or status.state not in _PUBLIC_JOB_STATE
        or (status.state == "succeeded" and not _succeeded_consistently(job, status))
    ):
        raise _internal()
    document: dict[str, Any] = {
        "workspaceId": job.workspace_id,
        "conversationId": job.conversation_id,
        "branchId": job.branch_id,
        "triggerMessageId": job.trigger_message_id,
        "jobId": job.generation_job_id,
        "state": _PUBLIC_JOB_STATE[status.state],
        "graphRevisionObserved": job.graph_revision_observed,
        "version": status.version,
        "schemaVersion": job.schema_version,
        "createdAt": _timestamp(job.created_at_us),
        "updatedAt": _timestamp(status.updated_at_us),
    }
    _optional(document, "currentAttemptId", status.current_attempt_id)
    if status.state == "succeeded":
        _optional(document, "resultMessageId", status.result_message_id)
    return document


def _direction(direction: Direction) -> Mapping[str, Any]:
    """One `Direction`, shaped by its state from the durable evidence it holds.

    A terminal row's single `terminal_at_us` is the one instant every state-specific
    member is stamped with (`appliedAt`, `supersededAt`, `terminalReason.occurredAt`):
    0069 stores no second one. The stored digest is `sha256:<hex>` and the public
    `contentHash` is the same bare hex; `validate_direction` then re-proves it against
    the canonical content, along with the frozen shape of the document.
    """
    parts = direction.content.get("editableParts")
    if not isinstance(parts, list) or not direction.content_digest.startswith(
        _STORED_DIGEST_PREFIX
    ):
        raise _internal()
    document: dict[str, Any] = {
        "workspaceId": direction.workspace_id,
        "conversationId": direction.conversation_id,
        "branchId": direction.branch_id,
        "actorId": direction.actor_id,
        "directionId": direction.direction_id,
        "jobId": direction.generation_job_id,
        "targetAttemptId": direction.target_attempt_id,
        "jobSequence": direction.direction_sequence,
        "state": direction.state,
        "version": direction.version,
        "editableParts": copy.deepcopy(parts),
        "contentHash": _digest(direction.content_digest),
        "requestedBoundary": direction.requested_boundary,
        "schemaVersion": direction.schema_version,
        "createdAt": _timestamp(direction.created_at_us),
        "updatedAt": _timestamp(direction.updated_at_us),
    }
    _optional(document, "supersedesDirectionId", direction.supersedes_direction_id)
    if direction.state == "applied":
        if None in (
            direction.terminal_at_us,
            direction.applied_attempt_id,
            direction.applied_provider_invocation_id,
            direction.applied_request_manifest_id,
            direction.applied_iteration,
        ):
            raise _internal()
        document["effectiveBoundary"] = direction.effective_boundary
        document["application"] = {
            "attemptId": direction.applied_attempt_id,
            "providerInvocationId": direction.applied_provider_invocation_id,
            "requestManifestId": direction.applied_request_manifest_id,
            "iteration": direction.applied_iteration,
            "appliedAt": _timestamp(direction.terminal_at_us),  # type: ignore[arg-type]
        }
    elif direction.state in ("superseded", "rejected", "cancelled"):
        stored = direction.terminal_reason
        if stored is None or direction.terminal_at_us is None or not set(stored) <= {
            "code",
            "safeMessage",
        }:
            raise _internal()
        occurred_at = _timestamp(direction.terminal_at_us)
        reason: dict[str, Any] = {"code": stored.get("code"), "occurredAt": occurred_at}
        _optional(reason, "safeMessage", stored.get("safeMessage"))
        document["terminalReason"] = reason
        if direction.state == "superseded":
            document["supersession"] = {
                "supersededByDirectionId": direction.superseded_by_direction_id,
                "supersededAt": occurred_at,
            }
    elif direction.state != "pending":
        raise _internal()
    validate_direction(document)
    return document


def _generation_jobs(
    connection: sqlite3.Connection, query: _SnapshotQuery, branch: Branch
) -> list[tuple[int, Mapping[str, Any]]]:
    """Every job the selected branch owns, or the whole snapshot refused.

    Read in the caller's transaction. Returned as `(createdAt-millisecond, document)`
    in the contract's order -- public `createdAt`, then `jobId` -- so the caller can
    state the same set, in the same order, as `path.generationJobIds`.
    """
    job_ids = read_branch_generation_job_ids(
        connection,
        workspace_id=query.workspace_id,
        conversation_id=query.conversation_id,
        branch_id=branch.branch_id,
        limit=_SNAPSHOT_LIMIT + 1,
    )
    if len(job_ids) > _SNAPSHOT_LIMIT:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_JOB_OVERFLOW)
    jobs: list[tuple[int, Mapping[str, Any]]] = []
    for job_id in job_ids:
        job = read_generation_job(
            connection, workspace_id=query.workspace_id, generation_job_id=job_id
        )
        status = read_generation_job_status_projection(
            connection, workspace_id=query.workspace_id, generation_job_id=job_id
        )
        document = _generation_job(
            job,
            status,
            workspace_id=query.workspace_id,
            conversation_id=query.conversation_id,
            branch_id=branch.branch_id,
            job_id=job_id,
        )
        jobs.append((job.created_at_us // 1000, document))  # type: ignore[union-attr]
    jobs.sort(key=lambda entry: (entry[0], entry[1]["jobId"]))
    return jobs


def _tool_call(
    call: Any, wait: Any | None, actor_id: str
) -> Mapping[str, Any]:
    """One tool call, with its wait projected ONLY to an admitted responder.

    The split is deliberate and is the disclosure decision this projection
    carries. Identity, lifecycle state and timing are CONVERSATION state: a
    turn blocked on a tool call is a fact about the conversation, and hiding it
    leaves a reader unable to explain why nothing is happening. Everything a
    reviewer needs sits under `review`, and `review` is absent unless this
    actor may answer the wait.

    `authorised_responder_basis` is the SAME predicate the decision command
    enforces, imported rather than restated. A projection with its own copy of
    "who may answer this wait" is how a snapshot comes to show a review the
    command would refuse, or hide one the actor could in fact answer.

    No arguments, in any form. A reviewer decides from `review.actionSummary` --
    the closed, bounded, registry-derived description of the action -- plus the
    digest and revision the decision command already requires them to echo.
    Publishing tool inputs would be this bundle's first outbound disclosure of
    them; adding that later is additive, removing it would not be.

    THE SUMMARY IS VALIDATED HERE TOO, not trusted. It was derived and checked
    when the wait opened, and waits are immutable once open, so this is about
    the rows that predate the shape: a `prompt_json` holding free prose would
    otherwise be published under `actionSummary` and read as the derived fact.
    An invalid document omits the member rather than failing the snapshot --
    a reviewer with nothing to read cannot approve, and one unanswerable wait
    is not a reason to refuse a whole conversation.
    """
    document: dict[str, Any] = {
        "toolCallId": call.tool_call_id,
        "turnId": call.turn_id,
        "stepId": call.step_id,
        "toolName": call.tool_name,
        "toolVersion": call.tool_version,
        "registryRef": call.registry_ref,
        "state": call.state,
        "policyState": call.policy_state,
        "schemaVersion": call.schema_version,
        "createdAt": _timestamp(call.created_at_us),
    }
    if call.generation_job_id:
        document["generationJobId"] = call.generation_job_id
    if call.failure_code is not None:
        document["failureCode"] = call.failure_code
    if call.finished_at_us is not None:
        document["finishedAt"] = _timestamp(call.finished_at_us)
    if wait is None or wait.expires_at_us is None:
        return document
    if authorised_responder_basis(wait.authorised_responder_policy, actor_id) is None:
        # Not this actor's wait to answer. Absent is also what a call with no
        # wait at all looks like, and the two are deliberately indistinguishable
        # -- a reader who could tell them apart would learn who may review.
        return document
    review: dict[str, Any] = {
        "waitId": wait.wait_id,
        "kind": wait.kind,
        "state": wait.state,
        "requestVersion": wait.version,
        "expiresAt": _timestamp(wait.expires_at_us),
    }
    member = "prompt" if wait.kind == "human_input" else "actionSummary"
    check = validate_human_input_prompt if member == "prompt" else validate_action_summary
    try:
        review[member] = check(wait.prompt)
    except (ChatWaitConflict, MalformedToolProposal):
        pass
    if wait.target_action_digest is not None:
        review["targetActionDigest"] = wait.target_action_digest
    if wait.target_resource_ref is not None:
        review["targetResourceRef"] = wait.target_resource_ref
    if wait.target_resource_revision is not None:
        review["targetResourceRevision"] = wait.target_resource_revision
    document["review"] = review
    return document


def _directions(
    connection: sqlite3.Connection,
    query: _SnapshotQuery,
    branch: Branch,
    job_ids: frozenset[str],
) -> list[Mapping[str, Any]]:
    """The authenticated actor's own Directions on the selected branch, in every state."""
    rows = read_actor_directions_for_branch(
        connection,
        workspace_id=query.workspace_id,
        conversation_id=query.conversation_id,
        branch_id=branch.branch_id,
        actor_id=query.actor_id,
    )
    if len(rows) > _SNAPSHOT_LIMIT:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_DIRECTION_OVERFLOW)
    for row in rows:
        if (
            (row.workspace_id, row.conversation_id, row.branch_id, row.actor_id)
            != (query.workspace_id, query.conversation_id, branch.branch_id, query.actor_id)
            or row.generation_job_id not in job_ids
        ):
            raise _internal()
    ordered = sorted(
        rows, key=lambda row: (row.generation_job_id, row.direction_sequence, row.direction_id)
    )
    return [_direction(row) for row in ordered]


def _read_snapshot(connection: sqlite3.Connection, query: _SnapshotQuery) -> dict[str, Any]:
    """Every read one snapshot is composed from, inside the caller's transaction."""
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
    try:
        jobs = _generation_jobs(connection, query, inputs.branch)
        job_ids = [str(document["jobId"]) for _, document in jobs]
        directions = _directions(connection, query, inputs.branch, frozenset(job_ids))
    except (StorageError, ChatContractDecodeError, ValueError, KeyError, TypeError):
        # Tampered or non-canonical stored content, an unreadable row or a document
        # the frozen contract rejects: one constant refusal, never a partial snapshot
        # and never the stored text, digest or exception that caused it.
        raise _internal() from None
    snapshot: dict[str, Any] = {
        "conversation": _conversation(inputs.conversation),
        "path": _path(inputs, inputs.branch, inputs.view_state, job_ids),
        "branch": _branch(inputs.branch),
        "viewState": _view_state(inputs.view_state),
        "generationJobs": [document for _, document in jobs],
        "directions": directions,
    }
    if inputs.queued_submissions:
        snapshot["queuedSubmissions"] = [
            _queued_submission(entry) for entry in inputs.queued_submissions
        ]
    # The actor's drafts for this conversation, device and principal, read inside
    # the one authoritative read (`read_conversation_snapshot_inputs` now carries
    # them), so a snapshot remains exactly one observation of one revision window.
    if inputs.drafts:
        snapshot["composerDrafts"] = [
            composer_draft(draft) for draft in inputs.drafts
        ]
    # C8. The calls come from the one authoritative read; each call's WAIT is
    # resolved by `replay_tool_call`, which is the authoritative assembly --
    # a wait is not keyed by the call id, it is found by scanning the call's
    # lifecycle events for the latest one that names a wait. Reusing the
    # library's own resolution is the point: a second copy of that rule here
    # would be a projection that disagrees with the lifecycle about which wait
    # a call is on.
    if inputs.tool_calls:
        snapshot["toolCalls"] = [
            _tool_call(
                call,
                replay_tool_call(
                    connection,
                    workspace_id=query.workspace_id,
                    tool_call_id=call.tool_call_id,
                ).wait,
                query.actor_id,
            )
            for call in inputs.tool_calls
        ]
        snapshot["toolCallsTruncated"] = inputs.tool_calls_truncated
    return snapshot


def resolve_chat_snapshot(
    connection: sqlite3.Connection,
    request: ChatSnapshotInput,
    context: OperationContext,
) -> Mapping[str, Any]:
    """One `chat.snapshot` request, as the `ChatSnapshotResult` wire mapping.

    Raises `invalid_request` for a query document this contract does not admit or
    that disagrees with the envelope it arrived in, `not_found` where the workspace
    holds no complete snapshot to answer with, `size_limit_exceeded` where a set the
    snapshot must state completely exceeds its bound, and `internal_non_recoverable`
    where durable state cannot be projected consistently.

    Every read -- the aggregate, each branch job's rows and the actor's Directions --
    happens in one read transaction, so a concurrent write can never leave a snapshot
    whose jobs, Directions and path disagree about the moment they were read at. A
    transaction the caller already holds is theirs and is never ended here; only one
    opened here is committed, or rolled back on any failure.
    """
    query = _decode(request.snapshot_query, request, context)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        snapshot = _read_snapshot(connection, query)
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")
    return ChatSnapshotResult(
        conversation_id=query.conversation_id,
        snapshot=snapshot,
    ).to_wire()


_DRAFT_QUERY_FIELDS: Final = frozenset(
    {"requestId", "workspaceId", "conversationId", "actorId", "draftId"}
)
_DRAFT_QUERY_REQUIRED: Final = (
    "requestId",
    "workspaceId",
    "conversationId",
    "actorId",
    "draftId",
)


def _decode_draft_query(
    query: Mapping[str, Any], context: OperationContext
) -> tuple[str, str, str]:
    """Decode one `ComposerDraftQuery`, or refuse it as an invalid request.

    The same strictness in both directions `_decode` holds a snapshot query to:
    a field the frozen query does not define is refused, a required field that is
    absent, wrongly typed or not an identifier is refused the same way, and the
    facts the envelope already decided must *agree* with it. The conversation is
    stated only by the query document -- `ChatDraftInput` carries no native
    conversation -- so it is bound at the lookup below: a draft that lives in a
    different conversation answers `found: false` rather than being served
    cross-conversation.
    """
    unknown = sorted(set(query) - _DRAFT_QUERY_FIELDS)
    if unknown:
        raise _invalid(unknown[0], "is not a field of this query")
    for field in _DRAFT_QUERY_REQUIRED:
        if field not in query:
            raise _invalid(field, "is required")
        _identifier(query, field)
    if query["workspaceId"] != context.workspace_id:
        raise _invalid(
            "workspaceId", "is not the workspace this request was authorized for"
        )
    if query["actorId"] != context.principal:
        raise _invalid(
            "actorId", "is not the principal this request was authenticated as"
        )
    if query["requestId"] != context.request.metadata.request_id:
        raise _invalid("requestId", "is not the identifier of this request")
    return query["conversationId"], context.principal, query["draftId"]


def resolve_draft_readback(
    connection: sqlite3.Connection,
    request: ChatDraftInput,
    context: OperationContext,
) -> Mapping[str, Any]:
    """One `chat.draft` request, as the `ChatDraftResult` wire mapping.

    Raises `invalid_request` for a query document this contract does not admit or
    that disagrees with the envelope it arrived in. A draft this actor does not
    hold -- never saved, one that lives in another conversation, or one another
    actor holds -- answers `found: false` without a `draft`, so the absence is
    authoritative rather than an error and never an oracle for whether anyone
    else ever saved here. (A draft emptied by an acknowledged `DiscardDraft`
    still answers `found: true` with its emptied composition: the row is the
    truth, and its `version` keeps advancing.)
    """
    conversation_id, actor_id, draft_id = _decode_draft_query(
        request.draft_query, context
    )
    draft = read_draft(
        connection,
        workspace_id=context.workspace_id,
        draft_id=draft_id,
    )
    if draft is None or draft.conversation_id != conversation_id or draft.actor_id != actor_id:
        return ChatDraftResult(found=False).to_wire()
    return ChatDraftResult(found=True, draft=composer_draft(draft)).to_wire()
