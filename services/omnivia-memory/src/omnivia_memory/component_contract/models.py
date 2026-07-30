"""Compatibility facade for Component Contract data models.

Deprecated: import these from ``omnivia_core.component_contract.models``
instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

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
    Enum,
    List,
    Optional,
    PermissionPolicy,
    ProvenanceBehavior,
    ProvenanceRequirement,
    ValidationResult,
    dataclass,
    field,
)
