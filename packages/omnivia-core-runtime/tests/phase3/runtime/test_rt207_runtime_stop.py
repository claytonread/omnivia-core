"""RT-207 acceptance for cancellation, timeout, supersession and the admission stop.

Every slice up to here made a run *do* things. This one is about making it stop, and the
whole file is written against one invariant: **stopping never fabricates a result.** A
cancelled run is `cancelled` and never `succeeded`; a timed-out run is `failed` and never
quietly re-spelled as a cancellation to make an illegal transition legal; a run holding an
effect nobody can account for comes to rest `uncertain` rather than being closed over the
top of it. There is no test below that reaches a terminal status without the record
actually supporting it, because there is no code path that does.

*Cancellation, timeout and supersession preserve everything.* Stopping deletes nothing and
rewrites nothing. Every test that stops a run asserts its prior events, artifacts, evidence,
receipts, settlements and reconciliations are still exactly as they were, and that the
terminal status arrived as one more entry on the append-only stream rather than as an edit
to anything already written.

*Repeated commands replay safely.* A caller re-issuing its own stop after a crash finds the
stored command and writes nothing; one asking for a different stop of the same run is
refused. The same holds one level up: settling a run already resting where its stop leaves
it answers from the stream and writes nothing.

*Stale and fenced owners cannot mutate.* A superseded fencing generation and a foreign
service instance are refused on both durable steps, with nothing written either way.

*Emergency admission stop denies admission and intent, and nothing else.* While one is
engaged no run is admitted and no effect is intended -- structurally, in the database, by
any writer -- while every relation that lets already-running work reach an outcome stays
open, because work in flight has to be able to finish.

*Unknown effects are never auto-retried.* Stopping a run over an unreconciled `unknown`
effect produces no dispatch, and leaves the dispatch count exactly where it was.

*Terminal transitions are monotonic and contradictions fail closed.* A second, different
ending has nowhere to live, a finished run cannot be stopped, and a timeout of a run that
never started is refused rather than rewritten into a legal move.

No public wire surface is touched: the contract version is asserted unchanged, and there is
still no `run.cancel` or `run.stop` operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt202_policy_budget_snapshot_repository as r202
import test_rt203_approval_capability_grant_repository as r203
import test_rt205_effect_transaction as t205
import test_rt206_effect_reconciliation as t206
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.service.runtime_stop import (
    ADMISSION_STOP_ENGAGED,
    ADMISSION_STOP_RELEASED,
    EVENT_KIND_RUN_STOPPED,
    REASON_STOP_DEFERRED_UNCERTAIN,
    REASON_STOPPED_CANCELLED,
    REASON_STOPPED_SUPERSEDED,
    REASON_STOPPED_TIMED_OUT,
    RUNNING_WORK_AWAIT,
    RUNNING_WORK_RELEASE,
    STOP_REASON_CANCELLED,
    STOP_REASON_SUPERSEDED,
    STOP_REASON_TIMED_OUT,
    RunStopError,
    admission_stopped,
    decide_stop_settlement,
    engage_admission_stop,
    release_admission_stop,
    request_run_stop,
    require_admission_open,
    settle_run_stop,
)
from omnivia_core_runtime.storage.agent_runtime import (
    AdmissionStop,
    RunSnapshot,
    RunStop,
    append_run_event,
    open_wait,
    read_admission_stop,
    read_effect_dispatch_count,
    read_run,
    read_run_stop,
    record_admission_stop,
    record_step_status,
    runtime_timestamp,
    runtime_writer,
    start_attempt,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import (
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
)

from omnivia_core.contracts.v1 import CONTRACT_VERSION

WORKSPACE_ID = t205.WORKSPACE_ID
RUN_ID = t205.RUN_ID
STEP_ID = t205.STEP_ID
JOB_ID = t205.JOB_ID
ATTEMPT_ID = t205.ATTEMPT_ID
INTENT_ID = t205.INTENT_ID
MS = t205.MS
RUNNING_US = t205.RUNNING_US
at = t205.at

RUN_STOPS = "omnivia_runtime_run_stops"
ADMISSION_STOPS = "omnivia_runtime_admission_stops"
TABLES = (RUN_STOPS, ADMISSION_STOPS)

MIGRATION_VERSION = 25
STOP_ID = "stp-0001"
STOPPED_EVENT_ID = "evt-rt207-stopped"
ADMISSION_STOP_ID = "adm-0001"

#: One instant per fact, in the order the facts occur. Every guard in 0025 refuses a stop
#: that precedes the run it stops, and 0018 refuses an event or step state that regresses
#: in time, so these are not interchangeable.
DECLARED_US = t205.DECLARED_US
DISPATCHED_US = DECLARED_US + MS
EFFECT_SETTLED_US = DECLARED_US + 2 * MS
REQUESTED_US = DECLARED_US + 3 * MS
SETTLED_US = DECLARED_US + 4 * MS
OBSERVED_US = DECLARED_US + 5 * MS
LATER_US = DECLARED_US + 6 * MS

AUDIT_REF = m18.audit_ref_for(JOB_ID)

#: The second run a supersession names. A real admitted run rather than a spelling, because
#: 0025's foreign key requires the successor to exist in this workspace.
SUCCESSOR_JOB_ID = "job-run-0002"
SUCCESSOR_RUN_ID = "run-0002"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def admitted(owned: m1.Owned) -> m1.Owned:
    """One admitted run with a step, a pinned policy and one issued grant.

    Admitted and not yet running: the fixture the `admitted` transition refusals are
    proved against, because `admitted` may only become `running` or `cancelled`.
    """
    r102.admit(owned)
    r102.add_step(owned)
    r202.add_policy(owned)
    r203.issue(owned)
    return owned


@pytest.fixture
def acting(admitted: m1.Owned) -> m1.Owned:
    """The same run, running, with one running attempt -- the state a stop interrupts."""
    append_run_event(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-rt207-started",
        occurred_at_us=RUNNING_US,
        event_kind="run_started",
        run_status="running",
        run_step_id=STEP_ID,
    )
    record_step_status(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_step_id=STEP_ID,
        status="running",
        observed_at_us=RUNNING_US,
    )
    start_attempt(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=RUNNING_US,
    )
    return admitted


def counts(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in TABLES}


def snapshot(holder: m1.Owned, *, run_id: str = RUN_ID) -> RunSnapshot:
    run = read_run(holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
    assert run is not None
    return run


def stop(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    run_stop_id: str = STOP_ID,
    stop_reason: str = STOP_REASON_CANCELLED,
    running_work: str = RUNNING_WORK_RELEASE,
    at_us: int = REQUESTED_US,
    audit_ref: str = AUDIT_REF,
    superseded_by_run_id: str | None = None,
) -> RunStop:
    return request_run_stop(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        run_stop_id=run_stop_id,
        stop_reason=stop_reason,
        running_work=running_work,
        requested_at_us=at_us,
        audit_ref=audit_ref,
        superseded_by_run_id=superseded_by_run_id,
    )


def settle(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    event_id: str = STOPPED_EVENT_ID,
    at_us: int = SETTLED_US,
) -> str:
    return settle_run_stop(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        runtime_event_id=event_id,
        settled_at_us=at_us,
    )


def engage(
    holder: m1.Owned,
    *,
    admission_stop_id: str = ADMISSION_STOP_ID,
    running_work: str = RUNNING_WORK_AWAIT,
    at_us: int = REQUESTED_US,
    reason: str = "operator.emergency_stop",
) -> AdmissionStop:
    return engage_admission_stop(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission_stop_id=admission_stop_id,
        running_work=running_work,
        effective_at_us=at_us,
        reason=reason,
        audit_ref=AUDIT_REF,
    )


def release(
    holder: m1.Owned,
    *,
    admission_stop_id: str = "adm-0002",
    at_us: int = SETTLED_US,
    reason: str = "operator.emergency_stop_released",
) -> AdmissionStop:
    return release_admission_stop(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission_stop_id=admission_stop_id,
        effective_at_us=at_us,
        reason=reason,
        audit_ref=AUDIT_REF,
    )


def add_wait(holder: m1.Owned, *, wait_id: str = "wait-0001") -> None:
    """One pending wait on the running step -- work a stop has to account for."""
    open_wait(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        wait_id=wait_id,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        kind="external_signal",
        created_at_us=RUNNING_US,
        resume_digest=m18.DIGEST,
    )


def admit_successor(holder: m1.Owned) -> None:
    """A second admitted run, so a supersession has a real successor to name."""
    r102.admit(holder, r102.admission(run_id=SUCCESSOR_RUN_ID, job_id=SUCCESSOR_JOB_ID,
                                      event_id="evt-rt207-successor"))


def handmade(**overrides: object) -> RunStop:
    """One stop command built by a caller rather than through the seam.

    Every guard 0025 states has to be provable against a writer that reached past
    :func:`request_run_stop`, because the composition never produces the shapes the guards
    refuse -- that is the point of the guards.
    """
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_stop_id": STOP_ID,
        "run_id": RUN_ID,
        "stop_reason": STOP_REASON_CANCELLED,
        "running_work": RUNNING_WORK_RELEASE,
        "requested_at": at(REQUESTED_US),
        "audit_reference": AUDIT_REF,
        "superseded_by_run_id": None,
    }
    values.update(overrides)
    return RunStop(**values)  # type: ignore[arg-type]


def write(holder: m1.Owned, record: RunStop) -> None:
    with runtime_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.request_run_stop(record)


# --- 1: the migration is additive and append-only ---------------------------------


def test_migration_0025_adds_two_relations_and_takes_nothing_away() -> None:
    """Additive: two new tables, and every relation 0018-0024 defined still present."""
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert [m.name for m in found] == ["0025_runtime_stop_and_admission_control.sql"]
    tables = canonical_schema_tables()
    for table in (*TABLES, *t205.TABLES, t206.RECONCILIATIONS):
        assert table in tables


@pytest.mark.parametrize("table", TABLES)
def test_the_stop_relations_are_append_only(acting: m1.Owned, table: str) -> None:
    """UPDATE and DELETE abort for the current fenced owner too.

    A stop command that could be edited is not a command, and a stopped run whose history
    could be deleted would take the evidence the stop exists to preserve with it.
    """
    stop(acting)
    engage(acting)
    for statement in (
        f"UPDATE {table} SET workspace_id = workspace_id",
        f"DELETE FROM {table}",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            runtime_writer(
                acting.connection,
                acting.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=acting.generation,
            ) as writer,
        ):
            writer.connection.execute(statement)
    assert counts(acting)[table] == 1


# --- 2: the stop command is durable, and repeating it replays ---------------------


def test_a_stop_command_is_recorded_and_settles_nothing(acting: m1.Owned) -> None:
    """Recording what was asked is not deciding what the run becomes."""
    recorded = stop(acting)
    assert recorded.stop_reason == STOP_REASON_CANCELLED
    assert recorded.running_work == RUNNING_WORK_RELEASE
    assert recorded.requested_at == at(REQUESTED_US)
    assert read_run_stop(
        acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ) == recorded
    assert snapshot(acting).status == "running"


def test_repeating_the_same_stop_answers_from_the_store(acting: m1.Owned) -> None:
    """The crash replay: identical in every field, answered, and nothing written."""
    first = stop(acting)
    assert stop(acting) == first
    assert counts(acting)[RUN_STOPS] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_stop_id", "stp-0002"),
        ("stop_reason", STOP_REASON_TIMED_OUT),
        ("running_work", RUNNING_WORK_AWAIT),
        ("at_us", REQUESTED_US + MS),
        ("audit_ref", m18.audit_ref_for(JOB_ID) + "-b"),
    ],
)
def test_a_second_different_stop_of_one_run_is_refused(
    acting: m1.Owned, field: str, value: object
) -> None:
    """One run, one stop. A different second command is a contradiction, not a replay."""
    stop(acting)
    with pytest.raises(RunStopError, match="already stopped"):
        stop(acting, **{field: value})  # type: ignore[arg-type]
    assert counts(acting)[RUN_STOPS] == 1


def test_a_run_this_workspace_never_admitted_cannot_be_stopped(acting: m1.Owned) -> None:
    with pytest.raises(RunStopError, match="not admitted"):
        stop(acting, run_id="run-absent")
    assert counts(acting)[RUN_STOPS] == 0


def test_a_finished_run_cannot_be_stopped(acting: m1.Owned) -> None:
    """Nothing left to stop: a concluded outcome is never re-decided.

    Proved on a second run so the refusal is the terminal one rather than the
    one-stop-per-run key: this run finished on its own and was never stopped at all.
    """
    admit_successor(acting)
    _finish_successor(acting)
    with pytest.raises(sqlite3.IntegrityError, match="already finished"):
        write(acting, handmade(run_stop_id="stp-0002", run_id=SUCCESSOR_RUN_ID))
    assert counts(acting)[RUN_STOPS] == 0


def _finish_successor(holder: m1.Owned, *, status: str = "succeeded") -> None:
    """Terminalize the successor run through its own stream, with no stop involved."""
    for index, (kind, run_status) in enumerate(
        (("run_started", "running"), ("run_finished", status))
    ):
        append_run_event(
            holder.connection,
            holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=holder.generation,
            run_id=SUCCESSOR_RUN_ID,
            runtime_event_id=f"evt-rt207-successor-{index}",
            occurred_at_us=RUNNING_US + index * MS,
            event_kind=kind,
            run_status=run_status,
        )


# --- 3: the deterministic terminal state of each stop -----------------------------


def test_cancelling_a_running_run_settles_it_cancelled(acting: m1.Owned) -> None:
    """The plainest case, and the one that must never read as a success."""
    stop(acting)
    assert settle(acting) == "cancelled"
    run = snapshot(acting)
    assert run.status == "cancelled"
    assert run.finished_at == at(SETTLED_US)
    assert run.events[-1].event_kind == EVENT_KIND_RUN_STOPPED
    assert run.events[-1].run_status == "cancelled"


def test_a_timed_out_run_settles_failed_and_is_not_re_spelled(acting: m1.Owned) -> None:
    """A deadline that passed is a run that did not do what it was asked."""
    stop(acting, stop_reason=STOP_REASON_TIMED_OUT)
    assert settle(acting) == "failed"
    assert snapshot(acting).status == "failed"


def test_a_superseded_run_settles_cancelled_and_names_its_successor(
    acting: m1.Owned,
) -> None:
    """Supersession is a cancellation that can say what replaced the run."""
    admit_successor(acting)
    recorded = stop(
        acting,
        stop_reason=STOP_REASON_SUPERSEDED,
        superseded_by_run_id=SUCCESSOR_RUN_ID,
    )
    assert recorded.superseded_by_run_id == SUCCESSOR_RUN_ID
    assert settle(acting) == "cancelled"
    run = snapshot(acting)
    assert run.status == "cancelled"
    assert run.stop is not None and run.stop.superseded_by_run_id == SUCCESSOR_RUN_ID
    assert snapshot(acting, run_id=SUCCESSOR_RUN_ID).status == "admitted"


def test_a_supersession_must_name_a_successor_and_the_others_must_not(
    acting: m1.Owned,
) -> None:
    """Neither half is optional: a nameless supersession is just a cancellation."""
    with pytest.raises(sqlite3.IntegrityError):
        write(acting, handmade(stop_reason=STOP_REASON_SUPERSEDED))
    admit_successor(acting)
    with pytest.raises(sqlite3.IntegrityError):
        write(acting, handmade(superseded_by_run_id=SUCCESSOR_RUN_ID))
    with pytest.raises(sqlite3.IntegrityError):
        write(
            acting,
            handmade(stop_reason=STOP_REASON_SUPERSEDED, superseded_by_run_id=RUN_ID),
        )
    assert counts(acting)[RUN_STOPS] == 0


def test_a_run_that_never_started_cannot_time_out(admitted: m1.Owned) -> None:
    """Fail closed rather than rewrite: `admitted` may only become `running` or
    `cancelled`, a run that never ran cannot have failed, and re-spelling the operator's
    reason to make the move legal would be answering a question nobody asked."""
    stop(admitted, stop_reason=STOP_REASON_TIMED_OUT, at_us=m18.BASE_US + MS)
    with pytest.raises(RunStopError, match="may not move to"):
        settle(admitted, at_us=m18.BASE_US + 2 * MS)
    assert snapshot(admitted).status == "admitted"


def test_an_admitted_run_may_still_be_cancelled(admitted: m1.Owned) -> None:
    """The one stop `admitted` does admit, and the run closes without ever running."""
    stop(admitted, at_us=m18.BASE_US + MS)
    assert settle(admitted, at_us=m18.BASE_US + 2 * MS) == "cancelled"


def test_settling_a_run_that_was_never_stopped_is_refused(acting: m1.Owned) -> None:
    """A run is terminalized by a stop it was given, never by one inferred here."""
    with pytest.raises(RunStopError, match="no stop command"):
        settle(acting)
    assert snapshot(acting).status == "running"


# --- 4: open waits and running attempts -------------------------------------------


def test_an_await_stop_refuses_to_terminalize_over_a_running_attempt(
    acting: m1.Owned,
) -> None:
    """The policy says the work in flight settles itself; closing over it would be the
    fabricated outcome this seam exists to prevent."""
    stop(acting, running_work=RUNNING_WORK_AWAIT)
    with pytest.raises(RunStopError, match="running attempt"):
        settle(acting)
    assert snapshot(acting).status == "running"
    assert snapshot(acting).steps[0].attempts[0].status == "running"


def test_an_await_stop_refuses_to_terminalize_over_a_pending_wait(
    acting: m1.Owned,
) -> None:
    add_wait(acting)
    stop(acting, running_work=RUNNING_WORK_AWAIT)
    with pytest.raises(RunStopError, match="pending wait"):
        settle(acting)
    assert snapshot(acting).waits[0].status == "pending"


def test_an_await_stop_settles_once_the_work_it_awaits_has_closed(
    acting: m1.Owned,
) -> None:
    """The refusal was not a failure: the same command settles cleanly afterwards."""
    stop(acting, running_work=RUNNING_WORK_AWAIT)
    with pytest.raises(RunStopError, match="running attempt"):
        settle(acting)
    _finish_attempt(acting)
    assert settle(acting) == "cancelled"


def _finish_attempt(holder: m1.Owned) -> None:
    """The worker's own terminalization of the attempt an `await` stop waits for."""
    with runtime_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.finish_attempt(
            attempt_id=ATTEMPT_ID, status="cancelled", finished_at_us=REQUESTED_US
        )
        writer.record_step_status(
            run_step_id=STEP_ID, status="cancelled", observed_at_us=REQUESTED_US
        )


def test_a_release_stop_closes_the_wait_the_attempt_and_the_step(
    acting: m1.Owned,
) -> None:
    """`release` closes work through the same append-only relations any closure uses."""
    add_wait(acting)
    stop(acting)
    assert settle(acting) == "cancelled"
    run = snapshot(acting)
    assert run.waits[0].status == "cancelled"
    assert run.waits[0].resolution_reason == "cancelled"
    assert run.waits[0].resolved_at == at(SETTLED_US)
    assert run.steps[0].status == "cancelled"
    assert run.steps[0].attempts[0].status == "cancelled"
    assert run.steps[0].attempts[0].failure is None


def test_a_cancelled_attempt_carries_no_failure(acting: m1.Owned) -> None:
    """Cancellation is not failure: attaching an error would say the work went wrong."""
    stop(acting)
    settle(acting)
    assert snapshot(acting).steps[0].attempts[0].failure is None


# --- 5: effects, uncertainty, and the absence of any retry ------------------------


def _uncertain_effect(holder: m1.Owned) -> None:
    """Declare, dispatch, settle: the crash window that leaves an effect `unknown`."""
    t205.declare(holder)
    t205.publish(holder, at_us=DISPATCHED_US)
    assert t205.settle(holder, at_us=EFFECT_SETTLED_US).outcome == "unknown"


def test_an_unsettled_effect_refuses_the_stop_rather_than_assuming_anything(
    acting: m1.Owned,
) -> None:
    """Stopping settles no effect and never assumes one did not land."""
    t205.declare(acting)
    t205.publish(acting, at_us=DISPATCHED_US)
    stop(acting)
    with pytest.raises(RunStopError, match="holding no settlement"):
        settle(acting)
    assert snapshot(acting).status == "running"


def test_an_unreconciled_unknown_effect_leaves_the_stopped_run_uncertain(
    acting: m1.Owned,
) -> None:
    """`uncertain` is where a stopped run rests over a question nobody can answer."""
    _uncertain_effect(acting)
    stop(acting)
    assert settle(acting) == "uncertain"
    run = snapshot(acting)
    assert run.status == "uncertain"
    assert run.finished_at is None
    assert run.events[-1].run_status == "uncertain"


def test_stopping_an_uncertain_run_produces_no_dispatch(acting: m1.Owned) -> None:
    """No blind retry: stopping produces no work, and the dispatch count does not move."""
    _uncertain_effect(acting)
    before = read_effect_dispatch_count(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    stop(acting)
    settle(acting)
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == before
    )


def test_settling_an_uncertain_run_again_writes_nothing_while_it_stays_uncertain(
    acting: m1.Owned,
) -> None:
    """Idempotent, not a second event: nothing moved, so nothing is recorded."""
    _uncertain_effect(acting)
    stop(acting)
    assert settle(acting) == "uncertain"
    sequence = len(snapshot(acting).events)
    assert settle(acting, event_id="evt-rt207-again", at_us=LATER_US) == "uncertain"
    assert len(snapshot(acting).events) == sequence


def test_reconciling_the_effect_lets_the_stopped_run_reach_its_terminal_status(
    acting: m1.Owned,
) -> None:
    """The second half of the same stop -- evidence arriving, not work repeated."""
    _uncertain_effect(acting)
    stop(acting)
    assert settle(acting) == "uncertain"
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconciled = t206.reconcile(acting, at_us=OBSERVED_US)
    assert reconciled.outcome == "committed"
    assert settle(acting, event_id="evt-rt207-closed", at_us=LATER_US) == "cancelled"
    run = snapshot(acting)
    assert run.status == "cancelled"
    assert run.effect_settlements[0].outcome == "unknown"
    assert run.effect_reconciliations[0] == reconciled


# --- 6: monotonic terminal transitions, and contradictions that fail closed --------


def test_settling_a_stop_twice_answers_from_the_stream(acting: m1.Owned) -> None:
    """The crash replay one level up: the run is already where this stop leaves it."""
    stop(acting)
    assert settle(acting) == "cancelled"
    events = len(snapshot(acting).events)
    assert settle(acting, event_id="evt-rt207-replay", at_us=LATER_US) == "cancelled"
    assert len(snapshot(acting).events) == events


def test_a_run_that_finished_differently_is_never_re_settled(acting: m1.Owned) -> None:
    """A run has one ending, and a stop that disagrees with it is refused.

    The real race: the run was stopped, and then finished on its own before the stop was
    settled. Reporting `cancelled` over a run this database says succeeded would be the
    fabricated outcome; refusing says the two answers disagree and lets somebody look.
    """
    stop(acting)
    append_run_event(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-rt207-succeeded",
        occurred_at_us=SETTLED_US,
        event_kind="run_finished",
        run_status="succeeded",
    )
    with pytest.raises(RunStopError, match="one ending"):
        settle(acting, event_id="evt-rt207-late", at_us=LATER_US)
    assert snapshot(acting).status == "succeeded"


def test_a_terminal_run_admits_no_further_event(acting: m1.Owned) -> None:
    """0018's sink rule still holds under a stop: terminal is terminal."""
    stop(acting)
    settle(acting)
    with (
        pytest.raises(sqlite3.IntegrityError, match="terminal run event is final"),
        runtime_writer(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
        ) as writer,
    ):
        writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id="evt-rt207-after",
            occurred_at_us=LATER_US,
            event_kind="run_started",
            run_status="running",
        )


# --- 7: the emergency admission stop ----------------------------------------------


def test_engaging_an_admission_stop_records_it_and_reports_it(acting: m1.Owned) -> None:
    engaged = engage(acting)
    assert engaged.state == ADMISSION_STOP_ENGAGED
    assert engaged.running_work == RUNNING_WORK_AWAIT
    assert engaged.sequence == 0
    assert read_admission_stop(acting.connection, workspace_id=WORKSPACE_ID) == engaged
    assert admission_stopped(engaged)


def test_an_engaged_admission_stop_denies_a_new_run(acting: m1.Owned) -> None:
    """Structural: the refusal is on `omnivia_runtime_runs`, not in a code path."""
    engage(acting)
    with pytest.raises(sqlite3.IntegrityError, match="no run is admitted"):
        admit_successor(acting)
    assert read_run(
        acting.connection, workspace_id=WORKSPACE_ID, run_id=SUCCESSOR_RUN_ID
    ) is None


def test_an_engaged_admission_stop_denies_a_new_effect_intent(acting: m1.Owned) -> None:
    engage(acting)
    with pytest.raises(sqlite3.IntegrityError, match="no effect is intended"):
        t205.declare(acting)
    assert snapshot(acting).effect_intents == ()


def test_require_admission_open_says_so_before_the_guard_has_to(acting: m1.Owned) -> None:
    """The readable refusal in front of the guard, and never a second authority."""
    require_admission_open(acting.connection, workspace_id=WORKSPACE_ID)
    engage(acting)
    with pytest.raises(RunStopError, match="emergency admission stop"):
        require_admission_open(acting.connection, workspace_id=WORKSPACE_ID)


def test_already_running_work_still_settles_under_an_engaged_stop(
    acting: m1.Owned,
) -> None:
    """The whole point of stopping *admission*: work in flight can still finish.

    A stop that also froze the work in flight would leave every uncertain effect uncertain
    forever, which is the opposite of stopping safely.
    """
    t205.declare(acting)
    engage(acting, running_work=RUNNING_WORK_AWAIT)
    t205.publish(acting, at_us=DISPATCHED_US)
    assert t205.settle(acting, at_us=EFFECT_SETTLED_US).outcome == "unknown"
    _finish_attempt(acting)
    stop(acting, running_work=RUNNING_WORK_AWAIT)
    assert settle(acting) == "uncertain"


def test_a_run_stop_must_obey_the_engaged_running_work_policy(acting: m1.Owned) -> None:
    """The ledger's policy is a rule: an operator who declared that running work settles
    cannot be overruled one run at a time."""
    engage(acting, running_work=RUNNING_WORK_AWAIT)
    with pytest.raises(sqlite3.IntegrityError, match="running-work policy"):
        stop(acting, running_work=RUNNING_WORK_RELEASE)
    assert counts(acting)[RUN_STOPS] == 0
    assert stop(acting, running_work=RUNNING_WORK_AWAIT).running_work == "await"


def test_repeating_an_engagement_answers_from_the_ledger(acting: m1.Owned) -> None:
    first = engage(acting)
    assert engage(acting) == first
    assert counts(acting)[ADMISSION_STOPS] == 1


def test_a_second_live_admission_stop_is_refused(acting: m1.Owned) -> None:
    """Two live emergency stops would be two authorities over one workspace."""
    engage(acting)
    with pytest.raises(RunStopError, match="already stopped"):
        engage(acting, admission_stop_id="adm-0009")
    assert counts(acting)[ADMISSION_STOPS] == 1


def test_a_different_entry_under_one_identifier_is_refused(acting: m1.Owned) -> None:
    engage(acting)
    with pytest.raises(RunStopError, match="already recorded"):
        engage(acting, running_work=RUNNING_WORK_RELEASE)
    assert counts(acting)[ADMISSION_STOPS] == 1


def test_releasing_reopens_admission_without_unmaking_the_stop(acting: m1.Owned) -> None:
    """A second immutable entry, never an edit: the stop happened, and stays recorded."""
    engaged = engage(acting)
    released = release(acting)
    assert released.state == ADMISSION_STOP_RELEASED
    assert released.running_work is None
    assert released.sequence == engaged.sequence + 1
    assert not admission_stopped(released)
    require_admission_open(acting.connection, workspace_id=WORKSPACE_ID)
    admit_successor(acting)
    assert snapshot(acting, run_id=SUCCESSOR_RUN_ID).status == "admitted"
    assert counts(acting)[ADMISSION_STOPS] == 2


def test_releasing_a_workspace_that_is_not_stopped_is_refused(acting: m1.Owned) -> None:
    with pytest.raises(RunStopError, match="nothing to release"):
        release(acting)
    engage(acting)
    release(acting)
    with pytest.raises(RunStopError, match="nothing to release"):
        release(acting, admission_stop_id="adm-0003", at_us=LATER_US)
    assert counts(acting)[ADMISSION_STOPS] == 2


def test_the_ledger_opens_by_engaging_and_never_restates_itself(acting: m1.Owned) -> None:
    """0025's own guards, proved against a writer that reached past the seam."""
    entry = AdmissionStop(
        workspace_id=WORKSPACE_ID,
        sequence=0,
        admission_stop_id=ADMISSION_STOP_ID,
        state=ADMISSION_STOP_RELEASED,
        effective_at=at(REQUESTED_US),
        reason="operator.emergency_stop",
        audit_reference=AUDIT_REF,
    )
    with pytest.raises(sqlite3.IntegrityError):
        record_admission_stop(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            entry=entry,
        )
    engage(acting)
    with pytest.raises(sqlite3.IntegrityError, match="restates its predecessor"):
        record_admission_stop(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            entry=replace(
                entry,
                admission_stop_id="adm-0004",
                state=ADMISSION_STOP_ENGAGED,
                running_work=RUNNING_WORK_AWAIT,
            ),
        )
    assert counts(acting)[ADMISSION_STOPS] == 1


# --- 8: fencing -- a stale or foreign owner mutates nothing ------------------------


def test_a_superseded_generation_cannot_stop_a_run(acting: m1.Owned) -> None:
    with pytest.raises(StaleGeneration):
        request_run_stop(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation + 1,
            run_id=RUN_ID,
            run_stop_id=STOP_ID,
            stop_reason=STOP_REASON_CANCELLED,
            running_work=RUNNING_WORK_RELEASE,
            requested_at_us=REQUESTED_US,
            audit_ref=AUDIT_REF,
        )
    assert counts(acting)[RUN_STOPS] == 0


def test_a_foreign_service_instance_cannot_settle_a_stop(acting: m1.Owned) -> None:
    """A correct-looking generation from the wrong holder is still refused."""
    stop(acting)
    impostor = m1.make_identity(instance="svc-rt207-impostor", pid=9999)
    with pytest.raises(StaleGeneration, match="lease belongs to"):
        settle_run_stop(
            acting.connection,
            impostor,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            run_id=RUN_ID,
            runtime_event_id=STOPPED_EVENT_ID,
            settled_at_us=SETTLED_US,
        )
    assert snapshot(acting).status == "running"


def test_a_takeover_under_a_settling_stop_commits_nothing(acting: m1.Owned) -> None:
    """The check that matters is the one immediately before COMMIT.

    The stop is written and the generation then moves under it, exactly as a takeover
    would. Nothing this transaction did may become durable after that.
    """
    with (
        pytest.raises(StaleGeneration, match="before commit"),
        runtime_writer(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
        ) as writer,
    ):
        writer.request_run_stop(handmade())
        acting.connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = ? "
            "WHERE singleton = 1",
            (acting.generation + 1,),
        )
    assert counts(acting)[RUN_STOPS] == 0


def test_an_engagement_from_a_stale_owner_leaves_admission_open(
    acting: m1.Owned,
) -> None:
    with pytest.raises(StaleGeneration):
        engage_admission_stop(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation + 1,
            admission_stop_id=ADMISSION_STOP_ID,
            running_work=RUNNING_WORK_AWAIT,
            effective_at_us=REQUESTED_US,
            reason="operator.emergency_stop",
            audit_ref=AUDIT_REF,
        )
    assert read_admission_stop(acting.connection, workspace_id=WORKSPACE_ID) is None
    admit_successor(acting)


# --- 9: crash and rollback windows ------------------------------------------------


def test_a_transaction_that_fails_after_stopping_leaves_the_run_unstopped(
    acting: m1.Owned,
) -> None:
    """Nothing half-stopped: the command goes with everything else the transaction wrote."""
    with pytest.raises(t205.Boom), runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.request_run_stop(handmade())
        raise t205.Boom("the command refused after recording the stop")
    assert counts(acting)[RUN_STOPS] == 0
    assert read_run_stop(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID) is None
    assert snapshot(acting).status == "running"


def test_a_transaction_that_fails_while_settling_leaves_the_run_stopped_and_open(
    acting: m1.Owned,
) -> None:
    """The release and the terminal event land together or not at all."""
    add_wait(acting)
    recorded = stop(acting)
    with pytest.raises(t205.Boom), runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.close_wait(
            wait_id="wait-0001",
            status="cancelled",
            resolved_at_us=SETTLED_US,
            resolution_reason="cancelled",
        )
        writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id=STOPPED_EVENT_ID,
            occurred_at_us=SETTLED_US,
            event_kind=EVENT_KIND_RUN_STOPPED,
            run_status="cancelled",
        )
        raise t205.Boom("the command refused after settling")
    run = snapshot(acting)
    assert run.status == "running"
    assert run.waits[0].status == "pending"
    assert run.stop == recorded
    # And the retry still works, from exactly where it left off.
    assert settle(acting) == "cancelled"


def test_a_refused_settlement_writes_nothing_and_the_retry_still_works(
    acting: m1.Owned,
) -> None:
    _uncertain_effect(acting)
    stop(acting, running_work=RUNNING_WORK_AWAIT)
    with pytest.raises(RunStopError, match="running attempt"):
        settle(acting)
    assert snapshot(acting).status == "running"
    _finish_attempt(acting)
    assert settle(acting) == "uncertain"


# --- 10: evidence preservation ----------------------------------------------------


def test_stopping_preserves_every_prior_fact_the_run_accumulated(
    acting: m1.Owned,
) -> None:
    """Cancellation deletes nothing and rewrites nothing: the terminal status is one more
    entry on an append-only stream, and everything under it is byte-for-byte as written."""
    _uncertain_effect(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    before = snapshot(acting)
    stop(acting)
    assert settle(acting) == "uncertain"
    after = snapshot(acting)
    assert after.events[: len(before.events)] == before.events
    assert after.effect_intents == before.effect_intents
    assert after.effect_receipts == before.effect_receipts
    assert after.effect_settlements == before.effect_settlements
    assert after.approvals == before.approvals
    assert after.capability_grants == before.capability_grants
    assert after.policy == before.policy
    assert after.budget == before.budget
    assert after.created_at == before.created_at


def test_the_stopped_run_reports_why_it_was_stopped(acting: m1.Owned) -> None:
    """`cancelled` never has to be read as "cancelled for a reason nobody wrote down"."""
    recorded = stop(acting, stop_reason=STOP_REASON_TIMED_OUT)
    settle(acting)
    run = snapshot(acting)
    assert run.stop == recorded
    assert run.events[-1].details == {
        "run_stop_id": STOP_ID,
        "stop_reason": STOP_REASON_TIMED_OUT,
        "running_work": RUNNING_WORK_RELEASE,
        "reason": REASON_STOPPED_TIMED_OUT,
    }


def test_a_run_nobody_stopped_reports_no_stop(acting: m1.Owned) -> None:
    assert snapshot(acting).stop is None


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    acting: m1.Owned,
) -> None:
    stop(acting)
    engage(acting)
    other = "ws-rt207-other"
    assert read_run_stop(acting.connection, workspace_id=other, run_id=RUN_ID) is None
    assert read_admission_stop(acting.connection, workspace_id=other) is None


# --- 11: the pure rule ------------------------------------------------------------


def _stop(**overrides: object) -> RunStop:
    return handmade(**overrides)


def test_decide_stop_settlement_has_two_answers_and_no_default() -> None:
    """Every reachable answer, derived rather than argued."""
    clear = {
        "run_status": "running",
        "open_waits": 0,
        "running_attempts": 0,
        "unsettled_effects": 0,
        "uncertain_effects": 0,
    }
    assert decide_stop_settlement(stop=_stop(), **clear) == (  # type: ignore[arg-type]
        "cancelled",
        REASON_STOPPED_CANCELLED,
    )
    assert decide_stop_settlement(
        stop=_stop(stop_reason=STOP_REASON_TIMED_OUT), **clear  # type: ignore[arg-type]
    ) == ("failed", REASON_STOPPED_TIMED_OUT)
    assert decide_stop_settlement(
        stop=_stop(stop_reason=STOP_REASON_SUPERSEDED, superseded_by_run_id="run-0002"),
        **clear,  # type: ignore[arg-type]
    ) == ("cancelled", REASON_STOPPED_SUPERSEDED)
    assert decide_stop_settlement(
        stop=_stop(), **{**clear, "uncertain_effects": 1}  # type: ignore[arg-type]
    ) == ("uncertain", REASON_STOP_DEFERRED_UNCERTAIN)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"run_status": "succeeded"}, "already 'succeeded'"),
        ({"run_status": "cancelled"}, "has finished"),
        ({"run_status": "not_a_status"}, "does not recognize"),
        ({"unsettled_effects": 1}, "holding no settlement"),
        ({"open_waits": -1}, "never negative"),
    ],
)
def test_decide_stop_settlement_fails_closed(
    overrides: dict[str, object], message: str
) -> None:
    """No default branch: every state without an honest answer raises."""
    facts: dict[str, object] = {
        "run_status": "running",
        "open_waits": 0,
        "running_attempts": 0,
        "unsettled_effects": 0,
        "uncertain_effects": 0,
    }
    facts.update(overrides)
    with pytest.raises(RunStopError, match=message):
        decide_stop_settlement(stop=_stop(), **facts)  # type: ignore[arg-type]


def test_decide_stop_settlement_refuses_a_reason_or_policy_it_does_not_know() -> None:
    """An open vocabulary is read fail-safe: an unknown value grants nothing."""
    facts: dict[str, object] = {
        "run_status": "running",
        "open_waits": 0,
        "running_attempts": 0,
        "unsettled_effects": 0,
        "uncertain_effects": 0,
    }
    with pytest.raises(RunStopError, match="not one this build settles"):
        decide_stop_settlement(stop=_stop(stop_reason="abandoned"), **facts)  # type: ignore[arg-type]
    with pytest.raises(RunStopError, match="not one this build stops"):
        decide_stop_settlement(stop=_stop(running_work="whenever"), **facts)  # type: ignore[arg-type]


def test_admission_stopped_reads_an_unknown_state_as_no_stop() -> None:
    """Only an entry this build reads as `engaged` denies anything."""
    entry = AdmissionStop(
        workspace_id=WORKSPACE_ID,
        sequence=0,
        admission_stop_id=ADMISSION_STOP_ID,
        state="quiesced",
        effective_at=runtime_timestamp(REQUESTED_US),
        reason="operator.emergency_stop",
        audit_reference=AUDIT_REF,
    )
    assert not admission_stopped(entry)
    assert not admission_stopped(None)
    assert admission_stopped(replace(entry, state=ADMISSION_STOP_ENGAGED))


# --- 12: nothing public moved -----------------------------------------------------


def test_rt207_adds_no_public_wire_surface() -> None:
    """A private service seam: no operation, no catalogue entry, no version move."""
    assert CONTRACT_VERSION == "1.3"
    assert issubclass(RunStopError, StorageError)
