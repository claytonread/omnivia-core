"""T-0693 acceptance for the live Workflow runtime: what actually executes a Run.

`test_t0693_workflow_application.py` holds the four public operations to the served
path. This file holds the lane underneath them -- the sealed plan's canonical steps, the
scheduler that claims them, the settlement that advances or finishes them, the recovery
that adopts them across a restart, and the composition that makes all of it reachable
from a real `ServiceRunner`.

*A started Run is executable, not merely recorded.* `workflow.start` opens the sealed
plan's steps in its own mutation, so the durable job, the canonical Run, the binding and
the work it describes are one commit. The identifiers are derived from the plan, so the
same Run always derives the same steps and a replay opens no second set.

*Dependencies gate claims, and succeeding is what satisfies one.* The next step is
claimed only when every step it depends on has succeeded, and settling a step advances to
the next ready one inside the transaction that settled it -- never leaving a claimed job
whose run holds no open attempt, which is the state RT-109 reads as contradictory.

*Nothing here manufactures a success.* A failure is retried within the durable job's own
attempt budget and then fails the step, the run and the job together. A settlement naming
a different attempt, arriving after a cancellation, or issued under a superseded fence,
writes nothing at all.

*A restart loses the process and keeps the workspace.* A durable wait survives one, is
adopted rather than interrupted, resolves through the public operation, and the claim its
step is running under is read back from stored rows so the Run can still finish.

*The composition is the production one.* The runner's own startup pass is what runs
recovery, the runner's own accessor is what hands out a scheduler, and the production
application surface serves Workflow against whichever release authority was injected into
it -- there is no test-only construction standing in for any of them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_t0688_workflow_transition_bundle_repository as ip07
import test_t0693_workflow_application as app
import test_workflow_runs_migration as m27
from omnivia_core_runtime.execution.workflow import (
    EXECUTION_CLASS_WAIT,
    ChildWorkflowDefinition,
    StepDefinition,
    WorkflowDefinition,
    materialise_workflow,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    ApplicationDispatcher,
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.workflow import (
    WORKFLOW_CONTROL_OPERATION,
    WORKFLOW_INSPECT_OPERATION,
    WORKFLOW_START_OPERATION,
)
from omnivia_core_runtime.service.main import (
    LOCAL_PRINCIPAL,
    _build_production_application_surface,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.runtime_recovery import (
    CLASSIFICATION_CONTRADICTORY_HISTORY,
    CLASSIFICATION_DURABLE_OPEN_WAIT,
    CLASSIFICATION_NO_OPEN_ATTEMPT,
    CLASSIFICATION_TERMINAL_HISTORY,
    recover_runtime_startup,
)
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeClaim,
    RuntimeScheduler,
    RuntimeSchedulingError,
)
from omnivia_core_runtime.service.runtime_waits import WaitPolicyDenied
from omnivia_core_runtime.service.workflow_runtime import (
    WorkflowStepPlan,
    open_workflow_runtime_steps,
    workflow_run_step_id,
    workflow_runtime_scheduler,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.storage.agent_runtime import (
    read_run_sequence,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runs import (
    ChildCorrelationRecord,
    ChildResultRecord,
    read_workflow_plan,
    transaction_local_workflow_writer,
    workflow_writer,
)
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    read_runtime_definition_binding,
    read_runtime_journal_events,
)

from omnivia_core.contracts.v1 import (
    ApiError,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
)

WORKSPACE_ID = app.WORKSPACE_ID
WALL = app.WALL
WALL_US = app.WALL_US

RUN_STEPS = "omnivia_runtime_run_steps"
STEP_STATES = "omnivia_runtime_run_step_states"
ATTEMPTS = "omnivia_runtime_attempts"
OUTCOMES = "omnivia_runtime_attempt_outcomes"
EVENTS = "omnivia_runtime_events"
JOB_ATTEMPTS = "omnivia_job_attempts"
STOP_REQUESTS = "omnivia_runtime_stop_requests"
STOP_OUTCOMES = "omnivia_runtime_stop_outcomes"
WAITS = "omnivia_runtime_waits"
WAIT_RESOLUTIONS = "omnivia_runtime_wait_resolutions"
DURABLE_JOBS = "omnivia_durable_jobs"
JOB_EVENTS = "omnivia_job_events"
TERMINALS = "omnivia_job_terminal_observations"
JOB_CONTROLS = "omnivia_application_job_controls"
OBSERVATIONS = "omnivia_workflow_run_step_observations"
BINDINGS = "omnivia_workflow_runtime_bindings"
WORKFLOW_RUNS = "omnivia_workflow_runs"

#: A failure a retry is permitted for, and one it never is. Both are frozen v1 codes, so
#: `is_error_retryable` decides them from the contract rather than from the stated class.
RETRYABLE = ApiError(
    code="internal_recoverable", message="the worker lost its connection",
    retry_class="retryable",
)
FATAL = ApiError(
    code="invalid_request", message="the step's own input is not valid",
    retry_class="non_retryable",
)


# --- one owned, migrated workspace with a started Workflow Run ----------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def served(holder: m1.Owned, **overrides: Any) -> ApplicationDispatcher:
    """The real Workflow family over this workspace, with a release authority."""
    overrides.setdefault("releases", (app.release(),))
    return app.dispatcher(holder, **overrides)


def started(holder: m1.Owned, **overrides: Any) -> tuple[ApplicationDispatcher, str]:
    """One Run of the two-step plan, admitted through the served operation."""
    dispatcher = served(holder, **overrides)
    return dispatcher, app.run_id_of(app.start(dispatcher))


def scheduler(holder: m1.Owned, *, clock: FakeClock | None = None) -> RuntimeScheduler:
    return workflow_runtime_scheduler(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        clock=FakeClock(wall=WALL) if clock is None else clock,
    )


def step_id(run_id: str, plan_step_id: str) -> str:
    """The canonical runtime step one plan step of one run is executed as."""
    return workflow_run_step_id(
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        plan_hash=app.plan().content_hash,
        step_id=plan_step_id,
    )


def count(holder: m1.Owned, table: str, where: str = "", *values: Any) -> int:
    clause = f" WHERE {where}" if where else ""
    return int(
        holder.connection.execute(
            f"SELECT COUNT(*) FROM {table}{clause}", values
        ).fetchone()[0]
    )


def statuses(holder: m1.Owned, run_step_id: str) -> list[str]:
    return [
        str(row[0])
        for row in holder.connection.execute(
            f"SELECT status FROM {STEP_STATES} WHERE workspace_id = ? "
            "AND run_step_id = ? ORDER BY state_sequence",
            (WORKSPACE_ID, run_step_id),
        ).fetchall()
    ]


def run_status(holder: m1.Owned, run_id: str) -> str:
    return str(
        holder.connection.execute(
            f"SELECT run_status FROM {EVENTS} WHERE workspace_id = ? AND run_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (WORKSPACE_ID, run_id),
        ).fetchone()[0]
    )


def job_state(holder: m1.Owned, job_id: str) -> str:
    return str(
        holder.connection.execute(
            "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    )


def ledger(holder: m1.Owned, run_id: str) -> tuple[int, ...]:
    """Every durable count a settlement could duplicate, as one comparable tuple.

    Both histories, because a cancellation writes to both: the canonical run's steps,
    attempts, outcomes, events and waits, and the durable job's attempts, events and
    terminal observations. A replay that wrote a second row anywhere moves this tuple.
    """
    return (
        count(holder, RUN_STEPS, "run_id = ?", run_id),
        count(holder, STEP_STATES),
        count(holder, ATTEMPTS, "run_id = ?", run_id),
        count(holder, OUTCOMES),
        count(holder, EVENTS, "run_id = ?", run_id),
        count(holder, JOB_ATTEMPTS, "job_id = ?", run_id),
        count(holder, STOP_REQUESTS),
        count(holder, STOP_OUTCOMES),
        count(holder, WAIT_RESOLUTIONS),
        count(holder, JOB_EVENTS, "job_id = ?", run_id),
        count(holder, TERMINALS, "job_id = ?", run_id),
    )


def terminal_observations(holder: m1.Owned, job_id: str) -> list[tuple[Any, ...]]:
    """Every terminal observation one durable job carries, oldest first."""
    return [
        tuple(row)
        for row in holder.connection.execute(
            "SELECT terminal_observation_number, attempt_number, terminal_state, "
            f"finished_at_us, cancellation_reason, provenance_kind FROM {TERMINALS} "
            "WHERE workspace_id = ? AND job_id = ? "
            "ORDER BY terminal_observation_number",
            (WORKSPACE_ID, job_id),
        ).fetchall()
    ]


def job_attempt_states(holder: m1.Owned, job_id: str) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT attempt_number, state, finished_at_us FROM {JOB_ATTEMPTS} "
            "WHERE workspace_id = ? AND job_id = ? ORDER BY attempt_number",
            (WORKSPACE_ID, job_id),
        ).fetchall()
    ]


# --- a started run is executable ----------------------------------------------------


def test_a_start_opens_exactly_the_sealed_plans_steps(owned: m1.Owned) -> None:
    """Derived from the plan, not allocated: identifier, ordinal and kind all come off it."""
    _dispatcher, run_id = started(owned)

    assert owned.connection.execute(
        f"SELECT run_step_id, ordinal, step_kind FROM {RUN_STEPS} "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY ordinal",
        (WORKSPACE_ID, run_id),
    ).fetchall() == [
        (step_id(run_id, "a-plan"), 1, "workflow.deterministic"),
        (step_id(run_id, "b-write"), 2, "workflow.deterministic"),
    ]
    assert statuses(owned, step_id(run_id, "a-plan")) == ["pending"]


def test_an_identical_start_replay_opens_no_second_step_claim_or_event(
    owned: m1.Owned,
) -> None:
    """The seam answers from the stored outcome, so the mutation never runs twice."""
    dispatcher, run_id = started(owned)
    before = ledger(owned, run_id)

    again = app.result(app.start(dispatcher))

    assert str(again["run"]["run_id"]) == run_id
    assert ledger(owned, run_id) == before


def test_a_conflicting_idempotency_key_opens_no_steps_at_all(owned: m1.Owned) -> None:
    """One key naming a different canonical request rebinds nothing and opens nothing."""
    dispatcher = served(
        owned, releases=(app.release(), app.release(app.plan(version="2.0.0")))
    )
    run_id = app.run_id_of(app.start(dispatcher))
    before = ledger(owned, run_id)

    response = dispatcher.dispatch(
        app.request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": app.WORKFLOW_ID, "workflow_version": "2.0.0"},
            request_id="req-conflict",
            idempotency_key="idem-start-1",
        )
    )

    assert app.code(response) == "idempotency_conflict"
    assert ledger(owned, run_id) == before
    assert count(owned, RUN_STEPS) == 2


def test_a_start_the_session_does_not_authorise_opens_no_step(owned: m1.Owned) -> None:
    """Authorization is refused before the handler, so zero unauthorized writes."""
    dispatcher = served(owned)
    narrowed = app._replace_session(dispatcher.session, scopes=frozenset())

    response = dispatcher.dispatch_for_session(
        app.request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": app.WORKFLOW_ID, "workflow_version": app.WORKFLOW_VERSION},
            request_id="req-denied",
            idempotency_key="idem-denied",
        ),
        narrowed,
    )

    assert app.code(response) == "authorization_denied"
    assert count(owned, RUN_STEPS) == count(owned, ATTEMPTS) == 0


# --- claiming, advancing, terminalizing ---------------------------------------------


def test_the_first_dependency_ready_step_is_claimed_and_the_run_holds_one_claim(
    owned: m1.Owned,
) -> None:
    """One claim per run: the durable job carries it, so a second poll finds nothing."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)

    claim = live.claim_next()

    assert claim is not None
    assert claim.run_step_id == step_id(run_id, "a-plan")
    assert claim.runtime_attempt_number == 1
    assert statuses(owned, claim.run_step_id) == ["pending", "running"]
    assert run_status(owned, run_id) == "running"
    assert job_state(owned, run_id) == "claimed"
    # The same run, and any other scheduler over the same workspace, has nothing left to
    # claim while this attempt is open.
    assert live.claim_next() is None
    assert scheduler(owned).claim_next() is None
    assert count(owned, ATTEMPTS, "run_id = ?", run_id) == 1


def test_a_dependant_is_not_ready_until_its_dependency_has_succeeded(
    owned: m1.Owned,
) -> None:
    """`succeeded`, not merely finished: the gate is the plan's own dependency edge."""
    _dispatcher, run_id = started(owned)
    gate = WorkflowStepPlan(WORKSPACE_ID)

    assert gate.ready(
        owned.connection, run_id=run_id, run_step_id=step_id(run_id, "a-plan")
    )
    assert not gate.ready(
        owned.connection, run_id=run_id, run_step_id=step_id(run_id, "b-write")
    )


def test_an_agent_component_run_is_still_planless_and_permissive(
    owned: m1.Owned,
) -> None:
    """The `agent_component` behaviour the fail-closed read must not change.

    0018's own `definition_kind` is what says this run is not this lane's business, so
    both seams answer for it exactly as a scheduler with no plan does: every step ready,
    nothing observed.
    """
    m27.seed_runtime_run(owned, definition_kind="agent_component")
    gate = WorkflowStepPlan(WORKSPACE_ID)

    assert gate.ready(owned.connection, run_id=m27.RUN_ID, run_step_id="step-any")
    assert (
        gate.observe(
            owned.connection,
            run_id=m27.RUN_ID,
            run_step_id="step-any",
            observed_at_us=WALL_US,
        )
        is None
    )
    assert count(owned, OBSERVATIONS) == 0


def test_a_run_this_workspace_does_not_hold_is_refused_by_both_seams(
    owned: m1.Owned,
) -> None:
    """No canonical run row is not a permissive answer -- it is a question refused.

    The absence used to read as "no plan gates this", which would execute an unplanned
    run. Both seams now refuse before `observe` can write a plan observation.
    """
    _dispatcher, run_id = started(owned)
    before = ledger(owned, run_id)
    gate = WorkflowStepPlan(WORKSPACE_ID)

    for seam in (
        lambda: gate.ready(
            owned.connection, run_id="run-nobody-admitted", run_step_id="step-any"
        ),
        lambda: gate.observe(
            owned.connection,
            run_id="run-nobody-admitted",
            run_step_id="step-any",
            observed_at_us=WALL_US,
        ),
    ):
        with pytest.raises(StorageError, match="holds no canonical run"):
            seam()

    assert ledger(owned, run_id) == before
    assert count(owned, OBSERVATIONS) == 0


@pytest.mark.parametrize(
    ("column", "value", "message"),
    (
        ("binding_digest", "sha256:" + "e" * 64, "does not match its recorded digest"),
        (
            "binding_byte_length",
            1,
            "does not match its recorded byte length",
        ),
        (
            "binding_id",
            "binding-somebody-elses",
            "disagrees with the columns it is indexed by",
        ),
    ),
    ids=("digest-drift", "length-drift", "cross-linked-identity"),
)
def test_drifted_binding_evidence_stops_a_claim_before_anything_is_written(
    tmp_path: Path, column: str, value: object, message: str
) -> None:
    """Execution fails closed on exactly what `workflow.inspect` fails closed on.

    The run is queued and perfectly claimable; the only thing wrong with it is binding
    evidence edited outside this database's guards. The claim must refuse before the
    durable job is claimed, before an application or runtime attempt exists, and before
    any step status, plan observation or run event is written -- so the whole ledger is
    compared before and after.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    before = ledger(first, run_id)
    assert job_state(first, run_id) == "queued"
    first.connection.close()

    # The file edited with nothing in the way, which is the only way to produce the
    # state a verified read exists for: the row is append-only and CHECK-guarded, so
    # this edit is unreachable from inside the runtime.
    raw = sqlite3.connect(str(path))
    try:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute(
            f"DROP TRIGGER omnivia_guard_{BINDINGS.removeprefix('omnivia_')}_update"
        )
        raw.execute(f"UPDATE {BINDINGS} SET {column} = ?", (value,))
        raw.commit()
    finally:
        raw.close()

    second = m1.take_ownership(path)
    try:
        with pytest.raises(StorageError, match=message):
            scheduler(second).claim_next()

        assert ledger(second, run_id) == before
        assert job_state(second, run_id) == "queued"
        assert statuses(second, step_id(run_id, "a-plan")) == ["pending"]
        assert count(second, OBSERVATIONS) == 0
    finally:
        second.connection.close()


def test_a_workflow_run_whose_binding_row_was_deleted_refuses_before_it_is_claimed(
    tmp_path: Path,
) -> None:
    """`definition_kind` decides, so a deleted row is drift rather than a planless run.

    The run row still says `workflow`; only `omnivia_workflow_runs` is gone, which no
    path in this build produces -- the row is append-only and its DELETE is guarded, so
    the guard is dropped on a raw handle to reach the state at all. Reading the absence
    as `agent_component` would run the whole plan ungated, so both seams refuse before
    the durable job is claimed, before an application or runtime attempt is opened, and
    before a step status, a plan observation or a run event is written.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    before = ledger(first, run_id)
    assert job_state(first, run_id) == "queued"
    first.connection.close()

    raw = sqlite3.connect(str(path))
    try:
        raw.execute(
            f"DROP TRIGGER omnivia_guard_{WORKFLOW_RUNS.removeprefix('omnivia_')}_delete"
        )
        raw.execute(f"DELETE FROM {WORKFLOW_RUNS} WHERE run_id = ?", (run_id,))
        raw.commit()
    finally:
        raw.close()

    second = m1.take_ownership(path)
    try:
        with pytest.raises(StorageError, match="holds no sealed plan binding"):
            WorkflowStepPlan(WORKSPACE_ID).ready(
                second.connection,
                run_id=run_id,
                run_step_id=step_id(run_id, "a-plan"),
            )
        with pytest.raises(StorageError, match="holds no sealed plan binding"):
            scheduler(second).claim_next()

        assert ledger(second, run_id) == before
        assert job_state(second, run_id) == "queued"
        assert statuses(second, step_id(run_id, "a-plan")) == ["pending"]
        assert count(second, OBSERVATIONS) == 0
    finally:
        second.connection.close()


def test_completing_a_step_advances_to_the_next_ready_dependency(
    owned: m1.Owned,
) -> None:
    """Settled and advanced in one transaction, so the run is never between steps."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    first = live.claim_next()
    assert first is not None

    second = live.complete(first, result_kind="runtime_completion", result={"ok": True})

    assert second is not None
    assert second.run_step_id == step_id(run_id, "b-write")
    assert second.runtime_attempt_number == 1
    assert statuses(owned, first.run_step_id) == ["pending", "running", "succeeded"]
    assert statuses(owned, second.run_step_id) == ["pending", "running"]
    # Still running, still claimed, still exactly one open attempt.
    assert run_status(owned, run_id) == "running"
    assert job_state(owned, run_id) == "claimed"
    assert count(owned, JOB_ATTEMPTS, "job_id = ?", run_id) == 1


def test_completing_the_last_step_terminalizes_the_run_and_its_job(
    owned: m1.Owned,
) -> None:
    """A run is successful only once every step of its plan is terminal."""
    dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    claim = live.complete(claim, result_kind="runtime_completion", result={"ok": True})
    assert claim is not None

    assert (
        live.complete(claim, result_kind="runtime_completion", result={"ok": True})
        is None
    )

    assert run_status(owned, run_id) == "succeeded"
    assert job_state(owned, run_id) == "succeeded"
    answer = app.result(
        dispatcher.dispatch(
            app.request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-done",
            )
        )
    )
    assert answer["run"]["state"] == "completed"
    # Both steps were observed as they were reached, at the plan's own route and position.
    assert [step["step_id"] for step in answer["observations"]] == ["a-plan", "b-write"]
    assert [step["sequence_index"] for step in answer["observations"]] == [0, 1]


def test_a_terminal_run_admits_no_further_claim(owned: m1.Owned) -> None:
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    claim = live.complete(claim, result_kind="runtime_completion", result={"ok": True})
    assert claim is not None
    live.complete(claim, result_kind="runtime_completion", result={"ok": True})

    assert scheduler(owned).claim_next() is None
    assert run_status(owned, run_id) == "succeeded"


# --- failure and bounded retry ------------------------------------------------------


def test_a_retryable_failure_reopens_the_same_step_under_a_new_attempt(
    owned: m1.Owned,
) -> None:
    """The retry is durable and in-place: same step, next attempt, run still running."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None

    retried = live.fail(claim, failure=RETRYABLE)

    assert retried is not None
    assert retried.run_step_id == claim.run_step_id
    assert retried.runtime_attempt_number == 2
    assert statuses(owned, claim.run_step_id) == [
        "pending",
        "running",
        "pending",
        "running",
    ]
    assert owned.connection.execute(
        f"SELECT status FROM {OUTCOMES} WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("failed",)
    assert run_status(owned, run_id) == "running"
    assert job_state(owned, run_id) == "claimed"


def test_an_exhausted_attempt_budget_fails_the_step_the_run_and_the_job(
    owned: m1.Owned,
) -> None:
    """The bound is the durable job's own `max_attempts`, not a number chosen here."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None

    for _ in range(2):
        claim = live.fail(claim, failure=RETRYABLE)
        assert claim is not None
    assert claim.runtime_attempt_number == 3
    assert live.fail(claim, failure=RETRYABLE) is None

    assert statuses(owned, claim.run_step_id)[-1] == "failed"
    assert run_status(owned, run_id) == "failed"
    assert job_state(owned, run_id) == "failed"
    # The second step was never started, and is not reported as having finished.
    assert statuses(owned, step_id(run_id, "b-write")) == ["pending"]


def test_a_non_retryable_failure_fails_the_run_without_spending_the_budget(
    owned: m1.Owned,
) -> None:
    """Retryability is the accepted contract's, decided from the frozen error code."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None

    assert live.fail(claim, failure=FATAL) is None

    assert count(owned, ATTEMPTS, "run_id = ?", run_id) == 1
    assert run_status(owned, run_id) == "failed"
    assert job_state(owned, run_id) == "failed"


# --- illegal, late and stale settlement ---------------------------------------------


def test_a_settlement_naming_a_different_attempt_writes_nothing(
    owned: m1.Owned,
) -> None:
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    before = ledger(owned, run_id)

    with pytest.raises(RuntimeSchedulingError):
        live.complete(
            replace(claim, runtime_attempt_id="attempt-someone-elses"),
            result_kind="runtime_completion",
            result={"ok": True},
        )

    assert ledger(owned, run_id) == before


def test_a_second_settlement_of_one_claim_writes_nothing(owned: m1.Owned) -> None:
    """A late settlement is refused rather than applied to whatever is open now."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    live.complete(claim, result_kind="runtime_completion", result={"ok": True})
    before = ledger(owned, run_id)

    with pytest.raises(RuntimeSchedulingError):
        live.complete(claim, result_kind="runtime_completion", result={"ok": True})

    assert ledger(owned, run_id) == before


def test_a_settlement_under_a_superseded_generation_writes_nothing(
    owned: m1.Owned,
) -> None:
    """Authority lost between the claim and the settlement refuses at the fence."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    before = ledger(owned, run_id)
    acquire_lease(
        owned.connection,
        m1.make_identity("svc-t0693-successor", pid=4343),
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )

    with pytest.raises(StaleGeneration):
        live.complete(claim, result_kind="runtime_completion", result={"ok": True})

    assert ledger(owned, run_id) == before


def test_a_claim_from_another_owner_cannot_be_settled_here(owned: m1.Owned) -> None:
    """A claim states who made it; a scheduler that did not make it settles nothing."""
    _dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None
    before = ledger(owned, run_id)

    stranger = RuntimeScheduler(
        connection=owned.connection,
        identity=m1.make_identity("svc-t0693-stranger", pid=4444),
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        clock=FakeClock(wall=WALL),
    )
    with pytest.raises(RuntimeSchedulingError):
        stranger.complete(claim, result_kind="runtime_completion", result={"ok": True})

    assert ledger(owned, run_id) == before


# --- durable waits: restart, adoption, resolution -----------------------------------


def suspend(holder: m1.Owned, claim: RuntimeClaim) -> str:
    """Suspend a claimed step on one durable external-signal wait."""
    with runtime_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.open_wait(
            wait_id="wait-live-1",
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            kind="external_signal",
            created_at_us=WALL_US,
            resume_digest="sha256:" + "9" * 64,
        )
        writer.record_step_status(
            run_step_id=claim.run_step_id,
            status="waiting",
            observed_at_us=WALL_US,
        )
        writer.append_run_event(
            run_id=claim.run_id,
            runtime_event_id="evt-live-waiting",
            occurred_at_us=WALL_US,
            event_kind="wait_opened",
            run_status="waiting",
            run_step_id=claim.run_step_id,
        )
    return "wait-live-1"


def resolve(
    dispatcher: ApplicationDispatcher,
    run_id: str,
    *,
    wait_id: str = "wait-live-1",
    resolution: str = "external_signal",
    request_id: str = "req-resolve",
    idempotency_key: str = "idem-resolve",
) -> Any:
    return dispatcher.dispatch(
        app.request(
            WORKFLOW_CONTROL_OPERATION,
            {
                "run_id": run_id,
                "action": "resolve_wait",
                "wait_id": wait_id,
                "resolution": resolution,
                "reason": "operator.resolved",
            },
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    )


def test_a_durable_wait_survives_a_restart_and_is_adopted_not_interrupted(
    tmp_path: Path,
) -> None:
    """The wait, its step and its running attempt are preserved; only the claim moves."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    claim = scheduler(first).claim_next()
    assert claim is not None
    suspend(first, claim)
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        assert second.generation != first.generation
        recovery = recover_runtime_startup(scheduler(second))

        adopted = [job for job in recovery.jobs if job.run_id == run_id]
        assert len(adopted) == 1
        assert adopted[0].classification == CLASSIFICATION_DURABLE_OPEN_WAIT
        assert adopted[0].adopted is True
        assert adopted[0].wait_id == "wait-live-1"
        # Nothing was interrupted: the attempt is still open and the wait still pending.
        assert count(second, OUTCOMES) == 0
        assert count(second, WAIT_RESOLUTIONS) == 0
        assert statuses(second, claim.run_step_id)[-1] == "waiting"
    finally:
        second.connection.close()


def test_resolving_an_adopted_wait_resumes_its_exact_step_and_finishes_the_run(
    tmp_path: Path,
) -> None:
    """The full arc: suspend, restart, adopt, resolve through the public operation, finish."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    claim = scheduler(first).claim_next()
    assert claim is not None
    suspend(first, claim)
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        recover_runtime_startup(scheduler(second))
        clock = FakeClock(wall=WALL)
        clock.advance_wall(2.0)
        dispatcher = served(
            second, tag="resumed", wait_policy=lambda *a, **k: None, clock=clock
        )

        answer = app.result(resolve(dispatcher, run_id))

        assert answer["disposition"] == "wait_resolved"
        assert answer["run"]["run_status"] == "running"
        # The exact step the wait suspended is running again, under the attempt it had.
        assert statuses(second, claim.run_step_id)[-1] == "running"
        assert count(second, ATTEMPTS, "run_id = ?", run_id) == 1

        # And the run can still be finished, because the claim is read back from rows.
        live = scheduler(second, clock=clock)
        resumed = live.resume_claim(run_id)
        assert resumed is not None
        assert resumed.runtime_attempt_id == claim.runtime_attempt_id
        nxt = live.complete(
            resumed, result_kind="runtime_completion", result={"ok": True}
        )
        assert nxt is not None
        assert live.complete(nxt, result_kind="runtime_completion", result={"ok": True}) is None
        assert run_status(second, run_id) == "succeeded"
    finally:
        second.connection.close()


def test_an_identical_resolve_wait_replay_returns_the_stored_workflow_result(
    owned: m1.Owned,
) -> None:
    """The public result is what the mutation stored, so a replay is those exact bytes."""
    clock = FakeClock(wall=WALL)
    dispatcher, run_id = started(
        owned, wait_policy=lambda *a, **k: None, clock=clock
    )
    claim = scheduler(owned).claim_next()
    assert claim is not None
    suspend(owned, claim)
    clock.advance_wall(3.0)
    first = app.result(resolve(dispatcher, run_id))

    again = app.result(resolve(dispatcher, run_id))

    assert again == first
    assert count(owned, WAIT_RESOLUTIONS) == 1
    # The stored idempotent outcome is the Workflow answer, not the wait authority's own
    # mapping: what was served and what was recorded are the same document.
    stored = owned.connection.execute(
        "SELECT outcome_json FROM omnivia_idempotency_outcomes o "
        "JOIN omnivia_idempotency_claims c ON c.claim_id = o.claim_id "
        "WHERE c.operation = 'workflow.control'"
    ).fetchone()
    assert stored is not None
    assert '"disposition":"wait_resolved"' in str(stored[0])


def test_a_conflicting_resolve_wait_replay_fails_closed(owned: m1.Owned) -> None:
    """A second key carrying a different resolution is a conflict, not a second answer."""
    clock = FakeClock(wall=WALL)
    dispatcher, run_id = started(
        owned, wait_policy=lambda *a, **k: None, clock=clock
    )
    claim = scheduler(owned).claim_next()
    assert claim is not None
    suspend(owned, claim)
    clock.advance_wall(3.0)
    app.result(resolve(dispatcher, run_id))
    before = ledger(owned, run_id)

    response = resolve(
        dispatcher,
        run_id,
        resolution="cancelled",
        request_id="req-resolve-2",
        idempotency_key="idem-resolve-2",
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "conflict"
    assert ledger(owned, run_id) == before


def test_a_policy_refusal_leaves_the_wait_pending_and_the_step_waiting(
    owned: m1.Owned,
) -> None:
    """A build that refuses a resolution performs none of it."""

    def deny(*_args: Any, **_kwargs: Any) -> None:
        raise WaitPolicyDenied("this operator may not resolve this wait")

    clock = FakeClock(wall=WALL)
    dispatcher, run_id = started(owned, wait_policy=deny, clock=clock)
    claim = scheduler(owned).claim_next()
    assert claim is not None
    suspend(owned, claim)
    clock.advance_wall(3.0)
    before = ledger(owned, run_id)

    response = resolve(dispatcher, run_id)

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "authorization_denied"
    assert ledger(owned, run_id) == before
    assert statuses(owned, claim.run_step_id)[-1] == "waiting"


# --- cancellation --------------------------------------------------------------------


def test_cancelling_a_queued_run_terminalizes_both_histories_together(
    owned: m1.Owned,
) -> None:
    """Nothing had claimed it, so the durable job carries no attempt -- and says so.

    A queued job is not claimed here to manufacture one: 0015 admits a `cancelled`
    observation naming no attempt when the job has none, and that is the truthful record
    of work that never started. What the cancellation must not leave behind is a queued
    row the scheduler still lists, which is what the last assertion is.
    """
    dispatcher, run_id = started(owned)
    assert job_state(owned, run_id) == "queued"

    answer = app.result(app.cancel(dispatcher, run_id))

    assert answer["disposition"] == "cancellation_accepted"
    assert answer["run"]["state"] == "cancelled"
    assert run_status(owned, run_id) == "cancelled"
    assert job_state(owned, run_id) == "cancelled"
    assert job_attempt_states(owned, run_id) == []
    assert terminal_observations(owned, run_id) == [
        (1, None, "cancelled", WALL_US, "operator.cancelled", "service_committed")
    ]
    # No forged `job.cancel`: 0036 admitted that observation through the accepted stop.
    assert count(owned, JOB_CONTROLS) == 0
    assert scheduler(owned).claim_next() is None


def test_cancelling_a_claimed_run_closes_its_work_and_blocks_later_settlement(
    owned: m1.Owned,
) -> None:
    """Cancellation is not a request here: the attempt and the step close with the run."""
    dispatcher, run_id = started(owned)
    live = scheduler(owned)
    claim = live.claim_next()
    assert claim is not None

    answer = app.result(app.cancel(dispatcher, run_id))

    assert answer["disposition"] == "cancellation_accepted"
    assert answer["run"]["state"] == "cancelled"
    assert statuses(owned, claim.run_step_id)[-1] == "cancelled"
    assert owned.connection.execute(
        f"SELECT status FROM {OUTCOMES} WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("cancelled",)
    # The claimed job is settled in the same transaction, and the application attempt
    # it was claimed under is closed as what ended it.
    assert job_state(owned, run_id) == "cancelled"
    assert job_attempt_states(owned, run_id) == [
        (claim.application_attempt_number, "cancelled", WALL_US)
    ]
    assert terminal_observations(owned, run_id) == [
        (
            1,
            claim.application_attempt_number,
            "cancelled",
            WALL_US,
            "operator.cancelled",
            "service_committed",
        )
    ]
    assert count(owned, JOB_CONTROLS) == 0

    # Later settlement is refused, and the scheduler will not claim the run again.
    before = ledger(owned, run_id)
    with pytest.raises(RuntimeSchedulingError):
        live.complete(claim, result_kind="runtime_completion", result={"ok": True})
    with pytest.raises(RuntimeSchedulingError):
        live.fail(claim, failure=RETRYABLE)
    assert ledger(owned, run_id) == before
    assert scheduler(owned).claim_next() is None


def test_cancelling_a_waiting_run_releases_the_wait_holding_it(
    owned: m1.Owned,
) -> None:
    """A cancelled run holds no unresolved wait, which is what a restart would read as open work."""
    dispatcher, run_id = started(owned)
    claim = scheduler(owned).claim_next()
    assert claim is not None
    suspend(owned, claim)

    app.result(app.cancel(dispatcher, run_id))

    assert owned.connection.execute(
        f"SELECT status FROM {WAIT_RESOLUTIONS} WHERE wait_id = ?", ("wait-live-1",)
    ).fetchone() == ("cancelled",)
    assert run_status(owned, run_id) == "cancelled"
    assert statuses(owned, claim.run_step_id)[-1] == "cancelled"
    assert owned.connection.execute(
        f"SELECT status FROM {OUTCOMES} WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("cancelled",)
    assert job_state(owned, run_id) == "cancelled"
    assert job_attempt_states(owned, run_id) == [
        (claim.application_attempt_number, "cancelled", WALL_US)
    ]
    assert len(terminal_observations(owned, run_id)) == 1


def test_repeating_a_cancellation_adds_no_second_stop_or_event(
    owned: m1.Owned,
) -> None:
    dispatcher, run_id = started(owned)
    claim = scheduler(owned).claim_next()
    assert claim is not None
    first = app.result(app.cancel(dispatcher, run_id))
    before = ledger(owned, run_id)

    again = app.result(app.cancel(dispatcher, run_id))

    assert again == first
    assert ledger(owned, run_id) == before
    assert len(terminal_observations(owned, run_id)) == 1


def test_cancelling_an_already_cancelled_run_downgrades_neither_history(
    owned: m1.Owned,
) -> None:
    """A second, differently-keyed cancellation is not a replay -- it runs, and settles
    as ignored. Neither the run's stream nor the job's terminal history may move."""
    dispatcher, run_id = started(owned)
    app.result(app.cancel(dispatcher, run_id))
    before = ledger(owned, run_id)
    observed = terminal_observations(owned, run_id)

    answer = app.result(
        app.cancel(
            dispatcher,
            run_id,
            request_id="req-cancel-2",
            idempotency_key="idem-cancel-2",
        )
    )

    assert answer["disposition"] == "cancellation_ignored_already_terminal"
    assert job_state(owned, run_id) == "cancelled"
    assert terminal_observations(owned, run_id) == observed
    # The second stop request and its ignored outcome are the only new rows.
    assert ledger(owned, run_id) == tuple(
        value + 1 if index in (6, 7) else value
        for index, value in enumerate(before)
    )


#: Every table an accepted cancellation writes to, with the scope and order each is read
#: in. `SELECT *` rather than named columns, so a column this test never thought to name
#: is compared too: what an ignored cancellation has to survive is that *nothing* moved,
#: not that nothing anybody listed here moved.
_RUN_WORK_READS: tuple[tuple[str, str, str], ...] = (
    (WAITS, "workspace_id = ? AND run_id = ?", "wait_id"),
    (ATTEMPTS, "workspace_id = ? AND run_id = ?", "attempt_id"),
    (RUN_STEPS, "workspace_id = ? AND run_id = ?", "ordinal"),
    (JOB_ATTEMPTS, "workspace_id = ? AND job_id = ?", "attempt_number"),
    (JOB_EVENTS, "workspace_id = ? AND job_id = ?", "sequence"),
    (TERMINALS, "workspace_id = ? AND job_id = ?", "terminal_observation_number"),
)

#: The application seam's own ledgers. One request that runs adds exactly one row to
#: each; a request answered from a stored outcome adds none.
_SEAM_TABLES: tuple[str, ...] = (
    "omnivia_application_audit_events",
    "omnivia_idempotency_claims",
    "omnivia_idempotency_outcomes",
    "omnivia_mutation_executions",
)


def run_work(holder: m1.Owned, run_id: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every durable row a cancellation would close or settle, whole and in a fixed order.

    The run's own work and the durable job's history together, because an accepted
    cancellation writes to both and an ignored one must write to neither. Keyed by table
    so a failure names which history moved rather than which tuple position did.
    """
    snapshot = {
        table: [
            tuple(row)
            for row in holder.connection.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY {order}",
                (WORKSPACE_ID, run_id),
            ).fetchall()
        ]
        for table, where, order in _RUN_WORK_READS
    }
    # Scoped by their own keys rather than by the run: the durable job row carries no
    # workspace column, and a resolution, an outcome or a step status is addressed by the
    # wait, attempt or step it belongs to rather than by the run.
    snapshot[WAIT_RESOLUTIONS] = [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT * FROM {WAIT_RESOLUTIONS} WHERE workspace_id = ? ORDER BY wait_id",
            (WORKSPACE_ID,),
        ).fetchall()
    ]
    snapshot[DURABLE_JOBS] = [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT * FROM {DURABLE_JOBS} WHERE job_id = ?", (run_id,)
        ).fetchall()
    ]
    snapshot[OUTCOMES] = [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT * FROM {OUTCOMES} WHERE workspace_id = ? ORDER BY attempt_id",
            (WORKSPACE_ID,),
        ).fetchall()
    ]
    snapshot[STEP_STATES] = [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT * FROM {STEP_STATES} WHERE workspace_id = ? "
            "ORDER BY run_step_id, state_sequence",
            (WORKSPACE_ID,),
        ).fetchall()
    ]
    return snapshot


def run_events(holder: m1.Owned, run_id: str) -> list[tuple[Any, ...]]:
    """One run's whole event stream, oldest first."""
    return [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT * FROM {EVENTS} WHERE workspace_id = ? AND run_id = ? "
            "ORDER BY sequence",
            (WORKSPACE_ID, run_id),
        ).fetchall()
    ]


def seam_rows(holder: m1.Owned) -> dict[str, set[tuple[Any, ...]]]:
    """The application seam's ledgers as row sets, so a diff names what one request added."""
    return {
        "audit": {
            tuple(row)
            for row in holder.connection.execute(
                "SELECT audit_ref, operation, outcome_class FROM "
                "omnivia_application_audit_events"
            ).fetchall()
        },
        "claims": {
            tuple(row)
            for row in holder.connection.execute(
                "SELECT claim_id, operation, idempotency_key, audit_ref FROM "
                "omnivia_idempotency_claims"
            ).fetchall()
        },
        "outcomes": {
            tuple(row)
            for row in holder.connection.execute(
                "SELECT outcome_id, claim_id, outcome_branch FROM "
                "omnivia_idempotency_outcomes"
            ).fetchall()
        },
        "executions": {
            tuple(row)
            for row in holder.connection.execute(
                "SELECT execution_id, operation, execution_kind, claim_id FROM "
                "omnivia_mutation_executions"
            ).fetchall()
        },
    }


def test_an_ignored_cancellation_leaves_the_open_work_of_a_terminal_run_untouched(
    owned: m1.Owned,
) -> None:
    """A finished run's still-open work is not this cancellation's to close.

    The state is written by hand because no guarded path in this build produces it: only
    the event stream is moved to `cancelled`, leaving the runtime attempt running, the
    wait pending, the step `waiting` and the durable job claimed. That is what RT-109
    reads as `contradictory_history`, and it is exactly the shape that tempts a later
    cancellation to tidy up -- close the wait, cancel the attempt, settle the job -- and
    so attribute those closures to a stop the ledger is about to record as having changed
    nothing.

    The handler reads terminality inside the fence *before* `_release_run_work`, and this
    is the consequence stated at full width: every run-work and durable-job row is byte
    for byte what the tamper left, no runtime event is appended, and the only new rows
    anywhere are this request's own claim, its success outcome, its `succeeded` audit and
    mutation record, its stop request and the ignored outcome that settles it.
    """
    dispatcher, run_id = started(owned)
    claim = scheduler(owned).claim_next()
    assert claim is not None
    wait_id = suspend(owned, claim)

    # The tamper, and nothing besides: one appended event. `waiting -> cancelled` is a
    # transition 0018 admits, so the run is genuinely terminal to every reader while the
    # work it was doing stays open.
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id="evt-tampered-terminal",
            occurred_at_us=WALL_US,
            event_kind="run.cancelled",
            run_status="cancelled",
            message="finished outside this lane",
        )
    assert run_status(owned, run_id) == "cancelled"
    assert job_state(owned, run_id) == "claimed"
    assert owned.connection.execute(
        f"SELECT COUNT(*) FROM {WAITS} w WHERE w.wait_id = ? AND NOT EXISTS ("
        f"SELECT 1 FROM {WAIT_RESOLUTIONS} r WHERE r.wait_id = w.wait_id)",
        (wait_id,),
    ).fetchone() == (1,)
    assert count(owned, OUTCOMES, "attempt_id = ?", claim.runtime_attempt_id) == 0

    before = run_work(owned, run_id)
    events_before = run_events(owned, run_id)
    seam_before = seam_rows(owned)

    answer = app.result(
        app.cancel(
            dispatcher,
            run_id,
            request_id="req-cancel-open-work",
            idempotency_key="idem-cancel-open-work",
        )
    )

    assert answer["disposition"] == "cancellation_ignored_already_terminal"
    # Not one row of either history moved: the wait is still pending, the runtime attempt
    # still has no outcome, the step is still `waiting`, and the durable job is still
    # claimed with its application attempt running and no terminal observation.
    assert run_work(owned, run_id) == before
    assert run_events(owned, run_id) == events_before
    assert statuses(owned, claim.run_step_id)[-1] == "waiting"
    assert count(owned, JOB_CONTROLS) == 0

    # What *was* written: this request's stop request, the outcome that ignored it, and
    # the seam's own four rows -- one each, and nothing else anywhere.
    assert owned.connection.execute(
        f"SELECT run_id, requested_by, reason FROM {STOP_REQUESTS}"
    ).fetchall() == [(run_id, app.PRINCIPAL, "operator.cancelled")]
    assert owned.connection.execute(
        f"SELECT outcome, runtime_event_sequence FROM {STOP_OUTCOMES}"
    ).fetchall() == [("ignored_already_terminal", None)]

    added = {
        ledger: seam_rows(owned)[ledger] - rows
        for ledger, rows in seam_before.items()
    }
    assert [len(rows) for rows in added.values()] == [1, 1, 1, 1]
    (audit_ref, operation, outcome_class) = next(iter(added["audit"]))
    assert (operation, outcome_class) == (WORKFLOW_CONTROL_OPERATION, "succeeded")
    (claim_id, claimed_operation, key, claim_audit) = next(iter(added["claims"]))
    assert (claimed_operation, key, claim_audit) == (
        WORKFLOW_CONTROL_OPERATION,
        "idem-cancel-open-work",
        audit_ref,
    )
    assert [row[1:] for row in added["outcomes"]] == [(claim_id, "success")]
    assert [row[1:] for row in added["executions"]] == [
        (WORKFLOW_CONTROL_OPERATION, "executed", claim_id)
    ]
    # The stop the ledger settled is this request's own, carried by the same audit.
    assert owned.connection.execute(
        f"SELECT audit_ref FROM {STOP_REQUESTS}"
    ).fetchall() == [(audit_ref,)]


def test_cancelling_a_run_requeued_by_recovery_records_the_attempt_it_took(
    tmp_path: Path,
) -> None:
    """The one shape that cannot be cancelled without an attempt, and why.

    A restart recovers an interrupted Workflow job by failing its attempt, recording a
    terminal observation for that failure and requeuing the job. 0015 will not then
    accept a `cancelled` observation over a job whose last attempt failed, so the
    cancellation claims the job and opens the attempt it is recorded on -- truthfully,
    because this instance did take the job in order to cancel it, and in the same
    transaction that cancels it.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    assert scheduler(first).claim_next() is not None
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        recovery = recover_runtime_startup(scheduler(second))
        assert [job.classification for job in recovery.jobs if job.run_id == run_id] == [
            "orphan_attempt"
        ]
        assert job_state(second, run_id) == "queued"
        assert job_attempt_states(second, run_id) == [(1, "failed", WALL_US)]
        assert len(terminal_observations(second, run_id)) == 1

        answer = app.result(app.cancel(served(second, tag="recovered"), run_id))

        assert answer["disposition"] == "cancellation_accepted"
        assert job_state(second, run_id) == "cancelled"
        assert job_attempt_states(second, run_id) == [
            (1, "failed", WALL_US),
            (2, "cancelled", WALL_US),
        ]
        assert [row[:3] for row in terminal_observations(second, run_id)] == [
            (1, 1, "failed"),
            (2, 2, "cancelled"),
        ]
        assert count(second, JOB_CONTROLS, "operation = 'job.cancel'") == 0
    finally:
        second.connection.close()


def test_a_cancelled_run_is_read_as_finished_rather_than_contradictory(
    tmp_path: Path,
) -> None:
    """Both histories are terminal, so a restart classifies the pair and repairs nothing.

    RT-109 consults no stop ledger to reach this: it reads a `cancelled` run beside a
    `cancelled` job, which is the state the cancellation actually commits. A terminal run
    beside a live job would be `contradictory_history` here, as it always was.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    dispatcher, run_id = started(first)
    claim = scheduler(first).claim_next()
    assert claim is not None
    app.result(app.cancel(dispatcher, run_id))
    assert job_state(first, run_id) == "cancelled"
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        recovery = recover_runtime_startup(scheduler(second))

        found = [job for job in recovery.jobs if job.run_id == run_id]
        assert len(found) == 1
        assert found[0].classification == CLASSIFICATION_TERMINAL_HISTORY
        assert found[0].detail is None
        assert scheduler(second).claim_next() is None
    finally:
        second.connection.close()


def test_a_terminal_run_beside_a_live_job_is_still_contradictory_history(
    owned: m1.Owned,
) -> None:
    """The half-written history the cancellation no longer leaves is still reported.

    Written here by hand precisely because no path in this build produces it: an
    accepted stop is no longer an excuse for it, so a startup pass that met one would
    be meeting evidence something outside these paths changed.
    """
    _dispatcher, run_id = started(owned)
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id="evt-live-cancelled",
            occurred_at_us=WALL_US,
            event_kind="run.cancelled",
            run_status="cancelled",
            message="operator.cancelled",
        )

    recovery = recover_runtime_startup(scheduler(owned))

    found = [job for job in recovery.jobs if job.run_id == run_id]
    assert len(found) == 1
    assert found[0].classification == CLASSIFICATION_CONTRADICTORY_HISTORY
    assert found[0].detail is not None
    assert "disagree about being finished" in found[0].detail


# --- governed journal and boundary results across a restart --------------------------


def test_the_governed_journal_and_boundary_records_replay_without_duplication(
    tmp_path: Path,
) -> None:
    """T-0688's own authorities decide the replay; a restart changes none of their answers."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    _dispatcher, run_id = started(first)
    binding = read_runtime_definition_binding(
        first.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )
    assert binding is not None
    document = journal_bundle(run_id)
    applied = ip07.apply(
        first, binding, document, journal_payload(run_id), stage="R2"
    )
    assert applied.disposition == "applied"
    settle_completion(first, run_id)
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        rebound = read_runtime_definition_binding(
            second.connection, workspace_id=WORKSPACE_ID, run_id=run_id
        )
        assert rebound is not None

        # The identical bundle after a restart is a replay, not a second event: the
        # journal's own governance decides that, and a restart changes none of it.
        replayed = ip07.apply(
            second, rebound, document, journal_payload(run_id), stage="R2"
        )
        assert replayed.disposition == "replayed"
        assert replayed.produced_revision == applied.produced_revision
        assert (
            len(
                read_runtime_journal_events(
                    second.connection, workspace_id=WORKSPACE_ID, run_id=run_id
                )
            )
            == 1
        )

        # And the settlement record cannot be applied twice: 0027 admits one completion
        # decision per Run, so a repeated settlement is refused rather than recorded.
        before = count(second, "omnivia_workflow_run_completions")
        with pytest.raises((StorageError, sqlite3.IntegrityError)):
            settle_completion(second, run_id, evidence=False)
        assert count(second, "omnivia_workflow_run_completions") == before
    finally:
        second.connection.close()


#: A released Workflow whose single step delegates to a child, which is the only shape
#: 0027 accepts a boundary result against: the correlation must name the exact child its
#: parent step declares, and a step declares one only under the `WAIT` execution class.
CHILD = {
    "workflow_id": "workflow-child",
    "version": "1.0.0",
    "workflow_hash": "sha256:" + "a" * 64,
    "budget": 1,
}


def delegating_release() -> Any:
    step = StepDefinition(
        step_id="c-delegate",
        component_id="component-echo",
        component_version="1.0.0",
        execution_class=EXECUTION_CLASS_WAIT,
        child_workflow=ChildWorkflowDefinition(
            workflow_id=str(CHILD["workflow_id"]),
            version=str(CHILD["version"]),
            workflow_hash=str(CHILD["workflow_hash"]),
            budget=int(CHILD["budget"]),
        ),
    ).sealed()
    return app.release(
        materialise_workflow(
            WorkflowDefinition(
                workflow_id=app.WORKFLOW_ID, version="3.0.0", steps=(step,)
            ).sealed()
        )
    )


def test_a_boundary_result_replays_after_restart_without_a_second_settlement(
    tmp_path: Path,
) -> None:
    """The fenced parent/child boundary is closed once, and a restart does not reopen it."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    dispatcher = app.dispatcher(first, releases=(delegating_release(),))
    run_id = app.run_id_of(app.start(dispatcher, workflow_version="3.0.0"))
    open_boundary(first, run_id)
    record_boundary_result(first, run_id)
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        before = count(second, "omnivia_workflow_child_correlation_results")

        # A closed correlation admits no further result, and a second correlation under
        # the same fence is refused too -- so a replayed settlement can neither be
        # recorded twice nor quietly attributed to a new boundary.
        with pytest.raises((StorageError, sqlite3.IntegrityError)):
            record_boundary_result(second, run_id)
        with pytest.raises((StorageError, sqlite3.IntegrityError)):
            open_boundary(second, run_id)

        assert count(second, "omnivia_workflow_child_correlation_results") == before
        assert count(second, "omnivia_workflow_child_correlations") == 1
    finally:
        second.connection.close()


def open_boundary(holder: m1.Owned, run_id: str) -> None:
    """One fenced, budgeted correlation between the delegating step and its child."""
    with workflow_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.open_child_correlation(
            ChildCorrelationRecord(
                correlation_id="corr-live-1",
                parent_run_id=run_id,
                parent_step_id="c-delegate",
                child_workflow_id=str(CHILD["workflow_id"]),
                child_version=str(CHILD["version"]),
                child_workflow_hash=str(CHILD["workflow_hash"]),
                fence=1,
                budget=int(CHILD["budget"]),
                opened_at_us=WALL_US,
            )
        )


def record_boundary_result(holder: m1.Owned, run_id: str) -> None:
    del run_id
    with workflow_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.record_child_result(
            ChildResultRecord(
                correlation_id="corr-live-1",
                result_sequence=1,
                outcome="closed",
                fence=1,
                child_workflow_id=str(CHILD["workflow_id"]),
                child_version=str(CHILD["version"]),
                child_workflow_hash=str(CHILD["workflow_hash"]),
                # A closing result carries no cost: 0027 admits one on an `accepted`
                # result and refuses one here, because closing a boundary is not work.
                cost=None,
                recorded_at_us=WALL_US,
            )
        )


def test_a_completion_no_evidence_gates_is_refused_rather_than_recorded(
    owned: m1.Owned,
) -> None:
    """No fabricated success: a decision resting on nothing is not a decision."""
    _dispatcher, run_id = started(owned)

    with (
        pytest.raises(sqlite3.IntegrityError),
        workflow_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.complete_run(
            run_id=run_id,
            outcome="SUCCEEDED",
            decided_at_us=WALL_US,
            audit_ref="aud-t0693-1",
        )

    assert count(owned, "omnivia_workflow_run_completions") == 0


def journal_payload(run_id: str) -> dict[str, Any]:
    return ip07.payload(0, runId=run_id)


def journal_bundle(run_id: str) -> dict[str, Any]:
    """One `RuntimeTransitionBundle` advancing this Workflow Run by a revision."""
    event = ip07.journal_event(
        0,
        ip07.journal_genesis_link(run_id),
        runId=run_id,
        eventId=f"event-live-{run_id}",
        payloadDigest=ip07.ip06.digest_of(
            ip07.canonicalize(journal_payload(run_id))
        ),
    )
    return ip07.bundle(0, event=event, runId=run_id, bundleId=f"bundle-live-{run_id}")


def settle_completion(
    holder: m1.Owned, run_id: str, *, evidence: bool = True
) -> None:
    """The evidence a completion rests on, and the single decision it gates."""
    with workflow_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        if evidence:
            writer.record_completion_evidence(
                run_id=run_id,
                evidence_kind="workflow.plan_steps",
                evidence_digest="sha256:" + "c" * 64,
                recorded_at_us=WALL_US,
            )
        writer.complete_run(
            run_id=run_id,
            outcome="SUCCEEDED",
            decided_at_us=WALL_US,
            audit_ref="aud-t0693-1",
        )


# --- the plan's own authority over the steps it derived ------------------------------


def test_runtime_steps_that_do_not_derive_from_the_plan_are_refused_not_repaired(
    owned: m1.Owned,
) -> None:
    """Rewriting a run's step lineage would rewrite its history rather than continue it."""
    _dispatcher, run_id = started(owned)
    plan = read_workflow_plan(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        workflow_id=app.WORKFLOW_ID,
        workflow_version=app.WORKFLOW_VERSION,
    )
    assert plan is not None

    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        # A second derivation for the same run is a replay and writes nothing.
        assert open_workflow_runtime_steps(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=run_id,
            plan=plan,
            opened_at_us=WALL_US + 5_000,
        ) == (step_id(run_id, "a-plan"), step_id(run_id, "b-write"))

    drifted = replace(plan, plan_hash="sha256:" + "b" * 64)
    with (
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(StorageError, match="does not derive"),
    ):
        open_workflow_runtime_steps(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=run_id,
            plan=drifted,
            opened_at_us=WALL_US + 6_000,
        )
    assert count(owned, RUN_STEPS, "run_id = ?", run_id) == 2


def test_a_plan_observation_that_contradicts_the_sealed_route_is_refused(
    owned: m1.Owned,
) -> None:
    """0027 holds the observation to the plan's own route and position, on every claim."""
    _dispatcher, run_id = started(owned)
    scheduler(owned).claim_next()

    with (
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError),
    ):
        transaction_local_workflow_writer(
            owned.connection, workspace_id=WORKSPACE_ID
        ).connection.execute(
            "INSERT INTO omnivia_workflow_run_step_observations (workspace_id, run_id, "
            "step_id, observation_kind, route, sequence_index, branch_outcome, "
            "branch_reason, observed_at_us) VALUES (?, ?, 'a-plan', 'plan', 'EFFECT', "
            "0, NULL, NULL, ?)",
            (WORKSPACE_ID, run_id, WALL_US + 9_000),
        )


# --- malformed control ---------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    (
        {"action": "resolve_wait", "resolution": "external_signal"},
        {"action": "cancel", "resolution": "external_signal"},
        {"action": "resume", "wait_id": "wait-live-1"},
    ),
    ids=("resolve-without-wait", "cancel-with-resolution", "unknown-action"),
)
def test_malformed_control_fields_are_refused_with_zero_writes(
    owned: m1.Owned, payload: dict[str, Any]
) -> None:
    """An action-dependent field that does not belong to its action performs nothing."""
    dispatcher, run_id = started(owned)
    before = ledger(owned, run_id)

    response = dispatcher.dispatch(
        app.request(
            WORKFLOW_CONTROL_OPERATION,
            {"run_id": run_id, **payload},
            request_id="req-malformed",
            idempotency_key="idem-malformed",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "invalid_request"
    assert ledger(owned, run_id) == before


# --- the production composition ------------------------------------------------------


class _InstallationService:
    """Construction-only shape; its bound installation handlers are never invoked here."""

    authority = SimpleNamespace(installation_id="inst-t0693-live")


@pytest.fixture
def workspace(tmp_path: Path) -> ServiceSettings:
    """A workspace created exactly as `--init` creates one, ready to be served."""
    result = initialise_workspace(
        workspace_root=tmp_path / "workspace",
        installation_root=tmp_path / "installation",
    )
    assert result.status is not WorkspaceInitStatus.REFUSED, result.reason
    return ServiceSettings(
        workspace_root=tmp_path / "workspace",
        installation_root=tmp_path / "installation",
    )


def test_the_production_runner_start_runs_the_runtime_startup_pass(
    workspace: ServiceSettings,
) -> None:
    """Recovery is composed into the runner's own lifecycle, not driven from a test.

    A first instance starts a Run and suspends its claimed step on a durable wait, then
    goes away. A second `ServiceRunner.start()` -- the real production startup sequence,
    nothing constructed by hand -- is what classifies and adopts it.
    """
    first = ServiceRunner(workspace, clock=FakeClock(wall=WALL))
    report = first.start()
    assert report.ready, report.to_dict()
    workspace_id = report.workspace_id
    assert workspace_id is not None
    holder = m1.Owned(
        connection=first.connection,  # type: ignore[arg-type]
        identity=first.identity,  # type: ignore[arg-type]
        generation=first.generation,  # type: ignore[arg-type]
        path=workspace.workspace_root,
    )
    dispatcher = app.dispatcher(
        holder, releases=(app.release(),), workspace_id=workspace_id
    )
    run_id = app.run_id_of(
        app.start(dispatcher, workspace_id=workspace_id)
    )
    live = first.runtime_scheduler()
    assert live is not None
    claim = live.claim_next()
    assert claim is not None and claim.run_id == run_id
    _suspend_in(holder, claim, workspace_id)
    first.stop()

    second = ServiceRunner(workspace, clock=FakeClock(wall=WALL))
    try:
        again = second.start()

        assert again.ready, again.to_dict()
        assert second.runtime_recovery is not None
        adopted = second.runtime_recovery.classified(CLASSIFICATION_DURABLE_OPEN_WAIT)
        assert [job.run_id for job in adopted] == [run_id]
        assert adopted[0].adopted is True
        # The suspension itself was preserved rather than recovered.
        assert (
            second.connection.execute(  # type: ignore[union-attr]
                f"SELECT COUNT(*) FROM {OUTCOMES}"
            ).fetchone()[0]
            == 0
        )
    finally:
        second.stop()


def _suspend_in(holder: m1.Owned, claim: RuntimeClaim, workspace_id: str) -> None:
    with runtime_writer(
        holder.connection,
        holder.identity,
        workspace_id=workspace_id,
        fencing_generation=holder.generation,
    ) as writer:
        writer.open_wait(
            wait_id="wait-runner-1",
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            kind="external_signal",
            created_at_us=WALL_US,
            resume_digest="sha256:" + "9" * 64,
        )
        writer.record_step_status(
            run_step_id=claim.run_step_id,
            status="waiting",
            observed_at_us=WALL_US,
        )
        writer.append_run_event(
            run_id=claim.run_id,
            runtime_event_id="evt-runner-waiting",
            occurred_at_us=WALL_US,
            event_kind="wait_opened",
            run_status="waiting",
            run_step_id=claim.run_step_id,
        )


def test_the_production_runner_hands_out_a_scheduler_bound_to_its_own_authority(
    workspace: ServiceSettings,
) -> None:
    """One accessor, carrying this instance's connection, identity and generation."""
    runner = ServiceRunner(workspace, clock=FakeClock(wall=WALL))
    assert runner.runtime_scheduler() is None

    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        live = runner.runtime_scheduler()

        assert live is not None
        assert live.connection is runner.connection
        assert live.identity is runner.identity
        assert live.fencing_generation == runner.generation
        assert live.plan is not None
        assert runner.runtime_recovery is not None
    finally:
        runner.stop()


def test_the_production_surface_serves_workflow_against_an_injected_release_authority(
    workspace: ServiceSettings,
) -> None:
    """The ordinary production composition, with the authority a deployment supplies.

    `_build_production_application_surface` is the function `main()`'s own `serve` calls,
    and the resolver reaching it here is the one `main()` threads through from its
    embedder. Nothing about the Workflow lane is stubbed: this is the frozen catalogue,
    the twelve-check authorization seam and the real handlers.
    """
    runner = ServiceRunner(workspace, clock=FakeClock(wall=WALL))
    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        workspace_id = report.workspace_id
        assert workspace_id is not None
        probe = Dispatcher.for_service_operations(
            app.Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({workspace_id}),
                operations=frozenset(app.SERVICE_OPERATIONS),
            )
        )
        installation = build_installation_application_dispatcher(
            service=_InstallationService(),  # type: ignore[arg-type]
            principal_id=LOCAL_PRINCIPAL,
            fallback=probe,
        )

        refusing = _build_production_application_surface(
            started=runner, probe=probe, installation=installation
        )
        served_live = _build_production_application_surface(
            started=runner,
            probe=probe,
            installation=installation,
            resolve_workflow_release=app.resolver(app.release()),
        )

        # The default build cannot say what a Run would execute, and refuses.
        assert (
            app.code(
                refusing.dispatch(
                    app.request(
                        WORKFLOW_START_OPERATION,
                        {
                            "workflow_id": app.WORKFLOW_ID,
                            "workflow_version": app.WORKFLOW_VERSION,
                        },
                        request_id="req-refused",
                        idempotency_key="idem-refused",
                        workspace_id=workspace_id,
                    )
                )
            )
            == "dependency_unavailable"
        )
        # The injected one admits a Run, binds it and opens its steps.
        answer = served_live.dispatch(
            app.request(
                WORKFLOW_START_OPERATION,
                {
                    "workflow_id": app.WORKFLOW_ID,
                    "workflow_version": app.WORKFLOW_VERSION,
                },
                request_id="req-injected",
                idempotency_key="idem-injected",
                workspace_id=workspace_id,
            )
        )
        assert isinstance(answer, SuccessResponseEnvelope), answer
        run_id = str(answer.result["run"]["run_id"])
        assert answer.result["run"]["state"] == "queued"

        # And the runner's own scheduler claims it, which is what makes the lane live.
        live = runner.runtime_scheduler()
        assert live is not None
        claim = live.claim_next()
        assert claim is not None
        assert claim.run_id == run_id
        assert read_run_sequence(
            runner.connection,  # type: ignore[arg-type]
            workspace_id=workspace_id,
            run_id=run_id,
        ) == 1
    finally:
        runner.stop()


def test_a_run_with_no_open_attempt_is_classified_as_ordinary_queued_work(
    owned: m1.Owned,
) -> None:
    """The unclaimed case still reads as what it is, now that steps exist for it."""
    _dispatcher, run_id = started(owned)

    recovery = recover_runtime_startup(scheduler(owned))

    found = [job for job in recovery.jobs if job.run_id == run_id]
    assert [job.classification for job in found] == [CLASSIFICATION_NO_OPEN_ATTEMPT]
