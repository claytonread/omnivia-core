"""Module Manifest contract for installable OmniVia Modules."""

from omnivia_memory.module_manifest.models import (
    Entrypoint,
    Integrity,
    ModuleKind,
    ModuleManifest,
    Permission,
    PublishedTarget,
)
from omnivia_memory.module_manifest.validation import (
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