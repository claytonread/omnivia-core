"""One generation's durable lifecycle and its replay, over the W2-R Chat repository.

The first bounded slice of Core's M4 generation service: claim one queued
submission, open the durable job and attempt it runs under, fold already-normalized
F2a provider events into the five durable generation event types migration 0029
admits, and replay that history after a cursor.

**Durable rows are the only authority.** Nothing here holds process-local state, so
a crash between any two calls loses nothing: the next call reads the queued
submission, the generation job and the generation event history back from SQLite
and continues from exactly where the rows say it was. That is also what makes the
idempotency real rather than best-effort -- a repeated claim of an
already-`submitted` queue row returns the job that row names, and a repeated
provider event with the same `providerEventId` returns the durable event already
written for it.

**No provider content is ever persisted.** A provider event wire reaches this module
having already crossed F2a, and it is still never copied into a durable row: the
payload written is built here from a closed allow-list of contract vocabulary
members and bounded sanitised fields (`providerEventType`, `providerEventId`,
`finishReason`, `errorCode`, `retryable`, `statusClass`, `safeMessage`), so a raw
request or response body, a header, a URL, a credential or an SDK object present on
the input mapping has no path into storage even if F2a were bypassed.

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
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.chat_contract.v1.generated import (
    F2A_FINISH_REASONS,
    F2A_PROVIDER_ERROR_CODES,
    F2A_PROVIDER_EVENT_TYPES,
    RESNAPSHOT_REASONS,
)
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage import chat

__all__ = [
    "ClaimedGeneration",
    "DuplicateProviderEvent",
    "GenerationConflict",
    "GenerationLifecycleError",
    "GenerationNotFound",
    "GenerationReplay",
    "GenerationSequenceGap",
    "GenerationTerminal",
    "UnsupportedProviderEvent",
    "append_provider_generation_event",
    "claim_queued_generation",
    "replay_generation_events",
]

#: How long a claim's lease runs from the moment it is taken or extended. 0029
#: requires a `running` job to carry a lease that has not expired at the moment of
#: the write, so every write below that leaves a job running re-states it.
DEFAULT_LEASE_US: Final = 60_000_000

_QUEUED: Final = "chat.generation.queued"
_STARTED: Final = "chat.generation.started"
_SUCCEEDED: Final = "chat.generation.succeeded"
_FAILED: Final = "chat.generation.failed"
_CANCELLED: Final = "chat.generation.cancelled"

#: The durable event types after which no further event may be appended.
_TERMINAL_EVENT_TYPES: Final = frozenset({_SUCCEEDED, _FAILED, _CANCELLED})
_TERMINAL_JOB_STATES: Final = frozenset({"succeeded", "failed", "cancelled"})

#: The durable job state each terminal durable event moves the job to.
_TERMINAL_JOB_STATE_OF: Final = {
    _SUCCEEDED: "succeeded",
    _FAILED: "failed",
    _CANCELLED: "cancelled",
}

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


def _optional_provider_event_id(wire: Mapping[str, Any]) -> str | None:
    value = _optional_wire_str(wire, "providerEventId")
    if value is None:
        return None
    if (
        len(value) > _DURABLE_PROVIDER_EVENT_ID_MAX
        or value[0] not in _DURABLE_IDENTIFIER_INITIAL
        or any(character not in _DURABLE_IDENTIFIER_CHARS for character in value)
    ):
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
    reasoning, tool and metadata event in the F2a vocabulary. Those are legal parts
    of a trace and are simply not durable generation-lifecycle facts: 0029 closes
    `omnivia_chat_generation_events.event_type` to the five it is, and inventing a
    sixth here to hold a text delta would be a schema change written in Python.

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
        return _claimed(connection, workspace_id=workspace_id, submission=submission)

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
        or existing_job.state != "queued"
        or existing_job.lease_epoch != 0
        or existing_job.current_attempt_id is not None
        or existing_job.last_event_sequence != 0
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
            )
        writer.append_generation_attempt(
            generation_attempt_id=generation_attempt_id,
            conversation_id=submission.conversation_id,
            generation_job_id=generation_job_id,
            attempt_number=1,
            state="running",
            schema_version=1,
            started_at_us=now_us,
        )
        writer.append_generation_event(
            event_id=f"{generation_job_id}.e1",
            conversation_id=submission.conversation_id,
            branch_id=submission.branch_id,
            generation_job_id=generation_job_id,
            event_type=_QUEUED,
            generation_event_sequence=1,
            trigger_message_id=trigger_message_id,
            cursor=_cursor(generation_job_id, 1),
            payload={"queuedSubmissionId": queued_submission_id, "attemptNumber": 1},
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
) -> chat.GenerationEvent | None:
    """Fold one already-normalized provider event into this job's durable history.

    Returns the durable event appended, the identical one already durable for this
    `providerEventId` (an idempotent redelivery), or `None` for a provider event
    that carries no generation-lifecycle transition -- a text or tool delta is a
    legal part of a trace and simply is not one of the five durable event types
    0029 admits, so nothing is written and nothing is refused.

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
    if provider_event_id is not None:
        existing = _existing_provider_event(
            events,
            generation_attempt_id=generation_attempt_id,
            provider_event_id=str(provider_event_id),
        )
        if existing is not None:
            if (
                existing.event_type == event_type
                and dict(existing.payload) == payload
                and existing.result_message_id == result_message_id
            ):
                return existing
            raise DuplicateProviderEvent(
                f"provider event {provider_event_id!r} is already durable on this attempt "
                "carrying a different durable event"
            )

    if job.state in _TERMINAL_JOB_STATES or any(
        event.event_type in _TERMINAL_EVENT_TYPES for event in events
    ):
        raise GenerationTerminal(f"generation job {generation_job_id!r} already ended")

    sequence = job.last_event_sequence + 1
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
            event_id=f"{generation_job_id}.e{sequence}",
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
        else:
            code, detail = (
                (None, None)
                if terminal_state == "succeeded"
                else _sanitized_failure(event_type, payload)
            )
            writer.update_generation_job(
                generation_job_id=generation_job_id,
                expected_state=job.state,
                expected_lease_epoch=job.lease_epoch,
                state=terminal_state,
                lease_epoch=job.lease_epoch,
                current_attempt_id=job.current_attempt_id,
                result_message_id=result_message_id if terminal_state == "succeeded" else None,
                last_event_sequence=sequence,
                sanitized_error_code=code,
                sanitized_error_detail=detail,
                updated_at_us=now_us,
                started_at_us=job.started_at_us,
                finished_at_us=now_us,
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


# --- replay --------------------------------------------------------------------------


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
