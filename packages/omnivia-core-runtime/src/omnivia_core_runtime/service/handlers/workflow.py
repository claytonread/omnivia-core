"""Production handlers for the four Workflow application operations.

`workflow.start`, `workflow.inspect`, `workflow.control` and `workflow.review` are
the public surface over the durable Workflow lane. This module is the seam between
the application wire and that lane, and it owns nothing else: no scheduler loop, no
executor, no second state machine, and no store of its own.

Where the material comes from, and why it is a seam
---------------------------------------------------

A Workflow Run is bound to an exact released Workflow: a materialised plan, and the
pinned digests -- release, execution profile, effective policy, Component
implementations, resource snapshots, model policy -- that make "this Run executes
*that*" a checkable claim rather than a label. **This repository ships no authority
that resolves a released Workflow to that material.** There is no definition store,
no release catalogue, and no execution-profile registry here; `execution/workflow.py`
is an in-memory oracle that materialises a definition somebody already has, and
`storage/workflow_runs.py` stores a plan somebody already sealed.

So the release authority is a composition seam, exactly as the Chat family's own
domain resolver is: :data:`WorkflowReleaseResolver` is supplied when the dispatcher
is composed, and a build without one refuses `workflow.start` at the domain step --
after the grant, before any write -- rather than being absent from the catalogue.
That refusal is the honest answer for a build that cannot prove what a Run would be
executing; inventing the material here would produce exactly the fabricated
execution history the binding exists to prevent.

The seam is deliberately narrow. A resolver states *material* and nothing else: the
plan and the pinned digests. It does not state the binding identity, the instant, or
the acting principal, because those are facts about this admission rather than about
the release, and a resolver that could state them could attribute a Run to a
principal that never asked for it.

What each operation is held to
------------------------------

*Start seals, admits, binds and opens the work, or writes nothing.* One
`execute_mutation` covers the durable job row, the canonical Runtime run, the sealed
plan, the `RuntimeDefinitionBinding` and the plan's canonical run steps -- so a Run with
no plan, a plan with no binding, a binding naming material this workspace never sealed,
and a Run nothing could ever execute are all states that cannot survive a commit. The
steps themselves are derived by `service/workflow_runtime.py` from the plan just sealed;
this module chooses none of them. Idempotency is the request's own `idempotency_key`, which migration
0018 requires the Run's `logical_key` to equal -- its admission trigger refuses a run
whose logical key is not the idempotency key of the claim admitting it. So a second
start under one key is answered from the stored outcome of the first, and one key
naming a different canonical request is an `idempotency_conflict` rather than a second
Run or a rebinding of the first.

*Inspect reads, and never predicts.* Every field it returns is read from durable
storage through `read_workflow_run`, whose binding projection is T-0688's verifying
reader. An unobserved step is absent rather than forecast, and the state is the
projection of the durable event stream rather than a guess at what the plan would do
next.

*Control does the thing, or says it did not.* `cancel` releases whatever the Run is
still holding -- its open attempt, its unresolved wait -- goes through the durable stop
ledger, which decides the outcome itself rather than accepting one, and then settles the
durable job that Run is carried by as `cancelled` in the same transaction, so neither
history is ever left finished beside a live counterpart. Migration 0036 is what admits
that terminal observation: it reads the accepted stop request, its outcome and this
operation's own `workflow.control` audit as the cancellation lineage, rather than making
this lane forge the `job.cancel` control it is not. A Run that was already finished
before the request arrived settles as `ignored_already_terminal`: its terminality is read
inside the same fence before anything is written, so no wait is released, no attempt is
closed and the durable job is not touched -- an open wait or attempt beside an
already-finished Run is history this operation did not write, and RT-109 reporting it is
a better answer than this lane silently closing it under a stop that changed nothing.
`resolve_wait` goes through the existing runtime wait authority, which is what
re-checks the resolution against the stored wait and the run's status, and projects this
operation's own answer inside that authority's transaction so the answer served and the
answer stored are the same bytes. An action this build does not implement is refused as
`invalid_request`; there is no branch here that reports a success for work nothing
performed.

*Review projects, and derives nothing new.* The verified journal in contiguous
sequence order, the resume eligibility the journal governance lane already computes,
and the evidence-gated completion decision if one was recorded. Deterministic in the
strict sense: the same durable rows produce the same projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_RUNNING,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    WAIT_RESOLUTION_CANCELLED,
    WAIT_STATUS_PENDING,
    ContractDecodeError,
    ContractSemanticError,
    JobReference,
    ResolveWait,
    RunDefinitionRef,
    WorkflowCompletion,
    WorkflowControlInput,
    WorkflowControlResult,
    WorkflowInspectInput,
    WorkflowInspectResult,
    WorkflowJournalEntry,
    WorkflowPlanStep,
    WorkflowReviewInput,
    WorkflowReviewResult,
    WorkflowRunProjection,
    WorkflowStartInput,
    WorkflowStartResult,
    WorkflowStepObservation,
    idempotency_equivalence,
    to_canonical_json,
)
from omnivia_core.contracts.v1.semantics_jobs import IdempotencyEquivalence
from omnivia_core.contracts.v1.semantics_runtime import is_terminal_run_status
from omnivia_core_runtime.execution.workflow import MaterialisedWorkflow
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationGrant,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    application_refusal,
)
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
from omnivia_core_runtime.service.runtime_waits import (
    WaitNotFound,
    WaitResolutionConflict,
    WaitResolutionPolicy,
    resolve_runtime_wait,
)
from omnivia_core_runtime.service.workflow_runtime import open_workflow_runtime_steps
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    RunSnapshot,
    read_run,
    read_run_sequence,
    read_run_waits,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.storage.runtime_stop import (
    STOP_OUTCOME_ACCEPTED,
    STOP_OUTCOME_IGNORED_ALREADY_TERMINAL,
    RunStopRequest,
    transaction_local_stop_writer,
)
from omnivia_core_runtime.storage.workflow_runs import (
    WorkflowRunView,
    read_workflow_run,
    transaction_local_workflow_writer,
)
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    BoundRunAdmission,
    evaluate_journal_resume,
    read_runtime_journal_events,
)

WORKFLOW_START_OPERATION: Final = "workflow.start"
WORKFLOW_INSPECT_OPERATION: Final = "workflow.inspect"
WORKFLOW_CONTROL_OPERATION: Final = "workflow.control"
WORKFLOW_REVIEW_OPERATION: Final = "workflow.review"
WORKFLOW_FAMILY_OPERATIONS: Final = frozenset(
    {
        WORKFLOW_START_OPERATION,
        WORKFLOW_INSPECT_OPERATION,
        WORKFLOW_CONTROL_OPERATION,
        WORKFLOW_REVIEW_OPERATION,
    }
)

#: The job kind and definition kind a Workflow Run is carried by. `workflow` is one of
#: the two `definition_kind` values migration 0018 admits, so this lane writes a
#: canonical generic Runtime run rather than a Workflow-only parallel record.
WORKFLOW_JOB_KIND: Final = "workflow.execute"
DEFINITION_KIND_WORKFLOW: Final = "workflow"

CONTROL_ACTION_CANCEL: Final = "cancel"
CONTROL_ACTION_RESOLVE_WAIT: Final = "resolve_wait"

DISPOSITION_CANCELLATION_ACCEPTED: Final = "cancellation_accepted"
DISPOSITION_ALREADY_TERMINAL: Final = "cancellation_ignored_already_terminal"
DISPOSITION_WAIT_RESOLVED: Final = "wait_resolved"

#: Every stop-ledger outcome this operation knows how to report, and its exact reading.
#:
#: Exhaustive by construction rather than by an `else`. The two entries here are the two
#: outcomes a cancellation may honestly publish; `rejected` -- and any outcome a later
#: build of the ledger adds -- is *absent*, so it reaches the miss branch and refuses
#: instead of being folded into `ignored_already_terminal`. A rejected stop changed
#: nothing and a run that was already finished changed nothing either, which is exactly
#: why the difference matters: only one of them means the run is terminal, and reporting a
#: refusal as an ignored already-terminal cancellation tells the caller their run has
#: finished when the ledger never said so.
_CANCELLATION_DISPOSITIONS: Final[Mapping[str, str]] = {
    STOP_OUTCOME_ACCEPTED: DISPOSITION_CANCELLATION_ACCEPTED,
    STOP_OUTCOME_IGNORED_ALREADY_TERMINAL: DISPOSITION_ALREADY_TERMINAL,
}

_EVENT_KIND_ADMITTED: Final = "run_admitted"
_BINDING_SCHEMA_VERSION: Final = "1.0.0"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_STEP_STATUS_CANCELLED: Final = "cancelled"
_JOB_STATE_QUEUED: Final = "queued"
_TERMINAL_JOB_STATES: Final = frozenset({"succeeded", "failed", "cancelled"})

_MESSAGE_INVALID: Final = (
    "the request payload is not valid for this workflow operation"
)
_MESSAGE_NOT_FOUND: Final = "the requested workflow run was not found"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative workflow storage"
)
_MESSAGE_NO_RELEASE_AUTHORITY: Final = (
    "this build cannot resolve a released workflow to the exact material a run binds"
)
_MESSAGE_NO_RELEASE: Final = "no such released workflow version is available here"
_MESSAGE_UNSUPPORTED_ACTION: Final = (
    "this build does not implement the requested workflow control action"
)
_MESSAGE_WAIT_ARGUMENTS: Final = (
    "resolving a wait requires exactly a wait_id and a resolution, and cancelling "
    "permits neither"
)
_MESSAGE_WAIT_NOT_FOUND: Final = "this run holds no such durable wait"


@dataclass(frozen=True, slots=True)
class WorkflowRelease:
    """One exact released Workflow version, and the material a Run binds to it.

    `plan` is the sealed :class:`MaterialisedWorkflow`; `material` is the pinned
    remainder of the `RuntimeDefinitionBinding` -- `releaseRef`,
    `executionProfileDigest`, `effectivePolicyDigest`,
    `componentImplementationDigests`, `resourceBindingSnapshots` and the optional
    model-policy pair.

    Deliberately not the whole binding document. `bindingId`, `boundAt` and `boundBy`
    are facts about one admission rather than about the release, so they are stamped
    by the handler from its own allocator, clock and authorised principal. A resolver
    that could state them could attribute a Run to a principal that never asked for
    it, and could name an instant the run row would then have to be reconciled
    against.
    """

    plan: MaterialisedWorkflow
    material: Mapping[str, object]


class WorkflowReleaseResolver(Protocol):
    """Resolves one exact released Workflow version to the material a Run binds.

    `None` means this build serves no such release, which is a `not_found` and never
    a licence to admit a Run against material nobody released.
    """

    def __call__(
        self, *, workflow_id: str, workflow_version: str
    ) -> WorkflowRelease | None: ...


def _instant(microseconds: int) -> str:
    """One microsecond instant as the `Timestamp` spelling the contract accepts."""
    moment = _EPOCH + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def _run_projection(view: WorkflowRunView) -> WorkflowRunProjection:
    """One stored Workflow Run, as the public projection of it.

    Every field comes off `view`, which `read_workflow_run` built from durable rows
    and T-0688's verifying binding reader. Nothing here recomputes a state, re-reads
    a binding, or fills a field the storage did not state.
    """
    return WorkflowRunProjection(
        run_id=view.binding.run_id,
        definition=RunDefinitionRef(
            definition_kind=DEFINITION_KIND_WORKFLOW,
            definition_id=view.binding.workflow_id,
            definition_version=view.binding.workflow_version,
        ),
        plan_digest=view.binding.plan_hash,
        state=view.state,
        run_status=view.run_status,
        binding=dict(view.binding_projection),
    )


@dataclass(frozen=True)
class WorkflowHandlers:
    """The four Workflow operations, over one workspace's durable storage.

    `resolve_release` is the release authority seam. It is optional and defaults to
    absent because this repository ships none; `workflow.start` refuses with
    `dependency_unavailable` when it is absent, which is a refusal a caller can act
    on rather than a crash or a silently fabricated binding.

    `wait_policy` is the wait-resolution authority the runtime wait seam requires and
    has no default for. Absent, `resolve_wait` refuses the same way: a build that
    cannot say whether a resolution is permitted must not perform one.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator
    resolve_release: WorkflowReleaseResolver | None = None
    wait_policy: WaitResolutionPolicy | None = None

    # -- the storage authority this instance is serving --

    def _authority(self) -> tuple[Any, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    def _decode(self, context: OperationContext, decoder: Any) -> Any:
        try:
            return decoder(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise application_refusal(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID
            ) from error

    def _view(self, connection: Any, workspace_id: str, run_id: str) -> WorkflowRunView:
        """One Workflow Run of *this* workspace, or a `not_found`.

        Workspace-scoped at the read, not filtered afterwards: `read_workflow_run`
        takes the workspace as a parameter, so a Run of another workspace is invisible
        rather than fetched and then hidden.
        """
        try:
            view = read_workflow_run(
                connection, workspace_id=workspace_id, run_id=run_id
            )
        except StorageError as error:
            # A stored record that cannot be believed is not a run this operation may
            # report on. Surfacing it as a refusal rather than as a projection is the
            # whole point of the verifying reader underneath.
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, str(error)
            ) from error
        if view is None:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return view

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, IdempotencyEquivalence, MutationGuard]:
        """The server-issued grant for one mutating Workflow request."""
        _connection, _identity, guard = self._authority()
        if context.authorization is None:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            payload,
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        return grant, equivalence, guard

    # -- workflow.start --------------------------------------------------------------

    def workflow_start(self, context: OperationContext) -> AuditedOperationResult:
        """Seal one released Workflow, admit a Run against it, and bind the two."""
        request: WorkflowStartInput = self._decode(
            context, WorkflowStartInput.from_wire
        )
        connection, identity, _guard = self._authority()
        if self.resolve_release is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_RELEASE_AUTHORITY
            )
        release = self.resolve_release(
            workflow_id=request.workflow_id, workflow_version=request.workflow_version
        )
        if release is None:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NO_RELEASE)
        if (
            release.plan.workflow_id != request.workflow_id
            or release.plan.version != request.workflow_version
        ):
            # A resolver that answered with a different release than it was asked for
            # would bind a Run to material the caller never named.
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                "the release authority answered with a different workflow version",
            )

        # The Run's durable logical identity is the request's own idempotency key,
        # because migration 0018 requires them to be equal: its admission trigger
        # refuses a run whose `logical_key` is not the `idempotency_key` of the claim
        # admitting it. So there is nothing to choose here and nothing for a caller to
        # state -- a second start under one key is the mutation seam's replay, and one
        # key naming a different canonical request is its `idempotency_conflict`.
        logical_key = context.request.metadata.idempotency_key
        if logical_key is None:
            raise application_refusal(
                ERROR_CODE_INVALID_REQUEST,
                "starting a workflow run requires an idempotency key",
            )
        grant, equivalence, guard = self._grant(context, request.to_wire())

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            admitted = _start_workflow_run(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                release=release,
                logical_key=logical_key,
                principal_id=context.principal,
                originating_operation=context.request.operation,
                fencing_generation=guard.fencing_generation,
                allocate_identifier=self.allocate_identifier,
            )
            view = read_workflow_run(
                fenced, workspace_id=context.workspace_id, run_id=admitted
            )
            if view is None:  # pragma: no cover - the rows were just written
                raise StorageError("a workflow run did not read back after admission")
            return WorkflowStartResult(run=_run_projection(view)).to_wire()

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                WorkflowStartResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        # Every `workflow.start` that succeeds has started durable work, so every one of
        # them says which -- the catalogue declares this operation `always_returns_job`,
        # and a response that named no job would leave the caller with a Run id and no way
        # to reach the generic job surface carrying it.
        #
        # Derived from the *persisted* result rather than from the request: the run id
        # comes off the bytes `execute_mutation` validated and stored, and the job id is
        # then read from that run's own durable row. A replay therefore answers with the
        # first call's job, not a second one, and no value a caller sent reaches this
        # reference.
        return AuditedOperationResult(
            outcome.result,
            audit_reference=outcome.audit_ref,
            job_reference=JobReference(
                job_id=self._run_job_id(
                    connection,
                    context.workspace_id,
                    WorkflowStartResult.from_wire(outcome.result).run.run_id,
                )
            ),
        )

    def _run_job_id(self, connection: Any, workspace_id: str, run_id: str) -> str:
        """The durable job the named Run is carried by, read rather than assumed.

        Migration 0018 makes `omnivia_runtime_runs.job_id` a foreign key into the
        application job metadata, so this is the one authority on the pairing. Reading it
        is what keeps the published reference true if a Run ever stops being allocated the
        same identifier as its job.
        """
        snapshot = read_run(connection, workspace_id=workspace_id, run_id=run_id)
        if snapshot is None:  # pragma: no cover - the mutation just persisted this run
            raise StorageError(
                f"workspace {workspace_id!r} holds no run {run_id!r} to reference a job for"
            )
        return snapshot.job_id

    # -- workflow.inspect ------------------------------------------------------------

    def workflow_inspect(self, context: OperationContext) -> Mapping[str, Any]:
        """One Run's current durable truth. Never a preview, never a simulation."""
        request: WorkflowInspectInput = self._decode(
            context, WorkflowInspectInput.from_wire
        )
        connection, _identity, _guard = self._authority()
        view = self._view(connection, context.workspace_id, request.run_id)
        return WorkflowInspectResult(
            run=_run_projection(view),
            plan=tuple(
                WorkflowPlanStep(
                    step_id=step.step_id,
                    component_id=step.component_id,
                    component_version=step.component_version,
                    # The route stored at sealing, not a route recomputed now: a plan
                    # is immutable once sealed, and a second router run here could
                    # publish a route this Run was never executed under.
                    route=step.route,
                    sequence_index=step.sequence_index,
                    step_definition_digest=step.step_definition_hash,
                    materialised_step_digest=step.materialised_step_hash,
                )
                for step in view.plan.steps
            ),
            observations=tuple(
                WorkflowStepObservation(
                    step_id=observation.step_id,
                    route=observation.route,
                    sequence_index=observation.sequence_index,
                    observed_at=_instant(observation.observed_at_us),
                )
                for observation in view.plan_observations
            ),
        ).to_wire()

    # -- workflow.control ------------------------------------------------------------

    def workflow_control(self, context: OperationContext) -> AuditedOperationResult:
        """Cancel one Run, or resolve one durable wait. Nothing else, and nothing quiet."""
        request: WorkflowControlInput = self._decode(
            context, WorkflowControlInput.from_wire
        )
        if request.action == CONTROL_ACTION_CANCEL:
            if request.wait_id is not None or request.resolution is not None:
                raise application_refusal(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_WAIT_ARGUMENTS
                )
            return self._cancel(context, request)
        if request.action == CONTROL_ACTION_RESOLVE_WAIT:
            if request.wait_id is None or request.resolution is None:
                raise application_refusal(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_WAIT_ARGUMENTS
                )
            # Passed narrowed, so the seam below cannot be reached with either absent:
            # the check that made them present and the code that relies on it are one
            # call apart rather than one optional field apart.
            return self._resolve_wait(
                context,
                request,
                wait_id=request.wait_id,
                resolution=request.resolution,
            )
        # An action outside the closed vocabulary -- including one a newer build
        # publishes -- is refused explicitly. Reporting a success for work nothing
        # performed is the single worst answer a control operation can give.
        raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UNSUPPORTED_ACTION)

    def _cancel(
        self, context: OperationContext, request: WorkflowControlInput
    ) -> AuditedOperationResult:
        connection, identity, _guard = self._authority()
        self._view(connection, context.workspace_id, request.run_id)
        grant, equivalence, issued_under = self._grant(context, request.to_wire())
        reason = request.reason or "operator.cancelled"

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            # What the run already is, read inside the fence and before anything is
            # written. The stop ledger decides the same question from the same event
            # stream a moment later, so this is not a second opinion -- it is the same
            # one, asked early enough to keep a stop that will settle as
            # `ignored_already_terminal` from touching either history first.
            snapshot = read_run(
                fenced, workspace_id=context.workspace_id, run_id=request.run_id
            )
            if snapshot is None:  # pragma: no cover - the handler read the run first
                raise StorageError(
                    f"workspace {context.workspace_id!r} holds no run "
                    f"{request.run_id!r}"
                )
            already_terminal = is_terminal_run_status(snapshot.status)
            # Close what the run is still holding *before* the stop makes it terminal.
            # A cancelled run whose step still has an open attempt, or whose wait is
            # still pending, is precisely what RT-109 reads as `contradictory_history`
            # -- it is the one classification that repairs nothing -- so a cancellation
            # that left either behind would be a cancellation the next startup could not
            # act on. Nothing is invented: the attempt is closed as `cancelled`, which
            # is what it was, and the step with it.
            #
            # A run that was *already* terminal is not this cancellation's to close.
            # Whatever finished it settled its own work, and an open wait or attempt
            # beside a finished run is history this operation did not write and must not
            # quietly rewrite: cancelling it would attribute the closure to a stop the
            # ledger is about to record as having changed nothing. RT-109 reports that
            # disagreement instead, which is the honest answer and the actionable one.
            if not already_terminal:
                _release_run_work(
                    fenced,
                    snapshot,
                    workspace_id=context.workspace_id,
                    at_us=settlement.settled_at_us,
                    reason=reason,
                )
            # The stop identity is the mutation's own claim. Two different requests
            # therefore cannot collide on one stop request id, and a replay of this
            # request never reaches here at all -- the seam answers it from the
            # stored outcome before `mutate` runs.
            settled = transaction_local_stop_writer(
                fenced, workspace_id=context.workspace_id
            ).stop_run(
                RunStopRequest(
                    stop_request_id=settlement.claim_id,
                    run_id=request.run_id,
                    requested_at_us=settlement.settled_at_us,
                    requested_by=context.principal,
                    reason=reason,
                    audit_ref=settlement.audit_ref,
                ),
                runtime_event_id=self.allocate_identifier("rtev"),
                occurred_at_us=settlement.settled_at_us,
                completed_at_us=settlement.settled_at_us,
            )
            # The durable job is settled here, in this same transaction, and only for a
            # cancellation the stop ledger actually accepted. A run terminal beside a
            # queued or claimed job is exactly the disagreement RT-109 reads as
            # `contradictory_history`, and leaving one behind would also leave dead
            # queued work the scheduler still lists. Nothing is forged to get it: 0036
            # admits this terminal observation through the accepted stop request, its
            # outcome and this operation's own `workflow.control` audit, so the lane
            # never has to write a `job.cancel` control it did not perform.
            #
            # Read before either history is published, so an outcome this build cannot
            # state refuses the whole transaction rather than settling a job under a
            # disposition nothing decided.
            disposition = _CANCELLATION_DISPOSITIONS.get(settled.outcome)
            if disposition is None:
                raise StorageError(
                    f"the stop ledger settled run {request.run_id!r} as "
                    f"{settled.outcome!r}, which this build cannot report as a "
                    "workflow cancellation disposition"
                )
            if settled.outcome == STOP_OUTCOME_ACCEPTED:
                _cancel_durable_job(
                    fenced,
                    workspace_id=context.workspace_id,
                    job_id=snapshot.job_id,
                    at_us=settlement.settled_at_us,
                    reason=reason,
                    service_instance_id=issued_under.service_instance_id,
                    fencing_generation=issued_under.fencing_generation,
                )
            view = read_workflow_run(
                fenced, workspace_id=context.workspace_id, run_id=request.run_id
            )
            if view is None:  # pragma: no cover - read above proved it is here
                raise StorageError("a workflow run vanished mid-cancellation")
            return WorkflowControlResult(
                run=_run_projection(view), disposition=disposition
            ).to_wire()

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=_valid_control_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def _resolve_wait(
        self,
        context: OperationContext,
        request: WorkflowControlInput,
        *,
        wait_id: str,
        resolution: str,
    ) -> AuditedOperationResult:
        connection, identity, _guard = self._authority()
        if self.wait_policy is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                "this build has no authority to decide whether a wait may be resolved",
            )
        self._view(connection, context.workspace_id, request.run_id)
        # The resume digest is the stored wait's own, read here rather than stated by
        # the caller or minted here. It addresses the state the step resumes from, so
        # a command carrying any other value would ask the wait authority to resume
        # something this wait never suspended.
        stored = next(
            (
                wait
                for wait in read_run_waits(
                    connection,
                    workspace_id=context.workspace_id,
                    run_id=request.run_id,
                )
                if wait.wait_id == wait_id
            ),
            None,
        )
        if stored is None:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_WAIT_NOT_FOUND)
        grant, equivalence, _second = self._grant(context, request.to_wire())
        command = ResolveWait(
            workspace_id=context.workspace_id,
            run_id=request.run_id,
            wait_id=wait_id,
            resolution=resolution,
            resume_digest=stored.resume_digest,
            requested_at=_instant(_wall_us(self.clock)),
            reason=request.reason or "operator.resolved",
        )
        expected = RuntimeAggregateExpectation(
            run_id=request.run_id,
            sequence=read_run_sequence(
                connection,
                workspace_id=context.workspace_id,
                run_id=request.run_id,
            ),
        )

        def project(
            fenced: Any, _resolution: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            """This operation's own answer, built where the resolution was written.

            Inside the wait authority's fenced transaction and after it recorded the
            resolution, so the run this reads is the run the resolution produced and the
            bytes returned here are the bytes the mutation seam validates, stores and
            replays. Building it outside would publish one answer and store another, and
            would rebuild a replay's wrapper from a run that has since moved on.
            """
            resumed = read_workflow_run(
                fenced, workspace_id=context.workspace_id, run_id=request.run_id
            )
            if resumed is None:  # pragma: no cover - the read above proved it is here
                raise StorageError("a workflow run vanished mid-resolution")
            return WorkflowControlResult(
                run=_run_projection(resumed), disposition=DISPOSITION_WAIT_RESOLVED
            ).to_wire()

        try:
            outcome = resolve_runtime_wait(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                command=command,
                policy=self.wait_policy,
                runtime_event_id=self.allocate_identifier("rtev"),
                validate_result=_valid_control_result,
                clock=self.clock,
                expected=expected,
                project_result=project,
            )
        except WaitNotFound as error:
            raise application_refusal(ERROR_CODE_NOT_FOUND, str(error)) from error
        except WaitResolutionConflict as error:
            raise application_refusal(ERROR_CODE_CONFLICT, str(error)) from error
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    # -- workflow.review -------------------------------------------------------------

    def workflow_review(self, context: OperationContext) -> Mapping[str, Any]:
        """One deterministic projection of a Run's durable history."""
        request: WorkflowReviewInput = self._decode(
            context, WorkflowReviewInput.from_wire
        )
        connection, _identity, _guard = self._authority()
        view = self._view(connection, context.workspace_id, request.run_id)
        try:
            events = read_runtime_journal_events(
                connection, workspace_id=context.workspace_id, run_id=request.run_id
            )
            eligibility = evaluate_journal_resume(
                connection, workspace_id=context.workspace_id, run_id=request.run_id
            )
        except StorageError as error:
            # A journal that cannot be recomputed whole is refused rather than
            # returned with its gap silently closed.
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, str(error)
            ) from error
        return WorkflowReviewResult(
            run=_run_projection(view),
            journal=tuple(
                WorkflowJournalEntry(
                    sequence=sequence,
                    bundle_id=stored.bundle_id,
                    event=dict(stored.event),
                    event_digest=stored.content_address,
                )
                for sequence, stored in enumerate(events)
            ),
            resumable=eligibility.resumable,
            resume_diagnostic=eligibility.diagnostic,
            completion=_completion(view),
        ).to_wire()


def _valid_control_result(wire: Mapping[str, Any]) -> bool:
    try:
        WorkflowControlResult.from_wire(wire)
    except (ContractDecodeError, ContractSemanticError):
        return False
    return True


def _completion(view: WorkflowRunView) -> WorkflowCompletion | None:
    """The recorded completion decision, or nothing at all.

    `None` is the whole answer for a Run that has not completed. A provisional
    completion would be a decision nobody made and no evidence gated.
    """
    if view.completion is None:
        return None
    return WorkflowCompletion(
        outcome=view.completion.outcome,
        decided_at=_instant(view.completion.decided_at_us),
        audit_reference=view.completion.audit_ref,
    )


def _wall_us(clock: Clock) -> int:
    """The server's own wall reading, as the microsecond instant storage holds."""
    return int((clock.wall_time() - _EPOCH) // timedelta(microseconds=1))


def _release_run_work(
    connection: Any,
    snapshot: RunSnapshot,
    *,
    workspace_id: str,
    at_us: int,
    reason: str,
) -> None:
    """Close whatever a run is still holding, so cancelling it leaves nothing open.

    An unresolved `Wait` and an open `Attempt` are the two things a cancelled run must not
    still hold: both are read by RT-109 as evidence that work is in flight, and a run that
    is finished and in flight at once is history no startup pass can act on. Both are
    settled as `cancelled` -- the honest reading, since a cancellation is exactly what
    ended them -- and the step they belong to with them.

    Nothing else is touched. Steps that were never started stay `pending`: they describe
    work the plan called for and this run will not do, and marking them terminal would
    state an outcome nothing produced. A terminal run admits no claim, so they are read
    by nothing.

    Called only for a run this cancellation actually stops. The snapshot is the caller's,
    read once inside the same fence and used for both decisions, so what is closed here is
    exactly the work the terminality check was made against.
    """
    writer = transaction_local_writer(connection, workspace_id=workspace_id)
    for wait in snapshot.waits:
        if wait.status == WAIT_STATUS_PENDING:
            writer.close_wait(
                wait_id=wait.wait_id,
                status=WAIT_RESOLUTION_CANCELLED,
                resolved_at_us=at_us,
                resolution_reason=reason,
            )
    for step in snapshot.steps:
        for attempt in step.attempts:
            if attempt.status == ATTEMPT_STATUS_RUNNING:
                writer.finish_attempt(
                    attempt_id=attempt.attempt_id,
                    status=ATTEMPT_STATUS_CANCELLED,
                    finished_at_us=at_us,
                )
                writer.record_step_status(
                    run_step_id=step.run_step_id,
                    status=_STEP_STATUS_CANCELLED,
                    observed_at_us=at_us,
                )


def _cancel_durable_job(
    connection: Any,
    *,
    workspace_id: str,
    job_id: str,
    at_us: int,
    reason: str,
    service_instance_id: str,
    fencing_generation: int,
) -> None:
    """Settle the durable job a cancelled Run is carried by, in the caller's transaction.

    The statements 0015 requires of a `cancelled` terminal observation, in the one order
    its own guards admit: the scheduler row first, because 0010 refuses an attempt
    terminalized as `cancelled` beside a job that is not; then whichever application
    attempt is open, because that attempt is work this cancellation is what ended; then
    the `cancelled` job event, because 0015 requires the observation to name the job's
    final event at its own instant; then the observation itself.

    **Three shapes of job, and the smallest truthful record of each.**

    *Queued, never attempted.* Nothing is claimed and no attempt is opened: 0015 admits a
    `cancelled` observation naming no attempt when the job has none, and that is exactly
    what a Run cancelled before anything picked it up did. Opening an attempt to satisfy
    a shape would state that this instance started work it never started.

    *Claimed.* The running attempt is the one that was actually interrupted, so it is
    closed as `cancelled` at this instant and the observation names it.

    *Queued after a recovery.* RT-106 requeues an interrupted job with its attempt
    already `failed` and a terminal observation recorded for it, and 0015 will not accept
    a later `cancelled` observation over a job whose last attempt failed -- nor a
    post-terminal `cancelled` event with no attempt to explain it. So this branch claims
    the job and opens one further attempt, which is a true statement: this instance did
    take the job, to cancel it. The claim, its `running` event, its `cancelled`
    settlement and the observation are all in this one transaction, so no other reader
    ever sees the job claimed by a worker that is not running it. 0015's own
    later-attempt rule is what admits that attempt at all, and it admits it only against
    the recovery lineage already durable.

    Idempotent by state rather than by a second ledger: a job already `succeeded`,
    `failed` or `cancelled` is left exactly as it is, so a late cancellation cannot write
    a second terminal observation or downgrade a finished job. The stop ledger refuses to
    accept a stop for a terminal *run* first, so this is the second of two guards rather
    than the only one.
    """
    row = connection.execute(
        "SELECT j.state, (SELECT MAX(a.attempt_number) FROM omnivia_job_attempts a "
        "WHERE a.workspace_id = m.workspace_id AND a.job_id = j.job_id) "
        "FROM omnivia_durable_jobs j "
        "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
        "WHERE m.workspace_id = ? AND j.job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    if row is None:  # pragma: no cover - 0018 requires the run to name a job row
        raise StorageError(
            f"run job {job_id!r} is not a durable job of workspace {workspace_id!r}"
        )
    if str(row[0]) in _TERMINAL_JOB_STATES:
        return
    if str(row[0]) == _JOB_STATE_QUEUED and row[1] is not None:
        _claim_for_cancellation(
            connection,
            workspace_id=workspace_id,
            job_id=job_id,
            at_us=at_us,
            service_instance_id=service_instance_id,
            fencing_generation=fencing_generation,
        )
    connection.execute(
        "UPDATE omnivia_durable_jobs SET state = 'cancelled', updated_at = ?, "
        "claimed_by_service_instance = NULL, fencing_generation = ? WHERE job_id = ?",
        (_job_moment(at_us), fencing_generation, job_id),
    )
    connection.execute(
        "UPDATE omnivia_job_attempts SET state = 'cancelled', finished_at_us = ? "
        "WHERE workspace_id = ? AND job_id = ? AND state = 'running'",
        (at_us, workspace_id, job_id),
    )
    connection.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "SELECT ?, ?, COALESCE(MAX(sequence), -1) + 1, ?, 'cancelled', "
        "'workflow run cancelled' FROM omnivia_job_events "
        "WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id, at_us, workspace_id, job_id),
    )
    connection.execute(
        "INSERT INTO omnivia_job_terminal_observations "
        "(workspace_id, job_id, terminal_observation_number, attempt_number, "
        "terminal_state, finished_at_us, result_kind, result_json, error_json, "
        "cancellation_reason, provenance_kind, fencing_generation) "
        "SELECT ?, ?, "
        "(SELECT COALESCE(MAX(terminal_observation_number), 0) + 1 "
        " FROM omnivia_job_terminal_observations WHERE workspace_id = ? AND job_id = ?), "
        "(SELECT MAX(attempt_number) FROM omnivia_job_attempts "
        " WHERE workspace_id = ? AND job_id = ?), "
        "'cancelled', ?, NULL, NULL, NULL, ?, 'service_committed', ?",
        (
            workspace_id,
            job_id,
            workspace_id,
            job_id,
            workspace_id,
            job_id,
            at_us,
            reason,
            fencing_generation,
        ),
    )


def _claim_for_cancellation(
    connection: Any,
    *,
    workspace_id: str,
    job_id: str,
    at_us: int,
    service_instance_id: str,
    fencing_generation: int,
) -> None:
    """Take a requeued job and open the one attempt its cancellation is recorded on.

    Three statements, in the order 0015 requires and no other: the claim, because an
    attempt may only be inserted against a job claimed by this instance at this
    generation; the attempt, whose number 0015 holds contiguous and inside the job's own
    budget and whose predecessor its later-attempt rule checks against the durable
    recovery lineage; and the `running` event, which is the same fact the scheduler's own
    claim records.

    Nothing here is a claim on the *work*. The transaction that opens this attempt is the
    transaction that cancels it, so the only history it can leave behind is one attempt
    that was taken and cancelled at the same instant -- which is what happened.
    """
    connection.execute(
        "UPDATE omnivia_durable_jobs SET state = 'claimed', updated_at = ?, "
        "claimed_by_service_instance = ?, fencing_generation = ? "
        "WHERE job_id = ? AND state = 'queued'",
        (_job_moment(at_us), service_instance_id, fencing_generation, job_id),
    )
    connection.execute(
        "INSERT INTO omnivia_job_attempts "
        "(workspace_id, job_id, attempt_number, started_at_us, state) "
        "SELECT ?, ?, COALESCE(MAX(attempt_number), 0) + 1, ?, 'running' "
        "FROM omnivia_job_attempts WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id, at_us, workspace_id, job_id),
    )
    connection.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "SELECT ?, ?, COALESCE(MAX(sequence), -1) + 1, ?, 'running', "
        "'workflow run cancellation claimed the requeued job' "
        "FROM omnivia_job_events WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id, at_us, workspace_id, job_id),
    )


def _job_moment(at_us: int) -> str:
    """One microsecond instant, as `omnivia_durable_jobs` spells its timestamps."""
    return (
        (_EPOCH + timedelta(microseconds=at_us)).isoformat().replace("+00:00", "Z")
    )


def _start_workflow_run(
    connection: Any,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    release: WorkflowRelease,
    logical_key: str,
    principal_id: str,
    originating_operation: str,
    fencing_generation: int,
    allocate_identifier: IdentifierAllocator,
) -> str:
    """Seal one plan, admit one canonical Run, bind them and open its steps, in the caller's transaction.

    Every statement is issued on the fenced connection the mutation seam opened, so a
    plan sealed without its Run, a Run admitted without its binding, a binding naming
    material this workspace never sealed, and a Run with no steps to execute are all
    states that are rolled back together.

    Reached only for a request the mutation seam had no stored answer for, so this
    always admits. A second start under one idempotency key is answered from the
    stored outcome without running this at all, and one key naming a different
    canonical request is refused as an `idempotency_conflict` before it. Two further
    guards in 0018 make that a property of the schema rather than of this ordering: a
    logical key already naming a Run refuses, and so does a claim that already
    admitted one.

    Re-sealing the same plan is a replay and writes nothing -- an operator may start
    many Runs of one released Workflow, and each is a Run of the same sealed plan --
    while re-sealing *different* content under one version is a conflict, because 0027
    makes a plan immutable once it has admitted a Run.
    """
    plan = release.plan
    writer = transaction_local_workflow_writer(connection, workspace_id=workspace_id)
    sealed = writer.seal_plan(
        plan, sealed_at_us=settlement.settled_at_us, audit_ref=settlement.audit_ref
    )

    run_id = allocate_identifier("run")
    binding = {
        "bindingSchemaVersion": _BINDING_SCHEMA_VERSION,
        "bindingId": allocate_identifier("binding"),
        "workflowId": plan.workflow_id,
        "workflowVersion": plan.version,
        "definitionDigest": plan.definition_hash,
        **dict(release.material),
        "boundAt": _instant(settlement.settled_at_us),
        "boundBy": {"principalId": principal_id},
    }
    admission = BoundRunAdmission(
        run_id=run_id,
        workflow_id=plan.workflow_id,
        workflow_version=plan.version,
        plan_hash=sealed.plan_hash,
        bound_at_us=settlement.settled_at_us,
        binding=binding,
    )

    _insert_workflow_job(
        connection,
        settlement,
        workspace_id=workspace_id,
        job_id=run_id,
        workflow_id=plan.workflow_id,
        workflow_version=plan.version,
        originating_operation=originating_operation,
        fencing_generation=fencing_generation,
    )
    transaction_local_writer(connection, workspace_id=workspace_id).admit_run(
        RunAdmission(
            run_id=run_id,
            job_id=run_id,
            claim_id=settlement.claim_id,
            definition=RunDefinitionRef(
                definition_kind=DEFINITION_KIND_WORKFLOW,
                definition_id=plan.workflow_id,
                definition_version=plan.version,
            ),
            logical_key=logical_key,
            originating_operation=originating_operation,
            audit_ref=settlement.audit_ref,
            admitted_at_us=settlement.settled_at_us,
            runtime_event_id=allocate_identifier("rtev"),
            event_kind=_EVENT_KIND_ADMITTED,
        )
    )
    writer.admit_run(admission)
    # The Run's canonical steps, derived from the plan just sealed, in this same
    # transaction. A Run admitted without them would be durable, inspectable and
    # permanently unexecutable: RT-106 claims a runnable *step*, so a Run with none is a
    # queued row nothing can ever pick up. Deriving them here is also what makes the four
    # records and the work they describe one commit rather than two.
    open_workflow_runtime_steps(
        connection,
        workspace_id=workspace_id,
        run_id=run_id,
        plan=sealed,
        opened_at_us=settlement.settled_at_us,
    )
    return run_id


def _insert_workflow_job(
    connection: Any,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    job_id: str,
    workflow_id: str,
    workflow_version: str,
    originating_operation: str,
    fencing_generation: int,
) -> None:
    """The durable job row a canonical Run is carried by.

    Migration 0018 requires one: `omnivia_runtime_runs.job_id` is a foreign key into
    `omnivia_job_application_metadata`, so a Workflow Run without a job row is not a
    canonical Run at all.

    **Queued, not claimed, and with no attempt open.** An import claim writes its job
    as `claimed` with a running attempt because the operation that wrote it is the work
    -- the ingestion happens on that request. Starting a Workflow Run is not: it admits
    work for a scheduler to pick up, and nothing here executes a step. Writing it
    `claimed` would state that this service instance is running it, which RT-109 reads
    exactly as it should -- `contradictory_history`, "the durable job is claimed and
    its run holds no open attempt" -- and which would strand the Run, because
    `RuntimeScheduler.claim_next` only claims a *queued* row. Queued is both the true
    statement and the one that makes the Run reachable.
    """
    moment = _job_moment(settlement.settled_at_us)
    # The accepted canonical encoder, not a hand-built string: a workflow id carrying a
    # quote or a backslash would otherwise be concatenated straight into the payload and
    # produce a row whose `payload_json` is not JSON at all.
    payload = to_canonical_json(
        {"workflow_id": workflow_id, "workflow_version": workflow_version}
    )
    connection.execute(
        "INSERT INTO omnivia_durable_jobs "
        "(job_id, job_type, state, payload_json, created_at, updated_at, "
        "fencing_generation) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (job_id, WORKFLOW_JOB_KIND, payload, moment, moment, fencing_generation),
    )
    connection.execute(
        "INSERT INTO omnivia_job_application_metadata "
        "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
        "created_at_us, terminal_result_kind, supports_checkpoint_resume, max_attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 1, 3)",
        (
            workspace_id,
            job_id,
            WORKFLOW_JOB_KIND,
            originating_operation,
            settlement.audit_ref,
            settlement.settled_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "VALUES (?, ?, 0, ?, 'queued', 'workflow run admitted')",
        (workspace_id, job_id, settlement.settled_at_us),
    )


__all__ = [
    "CONTROL_ACTION_CANCEL",
    "CONTROL_ACTION_RESOLVE_WAIT",
    "DISPOSITION_ALREADY_TERMINAL",
    "DISPOSITION_CANCELLATION_ACCEPTED",
    "DISPOSITION_WAIT_RESOLVED",
    "WORKFLOW_CONTROL_OPERATION",
    "WORKFLOW_FAMILY_OPERATIONS",
    "WORKFLOW_INSPECT_OPERATION",
    "WORKFLOW_JOB_KIND",
    "WORKFLOW_REVIEW_OPERATION",
    "WORKFLOW_START_OPERATION",
    "WorkflowHandlers",
    "WorkflowRelease",
    "WorkflowReleaseResolver",
]
