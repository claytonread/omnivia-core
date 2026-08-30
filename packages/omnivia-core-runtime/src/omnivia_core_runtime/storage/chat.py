"""Durable repository over the Chat foundation tables (migration 0029, W2-R).

Row-oriented, not service-shaped: every public function reads or writes exactly
one table's rows, using the caller-supplied identifiers and values migration 0029
already validates in SQL (contiguous sequences, immutability, terminal-state
refusal, transition legality). This module adds nothing 0029 does not already
enforce -- it is the seam that lets Python reach those guarded tables at all,
mirroring `storage/agent_runtime.py`'s writer/fence composition for the Runtime
tables.

Two ways to get a writer, and no third, following `agent_runtime.RuntimeWriter`:
:func:`chat_writer` opens its own `fenced_transaction` for a caller with none, and
:func:`transaction_local_writer` hands a :class:`ChatWriter` into a transaction the
caller already holds -- the composition seam a later W2-S command envelope needs to
settle more than one Chat write atomically. Every standalone function below is a
thin wrapper that opens one fence for a single write.

Mutability is exactly as narrow as 0029 declares it. Conversations, branches,
actor view state, drafts, queued submissions, generation jobs and outbox delivery
rows have compare-and-set update functions, each keyed by whatever expected prior
value is this table's optimistic-concurrency token (a `version` column that must
advance by exactly one, or the `graph_revision`/`head_version`/`state` +
`lease_epoch`/`delivery_state` + `delivery_attempts` tuple 0029's own triggers hold
the row to). A row that does not match the expected token is left untouched and
:class:`StaleVersion` is raised; 0029's own triggers separately refuse an illegal
transition regardless of whether the expected token matched. Every other table is
append-only: messages, message parts, derivations, branch head events, generation
attempts and generation events are written once and never updated.

`payload_json`, `references_json`, `editable_parts_json` and `target_json` are
stored as exact canonical JSON, because 0029's own triggers require it for the
columns they can check (`payload_json` on parts/generation events/outbox rows;
`references_json`/`target_json` on drafts; `editable_parts_json`/`references_json`
on queued submissions) and this module holds the rest to the same rule rather than
leaving them merely valid JSON. An object column canonicalises through the Chat
contract codec's `to_canonical_json`; an array column has no contract type to
canonicalise through, so it is serialised the same way by hand (sorted keys,
`allow_nan=False`, compact separators). Every read recomputes the canonical form
and requires the stored bytes to match it exactly, so a file edited outside this
database's own guards -- the case 0029's `CHECK` constraints do not run again on
read -- fails with :class:`StorageError` rather than returning something that
merely parses.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "Branch",
    "BranchHeadEvent",
    "ChatToolCall",
    "ChatToolResult",
    "ChatTurn",
    "ChatTurnStep",
    "ChatWriter",
    "Conversation",
    "Draft",
    "GenerationAttempt",
    "GenerationAttemptOutcome",
    "GenerationEvent",
    "GenerationJob",
    "GenerationJobStatusProjection",
    "GenerationTextChunk",
    "Message",
    "MessageDerivation",
    "MessagePart",
    "OutboxEntry",
    "QueueOrderProjection",
    "QueuedSubmission",
    "RequestManifest",
    "RequestManifestConflict",
    "StaleVersion",
    "ViewState",
    "append_branch",
    "append_branch_head_event",
    "append_chat_tool_call",
    "append_chat_tool_result",
    "append_chat_turn",
    "append_chat_turn_step",
    "append_conversation",
    "append_generation_attempt",
    "append_generation_attempt_outcome",
    "append_generation_event",
    "append_generation_job",
    "append_generation_text_chunk",
    "append_message",
    "append_message_derivation",
    "append_message_part",
    "append_outbox_entry",
    "append_queued_submission",
    "append_request_manifest_once",
    "chat_writer",
    "insert_draft",
    "insert_generation_job_status_projection",
    "insert_queue_order_projection",
    "insert_view_state",
    "read_active_draft",
    "read_actor_view_state",
    "read_branch",
    "read_branch_head_events",
    "read_chat_tool_call",
    "read_chat_tool_calls",
    "read_chat_tool_result",
    "read_chat_turn",
    "read_chat_turn_by_attempt",
    "read_chat_turn_steps",
    "read_conversation",
    "read_generation_attempt",
    "read_generation_attempt_outcome",
    "read_generation_attempt_outcomes",
    "read_generation_attempts",
    "read_generation_events",
    "read_generation_job",
    "read_generation_job_status_projection",
    "read_generation_text_chunks",
    "read_message_parts",
    "read_messages_by_conversation_sequence",
    "read_next_queued_submission",
    "read_outbox_event",
    "read_outbox_events_since",
    "read_queue_order_for_conversation",
    "read_queue_order_projection",
    "read_queued_submission",
    "read_request_manifest",
    "transaction_local_writer",
    "update_branch_head",
    "update_chat_tool_call",
    "update_chat_turn",
    "update_chat_turn_step",
    "update_conversation",
    "update_draft",
    "update_generation_job",
    "update_generation_job_status_projection",
    "update_outbox_delivery",
    "update_queue_order_projection",
    "update_queued_submission",
    "update_view_state",
]


class StaleVersion(StorageError):
    """A compare-and-set update did not match the row's expected prior state.

    The row is left exactly as it was: the `WHERE` clause carrying the expected
    token matched zero rows, so nothing committed. This is distinct from 0029's own
    trigger refusals (an illegal state transition, a terminal row reopened), which
    surface as `sqlite3.IntegrityError` regardless of whether the expected token
    matched.
    """


class RequestManifestConflict(StorageError):
    """An attempt already has a different request manifest.

    The existing row is left untouched. This is the request boundary equivalent
    of an idempotency-key conflict: replays of the same attempt may re-use the
    same manifest bytes, but a different manifest for that attempt must fail
    closed before any provider invocation is attempted.
    """


def _reject_json_constant(token: str) -> Any:
    raise StorageError(f"a stored chat JSON value contains the non-finite constant {token!r}")


def _canonical_json_object(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise StorageError("a chat JSON object column requires a mapping")
    try:
        return to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise StorageError(
            f"a chat JSON object column is not representable as canonical JSON: {error}"
        ) from error


def _canonical_json_object_digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + sha256(_canonical_json_object(value).encode("utf-8")).hexdigest()


def _canonical_json_array(value: Sequence[Any]) -> str:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        raise StorageError("a chat JSON array column requires a sequence")
    try:
        return json.dumps(
            list(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise StorageError(
            f"a chat JSON array column is not representable as canonical JSON: {error}"
        ) from error


def _verified_json_object(text: object, label: str) -> Mapping[str, Any]:
    document = str(text)
    try:
        decoded = json.loads(document, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise StorageError(f"a stored {label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise StorageError(f"a stored {label} is not a JSON object")
    if to_canonical_json(decoded) != document:
        raise StorageError(f"a stored {label} is not canonical JSON")
    return MappingProxyType(decoded)


def _verified_json_array(text: object, label: str) -> tuple[Any, ...]:
    document = str(text)
    try:
        decoded = json.loads(document, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise StorageError(f"a stored {label} is not valid JSON") from error
    if not isinstance(decoded, list):
        raise StorageError(f"a stored {label} is not a JSON array")
    canonical = json.dumps(
        decoded, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if canonical != document:
        raise StorageError(f"a stored {label} is not canonical JSON")
    return tuple(decoded)


def _require_cas_match(cursor: sqlite3.Cursor, label: str, identifier: str) -> None:
    if cursor.rowcount == 0:
        raise StaleVersion(f"{label} {identifier!r} did not match its expected prior state")


# --- rows -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conversation:
    workspace_id: str
    conversation_id: str
    title: str | None
    title_source: str | None
    state: str
    default_branch_id: str | None
    graph_revision: int
    latest_conversation_sequence: int
    schema_version: int
    created_by_actor_id: str
    created_at_us: int
    updated_at_us: int
    archived_at_us: int | None
    tombstoned_at_us: int | None


@dataclass(frozen=True, slots=True)
class Message:
    workspace_id: str
    conversation_id: str
    message_id: str
    parent_message_id: str | None
    role: str
    author_type: str
    author_id: str | None
    conversation_sequence: int
    schema_version: int
    content_hash: str
    completion_status: str
    visibility: str
    created_on_branch_id: str | None
    generation_job_id: str | None
    created_at_us: int
    committed_at_us: int
    tombstoned_at_us: int | None


@dataclass(frozen=True, slots=True)
class MessagePart:
    workspace_id: str
    conversation_id: str
    message_id: str
    part_id: str
    part_index: int
    part_type: str
    schema_version: int
    visibility: str
    payload: Mapping[str, Any]
    provenance: str | None
    content_hash: str
    created_at_us: int


@dataclass(frozen=True, slots=True)
class MessageDerivation:
    workspace_id: str
    conversation_id: str
    source_message_id: str
    derived_message_id: str
    derivation_kind: str
    created_by_actor_id: str
    created_at_us: int
    metadata: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class Branch:
    workspace_id: str
    conversation_id: str
    branch_id: str
    origin_kind: str
    created_from_branch_id: str | None
    fork_parent_message_id: str | None
    fork_source_message_id: str | None
    initial_head_message_id: str
    current_head_message_id: str
    created_by_actor_id: str
    created_at_us: int
    created_conversation_sequence: int
    head_version: int
    schema_version: int
    state: str
    archived_at_us: int | None
    tombstoned_at_us: int | None


@dataclass(frozen=True, slots=True)
class BranchHeadEvent:
    workspace_id: str
    conversation_id: str
    branch_id: str
    event_id: str
    head_version: int
    previous_head_message_id: str | None
    new_head_message_id: str
    cause: str
    command_id: str
    graph_revision: int
    conversation_sequence: int
    actor_id: str
    occurred_at_us: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class ViewState:
    workspace_id: str
    conversation_id: str
    actor_id: str
    device_id: str
    active_branch_id: str
    focused_message_id: str | None
    last_seen_graph_revision: int
    schema_version: int
    version: int
    updated_at_us: int


@dataclass(frozen=True, slots=True)
class Draft:
    workspace_id: str
    conversation_id: str
    actor_id: str
    device_id: str
    draft_id: str
    mode: str
    source_message_id: str | None
    text_content: str
    references: tuple[Any, ...]
    target: Mapping[str, Any] | None
    stashed_from_draft_id: str | None
    schema_version: int
    version: int
    updated_at_us: int
    expires_at_us: int | None


@dataclass(frozen=True, slots=True)
class QueuedSubmission:
    workspace_id: str
    conversation_id: str
    actor_id: str
    queued_submission_id: str
    queue_sequence: int
    branch_id: str
    editable_parts: tuple[Any, ...]
    references: tuple[Any, ...]
    idempotency_key: str
    state: str
    version: int
    claimed_by: str | None
    claim_epoch: int | None
    claim_expires_at_us: int | None
    submitted_message_id: str | None
    submitted_generation_job_id: str | None
    sanitized_error_code: str | None
    sanitized_error_detail: str | None
    created_at_us: int
    updated_at_us: int


@dataclass(frozen=True, slots=True)
class GenerationJob:
    workspace_id: str
    conversation_id: str
    branch_id: str
    trigger_message_id: str
    generation_job_id: str
    state: str
    graph_revision_observed: int
    idempotency_key: str
    current_attempt_id: str | None
    result_message_id: str | None
    lease_owner: str | None
    lease_epoch: int
    lease_expires_at_us: int | None
    heartbeat_at_us: int | None
    last_event_sequence: int
    sanitized_error_code: str | None
    sanitized_error_detail: str | None
    schema_version: int
    created_at_us: int
    updated_at_us: int
    started_at_us: int | None
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    workspace_id: str
    conversation_id: str
    generation_job_id: str
    generation_attempt_id: str
    attempt_number: int
    retry_of_attempt_id: str | None
    state: str
    provider_invocation_id: str | None
    schema_version: int
    started_at_us: int
    ended_at_us: int | None


@dataclass(frozen=True, slots=True)
class GenerationEvent:
    workspace_id: str
    conversation_id: str
    branch_id: str
    generation_job_id: str
    generation_attempt_id: str | None
    event_id: str
    event_type: str
    generation_event_sequence: int
    trigger_message_id: str
    result_message_id: str | None
    provider_event_id: str | None
    cursor: str
    payload: Mapping[str, Any]
    occurred_at_us: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class GenerationJobStatusProjection:
    workspace_id: str
    conversation_id: str
    generation_job_id: str
    state: str
    current_attempt_id: str | None
    result_message_id: str | None
    sanitized_error_code: str | None
    sanitized_error_detail: str | None
    version: int
    schema_version: int
    updated_at_us: int
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class GenerationAttemptOutcome:
    workspace_id: str
    conversation_id: str
    generation_job_id: str
    generation_attempt_id: str
    terminal_state: str
    result_message_id: str | None
    provider_event_id: str | None
    retryable: bool
    sanitized_error_code: str | None
    sanitized_error_detail: str | None
    schema_version: int
    occurred_at_us: int


@dataclass(frozen=True, slots=True)
class GenerationTextChunk:
    workspace_id: str
    conversation_id: str
    generation_job_id: str
    generation_attempt_id: str
    chunk_ordinal: int
    provider_event_id: str | None
    text_content: str
    content_hash: str
    schema_version: int
    occurred_at_us: int


@dataclass(frozen=True, slots=True)
class RequestManifest:
    workspace_id: str
    conversation_id: str
    branch_id: str
    generation_job_id: str
    generation_attempt_id: str
    trigger_message_id: str
    provider_invocation_id: str
    request_manifest_id: str
    idempotency_key: str
    schema_version: int
    manifest_digest: str
    manifest_body: Mapping[str, Any]
    created_at_us: int


@dataclass(frozen=True, slots=True)
class ChatTurn:
    workspace_id: str
    conversation_id: str
    branch_id: str
    generation_job_id: str
    generation_attempt_id: str
    turn_id: str
    state: str
    current_step_id: str | None
    version: int
    schema_version: int
    created_at_us: int
    updated_at_us: int
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class ChatTurnStep:
    workspace_id: str
    conversation_id: str
    turn_id: str
    step_id: str
    generation_job_id: str
    generation_attempt_id: str
    step_ordinal: int
    step_kind: str
    state: str
    version: int
    schema_version: int
    created_at_us: int
    updated_at_us: int
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class ChatToolCall:
    workspace_id: str
    conversation_id: str
    turn_id: str
    step_id: str
    generation_job_id: str
    generation_attempt_id: str
    tool_call_id: str
    tool_name: str
    tool_version: str
    registry_ref: str
    state: str
    policy_state: str
    proposed_arguments: Mapping[str, Any]
    proposed_arguments_digest: str
    post_policy_arguments: Mapping[str, Any] | None
    post_policy_arguments_digest: str | None
    executed_arguments_digest: str | None
    result_id: str | None
    failure_code: str | None
    version: int
    schema_version: int
    created_at_us: int
    updated_at_us: int
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class ChatToolResult:
    workspace_id: str
    conversation_id: str
    turn_id: str
    step_id: str
    generation_job_id: str
    generation_attempt_id: str
    tool_call_id: str
    result_id: str
    status: str
    result_payload: Mapping[str, Any]
    result_digest: str
    schema_version: int
    created_at_us: int


@dataclass(frozen=True, slots=True)
class QueueOrderProjection:
    workspace_id: str
    conversation_id: str
    queued_submission_id: str
    queue_position: int
    version: int
    updated_by_actor_id: str
    updated_at_us: int


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    workspace_id: str
    outbox_cursor: int
    domain_event_id: str
    event_kind: str
    conversation_id: str | None
    generation_job_id: str | None
    payload: Mapping[str, Any]
    delivery_state: str
    delivery_attempts: int
    next_delivery_after_us: int | None
    delivered_at_us: int | None
    retained_until_us: int | None
    created_at_us: int


# --- writer -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatWriter:
    """Every Chat write, issued into a transaction that is already open.

    Not constructible usefully on its own: :func:`chat_writer` and
    :func:`transaction_local_writer` are what hand one out, matching
    `agent_runtime.RuntimeWriter`. The workspace is bound at construction, because a
    composition is one workspace's work.
    """

    connection: sqlite3.Connection
    workspace_id: str

    # --- append: immutable facts, and the first row of a mutable projection -----

    def append_conversation(
        self,
        *,
        conversation_id: str,
        state: str,
        graph_revision: int,
        latest_conversation_sequence: int,
        schema_version: int,
        created_by_actor_id: str,
        created_at_us: int,
        updated_at_us: int,
        title: str | None = None,
        title_source: str | None = None,
        default_branch_id: str | None = None,
        archived_at_us: int | None = None,
        tombstoned_at_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_conversations "
            "(workspace_id, conversation_id, title, title_source, state, "
            "default_branch_id, graph_revision, latest_conversation_sequence, "
            "schema_version, created_by_actor_id, created_at_us, updated_at_us, "
            "archived_at_us, tombstoned_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                title,
                title_source,
                state,
                default_branch_id,
                graph_revision,
                latest_conversation_sequence,
                schema_version,
                created_by_actor_id,
                created_at_us,
                updated_at_us,
                archived_at_us,
                tombstoned_at_us,
            ),
        )

    def append_message(
        self,
        *,
        message_id: str,
        conversation_id: str,
        role: str,
        author_type: str,
        conversation_sequence: int,
        schema_version: int,
        content_hash: str,
        completion_status: str,
        visibility: str,
        created_at_us: int,
        committed_at_us: int,
        parent_message_id: str | None = None,
        author_id: str | None = None,
        created_on_branch_id: str | None = None,
        generation_job_id: str | None = None,
        tombstoned_at_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_messages "
            "(workspace_id, conversation_id, message_id, parent_message_id, role, "
            "author_type, author_id, conversation_sequence, schema_version, "
            "content_hash, completion_status, visibility, created_on_branch_id, "
            "generation_job_id, created_at_us, committed_at_us, tombstoned_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                message_id,
                parent_message_id,
                role,
                author_type,
                author_id,
                conversation_sequence,
                schema_version,
                content_hash,
                completion_status,
                visibility,
                created_on_branch_id,
                generation_job_id,
                created_at_us,
                committed_at_us,
                tombstoned_at_us,
            ),
        )

    def append_message_part(
        self,
        *,
        part_id: str,
        message_id: str,
        conversation_id: str,
        part_index: int,
        part_type: str,
        schema_version: int,
        visibility: str,
        payload: Mapping[str, Any],
        content_hash: str,
        created_at_us: int,
        provenance: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_message_parts "
            "(workspace_id, conversation_id, message_id, part_id, part_index, "
            "part_type, schema_version, visibility, payload_json, provenance, "
            "content_hash, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                message_id,
                part_id,
                part_index,
                part_type,
                schema_version,
                visibility,
                _canonical_json_object(payload),
                provenance,
                content_hash,
                created_at_us,
            ),
        )

    def append_message_derivation(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        derived_message_id: str,
        derivation_kind: str,
        created_by_actor_id: str,
        created_at_us: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_message_derivations "
            "(workspace_id, conversation_id, source_message_id, derived_message_id, "
            "derivation_kind, created_by_actor_id, created_at_us, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                source_message_id,
                derived_message_id,
                derivation_kind,
                created_by_actor_id,
                created_at_us,
                None if metadata is None else _canonical_json_object(metadata),
            ),
        )

    def append_branch(
        self,
        *,
        branch_id: str,
        conversation_id: str,
        origin_kind: str,
        initial_head_message_id: str,
        created_by_actor_id: str,
        created_at_us: int,
        created_conversation_sequence: int,
        schema_version: int,
        state: str = "open",
        created_from_branch_id: str | None = None,
        fork_parent_message_id: str | None = None,
        fork_source_message_id: str | None = None,
    ) -> None:
        """Insert a branch. 0029 requires `head_version = 1` and `current_head =
        initial_head` at creation, so both are fixed here rather than accepted as
        parameters a caller could get wrong.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_message_branches "
            "(workspace_id, conversation_id, branch_id, origin_kind, "
            "created_from_branch_id, fork_parent_message_id, fork_source_message_id, "
            "initial_head_message_id, current_head_message_id, created_by_actor_id, "
            "created_at_us, created_conversation_sequence, head_version, "
            "schema_version, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                origin_kind,
                created_from_branch_id,
                fork_parent_message_id,
                fork_source_message_id,
                initial_head_message_id,
                initial_head_message_id,
                created_by_actor_id,
                created_at_us,
                created_conversation_sequence,
                schema_version,
                state,
            ),
        )

    def append_branch_head_event(
        self,
        *,
        event_id: str,
        conversation_id: str,
        branch_id: str,
        head_version: int,
        new_head_message_id: str,
        cause: str,
        command_id: str,
        graph_revision: int,
        conversation_sequence: int,
        actor_id: str,
        occurred_at_us: int,
        schema_version: int,
        previous_head_message_id: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_branch_head_events "
            "(workspace_id, conversation_id, branch_id, event_id, head_version, "
            "previous_head_message_id, new_head_message_id, cause, command_id, "
            "graph_revision, conversation_sequence, actor_id, occurred_at_us, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                event_id,
                head_version,
                previous_head_message_id,
                new_head_message_id,
                cause,
                command_id,
                graph_revision,
                conversation_sequence,
                actor_id,
                occurred_at_us,
                schema_version,
            ),
        )

    def insert_view_state(
        self,
        *,
        conversation_id: str,
        actor_id: str,
        active_branch_id: str,
        last_seen_graph_revision: int,
        schema_version: int,
        updated_at_us: int,
        device_id: str = "",
        focused_message_id: str | None = None,
        version: int = 1,
    ) -> None:
        """First write of one actor/device's view-state row; later writes are
        :meth:`update_view_state`.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_conversation_view_states "
            "(workspace_id, conversation_id, actor_id, device_id, active_branch_id, "
            "focused_message_id, last_seen_graph_revision, schema_version, version, "
            "updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                actor_id,
                device_id,
                active_branch_id,
                focused_message_id,
                last_seen_graph_revision,
                schema_version,
                version,
                updated_at_us,
            ),
        )

    def insert_draft(
        self,
        *,
        draft_id: str,
        conversation_id: str,
        actor_id: str,
        mode: str,
        text_content: str,
        references: Sequence[Any],
        schema_version: int,
        updated_at_us: int,
        device_id: str = "",
        source_message_id: str | None = None,
        target: Mapping[str, Any] | None = None,
        stashed_from_draft_id: str | None = None,
        version: int = 1,
        expires_at_us: int | None = None,
    ) -> None:
        """First write of one actor/device/mode's draft row; later writes are
        :meth:`update_draft`.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_drafts "
            "(workspace_id, conversation_id, actor_id, device_id, draft_id, mode, "
            "source_message_id, text_content, references_json, target_json, "
            "stashed_from_draft_id, schema_version, version, updated_at_us, "
            "expires_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                actor_id,
                device_id,
                draft_id,
                mode,
                source_message_id,
                text_content,
                _canonical_json_array(references),
                None if target is None else _canonical_json_object(target),
                stashed_from_draft_id,
                schema_version,
                version,
                updated_at_us,
                expires_at_us,
            ),
        )

    def append_queued_submission(
        self,
        *,
        queued_submission_id: str,
        conversation_id: str,
        actor_id: str,
        queue_sequence: int,
        branch_id: str,
        editable_parts: Sequence[Any],
        references: Sequence[Any],
        idempotency_key: str,
        created_at_us: int,
        updated_at_us: int,
        version: int = 1,
    ) -> None:
        """0029 requires a queued submission to be inserted in `queued` state, so
        that is fixed here rather than accepted as a parameter.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_queued_submissions "
            "(workspace_id, conversation_id, actor_id, queued_submission_id, "
            "queue_sequence, branch_id, editable_parts_json, references_json, "
            "idempotency_key, state, version, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                actor_id,
                queued_submission_id,
                queue_sequence,
                branch_id,
                _canonical_json_array(editable_parts),
                _canonical_json_array(references),
                idempotency_key,
                version,
                created_at_us,
                updated_at_us,
            ),
        )

    def append_generation_job(
        self,
        *,
        generation_job_id: str,
        conversation_id: str,
        branch_id: str,
        trigger_message_id: str,
        graph_revision_observed: int,
        idempotency_key: str,
        schema_version: int,
        created_at_us: int,
        updated_at_us: int,
        lease_epoch: int = 0,
        last_event_sequence: int = 0,
    ) -> None:
        """0029 requires a generation job to be inserted in `queued` state, so that
        is fixed here rather than accepted as a parameter.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_jobs "
            "(workspace_id, conversation_id, branch_id, trigger_message_id, "
            "generation_job_id, state, graph_revision_observed, idempotency_key, "
            "lease_epoch, last_event_sequence, schema_version, created_at_us, "
            "updated_at_us) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                trigger_message_id,
                generation_job_id,
                graph_revision_observed,
                idempotency_key,
                lease_epoch,
                last_event_sequence,
                schema_version,
                created_at_us,
                updated_at_us,
            ),
        )

    def append_generation_attempt(
        self,
        *,
        generation_attempt_id: str,
        conversation_id: str,
        generation_job_id: str,
        attempt_number: int,
        state: str,
        schema_version: int,
        started_at_us: int,
        retry_of_attempt_id: str | None = None,
        provider_invocation_id: str | None = None,
        ended_at_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_attempts "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "attempt_number, retry_of_attempt_id, state, provider_invocation_id, "
            "schema_version, started_at_us, ended_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                generation_job_id,
                generation_attempt_id,
                attempt_number,
                retry_of_attempt_id,
                state,
                provider_invocation_id,
                schema_version,
                started_at_us,
                ended_at_us,
            ),
        )

    def append_generation_event(
        self,
        *,
        event_id: str,
        conversation_id: str,
        branch_id: str,
        generation_job_id: str,
        event_type: str,
        generation_event_sequence: int,
        trigger_message_id: str,
        cursor: str,
        payload: Mapping[str, Any],
        occurred_at_us: int,
        schema_version: int,
        generation_attempt_id: str | None = None,
        result_message_id: str | None = None,
        provider_event_id: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_events "
            "(workspace_id, conversation_id, branch_id, generation_job_id, "
            "generation_attempt_id, event_id, event_type, generation_event_sequence, "
            "trigger_message_id, result_message_id, provider_event_id, cursor, "
            "payload_json, occurred_at_us, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                generation_job_id,
                generation_attempt_id,
                event_id,
                event_type,
                generation_event_sequence,
                trigger_message_id,
                result_message_id,
                provider_event_id,
                cursor,
                _canonical_json_object(payload),
                occurred_at_us,
                schema_version,
            ),
        )

    def insert_generation_job_status_projection(
        self,
        *,
        generation_job_id: str,
        conversation_id: str,
        state: str,
        updated_at_us: int,
        current_attempt_id: str | None = None,
        result_message_id: str | None = None,
        sanitized_error_code: str | None = None,
        sanitized_error_detail: str | None = None,
        version: int = 1,
        schema_version: int = 1,
        finished_at_us: int | None = None,
    ) -> None:
        """First write of 0030's retry-aware generation-job projection.

        Migration 0029 stays immutable about its job state set; 0030 carries the
        successor projection that can pause a failed attempt as `retryable` and
        later resume the same job with a new attempt.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_job_status_projection "
            "(workspace_id, conversation_id, generation_job_id, state, "
            "current_attempt_id, result_message_id, sanitized_error_code, "
            "sanitized_error_detail, version, schema_version, updated_at_us, "
            "finished_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                generation_job_id,
                state,
                current_attempt_id,
                result_message_id,
                sanitized_error_code,
                sanitized_error_detail,
                version,
                schema_version,
                updated_at_us,
                finished_at_us,
            ),
        )

    def append_generation_attempt_outcome(
        self,
        *,
        conversation_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        terminal_state: str,
        retryable: bool,
        occurred_at_us: int,
        result_message_id: str | None = None,
        provider_event_id: str | None = None,
        sanitized_error_code: str | None = None,
        sanitized_error_detail: str | None = None,
        schema_version: int = 1,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_attempt_outcomes "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "terminal_state, result_message_id, provider_event_id, retryable, "
            "sanitized_error_code, sanitized_error_detail, schema_version, "
            "occurred_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                generation_job_id,
                generation_attempt_id,
                terminal_state,
                result_message_id,
                provider_event_id,
                1 if retryable else 0,
                sanitized_error_code,
                sanitized_error_detail,
                schema_version,
                occurred_at_us,
            ),
        )

    def append_generation_text_chunk(
        self,
        *,
        conversation_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        chunk_ordinal: int,
        text_content: str,
        content_hash: str,
        occurred_at_us: int,
        provider_event_id: str | None = None,
        schema_version: int = 1,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_text_chunks "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "chunk_ordinal, provider_event_id, text_content, content_hash, "
            "schema_version, occurred_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                generation_job_id,
                generation_attempt_id,
                chunk_ordinal,
                provider_event_id,
                text_content,
                content_hash,
                schema_version,
                occurred_at_us,
            ),
        )

    def append_request_manifest(
        self,
        *,
        conversation_id: str,
        branch_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        trigger_message_id: str,
        provider_invocation_id: str,
        request_manifest_id: str,
        idempotency_key: str,
        manifest_body: Mapping[str, Any],
        created_at_us: int,
        schema_version: int = 1,
        manifest_digest: str | None = None,
    ) -> None:
        digest = (
            _canonical_json_object_digest(manifest_body)
            if manifest_digest is None
            else manifest_digest
        )
        self.connection.execute(
            "INSERT INTO omnivia_chat_request_manifests "
            "(workspace_id, conversation_id, branch_id, generation_job_id, "
            "generation_attempt_id, trigger_message_id, provider_invocation_id, "
            "request_manifest_id, idempotency_key, schema_version, manifest_digest, "
            "request_manifest_body_json, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                generation_job_id,
                generation_attempt_id,
                trigger_message_id,
                provider_invocation_id,
                request_manifest_id,
                idempotency_key,
                schema_version,
                digest,
                _canonical_json_object(manifest_body),
                created_at_us,
            ),
        )

    def append_chat_turn(
        self,
        *,
        conversation_id: str,
        branch_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        turn_id: str,
        created_at_us: int,
        updated_at_us: int,
        state: str = "running",
        current_step_id: str | None = None,
        version: int = 1,
        schema_version: int = 1,
        finished_at_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_turns "
            "(workspace_id, conversation_id, branch_id, generation_job_id, "
            "generation_attempt_id, turn_id, state, current_step_id, version, "
            "schema_version, created_at_us, updated_at_us, finished_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                branch_id,
                generation_job_id,
                generation_attempt_id,
                turn_id,
                state,
                current_step_id,
                version,
                schema_version,
                created_at_us,
                updated_at_us,
                finished_at_us,
            ),
        )

    def append_chat_turn_step(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        step_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        step_ordinal: int,
        step_kind: str,
        created_at_us: int,
        updated_at_us: int,
        state: str = "proposed",
        version: int = 1,
        schema_version: int = 1,
        finished_at_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_turn_steps "
            "(workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
            "generation_attempt_id, step_ordinal, step_kind, state, version, "
            "schema_version, created_at_us, updated_at_us, finished_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                turn_id,
                step_id,
                generation_job_id,
                generation_attempt_id,
                step_ordinal,
                step_kind,
                state,
                version,
                schema_version,
                created_at_us,
                updated_at_us,
                finished_at_us,
            ),
        )

    def append_chat_tool_call(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        step_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        tool_version: str,
        registry_ref: str,
        proposed_arguments: Mapping[str, Any],
        created_at_us: int,
        updated_at_us: int,
        state: str = "proposed",
        policy_state: str = "pending",
        post_policy_arguments: Mapping[str, Any] | None = None,
        executed_arguments_digest: str | None = None,
        result_id: str | None = None,
        failure_code: str | None = None,
        version: int = 1,
        schema_version: int = 1,
        finished_at_us: int | None = None,
    ) -> None:
        proposed_digest = _canonical_json_object_digest(proposed_arguments)
        post_policy_digest = (
            None
            if post_policy_arguments is None
            else _canonical_json_object_digest(post_policy_arguments)
        )
        self.connection.execute(
            "INSERT INTO omnivia_chat_tool_calls "
            "(workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
            "generation_attempt_id, tool_call_id, tool_name, tool_version, "
            "registry_ref, state, policy_state, proposed_arguments_json, "
            "proposed_arguments_digest, post_policy_arguments_json, "
            "post_policy_arguments_digest, executed_arguments_digest, result_id, "
            "failure_code, version, schema_version, created_at_us, updated_at_us, "
            "finished_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                turn_id,
                step_id,
                generation_job_id,
                generation_attempt_id,
                tool_call_id,
                tool_name,
                tool_version,
                registry_ref,
                state,
                policy_state,
                _canonical_json_object(proposed_arguments),
                proposed_digest,
                None if post_policy_arguments is None else _canonical_json_object(post_policy_arguments),
                post_policy_digest,
                executed_arguments_digest,
                result_id,
                failure_code,
                version,
                schema_version,
                created_at_us,
                updated_at_us,
                finished_at_us,
            ),
        )

    def append_chat_tool_result(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        step_id: str,
        generation_job_id: str,
        generation_attempt_id: str,
        tool_call_id: str,
        result_id: str,
        status: str,
        result_payload: Mapping[str, Any],
        created_at_us: int,
        schema_version: int = 1,
        result_digest: str | None = None,
    ) -> None:
        digest = (
            _canonical_json_object_digest(result_payload)
            if result_digest is None
            else result_digest
        )
        self.connection.execute(
            "INSERT INTO omnivia_chat_tool_results "
            "(workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
            "generation_attempt_id, tool_call_id, result_id, status, "
            "result_payload_json, result_digest, schema_version, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                turn_id,
                step_id,
                generation_job_id,
                generation_attempt_id,
                tool_call_id,
                result_id,
                status,
                _canonical_json_object(result_payload),
                digest,
                schema_version,
                created_at_us,
            ),
        )

    def insert_queue_order_projection(
        self,
        *,
        queued_submission_id: str,
        conversation_id: str,
        queue_position: int,
        updated_by_actor_id: str,
        updated_at_us: int,
        version: int = 1,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_queue_order_projection "
            "(workspace_id, conversation_id, queued_submission_id, queue_position, "
            "version, updated_by_actor_id, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                queued_submission_id,
                queue_position,
                version,
                updated_by_actor_id,
                updated_at_us,
            ),
        )

    def append_outbox_entry(
        self,
        *,
        outbox_cursor: int,
        domain_event_id: str,
        event_kind: str,
        payload: Mapping[str, Any],
        created_at_us: int,
        conversation_id: str | None = None,
        generation_job_id: str | None = None,
        delivery_state: str = "pending",
        delivery_attempts: int = 0,
        next_delivery_after_us: int | None = None,
        delivered_at_us: int | None = None,
        retained_until_us: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_chat_transactional_outbox "
            "(workspace_id, outbox_cursor, domain_event_id, event_kind, "
            "conversation_id, generation_job_id, payload_json, delivery_state, "
            "delivery_attempts, next_delivery_after_us, delivered_at_us, "
            "retained_until_us, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                outbox_cursor,
                domain_event_id,
                event_kind,
                conversation_id,
                generation_job_id,
                _canonical_json_object(payload),
                delivery_state,
                delivery_attempts,
                next_delivery_after_us,
                delivered_at_us,
                retained_until_us,
                created_at_us,
            ),
        )

    # --- compare-and-set: mutable projections ------------------------------------

    def update_conversation(
        self,
        *,
        conversation_id: str,
        expected_graph_revision: int,
        graph_revision: int,
        latest_conversation_sequence: int,
        state: str,
        updated_at_us: int,
        title: str | None = None,
        title_source: str | None = None,
        default_branch_id: str | None = None,
        archived_at_us: int | None = None,
        tombstoned_at_us: int | None = None,
    ) -> None:
        """Replace the conversation's mutable fields, guarded by its current
        `graph_revision`. 0029 separately refuses a `graph_revision` that goes
        backwards and a terminal `state` that reopens.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_conversations SET title = ?, title_source = ?, "
            "state = ?, default_branch_id = ?, graph_revision = ?, "
            "latest_conversation_sequence = ?, updated_at_us = ?, archived_at_us = ?, "
            "tombstoned_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ? AND graph_revision = ?",
            (
                title,
                title_source,
                state,
                default_branch_id,
                graph_revision,
                latest_conversation_sequence,
                updated_at_us,
                archived_at_us,
                tombstoned_at_us,
                self.workspace_id,
                conversation_id,
                expected_graph_revision,
            ),
        )
        _require_cas_match(cursor, "conversation", conversation_id)

    def update_branch_head(
        self,
        *,
        branch_id: str,
        expected_head_version: int,
        head_version: int,
        current_head_message_id: str,
        state: str,
        archived_at_us: int | None = None,
        tombstoned_at_us: int | None = None,
    ) -> None:
        """Advance a branch's head projection to a version 0029 requires be backed
        by a `omnivia_chat_branch_head_events` row of the same `head_version` and
        `new_head_message_id` -- appended separately, in the same transaction, via
        :meth:`append_branch_head_event`.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_message_branches SET current_head_message_id = ?, "
            "head_version = ?, state = ?, archived_at_us = ?, tombstoned_at_us = ? "
            "WHERE workspace_id = ? AND branch_id = ? AND head_version = ?",
            (
                current_head_message_id,
                head_version,
                state,
                archived_at_us,
                tombstoned_at_us,
                self.workspace_id,
                branch_id,
                expected_head_version,
            ),
        )
        _require_cas_match(cursor, "branch", branch_id)

    def update_view_state(
        self,
        *,
        conversation_id: str,
        actor_id: str,
        expected_version: int,
        active_branch_id: str,
        last_seen_graph_revision: int,
        updated_at_us: int,
        device_id: str = "",
        focused_message_id: str | None = None,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_conversation_view_states SET active_branch_id = ?, "
            "focused_message_id = ?, last_seen_graph_revision = ?, version = ?, "
            "updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
            "AND device_id = ? AND version = ?",
            (
                active_branch_id,
                focused_message_id,
                last_seen_graph_revision,
                expected_version + 1,
                updated_at_us,
                self.workspace_id,
                conversation_id,
                actor_id,
                device_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "view state", f"{conversation_id}/{actor_id}/{device_id}")

    def update_draft(
        self,
        *,
        draft_id: str,
        expected_version: int,
        text_content: str,
        references: Sequence[Any],
        updated_at_us: int,
        target: Mapping[str, Any] | None = None,
        stashed_from_draft_id: str | None = None,
        expires_at_us: int | None = None,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_drafts SET text_content = ?, references_json = ?, "
            "target_json = ?, stashed_from_draft_id = ?, version = ?, "
            "updated_at_us = ?, expires_at_us = ? "
            "WHERE workspace_id = ? AND draft_id = ? AND version = ?",
            (
                text_content,
                _canonical_json_array(references),
                None if target is None else _canonical_json_object(target),
                stashed_from_draft_id,
                expected_version + 1,
                updated_at_us,
                expires_at_us,
                self.workspace_id,
                draft_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "draft", draft_id)

    def update_queued_submission(
        self,
        *,
        queued_submission_id: str,
        expected_version: int,
        state: str,
        updated_at_us: int,
        claimed_by: str | None = None,
        claim_epoch: int | None = None,
        claim_expires_at_us: int | None = None,
        submitted_message_id: str | None = None,
        submitted_generation_job_id: str | None = None,
        sanitized_error_code: str | None = None,
        sanitized_error_detail: str | None = None,
    ) -> None:
        """0029 separately refuses an illegal `state` transition regardless of
        whether `expected_version` matched.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_queued_submissions SET state = ?, version = ?, "
            "claimed_by = ?, claim_epoch = ?, claim_expires_at_us = ?, "
            "submitted_message_id = ?, submitted_generation_job_id = ?, "
            "sanitized_error_code = ?, sanitized_error_detail = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ? AND version = ?",
            (
                state,
                expected_version + 1,
                claimed_by,
                claim_epoch,
                claim_expires_at_us,
                submitted_message_id,
                submitted_generation_job_id,
                sanitized_error_code,
                sanitized_error_detail,
                updated_at_us,
                self.workspace_id,
                queued_submission_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "queued submission", queued_submission_id)

    def update_generation_job(
        self,
        *,
        generation_job_id: str,
        expected_state: str,
        expected_lease_epoch: int,
        state: str,
        lease_epoch: int,
        updated_at_us: int,
        current_attempt_id: str | None = None,
        result_message_id: str | None = None,
        lease_owner: str | None = None,
        lease_expires_at_us: int | None = None,
        heartbeat_at_us: int | None = None,
        last_event_sequence: int = 0,
        sanitized_error_code: str | None = None,
        sanitized_error_detail: str | None = None,
        started_at_us: int | None = None,
        finished_at_us: int | None = None,
    ) -> None:
        """A generation job has no `version` column; its own `state` and
        `lease_epoch` are the compare-and-set token. 0029 separately refuses an
        illegal `state` transition regardless of whether the token matched.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_generation_jobs SET state = ?, current_attempt_id = ?, "
            "result_message_id = ?, lease_owner = ?, lease_epoch = ?, "
            "lease_expires_at_us = ?, heartbeat_at_us = ?, last_event_sequence = ?, "
            "sanitized_error_code = ?, sanitized_error_detail = ?, updated_at_us = ?, "
            "started_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ? AND state = ? "
            "AND lease_epoch = ?",
            (
                state,
                current_attempt_id,
                result_message_id,
                lease_owner,
                lease_epoch,
                lease_expires_at_us,
                heartbeat_at_us,
                last_event_sequence,
                sanitized_error_code,
                sanitized_error_detail,
                updated_at_us,
                started_at_us,
                finished_at_us,
                self.workspace_id,
                generation_job_id,
                expected_state,
                expected_lease_epoch,
            ),
        )
        _require_cas_match(cursor, "generation job", generation_job_id)

    def update_generation_job_status_projection(
        self,
        *,
        generation_job_id: str,
        expected_version: int,
        state: str,
        updated_at_us: int,
        current_attempt_id: str | None = None,
        result_message_id: str | None = None,
        sanitized_error_code: str | None = None,
        sanitized_error_detail: str | None = None,
        finished_at_us: int | None = None,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_generation_job_status_projection SET state = ?, "
            "current_attempt_id = ?, result_message_id = ?, sanitized_error_code = ?, "
            "sanitized_error_detail = ?, version = ?, updated_at_us = ?, "
            "finished_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ? AND version = ?",
            (
                state,
                current_attempt_id,
                result_message_id,
                sanitized_error_code,
                sanitized_error_detail,
                expected_version + 1,
                updated_at_us,
                finished_at_us,
                self.workspace_id,
                generation_job_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "generation job status projection", generation_job_id)

    def update_queue_order_projection(
        self,
        *,
        queued_submission_id: str,
        expected_version: int,
        queue_position: int,
        updated_by_actor_id: str,
        updated_at_us: int,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_queue_order_projection SET queue_position = ?, "
            "version = ?, updated_by_actor_id = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ? AND version = ?",
            (
                queue_position,
                expected_version + 1,
                updated_by_actor_id,
                updated_at_us,
                self.workspace_id,
                queued_submission_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "queue order projection", queued_submission_id)

    def update_chat_turn(
        self,
        *,
        turn_id: str,
        expected_version: int,
        state: str,
        updated_at_us: int,
        current_step_id: str | None = None,
        finished_at_us: int | None = None,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_turns SET state = ?, current_step_id = ?, "
            "version = ?, updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND turn_id = ? AND version = ?",
            (
                state,
                current_step_id,
                expected_version + 1,
                updated_at_us,
                finished_at_us,
                self.workspace_id,
                turn_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "chat turn", turn_id)

    def update_chat_turn_step(
        self,
        *,
        step_id: str,
        expected_version: int,
        state: str,
        updated_at_us: int,
        finished_at_us: int | None = None,
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_turn_steps SET state = ?, version = ?, "
            "updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND step_id = ? AND version = ?",
            (
                state,
                expected_version + 1,
                updated_at_us,
                finished_at_us,
                self.workspace_id,
                step_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "chat turn step", step_id)

    def update_chat_tool_call(
        self,
        *,
        tool_call_id: str,
        expected_version: int,
        state: str,
        policy_state: str,
        updated_at_us: int,
        post_policy_arguments: Mapping[str, Any] | None = None,
        executed_arguments_digest: str | None = None,
        result_id: str | None = None,
        failure_code: str | None = None,
        finished_at_us: int | None = None,
    ) -> None:
        post_policy_digest = (
            None
            if post_policy_arguments is None
            else _canonical_json_object_digest(post_policy_arguments)
        )
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_tool_calls SET state = ?, policy_state = ?, "
            "post_policy_arguments_json = ?, post_policy_arguments_digest = ?, "
            "executed_arguments_digest = ?, result_id = ?, failure_code = ?, "
            "version = ?, updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND tool_call_id = ? AND version = ?",
            (
                state,
                policy_state,
                None if post_policy_arguments is None else _canonical_json_object(post_policy_arguments),
                post_policy_digest,
                executed_arguments_digest,
                result_id,
                failure_code,
                expected_version + 1,
                updated_at_us,
                finished_at_us,
                self.workspace_id,
                tool_call_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "chat tool call", tool_call_id)

    def update_outbox_delivery(
        self,
        *,
        outbox_cursor: int,
        expected_delivery_state: str,
        expected_delivery_attempts: int,
        delivery_state: str,
        delivery_attempts: int,
        next_delivery_after_us: int | None = None,
        delivered_at_us: int | None = None,
    ) -> None:
        """An outbox row has no `version` column; its own `delivery_state` and
        `delivery_attempts` are the compare-and-set token. 0029 separately refuses a
        delivered entry reopening regardless of whether the token matched.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_transactional_outbox SET delivery_state = ?, "
            "delivery_attempts = ?, next_delivery_after_us = ?, delivered_at_us = ? "
            "WHERE workspace_id = ? AND outbox_cursor = ? AND delivery_state = ? "
            "AND delivery_attempts = ?",
            (
                delivery_state,
                delivery_attempts,
                next_delivery_after_us,
                delivered_at_us,
                self.workspace_id,
                outbox_cursor,
                expected_delivery_state,
                expected_delivery_attempts,
            ),
        )
        _require_cas_match(cursor, "outbox entry", str(outbox_cursor))


def transaction_local_writer(connection: sqlite3.Connection, *, workspace_id: str) -> ChatWriter:
    """The Chat writes, for a caller that already holds a fenced transaction.

    Opens no transaction and validates no authority; everything it issues lands in
    whatever transaction the caller opened, and is covered by that transaction's
    entry and pre-commit validation. Calling it outside one is not a way past the
    guard: 0029's persisted triggers refuse an unguarded write regardless of which
    Python object issued it.
    """
    return ChatWriter(connection, workspace_id)


@contextmanager
def chat_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[ChatWriter]:
    """One fenced transaction, and the Chat writes that may be issued into it."""
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ):
        yield transaction_local_writer(connection, workspace_id=workspace_id)


# --- standalone writers: each opens its own fence --------------------------------


def append_conversation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_conversation(**fields)


def append_message(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_message(**fields)


def append_message_part(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_message_part(**fields)


def append_message_derivation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_message_derivation(**fields)


def append_branch(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_branch(**fields)


def append_branch_head_event(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_branch_head_event(**fields)


def insert_view_state(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.insert_view_state(**fields)


def insert_draft(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.insert_draft(**fields)


def append_queued_submission(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_queued_submission(**fields)


def append_generation_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_job(**fields)


def append_generation_attempt(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_attempt(**fields)


def append_generation_event(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_event(**fields)


def insert_generation_job_status_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.insert_generation_job_status_projection(**fields)


def append_generation_attempt_outcome(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_attempt_outcome(**fields)


def append_generation_text_chunk(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_text_chunk(**fields)


def append_request_manifest_once(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    conversation_id: str,
    branch_id: str,
    generation_job_id: str,
    generation_attempt_id: str,
    trigger_message_id: str,
    provider_invocation_id: str,
    request_manifest_id: str,
    idempotency_key: str,
    manifest_body: Mapping[str, Any],
    created_at_us: int,
    schema_version: int = 1,
) -> RequestManifest:
    """Append one attempt's manifest, or return the identical durable replay.

    The idempotency boundary is the generation attempt. Re-running the same
    attempt after a crash may see the existing manifest and continue only if the
    canonical bytes still match; any drift in path selection, policy, model,
    tool schemas or request options is refused before a provider can be invoked.
    """
    expected_digest = _canonical_json_object_digest(manifest_body)
    existing = read_request_manifest(
        connection,
        workspace_id=workspace_id,
        generation_attempt_id=generation_attempt_id,
    )
    if existing is not None:
        if (
            existing.manifest_digest != expected_digest
            or _canonical_json_object(existing.manifest_body)
            != _canonical_json_object(manifest_body)
        ):
            raise RequestManifestConflict(
                f"generation attempt {generation_attempt_id!r} already has a different request manifest"
            )
        return existing
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_request_manifest(
            conversation_id=conversation_id,
            branch_id=branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            trigger_message_id=trigger_message_id,
            provider_invocation_id=provider_invocation_id,
            request_manifest_id=request_manifest_id,
            idempotency_key=idempotency_key,
            manifest_body=manifest_body,
            manifest_digest=expected_digest,
            created_at_us=created_at_us,
            schema_version=schema_version,
        )
    appended = read_request_manifest(
        connection,
        workspace_id=workspace_id,
        generation_attempt_id=generation_attempt_id,
    )
    if appended is None:  # pragma: no cover - the transaction just committed it
        raise StorageError(
            f"chat request manifest {request_manifest_id!r} did not settle"
        )
    return appended


def append_chat_turn(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_turn(**fields)


def append_chat_turn_step(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_turn_step(**fields)


def append_chat_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_tool_call(**fields)


def append_chat_tool_result(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_tool_result(**fields)


def insert_queue_order_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.insert_queue_order_projection(**fields)


def append_outbox_entry(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_outbox_entry(**fields)


def update_conversation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_conversation(**fields)


def update_branch_head(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_branch_head(**fields)


def update_view_state(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_view_state(**fields)


def update_draft(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_draft(**fields)


def update_queued_submission(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_queued_submission(**fields)


def update_generation_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_generation_job(**fields)


def update_generation_job_status_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_generation_job_status_projection(**fields)


def update_queue_order_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_queue_order_projection(**fields)


def update_chat_turn(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_turn(**fields)


def update_chat_turn_step(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_turn_step(**fields)


def update_chat_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_tool_call(**fields)


def update_outbox_delivery(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    **fields: Any,
) -> None:
    with chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_outbox_delivery(**fields)


# --- reads: workspace-scoped, deterministic --------------------------------------

_CONVERSATION_COLUMNS = (
    "workspace_id, conversation_id, title, title_source, state, "
    "default_branch_id, graph_revision, latest_conversation_sequence, "
    "schema_version, created_by_actor_id, created_at_us, updated_at_us, "
    "archived_at_us, tombstoned_at_us"
)


def _conversation_from_row(row: tuple[Any, ...]) -> Conversation:
    return Conversation(
        workspace_id=row[0],
        conversation_id=row[1],
        title=row[2],
        title_source=row[3],
        state=row[4],
        default_branch_id=row[5],
        graph_revision=row[6],
        latest_conversation_sequence=row[7],
        schema_version=row[8],
        created_by_actor_id=row[9],
        created_at_us=row[10],
        updated_at_us=row[11],
        archived_at_us=row[12],
        tombstoned_at_us=row[13],
    )


def read_conversation(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> Conversation | None:
    row = connection.execute(
        f"SELECT {_CONVERSATION_COLUMNS} FROM omnivia_chat_conversations "
        "WHERE workspace_id = ? AND conversation_id = ?",
        (workspace_id, conversation_id),
    ).fetchone()
    return None if row is None else _conversation_from_row(row)


_MESSAGE_COLUMNS = (
    "workspace_id, conversation_id, message_id, parent_message_id, role, "
    "author_type, author_id, conversation_sequence, schema_version, content_hash, "
    "completion_status, visibility, created_on_branch_id, generation_job_id, "
    "created_at_us, committed_at_us, tombstoned_at_us"
)


def _message_from_row(row: tuple[Any, ...]) -> Message:
    return Message(
        workspace_id=row[0],
        conversation_id=row[1],
        message_id=row[2],
        parent_message_id=row[3],
        role=row[4],
        author_type=row[5],
        author_id=row[6],
        conversation_sequence=row[7],
        schema_version=row[8],
        content_hash=row[9],
        completion_status=row[10],
        visibility=row[11],
        created_on_branch_id=row[12],
        generation_job_id=row[13],
        created_at_us=row[14],
        committed_at_us=row[15],
        tombstoned_at_us=row[16],
    )


def read_messages_by_conversation_sequence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    from_sequence: int = 1,
    limit: int | None = None,
) -> tuple[Message, ...]:
    query = (
        f"SELECT {_MESSAGE_COLUMNS} FROM omnivia_chat_messages "
        "WHERE workspace_id = ? AND conversation_id = ? AND conversation_sequence >= ? "
        "ORDER BY conversation_sequence"
    )
    params: list[Any] = [workspace_id, conversation_id, from_sequence]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(query, params).fetchall()
    return tuple(_message_from_row(row) for row in rows)


_MESSAGE_PART_COLUMNS = (
    "workspace_id, conversation_id, message_id, part_id, part_index, part_type, "
    "schema_version, visibility, payload_json, provenance, content_hash, created_at_us"
)


def _message_part_from_row(row: tuple[Any, ...]) -> MessagePart:
    return MessagePart(
        workspace_id=row[0],
        conversation_id=row[1],
        message_id=row[2],
        part_id=row[3],
        part_index=row[4],
        part_type=row[5],
        schema_version=row[6],
        visibility=row[7],
        payload=_verified_json_object(row[8], "chat message part payload"),
        provenance=row[9],
        content_hash=row[10],
        created_at_us=row[11],
    )


def read_message_parts(
    connection: sqlite3.Connection, *, workspace_id: str, message_id: str
) -> tuple[MessagePart, ...]:
    rows = connection.execute(
        f"SELECT {_MESSAGE_PART_COLUMNS} FROM omnivia_chat_message_parts "
        "WHERE workspace_id = ? AND message_id = ? ORDER BY part_index",
        (workspace_id, message_id),
    ).fetchall()
    return tuple(_message_part_from_row(row) for row in rows)


_BRANCH_COLUMNS = (
    "workspace_id, conversation_id, branch_id, origin_kind, created_from_branch_id, "
    "fork_parent_message_id, fork_source_message_id, initial_head_message_id, "
    "current_head_message_id, created_by_actor_id, created_at_us, "
    "created_conversation_sequence, head_version, schema_version, state, "
    "archived_at_us, tombstoned_at_us"
)


def _branch_from_row(row: tuple[Any, ...]) -> Branch:
    return Branch(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        origin_kind=row[3],
        created_from_branch_id=row[4],
        fork_parent_message_id=row[5],
        fork_source_message_id=row[6],
        initial_head_message_id=row[7],
        current_head_message_id=row[8],
        created_by_actor_id=row[9],
        created_at_us=row[10],
        created_conversation_sequence=row[11],
        head_version=row[12],
        schema_version=row[13],
        state=row[14],
        archived_at_us=row[15],
        tombstoned_at_us=row[16],
    )


def read_branch(
    connection: sqlite3.Connection, *, workspace_id: str, branch_id: str
) -> Branch | None:
    """One branch's row, including the head projection a command appends against.

    The projection rather than the head-event history: `head_version` and
    `current_head_message_id` are what 0029 holds an advancing writer to, so a
    command checking a caller's expected head against anything else would be
    checking against a value the guard does not use.
    """
    row = connection.execute(
        f"SELECT {_BRANCH_COLUMNS} FROM omnivia_chat_message_branches "
        "WHERE workspace_id = ? AND branch_id = ?",
        (workspace_id, branch_id),
    ).fetchone()
    return None if row is None else _branch_from_row(row)


_BRANCH_HEAD_EVENT_COLUMNS = (
    "workspace_id, conversation_id, branch_id, event_id, head_version, "
    "previous_head_message_id, new_head_message_id, cause, command_id, "
    "graph_revision, conversation_sequence, actor_id, occurred_at_us, schema_version"
)


def _branch_head_event_from_row(row: tuple[Any, ...]) -> BranchHeadEvent:
    return BranchHeadEvent(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        event_id=row[3],
        head_version=row[4],
        previous_head_message_id=row[5],
        new_head_message_id=row[6],
        cause=row[7],
        command_id=row[8],
        graph_revision=row[9],
        conversation_sequence=row[10],
        actor_id=row[11],
        occurred_at_us=row[12],
        schema_version=row[13],
    )


def read_branch_head_events(
    connection: sqlite3.Connection, *, workspace_id: str, branch_id: str
) -> tuple[BranchHeadEvent, ...]:
    rows = connection.execute(
        f"SELECT {_BRANCH_HEAD_EVENT_COLUMNS} FROM omnivia_chat_branch_head_events "
        "WHERE workspace_id = ? AND branch_id = ? ORDER BY head_version",
        (workspace_id, branch_id),
    ).fetchall()
    return tuple(_branch_head_event_from_row(row) for row in rows)


_VIEW_STATE_COLUMNS = (
    "workspace_id, conversation_id, actor_id, device_id, active_branch_id, "
    "focused_message_id, last_seen_graph_revision, schema_version, version, "
    "updated_at_us"
)


def _view_state_from_row(row: tuple[Any, ...]) -> ViewState:
    return ViewState(
        workspace_id=row[0],
        conversation_id=row[1],
        actor_id=row[2],
        device_id=row[3],
        active_branch_id=row[4],
        focused_message_id=row[5],
        last_seen_graph_revision=row[6],
        schema_version=row[7],
        version=row[8],
        updated_at_us=row[9],
    )


def read_actor_view_state(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    device_id: str = "",
) -> ViewState | None:
    row = connection.execute(
        f"SELECT {_VIEW_STATE_COLUMNS} FROM omnivia_chat_conversation_view_states "
        "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
        "AND device_id = ?",
        (workspace_id, conversation_id, actor_id, device_id),
    ).fetchone()
    return None if row is None else _view_state_from_row(row)


_DRAFT_COLUMNS = (
    "workspace_id, conversation_id, actor_id, device_id, draft_id, mode, "
    "source_message_id, text_content, references_json, target_json, "
    "stashed_from_draft_id, schema_version, version, updated_at_us, expires_at_us"
)


def _draft_from_row(row: tuple[Any, ...]) -> Draft:
    return Draft(
        workspace_id=row[0],
        conversation_id=row[1],
        actor_id=row[2],
        device_id=row[3],
        draft_id=row[4],
        mode=row[5],
        source_message_id=row[6],
        text_content=row[7],
        references=_verified_json_array(row[8], "chat draft references"),
        target=None if row[9] is None else _verified_json_object(row[9], "chat draft target"),
        stashed_from_draft_id=row[10],
        schema_version=row[11],
        version=row[12],
        updated_at_us=row[13],
        expires_at_us=row[14],
    )


def read_active_draft(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    device_id: str = "",
    mode: str = "normal",
) -> Draft | None:
    row = connection.execute(
        f"SELECT {_DRAFT_COLUMNS} FROM omnivia_chat_drafts "
        "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
        "AND device_id = ? AND mode = ?",
        (workspace_id, conversation_id, actor_id, device_id, mode),
    ).fetchone()
    return None if row is None else _draft_from_row(row)


_QUEUED_SUBMISSION_COLUMNS = (
    "workspace_id, conversation_id, actor_id, queued_submission_id, queue_sequence, "
    "branch_id, editable_parts_json, references_json, idempotency_key, state, "
    "version, claimed_by, claim_epoch, claim_expires_at_us, submitted_message_id, "
    "submitted_generation_job_id, sanitized_error_code, sanitized_error_detail, "
    "created_at_us, updated_at_us"
)


def _queued_submission_from_row(row: tuple[Any, ...]) -> QueuedSubmission:
    return QueuedSubmission(
        workspace_id=row[0],
        conversation_id=row[1],
        actor_id=row[2],
        queued_submission_id=row[3],
        queue_sequence=row[4],
        branch_id=row[5],
        editable_parts=_verified_json_array(row[6], "chat queued submission editable parts"),
        references=_verified_json_array(row[7], "chat queued submission references"),
        idempotency_key=row[8],
        state=row[9],
        version=row[10],
        claimed_by=row[11],
        claim_epoch=row[12],
        claim_expires_at_us=row[13],
        submitted_message_id=row[14],
        submitted_generation_job_id=row[15],
        sanitized_error_code=row[16],
        sanitized_error_detail=row[17],
        created_at_us=row[18],
        updated_at_us=row[19],
    )


def read_queued_submission(
    connection: sqlite3.Connection, *, workspace_id: str, queued_submission_id: str
) -> QueuedSubmission | None:
    row = connection.execute(
        f"SELECT {_QUEUED_SUBMISSION_COLUMNS} FROM omnivia_chat_queued_submissions "
        "WHERE workspace_id = ? AND queued_submission_id = ?",
        (workspace_id, queued_submission_id),
    ).fetchone()
    return None if row is None else _queued_submission_from_row(row)


def read_next_queued_submission(
    connection: sqlite3.Connection, *, workspace_id: str
) -> QueuedSubmission | None:
    """Return the next claimable submission in deterministic queue order.

    0030's queue-order projection is authoritative when present; 0029's immutable
    `queue_sequence` is the fallback for rows created before the projection existed.
    The state predicate is part of the query: a restarted worker never mistakes an
    already submitted row for fresh work.
    """
    row = connection.execute(
        "SELECT q.workspace_id, q.conversation_id, q.actor_id, q.queued_submission_id, "
        "q.queue_sequence, q.branch_id, q.editable_parts_json, q.references_json, "
        "q.idempotency_key, q.state, q.version, q.claimed_by, q.claim_epoch, "
        "q.claim_expires_at_us, q.submitted_message_id, q.submitted_generation_job_id, "
        "q.sanitized_error_code, q.sanitized_error_detail, q.created_at_us, q.updated_at_us "
        "FROM omnivia_chat_queued_submissions q "
        "LEFT JOIN omnivia_chat_queue_order_projection p "
        "ON p.workspace_id = q.workspace_id "
        "AND p.conversation_id = q.conversation_id "
        "AND p.queued_submission_id = q.queued_submission_id "
        "WHERE q.workspace_id = ? AND q.state = 'queued' "
        "ORDER BY COALESCE(p.queue_position, q.queue_sequence), "
        "q.created_at_us, q.conversation_id, q.queued_submission_id "
        "LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return None if row is None else _queued_submission_from_row(row)


_GENERATION_ATTEMPT_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
    "attempt_number, retry_of_attempt_id, state, provider_invocation_id, "
    "schema_version, started_at_us, ended_at_us"
)


def _generation_attempt_from_row(row: tuple[Any, ...]) -> GenerationAttempt:
    return GenerationAttempt(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        generation_attempt_id=row[3],
        attempt_number=row[4],
        retry_of_attempt_id=row[5],
        state=row[6],
        provider_invocation_id=row[7],
        schema_version=row[8],
        started_at_us=row[9],
        ended_at_us=row[10],
    )


def read_generation_attempt(
    connection: sqlite3.Connection, *, workspace_id: str, generation_attempt_id: str
) -> GenerationAttempt | None:
    row = connection.execute(
        f"SELECT {_GENERATION_ATTEMPT_COLUMNS} FROM omnivia_chat_generation_attempts "
        "WHERE workspace_id = ? AND generation_attempt_id = ?",
        (workspace_id, generation_attempt_id),
    ).fetchone()
    return None if row is None else _generation_attempt_from_row(row)


def read_generation_attempts(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> tuple[GenerationAttempt, ...]:
    rows = connection.execute(
        f"SELECT {_GENERATION_ATTEMPT_COLUMNS} FROM omnivia_chat_generation_attempts "
        "WHERE workspace_id = ? AND generation_job_id = ? ORDER BY attempt_number",
        (workspace_id, generation_job_id),
    ).fetchall()
    return tuple(_generation_attempt_from_row(row) for row in rows)


_GENERATION_JOB_COLUMNS = (
    "workspace_id, conversation_id, branch_id, trigger_message_id, generation_job_id, "
    "state, graph_revision_observed, idempotency_key, current_attempt_id, "
    "result_message_id, lease_owner, lease_epoch, lease_expires_at_us, "
    "heartbeat_at_us, last_event_sequence, sanitized_error_code, "
    "sanitized_error_detail, schema_version, created_at_us, updated_at_us, "
    "started_at_us, finished_at_us"
)


def _generation_job_from_row(row: tuple[Any, ...]) -> GenerationJob:
    return GenerationJob(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        trigger_message_id=row[3],
        generation_job_id=row[4],
        state=row[5],
        graph_revision_observed=row[6],
        idempotency_key=row[7],
        current_attempt_id=row[8],
        result_message_id=row[9],
        lease_owner=row[10],
        lease_epoch=row[11],
        lease_expires_at_us=row[12],
        heartbeat_at_us=row[13],
        last_event_sequence=row[14],
        sanitized_error_code=row[15],
        sanitized_error_detail=row[16],
        schema_version=row[17],
        created_at_us=row[18],
        updated_at_us=row[19],
        started_at_us=row[20],
        finished_at_us=row[21],
    )


def read_generation_job(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> GenerationJob | None:
    row = connection.execute(
        f"SELECT {_GENERATION_JOB_COLUMNS} FROM omnivia_chat_generation_jobs "
        "WHERE workspace_id = ? AND generation_job_id = ?",
        (workspace_id, generation_job_id),
    ).fetchone()
    return None if row is None else _generation_job_from_row(row)


_GENERATION_EVENT_COLUMNS = (
    "workspace_id, conversation_id, branch_id, generation_job_id, "
    "generation_attempt_id, event_id, event_type, generation_event_sequence, "
    "trigger_message_id, result_message_id, provider_event_id, cursor, "
    "payload_json, occurred_at_us, schema_version"
)


def _generation_event_from_row(row: tuple[Any, ...]) -> GenerationEvent:
    return GenerationEvent(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        generation_job_id=row[3],
        generation_attempt_id=row[4],
        event_id=row[5],
        event_type=row[6],
        generation_event_sequence=row[7],
        trigger_message_id=row[8],
        result_message_id=row[9],
        provider_event_id=row[10],
        cursor=row[11],
        payload=_verified_json_object(row[12], "chat generation event payload"),
        occurred_at_us=row[13],
        schema_version=row[14],
    )


def read_generation_events(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> tuple[GenerationEvent, ...]:
    rows = connection.execute(
        f"SELECT {_GENERATION_EVENT_COLUMNS} FROM omnivia_chat_generation_events "
        "WHERE workspace_id = ? AND generation_job_id = ? "
        "ORDER BY generation_event_sequence",
        (workspace_id, generation_job_id),
    ).fetchall()
    return tuple(_generation_event_from_row(row) for row in rows)


_GENERATION_JOB_STATUS_PROJECTION_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, state, current_attempt_id, "
    "result_message_id, sanitized_error_code, sanitized_error_detail, version, "
    "schema_version, updated_at_us, finished_at_us"
)


def _generation_job_status_projection_from_row(
    row: tuple[Any, ...],
) -> GenerationJobStatusProjection:
    return GenerationJobStatusProjection(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        state=row[3],
        current_attempt_id=row[4],
        result_message_id=row[5],
        sanitized_error_code=row[6],
        sanitized_error_detail=row[7],
        version=row[8],
        schema_version=row[9],
        updated_at_us=row[10],
        finished_at_us=row[11],
    )


def read_generation_job_status_projection(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> GenerationJobStatusProjection | None:
    row = connection.execute(
        f"SELECT {_GENERATION_JOB_STATUS_PROJECTION_COLUMNS} "
        "FROM omnivia_chat_generation_job_status_projection "
        "WHERE workspace_id = ? AND generation_job_id = ?",
        (workspace_id, generation_job_id),
    ).fetchone()
    return None if row is None else _generation_job_status_projection_from_row(row)


_GENERATION_ATTEMPT_OUTCOME_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
    "terminal_state, result_message_id, provider_event_id, retryable, "
    "sanitized_error_code, sanitized_error_detail, schema_version, occurred_at_us"
)


def _generation_attempt_outcome_from_row(row: tuple[Any, ...]) -> GenerationAttemptOutcome:
    return GenerationAttemptOutcome(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        generation_attempt_id=row[3],
        terminal_state=row[4],
        result_message_id=row[5],
        provider_event_id=row[6],
        retryable=bool(row[7]),
        sanitized_error_code=row[8],
        sanitized_error_detail=row[9],
        schema_version=row[10],
        occurred_at_us=row[11],
    )


def read_generation_attempt_outcome(
    connection: sqlite3.Connection, *, workspace_id: str, generation_attempt_id: str
) -> GenerationAttemptOutcome | None:
    row = connection.execute(
        f"SELECT {_GENERATION_ATTEMPT_OUTCOME_COLUMNS} "
        "FROM omnivia_chat_generation_attempt_outcomes "
        "WHERE workspace_id = ? AND generation_attempt_id = ?",
        (workspace_id, generation_attempt_id),
    ).fetchone()
    return None if row is None else _generation_attempt_outcome_from_row(row)


def read_generation_attempt_outcomes(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> tuple[GenerationAttemptOutcome, ...]:
    rows = connection.execute(
        f"SELECT {_GENERATION_ATTEMPT_OUTCOME_COLUMNS} "
        "FROM omnivia_chat_generation_attempt_outcomes "
        "WHERE workspace_id = ? AND generation_job_id = ? ORDER BY occurred_at_us, generation_attempt_id",
        (workspace_id, generation_job_id),
    ).fetchall()
    return tuple(_generation_attempt_outcome_from_row(row) for row in rows)


_GENERATION_TEXT_CHUNK_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
    "chunk_ordinal, provider_event_id, text_content, content_hash, schema_version, "
    "occurred_at_us"
)


def _generation_text_chunk_from_row(row: tuple[Any, ...]) -> GenerationTextChunk:
    return GenerationTextChunk(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        generation_attempt_id=row[3],
        chunk_ordinal=row[4],
        provider_event_id=row[5],
        text_content=row[6],
        content_hash=row[7],
        schema_version=row[8],
        occurred_at_us=row[9],
    )


def read_generation_text_chunks(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    generation_job_id: str,
    generation_attempt_id: str | None = None,
) -> tuple[GenerationTextChunk, ...]:
    query = (
        f"SELECT {_GENERATION_TEXT_CHUNK_COLUMNS} "
        "FROM omnivia_chat_generation_text_chunks "
        "WHERE workspace_id = ? AND generation_job_id = ?"
    )
    params: list[Any] = [workspace_id, generation_job_id]
    if generation_attempt_id is not None:
        query += " AND generation_attempt_id = ?"
        params.append(generation_attempt_id)
    query += " ORDER BY generation_attempt_id, chunk_ordinal"
    rows = connection.execute(query, params).fetchall()
    return tuple(_generation_text_chunk_from_row(row) for row in rows)


_REQUEST_MANIFEST_COLUMNS = (
    "workspace_id, conversation_id, branch_id, generation_job_id, "
    "generation_attempt_id, trigger_message_id, provider_invocation_id, "
    "request_manifest_id, idempotency_key, schema_version, manifest_digest, "
    "request_manifest_body_json, created_at_us"
)


def _request_manifest_from_row(row: tuple[Any, ...]) -> RequestManifest:
    body = _verified_json_object(row[11], "chat request manifest body")
    digest = _canonical_json_object_digest(body)
    if digest != row[10]:
        raise StorageError("a stored chat request manifest digest does not match its body")
    return RequestManifest(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        generation_job_id=row[3],
        generation_attempt_id=row[4],
        trigger_message_id=row[5],
        provider_invocation_id=row[6],
        request_manifest_id=row[7],
        idempotency_key=row[8],
        schema_version=row[9],
        manifest_digest=row[10],
        manifest_body=body,
        created_at_us=row[12],
    )


def read_request_manifest(
    connection: sqlite3.Connection, *, workspace_id: str, generation_attempt_id: str
) -> RequestManifest | None:
    row = connection.execute(
        f"SELECT {_REQUEST_MANIFEST_COLUMNS} FROM omnivia_chat_request_manifests "
        "WHERE workspace_id = ? AND generation_attempt_id = ?",
        (workspace_id, generation_attempt_id),
    ).fetchone()
    return None if row is None else _request_manifest_from_row(row)


_CHAT_TURN_COLUMNS = (
    "workspace_id, conversation_id, branch_id, generation_job_id, "
    "generation_attempt_id, turn_id, state, current_step_id, version, "
    "schema_version, created_at_us, updated_at_us, finished_at_us"
)


def _chat_turn_from_row(row: tuple[Any, ...]) -> ChatTurn:
    return ChatTurn(
        workspace_id=row[0],
        conversation_id=row[1],
        branch_id=row[2],
        generation_job_id=row[3],
        generation_attempt_id=row[4],
        turn_id=row[5],
        state=row[6],
        current_step_id=row[7],
        version=row[8],
        schema_version=row[9],
        created_at_us=row[10],
        updated_at_us=row[11],
        finished_at_us=row[12],
    )


def read_chat_turn(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> ChatTurn | None:
    row = connection.execute(
        f"SELECT {_CHAT_TURN_COLUMNS} FROM omnivia_chat_turns "
        "WHERE workspace_id = ? AND turn_id = ?",
        (workspace_id, turn_id),
    ).fetchone()
    return None if row is None else _chat_turn_from_row(row)


def read_chat_turn_by_attempt(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    generation_job_id: str,
    generation_attempt_id: str,
) -> ChatTurn | None:
    row = connection.execute(
        f"SELECT {_CHAT_TURN_COLUMNS} FROM omnivia_chat_turns "
        "WHERE workspace_id = ? AND generation_job_id = ? "
        "AND generation_attempt_id = ?",
        (workspace_id, generation_job_id, generation_attempt_id),
    ).fetchone()
    return None if row is None else _chat_turn_from_row(row)


_CHAT_TURN_STEP_COLUMNS = (
    "workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
    "generation_attempt_id, step_ordinal, step_kind, state, version, "
    "schema_version, created_at_us, updated_at_us, finished_at_us"
)


def _chat_turn_step_from_row(row: tuple[Any, ...]) -> ChatTurnStep:
    return ChatTurnStep(
        workspace_id=row[0],
        conversation_id=row[1],
        turn_id=row[2],
        step_id=row[3],
        generation_job_id=row[4],
        generation_attempt_id=row[5],
        step_ordinal=row[6],
        step_kind=row[7],
        state=row[8],
        version=row[9],
        schema_version=row[10],
        created_at_us=row[11],
        updated_at_us=row[12],
        finished_at_us=row[13],
    )


def read_chat_turn_steps(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> tuple[ChatTurnStep, ...]:
    rows = connection.execute(
        f"SELECT {_CHAT_TURN_STEP_COLUMNS} FROM omnivia_chat_turn_steps "
        "WHERE workspace_id = ? AND turn_id = ? ORDER BY step_ordinal",
        (workspace_id, turn_id),
    ).fetchall()
    return tuple(_chat_turn_step_from_row(row) for row in rows)


_CHAT_TOOL_CALL_COLUMNS = (
    "workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
    "generation_attempt_id, tool_call_id, tool_name, tool_version, registry_ref, "
    "state, policy_state, proposed_arguments_json, proposed_arguments_digest, "
    "post_policy_arguments_json, post_policy_arguments_digest, "
    "executed_arguments_digest, result_id, failure_code, version, schema_version, "
    "created_at_us, updated_at_us, finished_at_us"
)


def _chat_tool_call_from_row(row: tuple[Any, ...]) -> ChatToolCall:
    proposed_arguments = _verified_json_object(row[12], "chat tool proposed arguments")
    proposed_digest = _canonical_json_object_digest(proposed_arguments)
    if proposed_digest != row[13]:
        raise StorageError("a stored chat tool proposed-arguments digest does not match")
    post_policy_arguments = (
        None
        if row[14] is None
        else _verified_json_object(row[14], "chat tool post-policy arguments")
    )
    if post_policy_arguments is None:
        if row[15] is not None:
            raise StorageError("a stored chat tool post-policy digest has no arguments")
    elif _canonical_json_object_digest(post_policy_arguments) != row[15]:
        raise StorageError("a stored chat tool post-policy digest does not match")
    return ChatToolCall(
        workspace_id=row[0],
        conversation_id=row[1],
        turn_id=row[2],
        step_id=row[3],
        generation_job_id=row[4],
        generation_attempt_id=row[5],
        tool_call_id=row[6],
        tool_name=row[7],
        tool_version=row[8],
        registry_ref=row[9],
        state=row[10],
        policy_state=row[11],
        proposed_arguments=proposed_arguments,
        proposed_arguments_digest=row[13],
        post_policy_arguments=post_policy_arguments,
        post_policy_arguments_digest=row[15],
        executed_arguments_digest=row[16],
        result_id=row[17],
        failure_code=row[18],
        version=row[19],
        schema_version=row[20],
        created_at_us=row[21],
        updated_at_us=row[22],
        finished_at_us=row[23],
    )


def read_chat_tool_call(
    connection: sqlite3.Connection, *, workspace_id: str, tool_call_id: str
) -> ChatToolCall | None:
    row = connection.execute(
        f"SELECT {_CHAT_TOOL_CALL_COLUMNS} FROM omnivia_chat_tool_calls "
        "WHERE workspace_id = ? AND tool_call_id = ?",
        (workspace_id, tool_call_id),
    ).fetchone()
    return None if row is None else _chat_tool_call_from_row(row)


def read_chat_tool_calls(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> tuple[ChatToolCall, ...]:
    rows = connection.execute(
        f"SELECT {_CHAT_TOOL_CALL_COLUMNS} FROM omnivia_chat_tool_calls "
        "WHERE workspace_id = ? AND turn_id = ? ORDER BY created_at_us, tool_call_id",
        (workspace_id, turn_id),
    ).fetchall()
    return tuple(_chat_tool_call_from_row(row) for row in rows)


_CHAT_TOOL_RESULT_COLUMNS = (
    "workspace_id, conversation_id, turn_id, step_id, generation_job_id, "
    "generation_attempt_id, tool_call_id, result_id, status, result_payload_json, "
    "result_digest, schema_version, created_at_us"
)


def _chat_tool_result_from_row(row: tuple[Any, ...]) -> ChatToolResult:
    result_payload = _verified_json_object(row[9], "chat tool result payload")
    digest = _canonical_json_object_digest(result_payload)
    if digest != row[10]:
        raise StorageError("a stored chat tool result digest does not match its payload")
    return ChatToolResult(
        workspace_id=row[0],
        conversation_id=row[1],
        turn_id=row[2],
        step_id=row[3],
        generation_job_id=row[4],
        generation_attempt_id=row[5],
        tool_call_id=row[6],
        result_id=row[7],
        status=row[8],
        result_payload=result_payload,
        result_digest=row[10],
        schema_version=row[11],
        created_at_us=row[12],
    )


def read_chat_tool_result(
    connection: sqlite3.Connection, *, workspace_id: str, tool_call_id: str
) -> ChatToolResult | None:
    row = connection.execute(
        f"SELECT {_CHAT_TOOL_RESULT_COLUMNS} FROM omnivia_chat_tool_results "
        "WHERE workspace_id = ? AND tool_call_id = ?",
        (workspace_id, tool_call_id),
    ).fetchone()
    return None if row is None else _chat_tool_result_from_row(row)


_QUEUE_ORDER_PROJECTION_COLUMNS = (
    "workspace_id, conversation_id, queued_submission_id, queue_position, version, "
    "updated_by_actor_id, updated_at_us"
)


def _queue_order_projection_from_row(row: tuple[Any, ...]) -> QueueOrderProjection:
    return QueueOrderProjection(
        workspace_id=row[0],
        conversation_id=row[1],
        queued_submission_id=row[2],
        queue_position=row[3],
        version=row[4],
        updated_by_actor_id=row[5],
        updated_at_us=row[6],
    )


def read_queue_order_projection(
    connection: sqlite3.Connection, *, workspace_id: str, queued_submission_id: str
) -> QueueOrderProjection | None:
    row = connection.execute(
        f"SELECT {_QUEUE_ORDER_PROJECTION_COLUMNS} FROM omnivia_chat_queue_order_projection "
        "WHERE workspace_id = ? AND queued_submission_id = ?",
        (workspace_id, queued_submission_id),
    ).fetchone()
    return None if row is None else _queue_order_projection_from_row(row)


def read_queue_order_for_conversation(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> tuple[QueueOrderProjection, ...]:
    rows = connection.execute(
        f"SELECT {_QUEUE_ORDER_PROJECTION_COLUMNS} FROM omnivia_chat_queue_order_projection "
        "WHERE workspace_id = ? AND conversation_id = ? ORDER BY queue_position, queued_submission_id",
        (workspace_id, conversation_id),
    ).fetchall()
    return tuple(_queue_order_projection_from_row(row) for row in rows)


_OUTBOX_COLUMNS = (
    "workspace_id, outbox_cursor, domain_event_id, event_kind, conversation_id, "
    "generation_job_id, payload_json, delivery_state, delivery_attempts, "
    "next_delivery_after_us, delivered_at_us, retained_until_us, created_at_us"
)


def _outbox_entry_from_row(row: tuple[Any, ...]) -> OutboxEntry:
    return OutboxEntry(
        workspace_id=row[0],
        outbox_cursor=row[1],
        domain_event_id=row[2],
        event_kind=row[3],
        conversation_id=row[4],
        generation_job_id=row[5],
        payload=_verified_json_object(row[6], "chat outbox payload"),
        delivery_state=row[7],
        delivery_attempts=row[8],
        next_delivery_after_us=row[9],
        delivered_at_us=row[10],
        retained_until_us=row[11],
        created_at_us=row[12],
    )


def read_outbox_event(
    connection: sqlite3.Connection, *, workspace_id: str, domain_event_id: str
) -> OutboxEntry | None:
    """One outbox row by its domain event identity."""
    row = connection.execute(
        f"SELECT {_OUTBOX_COLUMNS} FROM omnivia_chat_transactional_outbox "
        "WHERE workspace_id = ? AND domain_event_id = ?",
        (workspace_id, domain_event_id),
    ).fetchone()
    return None if row is None else _outbox_entry_from_row(row)


def read_outbox_events_since(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    after_cursor: int = 0,
    limit: int = 100,
) -> tuple[OutboxEntry, ...]:
    """Outbox rows after a delivery cursor, in cursor order -- the delivery
    worker's own read.
    """
    rows = connection.execute(
        f"SELECT {_OUTBOX_COLUMNS} FROM omnivia_chat_transactional_outbox "
        "WHERE workspace_id = ? AND outbox_cursor > ? ORDER BY outbox_cursor LIMIT ?",
        (workspace_id, after_cursor, limit),
    ).fetchall()
    return tuple(_outbox_entry_from_row(row) for row in rows)
