"""Compatibility facade for control-plane contract models.

Deprecated: import these from ``omnivia_core.control_plane.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`Any`, `Enum`, `annotations`, `dataclass`,
# `field`, and `ContractVersion`, which the canonical control-plane models leaf
# imports from the knowledge leaf to build `CONTROL_PLANE_CONTRACT_VERSION`) --
# names this leaf's historical namespace still has to resolve. No other error
# code is suppressed.
from omnivia_core.control_plane.models import (  # type: ignore[attr-defined,unused-ignore]
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
    Agent,
    Any,
    Approval,
    AuditEvent,
    Automation,
    Capability,
    CapabilityType,
    Connection,
    ConnectionKind,
    ConsultantAccessGrant,
    ConsultantGrantStatus,
    ContractVersion,
    ControlPlaneManifest,
    ControlPlaneRunStatus,
    Enum,
    ExecutionMode,
    ExecutionResult,
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    LocalModelInvocationRecord,
    LocalObservabilityLogRecord,
    LocalUsageLedgerEntry,
    Policy,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
    PolicyDecision,
    PolicyDecisionReason,
    PolicyDecisionRecord,
    PolicyRulePack,
    PolicyTemplate,
    RunMode,
    RunObservabilityMetrics,
    RunRecord,
    RunStepRecord,
    RunStepStatus,
    RunStepType,
    SecretMetadata,
    SecretReference,
    SecretResolutionResult,
    SecretStorageScope,
    SideEffect,
    SyncConflictStrategy,
    SyncDirection,
    SyncRule,
    TenantIsolationRule,
    Trigger,
    TriggerEventEnvelope,
    TriggerIngestionResult,
    TriggerKind,
    ValidationResult,
    WorkspaceRef,
    annotations,
    dataclass,
    field,
)
