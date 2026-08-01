"""Tests for the Module Manifest contract."""

import pytest

from omnivia_memory.module_manifest import (
    Entrypoint,
    Integrity,
    ModuleKind,
    ModuleManifest,
    ModuleManifestValidationError,
    Permission,
    PublishedTarget,
    validate_module_manifest,
)


# ---------------------------------------------------------------------------
# Valid manifest helpers
# ---------------------------------------------------------------------------

def _minimal_dict(kind: str = "apps") -> dict:
    """Return a minimal valid manifest dict."""
    return {
        "manifest_version": "1.0.0",
        "module_id": "com.omnivia.apps",
        "display_name": "OmniVia Apps",
        "kind": kind,
        "version": "1.0.0",
        "compatible_core_versions": [">=0.1.0"],
        "compatible_platform_versions": [">=1.0.0"],
        "entrypoint": {"module": "omnivia_apps"},
        "integrity": {
            "algorithm": "sha256",
            "digest": "abc123",
            "signature": "",
        },
        "permissions": [],
    }


# ---------------------------------------------------------------------------
# Valid manifest tests
# ---------------------------------------------------------------------------

def test_validate_apps_manifest():
    """A valid Apps manifest returns a populated ModuleManifest."""
    data = _minimal_dict("apps")
    manifest = validate_module_manifest(data)

    assert isinstance(manifest, ModuleManifest)
    assert manifest.manifest_version == "1.0.0"
    assert manifest.module_id == "com.omnivia.apps"
    assert manifest.display_name == "OmniVia Apps"
    assert manifest.kind == ModuleKind.APPS
    assert manifest.version == "1.0.0"
    assert manifest.compatible_core_versions == [">=0.1.0"]
    assert manifest.compatible_platform_versions == [">=1.0.0"]
    assert manifest.entrypoint.module == "omnivia_apps"
    assert manifest.integrity.algorithm == "sha256"
    assert manifest.integrity.digest == "abc123"
    assert manifest.permissions == []


def test_validate_dev_manifest():
    """A valid Dev manifest returns a ModuleManifest with correct kind."""
    data = _minimal_dict("dev")
    data["module_id"] = "com.omnivia.dev"
    data["display_name"] = "OmniVia Dev"

    manifest = validate_module_manifest(data)

    assert manifest.kind == ModuleKind.DEV


def test_validate_pro_manifest():
    """A valid Pro manifest returns a ModuleManifest with correct kind."""
    data = _minimal_dict("pro")
    data["module_id"] = "com.omnivia.pro"
    data["display_name"] = "OmniVia Pro"

    manifest = validate_module_manifest(data)

    assert manifest.kind == ModuleKind.PRO


def test_validate_with_permissions():
    """A manifest with permissions list parses correctly."""
    data = _minimal_dict()
    data["permissions"] = [
        {"name": "workspace.read", "description": "Read workspace files"},
        {"name": "workspace.write", "description": ""},
    ]

    manifest = validate_module_manifest(data)

    assert len(manifest.permissions) == 2
    assert manifest.permissions[0].name == "workspace.read"
    assert manifest.permissions[0].description == "Read workspace files"
    assert manifest.permissions[1].name == "workspace.write"


def test_validate_with_signature():
    """A manifest with a non-empty signature parses correctly."""
    data = _minimal_dict()
    data["integrity"]["signature"] = "sig_xyz789"

    manifest = validate_module_manifest(data)

    assert manifest.integrity.signature == "sig_xyz789"


def test_validate_entrypoint_with_class_name_and_config_key():
    """A manifest with optional entrypoint fields parses correctly."""
    data = _minimal_dict()
    data["entrypoint"] = {
        "module": "omnivia_apps",
        "class_name": "CustomModule",
        "config_key": "custom_config",
    }

    manifest = validate_module_manifest(data)

    assert manifest.entrypoint.module == "omnivia_apps"
    assert manifest.entrypoint.class_name == "CustomModule"
    assert manifest.entrypoint.config_key == "custom_config"


# ---------------------------------------------------------------------------
# Compatible versions required tests
# ---------------------------------------------------------------------------

def test_rejects_missing_compatible_core_versions():
    """Missing compatible_core_versions raises a clear error."""
    data = _minimal_dict()
    del data["compatible_core_versions"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_core_versions" in str(exc_info.value).lower()


def test_rejects_missing_compatible_platform_versions():
    """Missing compatible_platform_versions raises a clear error."""
    data = _minimal_dict()
    del data["compatible_platform_versions"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_platform_versions" in str(exc_info.value).lower()


def test_rejects_empty_compatible_core_versions():
    """Empty compatible_core_versions raises a clear error."""
    data = _minimal_dict()
    data["compatible_core_versions"] = []

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_core_versions" in str(exc_info.value).lower()


def test_rejects_empty_compatible_platform_versions():
    """Empty compatible_platform_versions raises a clear error."""
    data = _minimal_dict()
    data["compatible_platform_versions"] = []

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_platform_versions" in str(exc_info.value).lower()


def test_rejects_non_list_compatible_versions():
    """Non-list compatible versions raises a clear error."""
    data = _minimal_dict()
    data["compatible_core_versions"] = ">=0.1.0"

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_core_versions" in str(exc_info.value).lower()


def test_rejects_non_string_in_compatible_versions():
    """Non-string item in compatible versions raises a clear error."""
    data = _minimal_dict()
    data["compatible_core_versions"] = [">=0.1.0", 42]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_core_versions" in str(exc_info.value).lower()


def test_rejects_empty_string_in_compatible_versions():
    """Empty string in compatible versions raises a clear error."""
    data = _minimal_dict()
    data["compatible_core_versions"] = [">=0.1.0", ""]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "compatible_core_versions" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Entrypoint optional fields tests
# ---------------------------------------------------------------------------

def test_rejects_entrypoint_class_name_not_string():
    """entrypoint.class_name must be a string when present."""
    data = _minimal_dict()
    data["entrypoint"]["class_name"] = 42

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "entrypoint.class_name" in str(exc_info.value).lower()


def test_rejects_entrypoint_class_name_empty():
    """entrypoint.class_name must be non-empty when present."""
    data = _minimal_dict()
    data["entrypoint"]["class_name"] = ""

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "entrypoint.class_name" in str(exc_info.value).lower()


def test_rejects_entrypoint_config_key_not_string():
    """entrypoint.config_key must be a string when present."""
    data = _minimal_dict()
    data["entrypoint"]["config_key"] = 42

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "entrypoint.config_key" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Invalid manifest tests
# ---------------------------------------------------------------------------

def test_rejects_missing_manifest_version():
    """Missing manifest_version raises a clear error."""
    data = _minimal_dict()
    del data["manifest_version"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "manifest_version" in str(exc_info.value).lower()


def test_rejects_empty_manifest_version():
    """Empty manifest_version raises a clear error."""
    data = _minimal_dict()
    data["manifest_version"] = "   "

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "manifest_version" in str(exc_info.value).lower()


def test_rejects_missing_module_id():
    """Missing module_id raises a clear error."""
    data = _minimal_dict()
    del data["module_id"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "module_id" in str(exc_info.value).lower()


def test_rejects_empty_display_name():
    """Empty display_name raises a clear error."""
    data = _minimal_dict()
    data["display_name"] = ""

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "display_name" in str(exc_info.value).lower()


def test_rejects_missing_kind():
    """Missing kind raises a clear error."""
    data = _minimal_dict()
    del data["kind"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "kind" in str(exc_info.value).lower()


def test_rejects_invalid_kind():
    """Unknown kind value raises a clear error."""
    data = _minimal_dict()
    data["kind"] = "premium"

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "kind" in str(exc_info.value).lower()
    assert "premium" in str(exc_info.value)


def test_rejects_missing_entrypoint():
    """Missing entrypoint raises a clear error."""
    data = _minimal_dict()
    del data["entrypoint"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "entrypoint" in str(exc_info.value).lower()


def test_rejects_empty_entrypoint_module():
    """Empty entrypoint.module raises a clear error."""
    data = _minimal_dict()
    data["entrypoint"] = {"module": ""}

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "entrypoint.module" in str(exc_info.value).lower()


def test_rejects_missing_integrity():
    """Missing integrity raises a clear error."""
    data = _minimal_dict()
    del data["integrity"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "integrity" in str(exc_info.value).lower()


def test_rejects_empty_integrity_digest():
    """Empty integrity.digest raises a clear error."""
    data = _minimal_dict()
    data["integrity"]["digest"] = ""

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "integrity.digest" in str(exc_info.value).lower()


def test_rejects_missing_integrity_algorithm():
    """Missing integrity.algorithm raises a clear error."""
    data = _minimal_dict()
    del data["integrity"]["algorithm"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "integrity.algorithm" in str(exc_info.value).lower()


def test_rejects_invalid_permissions_type():
    """Non-list permissions raises a clear error."""
    data = _minimal_dict()
    data["permissions"] = "not a list"

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "permissions" in str(exc_info.value).lower()


def test_rejects_permission_with_empty_name():
    """Permission with empty name raises a clear error."""
    data = _minimal_dict()
    data["permissions"] = [{"name": "", "description": "desc"}]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "permissions[0].name" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Published targets tests
# ---------------------------------------------------------------------------

def test_published_targets_default_empty():
    """A manifest without published_targets defaults to an empty list."""
    data = _minimal_dict()

    manifest = validate_module_manifest(data)

    assert manifest.published_targets == []


def test_validate_with_published_targets():
    """A manifest with valid published_targets parses correctly."""
    data = _minimal_dict("dev")
    data["module_id"] = "com.omnivia.dev"
    data["display_name"] = "OmniVia Dev"
    data["published_targets"] = [
        {"target_id": "launch.web", "contract_path": "targets/web.json"},
        {"target_id": "launch.cli", "contract_path": "targets/cli.json"},
    ]

    manifest = validate_module_manifest(data)

    assert len(manifest.published_targets) == 2
    assert isinstance(manifest.published_targets[0], PublishedTarget)
    assert manifest.published_targets[0].target_id == "launch.web"
    assert manifest.published_targets[0].contract_path == "targets/web.json"
    assert manifest.published_targets[1].target_id == "launch.cli"
    assert manifest.published_targets[1].contract_path == "targets/cli.json"


def test_rejects_non_list_published_targets():
    """Non-list published_targets raises a clear error."""
    data = _minimal_dict()
    data["published_targets"] = "not a list"

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "published_targets" in str(exc_info.value).lower()


def test_rejects_published_target_not_object():
    """A non-object published_targets entry raises a clear error."""
    data = _minimal_dict()
    data["published_targets"] = ["not an object"]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "published_targets[0]" in str(exc_info.value).lower()


def test_rejects_published_target_empty_target_id():
    """A published target with an empty target_id raises a clear error."""
    data = _minimal_dict()
    data["published_targets"] = [
        {"target_id": "", "contract_path": "targets/web.json"}
    ]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "published_targets[0].target_id" in str(exc_info.value).lower()


def test_rejects_published_target_missing_contract_path():
    """A published target missing contract_path raises a clear error."""
    data = _minimal_dict()
    data["published_targets"] = [{"target_id": "launch.web"}]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "published_targets[0].contract_path" in str(exc_info.value).lower()


def test_rejects_published_target_non_string_contract_path():
    """A published target with a non-string contract_path raises a clear error."""
    data = _minimal_dict()
    data["published_targets"] = [
        {"target_id": "launch.web", "contract_path": 42}
    ]

    with pytest.raises(ModuleManifestValidationError) as exc_info:
        validate_module_manifest(data)
    assert "published_targets[0].contract_path" in str(exc_info.value).lower()


def test_published_target_model():
    """PublishedTarget model holds target_id and contract_path."""
    t = PublishedTarget(target_id="launch.web", contract_path="targets/web.json")
    assert t.target_id == "launch.web"
    assert t.contract_path == "targets/web.json"


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

def test_module_kind_enum_values():
    """ModuleKind enum has correct string values."""
    assert ModuleKind.APPS.value == "apps"
    assert ModuleKind.DEV.value == "dev"
    assert ModuleKind.PRO.value == "pro"


def test_entypoint_default_class_name():
    """Entrypoint defaults to class_name 'Module'."""
    e = Entrypoint(module="test_module")
    assert e.class_name == "Module"


def test_integrity_default_signature():
    """Integrity defaults to empty signature."""
    i = Integrity(algorithm="sha256", digest="abc")
    assert i.signature == ""


def test_module_manifest_dataclass():
    """ModuleManifest is a mutable dataclass with all fields."""
    manifest = ModuleManifest(
        manifest_version="1.0.0",
        module_id="com.omnivia.apps",
        display_name="Apps",
        kind=ModuleKind.APPS,
        version="1.0.0",
    )
    assert manifest.compatible_core_versions == []
    assert manifest.compatible_platform_versions == []
    assert isinstance(manifest.entrypoint, Entrypoint)
    assert isinstance(manifest.integrity, Integrity)
    assert manifest.permissions == []


def test_permission_model():
    """Permission model holds name and optional description."""
    p = Permission(name="workspace.read")
    assert p.name == "workspace.read"
    assert p.description == ""