"""App Shell host/body bridge contract data models."""

# ruff: noqa: UP006, UP035 -- canonical port preserves the legacy
# `typing.List` spelling verbatim; do not let pyupgrade modernize it.

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class AppShellRuntimeState(Enum):
    """Runtime state owned by the Platform App Shell host."""

    LOADING = "loading"
    EMPTY = "empty"
    ERROR = "error"
    MISSING_CONNECTOR = "missing-connector"
    MISSING_PERMISSION = "missing-permission"
    AI_RUNNING = "ai-running"
    READY = "ready"


@dataclass
class AppShellSource:
    """A source/provenance reference surfaced through the App Shell bridge."""

    name: str
    description: str = ""


@dataclass
class ValidationResult:
    """Result of validating an App Shell bridge contract object."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AppShellHostContext:
    """Host context the Platform App Shell host consumes.

    Attributes:
        app_id: Unique identifier of the App being hosted.
        app_name: Human-readable App name shown in host chrome.
        entity_label: Label of the focused entity (e.g. breadcrumb leaf).
        runtime_state: Current App Shell host runtime state.
        permissions_summary: Human-readable summary for the permissions
            indicator.
        sources: Source/provenance references for the Sources action.
        last_updated: Human-readable "Updated" timestamp text.
        host_command_ids: IDs of host commands exposed by the host toolbar.
        validation: Validation result metadata.
    """

    app_id: str
    app_name: str
    entity_label: str
    runtime_state: AppShellRuntimeState
    permissions_summary: str = ""
    sources: List[AppShellSource] = field(default_factory=list)
    last_updated: str = ""
    host_command_ids: List[str] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=lambda: ValidationResult(is_valid=True))


@dataclass
class AppShellBodyDescriptor:
    """Body descriptor an App body declares for the host body slot.

    Attributes:
        app_id: Unique identifier of the App owning the body.
        body_id: Unique identifier of the body within the App.
        source_count: Number of sources backing the body.
        sources: Source/provenance references backing the body.
        components: Component IDs used by the body.
        citations: Citation/provenance summary references.
        degraded_component_ids: Component IDs degraded when the host is in
            the missing-connector state. Must be a subset of components.
        validation: Validation result metadata.
    """

    app_id: str
    body_id: str
    source_count: int = 0
    sources: List[AppShellSource] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    degraded_component_ids: List[str] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=lambda: ValidationResult(is_valid=True))
