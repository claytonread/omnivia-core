"""Consumers, their declared dependencies, and exact version bindings.

Section 15 of the spec: build/deployment compatibility uses a supported
range, but runtime execution uses an exact binding -- "Every production run
MUST record the exact `model_version_id`...it used. A supported range alone
is insufficient runtime evidence." :class:`ExactBinding` is that record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omnivia_core.semantic_registry.errors import SemanticErrorCode, require


class ConsumerKind(str, Enum):
    """The initial consumer kinds (spec 15.1)."""

    APP = "app"
    WORKFLOW = "workflow"
    INSIGHT = "insight"
    CONNECTOR = "connector"
    AGENT_CONTRACT = "agent_contract"
    POLICY = "policy"
    PROJECTION = "projection"
    EXTERNAL_API = "external_api"


class DependencyMode(str, Enum):
    """How strongly a consumer relies on the element it references (spec 15.1)."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    DISPLAY_ONLY = "display_only"
    INFERRED = "inferred"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


@dataclass(frozen=True, slots=True)
class Consumer:
    """A registered semantic consumer: `semantic_consumers` (spec 10.8)."""

    consumer_id: str
    kind: ConsumerKind
    owner: str
    deployment_identity: str
    criticality: str = "standard"
    lifecycle_state: str = "active"

    def __post_init__(self) -> None:
        _require_id("consumer_id", self.consumer_id)
        _require_id("owner", self.owner)
        _require_id("deployment_identity", self.deployment_identity)


@dataclass(frozen=True, slots=True)
class ConsumerDependency:
    """One element/action a consumer declares it depends on (`consumer_dependencies`)."""

    consumer_id: str
    model_id: str
    element_id: str
    dependency_mode: DependencyMode
    usage_location: str
    fallback_behaviour: str | None = None

    def __post_init__(self) -> None:
        _require_id("consumer_id", self.consumer_id)
        _require_id("model_id", self.model_id)
        _require_id("element_id", self.element_id)
        _require_id("usage_location", self.usage_location)


@dataclass(frozen=True, slots=True)
class ExactBinding:
    """The exact `model_version_id` one consumer deployment used at runtime
    (`consumer_version_bindings`, spec 15.3).
    """

    consumer_id: str
    model_version_id: str
    binding_state: str = "bound"
    action_contract_digest: str | None = None

    def __post_init__(self) -> None:
        _require_id("consumer_id", self.consumer_id)
        _require_id("model_version_id", self.model_version_id)


__all__ = [
    "Consumer",
    "ConsumerDependency",
    "ConsumerKind",
    "DependencyMode",
    "ExactBinding",
]
