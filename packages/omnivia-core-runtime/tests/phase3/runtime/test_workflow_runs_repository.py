"""M2 acceptance for the durable Workflow Runtime repository and admission seam.

`storage/workflow_runs.py` is persistence and the bounded start seam above it, and
nothing else. These tests hold it to four properties migration 0027 alone cannot state.

*It enters its own fenced transaction.* Every write is called on a bare owned
connection here -- no surrounding transaction, no mutation seam -- and either commits
under current authority or leaves the database exactly as it found it.

*A plan is a re-addressing of a sealed definition, not a second decision.* Every
column, each step's route included, is derived from a sealed `MaterialisedWorkflow`
through the same seam the in-memory oracle routes with, so a stored plan and an
observation of it cannot disagree.

*Admission is pinned and idempotent.* A run binds the exact workflow, version and plan
hash it names or nothing at all; repeating an equivalent admission returns the stored
binding and writes nothing; repeating it under different pins raises.

*The projection reports stored facts and invents none.* This milestone stores no
attempt, no wait, no scheduler readiness and no executor result, so the run view has no
field for one. The tests say so explicitly rather than leaving the absence to be
noticed.

Out of scope here, and deliberately untested because unimplemented: mutating scheduler
readiness, dispatch, execution, durable waits, effects and compensation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_workflow_runs_migration as m27
from omnivia_core_runtime.execution.profile import (
    EXECUTION_CLASS_AGENT,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_EFFECT,
    EXECUTION_CLASS_WAIT,
)
from omnivia_core_runtime.execution.workflow import (
    BRANCH_OPERATOR_EQUALS,
    OUTCOME_SUCCEEDED,
    ROUTE_AGENT,
    ROUTE_CHILD_WORKFLOW,
    ROUTE_DETERMINISTIC,
    ROUTE_EFFECT,
    ROUTE_WAIT,
    BranchDefinition,
    ChildWorkflowDefinition,
    LoopDefinition,
    MaterialisedWorkflow,
    StepDefinition,
    WorkflowDefinition,
    materialise_workflow,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runs import (
    CHILD_RESULT_ACCEPTED,
    CHILD_RESULT_CLOSED,
    CONNECTION_INCOMING,
    CONNECTION_OUTGOING,
    CONNECTION_REQUIRED,
    CONNECTION_SELECTED,
    READINESS_CAPABILITY_GRANTED,
    READINESS_CAPABILITY_REQUIRED,
    READINESS_MAPPED_INPUT_READY,
    READINESS_MAPPED_INPUT_REQUIRED,
    WorkflowPlan,
    WorkflowRunAdmission,
    WorkflowRunBinding,
    WorkflowWriter,
    admit_workflow_run,
    read_workflow_plan,
    read_workflow_run,
    read_workflow_run_binding,
    read_workflow_run_step_connections,
    read_workflow_run_step_readiness_facts,
    read_workspace_workflow_run_ids,
    seal_workflow_plan,
    workflow_writer,
)

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m1.OTHER_WORKSPACE_ID
RUN_ID = m27.RUN_ID
BASE_US = m27.BASE_US

WORKFLOW_ID = "workflow.review"
WORKFLOW_VERSION = "1.0.0"
CHILD_WORKFLOW_ID = "workflow.summarise"
CHILD_HASH = "sha256:" + "7" * 64
EVIDENCE_DIGEST = "sha256:" + "f" * 64

PLAN_AUDIT = "audit-plan"
COMPLETION_AUDIT = "audit-completion"

#: The mixed golden workflow: one step of every route, a branch on the step branch
#: observations name, a loop on one more, and a declaration order that is not the
#: topological one, so the stored plan proves it ordered rather than transcribed.
GOLDEN_ORDER = ("a.plan", "b.compute", "c.write", "d.wait", "e.child")
GOLDEN_ROUTES = (
    ROUTE_AGENT,
    ROUTE_DETERMINISTIC,
    ROUTE_EFFECT,
    ROUTE_WAIT,
    ROUTE_CHILD_WORKFLOW,
)

BRANCH = BranchDefinition("mode", BRANCH_OPERATOR_EQUALS, "fast")
LOOP = LoopDefinition(3, 5, 15)


def child() -> ChildWorkflowDefinition:
    return ChildWorkflowDefinition(
        workflow_id=CHILD_WORKFLOW_ID,
        version="1.0.0",
        workflow_hash=CHILD_HASH,
        budget=10,
    )


def step(
    step_id: str,
    execution_class: str = EXECUTION_CLASS_DETERMINISTIC,
    **overrides: object,
) -> StepDefinition:
    fields: dict[str, object] = {
        "step_id": step_id,
        "component_id": "component.echo",
        "component_version": "1.0.0",
        "execution_class": execution_class,
    }
    fields.update(overrides)
    return StepDefinition(**fields).sealed()  # type: ignore[arg-type]


GOLDEN_STEPS = (
    step("d.wait", EXECUTION_CLASS_WAIT, depends_on=("c.write",), loop=LOOP),
    step("b.compute", depends_on=("a.plan",), branch=BRANCH),
    step(
        "e.child",
        EXECUTION_CLASS_WAIT,
        depends_on=("b.compute",),
        child_workflow=child(),
    ),
    step("a.plan", EXECUTION_CLASS_AGENT),
    step("c.write", EXECUTION_CLASS_EFFECT, depends_on=("b.compute",)),
)


def plan(*steps: StepDefinition, version: str = WORKFLOW_VERSION) -> MaterialisedWorkflow:
    return materialise_workflow(
        WorkflowDefinition(
            workflow_id=WORKFLOW_ID, version=version, steps=steps or GOLDEN_STEPS
        ).sealed()
    )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def audit(holder: m1.Owned, audit_ref: str) -> None:
    with m27.guarded(holder):
        m27.audit(holder, audit_ref)


def seal(
    holder: m1.Owned, materialised: MaterialisedWorkflow | None = None
) -> WorkflowPlan:
    return seal_workflow_plan(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        plan=materialised or plan(),
        sealed_at_us=BASE_US,
        audit_ref=PLAN_AUDIT,
    )


def admission(**overrides: object) -> WorkflowRunAdmission:
    fields: dict[str, object] = {
        "run_id": RUN_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "plan_hash": plan().content_hash,
        "bound_at_us": BASE_US + 20,
    }
    fields.update(overrides)
    return WorkflowRunAdmission(**fields)  # type: ignore[arg-type]


def admit(
    holder: m1.Owned, record: WorkflowRunAdmission | None = None
) -> WorkflowRunBinding:
    return admit_workflow_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record or admission(),
    )


def started(holder: m1.Owned, **run: object) -> None:
    """A sealed plan, an audited runtime run admitted as a workflow, and the binding."""
    audit(holder, PLAN_AUDIT)
    seal(holder)
    m27.seed_runtime_run(
        holder,
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
        **run,  # type: ignore[arg-type]
    )
    admit(holder)


def writer(holder: m1.Owned) -> AbstractContextManager[WorkflowWriter]:
    return workflow_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


# --- the plan is a re-addressing of a sealed definition ----------------------------


def test_a_sealed_plan_stores_the_order_and_routes_materialisation_derived(
    owned: m1.Owned,
) -> None:
    audit(owned, PLAN_AUDIT)

    sealed = seal(owned)

    stored = read_workflow_plan(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
    )
    assert stored == sealed
    assert stored is not None
    assert stored.plan_hash == plan().content_hash
    assert stored.definition_hash == plan().definition_hash
    assert tuple(entry.step_id for entry in stored.steps) == GOLDEN_ORDER
    assert tuple(entry.route for entry in stored.steps) == GOLDEN_ROUTES
    assert tuple(entry.sequence_index for entry in stored.steps) == (0, 1, 2, 3, 4)
    by_id = {entry.step_id: entry for entry in stored.steps}
    assert by_id["b.compute"].depends_on == ("a.plan",)
    assert by_id["b.compute"].branch == BRANCH.preimage
    assert by_id["d.wait"].loop == LOOP.preimage
    assert by_id["e.child"].child_workflow == child().preimage
    # The route a child step takes is CHILD_WORKFLOW while its execution class stays
    # WAIT: 0027 stores both, and neither is derivable from the other.
    assert by_id["e.child"].execution_class == EXECUTION_CLASS_WAIT
    assert by_id["a.plan"].branch is None


def test_a_step_carries_both_its_declared_and_its_materialised_address(
    owned: m1.Owned,
) -> None:
    audit(owned, PLAN_AUDIT)
    seal(owned)

    stored = read_workflow_plan(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
    )

    assert stored is not None
    materialised = {entry.step_id: entry for entry in plan().steps}
    for entry in stored.steps:
        source = materialised[entry.step_id]
        assert entry.step_definition_hash == source.definition_hash
        assert entry.materialised_step_hash == source.content_hash
        assert entry.step_definition_hash != entry.materialised_step_hash


def test_resealing_the_same_plan_returns_the_stored_one_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    audit(owned, PLAN_AUDIT)

    first = seal(owned)
    second = seal(owned)

    assert second == first
    assert owned.connection.execute(
        f"SELECT COUNT(*) FROM {m27.STEPS}"
    ).fetchone() == (len(GOLDEN_ORDER),)


def test_resealing_a_different_plan_under_the_same_version_is_a_conflict(
    owned: m1.Owned,
) -> None:
    audit(owned, PLAN_AUDIT)
    seal(owned)

    with pytest.raises(StorageError, match="already sealed"):
        seal(owned, plan(step("a.plan", EXECUTION_CLASS_AGENT)))

    # The conflict left the sealed plan exactly as it was.
    stored = read_workflow_plan(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
    )
    assert stored is not None
    assert tuple(entry.step_id for entry in stored.steps) == GOLDEN_ORDER


def test_sealing_a_plan_requires_the_current_fenced_owner(owned: m1.Owned) -> None:
    audit(owned, PLAN_AUDIT)

    with pytest.raises(StaleGeneration):
        seal_workflow_plan(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            plan=plan(),
            sealed_at_us=BASE_US,
            audit_ref=PLAN_AUDIT,
        )

    assert (
        read_workflow_plan(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            workflow_id=WORKFLOW_ID,
            workflow_version=WORKFLOW_VERSION,
        )
        is None
    )


# --- admission: pinned, idempotent, and refusing a conflict ------------------------


def test_admission_binds_the_runtime_run_to_the_plan_it_pins(owned: m1.Owned) -> None:
    started(owned)

    binding = read_workflow_run_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    assert binding is not None
    assert (binding.workflow_id, binding.workflow_version) == (
        WORKFLOW_ID,
        WORKFLOW_VERSION,
    )
    assert binding.plan_hash == plan().content_hash
    assert read_workspace_workflow_run_ids(
        owned.connection, workspace_id=WORKSPACE_ID
    ) == (RUN_ID,)


def test_admission_requires_the_current_fenced_owner(owned: m1.Owned) -> None:
    audit(owned, PLAN_AUDIT)
    seal(owned)
    m27.seed_runtime_run(
        owned, workflow_id=WORKFLOW_ID, workflow_version=WORKFLOW_VERSION
    )

    with pytest.raises(StaleGeneration):
        admit_workflow_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            admission=admission(),
        )

    assert (
        read_workflow_run_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )


def test_repeating_an_equivalent_admission_is_the_same_binding(
    owned: m1.Owned,
) -> None:
    started(owned)
    first = read_workflow_run_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    # A retry after a crash reads its own clock; the instant the binding already has
    # is the one it keeps, and nothing new is written.
    replayed = admit(owned, admission(bound_at_us=BASE_US + 999))

    assert replayed == first
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {m27.RUNS}").fetchone() == (
        1,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"plan_hash": "sha256:" + "9" * 64}, "sealed as"),
        ({"workflow_version": "2.0.0"}, "no sealed plan"),
        ({"workflow_id": "workflow.other"}, "no sealed plan"),
    ],
)
def test_an_admission_that_pins_a_plan_this_workspace_never_sealed_is_refused(
    owned: m1.Owned, overrides: dict[str, object], message: str
) -> None:
    audit(owned, PLAN_AUDIT)
    seal(owned)
    m27.seed_runtime_run(
        owned, workflow_id=WORKFLOW_ID, workflow_version=WORKFLOW_VERSION
    )

    with pytest.raises(StorageError, match=message):
        admit(owned, admission(**overrides))

    assert (
        read_workflow_run_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )


def test_rebinding_one_run_to_a_different_plan_is_a_conflict(owned: m1.Owned) -> None:
    started(owned)
    audit(owned, "audit-other-plan")
    other = materialise_workflow(
        WorkflowDefinition(
            workflow_id="workflow.other",
            version=WORKFLOW_VERSION,
            steps=(step("a.plan", EXECUTION_CLASS_AGENT),),
        ).sealed()
    )
    seal_workflow_plan(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        plan=other,
        sealed_at_us=BASE_US,
        audit_ref="audit-other-plan",
    )

    with pytest.raises(StorageError, match="already bound"):
        admit(
            owned,
            admission(workflow_id="workflow.other", plan_hash=other.content_hash),
        )

    binding = read_workflow_run_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert binding is not None
    assert binding.workflow_id == WORKFLOW_ID


def test_admission_refuses_a_runtime_run_that_was_not_admitted_as_a_workflow(
    owned: m1.Owned,
) -> None:
    """0027's guard, surfaced through the seam rather than restated inside it."""
    audit(owned, PLAN_AUDIT)
    seal(owned)
    m27.seed_runtime_run(
        owned,
        definition_kind="agent_component",
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
    )

    with pytest.raises(sqlite3.IntegrityError, match="workflow"):
        admit(owned)

    assert (
        read_workflow_run_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )


# --- the run projection: stored facts, and no invented ones ------------------------


def observe_steps(holder: m1.Owned) -> None:
    """The replay-safe half: every plan route, and the one branch that was evaluated."""
    materialised = {entry.step_id: entry for entry in plan().steps}
    with writer(holder) as write:
        for index, step_id in enumerate(GOLDEN_ORDER):
            write.observe_plan_step(
                run_id=RUN_ID,
                step=materialised[step_id],
                observed_at_us=BASE_US + 30 + index,
            )
        write.observe_branch(
            run_id=RUN_ID,
            step_id="b.compute",
            result=BRANCH.evaluate((("mode", "fast"),)),
            observed_at_us=BASE_US + 40,
        )


def correlate_and_evidence(holder: m1.Owned) -> None:
    """The half that is not replay-safe: one fenced correlation and its evidence.

    Opening a correlation mints a new fence and recording evidence claims a kind for
    the first time, so neither is repeated on restart. What survives a restart is what
    was already committed, which is exactly what the projection reads.
    """
    with writer(holder) as write:
        fence = write.open_child_correlation(
            correlation_id="corr-0001",
            parent_run_id=RUN_ID,
            parent_step_id="e.child",
            child=child(),
            opened_at_us=BASE_US + 50,
        )
        write.record_child_result(
            correlation_id="corr-0001",
            outcome=CHILD_RESULT_ACCEPTED,
            fence=fence,
            child_workflow_id=CHILD_WORKFLOW_ID,
            child_version="1.0.0",
            child_workflow_hash=CHILD_HASH,
            cost=4,
            recorded_at_us=BASE_US + 51,
        )
        write.record_child_result(
            correlation_id="corr-0001",
            outcome=CHILD_RESULT_CLOSED,
            fence=fence,
            child_workflow_id=CHILD_WORKFLOW_ID,
            child_version="1.0.0",
            child_workflow_hash=CHILD_HASH,
            recorded_at_us=BASE_US + 52,
        )
        write.record_completion_evidence(
            run_id=RUN_ID,
            evidence_kind="run.summary",
            evidence_digest=EVIDENCE_DIGEST,
            recorded_at_us=BASE_US + 60,
        )


def test_the_run_projection_reports_every_durable_fact_and_only_those(
    owned: m1.Owned,
) -> None:
    started(owned)
    observe_steps(owned)
    correlate_and_evidence(owned)
    audit(owned, COMPLETION_AUDIT)
    with writer(owned) as write:
        write.complete_run(
            run_id=RUN_ID,
            outcome=OUTCOME_SUCCEEDED,
            decided_at_us=BASE_US + 70,
            audit_ref=COMPLETION_AUDIT,
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert view is not None
    assert view.plan.plan_hash == plan().content_hash
    assert tuple(entry.step_id for entry in view.plan.steps) == GOLDEN_ORDER
    assert tuple(
        observation.step_id for observation in view.plan_observations
    ) == GOLDEN_ORDER
    assert tuple(
        observation.route for observation in view.plan_observations
    ) == GOLDEN_ROUTES
    assert [
        (observation.step_id, observation.branch_outcome, observation.branch_reason)
        for observation in view.branch_observations
    ] == [("b.compute", "MATCHED", "equals")]
    assert len(view.correlations) == 1
    correlation = view.correlations[0]
    assert (correlation.fence, correlation.budget, correlation.consumed) == (1, 10, 4)
    assert correlation.is_closed
    assert [result.outcome for result in correlation.results] == [
        CHILD_RESULT_ACCEPTED,
        CHILD_RESULT_CLOSED,
    ]
    assert [
        (evidence.evidence_kind, evidence.evidence_digest)
        for evidence in view.completion_evidence
    ] == [("run.summary", EVIDENCE_DIGEST)]
    assert view.completion is not None
    assert view.completion.outcome == OUTCOME_SUCCEEDED
    assert view.completion.audit_reference == COMPLETION_AUDIT


def test_the_run_projection_invents_no_attempt_wait_scheduler_or_executor_state(
    owned: m1.Owned,
) -> None:
    """M2 stores none of these, so the view has no field that could report one."""
    started(owned)

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert view is not None
    for absent in (
        "attempts",
        "waits",
        "status",
        "ready_steps",
        "next_step",
        "results",
        "effects",
    ):
        assert not hasattr(view, absent), absent
    # A run in flight reports empty collections and no decision, which is an answer
    # rather than a gap.
    assert view.plan_observations == ()
    assert view.branch_observations == ()
    assert view.correlations == ()
    assert view.completion_evidence == ()
    assert view.completion is None


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
        read_workflow_run(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        is None
    )


# --- restart: the sealed definition and the stored rows are all that survive -------


def test_a_restarted_service_reseals_readmits_and_reobserves_without_changing_anything(
    owned: m1.Owned,
) -> None:
    """Restart is a replay of the same three writes, not a second run of the workflow."""
    started(owned)
    observe_steps(owned)
    correlate_and_evidence(owned)
    before = read_workflow_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    # The process is gone. The sealed definition is rebuilt from its steps, the lease
    # is taken again under a new fencing generation, and every write is repeated.
    owned.connection.close()
    restarted = m1.take_ownership(owned.path)
    try:
        assert restarted.generation != owned.generation
        seal(restarted)
        admit(restarted)
        observe_steps(restarted)

        after = read_workflow_run(
            restarted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert after == before
        assert restarted.connection.execute(
            f"SELECT COUNT(*) FROM {m27.OBSERVATIONS}"
        ).fetchone() == (len(GOLDEN_ORDER) + 1,)
    finally:
        restarted.connection.close()


def test_an_observation_that_contradicts_the_one_recorded_is_refused(
    owned: m1.Owned,
) -> None:
    """`INSERT OR IGNORE` makes a replay silent; it does not make a conflict silent."""
    started(owned)
    observe_steps(owned)

    with (
        pytest.raises(sqlite3.IntegrityError, match="conflicts"),
        writer(owned) as write,
    ):
        write.observe_branch(
            run_id=RUN_ID,
            step_id="b.compute",
            result=BRANCH.evaluate((("mode", "slow"),)),
            observed_at_us=BASE_US + 41,
        )

    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.branch_observations[0].branch_outcome == "MATCHED"


def test_step_connection_facts_are_replay_safe_and_visible_in_the_run_view(
    owned: m1.Owned,
) -> None:
    started(owned)
    with writer(owned) as write:
        write.record_step_connection(
            run_id=RUN_ID,
            step_id="b.compute",
            direction=CONNECTION_INCOMING,
            connection_id="conn-fast",
            fact_kind=CONNECTION_REQUIRED,
            recorded_at_us=BASE_US + 42,
        )
        write.record_step_connection(
            run_id=RUN_ID,
            step_id="b.compute",
            direction=CONNECTION_INCOMING,
            connection_id="conn-fast",
            fact_kind=CONNECTION_REQUIRED,
            recorded_at_us=BASE_US + 43,
        )
        write.record_step_connection(
            run_id=RUN_ID,
            step_id="b.compute",
            direction=CONNECTION_OUTGOING,
            connection_id="conn-done",
            fact_kind=CONNECTION_SELECTED,
            recorded_at_us=BASE_US + 44,
        )

    facts = read_workflow_run_step_connections(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [
        (fact.step_id, fact.direction, fact.connection_id, fact.fact_kind)
        for fact in facts
    ] == [
        ("b.compute", CONNECTION_INCOMING, "conn-fast", CONNECTION_REQUIRED),
        ("b.compute", CONNECTION_OUTGOING, "conn-done", CONNECTION_SELECTED),
    ]
    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.step_connections == facts


def test_step_connection_facts_are_fenced_and_plan_step_bound(
    owned: m1.Owned,
) -> None:
    started(owned)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_workflow_run_step_connections "
            "(workspace_id, run_id, step_id, direction, connection_id, fact_kind, "
            "recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                RUN_ID,
                "b.compute",
                CONNECTION_INCOMING,
                "conn-fast",
                CONNECTION_REQUIRED,
                BASE_US + 42,
            ),
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        writer(owned) as write,
    ):
        write.record_step_connection(
            run_id=RUN_ID,
            step_id="b.compute",
            direction="sideways",
            connection_id="conn-fast",
            fact_kind=CONNECTION_REQUIRED,
            recorded_at_us=BASE_US + 42,
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="step of its own plan"),
        writer(owned) as write,
    ):
        write.record_step_connection(
            run_id=RUN_ID,
            step_id="missing.step",
            direction=CONNECTION_INCOMING,
            connection_id="conn-fast",
            fact_kind=CONNECTION_REQUIRED,
            recorded_at_us=BASE_US + 42,
        )

    assert (
        read_workflow_run_step_connections(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )


def test_step_readiness_facts_are_replay_safe_and_visible_in_the_run_view(
    owned: m1.Owned,
) -> None:
    started(owned)
    with writer(owned) as write:
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
            fact_id="input.customer",
            recorded_at_us=BASE_US + 42,
        )
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
            fact_id="input.customer",
            recorded_at_us=BASE_US + 43,
        )
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind=READINESS_MAPPED_INPUT_READY,
            fact_id="input.customer",
            recorded_at_us=BASE_US + 44,
        )
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind=READINESS_CAPABILITY_REQUIRED,
            fact_id="cap.mail.send",
            recorded_at_us=BASE_US + 45,
        )
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind=READINESS_CAPABILITY_GRANTED,
            fact_id="cap.mail.send",
            recorded_at_us=BASE_US + 46,
        )

    facts = read_workflow_run_step_readiness_facts(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [(fact.step_id, fact.fact_kind, fact.fact_id) for fact in facts] == [
        ("b.compute", READINESS_CAPABILITY_GRANTED, "cap.mail.send"),
        ("b.compute", READINESS_CAPABILITY_REQUIRED, "cap.mail.send"),
        ("b.compute", READINESS_MAPPED_INPUT_READY, "input.customer"),
        ("b.compute", READINESS_MAPPED_INPUT_REQUIRED, "input.customer"),
    ]
    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert view is not None
    assert view.step_readiness_facts == facts


def test_step_readiness_facts_are_fenced_and_plan_step_bound(
    owned: m1.Owned,
) -> None:
    started(owned)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_workflow_run_step_readiness_facts "
            "(workspace_id, run_id, step_id, fact_kind, fact_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                RUN_ID,
                "b.compute",
                READINESS_MAPPED_INPUT_REQUIRED,
                "input.customer",
                BASE_US + 42,
            ),
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        writer(owned) as write,
    ):
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="b.compute",
            fact_kind="mutable_ui_ready",
            fact_id="input.customer",
            recorded_at_us=BASE_US + 42,
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="step of its own plan"),
        writer(owned) as write,
    ):
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id="missing.step",
            fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
            fact_id="input.customer",
            recorded_at_us=BASE_US + 42,
        )

    assert (
        read_workflow_run_step_readiness_facts(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )


def test_a_completion_without_evidence_is_refused_by_the_gate_it_names(
    owned: m1.Owned,
) -> None:
    started(owned)
    audit(owned, COMPLETION_AUDIT)

    with (
        pytest.raises(sqlite3.IntegrityError, match="evidence"),
        writer(owned) as write,
    ):
        write.complete_run(
            run_id=RUN_ID,
            outcome=OUTCOME_SUCCEEDED,
            decided_at_us=BASE_US + 70,
            audit_ref=COMPLETION_AUDIT,
        )

    assert (
        read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        is not None
    )
