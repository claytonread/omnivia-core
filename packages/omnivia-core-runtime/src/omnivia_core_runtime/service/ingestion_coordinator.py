"""Service-owned coordination of one connector synchronisation run (V06-8).

This is a maintenance path, not an application operation: it adds nothing to the
frozen catalogue, is never reachable over a transport, and runs only while this
process holds the workspace lease, the mutation guard and the current fencing
generation. What it does is drive a `SourceConnector` through a durable run and
leave behind facts the next run can restart from.

Four properties are load-bearing, and each one is a consequence of an ordering
choice rather than of a check:

**The cursor never runs ahead of the work.** One batch is one fenced
transaction, and the cursor checkpoint is the last statement in it. A crash
therefore leaves either the batch applied *and* the cursor advanced, or neither.
There is no window in which the cursor says work is done that is not.

**Replay is a no-op, not a duplicate.** Every effect this module has is decided
by comparing the reported change against durable *current state* -- does an
artifact for these exact bytes exist, is the item present or tombstoned, where
is it now, which labels does it carry -- rather than by appending whatever
arrived. Re-processing a batch therefore finds its own work already done and
writes nothing. That is what makes the previous paragraph safe when a *previous*
run committed a batch and died before finishing. A dead letter follows the same
rule: an item this run has already given up on can legitimately arrive again on
a later page -- a paginated listing shifting under the reader is enough -- and
the second sighting writes nothing rather than colliding with the first.

**Bytes exist before the database names them.** Content is fetched, verified
against the connector's declared checksum *and* declared length, and published
to the content-addressed blob store before any transaction opens. A crash can
leave an unreferenced blob, which the next run republishes identically; it
cannot leave a row naming bytes that are not there.

**Cancellation lands on a boundary.** It is observed between batches, between
items, and once more after the last item's bytes are in hand and before the
batch transaction opens -- the window the per-item check cannot cover, because
the final `content()` call is where a long batch most often spends its time. A
cancelled run abandons the batch it was preparing entirely rather than
committing part of it, so the durable cursor always names a fully applied point.

Two more properties are checks rather than orderings, because nothing about the
shape of the work could have made them free:

**A connector is untrusted input.** Its identity, its declared state version,
every value it returns and the type of every return are validated at this
boundary. Text it authors -- failure messages, health detail -- is redacted and
bounded before it reaches a durable column, because a message is the one place a
credential reliably leaks: an exception carrying an authorization header is the
normal way for a connector to fail, not an exotic one.

**A run is bounded, and the bound is decided before the bytes move.** Batches,
changes and content bytes each have a per-run ceiling. A batch is the indivisible
unit -- it is applied whole in one transaction or not at all -- so each ceiling is
tested at the batch boundary, against what the *fetched but not yet materialised*
batch declares it will cost, and before any `content()` call. A batch that does
not fit the run's remaining budget is not fetched from at all: the run stops
successfully, reports `SyncOutcome.truncated`, leaves the cursor where the last
committed batch put it, and the next run picks the same batch up with a full
budget. A batch that could not fit the whole-run ceiling even at zero bytes spent
would truncate every run forever, so it is failed deterministically as a
connector contract violation instead. Declared cost, not fetched cost, is what
the run counts, so an item that dead-letters or is retried cannot make the
running total disagree with the decision that admitted its batch.

**What ordering can and cannot be enforced here** is worth stating precisely,
because an opaque cursor invites a guarantee it cannot support. A cursor token
is opaque and therefore carries no order; `source_version` is a source-native
token and is likewise not comparable. So this module does not, and cannot,
detect that a source delivered an old batch after a new one. What it does
guarantee is: the durable resume point never regresses, because it is a single
row under the total order `(sync_sequence, checkpoint_sequence)`; the same batch
applied twice is a no-op, because every decision is against current state; and a
batch that does arrive out of order is applied as the source's latest word,
with every earlier word preserved in the append-only evidence, provenance and
label streams. Last delivered writer wins, nothing is lost, and no observation
is silently discarded.

The one place that reads like staleness detection is the rename rule, and it is
narrower than it looks: a move whose `previous_locator` is not where the item
currently is has nothing to apply and is a no-op. That is a statement about
applicability, not about age. A replayed A->B after the item legitimately cycled
B->A is indistinguishable from a fresh A->B and is applied -- correctly, since
nothing here can tell the two apart.

Bidirectional synchronisation is out of scope by decision: nothing here writes to
a source.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from omnivia_core.connector import (
    IDENTIFIER_PATTERN,
    MAX_DEAD_LETTER_ATTEMPTS,
    MAX_MESSAGE_LENGTH,
    MAX_TOKEN_LENGTH,
    TOKEN_PATTERN,
    Cancellation,
    ChangeKind,
    ConnectorCursor,
    ConnectorError,
    ConnectorFailure,
    DeadLetter,
    HealthState,
    SourceBatch,
    SourceChange,
    SourceConnector,
    SourceHealth,
    SyncOutcome,
)
from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core.contracts.v1.generated import (
    RETRY_CLASS_NON_RETRYABLE,
    RETRY_CLASS_RETRYABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.source_capture import (
    MAX_SOURCE_BYTES,
    SourceCaptureRefused,
    publish_blob,
)
from omnivia_core_runtime.storage import connectors as connector_state

#: The durable job type a run carries. Fixed, because it is the only job kind
#: `omnivia_evidence_artifacts.import_run_id` accepts, which is what binds the
#: evidence a run captures to the run that captured it.
RUN_JOB_TYPE: Final = "ingestion.import"

#: Recorded as the run's originating operation. A service-initiated maintenance
#: label in the same family as `system.recovery`; it is not, and must never
#: become, a member of the frozen application catalogue.
RUN_OPERATION: Final = "system.connector_sync"

#: The failure code for a connector that broke the protocol itself, as opposed
#: to one whose source misbehaved. Always non-retryable: a contract violation
#: repeats.
CONTRACT_VIOLATION: Final = "connector_contract_violation"

_SERVICE_ACTOR: Final = "core-service"
_SERVICE_ACTOR_KIND: Final = "service"
_MAX_ITEM_ATTEMPTS: Final = 3
_MAX_RUN_ATTEMPTS: Final = 8

#: Per-run ceilings. Generous enough that no honest source meets them in a pass
#: and small enough that a dishonest one cannot run forever.
_MAX_BATCHES_PER_RUN: Final = 1024
_MAX_CHANGES_PER_RUN: Final = 100_000
_MAX_CONTENT_BYTES_PER_RUN: Final = 1024 * 1024 * 1024

#: Everything a durable message must not contain. Control characters first
#: because a column that accepts them still has no use for them, then the shapes
#: credentials actually arrive in. The high-entropy rule is last and deliberately
#: blunt: it costs a redacted checksum in a message nobody parses, and it catches
#: the bearer token that none of the keyed rules were written for.
_CONTROL_CHARACTERS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_REDACTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@"), r"\1[redacted]@"),
    (
        re.compile(
            r"(?i)\b([a-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|apikey"
            r"|authorization|auth|credential|cookie|session|signature"
            # A quote between the key and its separator is the JSON case, which is
            # how a connector quoting a request body most often arrives.
            r"|private[_-]?key)[a-z0-9_.-]*)[\"']?\s*[=:]\s*"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;)\]}]+)"
        ),
        r"\1=[redacted]",
    ),
    (re.compile(r"(?i)\b(bearer|basic|digest)\s+\S+"), r"\1 [redacted]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b"), "[redacted]"),
)


class IngestionRefused(RuntimeError):
    """The run cannot be started without weakening a durable guarantee."""


def _now_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


def _iso(at_us: int) -> str:
    return datetime.fromtimestamp(at_us / 1_000_000, tz=UTC).isoformat()


def _identity(prefix: str, *parts: str) -> str:
    """A stable identifier derived from the facts it names.

    Derivation is what makes replay idempotent: the second attempt at the same
    fact computes the identifier the first one already wrote. Where a fact can
    legitimately recur -- an item deleted, restored and deleted again, an item
    moved back to a locator it once had -- the stream position is one of the
    facts, so the recurrence is a new event rather than a collision with its own
    history.
    """
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}-{digest}"


def _redact(text: str) -> str:
    """Bounded, control-free text with credential-shaped material removed."""
    if not isinstance(text, str):
        return ""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return " ".join(_CONTROL_CHARACTERS.sub(" ", text).split())[:MAX_MESSAGE_LENGTH]


def _message(text: str, fallback: str) -> str:
    """A redacted, bounded, NUL-free message the durable columns will accept."""
    return _redact(text) or fallback


def _declared_bytes(batch: SourceBatch) -> int:
    """What a batch says it will cost to materialise, before any of it is.

    Deletions carry no content and cost nothing; every other change carries a
    validated non-negative length. The sum is deterministic and available before
    the first `content()` call, which is the only reason a byte ceiling can be
    enforced without either fetching the batch first or splitting it.
    """
    return sum(
        change.content_length_bytes or 0
        for change in batch.changes
        if change.change_kind is not ChangeKind.DELETED
    )


def _refusal(code: str, message: str, retry_class: str) -> ConnectorFailure:
    return ConnectorFailure(code=code, message=message, retry_class=retry_class)


def _failure_of(error: Exception) -> ConnectorFailure:
    """Classify whatever a connector raised.

    An unclassified failure is non-retryable on purpose: nobody has said it is
    safe to repeat, and guessing that it is turns one fault into a loop. A
    classified one keeps its classification but not necessarily its wording --
    the connector chose what to say, this chooses what is safe to store.
    """
    if isinstance(error, ConnectorError) and isinstance(
        getattr(error, "failure", None), ConnectorFailure
    ):
        failure = error.failure
        message = _message(failure.message, f"connector reported {failure.code}")
        if message == failure.message:
            return failure
        return _refusal(failure.code, message, failure.retry_class)
    return _refusal(
        "connector_fault",
        _message(str(error), f"connector raised {type(error).__name__}"),
        RETRY_CLASS_NON_RETRYABLE,
    )


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One change whose bytes are already durable, ready to be recorded."""

    change: SourceChange
    content_length_bytes: int


@dataclass(frozen=True, slots=True)
class _Bound:
    """A connector's identity, after this boundary has agreed it is one."""

    connector_id: str
    source_kind: str
    state_version: int


@dataclass(frozen=True, slots=True)
class IngestionCoordinator:
    """Drives one connector through durable, restartable synchronisation runs."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    blobs_root: Path
    clock: Clock
    max_item_attempts: int = _MAX_ITEM_ATTEMPTS
    max_batches_per_run: int = _MAX_BATCHES_PER_RUN
    max_changes_per_run: int = _MAX_CHANGES_PER_RUN
    max_content_bytes_per_run: int = _MAX_CONTENT_BYTES_PER_RUN

    def __post_init__(self) -> None:
        """Refuse a limit that no run could honour, before the run starts.

        A zero or negative ceiling admits nothing and truncates every run
        forever; a bool is an `int` that reads as a flag and means 1; a non-int
        compares in ways nobody intended. All three are configuration mistakes
        rather than run outcomes, so they are refused at construction -- which is
        necessarily before any durable write, since a coordinator that does not
        exist has performed none.

        `max_item_attempts` has an upper bound as well as a lower one, and it is
        not this module's to choose: the attempt count it produces is recorded in
        a column that accepts 1 to `MAX_DEAD_LETTER_ATTEMPTS`. Configured above
        that, the first exhausted retry would build a `DeadLetter` the contract
        refuses -- mid-run, after bytes had moved, as an exception rather than an
        outcome. Refusing here costs a construction instead.
        """
        for name in (
            "max_item_attempts",
            "max_batches_per_run",
            "max_changes_per_run",
            "max_content_bytes_per_run",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise IngestionRefused(f"{name} must be a positive integer")
        if self.max_item_attempts > MAX_DEAD_LETTER_ATTEMPTS:
            raise IngestionRefused(
                f"max_item_attempts must be at most {MAX_DEAD_LETTER_ATTEMPTS}, "
                "the most attempts a dead letter can record"
            )

    # --- public entry point ---------------------------------------------------

    def synchronise(
        self, connector: SourceConnector, *, cancelled: Cancellation | None = None
    ) -> SyncOutcome:
        """Run one synchronisation pass and return what it did.

        Resumes from the connector's last durably committed cursor, or starts
        from the beginning when there is none. Raises `IngestionRefused` only
        for conditions no run outcome could honestly describe -- a connector
        whose declared identity is not in the durable domain, one that has moved
        *backwards* in state format, or durable state written by a version this
        one cannot read. Everything a connector does wrong *after* that is a run
        outcome, because a run that started is a run that has something to say.
        """
        is_cancelled: Cancellation = cancelled or (lambda: False)
        bound = self._bind(connector)
        run = self._start_run(bound)
        cursor = self._resume_cursor(bound)

        totals = {
            "ingested": 0,
            "unchanged": 0,
            "relabelled": 0,
            "restored": 0,
            "renamed": 0,
            "deleted": 0,
        }
        dead_lettered = 0
        batches = changes = content_bytes = 0
        truncated = False
        failure: ConnectorFailure | None = None
        state = "succeeded"

        # Every cursor this run has already been handed, including the one it
        # resumed from. A connector claiming more work behind one of them is
        # describing a loop, and the loop is this method.
        handed: set[str] = set() if cursor is None else {cursor.token}

        health = self._observe_health(connector, bound)
        if health is HealthState.UNAVAILABLE:
            state = "failed"
            failure = _refusal(
                "source_unavailable",
                "the connector reports its source as unavailable",
                RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )

        while failure is None:
            if is_cancelled():
                state = "cancelled"
                break
            if batches >= self.max_batches_per_run:
                truncated = True
                break
            try:
                batch = connector.fetch(cursor, cancelled=is_cancelled)
            except Exception as error:  # noqa: BLE001 -- a connector fault is a run
                # outcome to classify and record, never a service crash.
                failure = _failure_of(error)
                state = "failed"
                break

            failure = self._check_batch(batch, bound, handed)
            if failure is not None:
                state = "failed"
                break

            declared = _declared_bytes(batch)
            failure = self._check_affordable(len(batch.changes), declared)
            if failure is not None:
                state = "failed"
                break
            if (
                changes + len(batch.changes) > self.max_changes_per_run
                or content_bytes + declared > self.max_content_bytes_per_run
            ):
                # Not fetched from, not partly applied, not checkpointed. The
                # next run meets this same batch with a whole budget.
                truncated = True
                break

            prepared, letters, stopped = self._prepare(
                connector, batch.changes, is_cancelled
            )
            # The last item's bytes arrived before this is asked, which is the
            # window `_prepare` cannot close from the inside.
            if stopped or is_cancelled():
                state = "cancelled"
                break

            applied, recorded = self._commit_batch(
                run, prepared, letters, batch.cursor
            )
            for verdict, count in applied.items():
                totals[verdict] += count
            dead_lettered += recorded
            batches += 1
            changes += len(batch.changes)
            # Declared, not fetched: an item that dead-lettered still consumed
            # the budget its batch was admitted against, so the running total
            # cannot drift away from the decision that let this batch in.
            content_bytes += declared
            handed.add(batch.cursor.token)
            cursor = batch.cursor
            if not batch.has_more:
                break

        self._finish_run(run, state, failure)
        return SyncOutcome(
            run_id=run.run_id,
            state=state,
            dead_lettered=dead_lettered,
            truncated=truncated,
            cursor=cursor,
            failure=failure,
            **totals,
        )

    # --- the connector boundary -----------------------------------------------

    def _bind(self, connector: SourceConnector) -> _Bound:
        """Agree that this object's declared identity is one the schema accepts.

        Before any durable write, and refusing without quoting the offending
        value: an identity outside the domain is exactly the kind of value that
        should not be copied into a message that may itself be stored.
        """
        connector_id = getattr(connector, "connector_id", None)
        source_kind = getattr(connector, "source_kind", None)
        state_version = getattr(connector, "state_version", None)
        if (
            not isinstance(connector_id, str)
            or len(connector_id) > MAX_TOKEN_LENGTH
            or IDENTIFIER_PATTERN.fullmatch(connector_id) is None
        ):
            raise IngestionRefused(
                "connector_id is outside the accepted identifier domain"
            )
        if (
            not isinstance(source_kind, str)
            or len(source_kind) > MAX_TOKEN_LENGTH
            or TOKEN_PATTERN.fullmatch(source_kind) is None
        ):
            raise IngestionRefused("source_kind is outside the accepted domain")
        if (
            not isinstance(state_version, int)
            or isinstance(state_version, bool)
            or not 1 <= state_version <= 2**31 - 1
        ):
            raise IngestionRefused("state_version must be a positive 32-bit integer")
        return _Bound(
            connector_id=connector_id,
            source_kind=source_kind,
            state_version=state_version,
        )

    def _check_batch(
        self, batch: object, bound: _Bound, handed: set[str]
    ) -> ConnectorFailure | None:
        """Whatever `fetch` returned, or the reason it cannot be applied.

        The state-version rule is the one that matters most and is the least
        obvious. A cursor is committed durably and read back by every later run;
        one carrying a state version the connector does not write would be
        refused on resume *forever*, so a run that committed it would have
        poisoned the connector rather than merely failed. Refusing the batch
        costs one run.
        """
        if not isinstance(batch, SourceBatch):
            return _refusal(
                CONTRACT_VIOLATION,
                "the connector did not return a SourceBatch",
                RETRY_CLASS_NON_RETRYABLE,
            )
        if batch.cursor.state_version != bound.state_version:
            return _refusal(
                CONTRACT_VIOLATION,
                "the batch cursor's state version is not the connector's",
                RETRY_CLASS_NON_RETRYABLE,
            )
        if batch.has_more and batch.cursor.token in handed:
            return _refusal(
                CONTRACT_VIOLATION,
                "the connector reports more work behind a cursor it already returned",
                RETRY_CLASS_NON_RETRYABLE,
            )
        return None

    def _check_affordable(
        self, change_count: int, declared_bytes: int
    ) -> ConnectorFailure | None:
        """Whether any run could ever afford this batch, or why none could.

        A batch is indivisible, so a batch bigger than a whole run's ceiling
        cannot be split down to fit and cannot be deferred to a run with more
        budget -- there is no such run. Truncating on it would park the source
        behind it forever while every run reported success, which is the one
        failure mode a bound is supposed to prevent rather than create. It is
        the connector's ceiling to respect, so it is the connector's violation.
        """
        if (
            change_count > self.max_changes_per_run
            or declared_bytes > self.max_content_bytes_per_run
        ):
            return _refusal(
                CONTRACT_VIOLATION,
                "the batch declares more work than a whole run is permitted to do",
                RETRY_CLASS_NON_RETRYABLE,
            )
        return None

    # --- run lifecycle --------------------------------------------------------

    def _start_run(self, bound: _Bound) -> _Run:
        connector_id = bound.connector_id
        last_version = connector_state.read_last_state_version(
            self.connection, workspace_id=self.workspace_id, connector_id=connector_id
        )
        if last_version is not None and bound.state_version < last_version:
            raise IngestionRefused(
                f"connector {connector_id!r} declares state version "
                f"{bound.state_version}, behind the durable {last_version}"
            )

        sequence = connector_state.next_sync_sequence(
            self.connection, workspace_id=self.workspace_id, connector_id=connector_id
        )
        at_us = _now_us(self.clock)
        seed = (self.workspace_id, connector_id, str(sequence))
        run = _Run(
            run_id=_identity("job-sync", *seed),
            audit_ref=_identity("aud-sync", *seed),
            connector_id=connector_id,
            source_kind=bound.source_kind,
            sync_sequence=sequence,
            started_at_us=at_us,
        )
        payload = to_canonical_json(
            {
                "connector_id": connector_id,
                "source_kind": bound.source_kind,
                "state_version": bound.state_version,
                "sync_sequence": sequence,
            }
        )
        moment = _iso(at_us)
        with self._fenced():
            self.connection.execute(
                "INSERT INTO omnivia_application_audit_events "
                "(audit_ref, workspace_id, principal_id, operation, purpose, "
                "request_id, correlation_id, trace_id, granted_authority_json, "
                "outcome_class, error_code, recorded_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 'succeeded', NULL, ?)",
                (
                    run.audit_ref,
                    self.workspace_id,
                    _SERVICE_ACTOR,
                    RUN_OPERATION,
                    "ingestion.sync",
                    _identity("req-sync", *seed),
                    _identity("cor-sync", *seed),
                    _identity("trc-sync", *seed),
                    at_us,
                ),
            )
            self.connection.execute(
                "INSERT INTO omnivia_durable_jobs "
                "(job_id, job_type, state, payload_json, created_at, updated_at, "
                "fencing_generation, claimed_by_service_instance) "
                "VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    RUN_JOB_TYPE,
                    payload,
                    moment,
                    moment,
                    self.fencing_generation,
                    self.identity.service_instance_id,
                ),
            )
            self.connection.execute(
                "INSERT INTO omnivia_job_application_metadata "
                "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
                "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
                "max_attempts) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)",
                (
                    self.workspace_id,
                    run.run_id,
                    RUN_JOB_TYPE,
                    RUN_OPERATION,
                    run.audit_ref,
                    at_us,
                    _MAX_RUN_ATTEMPTS,
                ),
            )
            self.connection.execute(
                "INSERT INTO omnivia_job_attempts "
                "(workspace_id, job_id, attempt_number, started_at_us, state) "
                "VALUES (?, ?, 1, ?, 'running')",
                (self.workspace_id, run.run_id, at_us),
            )
            self.connection.execute(
                "INSERT INTO omnivia_job_events "
                "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
                "VALUES (?, ?, 0, ?, 'running', 'connector synchronisation started')",
                (self.workspace_id, run.run_id, at_us),
            )
            connector_state.register_sync_run(
                self.connection,
                workspace_id=self.workspace_id,
                connector_id=connector_id,
                sync_sequence=sequence,
                run_id=run.run_id,
                source_kind=bound.source_kind,
                state_version=bound.state_version,
                started_at_us=at_us,
            )
        return run

    def _finish_run(
        self, run: _Run, state: str, failure: ConnectorFailure | None
    ) -> None:
        error_json = None if failure is None else to_canonical_json(failure.to_wire())
        with self._fenced():
            at_us = self._not_before(
                run.started_at_us,
                "SELECT COALESCE(MAX(occurred_at_us), 0) FROM omnivia_job_events "
                "WHERE workspace_id = ? AND job_id = ?",
                (self.workspace_id, run.run_id),
            )
            self.connection.execute(
                "UPDATE omnivia_durable_jobs SET state = ?, updated_at = ? "
                "WHERE job_id = ?",
                (state, _iso(at_us), run.run_id),
            )
            self.connection.execute(
                "UPDATE omnivia_job_attempts SET state = ?, finished_at_us = ?, "
                "error_json = ? WHERE workspace_id = ? AND job_id = ? "
                "AND attempt_number = 1",
                (state, at_us, error_json, self.workspace_id, run.run_id),
            )
            sequence = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_job_events "
                "WHERE workspace_id = ? AND job_id = ?",
                (self.workspace_id, run.run_id),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO omnivia_job_events "
                "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.workspace_id,
                    run.run_id,
                    int(sequence),
                    at_us,
                    state,
                    f"connector synchronisation {state}",
                ),
            )

    def _observe_health(
        self, connector: SourceConnector, bound: _Bound
    ) -> HealthState:
        try:
            reported = connector.health()
        except Exception as error:  # noqa: BLE001 -- a health probe that raises is
            # itself the observation; it must not escape and end the run.
            health = SourceHealth(
                state=HealthState.UNAVAILABLE,
                detail=_message(
                    str(error), f"health check raised {type(error).__name__}"
                ),
            )
        else:
            if not isinstance(reported, SourceHealth):
                health = SourceHealth(
                    state=HealthState.UNAVAILABLE,
                    detail="the connector did not report a SourceHealth",
                )
            else:
                # Rebuilt rather than trusted: the state is already a closed
                # enum, but the detail is free text the connector wrote.
                health = SourceHealth(
                    state=reported.state, detail=_redact(reported.detail)
                )
        with self._fenced():
            connector_state.record_health(
                self.connection,
                workspace_id=self.workspace_id,
                connector_id=bound.connector_id,
                health=health,
                observed_at_us=self._not_before(
                    0,
                    "SELECT COALESCE(MAX(observed_at_us), 0) "
                    "FROM omnivia_connector_health_events "
                    "WHERE workspace_id = ? AND connector_id = ?",
                    (self.workspace_id, bound.connector_id),
                ),
            )
        return health.state

    def _resume_cursor(self, bound: _Bound) -> ConnectorCursor | None:
        """The cursor to resume from, or None to synchronise from the beginning.

        A cursor written by an older state version is discarded rather than
        reinterpreted: its token meant something the current version no longer
        promises. A cursor from a *newer* version is refused outright, because
        discarding it would silently re-ingest a source under rules that have
        already moved on.
        """
        cursor = connector_state.read_resume_cursor(
            self.connection,
            workspace_id=self.workspace_id,
            connector_id=bound.connector_id,
        )
        if cursor is None:
            return None
        if cursor.state_version > bound.state_version:
            raise IngestionRefused(
                f"durable cursor for {bound.connector_id!r} is state version "
                f"{cursor.state_version}, ahead of the connector's "
                f"{bound.state_version}"
            )
        if cursor.state_version < bound.state_version:
            return None
        return cursor

    # --- item preparation -----------------------------------------------------

    def _prepare(
        self,
        connector: SourceConnector,
        changes: Sequence[SourceChange],
        is_cancelled: Cancellation,
    ) -> tuple[list[_Prepared], list[DeadLetter], bool]:
        """Fetch, verify and publish the bytes one batch needs.

        Everything here happens outside a transaction. The third element of the
        result says the caller asked to stop, in which case nothing from this
        batch is committed at all -- a half-applied batch would put the cursor
        and the workspace into a relationship neither can describe.
        """
        prepared: list[_Prepared] = []
        letters: list[DeadLetter] = []
        for change in changes:
            if is_cancelled():
                return [], [], True
            if change.change_kind is ChangeKind.DELETED:
                prepared.append(_Prepared(change=change, content_length_bytes=0))
                continue
            outcome = self._materialise(connector, change)
            if isinstance(outcome, DeadLetter):
                letters.append(outcome)
            else:
                prepared.append(outcome)
        return prepared, letters, False

    def _materialise(
        self, connector: SourceConnector, change: SourceChange
    ) -> _Prepared | DeadLetter:
        """Get one item's bytes durable, retrying only what is classed retryable.

        The three ways a source can contradict itself -- too large for the
        capture limit, not the length it declared, not the checksum it declared
        -- are all non-retryable, and for one reason: repeating the fetch cannot
        resolve a disagreement the source is having with itself. Only the source
        changing its mind can, and that arrives as a later change.
        """
        assert change.checksum is not None
        declared_length = change.content_length_bytes
        assert declared_length is not None
        if declared_length > MAX_SOURCE_BYTES:
            return self._gave_up(
                change,
                "content_too_large",
                "the declared content length exceeds the capture size limit",
                attempts=1,
            )

        failure: ConnectorFailure | None = None
        for attempt in range(1, self.max_item_attempts + 1):
            try:
                content = connector.content(change)
            except Exception as error:  # noqa: BLE001 -- one item's fault becomes one
                # classified dead letter, not the end of the batch.
                failure = _failure_of(error)
                if not failure.retryable:
                    return DeadLetter(
                        native_id=change.native_id, failure=failure, attempts=attempt
                    )
                continue

            if not isinstance(content, bytes | bytearray | memoryview):
                return self._gave_up(
                    change,
                    CONTRACT_VIOLATION,
                    "the connector did not return bytes for this item",
                    attempts=attempt,
                )
            content = bytes(content)
            if len(content) > MAX_SOURCE_BYTES:
                return self._gave_up(
                    change,
                    "content_too_large",
                    "the fetched bytes exceed the capture size limit",
                    attempts=attempt,
                )
            if len(content) != declared_length:
                return self._gave_up(
                    change,
                    "content_length_mismatch",
                    "fetched bytes do not match the declared content length",
                    attempts=attempt,
                )
            if f"sha256:{sha256(content).hexdigest()}" != change.checksum:
                return self._gave_up(
                    change,
                    "content_checksum_mismatch",
                    "fetched bytes do not match the declared checksum",
                    attempts=attempt,
                )
            try:
                publish_blob(self.blobs_root, change.checksum, content)
            except (SourceCaptureRefused, OSError) as error:
                failure = _refusal(
                    "blob_publication_failed",
                    _message(str(error), "the blob could not be published"),
                    RETRY_CLASS_RETRYABLE,
                )
                if attempt == self.max_item_attempts:
                    break
                continue
            return _Prepared(change=change, content_length_bytes=len(content))

        assert failure is not None
        return DeadLetter(
            native_id=change.native_id,
            failure=failure,
            attempts=self.max_item_attempts,
        )

    @staticmethod
    def _gave_up(
        change: SourceChange, code: str, message: str, *, attempts: int
    ) -> DeadLetter:
        """One item refused for a reason retrying could not change."""
        return DeadLetter(
            native_id=change.native_id,
            failure=_refusal(code, message, RETRY_CLASS_NON_RETRYABLE),
            attempts=attempts,
        )

    # --- durable application --------------------------------------------------

    def _commit_batch(
        self,
        run: _Run,
        prepared: Sequence[_Prepared],
        letters: Sequence[DeadLetter],
        cursor: ConnectorCursor,
    ) -> tuple[dict[str, int], int]:
        """Apply one whole batch and its cursor in one transaction.

        The checkpoint is written last, and that ordering is the crash
        guarantee: nothing here can leave the cursor ahead of the rows.

        Returns the verdict counts and how many dead letters this batch actually
        wrote, which is not always how many it carried: an item this run already
        gave up on can be reported again on a later page, and the second sighting
        records nothing. Counting rows rather than sightings is what keeps
        `SyncOutcome.dead_lettered` equal to what the run left behind.
        """
        counts = {
            "ingested": 0,
            "unchanged": 0,
            "relabelled": 0,
            "restored": 0,
            "renamed": 0,
            "deleted": 0,
        }
        recorded = 0
        with self._fenced():
            # Strictly after the previous batch of this run, not merely not
            # before it. Two batches of one run can report the same item with
            # different content, and the artifact each produces is told apart by
            # nothing but this instant; a wall clock too coarse to separate them
            # would leave "the latest one" decided by a digest.
            at_us = self._not_before(
                run.started_at_us,
                "SELECT COALESCE(MAX(created_at_us), -1) + 1 "
                "FROM omnivia_job_checkpoints "
                "WHERE workspace_id = ? AND job_id = ?",
                (self.workspace_id, run.run_id),
            )
            for item in prepared:
                counts[self._apply(run, item, at_us)] += 1
            for letter in letters:
                if (
                    connector_state.record_dead_letter(
                        self.connection,
                        workspace_id=self.workspace_id,
                        connector_id=run.connector_id,
                        run_id=run.run_id,
                        dead_letter=letter,
                        recorded_at_us=at_us,
                    )
                    is not None
                ):
                    recorded += 1
            connector_state.write_cursor_checkpoint(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run.run_id,
                attempt_number=1,
                cursor=cursor,
                created_at_us=at_us,
            )
        return counts, recorded

    def _apply(self, run: _Run, item: _Prepared, at_us: int) -> str:
        """Record one prepared change, whatever kind it is.

        A rename is captured *and then* recorded as a move, in that order and
        unconditionally. Splitting the two -- rename here, capture there --
        needs a rule for the case where an item moved and its content changed in
        the same pass, and every version of that rule is a way to lose one of
        the two facts. Capturing first is a no-op when the bytes are already
        held, so the uniform path costs nothing when nothing changed.
        """
        change = item.change
        if change.change_kind is ChangeKind.DELETED:
            return self._apply_deletion(run, change, at_us)
        captured = self._apply_capture(run, item, at_us)
        if change.change_kind is ChangeKind.RENAMED:
            moved = self._apply_rename(run, change, at_us, fresh=captured == "ingested")
            return moved if moved == "renamed" else captured
        return captured

    def _apply_capture(self, run: _Run, item: _Prepared, at_us: int) -> str:
        change = item.change
        assert change.checksum is not None
        assert change.media_type is not None
        seed = (self.workspace_id, run.connector_id, change.native_id, change.checksum)
        evidence_id = _identity("evd", *seed)
        if self._row_exists(
            "SELECT 1 FROM omnivia_evidence_artifacts WHERE evidence_id = ?",
            (evidence_id,),
        ):
            return self._reconcile_known(run, change, evidence_id, at_us)

        digest = change.checksum
        metadata = to_canonical_json(
            {
                "connector_id": run.connector_id,
                "native_id": change.native_id,
                "locator": change.locator,
                "source_version": change.source_version,
            }
        )
        metadata_digest = f"sha256:{sha256(metadata.encode('utf-8')).hexdigest()}"
        staged_ref = _identity("stg", *seed)

        if not self._row_exists(
            "SELECT 1 FROM omnivia_blob_objects WHERE workspace_id = ? "
            "AND content_digest = ?",
            (self.workspace_id, digest),
        ):
            self.connection.execute(
                "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
                "content_length_bytes, created_at_us, verified_at_us) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.workspace_id, digest, item.content_length_bytes, at_us, at_us),
            )
        self.connection.execute(
            "INSERT INTO omnivia_blob_integrity_events "
            "(integrity_event_id, workspace_id, content_digest, integrity_sequence, "
            "outcome, observed_digest, observed_length_bytes, expected_length_bytes, "
            "inventory_id, checked_at_us) "
            "VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, NULL, ?)",
            (
                _identity("bie", *seed),
                self.workspace_id,
                digest,
                self._next(
                    "SELECT COALESCE(MAX(integrity_sequence), 0) + 1 "
                    "FROM omnivia_blob_integrity_events "
                    "WHERE workspace_id = ? AND content_digest = ?",
                    (self.workspace_id, digest),
                ),
                digest,
                item.content_length_bytes,
                item.content_length_bytes,
                at_us,
            ),
        )
        if not self._row_exists(
            "SELECT 1 FROM omnivia_staged_sources WHERE staged_source_ref = ?",
            (staged_ref,),
        ):
            self.connection.execute(
                "INSERT INTO omnivia_staged_sources "
                "(staged_source_ref, workspace_id, source_kind, declared_checksum, "
                "content_length_bytes, media_type, source_version, computed_checksum, "
                "original_metadata_json, original_metadata_digest, staging_outcome, "
                "blob_workspace_id, blob_content_digest, recorded_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)",
                (
                    staged_ref,
                    self.workspace_id,
                    run.source_kind,
                    digest,
                    item.content_length_bytes,
                    change.media_type,
                    change.source_version,
                    digest,
                    metadata,
                    metadata_digest,
                    self.workspace_id,
                    digest,
                    at_us,
                ),
            )
        self.connection.execute(
            "INSERT INTO omnivia_evidence_artifacts "
            "(evidence_id, workspace_id, source_kind, source_native_id, "
            "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
            "ingested_at_us, recorded_at_us, content_checksum, blob_content_digest, "
            "media_type, original_metadata_json, original_metadata_digest, "
            "sensitivity, parser_status, ingestion_status, staged_source_ref, "
            "import_run_id) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'private', 'not_parsed', 'ingested', ?, ?)",
            (
                evidence_id,
                self.workspace_id,
                run.source_kind,
                change.native_id,
                change.locator,
                at_us,
                at_us,
                at_us,
                at_us,
                digest,
                digest,
                change.media_type,
                metadata,
                metadata_digest,
                staged_ref,
                run.run_id,
            ),
        )
        self._append_provenance(
            prefix="prv",
            seed=seed,
            evidence_id=evidence_id,
            source_kind=run.source_kind,
            native_id=change.native_id,
            action="source.ingested",
            at_us=at_us,
            ingestion_status="ingested",
            tombstoned=0,
        )
        self._reconcile_labels(evidence_id, change.permission_labels, at_us)
        return "ingested"

    def _reconcile_known(
        self, run: _Run, change: SourceChange, evidence_id: str, at_us: int
    ) -> str:
        """Bring an artifact this connector already holds up to what was reported.

        The bytes are identical -- that is what finding the artifact by content
        identity means -- so nothing about the artifact changes. What can still
        have changed is what is *true about* it: whether the source still has it,
        and who may see it. Both are streams, so both are brought current by
        appending, and appending nothing is exactly what "nothing changed" looks
        like. That is why a replay of this path is free rather than merely
        harmless.
        """
        verdict = "unchanged"
        if not self._is_present(evidence_id):
            self._append_provenance(
                prefix="prvi",
                seed=(
                    self.workspace_id,
                    run.connector_id,
                    change.native_id,
                    evidence_id,
                ),
                evidence_id=evidence_id,
                source_kind=run.source_kind,
                native_id=change.native_id,
                action="source.ingested",
                at_us=at_us,
                ingestion_status="ingested",
                tombstoned=0,
            )
            verdict = "restored"
        relabelled = self._reconcile_labels(
            evidence_id, change.permission_labels, at_us
        )
        # A restoration that also changed the ACL is still one change, and the
        # reappearance is the larger fact; the label stream records the rest.
        if relabelled and verdict == "unchanged":
            verdict = "relabelled"
        return verdict

    def _reconcile_labels(
        self, evidence_id: str, declared: Sequence[str], at_us: int
    ) -> bool:
        """Append whatever it takes for the label stream to say `declared`.

        The stream is the authority and stays the only one: the current label
        set is folded out of it, compared with what the source now reports, and
        the difference is appended as `withdrawn` then `attached` in ascending
        label order. Identical input therefore appends nothing, which is what
        makes an ACL replay idempotent without a second record of "current".
        """
        current = self._current_labels(evidence_id)
        target = set(declared)
        pending = [("withdrawn", label) for label in sorted(current - target)]
        pending += [("attached", label) for label in sorted(target - current)]
        if not pending:
            return False
        sequence = self._next(
            "SELECT COALESCE(MAX(label_sequence), 0) + 1 "
            "FROM omnivia_evidence_permission_labels WHERE evidence_id = ?",
            (evidence_id,),
        )
        for action, label in pending:
            self.connection.execute(
                "INSERT INTO omnivia_evidence_permission_labels "
                "(label_event_id, evidence_id, workspace_id, label_sequence, "
                "label_action, permission_label, recorded_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _identity("lbl", evidence_id, action, label, str(sequence)),
                    evidence_id,
                    self.workspace_id,
                    sequence,
                    action,
                    label,
                    at_us,
                ),
            )
            sequence += 1
        return True

    def _apply_rename(
        self, run: _Run, change: SourceChange, at_us: int, *, fresh: bool
    ) -> str:
        """Record that a known item moved, against its most recent artifact.

        The capture that always precedes this guarantees the artifact exists,
        which is why there is no "renamed something we never saw" branch here.

        Whether the move still needs recording is decided from where the item is
        *now*, never from whether an event with this shape exists: the same item
        can legitimately move A to B, back to A and to B again, and an identity
        derived from the pair of locators alone would read the third move as a
        duplicate of the first. The rule is exactly this and no wider: a move
        whose `previous_locator` is not the current locator has nothing to apply
        and is a no-op; otherwise it is applied. That is applicability, not
        staleness -- a replayed A-to-B arriving after the item cycled back to A
        is indistinguishable from a fresh A-to-B, and is applied. Last delivered
        writer wins. A capture that just created the artifact is
        the exception -- the artifact was born at the destination, so the move is
        recorded to keep the trajectory rather than inferred from a locator that
        was never `previous_locator`.
        """
        known = self._latest_evidence(run.connector_id, change.native_id)
        assert known is not None
        evidence_id, source_kind = known
        if not fresh and self._current_locator(evidence_id) != change.previous_locator:
            return "unchanged"
        event_id = self._append_provenance(
            prefix="prvr",
            seed=(
                self.workspace_id,
                run.connector_id,
                change.native_id,
                change.previous_locator or "",
                change.locator or "",
            ),
            evidence_id=evidence_id,
            source_kind=source_kind,
            native_id=change.native_id,
            action="source.renamed",
            at_us=at_us,
            ingestion_status=None,
            tombstoned=None,
        )
        # The artifact is append-only, so its `source_locator` is where the item
        # was at capture and stays that way. The current locator is this
        # reference: the rename event's own record of where the item moved to.
        self.connection.execute(
            "INSERT INTO omnivia_evidence_event_references "
            "(event_reference_id, provenance_event_id, evidence_id, workspace_id, "
            "reference_ordinal, source_kind, source_native_id, source_locator, "
            "source_retrieved_at_us, span_pointer, span_start_offset, "
            "span_end_offset, excerpt) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (
                _identity("evr", event_id),
                event_id,
                evidence_id,
                self.workspace_id,
                source_kind,
                change.native_id,
                change.locator,
                at_us,
            ),
        )
        return "renamed"

    def _apply_deletion(self, run: _Run, change: SourceChange, at_us: int) -> str:
        """Record that a known item is gone from its source.

        A deletion at the source is a tombstone observation, never a DELETE:
        the artifact and its bytes stay exactly as captured, and what changes is
        what the provenance stream says is true about them now. Whether it needs
        recording is read from the stream's *head* rather than from whether a
        tombstone has ever been written, so an item that was deleted, reappeared
        and was deleted again is tombstoned again -- and every one of those
        observations is still there.
        """
        known = self._latest_evidence(run.connector_id, change.native_id)
        if known is None:
            return "unchanged"
        evidence_id, source_kind = known
        if not self._is_present(evidence_id):
            return "unchanged"
        self._append_provenance(
            prefix="prvd",
            seed=(
                self.workspace_id,
                run.connector_id,
                change.native_id,
                evidence_id,
            ),
            evidence_id=evidence_id,
            source_kind=source_kind,
            native_id=change.native_id,
            action="source.deleted",
            at_us=at_us,
            ingestion_status="tombstoned",
            tombstoned=1,
        )
        return "deleted"

    # --- small durable helpers ------------------------------------------------

    def _fenced(self) -> AbstractContextManager[sqlite3.Connection]:
        return fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        )

    def _not_before(
        self, floor: int, sql: str, parameters: tuple[object, ...]
    ) -> int:
        """Now, but never behind a durable instant the schema orders against.

        Several relations here require a new row's timestamp not to regress
        against the previous one, so a wall clock that steps backwards -- an NTP
        correction mid-run is enough -- would abort an otherwise valid commit.
        Read inside the transaction, because the value being clamped against is
        the one this statement is about to be compared with.
        """
        row = self.connection.execute(sql, parameters).fetchone()
        assert row is not None
        return max(_now_us(self.clock), floor, int(row[0]))

    def _row_exists(self, sql: str, parameters: tuple[object, ...]) -> bool:
        return self.connection.execute(sql, parameters).fetchone() is not None

    def _next(self, sql: str, parameters: tuple[object, ...]) -> int:
        row = self.connection.execute(sql, parameters).fetchone()
        assert row is not None
        return int(row[0])

    def _latest_evidence(
        self, connector_id: str, native_id: str
    ) -> tuple[str, str] | None:
        """This connector's most recent artifact for one source-native id.

        Scoped through the run binding rather than by source kind alone: two
        connectors of the same kind may legitimately use the same native id, and
        one must never observe a rename or a deletion against the other's
        evidence.

        The order leads with the run's `sync_sequence`, which is contiguous and
        per connector, so "most recent" is decided by a real total order rather
        than by a timestamp two runs could share. Within one run the capture
        instants are strictly increasing per batch, and a batch reports each item
        at most once, so no tie survives to reach the digest at the end.
        """
        row = self.connection.execute(
            "SELECT a.evidence_id, a.source_kind FROM omnivia_evidence_artifacts a "
            "JOIN omnivia_connector_sync_runs r "
            "  ON r.workspace_id = a.workspace_id AND r.run_id = a.import_run_id "
            "WHERE a.workspace_id = ? AND r.connector_id = ? "
            "AND a.source_native_id = ? "
            "ORDER BY r.sync_sequence DESC, a.ingested_at_us DESC, "
            "a.evidence_id DESC LIMIT 1",
            (self.workspace_id, connector_id, native_id),
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def _is_present(self, evidence_id: str) -> bool:
        """Whether the source still has this artifact, per the stream's head.

        The head is the most recent event that said anything about presence at
        all; events that say nothing (a rename) leave the answer alone. An
        artifact nothing has ever observed either way is present, which is the
        only reading that makes an artifact captured outside this path -- local
        capture, say -- mean what it plainly means.
        """
        row = self.connection.execute(
            "SELECT tombstoned_observation FROM omnivia_evidence_provenance_events "
            "WHERE evidence_id = ? AND workspace_id = ? "
            "AND tombstoned_observation IS NOT NULL "
            "ORDER BY provenance_sequence DESC LIMIT 1",
            (evidence_id, self.workspace_id),
        ).fetchone()
        return row is None or int(row[0]) == 0

    def _current_locator(self, evidence_id: str) -> str | None:
        """Where the item is now: its latest move, or where it was captured."""
        row = self.connection.execute(
            "SELECT r.source_locator FROM omnivia_evidence_event_references r "
            "JOIN omnivia_evidence_provenance_events p "
            "  ON p.provenance_event_id = r.provenance_event_id "
            "WHERE p.evidence_id = ? AND p.workspace_id = ? "
            "AND p.action = 'source.renamed' "
            "ORDER BY p.provenance_sequence DESC, r.reference_ordinal DESC LIMIT 1",
            (evidence_id, self.workspace_id),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT source_locator FROM omnivia_evidence_artifacts "
                "WHERE evidence_id = ? AND workspace_id = ?",
                (evidence_id, self.workspace_id),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _current_labels(self, evidence_id: str) -> set[str]:
        """The labels the stream currently says this artifact carries.

        Folded in `label_sequence` order, which is the tie-break the schema
        itself names: an `attached` and a `withdrawn` in the same microsecond are
        ordered by sequence and nothing else. `corrected` is a superseding
        attachment, so anything that is not a withdrawal leaves the label on.
        """
        labels: set[str] = set()
        for label, action in self.connection.execute(
            "SELECT permission_label, label_action "
            "FROM omnivia_evidence_permission_labels "
            "WHERE evidence_id = ? AND workspace_id = ? ORDER BY label_sequence",
            (evidence_id, self.workspace_id),
        ).fetchall():
            if str(action) == "withdrawn":
                labels.discard(str(label))
            else:
                labels.add(str(label))
        return labels

    def _append_provenance(
        self,
        *,
        prefix: str,
        seed: tuple[str, ...],
        evidence_id: str,
        source_kind: str,
        native_id: str,
        action: str,
        at_us: int,
        ingestion_status: str | None,
        tombstoned: int | None,
    ) -> str:
        """Append one provenance event and return the identity it was given.

        The event's position in its own stream is part of its identity. That is
        what lets a fact recur -- deleted, restored, deleted again -- without the
        second occurrence colliding with the first, while a replay still writes
        nothing, because a replay never gets here: the callers decide from
        current state whether there is anything to append.
        """
        sequence = self._next(
            "SELECT COALESCE(MAX(provenance_sequence), 0) + 1 "
            "FROM omnivia_evidence_provenance_events WHERE evidence_id = ?",
            (evidence_id,),
        )
        provenance_event_id = _identity(prefix, *seed, str(sequence))
        self.connection.execute(
            "INSERT INTO omnivia_evidence_provenance_events "
            "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, "
            "actor_id, actor_kind, action, occurred_at_us, reason_code, "
            "reason_comment, parser_status, ingestion_status, tombstoned_observation, "
            "source_kind, source_native_id, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, NULL)",
            (
                provenance_event_id,
                evidence_id,
                self.workspace_id,
                sequence,
                _SERVICE_ACTOR,
                _SERVICE_ACTOR_KIND,
                action,
                at_us,
                ingestion_status,
                tombstoned,
                source_kind,
                native_id,
            ),
        )
        return provenance_event_id


@dataclass(frozen=True, slots=True)
class _Run:
    """The durable identity of one synchronisation run."""

    run_id: str
    audit_ref: str
    connector_id: str
    source_kind: str
    sync_sequence: int
    started_at_us: int


__all__ = [
    "CONTRACT_VIOLATION",
    "RUN_JOB_TYPE",
    "RUN_OPERATION",
    "IngestionCoordinator",
    "IngestionRefused",
]
