"""Durable repository over the Chat foundation tables (migrations 0029 and 0030, W2-R).

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

Migration 0030 adds three successor projections and this module reaches them the
same way. Two are append-only facts -- the terminal Generation Attempt outcome
and durable generation text chunks -- and the third, the per-conversation queue
order, is a compare-and-set projection keyed by its own `version`.

Two of the reads here are *composed* rather than row-oriented, because the thing
they answer is not one table's row. :func:`read_effective_generation_job` answers
what a Job's state actually is: 0029's base row plus its Attempts plus each
Attempt's terminal outcome fact. A Job whose base row is still `running` and
whose latest Attempt failed projects as the non-terminal `retryable`; appending
the next Attempt projects it as `running` again; a base terminal Job stays
terminal and never projects `retryable`. :func:`read_effective_queue_order`
answers what order a conversation's queue is actually in: the 0030 projection
where one exists, then every queued submission the projection does not name, in
0029's immutable `queue_sequence` order. Both fall back cleanly on a database
that has 0030's tables but no rows in them -- which is every workspace upgraded
from 0029 -- and neither invents a row the successor tables do not hold.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "MAX_CHUNK_BATCH",
    "MAX_QUEUE_ORDER_MEMBERS",
    "Branch",
    "BranchHeadEvent",
    "ChatWriter",
    "Conversation",
    "ConversationSnapshotInputs",
    "Draft",
    "EffectiveAttempt",
    "EffectiveJob",
    "GenerationAttempt",
    "GenerationAttemptOutcome",
    "GenerationChunk",
    "GenerationEvent",
    "GenerationJob",
    "Message",
    "MessageDerivation",
    "MessagePart",
    "OutboxEntry",
    "QueueOrder",
    "QueuedSubmission",
    "StaleVersion",
    "ViewState",
    "append_branch",
    "append_branch_head_event",
    "append_conversation",
    "append_generation_attempt",
    "append_generation_attempt_outcome",
    "append_generation_chunks",
    "append_generation_event",
    "append_generation_job",
    "append_message",
    "append_message_derivation",
    "append_message_part",
    "append_outbox_entry",
    "append_queued_submission",
    "chat_writer",
    "insert_draft",
    "insert_queue_order",
    "insert_view_state",
    "read_active_draft",
    "read_actor_view_state",
    "read_branch",
    "read_branch_head_events",
    "read_conversation",
    "read_conversation_snapshot_inputs",
    "read_effective_generation_job",
    "read_effective_queue_order",
    "read_generation_attempt_outcome",
    "read_generation_attempts",
    "read_generation_chunks",
    "read_generation_events",
    "read_generation_job",
    "read_message_parts",
    "read_messages_by_conversation_sequence",
    "read_next_queued_submission",
    "read_outbox_event",
    "read_outbox_events_since",
    "read_queue_order",
    "read_queued_submission",
    "transaction_local_writer",
    "update_branch_head",
    "update_conversation",
    "update_draft",
    "update_generation_job",
    "update_outbox_delivery",
    "update_queue_order",
    "update_queued_submission",
    "update_view_state",
]

#: The largest chunk batch one call may append. 0030 bounds each chunk's bytes;
#: this bounds how many a single fenced write may carry, so a caller streaming a
#: long generation commits bounded batches rather than one unbounded transaction.
MAX_CHUNK_BATCH = 256

#: 0030's own bound on a queue order's member count, restated here so a caller is
#: refused before the write rather than by the trigger.
MAX_QUEUE_ORDER_MEMBERS = 1000

#: The Job states 0029 writes and never reopens.
_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})


class StaleVersion(StorageError):
    """A compare-and-set update did not match the row's expected prior state.

    The row is left exactly as it was: the `WHERE` clause carrying the expected
    token matched zero rows, so nothing committed. This is distinct from 0029's own
    trigger refusals (an illegal state transition, a terminal row reopened), which
    surface as `sqlite3.IntegrityError` regardless of whether the expected token
    matched.
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


def _canonical_queue_order(order: Sequence[str]) -> str:
    """`order` as the exact canonical JSON array 0030's trigger will compare against.

    Refused here rather than only in SQL where the refusal can name the caller's
    mistake: a queue order is a bounded sequence of distinct submission ids, and
    a duplicate or a non-string member is a caller error, not a storage one.
    """
    if isinstance(order, (str, bytes, bytearray, Mapping)) or not isinstance(order, Sequence):
        raise StorageError("a chat queue order requires a sequence of submission identifiers")
    members = list(order)
    if not members or len(members) > MAX_QUEUE_ORDER_MEMBERS:
        raise StorageError(
            f"a chat queue order carries between 1 and {MAX_QUEUE_ORDER_MEMBERS} "
            f"submissions, got {len(members)}"
        )
    if not all(isinstance(member, str) for member in members):
        raise StorageError("a chat queue order carries submission identifiers as strings")
    if len(set(members)) != len(members):
        raise StorageError("a chat queue order names a submission more than once")
    return _canonical_json_array(members)


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


@dataclass(frozen=True, slots=True)
class GenerationAttemptOutcome:
    """0030's single terminal fact for one Attempt.

    0029 writes an Attempt's durable start and can never revise it, so this is
    where an Attempt's end lives: exactly one outcome per Attempt, its display-
    safe classification where it failed or was cancelled, and the end timestamp
    it binds.
    """

    workspace_id: str
    conversation_id: str
    generation_job_id: str
    generation_attempt_id: str
    outcome: str
    error_class: str | None
    error_detail: str | None
    schema_version: int
    ended_at_us: int
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class GenerationChunk:
    workspace_id: str
    conversation_id: str
    generation_job_id: str
    generation_attempt_id: str
    chunk_ordinal: int
    provider_event_id: str | None
    text_content: str
    schema_version: int
    created_at_us: int


@dataclass(frozen=True, slots=True)
class EffectiveAttempt:
    """One Attempt as the public GenerationAttempt projection sees it.

    `attempt` is 0029's immutable start row and `outcome` is 0030's terminal fact
    where one has been appended. An Attempt written terminal under 0029 alone --
    every pre-0030 record -- carries no outcome row and reads from its own row,
    which is why `state` and `ended_at_us` are derived rather than read from one
    place.
    """

    attempt: GenerationAttempt
    outcome: GenerationAttemptOutcome | None

    @property
    def state(self) -> str:
        return self.attempt.state if self.outcome is None else self.outcome.outcome

    @property
    def ended_at_us(self) -> int | None:
        return self.attempt.ended_at_us if self.outcome is None else self.outcome.ended_at_us

    @property
    def is_terminal(self) -> bool:
        return self.state != "running"


@dataclass(frozen=True, slots=True)
class EffectiveJob:
    """One Job's base row and the state it actually projects.

    `state` is `job.state` except in one case: a Job whose base row is still
    `running` and whose latest Attempt failed projects the non-terminal
    `retryable`. Nothing here writes that state -- 0029's `state` column and its
    transition trigger are untouched -- and a base terminal Job never projects
    it.
    """

    job: GenerationJob
    state: str
    attempts: tuple[EffectiveAttempt, ...]

    @property
    def latest_attempt(self) -> EffectiveAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def is_terminal(self) -> bool:
        return self.job.state in _TERMINAL_JOB_STATES

    @property
    def is_retryable(self) -> bool:
        return self.state == "retryable"


@dataclass(frozen=True, slots=True)
class QueueOrder:
    workspace_id: str
    conversation_id: str
    order: tuple[str, ...]
    version: int
    created_at_us: int
    updated_at_us: int


@dataclass(frozen=True, slots=True)
class ConversationSnapshotInputs:
    """Everything a Conversation snapshot is composed from, read at one revision.

    The rows, not the contract document: this module owns no Chat Contract shape,
    so the later handler is what turns these into a `ConversationSnapshotResult`.
    `path` is the branch's real message path -- the parent chain walked back from
    the branch head and reversed -- rather than every message that happens to name
    the branch, because that chain is what the snapshot publishes.
    """

    conversation: Conversation
    branch: Branch | None
    view_state: ViewState | None
    path: tuple[Message, ...]
    parts_by_message_id: Mapping[str, tuple[MessagePart, ...]]
    generation_job_ids: tuple[str, ...]


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

    def append_generation_attempt_outcome(
        self,
        *,
        generation_attempt_id: str,
        conversation_id: str,
        generation_job_id: str,
        outcome: str,
        schema_version: int,
        ended_at_us: int,
        recorded_at_us: int,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Append the one terminal fact for an Attempt.

        0030 holds the rest: exactly one outcome per Attempt, a classification
        required for `failed` and `cancelled` and forbidden for `succeeded`, an
        end that cannot precede the Attempt's start, and a refusal for an Attempt
        0029 already wrote terminal.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_generation_attempt_outcomes "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "outcome, error_class, error_detail, schema_version, ended_at_us, "
            "recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                generation_job_id,
                generation_attempt_id,
                outcome,
                error_class,
                error_detail,
                schema_version,
                ended_at_us,
                recorded_at_us,
            ),
        )

    def append_generation_chunks(
        self,
        *,
        generation_attempt_id: str,
        conversation_id: str,
        generation_job_id: str,
        first_ordinal: int,
        chunks: Sequence[tuple[str, str | None]],
        schema_version: int,
        created_at_us: int,
    ) -> None:
        """Append one bounded batch of durable text chunks, in ordinal order.

        `chunks` is `(text_content, provider_event_id)` pairs assigned ordinals
        from `first_ordinal` upwards, and the batch shares one `created_at_us`
        because it shares one commit. 0030 requires the ordinals to be contiguous
        from one within the Attempt, deduplicates on `provider_event_id` where
        the provider supplied one, and refuses any chunk once the Attempt has a
        terminal outcome -- so a caller never needs a per-token transaction to
        keep the stream honest, only a bounded batch per fenced write.
        """
        if not chunks:
            raise StorageError("a chat generation chunk batch must carry at least one chunk")
        if len(chunks) > MAX_CHUNK_BATCH:
            raise StorageError(
                f"a chat generation chunk batch carries at most {MAX_CHUNK_BATCH} chunks, "
                f"got {len(chunks)}"
            )
        self.connection.executemany(
            "INSERT INTO omnivia_chat_generation_chunks "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "chunk_ordinal, provider_event_id, text_content, schema_version, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    self.workspace_id,
                    conversation_id,
                    generation_job_id,
                    generation_attempt_id,
                    first_ordinal + offset,
                    provider_event_id,
                    text_content,
                    schema_version,
                    created_at_us,
                )
                for offset, (text_content, provider_event_id) in enumerate(chunks)
            ],
        )

    def insert_queue_order(
        self,
        *,
        conversation_id: str,
        order: Sequence[str],
        created_at_us: int,
    ) -> None:
        """First write of one conversation's queue order; later writes are
        :meth:`update_queue_order`.

        0030 requires the first write to be version one with `updated_at_us`
        equal to `created_at_us`, so both are fixed here rather than accepted as
        parameters a caller could get wrong.
        """
        self.connection.execute(
            "INSERT INTO omnivia_chat_queued_submission_order "
            "(workspace_id, conversation_id, order_json, version, created_at_us, "
            "updated_at_us) VALUES (?, ?, ?, 1, ?, ?)",
            (
                self.workspace_id,
                conversation_id,
                _canonical_queue_order(order),
                created_at_us,
                created_at_us,
            ),
        )

    # --- compare-and-set: mutable projections ------------------------------------

    def update_queue_order(
        self,
        *,
        conversation_id: str,
        expected_version: int,
        order: Sequence[str],
        updated_at_us: int,
    ) -> None:
        """Reorder a conversation's queue: one row, one compare-and-set, no partial
        reorder to observe.

        The whole order moves or none of it does, because the whole order is one
        value under one `version`. 0030 separately refuses an order naming a
        submission that is no longer `queued`, so a claimed or terminal submission
        cannot be moved regardless of whether the expected version matched.
        """
        cursor = self.connection.execute(
            "UPDATE omnivia_chat_queued_submission_order SET order_json = ?, version = ?, "
            "updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ? AND version = ?",
            (
                _canonical_queue_order(order),
                expected_version + 1,
                updated_at_us,
                self.workspace_id,
                conversation_id,
                expected_version,
            ),
        )
        _require_cas_match(cursor, "queue order", conversation_id)

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


def append_generation_chunks(
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
        writer.append_generation_chunks(**fields)


def insert_queue_order(
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
        writer.insert_queue_order(**fields)


def update_queue_order(
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
        writer.update_queue_order(**fields)


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

    Queue sequence is scoped to a conversation, so the workspace-wide worker uses
    creation time and stable identities as the outer ordering keys.  The state
    predicate is part of the query: a restarted worker never mistakes an already
    submitted row for fresh work.
    """
    row = connection.execute(
        f"SELECT {_QUEUED_SUBMISSION_COLUMNS} FROM omnivia_chat_queued_submissions "
        "WHERE workspace_id = ? AND state = 'queued' "
        "ORDER BY created_at_us, conversation_id, queue_sequence, queued_submission_id "
        "LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return None if row is None else _queued_submission_from_row(row)


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


# --- 0030 successor projections ---------------------------------------------------

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


def read_generation_attempts(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> tuple[GenerationAttempt, ...]:
    """One Job's Attempts in `attempt_number` order -- 0029's contiguous sequence."""
    rows = connection.execute(
        f"SELECT {_GENERATION_ATTEMPT_COLUMNS} FROM omnivia_chat_generation_attempts "
        "WHERE workspace_id = ? AND generation_job_id = ? ORDER BY attempt_number",
        (workspace_id, generation_job_id),
    ).fetchall()
    return tuple(_generation_attempt_from_row(row) for row in rows)


_ATTEMPT_OUTCOME_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
    "outcome, error_class, error_detail, schema_version, ended_at_us, recorded_at_us"
)


def _attempt_outcome_from_row(row: tuple[Any, ...]) -> GenerationAttemptOutcome:
    return GenerationAttemptOutcome(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        generation_attempt_id=row[3],
        outcome=row[4],
        error_class=row[5],
        error_detail=row[6],
        schema_version=row[7],
        ended_at_us=row[8],
        recorded_at_us=row[9],
    )


def read_generation_attempt_outcome(
    connection: sqlite3.Connection, *, workspace_id: str, generation_attempt_id: str
) -> GenerationAttemptOutcome | None:
    row = connection.execute(
        f"SELECT {_ATTEMPT_OUTCOME_COLUMNS} FROM omnivia_chat_generation_attempt_outcomes "
        "WHERE workspace_id = ? AND generation_attempt_id = ?",
        (workspace_id, generation_attempt_id),
    ).fetchone()
    return None if row is None else _attempt_outcome_from_row(row)


_GENERATION_CHUNK_COLUMNS = (
    "workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
    "chunk_ordinal, provider_event_id, text_content, schema_version, created_at_us"
)


def _generation_chunk_from_row(row: tuple[Any, ...]) -> GenerationChunk:
    return GenerationChunk(
        workspace_id=row[0],
        conversation_id=row[1],
        generation_job_id=row[2],
        generation_attempt_id=row[3],
        chunk_ordinal=row[4],
        provider_event_id=row[5],
        text_content=row[6],
        schema_version=row[7],
        created_at_us=row[8],
    )


def read_generation_chunks(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    generation_attempt_id: str,
    after_ordinal: int = 0,
    limit: int = 256,
) -> tuple[GenerationChunk, ...]:
    """One Attempt's durable chunks after an ordinal, in ordinal order.

    Bounded by `limit` and continued by `after_ordinal`, so replaying a long
    generation is a series of bounded reads rather than one unbounded one.
    `limit` is refused outside 1..`MAX_CHUNK_BATCH` rather than passed to SQL,
    where SQLite reads a non-positive `LIMIT` as unbounded.
    """
    if after_ordinal < 0:
        raise StorageError(f"after_ordinal must be >= 0, got {after_ordinal}")
    if not 1 <= limit <= MAX_CHUNK_BATCH:
        raise StorageError(f"limit must be between 1 and {MAX_CHUNK_BATCH}, got {limit}")
    rows = connection.execute(
        f"SELECT {_GENERATION_CHUNK_COLUMNS} FROM omnivia_chat_generation_chunks "
        "WHERE workspace_id = ? AND generation_attempt_id = ? AND chunk_ordinal > ? "
        "ORDER BY chunk_ordinal LIMIT ?",
        (workspace_id, generation_attempt_id, after_ordinal, limit),
    ).fetchall()
    return tuple(_generation_chunk_from_row(row) for row in rows)


def read_effective_generation_job(
    connection: sqlite3.Connection, *, workspace_id: str, generation_job_id: str
) -> EffectiveJob | None:
    """One Job's base row, its Attempts and the state it actually projects.

    The one derived state is `retryable`: the base row still says `running`, the
    latest Attempt has a failed terminal outcome, and a further Attempt is
    therefore admissible. Appending that Attempt makes the latest Attempt
    `running` again and the Job projects `running`. A base terminal Job is
    returned with its own terminal state and never projects `retryable`, so a
    caller reading this cannot mistake a closed Job for a retryable one.
    """
    job = read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        return None

    attempts = tuple(
        EffectiveAttempt(
            attempt=attempt,
            outcome=read_generation_attempt_outcome(
                connection,
                workspace_id=workspace_id,
                generation_attempt_id=attempt.generation_attempt_id,
            ),
        )
        for attempt in read_generation_attempts(
            connection, workspace_id=workspace_id, generation_job_id=generation_job_id
        )
    )

    state = job.state
    if state == "running" and attempts and attempts[-1].state == "failed":
        state = "retryable"
    return EffectiveJob(job=job, state=state, attempts=attempts)


_QUEUE_ORDER_COLUMNS = (
    "workspace_id, conversation_id, order_json, version, created_at_us, updated_at_us"
)


def _queue_order_from_row(row: tuple[Any, ...]) -> QueueOrder:
    members = _verified_json_array(row[2], "chat queue order")
    if not all(isinstance(member, str) for member in members):
        raise StorageError("a stored chat queue order names a non-string submission identifier")
    return QueueOrder(
        workspace_id=row[0],
        conversation_id=row[1],
        order=tuple(members),
        version=row[3],
        created_at_us=row[4],
        updated_at_us=row[5],
    )


def read_queue_order(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> QueueOrder | None:
    """One conversation's queue-order projection, or None where none was written.

    None is not disorder: a conversation with no projection row reads in 0029's
    immutable `queue_sequence` order, which is what :func:`read_effective_queue_order`
    does with this answer.
    """
    row = connection.execute(
        f"SELECT {_QUEUE_ORDER_COLUMNS} FROM omnivia_chat_queued_submission_order "
        "WHERE workspace_id = ? AND conversation_id = ?",
        (workspace_id, conversation_id),
    ).fetchone()
    return None if row is None else _queue_order_from_row(row)


def read_effective_queue_order(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    limit: int = 100,
) -> tuple[QueuedSubmission, ...]:
    """A conversation's queued submissions in the order they will actually be taken.

    Projection order first, for every member the projection names that is still
    queued, then every other queued submission in `queue_sequence` order. Both
    halves are total and deterministic, and a workspace that has never written a
    projection row gets exactly 0029's creation order -- which is what every
    database upgraded from 0029 holds.

    A member the projection names that is no longer queued is simply absent: the
    read never resurrects a claimed or terminal submission, and never fabricates
    a row for an id the queue does not hold.

    `limit` slices the ordered result, not the rows the order is computed from --
    otherwise a projection that moved the tenth submission to the front would
    drop it from a five-row read. The row read is bounded regardless, by the same
    constant 0030 bounds a projection's membership with.
    """
    if not 1 <= limit <= MAX_QUEUE_ORDER_MEMBERS:
        raise StorageError(
            f"limit must be between 1 and {MAX_QUEUE_ORDER_MEMBERS}, got {limit}"
        )
    rows = connection.execute(
        f"SELECT {_QUEUED_SUBMISSION_COLUMNS} FROM omnivia_chat_queued_submissions "
        "WHERE workspace_id = ? AND conversation_id = ? AND state = 'queued' "
        "ORDER BY queue_sequence, queued_submission_id LIMIT ?",
        (workspace_id, conversation_id, MAX_QUEUE_ORDER_MEMBERS),
    ).fetchall()
    queued = {
        submission.queued_submission_id: submission
        for submission in (_queued_submission_from_row(row) for row in rows)
    }

    projection = read_queue_order(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    named = [] if projection is None else [i for i in projection.order if i in queued]
    already = set(named)
    ordered = (*named, *(i for i in queued if i not in already))
    return tuple(queued[identifier] for identifier in ordered[:limit])


def read_conversation_snapshot_inputs(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    device_id: str = "",
    branch_id: str | None = None,
    max_path_messages: int = 200,
) -> ConversationSnapshotInputs | None:
    """Every row an authoritative Conversation snapshot is composed from.

    Workspace-scoped throughout, and None for a conversation this workspace does
    not hold -- the same answer :func:`read_conversation` already gives, so this
    is no wider an existence oracle than the read it starts from.

    The branch is the caller's `branch_id`, else the actor's active branch, else
    the conversation's default branch. The path is that branch's head walked back
    through `parent_message_id` and reversed, bounded by `max_path_messages`; a
    chain longer than the bound is truncated at its oldest end, so the newest
    messages -- the ones a snapshot exists to show -- are always present.
    """
    if not 1 <= max_path_messages <= 200:
        raise StorageError(
            f"max_path_messages must be between 1 and 200, got {max_path_messages}"
        )
    conversation = read_conversation(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    if conversation is None:
        return None

    view_state = read_actor_view_state(
        connection,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        actor_id=actor_id,
        device_id=device_id,
    )
    selected = branch_id
    if selected is None and view_state is not None:
        selected = view_state.active_branch_id
    if selected is None:
        selected = conversation.default_branch_id

    branch = (
        None
        if selected is None
        else read_branch(connection, workspace_id=workspace_id, branch_id=selected)
    )
    if branch is not None and branch.conversation_id != conversation_id:
        branch = None

    path: list[Message] = []
    if branch is not None:
        message_id: str | None = branch.current_head_message_id
        seen: set[str] = set()
        while message_id is not None and len(path) < max_path_messages:
            if message_id in seen:
                break
            seen.add(message_id)
            row = connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM omnivia_chat_messages "
                "WHERE workspace_id = ? AND conversation_id = ? AND message_id = ?",
                (workspace_id, conversation_id, message_id),
            ).fetchone()
            if row is None:
                break
            message = _message_from_row(row)
            path.append(message)
            message_id = message.parent_message_id
    path.reverse()

    parts = {
        message.message_id: read_message_parts(
            connection, workspace_id=workspace_id, message_id=message.message_id
        )
        for message in path
    }
    job_ids = tuple(
        dict.fromkeys(
            message.generation_job_id
            for message in path
            if message.generation_job_id is not None
        )
    )

    return ConversationSnapshotInputs(
        conversation=conversation,
        branch=branch,
        view_state=view_state,
        path=tuple(path),
        parts_by_message_id=MappingProxyType(parts),
        generation_job_ids=job_ids,
    )
