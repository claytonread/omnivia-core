"""Production handlers for the four V06-5 S4 governed transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    CandidateApproveInput,
    CandidateApproveResult,
    CandidateRejectInput,
    CandidateRejectResult,
    ContractDecodeError,
    ContractSemanticError,
    KnowledgeProposeInput,
    KnowledgeProposeResult,
    RecordSupersedeInput,
    RecordSupersedeResult,
    decode_candidate_approve_input,
    decode_candidate_reject_input,
    decode_knowledge_propose_input,
    decode_record_supersede_input,
    idempotency_equivalence,
    validate_candidate_approve_result,
    validate_candidate_reject_result,
    validate_knowledge_propose_result,
    validate_record_supersede_result,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
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
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage.governance import (
    CANDIDATE_APPROVE_OPERATION,
    CANDIDATE_REJECT_OPERATION,
    KNOWLEDGE_PROPOSE_OPERATION,
    RECORD_SUPERSEDE_OPERATION,
    apply_governance_transition,
    read_governance_precondition,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

GOVERNANCE_FAMILY_OPERATIONS: Final = frozenset(
    {
        KNOWLEDGE_PROPOSE_OPERATION,
        CANDIDATE_APPROVE_OPERATION,
        CANDIDATE_REJECT_OPERATION,
        RECORD_SUPERSEDE_OPERATION,
    }
)
_ACTOR_KIND: Final = "user"
_MESSAGE_INVALID: Final = "the request payload is not valid for this governance operation"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_NOT_FOUND: Final = "the requested governed record was not found"
_MESSAGE_WRONG_STATE: Final = "the governed record is not in the required state"

GovernanceRequest = (
    KnowledgeProposeInput
    | CandidateApproveInput
    | CandidateRejectInput
    | RecordSupersedeInput
)


@dataclass(frozen=True)
class GovernanceHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    label_grant: EvidenceLabelGrant
    clock: Clock
    allocate_identifier: IdentifierAllocator

    def knowledge_propose(self, context: OperationContext) -> AuditedOperationResult:
        return self._transition(
            context,
            operation=KNOWLEDGE_PROPOSE_OPERATION,
            decode=decode_knowledge_propose_input,
        )

    def candidate_approve(self, context: OperationContext) -> AuditedOperationResult:
        return self._transition(
            context,
            operation=CANDIDATE_APPROVE_OPERATION,
            decode=decode_candidate_approve_input,
        )

    def candidate_reject(self, context: OperationContext) -> AuditedOperationResult:
        return self._transition(
            context,
            operation=CANDIDATE_REJECT_OPERATION,
            decode=decode_candidate_reject_input,
        )

    def record_supersede(self, context: OperationContext) -> AuditedOperationResult:
        return self._transition(
            context,
            operation=RECORD_SUPERSEDE_OPERATION,
            decode=decode_record_supersede_input,
        )

    def _transition(
        self,
        context: OperationContext,
        *,
        operation: str,
        decode: Any,
    ) -> AuditedOperationResult:
        request: GovernanceRequest | None = None
        try:
            request = decode(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        if context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        precondition_value = context.authorization.mutation_precondition
        if precondition_value is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        equivalence = idempotency_equivalence(
            operation,
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

        def precondition(fenced: Any) -> str | None:
            current = read_governance_precondition(
                fenced,
                workspace_id=context.workspace_id,
                record_id=request.record_id,
                operation=operation,
            )
            if current is not None:
                return current
            exists = fenced.execute(
                "SELECT 1 FROM omnivia_governed_records WHERE workspace_id=? "
                "AND governed_record_id=?",
                (context.workspace_id, request.record_id),
            ).fetchone()
            raise OperationError(
                ERROR_CODE_NOT_FOUND if exists is None else ERROR_CODE_CONFLICT,
                _MESSAGE_NOT_FOUND if exists is None else _MESSAGE_WRONG_STATE,
            )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> dict[str, object]:
            return apply_governance_transition(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                operation=operation,
                request=request,
                required_version=precondition_value.record_version,
                label_grant=self.label_grant,
                allocate_identifier=self.allocate_identifier,
            )

        def valid_result(wire: Any) -> bool:
            try:
                if operation == KNOWLEDGE_PROPOSE_OPERATION:
                    proposed_value = KnowledgeProposeResult.from_wire(wire)
                    assert isinstance(request, KnowledgeProposeInput)
                    validate_knowledge_propose_result(
                        proposed_value,
                        request,
                        precondition_value,
                        context.workspace_id,
                        context.principal,
                        _ACTOR_KIND,
                    )
                elif operation == CANDIDATE_APPROVE_OPERATION:
                    approved_value = CandidateApproveResult.from_wire(wire)
                    assert isinstance(request, CandidateApproveInput)
                    validate_candidate_approve_result(
                        approved_value,
                        request,
                        precondition_value,
                        context.workspace_id,
                        context.principal,
                        _ACTOR_KIND,
                    )
                elif operation == CANDIDATE_REJECT_OPERATION:
                    rejected_value = CandidateRejectResult.from_wire(wire)
                    assert isinstance(request, CandidateRejectInput)
                    validate_candidate_reject_result(
                        rejected_value,
                        request,
                        precondition_value,
                        context.workspace_id,
                        context.principal,
                        _ACTOR_KIND,
                    )
                else:
                    assert operation == RECORD_SUPERSEDE_OPERATION
                    superseded_value = RecordSupersedeResult.from_wire(wire)
                    assert isinstance(request, RecordSupersedeInput)
                    validate_record_supersede_result(
                        superseded_value,
                        request,
                        precondition_value,
                        context.workspace_id,
                        context.principal,
                        _ACTOR_KIND,
                    )
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            precondition=precondition,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


__all__ = [
    "GOVERNANCE_FAMILY_OPERATIONS",
    "GovernanceHandlers",
]
