"""RT-109 acceptance for the fenced startup recovery pass.

Every test here restarts for real: the owning connection is closed and a successor
service instance reopens the file and acquires a new fencing generation, exactly as a
crashed-and-restarted Core service would. What the successor then knows about the
previous one is only what the file holds, which is the whole point of the slice.

The seeds are the accepted ones. `m1` owns the workspace, `m18` owns the durable job
and the claim a run is admitted against, RT-106's scheduler makes the claims, and
RT-107's real command seam opens and resolves the waits -- so what recovery is tested
against is history this repository already knows how to produce.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as rt102
import test_rt104_runtime_command_transaction as rt104
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration, open_guard
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
from omnivia_core_runtime.service.runtime_recovery import (
    CLASSIFICATION_ACTIVE_CLAIM,
    CLASSIFICATION_CONTRADICTORY_HISTORY,
    CLASSIFICATION_DURABLE_OPEN_WAIT,
    CLASSIFICATION_NO_OPEN_ATTEMPT,
    CLASSIFICATION_ORPHAN_ATTEMPT,
    CLASSIFICATION_TERMINAL_HISTORY,
    RUNTIME_JOB_CLASSIFICATIONS,
    RuntimeStartupRecovery,
    recover_runtime_startup,
)
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeScheduler,
    RuntimeSchedulingError,
)
from omnivia_core_runtime.service.runtime_waits import (
    WaitOpening,
    open_runtime_wait,
    resolve_runtime_wait,
)
from omnivia_core_runtime.service.worker_adapter import HostLineage, WorkerAdapter
from omnivia_core_runtime.storage.agent_runtime import (
    admit_run,
    append_run_event,
    append_run_step,
    read_run,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    rebuild_runtime_run_summaries,
    runtime_run_summary_projection_digest,
)

from omnivia_core.contracts.v1 import Approval, ResolveWait, Wait

WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = m18.BASE_US

#: One claim, one suspension, one restart and one resolution, in that order. The
#: command instants sit beside RT-104's own settlement instant because the grants these
#: tests issue are RT-104's, and a grant has a validity window.
CLAIM_US = BASE_US + 1_000
OPEN_US = rt104.SETTLED_US + 1_000
RECOVER_US = OPEN_US + 1_000
RESOLVE_US = RECOVER_US + 1_000

#: Every relation a startup pass could reach, mutable and canonical alike. Compared as
#: a whole, because "left untouched" and "idempotent" are statements about the set.
STATE_TABLES = (
    "omnivia_durable_jobs",
    "omnivia_job_attempts",
    "omnivia_job_events",
    "omnivia_job_terminal_observations",
    "omnivia_application_job_controls",
    "omnivia_runtime_runs",
    "omnivia_runtime_run_steps",
    "omnivia_runtime_run_step_states",
    "omnivia_runtime_attempts",
    "omnivia_runtime_attempt_outcomes",
    "omnivia_runtime_waits",
    "omnivia_runtime_wait_resolutions",
    "omnivia_runtime_events",
    "omnivia_runtime_run_summaries",
)


def timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1_000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def clock_at(value: int) -> FakeClock:
    return FakeClock(
        monotonic=s0.MONOTONIC_BASE,
        wall=datetime.fromtimestamp(value / 1_000_000, tz=UTC),
    )


def state_of(holder: m1.Owned) -> dict[str, list[Any]]:
    """Every row of every relation this pass could write, for exact comparison."""
    return {
        table: holder.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in STATE_TABLES
    }


@dataclass
class Service:
    """One workspace file and whichever service instance currently owns it."""

    current: m1.Owned
    restarts: int = field(default=0)

    def restart(self) -> m1.Owned:
        """Close this owner's connection and reopen the file as a successor.

        A real restart, not a takeover on a live connection: the previous instance's
        connection is gone before the successor acquires the lease, so everything the
        successor can know it reads back out of the file.
        """
        previous = self.current
        previous.connection.close()
        self.restarts += 1
        identity = m1.make_identity(
            instance=f"svc-rt109-successor-{self.restarts}", pid=6109 + self.restarts
        )
        connection = open_database(previous.path, OpenMode.SERVICE_OWNED)
        lease = acquire_lease(
            connection,
            identity,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            holds_storage_lock=True,
            lock_mechanism="flock",
            predecessor=previous.identity.service_instance_id,
        )
        open_guard(
            connection,
            identity,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            fencing_generation=lease.fencing_generation,
        )
        self.current = m1.Owned(
            connection=connection,
            identity=identity,
            generation=lease.fencing_generation,
            path=previous.path,
        )
        return self.current


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    session = Service(m1.take_ownership(path))
    yield session
    session.current.connection.close()


def seed_run(
    holder: m1.Owned,
    *,
    job_id: str,
    run_id: str,
    step_id: str,
    state: str = "queued",
    max_attempts: int = 8,
) -> None:
    """One audited durable job, its claim, its canonical run and that run's first step."""
    audit_ref = m18.audit_ref_for(job_id)
    with m18.guarded(holder):
        holder.connection.execute(
            "INSERT OR IGNORE INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) VALUES "
            "(?, ?, 'core-service', 'runtime.admit', 'runtime.execute', ?, ?, ?, "
            "'{}', 'succeeded', NULL, ?)",
            (
                audit_ref,
                WORKSPACE_ID,
                f"req-{job_id}",
                f"cor-{job_id}",
                f"trc-{job_id}",
                BASE_US,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', ?, '{}', ?, ?, ?, ?)",
            (
                job_id,
                state,
                f"2039-09-18T23:06:{40 + len(job_id) % 20:02d}Z",
                "2039-09-18T23:06:40Z",
                holder.generation,
                holder.identity.service_instance_id,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_job_application_metadata "
            "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
            "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
            "max_attempts) VALUES (?, ?, 'ingestion.import', 'runtime.admit', ?, ?, "
            "NULL, 1, ?)",
            (WORKSPACE_ID, job_id, audit_ref, BASE_US, max_attempts),
        )
        m18.insert_claim(
            holder,
            claim_id=m18.claim_id_for(job_id),
            audit_ref=audit_ref,
            idempotency_key=m18.logical_key_for(job_id),
            workspace_id=WORKSPACE_ID,
        )
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=rt102.admission(run_id=run_id, job_id=job_id, event_id=f"evt-{run_id}"),
    )
    append_run_step(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        run_step_id=step_id,
        ordinal=1,
        step_kind="worker_invocation",
        created_at_us=BASE_US,
    )


def scheduler_at(holder: m1.Owned, at_us: int = RECOVER_US) -> RuntimeScheduler:
    return RuntimeScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        clock_at(at_us),
    )


def authority(holder: m1.Owned, key: str) -> tuple[Any, Any, Any]:
    context = rt104.authorize(idempotency_key=key)
    equivalence = rt104.equivalence_for(idempotency_key=key)
    return context, equivalence, rt104.issue(holder, context, equivalence=equivalence)


def open_wait(
    holder: m1.Owned,
    *,
    run_id: str,
    step_id: str,
    wait_id: str,
    sequence: int = 1,
    at_us: int = OPEN_US,
) -> None:
    """Suspend one running step on one durable wait, through RT-107's own seam."""
    context, equivalence, grant = authority(holder, f"rt109-open-{wait_id}")
    open_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        opening=WaitOpening(
            wait_id=wait_id,
            run_id=run_id,
            run_step_id=step_id,
            kind="external_signal",
            resume_digest=m18.DIGEST,
            runtime_event_id=f"evt-{wait_id}",
        ),
        validate_result=s0.accept_any,
        clock=clock_at(at_us),
        expected=RuntimeAggregateExpectation(run_id=run_id, sequence=sequence),
    )


def no_approval(_context: Any, _command: ResolveWait, _wait: Wait) -> Approval | None:
    """The fail-closed policy seam's answer for a resolution that carries no approval."""
    return None


def resolve_wait(
    holder: m1.Owned,
    *,
    run_id: str,
    wait_id: str,
    sequence: int,
    at_us: int = RESOLVE_US,
) -> Any:
    context, equivalence, grant = authority(holder, f"rt109-resolve-{wait_id}")
    return resolve_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=ResolveWait(
            workspace_id=WORKSPACE_ID,
            run_id=run_id,
            wait_id=wait_id,
            resolution="external_signal",
            approval_id=None,
            resume_digest=m18.DIGEST,
            requested_at=timestamp(at_us),
            reason="signal_received",
        ),
        policy=no_approval,
        runtime_event_id=f"evt-resolve-{wait_id}",
        validate_result=s0.accept_any,
        clock=clock_at(at_us),
        expected=RuntimeAggregateExpectation(run_id=run_id, sequence=sequence),
    )


def classifications(result: RuntimeStartupRecovery) -> dict[str, str]:
    return {job.job_id: job.classification for job in result.jobs}


def events_of(holder: m1.Owned, run_id: str) -> list[tuple[Any, ...]]:
    return holder.connection.execute(
        "SELECT sequence, event_kind, run_status FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence",
        (WORKSPACE_ID, run_id),
    ).fetchall()


def waiting_workspace(service: Service) -> m1.Owned:
    """One run claimed, suspended on a durable wait, then restarted under a successor."""
    seed_run(
        service.current,
        job_id="job-wait",
        run_id="run-wait",
        step_id="step-wait",
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    open_wait(
        service.current, run_id="run-wait", step_id="step-wait", wait_id="wait-0001"
    )
    return service.restart()


# --- what the pass reads out of persisted evidence ------------------------------


def test_a_queued_job_before_any_claim_has_no_open_attempt(service: Service) -> None:
    seed_run(
        service.current, job_id="job-queued", run_id="run-queued", step_id="step-queued"
    )
    successor = service.restart()
    before = state_of(successor)

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-queued": CLASSIFICATION_NO_OPEN_ATTEMPT}
    assert state_of(successor) == before


def test_a_claim_with_no_worker_progress_is_an_orphan_attempt(
    service: Service,
) -> None:
    seed_run(
        service.current, job_id="job-orphan", run_id="run-orphan", step_id="step-orphan"
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    successor = service.restart()

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-orphan": CLASSIFICATION_ORPHAN_ATTEMPT}
    assert result.jobs[0].requeued is True
    assert result.jobs[0].runtime_attempt_id == claim.runtime_attempt_id
    assert successor.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = 'job-orphan'"
    ).fetchone() == ("queued",)
    assert successor.connection.execute(
        "SELECT status FROM omnivia_runtime_attempt_outcomes WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("failed",)
    assert successor.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states WHERE run_step_id = ? "
        "ORDER BY state_sequence DESC LIMIT 1",
        ("step-orphan",),
    ).fetchone() == ("pending",)


def test_a_partial_nonterminal_stream_is_still_an_orphan_attempt(
    service: Service,
) -> None:
    seed_run(
        service.current,
        job_id="job-partial",
        run_id="run-partial",
        step_id="step-partial",
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    append_run_event(
        service.current.connection,
        service.current.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=service.current.generation,
        run_id="run-partial",
        runtime_event_id="evt-partial-progress",
        occurred_at_us=CLAIM_US + 100,
        event_kind="worker_event_observed",
        run_status="running",
        run_step_id="step-partial",
        message="a worker reported progress and then the service was lost",
    )
    successor = service.restart()

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-partial": CLASSIFICATION_ORPHAN_ATTEMPT}
    assert events_of(successor, "run-partial") == [
        (0, "run_admitted", "admitted"),
        (1, "attempt_started", "running"),
        (2, "worker_event_observed", "running"),
        (3, "attempt_interrupted", "running"),
    ]


def test_a_finished_run_is_terminal_history_and_is_not_touched(
    service: Service,
) -> None:
    seed_run(
        service.current, job_id="job-done", run_id="run-done", step_id="step-done"
    )
    scheduler = scheduler_at(service.current, CLAIM_US)
    claim = scheduler.claim_next()
    assert claim is not None
    scheduler.complete(claim, result_kind="runtime_completion", result={"ok": True})
    successor = service.restart()
    before = state_of(successor)

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-done": CLASSIFICATION_TERMINAL_HISTORY}
    assert result.jobs[0].superseded is False
    assert state_of(successor) == before


def test_a_claim_held_at_this_generation_is_active_and_is_not_recovered(
    service: Service,
) -> None:
    seed_run(
        service.current, job_id="job-live", run_id="run-live", step_id="step-live"
    )
    successor = service.restart()
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None
    before = state_of(successor)

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-live": CLASSIFICATION_ACTIVE_CLAIM}
    assert result.jobs[0].superseded is False
    assert state_of(successor) == before


def test_the_classification_vocabulary_is_closed(service: Service) -> None:
    assert RUNTIME_JOB_CLASSIFICATIONS == frozenset(
        {
            "active_claim",
            "durable_open_wait",
            "orphan_attempt",
            "no_open_attempt",
            "terminal_history",
            "contradictory_history",
        }
    )
    successor = waiting_workspace(service)

    result = recover_runtime_startup(scheduler_at(successor))

    assert {job.classification for job in result.jobs} <= RUNTIME_JOB_CLASSIFICATIONS
    assert result.classified(CLASSIFICATION_DURABLE_OPEN_WAIT) == result.jobs
    with pytest.raises(RuntimeSchedulingError, match="not a runtime recovery"):
        result.classified("recovered_somehow")


# --- adopting a durable open wait ------------------------------------------------


def test_a_superseded_open_wait_rebinds_only_the_claim(service: Service) -> None:
    successor = waiting_workspace(service)
    before = read_run(successor.connection, workspace_id=WORKSPACE_ID, run_id="run-wait")
    assert before is not None

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-wait": CLASSIFICATION_DURABLE_OPEN_WAIT}
    assert result.jobs[0].adopted is True
    assert result.jobs[0].wait_id == "wait-0001"
    assert successor.connection.execute(
        "SELECT state, claimed_by_service_instance, fencing_generation "
        "FROM omnivia_durable_jobs WHERE job_id = 'job-wait'"
    ).fetchone() == (
        "claimed",
        successor.identity.service_instance_id,
        successor.generation,
    )
    assert successor.connection.execute(
        "SELECT attempt_number, state FROM omnivia_job_attempts WHERE job_id = ?",
        ("job-wait",),
    ).fetchall() == [(1, "running")]

    after = read_run(successor.connection, workspace_id=WORKSPACE_ID, run_id="run-wait")
    assert after is not None
    assert after.status == "waiting"
    assert after.waits == before.waits
    assert after.steps == before.steps
    assert events_of(successor, "run-wait") == [
        (0, "run_admitted", "admitted"),
        (1, "attempt_started", "running"),
        (2, "wait_opened", "waiting"),
        (3, "wait_adopted", "waiting"),
    ]
    adopted = successor.connection.execute(
        "SELECT details_json FROM omnivia_runtime_events WHERE run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        ("run-wait",),
    ).fetchone()
    assert adopted is not None
    assert successor.identity.service_instance_id in str(adopted[0])
    assert "wait-0001" in str(adopted[0])


def test_resolving_the_adopted_wait_resumes_the_same_step_and_attempt(
    service: Service,
) -> None:
    successor = waiting_workspace(service)
    before = read_run(successor.connection, workspace_id=WORKSPACE_ID, run_id="run-wait")
    assert before is not None
    attempts = before.steps[0].attempts
    recover_runtime_startup(scheduler_at(successor))

    outcome = resolve_wait(
        successor, run_id="run-wait", wait_id="wait-0001", sequence=3
    )

    assert outcome.result["status"] == "resolved"
    resumed = read_run(
        successor.connection, workspace_id=WORKSPACE_ID, run_id="run-wait"
    )
    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.steps[0].run_step_id == "step-wait"
    assert resumed.steps[0].status == "running"
    assert [attempt.attempt_id for attempt in resumed.steps[0].attempts] == [
        attempt.attempt_id for attempt in attempts
    ]
    assert resumed.steps[0].attempts[-1].status == "running"


def test_an_open_wait_is_never_handed_to_the_stranded_job_recovery(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting job is a stale claim, and the all-stale pass would have taken it."""
    successor = waiting_workspace(service)
    seed_run(
        successor, job_id="job-also-orphan", run_id="run-also", step_id="step-also"
    )
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None and claim.job_id == "job-also-orphan"
    second = service.restart()
    allowlists: list[Any] = []
    recover_locked = RuntimeScheduler._recover_stranded_locked

    def record(scheduler: RuntimeScheduler, *, job_ids: Any = None) -> Any:
        allowlists.append(job_ids)
        return recover_locked(scheduler, job_ids=job_ids)

    monkeypatch.setattr(RuntimeScheduler, "_recover_stranded_locked", record)

    result = recover_runtime_startup(scheduler_at(second))

    assert allowlists == [("job-also-orphan",)]
    assert classifications(result) == {
        "job-wait": CLASSIFICATION_DURABLE_OPEN_WAIT,
        "job-also-orphan": CLASSIFICATION_ORPHAN_ATTEMPT,
    }
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes WHERE attempt_id IN "
        "(SELECT attempt_id FROM omnivia_runtime_attempts WHERE run_id = 'run-wait')"
    ).fetchone() == (0,)
    assert second.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states WHERE run_step_id = ? "
        "ORDER BY state_sequence DESC LIMIT 1",
        ("step-wait",),
    ).fetchone() == ("waiting",)


# --- the bounded recovery this pass reuses ---------------------------------------


def test_attempt_exhaustion_fails_the_run_instead_of_requeueing(
    service: Service,
) -> None:
    seed_run(
        service.current,
        job_id="job-last",
        run_id="run-last",
        step_id="step-last",
        max_attempts=1,
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    successor = service.restart()

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {"job-last": CLASSIFICATION_ORPHAN_ATTEMPT}
    assert result.jobs[0].requeued is False
    assert successor.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = 'job-last'"
    ).fetchone() == ("failed",)
    assert successor.connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE run_id = 'run-last' "
        "ORDER BY sequence DESC LIMIT 1"
    ).fetchone() == ("failed",)


def test_an_empty_or_foreign_allowlist_recovers_nothing(service: Service) -> None:
    seed_run(
        service.current, job_id="job-listed", run_id="run-listed", step_id="step-listed"
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    successor = service.restart()
    scheduler = scheduler_at(successor)
    before = state_of(successor)

    assert scheduler.recover_stranded(job_ids=()) == ()
    assert scheduler.recover_stranded(job_ids=("job-somewhere-else",)) == ()
    assert state_of(successor) == before

    recovered = scheduler.recover_stranded(job_ids=("job-listed",))

    assert [job.job_id for job in recovered] == ["job-listed"]
    assert scheduler.recover_stranded() == ()


def test_mixed_waiting_orphaned_and_queued_jobs_settle_in_one_pass(
    service: Service,
) -> None:
    successor = waiting_workspace(service)
    seed_run(successor, job_id="job-mix-orphan", run_id="run-mix-o", step_id="step-o")
    seed_run(successor, job_id="job-mix-queued", run_id="run-mix-q", step_id="step-q")
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None and claim.job_id == "job-mix-orphan"
    second = service.restart()

    result = recover_runtime_startup(scheduler_at(second))

    assert classifications(result) == {
        "job-wait": CLASSIFICATION_DURABLE_OPEN_WAIT,
        "job-mix-orphan": CLASSIFICATION_ORPHAN_ATTEMPT,
        "job-mix-queued": CLASSIFICATION_NO_OPEN_ATTEMPT,
    }
    assert second.connection.execute(
        "SELECT job_id, state FROM omnivia_durable_jobs ORDER BY job_id"
    ).fetchall() == [
        ("job-mix-orphan", "queued"),
        ("job-mix-queued", "queued"),
        ("job-wait", "claimed"),
    ]


# --- what the pass refuses to do -------------------------------------------------


def test_a_contradictory_history_is_left_exactly_as_it_is(service: Service) -> None:
    seed_run(
        service.current,
        job_id="job-contradictory",
        run_id="run-contradictory",
        step_id="step-contradictory",
        state="claimed",
    )
    seed_run(
        service.current, job_id="job-sound", run_id="run-sound", step_id="step-sound"
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None and claim.job_id == "job-sound"
    successor = service.restart()
    untouched = "SELECT * FROM omnivia_durable_jobs WHERE job_id = 'job-contradictory'"
    before = successor.connection.execute(untouched).fetchone()

    result = recover_runtime_startup(scheduler_at(successor))

    assert classifications(result) == {
        "job-contradictory": CLASSIFICATION_CONTRADICTORY_HISTORY,
        "job-sound": CLASSIFICATION_ORPHAN_ATTEMPT,
    }
    contradictory = result.classified(CLASSIFICATION_CONTRADICTORY_HISTORY)[0]
    assert contradictory.detail is not None
    assert contradictory.adopted is False and contradictory.requeued is None
    assert successor.connection.execute(untouched).fetchone() == before
    assert events_of(successor, "run-contradictory") == [
        (0, "run_admitted", "admitted")
    ]


def test_a_superseded_owner_recovers_nothing(service: Service) -> None:
    seed_run(
        service.current, job_id="job-fenced", run_id="run-fenced", step_id="step-fenced"
    )
    claim = scheduler_at(service.current, CLAIM_US).claim_next()
    assert claim is not None
    superseded = service.current.identity
    superseded_generation = service.current.generation
    successor = service.restart()
    before = state_of(successor)
    stale = RuntimeScheduler(
        successor.connection,
        superseded,
        WORKSPACE_ID,
        superseded_generation,
        clock_at(RECOVER_US),
    )

    with pytest.raises(StaleGeneration):
        recover_runtime_startup(stale)

    assert state_of(successor) == before


def test_a_failure_anywhere_in_the_pass_rolls_the_whole_pass_back(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption has already written by the time orphan recovery runs, and still rolls back."""
    successor = waiting_workspace(service)
    seed_run(successor, job_id="job-atomic", run_id="run-atomic", step_id="step-atomic")
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None
    second = service.restart()
    before = state_of(second)

    def fail(_scheduler: RuntimeScheduler, *, job_ids: Any = None) -> Any:
        raise RuntimeError("injected orphan recovery failure")

    monkeypatch.setattr(RuntimeScheduler, "_recover_stranded_locked", fail)

    with pytest.raises(RuntimeError, match="injected orphan recovery failure"):
        recover_runtime_startup(scheduler_at(second))

    assert state_of(second) == before


def test_no_startup_path_manufactures_success_from_an_absent_worker(
    service: Service,
) -> None:
    """A worker session this process never held is not evidence that a turn finished."""
    successor = waiting_workspace(service)
    seed_run(successor, job_id="job-absent", run_id="run-absent", step_id="step-absent")
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None
    second = service.restart()
    adapter = WorkerAdapter()
    lineage = HostLineage(
        workspace_id=WORKSPACE_ID,
        run_id=claim.run_id,
        run_step_id=claim.run_step_id,
        attempt_id=claim.runtime_attempt_id,
    )
    assert adapter.session_count == 0  # nothing in memory knows this attempt

    recover_runtime_startup(scheduler_at(second))

    assert adapter.session_count == 0 and lineage.run_id == claim.run_id
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes WHERE status = 'succeeded'"
    ).fetchone() == (0,)
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_events WHERE run_status = 'succeeded'"
    ).fetchone() == (0,)
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_durable_jobs WHERE state = 'succeeded'"
    ).fetchone() == (0,)
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_attempts WHERE state = 'succeeded'"
    ).fetchone() == (0,)
    assert second.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_terminal_observations "
        "WHERE terminal_state = 'succeeded'"
    ).fetchone() == (0,)


# --- repeating the pass, and the projection it leaves behind ----------------------


def test_a_second_pass_reclassifies_and_writes_nothing(service: Service) -> None:
    successor = waiting_workspace(service)
    seed_run(successor, job_id="job-again", run_id="run-again", step_id="step-again")
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None
    second = service.restart()
    scheduler = scheduler_at(second)

    first_result = recover_runtime_startup(scheduler)
    settled = state_of(second)
    second_result = recover_runtime_startup(scheduler)

    assert state_of(second) == settled
    assert classifications(first_result) == {
        "job-wait": CLASSIFICATION_DURABLE_OPEN_WAIT,
        "job-again": CLASSIFICATION_ORPHAN_ATTEMPT,
    }
    assert classifications(second_result) == {
        "job-wait": CLASSIFICATION_DURABLE_OPEN_WAIT,
        "job-again": CLASSIFICATION_NO_OPEN_ATTEMPT,
    }
    assert [job.adopted for job in second_result.jobs] == [False, False]
    assert [job.requeued for job in second_result.jobs] == [None, None]


def test_the_pass_preserves_live_and_rebuilt_projection_equivalence(
    service: Service,
) -> None:
    successor = waiting_workspace(service)
    seed_run(successor, job_id="job-proj", run_id="run-proj", step_id="step-proj")
    claim = scheduler_at(successor, CLAIM_US).claim_next()
    assert claim is not None
    second = service.restart()

    recover_runtime_startup(scheduler_at(second))
    live = runtime_run_summary_projection_digest(
        second.connection, workspace_id=WORKSPACE_ID
    )
    rebuild = rebuild_runtime_run_summaries(
        second.connection,
        second.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=second.generation,
    )

    assert rebuild.record_count == 2
    assert rebuild.build_digest == live
    assert (
        runtime_run_summary_projection_digest(
            second.connection, workspace_id=WORKSPACE_ID
        )
        == live
    )
