"""Fenced Phase 2 evidence and observation persistence.

The migration owns relational invariants.  This module translates the public,
standard-library domain records to those relations and always composes writes
inside the existing workspace writer fence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from omnivia_core.semantic_registry import (
    Classification,
    EvidenceExtraction,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSpan,
    EvidenceSupportRole,
    ObservationBundle,
    ObservationFeature,
    ObservationGeneration,
    ObservationStatus,
    ObservationValueKind,
    SemanticObservation,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
    content_digest,
    evidence_extraction_digest,
    evidence_item_digest,
    evidence_link_digest,
    observation_digest,
    observation_feature_digest,
    observation_feature_payload,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.semantic_registry import (
    SemanticRegistryWriter,
    canonical_text,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _to_us(instant: TemporalInstant) -> int:
    delta = instant.value - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _from_us(value: int, precision: str, provenance: str) -> TemporalInstant:
    return TemporalInstant(
        value=_EPOCH + timedelta(microseconds=value),
        precision=TemporalPrecision(precision),
        provenance=TemporalProvenance(provenance),
    )


def _optional_instant(
    value: int | None, precision: str | None, provenance: str | None
) -> TemporalInstant | None:
    if value is None:
        return None
    if precision is None or provenance is None:  # schema corruption, not input error
        raise StorageError("stored temporal instant is missing precision or provenance")
    return _from_us(value, precision, provenance)


def _confidence_to_ppm(value: float) -> int:
    return round(value * 1_000_000)


def _confidence_from_ppm(value: int) -> float:
    return value / 1_000_000


def _span_digest(span: EvidenceSpan) -> str:
    return content_digest(
        {
            "span_id": span.span_id,
            "start_offset": span.start_offset,
            "end_offset": span.end_offset,
            "page": span.page,
            "section": span.section,
        }
    )


def _read_source(
    connection: sqlite3.Connection, workspace_id: str, source_id: str
) -> EvidenceSource | None:
    row = connection.execute(
        "SELECT source_id, source_kind, locator_scheme, locator, source_version "
        "FROM omnivia_semantic_evidence_sources "
        "WHERE workspace_id = ? AND source_id = ?",
        (workspace_id, source_id),
    ).fetchone()
    if row is None:
        return None
    return EvidenceSource(
        source_id=str(row[0]),
        kind=EvidenceSourceKind(str(row[1])),
        locator_scheme=EvidenceLocatorScheme(str(row[2])),
        locator=str(row[3]),
        version=str(row[4]),
    )


def read_evidence_item(
    connection: sqlite3.Connection, workspace_id: str, evidence_id: str
) -> EvidenceItem | None:
    """Read one evidence metadata record without retrieving protected bytes."""
    row = connection.execute(
        "SELECT source_id, content_ref, content_digest, integrity_digest, mime_type, "
        "classification, retention_class, captured_at_us, captured_at_precision, "
        "captured_at_provenance, source_time_us, source_time_precision, "
        "source_time_provenance "
        "FROM omnivia_semantic_evidence_items "
        "WHERE workspace_id = ? AND evidence_id = ?",
        (workspace_id, evidence_id),
    ).fetchone()
    if row is None:
        return None
    source = _read_source(connection, workspace_id, str(row[0]))
    if source is None:
        raise StorageError("stored evidence source is missing")
    span_row = connection.execute(
        "SELECT span_id, start_offset, end_offset, page_number, section_ref "
        "FROM omnivia_semantic_evidence_spans "
        "WHERE workspace_id = ? AND evidence_id = ? ORDER BY span_id LIMIT 1",
        (workspace_id, evidence_id),
    ).fetchone()
    span = None
    if span_row is not None:
        span = EvidenceSpan(
            span_id=str(span_row[0]),
            start_offset=int(span_row[1]),
            end_offset=int(span_row[2]),
            page=None if span_row[3] is None else int(span_row[3]),
            section=None if span_row[4] is None else str(span_row[4]),
        )
    return EvidenceItem(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        source=source,
        content_ref=str(row[1]),
        content_digest=str(row[2]),
        integrity_digest=str(row[3]),
        mime_type=str(row[4]),
        classification=Classification(str(row[5])),
        retention_class=str(row[6]),
        captured_at=_from_us(int(row[7]), str(row[8]), str(row[9])),
        source_time=_optional_instant(
            None if row[10] is None else int(row[10]),
            None if row[11] is None else str(row[11]),
            None if row[12] is None else str(row[12]),
        ),
        span=span,
    )


def read_evidence_by_digest(
    connection: sqlite3.Connection, workspace_id: str, digest: str
) -> EvidenceItem | None:
    row = connection.execute(
        "SELECT evidence_id FROM omnivia_semantic_evidence_items "
        "WHERE workspace_id = ? AND content_digest = ?",
        (workspace_id, digest),
    ).fetchone()
    return (
        None
        if row is None
        else read_evidence_item(connection, workspace_id, str(row[0]))
    )


def read_evidence_extraction(
    connection: sqlite3.Connection, workspace_id: str, extraction_id: str
) -> EvidenceExtraction | None:
    row = connection.execute(
        "SELECT evidence_id, worker_version, template_version, input_digest, "
        "output_digest, confidence_ppm, model_version, raw_completion_ref "
        "FROM omnivia_semantic_evidence_extractions "
        "WHERE workspace_id = ? AND extraction_id = ?",
        (workspace_id, extraction_id),
    ).fetchone()
    if row is None:
        return None
    return EvidenceExtraction(
        extraction_id=extraction_id,
        workspace_id=workspace_id,
        evidence_id=str(row[0]),
        worker_version=str(row[1]),
        template_version=str(row[2]),
        input_digest=str(row[3]),
        output_digest=str(row[4]),
        confidence=_confidence_from_ppm(int(row[5])),
        model_version=None if row[6] is None else str(row[6]),
        raw_completion_ref=None if row[7] is None else str(row[7]),
    )


def read_observation_bundle(
    connection: sqlite3.Connection, workspace_id: str, observation_id: str
) -> ObservationBundle | None:
    row = connection.execute(
        "SELECT observation_kind, value_kind, original_form_ref, normalized_form, "
        "proposed_semantic_role, classification, generation, status, source_time_us, "
        "source_time_precision, source_time_provenance, recorded_at_us, "
        "recorded_at_precision, recorded_at_provenance, "
        "supersedes_observation_id, rule_version "
        "FROM omnivia_semantic_observations "
        "WHERE workspace_id = ? AND observation_id = ?",
        (workspace_id, observation_id),
    ).fetchone()
    if row is None:
        return None
    observation = SemanticObservation(
        observation_id=observation_id,
        workspace_id=workspace_id,
        kind=str(row[0]),
        value_kind=ObservationValueKind(str(row[1])),
        original_form=str(row[2]),
        normalized_form=str(row[3]),
        proposed_semantic_role=str(row[4]),
        classification=Classification(str(row[5])),
        generation=ObservationGeneration(str(row[6])),
        status=ObservationStatus(str(row[7])),
        source_time=_optional_instant(
            None if row[8] is None else int(row[8]),
            None if row[9] is None else str(row[9]),
            None if row[10] is None else str(row[10]),
        ),
        recorded_at=_from_us(
            int(row[11]),
            str(row[12]),
            str(row[13]),
        ),
        supersedes_observation_id=None if row[14] is None else str(row[14]),
        rule_version=None if row[15] is None else str(row[15]),
    )
    link_rows = connection.execute(
        "SELECT evidence_id, span_id, support_role, confidence_ppm "
        "FROM omnivia_semantic_observation_evidence "
        "WHERE workspace_id = ? AND observation_id = ? "
        "ORDER BY evidence_id, COALESCE(span_id, ''), support_role",
        (workspace_id, observation_id),
    ).fetchall()
    links = tuple(
        EvidenceLink(
            workspace_id=workspace_id,
            observation_id=observation_id,
            evidence_id=str(link[0]),
            span_id=None if link[1] is None else str(link[1]),
            role=EvidenceSupportRole(str(link[2])),
            confidence=_confidence_from_ppm(int(link[3])),
        )
        for link in link_rows
    )
    feature_rows = connection.execute(
        "SELECT feature_name, feature_json, policy_version, calculation_version "
        "FROM omnivia_semantic_observation_features "
        "WHERE workspace_id = ? AND observation_id = ? ORDER BY feature_name",
        (workspace_id, observation_id),
    ).fetchall()
    features = tuple(
        ObservationFeature(
            workspace_id=workspace_id,
            observation_id=observation_id,
            feature_name=str(feature[0]),
            value=json.loads(str(feature[1])),
            policy_version=str(feature[2]),
            calculation_version=str(feature[3]),
        )
        for feature in feature_rows
    )
    return ObservationBundle(
        observation=observation, evidence_links=links, features=features
    )


class EvidenceObservationWriter:
    """Writes issued inside a transaction owned by ``semantic_evidence_writer``."""

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

    def register_evidence(self, item: EvidenceItem) -> EvidenceItem:
        if item.workspace_id != self._workspace_id:
            raise StorageError("evidence workspace does not match writer workspace")
        duplicate = read_evidence_by_digest(
            self._connection, self._workspace_id, item.content_digest
        )
        if duplicate is not None:
            return duplicate
        self._connection.execute(
            "INSERT OR IGNORE INTO omnivia_semantic_evidence_sources "
            "(workspace_id,source_id,source_kind,locator_scheme,locator,source_version,"
            "classification,created_at_us) VALUES (?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                item.source.source_id,
                item.source.kind.value,
                item.source.locator_scheme.value,
                item.source.locator,
                item.source.version,
                item.classification.value,
                _to_us(item.captured_at),
            ),
        )
        stored_source = _read_source(
            self._connection, self._workspace_id, item.source.source_id
        )
        if stored_source != item.source:
            raise StorageError(
                "evidence source identity conflicts with stored metadata"
            )
        source_time = item.source_time
        self._connection.execute(
            "INSERT INTO omnivia_semantic_evidence_items "
            "(workspace_id,evidence_id,source_id,content_ref,content_digest,integrity_digest,"
            "mime_type,classification,retention_class,captured_at_us,"
            "captured_at_precision,captured_at_provenance,source_time_us,"
            "source_time_precision,source_time_provenance,schema_version,record_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                item.evidence_id,
                item.source.source_id,
                item.content_ref,
                item.content_digest,
                item.integrity_digest,
                item.mime_type,
                item.classification.value,
                item.retention_class,
                _to_us(item.captured_at),
                item.captured_at.precision.value,
                item.captured_at.provenance.value,
                None if source_time is None else _to_us(source_time),
                None if source_time is None else source_time.precision.value,
                None if source_time is None else source_time.provenance.value,
                item.schema_version,
                evidence_item_digest(item),
            ),
        )
        if item.span is not None:
            self._connection.execute(
                "INSERT INTO omnivia_semantic_evidence_spans "
                "(workspace_id,evidence_id,span_id,start_offset,end_offset,page_number,"
                "section_ref,span_digest) VALUES (?,?,?,?,?,?,?,?)",
                (
                    self._workspace_id,
                    item.evidence_id,
                    item.span.span_id,
                    item.span.start_offset,
                    item.span.end_offset,
                    item.span.page,
                    item.span.section,
                    _span_digest(item.span),
                ),
            )
        return item

    def append_extraction(
        self, extraction: EvidenceExtraction, *, created_at_us: int
    ) -> None:
        if extraction.workspace_id != self._workspace_id:
            raise StorageError("extraction workspace does not match writer workspace")
        self._connection.execute(
            "INSERT INTO omnivia_semantic_evidence_extractions "
            "(workspace_id,extraction_id,evidence_id,worker_version,model_version,"
            "template_version,input_digest,output_digest,raw_completion_ref,"
            "confidence_ppm,schema_version,created_at_us,extraction_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                extraction.extraction_id,
                extraction.evidence_id,
                extraction.worker_version,
                extraction.model_version,
                extraction.template_version,
                extraction.input_digest,
                extraction.output_digest,
                extraction.raw_completion_ref,
                _confidence_to_ppm(extraction.confidence),
                extraction.schema_version,
                created_at_us,
                evidence_extraction_digest(extraction),
            ),
        )

    def append_observation(self, bundle: ObservationBundle) -> None:
        observation = bundle.observation
        if observation.workspace_id != self._workspace_id:
            raise StorageError("observation workspace does not match writer workspace")
        source_time = observation.source_time
        self._connection.execute(
            "INSERT INTO omnivia_semantic_observations "
            "(workspace_id,observation_id,observation_kind,value_kind,original_form_ref,"
            "normalized_form,proposed_semantic_role,classification,generation,status,"
            "source_time_us,source_time_precision,source_time_provenance,recorded_at_us,"
            "recorded_at_precision,recorded_at_provenance,supersedes_observation_id,"
            "rule_version,normalization_version,schema_version,observation_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._workspace_id,
                observation.observation_id,
                observation.kind,
                observation.value_kind.value,
                observation.original_form,
                observation.normalized_form,
                observation.proposed_semantic_role,
                observation.classification.value,
                observation.generation.value,
                observation.status.value,
                None if source_time is None else _to_us(source_time),
                None if source_time is None else source_time.precision.value,
                None if source_time is None else source_time.provenance.value,
                _to_us(observation.recorded_at),
                observation.recorded_at.precision.value,
                observation.recorded_at.provenance.value,
                observation.supersedes_observation_id,
                observation.rule_version,
                "observation-normalization-v1",
                observation.schema_version,
                observation_digest(observation),
            ),
        )
        for link in sorted(
            bundle.evidence_links,
            key=lambda value: (
                value.evidence_id,
                value.span_id or "",
                value.role.value,
            ),
        ):
            self._connection.execute(
                "INSERT INTO omnivia_semantic_observation_evidence "
                "(workspace_id,observation_id,evidence_id,span_id,support_role,"
                "confidence_ppm,link_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    self._workspace_id,
                    observation.observation_id,
                    link.evidence_id,
                    link.span_id,
                    link.role.value,
                    _confidence_to_ppm(link.confidence),
                    evidence_link_digest(link),
                ),
            )
        for feature in sorted(bundle.features, key=lambda value: value.feature_name):
            self._connection.execute(
                "INSERT INTO omnivia_semantic_observation_features "
                "(workspace_id,observation_id,feature_name,feature_json,policy_version,"
                "calculation_version,feature_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    self._workspace_id,
                    observation.observation_id,
                    feature.feature_name,
                    canonical_text(observation_feature_payload(feature)["value"]),
                    feature.policy_version,
                    feature.calculation_version,
                    observation_feature_digest(feature),
                ),
            )


@contextmanager
def semantic_evidence_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[EvidenceObservationWriter]:
    """Open one authoritative transaction and lend its fenced writes."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield EvidenceObservationWriter(connection, workspace_id)


def verify_evidence_observation_digests(
    connection: sqlite3.Connection, workspace_id: str
) -> None:
    """Reconstruct every evidence/observation record and verify stored digests."""
    evidence_rows: Sequence[tuple[str, str]] = connection.execute(
        "SELECT evidence_id, record_digest FROM omnivia_semantic_evidence_items "
        "WHERE workspace_id = ? ORDER BY evidence_id",
        (workspace_id,),
    ).fetchall()
    for evidence_id, stored_digest in evidence_rows:
        item = read_evidence_item(connection, workspace_id, evidence_id)
        if item is None or evidence_item_digest(item) != stored_digest:
            raise StorageError("stored evidence digest verification failed")
    extraction_rows: Sequence[tuple[str, str]] = connection.execute(
        "SELECT extraction_id, extraction_digest "
        "FROM omnivia_semantic_evidence_extractions "
        "WHERE workspace_id = ? ORDER BY extraction_id",
        (workspace_id,),
    ).fetchall()
    for extraction_id, stored_digest in extraction_rows:
        extraction = read_evidence_extraction(connection, workspace_id, extraction_id)
        if (
            extraction is None
            or evidence_extraction_digest(extraction) != stored_digest
        ):
            raise StorageError("stored evidence-extraction digest verification failed")
    observation_rows: Sequence[tuple[str, str]] = connection.execute(
        "SELECT observation_id, observation_digest FROM omnivia_semantic_observations "
        "WHERE workspace_id = ? ORDER BY observation_id",
        (workspace_id,),
    ).fetchall()
    for observation_id, stored_digest in observation_rows:
        bundle = read_observation_bundle(connection, workspace_id, observation_id)
        if bundle is None or observation_digest(bundle.observation) != stored_digest:
            raise StorageError("stored observation digest verification failed")
        link_digests = dict(
            connection.execute(
                "SELECT evidence_id || ':' || COALESCE(span_id,'') || ':' || support_role, "
                "link_digest FROM omnivia_semantic_observation_evidence "
                "WHERE workspace_id = ? AND observation_id = ?",
                (workspace_id, observation_id),
            ).fetchall()
        )
        for link in bundle.evidence_links:
            key = f"{link.evidence_id}:{link.span_id or ''}:{link.role.value}"
            if link_digests.get(key) != evidence_link_digest(link):
                raise StorageError("stored evidence-link digest verification failed")
        feature_digests = dict(
            connection.execute(
                "SELECT feature_name, feature_digest "
                "FROM omnivia_semantic_observation_features "
                "WHERE workspace_id = ? AND observation_id = ?",
                (workspace_id, observation_id),
            ).fetchall()
        )
        for feature in bundle.features:
            if feature_digests.get(feature.feature_name) != observation_feature_digest(
                feature
            ):
                raise StorageError(
                    "stored observation-feature digest verification failed"
                )


__all__ = [
    "EvidenceObservationWriter",
    "read_evidence_by_digest",
    "read_evidence_extraction",
    "read_evidence_item",
    "read_observation_bundle",
    "semantic_evidence_writer",
    "verify_evidence_observation_digests",
]
