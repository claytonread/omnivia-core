"""Canonical memory_graph package (public-safe contracts only)."""

from __future__ import annotations

from omnivia_core._shared.validation import ValidationResult
from omnivia_core.memory_graph.assembly import (
    assemble_evidence_graph,
    assemble_graph_preview,
    redact_segment_preview,
)
from omnivia_core.memory_graph.fixtures import (
    FIXTURE_TIME,
    MemoryGraphFixture,
    build_memory_graph_fixture,
)
from omnivia_core.memory_graph.models import (
    Confidence,
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
    RetrievalTrace,
    SourceRef,
)
from omnivia_core.memory_graph.validation import (
    validate_evidence_graph_response,
    validate_graph_preview_response,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
)

__all__ = [  # noqa: RUF022 -- preserve the historical compatibility order
    "Confidence",
    "EvidenceGraphResponse",
    "FIXTURE_TIME",
    "GraphPreviewEdge",
    "GraphPreviewKind",
    "GraphPreviewNode",
    "GraphPreviewResponse",
    "GraphPreviewState",
    "MemoryEntity",
    "MemoryFact",
    "MemoryFactStatus",
    "MemoryGraphFixture",
    "MemorySegment",
    "MemorySegmentKind",
    "MemorySource",
    "MemorySourceFreshness",
    "MemorySourceStatus",
    "MemorySourceType",
    "RetrievalTrace",
    "SourceRef",
    "ValidationResult",
    "assemble_evidence_graph",
    "assemble_graph_preview",
    "build_memory_graph_fixture",
    "redact_segment_preview",
    "validate_evidence_graph_response",
    "validate_graph_preview_response",
    "validate_memory_entity",
    "validate_memory_fact",
    "validate_memory_segment",
    "validate_memory_source",
]
