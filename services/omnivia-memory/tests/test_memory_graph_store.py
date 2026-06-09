"""Tests for durable memory graph read storage and display assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnivia_memory.memory_graph import (
    EvidenceGraphResponse,
    GraphPreviewResponse,
    GraphPreviewState,
    IngestionGraphAdapterError,
    MemoryEntity,
    MemoryFact,
    MemoryFactStatus,
    MemoryGraphStore,
    MemoryGraphStoreError,
    MemorySegment,
    MemorySegmentKind,
    MemorySource,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
    SourceRef,
    assemble_evidence_graph,
    assemble_graph_preview,
    source_to_memory_source,
    write_ingestion_records_to_graph,
    redact_segment_preview,
    validate_evidence_graph_response,
    validate_graph_preview_response,
)
from omnivia_memory.ingestion.models import Chunk, FileType, ParseStatus, Source

TIME = "2026-06-08T00:00:00+00:00"
WORKSPACE = "workspace-durable"


def _source(uri: str = "docs/specs/memory.md") -> MemorySource:
    return MemorySource(
        id="source-durable",
        workspace_id=WORKSPACE,
        type=MemorySourceType.FILE,
        uri=uri,
        title="Durable Memory Spec",
        owner_ref="Core",
        freshness=MemorySourceFreshness.FRESH,
        status=MemorySourceStatus.READY,
        checksum="sha256:durable",
        created_at=TIME,
        updated_at=TIME,
    )


def _segment() -> MemorySegment:
    return MemorySegment(
        id="segment-durable",
        source_id="source-durable",
        workspace_id=WORKSPACE,
        kind=MemorySegmentKind.TEXT,
        label="Overview",
        span={"lines": [1, 8]},
        text_preview="Durable records back the live read APIs.",
        checksum="sha256:segment-durable",
        parser="markdown",
        parser_settings_ref="default-v1",
        created_at=TIME,
    )


def _source_ref() -> SourceRef:
    return SourceRef(
        source_id="source-durable",
        segment_id="segment-durable",
        span={"lines": [1, 8]},
        quote_preview="Durable records back the live read APIs.",
        confidence="extracted",
    )


def _entities() -> list[MemoryEntity]:
    return [
        MemoryEntity(
            id="entity-core",
            workspace_id=WORKSPACE,
            type="module",
            canonical_name="Core",
            aliases=["omnivia-core"],
            confidence="extracted",
            source_refs=[_source_ref()],
            created_at=TIME,
            updated_at=TIME,
        ),
        MemoryEntity(
            id="entity-store",
            workspace_id=WORKSPACE,
            type="contract",
            canonical_name="Durable Store",
            aliases=["graph store"],
            confidence="inferred",
            source_refs=[_source_ref()],
            created_at=TIME,
            updated_at=TIME,
        ),
    ]


def _facts() -> list[MemoryFact]:
    return [
        MemoryFact(
            id="fact-owns",
            workspace_id=WORKSPACE,
            subject_id="entity-core",
            predicate="owns",
            object_id="entity-store",
            confidence="extracted",
            source_refs=[_source_ref()],
            valid_from=TIME,
            status=MemoryFactStatus.APPROVED,
            created_at=TIME,
            updated_at=TIME,
        ),
        MemoryFact(
            id="fact-missing",
            workspace_id=WORKSPACE,
            subject_id="entity-store",
            predicate="may_support",
            object_value="future preview index",
            confidence="ambiguous",
            source_refs=[],
            status=MemoryFactStatus.PROPOSED,
            created_at=TIME,
            updated_at=TIME,
        ),
    ]


def _seeded_store(root: Path) -> MemoryGraphStore:
    store = MemoryGraphStore(root)
    store.save_source(_source())
    store.save_segment(_segment())
    for entity in _entities():
        store.save_entity(entity)
    for fact in _facts():
        store.save_fact(fact)
    return store


def test_records_save_list_and_read_back(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path / "graph")

    assert store.has_workspace(WORKSPACE)
    assert store.get_source(WORKSPACE, "source-durable") == _source()
    assert [segment.id for segment in store.list_segments(WORKSPACE)] == ["segment-durable"]
    assert {entity.id for entity in store.list_entities(WORKSPACE)} == {
        "entity-core",
        "entity-store",
    }
    assert {fact.id for fact in store.list_facts(WORKSPACE)} == {"fact-owns", "fact-missing"}


def test_reads_are_workspace_scoped(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path / "graph")

    assert store.get_source("workspace-other", "source-durable") is None
    assert store.list_sources("workspace-other") == []
    assert store.has_workspace("workspace-other") is False


def test_list_segments_and_facts_filter_by_source(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path / "graph")

    assert store.list_segments(WORKSPACE, source_id="missing") == []
    assert [s.id for s in store.list_segments(WORKSPACE, source_id="source-durable")] == [
        "segment-durable"
    ]
    facts = store.list_facts(WORKSPACE, source_id="source-durable")
    assert [fact.id for fact in facts] == ["fact-owns"]


def test_save_rejects_absolute_source_uri(tmp_path: Path) -> None:
    store = MemoryGraphStore(tmp_path / "graph")

    with pytest.raises(MemoryGraphStoreError, match="absolute local path"):
        store.save_source(_source(uri="/Users/secret/private.md"))


def test_save_rejects_parent_traversal_source_uri(tmp_path: Path) -> None:
    store = MemoryGraphStore(tmp_path / "graph")

    with pytest.raises(MemoryGraphStoreError, match="parent traversal"):
        store.save_source(_source(uri="../private.md"))


def test_ingestion_source_maps_to_safe_relative_memory_source(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    source_path = workspace_root / "docs" / "memory.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Memory\n\nSource-backed graph.", encoding="utf-8")
    source = Source(
        id="source-local",
        workspace_id=WORKSPACE,
        path=str(source_path),
        file_type=FileType.MARKDOWN,
        size=source_path.stat().st_size,
        hash="sha256:local",
        status=ParseStatus.SUCCESS,
        created_at=TIME,
        updated_at=TIME,
    )

    graph_source = source_to_memory_source(source, workspace_root=workspace_root)

    assert graph_source.id == "source-local"
    assert graph_source.workspace_id == WORKSPACE
    assert graph_source.uri == "docs/memory.md"
    assert graph_source.title == "memory.md"
    assert graph_source.status is MemorySourceStatus.READY


def test_ingestion_source_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("private", encoding="utf-8")
    source = Source(
        id="source-outside",
        workspace_id=WORKSPACE,
        path=str(outside),
        file_type=FileType.MARKDOWN,
        status=ParseStatus.SUCCESS,
        created_at=TIME,
        updated_at=TIME,
    )

    with pytest.raises(IngestionGraphAdapterError, match="inside workspace root"):
        source_to_memory_source(source, workspace_root=workspace_root)


def test_ingestion_records_write_source_and_segments_to_graph_store(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    source_path = workspace_root / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("One\n\nTwo", encoding="utf-8")
    source = Source(
        id="source-notes",
        workspace_id=WORKSPACE,
        path=str(source_path),
        file_type=FileType.TEXT,
        size=source_path.stat().st_size,
        hash="sha256:notes",
        status=ParseStatus.SUCCESS,
        created_at=TIME,
        updated_at=TIME,
    )
    chunks = [
        Chunk(
            id="chunk-one",
            source_id=source.id,
            chunk_index=0,
            content="One",
            start_offset=0,
            end_offset=3,
            content_hash="sha256:one",
        ),
        Chunk(
            id="chunk-two",
            source_id=source.id,
            chunk_index=1,
            content="Two",
            start_offset=5,
            end_offset=8,
            content_hash="sha256:two",
        ),
    ]
    store = MemoryGraphStore(tmp_path / "graph")

    result = write_ingestion_records_to_graph(
        store=store,
        source=source,
        chunks=chunks,
        workspace_root=workspace_root,
    )

    assert result.source_id == "source-notes"
    assert result.segments_created == 2
    assert store.get_source(WORKSPACE, "source-notes").uri == "notes.txt"
    assert [segment.id for segment in store.list_segments(WORKSPACE)] == [
        "chunk-one",
        "chunk-two",
    ]


def test_save_rejects_sensitive_segment_fields(tmp_path: Path) -> None:
    store = MemoryGraphStore(tmp_path / "graph")
    unsafe = MemorySegment(
        id="segment-unsafe",
        source_id="source-durable",
        workspace_id=WORKSPACE,
        kind=MemorySegmentKind.TEXT,
        label="Unsafe",
        span={"token": "leaked-secret"},
        parser="markdown",
        parser_settings_ref="default-v1",
        created_at=TIME,
    )

    with pytest.raises(MemoryGraphStoreError, match="sensitive"):
        store.save_segment(unsafe)


def test_save_rejects_unsafe_record_id(tmp_path: Path) -> None:
    store = MemoryGraphStore(tmp_path / "graph")
    bad = MemoryEntity(
        id="../escape",
        workspace_id=WORKSPACE,
        type="module",
        canonical_name="Escape",
        aliases=[],
        confidence="extracted",
        source_refs=[_source_ref()],
        created_at=TIME,
        updated_at=TIME,
    )

    with pytest.raises(MemoryGraphStoreError, match="safe file name"):
        store.save_entity(bad)


def test_assemble_graph_preview_from_records_is_valid(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path / "graph")

    preview = assemble_graph_preview(
        workspace_id=WORKSPACE,
        query="source:source-durable",
        source=store.get_source(WORKSPACE, "source-durable"),
        entities=store.list_entities(WORKSPACE),
        facts=store.list_facts(WORKSPACE),
        generated_at=TIME,
        node_limit=100,
        edge_limit=150,
    )

    assert isinstance(preview, GraphPreviewResponse)
    assert validate_graph_preview_response(preview).valid
    # source node plus two entity nodes.
    assert len(preview.nodes) == 3
    # only the entity-to-entity approved fact becomes an edge.
    assert [edge.id for edge in preview.edges] == ["edge-fact-owns"]
    assert preview.edges[0].state is GraphPreviewState.READY
    # the missing-evidence property fact is surfaced as a warning, not dropped.
    assert any("fact-missing" in warning for warning in preview.warnings)


def test_assemble_graph_preview_marks_missing_evidence_edges(tmp_path: Path) -> None:
    store = MemoryGraphStore(tmp_path / "graph")
    store.save_source(_source())
    for entity in _entities():
        store.save_entity(entity)
    store.save_fact(
        MemoryFact(
            id="fact-edge-missing",
            workspace_id=WORKSPACE,
            subject_id="entity-core",
            predicate="relates_to",
            object_id="entity-store",
            confidence="inferred",
            source_refs=[],
            status=MemoryFactStatus.PROPOSED,
            created_at=TIME,
            updated_at=TIME,
        )
    )

    preview = assemble_graph_preview(
        workspace_id=WORKSPACE,
        query="source:source-durable",
        source=store.get_source(WORKSPACE, "source-durable"),
        entities=store.list_entities(WORKSPACE),
        facts=store.list_facts(WORKSPACE),
        generated_at=TIME,
        node_limit=100,
        edge_limit=150,
    )

    assert preview.edges[0].state is GraphPreviewState.MISSING_EVIDENCE
    assert preview.edges[0].confidence == "inferred"


def test_assemble_evidence_graph_collects_citations(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path / "graph")

    evidence = assemble_evidence_graph(
        workspace_id=WORKSPACE,
        answer_id="answer-durable",
        query="What owns the durable store?",
        source=store.get_source(WORKSPACE, "source-durable"),
        entities=store.list_entities(WORKSPACE),
        facts=store.list_facts(WORKSPACE),
        generated_at=TIME,
    )

    assert isinstance(evidence, EvidenceGraphResponse)
    assert validate_evidence_graph_response(evidence).valid
    assert evidence.answer_id == "answer-durable"
    assert [citation.source_id for citation in evidence.citations] == ["source-durable"]


def test_redact_segment_preview_truncates_long_text(tmp_path: Path) -> None:
    long_segment = MemorySegment(
        id="segment-long",
        source_id="source-durable",
        workspace_id=WORKSPACE,
        kind=MemorySegmentKind.TEXT,
        label="Long",
        text_preview="x" * 900,
        parser="markdown",
        parser_settings_ref="default-v1",
        created_at=TIME,
    )

    redacted = redact_segment_preview(long_segment, max_chars=500)

    assert len(redacted["text_preview"]) == 500
