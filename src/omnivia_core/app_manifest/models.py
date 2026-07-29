"""App Manifest data models."""

# ruff: noqa: UP006, UP035 -- canonical port preserves the legacy
# `typing.List` spelling verbatim; do not let pyupgrade modernize it.

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class AppState(Enum):
    """App lifecycle state."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"


@dataclass
class DataSource:
    """A required data source for the App."""

    source_id: str
    display_name: str = ""
    description: str = ""


@dataclass
class ProvenanceRequirement:
    """Source/provenance requirements for the App."""

    require_signed: bool = False
    require_audit_log: bool = False
    allowed_source_types: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Result of validating an App Manifest."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AppManifest:
    """Manifest definition for an App.

    Attributes:
        manifest_version: Semantic version of the manifest schema.
        app_id: Unique identifier for the App (e.g., "com.acme.expense-app").
        display_name: Human-readable name shown in the UI.
        app_version: Semantic version of the App.
        required_module_id: Module that must be installed for this App to run.
        compatible_harness_versions: Harness version ranges this App supports.
        compatible_api_versions: API version ranges this App supports.
        required_components: List of required Component IDs.
        required_data_sources: List of required data sources.
        requested_permissions: List of permission names requested by the App.
        provenance: Provenance/source requirements.
        state: Current lifecycle state.
        validation: Validation result metadata.
    """

    manifest_version: str
    app_id: str
    display_name: str
    app_version: str
    required_module_id: str
    compatible_harness_versions: List[str] = field(default_factory=list)
    compatible_api_versions: List[str] = field(default_factory=list)
    required_components: List[str] = field(default_factory=list)
    required_data_sources: List[DataSource] = field(default_factory=list)
    requested_permissions: List[str] = field(default_factory=list)
    provenance: ProvenanceRequirement = field(default_factory=lambda: ProvenanceRequirement())
    state: AppState = AppState.DRAFT
    validation: ValidationResult = field(default_factory=lambda: ValidationResult(is_valid=True))
