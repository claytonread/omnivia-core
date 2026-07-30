"""Compatibility facade for Component Contract data models.

Deprecated: import these from ``omnivia_core.component_contract.models``
instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`Enum`, `List`, `Optional`, `dataclass`, `field`) -- names the
# canonical module's own imports left at its module scope and that this leaf's
# historical namespace still has to resolve. No other code is suppressed.
from omnivia_core.component_contract.models import (  # type: ignore[attr-defined]
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
