"""Validation helpers for control-plane contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from omnivia_memory._shared.validation import scan_sensitive_fields
from omnivia_memory.control_plane.models import (
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
    Agent,
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
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    Policy,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
    PolicyDecision,
    PolicyRulePack,
    PolicyTemplate,
    RunMode,
    RunRecord,
    SecretReference,
    SecretMetadata,
    SecretStorageScope,
    SideEffect,
    SyncConflictStrategy,
    SyncDirection,
    SyncRule,
    TenantIsolationRule,
    Trigger,
    TriggerKind,
    ValidationResult,
    WorkspaceRef,
)
from omnivia_memory.knowledge import check_contract_version_compatibility


STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
EXTRA_SENSITIVE_KEYS = frozenset({"client_secret", "private_key"})
DANGEROUS_SIDE_EFFECTS = frozenset(
    {
        SideEffect.EXTERNAL_WRITE,
        SideEffect.SEND,
        SideEffect.PUBLISH,
        SideEffect.DESTRUCTIVE,
        SideEffect.FINANCIAL,
        SideEffect.ADMIN,
    }
)
RRULE_FREQUENCIES = frozenset(
    {"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
)
RRULE_WEEKDAYS = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})
APPROVAL_ESCALATION_STATES = frozenset(
    {"", "none", "pending", "assigned", "escalated", "expired", "resolved"}
)
POLICY_ATTRIBUTE_SCOPES = frozenset({"actor", "workspace"})
POLICY_ATTRIBUTE_VALUE_OPERATORS = frozenset({"equals", "not_equals"})
POLICY_ATTRIBUTE_VALUES_OPERATORS = frozenset({"in", "not_in"})
POLICY_ATTRIBUTE_PRESENCE_OPERATORS = frozenset({"exists", "not_exists"})
# Deterministic numeric comparison operators. Each compares a single attribute
# value against condition.value after both are parsed as finite numbers. This is
# bounded comparison, not a free-form expression language.
POLICY_ATTRIBUTE_NUMERIC_OPERATORS = frozenset(
    {"greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal"}
)
# Operators that read condition.value (and require condition.values to be empty).
POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS = (
    POLICY_ATTRIBUTE_VALUE_OPERATORS | POLICY_ATTRIBUTE_NUMERIC_OPERATORS
)
POLICY_ATTRIBUTE_OPERATORS = (
    POLICY_ATTRIBUTE_VALUE_OPERATORS
    | POLICY_ATTRIBUTE_VALUES_OPERATORS
    | POLICY_ATTRIBUTE_PRESENCE_OPERATORS
    | POLICY_ATTRIBUTE_NUMERIC_OPERATORS
)
# Bounded structured policy attribute expression tree. ``all``/``any``/``not``
# combine bounded child nodes and ``condition`` wraps a single
# PolicyAttributeCondition. This is not CEL or a free-form expression language:
# there is no eval, regex, imports, functions, or arbitrary expression text, and
# depth/node count are bounded so evaluation stays finite and deterministic.
POLICY_EXPRESSION_OPS = frozenset({"all", "any", "not", "condition"})
POLICY_EXPRESSION_BOOLEAN_OPS = frozenset({"all", "any", "not"})
POLICY_EXPRESSION_MAX_DEPTH = 5
POLICY_EXPRESSION_MAX_NODES = 32
# Raw free-form/CEL expression fields that must never appear on a policy or
# policy template. The bounded attribute_expression tree replaces them.
POLICY_EXPRESSION_RAW_FIELDS = frozenset(
    {"policy_expression", "cel_expression", "expression"}
)

# Bounded CEL-shaped string compiler. ``compile_policy_expression`` accepts an
# intentionally tiny, deterministic subset of CEL-looking text and returns a
# bounded :class:`PolicyAttributeExpression` tree. It is an explicit-API helper
# only: it is never wired into manifest parsing, so raw policy_expression/
# cel_expression/expression fields stay rejected. There is no eval, no regex,
# no function calls, no imports, no arithmetic, and no arbitrary identifiers.
# Maximum accepted source length (cheap guard against pathological input before
# the bounded tree validation runs).
POLICY_EXPRESSION_SOURCE_MAX_LENGTH = 1000
# Maximum parser recursion depth. This is a safety bound well above the bounded
# tree depth; the compiled tree is still validated against
# POLICY_EXPRESSION_MAX_DEPTH before it is returned.
POLICY_EXPRESSION_PARSE_MAX_DEPTH = 64
# CEL-shaped comparison operators mapped to bounded condition operators.
POLICY_EXPRESSION_COMPARISON_OPERATORS = {
    "==": "equals",
    "!=": "not_equals",
    ">": "greater_than",
    ">=": "greater_than_or_equal",
    "<": "less_than",
    "<=": "less_than_or_equal",
}
# Comparison operators whose right-hand literal must be a finite number.
POLICY_EXPRESSION_NUMERIC_COMPARISONS = frozenset({">", ">=", "<", "<="})
# Secret-looking markers rejected in attribute keys and string literals so no
# secret-bearing name or value can be smuggled through the helper. Substring
# matching is intentional: it fails closed on anything that merely looks secret.
POLICY_EXPRESSION_SECRET_MARKERS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
# Single master token pattern. Any character that does not match one of these
# named groups is unsupported syntax and fails closed during tokenization. The
# string groups forbid backslashes and newlines, so escapes are rejected;
# backticks, ``$``/``{`` template interpolation, ``@``/``#`` and arithmetic
# operators match no group at all and are likewise rejected.
_EXPRESSION_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<NUMBER>-?\d+(?:\.\d+)?)
    | (?P<STRING>'[^'\\\n]*'|"[^"\\\n]*")
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<OP>==|!=|>=|<=|>|<)
    | (?P<AND>&&)
    | (?P<OR>\|\|)
    | (?P<NOT>!)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACKET>\[)
    | (?P<RBRACKET>\])
    | (?P<COMMA>,)
    | (?P<DOT>\.)
    """,
    re.VERBOSE,
)

T = TypeVar("T")


class ControlPlaneValidationError(Exception):
    """Raised when a control-plane manifest cannot be coerced for validation."""


def validate_control_plane_manifest(
    manifest: ControlPlaneManifest | Mapping[str, Any],
) -> ValidationResult:
    """Validate a control-plane manifest or raw manifest dictionary."""

    errors: list[str] = []
    warnings: list[str] = []

    if isinstance(manifest, Mapping):
        raw = dict(manifest)
        _scan_extra_sensitive_fields(raw, errors)
        scan_sensitive_fields(raw, errors)
        _validate_raw_disallowed_fields(raw, errors)
        try:
            manifest_obj = manifest_from_dict(raw)
        except ControlPlaneValidationError as exc:
            errors.append(str(exc))
            return ValidationResult(valid=False, errors=errors, warnings=warnings)
    else:
        manifest_obj = manifest

    if manifest_obj.schema_version != CONTROL_PLANE_SCHEMA_VERSION:
        errors.append(
            "schema_version must be "
            f"{CONTROL_PLANE_SCHEMA_VERSION}, got {manifest_obj.schema_version!r}"
        )

    version_result = check_contract_version_compatibility(
        manifest_obj.contract_version,
        CONTROL_PLANE_CONTRACT_VERSION,
    )
    errors.extend(version_result.errors)
    warnings.extend(version_result.warnings)

    _require_id("workspace.id", manifest_obj.workspace.id, errors)
    for field_name, value in (
        ("max_cost_units", manifest_obj.workspace.max_cost_units),
        ("max_token_usage", manifest_obj.workspace.max_token_usage),
    ):
        if value is not None and value < 0:
            errors.append(f"workspace.{field_name} must be >= 0")
    _validate_resources("connections", manifest_obj.connections, errors)
    _validate_resources("capabilities", manifest_obj.capabilities, errors)
    _validate_resources("agents", manifest_obj.agents, errors)
    _validate_resources("triggers", manifest_obj.triggers, errors)
    _validate_resources("automations", manifest_obj.automations, errors)
    _validate_resources("policies", manifest_obj.policies, errors)
    _validate_resources("policy_templates", manifest_obj.policy_templates, errors)
    _validate_resources("policy_rule_packs", manifest_obj.policy_rule_packs, errors)
    _validate_resources("approvals", manifest_obj.approvals, errors)
    _validate_resources("run_records", manifest_obj.run_records, errors)
    _validate_resources("audit_events", manifest_obj.audit_events, errors)
    _validate_resources("import_records", manifest_obj.import_records, errors)
    _validate_resources("secret_metadata", manifest_obj.secret_metadata, errors)
    _validate_resources("sync_rules", manifest_obj.sync_rules, errors)
    _validate_resources(
        "tenant_isolation_rules", manifest_obj.tenant_isolation_rules, errors
    )
    _validate_resources(
        "consultant_access_grants", manifest_obj.consultant_access_grants, errors
    )
    _validate_resources(
        "local_approval_notifications",
        manifest_obj.local_approval_notifications,
        errors,
    )

    connection_ids = {connection.id for connection in manifest_obj.connections}
    capability_ids = {capability.id for capability in manifest_obj.capabilities}
    agent_ids = {agent.id for agent in manifest_obj.agents}
    trigger_ids = {trigger.id for trigger in manifest_obj.triggers}
    automation_ids = {automation.id for automation in manifest_obj.automations}
    policy_ids = {policy.id for policy in manifest_obj.policies}
    approval_ids = {approval.id for approval in manifest_obj.approvals}
    run_ids = {run_record.id for run_record in manifest_obj.run_records}
    import_record_ids = {
        import_record.id for import_record in manifest_obj.import_records
    }

    for capability in manifest_obj.capabilities:
        _require_ref(
            f"capabilities[{capability.id}].connection_id",
            capability.connection_id,
            connection_ids,
            errors,
        )
        _validate_json_schema(
            f"capabilities[{capability.id}].input_schema",
            capability.input_schema,
            errors,
        )
        _validate_json_schema(
            f"capabilities[{capability.id}].output_schema",
            capability.output_schema,
            errors,
        )
        if capability.import_record_id is not None:
            _require_ref(
                f"capabilities[{capability.id}].import_record_id",
                capability.import_record_id,
                import_record_ids,
                errors,
            )

    for agent in manifest_obj.agents:
        for capability_id in agent.allowed_capabilities:
            _require_ref(
                f"agents[{agent.id}].allowed_capabilities",
                capability_id,
                capability_ids,
                errors,
            )

    for trigger in manifest_obj.triggers:
        _require_ref(
            f"triggers[{trigger.id}].capability_id",
            trigger.capability_id,
            capability_ids,
            errors,
        )
        _validate_trigger_schedule(trigger, errors)

    for automation in manifest_obj.automations:
        _require_ref(
            f"automations[{automation.id}].agent_id",
            automation.agent_id,
            agent_ids,
            errors,
        )
        _require_ref(
            f"automations[{automation.id}].capability_id",
            automation.capability_id,
            capability_ids,
            errors,
        )
        if automation.trigger_id is not None:
            _require_ref(
                f"automations[{automation.id}].trigger_id",
                automation.trigger_id,
                trigger_ids,
                errors,
            )
        for policy_id in automation.policy_ids:
            _require_ref(
                f"automations[{automation.id}].policy_ids",
                policy_id,
                policy_ids,
                errors,
            )
        for approval_id in automation.approval_ids:
            _require_ref(
                f"automations[{automation.id}].approval_ids",
                approval_id,
                approval_ids,
                errors,
            )
        for field_name, value in (
            ("max_steps", automation.max_steps),
            ("max_cost_units", automation.max_cost_units),
            ("max_token_usage", automation.max_token_usage),
            ("max_retries", automation.max_retries),
        ):
            if value is not None and value < 0:
                errors.append(f"automations[{automation.id}].{field_name} must be >= 0")
        if automation.max_steps is not None and automation.max_steps < 2:
            errors.append(
                f"automations[{automation.id}].max_steps must allow agent and capability steps"
            )

    for policy in manifest_obj.policies:
        for capability_id in policy.capability_ids:
            _require_ref(
                f"policies[{policy.id}].capability_ids",
                capability_id,
                capability_ids,
                errors,
            )
        for automation_id in policy.automation_ids:
            _require_ref(
                f"policies[{policy.id}].automation_ids",
                automation_id,
                automation_ids,
                errors,
            )
        for field_name, value in (
            ("max_cost_units", policy.max_cost_units),
            ("max_token_usage", policy.max_token_usage),
            ("timeout_seconds", policy.timeout_seconds),
            ("retention_days", policy.retention_days),
        ):
            if value is not None and value < 0:
                errors.append(f"policies[{policy.id}].{field_name} must be >= 0")
        _validate_attribute_conditions(
            f"policies[{policy.id}]", policy.attribute_conditions, errors
        )
        _validate_attribute_expression(
            f"policies[{policy.id}]", policy.attribute_expression, errors
        )

    policy_template_ids = {template.id for template in manifest_obj.policy_templates}
    for template in manifest_obj.policy_templates:
        for field_name, value in (
            ("max_cost_units", template.max_cost_units),
            ("max_token_usage", template.max_token_usage),
            ("timeout_seconds", template.timeout_seconds),
            ("retention_days", template.retention_days),
        ):
            if value is not None and value < 0:
                errors.append(
                    f"policy_templates[{template.id}].{field_name} must be >= 0"
                )
        _validate_attribute_conditions(
            f"policy_templates[{template.id}]",
            template.attribute_conditions,
            errors,
        )
        _validate_attribute_expression(
            f"policy_templates[{template.id}]",
            template.attribute_expression,
            errors,
        )

    for rule_pack in manifest_obj.policy_rule_packs:
        for template_id in rule_pack.template_ids:
            _require_ref(
                f"policy_rule_packs[{rule_pack.id}].template_ids",
                template_id,
                policy_template_ids,
                errors,
            )

    for approval in manifest_obj.approvals:
        for capability_id in approval.capability_ids:
            _require_ref(
                f"approvals[{approval.id}].capability_ids",
                capability_id,
                capability_ids,
                errors,
            )
        for automation_id in approval.automation_ids:
            _require_ref(
                f"approvals[{approval.id}].automation_ids",
                automation_id,
                automation_ids,
                errors,
            )
        if approval.timeout_seconds is not None and approval.timeout_seconds < 0:
            errors.append(f"approvals[{approval.id}].timeout_seconds must be >= 0")
        if approval.escalation_state not in APPROVAL_ESCALATION_STATES:
            allowed = ", ".join(
                sorted(state for state in APPROVAL_ESCALATION_STATES if state)
            )
            errors.append(
                f"approvals[{approval.id}].escalation_state must be one of: "
                f"{allowed}"
            )
        if (approval.resource_type is None) != (approval.resource_id is None):
            errors.append(
                f"approvals[{approval.id}].resource_type and resource_id must "
                "be set together"
            )

    for run_record in manifest_obj.run_records:
        _require_ref(
            f"run_records[{run_record.id}].automation_id",
            run_record.automation_id,
            automation_ids,
            errors,
        )
        if run_record.run_ledger_ref is None and run_record.run_ledger_entry_id is None:
            warnings.append(
                f"run_records[{run_record.id}] has no run-ledger correlation"
            )
        if run_record.retry_count < 0:
            errors.append(f"run_records[{run_record.id}].retry_count must be >= 0")

    for audit_event in manifest_obj.audit_events:
        if audit_event.run_id is not None:
            _require_ref(
                f"audit_events[{audit_event.id}].run_id",
                audit_event.run_id,
                run_ids,
                errors,
            )

    for import_record in manifest_obj.import_records:
        if import_record.lifecycle == LifecycleState.ACTIVE:
            errors.append(
                f"import_records[{import_record.id}].lifecycle must remain "
                "candidate/review metadata, not active runtime state"
            )
        released_capability_ids = set(import_record.released_capability_ids)
        candidate_capability_ids = set(import_record.candidate_capability_ids)
        unknown_release_ids = released_capability_ids - candidate_capability_ids
        for capability_id in sorted(unknown_release_ids):
            errors.append(
                f"import_records[{import_record.id}].released_capability_ids "
                f"must be a subset of candidate_capability_ids; got {capability_id}"
            )
        for capability_id in import_record.candidate_capability_ids:
            _require_ref(
                f"import_records[{import_record.id}].candidate_capability_ids",
                capability_id,
                capability_ids,
                errors,
            )
            capability = _find_by_id(manifest_obj.capabilities, capability_id)
            if (
                capability is not None
                and capability_id in released_capability_ids
                and capability.lifecycle
                not in {LifecycleState.REVIEWED, LifecycleState.ACTIVE}
            ):
                errors.append(
                    f"import_records[{import_record.id}].released_capability_ids "
                    f"references {capability_id}, which must be reviewed or active"
                )
            if (
                capability is not None
                and capability.lifecycle == LifecycleState.ACTIVE
                and capability_id not in released_capability_ids
            ):
                errors.append(
                    f"import_records[{import_record.id}] references active "
                    f"capability {capability_id}; imports must stay candidate-only"
                )

    for secret_metadata in manifest_obj.secret_metadata:
        if secret_metadata.owner_workspace_id != manifest_obj.workspace.id:
            errors.append(
                f"secret_metadata[{secret_metadata.id}].owner_workspace_id must "
                f"match workspace.id {manifest_obj.workspace.id!r}"
            )
        if not secret_metadata.secret_ref.startswith("secret://"):
            errors.append(
                f"secret_metadata[{secret_metadata.id}].secret_ref must be a "
                "secret:// reference"
            )
        if (
            secret_metadata.storage_scope == SecretStorageScope.LOCAL_ONLY
            and secret_metadata.syncable
        ):
            errors.append(
                f"secret_metadata[{secret_metadata.id}] is local_only and cannot "
                "be syncable"
            )
        if secret_metadata.storage_scope == SecretStorageScope.CLIENT_OWNED:
            if not secret_metadata.client_owned:
                errors.append(
                    f"secret_metadata[{secret_metadata.id}] uses client_owned "
                    "storage scope without client_owned=true"
                )

    for sync_rule in manifest_obj.sync_rules:
        if sync_rule.direction == SyncDirection.NONE and sync_rule.resource_type != "*":
            warnings.append(
                f"sync_rules[{sync_rule.id}] marks {sync_rule.resource_type} as local-only"
            )
        if (
            sync_rule.direction != SyncDirection.NONE
            and sync_rule.conflict_strategy == SyncConflictStrategy.NEWEST_VERSION_WINS
        ):
            warnings.append(
                f"sync_rules[{sync_rule.id}] uses newest_version_wins; audit "
                "events must record the chosen version"
            )
        if sync_rule.direction != SyncDirection.NONE and not sync_rule.audit_required:
            errors.append(
                f"sync_rules[{sync_rule.id}] syncs resources without audit_required"
            )

    for isolation_rule in manifest_obj.tenant_isolation_rules:
        if isolation_rule.workspace_id != manifest_obj.workspace.id:
            errors.append(
                f"tenant_isolation_rules[{isolation_rule.id}].workspace_id must "
                f"match workspace.id {manifest_obj.workspace.id!r}"
            )
        if isolation_rule.cross_client_sharing_allowed:
            errors.append(
                f"tenant_isolation_rules[{isolation_rule.id}] enables "
                "cross-client sharing"
            )

    for grant in manifest_obj.consultant_access_grants:
        if grant.client_workspace_id != manifest_obj.workspace.id:
            errors.append(
                f"consultant_access_grants[{grant.id}].client_workspace_id must "
                f"match workspace.id {manifest_obj.workspace.id!r}"
            )
        if grant.status == ConsultantGrantStatus.ACTIVE and not grant.policy_ids:
            errors.append(
                f"consultant_access_grants[{grant.id}] is active without policy_ids"
            )
        for policy_id in grant.policy_ids:
            _require_ref(
                f"consultant_access_grants[{grant.id}].policy_ids",
                policy_id,
                policy_ids,
                errors,
            )
        if grant.status == ConsultantGrantStatus.REVOKED and not grant.revoked_at:
            errors.append(
                f"consultant_access_grants[{grant.id}] is revoked without revoked_at"
            )

    for notification in manifest_obj.local_approval_notifications:
        prefix = f"local_approval_notifications[{notification.id}]"
        if notification.workspace_id != manifest_obj.workspace.id:
            errors.append(
                f"{prefix}.workspace_id must match workspace.id "
                f"{manifest_obj.workspace.id!r}"
            )
        _require_ref(f"{prefix}.run_id", notification.run_id, run_ids, errors)
        if notification.approval_id is not None:
            _require_ref(
                f"{prefix}.approval_id",
                notification.approval_id,
                approval_ids,
                errors,
            )
        if notification.capability_id is not None:
            _require_ref(
                f"{prefix}.capability_id",
                notification.capability_id,
                capability_ids,
                errors,
            )
        if notification.automation_id is not None:
            _require_ref(
                f"{prefix}.automation_id",
                notification.automation_id,
                automation_ids,
                errors,
            )
        if notification.attempt_count < 0:
            errors.append(f"{prefix}.attempt_count must be >= 0")

    _validate_approval_coverage(manifest_obj, errors)

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def manifest_from_dict(data: Mapping[str, Any]) -> ControlPlaneManifest:
    """Coerce a raw dictionary into a ControlPlaneManifest for validation."""

    workspace_raw = _object(data.get("workspace"), "workspace")
    return ControlPlaneManifest(
        schema_version=str(data.get("schema_version", "")),
        workspace=WorkspaceRef(
            id=str(workspace_raw.get("id", "")),
            name=str(workspace_raw.get("name", "")),
            max_cost_units=_optional_int(
                workspace_raw.get("max_cost_units"), "workspace.max_cost_units"
            ),
            max_token_usage=_optional_int(
                workspace_raw.get("max_token_usage"), "workspace.max_token_usage"
            ),
        ),
        connections=[
            _connection_from_dict(item)
            for item in _list(data.get("connections", []), "connections")
        ],
        capabilities=[
            _capability_from_dict(item)
            for item in _list(data.get("capabilities", []), "capabilities")
        ],
        agents=[
            _agent_from_dict(item) for item in _list(data.get("agents", []), "agents")
        ],
        triggers=[
            _trigger_from_dict(item)
            for item in _list(data.get("triggers", []), "triggers")
        ],
        automations=[
            _automation_from_dict(item)
            for item in _list(data.get("automations", []), "automations")
        ],
        policies=[
            _policy_from_dict(item)
            for item in _list(data.get("policies", []), "policies")
        ],
        policy_templates=[
            _policy_template_from_dict(item)
            for item in _list(data.get("policy_templates", []), "policy_templates")
        ],
        policy_rule_packs=[
            _policy_rule_pack_from_dict(item)
            for item in _list(data.get("policy_rule_packs", []), "policy_rule_packs")
        ],
        approvals=[
            _approval_from_dict(item)
            for item in _list(data.get("approvals", []), "approvals")
        ],
        run_records=[
            _run_record_from_dict(item)
            for item in _list(data.get("run_records", []), "run_records")
        ],
        audit_events=[
            _audit_event_from_dict(item)
            for item in _list(data.get("audit_events", []), "audit_events")
        ],
        import_records=[
            _import_record_from_dict(item)
            for item in _list(data.get("import_records", []), "import_records")
        ],
        secret_metadata=[
            _secret_metadata_from_dict(item)
            for item in _list(data.get("secret_metadata", []), "secret_metadata")
        ],
        sync_rules=[
            _sync_rule_from_dict(item)
            for item in _list(data.get("sync_rules", []), "sync_rules")
        ],
        tenant_isolation_rules=[
            _tenant_isolation_rule_from_dict(item)
            for item in _list(
                data.get("tenant_isolation_rules", []), "tenant_isolation_rules"
            )
        ],
        consultant_access_grants=[
            _consultant_access_grant_from_dict(item)
            for item in _list(
                data.get("consultant_access_grants", []),
                "consultant_access_grants",
            )
        ],
        local_approval_notifications=[
            _local_approval_notification_from_dict(item)
            for item in _list(
                data.get("local_approval_notifications", []),
                "local_approval_notifications",
            )
        ],
    )


def _connection_from_dict(data: Any) -> Connection:
    raw = _object(data, "connections[]")
    return Connection(
        id=str(raw.get("id", "")),
        kind=_enum(ConnectionKind, raw.get("kind"), "connections[].kind"),
        display_name=str(raw.get("display_name", "")),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "connections[].lifecycle",
        ),
        secret_refs=[
            _secret_reference_from_dict(item)
            for item in _list(raw.get("secret_refs", []), "connections[].secret_refs")
        ],
        catalogue_entry_id=_optional_str(raw.get("catalogue_entry_id")),
    )


def _secret_reference_from_dict(data: Any) -> SecretReference:
    raw = _object(data, "secret_refs[]")
    return SecretReference(
        secret_ref=str(raw.get("secret_ref", "")),
        provider=str(raw.get("provider", "")),
        scope=str(raw.get("scope", "")),
    )


def _capability_from_dict(data: Any) -> Capability:
    raw = _object(data, "capabilities[]")
    return Capability(
        id=str(raw.get("id", "")),
        capability_type=_enum(
            CapabilityType,
            raw.get("capability_type", raw.get("type")),
            "capabilities[].capability_type",
        ),
        connection_id=str(raw.get("connection_id", "")),
        side_effect=_enum(
            SideEffect,
            raw.get("side_effect", SideEffect.NONE.value),
            "capabilities[].side_effect",
        ),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "capabilities[].lifecycle",
        ),
        display_name=str(raw.get("display_name", "")),
        import_record_id=_optional_str(raw.get("import_record_id")),
        input_schema=_schema_dict(raw.get("input_schema"), "capabilities[].input_schema"),
        output_schema=_schema_dict(raw.get("output_schema"), "capabilities[].output_schema"),
        metadata=dict(raw.get("metadata", {})),
    )


def _agent_from_dict(data: Any) -> Agent:
    raw = _object(data, "agents[]")
    return Agent(
        id=str(raw.get("id", "")),
        display_name=str(raw.get("display_name", "")),
        allowed_capabilities=_str_list(
            raw.get("allowed_capabilities", []),
            "agents[].allowed_capabilities",
        ),
        run_mode=_enum(
            RunMode,
            raw.get("run_mode", RunMode.MANUAL.value),
            "agents[].run_mode",
        ),
    )


def _trigger_from_dict(data: Any) -> Trigger:
    raw = _object(data, "triggers[]")
    return Trigger(
        id=str(raw.get("id", "")),
        kind=_enum(TriggerKind, raw.get("kind"), "triggers[].kind"),
        capability_id=str(raw.get("capability_id", "")),
        event_type=str(raw.get("event_type", "")),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "triggers[].lifecycle",
        ),
        schedule_rrule=_optional_str(raw.get("schedule_rrule")),
        schedule_start_at=_optional_str(raw.get("schedule_start_at")),
        cooldown_seconds=_optional_int(raw.get("cooldown_seconds"), "triggers[].cooldown_seconds"),
        debounce_seconds=_optional_int(raw.get("debounce_seconds"), "triggers[].debounce_seconds"),
    )


def _automation_from_dict(data: Any) -> Automation:
    raw = _object(data, "automations[]")
    return Automation(
        id=str(raw.get("id", "")),
        agent_id=str(raw.get("agent_id", "")),
        capability_id=str(raw.get("capability_id", "")),
        trigger_id=_optional_str(raw.get("trigger_id")),
        policy_ids=_str_list(raw.get("policy_ids", []), "automations[].policy_ids"),
        approval_ids=_str_list(
            raw.get("approval_ids", []), "automations[].approval_ids"
        ),
        run_mode=_enum(
            RunMode,
            raw.get("run_mode", RunMode.MANUAL.value),
            "automations[].run_mode",
        ),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "automations[].lifecycle",
        ),
        max_steps=_optional_int(raw.get("max_steps"), "automations[].max_steps"),
        max_cost_units=_optional_int(
            raw.get("max_cost_units"), "automations[].max_cost_units"
        ),
        max_token_usage=_optional_int(
            raw.get("max_token_usage"), "automations[].max_token_usage"
        ),
        max_retries=_optional_int(raw.get("max_retries"), "automations[].max_retries"),
    )


def _policy_from_dict(data: Any) -> Policy:
    raw = _object(data, "policies[]")
    return Policy(
        id=str(raw.get("id", "")),
        decision=_enum(PolicyDecision, raw.get("decision"), "policies[].decision"),
        capability_ids=_str_list(
            raw.get("capability_ids", []), "policies[].capability_ids"
        ),
        automation_ids=_str_list(
            raw.get("automation_ids", []), "policies[].automation_ids"
        ),
        allowed_actor_roles=_str_list(
            raw.get("allowed_actor_roles", []), "policies[].allowed_actor_roles"
        ),
        required_actor_attributes=_str_str_map(
            raw.get("required_actor_attributes", {}),
            "policies[].required_actor_attributes",
        ),
        required_workspace_attributes=_str_str_map(
            raw.get("required_workspace_attributes", {}),
            "policies[].required_workspace_attributes",
        ),
        attribute_conditions=_attribute_conditions_from_dict(
            raw.get("attribute_conditions", []),
            "policies[].attribute_conditions",
        ),
        attribute_expression=_optional_attribute_expression_from_dict(
            raw.get("attribute_expression"),
            "policies[].attribute_expression",
        ),
        max_cost_units=_optional_int(raw.get("max_cost_units"), "policies[].max_cost_units"),
        max_token_usage=_optional_int(
            raw.get("max_token_usage"), "policies[].max_token_usage"
        ),
        timeout_seconds=_optional_int(raw.get("timeout_seconds"), "policies[].timeout_seconds"),
        retention_days=_optional_int(raw.get("retention_days"), "policies[].retention_days"),
        safety_classification=str(raw.get("safety_classification", "")),
        compliance_blocked=bool(raw.get("compliance_blocked", False)),
        reason=str(raw.get("reason", "")),
    )


def _policy_template_from_dict(data: Any) -> PolicyTemplate:
    raw = _object(data, "policy_templates[]")
    return PolicyTemplate(
        id=str(raw.get("id", "")),
        display_name=str(raw.get("display_name", "")),
        decision=_enum(
            PolicyDecision,
            raw.get("decision", PolicyDecision.REQUIRE_APPROVAL.value),
            "policy_templates[].decision",
        ),
        allowed_actor_roles=_str_list(
            raw.get("allowed_actor_roles", []),
            "policy_templates[].allowed_actor_roles",
        ),
        required_actor_attributes=_str_str_map(
            raw.get("required_actor_attributes", {}),
            "policy_templates[].required_actor_attributes",
        ),
        required_workspace_attributes=_str_str_map(
            raw.get("required_workspace_attributes", {}),
            "policy_templates[].required_workspace_attributes",
        ),
        attribute_conditions=_attribute_conditions_from_dict(
            raw.get("attribute_conditions", []),
            "policy_templates[].attribute_conditions",
        ),
        attribute_expression=_optional_attribute_expression_from_dict(
            raw.get("attribute_expression"),
            "policy_templates[].attribute_expression",
        ),
        max_cost_units=_optional_int(
            raw.get("max_cost_units"), "policy_templates[].max_cost_units"
        ),
        max_token_usage=_optional_int(
            raw.get("max_token_usage"), "policy_templates[].max_token_usage"
        ),
        timeout_seconds=_optional_int(
            raw.get("timeout_seconds"), "policy_templates[].timeout_seconds"
        ),
        retention_days=_optional_int(
            raw.get("retention_days"), "policy_templates[].retention_days"
        ),
        safety_classification=str(raw.get("safety_classification", "")),
        compliance_blocked=bool(raw.get("compliance_blocked", False)),
        reason=str(raw.get("reason", "")),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "policy_templates[].lifecycle",
        ),
    )


def _policy_rule_pack_from_dict(data: Any) -> PolicyRulePack:
    raw = _object(data, "policy_rule_packs[]")
    return PolicyRulePack(
        id=str(raw.get("id", "")),
        display_name=str(raw.get("display_name", "")),
        template_ids=_str_list(
            raw.get("template_ids", []), "policy_rule_packs[].template_ids"
        ),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "policy_rule_packs[].lifecycle",
        ),
        reason=str(raw.get("reason", "")),
    )


def _approval_from_dict(data: Any) -> Approval:
    raw = _object(data, "approvals[]")
    return Approval(
        id=str(raw.get("id", "")),
        capability_ids=_str_list(
            raw.get("capability_ids", []), "approvals[].capability_ids"
        ),
        automation_ids=_str_list(
            raw.get("automation_ids", []), "approvals[].automation_ids"
        ),
        approved=bool(raw.get("approved", False)),
        approver_role=str(raw.get("approver_role", "")),
        actor_id=_optional_str(raw.get("actor_id")),
        comment=_optional_str(raw.get("comment")),
        timeout_seconds=_optional_int(
            raw.get("timeout_seconds"), "approvals[].timeout_seconds"
        ),
        escalation_state=str(raw.get("escalation_state", "")),
        run_id=_optional_str(raw.get("run_id")),
        resource_type=_optional_str(raw.get("resource_type")),
        resource_id=_optional_str(raw.get("resource_id")),
        requested_at=_optional_str(raw.get("requested_at")),
        decided_at=_optional_str(raw.get("decided_at")),
        expires_at=_optional_str(raw.get("expires_at")),
        assigned_actor_id=_optional_str(raw.get("assigned_actor_id")),
        assigned_role=_optional_str(raw.get("assigned_role")),
        assigned_at=_optional_str(raw.get("assigned_at")),
        escalated_at=_optional_str(raw.get("escalated_at")),
        escalation_reason=_optional_str(raw.get("escalation_reason")),
    )


def _run_record_from_dict(data: Any) -> RunRecord:
    raw = _object(data, "run_records[]")
    return RunRecord(
        id=str(raw.get("id", "")),
        automation_id=str(raw.get("automation_id", "")),
        status=_enum(ControlPlaneRunStatus, raw.get("status"), "run_records[].status"),
        run_ledger_ref=_optional_str(raw.get("run_ledger_ref")),
        run_ledger_entry_id=_optional_str(raw.get("run_ledger_entry_id")),
        trace_id=_optional_str(raw.get("trace_id")),
        retention_expires_at=_optional_str(raw.get("retention_expires_at")),
        started_at=_optional_str(raw.get("started_at")),
        updated_at=_optional_str(raw.get("updated_at")),
        resume_token=_optional_str(raw.get("resume_token")),
        resume_after=_optional_str(raw.get("resume_after")),
        resume_reason=_optional_str(raw.get("resume_reason")),
        retry_count=_optional_int(raw.get("retry_count", 0), "run_records[].retry_count") or 0,
    )


def _audit_event_from_dict(data: Any) -> AuditEvent:
    raw = _object(data, "audit_events[]")
    return AuditEvent(
        id=str(raw.get("id", "")),
        event_type=str(raw.get("event_type", "")),
        resource_id=str(raw.get("resource_id", "")),
        run_id=_optional_str(raw.get("run_id")),
    )


def _import_record_from_dict(data: Any) -> ImportRecord:
    raw = _object(data, "import_records[]")
    return ImportRecord(
        id=str(raw.get("id", "")),
        source_protocol=_enum(
            ImportSourceProtocol,
            raw.get("source_protocol"),
            "import_records[].source_protocol",
        ),
        source_ref=str(raw.get("source_ref", "")),
        candidate_capability_ids=_str_list(
            raw.get("candidate_capability_ids", []),
            "import_records[].candidate_capability_ids",
        ),
        released_capability_ids=_str_list(
            raw.get("released_capability_ids", []),
            "import_records[].released_capability_ids",
        ),
        source_hash=_optional_str(raw.get("source_hash")),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "import_records[].lifecycle",
        ),
    )


def _secret_metadata_from_dict(data: Any) -> SecretMetadata:
    raw = _object(data, "secret_metadata[]")
    return SecretMetadata(
        id=str(raw.get("id", "")),
        secret_ref=str(raw.get("secret_ref", "")),
        owner_workspace_id=str(raw.get("owner_workspace_id", "")),
        storage_scope=_enum(
            SecretStorageScope,
            raw.get("storage_scope", SecretStorageScope.LOCAL_ONLY.value),
            "secret_metadata[].storage_scope",
        ),
        provider=str(raw.get("provider", "")),
        client_owned=bool(raw.get("client_owned", False)),
        syncable=bool(raw.get("syncable", False)),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "secret_metadata[].lifecycle",
        ),
    )


def _sync_rule_from_dict(data: Any) -> SyncRule:
    raw = _object(data, "sync_rules[]")
    return SyncRule(
        id=str(raw.get("id", "")),
        resource_type=str(raw.get("resource_type", "")),
        direction=_enum(
            SyncDirection,
            raw.get("direction", SyncDirection.NONE.value),
            "sync_rules[].direction",
        ),
        conflict_strategy=_enum(
            SyncConflictStrategy,
            raw.get("conflict_strategy", SyncConflictStrategy.MANUAL_REVIEW.value),
            "sync_rules[].conflict_strategy",
        ),
        include_lifecycle_states=[
            _enum(
                LifecycleState,
                item,
                "sync_rules[].include_lifecycle_states",
            )
            for item in _list(
                raw.get("include_lifecycle_states", []),
                "sync_rules[].include_lifecycle_states",
            )
        ],
        audit_required=bool(raw.get("audit_required", True)),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "sync_rules[].lifecycle",
        ),
    )


def _tenant_isolation_rule_from_dict(data: Any) -> TenantIsolationRule:
    raw = _object(data, "tenant_isolation_rules[]")
    return TenantIsolationRule(
        id=str(raw.get("id", "")),
        tenant_id=str(raw.get("tenant_id", "")),
        workspace_id=str(raw.get("workspace_id", "")),
        client_workspace_id=_optional_str(raw.get("client_workspace_id")),
        data_boundary=str(raw.get("data_boundary", "workspace")),
        cross_client_sharing_allowed=bool(
            raw.get("cross_client_sharing_allowed", False)
        ),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "tenant_isolation_rules[].lifecycle",
        ),
    )


def _consultant_access_grant_from_dict(data: Any) -> ConsultantAccessGrant:
    raw = _object(data, "consultant_access_grants[]")
    return ConsultantAccessGrant(
        id=str(raw.get("id", "")),
        consultant_id=str(raw.get("consultant_id", "")),
        client_workspace_id=str(raw.get("client_workspace_id", "")),
        role=str(raw.get("role", "")),
        policy_ids=_str_list(
            raw.get("policy_ids", []), "consultant_access_grants[].policy_ids"
        ),
        status=_enum(
            ConsultantGrantStatus,
            raw.get("status", ConsultantGrantStatus.ACTIVE.value),
            "consultant_access_grants[].status",
        ),
        granted_by=str(raw.get("granted_by", "")),
        granted_at=str(raw.get("granted_at", "")),
        revoked_at=_optional_str(raw.get("revoked_at")),
        lifecycle=_enum(
            LifecycleState,
            raw.get("lifecycle", LifecycleState.CANDIDATE.value),
            "consultant_access_grants[].lifecycle",
        ),
    )


def _local_approval_notification_from_dict(data: Any) -> LocalApprovalNotification:
    raw = _object(data, "local_approval_notifications[]")
    return LocalApprovalNotification(
        id=str(raw.get("id", "")),
        run_id=str(raw.get("run_id", "")),
        workspace_id=str(raw.get("workspace_id", "")),
        channel=_enum(
            LocalApprovalNotificationChannel,
            raw.get("channel"),
            "local_approval_notifications[].channel",
        ),
        event_type=_enum(
            LocalApprovalNotificationEvent,
            raw.get("event_type"),
            "local_approval_notifications[].event_type",
        ),
        status=_enum(
            LocalApprovalNotificationStatus,
            raw.get("status", LocalApprovalNotificationStatus.QUEUED.value),
            "local_approval_notifications[].status",
        ),
        approval_id=_optional_str(raw.get("approval_id")),
        capability_id=_optional_str(raw.get("capability_id")),
        automation_id=_optional_str(raw.get("automation_id")),
        recipient_actor_id=_optional_str(raw.get("recipient_actor_id")),
        recipient_role=_optional_str(raw.get("recipient_role")),
        reason=_optional_str(raw.get("reason")),
        comment=_optional_str(raw.get("comment")),
        attempt_count=_optional_int(
            raw.get("attempt_count", 0),
            "local_approval_notifications[].attempt_count",
        )
        or 0,
        last_error=_optional_str(raw.get("last_error")),
        audit_event_id=_optional_str(raw.get("audit_event_id")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def _validate_approval_coverage(
    manifest: ControlPlaneManifest, errors: list[str]
) -> None:
    approved_capability_ids: set[str] = set()
    approved_automation_ids: set[str] = set()
    for approval in manifest.approvals:
        if approval.approved:
            approved_capability_ids.update(approval.capability_ids)
            approved_automation_ids.update(approval.automation_ids)

    approval_policy_capability_ids = {
        capability_id
        for policy in manifest.policies
        if policy.decision == PolicyDecision.REQUIRE_APPROVAL
        for capability_id in policy.capability_ids
    }

    automations_by_capability: dict[str, list[Automation]] = {}
    for automation in manifest.automations:
        automations_by_capability.setdefault(automation.capability_id, []).append(
            automation
        )

    approved_approval_ids = {
        approval.id for approval in manifest.approvals if approval.approved
    }

    for capability in manifest.capabilities:
        if (
            capability.lifecycle != LifecycleState.ACTIVE
            or capability.side_effect not in DANGEROUS_SIDE_EFFECTS
        ):
            continue
        direct_coverage = (
            capability.id in approved_capability_ids
            or capability.id in approval_policy_capability_ids
        )
        automation_coverage = any(
            automation.id in approved_automation_ids
            or any(
                approval_id in approved_approval_ids
                for approval_id in automation.approval_ids
            )
            for automation in automations_by_capability.get(capability.id, [])
        )
        if not direct_coverage and not automation_coverage:
            errors.append(
                f"capabilities[{capability.id}] has dangerous side effect "
                f"{capability.side_effect.value!r} and active lifecycle without "
                "approval coverage"
            )


def _validate_attribute_conditions(
    field_prefix: str,
    conditions: list[PolicyAttributeCondition],
    errors: list[str],
) -> None:
    for index, condition in enumerate(conditions):
        prefix = f"{field_prefix}.attribute_conditions[{index}]"
        if condition.scope not in POLICY_ATTRIBUTE_SCOPES:
            errors.append(f"{prefix}.scope must be one of: actor, workspace")
        if not condition.key.strip():
            errors.append(f"{prefix}.key is required")
        if condition.operator not in POLICY_ATTRIBUTE_OPERATORS:
            allowed = ", ".join(sorted(POLICY_ATTRIBUTE_OPERATORS))
            errors.append(f"{prefix}.operator must be one of: {allowed}")
            continue
        if condition.operator in POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS:
            if condition.value is None:
                errors.append(
                    f"{prefix}.value is required for {condition.operator}"
                )
            if condition.values:
                errors.append(
                    f"{prefix}.values must be empty for {condition.operator}"
                )
        elif condition.operator in POLICY_ATTRIBUTE_VALUES_OPERATORS:
            if not condition.values:
                errors.append(
                    f"{prefix}.values must be non-empty for {condition.operator}"
                )
            if condition.value is not None:
                errors.append(
                    f"{prefix}.value must be unset for {condition.operator}"
                )
        else:  # exists / not_exists
            if condition.value is not None:
                errors.append(
                    f"{prefix}.value must be unset for {condition.operator}"
                )
            if condition.values:
                errors.append(
                    f"{prefix}.values must be empty for {condition.operator}"
                )


def _validate_attribute_expression(
    field_prefix: str,
    expression: PolicyAttributeExpression | None,
    errors: list[str],
) -> None:
    """Validate a bounded structured attribute expression tree, failing closed.

    The tree is rejected when it uses an unsupported op, mixes children with a
    condition node, leaves boolean nodes empty, exceeds the bounded depth/node
    count, or contains a malformed leaf condition (which reuses the existing
    condition validation rules). This is a bounded boolean combinator over
    bounded condition operators, not CEL or a free-form expression language.
    """

    if expression is None:
        return
    prefix = f"{field_prefix}.attribute_expression"
    node_count = _count_expression_nodes(expression)
    if node_count > POLICY_EXPRESSION_MAX_NODES:
        errors.append(
            f"{prefix} must not exceed {POLICY_EXPRESSION_MAX_NODES} nodes; "
            f"got {node_count}"
        )
    _validate_expression_node(prefix, expression, errors, depth=1)


def _count_expression_nodes(expression: PolicyAttributeExpression) -> int:
    return 1 + sum(_count_expression_nodes(child) for child in expression.children)


def _validate_expression_node(
    prefix: str,
    node: PolicyAttributeExpression,
    errors: list[str],
    depth: int,
) -> None:
    if depth > POLICY_EXPRESSION_MAX_DEPTH:
        errors.append(
            f"{prefix} must not exceed depth {POLICY_EXPRESSION_MAX_DEPTH}"
        )
        return
    op = node.op
    if op not in POLICY_EXPRESSION_OPS:
        allowed = ", ".join(sorted(POLICY_EXPRESSION_OPS))
        errors.append(f"{prefix}.op must be one of: {allowed}")
        return
    if op == "condition":
        if node.children:
            errors.append(f"{prefix} condition node must not contain children")
        if node.condition is None:
            errors.append(f"{prefix} condition node requires exactly one condition")
        else:
            _validate_attribute_conditions(prefix, [node.condition], errors)
        return
    # Boolean combinator nodes (all/any/not) never carry a direct condition.
    if node.condition is not None:
        errors.append(f"{prefix} {op} node must not contain a direct condition")
    if op == "not":
        if len(node.children) != 1:
            errors.append(f"{prefix} not node must contain exactly one child")
    elif not node.children:
        errors.append(f"{prefix} {op} node must contain at least one child")
    for index, child in enumerate(node.children):
        _validate_expression_node(
            f"{prefix}.children[{index}]", child, errors, depth + 1
        )


def compile_policy_expression(source: str) -> PolicyAttributeExpression:
    """Compile a tiny CEL-shaped string into a bounded attribute expression.

    This is a safe interoperability helper for callers that hold a small
    CEL-looking policy string and want the bounded
    :class:`PolicyAttributeExpression` tree that the registry already evaluates.
    It is deliberately not an expression language and not free-form policy
    execution: there is no eval, regex, function calls, imports, arithmetic,
    attribute paths beyond ``actor.<key>``/``workspace.<key>`` simple keys, or
    arbitrary identifiers. The helper is explicit-API only; manifests still
    reject raw ``policy_expression``/``cel_expression``/``expression`` fields.

    Supported grammar (whitespace-insensitive)::

        expr        := or_expr
        or_expr     := and_expr ("||" and_expr)*
        and_expr    := unary ("&&" unary)*
        unary       := "!" unary | primary
        primary     := "(" expr ")" | comparison
        comparison  := attribute (compare_op literal
                                   | "in" list | "not" "in" list)
        attribute   := ("actor" | "workspace") "." key
        compare_op  := "==" | "!=" | ">" | ">=" | "<" | "<="
        literal     := string | number       (numeric ops require number)
        list        := "[" string ("," string)* "]"

    Equal/unequal chains of ``&&`` collapse into one ``all`` node and chains of
    ``||`` into one ``any`` node, so the compiled tree stays shallow. The
    returned tree is validated against the same bounded depth/node limits as a
    manifest-loaded expression before it is returned.

    Raises :class:`ControlPlaneValidationError` (fail-closed) on any
    unsupported syntax, secret-looking key/value, or an expression that exceeds
    the bounded limits.
    """

    if not isinstance(source, str):
        raise ControlPlaneValidationError("policy expression must be a string")
    if len(source) > POLICY_EXPRESSION_SOURCE_MAX_LENGTH:
        raise ControlPlaneValidationError(
            "policy expression exceeds "
            f"{POLICY_EXPRESSION_SOURCE_MAX_LENGTH} characters"
        )
    tokens = _tokenize_policy_expression(source)
    if not tokens:
        raise ControlPlaneValidationError("policy expression is empty")
    parser = _PolicyExpressionParser(tokens)
    expression = parser.parse()
    errors: list[str] = []
    _validate_attribute_expression("policy_expression", expression, errors)
    if errors:
        raise ControlPlaneValidationError("; ".join(errors))
    return expression


def _tokenize_policy_expression(source: str) -> list[tuple[str, str]]:
    """Tokenize a CEL-shaped string, rejecting any unsupported character."""

    tokens: list[tuple[str, str]] = []
    position = 0
    length = len(source)
    while position < length:
        match = _EXPRESSION_TOKEN_RE.match(source, position)
        if match is None:
            raise ControlPlaneValidationError(
                f"policy expression has unsupported syntax at position {position}: "
                f"{source[position]!r}"
            )
        kind = match.lastgroup
        value = match.group()
        position = match.end()
        if kind == "WS":
            continue
        assert kind is not None
        tokens.append((kind, value))
    return tokens


class _PolicyExpressionParser:
    """Recursive-descent parser for the bounded CEL-shaped expression subset."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> PolicyAttributeExpression:
        expression = self._parse_or(depth=1)
        if self._index != len(self._tokens):
            kind, value = self._tokens[self._index]
            raise ControlPlaneValidationError(
                f"policy expression has unexpected trailing token {value!r}"
            )
        return expression

    def _peek(self) -> tuple[str, str] | None:
        if self._index < len(self._tokens):
            return self._tokens[self._index]
        return None

    def _peek_kind(self) -> str | None:
        token = self._peek()
        return token[0] if token is not None else None

    def _advance(self) -> tuple[str, str]:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _guard_depth(self, depth: int) -> None:
        if depth > POLICY_EXPRESSION_PARSE_MAX_DEPTH:
            raise ControlPlaneValidationError(
                "policy expression nests too deeply"
            )

    def _parse_or(self, depth: int) -> PolicyAttributeExpression:
        self._guard_depth(depth)
        children = [self._parse_and(depth + 1)]
        while self._peek_kind() == "OR":
            self._advance()
            children.append(self._parse_and(depth + 1))
        if len(children) == 1:
            return children[0]
        return PolicyAttributeExpression(op="any", children=children)

    def _parse_and(self, depth: int) -> PolicyAttributeExpression:
        self._guard_depth(depth)
        children = [self._parse_unary(depth + 1)]
        while self._peek_kind() == "AND":
            self._advance()
            children.append(self._parse_unary(depth + 1))
        if len(children) == 1:
            return children[0]
        return PolicyAttributeExpression(op="all", children=children)

    def _parse_unary(self, depth: int) -> PolicyAttributeExpression:
        self._guard_depth(depth)
        if self._peek_kind() == "NOT":
            self._advance()
            child = self._parse_unary(depth + 1)
            return PolicyAttributeExpression(op="not", children=[child])
        return self._parse_primary(depth + 1)

    def _parse_primary(self, depth: int) -> PolicyAttributeExpression:
        self._guard_depth(depth)
        token = self._peek()
        if token is None:
            raise ControlPlaneValidationError(
                "policy expression ended unexpectedly"
            )
        if token[0] == "LPAREN":
            self._advance()
            expression = self._parse_or(depth + 1)
            closing = self._peek()
            if closing is None or closing[0] != "RPAREN":
                raise ControlPlaneValidationError(
                    "policy expression has an unbalanced parenthesis"
                )
            self._advance()
            return expression
        return self._parse_comparison()

    def _parse_comparison(self) -> PolicyAttributeExpression:
        scope, key = self._parse_attribute()
        token = self._peek()
        if token is None:
            raise ControlPlaneValidationError(
                f"policy expression attribute {scope}.{key} is missing a comparison"
            )
        kind, value = token
        if kind == "OP":
            self._advance()
            return self._build_value_condition(scope, key, value)
        if kind == "IDENT" and value == "in":
            self._advance()
            values = self._parse_string_list()
            return _condition_expression(scope, key, "in", values=values)
        if kind == "IDENT" and value == "not":
            self._advance()
            following = self._peek()
            if following is None or following != ("IDENT", "in"):
                raise ControlPlaneValidationError(
                    "policy expression expected 'not in' membership test"
                )
            self._advance()
            values = self._parse_string_list()
            return _condition_expression(scope, key, "not_in", values=values)
        raise ControlPlaneValidationError(
            f"policy expression has unsupported operator near {value!r}"
        )

    def _parse_attribute(self) -> tuple[str, str]:
        token = self._peek()
        if token is None or token[0] != "IDENT":
            raise ControlPlaneValidationError(
                "policy expression expected an actor/workspace attribute"
            )
        scope = self._advance()[1]
        if scope not in POLICY_ATTRIBUTE_SCOPES:
            raise ControlPlaneValidationError(
                "policy expression scope must be actor or workspace, got "
                f"{scope!r}"
            )
        dot = self._peek()
        if dot is None or dot[0] != "DOT":
            raise ControlPlaneValidationError(
                f"policy expression attribute {scope} requires a .key segment"
            )
        self._advance()
        key_token = self._peek()
        if key_token is None or key_token[0] != "IDENT":
            raise ControlPlaneValidationError(
                f"policy expression attribute {scope} requires a simple key"
            )
        key = self._advance()[1]
        trailing = self._peek()
        if trailing is not None and trailing[0] == "DOT":
            raise ControlPlaneValidationError(
                "policy expression does not support attribute paths beyond "
                f"{scope}.<key>"
            )
        _reject_secret_text("attribute key", key)
        return scope, key

    def _build_value_condition(
        self, scope: str, key: str, operator: str
    ) -> PolicyAttributeExpression:
        condition_operator = POLICY_EXPRESSION_COMPARISON_OPERATORS[operator]
        token = self._peek()
        if token is None:
            raise ControlPlaneValidationError(
                f"policy expression {scope}.{key} {operator} is missing a literal"
            )
        kind, raw = token
        if operator in POLICY_EXPRESSION_NUMERIC_COMPARISONS:
            if kind != "NUMBER":
                raise ControlPlaneValidationError(
                    f"policy expression {operator} requires a numeric literal"
                )
            self._advance()
            return _condition_expression(
                scope, key, condition_operator, value=raw
            )
        if kind == "NUMBER":
            self._advance()
            return _condition_expression(
                scope, key, condition_operator, value=raw
            )
        if kind == "STRING":
            self._advance()
            literal = _string_literal_value(raw)
            return _condition_expression(
                scope, key, condition_operator, value=literal
            )
        raise ControlPlaneValidationError(
            f"policy expression {scope}.{key} {operator} requires a string or "
            "number literal"
        )

    def _parse_string_list(self) -> list[str]:
        opening = self._peek()
        if opening is None or opening[0] != "LBRACKET":
            raise ControlPlaneValidationError(
                "policy expression membership test requires a [..] list"
            )
        self._advance()
        values: list[str] = []
        while True:
            token = self._peek()
            if token is None:
                raise ControlPlaneValidationError(
                    "policy expression membership list is unterminated"
                )
            if token[0] != "STRING":
                raise ControlPlaneValidationError(
                    "policy expression membership list accepts only string literals"
                )
            self._advance()
            values.append(_string_literal_value(token[1]))
            separator = self._peek()
            if separator is None:
                raise ControlPlaneValidationError(
                    "policy expression membership list is unterminated"
                )
            if separator[0] == "COMMA":
                self._advance()
                continue
            if separator[0] == "RBRACKET":
                self._advance()
                break
            raise ControlPlaneValidationError(
                "policy expression membership list has unsupported syntax"
            )
        if not values:
            raise ControlPlaneValidationError(
                "policy expression membership list must not be empty"
            )
        return values


def _condition_expression(
    scope: str,
    key: str,
    operator: str,
    *,
    value: str | None = None,
    values: list[str] | None = None,
) -> PolicyAttributeExpression:
    condition = PolicyAttributeCondition(
        scope=scope,
        key=key,
        operator=operator,
        value=value,
        values=list(values) if values is not None else [],
    )
    return PolicyAttributeExpression(op="condition", condition=condition)


def _string_literal_value(raw: str) -> str:
    value = raw[1:-1]
    # Reject template interpolation and backtick characters inside literals so
    # no ``${...}``/``{{...}}``/backtick interpolation can ride in as a value.
    for forbidden in ("$", "`", "{", "}"):
        if forbidden in value:
            raise ControlPlaneValidationError(
                "policy expression string literal has unsupported syntax"
            )
    _reject_secret_text("string literal", value)
    return value


def _reject_secret_text(label: str, text: str) -> None:
    lowered = text.lower()
    for marker in POLICY_EXPRESSION_SECRET_MARKERS:
        if marker in lowered:
            raise ControlPlaneValidationError(
                f"policy expression {label} looks secret and is rejected"
            )


def _validate_trigger_schedule(trigger: Trigger, errors: list[str]) -> None:
    if trigger.kind == TriggerKind.SCHEDULE:
        if not trigger.schedule_rrule:
            errors.append(
                f"triggers[{trigger.id}].schedule_rrule is required for schedule triggers"
            )
        elif not _valid_rrule(trigger.schedule_rrule):
            errors.append(
                f"triggers[{trigger.id}].schedule_rrule must be a valid RRULE"
            )
        # schedule_start_at is optional; schedule triggers may omit it to
        # preserve the existing 1970-anchored materialization behavior.
        if trigger.schedule_start_at is not None and not _valid_iso_timestamp(
            trigger.schedule_start_at
        ):
            errors.append(
                f"triggers[{trigger.id}].schedule_start_at must be an ISO timestamp"
            )
    else:
        if trigger.schedule_rrule:
            errors.append(
                f"triggers[{trigger.id}].schedule_rrule is only valid for schedule triggers"
            )
        if trigger.schedule_start_at is not None:
            errors.append(
                f"triggers[{trigger.id}].schedule_start_at is only valid for schedule triggers"
            )
    for field_name, value in (
        ("cooldown_seconds", trigger.cooldown_seconds),
        ("debounce_seconds", trigger.debounce_seconds),
    ):
        if value is not None and value < 0:
            errors.append(f"triggers[{trigger.id}].{field_name} must be >= 0")


def _validate_json_schema(
    field_name: str,
    schema: Mapping[str, Any],
    errors: list[str],
) -> None:
    if not schema:
        return
    schema_type = schema.get("type")
    if schema_type not in {"object", "array", "string", "number", "integer", "boolean"}:
        errors.append(
            f"{field_name}.type must be a supported JSON schema type"
        )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        errors.append(f"{field_name}.properties must be an object")
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            errors.append(f"{field_name}.required must be a list of strings")
        elif isinstance(properties, Mapping):
            missing = [item for item in required if item not in properties]
            if missing:
                errors.append(
                    f"{field_name}.required references unknown properties: "
                    + ", ".join(missing)
                )
    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None and not isinstance(
        additional_properties, bool | Mapping
    ):
        errors.append(
            f"{field_name}.additionalProperties must be a boolean or object"
        )
    items = schema.get("items")
    if items is not None and not isinstance(items, Mapping):
        errors.append(f"{field_name}.items must be an object")


def _valid_rrule(value: str) -> bool:
    text = value.strip()
    if text.startswith("RRULE:"):
        text = text.removeprefix("RRULE:")
    if not text:
        return False
    pairs: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            return False
        key, raw_value = part.split("=", 1)
        key = key.strip().upper()
        raw_value = raw_value.strip().upper()
        if not key or not raw_value or key in pairs:
            return False
        pairs[key] = raw_value
    if pairs.get("FREQ") not in RRULE_FREQUENCIES:
        return False
    for key in ("INTERVAL", "COUNT"):
        if key in pairs and not _positive_int_text(pairs[key]):
            return False
    if "BYDAY" in pairs:
        days = [day.strip() for day in pairs["BYDAY"].split(",")]
        if not days or any(day not in RRULE_WEEKDAYS for day in days):
            return False
    if "UNTIL" in pairs and not re.fullmatch(r"\d{8}(T\d{6}Z?)?", pairs["UNTIL"]):
        return False
    allowed_keys = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY"}
    return set(pairs).issubset(allowed_keys)


def _valid_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _positive_int_text(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def _validate_raw_disallowed_fields(data: Mapping[str, Any], errors: list[str]) -> None:
    for index, agent in enumerate(_raw_list(data.get("agents", []))):
        if isinstance(agent, Mapping) and "allowed_connections" in agent:
            errors.append(
                f"agents[{index}].allowed_connections is invalid; use "
                "allowed_capabilities"
            )
    for index, import_record in enumerate(_raw_list(data.get("import_records", []))):
        if isinstance(import_record, Mapping):
            for field_name in ("activated_capability_id", "runtime_active"):
                if field_name in import_record:
                    errors.append(
                        f"import_records[{index}].{field_name} must not imply "
                        "runtime activation"
                    )
    for collection in ("policies", "policy_templates"):
        for index, item in enumerate(_raw_list(data.get(collection, []))):
            if not isinstance(item, Mapping):
                continue
            for field_name in sorted(POLICY_EXPRESSION_RAW_FIELDS):
                if field_name in item:
                    errors.append(
                        f"{collection}[{index}].{field_name} is invalid; use the "
                        "bounded attribute_expression tree"
                    )


def _scan_extra_sensitive_fields(
    data: dict[str, Any] | list[Any] | Any,
    errors: list[str],
    prefix: str = "",
) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if key.lower() in EXTRA_SENSITIVE_KEYS:
                errors.append(f"{path} must not expose sensitive fields")
            _scan_extra_sensitive_fields(value, errors, path)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            _scan_extra_sensitive_fields(value, errors, f"{prefix}[{index}]")


def _validate_resources(name: str, resources: list[Any], errors: list[str]) -> None:
    seen: set[str] = set()
    for index, resource in enumerate(resources):
        resource_id = getattr(resource, "id", "")
        _require_id(f"{name}[{index}].id", resource_id, errors)
        if resource_id in seen:
            errors.append(f"{name}[{index}].id duplicates {resource_id!r}")
        seen.add(resource_id)


def _require_id(field_name: str, value: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field_name} is required")
    elif STABLE_ID_PATTERN.fullmatch(value) is None:
        errors.append(f"{field_name} must be a stable identifier")


def _require_ref(
    field_name: str,
    value: str,
    allowed_values: set[str],
    errors: list[str],
) -> None:
    if not value:
        errors.append(f"{field_name} is required")
    elif value not in allowed_values:
        errors.append(f"{field_name} references unknown id {value!r}")


def _find_by_id(resources: list[T], resource_id: str) -> T | None:
    for resource in resources:
        if getattr(resource, "id", None) == resource_id:
            return resource
    return None


def _object(data: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ControlPlaneValidationError(f"{field_name} must be an object")
    return data


def _list(data: Any, field_name: str) -> list[Any]:
    if not isinstance(data, list):
        raise ControlPlaneValidationError(f"{field_name} must be a list")
    return data


def _raw_list(data: Any) -> list[Any]:
    return data if isinstance(data, list) else []


def _str_list(data: Any, field_name: str) -> list[str]:
    values = _list(data, field_name)
    if not all(isinstance(value, str) for value in values):
        raise ControlPlaneValidationError(f"{field_name} must contain strings")
    return list(values)


def _str_str_map(data: Any, field_name: str) -> dict[str, str]:
    if data is None:
        return {}
    mapping = _object(data, field_name)
    result: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ControlPlaneValidationError(
                f"{field_name} must map strings to strings"
            )
        result[key] = value
    return result


def _attribute_conditions_from_dict(
    data: Any, field_name: str
) -> list[PolicyAttributeCondition]:
    return [
        _attribute_condition_from_dict(item, field_name)
        for item in _list(data, field_name)
    ]


def _attribute_condition_from_dict(
    data: Any, field_name: str
) -> PolicyAttributeCondition:
    raw = _object(data, f"{field_name}[]")
    return PolicyAttributeCondition(
        scope=str(raw.get("scope", "")),
        key=str(raw.get("key", "")),
        operator=str(raw.get("operator", "")),
        value=_optional_str(raw.get("value")),
        values=_str_list(raw.get("values", []), f"{field_name}[].values"),
    )


def _optional_attribute_expression_from_dict(
    data: Any, field_name: str
) -> PolicyAttributeExpression | None:
    if data is None:
        return None
    return _attribute_expression_from_dict(data, field_name)


def _attribute_expression_from_dict(
    data: Any, field_name: str
) -> PolicyAttributeExpression:
    raw = _object(data, field_name)
    condition_raw = raw.get("condition")
    condition = (
        _attribute_condition_from_dict(condition_raw, f"{field_name}.condition")
        if condition_raw is not None
        else None
    )
    children = [
        _attribute_expression_from_dict(item, f"{field_name}.children[{index}]")
        for index, item in enumerate(
            _list(raw.get("children", []), f"{field_name}.children")
        )
    ]
    return PolicyAttributeExpression(
        op=str(raw.get("op", "")),
        children=children,
        condition=condition,
    )


def _schema_dict(data: Any, field_name: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ControlPlaneValidationError(f"{field_name} must be an object")
    return dict(data)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlPlaneValidationError(f"{field_name} must be an integer")
    return value


def _enum(enum_type: type[Enum], value: Any, field_name: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ControlPlaneValidationError(
            f"{field_name} must be one of: {allowed}, got {value!r}"
        ) from exc
