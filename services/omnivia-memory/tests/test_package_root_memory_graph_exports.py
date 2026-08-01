"""Tests for package-root memory graph export parity."""

import omnivia_memory
from omnivia_memory import (
    EvidenceGraphResponse,
    GraphPreviewResponse,
    MemoryGraphFixture,
    RetrievalTrace,
    build_memory_graph_fixture,
)


def test_package_root_exports_memory_graph_contracts() -> None:
    """Approved memory graph contracts remain importable from the package root."""
    fixture: MemoryGraphFixture = build_memory_graph_fixture()
    graph_preview = fixture["graph_preview"]
    evidence_graph = fixture["evidence_graph"]
    trace = RetrievalTrace(
        id="trace-001",
        workspace_id="workspace-001",
        query="memory graph",
        mode="hybrid",
        matched_fact_ids=["fact-001"],
        matched_segment_ids=["segment-001"],
        rank_scores={"fact-001": 0.98},
        timing={"search_ms": 12},
        resource_indicators={"segments_examined": 1},
        warnings=["package-root parity"],
        generated_at="2026-06-09T00:00:00Z",
    )

    assert isinstance(graph_preview, GraphPreviewResponse)
    assert isinstance(evidence_graph, EvidenceGraphResponse)
    assert trace.matched_segment_ids == ["segment-001"]


def test_package_root_keeps_runtime_service_exports_out() -> None:
    """Export parity must not reopen the old runtime-heavy package root."""
    forbidden = {
        "GraphService",
        "Database",
        "get_database",
        "WorkspaceService",
        "SearchService",
        "IngestionPipeline",
        "FileScanner",
    }

    assert forbidden.isdisjoint(set(omnivia_memory.__all__))
