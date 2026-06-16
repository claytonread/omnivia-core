"""App Shell bridge contract validation."""

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .models import AppShellBodyDescriptor, AppShellHostContext


class AppShellBridgeValidationError(Exception):
    """Raised when an App Shell bridge contract object fails validation."""
    pass


def _check_required_strings(data: Dict[str, Any], field_names: tuple, errors: List[str]) -> None:
    """Collect errors for required non-empty string fields."""
    for field_name in field_names:
        value = data.get(field_name)
        if value is None:
            errors.append(f"'{field_name}' is required")
        elif not isinstance(value, str):
            errors.append(f"'{field_name}' must be a string, got {type(value).__name__}")
        elif not value.strip():
            errors.append(f"'{field_name}' must not be empty")


def _check_optional_string(data: Dict[str, Any], field_name: str, errors: List[str]) -> None:
    """Collect errors for an optional string field."""
    value = data.get(field_name)
    if value is not None and not isinstance(value, str):
        errors.append(f"'{field_name}' must be a string, got {type(value).__name__}")


def _check_sources(data: Dict[str, Any], errors: List[str]) -> None:
    """Collect errors for the optional sources list."""
    sources_raw = data.get("sources")
    if sources_raw is None:
        return
    if not isinstance(sources_raw, list):
        errors.append("'sources' must be a list")
        return
    for i, item in enumerate(sources_raw):
        if not isinstance(item, dict):
            errors.append(f"'sources[{i}]' must be a dict, got {type(item).__name__}")
        elif not item.get("name"):
            errors.append(f"'sources[{i}]' is missing 'name'")


def _check_string_list(data: Dict[str, Any], field_name: str, errors: List[str]) -> None:
    """Collect errors for an optional list of non-empty strings."""
    values = data.get(field_name)
    if values is None:
        return
    if not isinstance(values, list):
        errors.append(f"'{field_name}' must be a list")
        return
    for i, item in enumerate(values):
        if not isinstance(item, str):
            errors.append(f"'{field_name}[{i}]' must be a string, got {type(item).__name__}")
        elif not item.strip():
            errors.append(f"'{field_name}[{i}]' must not be empty")


def _build_sources(sources_raw: Any) -> list:
    from .models import AppShellSource

    return [
        AppShellSource(name=item["name"], description=item.get("description", ""))
        for item in (sources_raw or [])
    ]


def validate_app_shell_host_context(data: Dict[str, Any]) -> "AppShellHostContext":
    """Validate a plain dict and return an AppShellHostContext.

    Args:
        data: A dictionary containing host context data.

    Returns:
        A validated AppShellHostContext instance.

    Raises:
        AppShellBridgeValidationError: If validation fails.
    """
    errors: List[str] = []

    _check_required_strings(data, ("app_id", "app_name", "entity_label"), errors)

    # runtime_state: required enum
    runtime_state_raw = data.get("runtime_state")
    if runtime_state_raw is None:
        errors.append("'runtime_state' is required")
    else:
        from .models import AppShellRuntimeState
        try:
            AppShellRuntimeState(runtime_state_raw)
        except ValueError:
            valid = ", ".join(s.value for s in AppShellRuntimeState)
            errors.append(
                f"'runtime_state' must be one of: {valid}, got {runtime_state_raw!r}"
            )

    _check_optional_string(data, "permissions_summary", errors)
    _check_optional_string(data, "last_updated", errors)
    _check_sources(data, errors)
    _check_string_list(data, "host_command_ids", errors)

    if errors:
        raise AppShellBridgeValidationError("; ".join(errors))

    from .models import AppShellHostContext, AppShellRuntimeState, ValidationResult

    return AppShellHostContext(
        app_id=data["app_id"],
        app_name=data["app_name"],
        entity_label=data["entity_label"],
        runtime_state=AppShellRuntimeState(data["runtime_state"]),
        permissions_summary=data.get("permissions_summary", ""),
        sources=_build_sources(data.get("sources")),
        last_updated=data.get("last_updated", ""),
        host_command_ids=list(data.get("host_command_ids") or []),
        validation=ValidationResult(is_valid=True),
    )


def validate_app_shell_body_descriptor(data: Dict[str, Any]) -> "AppShellBodyDescriptor":
    """Validate a plain dict and return an AppShellBodyDescriptor.

    Args:
        data: A dictionary containing body descriptor data.

    Returns:
        A validated AppShellBodyDescriptor instance.

    Raises:
        AppShellBridgeValidationError: If validation fails.
    """
    errors: List[str] = []

    _check_required_strings(data, ("app_id", "body_id"), errors)
    _check_sources(data, errors)
    _check_string_list(data, "components", errors)
    _check_string_list(data, "citations", errors)
    _check_string_list(data, "degraded_component_ids", errors)

    # source_count is the count displayed by the App body. It may be more
    # granular than the connector/source list, e.g. 9 source items across 4
    # named connectors.
    sources_raw = data.get("sources")
    source_count_raw = data.get("source_count")
    if source_count_raw is not None:
        if isinstance(source_count_raw, bool) or not isinstance(source_count_raw, int):
            errors.append(
                f"'source_count' must be an integer, got {type(source_count_raw).__name__}"
            )
        elif source_count_raw < 0:
            errors.append("'source_count' must not be negative")

    # degraded_component_ids must be a subset of components
    components_raw = data.get("components")
    degraded_raw = data.get("degraded_component_ids")
    if isinstance(degraded_raw, list) and all(isinstance(item, str) for item in degraded_raw):
        known = set(components_raw) if isinstance(components_raw, list) else set()
        unknown = [item for item in degraded_raw if item not in known]
        if unknown:
            errors.append(
                "'degraded_component_ids' must be a subset of 'components', "
                f"unknown: {', '.join(unknown)}"
            )

    if errors:
        raise AppShellBridgeValidationError("; ".join(errors))

    from .models import AppShellBodyDescriptor, ValidationResult

    sources = _build_sources(sources_raw)
    source_count = source_count_raw if source_count_raw is not None else len(sources)

    return AppShellBodyDescriptor(
        app_id=data["app_id"],
        body_id=data["body_id"],
        source_count=source_count,
        sources=sources,
        components=list(data.get("components") or []),
        citations=list(data.get("citations") or []),
        degraded_component_ids=list(data.get("degraded_component_ids") or []),
        validation=ValidationResult(is_valid=True),
    )
