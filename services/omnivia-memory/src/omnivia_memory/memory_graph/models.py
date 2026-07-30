"""Compatibility facade for backend-neutral memory graph contracts.

Deprecated: import these from ``omnivia_core.memory_graph.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve (`Any`, `Enum`,
# `TypeAlias`, `annotations`, `dataclass`, `field`). No other error code is
# suppressed.
from omnivia_core.memory_graph.models import (  # type: ignore[attr-defined]
    Any,
    Confidence,
    Enum,
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
    TypeAlias,
    annotations,
    dataclass,
    field,
)
