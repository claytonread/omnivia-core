"""Compatibility facade for App Manifest validation.

Deprecated: import ``AppManifestValidationError`` / ``validate_app_manifest``
from ``omnivia_core.app_manifest.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and sibling-imported bindings (`Any`, `Dict`, and the contract classes the
# canonical validation module imports from its sibling `models`) -- names this
# leaf's historical namespace still has to resolve. No other code is suppressed.
from omnivia_core.app_manifest.validation import (  # type: ignore[attr-defined,unused-ignore]
    Any,
    AppManifest,
    AppManifestValidationError,
    AppState,
    DataSource,
    Dict,
    ProvenanceRequirement,
    ValidationResult,
    validate_app_manifest,
)
