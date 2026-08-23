"""Compatibility facade for App Manifest data models.

Deprecated: import these from ``omnivia_core.app_manifest.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`Enum`, `List`, `dataclass`, `field`) -- names the canonical module's
# own imports left at its module scope and that this leaf's historical namespace
# still has to resolve. No other error code is suppressed.
from omnivia_core.app_manifest.models import (  # type: ignore[attr-defined,unused-ignore]
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
