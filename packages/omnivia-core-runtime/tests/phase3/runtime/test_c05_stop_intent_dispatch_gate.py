"""C05 acceptance: a recorded stop intent inhibits dispatch at the production seam.

T-0688 states the rule and `service/material_dispatch_safety.py` holds the mechanism, but
a rule nothing in production consults is a rule about tests. These are the tests for the
consultation itself: migration 0025's durable stop request, read at
:meth:`RuntimeScheduler._open_attempt`, which is the one place a claimed step's work is
handed to something that executes it -- on a fresh claim, on the advance out of a step
that just succeeded, and on a retry.

Real SQLite through the real migrator, the real lease and guard, and the real stop
writer throughout. Nothing here stubs the ledger, because the whole claim under test is
that the gate reads it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt106_runtime_scheduler as rt106
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.material_dispatch_safety import (
    STOP_RUN_STOP_REQUESTED,
)
from omnivia_core_runtime.service.runtime_scheduler import RuntimeStopRequested
from omnivia_core_runtime.storage.agent_runtime import append_run_step
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.runtime_stop import (
    RunStopRequest,
    runtime_stop_writer,
)

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US

_seed_run = rt106._seed_run
_scheduler = rt106._scheduler


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- helpers -------------------------------------------------------------------------


def _record_stop_intent(
    holder: m1.Owned, *, job_id: str, run_id: str, stop_request_id: str
) -> None:
    """Record the durable no-further-dispatch intent, and settle nothing.

    Exactly what `workflow.control`'s `cancel` leaves behind for a Run it cannot settle:
    a 0025 request with no outcome. The audit reference is the one `_seed_run` already
    recorded for this job, because 0025's foreign key requires a real one.
    """
    with runtime_stop_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.record_stop_intent(
            RunStopRequest(
                stop_request_id=stop_request_id,
                run_id=run_id,
                requested_at_us=BASE_US + 500,
                requested_by="core-operator",
                reason="operator.cancelled",
                audit_ref=m18.audit_ref_for(job_id),
            )
        )


def _restart(holder: m1.Owned) -> m1.Owned:
    """Drop this service's connection entirely and adopt the workspace on a new one.

    The strongest available reading of "another connection": the writing connection is
    closed before the reading one exists, so nothing in process memory can carry the
    intent across. `SERVICE_OWNED` holds an exclusive lock, so this is also the only
    reading available.
    """
    holder.connection.close()
    successor = m1.make_identity(instance="svc-c05-gate-successor", pid=9105)
    clock = m1.FakeClock(
        wall=datetime.fromtimestamp((BASE_US + 1_000) / 1_000_000, UTC)
    )
    connection = open_database(holder.path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=holder.identity.service_instance_id,
    )
    m1.open_guard(
        connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return m1.Owned(
        connection=connection,
        identity=successor,
        generation=lease.fencing_generation,
        path=holder.path,
    )


def _counts(holder: m1.Owned, job_id: str, run_id: str) -> tuple[str, int, int]:
    """The durable job's state, and how much work has ever been opened under it."""
    state = holder.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()[0]
    application_attempts = holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_attempts WHERE workspace_id = ? AND job_id = ?",
        (WORKSPACE_ID, job_id),
    ).fetchone()[0]
    runtime_attempts = holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempts WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, run_id),
    ).fetchone()[0]
    return str(state), int(application_attempts), int(runtime_attempts)


# --- (a) a recorded stop intent refuses the claim before anything is issued -----------


def test_recorded_stop_intent_refuses_the_claim_before_issuance(
    owned: m1.Owned,
) -> None:
    _seed_run(owned, job_id="job-gate-a", run_id="run-gate-a", step_id="step-gate-a")
    _record_stop_intent(
        owned,
        job_id="job-gate-a",
        run_id="run-gate-a",
        stop_request_id="stop-gate-a",
    )

    with pytest.raises(RuntimeStopRequested) as refusal:
        _scheduler(owned).claim_next()

    assert STOP_RUN_STOP_REQUESTED in str(refusal.value)
    # Nothing was issued and nothing was half-claimed: the fenced transaction the claim
    # opened rolled back, so the job is exactly as runnable as it was.
    assert _counts(owned, "job-gate-a", "run-gate-a") == ("queued", 0, 0)


# --- (b) the same step proceeds where no stop intent exists ---------------------------


def test_the_same_step_is_claimed_when_no_stop_intent_is_recorded(
    owned: m1.Owned,
) -> None:
    _seed_run(owned, job_id="job-gate-b", run_id="run-gate-b", step_id="step-gate-b")

    claim = _scheduler(owned).claim_next()

    assert claim is not None
    assert (claim.job_id, claim.run_id, claim.run_step_id) == (
        "job-gate-b",
        "run-gate-b",
        "step-gate-b",
    )
    assert _counts(owned, "job-gate-b", "run-gate-b") == ("claimed", 1, 1)


def test_a_settled_stop_is_not_an_open_intent_but_a_terminal_run(
    owned: m1.Owned,
) -> None:
    """The gate tests the *unsettled* request, which is the narrow fact it claims to.

    A cancellation that settled left the run terminal, and `_select_claimable` already
    skips a terminal run, so the empty poll here is the whole answer -- no refusal, and
    no second rule restating the run-status one.
    """
    _seed_run(owned, job_id="job-gate-s", run_id="run-gate-s", step_id="step-gate-s")
    with runtime_stop_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.stop_run(
            RunStopRequest(
                stop_request_id="stop-gate-s",
                run_id="run-gate-s",
                requested_at_us=BASE_US + 500,
                requested_by="core-operator",
                reason="operator.cancelled",
                audit_ref=m18.audit_ref_for("job-gate-s"),
            ),
            runtime_event_id="evt-stop-gate-s",
            occurred_at_us=BASE_US + 501,
            completed_at_us=BASE_US + 502,
        )

    assert _scheduler(owned).claim_next() is None


# --- (c) a refused dispatch leaves nothing a retry can issue twice --------------------


def test_repolling_after_a_refusal_issues_no_second_attempt(owned: m1.Owned) -> None:
    _seed_run(owned, job_id="job-gate-c", run_id="run-gate-c", step_id="step-gate-c")
    _record_stop_intent(
        owned,
        job_id="job-gate-c",
        run_id="run-gate-c",
        stop_request_id="stop-gate-c",
    )
    scheduler = _scheduler(owned)

    for _ in range(3):
        with pytest.raises(RuntimeStopRequested):
            scheduler.claim_next()

    # Three refused polls, zero attempts of either history -- so there is no first
    # dispatch for a retry to duplicate, and no claimed job left stranded behind one.
    assert _counts(owned, "job-gate-c", "run-gate-c") == ("queued", 0, 0)


def test_a_stop_recorded_mid_run_refuses_the_advance_to_the_next_step(
    owned: m1.Owned,
) -> None:
    """The gate covers the advance, not only the first claim.

    A run already claimed is the case "no further dispatch" is actually about: its first
    step's work is in flight, and the step after it is the effect the stop exists to
    inhibit. The settlement rolls back with the advance, which is the fail-closed side --
    the run keeps its open attempt for `cancel` to release, rather than becoming a
    claimed job holding no attempt at all, which RT-109 reads as contradictory history.
    """
    _seed_run(owned, job_id="job-gate-m", run_id="run-gate-m", step_id="step-gate-m1")
    append_run_step(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id="run-gate-m",
        run_step_id="step-gate-m2",
        ordinal=2,
        step_kind="plan",
        created_at_us=BASE_US,
    )
    scheduler = _scheduler(owned)
    claim = scheduler.claim_next()
    assert claim is not None and claim.run_step_id == "step-gate-m1"

    _record_stop_intent(
        owned,
        job_id="job-gate-m",
        run_id="run-gate-m",
        stop_request_id="stop-gate-m",
    )

    with pytest.raises(RuntimeStopRequested):
        scheduler.complete(
            claim, result_kind="runtime_completion", result={"outcome": "complete"}
        )

    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempts WHERE workspace_id = ? "
        "AND run_id = ?",
        (WORKSPACE_ID, "run-gate-m"),
    ).fetchone() == (1,)
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()
        == (0,)
    )


def test_recovery_requeues_a_stopped_run_and_the_gate_still_refuses_it(
    owned: m1.Owned,
) -> None:
    """The concrete way a stopped run would otherwise be dispatched a second time.

    `uncertain` is not a terminal run status, so a Run parked by
    `cancellation_pending_reconciliation` is not excluded by the terminal-status check in
    `_select_claimable`. Its open attempt is what keeps its step unrunnable -- and RT-109
    recovery exists precisely to fail that attempt and requeue the job. At that point the
    step is pending, the job is queued, and nothing but this gate stands between the next
    poll and a second dispatch of work a stop already refused.
    """
    _seed_run(owned, job_id="job-gate-r", run_id="run-gate-r", step_id="step-gate-r")
    claim = _scheduler(owned).claim_next()
    assert claim is not None
    _record_stop_intent(
        owned,
        job_id="job-gate-r",
        run_id="run-gate-r",
        stop_request_id="stop-gate-r",
    )

    successor = rt106._takeover(owned)
    recovered = _scheduler(successor).recover_stranded()

    assert [r.requeued for r in recovered] == [True]
    assert _counts(successor, "job-gate-r", "run-gate-r")[0] == "queued"
    with pytest.raises(RuntimeStopRequested):
        _scheduler(successor).claim_next()


# --- (d) the gate reads the durable ledger, not an in-process flag --------------------


def test_the_gate_reads_an_intent_written_by_another_connection(
    owned: m1.Owned,
) -> None:
    _seed_run(owned, job_id="job-gate-d", run_id="run-gate-d", step_id="step-gate-d")
    _record_stop_intent(
        owned,
        job_id="job-gate-d",
        run_id="run-gate-d",
        stop_request_id="stop-gate-d",
    )

    restarted = _restart(owned)
    try:
        with pytest.raises(RuntimeStopRequested):
            _scheduler(restarted).claim_next()
        assert _counts(restarted, "job-gate-d", "run-gate-d") == ("queued", 0, 0)
    finally:
        restarted.connection.close()


def test_a_restart_with_no_recorded_intent_still_claims(owned: m1.Owned) -> None:
    """The control for the test above: the restart itself is not what refuses."""
    _seed_run(owned, job_id="job-gate-e", run_id="run-gate-e", step_id="step-gate-e")

    restarted = _restart(owned)
    try:
        assert _scheduler(restarted).claim_next() is not None
    finally:
        restarted.connection.close()
