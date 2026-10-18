"""Immutable execution evidence for factual engineering validation results.

``memory.create`` intentionally keeps engineering observation content open, but a
``validation_result`` that claims observed or derived facts is a narrower profile.
It must carry a typed receipt whose canonical document is already present as L0
evidence.  The verifier binds that document to all of the durable facts that make
the claim meaningful: the exact observation, a covered source frontier, a successful
command or workflow outcome, immutable output bytes, and the trusted ingestion run
that produced both evidence artifacts.

The receipt lives in observation content as::

    {
      "validation_receipt": {
        "evidence_id": "...",
        "document": {
          "schema_version": "1.0",
          "execution": {
            "kind": "command | workflow",
            "identity": "...",
            "definition_digest": "sha256:..."
          },
          "evaluated_frontier": {
            "repository_id": "...",
            "stream_id": "...",
            "sequence": 1,
            "snapshot_id": "...",
            "manifest_digest": "sha256:..."
          },
          "outcome": {
            "status": "passed | failed",
            "exit_code": 0,
            "output_digest": "sha256:...",
            "output_evidence_id": "..."
          },
          "producer": {"id": "...", "version": "...", "run_id": "..."},
          "subject_digest": "sha256:..."
        }
      }
    }

``evidence_id`` is outside the canonical receipt document because the evidence
identity is allocated only after the document bytes have been ingested.  Every
failure is collapsed by callers to one bounded, payload-free application error.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, TypeGuard

from omnivia_core.contracts.v1 import is_identifier, to_canonical_json

VALIDATION_RECEIPT_FIELD: Final = "validation_receipt"
VALIDATION_SOURCE_KIND: Final = "validation.execution"
VALIDATION_RECEIPT_MEDIA_TYPE: Final = (
    "application/vnd.omnivia.validation-receipt+json"
)

_NON_FACTUAL_BASES: Final = frozenset({"reported", "hypothesis"})
_EXECUTION_KINDS: Final = frozenset({"command", "workflow"})
_OUTCOMES: Final = frozenset({"passed", "failed"})
_RECEIPT_KEYS: Final = frozenset({"evidence_id", "document"})
_DOCUMENT_KEYS: Final = frozenset(
    {
        "schema_version",
        "execution",
        "evaluated_frontier",
        "outcome",
        "producer",
        "subject_digest",
    }
)
_EXECUTION_KEYS: Final = frozenset({"kind", "identity", "definition_digest"})
_FRONTIER_KEYS: Final = frozenset(
    {"repository_id", "stream_id", "sequence", "snapshot_id", "manifest_digest"}
)
_OUTCOME_KEYS: Final = frozenset(
    {"status", "exit_code", "output_digest", "output_evidence_id"}
)
_PRODUCER_KEYS: Final = frozenset({"id", "version", "run_id"})
_METADATA_KEYS: Final = frozenset(
    {"connector_id", "native_id", "locator", "source_version"}
)
_RECEIPT_DOCUMENT_CAP_BYTES: Final = 8192
_SHA256_LENGTH: Final = 71


class ValidationReceiptInvalid(ValueError):
    """The claimed factual validation is not backed by a verified receipt."""


@dataclass(frozen=True, slots=True)
class ValidationExecution:
    kind: str
    identity: str
    definition_digest: str


@dataclass(frozen=True, slots=True)
class EvaluatedFrontier:
    repository_id: str
    stream_id: str
    sequence: int
    snapshot_id: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    status: str
    exit_code: int
    output_digest: str
    output_evidence_id: str


@dataclass(frozen=True, slots=True)
class ValidationProducer:
    id: str
    version: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ValidationExecutionReceipt:
    """One parsed receipt plus the L0 evidence artifact that stores its bytes."""

    evidence_id: str
    execution: ValidationExecution
    evaluated_frontier: EvaluatedFrontier
    outcome: ValidationOutcome
    producer: ValidationProducer
    subject_digest: str

    def document(self) -> dict[str, object]:
        """Return the exact canonical-document value the evidence blob must hold."""
        return {
            "schema_version": "1.0",
            "execution": {
                "kind": self.execution.kind,
                "identity": self.execution.identity,
                "definition_digest": self.execution.definition_digest,
            },
            "evaluated_frontier": {
                "repository_id": self.evaluated_frontier.repository_id,
                "stream_id": self.evaluated_frontier.stream_id,
                "sequence": self.evaluated_frontier.sequence,
                "snapshot_id": self.evaluated_frontier.snapshot_id,
                "manifest_digest": self.evaluated_frontier.manifest_digest,
            },
            "outcome": {
                "status": self.outcome.status,
                "exit_code": self.outcome.exit_code,
                "output_digest": self.outcome.output_digest,
                "output_evidence_id": self.outcome.output_evidence_id,
            },
            "producer": {
                "id": self.producer.id,
                "version": self.producer.version,
                "run_id": self.producer.run_id,
            },
            "subject_digest": self.subject_digest,
        }


@dataclass(frozen=True, slots=True)
class _EvidenceProof:
    evidence_id: str
    source_native_id: str
    source_locator: str | None
    content_digest: str
    media_type: str
    metadata_json: str
    metadata_digest: str
    content_length_bytes: int
    integrity_outcome: str
    integrity_observed_digest: str | None
    integrity_observed_length: int | None
    integrity_expected_length: int | None
    provenance_actor: str
    provenance_actor_kind: str
    provenance_action: str
    provenance_ingestion_status: str | None
    provenance_tombstoned: int | None
    run_id: str
    run_connector_id: str
    run_source_kind: str
    run_job_type: str


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    return value


def _digest(document: str) -> str:
    return "sha256:" + sha256(document.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _bounded_text(value: object, limit: int) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and all(unicodedata.category(character) not in {"Cc", "Cs"} for character in value)
    )


def _object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    plain = _plain(value)
    if not isinstance(plain, dict) or set(plain) != keys:
        raise ValidationReceiptInvalid
    return plain


def validation_subject_digest(content: Mapping[str, Any]) -> str:
    """Digest every observation field except the receipt that attests to it."""
    subject = _plain(content)
    if not isinstance(subject, dict):  # Mapping above makes this defensive only.
        raise ValidationReceiptInvalid
    subject.pop(VALIDATION_RECEIPT_FIELD, None)
    return _digest(to_canonical_json(subject))


def validation_receipt_document_digest(document: Mapping[str, Any]) -> str:
    """Content address a receipt document using the contract's canonical JSON."""
    return _digest(to_canonical_json(_plain(document)))


def parse_factual_validation_receipt(
    content: Mapping[str, Any],
) -> ValidationExecutionReceipt | None:
    """Parse the receipt required by a factual ``validation_result``.

    Reported and hypothetical validation statements remain ordinary candidate
    claims and must not carry a field that looks like verified execution evidence.
    Other observation kinds do not use this profile either.
    """
    kind = content.get("kind")
    basis = content.get("assertion_basis")
    has_receipt = VALIDATION_RECEIPT_FIELD in content
    raw_receipt = content.get(VALIDATION_RECEIPT_FIELD)
    if kind != "validation_result":
        if has_receipt:
            raise ValidationReceiptInvalid
        return None
    if isinstance(basis, str) and basis in _NON_FACTUAL_BASES:
        if has_receipt:
            raise ValidationReceiptInvalid
        return None
    if basis not in {"observed", "derived"} or not has_receipt:
        raise ValidationReceiptInvalid

    envelope = _object(raw_receipt, _RECEIPT_KEYS)
    evidence_id = envelope["evidence_id"]
    if not isinstance(evidence_id, str) or not is_identifier(evidence_id):
        raise ValidationReceiptInvalid
    document = _object(envelope["document"], _DOCUMENT_KEYS)
    canonical = to_canonical_json(document)
    if (
        document["schema_version"] != "1.0"
        or len(canonical.encode("utf-8")) > _RECEIPT_DOCUMENT_CAP_BYTES
    ):
        raise ValidationReceiptInvalid

    execution = _object(document["execution"], _EXECUTION_KEYS)
    kind_value = execution["kind"]
    identity = execution["identity"]
    definition_digest = execution["definition_digest"]
    if (
        not isinstance(kind_value, str)
        or kind_value not in _EXECUTION_KINDS
        or not isinstance(identity, str)
        or not is_identifier(identity)
        or not _is_sha256(definition_digest)
    ):
        raise ValidationReceiptInvalid

    frontier = _object(document["evaluated_frontier"], _FRONTIER_KEYS)
    repository_id = frontier["repository_id"]
    stream_id = frontier["stream_id"]
    sequence = frontier["sequence"]
    snapshot_id = frontier["snapshot_id"]
    manifest_digest = frontier["manifest_digest"]
    if (
        not isinstance(repository_id, str)
        or not is_identifier(repository_id)
        or not isinstance(stream_id, str)
        or not is_identifier(stream_id)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= 2_147_483_647
        or not isinstance(snapshot_id, str)
        or not is_identifier(snapshot_id)
        or not _is_sha256(manifest_digest)
    ):
        raise ValidationReceiptInvalid

    outcome = _object(document["outcome"], _OUTCOME_KEYS)
    status = outcome["status"]
    exit_code = outcome["exit_code"]
    output_digest = outcome["output_digest"]
    output_evidence_id = outcome["output_evidence_id"]
    if (
        not isinstance(status, str)
        or status not in _OUTCOMES
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -(2**31) <= exit_code < 2**31
        or not _is_sha256(output_digest)
        or not isinstance(output_evidence_id, str)
        or not is_identifier(output_evidence_id)
        or output_evidence_id == evidence_id
    ):
        raise ValidationReceiptInvalid

    producer = _object(document["producer"], _PRODUCER_KEYS)
    producer_id = producer["id"]
    producer_version = producer["version"]
    run_id = producer["run_id"]
    if (
        not isinstance(producer_id, str)
        or not is_identifier(producer_id)
        or not _bounded_text(producer_version, 128)
        or not _bounded_text(run_id, 128)
    ):
        raise ValidationReceiptInvalid
    subject_digest = document["subject_digest"]
    if not _is_sha256(subject_digest):
        raise ValidationReceiptInvalid

    return ValidationExecutionReceipt(
        evidence_id=evidence_id,
        execution=ValidationExecution(
            kind=kind_value,
            identity=identity,
            definition_digest=definition_digest,
        ),
        evaluated_frontier=EvaluatedFrontier(
            repository_id=repository_id,
            stream_id=stream_id,
            sequence=sequence,
            snapshot_id=snapshot_id,
            manifest_digest=manifest_digest,
        ),
        outcome=ValidationOutcome(
            status=status,
            exit_code=exit_code,
            output_digest=output_digest,
            output_evidence_id=output_evidence_id,
        ),
        producer=ValidationProducer(
            id=producer_id,
            version=producer_version,
            run_id=run_id,
        ),
        subject_digest=subject_digest,
    )


def _evidence_proof(
    connection: sqlite3.Connection, *, workspace_id: str, evidence_id: str
) -> _EvidenceProof:
    row = connection.execute(
        "SELECT a.evidence_id, a.source_native_id, a.source_locator, "
        "a.blob_content_digest, a.media_type, a.original_metadata_json, "
        "a.original_metadata_digest, b.content_length_bytes, i.outcome, "
        "i.observed_digest, i.observed_length_bytes, i.expected_length_bytes, "
        "(SELECT p.actor_id FROM omnivia_evidence_provenance_events p "
        "WHERE p.workspace_id=a.workspace_id AND p.evidence_id=a.evidence_id "
        "AND p.action='source.ingested' ORDER BY p.provenance_sequence LIMIT 1), "
        "(SELECT p.actor_kind FROM omnivia_evidence_provenance_events p "
        "WHERE p.workspace_id=a.workspace_id AND p.evidence_id=a.evidence_id "
        "AND p.action='source.ingested' ORDER BY p.provenance_sequence LIMIT 1), "
        "(SELECT p.action FROM omnivia_evidence_provenance_events p "
        "WHERE p.workspace_id=a.workspace_id AND p.evidence_id=a.evidence_id "
        "AND p.action='source.ingested' ORDER BY p.provenance_sequence LIMIT 1), "
        "(SELECT p.ingestion_status FROM omnivia_evidence_provenance_events p "
        "WHERE p.workspace_id=a.workspace_id AND p.evidence_id=a.evidence_id "
        "AND p.ingestion_status IS NOT NULL "
        "ORDER BY p.provenance_sequence DESC LIMIT 1), "
        "(SELECT p.tombstoned_observation "
        "FROM omnivia_evidence_provenance_events p "
        "WHERE p.workspace_id=a.workspace_id AND p.evidence_id=a.evidence_id "
        "AND p.tombstoned_observation IS NOT NULL "
        "ORDER BY p.provenance_sequence DESC LIMIT 1), "
        "a.import_run_id, r.connector_id, r.source_kind, j.job_type "
        "FROM omnivia_evidence_artifacts a "
        "JOIN omnivia_blob_objects b ON b.workspace_id=a.workspace_id "
        "AND b.content_digest=a.blob_content_digest "
        "JOIN omnivia_staged_sources s ON s.workspace_id=a.workspace_id "
        "AND s.staged_source_ref=a.staged_source_ref "
        "AND s.source_kind=a.source_kind "
        "AND s.blob_content_digest=a.blob_content_digest "
        "AND s.original_metadata_json=a.original_metadata_json "
        "AND s.original_metadata_digest=a.original_metadata_digest "
        "AND s.staging_outcome='verified' "
        "JOIN omnivia_blob_integrity_events i ON i.workspace_id=a.workspace_id "
        "AND i.content_digest=a.blob_content_digest "
        "AND i.integrity_sequence=(SELECT MAX(ii.integrity_sequence) "
        "FROM omnivia_blob_integrity_events ii WHERE ii.workspace_id=a.workspace_id "
        "AND ii.content_digest=a.blob_content_digest) "
        "JOIN omnivia_connector_sync_runs r ON r.workspace_id=a.workspace_id "
        "AND r.run_id=a.import_run_id AND r.source_kind=a.source_kind "
        "JOIN omnivia_durable_jobs j ON j.job_id=a.import_run_id "
        "WHERE a.workspace_id=? AND a.evidence_id=? "
        "AND a.source_kind=? AND a.content_checksum=a.blob_content_digest "
        "AND a.ingestion_status='ingested'",
        (workspace_id, evidence_id, VALIDATION_SOURCE_KIND),
    ).fetchone()
    if row is None:
        raise ValidationReceiptInvalid
    return _EvidenceProof(
        evidence_id=str(row[0]),
        source_native_id=str(row[1]),
        source_locator=None if row[2] is None else str(row[2]),
        content_digest=str(row[3]),
        media_type=str(row[4]),
        metadata_json=str(row[5]),
        metadata_digest=str(row[6]),
        content_length_bytes=int(row[7]),
        integrity_outcome=str(row[8]),
        integrity_observed_digest=None if row[9] is None else str(row[9]),
        integrity_observed_length=None if row[10] is None else int(row[10]),
        integrity_expected_length=None if row[11] is None else int(row[11]),
        provenance_actor=str(row[12]),
        provenance_actor_kind=str(row[13]),
        provenance_action=str(row[14]),
        provenance_ingestion_status=None if row[15] is None else str(row[15]),
        provenance_tombstoned=None if row[16] is None else int(row[16]),
        run_id=str(row[17]),
        run_connector_id=str(row[18]),
        run_source_kind=str(row[19]),
        run_job_type=str(row[20]),
    )


def _verify_evidence(
    proof: _EvidenceProof,
    *,
    expected_digest: str,
    producer: ValidationProducer,
) -> None:
    try:
        metadata_value = json.loads(proof.metadata_json)
    except (TypeError, ValueError):
        metadata_value = None
    if not isinstance(metadata_value, dict) or set(metadata_value) != _METADATA_KEYS:
        raise ValidationReceiptInvalid
    if to_canonical_json(metadata_value) != proof.metadata_json:
        raise ValidationReceiptInvalid
    observed_lengths = {
        proof.content_length_bytes,
        proof.integrity_observed_length,
        proof.integrity_expected_length,
    }
    if (
        proof.content_digest != expected_digest
        or _digest(proof.metadata_json) != proof.metadata_digest
        or proof.integrity_outcome != "verified"
        or proof.integrity_observed_digest not in {None, expected_digest}
        or None in observed_lengths
        or len(observed_lengths) != 1
        or proof.provenance_actor != "core-service"
        or proof.provenance_actor_kind != "service"
        or proof.provenance_action != "source.ingested"
        or proof.provenance_ingestion_status != "ingested"
        or proof.provenance_tombstoned != 0
        or proof.run_id != producer.run_id
        or proof.run_connector_id != producer.id
        or proof.run_source_kind != VALIDATION_SOURCE_KIND
        or proof.run_job_type != "ingestion.import"
        or metadata_value["connector_id"] != producer.id
        or metadata_value["native_id"] != proof.source_native_id
        or metadata_value["locator"] != proof.source_locator
        or metadata_value["source_version"] != producer.version
    ):
        raise ValidationReceiptInvalid


def verify_factual_validation_receipt(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    content: Mapping[str, Any],
    evidence_ids: Sequence[str],
    receipt: ValidationExecutionReceipt,
) -> None:
    """Verify one factual result against immutable storage, or fail closed."""
    if receipt.subject_digest != validation_subject_digest(content):
        raise ValidationReceiptInvalid
    if receipt.outcome.status != "passed" or receipt.outcome.exit_code != 0:
        raise ValidationReceiptInvalid

    applicability = content.get("applicability")
    if not isinstance(applicability, Mapping):
        raise ValidationReceiptInvalid
    if (
        applicability.get("repository_id") != receipt.evaluated_frontier.repository_id
        or applicability.get("snapshot_id") != receipt.evaluated_frontier.snapshot_id
    ):
        raise ValidationReceiptInvalid
    linked = frozenset(evidence_ids)
    if (
        receipt.evidence_id not in linked
        or receipt.outcome.output_evidence_id not in linked
    ):
        raise ValidationReceiptInvalid

    frontier = receipt.evaluated_frontier
    source = connection.execute(
        "SELECT s.repository_id, s.capture_status, s.manifest_digest, e.stream_id, "
        "e.sequence, e.manifest_digest, st.covered_sequence "
        "FROM omnivia_engineering_snapshots s "
        "JOIN omnivia_engineering_source_events e "
        "ON e.workspace_id=s.workspace_id AND e.snapshot_id=s.snapshot_id "
        "JOIN omnivia_engineering_source_streams st "
        "ON st.workspace_id=e.workspace_id AND st.stream_id=e.stream_id "
        "AND st.repository_id=s.repository_id "
        "WHERE s.workspace_id=? AND s.snapshot_id=?",
        (workspace_id, frontier.snapshot_id),
    ).fetchone()
    if source is None:
        raise ValidationReceiptInvalid
    if (
        str(source[0]) != frontier.repository_id
        or str(source[1]) != "complete"
        or str(source[2]) != frontier.manifest_digest
        or str(source[3]) != frontier.stream_id
        or int(source[4]) != frontier.sequence
        or str(source[5]) != frontier.manifest_digest
        or int(source[6]) < frontier.sequence
    ):
        raise ValidationReceiptInvalid

    receipt_proof = _evidence_proof(
        connection, workspace_id=workspace_id, evidence_id=receipt.evidence_id
    )
    document_json = to_canonical_json(receipt.document())
    _verify_evidence(
        receipt_proof,
        expected_digest=_digest(document_json),
        producer=receipt.producer,
    )
    if (
        receipt_proof.media_type != VALIDATION_RECEIPT_MEDIA_TYPE
        or receipt_proof.source_native_id != receipt.execution.identity
        or receipt_proof.content_length_bytes != len(document_json.encode("utf-8"))
    ):
        raise ValidationReceiptInvalid

    output_proof = _evidence_proof(
        connection,
        workspace_id=workspace_id,
        evidence_id=receipt.outcome.output_evidence_id,
    )
    _verify_evidence(
        output_proof,
        expected_digest=receipt.outcome.output_digest,
        producer=receipt.producer,
    )

    replay = connection.execute(
        "SELECT 1 FROM omnivia_governed_version_evidence_links l "
        "JOIN omnivia_governed_version_assemblies a "
        "ON a.workspace_id=l.workspace_id AND a.assembly_id=l.assembly_id "
        "WHERE l.workspace_id=? AND l.evidence_id=? "
        "AND a.governed_record_id<>? LIMIT 1",
        (workspace_id, receipt.evidence_id, record_id),
    ).fetchone()
    if replay is not None:
        raise ValidationReceiptInvalid


__all__ = [
    "VALIDATION_RECEIPT_FIELD",
    "VALIDATION_RECEIPT_MEDIA_TYPE",
    "VALIDATION_SOURCE_KIND",
    "ValidationExecutionReceipt",
    "ValidationReceiptInvalid",
    "parse_factual_validation_receipt",
    "validation_receipt_document_digest",
    "validation_subject_digest",
    "verify_factual_validation_receipt",
]
