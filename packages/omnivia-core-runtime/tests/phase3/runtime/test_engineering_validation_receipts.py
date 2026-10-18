"""AC-025: factual validation results require immutable execution evidence.

The tests use the real application surface for create, proposal and acceptance.
Only the producer-owned L0 fixtures are seeded directly: they model the rows a
trusted validation runner's ingestion transaction leaves behind, while every
governed-memory mutation still crosses the production dispatcher and fenced writer.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import governance as governance_storage
from omnivia_core_runtime.storage import memory as memory_storage
from omnivia_core_runtime.storage.engineering_validation import (
    VALIDATION_RECEIPT_MEDIA_TYPE,
    VALIDATION_SOURCE_KIND,
    validation_receipt_document_digest,
    validation_subject_digest,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INVALID_REQUEST,
    ErrorResponseEnvelope,
    MutationPrecondition,
    SuccessResponseEnvelope,
    to_canonical_json,
)

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID
REPOSITORY = esc.REPOSITORY
STREAM = esc.STREAM
SNAPSHOT = "esnap-validation"
SNAPSHOT_B = "esnap-validation-b"
PRODUCER = "validation.runner"
PRODUCER_VERSION = "1.0.0"
RUN_ID = "job-validation-1"
EXECUTION_ID = "pytest-validation"
OUTPUT_NATIVE_ID = "pytest-validation-output"
RECEIPT_EVIDENCE_ID = "evd-validation-receipt"
OUTPUT_EVIDENCE_ID = "evd-validation-output"
INVALID_MESSAGE = (
    "factual validation results require verified immutable execution evidence"
)


def _sha(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    opened.record(esc._source(1, SNAPSHOT, esc.FILES_A), key="validation-source")
    opened.record(
        esc._source(2, SNAPSHOT_B, esc.FILES_A, predecessor=SNAPSHOT),
        key="validation-source-b",
    )
    yield opened
    opened.holder.connection.close()


def _frontier(workspace: Workspace, snapshot: str = SNAPSHOT) -> dict[str, Any]:
    row = workspace.holder.connection.execute(
        "SELECT e.sequence, e.manifest_digest FROM omnivia_engineering_source_events e "
        "WHERE e.workspace_id=? AND e.stream_id=? AND e.snapshot_id=?",
        (WORKSPACE_ID, STREAM, snapshot),
    ).fetchone()
    assert row is not None
    return {
        "repository_id": REPOSITORY,
        "stream_id": STREAM,
        "sequence": int(row[0]),
        "snapshot_id": snapshot,
        "manifest_digest": str(row[1]),
    }


def _base_content(
    *,
    kind: str = "validation_result",
    basis: str = "observed",
    applicability_snapshot: str = SNAPSHOT,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": kind,
        "title": "Core validation",
        "summary": "The focused Core validation passed.",
        "what": "The focused test command completed successfully.",
        "assertion_basis": basis,
        "applicability": {
            "repository_id": REPOSITORY,
            "snapshot_id": applicability_snapshot,
        },
    }


def _document(
    workspace: Workspace,
    content: dict[str, Any],
    *,
    producer: str = PRODUCER,
    snapshot: str = SNAPSHOT,
    execution_kind: str = "command",
    status: str = "passed",
    exit_code: int = 0,
    frontier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if frontier is None:
        frontier = _frontier(workspace, snapshot=SNAPSHOT)
        frontier["snapshot_id"] = snapshot
    return {
        "schema_version": "1.0",
        "execution": {
            "kind": execution_kind,
            "identity": EXECUTION_ID,
            "definition_digest": _sha("pytest focused-validation"),
        },
        "evaluated_frontier": frontier,
        "outcome": {
            "status": status,
            "exit_code": exit_code,
            "output_digest": _sha("17 passed in 1.23s\n"),
            "output_evidence_id": OUTPUT_EVIDENCE_ID,
        },
        "producer": {
            "id": producer,
            "version": PRODUCER_VERSION,
            "run_id": RUN_ID,
        },
        "subject_digest": validation_subject_digest(content),
    }


def _source(native_id: str, locator: str) -> dict[str, Any]:
    return {
        "kind": VALIDATION_SOURCE_KIND,
        "source_id": native_id,
        "locator": locator,
        "retrieved_at": _timestamp(m2.BASE_US),
    }


def _claim(content: dict[str, Any], *, evidence: bool = True) -> dict[str, Any]:
    sources = (
        [
            _source(EXECUTION_ID, "validation://receipt"),
            _source(OUTPUT_NATIVE_ID, "validation://output"),
        ]
        if evidence
        else []
    )
    return {
        "record_type": "knowledge.finding",
        "domain_scope": "engineering.codebase",
        "content": content,
        "evidence_disposition": "available" if evidence else "unavailable",
        "sources": sources,
        "assertion": {
            "actor_id": "agent-1",
            "actor_kind": "agent",
            "actor_role": "contributor",
            "asserted_at": "2026-01-27T00:00:00Z",
            "evidence": [{"source": source} for source in sources],
        },
    }


def _insert_artifact(
    workspace: Workspace,
    *,
    evidence_id: str,
    native_id: str,
    locator: str,
    media_type: str,
    content_digest: str,
    content_length: int,
    ordinal: int,
) -> None:
    connection = workspace.holder.connection
    at_us = m2.BASE_US + 100 + ordinal
    metadata = to_canonical_json(
        {
            "connector_id": PRODUCER,
            "native_id": native_id,
            "locator": locator,
            "source_version": PRODUCER_VERSION,
        }
    )
    metadata_digest = _sha(metadata)
    staged_ref = f"stg-validation-{ordinal}"
    m2.insert(
        connection,
        m2.BLOBS,
        m2.row_for(
            m2.BLOBS,
            content_digest=content_digest,
            content_length_bytes=content_length,
            created_at_us=at_us,
            verified_at_us=at_us,
        ),
    )
    m2.insert(
        connection,
        m2.INTEGRITY,
        m2.row_for(
            m2.INTEGRITY,
            integrity_event_id=f"bie-validation-{ordinal}",
            content_digest=content_digest,
            integrity_sequence=1,
            observed_digest=content_digest,
            observed_length_bytes=content_length,
            expected_length_bytes=content_length,
            checked_at_us=at_us,
        ),
    )
    m2.insert(
        connection,
        m2.STAGED,
        m2.row_for(
            m2.STAGED,
            staged_source_ref=staged_ref,
            source_kind=VALIDATION_SOURCE_KIND,
            declared_checksum=content_digest,
            content_length_bytes=content_length,
            media_type=media_type,
            source_version=PRODUCER_VERSION,
            computed_checksum=content_digest,
            original_metadata_json=metadata,
            original_metadata_digest=metadata_digest,
            blob_content_digest=content_digest,
            recorded_at_us=at_us,
        ),
    )
    m2.insert(
        connection,
        m2.EVIDENCE,
        m2.row_for(
            m2.EVIDENCE,
            evidence_id=evidence_id,
            source_kind=VALIDATION_SOURCE_KIND,
            source_native_id=native_id,
            source_locator=locator,
            source_retrieved_at_us=m2.BASE_US,
            event_at_us=at_us,
            observed_at_us=at_us,
            ingested_at_us=at_us,
            recorded_at_us=at_us,
            content_checksum=content_digest,
            blob_content_digest=content_digest,
            media_type=media_type,
            original_metadata_json=metadata,
            original_metadata_digest=metadata_digest,
            sensitivity="internal",
            parser_status="not_parsed",
            ingestion_status="ingested",
            staged_source_ref=staged_ref,
            import_run_id=RUN_ID,
        ),
    )
    m2.insert(
        connection,
        m2.LABELS,
        m2.row_for(
            m2.LABELS,
            label_event_id=f"lbl-validation-{ordinal}",
            evidence_id=evidence_id,
            label_sequence=1,
            recorded_at_us=at_us,
        ),
    )
    m2.insert(
        connection,
        m2.PROVENANCE,
        m2.row_for(
            m2.PROVENANCE,
            provenance_event_id=f"prv-validation-{ordinal}",
            evidence_id=evidence_id,
            provenance_sequence=1,
            actor_id="core-service",
            actor_kind="service",
            action="source.ingested",
            occurred_at_us=at_us,
            ingestion_status="ingested",
            tombstoned_observation=0,
            source_kind=VALIDATION_SOURCE_KIND,
            source_native_id=native_id,
        ),
    )


def _seed_execution_evidence(
    workspace: Workspace, document: dict[str, Any]
) -> None:
    connection = workspace.holder.connection
    audit = connection.execute(
        "SELECT audit_ref FROM omnivia_application_audit_events "
        "WHERE workspace_id=? ORDER BY recorded_at_us DESC LIMIT 1",
        (WORKSPACE_ID,),
    ).fetchone()
    assert audit is not None
    document_json = to_canonical_json(document)
    receipt_digest = validation_receipt_document_digest(document)
    output_digest = str(document["outcome"]["output_digest"])
    with fenced_transaction(
        connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    ):
        connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', 'claimed', '{}', ?, ?, ?, ?)",
            (
                RUN_ID,
                "2026-09-27T00:00:00+00:00",
                "2026-09-27T00:00:00+00:00",
                workspace.holder.generation,
                workspace.holder.identity.service_instance_id,
            ),
        )
        connection.execute(
            "INSERT INTO omnivia_job_application_metadata "
            "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
            "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
            "max_attempts) VALUES (?, ?, 'ingestion.import', 'import.start', ?, ?, "
            "NULL, 1, 3)",
            (WORKSPACE_ID, RUN_ID, str(audit[0]), m2.BASE_US + 100),
        )
        connection.execute(
            "INSERT INTO omnivia_connector_sync_runs "
            "(workspace_id, connector_id, sync_sequence, run_id, source_kind, "
            "state_version, started_at_us) VALUES (?, ?, 1, ?, ?, 1, ?)",
            (
                WORKSPACE_ID,
                PRODUCER,
                RUN_ID,
                VALIDATION_SOURCE_KIND,
                m2.BASE_US + 100,
            ),
        )
        _insert_artifact(
            workspace,
            evidence_id=RECEIPT_EVIDENCE_ID,
            native_id=EXECUTION_ID,
            locator="validation://receipt",
            media_type=VALIDATION_RECEIPT_MEDIA_TYPE,
            content_digest=receipt_digest,
            content_length=len(document_json.encode("utf-8")),
            ordinal=1,
        )
        _insert_artifact(
            workspace,
            evidence_id=OUTPUT_EVIDENCE_ID,
            native_id=OUTPUT_NATIVE_ID,
            locator="validation://output",
            media_type="text/plain",
            content_digest=output_digest,
            content_length=len(b"17 passed in 1.23s\n"),
            ordinal=2,
        )


def _prepared_claim(
    workspace: Workspace, **document_overrides: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    content = _base_content()
    document = _document(workspace, content, **document_overrides)
    _seed_execution_evidence(workspace, document)
    content["validation_receipt"] = {
        "evidence_id": RECEIPT_EVIDENCE_ID,
        "document": copy.deepcopy(document),
    }
    return _claim(content), document


def _identity(result: dict[str, Any], key: str = "record") -> dict[str, str]:
    identity = result[key]["provenance"]["identity"]
    return {"record_id": str(identity["record_id"]), "version": str(identity["version"])}


def _transition(workspace: Workspace, operation: str, record: dict[str, str]) -> Any:
    return workspace.call(
        operation,
        {"record_id": record["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=record["version"]),
    )


def _assert_receipt_refusal(response: Any) -> None:
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_INVALID_REQUEST
    assert response.error.message == INVALID_MESSAGE
    assert "pytest" not in response.error.message
    assert "sha256:" not in response.error.message


def test_observed_validation_without_receipt_fails_closed(workspace: Workspace) -> None:
    response = workspace.call("memory.create", _claim(_base_content(), evidence=False))
    _assert_receipt_refusal(response)


@pytest.mark.parametrize("basis", ["reported", "hypothesis"])
def test_non_factual_manual_validation_remains_a_claim(
    workspace: Workspace, basis: str
) -> None:
    response = workspace.call(
        "memory.create", _claim(_base_content(basis=basis), evidence=False)
    )
    assert isinstance(response, SuccessResponseEnvelope), response


def test_non_validation_observation_does_not_need_execution_evidence(
    workspace: Workspace,
) -> None:
    response = workspace.call(
        "memory.create",
        _claim(_base_content(kind="decision", basis="observed"), evidence=False),
    )
    assert isinstance(response, SuccessResponseEnvelope), response


def test_manual_execution_cannot_be_promoted_to_factual_validation(
    workspace: Workspace,
) -> None:
    content = _base_content()
    document = _document(workspace, content, execution_kind="manual")
    content["validation_receipt"] = {
        "evidence_id": RECEIPT_EVIDENCE_ID,
        "document": document,
    }
    _assert_receipt_refusal(workspace.call("memory.create", _claim(content, evidence=False)))


@pytest.mark.parametrize("execution_kind", ["command", "workflow"])
def test_verified_receipt_survives_proposal_and_acceptance(
    workspace: Workspace, execution_kind: str
) -> None:
    claim, _document_value = _prepared_claim(
        workspace, execution_kind=execution_kind
    )
    created = _identity(workspace.ok("memory.create", claim, key="validation-create"))
    proposed_response = _transition(workspace, "knowledge.propose", created)
    assert isinstance(proposed_response, SuccessResponseEnvelope), proposed_response
    proposed = _identity(dict(proposed_response.to_wire()["result"]), "updated_record")
    approved_response = _transition(workspace, "candidate.approve", proposed)
    assert isinstance(approved_response, SuccessResponseEnvelope), approved_response


@pytest.mark.parametrize(
    ("overrides", "case"),
    [
        ({"snapshot": "esnap-wrong"}, "wrong snapshot"),
        ({"producer": "forged.runner"}, "wrong producer"),
        ({"status": "failed", "exit_code": 1}, "failed exit"),
    ],
)
def test_receipt_authority_failures_are_bounded(
    workspace: Workspace, overrides: dict[str, Any], case: str
) -> None:
    claim, _document_value = _prepared_claim(workspace, **overrides)
    response = workspace.call(
        "memory.create", claim, key=f"invalid-{case.replace(' ', '-')}"
    )
    _assert_receipt_refusal(response)


def test_inline_receipt_tampering_breaks_its_immutable_digest(
    workspace: Workspace,
) -> None:
    claim, _document_value = _prepared_claim(workspace)
    receipt = claim["content"]["validation_receipt"]["document"]
    receipt["outcome"]["output_digest"] = _sha("fabricated output")
    _assert_receipt_refusal(workspace.call("memory.create", claim, key="tampered"))


def test_receipt_cannot_be_replayed_into_a_second_record(workspace: Workspace) -> None:
    claim, _document_value = _prepared_claim(workspace)
    first = workspace.call("memory.create", claim, key="receipt-first")
    assert isinstance(first, SuccessResponseEnvelope), first
    replay = workspace.call("memory.create", claim, key="receipt-second")
    _assert_receipt_refusal(replay)
    honest_retry = workspace.call("memory.create", claim, key="receipt-first")
    assert isinstance(honest_retry, SuccessResponseEnvelope), honest_retry
    assert honest_retry.to_wire()["result"] == first.to_wire()["result"]


@pytest.mark.parametrize("operation", ["knowledge.propose", "candidate.approve"])
def test_governance_rechecks_receipt_after_a_later_payload_mutation(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    claim, _document_value = _prepared_claim(workspace)
    created = _identity(workspace.ok("memory.create", claim, key="mutation-create"))
    source = created
    if operation == "candidate.approve":
        proposed_response = _transition(workspace, "knowledge.propose", created)
        assert isinstance(proposed_response, SuccessResponseEnvelope), proposed_response
        source = _identity(
            dict(proposed_response.to_wire()["result"]), "updated_record"
        )

    original_source = governance_storage._source

    def altered_source(*args: Any, **kwargs: Any) -> Any:
        stored = original_source(*args, **kwargs)
        content = json.loads(stored.content_json)
        content["summary"] = "A different payload tried to reuse the receipt."
        changed = to_canonical_json(content)
        return replace(
            stored,
            content_json=changed,
            content_digest=_sha(changed),
        )

    monkeypatch.setattr(governance_storage, "_source", altered_source)
    response = _transition(workspace, operation, source)
    _assert_receipt_refusal(response)


def test_output_evidence_mutation_is_rechecked_before_acceptance(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim, _document_value = _prepared_claim(workspace)
    created = _identity(workspace.ok("memory.create", claim, key="evidence-create"))
    proposed_response = _transition(workspace, "knowledge.propose", created)
    assert isinstance(proposed_response, SuccessResponseEnvelope), proposed_response
    proposed = _identity(dict(proposed_response.to_wire()["result"]), "updated_record")

    original_proof = governance_storage._verify_validation_receipt

    def verify_with_missing_output(*args: Any, **kwargs: Any) -> None:
        kwargs["evidence_ids"] = tuple(
            evidence_id
            for evidence_id in kwargs["evidence_ids"]
            if evidence_id != OUTPUT_EVIDENCE_ID
        )
        original_proof(*args, **kwargs)

    monkeypatch.setattr(
        governance_storage, "_verify_validation_receipt", verify_with_missing_output
    )
    _assert_receipt_refusal(_transition(workspace, "candidate.approve", proposed))


def _cross_snapshot_claim(workspace: Workspace) -> dict[str, Any]:
    """Content scoped to snapshot A, backed by a receipt evaluated against B."""
    content = _base_content(applicability_snapshot=SNAPSHOT)
    document = _document(workspace, content, frontier=_frontier(workspace, SNAPSHOT_B))
    _seed_execution_evidence(workspace, document)
    content["validation_receipt"] = {
        "evidence_id": RECEIPT_EVIDENCE_ID,
        "document": copy.deepcopy(document),
    }
    return _claim(content)


def test_receipt_scoped_to_other_snapshot_cannot_create(workspace: Workspace) -> None:
    claim = _cross_snapshot_claim(workspace)
    _assert_receipt_refusal(workspace.call("memory.create", claim, key="cross-snapshot"))


@pytest.mark.parametrize("operation", ["knowledge.propose", "candidate.approve"])
def test_receipt_scoped_to_other_snapshot_cannot_propose_or_approve(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Governance rechecks a legacy cross-snapshot record at each boundary."""

    def bypass_for_legacy_fixture(*_args: Any, **_kwargs: Any) -> None:
        return None

    claim = _cross_snapshot_claim(workspace)
    with monkeypatch.context() as creation_bypass:
        creation_bypass.setattr(
            memory_storage,
            "_verify_validation_receipt",
            bypass_for_legacy_fixture,
        )
        created = _identity(
            workspace.ok("memory.create", claim, key="legacy-cross-snapshot")
        )
    source = created
    if operation == "candidate.approve":
        with monkeypatch.context() as proposal_bypass:
            proposal_bypass.setattr(
                governance_storage,
                "_verify_validation_receipt",
                bypass_for_legacy_fixture,
            )
            proposed_response = _transition(workspace, "knowledge.propose", created)
        assert isinstance(proposed_response, SuccessResponseEnvelope), proposed_response
        source = _identity(dict(proposed_response.to_wire()["result"]), "updated_record")

    _assert_receipt_refusal(_transition(workspace, operation, source))


@pytest.mark.parametrize("applicability", [None, "snapshot-a"])
def test_factual_receipt_requires_mapping_applicability(
    workspace: Workspace, applicability: object
) -> None:
    content = _base_content()
    content["applicability"] = applicability
    document = _document(workspace, content, frontier=_frontier(workspace, SNAPSHOT))
    _seed_execution_evidence(workspace, document)
    content["validation_receipt"] = {
        "evidence_id": RECEIPT_EVIDENCE_ID,
        "document": copy.deepcopy(document),
    }
    _assert_receipt_refusal(
        workspace.call("memory.create", _claim(content), key="invalid-applicability")
    )


@pytest.mark.parametrize("snapshot", [SNAPSHOT, SNAPSHOT_B])
def test_matching_snapshot_binding_survives_creation_proposal_and_acceptance(
    workspace: Workspace, snapshot: str
) -> None:
    content = _base_content(applicability_snapshot=snapshot)
    document = _document(workspace, content, frontier=_frontier(workspace, snapshot))
    _seed_execution_evidence(workspace, document)
    content["validation_receipt"] = {
        "evidence_id": RECEIPT_EVIDENCE_ID,
        "document": copy.deepcopy(document),
    }
    claim = _claim(content)

    created = _identity(workspace.ok("memory.create", claim, key=f"matching-{snapshot}"))
    proposed_response = _transition(workspace, "knowledge.propose", created)
    assert isinstance(proposed_response, SuccessResponseEnvelope), proposed_response
    proposed = _identity(dict(proposed_response.to_wire()["result"]), "updated_record")
    approved_response = _transition(workspace, "candidate.approve", proposed)
    assert isinstance(approved_response, SuccessResponseEnvelope), approved_response
