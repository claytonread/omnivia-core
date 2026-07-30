"""Compatibility facade for App Manifest data models.

Deprecated: import these from ``omnivia_core.app_manifest.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.app_manifest.models import (
    AppManifest,
    AppState,
    DataSource,
    Enum,
    List,
    ProvenanceRequirement,
    ValidationResult,
    dataclass,
    field,
)
