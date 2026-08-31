"""M3-A acceptance for workflow-owned scheduling over canonical runtime steps.

This is not the whole workflow scheduler. It proves that an admitted Workflow Run can
be expanded from its durable M2 plan into canonical pending Runtime steps exactly
once, then claimed and completed by dependency-aware workflow scheduling over those
stored rows.

It also proves the first durable loop slice: a loop step keeps one canonical runtime
attempt open while an append-only iteration ledger counts each iteration and closes the
attempt only when the bounded loop exits.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt107_runtime_waits as rt107
import test_rt202_policy_budget_snapshot_repository as r202
import test_rt203_approval_capability_grant_repository as r203
import test_v06_5_s0_mutation_foundation as s0
import test_workflow_runs_migration as m27
import test_workflow_runs_repository as repo
from omnivia_core_runtime.execution.profile import (
    BUILD_TRUST_APPROVED,
    CONTRACT_VERSION,
    EXECUTION_CLASS_EFFECT,
    EXECUTION_CLASS_WAIT,
    EXECUTOR_KIND_COMMAND,
    HEALTH_READY,
    ExecutionRefused,
    ExecutorDescriptor,
)
from omnivia_core_runtime.execution.registry import (
    ENTRY_EXECUTOR,
    RuntimeExecutionRegistry,
)
from omnivia_core_runtime.execution.workflow import (
    LoopDefinition,
    MaterialisedWorkflow,
    StepDefinition,
    WorkflowDispatchPlanner,
    WorkflowExecutorBinding,
    WorkResultEnvelope,
    WorkUnitEnvelope,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.effect_reconciliation import (
    reconcile_effect_transaction,
)
from omnivia_core_runtime.service.effect_transaction import (
    EffectTransactionError,
    publish_dispatch_request,
    settle_effect_transaction,
)
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
from omnivia_core_runtime.service.runtime_waits import WaitResolutionConflict
from omnivia_core_runtime.service.workflow_scheduler import (
    CAPACITY_LANE_RECOVERY,
    CAPACITY_WAIT_KIND,
    CAPACITY_WAIT_RESOLUTION_REASON,
    COMPENSATION_SETTLED_EVENT,
    COMPENSATION_STARTED_EVENT,
    GOVERNED_REPAIR_EVENT,
    GOVERNED_REPAIR_EVIDENCE_KIND,
    GOVERNED_REPAIR_EVIDENCE_SOURCE,
    RECONCILIATION_WAIT_KIND,
    RECONCILIATION_WAIT_PURPOSE,
    RECONCILIATION_WAIT_RESOLUTION_REASON,
    RETRY_WAIT_KIND,
    RETRY_WAIT_PURPOSE,
    RETRY_WAIT_RESOLUTION_REASON,
    WORKFLOW_WAIT_KIND,
    WORKFLOW_WAIT_RESOLUTION_REASON,
    WorkflowCapacityPolicy,
    WorkflowGovernanceDenied,
    WorkflowGovernancePolicy,
    WorkflowGovernedAuthority,
    WorkflowLoopIterationClaim,
    WorkflowRetryPolicy,
    WorkflowScheduler,
    WorkflowSchedulingError,
    WorkflowStepClaim,
    WorkflowWaitPurpose,
    open_workflow_runtime_steps,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    admit_run,
    append_run_step,
    read_effect_dispatch_count,
    read_effect_reconciliation_for_intent,
    read_effect_settlement_for_intent,
    read_run,
    read_run_effect_intents,
    read_run_sequence,
    read_run_steps,
    read_run_waits,
    record_effect_dispatch,
    record_effect_receipt,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runs import (
    CONNECTION_INCOMING,
    CONNECTION_OUTGOING,
    CONNECTION_REQUIRED,
    CONNECTION_SELECTED,
    READINESS_CAPABILITY_GRANTED,
    READINESS_CAPABILITY_REQUIRED,
    READINESS_MAPPED_INPUT_READY,
    READINESS_MAPPED_INPUT_REQUIRED,
    read_workflow_loop_iterations,
    read_workflow_run,
)

from omnivia_core.contracts.v1 import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_UNCERTAIN,
    WAIT_RESOLUTION_FOR_KIND,
    ApiError,
    Approval,
    EffectIntent,
    EffectReceipt,
    RunDefinitionRef,
)

WORKSPACE_ID = repo.WORKSPACE_ID
RUN_ID = repo.RUN_ID
BASE_US = repo.BASE_US
JOB_ID = m18.JOB_ID
EXECUTOR_BUILD_DIGEST = "sha256:" + "b" * 64
EXECUTOR_REMOVAL_DIGEST = "sha256:" + "c" * 64
WORK_PAYLOAD_DIGEST = "sha256:" + "d" * 64
WORK_RESULT_DIGEST = "sha256:" + "e" * 64
EFFECT_INTENT_ID = "eff-workflow-0001"
EFFECT_SETTLEMENT_ID = "stl-workflow-0001"
EFFECT_RECEIPT_ID = "rcp-workflow-0001"
EFFECT_REQUEST_DIGEST = "sha256:" + "6" * 64
EFFECT_RESPONSE_DIGEST = "sha256:" + "7" * 64
EFFECT_POLICY_US = BASE_US + 1_000
EFFECT_GRANT_US = BASE_US + 2_000
EFFECT_CLAIM_US = BASE_US + 3_000
EFFECT_DECLARED_US = BASE_US + 4_000
EFFECT_DISPATCH_US = BASE_US + 5_000
EFFECT_REDISPATCH_US = BASE_US + 5_500
EFFECT_RECEIPT_US = BASE_US + 6_000
EFFECT_SETTLED_US = BASE_US + 7_000
EFFECT_RECONCILIATION_ID = "rec-workflow-0001"
COMPENSATION_ID = "cmp-workflow-0001"
REPAIR_ID = "repair-workflow-0001"
REPAIR_EVIDENCE_SOURCE_ID = "review-log:workflow-repair-0001"
#: The receipt that arrives *after* the effect was already settled `unknown`, and the
#: instant the reconciliation it makes possible is recorded at. 0024 refuses a
#: reconciliation that predates the settlement it answers or the receipt it rests on, so
#: these two are ordered rather than interchangeable.
EFFECT_LATE_RECEIPT_US = BASE_US + 8_000
EFFECT_RECONCILED_US = BASE_US + 9_000
COMPENSATION_RECORDED_US = BASE_US + 10_000
REPAIR_RECORDED_US = BASE_US + 11_000


@pytest.fixture  # type: ignore[untyped-decorator]
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def started(
    holder: m1.Owned, *, materialised: MaterialisedWorkflow | None = None
) -> None:
    repo.audit(holder, repo.PLAN_AUDIT)
    sealed = repo.seal(holder, materialised)
    m18.seed_job(holder, job_id=JOB_ID)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=RunAdmission(
            run_id=RUN_ID,
            job_id=JOB_ID,
            claim_id=m18.claim_id_for(JOB_ID),
            definition=RunDefinitionRef(
                definition_kind="workflow",
                definition_id=repo.WORKFLOW_ID,
                definition_version=repo.WORKFLOW_VERSION,
            ),
            logical_key=m18.logical_key_for(JOB_ID),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(JOB_ID),
            admitted_at_us=BASE_US,
            runtime_event_id="evt-workflow-admitted",
            message="workflow run admitted",
        ),
    )
    repo.admit(holder, repo.admission(plan_hash=sealed.plan_hash))


def executor() -> ExecutorDescriptor:
    return ExecutorDescriptor(
        source_id="source.core",
        executor_id="executor.echo",
        version="1.0.0",
        build_hash=EXECUTOR_BUILD_DIGEST,
        executor_kind=EXECUTOR_KIND_COMMAND,
        capabilities=("component.run",),
        supported_contract_versions=(CONTRACT_VERSION,),
        required_isolation=2,
        trust_state=BUILD_TRUST_APPROVED,
        reconciliation_capabilities=(),
        removal_instructions_ref=EXECUTOR_REMOVAL_DIGEST,
    ).sealed()


def result_bound_work_unit(
    claim: WorkflowStepClaim, materialised: MaterialisedWorkflow
) -> WorkUnitEnvelope:
    descriptor = executor()
    registry = RuntimeExecutionRegistry()
    entry = registry.register_executor(descriptor)
    registry.set_health(ENTRY_EXECUTOR, entry.key, HEALTH_READY)
    step = next(item for item in materialised.steps if item.step_id == claim.workflow_step_id)
    return WorkflowDispatchPlanner(registry).plan(
        run_id=claim.run_id,
        workflow=materialised,
        step=step,
        binding=WorkflowExecutorBinding(
            step_id=claim.workflow_step_id,
            route=step.execution_class,
            source_id=descriptor.source_id,
            executor_id=descriptor.executor_id,
            executor_version=descriptor.version,
            capability="component.run",
            contract_version=CONTRACT_VERSION,
            minimum_isolation=2,
        ),
        payload_digest=WORK_PAYLOAD_DIGEST,
        fence=claim.fencing_generation,
    )


def result_for(work_unit: WorkUnitEnvelope, **overrides: object) -> WorkResultEnvelope:
    fields: dict[str, object] = {
        "work_unit_hash": work_unit.content_hash,
        "source_id": work_unit.source_id,
        "executor_id": work_unit.executor_id,
        "executor_version": work_unit.executor_version,
        "executor_content_hash": work_unit.executor_content_hash,
        "fence": work_unit.fence,
        "outcome": "SUCCEEDED",
        "result_digest": WORK_RESULT_DIGEST,
    }
    fields.update(overrides)
    return WorkResultEnvelope(**fields).sealed()


def effect_intent_for(claim: WorkflowStepClaim, **overrides: object) -> EffectIntent:
    fields: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_intent_id": EFFECT_INTENT_ID,
        "run_id": claim.run_id,
        "run_step_id": claim.run_step_id,
        "attempt_id": claim.runtime_attempt_id,
        "capability_id": "memory.read",
        "capability_grant_id": r203.GRANT_ID,
        "effect_kind": "memory.read",
        "idempotency_key": "workflow-effect-0001",
        "request_digest": EFFECT_REQUEST_DIGEST,
        "declared_at": rt107.timestamp(EFFECT_DECLARED_US),
    }
    fields.update(overrides)
    return EffectIntent(**fields)


def effect_receipt(**overrides: object) -> EffectReceipt:
    fields: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_receipt_id": EFFECT_RECEIPT_ID,
        "run_id": RUN_ID,
        "effect_intent_id": EFFECT_INTENT_ID,
        "observed_at": rt107.timestamp(EFFECT_RECEIPT_US),
        "response_digest": EFFECT_RESPONSE_DIGEST,
    }
    fields.update(overrides)
    return EffectReceipt(**fields)


def issue_effect_capability(holder: m1.Owned) -> None:
    r202.add_policy(
        holder,
        r202.policy(
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            pinned_at=rt107.timestamp(EFFECT_POLICY_US),
        ),
    )
    r203.issue(
        holder,
        r203.grant(
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            capability_grant_id=r203.GRANT_ID,
            policy_snapshot_id=r202.POLICY_ID,
            granted_at=rt107.timestamp(EFFECT_GRANT_US),
        ),
    )


def claimed_effect_step(
    owned: m1.Owned, *downstream: StepDefinition
) -> tuple[WorkflowScheduler, WorkflowStepClaim]:
    materialised = repo.plan(
        repo.step("c.write", execution_class=EXECUTION_CLASS_EFFECT), *downstream
    )
    started(owned, materialised=materialised)
    issue_effect_capability(owned)
    open_steps(owned)
    scheduler = workflow_scheduler_at(owned, now_us=EFFECT_CLAIM_US)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    assert claim.workflow_step_id == "c.write"
    return scheduler, claim


def open_steps(holder: m1.Owned) -> tuple[str, ...]:
    opening = open_workflow_runtime_steps(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=RUN_ID,
        opened_at_us=BASE_US + 30,
    )
    return cast(tuple[str, ...], opening.run_step_ids)


def workflow_scheduler(holder: m1.Owned) -> WorkflowScheduler:
    return workflow_scheduler_at(holder, now_us=BASE_US + 40)


def workflow_scheduler_at(holder: m1.Owned, *, now_us: int) -> WorkflowScheduler:
    return WorkflowScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        FakeClock(wall=datetime.fromtimestamp(now_us / 1_000_000, UTC)),
    )


def governed_authority(role: str = "release_manager") -> WorkflowGovernedAuthority:
    return WorkflowGovernedAuthority(
        actor_id="operator-1",
        actor_role=role,
        approval_id="approval-workflow-governance-0001",
        reason="recover unresolved workflow effect",
    )


def governed_scheduler_at(
    holder: m1.Owned,
    *,
    now_us: int,
    compensation_roles: frozenset[str] = frozenset({"release_manager"}),
    repair_roles: frozenset[str] = frozenset({"release_manager"}),
) -> WorkflowScheduler:
    return WorkflowScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        FakeClock(wall=datetime.fromtimestamp(now_us / 1_000_000, UTC)),
        governance=WorkflowGovernancePolicy(
            compensation_roles=compensation_roles,
            repair_roles=repair_roles,
        ),
    )


def capacity_scheduler(
    holder: m1.Owned,
    *,
    now_us: int = BASE_US + 40,
    priorities: dict[str, int] | None = None,
    recovery_runs: frozenset[str] = frozenset(),
    recovery_reserved: dict[str, int] | None = None,
    escalation_us: int | None = None,
) -> WorkflowScheduler:
    return WorkflowScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        FakeClock(wall=datetime.fromtimestamp(now_us / 1_000_000, UTC)),
        WorkflowCapacityPolicy(
            pools={"shared": 1},
            routes={"DETERMINISTIC": "shared"},
            priorities=priorities or {},
            recovery_runs=recovery_runs,
            recovery_reserved=recovery_reserved or {},
            escalation_us=escalation_us,
        ),
    )


def observe_branch(holder: m1.Owned, values: tuple[tuple[str, str], ...]) -> None:
    with repo.writer(holder) as write:
        write.observe_branch(
            run_id=RUN_ID,
            step_id="b.compute",
            result=repo.BRANCH.evaluate(values),
            observed_at_us=BASE_US + 35,
        )


def record_step_connection(
    holder: m1.Owned,
    *,
    step_id: str = "b.compute",
    direction: str = CONNECTION_INCOMING,
    connection_id: str = "conn-fast",
    fact_kind: str,
    at_us: int = BASE_US + 36,
) -> None:
    with repo.writer(holder) as write:
        write.record_step_connection(
            run_id=RUN_ID,
            step_id=step_id,
            direction=direction,
            connection_id=connection_id,
            fact_kind=fact_kind,
            recorded_at_us=at_us,
        )


def record_step_readiness_fact(
    holder: m1.Owned,
    *,
    step_id: str = "b.compute",
    fact_kind: str,
    fact_id: str,
    at_us: int = BASE_US + 38,
) -> None:
    with repo.writer(holder) as write:
        write.record_step_readiness_fact(
            run_id=RUN_ID,
            step_id=step_id,
            fact_kind=fact_kind,
            fact_id=fact_id,
            recorded_at_us=at_us,
        )


def force_cancelled_event(holder: m1.Owned, *, at_us: int = BASE_US + 41) -> None:
    """A terminal `cancelled` event appended directly, for the terminal guardrails only.

    Deliberately not the public seam: these tests assert what the scheduler refuses once a
    run is terminal, whatever terminalized it. `WorkflowScheduler.cancel_run` is the seam,
    and the cancellation tests below use it.
    """
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        transaction_local_writer(
            holder.connection, workspace_id=WORKSPACE_ID
        ).append_run_event(
            run_id=RUN_ID,
            runtime_event_id="evt-workflow-cancelled",
            occurred_at_us=at_us,
            event_kind="workflow_run_cancelled",
            run_status=RUN_STATUS_CANCELLED,
            message="workflow run cancelled by test control",
        )


def test_workflow_plan_opens_canonical_runtime_steps_in_plan_order(
    owned: m1.Owned,
) -> None:
    started(owned)

    opened = open_steps(owned)
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    view = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert tuple(step.run_step_id for step in runtime_steps) == opened
    assert tuple(step.ordinal for step in runtime_steps) == (1, 2, 3, 4, 5)
    assert tuple(step.step_kind for step in runtime_steps) == (
        "workflow.agent",
        "workflow.deterministic",
        "workflow.effect",
        "workflow.wait",
        "workflow.child_workflow",
    )
    assert tuple(step.status for step in runtime_steps) == ("pending",) * 5
    assert view is not None
    assert tuple(observation.step_id for observation in view.plan_observations) == (
        "a.plan",
        "b.compute",
        "c.write",
        "d.wait",
        "e.child",
    )


def test_reopening_workflow_steps_is_idempotent_after_restart(
    owned: m1.Owned,
) -> None:
    started(owned)
    first = open_steps(owned)
    before = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    owned.connection.close()
    restarted = m1.take_ownership(owned.path)
    try:
        second = open_steps(restarted)
        after = read_workflow_run(
            restarted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )

        assert second == first
        assert after == before
        assert restarted.connection.execute(
            f"SELECT COUNT(*) FROM {m27.OBSERVATIONS}"
        ).fetchone() == (5,)
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_run_steps WHERE workspace_id = ? "
            "AND run_id = ?",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone() == (5,)
    finally:
        restarted.connection.close()


def test_workflow_step_opening_requires_the_current_fenced_owner(
    owned: m1.Owned,
) -> None:
    started(owned)

    with pytest.raises(StaleGeneration):
        open_workflow_runtime_steps(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            run_id=RUN_ID,
            opened_at_us=BASE_US + 30,
        )

    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_run_steps WHERE workspace_id = ? "
            "AND run_id = ?",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone()
        == (0,)
    )


def test_existing_runtime_steps_must_match_the_workflow_plan(
    owned: m1.Owned,
) -> None:
    started(owned)
    append_run_step(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=RUN_ID,
        run_step_id="step-wrong-lineage",
        ordinal=1,
        step_kind="workflow.agent",
        created_at_us=BASE_US + 30,
    )

    with pytest.raises(WorkflowSchedulingError, match="do not match"):
        open_workflow_runtime_steps(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            run_id=RUN_ID,
            opened_at_us=BASE_US + 31,
        )

    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.run_step_id for step in runtime_steps) == ("step-wrong-lineage",)


def test_workflow_scheduler_claims_only_dependency_ready_steps(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)

    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert first.workflow_step_id == "a.plan"
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    status = scheduler.complete_step(first)
    assert status == "running"
    second = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert second is not None
    assert second.workflow_step_id == "b.compute"


def test_cancelled_run_does_not_claim_pending_work(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    force_cancelled_event(owned)
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_run_cancelled", "cancelled")


def test_cancelled_run_refuses_late_step_completion_without_mutating(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    force_cancelled_event(owned)
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="terminal"):
        scheduler.complete_step(first)

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_run_cancelled", "cancelled")


def test_workflow_scheduler_cancel_run_records_stop_and_closes_open_attempt(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None

    assert scheduler.cancel_run(run_id=RUN_ID, stop_request_id="stp-workflow-0001") == (
        "cancelled"
    )
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert run is not None
    assert run.status == "cancelled"
    assert run.stop is not None
    assert run.stop.run_stop_id == "stp-workflow-0001"
    assert run.stop.stop_reason == "cancelled"
    assert run.steps[0].status == "cancelled"
    assert run.steps[0].attempts[0].status == "cancelled"
    assert run.steps[1].status == "cancelled"
    assert stop_counts(owned) == (1, 1)
    assert last_event(owned) == ("run_stopped", "cancelled")


def test_branch_step_waits_for_branch_observation(
    owned: m1.Owned,
) -> None:
    started(owned)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[1].status == "pending"


def test_matched_branch_step_can_be_claimed(
    owned: m1.Owned,
) -> None:
    started(owned)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    observe_branch(owned, (("mode", "fast"),))

    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert second is not None
    assert second.workflow_step_id == "b.compute"


def test_blocked_branch_step_remains_pending(
    owned: m1.Owned,
) -> None:
    started(owned)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    observe_branch(owned, ())

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[1].status == "pending"


def test_unmatched_branch_step_is_skipped_without_attempt(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", branch=repo.BRANCH, depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    observe_branch(owned, (("mode", "slow"),))

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "skipped")
    assert runtime_steps[1].attempts == ()
    assert owned.connection.execute(
        "SELECT event_kind, run_status FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 1",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone() == ("workflow_run_partially_completed", "partially_completed")


def test_required_selected_connection_blocks_ready_step_without_mutating(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_connection(owned, fact_kind=CONNECTION_REQUIRED)
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "pending")
    assert runtime_steps[1].attempts == ()
    assert last_event(owned) == ("workflow_step_succeeded", "running")


def test_selected_connection_with_wrong_direction_does_not_satisfy_required_gate(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_connection(owned, fact_kind=CONNECTION_REQUIRED)
    record_step_connection(
        owned,
        direction=CONNECTION_OUTGOING,
        fact_kind=CONNECTION_SELECTED,
        at_us=BASE_US + 37,
    )
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before


def test_recorded_selected_connection_permits_claim_and_is_reported(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_connection(owned, fact_kind=CONNECTION_REQUIRED)
    record_step_connection(owned, fact_kind=CONNECTION_SELECTED, at_us=BASE_US + 37)

    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert second is not None
    assert second.workflow_step_id == "b.compute"
    details = json.loads(
        owned.connection.execute(
            "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? "
            "AND run_id = ? ORDER BY sequence DESC LIMIT 1",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone()[0]
    )
    assert details["selected_incoming_connections"] == ["conn-fast"]
    assert details["selected_outgoing_connections"] == []


def test_required_mapped_input_blocks_ready_step_without_mutating(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_readiness_fact(
        owned,
        fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
        fact_id="input.customer",
    )
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "pending")
    assert runtime_steps[1].attempts == ()


def test_recorded_mapped_input_permits_claim_and_is_reported(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_readiness_fact(
        owned,
        fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
        fact_id="input.customer",
    )
    record_step_readiness_fact(
        owned,
        fact_kind=READINESS_MAPPED_INPUT_READY,
        fact_id="input.customer",
        at_us=BASE_US + 39,
    )

    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert second is not None
    assert second.workflow_step_id == "b.compute"
    details = json.loads(
        owned.connection.execute(
            "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? "
            "AND run_id = ? ORDER BY sequence DESC LIMIT 1",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone()[0]
    )
    assert details["mapped_inputs_ready"] == ["input.customer"]


def test_required_capability_blocks_until_grant_fact_is_recorded(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"
    record_step_readiness_fact(
        owned,
        fact_kind=READINESS_CAPABILITY_REQUIRED,
        fact_id="cap.mail.send",
    )
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    record_step_readiness_fact(
        owned,
        fact_kind=READINESS_CAPABILITY_GRANTED,
        fact_id="cap.mail.send",
        at_us=BASE_US + 39,
    )
    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert counts(owned)[0:2] == before[0:2]
    assert second is not None
    assert second.workflow_step_id == "b.compute"
    details = json.loads(
        owned.connection.execute(
            "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? "
            "AND run_id = ? ORDER BY sequence DESC LIMIT 1",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone()[0]
    )
    assert details["capability_grants"] == ["cap.mail.send"]


def wait_run(owned: m1.Owned) -> WorkflowScheduler:
    """A run whose second step is a ROUTE_WAIT step, advanced up to that step."""
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("d.wait", EXECUTION_CLASS_WAIT, depends_on=("a.plan",)),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    return scheduler


def workflow_wait_command(
    owned: m1.Owned,
    scheduler: WorkflowScheduler,
    *,
    key: str = "workflow-rt107-resolve-0001",
    reason: str = "signal_received",
    resume_digest: str | None = None,
    wait_id: str | None = None,
    policy: rt107.RecordingPolicy | None = None,
) -> Any:
    waits = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    wait = waits[0]
    context, equivalence, grant = rt107.authority(owned, key)
    command = rt107.resolution(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        wait_id=wait.wait_id if wait_id is None else wait_id,
        resume_digest=wait.resume_digest if resume_digest is None else resume_digest,
        reason=reason,
    )
    return scheduler.resolve_wait_step_command(
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=command,
        policy=rt107.RecordingPolicy() if policy is None else policy,
        runtime_event_id=f"evt-{key}",
        validate_result=s0.accept_any,
        expected=RuntimeAggregateExpectation(
            run_id=RUN_ID,
            sequence=read_run_sequence(
                owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            ),
        ),
    )


def last_event(owned: m1.Owned) -> tuple[str, str]:
    return cast(
        tuple[str, str],
        owned.connection.execute(
            "SELECT event_kind, run_status FROM omnivia_runtime_events "
            "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 1",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone(),
    )


def dispatch_count(owned: m1.Owned, intent_id: str = EFFECT_INTENT_ID) -> int:
    return cast(
        int,
        read_effect_dispatch_count(
            owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=intent_id
        ),
    )


def counts(owned: m1.Owned) -> tuple[int, int, int, int]:
    """Waits, wait resolutions, step status entries and run events this run recorded."""
    return cast(
        tuple[int, int, int, int],
        owned.connection.execute(
            "SELECT (SELECT COUNT(*) FROM omnivia_runtime_waits WHERE workspace_id = ? "
            "AND run_id = ?), (SELECT COUNT(*) FROM omnivia_runtime_wait_resolutions "
            "WHERE workspace_id = ?), (SELECT COUNT(*) FROM omnivia_runtime_run_step_states "
            "WHERE workspace_id = ?), (SELECT COUNT(*) FROM omnivia_runtime_events "
            "WHERE workspace_id = ? AND run_id = ?)",
            (WORKSPACE_ID, RUN_ID, WORKSPACE_ID, WORKSPACE_ID, WORKSPACE_ID, RUN_ID),
        ).fetchone(),
    )


def stop_counts(owned: m1.Owned) -> tuple[int, int]:
    return cast(
        tuple[int, int],
        owned.connection.execute(
            "SELECT (SELECT COUNT(*) FROM omnivia_runtime_stop_requests "
            "WHERE workspace_id = ? AND run_id = ?), "
            "(SELECT COUNT(*) FROM omnivia_runtime_stop_outcomes WHERE workspace_id = ?)",
            (WORKSPACE_ID, RUN_ID, WORKSPACE_ID),
        ).fetchone(),
    )


def loop_counts(owned: m1.Owned) -> tuple[int, int, int, int]:
    """Loop iterations, loop outcomes, step states and run events this run recorded."""
    return cast(
        tuple[int, int, int, int],
        owned.connection.execute(
            "SELECT (SELECT COUNT(*) FROM omnivia_workflow_loop_iterations "
            "WHERE workspace_id = ? AND run_id = ?), "
            "(SELECT COUNT(*) FROM omnivia_workflow_loop_iteration_outcomes "
            "WHERE workspace_id = ?), "
            "(SELECT COUNT(*) FROM omnivia_runtime_run_step_states "
            "WHERE workspace_id = ?), "
            "(SELECT COUNT(*) FROM omnivia_runtime_events "
            "WHERE workspace_id = ? AND run_id = ?)",
            (WORKSPACE_ID, RUN_ID, WORKSPACE_ID, WORKSPACE_ID, WORKSPACE_ID, RUN_ID),
        ).fetchone(),
    )


def test_ready_wait_step_suspends_on_one_durable_wait_without_an_attempt(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    waits = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "waiting")
    assert runtime_steps[1].attempts == ()
    assert len(waits) == 1
    assert waits[0].run_step_id == runtime_steps[1].run_step_id
    assert waits[0].kind == WORKFLOW_WAIT_KIND
    assert waits[0].status == "pending"
    assert last_event(owned) == ("workflow_step_waiting", "waiting")


def test_advancing_an_unresolved_wait_again_changes_nothing(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_step_waiting", "waiting")


def test_resolving_the_last_wait_step_succeeds_it_and_the_run(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "succeeded"
    )

    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    waits = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "succeeded")
    assert runtime_steps[1].attempts == ()
    assert (waits[0].status, waits[0].resolution_reason) == (
        "resolved",
        WORKFLOW_WAIT_RESOLUTION_REASON,
    )
    # `waiting` reaches a terminal run status only through `running`, so the resolution
    # and the run's completion are two events, in that order.
    assert owned.connection.execute(
        "SELECT event_kind, run_status FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 2",
        (WORKSPACE_ID, RUN_ID),
    ).fetchall() == [
        ("workflow_run_succeeded", "succeeded"),
        ("workflow_step_wait_resolved", "running"),
    ]
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None


def test_resolved_wait_makes_downstream_work_claimable(
    owned: m1.Owned,
) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("d.wait", EXECUTION_CLASS_WAIT, depends_on=("a.plan",)),
            repo.step("c.finish", depends_on=("d.wait",)),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "running"
    )

    downstream = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert downstream is not None
    assert downstream.workflow_step_id == "c.finish"
    assert scheduler.complete_step(downstream) == "succeeded"
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_resolving_the_same_wait_again_changes_nothing(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "succeeded"
    )
    before = counts(owned)

    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "succeeded"
    )

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_rt107_command_resolves_scheduler_owned_workflow_wait(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    policy = rt107.RecordingPolicy()

    outcome = workflow_wait_command(owned, scheduler, policy=policy)

    assert outcome.replayed is False
    assert outcome.result["status"] == "resolved"
    assert outcome.result["resolution_reason"] == "signal_received"
    assert policy.calls == 1
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    waits = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "succeeded")
    assert runtime_steps[1].attempts == ()
    assert waits[0].status == "resolved"
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_rt107_same_key_replays_workflow_wait_without_second_write_or_policy(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    policy = rt107.RecordingPolicy()
    first = workflow_wait_command(owned, scheduler, policy=policy)
    settled = counts(owned)

    second = workflow_wait_command(owned, scheduler, policy=policy)

    assert first.replayed is False
    assert second.replayed is True
    assert second.result == first.result
    assert policy.calls == 1
    assert counts(owned) == settled


def test_rt107_identical_second_key_returns_workflow_wait_resolution(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    first = workflow_wait_command(owned, scheduler)
    settled = counts(owned)
    second_policy = rt107.RecordingPolicy()

    second = workflow_wait_command(
        owned,
        scheduler,
        key="workflow-rt107-resolve-0002",
        policy=second_policy,
    )

    assert second.replayed is False
    assert second.result == first.result
    assert second_policy.calls == 1
    assert counts(owned) == settled


def test_rt107_conflicting_second_key_refuses_workflow_wait_resolution(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    workflow_wait_command(owned, scheduler)
    settled = counts(owned)
    policy = rt107.RecordingPolicy()

    with pytest.raises(WaitResolutionConflict, match="resolved exactly once"):
        workflow_wait_command(
            owned,
            scheduler,
            key="workflow-rt107-resolve-conflict",
            reason="different_signal",
            policy=policy,
        )

    assert policy.calls == 0
    assert counts(owned) == settled


def test_rt107_stale_digest_refuses_workflow_wait_before_policy(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)
    policy = rt107.RecordingPolicy()

    with pytest.raises(WaitResolutionConflict, match="resume_digest"):
        workflow_wait_command(
            owned,
            scheduler,
            resume_digest="sha256:" + "f" * 64,
            policy=policy,
        )

    assert policy.calls == 0
    assert counts(owned) == before


def test_rt107_command_refuses_capacity_wait_as_workflow_route_resolution(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    scheduler = capacity_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)

    with pytest.raises(WaitResolutionConflict, match="not a scheduler-owned"):
        workflow_wait_command(owned, scheduler)

    assert counts(owned) == before


def test_resolving_a_wait_that_was_never_opened_is_refused(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)

    with pytest.raises(WorkflowSchedulingError, match="not waiting"):
        scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait")

    assert read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID) == ()


# --- M4-B2: durable wait purposes over the three canonical wait kinds -------------


def purposed_scheduler(
    owned: m1.Owned, purpose: WorkflowWaitPurpose
) -> WorkflowScheduler:
    """A scheduler that declares why `d.wait` suspends, over the same connection."""
    return WorkflowScheduler(
        owned.connection,
        owned.identity,
        WORKSPACE_ID,
        owned.generation,
        FakeClock(wall=datetime.fromtimestamp((BASE_US + 40) / 1_000_000, UTC)),
        wait_purposes={"d.wait": purpose},
    )


def purposed_wait_run(
    owned: m1.Owned, purpose: WorkflowWaitPurpose
) -> WorkflowScheduler:
    """`wait_run`, with a declared purpose, advanced up to the suspended wait step."""
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("d.wait", EXECUTION_CLASS_WAIT, depends_on=("a.plan",)),
        ),
    )
    open_steps(owned)
    scheduler = purposed_scheduler(owned, purpose)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    return scheduler


def last_details(owned: m1.Owned) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            owned.connection.execute(
                "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? "
                "AND run_id = ? ORDER BY sequence DESC LIMIT 1",
                (WORKSPACE_ID, RUN_ID),
            ).fetchone()[0]
        ),
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("purpose", "kind"),
    [
        (WorkflowWaitPurpose("event"), "external_signal"),
        (WorkflowWaitPurpose("asynchronous_operation"), "external_signal"),
        (WorkflowWaitPurpose("child_workflow_run"), "external_signal"),
        (WorkflowWaitPurpose("retry_backoff", checkpoint="attempt-2"), "external_signal"),
        (WorkflowWaitPurpose("effect_reconciliation"), "external_signal"),
        (WorkflowWaitPurpose("capacity_or_quota", checkpoint="shared"), "external_signal"),
        (WorkflowWaitPurpose("timer", expires_at_us=BASE_US + 1_000_000), "timer"),
        (WorkflowWaitPurpose("human_task", approver_role="runtime_approver"), "approval"),
        (
            WorkflowWaitPurpose("administrative_suspension", approver_role="operator"),
            "approval",
        ),
    ],
)
def test_every_supported_purpose_opens_its_canonical_kind_with_stated_metadata(
    owned: m1.Owned, purpose: WorkflowWaitPurpose, kind: str
) -> None:
    purposed_wait_run(owned, purpose)

    waits = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    details = last_details(owned)
    assert len(waits) == 1
    assert waits[0].kind == kind
    assert last_event(owned) == ("workflow_step_waiting", "waiting")
    assert details["wait_purpose"] == purpose.purpose
    assert details["wait_kind"] == kind
    assert details["wait_checkpoint"] == purpose.checkpoint
    assert details["expected_resolution"] == WAIT_RESOLUTION_FOR_KIND[kind]
    assert details["wait_id"] == waits[0].wait_id
    assert details["resume_digest"] == waits[0].resume_digest
    # The digest is auditable rather than opaque: the event states the exact preimage,
    # and it names the step, the purpose and the checkpoint this suspension is for.
    preimage = cast(str, details["resume_digest_preimage"])
    assert details["resume_digest"] == "sha256:" + sha256(preimage.encode()).hexdigest()
    assert preimage.split("|")[-3:] == [
        waits[0].run_step_id,
        purpose.purpose,
        purpose.checkpoint,
    ]
    assert details.get("approver_role") == purpose.approver_role
    assert details.get("wait_expires_at_us") == purpose.expires_at_us
    assert (waits[0].expires_at is not None) == (purpose.expires_at_us is not None)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("purpose", "message"),
    [
        ({"purpose": "webhook"}, "not a workflow wait purpose"),
        ({"purpose": "human_task"}, "opened with an approver_role"),
        ({"purpose": "administrative_suspension"}, "opened with an approver_role"),
        (
            {"purpose": "event", "approver_role": "operator"},
            "opened with an approver_role",
        ),
        ({"purpose": "timer"}, "opened with an expires_at_us"),
        ({"purpose": "event", "expires_at_us": 1}, "opened with an expires_at_us"),
    ],
)
def test_a_purpose_the_durable_schema_cannot_honour_is_refused(
    purpose: dict[str, Any], message: str
) -> None:
    with pytest.raises(WorkflowSchedulingError, match=message):
        WorkflowWaitPurpose(**purpose)


def test_advancing_a_purposed_wait_again_opens_no_second_wait(owned: m1.Owned) -> None:
    purpose = WorkflowWaitPurpose("retry_backoff", checkpoint="attempt-2")
    scheduler = purposed_wait_run(owned, purpose)
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    assert len(read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)) == 1


def test_a_restarted_scheduler_keeps_the_purposed_wait_and_resolves_it(
    owned: m1.Owned,
) -> None:
    purpose = WorkflowWaitPurpose("child_workflow_run", checkpoint="child-1")
    purposed_wait_run(owned, purpose)
    suspended = read_run_waits(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    before = counts(owned)

    # A second scheduler instance over the same durable records: nothing about the
    # suspension lived in the first one, so re-advancing opens no second wait and the
    # restarted scheduler resolves the wait the previous one published.
    restarted = purposed_scheduler(owned, purpose)
    assert restarted.claim_next_ready_step(run_id=RUN_ID) is None
    assert counts(owned) == before
    assert (
        read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == suspended
    )

    outcome = workflow_wait_command(owned, restarted)

    assert outcome.result["status"] == "resolved"
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_a_wait_opened_for_another_purpose_is_refused_before_policy(
    owned: m1.Owned,
) -> None:
    purposed_wait_run(owned, WorkflowWaitPurpose("retry_backoff", checkpoint="attempt-2"))
    before = counts(owned)
    policy = rt107.RecordingPolicy()

    # Same step, same kind, same command digest -- a different purpose. The wait row has
    # no purpose column, so this is caught by re-deriving the digest the wait published.
    relabelled = purposed_scheduler(
        owned, WorkflowWaitPurpose("retry_backoff", checkpoint="attempt-3")
    )
    with pytest.raises(WaitResolutionConflict, match="not opened for purpose"):
        workflow_wait_command(owned, relabelled, policy=policy)

    assert policy.calls == 0
    assert counts(owned) == before


def test_an_approval_purpose_is_not_settled_in_process(owned: m1.Owned) -> None:
    scheduler = purposed_wait_run(
        owned, WorkflowWaitPurpose("human_task", approver_role="runtime_approver")
    )
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="RT-107 command"):
        scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait")

    assert counts(owned) == before


def test_an_approval_purpose_resolves_through_a_recorded_decision(
    owned: m1.Owned,
) -> None:
    scheduler = purposed_wait_run(
        owned, WorkflowWaitPurpose("human_task", approver_role="runtime_approver")
    )
    wait = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    approval = Approval(
        workspace_id=WORKSPACE_ID,
        approval_id="apr-workflow-0001",
        run_id=RUN_ID,
        wait_id=wait.wait_id,
        requested_at=rt107.timestamp(BASE_US + 40),
        approver_role="runtime_approver",
        decision="approved",
        decided_at=rt107.timestamp(BASE_US + 50),
        decided_by="principal-approver",
        audit_reference=m18.audit_ref_for(JOB_ID),
    )
    context, equivalence, grant = rt107.authority(owned, "workflow-human-task-0001")

    outcome = scheduler.resolve_wait_step_command(
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=rt107.resolution(
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            wait_id=wait.wait_id,
            resume_digest=wait.resume_digest,
            resolution_kind="approval_decision",
            approval_id=approval.approval_id,
            reason="human_task_completed",
        ),
        policy=rt107.RecordingPolicy(approval=approval),
        runtime_event_id="evt-workflow-human-task-0001",
        validate_result=s0.accept_any,
        expected=RuntimeAggregateExpectation(
            run_id=RUN_ID,
            sequence=read_run_sequence(
                owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            ),
        ),
    )

    resolved = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    assert outcome.result["status"] == "resolved"
    assert (resolved.status, resolved.approval_id) == ("resolved", approval.approval_id)
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_an_approval_purpose_refuses_an_external_signal_resolution(
    owned: m1.Owned,
) -> None:
    scheduler = purposed_wait_run(
        owned, WorkflowWaitPurpose("administrative_suspension", approver_role="operator")
    )
    before = counts(owned)
    policy = rt107.RecordingPolicy()

    with pytest.raises(WaitResolutionConflict, match="resolves through 'approval_decision'"):
        workflow_wait_command(owned, scheduler, policy=policy)

    assert policy.calls == 0
    assert counts(owned) == before


def test_resolving_a_step_that_is_not_a_wait_step_is_refused(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="no workflow wait step"):
        scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="a.plan")

    assert counts(owned) == before


def test_cancelled_waiting_run_does_not_resolve_scheduler_wait(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    force_cancelled_event(owned)
    before = counts(owned)

    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "cancelled"
    )

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_run_cancelled", "cancelled")


def test_workflow_scheduler_cancel_run_closes_scheduler_owned_wait(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert scheduler.cancel_run(run_id=RUN_ID, stop_request_id="stp-workflow-0002") == (
        "cancelled"
    )
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)

    assert run is not None
    assert run.status == "cancelled"
    assert run.waits[0].status == "cancelled"
    assert run.waits[0].resolution_reason == "cancelled"
    assert stop_counts(owned) == (1, 1)
    assert scheduler.resolve_wait_step(run_id=RUN_ID, workflow_step_id="d.wait") == (
        "cancelled"
    )


@contextmanager
def restarted(owned: m1.Owned) -> Iterator[m1.Owned]:
    """The same workspace reopened by a new service instance under a new generation."""
    owned.connection.close()
    holder = m1.take_ownership(owned.path)
    try:
        yield holder
    finally:
        holder.connection.close()


def two_step_run(owned: m1.Owned) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("b.compute", depends_on=("a.plan",)),
        ),
    )
    open_steps(owned)


def independent_two_step_run(owned: m1.Owned) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("b.compute"),
        ),
    )
    open_steps(owned)


def second_workflow_run(owned: m1.Owned, *, run_id: str) -> None:
    first = read_workflow_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert first is not None
    m18.seed_job(owned, job_id=f"{JOB_ID}-{run_id}")
    admit_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        admission=RunAdmission(
            run_id=run_id,
            job_id=f"{JOB_ID}-{run_id}",
            claim_id=m18.claim_id_for(f"{JOB_ID}-{run_id}"),
            definition=RunDefinitionRef(
                definition_kind="workflow",
                definition_id=repo.WORKFLOW_ID,
                definition_version=repo.WORKFLOW_VERSION,
            ),
            logical_key=m18.logical_key_for(f"{JOB_ID}-{run_id}"),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(f"{JOB_ID}-{run_id}"),
            admitted_at_us=BASE_US + 10,
            runtime_event_id=f"evt-workflow-admitted-{run_id}",
            message="workflow run admitted",
        ),
    )
    repo.admit(
        owned,
        repo.admission(
            run_id=run_id,
            plan_hash=first.plan.plan_hash,
            bound_at_us=BASE_US + 11,
        ),
    )
    open_workflow_runtime_steps(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=run_id,
        opened_at_us=BASE_US + 30,
    )


def capacity_wait_rows(
    owned: m1.Owned,
) -> list[tuple[str, str, str, str, str, str | None, str | None]]:
    return cast(
        list[tuple[str, str, str, str, str, str | None, str | None]],
        owned.connection.execute(
            "SELECT w.wait_id, w.run_id, w.run_step_id, w.kind, w.resume_digest, "
            "r.status, r.resolution_reason "
            "FROM omnivia_runtime_waits w "
            "LEFT JOIN omnivia_runtime_wait_resolutions r "
            "ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id "
            "WHERE w.workspace_id = ? ORDER BY w.created_at_us, w.wait_id",
            (WORKSPACE_ID,),
        ).fetchall(),
    )


def test_capacity_full_step_queues_on_a_durable_external_signal_wait(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    scheduler = capacity_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    before_second_pass = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    after_wait = counts(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    waits = capacity_wait_rows(owned)
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert after_wait[0] == before_second_pass[0] + 1
    assert counts(owned) == after_wait
    assert len(waits) == 1
    assert waits[0][3] == CAPACITY_WAIT_KIND
    assert waits[0][5] is None
    assert runtime_steps[1].status == "waiting"
    assert runtime_steps[1].attempts == ()
    assert last_event(owned) == ("workflow_step_capacity_waiting", "running")


def test_capacity_wait_resolves_to_a_real_claim_when_the_slot_frees(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    scheduler = capacity_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert scheduler.complete_step(first) == "running"
    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    waits = capacity_wait_rows(owned)
    assert second is not None
    assert second.workflow_step_id == "b.compute"
    assert waits[0][5] == "resolved"
    assert waits[0][6] == CAPACITY_WAIT_RESOLUTION_REASON
    assert scheduler.complete_step(second) == "succeeded"


def test_capacity_pool_is_shared_across_workflow_runs(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    second_workflow_run(owned, run_id="run-workflow-2")
    scheduler = capacity_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None

    assert scheduler.claim_next_ready_step(run_id="run-workflow-2") is None

    waits = capacity_wait_rows(owned)
    assert len(waits) == 1
    assert waits[0][1] == "run-workflow-2"


def test_capacity_waiters_use_priority_then_fifo_order(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    second_workflow_run(owned, run_id="run-workflow-2")
    scheduler = capacity_scheduler(
        owned, priorities={RUN_ID: 0, "run-workflow-2": 5}
    )
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    assert scheduler.claim_next_ready_step(run_id="run-workflow-2") is None
    assert scheduler.complete_step(first) == "running"

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    preferred = scheduler.claim_next_ready_step(run_id="run-workflow-2")

    assert preferred is not None
    assert preferred.run_id == "run-workflow-2"


def test_queue_age_escalation_can_overtake_newer_priority(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    second_workflow_run(owned, run_id="run-workflow-2")
    scheduler = capacity_scheduler(
        owned,
        priorities={RUN_ID: 0, "run-workflow-2": 5},
        escalation_us=1,
    )
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    later_arrival = capacity_scheduler(
        owned,
        now_us=BASE_US + 90,
        priorities={RUN_ID: 0, "run-workflow-2": 5},
        escalation_us=1,
    )
    assert later_arrival.claim_next_ready_step(run_id="run-workflow-2") is None
    later = capacity_scheduler(
        owned,
        now_us=BASE_US + 100,
        priorities={RUN_ID: 0, "run-workflow-2": 5},
        escalation_us=1,
    )
    assert later.complete_step(first) == "running"

    escalated = later.claim_next_ready_step(run_id=RUN_ID)

    assert escalated is not None
    assert escalated.run_id == RUN_ID


def test_recovery_lane_can_use_a_reserved_capacity_slot(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    second_workflow_run(owned, run_id="run-workflow-2")
    scheduler = WorkflowScheduler(
        owned.connection,
        owned.identity,
        WORKSPACE_ID,
        owned.generation,
        FakeClock(wall=datetime.fromtimestamp((BASE_US + 40) / 1_000_000, UTC)),
        WorkflowCapacityPolicy(
            pools={"shared": 2},
            routes={"DETERMINISTIC": "shared"},
            recovery_runs=frozenset({"run-workflow-2"}),
            recovery_reserved={"shared": 1},
        ),
    )
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    recovery = scheduler.claim_next_ready_step(run_id="run-workflow-2")

    assert recovery is not None
    assert recovery.run_id == "run-workflow-2"
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    details = json.loads(
        owned.connection.execute(
            "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? "
            "AND run_id = ? AND event_kind = 'workflow_step_capacity_waiting'",
            (WORKSPACE_ID, RUN_ID),
        ).fetchone()[0]
    )
    assert details["capacity_lane"] == "default"
    assert details["capacity_pool"] == "shared"
    assert CAPACITY_LANE_RECOVERY == "recovery"


def test_restarted_owner_does_not_reclaim_a_step_left_running(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    claim = workflow_scheduler(owned).claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    before = counts(owned)

    with restarted(owned) as holder:
        assert workflow_scheduler(holder).claim_next_ready_step(run_id=RUN_ID) is None

        assert counts(holder) == before
        runtime_steps = read_run_steps(
            holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert tuple(step.status for step in runtime_steps) == ("running", "pending")
        assert len(runtime_steps[0].attempts) == 1
        assert runtime_steps[0].attempts[0].attempt_id == claim.runtime_attempt_id


def test_restarted_owner_refuses_to_complete_the_previous_owners_claim(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    claim = workflow_scheduler(owned).claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    before = counts(owned)

    with restarted(owned) as holder:
        with pytest.raises(WorkflowSchedulingError, match="does not match this"):
            workflow_scheduler(holder).complete_step(claim)

        assert counts(holder) == before
        runtime_steps = read_run_steps(
            holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert runtime_steps[0].status == "running"
        assert runtime_steps[0].attempts[0].status == "running"


def test_restarted_owner_claims_the_dependency_ready_step_exactly_once(
    owned: m1.Owned,
) -> None:
    two_step_run(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.complete_step(first) == "running"

    with restarted(owned) as holder:
        resumed = workflow_scheduler(holder)
        second = resumed.claim_next_ready_step(run_id=RUN_ID)

        assert second is not None
        assert second.workflow_step_id == "b.compute"
        assert second.service_instance_id == holder.identity.service_instance_id
        assert resumed.claim_next_ready_step(run_id=RUN_ID) is None
        runtime_steps = read_run_steps(
            holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert len(runtime_steps[1].attempts) == 1
        assert resumed.complete_step(second) == "succeeded"


def test_restarted_owner_resolves_an_open_route_wait_without_reopening_it(
    owned: m1.Owned,
) -> None:
    scheduler = wait_run(owned)
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)

    with restarted(owned) as holder:
        resumed = workflow_scheduler(holder)

        assert resumed.resolve_wait_step(
            run_id=RUN_ID, workflow_step_id="d.wait"
        ) == "succeeded"

        assert counts(holder) == (before[0], before[1] + 1, before[2] + 1, before[3] + 2)
        runtime_steps = read_run_steps(
            holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert tuple(step.status for step in runtime_steps) == (
            "succeeded",
            "succeeded",
        )


def test_restarted_owner_preserves_capacity_wait_without_duplicate_queue_entry(
    owned: m1.Owned,
) -> None:
    independent_two_step_run(owned)
    scheduler = capacity_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    before = counts(owned)
    waits_before = capacity_wait_rows(owned)

    with restarted(owned) as holder:
        resumed = capacity_scheduler(holder)

        assert resumed.claim_next_ready_step(run_id=RUN_ID) is None
        assert counts(holder) == before
        assert capacity_wait_rows(holder) == waits_before


def loop_run(owned: m1.Owned) -> WorkflowScheduler:
    """A run whose second step declares a loop, advanced up to that step."""
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step("b.compute", depends_on=("a.plan",), loop=repo.LOOP),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    return scheduler


def test_dependency_ready_loop_step_claims_a_durable_iteration_not_a_one_shot(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)

    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert isinstance(claim, WorkflowLoopIterationClaim)
    assert claim.workflow_step_id == "b.compute"
    assert claim.loop_iteration_number == 1
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    iterations = read_workflow_loop_iterations(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "running")
    assert len(runtime_steps[1].attempts) == 1
    assert runtime_steps[1].attempts[0].status == "running"
    assert len(iterations) == 1
    assert iterations[0].loop_iteration_id == claim.loop_iteration_id
    assert iterations[0].is_open
    assert last_event(owned) == ("workflow_loop_iteration_claimed", "running")


def test_open_loop_iteration_is_not_claimed_twice(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)
    before = loop_counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert loop_counts(owned) == before


def test_restarted_owner_preserves_open_loop_iteration_without_duplicate_claim(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)
    before = loop_counts(owned)

    with restarted(owned) as holder:
        resumed = workflow_scheduler(holder)

        assert resumed.claim_next_ready_step(run_id=RUN_ID) is None
        assert loop_counts(holder) == before
        iterations = read_workflow_loop_iterations(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            step_id="b.compute",
        )
        assert len(iterations) == 1
        assert iterations[0].loop_iteration_id == claim.loop_iteration_id
        assert iterations[0].is_open


def test_continuing_a_loop_reuses_the_same_runtime_attempt(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(first, WorkflowLoopIterationClaim)

    assert scheduler.complete_loop_iteration(
        first, cost=2, continue_requested=True
    ) == "running"
    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert isinstance(second, WorkflowLoopIterationClaim)
    assert second.loop_iteration_number == 2
    assert second.runtime_attempt_id == first.runtime_attempt_id
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    iterations = read_workflow_loop_iterations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_id="b.compute",
    )
    assert len(runtime_steps[1].attempts) == 1
    assert tuple(iteration.is_open for iteration in iterations) == (False, True)


def test_requested_loop_exit_succeeds_the_step_and_the_run(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)

    assert scheduler.complete_loop_iteration(
        claim, cost=2, continue_requested=False
    ) == "succeeded"

    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    iterations = read_workflow_loop_iterations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_id="b.compute",
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "succeeded")
    assert runtime_steps[1].attempts[0].status == "succeeded"
    assert iterations[-1].exit_reason == "requested"
    view = read_workflow_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert view is not None
    assert tuple(
        (iteration.iteration_number, iteration.exit_reason)
        for iteration in view.loop_iterations
    ) == ((1, "requested"),)
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")


def test_loop_exits_when_max_iterations_is_reached(
    owned: m1.Owned,
) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step(
                "b.compute",
                depends_on=("a.plan",),
                loop=LoopDefinition(max_iterations=2, per_iteration_budget=5, total_budget=15),
            ),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None and not isinstance(first, WorkflowLoopIterationClaim)
    scheduler.complete_step(first)
    loop_first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(loop_first, WorkflowLoopIterationClaim)
    assert scheduler.complete_loop_iteration(
        loop_first, cost=2, continue_requested=True
    ) == "running"
    loop_second = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(loop_second, WorkflowLoopIterationClaim)

    assert scheduler.complete_loop_iteration(
        loop_second, cost=2, continue_requested=True
    ) == "succeeded"

    iterations = read_workflow_loop_iterations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_id="b.compute",
    )
    assert iterations[-1].continue_requested is True
    assert iterations[-1].exit_reason == "max_iterations"
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None


def test_loop_exits_when_total_budget_is_reached(
    owned: m1.Owned,
) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step(
                "b.compute",
                depends_on=("a.plan",),
                loop=LoopDefinition(max_iterations=3, per_iteration_budget=5, total_budget=6),
            ),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None and not isinstance(first, WorkflowLoopIterationClaim)
    scheduler.complete_step(first)
    loop_first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(loop_first, WorkflowLoopIterationClaim)
    assert scheduler.complete_loop_iteration(
        loop_first, cost=5, continue_requested=True
    ) == "running"
    loop_second = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(loop_second, WorkflowLoopIterationClaim)

    assert scheduler.complete_loop_iteration(
        loop_second, cost=1, continue_requested=True
    ) == "succeeded"

    iterations = read_workflow_loop_iterations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_id="b.compute",
    )
    assert iterations[-1].continue_requested is True
    assert iterations[-1].exit_reason == "total_budget"
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None


def test_over_budget_loop_completion_refuses_without_mutating_history(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)
    before = loop_counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="could not be completed"):
        scheduler.complete_loop_iteration(claim, cost=6, continue_requested=False)

    assert loop_counts(owned) == before


def test_loop_iteration_rows_are_append_only(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(
                "UPDATE omnivia_workflow_loop_iterations SET opened_at_us = ? "
                "WHERE workspace_id = ? AND loop_iteration_id = ?",
                (BASE_US + 999, WORKSPACE_ID, claim.loop_iteration_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(
                "DELETE FROM omnivia_workflow_loop_iterations "
                "WHERE workspace_id = ? AND loop_iteration_id = ?",
                (WORKSPACE_ID, claim.loop_iteration_id),
            )


def test_loop_iteration_outcome_rows_are_append_only(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)
    scheduler.complete_loop_iteration(claim, cost=2, continue_requested=True)

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(
                "UPDATE omnivia_workflow_loop_iteration_outcomes SET cost = ? "
                "WHERE workspace_id = ? AND loop_iteration_id = ?",
                (3, WORKSPACE_ID, claim.loop_iteration_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(
                "DELETE FROM omnivia_workflow_loop_iteration_outcomes "
                "WHERE workspace_id = ? AND loop_iteration_id = ?",
                (WORKSPACE_ID, claim.loop_iteration_id),
            )


def test_loop_iteration_sequence_is_contiguous_at_the_migration_boundary(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert isinstance(claim, WorkflowLoopIterationClaim)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        owned.connection.execute(
            "INSERT INTO omnivia_workflow_loop_iterations "
            "(workspace_id, run_id, step_id, iteration_number, "
            "loop_iteration_id, runtime_attempt_id, opened_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                RUN_ID,
                "b.compute",
                3,
                "loop-gap",
                claim.runtime_attempt_id,
                BASE_US + 50,
            ),
        )


def test_loop_step_of_an_unmatched_branch_is_still_skipped(
    owned: m1.Owned,
) -> None:
    started(
        owned,
        materialised=repo.plan(
            repo.step("a.plan"),
            repo.step(
                "b.compute", depends_on=("a.plan",), branch=repo.BRANCH, loop=repo.LOOP
            ),
        ),
    )
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    scheduler.complete_step(first)
    observe_branch(owned, (("mode", "slow"),))

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("succeeded", "skipped")
    assert runtime_steps[1].attempts == ()


def test_cancelled_run_with_a_loop_step_refuses_nothing_and_claims_nothing(
    owned: m1.Owned,
) -> None:
    scheduler = loop_run(owned)
    force_cancelled_event(owned)
    before = counts(owned)

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None

    assert counts(owned) == before
    assert last_event(owned) == ("workflow_run_cancelled", "cancelled")


def test_workflow_scheduler_completes_a_linear_run_without_replaying_steps(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
        repo.step("c.finish", depends_on=("b.compute",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)

    observed: list[tuple[str, str]] = []
    while True:
        claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
        if claim is None:
            break
        observed.append((claim.workflow_step_id, scheduler.complete_step(claim)))

    assert observed == [
        ("a.plan", "running"),
        ("b.compute", "running"),
        ("c.finish", "succeeded"),
    ]
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == (
        "succeeded",
        "succeeded",
        "succeeded",
    )
    assert owned.connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE workspace_id = ? "
        "AND run_id = ? ORDER BY sequence DESC LIMIT 1",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone() == ("succeeded",)


def test_result_envelope_completion_records_worker_identity(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)
    result = result_for(work_unit)

    assert scheduler.complete_step_with_result(
        claim, work_unit=work_unit, result=result
    ) == "succeeded"

    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.events[-1].details is not None
    assert run.events[-1].details["work_unit_hash"] == work_unit.content_hash
    assert run.events[-1].details["result_digest"] == WORK_RESULT_DIGEST
    assert run.events[-1].details["executor_id"] == "executor.echo"
    assert run.events[-1].details["result_fence"] == claim.fencing_generation
    assert run.steps[0].attempts[0].status == "succeeded"


def test_failure_injection_duplicate_result_after_success_writes_nothing(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)

    assert scheduler.complete_step_with_result(
        claim, work_unit=work_unit, result=result_for(work_unit)
    ) == "succeeded"
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="terminal"):
        scheduler.complete_step_with_result(
            claim, work_unit=work_unit, result=result_for(work_unit)
        )

    assert counts(owned) == before
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.status == "succeeded"
    assert run.steps[0].attempts[0].status == "succeeded"


def test_stale_result_envelope_does_not_complete_the_open_attempt(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)
    before = counts(owned)

    with pytest.raises(ExecutionRefused) as caught:
        scheduler.complete_step_with_result(
            claim,
            work_unit=work_unit,
            result=result_for(work_unit, work_unit_hash="sha256:" + "f" * 64),
        )
    assert caught.value.reason == "wrong_work_unit"

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "running"
    assert runtime_steps[0].attempts[0].status == "running"


def test_mismatched_result_envelope_does_not_complete_the_open_attempt(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)
    before = counts(owned)

    with pytest.raises(ExecutionRefused) as caught:
        scheduler.complete_step_with_result(
            claim,
            work_unit=work_unit,
            result=result_for(work_unit, executor_version="2.0.0", outcome="FAILED"),
        )
    assert caught.value.reason == "stale_executor_identity"

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "running"
    assert runtime_steps[0].attempts[0].status == "running"


def test_failed_result_envelope_fails_the_attempt_step_and_run(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)

    assert scheduler.complete_step_with_result(
        claim, work_unit=work_unit, result=result_for(work_unit, outcome="FAILED")
    ) == "failed"

    assert last_event(owned) == ("workflow_run_failed", "failed")
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.steps[0].status == "failed"
    assert run.steps[0].attempts[0].status == "failed"
    details = run.events[-1].details
    assert details is not None
    assert details["result_outcome"] == "FAILED"
    assert details["work_unit_hash"] == work_unit.content_hash
    assert details["result_digest"] == WORK_RESULT_DIGEST
    assert details["runtime_attempt_id"] == claim.runtime_attempt_id


def test_failed_result_envelope_stops_the_run_from_claiming_more_work(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(
        repo.step("a.plan"),
        repo.step("b.compute", depends_on=("a.plan",)),
    )
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)

    assert scheduler.complete_step_with_result(
        claim, work_unit=work_unit, result=result_for(work_unit, outcome="FAILED")
    ) == "failed"

    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert tuple(step.status for step in runtime_steps) == ("failed", "pending")


RETRYABLE_FAILURE = ApiError(
    code="internal_recoverable",
    message="workflow step failed and may be retried",
    retry_class="retryable",
    retry_after_ms=250,
)
NON_RETRYABLE_FAILURE = ApiError(
    code="invalid_request",
    message="workflow step failed for good",
    retry_class="non_retryable",
)


def retry_scheduler(
    holder: m1.Owned, *, max_attempts: int, now_us: int = BASE_US + 40
) -> WorkflowScheduler:
    return WorkflowScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        FakeClock(wall=datetime.fromtimestamp(now_us / 1_000_000, UTC)),
        retry=WorkflowRetryPolicy(max_attempts=max_attempts),
    )


def one_step_run(owned: m1.Owned) -> MaterialisedWorkflow:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    return materialised


def fail_next_attempt(
    scheduler: WorkflowScheduler,
    materialised: MaterialisedWorkflow,
    *,
    failure: ApiError | None,
) -> tuple[str, WorkflowStepClaim]:
    """Claim the next ready step and complete it with a FAILED result and that failure."""
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)
    status = scheduler.complete_step_with_result(
        claim,
        work_unit=work_unit,
        result=result_for(work_unit, outcome="FAILED"),
        failure=failure,
    )
    return status, claim


def wait_rows(owned: m1.Owned) -> list[tuple[str, str, str | None, str | None]]:
    return cast(
        list[tuple[str, str, str | None, str | None]],
        owned.connection.execute(
            "SELECT w.wait_id, w.kind, r.status, r.resolution_reason "
            "FROM omnivia_runtime_waits w "
            "LEFT JOIN omnivia_runtime_wait_resolutions r "
            "ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id "
            "WHERE w.workspace_id = ? AND w.run_id = ? "
            "ORDER BY w.created_at_us, w.wait_id",
            (WORKSPACE_ID, RUN_ID),
        ).fetchall(),
    )


def test_retryable_failure_opens_one_retry_wait_and_leaves_the_step_waiting(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)

    status, claim = fail_next_attempt(
        scheduler, materialised, failure=RETRYABLE_FAILURE
    )

    assert status == "waiting"
    assert last_event(owned) == ("workflow_step_retry_scheduled", "waiting")
    waits = wait_rows(owned)
    assert len(waits) == 1
    assert waits[0][1] == RETRY_WAIT_KIND
    assert waits[0][2] is None
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.steps[0].status == "waiting"
    assert run.steps[0].attempts[0].status == "failed"
    assert run.steps[0].attempts[0].failure == RETRYABLE_FAILURE
    details = run.events[-1].details
    assert details is not None
    assert details["wait_purpose"] == RETRY_WAIT_PURPOSE
    assert details["wait_checkpoint"] == "attempt:2"
    assert details["next_attempt_number"] == claim.runtime_attempt_number + 1
    assert details["retry_after_ms"] == 250


def test_non_retryable_failure_still_fails_the_step_and_the_run(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=3)

    status, _ = fail_next_attempt(
        scheduler, materialised, failure=NON_RETRYABLE_FAILURE
    )

    assert status == "failed"
    assert last_event(owned) == ("workflow_run_failed", "failed")
    assert wait_rows(owned) == []
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.steps[0].status == "failed"
    assert run.steps[0].attempts[0].failure == NON_RETRYABLE_FAILURE


def test_a_scheduler_with_no_retry_policy_fails_a_retryable_failure(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)

    status, _ = fail_next_attempt(
        workflow_scheduler(owned), materialised, failure=RETRYABLE_FAILURE
    )

    assert status == "failed"
    assert wait_rows(owned) == []


def test_exhausted_retry_budget_fails_the_step_and_the_run(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)
    assert fail_next_attempt(scheduler, materialised, failure=RETRYABLE_FAILURE)[
        0
    ] == "waiting"
    assert scheduler.release_retry_wait(run_id=RUN_ID, workflow_step_id="a.plan") == (
        "running"
    )

    status, claim = fail_next_attempt(
        scheduler, materialised, failure=RETRYABLE_FAILURE
    )

    assert claim.runtime_attempt_number == 2
    assert status == "failed"
    assert last_event(owned) == ("workflow_run_failed", "failed")
    assert len(wait_rows(owned)) == 1
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert run.steps[0].status == "failed"
    assert tuple(attempt.status for attempt in run.steps[0].attempts) == (
        "failed",
        "failed",
    )


def test_releasing_the_retry_wait_makes_the_step_pending_and_claimable(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)
    fail_next_attempt(scheduler, materialised, failure=RETRYABLE_FAILURE)

    assert scheduler.release_retry_wait(run_id=RUN_ID, workflow_step_id="a.plan") == (
        "running"
    )

    assert last_event(owned) == ("workflow_step_retry_ready", "running")
    waits = wait_rows(owned)
    assert waits[0][2] == "resolved"
    assert waits[0][3] == RETRY_WAIT_RESOLUTION_REASON
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "pending"

    second = scheduler.claim_next_ready_step(run_id=RUN_ID)

    assert second is not None
    assert second.runtime_attempt_number == 2
    assert second.runtime_attempt_id != runtime_steps[0].attempts[0].attempt_id
    assert scheduler.complete_step(second) == "succeeded"


def test_failure_injection_late_result_from_retried_attempt_writes_nothing(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert first is not None
    first_work_unit = result_bound_work_unit(first, materialised)
    assert scheduler.complete_step_with_result(
        first,
        work_unit=first_work_unit,
        result=result_for(first_work_unit, outcome="FAILED"),
        failure=RETRYABLE_FAILURE,
    ) == "waiting"
    assert scheduler.release_retry_wait(run_id=RUN_ID, workflow_step_id="a.plan") == (
        "running"
    )
    second = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert second is not None
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="not the open attempt"):
        scheduler.complete_step_with_result(
            first,
            work_unit=first_work_unit,
            result=result_for(first_work_unit),
        )

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "running"
    assert tuple(attempt.status for attempt in runtime_steps[0].attempts) == (
        "failed",
        "running",
    )


def test_restarted_owner_sees_the_retry_wait_without_reopening_it(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    fail_next_attempt(
        retry_scheduler(owned, max_attempts=2), materialised, failure=RETRYABLE_FAILURE
    )
    before = counts(owned)
    waits_before = wait_rows(owned)

    with restarted(owned) as holder:
        resumed = retry_scheduler(holder, max_attempts=2)

        assert resumed.claim_next_ready_step(run_id=RUN_ID) is None
        assert counts(holder) == before
        assert wait_rows(holder) == waits_before

        assert resumed.release_retry_wait(
            run_id=RUN_ID, workflow_step_id="a.plan"
        ) == "running"
        claim = resumed.claim_next_ready_step(run_id=RUN_ID)
        assert claim is not None
        assert claim.runtime_attempt_number == 2
        assert len(wait_rows(holder)) == 1


def test_releasing_the_retry_wait_twice_opens_one_second_attempt(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)
    fail_next_attempt(scheduler, materialised, failure=RETRYABLE_FAILURE)
    assert scheduler.release_retry_wait(run_id=RUN_ID, workflow_step_id="a.plan") == (
        "running"
    )
    released = counts(owned)

    assert scheduler.release_retry_wait(run_id=RUN_ID, workflow_step_id="a.plan") == (
        "running"
    )

    assert counts(owned) == released
    first = scheduler.claim_next_ready_step(run_id=RUN_ID)
    after_claim = counts(owned)
    assert first is not None
    assert scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    assert counts(owned) == after_claim
    run = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert run is not None
    assert len(run.steps[0].attempts) == 2
    assert len(wait_rows(owned)) == 1


def test_completing_a_retried_step_again_does_not_reopen_its_retry_wait(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=3)
    _, claim = fail_next_attempt(scheduler, materialised, failure=RETRYABLE_FAILURE)
    after_retry = counts(owned)
    work_unit = result_bound_work_unit(claim, materialised)

    with pytest.raises(WorkflowSchedulingError):
        scheduler.complete_step_with_result(
            claim,
            work_unit=work_unit,
            result=result_for(work_unit, outcome="FAILED"),
            failure=RETRYABLE_FAILURE,
        )

    assert counts(owned) == after_retry
    assert len(wait_rows(owned)) == 1


def test_a_failure_supplied_with_a_succeeded_result_is_refused(
    owned: m1.Owned,
) -> None:
    materialised = one_step_run(owned)
    scheduler = retry_scheduler(owned, max_attempts=2)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    work_unit = result_bound_work_unit(claim, materialised)
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError):
        scheduler.complete_step_with_result(
            claim,
            work_unit=work_unit,
            result=result_for(work_unit),
            failure=RETRYABLE_FAILURE,
        )

    assert counts(owned) == before


def test_non_effect_claim_cannot_declare_or_settle_effect(
    owned: m1.Owned,
) -> None:
    materialised = repo.plan(repo.step("a.plan"))
    started(owned, materialised=materialised)
    open_steps(owned)
    scheduler = workflow_scheduler(owned)
    claim = scheduler.claim_next_ready_step(run_id=RUN_ID)
    assert claim is not None
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="not an EFFECT-route step"):
        scheduler.declare_effect_intent(claim, effect_intent_for(claim))
    with pytest.raises(WorkflowSchedulingError, match="not an EFFECT-route step"):
        scheduler.settle_effect_step(
            claim,
            effect_intent_id=EFFECT_INTENT_ID,
            effect_settlement_id=EFFECT_SETTLEMENT_ID,
        )
    with pytest.raises(WorkflowSchedulingError, match="not an EFFECT-route step"):
        scheduler.dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert counts(owned) == before
    assert dispatch_count(owned) == 0


def test_effect_step_declares_intent_before_any_completion(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    declared = scheduler.declare_effect_intent(claim, effect_intent_for(claim))

    assert declared.effect_intent_id == EFFECT_INTENT_ID
    assert read_run_effect_intents(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ) == (declared,)
    assert last_event(owned) == ("workflow_effect_intended", "running")
    details = read_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ).events[-1].details
    assert details is not None
    assert details["effect_intent_id"] == EFFECT_INTENT_ID


def test_plain_completion_refuses_effect_step_without_settlement(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="effect settlement"):
        scheduler.complete_step(claim)

    assert counts(owned) == before
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "running"
    assert runtime_steps[0].attempts[0].status == "running"


def test_committed_effect_settlement_succeeds_the_effect_step(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    scheduler.declare_effect_intent(claim, effect_intent_for(claim))
    record_effect_dispatch(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        requested_at_us=EFFECT_DISPATCH_US,
    )
    record_effect_receipt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        receipt=effect_receipt(),
    )

    settled_by_later_scheduler = workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US)
    assert settled_by_later_scheduler.settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == "succeeded"

    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "committed"
    assert settlement.effect_receipt_id == EFFECT_RECEIPT_ID
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "succeeded"
    assert runtime_steps[0].attempts[0].status == "succeeded"


def test_undispatched_effect_settlement_fails_the_effect_step(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    scheduler.declare_effect_intent(claim, effect_intent_for(claim))

    settled_by_later_scheduler = workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US)
    assert settled_by_later_scheduler.settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == "failed"

    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "not_committed"
    assert last_event(owned) == ("workflow_run_failed", "failed")
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "failed"
    assert runtime_steps[0].attempts[0].status == "failed"


def test_dispatched_effect_without_receipt_makes_the_run_uncertain(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    scheduler.declare_effect_intent(claim, effect_intent_for(claim))
    record_effect_dispatch(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        requested_at_us=EFFECT_DISPATCH_US,
    )

    settled_by_later_scheduler = workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US)
    assert settled_by_later_scheduler.settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == RUN_STATUS_UNCERTAIN

    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "unknown"
    assert last_event(owned) == ("workflow_effect_uncertain", "uncertain")
    assert settled_by_later_scheduler.claim_next_ready_step(run_id=RUN_ID) is None
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    # The step is suspended rather than left running: the uncertainty is now a durable
    # `effect_reconciliation` wait a restarted scheduler can see and answer.
    assert runtime_steps[0].status == "waiting"
    assert runtime_steps[0].attempts[0].status == "uncertain"
    waits = wait_rows(owned)
    assert len(waits) == 1
    assert (waits[0][1], waits[0][2]) == (RECONCILIATION_WAIT_KIND, None)
    details = read_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ).events[-1].details
    assert details is not None
    assert details["wait_purpose"] == RECONCILIATION_WAIT_PURPOSE
    assert details["wait_checkpoint"] == f"attempt:{claim.runtime_attempt_number}"
    assert details["expected_resolution"] == WAIT_RESOLUTION_FOR_KIND[
        RECONCILIATION_WAIT_KIND
    ]
    assert details["effect_outcome"] == "unknown"


def declared_effect_step(
    owned: m1.Owned,
) -> tuple[WorkflowScheduler, WorkflowStepClaim, EffectIntent]:
    """A claimed EFFECT step whose durable intent is declared and committed."""
    scheduler, claim = claimed_effect_step(owned)
    return scheduler, claim, scheduler.declare_effect_intent(claim, effect_intent_for(claim))


def test_dispatch_request_is_refused_before_a_durable_intent(owned: m1.Owned) -> None:
    scheduler, claim = claimed_effect_step(owned)
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="without a durable intent"):
        scheduler.dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert dispatch_count(owned) == 0
    assert counts(owned) == before


def test_dispatch_request_is_refused_while_the_caller_holds_a_transaction(
    owned: m1.Owned,
) -> None:
    _, claim, _ = declared_effect_step(owned)
    dispatcher = workflow_scheduler_at(owned, now_us=EFFECT_DISPATCH_US)
    before = counts(owned)

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), pytest.raises(EffectTransactionError, match="outside an open transaction"):
        dispatcher.dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert dispatch_count(owned) == 0
    assert counts(owned) == before


def test_first_dispatch_returns_the_durable_intent_and_counts_one_dispatch(
    owned: m1.Owned,
) -> None:
    _, claim, intent = declared_effect_step(owned)
    dispatcher = workflow_scheduler_at(owned, now_us=EFFECT_DISPATCH_US)

    request = dispatcher.dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert (
        request.workspace_id,
        request.run_id,
        request.effect_intent_id,
        request.capability_id,
        request.capability_grant_id,
        request.effect_kind,
        request.idempotency_key,
        request.request_digest,
        request.dispatch_number,
    ) == (
        WORKSPACE_ID,
        intent.run_id,
        intent.effect_intent_id,
        intent.capability_id,
        intent.capability_grant_id,
        intent.effect_kind,
        intent.idempotency_key,
        intent.request_digest,
        1,
    )
    assert dispatch_count(owned) == 1
    assert last_event(owned) == ("workflow_effect_dispatch_requested", "running")
    details = read_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ).events[-1].details
    assert details is not None
    assert details["dispatch_number"] == 1
    # A published request asserts nothing about delivery: no receipt, no settlement.
    assert (
        read_effect_settlement_for_intent(
            owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
        )
        is None
    )


def test_redelivery_of_the_same_intent_numbers_a_second_dispatch(
    owned: m1.Owned,
) -> None:
    _, claim, _ = declared_effect_step(owned)
    first = workflow_scheduler_at(owned, now_us=EFFECT_DISPATCH_US).dispatch_effect_step(
        claim, effect_intent_id=EFFECT_INTENT_ID
    )

    replayed = workflow_scheduler_at(
        owned, now_us=EFFECT_REDISPATCH_US
    ).dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert replayed.idempotency_key == first.idempotency_key
    assert replayed.request_digest == first.request_digest
    assert (first.dispatch_number, replayed.dispatch_number) == (1, 2)
    assert dispatch_count(owned) == 2


def test_failure_injection_dispatch_outbox_without_event_settles_unknown(
    owned: m1.Owned,
) -> None:
    _, claim, _ = declared_effect_step(owned)
    publish_dispatch_request(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        requested_at_us=EFFECT_DISPATCH_US,
    )
    assert dispatch_count(owned) == 1
    assert last_event(owned) == ("workflow_effect_intended", "running")

    assert workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US).settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == RUN_STATUS_UNCERTAIN

    assert last_event(owned) == ("workflow_effect_uncertain", "uncertain")
    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "unknown"
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "waiting"
    assert runtime_steps[0].attempts[0].status == "uncertain"


def test_dispatch_after_the_effect_is_settled_is_refused(owned: m1.Owned) -> None:
    _, claim, _ = declared_effect_step(owned)
    workflow_scheduler_at(owned, now_us=EFFECT_DISPATCH_US).dispatch_effect_step(
        claim, effect_intent_id=EFFECT_INTENT_ID
    )
    settle_effect_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
        settled_at_us=EFFECT_SETTLED_US,
        audit_ref=m18.audit_ref_for(JOB_ID),
    )

    with pytest.raises(EffectTransactionError, match="already settled"):
        workflow_scheduler_at(
            owned, now_us=EFFECT_REDISPATCH_US
        ).dispatch_effect_step(claim, effect_intent_id=EFFECT_INTENT_ID)

    assert dispatch_count(owned) == 1


def test_dispatch_without_a_receipt_settles_unknown_rather_than_succeeding(
    owned: m1.Owned,
) -> None:
    _, claim, _ = declared_effect_step(owned)
    workflow_scheduler_at(owned, now_us=EFFECT_DISPATCH_US).dispatch_effect_step(
        claim, effect_intent_id=EFFECT_INTENT_ID
    )

    assert workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US).settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == RUN_STATUS_UNCERTAIN

    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "unknown"
    assert settlement.effect_receipt_id is None
    assert last_event(owned) == ("workflow_effect_uncertain", "uncertain")
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].attempts[0].status == "uncertain"


def uncertain_effect_step(
    owned: m1.Owned, *downstream: StepDefinition
) -> tuple[WorkflowScheduler, WorkflowStepClaim]:
    """One dispatched EFFECT step settled `unknown` and suspended for reconciliation."""
    scheduler, claim = claimed_effect_step(owned, *downstream)
    scheduler.declare_effect_intent(claim, effect_intent_for(claim))
    record_effect_dispatch(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        requested_at_us=EFFECT_DISPATCH_US,
    )
    assert workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US).settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == RUN_STATUS_UNCERTAIN
    return scheduler, claim


def record_late_receipt(holder: m1.Owned) -> None:
    """The receipt 0023 retains when it arrives after the effect was already uncertain."""
    record_effect_receipt(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        receipt=effect_receipt(observed_at=rt107.timestamp(EFFECT_LATE_RECEIPT_US)),
    )


def reconcile(
    holder: m1.Owned,
    *,
    workflow_step_id: str = "c.write",
    effect_intent_id: str = EFFECT_INTENT_ID,
    reconciliation_id: str = EFFECT_RECONCILIATION_ID,
    now_us: int = EFFECT_RECONCILED_US,
) -> str:
    return cast(
        str,
        workflow_scheduler_at(holder, now_us=now_us).reconcile_effect_step(
            run_id=RUN_ID,
            workflow_step_id=workflow_step_id,
            effect_intent_id=effect_intent_id,
            effect_reconciliation_id=reconciliation_id,
        ),
    )


def reconciliations(holder: m1.Owned) -> int:
    return cast(
        int,
        holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_effect_reconciliations "
            "WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0],
    )


def test_late_receipt_reconciles_the_uncertain_effect_and_succeeds_the_step(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    record_late_receipt(owned)

    assert reconcile(owned) == "succeeded"

    stored = read_effect_reconciliation_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert stored is not None
    assert (stored.outcome, stored.effect_settlement_id, stored.effect_receipt_id) == (
        "committed",
        EFFECT_SETTLEMENT_ID,
        EFFECT_RECEIPT_ID,
    )
    # The settlement it answers is untouched: the effect *was* uncertain, and 0024 records
    # the late answer beside that fact rather than amending it away.
    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "unknown"
    assert last_event(owned) == ("workflow_run_succeeded", "succeeded")
    waits = wait_rows(owned)
    assert len(waits) == 1
    assert (waits[0][2], waits[0][3]) == (
        "resolved",
        RECONCILIATION_WAIT_RESOLUTION_REASON,
    )
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "succeeded"
    # The attempt stays uncertain: it really did end without an answer, and the step
    # succeeding later does not make that untrue.
    assert runtime_steps[0].attempts[0].status == "uncertain"


def test_reconciliation_without_a_retained_receipt_keeps_the_step_suspended(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    before = counts(owned)

    with pytest.raises(EffectTransactionError, match="no receipt is retained"):
        reconcile(owned)

    assert counts(owned) == before
    assert reconciliations(owned) == 0
    assert last_event(owned) == ("workflow_effect_uncertain", "uncertain")
    waits = wait_rows(owned)
    assert (waits[0][1], waits[0][2]) == (RECONCILIATION_WAIT_KIND, None)
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "waiting"


def test_reconciling_the_same_uncertain_effect_again_answers_it_once(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned, repo.step("b.compute", depends_on=("c.write",)))
    record_late_receipt(owned)

    assert reconcile(owned) == "running"
    before = counts(owned)

    assert reconcile(owned) == "running"
    with pytest.raises(WorkflowSchedulingError, match="already reconciled"):
        reconcile(owned, reconciliation_id="rec-workflow-0002")

    assert counts(owned) == before
    assert reconciliations(owned) == 1


def test_reconciliation_refuses_an_effect_of_another_step(owned: m1.Owned) -> None:
    uncertain_effect_step(
        owned, repo.step("e.write", execution_class=EXECUTION_CLASS_EFFECT)
    )
    later = workflow_scheduler_at(owned, now_us=EFFECT_LATE_RECEIPT_US)
    other = later.claim_next_ready_step(run_id=RUN_ID)
    assert other is not None
    assert other.workflow_step_id == "e.write"
    later.declare_effect_intent(
        other,
        effect_intent_for(
            other,
            effect_intent_id="eff-workflow-0002",
            idempotency_key="workflow-effect-0002",
            declared_at=rt107.timestamp(EFFECT_LATE_RECEIPT_US),
        ),
    )
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="does not name the step and attempt"):
        reconcile(owned, effect_intent_id="eff-workflow-0002")
    with pytest.raises(WorkflowSchedulingError, match="without an effect intent"):
        reconcile(owned, effect_intent_id="eff-workflow-absent")

    assert counts(owned) == before
    assert reconciliations(owned) == 0


def test_restarted_owner_sees_the_reconciliation_wait_and_answers_it(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    record_late_receipt(owned)
    before = counts(owned)

    with restarted(owned) as holder:
        assert reconcile(holder) == "succeeded"

        # One wait, one resolution: the restart neither reopened the suspension nor
        # needed the claim of the scheduler that opened it.
        assert counts(holder) == (before[0], before[1] + 1, before[2] + 1, before[3] + 1)
        assert reconciliations(holder) == 1
        runtime_steps = read_run_steps(
            holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert runtime_steps[0].status == "succeeded"


def test_replay_after_rt206_recorded_reconciliation_closes_the_wait_once(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    record_late_receipt(owned)
    recorded = reconcile_effect_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_reconciliation_id=EFFECT_RECONCILIATION_ID,
        reconciled_at_us=EFFECT_RECONCILED_US,
        audit_ref=m18.audit_ref_for(JOB_ID),
    )
    assert recorded.outcome == "committed"
    before_scheduler_replay = counts(owned)

    assert reconcile(owned, now_us=EFFECT_RECONCILED_US + 1_000) == "succeeded"

    assert reconciliations(owned) == 1
    assert counts(owned) == (
        before_scheduler_replay[0],
        before_scheduler_replay[1] + 1,
        before_scheduler_replay[2] + 1,
        before_scheduler_replay[3] + 1,
    )
    waits = wait_rows(owned)
    assert len(waits) == 1
    assert (waits[0][2], waits[0][3]) == (
        "resolved",
        RECONCILIATION_WAIT_RESOLUTION_REASON,
    )
    runtime_steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert runtime_steps[0].status == "succeeded"
    after_scheduler_replay = counts(owned)

    assert reconcile(owned, now_us=EFFECT_RECONCILED_US + 2_000) == "succeeded"

    assert counts(owned) == after_scheduler_replay
    assert reconciliations(owned) == 1


def test_reconciling_a_step_that_is_not_an_effect_step_is_refused(
    owned: m1.Owned,
) -> None:
    started(owned, materialised=repo.plan(repo.step("a.plan")))
    open_steps(owned)
    before = counts(owned)

    with pytest.raises(WorkflowSchedulingError, match="no EFFECT-route workflow step"):
        reconcile(owned, workflow_step_id="a.plan")

    assert counts(owned) == before


def test_reconciling_an_effect_step_that_is_not_suspended_is_refused(
    owned: m1.Owned,
) -> None:
    claimed_effect_step(owned)
    before = counts(owned)

    with pytest.raises(
        WorkflowSchedulingError, match="not suspended on an effect reconciliation wait"
    ):
        reconcile(owned)

    assert counts(owned) == before
    assert reconciliations(owned) == 0


def test_terminal_run_reconciles_no_uncertain_effect(owned: m1.Owned) -> None:
    uncertain_effect_step(owned)
    record_late_receipt(owned)
    force_cancelled_event(owned, at_us=EFFECT_LATE_RECEIPT_US + 100)
    before = counts(owned)

    assert reconcile(owned) == RUN_STATUS_CANCELLED

    assert counts(owned) == before
    assert reconciliations(owned) == 0


def test_governed_compensation_records_own_lineage_without_rewriting_truth(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    before = counts(owned)
    scheduler = governed_scheduler_at(owned, now_us=COMPENSATION_RECORDED_US)

    assert scheduler.record_compensation(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        compensation_id=COMPENSATION_ID,
        authority=governed_authority(),
        outcome="committed",
        result_digest=WORK_RESULT_DIGEST,
    ) == RUN_STATUS_UNCERTAIN

    assert counts(owned) == (before[0], before[1], before[2], before[3] + 2)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot.status == RUN_STATUS_UNCERTAIN
    assert snapshot.steps[0].status == "waiting"
    assert snapshot.steps[0].attempts[0].status == "uncertain"
    started, settled = snapshot.events[-2:]
    assert (started.event_kind, settled.event_kind) == (
        COMPENSATION_STARTED_EVENT,
        COMPENSATION_SETTLED_EVENT,
    )
    assert started.runtime_event_id != settled.runtime_event_id
    assert started.details is not None
    assert settled.details is not None
    assert started.details["compensation_id"] == COMPENSATION_ID
    assert settled.details["compensation_outcome"] == "committed"
    assert settled.details["result_digest"] == WORK_RESULT_DIGEST


def test_governed_compensation_replays_identical_command_and_refuses_conflict(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    scheduler = governed_scheduler_at(owned, now_us=COMPENSATION_RECORDED_US)
    scheduler.record_compensation(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        compensation_id=COMPENSATION_ID,
        authority=governed_authority(),
        outcome="unknown",
        result_digest=WORK_RESULT_DIGEST,
    )
    after_first = counts(owned)

    scheduler.record_compensation(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        compensation_id=COMPENSATION_ID,
        authority=governed_authority(),
        outcome="unknown",
        result_digest=WORK_RESULT_DIGEST,
    )
    with pytest.raises(WorkflowSchedulingError, match="different governed request"):
        scheduler.record_compensation(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            compensation_id=COMPENSATION_ID,
            authority=governed_authority(),
            outcome="committed",
            result_digest=WORK_RESULT_DIGEST,
        )

    assert counts(owned) == after_first


def test_governed_compensation_without_authority_fails_closed(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    before = counts(owned)
    scheduler = governed_scheduler_at(
        owned, now_us=COMPENSATION_RECORDED_US, compensation_roles=frozenset()
    )

    with pytest.raises(WorkflowGovernanceDenied, match="may not order"):
        scheduler.record_compensation(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            compensation_id=COMPENSATION_ID,
            authority=governed_authority(),
            outcome="committed",
            result_digest=WORK_RESULT_DIGEST,
        )

    assert counts(owned) == before


def test_governed_repair_records_forward_evidence_only(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    before = counts(owned)
    scheduler = governed_scheduler_at(owned, now_us=REPAIR_RECORDED_US)

    assert scheduler.record_governed_repair(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        repair_id=REPAIR_ID,
        authority=governed_authority(),
        evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
        evidence_checksum=WORK_RESULT_DIGEST,
        summary="reviewer confirmed external repair plan for unresolved effect",
    ) == RUN_STATUS_UNCERTAIN

    assert counts(owned) == (before[0], before[1], before[2], before[3] + 1)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot.status == RUN_STATUS_UNCERTAIN
    assert snapshot.steps[0].status == "waiting"
    assert snapshot.steps[0].attempts[0].status == "uncertain"
    settlement = read_effect_settlement_for_intent(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=EFFECT_INTENT_ID
    )
    assert settlement is not None
    assert settlement.outcome == "unknown"
    assert len(snapshot.evidence) == 1
    evidence = snapshot.evidence[0]
    assert evidence.evidence_kind == GOVERNED_REPAIR_EVIDENCE_KIND
    assert evidence.source.source_kind == GOVERNED_REPAIR_EVIDENCE_SOURCE
    assert evidence.source.source_id == REPAIR_EVIDENCE_SOURCE_ID
    assert evidence.content_checksum == WORK_RESULT_DIGEST
    assert evidence.authoritative is False
    assert evidence.retained is True
    event = snapshot.events[-1]
    assert event.event_kind == GOVERNED_REPAIR_EVENT
    assert event.details is not None
    assert event.details["repair_id"] == REPAIR_ID
    assert event.details["evidence_item_id"] == evidence.evidence_item_id


def test_governed_repair_replays_identical_command_and_refuses_conflict(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    scheduler = governed_scheduler_at(owned, now_us=REPAIR_RECORDED_US)
    scheduler.record_governed_repair(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        repair_id=REPAIR_ID,
        authority=governed_authority(),
        evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
        evidence_checksum=WORK_RESULT_DIGEST,
        summary="reviewer confirmed external repair plan for unresolved effect",
    )
    after_first = counts(owned)

    scheduler.record_governed_repair(
        run_id=RUN_ID,
        workflow_step_id="c.write",
        repair_id=REPAIR_ID,
        authority=governed_authority(),
        evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
        evidence_checksum=WORK_RESULT_DIGEST,
        summary="reviewer confirmed external repair plan for unresolved effect",
    )
    with pytest.raises(WorkflowSchedulingError, match="different evidence"):
        scheduler.record_governed_repair(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            repair_id=REPAIR_ID,
            authority=governed_authority(),
            evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
            evidence_checksum="sha256:" + "f" * 64,
            summary="reviewer confirmed external repair plan for unresolved effect",
        )
    with pytest.raises(WorkflowSchedulingError, match="different governed request"):
        scheduler.record_governed_repair(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            repair_id=REPAIR_ID,
            authority=governed_authority(),
            evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
            evidence_checksum=WORK_RESULT_DIGEST,
            summary="different repair summary",
        )

    assert counts(owned) == after_first


def test_governed_repair_without_authority_fails_closed(
    owned: m1.Owned,
) -> None:
    uncertain_effect_step(owned)
    before = counts(owned)
    scheduler = governed_scheduler_at(
        owned, now_us=REPAIR_RECORDED_US, repair_roles=frozenset()
    )

    with pytest.raises(WorkflowGovernanceDenied, match="may not record"):
        scheduler.record_governed_repair(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            repair_id=REPAIR_ID,
            authority=governed_authority(),
            evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
            evidence_checksum=WORK_RESULT_DIGEST,
            summary="reviewer confirmed external repair plan for unresolved effect",
        )

    assert counts(owned) == before


def test_governed_repair_refuses_to_rewrite_terminal_run_truth(
    owned: m1.Owned,
) -> None:
    scheduler, claim = claimed_effect_step(owned)
    scheduler.declare_effect_intent(claim, effect_intent_for(claim))
    record_effect_dispatch(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        effect_intent_id=EFFECT_INTENT_ID,
        requested_at_us=EFFECT_DISPATCH_US,
    )
    record_effect_receipt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        receipt=effect_receipt(),
    )
    assert workflow_scheduler_at(owned, now_us=EFFECT_SETTLED_US).settle_effect_step(
        claim,
        effect_intent_id=EFFECT_INTENT_ID,
        effect_settlement_id=EFFECT_SETTLEMENT_ID,
    ) == "succeeded"
    before = counts(owned)
    governed = governed_scheduler_at(owned, now_us=REPAIR_RECORDED_US)

    with pytest.raises(WorkflowSchedulingError, match="terminal"):
        governed.record_governed_repair(
            run_id=RUN_ID,
            workflow_step_id="c.write",
            repair_id=REPAIR_ID,
            authority=governed_authority(),
            evidence_source_id=REPAIR_EVIDENCE_SOURCE_ID,
            evidence_checksum=WORK_RESULT_DIGEST,
            summary="attempt to rewrite terminal success",
        )

    assert counts(owned) == before
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot.status == "succeeded"
    assert snapshot.steps[0].status == "succeeded"
