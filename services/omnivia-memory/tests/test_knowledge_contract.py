"""Tests for the portable knowledge contracts, fixtures, and helpers."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

from omnivia_memory.knowledge import (
    GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_CONTRACT_VERSION,
    AgentGraphContext,
    ContractVersion,
    GraphConfidence,
    GraphSourceType,
    GraphReviewStatus,
    GraphSensitivity,
    KnowledgeExtensionManifest,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    check_contract_version_compatibility,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_label,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
    summarize_confidence,
    summarize_review_status,
    summarize_sensitivity,
    validate_knowledge_space,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "knowledge"
POSITIVE_ROOT = FIXTURE_ROOT / "positive"
NEGATIVE_ROOT = FIXTURE_ROOT / "negative"


def _load_space_fixture(path: Path) -> KnowledgeSpace:
    return KnowledgeSpace.from_dict(json.loads(path.read_text(encoding="utf-8")))


def test_positive_fixtures_validate() -> None:
    for path in sorted(POSITIVE_ROOT.glob("*.json")):
        fixture = _load_space_fixture(path)
        result = validate_knowledge_space(fixture)
        assert result.valid, f"{path.name}: {result.errors}"


def test_negative_fixtures_fail_for_expected_reasons() -> None:
    for path in sorted(NEGATIVE_ROOT.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        expected_errors = data.pop("expected_errors")
        fixture = KnowledgeSpace.from_dict(data)
        result = validate_knowledge_space(fixture)

        assert not result.valid, path.name
        for expected_error in expected_errors:
            assert any(expected_error in error for error in result.errors), (
                path.name,
                expected_error,
                result.errors,
            )


def test_contracts_round_trip_through_dicts() -> None:
    fixture = _load_space_fixture(POSITIVE_ROOT / "research_corpus.json")

    assert KnowledgeSpace.from_dict(fixture.to_dict()) == fixture


def test_version_compatibility_allows_forward_minor_but_rejects_major() -> None:
    assert check_contract_version_compatibility(
        ContractVersion(1, 3),
        KNOWLEDGE_CONTRACT_VERSION,
    ).warnings == ["contract minor version 1.3 is newer than validated 1.0"]
    assert check_contract_version_compatibility(
        ContractVersion(2, 0),
        GRAPH_CONTRACT_VERSION,
    ).errors == ["unsupported contract major version 2.0; expected 1.x"]


def test_normalization_helpers_are_deterministic() -> None:
    assert normalize_space_id("Research Vault") == "research-vault"
    assert normalize_graph_node_id("API Surface") == "api-surface"
    assert normalize_graph_edge_id("Call Graph / Edge") == "call-graph-edge"
    assert normalize_label("  Unicode  Label\t") == "Unicode Label"
    assert normalize_tags(["Research", "research", "team notes"]) == [
        "research",
        "team-notes",
    ]
    assert normalize_source_path(r"docs\\notes\\summary.md") == "docs/notes/summary.md"


def test_summary_helpers_preserve_review_and_sensitivity_distinctions() -> None:
    confidence_summary = summarize_confidence(
        [GraphConfidence.EXTRACTED, GraphConfidence.INFERRED, 0.7]
    )
    review_summary = summarize_review_status(
        [GraphReviewStatus.REVIEWED, GraphReviewStatus.REVIEWED, GraphReviewStatus.APPROVED]
    )
    sensitivity_summary = summarize_sensitivity(
        [GraphSensitivity.RESTRICTED, GraphSensitivity.INTERNAL, GraphSensitivity.RESTRICTED]
    )

    assert confidence_summary == {
        "EXTRACTED": 1,
        "INFERRED": 1,
        "AMBIGUOUS": 0,
        "NUMERIC": 1,
    }
    assert review_summary["REVIEWED"] == 2
    assert sensitivity_summary["restricted"] == 2


def test_agent_context_requires_provenance_or_missing_evidence_marker() -> None:
    fixture = _load_space_fixture(POSITIVE_ROOT / "agent_memory.json")
    invalid_context = AgentGraphContext(
        id="agent-context-no-evidence",
        space_id=fixture.id,
        summary="Context without provenance",
        object_ids=["agent-memory-entry"],
        link_ids=[],
        claim_ids=[],
        confidence_summary={},
        review_summary={},
        sensitivity_summary={},
        warnings=[],
        source_refs=[],
        missing_evidence=False,
    )
    invalid_fixture = KnowledgeSpace(
        **{
            **fixture.__dict__,
            "agent_contexts": [invalid_context],
        }
    )

    result = validate_knowledge_space(invalid_fixture)

    assert not result.valid
    assert (
        "agent_contexts[0].agent_graph_context must include source_refs or set "
        "missing_evidence when it influences decisions" in result.errors
    )


def test_official_omnivia_extensions_validate_when_manifest_declares_them() -> None:
    source = KnowledgeSource(
        id="source-official",
        space_id="omnivia-space",
        source_type=GraphSourceType.DOCUMENT,
        title="Official extension source",
        relative_path="docs/official.md",
    )
    obj = KnowledgeObject(
        id="review-gate-object",
        space_id="omnivia-space",
        kind="omnivia:review_gate",
        title="Review Gate",
        tags=["review-gate"],
        source_refs=[
            SourceRef(
                source_id="source-official",
                source_type=GraphSourceType.DOCUMENT,
                path="docs/official.md",
                confidence=GraphConfidence.EXTRACTED,
            )
        ],
    )
    space = KnowledgeSpace(
        id="omnivia-space",
        title="Official OmniVia Extensions",
        space_type="team workspace",
        contract_version=KNOWLEDGE_CONTRACT_VERSION,
        tags=["omnivia-space"],
        sources=[source],
        objects=[obj],
        extension_manifests=[
            KnowledgeExtensionManifest(
                id="omnivia-extension-manifest",
                contract_version=KNOWLEDGE_CONTRACT_VERSION,
                namespace="omnivia",
                title="Official OmniVia Extensions",
                version="1.0",
                object_kinds=["omnivia:review_gate"],
                node_kinds=["omnivia:review_gate"],
                relations=["omnivia:review_path"],
                official=True,
            )
        ],
    )

    result = validate_knowledge_space(space)

    assert result.valid, result.errors


def test_no_graphify_or_obsidian_dependencies_are_declared() -> None:
    package_json = json.loads(
        (Path(__file__).parents[3] / "package.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    serialized = json.dumps(package_json).lower()
    dependencies = " ".join(pyproject["project"].get("dependencies", [])).lower()

    assert "graphify" not in serialized
    assert "graphify" not in dependencies
    assert "obsidian" not in serialized
    assert "obsidian" not in dependencies
