"""T-0693 acceptance for the four Workflow application operations.

These drive the real `ApplicationDispatcher` -- the twelve-check authorization seam,
the mutation seam, the durable stop ledger and the runtime wait authority -- against a
real migrated workspace. Nothing here fabricates an authorized context, and no test
calls a handler method directly, because the properties worth holding are properties of
the served path rather than of a Python function.

*Authority is not implied by a build shipping the handler.* A request without the
scope, without the capability, or naming a purpose the session does not allow is
refused before the handler is reached, and the refusals are the contract's own codes.

*A workspace is a boundary, not a filter.* A Run of another workspace is `not_found`,
because the read is scoped at the query rather than after it.

*Start is one write or none.* The durable job, the canonical Run, the sealed plan and
the `RuntimeDefinitionBinding` land together. A start that cannot resolve its release
leaves nothing at all -- not a job row, not a plan.

*One idempotency key is one Run.* Migration 0018 requires a Run's `logical_key` to be
the `idempotency_key` of the claim admitting it, so the key *is* the Run's durable
identity: repeating a start answers from the stored outcome, a new key is a new Run,
and one key naming a different canonical request is an `idempotency_conflict` that
rebinds nothing.

*A stale fence writes nothing.* The generation is validated on entry and again before
commit, so authority lost between them refuses rather than commits.

*Control does the thing or says it did not.* Cancelling a live Run appends the
cancellation its outcome names; cancelling a finished one settles as
`ignored_already_terminal` and leaves the stream untouched. An action this build does
not implement is refused rather than answered with a fabricated success.

*Inspect and review read, and never predict.* An unobserved step is absent rather than
forecast, and a tampered binding fails the read closed instead of being reported as the
material a Run is executing against.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_t0688_workflow_runtime_hardening_repository as ip06
import test_v06_5_s0_mutation_foundation as s0
import test_workflow_runs_migration as m27
from omnivia_core_runtime.execution.workflow import (
    EXECUTION_CLASS_DETERMINISTIC,
    ROUTE_DETERMINISTIC,
    MaterialisedWorkflow,
    StepDefinition,
    WorkflowDefinition,
    materialise_workflow,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    JOB_FAMILY_PURPOSES,
    WORKFLOW_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_job_application_dispatcher,
    build_workflow_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import workflow as workflow_handlers
from omnivia_core_runtime.service.handlers.workflow import (
    WORKFLOW_CONTROL_OPERATION,
    WORKFLOW_INSPECT_OPERATION,
    WORKFLOW_REVIEW_OPERATION,
    WORKFLOW_START_OPERATION,
    WorkflowRelease,
)
from omnivia_core_runtime.service.jobs import (
    claim_application_job,
    fail_application_job,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runtime_recovery import (
    CLASSIFICATION_NO_OPEN_ATTEMPT,
    recover_runtime_startup,
)
from omnivia_core_runtime.service.workflow_runtime import workflow_runtime_scheduler
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    read_run,
    read_run_sequence,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER
from omnivia_core_runtime.storage.runtime_stop import STOP_OUTCOME_REJECTED

from omnivia_core import contracts as _contracts_package
from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

#: The module the handler imports its `ERROR_CODE_*` names from, so an AST read of one
#: of those names resolves to the same string the handler would raise.
contracts = _contracts_package.v1

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-t0693-other"
INSTALLATION_ID = s0.INSTALLATION_ID
PRINCIPAL = CONFIGURED_LOCAL_OWNER
WALL = datetime(2026, 9, 3, tzinfo=UTC)
WALL_US = int(WALL.timestamp() * 1_000_000)

WORKFLOW_ID = m27.WORKFLOW_ID
WORKFLOW_VERSION = m27.WORKFLOW_VERSION

RUNS = m27.RUNS
PLANS = m27.PLANS
BINDINGS = ip06.BINDINGS
EVENTS = "omnivia_runtime_events"
STOP_REQUESTS = "omnivia_runtime_stop_requests"
STOP_OUTCOMES = "omnivia_runtime_stop_outcomes"
DURABLE_JOBS = "omnivia_durable_jobs"


# --- the released workflow this build is told about ---------------------------------


def plan(version: str = WORKFLOW_VERSION) -> MaterialisedWorkflow:
    """One two-step deterministic plan, materialised the way production would."""
    steps = tuple(
        StepDefinition(
            step_id=step_id,
            component_id="component-echo",
            component_version="1.0.0",
            execution_class=EXECUTION_CLASS_DETERMINISTIC,
            depends_on=depends,
        ).sealed()
        for step_id, depends in (("a-plan", ()), ("b-write", ("a-plan",)))
    )
    return materialise_workflow(
        WorkflowDefinition(
            workflow_id=WORKFLOW_ID, version=version, steps=steps
        ).sealed()
    )


def release(
    materialised: MaterialisedWorkflow | None = None, **overrides: Any
) -> WorkflowRelease:
    """The material a release authority would state, minus the admission's own facts."""
    sealed = materialised or plan()
    material: dict[str, Any] = {
        "releaseRef": {"releaseId": "release-t0693"},
        "executionProfileDigest": "sha256:" + "3" * 64,
        "effectivePolicyDigest": "sha256:" + "4" * 64,
        "componentImplementationDigests": {"component-echo": "sha256:" + "5" * 64},
        "resourceBindingSnapshots": [
            {
                "resourceRequirementId": "resource-store",
                "resourceRef": {"resourceId": "store-primary"},
                "snapshotRef": {"snapshotId": "snapshot-t0693"},
                "snapshotDigest": "sha256:" + "6" * 64,
            }
        ],
        "modelPolicySnapshotRef": {"snapshotId": "model-policy-t0693"},
        "modelPolicySnapshotDigest": "sha256:" + "7" * 64,
    }
    material.update(overrides)
    return WorkflowRelease(plan=sealed, material=material)


def resolver(
    *releases: WorkflowRelease,
) -> Callable[..., WorkflowRelease | None]:
    """A release authority that knows exactly the releases it was handed."""
    known = {(item.plan.workflow_id, item.plan.version): item for item in releases}

    def resolve(*, workflow_id: str, workflow_version: str) -> WorkflowRelease | None:
        return known.get((workflow_id, workflow_version))

    return resolve


# --- one owned, migrated workspace --------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    """One clock the test holds too, so it can advance past what it seeded."""
    return FakeClock(wall=WALL)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def allocator(tag: str) -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


def fallback(principal: str = PRINCIPAL) -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=principal,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


def dispatcher(
    holder: m1.Owned,
    *,
    tag: str = "t0693",
    releases: tuple[WorkflowRelease, ...] | None = None,
    workspace_id: str = WORKSPACE_ID,
    principal: str = PRINCIPAL,
    wait_policy: Any = None,
    clock: FakeClock | None = None,
) -> ApplicationDispatcher:
    """The real production family, with only its two authority seams supplied."""
    return build_workflow_application_dispatcher(
        service=holder,
        principal_id=principal,
        installation_id=INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=fallback(principal),
        clock=FakeClock(wall=WALL) if clock is None else clock,
        allocate_identifier=allocator(tag),
        resolve_release=None if releases is None else resolver(*releases),
        wait_policy=wait_policy,
    )


def request(
    operation: str,
    operation_input: Mapping[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
    workspace_id: str = WORKSPACE_ID,
    **overrides: Any,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=WORKFLOW_FAMILY_PURPOSES[operation],
        workspace_id=workspace_id,
        **overrides,
    )


def start(
    served: ApplicationDispatcher,
    *,
    request_id: str = "req-start-1",
    idempotency_key: str = "idem-start-1",
    workflow_version: str = WORKFLOW_VERSION,
    **overrides: Any,
) -> ResponseEnvelope:
    payload: dict[str, object] = {
        "workflow_id": WORKFLOW_ID,
        "workflow_version": workflow_version,
    }
    return served.dispatch(
        request(
            WORKFLOW_START_OPERATION,
            payload,
            request_id=request_id,
            idempotency_key=idempotency_key,
            **overrides,
        )
    )


def result(response: ResponseEnvelope) -> Mapping[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), _error(response)
    return response.result


def code(response: ResponseEnvelope) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response.error.code


def _error(response: ResponseEnvelope) -> str:
    return (
        f"{response.error.code}: {response.error.message}"
        if isinstance(response, ErrorResponseEnvelope)
        else "success"
    )


def count(holder: m1.Owned, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def run_id_of(response: ResponseEnvelope) -> str:
    return str(result(response)["run"]["run_id"])


# --- start: one write, or none ------------------------------------------------------


def test_a_start_seals_the_plan_admits_the_run_and_binds_them(
    owned: m1.Owned,
) -> None:
    """The four records are one write, and every one of them names the same release."""
    served = dispatcher(owned, releases=(release(),))

    answer = result(start(served))

    projection = answer["run"]
    assert projection["definition"] == {
        "definition_kind": "workflow",
        "definition_id": WORKFLOW_ID,
        "definition_version": WORKFLOW_VERSION,
    }
    assert projection["plan_digest"] == plan().content_hash
    # A bound run whose plan's steps are open is `queued`: durable work nothing has
    # claimed yet, projected from the `admitted` event at sequence zero plus the step
    # ledger the same mutation opened.
    assert projection["state"] == "queued"
    assert projection["run_status"] == "admitted"
    assert projection["binding"]["legacyBinding"] is False
    assert count(owned, RUNS) == count(owned, PLANS) == count(owned, BINDINGS) == 1
    assert count(owned, DURABLE_JOBS) == 1
    assert owned.connection.execute(
        "SELECT ordinal, step_kind FROM omnivia_runtime_run_steps "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY ordinal",
        (WORKSPACE_ID, str(projection["run_id"])),
    ).fetchall() == [(1, "workflow.deterministic"), (2, "workflow.deterministic")]


def test_a_start_names_the_job_its_run_is_carried_by_first_and_on_replay(
    owned: m1.Owned,
) -> None:
    """The catalogue declares this operation `always_returns_job`, so every start says which.

    The reference is read off the persisted run's own `job_id` rather than assumed from
    the run identifier, so a replay answers with the first call's job instead of minting a
    second reference for a caller who started nothing.
    """
    served = dispatcher(owned, releases=(release(),))

    first = start(served)
    again = start(served)

    assert isinstance(first, SuccessResponseEnvelope), _error(first)
    assert isinstance(again, SuccessResponseEnvelope), _error(again)
    stored = read_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id_of(first)
    )
    assert stored is not None
    assert first.metadata.job is not None
    assert first.metadata.job.job_id == stored.job_id
    assert again.metadata.job == first.metadata.job
    assert count(owned, DURABLE_JOBS) == 1


def test_a_build_with_no_release_authority_refuses_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    """The shipped default. A build that cannot prove what a run would execute does not run it."""
    served = dispatcher(owned)

    assert code(start(served)) == "dependency_unavailable"
    assert count(owned, RUNS) == count(owned, PLANS) == count(owned, DURABLE_JOBS) == 0


def test_a_release_this_build_does_not_serve_is_not_found_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))

    assert code(start(served, workflow_version="9.9.9")) == "not_found"
    assert count(owned, RUNS) == count(owned, PLANS) == count(owned, DURABLE_JOBS) == 0


# --- start: one logical key is one run ----------------------------------------------


def test_repeating_one_start_replays_it_and_admits_no_second_run(
    owned: m1.Owned,
) -> None:
    """The same request twice is the mutation seam's own replay: the stored bytes."""
    served = dispatcher(owned, releases=(release(),))
    first = result(start(served))

    again = result(start(served))

    assert again == first
    assert count(owned, RUNS) == 1
    assert count(owned, DURABLE_JOBS) == 1


def test_a_second_idempotency_key_is_a_second_run(owned: m1.Owned) -> None:
    """An idempotency key is the Run's identity, so a new key is a new Run.

    Migration 0018 makes that a property of the schema rather than of this handler: a
    run's `logical_key` must be the `idempotency_key` of the claim admitting it, so
    there is no second identity a caller could supply to reach the first Run again,
    and none this build could invent for it.
    """
    served = dispatcher(owned, releases=(release(),))
    first = result(start(served))

    again = result(
        start(served, request_id="req-start-2", idempotency_key="idem-start-2")
    )

    assert again["run"]["run_id"] != first["run"]["run_id"]
    assert count(owned, RUNS) == count(owned, DURABLE_JOBS) == 2
    # One release, one sealed plan: a second Run of the same Workflow version re-seals
    # nothing, because 0027 makes a plan immutable once it has admitted a Run.
    assert count(owned, PLANS) == 1


def test_one_idempotency_key_naming_a_different_request_is_a_conflict(
    owned: m1.Owned,
) -> None:
    """One key is one logical identity, not a slot to be overwritten."""
    served = dispatcher(owned, releases=(release(), release(plan(version="2.0.0"))))
    first = result(start(served))

    response = served.dispatch(
        request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": WORKFLOW_ID, "workflow_version": "2.0.0"},
            request_id="req-start-3",
            idempotency_key="idem-start-1",
        )
    )

    assert code(response) == "idempotency_conflict"
    assert count(owned, RUNS) == 1
    # The Run that exists is still bound to the release it was admitted with, and the
    # second version was never sealed.
    stored = ip06.read_runtime_definition_binding(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=str(first["run"]["run_id"]),
    )
    assert stored is not None
    assert stored.binding["workflowVersion"] == WORKFLOW_VERSION
    assert count(owned, PLANS) == 1


# --- authority ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("narrowed", "expected"),
    (
        ({"scopes": frozenset()}, "authorization_denied"),
        ({"capabilities": ()}, "capability_not_granted"),
        ({"purposes": frozenset({"chat_authoring"})}, "invalid_purpose"),
        ({"workspaces": frozenset({OTHER_WORKSPACE_ID})}, "workspace_not_granted"),
        ({"operations": frozenset({WORKFLOW_INSPECT_OPERATION})}, "authorization_denied"),
    ),
    ids=("no-scope", "no-capability", "wrong-purpose", "other-workspace", "not-granted"),
)
def test_a_start_the_session_does_not_authorise_writes_nothing(
    owned: m1.Owned, narrowed: dict[str, Any], expected: str
) -> None:
    """Authority is checked against the server's session, not against the request.

    Dispatched through `dispatch_for_session`, which is the seam a transport resolving
    its own credentials uses, so the narrowing under test is the one that decides --
    a request that simply omitted a scope would be refused earlier, as `invalid_request`,
    for being malformed rather than for being unauthorised.
    """
    served = dispatcher(owned, releases=(release(),))
    session = _replace_session(served.session, **narrowed)

    response = served.dispatch_for_session(
        request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": WORKFLOW_ID, "workflow_version": WORKFLOW_VERSION},
            request_id="req-denied",
            idempotency_key="idem-denied",
        ),
        session,
    )

    assert code(response) == expected
    assert count(owned, RUNS) == count(owned, PLANS) == count(owned, DURABLE_JOBS) == 0


def _replace_session(session: Any, **narrowed: Any) -> Any:
    """The same server session, granting strictly less."""
    fields = {
        "principal_id": session.principal_id,
        "roles": session.roles,
        "installations": session.installations,
        "workspaces": session.workspaces,
        "operations": session.operations,
        "scopes": session.scopes,
        "purposes": session.purposes,
        "capabilities": session.capabilities,
    }
    fields.update(narrowed)
    return type(session)(**fields)


def test_a_mutating_workflow_request_without_an_idempotency_key_is_refused(
    owned: m1.Owned,
) -> None:
    """The catalogue requires one for both Workflow mutations; the seam enforces it."""
    served = dispatcher(owned, releases=(release(),))

    response = served.dispatch(
        request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": WORKFLOW_ID, "workflow_version": WORKFLOW_VERSION},
            request_id="req-nokey",
            idempotency_key=None,
        )
    )

    assert code(response) == "invalid_request"
    assert count(owned, RUNS) == 0


def test_a_read_of_another_workspaces_run_is_invisible_rather_than_filtered(
    owned: m1.Owned,
) -> None:
    """Workspace isolation is at the query. The Run exists; this workspace has no such Run."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    foreign = dispatcher(
        owned, tag="foreign", releases=(release(),), workspace_id=OTHER_WORKSPACE_ID
    )
    response = foreign.dispatch(
        request(
            WORKFLOW_INSPECT_OPERATION,
            {"run_id": run_id},
            request_id="req-foreign",
            workspace_id=OTHER_WORKSPACE_ID,
        )
    )

    assert code(response) == "not_found"


# --- a stale fence ------------------------------------------------------------------


# --- inspect ------------------------------------------------------------------------


def test_inspect_returns_the_sealed_plan_and_no_unobserved_step(
    owned: m1.Owned,
) -> None:
    """A step nothing reached is absent, not forecast."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    answer = result(
        served.dispatch(
            request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-inspect",
            )
        )
    )

    assert [step["step_id"] for step in answer["plan"]] == ["a-plan", "b-write"]
    assert [step["route"] for step in answer["plan"]] == [
        ROUTE_DETERMINISTIC,
        ROUTE_DETERMINISTIC,
    ]
    assert answer["observations"] == []
    assert answer["run"]["state"] == "queued"


def test_inspect_reports_the_state_the_durable_stream_moved_to(
    owned: m1.Owned,
) -> None:
    """The state is read off the event stream, not computed from what the plan would do."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=run_id,
        runtime_event_id="evt-running",
        occurred_at_us=m27.BASE_US + 5_000,
        event_kind="run_started",
        run_status="running",
    )

    answer = result(
        served.dispatch(
            request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-inspect-running",
            )
        )
    )

    assert answer["run"]["run_status"] == "running"
    assert answer["run"]["state"] == "running"


def test_a_run_this_workspace_does_not_hold_is_not_found(owned: m1.Owned) -> None:
    served = dispatcher(owned, releases=(release(),))

    response = served.dispatch(
        request(
            WORKFLOW_INSPECT_OPERATION,
            {"run_id": "run-nobody-admitted"},
            request_id="req-missing",
        )
    )

    assert code(response) == "not_found"


def test_a_tampered_binding_refuses_the_read_rather_than_reporting_it(
    owned: m1.Owned,
) -> None:
    """The read is T-0688's verifying reader, so edited bytes are not truth about a run."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    forged = ip06.canonicalize(
        dict(
            ip06.read_runtime_definition_binding(
                owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
            ).binding,  # type: ignore[union-attr]
            releaseRef={"releaseId": "release-substituted"},
        )
    )
    connection = ip06.corrupt(
        owned, f"UPDATE {BINDINGS} SET binding_json = ?", forged
    )
    try:
        tampered = dispatcher(
            _rebound(owned, connection), tag="tampered", releases=(release(),)
        )
        response = tampered.dispatch(
            request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-tampered",
            )
        )
    finally:
        connection.close()

    assert code(response) == "internal_non_recoverable"


def _rebound(holder: m1.Owned, connection: sqlite3.Connection) -> Any:
    """The same owner, reading through another handle on the same file."""

    class _Rebound:
        pass

    rebound = _Rebound()
    rebound.connection = connection  # type: ignore[attr-defined]
    rebound.identity = holder.identity  # type: ignore[attr-defined]
    return rebound


# --- control ------------------------------------------------------------------------


def cancel(
    served: ApplicationDispatcher,
    run_id: str,
    *,
    request_id: str = "req-cancel",
    idempotency_key: str = "idem-cancel",
    **overrides: Any,
) -> ResponseEnvelope:
    payload: dict[str, object] = {"run_id": run_id, "action": "cancel"}
    payload.update(overrides)
    return served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            payload,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    )


def test_cancelling_a_live_run_appends_the_cancellation_its_outcome_names(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    before = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )

    answer = result(cancel(served, run_id))

    assert answer["disposition"] == "cancellation_accepted"
    assert answer["run"]["state"] == "cancelled"
    assert answer["run"]["run_status"] == "cancelled"
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
        == before + 1
    )
    assert count(owned, STOP_REQUESTS) == 1


def test_cancelling_a_terminal_run_settles_as_ignored_and_touches_no_stream(
    owned: m1.Owned,
) -> None:
    """A finished Run is not downgraded, and this is a success rather than a conflict."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    result(cancel(served, run_id))
    after_first = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )

    answer = result(
        cancel(
            served,
            run_id,
            request_id="req-cancel-2",
            idempotency_key="idem-cancel-2",
        )
    )

    assert answer["disposition"] == "cancellation_ignored_already_terminal"
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
        == after_first
    )


def test_a_stop_outcome_this_build_cannot_report_refuses_the_whole_transaction(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rejected` is not `ignored_already_terminal`, and is not reported as one.

    A rejected stop changed nothing, and neither did an ignored already-terminal one --
    but only the second means the run has finished. So the ledger's own outcome is read
    through a mapping that publishes exactly the two dispositions this build can state,
    and anything else raises before either history is published. The stop rows and the
    `cancelled` event the real writer had already issued go with the transaction, so the
    run is left exactly where the cancellation found it.
    """
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    stored = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
    assert stored is not None
    before = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )
    honest = workflow_handlers.transaction_local_stop_writer

    def rejecting(connection: Any, *, workspace_id: str) -> Any:
        writer = honest(connection, workspace_id=workspace_id)

        class _Rejecting:
            """The real ledger's writes, reported under an outcome this build cannot state."""

            def stop_run(self, request: Any, **settlement: Any) -> Any:
                return replace(
                    writer.stop_run(request, **settlement),
                    outcome=STOP_OUTCOME_REJECTED,
                )

        return _Rejecting()

    monkeypatch.setattr(
        workflow_handlers, "transaction_local_stop_writer", rejecting
    )

    # It escapes as `StorageError` rather than as a response envelope, which is this
    # repository's existing posture for a durable record this build cannot state: no
    # handler and no dispatcher catches it. What is held here is what it left behind.
    with pytest.raises(StorageError, match="rejected"):
        cancel(served, run_id)

    assert count(owned, STOP_REQUESTS) == count(owned, STOP_OUTCOMES) == 0
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
        == before
    )
    settled = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
    assert settled is not None
    # Not published as `ignored_already_terminal`, and not made terminal to match one.
    assert settled.status == stored.status
    assert (
        owned.connection.execute(
            f"SELECT state FROM {DURABLE_JOBS} WHERE job_id = ?", (stored.job_id,)
        ).fetchone()[0]
        == "queued"
    )


def test_repeating_one_cancellation_replays_it_and_cancels_once(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    first = result(cancel(served, run_id))

    again = result(cancel(served, run_id))

    assert again == first
    assert count(owned, STOP_REQUESTS) == 1


def test_an_action_this_build_does_not_implement_is_refused_explicitly(
    owned: m1.Owned,
) -> None:
    """No branch here reports a success for work nothing performed."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    response = served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            {"run_id": run_id, "action": "pause"},
            request_id="req-pause",
            idempotency_key="idem-pause",
        )
    )

    assert code(response) == "invalid_request"
    assert count(owned, STOP_REQUESTS) == 0


@pytest.mark.parametrize(
    "payload",
    (
        {"action": "cancel", "wait_id": "wait-1"},
        {"action": "resolve_wait"},
        {"action": "resolve_wait", "wait_id": "wait-1"},
    ),
    ids=("cancel-with-wait", "resolve-without-either", "resolve-without-resolution"),
)
def test_control_arguments_must_match_the_action_they_accompany(
    owned: m1.Owned, payload: dict[str, Any]
) -> None:
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    response = served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            {"run_id": run_id, **payload},
            request_id="req-mismatch",
            idempotency_key="idem-mismatch",
        )
    )

    assert code(response) == "invalid_request"
    assert count(owned, STOP_REQUESTS) == 0


def test_resolving_a_wait_without_a_policy_authority_refuses(
    owned: m1.Owned,
) -> None:
    """A build that cannot decide whether a resolution is permitted must not perform one."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    response = served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            {
                "run_id": run_id,
                "action": "resolve_wait",
                "wait_id": "wait-1",
                "resolution": "external_signal",
            },
            request_id="req-resolve",
            idempotency_key="idem-resolve",
        )
    )

    assert code(response) == "dependency_unavailable"


def test_resolving_a_wait_this_run_does_not_hold_is_not_found(
    owned: m1.Owned,
) -> None:
    served = dispatcher(
        owned, releases=(release(),), wait_policy=lambda *args, **kwargs: None
    )
    run_id = run_id_of(start(served))

    response = served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            {
                "run_id": run_id,
                "action": "resolve_wait",
                "wait_id": "wait-nobody-opened",
                "resolution": "external_signal",
            },
            request_id="req-resolve-missing",
            idempotency_key="idem-resolve-missing",
        )
    )

    assert code(response) == "not_found"


def test_controlling_a_run_this_workspace_does_not_hold_is_not_found(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))

    assert code(cancel(served, "run-nobody-admitted")) == "not_found"
    assert count(owned, STOP_REQUESTS) == 0


# --- review -------------------------------------------------------------------------


def test_review_projects_an_empty_journal_as_empty_and_resumable(
    owned: m1.Owned,
) -> None:
    """A Run with no journal has nothing held against it, and no completion invented."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    answer = result(
        served.dispatch(
            request(
                WORKFLOW_REVIEW_OPERATION,
                {"run_id": run_id},
                request_id="req-review",
            )
        )
    )

    assert answer["journal"] == []
    assert answer["resumable"] is True
    assert "completion" not in answer
    assert answer["run"]["run_id"] == run_id


def test_review_is_deterministic_over_unchanged_durable_rows(
    owned: m1.Owned,
) -> None:
    """The same rows produce the same projection, because every field is read."""
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    first = result(
        served.dispatch(
            request(
                WORKFLOW_REVIEW_OPERATION,
                {"run_id": run_id},
                request_id="req-review-a",
            )
        )
    )
    second = result(
        served.dispatch(
            request(
                WORKFLOW_REVIEW_OPERATION,
                {"run_id": run_id},
                request_id="req-review-b",
            )
        )
    )

    assert first == second


def test_review_of_a_run_this_workspace_does_not_hold_is_not_found(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))

    response = served.dispatch(
        request(
            WORKFLOW_REVIEW_OPERATION,
            {"run_id": "run-nobody-admitted"},
            request_id="req-review-missing",
        )
    )

    assert code(response) == "not_found"


# --- restart and recovery -----------------------------------------------------------


def test_a_restarted_service_replays_the_start_and_admits_no_second_run(
    tmp_path: Path,
) -> None:
    """Recovery is deterministic: the same start under a new generation is a replay.

    A new generation means a new owner, a new dispatcher and a fresh identifier
    allocator -- so nothing carries over except the durable rows themselves, which is
    exactly what a restart is.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)

    first = m1.take_ownership(path)
    before = result(start(dispatcher(first, releases=(release(),))))
    first.connection.close()

    second = m1.take_ownership(path)
    try:
        assert second.generation != first.generation
        served = dispatcher(second, tag="restart", releases=(release(),))
        # The same key, because the key is the Run's durable identity. A restarted
        # caller retrying its request is not a caller asking for a second Run.
        after = result(start(served, request_id="req-start-restart"))
        assert after["run"]["run_id"] == before["run"]["run_id"]
        assert count(second, RUNS) == 1
        assert count(second, PLANS) == 1
        assert count(second, DURABLE_JOBS) == 1
        # And the Run still reads the same way it did before the restart.
        answer = result(
            served.dispatch(
                request(
                    WORKFLOW_INSPECT_OPERATION,
                    {"run_id": str(before["run"]["run_id"])},
                    request_id="req-inspect-restart",
                )
            )
        )
        assert answer["run"] == before["run"]
    finally:
        second.connection.close()


# --- the registry and the catalogue --------------------------------------------------


#: Which of this module's functions each operation's dispatch can actually reach. The
#: only thing restated here is routing -- the same routing the handler module states in
#: prose -- and the codes themselves are read out of the source below, so a branch added
#: with an unpublished code fails without anyone maintaining a second list of codes.
REACHABLE_REFUSERS: dict[str, tuple[str, ...]] = {
    WORKFLOW_START_OPERATION: ("_authority", "_decode", "_grant", "workflow_start"),
    WORKFLOW_INSPECT_OPERATION: (
        "_authority",
        "_decode",
        "_view",
        "workflow_inspect",
    ),
    WORKFLOW_CONTROL_OPERATION: (
        "_authority",
        "_decode",
        "_grant",
        "_view",
        "workflow_control",
        "_cancel",
        "_resolve_wait",
    ),
    WORKFLOW_REVIEW_OPERATION: ("_authority", "_decode", "_view", "workflow_review"),
}


def _refusals_by_function() -> dict[str, set[str]]:
    """Every `application_refusal` code the Workflow handler module can raise.

    Read out of the module's own syntax tree rather than restated, because the defect
    this guards against is a handler branch the published posture never learned about:
    a list of codes written here by hand would have to be edited by the same change
    that forgot to edit the catalogue.
    """
    tree = ast.parse(inspect.getsource(workflow_handlers))
    found: dict[str, set[str]] = {}

    def codes(node: ast.AST) -> set[str]:
        return {
            getattr(contracts, call.args[0].id)
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "application_refusal"
            and call.args
            and isinstance(call.args[0], ast.Name)
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            raised = codes(node)
            if raised:
                found[node.name] = raised
    return found


@pytest.mark.parametrize("operation", sorted(REACHABLE_REFUSERS))
def test_the_catalogue_publishes_every_refusal_its_handler_can_return(
    operation: str,
) -> None:
    """The published posture is what the branches actually do, not a superset of it.

    `workflow.start` answers `not_found` for a release authority that serves no such
    version, and `workflow.control` answers `conflict` for a wait resolution the wait
    authority rejects. Both were live branches the catalogue did not publish, and this
    is what stops either drifting apart again.
    """
    published = set(get_operation_metadata(operation).allowed_errors)
    raised = _refusals_by_function()
    reachable = {
        code
        for name in REACHABLE_REFUSERS[operation]
        for code in raised.get(name, set())
    }

    assert reachable, operation
    assert reachable <= published, sorted(reachable - published)


def test_every_refusing_function_in_the_module_is_routed_to_an_operation() -> None:
    """So a new refusing helper cannot escape the check above by not being listed."""
    routed = {name for names in REACHABLE_REFUSERS.values() for name in names}

    assert set(_refusals_by_function()) <= routed


def test_the_two_branches_this_catalogue_had_missed_are_published() -> None:
    """Named exactly, so a regression reads as the regression it is."""
    assert "not_found" in get_operation_metadata(WORKFLOW_START_OPERATION).allowed_errors
    assert "conflict" in get_operation_metadata(WORKFLOW_CONTROL_OPERATION).allowed_errors


def test_the_family_registers_exactly_the_four_workflow_operations(
    owned: m1.Owned,
) -> None:
    served = dispatcher(owned, releases=(release(),))

    assert served.registry.operations == {
        WORKFLOW_START_OPERATION,
        WORKFLOW_INSPECT_OPERATION,
        WORKFLOW_CONTROL_OPERATION,
        WORKFLOW_REVIEW_OPERATION,
    }


def test_the_family_session_grants_exactly_what_the_catalogue_requires(
    owned: m1.Owned,
) -> None:
    """The grant is derived from the frozen catalogue, so it cannot quietly widen."""
    served = dispatcher(owned, releases=(release(),))

    assert served.session.scopes == {"workflow:read", "workflow:write", "workflow:control"}
    assert {ref.id for ref in served.session.capabilities} == {
        "workflow.read",
        "workflow.write",
        "workflow.control",
    }
    assert served.session.purposes == set(WORKFLOW_FAMILY_PURPOSES.values())


def test_an_operation_outside_the_family_falls_through_to_the_probe(
    owned: m1.Owned,
) -> None:
    """This family serves four operations and refuses to answer for any other."""
    served = dispatcher(owned, releases=(release(),))

    assert served.registry.get("memory.get") is None
    with pytest.raises(ValueError, match="already registered"):
        served.registry.register(
            WORKFLOW_START_OPERATION, lambda context: {}
        )


def test_a_start_under_a_superseded_generation_leaves_nothing_behind(
    owned: m1.Owned,
) -> None:
    """Authority lost between entry and commit refuses rather than commits.

    The guard row is advanced underneath the dispatcher, which is what a takeover
    looks like from inside a transaction that has already begun: the handler read the
    generation that was current, and `fenced_transaction` re-validates it immediately
    before COMMIT.
    """
    served = dispatcher(owned, releases=(release(),))
    # A takeover: another service instance acquires the lease, which advances the
    # generation. The guard row this handler reads still names the old one.
    successor = m1.make_identity("svc-t0693-successor", pid=4343)
    taken = acquire_lease(
        owned.connection,
        successor,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    assert taken.fencing_generation > owned.generation

    # It escapes as `StaleGeneration` rather than as a response envelope, which is
    # this repository's existing posture: no application handler catches it, because a
    # service that has lost its lease has nothing truthful left to answer with.
    with pytest.raises(StaleGeneration):
        start(served)

    assert count(owned, RUNS) == count(owned, PLANS) == count(owned, DURABLE_JOBS) == 0


def test_a_started_run_is_a_job_the_startup_recovery_pass_classifies(
    owned: m1.Owned,
) -> None:
    """A Workflow Run is carried by an ordinary durable job, and is recovered as one.

    This is what "composes the current scheduler and startup recovery" means here, and
    it is deliberately all it means: `workflow.start` writes the exact job, application
    metadata, attempt and event rows an import claim writes, so RT-109's fenced startup
    pass sweeps a Workflow Run without knowing it is one. Nothing in this lane is a
    second scheduler, and there is no Workflow-shaped job row the generic pass would
    walk past.
    """
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))

    recovery = recover_runtime_startup(
        workflow_runtime_scheduler(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            clock=FakeClock(wall=WALL),
        )
    )

    carried = [job for job in recovery.jobs if job.run_id == run_id]
    assert len(carried) == 1
    # Admitted, no step opened, no attempt claimed: the classification RT-109 gives a
    # runtime-bound job whose work has not started, and never `contradictory_history`.
    assert carried[0].classification == CLASSIFICATION_NO_OPEN_ATTEMPT
    assert carried[0].job_id == run_id


def test_a_started_run_is_queued_work_the_scheduler_can_claim(
    owned: m1.Owned,
) -> None:
    """A started Run is executable, not merely recorded.

    Its durable job is `queued` and its plan's steps are open, which is exactly what
    RT-106 selects: `claim_next` claims the first dependency-ready step of the Workflow
    the Run is bound to. The live behaviour is exercised properly in
    `test_t0693_workflow_live_runtime.py`; what this holds is the join between the
    application lane and the scheduler.
    """
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    scheduler = workflow_runtime_scheduler(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        clock=FakeClock(wall=WALL),
    )

    claim = scheduler.claim_next()

    assert claim is not None
    assert claim.run_id == run_id
    assert owned.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (run_id,)
    ).fetchone() == ("claimed",)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_run_steps WHERE workspace_id = ? "
        "AND run_id = ?",
        (WORKSPACE_ID, run_id),
    ).fetchone() == (2,)


# --- the generic job family, over the job a Run is carried by ------------------------


def job_dispatcher(
    holder: m1.Owned, *, tag: str = "t0693-jobs", clock: FakeClock | None = None
) -> ApplicationDispatcher:
    """The real S3 job family, over the same workspace and the same durable rows."""
    return build_job_application_dispatcher(
        service=holder,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=fallback(),
        clock=FakeClock(wall=WALL) if clock is None else clock,
        allocate_identifier=allocator(tag),
    )


def job_request(
    operation: str,
    operation_input: Mapping[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    """One job-family request, under that family's own purpose rather than this one's."""
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=JOB_FAMILY_PURPOSES[operation],
        workspace_id=WORKSPACE_ID,
    )


def job_facts(holder: m1.Owned, *, run_id: str, job_id: str) -> tuple[Any, ...]:
    """Everything a control that actually did something would have moved."""
    read = holder.connection.execute
    return (
        read(
            f"SELECT state FROM {DURABLE_JOBS} WHERE job_id = ?", (job_id,)
        ).fetchall(),
        read(
            "SELECT sequence, state FROM omnivia_job_events "
            "WHERE workspace_id = ? AND job_id = ? ORDER BY sequence",
            (WORKSPACE_ID, job_id),
        ).fetchall(),
        read(
            "SELECT attempt_number, state FROM omnivia_job_attempts "
            "WHERE workspace_id = ? AND job_id = ? ORDER BY attempt_number",
            (WORKSPACE_ID, job_id),
        ).fetchall(),
        read(
            "SELECT terminal_observation_number, terminal_state "
            "FROM omnivia_job_terminal_observations "
            "WHERE workspace_id = ? AND job_id = ? "
            "ORDER BY terminal_observation_number",
            (WORKSPACE_ID, job_id),
        ).fetchall(),
        count(holder, EVENTS),
        read_run_sequence(holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id),
    )


def test_the_generic_job_family_steers_no_workflow_run(owned: m1.Owned) -> None:
    """Generic job controls refuse a Workflow Run's carrying job in every state."""
    clock = FakeClock(wall=WALL)
    served = dispatcher(owned, releases=(release(),))
    run_id = run_id_of(start(served))
    stored = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
    assert stored is not None
    jobs = job_dispatcher(owned, clock=clock)
    projected = result(
        served.dispatch(
            request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-jobs-inspect-before",
            )
        )
    )

    before = job_facts(owned, run_id=run_id, job_id=stored.job_id)
    queued = result(
        jobs.dispatch(
            job_request(
                "job.cancel",
                {"job_id": stored.job_id},
                request_id="req-jobs-cancel-queued",
                idempotency_key="idem-jobs-cancel-queued",
            )
        )
    )
    assert queued["cancellation_disposition"] == "not_cancellable"
    assert queued["job"]["identity"]["job_kind"] == workflow_handlers.WORKFLOW_JOB_KIND
    assert queued["job"]["state"] == "queued"
    assert queued["job"]["control"] == {
        "cancellation": "not_cancellable",
        "recovery": "not_retryable",
    }
    assert job_facts(owned, run_id=run_id, job_id=stored.job_id) == before

    clock.advance_wall(10)
    assert (
        claim_application_job(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            clock=clock,
            job_id=stored.job_id,
        )
        is not None
    )
    claimed = job_facts(owned, run_id=run_id, job_id=stored.job_id)
    running = result(
        jobs.dispatch(
            job_request(
                "job.cancel",
                {"job_id": stored.job_id},
                request_id="req-jobs-cancel-running",
                idempotency_key="idem-jobs-cancel-running",
            )
        )
    )
    assert running["cancellation_disposition"] == "not_cancellable"
    assert running["job"]["state"] == "running"
    assert running["job"]["control"]["cancellation"] == "not_cancellable"
    assert job_facts(owned, run_id=run_id, job_id=stored.job_id) == claimed

    clock.advance_wall(10)
    fail_application_job(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        job_id=stored.job_id,
        fencing_generation=owned.generation,
        clock=clock,
        error={
            "code": "internal_recoverable",
            "message": "transient",
            "retry_class": "retryable",
        },
    )
    failed = job_facts(owned, run_id=run_id, job_id=stored.job_id)
    retried = result(
        jobs.dispatch(
            job_request(
                "job.retry",
                {"job_id": stored.job_id},
                request_id="req-jobs-retry-failed",
                idempotency_key="idem-jobs-retry-failed",
            )
        )
    )
    assert retried["recovery_disposition"] == "not_retryable"
    assert retried["job"]["state"] == "failed"
    assert retried["job"]["control"]["recovery"] == "not_retryable"
    assert job_facts(owned, run_id=run_id, job_id=stored.job_id) == failed

    assert result(
        served.dispatch(
            request(
                WORKFLOW_INSPECT_OPERATION,
                {"run_id": run_id},
                request_id="req-jobs-inspect-after",
            )
        )
    )["run"] == projected["run"]


def test_resolving_a_pending_wait_goes_through_the_runtime_wait_authority(
    owned: m1.Owned, clock: FakeClock
) -> None:
    """A wait is resolved by RT-107, and this operation reports the run it resumed.

    The step, attempt and wait are opened directly here because nothing in this
    repository opens them yet -- that is the execution lane this tranche does not port.
    What is under test is the seam above them: `workflow.control` hands the wait
    authority a command built from the *stored* wait, and reports the Workflow
    projection of the run the resolution actually produced.
    """
    served = dispatcher(
        owned,
        releases=(release(),),
        # Every resolution permitted, and it is still the seam that decides whether the
        # resolution matches the wait's kind and the run's status.
        wait_policy=lambda *args, **kwargs: None,
        clock=clock,
    )
    run_id = run_id_of(start(served))
    _suspend(owned, run_id)
    # Past the suspension it just seeded: 0018 refuses a wait resolved before it was
    # created, which is a rule about instants rather than about ordering in Python.
    clock.advance_wall(1.0)

    answer = result(
        served.dispatch(
            request(
                WORKFLOW_CONTROL_OPERATION,
                {
                    "run_id": run_id,
                    "action": "resolve_wait",
                    "wait_id": "wait-t0693-1",
                    "resolution": "external_signal",
                    "reason": "operator.resolved",
                },
                request_id="req-resolve-ok",
                idempotency_key="idem-resolve-ok",
            )
        )
    )

    assert answer["disposition"] == "wait_resolved"
    # The run resumed: RT-107 appended a `running` event and reopened the step.
    assert answer["run"]["run_status"] == "running"
    assert answer["run"]["state"] == "running"
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_wait_resolutions WHERE wait_id = ?",
        ("wait-t0693-1",),
    ).fetchone() == ("resolved",)


def test_a_resolution_the_wait_authority_refuses_leaves_the_wait_pending(
    owned: m1.Owned, clock: FakeClock
) -> None:
    """A signal is never accepted as an approval, and the refusal is this seam's too."""
    served = dispatcher(
        owned,
        releases=(release(),),
        wait_policy=lambda *args, **kwargs: None,
        clock=clock,
    )
    run_id = run_id_of(start(served))
    _suspend(owned, run_id)
    clock.advance_wall(1.0)

    response = served.dispatch(
        request(
            WORKFLOW_CONTROL_OPERATION,
            {
                "run_id": run_id,
                "action": "resolve_wait",
                "wait_id": "wait-t0693-1",
                "resolution": "approval_decision",
                "reason": "operator.resolved",
            },
            request_id="req-resolve-bad",
            idempotency_key="idem-resolve-bad",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_wait_resolutions"
    ).fetchone() == (0,)


def _suspend(holder: m1.Owned, run_id: str) -> str:
    """Suspend the run's own first plan step on a durable wait, and name it.

    The step and its attempt are the ones `workflow.start` and the scheduler produced --
    no step is invented here, because `workflow.start` now opens the sealed plan's steps
    and a second one would not even have a contiguous ordinal. What is seeded is only the
    suspension itself: the wait, the step's `waiting` status and the run's `waiting`
    event, which is the shape RT-107 requires to resolve one.
    """
    scheduler = workflow_runtime_scheduler(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        clock=FakeClock(wall=WALL),
    )
    claim = scheduler.claim_next()
    assert claim is not None and claim.run_id == run_id
    with runtime_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.open_wait(
            wait_id="wait-t0693-1",
            run_id=run_id,
            run_step_id=claim.run_step_id,
            kind="external_signal",
            created_at_us=WALL_US + 1_300,
            resume_digest="sha256:" + "8" * 64,
        )
        writer.record_step_status(
            run_step_id=claim.run_step_id,
            status="waiting",
            observed_at_us=WALL_US + 1_400,
        )
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id="evt-t0693-waiting",
            occurred_at_us=WALL_US + 1_500,
            event_kind="wait_opened",
            run_status="waiting",
            run_step_id=claim.run_step_id,
        )
    return claim.run_step_id
