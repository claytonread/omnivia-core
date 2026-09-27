"""Durable, authorization-safe inputs and results for relation assessment.

This module owns the SQLite boundary only.  It stages one bounded pair after the
deterministic discovery observation has committed, then lets the service call an
optional provider with an immutable value after the transaction is closed.  A
second short fenced transaction appends the terminal reconciliation.  Neither
table changes the relation candidate's pending state or writes governance data.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_conflicts import (
    RelationCandidate,
    RelationEndpoint,
    read_authorized_relation_candidates_for_anchor,
)
from omnivia_core_runtime.storage.engineering_preview import (
    PreviewCandidate,
    read_authorized_previews,
)
from omnivia_core_runtime.storage.memory import read_snapshot
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

IdentifierAllocator = Callable[[str], str]

REQUEST_SCHEMA_VERSION: Final = "engineering.relation-assessment.request.v1"
RESPONSE_SCHEMA_VERSION: Final = "engineering.relation-assessment.response.v1"
TOKENIZER_ID: Final = "engineering.assessment.alnum-run-or-char.v1"
ALLOWED_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "related",
        "compatible",
        "scoped_difference",
        "conflicts_with",
        "supersedes",
        "not_conflict",
    }
)
_DISCOVERY_VIEWS: Final[tuple[str | None, ...]] = ("candidates", None, "history")
_CANDIDATE_STAGE_PAGE: Final = 128
_TOKEN: Final = re.compile(r"[^\W_]+|[^\s\w]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class AssessmentEndpointInput:
    """One exact, currently authorized endpoint sent as bounded data."""

    assembly_id: str
    record_id: str
    version: str
    content_digest: str
    title: str
    preview: str
    truncated: bool
    observation_kind: str | None
    assertion_basis: str | None
    topic_key: str | None
    repository_id: str | None
    snapshot_id: str | None
    evidence_refs: tuple[str, ...]

    def to_document(self, *, role: str) -> dict[str, object]:
        return {
            "role": role,
            "assembly_id": self.assembly_id,
            "record_id": self.record_id,
            "version": self.version,
            "content_digest": self.content_digest,
            "title": self.title,
            "preview": self.preview,
            "truncated": self.truncated,
            "observation_kind": self.observation_kind,
            "assertion_basis": self.assertion_basis,
            "topic_key": self.topic_key,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class RelationAssessmentInput:
    """The complete immutable value a provider may receive.

    It intentionally carries no connection, principal, grant, writer identity,
    workspace id, capability, or governance service.
    """

    assessment_request_id: str
    relation_candidate_id: str
    detector_version: str
    scope_classification: str
    proposed_relation: str
    prompt_version: str
    response_schema_version: str
    endpoint_a: AssessmentEndpointInput
    endpoint_b: AssessmentEndpointInput

    @property
    def allowed_evidence_refs(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.endpoint_a.evidence_refs) | set(self.endpoint_b.evidence_refs))
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "assessment_request_id": self.assessment_request_id,
            "relation_candidate_id": self.relation_candidate_id,
            "detector_version": self.detector_version,
            "scope_classification": self.scope_classification,
            "proposed_relation": self.proposed_relation,
            "prompt_version": self.prompt_version,
            "response_schema_version": self.response_schema_version,
            "allowed_relations": sorted(ALLOWED_RELATIONS),
            "confidence_semantics": "self_reported",
            "retrieved_content_treatment": "data",
            "endpoint_a": self.endpoint_a.to_document(role="a"),
            "endpoint_b": self.endpoint_b.to_document(role="b"),
        }


@dataclass(frozen=True, slots=True)
class StagedRelationAssessment:
    assessment_request_id: str
    relation_candidate_id: str
    configuration_digest: str
    provider_id: str
    model_id: str
    prompt_version: str
    request_schema_version: str
    response_schema_version: str
    input_digest: str
    input_byte_count: int
    input_token_count: int
    tokenizer_id: str
    timeout_ms: int
    maximum_calls: int
    maximum_concurrency: int
    requested_at_us: int


@dataclass(frozen=True, slots=True)
class RelationAssessmentReconciliation:
    assessment_request_id: str
    relation_candidate_id: str
    result_id: str | None
    status: str
    assessed_relation: str | None
    evidence_refs: tuple[str, ...]
    self_reported_confidence_ppm: int | None
    response_digest: str | None
    failure_code: str | None
    reconciled_at_us: int | None


def canonical_input(value: RelationAssessmentInput) -> str:
    return to_canonical_json(value.to_document())


def content_digest(document: str) -> str:
    return f"sha256:{hashlib.sha256(document.encode('utf-8')).hexdigest()}"


def input_token_count(document: str) -> int:
    """Count the published assessment-tokenizer units in exact input bytes."""

    return len(_TOKEN.findall(document))


def _candidate(row: sqlite3.Row | tuple[object, ...]) -> RelationCandidate:
    return RelationCandidate(
        relation_candidate_id=str(row[0]),
        endpoint_a=RelationEndpoint(*(str(value) for value in row[1:5])),
        endpoint_b=RelationEndpoint(*(str(value) for value in row[5:9])),
        detector_version=str(row[9]),
        scope_classification=str(row[10]),
        proposed_relation=str(row[11]),
        status=str(row[12]),
        first_discovery_run_id=str(row[13]),
        recorded_at_us=int(str(row[14])),
    )


_CANDIDATE_COLUMNS: Final = (
    "c.relation_candidate_id, c.endpoint_a_assembly_id, c.endpoint_a_record_id, "
    "c.endpoint_a_version, c.endpoint_a_digest, c.endpoint_b_assembly_id, "
    "c.endpoint_b_record_id, c.endpoint_b_version, c.endpoint_b_digest, "
    "c.detector_version, c.scope_classification, c.proposed_relation, c.status, "
    "c.first_discovery_run_id, c.recorded_at_us"
)


def _request(row: sqlite3.Row | tuple[object, ...]) -> StagedRelationAssessment:
    return StagedRelationAssessment(
        assessment_request_id=str(row[0]),
        relation_candidate_id=str(row[1]),
        configuration_digest=str(row[2]),
        provider_id=str(row[3]),
        model_id=str(row[4]),
        prompt_version=str(row[5]),
        request_schema_version=str(row[6]),
        response_schema_version=str(row[7]),
        input_digest=str(row[8]),
        input_byte_count=int(str(row[9])),
        input_token_count=int(str(row[10])),
        tokenizer_id=str(row[11]),
        timeout_ms=int(str(row[12])),
        maximum_calls=int(str(row[13])),
        maximum_concurrency=int(str(row[14])),
        requested_at_us=int(str(row[15])),
    )


_REQUEST_COLUMNS: Final = (
    "assessment_request_id, relation_candidate_id, configuration_digest, provider_id, "
    "model_id, prompt_version, request_schema_version, response_schema_version, "
    "input_digest, input_byte_count, input_token_count, tokenizer_id, timeout_ms, "
    "maximum_calls, maximum_concurrency, requested_at_us"
)


def read_oldest_pending_assessment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    configuration_digest: str,
) -> StagedRelationAssessment | None:
    row = connection.execute(
        f"SELECT {', '.join('request.' + item.strip() for item in _REQUEST_COLUMNS.split(','))} "
        "FROM omnivia_engineering_relation_assessment_requests request "
        "LEFT JOIN omnivia_engineering_relation_assessment_results result "
        "ON result.workspace_id = request.workspace_id "
        "AND result.assessment_request_id = request.assessment_request_id "
        "WHERE request.workspace_id = ? AND request.configuration_digest = ? "
        "AND result.assessment_request_id IS NULL "
        "ORDER BY request.requested_at_us, request.assessment_request_id LIMIT 1",
        (workspace_id, configuration_digest),
    ).fetchone()
    return None if row is None else _request(row)


def _candidate_rows_without_configuration(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    configuration_digest: str,
) -> tuple[RelationCandidate, ...]:
    rows = connection.execute(
        f"SELECT {_CANDIDATE_COLUMNS} "
        "FROM omnivia_engineering_relation_candidates c "
        "LEFT JOIN omnivia_engineering_relation_assessment_requests request "
        "ON request.workspace_id = c.workspace_id "
        "AND request.relation_candidate_id = c.relation_candidate_id "
        "AND request.configuration_digest = ? "
        "WHERE c.workspace_id = ? AND c.status = 'pending' "
        "AND request.assessment_request_id IS NULL "
        "ORDER BY c.recorded_at_us, c.relation_candidate_id LIMIT ?",
        (configuration_digest, workspace_id, _CANDIDATE_STAGE_PAGE),
    ).fetchall()
    return tuple(_candidate(row) for row in rows)


def _visible_candidate(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    candidate: RelationCandidate,
    resolution_instant_us: int,
    label_grant: EvidenceLabelGrant,
) -> RelationCandidate | None:
    visible = read_authorized_relation_candidates_for_anchor(
        connection,
        workspace_id=workspace_id,
        anchor_record_id=candidate.endpoint_a.record_id,
        anchor_version=candidate.endpoint_a.version,
        resolution_instant_us=resolution_instant_us,
        label_grant=label_grant,
    )
    for item in visible:
        if item.relation_candidate_id == candidate.relation_candidate_id:
            return item
    return None


def _endpoint_input(
    endpoint: RelationEndpoint,
    preview: PreviewCandidate,
    evidence_refs: tuple[str, ...],
) -> AssessmentEndpointInput:
    if (
        preview.assembly_id != endpoint.assembly_id
        or preview.record_id != endpoint.record_id
        or preview.version != endpoint.version
        or preview.content_digest != endpoint.content_digest
    ):
        raise StorageError("an assessment preview does not match its exact endpoint")
    return AssessmentEndpointInput(
        assembly_id=endpoint.assembly_id,
        record_id=endpoint.record_id,
        version=endpoint.version,
        content_digest=endpoint.content_digest,
        title=preview.title,
        preview=preview.preview,
        truncated=preview.truncated,
        observation_kind=preview.observation_kind,
        assertion_basis=preview.assertion_basis,
        topic_key=preview.topic_key,
        repository_id=preview.repository_id,
        snapshot_id=preview.snapshot_id,
        evidence_refs=evidence_refs,
    )


def build_authorized_assessment_input(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    assessment_request_id: str,
    relation_candidate_id: str,
    prompt_version: str,
    response_schema_version: str,
    resolution_instant_us: int,
    label_grant: EvidenceLabelGrant,
) -> RelationAssessmentInput | None:
    """Rebuild one bounded input only while both exact endpoints remain visible."""

    with read_snapshot(connection):
        row = connection.execute(
            f"SELECT {_CANDIDATE_COLUMNS} "
            "FROM omnivia_engineering_relation_candidates c "
            "WHERE c.workspace_id = ? AND c.relation_candidate_id = ? "
            "AND c.status = 'pending'",
            (workspace_id, relation_candidate_id),
        ).fetchone()
        if row is None:
            return None
        candidate = _candidate(row)
        visible = _visible_candidate(
            connection,
            workspace_id=workspace_id,
            candidate=candidate,
            resolution_instant_us=resolution_instant_us,
            label_grant=label_grant,
        )
        if visible is None:
            return None

        previews: dict[str, PreviewCandidate] = {}
        record_ids = (
            visible.endpoint_a.record_id,
            visible.endpoint_b.record_id,
        )
        for view in _DISCOVERY_VIEWS:
            for preview in read_authorized_previews(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                record_ids=record_ids,
            ):
                if preview.assembly_id in {
                    visible.endpoint_a.assembly_id,
                    visible.endpoint_b.assembly_id,
                }:
                    prior = previews.setdefault(preview.assembly_id, preview)
                    if prior != preview:
                        raise StorageError(
                            "an exact assessment preview changed within one read snapshot"
                        )
        if set(previews) != {
            visible.endpoint_a.assembly_id,
            visible.endpoint_b.assembly_id,
        }:
            return None

        evidence_by_assembly: dict[str, list[str]] = {
            visible.endpoint_a.assembly_id: [],
            visible.endpoint_b.assembly_id: [],
        }
        for assembly_id, evidence_id in connection.execute(
            "SELECT assembly_id, evidence_id "
            "FROM omnivia_governed_version_evidence_links "
            "WHERE workspace_id = ? AND assembly_id IN (?, ?) "
            "ORDER BY assembly_id, link_ordinal, evidence_id",
            (
                workspace_id,
                visible.endpoint_a.assembly_id,
                visible.endpoint_b.assembly_id,
            ),
        ):
            evidence_by_assembly[str(assembly_id)].append(str(evidence_id))

        return RelationAssessmentInput(
            assessment_request_id=assessment_request_id,
            relation_candidate_id=visible.relation_candidate_id,
            detector_version=visible.detector_version,
            scope_classification=visible.scope_classification,
            proposed_relation=visible.proposed_relation,
            prompt_version=prompt_version,
            response_schema_version=response_schema_version,
            endpoint_a=_endpoint_input(
                visible.endpoint_a,
                previews[visible.endpoint_a.assembly_id],
                tuple(evidence_by_assembly[visible.endpoint_a.assembly_id]),
            ),
            endpoint_b=_endpoint_input(
                visible.endpoint_b,
                previews[visible.endpoint_b.assembly_id],
                tuple(evidence_by_assembly[visible.endpoint_b.assembly_id]),
            ),
        )


def stage_next_assessment(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    configuration_digest: str,
    provider_id: str,
    model_id: str,
    prompt_version: str,
    response_schema_version: str,
    timeout_ms: int,
    maximum_calls: int,
    maximum_concurrency: int,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator,
    occurred_at_us: int,
) -> StagedRelationAssessment | None:
    """Stage at most one authorized pair and return after its commit."""

    allocator = allocate_identifier
    if not callable(allocator):
        raise TypeError("allocate_identifier must be callable")
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        pending = read_oldest_pending_assessment(
            connection,
            workspace_id=workspace_id,
            configuration_digest=configuration_digest,
        )
        if pending is not None:
            return pending
        for candidate in _candidate_rows_without_configuration(
            connection,
            workspace_id=workspace_id,
            configuration_digest=configuration_digest,
        ):
            request_id = str(allocator("era"))
            input_value = build_authorized_assessment_input(
                connection,
                workspace_id=workspace_id,
                assessment_request_id=request_id,
                relation_candidate_id=candidate.relation_candidate_id,
                prompt_version=prompt_version,
                response_schema_version=response_schema_version,
                resolution_instant_us=occurred_at_us,
                label_grant=label_grant,
            )
            if input_value is None:
                continue
            document = canonical_input(input_value)
            encoded = document.encode("utf-8")
            tokens = input_token_count(document)
            connection.execute(
                "INSERT INTO omnivia_engineering_relation_assessment_requests "
                "(workspace_id, assessment_request_id, relation_candidate_id, "
                "configuration_digest, provider_id, model_id, prompt_version, "
                "request_schema_version, response_schema_version, input_digest, "
                "input_byte_count, input_token_count, tokenizer_id, timeout_ms, "
                "maximum_calls, maximum_concurrency, requested_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    request_id,
                    candidate.relation_candidate_id,
                    configuration_digest,
                    provider_id,
                    model_id,
                    prompt_version,
                    REQUEST_SCHEMA_VERSION,
                    response_schema_version,
                    content_digest(document),
                    len(encoded),
                    tokens,
                    TOKENIZER_ID,
                    timeout_ms,
                    maximum_calls,
                    maximum_concurrency,
                    occurred_at_us,
                ),
            )
            row = connection.execute(
                f"SELECT {_REQUEST_COLUMNS} "
                "FROM omnivia_engineering_relation_assessment_requests "
                "WHERE workspace_id = ? AND assessment_request_id = ?",
                (workspace_id, request_id),
            ).fetchone()
            assert row is not None
            return _request(row)
    return None


def append_assessment_result(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    request: StagedRelationAssessment,
    status: str,
    assessed_relation: str | None,
    evidence_refs: tuple[str, ...],
    self_reported_confidence_ppm: int | None,
    response_digest: str | None,
    failure_code: str | None,
    allocate_identifier: IdentifierAllocator,
    occurred_at_us: int,
) -> RelationAssessmentReconciliation:
    """Append one terminal result, idempotently, without changing governance."""

    allocator = allocate_identifier
    if not callable(allocator):
        raise TypeError("allocate_identifier must be callable")
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        existing = _read_reconciliation(
            connection,
            workspace_id=workspace_id,
            assessment_request_id=request.assessment_request_id,
        )
        if existing is not None and existing.result_id is not None:
            return existing
        evidence_json = (
            None
            if status != "assessed"
            else json.dumps(
                list(evidence_refs),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        result_id = str(allocator("erar"))
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_assessment_results "
            "(workspace_id, assessment_request_id, relation_candidate_id, result_id, "
            "status, assessed_relation, evidence_refs_json, "
            "self_reported_confidence_ppm, response_digest, failure_code, "
            "reconciled_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                request.assessment_request_id,
                request.relation_candidate_id,
                result_id,
                status,
                assessed_relation,
                evidence_json,
                self_reported_confidence_ppm,
                response_digest,
                failure_code,
                max(occurred_at_us, request.requested_at_us),
            ),
        )
        result = _read_reconciliation(
            connection,
            workspace_id=workspace_id,
            assessment_request_id=request.assessment_request_id,
        )
        assert result is not None
        return result


def _read_reconciliation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    assessment_request_id: str,
) -> RelationAssessmentReconciliation | None:
    row = connection.execute(
        "SELECT request.assessment_request_id, request.relation_candidate_id, "
        "result.result_id, result.status, result.assessed_relation, "
        "result.evidence_refs_json, result.self_reported_confidence_ppm, "
        "result.response_digest, result.failure_code, result.reconciled_at_us "
        "FROM omnivia_engineering_relation_assessment_requests request "
        "LEFT JOIN omnivia_engineering_relation_assessment_results result "
        "ON result.workspace_id = request.workspace_id "
        "AND result.assessment_request_id = request.assessment_request_id "
        "WHERE request.workspace_id = ? AND request.assessment_request_id = ?",
        (workspace_id, assessment_request_id),
    ).fetchone()
    if row is None:
        return None
    evidence_refs: tuple[str, ...] = ()
    if row[5] is not None:
        decoded = json.loads(str(row[5]))
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) for item in decoded
        ):
            raise StorageError("stored assessment evidence references are invalid")
        evidence_refs = tuple(decoded)
    return RelationAssessmentReconciliation(
        assessment_request_id=str(row[0]),
        relation_candidate_id=str(row[1]),
        result_id=None if row[2] is None else str(row[2]),
        status="pending" if row[3] is None else str(row[3]),
        assessed_relation=None if row[4] is None else str(row[4]),
        evidence_refs=evidence_refs,
        self_reported_confidence_ppm=(
            None if row[6] is None else int(str(row[6]))
        ),
        response_digest=None if row[7] is None else str(row[7]),
        failure_code=None if row[8] is None else str(row[8]),
        reconciled_at_us=None if row[9] is None else int(str(row[9])),
    )


def read_assessment_reconciliations(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    relation_candidate_id: str,
) -> tuple[RelationAssessmentReconciliation, ...]:
    rows = connection.execute(
        "SELECT assessment_request_id "
        "FROM omnivia_engineering_relation_assessment_requests "
        "WHERE workspace_id = ? AND relation_candidate_id = ? "
        "ORDER BY requested_at_us, assessment_request_id",
        (workspace_id, relation_candidate_id),
    ).fetchall()
    reconciliations: list[RelationAssessmentReconciliation] = []
    for row in rows:
        item = _read_reconciliation(
            connection,
            workspace_id=workspace_id,
            assessment_request_id=str(row[0]),
        )
        assert item is not None
        reconciliations.append(item)
    return tuple(reconciliations)


__all__ = [
    "ALLOWED_RELATIONS",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "TOKENIZER_ID",
    "AssessmentEndpointInput",
    "RelationAssessmentInput",
    "RelationAssessmentReconciliation",
    "StagedRelationAssessment",
    "append_assessment_result",
    "build_authorized_assessment_input",
    "canonical_input",
    "content_digest",
    "input_token_count",
    "read_assessment_reconciliations",
    "read_oldest_pending_assessment",
    "stage_next_assessment",
]
