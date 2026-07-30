"""Compatibility facade for knowledge normalization helpers.

Deprecated: import these from ``omnivia_core.knowledge.normalize`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `annotations`, the `PurePosixPath` class, the `re`/`unicodedata`
# module bindings, and the three bounded-vocabulary frozensets the canonical
# normalizer imports from its sibling `models` leaf. No other error code is
# suppressed.
from omnivia_core.knowledge.normalize import (  # type: ignore[attr-defined]
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    PurePosixPath,
    annotations,
    normalize_extension_value,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_graph_node_kind,
    normalize_graph_relation,
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_object_kind,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
    re,
    unicodedata,
)
