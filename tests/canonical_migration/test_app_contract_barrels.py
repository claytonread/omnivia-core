"""``omnivia_core.app_manifest`` and ``omnivia_core.app_shell_bridge`` barrel
and behavioral parity coverage.

``test_parity.py`` proves the app-manifest owner leaves remain exact ports,
while ``test_facade_foundation.py`` proves the app-shell owner leaves are
identity-preserving compatibility facades. ``test_behavioral_parity.py`` also
pins the plain-dataclass defaults for both. This module is concerned with what
those suites leave uncovered: the barrels' own re-export contract (exact
historical ``__all__`` order, owner bindings, star-import surface,
isolated-import module closure and forbidden-import leakage -- the same shape
of coverage
``test_knowledge_barrel.py`` gives the knowledge barrel), and representative
validator behavioral parity beyond bare construction (fail-closed error
paths, exception identity, and non-mutation of caller input).

``baseline.inventory.describe_symbol`` records each barrel's ``__all__``
*sorted*, so ``baseline/inventories/public-exports.json`` is a membership /
definition oracle for these barrels, not an order oracle. The historical
source-preserving order -- alphabetical for ``app_manifest``, deliberately
unsorted for ``app_shell_bridge`` -- is frozen explicitly below instead, and
compared against both trees' live ``__all__`` verbatim.
"""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PUBLIC_EXPORTS_INVENTORY = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

with PUBLIC_EXPORTS_INVENTORY.open(encoding="utf-8") as _inventory_file:
    FROZEN_MODULES = json.load(_inventory_file)["modules"]

# ---------------------------------------------------------------------------
# Frozen historical ordered __all__ tuples (source-preserving, not sorted).
# ---------------------------------------------------------------------------

APP_MANIFEST_ALL: tuple[str, ...] = (
    "AppManifest",
    "AppManifestValidationError",
    "AppState",
    "DataSource",
    "ProvenanceRequirement",
    "ValidationResult",
    "validate_app_manifest",
)

APP_SHELL_BRIDGE_ALL: tuple[str, ...] = (
    "AppShellRuntimeState",
    "AppShellSource",
    "ValidationResult",
    "AppShellHostContext",
    "AppShellBodyDescriptor",
    "AppShellBridgeValidationError",
    "validate_app_shell_host_context",
    "validate_app_shell_body_descriptor",
)

#: Explicit frozen canonical owner map: every barrel export bound to the
#: exact leaf module that defines it.
APP_MANIFEST_OWNER_BY_EXPORT: dict[str, str] = {
    "AppManifest": "omnivia_core.app_manifest.models",
    "AppManifestValidationError": "omnivia_core.app_manifest.validation",
    "AppState": "omnivia_core.app_manifest.models",
    "DataSource": "omnivia_core.app_manifest.models",
    "ProvenanceRequirement": "omnivia_core.app_manifest.models",
    "ValidationResult": "omnivia_core.app_manifest.models",
    "validate_app_manifest": "omnivia_core.app_manifest.validation",
}

APP_SHELL_BRIDGE_OWNER_BY_EXPORT: dict[str, str] = {
    "AppShellRuntimeState": "omnivia_core.app_shell_bridge.models",
    "AppShellSource": "omnivia_core.app_shell_bridge.models",
    "ValidationResult": "omnivia_core.app_shell_bridge.models",
    "AppShellHostContext": "omnivia_core.app_shell_bridge.models",
    "AppShellBodyDescriptor": "omnivia_core.app_shell_bridge.models",
    "AppShellBridgeValidationError": "omnivia_core.app_shell_bridge.validation",
    "validate_app_shell_host_context": "omnivia_core.app_shell_bridge.validation",
    "validate_app_shell_body_descriptor": "omnivia_core.app_shell_bridge.validation",
}

#: Modules a canonical app-contract leaf/barrel import must never pull in,
#: covering: the legacy runtime package; SQLAlchemy/SQLite; Core's own
#: runtime/CLI/MCP sibling distributions; the Platform/Dev/Cloud/Apps/Pro
#: Modules; HTTP frameworks and clients; filesystem, network, credential
#: store, and process packages. None of these are ever legitimately reached
#: from a pure dataclass/enum + stdlib-typing leaf.
FORBIDDEN_LEAKAGE_MODULES: frozenset[str] = frozenset(
    {
        "omnivia_memory",
        "sqlalchemy",
        "sqlite3",
        "omnivia_core_runtime",
        "omnivia_core_mcp",
        "omnivia_core_cli",
        "omnivia_platform",
        "omnivia_dev",
        "omnivia_cloud",
        "omnivia_apps",
        "omnivia_pro",
        "fastapi",
        "starlette",
        "httpx",
        "requests",
        "urllib3",
        "aiohttp",
        "http",
        "shutil",
        "tempfile",
        "socket",
        "ssl",
        "keyring",
        "subprocess",
        "multiprocessing",
    }
)

BARRELS: tuple[dict[str, Any], ...] = (
    {
        "id": "app_manifest",
        "canonical": "omnivia_core.app_manifest",
        "legacy": "omnivia_memory.app_manifest",
        "frozen_all": APP_MANIFEST_ALL,
        "owner_by_export": APP_MANIFEST_OWNER_BY_EXPORT,
        "owner_order": (
            "omnivia_core.app_manifest.models",
            "omnivia_core.app_manifest.validation",
            "omnivia_core.app_manifest",
        ),
        "reverse_owner_order": (
            "omnivia_core.app_manifest.validation",
            "omnivia_core.app_manifest.models",
            "omnivia_core.app_manifest",
        ),
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.app_manifest",
                "omnivia_core.app_manifest.models",
                "omnivia_core.app_manifest.validation",
            }
        ),
    },
    {
        "id": "app_shell_bridge",
        "canonical": "omnivia_core.app_shell_bridge",
        "legacy": "omnivia_memory.app_shell_bridge",
        "frozen_all": APP_SHELL_BRIDGE_ALL,
        "owner_by_export": APP_SHELL_BRIDGE_OWNER_BY_EXPORT,
        "owner_order": (
            "omnivia_core.app_shell_bridge.models",
            "omnivia_core.app_shell_bridge.validation",
            "omnivia_core.app_shell_bridge",
        ),
        "reverse_owner_order": (
            "omnivia_core.app_shell_bridge.validation",
            "omnivia_core.app_shell_bridge.models",
            "omnivia_core.app_shell_bridge",
        ),
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.app_shell_bridge",
                "omnivia_core.app_shell_bridge.models",
                "omnivia_core.app_shell_bridge.validation",
            }
        ),
    },
)


def _barrel_id(barrel: dict[str, Any]) -> str:
    return str(barrel["id"])


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_matches_frozen_historical_tuple_exactly(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    legacy = importlib.import_module(barrel["legacy"])
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert tuple(canonical.__all__) == frozen_all, (
        f"{barrel['canonical']}.__all__ drifted from the frozen historical order:\n"
        f"canonical: {list(canonical.__all__)}\nfrozen: {list(frozen_all)}"
    )
    assert tuple(legacy.__all__) == frozen_all, (
        f"live {barrel['legacy']}.__all__ drifted from the frozen historical order:\n"
        f"legacy: {list(legacy.__all__)}\nfrozen: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_membership_matches_inventory_without_being_an_order_oracle(
    barrel: dict[str, Any],
) -> None:
    """The frozen inventory sorts ``__all__``, so it can only be trusted for
    *membership*, never order -- confirm the inventory really is sorted (so a
    future reader does not mistake it for an order oracle) and that its
    membership matches the explicit historical tuple's."""
    inventory_all: list[str] = FROZEN_MODULES[barrel["legacy"]]["all"]
    assert inventory_all == sorted(inventory_all), (
        f"{barrel['legacy']} inventory 'all' is expected to be sorted by "
        "baseline.inventory; if this fails, the oracle's sorting behavior changed"
    )
    assert set(barrel["frozen_all"]) == set(inventory_all), (
        f"{barrel['canonical']} historical __all__ membership does not match the "
        f"frozen inventory: only in historical={sorted(set(barrel['frozen_all']) - set(inventory_all))}, "
        f"only in inventory={sorted(set(inventory_all) - set(barrel['frozen_all']))}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_has_no_duplicates(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    legacy = importlib.import_module(barrel["legacy"])
    assert len(canonical.__all__) == len(set(canonical.__all__)), (
        f"{barrel['canonical']}.__all__ contains duplicates: {canonical.__all__}"
    )
    assert len(legacy.__all__) == len(set(legacy.__all__)), (
        f"{barrel['legacy']}.__all__ contains duplicates: {legacy.__all__}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_owner_map_covers_every_export_exactly(barrel: dict[str, Any]) -> None:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    assert set(owner_by_export) == set(barrel["frozen_all"]), (
        f"{barrel['id']} owner map does not cover the complete barrel: "
        f"missing={sorted(set(barrel['frozen_all']) - set(owner_by_export))}, "
        f"extra={sorted(set(owner_by_export) - set(barrel['frozen_all']))}"
    )
    for name, canonical_owner in owner_by_export.items():
        legacy_owner = canonical_owner.replace("omnivia_core", "omnivia_memory", 1)
        assert name in FROZEN_MODULES[legacy_owner]["defines"], (
            f"{name} is mapped to {canonical_owner}, but the frozen Phase 0 "
            f"inventory does not record {legacy_owner} as its defining leaf"
        )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_bindings_match_frozen_owner_map_by_identity(barrel: dict[str, Any]) -> None:
    """Every advertised name is the exact same object as its frozen owner
    binding, in both the canonical and the live legacy tree."""
    canonical = importlib.import_module(barrel["canonical"])
    legacy = importlib.import_module(barrel["legacy"])
    owner_by_export: dict[str, str] = barrel["owner_by_export"]

    for name in barrel["frozen_all"]:
        canonical_owner_name = owner_by_export[name]
        canonical_owner = importlib.import_module(canonical_owner_name)
        assert getattr(canonical, name) is getattr(canonical_owner, name), (
            f"{barrel['canonical']}.{name} is not identical to its frozen owner "
            f"{canonical_owner_name}.{name}"
        )

        legacy_owner_name = canonical_owner_name.replace("omnivia_core", "omnivia_memory", 1)
        legacy_owner = importlib.import_module(legacy_owner_name)
        assert getattr(legacy, name) is getattr(legacy_owner, name), (
            f"{barrel['legacy']}.{name} is not identical to its live owner "
            f"{legacy_owner_name}.{name}"
        )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_star_import_exposes_exactly_advertised_names(barrel: dict[str, Any]) -> None:
    namespace: dict[str, object] = {}
    exec(f"from {barrel['canonical']} import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(barrel["frozen_all"]), (
        f"star import of {barrel['canonical']} exposed {sorted(exported)}, "
        f"expected exactly {sorted(barrel['frozen_all'])}"
    )


def _isolated_barrel_import_script(
    import_order: tuple[str, ...],
    expected_closure: frozenset[str],
) -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            "import importlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {import_order!r}:",
            "    importlib.import_module(module_name)",
            f"core_src = pathlib.Path({str(CORE_SRC)!r}).resolve()",
            "outside = []",
            "for name, module in sorted(sys.modules.items()):",
            '    if name != "omnivia_core" and not name.startswith("omnivia_core."):',
            "        continue",
            '    path = getattr(module, "__file__", None)',
            "    if path is None:",
            "        continue",
            "    resolved = pathlib.Path(path).resolve()",
            "    try:",
            "        resolved.relative_to(core_src)",
            "    except ValueError:",
            '        outside.append(f"{name} -> {resolved}")',
            "if outside:",
            '    raise SystemExit("omnivia_core modules loaded from outside src: " + ", ".join(outside))',
            f"expected_core = {expected_closure!r}",
            'loaded_core = {name for name in sys.modules if name == "omnivia_core" or name.startswith("omnivia_core.")}',
            "if loaded_core != expected_core:",
            '    raise SystemExit(f"unexpected canonical module closure: {sorted(loaded_core)}")',
            f"forbidden = {FORBIDDEN_LEAKAGE_MODULES!r}",
            'leaked = sorted(forbidden & {m.split(".")[0] for m in sys.modules})',
            "if leaked:",
            '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
            'print("OK")',
        ]
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
@pytest.mark.parametrize(
    "order_key", ("barrel-first", "owner-first", "reverse-owner-order"), ids=lambda v: v
)
def test_barrel_imports_in_isolation_from_src_alone(
    barrel: dict[str, Any], order_key: str
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    import_order: tuple[str, ...] = {
        "barrel-first": (barrel["canonical"],),
        "owner-first": barrel["owner_order"],
        "reverse-owner-order": barrel["reverse_owner_order"],
    }[order_key]

    result = subprocess.run(
        [
            PYTHON,
            "-I",
            "-S",
            "-c",
            _isolated_barrel_import_script(import_order, barrel["expected_closure"]),
        ],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of {barrel['canonical']} ({order_key}) failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# Behavioral parity: validators, not just shape.
#
# Every case below deep-copies its payload before handing it to either
# engine, both so one engine's call can never see the other's mutation and
# so the original payload survives as an independent oracle for the
# non-mutation assertions.
# ---------------------------------------------------------------------------


def _pair(canonical_name: str) -> tuple[Any, Any]:
    legacy_name = canonical_name.replace("omnivia_core", "omnivia_memory", 1)
    return importlib.import_module(canonical_name), importlib.import_module(legacy_name)


def _assert_same_validation_error(
    canonical_exc: BaseException, legacy_exc: BaseException, expected_class_name: str
) -> None:
    assert type(canonical_exc).__name__ == type(legacy_exc).__name__ == expected_class_name
    assert canonical_exc.args == legacy_exc.args
    assert str(canonical_exc) == str(legacy_exc)


def _apply_case(
    base: dict[str, Any], overrides: dict[str, Any], deletes: tuple[str, ...]
) -> dict[str, Any]:
    payload = copy.deepcopy(base)
    payload.update(copy.deepcopy(overrides))
    for key in deletes:
        payload.pop(key, None)
    return payload


# ---------------------------------------------------------------------------
# app_manifest.validate_app_manifest
# ---------------------------------------------------------------------------

APP_MANIFEST_VALID_PAYLOAD: dict[str, Any] = {
    "manifest_version": "1.0",
    "app_id": "com.acme.expense-app",
    "display_name": "Expense App",
    "app_version": "2.1.0",
    "required_module_id": "com.acme.finance-module",
    "compatible_harness_versions": ["1.x", "2.x"],
    "compatible_api_versions": ["v1"],
    "required_components": ["comp.table", "comp.chart"],
    "required_data_sources": [
        {"source_id": "ds-1", "display_name": "Ledger", "description": "General ledger"},
    ],
    "requested_permissions": ["read:transactions"],
    "provenance": {
        "require_signed": True,
        "require_audit_log": True,
        "allowed_source_types": ["file", "api"],
    },
    "state": "active",
}


def _app_manifest_plain(manifest: Any) -> dict[str, Any]:
    return {
        "manifest_version": manifest.manifest_version,
        "app_id": manifest.app_id,
        "display_name": manifest.display_name,
        "app_version": manifest.app_version,
        "required_module_id": manifest.required_module_id,
        "compatible_harness_versions": list(manifest.compatible_harness_versions),
        "compatible_api_versions": list(manifest.compatible_api_versions),
        "required_components": list(manifest.required_components),
        "required_data_sources": [
            (ds.source_id, ds.display_name, ds.description) for ds in manifest.required_data_sources
        ],
        "requested_permissions": list(manifest.requested_permissions),
        "provenance": (
            manifest.provenance.require_signed,
            manifest.provenance.require_audit_log,
            list(manifest.provenance.allowed_source_types),
        ),
        "state": manifest.state.value,
        "validation_is_valid": manifest.validation.is_valid,
    }


def test_app_manifest_valid_payload_produces_equivalent_normalized_result() -> None:
    canonical, legacy = _pair("omnivia_core.app_manifest")
    canonical_result = canonical.validate_app_manifest(copy.deepcopy(APP_MANIFEST_VALID_PAYLOAD))
    legacy_result = legacy.validate_app_manifest(copy.deepcopy(APP_MANIFEST_VALID_PAYLOAD))
    assert _app_manifest_plain(canonical_result) == _app_manifest_plain(legacy_result)
    assert canonical_result.state is canonical.AppState.ACTIVE
    assert legacy_result.state is legacy.AppState.ACTIVE


def test_app_manifest_validate_does_not_mutate_input() -> None:
    canonical, legacy = _pair("omnivia_core.app_manifest")
    before = copy.deepcopy(APP_MANIFEST_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(APP_MANIFEST_VALID_PAYLOAD)
    canonical.validate_app_manifest(canonical_payload)
    assert canonical_payload == before

    legacy_payload = copy.deepcopy(APP_MANIFEST_VALID_PAYLOAD)
    legacy.validate_app_manifest(legacy_payload)
    assert legacy_payload == before


APP_MANIFEST_FAIL_CLOSED_CASES: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
    ("missing_app_id", {}, ("app_id",)),
    ("empty_display_name", {"display_name": "   "}, ()),
    ("wrong_type_app_version", {"app_version": 123}, ()),
    ("invalid_state_enum", {"state": "not-a-state"}, ()),
    ("compatible_harness_versions_not_list", {"compatible_harness_versions": "1.x"}, ()),
    ("compatible_harness_versions_item_not_string", {"compatible_harness_versions": [1, 2]}, ()),
    ("required_data_sources_not_list", {"required_data_sources": "oops"}, ()),
    ("required_data_sources_item_not_object", {"required_data_sources": ["oops"]}, ()),
    (
        "required_data_sources_missing_source_id",
        {"required_data_sources": [{"display_name": "x"}]},
        (),
    ),
    ("provenance_not_object", {"provenance": "oops"}, ()),
    ("provenance_require_signed_wrong_type", {"provenance": {"require_signed": "yes"}}, ()),
    (
        "provenance_allowed_source_types_not_list",
        {"provenance": {"allowed_source_types": "file"}},
        (),
    ),
)


@pytest.mark.parametrize(
    "case_id,overrides,deletes", APP_MANIFEST_FAIL_CLOSED_CASES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_app_manifest_fail_closed_cases_match(
    case_id: str, overrides: dict[str, Any], deletes: tuple[str, ...]
) -> None:
    canonical, legacy = _pair("omnivia_core.app_manifest")
    payload = _apply_case(APP_MANIFEST_VALID_PAYLOAD, overrides, deletes)

    with pytest.raises(canonical.AppManifestValidationError) as canonical_exc:
        canonical.validate_app_manifest(copy.deepcopy(payload))
    with pytest.raises(legacy.AppManifestValidationError) as legacy_exc:
        legacy.validate_app_manifest(copy.deepcopy(payload))

    _assert_same_validation_error(
        canonical_exc.value, legacy_exc.value, "AppManifestValidationError"
    )
    assert case_id  # sanity: parametrize id is meaningful, not empty


# ---------------------------------------------------------------------------
# app_shell_bridge.validate_app_shell_host_context
# ---------------------------------------------------------------------------

APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD: dict[str, Any] = {
    "app_id": "app-1",
    "app_name": "App One",
    "entity_label": "Q3 Report",
    "runtime_state": "ready",
    "permissions_summary": "Read-only access",
    "sources": [{"name": "Salesforce", "description": "CRM data"}],
    "last_updated": "2024-01-01T00:00:00Z",
    "host_command_ids": ["cmd.refresh", "cmd.export"],
}


def _app_shell_host_context_plain(context: Any) -> dict[str, Any]:
    return {
        "app_id": context.app_id,
        "app_name": context.app_name,
        "entity_label": context.entity_label,
        "runtime_state": context.runtime_state.value,
        "permissions_summary": context.permissions_summary,
        "sources": [(s.name, s.description) for s in context.sources],
        "last_updated": context.last_updated,
        "host_command_ids": list(context.host_command_ids),
        "validation_is_valid": context.validation.is_valid,
    }


def test_app_shell_host_context_valid_payload_produces_equivalent_normalized_result() -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    canonical_result = canonical.validate_app_shell_host_context(
        copy.deepcopy(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD)
    )
    legacy_result = legacy.validate_app_shell_host_context(
        copy.deepcopy(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD)
    )
    assert _app_shell_host_context_plain(canonical_result) == _app_shell_host_context_plain(legacy_result)
    assert canonical_result.runtime_state is canonical.AppShellRuntimeState.READY
    assert legacy_result.runtime_state is legacy.AppShellRuntimeState.READY


def test_app_shell_host_context_validate_does_not_mutate_input() -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    before = copy.deepcopy(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD)
    canonical.validate_app_shell_host_context(canonical_payload)
    assert canonical_payload == before

    legacy_payload = copy.deepcopy(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD)
    legacy.validate_app_shell_host_context(legacy_payload)
    assert legacy_payload == before


APP_SHELL_HOST_CONTEXT_FAIL_CLOSED_CASES: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
    ("missing_app_id", {}, ("app_id",)),
    ("empty_app_name", {"app_name": ""}, ()),
    ("wrong_type_entity_label", {"entity_label": 42}, ()),
    ("missing_runtime_state", {}, ("runtime_state",)),
    ("invalid_runtime_state_value", {"runtime_state": "not-a-state"}, ()),
    ("wrong_type_permissions_summary", {"permissions_summary": 7}, ()),
    ("sources_not_list", {"sources": "oops"}, ()),
    ("sources_item_not_dict", {"sources": ["oops"]}, ()),
    ("sources_item_missing_name", {"sources": [{"description": "x"}]}, ()),
    ("host_command_ids_item_wrong_type", {"host_command_ids": [1, 2]}, ()),
)


@pytest.mark.parametrize(
    "case_id,overrides,deletes",
    APP_SHELL_HOST_CONTEXT_FAIL_CLOSED_CASES,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_app_shell_host_context_fail_closed_cases_match(
    case_id: str, overrides: dict[str, Any], deletes: tuple[str, ...]
) -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    payload = _apply_case(APP_SHELL_HOST_CONTEXT_VALID_PAYLOAD, overrides, deletes)

    with pytest.raises(canonical.AppShellBridgeValidationError) as canonical_exc:
        canonical.validate_app_shell_host_context(copy.deepcopy(payload))
    with pytest.raises(legacy.AppShellBridgeValidationError) as legacy_exc:
        legacy.validate_app_shell_host_context(copy.deepcopy(payload))

    _assert_same_validation_error(
        canonical_exc.value, legacy_exc.value, "AppShellBridgeValidationError"
    )
    assert case_id


# ---------------------------------------------------------------------------
# app_shell_bridge.validate_app_shell_body_descriptor
# ---------------------------------------------------------------------------

APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD: dict[str, Any] = {
    "app_id": "app-1",
    "body_id": "body-1",
    "sources": [{"name": "Salesforce", "description": "CRM data"}],
    "components": ["comp.table", "comp.chart"],
    "citations": ["cite-1"],
    "degraded_component_ids": ["comp.table"],
    "source_count": 3,
}


def _app_shell_body_descriptor_plain(descriptor: Any) -> dict[str, Any]:
    return {
        "app_id": descriptor.app_id,
        "body_id": descriptor.body_id,
        "source_count": descriptor.source_count,
        "sources": [(s.name, s.description) for s in descriptor.sources],
        "components": list(descriptor.components),
        "citations": list(descriptor.citations),
        "degraded_component_ids": list(descriptor.degraded_component_ids),
        "validation_is_valid": descriptor.validation.is_valid,
    }


def test_app_shell_body_descriptor_valid_payload_produces_equivalent_normalized_result() -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    canonical_result = canonical.validate_app_shell_body_descriptor(
        copy.deepcopy(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD)
    )
    legacy_result = legacy.validate_app_shell_body_descriptor(
        copy.deepcopy(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD)
    )
    assert _app_shell_body_descriptor_plain(canonical_result) == _app_shell_body_descriptor_plain(
        legacy_result
    )


def test_app_shell_body_descriptor_validate_does_not_mutate_input() -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    before = copy.deepcopy(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD)
    canonical.validate_app_shell_body_descriptor(canonical_payload)
    assert canonical_payload == before

    legacy_payload = copy.deepcopy(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD)
    legacy.validate_app_shell_body_descriptor(legacy_payload)
    assert legacy_payload == before


APP_SHELL_BODY_DESCRIPTOR_FAIL_CLOSED_CASES: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
    ("missing_app_id", {}, ("app_id",)),
    ("missing_body_id", {}, ("body_id",)),
    ("source_count_wrong_type_bool", {"source_count": True}, ()),
    ("source_count_wrong_type_str", {"source_count": "3"}, ()),
    ("source_count_negative", {"source_count": -1}, ()),
    ("degraded_component_ids_not_subset", {"degraded_component_ids": ["comp.unknown"]}, ()),
    ("components_item_wrong_type", {"components": [1, 2]}, ()),
    ("sources_item_not_dict", {"sources": ["oops"]}, ()),
)


@pytest.mark.parametrize(
    "case_id,overrides,deletes",
    APP_SHELL_BODY_DESCRIPTOR_FAIL_CLOSED_CASES,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_app_shell_body_descriptor_fail_closed_cases_match(
    case_id: str, overrides: dict[str, Any], deletes: tuple[str, ...]
) -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge")
    payload = _apply_case(APP_SHELL_BODY_DESCRIPTOR_VALID_PAYLOAD, overrides, deletes)

    with pytest.raises(canonical.AppShellBridgeValidationError) as canonical_exc:
        canonical.validate_app_shell_body_descriptor(copy.deepcopy(payload))
    with pytest.raises(legacy.AppShellBridgeValidationError) as legacy_exc:
        legacy.validate_app_shell_body_descriptor(copy.deepcopy(payload))

    _assert_same_validation_error(
        canonical_exc.value, legacy_exc.value, "AppShellBridgeValidationError"
    )
    assert case_id
