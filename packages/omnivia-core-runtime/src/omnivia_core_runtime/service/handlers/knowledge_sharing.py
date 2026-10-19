"""The four `knowledge.share.*` operations of explicit cross-Project knowledge sharing (DEV-REQ-081).

Propose and decide are mutations and run through `execute_mutation`, so their grant, idempotency,
audit and rollback are the ones every mutation has. Read and lineage are observations.

Project authority is not read from a request. Every handler takes the authenticated principal from
the authorization seam's context and decides against the `ProjectAuthority` this family was composed
with, so the payload carries no source Project at all and its recipient Project names only the
addressee of a proposal. A recipient read and a lineage read take the Project from the stored share
and require the principal to be bound to it. The domain rules are in `service/knowledge_sharing.py`;
this module maps their closed refusals onto the contract's error codes and builds the wire results.

A refusal carries a fixed message and no caller, Project or stored value. A stored row that fails its
digest is a corruption fault, never an ineligible share.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    KnowledgeShareDecideInput,
    KnowledgeShareDecideResult,
    KnowledgeShareDecisionRecord,
    KnowledgeShareLineageInput,
    KnowledgeShareLineageResult,
    KnowledgeShareProposeInput,
    KnowledgeShareProposeResult,
    KnowledgeShareReadInput,
    KnowledgeShareReadResult,
    idempotency_equivalence,
)
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.knowledge_sharing import (
    NO_PROJECTS,
    OPERATION_DECIDE,
    OPERATION_LINEAGE,
    OPERATION_PROPOSE,
    OPERATION_READ,
    REFUSED_NOT_ELIGIBLE,
    REFUSED_NOT_FOUND,
    REFUSED_NOT_OWNER,
    REFUSED_SELF_DECISION,
    REFUSED_SELF_SHARE,
    REFUSED_UNKNOWN_RECIPIENT,
    REFUSED_UNKNOWN_RECORD,
    KnowledgeShareRefused,
    ProjectAuthority,
    decide_share,
    propose_share,
    read_share_lineage,
    read_shared_lesson,
    require_share_owner,
    share_state,
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
from omnivia_core_runtime.storage.knowledge_shares import (
    KnowledgeShareConflict,
    KnowledgeShareInvalid,
    read_decisions,
)

KNOWLEDGE_SHARING_FAMILY_OPERATIONS: Final = frozenset(
    {OPERATION_PROPOSE, OPERATION_DECIDE, OPERATION_READ, OPERATION_LINEAGE}
)

_MESSAGE_NO_STORAGE: Final = "the knowledge sharing store is not reachable from this service instance"
_MESSAGE_INVALID: Final = "the request payload is not valid for this sharing operation"
_MESSAGE_NOT_FOUND: Final = "no such record, Project or share is visible to the caller"
_MESSAGE_DENIED: Final = "the caller does not own the Project that holds this record"
_MESSAGE_SELF_DECISION: Final = "a share must be accepted by an owner other than its proposer"
_MESSAGE_CONFLICT: Final = "the share is not in a state that allows this request"
_MESSAGE_CORRUPT: Final = "the stored share failed its integrity check"

_STATUS_BY_REASON: Final[Mapping[str, tuple[str, str]]] = {
    REFUSED_NOT_FOUND: (ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND),
    REFUSED_UNKNOWN_RECORD: (ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND),
    REFUSED_UNKNOWN_RECIPIENT: (ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND),
    REFUSED_NOT_OWNER: (ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_DENIED),
    REFUSED_SELF_DECISION: (ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_SELF_DECISION),
    REFUSED_SELF_SHARE: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_NOT_ELIGIBLE: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
}


def _timestamp(microseconds: int) -> str:
    """One microsecond instant, spelled as the wire's UTC `Timestamp`."""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def _servable(decode: Callable[[object], object]) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a result, fresh or replayed, still decodes as the contract's result type."""

    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_PROPOSE = _servable(KnowledgeShareProposeResult.from_wire)
_VALID_DECIDE = _servable(KnowledgeShareDecideResult.from_wire)


def _refusing(action: Callable[[], Any]) -> Any:
    """Run one domain step, mapping its closed refusals onto the contract's error codes.

    A refusal reason with no entry is a `conflict`, a share that fails its digest is a fault, and a
    storage conflict is a `conflict`. The messages are fixed, so no identifier, Project or principal
    reaches a caller.
    """
    try:
        return action()
    except KnowledgeShareRefused as refused:
        code, message = _STATUS_BY_REASON.get(
            refused.reason, (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT)
        )
        raise application_refusal(code, message) from refused
    except KnowledgeShareConflict as conflict:
        raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT) from conflict
    except KnowledgeShareInvalid as invalid:
        raise application_refusal(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_CORRUPT
        ) from invalid
    except sqlite3.IntegrityError as integrity:
        # A guard trigger refused a row the domain checks had allowed, such as a source version that
        # was superseded between the check and the insert. The request lost a race, not a lookup.
        raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT) from integrity


@dataclass
class KnowledgeSharingHandlers:
    """The four sharing operations over one workspace, deciding against one `ProjectAuthority`.

    `projects` is fixed when the service is composed. Nothing a request carries can add to it, and the
    default is empty, which refuses every sharing operation.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Callable[[str], str]
    projects: ProjectAuthority = NO_PROJECTS

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection

    def _authority(self) -> tuple[Any, Any, MutationGuard]:
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection, identity, guard

    @staticmethod
    def _input(context: OperationContext, allowed: frozenset[str], decode: Any) -> Any:
        """Decode the request, refusing a key the operation does not declare rather than dropping it.

        The contract's decoder ignores unknown members, so a request that names a Project it has no
        field for would otherwise be decoded as if it had not. It is refused instead.
        """
        raw = context.request.input
        if not isinstance(raw, Mapping) or not set(raw) <= allowed:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        try:
            return decode(raw)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, Any]:
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

    def _mutate(
        self,
        context: OperationContext,
        payload: Mapping[str, Any],
        share_id: str,
        mutate: Callable[[Any, MutationGrant, MutationSettlementContext], Mapping[str, Any]],
        valid: Callable[[Mapping[str, Any]], bool],
    ) -> AuditedOperationResult:
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, payload)

        def replay_authority(fenced: Any) -> None:
            _refusing(
                lambda: require_share_owner(
                    fenced,
                    self.projects,
                    principal=context.principal,
                    workspace_id=context.workspace_id,
                    share_id=share_id,
                )
            )

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            replay_authority=replay_authority,
            mutate=lambda fenced, settlement: mutate(fenced, grant, settlement),
            validate_result=valid,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def knowledge_share_propose(self, context: OperationContext) -> AuditedOperationResult:
        """Propose sharing a record's current sealed version from the Project that owns its scope."""
        request = self._input(
            context,
            frozenset({"share_id", "record_id", "recipient_project_id"}),
            KnowledgeShareProposeInput.from_wire,
        )

        def mutate(
            fenced: Any, grant: MutationGrant, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            share = _refusing(
                lambda: propose_share(
                    fenced,
                    self.projects,
                    principal=context.principal,
                    workspace_id=context.workspace_id,
                    generation=grant.fencing_generation,
                    share_id=request.share_id,
                    record_id=request.record_id,
                    recipient_project_id=request.recipient_project_id,
                    proposed_at_us=settlement.settled_at_us,
                )
            )
            return KnowledgeShareProposeResult(
                share_id=share.share_id,
                source_project_id=share.source_project_id,
                recipient_project_id=share.recipient_project_id,
                record_id=share.governed_record_id,
                governed_record_version_id=share.governed_record_version_id,
                content_digest=share.content_digest,
                state="proposed",
                proposed_at=_timestamp(share.proposed_at_us),
            ).to_wire()

        return self._mutate(
            context, request.to_wire(), request.share_id, mutate, _VALID_PROPOSE
        )

    def knowledge_share_decide(self, context: OperationContext) -> AuditedOperationResult:
        """Accept or revoke one share as an owner of its source Project."""
        request = self._input(
            context,
            frozenset({"share_id", "decision"}),
            KnowledgeShareDecideInput.from_wire,
        )

        def mutate(
            fenced: Any, grant: MutationGrant, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            decision = _refusing(
                lambda: decide_share(
                    fenced,
                    self.projects,
                    principal=context.principal,
                    workspace_id=context.workspace_id,
                    generation=grant.fencing_generation,
                    share_id=request.share_id,
                    decision=request.decision,
                    decided_at_us=settlement.settled_at_us,
                )
            )
            decisions = read_decisions(
                fenced, workspace_id=context.workspace_id, share_id=request.share_id
            )
            return KnowledgeShareDecideResult(
                share_id=request.share_id,
                decision=decision.decision,
                state=share_state(decisions),
                decided_at=_timestamp(decision.decided_at_us),
            ).to_wire()

        return self._mutate(
            context, request.to_wire(), request.share_id, mutate, _VALID_DECIDE
        )

    def knowledge_share_read(self, context: OperationContext) -> Mapping[str, Any]:
        """Serve the shared version to a member of its recipient Project, only while it is eligible."""
        request = self._input(
            context, frozenset({"share_id"}), KnowledgeShareReadInput.from_wire
        )
        connection = self._connection()
        lesson = _refusing(
            lambda: read_shared_lesson(
                connection,
                self.projects,
                principal=context.principal,
                workspace_id=context.workspace_id,
                share_id=request.share_id,
            )
        )
        content = json.loads(lesson.content_json)
        if not isinstance(content, dict):
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_CORRUPT)
        share = lesson.share
        return KnowledgeShareReadResult(
            share_id=share.share_id,
            source_project_id=share.source_project_id,
            recipient_project_id=share.recipient_project_id,
            record_id=share.governed_record_id,
            governed_record_version_id=share.governed_record_version_id,
            content_digest=share.content_digest,
            domain_scope=share.domain_scope,
            content=content,
        ).to_wire()

    def knowledge_share_lineage(self, context: OperationContext) -> Mapping[str, Any]:
        """Serve a share and every decision against it to an owner of its source Project."""
        request = self._input(
            context, frozenset({"share_id"}), KnowledgeShareLineageInput.from_wire
        )
        connection = self._connection()
        lineage = _refusing(
            lambda: read_share_lineage(
                connection,
                self.projects,
                principal=context.principal,
                workspace_id=context.workspace_id,
                share_id=request.share_id,
            )
        )
        share = lineage.share
        return KnowledgeShareLineageResult(
            share_id=share.share_id,
            source_project_id=share.source_project_id,
            recipient_project_id=share.recipient_project_id,
            record_id=share.governed_record_id,
            governed_record_version_id=share.governed_record_version_id,
            content_digest=share.content_digest,
            state=lineage.state,
            proposed_by=share.proposed_by,
            decisions=tuple(
                KnowledgeShareDecisionRecord(
                    decision=decision.decision,
                    decided_by=decision.decided_by,
                    decided_at=_timestamp(decision.decided_at_us),
                )
                for decision in lineage.decisions
            ),
        ).to_wire()


__all__ = ["KNOWLEDGE_SHARING_FAMILY_OPERATIONS", "KnowledgeSharingHandlers"]
