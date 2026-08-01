"""Public control-plane contract models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnivia_core.knowledge.models import ContractVersion

CONTROL_PLANE_CONTRACT_VERSION = ContractVersion(1, 0)
CONTROL_PLANE_SCHEMA_VERSION = "omnivia.control_plane_manifest.v1"


class ConnectionKind(str, Enum):
    """Connection kinds that can back runtime capabilities."""

    APP = "app"
    API = "api"
    MCP = "mcp"
    WEBHOOK = "webhook"
    INTERNAL = "internal"


class CapabilityType(str, Enum):
    """Capability source/type classification."""

    ACTION = "action"
    QUERY = "query"
    RESOURCE = "resource"
    PROMPT = "prompt"
    EVENT_SOURCE = "event_source"


class SideEffect(str, Enum):
    """Observable side-effect category for a capability."""

    NONE = "none"
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    SEND = "send"
    PUBLISH = "publish"
    DESTRUCTIVE = "destructive"
    FINANCIAL = "financial"
    ADMIN = "admin"


class TriggerKind(str, Enum):
    """Supported trigger categories."""

    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    CLOUDEVENT = "cloudevent"
    CATALOGUE_EVENT = "catalogue_event"


class RunMode(str, Enum):
    """How an automation may be started."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"
    APPROVAL_GATED = "approval_gated"
    DRY_RUN = "dry_run"


class ExecutionMode(str, Enum):
    """Supported Phase 6 execution modes."""

    DRY_RUN = "dry_run"
    SUPERVISED = "supervised"


class ControlPlaneRunStatus(str, Enum):
    """Public run status for control-plane correlation records."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    APPROVAL_REQUIRED = "approval_required"
    COMPLETED = "completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    PARTIALLY_COMPLETED = "partially_completed"


class RunStepStatus(str, Enum):
    """Deterministic run-step status for local execution evidence."""

    PLANNED = "planned"
    SIMULATED = "simulated"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStepType(str, Enum):
    """Supported bounded step types."""

    AGENT = "agent"
    CAPABILITY = "capability"
    APPROVAL_WAIT = "approval_wait"


class LifecycleState(str, Enum):
    """Lifecycle for registry resources."""

    DRAFT = "draft"
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REVIEWED = "reviewed"
    ACTIVE = "active"
    DISABLED = "disabled"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class PolicyDecision(str, Enum):
    """Policy decision for a capability or automation path."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecisionReason(str, Enum):
    """Deterministic reason codes for policy decision audit records."""

    ALLOW_READ_SAFE = "allow_read_safe"
    ALLOW_APPROVED = "allow_approved"
    DENY_UNKNOWN_WORKSPACE = "deny_unknown_workspace"
    DENY_UNKNOWN_CAPABILITY = "deny_unknown_capability"
    DENY_UNKNOWN_AUTOMATION = "deny_unknown_automation"
    DENY_AGENT_CAPABILITY_SCOPE = "deny_agent_capability_scope"
    DENY_INACTIVE_CAPABILITY = "deny_inactive_capability"
    DENY_INACTIVE_AUTOMATION = "deny_inactive_automation"
    DENY_EXPLICIT_POLICY = "deny_explicit_policy"
    DENY_ACTOR_ROLE = "deny_actor_role"
    DENY_POLICY_ATTRIBUTE = "deny_policy_attribute"
    DENY_POLICY_BUDGET = "deny_policy_budget"
    DENY_WORKSPACE_BUDGET = "deny_workspace_budget"
    DENY_SAFETY_COMPLIANCE = "deny_safety_compliance"
    DENY_MISSING_POLICY = "deny_missing_policy"
    REQUIRE_APPROVAL_POLICY = "require_approval_policy"
    REQUIRE_APPROVAL_DANGEROUS = "require_approval_dangerous"


class ImportSourceProtocol(str, Enum):
    """Supported import source protocol families."""

    MCP = "mcp"
    OPENAPI = "openapi"
    ASYNCAPI = "asyncapi"
    CLOUDEVENTS = "cloudevents"
    CATALOGUE = "catalogue"


class SecretStorageScope(str, Enum):
    """Where secret material is owned and allowed to live."""

    LOCAL_ONLY = "local_only"
    CLOUD = "cloud"
    CLIENT_OWNED = "client_owned"


class SyncDirection(str, Enum):
    """Allowed synchronization direction for a registry resource family."""

    NONE = "none"
    LOCAL_TO_CLOUD = "local_to_cloud"
    CLOUD_TO_LOCAL = "cloud_to_local"
    BIDIRECTIONAL = "bidirectional"


class SyncConflictStrategy(str, Enum):
    """Deterministic conflict handling for syncable resources."""

    LOCAL_WINS = "local_wins"
    CLOUD_WINS = "cloud_wins"
    NEWEST_VERSION_WINS = "newest_version_wins"
    MANUAL_REVIEW = "manual_review"


class ConsultantGrantStatus(str, Enum):
    """Lifecycle for consultant access to a client workspace."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class LocalApprovalNotificationChannel(str, Enum):
    """Bounded outbox channel a future delivery adapter may consume.

    These are display-safe channel labels only. Core never sends anything on
    any of them; the outbox is local evidence that a Platform/Cloud adapter can
    later pick up out of band. External labels do not imply addresses, URLs,
    payloads, secrets or active delivery.
    """

    LOCAL_INBOX = "local_inbox"
    IN_APP = "in_app"
    LOG = "log"
    EMAIL = "email"
    WEBHOOK = "webhook"


class LocalApprovalNotificationEvent(str, Enum):
    """Why a local approval notification was enqueued."""

    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_ASSIGNED = "approval_assigned"
    APPROVAL_ESCALATED = "approval_escalated"
    APPROVAL_TIMEOUT_PENDING = "approval_timeout_pending"
    APPROVAL_EXPIRED = "approval_expired"


class LocalApprovalNotificationStatus(str, Enum):
    """Local attempt lifecycle for an approval notification.

    The lifecycle is local-only bookkeeping: ``queued`` records that the outbox
    holds evidence a notification is wanted, while ``sent``/``failed``/
    ``suppressed`` record the result a future delivery adapter reported back.
    Core never transitions these itself by sending anything.
    """

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class ValidationResult:
    """Validation result for control-plane contract helpers."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkspaceRef:
    """Workspace owning a control-plane manifest."""

    id: str
    name: str = ""
    max_cost_units: int | None = None
    max_token_usage: int | None = None


@dataclass(frozen=True)
class SecretReference:
    """Reference to a secret stored outside public manifest payloads."""

    secret_ref: str
    provider: str = ""
    scope: str = ""


@dataclass(frozen=True)
class Connection:
    """Workspace-bound connection metadata."""

    id: str
    kind: ConnectionKind
    display_name: str = ""
    lifecycle: LifecycleState = LifecycleState.CANDIDATE
    secret_refs: list[SecretReference] = field(default_factory=list)
    catalogue_entry_id: str | None = None


@dataclass(frozen=True)
class Capability:
    """Callable or import-candidate capability mediated by the registry."""

    id: str
    capability_type: CapabilityType
    connection_id: str
    side_effect: SideEffect = SideEffect.NONE
    lifecycle: LifecycleState = LifecycleState.CANDIDATE
    display_name: str = ""
    import_record_id: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Agent:
    """Agent contract that may invoke capabilities, not raw connections."""

    id: str
    display_name: str = ""
    allowed_capabilities: list[str] = field(default_factory=list)
    run_mode: RunMode = RunMode.MANUAL


@dataclass(frozen=True)
class Trigger:
    """Trigger candidate or active trigger bound to a capability."""

    id: str
    kind: TriggerKind
    capability_id: str
    event_type: str = ""
    lifecycle: LifecycleState = LifecycleState.CANDIDATE
    schedule_rrule: str | None = None
    schedule_start_at: str | None = None
    cooldown_seconds: int | None = None
    debounce_seconds: int | None = None


@dataclass(frozen=True)
class PolicyAttributeCondition:
    """Structured actor/workspace attribute condition for policy evaluation.

    A condition reads a single actor or workspace attribute and applies one
    bounded operator. This is deliberately not an expression language: there is
    no CEL, regex, or arbitrary expression support. Numeric operators are
    limited to finite-number comparisons against a single configured value.
    """

    scope: str
    key: str
    operator: str
    value: str | None = None
    values: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyAttributeExpression:
    """Bounded structured boolean expression over attribute conditions.

    This is a portable, JSON-serializable expression tree, not an expression
    language. There is deliberately no CEL, eval, regex, or free-form text. A
    node is one of:

    - a leaf ``condition`` node that wraps exactly one
      :class:`PolicyAttributeCondition` and has no children; or
    - a boolean ``all``/``any`` node that combines a non-empty list of child
      nodes; or
    - a ``not`` node that negates exactly one child node.

    Validation bounds the tree depth and total node count so evaluation is
    always finite and deterministic. The same bounded condition operators used
    by :class:`PolicyAttributeCondition` apply at the leaves; nothing here adds
    new operators, functions, imports, or arbitrary expression text.
    """

    op: str
    children: list[PolicyAttributeExpression] = field(default_factory=list)
    condition: PolicyAttributeCondition | None = None


@dataclass(frozen=True)
class Policy:
    """Policy decision attached to capability or automation execution."""

    id: str
    decision: PolicyDecision
    capability_ids: list[str] = field(default_factory=list)
    automation_ids: list[str] = field(default_factory=list)
    allowed_actor_roles: list[str] = field(default_factory=list)
    required_actor_attributes: dict[str, str] = field(default_factory=dict)
    required_workspace_attributes: dict[str, str] = field(default_factory=dict)
    attribute_conditions: list[PolicyAttributeCondition] = field(default_factory=list)
    attribute_expression: PolicyAttributeExpression | None = None
    max_cost_units: int | None = None
    max_token_usage: int | None = None
    timeout_seconds: int | None = None
    retention_days: int | None = None
    safety_classification: str = ""
    compliance_blocked: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PolicyTemplate:
    """Reusable policy template applied into concrete Policy records.

    A template carries portable policy fields only. It never references
    capabilities or automations; the target ids are supplied when the template
    is applied into a normal :class:`Policy` record.
    """

    id: str
    display_name: str = ""
    decision: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL
    allowed_actor_roles: list[str] = field(default_factory=list)
    required_actor_attributes: dict[str, str] = field(default_factory=dict)
    required_workspace_attributes: dict[str, str] = field(default_factory=dict)
    attribute_conditions: list[PolicyAttributeCondition] = field(default_factory=list)
    attribute_expression: PolicyAttributeExpression | None = None
    max_cost_units: int | None = None
    max_token_usage: int | None = None
    timeout_seconds: int | None = None
    retention_days: int | None = None
    safety_classification: str = ""
    compliance_blocked: bool = False
    reason: str = ""
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class PolicyRulePack:
    """Named bundle of policy templates applied together as a unit."""

    id: str
    display_name: str = ""
    template_ids: list[str] = field(default_factory=list)
    lifecycle: LifecycleState = LifecycleState.CANDIDATE
    reason: str = ""


@dataclass(frozen=True)
class Approval:
    """Human approval coverage for higher-risk capability execution."""

    id: str
    capability_ids: list[str] = field(default_factory=list)
    automation_ids: list[str] = field(default_factory=list)
    approved: bool = False
    approver_role: str = ""
    actor_id: str | None = None
    comment: str | None = None
    timeout_seconds: int | None = None
    escalation_state: str = ""
    run_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    requested_at: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None
    # Display-safe human-assignment metadata. These carry only actor/role
    # identifiers, timestamps, and a redacted escalation reason; raw secret,
    # payload, prompt/output, or connection-wide data never lands here.
    assigned_actor_id: str | None = None
    assigned_role: str | None = None
    assigned_at: str | None = None
    escalated_at: str | None = None
    escalation_reason: str | None = None


@dataclass(frozen=True)
class Automation:
    """Automation wiring an agent, optional trigger, policy, and capability."""

    id: str
    agent_id: str
    capability_id: str
    trigger_id: str | None = None
    policy_ids: list[str] = field(default_factory=list)
    approval_ids: list[str] = field(default_factory=list)
    run_mode: RunMode = RunMode.MANUAL
    lifecycle: LifecycleState = LifecycleState.CANDIDATE
    max_steps: int | None = None
    max_cost_units: int | None = None
    max_token_usage: int | None = None
    max_retries: int | None = None


@dataclass(frozen=True)
class RunRecord:
    """Control-plane run correlation record; the run ledger remains canonical."""

    id: str
    automation_id: str
    status: ControlPlaneRunStatus
    run_ledger_ref: str | None = None
    run_ledger_entry_id: str | None = None
    trace_id: str | None = None
    retention_expires_at: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    resume_token: str | None = None
    resume_after: str | None = None
    resume_reason: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class AuditEvent:
    """Public-safe audit event reference."""

    id: str
    event_type: str
    resource_id: str = ""
    run_id: str | None = None


@dataclass(frozen=True)
class PolicyDecisionRecord:
    """Immutable, display-safe policy decision record."""

    id: str
    workspace_id: str
    decision: PolicyDecision
    reason_code: PolicyDecisionReason
    capability_id: str
    automation_id: str | None = None
    agent_id: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    policy_ids: list[str] = field(default_factory=list)
    approval_ids: list[str] = field(default_factory=list)
    audit_event_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class TriggerEventEnvelope:
    """CloudEvents-compatible local trigger event envelope."""

    id: str
    trigger_id: str
    event_type: str
    source: str = ""
    subject: str | None = None
    occurred_at: str | None = None
    idempotency_key: str | None = None
    actor_id: str | None = None
    data_ref: str | None = None


@dataclass(frozen=True)
class TriggerIngestionResult:
    """Result of pre-execution trigger ingestion."""

    accepted: bool
    run_record: RunRecord | None = None
    duplicate_of_run_id: str | None = None
    dead_letter_reason: str | None = None
    audit_event_id: str | None = None


@dataclass(frozen=True)
class RunStepRecord:
    """Display-safe run-step execution evidence."""

    id: str
    run_id: str
    step_type: RunStepType
    status: RunStepStatus
    capability_id: str | None = None
    agent_id: str | None = None
    policy_decision_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    audit_event_id: str | None = None
    attempt: int = 1
    error: str | None = None
    cost_units: int = 0
    token_usage: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class LocalModelInvocationRecord:
    """Display-safe local evidence for a bounded agent/model planning step."""

    id: str
    run_id: str
    step_id: str
    agent_id: str
    trace_id: str
    span_id: str
    model_provider: str = "not_invoked"
    model_name: str = "not_invoked"
    invocation_type: str = "planning_placeholder"
    prompt_redacted: bool = True
    output_redacted: bool = True
    token_usage: int = 0
    cost_units: int = 0
    audit_event_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    """Result from the Phase 6 dry-run/supervised runner."""

    run_record: RunRecord
    steps: list[RunStepRecord] = field(default_factory=list)
    policy_decision: PolicyDecisionRecord | None = None
    model_invocation: LocalModelInvocationRecord | None = None
    paused: bool = False
    error: str | None = None


@dataclass(frozen=True)
class LocalObservabilityLogRecord:
    """Redacted local log evidence retained in the control-plane event store."""

    id: str
    workspace_id: str
    run_id: str | None
    trace_id: str | None
    event_type: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    retention_expires_at: str | None = None
    audit_event_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class RunObservabilityMetrics:
    """Local-only observability counters for control-plane evidence views."""

    workspace_id: str
    run_count: int
    completed_count: int
    failed_count: int
    waiting_for_approval_count: int
    success_rate: float
    retry_count: int
    token_usage: int
    cost_units: int
    connector_health: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""


@dataclass(frozen=True)
class LocalUsageLedgerEntry:
    """Display-safe local usage attribution summary for a single subject.

    A subject is an actor, cost center, or workspace. The entry carries only
    aggregated, display-safe counters (run count, token usage, cost units) and
    never any raw prompts, outputs, secrets, or payload bodies. Usage follows
    the same model-supersedes-capability accounting as
    ``RunObservabilityMetrics`` so each run's usage is counted exactly once.
    """

    workspace_id: str
    subject_type: str
    subject_id: str
    run_count: int
    token_usage: int
    cost_units: int
    generated_at: str = ""


@dataclass(frozen=True)
class LocalApprovalNotification:
    """Display-safe local outbox record for an approval notification.

    This is local notification-workflow evidence only. It carries actor/role
    identifiers, deterministic event/channel/status labels, timestamps, attempt
    counts, and a redacted reason/comment/error. It never holds secret material,
    raw payloads, prompt text, provider output, connection handles, or full
    manifests. A future Platform/Cloud delivery adapter consumes these records
    out of band; Core never sends anything.
    """

    id: str
    run_id: str
    workspace_id: str
    channel: LocalApprovalNotificationChannel
    event_type: LocalApprovalNotificationEvent
    status: LocalApprovalNotificationStatus = LocalApprovalNotificationStatus.QUEUED
    approval_id: str | None = None
    capability_id: str | None = None
    automation_id: str | None = None
    recipient_actor_id: str | None = None
    recipient_role: str | None = None
    reason: str | None = None
    comment: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    audit_event_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ImportRecord:
    """Imported MCP/OpenAPI/AsyncAPI/catalogue metadata, non-runtime state."""

    id: str
    source_protocol: ImportSourceProtocol
    source_ref: str
    candidate_capability_ids: list[str] = field(default_factory=list)
    released_capability_ids: list[str] = field(default_factory=list)
    source_hash: str | None = None
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class SecretMetadata:
    """Display-safe metadata for secret material stored outside manifests."""

    id: str
    secret_ref: str
    owner_workspace_id: str
    storage_scope: SecretStorageScope = SecretStorageScope.LOCAL_ONLY
    provider: str = ""
    client_owned: bool = False
    syncable: bool = False
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class SecretResolutionResult:
    """Display-safe result of resolving a secret reference from a local vault."""

    secret_ref: str
    resolved: bool
    provider: str = ""
    storage_scope: SecretStorageScope = SecretStorageScope.LOCAL_ONLY
    client_owned: bool = False
    audit_event_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SyncRule:
    """Sync eligibility and deterministic conflict policy for resources."""

    id: str
    resource_type: str
    direction: SyncDirection = SyncDirection.NONE
    conflict_strategy: SyncConflictStrategy = SyncConflictStrategy.MANUAL_REVIEW
    include_lifecycle_states: list[LifecycleState] = field(default_factory=list)
    audit_required: bool = True
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class TenantIsolationRule:
    """Client/tenant isolation requirement for a workspace boundary."""

    id: str
    tenant_id: str
    workspace_id: str
    client_workspace_id: str | None = None
    data_boundary: str = "workspace"
    cross_client_sharing_allowed: bool = False
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class ConsultantAccessGrant:
    """Display-safe consultant grant for a client-owned workspace."""

    id: str
    consultant_id: str
    client_workspace_id: str
    role: str
    policy_ids: list[str] = field(default_factory=list)
    status: ConsultantGrantStatus = ConsultantGrantStatus.ACTIVE
    granted_by: str = ""
    granted_at: str = ""
    revoked_at: str | None = None
    lifecycle: LifecycleState = LifecycleState.CANDIDATE


@dataclass(frozen=True)
class ControlPlaneManifest:
    """Portable manifest for control-plane registry resources."""

    workspace: WorkspaceRef
    schema_version: str = CONTROL_PLANE_SCHEMA_VERSION
    connections: list[Connection] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    automations: list[Automation] = field(default_factory=list)
    policies: list[Policy] = field(default_factory=list)
    policy_templates: list[PolicyTemplate] = field(default_factory=list)
    policy_rule_packs: list[PolicyRulePack] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    run_records: list[RunRecord] = field(default_factory=list)
    audit_events: list[AuditEvent] = field(default_factory=list)
    import_records: list[ImportRecord] = field(default_factory=list)
    secret_metadata: list[SecretMetadata] = field(default_factory=list)
    sync_rules: list[SyncRule] = field(default_factory=list)
    tenant_isolation_rules: list[TenantIsolationRule] = field(default_factory=list)
    consultant_access_grants: list[ConsultantAccessGrant] = field(default_factory=list)
    local_approval_notifications: list[LocalApprovalNotification] = field(
        default_factory=list
    )
    contract_version: ContractVersion = CONTROL_PLANE_CONTRACT_VERSION
