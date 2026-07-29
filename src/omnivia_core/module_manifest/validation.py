"""Module Manifest validation."""

# ruff: noqa: PIE790, UP006, UP035 -- canonical port preserves the legacy
# source's `typing.Dict` spelling and `pass` statement verbatim; do not let
# pyupgrade modernize them.

from typing import Any, Dict

from omnivia_core.module_manifest.models import (
    Entrypoint,
    Integrity,
    ModuleKind,
    ModuleManifest,
    Permission,
    PublishedTarget,
)


class ModuleManifestValidationError(Exception):
    """Raised when a Module Manifest fails validation."""

    pass


def validate_module_manifest(data: Dict[str, Any]) -> ModuleManifest:
    """Validate a plain dict and return a ModuleManifest.

    Args:
        data: A dictionary containing manifest data.

    Returns:
        A validated ModuleManifest instance.

    Raises:
        ModuleManifestValidationError: If validation fails.
    """
    errors: list[str] = []

    # Required string fields
    for field_name in ("manifest_version", "module_id", "display_name", "version"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field_name} must be a non-empty string")

    # Module kind
    kind_value = data.get("kind")
    if not isinstance(kind_value, str):
        errors.append("kind must be a string")
    else:
        try:
            kind = ModuleKind(kind_value)
        except ValueError:
            valid_kinds = [k.value for k in ModuleKind]
            errors.append(f"kind must be one of {valid_kinds}, got '{kind_value}'")
            kind = None

    # Required list fields: compatible versions
    for list_field in ("compatible_core_versions", "compatible_platform_versions"):
        value = data.get(list_field)
        if value is None:
            errors.append(f"{list_field} is required")
        elif not isinstance(value, list):
            errors.append(f"{list_field} must be a list")
        elif len(value) == 0:
            errors.append(f"{list_field} must not be empty")
        else:
            for i, v in enumerate(value):
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{list_field}[{i}] must be a non-empty string")

    # Entrypoint validation
    entrypoint_data = data.get("entrypoint")
    if entrypoint_data is None:
        errors.append("entrypoint is required")
    elif not isinstance(entrypoint_data, dict):
        errors.append("entrypoint must be an object")
    else:
        module = entrypoint_data.get("module")
        if not isinstance(module, str) or not module.strip():
            errors.append("entrypoint.module must be a non-empty string")

        class_name = entrypoint_data.get("class_name")
        if class_name is not None:
            if not isinstance(class_name, str):
                errors.append("entrypoint.class_name must be a string")
            elif not class_name.strip():
                errors.append("entrypoint.class_name must be a non-empty string when present")

        config_key = entrypoint_data.get("config_key")
        if config_key is not None and not isinstance(config_key, str):
            errors.append("entrypoint.config_key must be a string")

    # Integrity validation
    integrity_data = data.get("integrity")
    if integrity_data is None:
        errors.append("integrity is required")
    elif not isinstance(integrity_data, dict):
        errors.append("integrity must be an object")
    else:
        algorithm = integrity_data.get("algorithm")
        digest = integrity_data.get("digest")
        signature = integrity_data.get("signature", "")

        if not isinstance(algorithm, str) or not algorithm.strip():
            errors.append("integrity.algorithm must be a non-empty string")
        if not isinstance(digest, str) or not digest.strip():
            errors.append("integrity.digest must be a non-empty string")
        if not isinstance(signature, str):
            errors.append("integrity.signature must be a string")

    # Permissions validation
    permissions_data = data.get("permissions")
    permissions: list[Permission] = []
    if permissions_data is not None:
        if not isinstance(permissions_data, list):
            errors.append("permissions must be a list")
        else:
            for i, perm in enumerate(permissions_data):
                if not isinstance(perm, dict):
                    errors.append(f"permissions[{i}] must be an object")
                    continue
                name = perm.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"permissions[{i}].name must be a non-empty string")
                perm_desc = perm.get("description", "")
                if not isinstance(perm_desc, str):
                    errors.append(f"permissions[{i}].description must be a string")
                permissions.append(Permission(name=name or "", description=perm_desc))

    # Published targets validation (optional, inert metadata)
    published_targets_data = data.get("published_targets")
    published_targets: list[PublishedTarget] = []
    if published_targets_data is not None:
        if not isinstance(published_targets_data, list):
            errors.append("published_targets must be a list")
        else:
            for i, target in enumerate(published_targets_data):
                if not isinstance(target, dict):
                    errors.append(f"published_targets[{i}] must be an object")
                    continue
                target_id = target.get("target_id")
                if not isinstance(target_id, str) or not target_id.strip():
                    errors.append(
                        f"published_targets[{i}].target_id must be a non-empty string"
                    )
                contract_path = target.get("contract_path")
                if not isinstance(contract_path, str) or not contract_path.strip():
                    errors.append(
                        f"published_targets[{i}].contract_path must be a non-empty string"
                    )
                published_targets.append(
                    PublishedTarget(
                        target_id=target_id if isinstance(target_id, str) else "",
                        contract_path=contract_path if isinstance(contract_path, str) else "",
                    )
                )

    # Raise if errors found
    if errors:
        raise ModuleManifestValidationError("; ".join(errors))

    # Construct the manifest
    manifest_version = data["manifest_version"]
    module_id = data["module_id"]
    display_name = data["display_name"]
    version = data["version"]

    entrypoint_obj = Entrypoint(
        module=entrypoint_data["module"],
        class_name=entrypoint_data.get("class_name", "Module"),
        config_key=entrypoint_data.get("config_key", ""),
    ) if entrypoint_data else None
    integrity_obj = Integrity(
        algorithm=integrity_data["algorithm"],
        digest=integrity_data["digest"],
        signature=integrity_data.get("signature", ""),
    ) if integrity_data else None

    return ModuleManifest(
        manifest_version=manifest_version,
        module_id=module_id,
        display_name=display_name,
        kind=kind,  # type: ignore
        version=version,
        compatible_core_versions=data["compatible_core_versions"],
        compatible_platform_versions=data["compatible_platform_versions"],
        entrypoint=entrypoint_obj,  # type: ignore
        permissions=permissions,
        integrity=integrity_obj,  # type: ignore
        published_targets=published_targets,
    )
