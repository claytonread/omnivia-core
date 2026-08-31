"""CP-04A: the live Workflow application dependency over durable Core truth.

The seam suite beside this one proves `WorkflowHandlers` delegates and fails closed.
This one holds the object it delegates *to* -- `WorkflowApplicationRuntime` -- to the
only property that makes a live binding worth having: every answer it gives is read
off stored rows, and everything else is a refusal.

So the durable fixture here is the real one. A sealed M2 plan, a seeded durable job
and idempotency claim, a canonical runtime run admitted against them, the 0027
workflow binding, and the run's recorded policy and budget decision. Nothing is
stubbed, and the negative cases are built by *removing* one of those facts rather
than by mocking a failure.

`workflow.start` is proved to refuse and to write nothing, and -- this is the part
that makes the refusal evidence rather than an excuse -- to move through the questions
in order. With no configured decision authority it refuses there. With a coherent
decision authority it reaches the next missing C3 seam: composing run allocation,
durable job metadata, Runtime admission, Workflow plan binding and decision persistence
into one fenced mutation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt202_policy_budget_snapshot_repository as r202
import test_v06_5_s0_mutation_foundation as s0
import test_workflow_runs_repository as repo
import test_workflow_runtime_scheduler as ws
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.service.application import workflow_family_session
from omnivia_core_runtime.service.authorization import (
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.handlers.workflow import (
    WORKFLOW_CONTROL_OPERATION,
    WORKFLOW_INSPECT_OPERATION,
    WORKFLOW_REVIEW_OPERATION,
    WORKFLOW_START_OPERATION,
    WorkflowHandlers,
)
from omnivia_core_runtime.service.mutation import MUTATION_ROLES, issue_mutation_grant
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workflow_policy import (
    AUTHORITY_FILENAME,
    PolicySource,
)
from omnivia_core_runtime.service.workflow_runtime import (
    CONTROL_DISPOSITION_UNSUPPORTED,
    WorkflowApplicationRuntime,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.storage.agent_runtime import RunAdmission, admit_run
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runs import read_workflow_plan

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_STALE_PROJECTION,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    RunDefinitionRef,
    WorkflowControlResult,
    WorkflowInspectResult,
    WorkflowReviewResult,
    WorkflowStartResult,
    get_operation_metadata,
    validate_run,
)

WORKSPACE_ID = repo.WORKSPACE_ID
RUN_ID = repo.RUN_ID
PRINCIPAL = "principal-cp04a-live"
INSTALLATION_ID = "installation-cp04a-live"

#: The purposes the command surface declares, keyed the way the seam suite keys them.
PURPOSES = {
    WORKFLOW_START_OPERATION: "workflow_run",
    WORKFLOW_INSPECT_OPERATION: "workflow_observation",
    WORKFLOW_CONTROL_OPERATION: "workflow_control",
    WORKFLOW_REVIEW_OPERATION: "workflow_review",
}

#: The agent-component run admitted beside the Workflow Run, so "not a Workflow Run"
#: is tested against a run that genuinely exists rather than against an absent one.
AGENT_JOB_ID = "job-run-0002"
AGENT_RUN_ID = "run-0002"

#: Both decisions are pinned after the run's admission instant, because 0021 refuses a
#: snapshot pinned before the run it belongs to and `repo.BASE_US` is one millisecond
#: past the instant `r202.pinned_at()` derives from.
DECISION_OFFSET_MS = 10


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def record_decisions(holder: m1.Owned, run_id: str = RUN_ID) -> None:
    """The policy and budget a canonical `Run` states, as stored rows."""
    r202.add_policy(
        holder,
        r202.policy(
            run_id=run_id,
            policy_snapshot_id=f"policy-{run_id}",
            pinned_at=r202.pinned_at(DECISION_OFFSET_MS),
        ),
    )
    r202.add_budget(
        holder,
        r202.budget(
            run_id=run_id,
            budget_snapshot_id=f"budget-{run_id}",
            pinned_at=r202.pinned_at(DECISION_OFFSET_MS),
        ),
    )


def admit_agent_run(holder: m1.Owned) -> None:
    """A second canonical run that is not a Workflow Run and never binds a plan."""
    m18.seed_job(holder, job_id=AGENT_JOB_ID)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=RunAdmission(
            run_id=AGENT_RUN_ID,
            job_id=AGENT_JOB_ID,
            claim_id=m18.claim_id_for(AGENT_JOB_ID),
            definition=RunDefinitionRef(
                definition_kind="agent_component",
                definition_id="component.echo",
                definition_version="1.0.0",
            ),
            logical_key=m18.logical_key_for(AGENT_JOB_ID),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(AGENT_JOB_ID),
            admitted_at_us=repo.BASE_US,
            runtime_event_id="evt-agent-admitted",
            message="agent run admitted",
        ),
    )
    record_decisions(holder, run_id=AGENT_RUN_ID)


WORKFLOW_BINDING = ServiceBinding(
    installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
)


def projection_version_for(run_id: str = RUN_ID, sequence: int = 0) -> str:
    return f"core.workflow.run.{run_id}.sequence.{sequence}"


def session_for_workflow() -> Any:
    return workflow_family_session(
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )


def context_for(
    operation: str,
    payload: Mapping[str, Any],
    *,
    authorized: bool = False,
    idempotency_key: str | None = None,
    request_id: str | None = None,
) -> OperationContext:
    """One authorized-shape context carrying a real request payload."""
    entry = get_operation_metadata(operation)
    required = entry.required_capability
    request = RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id=f"req-{operation}",
            correlation_id=f"cor-{operation}",
            trace_id=f"trc-{operation}",
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id="cp04a-live-client", version="0.1.0"),
            workspace_id=WORKSPACE_ID,
            scopes=tuple(entry.scope.required_scopes),
            purpose=PURPOSES[operation],
            idempotency_key=(
                idempotency_key or f"idm-{operation}"
                if entry.idempotency.supports_idempotency_key
                else None
            ),
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=True,
                ),
            ),
        ),
        input=dict(payload),
    )
    if request_id is not None:
        request = RequestEnvelope(
            operation=request.operation,
            metadata=RequestMetadata(
                request_id=request_id,
                correlation_id=request.metadata.correlation_id,
                trace_id=request.metadata.trace_id,
                api_version=request.metadata.api_version,
                client=request.metadata.client,
                workspace_id=request.metadata.workspace_id,
                deadline_ms=request.metadata.deadline_ms,
                scopes=request.metadata.scopes,
                purpose=request.metadata.purpose,
                idempotency_key=request.metadata.idempotency_key,
                mutation_precondition=request.metadata.mutation_precondition,
                required_capabilities=request.metadata.required_capabilities,
                principal_claim=request.metadata.principal_claim,
            ),
            input=request.input,
        )
    session = session_for_workflow()
    authorization = (
        authorize_application_request(
            request,
            session=session,
            binding=WORKFLOW_BINDING,
            supported_capabilities=s0.SUPPORTED,
        )
        if authorized
        else None
    )
    return OperationContext(
        request=request,
        principal=PRINCIPAL if authorization is None else authorization.principal_id,
        workspace_id=WORKSPACE_ID,
        granted_operations=(
            frozenset({operation}) if authorization is None else session.operations
        ),
        service=None,
        authority=None if authorization is None else authorization.authority,
        scopes=None if authorization is None else authorization.scopes,
        purpose=None if authorization is None else authorization.purpose,
        authorization=authorization,
    )


def decision_sources() -> tuple[PolicySource, ...]:
    """A coherent configured authority: enough for C2, not a durable admission."""
    return (
        PolicySource(
            kind="platform_safety_boundary",
            source_id="platform-default",
            allowed_capabilities=("memory.read", "memory.write", "workflow.run"),
            offered_capabilities=(
                "memory.read",
                "memory.write",
                "tools.execute",
                "workflow.run",
            ),
            max_cost_units=1_000,
            max_token_units=200_000,
            max_wall_clock_ms=600_000,
            side_effects_allowed=True,
        ),
        PolicySource(
            kind="workspace",
            source_id="workspace-standard",
            allowed_capabilities=("memory.read", "workflow.run"),
            required_capabilities=("workflow.run",),
            max_cost_units=250,
            max_token_units=50_000,
            side_effects_allowed=False,
        ),
    )


def runtime_for(
    holder: m1.Owned | None,
    *,
    sources: tuple[PolicySource, ...] | None = None,
    application_authority: bool = False,
    clock: Any | None = None,
) -> WorkflowApplicationRuntime:
    service = SimpleNamespace(
        connection=None if holder is None else holder.connection,
        identity=None if holder is None else holder.identity,
        workflow_decision_authority=sources,
    )
    return WorkflowApplicationRuntime(
        service=service,
        session=session_for_workflow() if application_authority else None,
        binding=WORKFLOW_BINDING if application_authority else None,
        clock=(clock or s0.clock_at()) if application_authority else None,
    )


def inspect(holder: m1.Owned, **payload: Any) -> Mapping[str, Any]:
    fields: dict[str, Any] = {"run_id": RUN_ID}
    fields.update(payload)
    return runtime_for(holder).workflow_inspect(
        context_for(WORKFLOW_INSPECT_OPERATION, fields)
    )


def start(
    holder: m1.Owned,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str = "idm-workflow.start",
    request_id: str = "req-workflow.start",
) -> Mapping[str, Any]:
    operation_input = {
        "definition_id": repo.WORKFLOW_ID,
        "definition_version": repo.WORKFLOW_VERSION,
    }
    if payload is not None:
        operation_input.update(payload)
    return runtime_for(
        holder, sources=decision_sources(), application_authority=True
    ).workflow_start(
        context_for(
            WORKFLOW_START_OPERATION,
            operation_input,
            authorized=True,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )


def review(holder: m1.Owned | None, **payload: Any) -> Mapping[str, Any]:
    fields: dict[str, Any] = {"run_id": RUN_ID}
    fields.update(payload)
    return runtime_for(holder).workflow_review(
        context_for(WORKFLOW_REVIEW_OPERATION, fields)
    )


def control(holder: m1.Owned | None, **payload: Any) -> Mapping[str, Any]:
    fields: dict[str, Any] = {"run_id": RUN_ID, "action": "cancel"}
    fields.update(payload)
    return runtime_for(holder, application_authority=True).workflow_control(
        context_for(WORKFLOW_CONTROL_OPERATION, fields, authorized=True)
    )


def refusal(holder: m1.Owned, **payload: Any) -> OperationError:
    with pytest.raises(OperationError) as raised:
        inspect(holder, **payload)
    return raised.value


def refused(call: Any, holder: m1.Owned | None, **payload: Any) -> OperationError:
    with pytest.raises(OperationError) as raised:
        call(holder, **payload)
    return raised.value


def row_count(holder: m1.Owned, table: str) -> int:
    row = holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


# --- inspect over durable truth ---------------------------------------------------


def test_inspect_projects_the_stored_run_as_a_canonical_run(owned: m1.Owned) -> None:
    """Every field of the answer is a stored fact, and the answer is a valid `Run`."""
    ws.started(owned)
    record_decisions(owned)

    wire = inspect(owned)

    result = WorkflowInspectResult.from_wire(wire)
    validate_run(result.run, workspace_id=WORKSPACE_ID)
    assert result.run.run_id == RUN_ID
    assert result.run.workspace_id == WORKSPACE_ID
    assert result.run.definition == RunDefinitionRef(
        definition_kind="workflow",
        definition_id=repo.WORKFLOW_ID,
        definition_version=repo.WORKFLOW_VERSION,
    )
    assert result.run.logical_key == m18.logical_key_for(ws.JOB_ID)
    assert result.run.audit_reference == m18.audit_ref_for(ws.JOB_ID)
    assert result.run.originating_operation == "runtime.admit"
    assert result.run.policy.policy_snapshot_id == f"policy-{RUN_ID}"
    assert result.run.budget.budget_snapshot_id == f"budget-{RUN_ID}"
    # The run was admitted and nothing has executed, so the history is empty rather
    # than filled in: an absent step is an answer here, not a gap to be papered over.
    assert result.run.steps == ()
    assert result.run.waits == ()
    assert len(result.run.events) == 1
    assert result.projection_version == projection_version_for()
    assert wire["projection_version"] == projection_version_for()


def test_inspect_reports_the_status_the_event_stream_states(owned: m1.Owned) -> None:
    """`status` is derived from stored events, never chosen by the projection."""
    ws.started(owned)
    record_decisions(owned)
    stored = ws.read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert stored is not None

    result = WorkflowInspectResult.from_wire(inspect(owned))

    assert result.run.status == stored.status
    assert result.run.updated_at == stored.updated_at
    assert result.run.finished_at == stored.finished_at


# --- fail closed -------------------------------------------------------------------


def test_a_run_without_recorded_decisions_is_refused_not_defaulted(
    owned: m1.Owned,
) -> None:
    """The one case a fabricated answer would be easiest: no policy, no budget."""
    ws.started(owned)

    error = refusal(owned)

    assert error.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert "policy and budget" in error.message


@pytest.mark.parametrize("missing", ["policy", "budget"])
def test_half_a_decision_is_still_no_decision(owned: m1.Owned, missing: str) -> None:
    """A run that recorded one of the two cannot borrow a default for the other."""
    ws.started(owned)
    if missing == "budget":
        r202.add_policy(
            owned, r202.policy(pinned_at=r202.pinned_at(DECISION_OFFSET_MS))
        )
    else:
        r202.add_budget(
            owned, r202.budget(pinned_at=r202.pinned_at(DECISION_OFFSET_MS))
        )

    assert refusal(owned).code == ERROR_CODE_DEPENDENCY_UNAVAILABLE


def test_an_unknown_run_is_not_found(owned: m1.Owned) -> None:
    ws.started(owned)
    record_decisions(owned)

    assert refusal(owned, run_id="run-nobody-admitted").code == ERROR_CODE_NOT_FOUND


def test_a_run_that_is_not_a_workflow_run_is_not_found(owned: m1.Owned) -> None:
    """An agent-component run is complete durable truth, and still not this family's.

    It has a policy, a budget and an event stream, so the only thing standing between
    it and a served answer is the 0027 binding it does not have.
    """
    ws.started(owned)
    record_decisions(owned)
    admit_agent_run(owned)

    assert refusal(owned, run_id=AGENT_RUN_ID).code == ERROR_CODE_NOT_FOUND


def test_a_prior_projection_version_for_the_same_run_is_served(
    owned: m1.Owned,
) -> None:
    """Reconnect can pin a Core-issued cursor without falling back to memory."""
    ws.started(owned)
    record_decisions(owned)

    first = WorkflowInspectResult.from_wire(inspect(owned))
    second = WorkflowInspectResult.from_wire(
        inspect(owned, projection_version=first.projection_version)
    )

    assert second.run == first.run
    assert second.projection_version == first.projection_version


@pytest.mark.parametrize(
    "projection_version",
    [
        "v1",
        projection_version_for("run-other", 0),
        projection_version_for(sequence=999),
    ],
)
def test_a_stale_or_wrong_projection_version_is_refused(
    owned: m1.Owned, projection_version: str
) -> None:
    """A pinned read may resume only from a cursor Core could have issued here."""
    ws.started(owned)
    record_decisions(owned)

    error = refusal(owned, projection_version=projection_version)

    assert error.code == ERROR_CODE_STALE_PROJECTION
    assert "projection version" in error.message


@pytest.mark.parametrize("payload", [{}, {"run_id": 7}, {"projection_version": "v1"}])
def test_an_undecodable_payload_is_an_invalid_request(
    owned: m1.Owned, payload: dict[str, Any]
) -> None:
    ws.started(owned)
    record_decisions(owned)
    context = context_for(WORKFLOW_INSPECT_OPERATION, payload)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned).workflow_inspect(context)

    assert raised.value.code == ERROR_CODE_INVALID_REQUEST


def test_a_runtime_without_storage_refuses_before_reading_anything() -> None:
    """The runtime is bound before startup acquires a workspace, and says so."""
    with pytest.raises(OperationError) as raised:
        runtime_for(None).workflow_inspect(
            context_for(WORKFLOW_INSPECT_OPERATION, {"run_id": RUN_ID})
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE


# --- start refuses, and writes nothing ---------------------------------------------


def test_start_without_configured_decision_authority_refuses_there(
    owned: m1.Owned,
) -> None:
    ws.started(owned)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned, application_authority=True).workflow_start(
            context_for(
                WORKFLOW_START_OPERATION,
                {"definition_id": repo.WORKFLOW_ID, "definition_version": "1.0.0"},
                authorized=True,
            )
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    message = raised.value.message
    assert "decision authority configured" in message
    assert "will not invent" in message


def test_start_without_dispatcher_owned_application_authority_refuses_there(
    owned: m1.Owned,
) -> None:
    """The C3 admission seam may not reconstruct grant authority from metadata."""
    ws.started(owned)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned, sources=decision_sources()).workflow_start(
            context_for(
                WORKFLOW_START_OPERATION,
                {"definition_id": repo.WORKFLOW_ID, "definition_version": "1.0.0"},
                authorized=True,
            )
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    message = raised.value.message
    assert "dispatcher-owned application" in message
    assert "will not reconstruct authority" in message


def test_start_without_authorized_context_refuses_before_grant(
    owned: m1.Owned,
) -> None:
    """The handler must receive the dispatcher pass-through authorization."""
    ws.started(owned)

    with pytest.raises(OperationError) as raised:
        runtime_for(
            owned, sources=decision_sources(), application_authority=True
        ).workflow_start(
            context_for(
                WORKFLOW_START_OPERATION,
                {"definition_id": repo.WORKFLOW_ID, "definition_version": "1.0.0"},
            )
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert "dispatcher-owned application" in raised.value.message


def test_start_with_coherent_decision_admits_a_canonical_workflow_run(
    owned: m1.Owned,
) -> None:
    ws.started(owned)
    before = {
        table: row_count(owned, table)
        for table in (
            "omnivia_durable_jobs",
            "omnivia_job_application_metadata",
            "omnivia_runtime_runs",
            "omnivia_workflow_runs",
            "omnivia_runtime_events",
            "omnivia_idempotency_claims",
            "omnivia_runtime_policy_snapshots",
            "omnivia_runtime_budget_snapshots",
            "omnivia_runtime_capability_grants",
        )
    }

    wire = start(owned)

    result = WorkflowStartResult.from_wire(wire)
    validate_run(result.run, workspace_id=WORKSPACE_ID)
    assert result.admission == "created"
    assert result.run.run_id != RUN_ID
    assert result.run.definition == RunDefinitionRef(
        definition_kind="workflow",
        definition_id=repo.WORKFLOW_ID,
        definition_version=repo.WORKFLOW_VERSION,
    )
    assert result.run.originating_operation == WORKFLOW_START_OPERATION
    assert result.run.logical_key == "idm-workflow.start"
    assert result.run.audit_reference.startswith("aud-")
    assert result.run.policy.run_id == result.run.run_id
    assert result.run.budget.run_id == result.run.run_id
    assert "workflow.run" in {
        grant.capability_id for grant in result.run.capability_grants
    }
    assert len(result.run.events) == 1
    assert result.run.events[0].event_kind == "run_admitted"

    inspected = WorkflowInspectResult.from_wire(
        inspect(owned, run_id=result.run.run_id)
    )
    assert inspected.run == result.run
    assert row_count(owned, "omnivia_durable_jobs") == before["omnivia_durable_jobs"] + 1
    assert (
        row_count(owned, "omnivia_job_application_metadata")
        == before["omnivia_job_application_metadata"] + 1
    )
    assert row_count(owned, "omnivia_runtime_runs") == before["omnivia_runtime_runs"] + 1
    assert row_count(owned, "omnivia_workflow_runs") == before["omnivia_workflow_runs"] + 1
    assert (
        row_count(owned, "omnivia_idempotency_claims")
        == before["omnivia_idempotency_claims"] + 1
    )
    assert (
        row_count(owned, "omnivia_runtime_policy_snapshots")
        == before["omnivia_runtime_policy_snapshots"] + 1
    )
    assert (
        row_count(owned, "omnivia_runtime_budget_snapshots")
        == before["omnivia_runtime_budget_snapshots"] + 1
    )
    assert (
        row_count(owned, "omnivia_runtime_capability_grants")
        == before["omnivia_runtime_capability_grants"]
        + len(result.run.capability_grants)
    )


def test_start_with_microsecond_wall_clock_pins_decisions_after_admission(
    owned: m1.Owned,
) -> None:
    """Production wall clocks are not millisecond-aligned; canonical timestamps are."""
    ws.started(owned)
    clock = s0.clock_at(wall=s0.WALL_BASE.replace(microsecond=123456))
    runtime = runtime_for(
        owned,
        sources=decision_sources(),
        application_authority=True,
        clock=clock,
    )

    wire = runtime.workflow_start(
        context_for(
            WORKFLOW_START_OPERATION,
            {
                "definition_id": repo.WORKFLOW_ID,
                "definition_version": repo.WORKFLOW_VERSION,
            },
            authorized=True,
            idempotency_key="idm-workflow-start-microsecond",
            request_id="req-workflow-start-microsecond",
        )
    )

    result = WorkflowStartResult.from_wire(wire)
    assert result.run.policy.pinned_at >= result.run.created_at
    assert result.run.budget.pinned_at >= result.run.created_at


def test_start_replay_returns_the_same_run_without_duplicate_admission(
    owned: m1.Owned,
) -> None:
    ws.started(owned)
    first = WorkflowStartResult.from_wire(start(owned, request_id="req-start-first"))
    before = {
        table: row_count(owned, table)
        for table in (
            "omnivia_durable_jobs",
            "omnivia_runtime_runs",
            "omnivia_workflow_runs",
            "omnivia_idempotency_claims",
            "omnivia_runtime_policy_snapshots",
            "omnivia_runtime_budget_snapshots",
            "omnivia_runtime_capability_grants",
        )
    }

    second = WorkflowStartResult.from_wire(start(owned, request_id="req-start-replay"))

    assert second.admission == "replayed"
    assert second.run == first.run
    for table, count in before.items():
        assert row_count(owned, table) == count


def test_start_conflicting_replay_refuses_without_duplicate_admission(
    owned: m1.Owned,
) -> None:
    ws.started(owned)
    start(owned)
    before = {
        table: row_count(owned, table)
        for table in (
            "omnivia_durable_jobs",
            "omnivia_runtime_runs",
            "omnivia_workflow_runs",
            "omnivia_idempotency_claims",
        )
    }

    with pytest.raises(OperationError) as raised:
        start(
            owned,
            payload={"logical_key": "different-caller-correlation"},
            request_id="req-start-conflict",
        )

    assert raised.value.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    for table, count in before.items():
        assert row_count(owned, table) == count


def test_start_refuses_bad_configured_decision_authority(owned: m1.Owned) -> None:
    ws.started(owned)
    sources = (
        PolicySource(
            kind="platform_safety_boundary",
            source_id="platform-default",
            allowed_capabilities=("memory.read",),
            max_cost_units=1_000,
            # Missing token ceiling, so the authority cannot say what was admitted.
        ),
    )

    with pytest.raises(OperationError) as raised:
        runtime_for(owned, sources=sources, application_authority=True).workflow_start(
            context_for(
                WORKFLOW_START_OPERATION,
                {"definition_id": repo.WORKFLOW_ID, "definition_version": "1.0.0"},
                authorized=True,
            )
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert "does not resolve a coherent decision" in raised.value.message
    assert "max_token_units" in raised.value.message


def test_start_decision_gap_is_after_plan_and_mutation_grant_are_available(
    owned: m1.Owned,
) -> None:
    """The refusal names the gap left after the existing admission seams."""
    ws.started(owned)
    plan = read_workflow_plan(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        workflow_id=repo.WORKFLOW_ID,
        workflow_version=repo.WORKFLOW_VERSION,
    )
    assert plan is not None
    assert MUTATION_ROLES[WORKFLOW_START_OPERATION] == "workspace_contributor"
    guard = read_guard(owned.connection)
    assert guard is not None
    entry = get_operation_metadata(WORKFLOW_START_OPERATION)
    operation_input = {
        "definition_id": repo.WORKFLOW_ID,
        "definition_version": repo.WORKFLOW_VERSION,
    }
    authorized = s0.authorize(entry, operation_input=operation_input)

    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=guard,
        equivalence=s0.equivalence_for(entry, operation_input=operation_input),
        clock=s0.clock_at(),
    )

    assert grant.operation == WORKFLOW_START_OPERATION
    assert grant.required_role == "workspace_contributor"


@pytest.mark.parametrize(
    "table",
    [
        "omnivia_runtime_runs",
        "omnivia_workflow_runs",
        "omnivia_runtime_events",
        "omnivia_idempotency_claims",
    ],
)
def test_start_missing_plan_writes_no_durable_row(
    owned: m1.Owned, table: str
) -> None:
    """A request naming no sealed plan is refused before the mutation opens."""
    ws.started(owned)
    record_decisions(owned)
    before = row_count(owned, table)

    with pytest.raises(OperationError) as raised:
        start(owned, payload={"definition_id": "workflow.absent"})

    assert raised.value.code == ERROR_CODE_NOT_FOUND
    assert row_count(owned, table) == before


# --- review over the same durable truth ---------------------------------------------


def test_review_serves_the_run_inspect_serves(owned: m1.Owned) -> None:
    """One durable truth, two operations: the aggregates must be identical."""
    ws.started(owned)
    record_decisions(owned)

    result = WorkflowReviewResult.from_wire(review(owned))

    validate_run(result.run, workspace_id=WORKSPACE_ID)
    assert result.run == WorkflowInspectResult.from_wire(inspect(owned)).run
    assert result.projection_version == projection_version_for()


def test_the_review_projection_states_only_what_the_run_states(
    owned: m1.Owned,
) -> None:
    """Every value in the projection is checked against the aggregate beside it.

    This is the whole point of the operation: the review is display data, so the test
    that matters is that it cannot say anything the served `Run` does not already say.
    """
    ws.started(owned)
    record_decisions(owned)

    result = WorkflowReviewResult.from_wire(review(owned))

    run, projection = result.run, result.review
    assert projection["run_id"] == run.run_id
    assert projection["status"] == run.status
    assert projection["definition"] == {
        "kind": run.definition.definition_kind,
        "id": run.definition.definition_id,
        "version": run.definition.definition_version,
    }
    assert projection["created_at"] == run.created_at
    assert projection["updated_at"] == run.updated_at
    assert projection["finished_at"] == run.finished_at
    assert projection["policy_snapshot_id"] == run.policy.policy_snapshot_id
    assert projection["budget_snapshot_id"] == run.budget.budget_snapshot_id
    assert projection["totals"]["events"] == len(run.events)
    assert projection["totals"]["capability_grants"] == len(run.capability_grants)
    # An admitted run that has executed nothing: the projection reports the absence
    # rather than filling it with plausible work.
    assert projection["steps"] == ()
    assert projection["open_waits"] == ()
    assert projection["pending_approvals"] == ()
    assert projection["totals"]["steps"] == 0


def test_review_reports_the_steps_the_scheduler_actually_opened(
    owned: m1.Owned,
) -> None:
    """With durable steps stored, the projection is read off them, not invented."""
    ws.started(owned)
    record_decisions(owned)
    ws.open_steps(owned)

    result = WorkflowReviewResult.from_wire(review(owned))

    assert result.review["totals"]["steps"] == len(result.run.steps)
    assert result.run.steps != ()
    assert [step["run_step_id"] for step in result.review["steps"]] == [
        step.run_step_id for step in result.run.steps
    ]
    assert [step["status"] for step in result.review["steps"]] == [
        step.status for step in result.run.steps
    ]


def test_review_of_a_run_without_recorded_decisions_is_refused(
    owned: m1.Owned,
) -> None:
    """Review fails closed exactly where inspect does: no decision, no answer."""
    ws.started(owned)

    error = refused(review, owned)

    assert error.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert "policy and budget" in error.message


def test_review_of_an_unknown_run_is_not_found(owned: m1.Owned) -> None:
    ws.started(owned)
    record_decisions(owned)

    assert (
        refused(review, owned, run_id="run-nobody-admitted").code
        == ERROR_CODE_NOT_FOUND
    )


def test_review_of_a_run_that_is_not_a_workflow_run_is_not_found(
    owned: m1.Owned,
) -> None:
    ws.started(owned)
    record_decisions(owned)
    admit_agent_run(owned)

    assert refused(review, owned, run_id=AGENT_RUN_ID).code == ERROR_CODE_NOT_FOUND


def test_review_pinned_to_a_prior_same_run_projection_version_is_served(
    owned: m1.Owned,
) -> None:
    """Review and inspect share the same Core-issued reconnect cursor."""
    ws.started(owned)
    record_decisions(owned)

    inspected = WorkflowInspectResult.from_wire(inspect(owned))
    reviewed = WorkflowReviewResult.from_wire(
        review(owned, projection_version=inspected.projection_version)
    )

    assert reviewed.run == inspected.run
    assert reviewed.projection_version == inspected.projection_version


@pytest.mark.parametrize(
    "projection_version",
    [
        "v1",
        projection_version_for("run-other", 0),
        projection_version_for(sequence=999),
    ],
)
def test_review_pinned_to_a_stale_or_wrong_projection_version_is_refused(
    owned: m1.Owned, projection_version: str
) -> None:
    ws.started(owned)
    record_decisions(owned)

    error = refused(review, owned, projection_version=projection_version)

    assert error.code == ERROR_CODE_STALE_PROJECTION
    assert "projection version" in error.message


@pytest.mark.parametrize("payload", [{}, {"run_id": 7}, {"projection_version": 3}])
def test_review_of_an_undecodable_payload_is_an_invalid_request(
    owned: m1.Owned, payload: dict[str, Any]
) -> None:
    ws.started(owned)
    record_decisions(owned)
    context = context_for(WORKFLOW_REVIEW_OPERATION, payload)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned).workflow_review(context)

    assert raised.value.code == ERROR_CODE_INVALID_REQUEST


def test_review_without_storage_refuses_before_reading_anything() -> None:
    assert refused(review, None).code == ERROR_CODE_DEPENDENCY_UNAVAILABLE


# --- control refuses explicitly, and changes nothing ---------------------------------

#: Everything a control that acted would have to touch. Checked around every control
#: call, because "the disposition said unsupported" is only worth having if the database
#: agrees that nothing happened.
CONTROL_TABLES = (
    "omnivia_runtime_runs",
    "omnivia_workflow_runs",
    "omnivia_runtime_events",
    "omnivia_runtime_run_steps",
    "omnivia_runtime_waits",
    "omnivia_runtime_stop_requests",
    "omnivia_runtime_stop_outcomes",
    "omnivia_idempotency_claims",
    "omnivia_durable_jobs",
)


@pytest.mark.parametrize("action", ["cancel", "pause", "resume", "not_a_real_action"])
def test_control_answers_an_explicit_unsupported_disposition(
    owned: m1.Owned, action: str
) -> None:
    """A contract-valid disposition naming the action, with the unchanged `Run`."""
    ws.started(owned)
    record_decisions(owned)

    result = WorkflowControlResult.from_wire(control(owned, action=action))
    inspected = WorkflowInspectResult.from_wire(inspect(owned)).run

    assert result.run_id == RUN_ID
    assert result.disposition == CONTROL_DISPOSITION_UNSUPPORTED
    assert result.run == inspected
    assert result.details is not None
    assert result.details["action"] == action
    assert result.details["run_status"] == inspected.status
    assert "no Workflow scheduler or executor" in result.details["reason"]


def test_control_writes_nothing(owned: m1.Owned) -> None:
    """The disposition is not a quiet transition: no durable row moves."""
    ws.started(owned)
    record_decisions(owned)
    before = {table: row_count(owned, table) for table in CONTROL_TABLES}
    status_before = WorkflowInspectResult.from_wire(inspect(owned)).run.status

    control(owned)

    for table, count in before.items():
        assert row_count(owned, table) == count
    after = WorkflowInspectResult.from_wire(inspect(owned)).run
    assert after.status == status_before
    assert after.finished_at is None


def test_control_without_dispatcher_owned_application_authority_refuses(
    owned: m1.Owned,
) -> None:
    """Unsupported control is still a mutating operation and needs authority."""
    ws.started(owned)
    record_decisions(owned)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned).workflow_control(
            context_for(WORKFLOW_CONTROL_OPERATION, {"run_id": RUN_ID, "action": "cancel"})
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert "workflow.control" in raised.value.message
    assert "dispatcher-owned application" in raised.value.message


def test_control_of_an_unknown_run_is_not_found(owned: m1.Owned) -> None:
    """An unresolvable target is a refusal, not a polite unsupported disposition."""
    ws.started(owned)
    record_decisions(owned)

    assert (
        refused(control, owned, run_id="run-nobody-admitted").code
        == ERROR_CODE_NOT_FOUND
    )


def test_control_of_a_run_that_is_not_a_workflow_run_is_not_found(
    owned: m1.Owned,
) -> None:
    ws.started(owned)
    record_decisions(owned)
    admit_agent_run(owned)

    assert refused(control, owned, run_id=AGENT_RUN_ID).code == ERROR_CODE_NOT_FOUND


@pytest.mark.parametrize(
    "payload",
    [{}, {"run_id": RUN_ID}, {"action": "cancel"}, {"run_id": RUN_ID, "action": 7}],
)
def test_control_of_an_undecodable_payload_is_an_invalid_request(
    owned: m1.Owned, payload: dict[str, Any]
) -> None:
    ws.started(owned)
    record_decisions(owned)
    context = context_for(WORKFLOW_CONTROL_OPERATION, payload)

    with pytest.raises(OperationError) as raised:
        runtime_for(owned).workflow_control(context)

    assert raised.value.code == ERROR_CODE_INVALID_REQUEST


def test_control_without_storage_refuses_before_reading_anything() -> None:
    assert refused(control, None).code == ERROR_CODE_DEPENDENCY_UNAVAILABLE


# --- the seam over the live dependency ----------------------------------------------


def test_handlers_route_inspect_to_the_live_runtime(owned: m1.Owned) -> None:
    """The production seam, not the runtime object, is what a caller reaches."""
    ws.started(owned)
    record_decisions(owned)
    service = SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        workflow_runtime=runtime_for(owned),
    )

    wire = WorkflowHandlers(service=service).workflow_inspect(
        context_for(WORKFLOW_INSPECT_OPERATION, {"run_id": RUN_ID})
    )

    assert WorkflowInspectResult.from_wire(wire).run.run_id == RUN_ID


@pytest.mark.parametrize(
    ("operation", "method", "payload"),
    [
        (WORKFLOW_REVIEW_OPERATION, "workflow_review", {"run_id": RUN_ID}),
        (
            WORKFLOW_CONTROL_OPERATION,
            "workflow_control",
            {"run_id": RUN_ID, "action": "cancel"},
        ),
    ],
)
def test_handlers_route_review_and_control_to_the_live_runtime(
    owned: m1.Owned, operation: str, method: str, payload: dict[str, Any]
) -> None:
    """Both now answer through the seam, from the live runtime rather than a mock."""
    ws.started(owned)
    record_decisions(owned)
    service = SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        workflow_runtime=runtime_for(owned, application_authority=True),
    )

    wire = getattr(WorkflowHandlers(service=service), method)(
        context_for(
            operation,
            payload,
            authorized=operation == WORKFLOW_CONTROL_OPERATION,
        )
    )

    assert wire["run" if operation == WORKFLOW_REVIEW_OPERATION else "run_id"]


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        (WORKFLOW_CONTROL_OPERATION, "workflow_control"),
        (WORKFLOW_REVIEW_OPERATION, "workflow_review"),
    ],
)
def test_a_runtime_without_the_method_still_fails_closed(
    operation: str, method: str
) -> None:
    """The seam's own property survives the family being implemented: a build whose
    runtime cannot answer refuses rather than letting an incidental double through."""
    service = SimpleNamespace(workflow_runtime=SimpleNamespace())

    with pytest.raises(OperationError) as raised:
        getattr(WorkflowHandlers(service=service), method)(
            context_for(operation, {"run_id": RUN_ID, "action": "cancel"})
        )

    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE


# --- the production configuration seam ----------------------------------------------
#
# Everything above builds the runtime over a hand-made `SimpleNamespace`, which is what
# let `workflow_decision_authority` be a test-only fact: production `ServiceRunner`
# initialised it to `None` and nothing ever wrote to it, so `workflow.start` was closed
# outside this file. These prove the real startup path binds it from a real configured
# statement, and that every way of not stating one keeps it closed.


def authority_document() -> dict[str, Any]:
    """`decision_sources()` as the configured file, so both stay one statement."""
    return {
        "sources": [
            {
                "kind": "platform_safety_boundary",
                "source_id": "platform-default",
                "allowed_capabilities": ["memory.read", "memory.write", "workflow.run"],
                "offered_capabilities": [
                    "memory.read",
                    "memory.write",
                    "tools.execute",
                    "workflow.run",
                ],
                "max_cost_units": 1_000,
                "max_token_units": 200_000,
                "max_wall_clock_ms": 600_000,
                "side_effects_allowed": True,
            },
            {
                "kind": "workspace",
                "source_id": "workspace-standard",
                "allowed_capabilities": ["memory.read", "workflow.run"],
                "required_capabilities": ["workflow.run"],
                "max_cost_units": 250,
                "max_token_units": 50_000,
                "side_effects_allowed": False,
            },
        ]
    }


def configured_service(tmp_path: Path, document: object | None) -> ServiceRunner:
    """A real workspace, a real `ServiceRunner`, and `document` as its authority file.

    Returns the runner rather than only its report because what is being proved is the
    state of the started service, not the shape of the report.
    """
    workspace = tmp_path / "workspace"
    installation = tmp_path / "installation-state"
    initialised = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
        core_version=SERVER_VERSION,
    )
    assert initialised.status is not WorkspaceInitStatus.REFUSED, initialised.reason
    assert initialised.workspace_id is not None
    if document is not None:
        layout = InstallationLayout(root=installation)
        layout.create(initialised.workspace_id)
        path = layout.runtime_for(initialised.workspace_id) / AUTHORITY_FILENAME
        path.write_text(
            document if isinstance(document, str) else json.dumps(document),
            encoding="utf-8",
        )
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace,
            installation_root=installation,
            core_version=SERVER_VERSION,
        )
    )
    return runner


def test_a_started_service_binds_the_configured_decision_authority(
    tmp_path: Path,
) -> None:
    """The production blocker: `workflow.start` reached `None` and could go no further.

    The runner now carries exactly the sources the file states, and the same
    `effective_policy()` call `workflow_start` makes resolves instead of refusing.
    """
    runner = configured_service(tmp_path, authority_document())

    report = runner.start()

    try:
        assert report.ready, report.to_dict()
        assert runner.workflow_decision_authority == decision_sources()
        effective = runner.workflow_runtime.effective_policy()
        # The workspace source narrows the platform boundary, and the resolution shows
        # it: an intersected capability set, the smaller of each ceiling, and deny-wins
        # on side effects.
        assert effective.granted_capabilities == ("memory.read", "workflow.run")
        assert effective.max_cost_units == 250
        assert effective.max_token_units == 50_000
        assert effective.max_wall_clock_ms == 600_000
        assert effective.side_effects_allowed is False
    finally:
        runner.stop()


def test_a_started_service_without_the_file_still_refuses_to_start_a_run(
    tmp_path: Path,
) -> None:
    """Fail closed by default: no configured statement, no admission."""
    runner = configured_service(tmp_path, None)

    report = runner.start()

    try:
        assert report.ready, report.to_dict()
        assert runner.workflow_decision_authority is None
        with pytest.raises(OperationError) as raised:
            runner.workflow_runtime.effective_policy()
        assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
        assert "decision authority configured" in raised.value.message
        assert "will not invent" in raised.value.message
    finally:
        runner.stop()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param("{not json", "not readable UTF-8 JSON", id="unparseable"),
        pytest.param(
            {"sources": []},
            "non-empty list",
            id="empty",
        ),
        pytest.param(
            {"sources": [{"kind": "workspace", "source_id": "w", "budget": 5}]},
            "members this build does not apply",
            id="unknown_member",
        ),
        pytest.param(
            {
                "sources": [
                    {
                        "kind": "platform_safety_boundary",
                        "source_id": "p",
                        "allowed_capabilities": ["workflow.run"],
                        "max_cost_units": True,
                        "max_token_units": 10,
                    }
                ]
            },
            "not a non-negative integer",
            id="boolean_ceiling",
        ),
        pytest.param(
            {
                "sources": [
                    {
                        "kind": "workspace",
                        "source_id": "w",
                        "allowed_capabilities": ["workflow.run"],
                        "max_cost_units": 10,
                        "max_token_units": 10,
                    }
                ]
            },
            "no floor",
            id="no_safety_boundary",
        ),
        pytest.param(
            {
                "sources": [
                    {
                        "kind": "platform_safety_boundary",
                        "source_id": "p",
                        "allowed_capabilities": ["memory.read"],
                        "required_capabilities": ["workflow.run"],
                        "max_cost_units": 10,
                        "max_token_units": 10,
                    }
                ]
            },
            "does not grant",
            id="insufficient",
        ),
    ],
)
def test_an_unusable_authority_file_refuses_startup(
    tmp_path: Path, document: object, expected: str
) -> None:
    """Malformed, incoherent or insufficient stops the service, not the first run.

    The refusal names the file and the reason, and nothing else: a startup report is
    public output, so it carries no path, no workspace identity and no member value.
    """
    runner = configured_service(tmp_path, document)

    report = runner.start()

    try:
        assert not report.ready
        assert AUTHORITY_FILENAME in report.reason
        assert expected in report.reason
        assert str(tmp_path) not in report.reason
        assert runner.workflow_decision_authority is None
    finally:
        runner.stop()
