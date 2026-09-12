"""T-0693 acceptance for the durable Workflow Run repository over migration 0027.

`storage/workflow_runs.py` is persistence and the state projection over persistence,
and nothing else. These tests hold it to five properties the migration alone cannot
state.

*It enters its own fenced transaction.* Every write is called on a bare owned
connection here -- no surrounding transaction, no mutation seam -- and either commits
under current authority or leaves the database exactly as it found it.

*A plan is a re-addressing of a sealed definition, not a second decision.* Every
stored column, each step's route included, is derived from a sealed
`MaterialisedWorkflow` through the same `StepRouter` an in-memory observation routes
with, so a stored plan and an observation of it cannot disagree.

*Admission is pinned, idempotent, and T-0688's write.* A run binds the exact
workflow, version and plan hash it names or nothing at all; repeating an equivalent
admission returns the stored binding and writes nothing; repeating it under different
pins raises. The row itself is still written by `admit_bound_run`, so
`omnivia_workflow_runs` keeps exactly one writer.

*The eight run states are a projection, never a column.* `created`, `queued`,
`running`, `waiting`, `completed`, `failed`, `cancelled` and `indeterminate` are
derived from migration 0018's own event stream and step ledger. The tests walk the
real stream rather than asserting the mapping in isolation, so a state this module
reports is one the durable triggers actually permitted.

*Illegal transitions and terminal downgrades fail closed, and typed.* The incumbent
runtime path lets 0018's trigger surface as a bare `sqlite3.IntegrityError` whose
only discriminator is its message; these refusals carry a closed `diagnostic`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_t0688_workflow_runtime_hardening_repository as ip06
import test_workflow_runs_migration as m27
from omnivia_core_runtime.execution.workflow import (
    BRANCH_MATCHED,
    BRANCH_OPERATOR_EQUALS,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_WAIT,
    ROUTE_CHILD_WORKFLOW,
    ROUTE_DETERMINISTIC,
    ROUTE_WAIT,
    BranchDefinition,
    ChildWorkflowDefinition,
    MaterialisedWorkflow,
    StepDefinition,
    WorkflowDefinition,
    materialise_workflow,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    append_run_step,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runs import (
    DIAGNOSTIC_ILLEGAL_TRANSITION,
    DIAGNOSTIC_TERMINAL_DOWNGRADE,
    DIAGNOSTIC_UNKNOWN_STATE,
    WORKFLOW_RUN_STATE_CANCELLED,
    WORKFLOW_RUN_STATE_COMPLETED,
    WORKFLOW_RUN_STATE_CREATED,
    WORKFLOW_RUN_STATE_FAILED,
    WORKFLOW_RUN_STATE_INDETERMINATE,
    WORKFLOW_RUN_STATE_QUEUED,
    WORKFLOW_RUN_STATE_RUNNING,
    WORKFLOW_RUN_STATE_TRANSITIONS,
    WORKFLOW_RUN_STATE_WAITING,
    WORKFLOW_RUN_STATES,
    WORKFLOW_RUN_TERMINAL_STATES,
    ChildCorrelationRecord,
    ChildResultRecord,
    WorkflowRunStateRefused,
    admit_workflow_run,
    read_workflow_plan,
    read_workflow_run,
    read_workspace_workflow_run_ids,
    seal_workflow_plan,
    validate_workflow_run_state_transition,
    workflow_run_state,
    workflow_writer,
)

from omnivia_core.contracts.v1.canonical_json import canonicalize

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
RUN_ID = m27.RUN_ID
BASE_US = m27.BASE_US
WORKFLOW_ID = m27.WORKFLOW_ID
WORKFLOW_VERSION = m27.WORKFLOW_VERSION
CHILD_WORKFLOW_ID = m27.CHILD_WORKFLOW_ID
CHILD_WORKFLOW_HASH = m27.CHILD_WORKFLOW_HASH
EVIDENCE_DIGEST = m27.EVIDENCE_DIGEST

#: The declared steps, given out of dependency order on purpose. A stored plan that
#: matched this order would be transcribing the authoring rather than materialising it.
DECLARED = ("d-wait", "b-compute", "e-child", "a-plan", "c-write")

#: What `materialise_workflow` orders them into, and the route each one takes.
GOLDEN_ORDER = ("a-plan", "b-compute", "c-write", "d-wait", "e-child")
GOLDEN_ROUTES = (
    ROUTE_DETERMINISTIC,
    ROUTE_DETERMINISTIC,
    ROUTE_DETERMINISTIC,
    ROUTE_WAIT,
    ROUTE_CHILD_WORKFLOW,
)

BRANCH = BranchDefinition(
    input_key="mode", operator=BRANCH_OPERATOR_EQUALS, expected_value="fast"
)


def step(step_id: str, **overrides: Any) -> StepDefinition:
    values: dict[str, Any] = {
        "step_id": step_id,
        "component_id": "component-echo",
        "component_version": "1.0.0",
        "execution_class": EXECUTION_CLASS_DETERMINISTIC,
    }
    values.update(overrides)
    return StepDefinition(**values).sealed()


def plan(version: str = WORKFLOW_VERSION) -> MaterialisedWorkflow:
    """The golden plan: five steps, one branch, one wait, one child delegation."""
    steps = (
        step("d-wait", execution_class=EXECUTION_CLASS_WAIT, depends_on=("c-write",)),
        step("b-compute", depends_on=("a-plan",), branch=BRANCH),
        step(
            "e-child",
            execution_class=EXECUTION_CLASS_WAIT,
            depends_on=("d-wait",),
            child_workflow=ChildWorkflowDefinition(
                workflow_id=CHILD_WORKFLOW_ID,
                version="1.0.0",
                workflow_hash=CHILD_WORKFLOW_HASH,
                budget=10,
            ),
        ),
        step("a-plan"),
        step("c-write", depends_on=("b-compute",)),
    )
    assert tuple(item.step_id for item in steps) == DECLARED
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID, version=version, steps=steps
    ).sealed()
    return materialise_workflow(definition)


def other_plan() -> MaterialisedWorkflow:
    """A genuinely different plan under the same workflow identity."""
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        steps=(step("a-plan"), step("z-extra", depends_on=("a-plan",))),
    ).sealed()
    return materialise_workflow(definition)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def record_audit(holder: m1.Owned, audit_ref: str) -> None:
    """One audit event, which 0027 requires a plan or completion to reference."""
    with m27.guarded(holder):
        m27.audit(holder, audit_ref)


def seal(holder: m1.Owned, materialised: MaterialisedWorkflow | None = None) -> Any:
    return seal_workflow_plan(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        plan=materialised or plan(),
        sealed_at_us=BASE_US,
        audit_ref="audit-plan",
    )


def admission(sealed: Any, **overrides: Any) -> Any:
    """A T-0688 admission whose binding pins the plan that was actually sealed."""
    document = ip06.binding(
        workflowId=sealed.workflow_id,
        workflowVersion=sealed.workflow_version,
        definitionDigest=sealed.definition_hash,
    )
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "workflow_id": sealed.workflow_id,
        "workflow_version": sealed.workflow_version,
        "plan_hash": sealed.plan_hash,
        "bound_at_us": BASE_US + 20,
        "binding": document,
    }
    values.update(overrides)
    return ip06.BoundRunAdmission(**values)


def admit(holder: m1.Owned, record: Any) -> Any:
    return admit_workflow_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record,
    )


def started(holder: m1.Owned) -> Any:
    """A sealed plan, a canonical Runtime run, and the Workflow binding between them.

    The run's event stream is opened here because a canonical Runtime run always has
    one -- `agent_runtime.admit_run` writes sequence 0 as `admitted`, and 0018's guard
    requires the stream to open exactly that way. m27's raw seed inserts the run row
    alone, so a fixture that stopped there would be a run shape production never
    produces.
    """
    record_audit(holder, "audit-plan")
    sealed = seal(holder)
    m27.seed_runtime_run(holder)
    admit(holder, admission(sealed))
    admitted(holder)
    return sealed


def count(holder: m1.Owned, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def event(
    holder: m1.Owned, sequence: int, kind: str, status: str, **overrides: Any
) -> None:
    """One runtime event, through the canonical writer 0018's trigger guards."""
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "runtime_event_id": f"evt-{sequence:04d}",
        "occurred_at_us": BASE_US + 1_000 * (sequence + 1),
        "event_kind": kind,
        "run_status": status,
    }
    values.update(overrides)
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        **values,
    )


def admitted(holder: m1.Owned) -> None:
    """Open the run's event stream, which 0018 requires to start at `admitted`."""
    event(holder, 0, "run_admitted", "admitted")


def open_step(holder: m1.Owned, run_step_id: str = "runstep-0001") -> None:
    append_run_step(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=RUN_ID,
        run_step_id=run_step_id,
        ordinal=1,
        step_kind="workflow.step",
        created_at_us=BASE_US + 30,
    )


def state_of(holder: m1.Owned) -> str:
    view = read_workflow_run(
        holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert view is not None
    return view.state


# --- plan sealing -------------------------------------------------------------------


def test_a_sealed_plan_stores_the_order_and_routes_materialisation_derived(
    owned: m1.Owned,
) -> None:
    record_audit(owned, "audit-plan")
    sealed = seal(owned)

    assert tuple(item.step_id for item in sealed.steps) == GOLDEN_ORDER
    assert tuple(item.route for item in sealed.steps) == GOLDEN_ROUTES
    assert tuple(item.sequence_index for item in sealed.steps) == (0, 1, 2, 3, 4)
    assert sealed.plan_hash == plan().content_hash
    assert sealed.definition_hash == plan().definition_hash

    by_id = {item.step_id: item for item in sealed.steps}
    assert by_id["b-compute"].depends_on == ("a-plan",)
    assert by_id["b-compute"].branch == BRANCH.preimage
    assert by_id["e-child"].child_workflow is not None
    assert by_id["e-child"].child_workflow["workflow_id"] == CHILD_WORKFLOW_ID
    # The wait class and the child route are independent facts; neither derives the
    # other, so a plan that stored one and inferred the other would be guessing.
    assert by_id["e-child"].execution_class == EXECUTION_CLASS_WAIT
    assert by_id["e-child"].route == ROUTE_CHILD_WORKFLOW


def test_a_step_carries_both_its_declared_and_its_materialised_address(
    owned: m1.Owned,
) -> None:
    record_audit(owned, "audit-plan")
    sealed = seal(owned)
    for stored in sealed.steps:
        assert stored.step_definition_hash != stored.materialised_step_hash


def test_resealing_the_same_plan_returns_the_stored_one_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    record_audit(owned, "audit-plan")
    first = seal(owned)
    assert count(owned, m27.STEPS) == len(GOLDEN_ORDER)

    assert seal(owned) == first
    assert count(owned, m27.STEPS) == len(GOLDEN_ORDER)
    assert count(owned, m27.PLANS) == 1


def test_resealing_a_different_plan_under_the_same_version_is_a_conflict(
    owned: m1.Owned,
) -> None:
    record_audit(owned, "audit-plan")
    first = seal(owned)

    with pytest.raises(StorageError, match="already sealed"):
        seal(owned, other_plan())

    assert (
        read_workflow_plan(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            workflow_id=WORKFLOW_ID,
            workflow_version=WORKFLOW_VERSION,
        )
        == first
    )


def test_sealing_a_plan_requires_the_current_fenced_owner(owned: m1.Owned) -> None:
    record_audit(owned, "audit-plan")
    with pytest.raises(StaleGeneration):
        seal_workflow_plan(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            plan=plan(),
            sealed_at_us=BASE_US,
            audit_ref="audit-plan",
        )
    assert count(owned, m27.PLANS) == 0
    assert count(owned, m27.STEPS) == 0


# --- admission ----------------------------------------------------------------------


def test_admission_binds_the_runtime_run_to_the_plan_it_pins(owned: m1.Owned) -> None:
    sealed = started(owned)

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.binding.workflow_id == WORKFLOW_ID
    assert view.binding.workflow_version == WORKFLOW_VERSION
    assert view.binding.plan_hash == sealed.plan_hash
    assert read_workspace_workflow_run_ids(
        owned.connection, workspace_id=WORKSPACE_ID
    ) == (RUN_ID,)


def test_admission_requires_the_current_fenced_owner(owned: m1.Owned) -> None:
    record_audit(owned, "audit-plan")
    sealed = seal(owned)
    m27.seed_runtime_run(owned)

    with pytest.raises(StaleGeneration):
        admit_workflow_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            admission=admission(sealed),
        )
    assert count(owned, m27.RUNS) == 0


def test_repeating_an_equivalent_admission_is_the_same_binding(owned: m1.Owned) -> None:
    sealed = started(owned)
    before = count(owned, m27.RUNS)

    # A crash-retry reads its own clock; the stored instant is what wins.
    admit(owned, admission(sealed, bound_at_us=BASE_US + 999))

    assert count(owned, m27.RUNS) == before == 1
    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.binding.bound_at_us == BASE_US + 20


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"plan_hash": "sha256:" + "9" * 64}, "is sealed as"),
        ({"workflow_version": "2.0.0"}, "no sealed plan"),
        ({"workflow_id": "workflow-other"}, "no sealed plan"),
    ),
    ids=("wrong-plan-hash", "wrong-version", "wrong-workflow"),
)
def test_an_admission_that_pins_a_plan_this_workspace_never_sealed_is_refused(
    owned: m1.Owned, overrides: dict[str, Any], message: str
) -> None:
    record_audit(owned, "audit-plan")
    sealed = seal(owned)
    m27.seed_runtime_run(owned)

    with pytest.raises(StorageError, match=message):
        admit(owned, admission(sealed, **overrides))
    assert count(owned, m27.RUNS) == 0


def test_rebinding_one_run_to_a_different_plan_is_a_conflict(owned: m1.Owned) -> None:
    sealed = started(owned)
    record_audit(owned, "audit-other-plan")
    other = seal_workflow_plan(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        plan=plan(version="2.0.0"),
        sealed_at_us=BASE_US,
        audit_ref="audit-other-plan",
    )
    assert other.plan_hash != sealed.plan_hash

    with pytest.raises(StorageError, match="already bound"):
        admit(owned, admission(other))

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.binding.workflow_version == WORKFLOW_VERSION


def test_admission_refuses_a_runtime_run_that_was_not_admitted_as_a_workflow(
    owned: m1.Owned,
) -> None:
    record_audit(owned, "audit-plan")
    sealed = seal(owned)
    m27.seed_runtime_run(owned, definition_kind="agent_component")

    with pytest.raises(sqlite3.IntegrityError, match="workflow"):
        admit(owned, admission(sealed))
    assert count(owned, m27.RUNS) == 0


# --- observations, correlations and the completion gate -----------------------------


def test_observations_are_replay_safe_and_visible_in_the_run_view(
    owned: m1.Owned,
) -> None:
    started(owned)
    with workflow_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        for index, (step_id, route) in enumerate(zip(GOLDEN_ORDER, GOLDEN_ROUTES)):
            writer.observe_plan_step(
                run_id=RUN_ID,
                step_id=step_id,
                route=route,
                sequence_index=index,
                observed_at_us=BASE_US + 40 + index,
            )
        writer.observe_branch(
            run_id=RUN_ID,
            step_id="b-compute",
            outcome=BRANCH_MATCHED,
            reason="equals",
            observed_at_us=BASE_US + 50,
        )
        # A replay of both halves collapses rather than duplicating or raising.
        writer.observe_plan_step(
            run_id=RUN_ID,
            step_id="a-plan",
            route=ROUTE_DETERMINISTIC,
            sequence_index=0,
            observed_at_us=BASE_US + 999,
        )
        writer.observe_branch(
            run_id=RUN_ID,
            step_id="b-compute",
            outcome=BRANCH_MATCHED,
            reason="equals",
            observed_at_us=BASE_US + 999,
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert tuple(item.step_id for item in view.plan_observations) == GOLDEN_ORDER
    assert tuple(item.route for item in view.plan_observations) == GOLDEN_ROUTES
    assert len(view.branch_observations) == 1
    assert view.branch_observations[0].outcome == BRANCH_MATCHED


def test_an_observation_that_contradicts_the_one_recorded_is_refused(
    owned: m1.Owned,
) -> None:
    started(owned)
    with workflow_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.observe_branch(
            run_id=RUN_ID,
            step_id="b-compute",
            outcome=BRANCH_MATCHED,
            reason="equals",
            observed_at_us=BASE_US + 50,
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="conflicts"),
        workflow_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.observe_branch(
            run_id=RUN_ID,
            step_id="b-compute",
            outcome="UNMATCHED",
            reason="equals",
            observed_at_us=BASE_US + 60,
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.branch_observations[0].outcome == BRANCH_MATCHED


def test_a_boundary_result_names_the_fence_its_correlation_was_opened_with(
    owned: m1.Owned,
) -> None:
    started(owned)
    correlation = ChildCorrelationRecord(
        correlation_id="corr-0001",
        parent_run_id=RUN_ID,
        parent_step_id="e-child",
        child_workflow_id=CHILD_WORKFLOW_ID,
        child_version="1.0.0",
        child_workflow_hash=CHILD_WORKFLOW_HASH,
        fence=1,
        budget=10,
        opened_at_us=BASE_US + 60,
    )
    with workflow_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.open_child_correlation(correlation)
        writer.record_child_result(
            ChildResultRecord(
                correlation_id="corr-0001",
                result_sequence=1,
                outcome="accepted",
                fence=1,
                child_workflow_id=CHILD_WORKFLOW_ID,
                child_version="1.0.0",
                child_workflow_hash=CHILD_WORKFLOW_HASH,
                cost=4,
                recorded_at_us=BASE_US + 70,
            )
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.correlations == (correlation,)
    assert tuple(item.outcome for item in view.correlation_results) == ("accepted",)
    assert view.correlation_results[0].cost == 4


def test_a_boundary_result_minted_under_a_foreign_fence_writes_nothing(
    owned: m1.Owned,
) -> None:
    started(owned)
    with workflow_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.open_child_correlation(
            ChildCorrelationRecord(
                correlation_id="corr-0001",
                parent_run_id=RUN_ID,
                parent_step_id="e-child",
                child_workflow_id=CHILD_WORKFLOW_ID,
                child_version="1.0.0",
                child_workflow_hash=CHILD_WORKFLOW_HASH,
                fence=1,
                budget=10,
                opened_at_us=BASE_US + 60,
            )
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="fence"),
        workflow_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.record_child_result(
            ChildResultRecord(
                correlation_id="corr-0001",
                result_sequence=1,
                outcome="accepted",
                fence=2,
                child_workflow_id=CHILD_WORKFLOW_ID,
                child_version="1.0.0",
                child_workflow_hash=CHILD_WORKFLOW_HASH,
                cost=4,
                recorded_at_us=BASE_US + 70,
            )
        )

    assert count(owned, m27.RESULTS) == 0


def test_a_completion_without_evidence_is_refused_by_the_gate_it_names(
    owned: m1.Owned,
) -> None:
    started(owned)
    record_audit(owned, "audit-completion")

    with (
        pytest.raises(sqlite3.IntegrityError, match="evidence"),
        workflow_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.complete_run(
            run_id=RUN_ID,
            outcome="SUCCEEDED",
            decided_at_us=BASE_US + 80,
            audit_ref="audit-completion",
        )

    assert count(owned, m27.COMPLETIONS) == 0
    # The refusal declines the decision; it does not destroy the run.
    assert (
        read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        is not None
    )


def test_a_completion_gated_on_recorded_evidence_is_stored(owned: m1.Owned) -> None:
    started(owned)
    record_audit(owned, "audit-completion")
    with workflow_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.record_completion_evidence(
            run_id=RUN_ID,
            evidence_kind="run.summary",
            evidence_digest=EVIDENCE_DIGEST,
            recorded_at_us=BASE_US + 75,
        )
        writer.complete_run(
            run_id=RUN_ID,
            outcome="SUCCEEDED",
            decided_at_us=BASE_US + 80,
            audit_ref="audit-completion",
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.completion is not None
    assert view.completion.outcome == "SUCCEEDED"
    assert view.completion_evidence[0].evidence_digest == EVIDENCE_DIGEST


# --- the eight run states, over the real event stream -------------------------------


def test_a_bound_run_with_no_open_step_is_created(owned: m1.Owned) -> None:
    started(owned)
    assert state_of(owned) == WORKFLOW_RUN_STATE_CREATED


def test_opening_the_run_steps_moves_created_to_queued(owned: m1.Owned) -> None:
    started(owned)
    open_step(owned)
    assert state_of(owned) == WORKFLOW_RUN_STATE_QUEUED


@pytest.mark.parametrize(
    ("statuses", "expected"),
    (
        (("running",), WORKFLOW_RUN_STATE_RUNNING),
        (("running", "waiting"), WORKFLOW_RUN_STATE_WAITING),
        (("running", "succeeded"), WORKFLOW_RUN_STATE_COMPLETED),
        (("running", "failed"), WORKFLOW_RUN_STATE_FAILED),
        (("running", "partially_completed"), WORKFLOW_RUN_STATE_FAILED),
        (("cancelled",), WORKFLOW_RUN_STATE_CANCELLED),
        (("running", "uncertain"), WORKFLOW_RUN_STATE_INDETERMINATE),
    ),
    ids=(
        "running",
        "waiting",
        "completed",
        "failed",
        "partially-completed-is-not-a-success",
        "cancelled",
        "indeterminate",
    ),
)
def test_every_run_state_is_projected_from_the_durable_stream(
    owned: m1.Owned, statuses: tuple[str, ...], expected: str
) -> None:
    started(owned)
    open_step(owned)
    for index, status in enumerate(statuses, start=1):
        event(owned, index, f"run_{status}", status)
    assert state_of(owned) == expected


def test_the_projection_reports_the_durable_status_beside_the_state(
    owned: m1.Owned,
) -> None:
    started(owned)
    open_step(owned)
    event(owned, 1, "run_running", "running")
    event(owned, 2, "run_partially_completed", "partially_completed")

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    # The state fails closed to `failed`; the raw status is still reported, so a
    # caller that needs the distinction is not left guessing which failure it was.
    assert view.state == WORKFLOW_RUN_STATE_FAILED
    assert view.run_status == "partially_completed"


def test_a_terminal_run_admits_no_further_event(owned: m1.Owned) -> None:
    started(owned)
    event(owned, 1, "run_running", "running")
    event(owned, 2, "run_succeeded", "succeeded")

    # 0018's own trigger is the enforcement; the projection never sees a downgrade
    # because the write that would cause one never lands.
    with pytest.raises(sqlite3.IntegrityError):
        event(owned, 3, "run_running", "running")
    assert state_of(owned) == WORKFLOW_RUN_STATE_COMPLETED


def test_an_illegal_durable_transition_is_refused_before_the_projection_sees_it(
    owned: m1.Owned,
) -> None:
    started(owned)
    with pytest.raises(sqlite3.IntegrityError):
        event(owned, 1, "run_succeeded", "succeeded")
    assert state_of(owned) == WORKFLOW_RUN_STATE_CREATED


# --- the transition table ------------------------------------------------------------


def test_the_state_vocabulary_is_exactly_the_eight_required_states() -> None:
    assert WORKFLOW_RUN_STATES == (
        "created",
        "queued",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
        "indeterminate",
    )
    assert set(WORKFLOW_RUN_STATE_TRANSITIONS) == set(WORKFLOW_RUN_STATES)
    assert WORKFLOW_RUN_TERMINAL_STATES == {"completed", "failed", "cancelled"}
    # `indeterminate` is an open question, not an ending.
    assert WORKFLOW_RUN_STATE_INDETERMINATE not in WORKFLOW_RUN_TERMINAL_STATES


@pytest.mark.parametrize("state", WORKFLOW_RUN_STATES)
def test_re_observing_a_run_that_has_not_moved_is_not_a_transition(state: str) -> None:
    validate_workflow_run_state_transition(state, state)


@pytest.mark.parametrize(
    ("previous", "current"),
    (
        (WORKFLOW_RUN_STATE_CREATED, WORKFLOW_RUN_STATE_QUEUED),
        (WORKFLOW_RUN_STATE_QUEUED, WORKFLOW_RUN_STATE_RUNNING),
        (WORKFLOW_RUN_STATE_RUNNING, WORKFLOW_RUN_STATE_WAITING),
        (WORKFLOW_RUN_STATE_WAITING, WORKFLOW_RUN_STATE_RUNNING),
        (WORKFLOW_RUN_STATE_RUNNING, WORKFLOW_RUN_STATE_INDETERMINATE),
        (WORKFLOW_RUN_STATE_INDETERMINATE, WORKFLOW_RUN_STATE_COMPLETED),
        (WORKFLOW_RUN_STATE_RUNNING, WORKFLOW_RUN_STATE_CANCELLED),
    ),
)
def test_a_legal_transition_is_permitted(previous: str, current: str) -> None:
    validate_workflow_run_state_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    (
        (WORKFLOW_RUN_STATE_CREATED, WORKFLOW_RUN_STATE_RUNNING),
        (WORKFLOW_RUN_STATE_CREATED, WORKFLOW_RUN_STATE_COMPLETED),
        (WORKFLOW_RUN_STATE_QUEUED, WORKFLOW_RUN_STATE_WAITING),
        (WORKFLOW_RUN_STATE_RUNNING, WORKFLOW_RUN_STATE_QUEUED),
        (WORKFLOW_RUN_STATE_WAITING, WORKFLOW_RUN_STATE_COMPLETED),
    ),
)
def test_an_illegal_transition_fails_closed_and_says_which_kind(
    previous: str, current: str
) -> None:
    with pytest.raises(WorkflowRunStateRefused) as raised:
        validate_workflow_run_state_transition(previous, current)
    assert raised.value.diagnostic == DIAGNOSTIC_ILLEGAL_TRANSITION


@pytest.mark.parametrize("previous", sorted(WORKFLOW_RUN_TERMINAL_STATES))
@pytest.mark.parametrize(
    "current",
    (
        WORKFLOW_RUN_STATE_CREATED,
        WORKFLOW_RUN_STATE_QUEUED,
        WORKFLOW_RUN_STATE_RUNNING,
        WORKFLOW_RUN_STATE_WAITING,
        WORKFLOW_RUN_STATE_INDETERMINATE,
    ),
)
def test_a_terminal_state_is_never_downgraded(previous: str, current: str) -> None:
    with pytest.raises(WorkflowRunStateRefused) as raised:
        validate_workflow_run_state_transition(previous, current)
    assert raised.value.diagnostic == DIAGNOSTIC_TERMINAL_DOWNGRADE


@pytest.mark.parametrize(
    ("previous", "current"),
    (
        ("admitted", WORKFLOW_RUN_STATE_RUNNING),
        (WORKFLOW_RUN_STATE_RUNNING, "succeeded"),
    ),
)
def test_a_state_this_build_does_not_know_fails_closed(
    previous: str, current: str
) -> None:
    """The durable spellings are not Workflow Run states and are not silently accepted.

    `semantics_runtime.validate_run_status_transition` returns silently on an unknown
    status because it judges wire values an older build may never have seen. Both ends
    here are derived from stored rows, so an unrecognised one means this module and the
    database disagree -- and continuing would write under a state machine it cannot
    evaluate.
    """
    with pytest.raises(WorkflowRunStateRefused) as raised:
        validate_workflow_run_state_transition(previous, current)
    assert raised.value.diagnostic == DIAGNOSTIC_UNKNOWN_STATE


def test_the_transition_table_never_contradicts_the_durable_one() -> None:
    """Every projected edge is one migration 0018's event guard already permits.

    The one edge with no durable counterpart is `created -> queued`: both of its
    endpoints are the same `admitted` status, so 0018 has nothing to say about it.
    """
    from omnivia_core.contracts.v1.semantics_runtime import RUN_STATUS_TRANSITIONS

    projected = {
        "admitted": WORKFLOW_RUN_STATE_CREATED,
        "running": WORKFLOW_RUN_STATE_RUNNING,
        "waiting": WORKFLOW_RUN_STATE_WAITING,
        "succeeded": WORKFLOW_RUN_STATE_COMPLETED,
        "partially_completed": WORKFLOW_RUN_STATE_FAILED,
        "failed": WORKFLOW_RUN_STATE_FAILED,
        "cancelled": WORKFLOW_RUN_STATE_CANCELLED,
        "uncertain": WORKFLOW_RUN_STATE_INDETERMINATE,
    }
    durable_edges = {
        (projected[before], projected[after])
        for before, targets in RUN_STATUS_TRANSITIONS.items()
        for after in targets
    }
    # `queued` stands in for `created` on the durable side: both are `admitted`.
    allowed = durable_edges | {
        (WORKFLOW_RUN_STATE_CREATED, WORKFLOW_RUN_STATE_QUEUED),
        (WORKFLOW_RUN_STATE_QUEUED, WORKFLOW_RUN_STATE_RUNNING),
        (WORKFLOW_RUN_STATE_QUEUED, WORKFLOW_RUN_STATE_CANCELLED),
    }
    for before, targets in WORKFLOW_RUN_STATE_TRANSITIONS.items():
        for after in targets:
            if before == after:
                continue
            assert (before, after) in allowed, (
                f"{before} -> {after} has no durable edge"
            )


def test_a_status_this_build_does_not_project_is_refused() -> None:
    with pytest.raises(WorkflowRunStateRefused) as raised:
        workflow_run_state(run_status="stale", steps_opened=False)
    assert raised.value.diagnostic == DIAGNOSTIC_UNKNOWN_STATE


# --- isolation and restart -----------------------------------------------------------


def test_a_run_of_another_workspace_is_invisible_rather_than_merely_unasked_for(
    owned: m1.Owned,
) -> None:
    started(owned)

    assert (
        read_workflow_run(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )
    assert (
        read_workflow_plan(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            workflow_id=WORKFLOW_ID,
            workflow_version=WORKFLOW_VERSION,
        )
        is None
    )
    assert (
        read_workspace_workflow_run_ids(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID
        )
        == ()
    )
    assert (
        read_workflow_run(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-nobody-admitted"
        )
        is None
    )


def test_a_restarted_service_reseals_readmits_and_reobserves_without_changing_anything(
    tmp_path: Path,
) -> None:
    """Recovery is deterministic: the same calls under a new generation are a replay."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    sealed = started(first)
    with workflow_writer(
        first.connection,
        first.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=first.generation,
    ) as writer:
        writer.observe_plan_step(
            run_id=RUN_ID,
            step_id="a-plan",
            route=ROUTE_DETERMINISTIC,
            sequence_index=0,
            observed_at_us=BASE_US + 40,
        )
    before = read_workflow_run(
        first.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    first.connection.close()

    second = m1.take_ownership(path)
    assert second.generation != first.generation
    try:
        assert seal(second) == sealed
        admit(second, admission(sealed))
        with workflow_writer(
            second.connection,
            second.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=second.generation,
        ) as writer:
            writer.observe_plan_step(
                run_id=RUN_ID,
                step_id="a-plan",
                route=ROUTE_DETERMINISTIC,
                sequence_index=0,
                observed_at_us=BASE_US + 500,
            )
        after = read_workflow_run(
            second.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert after == before
        assert count(second, m27.OBSERVATIONS) == 1
        assert count(second, m27.RUNS) == 1
        assert count(second, m27.PLANS) == 1
    finally:
        second.connection.close()


# --- a reused run identity that names different material -----------------------------


@pytest.mark.parametrize(
    "overrides",
    (
        {"releaseRef": {"releaseId": "release-substituted"}},
        {"executionProfileDigest": "sha256:" + "a" * 64},
        {"effectivePolicyDigest": "sha256:" + "b" * 64},
        {"componentImplementationDigests": {"component-echo": "sha256:" + "c" * 64}},
        {"modelPolicySnapshotDigest": "sha256:" + "d" * 64},
    ),
    ids=("release", "profile", "policy", "component", "model-policy"),
)
def test_readmitting_a_run_with_different_bound_material_is_a_conflict(
    owned: m1.Owned, overrides: dict[str, Any]
) -> None:
    """`omnivia_workflow_runs` holds three columns; a binding pins far more than three.

    Two admissions can agree on the workflow, the version and the plan digest and still
    name a different release, execution profile, effective policy, Component
    implementation or model policy. Answering the second with the *stored* binding
    would report material that was never bound as the material this run executes
    against, which is the fabricated execution history the binding exists to prevent.
    """
    sealed = started(owned)
    substituted = ip06.binding(
        workflowId=sealed.workflow_id,
        workflowVersion=sealed.workflow_version,
        definitionDigest=sealed.definition_hash,
        **overrides,
    )

    with pytest.raises(StorageError, match="different material"):
        admit(owned, admission(sealed, binding=substituted))

    assert count(owned, m27.RUNS) == 1
    assert count(owned, ip06.BINDINGS) == 1
    stored = ip06.read_runtime_definition_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert stored is not None
    for key, value in overrides.items():
        assert stored.binding[key] != value


@pytest.mark.parametrize(
    "overrides",
    (
        {"bindingId": "binding-retried"},
        {"boundAt": ip06.instant(BASE_US + 900)},
        {"boundBy": {"principalId": "core-service-restarted"}},
    ),
    ids=("binding-id", "instant", "actor"),
)
def test_readmitting_a_run_with_the_same_material_is_still_a_replay(
    owned: m1.Owned, overrides: dict[str, Any]
) -> None:
    """A crash-retry mints its own identifier and reads its own clock.

    `bindingId`, `bindingSchemaVersion`, `boundAt` and `boundBy` record when and by
    whom a binding was written, not what the Run executes against -- exactly the fields
    `bound_material` excludes and a resume does not drift on. Restating the same
    material under a new one of them is the replay it was.
    """
    sealed = started(owned)
    retried = ip06.binding(
        workflowId=sealed.workflow_id,
        workflowVersion=sealed.workflow_version,
        definitionDigest=sealed.definition_hash,
        **overrides,
    )

    stored = admit(owned, admission(sealed, binding=retried))

    # The stored binding wins, unchanged: a replay returns what is durable rather
    # than the document the retry arrived with.
    assert stored.binding["bindingId"] == ip06.BINDING_ID
    assert count(owned, m27.RUNS) == 1
    assert count(owned, ip06.BINDINGS) == 1


def test_readmitting_a_run_with_a_malformed_binding_refuses_as_malformed(
    owned: m1.Owned,
) -> None:
    """A binding missing every pinned field must not decide its own comparison."""
    sealed = started(owned)

    with pytest.raises(StorageError, match="not a valid RuntimeDefinitionBinding"):
        admit(owned, admission(sealed, binding={"bindingId": "binding-empty"}))

    assert count(owned, m27.RUNS) == 1


# --- the read side believes nothing it has not recomputed ----------------------------


def test_a_tampered_binding_is_not_returned_as_the_run_view(owned: m1.Owned) -> None:
    """Bytes edited outside this database's guards are not truth about a run.

    The projection is T-0688's verifying reader, so the digest, the length, the
    canonical spelling, the public contract and 0035's own admission join are all
    recomputed before anything is stated. A read that merely parsed the row would
    report the substituted release as the release this run is pinned to.
    """
    started(owned)
    forged = canonicalize(ip06.binding(releaseRef={"releaseId": "release-substituted"}))
    # `ip06.corrupt` reopens the file with the append-only guard dropped, which is the
    # only way this edit lands at all -- and exactly the offline edit the recomputation
    # exists to catch.
    connection = ip06.corrupt(
        owned, f"UPDATE {ip06.BINDINGS} SET binding_json = ?", forged
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_workflow_run(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_the_run_view_carries_the_verified_binding_projection(
    owned: m1.Owned,
) -> None:
    """A bound run reads as bound, and names the binding a verified read produced."""
    started(owned)

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert view is not None
    assert view.binding_projection["runId"] == RUN_ID
    assert view.binding_projection["legacyBinding"] is False
    assert view.binding_projection["bindingRef"]["bindingId"] == ip06.BINDING_ID
