"""Compatibility facade for App Manifest validation.

Deprecated: import ``AppManifestValidationError`` / ``validate_app_manifest``
from ``omnivia_core.app_manifest.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.app_manifest.validation import (
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
