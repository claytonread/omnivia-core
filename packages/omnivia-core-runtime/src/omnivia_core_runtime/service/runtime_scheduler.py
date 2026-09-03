"""Adapt the existing fenced durable-job queue to canonical runtime claims (RT-106).

The scheduler owns no second queue.  It selects a canonical Run/RunStep through the
job already bound to that Run, claims the mutable ``omnivia_durable_jobs`` row through
the existing application-job seam, then appends the runtime Attempt, step state and
event in the same fenced transaction.  Either both histories describe the claim, or
neither does.

This slice deliberately models the Core service's scheduling claim.  Worker-protocol
leases and their independent expiry belong to the WorkerAdapter slice (RT-108); the
authority here is the existing workspace lease plus its monotonic fencing generation.

**One claim covers one run, not one step.** A run with several steps is settled step by
step through :meth:`RuntimeScheduler.complete` and :meth:`RuntimeScheduler.fail`, and
each of them opens the next attempt *inside the transaction that closed the previous
one*. That is deliberate and it is what the schema requires: 0010 admits a second
application attempt only after the first one failed or was cancelled, so a run cannot be
walked by requeuing its durable job between steps, and RT-109 reads a claimed job with no
open runtime attempt as contradictory history. Settling and advancing together means the
invariant those two rules share -- *a claimed runtime-bound job has exactly one open
attempt, or a wait holding its step* -- is never momentarily false, not even inside a
transaction that crashes.

**What a definition may say about its own steps.** :data:`RuntimeStepPlan` is the one
seam a bound definition gets: whether a step's dependencies are satisfied, and a hook to
record that a step was reached. It is optional and absent by default, so a run with no
such plan behaves exactly as it did -- the first runnable step in ordinal order. It
cannot invent a step, a route or an ordinal, because it is consulted about steps that are
already open.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Protocol

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
    is_error_retryable,
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

_RUNNABLE_STEPS: Final = (
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
    "ORDER BY s.ordinal, s.run_step_id"
)

_NEXT_RUNTIME_ATTEMPT_NUMBER: Final = (
    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM omnivia_runtime_attempts "
    "WHERE workspace_id = ? AND run_step_id = ?"
)

#: The attempt budget the durable job already states, which is the bound a retry is held
#: to. Read rather than configured here: 0010 stores it per job and enforces it on the
#: application attempt, so a second bound in Python could only disagree with it. Every
#: step of the run inherits it in full and independently -- see :meth:`RuntimeScheduler.fail`.
_JOB_ATTEMPT_BUDGET: Final = (
    "SELECT max_attempts FROM omnivia_job_application_metadata "
    "WHERE workspace_id = ? AND job_id = ?"
)

_OPEN_APPLICATION_ATTEMPT: Final = (
    "SELECT attempt_number FROM omnivia_job_attempts "
    "WHERE workspace_id = ? AND job_id = ? AND state = 'running' "
    "ORDER BY attempt_number DESC LIMIT 1"
)

_RUN_JOB: Final = (
    "SELECT job_id FROM omnivia_runtime_runs WHERE workspace_id = ? AND run_id = ?"
)

_OPEN_RUNTIME_ATTEMPTS: Final = (
    "SELECT a.attempt_id, a.run_step_id, a.attempt_number, a.started_at_us "
    "FROM omnivia_runtime_attempts a "
    "LEFT JOIN omnivia_runtime_attempt_outcomes o "
    "  ON o.workspace_id = a.workspace_id AND o.attempt_id = a.attempt_id "
    "WHERE a.workspace_id = ? AND a.run_id = ? AND o.attempt_id IS NULL "
    "ORDER BY a.attempt_id"
)

_EVENT_ATTEMPT_STARTED: Final = "attempt_started"
_EVENT_STEP_SUCCEEDED: Final = "step_succeeded"
_EVENT_ATTEMPT_RETRIED: Final = "attempt_retried"
_EVENT_RUN_SUCCEEDED: Final = "run_succeeded"
_EVENT_RUN_FAILED: Final = "run_failed"

_STEP_STATUS_PENDING: Final = "pending"
_STEP_STATUS_RUNNING: Final = "running"
_STEP_STATUS_SUCCEEDED: Final = "succeeded"
_STEP_STATUS_FAILED: Final = "failed"


class RuntimeSchedulingError(StorageError):
    """A selected runtime-bound job has corrupt or contradictory history."""


class RuntimeStepPlan(Protocol):
    """What the definition a run is bound to says about the steps already open for it.

    Two questions, and deliberately no third. `ready` decides whether an open, pending
    step may be claimed *now*, which is the one thing the generic runnable-step query
    cannot know: it reads statuses, attempts and waits, and a definition's dependency
    edges live in the definition. `observe` is told that a step was reached, so a
    definition that records observations records them at the instant its step actually
    started rather than at the instant its run was admitted.

    Neither may open a step, choose an ordinal, or state a route. Those are the sealed
    definition's, fixed before the run existed, and a scheduler that could re-decide them
    could publish an execution the run was never planned as.
    """

    def ready(
        self, connection: sqlite3.Connection, *, run_id: str, run_step_id: str
    ) -> bool: ...

    def observe(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        run_step_id: str,
        observed_at_us: int,
    ) -> None: ...


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
    plan: RuntimeStepPlan | None = None

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

            claim = self._open_attempt(
                run_id=run_id,
                job_id=job_id,
                run_step_id=run_step_id,
                application_attempt_number=claimed.attempt_number,
                now_us=claimed.now_us,
                event_kind=_EVENT_ATTEMPT_STARTED,
                message="runtime scheduler claimed the next runnable attempt",
            )

        return claim

    def resume_claim(self, run_id: str) -> RuntimeClaim | None:
        """The claim this instance already holds over one run, rebuilt from stored rows.

        A restart keeps the workspace and loses the process. What survives is exactly a
        `RuntimeClaim`'s content -- the claimed durable job, its running application
        attempt, and the run's single open runtime attempt -- so the object a settlement
        needs is read back rather than reissued. Nothing is written and nothing is
        re-decided: a run whose claim is not held here at this generation, or whose
        history does not state exactly one open attempt, has no claim to resume and
        answers `None`.

        This is what lets an adopted run continue after the pass that adopted it. Without
        it a run recovered by RT-109 would be correctly recovered and permanently
        unfinishable, because every settlement is addressed by the claim that opened the
        attempt it settles.
        """
        bound = self.connection.execute(
            _RUN_JOB, (self.workspace_id, run_id)
        ).fetchone()
        if bound is None:
            return None
        job_id = str(bound[0])
        job = read_job(self.connection, job_id)
        if (
            job is None
            or job.state != JobState.CLAIMED.value
            or job.claimed_by_service_instance != self.identity.service_instance_id
            or job.fencing_generation != self.fencing_generation
        ):
            return None
        application = self.connection.execute(
            _OPEN_APPLICATION_ATTEMPT, (self.workspace_id, job_id)
        ).fetchone()
        if application is None:
            return None
        open_attempts = self.connection.execute(
            _OPEN_RUNTIME_ATTEMPTS, (self.workspace_id, run_id)
        ).fetchall()
        if len(open_attempts) != 1:
            return None
        attempt_id, run_step_id, attempt_number, started_at_us = open_attempts[0]
        return RuntimeClaim(
            workspace_id=self.workspace_id,
            run_id=run_id,
            job_id=job_id,
            run_step_id=str(run_step_id),
            runtime_attempt_id=str(attempt_id),
            runtime_attempt_number=int(attempt_number),
            application_attempt_number=int(application[0]),
            service_instance_id=self.identity.service_instance_id,
            fencing_generation=self.fencing_generation,
            claimed_at_us=int(started_at_us),
        )

    def complete(
        self, claim: RuntimeClaim, *, result_kind: str, result: Mapping[str, object]
    ) -> RuntimeClaim | None:
        """Settle the exact claimed attempt, and either advance the run or finish it.

        The claimed step succeeds either way. What differs is what that means for the
        run: a run whose steps are now all terminal is settled as succeeded together with
        its durable job, and a run with work left has its next dependency-ready step
        claimed in this same transaction. The returned claim is that next one, and `None`
        says the run is finished.

        Advancing here rather than in a second call is the schema's requirement, not a
        convenience. 0010 admits another application attempt only after the previous one
        failed or was cancelled, so a successful step cannot release and re-take the
        durable job; and RT-109 reads a claimed job whose run holds no open attempt as
        contradictory history, so the gap between two steps must not be observable. Both
        are satisfied by never leaving one.

        A run whose remaining steps are all blocked is a refusal, not a silent stall: the
        transaction rolls back, this step is not recorded as succeeded, and the caller is
        told the run cannot proceed rather than left holding a claim over a run nothing
        will ever pick up.
        """
        self._require_scheduler_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            self._require_live_run(claim)
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
                status=_STEP_STATUS_SUCCEEDED,
                observed_at_us=now_us,
            )
            unfinished = [
                step.run_step_id
                for step in read_run_steps(
                    self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
                )
                if step.status not in RUN_STEP_TERMINAL_STATUSES
            ]
            if unfinished:
                nxt = self._ready_step(claim.run_id)
                if nxt is None:
                    raise RuntimeSchedulingError(
                        f"run {claim.run_id!r} has unfinished steps {unfinished!r} and "
                        "none of them is ready to run"
                    )
                return self._open_attempt(
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    run_step_id=nxt,
                    application_attempt_number=claim.application_attempt_number,
                    now_us=now_us,
                    event_kind=_EVENT_STEP_SUCCEEDED,
                    message="runtime scheduler advanced to the next ready step",
                    settled_step_id=claim.run_step_id,
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
            self._append_event(
                claim,
                now_us=now_us,
                event_kind=_EVENT_RUN_SUCCEEDED,
                run_status=RUN_STATUS_SUCCEEDED,
                message="runtime scheduler settled the run as succeeded",
            )
        return None

    def fail(
        self,
        claim: RuntimeClaim,
        *,
        failure: ApiError,
        result_kind: str | None = None,
        result: Mapping[str, object] | None = None,
    ) -> RuntimeClaim | None:
        """Settle the exact claimed attempt as failed, and retry it or fail the run.

        The bound is the durable job's own `max_attempts`, read from
        `omnivia_job_application_metadata` rather than configured here, and the
        retryability is the accepted contract's -- `is_error_retryable`, which fails safe
        when a stated retry class contradicts a frozen error code. A failure that is
        either non-retryable or out of budget fails the step, the run and the durable job
        together.

        **That budget is inherited by each step in full, not spent across the run.** The
        number compared against it is `runtime_attempt_number`, and both 0018 and the
        frozen `Attempt` contract define that as the ordinal *within its step* -- numbered
        `1..N` per `run_step_id`, with nothing counting attempts across a run. So a step
        that follows a retried one opens at 1 and holds the whole budget. That is the same
        shape the application layer already holds itself to: 0010's trigger, RT-109's
        requeue rule and `JobControl` recovery all compare one lineage's own contiguous
        counter against the job's ceiling, never a sum spent by other lineages. A run-wide
        aggregate would be a different policy needing a run-wide counter, and no schema,
        contract or accepted test states one. The retry loop still terminates: steps are
        finite and each step's ceiling is.

        A retry reopens *the same step* inside this transaction: its status moves through
        `pending` back to `running` under a new attempt, which is the only spelling 0018
        admits -- a step state may not restate its predecessor, and a step marked `failed`
        is final. So a retried step is never momentarily a step nothing holds.
        """
        self._require_scheduler_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            self._require_live_run(claim)
            self._require_open_claim(claim)
            now_us = self._now_us()
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id,
                status=ATTEMPT_STATUS_FAILED,
                finished_at_us=now_us,
                failure=failure,
            )
            if is_error_retryable(failure) and (
                claim.runtime_attempt_number < self._attempt_budget(claim.job_id)
            ):
                writer.record_step_status(
                    run_step_id=claim.run_step_id,
                    status=_STEP_STATUS_PENDING,
                    observed_at_us=now_us,
                )
                return self._open_attempt(
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    run_step_id=claim.run_step_id,
                    application_attempt_number=claim.application_attempt_number,
                    now_us=now_us,
                    event_kind=_EVENT_ATTEMPT_RETRIED,
                    message="runtime scheduler retried a failed attempt",
                    failure=failure,
                )
            writer.record_step_status(
                run_step_id=claim.run_step_id,
                status=_STEP_STATUS_FAILED,
                observed_at_us=now_us,
            )
            _terminalize_application_job(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                job_id=claim.job_id,
                fencing_generation=self.fencing_generation,
                clock=self.clock,
                state="failed",
                result_kind=result_kind,
                result=result,
                error=failure.to_wire(),
                _transaction_open=True,
            )
            self._append_event(
                claim,
                now_us=now_us,
                event_kind=_EVENT_RUN_FAILED,
                run_status=RUN_STATUS_FAILED,
                message="runtime scheduler settled the run as failed",
                failure=failure,
            )
        return None

    def recover_stranded(
        self, *, job_ids: Collection[str] | None = None
    ) -> tuple[RuntimeRecovery, ...]:
        """Recover superseded job claims and their exact open runtime attempts.

        `job_ids` narrows the pass to an exact allowlist, which is what RT-109's
        startup classification needs: only the jobs it classified as orphaned attempts
        may be interrupted, and a job suspended on a durable wait must not be. `None`
        keeps the whole-queue behaviour, an empty collection recovers nothing.
        """
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            results = self._recover_stranded_locked(job_ids=job_ids)
        return results

    def _recover_stranded_locked(
        self, *, job_ids: Collection[str] | None = None
    ) -> tuple[RuntimeRecovery, ...]:
        """The recovery itself, issued into a fenced transaction the caller opened.

        The seam RT-109 composes with: its classification, wait adoption and orphan
        recovery are one atomic startup pass, and it cannot reach `recover_stranded`
        from inside that pass because `BEGIN IMMEDIATE` does not nest. Nothing is
        weakened by it -- every statement lands in the caller's fenced transaction and
        is covered by that transaction's entry and pre-commit validation.
        """
        now_us = self._now_us()
        recovered = _recover_stranded_application_jobs_locked(
            self.connection,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            now_us=now_us,
            job_ids=job_ids,
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
            run_step_id = self._ready_step(run_id)
            if run_step_id is not None:
                return job_id, run_id, run_step_id
        return None

    def _ready_step(self, run_id: str) -> str | None:
        """The first runnable step of one run its bound definition will admit now.

        Runnable is the generic reading -- pending, no open attempt, no unresolved wait,
        lowest ordinal first -- and readiness is what the definition adds to it. Without a
        plan every runnable step is ready, which is the behaviour a run bound to no
        definition-level dependency graph has always had.
        """
        for row in self.connection.execute(
            _RUNNABLE_STEPS, (self.workspace_id, run_id)
        ).fetchall():
            run_step_id = str(row[0])
            if self.plan is None or self.plan.ready(
                self.connection, run_id=run_id, run_step_id=run_step_id
            ):
                return run_step_id
        return None

    def _open_attempt(
        self,
        *,
        run_id: str,
        job_id: str,
        run_step_id: str,
        application_attempt_number: int,
        now_us: int,
        event_kind: str,
        message: str,
        settled_step_id: str | None = None,
        failure: ApiError | None = None,
    ) -> RuntimeClaim:
        """Open one runtime attempt over one step, and record that it started.

        The single place an attempt is opened, so a first claim, an advance to the next
        step and a retry all write the same three rows in the same order under the same
        derived lineage. `settled_step_id` names the step whose settlement produced this
        attempt, where one did, so the event that opens a step also states the step it
        followed rather than leaving the order to be inferred from timestamps.
        """
        attempt_row = self.connection.execute(
            _NEXT_RUNTIME_ATTEMPT_NUMBER, (self.workspace_id, run_step_id)
        ).fetchone()
        if attempt_row is None:  # pragma: no cover - aggregate always returns
            raise RuntimeSchedulingError("runtime attempt allocation returned no row")
        attempt_number = int(attempt_row[0])
        attempt_id = _lineage_id(
            "runtime_attempt",
            self.workspace_id,
            run_id,
            run_step_id,
            str(attempt_number),
            job_id,
        )
        writer = transaction_local_writer(
            self.connection, workspace_id=self.workspace_id
        )
        writer.record_step_status(
            run_step_id=run_step_id,
            status=_STEP_STATUS_RUNNING,
            observed_at_us=now_us,
        )
        writer.start_attempt(
            attempt_id=attempt_id,
            run_id=run_id,
            run_step_id=run_step_id,
            attempt_number=attempt_number,
            started_at_us=now_us,
        )
        if self.plan is not None:
            self.plan.observe(
                self.connection,
                run_id=run_id,
                run_step_id=run_step_id,
                observed_at_us=now_us,
            )
        claim = RuntimeClaim(
            workspace_id=self.workspace_id,
            run_id=run_id,
            job_id=job_id,
            run_step_id=run_step_id,
            runtime_attempt_id=attempt_id,
            runtime_attempt_number=attempt_number,
            application_attempt_number=application_attempt_number,
            service_instance_id=self.identity.service_instance_id,
            fencing_generation=self.fencing_generation,
            claimed_at_us=now_us,
        )
        self._append_event(
            claim,
            now_us=now_us,
            event_kind=event_kind,
            run_status=RUN_STATUS_RUNNING,
            message=message,
            settled_step_id=settled_step_id,
            failure=failure,
        )
        return claim

    def _append_event(
        self,
        claim: RuntimeClaim,
        *,
        now_us: int,
        event_kind: str,
        run_status: str,
        message: str,
        settled_step_id: str | None = None,
        failure: ApiError | None = None,
    ) -> None:
        """One entry on the run's stream, carrying the lineage this claim was made under."""
        event_sequence = (
            read_run_sequence(
                self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
            )
            + 1
        )
        details: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "run_id": claim.run_id,
            "job_id": claim.job_id,
            "run_step_id": claim.run_step_id,
            "runtime_attempt_id": claim.runtime_attempt_id,
            "runtime_attempt_number": claim.runtime_attempt_number,
            "application_attempt_number": claim.application_attempt_number,
            "service_instance_id": self.identity.service_instance_id,
            "fencing_generation": self.fencing_generation,
        }
        if settled_step_id is not None:
            details["settled_run_step_id"] = settled_step_id
        if failure is not None:
            details["failure"] = dict(failure.to_wire())
        transaction_local_writer(
            self.connection, workspace_id=self.workspace_id
        ).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=_lineage_id(
                "runtime_event",
                self.workspace_id,
                claim.run_id,
                str(event_sequence),
                claim.run_step_id,
                claim.job_id,
            ),
            occurred_at_us=now_us,
            event_kind=event_kind,
            run_status=run_status,
            run_step_id=claim.run_step_id,
            message=message,
            details=details,
        )

    def _attempt_budget(self, job_id: str) -> int:
        row = self.connection.execute(
            _JOB_ATTEMPT_BUDGET, (self.workspace_id, job_id)
        ).fetchone()
        if row is None:
            raise RuntimeSchedulingError(
                f"job {job_id!r} states no attempt budget to hold a retry to"
            )
        return int(row[0])

    def _require_live_run(self, claim: RuntimeClaim) -> None:
        """Refuse to settle a run that has already finished, before any write.

        0018 refuses an event after a terminal one, so this would fail anyway -- as an
        integrity error, after the attempt outcome and step status had been written and
        were about to be rolled back. Reading the status first turns "a cancelled run
        cannot be settled" into a typed refusal a caller can act on, and it is the check
        that makes a cancellation actually block a settlement racing it.
        """
        row = self.connection.execute(
            _LATEST_RUN_STATUS, (self.workspace_id, claim.run_id)
        ).fetchone()
        if row is None:
            raise RuntimeSchedulingError(
                f"run {claim.run_id!r} has no event stream to read its status from"
            )
        if str(row[0]) in RUN_TERMINAL_STATUSES:
            raise RuntimeSchedulingError(
                f"run {claim.run_id!r} is {str(row[0])!r} and admits no further settlement"
            )

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
    "RuntimeStepPlan",
]
