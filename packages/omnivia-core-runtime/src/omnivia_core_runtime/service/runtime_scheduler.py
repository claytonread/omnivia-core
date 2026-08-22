"""Adapt the existing fenced durable-job queue to canonical runtime claims (RT-106).

The scheduler owns no second queue.  It selects a canonical Run/RunStep through the
job already bound to that Run, claims the mutable ``omnivia_durable_jobs`` row through
the existing application-job seam, then appends the runtime Attempt, step state and
event in the same fenced transaction.  Either both histories describe the claim, or
neither does.

This slice deliberately models the Core service's scheduling claim.  Worker-protocol
leases and their independent expiry belong to the WorkerAdapter slice (RT-108); the
authority here is the existing workspace lease plus its monotonic fencing generation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STEP_TERMINAL_STATUSES,
    RUN_TERMINAL_STATUSES,
    ApiError,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.jobs import (
    JobState,
    _claim_application_job_locked,
    _terminalize_application_job,
    read_job,
)
from omnivia_core_runtime.storage.agent_runtime import (
    read_run_id_by_job,
    read_run_sequence,
    read_run_steps,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.jobs import _recover_stranded_application_jobs_locked

_QUEUED_RUNTIME_JOBS: Final = (
    "SELECT j.job_id, r.run_id FROM omnivia_durable_jobs j "
    "JOIN omnivia_runtime_runs r ON r.workspace_id = ? AND r.job_id = j.job_id "
    "WHERE j.state = 'queued' ORDER BY j.created_at, j.job_id"
)

_LATEST_RUN_STATUS: Final = (
    "SELECT run_status FROM omnivia_runtime_events "
    "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 1"
)

_FIRST_RUNNABLE_STEP: Final = (
    "SELECT s.run_step_id FROM omnivia_runtime_run_steps s "
    "WHERE s.workspace_id = ? AND s.run_id = ? "
    "AND (SELECT status FROM omnivia_runtime_run_step_states "
    "     WHERE workspace_id = s.workspace_id AND run_step_id = s.run_step_id "
    "     ORDER BY state_sequence DESC LIMIT 1) = 'pending' "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM omnivia_runtime_attempts a "
    "  LEFT JOIN omnivia_runtime_attempt_outcomes o "
    "    ON o.workspace_id = a.workspace_id AND o.attempt_id = a.attempt_id "
    "  WHERE a.workspace_id = s.workspace_id AND a.run_step_id = s.run_step_id "
    "    AND o.attempt_id IS NULL) "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM omnivia_runtime_waits w "
    "  LEFT JOIN omnivia_runtime_wait_resolutions wr "
    "    ON wr.workspace_id = w.workspace_id AND wr.wait_id = w.wait_id "
    "  WHERE w.workspace_id = s.workspace_id AND w.run_step_id = s.run_step_id "
    "    AND wr.wait_id IS NULL) "
    "ORDER BY s.ordinal, s.run_step_id LIMIT 1"
)

_NEXT_RUNTIME_ATTEMPT_NUMBER: Final = (
    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM omnivia_runtime_attempts "
    "WHERE workspace_id = ? AND run_step_id = ?"
)


class RuntimeSchedulingError(StorageError):
    """A selected runtime-bound job has corrupt or contradictory history."""


@dataclass(frozen=True, slots=True)
class RuntimeClaim:
    """The exact durable-job and runtime-attempt lineage opened by one claim."""

    workspace_id: str
    run_id: str
    job_id: str
    run_step_id: str
    runtime_attempt_id: str
    runtime_attempt_number: int
    application_attempt_number: int
    service_instance_id: str
    fencing_generation: int
    claimed_at_us: int


@dataclass(frozen=True, slots=True)
class RuntimeRecovery:
    """One runtime-bound job recovered from a superseded fence."""

    job_id: str
    run_id: str
    run_step_id: str
    runtime_attempt_id: str
    application_attempt_number: int
    requeued: bool


def _lineage_id(*parts: str) -> str:
    """Return a bounded canonical Identifier derived only from persisted lineage."""
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass
class RuntimeScheduler:
    """Fenced scheduler adapter for one owning Core service instance."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock

    def claim_next(self) -> RuntimeClaim | None:
        """Atomically claim the oldest runnable runtime-bound durable job.

        ``None`` is the ordinary polling result when no pending step is runnable.
        Corrupt canonical history raises instead of being silently treated as blocked.
        """
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            selected = self._select_claimable()
            if selected is None:
                return None
            job_id, run_id, run_step_id = selected
            claimed = _claim_application_job_locked(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                clock=self.clock,
                job_id=job_id,
            )
            if claimed is None:  # pragma: no cover - BEGIN IMMEDIATE excludes a race
                raise RuntimeSchedulingError(
                    f"job {job_id!r} was selected but could not be claimed"
                )

            attempt_row = self.connection.execute(
                _NEXT_RUNTIME_ATTEMPT_NUMBER, (self.workspace_id, run_step_id)
            ).fetchone()
            if attempt_row is None:  # pragma: no cover - aggregate always returns
                raise RuntimeSchedulingError("runtime attempt allocation returned no row")
            runtime_attempt_number = int(attempt_row[0])
            runtime_attempt_id = _lineage_id(
                "runtime_attempt",
                self.workspace_id,
                run_id,
                run_step_id,
                str(runtime_attempt_number),
                job_id,
            )
            event_sequence = (
                read_run_sequence(
                    self.connection, workspace_id=self.workspace_id, run_id=run_id
                )
                + 1
            )
            runtime_event_id = _lineage_id(
                "runtime_event",
                self.workspace_id,
                run_id,
                str(event_sequence),
                run_step_id,
                job_id,
            )

            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.record_step_status(
                run_step_id=run_step_id,
                status="running",
                observed_at_us=claimed.now_us,
            )
            writer.start_attempt(
                attempt_id=runtime_attempt_id,
                run_id=run_id,
                run_step_id=run_step_id,
                attempt_number=runtime_attempt_number,
                started_at_us=claimed.now_us,
            )
            writer.append_run_event(
                run_id=run_id,
                runtime_event_id=runtime_event_id,
                occurred_at_us=claimed.now_us,
                event_kind="attempt_started",
                run_status="running",
                run_step_id=run_step_id,
                message="runtime scheduler claimed the next runnable attempt",
                details={
                    "workspace_id": self.workspace_id,
                    "run_id": run_id,
                    "job_id": job_id,
                    "run_step_id": run_step_id,
                    "runtime_attempt_id": runtime_attempt_id,
                    "runtime_attempt_number": runtime_attempt_number,
                    "application_attempt_number": claimed.attempt_number,
                    "service_instance_id": self.identity.service_instance_id,
                    "fencing_generation": self.fencing_generation,
                },
            )

        return RuntimeClaim(
            workspace_id=self.workspace_id,
            run_id=run_id,
            job_id=job_id,
            run_step_id=run_step_id,
            runtime_attempt_id=runtime_attempt_id,
            runtime_attempt_number=runtime_attempt_number,
            application_attempt_number=claimed.attempt_number,
            service_instance_id=self.identity.service_instance_id,
            fencing_generation=self.fencing_generation,
            claimed_at_us=claimed.now_us,
        )

    def complete(
        self, claim: RuntimeClaim, *, result_kind: str, result: Mapping[str, object]
    ) -> None:
        """Settle the exact claimed attempt and its durable job as succeeded."""
        self._require_scheduler_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            self._require_open_claim(claim)
            now_us = self._now_us()
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id,
                status=ATTEMPT_STATUS_SUCCEEDED,
                finished_at_us=now_us,
            )
            writer.record_step_status(
                run_step_id=claim.run_step_id,
                status="succeeded",
                observed_at_us=now_us,
            )
            steps = read_run_steps(
                self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
            )
            unfinished = [
                step.run_step_id
                for step in steps
                if step.status not in RUN_STEP_TERMINAL_STATUSES
            ]
            if unfinished:
                raise RuntimeSchedulingError(
                    f"run {claim.run_id!r} still has unfinished steps {unfinished!r}"
                )
            _terminalize_application_job(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                job_id=claim.job_id,
                fencing_generation=self.fencing_generation,
                clock=self.clock,
                state="succeeded",
                result_kind=result_kind,
                result=result,
                _transaction_open=True,
            )
            event_sequence = (
                read_run_sequence(
                    self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
                )
                + 1
            )
            runtime_event_id = _lineage_id(
                "runtime_event",
                self.workspace_id,
                claim.run_id,
                str(event_sequence),
                claim.run_step_id,
                claim.job_id,
            )
            writer.append_run_event(
                run_id=claim.run_id,
                runtime_event_id=runtime_event_id,
                occurred_at_us=now_us,
                event_kind="run_succeeded",
                run_status=RUN_STATUS_SUCCEEDED,
                run_step_id=claim.run_step_id,
                message="runtime scheduler settled the run as succeeded",
                details={
                    "workspace_id": self.workspace_id,
                    "run_id": claim.run_id,
                    "job_id": claim.job_id,
                    "run_step_id": claim.run_step_id,
                    "runtime_attempt_id": claim.runtime_attempt_id,
                    "runtime_attempt_number": claim.runtime_attempt_number,
                    "application_attempt_number": claim.application_attempt_number,
                    "service_instance_id": self.identity.service_instance_id,
                    "fencing_generation": self.fencing_generation,
                },
            )

    def recover_stranded(self) -> tuple[RuntimeRecovery, ...]:
        """Recover superseded job claims and their exact open runtime attempts."""
        now_us = self._now_us()
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            recovered = _recover_stranded_application_jobs_locked(
                self.connection,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                now_us=now_us,
            )
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            results: list[RuntimeRecovery] = []
            for job in recovered:
                run_id = read_run_id_by_job(
                    self.connection, workspace_id=self.workspace_id, job_id=job.job_id
                )
                if run_id is None:
                    continue
                steps = read_run_steps(
                    self.connection, workspace_id=self.workspace_id, run_id=run_id
                )
                open_attempts = [
                    (step.run_step_id, attempt)
                    for step in steps
                    for attempt in step.attempts
                    if attempt.status == ATTEMPT_STATUS_RUNNING
                ]
                if len(open_attempts) != 1:
                    raise RuntimeSchedulingError(
                        f"run {run_id!r} has {len(open_attempts)} open runtime "
                        "attempts; expected exactly one to recover"
                    )
                run_step_id, attempt = open_attempts[0]
                writer.finish_attempt(
                    attempt_id=attempt.attempt_id,
                    status=ATTEMPT_STATUS_FAILED,
                    finished_at_us=now_us,
                    failure=ApiError(
                        code="internal_recoverable",
                        message="the previous worker lost its fencing authority",
                        retry_class="retryable",
                    ),
                )
                runtime_event_id = _lineage_id(
                    "runtime_event",
                    self.workspace_id,
                    run_id,
                    str(
                        read_run_sequence(
                            self.connection,
                            workspace_id=self.workspace_id,
                            run_id=run_id,
                        )
                        + 1
                    ),
                    run_step_id,
                    job.job_id,
                )
                step_status = "pending" if job.requeued else "failed"
                run_status = RUN_STATUS_RUNNING if job.requeued else RUN_STATUS_FAILED
                event_kind = (
                    "attempt_interrupted" if job.requeued else "attempts_exhausted"
                )
                writer.record_step_status(
                    run_step_id=run_step_id,
                    status=step_status,
                    observed_at_us=now_us,
                )
                writer.append_run_event(
                    run_id=run_id,
                    runtime_event_id=runtime_event_id,
                    occurred_at_us=now_us,
                    event_kind=event_kind,
                    run_status=run_status,
                    run_step_id=run_step_id,
                    message=(
                        "runtime scheduler recovered a stranded attempt for retry"
                        if job.requeued
                        else "runtime scheduler exhausted attempts for a stranded job"
                    ),
                    details={
                        "workspace_id": self.workspace_id,
                        "run_id": run_id,
                        "job_id": job.job_id,
                        "run_step_id": run_step_id,
                        "runtime_attempt_id": attempt.attempt_id,
                        "runtime_attempt_number": attempt.attempt_number,
                        "application_attempt_number": job.application_attempt_number,
                        "service_instance_id": self.identity.service_instance_id,
                        "fencing_generation": self.fencing_generation,
                        "requeued": job.requeued,
                    },
                )
                results.append(
                    RuntimeRecovery(
                        job_id=job.job_id,
                        run_id=run_id,
                        run_step_id=run_step_id,
                        runtime_attempt_id=attempt.attempt_id,
                        application_attempt_number=job.application_attempt_number,
                        requeued=job.requeued,
                    )
                )
        return tuple(results)

    def _select_claimable(self) -> tuple[str, str, str] | None:
        """Return the oldest persisted job/run/step decision, or no ready work."""
        candidates = self.connection.execute(
            _QUEUED_RUNTIME_JOBS, (self.workspace_id,)
        ).fetchall()
        for job_id_value, run_id_value in candidates:
            job_id = str(job_id_value)
            run_id = str(run_id_value)
            status_row = self.connection.execute(
                _LATEST_RUN_STATUS, (self.workspace_id, run_id)
            ).fetchone()
            if status_row is None:
                raise RuntimeSchedulingError(
                    f"run {run_id!r} has no event stream to read its status from"
                )
            if str(status_row[0]) in RUN_TERMINAL_STATUSES:
                continue
            step_row = self.connection.execute(
                _FIRST_RUNNABLE_STEP, (self.workspace_id, run_id)
            ).fetchone()
            if step_row is not None:
                return job_id, run_id, str(step_row[0])
        return None

    def _now_us(self) -> int:
        return int(self.clock.wall_time().timestamp() * 1_000_000)

    def _require_scheduler_claim(self, claim: RuntimeClaim) -> None:
        if (
            claim.workspace_id != self.workspace_id
            or claim.service_instance_id != self.identity.service_instance_id
            or claim.fencing_generation != self.fencing_generation
        ):
            raise RuntimeSchedulingError(
                f"claim for job {claim.job_id!r} does not match this scheduler"
            )

    def _require_open_claim(self, claim: RuntimeClaim) -> None:
        """Prove the mutable job claim and canonical attempt match exactly."""
        job = read_job(self.connection, claim.job_id)
        run_id = read_run_id_by_job(
            self.connection, workspace_id=self.workspace_id, job_id=claim.job_id
        )
        if (
            job is None
            or job.state != JobState.CLAIMED.value
            or job.claimed_by_service_instance != claim.service_instance_id
            or job.fencing_generation != claim.fencing_generation
            or run_id != claim.run_id
        ):
            raise RuntimeSchedulingError(
                f"job {claim.job_id!r} is not claimed exactly as the claim states"
            )
        application_attempt = self.connection.execute(
            "SELECT attempt_number, state FROM omnivia_job_attempts "
            "WHERE workspace_id = ? AND job_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (self.workspace_id, claim.job_id),
        ).fetchone()
        if application_attempt != (claim.application_attempt_number, "running"):
            raise RuntimeSchedulingError(
                f"job {claim.job_id!r} does not have the claimed application attempt"
            )
        steps = read_run_steps(
            self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
        )
        step = next((s for s in steps if s.run_step_id == claim.run_step_id), None)
        if step is None or not step.attempts:
            raise RuntimeSchedulingError(
                f"run step {claim.run_step_id!r} has no attempt to settle"
            )
        attempt = step.attempts[-1]
        if (
            attempt.attempt_id != claim.runtime_attempt_id
            or attempt.attempt_number != claim.runtime_attempt_number
            or attempt.status != ATTEMPT_STATUS_RUNNING
        ):
            raise RuntimeSchedulingError(
                f"attempt {claim.runtime_attempt_id!r} is not the open attempt of "
                f"{claim.run_step_id!r}"
            )


__all__ = [
    "RuntimeClaim",
    "RuntimeRecovery",
    "RuntimeScheduler",
    "RuntimeSchedulingError",
]
