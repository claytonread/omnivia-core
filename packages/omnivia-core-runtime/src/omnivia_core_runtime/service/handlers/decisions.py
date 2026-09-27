"""The `decision.*` application handlers (ADR-042, plan PR-3).

Eleven of the fifteen catalogue operations are real here; the three model
lifecycle operations (`decision.model.install/activate/remove`) remain the
honest `not_implemented` stub, because the model lifecycle is the runtime
slice's work (plan PR-4) and an intentionally unavailable state is correct when
the backend is absent (§28.4).

The security shape every handler follows is the one the other families
established:

1. decode and validate the payload through the contract's own decoder — never a
   local opinion about its shape;
2. take the workspace, the effective principal and the purpose from the
   *authorised* context, never from the payload;
3. require the authoritative connection and the current mutation guard;
4. mutations run through the existing idempotency coordinator
   (`issue_mutation_grant` / `execute_mutation`), so replay and conflict are the
   coordinator's decisions, not this family's (AT-42/43), and every write lands
   inside the fenced transaction with its audit event;
5. the capability switch and the definition fence are re-read *inside* that
   transaction (§8 step 7), so a disable or a definition change that raced the
   request is honoured, and a cancelled or fenced-out mutation cannot commit;
6. refusals carry no caller value: every message is a frozen module constant.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    DecisionDefinitionDisableInput,
    DecisionDefinitionDisableResult,
    DecisionDefinitionGetInput,
    DecisionDefinitionGetResult,
    DecisionDefinitionListInput,
    DecisionDefinitionListResult,
    DecisionDefinitionPublishInput,
    DecisionDefinitionPublishResult,
    DecisionEvaluateInput,
    DecisionEvaluateResult,
    DecisionModelListResult,
    DecisionOutcomeSubmitInput,
    DecisionOutcomeSubmitResult,
    DecisionRecordGetInput,
    DecisionRecordGetResult,
    DecisionRecordListInput,
    DecisionRecordListResult,
    DecisionSettingsGetInput,
    DecisionSettingsGetResult,
    DecisionSettingsUpdateInput,
    DecisionSettingsUpdateResult,
    DecisionStatusInput,
    DecisionStatusResult,
    JobReference,
    RequestMetadata,
    idempotency_equivalence,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.decision_runtime import (
    DETERMINISTIC_EXECUTION,
    DISPOSITION_ABSTAINED,
    DISPOSITION_ADVISORY_ONLY,
    SCHEMA_VERSION,
    UNAVAILABLE_MODEL_EXECUTION,
    DecisionDefinitionError,
    DecisionPolicyDenied,
    compose_policy,
    evaluate_deterministic,
    model_route_unavailable,
    validate_definition_document,
)
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationOutcome,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage.decisions import (
    append_decision_outbox_event,
    canonical_document,
    content_digest,
    definition_digest_matches,
    insert_decision_attempt,
    insert_decision_definition,
    insert_decision_evaluation,
    insert_decision_outcome,
    insert_decision_result,
    list_decision_definitions,
    list_decision_evaluations,
    read_decision_definition,
    read_decision_evaluation,
    read_decision_result,
    read_decision_settings_revision,
    read_processing_state,
    set_decision_definition_enabled,
    settle_decision_evaluation,
    write_decision_settings,
)
from omnivia_core_runtime.storage.jobs import (
    _digest,
    _document,
    _next_number,
    _timestamp,
    read_application_job_snapshot,
)

DECISION_EVALUATE_OPERATION: Final = "decision.evaluate"
DECISION_RECORD_GET_OPERATION: Final = "decision.record.get"
DECISION_RECORD_LIST_OPERATION: Final = "decision.record.list"
DECISION_STATUS_OPERATION: Final = "decision.status"
DECISION_DEFINITION_LIST_OPERATION: Final = "decision.definition.list"
DECISION_DEFINITION_GET_OPERATION: Final = "decision.definition.get"
DECISION_DEFINITION_PUBLISH_OPERATION: Final = "decision.definition.publish"
DECISION_DEFINITION_DISABLE_OPERATION: Final = "decision.definition.disable"
DECISION_OUTCOME_SUBMIT_OPERATION: Final = "decision.outcome.submit"
DECISION_MODEL_LIST_OPERATION: Final = "decision.model.list"
DECISION_SETTINGS_GET_OPERATION: Final = "decision.settings.get"
DECISION_SETTINGS_UPDATE_OPERATION: Final = "decision.settings.update"
DECISION_MODEL_OPERATIONS: Final = frozenset(
    {
        "decision.model.install",
        "decision.model.activate",
        "decision.model.remove",
    }
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this decision operation"
_MESSAGE_NOT_FOUND: Final = "the requested decision record was not found"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_DISABLED: Final = (
    "Local Decisions processing is not enabled for this workspace"
)
_MESSAGE_CONFLICT: Final = (
    "this decision request conflicts with an existing evaluation for the same "
    "idempotency key"
)

_EVENT_COMPLETED: Final = "decision.completed.v1"
_EVENT_ABSTAINED: Final = "decision.abstained.v1"
_EVENT_FAILED: Final = "decision.failed.v1"
_EVENT_OUTCOME: Final = "decision.outcome_recorded.v1"


@dataclass(frozen=True)
class DecisionHandlers:
    """The S-decision workspace-family handlers, wired as the other families are."""

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Any

    def _authority(self) -> tuple[sqlite3.Connection, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    # --- decision.evaluate ---------------------------------------------------

    def decision_evaluate(self, context: OperationContext) -> AuditedOperationResult:
        request: DecisionEvaluateInput | None = None
        try:
            request = DecisionEvaluateInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            request = None
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, identity, guard = self._authority()

        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            return _evaluate_transaction(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                request=request,
                metadata=context.request.metadata,
                fencing_generation=guard.fencing_generation,
                claimed_by_service_instance=identity.service_instance_id,
                allocate_identifier=self.allocate_identifier,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DecisionEvaluateResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
        outcome: MutationOutcome | None = None
        try:
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
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None or outcome is None:
            code = (
                ERROR_CODE_IDEMPOTENCY_CONFLICT
                if conflict is None or conflict.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
                else conflict.code
            )
            raise OperationError(
                code,
                _MESSAGE_CONFLICT,
                retry_class=(
                    "non_retryable" if conflict is None else conflict.retry_class
                ),
                audit_reference=(
                    None if conflict is None else conflict.audit_reference
                ),
            )
        result = DecisionEvaluateResult.from_wire(outcome.result)
        return AuditedOperationResult(
            outcome.result,
            audit_reference=outcome.audit_ref,
            job_reference=JobReference(job_id=result.job.identity.job_id),
        )

    # --- decision.record.get ---------------------------------------------------

    def decision_record_get(self, context: OperationContext) -> Mapping[str, Any]:
        try:
            request = DecisionRecordGetInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            evaluation = read_decision_evaluation(
                connection,
                workspace_id=context.workspace_id,
                evaluation_id=request.evaluation_id,
            )
            if evaluation is None:
                raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            stored = read_decision_result(
                connection,
                workspace_id=context.workspace_id,
                evaluation_id=request.evaluation_id,
            )
            record = _record_wire(evaluation, stored)
        finally:
            connection.execute("ROLLBACK")
        wire = {"record": record}
        result = DecisionRecordGetResult.from_wire(wire)
        validate_wire(result)
        return wire

    # --- decision.record.list --------------------------------------------------

    def decision_record_list(self, context: OperationContext) -> Mapping[str, Any]:
        try:
            request = DecisionRecordListInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            rows = list_decision_evaluations(
                connection,
                workspace_id=context.workspace_id,
                definition_id=request.definition_id,
                status=request.status,
                limit=request.limit if request.limit is not None else 50,
            )
            stored = [
                read_decision_result(
                    connection,
                    workspace_id=context.workspace_id,
                    evaluation_id=row["evaluation_id"],
                )
                for row in rows
            ]
            records = [
                _record_wire(row, stored)
                for row, stored in zip(rows, stored, strict=True)
            ]
        finally:
            connection.execute("ROLLBACK")
        wire = {"records": records, "page": {}}
        result = DecisionRecordListResult.from_wire(wire)
        validate_wire(result)
        return wire

    # --- decision.status ---------------------------------------------------------

    def decision_status(self, context: OperationContext) -> Mapping[str, Any]:
        try:
            DecisionStatusInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            processing = read_processing_state(
                connection, workspace_id=context.workspace_id
            )
            subscriptions = connection.execute(
                "SELECT COUNT(*) FROM omnivia_decision_subscriptions "
                "WHERE workspace_id = ? AND enabled = 1",
                (context.workspace_id,),
            ).fetchone()[0]
        finally:
            connection.execute("ROLLBACK")
        wire = {
            "schema_version": SCHEMA_VERSION,
            "host_engine_available": _host_engine_available(),
            "host_support_reason": _host_support_reason(),
            "enabled": processing == "advisory",
            "installed_profiles": 0,
            "active_subscriptions": int(subscriptions),
        }
        result = DecisionStatusResult.from_wire(wire)
        validate_wire(result)
        return wire

    # --- definitions -------------------------------------------------------------

    def decision_definition_list(
        self, context: OperationContext
    ) -> Mapping[str, Any]:
        try:
            DecisionDefinitionListInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            rows = list_decision_definitions(
                connection, workspace_id=context.workspace_id
            )
        finally:
            connection.execute("ROLLBACK")
        summaries = [
            {
                "id": row["id"],
                "version": row["version"],
                "title": row["title"],
                "purpose": row["purpose"],
                "kind": row["kind"],
                "option_count": row["option_count"],
                "enabled": row["enabled"],
                "digest": row["digest"],
            }
            for row in rows
        ]
        wire = {"definitions": summaries}
        result = DecisionDefinitionListResult.from_wire(wire)
        validate_wire(result)
        return wire

    def decision_definition_get(self, context: OperationContext) -> Mapping[str, Any]:
        try:
            request = DecisionDefinitionGetInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            stored = read_decision_definition(
                connection,
                workspace_id=context.workspace_id,
                definition_id=request.definition_ref.id,
                version=request.definition_ref.version,
            )
        finally:
            connection.execute("ROLLBACK")
        if stored is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        document = stored["definition"]
        wire = {
            "definition": {
                "id": str(document["id"]),
                "version": str(document["version"]),
                "title": str(document["title"]),
                "purpose": str(document["purpose"]),
                "kind": str(document["kind"]),
                "option_count": len(document["options"]),
                "enabled": bool(stored["enabled"]),
                "digest": str(stored["digest"]),
            }
        }
        result = DecisionDefinitionGetResult.from_wire(wire)
        validate_wire(result)
        return wire

    def decision_definition_publish(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = DecisionDefinitionPublishInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
        try:
            normalised = validate_definition_document(request.definition)
        except DecisionDefinitionError as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID
            ) from error
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            existing = read_decision_definition(
                fenced,
                workspace_id=context.workspace_id,
                definition_id=str(normalised["id"]),
                version=str(normalised["version"]),
            )
            if existing is not None:
                raise OperationError(
                    ERROR_CODE_CONFLICT,
                    "a definition version with this identity is already published",
                )
            insert_decision_definition(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                definition_id=str(normalised["id"]),
                version=str(normalised["version"]),
                title=str(normalised["title"]),
                kind=str(normalised["kind"]),
                purpose=str(normalised["purpose"]),
                options=list(normalised["options"]),
                recipe=dict(normalised["recipe"]),
                required_sources=int(normalised["required_sources"]),
                min_source_count=int(normalised["min_source_count"]),
                definition=normalised,
                digest=str(normalised["digest"]),
                published_by=context.principal,
                allocate_identifier=self.allocate_identifier,
            )
            return {
                "definition_ref": {
                    "id": normalised["id"],
                    "version": normalised["version"],
                },
                "digest": normalised["digest"],
                "enabled": True,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DecisionDefinitionPublishResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
        outcome: MutationOutcome | None = None
        try:
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
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None or outcome is None:
            code = (
                ERROR_CODE_CONFLICT
                if conflict is None or conflict.code == ERROR_CODE_CONFLICT
                else conflict.code
            )
            raise OperationError(
                code,
                _MESSAGE_CONFLICT,
                retry_class=(
                    "non_retryable" if conflict is None else conflict.retry_class
                ),
                audit_reference=(
                    None if conflict is None else conflict.audit_reference
                ),
            )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    def decision_definition_disable(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = DecisionDefinitionDisableInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            found = set_decision_definition_enabled(
                fenced,
                workspace_id=context.workspace_id,
                definition_id=request.definition_ref.id,
                version=request.definition_ref.version,
                enabled=False,
            )
            if not found:
                raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            return {
                "definition_ref": {
                    "id": request.definition_ref.id,
                    "version": request.definition_ref.version,
                },
                "enabled": False,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DecisionDefinitionDisableResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
        outcome: MutationOutcome | None = None
        try:
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
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None or outcome is None:
            raise OperationError(
                ERROR_CODE_CONFLICT if conflict is None else conflict.code,
                _MESSAGE_CONFLICT if conflict is None else conflict.message,
                retry_class=(
                    "non_retryable" if conflict is None else conflict.retry_class
                ),
            )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- outcomes --------------------------------------------------------------

    def decision_outcome_submit(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = DecisionOutcomeSubmitInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            evaluation = read_decision_evaluation(
                fenced,
                workspace_id=context.workspace_id,
                evaluation_id=request.evaluation_id,
            )
            if evaluation is None:
                raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            outcome_id = self.allocate_identifier("dout")
            evidence = {
                "kind": "caller_evidence",
                "refs": list(request.evidence_refs or []),
            }
            insert_decision_outcome(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                outcome_id=outcome_id,
                evaluation_id=request.evaluation_id,
                outcome=request.outcome,
                corrected_option_id=request.corrected_option_id,
                note=request.note,
                evidence=evidence,
                actor_id=context.principal,
                event_at_us=settlement.settled_at_us,
                superseded_outcome_id=None,
            )
            append_decision_outbox_event(
                fenced,
                workspace_id=context.workspace_id,
                aggregate_id=request.evaluation_id,
                outbox_id=self.allocate_identifier("devo"),
                event_kind=_EVENT_OUTCOME,
                payload={
                    "evaluation_id": request.evaluation_id,
                    "outcome_id": outcome_id,
                    "outcome": request.outcome,
                    "actor_id": context.principal,
                },
                created_at_us=settlement.settled_at_us,
            )
            return {
                "outcome_id": outcome_id,
                "evaluation_id": request.evaluation_id,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DecisionOutcomeSubmitResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
        outcome: MutationOutcome | None = None
        try:
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
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None or outcome is None:
            raise OperationError(
                ERROR_CODE_CONFLICT if conflict is None else conflict.code,
                _MESSAGE_CONFLICT if conflict is None else conflict.message,
                retry_class=(
                    "non_retryable" if conflict is None else conflict.retry_class
                ),
            )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- settings ----------------------------------------------------------------

    def decision_settings_get(self, context: OperationContext) -> Mapping[str, Any]:
        try:
            DecisionSettingsGetInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            processing = read_processing_state(
                connection, workspace_id=context.workspace_id
            )
            revision = read_decision_settings_revision(
                connection, workspace_id=context.workspace_id
            )
            subscription = connection.execute(
                "SELECT subscription_enabled FROM omnivia_decision_settings "
                "WHERE workspace_id = ?",
                (context.workspace_id,),
            ).fetchone()
        finally:
            connection.execute("ROLLBACK")
        wire: dict[str, Any] = {
            "settings": {
                "schema_version": SCHEMA_VERSION,
                "processing": processing,
                "subscription_enabled": bool(subscription and subscription[0]),
                "subscription_daily_budget": 10000,
                "revision": revision,
            }
        }
        result = DecisionSettingsGetResult.from_wire(wire)
        validate_wire(result)
        return wire

    def decision_settings_update(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = DecisionSettingsUpdateInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            try:
                revision = write_decision_settings(
                    fenced,
                    settlement,
                    workspace_id=context.workspace_id,
                    processing=(
                    "off" if request.processing is None else request.processing
                ),
                    subscription_enabled=False,
                    expected_revision=request.revision,
                )
            except LookupError as error:
                message = (
                    "decision settings have never been written"
                    if str(error) == "decision-settings-missing"
                    else "the settings revision does not match; re-read and retry"
                )
                raise OperationError(
                    ERROR_CODE_CONFLICT, message, retry_class="non_retryable"
                ) from error
            return {
                "settings": {
                    "schema_version": SCHEMA_VERSION,
                    "processing": request.processing,
                    "subscription_enabled": False,
                    "subscription_daily_budget": 10000,
                    "revision": revision,
                }
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DecisionSettingsUpdateResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
        outcome: MutationOutcome | None = None
        try:
            outcome = execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                mutate=mutate,
                precondition=lambda conn: str(
                    read_decision_settings_revision(
                        conn, workspace_id=context.workspace_id
                    )
                ),
                validate_result=valid_result,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None or outcome is None:
            raise OperationError(
                ERROR_CODE_CONFLICT if conflict is None else conflict.code,
                _MESSAGE_CONFLICT if conflict is None else conflict.message,
                retry_class=(
                    "non_retryable" if conflict is None else conflict.retry_class
                ),
            )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- models -------------------------------------------------------------------

    def decision_model_list(self, context: OperationContext) -> Mapping[str, Any]:
        """Approved model profiles. None exist until the runtime slice lands."""
        wire: dict[str, Any] = {"profiles": []}
        result = DecisionModelListResult.from_wire(wire)
        validate_wire(result)
        return wire

    # --- the model lifecycle stays the runtime slice's work ----------------------

    def decision_model_not_implemented(
        self, context: OperationContext
    ) -> Mapping[str, Any]:
        raise OperationError(
            ERROR_CODE_DEPENDENCY_UNAVAILABLE,
            "decision model lifecycle is not yet active in this build",
        )


def _evaluate_transaction(
    fenced: Any,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    principal_id: str,
    request: DecisionEvaluateInput,
    metadata: RequestMetadata,
    fencing_generation: int,
    claimed_by_service_instance: str,
    allocate_identifier: Any,
) -> Mapping[str, Any]:
    """The whole of §8's admission and deterministic route, in one fenced write.

    Everything authoritative happens here, inside the mutation transaction: the
    capability switch, the definition fence, the policy composition, the route
    and every record the evaluation leaves behind. A raise from this function
    fails the mutation; nothing partial can survive it.
    """
    processing = read_processing_state(fenced, workspace_id=workspace_id)
    try:
        policy = compose_policy(
            processing_state=processing,
            caller_deadline_ms=(
                5000
                if request.execution.deadline_ms is None
                else request.execution.deadline_ms
            ),
            caller_attempts=(
                1
                if request.execution.maximum_provider_attempts is None
                else request.execution.maximum_provider_attempts
            ),
            required_sources=0,
        )
    except DecisionPolicyDenied as denied:
        raise OperationError(
            ERROR_CODE_CAPABILITY_NOT_GRANTED,
            _MESSAGE_DISABLED,
            retry_class="non_retryable",
        ) from denied

    definition_ref = request.definition_ref
    stored = read_decision_definition(
        fenced,
        workspace_id=workspace_id,
        definition_id=definition_ref.id,
        version=definition_ref.version,
    )
    if stored is None or not stored["enabled"]:
        raise OperationError(
            ERROR_CODE_NOT_FOUND,
            "the requested decision definition is not published or is disabled",
        )
    definition = stored["definition"]
    if not definition_digest_matches(
        fenced,
        workspace_id=workspace_id,
        definition_id=definition_ref.id,
        version=definition_ref.version,
        digest=stored["digest"],
    ):
        raise OperationError(ERROR_CODE_NOT_FOUND, "the decision definition digest moved")

    evaluation_id = allocate_identifier("deval")
    job_id = allocate_identifier("job")
    request_digest = _digest(_document(request.to_wire()))
    resolved_sources = 0
    subject_refs = [subject.to_wire() for subject in request.subject_refs]
    source_snapshot = {
        "resolved": resolved_sources,
        "inline_state": request.input.inline_state or {},
        "unresolved_source_refs": len(request.input.source_refs),
    }

    insert_decision_evaluation(
        fenced,
        settlement,
        workspace_id=workspace_id,
        evaluation_id=evaluation_id,
        principal_id=principal_id,
        idempotency_key=str(metadata.idempotency_key),
        request_digest=request_digest,
        definition_id=definition_ref.id,
        definition_version=definition_ref.version,
        definition_digest=str(stored["digest"]),
        mode=str(policy["mode"]),
        subject_refs=subject_refs,
        source_snapshot=source_snapshot,
        job_id=job_id,
    )
    _start_decision_job(
        fenced,
        settlement,
        workspace_id=workspace_id,
        job_id=job_id,
        evaluation_id=evaluation_id,
        request_digest=request_digest,
        fencing_generation=fencing_generation,
        claimed_by_service_instance=claimed_by_service_instance,
    )

    attempt_id = allocate_identifier("datm")
    route = (
        "deterministic"
        if definition["recipe"]["mode"] == "deterministic"
        else "local_model"
    )
    if route == "deterministic":
        outcome = evaluate_deterministic(
            definition,
            inline_state=dict(request.input.inline_state or {}),
            resolved_sources=resolved_sources,
        )
        attempt_status = "succeeded"
        failure_code = None
        event_kind = (
            _EVENT_COMPLETED
            if outcome["disposition"]["code"] == DISPOSITION_ADVISORY_ONLY
            else _EVENT_ABSTAINED
        )
        status = (
            "succeeded"
            if outcome["disposition"]["code"] == DISPOSITION_ADVISORY_ONLY
            else "abstained"
        )
        execution = dict(DETERMINISTIC_EXECUTION)
    else:
        outcome = model_route_unavailable()
        attempt_status = "failed"
        failure_code = "model_not_installed"
        event_kind = _EVENT_FAILED
        status = "failed"
        execution = dict(UNAVAILABLE_MODEL_EXECUTION)
    insert_decision_attempt(
        fenced,
        settlement,
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        evaluation_id=evaluation_id,
        attempt_number=1,
        route=route,
        provider_id=str(execution["provider_id"]),
        profile_id=str(execution["profile_id"]),
        policy_generation=f"settings-revision@{fencing_generation}",
        status=attempt_status,
        failure_code=failure_code,
    )
    result_id = allocate_identifier("dres")
    input_digest = content_digest(canonical_document(subject_refs))
    insert_decision_result(
        fenced,
        settlement,
        workspace_id=workspace_id,
        result_id=result_id,
        evaluation_id=evaluation_id,
        status=status,
        prediction=outcome["prediction"],
        disposition=outcome["disposition"],
        quality=outcome["quality"],
        execution=execution,
        abstention_reasons=outcome.get("abstention_reasons"),
        input_digest=input_digest,
    )
    settle_decision_evaluation(
        fenced,
        workspace_id=workspace_id,
        evaluation_id=evaluation_id,
        status=status,
        abstention_reasons=outcome.get("abstention_reasons"),
        terminal_at_us=settlement.settled_at_us,
    )
    append_decision_outbox_event(
        fenced,
        workspace_id=workspace_id,
        aggregate_id=evaluation_id,
        outbox_id=allocate_identifier("devo"),
        event_kind=event_kind,
        payload={
            "evaluation_id": evaluation_id,
            "definition_ref": {"id": definition_ref.id, "version": definition_ref.version},
            "status": status,
            "mode": str(policy["mode"]),
        },
        created_at_us=settlement.settled_at_us,
    )
    _finish_decision_job(
        fenced,
        settlement,
        workspace_id=workspace_id,
        job_id=job_id,
        state="succeeded",
        result_kind="decision_evaluation",
        result_json=_document(
            {
                "evaluation_id": evaluation_id,
                "status": status,
                "definition_ref": {
                    "id": definition_ref.id,
                    "version": definition_ref.version,
                },
            }
        ),
        fencing_generation=fencing_generation,
    )
    snapshot = read_application_job_snapshot(
        fenced, workspace_id=workspace_id, job_id=job_id
    )
    assert snapshot is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": evaluation_id,
        "job": snapshot["job"],
    }


def _start_decision_job(
    fenced: Any,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    job_id: str,
    evaluation_id: str,
    request_digest: str,
    fencing_generation: int,
    claimed_by_service_instance: str,
) -> None:
    moment = _timestamp(settlement.settled_at_us)
    fenced.execute(
        "INSERT INTO omnivia_durable_jobs "
        "(job_id, job_type, state, payload_json, created_at, updated_at, "
        "fencing_generation, claimed_by_service_instance) "
        "VALUES (?, 'decision.evaluate', 'claimed', ?, ?, ?, ?, ?)",
        (
            job_id,
            _document({"evaluation_id": evaluation_id, "request_digest": request_digest}),
            moment,
            moment,
            fencing_generation,
            claimed_by_service_instance,
        ),
    )
    fenced.execute(
        "INSERT INTO omnivia_job_application_metadata "
        "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
        "created_at_us, terminal_result_kind, supports_checkpoint_resume, max_attempts) "
        "VALUES (?, ?, 'decision.evaluate', 'decision.evaluate', ?, ?, "
        "'decision_evaluation', 0, 1)",
        (workspace_id, job_id, settlement.audit_ref, settlement.settled_at_us),
    )
    fenced.execute(
        "INSERT INTO omnivia_job_attempts "
        "(workspace_id, job_id, attempt_number, started_at_us, state) "
        "VALUES (?, ?, 1, ?, 'running')",
        (workspace_id, job_id, settlement.settled_at_us),
    )
    fenced.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "VALUES (?, ?, 0, ?, 'running', 'decision evaluation admitted')",
        (workspace_id, job_id, settlement.settled_at_us),
    )


def _finish_decision_job(
    fenced: Any,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    job_id: str,
    state: str,
    result_kind: str,
    result_json: str,
    fencing_generation: int,
) -> None:
    fenced.execute(
        "UPDATE omnivia_durable_jobs SET state = ?, updated_at = ?, "
        "claimed_by_service_instance = NULL WHERE job_id = ?",
        (state, _timestamp(settlement.settled_at_us), job_id),
    )
    fenced.execute(
        "UPDATE omnivia_job_attempts SET state = ?, finished_at_us = ? "
        "WHERE workspace_id = ? AND job_id = ? AND state = 'running'",
        (state, settlement.settled_at_us, workspace_id, job_id),
    )
    sequence = _next_number(
        fenced,
        "omnivia_job_events",
        "sequence",
        workspace_id=workspace_id,
        job_id=job_id,
        base=-1,
    )
    fenced.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "VALUES (?, ?, ?, ?, ?, 'decision evaluation settled')",
        (workspace_id, job_id, sequence, settlement.settled_at_us, state),
    )
    observation = _next_number(
        fenced,
        "omnivia_job_terminal_observations",
        "terminal_observation_number",
        workspace_id=workspace_id,
        job_id=job_id,
        base=0,
    )
    fenced.execute(
        "INSERT INTO omnivia_job_terminal_observations "
        "(workspace_id, job_id, terminal_observation_number, attempt_number, "
        "terminal_state, finished_at_us, result_kind, result_json, "
        "provenance_kind, fencing_generation) VALUES (?, ?, ?, 1, ?, ?, ?, ?, "
        "'service_committed', ?)",
        (
            workspace_id,
            job_id,
            observation,
            state,
            settlement.settled_at_us,
            result_kind,
            result_json,
            fencing_generation,
        ),
    )


def _host_engine_available() -> bool:
    """Truthful host support (§4.4): the deterministic engine always runs; the
    model worker does not exist in this build, so the *engine* answer is about
    the deterministic route only."""
    return True


def _host_support_reason() -> str:
    import platform

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "deterministic_rules_available"
    return "model_worker_unsupported_on_this_host"


def _record_wire(
    evaluation: Mapping[str, Any], stored: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The decision.1 Decision Record envelope (§14.2) for one evaluation."""
    status = str(evaluation["status"])
    prediction = None if stored is None else stored["prediction"]
    disposition = (
        {
            "code": DISPOSITION_ABSTAINED,
            "reason_codes": list(evaluation["abstention_reasons"] or []),
            "authorises_action": False,
        }
        if stored is None
        else stored["disposition"]
    )
    quality = (
        {
            "calibration_status": "unvalidated_for_task",
            "input_complete": False,
        }
        if stored is None
        else stored["quality"]
    )
    execution = (
        dict(UNAVAILABLE_MODEL_EXECUTION)
        if stored is None
        else stored["execution"]
    )
    created = evaluation["created_at_us"]
    wire: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": str(evaluation["evaluation_id"]),
        "status": status,
        "mode": str(evaluation["mode"]),
        "definition_ref": dict(evaluation["definition_ref"]),
        "subject_refs": list(evaluation["subject_refs"]),
        "quality": quality,
        "disposition": disposition,
        "execution": execution,
        "created_at": _timestamp(created),
        "observed_at": _timestamp(
            evaluation["terminal_at_us"] if evaluation["terminal_at_us"] else created
        ),
    }
    if prediction is not None:
        wire["prediction"] = prediction
    if evaluation["abstention_reasons"]:
        wire["abstention_reasons"] = list(evaluation["abstention_reasons"])
    return wire


def validate_wire(result: Any) -> None:
    """One round-trip through the contract decoder; refusals stay bounded."""
    result.to_wire()
