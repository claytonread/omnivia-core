"""Compatibility facade for control-plane import adapters.

Deprecated: import these from ``omnivia_core.control_plane.imports`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`Any`, `annotations`, `dataclass`, `field`,
# the `hashlib`/`json`/`re` module bindings, and the ten control-plane contract
# classes the canonical imports leaf imports from its sibling `models`) -- names
# this leaf's historical namespace still has to resolve. No other error code is
# suppressed.
from omnivia_core.control_plane.imports import (  # type: ignore[attr-defined]
    Any,
    Capability,
    CapabilityType,
    CatalogueArtifactVerification,
    Connection,
    ConnectionKind,
    ImportedCandidateSet,
    ImportRecord,
    ImportSourceChange,
    ImportSourceProtocol,
    ImportSpecValidation,
    LifecycleState,
    SideEffect,
    Trigger,
    TriggerKind,
    annotations,
    dataclass,
    detect_import_source_change,
    field,
    hashlib,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    json,
    re,
    validate_asyncapi_import_spec,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    verify_catalogue_artifacts,
)
