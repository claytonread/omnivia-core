"""App Manifest validation."""

# ruff: noqa: PIE790, UP006, UP035 -- canonical port preserves the legacy
# `typing.Dict` spelling verbatim; do not let pyupgrade modernize it.

from typing import Any, Dict

from omnivia_core.app_manifest.models import (
    AppManifest,
    AppState,
    DataSource,
    ProvenanceRequirement,
    ValidationResult,
)


class AppManifestValidationError(Exception):
    """Raised when an App Manifest fails validation."""

    pass


def validate_app_manifest(data: Dict[str, Any]) -> AppManifest:
    """Validate a plain dict and return an AppManifest.

    Args:
        data: A dictionary containing manifest data.

    Returns:
        A validated AppManifest instance.

    Raises:
        AppManifestValidationError: If validation fails.
    """
    errors: list[str] = []

    # Required string fields
    for field_name in ("manifest_version", "app_id", "display_name", "app_version", "required_module_id"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field_name} must be a non-empty string")

    def validate_optional_string_list(field_name: str) -> list[str]:
        value = data.get(field_name)
        if value is None:
            return []
        if not isinstance(value, list):
            errors.append(f"{field_name} must be a list")
            return []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{field_name}[{i}] must be a non-empty string")
        return value

    compatible_harness_versions = validate_optional_string_list("compatible_harness_versions")
    compatible_api_versions = validate_optional_string_list("compatible_api_versions")
    required_components_data = validate_optional_string_list("required_components")
    requested_permissions_data = validate_optional_string_list("requested_permissions")

    # Optional list fields: required_data_sources
    required_data_sources_data = data.get("required_data_sources")
    data_sources: list[DataSource] = []
    if required_data_sources_data is not None:
        if not isinstance(required_data_sources_data, list):
            errors.append("required_data_sources must be a list")
        else:
            for i, ds in enumerate(required_data_sources_data):
                if not isinstance(ds, dict):
                    errors.append(f"required_data_sources[{i}] must be an object")
                    continue
                source_id = ds.get("source_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    errors.append(f"required_data_sources[{i}].source_id must be a non-empty string")
                ds_display_name = ds.get("display_name", "")
                if not isinstance(ds_display_name, str):
                    errors.append(f"required_data_sources[{i}].display_name must be a string")
                ds_description = ds.get("description", "")
                if not isinstance(ds_description, str):
                    errors.append(f"required_data_sources[{i}].description must be a string")
                data_sources.append(DataSource(
                    source_id=source_id or "",
                    display_name=ds_display_name,
                    description=ds_description,
                ))

    # State validation
    state_value = data.get("state")
    state: AppState = AppState.DRAFT
    if state_value is not None:
        if not isinstance(state_value, str):
            errors.append("state must be a string")
        else:
            try:
                state = AppState(state_value)
            except ValueError:
                valid_states = [s.value for s in AppState]
                errors.append(f"state must be one of {valid_states}, got '{state_value}'")

    # Provenance validation
    provenance_data = data.get("provenance")
    provenance: ProvenanceRequirement = ProvenanceRequirement()
    if provenance_data is not None:
        if not isinstance(provenance_data, dict):
            errors.append("provenance must be an object")
        else:
            require_signed = provenance_data.get("require_signed")
            if require_signed is not None and not isinstance(require_signed, bool):
                errors.append("provenance.require_signed must be a boolean")

            require_audit_log = provenance_data.get("require_audit_log")
            if require_audit_log is not None and not isinstance(require_audit_log, bool):
                errors.append("provenance.require_audit_log must be a boolean")

            allowed_source_types = provenance_data.get("allowed_source_types")
            if allowed_source_types is not None:
                if not isinstance(allowed_source_types, list):
                    errors.append("provenance.allowed_source_types must be a list")
                else:
                    for i, v in enumerate(allowed_source_types):
                        if not isinstance(v, str) or not v.strip():
                            errors.append(f"provenance.allowed_source_types[{i}] must be a non-empty string")

            provenance = ProvenanceRequirement(
                require_signed=bool(require_signed) if isinstance(require_signed, bool) else False,
                require_audit_log=bool(require_audit_log) if isinstance(require_audit_log, bool) else False,
                allowed_source_types=allowed_source_types or [],
            )

    # Raise if errors found
    if errors:
        raise AppManifestValidationError("; ".join(errors))

    # Construct the manifest
    return AppManifest(
        manifest_version=data["manifest_version"],
        app_id=data["app_id"],
        display_name=data["display_name"],
        app_version=data["app_version"],
        required_module_id=data["required_module_id"],
        compatible_harness_versions=compatible_harness_versions,
        compatible_api_versions=compatible_api_versions,
        required_components=required_components_data,
        required_data_sources=data_sources,
        requested_permissions=requested_permissions_data,
        provenance=provenance,
        state=state,
        validation=ValidationResult(is_valid=True),
    )
