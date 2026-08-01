"""Reusable in-code fixtures for memory graph contract tests and demos."""

from __future__ import annotations

from typing import TypedDict

from omnivia_core.memory_graph.models import (
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewKind,
    GraphPreviewNode,
    GraphPreviewResponse,
    GraphPreviewState,
    MemoryEntity,
    MemoryFact,
    MemoryFactStatus,
    MemorySegment,
    MemorySegmentKind,
    MemorySource,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
    SourceRef,
)

FIXTURE_TIME = "2026-06-07T00:00:00+00:00"


class MemoryGraphFixture(TypedDict):
    """Typed fixture bundle for contract tests."""

    source: MemorySource
    segments: list[MemorySegment]
    entities: list[MemoryEntity]
    facts: list[MemoryFact]
    graph_preview: GraphPreviewResponse
    evidence_graph: EvidenceGraphResponse


def build_memory_graph_fixture() -> MemoryGraphFixture:
    """Build a source-backed fixture with temporal and warning cases."""

    source = MemorySource(
        id="source-001",
        workspace_id="workspace-001",
        type=MemorySourceType.FILE,
        uri="docs/adr/001-memory.md",
        title="Memory ADR",
        owner_ref="Core",
        connector_ref=None,
        freshness=MemorySourceFreshness.FRESH,
        status=MemorySourceStatus.READY,
        checksum="sha256:source",
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    segment_a = MemorySegment(
        id="segment-001",
        source_id=source.id,
        workspace_id=source.workspace_id,
        kind=MemorySegmentKind.TEXT,
        label="Purpose",
        span={"lines": [1, 12]},
        text_preview="Memory facts must cite source evidence.",
        checksum="sha256:segment-a",
        parser="markdown",
        parser_settings_ref="default-v1",
        created_at=FIXTURE_TIME,
    )
    segment_b = MemorySegment(
        id="segment-002",
        source_id=source.id,
        workspace_id=source.workspace_id,
        kind=MemorySegmentKind.TEXT,
        label="Ownership",
        span={"lines": [14, 22]},
        text_preview="Core owns public-safe memory graph types.",
        checksum="sha256:segment-b",
        parser="markdown",
        parser_settings_ref="default-v1",
        created_at=FIXTURE_TIME,
    )
    source_ref = SourceRef(
        source_id=source.id,
        segment_id=segment_a.id,
        span={"lines": [1, 12]},
        quote_preview="Memory facts must cite source evidence.",
        confidence="extracted",
    )
    core_entity = MemoryEntity(
        id="entity-core",
        workspace_id=source.workspace_id,
        type="module",
        canonical_name="Core",
        aliases=["omnivia-core"],
        confidence="extracted",
        source_refs=[source_ref],
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    graph_entity = MemoryEntity(
        id="entity-memory-graph",
        workspace_id=source.workspace_id,
        type="contract",
        canonical_name="Memory Graph Contract",
        aliases=["memory graph"],
        confidence=0.96,
        source_refs=[source_ref],
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    fact = MemoryFact(
        id="fact-001",
        workspace_id=source.workspace_id,
        subject_id=core_entity.id,
        predicate="owns",
        object_id=graph_entity.id,
        object_value=None,
        confidence="extracted",
        source_refs=[source_ref],
        valid_from="2026-06-07T00:00:00+00:00",
        valid_to=None,
        supersedes=None,
        status=MemoryFactStatus.APPROVED,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    proposed_fact = MemoryFact(
        id="fact-002",
        workspace_id=source.workspace_id,
        subject_id=graph_entity.id,
        predicate="may_support",
        object_id=None,
        object_value="future visual graph UI",
        confidence="ambiguous",
        source_refs=[source_ref],
        valid_from=None,
        valid_to=None,
        supersedes=None,
        status=MemoryFactStatus.PROPOSED,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    missing_evidence_fact = MemoryFact(
        id="fact-003",
        workspace_id=source.workspace_id,
        subject_id=graph_entity.id,
        predicate="missing_evidence_example",
        object_id=None,
        object_value=True,
        confidence="inferred",
        source_refs=[],
        valid_from=None,
        valid_to=None,
        supersedes=None,
        status=MemoryFactStatus.PROPOSED,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )

    graph_preview = GraphPreviewResponse(
        workspace_id=source.workspace_id,
        query="source:source-001",
        nodes=[
            GraphPreviewNode(
                id="node-source-001",
                source_ref=source.id,
                label=source.title,
                type="file",
                kind=GraphPreviewKind.SOURCE,
                state=GraphPreviewState.READY,
                confidence="extracted",
                source_refs=[source_ref],
            ),
            GraphPreviewNode(
                id="node-entity-core",
                entity_ref=core_entity.id,
                label=core_entity.canonical_name,
                type=core_entity.type,
                kind=GraphPreviewKind.ENTITY,
                state=GraphPreviewState.READY,
                confidence=core_entity.confidence,
                source_refs=[source_ref],
            ),
            GraphPreviewNode(
                id="node-entity-memory-graph",
                entity_ref=graph_entity.id,
                label=graph_entity.canonical_name,
                type=graph_entity.type,
                kind=GraphPreviewKind.ENTITY,
                state=GraphPreviewState.READY,
                confidence=graph_entity.confidence,
                source_refs=[source_ref],
            ),
        ],
        edges=[
            GraphPreviewEdge(
                id="edge-fact-001",
                source="node-entity-core",
                target="node-entity-memory-graph",
                label=fact.predicate,
                type="owns",
                state=GraphPreviewState.READY,
                confidence=fact.confidence,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                source_refs=[source_ref],
            )
        ],
        warnings=["preview limited to 100 nodes and 150 edges"],
        limits={"nodes": 100, "edges": 150},
        generated_at=FIXTURE_TIME,
    )
    evidence_graph = EvidenceGraphResponse(
        workspace_id=source.workspace_id,
        answer_id="answer-001",
        query="What owns the Memory Graph Contract?",
        nodes=graph_preview.nodes,
        edges=graph_preview.edges,
        citations=[source_ref],
        warnings=[],
        generated_at=FIXTURE_TIME,
    )

    return {
        "source": source,
        "segments": [segment_a, segment_b],
        "entities": [core_entity, graph_entity],
        "facts": [fact, proposed_fact, missing_evidence_fact],
        "graph_preview": graph_preview,
        "evidence_graph": evidence_graph,
    }
