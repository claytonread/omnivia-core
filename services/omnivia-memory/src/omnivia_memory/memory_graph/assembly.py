"""Assemble bounded display contracts from durable memory graph records.

These helpers are backend-neutral: given source, segment, entity and fact
records (for example read from the durable store), they build the same
`GraphPreviewResponse` / `EvidenceGraphResponse` display contracts that the
static fixtures produce. Keeping this assembly in Core means Platform stays a
thin runtime adapter and Dev keeps consuming one contract surface.

Assembly is read-only and source-grounded:

- Confidence values pass through unchanged, so ``inferred`` and ``ambiguous``
  stay distinguishable from ``extracted``.
- Facts whose evidence is missing are surfaced as an explicit warning and a
  ``missing_evidence`` edge state rather than being silently dropped.
"""

from __future__ import annotations

from omnivia_memory.memory_graph.models import (
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
    MemorySource,
    MemorySourceStatus,
    SourceRef,
)

_SOURCE_STATE = {
    MemorySourceStatus.READY: GraphPreviewState.READY,
    MemorySourceStatus.PROCESSING: GraphPreviewState.PROPOSED,
    MemorySourceStatus.PENDING: GraphPreviewState.PROPOSED,
    MemorySourceStatus.FAILED: GraphPreviewState.FAILED,
    MemorySourceStatus.DELETED: GraphPreviewState.STALE,
}

_FACT_STATE = {
    MemoryFactStatus.APPROVED: GraphPreviewState.READY,
    MemoryFactStatus.PROPOSED: GraphPreviewState.PROPOSED,
    MemoryFactStatus.REJECTED: GraphPreviewState.STALE,
    MemoryFactStatus.SUPERSEDED: GraphPreviewState.STALE,
    MemoryFactStatus.UNKNOWN: GraphPreviewState.PROPOSED,
}


def _source_node_id(source_id: str) -> str:
    return f"node-source-{source_id}"


def _entity_node_id(entity_id: str) -> str:
    return f"node-entity-{entity_id}"


def _build_nodes(
    source: MemorySource | None,
    entities: list[MemoryEntity],
) -> tuple[list[GraphPreviewNode], dict[str, str]]:
    """Return display nodes plus an entity-id -> node-id map."""

    nodes: list[GraphPreviewNode] = []
    if source is not None:
        nodes.append(
            GraphPreviewNode(
                id=_source_node_id(source.id),
                source_ref=source.id,
                label=source.title,
                type=source.type.value,
                kind=GraphPreviewKind.SOURCE,
                state=_SOURCE_STATE.get(source.status, GraphPreviewState.READY),
                source_refs=[SourceRef(source_id=source.id)],
            )
        )
    entity_node_ids: dict[str, str] = {}
    for entity in entities:
        node_id = _entity_node_id(entity.id)
        entity_node_ids[entity.id] = node_id
        nodes.append(
            GraphPreviewNode(
                id=node_id,
                entity_ref=entity.id,
                label=entity.canonical_name,
                type=entity.type,
                kind=GraphPreviewKind.ENTITY,
                state=GraphPreviewState.READY,
                confidence=entity.confidence,
                source_refs=list(entity.source_refs),
            )
        )
    return nodes, entity_node_ids


def _build_edges(
    facts: list[MemoryFact],
    entity_node_ids: dict[str, str],
) -> tuple[list[GraphPreviewEdge], list[str]]:
    """Return relationship edges plus explicit missing-evidence warnings."""

    edges: list[GraphPreviewEdge] = []
    warnings: list[str] = []
    for fact in facts:
        if not fact.source_refs:
            warnings.append(
                f"fact {fact.id} has no source evidence; shown as missing-evidence warning"
            )
        source_node = entity_node_ids.get(fact.subject_id)
        target_node = (
            entity_node_ids.get(fact.object_id) if fact.object_id is not None else None
        )
        if source_node is None or target_node is None:
            # Property assertions and facts that do not connect two graphed
            # entities are not drawn as edges; their evidence state is still
            # reported through warnings above.
            continue
        state = (
            GraphPreviewState.MISSING_EVIDENCE
            if not fact.source_refs
            else _FACT_STATE.get(fact.status, GraphPreviewState.READY)
        )
        edges.append(
            GraphPreviewEdge(
                id=f"edge-{fact.id}",
                source=source_node,
                target=target_node,
                label=fact.predicate,
                type=fact.predicate,
                state=state,
                confidence=fact.confidence,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                source_refs=list(fact.source_refs),
            )
        )
    return edges, warnings


def _collect_citations(
    entities: list[MemoryEntity],
    facts: list[MemoryFact],
) -> list[SourceRef]:
    citations: list[SourceRef] = []
    seen: set[tuple[str, str | None]] = set()
    records: list[MemoryEntity | MemoryFact] = [*entities, *facts]
    for ref in (ref for record in records for ref in record.source_refs):
        key = (ref.source_id, ref.segment_id)
        if key in seen:
            continue
        seen.add(key)
        citations.append(ref)
    return citations


def assemble_graph_preview(
    *,
    workspace_id: str,
    query: str,
    source: MemorySource | None,
    entities: list[MemoryEntity],
    facts: list[MemoryFact],
    generated_at: str,
    node_limit: int,
    edge_limit: int,
) -> GraphPreviewResponse:
    """Build a `GraphPreviewResponse` from durable records."""

    nodes, entity_node_ids = _build_nodes(source, entities)
    edges, warnings = _build_edges(facts, entity_node_ids)
    return GraphPreviewResponse(
        workspace_id=workspace_id,
        query=query,
        nodes=nodes,
        edges=edges,
        warnings=warnings,
        limits={"nodes": node_limit, "edges": edge_limit},
        generated_at=generated_at,
    )


def assemble_evidence_graph(
    *,
    workspace_id: str,
    answer_id: str | None,
    query: str,
    source: MemorySource | None,
    entities: list[MemoryEntity],
    facts: list[MemoryFact],
    generated_at: str,
) -> EvidenceGraphResponse:
    """Build an `EvidenceGraphResponse` from durable records."""

    nodes, entity_node_ids = _build_nodes(source, entities)
    edges, warnings = _build_edges(facts, entity_node_ids)
    citations = _collect_citations(entities, facts)
    if not citations:
        warnings.append("evidence graph has no source citations")
    return EvidenceGraphResponse(
        workspace_id=workspace_id,
        answer_id=answer_id,
        query=query,
        nodes=nodes,
        edges=edges,
        citations=citations,
        warnings=warnings,
        generated_at=generated_at,
    )


def redact_segment_preview(
    segment: MemorySegment,
    *,
    max_chars: int = 500,
) -> dict[str, object]:
    """Return a segment dict with its text preview truncated for UI safety."""

    data = segment.to_dict()
    preview = data.get("text_preview")
    if isinstance(preview, str) and len(preview) > max_chars:
        data["text_preview"] = preview[:max_chars]
    return data
