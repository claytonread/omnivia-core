"""The trigger operations: declaration, subscription lifecycle, admission and health.

`trigger.declare` and `trigger.lifecycle` configure a trigger, and `trigger.ingest` admits one
stimulus to it. Each is one fenced mutation under `execute_mutation`, so its grant, idempotency,
audit and rollback are the ones every mutation has. `trigger.health` is a bounded read of the same
telemetry, by trigger or by Project and Workflow.

These handlers own no trigger state. The store is `storage.trigger_telemetry`, and a declared
Workflow version is confirmed through the release authority seam `workflow.start` uses. Nothing
here schedules, polls or enqueues. `trigger.ingest` records one observation and starts no job or
run, so an accepted stimulus reads `unlinked` until a Workflow Runtime enqueue seam links it.

Delivery is decided here, from the declaration and the subscription the store holds, and recorded
by the store, which decides no acceptance of its own. A stimulus the trigger does not admit is
recorded as dead-lettered; it is not a refusal. A request that names no trigger of this Project and
Workflow, or that the store could not represent, is refused before anything is written.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    TRIGGER_INITIAL_SUBSCRIPTION_STATES,
    TRIGGER_KINDS,
    TRIGGER_SUBSCRIPTION_STATES,
    ContractDecodeError,
    ContractSemanticError,
    PageMetadata,
    TriggerDeclareInput,
    TriggerDeclareResult,
    TriggerDeliveryCounts,
    TriggerFailure,
    TriggerHealth,
    TriggerHealthInput,
    TriggerHealthResult,
    TriggerIngestInput,
    TriggerIngestResult,
    TriggerLifecycleInput,
    TriggerLifecycleResult,
    TriggerObservationHealth,
    TriggerSubscriptionHealth,
    idempotency_equivalence,
    is_content_checksum,
    is_identifier,
    is_open_code,
    is_release_version,
    is_timestamp,
)
from omnivia_core.contracts.v1.semantics_jobs import IdempotencyEquivalence
from omnivia_core_runtime.execution.profile import ExecutionContractError
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.handlers.workflow import (
    WorkflowRelease,
    WorkflowReleaseResolver,
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
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.storage.trigger_telemetry import (
    DEFAULT_TRIGGER_PAGE,
    MAX_PAGE_OBSERVATION_WINDOW,
    MAX_TRIGGER_PAGE,
    ObservationView,
    TriggerObservation,
    TriggerTelemetry,
    list_workflow_trigger_telemetry,
    read_accepted_trigger_observation,
    read_trigger_telemetry,
    transaction_local_telemetry_writer,
)
from omnivia_core_runtime.storage.workflow_runs import transaction_local_workflow_writer

TRIGGER_DECLARE_OPERATION: Final = "trigger.declare"
TRIGGER_LIFECYCLE_OPERATION: Final = "trigger.lifecycle"
TRIGGER_INGEST_OPERATION: Final = "trigger.ingest"
TRIGGER_HEALTH_OPERATION: Final = "trigger.health"
TRIGGER_FAMILY_OPERATIONS: Final = frozenset(
    {
        TRIGGER_DECLARE_OPERATION,
        TRIGGER_LIFECYCLE_OPERATION,
        TRIGGER_INGEST_OPERATION,
        TRIGGER_HEALTH_OPERATION,
    }
)

#: The observation window a health read returns when the caller names none.
_DEFAULT_OBSERVATION_LIMIT: Final = 5
#: The store's Workflow identifier: lowercase, which the wire's identifier does not require.
_WORKFLOW_ID: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
#: A UTC instant with at most nanosecond precision, which the store reads to microseconds.
_STAMP: Final = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z"
)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_MESSAGE_NO_STORAGE: Final = "the trigger store is not reachable from this service instance"
_MESSAGE_NO_RELEASE_AUTHORITY: Final = (
    "this build has no Workflow release authority, so no trigger can be declared against a "
    "released Workflow version"
)
_MESSAGE_NO_RELEASE: Final = "no released version of that Workflow matches the declaration"
_MESSAGE_RELEASE_MISMATCH: Final = (
    "the release authority answered with a different Workflow version"
)
_MESSAGE_PLAN_NOT_RELEASED: Final = (
    "the declared plan is not the plan released under that Workflow version"
)
_MESSAGE_UNSEALED_RELEASE: Final = "the released plan does not verify its content hash"
_MESSAGE_NOT_BOUND: Final = "no trigger of this Project and Workflow has that identifier"
_MESSAGE_REUSED_KEY: Final = (
    "this event idempotency key was already accepted for a different envelope"
)
_MESSAGE_EXACT_READ_PAGE: Final = "an exact trigger read takes no limit or page"
_MESSAGE_UNDECLARED_KEY: Final = (
    "the request names a key the trigger contract does not declare, so it is refused"
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise application_refusal(ERROR_CODE_INVALID_REQUEST, message)


def _check_bindings(*, project_id: str, workflow_id: str, trigger_id: str | None) -> None:
    """The keys every trigger operation names, refused before any read or write when malformed."""
    _require(is_identifier(project_id), "project_id is malformed")
    _require(_WORKFLOW_ID.fullmatch(workflow_id) is not None, "workflow_id is malformed")
    if trigger_id is not None:
        _require(is_identifier(trigger_id), "trigger_id is malformed")


def _timestamp(microseconds: int) -> str:
    """One microsecond instant, spelled as the wire's UTC `Timestamp`."""
    moment = _EPOCH + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def _microseconds(stamp: str) -> int:
    """A wire `Timestamp` as integer microseconds. Precision past the microsecond is dropped."""
    match = _STAMP.fullmatch(stamp) if is_timestamp(stamp) else None
    if match is None:
        raise application_refusal(ERROR_CODE_INVALID_REQUEST, "occurred_at is not a UTC instant")
    moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    micro = int((match.group(2) or "").ljust(6, "0")[:6])
    return (moment - _EPOCH) // timedelta(microseconds=1) + micro


def _row_id(prefix: str, settlement: MutationSettlementContext) -> str:
    """A row identity taken from this mutation's own claim, so no two settlements share one."""
    return f"{prefix}-{settlement.claim_id}"


def _servable(decode: Callable[[object], object]) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a result, fresh or replayed, still decodes as the contract's result type."""

    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_DECLARE = _servable(TriggerDeclareResult.from_wire)
_VALID_LIFECYCLE = _servable(TriggerLifecycleResult.from_wire)
_VALID_INGEST = _servable(TriggerIngestResult.from_wire)


def _bound_telemetry(
    connection: Any,
    *,
    workspace_id: str,
    project_id: str,
    workflow_id: str,
    trigger_id: str,
    observation_limit: int,
) -> TriggerTelemetry:
    """The trigger's telemetry, or a `not_found` when it is not of this Project and Workflow."""
    telemetry = read_trigger_telemetry(
        connection,
        workspace_id=workspace_id,
        project_id=project_id,
        workflow_id=workflow_id,
        trigger_id=trigger_id,
        observation_limit=observation_limit,
    )
    if telemetry is None:
        raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_BOUND)
    return telemetry


def _observation_health(view: ObservationView) -> TriggerObservationHealth:
    observation = view.observation
    return TriggerObservationHealth(
        trigger_observation_id=observation.trigger_observation_id,
        observation_sequence=observation.observation_sequence,
        event_id=observation.event_id,
        event_idempotency_key=observation.idempotency_key,
        event_type=observation.event_type,
        envelope_digest=observation.envelope_digest,
        observed_at=_timestamp(observation.observed_at_us),
        delivery_status=observation.delivery_status,
        processing=view.processing,
        uncertainty=view.uncertainty,
        occurred_at=None
        if observation.occurred_at_us is None
        else _timestamp(observation.occurred_at_us),
        delivery_reason=observation.delivery_reason,
        duplicate_of_observation_id=observation.duplicate_of_observation_id,
        job_id=observation.job_id,
        run_id=observation.run_id,
        job_state=view.job_state,
        run_status=view.run_status,
    )


def _trigger_health(telemetry: TriggerTelemetry) -> TriggerHealth:
    declaration = telemetry.declaration
    subscription = telemetry.subscription
    return TriggerHealth(
        trigger_id=declaration.trigger_id,
        trigger_kind=declaration.trigger_kind,
        workflow_version=declaration.workflow_version,
        declaration_sequence=declaration.declaration_sequence,
        event_type=declaration.event_type,
        subscription=TriggerSubscriptionHealth(
            state=subscription.state,
            reason=subscription.reason,
            observed_at=None
            if subscription.observed_at_us is None
            else _timestamp(subscription.observed_at_us),
            subscription_sequence=subscription.subscription_sequence,
        ),
        last_observation=None
        if telemetry.last_observation is None
        else _observation_health(telemetry.last_observation),
        observation_total=telemetry.observation_total,
        observations=tuple(_observation_health(view) for view in telemetry.window),
        delivery_counts=TriggerDeliveryCounts(**telemetry.delivery_counts),
        failures=tuple(
            TriggerFailure(
                trigger_observation_id=failure.trigger_observation_id,
                source=failure.source,
                reason=failure.reason,
            )
            for failure in telemetry.failures
        ),
        uncertainty=telemetry.uncertainty,
    )


def _decide_delivery(
    request: TriggerIngestInput,
    telemetry: TriggerTelemetry,
    accepted: TriggerObservation | None,
) -> tuple[str, str | None, str | None]:
    """The door's decision for one stimulus: (delivery status, reason, duplicate of).

    An accepted stimulus under the same key is a duplicate when its envelope digest matches. An
    altered repeat is refused, since it names no stimulus the store can record. A new stimulus is
    accepted only into an `active` subscription and only when its type matches the declared type
    exactly; anything else is dead-lettered with the reason that refused it.
    """
    if accepted is not None:
        if accepted.envelope_digest != request.envelope_digest:
            raise application_refusal(ERROR_CODE_IDEMPOTENCY_CONFLICT, _MESSAGE_REUSED_KEY)
        return "duplicate", None, accepted.trigger_observation_id
    if telemetry.subscription.state != "active":
        return "dead_lettered", "inactive_trigger", None
    if request.event_type != telemetry.declaration.event_type:
        return "dead_lettered", "event_type_mismatch", None
    return "accepted", None, None


@dataclass(frozen=True)
class TriggerHandlers:
    """The four trigger operations, over one workspace's trigger telemetry.

    `resolve_release` is the release authority seam `workflow.start` uses. Absent,
    `trigger.declare` refuses with `dependency_unavailable`: a trigger is never declared against a
    Workflow version nobody can confirm.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator
    resolve_release: WorkflowReleaseResolver | None = None

    # -- the storage authority this instance is serving --

    def _authority(self) -> tuple[Any, Any, MutationGuard]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection, identity, guard

    def _decode(self, context: OperationContext, wire_type: Any) -> Any:
        """Decode the request, refusing a key the contract does not declare rather than dropping it.

        The generated decoder drops unknown keys, so a newer peer's additive release still
        decodes. A trigger carries no raw payload, so a stray key, a payload say, is refused
        before decoding, as `engineering.repository.register` refuses its own keys.
        """
        raw = context.request.input
        if isinstance(raw, Mapping) and not set(raw) <= {f.name for f in fields(wire_type)}:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UNDECLARED_KEY)
        try:
            return wire_type.from_wire(raw)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, str(error)) from error

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, IdempotencyEquivalence]:
        _connection, _identity, guard = self._authority()
        if context.authorization is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
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
        return grant, equivalence

    def _confirm_release(self, request: TriggerDeclareInput) -> WorkflowRelease:
        """The declared Workflow version and plan must be the ones the release authority holds."""
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
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_RELEASE_MISMATCH)
        try:
            release.plan.verify_content_hash()
        except ExecutionContractError as error:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_UNSEALED_RELEASE
            ) from error
        if release.plan.content_hash != request.plan_hash:
            raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_PLAN_NOT_RELEASED)
        return release

    # -- trigger.declare -------------------------------------------------------------

    def trigger_declare(self, context: OperationContext) -> AuditedOperationResult:
        """Declare one trigger, with the subscription state it starts in, in one fenced write."""
        request: TriggerDeclareInput = self._decode(context, TriggerDeclareInput)
        _check_bindings(
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            trigger_id=request.trigger_id,
        )
        _require(request.trigger_kind in TRIGGER_KINDS, "trigger_kind is not a trigger kind")
        _require(
            request.subscription_state in TRIGGER_INITIAL_SUBSCRIPTION_STATES,
            "a subscription starts active or paused",
        )
        _require(is_release_version(request.workflow_version), "workflow_version is malformed")
        _require(is_identifier(request.event_type), "event_type is malformed")
        _require(is_content_checksum(request.plan_hash), "plan_hash is malformed")
        _require(
            is_content_checksum(request.event_contract_digest),
            "event_contract_digest is malformed",
        )
        _require(
            is_content_checksum(request.configuration_digest),
            "configuration_digest is malformed",
        )
        _require(is_open_code(request.subscription_reason), "subscription_reason is malformed")
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, request.to_wire())

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            # Confirmed inside the fence, as workflow.start confirms its release: a retry of a
            # declaration that already committed is answered from the stored outcome and never
            # consults the authority.
            release = self._confirm_release(request)
            # The declaration binds the sealed plan it names, so the plan must be sealed in this
            # workspace, as `workflow.start` seals it. Re-sealing identical content writes nothing.
            try:
                transaction_local_workflow_writer(
                    fenced, workspace_id=context.workspace_id
                ).seal_plan(
                    release.plan,
                    sealed_at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            except (StorageError, sqlite3.IntegrityError) as error:
                raise application_refusal(ERROR_CODE_CONFLICT, str(error)) from error
            writer = transaction_local_telemetry_writer(
                fenced, workspace_id=context.workspace_id
            )
            try:
                declaration = writer.declare_trigger(
                    trigger_declaration_id=_row_id("tdecl", settlement),
                    trigger_id=request.trigger_id,
                    trigger_kind=request.trigger_kind,
                    project_id=request.project_id,
                    workflow_id=request.workflow_id,
                    workflow_version=request.workflow_version,
                    plan_hash=request.plan_hash,
                    event_type=request.event_type,
                    event_contract_digest=request.event_contract_digest,
                    configuration_digest=request.configuration_digest,
                    declared_at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
                # A later version keeps the subscription it already holds. The store refuses a
                # move to the state a subscription is in, so only a change of state is a step.
                held = _bound_telemetry(
                    fenced,
                    workspace_id=context.workspace_id,
                    project_id=request.project_id,
                    workflow_id=request.workflow_id,
                    trigger_id=request.trigger_id,
                    observation_limit=1,
                ).subscription
                if held.state == request.subscription_state:
                    subscription_state = request.subscription_state
                    subscription_sequence = held.subscription_sequence
                else:
                    step = writer.record_subscription_state(
                        subscription_event_id=_row_id("tsub", settlement),
                        trigger_id=request.trigger_id,
                        subscription_state=request.subscription_state,
                        reason=request.subscription_reason,
                        observed_at_us=settlement.settled_at_us,
                        audit_ref=settlement.audit_ref,
                    )
                    subscription_state = step.subscription_state
                    subscription_sequence = step.subscription_sequence
            except (StorageError, sqlite3.IntegrityError) as error:
                raise application_refusal(ERROR_CODE_CONFLICT, str(error)) from error
            return TriggerDeclareResult(
                trigger_id=declaration.trigger_id,
                declaration_sequence=declaration.declaration_sequence,
                subscription_state=subscription_state,
                subscription_sequence=subscription_sequence,
                declared_at=_timestamp(declaration.declared_at_us),
            ).to_wire()

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=_VALID_DECLARE,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    # -- trigger.lifecycle -----------------------------------------------------------

    def trigger_lifecycle(self, context: OperationContext) -> AuditedOperationResult:
        """Move one declared trigger's subscription through the store's transition table."""
        request: TriggerLifecycleInput = self._decode(context, TriggerLifecycleInput)
        _check_bindings(
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            trigger_id=request.trigger_id,
        )
        _require(
            request.subscription_state in TRIGGER_SUBSCRIPTION_STATES,
            "subscription_state is not a subscription state",
        )
        _require(is_open_code(request.reason), "reason is malformed")
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, request.to_wire())

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            _bound_telemetry(
                fenced,
                workspace_id=context.workspace_id,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                trigger_id=request.trigger_id,
                observation_limit=1,
            )
            writer = transaction_local_telemetry_writer(
                fenced, workspace_id=context.workspace_id
            )
            try:
                subscription = writer.record_subscription_state(
                    subscription_event_id=_row_id("tsub", settlement),
                    trigger_id=request.trigger_id,
                    subscription_state=request.subscription_state,
                    reason=request.reason,
                    observed_at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            except (StorageError, sqlite3.IntegrityError) as error:
                raise application_refusal(ERROR_CODE_CONFLICT, str(error)) from error
            return TriggerLifecycleResult(
                trigger_id=subscription.trigger_id,
                subscription_state=subscription.subscription_state,
                subscription_sequence=subscription.subscription_sequence,
                reason=subscription.reason,
                observed_at=_timestamp(subscription.observed_at_us),
            ).to_wire()

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=_VALID_LIFECYCLE,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    # -- trigger.ingest --------------------------------------------------------------

    def trigger_ingest(self, context: OperationContext) -> AuditedOperationResult:
        """Admit one stimulus to a declared trigger: one observation, recorded and not run."""
        request: TriggerIngestInput = self._decode(context, TriggerIngestInput)
        _check_bindings(
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            trigger_id=request.trigger_id,
        )
        _require(is_identifier(request.event_id), "event_id is malformed")
        _require(
            is_identifier(request.event_idempotency_key),
            "event_idempotency_key is malformed",
        )
        _require(is_identifier(request.event_type), "event_type is malformed")
        _require(is_content_checksum(request.envelope_digest), "envelope_digest is malformed")
        occurred = None if request.occurred_at is None else _microseconds(request.occurred_at)
        _require(occurred is None or occurred > 0, "occurred_at must be after the epoch")
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, request.to_wire())

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            telemetry = _bound_telemetry(
                fenced,
                workspace_id=context.workspace_id,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                trigger_id=request.trigger_id,
                observation_limit=1,
            )
            accepted = read_accepted_trigger_observation(
                fenced,
                workspace_id=context.workspace_id,
                trigger_id=request.trigger_id,
                idempotency_key=request.event_idempotency_key,
            )
            status, reason, duplicate_of = _decide_delivery(request, telemetry, accepted)
            writer = transaction_local_telemetry_writer(
                fenced, workspace_id=context.workspace_id
            )
            try:
                recorded = writer.record_observation(
                    trigger_observation_id=_row_id("tobs", settlement),
                    trigger_id=request.trigger_id,
                    event_id=request.event_id,
                    idempotency_key=request.event_idempotency_key,
                    event_type=request.event_type,
                    envelope_digest=request.envelope_digest,
                    occurred_at_us=occurred,
                    observed_at_us=settlement.settled_at_us,
                    delivery_status=status,
                    delivery_reason=reason,
                    duplicate_of_observation_id=duplicate_of,
                    audit_ref=settlement.audit_ref,
                )
            except (StorageError, sqlite3.IntegrityError) as error:
                # Every refusal the door can name was decided above, so a refusal here is a fault
                # in this seam. The operation's profile admits no conflict, so it is not one.
                raise application_refusal(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE, str(error)
                ) from error
            # The processing reading is the store's own, read back from the row just recorded, so
            # an admission and the health read cannot disagree about what it means.
            view = _bound_telemetry(
                fenced,
                workspace_id=context.workspace_id,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                trigger_id=request.trigger_id,
                observation_limit=1,
            ).last_observation
            if view is None:  # pragma: no cover - the row was recorded just above
                raise application_refusal(
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                    "the stimulus was recorded but cannot be read back",
                )
            return TriggerIngestResult(
                trigger_id=request.trigger_id,
                trigger_observation_id=recorded.trigger_observation_id,
                observation_sequence=recorded.observation_sequence,
                delivery_status=recorded.delivery_status,
                processing=view.processing,
                uncertainty=view.uncertainty,
                observed_at=_timestamp(recorded.observed_at_us),
                delivery_reason=recorded.delivery_reason,
                duplicate_of_observation_id=recorded.duplicate_of_observation_id,
            ).to_wire()

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=_VALID_INGEST,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    # -- trigger.health --------------------------------------------------------------

    def trigger_health(self, context: OperationContext) -> Mapping[str, Any]:
        """A bounded read: one trigger by identifier, or one page of a Workflow's triggers."""
        request: TriggerHealthInput = self._decode(context, TriggerHealthInput)
        _check_bindings(
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            trigger_id=request.trigger_id,
        )
        observation_limit = (
            _DEFAULT_OBSERVATION_LIMIT
            if request.observation_limit is None
            else request.observation_limit
        )
        _require(
            1 <= observation_limit <= MAX_PAGE_OBSERVATION_WINDOW,
            f"observation_limit must be between 1 and {MAX_PAGE_OBSERVATION_WINDOW}",
        )
        connection, _identity, _guard = self._authority()
        if request.trigger_id is not None:
            _require(request.limit is None and request.page is None, _MESSAGE_EXACT_READ_PAGE)
            telemetry = _bound_telemetry(
                connection,
                workspace_id=context.workspace_id,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                trigger_id=request.trigger_id,
                observation_limit=observation_limit,
            )
            return TriggerHealthResult(
                items=(_trigger_health(telemetry),), page=PageMetadata()
            ).to_wire()

        _require(request.limit is None or request.limit >= 1, "limit must be at least 1")
        limit = (
            DEFAULT_TRIGGER_PAGE
            if request.limit is None
            else min(request.limit, MAX_TRIGGER_PAGE)
        )
        after = None if request.page is None else request.page.continuation_token
        _require(
            request.page is None or after is not None,
            "a page must name a continuation token",
        )
        if after is not None:
            _require(is_identifier(after), "the continuation token is malformed")
        page = list_workflow_trigger_telemetry(
            connection,
            workspace_id=context.workspace_id,
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            limit=limit,
            after_trigger_id=after,
            observation_limit=observation_limit,
        )
        return TriggerHealthResult(
            items=tuple(_trigger_health(telemetry) for telemetry in page.items),
            page=PageMetadata(continuation_token=page.next_after_trigger_id),
        ).to_wire()


__all__ = [
    "TRIGGER_DECLARE_OPERATION",
    "TRIGGER_FAMILY_OPERATIONS",
    "TRIGGER_HEALTH_OPERATION",
    "TRIGGER_INGEST_OPERATION",
    "TRIGGER_LIFECYCLE_OPERATION",
    "TriggerHandlers",
]
