"""Compatibility facade for the in-code memory graph fixtures.

Deprecated: import these from ``omnivia_core.memory_graph.fixtures`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `TypedDict`, `annotations`, and the sixteen contract names the
# canonical fixture builder imports from its sibling `models` leaf. No other
# error code is suppressed.
from omnivia_core.memory_graph.fixtures import (  # type: ignore[attr-defined]
    FIXTURE_TIME,
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewKind,
    GraphPreviewNode,
    GraphPreviewResponse,
    GraphPreviewState,
    MemoryEntity,
    MemoryFact,
    MemoryFactStatus,
    MemoryGraphFixture,
    MemorySegment,
    MemorySegmentKind,
    MemorySource,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
    SourceRef,
    TypedDict,
    annotations,
    build_memory_graph_fixture,
)
