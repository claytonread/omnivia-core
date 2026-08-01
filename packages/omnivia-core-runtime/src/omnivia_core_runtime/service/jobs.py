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
from contextlib import AbstractContextManager
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


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Job",
    "JobError",
    "JobQueue",
    "JobState",
    "list_jobs",
    "read_job",
]
