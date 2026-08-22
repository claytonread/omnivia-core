"""RT-109 acceptance for startup recovery and orphan-attempt classification.

Every crash point is reached by actually crashing: the SQLite connection is closed
and the file is reopened by a *different* service instance which acquires a higher
fencing generation, which is what a process restart is here. Nothing is simulated by
hand-editing rows into the state a crash would have left; the state is whatever the
real scheduler, the real wait writes and the real fence left behind when the process
went away.

The single property behind all of it: recovery reads evidence and never invents it.
No Run, Step, Attempt or job is marked succeeded by this pass, an absent worker
session is never read as a completion, and an item whose history contradicts itself
is reported and left exactly as found.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt104_runtime_command_transaction as rt104
import test_rt106_runtime_scheduler as rt106
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
from omnivia_core_runtime.service.runtime_recovery import (
    CLASSIFICATION_ACTIVE_CLAIM,
    CLASSIFICATION_CONTRADICTORY_HISTORY,
    CLASSIFICATION_DURABLE_OPEN_WAIT,
    CLASSIFICATION_NO_OPEN_ATTEMPT,
    CLASSIFICATION_ORPHAN_ATTEMPT,
    CLASSIFICATION_TERMINAL_HISTORY,
    RECOVERY_CLASSIFICATIONS,
    StartupRecoveryReport,
    recover_at_startup,
)
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeClaim,
    RuntimeScheduler,
)
from omnivia_core_runtime.service.runtime_waits import resolve_runtime_wait
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    open_wait,
    read_run,
    read_run_sequence,
    record_step_status,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    rebuild_runtime_run_summaries,
    runtime_run_summary_projection_digest,
)

from omnivia_core.contracts.v1 import ResolveWait

WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = m18.BASE_US
DIGEST = m18.DIGEST

WAIT_OPEN_US = BASE_US + 2_000
RECOVER_US = BASE_US + 10_000
#: Inside the mutation grant's own window, which `rt104` issues at its settled
#: instant. A resolution is a real RT-104 command here, not a storage write.
RESOLVE_US = rt104.SETTLED_US + 10_000

#: Everything a startup pass could possibly change. Counted as one set, because
#: "this classification changed nothing" is a statement about the whole ledger.
LEDGER_TABLES = (
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
)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- crashing, and what comes back up -------------------------------------------


def timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1_000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def clock_at(value: int) -> m1.FakeClock:
    return m1.FakeClock(wall=datetime.fromtimestamp(value / 1_000_000, tz=UTC))


def restart(holder: m1.Owned, *, instance: str = "svc-rt109-successor") -> m1.Owned:
    """Close the database and bring it back up under a new instance and fence.

    The connection really is closed, so nothing the previous process held in memory
    survives -- including RT-108's adapter sessions, which is the point: what the
    successor knows is what the file says.
    """
    path = holder.path
    holder.connection.close()
    successor = m1.make_identity(instance=instance, pid=6109)
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        successor,
        clock=clock_at(RECOVER_US),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=holder.identity.service_instance_id,
    )
    m1.open_guard(
        connection,
        successor,
        clock=clock_at(RECOVER_US),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return m1.Owned(
        connection=connection,
        identity=successor,
        generation=lease.fencing_generation,
        path=path,
    )


def scheduler_at(holder: m1.Owned, *, now_us: int = RECOVER_US) -> RuntimeScheduler:
    return RuntimeScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        clock_at(now_us),
    )


@dataclass(frozen=True)
class Seeded:
    """One runtime-bound job, its run and the single step the tests drive."""

    job_id: str
    run_id: str
    step_id: str


def seed(holder: m1.Owned, name: str, *, max_attempts: int = 8) -> Seeded:
    seeded = Seeded(f"job-{name}", f"run-{name}", f"step-{name}")
    rt106._seed_run(
        holder,
        job_id=seeded.job_id,
        run_id=seeded.run_id,
        step_id=seeded.step_id,
        max_attempts=max_attempts,
    )
    return seeded


def claim(holder: m1.Owned) -> RuntimeClaim:
    claimed = scheduler_at(holder, now_us=BASE_US + 1_000).claim_next()
    assert claimed is not None
    return claimed


def suspend(holder: m1.Owned, claimed: RuntimeClaim, *, wait_id: str) -> str:
    """Open one durable wait over the claimed attempt, as RT-107's three writes do.

    The order is the migration's: the wait exists before the step may say it is
    waiting, and the run's event stream states `waiting` last.
    """
    open_wait(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        wait_id=wait_id,
        run_id=claimed.run_id,
        run_step_id=claimed.run_step_id,
        kind="external_signal",
        created_at_us=WAIT_OPEN_US,
        resume_digest=DIGEST,
    )
    record_step_status(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_step_id=claimed.run_step_id,
        status="waiting",
        observed_at_us=WAIT_OPEN_US,
    )
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=claimed.run_id,
        runtime_event_id=f"evt-{wait_id}-opened",
        occurred_at_us=WAIT_OPEN_US,
        event_kind="wait_opened",
        run_status="waiting",
        run_step_id=claimed.run_step_id,
    )
    return wait_id


def stream_partial_worker_evidence(holder: m1.Owned, claimed: RuntimeClaim) -> str:
    """Persist one nonterminal worker observation, then stop mid-stream.

    This is the only thing a partial worker stream leaves behind that outlives the
    process: RT-108's adapter holds its session in memory and the restart destroys
    it. The event says a message was seen; it says nothing about a turn completing.
    """
    event_id = f"evt-{claimed.run_id}-partial"
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=claimed.run_id,
        runtime_event_id=event_id,
        occurred_at_us=BASE_US + 3_000,
        event_kind="worker_message_observed",
        run_status="running",
        run_step_id=claimed.run_step_id,
        message="a partial worker message was persisted before the process stopped",
    )
    return event_id


# --- reading the ledger back ----------------------------------------------------


def ledger(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in LEDGER_TABLES}


def ledger_rows(holder: m1.Owned) -> dict[str, list[str]]:
    """Every stored row of every table a pass could touch, not merely how many.

    A count proves nothing was inserted; this proves nothing was rewritten either,
    which is what "left exactly as found" has to mean for a refusal.
    """
    return {
        table: sorted(
            repr(row)
            for row in holder.connection.execute(f"SELECT * FROM {table}").fetchall()
        )
        for table in LEDGER_TABLES
    }


def job_row(holder: m1.Owned, job_id: str) -> tuple[Any, ...]:
    row = holder.connection.execute(
        "SELECT state, claimed_by_service_instance, fencing_generation "
        "FROM omnivia_durable_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def classification_of(report: StartupRecoveryReport, job_id: str) -> str:
    matched = [item for item in report.classifications if item.job_id == job_id]
    assert len(matched) == 1, matched
    return matched[0].classification


def assert_nothing_succeeded(holder: m1.Owned) -> None:
    """No pass may leave a success behind that no worker ever reported."""
    assert holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes WHERE status = 'succeeded'"
    ).fetchone() == (0,)
    assert holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_run_step_states WHERE status = 'succeeded'"
    ).fetchone() == (0,)
    assert holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_events WHERE run_status = 'succeeded'"
    ).fetchone() == (0,)
    assert holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_durable_jobs WHERE state = 'succeeded'"
    ).fetchone() == (0,)
    assert holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_attempts WHERE state = 'succeeded'"
    ).fetchone() == (0,)


# --- the crash points -----------------------------------------------------------


def test_crash_before_the_claim_invents_no_work(owned: m1.Owned) -> None:
    seeded = seed(owned, "before-claim")
    successor = restart(owned)
    before = ledger(successor)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, seeded.job_id) == CLASSIFICATION_NO_OPEN_ATTEMPT
    assert report.adoptions == () and report.recoveries == ()
    assert ledger(successor) == before
    assert job_row(successor, seeded.job_id)[0] == "queued"
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_crash_after_the_claim_before_worker_start_recovers_the_orphan(
    owned: m1.Owned,
) -> None:
    seed(owned, "after-claim")
    claimed = claim(owned)
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, claimed.job_id) == CLASSIFICATION_ORPHAN_ATTEMPT
    assert len(report.recoveries) == 1
    assert report.recoveries[0].runtime_attempt_id == claimed.runtime_attempt_id
    assert report.recoveries[0].requeued is True
    assert report.adoptions == ()
    assert job_row(successor, claimed.job_id) == ("queued", None, successor.generation)
    assert successor.connection.execute(
        "SELECT status FROM omnivia_runtime_attempt_outcomes WHERE attempt_id = ?",
        (claimed.runtime_attempt_id,),
    ).fetchone() == ("failed",)
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_crash_during_a_partial_worker_stream_retains_its_evidence(
    owned: m1.Owned,
) -> None:
    seed(owned, "partial-stream")
    claimed = claim(owned)
    partial_event_id = stream_partial_worker_evidence(owned, claimed)
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, claimed.job_id) == CLASSIFICATION_ORPHAN_ATTEMPT
    assert report.recoveries[0].requeued is True
    # The partial observation survives untouched, and is not read as a completion.
    assert successor.connection.execute(
        "SELECT event_kind, run_status FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND runtime_event_id = ?",
        (WORKSPACE_ID, partial_event_id),
    ).fetchone() == ("worker_message_observed", "running")
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_open_wait_survives_a_restart_and_is_adopted_not_recovered(
    owned: m1.Owned,
) -> None:
    seed(owned, "waiting")
    claimed = claim(owned)
    wait_id = suspend(owned, claimed, wait_id="wait-rt109-waiting")
    stale_generation = owned.generation
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, claimed.job_id) == CLASSIFICATION_DURABLE_OPEN_WAIT
    assert report.recoveries == ()
    assert len(report.adoptions) == 1
    adopted = report.adoptions[0]
    assert adopted.wait_id == wait_id
    assert adopted.runtime_attempt_id == claimed.runtime_attempt_id
    assert adopted.previous_fencing_generation == stale_generation

    # Only the claim moved. The wait, the step and the attempt are the ones that
    # were already there.
    assert job_row(successor, claimed.job_id) == (
        "claimed",
        successor.identity.service_instance_id,
        successor.generation,
    )
    snapshot = read_run(
        successor.connection, workspace_id=WORKSPACE_ID, run_id=claimed.run_id
    )
    assert snapshot is not None
    assert snapshot.status == "waiting"
    assert snapshot.waits[0].wait_id == wait_id
    assert snapshot.waits[0].status == "pending"
    assert snapshot.steps[0].status == "waiting"
    assert [attempt.attempt_id for attempt in snapshot.steps[0].attempts] == [
        claimed.runtime_attempt_id
    ]
    assert snapshot.steps[0].attempts[-1].status == "running"
    assert snapshot.events[-1].event_kind == "wait_adopted"
    assert snapshot.events[-1].run_status == "waiting"
    assert successor.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes"
    ).fetchone() == (0,)
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_resolve_wait_after_adoption_resumes_the_same_step_and_attempt(
    owned: m1.Owned,
) -> None:
    seed(owned, "resume")
    claimed = claim(owned)
    wait_id = suspend(owned, claimed, wait_id="wait-rt109-resume")
    successor = restart(owned)
    recover_at_startup(scheduler_at(successor))
    job_before = job_row(successor, claimed.job_id)

    key = "rt109-resolve-0001"
    context = rt104.authorize(idempotency_key=key)
    equivalence = rt104.equivalence_for(idempotency_key=key)
    outcome = resolve_runtime_wait(
        successor.connection,
        successor.identity,
        grant=rt104.issue(successor, context, equivalence=equivalence),
        context=context,
        equivalence=equivalence,
        command=ResolveWait(
            workspace_id=WORKSPACE_ID,
            run_id=claimed.run_id,
            wait_id=wait_id,
            resolution="external_signal",
            approval_id=None,
            resume_digest=DIGEST,
            requested_at=timestamp(RESOLVE_US),
            reason="signal_received",
        ),
        policy=lambda _context, _command, _wait: None,
        runtime_event_id=f"evt-{key}",
        validate_result=s0.accept_any,
        clock=clock_at(RESOLVE_US),
        expected=RuntimeAggregateExpectation(
            run_id=claimed.run_id,
            sequence=read_run_sequence(
                successor.connection, workspace_id=WORKSPACE_ID, run_id=claimed.run_id
            ),
        ),
    )

    assert outcome.result["status"] == "resolved"
    snapshot = read_run(
        successor.connection, workspace_id=WORKSPACE_ID, run_id=claimed.run_id
    )
    assert snapshot is not None
    assert snapshot.status == "running"
    assert snapshot.steps[0].run_step_id == claimed.run_step_id
    assert snapshot.steps[0].status == "running"
    # The same attempt, not a replacement, and no requeue: the job's claim is the
    # one adoption left in place.
    assert [attempt.attempt_id for attempt in snapshot.steps[0].attempts] == [
        claimed.runtime_attempt_id
    ]
    assert snapshot.steps[0].attempts[-1].status == "running"
    assert job_row(successor, claimed.job_id) == job_before
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_terminal_history_is_observed_and_never_re_settled(owned: m1.Owned) -> None:
    seed(owned, "terminal")
    claimed = claim(owned)
    scheduler_at(owned, now_us=BASE_US + 1_000).complete(
        claimed, result_kind="runtime_completion", result={"outcome": "complete"}
    )
    successor = restart(owned)
    before = ledger(successor)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, claimed.job_id) == CLASSIFICATION_TERMINAL_HISTORY
    assert report.adoptions == () and report.recoveries == ()
    assert ledger(successor) == before
    successor.connection.close()


def test_attempt_exhaustion_fails_the_orphan_without_requeueing(
    owned: m1.Owned,
) -> None:
    seed(owned, "exhausted", max_attempts=1)
    claimed = claim(owned)
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert classification_of(report, claimed.job_id) == CLASSIFICATION_ORPHAN_ATTEMPT
    assert len(report.recoveries) == 1 and report.recoveries[0].requeued is False
    assert job_row(successor, claimed.job_id)[0] == "failed"
    assert successor.connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (claimed.run_id,),
    ).fetchone() == ("failed",)
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_mixed_waiting_and_orphan_jobs_are_settled_independently(
    owned: m1.Owned,
) -> None:
    seed(owned, "mixed-a-waiting")
    waiting_claim = claim(owned)
    suspend(owned, waiting_claim, wait_id="wait-rt109-mixed")
    seed(owned, "mixed-b-orphan")
    orphan_claim = claim(owned)
    seed(owned, "mixed-c-queued")
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert {item.job_id: item.classification for item in report.classifications} == {
        waiting_claim.job_id: CLASSIFICATION_DURABLE_OPEN_WAIT,
        orphan_claim.job_id: CLASSIFICATION_ORPHAN_ATTEMPT,
        "job-mixed-c-queued": CLASSIFICATION_NO_OPEN_ATTEMPT,
    }
    assert [adoption.job_id for adoption in report.adoptions] == [waiting_claim.job_id]
    assert [recovery.job_id for recovery in report.recoveries] == [orphan_claim.job_id]

    # The waiting job keeps its attempt; only the orphan's is failed.
    assert successor.connection.execute(
        "SELECT attempt_id FROM omnivia_runtime_attempt_outcomes"
    ).fetchall() == [(orphan_claim.runtime_attempt_id,)]
    assert job_row(successor, waiting_claim.job_id) == (
        "claimed",
        successor.identity.service_instance_id,
        successor.generation,
    )
    assert job_row(successor, "job-mixed-c-queued")[0] == "queued"
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_contradictory_history_is_fail_closed_and_mutates_nothing(
    owned: m1.Owned,
) -> None:
    seed(owned, "contradictory")
    claimed = claim(owned)
    # A running runtime attempt whose durable job says it was never claimed.
    with m18.guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'queued' WHERE job_id = ?",
            (claimed.job_id,),
        )
    successor = restart(owned)
    before = ledger(successor)

    report = recover_at_startup(scheduler_at(successor))

    item = next(
        candidate
        for candidate in report.classifications
        if candidate.job_id == claimed.job_id
    )
    assert item.classification == CLASSIFICATION_CONTRADICTORY_HISTORY
    assert item.runtime_attempt_id == claimed.runtime_attempt_id
    assert report.adoptions == () and report.recoveries == ()
    assert ledger(successor) == before
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_an_uncertain_run_is_refused_rather_than_recovered_as_an_orphan(
    owned: m1.Owned,
) -> None:
    """A stale claim shaped exactly like an orphan, except the run says `uncertain`.

    One running step, one running attempt, no wait: the only difference from
    `test_crash_after_the_claim_before_worker_start_recovers_the_orphan` is the run's
    own status, and that difference alone has to stop the pass. `uncertain` is an open
    question about work nobody can account for, so failing the attempt and requeueing
    the job would settle a question the ledger has not answered.
    """
    seed(owned, "uncertain")
    claimed = claim(owned)
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=claimed.run_id,
        runtime_event_id=f"evt-{claimed.run_id}-uncertain",
        occurred_at_us=BASE_US + 4_000,
        event_kind="run_marked_uncertain",
        run_status="uncertain",
        run_step_id=claimed.run_step_id,
        message="the run's outcome was unaccounted for when the process stopped",
    )
    successor = restart(owned)
    before = ledger_rows(successor)

    report = recover_at_startup(scheduler_at(successor))

    item = next(
        candidate
        for candidate in report.classifications
        if candidate.job_id == claimed.job_id
    )
    assert item.classification == CLASSIFICATION_CONTRADICTORY_HISTORY
    assert item.runtime_attempt_id == claimed.runtime_attempt_id
    assert report.adoptions == () and report.recoveries == ()

    # Nothing was settled: no outcome for the attempt, no requeue, no event appended.
    assert ledger_rows(successor) == before
    snapshot = read_run(
        successor.connection, workspace_id=WORKSPACE_ID, run_id=claimed.run_id
    )
    assert snapshot is not None
    assert snapshot.status == "uncertain"
    assert snapshot.steps[0].status == "running"
    assert snapshot.steps[0].attempts[-1].status == "running"
    assert snapshot.events[-1].event_kind == "run_marked_uncertain"
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_a_claim_from_a_later_generation_is_refused_not_read_as_active(
    owned: m1.Owned,
) -> None:
    """Only equality is a live claim; a generation beyond this pass's is a refusal.

    Reading `>` as `active_claim` would report a claim this pass has no standing to
    speak for as the healthy one, and would hide the drift behind a benign class.
    """
    seed(owned, "future-generation")
    claimed = claim(owned)
    successor = restart(owned)
    with m18.guarded(successor):
        successor.connection.execute(
            "UPDATE omnivia_durable_jobs SET fencing_generation = ? WHERE job_id = ?",
            (successor.generation + 5, claimed.job_id),
        )
    before = ledger_rows(successor)

    report = recover_at_startup(scheduler_at(successor))

    assert (
        classification_of(report, claimed.job_id) == CLASSIFICATION_CONTRADICTORY_HISTORY
    )
    assert report.adoptions == () and report.recoveries == ()
    assert ledger_rows(successor) == before
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_repeated_startup_recovery_changes_nothing_the_second_time(
    owned: m1.Owned,
) -> None:
    seed(owned, "repeat-waiting")
    waiting_claim = claim(owned)
    suspend(owned, waiting_claim, wait_id="wait-rt109-repeat")
    seed(owned, "repeat-orphan")
    orphan_claim = claim(owned)
    successor = restart(owned)

    first = recover_at_startup(scheduler_at(successor))
    settled = ledger(successor)

    second = recover_at_startup(scheduler_at(successor, now_us=RECOVER_US + 5_000))

    assert len(first.adoptions) == 1 and len(first.recoveries) == 1
    assert second.adoptions == () and second.recoveries == ()
    assert ledger(successor) == settled
    assert classification_of(second, waiting_claim.job_id) == (
        CLASSIFICATION_ACTIVE_CLAIM
    )
    assert classification_of(second, orphan_claim.job_id) == (
        CLASSIFICATION_NO_OPEN_ATTEMPT
    )
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_a_superseded_owner_cannot_run_startup_recovery(owned: m1.Owned) -> None:
    seed(owned, "stale-fence")
    claim(owned)
    stale = scheduler_at(owned)
    successor = restart(owned)
    # The stale scheduler still holds the same file handle only because the test
    # keeps it; its generation is the one that was superseded.
    stale.connection = successor.connection
    before = ledger(successor)

    with pytest.raises(StaleGeneration):
        recover_at_startup(stale)

    assert ledger(successor) == before
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_the_recovery_allowlist_ignores_identifiers_this_workspace_does_not_hold(
    owned: m1.Owned,
) -> None:
    """A foreign or empty allowlist recovers nothing rather than everything."""
    seed(owned, "allowlist")
    claimed = claim(owned)
    successor = restart(owned)
    stale_claim = job_row(successor, claimed.job_id)
    scheduler = scheduler_at(successor)

    assert scheduler.recover_stranded(job_ids=()) == ()
    assert scheduler.recover_stranded(job_ids=("job-of-another-workspace",)) == ()
    assert job_row(successor, claimed.job_id) == stale_claim

    assert len(scheduler.recover_stranded(job_ids=(claimed.job_id,))) == 1
    assert job_row(successor, claimed.job_id)[0] == "queued"
    assert_nothing_succeeded(successor)
    successor.connection.close()


def test_recovery_preserves_replay_and_live_projection_equivalence(
    owned: m1.Owned,
) -> None:
    seed(owned, "projection-waiting")
    waiting_claim = claim(owned)
    suspend(owned, waiting_claim, wait_id="wait-rt109-projection")
    seed(owned, "projection-orphan")
    claim(owned)
    successor = restart(owned)

    recover_at_startup(scheduler_at(successor))

    live = runtime_run_summary_projection_digest(
        successor.connection, workspace_id=WORKSPACE_ID
    )
    replayed = rebuild_runtime_run_summaries(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
    )
    assert replayed.build_digest == live
    assert (
        runtime_run_summary_projection_digest(
            successor.connection, workspace_id=WORKSPACE_ID
        )
        == live
    )
    successor.connection.close()


def test_the_classification_vocabulary_is_closed(owned: m1.Owned) -> None:
    seed(owned, "vocabulary")
    claim(owned)
    successor = restart(owned)

    report = recover_at_startup(scheduler_at(successor))

    assert report.classifications
    assert {
        item.classification for item in report.classifications
    } <= RECOVERY_CLASSIFICATIONS
    assert report.with_classification(CLASSIFICATION_ORPHAN_ATTEMPT) == tuple(
        item
        for item in report.classifications
        if item.classification == CLASSIFICATION_ORPHAN_ATTEMPT
    )
    successor.connection.close()
