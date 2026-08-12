"""Authoritative persistence for the V06-5 S4 governance operation family."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CandidateApproveInput,
    CandidateApproveResult,
    CandidateRejectInput,
    CandidateRejectResult,
    GovernedRecord,
    KnowledgeProposeInput,
    KnowledgeProposeResult,
    MemoryCreateInput,
    RecordSupersedeInput,
    RecordSupersedeResult,
    to_canonical_json,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.governed import (
    hydrate_authorized_governed_record_values,
)
from omnivia_core_runtime.storage.memory import (
    IdentifierAllocator,
    resolve_memory_claim_evidence,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

KNOWLEDGE_PROPOSE_OPERATION: Final = "knowledge.propose"
CANDIDATE_APPROVE_OPERATION: Final = "candidate.approve"
CANDIDATE_REJECT_OPERATION: Final = "candidate.reject"
RECORD_SUPERSEDE_OPERATION: Final = "record.supersede"

_AUTHORITY_POLICY_ID: Final = "omnivia.human-governance"
_AUTHORITY_POLICY_VERSION: Final = "1"
_ACTOR_KIND: Final = "user"
_REVIEWER_ROLE: Final = "knowledge_reviewer"
_MESSAGE_NOT_FOUND: Final = "the requested governed record was not found"
_MESSAGE_WRONG_STATE: Final = "the governed record is not in the required state"
_MESSAGE_RECLASSIFIED: Final = "a supersession cannot reclassify the governed record"
_MESSAGE_INVALID_REPLACEMENT: Final = "the replacement claim is outside this supported profile"


@dataclass(frozen=True, slots=True)
class GovernanceSource:
    assembly_id: str
    record_id: str
    version_id: str
    record_type: str
    domain_scope: str
    layer: str
    governance_disposition: str | None
    authority_level: str
    decision_source_id: str | None
    content_json: str
    content_digest: str
    evidence_disposition: str
    assertion_actor_id: str
    assertion_actor_kind: str
    assertion_actor_role: str
    valid_from_us: int
    valid_to_us: int | None
    append_ordinal: int
    claim_json: str
    claim_digest: str
    claim_byte_length: int
    claim_ingested_at_us: int
    claim_operation: str


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def read_governance_precondition(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    operation: str,
) -> str | None:
    """Return the one current application version eligible for ``operation``."""
    expected = {
        KNOWLEDGE_PROPOSE_OPERATION: ("memory.create", "candidate", None, "proposed"),
        CANDIDATE_APPROVE_OPERATION: (
            KNOWLEDGE_PROPOSE_OPERATION,
            "candidate",
            None,
            "proposed",
        ),
        CANDIDATE_REJECT_OPERATION: (
            KNOWLEDGE_PROPOSE_OPERATION,
            "candidate",
            None,
            "proposed",
        ),
        RECORD_SUPERSEDE_OPERATION: (
            (CANDIDATE_APPROVE_OPERATION, RECORD_SUPERSEDE_OPERATION),
            "governed",
            "accepted",
            "canonical",
        ),
    }[operation]
    allowed_operations = (
        expected[0] if isinstance(expected[0], tuple) else (expected[0],)
    )
    placeholders = ", ".join("?" for _ in allowed_operations)
    rows = connection.execute(
        "SELECT a.governed_record_version_id "
        "FROM omnivia_authoritative_governed_versions a "
        "JOIN omnivia_application_claim_lineage l "
        "ON l.workspace_id=a.workspace_id AND l.assembly_id=a.assembly_id "
        "WHERE a.workspace_id=? AND a.governed_record_id=? "
        f"AND l.operation IN ({placeholders}) AND a.layer=? "
        "AND a.governance_disposition IS ? AND a.authority_level=? "
        "AND NOT EXISTS (SELECT 1 FROM omnivia_application_governance_transitions t "
        "WHERE t.workspace_id=a.workspace_id "
        "AND t.source_assembly_id=a.assembly_id "
        "AND t.source_record_version_id=a.governed_record_version_id) "
        "ORDER BY a.recorded_at_us DESC, a.append_ordinal DESC, a.assembly_id DESC",
        (
            workspace_id,
            record_id,
            *allowed_operations,
            expected[1],
            expected[2],
            expected[3],
        ),
    ).fetchall()
    if len(rows) != 1:
        return None
    return str(rows[0][0])


def _source(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    version_id: str,
    operation: str,
) -> GovernanceSource:
    current = read_governance_precondition(
        connection,
        workspace_id=workspace_id,
        record_id=record_id,
        operation=operation,
    )
    if current is None:
        exists = connection.execute(
            "SELECT 1 FROM omnivia_governed_records WHERE workspace_id=? "
            "AND governed_record_id=?",
            (workspace_id, record_id),
        ).fetchone()
        raise OperationError(
            ERROR_CODE_NOT_FOUND if exists is None else ERROR_CODE_CONFLICT,
            _MESSAGE_NOT_FOUND if exists is None else _MESSAGE_WRONG_STATE,
        )
    if current != version_id:
        # The generic S0 executor normally catches this first.  Keeping the storage
        # check makes direct callers fail closed under the same transaction.
        raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_WRONG_STATE)
    row = connection.execute(
        "SELECT a.assembly_id, a.governed_record_id, a.governed_record_version_id, "
        "a.record_type, a.domain_scope, a.layer, a.governance_disposition, "
        "a.authority_level, a.decision_source_id, a.content_json, a.content_digest, "
        "a.evidence_disposition, a.assertion_actor_id, a.assertion_actor_kind, "
        "a.assertion_actor_role, a.valid_from_us, a.valid_to_us, a.append_ordinal, "
        "l.claim_json, l.claim_digest, l.claim_byte_length, l.claim_ingested_at_us, "
        "l.operation FROM omnivia_governed_version_assemblies a "
        "JOIN omnivia_governed_version_seals s "
        "ON s.workspace_id=a.workspace_id AND s.assembly_id=a.assembly_id "
        "JOIN omnivia_application_claim_lineage l "
        "ON l.workspace_id=a.workspace_id AND l.assembly_id=a.assembly_id "
        "WHERE a.workspace_id=? AND a.governed_record_id=? "
        "AND a.governed_record_version_id=?",
        (workspace_id, record_id, version_id),
    ).fetchone()
    if row is None:
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    return GovernanceSource(*row)


def _governed_record(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    version_id: str,
    instant_us: int,
) -> GovernedRecord:
    row = connection.execute(
        "SELECT assembly_id FROM omnivia_authoritative_governed_versions "
        "WHERE workspace_id=? AND governed_record_id=? "
        "AND governed_record_version_id=?",
        (workspace_id, record_id, version_id),
    ).fetchone()
    if row is None:
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    assembly_id = str(row[0])
    support = tuple(
        str(value[0])
        for value in connection.execute(
            "SELECT assembly_id FROM omnivia_authoritative_governed_versions "
            "WHERE workspace_id=? AND governed_record_id=? ORDER BY assembly_id",
            (workspace_id, record_id),
        ).fetchall()
    )
    values = hydrate_authorized_governed_record_values(
        connection,
        workspace_id=workspace_id,
        resolution_instant_us=instant_us,
        assembly_ids=(assembly_id,),
        support_assembly_ids=support,
    )
    if len(values) != 1:
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    return values[0].record


def _insert_evidence_links(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    assembly_id: str,
    event_id: str,
    evidence_ids: tuple[str, ...],
    settled_at_us: int,
) -> None:
    for ordinal, evidence_id in enumerate(evidence_ids, 1):
        connection.execute(
            "INSERT INTO omnivia_governed_version_evidence_links "
            "(workspace_id, assembly_id, provenance_event_id, link_ordinal, "
            "evidence_id, normalized_record_id, normalized_span_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                workspace_id,
                assembly_id,
                event_id,
                ordinal,
                evidence_id,
                settled_at_us,
            ),
        )


def apply_governance_transition(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    principal_id: str,
    operation: str,
    request: KnowledgeProposeInput
    | CandidateApproveInput
    | CandidateRejectInput
    | RecordSupersedeInput,
    required_version: str,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator,
) -> dict[str, object]:
    """Create, seal and bridge one immutable governance transition."""
    source = _source(
        connection,
        workspace_id=workspace_id,
        record_id=request.record_id,
        version_id=required_version,
        operation=operation,
    )
    claim = (
        request.replacement
        if isinstance(request, RecordSupersedeInput)
        else MemoryCreateInput.from_wire(json.loads(source.claim_json))
    )
    if isinstance(request, RecordSupersedeInput):
        if (
            claim.record_type != source.record_type
            or claim.domain_scope != source.domain_scope
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_RECLASSIFIED)
        if claim.record_type != "memory.fact" or claim.extraction is not None:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE
                if claim.extraction is not None
                else ERROR_CODE_INVALID_REQUEST,
                _MESSAGE_INVALID_REPLACEMENT,
                retry_class=(
                    RETRY_CLASS_RETRYABLE_AFTER_DELAY
                    if claim.extraction is not None
                    else "non_retryable"
                ),
            )
        evidence_ids = resolve_memory_claim_evidence(
            connection,
            workspace_id=workspace_id,
            claim=claim,
            label_grant=label_grant,
        )
        content_json = to_canonical_json(dict(claim.content))
        claim_json = to_canonical_json(claim.to_wire())
        claim_digest = _digest(claim_json)
        claim_byte_length = len(claim_json.encode("utf-8"))
        claim_ingested_at_us = settlement.settled_at_us
        assertion = claim.assertion
        evidence_disposition = claim.evidence_disposition
        valid_from_us = _microseconds(
            claim.assertion.proposed_valid_from or claim.assertion.asserted_at
        )
        valid_to_us = (
            None
            if claim.assertion.proposed_valid_until is None
            else _microseconds(claim.assertion.proposed_valid_until)
        )
    else:
        evidence_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT evidence_id FROM omnivia_governed_version_evidence_links "
                "WHERE workspace_id=? AND assembly_id=? ORDER BY evidence_id",
                (workspace_id, source.assembly_id),
            ).fetchall()
        )
        content_json = source.content_json
        claim_json = source.claim_json
        claim_digest = source.claim_digest
        claim_byte_length = source.claim_byte_length
        claim_ingested_at_us = source.claim_ingested_at_us
        assertion = claim.assertion
        evidence_disposition = source.evidence_disposition
        valid_from_us = source.valid_from_us
        valid_to_us = source.valid_to_us

    assembly_id = allocate_identifier("asm")
    version_id = allocate_identifier("ver")
    event_id = allocate_identifier("pev")
    seal_id = allocate_identifier("seal")
    transition_id = allocate_identifier("trn")
    reason_code = request.rationale.reason_code
    reason_comment = request.rationale.comment
    rationale_json = to_canonical_json(request.rationale.to_wire())
    governed = operation != KNOWLEDGE_PROPOSE_OPERATION
    accepted = operation in {CANDIDATE_APPROVE_OPERATION, RECORD_SUPERSEDE_OPERATION}
    rejected = operation == CANDIDATE_REJECT_OPERATION
    action = {
        KNOWLEDGE_PROPOSE_OPERATION: "candidate.human_proposed",
        CANDIDATE_APPROVE_OPERATION: "governance.accepted",
        CANDIDATE_REJECT_OPERATION: "governance.rejected",
        RECORD_SUPERSEDE_OPERATION: "governance.corrected",
    }[operation]

    connection.execute(
        "INSERT INTO omnivia_governed_version_assemblies "
        "(workspace_id, assembly_id, governed_record_id, governed_record_version_id, "
        "record_type, domain_scope, layer, authority_level, governance_disposition, "
        "candidate_origin, extraction_kind, decision_source_kind, decision_source_id, "
        "authority_policy_id, authority_policy_version, policy_decision_ref, "
        "content_schema_version, content_json, content_digest, evidence_disposition, "
        "confidence_ppm, assertion_actor_id, assertion_actor_kind, assertion_actor_role, "
        "reason_code, reason_comment, valid_from_us, valid_to_us, recorded_at_us, "
        "append_ordinal, correlation_kind, correlation_id, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, '1.0', ?, ?, ?, "
        "NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'm1_audit', ?, ?)",
        (
            workspace_id,
            assembly_id,
            source.record_id,
            version_id,
            claim.record_type,
            claim.domain_scope,
            "governed" if governed else "candidate",
            "canonical" if accepted else ("reviewed" if rejected else "proposed"),
            "accepted" if accepted else ("rejected" if rejected else None),
            None if governed else "human_proposed",
            "human_reviewer" if governed else None,
            principal_id if governed else None,
            _AUTHORITY_POLICY_ID if governed else None,
            _AUTHORITY_POLICY_VERSION if governed else None,
            settlement.audit_ref if governed else None,
            content_json,
            _digest(content_json),
            evidence_disposition,
            assertion.actor_id,
            assertion.actor_kind,
            assertion.actor_role,
            reason_code,
            reason_comment,
            valid_from_us,
            valid_to_us,
            settlement.settled_at_us,
            source.append_ordinal + 1,
            settlement.audit_ref,
            settlement.audit_ref,
        ),
    )
    event_actor_id = assertion.actor_id if not governed else principal_id
    event_actor_kind = assertion.actor_kind if not governed else _ACTOR_KIND
    event_actor_role = assertion.actor_role if not governed else _REVIEWER_ROLE
    connection.execute(
        "INSERT INTO omnivia_governed_provenance_events "
        "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
        "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
        "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
        "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
        "predecessor_version_id, evidence_disposition) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, 'm1_audit', ?, "
        "?, ?, ?)",
        (
            workspace_id,
            event_id,
            assembly_id,
            version_id,
            action,
            event_actor_id,
            event_actor_kind,
            event_actor_role,
            (
                _microseconds(assertion.asserted_at)
                if not governed
                else settlement.settled_at_us
            ),
            settlement.settled_at_us,
            reason_code,
            reason_comment,
            settlement.audit_ref,
            settlement.audit_ref,
            None if not governed else source.record_id,
            None if not governed else source.version_id,
            evidence_disposition,
        ),
    )
    _insert_evidence_links(
        connection,
        workspace_id=workspace_id,
        assembly_id=assembly_id,
        event_id=event_id,
        evidence_ids=evidence_ids,
        settled_at_us=settlement.settled_at_us,
    )
    supersession_event_id: str | None = None
    if operation == RECORD_SUPERSEDE_OPERATION:
        supersession_event_id = allocate_identifier("pev")
        connection.execute(
            "INSERT INTO omnivia_governed_provenance_events "
            "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
            "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
            "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
            "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
            "predecessor_version_id, evidence_disposition) "
            "VALUES (?, ?, ?, ?, 2, 'record.superseded', ?, ?, ?, NULL, NULL, ?, ?, ?, ?, "
            "?, 'm1_audit', ?, ?, ?, ?)",
            (
                workspace_id,
                supersession_event_id,
                assembly_id,
                version_id,
                principal_id,
                _ACTOR_KIND,
                _REVIEWER_ROLE,
                settlement.settled_at_us,
                settlement.settled_at_us,
                reason_code,
                reason_comment,
                settlement.audit_ref,
                settlement.audit_ref,
                source.record_id,
                source.version_id,
                evidence_disposition,
            ),
        )
        _insert_evidence_links(
            connection,
            workspace_id=workspace_id,
            assembly_id=assembly_id,
            event_id=supersession_event_id,
            evidence_ids=evidence_ids,
            settled_at_us=settlement.settled_at_us,
        )
        connection.execute(
            "INSERT INTO omnivia_record_supersessions "
            "(workspace_id, supersession_id, assembly_id, governed_record_id, "
            "source_version_id, target_version_id, provenance_event_id, correlation_kind, "
            "correlation_id, recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, 'm1_audit', ?, ?)",
            (
                workspace_id,
                allocate_identifier("sup"),
                assembly_id,
                source.record_id,
                source.version_id,
                version_id,
                supersession_event_id,
                settlement.audit_ref,
                settlement.settled_at_us,
            ),
        )

    connection.execute(
        "INSERT INTO omnivia_governed_version_seals "
        "(workspace_id, seal_id, assembly_id, governed_record_version_id, "
        "correlation_kind, correlation_id, sealed_at_us) "
        "VALUES (?, ?, ?, ?, 'm1_audit', ?, ?)",
        (
            workspace_id,
            seal_id,
            assembly_id,
            version_id,
            settlement.audit_ref,
            settlement.settled_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_application_claim_lineage "
        "(workspace_id, assembly_id, governed_record_version_id, operation, audit_ref, "
        "claim_json, claim_digest, claim_byte_length, claim_ingested_at_us, settled_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            assembly_id,
            version_id,
            operation,
            settlement.audit_ref,
            claim_json,
            claim_digest,
            claim_byte_length,
            claim_ingested_at_us,
            settlement.settled_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_application_governance_transitions "
        "(workspace_id, transition_id, governed_record_id, source_assembly_id, "
        "source_record_version_id, target_assembly_id, target_record_version_id, "
        "operation, rationale_json, rationale_digest, rationale_byte_length, reason_code, "
        "reason_comment, actor_id, actor_kind, audit_ref, settled_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            transition_id,
            source.record_id,
            source.assembly_id,
            source.version_id,
            assembly_id,
            version_id,
            operation,
            rationale_json,
            _digest(rationale_json),
            len(rationale_json.encode("utf-8")),
            reason_code,
            reason_comment,
            principal_id,
            _ACTOR_KIND,
            settlement.audit_ref,
            settlement.settled_at_us,
        ),
    )

    previous = _governed_record(
        connection,
        workspace_id=workspace_id,
        record_id=source.record_id,
        version_id=source.version_id,
        instant_us=settlement.settled_at_us,
    )
    updated = _governed_record(
        connection,
        workspace_id=workspace_id,
        record_id=source.record_id,
        version_id=version_id,
        instant_us=settlement.settled_at_us,
    )
    result: dict[str, object] = {
        "previous_record": previous.to_wire(),
        "updated_record": updated.to_wire(),
    }
    # Decode at the persistence boundary so a result that cannot satisfy the
    # operation's generated shape never reaches the generic settlement store.
    if operation == KNOWLEDGE_PROPOSE_OPERATION:
        KnowledgeProposeResult.from_wire(result)
    elif operation == CANDIDATE_APPROVE_OPERATION:
        CandidateApproveResult.from_wire(result)
    elif operation == CANDIDATE_REJECT_OPERATION:
        CandidateRejectResult.from_wire(result)
    else:
        RecordSupersedeResult.from_wire(result)
    return result


def _microseconds(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value).timestamp() * 1_000_000)


__all__ = [
    "CANDIDATE_APPROVE_OPERATION",
    "CANDIDATE_REJECT_OPERATION",
    "KNOWLEDGE_PROPOSE_OPERATION",
    "RECORD_SUPERSEDE_OPERATION",
    "apply_governance_transition",
    "read_governance_precondition",
]
