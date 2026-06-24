from copy import deepcopy
from dataclasses import asdict

from omnivia_memory.control_plane import (
    CONTROL_PLANE_SCHEMA_VERSION,
    manifest_from_dict,
    validate_control_plane_manifest,
)


def _enriched_approval() -> dict[str, object]:
    return {
        "id": "approval.sales.manager",
        "capability_ids": ["capability.hubspot.update.contact"],
        "automation_ids": ["automation.sync.contact"],
        "approved": True,
        "approver_role": "sales-manager",
        "actor_id": "user.alice",
        "comment": "Approved after manual review.",
        "timeout_seconds": 3600,
        "escalation_state": "resolved",
        "run_id": "run.control-plane.001",
        "resource_type": "capability",
        "resource_id": "capability.hubspot.update.contact",
        "requested_at": "2026-06-21T00:00:00Z",
        "decided_at": "2026-06-21T00:30:00Z",
        "expires_at": "2026-06-21T01:00:00Z",
    }


def _valid_manifest() -> dict[str, object]:
    return {
        "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
        "workspace": {"id": "workspace.demo", "name": "Demo"},
        "connections": [
            {
                "id": "connection.hubspot",
                "kind": "app",
                "display_name": "HubSpot",
                "lifecycle": "active",
                "secret_refs": [
                    {
                        "secret_ref": "secret://workspace.demo/hubspot/oauth",
                        "provider": "local-keychain",
                    }
                ],
                "catalogue_entry_id": "app-hubspot",
            }
        ],
        "import_records": [
            {
                "id": "import.catalogue.hubspot",
                "source_protocol": "catalogue",
                "source_ref": "omnivia-catalog:catalog/connections/custom-app/app-hubspot.yaml",
                "candidate_capability_ids": ["capability.hubspot.read.contacts"],
                "lifecycle": "candidate",
            }
        ],
        "capabilities": [
            {
                "id": "capability.hubspot.read.contacts",
                "capability_type": "query",
                "connection_id": "connection.hubspot",
                "side_effect": "read",
                "lifecycle": "candidate",
                "import_record_id": "import.catalogue.hubspot",
            },
            {
                "id": "capability.hubspot.update.contact",
                "capability_type": "action",
                "connection_id": "connection.hubspot",
                "side_effect": "external_write",
                "lifecycle": "active",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "lifecycle_stage": {"type": "string"},
                    },
                    "required": ["contact_id"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "updated": {"type": "boolean"},
                    },
                    "required": ["updated"],
                    "additionalProperties": False,
                },
            },
        ],
        "agents": [
            {
                "id": "agent.sales.ops",
                "allowed_capabilities": [
                    "capability.hubspot.read.contacts",
                    "capability.hubspot.update.contact",
                ],
                "run_mode": "approval_gated",
            }
        ],
        "triggers": [
            {
                "id": "trigger.contact.updated",
                "kind": "cloudevent",
                "capability_id": "capability.hubspot.read.contacts",
                "event_type": "com.hubspot.contact.updated",
            }
        ],
        "policies": [
            {
                "id": "policy.require.update.approval",
                "decision": "require_approval",
                "capability_ids": ["capability.hubspot.update.contact"],
            }
        ],
        "approvals": [
            {
                "id": "approval.sales.manager",
                "capability_ids": ["capability.hubspot.update.contact"],
                "automation_ids": ["automation.sync.contact"],
                "approved": True,
                "approver_role": "sales-manager",
            }
        ],
        "automations": [
            {
                "id": "automation.sync.contact",
                "agent_id": "agent.sales.ops",
                "capability_id": "capability.hubspot.update.contact",
                "trigger_id": "trigger.contact.updated",
                "policy_ids": ["policy.require.update.approval"],
                "approval_ids": ["approval.sales.manager"],
                "run_mode": "approval_gated",
                "lifecycle": "active",
            }
        ],
        "run_records": [
            {
                "id": "run.control-plane.001",
                "automation_id": "automation.sync.contact",
                "status": "succeeded",
                "run_ledger_ref": "OMNIVIA_RUN_LEDGER_PATH",
                "run_ledger_entry_id": "run-ledger-entry-001",
            }
        ],
        "audit_events": [
            {
                "id": "audit.control-plane.001",
                "event_type": "automation.succeeded",
                "resource_id": "automation.sync.contact",
                "run_id": "run.control-plane.001",
            }
        ],
    }


def test_valid_manifest_covers_all_resource_kinds() -> None:
    result = validate_control_plane_manifest(_valid_manifest())

    assert result.valid
    assert result.errors == []


def test_capability_input_output_schemas_are_parsed() -> None:
    manifest = manifest_from_dict(_valid_manifest())

    capability = manifest.capabilities[1]

    assert capability.input_schema["type"] == "object"
    assert capability.input_schema["required"] == ["contact_id"]
    assert capability.output_schema["properties"]["updated"]["type"] == "boolean"


def test_capability_schema_required_fields_must_exist() -> None:
    manifest = _valid_manifest()
    manifest["capabilities"][1]["input_schema"]["required"] = ["missing_field"]  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "capabilities[capability.hubspot.update.contact].input_schema.required "
        "references unknown properties: missing_field"
    ) in result.errors


def test_capability_schema_must_be_an_object() -> None:
    manifest = _valid_manifest()
    manifest["capabilities"][1]["input_schema"] = "not-a-schema"  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert "capabilities[].input_schema must be an object" in result.errors


def test_automation_execution_limits_must_be_valid() -> None:
    manifest = _valid_manifest()
    manifest["automations"][0]["max_steps"] = 1  # type: ignore[index]
    manifest["automations"][0]["max_cost_units"] = -1  # type: ignore[index]
    manifest["automations"][0]["max_retries"] = -1  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "automations[automation.sync.contact].max_steps must allow agent and "
        "capability steps"
    ) in result.errors
    assert "automations[automation.sync.contact].max_cost_units must be >= 0" in result.errors
    assert "automations[automation.sync.contact].max_retries must be >= 0" in result.errors


def test_policy_timeout_and_retention_values_must_be_valid() -> None:
    manifest = _valid_manifest()
    manifest["policies"][0]["timeout_seconds"] = -1  # type: ignore[index]
    manifest["policies"][0]["retention_days"] = -1  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert "policies[policy.require.update.approval].timeout_seconds must be >= 0" in result.errors
    assert "policies[policy.require.update.approval].retention_days must be >= 0" in result.errors


def test_schedule_trigger_accepts_valid_rrule_and_timing_guards() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"].append(  # type: ignore[union-attr]
        {
            "id": "trigger.weekly.review",
            "kind": "schedule",
            "capability_id": "capability.hubspot.read.contacts",
            "schedule_rrule": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE",
            "schedule_start_at": "2026-06-21T10:15:00Z",
            "cooldown_seconds": 300,
            "debounce_seconds": 60,
        }
    )

    result = validate_control_plane_manifest(manifest)

    assert result.valid, result.errors


def test_schedule_trigger_requires_valid_start_at() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"].append(  # type: ignore[union-attr]
        {
            "id": "trigger.bad.start",
            "kind": "schedule",
            "capability_id": "capability.hubspot.read.contacts",
            "schedule_rrule": "FREQ=DAILY",
            "schedule_start_at": "not-a-date",
        }
    )

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "schedule_start_at must be an ISO timestamp" in error
        for error in result.errors
    )


def test_non_schedule_trigger_rejects_start_at() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"][0]["schedule_start_at"] = "2026-06-21T10:15:00Z"  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("schedule_start_at is only valid" in error for error in result.errors)


def test_schedule_trigger_requires_valid_rrule() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"].append(  # type: ignore[union-attr]
        {
            "id": "trigger.bad.schedule",
            "kind": "schedule",
            "capability_id": "capability.hubspot.read.contacts",
            "schedule_rrule": "INTERVAL=0;BYDAY=NOPE",
        }
    )

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("schedule_rrule must be a valid RRULE" in error for error in result.errors)


def test_schedule_trigger_requires_rrule_field() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"].append(  # type: ignore[union-attr]
        {
            "id": "trigger.missing.schedule",
            "kind": "schedule",
            "capability_id": "capability.hubspot.read.contacts",
        }
    )

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("schedule_rrule is required" in error for error in result.errors)


def test_non_schedule_trigger_rejects_rrule() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"][0]["schedule_rrule"] = "FREQ=DAILY"  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("schedule_rrule is only valid" in error for error in result.errors)


def test_trigger_timing_guards_must_be_non_negative() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["triggers"][0]["cooldown_seconds"] = -1  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("cooldown_seconds must be >= 0" in error for error in result.errors)


def test_manifest_resource_lists_default_empty() -> None:
    result = validate_control_plane_manifest(
        {
            "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
            "workspace": {"id": "workspace.empty"},
        }
    )

    assert result.valid
    assert result.errors == []


def test_phase9_local_vault_sync_and_consultant_resources_validate() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["secret_metadata"] = [
        {
            "id": "secret-meta.hubspot.oauth",
            "secret_ref": "secret://workspace.demo/hubspot/oauth",
            "owner_workspace_id": "workspace.demo",
            "storage_scope": "client_owned",
            "provider": "local-keychain",
            "client_owned": True,
            "syncable": False,
            "lifecycle": "active",
        }
    ]
    manifest["sync_rules"] = [
        {
            "id": "sync.local-only.secrets",
            "resource_type": "secret_metadata",
            "direction": "none",
            "conflict_strategy": "manual_review",
            "audit_required": True,
        },
        {
            "id": "sync.audit.bidirectional",
            "resource_type": "audit_event",
            "direction": "bidirectional",
            "conflict_strategy": "manual_review",
            "audit_required": True,
        },
    ]
    manifest["tenant_isolation_rules"] = [
        {
            "id": "tenant.client.workspace",
            "tenant_id": "tenant.acme",
            "workspace_id": "workspace.demo",
            "client_workspace_id": "workspace.demo",
            "cross_client_sharing_allowed": False,
            "lifecycle": "active",
        }
    ]
    manifest["policies"].append(  # type: ignore[union-attr]
        {
            "id": "policy.consultant.view",
            "decision": "allow",
            "reason": "Consultant can review local evidence only.",
        }
    )
    manifest["consultant_access_grants"] = [
        {
            "id": "consultant-grant.acme.ops",
            "consultant_id": "consultant.jules",
            "client_workspace_id": "workspace.demo",
            "role": "reviewer",
            "policy_ids": ["policy.consultant.view"],
            "status": "active",
            "granted_by": "client.admin",
            "granted_at": "2026-06-21T00:00:00Z",
            "lifecycle": "active",
        }
    ]

    result = validate_control_plane_manifest(manifest)

    assert result.valid
    assert result.errors == []


def test_local_only_secret_metadata_cannot_sync() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["secret_metadata"] = [
        {
            "id": "secret-meta.local",
            "secret_ref": "secret://workspace.demo/local/api",
            "owner_workspace_id": "workspace.demo",
            "storage_scope": "local_only",
            "syncable": True,
        }
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("local_only" in error and "syncable" in error for error in result.errors)


def test_tenant_isolation_rejects_cross_client_sharing() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["tenant_isolation_rules"] = [
        {
            "id": "tenant.bad",
            "tenant_id": "tenant.acme",
            "workspace_id": "workspace.demo",
            "cross_client_sharing_allowed": True,
        }
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("cross-client sharing" in error for error in result.errors)


def test_active_consultant_grant_requires_policy_coverage() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["consultant_access_grants"] = [
        {
            "id": "consultant-grant.no-policy",
            "consultant_id": "consultant.jules",
            "client_workspace_id": "workspace.demo",
            "role": "reviewer",
            "status": "active",
        }
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("active without policy_ids" in error for error in result.errors)


def test_unsupported_schema_version_is_rejected() -> None:
    manifest = _valid_manifest()
    manifest["schema_version"] = "omnivia.control_plane_manifest.v999"

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("schema_version" in error for error in result.errors)


def test_raw_secret_fields_are_rejected() -> None:
    manifest = _valid_manifest()
    manifest["connections"][0]["api_key"] = "not-public"  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("api_key" in error for error in result.errors)


def test_agents_cannot_reference_raw_connections() -> None:
    manifest = _valid_manifest()
    manifest["agents"][0]["allowed_connections"] = ["connection.hubspot"]  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("allowed_connections" in error for error in result.errors)


def test_unknown_capability_references_are_rejected() -> None:
    manifest = _valid_manifest()
    manifest["agents"][0]["allowed_capabilities"] = ["capability.missing"]  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("capability.missing" in error for error in result.errors)


def test_dangerous_active_capability_requires_approval_coverage() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["approvals"] = []
    manifest["policies"] = []
    manifest["automations"][0]["approval_ids"] = []  # type: ignore[index]
    manifest["automations"][0]["policy_ids"] = []  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("approval coverage" in error for error in result.errors)


def test_import_records_remain_candidate_only() -> None:
    manifest = _valid_manifest()
    manifest["import_records"][0]["runtime_active"] = True  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("runtime activation" in error for error in result.errors)


def test_enriched_approval_metadata_parses_and_validates() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["approvals"] = [_enriched_approval()]

    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors

    parsed = manifest_from_dict(manifest)
    approval = parsed.approvals[0]
    assert approval.actor_id == "user.alice"
    assert approval.comment == "Approved after manual review."
    assert approval.timeout_seconds == 3600
    assert approval.escalation_state == "resolved"
    assert approval.run_id == "run.control-plane.001"
    assert approval.resource_type == "capability"
    assert approval.resource_id == "capability.hubspot.update.contact"
    assert approval.requested_at == "2026-06-21T00:00:00Z"
    assert approval.decided_at == "2026-06-21T00:30:00Z"
    assert approval.expires_at == "2026-06-21T01:00:00Z"


def test_legacy_approval_without_metadata_still_validates() -> None:
    manifest = deepcopy(_valid_manifest())

    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors

    approval = manifest_from_dict(manifest).approvals[0]
    assert approval.actor_id is None
    assert approval.comment is None
    assert approval.timeout_seconds is None
    assert approval.escalation_state == ""
    assert approval.resource_type is None
    assert approval.resource_id is None


def test_enriched_approval_round_trips_through_manifest_from_dict() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["approvals"] = [_enriched_approval()]

    parsed = manifest_from_dict(manifest)
    reparsed = manifest_from_dict(asdict(parsed))

    assert reparsed.approvals[0] == parsed.approvals[0]
    assert validate_control_plane_manifest(reparsed).valid


def test_negative_approval_timeout_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    approval = _enriched_approval()
    approval["timeout_seconds"] = -1
    manifest["approvals"] = [approval]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "approvals[approval.sales.manager].timeout_seconds must be >= 0"
        in result.errors
    )


def test_invalid_approval_escalation_state_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    approval = _enriched_approval()
    approval["escalation_state"] = "exploded"
    manifest["approvals"] = [approval]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "escalation_state must be one of" in error for error in result.errors
    )


def test_approval_resource_type_and_id_must_be_paired() -> None:
    manifest = deepcopy(_valid_manifest())
    approval = _enriched_approval()
    approval.pop("resource_id")
    manifest["approvals"] = [approval]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "approvals[approval.sales.manager].resource_type and resource_id must "
        "be set together"
    ) in result.errors


def test_policy_attribute_conditions_round_trip() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "actor", "key": "clearance", "operator": "equals", "value": "high"},
        {
            "scope": "workspace",
            "key": "tier",
            "operator": "in",
            "values": ["gold", "platinum"],
        },
    ]
    manifest["policy_templates"] = [
        {
            "id": "template.gate",
            "decision": "require_approval",
            "attribute_conditions": [
                {"scope": "actor", "key": "team", "operator": "exists"}
            ],
        }
    ]

    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors

    parsed = manifest_from_dict(manifest)
    conditions = parsed.policies[0].attribute_conditions
    assert conditions[0].scope == "actor"
    assert conditions[0].operator == "equals"
    assert conditions[0].value == "high"
    assert conditions[1].operator == "in"
    assert conditions[1].values == ["gold", "platinum"]
    template_conditions = parsed.policy_templates[0].attribute_conditions
    assert template_conditions[0].key == "team"
    assert template_conditions[0].operator == "exists"


def test_policy_attribute_condition_invalid_scope_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "tenant", "key": "clearance", "operator": "exists"}
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "policies[policy.require.update.approval].attribute_conditions[0].scope "
        "must be one of: actor, workspace"
    ) in result.errors


def test_policy_attribute_condition_invalid_operator_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "actor", "key": "clearance", "operator": "matches", "value": "x"}
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "attribute_conditions[0].operator must be one of" in error
        for error in result.errors
    )


def test_policy_attribute_condition_requires_non_empty_key() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "actor", "key": "", "operator": "exists"}
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert (
        "policies[policy.require.update.approval].attribute_conditions[0].key "
        "is required"
    ) in result.errors


def test_policy_attribute_condition_equals_requires_value_without_values() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "actor", "key": "clearance", "operator": "equals", "values": ["x"]}
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_conditions[0]"
    assert f"{prefix}.value is required for equals" in result.errors
    assert f"{prefix}.values must be empty for equals" in result.errors


def test_policy_attribute_condition_in_requires_values_without_value() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {"scope": "actor", "key": "clearance", "operator": "in", "value": "high"}
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_conditions[0]"
    assert f"{prefix}.values must be non-empty for in" in result.errors
    assert f"{prefix}.value must be unset for in" in result.errors


def test_policy_attribute_condition_exists_rejects_value_and_values() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_conditions"] = [  # type: ignore[index]
        {
            "scope": "actor",
            "key": "clearance",
            "operator": "exists",
            "value": "high",
            "values": ["x"],
        }
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_conditions[0]"
    assert f"{prefix}.value must be unset for exists" in result.errors
    assert f"{prefix}.values must be empty for exists" in result.errors


def _nested_expression() -> dict[str, object]:
    return {
        "op": "all",
        "children": [
            {
                "op": "condition",
                "condition": {
                    "scope": "actor",
                    "key": "clearance",
                    "operator": "equals",
                    "value": "high",
                },
            },
            {
                "op": "any",
                "children": [
                    {
                        "op": "condition",
                        "condition": {
                            "scope": "workspace",
                            "key": "tier",
                            "operator": "in",
                            "values": ["gold", "platinum"],
                        },
                    },
                    {
                        "op": "not",
                        "children": [
                            {
                                "op": "condition",
                                "condition": {
                                    "scope": "actor",
                                    "key": "suspended",
                                    "operator": "exists",
                                },
                            }
                        ],
                    },
                ],
            },
        ],
    }


def test_policy_attribute_expression_round_trips_on_policies_and_templates() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = _nested_expression()  # type: ignore[index]
    manifest["policy_templates"] = [
        {
            "id": "template.gate",
            "decision": "require_approval",
            "attribute_expression": _nested_expression(),
        }
    ]

    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors

    parsed = manifest_from_dict(manifest)
    expression = parsed.policies[0].attribute_expression
    assert expression is not None
    assert expression.op == "all"
    assert expression.children[0].op == "condition"
    assert expression.children[0].condition is not None
    assert expression.children[0].condition.value == "high"
    nested_any = expression.children[1]
    assert nested_any.op == "any"
    assert nested_any.children[0].condition is not None
    assert nested_any.children[0].condition.values == ["gold", "platinum"]
    assert nested_any.children[1].op == "not"
    assert nested_any.children[1].children[0].condition is not None
    assert nested_any.children[1].children[0].condition.operator == "exists"

    template_expression = parsed.policy_templates[0].attribute_expression
    assert template_expression is not None
    assert template_expression.op == "all"


def test_policy_attribute_expression_unsupported_op_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "xor",
        "children": [
            {
                "op": "condition",
                "condition": {
                    "scope": "actor",
                    "key": "clearance",
                    "operator": "exists",
                },
            }
        ],
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "attribute_expression.op must be one of:" in error for error in result.errors
    )


def test_policy_attribute_expression_condition_node_rejects_children() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "condition",
        "condition": {"scope": "actor", "key": "clearance", "operator": "exists"},
        "children": [
            {
                "op": "condition",
                "condition": {
                    "scope": "actor",
                    "key": "team",
                    "operator": "exists",
                },
            }
        ],
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_expression"
    assert f"{prefix} condition node must not contain children" in result.errors


def test_policy_attribute_expression_condition_node_requires_condition() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {"op": "condition"}  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_expression"
    assert f"{prefix} condition node requires exactly one condition" in result.errors


def test_policy_attribute_expression_all_requires_children_without_condition() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "all",
        "children": [],
        "condition": {"scope": "actor", "key": "clearance", "operator": "exists"},
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_expression"
    assert f"{prefix} all node must not contain a direct condition" in result.errors
    assert f"{prefix} all node must contain at least one child" in result.errors


def test_policy_attribute_expression_not_requires_exactly_one_child() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "not",
        "children": [
            {
                "op": "condition",
                "condition": {
                    "scope": "actor",
                    "key": "clearance",
                    "operator": "exists",
                },
            },
            {
                "op": "condition",
                "condition": {
                    "scope": "actor",
                    "key": "team",
                    "operator": "exists",
                },
            },
        ],
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = "policies[policy.require.update.approval].attribute_expression"
    assert f"{prefix} not node must contain exactly one child" in result.errors


def test_policy_attribute_expression_malformed_leaf_condition_is_rejected() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "all",
        "children": [
            {
                "op": "condition",
                "condition": {
                    "scope": "tenant",
                    "key": "clearance",
                    "operator": "matches",
                },
            }
        ],
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = (
        "policies[policy.require.update.approval].attribute_expression"
        ".children[0].attribute_conditions[0]"
    )
    assert f"{prefix}.scope must be one of: actor, workspace" in result.errors
    assert any(
        f"{prefix}.operator must be one of" in error for error in result.errors
    )


def test_policy_attribute_expression_depth_is_bounded() -> None:
    manifest = deepcopy(_valid_manifest())
    node: dict[str, object] = {
        "op": "condition",
        "condition": {"scope": "actor", "key": "clearance", "operator": "exists"},
    }
    # Wrap the leaf in enough not-nodes to exceed the depth bound of 5.
    for _ in range(6):
        node = {"op": "not", "children": [node]}
    manifest["policies"][0]["attribute_expression"] = node  # type: ignore[index]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("must not exceed depth 5" in error for error in result.errors)


def test_policy_attribute_expression_node_count_is_bounded() -> None:
    manifest = deepcopy(_valid_manifest())
    leaves = [
        {
            "op": "condition",
            "condition": {
                "scope": "actor",
                "key": "clearance",
                "operator": "exists",
            },
        }
        for _ in range(40)
    ]
    manifest["policies"][0]["attribute_expression"] = {  # type: ignore[index]
        "op": "all",
        "children": leaves,
    }

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("must not exceed 32 nodes" in error for error in result.errors)


def test_policy_raw_expression_string_fields_are_rejected() -> None:
    for field_name in ("policy_expression", "cel_expression", "expression"):
        manifest = deepcopy(_valid_manifest())
        manifest["policies"][0][field_name] = "actor.clearance == 'high'"  # type: ignore[index]
        manifest["policy_templates"] = [
            {
                "id": "template.gate",
                "decision": "require_approval",
                field_name: "workspace.tier in ['gold']",
            }
        ]

        result = validate_control_plane_manifest(manifest)

        assert not result.valid, field_name
        assert (
            f"policies[0].{field_name} is invalid; use the bounded "
            "attribute_expression tree"
        ) in result.errors
        assert (
            f"policy_templates[0].{field_name} is invalid; use the bounded "
            "attribute_expression tree"
        ) in result.errors


def test_legacy_policy_without_expression_still_validates() -> None:
    manifest = deepcopy(_valid_manifest())

    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors

    parsed = manifest_from_dict(manifest)
    assert parsed.policies[0].attribute_expression is None


def _notification(**overrides: object) -> dict[str, object]:
    notification: dict[str, object] = {
        "id": "notification.approval.assigned.run.control-plane.001",
        "run_id": "run.control-plane.001",
        "workspace_id": "workspace.demo",
        "channel": "local_inbox",
        "event_type": "approval_assigned",
        "status": "queued",
        "approval_id": "approval.sales.manager",
        "capability_id": "capability.hubspot.update.contact",
        "automation_id": "automation.sync.contact",
        "recipient_role": "sales-manager",
        "attempt_count": 0,
    }
    notification.update(overrides)
    return notification


def test_valid_manifest_accepts_local_approval_notification() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [_notification()]

    result = validate_control_plane_manifest(manifest)

    assert result.valid
    assert result.errors == []
    parsed = manifest_from_dict(manifest)
    assert parsed.local_approval_notifications[0].event_type.value == "approval_assigned"
    assert parsed.local_approval_notifications[0].status.value == "queued"


def test_valid_manifest_accepts_external_approval_notification_channel_labels() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [
        _notification(id="notification.email", channel="email"),
        _notification(id="notification.webhook", channel="webhook"),
    ]

    result = validate_control_plane_manifest(manifest)

    assert result.valid
    assert result.errors == []
    parsed = manifest_from_dict(manifest)
    assert [item.channel.value for item in parsed.local_approval_notifications] == [
        "email",
        "webhook",
    ]


def test_notification_requires_known_run_reference() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [_notification(run_id="run.missing")]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = (
        "local_approval_notifications"
        "[notification.approval.assigned.run.control-plane.001]"
    )
    assert f"{prefix}.run_id references unknown id 'run.missing'" in result.errors


def test_notification_invalid_status_fails_closed() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [_notification(status="delivered")]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "local_approval_notifications[].status must be one of" in error
        for error in result.errors
    )


def test_notification_invalid_channel_fails_closed() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [_notification(channel="sms")]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any(
        "local_approval_notifications[].channel must be one of" in error
        for error in result.errors
    )


def test_notification_workspace_must_match() -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["local_approval_notifications"] = [
        _notification(workspace_id="workspace.other")
    ]

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    prefix = (
        "local_approval_notifications"
        "[notification.approval.assigned.run.control-plane.001]"
    )
    assert f"{prefix}.workspace_id must match workspace.id 'workspace.demo'" in result.errors
