"""Canonical portable module-manifest contracts and validators."""

from __future__ import annotations

from omnivia_core.module_manifest.models import (
    Entrypoint,
    Integrity,
    ModuleKind,
    ModuleManifest,
    Permission,
    PublishedTarget,
)
from omnivia_core.module_manifest.validation import (
    ModuleManifestValidationError,
    validate_module_manifest,
)

__all__ = [
    "Entrypoint",
    "Integrity",
    "ModuleKind",
    "ModuleManifest",
    "ModuleManifestValidationError",
    "Permission",
    "PublishedTarget",
    "validate_module_manifest",
]
