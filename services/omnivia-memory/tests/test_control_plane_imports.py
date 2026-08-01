import hashlib
import json

from omnivia_memory.control_plane import (
    CONTROL_PLANE_SCHEMA_VERSION,
    LifecycleState,
    WorkspaceRef,
    ControlPlaneManifest,
    detect_import_source_change,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    validate_asyncapi_import_spec,
    validate_control_plane_manifest,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    verify_catalogue_artifacts,
)


def _assert_valid_candidate_set(candidate_set) -> None:
    manifest = ControlPlaneManifest(
        workspace=WorkspaceRef(id="workspace.imports"),
        schema_version=CONTROL_PLANE_SCHEMA_VERSION,
        connections=candidate_set.connections,
        capabilities=candidate_set.capabilities,
        triggers=candidate_set.triggers,
        import_records=[candidate_set.import_record],
    )
    result = validate_control_plane_manifest(manifest)

    assert result.valid, result.errors
    assert candidate_set.import_record.lifecycle == LifecycleState.CANDIDATE
    assert all(cap.lifecycle == LifecycleState.CANDIDATE for cap in candidate_set.capabilities)
    assert all(trigger.lifecycle == LifecycleState.CANDIDATE for trigger in candidate_set.triggers)
    assert candidate_set.import_record.candidate_capability_ids == [
        cap.id for cap in candidate_set.capabilities
    ]


def test_import_mcp_candidates_stays_inactive_and_mediated() -> None:
    candidates = import_mcp_candidates(
        "mcp://local/notion",
        {
            "name": "Notion MCP",
            "tools": [{"name": "create_page"}],
            "resources": [{"uri": "notion://database/pages"}],
            "prompts": [{"name": "summarize_page"}],
        },
    )

    _assert_valid_candidate_set(candidates)
    assert candidates.connections[0].kind.value == "mcp"
    assert [cap.capability_type.value for cap in candidates.capabilities] == [
        "action",
        "resource",
        "prompt",
    ]
    assert candidates.capabilities[0].side_effect.value == "external_write"
    assert candidates.capabilities[1].side_effect.value == "read"
    assert candidates.capabilities[2].side_effect.value == "none"
    rendered = repr(candidates)
    assert "client_secret" not in rendered
    assert "access_token" not in rendered


def test_validate_mcp_import_spec_rejects_empty_or_unnamed_candidates() -> None:
    empty = validate_mcp_import_spec({"name": "Empty MCP"})
    unnamed = validate_mcp_import_spec({"tools": [{}]})

    assert not empty.valid
    assert "at least one tool, resource or prompt" in empty.errors[0]
    assert not unnamed.valid
    assert "tools[0] must include name or id" in unnamed.errors[0]

    try:
        import_mcp_candidates("mcp://empty", {"tools": [{}]})
    except ValueError as exc:
        assert "tools[0] must include name or id" in str(exc)
    else:
        raise AssertionError("invalid MCP spec should fail closed")


def test_import_openapi_candidates_defaults_write_operations_conservative() -> None:
    candidates = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {
                "/contacts": {
                    "get": {"operationId": "listContacts"},
                    "post": {"operationId": "createContact"},
                },
                "/contacts/{id}": {
                    "delete": {"operationId": "deleteContact"},
                },
            },
        },
    )

    _assert_valid_candidate_set(candidates)
    effects = {cap.display_name: cap.side_effect.value for cap in candidates.capabilities}
    assert effects["listContacts"] == "read"
    assert effects["createContact"] == "external_write"
    assert effects["deleteContact"] == "destructive"
    assert all(cap.metadata["source_protocol"] == "openapi" for cap in candidates.capabilities)


def test_validate_openapi_import_spec_rejects_invalid_contracts() -> None:
    missing_version = validate_openapi_import_spec(
        {
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        }
    )
    empty_paths = validate_openapi_import_spec(
        {"openapi": "3.1.0", "info": {"title": "CRM API"}, "paths": {}}
    )
    empty_operation_id = validate_openapi_import_spec(
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": ""}}},
        }
    )

    assert not missing_version.valid
    assert "OpenAPI 3.x version" in missing_version.errors[0]
    assert not empty_paths.valid
    assert "paths must be a non-empty object" in empty_paths.errors[0]
    assert not empty_operation_id.valid
    assert "empty operationId" in empty_operation_id.errors[0]

    try:
        import_openapi_candidates(
            "openapi://invalid",
            {"openapi": "2.0", "info": {"title": "Old"}, "paths": {}},
        )
    except ValueError as exc:
        assert "OpenAPI 3.x version" in str(exc)
    else:
        raise AssertionError("invalid OpenAPI spec should fail closed")


def test_import_asyncapi_candidates_creates_event_source_triggers() -> None:
    candidates = import_asyncapi_candidates(
        "asyncapi://events",
        {
            "asyncapi": "3.0.0",
            "info": {"title": "Events"},
            "channels": {
                "crm.contact.updated": {
                    "subscribe": {"message": {"name": "com.crm.contact.updated"}}
                }
            },
        },
    )

    _assert_valid_candidate_set(candidates)
    assert candidates.capabilities[0].capability_type.value == "event_source"
    assert candidates.triggers[0].kind.value == "cloudevent"
    assert candidates.triggers[0].event_type == "com.crm.contact.updated"


def test_validate_asyncapi_import_spec_rejects_invalid_contracts() -> None:
    missing_channels = validate_asyncapi_import_spec(
        {"asyncapi": "3.0.0", "info": {"title": "Events"}}
    )
    no_operations = validate_asyncapi_import_spec(
        {"asyncapi": "3.0.0", "info": {"title": "Events"}, "channels": {"events": {}}}
    )
    invalid_operation = validate_asyncapi_import_spec(
        {
            "asyncapi": "3.0.0",
            "info": {"title": "Events"},
            "channels": {"events": {"subscribe": "not-an-object"}},
        }
    )

    assert not missing_channels.valid
    assert "channels must be a non-empty object" in missing_channels.errors[0]
    assert not no_operations.valid
    assert "at least one subscribe or publish operation" in no_operations.errors[0]
    assert not invalid_operation.valid
    assert "must be an object" in invalid_operation.errors[0]

    try:
        import_asyncapi_candidates(
            "asyncapi://invalid",
            {"asyncapi": "1.0.0", "info": {"title": "Events"}, "channels": {}},
        )
    except ValueError as exc:
        assert "AsyncAPI 2.x or 3.x version" in str(exc)
    else:
        raise AssertionError("invalid AsyncAPI spec should fail closed")


def test_import_catalogue_connection_and_trigger_candidates() -> None:
    connection_candidates = import_catalogue_candidates(
        "omnivia-catalog:generated/catalog.json#app-hubspot",
        {
            "id": "app-hubspot",
            "component_type": "connection",
            "connection_type": "custom_app",
            "display_name": "HubSpot",
            "capabilities": {
                "read": ["contacts"],
                "write": ["contacts"],
                "send": ["email"],
            },
            "risk": {"level": "high"},
            "default_permissions": {"needs_approval": ["write", "send"]},
        },
    )
    trigger_candidates = import_catalogue_candidates(
        "omnivia-catalog:generated/catalog.json#trigger-hubspot-contact",
        {
            "id": "trigger-hubspot-contact",
            "component_type": "trigger",
            "trigger_type": "webhook",
            "display_name": "HubSpot contact updated",
            "events": [{"name": "com.hubspot.contact.updated"}],
        },
    )

    _assert_valid_candidate_set(connection_candidates)
    _assert_valid_candidate_set(trigger_candidates)
    assert connection_candidates.connections[0].catalogue_entry_id == "app-hubspot"
    assert all(
        cap.metadata["source_protocol"] == "catalogue"
        for cap in connection_candidates.capabilities
    )
    assert trigger_candidates.triggers[0].lifecycle == LifecycleState.CANDIDATE
    assert trigger_candidates.triggers[0].event_type == "com.hubspot.contact.updated"


def test_verify_catalogue_artifacts_matches_generated_hashes_and_counts() -> None:
    catalog = _generated_catalog()
    search = {"documents": [{"id": "app-hubspot"}, {"id": "template-review"}]}
    manifest = _generated_manifest(catalog, search)

    result = verify_catalogue_artifacts(catalog, search, manifest)

    assert result.valid, result.errors


def test_verify_catalogue_artifacts_rejects_tampered_catalog_hash() -> None:
    catalog = _generated_catalog()
    search = {"documents": [{"id": "app-hubspot"}, {"id": "template-review"}]}
    manifest = _generated_manifest(catalog, search)
    catalog["connections"][0]["display_name"] = "Tampered"

    result = verify_catalogue_artifacts(catalog, search, manifest)

    assert not result.valid
    assert "catalog_hash does not match" in result.errors[0]


def test_import_generated_catalogue_artifacts_includes_template_candidates() -> None:
    catalog = _generated_catalog()
    search = {"documents": [{"id": "app-hubspot"}, {"id": "template-review"}]}
    manifest = _generated_manifest(catalog, search)

    candidate_sets = import_catalogue_generated_candidates(
        "omnivia-catalog:generated/catalog.json",
        catalog,
        search,
        manifest,
    )

    assert len(candidate_sets) == 2
    template_set = candidate_sets[1]
    _assert_valid_candidate_set(template_set)
    assert template_set.import_record.lifecycle == LifecycleState.CANDIDATE
    assert template_set.connections[0].catalogue_entry_id == "template-review"
    assert [cap.id for cap in template_set.capabilities] == [
        "capability.candidate.catalogue.template.capability.review.read",
        "capability.candidate.catalogue.template.capability.review.send",
    ]
    assert [cap.side_effect.value for cap in template_set.capabilities] == [
        "read",
        "send",
    ]
    assert all(cap.lifecycle == LifecycleState.CANDIDATE for cap in template_set.capabilities)


def test_import_generated_catalogue_artifacts_fails_closed_on_hash_mismatch() -> None:
    catalog = _generated_catalog()
    search = {"documents": [{"id": "app-hubspot"}, {"id": "template-review"}]}
    manifest = _generated_manifest(catalog, search)
    manifest["catalog_hash"] = "wrong"

    try:
        import_catalogue_generated_candidates(
            "omnivia-catalog:generated/catalog.json",
            catalog,
            search,
            manifest,
        )
    except ValueError as exc:
        assert "catalog_hash does not match" in str(exc)
    else:
        raise AssertionError("hash mismatch should fail closed")


def test_detect_import_source_change_reports_unchanged_sources() -> None:
    candidates = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        },
    )

    result = detect_import_source_change(candidates.import_record, candidates)

    assert not result.changed
    assert result.reason == "unchanged"
    assert result.previous_source_hash == candidates.import_record.source_hash
    assert result.current_source_hash == candidates.import_record.source_hash


def test_detect_import_source_change_reports_hash_changes() -> None:
    original = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        },
    )
    refreshed = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {
                "/contacts": {"get": {"operationId": "listContacts"}},
                "/companies": {"get": {"operationId": "listCompanies"}},
            },
        },
    )

    result = detect_import_source_change(original.import_record, refreshed)

    assert result.changed
    assert result.reason == "source_hash_changed"
    assert result.previous_source_hash == original.import_record.source_hash
    assert result.current_source_hash == refreshed.import_record.source_hash


def test_import_record_rejects_active_candidate_activation() -> None:
    candidates = import_openapi_candidates(
        "openapi://unsafe",
        {
            "openapi": "3.1.0",
            "info": {"title": "Unsafe"},
            "paths": {"/danger": {"post": {"operationId": "mutate"}}},
        },
    )
    active_capability = candidates.capabilities[0].__class__(
        **{
            **candidates.capabilities[0].__dict__,
            "lifecycle": LifecycleState.ACTIVE,
        }
    )
    manifest = ControlPlaneManifest(
        workspace=WorkspaceRef(id="workspace.imports"),
        connections=candidates.connections,
        capabilities=[active_capability],
        import_records=[candidates.import_record],
    )

    result = validate_control_plane_manifest(manifest)

    assert not result.valid
    assert any("imports must stay candidate-only" in error for error in result.errors)


def test_import_record_allows_explicit_released_active_capability() -> None:
    candidates = import_openapi_candidates(
        "openapi://reviewed",
        {
            "openapi": "3.1.0",
            "info": {"title": "Reviewed"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        },
    )
    active_capability = candidates.capabilities[0].__class__(
        **{
            **candidates.capabilities[0].__dict__,
            "lifecycle": LifecycleState.ACTIVE,
        }
    )
    released_import_record = candidates.import_record.__class__(
        **{
            **candidates.import_record.__dict__,
            "released_capability_ids": [active_capability.id],
        }
    )
    manifest = ControlPlaneManifest(
        workspace=WorkspaceRef(id="workspace.imports"),
        connections=candidates.connections,
        capabilities=[active_capability],
        import_records=[released_import_record],
    )

    result = validate_control_plane_manifest(manifest)

    assert result.valid, result.errors


def _generated_catalog() -> dict:
    return {
        "version": "0.1.0",
        "schema_version": "0.1.0",
        "connections": [
            {
                "id": "app-hubspot",
                "component_type": "connection",
                "connection_type": "custom_app",
                "display_name": "HubSpot",
                "capabilities": {"read": ["contacts"]},
                "risk": {"level": "medium"},
                "default_permissions": {"can": ["Read contacts"]},
            }
        ],
        "triggers": [],
        "templates": [
            {
                "id": "template-review",
                "component_type": "automation_template",
                "display_name": "Review Template",
                "candidate_capabilities": [
                    {
                        "id": "capability.review.read",
                        "side_effect": "read",
                        "approval_required": False,
                    },
                    {
                        "id": "capability.review.send",
                        "side_effect": "send",
                        "approval_required": True,
                    },
                ],
                "default_permissions": {
                    "can": ["Read source metadata"],
                    "needs_approval": ["Send external messages"],
                },
                "marketplace": {"pack_id": "pack-review"},
            }
        ],
        "sources": [{"id": "manual"}],
        "generated_at": "2026-06-21T00:00:00Z",
    }


def _generated_manifest(catalog: dict, search: dict) -> dict:
    return {
        "version": catalog["version"],
        "schema_version": catalog["schema_version"],
        "generated_at": catalog["generated_at"],
        "catalog_hash": _json_stringify_hash(catalog),
        "search_index_hash": _json_stringify_hash(search),
        "entry_counts": {
            "connections_total": 1,
            "custom_app_total": 1,
            "custom_api_total": 0,
            "custom_mcp_total": 0,
            "triggers_total": 0,
            "templates_total": 1,
        },
        "sources_included": ["manual"],
    }


def _json_stringify_hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
