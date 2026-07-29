"""Import adapters for inactive control-plane capability candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from omnivia_core.control_plane.models import (
    Capability,
    CapabilityType,
    Connection,
    ConnectionKind,
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    SideEffect,
    Trigger,
    TriggerKind,
)


@dataclass(frozen=True)
class ImportedCandidateSet:
    """Candidate-only resources derived from an external metadata source."""

    import_record: ImportRecord
    connections: list[Connection] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogueArtifactVerification:
    """Verification result for generated omnivia-catalog artifacts."""

    valid: bool
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportSourceChange:
    """Source-change detection result for imported candidate metadata."""

    import_record_id: str
    source_ref: str
    previous_source_hash: str | None
    current_source_hash: str | None
    changed: bool
    reason: str = ""


@dataclass(frozen=True)
class ImportSpecValidation:
    """Local parser validation result for external import specs."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_mcp_import_spec(spec: dict[str, Any]) -> ImportSpecValidation:
    """Validate an MCP import payload before candidate conversion."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return ImportSpecValidation(valid=False, errors=["mcp spec must be an object"])

    collection_names = ("tools", "resources", "prompts")
    if not any(_list(spec.get(name)) for name in collection_names):
        errors.append("mcp spec must include at least one tool, resource or prompt")

    for index, tool in enumerate(_list(spec.get("tools"))):
        if not isinstance(tool, dict):
            errors.append(f"mcp tools[{index}] must be an object")
            continue
        if not _non_empty_string(tool.get("name") or tool.get("id")):
            errors.append(f"mcp tools[{index}] must include name or id")

    for index, resource in enumerate(_list(spec.get("resources"))):
        if not isinstance(resource, dict):
            errors.append(f"mcp resources[{index}] must be an object")
            continue
        if not _non_empty_string(resource.get("uri") or resource.get("name")):
            errors.append(f"mcp resources[{index}] must include uri or name")

    for index, prompt in enumerate(_list(spec.get("prompts"))):
        if not isinstance(prompt, dict):
            errors.append(f"mcp prompts[{index}] must be an object")
            continue
        if not _non_empty_string(prompt.get("name") or prompt.get("id")):
            errors.append(f"mcp prompts[{index}] must include name or id")

    if not _non_empty_string(spec.get("name")):
        warnings.append("mcp spec has no display name; source_ref will be used")
    return ImportSpecValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_openapi_import_spec(spec: dict[str, Any]) -> ImportSpecValidation:
    """Validate an OpenAPI import payload before candidate conversion."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return ImportSpecValidation(valid=False, errors=["openapi spec must be an object"])

    version = spec.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        errors.append("openapi spec must declare an OpenAPI 3.x version")

    info = spec.get("info")
    if not isinstance(info, dict):
        errors.append("openapi spec info must be an object")
    elif not _non_empty_string(info.get("title")):
        errors.append("openapi spec info.title is required")

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        errors.append("openapi spec paths must be a non-empty object")
        return ImportSpecValidation(valid=False, errors=errors, warnings=warnings)

    operation_count = 0
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            errors.append(f"openapi path {path} must be an object")
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_count += 1
            if not isinstance(operation, dict):
                errors.append(f"openapi operation {method.upper()} {path} must be an object")
                continue
            if "operationId" in operation and not _non_empty_string(
                operation.get("operationId")
            ):
                errors.append(
                    f"openapi operation {method.upper()} {path} has an empty operationId"
                )

    if operation_count == 0:
        errors.append("openapi spec must include at least one HTTP operation")
    if operation_count > 0 and not any(
        isinstance(operation, dict) and _non_empty_string(operation.get("operationId"))
        for path_item in paths.values()
        if isinstance(path_item, dict)
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    ):
        warnings.append("openapi spec has no operationId values; fallback names will be used")
    return ImportSpecValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_asyncapi_import_spec(spec: dict[str, Any]) -> ImportSpecValidation:
    """Validate an AsyncAPI import payload before candidate conversion."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return ImportSpecValidation(valid=False, errors=["asyncapi spec must be an object"])

    version = spec.get("asyncapi")
    if not isinstance(version, str) or not (
        version.startswith("2.") or version.startswith("3.")  # noqa: PIE810
    ):
        errors.append("asyncapi spec must declare an AsyncAPI 2.x or 3.x version")

    info = spec.get("info")
    if not isinstance(info, dict):
        errors.append("asyncapi spec info must be an object")
    elif not _non_empty_string(info.get("title")):
        errors.append("asyncapi spec info.title is required")

    channels = spec.get("channels")
    if not isinstance(channels, dict) or not channels:
        errors.append("asyncapi spec channels must be a non-empty object")
        return ImportSpecValidation(valid=False, errors=errors, warnings=warnings)

    operation_count = 0
    named_message_count = 0
    for channel_name, channel in channels.items():
        if not isinstance(channel, dict):
            errors.append(f"asyncapi channel {channel_name} must be an object")
            continue
        for operation_name in ("subscribe", "publish"):
            operation = channel.get(operation_name)
            if operation is None:
                continue
            operation_count += 1
            if not isinstance(operation, dict):
                errors.append(
                    f"asyncapi {operation_name} operation on {channel_name} must be an object"
                )
                continue
            message = operation.get("message")
            if isinstance(message, dict) and _non_empty_string(
                message.get("name") or message.get("messageId")
            ):
                named_message_count += 1

    if operation_count == 0:
        errors.append("asyncapi spec must include at least one subscribe or publish operation")
    if operation_count > 0 and named_message_count == 0:
        warnings.append("asyncapi spec has no named messages; channel names will be used")
    return ImportSpecValidation(valid=not errors, errors=errors, warnings=warnings)


def import_mcp_candidates(
    source_ref: str,
    spec: dict[str, Any],
    *,
    connection_id: str | None = None,
) -> ImportedCandidateSet:
    """Convert MCP tools/resources/prompts into inactive capability candidates."""

    validation = validate_mcp_import_spec(spec)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    connection = Connection(
        id=connection_id or f"connection.import.mcp.{_slug(source_ref)}",
        kind=ConnectionKind.MCP,
        display_name=str(spec.get("name") or source_ref),
        lifecycle=LifecycleState.CANDIDATE,
    )
    import_record_id = f"import.mcp.{_slug(source_ref)}"
    capabilities: list[Capability] = []

    for tool in _list(spec.get("tools")):
        name = str(tool.get("name") or tool.get("id") or "tool")
        side_effect = _side_effect_from_hint(tool, default=SideEffect.EXTERNAL_WRITE)
        capabilities.append(
            _capability(
                import_record_id,
                connection.id,
                "mcp.tool",
                name,
                CapabilityType.ACTION,
                side_effect,
                {"source_protocol": "mcp", "mcp_kind": "tool"},
            )
        )
    for resource in _list(spec.get("resources")):
        name = str(resource.get("name") or resource.get("uri") or "resource")
        capabilities.append(
            _capability(
                import_record_id,
                connection.id,
                "mcp.resource",
                name,
                CapabilityType.RESOURCE,
                SideEffect.READ,
                {"source_protocol": "mcp", "mcp_kind": "resource"},
            )
        )
    for prompt in _list(spec.get("prompts")):
        name = str(prompt.get("name") or prompt.get("id") or "prompt")
        capabilities.append(
            _capability(
                import_record_id,
                connection.id,
                "mcp.prompt",
                name,
                CapabilityType.PROMPT,
                SideEffect.NONE,
                {"source_protocol": "mcp", "mcp_kind": "prompt"},
            )
        )

    return _candidate_set(
        ImportSourceProtocol.MCP,
        import_record_id,
        source_ref,
        [connection],
        capabilities,
        [],
        spec,
    )


def import_openapi_candidates(
    source_ref: str,
    spec: dict[str, Any],
    *,
    connection_id: str | None = None,
) -> ImportedCandidateSet:
    """Convert OpenAPI 3.1 operations into inactive capability candidates."""

    validation = validate_openapi_import_spec(spec)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    connection = Connection(
        id=connection_id or f"connection.import.openapi.{_slug(source_ref)}",
        kind=ConnectionKind.API,
        display_name=str(info.get("title") or source_ref),  # type: ignore[union-attr]
        lifecycle=LifecycleState.CANDIDATE,
    )
    import_record_id = f"import.openapi.{_slug(source_ref)}"
    capabilities: list[Capability] = []
    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    for path, path_item in paths.items():  # type: ignore[union-attr]
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_obj = operation if isinstance(operation, dict) else {}
            operation_id = str(operation_obj.get("operationId") or f"{method}_{path}")
            capabilities.append(
                _capability(
                    import_record_id,
                    connection.id,
                    f"openapi.{method.lower()}",
                    operation_id,
                    CapabilityType.QUERY if method.lower() == "get" else CapabilityType.ACTION,
                    _http_side_effect(method.lower()),
                    {
                        "source_protocol": "openapi",
                        "method": method.upper(),
                        "path": str(path),
                    },
                )
            )

    return _candidate_set(
        ImportSourceProtocol.OPENAPI,
        import_record_id,
        source_ref,
        [connection],
        capabilities,
        [],
        spec,
    )


def import_asyncapi_candidates(
    source_ref: str,
    spec: dict[str, Any],
    *,
    connection_id: str | None = None,
) -> ImportedCandidateSet:
    """Convert AsyncAPI channels into inactive event-source candidates."""

    validation = validate_asyncapi_import_spec(spec)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    connection = Connection(
        id=connection_id or f"connection.import.asyncapi.{_slug(source_ref)}",
        kind=ConnectionKind.WEBHOOK,
        display_name=str(info.get("title") or source_ref),  # type: ignore[union-attr]
        lifecycle=LifecycleState.CANDIDATE,
    )
    import_record_id = f"import.asyncapi.{_slug(source_ref)}"
    capabilities: list[Capability] = []
    triggers: list[Trigger] = []
    channels = spec.get("channels") if isinstance(spec.get("channels"), dict) else {}
    for channel_name, channel in channels.items():  # type: ignore[union-attr]
        channel_obj = channel if isinstance(channel, dict) else {}
        for operation_name in ("subscribe", "publish"):
            operation = channel_obj.get(operation_name)
            if not isinstance(operation, dict):
                continue
            event_type = _asyncapi_event_type(channel_name, operation)
            capability = _capability(
                import_record_id,
                connection.id,
                f"asyncapi.{operation_name}",
                event_type,
                CapabilityType.EVENT_SOURCE,
                SideEffect.READ,
                {
                    "source_protocol": "asyncapi",
                    "channel": str(channel_name),
                    "operation": operation_name,
                },
            )
            capabilities.append(capability)
            triggers.append(
                Trigger(
                    id=f"trigger.candidate.{_slug(event_type)}",
                    kind=TriggerKind.CLOUDEVENT,
                    capability_id=capability.id,
                    event_type=event_type,
                    lifecycle=LifecycleState.CANDIDATE,
                )
            )

    return _candidate_set(
        ImportSourceProtocol.ASYNCAPI,
        import_record_id,
        source_ref,
        [connection],
        capabilities,
        triggers,
        spec,
    )


def import_catalogue_candidates(
    source_ref: str,
    entry: dict[str, Any],
) -> ImportedCandidateSet:
    """Convert public omnivia-catalog metadata into inactive candidates."""

    entry_id = str(entry.get("id") or entry.get("slug") or source_ref)
    component_type = str(entry.get("component_type") or "connection")
    import_record_id = f"import.catalogue.{_slug(entry_id)}"
    connections: list[Connection] = []
    capabilities: list[Capability] = []
    triggers: list[Trigger] = []

    if component_type == "trigger":
        connection = Connection(
            id=f"catalog.connection.trigger-source.{_slug(entry_id)}",
            kind=ConnectionKind.WEBHOOK,
            display_name=str(entry.get("display_name") or entry_id),
            lifecycle=LifecycleState.CANDIDATE,
            catalogue_entry_id=entry_id,
        )
        connections.append(connection)
        events = _list(entry.get("events")) or [{"name": entry.get("trigger_type") or entry_id}]
        for event in events:
            event_name = str(event.get("name") or event.get("type") or event)
            capability = _capability(
                import_record_id,
                connection.id,
                "catalogue.trigger",
                event_name,
                CapabilityType.EVENT_SOURCE,
                SideEffect.READ,
                {"source_protocol": "catalogue", "catalogue_entry_id": entry_id},
            )
            capabilities.append(capability)
            triggers.append(
                Trigger(
                    id=f"trigger.candidate.{_slug(entry_id)}.{_slug(event_name)}",
                    kind=TriggerKind.CATALOGUE_EVENT,
                    capability_id=capability.id,
                    event_type=event_name,
                    lifecycle=LifecycleState.CANDIDATE,
                )
            )
    elif component_type == "automation_template":
        connection = Connection(
            id=f"catalog.connection.template.{_slug(entry_id)}",
            kind=ConnectionKind.APP,
            display_name=str(entry.get("display_name") or entry_id),
            lifecycle=LifecycleState.CANDIDATE,
            catalogue_entry_id=entry_id,
        )
        connections.append(connection)
        marketplace = entry.get("marketplace") if isinstance(entry.get("marketplace"), dict) else {}
        for candidate in _list(entry.get("candidate_capabilities")):
            name = str(candidate.get("id") or candidate.get("name") or "capability")
            capabilities.append(
                _capability(
                    import_record_id,
                    connection.id,
                    "catalogue.template",
                    name,
                    CapabilityType.ACTION,
                    _side_effect_from_hint(candidate, default=SideEffect.EXTERNAL_WRITE),
                    {
                        "source_protocol": "catalogue",
                        "catalogue_entry_id": entry_id,
                        "template_pack_id": marketplace.get("pack_id"),  # type: ignore[union-attr]
                        "approval_required": candidate.get("approval_required"),
                        "default_permissions": entry.get("default_permissions", {}),
                    },
                )
            )
    else:
        connection = Connection(
            id=f"catalog.connection.{_slug(entry_id)}",
            kind=_catalogue_connection_kind(str(entry.get("connection_type") or "")),
            display_name=str(entry.get("display_name") or entry_id),
            lifecycle=LifecycleState.CANDIDATE,
            catalogue_entry_id=entry_id,
        )
        connections.append(connection)
        caps = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
        for group_name, names in caps.items():  # type: ignore[union-attr]
            for item in _capability_names(names):
                capabilities.append(
                    _capability(
                        import_record_id,
                        connection.id,
                        f"catalogue.{group_name}",
                        item,
                        _catalogue_capability_type(str(group_name)),
                        _catalogue_side_effect(str(group_name), entry),
                        {
                            "source_protocol": "catalogue",
                            "catalogue_entry_id": entry_id,
                            "capability_group": str(group_name),
                            "risk": entry.get("risk", {}),
                            "default_permissions": entry.get("default_permissions", {}),
                        },
                    )
                )

    return _candidate_set(
        ImportSourceProtocol.CATALOGUE,
        import_record_id,
        source_ref,
        connections,
        capabilities,
        triggers,
        entry,
    )


def verify_catalogue_artifacts(
    catalog: dict[str, Any],
    search_index: dict[str, Any],
    manifest: dict[str, Any],
) -> CatalogueArtifactVerification:
    """Verify generated omnivia-catalog artifacts before candidate import."""

    errors: list[str] = []
    expected_catalog_hash = manifest.get("catalog_hash")
    expected_search_hash = manifest.get("search_index_hash")
    if expected_catalog_hash != _json_stringify_hash(catalog):
        errors.append("catalog_hash does not match generated catalog")
    if expected_search_hash != _json_stringify_hash(search_index):
        errors.append("search_index_hash does not match generated search index")

    counts = manifest.get("entry_counts") if isinstance(manifest.get("entry_counts"), dict) else {}
    connections = _catalog_entries(catalog, "connections")
    triggers = _catalog_entries(catalog, "triggers")
    templates = _catalog_entries(catalog, "templates")
    expected_counts = {
        "connections_total": len(connections),
        "triggers_total": len(triggers),
        "templates_total": len(templates),
    }
    for key, value in expected_counts.items():
        if counts.get(key) != value:  # type: ignore[union-attr]
            errors.append(f"{key} does not match generated catalog")

    custom_counts = {
        "custom_app_total": sum(
            1 for entry in connections if entry.get("connection_type") == "custom_app"
        ),
        "custom_api_total": sum(
            1 for entry in connections if entry.get("connection_type") == "custom_api"
        ),
        "custom_mcp_total": sum(
            1 for entry in connections if entry.get("connection_type") == "custom_mcp"
        ),
    }
    for key, value in custom_counts.items():
        if counts.get(key) != value:  # type: ignore[union-attr]
            errors.append(f"{key} does not match generated catalog")

    if not isinstance(manifest.get("sources_included"), list):
        errors.append("sources_included must be present")

    return CatalogueArtifactVerification(valid=not errors, errors=errors)


def import_catalogue_generated_candidates(
    source_ref: str,
    catalog: dict[str, Any],
    search_index: dict[str, Any],
    manifest: dict[str, Any],
) -> list[ImportedCandidateSet]:
    """Verify generated artifacts and convert entries to inactive candidates."""

    verification = verify_catalogue_artifacts(catalog, search_index, manifest)
    if not verification.valid:
        raise ValueError("; ".join(verification.errors))

    candidate_sets: list[ImportedCandidateSet] = []
    for collection_name in ("connections", "triggers", "templates"):
        for entry in _catalog_entries(catalog, collection_name):
            entry_id = str(entry.get("id") or collection_name)
            candidate_sets.append(
                import_catalogue_candidates(f"{source_ref}#{entry_id}", entry)
            )
    return candidate_sets


def detect_import_source_change(
    previous: ImportRecord,
    current: ImportedCandidateSet,
) -> ImportSourceChange:
    """Compare a stored import record with a refreshed candidate import."""

    if previous.id != current.import_record.id:
        return ImportSourceChange(
            import_record_id=current.import_record.id,
            source_ref=current.import_record.source_ref,
            previous_source_hash=previous.source_hash,
            current_source_hash=current.import_record.source_hash,
            changed=True,
            reason="import_record_id_changed",
        )
    if previous.source_ref != current.import_record.source_ref:
        return ImportSourceChange(
            import_record_id=current.import_record.id,
            source_ref=current.import_record.source_ref,
            previous_source_hash=previous.source_hash,
            current_source_hash=current.import_record.source_hash,
            changed=True,
            reason="source_ref_changed",
        )
    if previous.source_hash != current.import_record.source_hash:
        return ImportSourceChange(
            import_record_id=current.import_record.id,
            source_ref=current.import_record.source_ref,
            previous_source_hash=previous.source_hash,
            current_source_hash=current.import_record.source_hash,
            changed=True,
            reason="source_hash_changed",
        )
    return ImportSourceChange(
        import_record_id=current.import_record.id,
        source_ref=current.import_record.source_ref,
        previous_source_hash=previous.source_hash,
        current_source_hash=current.import_record.source_hash,
        changed=False,
        reason="unchanged",
    )


def _candidate_set(
    protocol: ImportSourceProtocol,
    import_record_id: str,
    source_ref: str,
    connections: list[Connection],
    capabilities: list[Capability],
    triggers: list[Trigger],
    raw_source: dict[str, Any],
) -> ImportedCandidateSet:
    return ImportedCandidateSet(
        import_record=ImportRecord(
            id=import_record_id,
            source_protocol=protocol,
            source_ref=source_ref,
            candidate_capability_ids=[capability.id for capability in capabilities],
            source_hash=_stable_hash(raw_source),
            lifecycle=LifecycleState.CANDIDATE,
        ),
        connections=connections,
        capabilities=capabilities,
        triggers=triggers,
    )


def _capability(
    import_record_id: str,
    connection_id: str,
    namespace: str,
    name: str,
    capability_type: CapabilityType,
    side_effect: SideEffect,
    metadata: dict[str, Any],
) -> Capability:
    return Capability(
        id=f"capability.candidate.{_slug(namespace)}.{_slug(name)}",
        capability_type=capability_type,
        connection_id=connection_id,
        side_effect=side_effect,
        lifecycle=LifecycleState.CANDIDATE,
        display_name=name,
        import_record_id=import_record_id,
        metadata=metadata,
    )


def _side_effect_from_hint(data: dict[str, Any], *, default: SideEffect) -> SideEffect:
    raw = str(data.get("side_effect") or data.get("sideEffect") or "").lower()
    if raw in {item.value for item in SideEffect}:
        return SideEffect(raw)
    if data.get("readOnly") is True or data.get("read_only") is True:
        return SideEffect.READ
    return default


def _http_side_effect(method: str) -> SideEffect:
    if method == "get":
        return SideEffect.READ
    if method == "delete":
        return SideEffect.DESTRUCTIVE
    return SideEffect.EXTERNAL_WRITE


def _asyncapi_event_type(channel_name: Any, operation: dict[str, Any]) -> str:
    message = operation.get("message")
    if isinstance(message, dict):
        name = message.get("name") or message.get("messageId")
        if name:
            return str(name)
    return str(channel_name)


def _catalogue_connection_kind(connection_type: str) -> ConnectionKind:
    if connection_type.endswith("mcp"):
        return ConnectionKind.MCP
    if connection_type.endswith("api"):
        return ConnectionKind.API
    return ConnectionKind.APP


def _catalogue_capability_type(group_name: str) -> CapabilityType:
    if group_name in {"read", "search"}:
        return CapabilityType.QUERY
    if group_name in {"triggers", "events"}:
        return CapabilityType.EVENT_SOURCE
    if group_name == "resources":
        return CapabilityType.RESOURCE
    if group_name == "prompts":
        return CapabilityType.PROMPT
    return CapabilityType.ACTION


def _catalogue_side_effect(group_name: str, entry: dict[str, Any]) -> SideEffect:
    risk = entry.get("risk") if isinstance(entry.get("risk"), dict) else {}
    risk_level = str(risk.get("level") or "").lower()  # type: ignore[union-attr]
    if risk_level in {"high", "critical"}:
        return SideEffect.EXTERNAL_WRITE
    if group_name in {"read", "search", "triggers", "events", "resources", "prompts"}:
        return SideEffect.READ if group_name != "prompts" else SideEffect.NONE
    if group_name == "send":
        return SideEffect.SEND
    if group_name == "write":
        return SideEffect.EXTERNAL_WRITE
    return SideEffect.EXTERNAL_WRITE


def _capability_names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item.get("name") if isinstance(item, dict) else item) for item in value]
    if isinstance(value, dict):
        return [str(key) for key, enabled in value.items() if enabled]
    if isinstance(value, str):
        return [value]
    return []


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"name": item} for item in value]
    return []


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _slug(value: Any) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", ".", text)
    return text.strip(".") or "candidate"


def _stable_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_stringify_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _catalog_entries(catalog: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = catalog.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]
