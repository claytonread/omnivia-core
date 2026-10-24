"""One generation's durable lifecycle and its replay, over the W2-R Chat repository.

The first bounded slice of Core's M4 generation service: claim one queued
submission, open the durable job and attempt it runs under, fold already-normalized
F2a provider events into the durable generation event types migrations 0029 and 0034
admit, and replay that history after a cursor.

**Durable rows are the only authority.** Nothing here holds process-local state, so
a crash between any two calls loses nothing: the next call reads the queued
submission, the generation job and the generation event history back from SQLite
and continues from exactly where the rows say it was. That is also what makes the
idempotency real rather than best-effort -- a repeated claim of an
already-`submitted` queue row returns the job that row names, and a repeated
provider event with the same `providerEventId` returns the durable event already
written for it.

**No provider content is ever persisted in an event payload.** A provider event wire
reaches this module having already crossed F2a, and it is still never copied into a
durable event row: the payload written is built here from a closed allow-list of
contract vocabulary members and bounded sanitised fields (`providerEventType`,
`providerEventId`, `finishReason`, `errorCode`, `retryable`, `statusClass`,
`safeMessage`, `chunkOrdinal`), so a raw request or response body, a header, a URL, a
credential or an SDK object present on the input mapping has no path into storage even
if F2a were bypassed. The one piece of generated text this module does persist -- a
streamed delta -- goes to `omnivia_chat_generation_text_chunks` and only there; its
ordering event carries the ordinal that names it, never the text.

**What this lane deliberately does not do.** It does not touch the conversation
graph: no message, no message part, no branch head event, no `graph_revision` move.
A `succeeded` generation names the assistant message a *different* command
committed, so `result_message_id` is a parameter here rather than something this
module creates. It also opens no provider connection, adds no SDK dependency, and
registers no public wire operation -- the Chat contract is frozen and this is a
package-neutral service seam beneath it.

**Sequencing.** `omnivia_chat_generation_events.generation_event_sequence` is
contiguous from one per job and is 0029's own rule; the generation job's
`last_event_sequence` is the projection of it this module advances through the
job's existing compare-and-set token. A caller that believes the stream is at some
other position states its `expected_sequence` and is refused
(:class:`GenerationSequenceGap`) rather than silently writing over the gap.

**Directions (C6).** A Direction is taken in only where a provider iteration has
completed, never mid-stream, and that is two commits, never one. 0069 can see that the
successor's rows exist when it applies a Direction but not that they were committed
before, so the sequencing is this module's. Phase A (:func:`stage_direction_continuation`)
commits the iteration's identity-only `chat.generation.iteration_completed` event, the
successor's provider invocation identity, its Attempt and its request manifest, which binds
the Direction. Phase B (:func:`apply_direction_continuation`) is a separate transaction: the
Direction's `pending` -> `applied` move, the continuation record, the Direction's state
event, and the one step that moves both the Job row and the status projection to the
successor. Until Phase B commits the Generation is still on the prior Attempt and no
provider has been invoked for the successor. Every identity is derived
(:func:`continuation_identities`), so a crash after either commit, and a duplicate worker,
find the rows that were written rather than writing others, and any row that disagrees
with what a replay would write is a refusal, never a repair. A Direction that can no longer
be applied is closed honestly and without prose in the event stream
(:func:`close_unapplied_direction`).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core.chat_contract.v1.generated import (
    F2A_FINISH_REASONS,
    F2A_PROVIDER_ERROR_CODES,
    F2A_PROVIDER_EVENT_TYPES,
    RESNAPSHOT_REASONS,
)
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.chat_part_registry import (
    ACTIVITY_SERIES_PART_TYPE,
    ACTIVITY_SERIES_RESPONSE_SCHEMA_REF,
    CHART_PART_TYPE,
    CHART_RESPONSE_SCHEMA_REF,
    COMPARISON_MATRIX_PART_TYPE,
    COMPARISON_MATRIX_RESPONSE_SCHEMA_REF,
    DATA_TABLE_RESPONSE_SCHEMA_REF,
    DIAGRAM_FLOW_PART_TYPE,
    DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
    MATH_STEPS_PART_TYPE,
    MATH_STEPS_RESPONSE_SCHEMA_REF,
    METRIC_NUMBER_PART_TYPE,
    NUMBER_TICKER_RESPONSE_SCHEMA_REF,
    RICH_PROSE_PART_TYPE,
    SPEC_SHEET_PART_TYPE,
    SPEC_SHEET_RESPONSE_SCHEMA_REF,
    PartPayloadInvalid,
    validate_registered_payload,
)
from omnivia_core_runtime.storage import chat

__all__ = [
    "MAX_GENERATION_ATTEMPTS",
    "MAX_REASONING_SUMMARY_BYTES",
    "MAX_RESPONSE_TEXT_BYTES",
    "BoundaryUnavailable",
    "ClaimedGeneration",
    "ContinuationIdentities",
    "DirectionContinuation",
    "DirectionNotPending",
    "DuplicateProviderEvent",
    "GenerationConflict",
    "GenerationHeartbeat",
    "GenerationLifecycleError",
    "GenerationNotFound",
    "GenerationReplay",
    "GenerationSequenceGap",
    "GenerationTerminal",
    "StagedContinuation",
    "StaleGenerationVersion",
    "UnsupportedProviderEvent",
    "append_provider_generation_event",
    "append_provider_reasoning_summary",
    "append_provider_structured_output",
    "append_provider_text_delta",
    "apply_direction_continuation",
    "claim_fork_generation",
    "claim_queued_generation",
    "close_unapplied_direction",
    "completed_iteration",
    "continuation_identities",
    "generated_text_digest",
    "provider_finish_id",
    "recover_generation_attempt_lease",
    "renew_generation_lease",
    "replay_generation_events",
    "stage_direction_continuation",
]

#: How long a claim's lease runs from the moment it is taken or extended. 0029
#: requires a `running` job to carry a lease that has not expired at the moment of
#: the write, so every write below that leaves a job running re-states it.
DEFAULT_LEASE_US: Final = 60_000_000

#: 0030 bounds one durable chunk's `text_content` to 262144 UTF-8 bytes, and one
#: generation's whole response is held to the same bound: a stream that would push the
#: assistant message past what a message part may carry is refused while it is still
#: only a stream, rather than after the graph write fails.
MAX_RESPONSE_TEXT_BYTES: Final = 262_144

#: One Generation's complete collection of explicit display-safe reasoning
#: summaries is bounded independently of answer text. At four bytes per Unicode
#: code point this also admits the contract's 65536-character single-event bound.
MAX_REASONING_SUMMARY_BYTES: Final = 262_144

#: A provider structured object is held to the same durable Message-part ceiling
#: as generated text. Per-column and per-cell bounds are enforced independently by
#: the registered `rich.prose` table shape below.
MAX_STRUCTURED_OUTPUT_BYTES: Final = 262_144

_QUEUED: Final = "chat.generation.queued"
_STARTED: Final = "chat.generation.started"
_TEXT_APPENDED: Final = "chat.generation.text_appended"
_SUCCEEDED: Final = "chat.generation.succeeded"
_FAILED: Final = "chat.generation.failed"
_CANCELLED: Final = "chat.generation.cancelled"
_ITERATION_COMPLETED: Final = "chat.generation.iteration_completed"
_DIRECTION_STATE_CHANGED: Final = "chat.direction.state_changed"

#: 0029 bounds `attempt_number` to 1..1000, so a job that has opened its thousandth
#: Attempt has no successor to open.
MAX_GENERATION_ATTEMPTS: Final = 1000

#: 0029 and 0069 bound `generation_event_sequence` to 1..1000000.
_MAX_EVENT_SEQUENCE: Final = 1_000_000

_SHA256_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Why a Direction ended without being applied: the closed `(state, code)` pairs of
#: `common.schema.json#/$defs/DirectionTerminalReasonCode`, each with the one constant
#: sentence it reads as. A reason is chosen, never composed, so nothing a provider, an
#: exception or the Direction itself said can reach a stored reason.
_DIRECTION_REASONS: Final = {
    ("rejected", "stale_generation_version"): (
        "The generation changed after this direction was written, so it was not applied."
    ),
    ("rejected", "target_attempt_ended"): (
        "The generation attempt this direction targeted ended before it could be applied."
    ),
    ("rejected", "boundary_unavailable"): (
        "The generation reached no safe point at which this direction could be applied."
    ),
    ("rejected", "job_not_running"): (
        "The generation was not running when this direction could have been applied."
    ),
    ("rejected", "policy_refused"): "This direction could not be applied to this generation.",
    ("cancelled", "generation_stopped"): (
        "The generation was stopped before this direction was applied."
    ),
    ("cancelled", "job_terminal"): "The generation ended before this direction was applied.",
}

#: The durable event types after which no further event may be appended.
_TERMINAL_EVENT_TYPES: Final = frozenset({_SUCCEEDED, _FAILED, _CANCELLED})
_TERMINAL_JOB_STATES: Final = frozenset({"succeeded", "failed", "cancelled"})

#: The durable job state each terminal durable event moves the job to.
_TERMINAL_JOB_STATE_OF: Final = {
    _SUCCEEDED: "succeeded",
    _FAILED: "failed",
    _CANCELLED: "cancelled",
}

#: 0030 bounds one chunk's `chunk_ordinal` to 0..1000000. Held here so an ordinal
#: outside it is a refusal naming the rule rather than a CHECK naming a column.
_MAX_CHUNK_ORDINAL: Final = 1_000_000
_MAX_REASONING_SUMMARY_CHARACTERS: Final = 65_536

#: 0029 bounds `sanitized_error_detail` to 4096 bytes; the contract already bounds
#: `safeMessage` to 2048 characters. Held to the smaller of the two here.
_MAX_SAFE_MESSAGE: Final = 1024
_DURABLE_PROVIDER_EVENT_ID_MAX: Final = 128
_DURABLE_IDENTIFIER_INITIAL: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
_DURABLE_IDENTIFIER_CHARS: Final = _DURABLE_IDENTIFIER_INITIAL | frozenset("._:-")

#: `ErrorEvent.statusClass`'s closed set. Coarse and sanitised by construction --
#: never a raw upstream HTTP status or provider status string. It is the one F2a
#: vocabulary the generated metadata does not export as a named tuple, so it is
#: restated here against `provider.schema.json`'s own inline enum.
_STATUS_CLASSES: Final = frozenset(
    {"client_error", "server_error", "rate_limited", "timeout", "unknown"}
)


def _event_id(generation_job_id: str, sequence: int) -> str:
    candidate = f"{generation_job_id}.e{sequence}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"generation-event-{digest[:40]}"


def _now_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


class GenerationLifecycleError(Exception):
    """A refusal from this seam. Carries identifiers only, never provider content."""


class GenerationNotFound(GenerationLifecycleError):
    """This workspace holds no such queued submission, generation job or conversation."""


class GenerationConflict(GenerationLifecycleError):
    """The durable row is not in the state this call requires, or names another job."""


class GenerationSequenceGap(GenerationLifecycleError):
    """The stated next event sequence is not the one the durable job is at."""


class GenerationTerminal(GenerationLifecycleError):
    """The generation already ended; nothing may be appended after a terminal event."""


class DuplicateProviderEvent(GenerationLifecycleError):
    """A provider event id already durable here, carrying a different durable event."""


class StaleGenerationVersion(GenerationConflict):
    """The job's public CAS token is not the one the caller decided on."""


class BoundaryUnavailable(GenerationConflict):
    """A Direction cannot be taken in at this boundary, however valid it is.

    No Attempt or event position is left to open a successor in, or another Attempt
    already follows the one the Direction targets (a successor staged for a Direction
    that has since been replaced). Raised before anything is written, so the caller can
    close the Direction as unapplied and settle the Generation without it.
    """


class DirectionNotPending(GenerationConflict):
    """The Direction is no longer pending at the version the caller decided on.

    Superseded, rejected, cancelled, or applied by something other than the exact
    transition the caller is replaying. Never retried by this module: the caller re-reads
    which Direction, if any, is pending.
    """


class UnsupportedProviderEvent(GenerationLifecycleError):
    """A provider event wire this version cannot safely interpret.

    Raised rather than guessed: an unrecognised `eventType`, a missing or
    ill-typed required field, or a vocabulary member outside the contract's own
    closed set. The message names the field and the rule, never the value, so a
    refused document cannot leak through a diagnostic.
    """


@dataclass(frozen=True, slots=True)
class ClaimedGeneration:
    """One queued submission and the durable job and attempt it now runs under.

    The attempt is named rather than carried whole: `storage/chat.py` has no
    generation-attempt reader (the table is append-only and nothing before this
    lane needed to read it back), and `GenerationJob.current_attempt_id` is the
    same identity 0029 holds the job to.
    """

    submission: chat.QueuedSubmission
    job: chat.GenerationJob
    generation_attempt_id: str


@dataclass(frozen=True, slots=True)
class GenerationReplay:
    """The durable event suffix after a cursor, or the demand for a fresh snapshot.

    Never both: when `requires_resnapshot` is true `events` is empty and `reason` is
    a member of the contract's own closed `RESNAPSHOT_REASONS` vocabulary. A
    fabricated continuation is exactly what the Chat contract's resnapshot answer
    exists to prevent, so a cursor this module cannot place in this job's own
    history produces a reason rather than a guess.
    """

    events: tuple[chat.GenerationEvent, ...] = ()
    requires_resnapshot: bool = False
    reason: str | None = None


@dataclass(slots=True)
class GenerationHeartbeat:
    """Independent lease-renewal driver for one running Chat attempt.

    The object writes no transcript or provider events. A caller can tick it from
    a scheduler, timer thread or fake-clock test while the provider emits nothing;
    the renewal itself still goes through the same guarded compare-and-set lease
    path as event-time renewal. Ownership loss stops the heartbeat and is surfaced
    as a conflict, so stale owners do not silently revive themselves.
    """

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    generation_job_id: str
    generation_attempt_id: str
    lease_owner: str
    clock: Clock
    lease_duration_us: int = DEFAULT_LEASE_US
    lease_expires_at_us: int | None = None
    active: bool = False

    def start(self, lease_expires_at_us: int | None) -> None:
        self.lease_expires_at_us = lease_expires_at_us
        self.active = True

    def stop(self) -> None:
        self.active = False

    def tick(self) -> int | None:
        if not self.active:
            return self.lease_expires_at_us

        job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=self.generation_job_id,
        )
        if job is None:
            self.stop()
            raise GenerationNotFound(
                f"generation job {self.generation_job_id!r} is not in this workspace"
            )
        if job.state in _TERMINAL_JOB_STATES:
            self.stop()
            self.lease_expires_at_us = job.lease_expires_at_us
            return self.lease_expires_at_us
        if job.state != "running" or job.current_attempt_id != self.generation_attempt_id:
            self.stop()
            raise GenerationConflict(
                f"generation job {self.generation_job_id!r} is not running this attempt"
            )
        if job.lease_owner != self.lease_owner:
            self.stop()
            raise GenerationConflict(
                f"generation job {self.generation_job_id!r} is leased by another instance"
            )

        now_us = _now_us(self.clock)
        expires_at_us = job.lease_expires_at_us or self.lease_expires_at_us
        if expires_at_us is not None and now_us < expires_at_us - self.lease_duration_us // 2:
            self.lease_expires_at_us = expires_at_us
            return expires_at_us
        self.lease_expires_at_us = renew_generation_lease(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            generation_job_id=self.generation_job_id,
            lease_owner=self.lease_owner,
            now_us=now_us,
            lease_duration_us=self.lease_duration_us,
        )
        return self.lease_expires_at_us


# --- cursors ---------------------------------------------------------------------


def _cursor(generation_job_id: str, sequence: int) -> str:
    """The durable cursor for one event: its job's identity and its position.

    Both halves are needed. A cursor is presented by a reader that may have been
    reading another job -- or another workspace's job -- and a bare sequence number
    would be silently valid there.
    """
    return f"{generation_job_id}:{sequence:06d}"


def _cursor_position(cursor: str, generation_job_id: str) -> tuple[int | None, str | None]:
    """The position `cursor` names in this job, or the resnapshot reason it earns.

    `rpartition` rather than `split`: a generation job identifier may itself contain
    `:`, and only the final segment is the sequence. A well-formed cursor for
    another job is a reader on the wrong stream (`unauthorized_cursor`); one this
    version cannot parse at all is simply unknown.
    """
    job, separator, position = cursor.rpartition(":")
    if not separator or not position.isdigit():
        return None, "cursor_unknown_or_expired"
    if job != generation_job_id:
        return None, "unauthorized_cursor"
    return int(position), None


# --- provider event wires ---------------------------------------------------------


def _wire_str(wire: Mapping[str, Any], name: str) -> str:
    if name not in wire:
        raise UnsupportedProviderEvent(f"provider event field {name!r} is missing")
    value = wire[name]
    if not isinstance(value, str) or not value:
        raise UnsupportedProviderEvent(f"provider event field {name!r} is not a non-empty string")
    return value


def _optional_wire_str(wire: Mapping[str, Any], name: str) -> str | None:
    return None if name not in wire else _wire_str(wire, name)


def _is_durable_identifier(value: str) -> bool:
    """Whether a non-empty `value` is inside the grammar and length the tables hold ids to."""
    return (
        len(value) <= _DURABLE_PROVIDER_EVENT_ID_MAX
        and value[0] in _DURABLE_IDENTIFIER_INITIAL
        and all(character in _DURABLE_IDENTIFIER_CHARS for character in value)
    )


def _optional_provider_event_id(wire: Mapping[str, Any]) -> str | None:
    value = _optional_wire_str(wire, "providerEventId")
    if value is None:
        return None
    if not _is_durable_identifier(value):
        raise UnsupportedProviderEvent(
            "provider event field 'providerEventId' is outside durable identifier bounds"
        )
    return value


def _wire_vocabulary(
    wire: Mapping[str, Any], name: str, vocabulary: tuple[str, ...] | frozenset[str]
) -> str:
    value = _wire_str(wire, name)
    if value not in vocabulary:
        raise UnsupportedProviderEvent(
            f"provider event field {name!r} is outside this version's closed vocabulary"
        )
    return value


def _durable_event(wire: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The durable event type one provider event carries, and its sanitised payload.

    `None` for a provider event that carries no lifecycle transition -- every text,
    reasoning, tool and metadata event in the F2a vocabulary. Those are legal parts of
    a trace and are not lifecycle transitions. `text-delta` and the explicit,
    display-safe `reasoning-summary` are nonetheless durable through their dedicated
    append functions. Neither copies its content into a generation event payload.

    The returned payload is built field by field from the contract's own closed
    vocabularies and bounded sanitised strings. The input mapping is never copied.
    """
    if not isinstance(wire, Mapping):
        raise UnsupportedProviderEvent("a provider event wire must be a JSON object")
    event_type = _wire_vocabulary(wire, "eventType", F2A_PROVIDER_EVENT_TYPES)
    payload: dict[str, Any] = {"providerEventType": event_type}
    provider_event_id = _optional_provider_event_id(wire)
    if provider_event_id is not None:
        payload["providerEventId"] = provider_event_id

    if event_type == "stream-start":
        return _STARTED, payload

    if event_type == "finish":
        reason = _wire_vocabulary(wire, "finishReason", F2A_FINISH_REASONS)
        payload["finishReason"] = reason
        if reason == "cancelled":
            return _CANCELLED, payload
        if reason == "error":
            return _FAILED, payload
        return _SUCCEEDED, payload

    if event_type == "error":
        payload["errorCode"] = _wire_vocabulary(wire, "errorCode", F2A_PROVIDER_ERROR_CODES)
        retryable = wire.get("retryable")
        if not isinstance(retryable, bool):
            raise UnsupportedProviderEvent("provider event field 'retryable' is not a boolean")
        payload["retryable"] = retryable
        payload["statusClass"] = _wire_vocabulary(wire, "statusClass", _STATUS_CLASSES)
        payload["safeMessage"] = _wire_str(wire, "safeMessage").replace("\x00", "")[
            :_MAX_SAFE_MESSAGE
        ]
        return _FAILED, payload

    return None, payload


def _sanitized_failure(event_type: str, payload: Mapping[str, Any]) -> tuple[str, str | None]:
    """The job's `sanitized_error_code`/`sanitized_error_detail` for a terminal event.

    0029 requires a code on a `failed` or `cancelled` job and holds it to lowercase
    `[a-z0-9._-]`, which every member of the contract's own provider error-code and
    finish-reason vocabularies already satisfies.
    """
    if event_type == _CANCELLED:
        return "cancelled", None
    code = payload.get("errorCode") or payload.get("finishReason") or "unknown"
    detail = payload.get("safeMessage")
    return str(code), None if detail is None else str(detail)


# --- status projection ---------------------------------------------------------------


def _require_status(
    connection: sqlite3.Connection, *, workspace_id: str, job: chat.GenerationJob
) -> chat.GenerationJobStatusProjection:
    """The job's 0030 status projection, or a refusal before anything is written.

    The projection's `version` is the Job's C6 control token, so a job without one --
    or one whose projection names another conversation -- is refused rather than given
    a token late.
    """
    status = chat.read_generation_job_status_projection(
        connection, workspace_id=workspace_id, generation_job_id=job.generation_job_id
    )
    if status is None or status.conversation_id != job.conversation_id:
        raise GenerationConflict(
            f"generation job {job.generation_job_id!r} has no valid status projection"
        )
    return status


def _restate_status(
    writer: chat.ChatWriter,
    status: chat.GenerationJobStatusProjection,
    *,
    updated_at_us: int,
    **changes: Any,
) -> None:
    """Advance the projection by one, carrying over every fact `changes` does not set."""
    facts: dict[str, Any] = {
        "state": status.state,
        "current_attempt_id": status.current_attempt_id,
        "result_message_id": status.result_message_id,
        "sanitized_error_code": status.sanitized_error_code,
        "sanitized_error_detail": status.sanitized_error_detail,
        "finished_at_us": status.finished_at_us,
    }
    facts.update(changes)
    writer.update_generation_job_status_projection(
        generation_job_id=status.generation_job_id,
        expected_version=status.version,
        updated_at_us=updated_at_us,
        **facts,
    )


# --- claim -------------------------------------------------------------------------


def claim_queued_generation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    queued_submission_id: str,
    generation_job_id: str,
    generation_attempt_id: str,
    trigger_message_id: str,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> ClaimedGeneration:
    """Claim one queued submission, opening the job and attempt that will run it.

    Idempotent on the durable queue row rather than on anything remembered here: a
    submission already `submitted` against `generation_job_id` returns that job's
    rows without writing, and one submitted against a *different* job is a
    :class:`GenerationConflict` rather than a second job for the same submission.

    Everything the first claim writes commits together, in one fenced transaction:
    the queue row's `queued` -> `claimed` -> `submitted` walk (0029 admits no direct
    `queued` -> `submitted` edge), the generation job when the submitter did not
    already create it, its first attempt, the `chat.generation.queued` event at
    sequence one, and the job's move to `running` under a fresh lease. A crash anywhere
    inside it leaves the submission `queued` and this call repeatable.

    The conversation graph is not touched. `trigger_message_id` names a message some
    earlier command committed; this call reads the conversation only for the
    `graph_revision` 0029 requires the job to observe.
    """
    submission = chat.read_queued_submission(
        connection, workspace_id=workspace_id, queued_submission_id=queued_submission_id
    )
    if submission is None:
        raise GenerationNotFound(f"queued submission {queued_submission_id!r} is not in this workspace")

    if submission.state == "submitted":
        if submission.submitted_generation_job_id != generation_job_id:
            raise GenerationConflict(
                f"queued submission {queued_submission_id!r} was already submitted under another job"
            )
        claimed = _claimed(connection, workspace_id=workspace_id, submission=submission)
        # A replay only verifies, and only the exact authority the claim left: a
        # running projection on the durable submitted attempt, which is the attempt
        # the caller names. The claim already advanced the token to 2 and a repeat of
        # it must not move it again; anything else fails closed without a write.
        replayed = _require_status(connection, workspace_id=workspace_id, job=claimed.job)
        if (
            replayed.version < 2
            or replayed.state != "running"
            or claimed.job.state != "running"
            or replayed.current_attempt_id != claimed.generation_attempt_id
            or generation_attempt_id != claimed.generation_attempt_id
            or replayed.result_message_id is not None
            or replayed.sanitized_error_code is not None
            or replayed.sanitized_error_detail is not None
            or replayed.finished_at_us is not None
        ):
            raise GenerationConflict(
                f"generation job {generation_job_id!r} has no claimed status projection "
                f"for attempt {generation_attempt_id!r}"
            )
        return claimed

    if submission.state != "queued":
        raise GenerationConflict(
            f"queued submission {queued_submission_id!r} is {submission.state!r}, not claimable"
        )

    conversation = chat.read_conversation(
        connection, workspace_id=workspace_id, conversation_id=submission.conversation_id
    )
    if conversation is None:
        raise GenerationNotFound(
            f"conversation {submission.conversation_id!r} is not in this workspace"
        )

    existing_job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if existing_job is not None and (
        existing_job.conversation_id != submission.conversation_id
        or existing_job.branch_id != submission.branch_id
        or existing_job.trigger_message_id != trigger_message_id
        or existing_job.graph_revision_observed != conversation.graph_revision
        or existing_job.idempotency_key != submission.idempotency_key
        or existing_job.provider_connection_id != submission.provider_connection_id
        or existing_job.provider_model_id != submission.provider_model_id
        or existing_job.state != "queued"
        or existing_job.lease_epoch != 0
        or existing_job.current_attempt_id is not None
        or existing_job.last_event_sequence != 0
    ):
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is not the queued job for this submission"
        )
    if existing_job is not None:
        queued_status = _require_status(connection, workspace_id=workspace_id, job=existing_job)
        if (
            queued_status.state != "queued"
            or queued_status.version != 1
            or queued_status.current_attempt_id is not None
            or queued_status.result_message_id is not None
            or queued_status.sanitized_error_code is not None
            or queued_status.sanitized_error_detail is not None
            or queued_status.finished_at_us is not None
        ):
            raise GenerationConflict(
                f"generation job {generation_job_id!r} is not the queued job for this submission"
            )

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_queued_submission(
            queued_submission_id=queued_submission_id,
            expected_version=submission.version,
            state="claimed",
            updated_at_us=now_us,
            claimed_by=lease_owner,
            claim_epoch=1,
            claim_expires_at_us=expires_at_us,
        )
        if existing_job is None:
            writer.append_generation_job(
                generation_job_id=generation_job_id,
                conversation_id=submission.conversation_id,
                branch_id=submission.branch_id,
                trigger_message_id=trigger_message_id,
                graph_revision_observed=conversation.graph_revision,
                idempotency_key=submission.idempotency_key,
                schema_version=1,
                created_at_us=now_us,
                updated_at_us=now_us,
                provider_connection_id=submission.provider_connection_id,
                provider_model_id=submission.provider_model_id,
            )
            writer.insert_generation_job_status_projection(
                generation_job_id=generation_job_id,
                conversation_id=submission.conversation_id,
                state="queued",
                updated_at_us=now_us,
            )
        _open_first_attempt(
            writer,
            generation_job_id=generation_job_id,
            conversation_id=submission.conversation_id,
            branch_id=submission.branch_id,
            trigger_message_id=trigger_message_id,
            generation_attempt_id=generation_attempt_id,
            lease_owner=lease_owner,
            now_us=now_us,
            expires_at_us=expires_at_us,
            payload={"queuedSubmissionId": queued_submission_id, "attemptNumber": 1},
        )
        writer.update_queued_submission(
            queued_submission_id=queued_submission_id,
            expected_version=submission.version + 1,
            state="submitted",
            updated_at_us=now_us,
            submitted_message_id=trigger_message_id,
            submitted_generation_job_id=generation_job_id,
        )

    settled = chat.read_queued_submission(
        connection, workspace_id=workspace_id, queued_submission_id=queued_submission_id
    )
    if settled is None:  # pragma: no cover - the transaction above just committed it
        raise GenerationNotFound(f"queued submission {queued_submission_id!r} did not settle")
    return _claimed(connection, workspace_id=workspace_id, submission=settled)


def _open_first_attempt(
    writer: chat.ChatWriter,
    *,
    generation_job_id: str,
    conversation_id: str,
    branch_id: str,
    trigger_message_id: str,
    generation_attempt_id: str,
    lease_owner: str,
    now_us: int,
    expires_at_us: int,
    payload: Mapping[str, Any],
) -> None:
    """A fresh `queued` job's first attempt, its sequence-one event and its move to `running`.

    The writes both claims share, so a submission's and a fork's first attempt cannot
    drift: attempt number one, the `chat.generation.queued` event at sequence one, the job
    under lease epoch one, and the status projection advanced from version one to two.
    """
    writer.append_generation_attempt(
        generation_attempt_id=generation_attempt_id,
        conversation_id=conversation_id,
        generation_job_id=generation_job_id,
        attempt_number=1,
        state="running",
        schema_version=1,
        started_at_us=now_us,
    )
    writer.append_generation_event(
        event_id=_event_id(generation_job_id, 1),
        conversation_id=conversation_id,
        branch_id=branch_id,
        generation_job_id=generation_job_id,
        event_type=_QUEUED,
        generation_event_sequence=1,
        trigger_message_id=trigger_message_id,
        cursor=_cursor(generation_job_id, 1),
        payload=payload,
        occurred_at_us=now_us,
        schema_version=1,
    )
    writer.update_generation_job(
        generation_job_id=generation_job_id,
        expected_state="queued",
        expected_lease_epoch=0,
        state="running",
        lease_epoch=1,
        current_attempt_id=generation_attempt_id,
        lease_owner=lease_owner,
        lease_expires_at_us=expires_at_us,
        heartbeat_at_us=now_us,
        last_event_sequence=1,
        updated_at_us=now_us,
        started_at_us=now_us,
    )
    writer.update_generation_job_status_projection(
        generation_job_id=generation_job_id,
        expected_version=1,
        state="running",
        current_attempt_id=generation_attempt_id,
        updated_at_us=now_us,
    )


def claim_fork_generation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    trigger_message_id: str,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> chat.GenerationJob:
    """Claim a fork-created job (`RegenerateResponse`, `CommitMessageAmendment`) to run.

    Such a job has no queue row to walk, so the job and its status projection are the
    whole claim. It is claimable only while exactly as the fork committed it
    (:func:`chat.read_next_executable_fork_generation_job`, the predicate the startup
    drain reads with), and only for the trigger Message the job itself states: anything
    else, including a job already running or finished, is a :class:`GenerationConflict`
    before a byte is written. The first attempt, its event, and the job's and status
    projection's move to `running` under the lease commit together in one fenced
    transaction, with the identities :func:`claim_queued_generation` gives a submission's,
    so a crash inside it leaves the job `queued` and this call repeatable.
    """
    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    if (
        job.trigger_message_id != trigger_message_id
        or chat.read_next_executable_fork_generation_job(
            connection, workspace_id=workspace_id, generation_job_id=generation_job_id
        )
        is None
    ):
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is not an unclaimed fork job for this trigger"
        )
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        _open_first_attempt(
            writer,
            generation_job_id=generation_job_id,
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            trigger_message_id=trigger_message_id,
            generation_attempt_id=generation_attempt_id,
            lease_owner=lease_owner,
            now_us=now_us,
            expires_at_us=now_us + lease_duration_us,
            payload={"attemptNumber": 1},
        )
    claimed = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if claimed is None:  # pragma: no cover - the transaction above just committed it
        raise GenerationNotFound(f"generation job {generation_job_id!r} did not settle")
    return claimed


def _claimed(
    connection: sqlite3.Connection, *, workspace_id: str, submission: chat.QueuedSubmission
) -> ClaimedGeneration:
    job_id = submission.submitted_generation_job_id
    job = (
        None
        if job_id is None
        else chat.read_generation_job(
            connection, workspace_id=workspace_id, generation_job_id=job_id
        )
    )
    if job is None or job.current_attempt_id is None:
        raise GenerationNotFound(
            f"queued submission {submission.queued_submission_id!r} names no running generation job"
        )
    return ClaimedGeneration(
        submission=submission, job=job, generation_attempt_id=job.current_attempt_id
    )


# --- provider events ---------------------------------------------------------------


def append_provider_generation_event(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    provider_event: Mapping[str, Any],
    now_us: int,
    result_message_id: str | None = None,
    expected_sequence: int | None = None,
    lease_duration_us: int = DEFAULT_LEASE_US,
    lease_owner: str | None = None,
) -> chat.GenerationEvent | None:
    """Fold one already-normalized provider event into this job's durable history.

    Returns the durable event appended, the identical one already durable for this
    `providerEventId` (an idempotent redelivery), or `None` for a provider event that
    carries no generation-lifecycle transition -- a reasoning or tool delta is a legal
    part of a trace and is not a lifecycle transition, so nothing is written and
    nothing is refused. A `text-delta` belongs to :func:`append_provider_text_delta`,
    which writes its chunk and its ordering event as one fact; passing one here
    writes nothing.

    Refusals, in the order they are decided:

    1. a wire this version cannot safely interpret -- unknown `eventType`, missing
       or ill-typed field, vocabulary member outside the closed set --
       :class:`UnsupportedProviderEvent`;
    2. a `providerEventId` already durable on this `(job, attempt)` whose durable
       event would differ -- :class:`DuplicateProviderEvent`. An identical one
       returns the existing event instead, which is what makes a redelivered
       provider stream safe to replay;
    3. an append after a terminal durable event or onto a terminal job --
       :class:`GenerationTerminal`;
    4. an `expected_sequence` that is not the position the durable job is actually
       at -- :class:`GenerationSequenceGap`.

    A terminal event moves the job to its matching terminal state in the same
    transaction, with the sanitised code 0029 requires; a non-terminal one advances
    `last_event_sequence` and re-states the lease. Both go through the job's
    existing `(state, lease_epoch)` compare-and-set token, so a job another writer
    moved underneath this call refuses rather than overwrites.
    """
    event_type, payload = _durable_event(provider_event)

    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")

    if event_type is None:
        return None

    # 0029 ties the result message to exactly one durable event type: `succeeded`
    # requires it and every other type forbids it. A caller that has them the wrong
    # way round is refused here rather than at the CHECK, so the diagnostic names
    # the rule instead of the constraint.
    if (event_type == _SUCCEEDED) != (result_message_id is not None):
        raise GenerationConflict(
            "a result message belongs to a succeeded generation event and to no other"
        )

    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    provider_event_id = payload.get("providerEventId")
    existing = None
    if provider_event_id is not None:
        existing = _existing_provider_event(
            events,
            generation_attempt_id=generation_attempt_id,
            provider_event_id=str(provider_event_id),
        )
        if existing is not None and not (
            existing.event_type == event_type
            and dict(existing.payload) == payload
            and existing.result_message_id == result_message_id
        ):
            raise DuplicateProviderEvent(
                f"provider event {provider_event_id!r} is already durable on this attempt "
                "carrying a different durable event"
            )

    # Status authority is decided before an exact replay is answered: a redelivery is
    # served only to the caller whose running attempt still owns the job.
    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if status.state != "running" or status.current_attempt_id != generation_attempt_id:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is not running this attempt"
        )
    if existing is not None:
        return existing
    if (
        lease_owner is not None
        and job.state == "running"
        and job.current_attempt_id == generation_attempt_id
        and job.lease_owner != lease_owner
    ):
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is leased by another instance"
        )
    if any(
        event.generation_attempt_id == generation_attempt_id
        and event.event_type in _TERMINAL_EVENT_TYPES
        for event in events
    ):
        raise GenerationTerminal(f"generation attempt {generation_attempt_id!r} already ended")

    sequence = (
        max((event.generation_event_sequence for event in events), default=0) + 1
    )
    if expected_sequence is not None and expected_sequence != sequence:
        raise GenerationSequenceGap(
            f"generation job {generation_job_id!r} expects its next event at sequence "
            f"{sequence}, not {expected_sequence}"
        )

    terminal_state = _TERMINAL_JOB_STATE_OF.get(event_type)
    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_event(
            event_id=_event_id(generation_job_id, sequence),
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            event_type=event_type,
            generation_event_sequence=sequence,
            trigger_message_id=job.trigger_message_id,
            result_message_id=result_message_id,
            provider_event_id=None if provider_event_id is None else str(provider_event_id),
            cursor=_cursor(generation_job_id, sequence),
            payload=payload,
            occurred_at_us=now_us,
            schema_version=1,
        )
        if terminal_state is None:
            if job.state not in _TERMINAL_JOB_STATES:
                writer.update_generation_job(
                    generation_job_id=generation_job_id,
                    expected_state=job.state,
                    expected_lease_epoch=job.lease_epoch,
                    state=job.state,
                    lease_epoch=job.lease_epoch,
                    current_attempt_id=job.current_attempt_id,
                    lease_owner=job.lease_owner,
                    lease_expires_at_us=expires_at_us,
                    heartbeat_at_us=now_us,
                    last_event_sequence=sequence,
                    updated_at_us=now_us,
                    started_at_us=job.started_at_us,
                )
            _restate_status(writer, status, updated_at_us=now_us)
        else:
            retryable = bool(payload.get("retryable", False))
            projected_state = (
                "retryable"
                if terminal_state == "failed" and retryable
                else terminal_state
            )
            projected_finished_at_us = (
                None if projected_state == "retryable" else now_us
            )
            code, detail = (
                (None, None)
                if terminal_state == "succeeded"
                else _sanitized_failure(event_type, payload)
            )
            writer.append_generation_attempt_outcome(
                conversation_id=job.conversation_id,
                generation_job_id=generation_job_id,
                generation_attempt_id=generation_attempt_id,
                terminal_state=terminal_state,
                result_message_id=result_message_id if terminal_state == "succeeded" else None,
                provider_event_id=None if provider_event_id is None else str(provider_event_id),
                retryable=retryable,
                sanitized_error_code=code,
                sanitized_error_detail=detail,
                occurred_at_us=now_us,
            )
            if job.state not in _TERMINAL_JOB_STATES:
                writer.update_generation_job(
                    generation_job_id=generation_job_id,
                    expected_state=job.state,
                    expected_lease_epoch=job.lease_epoch,
                    state=terminal_state,
                    lease_epoch=job.lease_epoch,
                    current_attempt_id=job.current_attempt_id,
                    result_message_id=(
                        result_message_id if terminal_state == "succeeded" else None
                    ),
                    last_event_sequence=sequence,
                    sanitized_error_code=code,
                    sanitized_error_detail=detail,
                    updated_at_us=now_us,
                    started_at_us=job.started_at_us,
                    finished_at_us=now_us,
                )
            _restate_status(
                writer,
                status,
                updated_at_us=now_us,
                state=projected_state,
                result_message_id=result_message_id if terminal_state == "succeeded" else None,
                sanitized_error_code=code,
                sanitized_error_detail=detail,
                finished_at_us=projected_finished_at_us,
            )

    appended = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    return appended[sequence - 1]


def _existing_provider_event(
    events: tuple[chat.GenerationEvent, ...],
    *,
    generation_attempt_id: str,
    provider_event_id: str,
) -> chat.GenerationEvent | None:
    for event in events:
        if (
            event.generation_attempt_id == generation_attempt_id
            and event.provider_event_id == provider_event_id
        ):
            return event
    return None


def _text_chunk_hash(delta_bytes: bytes) -> str:
    """0030's `content_hash` for one exact delta: `sha256:` and 64 lowercase hex."""
    return "sha256:" + hashlib.sha256(delta_bytes).hexdigest()


def append_provider_text_delta(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    provider_event: Mapping[str, Any],
    chunk_ordinal: int,
    now_us: int,
    expected_sequence: int | None = None,
    lease_duration_us: int = DEFAULT_LEASE_US,
    prior_response_bytes: int = 0,
) -> chat.GenerationEvent:
    """Make one streamed text delta a single durable fact, or write nothing at all.

    A delta is three rows -- the chunk that holds the exact text, the
    `chat.generation.text_appended` event that orders it, and the job's advanced
    `last_event_sequence` under a re-stated lease -- and they are only true together.
    Written separately, a crash between them leaves a chunk no event orders or an
    event naming a chunk that is not there, and a replay cannot tell which. They go in
    one `chat_writer` transaction here for exactly that reason.

    The text lives in `omnivia_chat_generation_text_chunks` and nowhere else. The
    event's payload carries only what orders and identifies that chunk --
    `providerEventType`, the optional `providerEventId`, and `chunkOrdinal` -- so the
    generated text is never copied into `payload_json`.

    `chunk_ordinal` is the server's own zero-based position, not something the
    provider asserted, and must be the next one this attempt is at.

    Refusals, in the order they are decided, none of which leaves a row behind:

    1. a wire this version cannot safely interpret, or a `delta` that is not a
       non-empty, NUL-free, UTF-8-encodable string --
       :class:`UnsupportedProviderEvent`;
    2. a `providerEventId` already durable on this `(job, attempt)` whose event *or*
       chunk would differ -- :class:`DuplicateProviderEvent`. An identical redelivery
       returns the durable event and writes nothing, which is what makes a replayed
       provider stream safe;
    3. a terminal job or attempt -- :class:`GenerationTerminal` -- or a job not
       running this attempt, or an attempt with no durable
       `chat.generation.started` event -- :class:`GenerationConflict`;
    4. an ordinal outside 0029's own bound or not the next one, or an
       `expected_sequence` that is not the position the durable job is at --
       :class:`GenerationSequenceGap`;
    5. a response whose cumulative UTF-8 size would pass
       :data:`MAX_RESPONSE_TEXT_BYTES` -- :class:`GenerationConflict`. A continuation
       Attempt's response is the end of the one the earlier Attempts began, so the
       caller states what they already wrote in `prior_response_bytes` and the bound
       holds for the Generation, not for each Attempt alone.

    A redelivery is decided on both tables, never one: an event without its chunk, or
    a chunk without its event, is a torn write rather than a replay, and is refused.
    """
    _, payload = _durable_event(provider_event)
    if payload["providerEventType"] != "text-delta":
        raise UnsupportedProviderEvent("provider event field 'eventType' is not 'text-delta'")
    delta = provider_event.get("delta")
    # 0030 holds `text_content` to a non-empty, NUL-free, encodable string. Decided
    # here rather than left to the CHECK, for the reason the rest of this module
    # decides its own refusals: a constraint violation is an `IntegrityError` naming a
    # column, where a refusal names the field and the rule.
    if not isinstance(delta, str) or not delta or "\x00" in delta:
        raise UnsupportedProviderEvent(
            "provider event field 'delta' is not a non-empty NUL-free string"
        )
    delta_bytes: bytes | None = None
    try:
        delta_bytes = delta.encode("utf-8")
    except UnicodeEncodeError:
        # The `pass` is load-bearing: `UnicodeEncodeError` quotes the offending
        # substring and its position, and a refusal raised inside this handler would
        # carry it on `__context__` for anything that logs or serializes the error.
        # Ending the handler first means there is nothing to chain to.
        pass
    if delta_bytes is None:
        raise UnsupportedProviderEvent("provider event field 'delta' is not encodable as UTF-8")
    if (
        isinstance(chunk_ordinal, bool)
        or not isinstance(chunk_ordinal, int)
        or not 0 <= chunk_ordinal <= _MAX_CHUNK_ORDINAL
    ):
        raise GenerationSequenceGap(
            "a chunk ordinal is a zero-based integer position within the durable bound"
        )
    payload["chunkOrdinal"] = chunk_ordinal
    content_hash = _text_chunk_hash(delta_bytes)

    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")

    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    chunks = chat.read_generation_text_chunks(
        connection,
        workspace_id=workspace_id,
        generation_job_id=generation_job_id,
        generation_attempt_id=generation_attempt_id,
    )
    provider_event_id = payload.get("providerEventId")
    existing = None
    if provider_event_id is not None:
        existing = _existing_provider_event(
            events,
            generation_attempt_id=generation_attempt_id,
            provider_event_id=str(provider_event_id),
        )
        if existing is not None:
            durable = next(
                (
                    chunk
                    for chunk in chunks
                    if chunk.provider_event_id == str(provider_event_id)
                ),
                None,
            )
            if not (
                existing.event_type == _TEXT_APPENDED
                and dict(existing.payload) == payload
                and existing.result_message_id is None
                and durable is not None
                and durable.chunk_ordinal == chunk_ordinal
                and durable.text_content == delta
                and durable.content_hash == content_hash
            ):
                raise DuplicateProviderEvent(
                    f"provider event {provider_event_id!r} is already durable on this attempt "
                    "carrying a different durable text append"
                )

    # Status authority is decided before an exact replay is answered, as for events.
    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if status.state != "running" or status.current_attempt_id != generation_attempt_id:
        raise GenerationConflict(f"generation job {generation_job_id!r} is not running this attempt")
    if existing is not None:
        return existing
    if any(
        event.generation_attempt_id == generation_attempt_id
        and event.event_type in _TERMINAL_EVENT_TYPES
        for event in events
    ):
        raise GenerationTerminal(f"generation attempt {generation_attempt_id!r} already ended")
    # Text can only be appended to a stream that durably started. A `text_appended`
    # before `started` would be a history no replay can honestly order, and the durable
    # start is the only evidence this attempt -- not a previous one on the same job --
    # is the one the provider is streaming.
    if not any(
        event.generation_attempt_id == generation_attempt_id and event.event_type == _STARTED
        for event in events
    ):
        raise GenerationConflict(
            f"generation attempt {generation_attempt_id!r} has no durable started event"
        )

    if chunk_ordinal != len(chunks):
        raise GenerationSequenceGap(
            f"generation attempt {generation_attempt_id!r} expects its next text chunk at "
            f"ordinal {len(chunks)}, not {chunk_ordinal}"
        )
    sequence = max((event.generation_event_sequence for event in events), default=0) + 1
    if expected_sequence is not None and expected_sequence != sequence:
        raise GenerationSequenceGap(
            f"generation job {generation_job_id!r} expects its next event at sequence "
            f"{sequence}, not {expected_sequence}"
        )

    cumulative = (
        prior_response_bytes
        + sum(len(chunk.text_content.encode("utf-8")) for chunk in chunks)
        + len(delta_bytes)
    )
    if cumulative > MAX_RESPONSE_TEXT_BYTES:
        raise GenerationConflict(
            f"generation attempt {generation_attempt_id!r} would exceed the bounded "
            "response text size"
        )

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_text_chunk(
            conversation_id=job.conversation_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            chunk_ordinal=chunk_ordinal,
            provider_event_id=None if provider_event_id is None else str(provider_event_id),
            text_content=delta,
            content_hash=content_hash,
            occurred_at_us=now_us,
            schema_version=1,
        )
        writer.append_generation_event(
            event_id=_event_id(generation_job_id, sequence),
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            event_type=_TEXT_APPENDED,
            generation_event_sequence=sequence,
            trigger_message_id=job.trigger_message_id,
            result_message_id=None,
            provider_event_id=None if provider_event_id is None else str(provider_event_id),
            cursor=_cursor(generation_job_id, sequence),
            payload=payload,
            occurred_at_us=now_us,
            schema_version=1,
        )
        if job.state not in _TERMINAL_JOB_STATES:
            writer.update_generation_job(
                generation_job_id=generation_job_id,
                expected_state=job.state,
                expected_lease_epoch=job.lease_epoch,
                state=job.state,
                lease_epoch=job.lease_epoch,
                current_attempt_id=job.current_attempt_id,
                lease_owner=job.lease_owner,
                lease_expires_at_us=expires_at_us,
                heartbeat_at_us=now_us,
                last_event_sequence=sequence,
                updated_at_us=now_us,
                started_at_us=job.started_at_us,
            )
        _restate_status(writer, status, updated_at_us=now_us)

    appended = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    return appended[sequence - 1]


def append_provider_reasoning_summary(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    provider_event: Mapping[str, Any],
    summary_ordinal: int,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> chat.GenerationReasoningSummary:
    """Persist one explicit provider display summary before the stream advances.

    Only F2a's `reasoning-summary` event enters this path. Lifecycle reasoning
    events remain content-free, and no fallback field is inspected, so raw
    `reasoning_text` or chain-of-thought has no route into durable state.

    The summary row, lease restatement and status-version advance commit together.
    A provider event id makes exact redelivery idempotent; a conflicting reuse,
    malformed summary, ordinal gap, terminal Attempt or aggregate bound overflow is
    refused without a partial write.
    """
    _, payload = _durable_event(provider_event)
    if payload["providerEventType"] != "reasoning-summary":
        raise UnsupportedProviderEvent(
            "provider event field 'eventType' is not 'reasoning-summary'"
        )
    summary = provider_event.get("summary")
    if (
        not isinstance(summary, str)
        or not summary
        or "\x00" in summary
        or len(summary) > _MAX_REASONING_SUMMARY_CHARACTERS
    ):
        raise UnsupportedProviderEvent(
            "provider event field 'summary' is not a non-empty bounded NUL-free string"
        )
    summary_bytes: bytes | None = None
    try:
        summary_bytes = summary.encode("utf-8")
    except UnicodeEncodeError:
        pass
    if summary_bytes is None or len(summary_bytes) > MAX_REASONING_SUMMARY_BYTES:
        raise UnsupportedProviderEvent(
            "provider event field 'summary' is outside the UTF-8 byte bound"
        )
    part_id = _wire_str(provider_event, "partId")
    step_id = _wire_str(provider_event, "stepId")
    if not _is_durable_identifier(part_id) or not _is_durable_identifier(step_id):
        raise UnsupportedProviderEvent(
            "provider reasoning summary identity is outside durable identifier bounds"
        )
    if (
        isinstance(summary_ordinal, bool)
        or not isinstance(summary_ordinal, int)
        or not 0 <= summary_ordinal <= _MAX_CHUNK_ORDINAL
    ):
        raise GenerationSequenceGap(
            "a reasoning summary ordinal is a zero-based integer within the durable bound"
        )
    content_hash = _text_chunk_hash(summary_bytes)

    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    all_summaries = chat.read_generation_reasoning_summaries(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    attempt_summaries = tuple(
        item for item in all_summaries if item.generation_attempt_id == generation_attempt_id
    )
    provider_event_id = payload.get("providerEventId")
    existing_summary = None
    if provider_event_id is not None:
        existing_event = _existing_provider_event(
            events,
            generation_attempt_id=generation_attempt_id,
            provider_event_id=str(provider_event_id),
        )
        existing_summary = next(
            (
                item
                for item in all_summaries
                if item.provider_event_id == str(provider_event_id)
            ),
            None,
        )
        if existing_event is not None or (
            existing_summary is not None
            and not (
                existing_summary.generation_attempt_id == generation_attempt_id
                and existing_summary.summary_ordinal == summary_ordinal
                and existing_summary.provider_part_id == part_id
                and existing_summary.provider_step_id == step_id
                and existing_summary.summary_text == summary
                and existing_summary.content_hash == content_hash
            )
        ):
            raise DuplicateProviderEvent(
                f"provider event {provider_event_id!r} is already durable carrying a different fact"
            )

    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if status.state != "running" or status.current_attempt_id != generation_attempt_id:
        raise GenerationConflict(f"generation job {generation_job_id!r} is not running this attempt")
    if existing_summary is not None:
        return existing_summary
    if any(
        event.generation_attempt_id == generation_attempt_id
        and event.event_type in _TERMINAL_EVENT_TYPES
        for event in events
    ):
        raise GenerationTerminal(f"generation attempt {generation_attempt_id!r} already ended")
    if not any(
        event.generation_attempt_id == generation_attempt_id and event.event_type == _STARTED
        for event in events
    ):
        raise GenerationConflict(
            f"generation attempt {generation_attempt_id!r} has no durable started event"
        )
    if summary_ordinal != len(attempt_summaries):
        raise GenerationSequenceGap(
            f"generation attempt {generation_attempt_id!r} expects its next reasoning summary "
            f"at ordinal {len(attempt_summaries)}, not {summary_ordinal}"
        )
    combined_summary = "\n\n".join(
        [*(item.summary_text for item in all_summaries), summary]
    )
    if len(combined_summary.encode("utf-8")) > MAX_REASONING_SUMMARY_BYTES:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} would exceed the reasoning summary bound"
        )

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_reasoning_summary(
            conversation_id=job.conversation_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            summary_ordinal=summary_ordinal,
            provider_event_id=None if provider_event_id is None else str(provider_event_id),
            provider_part_id=part_id,
            provider_step_id=step_id,
            summary_text=summary,
            content_hash=content_hash,
            occurred_at_us=now_us,
            schema_version=1,
        )
        writer.update_generation_job(
            generation_job_id=generation_job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state=job.state,
            lease_epoch=job.lease_epoch,
            current_attempt_id=job.current_attempt_id,
            lease_owner=job.lease_owner,
            lease_expires_at_us=expires_at_us,
            heartbeat_at_us=now_us,
            last_event_sequence=job.last_event_sequence,
            updated_at_us=now_us,
            started_at_us=job.started_at_us,
        )
        _restate_status(writer, status, updated_at_us=now_us)

    appended = chat.read_generation_reasoning_summaries(
        connection,
        workspace_id=workspace_id,
        generation_job_id=generation_job_id,
        generation_attempt_id=generation_attempt_id,
    )
    return appended[summary_ordinal]


def append_provider_structured_output(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    provider_event: Mapping[str, Any],
    output_ordinal: int,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> chat.GenerationStructuredOutput:
    """Persist one allow-listed structured response before the stream advances.

    F2a deliberately treats `schemaRef` as opaque. Core does not: it accepts only
    schemas it can validate and materialise, and stores the canonical object in
    the append-only structured-output ledger. The provider event's `output` is
    never copied into a generation-event payload.

    A provider event id makes exact redelivery idempotent. Conflicting reuse,
    malformed or oversized output, a second output for the Attempt, or an output
    arriving outside a live started Attempt is refused without a partial write.
    """
    _, payload = _durable_event(provider_event)
    if payload["providerEventType"] != "structured-output":
        raise UnsupportedProviderEvent(
            "provider event field 'eventType' is not 'structured-output'"
        )
    schema_ref = _wire_str(provider_event, "schemaRef")
    if schema_ref not in {
        ACTIVITY_SERIES_RESPONSE_SCHEMA_REF,
        CHART_RESPONSE_SCHEMA_REF,
        COMPARISON_MATRIX_RESPONSE_SCHEMA_REF,
        DATA_TABLE_RESPONSE_SCHEMA_REF,
        DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
        MATH_STEPS_RESPONSE_SCHEMA_REF,
        NUMBER_TICKER_RESPONSE_SCHEMA_REF,
        SPEC_SHEET_RESPONSE_SCHEMA_REF,
    }:
        raise UnsupportedProviderEvent(
            "provider event field 'schemaRef' is not an allow-listed structured schema"
        )
    output = provider_event.get("output")
    if not isinstance(output, Mapping):
        raise UnsupportedProviderEvent("provider event field 'output' is not an object")
    normalized_output = dict(output)
    # Strict provider schemas require every declared property to be present. The
    # public metric contract still makes ``unit`` optional, so Platform represents
    # absence as JSON null at that provider-only edge. Remove exactly that sentinel
    # before canonicalisation, validation and persistence; a null anywhere else
    # remains ordinary input and is refused by the registered payload validator.
    if (
        schema_ref == NUMBER_TICKER_RESPONSE_SCHEMA_REF
        and "unit" in normalized_output
        and normalized_output["unit"] is None
    ):
        del normalized_output["unit"]
    if schema_ref == CHART_RESPONSE_SCHEMA_REF:
        for optional_field in ("detail", "unit"):
            if normalized_output.get(optional_field) is None:
                normalized_output.pop(optional_field, None)
    if (
        schema_ref == ACTIVITY_SERIES_RESPONSE_SCHEMA_REF
        and "unit" in normalized_output
        and normalized_output["unit"] is None
    ):
        del normalized_output["unit"]
    if schema_ref == MATH_STEPS_RESPONSE_SCHEMA_REF:
        if normalized_output.get("label") is None:
            normalized_output.pop("label", None)
        steps = normalized_output.get("steps")
        if isinstance(steps, list):
            normalized_output["steps"] = [
                {
                    key: value
                    for key, value in candidate.items()
                    if key != "note" or value is not None
                }
                if isinstance(candidate, Mapping)
                else candidate
                for candidate in steps
            ]
    if (
        schema_ref == SPEC_SHEET_RESPONSE_SCHEMA_REF
        and normalized_output.get("detail") is None
    ):
        normalized_output.pop("detail", None)
    if (
        schema_ref == COMPARISON_MATRIX_RESPONSE_SCHEMA_REF
        and normalized_output.get("detail") is None
    ):
        normalized_output.pop("detail", None)
    try:
        output_document = to_canonical_json(normalized_output).encode("utf-8")
    except ChatContractDecodeError:
        raise UnsupportedProviderEvent(
            "provider event field 'output' is not canonicalisable"
        ) from None
    if len(output_document) > MAX_STRUCTURED_OUTPUT_BYTES:
        raise UnsupportedProviderEvent(
            "provider event field 'output' exceeds the structured-output byte bound"
        )

    if schema_ref == ACTIVITY_SERIES_RESPONSE_SCHEMA_REF:
        part_type = ACTIVITY_SERIES_PART_TYPE
        part_payload = normalized_output
        schema_label = "activity-series"
    elif schema_ref == DATA_TABLE_RESPONSE_SCHEMA_REF:
        part_type = RICH_PROSE_PART_TYPE
        part_payload = {"blocks": [{**normalized_output, "kind": "table"}]}
        schema_label = "data-table"
    elif schema_ref == NUMBER_TICKER_RESPONSE_SCHEMA_REF:
        part_type = METRIC_NUMBER_PART_TYPE
        part_payload = normalized_output
        schema_label = "number-ticker"
    elif schema_ref == CHART_RESPONSE_SCHEMA_REF:
        part_type = CHART_PART_TYPE
        part_payload = normalized_output
        schema_label = "chart"
    elif schema_ref == DIAGRAM_FLOW_RESPONSE_SCHEMA_REF:
        part_type = DIAGRAM_FLOW_PART_TYPE
        part_payload = normalized_output
        schema_label = "diagram-flow"
    elif schema_ref == MATH_STEPS_RESPONSE_SCHEMA_REF:
        part_type = MATH_STEPS_PART_TYPE
        part_payload = normalized_output
        schema_label = "math-steps"
    elif schema_ref == COMPARISON_MATRIX_RESPONSE_SCHEMA_REF:
        part_type = COMPARISON_MATRIX_PART_TYPE
        part_payload = normalized_output
        schema_label = "comparison-matrix"
    else:
        part_type = SPEC_SHEET_PART_TYPE
        part_payload = normalized_output
        schema_label = "spec-sheet"
    try:
        validate_registered_payload(part_type, part_payload)
    except PartPayloadInvalid:
        raise UnsupportedProviderEvent(
            f"provider event field 'output' does not match the {schema_label} schema"
        ) from None

    if output_ordinal != 0:
        raise GenerationSequenceGap(
            "a structured output must be the Attempt's sole output at ordinal zero"
        )

    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    all_outputs = chat.read_generation_structured_outputs(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    attempt_outputs = tuple(
        item for item in all_outputs if item.generation_attempt_id == generation_attempt_id
    )
    provider_event_id = payload.get("providerEventId")
    existing_output = None
    if provider_event_id is not None:
        existing_event = _existing_provider_event(
            events,
            generation_attempt_id=generation_attempt_id,
            provider_event_id=str(provider_event_id),
        )
        existing_output = next(
            (
                item
                for item in all_outputs
                if item.provider_event_id == str(provider_event_id)
            ),
            None,
        )
        if existing_event is not None or (
            existing_output is not None
            and not (
                existing_output.generation_attempt_id == generation_attempt_id
                and existing_output.output_ordinal == output_ordinal
                and existing_output.schema_ref == schema_ref
                and dict(existing_output.output) == normalized_output
            )
        ):
            raise DuplicateProviderEvent(
                f"provider event {provider_event_id!r} is already durable carrying a different fact"
            )

    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if status.state != "running" or status.current_attempt_id != generation_attempt_id:
        raise GenerationConflict(f"generation job {generation_job_id!r} is not running this attempt")
    if existing_output is not None:
        return existing_output
    if any(
        event.generation_attempt_id == generation_attempt_id
        and event.event_type in _TERMINAL_EVENT_TYPES
        for event in events
    ):
        raise GenerationTerminal(f"generation attempt {generation_attempt_id!r} already ended")
    if not any(
        event.generation_attempt_id == generation_attempt_id and event.event_type == _STARTED
        for event in events
    ):
        raise GenerationConflict(
            f"generation attempt {generation_attempt_id!r} has no durable started event"
        )
    if attempt_outputs:
        raise GenerationSequenceGap(
            f"generation attempt {generation_attempt_id!r} already has its structured output"
        )

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_generation_structured_output(
            conversation_id=job.conversation_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            output_ordinal=output_ordinal,
            provider_event_id=None if provider_event_id is None else str(provider_event_id),
            schema_ref=schema_ref,
            output=normalized_output,
            occurred_at_us=now_us,
            schema_version=1,
        )
        writer.update_generation_job(
            generation_job_id=generation_job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state=job.state,
            lease_epoch=job.lease_epoch,
            current_attempt_id=job.current_attempt_id,
            lease_owner=job.lease_owner,
            lease_expires_at_us=expires_at_us,
            heartbeat_at_us=now_us,
            last_event_sequence=job.last_event_sequence,
            updated_at_us=now_us,
            started_at_us=job.started_at_us,
        )
        _restate_status(writer, status, updated_at_us=now_us)

    appended = chat.read_generation_structured_outputs(
        connection,
        workspace_id=workspace_id,
        generation_job_id=generation_job_id,
        generation_attempt_id=generation_attempt_id,
    )
    return appended[0]


# --- C6: a Direction taken in at an iteration boundary -------------------------------


def _stable_id(kind: str, source: str) -> str:
    return f"{kind}-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:40]}"


def _timestamp(moment_us: int) -> str:
    """RFC 3339 UTC for a microsecond instant, by integer arithmetic so nothing rounds."""
    moment = datetime.fromtimestamp(moment_us // 1_000_000, tz=UTC)
    return moment.replace(microsecond=moment_us % 1_000_000).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ContinuationIdentities:
    """Every identity one continuation writes, derived and never allocated.

    Nothing in them is random or time-dependent, so a replay and a crash recovery derive
    the identities the first run wrote. They are scoped to the job, the Direction and the
    iteration the Direction takes effect at, and are disjoint from the Attempt a job
    starts with, whose identity is derived from the job alone.
    """

    continuation_id: str
    generation_attempt_id: str
    provider_invocation_id: str
    request_manifest_id: str


def continuation_identities(
    *, generation_job_id: str, direction_id: str, iteration: int
) -> ContinuationIdentities:
    """The identities of the continuation that applies `direction_id` at `iteration`.

    `iteration` is the zero-based iteration the Direction takes effect at, the completed
    iteration plus one. The provider invocation and manifest follow the recipe every other
    Attempt's use (`provider-inv-` and `request-manifest-` over `job:attempt`), so a
    continuation Attempt reads as any other Attempt to everything that reads them.
    """
    scope = f"{generation_job_id}:{direction_id}:{iteration}"
    attempt_id = _stable_id("genatt", scope)
    return ContinuationIdentities(
        continuation_id=_stable_id("continuation", scope),
        generation_attempt_id=attempt_id,
        provider_invocation_id=_stable_id("provider-inv", f"{generation_job_id}:{attempt_id}"),
        request_manifest_id=_stable_id("request-manifest", f"{generation_job_id}:{attempt_id}"),
    )


@dataclass(frozen=True, slots=True)
class StagedContinuation:
    """What phase A left durable, as the rows now read.

    `replayed` is true when every one of them was already durable and this call only
    verified it, so nothing was written and no version moved.
    """

    iteration_event: chat.GenerationEvent
    invocation: chat.ProviderInvocation
    attempt: chat.GenerationAttempt
    manifest: chat.RequestManifest
    replayed: bool


@dataclass(frozen=True, slots=True)
class DirectionContinuation:
    """What phase B left behind, as the rows now read.

    `replayed` is true when the transition had already been written and this call only
    verified it.
    """

    direction: chat.Direction
    continuation: chat.GenerationContinuation
    attempt: chat.GenerationAttempt
    manifest: chat.RequestManifest
    replayed: bool


def completed_iteration(directions: Sequence[chat.Direction], generation_attempt_id: str) -> int:
    """The zero-based iteration `generation_attempt_id` runs, and so the one it completes.

    A Job's iterations are counted along its continuation chain. An Attempt that no
    Direction opened runs iteration 0; one a Direction opened runs the iteration that
    Direction took effect at, which is how `applied_iteration` is defined in 0069.
    """
    for direction in directions:
        if (
            direction.state == "applied"
            and direction.applied_attempt_id == generation_attempt_id
            and direction.applied_iteration is not None
        ):
            return direction.applied_iteration
    return 0


def generated_text_digest(text: str) -> str:
    """`sha256:` and the lower-case hex SHA-256 of the exact UTF-8 bytes of `text`.

    Of the text alone: an Attempt's ordered durable chunks concatenated, with no JSON
    wrapper, separator or length around them.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def provider_finish_id(provider_event: Mapping[str, Any]) -> str:
    """The identity a provider `finish` carries into the durable iteration record.

    The provider's own `providerEventId` when it sent one this database can hold: inside
    the durable identifier grammar and length. It is optional on the wire, and a provider
    may send one too long or too odd to store, so otherwise the identity is derived from
    the transport Attempt the event belongs to (`ProviderEvent.attemptId`, a
    `ProviderAttemptId`, which is not the Chat Generation Attempt a request's `attemptId`
    names) and the event's ordinal. A replay of the same stream derives it again.

    An id of the wrong type is refused as it is everywhere else
    (:class:`UnsupportedProviderEvent`); one that merely cannot be stored is not a reason
    to refuse an otherwise valid finish.
    """
    if not isinstance(provider_event, Mapping):
        raise UnsupportedProviderEvent("a provider event wire must be a JSON object")
    given = _optional_wire_str(provider_event, "providerEventId")
    if given is not None and _is_durable_identifier(given):
        return given
    transport_attempt = _wire_str(provider_event, "attemptId")
    ordinal = provider_event.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise UnsupportedProviderEvent("provider event field 'ordinal' is not a non-negative integer")
    return _stable_id("finish", f"{transport_attempt}:{ordinal}")


def _record_event_on_job(
    writer: chat.ChatWriter,
    job: chat.GenerationJob,
    status: chat.GenerationJobStatusProjection,
    *,
    sequence: int,
    now_us: int,
    lease_duration_us: int,
    current_attempt_id: str | None = None,
) -> None:
    """What an appended event leaves on a job that is still alive.

    A running job row keeps the event's sequence under a re-stated lease; a row that has
    already ended keeps nothing. The public token advances once, and only while the
    projection is `running`: a finished Job did not change because one of its Directions
    was closed. `current_attempt_id` moves both to the successor Attempt in the same
    write that records the event.
    """
    if job.state not in _TERMINAL_JOB_STATES:
        writer.update_generation_job(
            generation_job_id=job.generation_job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state=job.state,
            lease_epoch=job.lease_epoch,
            current_attempt_id=current_attempt_id or job.current_attempt_id,
            lease_owner=job.lease_owner,
            lease_expires_at_us=now_us + lease_duration_us,
            heartbeat_at_us=now_us,
            last_event_sequence=sequence,
            updated_at_us=now_us,
            started_at_us=job.started_at_us,
        )
    if status.state == "running":
        changes: dict[str, Any] = {}
        if current_attempt_id is not None:
            changes["current_attempt_id"] = current_attempt_id
        _restate_status(writer, status, updated_at_us=now_us, **changes)


def _manifest_route(manifest_body: Mapping[str, Any]) -> tuple[str, str]:
    route = manifest_body.get("route")
    if not isinstance(route, Mapping):
        raise GenerationConflict("the request manifest names no provider route")
    connection_id = route.get("connectionId")
    model_id = route.get("modelId")
    if (
        not isinstance(connection_id, str)
        or not isinstance(model_id, str)
        or route.get("operation") != "language.stream"
    ):
        raise GenerationConflict("the request manifest names no provider route")
    return connection_id, model_id


def _require_manifest_binding(
    manifest_body: Mapping[str, Any],
    *,
    workspace_id: str,
    job: chat.GenerationJob,
    direction: chat.Direction,
    identities: ContinuationIdentities,
) -> None:
    """The manifest is the successor's and binds exactly this Direction, or nothing is written.

    0069 refuses an application whose manifest does not carry `direction` with exactly
    three keys; this is the same rule stated before the first write, so the refusal names
    the disagreement rather than a trigger. `directionVersion` is the pending version the
    manifest was built from, always 1.
    """
    expected: dict[str, Any] = {
        "workspaceId": workspace_id,
        "conversationId": job.conversation_id,
        "branchId": job.branch_id,
        "generationJobId": job.generation_job_id,
        "generationAttemptId": identities.generation_attempt_id,
        "triggerMessageId": job.trigger_message_id,
        "providerInvocationId": identities.provider_invocation_id,
        "requestManifestId": identities.request_manifest_id,
    }
    binding = {
        "contentDigest": direction.content_digest,
        "directionId": direction.direction_id,
        "directionVersion": 1,
    }
    if (
        any(manifest_body.get(key) != value for key, value in expected.items())
        or not isinstance(manifest_body.get("idempotencyKey"), str)
        or manifest_body.get("direction") != binding
    ):
        raise GenerationConflict(
            "the request manifest does not belong to this continuation of "
            f"direction {direction.direction_id!r}"
        )


@dataclass(frozen=True, slots=True)
class _Boundary:
    """One iteration boundary as the rows read it, the Direction proved to belong to it."""

    job: chat.GenerationJob
    direction: chat.Direction
    prior: chat.GenerationAttempt
    attempts: tuple[chat.GenerationAttempt, ...]
    events: tuple[chat.GenerationEvent, ...]
    closing: tuple[chat.GenerationEvent, ...]
    iteration: int
    route: tuple[str, str]


def _read_boundary(
    writer: chat.ChatWriter,
    *,
    generation_job_id: str,
    prior_attempt_id: str,
    direction_id: str,
    actor_id: str,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
) -> _Boundary:
    """Read, inside the open transaction, everything either phase proves before it writes.

    The Direction is this job's, aimed at `prior_attempt_id` and the caller's `actor_id`
    (its reader recomputes the content digest, so a tampered one fails here); the
    manifest names the successor's own identities and binds the Direction, under a
    provider idempotency key that is neither the Job's nor the prior Attempt's; and the
    identities are those derived for the iteration after the one `prior_attempt_id` runs.
    """
    connection = writer.connection
    workspace_id = writer.workspace_id
    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    direction = chat.read_direction(connection, workspace_id=workspace_id, direction_id=direction_id)
    if direction is None:
        raise GenerationNotFound(f"direction {direction_id!r} is not in this workspace")
    if (
        direction.generation_job_id != job.generation_job_id
        or direction.conversation_id != job.conversation_id
        or direction.branch_id != job.branch_id
        or direction.target_attempt_id != prior_attempt_id
        or direction.actor_id != actor_id
        or direction.requested_boundary != "earliest_safe"
    ):
        raise GenerationConflict(
            f"direction {direction_id!r} does not belong to this generation, attempt and actor"
        )
    _require_manifest_binding(
        manifest_body,
        workspace_id=workspace_id,
        job=job,
        direction=direction,
        identities=identities,
    )
    directions = chat.read_directions_for_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    iteration = completed_iteration(directions, prior_attempt_id)
    if identities != continuation_identities(
        generation_job_id=generation_job_id, direction_id=direction_id, iteration=iteration + 1
    ):
        raise GenerationConflict(
            f"the identities given are not those of the continuation of direction {direction_id!r}"
        )
    prior = chat.read_generation_attempt(
        connection, workspace_id=workspace_id, generation_attempt_id=prior_attempt_id
    )
    if prior is None or prior.generation_job_id != generation_job_id:
        raise GenerationConflict(
            f"generation attempt {prior_attempt_id!r} is not an attempt of this generation"
        )
    # A provider deduplicates on this key, so a successor that carried the Job's or the
    # prior Attempt's would be answered with the prior response.
    prior_manifest = chat.read_request_manifest(
        connection, workspace_id=workspace_id, generation_attempt_id=prior_attempt_id
    )
    reused = {job.idempotency_key}
    if prior_manifest is not None:
        reused.add(prior_manifest.idempotency_key)
    if manifest_body["idempotencyKey"] in reused:
        raise GenerationConflict(
            f"the continuation of direction {direction_id!r} does not have a provider "
            "idempotency key of its own"
        )
    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    return _Boundary(
        job=job,
        direction=direction,
        prior=prior,
        attempts=chat.read_generation_attempts(
            connection, workspace_id=workspace_id, generation_job_id=generation_job_id
        ),
        events=events,
        closing=tuple(
            event
            for event in events
            if event.generation_attempt_id == prior_attempt_id
            and event.event_type == _ITERATION_COMPLETED
        ),
        iteration=iteration,
        route=_manifest_route(manifest_body),
    )


def _require_running_on(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    job: chat.GenerationJob,
    attempt_id: str,
    lease_owner: str,
) -> chat.GenerationJobStatusProjection:
    """Both authorities say the caller's lease is running `attempt_id`, or nothing proceeds.

    The foundation Job row and the public status projection are two records of one
    fact, and a continuation moves both, so it starts only from a state where they agree.
    """
    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES or job.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {job.generation_job_id!r} already ended")
    if (
        status.state != "running"
        or status.current_attempt_id != attempt_id
        or job.state != "running"
        or job.current_attempt_id != attempt_id
    ):
        raise GenerationConflict(
            f"generation job {job.generation_job_id!r} is not running this attempt"
        )
    if job.lease_owner != lease_owner:
        raise GenerationConflict(
            f"generation job {job.generation_job_id!r} is leased by another instance"
        )
    return status


def _durable_output_digest(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    generation_job_id: str,
    generation_attempt_id: str,
) -> str:
    return generated_text_digest(
        "".join(
            chunk.text_content
            for chunk in chat.read_generation_text_chunks(
                connection,
                workspace_id=workspace_id,
                generation_job_id=generation_job_id,
                generation_attempt_id=generation_attempt_id,
            )
        )
    )


def _read_staged(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    boundary: _Boundary,
    identities: ContinuationIdentities,
) -> tuple[chat.ProviderInvocation | None, chat.GenerationAttempt | None, chat.RequestManifest | None]:
    """The successor's invocation identity, Attempt and manifest, whichever exist."""
    return (
        chat.read_provider_invocation(
            connection, workspace_id=workspace_id, invocation_id=identities.provider_invocation_id
        ),
        next(
            (
                attempt
                for attempt in boundary.attempts
                if attempt.generation_attempt_id == identities.generation_attempt_id
            ),
            None,
        ),
        chat.read_request_manifest(
            connection,
            workspace_id=workspace_id,
            generation_attempt_id=identities.generation_attempt_id,
        ),
    )


def _staged_mismatch(
    boundary: _Boundary,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    invocation: chat.ProviderInvocation | None,
    attempt: chat.GenerationAttempt | None,
    manifest: chat.RequestManifest | None,
) -> bool:
    """True when a staged row that exists is not the one a replay would write.

    Compared on every identity and, for the manifest, on its canonical bytes. A row that
    is absent is not a mismatch here; whether it must exist is for the caller.
    """
    job = boundary.job
    connection_id, model_id = boundary.route
    if invocation is not None and (
        invocation.conversation_id,
        invocation.job_id,
        invocation.generation_attempt_id,
        invocation.operation,
        invocation.configured_connection_id,
        invocation.configured_model_id,
    ) != (
        job.conversation_id,
        job.generation_job_id,
        identities.generation_attempt_id,
        "language.stream",
        connection_id,
        model_id,
    ):
        return True
    if attempt is not None and (
        attempt.conversation_id,
        attempt.generation_job_id,
        attempt.attempt_number,
        attempt.retry_of_attempt_id,
        attempt.state,
        attempt.provider_invocation_id,
        attempt.ended_at_us,
    ) != (
        job.conversation_id,
        job.generation_job_id,
        boundary.prior.attempt_number + 1,
        boundary.prior.generation_attempt_id,
        "running",
        identities.provider_invocation_id,
        None,
    ):
        return True
    return manifest is not None and (
        (
            manifest.conversation_id,
            manifest.branch_id,
            manifest.generation_job_id,
            manifest.trigger_message_id,
            manifest.provider_invocation_id,
            manifest.request_manifest_id,
            manifest.idempotency_key,
        )
        != (
            job.conversation_id,
            job.branch_id,
            job.generation_job_id,
            job.trigger_message_id,
            identities.provider_invocation_id,
            identities.request_manifest_id,
            manifest_body["idempotencyKey"],
        )
        or to_canonical_json(dict(manifest.manifest_body)) != to_canonical_json(dict(manifest_body))
    )


def _require_applied(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    boundary: _Boundary,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    iteration_event: chat.GenerationEvent,
) -> tuple[chat.ProviderInvocation, chat.GenerationAttempt, chat.RequestManifest]:
    """The Direction was applied by exactly this continuation and every row says so."""
    direction = boundary.direction
    applied = (
        direction.version == 2
        and direction.effective_boundary == "next_iteration"
        and direction.applied_attempt_id == identities.generation_attempt_id
        and direction.applied_provider_invocation_id == identities.provider_invocation_id
        and direction.applied_request_manifest_id == identities.request_manifest_id
        and direction.applied_iteration == boundary.iteration + 1
    )
    if not applied:
        raise DirectionNotPending(
            f"direction {direction.direction_id!r} was applied by another continuation"
        )
    continuation = chat.read_generation_continuation(
        connection, workspace_id=workspace_id, continuation_id=identities.continuation_id
    )
    invocation, attempt, manifest = _read_staged(
        connection, workspace_id=workspace_id, boundary=boundary, identities=identities
    )
    if (
        continuation is None
        or continuation.prior_attempt_id != boundary.prior.generation_attempt_id
        or continuation.next_attempt_id != identities.generation_attempt_id
        or continuation.direction_id != direction.direction_id
        or continuation.direction_version != 2
        or continuation.provider_finish_id != iteration_event.provider_event_id
        or continuation.prior_output_digest != iteration_event.payload.get("priorOutputDigest")
        or invocation is None
        or attempt is None
        or manifest is None
        or _staged_mismatch(boundary, identities, manifest_body, invocation, attempt, manifest)
    ):
        raise GenerationConflict(
            f"direction {direction.direction_id!r} is applied, but its continuation rows "
            "do not match the ones this replay would write"
        )
    return invocation, attempt, manifest


def stage_direction_continuation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    prior_attempt_id: str,
    direction_id: str,
    actor_id: str,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    provider_event: Mapping[str, Any],
    prior_output_digest: str,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> StagedContinuation:
    """Phase A: close the iteration and stage the successor, in one commit of their own.

    One transaction writes what the Direction's application will need to find already
    committed (0069 can only see it exist; this sequencing is what makes it committed):

    1. the prior Attempt's `chat.generation.iteration_completed` event, carrying the
       job's public CAS token as it stood at the insert (including any Direction
       admission already committed at this boundary) and the digest of the output the
       iteration produced, never the output. The projection then advances once, as it
       does for every event;
    2. the successor's provider invocation identity (0028), through the typed writer;
    3. the successor Attempt: the next number, retrying the prior Attempt;
    4. the successor's request manifest, binding this Direction as
       `{"contentDigest", "directionId", "directionVersion": 1}`.

    Nothing here moves the Direction, the current Attempt or the Job's lease owner: until
    :func:`apply_direction_continuation` commits, the Generation is still on the prior
    Attempt and these rows are only staged. Every identity is derived, so a crash after
    this commit and a duplicate worker both find the rows rather than writing others: each
    row that exists is held to what a replay would write (identities, route, and the
    manifest's canonical bytes; the event's iteration and digest, never its recorded
    version), and a call that finds them all writes nothing and moves no version
    (`replayed`). One that finds a row that differs is :class:`GenerationConflict`, and
    nothing is repaired.

    Refused without a write, in the order decided: a job or attempt that already ended
    (:class:`GenerationTerminal`), a lease another instance holds or an attempt that is
    not the running one (:class:`GenerationConflict`), a Direction that is not pending
    (:class:`DirectionNotPending`), and a boundary with no Attempt or event position left
    to open a successor in, or one another Attempt already follows
    (:class:`BoundaryUnavailable`).
    """
    finish_id = provider_finish_id(provider_event)
    if not _SHA256_DIGEST.match(prior_output_digest):
        raise GenerationConflict("an iteration's prior output digest must be sha256:<64 lower hex>")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        staged = _stage(
            writer,
            generation_job_id=generation_job_id,
            prior_attempt_id=prior_attempt_id,
            direction_id=direction_id,
            actor_id=actor_id,
            identities=identities,
            manifest_body=manifest_body,
            finish_id=finish_id,
            prior_output_digest=prior_output_digest,
            lease_owner=lease_owner,
            now_us=now_us,
            lease_duration_us=lease_duration_us,
        )
    return staged


def _stage(
    writer: chat.ChatWriter,
    *,
    generation_job_id: str,
    prior_attempt_id: str,
    direction_id: str,
    actor_id: str,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    finish_id: str,
    prior_output_digest: str,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int,
) -> StagedContinuation:
    connection = writer.connection
    workspace_id = writer.workspace_id
    boundary = _read_boundary(
        writer,
        generation_job_id=generation_job_id,
        prior_attempt_id=prior_attempt_id,
        direction_id=direction_id,
        actor_id=actor_id,
        identities=identities,
        manifest_body=manifest_body,
    )
    job, direction, prior = boundary.job, boundary.direction, boundary.prior
    if prior_output_digest != _durable_output_digest(
        connection,
        workspace_id=workspace_id,
        generation_job_id=generation_job_id,
        generation_attempt_id=prior_attempt_id,
    ):
        raise GenerationConflict(
            f"the output digest given for attempt {prior_attempt_id!r} is not that of what it wrote"
        )

    iteration_event: chat.GenerationEvent | None = None
    for event in boundary.closing:
        if event.provider_event_id != finish_id:
            raise GenerationConflict(
                f"generation attempt {prior_attempt_id!r} already closed its iteration "
                "on another provider event"
            )
        if (
            event.payload.get("iteration") != boundary.iteration
            or event.payload.get("priorOutputDigest") != prior_output_digest
        ):
            raise DuplicateProviderEvent(
                f"provider event {finish_id!r} already closed an iteration of this attempt "
                "with other facts"
            )
        iteration_event = event
    invocation, attempt, manifest = _read_staged(
        connection, workspace_id=workspace_id, boundary=boundary, identities=identities
    )

    if direction.state == "applied":
        # Phase B has already committed: stage nothing, prove the whole of it.
        if iteration_event is None:
            raise GenerationConflict(
                f"generation attempt {prior_attempt_id!r} has no committed iteration event"
            )
        invocation, attempt, manifest = _require_applied(
            connection,
            workspace_id=workspace_id,
            boundary=boundary,
            identities=identities,
            manifest_body=manifest_body,
            iteration_event=iteration_event,
        )
        return StagedContinuation(
            iteration_event=iteration_event,
            invocation=invocation,
            attempt=attempt,
            manifest=manifest,
            replayed=True,
        )
    if direction.state != "pending" or direction.version != 1:
        raise DirectionNotPending(f"direction {direction_id!r} is not pending")

    status = _require_running_on(
        connection,
        workspace_id=workspace_id,
        job=job,
        attempt_id=prior_attempt_id,
        lease_owner=lease_owner,
    )
    if _staged_mismatch(boundary, identities, manifest_body, invocation, attempt, manifest):
        raise GenerationConflict(
            f"rows of the continuation of direction {direction_id!r} already exist "
            "and are not the ones this replay would write"
        )
    if attempt is None and (
        prior.attempt_number != max(a.attempt_number for a in boundary.attempts)
        or prior.attempt_number >= MAX_GENERATION_ATTEMPTS
    ):
        raise BoundaryUnavailable(f"generation attempt {prior_attempt_id!r} has no successor to open")
    next_sequence = max(event.generation_event_sequence for event in boundary.events) + 1
    # The seam writes two events, this iteration's and the Direction's state change.
    if iteration_event is None and next_sequence + 1 > _MAX_EVENT_SEQUENCE:
        raise BoundaryUnavailable(
            f"generation job {generation_job_id!r} has no event position left for a continuation"
        )

    at_us = max(now_us, direction.updated_at_us, status.updated_at_us, job.updated_at_us)
    connection_id, model_id = boundary.route
    if iteration_event is None:
        writer.append_generation_event(
            event_id=_event_id(generation_job_id, next_sequence),
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=prior_attempt_id,
            event_type=_ITERATION_COMPLETED,
            generation_event_sequence=next_sequence,
            trigger_message_id=job.trigger_message_id,
            provider_event_id=finish_id,
            cursor=_cursor(generation_job_id, next_sequence),
            payload={
                "generationJobVersion": status.version,
                "iteration": boundary.iteration,
                "priorOutputDigest": prior_output_digest,
            },
            occurred_at_us=now_us,
            schema_version=1,
        )
        _record_event_on_job(
            writer,
            job,
            status,
            sequence=next_sequence,
            now_us=now_us,
            lease_duration_us=lease_duration_us,
        )
    if invocation is None:
        writer.append_provider_invocation(
            invocation_id=identities.provider_invocation_id,
            conversation_id=job.conversation_id,
            job_id=generation_job_id,
            generation_attempt_id=identities.generation_attempt_id,
            configured_connection_id=connection_id,
            configured_model_id=model_id,
            created_at_us=at_us,
        )
    if attempt is None:
        writer.append_generation_attempt(
            generation_attempt_id=identities.generation_attempt_id,
            conversation_id=job.conversation_id,
            generation_job_id=generation_job_id,
            attempt_number=prior.attempt_number + 1,
            retry_of_attempt_id=prior_attempt_id,
            state="running",
            schema_version=1,
            provider_invocation_id=identities.provider_invocation_id,
            started_at_us=at_us,
        )
    if manifest is None:
        writer.append_request_manifest(
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=identities.generation_attempt_id,
            trigger_message_id=job.trigger_message_id,
            provider_invocation_id=identities.provider_invocation_id,
            request_manifest_id=identities.request_manifest_id,
            idempotency_key=str(manifest_body["idempotencyKey"]),
            manifest_body=manifest_body,
            created_at_us=at_us,
        )

    wrote = iteration_event is None or invocation is None or attempt is None or manifest is None
    closed = next(
        (
            event
            for event in chat.read_generation_events(
                connection, workspace_id=workspace_id, generation_job_id=generation_job_id
            )
            if event.generation_attempt_id == prior_attempt_id
            and event.event_type == _ITERATION_COMPLETED
        ),
        None,
    )
    staged_attempt = chat.read_generation_attempt(
        connection, workspace_id=workspace_id, generation_attempt_id=identities.generation_attempt_id
    )
    staged_invocation = chat.read_provider_invocation(
        connection, workspace_id=workspace_id, invocation_id=identities.provider_invocation_id
    )
    staged_manifest = chat.read_request_manifest(
        connection,
        workspace_id=workspace_id,
        generation_attempt_id=identities.generation_attempt_id,
    )
    if (
        closed is None
        or staged_attempt is None
        or staged_invocation is None
        or staged_manifest is None
    ):  # pragma: no cover - the writes above just made all four
        raise GenerationNotFound(f"continuation of direction {direction_id!r} did not stage")
    return StagedContinuation(
        iteration_event=closed,
        invocation=staged_invocation,
        attempt=staged_attempt,
        manifest=staged_manifest,
        replayed=not wrote,
    )


def apply_direction_continuation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    prior_attempt_id: str,
    direction_id: str,
    actor_id: str,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    expected_status_version: int,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> DirectionContinuation:
    """Phase B: apply the Direction to the successor phase A staged, in a commit of its own.

    Only after :func:`stage_direction_continuation` has committed, never in the same
    transaction (opening one inside another is refused by the fence). One transaction
    moves the Direction `pending` v1 -> `applied` v2 at `next_iteration`, bound to the
    staged invocation, Attempt and manifest; appends the continuation record, bound to the
    prior Attempt's committed iteration event; appends the Direction's
    `chat.direction.state_changed` event (the id, the new version and the state, nothing
    of its content); and last moves both authorities to the successor: the Job row's
    current Attempt, event sequence and re-stated lease, and the status projection's one
    compare-and-set step. The Direction moves while the projection is still on the prior
    Attempt because 0069 requires exactly that. A failure anywhere rolls back only this.

    Everything is re-proved from the rows before the first write, and none of the staged
    rows is written here: the iteration event is the single one of the prior Attempt and
    still the digest of its durable output, the invocation, Attempt and manifest are the
    successor's and the manifest's canonical bytes are `manifest_body`'s, the projection
    is running on the prior Attempt at `expected_status_version`, and the Job row agrees
    and is leased by `lease_owner`. Rows that are not staged, or differ, are
    :class:`GenerationConflict`; a stale token is :class:`StaleGenerationVersion`; a
    Direction that is no longer pending is :class:`DirectionNotPending`; a finished job is
    :class:`GenerationTerminal`. Nothing is written for any of them.

    The exception is the exact replay: a Direction already applied by these identities
    with every row present and equal to what would be written is returned with nothing
    written, so no version moves.
    """
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        replayed = _apply(
            writer,
            generation_job_id=generation_job_id,
            prior_attempt_id=prior_attempt_id,
            direction_id=direction_id,
            actor_id=actor_id,
            identities=identities,
            manifest_body=manifest_body,
            expected_status_version=expected_status_version,
            lease_owner=lease_owner,
            now_us=now_us,
            lease_duration_us=lease_duration_us,
        )
    direction = chat.read_direction(connection, workspace_id=workspace_id, direction_id=direction_id)
    continuation = chat.read_generation_continuation(
        connection, workspace_id=workspace_id, continuation_id=identities.continuation_id
    )
    attempt = chat.read_generation_attempt(
        connection, workspace_id=workspace_id, generation_attempt_id=identities.generation_attempt_id
    )
    manifest = chat.read_request_manifest(
        connection, workspace_id=workspace_id, generation_attempt_id=identities.generation_attempt_id
    )
    # The transaction above just committed all four.
    if direction is None or continuation is None or attempt is None or manifest is None:  # pragma: no cover
        raise GenerationNotFound(f"direction {direction_id!r} did not settle")
    return DirectionContinuation(
        direction=direction,
        continuation=continuation,
        attempt=attempt,
        manifest=manifest,
        replayed=replayed,
    )


def _apply(
    writer: chat.ChatWriter,
    *,
    generation_job_id: str,
    prior_attempt_id: str,
    direction_id: str,
    actor_id: str,
    identities: ContinuationIdentities,
    manifest_body: Mapping[str, Any],
    expected_status_version: int,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int,
) -> bool:
    connection = writer.connection
    workspace_id = writer.workspace_id
    boundary = _read_boundary(
        writer,
        generation_job_id=generation_job_id,
        prior_attempt_id=prior_attempt_id,
        direction_id=direction_id,
        actor_id=actor_id,
        identities=identities,
        manifest_body=manifest_body,
    )
    job, direction = boundary.job, boundary.direction
    iteration_event = boundary.closing[0] if len(boundary.closing) == 1 else None
    finish_id = None if iteration_event is None else iteration_event.provider_event_id
    prior_output_digest = None if iteration_event is None else iteration_event.payload.get("priorOutputDigest")
    if (
        iteration_event is None
        or finish_id is None
        or not isinstance(prior_output_digest, str)
        or not _SHA256_DIGEST.match(prior_output_digest)
        or iteration_event.payload.get("iteration") != boundary.iteration
        or prior_output_digest
        != _durable_output_digest(
            connection,
            workspace_id=workspace_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=prior_attempt_id,
        )
    ):
        raise GenerationConflict(
            f"generation attempt {prior_attempt_id!r} has no single committed iteration "
            "event for this continuation"
        )

    if direction.state == "applied":
        _require_applied(
            connection,
            workspace_id=workspace_id,
            boundary=boundary,
            identities=identities,
            manifest_body=manifest_body,
            iteration_event=iteration_event,
        )
        return True
    if direction.state != "pending" or direction.version != 1:
        raise DirectionNotPending(f"direction {direction_id!r} is not pending")

    status = _require_running_on(
        connection,
        workspace_id=workspace_id,
        job=job,
        attempt_id=prior_attempt_id,
        lease_owner=lease_owner,
    )
    if status.version != expected_status_version:
        raise StaleGenerationVersion(
            f"generation job {generation_job_id!r} moved since its version was read"
        )
    invocation, attempt, manifest = _read_staged(
        connection, workspace_id=workspace_id, boundary=boundary, identities=identities
    )
    if (
        invocation is None
        or attempt is None
        or manifest is None
        or _staged_mismatch(boundary, identities, manifest_body, invocation, attempt, manifest)
    ):
        raise GenerationConflict(
            f"the successor of direction {direction_id!r} is not staged as this continuation "
            "would write it"
        )
    if (
        chat.read_generation_continuation(
            connection, workspace_id=workspace_id, continuation_id=identities.continuation_id
        )
        is not None
        or chat.read_generation_continuation_for_direction(
            connection, workspace_id=workspace_id, direction_id=direction_id
        )
        is not None
    ):
        raise GenerationConflict(
            f"a continuation of direction {direction_id!r} already exists"
        )
    sequence = max(event.generation_event_sequence for event in boundary.events) + 1
    if sequence > _MAX_EVENT_SEQUENCE:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} has no event position left for a continuation"
        )

    at_us = max(
        now_us,
        direction.updated_at_us,
        iteration_event.occurred_at_us,
        manifest.created_at_us,
        attempt.started_at_us,
        status.updated_at_us,
        job.updated_at_us,
    )
    writer.update_direction(
        direction_id=direction_id,
        expected_version=direction.version,
        state="applied",
        terminal_at_us=at_us,
        effective_boundary="next_iteration",
        applied_attempt_id=identities.generation_attempt_id,
        applied_provider_invocation_id=identities.provider_invocation_id,
        applied_request_manifest_id=identities.request_manifest_id,
        applied_iteration=boundary.iteration + 1,
    )
    writer.append_generation_continuation(
        continuation_id=identities.continuation_id,
        conversation_id=job.conversation_id,
        generation_job_id=generation_job_id,
        prior_attempt_id=prior_attempt_id,
        next_attempt_id=identities.generation_attempt_id,
        direction_id=direction_id,
        direction_version=direction.version + 1,
        provider_finish_id=finish_id,
        prior_output_digest=prior_output_digest,
        schema_version=1,
        created_at_us=at_us,
    )
    writer.append_generation_event(
        event_id=_event_id(generation_job_id, sequence),
        conversation_id=job.conversation_id,
        branch_id=job.branch_id,
        generation_job_id=generation_job_id,
        event_type=_DIRECTION_STATE_CHANGED,
        generation_event_sequence=sequence,
        trigger_message_id=job.trigger_message_id,
        cursor=_cursor(generation_job_id, sequence),
        payload={
            "directionId": direction_id,
            "directionVersion": direction.version + 1,
            "state": "applied",
        },
        occurred_at_us=at_us,
        schema_version=1,
    )
    _record_event_on_job(
        writer,
        job,
        status,
        sequence=sequence,
        now_us=at_us,
        lease_duration_us=lease_duration_us,
        current_attempt_id=identities.generation_attempt_id,
    )
    return False


def close_unapplied_direction(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    direction_id: str,
    state: str,
    code: str,
    now_us: int,
    lease_owner: str | None = None,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> chat.Direction:
    """End a pending Direction that was never applied: `rejected` or `cancelled`.

    `(state, code)` must be one of the contract's closed pairs, and the stored reason is
    that code, the instant, and the one constant sentence for the pair; nothing of the
    Direction, the provider or an exception. The move and its identity-only
    `chat.direction.state_changed` event (the Direction's id, new version and state, no
    content, hash or reason) are one transaction, and the job's token advances only if the
    job is still running. Closing a Direction already closed this exact way returns it
    unchanged and writes nothing; closing one that is anything else is
    :class:`DirectionNotPending`.
    """
    message = _DIRECTION_REASONS.get((state, code))
    if message is None:
        raise GenerationConflict("a Direction is closed unapplied only as rejected or cancelled")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        job = chat.read_generation_job(
            connection, workspace_id=workspace_id, generation_job_id=generation_job_id
        )
        if job is None:
            raise GenerationNotFound(
                f"generation job {generation_job_id!r} is not in this workspace"
            )
        direction = chat.read_direction(
            connection, workspace_id=workspace_id, direction_id=direction_id
        )
        if direction is None or direction.generation_job_id != generation_job_id:
            raise GenerationNotFound(f"direction {direction_id!r} is not in this generation")
        if direction.state != "pending":
            reason = direction.terminal_reason
            if direction.state == state and reason is not None and reason.get("code") == code:
                return direction
            raise DirectionNotPending(f"direction {direction_id!r} is not pending")
        status = _require_status(connection, workspace_id=workspace_id, job=job)
        if (
            lease_owner is not None
            and job.state == "running"
            and job.lease_owner != lease_owner
        ):
            raise GenerationConflict(
                f"generation job {generation_job_id!r} is leased by another instance"
            )
        events = chat.read_generation_events(
            connection, workspace_id=workspace_id, generation_job_id=generation_job_id
        )
        sequence = max((event.generation_event_sequence for event in events), default=0) + 1
        at_us = max(now_us, direction.updated_at_us)
        writer.update_direction(
            direction_id=direction_id,
            expected_version=direction.version,
            state=state,
            terminal_at_us=at_us,
            terminal_reason={"code": code, "occurredAt": _timestamp(at_us), "safeMessage": message},
        )
        writer.append_generation_event(
            event_id=_event_id(generation_job_id, sequence),
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            event_type=_DIRECTION_STATE_CHANGED,
            generation_event_sequence=sequence,
            trigger_message_id=job.trigger_message_id,
            cursor=_cursor(generation_job_id, sequence),
            payload={
                "directionId": direction_id,
                "directionVersion": direction.version + 1,
                "state": state,
            },
            occurred_at_us=at_us,
            schema_version=1,
        )
        _record_event_on_job(
            writer,
            job,
            status,
            sequence=sequence,
            now_us=at_us,
            lease_duration_us=lease_duration_us,
        )
    closed = chat.read_direction(connection, workspace_id=workspace_id, direction_id=direction_id)
    if closed is None:  # pragma: no cover - the transaction above just committed it
        raise GenerationNotFound(f"direction {direction_id!r} did not settle")
    return closed


# --- replay --------------------------------------------------------------------------


# --- lease renewal -----------------------------------------------------------------


def renew_generation_lease(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> int:
    """Extend a running job's lease without writing an event. Returns the new expiry.

    0029 requires a `running` job to carry a lease that has not expired at the moment
    of the write, and every write above re-states it -- which is enough only while
    events keep arriving. A provider that thinks for longer than the lease before its
    first token, or pauses mid-stream, would let its own claim lapse and leave a job
    that is durably `running` under a dead lease. This is the heartbeat that does not
    depend on the stream producing anything.

    It is also the fencing check. The compare-and-set is on `(state, lease_epoch)`, so
    a renewal whose job has been taken over by another instance fails rather than
    silently reviving a claim someone else now holds; the owner is compared too, so a
    caller learns it lost the lease instead of extending a stranger's.

    Nothing else about the job moves: state, epoch, attempt and event sequence are all
    written back as they were read, because `update_generation_job` sets every column
    and a heartbeat that reset `last_event_sequence` would corrupt the replay cursor
    it is supposed to protect.
    """
    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    if job.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if job.state != "running":
        raise GenerationLifecycleError(
            f"generation job {generation_job_id!r} is {job.state!r}, not running"
        )
    if job.lease_owner != lease_owner:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is leased by another instance"
        )
    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state != "running" or status.current_attempt_id != job.current_attempt_id:
        raise GenerationConflict(f"generation job {generation_job_id!r} is not running")

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_generation_job(
            generation_job_id=generation_job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state=job.state,
            lease_epoch=job.lease_epoch,
            current_attempt_id=job.current_attempt_id,
            lease_owner=job.lease_owner,
            lease_expires_at_us=expires_at_us,
            heartbeat_at_us=now_us,
            last_event_sequence=job.last_event_sequence,
            updated_at_us=now_us,
            started_at_us=job.started_at_us,
        )
        _restate_status(writer, status, updated_at_us=now_us)
    return expires_at_us


def recover_generation_attempt_lease(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    recovery_owner: str,
    now_us: int,
    lease_duration_us: int = DEFAULT_LEASE_US,
) -> chat.GenerationJob:
    """Take over one expired running attempt under a new lease epoch.

    Recovery is intentionally narrow: it does not enqueue a retry, append a
    provider event or change transcript state. It only moves an expired running
    attempt to a new lease owner/fence. The ``(state, lease_epoch)`` compare-and-set
    means concurrent recovery attempts produce one winner, and stale provider
    output from the old owner can be refused by the event append's ``lease_owner``
    check.
    """
    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        raise GenerationNotFound(f"generation job {generation_job_id!r} is not in this workspace")
    status = _require_status(connection, workspace_id=workspace_id, job=job)
    if status.state in _TERMINAL_JOB_STATES:
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")
    if status.state != "running" or status.current_attempt_id != generation_attempt_id:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} is not running this attempt"
        )
    if job.lease_expires_at_us is None or job.lease_expires_at_us > now_us:
        raise GenerationConflict(
            f"generation job {generation_job_id!r} still has an active lease"
        )

    expires_at_us = now_us + lease_duration_us
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_generation_job(
            generation_job_id=generation_job_id,
            expected_state=job.state,
            expected_lease_epoch=job.lease_epoch,
            state=job.state,
            lease_epoch=job.lease_epoch + 1,
            current_attempt_id=job.current_attempt_id,
            result_message_id=job.result_message_id,
            lease_owner=recovery_owner,
            lease_expires_at_us=expires_at_us,
            heartbeat_at_us=now_us,
            last_event_sequence=job.last_event_sequence,
            sanitized_error_code=job.sanitized_error_code,
            sanitized_error_detail=job.sanitized_error_detail,
            updated_at_us=now_us,
            started_at_us=job.started_at_us,
            finished_at_us=job.finished_at_us,
        )
        _restate_status(writer, status, updated_at_us=now_us)
    recovered = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if recovered is None:  # pragma: no cover - the transaction just committed it
        raise GenerationNotFound(f"generation job {generation_job_id!r} vanished during recovery")
    return recovered


def replay_generation_events(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    generation_job_id: str,
    after_cursor: str | None = None,
) -> GenerationReplay:
    """This job's durable events after `after_cursor`, or the demand for a snapshot.

    `after_cursor` omitted replays the whole history. A cursor that is malformed,
    names another job, or names a position this job's durable history does not hold
    produces `requires_resnapshot` with one of the contract's own closed reasons
    rather than a stream this module cannot honestly claim continues from there. So
    does a history whose sequences are not contiguous, which 0029 makes unreachable
    through the guarded writers and which is therefore evidence the file was edited
    from outside them.
    """
    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if job is None:
        return _resnapshot("unauthorized_cursor")

    events = chat.read_generation_events(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    if [event.generation_event_sequence for event in events] != list(range(1, len(events) + 1)):
        return _resnapshot("gap_detected")

    if after_cursor is None:
        return GenerationReplay(events=events)

    sequence, reason = _cursor_position(after_cursor, generation_job_id)
    if sequence is None:
        return _resnapshot(reason)
    if sequence > len(events):
        return _resnapshot("cursor_unknown_or_expired")
    return GenerationReplay(events=events[sequence:])


def _resnapshot(reason: str | None) -> GenerationReplay:
    if reason not in RESNAPSHOT_REASONS:  # pragma: no cover - a closed vocabulary
        raise GenerationLifecycleError("a resnapshot reason must come from the contract's own set")
    return GenerationReplay(requires_resnapshot=True, reason=reason)
