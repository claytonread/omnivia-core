"""Compatibility facade for control-plane validation.

Deprecated: import ``validate_control_plane_manifest`` / ``manifest_from_dict``
/ ``compile_policy_expression`` / ``ControlPlaneValidationError`` and the
bounded policy constants from ``omnivia_core.control_plane.validation``
instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`Any`, `Enum`, `Mapping`, `TypeVar`,
# `annotations`, `datetime`, the `re` module binding, the contract classes and
# the two version constants this leaf's sibling `models` owns, and
# `scan_sensitive_fields` / `check_contract_version_compatibility`, which the
# canonical validation module imports from the shared-validation and
# knowledge-validation leaves) -- names this leaf's historical namespace still
# has to resolve. No other error code is suppressed.
from omnivia_core.control_plane.validation import (  # type: ignore[attr-defined]
    APPROVAL_ESCALATION_STATES,
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
    DANGEROUS_SIDE_EFFECTS,
    EXTRA_SENSITIVE_KEYS,
    POLICY_ATTRIBUTE_NUMERIC_OPERATORS,
    POLICY_ATTRIBUTE_OPERATORS,
    POLICY_ATTRIBUTE_PRESENCE_OPERATORS,
    POLICY_ATTRIBUTE_SCOPES,
    POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS,
    POLICY_ATTRIBUTE_VALUE_OPERATORS,
    POLICY_ATTRIBUTE_VALUES_OPERATORS,
    POLICY_EXPRESSION_BOOLEAN_OPS,
    POLICY_EXPRESSION_COMPARISON_OPERATORS,
    POLICY_EXPRESSION_MAX_DEPTH,
    POLICY_EXPRESSION_MAX_NODES,
    POLICY_EXPRESSION_NUMERIC_COMPARISONS,
    POLICY_EXPRESSION_OPS,
    POLICY_EXPRESSION_PARSE_MAX_DEPTH,
    POLICY_EXPRESSION_RAW_FIELDS,
    POLICY_EXPRESSION_SECRET_MARKERS,
    POLICY_EXPRESSION_SOURCE_MAX_LENGTH,
    RRULE_FREQUENCIES,
    RRULE_WEEKDAYS,
    STABLE_ID_PATTERN,
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
    ControlPlaneManifest,
    ControlPlaneRunStatus,
    ControlPlaneValidationError,
    Enum,
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    Mapping,
    Policy,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
    PolicyDecision,
    PolicyRulePack,
    PolicyTemplate,
    RunMode,
    RunRecord,
    SecretMetadata,
    SecretReference,
    SecretStorageScope,
    SideEffect,
    SyncConflictStrategy,
    SyncDirection,
    SyncRule,
    T,
    TenantIsolationRule,
    Trigger,
    TriggerKind,
    TypeVar,
    ValidationResult,
    WorkspaceRef,
    annotations,
    check_contract_version_compatibility,
    compile_policy_expression,
    datetime,
    manifest_from_dict,
    re,
    scan_sensitive_fields,
    validate_control_plane_manifest,
)
