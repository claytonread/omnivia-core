"""Compatibility facade for memory graph validation helpers.

Deprecated: import these from ``omnivia_core.memory_graph.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# `memory_graph.store` and the hybrid `memory_graph` barrel both take
# `ValidationResult` from here, so it has to stay explicitly exported.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `annotations`, the ten contract names plus the `Confidence` alias the
# canonical validator imports from its sibling `models` leaf, and
# `ValidationResult` from the shared validation primitive. No other error code is
# suppressed.
from omnivia_core.memory_graph.validation import (  # type: ignore[attr-defined]
    CONFIDENCE_BUCKETS,
    Confidence,
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewNode,
    GraphPreviewResponse,
    MemoryEntity,
    MemoryFact,
    MemorySegment,
    MemorySource,
    SourceRef,
    ValidationResult,
    annotations,
    validate_evidence_graph_response,
    validate_graph_preview_response,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
)
