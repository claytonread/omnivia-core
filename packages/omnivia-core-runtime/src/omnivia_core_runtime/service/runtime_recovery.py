"""Startup recovery and orphan-attempt classification (RT-109).

One Core-private pass, run once by a successor that has already acquired a higher
fencing generation. It reads every runtime-bound job of the workspace, classifies
the exact open runtime Attempt from persisted evidence alone, and then does the
one thing that classification licences -- and nothing else.

**Evidence, never inference.** Every decision is read from the canonical ledger and
the mutable durable-job row: the job's state and the generation that claimed it, the
run's status from its own event stream, each step's latest status, each Attempt's
outcome (or its absence, which is what `running` means here) and each Wait's
resolution (or its absence, which is what `pending` means). Absence of a worker is
not evidence of anything: RT-108's adapter holds its sessions in memory, so a
restart loses them by construction. That loss is subordinate evidence, it is not
persisted or restored here, and it never licences a completion. Nothing in this
module writes a `succeeded` status to a Run, Step, Attempt or job.

**Five classifications and a refusal.** :data:`RECOVERY_CLASSIFICATIONS` is closed.

* `durable_open_wait` -- a stale claim whose run is `waiting` on exactly one pending
  Wait, held by the same step as its one running Attempt. The Wait and the Attempt
  are *preserved*. All that moves is the stale durable-job claim, rebound to the
  successor's fence and owner, so a later blanket recovery cannot sweep the job up
  and so `ResolveWait` resumes the same step under the same Attempt -- not
  `job.resume`, not a replacement Attempt. RT-107 owns that resumption and is not
  called from here.
* `orphan_attempt` -- a stale claim whose run status is exactly `running`, holding one
  running Attempt and no open Wait. This is the case RT-106's bounded recovery already
  settles: the old Attempt fails and the job is requeued only while the persisted
  attempt budget permits. `uncertain` is deliberately not this case.
* `active_claim` -- a claim at exactly this fencing generation. Observed, never
  stolen and never recovered, whatever its shape: it belongs to the live owner.
* `no_open_attempt` -- a queued job with nothing open. Observed. No work is invented.
* `terminal_history` -- job and run both terminal. Observed. Nothing is re-settled.
* `contradictory_history` -- anything else, which is a refusal rather than a class:
  a queued job holding a running Attempt, a stale claim with zero or several open
  Attempts, a `waiting` run that does not hold exactly one pending Wait on that
  step, a stale claim whose run is neither `running` nor `waiting` -- `uncertain` and
  `admitted` among them -- a claim holding a generation beyond this pass's own, a job
  and a run that disagree about termination. The item is reported and left exactly as
  found.

**Fenced and atomic per invocation.** Classification, adoption and orphan recovery
run inside one :func:`fenced_transaction`, so a superseded owner cannot mutate
anything mid-pass and a failed pass leaves nothing behind. Repeating the pass is a
no-op: an adopted job is then at the current generation, so it classifies as
`active_claim` and writes nothing, and a recovered job is no longer a stale claim.

**Effects, sandboxes and external sessions are out of scope.** Section 4.2 of the
recovery spec also classifies dispatched effects, sandbox/workspace leases and
external worker sessions. None of those has a store in this repository yet, and a
classification with no evidence behind it would be exactly the fabrication this
module exists to prevent. They belong to the slices that introduce their stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_RUNNING,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING,
    RUN_TERMINAL_STATUSES,
    WAIT_STATUS_PENDING,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.jobs import JobState
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeRecovery,
    RuntimeScheduler,
    RuntimeSchedulingError,
    _lineage_id,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunSnapshot,
    read_run,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.jobs import _now_us, _timestamp

CLASSIFICATION_ACTIVE_CLAIM: Final = "active_claim"
CLASSIFICATION_DURABLE_OPEN_WAIT: Final = "durable_open_wait"
CLASSIFICATION_ORPHAN_ATTEMPT: Final = "orphan_attempt"
CLASSIFICATION_NO_OPEN_ATTEMPT: Final = "no_open_attempt"
CLASSIFICATION_TERMINAL_HISTORY: Final = "terminal_history"
CLASSIFICATION_CONTRADICTORY_HISTORY: Final = "contradictory_history"

#: The closed vocabulary. A startup pass reports one of these per runtime-bound job
#: and there is no sixth answer, so an unrecognised shape is a refusal rather than a
#: new class invented at runtime.
RECOVERY_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        CLASSIFICATION_ACTIVE_CLAIM,
        CLASSIFICATION_DURABLE_OPEN_WAIT,
        CLASSIFICATION_ORPHAN_ATTEMPT,
        CLASSIFICATION_NO_OPEN_ATTEMPT,
        CLASSIFICATION_TERMINAL_HISTORY,
        CLASSIFICATION_CONTRADICTORY_HISTORY,
    }
)

_EVENT_KIND_WAIT_ADOPTED: Final = "wait_adopted"
_STEP_STATUS_RUNNING: Final = "running"
_STEP_STATUS_WAITING: Final = "waiting"

#: Every run of the workspace beside the mutable job row that carries it. Ordered by
#: admission so one pass reports the same sequence every time it is repeated.
_RUNTIME_BOUND_JOBS: Final = (
    "SELECT r.job_id, r.run_id, j.state, COALESCE(j.fencing_generation, 0) "
    "FROM omnivia_runtime_runs r "
    "JOIN omnivia_durable_jobs j ON j.job_id = r.job_id "
    "WHERE r.workspace_id = ? ORDER BY r.created_at_us, r.run_id"
)

#: Rebind one stale claim to the successor, and only while it is still the exact
#: stale claim that was classified. A compare-and-swap rather than a blind write, so
#: a repeated pass -- or one racing a generation that already moved -- changes no row.
_ADOPT_STALE_CLAIM: Final = (
    "UPDATE omnivia_durable_jobs SET updated_at = ?, "
    "claimed_by_service_instance = ?, fencing_generation = ? "
    "WHERE job_id = ? AND state = 'claimed' AND COALESCE(fencing_generation, 0) = ?"
)


@dataclass(frozen=True, slots=True)
class RuntimeItemClassification:
    """What one runtime-bound job's persisted history says at startup.

    `reason` states the evidence, not a remedy: the classification is what decides
    whether anything happens, and four of the six decide that nothing does.
    """

    job_id: str
    run_id: str
    job_state: str
    job_fencing_generation: int
    classification: str
    reason: str
    run_step_id: str | None = None
    runtime_attempt_id: str | None = None
    wait_id: str | None = None


@dataclass(frozen=True, slots=True)
class WaitAdoption:
    """One durable open Wait carried across a restart, with nothing else changed.

    The Wait and the Attempt named here are the ones that already existed; the only
    state this records is that the job's claim moved from
    `previous_fencing_generation` to the successor's.
    """

    job_id: str
    run_id: str
    run_step_id: str
    runtime_attempt_id: str
    wait_id: str
    previous_fencing_generation: int


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    """Everything one startup pass decided, and everything it changed."""

    service_instance_id: str
    fencing_generation: int
    classifications: tuple[RuntimeItemClassification, ...]
    adoptions: tuple[WaitAdoption, ...]
    recoveries: tuple[RuntimeRecovery, ...]

    def with_classification(
        self, classification: str
    ) -> tuple[RuntimeItemClassification, ...]:
        """The items decided one way, in the order the pass reported them."""
        return tuple(
            item
            for item in self.classifications
            if item.classification == classification
        )


@dataclass
class RuntimeStartupRecovery:
    """One fenced startup pass, for the service instance the scheduler speaks for.

    Composed from :class:`~service.runtime_scheduler.RuntimeScheduler` rather than
    repeating its connection, identity, workspace, generation and clock: the bounded
    recovery semantics for an orphan Attempt are the scheduler's and are reused
    exactly, not restated with a second opinion about attempt budgets.
    """

    scheduler: RuntimeScheduler

    def recover(self) -> StartupRecoveryReport:
        """Classify every runtime-bound job, then act only where evidence licences it."""
        scheduler = self.scheduler
        now_us = _now_us(scheduler.clock)
        with fenced_transaction(
            scheduler.connection,
            scheduler.identity,
            workspace_id=scheduler.workspace_id,
            fencing_generation=scheduler.fencing_generation,
        ):
            classifications = self._classify_all()
            adoptions = tuple(
                self._adopt(item, now_us=now_us)
                for item in classifications
                if item.classification == CLASSIFICATION_DURABLE_OPEN_WAIT
            )
            orphans = tuple(
                item.job_id
                for item in classifications
                if item.classification == CLASSIFICATION_ORPHAN_ATTEMPT
            )
            recoveries: tuple[RuntimeRecovery, ...] = (
                scheduler.recover_stranded_locked(now_us=now_us, job_ids=orphans)
                if orphans
                else ()
            )
        return StartupRecoveryReport(
            service_instance_id=scheduler.identity.service_instance_id,
            fencing_generation=scheduler.fencing_generation,
            classifications=classifications,
            adoptions=adoptions,
            recoveries=recoveries,
        )

    def _classify_all(self) -> tuple[RuntimeItemClassification, ...]:
        scheduler = self.scheduler
        rows = scheduler.connection.execute(
            _RUNTIME_BOUND_JOBS, (scheduler.workspace_id,)
        ).fetchall()
        items: list[RuntimeItemClassification] = []
        for job_id, run_id, job_state, job_generation in rows:
            snapshot = read_run(
                scheduler.connection,
                workspace_id=scheduler.workspace_id,
                run_id=str(run_id),
            )
            if snapshot is None:  # pragma: no cover - the join already proved the run
                continue
            items.append(
                classify_runtime_item(
                    job_id=str(job_id),
                    job_state=str(job_state),
                    job_fencing_generation=int(job_generation),
                    snapshot=snapshot,
                    current_fencing_generation=scheduler.fencing_generation,
                )
            )
        return tuple(items)

    def _adopt(self, item: RuntimeItemClassification, *, now_us: int) -> WaitAdoption:
        """Rebind one stale claim whose run is durably waiting, and say so once.

        The Wait, the step and the Attempt are untouched. The rebound job row is the
        idempotent proof on its own -- a second pass sees the current generation and
        classifies `active_claim` -- and the appended event is the readable evidence
        beside it, written through the same :class:`RuntimeWriter` seam every other
        runtime event uses. It carries the run's existing `waiting` status because
        adopting a claim is not a state transition of the run.
        """
        scheduler = self.scheduler
        # Structurally guaranteed by the classification: `durable_open_wait` is the
        # only branch that names all three, and it is the only caller.
        assert item.run_step_id is not None
        assert item.runtime_attempt_id is not None
        assert item.wait_id is not None
        adopted = scheduler.connection.execute(
            _ADOPT_STALE_CLAIM,
            (
                _timestamp(now_us),
                scheduler.identity.service_instance_id,
                scheduler.fencing_generation,
                item.job_id,
                item.job_fencing_generation,
            ),
        )
        if adopted.rowcount != 1:
            # The row is no longer the stale claim that was classified. Raising rolls
            # the whole pass back rather than reporting an adoption that did not happen.
            raise RuntimeSchedulingError(
                f"adopting job {item.job_id!r} at fencing generation "
                f"{item.job_fencing_generation} updated {adopted.rowcount} rows"
            )
        writer = transaction_local_writer(
            scheduler.connection, workspace_id=scheduler.workspace_id
        )
        writer.append_run_event(
            run_id=item.run_id,
            runtime_event_id=_lineage_id(
                "runtime_event",
                scheduler.workspace_id,
                item.run_id,
                _EVENT_KIND_WAIT_ADOPTED,
                item.wait_id,
                str(scheduler.fencing_generation),
            ),
            occurred_at_us=now_us,
            event_kind=_EVENT_KIND_WAIT_ADOPTED,
            run_status=RUN_STATUS_WAITING,
            run_step_id=item.run_step_id,
            message="startup recovery adopted the durable wait of a superseded claim",
            details={
                "workspace_id": scheduler.workspace_id,
                "run_id": item.run_id,
                "job_id": item.job_id,
                "run_step_id": item.run_step_id,
                "runtime_attempt_id": item.runtime_attempt_id,
                "wait_id": item.wait_id,
                "service_instance_id": scheduler.identity.service_instance_id,
                "fencing_generation": scheduler.fencing_generation,
                "previous_fencing_generation": item.job_fencing_generation,
            },
        )
        return WaitAdoption(
            job_id=item.job_id,
            run_id=item.run_id,
            run_step_id=item.run_step_id,
            runtime_attempt_id=item.runtime_attempt_id,
            wait_id=item.wait_id,
            previous_fencing_generation=item.job_fencing_generation,
        )


def classify_runtime_item(
    *,
    job_id: str,
    job_state: str,
    job_fencing_generation: int,
    snapshot: RunSnapshot,
    current_fencing_generation: int,
) -> RuntimeItemClassification:
    """Decide one runtime-bound job from persisted evidence, and change nothing.

    Pure, and separable from the pass that acts on it: what a crash left behind is a
    question about stored rows, and answering it should not require a transaction, a
    clock or a fence.
    """

    def decided(
        classification: str,
        reason: str,
        *,
        run_step_id: str | None = None,
        runtime_attempt_id: str | None = None,
        wait_id: str | None = None,
    ) -> RuntimeItemClassification:
        return RuntimeItemClassification(
            job_id=job_id,
            run_id=snapshot.run_id,
            job_state=job_state,
            job_fencing_generation=job_fencing_generation,
            classification=classification,
            reason=reason,
            run_step_id=run_step_id,
            runtime_attempt_id=runtime_attempt_id,
            wait_id=wait_id,
        )

    job_terminal = JobState(job_state).terminal
    run_terminal = snapshot.status in RUN_TERMINAL_STATUSES
    if job_terminal or run_terminal:
        if job_terminal and run_terminal:
            return decided(
                CLASSIFICATION_TERMINAL_HISTORY,
                "the job and its run are both terminal",
            )
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"job state {job_state!r} and run status {snapshot.status!r} disagree "
            "about termination",
        )

    open_attempts = tuple(
        (step, attempt)
        for step in snapshot.steps
        for attempt in step.attempts
        if attempt.status == ATTEMPT_STATUS_RUNNING
    )
    pending_waits = tuple(
        wait for wait in snapshot.waits if wait.status == WAIT_STATUS_PENDING
    )

    if job_state == JobState.QUEUED.value:
        if open_attempts:
            return decided(
                CLASSIFICATION_CONTRADICTORY_HISTORY,
                f"a queued job holds {len(open_attempts)} open runtime attempts",
                run_step_id=open_attempts[0][0].run_step_id,
                runtime_attempt_id=open_attempts[0][1].attempt_id,
            )
        if pending_waits:
            return decided(
                CLASSIFICATION_CONTRADICTORY_HISTORY,
                "a queued job holds an unresolved wait",
                wait_id=pending_waits[0].wait_id,
            )
        return decided(
            CLASSIFICATION_NO_OPEN_ATTEMPT,
            "the job is queued and holds no open runtime attempt",
        )

    if job_fencing_generation == current_fencing_generation:
        return decided(
            CLASSIFICATION_ACTIVE_CLAIM,
            "the claim already holds this fencing generation",
        )
    if job_fencing_generation > current_fencing_generation:
        # A claim from the future is not ours to observe as live: this pass holds a
        # generation that has already been superseded, so the only safe answer is none.
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"the claim holds fencing generation {job_fencing_generation} beyond this "
            f"pass's generation {current_fencing_generation}",
        )

    if len(open_attempts) != 1:
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"the superseded claim holds {len(open_attempts)} open runtime attempts; "
            "exactly one is recoverable",
        )

    step, attempt = open_attempts[0]
    if snapshot.status == RUN_STATUS_WAITING:
        if (
            len(pending_waits) != 1
            or pending_waits[0].run_step_id != step.run_step_id
            or step.status != _STEP_STATUS_WAITING
        ):
            return decided(
                CLASSIFICATION_CONTRADICTORY_HISTORY,
                "a waiting run does not hold exactly one pending wait on the step "
                "of its open attempt",
                run_step_id=step.run_step_id,
                runtime_attempt_id=attempt.attempt_id,
            )
        return decided(
            CLASSIFICATION_DURABLE_OPEN_WAIT,
            "one pending wait durably suspends the open attempt of this step",
            run_step_id=step.run_step_id,
            runtime_attempt_id=attempt.attempt_id,
            wait_id=pending_waits[0].wait_id,
        )

    if snapshot.status != RUN_STATUS_RUNNING:
        # `uncertain`, `admitted` and anything else are open questions, not orphans:
        # only a run the ledger still says is `running` may be failed and requeued.
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"run status {snapshot.status!r} is neither running nor waiting; only a "
            "running run may be recovered as an orphan attempt",
            run_step_id=step.run_step_id,
            runtime_attempt_id=attempt.attempt_id,
            wait_id=pending_waits[0].wait_id if pending_waits else None,
        )

    if pending_waits:
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"run status {snapshot.status!r} contradicts an unresolved wait",
            run_step_id=step.run_step_id,
            runtime_attempt_id=attempt.attempt_id,
            wait_id=pending_waits[0].wait_id,
        )
    if step.status != _STEP_STATUS_RUNNING:
        return decided(
            CLASSIFICATION_CONTRADICTORY_HISTORY,
            f"step status {step.status!r} contradicts its open runtime attempt",
            run_step_id=step.run_step_id,
            runtime_attempt_id=attempt.attempt_id,
        )
    return decided(
        CLASSIFICATION_ORPHAN_ATTEMPT,
        "a superseded claim holds one running attempt and no open wait",
        run_step_id=step.run_step_id,
        runtime_attempt_id=attempt.attempt_id,
    )


def recover_at_startup(
    scheduler: RuntimeScheduler,
) -> StartupRecoveryReport:
    """Run one startup pass for this scheduler's workspace and fencing generation."""
    return RuntimeStartupRecovery(scheduler).recover()


__all__ = [
    "CLASSIFICATION_ACTIVE_CLAIM",
    "CLASSIFICATION_CONTRADICTORY_HISTORY",
    "CLASSIFICATION_DURABLE_OPEN_WAIT",
    "CLASSIFICATION_NO_OPEN_ATTEMPT",
    "CLASSIFICATION_ORPHAN_ATTEMPT",
    "CLASSIFICATION_TERMINAL_HISTORY",
    "RECOVERY_CLASSIFICATIONS",
    "RuntimeItemClassification",
    "RuntimeStartupRecovery",
    "StartupRecoveryReport",
    "WaitAdoption",
    "classify_runtime_item",
    "recover_at_startup",
]
