"""One fenced startup pass over every runtime-bound job of a workspace (RT-109).

A service instance that has just taken the workspace has no memory of what the
previous one was doing. What it has is the persisted history: the durable job row and
its claim, the canonical Run's event stream, its steps, their attempts and their
waits. This module reads exactly that, classifies each runtime-bound job into one of
six named states, and repairs only the two that are repairable -- all inside a single
fenced transaction, so the workspace is never observed half-recovered.

**The classification vocabulary is closed** (:data:`RUNTIME_JOB_CLASSIFICATIONS`) and
is a statement about persisted evidence, never about what happens to be in memory:

* `active_claim` -- a claim held at *this* generation with one open attempt. Nothing
  to recover; the owner is the current one.
* `durable_open_wait` -- the run is suspended on an unresolved `Wait`. The work was
  not interrupted, so it is adopted rather than recovered: when the claim is stale,
  only the claim is rebound to this owner and generation, and the exact `Wait`, step
  and running `Attempt` are preserved so the wait's own resolution still resumes them.
* `orphan_attempt` -- a stale claim with one open attempt and no wait. This is the
  interrupted case, and it is handed to the *existing* bounded scheduler recovery
  (RT-106) narrowed to an exact job-id allowlist, so its attempt budget, requeue rule
  and exhaustion behaviour are reused rather than restated.
* `no_open_attempt` -- non-terminal, unclaimed, nothing open. An ordinary queued job.
* `terminal_history` -- the run has finished. Left exactly as it is.
* `contradictory_history` -- the two histories cannot both be true. Left untouched and
  reported, which is the whole of the response: a startup pass that "resolved" a
  contradiction would be inventing the fact it could not read.

**Nothing here manufactures success.** There is no path that writes a `succeeded` run,
step, attempt, job or event, and the absence of an in-memory worker session is not
evidence of anything: a worker that this process never spoke to may have done all of
its work, none of it, or half of it, and the only honest reading of an open attempt
whose owner is gone is that it was interrupted. Recovery therefore fails such an
attempt as retryable through RT-106, which is a statement the persisted history
supports.

**Repeating the pass changes nothing.** Every repair moves the evidence out of the
state that asked for it -- an adopted claim is no longer stale, a recovered orphan is
queued or exhausted -- so a second pass classifies the same jobs and writes nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_RUNNING,
    RUN_STATUS_WAITING,
    RUN_TERMINAL_STATUSES,
    WAIT_STATUS_PENDING,
    Attempt,
    Wait,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeScheduler,
    RuntimeSchedulingError,
    _lineage_id,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunSnapshot,
    RuntimeWriter,
    read_run,
    read_run_sequence,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.jobs import _adopt_stale_job_claim_locked

CLASSIFICATION_ACTIVE_CLAIM: Final = "active_claim"
CLASSIFICATION_DURABLE_OPEN_WAIT: Final = "durable_open_wait"
CLASSIFICATION_ORPHAN_ATTEMPT: Final = "orphan_attempt"
CLASSIFICATION_NO_OPEN_ATTEMPT: Final = "no_open_attempt"
CLASSIFICATION_TERMINAL_HISTORY: Final = "terminal_history"
CLASSIFICATION_CONTRADICTORY_HISTORY: Final = "contradictory_history"

#: Every state this pass can read out of persisted evidence, and there is no seventh.
#: Closed on purpose: an unclassifiable job is `contradictory_history`, which is left
#: untouched, rather than a new name invented at the moment it is met.
RUNTIME_JOB_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
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
_JOB_STATE_CLAIMED: Final = "claimed"
_TERMINAL_JOB_STATES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed", "cancelled"}
)

_RUNTIME_BOUND_JOBS: Final = (
    "SELECT j.job_id, r.run_id, j.state, COALESCE(j.fencing_generation, 0), "
    "j.claimed_by_service_instance FROM omnivia_durable_jobs j "
    "JOIN omnivia_runtime_runs r ON r.workspace_id = ? AND r.job_id = j.job_id "
    "ORDER BY j.created_at, j.job_id"
)


@dataclass(frozen=True, slots=True)
class RuntimeJobRecovery:
    """What one runtime-bound job was found to be, and what the pass did about it."""

    job_id: str
    run_id: str
    classification: str
    superseded: bool
    run_step_id: str | None = None
    runtime_attempt_id: str | None = None
    wait_id: str | None = None
    adopted: bool = False
    requeued: bool | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStartupRecovery:
    """The complete, atomic result of one startup pass over one workspace."""

    workspace_id: str
    fencing_generation: int
    jobs: tuple[RuntimeJobRecovery, ...]

    def classified(self, classification: str) -> tuple[RuntimeJobRecovery, ...]:
        """Every job the pass read as `classification`, in the order it read them."""
        if classification not in RUNTIME_JOB_CLASSIFICATIONS:
            raise RuntimeSchedulingError(
                f"{classification!r} is not a runtime recovery classification"
            )
        return tuple(job for job in self.jobs if job.classification == classification)


def recover_runtime_startup(scheduler: RuntimeScheduler) -> RuntimeStartupRecovery:
    """Classify and repair every runtime-bound job of one workspace, atomically.

    Classification, wait adoption and orphan recovery all run inside one fenced
    transaction opened here, which is why the scheduler is reached through its
    transaction-local recovery seam rather than its public one.
    """
    connection = scheduler.connection
    workspace_id = scheduler.workspace_id
    generation = scheduler.fencing_generation
    now_us = int(scheduler.clock.wall_time().timestamp() * 1_000_000)

    with fenced_transaction(
        connection,
        scheduler.identity,
        workspace_id=workspace_id,
        fencing_generation=generation,
    ):
        writer = transaction_local_writer(connection, workspace_id=workspace_id)
        found: list[RuntimeJobRecovery] = []
        orphans: list[str] = []
        for row in connection.execute(_RUNTIME_BOUND_JOBS, (workspace_id,)).fetchall():
            job_id, run_id = str(row[0]), str(row[1])
            snapshot = read_run(connection, workspace_id=workspace_id, run_id=run_id)
            if snapshot is None:  # pragma: no cover - the join proved the run exists
                raise RuntimeSchedulingError(
                    f"job {job_id!r} names run {run_id!r}, which is unreadable"
                )
            entry = _classify(
                snapshot,
                job_id=job_id,
                job_state=str(row[2]),
                job_generation=int(row[3]),
                fencing_generation=generation,
            )
            if entry.classification == CLASSIFICATION_ORPHAN_ATTEMPT:
                orphans.append(job_id)
            elif entry.classification == CLASSIFICATION_DURABLE_OPEN_WAIT and (
                entry.superseded
            ):
                entry = _adopt_open_wait(
                    scheduler,
                    writer,
                    entry=entry,
                    snapshot=snapshot,
                    superseded_by=None if row[4] is None else str(row[4]),
                    superseded_generation=int(row[3]),
                    now_us=now_us,
                )
            found.append(entry)

        recovered = {
            item.job_id: item
            for item in scheduler._recover_stranded_locked(job_ids=tuple(orphans))
        }
        if set(recovered) != set(orphans):
            raise RuntimeSchedulingError(
                "startup recovery classified "
                f"{sorted(set(orphans) - set(recovered))!r} as orphaned attempts that "
                "the bounded scheduler recovery did not recover"
            )
        jobs = tuple(
            entry
            if entry.job_id not in recovered
            else replace(entry, requeued=recovered[entry.job_id].requeued)
            for entry in found
        )

    return RuntimeStartupRecovery(
        workspace_id=workspace_id, fencing_generation=generation, jobs=jobs
    )


def _classify(
    snapshot: RunSnapshot,
    *,
    job_id: str,
    job_state: str,
    job_generation: int,
    fencing_generation: int,
) -> RuntimeJobRecovery:
    """Read one job and its run out of persisted evidence alone."""
    open_attempts = [
        (step.run_step_id, attempt)
        for step in snapshot.steps
        for attempt in step.attempts
        if attempt.status == ATTEMPT_STATUS_RUNNING
    ]
    open_waits = [wait for wait in snapshot.waits if wait.status == WAIT_STATUS_PENDING]
    claimed = job_state == _JOB_STATE_CLAIMED
    superseded = claimed and job_generation < fencing_generation
    entry = RuntimeJobRecovery(
        job_id=job_id,
        run_id=snapshot.run_id,
        classification=CLASSIFICATION_CONTRADICTORY_HISTORY,
        superseded=superseded,
        run_step_id=open_attempts[0][0] if len(open_attempts) == 1 else None,
        runtime_attempt_id=(
            open_attempts[0][1].attempt_id if len(open_attempts) == 1 else None
        ),
        wait_id=open_waits[0].wait_id if len(open_waits) == 1 else None,
    )

    contradiction = _contradiction(
        snapshot,
        job_state=job_state,
        claimed=claimed,
        open_attempts=open_attempts,
        open_waits=open_waits,
    )
    if contradiction is not None:
        return replace(entry, detail=contradiction)
    if snapshot.status in RUN_TERMINAL_STATUSES:
        # `superseded` is already false here: a claimed job beside a terminal run is
        # the disagreement the rule above refuses, so a terminal run's job is terminal.
        return replace(entry, classification=CLASSIFICATION_TERMINAL_HISTORY)
    if open_waits:
        return replace(entry, classification=CLASSIFICATION_DURABLE_OPEN_WAIT)
    if not open_attempts:
        return replace(entry, classification=CLASSIFICATION_NO_OPEN_ATTEMPT)
    return replace(
        entry,
        classification=(
            CLASSIFICATION_ORPHAN_ATTEMPT
            if superseded
            else CLASSIFICATION_ACTIVE_CLAIM
        ),
    )


def _contradiction(
    snapshot: RunSnapshot,
    *,
    job_state: str,
    claimed: bool,
    open_attempts: list[tuple[str, Attempt]],
    open_waits: list[Wait],
) -> str | None:
    """Why these two histories cannot both be true, or `None` when they can.

    Each rule names a pair of facts this codebase writes in one transaction and can
    therefore never have written apart. Meeting them apart means something outside
    these paths changed one of them, which is not a state to repair from.

    That includes cancellation, which is why no stop-ledger record is consulted here.
    `workflow.control` settles the canonical run and the durable job in one fenced
    transaction -- migration 0036 admits the `cancelled` terminal observation through
    that same accepted stop -- so a cancelled run beside a live job is not something the
    cancellation path can produce. Excusing the pair because a stop was recorded would
    hide exactly the half-written history this classification exists to report.
    """
    job_terminal = job_state in _TERMINAL_JOB_STATES
    run_terminal = snapshot.status in RUN_TERMINAL_STATUSES
    if len(open_attempts) > 1:
        return f"{len(open_attempts)} runtime attempts are open; at most one may be"
    if len(open_waits) > 1:
        return f"{len(open_waits)} waits are unresolved; at most one may be"
    if run_terminal != job_terminal:
        return (
            f"the run is {snapshot.status!r} and its durable job is {job_state!r}; "
            "the two histories disagree about being finished"
        )
    if run_terminal and (open_attempts or open_waits):
        return (
            f"the run is {snapshot.status!r} and still holds an open attempt or wait"
        )
    if bool(open_waits) != (snapshot.status == RUN_STATUS_WAITING):
        return (
            f"the run is {snapshot.status!r} and holds {len(open_waits)} unresolved "
            "waits; a run waits exactly while a wait holds it"
        )
    if open_waits and open_waits[0].run_step_id != (
        open_attempts[0][0] if open_attempts else None
    ):
        return (
            f"wait {open_waits[0].wait_id!r} suspends a step with no open attempt to "
            "resume"
        )
    if claimed and not open_attempts:
        return "the durable job is claimed and its run holds no open attempt"
    if open_attempts and not claimed:
        return f"a runtime attempt is open while its durable job is {job_state!r}"
    return None


def _adopt_open_wait(
    scheduler: RuntimeScheduler,
    writer: RuntimeWriter,
    *,
    entry: RuntimeJobRecovery,
    snapshot: RunSnapshot,
    superseded_by: str | None,
    superseded_generation: int,
    now_us: int,
) -> RuntimeJobRecovery:
    """Rebind one stale claim to this owner and record that the wait was adopted.

    The `Wait`, the step and the running `Attempt` are read here and written nowhere:
    the run is still suspended on exactly what suspended it, and resolving that wait
    resumes that step under that attempt, as RT-107 already does. The only canonical
    write is the audit event, and the only mutable one is the claim.
    """
    run_step_id, attempt_id = entry.run_step_id, entry.runtime_attempt_id
    if run_step_id is None or attempt_id is None:  # pragma: no cover - classified pair
        raise RuntimeSchedulingError(
            f"job {entry.job_id!r} was classified as an open wait without a step"
        )
    connection: sqlite3.Connection = scheduler.connection
    if not _adopt_stale_job_claim_locked(
        connection,
        workspace_id=scheduler.workspace_id,
        job_id=entry.job_id,
        service_instance_id=scheduler.identity.service_instance_id,
        fencing_generation=scheduler.fencing_generation,
        now_us=now_us,
    ):
        raise RuntimeSchedulingError(
            f"job {entry.job_id!r} was classified as a superseded claim but is not one"
        )
    sequence = (
        read_run_sequence(
            connection, workspace_id=scheduler.workspace_id, run_id=entry.run_id
        )
        + 1
    )
    details: dict[str, object] = {
        "workspace_id": scheduler.workspace_id,
        "run_id": entry.run_id,
        "job_id": entry.job_id,
        "run_step_id": run_step_id,
        "wait_id": entry.wait_id,
        "runtime_attempt_id": attempt_id,
        "service_instance_id": scheduler.identity.service_instance_id,
        "fencing_generation": scheduler.fencing_generation,
        "superseded_fencing_generation": superseded_generation,
    }
    if superseded_by is not None:
        details["superseded_service_instance_id"] = superseded_by
    writer.append_run_event(
        run_id=entry.run_id,
        runtime_event_id=_lineage_id(
            "runtime_event",
            scheduler.workspace_id,
            entry.run_id,
            str(sequence),
            run_step_id,
            entry.job_id,
        ),
        occurred_at_us=now_us,
        event_kind=_EVENT_KIND_WAIT_ADOPTED,
        run_status=snapshot.status,
        run_step_id=run_step_id,
        message="runtime startup recovery adopted a durable open wait",
        details=details,
    )
    return replace(entry, adopted=True)


__all__ = [
    "CLASSIFICATION_ACTIVE_CLAIM",
    "CLASSIFICATION_CONTRADICTORY_HISTORY",
    "CLASSIFICATION_DURABLE_OPEN_WAIT",
    "CLASSIFICATION_NO_OPEN_ATTEMPT",
    "CLASSIFICATION_ORPHAN_ATTEMPT",
    "CLASSIFICATION_TERMINAL_HISTORY",
    "RUNTIME_JOB_CLASSIFICATIONS",
    "RuntimeJobRecovery",
    "RuntimeStartupRecovery",
    "recover_runtime_startup",
]
