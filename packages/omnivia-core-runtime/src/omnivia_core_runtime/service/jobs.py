"""Durable job queue (B9).

Every state change runs inside a fenced transaction, so a job cannot be claimed,
completed or retried by a service that no longer holds the workspace. Claiming in
particular is a read-then-write, which is the shape that goes wrong under
concurrency: two schedulers reading the same queued row and both claiming it. The
claim is therefore a conditional update inside `BEGIN IMMEDIATE`, and the row is
claimed only if it is still queued when the write happens.

Retry is bounded and records why. An unbounded retry turns a permanently failing job
into an infinite loop that looks like activity, which is worse than a job that stops
and says what happened.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from enum import Enum
from typing import Any

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

DEFAULT_MAX_ATTEMPTS = 3


class JobState(str, Enum):
    """Terminal and non-terminal job states."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobError(StorageError):
    """A job could not be enqueued or transitioned."""


@dataclass(frozen=True)
class Job:
    """One durable job."""

    job_id: str
    job_type: str
    state: str
    payload: dict[str, Any]
    attempts: int
    fencing_generation: int | None
    claimed_by_service_instance: str | None
    last_error: str | None = None

    @property
    def terminal(self) -> bool:
        return JobState(self.state).terminal


def _row_to_job(row: tuple[Any, ...]) -> Job:
    payload: dict[str, Any] = {}
    attempts = 0
    last_error: str | None = None
    if row[3]:
        try:
            decoded = json.loads(str(row[3]))
        except ValueError:
            decoded = {}
        if isinstance(decoded, dict):
            payload = decoded.get("payload", {}) if "payload" in decoded else decoded
            attempts = int(decoded.get("attempts", 0))
            raw_error = decoded.get("last_error")
            last_error = None if raw_error is None else str(raw_error)
    return Job(
        job_id=str(row[0]),
        job_type=str(row[1]),
        state=str(row[2]),
        payload=payload,
        attempts=attempts,
        fencing_generation=None if row[4] is None else int(row[4]),
        claimed_by_service_instance=None if row[5] is None else str(row[5]),
        last_error=last_error,
    )


def _encode(payload: dict[str, Any], attempts: int, last_error: str | None) -> str:
    """Store payload, attempt count and last error in the one JSON column.

    The reserved table has a single `payload_json` column, and adding columns would
    be a schema migration for bookkeeping. Wrapping keeps the substrate stable while
    the queue's own shape can still evolve.
    """
    return json.dumps(
        {"payload": payload, "attempts": attempts, "last_error": last_error},
        sort_keys=True,
    )


_SELECT = (
    "SELECT job_id, job_type, state, payload_json, fencing_generation, "
    "claimed_by_service_instance FROM omnivia_durable_jobs"
)


def read_job(connection: sqlite3.Connection, job_id: str) -> Job | None:
    row = connection.execute(f"{_SELECT} WHERE job_id = ?", (job_id,)).fetchone()
    return None if row is None else _row_to_job(row)


def list_jobs(connection: sqlite3.Connection, *, state: str | None = None) -> list[Job]:
    """Jobs in deterministic id order, optionally filtered by state."""
    if state is None:
        rows = connection.execute(f"{_SELECT} ORDER BY job_id").fetchall()
    else:
        rows = connection.execute(
            f"{_SELECT} WHERE state = ? ORDER BY job_id", (state,)
        ).fetchall()
    return [_row_to_job(row) for row in rows]


@dataclass
class JobQueue:
    """Fenced durable job operations for one owning service instance."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def _fenced(self) -> AbstractContextManager[sqlite3.Connection]:
        return fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        )

    def enqueue(
        self, job_type: str, payload: dict[str, Any] | None = None, *, job_id: str | None = None
    ) -> Job:
        identifier = job_id or f"job-{uuid.uuid4()}"
        moment = self.clock.wall_time().isoformat()
        with self._fenced():
            existing = self.connection.execute(
                "SELECT 1 FROM omnivia_durable_jobs WHERE job_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                raise JobError(f"job {identifier!r} already exists")
            self.connection.execute(
                "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, "
                "payload_json, created_at, updated_at, fencing_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    job_type,
                    JobState.QUEUED.value,
                    _encode(payload or {}, 0, None),
                    moment,
                    moment,
                    self.fencing_generation,
                ),
            )
        job = read_job(self.connection, identifier)
        if job is None:  # pragma: no cover - the transaction committed
            raise JobError("enqueue committed but the job is unreadable")
        return job

    def claim_next(self, *, job_type: str | None = None) -> Job | None:
        """Claim the oldest queued job, or return None.

        The conditional `WHERE state = 'queued'` is what makes this safe: two
        schedulers can read the same row, but only the one whose UPDATE actually
        matches a queued row claims it. Checking state in Python and then writing
        would let both win.
        """
        with self._fenced():
            if job_type is None:
                candidate = self.connection.execute(
                    "SELECT job_id FROM omnivia_durable_jobs WHERE state = ? "
                    "ORDER BY created_at, job_id LIMIT 1",
                    (JobState.QUEUED.value,),
                ).fetchone()
            else:
                candidate = self.connection.execute(
                    "SELECT job_id FROM omnivia_durable_jobs WHERE state = ? "
                    "AND job_type = ? ORDER BY created_at, job_id LIMIT 1",
                    (JobState.QUEUED.value, job_type),
                ).fetchone()
            if candidate is None:
                return None

            identifier = str(candidate[0])
            cursor = self.connection.execute(
                "UPDATE omnivia_durable_jobs SET state = ?, "
                "claimed_by_service_instance = ?, fencing_generation = ?, updated_at = ? "
                "WHERE job_id = ? AND state = ?",
                (
                    JobState.CLAIMED.value,
                    self.identity.service_instance_id,
                    self.fencing_generation,
                    self.clock.wall_time().isoformat(),
                    identifier,
                    JobState.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                # Another claimant won between the select and the update.
                return None
        return read_job(self.connection, identifier)

    def _transition(
        self,
        job_id: str,
        *,
        to: JobState,
        from_states: tuple[JobState, ...],
        attempts: int | None = None,
        last_error: str | None = None,
        clear_claim: bool = False,
    ) -> Job:
        with self._fenced():
            current = read_job(self.connection, job_id)
            if current is None:
                raise JobError(f"no job {job_id!r}")
            if JobState(current.state) not in from_states:
                raise JobError(
                    f"job {job_id!r} is {current.state!r}; expected one of "
                    f"{[s.value for s in from_states]}"
                )
            self.connection.execute(
                "UPDATE omnivia_durable_jobs SET state = ?, payload_json = ?, "
                "updated_at = ?, claimed_by_service_instance = ? WHERE job_id = ?",
                (
                    to.value,
                    _encode(
                        current.payload,
                        current.attempts if attempts is None else attempts,
                        last_error,
                    ),
                    self.clock.wall_time().isoformat(),
                    None if clear_claim else current.claimed_by_service_instance,
                    job_id,
                ),
            )
        job = read_job(self.connection, job_id)
        if job is None:  # pragma: no cover
            raise JobError("transition committed but the job is unreadable")
        return job

    def complete(self, job_id: str) -> Job:
        return self._transition(
            job_id, to=JobState.SUCCEEDED, from_states=(JobState.CLAIMED,)
        )

    def cancel(self, job_id: str) -> Job:
        """Cancel a job that has not reached a terminal state."""
        return self._transition(
            job_id,
            to=JobState.CANCELLED,
            from_states=(JobState.QUEUED, JobState.CLAIMED),
            clear_claim=True,
        )

    def fail(self, job_id: str, error: str) -> Job:
        """Record a failed attempt, requeueing until the attempt budget is spent.

        The job goes to FAILED rather than back to QUEUED once attempts are
        exhausted, so a permanently failing job stops instead of cycling forever and
        looking like progress.
        """
        current = read_job(self.connection, job_id)
        if current is None:
            raise JobError(f"no job {job_id!r}")
        attempts = current.attempts + 1
        exhausted = attempts >= self.max_attempts
        return self._transition(
            job_id,
            to=JobState.FAILED if exhausted else JobState.QUEUED,
            from_states=(JobState.CLAIMED,),
            attempts=attempts,
            last_error=error,
            clear_claim=not exhausted,
        )

    def recover_stranded(self) -> list[str]:
        """Requeue jobs claimed under an older generation.

        Their claimant cannot still be running: a newer generation exists, so the
        holder has been superseded. Returns the ids requeued.
        """
        with self._fenced():
            stranded = [
                str(row[0])
                for row in self.connection.execute(
                    "SELECT job_id FROM omnivia_durable_jobs WHERE state = ? "
                    "AND COALESCE(fencing_generation, 0) < ? ORDER BY job_id",
                    (JobState.CLAIMED.value, self.fencing_generation),
                ).fetchall()
            ]
            if stranded:
                self.connection.execute(
                    "UPDATE omnivia_durable_jobs SET state = ?, "
                    "claimed_by_service_instance = NULL, updated_at = ? "
                    "WHERE state = ? AND COALESCE(fencing_generation, 0) < ?",
                    (
                        JobState.QUEUED.value,
                        self.clock.wall_time().isoformat(),
                        JobState.CLAIMED.value,
                        self.fencing_generation,
                    ),
                )
        return stranded


def _application_timestamp(clock: Clock) -> tuple[int, str]:
    moment = clock.wall_time()
    return int(moment.timestamp() * 1_000_000), moment.isoformat()


def _application_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> dict[str, object]:
    from omnivia_core_runtime.storage.jobs import read_application_job_snapshot

    snapshot = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    if snapshot is None:
        raise JobError(f"no application job {job_id!r}")
    return snapshot


def _next_application_number(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    *,
    workspace_id: str,
    job_id: str,
    base: int,
) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX({column}), ?) + 1 FROM {table} "
        "WHERE workspace_id = ? AND job_id = ?",
        (base, workspace_id, job_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def claim_application_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    clock: Clock,
    job_id: str | None = None,
) -> dict[str, object] | None:
    """Claim one queued application job and append its next running attempt/event."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        if job_id is None:
            row = connection.execute(
                "SELECT j.job_id FROM omnivia_durable_jobs j "
                "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
                "WHERE m.workspace_id = ? AND j.state = 'queued' "
                "ORDER BY j.created_at, j.job_id LIMIT 1",
                (workspace_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT j.job_id FROM omnivia_durable_jobs j "
                "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
                "WHERE m.workspace_id = ? AND j.job_id = ? AND j.state = 'queued'",
                (workspace_id, job_id),
            ).fetchone()
        if row is None:
            return None
        identifier = str(row[0])
        now_us, moment = _application_timestamp(clock)
        updated = connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'claimed', updated_at = ?, "
            "claimed_by_service_instance = ?, fencing_generation = ? "
            "WHERE job_id = ? AND state = 'queued'",
            (
                moment,
                identity.service_instance_id,
                fencing_generation,
                identifier,
            ),
        )
        if updated.rowcount != 1:
            return None
        attempt = _next_application_number(
            connection,
            "omnivia_job_attempts",
            "attempt_number",
            workspace_id=workspace_id,
            job_id=identifier,
            base=0,
        )
        connection.execute(
            "INSERT INTO omnivia_job_attempts "
            "(workspace_id, job_id, attempt_number, started_at_us, state) "
            "VALUES (?, ?, ?, ?, 'running')",
            (workspace_id, identifier, attempt, now_us),
        )
        sequence = _next_application_number(
            connection,
            "omnivia_job_events",
            "sequence",
            workspace_id=workspace_id,
            job_id=identifier,
            base=-1,
        )
        connection.execute(
            "INSERT INTO omnivia_job_events "
            "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
            "VALUES (?, ?, ?, ?, 'running', 'job execution started')",
            (workspace_id, identifier, sequence, now_us),
        )
    return _application_snapshot(
        connection, workspace_id=workspace_id, job_id=identifier
    )


def _terminalize_application_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    job_id: str,
    fencing_generation: int,
    clock: Clock,
    state: str,
    result_kind: str | None = None,
    result: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
    cancellation_reason: str | None = None,
    _transaction_open: bool = False,
) -> dict[str, object]:
    transaction: AbstractContextManager[sqlite3.Connection]
    if _transaction_open:
        transaction = nullcontext(connection)
    else:
        transaction = fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=fencing_generation,
        )
    with transaction:
        current = connection.execute(
            "SELECT j.state FROM omnivia_durable_jobs j "
            "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
            "WHERE m.workspace_id = ? AND j.job_id = ?",
            (workspace_id, job_id),
        ).fetchone()
        if current is None:
            raise JobError(f"no application job {job_id!r}")
        if str(current[0]) != "claimed":
            raise JobError(f"application job {job_id!r} is not running")
        now_us, moment = _application_timestamp(clock)
        connection.execute(
            "UPDATE omnivia_durable_jobs SET state = ?, updated_at = ?, "
            "claimed_by_service_instance = NULL, fencing_generation = ? "
            "WHERE job_id = ?",
            (state, moment, fencing_generation, job_id),
        )
        error_json = None if error is None else _encode_document(error)
        connection.execute(
            "UPDATE omnivia_job_attempts SET state = ?, finished_at_us = ?, "
            "error_json = ? WHERE workspace_id = ? AND job_id = ? "
            "AND attempt_number = (SELECT MAX(a.attempt_number) "
            "FROM omnivia_job_attempts a WHERE a.workspace_id = ? AND a.job_id = ?) "
            "AND state = 'running'",
            (
                state,
                now_us,
                error_json,
                workspace_id,
                job_id,
                workspace_id,
                job_id,
            ),
        )
        sequence = _next_application_number(
            connection,
            "omnivia_job_events",
            "sequence",
            workspace_id=workspace_id,
            job_id=job_id,
            base=-1,
        )
        connection.execute(
            "INSERT INTO omnivia_job_events "
            "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (workspace_id, job_id, sequence, now_us, state, f"job {state}"),
        )
        observation = _next_application_number(
            connection,
            "omnivia_job_terminal_observations",
            "terminal_observation_number",
            workspace_id=workspace_id,
            job_id=job_id,
            base=0,
        )
        attempt_row = connection.execute(
            "SELECT MAX(attempt_number) FROM omnivia_job_attempts "
            "WHERE workspace_id = ? AND job_id = ?",
            (workspace_id, job_id),
        ).fetchone()
        attempt = None if attempt_row is None else attempt_row[0]
        connection.execute(
            "INSERT INTO omnivia_job_terminal_observations "
            "(workspace_id, job_id, terminal_observation_number, attempt_number, "
            "terminal_state, finished_at_us, result_kind, result_json, error_json, "
            "cancellation_reason, provenance_kind, fencing_generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'service_committed', ?)",
            (
                workspace_id,
                job_id,
                observation,
                attempt,
                state,
                now_us,
                result_kind,
                None if result is None else _encode_document(result),
                error_json,
                cancellation_reason,
                fencing_generation,
            ),
        )
    return _application_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )


def _encode_document(value: Mapping[str, object]) -> str:
    from omnivia_core.contracts.v1 import to_canonical_json

    return to_canonical_json(dict(value))


def acknowledge_application_job_cancellation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    job_id: str,
    fencing_generation: int,
    clock: Clock,
    reason: str = "requested",
) -> dict[str, object]:
    """Acknowledge a pending cancellation and preserve its terminal observation."""
    current = read_job(connection, job_id)
    if current is not None and current.state == JobState.QUEUED.value:
        with fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=fencing_generation,
        ):
            pending = connection.execute(
                "SELECT 1 FROM omnivia_application_job_controls "
                "WHERE workspace_id = ? AND job_id = ? "
                "AND operation = 'job.cancel' "
                "AND disposition = 'cancellation_requested' "
                "AND resulting_state = 'queued' "
                "ORDER BY settled_at_us DESC LIMIT 1",
                (workspace_id, job_id),
            ).fetchone()
            if pending is None:
                raise JobError(f"application job {job_id!r} has no pending cancellation")
            now_us, moment = _application_timestamp(clock)
            connection.execute(
                "UPDATE omnivia_durable_jobs SET state = 'claimed', updated_at = ?, "
                "claimed_by_service_instance = ?, fencing_generation = ? "
                "WHERE job_id = ? AND state = 'queued'",
                (
                    moment,
                    identity.service_instance_id,
                    fencing_generation,
                    job_id,
                ),
            )
            attempt = _next_application_number(
                connection,
                "omnivia_job_attempts",
                "attempt_number",
                workspace_id=workspace_id,
                job_id=job_id,
                base=0,
            )
            connection.execute(
                "INSERT INTO omnivia_job_attempts "
                "(workspace_id, job_id, attempt_number, started_at_us, state) "
                "VALUES (?, ?, ?, ?, 'running')",
                (workspace_id, job_id, attempt, now_us),
            )
            sequence = _next_application_number(
                connection,
                "omnivia_job_events",
                "sequence",
                workspace_id=workspace_id,
                job_id=job_id,
                base=-1,
            )
            connection.execute(
                "INSERT INTO omnivia_job_events "
                "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
                "VALUES (?, ?, ?, ?, 'running', 'cancellation acknowledgement started')",
                (workspace_id, job_id, sequence, now_us),
            )
            return _terminalize_application_job(
                connection,
                identity,
                workspace_id=workspace_id,
                job_id=job_id,
                fencing_generation=fencing_generation,
                clock=clock,
                state="cancelled",
                cancellation_reason=reason,
                _transaction_open=True,
            )
    return _terminalize_application_job(
        connection,
        identity,
        workspace_id=workspace_id,
        job_id=job_id,
        fencing_generation=fencing_generation,
        clock=clock,
        state="cancelled",
        cancellation_reason=reason,
    )


def complete_application_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    job_id: str,
    fencing_generation: int,
    clock: Clock,
    result_kind: str,
    result: Mapping[str, object],
) -> dict[str, object]:
    """Commit a successful application-job attempt and terminal observation."""
    return _terminalize_application_job(
        connection,
        identity,
        workspace_id=workspace_id,
        job_id=job_id,
        fencing_generation=fencing_generation,
        clock=clock,
        state="succeeded",
        result_kind=result_kind,
        result=result,
    )


def fail_application_job(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    job_id: str,
    fencing_generation: int,
    clock: Clock,
    error: Mapping[str, object],
) -> dict[str, object]:
    """Commit a failed application-job attempt and terminal observation."""
    return _terminalize_application_job(
        connection,
        identity,
        workspace_id=workspace_id,
        job_id=job_id,
        fencing_generation=fencing_generation,
        clock=clock,
        state="failed",
        error=error,
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Job",
    "JobError",
    "JobQueue",
    "JobState",
    "acknowledge_application_job_cancellation",
    "claim_application_job",
    "complete_application_job",
    "fail_application_job",
    "list_jobs",
    "read_job",
]
