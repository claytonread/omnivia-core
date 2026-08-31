"""The live Workflow application dependency ``WorkflowHandlers`` delegates to.

This is the first live binding of the CP-03 Workflow family to durable Core truth.
``workflow.inspect`` projects a canonical ``Run`` out of stored runtime and workflow
rows, and refuses whenever those rows cannot assemble one. ``workflow.start`` admits a
new Workflow Run through the normal application mutation fence: the dispatcher-owned
session and binding issue the mutation grant, the idempotency claim is materialised
before the domain rows that foreign-key it, and the durable job, Runtime Run, Workflow
plan binding, policy snapshot, budget snapshot and capability grants commit together.

**Why ``workflow.start`` is narrow.** It admits only a released Workflow definition
whose sealed plan is already stored in this workspace, and it does not reconstruct any
authority from request metadata. A service without storage, identity, a live mutation
guard, dispatcher-owned application authority or configured policy/budget decision
authority refuses before writing anything. A replay is answered from the stored
idempotency outcome rather than by running the admission again.

``workflow.review`` answers from exactly the durable rows ``workflow.inspect`` reads --
the same 0027 binding and the same canonical ``Run`` -- and its ``review`` projection is
a restatement of that one aggregate. Nothing in it is sourced from a Simulation, a
preview, a proof record or a fixture, and it holds no fact the served ``Run`` does not
already carry, so a caller comparing the two can never find the projection asserting
something the aggregate does not.

``workflow.control`` resolves the same durable target and then refuses to act.
Every first-release action is answered ``unsupported`` with the unchanged canonical
``Run`` it reread, because nothing binds this application runtime to a
:class:`~service.workflow_scheduler.WorkflowScheduler`
or to an executor: no request that arrives here is executing, so nothing can be paused,
resumed or released, and a wait resolved through this seam would be picked up by nobody.
Driving the RT-207 stop ledger from here would settle a run that was never running and
publish a ``cancelled`` status as though a control had reached work -- a state transition
in the rows and a fiction in the world. The contract carries a disposition for exactly
this so it does not have to be faked. The target is still resolved first, so an unknown
or non-Workflow run is a ``not_found`` rather than a polite disposition about an
identifier nobody ever admitted.

**Nothing is derived that is not stored.** The canonical ``Run`` requires a
``PolicySnapshot`` and a ``BudgetSnapshot``; a run whose decisions were never
recorded has neither, and this module refuses rather than substituting a default,
because an invented budget is a statement about what a run was allowed to spend that
nobody ever made. The assembled aggregate is then put through
:func:`~contracts.v1.validate_run` before it is served, so durable state that cannot
form a coherent run is a refusal rather than an incoherent answer.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, NoReturn

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_STALE_PROJECTION,
    ContractDecodeError,
    ContractSemanticError,
    Run,
    RunDefinitionRef,
    WorkflowControlInput,
    WorkflowControlResult,
    WorkflowInspectInput,
    WorkflowInspectResult,
    WorkflowReviewInput,
    WorkflowReviewResult,
    WorkflowStartInput,
    WorkflowStartResult,
    idempotency_equivalence,
    validate_run,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock, SystemClock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    OperationContext,
    application_refusal,
)
from omnivia_core_runtime.service.workflow_policy import (
    DecisionRefused,
    EffectivePolicy,
    resolve_effective_policy,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    RunSnapshot,
    read_run,
    runtime_timestamp,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.jobs import start_workflow_run_job
from omnivia_core_runtime.storage.workflow_runs import (
    WorkflowPlan,
    WorkflowRunAdmission,
    read_workflow_plan,
    read_workflow_run,
    transaction_local_workflow_writer,
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this workflow operation"
_MESSAGE_NOT_FOUND: Final = "this workspace holds no Workflow Run with that identifier"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative workflow storage"
)
_MESSAGE_STALE_PROJECTION: Final = (
    "the requested workflow projection version does not name this run or is ahead "
    "of the durable event sequence this service can serve"
)
_MESSAGE_NO_DECISIONS: Final = (
    "durable state holds no policy and budget decision for this run, and a canonical "
    "Run states both; this build will not substitute a default for a decision nobody "
    "recorded"
)
_MESSAGE_INCOHERENT: Final = (
    "durable state for this run does not assemble a coherent canonical Run"
)
#: Refused when this service instance has no configured policy/budget decision
#: authority at all. The seam exists and resolves; what is missing is the statement of
#: which sources are authoritative for this workspace, and a run admitted without one
#: would be admitted under a policy nobody decided.
_MESSAGE_NO_AUTHORITY: Final = (
    "this service instance has no policy and budget decision authority configured, so "
    "nothing states which capabilities a new Workflow Run is granted or what cost and "
    "token ceilings it is admitted under; this seam will not invent them"
)
#: Refused when an authority is configured but cannot resolve a coherent decision. The
#: resolver's own reason is appended, because "misconfigured" without saying which
#: source is unusable is a refusal an operator cannot act on.
_MESSAGE_BAD_AUTHORITY: Final = (
    "the configured policy and budget decision authority does not resolve a coherent "
    "decision for this workspace"
)
_MESSAGE_NO_APPLICATION_AUTHORITY: Final = (
    "this live workflow runtime was not bound to dispatcher-owned application "
    "session and service-binding authority, so workflow.start cannot issue a "
    "server mutation grant; this seam will not reconstruct authority from request "
    "metadata"
)
_MESSAGE_NO_CONTROL_AUTHORITY: Final = (
    "this live workflow runtime was not bound to dispatcher-owned application "
    "session and service-binding authority, so workflow.control cannot answer a "
    "mutating control request; this seam will not reconstruct authority from "
    "request metadata"
)
_MESSAGE_NO_IDENTITY: Final = (
    "this service instance has no owned workspace identity or live mutation guard for "
    "workflow.start admission"
)
_MESSAGE_NO_CONTROL_IDENTITY: Final = (
    "this service instance has no owned workspace identity or live mutation guard for "
    "workflow.control"
)
#: Refused when the named definition has no sealed plan in this workspace. Checked
#: before the mutation opens, so a caller naming a workflow nobody released leaves the
#: database exactly as it found it rather than a job and a run bound to nothing.
_MESSAGE_NO_PLAN: Final = (
    "this workspace holds no sealed plan for that workflow definition and version, so "
    "there is nothing for a run to be admitted onto"
)
#: Refused when the admission transaction itself could not settle. The stored rules --
#: the fence, the admission guards, the plan pins, the snapshot progressions -- refused
#: it, so nothing was written; naming the composition rather than the caller's request
#: is what stops an operator reading this as a malformed payload.
_MESSAGE_ADMISSION_FAILED: Final = (
    "durable state refused this Workflow Run admission, so nothing was recorded"
)

#: Answered for every `workflow.control` action. This build binds no Workflow scheduler,
#: so there is no running work for a control to reach; the disposition says exactly that
#: rather than reporting a transition nothing performed.
_MESSAGE_NO_CONTROL_SEAM: Final = (
    "this live workflow application runtime is bound to no Workflow scheduler or "
    "executor, so no control action reaches running work; the action is reported "
    "unsupported rather than recorded as a transition nothing performed"
)

#: The two dispositions `WorkflowStartResult.admission` states. `created` is the run
#: this request admitted; `replayed` is the same run answered from the stored outcome of
#: an earlier identical request, which is what makes a retry safe rather than a second
#: run.
ADMISSION_CREATED: Final = "created"
ADMISSION_REPLAYED: Final = "replayed"

#: The operation this seam admits under. The run's `originating_operation`, the claim's
#: operation and the job's are all this one value, because 0018's admission guard
#: requires the three to agree.
WORKFLOW_START_OPERATION: Final = "workflow.start"

#: The `WorkflowControlResult.disposition` this build states. The contract lists it
#: first among the codes a first-release implementation may answer with.
CONTROL_DISPOSITION_UNSUPPORTED: Final = "unsupported"


@dataclass(frozen=True)
class WorkflowApplicationRuntime:
    """One workspace's live Workflow dependency, read off the owning service.

    ``service`` is the same object the application dispatcher holds, and its
    ``connection`` is read at call time rather than captured: the runtime is bound
    before startup acquires the workspace, and until it has one every operation
    refuses instead of answering from nothing.
    """

    service: Any
    session: AuthenticatedSession | None = None
    binding: ServiceBinding | None = None
    clock: Clock | None = None

    def workflow_inspect(self, context: OperationContext) -> Mapping[str, Any]:
        """One canonical Runtime ``Run``, projected from stored rows or refused."""
        invalid_request = False
        request: WorkflowInspectInput | None = None
        try:
            request = WorkflowInspectInput.from_wire(context.request.input)
        except ContractDecodeError:
            invalid_request = True
        if invalid_request:
            _refuse(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        run = _canonical_run(self._workflow_run(context, request.run_id))
        _require_served_projection(request.projection_version, run)
        return WorkflowInspectResult(
            run=run, projection_version=_projection_version(run)
        ).to_wire()

    def workflow_review(self, context: OperationContext) -> Mapping[str, Any]:
        """The same canonical ``Run`` inspect serves, plus a review of that run.

        Deliberately the same durable read rather than a second one: a review that could
        disagree with the inspection of the same run would be two truths about one run,
        and the aggregate is the one that counts.
        """
        invalid_request = False
        request: WorkflowReviewInput | None = None
        try:
            request = WorkflowReviewInput.from_wire(context.request.input)
        except ContractDecodeError:
            invalid_request = True
        if invalid_request:
            _refuse(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        run = _canonical_run(self._workflow_run(context, request.run_id))
        _require_served_projection(request.projection_version, run)
        return _served(
            WorkflowReviewResult(
                run=run,
                review=_review_projection(run),
                projection_version=_projection_version(run),
            ).to_wire(),
            WorkflowReviewResult.from_wire,
        )

    def workflow_control(self, context: OperationContext) -> Mapping[str, Any]:
        """An explicit ``unsupported`` disposition over a real durable run.

        The target is resolved before the refusal so the answer distinguishes "this
        build cannot do that to that run" from "that run does not exist here". Nothing
        is written, and no ``run`` is returned, because nothing changed.
        """
        invalid_request = False
        request: WorkflowControlInput | None = None
        try:
            request = WorkflowControlInput.from_wire(context.request.input)
        except ContractDecodeError:
            invalid_request = True
        if invalid_request:
            _refuse(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        self._control_authority(context)
        run = _canonical_run(self._workflow_run(context, request.run_id))
        return _served(
            WorkflowControlResult(
                run_id=run.run_id,
                disposition=CONTROL_DISPOSITION_UNSUPPORTED,
                run=run,
                details={
                    "action": request.action,
                    "reason": _MESSAGE_NO_CONTROL_SEAM,
                    "run_status": run.status,
                },
            ).to_wire(),
            WorkflowControlResult.from_wire,
        )

    def workflow_start(self, context: OperationContext) -> Mapping[str, Any]:
        """Admit one Workflow Run through the application mutation fence."""
        invalid_request = False
        request: WorkflowStartInput | None = None
        try:
            request = WorkflowStartInput.from_wire(context.request.input)
        except ContractDecodeError:
            invalid_request = True
        if invalid_request:
            _refuse(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        connection, identity, guard = self._admission_authority(context)
        decision = self.effective_policy()
        unavailable_plan = False
        plan: WorkflowPlan | None = None
        try:
            plan = read_workflow_plan(
                connection,
                workspace_id=context.workspace_id,
                workflow_id=request.definition_id,
                workflow_version=request.definition_version,
            )
        except StorageError:
            unavailable_plan = True
        if unavailable_plan:
            _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_INCOHERENT)
        if plan is None:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NO_PLAN)

        assert context.authorization is not None
        assert self.session is not None
        assert self.binding is not None
        assert self.clock is not None
        admission_failed = False
        try:
            equivalence = idempotency_equivalence(
                WORKFLOW_START_OPERATION,
                context.request.metadata,
                request.to_wire(),
                principal_id=context.authorization.principal_id,
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
            outcome = execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                mutate=lambda transaction, settlement: self._admit_start(
                    transaction,
                    settlement,
                    request=request,
                    plan=plan,
                    decision=decision,
                    logical_key=context.authorization.idempotency_key or "",
                    scopes=tuple(context.authorization.scopes),
                    purpose=context.authorization.purpose,
                ),
                validate_result=_valid_start_result,
                clock=self.clock,
                materialise_claim_before_mutation=True,
            )
        except (ContractSemanticError, StorageError, sqlite3.Error):
            admission_failed = True
        if admission_failed:
            _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_ADMISSION_FAILED)
        if outcome.replayed:
            result = dict(outcome.result)
            result["admission"] = ADMISSION_REPLAYED
            if not _valid_start_result(result):
                raise application_refusal(
                    ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_INCOHERENT
                )
            return result
        return outcome.result

    def _admit_start(
        self,
        connection: sqlite3.Connection,
        settlement: MutationSettlementContext,
        *,
        request: WorkflowStartInput,
        plan: WorkflowPlan,
        decision: EffectivePolicy,
        logical_key: str,
        scopes: tuple[str, ...],
        purpose: str,
    ) -> Mapping[str, Any]:
        """Write the job, Runtime Run, Workflow binding and decision in one fence."""
        assert self.clock is not None
        created_at_us = settlement.settled_at_us
        run_id = f"run-{uuid.uuid4()}"
        job_id = f"job-{uuid.uuid4()}"
        runtime_writer = transaction_local_writer(
            connection, workspace_id=plan.workspace_id
        )
        workflow_writer = transaction_local_workflow_writer(
            connection, workspace_id=plan.workspace_id
        )
        start_workflow_run_job(
            connection,
            settlement,
            workspace_id=plan.workspace_id,
            job_id=job_id,
            payload=request.to_wire(),
            created_at_us=created_at_us,
        )
        runtime_writer.admit_run(
            RunAdmission(
                run_id=run_id,
                job_id=job_id,
                claim_id=settlement.claim_id,
                definition=RunDefinitionRef(
                    definition_kind="workflow",
                    definition_id=request.definition_id,
                    definition_version=request.definition_version,
                ),
                logical_key=logical_key,
                originating_operation=WORKFLOW_START_OPERATION,
                audit_ref=settlement.audit_ref,
                admitted_at_us=created_at_us,
                runtime_event_id=f"evt-{uuid.uuid4()}",
                message="workflow run admitted",
            )
        )
        workflow_writer.admit_workflow_run(
            WorkflowRunAdmission(
                run_id=run_id,
                workflow_id=request.definition_id,
                workflow_version=request.definition_version,
                plan_hash=plan.plan_hash,
                bound_at_us=created_at_us,
            )
        )
        run_decision = decision.decide(
            workspace_id=plan.workspace_id,
            run_id=run_id,
            pinned_at=runtime_timestamp(_first_canonical_instant_at_or_after(created_at_us)),
            audit_reference=settlement.audit_ref,
            scopes=tuple(sorted(scopes)),
            purpose=purpose,
        )
        runtime_writer.append_policy_snapshot(run_decision.policy)
        runtime_writer.append_budget_snapshot(run_decision.budget)
        for capability_grant in run_decision.grants:
            runtime_writer.issue_capability_grant(capability_grant)
        snapshot = read_run(connection, workspace_id=plan.workspace_id, run_id=run_id)
        if snapshot is None:
            raise StorageError("admitted Workflow Run did not read back")
        result = WorkflowStartResult(
            run=_canonical_run(snapshot), admission=ADMISSION_CREATED
        ).to_wire()
        if not _valid_start_result(result):
            raise StorageError("admitted Workflow Run is not a valid start result")
        return result

    def effective_policy(self) -> EffectivePolicy:
        """The decision this workspace's configured authority resolves, or a refusal.

        Separate from :meth:`workflow_start` because the admission C3 will build needs
        the same resolution inside its transaction, and because a resolution that
        needs no run is the only kind that can be checked before one is allocated.
        """
        sources = getattr(self.service, "workflow_decision_authority", None)
        if sources is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_AUTHORITY
            )
        refused_message: str | None = None
        policy: EffectivePolicy | None = None
        try:
            policy = resolve_effective_policy(sources)
        except DecisionRefused as refused:
            refused_message = f"{_MESSAGE_BAD_AUTHORITY}: {refused}"
        if refused_message is not None:
            _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, refused_message)
        assert policy is not None
        return policy

    def _workflow_run(self, context: OperationContext, run_id: str) -> RunSnapshot:
        """The stored run behind one Workflow Run identifier, or a refusal.

        Both reads, because either one alone would answer a different question: the view
        proves this is a Workflow Run bound to a sealed plan, and the snapshot is the run
        history the canonical aggregate is made of. Nothing on the view has a field on
        `Run`, so none of it is projected. Every operation in this family that names a run
        asks exactly this question, so an agent-component run is out of the family's reach
        in one place rather than in three that could drift apart.
        """
        connection = self._connection()
        unavailable_run = False
        view = None
        snapshot = None
        try:
            view = read_workflow_run(
                connection, workspace_id=context.workspace_id, run_id=run_id
            )
            snapshot = (
                None
                if view is None
                else read_run(
                    connection, workspace_id=context.workspace_id, run_id=run_id
                )
            )
        except StorageError:
            unavailable_run = True
        if unavailable_run:
            _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_INCOHERENT)
        if view is None or snapshot is None:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return snapshot

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_STORAGE
            )
        return connection

    def _admission_authority(self, context: OperationContext) -> tuple[Any, Any, Any]:
        """The server facts C3-B will need before it may issue a mutation grant."""
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_IDENTITY
            )
        if context.authorization is None or self.session is None or self.binding is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_APPLICATION_AUTHORITY
            )
        if self.clock is None:
            object.__setattr__(self, "clock", SystemClock())
        return connection, identity, guard

    def _control_authority(self, context: OperationContext) -> None:
        """The server facts required before a mutating control request may read.

        This first-release control path writes no rows because every action is
        unsupported, but the operation is still a catalogue mutation. It therefore
        may not even confirm the target exists unless the dispatcher supplied the
        same server-owned authority that a future supported control will need.
        """
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_CONTROL_IDENTITY
            )
        if context.authorization is None or self.session is None or self.binding is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_CONTROL_AUTHORITY
            )
        if self.clock is None:
            object.__setattr__(self, "clock", SystemClock())


def _canonical_run(snapshot: RunSnapshot) -> Run:
    """The stored run as the accepted aggregate, or a refusal.

    `RunSnapshot.effect_reconciliations` has no counterpart on `Run` and is dropped
    rather than folded into the settlements it answers: the aggregate the contract
    accepts has no field for it, and merging it into one would overwrite the fact that
    an effect was once uncertain.
    """
    if snapshot.policy is None or snapshot.budget is None:
        raise application_refusal(
            ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_DECISIONS
        )
    run = Run(
        workspace_id=snapshot.workspace_id,
        run_id=snapshot.run_id,
        definition=snapshot.definition,
        status=snapshot.status,
        logical_key=snapshot.logical_key,
        originating_operation=snapshot.originating_operation,
        audit_reference=snapshot.audit_reference,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        finished_at=snapshot.finished_at,
        policy=snapshot.policy,
        budget=snapshot.budget,
        capability_grants=snapshot.capability_grants,
        steps=snapshot.steps,
        waits=snapshot.waits,
        approvals=snapshot.approvals,
        effect_intents=snapshot.effect_intents,
        effect_receipts=snapshot.effect_receipts,
        effect_settlements=snapshot.effect_settlements,
        events=snapshot.events,
        artifacts=snapshot.artifacts,
        evidence=snapshot.evidence,
        cleanup_receipts=snapshot.cleanup_receipts,
        correlations=snapshot.correlations,
    )
    try:
        validate_run(run, workspace_id=snapshot.workspace_id)
    except ContractSemanticError:
        incoherent = True
    else:
        incoherent = False
    if incoherent:
        _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_INCOHERENT)
    return run


def _latest_event_sequence(run: Run) -> int:
    """The durable journal sequence currently represented by this aggregate."""
    return max((event.sequence for event in run.events), default=0)


def _projection_version(run: Run) -> str:
    """A Core-issued opaque cursor for the workflow projection this build serves."""
    return f"core.workflow.run.{run.run_id}.sequence.{_latest_event_sequence(run)}"


def _first_canonical_instant_at_or_after(value: int) -> int:
    """Return the first millisecond-renderable instant that does not predate ``value``."""
    remainder = value % 1000
    if remainder == 0:
        return value
    return value + (1000 - remainder)


def _require_served_projection(requested: str | None, run: Run) -> None:
    """Accept only same-run cursors that this durable read can satisfy."""
    if requested is None:
        return
    prefix = f"core.workflow.run.{run.run_id}.sequence."
    if not requested.startswith(prefix):
        raise application_refusal(ERROR_CODE_STALE_PROJECTION, _MESSAGE_STALE_PROJECTION)
    raw_sequence = requested[len(prefix):]
    if not raw_sequence.isdecimal() or int(raw_sequence) > _latest_event_sequence(run):
        raise application_refusal(ERROR_CODE_STALE_PROJECTION, _MESSAGE_STALE_PROJECTION)


def _review_projection(run: Run) -> dict[str, Any]:
    """The review a served canonical ``Run`` supports, and nothing more.

    Every value here is copied from the aggregate beside it in the same result, so the
    projection can restate the run but can never contradict it. It carries no judgement,
    no estimate and no derived verdict: a review that scored a run would be asserting
    something no stored row says. What it does is arrange the journal the way a reviewer
    reads it -- what each step is doing, what the run is still waiting on, who has yet to
    decide -- and count the rest so a caller knows whether anything was left out.
    """
    return {
        "run_id": run.run_id,
        "status": run.status,
        "definition": {
            "kind": run.definition.definition_kind,
            "id": run.definition.definition_id,
            "version": run.definition.definition_version,
        },
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        "policy_snapshot_id": run.policy.policy_snapshot_id,
        "budget_snapshot_id": run.budget.budget_snapshot_id,
        "steps": [
            {
                "run_step_id": step.run_step_id,
                "ordinal": step.ordinal,
                "step_kind": step.step_kind,
                "status": step.status,
                "attempts": step.attempts,
            }
            for step in run.steps
        ],
        # Open work only: a resolved wait and a decided approval are history the `Run`
        # already carries, and repeating them here as outstanding would misread it.
        "open_waits": [
            {"wait_id": wait.wait_id, "kind": wait.kind, "status": wait.status}
            for wait in run.waits
            if wait.resolved_at is None
        ],
        "pending_approvals": [
            approval.approval_id
            for approval in run.approvals
            if approval.decision is None
        ],
        "totals": {
            "steps": len(run.steps),
            "waits": len(run.waits),
            "approvals": len(run.approvals),
            "events": len(run.events),
            "artifacts": len(run.artifacts),
            "evidence": len(run.evidence),
            "effect_intents": len(run.effect_intents),
            "effect_receipts": len(run.effect_receipts),
            "effect_settlements": len(run.effect_settlements),
            "capability_grants": len(run.capability_grants),
        },
    }


def _served(
    result: Mapping[str, Any], decode: Any
) -> Mapping[str, Any]:
    """A result served only if its own generated decoder still accepts it."""
    incoherent = False
    try:
        decode(result)
    except (ContractDecodeError, ContractSemanticError):
        incoherent = True
    if incoherent:
        _refuse(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_INCOHERENT)
    return result


def _refuse(code: str, message: str) -> NoReturn:
    raise application_refusal(code, message)


def _valid_start_result(result: Mapping[str, Any]) -> bool:
    """Whether a stored or freshly produced start result is still servable."""
    try:
        decoded = WorkflowStartResult.from_wire(result)
        validate_run(decoded.run, workspace_id=decoded.run.workspace_id)
    except (ContractDecodeError, ContractSemanticError):
        return False
    return True


__all__ = ["WorkflowApplicationRuntime"]
