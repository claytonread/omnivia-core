"""Fenced Phase 2 governed-assertion and candidate persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from omnivia_core.semantic_registry import (
    AssertionEvidence,
    AssertionRetraction,
    AssertionSupersession,
    CandidateBand,
    CandidateContribution,
    CandidateReconsideration,
    CandidateRiskBand,
    CandidateState,
    CandidateSuppression,
    ChangeOperation,
    Classification,
    ContributionRole,
    EndBoundaryState,
    EvidenceSupportRole,
    KnowledgeAssertion,
    KnowledgeObjectKind,
    OperationKind,
    SemanticCandidate,
    TemporalInstant,
    assertion_digest,
    assertion_effective_interval,
    assertion_evidence_digest,
    assertion_retraction_digest,
    assertion_supersession_digest,
    candidate_contribution_digest,
    candidate_digest,
    candidate_equivalence_signature,
    operation_payload,
    reconsideration_digest,
    suppression_active,
    suppression_digest,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.semantic_evidence import (
    _confidence_from_ppm,
    _confidence_to_ppm,
    _from_us,
    _optional_instant,
    _to_us,
)
from omnivia_core_runtime.storage.semantic_registry import (
    SemanticRegistryWriter,
    canonical_text,
)


@dataclass(frozen=True, slots=True)
class AssertionRecord:
    assertion: KnowledgeAssertion
    evidence: tuple[AssertionEvidence, ...]


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate: SemanticCandidate
    contributions: tuple[CandidateContribution, ...]


def _operation_from_text(document: str) -> ChangeOperation:
    value: dict[str, Any] = json.loads(document)
    value["kind"] = OperationKind(value["kind"])
    value["depends_on_operation_ids"] = tuple(value["depends_on_operation_ids"])
    value["evidence_refs"] = tuple(value["evidence_refs"])
    value["rationale"] = None
    return ChangeOperation(**value)


def read_assertion(
    connection: sqlite3.Connection, workspace_id: str, assertion_id: str
) -> AssertionRecord | None:
    row = connection.execute(
        "SELECT subject_id,predicate_element_id,model_version_id,object_kind,object_id,"
        "literal_json,confidence_ppm,classification,valid_from_us,valid_from_precision,"
        "valid_from_provenance,valid_from_original_text,valid_from_timezone,"
        "valid_to_state,valid_to_us,valid_to_precision,valid_to_provenance,"
        "valid_to_original_text,valid_to_timezone,attested_from_us,"
        "attested_from_precision,attested_from_provenance,"
        "attested_from_original_text,attested_from_timezone,attested_to_us,"
        "attested_to_precision,attested_to_provenance,attested_to_original_text,"
        "attested_to_timezone,recorded_at_us,recorded_at_precision,"
        "recorded_at_provenance,recorded_until_us,recorded_until_precision,"
        "recorded_until_provenance FROM omnivia_semantic_assertions "
        "WHERE workspace_id=? AND assertion_id=?",
        (workspace_id, assertion_id),
    ).fetchone()
    if row is None:
        return None
    assertion = KnowledgeAssertion(
        assertion_id=assertion_id,
        workspace_id=workspace_id,
        subject_id=str(row[0]),
        predicate_element_id=str(row[1]),
        model_version_id=str(row[2]),
        object_kind=KnowledgeObjectKind(str(row[3])),
        object_id=None if row[4] is None else str(row[4]),
        literal_value=None if row[5] is None else json.loads(str(row[5])),
        confidence=_confidence_from_ppm(int(row[6])),
        classification=Classification(str(row[7])),
        valid_from=_optional_instant(
            None if row[8] is None else int(row[8]),
            None if row[9] is None else str(row[9]),
            None if row[10] is None else str(row[10]),
            None if row[11] is None else str(row[11]),
            None if row[12] is None else str(row[12]),
        ),
        valid_to_state=EndBoundaryState(str(row[13])),
        valid_to=_optional_instant(
            None if row[14] is None else int(row[14]),
            None if row[15] is None else str(row[15]),
            None if row[16] is None else str(row[16]),
            None if row[17] is None else str(row[17]),
            None if row[18] is None else str(row[18]),
        ),
        attested_from=_from_us(
            int(row[19]),
            str(row[20]),
            str(row[21]),
            None if row[22] is None else str(row[22]),
            None if row[23] is None else str(row[23]),
        ),
        attested_to=_optional_instant(
            None if row[24] is None else int(row[24]),
            None if row[25] is None else str(row[25]),
            None if row[26] is None else str(row[26]),
            None if row[27] is None else str(row[27]),
            None if row[28] is None else str(row[28]),
        ),
        recorded_at=_from_us(int(row[29]), str(row[30]), str(row[31])),
        recorded_until=_optional_instant(
            None if row[32] is None else int(row[32]),
            None if row[33] is None else str(row[33]),
            None if row[34] is None else str(row[34]),
        ),
    )
    evidence_rows = connection.execute(
        "SELECT evidence_id,span_id,support_role,confidence_ppm "
        "FROM omnivia_semantic_assertion_evidence "
        "WHERE workspace_id=? AND assertion_id=? "
        "ORDER BY evidence_id,COALESCE(span_id,''),support_role",
        (workspace_id, assertion_id),
    ).fetchall()
    evidence = tuple(
        AssertionEvidence(
            workspace_id=workspace_id,
            assertion_id=assertion_id,
            evidence_id=str(value[0]),
            span_id=None if value[1] is None else str(value[1]),
            role=EvidenceSupportRole(str(value[2])),
            confidence=_confidence_from_ppm(int(value[3])),
        )
        for value in evidence_rows
    )
    return AssertionRecord(assertion=assertion, evidence=evidence)


def read_assertion_supersessions(
    connection: sqlite3.Connection, workspace_id: str
) -> tuple[AssertionSupersession, ...]:
    rows = connection.execute(
        "SELECT supersession_id,prior_assertion_id,successor_assertion_id,reason_code,"
        "decision_id,recorded_at_us,recorded_at_precision,recorded_at_provenance "
        "FROM omnivia_semantic_assertion_supersessions WHERE workspace_id=? "
        "ORDER BY recorded_at_us,supersession_id",
        (workspace_id,),
    ).fetchall()
    return tuple(
        AssertionSupersession(
            workspace_id=workspace_id,
            supersession_id=str(row[0]),
            prior_assertion_id=str(row[1]),
            successor_assertion_id=str(row[2]),
            reason_code=str(row[3]),
            decision_id=str(row[4]),
            recorded_at=_from_us(int(row[5]), str(row[6]), str(row[7])),
        )
        for row in rows
    )


def read_assertion_retractions(
    connection: sqlite3.Connection, workspace_id: str
) -> tuple[AssertionRetraction, ...]:
    rows = connection.execute(
        "SELECT retraction_id,assertion_id,retracted_at_us,retracted_at_precision,"
        "retracted_at_provenance,reason_code,policy_version,actor_principal_id "
        "FROM omnivia_semantic_assertion_retractions WHERE workspace_id=? "
        "ORDER BY retracted_at_us,retraction_id",
        (workspace_id,),
    ).fetchall()
    return tuple(
        AssertionRetraction(
            workspace_id=workspace_id,
            retraction_id=str(row[0]),
            assertion_id=str(row[1]),
            retracted_at=_from_us(int(row[2]), str(row[3]), str(row[4])),
            reason_code=str(row[5]),
            policy_version=str(row[6]),
            actor_principal_id=str(row[7]),
        )
        for row in rows
    )


def query_assertions(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    recorded_at: TemporalInstant,
    valid_at: TemporalInstant,
) -> tuple[AssertionRecord, ...]:
    """Resolve both temporal axes and return records valid on half-open intervals.

    Supersession and retraction facts close the recorded interval at query time;
    the predecessor row remains immutable.
    """
    superseded_at = {
        value.prior_assertion_id: value.recorded_at
        for value in read_assertion_supersessions(connection, workspace_id)
    }
    retracted_at = {
        value.assertion_id: value.retracted_at
        for value in read_assertion_retractions(connection, workspace_id)
    }
    ids = connection.execute(
        "SELECT assertion_id FROM omnivia_semantic_assertions "
        "WHERE workspace_id=? ORDER BY assertion_id",
        (workspace_id,),
    ).fetchall()
    results: list[AssertionRecord] = []
    for row in ids:
        record = read_assertion(connection, workspace_id, str(row[0]))
        if record is None:
            continue
        assertion = record.assertion
        recorded_ends = [
            value
            for value in (
                assertion.recorded_until,
                superseded_at.get(assertion.assertion_id),
                retracted_at.get(assertion.assertion_id),
            )
            if value is not None
        ]
        recorded_end = min(recorded_ends, key=lambda value: value.value, default=None)
        if assertion.recorded_at.value > recorded_at.value:
            continue
        if recorded_end is not None and recorded_at.value >= recorded_end.value:
            continue
        valid_interval = assertion_effective_interval(assertion)
        if valid_at.value < valid_interval.effective_from.value:
            continue
        if (
            valid_interval.effective_to is not None
            and valid_at.value >= valid_interval.effective_to.value
        ):
            continue
        results.append(record)
    return tuple(results)


def read_candidate(
    connection: sqlite3.Connection, workspace_id: str, candidate_id: str
) -> CandidateRecord | None:
    row = connection.execute(
        "SELECT candidate_kind,target_model_id,proposed_operation_json,support_band,"
        "novelty_band,risk_band,candidate_state,aggregation_version,"
        "normalization_version,base_version_id,evidence_snapshot_digest,"
        "created_at_us,created_at_precision,created_at_provenance,rejection_signature "
        "FROM omnivia_semantic_candidates WHERE workspace_id=? AND candidate_id=?",
        (workspace_id, candidate_id),
    ).fetchone()
    if row is None:
        return None
    candidate = SemanticCandidate(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        candidate_kind=str(row[0]),
        target_model_id=str(row[1]),
        proposed_operation=_operation_from_text(str(row[2])),
        support_band=CandidateBand(str(row[3])),
        novelty_band=CandidateBand(str(row[4])),
        risk_band=CandidateRiskBand(str(row[5])),
        state=CandidateState(str(row[6])),
        aggregation_version=str(row[7]),
        normalization_version=str(row[8]),
        base_version_id=str(row[9]),
        evidence_snapshot_digest=str(row[10]),
        created_at=_from_us(int(row[11]), str(row[12]), str(row[13])),
        rejection_signature=None if row[14] is None else str(row[14]),
    )
    contribution_rows = connection.execute(
        "SELECT observation_id,contribution_role,weight,observation_digest "
        "FROM omnivia_semantic_candidate_contributions "
        "WHERE workspace_id=? AND candidate_id=? ORDER BY observation_id",
        (workspace_id, candidate_id),
    ).fetchall()
    contributions = tuple(
        CandidateContribution(
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            observation_id=str(value[0]),
            role=ContributionRole(str(value[1])),
            weight=int(value[2]),
            observation_digest=str(value[3]),
        )
        for value in contribution_rows
    )
    return CandidateRecord(candidate=candidate, contributions=contributions)


def read_suppressions(
    connection: sqlite3.Connection, workspace_id: str, equivalence_signature: str
) -> tuple[CandidateSuppression, ...]:
    rows = connection.execute(
        "SELECT suppression_id,rejection_ref,suppression_rule_version,created_at_us,"
        "created_at_precision,created_at_provenance,evidence_snapshot_digest,"
        "aggregation_version,expires_at_us,expires_at_precision,expires_at_provenance "
        "FROM omnivia_semantic_candidate_suppressions "
        "WHERE workspace_id=? AND equivalence_signature=? "
        "ORDER BY created_at_us,suppression_id",
        (workspace_id, equivalence_signature),
    ).fetchall()
    return tuple(
        CandidateSuppression(
            workspace_id=workspace_id,
            suppression_id=str(row[0]),
            equivalence_signature=equivalence_signature,
            rejection_ref=str(row[1]),
            suppression_rule_version=str(row[2]),
            created_at=_from_us(int(row[3]), str(row[4]), str(row[5])),
            evidence_snapshot_digest=str(row[6]),
            aggregation_version=str(row[7]),
            expires_at=_optional_instant(
                None if row[8] is None else int(row[8]),
                None if row[9] is None else str(row[9]),
                None if row[10] is None else str(row[10]),
            ),
        )
        for row in rows
    )


def active_suppression(
    connection: sqlite3.Connection,
    workspace_id: str,
    equivalence_signature: str,
    *,
    at: TemporalInstant,
    evidence_snapshot_digest: str,
    aggregation_version: str,
) -> CandidateSuppression | None:
    for suppression in reversed(
        read_suppressions(connection, workspace_id, equivalence_signature)
    ):
        if suppression_active(
            suppression, at, evidence_snapshot_digest, aggregation_version
        ).active:
            return suppression
    return None


class SemanticGovernanceWriter:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self._connection = connection
        self._workspace_id = workspace_id

    def append_outbox(
        self,
        *,
        outbox_id: str,
        aggregate_id: str,
        event_kind: str,
        payload: Mapping[str, object],
        now_us: int,
    ) -> None:
        """Append an IDs-only event inside this writer's current transaction."""
        SemanticRegistryWriter(self._connection, self._workspace_id).append_outbox(
            outbox_id=outbox_id,
            aggregate_id=aggregate_id,
            event_kind=event_kind,
            payload=payload,
            now_us=now_us,
        )

    def _workspace(self, value: str, record: str) -> None:
        if value != self._workspace_id:
            raise StorageError(f"{record} workspace does not match writer workspace")

    def append_assertion(
        self, assertion: KnowledgeAssertion, evidence: Sequence[AssertionEvidence]
    ) -> None:
        self._workspace(assertion.workspace_id, "assertion")
        valid_from = assertion.valid_from
        valid_to = assertion.valid_to
        attested_to = assertion.attested_to
        recorded_until = assertion.recorded_until
        self._connection.execute(
            "INSERT INTO omnivia_semantic_assertions "
            "(workspace_id,assertion_id,subject_id,predicate_element_id,model_version_id,"
            "object_kind,object_id,literal_json,confidence_ppm,classification,valid_from_us,"
            "valid_from_precision,valid_from_provenance,valid_from_original_text,"
            "valid_from_timezone,valid_to_state,valid_to_us,valid_to_precision,"
            "valid_to_provenance,valid_to_original_text,valid_to_timezone,"
            "attested_from_us,attested_from_precision,attested_from_provenance,"
            "attested_from_original_text,attested_from_timezone,attested_to_us,"
            "attested_to_precision,attested_to_provenance,attested_to_original_text,"
            "attested_to_timezone,recorded_at_us,"
            "recorded_at_precision,recorded_at_provenance,recorded_until_us,"
            "recorded_until_precision,recorded_until_provenance,schema_version,"
            "assertion_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                assertion.assertion_id,
                assertion.subject_id,
                assertion.predicate_element_id,
                assertion.model_version_id,
                assertion.object_kind.value,
                assertion.object_id,
                None
                if assertion.literal_value is None
                else canonical_text(assertion.literal_value),
                _confidence_to_ppm(assertion.confidence),
                assertion.classification.value,
                None if valid_from is None else _to_us(valid_from),
                None if valid_from is None else valid_from.precision.value,
                None if valid_from is None else valid_from.provenance.value,
                None if valid_from is None else valid_from.original_source_text,
                None if valid_from is None else valid_from.source_timezone,
                assertion.valid_to_state.value,
                None if valid_to is None else _to_us(valid_to),
                None if valid_to is None else valid_to.precision.value,
                None if valid_to is None else valid_to.provenance.value,
                None if valid_to is None else valid_to.original_source_text,
                None if valid_to is None else valid_to.source_timezone,
                _to_us(assertion.attested_from),
                assertion.attested_from.precision.value,
                assertion.attested_from.provenance.value,
                assertion.attested_from.original_source_text,
                assertion.attested_from.source_timezone,
                None if attested_to is None else _to_us(attested_to),
                None if attested_to is None else attested_to.precision.value,
                None if attested_to is None else attested_to.provenance.value,
                None if attested_to is None else attested_to.original_source_text,
                None if attested_to is None else attested_to.source_timezone,
                _to_us(assertion.recorded_at),
                assertion.recorded_at.precision.value,
                assertion.recorded_at.provenance.value,
                None if recorded_until is None else _to_us(recorded_until),
                None if recorded_until is None else recorded_until.precision.value,
                None if recorded_until is None else recorded_until.provenance.value,
                assertion.schema_version,
                assertion_digest(assertion),
            ),
        )
        for link in sorted(
            evidence, key=lambda value: (value.evidence_id, value.role.value)
        ):
            self._workspace(link.workspace_id, "assertion evidence")
            if link.assertion_id != assertion.assertion_id:
                raise StorageError("assertion evidence references another assertion")
            self._connection.execute(
                "INSERT INTO omnivia_semantic_assertion_evidence "
                "(workspace_id,assertion_id,evidence_id,span_id,support_role,"
                "confidence_ppm,evidence_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    self._workspace_id,
                    assertion.assertion_id,
                    link.evidence_id,
                    link.span_id,
                    link.role.value,
                    _confidence_to_ppm(link.confidence),
                    assertion_evidence_digest(link),
                ),
            )

    def append_supersession(self, value: AssertionSupersession) -> None:
        self._workspace(value.workspace_id, "assertion supersession")
        self._connection.execute(
            "INSERT INTO omnivia_semantic_assertion_supersessions "
            "(workspace_id,supersession_id,prior_assertion_id,successor_assertion_id,"
            "reason_code,decision_id,recorded_at_us,recorded_at_precision,"
            "recorded_at_provenance,supersession_digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                value.supersession_id,
                value.prior_assertion_id,
                value.successor_assertion_id,
                value.reason_code,
                value.decision_id,
                _to_us(value.recorded_at),
                value.recorded_at.precision.value,
                value.recorded_at.provenance.value,
                assertion_supersession_digest(value),
            ),
        )

    def append_retraction(self, value: AssertionRetraction) -> None:
        self._workspace(value.workspace_id, "assertion retraction")
        self._connection.execute(
            "INSERT INTO omnivia_semantic_assertion_retractions "
            "(workspace_id,retraction_id,assertion_id,retracted_at_us,"
            "retracted_at_precision,retracted_at_provenance,reason_code,policy_version,"
            "actor_principal_id,retraction_digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                value.retraction_id,
                value.assertion_id,
                _to_us(value.retracted_at),
                value.retracted_at.precision.value,
                value.retracted_at.provenance.value,
                value.reason_code,
                value.policy_version,
                value.actor_principal_id,
                assertion_retraction_digest(value),
            ),
        )

    def append_candidate(
        self,
        candidate: SemanticCandidate,
        contributions: Sequence[CandidateContribution],
    ) -> None:
        self._workspace(candidate.workspace_id, "candidate")
        pointer = self._connection.execute(
            "SELECT current_version_id FROM omnivia_semantic_current_pointers "
            "WHERE workspace_id=? AND model_id=?",
            (self._workspace_id, candidate.target_model_id),
        ).fetchone()
        if pointer is None or pointer[0] != candidate.base_version_id:
            raise StorageError("candidate base version is stale")
        signature = candidate_equivalence_signature(
            self._workspace_id,
            candidate.candidate_kind,
            candidate.target_model_id,
            candidate.proposed_operation,
            candidate.normalization_version,
            candidate.aggregation_version,
        )
        self._connection.execute(
            "INSERT INTO omnivia_semantic_candidates "
            "(workspace_id,candidate_id,candidate_kind,target_model_id,"
            "proposed_operation_json,support_band,novelty_band,risk_band,candidate_state,"
            "aggregation_version,normalization_version,base_version_id,"
            "evidence_snapshot_digest,equivalence_signature,rejection_signature,"
            "schema_version,candidate_digest,created_at_us,created_at_precision,"
            "created_at_provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                candidate.candidate_id,
                candidate.candidate_kind,
                candidate.target_model_id,
                canonical_text(operation_payload(candidate.proposed_operation)),
                candidate.support_band.value,
                candidate.novelty_band.value,
                candidate.risk_band.value,
                candidate.state.value,
                candidate.aggregation_version,
                candidate.normalization_version,
                candidate.base_version_id,
                candidate.evidence_snapshot_digest,
                signature,
                candidate.rejection_signature,
                candidate.schema_version,
                candidate_digest(candidate),
                _to_us(candidate.created_at),
                candidate.created_at.precision.value,
                candidate.created_at.provenance.value,
            ),
        )
        for contribution in sorted(
            contributions, key=lambda value: value.observation_id
        ):
            self._workspace(contribution.workspace_id, "candidate contribution")
            if contribution.candidate_id != candidate.candidate_id:
                raise StorageError(
                    "candidate contribution references another candidate"
                )
            self._connection.execute(
                "INSERT INTO omnivia_semantic_candidate_contributions "
                "(workspace_id,candidate_id,observation_id,contribution_role,weight,"
                "observation_digest,contribution_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    self._workspace_id,
                    candidate.candidate_id,
                    contribution.observation_id,
                    contribution.role.value,
                    contribution.weight,
                    contribution.observation_digest,
                    candidate_contribution_digest(contribution),
                ),
            )

    def append_suppression(self, value: CandidateSuppression) -> None:
        self._workspace(value.workspace_id, "candidate suppression")
        expires = value.expires_at
        self._connection.execute(
            "INSERT INTO omnivia_semantic_candidate_suppressions "
            "(workspace_id,suppression_id,equivalence_signature,rejection_ref,"
            "suppression_rule_version,evidence_snapshot_digest,aggregation_version,"
            "created_at_us,created_at_precision,created_at_provenance,expires_at_us,"
            "expires_at_precision,expires_at_provenance,suppression_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                value.suppression_id,
                value.equivalence_signature,
                value.rejection_ref,
                value.suppression_rule_version,
                value.evidence_snapshot_digest,
                value.aggregation_version,
                _to_us(value.created_at),
                value.created_at.precision.value,
                value.created_at.provenance.value,
                None if expires is None else _to_us(expires),
                None if expires is None else expires.precision.value,
                None if expires is None else expires.provenance.value,
                suppression_digest(value),
            ),
        )

    def append_reconsideration(self, value: CandidateReconsideration) -> None:
        self._workspace(value.workspace_id, "candidate reconsideration")
        self._connection.execute(
            "INSERT INTO omnivia_semantic_candidate_reconsiderations "
            "(workspace_id,reconsideration_id,suppression_id,reason,"
            "previous_evidence_digest,new_evidence_digest,previous_rule_version,"
            "new_rule_version,actor_principal_id,recorded_at_us,recorded_at_precision,"
            "recorded_at_provenance,reconsideration_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                value.reconsideration_id,
                value.suppression_id,
                value.reason.value,
                value.previous_evidence_digest,
                value.new_evidence_digest,
                value.previous_rule_version,
                value.new_rule_version,
                value.actor_principal_id,
                _to_us(value.recorded_at),
                value.recorded_at.precision.value,
                value.recorded_at.provenance.value,
                reconsideration_digest(value),
            ),
        )


@contextmanager
def semantic_governance_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[SemanticGovernanceWriter]:
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield SemanticGovernanceWriter(connection, workspace_id)


def verify_governance_digests(
    connection: sqlite3.Connection, workspace_id: str
) -> None:
    for assertion_id, stored in connection.execute(
        "SELECT assertion_id,assertion_digest FROM omnivia_semantic_assertions "
        "WHERE workspace_id=? ORDER BY assertion_id",
        (workspace_id,),
    ).fetchall():
        assertion_record = read_assertion(connection, workspace_id, str(assertion_id))
        if (
            assertion_record is None
            or assertion_digest(assertion_record.assertion) != stored
        ):
            raise StorageError("stored assertion digest verification failed")
    for candidate_id, stored in connection.execute(
        "SELECT candidate_id,candidate_digest FROM omnivia_semantic_candidates "
        "WHERE workspace_id=? ORDER BY candidate_id",
        (workspace_id,),
    ).fetchall():
        candidate_record = read_candidate(connection, workspace_id, str(candidate_id))
        if (
            candidate_record is None
            or candidate_digest(candidate_record.candidate) != stored
        ):
            raise StorageError("stored candidate digest verification failed")
    for suppression in connection.execute(
        "SELECT equivalence_signature FROM omnivia_semantic_candidate_suppressions "
        "WHERE workspace_id=? GROUP BY equivalence_signature",
        (workspace_id,),
    ).fetchall():
        for value in read_suppressions(connection, workspace_id, str(suppression[0])):
            stored = connection.execute(
                "SELECT suppression_digest FROM omnivia_semantic_candidate_suppressions "
                "WHERE workspace_id=? AND suppression_id=?",
                (workspace_id, value.suppression_id),
            ).fetchone()
            if stored is None or suppression_digest(value) != stored[0]:
                raise StorageError("stored suppression digest verification failed")


__all__ = [
    "AssertionRecord",
    "CandidateRecord",
    "SemanticGovernanceWriter",
    "active_suppression",
    "query_assertions",
    "read_assertion",
    "read_assertion_retractions",
    "read_assertion_supersessions",
    "read_candidate",
    "read_suppressions",
    "semantic_governance_writer",
    "verify_governance_digests",
]
