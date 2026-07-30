"""Compatibility facade for knowledge validation helpers.

Deprecated: import these from ``omnivia_core.knowledge.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `Any`, `annotations`, the twenty-three contract names the canonical
# validator imports from its sibling `models` leaf, the nine normalizers it
# imports from `normalize`, and `ValidationResult`/`scan_sensitive_fields` from
# the shared validation primitive. No other error code is suppressed.
from omnivia_core.knowledge.validation import (  # type: ignore[attr-defined]
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    EXTENSION_MANIFEST_CONTRACT_VERSION,
    GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_CONTRACT_VERSION,
    MAX_LABEL_LENGTH,
    MAX_QUOTE_PREVIEW_LENGTH,
    SCRIPT_LIKE_MARKERS,
    AgentGraphContext,
    Any,
    ContractVersion,
    GraphConfidence,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphReviewStatus,
    GraphSensitivity,
    KnowledgeClaim,
    KnowledgeCollection,
    KnowledgeExtensionManifest,
    KnowledgeLink,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    ValidationResult,
    annotations,
    check_contract_version_compatibility,
    normalize_extension_value,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
    scan_sensitive_fields,
    summarize_confidence,
    summarize_review_status,
    summarize_sensitivity,
    validate_agent_graph_context,
    validate_graph_edge,
    validate_graph_fragment,
    validate_graph_node,
    validate_knowledge_claim,
    validate_knowledge_collection,
    validate_knowledge_extension_manifest,
    validate_knowledge_link,
    validate_knowledge_object,
    validate_knowledge_source,
    validate_knowledge_space,
    validate_source_ref,
)
