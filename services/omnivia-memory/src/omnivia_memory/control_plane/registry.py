"""Local-first control-plane registry persistence."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, TypeVar
from uuid import uuid4

from omnivia_memory.control_plane.models import (
    CONTROL_PLANE_SCHEMA_VERSION,
    Approval,
    Automation,
    Capability,
    Connection,
    ConsultantAccessGrant,
    ConsultantGrantStatus,
    ControlPlaneManifest,
    ControlPlaneRunStatus,
    ExecutionMode,
    ExecutionResult,
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
    TriggerKind,
    TriggerEventEnvelope,
    TriggerIngestionResult,
)
from omnivia_memory.control_plane.validation import (
    DANGEROUS_SIDE_EFFECTS,
    manifest_from_dict,
    validate_control_plane_manifest,
)
from omnivia_memory.persistence.database import Database
from omnivia_memory.run_ledger import (
    RUN_LEDGER_PATH_ENV,
    EvidenceFileRef,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
)


# Element type of a manifest resource list, so `_find_resource` returns the
# concrete resource type its caller passed in rather than `Any`. Private so it
# stays out of the frozen Phase 0 public export inventory.
_ResourceT = TypeVar("_ResourceT")


RESOURCE_LIST_FIELDS = (
    ("connections", "connection"),
    ("capabilities", "capability"),
    ("agents", "agent"),
    ("triggers", "trigger"),
    ("automations", "automation"),
    ("policies", "policy"),
    ("policy_templates", "policy_template"),
    ("policy_rule_packs", "policy_rule_pack"),
    ("approvals", "approval"),
    ("run_records", "run_record"),
    ("audit_events", "audit_event"),
    ("import_records", "import_record"),
    ("secret_metadata", "secret_metadata"),
    ("sync_rules", "sync_rule"),
    ("tenant_isolation_rules", "tenant_isolation_rule"),
    ("consultant_access_grants", "consultant_access_grant"),
    ("local_approval_notifications", "local_approval_notification"),
)

SENSITIVE_OBSERVABILITY_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "secret",
    "session",
    "token",
)

# Markers for executor diagnostic/metadata keys that must never reach
# display-safe capability invocation evidence. These cover raw secret material,
# Authorization headers, model prompt/output text, provider response bodies, and
# raw client/connection/MCP/OpenAPI handles. A key containing any marker (case
# insensitive) is dropped entirely before the remaining payload is redacted.
FORBIDDEN_EXECUTOR_METADATA_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client",
    "client_secret",
    "completion",
    "connection",
    "credential",
    "handle",
    "mcp",
    "model_output",
    "openapi",
    "password",
    "private_key",
    "prompt",
    "raw_response",
    "response_body",
    "secret",
    "token",
)
SAFE_EXECUTOR_SIDE_EFFECTS = frozenset({"none", "read"})

IN_FLIGHT_RUN_STATUSES = frozenset(
    {
        ControlPlaneRunStatus.PENDING,
        ControlPlaneRunStatus.QUEUED,
        ControlPlaneRunStatus.RUNNING,
        ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
        ControlPlaneRunStatus.APPROVAL_REQUIRED,
        ControlPlaneRunStatus.BLOCKED,
    }
)
TERMINAL_RUN_STATUSES = frozenset(
    {
        ControlPlaneRunStatus.COMPLETED,
        ControlPlaneRunStatus.SUCCEEDED,
        ControlPlaneRunStatus.FAILED,
        ControlPlaneRunStatus.CANCELLED,
        ControlPlaneRunStatus.REJECTED,
        ControlPlaneRunStatus.TIMED_OUT,
        ControlPlaneRunStatus.PARTIALLY_COMPLETED,
    }
)
RETRYABLE_RUN_STATUSES = frozenset(
    {
        ControlPlaneRunStatus.FAILED,
        ControlPlaneRunStatus.TIMED_OUT,
        ControlPlaneRunStatus.PARTIALLY_COMPLETED,
        ControlPlaneRunStatus.BLOCKED,
    }
)

# Deterministic local retry backoff defaults. These materialize queue metadata
# only; no daemon, scheduler, or worker consumes them in Core.
DEFAULT_RETRY_BASE_DELAY_SECONDS = 30
DEFAULT_RETRY_MAX_DELAY_SECONDS = 3600

# Resume reason that record_approval_decision stamps on a run when a human
# approves its paused capability. The local post-approval worker pump claims
# only queued runs carrying this reason, so normal retry/trigger/resume queued
# work is never picked up by the approval-resume path.
APPROVAL_RESUME_REASON = "approval_approved"


@dataclass(frozen=True)
class ControlPlaneResourceRecord:
    """Display-safe registry record without secret-bearing payloads."""

    workspace_id: str
    resource_type: str
    resource_id: str
    lifecycle: str
    display_name: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CapabilityExecutionContext:
    """Sanitized invocation context handed to a supervised capability executor.

    The context carries only display-safe identifiers and schema summaries so a
    caller-supplied executor can perform an approved safe/read capability call
    out of Core. It never includes raw secret values, ``secret://`` references,
    raw client objects, MCP/OpenAPI clients, connection-wide handles, prompt
    text, model output, provider response bodies, or the full manifest. The
    ``connection_id``/``connection_kind`` are display-safe hints only; the
    executor is responsible for resolving any real connection out of band.
    """

    workspace_id: str
    run_id: str
    capability_id: str
    side_effect: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    trace_id: str
    span_id: str
    policy_decision_id: str
    connection_id: str | None = None
    connection_kind: str | None = None
    input_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityExecutionOutput:
    """Display-safe result returned by a supervised capability executor.

    ``output`` is validated against the capability output schema and drives the
    capability invocation evidence. ``diagnostics`` is optional safe metadata;
    it is sanitized (forbidden keys dropped, remaining values redacted) before
    any of it is recorded.
    """

    output: Mapping[str, Any]
    diagnostics: Mapping[str, Any] | None = None


# A supervised capability executor receives a sanitized context and returns a
# display-safe output payload (optionally wrapped with safe diagnostics).
CapabilityExecutor = Callable[
    [CapabilityExecutionContext], "CapabilityExecutionOutput | Mapping[str, Any]"
]


class ControlPlaneRegistryError(ValueError):
    """Raised when a registry operation would create invalid state."""


class ControlPlaneRegistry:
    """SQLite-backed local control-plane registry.

    The registry stores definitions and lifecycle state only. It does not
    execute automations, call connectors, resolve secrets, or expose raw
    resource payloads through display records.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # The return annotation is deliberately omitted: `inspect.signature` feeds
    # the frozen Phase 0 public export inventory, which pins this method's
    # rendered signature. Annotating the return would be public-surface drift,
    # so the strict-mypy rule is suppressed on this one line instead.
    def validate_manifest(  # type: ignore[no-untyped-def]
        self, manifest: ControlPlaneManifest | dict[str, Any]
    ):
        """Validate a manifest with the Core control-plane contract rules."""

        return validate_control_plane_manifest(manifest)

    def create_manifest(
        self, manifest: ControlPlaneManifest | dict[str, Any]
    ) -> ControlPlaneManifest:
        """Create a workspace manifest if one does not already exist."""

        manifest_obj = self._coerce_valid_manifest(manifest)
        if self.get_manifest(manifest_obj.workspace.id) is not None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {manifest_obj.workspace.id} already exists"
            )
        self._store_manifest(manifest_obj, event_type="manifest.created")
        return manifest_obj

    def update_manifest(
        self, manifest: ControlPlaneManifest | dict[str, Any]
    ) -> ControlPlaneManifest:
        """Replace a workspace manifest after validation."""

        manifest_obj = self._coerce_valid_manifest(manifest)
        self._store_manifest(manifest_obj, event_type="manifest.updated")
        return manifest_obj

    def store_manifest(
        self, manifest: ControlPlaneManifest | dict[str, Any]
    ) -> ControlPlaneManifest:
        """Create or update a manifest after validation."""

        manifest_obj = self._coerce_valid_manifest(manifest)
        event_type = (
            "manifest.updated"
            if self.get_manifest(manifest_obj.workspace.id) is not None
            else "manifest.created"
        )
        self._store_manifest(manifest_obj, event_type=event_type)
        return manifest_obj

    def get_manifest(self, workspace_id: str) -> ControlPlaneManifest | None:
        """Load a workspace manifest from stored resource rows."""

        cursor = self.db.execute(
            """
            SELECT schema_version, payload_json FROM control_plane_manifests
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        )
        manifest_row = cursor.fetchone()
        if manifest_row is None:
            return None

        payload: dict[str, Any] = json.loads(manifest_row["payload_json"])
        payload["schema_version"] = manifest_row["schema_version"]
        payload.setdefault("workspace", {"id": workspace_id})
        payload["workspace"]["id"] = workspace_id
        for field_name, resource_type in RESOURCE_LIST_FIELDS:
            rows = self.db.execute(
                """
                SELECT payload_json FROM control_plane_resources
                WHERE workspace_id = ? AND resource_type = ?
                ORDER BY resource_id
                """,
                (workspace_id, resource_type),
            ).fetchall()
            payload[field_name] = [json.loads(row["payload_json"]) for row in rows]
        return manifest_from_dict(payload)

    def list_display_records(
        self,
        workspace_id: str,
        resource_type: str | None = None,
        lifecycle: LifecycleState | None = None,
    ) -> list[ControlPlaneResourceRecord]:
        """List display-safe registry records for Platform or Dev surfaces."""

        conditions = ["workspace_id = ?"]
        params: list[Any] = [workspace_id]
        if resource_type is not None:
            conditions.append("resource_type = ?")
            params.append(resource_type)
        if lifecycle is not None:
            conditions.append("lifecycle = ?")
            params.append(lifecycle.value)

        cursor = self.db.execute(
            f"""
            SELECT workspace_id, resource_type, resource_id, lifecycle,
                   payload_json, updated_at
            FROM control_plane_resources
            WHERE {" AND ".join(conditions)}
            ORDER BY resource_type, resource_id
            """,
            tuple(params),
        )
        records: list[ControlPlaneResourceRecord] = []
        for row in cursor.fetchall():
            payload = json.loads(row["payload_json"])
            records.append(
                ControlPlaneResourceRecord(
                    workspace_id=row["workspace_id"],
                    resource_type=row["resource_type"],
                    resource_id=row["resource_id"],
                    lifecycle=row["lifecycle"],
                    display_name=str(payload.get("display_name", "")),
                    updated_at=row["updated_at"],
                )
            )
        return records

    def activate_resource(self, workspace_id: str, resource_type: str, resource_id: str) -> None:
        """Activate a resource only if the resulting manifest still validates."""

        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.ACTIVE
        )

    def disable_resource(self, workspace_id: str, resource_type: str, resource_id: str) -> None:
        """Disable a resource without deleting its audit/provenance trail."""

        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.DISABLED
        )

    def review_candidate(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Mark an imported candidate reviewed without activating it."""

        if resource_type not in {"connection", "capability", "trigger"}:
            raise ControlPlaneRegistryError(
                f"{resource_type} candidates cannot be reviewed through this API"
            )
        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.REVIEWED
        )
        self._record_event(
            workspace_id,
            "candidate.reviewed",
            resource_type,
            resource_id,
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def promote_reviewed_candidate(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Promote a reviewed candidate to active runtime state after validation."""

        if resource_type not in {"connection", "capability", "trigger"}:
            raise ControlPlaneRegistryError(
                f"{resource_type} candidates cannot be promoted through this API"
            )
        record = self._load_resource_payload(workspace_id, resource_type, resource_id)
        if record.get("lifecycle") != LifecycleState.REVIEWED.value:
            raise ControlPlaneRegistryError(
                f"{resource_type} {resource_id} must be reviewed before promotion"
            )
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        _validate_candidate_promotion_ready(manifest, resource_type, resource_id)
        imported_capability_promoted = (
            resource_type == "capability"
            and self._promote_imported_capability(
                workspace_id,
                manifest,
                resource_id,
            )
        )
        if not imported_capability_promoted:
            self._set_lifecycle(
                workspace_id, resource_type, resource_id, LifecycleState.ACTIVE
            )
        self._record_event(
            workspace_id,
            "candidate.promoted",
            resource_type,
            resource_id,
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def _promote_imported_capability(
        self,
        workspace_id: str,
        manifest: ControlPlaneManifest,
        capability_id: str,
    ) -> bool:
        imported = [
            record
            for record in manifest.import_records
            if capability_id in record.candidate_capability_ids
        ]
        if not imported:
            return False

        import_records = []
        for record in manifest.import_records:
            if capability_id not in record.candidate_capability_ids:
                import_records.append(record)
                continue
            released_capability_ids = sorted({
                *record.released_capability_ids,
                capability_id,
            })
            import_records.append(
                replace(record, released_capability_ids=released_capability_ids)
            )

        capabilities = [
            replace(capability, lifecycle=LifecycleState.ACTIVE)
            if capability.id == capability_id
            else capability
            for capability in manifest.capabilities
        ]
        released_manifest = replace(
            manifest,
            capabilities=capabilities,
            import_records=import_records,
        )
        result = validate_control_plane_manifest(released_manifest)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))

        self._store_manifest(released_manifest, event_type="manifest.import_release.updated")
        self._record_event(
            workspace_id,
            "resource.active",
            "capability",
            capability_id,
            {
                "lifecycle": LifecycleState.ACTIVE.value,
                "import_release": True,
            },
            _now(),
        )
        return True

    def reject_candidate(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Reject an inactive candidate without activating or deleting it."""

        if resource_type not in {"connection", "capability", "trigger"}:
            raise ControlPlaneRegistryError(
                f"{resource_type} candidates cannot be rejected through this API"
            )
        record = self._load_resource_payload(workspace_id, resource_type, resource_id)
        if record.get("lifecycle") == LifecycleState.ACTIVE.value:
            raise ControlPlaneRegistryError(
                f"{resource_type} {resource_id} is active and cannot be rejected"
            )
        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.REJECTED
        )
        self._record_event(
            workspace_id,
            "candidate.rejected",
            resource_type,
            resource_id,
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def bind_connection_secret_reference(
        self,
        workspace_id: str,
        connection_id: str,
        *,
        secret_ref: str,
        provider: str = "local-keychain",
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Bind display-safe secret metadata to a connection without secret material."""

        if not secret_ref.startswith("secret://"):
            raise ControlPlaneRegistryError("secret_ref must be a secret:// reference")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        connection = _find_resource(manifest.connections, connection_id)
        if connection is None:
            raise ControlPlaneRegistryError(f"connection {connection_id} does not exist")
        if _contains_raw_secret_material(secret_ref) or _contains_raw_secret_material(provider):
            raise ControlPlaneRegistryError("secret binding metadata contains raw secret material")

        secret_reference = SecretReference(secret_ref=secret_ref, provider=provider)
        secret_metadata = SecretMetadata(
            id=f"secret.metadata.{_resource_id_slug(connection_id)}",
            secret_ref=secret_ref,
            owner_workspace_id=workspace_id,
            storage_scope=SecretStorageScope.LOCAL_ONLY,
            provider=provider,
            syncable=False,
            lifecycle=LifecycleState.ACTIVE,
        )
        connections = [
            replace(
                item,
                secret_refs=_append_unique_secret_ref(item.secret_refs, secret_reference),
            )
            if item.id == connection_id
            else item
            for item in manifest.connections
        ]
        secret_metadata_records = [
            item
            for item in manifest.secret_metadata
            if item.secret_ref != secret_ref and item.id != secret_metadata.id
        ]
        secret_metadata_records.append(secret_metadata)
        updated = replace(
            manifest,
            connections=connections,
            secret_metadata=secret_metadata_records,
        )
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "connection.secret_ref.bound",
            "connection",
            connection_id,
            {
                "connection_id": connection_id,
                "secret_ref": secret_ref,
                "provider": provider,
                "actor_id": actor_id,
                "comment": comment,
                "secret_value_redacted": True,
            },
            _now(),
        )

    def add_capability_policy_coverage(
        self,
        workspace_id: str,
        capability_id: str,
        *,
        decision: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL,
        policy_id: str | None = None,
        reason: str = "Policy coverage required before promotion.",
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Add explicit policy coverage for a capability before promotion."""

        if decision not in {PolicyDecision.ALLOW, PolicyDecision.REQUIRE_APPROVAL}:
            raise ControlPlaneRegistryError("policy coverage must allow or require approval")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        capability = _find_resource(manifest.capabilities, capability_id)
        if capability is None:
            raise ControlPlaneRegistryError(f"capability {capability_id} does not exist")
        next_policy_id = policy_id or f"policy.coverage.{_resource_id_slug(capability_id)}"
        policies = [
            item for item in manifest.policies if item.id != next_policy_id
        ]
        policies.append(
            Policy(
                id=next_policy_id,
                decision=decision,
                capability_ids=[capability_id],
                reason=reason,
            )
        )
        updated = replace(manifest, policies=policies)
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "capability.policy.coverage.added",
            "capability",
            capability_id,
            {
                "capability_id": capability_id,
                "policy_id": next_policy_id,
                "decision": decision.value,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def author_policy(
        self,
        workspace_id: str,
        policy: Policy,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Create or replace a single local policy after manifest validation.

        The policy is matched by id: an existing policy with the same id is
        replaced, otherwise the policy is appended. The resulting manifest is
        validated through the existing control-plane validation path before it
        is stored, so unknown referenced capability_ids/automation_ids, empty
        ids, and other rule violations fail closed.
        """

        if not isinstance(policy, Policy):
            raise ControlPlaneRegistryError("policy must be a Policy instance")
        if not policy.id or not policy.id.strip():
            raise ControlPlaneRegistryError("policy id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        policies = [item for item in manifest.policies if item.id != policy.id]
        is_update = len(policies) != len(manifest.policies)
        policies.append(policy)
        updated = replace(manifest, policies=policies)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.updated" if is_update else "policy.authored",
            "policy",
            policy.id,
            {
                "policy_id": policy.id,
                "decision": policy.decision.value,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def remove_policy(
        self,
        workspace_id: str,
        policy_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Remove a local policy when no automation still references it.

        Removal fails closed if the policy is referenced by any
        automation.policy_ids; the resulting manifest is validated before it is
        stored, and a display-safe event without raw secret or attribute values
        is recorded.
        """

        if not policy_id or not policy_id.strip():
            raise ControlPlaneRegistryError("policy id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        policy = _find_resource(manifest.policies, policy_id)
        if policy is None:
            raise ControlPlaneRegistryError(f"policy {policy_id} does not exist")
        referencing = sorted(
            automation.id
            for automation in manifest.automations
            if policy_id in automation.policy_ids
        )
        if referencing:
            raise ControlPlaneRegistryError(
                f"policy {policy_id} is referenced by automations "
                f"{', '.join(referencing)}"
            )
        policies = [item for item in manifest.policies if item.id != policy_id]
        updated = replace(manifest, policies=policies)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.removed",
            "policy",
            policy_id,
            {
                "policy_id": policy_id,
                "decision": policy.decision.value,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def author_policy_template(
        self,
        workspace_id: str,
        template: PolicyTemplate,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Create or replace a reusable policy template after validation.

        The template is matched by id: an existing template with the same id is
        replaced, otherwise it is appended. The resulting manifest is validated
        through the existing control-plane validation path before it is stored.
        Templates carry portable policy fields only; capability/automation
        targets are bound when the template is applied.
        """

        if not isinstance(template, PolicyTemplate):
            raise ControlPlaneRegistryError(
                "template must be a PolicyTemplate instance"
            )
        if not template.id or not template.id.strip():
            raise ControlPlaneRegistryError("policy template id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        templates = [
            item for item in manifest.policy_templates if item.id != template.id
        ]
        is_update = len(templates) != len(manifest.policy_templates)
        templates.append(template)
        updated = replace(manifest, policy_templates=templates)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.template.updated" if is_update else "policy.template.authored",
            "policy_template",
            template.id,
            {
                "policy_template_id": template.id,
                "decision": template.decision.value,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def remove_policy_template(
        self,
        workspace_id: str,
        template_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Remove a policy template when no rule pack still references it."""

        if not template_id or not template_id.strip():
            raise ControlPlaneRegistryError("policy template id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        template = _find_resource(manifest.policy_templates, template_id)
        if template is None:
            raise ControlPlaneRegistryError(
                f"policy template {template_id} does not exist"
            )
        referencing = sorted(
            rule_pack.id
            for rule_pack in manifest.policy_rule_packs
            if template_id in rule_pack.template_ids
        )
        if referencing:
            raise ControlPlaneRegistryError(
                f"policy template {template_id} is referenced by rule packs "
                f"{', '.join(referencing)}"
            )
        templates = [
            item for item in manifest.policy_templates if item.id != template_id
        ]
        updated = replace(manifest, policy_templates=templates)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.template.removed",
            "policy_template",
            template_id,
            {
                "policy_template_id": template_id,
                "decision": template.decision.value,
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def author_policy_rule_pack(
        self,
        workspace_id: str,
        rule_pack: PolicyRulePack,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Create or replace a policy rule pack after validation.

        The rule pack is matched by id and validated through the existing
        control-plane validation path, so a rule pack that references an unknown
        policy template id fails closed before it is stored.
        """

        if not isinstance(rule_pack, PolicyRulePack):
            raise ControlPlaneRegistryError(
                "rule_pack must be a PolicyRulePack instance"
            )
        if not rule_pack.id or not rule_pack.id.strip():
            raise ControlPlaneRegistryError("policy rule pack id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        rule_packs = [
            item for item in manifest.policy_rule_packs if item.id != rule_pack.id
        ]
        is_update = len(rule_packs) != len(manifest.policy_rule_packs)
        rule_packs.append(rule_pack)
        updated = replace(manifest, policy_rule_packs=rule_packs)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.rule_pack.updated" if is_update else "policy.rule_pack.authored",
            "policy_rule_pack",
            rule_pack.id,
            {
                "policy_rule_pack_id": rule_pack.id,
                "template_ids": list(rule_pack.template_ids),
                "template_count": len(rule_pack.template_ids),
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def remove_policy_rule_pack(
        self,
        workspace_id: str,
        rule_pack_id: str,
        *,
        actor_id: str | None = None,
        comment: str = "",
    ) -> None:
        """Remove a policy rule pack without deleting its referenced templates."""

        if not rule_pack_id or not rule_pack_id.strip():
            raise ControlPlaneRegistryError("policy rule pack id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        rule_pack = _find_resource(manifest.policy_rule_packs, rule_pack_id)
        if rule_pack is None:
            raise ControlPlaneRegistryError(
                f"policy rule pack {rule_pack_id} does not exist"
            )
        rule_packs = [
            item for item in manifest.policy_rule_packs if item.id != rule_pack_id
        ]
        updated = replace(manifest, policy_rule_packs=rule_packs)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.rule_pack.removed",
            "policy_rule_pack",
            rule_pack_id,
            {
                "policy_rule_pack_id": rule_pack_id,
                "template_count": len(rule_pack.template_ids),
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )

    def apply_policy_template(
        self,
        workspace_id: str,
        template_id: str,
        *,
        capability_ids: list[str] | None = None,
        automation_ids: list[str] | None = None,
        policy_id: str | None = None,
        actor_id: str | None = None,
        comment: str = "",
    ) -> Policy:
        """Apply a policy template into a concrete Policy record.

        Creates or replaces a normal :class:`Policy` (matched by id) that copies
        all template fields and binds the supplied capability/automation
        targets. The resulting manifest is validated through the existing path,
        so unknown capability/automation targets fail closed. The created policy
        is returned.
        """

        if not template_id or not template_id.strip():
            raise ControlPlaneRegistryError("policy template id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        template = _find_resource(manifest.policy_templates, template_id)
        if template is None:
            raise ControlPlaneRegistryError(
                f"policy template {template_id} does not exist"
            )
        target_policy_id = (
            policy_id or f"policy.template.{_resource_id_slug(template_id)}"
        )
        policy = _policy_from_template(
            template,
            target_policy_id,
            capability_ids,
            automation_ids,
        )
        policies = [item for item in manifest.policies if item.id != policy.id]
        policies.append(policy)
        updated = replace(manifest, policies=policies)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.template.applied",
            "policy_template",
            template_id,
            {
                "policy_template_id": template_id,
                "policy_id": policy.id,
                "decision": policy.decision.value,
                "capability_count": len(policy.capability_ids),
                "automation_count": len(policy.automation_ids),
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )
        return policy

    def apply_policy_rule_pack(
        self,
        workspace_id: str,
        rule_pack_id: str,
        *,
        capability_ids: list[str] | None = None,
        automation_ids: list[str] | None = None,
        policy_id_prefix: str | None = None,
        actor_id: str | None = None,
        comment: str = "",
    ) -> list[Policy]:
        """Apply every template referenced by a rule pack into Policy records.

        Each referenced template is applied with a deterministic policy id:
        ``<policy_id_prefix>.<template slug>`` when a prefix is supplied,
        otherwise ``policy.rule_pack.<rule pack slug>.<template slug>``. The
        manifest is validated once before it is stored, so unknown
        capability/automation targets fail closed and no partial state lands.
        The created policies are returned.
        """

        if not rule_pack_id or not rule_pack_id.strip():
            raise ControlPlaneRegistryError("policy rule pack id is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        rule_pack = _find_resource(manifest.policy_rule_packs, rule_pack_id)
        if rule_pack is None:
            raise ControlPlaneRegistryError(
                f"policy rule pack {rule_pack_id} does not exist"
            )
        rule_pack_slug = _resource_id_slug(rule_pack_id)
        created: list[Policy] = []
        for template_id in rule_pack.template_ids:
            template = _find_resource(manifest.policy_templates, template_id)
            if template is None:
                raise ControlPlaneRegistryError(
                    f"policy template {template_id} does not exist"
                )
            template_slug = _resource_id_slug(template_id)
            target_policy_id = (
                f"{policy_id_prefix}.{template_slug}"
                if policy_id_prefix
                else f"policy.rule_pack.{rule_pack_slug}.{template_slug}"
            )
            created.append(
                _policy_from_template(
                    template,
                    target_policy_id,
                    capability_ids,
                    automation_ids,
                )
            )

        created_ids = {policy.id for policy in created}
        policies = [item for item in manifest.policies if item.id not in created_ids]
        policies.extend(created)
        updated = replace(manifest, policies=policies)
        result = validate_control_plane_manifest(updated)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated, event_type="manifest.updated")
        self._record_event(
            workspace_id,
            "policy.rule_pack.applied",
            "policy_rule_pack",
            rule_pack_id,
            {
                "policy_rule_pack_id": rule_pack_id,
                "template_ids": list(rule_pack.template_ids),
                "policy_ids": [policy.id for policy in created],
                "policy_count": len(created),
                "actor_id": actor_id,
                "comment": comment,
            },
            _now(),
        )
        return created

    def deprecate_resource(
        self, workspace_id: str, resource_type: str, resource_id: str
    ) -> None:
        """Mark a resource deprecated."""

        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.DEPRECATED
        )

    def archive_resource(self, workspace_id: str, resource_type: str, resource_id: str) -> None:
        """Archive a resource."""

        self._set_lifecycle(
            workspace_id, resource_type, resource_id, LifecycleState.ARCHIVED
        )

    def record_consultant_action(
        self,
        workspace_id: str,
        *,
        consultant_id: str,
        grant_id: str,
        action: str,
        policy_id: str,
        actor_role: str = "consultant",
    ) -> PolicyDecisionRecord:
        """Audit a consultant action against a client workspace grant.

        This records identity, client workspace, grant status, policy coverage
        and policy decision only. It does not execute a capability or resolve
        client-owned secrets.
        """

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_UNKNOWN_WORKSPACE,
                capability_id=action,
                actor_id=consultant_id,
                actor_role=actor_role,
                policy_ids=[policy_id],
            )

        grant = _find_resource(manifest.consultant_access_grants, grant_id)
        policy = _find_resource(manifest.policies, policy_id)
        if (
            grant is None
            or grant.consultant_id != consultant_id
            or grant.client_workspace_id != workspace_id
            or grant.status.value != "active"
            or policy is None
            or policy_id not in grant.policy_ids
        ):
            decision = self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_EXPLICIT_POLICY,
                capability_id=action,
                actor_id=consultant_id,
                actor_role=actor_role,
                policy_ids=[policy_id],
            )
            self._record_consultant_audit_event(
                workspace_id,
                consultant_id,
                grant_id,
                action,
                decision,
            )
            return decision

        decision = self._record_policy_decision(
            workspace_id=workspace_id,
            decision=policy.decision,
            reason_code=(
                PolicyDecisionReason.ALLOW_APPROVED
                if policy.decision == PolicyDecision.ALLOW
                else PolicyDecisionReason.REQUIRE_APPROVAL_POLICY
                if policy.decision == PolicyDecision.REQUIRE_APPROVAL
                else PolicyDecisionReason.DENY_EXPLICIT_POLICY
            ),
            capability_id=action,
            actor_id=consultant_id,
            actor_role=actor_role,
            policy_ids=[policy_id],
        )
        self._record_consultant_audit_event(
            workspace_id,
            consultant_id,
            grant_id,
            action,
            decision,
        )
        return decision

    def revoke_consultant_access(
        self,
        workspace_id: str,
        grant_id: str,
        *,
        actor_id: str,
        reason: str = "",
        revoked_at: str | None = None,
    ) -> ConsultantAccessGrant:
        """Revoke a consultant grant and audit the local registry mutation.

        This mutates only the display-safe grant metadata in the local registry.
        It does not resolve client-owned secrets, execute capabilities, or sync
        to Cloud.
        """

        payload = self._load_resource_payload(
            workspace_id,
            "consultant_access_grant",
            grant_id,
        )
        if payload.get("client_workspace_id") != workspace_id:
            raise ControlPlaneRegistryError(
                f"consultant grant {grant_id} does not belong to {workspace_id}"
            )

        now = revoked_at or _now()
        payload["status"] = ConsultantGrantStatus.REVOKED.value
        payload["revoked_at"] = now
        manifest_payload = self._manifest_payload_with_resource(
            workspace_id,
            "consultant_access_grant",
            grant_id,
            payload,
        )
        result = validate_control_plane_manifest(manifest_payload)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        updated_manifest = manifest_from_dict(manifest_payload)
        updated_grant = _find_resource(updated_manifest.consultant_access_grants, grant_id)
        if updated_grant is None:
            raise ControlPlaneRegistryError(
                f"consultant grant {grant_id} does not exist in {workspace_id}"
            )

        self.db.execute(
            """
            UPDATE control_plane_resources
            SET payload_json = ?, updated_at = ?
            WHERE workspace_id = ? AND resource_type = 'consultant_access_grant'
              AND resource_id = ?
            """,
            (
                json.dumps(payload, sort_keys=True),
                now,
                workspace_id,
                grant_id,
            ),
        )
        self._record_event(
            workspace_id,
            "consultant.grant.revoked",
            "consultant_access_grant",
            grant_id,
            {
                "grant_id": grant_id,
                "consultant_id": str(payload.get("consultant_id", "")),
                "client_workspace_id": workspace_id,
                "actor_id": actor_id,
                "reason": reason,
                "revoked_at": now,
            },
            now,
        )
        return updated_grant

    def evaluate_capability_access(
        self,
        workspace_id: str,
        capability_id: str,
        *,
        automation_id: str | None = None,
        agent_id: str | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
        actor_attributes: Mapping[str, str] | None = None,
        workspace_attributes: Mapping[str, str] | None = None,
        estimated_cost_units: int = 0,
        estimated_token_usage: int = 0,
    ) -> PolicyDecisionRecord:
        """Evaluate capability access and record an immutable audit event.

        This is a policy/audit decision surface only. It never executes a
        capability, calls a connector, resolves a secret, starts an automation,
        or writes to an external system.
        """

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_UNKNOWN_WORKSPACE,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )

        capability = _find_resource(manifest.capabilities, capability_id)
        if capability is None:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_UNKNOWN_CAPABILITY,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )

        automation = None
        if automation_id is not None:
            automation = _find_resource(manifest.automations, automation_id)
            if automation is None or automation.capability_id != capability_id:
                return self._record_policy_decision(
                    workspace_id=workspace_id,
                    decision=PolicyDecision.DENY,
                    reason_code=PolicyDecisionReason.DENY_UNKNOWN_AUTOMATION,
                    capability_id=capability_id,
                    automation_id=automation_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )

        agent = None
        if agent_id is not None:
            agent = _find_resource(manifest.agents, agent_id)
            if agent is None or capability_id not in agent.allowed_capabilities:
                return self._record_policy_decision(
                    workspace_id=workspace_id,
                    decision=PolicyDecision.DENY,
                    reason_code=PolicyDecisionReason.DENY_AGENT_CAPABILITY_SCOPE,
                    capability_id=capability_id,
                    automation_id=automation_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )

        if capability.lifecycle != LifecycleState.ACTIVE:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_INACTIVE_CAPABILITY,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )

        if automation is not None and automation.lifecycle != LifecycleState.ACTIVE:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_INACTIVE_AUTOMATION,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )

        policy_ids = _matching_policy_ids(
            manifest.policies, capability_id, automation_id
        )
        approval_ids = _matching_approval_ids(
            manifest.approvals, capability_id, automation_id, approved_only=True
        )
        explicit_decisions = _matching_policies(
            manifest.policies, capability_id, automation_id
        )

        safety_denials = [
            policy
            for policy in explicit_decisions
            if policy.compliance_blocked
            or policy.safety_classification.strip().lower()
            in {"blocked", "disallowed", "prohibited", "unsafe"}
        ]
        if safety_denials:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_SAFETY_COMPLIANCE,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=[policy.id for policy in safety_denials],
                approval_ids=approval_ids,
            )

        role_denials = [
            policy
            for policy in explicit_decisions
            if policy.allowed_actor_roles
            and (actor_role is None or actor_role not in policy.allowed_actor_roles)
        ]
        if role_denials:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_ACTOR_ROLE,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=[policy.id for policy in role_denials],
                approval_ids=approval_ids,
            )

        attribute_denials = [
            policy
            for policy in explicit_decisions
            if not _attributes_satisfied(
                policy.required_actor_attributes, actor_attributes
            )
            or not _attributes_satisfied(
                policy.required_workspace_attributes, workspace_attributes
            )
        ]
        if attribute_denials:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_POLICY_ATTRIBUTE,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=[policy.id for policy in attribute_denials],
                approval_ids=approval_ids,
            )

        # Structured attribute gates. Both the legacy flat attribute_conditions
        # list and the bounded attribute_expression tree are ANDed gates: a
        # policy passes only when both hold. Only deterministic reason codes and
        # policy ids are recorded; raw actor/workspace attribute values and the
        # expression text are evaluated here and never reach the audit payload.
        condition_denials = [
            policy
            for policy in explicit_decisions
            if not _attribute_conditions_satisfied(
                policy.attribute_conditions,
                actor_attributes,
                workspace_attributes,
            )
            or not _attribute_expression_satisfied(
                policy.attribute_expression,
                actor_attributes,
                workspace_attributes,
            )
        ]
        if condition_denials:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_POLICY_ATTRIBUTE,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=[policy.id for policy in condition_denials],
                approval_ids=approval_ids,
            )

        budget_denials = [
            policy
            for policy in explicit_decisions
            if (
                policy.max_cost_units is not None
                and estimated_cost_units > policy.max_cost_units
            )
            or (
                policy.max_token_usage is not None
                and estimated_token_usage > policy.max_token_usage
            )
        ]
        if budget_denials:
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_POLICY_BUDGET,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=[policy.id for policy in budget_denials],
                approval_ids=approval_ids,
            )

        # Local workspace budget enforcement. The current aggregate uses the same
        # model-supersedes-capability semantics as summarize_observability_metrics
        # so model invocation usage replaces capability estimates per run. Only
        # the deterministic reason code is recorded; raw aggregate event payloads
        # never reach the policy decision audit record.
        workspace = manifest.workspace
        if (
            workspace.max_cost_units is not None
            or workspace.max_token_usage is not None
        ):
            aggregate_token_usage, aggregate_cost_units = self._aggregate_local_usage(
                workspace_id
            )
            if (
                workspace.max_cost_units is not None
                and aggregate_cost_units + estimated_cost_units
                > workspace.max_cost_units
            ) or (
                workspace.max_token_usage is not None
                and aggregate_token_usage + estimated_token_usage
                > workspace.max_token_usage
            ):
                return self._record_policy_decision(
                    workspace_id=workspace_id,
                    decision=PolicyDecision.DENY,
                    reason_code=PolicyDecisionReason.DENY_WORKSPACE_BUDGET,
                    capability_id=capability_id,
                    automation_id=automation_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    policy_ids=policy_ids,
                    approval_ids=approval_ids,
                )

        if any(policy.decision == PolicyDecision.DENY for policy in explicit_decisions):
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.DENY,
                reason_code=PolicyDecisionReason.DENY_EXPLICIT_POLICY,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=policy_ids,
                approval_ids=approval_ids,
            )

        if any(
            policy.decision == PolicyDecision.REQUIRE_APPROVAL
            for policy in explicit_decisions
        ):
            if approval_ids:
                return self._record_policy_decision(
                    workspace_id=workspace_id,
                    decision=PolicyDecision.ALLOW,
                    reason_code=PolicyDecisionReason.ALLOW_APPROVED,
                    capability_id=capability_id,
                    automation_id=automation_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    policy_ids=policy_ids,
                    approval_ids=approval_ids,
                )
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.REQUIRE_APPROVAL,
                reason_code=PolicyDecisionReason.REQUIRE_APPROVAL_POLICY,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=policy_ids,
            )

        if capability.side_effect in DANGEROUS_SIDE_EFFECTS:
            if not explicit_decisions:
                return self._record_policy_decision(
                    workspace_id=workspace_id,
                    decision=PolicyDecision.DENY,
                    reason_code=PolicyDecisionReason.DENY_MISSING_POLICY,
                    capability_id=capability_id,
                    automation_id=automation_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
            return self._record_policy_decision(
                workspace_id=workspace_id,
                decision=PolicyDecision.REQUIRE_APPROVAL,
                reason_code=PolicyDecisionReason.REQUIRE_APPROVAL_DANGEROUS,
                capability_id=capability_id,
                automation_id=automation_id,
                agent_id=agent_id,
                actor_id=actor_id,
                actor_role=actor_role,
                policy_ids=policy_ids,
                approval_ids=approval_ids,
            )

        return self._record_policy_decision(
            workspace_id=workspace_id,
            decision=PolicyDecision.ALLOW,
            reason_code=PolicyDecisionReason.ALLOW_READ_SAFE,
            capability_id=capability_id,
            automation_id=automation_id,
            agent_id=agent_id,
            actor_id=actor_id,
            actor_role=actor_role,
            policy_ids=policy_ids,
            approval_ids=approval_ids,
        )

    def ingest_trigger_event(
        self,
        workspace_id: str,
        event: TriggerEventEnvelope | dict[str, Any],
    ) -> TriggerIngestionResult:
        """Create a run record from a manual or fixture event trigger.

        The method validates registry state, dedupes by idempotency key, writes
        event/audit records, and stores a run correlation record. It never
        executes an automation or invokes a capability.
        """

        envelope = event if isinstance(event, TriggerEventEnvelope) else _event_from_dict(event)
        now = _now()
        idempotency_key = envelope.idempotency_key or envelope.id
        duplicate_run_id = self._find_run_for_idempotency_key(
            workspace_id, idempotency_key
        )
        if duplicate_run_id is not None:
            audit_event_id = self._record_event_with_id(
                workspace_id,
                "trigger.duplicate",
                "trigger",
                envelope.trigger_id,
                {
                    "event_id": envelope.id,
                    "idempotency_key": idempotency_key,
                    "duplicate_of_run_id": duplicate_run_id,
                },
                now,
            )
            return TriggerIngestionResult(
                accepted=False,
                duplicate_of_run_id=duplicate_run_id,
                audit_event_id=audit_event_id,
            )

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "unknown_workspace",
                idempotency_key,
                now,
            )

        trigger = _find_resource(manifest.triggers, envelope.trigger_id)
        if trigger is None:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "unknown_trigger",
                idempotency_key,
                now,
            )
        if trigger.lifecycle != LifecycleState.ACTIVE:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "inactive_trigger",
                idempotency_key,
                now,
            )
        if trigger.event_type and envelope.event_type != trigger.event_type:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "event_type_mismatch",
                idempotency_key,
                now,
            )
        if trigger.cooldown_seconds:
            cooldown_run_id = self._find_recent_trigger_run(
                workspace_id,
                trigger.id,
                now,
                trigger.cooldown_seconds,
                mode="cooldown",
            )
            if cooldown_run_id is not None:
                return self._record_dead_letter(
                    workspace_id,
                    envelope,
                    "trigger_cooldown",
                    idempotency_key,
                    now,
                )
        if trigger.debounce_seconds:
            debounce_run_id = self._find_recent_trigger_run(
                workspace_id,
                trigger.id,
                now,
                trigger.debounce_seconds,
                mode="debounce",
                envelope=envelope,
            )
            if debounce_run_id is not None:
                return self._record_dead_letter(
                    workspace_id,
                    envelope,
                    "trigger_debounce",
                    idempotency_key,
                    now,
                )

        automation = next(
            (
                item
                for item in manifest.automations
                if item.trigger_id == trigger.id
                and item.capability_id == trigger.capability_id
            ),
            None,
        )
        if automation is None:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "no_automation_for_trigger",
                idempotency_key,
                now,
            )
        if automation.lifecycle != LifecycleState.ACTIVE:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "inactive_automation",
                idempotency_key,
                now,
            )
        in_flight_run_id = _find_in_flight_automation_run(manifest, automation.id)
        if in_flight_run_id is not None:
            return self._record_dead_letter(
                workspace_id,
                envelope,
                "automation_concurrency",
                idempotency_key,
                now,
            )

        run_status = (
            ControlPlaneRunStatus.WAITING_FOR_APPROVAL
            if automation.run_mode == RunMode.APPROVAL_GATED
            else ControlPlaneRunStatus.QUEUED
        )
        retention_days = _effective_retention_days(
            manifest.policies,
            automation.capability_id,
            automation.id,
        )
        run_record = RunRecord(
            id=f"run.{uuid4()}",
            automation_id=automation.id,
            status=run_status,
            run_ledger_ref=RUN_LEDGER_PATH_ENV,
            run_ledger_entry_id=_run_ledger_entry_id(idempotency_key),
            trace_id=_new_trace_id(),
            retention_expires_at=(
                _retention_expires_at(now, retention_days)
                if retention_days is not None
                else None
            ),
            started_at=None,
            updated_at=now,
        )
        run_record = _materialize_resume_metadata(
            run_record,
            now,
            reason="trigger_ingestion",
        )

        audit_event_id = self._append_run_record(
            workspace_id,
            run_record,
            envelope,
            idempotency_key,
            now,
        )
        return TriggerIngestionResult(
            accepted=True,
            run_record=run_record,
            audit_event_id=audit_event_id,
        )

    def materialize_due_schedule_triggers_once(
        self,
        workspace_id: str,
        *,
        now: str | None = None,
        limit: int = 20,
    ) -> list[TriggerIngestionResult]:
        """Materialize due active schedule triggers into local run records.

        This is a deterministic one-shot scheduler pass, not a daemon. It does
        not sleep, start background work, invoke connectors, call models, or
        sync Cloud state. For each due schedule trigger it synthesizes a
        display-safe trigger event with an occurrence-derived idempotency key
        and delegates to :meth:`ingest_trigger_event`, preserving the existing
        lifecycle, cooldown/debounce, concurrency, approval-gated, dead-letter
        and audit behavior in one path.
        """

        if limit <= 0:
            return []

        now_ts = now or _now()
        try:
            now_dt = _parse_datetime(now_ts).astimezone(timezone.utc)
        except ValueError:
            raise ControlPlaneRegistryError("now must be an ISO timestamp") from None

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return []

        results: list[TriggerIngestionResult] = []
        for trigger in sorted(manifest.triggers, key=lambda item: item.id):
            if len(results) >= limit:
                break
            if trigger.kind != TriggerKind.SCHEDULE:
                continue
            if trigger.lifecycle != LifecycleState.ACTIVE:
                continue
            occurrence = _due_schedule_occurrence(
                trigger.schedule_rrule,
                now_dt,
                schedule_start_at=trigger.schedule_start_at,
            )
            if occurrence is None:
                continue
            occurrence_key = occurrence.isoformat().replace("+00:00", "Z")
            result = self.ingest_trigger_event(
                workspace_id,
                TriggerEventEnvelope(
                    id=f"evt.schedule.{_safe_schedule_id(trigger.id)}.{_safe_schedule_id(occurrence_key)}",
                    trigger_id=trigger.id,
                    event_type=trigger.event_type
                    or f"omnivia.schedule.{trigger.id}",
                    source=f"schedule://{workspace_id}/{trigger.id}",
                    subject=trigger.id,
                    idempotency_key=f"schedule:{trigger.id}:{occurrence_key}",
                    data_ref=f"schedule://{workspace_id}/{trigger.id}/{occurrence_key}",
                ),
            )
            results.append(result)
        return results

    def execute_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        mode: ExecutionMode | str = ExecutionMode.DRY_RUN,
        actor_id: str | None = None,
        cost_center: str | None = None,
        actor_role: str | None = None,
        actor_attributes: Mapping[str, str] | None = None,
        workspace_attributes: Mapping[str, str] | None = None,
        max_steps: int = 4,
        input_payload: Mapping[str, Any] | None = None,
        output_payload: Mapping[str, Any] | None = None,
        estimated_cost_units: int = 0,
        estimated_token_usage: int = 0,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_token_usage: int = 0,
        model_cost_units: int = 0,
        invocation_type: str | None = None,
        capability_executor: CapabilityExecutor | None = None,
    ) -> ExecutionResult:
        """Run a bounded dry-run/supervised execution plan.

        The runner records deterministic step evidence and policy decisions and
        never sends messages, executes arbitrary code, invokes a model, or
        passes raw connection/client objects to an agent.

        Optional ``model_*`` parameters accept display-safe model invocation
        usage metadata accumulated by a caller. They never carry raw prompt or
        output text; the recorded evidence keeps prompt/output redacted.

        ``capability_executor`` is an optional injected seam for supervised mode
        only. When supplied, and only after the policy decision is ALLOW and the
        input payload validates, it is called with a sanitized
        :class:`CapabilityExecutionContext` (display-safe identifiers, schema
        summaries, trace/span and policy-decision IDs; never raw secrets,
        clients, MCP/OpenAPI handles, or the full manifest). Its display-safe
        output drives output-schema validation and the capability invocation
        evidence. Dry-run never calls the executor, and an approval-gated
        decision pauses before the executor is reached. If the executor raises
        or returns an unusable result, the run fails closed with a failed
        capability step and no completed capability invocation event.
        """

        execution_mode = (
            mode if isinstance(mode, ExecutionMode) else ExecutionMode(str(mode))
        )
        if max_steps < 2:
            raise ControlPlaneRegistryError("max_steps must allow agent and capability steps")
        if model_token_usage < 0 or model_cost_units < 0:
            raise ControlPlaneRegistryError(
                "model token and cost usage must be non-negative"
            )

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        automation = _find_resource(manifest.automations, run_record.automation_id)
        if automation is None:
            raise ControlPlaneRegistryError(
                f"automation {run_record.automation_id} does not exist"
            )
        timeout_seconds = _effective_timeout_seconds(
            manifest.policies,
            automation.capability_id,
            automation.id,
        )
        if timeout_seconds is not None and _run_timed_out(run_record, timeout_seconds):
            now = _now()
            updated = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.TIMED_OUT,
                now,
                trace_id=run_record.trace_id,
            )
            self._record_event(
                workspace_id,
                "run.timeout.enforced",
                "run_record",
                run_id,
                {
                    "run_id": run_id,
                    "automation_id": automation.id,
                    "timeout_seconds": timeout_seconds,
                    "trace_id": updated.trace_id,
                },
                now,
            )
            return ExecutionResult(run_record=updated, error="run_timed_out")
        _enforce_execution_limits(automation, max_steps=max_steps)
        capability = _find_resource(manifest.capabilities, automation.capability_id)
        if capability is None:
            raise ControlPlaneRegistryError(
                f"capability {automation.capability_id} does not exist"
            )
        agent = _find_resource(manifest.agents, automation.agent_id)
        if agent is None:
            raise ControlPlaneRegistryError(
                f"agent {automation.agent_id} does not exist"
            )

        now = _now()
        trace_id = run_record.trace_id or _new_trace_id()
        self._update_run_status(
            workspace_id,
            run_id,
            ControlPlaneRunStatus.RUNNING,
            now,
            trace_id=trace_id,
        )

        agent_step = self._record_run_step(
            workspace_id,
            RunStepRecord(
                id=f"run-step.{uuid4()}",
                run_id=run_id,
                step_type=RunStepType.AGENT,
                status=RunStepStatus.SIMULATED,
                agent_id=agent.id,
                trace_id=trace_id,
                span_id=_new_span_id(),
                attempt=1,
                created_at=now,
            ),
            {
                "mode": execution_mode.value,
                "permitted_capability_ids": list(agent.allowed_capabilities),
                "execution_limits": _execution_limit_payload(automation),
                "estimated_cost_units": estimated_cost_units,
                "estimated_token_usage": estimated_token_usage,
            },
        )
        model_invocation = self._record_model_invocation(
            workspace_id,
            LocalModelInvocationRecord(
                id=f"model-invocation.{uuid4()}",
                run_id=run_id,
                step_id=agent_step.id,
                agent_id=agent.id,
                trace_id=trace_id,
                span_id=agent_step.span_id or _new_span_id(),
                model_provider=(
                    model_provider if model_provider is not None else "not_invoked"
                ),
                model_name=model_name if model_name is not None else "not_invoked",
                invocation_type=(
                    invocation_type
                    if invocation_type is not None
                    else "planning_placeholder"
                ),
                token_usage=model_token_usage,
                cost_units=model_cost_units,
                created_at=_now(),
            ),
            actor_id=actor_id,
            cost_center=cost_center,
        )

        input_validation_errors = _validate_payload_against_schema(
            capability.input_schema,
            input_payload,
            field_name="input_payload",
        )
        if input_validation_errors:
            validation_event_id = self._record_payload_validation_event(
                workspace_id,
                run_id,
                capability.id,
                trace_id,
                "input",
                input_validation_errors,
            )
            failed_step = self._record_run_step(
                workspace_id,
                RunStepRecord(
                    id=f"run-step.{uuid4()}",
                    run_id=run_id,
                    step_type=RunStepType.CAPABILITY,
                    status=RunStepStatus.FAILED,
                    capability_id=automation.capability_id,
                    trace_id=trace_id,
                    span_id=_new_span_id(),
                    error="invalid_input_payload",
                    attempt=1,
                    created_at=_now(),
                ),
                {
                    "mode": execution_mode.value,
                    "payload_validation_event_id": validation_event_id,
                },
            )
            updated = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.FAILED,
                _now(),
                trace_id=trace_id,
            )
            return ExecutionResult(
                run_record=updated,
                steps=[agent_step, failed_step],
                model_invocation=model_invocation,
                error="invalid_input_payload",
            )

        decision = self.evaluate_capability_access(
            workspace_id,
            automation.capability_id,
            automation_id=automation.id,
            agent_id=agent.id,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_attributes=actor_attributes,
            workspace_attributes=workspace_attributes,
            estimated_cost_units=estimated_cost_units,
            estimated_token_usage=estimated_token_usage,
        )

        if decision.decision == PolicyDecision.DENY:
            failed_step = self._record_run_step(
                workspace_id,
                RunStepRecord(
                    id=f"run-step.{uuid4()}",
                    run_id=run_id,
                    step_type=RunStepType.CAPABILITY,
                    status=RunStepStatus.FAILED,
                    capability_id=automation.capability_id,
                    policy_decision_id=decision.id,
                    trace_id=trace_id,
                    span_id=_new_span_id(),
                    error=decision.reason_code.value,
                    attempt=1,
                    created_at=_now(),
                ),
                {"mode": execution_mode.value},
            )
            updated = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.FAILED,
                _now(),
                trace_id=trace_id,
            )
            return ExecutionResult(
                run_record=updated,
                steps=[agent_step, failed_step],
                policy_decision=decision,
                model_invocation=model_invocation,
                error=decision.reason_code.value,
            )

        if decision.decision == PolicyDecision.REQUIRE_APPROVAL:
            approval_step = self._record_run_step(
                workspace_id,
                RunStepRecord(
                    id=f"run-step.{uuid4()}",
                    run_id=run_id,
                    step_type=RunStepType.APPROVAL_WAIT,
                    status=RunStepStatus.WAITING_FOR_APPROVAL,
                    capability_id=automation.capability_id,
                    agent_id=agent.id,
                    policy_decision_id=decision.id,
                    trace_id=trace_id,
                    span_id=_new_span_id(),
                    attempt=1,
                    created_at=_now(),
                ),
                {"mode": execution_mode.value},
            )
            self._record_approval_wait(
                workspace_id,
                approval_step,
                decision,
                execution_mode.value,
            )
            updated = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
                _now(),
                trace_id=trace_id,
            )
            return ExecutionResult(
                run_record=updated,
                steps=[agent_step, approval_step],
                policy_decision=decision,
                model_invocation=model_invocation,
                paused=True,
            )

        # Supervised executor seam: only supervised mode, only after ALLOW and
        # input validation, may an injected executor actually produce the
        # capability output. Dry-run keeps simulating and never reaches here with
        # an executor effect; approval-gated decisions have already paused above.
        capability_span_id = _new_span_id()
        effective_output_payload: Mapping[str, Any] | None = output_payload
        executor_metadata: dict[str, Any] | None = None
        # The executor-backed condition is tested inline rather than through a
        # pre-computed flag so that `capability_executor is not None` narrows the
        # optional seam for the `_run_supervised_executor` call below; a bool
        # variable carries the same runtime meaning but discards the narrowing.
        executor_backed = False
        if (
            execution_mode == ExecutionMode.SUPERVISED
            and capability_executor is not None
            and capability.side_effect.value in SAFE_EXECUTOR_SIDE_EFFECTS
        ):
            executor_backed = True
            connection = _find_resource(
                manifest.connections, capability.connection_id
            )
            executor_output, executor_diagnostics, executor_error = (
                self._run_supervised_executor(
                    capability_executor,
                    capability=capability,
                    connection=connection,
                    decision=decision,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    span_id=capability_span_id,
                    input_payload=input_payload,
                )
            )
            if executor_error is not None:
                failed_step = self._record_run_step(
                    workspace_id,
                    RunStepRecord(
                        id=f"run-step.{uuid4()}",
                        run_id=run_id,
                        step_type=RunStepType.CAPABILITY,
                        status=RunStepStatus.FAILED,
                        capability_id=automation.capability_id,
                        agent_id=agent.id,
                        policy_decision_id=decision.id,
                        trace_id=trace_id,
                        span_id=capability_span_id,
                        error=executor_error,
                        attempt=1,
                        created_at=_now(),
                    ),
                    {
                        "mode": execution_mode.value,
                        "executor_backed": True,
                    },
                )
                updated = self._update_run_status(
                    workspace_id,
                    run_id,
                    ControlPlaneRunStatus.FAILED,
                    _now(),
                    trace_id=trace_id,
                )
                return ExecutionResult(
                    run_record=updated,
                    steps=[agent_step, failed_step],
                    policy_decision=decision,
                    model_invocation=model_invocation,
                    error=executor_error,
                )
            effective_output_payload = executor_output
            executor_metadata = _sanitize_executor_metadata(executor_diagnostics)

        capability_step = self._record_run_step(
            workspace_id,
            RunStepRecord(
                id=f"run-step.{uuid4()}",
                run_id=run_id,
                step_type=RunStepType.CAPABILITY,
                status=(
                    RunStepStatus.SIMULATED
                    if execution_mode == ExecutionMode.DRY_RUN
                    else RunStepStatus.COMPLETED
                ),
                capability_id=automation.capability_id,
                agent_id=agent.id,
                policy_decision_id=decision.id,
                trace_id=trace_id,
                span_id=capability_span_id,
                attempt=1,
                cost_units=estimated_cost_units,
                token_usage=estimated_token_usage,
                created_at=_now(),
            ),
            {
                "mode": execution_mode.value,
                "simulated": execution_mode == ExecutionMode.DRY_RUN,
                "executor_backed": executor_backed,
            },
        )
        output_validation_errors = _validate_payload_against_schema(
            capability.output_schema,
            effective_output_payload,
            field_name="output_payload",
        )
        if output_validation_errors:
            validation_event_id = self._record_payload_validation_event(
                workspace_id,
                run_id,
                capability.id,
                trace_id,
                "output",
                output_validation_errors,
            )
            failed_step = self._record_run_step(
                workspace_id,
                replace(
                    capability_step,
                    status=RunStepStatus.FAILED,
                    error="invalid_output_payload",
                    audit_event_id=None,
                ),
                {
                    "mode": execution_mode.value,
                    "payload_validation_event_id": validation_event_id,
                },
            )
            updated = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.FAILED,
                _now(),
                trace_id=trace_id,
            )
            return ExecutionResult(
                run_record=updated,
                steps=[agent_step, failed_step],
                policy_decision=decision,
                model_invocation=model_invocation,
                error="invalid_output_payload",
            )
        self._record_capability_invocation(
            workspace_id,
            capability_step,
            decision,
            capability,
            automation,
            execution_mode.value,
            simulated=execution_mode == ExecutionMode.DRY_RUN,
            executor_backed=executor_backed,
            executor_metadata=executor_metadata,
            output_summary=(
                _output_payload_summary(effective_output_payload)
                if executor_backed
                else None
            ),
            actor_id=actor_id,
            cost_center=cost_center,
        )
        updated = self._update_run_status(
            workspace_id,
            run_id,
            ControlPlaneRunStatus.COMPLETED,
            _now(),
            trace_id=trace_id,
        )
        return ExecutionResult(
            run_record=updated,
            steps=[agent_step, capability_step],
            policy_decision=decision,
            model_invocation=model_invocation,
        )

    def cancel_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        actor_id: str | None = None,
        reason: str = "",
    ) -> RunRecord:
        """Cancel a queued, running, or approval-waiting local run record."""

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        if run_record.status in TERMINAL_RUN_STATUSES:
            raise ControlPlaneRegistryError(f"run {run_id} is already terminal")
        now = _now()
        updated = self._update_run_status(
            workspace_id,
            run_id,
            ControlPlaneRunStatus.CANCELLED,
            now,
            trace_id=run_record.trace_id,
        )
        self._record_event(
            workspace_id,
            "run.cancelled",
            "run_record",
            run_id,
            {
                "run_id": run_id,
                "status": ControlPlaneRunStatus.CANCELLED.value,
                "actor_id": actor_id,
                "reason": reason,
                "trace_id": updated.trace_id,
            },
            now,
        )
        return updated

    def retry_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        actor_id: str | None = None,
        reason: str = "",
        base_delay_seconds: int = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        max_delay_seconds: int = DEFAULT_RETRY_MAX_DELAY_SECONDS,
        now: str | None = None,
    ) -> RunRecord:
        """Requeue a failed local run with deterministic backoff metadata.

        This materializes a future ``resume_after`` timestamp based on the
        run's retry count and an exponential backoff capped at
        ``max_delay_seconds``. It does not execute, schedule, or sleep; a
        caller (or a future out-of-Core worker) is responsible for honouring
        the timestamp. The ``now`` override exists only for deterministic tests.
        """

        if base_delay_seconds < 1:
            raise ControlPlaneRegistryError("base_delay_seconds must be >= 1")
        if max_delay_seconds < base_delay_seconds:
            raise ControlPlaneRegistryError(
                "max_delay_seconds must be >= base_delay_seconds"
            )

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        if run_record.status not in RETRYABLE_RUN_STATUSES:
            raise ControlPlaneRegistryError(f"run {run_id} is not retryable")
        automation = _find_resource(manifest.automations, run_record.automation_id)
        if automation is None:
            raise ControlPlaneRegistryError(
                f"automation {run_record.automation_id} does not exist"
            )
        next_retry_count = run_record.retry_count + 1
        if (
            automation.max_retries is not None
            and next_retry_count > automation.max_retries
        ):
            raise ControlPlaneRegistryError(
                f"automation {automation.id} max_retries limit exceeded"
            )
        now_ts = now or _now()
        delay_seconds = _retry_backoff_delay_seconds(
            next_retry_count, base_delay_seconds, max_delay_seconds
        )
        resume_after = _future_iso_timestamp(now_ts, delay_seconds)
        updated = self._update_run_status(
            workspace_id,
            run_id,
            ControlPlaneRunStatus.QUEUED,
            now_ts,
            trace_id=run_record.trace_id,
            retry_count=next_retry_count,
            resume_reason="retry_requested",
            resume_after=resume_after,
        )
        self._record_event(
            workspace_id,
            "run.retry.queued",
            "run_record",
            run_id,
            {
                "run_id": run_id,
                "status": ControlPlaneRunStatus.QUEUED.value,
                "actor_id": actor_id,
                "reason": reason,
                "retry_count": next_retry_count,
                "max_retries": automation.max_retries,
                "retry_after": updated.resume_after,
                "resume_after": updated.resume_after,
                "delay_seconds": delay_seconds,
                "base_delay_seconds": base_delay_seconds,
                "max_delay_seconds": max_delay_seconds,
                "secret_value_redacted": True,
                "trace_id": updated.trace_id,
            },
            now_ts,
        )
        return updated

    def resume_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        resume_token: str | None = None,
        actor_id: str | None = None,
        reason: str = "",
    ) -> RunRecord:
        """Resume a local in-flight run by returning it to queued state."""

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        if run_record.status in TERMINAL_RUN_STATUSES:
            raise ControlPlaneRegistryError(f"run {run_id} is already terminal")
        if not run_record.resume_token:
            raise ControlPlaneRegistryError(f"run {run_id} has no resume token")
        if resume_token is not None and resume_token != run_record.resume_token:
            raise ControlPlaneRegistryError(f"run {run_id} resume token mismatch")

        now = _now()
        updated = self._update_run_status(
            workspace_id,
            run_id,
            ControlPlaneRunStatus.QUEUED,
            now,
            trace_id=run_record.trace_id,
            retry_count=run_record.retry_count,
            resume_reason="resume_requested",
        )
        self._record_event(
            workspace_id,
            "run.resume.queued",
            "run_record",
            run_id,
            {
                "run_id": run_id,
                "status": ControlPlaneRunStatus.QUEUED.value,
                "actor_id": actor_id,
                "reason": reason,
                "retry_count": run_record.retry_count,
                "resume_token_present": True,
                "trace_id": updated.trace_id,
            },
            now,
        )
        return updated

    def record_approval_decision(
        self,
        workspace_id: str,
        run_id: str,
        *,
        approved: bool,
        approver_role: str,
        actor_id: str | None = None,
        comment: str = "",
        approval_id: str | None = None,
        reason: str = "",
    ) -> RunRecord:
        """Record a human approval decision for a paused local run record.

        This is a decision/resume-metadata surface only. It records a
        display-safe :class:`Approval` record plus run-status/decision events and
        transitions the run through existing resume/terminal semantics. It never
        executes a capability, calls a worker pump, syncs to Cloud, drives any
        Platform UI, or invokes a connector/tool/model. Raw input/output
        payloads and secrets never reach the recorded approval or events.

        The decision applies only to a run that is currently
        ``waiting_for_approval`` or ``approval_required``; any other status
        (terminal or otherwise) fails closed without mutating state. An approved
        decision requeues the run to ``queued`` with
        ``resume_reason='approval_approved'``, preserving the run trace and retry
        count. A rejected decision marks the run ``rejected`` and clears resume
        metadata through the existing terminal-status path.
        """

        if not approver_role or not approver_role.strip():
            raise ControlPlaneRegistryError("approver_role is required")
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        if run_record.status not in {
            ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
            ControlPlaneRunStatus.APPROVAL_REQUIRED,
        }:
            raise ControlPlaneRegistryError(
                f"run {run_id} is not awaiting approval"
            )
        automation = _find_resource(manifest.automations, run_record.automation_id)
        if automation is None:
            raise ControlPlaneRegistryError(
                f"automation {run_record.automation_id} does not exist"
            )
        capability_id = automation.capability_id

        now = _now()
        next_approval_id = (
            approval_id
            or f"approval.decision.{_resource_id_slug(run_id)}"
        )
        existing_approval = _find_resource(manifest.approvals, next_approval_id)
        # A decided approval resolves the escalation in either direction; the
        # human verdict closes the outstanding approval question.
        escalation_state = "resolved"
        approval = Approval(
            id=next_approval_id,
            capability_ids=[capability_id],
            automation_ids=[automation.id],
            approved=approved,
            approver_role=approver_role,
            actor_id=actor_id,
            comment=comment,
            escalation_state=escalation_state,
            run_id=run_id,
            resource_type="capability",
            resource_id=capability_id,
            decided_at=now,
            assigned_actor_id=(
                existing_approval.assigned_actor_id if existing_approval else None
            ),
            assigned_role=existing_approval.assigned_role if existing_approval else None,
            assigned_at=existing_approval.assigned_at if existing_approval else None,
            escalated_at=existing_approval.escalated_at if existing_approval else None,
            escalation_reason=(
                existing_approval.escalation_reason if existing_approval else None
            ),
        )
        approvals = [item for item in manifest.approvals if item.id != next_approval_id]
        approvals.append(approval)
        updated_manifest = replace(manifest, approvals=approvals)
        result = validate_control_plane_manifest(updated_manifest)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated_manifest, event_type="manifest.updated")

        if approved:
            updated_run = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.QUEUED,
                now,
                trace_id=run_record.trace_id,
                retry_count=run_record.retry_count,
                resume_reason=APPROVAL_RESUME_REASON,
            )
        else:
            updated_run = self._update_run_status(
                workspace_id,
                run_id,
                ControlPlaneRunStatus.REJECTED,
                now,
                trace_id=run_record.trace_id,
            )

        self._record_event(
            workspace_id,
            "approval.decision.approved" if approved else "approval.decision.rejected",
            "approval",
            approval.id,
            {
                "run_id": run_id,
                "approval_id": approval.id,
                "automation_id": automation.id,
                "capability_id": capability_id,
                "approved": approved,
                "approver_role": approver_role,
                "actor_id": actor_id,
                "comment": comment,
                "reason": reason,
                "escalation_state": escalation_state,
                "assigned_actor_id": approval.assigned_actor_id,
                "assigned_role": approval.assigned_role,
                "assigned_at": approval.assigned_at,
                "escalated_at": approval.escalated_at,
                "escalation_reason": approval.escalation_reason,
                "decided_at": now,
                "status": updated_run.status.value,
                "resume_reason": updated_run.resume_reason,
                "trace_id": updated_run.trace_id,
                "payload_redacted": True,
                "secret_value_redacted": True,
            },
            now,
        )
        return updated_run

    def assign_approval_request(
        self,
        workspace_id: str,
        run_id: str,
        *,
        assigned_role: str | None = None,
        assigned_actor_id: str | None = None,
        actor_id: str | None = None,
        approval_id: str | None = None,
        timeout_seconds: int | None = None,
        comment: str = "",
    ) -> Approval:
        """Assign a local waiting approval to a role or actor.

        This is display-safe assignment metadata only. It creates or updates a
        pending/assigned :class:`Approval` record for a local run that is already
        waiting for approval, emits an audit event, and enqueues local
        notification-outbox evidence. It never delivers a notification, starts a
        worker, resolves secrets, syncs to Cloud, or invokes a
        connector/tool/model.
        """

        if not (assigned_role and assigned_role.strip()) and not (
            assigned_actor_id and assigned_actor_id.strip()
        ):
            raise ControlPlaneRegistryError(
                "assigned_role or assigned_actor_id is required"
            )
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ControlPlaneRegistryError("timeout_seconds must be >= 0")

        manifest, run_record, automation, capability_id = self._approval_run_context(
            workspace_id, run_id
        )
        now = _now()
        next_approval_id = (
            approval_id
            or f"approval.assignment.{_resource_id_slug(run_id)}"
        )
        existing = _find_resource(manifest.approvals, next_approval_id)
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else (existing.timeout_seconds if existing else None)
        )
        approval = Approval(
            id=next_approval_id,
            capability_ids=[capability_id],
            automation_ids=[automation.id],
            approved=False,
            approver_role=existing.approver_role if existing else "",
            actor_id=actor_id,
            comment=_redact_approval_metadata_text(comment),
            timeout_seconds=effective_timeout,
            escalation_state="assigned",
            run_id=run_id,
            resource_type="capability",
            resource_id=capability_id,
            requested_at=existing.requested_at if existing else now,
            expires_at=(
                _future_iso_timestamp(now, effective_timeout)
                if effective_timeout is not None
                else (existing.expires_at if existing else None)
            ),
            assigned_actor_id=assigned_actor_id,
            assigned_role=assigned_role,
            assigned_at=now,
            escalated_at=existing.escalated_at if existing else None,
            escalation_reason=existing.escalation_reason if existing else None,
        )
        self._store_approval(manifest, approval)
        self._record_event(
            workspace_id,
            "approval.assignment.assigned",
            "approval",
            approval.id,
            {
                "run_id": run_record.id,
                "approval_id": approval.id,
                "automation_id": automation.id,
                "capability_id": capability_id,
                "assigned_actor_id": approval.assigned_actor_id,
                "assigned_role": approval.assigned_role,
                "assigned_at": approval.assigned_at,
                "timeout_seconds": approval.timeout_seconds,
                "expires_at": approval.expires_at,
                "actor_id": actor_id,
                "comment": approval.comment,
                "escalation_state": approval.escalation_state,
                "trace_id": run_record.trace_id,
                "payload_redacted": True,
                "secret_value_redacted": True,
            },
            now,
        )
        self.enqueue_approval_notification(
            workspace_id,
            run_record.id,
            event_type=LocalApprovalNotificationEvent.APPROVAL_ASSIGNED,
            approval_id=approval.id,
            recipient_actor_id=assigned_actor_id,
            recipient_role=assigned_role,
            comment=comment,
            actor_id=actor_id,
        )
        return approval

    def escalate_approval_request(
        self,
        workspace_id: str,
        run_id: str,
        *,
        approval_id: str | None = None,
        actor_id: str | None = None,
        escalation_reason: str = "",
    ) -> Approval:
        """Escalate an assigned/pending local approval without notification delivery."""

        manifest, run_record, automation, capability_id = self._approval_run_context(
            workspace_id, run_id
        )
        next_approval_id = (
            approval_id
            or f"approval.assignment.{_resource_id_slug(run_id)}"
        )
        existing = _find_resource(manifest.approvals, next_approval_id)
        if existing is None:
            raise ControlPlaneRegistryError(
                f"approval {next_approval_id} does not exist"
            )
        if existing.approved or existing.escalation_state == "resolved":
            raise ControlPlaneRegistryError(
                f"approval {next_approval_id} is already resolved"
            )

        now = _now()
        approval = replace(
            existing,
            escalation_state="escalated",
            escalated_at=now,
            escalation_reason=_redact_approval_metadata_text(escalation_reason),
        )
        self._store_approval(manifest, approval)
        self._record_event(
            workspace_id,
            "approval.assignment.escalated",
            "approval",
            approval.id,
            {
                "run_id": run_record.id,
                "approval_id": approval.id,
                "automation_id": automation.id,
                "capability_id": capability_id,
                "assigned_actor_id": approval.assigned_actor_id,
                "assigned_role": approval.assigned_role,
                "assigned_at": approval.assigned_at,
                "escalated_at": approval.escalated_at,
                "escalation_reason": approval.escalation_reason,
                "actor_id": actor_id,
                "escalation_state": approval.escalation_state,
                "trace_id": run_record.trace_id,
                "payload_redacted": True,
                "secret_value_redacted": True,
            },
            now,
        )
        self.enqueue_approval_notification(
            workspace_id,
            run_record.id,
            event_type=LocalApprovalNotificationEvent.APPROVAL_ESCALATED,
            approval_id=approval.id,
            recipient_actor_id=approval.assigned_actor_id,
            recipient_role=approval.assigned_role,
            reason=escalation_reason,
            actor_id=actor_id,
        )
        return approval

    def process_approval_timeouts(
        self,
        workspace_id: str,
        *,
        now: str | None = None,
        actor_id: str | None = None,
        reason: str = "approval_timeout",
    ) -> list[RunRecord]:
        """Scan waiting approval runs and materialize timeout/escalation outcomes.

        This is a local metadata/status scheduler pass only. For each run that is
        currently ``waiting_for_approval`` or ``approval_required`` it computes the
        effective approval timeout from matching policies (reusing the existing
        ``timeout_seconds`` semantics) and either records a display-safe *pending*
        approval-timeout record (run unchanged) or, once the wait has elapsed,
        records a display-safe *expired* approval record and transitions the run
        to ``rejected`` through the existing terminal-status path.

        It never executes a capability, calls a worker pump, resolves a secret,
        invokes a model/connector, drives a Platform UI, or syncs to Cloud. Raw
        input/output payloads, secrets, and connector/import metadata never reach
        the recorded approval or its events. The ``now`` override exists only for
        deterministic callers and tests.

        Returns the run records moved to ``rejected`` by this pass, in manifest
        order. Runs that stay waiting (a pending timeout or no applicable timeout
        policy) are not returned. Re-running before expiry does not duplicate the
        single pending record per run/capability or re-emit its event; re-running
        after expiry leaves the now-terminal run untouched. Fails closed with
        :class:`ControlPlaneRegistryError` if a waiting run references a missing
        automation or capability.
        """

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return []

        now_ts = now or _now()
        safe_reason = _redact_observability_text(reason)
        try:
            now_dt = _parse_datetime(now_ts)
        except ValueError as error:
            raise ControlPlaneRegistryError(
                "now must be an ISO-8601 timestamp"
            ) from error

        waiting_run_ids = [
            run_record.id
            for run_record in manifest.run_records
            if run_record.status
            in {
                ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
                ControlPlaneRunStatus.APPROVAL_REQUIRED,
            }
        ]

        rejected: list[RunRecord] = []
        for run_id in waiting_run_ids:
            current = self.get_manifest(workspace_id)
            if current is None:
                break
            run_record = _find_resource(current.run_records, run_id)
            if run_record is None:
                continue
            automation = _find_resource(
                current.automations, run_record.automation_id
            )
            if automation is None:
                raise ControlPlaneRegistryError(
                    f"automation {run_record.automation_id} does not exist"
                )
            capability = _find_resource(
                current.capabilities, automation.capability_id
            )
            if capability is None:
                raise ControlPlaneRegistryError(
                    f"capability {automation.capability_id} does not exist"
                )
            capability_id = automation.capability_id

            timeout_seconds = _effective_timeout_seconds(
                current.policies, capability_id, automation.id
            )
            if timeout_seconds is None:
                # No timeout policy applies; the run keeps waiting untouched.
                continue

            wait_start = run_record.started_at or run_record.updated_at
            requested_at = wait_start or now_ts
            expires_at = _approval_expiry(wait_start, timeout_seconds)
            approval_id = f"approval.timeout.{_resource_id_slug(run_id)}"

            if _approval_wait_expired(wait_start, timeout_seconds, now_dt):
                approval = Approval(
                    id=approval_id,
                    capability_ids=[capability_id],
                    automation_ids=[automation.id],
                    approved=False,
                    actor_id=actor_id,
                    comment=safe_reason,
                    timeout_seconds=timeout_seconds,
                    escalation_state="expired",
                    run_id=run_id,
                    resource_type="capability",
                    resource_id=capability_id,
                    requested_at=requested_at,
                    decided_at=now_ts,
                    expires_at=expires_at or now_ts,
                )
                self._store_approval(current, approval)
                updated_run = self._update_run_status(
                    workspace_id,
                    run_id,
                    ControlPlaneRunStatus.REJECTED,
                    now_ts,
                    trace_id=run_record.trace_id,
                )
                self._record_event(
                    workspace_id,
                    "approval.timeout.expired",
                    "approval",
                    approval.id,
                    {
                        "run_id": run_id,
                        "approval_id": approval.id,
                        "automation_id": automation.id,
                        "capability_id": capability_id,
                        "timeout_seconds": timeout_seconds,
                        "requested_at": requested_at,
                        "expires_at": approval.expires_at,
                        "decided_at": now_ts,
                        "status": updated_run.status.value,
                        "escalation_state": "expired",
                        "actor_id": actor_id,
                        "reason": safe_reason,
                        "payload_redacted": True,
                        "secret_value_redacted": True,
                    },
                    now_ts,
                )
                self.enqueue_approval_notification(
                    workspace_id,
                    run_id,
                    event_type=LocalApprovalNotificationEvent.APPROVAL_EXPIRED,
                    approval_id=approval.id,
                    reason=safe_reason,
                    actor_id=actor_id,
                )
                rejected.append(updated_run)
                continue

            approval = Approval(
                id=approval_id,
                capability_ids=[capability_id],
                automation_ids=[automation.id],
                approved=False,
                actor_id=actor_id,
                comment=safe_reason,
                timeout_seconds=timeout_seconds,
                escalation_state="pending",
                run_id=run_id,
                resource_type="capability",
                resource_id=capability_id,
                requested_at=requested_at,
                expires_at=expires_at,
            )
            existing = _find_resource(current.approvals, approval_id)
            if existing == approval:
                # An equivalent pending timeout record already exists for this
                # run/capability; do not duplicate metadata or re-emit events.
                continue
            self._store_approval(current, approval)
            self._record_event(
                workspace_id,
                "approval.timeout.pending",
                "approval",
                approval.id,
                {
                    "run_id": run_id,
                    "approval_id": approval.id,
                    "automation_id": automation.id,
                    "capability_id": capability_id,
                    "timeout_seconds": timeout_seconds,
                    "requested_at": requested_at,
                    "expires_at": expires_at,
                    "status": run_record.status.value,
                    "escalation_state": "pending",
                    "actor_id": actor_id,
                    "reason": safe_reason,
                    "payload_redacted": True,
                    "secret_value_redacted": True,
                },
                now_ts,
            )
            self.enqueue_approval_notification(
                workspace_id,
                run_id,
                event_type=LocalApprovalNotificationEvent.APPROVAL_TIMEOUT_PENDING,
                approval_id=approval.id,
                reason=safe_reason,
                actor_id=actor_id,
            )
        return rejected

    def _approval_run_context(
        self, workspace_id: str, run_id: str
    ) -> tuple[ControlPlaneManifest, RunRecord, Automation, str]:
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        if run_record.status not in {
            ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
            ControlPlaneRunStatus.APPROVAL_REQUIRED,
        }:
            raise ControlPlaneRegistryError(
                f"run {run_id} is not awaiting approval"
            )
        automation = _find_resource(manifest.automations, run_record.automation_id)
        if automation is None:
            raise ControlPlaneRegistryError(
                f"automation {run_record.automation_id} does not exist"
            )
        return manifest, run_record, automation, automation.capability_id

    def _store_approval(
        self, manifest: ControlPlaneManifest, approval: Approval
    ) -> None:
        """Replace/append an approval record and persist the validated manifest."""

        approvals = [
            item for item in manifest.approvals if item.id != approval.id
        ]
        approvals.append(approval)
        updated_manifest = replace(manifest, approvals=approvals)
        result = validate_control_plane_manifest(updated_manifest)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated_manifest, event_type="manifest.updated")

    def enqueue_approval_notification(
        self,
        workspace_id: str,
        run_id: str,
        *,
        event_type: LocalApprovalNotificationEvent | str,
        channel: LocalApprovalNotificationChannel
        | str = LocalApprovalNotificationChannel.LOCAL_INBOX,
        approval_id: str | None = None,
        recipient_actor_id: str | None = None,
        recipient_role: str | None = None,
        reason: str = "",
        comment: str = "",
        notification_id: str | None = None,
        actor_id: str | None = None,
    ) -> LocalApprovalNotification:
        """Enqueue a display-safe local approval notification into the outbox.

        This only records local notification-workflow evidence that a future
        Platform/Cloud delivery adapter can consume out of band. It never sends
        email/Slack/OS notifications/webhooks, resolves secrets, calls a
        connector/model, or syncs to Cloud. Raw payloads, prompt/output text,
        secrets, and connection handles never reach the record or its event.

        The notification id is deterministic per run/event by default, so a
        repeated enqueue with identical metadata is idempotent: it returns the
        existing record without storing a duplicate or re-emitting an event.
        Fails closed if the run does not exist or the channel/event identifier
        is invalid.
        """

        event = _coerce_notification_event(event_type)
        channel_value = _coerce_notification_channel(channel)

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        automation = _find_resource(manifest.automations, run_record.automation_id)
        capability_id = automation.capability_id if automation is not None else None
        automation_id = automation.id if automation is not None else None

        now = _now()
        next_id = notification_id or (
            f"notification.{_resource_id_slug(event.value)}."
            f"{_resource_id_slug(run_id)}"
        )
        existing = _find_resource(manifest.local_approval_notifications, next_id)
        audit_event_id = f"control-plane-event-{uuid4()}"
        notification = LocalApprovalNotification(
            id=next_id,
            run_id=run_id,
            workspace_id=workspace_id,
            channel=channel_value,
            event_type=event,
            status=LocalApprovalNotificationStatus.QUEUED,
            approval_id=approval_id,
            capability_id=capability_id,
            automation_id=automation_id,
            recipient_actor_id=recipient_actor_id,
            recipient_role=recipient_role,
            reason=_redact_optional_metadata_text(reason),
            comment=_redact_optional_metadata_text(comment),
            attempt_count=0,
            last_error=None,
            audit_event_id=audit_event_id,
            created_at=now,
            updated_at=now,
        )
        if existing is not None and existing == replace(
            notification,
            audit_event_id=existing.audit_event_id,
            created_at=existing.created_at,
            updated_at=existing.updated_at,
        ):
            # An equivalent queued notification already exists for this
            # run/event; do not duplicate evidence or re-emit an audit event.
            return existing

        self._store_local_approval_notification(manifest, notification)
        self._insert_event(
            audit_event_id,
            workspace_id,
            "notification.approval.enqueued",
            "local_approval_notification",
            notification.id,
            {
                "notification_id": notification.id,
                "run_id": run_id,
                "approval_id": approval_id,
                "automation_id": automation_id,
                "capability_id": capability_id,
                "channel": channel_value.value,
                "event_type": event.value,
                "status": notification.status.value,
                "recipient_actor_id": recipient_actor_id,
                "recipient_role": recipient_role,
                "attempt_count": notification.attempt_count,
                "reason": notification.reason,
                "comment": notification.comment,
                "actor_id": actor_id,
                "trace_id": run_record.trace_id,
                "payload_redacted": True,
                "secret_value_redacted": True,
            },
            now,
        )
        return notification

    def update_approval_notification(
        self,
        workspace_id: str,
        notification_id: str,
        *,
        status: LocalApprovalNotificationStatus | str,
        last_error: str | None = None,
        comment: str | None = None,
        actor_id: str | None = None,
    ) -> LocalApprovalNotification:
        """Record a local notification attempt result reported by an adapter.

        This only updates local outbox metadata: it advances the attempt count,
        sets the new local status, and stores a redacted failure/comment note.
        It never sends anything, resolves secrets, calls a connector/model, or
        syncs to Cloud. Failure metadata is redacted before it lands and the
        audit event carries no secrets or raw payloads. Fails closed if the
        notification does not exist or the status identifier is invalid.
        """

        status_value = _coerce_notification_status(status)
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        existing = _find_resource(
            manifest.local_approval_notifications, notification_id
        )
        if existing is None:
            raise ControlPlaneRegistryError(
                f"notification {notification_id} does not exist"
            )

        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        notification = replace(
            existing,
            status=status_value,
            attempt_count=existing.attempt_count + 1,
            last_error=(
                _redact_optional_metadata_text(last_error)
                if last_error is not None
                else existing.last_error
            ),
            comment=(
                _redact_optional_metadata_text(comment)
                if comment is not None
                else existing.comment
            ),
            audit_event_id=audit_event_id,
            updated_at=now,
        )
        self._store_local_approval_notification(manifest, notification)
        self._insert_event(
            audit_event_id,
            workspace_id,
            "notification.approval.updated",
            "local_approval_notification",
            notification.id,
            {
                "notification_id": notification.id,
                "run_id": notification.run_id,
                "approval_id": notification.approval_id,
                "channel": notification.channel.value,
                "event_type": notification.event_type.value,
                "status": notification.status.value,
                "attempt_count": notification.attempt_count,
                "last_error": notification.last_error,
                "comment": notification.comment,
                "actor_id": actor_id,
                "payload_redacted": True,
                "secret_value_redacted": True,
            },
            now,
        )
        return notification

    def list_pending_approval_notifications(
        self, workspace_id: str
    ) -> list[LocalApprovalNotification]:
        """List queued local approval notifications in deterministic order.

        Returns only ``queued`` outbox records (the ones a future delivery
        adapter still has to act on), ordered by ``created_at`` then ``id`` so
        callers see a stable list. This is a read-only local view; it never
        sends anything or mutates state.
        """

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return []
        pending = [
            notification
            for notification in manifest.local_approval_notifications
            if notification.status == LocalApprovalNotificationStatus.QUEUED
        ]
        return sorted(pending, key=lambda item: (item.created_at, item.id))

    def _store_local_approval_notification(
        self, manifest: ControlPlaneManifest, notification: LocalApprovalNotification
    ) -> None:
        """Replace/append a notification record and persist a validated manifest."""

        notifications = [
            item
            for item in manifest.local_approval_notifications
            if item.id != notification.id
        ]
        notifications.append(notification)
        updated_manifest = replace(
            manifest, local_approval_notifications=notifications
        )
        result = validate_control_plane_manifest(updated_manifest)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self._store_manifest(updated_manifest, event_type="manifest.updated")

    def list_due_runs(
        self,
        workspace_id: str,
        *,
        now: str | None = None,
        limit: int | None = None,
        resume_reasons: frozenset[str] | None = None,
    ) -> list[RunRecord]:
        """Return queued local runs that are due for a future worker to pick up.

        This is a read-only planning/read-model view for a not-yet-built
        out-of-Core daemon worker. It returns only ``queued`` run records whose
        ``resume_after`` is missing/empty or has elapsed relative to ``now``,
        ordered deterministically: immediate (empty ``resume_after``) runs
        first, then by ``resume_after`` ascending, then by ``updated_at`` and
        ``id`` for stability.

        When ``resume_reasons`` is supplied, only queued runs whose
        ``resume_reason`` is in that set are returned; every other queued run
        (retry, trigger, resume, or unset) is withheld. This is the selection
        seam the post-approval worker pump uses to claim only approval-resumed
        work without touching normal retry/trigger queued runs. The default of
        ``None`` preserves the generic behavior of returning every due queued
        run regardless of resume reason.

        The method never executes, schedules, sleeps, mutates the registry,
        calls connectors, resolves secrets, or invokes a model provider. A run
        with a malformed ``resume_after`` is failed closed (withheld) without
        crashing or recording any audit event. The ``now`` override exists only
        for deterministic callers and tests.
        """

        if limit is not None and limit < 1:
            raise ControlPlaneRegistryError("limit must be >= 1")

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            return []

        now_ts = now or _now()
        try:
            now_dt = _parse_datetime(now_ts)
        except ValueError as error:
            raise ControlPlaneRegistryError(
                "now must be an ISO-8601 timestamp"
            ) from error

        due: list[tuple[tuple[int, datetime, str, str], RunRecord]] = []
        for run_record in manifest.run_records:
            if run_record.status != ControlPlaneRunStatus.QUEUED:
                continue
            if (
                resume_reasons is not None
                and (run_record.resume_reason or "") not in resume_reasons
            ):
                # Withhold queued runs that do not carry a requested resume
                # reason so a reason-scoped pump never claims other work.
                continue
            resume_after = run_record.resume_after
            if resume_after:
                try:
                    resume_dt = _parse_datetime(resume_after)
                except ValueError:
                    # Fail closed: never surface a run with a malformed
                    # resume_after and never mutate/audit on this read path.
                    continue
                if resume_dt > now_dt:
                    continue
                sort_key = (1, resume_dt, run_record.updated_at or "", run_record.id)
            else:
                # Immediate runs (no resume_after) sort ahead of dated runs.
                sort_key = (0, now_dt, run_record.updated_at or "", run_record.id)
            due.append((sort_key, run_record))

        due.sort(key=lambda item: item[0])
        ordered = [run_record for _, run_record in due]
        if limit is not None:
            return ordered[:limit]
        return ordered

    def claim_due_run(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        now: str | None = None,
        resume_reasons: frozenset[str] | None = None,
    ) -> RunRecord | None:
        """Claim the first due queued run for a local worker and mark it running.

        This is the worker-safe state transition a future out-of-Core daemon
        will call to pick up queued work. It selects the first eligible run
        using the same semantics and deterministic ordering as
        ``list_due_runs(..., limit=1)``, then transitions only that run from
        ``queued`` to ``running`` through the existing status-update path,
        preserving ``trace_id`` and ``retry_count``.

        When ``resume_reasons`` is supplied it is forwarded to
        ``list_due_runs`` selection, so the claim is scoped to queued runs
        carrying one of those resume reasons and never picks up other queued
        work. Selection reads the queued ``resume_reason``; the claim write then
        overwrites it via the existing ``worker_claimed`` resume materialization,
        and the payload compare-and-set still guards against the row changing
        between selection and write. The default ``None`` preserves the generic
        claim behavior.

        The method never executes the run, calls connectors, resolves secrets,
        invokes a model provider, schedules, or sleeps. If no run is due it
        returns ``None`` without mutating the registry or recording an audit
        event. A malformed ``worker_id`` raises ``ControlPlaneRegistryError``;
        a malformed ``now`` raises consistently with ``list_due_runs``. The
        ``now`` override exists only for deterministic callers and tests.

        Claiming is guarded by a local SQLite immediate transaction plus an
        atomic compare-and-set. Selection still uses
        ``list_due_runs(..., limit=1)``, but the selection, queued -> running
        write and ``run.worker.claimed`` event commit as one local write
        transaction. If another local worker already holds the write lock,
        SQLite serializes this claim attempt (or raises a lock error according
        to the connection busy timeout). If another writer changed the row
        before the conditional write, the claim is abandoned: no status or
        claim event is recorded and ``None`` is returned so the caller can
        re-select.

        Caveat: this is local SQLite process-level locking for one database
        file. It is still not a distributed Cloud worker-fleet lock.

        Running is an in-flight status, so the claim write does not clear
        ``resume_after``; it is preserved (the safer current-pattern behavior).
        The pre-claim ``resume_after`` is captured in the display-safe
        ``run.worker.claimed`` audit event.
        """

        if not worker_id or not worker_id.strip():
            raise ControlPlaneRegistryError("worker_id must be a non-empty string")

        now_ts = now or _now()
        try:
            with self.db.immediate_transaction():
                return self._claim_due_run_in_transaction(
                    workspace_id,
                    worker_id=worker_id,
                    now_ts=now_ts,
                    resume_reasons=resume_reasons,
                )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return None
            raise

    def _claim_due_run_in_transaction(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        now_ts: str,
        resume_reasons: frozenset[str] | None = None,
    ) -> RunRecord | None:
        """Claim a due run inside an active immediate transaction."""

        # Reuse list_due_runs for selection so ordering, queued-only filtering,
        # resume-reason scoping, malformed-now rejection, and fail-closed
        # handling of malformed per-run resume_after values stay identical to
        # the read-model view.
        due = self.list_due_runs(
            workspace_id, now=now_ts, limit=1, resume_reasons=resume_reasons
        )
        if not due:
            return None
        run_record = due[0]

        updated = self._claim_queued_run(
            workspace_id,
            run_record.id,
            now_ts,
            trace_id=run_record.trace_id,
            retry_count=run_record.retry_count,
        )
        if updated is None:
            # Lost the claim race: the selected row was no longer a due
            # queued run at write time (stale selection or compare-and-set
            # miss). Record no claim event and let the caller re-select.
            return None

        self._record_event(
            workspace_id,
            "run.worker.claimed",
            "run_record",
            run_record.id,
            {
                "run_id": run_record.id,
                "worker_id": worker_id,
                "status": ControlPlaneRunStatus.RUNNING.value,
                "resume_after": run_record.resume_after,
                "retry_count": run_record.retry_count,
                "trace_id": updated.trace_id,
                "secret_value_redacted": True,
            },
            now_ts,
        )
        return updated

    def process_due_runs_once(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        now: str | None = None,
        limit: int = 1,
        mode: ExecutionMode | str = ExecutionMode.DRY_RUN,
        actor_id: str | None = None,
        cost_center: str | None = None,
        actor_role: str | None = None,
        actor_attributes: Mapping[str, str] | None = None,
        workspace_attributes: Mapping[str, str] | None = None,
        max_steps: int = 4,
        input_payload: Mapping[str, Any] | None = None,
        output_payload: Mapping[str, Any] | None = None,
        estimated_cost_units: int = 0,
        estimated_token_usage: int = 0,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_token_usage: int = 0,
        model_cost_units: int = 0,
        invocation_type: str | None = None,
        resume_reasons: frozenset[str] | None = None,
        capability_executor: CapabilityExecutor | None = None,
    ) -> list[ExecutionResult]:
        """Pump up to ``limit`` due queued runs through the bounded executor once.

        This is a single bounded pass of the worker loop a future out-of-Core
        daemon will own, not the daemon itself. Each iteration claims the first
        eligible due run via :meth:`claim_due_run` (which reuses
        :meth:`list_due_runs` selection, so retry ``resume_after`` gating,
        queued-only filtering, deterministic ordering, and fail-closed handling
        all stay identical) and then runs it through :meth:`execute_run` with the
        supplied dry-run/supervised parameters.

        When ``resume_reasons`` is supplied it is forwarded to
        :meth:`claim_due_run`, so the pump claims only queued runs carrying one
        of those resume reasons. The default ``None`` preserves the generic
        behavior of pumping every due queued run. :meth:`process_approved_runs_once`
        is the bounded post-approval wrapper built on this seam.

        It does NOT itself start a thread, process, scheduler, sleep loop,
        network call, connector call, or model provider call, and it adds no
        model execution beyond the display-safe ``model_*`` metadata
        placeholders that :meth:`execute_run` already records. It can, however,
        forward an injected supervised safe/read ``capability_executor`` into
        :meth:`execute_run`, so worker/due-run paths can exercise the same
        executor-backed safe/read seam. The executor is the caller's
        responsibility; the pump never constructs one. Dry-run never calls the
        executor and approval-gated behavior remains governed by
        :meth:`execute_run` (dry-run keeps simulating; an approval-gated run
        pauses before the executor is reached). It performs at most ``limit``
        claim+execute cycles and returns the accumulated
        :class:`ExecutionResult` list, in claim order.

        Stopping rules:

        * If a claim returns ``None`` (no run due, lost race, or malformed
          ``resume_after``), the pump stops and returns the results gathered so
          far. Future retry runs are therefore never claimed or executed before
          their ``resume_after``.
        * If an execution result is ``paused`` (a supervised run waiting for
          approval), the pump appends that result and stops; it does not keep
          claiming more work behind a pending approval.

        The pump records no worker-complete event: per-run claim and execution
        evidence (including ``run.worker.claimed`` and the simulated capability
        invocation) is the audit trail, so a failure or exception cannot leave a
        misleading worker-complete marker behind. Execution exceptions propagate
        to the caller rather than being swallowed.

        A malformed ``worker_id`` or ``now`` is rejected by ``claim_due_run``;
        ``limit`` must be ``>= 1``. The ``now`` override exists only for
        deterministic callers and tests.

        Note: :meth:`execute_run` emits its own ``run.status.running`` (and step
        evidence) after the claim's ``run.worker.claimed`` event, so for a single
        claimed run the claim event precedes the execution evidence.
        """

        if limit < 1:
            raise ControlPlaneRegistryError("limit must be >= 1")

        results: list[ExecutionResult] = []
        for _ in range(limit):
            claimed = self.claim_due_run(
                workspace_id,
                worker_id=worker_id,
                now=now,
                resume_reasons=resume_reasons,
            )
            if claimed is None:
                # No more due work (or a lost claim race): stop and return what
                # has been processed so far without recording anything further.
                break

            result = self.execute_run(
                workspace_id,
                claimed.id,
                mode=mode,
                actor_id=actor_id,
                cost_center=cost_center,
                actor_role=actor_role,
                actor_attributes=actor_attributes,
                workspace_attributes=workspace_attributes,
                max_steps=max_steps,
                input_payload=input_payload,
                output_payload=output_payload,
                estimated_cost_units=estimated_cost_units,
                estimated_token_usage=estimated_token_usage,
                model_provider=model_provider,
                model_name=model_name,
                model_token_usage=model_token_usage,
                model_cost_units=model_cost_units,
                invocation_type=invocation_type,
                capability_executor=capability_executor,
            )
            results.append(result)

            if result.paused:
                # A supervised run is waiting for approval: do not claim more
                # work behind a pending human decision.
                break

        return results

    def process_approved_runs_once(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        now: str | None = None,
        limit: int = 1,
        mode: ExecutionMode | str = ExecutionMode.DRY_RUN,
        actor_id: str | None = None,
        cost_center: str | None = None,
        actor_role: str | None = None,
        actor_attributes: Mapping[str, str] | None = None,
        workspace_attributes: Mapping[str, str] | None = None,
        max_steps: int = 4,
        input_payload: Mapping[str, Any] | None = None,
        output_payload: Mapping[str, Any] | None = None,
        estimated_cost_units: int = 0,
        estimated_token_usage: int = 0,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_token_usage: int = 0,
        model_cost_units: int = 0,
        invocation_type: str | None = None,
        capability_executor: CapabilityExecutor | None = None,
    ) -> list[ExecutionResult]:
        """Pump only approval-resumed queued runs through the bounded executor.

        This is the post-approval half of the worker loop a future out-of-Core
        daemon will own. After a human approves a paused run via
        :meth:`record_approval_decision`, that run is requeued to ``queued`` with
        ``resume_reason='approval_approved'``. This bounded pass claims and
        executes only those approval-resumed runs: it delegates to
        :meth:`process_due_runs_once` with ``resume_reasons`` fixed to the single
        approval reason, so normal retry/trigger/resume queued runs are never
        claimed or executed here even when they are also due.

        Because the matching :class:`Approval` record already exists by the time
        the run is approval-resumed, the re-evaluated policy decision resolves to
        ``ALLOW_APPROVED`` and the capability runs to completion without pausing
        for approval again. Every boundary of :meth:`process_due_runs_once`
        holds: it starts no thread, process, scheduler, sleep loop, network call,
        connector call, or model provider call, and it adds no model execution
        beyond the display-safe ``model_*`` metadata placeholders
        :meth:`execute_run` already records.

        Like :meth:`process_due_runs_once`, this wrapper accepts an optional
        supervised safe/read ``capability_executor`` and forwards it unchanged.
        The seam is bounded identically to the due-run path: only
        :meth:`execute_run` invokes it, and only for a ``SAFE_EXECUTOR_SIDE_EFFECTS``
        (``none``/``read``) capability that has already resolved to ALLOW in
        supervised mode after approval. Dry-run never calls the executor, no
        write/send/network/secret/model execution is enabled, and the executor is
        the caller's responsibility; the pump never constructs one. When the
        executor is omitted the post-approval pump runs dry-run or
        simulated-supervised work only. It performs at most ``limit``
        claim+execute cycles and returns the accumulated results in claim order.

        The ``now`` override exists only for deterministic callers and tests.
        """

        return self.process_due_runs_once(
            workspace_id,
            worker_id=worker_id,
            now=now,
            limit=limit,
            mode=mode,
            actor_id=actor_id,
            cost_center=cost_center,
            actor_role=actor_role,
            actor_attributes=actor_attributes,
            workspace_attributes=workspace_attributes,
            max_steps=max_steps,
            input_payload=input_payload,
            output_payload=output_payload,
            estimated_cost_units=estimated_cost_units,
            estimated_token_usage=estimated_token_usage,
            model_provider=model_provider,
            model_name=model_name,
            model_token_usage=model_token_usage,
            model_cost_units=model_cost_units,
            invocation_type=invocation_type,
            resume_reasons=frozenset({APPROVAL_RESUME_REASON}),
            capability_executor=capability_executor,
        )

    def materialize_run_ledger_entry(
        self,
        workspace_id: str,
        run_id: str,
        *,
        task_id: str = "control-plane-automation",
        target_repo: str = "omnivia-core",
        lane_id: str = "control-plane",
        evidence_file_refs: list[EvidenceFileRef] | None = None,
    ) -> RunLedgerEntry:
        """Materialize a portable run-ledger entry from a local run record.

        The control plane keeps run records locally and does not replace the
        canonical run-ledger contract. This helper gives callers a validated
        ledger-shaped entry for append/export workflows.
        """

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        run_record = _find_resource(manifest.run_records, run_id)
        if run_record is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")

        updated_at = run_record.updated_at or _now()
        started_at = run_record.started_at or updated_at
        status = _run_ledger_status(run_record.status)
        completed_at = updated_at if status in _TERMINAL_RUN_LEDGER_STATUSES else None
        refs = evidence_file_refs or [
            EvidenceFileRef(
                path=f"control-plane/events/{workspace_id}/{run_id}",
                kind="control-plane-events",
                description="Local control-plane event correlation",
            )
        ]
        entry = RunLedgerEntry(
            run_id=run_record.run_ledger_entry_id or _run_ledger_entry_id(run_id),
            task_id=task_id,
            target_repo=target_repo,
            lane_id=lane_id,
            status=status,
            started_at=started_at,
            updated_at=updated_at,
            completed_at=completed_at,
            evidence_file_refs=refs,
            provenance=RunLedgerProvenance(
                producer="omnivia-memory.control-plane",
                source_ref=run_record.id,
                producer_version=str(CONTROL_PLANE_SCHEMA_VERSION),
            ),
        )
        return entry

    def record_local_observability_log(
        self,
        workspace_id: str,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        event_type: str = "local.log",
        message: str = "",
        metadata: dict[str, Any] | None = None,
        retention_days: int = 30,
    ) -> LocalObservabilityLogRecord:
        """Record redacted local-only log evidence for Dev/Platform views."""

        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        record = LocalObservabilityLogRecord(
            id=f"observability-log-{uuid4()}",
            workspace_id=workspace_id,
            run_id=run_id,
            trace_id=trace_id,
            event_type=event_type,
            message=_redact_observability_text(message),
            metadata=_redact_observability_payload(metadata or {}),
            retention_expires_at=_retention_expires_at(now, retention_days),
            audit_event_id=audit_event_id,
            created_at=now,
        )
        self._insert_event(
            audit_event_id,
            workspace_id,
            "observability.log",
            "observability_log",
            record.id,
            asdict(record),
            now,
        )
        return record

    def resolve_local_secret_reference(
        self,
        workspace_id: str,
        secret_ref: str,
        local_vault: Mapping[str, str],
        *,
        actor_id: str | None = None,
    ) -> SecretResolutionResult:
        """Resolve a local secret ref without storing or returning raw material."""

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        metadata = next(
            (
                item
                for item in manifest.secret_metadata
                if item.secret_ref == secret_ref
            ),
            None,
        )
        if metadata is None:
            return self._record_secret_resolution(
                workspace_id,
                secret_ref,
                resolved=False,
                actor_id=actor_id,
                error="unknown_secret_ref",
            )
        if metadata.storage_scope != SecretStorageScope.LOCAL_ONLY:
            return self._record_secret_resolution(
                workspace_id,
                secret_ref,
                resolved=False,
                actor_id=actor_id,
                provider=metadata.provider,
                storage_scope=metadata.storage_scope,
                client_owned=metadata.client_owned,
                error="non_local_secret_scope",
            )
        if secret_ref not in local_vault or not local_vault[secret_ref]:
            return self._record_secret_resolution(
                workspace_id,
                secret_ref,
                resolved=False,
                actor_id=actor_id,
                provider=metadata.provider,
                storage_scope=metadata.storage_scope,
                client_owned=metadata.client_owned,
                error="secret_not_available",
            )
        return self._record_secret_resolution(
            workspace_id,
            secret_ref,
            resolved=True,
            actor_id=actor_id,
            provider=metadata.provider,
            storage_scope=metadata.storage_scope,
            client_owned=metadata.client_owned,
        )

    def get_run_observability_events(
        self, workspace_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        """Return local evidence events for a run with audit row IDs included."""

        rows = self.db.execute(
            """
            SELECT id, event_type, resource_type, resource_id, payload_json, created_at
            FROM control_plane_events
            WHERE workspace_id = ?
            ORDER BY created_at, id
            """,
            (workspace_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("run_id") != run_id:
                continue
            events.append(
                {
                    "audit_event_id": row["id"],
                    "event_type": row["event_type"],
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "created_at": row["created_at"],
                    "payload": payload,
                }
            )
        return events

    def _local_usage_by_run(
        self, workspace_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return per-run effective usage and display-safe attribution.

        Each entry maps a run id to ``{"token_usage", "cost_units", "actor_id",
        "cost_center"}``. Capability invocation estimates and model invocation
        usage are summed per run, and a run with a real model invocation uses its
        model usage in place of the capability estimate so the two are never
        double counted. ``actor_id``/``cost_center`` are captured from the safe
        usage event metadata (first non-empty value seen for the run).
        """

        rows = self.db.execute(
            """
            SELECT event_type, payload_json FROM control_plane_events
            WHERE workspace_id = ?
              AND (
                event_type LIKE 'capability.invocation.%'
                OR event_type LIKE 'model.invocation.%'
              )
            ORDER BY created_at
            """,
            (workspace_id,),
        ).fetchall()
        capability_usage_by_run: dict[str, list[int]] = {}
        model_usage_by_run: dict[str, list[int]] = {}
        model_invoked_runs: set[str] = set()
        actor_by_run: dict[str, str | None] = {}
        cost_center_by_run: dict[str, str | None] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            event_type = str(row["event_type"])
            run_id = str(payload.get("run_id") or "")
            token = int(payload.get("token_usage") or 0)
            cost = int(payload.get("cost_units") or 0)
            actor_id = payload.get("actor_id")
            cost_center = payload.get("cost_center")
            if actor_by_run.get(run_id) in (None, "") and actor_id:
                actor_by_run[run_id] = str(actor_id)
            if cost_center_by_run.get(run_id) in (None, "") and cost_center:
                cost_center_by_run[run_id] = str(cost_center)
            if event_type.startswith("model.invocation."):
                usage = model_usage_by_run.setdefault(run_id, [0, 0])
                usage[0] += token
                usage[1] += cost
                if str(payload.get("model_provider") or "not_invoked") != "not_invoked":
                    model_invoked_runs.add(run_id)
                continue
            usage = capability_usage_by_run.setdefault(run_id, [0, 0])
            usage[0] += token
            usage[1] += cost

        usage_by_run: dict[str, dict[str, Any]] = {}
        for run_id in set(capability_usage_by_run) | set(model_usage_by_run):
            if run_id in model_invoked_runs:
                token, cost = model_usage_by_run.get(run_id, [0, 0])
            else:
                token, cost = capability_usage_by_run.get(run_id, [0, 0])
            usage_by_run[run_id] = {
                "token_usage": token,
                "cost_units": cost,
                "actor_id": actor_by_run.get(run_id),
                "cost_center": cost_center_by_run.get(run_id),
            }
        return usage_by_run

    def _aggregate_local_usage(self, workspace_id: str) -> tuple[int, int]:
        """Aggregate local token/cost usage as ``(token_usage, cost_units)``.

        This mirrors the usage accounting in
        :meth:`summarize_observability_metrics`: capability invocation estimates
        and model invocation usage are summed per run, and a run with a real
        model invocation uses its model usage in place of the capability
        estimate so the two are never double counted.
        """

        token_usage = 0
        cost_units = 0
        for usage in self._local_usage_by_run(workspace_id).values():
            token_usage += int(usage["token_usage"])
            cost_units += int(usage["cost_units"])
        return token_usage, cost_units

    def summarize_usage_by_actor(
        self, workspace_id: str
    ) -> list[LocalUsageLedgerEntry]:
        """Summarize local usage attribution per actor for display surfaces.

        Runs without an ``actor_id`` in their safe usage event metadata are
        omitted from the per-actor ledger (they still contribute to the
        workspace aggregate in :meth:`summarize_observability_metrics`). Usage
        uses the same model-supersedes-capability accounting so each run counts
        once.
        """

        return self._summarize_usage_ledger(workspace_id, "actor")

    def summarize_usage_by_cost_center(
        self, workspace_id: str
    ) -> list[LocalUsageLedgerEntry]:
        """Summarize local usage attribution per cost center for display.

        Runs without a ``cost_center`` in their safe usage event metadata are
        omitted from the per-cost-center ledger. Usage uses the same
        model-supersedes-capability accounting so each run counts once.
        """

        return self._summarize_usage_ledger(workspace_id, "cost_center")

    def _summarize_usage_ledger(
        self, workspace_id: str, subject_type: str
    ) -> list[LocalUsageLedgerEntry]:
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )

        subject_key = "actor_id" if subject_type == "actor" else subject_type
        buckets: dict[str, list[int]] = {}
        for usage in self._local_usage_by_run(workspace_id).values():
            subject_id = usage.get(subject_key)
            if not subject_id:
                continue
            bucket = buckets.setdefault(str(subject_id), [0, 0, 0])
            bucket[0] += 1
            bucket[1] += int(usage["token_usage"])
            bucket[2] += int(usage["cost_units"])

        now = _now()
        return [
            LocalUsageLedgerEntry(
                workspace_id=workspace_id,
                subject_type=subject_type,
                subject_id=subject_id,
                run_count=run_count,
                token_usage=token_usage,
                cost_units=cost_units,
                generated_at=now,
            )
            for subject_id, (run_count, token_usage, cost_units) in sorted(
                buckets.items()
            )
        ]

    def summarize_observability_metrics(
        self, workspace_id: str
    ) -> RunObservabilityMetrics:
        """Summarize local run, cost, retry, and connector-health evidence."""

        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )

        run_count = len(manifest.run_records)
        completed_count = len(
            [
                run
                for run in manifest.run_records
                if run.status
                in {ControlPlaneRunStatus.COMPLETED, ControlPlaneRunStatus.SUCCEEDED}
            ]
        )
        failed_count = len(
            [
                run
                for run in manifest.run_records
                if run.status == ControlPlaneRunStatus.FAILED
            ]
        )
        waiting_count = len(
            [
                run
                for run in manifest.run_records
                if run.status == ControlPlaneRunStatus.WAITING_FOR_APPROVAL
            ]
        )
        terminal_count = completed_count + failed_count
        success_rate = completed_count / terminal_count if terminal_count else 0.0

        step_rows = self.db.execute(
            """
            SELECT payload_json FROM control_plane_events
            WHERE workspace_id = ? AND event_type LIKE 'run.step.%'
            """,
            (workspace_id,),
        ).fetchall()
        retry_count = 0
        for row in step_rows:
            payload = json.loads(row["payload_json"])
            attempt = int(payload.get("attempt") or 1)
            if attempt > 1:
                retry_count += attempt - 1

        # Count model usage exactly once per run. When a run has a real model
        # invocation, its model token/cost supersedes the capability estimate so
        # the two are never double counted; otherwise the capability estimate
        # stands in for the run's usage.
        token_usage, cost_units = self._aggregate_local_usage(workspace_id)

        active_connections = len(
            [
                connection
                for connection in manifest.connections
                if connection.lifecycle == LifecycleState.ACTIVE
            ]
        )
        return RunObservabilityMetrics(
            workspace_id=workspace_id,
            run_count=run_count,
            completed_count=completed_count,
            failed_count=failed_count,
            waiting_for_approval_count=waiting_count,
            success_rate=success_rate,
            retry_count=retry_count,
            token_usage=token_usage,
            cost_units=cost_units,
            connector_health={
                "source": "local_registry",
                "active_connections": active_connections,
                "remote_health_checks": "not_configured",
            },
            generated_at=_now(),
        )

    def project_redacted_otel_observability(
        self,
        workspace_id: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a read-only redacted OTel-style projection of local evidence.

        This is a display/export view only: it reads existing
        ``control_plane_events`` rows and
        :meth:`summarize_observability_metrics` and never writes to canonical
        control-plane state, so ``control_plane_events`` is left unchanged.
        Span attributes are copied from an explicit allowlist rather than the
        raw event payload, so raw prompts, outputs, secrets, secret refs,
        token/connector values, and arbitrary payload keys never reach the
        projection.
        """

        metrics = self.summarize_observability_metrics(workspace_id)

        rows = self.db.execute(
            """
            SELECT id, event_type, resource_type, resource_id, payload_json, created_at
            FROM control_plane_events
            WHERE workspace_id = ?
            ORDER BY created_at, id
            """,
            (workspace_id,),
        ).fetchall()

        spans: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if run_id is not None and payload.get("run_id") != run_id:
                continue
            raw_trace_id = payload.get("trace_id")
            if not raw_trace_id:
                # No trace correlation: skip rather than fabricate one.
                continue
            trace_id = _redact_observability_text(str(raw_trace_id))
            audit_event_id = str(row["id"])
            raw_span_id = payload.get("span_id")
            span_id = (
                _redact_observability_text(str(raw_span_id))
                if raw_span_id
                else f"projected.{audit_event_id}"
            )
            event_type = str(row["event_type"])
            spans.append(
                {
                    "name": _redact_observability_text(event_type),
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": None,
                    "kind": "internal",
                    "status": _observability_span_status(event_type, payload),
                    "created_at": row["created_at"],
                    "attributes": _observability_span_attributes(
                        audit_event_id,
                        event_type,
                        row["resource_type"],
                        row["resource_id"],
                        str(row["created_at"]),
                        payload,
                    ),
                }
            )

        return {
            "schema": "omnivia.control_plane.redacted_otel_projection.v1",
            "source": "local-registry",
            "redacted": True,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "generated_at": _now(),
            "spans": spans,
            "metrics": asdict(metrics),
        }

    def _coerce_valid_manifest(
        self, manifest: ControlPlaneManifest | dict[str, Any]
    ) -> ControlPlaneManifest:
        result = validate_control_plane_manifest(manifest)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        if isinstance(manifest, ControlPlaneManifest):
            return manifest
        return manifest_from_dict(manifest)

    def _store_manifest(
        self, manifest: ControlPlaneManifest, event_type: str
    ) -> None:
        now = _now()
        payload = _manifest_to_payload(manifest)
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO control_plane_manifests (
                    workspace_id, schema_version, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    manifest.workspace.id,
                    manifest.schema_version,
                    json.dumps(payload, sort_keys=True),
                    now,
                    now,
                ),
            )
            self.db.execute(
                "DELETE FROM control_plane_resources WHERE workspace_id = ?",
                (manifest.workspace.id,),
            )
            for field_name, resource_type in RESOURCE_LIST_FIELDS:
                for resource in getattr(manifest, field_name):
                    resource_payload = _to_jsonable(asdict(resource))
                    self.db.execute(
                        """
                        INSERT INTO control_plane_resources (
                            workspace_id, resource_type, resource_id, lifecycle,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            manifest.workspace.id,
                            resource_type,
                            resource.id,
                            _resource_lifecycle(resource_payload),
                            json.dumps(resource_payload, sort_keys=True),
                            now,
                            now,
                        ),
                    )
            self._record_event(
                manifest.workspace.id,
                event_type,
                None,
                None,
                {"schema_version": manifest.schema_version},
                now,
            )

    def _set_lifecycle(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        lifecycle: LifecycleState,
    ) -> None:
        row = self.db.execute(
            """
            SELECT payload_json FROM control_plane_resources
            WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?
            """,
            (workspace_id, resource_type, resource_id),
        ).fetchone()
        if row is None:
            raise ControlPlaneRegistryError(
                f"{resource_type} {resource_id} does not exist in {workspace_id}"
            )

        payload = json.loads(row["payload_json"])
        payload["lifecycle"] = lifecycle.value
        manifest_payload = self._manifest_payload_with_resource(
            workspace_id, resource_type, resource_id, payload
        )
        result = validate_control_plane_manifest(manifest_payload)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))

        now = _now()
        self.db.execute(
            """
            UPDATE control_plane_resources
            SET lifecycle = ?, payload_json = ?, updated_at = ?
            WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?
            """,
            (
                lifecycle.value,
                json.dumps(payload, sort_keys=True),
                now,
                workspace_id,
                resource_type,
                resource_id,
            ),
        )
        self._record_event(
            workspace_id,
            f"resource.{lifecycle.value}",
            resource_type,
            resource_id,
            {"lifecycle": lifecycle.value},
            now,
        )

    def _load_resource_payload(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
    ) -> dict[str, Any]:
        row = self.db.execute(
            """
            SELECT payload_json FROM control_plane_resources
            WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?
            """,
            (workspace_id, resource_type, resource_id),
        ).fetchone()
        if row is None:
            raise ControlPlaneRegistryError(
                f"{resource_type} {resource_id} does not exist in {workspace_id}"
            )
        payload: dict[str, Any] = json.loads(row["payload_json"])
        return payload

    def _manifest_payload_with_resource(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        replacement_payload: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        payload = _manifest_to_payload(manifest)
        for field_name, current_resource_type in RESOURCE_LIST_FIELDS:
            if current_resource_type != resource_type:
                continue
            resources = []
            for resource in payload[field_name]:
                if resource["id"] == resource_id:
                    resources.append(replacement_payload)
                else:
                    resources.append(resource)
            payload[field_name] = resources
        return payload

    def _record_event(
        self,
        workspace_id: str,
        event_type: str,
        resource_type: str | None,
        resource_id: str | None,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        self._insert_event(
            f"control-plane-event-{uuid4()}",
            workspace_id,
            event_type,
            resource_type,
            resource_id,
            payload,
            created_at,
        )

    def _insert_event(
        self,
        event_id: str,
        workspace_id: str,
        event_type: str,
        resource_type: str | None,
        resource_id: str | None,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO control_plane_events (
                id, workspace_id, event_type, resource_type, resource_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                workspace_id,
                event_type,
                resource_type,
                resource_id,
                json.dumps(_to_jsonable(payload), sort_keys=True),
                created_at,
            ),
        )

    def _record_event_with_id(
        self,
        workspace_id: str,
        event_type: str,
        resource_type: str | None,
        resource_id: str | None,
        payload: dict[str, Any],
        created_at: str,
    ) -> str:
        event_id = f"control-plane-event-{uuid4()}"
        self._insert_event(
            event_id,
            workspace_id,
            event_type,
            resource_type,
            resource_id,
            payload,
            created_at,
        )
        return event_id

    def _record_dead_letter(
        self,
        workspace_id: str,
        envelope: TriggerEventEnvelope,
        reason: str,
        idempotency_key: str,
        created_at: str,
    ) -> TriggerIngestionResult:
        audit_event_id = self._record_event_with_id(
            workspace_id,
            "trigger.dead_letter",
            "trigger",
            envelope.trigger_id,
            {
                "event_id": envelope.id,
                "event_type": envelope.event_type,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
            created_at,
        )
        return TriggerIngestionResult(
            accepted=False,
            dead_letter_reason=reason,
            audit_event_id=audit_event_id,
        )

    def _find_run_for_idempotency_key(
        self, workspace_id: str, idempotency_key: str
    ) -> str | None:
        rows = self.db.execute(
            """
            SELECT payload_json FROM control_plane_events
            WHERE workspace_id = ?
              AND event_type = 'trigger.accepted'
              AND resource_id = ?
            ORDER BY created_at
            """,
            (workspace_id, idempotency_key),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return None

    def _find_recent_trigger_run(
        self,
        workspace_id: str,
        trigger_id: str,
        now: str,
        window_seconds: int,
        *,
        mode: str,
        envelope: TriggerEventEnvelope | None = None,
    ) -> str | None:
        cutoff = _parse_datetime(now) - timedelta(seconds=window_seconds)
        rows = self.db.execute(
            """
            SELECT payload_json, created_at FROM control_plane_events
            WHERE workspace_id = ?
              AND event_type = 'trigger.accepted'
            ORDER BY created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
        for row in rows:
            created_at = str(row["created_at"])
            try:
                event_time = _parse_datetime(created_at)
            except ValueError:
                continue
            if event_time < cutoff:
                break
            payload = json.loads(row["payload_json"])
            if payload.get("trigger_id") != trigger_id:
                continue
            if mode == "debounce" and envelope is not None:
                if payload.get("event_type") != envelope.event_type:
                    continue
                previous_subject = payload.get("subject")
                previous_data_ref = payload.get("data_ref")
                if envelope.subject is not None:
                    if previous_subject != envelope.subject:
                        continue
                elif envelope.data_ref is not None:
                    if previous_data_ref != envelope.data_ref:
                        continue
                else:
                    continue
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return None

    def _append_run_record(
        self,
        workspace_id: str,
        run_record: RunRecord,
        envelope: TriggerEventEnvelope,
        idempotency_key: str,
        created_at: str,
    ) -> str:
        manifest = self.get_manifest(workspace_id)
        if manifest is None:
            raise ControlPlaneRegistryError(
                f"manifest for workspace {workspace_id} does not exist"
            )
        manifest_payload = _manifest_to_payload(manifest)
        run_payload = _to_jsonable(asdict(run_record))
        manifest_payload.setdefault("run_records", []).append(run_payload)
        result = validate_control_plane_manifest(manifest_payload)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))

        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO control_plane_resources (
                    workspace_id, resource_type, resource_id, lifecycle,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    "run_record",
                    run_record.id,
                    "immutable",
                    json.dumps(run_payload, sort_keys=True),
                    created_at,
                    created_at,
                ),
            )
            return self._record_event_with_id(
                workspace_id,
                "trigger.accepted",
                "trigger_event",
                idempotency_key,
                {
                    "event_id": envelope.id,
                    "event_type": envelope.event_type,
                    "trigger_id": envelope.trigger_id,
                    "run_id": run_record.id,
                    "automation_id": run_record.automation_id,
                    "trace_id": run_record.trace_id,
                    "idempotency_key": idempotency_key,
                    "subject": envelope.subject,
                    "occurred_at": envelope.occurred_at,
                    "actor_id": envelope.actor_id,
                    "data_ref": envelope.data_ref,
                },
                created_at,
            )

    def _record_run_step(
        self,
        workspace_id: str,
        step: RunStepRecord,
        extra_payload: dict[str, Any] | None = None,
    ) -> RunStepRecord:
        audit_event_id = step.audit_event_id or f"control-plane-event-{uuid4()}"
        step_with_audit = replace(step, audit_event_id=audit_event_id)
        payload = _to_jsonable(asdict(step_with_audit))
        if extra_payload:
            payload.update(_to_jsonable(extra_payload))
        self._insert_event(
            audit_event_id,
            workspace_id,
            f"run.step.{step.step_type.value}.{step.status.value}",
            "run_step",
            step.id,
            payload,
            step.created_at or _now(),
        )
        return step_with_audit

    def _run_supervised_executor(
        self,
        executor: CapabilityExecutor,
        *,
        capability: Capability,
        connection: Connection | None,
        decision: PolicyDecisionRecord,
        workspace_id: str,
        run_id: str,
        trace_id: str,
        span_id: str,
        input_payload: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
        """Invoke a supervised executor with a sanitized context, fail closed.

        Returns ``(output, diagnostics, error)``. ``error`` is non-None when the
        executor raised or returned an unusable (non-mapping) result; the caller
        then records a failed capability step and never emits a completed
        capability invocation event. Schema validation of a mapping output is
        left to the shared output-validation path so an invalid output also
        fails closed.
        """

        context = CapabilityExecutionContext(
            workspace_id=workspace_id,
            run_id=run_id,
            capability_id=capability.id,
            side_effect=capability.side_effect.value,
            input_schema=_schema_summary(capability.input_schema),
            output_schema=_schema_summary(capability.output_schema),
            trace_id=trace_id,
            span_id=span_id,
            policy_decision_id=decision.id,
            connection_id=capability.connection_id,
            connection_kind=connection.kind.value if connection is not None else None,
            input_payload=dict(input_payload) if input_payload is not None else None,
        )
        try:
            raw = executor(context)
        except Exception:
            # Fail closed: any executor exception becomes a failed capability
            # step with no completed capability invocation event.
            return None, None, "capability_executor_error"

        if isinstance(raw, CapabilityExecutionOutput):
            output: Any = raw.output
            diagnostics = raw.diagnostics
        elif isinstance(raw, Mapping):
            output = raw
            diagnostics = None
        else:
            return None, None, "invalid_executor_output"

        if not isinstance(output, Mapping):
            return None, None, "invalid_executor_output"
        return output, diagnostics, None

    def _record_capability_invocation(
        self,
        workspace_id: str,
        step: RunStepRecord,
        decision: PolicyDecisionRecord,
        capability: Capability,
        automation: Automation,
        mode: str,
        *,
        simulated: bool,
        executor_backed: bool = False,
        executor_metadata: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        actor_id: str | None = None,
        cost_center: str | None = None,
    ) -> str:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        payload: dict[str, Any] = {
            "audit_event_id": audit_event_id,
            "run_id": step.run_id,
            "step_id": step.id,
            "trace_id": step.trace_id,
            "span_id": step.span_id,
            "capability_id": step.capability_id,
            "agent_id": step.agent_id,
            "actor_id": actor_id,
            "cost_center": cost_center,
            "policy_decision_id": decision.id,
            "policy_audit_event_id": decision.audit_event_id,
            "mode": mode,
            "simulated": simulated,
            "executor_backed": executor_backed,
            "input_schema": _schema_summary(capability.input_schema),
            "output_schema": _schema_summary(capability.output_schema),
            "execution_limits": _execution_limit_payload(automation),
            "attempt": step.attempt,
            "retry_count": max(step.attempt - 1, 0),
            "token_usage": step.token_usage,
            "cost_units": step.cost_units,
            "created_at": now,
        }
        if output_summary is not None:
            payload["output_summary"] = output_summary
        if executor_backed:
            # Always present (even if empty) so executor-backed evidence is
            # explicit that diagnostics were sanitized rather than omitted.
            payload["executor_metadata"] = executor_metadata or {}
        self._insert_event(
            audit_event_id,
            workspace_id,
            (
                "capability.invocation.simulated"
                if simulated
                else "capability.invocation.completed"
            ),
            "capability",
            step.capability_id,
            payload,
            now,
        )
        return audit_event_id

    def _record_model_invocation(
        self,
        workspace_id: str,
        record: LocalModelInvocationRecord,
        *,
        actor_id: str | None = None,
        cost_center: str | None = None,
    ) -> LocalModelInvocationRecord:
        audit_event_id = record.audit_event_id or f"control-plane-event-{uuid4()}"
        record_with_audit = replace(record, audit_event_id=audit_event_id)
        payload = _to_jsonable(asdict(record_with_audit))
        # Display-safe usage attribution metadata only; no prompt/output text.
        payload["actor_id"] = actor_id
        payload["cost_center"] = cost_center
        self._insert_event(
            audit_event_id,
            workspace_id,
            "model.invocation.planned",
            "model_invocation",
            record.id,
            payload,
            record.created_at or _now(),
        )
        return record_with_audit

    def _record_approval_wait(
        self,
        workspace_id: str,
        step: RunStepRecord,
        decision: PolicyDecisionRecord,
        mode: str,
    ) -> str:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        self._insert_event(
            audit_event_id,
            workspace_id,
            "approval.wait.started",
            "approval_wait",
            step.id,
            {
                "audit_event_id": audit_event_id,
                "run_id": step.run_id,
                "step_id": step.id,
                "trace_id": step.trace_id,
                "span_id": step.span_id,
                "capability_id": step.capability_id,
                "agent_id": step.agent_id,
                "policy_decision_id": decision.id,
                "policy_audit_event_id": decision.audit_event_id,
                "mode": mode,
                "created_at": now,
            },
            now,
        )
        return audit_event_id

    def _record_payload_validation_event(
        self,
        workspace_id: str,
        run_id: str,
        capability_id: str,
        trace_id: str,
        direction: str,
        errors: list[str],
    ) -> str:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        self._insert_event(
            audit_event_id,
            workspace_id,
            f"run.payload.{direction}.invalid",
            "payload_validation",
            run_id,
            {
                "audit_event_id": audit_event_id,
                "run_id": run_id,
                "capability_id": capability_id,
                "trace_id": trace_id,
                "direction": direction,
                "valid": False,
                "errors": errors,
                "payload_redacted": True,
                "created_at": now,
            },
            now,
        )
        return audit_event_id

    def _record_secret_resolution(
        self,
        workspace_id: str,
        secret_ref: str,
        *,
        resolved: bool,
        actor_id: str | None,
        provider: str = "",
        storage_scope: SecretStorageScope = SecretStorageScope.LOCAL_ONLY,
        client_owned: bool = False,
        error: str | None = None,
    ) -> SecretResolutionResult:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        result = SecretResolutionResult(
            secret_ref=secret_ref,
            resolved=resolved,
            provider=provider,
            storage_scope=storage_scope,
            client_owned=client_owned,
            audit_event_id=audit_event_id,
            error=error,
        )
        self._insert_event(
            audit_event_id,
            workspace_id,
            "secret.resolution.succeeded" if resolved else "secret.resolution.failed",
            "secret_metadata",
            secret_ref,
            {
                "audit_event_id": audit_event_id,
                "secret_ref": secret_ref,
                "resolved": resolved,
                "provider": provider,
                "storage_scope": storage_scope.value,
                "client_owned": client_owned,
                "actor_id": actor_id,
                "error": error,
                "secret_value_redacted": True,
                "created_at": now,
            },
            now,
        )
        return result

    def _record_consultant_audit_event(
        self,
        workspace_id: str,
        consultant_id: str,
        grant_id: str,
        action: str,
        decision: PolicyDecisionRecord,
    ) -> str:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        self._insert_event(
            audit_event_id,
            workspace_id,
            f"consultant.action.{decision.decision.value}",
            "consultant_access_grant",
            grant_id,
            {
                "audit_event_id": audit_event_id,
                "consultant_id": consultant_id,
                "client_workspace_id": workspace_id,
                "grant_id": grant_id,
                "action": action,
                "policy_decision_id": decision.id,
                "policy_audit_event_id": decision.audit_event_id,
                "decision": decision.decision.value,
                "reason_code": decision.reason_code.value,
                "policy_ids": decision.policy_ids,
                "created_at": now,
            },
            now,
        )
        return audit_event_id

    def _update_run_status(
        self,
        workspace_id: str,
        run_id: str,
        status: ControlPlaneRunStatus,
        updated_at: str,
        *,
        trace_id: str | None = None,
        retry_count: int | None = None,
        resume_reason: str = "status_update",
        resume_after: str | None = None,
    ) -> RunRecord:
        row = self.db.execute(
            """
            SELECT payload_json FROM control_plane_resources
            WHERE workspace_id = ? AND resource_type = 'run_record'
              AND resource_id = ?
            """,
            (workspace_id, run_id),
        ).fetchone()
        if row is None:
            raise ControlPlaneRegistryError(f"run {run_id} does not exist")
        payload = json.loads(row["payload_json"])
        payload["status"] = status.value
        payload["updated_at"] = updated_at
        if trace_id is not None:
            payload["trace_id"] = trace_id
        if retry_count is not None:
            payload["retry_count"] = retry_count
        if resume_after is not None:
            payload["resume_after"] = resume_after
        payload = _materialize_resume_payload(payload, updated_at, reason=resume_reason)
        manifest_payload = self._manifest_payload_with_resource(
            workspace_id,
            "run_record",
            run_id,
            payload,
        )
        result = validate_control_plane_manifest(manifest_payload)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))
        self.db.execute(
            """
            UPDATE control_plane_resources
            SET payload_json = ?, updated_at = ?
            WHERE workspace_id = ? AND resource_type = 'run_record'
              AND resource_id = ?
            """,
            (
                json.dumps(payload, sort_keys=True),
                updated_at,
                workspace_id,
                run_id,
            ),
        )
        self._record_event(
            workspace_id,
            f"run.status.{status.value}",
            "run_record",
            run_id,
            {"run_id": run_id, "status": status.value, "trace_id": payload.get("trace_id")},
            updated_at,
        )
        return RunRecord(
            id=str(payload["id"]),
            automation_id=str(payload["automation_id"]),
            status=status,
            run_ledger_ref=_optional_str(payload.get("run_ledger_ref")),
            run_ledger_entry_id=_optional_str(payload.get("run_ledger_entry_id")),
            trace_id=_optional_str(payload.get("trace_id")),
            retention_expires_at=_optional_str(payload.get("retention_expires_at")),
            started_at=_optional_str(payload.get("started_at")),
            updated_at=updated_at,
            resume_token=_optional_str(payload.get("resume_token")),
            resume_after=_optional_str(payload.get("resume_after")),
            resume_reason=_optional_str(payload.get("resume_reason")),
            retry_count=int(payload.get("retry_count", 0)),
        )

    def _claim_queued_run(
        self,
        workspace_id: str,
        run_id: str,
        now_ts: str,
        *,
        trace_id: str | None,
        retry_count: int,
    ) -> RunRecord | None:
        """Conditionally transition a selected queued run to ``running``.

        This is the atomic compare-and-set backing :meth:`claim_due_run`. It
        reloads the selected run's stored row, fails closed unless it is still a
        due queued run, builds the running payload exactly as
        :meth:`_update_run_status` would, validates the resulting manifest, then
        performs an UPDATE guarded by the exact pre-claim ``payload_json``. If
        another writer changed the row between this read and the write, the row
        count is 0 and the claim is abandoned: ``None`` is returned and no
        ``run.status.running`` event is recorded.

        The public claim path wraps selection and this conditional write in a
        local SQLite immediate transaction. This helper keeps the payload
        compare-and-set guard so claims still fail closed if the selected row
        changes unexpectedly.
        """

        row = self.db.execute(
            """
            SELECT payload_json FROM control_plane_resources
            WHERE workspace_id = ? AND resource_type = 'run_record'
              AND resource_id = ?
            """,
            (workspace_id, run_id),
        ).fetchone()
        if row is None:
            return None
        current_payload_json = row["payload_json"]
        payload = json.loads(current_payload_json)
        if payload.get("status") != ControlPlaneRunStatus.QUEUED.value:
            # Lost race: the selected row is no longer the queued run we picked.
            return None
        if not _run_payload_due(payload, now_ts):
            # The stored row changed to a not-yet-due (or malformed) queued state
            # since selection; fail closed without claiming.
            return None

        if trace_id is not None:
            payload["trace_id"] = trace_id
        payload["retry_count"] = retry_count
        payload["status"] = ControlPlaneRunStatus.RUNNING.value
        payload["updated_at"] = now_ts
        payload = _materialize_resume_payload(
            payload, now_ts, reason="worker_claimed"
        )

        manifest_payload = self._manifest_payload_with_resource(
            workspace_id,
            "run_record",
            run_id,
            payload,
        )
        result = validate_control_plane_manifest(manifest_payload)
        if not result.valid:
            raise ControlPlaneRegistryError("; ".join(result.errors))

        # Conditional claim write: only succeeds if the row still holds the exact
        # pre-claim payload. A mismatched/lost row yields rowcount 0.
        cursor = self.db.execute(
            """
            UPDATE control_plane_resources
            SET payload_json = ?, updated_at = ?
            WHERE workspace_id = ? AND resource_type = 'run_record'
              AND resource_id = ? AND payload_json = ?
            """,
            (
                json.dumps(payload, sort_keys=True),
                now_ts,
                workspace_id,
                run_id,
                current_payload_json,
            ),
        )
        if cursor.rowcount != 1:
            # Compare-and-set miss: the row changed between read and write.
            return None

        self._record_event(
            workspace_id,
            f"run.status.{ControlPlaneRunStatus.RUNNING.value}",
            "run_record",
            run_id,
            {
                "run_id": run_id,
                "status": ControlPlaneRunStatus.RUNNING.value,
                "trace_id": payload.get("trace_id"),
            },
            now_ts,
        )
        return RunRecord(
            id=str(payload["id"]),
            automation_id=str(payload["automation_id"]),
            status=ControlPlaneRunStatus.RUNNING,
            run_ledger_ref=_optional_str(payload.get("run_ledger_ref")),
            run_ledger_entry_id=_optional_str(payload.get("run_ledger_entry_id")),
            trace_id=_optional_str(payload.get("trace_id")),
            retention_expires_at=_optional_str(payload.get("retention_expires_at")),
            started_at=_optional_str(payload.get("started_at")),
            updated_at=now_ts,
            resume_token=_optional_str(payload.get("resume_token")),
            resume_after=_optional_str(payload.get("resume_after")),
            resume_reason=_optional_str(payload.get("resume_reason")),
            retry_count=int(payload.get("retry_count", 0)),
        )

    def _record_policy_decision(
        self,
        *,
        workspace_id: str,
        decision: PolicyDecision,
        reason_code: PolicyDecisionReason,
        capability_id: str,
        automation_id: str | None = None,
        agent_id: str | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
        policy_ids: list[str] | None = None,
        approval_ids: list[str] | None = None,
    ) -> PolicyDecisionRecord:
        now = _now()
        audit_event_id = f"control-plane-event-{uuid4()}"
        record = PolicyDecisionRecord(
            id=f"policy-decision-{uuid4()}",
            workspace_id=workspace_id,
            decision=decision,
            reason_code=reason_code,
            capability_id=capability_id,
            automation_id=automation_id,
            agent_id=agent_id,
            actor_id=actor_id,
            actor_role=actor_role,
            policy_ids=policy_ids or [],
            approval_ids=approval_ids or [],
            audit_event_id=audit_event_id,
            created_at=now,
        )
        self.db.execute(
            """
            INSERT INTO control_plane_events (
                id, workspace_id, event_type, resource_type, resource_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_event_id,
                workspace_id,
                f"policy.decision.{decision.value}",
                "capability",
                capability_id,
                json.dumps(_to_jsonable(asdict(record)), sort_keys=True),
                now,
            ),
        )
        return record


def _manifest_to_payload(manifest: ControlPlaneManifest) -> dict[str, Any]:
    payload: dict[str, Any] = _to_jsonable(asdict(manifest))
    payload.pop("contract_version", None)
    return payload


def _resource_lifecycle(payload: dict[str, Any]) -> str:
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, str):
        return lifecycle
    default_lifecycle: str = LifecycleState.CANDIDATE.value
    return default_lifecycle


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "major") and hasattr(value, "minor"):
        return str(value)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _due_schedule_occurrence(
    rrule: str | None,
    now_dt: datetime,
    *,
    schedule_start_at: str | None = None,
) -> datetime | None:
    pairs = _parse_rrule_pairs(rrule)
    if not pairs:
        return None
    freq = pairs.get("FREQ")
    if freq not in {
        "SECONDLY",
        "MINUTELY",
        "HOURLY",
        "DAILY",
        "WEEKLY",
        "MONTHLY",
        "YEARLY",
    }:
        return None
    if not _rrule_until_allows(pairs, now_dt):
        return None
    if not _rrule_byday_allows(pairs, now_dt):
        return None

    try:
        interval = int(pairs.get("INTERVAL", "1"))
    except ValueError:
        return None
    if interval <= 0:
        return None
    if schedule_start_at:
        try:
            start_dt = _parse_datetime(schedule_start_at).astimezone(timezone.utc)
        except ValueError:
            return None
        start_dt = start_dt.replace(microsecond=0)
        due_dt = now_dt.replace(microsecond=0)
        if due_dt < start_dt:
            return None
        if not _rrule_start_boundary_matches(freq, start_dt, due_dt, pairs):
            return None
        elapsed = _rrule_elapsed_units_since(freq, start_dt, due_dt, pairs)
    else:
        if not _rrule_boundary_matches(freq, now_dt):
            return None
        elapsed = _rrule_elapsed_units(freq, now_dt)
    if elapsed is None or elapsed % interval != 0:
        return None
    if "COUNT" in pairs:
        try:
            count = int(pairs["COUNT"])
        except ValueError:
            return None
        if count <= 0 or elapsed // interval >= count:
            return None
    return now_dt.replace(microsecond=0)


def _parse_rrule_pairs(rrule: str | None) -> dict[str, str]:
    if not rrule:
        return {}
    text = rrule.strip()
    if text.startswith("RRULE:"):
        text = text.removeprefix("RRULE:")
    pairs: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            return {}
        key, value = part.split("=", 1)
        key = key.strip().upper()
        value = value.strip().upper()
        if not key or not value or key in pairs:
            return {}
        pairs[key] = value
    return pairs


def _rrule_until_allows(pairs: Mapping[str, str], now_dt: datetime) -> bool:
    until = pairs.get("UNTIL")
    if not until:
        return True
    try:
        if "T" in until:
            until_dt = datetime.strptime(
                until.removesuffix("Z"), "%Y%m%dT%H%M%S"
            ).replace(tzinfo=timezone.utc)
        else:
            until_dt = datetime.strptime(until, "%Y%m%d").replace(
                tzinfo=timezone.utc
            )
    except ValueError:
        return False
    return now_dt <= until_dt


def _rrule_byday_allows(pairs: Mapping[str, str], now_dt: datetime) -> bool:
    byday = pairs.get("BYDAY")
    if not byday:
        return True
    days = {part.strip().upper() for part in byday.split(",") if part.strip()}
    weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[now_dt.weekday()]
    return weekday in days


def _rrule_boundary_matches(freq: str, now_dt: datetime) -> bool:
    if freq == "SECONDLY":
        return True
    if now_dt.second != 0:
        return False
    if freq == "MINUTELY":
        return True
    if now_dt.minute != 0:
        return False
    if freq == "HOURLY":
        return True
    if now_dt.hour != 0:
        return False
    if freq in {"DAILY", "WEEKLY"}:
        return True
    if now_dt.day != 1:
        return False
    if freq == "MONTHLY":
        return True
    return freq == "YEARLY" and now_dt.month == 1


def _rrule_start_boundary_matches(
    freq: str,
    start_dt: datetime,
    due_dt: datetime,
    pairs: Mapping[str, str],
) -> bool:
    if freq == "SECONDLY":
        return True
    if freq == "MINUTELY":
        return due_dt.second == start_dt.second
    if due_dt.minute != start_dt.minute or due_dt.second != start_dt.second:
        return False
    if freq == "HOURLY":
        return True
    if due_dt.hour != start_dt.hour:
        return False
    if freq == "DAILY":
        return True
    if freq == "WEEKLY":
        if "BYDAY" in pairs:
            return True
        return due_dt.weekday() == start_dt.weekday()
    if due_dt.day != start_dt.day:
        return False
    if freq == "MONTHLY":
        return True
    return freq == "YEARLY" and due_dt.month == start_dt.month


def _rrule_elapsed_units_since(
    freq: str,
    start_dt: datetime,
    due_dt: datetime,
    pairs: Mapping[str, str],
) -> int | None:
    delta = due_dt - start_dt
    if delta.total_seconds() < 0:
        return None
    if freq == "SECONDLY":
        return int(delta.total_seconds())
    if freq == "MINUTELY":
        return int(delta.total_seconds() // 60)
    if freq == "HOURLY":
        return int(delta.total_seconds() // 3600)
    if freq == "DAILY":
        return delta.days
    if freq == "WEEKLY":
        if "BYDAY" in pairs:
            start_week = start_dt.date() - timedelta(days=start_dt.weekday())
            due_week = due_dt.date() - timedelta(days=due_dt.weekday())
            return (due_week - start_week).days // 7
        return delta.days // 7
    if freq == "MONTHLY":
        return (due_dt.year - start_dt.year) * 12 + (
            due_dt.month - start_dt.month
        )
    if freq == "YEARLY":
        return due_dt.year - start_dt.year
    return None


def _rrule_elapsed_units(freq: str, now_dt: datetime) -> int | None:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = now_dt - epoch
    if delta.total_seconds() < 0:
        return None
    if freq == "SECONDLY":
        return int(delta.total_seconds())
    if freq == "MINUTELY":
        return int(delta.total_seconds() // 60)
    if freq == "HOURLY":
        return int(delta.total_seconds() // 3600)
    if freq == "DAILY":
        return delta.days
    if freq == "WEEKLY":
        return delta.days // 7
    if freq == "MONTHLY":
        return (now_dt.year - 1970) * 12 + (now_dt.month - 1)
    if freq == "YEARLY":
        return now_dt.year - 1970
    return None


def _safe_schedule_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


def _new_trace_id() -> str:
    return f"trace-{uuid4().hex}"


def _new_span_id() -> str:
    return f"span-{uuid4().hex[:16]}"


def _retention_expires_at(created_at: str, retention_days: int) -> str:
    retention_delta = max(retention_days, 1)
    parsed = _parse_datetime(created_at)
    return (parsed + timedelta(days=retention_delta)).isoformat().replace("+00:00", "Z")


def _retry_backoff_delay_seconds(
    retry_count: int, base_delay_seconds: int, max_delay_seconds: int
) -> int:
    """Deterministic exponential backoff for a 1-based retry count.

    Retry 1 returns ``base_delay_seconds``; each later retry doubles the prior
    delay until it reaches ``max_delay_seconds``, where it stays capped.
    """

    exponent = max(retry_count, 1) - 1
    # Cap the exponent so the doubling cannot overflow into an enormous int
    # before the min() clamp; once base * 2**exponent >= max we are capped.
    capped_exponent = min(exponent, max_delay_seconds.bit_length() + 1)
    delay: int = base_delay_seconds * (2**capped_exponent)
    return min(delay, max_delay_seconds)


def _future_iso_timestamp(now: str, delay_seconds: int) -> str:
    parsed = _parse_datetime(now)
    return (
        (parsed + timedelta(seconds=max(delay_seconds, 0)))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _redact_observability_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_observability_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact_observability_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_observability_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_observability_text(value)
    return value


def _redact_observability_text(value: str) -> str:
    redacted = value
    for marker in ("secret://", "token=", "password=", "api_key=", "apikey="):
        index = redacted.lower().find(marker)
        if index >= 0:
            redacted = redacted[:index] + "[REDACTED]"
    return redacted


def _redact_approval_metadata_text(value: str) -> str:
    """Redact free-form local approval assignment/escalation text."""

    redacted = _redact_observability_text(value)
    lowered = redacted.lower()
    for marker in FORBIDDEN_EXECUTOR_METADATA_MARKERS:
        if marker in lowered:
            return "[REDACTED]"
    return redacted


def _redact_optional_metadata_text(value: str | None) -> str | None:
    """Redact free-form local notification text, preserving an empty/None value."""

    if value is None or value == "":
        return None
    return _redact_approval_metadata_text(value)


def _coerce_notification_event(
    value: LocalApprovalNotificationEvent | str,
) -> LocalApprovalNotificationEvent:
    if isinstance(value, LocalApprovalNotificationEvent):
        return value
    try:
        return LocalApprovalNotificationEvent(value)
    except ValueError as error:
        raise ControlPlaneRegistryError(
            f"invalid notification event_type {value!r}"
        ) from error


def _coerce_notification_channel(
    value: LocalApprovalNotificationChannel | str,
) -> LocalApprovalNotificationChannel:
    if isinstance(value, LocalApprovalNotificationChannel):
        return value
    try:
        return LocalApprovalNotificationChannel(value)
    except ValueError as error:
        raise ControlPlaneRegistryError(
            f"invalid notification channel {value!r}"
        ) from error


def _coerce_notification_status(
    value: LocalApprovalNotificationStatus | str,
) -> LocalApprovalNotificationStatus:
    if isinstance(value, LocalApprovalNotificationStatus):
        return value
    try:
        return LocalApprovalNotificationStatus(value)
    except ValueError as error:
        raise ControlPlaneRegistryError(
            f"invalid notification status {value!r}"
        ) from error


def _is_sensitive_observability_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_OBSERVABILITY_KEYS)


# Closed allowlist of event-payload keys the redacted OTel projection copies
# into a span's attributes. Every value behind these keys is already a
# display-safe scalar in the underlying event dataclasses/payloads (see
# RunStepRecord, LocalModelInvocationRecord, and the capability/approval
# invocation payloads above); anything not named here is dropped rather than
# copied, so unlisted payload keys (secret refs, executor metadata, schema
# summaries, output summaries, ...) never reach the projection.
_OTEL_PROJECTION_ATTRIBUTE_KEYS = (
    "run_id",
    "step_id",
    "automation_id",
    "agent_id",
    "capability_id",
    "policy_decision_id",
    "policy_audit_event_id",
    "mode",
    "simulated",
    "executor_backed",
    "attempt",
    "retry_count",
    "token_usage",
    "cost_units",
    "model_provider",
    "model_name",
    "invocation_type",
    "prompt_redacted",
    "output_redacted",
)

_OTEL_SPAN_OK_STATUSES = frozenset({"completed", "succeeded", "simulated"})
_OTEL_SPAN_ERROR_STATUSES = frozenset(
    {"failed", "cancelled", "rejected", "timed_out", "blocked", "dead_letter"}
)
_OTEL_SPAN_WAITING_STATUSES = frozenset({"waiting_for_approval", "approval_required"})
_OTEL_PROJECTION_FORBIDDEN_TEXT_MARKERS = (
    "client_handle",
    "model_output",
    "output=",
    "output:",
    "prompt=",
    "prompt:",
    "raw_response",
    "response_body",
)


def _observability_span_status(event_type: str, payload: Mapping[str, Any]) -> str:
    """Derive a closed OTel-style span status from a display-safe event.

    Prefers the event's own ``status`` field (run/run-step evidence); falls
    back to the event type's final segment (e.g. ``capability.invocation.
    simulated``) for events that carry no ``status`` key. Unrecognized values
    map to ``"unset"`` rather than guessing.
    """

    status = payload.get("status")
    candidate = status if isinstance(status, str) and status else event_type.rsplit(".", 1)[-1]
    if candidate in _OTEL_SPAN_OK_STATUSES:
        return "ok"
    if candidate in _OTEL_SPAN_ERROR_STATUSES:
        return "error"
    if candidate in _OTEL_SPAN_WAITING_STATUSES:
        return "waiting"
    if candidate == "started" and "wait" in event_type:
        return "waiting"
    return "unset"


def _observability_span_attributes(
    audit_event_id: str,
    event_type: str,
    resource_type: Any,
    resource_id: Any,
    created_at: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a closed, display-safe attribute set for one projected span."""

    attributes: dict[str, Any] = {
        "audit_event_id": _redact_observability_text(audit_event_id),
        "event_type": _redact_observability_text(event_type),
        "created_at": _redact_observability_text(created_at),
    }
    if resource_type is not None:
        attributes["resource_type"] = _redact_observability_text(str(resource_type))
    if resource_id is not None:
        attributes["resource_id"] = _redact_observability_text(str(resource_id))
    for key in _OTEL_PROJECTION_ATTRIBUTE_KEYS:
        value = payload.get(key)
        if value is not None:
            attributes[key] = _redact_otel_projection_value(value)
    return attributes


def _redact_otel_projection_value(value: Any) -> Any:
    """Redact scalar values that are safe by schema but hostile if misused."""

    redacted = _redact_observability_payload(value)
    if isinstance(redacted, str) and any(
        marker in redacted.lower()
        for marker in _OTEL_PROJECTION_FORBIDDEN_TEXT_MARKERS
    ):
        return "[REDACTED]"
    return redacted


def _event_from_dict(data: dict[str, Any]) -> TriggerEventEnvelope:
    return TriggerEventEnvelope(
        id=str(data.get("id", "")),
        trigger_id=str(data.get("trigger_id", "")),
        event_type=str(data.get("event_type", "")),
        source=str(data.get("source", "")),
        subject=_optional_str(data.get("subject")),
        occurred_at=_optional_str(data.get("occurred_at")),
        idempotency_key=_optional_str(data.get("idempotency_key")),
        actor_id=_optional_str(data.get("actor_id")),
        data_ref=_optional_str(data.get("data_ref")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _find_resource(resources: list[_ResourceT], resource_id: str) -> _ResourceT | None:
    for resource in resources:
        if getattr(resource, "id", None) == resource_id:
            return resource
    return None


def _append_unique_secret_ref(
    secret_refs: list[SecretReference], secret_reference: SecretReference
) -> list[SecretReference]:
    if any(item.secret_ref == secret_reference.secret_ref for item in secret_refs):
        return list(secret_refs)
    return [*secret_refs, secret_reference]


def _resource_id_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", ".", value).strip(".") or "resource"


def _policy_from_template(
    template: PolicyTemplate,
    policy_id: str,
    capability_ids: list[str] | None,
    automation_ids: list[str] | None,
) -> Policy:
    """Materialize a Policy from a template, binding capability/automation targets."""

    return Policy(
        id=policy_id,
        decision=template.decision,
        capability_ids=list(capability_ids or []),
        automation_ids=list(automation_ids or []),
        allowed_actor_roles=list(template.allowed_actor_roles),
        required_actor_attributes=dict(template.required_actor_attributes),
        required_workspace_attributes=dict(template.required_workspace_attributes),
        attribute_conditions=[
            replace(condition, values=list(condition.values))
            for condition in template.attribute_conditions
        ],
        attribute_expression=_clone_attribute_expression(
            template.attribute_expression
        ),
        max_cost_units=template.max_cost_units,
        max_token_usage=template.max_token_usage,
        timeout_seconds=template.timeout_seconds,
        retention_days=template.retention_days,
        safety_classification=template.safety_classification,
        compliance_blocked=template.compliance_blocked,
        reason=template.reason,
    )


def _clone_attribute_expression(
    expression: PolicyAttributeExpression | None,
) -> PolicyAttributeExpression | None:
    """Deep-copy a bounded attribute expression tree so lists are not shared."""

    if expression is None:
        return None
    return _clone_expression_node(expression)


def _clone_expression_node(
    expression: PolicyAttributeExpression,
) -> PolicyAttributeExpression:
    condition = expression.condition
    return PolicyAttributeExpression(
        op=expression.op,
        children=[_clone_expression_node(child) for child in expression.children],
        condition=(
            replace(condition, values=list(condition.values))
            if condition is not None
            else None
        ),
    )


def _contains_raw_secret_material(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "api_key=",
            "apikey=",
            "authorization:",
            "bearer ",
            "client_secret=",
            "password=",
            "private_key=",
            "token=",
        )
    )


def _validate_candidate_promotion_ready(
    manifest: ControlPlaneManifest, resource_type: str, resource_id: str
) -> None:
    if resource_type == "connection":
        connection = _find_resource(manifest.connections, resource_id)
        if connection is None:
            raise ControlPlaneRegistryError(f"connection {resource_id} does not exist")
        if not connection.secret_refs:
            raise ControlPlaneRegistryError(
                f"connection {resource_id} requires a workspace secret_ref before promotion"
            )
        return

    if resource_type == "capability":
        capability = _find_resource(manifest.capabilities, resource_id)
        if capability is None:
            raise ControlPlaneRegistryError(f"capability {resource_id} does not exist")
        connection = _find_resource(manifest.connections, capability.connection_id)
        if connection is None:
            raise ControlPlaneRegistryError(
                f"capability {resource_id} references unknown connection {capability.connection_id}"
            )
        if connection.lifecycle != LifecycleState.ACTIVE:
            raise ControlPlaneRegistryError(
                f"capability {resource_id} requires active connection {capability.connection_id}"
            )
        if _capability_requires_policy_coverage(capability) and not _has_policy_coverage(
            manifest, capability.id
        ):
            raise ControlPlaneRegistryError(
                f"capability {resource_id} requires policy coverage before promotion"
            )
        return

    if resource_type == "trigger":
        trigger = _find_resource(manifest.triggers, resource_id)
        if trigger is None:
            raise ControlPlaneRegistryError(f"trigger {resource_id} does not exist")
        capability = _find_resource(manifest.capabilities, trigger.capability_id)
        if capability is None:
            raise ControlPlaneRegistryError(
                f"trigger {resource_id} references unknown capability {trigger.capability_id}"
            )
        if capability.lifecycle != LifecycleState.ACTIVE:
            raise ControlPlaneRegistryError(
                f"trigger {resource_id} requires active capability {trigger.capability_id}"
            )


def _capability_requires_policy_coverage(capability: Capability) -> bool:
    return capability.side_effect.value in {
        "external_write",
        "send",
        "publish",
        *DANGEROUS_SIDE_EFFECTS,
    }


def _has_policy_coverage(manifest: ControlPlaneManifest, capability_id: str) -> bool:
    return any(
        capability_id in policy.capability_ids
        and policy.decision
        in {
            PolicyDecision.ALLOW,
            PolicyDecision.REQUIRE_APPROVAL,
        }
        for policy in manifest.policies
    )


def _find_in_flight_automation_run(
    manifest: ControlPlaneManifest, automation_id: str
) -> str | None:
    for run_record in manifest.run_records:
        if (
            run_record.automation_id == automation_id
            and run_record.status in IN_FLIGHT_RUN_STATUSES
        ):
            run_id: str = run_record.id
            return run_id
    return None


def _materialize_resume_metadata(
    run_record: RunRecord, updated_at: str, *, reason: str
) -> RunRecord:
    payload = _materialize_resume_payload(
        _to_jsonable(asdict(run_record)),
        updated_at,
        reason=reason,
    )
    return RunRecord(
        id=str(payload["id"]),
        automation_id=str(payload["automation_id"]),
        status=ControlPlaneRunStatus(payload["status"]),
        run_ledger_ref=_optional_str(payload.get("run_ledger_ref")),
        run_ledger_entry_id=_optional_str(payload.get("run_ledger_entry_id")),
        trace_id=_optional_str(payload.get("trace_id")),
        retention_expires_at=_optional_str(payload.get("retention_expires_at")),
        started_at=_optional_str(payload.get("started_at")),
        updated_at=_optional_str(payload.get("updated_at")),
        resume_token=_optional_str(payload.get("resume_token")),
        resume_after=_optional_str(payload.get("resume_after")),
        resume_reason=_optional_str(payload.get("resume_reason")),
        retry_count=int(payload.get("retry_count", 0)),
    )


def _materialize_resume_payload(
    payload: dict[str, Any], updated_at: str, *, reason: str
) -> dict[str, Any]:
    status = ControlPlaneRunStatus(payload["status"])
    if status in TERMINAL_RUN_STATUSES:
        payload["resume_token"] = None
        payload["resume_after"] = None
        payload["resume_reason"] = None
        return payload
    if status in IN_FLIGHT_RUN_STATUSES:
        payload["resume_token"] = payload.get("resume_token") or _resume_token(
            str(payload["id"])
        )
        payload["resume_after"] = payload.get("resume_after") or updated_at
        payload["resume_reason"] = reason
    return payload


def _run_payload_due(payload: Mapping[str, Any], now_ts: str) -> bool:
    """Return whether a stored queued run payload is due relative to ``now_ts``.

    Mirrors the per-run due check in ``list_due_runs`` for a single stored row:
    a missing/empty ``resume_after`` is immediately due, an elapsed timestamp is
    due, and a malformed timestamp fails closed (not due). ``now_ts`` is assumed
    already validated by the caller.
    """

    resume_after = payload.get("resume_after")
    if not resume_after:
        return True
    try:
        resume_dt = _parse_datetime(str(resume_after))
    except ValueError:
        return False
    return resume_dt <= _parse_datetime(now_ts)


def _resume_token(run_id: str) -> str:
    return f"resume.{uuid4().hex}.{run_id.replace(':', '.').replace('/', '.')}"


def _output_payload_summary(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a display-safe output payload as shape only (no values)."""

    if not isinstance(payload, Mapping):
        return {"declared": False}
    return {
        "declared": True,
        "field_count": len(payload),
        "property_names": sorted(str(key) for key in payload),
    }


def _sanitize_executor_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip forbidden executor diagnostic keys, then redact remaining values."""

    if not isinstance(metadata, Mapping):
        return {}
    dropped = _drop_forbidden_executor_keys(metadata)
    sanitized: dict[str, Any] = _redact_observability_payload(dropped)
    return sanitized


def _drop_forbidden_executor_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _is_forbidden_executor_key(str(key)):
                continue
            cleaned[str(key)] = _drop_forbidden_executor_keys(item)
        return cleaned
    if isinstance(value, list):
        return [_drop_forbidden_executor_keys(item) for item in value]
    return value


def _is_forbidden_executor_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in FORBIDDEN_EXECUTOR_METADATA_MARKERS)


def _schema_summary(schema: Mapping[str, Any]) -> dict[str, Any]:
    if not schema:
        return {"declared": False}
    properties = schema.get("properties")
    required = schema.get("required")
    return {
        "declared": True,
        "type": schema.get("type"),
        "property_names": sorted(properties) if isinstance(properties, Mapping) else [],
        "required": list(required) if isinstance(required, list) else [],
        "additional_properties": schema.get("additionalProperties"),
    }


def _validate_payload_against_schema(
    schema: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    *,
    field_name: str,
) -> list[str]:
    if not schema or payload is None:
        return []
    return _validate_value_against_schema(schema, payload, field_name)


def _validate_value_against_schema(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _payload_type_matches(value, expected_type):
        if expected_type == "object":
            return [f"{path} must be an object"]
        return [f"{path} must be {_schema_type_label(expected_type)}"]

    allowed_values = schema.get("enum")
    if isinstance(allowed_values, list) and value not in allowed_values:
        options = ", ".join(str(item) for item in allowed_values)
        errors.append(f"{path} must be one of: {options}")

    if "const" in schema and value != schema.get("const"):
        errors.append(f"{path} must equal {schema.get('const')}")

    if isinstance(value, str):
        errors.extend(_validate_string_keywords(schema, value, path))
    if isinstance(value, int | float) and not isinstance(value, bool):
        errors.extend(_validate_number_keywords(schema, value, path))
    if isinstance(value, list):
        errors.extend(_validate_array_keywords(schema, value, path))
    if isinstance(value, Mapping):
        errors.extend(_validate_object_keywords(schema, value, path))
    return errors


def _validate_object_keywords(
    schema: Mapping[str, Any],
    payload: Mapping[str, Any],
    field_name: str,
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in payload:
                errors.append(f"{field_name}.{key} is required")
    if isinstance(properties, Mapping):
        additional_properties = schema.get("additionalProperties")
        if additional_properties is False:
            for key in payload:
                if key not in properties:
                    errors.append(f"{field_name}.{key} is not allowed")
        for key, property_schema in properties.items():
            if key not in payload or not isinstance(property_schema, Mapping):
                continue
            errors.extend(
                _validate_value_against_schema(
                    property_schema,
                    payload[key],
                    f"{field_name}.{key}",
                )
            )
    elif schema.get("additionalProperties") is False and payload:
        errors.append(f"{field_name} does not allow properties")
    return errors


def _validate_string_keywords(
    schema: Mapping[str, Any],
    value: str,
    path: str,
) -> list[str]:
    errors: list[str] = []
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(value) < min_length:
        errors.append(f"{path} must be at least {min_length} characters")
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(value) > max_length:
        errors.append(f"{path} must be at most {max_length} characters")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, value) is None:
                errors.append(f"{path} must match pattern {pattern}")
        except re.error:
            errors.append(f"{path} schema pattern is invalid")
    return errors


def _validate_number_keywords(
    schema: Mapping[str, Any],
    value: int | float,
    path: str,
) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, int | float) and value < minimum:
        errors.append(f"{path} must be >= {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, int | float) and value > maximum:
        errors.append(f"{path} must be <= {maximum}")
    exclusive_minimum = schema.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, int | float) and value <= exclusive_minimum:
        errors.append(f"{path} must be > {exclusive_minimum}")
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, int | float) and value >= exclusive_maximum:
        errors.append(f"{path} must be < {exclusive_maximum}")
    multiple_of = schema.get("multipleOf")
    if isinstance(multiple_of, int | float) and multiple_of != 0:
        quotient = value / multiple_of
        if abs(quotient - round(quotient)) > 1e-9:
            errors.append(f"{path} must be a multiple of {multiple_of}")
    return errors


def _validate_array_keywords(
    schema: Mapping[str, Any],
    value: list[Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        errors.append(f"{path} must contain at least {min_items} items")
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        errors.append(f"{path} must contain at most {max_items} items")
    if schema.get("uniqueItems") is True:
        seen: set[str] = set()
        for item in value:
            marker = json.dumps(_to_jsonable(item), sort_keys=True)
            if marker in seen:
                errors.append(f"{path} must contain unique items")
                break
            seen.add(marker)
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            errors.extend(_validate_value_against_schema(item_schema, item, f"{path}[{index}]"))
    return errors


def _payload_type_matches(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_payload_type_matches(value, item) for item in expected_type)
    if not isinstance(expected_type, str):
        return True
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, Mapping)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    return True


def _schema_type_label(expected_type: Any) -> str:
    if isinstance(expected_type, list):
        return " or ".join(str(item) for item in expected_type)
    return str(expected_type)


def _enforce_execution_limits(automation: Automation, *, max_steps: int) -> None:
    if automation.max_steps is not None and max_steps > automation.max_steps:
        raise ControlPlaneRegistryError(
            f"automation {automation.id} max_steps limit exceeded"
        )
    for field_name, value in (
        ("max_cost_units", automation.max_cost_units),
        ("max_token_usage", automation.max_token_usage),
    ):
        if value is not None and value < 0:
            raise ControlPlaneRegistryError(
                f"automation {automation.id} {field_name} limit is invalid"
            )


def _execution_limit_payload(automation: Automation) -> dict[str, int | None]:
    return {
        "max_steps": automation.max_steps,
        "max_cost_units": automation.max_cost_units,
        "max_token_usage": automation.max_token_usage,
    }


def _effective_timeout_seconds(
    policies: list[Policy],
    capability_id: str,
    automation_id: str,
) -> int | None:
    values = [
        policy.timeout_seconds
        for policy in _matching_policies(policies, capability_id, automation_id)
        if policy.timeout_seconds is not None
    ]
    return min(values) if values else None


def _effective_retention_days(
    policies: list[Policy],
    capability_id: str,
    automation_id: str,
) -> int | None:
    values = [
        policy.retention_days
        for policy in _matching_policies(policies, capability_id, automation_id)
        if policy.retention_days is not None
    ]
    return min(values) if values else None


def _run_timed_out(run_record: RunRecord, timeout_seconds: int) -> bool:
    if timeout_seconds <= 0:
        return True
    timestamp = run_record.started_at or run_record.updated_at
    if not timestamp:
        return False
    try:
        started_at = _parse_datetime(timestamp)
    except ValueError:
        return False
    return _parse_datetime(_now()) - started_at > timedelta(seconds=timeout_seconds)


def _approval_wait_expired(
    wait_start: str | None, timeout_seconds: int, now_dt: datetime
) -> bool:
    """Return whether an approval wait has elapsed relative to ``now_dt``.

    Mirrors :func:`_run_timed_out` but takes a caller-supplied ``now`` so the
    timeout scheduler is deterministic: a non-positive timeout expires
    immediately, while a missing or malformed wait-start fails closed (treated
    as not expired).
    """

    if timeout_seconds <= 0:
        return True
    if not wait_start:
        return False
    try:
        start_dt = _parse_datetime(wait_start)
    except ValueError:
        return False
    return now_dt - start_dt > timedelta(seconds=timeout_seconds)


def _approval_expiry(wait_start: str | None, timeout_seconds: int) -> str | None:
    """Return the ISO-8601 approval expiry instant when it can be computed."""

    if not wait_start or timeout_seconds <= 0:
        return None
    try:
        start_dt = _parse_datetime(wait_start)
    except ValueError:
        return None
    return _format_datetime(start_dt + timedelta(seconds=timeout_seconds))


def _format_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


_TERMINAL_RUN_LEDGER_STATUSES = frozenset(
    {
        RunLedgerStatus.SUCCEEDED,
        RunLedgerStatus.FAILED,
        RunLedgerStatus.BLOCKED,
        RunLedgerStatus.CANCELLED,
    }
)


def _run_ledger_entry_id(source_id: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "."
        for character in source_id
    ).strip(".")
    return f"run-ledger.{safe or uuid4().hex}"


def _run_ledger_status(status: ControlPlaneRunStatus) -> RunLedgerStatus:
    if status in {
        ControlPlaneRunStatus.PENDING,
        ControlPlaneRunStatus.QUEUED,
        ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
        ControlPlaneRunStatus.APPROVAL_REQUIRED,
    }:
        return RunLedgerStatus.QUEUED
    if status == ControlPlaneRunStatus.RUNNING:
        return RunLedgerStatus.RUNNING
    if status in {ControlPlaneRunStatus.COMPLETED, ControlPlaneRunStatus.SUCCEEDED}:
        return RunLedgerStatus.SUCCEEDED
    if status in {ControlPlaneRunStatus.CANCELLED, ControlPlaneRunStatus.REJECTED}:
        return RunLedgerStatus.CANCELLED
    if status == ControlPlaneRunStatus.BLOCKED:
        return RunLedgerStatus.BLOCKED
    return RunLedgerStatus.FAILED


def _attributes_satisfied(
    required: Mapping[str, str],
    provided: Mapping[str, str] | None,
) -> bool:
    """Return True when every required attribute is present and matches.

    Missing required keys or mismatched values fail closed. Raw attribute
    values are only compared here and never recorded in audit payloads.
    """

    if not required:
        return True
    if not provided:
        return False
    return all(provided.get(key) == value for key, value in required.items())


def _attribute_conditions_satisfied(
    conditions: list[PolicyAttributeCondition],
    actor_attributes: Mapping[str, str] | None,
    workspace_attributes: Mapping[str, str] | None,
) -> bool:
    """Return True when every structured attribute condition holds.

    Each condition reads only the actor or workspace attribute map named by its
    scope. Raw attribute values are compared here and never recorded in audit
    payloads.
    """

    for condition in conditions:
        provided = (
            actor_attributes
            if condition.scope == "actor"
            else workspace_attributes
        ) or {}
        if not _attribute_condition_satisfied(condition, provided):
            return False
    return True


def _attribute_condition_satisfied(
    condition: PolicyAttributeCondition,
    provided: Mapping[str, str],
) -> bool:
    present = condition.key in provided
    operator = condition.operator
    if operator == "equals":
        return present and provided[condition.key] == condition.value
    if operator == "not_equals":
        return present and provided[condition.key] != condition.value
    if operator == "in":
        return present and provided[condition.key] in condition.values
    if operator == "not_in":
        return not present or provided[condition.key] not in condition.values
    if operator == "exists":
        return present
    if operator == "not_exists":
        return not present
    comparator = _NUMERIC_CONDITION_COMPARATORS.get(operator)
    if comparator is not None:
        if not present:
            return False
        left = _parse_finite_number(provided[condition.key])
        right = _parse_finite_number(condition.value)
        if left is None or right is None:
            return False
        return comparator(left, right)
    return False


def _attribute_expression_satisfied(
    expression: PolicyAttributeExpression | None,
    actor_attributes: Mapping[str, str] | None,
    workspace_attributes: Mapping[str, str] | None,
) -> bool:
    """Evaluate a bounded structured attribute expression tree, failing closed.

    The tree is a bounded boolean combinator over bounded condition leaves, not
    CEL or a free-form expression language. ``all``/``any`` combine child nodes,
    ``not`` negates its single child, and a ``condition`` leaf reads only the
    actor or workspace attribute map named by its scope. An unknown op or a
    malformed node fails closed. Raw attribute values and the expression text are
    only compared here and never recorded in audit payloads.
    """

    if expression is None:
        return True
    op = expression.op
    if op == "condition":
        condition = expression.condition
        if condition is None or expression.children:
            return False
        provided = (
            actor_attributes
            if condition.scope == "actor"
            else workspace_attributes
        ) or {}
        return _attribute_condition_satisfied(condition, provided)
    if expression.condition is not None:
        return False
    if op == "all":
        return bool(expression.children) and all(
            _attribute_expression_satisfied(
                child, actor_attributes, workspace_attributes
            )
            for child in expression.children
        )
    if op == "any":
        return any(
            _attribute_expression_satisfied(
                child, actor_attributes, workspace_attributes
            )
            for child in expression.children
        )
    if op == "not":
        if len(expression.children) != 1:
            return False
        return not _attribute_expression_satisfied(
            expression.children[0], actor_attributes, workspace_attributes
        )
    return False


# Deterministic numeric comparison operators. Both the provided attribute value
# and condition.value are parsed as finite numbers; a missing key or a
# non-numeric/non-finite value on either side fails closed (the condition is not
# satisfied). This is bounded comparison, not an expression language.
_NUMERIC_CONDITION_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "greater_than": lambda left, right: left > right,
    "greater_than_or_equal": lambda left, right: left >= right,
    "less_than": lambda left, right: left < right,
    "less_than_or_equal": lambda left, right: left <= right,
}


def _parse_finite_number(value: str | None) -> float | None:
    """Parse a finite number from a string, or None when not a finite number."""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _matching_policies(
    policies: list[Policy],
    capability_id: str,
    automation_id: str | None,
) -> list[Policy]:
    return [
        policy
        for policy in policies
        if capability_id in policy.capability_ids
        or (automation_id is not None and automation_id in policy.automation_ids)
    ]


def _matching_policy_ids(
    policies: list[Policy],
    capability_id: str,
    automation_id: str | None,
) -> list[str]:
    return [
        policy.id
        for policy in _matching_policies(policies, capability_id, automation_id)
    ]


def _matching_approval_ids(
    approvals: list[Approval],
    capability_id: str,
    automation_id: str | None,
    *,
    approved_only: bool,
) -> list[str]:
    return [
        approval.id
        for approval in approvals
        if (approval.approved or not approved_only)
        and (
            capability_id in approval.capability_ids
            or (automation_id is not None and automation_id in approval.automation_ids)
        )
    ]
