"""Tests for the package-level public API."""

from pathlib import Path

import omnivia_memory
from omnivia_memory import (
    GRAPH_CONTRACT_VERSION,
    GraphConfidence,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphOrigin,
    GraphReviewStatus,
    GraphSensitivity,
    GraphSourceType,
    GraphVisibility,
    KNOWLEDGE_CONTRACT_VERSION,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_source_path,
    normalize_space_id,
    validate_knowledge_space,
)


def test_top_level_exports_portable_knowledge_contracts() -> None:
    """Package root imports construct a valid portable knowledge space."""
    source = KnowledgeSource(
        id="source-01",
        space_id=normalize_space_id("Example Space"),
        source_type=GraphSourceType.NOTE,
        title="Daily Note",
        relative_path=normalize_source_path("notes/daily.md"),
    )
    object_source_ref = SourceRef(
        source_id=source.id,
        source_type=GraphSourceType.NOTE,
        path=source.relative_path,
        confidence=GraphConfidence.EXTRACTED,
    )
    object_one = KnowledgeObject(
        id="daily-note",
        space_id=source.space_id,
        kind="note",
        title="Daily Note",
        tags=["daily-note"],
        source_refs=[object_source_ref],
        confidence=GraphConfidence.EXTRACTED,
        review_status=GraphReviewStatus.REVIEWED,
    )
    graph_fragment = GraphFragment(
        id="fragment-01",
        space_id=source.space_id,
        contract_version=GRAPH_CONTRACT_VERSION,
        origin=GraphOrigin.MANUAL,
        owner="portable",
        nodes=[
            GraphNode(
                id=normalize_graph_node_id("Daily Note"),
                space_id=source.space_id,
                label="Daily Note",
                kind="note",
                object_id=object_one.id,
                source_refs=[object_source_ref],
                confidence=GraphConfidence.EXTRACTED,
            )
        ],
        edges=[
            GraphEdge(
                id=normalize_graph_edge_id("Daily Note self edge"),
                space_id=source.space_id,
                source="daily-note",
                target="daily-note",
                relation="related_to",
                source_refs=[object_source_ref],
                confidence=GraphConfidence.AMBIGUOUS,
                evidence_strength=GraphEvidenceStrength.WEAK,
                visibility=GraphVisibility.PRIVATE,
                sensitivity=GraphSensitivity.INTERNAL,
                missing_evidence=False,
            )
        ],
        source_refs=[object_source_ref],
    )
    space = KnowledgeSpace(
        id=source.space_id,
        title="Example Space",
        space_type="personal vault",
        contract_version=KNOWLEDGE_CONTRACT_VERSION,
        sources=[source],
        objects=[object_one],
        graph_fragments=[graph_fragment],
    )

    result = validate_knowledge_space(space)

    assert result.valid
    assert not result.errors


def test_all_declares_contract_only_root_api() -> None:
    """__all__ records the intentional contract-only root API."""
    expected = {
        "GraphFragment",
        "GraphNode",
        "GraphEdge",
        "GraphConfidence",
        "GraphReviewStatus",
        "KnowledgeObject",
        "KnowledgeSource",
        "KnowledgeSpace",
        "SourceRef",
        "ValidationResult",
        "normalize_space_id",
        "normalize_source_path",
        "validate_knowledge_space",
    }
    forbidden = {
        "MemoryService",
        "IngestionPipeline",
        "FileScanner",
        "SearchService",
        "GraphService",
        "Database",
        "WorkspaceService",
        "get_database",
    }

    assert expected.issubset(set(omnivia_memory.__all__))
    assert forbidden.isdisjoint(set(omnivia_memory.__all__))


def test_contract_surface_has_no_runtime_import_creep() -> None:
    """Knowledge surface files stay isolated from runtime-oriented modules."""
    source_root = Path(__file__).parents[1] / "src" / "omnivia_memory"
    files_to_check = list((source_root / "knowledge").glob("*.py")) + [source_root / "__init__.py"]
    forbidden_import_markers = {
        "omnivia_memory.ingestion",
        "omnivia_memory.persistence",
        "omnivia_memory.search",
        "omnivia_memory.workspace",
        "omnivia_memory.app_shell_bridge",
        "omnivia_memory.graph.service",
        "omnivia_memory.graph.repository",
        "omnivia_memory.memory.service",
        "omnivia_memory.ingestion.watcher",
        "mcp",
        "cli",
    }

    for path in files_to_check:
        content = path.read_text(encoding="utf-8")
        for marker in forbidden_import_markers:
            assert marker not in content, (path.name, marker)
