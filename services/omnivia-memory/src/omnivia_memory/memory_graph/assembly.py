"""Compatibility facade for memory graph display assembly helpers.

Deprecated: import these from ``omnivia_core.memory_graph.assembly`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `annotations` and the thirteen contract names the canonical assembler
# imports from its sibling `models` leaf. No other error code is suppressed.
from omnivia_core.memory_graph.assembly import (  # type: ignore[attr-defined]
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
    annotations,
    assemble_evidence_graph,
    assemble_graph_preview,
    redact_segment_preview,
)
