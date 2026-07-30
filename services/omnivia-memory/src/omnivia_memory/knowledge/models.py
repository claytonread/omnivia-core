"""Compatibility facade for portable knowledge substrate contracts.

Deprecated: import these from ``omnivia_core.knowledge.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve (`Any`, `Enum`,
# `annotations`, `dataclass`, `field`). No other error code is suppressed.
from omnivia_core.knowledge.models import (  # type: ignore[attr-defined]
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    EXTENSION_MANIFEST_CONTRACT_VERSION,
    GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_CONTRACT_VERSION,
    AgentGraphContext,
    Any,
    ContractVersion,
    Enum,
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
    KnowledgeClaim,
    KnowledgeCollection,
    KnowledgeExtensionManifest,
    KnowledgeLink,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    annotations,
    dataclass,
    field,
)
