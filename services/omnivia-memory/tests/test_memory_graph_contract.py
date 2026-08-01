"""Tests for backend-neutral memory graph contracts."""

import json
from pathlib import Path

from omnivia_memory.memory_graph import (
    EvidenceGraphResponse,
    GraphPreviewResponse,
    MemoryFact,
    SourceRef,
    build_memory_graph_fixture,
    validate_evidence_graph_response,
    validate_graph_preview_response,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memory_graph" / "fixture_bundle.json"


def test_fixture_contains_required_contract_shapes() -> None:
    fixture = build_memory_graph_fixture()

    assert fixture["source"].id == "source-001"
    assert len(fixture["segments"]) == 2
    assert len(fixture["entities"]) == 2
    assert len(fixture["facts"]) == 3
    assert isinstance(fixture["graph_preview"], GraphPreviewResponse)
    assert isinstance(fixture["evidence_graph"], EvidenceGraphResponse)


def test_json_fixture_file_matches_contract_shapes() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    graph_preview = GraphPreviewResponse.from_dict(data["graph_preview"])
    evidence_graph = EvidenceGraphResponse.from_dict(data["evidence_graph"])
    facts = [MemoryFact.from_dict(item) for item in data["facts"]]

    assert validate_graph_preview_response(graph_preview).valid
    assert validate_evidence_graph_response(evidence_graph).valid
    assert [fact.id for fact in facts] == ["fact-001", "fact-002", "fact-003"]


def test_contracts_round_trip_through_dicts() -> None:
    fixture = build_memory_graph_fixture()
    graph_preview = fixture["graph_preview"]
    evidence_graph = fixture["evidence_graph"]

    assert GraphPreviewResponse.from_dict(graph_preview.to_dict()) == graph_preview
    assert EvidenceGraphResponse.from_dict(evidence_graph.to_dict()) == evidence_graph
    for fact in fixture["facts"]:
        assert MemoryFact.from_dict(fact.to_dict()) == fact


def test_fixture_contracts_validate() -> None:
    fixture = build_memory_graph_fixture()

    assert validate_memory_source(fixture["source"]).valid
    for segment in fixture["segments"]:
        assert validate_memory_segment(segment).valid
    for entity in fixture["entities"]:
        assert validate_memory_entity(entity).valid
    for fact in fixture["facts"]:
        assert validate_memory_fact(fact).valid
    assert validate_graph_preview_response(fixture["graph_preview"]).valid
    assert validate_evidence_graph_response(fixture["evidence_graph"]).valid


def test_fact_requires_exactly_one_object_shape() -> None:
    fixture = build_memory_graph_fixture()
    source_fact = fixture["facts"][0]

    with_two_targets = MemoryFact(
        id="fact-invalid",
        workspace_id=source_fact.workspace_id,
        subject_id=source_fact.subject_id,
        predicate=source_fact.predicate,
        object_id=source_fact.object_id,
        object_value="also present",
        confidence=source_fact.confidence,
        source_refs=source_fact.source_refs,
        status=source_fact.status,
        created_at=source_fact.created_at,
        updated_at=source_fact.updated_at,
    )
    no_target = MemoryFact(
        id="fact-invalid-2",
        workspace_id=source_fact.workspace_id,
        subject_id=source_fact.subject_id,
        predicate=source_fact.predicate,
        object_id=None,
        object_value=None,
        confidence=source_fact.confidence,
        source_refs=source_fact.source_refs,
        status=source_fact.status,
        created_at=source_fact.created_at,
        updated_at=source_fact.updated_at,
    )

    assert validate_memory_fact(with_two_targets).errors == [
        "exactly one of object_id or object_value is required"
    ]
    assert validate_memory_fact(no_target).errors == [
        "exactly one of object_id or object_value is required"
    ]


def test_missing_evidence_is_explicit_warning_not_silent() -> None:
    fixture = build_memory_graph_fixture()
    missing_evidence_fact = fixture["facts"][2]

    result = validate_memory_fact(missing_evidence_fact)

    assert result.valid
    assert result.warnings == [
        "fact has no source_refs; UI must show missing-evidence warning"
    ]


def test_confidence_values_are_constrained() -> None:
    fixture = build_memory_graph_fixture()
    source_fact = fixture["facts"][0]
    invalid_fact = MemoryFact(
        id="fact-invalid-confidence",
        workspace_id=source_fact.workspace_id,
        subject_id=source_fact.subject_id,
        predicate=source_fact.predicate,
        object_id=source_fact.object_id,
        object_value=None,
        confidence=2.0,
        source_refs=source_fact.source_refs,
        status=source_fact.status,
        created_at=source_fact.created_at,
        updated_at=source_fact.updated_at,
    )

    assert validate_memory_fact(invalid_fact).errors == [
        "confidence must be between 0.0 and 1.0"
    ]


def test_sensitive_fields_are_rejected_recursively() -> None:
    fixture = build_memory_graph_fixture()
    source_ref = SourceRef(
        source_id="source-001",
        segment_id="segment-001",
        span={"token": "not allowed", "cookie": "session=123"},
        confidence="extracted",
    )
    unsafe_fact = MemoryFact(
        id="fact-unsafe",
        workspace_id=fixture["source"].workspace_id,
        subject_id="entity-core",
        predicate="uses",
        object_value="redacted connector",
        confidence="extracted",
        source_refs=[source_ref],
        status=fixture["facts"][0].status,
        created_at=fixture["facts"][0].created_at,
        updated_at=fixture["facts"][0].updated_at,
    )

    errors = validate_memory_fact(unsafe_fact).errors

    assert "source_refs[0].span.token must not expose sensitive fields" in errors
    assert "source_refs[0].span.cookie must not expose sensitive fields" in errors
