"""Authoritative persistence for the V06-5 S2 memory operation family."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, cast

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CandidateAssertion,
    GovernedRecord,
    MemoryCreateInput,
    MemoryCreateResult,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    SourceReference,
    resolve_governed_record_view,
    to_canonical_json,
    validate_memory_create_result,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.governed import (
    GovernedRecordValue,
    hydrate_authorized_governed_record_values,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

IdentifierAllocator = Callable[[str], str]

_PROFILE_TYPE: Final = "memory.fact"
_MESSAGE_INVALID_PROFILE: Final = "the memory claim is outside this supported profile"
_MESSAGE_EVIDENCE_UNAVAILABLE: Final = (
    "the memory claim's evidence is not currently available"
)


@dataclass(frozen=True, slots=True)
class AuthorizedMemorySnapshot:
    resolution_instant_us: int
    view: str
    values: tuple[GovernedRecordValue, ...]
    digest: str


def random_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _microseconds(value: str) -> int:
    moment = datetime.fromisoformat(value)
    return int(moment.timestamp() * 1_000_000)


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _fold_labels(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    held: set[str] = set()
    for _sequence, action, label in rows:
        if str(action) == "withdrawn":
            held.discard(str(label))
        else:
            held.add(str(label))
    return tuple(sorted(held))


def _source_key(source: SourceReference) -> tuple[str, str, str | None, int | None]:
    return (
        source.kind,
        source.source_id,
        source.locator,
        None if source.retrieved_at is None else _microseconds(source.retrieved_at),
    )


def _resolve_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    claim: MemoryCreateInput,
    label_grant: EvidenceLabelGrant,
) -> tuple[str, ...]:
    if claim.evidence_disposition != "available":
        if claim.sources or claim.assertion.evidence:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)
        return ()

    resolved: dict[tuple[str, str, str | None, int | None], str] = {}
    for source in claim.sources:
        key = _source_key(source)
        rows = connection.execute(
            "SELECT evidence_id FROM omnivia_evidence_artifacts "
            "WHERE workspace_id = ? AND source_kind = ? AND source_native_id = ? "
            "AND source_locator IS ? AND source_retrieved_at_us IS ? "
            "ORDER BY evidence_id ASC",
            (workspace_id, *key),
        ).fetchall()
        if len(rows) != 1:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        evidence_id = str(rows[0][0])
        labels = _fold_labels(
            connection.execute(
                "SELECT label_sequence, label_action, permission_label "
                "FROM omnivia_evidence_permission_labels "
                "WHERE workspace_id = ? AND evidence_id = ? "
                "ORDER BY label_sequence ASC",
                (workspace_id, evidence_id),
            ).fetchall()
        )
        if not label_grant.permits(labels):
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        resolved[key] = evidence_id

    resolved_by_source = {
        (source.kind, source.source_id): resolved[_source_key(source)]
        for source in claim.sources
    }
    for evidence in claim.assertion.evidence:
        if evidence.span is not None or evidence.excerpt is not None:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        if (evidence.source.kind, evidence.source.source_id) not in resolved_by_source:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)
    return tuple(resolved[_source_key(source)] for source in claim.sources)


def create_memory_record(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    claim: MemoryCreateInput,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> dict[str, object]:
    """Persist one sealed human proposal plus its immutable application lineage."""
    fact = claim.content.get("fact")
    if (
        claim.record_type != _PROFILE_TYPE
        or not isinstance(fact, str)
        or not fact
        or claim.extraction is not None
    ):
        code = (
            ERROR_CODE_DEPENDENCY_UNAVAILABLE
            if claim.extraction is not None
            else ERROR_CODE_INVALID_REQUEST
        )
        retry = (
            RETRY_CLASS_RETRYABLE_AFTER_DELAY
            if claim.extraction is not None
            else "non_retryable"
        )
        raise OperationError(code, _MESSAGE_INVALID_PROFILE, retry_class=retry)

    evidence_ids = _resolve_evidence(
        connection,
        workspace_id=workspace_id,
        claim=claim,
        label_grant=label_grant,
    )
    asserted_at_us = _microseconds(claim.assertion.asserted_at)
    valid_from_us = _microseconds(
        claim.assertion.proposed_valid_from or claim.assertion.asserted_at
    )
    valid_to_us = (
        None
        if claim.assertion.proposed_valid_until is None
        else _microseconds(claim.assertion.proposed_valid_until)
    )
    event_at_us = None if claim.event_at is None else _microseconds(claim.event_at)
    observed_at_us = (
        None if claim.observed_at is None else _microseconds(claim.observed_at)
    )
    if (
        asserted_at_us > settlement.settled_at_us
        or (event_at_us is not None and event_at_us > settlement.settled_at_us)
        or (observed_at_us is not None and observed_at_us > settlement.settled_at_us)
    ):
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)

    record_id = allocate_identifier("rec")
    version_id = allocate_identifier("ver")
    assembly_id = allocate_identifier("asm")
    event_id = allocate_identifier("pev")
    seal_id = allocate_identifier("seal")
    content_json = to_canonical_json(dict(claim.content))
    claim_json = to_canonical_json(claim.to_wire())
    reason = (
        None if claim.evidence_disposition == "available" else "evidence.unavailable"
    )

    connection.execute(
        "INSERT INTO omnivia_governed_records "
        "(workspace_id, governed_record_id, record_type, domain_scope, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            workspace_id,
            record_id,
            claim.record_type,
            claim.domain_scope,
            settlement.settled_at_us,
        ),
    )
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
        "VALUES (?, ?, ?, ?, ?, ?, 'candidate', 'proposed', NULL, 'human_proposed', "
        "NULL, NULL, NULL, NULL, NULL, NULL, '1.0', ?, ?, ?, NULL, ?, ?, ?, ?, NULL, "
        "?, ?, ?, 1, 'm1_audit', ?, ?)",
        (
            workspace_id,
            assembly_id,
            record_id,
            version_id,
            claim.record_type,
            claim.domain_scope,
            content_json,
            _digest(content_json),
            claim.evidence_disposition,
            claim.assertion.actor_id,
            claim.assertion.actor_kind,
            claim.assertion.actor_role,
            reason,
            valid_from_us,
            valid_to_us,
            settlement.settled_at_us,
            settlement.audit_ref,
            settlement.audit_ref,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_governed_provenance_events "
        "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
        "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
        "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
        "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
        "predecessor_version_id, evidence_disposition) "
        "VALUES (?, ?, ?, ?, 1, 'candidate.human_proposed', ?, ?, ?, NULL, NULL, ?, ?, "
        "?, NULL, ?, 'm1_audit', ?, NULL, NULL, ?)",
        (
            workspace_id,
            event_id,
            assembly_id,
            version_id,
            claim.assertion.actor_id,
            claim.assertion.actor_kind,
            claim.assertion.actor_role,
            asserted_at_us,
            settlement.settled_at_us,
            reason,
            settlement.audit_ref,
            settlement.audit_ref,
            claim.evidence_disposition,
        ),
    )
    for ordinal, evidence_id in enumerate(evidence_ids, 1):
        connection.execute(
            "INSERT INTO omnivia_governed_version_evidence_links "
            "(workspace_id, assembly_id, provenance_event_id, link_ordinal, evidence_id, "
            "normalized_record_id, normalized_span_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                workspace_id,
                assembly_id,
                event_id,
                ordinal,
                evidence_id,
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
        "VALUES (?, ?, ?, 'memory.create', ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            assembly_id,
            version_id,
            settlement.audit_ref,
            claim_json,
            _digest(claim_json),
            len(claim_json.encode("utf-8")),
            settlement.settled_at_us,
            settlement.settled_at_us,
        ),
    )

    at = _timestamp(settlement.settled_at_us)
    temporal = RecordTemporalMetadata(
        event_at=claim.event_at,
        observed_at=claim.observed_at,
        ingested_at=at,
        recorded_at=at,
        valid_from=claim.assertion.proposed_valid_from,
        valid_until=claim.assertion.proposed_valid_until,
    )
    record = GovernedRecord(
        workspace_id=workspace_id,
        record_type=claim.record_type,
        domain_scope=claim.domain_scope,
        authority_level="proposed",
        reviewer=None,
        provenance=RecordProvenance(
            identity=RecordIdentity(
                record_id=record_id,
                version=version_id,
                layer="l1",
                governance_state="proposed",
                currentness="current",
            ),
            temporal=temporal,
            history=(),
            evidence_disposition=claim.evidence_disposition,
            sources=claim.sources,
            assertion=CandidateAssertion.from_wire(claim.assertion.to_wire()),
        ),
        content=claim.content,
    )
    result = MemoryCreateResult(record=record)
    validate_memory_create_result(result, workspace_id)
    return result.to_wire()


def read_authorized_memory_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
) -> AuthorizedMemorySnapshot:
    """Select identity and ACL facts first, then hydrate only admitted assemblies."""
    resolved_view = resolve_governed_record_view(view)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        rows = connection.execute(
            "SELECT assembly_id, governed_record_id, governed_record_version_id, layer, "
            "governance_disposition, authority_level, valid_from_us, valid_to_us, "
            "recorded_at_us, append_ordinal, correlation_kind, correlation_id "
            "FROM omnivia_authoritative_governed_versions "
            "WHERE workspace_id = ? AND recorded_at_us <= ? "
            "ORDER BY governed_record_id, recorded_at_us, append_ordinal, assembly_id",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        # This first phase may read only identities and the minimum currentness
        # facts required to select the view.  In particular, do not join the
        # provenance event that carries the public supersession reason until the
        # record's evidence grant has been evaluated below.
        supersessions = connection.execute(
            "SELECT r.governed_record_id, r.source_version_id, "
            "r.target_version_id, r.assembly_id, "
            "MAX(r.recorded_at_us, t.recorded_at_us) "
            "FROM omnivia_record_supersessions r "
            "JOIN omnivia_governed_version_seals s "
            "ON s.workspace_id = r.workspace_id AND s.assembly_id = r.assembly_id "
            "JOIN omnivia_authoritative_governed_versions t "
            "ON t.workspace_id = r.workspace_id AND t.assembly_id = r.assembly_id "
            "AND t.governed_record_version_id = r.target_version_id "
            "WHERE r.workspace_id = ? "
            "AND MAX(r.recorded_at_us, t.recorded_at_us) <= ? "
            "ORDER BY r.source_version_id, r.target_version_id, r.assembly_id",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        replaced = {
            str(row[1]): int(row[4]) for row in supersessions
        }
        # Endpoint identity is needed both to remove transitioned candidates and
        # to include every supporting assembly in the evidence-label fold.  All
        # public transition material remains deferred until `authorized_ids` is
        # frozen.
        application_transition_endpoints = connection.execute(
            "SELECT governed_record_id, source_assembly_id, source_record_version_id, "
            "target_assembly_id, target_record_version_id "
            "FROM omnivia_application_governance_transitions "
            "WHERE workspace_id = ? AND settled_at_us <= ? "
            "ORDER BY governed_record_id, source_record_version_id, "
            "target_record_version_id, source_assembly_id, target_assembly_id",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        application_replaced = {
            str(row[2]) for row in application_transition_endpoints
        }
        if resolved_view == "candidates":
            selected = [
                row
                for row in rows
                if str(row[3]) == "candidate"
                and str(row[2]) not in application_replaced
            ]
        else:
            canonical = [
                row
                for row in rows
                if str(row[3]) == "governed"
                and str(row[4]) == "accepted"
                and str(row[5]) == "canonical"
            ]
            if resolved_view == "history":
                selected = [
                    row
                    for row in canonical
                    if replaced.get(str(row[2]), resolution_instant_us + 1)
                    <= resolution_instant_us
                ]
            else:
                frontier: dict[str, tuple[object, ...]] = {}
                for row in canonical:
                    version_id = str(row[2])
                    if (
                        replaced.get(version_id, resolution_instant_us + 1)
                        <= resolution_instant_us
                    ):
                        continue
                    valid_to = None if row[7] is None else int(row[7])
                    if not (
                        int(row[6]) <= resolution_instant_us
                        and (valid_to is None or resolution_instant_us < valid_to)
                    ):
                        continue
                    key = (
                        int(row[8]),
                        str(row[10]),
                        str(row[11]),
                        int(row[9]),
                        str(row[0]),
                    )
                    held = frontier.get(str(row[1]))
                    held_key = (
                        None
                        if held is None
                        else (
                            cast("int", held[8]),
                            str(held[10]),
                            str(held[11]),
                            cast("int", held[9]),
                            str(held[0]),
                        )
                    )
                    if held_key is None or key > held_key:
                        frontier[str(row[1])] = row
                selected = [frontier[key] for key in sorted(frontier)]

        assembly_ids = tuple(str(row[0]) for row in selected)
        record_by_assembly = {str(row[0]): str(row[1]) for row in selected}
        support_by_record: dict[str, set[str]] = {
            record_id: set() for record_id in record_by_assembly.values()
        }
        for transition in application_transition_endpoints:
            (
                record_id,
                source_assembly,
                _source_version,
                target_assembly,
                _target_version,
            ) = transition[:5]
            if str(record_id) in support_by_record:
                support_by_record[str(record_id)].update(
                    {str(source_assembly), str(target_assembly)}
                )
        for assembly_id, record_id in record_by_assembly.items():
            support_by_record[record_id].add(assembly_id)
        support_ids = tuple(
            sorted({item for values in support_by_record.values() for item in values})
        )
        evidence_rows: list[tuple[object, ...]] = []
        label_rows: list[tuple[object, ...]] = []
        if support_ids:
            placeholders = ", ".join("?" for _ in support_ids)
            evidence_rows = connection.execute(
                "SELECT assembly_id, evidence_id FROM omnivia_governed_version_evidence_links "
                f"WHERE workspace_id = ? AND assembly_id IN ({placeholders}) "
                "ORDER BY assembly_id, evidence_id",
                (workspace_id, *support_ids),
            ).fetchall()
            evidence_ids = tuple(sorted({str(row[1]) for row in evidence_rows}))
            if evidence_ids:
                evidence_placeholders = ", ".join("?" for _ in evidence_ids)
                label_rows = connection.execute(
                    "SELECT evidence_id, label_sequence, label_action, permission_label "
                    "FROM omnivia_evidence_permission_labels WHERE workspace_id = ? "
                    f"AND evidence_id IN ({evidence_placeholders}) "
                    "ORDER BY evidence_id, label_sequence",
                    (workspace_id, *evidence_ids),
                ).fetchall()
        labels_by_evidence: dict[str, list[tuple[object, ...]]] = {}
        for evidence_id, sequence, action, label in label_rows:
            labels_by_evidence.setdefault(str(evidence_id), []).append(
                (sequence, action, label)
            )
        evidence_by_assembly: dict[str, list[str]] = {}
        for evidence_assembly_id, evidence_id in evidence_rows:
            evidence_by_assembly.setdefault(str(evidence_assembly_id), []).append(
                str(evidence_id)
            )
        authorized_ids = tuple(
            assembly_id
            for assembly_id in assembly_ids
            if all(
                label_grant.permits(
                    _fold_labels(labels_by_evidence.get(evidence_id, []))
                )
                for support_id in support_by_record[record_by_assembly[assembly_id]]
                for evidence_id in evidence_by_assembly.get(support_id, [])
            )
        )
        authorized_support_ids = tuple(
            sorted(
                {
                    support_id
                    for assembly_id in authorized_ids
                    for support_id in support_by_record[record_by_assembly[assembly_id]]
                }
            )
        )
        authorized_record_ids = tuple(
            sorted(
                {
                    record_by_assembly[assembly_id]
                    for assembly_id in authorized_ids
                }
            )
        )
        application_transitions: list[tuple[object, ...]] = []
        if authorized_record_ids:
            record_placeholders = ", ".join("?" for _ in authorized_record_ids)
            application_transitions = connection.execute(
                "SELECT governed_record_id, source_assembly_id, "
                "source_record_version_id, target_assembly_id, "
                "target_record_version_id, transition_id, operation, "
                "rationale_digest, rationale_byte_length, reason_code, "
                "reason_comment, actor_id, actor_kind, audit_ref, settled_at_us "
                "FROM omnivia_application_governance_transitions "
                "WHERE workspace_id = ? "
                f"AND governed_record_id IN ({record_placeholders}) "
                "AND settled_at_us <= ? "
                "ORDER BY governed_record_id, settled_at_us, transition_id",
                (workspace_id, *authorized_record_ids, resolution_instant_us),
            ).fetchall()
        digest_document = to_canonical_json(
            {
                "view_policy": "memory-s2-v1",
                "view": resolved_view,
                "resolution_instant_us": resolution_instant_us,
                "frontier": [
                    [str(row[0]), str(row[1]), str(row[2]), int(row[8])]
                    for row in selected
                    if str(row[0]) in authorized_ids
                ],
                "evidence": [
                    list(map(str, row))
                    for row in evidence_rows
                    if str(row[0]) in authorized_support_ids
                ],
                "label_stream": [
                    [str(item) for item in row]
                    for row in label_rows
                    if any(
                        str(e[1]) == str(row[0]) and str(e[0]) in authorized_support_ids
                        for e in evidence_rows
                    )
                ],
                "transition_chain": [
                    [None if item is None else str(item) for item in row]
                    for row in application_transitions
                    if str(row[0]) in set(authorized_record_ids)
                ],
                "grant": {
                    "principal_id": label_grant.principal_id,
                    "workspace_id": label_grant.workspace_id,
                    "all_labels": label_grant.all_labels,
                    "labels": sorted(label_grant.labels),
                },
            }
        )
        values = hydrate_authorized_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            assembly_ids=authorized_ids,
            support_assembly_ids=authorized_support_ids,
        )
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")
    return AuthorizedMemorySnapshot(
        resolution_instant_us=resolution_instant_us,
        view=resolved_view,
        values=values,
        digest=_digest(digest_document),
    )


__all__ = [
    "AuthorizedMemorySnapshot",
    "IdentifierAllocator",
    "create_memory_record",
    "random_identifier",
    "read_authorized_memory_snapshot",
]
