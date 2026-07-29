"""Canonical portable component contracts and validators."""

from __future__ import annotations

# ruff: noqa: I001, RUF022
# RUF022 would resort __all__ alphabetically. This barrel groups
# component_contract.models exports first and component_contract.validation
# exports last so ownership of each name is visible at a glance; resorting
# would obscure that grouping.

from omnivia_core.component_contract.models import (
    AgentAction,
    AgentBackedComponentContract,
    AgentBehavior,
    AgentRunRecord,
    AgentRunStatus,
    ApprovalPolicy,
    AuditRequirement,
    ComponentAIMode,
    ComponentConnectorScope,
    ComponentContract,
    ComponentDataSource,
    ComponentFamily,
    ComponentGraphScope,
    ComponentInput,
    ComponentOutput,
    ComponentOutputType,
    ComponentPermission,
    ComponentRunMode,
    ComponentSafetyLevel,
    PermissionPolicy,
    ProvenanceBehavior,
    ProvenanceRequirement,
    ValidationResult,
)
from omnivia_core.component_contract.validation import (
    ComponentContractValidationError,
    validate_agent_run_record,
    validate_component_contract,
)

__all__ = [
    "AgentAction",
    "AgentBackedComponentContract",
    "AgentBehavior",
    "AgentRunRecord",
    "AgentRunStatus",
    "ApprovalPolicy",
    "AuditRequirement",
    "ComponentAIMode",
    "ComponentConnectorScope",
    "ComponentContract",
    "ComponentDataSource",
    "ComponentFamily",
    "ComponentGraphScope",
    "ComponentInput",
    "ComponentOutput",
    "ComponentOutputType",
    "ComponentPermission",
    "ComponentRunMode",
    "ComponentSafetyLevel",
    "PermissionPolicy",
    "ProvenanceBehavior",
    "ProvenanceRequirement",
    "ValidationResult",
    "ComponentContractValidationError",
    "validate_agent_run_record",
    "validate_component_contract",
]
