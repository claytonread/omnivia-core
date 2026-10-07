"""The `knowledge.evaluation.produce` operation (C16b): one mutation over the Stage 2 producer.

The caller submits the exact Stage 2 content it holds and the identifiers and source the evidence ledger
needs. `produce_evaluation_report` derives the report, its status, coverage, findings and integrity digest
from that content, and every canonical record it returns is registered as one evidence item in the
workspace's existing evidence ledger. That write runs inside the same `execute_mutation` transaction as the
audit, claim and outcome, so a refusal anywhere leaves nothing behind.

The caller states no verdict and names no actor or workspace. The principal is the authenticated caller and
the workspace is the one the request selects. The attempts are the principal's submission: the result says
who submitted them, and Core does not claim it observed any external model output.

Evidence identity is the profile's own ID. Each record is registered under its `record_id`, so the ledger holds
the Stage 2 profile by the ID Dev reads it by, and its content and integrity digests are the record's checksum.
The same canonical record submitted again is the same evidence and is reused rather than duplicated. A different
record, source, classification or retention under an existing identity is a conflict.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ContractSemanticError,
    KnowledgeEvaluationEvidenceRecord,
    KnowledgeEvaluationProduceInput,
    KnowledgeEvaluationProduceResult,
    idempotency_equivalence,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeError
from omnivia_core.governed_knowledge.evaluation import evaluation_report_to_content
from omnivia_core.governed_knowledge.stage2_producer import (
    CanonicalRecord,
    Stage2Production,
    produce_evaluation_report,
)
from omnivia_core.semantic_registry.errors import SemanticRegistryError
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceItem,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
)
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)
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
from omnivia_core_runtime.storage.semantic_evidence import (
    EvidenceObservationWriter,
    read_evidence_by_digest,
    read_evidence_item,
    read_evidence_source,
)

OPERATION_EVALUATION_PRODUCE: Final = "knowledge.evaluation.produce"
KNOWLEDGE_EVALUATION_FAMILY_OPERATIONS: Final = frozenset({OPERATION_EVALUATION_PRODUCE})

_INPUT_KEYS: Final = frozenset(
    {
        "overlay",
        "suite",
        "cases",
        "attempts",
        "worker_bindings",
        "report_id",
        "triggering_case_ref",
        "classification",
        "retention_class",
        "source",
    }
)
_SOURCE_KEYS: Final = frozenset({"source_id", "kind", "locator_scheme", "locator", "version"})
_EVIDENCE_MIME_TYPE: Final = "application/json"
_MESSAGE_NO_STORAGE: Final = (
    "the evidence store is not reachable from this service instance"
)
_MESSAGE_INVALID: Final = "the request payload is not valid for this evaluation"
_MESSAGE_CONFLICT: Final = (
    "the submitted evidence conflicts with evidence already registered under the same identity"
)


class _IdentityConflict(Exception):
    """A record or source would replace a different one already in the ledger."""


def _servable(decode: Callable[[object], object]) -> Callable[[Mapping[str, Any]], bool]:
    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_PRODUCE = _servable(KnowledgeEvaluationProduceResult.from_wire)


def _refusing(action: Callable[[], Any]) -> Any:
    """Run one domain step, mapping its refusals onto the contract's error codes.

    A domain validation failure is `invalid_request` with a fixed message, so no submitted value reaches a
    caller. An identity conflict is `conflict`. Nothing here writes, so the caller's rollback covers the rest.
    """
    try:
        return action()
    except _IdentityConflict as conflict:
        raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT) from conflict
    except (GovernedKnowledgeError, SemanticRegistryError, ValueError) as invalid:
        raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from invalid


def _plain(value: Any) -> Any:
    """Decoded wire content holds tuples and mappings; the Stage 2 producer reads plain lists and dicts."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _captured_at(settled_at_us: int) -> TemporalInstant:
    """Core's own capture moment, at the second. No source time is authorised, so it is an ingestion fallback."""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=settled_at_us // 1_000_000)
    return TemporalInstant(
        value=moment,
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.INGESTION_FALLBACK,
    )


def _identity_of(item: EvidenceItem) -> tuple[object, ...]:
    """Everything that makes an evidence item *the same item*. The capture moment is not part of it."""
    return (
        item.evidence_id,
        item.content_ref,
        item.content_digest,
        item.integrity_digest,
        item.mime_type,
        item.classification,
        item.retention_class,
        item.source,
        item.source_time,
        item.span,
    )


def _evidence_item(
    record: CanonicalRecord,
    *,
    workspace_id: str,
    source: EvidenceSource,
    classification: Classification,
    retention_class: str,
    captured_at: TemporalInstant,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=record.record_id,
        workspace_id=workspace_id,
        source=source,
        content_ref="omnivia-content:" + record.checksum,
        content_digest=record.checksum,
        integrity_digest=record.checksum,
        mime_type=_EVIDENCE_MIME_TYPE,
        classification=classification,
        retention_class=retention_class,
        captured_at=captured_at,
    )


def _plan_writes(
    connection: Any,
    *,
    workspace_id: str,
    source: EvidenceSource,
    items: list[EvidenceItem],
) -> list[EvidenceItem]:
    """Refuse every identity conflict before a single write, and return only the items that are new.

    An item already stored under its identity, byte for byte, is reused. Anything else under that identity
    or digest, or a source reused for different metadata, is a conflict. Two records in one submission that
    share an identity are a conflict too, since the second would replace the first.
    """
    stored_source = read_evidence_source(connection, workspace_id, source.source_id)
    if stored_source is not None and stored_source != source:
        raise _IdentityConflict()
    if len({item.evidence_id for item in items}) != len(items):
        raise _IdentityConflict()
    planned: list[EvidenceItem] = []
    for item in items:
        by_id = read_evidence_item(connection, workspace_id, item.evidence_id)
        by_digest = read_evidence_by_digest(connection, workspace_id, item.content_digest)
        if by_id is None and by_digest is None:
            planned.append(item)
        elif (
            by_id is not None
            and by_digest is not None
            and by_id.evidence_id == by_digest.evidence_id
            and _identity_of(by_id) == _identity_of(item)
        ):
            continue
        else:
            raise _IdentityConflict()
    return planned


@dataclass
class KnowledgeEvaluationHandlers:
    """The one evaluation operation over one workspace, under the contributor session it is composed with."""

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Callable[[str], str]

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
    def _input(context: OperationContext) -> KnowledgeEvaluationProduceInput:
        """Decode the request, refusing any key the operation does not declare rather than dropping it."""
        raw = context.request.input
        if not isinstance(raw, Mapping) or not set(raw) <= _INPUT_KEYS:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        source = raw.get("source")
        if not isinstance(source, Mapping) or not set(source) <= _SOURCE_KEYS:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        try:
            return KnowledgeEvaluationProduceInput.from_wire(raw)
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

    def _produce(
        self,
        fenced: Any,
        context: OperationContext,
        request: KnowledgeEvaluationProduceInput,
        settlement: MutationSettlementContext,
    ) -> Mapping[str, Any]:
        """Derive the report, check every identity, then register the records. Runs inside the transaction."""
        production: Stage2Production = _refusing(
            lambda: produce_evaluation_report(
                {
                    "overlay": _plain(request.overlay),
                    "suite": _plain(request.suite),
                    "cases": _plain(request.cases),
                    "attempts": _plain(request.attempts),
                    "worker_bindings": _plain(request.worker_bindings),
                },
                report_id=request.report_id,
                triggering_case_ref=request.triggering_case_ref,
                classification=Classification(request.classification),
                retention_class=request.retention_class,
            )
        )
        source = _refusing(
            lambda: EvidenceSource(
                source_id=request.source.source_id,
                kind=EvidenceSourceKind(request.source.kind),
                locator_scheme=EvidenceLocatorScheme(request.source.locator_scheme),
                locator=request.source.locator,
                version=request.source.version,
            )
        )
        captured_at = _captured_at(settlement.settled_at_us)

        def item_for(record: CanonicalRecord) -> EvidenceItem:
            return _evidence_item(
                record,
                workspace_id=context.workspace_id,
                source=source,
                classification=Classification(request.classification),
                retention_class=request.retention_class,
                captured_at=captured_at,
            )

        items = _refusing(lambda: [item_for(record) for record in production.records])
        # Every identity is checked before the first write, so a conflict on the last record leaves nothing
        # written for the first. The transaction would roll that back too; checking first keeps it obvious.
        planned = _refusing(
            lambda: _plan_writes(
                fenced,
                workspace_id=context.workspace_id,
                source=source,
                items=items,
            )
        )
        writer = EvidenceObservationWriter(fenced, context.workspace_id)
        for item in planned:
            writer.register_evidence(item)
        return KnowledgeEvaluationProduceResult(
            report_id=production.report.report_id,
            report=evaluation_report_to_content(production.report),
            submitted_by=context.principal,
            evidence=tuple(
                KnowledgeEvaluationEvidenceRecord(
                    evidence_id=record.record_id,
                    record_kind=record.kind,
                    record_id=record.record_id,
                    content_digest=record.checksum,
                )
                for record in production.records
            ),
        ).to_wire()

    def knowledge_evaluation_produce(self, context: OperationContext) -> AuditedOperationResult:
        """Derive one evaluation report from submitted Stage 2 content and register its evidence."""
        request = self._input(context)
        payload = request.to_wire()
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, payload)
        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=lambda fenced, settlement: self._produce(fenced, context, request, settlement),
            validate_result=_VALID_PRODUCE,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


__all__ = [
    "KNOWLEDGE_EVALUATION_FAMILY_OPERATIONS",
    "OPERATION_EVALUATION_PRODUCE",
    "KnowledgeEvaluationHandlers",
]
