"""``omnivia_core.component_contract`` and ``omnivia_core.module_manifest``
barrel acceptance coverage.

``test_parity.py`` already proves each owner leaf (``component_contract.models``,
``component_contract.validation``, ``module_manifest.models``,
``module_manifest.validation``) is an exact port of its legacy counterpart,
and the legacy suites in ``services/omnivia-memory/tests`` already cover the
substantive valid/invalid matrices for both validators. This module is only
concerned with what those leave uncovered: the barrels' own re-export
contract (exact historical ``__all__`` order, owner bindings, star-import
surface, no ``__getattr__``/lazy escape hatch, and isolated-import module
closure), and one compact canonical-vs-legacy behavioral smoke per validator.
"""

from __future__ import annotations
import __future__

import copy
import dataclasses
import enum
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

COMPONENT_ALL: tuple[str, ...] = (
    "AgentAction",
    "AgentBackedComponentContract",
    "AgentBehavior",
    "AgentRunRecord",
    "AgentRunStatus",
    "ApprovalPolicy",
    "AuditRequirement",
    "ComponentAIMode",
    "ComponentConnectorScope",
    "ComponentContract",
    "ComponentDataSource",
    "ComponentFamily",
    "ComponentGraphScope",
    "ComponentInput",
    "ComponentOutput",
    "ComponentOutputType",
    "ComponentPermission",
    "ComponentRunMode",
    "ComponentSafetyLevel",
    "PermissionPolicy",
    "ProvenanceBehavior",
    "ProvenanceRequirement",
    "ValidationResult",
    "ComponentContractValidationError",
    "validate_agent_run_record",
    "validate_component_contract",
)

MODULE_ALL: tuple[str, ...] = (
    "Entrypoint",
    "Integrity",
    "ModuleKind",
    "ModuleManifest",
    "ModuleManifestValidationError",
    "Permission",
    "PublishedTarget",
    "validate_module_manifest",
)

#: Explicit frozen canonical owner map: every barrel export bound to the
#: exact leaf module that defines it. Component: the first 23 names (through
#: ``ValidationResult``) come from ``models``; the exception and the two
#: validators come from ``validation``. Module: everything but the
#: validation-error exception and the validator function comes from
#: ``models``.
COMPONENT_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.component_contract.models"
    for name in COMPONENT_ALL[: COMPONENT_ALL.index("ValidationResult") + 1]
} | {
    name: "omnivia_core.component_contract.validation"
    for name in COMPONENT_ALL[COMPONENT_ALL.index("ValidationResult") + 1 :]
}

MODULE_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.module_manifest.validation"
    if name in ("ModuleManifestValidationError", "validate_module_manifest")
    else "omnivia_core.module_manifest.models"
    for name in MODULE_ALL
}

#: Modules a canonical component-contract/module-manifest leaf/barrel import
#: must never pull in: the legacy runtime package; SQLAlchemy/SQLite; Core's
#: own runtime/CLI/MCP sibling distributions; the Platform/Dev/Cloud/Apps/Pro
#: Modules. None of these are ever legitimately reached from a pure
#: dataclass/enum + stdlib-typing leaf.
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
    }
)

BARRELS: tuple[dict[str, Any], ...] = (
    {
        "id": "component_contract",
        "canonical": "omnivia_core.component_contract",
        "legacy": "omnivia_memory.component_contract",
        "frozen_all": COMPONENT_ALL,
        "owner_by_export": COMPONENT_OWNER_BY_EXPORT,
        "models": "omnivia_core.component_contract.models",
        "validation": "omnivia_core.component_contract.validation",
        "models_first_order": (
            "omnivia_core.component_contract.models",
            "omnivia_core.component_contract.validation",
            "omnivia_core.component_contract",
        ),
        "validation_first_order": (
            "omnivia_core.component_contract.validation",
            "omnivia_core.component_contract.models",
            "omnivia_core.component_contract",
        ),
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.component_contract",
                "omnivia_core.component_contract.models",
                "omnivia_core.component_contract.validation",
            }
        ),
    },
    {
        "id": "module_manifest",
        "canonical": "omnivia_core.module_manifest",
        "legacy": "omnivia_memory.module_manifest",
        "frozen_all": MODULE_ALL,
        "owner_by_export": MODULE_OWNER_BY_EXPORT,
        "models": "omnivia_core.module_manifest.models",
        "validation": "omnivia_core.module_manifest.validation",
        "models_first_order": (
            "omnivia_core.module_manifest.models",
            "omnivia_core.module_manifest.validation",
            "omnivia_core.module_manifest",
        ),
        "validation_first_order": (
            "omnivia_core.module_manifest.validation",
            "omnivia_core.module_manifest.models",
            "omnivia_core.module_manifest",
        ),
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.module_manifest",
                "omnivia_core.module_manifest.models",
                "omnivia_core.module_manifest.validation",
            }
        ),
    },
)


def _barrel_id(barrel: dict[str, Any]) -> str:
    return str(barrel["id"])


# ---------------------------------------------------------------------------
# Ordered __all__ oracles.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_matches_frozen_historical_tuple_exactly(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert tuple(canonical.__all__) == frozen_all, (
        f"{barrel['canonical']}.__all__ drifted from the frozen historical order:\n"
        f"canonical: {list(canonical.__all__)}\nfrozen: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_live_legacy_all_matches_frozen_historical_tuple_exactly(barrel: dict[str, Any]) -> None:
    legacy = importlib.import_module(barrel["legacy"])
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert tuple(legacy.__all__) == frozen_all, (
        f"live {barrel['legacy']}.__all__ drifted from the frozen historical order:\n"
        f"legacy: {list(legacy.__all__)}\nfrozen: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_membership_matches_inventory(barrel: dict[str, Any]) -> None:
    """Phase 0 inventory sorts ``__all__``, so it is a membership oracle only,
    never an order oracle -- confirm it really is sorted (so a future reader
    does not mistake it for an order oracle) and that its membership matches
    the frozen historical tuple's."""
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
def test_barrel_all_is_a_list_with_no_duplicates(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    legacy = importlib.import_module(barrel["legacy"])
    assert isinstance(canonical.__all__, list), (
        f"{barrel['canonical']}.__all__ must be a list, got {type(canonical.__all__).__name__}"
    )
    assert isinstance(legacy.__all__, list), (
        f"{barrel['legacy']}.__all__ must be a list, got {type(legacy.__all__).__name__}"
    )
    assert len(canonical.__all__) == len(set(canonical.__all__)), (
        f"{barrel['canonical']}.__all__ contains duplicates: {canonical.__all__}"
    )
    assert len(legacy.__all__) == len(set(legacy.__all__)), (
        f"{barrel['legacy']}.__all__ contains duplicates: {legacy.__all__}"
    )


# ---------------------------------------------------------------------------
# Owner identity, anchored to the frozen Phase 0 `defines` provenance.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_owner_map_covers_every_export_exactly(barrel: dict[str, Any]) -> None:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    assert set(owner_by_export) == set(barrel["frozen_all"]), (
        f"{barrel['id']} owner map does not cover the complete barrel: "
        f"missing={sorted(set(barrel['frozen_all']) - set(owner_by_export))}, "
        f"extra={sorted(set(owner_by_export) - set(barrel['frozen_all']))}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_owner_map_matches_frozen_leaf_defines_provenance(barrel: dict[str, Any]) -> None:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
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


# ---------------------------------------------------------------------------
# Star import surface, and no __getattr__/lazy/runtime escape hatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_star_import_exposes_exactly_advertised_names(barrel: dict[str, Any]) -> None:
    namespace: dict[str, object] = {}
    exec(f"from {barrel['canonical']} import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(barrel["frozen_all"]), (
        f"star import of {barrel['canonical']} exposed {sorted(exported)}, "
        f"expected exactly {sorted(barrel['frozen_all'])}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_has_no_getattr_exclusions_or_lazy_surface(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    module_vars = vars(canonical)
    assert "__getattr__" not in module_vars, (
        f"{barrel['canonical']} must not define module __getattr__ -- every "
        "export must be a real, statically-visible binding"
    )
    assert "__dir__" not in module_vars, (
        f"{barrel['canonical']} must not define a custom module __dir__ -- "
        "no lazy/dynamic export surface"
    )
    expected_namespace = set(barrel["frozen_all"]) | {"annotations", "models", "validation"}
    actual_namespace = {name for name in module_vars if not name.startswith("__")}
    assert actual_namespace == expected_namespace, (
        f"{barrel['canonical']} public namespace drifted: "
        f"missing={sorted(expected_namespace - actual_namespace)}, "
        f"extra={sorted(actual_namespace - expected_namespace)}"
    )
    assert module_vars["annotations"] is __future__.annotations
    assert module_vars["models"] is importlib.import_module(f"{barrel['canonical']}.models")
    assert module_vars["validation"] is importlib.import_module(
        f"{barrel['canonical']}.validation"
    )


# ---------------------------------------------------------------------------
# Isolated imports: barrel-first, models -> validation -> barrel, and
# validation -> models -> barrel, under `python -I -S` with only checkout src.
# ---------------------------------------------------------------------------


def _isolated_barrel_import_script(
    barrel: dict[str, Any], import_order: tuple[str, ...]
) -> str:
    owner_by_export = barrel["owner_by_export"]
    expected_all = barrel["frozen_all"]
    expected_closure = barrel["expected_closure"]
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            "import importlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {import_order!r}:",
            "    importlib.import_module(module_name)",
            f'barrel = sys.modules[{barrel["canonical"]!r}]',
            f"expected_all = {expected_all!r}",
            "if tuple(barrel.__all__) != expected_all:",
            '    raise SystemExit(f"__all__ drifted: {tuple(barrel.__all__)!r}")',
            f"owner_by_export = {owner_by_export!r}",
            "for export_name in expected_all:",
            "    owner_module = importlib.import_module(owner_by_export[export_name])",
            "    if getattr(barrel, export_name) is not getattr(owner_module, export_name):",
            '        raise SystemExit(f"{export_name} is not identical to its canonical owner")',
            'if "__getattr__" in vars(barrel):',
            '    raise SystemExit("barrel must not define __getattr__")',
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
    "order_key", ("barrel-first", "models-first", "validation-first"), ids=lambda v: v
)
def test_barrel_imports_in_isolation_from_src_alone(
    barrel: dict[str, Any], order_key: str
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    import_order: tuple[str, ...] = {
        "barrel-first": (barrel["canonical"],),
        "models-first": barrel["models_first_order"],
        "validation-first": barrel["validation_first_order"],
    }[order_key]

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_barrel_import_script(barrel, import_order)],
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
# Minimal behavior delta: one valid, all-fields canonical-vs-legacy smoke per
# validator. Every case deep-copies its payload before handing it to either
# engine, and proves neither engine mutates it.
# ---------------------------------------------------------------------------


def _pair(canonical_name: str) -> tuple[Any, Any]:
    legacy_name = canonical_name.replace("omnivia_core", "omnivia_memory", 1)
    return importlib.import_module(canonical_name), importlib.import_module(legacy_name)


def _normalize(value: Any) -> Any:
    """Recursively normalize dataclasses/enums/lists/dicts to plain data so
    canonical and legacy results can be compared for equality regardless of
    which package's classes they are instances of."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _normalize(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# component_contract.validate_component_contract
# ---------------------------------------------------------------------------

COMPONENT_CONTRACT_VALID_PAYLOAD: dict[str, Any] = {
    "contract_version": "1.0.0",
    "component_id": "test-component",
    "display_name": "Test Component",
    "family": "logic",
    "version": "1.0.0",
    "inputs": [{"name": "in1", "description": "desc"}],
    "outputs": [{"name": "out1", "description": "desc"}],
    "permission_requirements": [{"name": "perm1", "description": "desc"}],
    "provenance_behavior": "track",
    "agent_backed": {
        "app_compatibility": ["apps.customer-success"],
        "data_sources": [
            {
                "source_id": "meeting-notes",
                "description": "Meeting notes and transcript summary",
            }
        ],
        "graph_scope": {
            "node_types": ["contact", "meeting"],
            "edge_types": ["attended"],
            "allow_write": False,
        },
        "connector_scope": {
            "connector_ids": [],
            "allow_external_action": False,
        },
        "ai_mode": "local_first",
        "run_mode": "manual",
        "agent_behavior": {
            "objective": "Prepare a source-backed meeting brief",
            "allowed_actions": ["read", "suggest", "draft"],
            "max_safety_level": "level_2",
        },
        "output_type": "brief",
        "permission_policy": {
            "required_permissions": ["graph:read", "memory:read"],
            "allowed_actions": ["read", "suggest", "draft"],
        },
        "approval_policy": {
            "human_required": False,
        },
        "safety_level": "level_2",
        "provenance_requirements": {
            "require_sources": True,
            "require_citations": True,
            "require_audit_log": True,
        },
        "audit_requirements": {
            "required": True,
            "event_types": ["started", "completed", "blocked"],
        },
        "error_states": ["missing_source", "approval_required", "route_blocked"],
    },
}


def test_validate_component_contract_valid_payload_canonical_vs_legacy_smoke() -> None:
    canonical, legacy = _pair("omnivia_core.component_contract")
    before = copy.deepcopy(COMPONENT_CONTRACT_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(COMPONENT_CONTRACT_VALID_PAYLOAD)
    legacy_payload = copy.deepcopy(COMPONENT_CONTRACT_VALID_PAYLOAD)

    canonical_result = canonical.validate_component_contract(canonical_payload)
    legacy_result = legacy.validate_component_contract(legacy_payload)

    assert canonical_payload == before, "canonical validator mutated its input payload"
    assert legacy_payload == before, "legacy validator mutated its input payload"

    assert isinstance(canonical_result, canonical.ComponentContract)
    assert isinstance(legacy_result, legacy.ComponentContract)
    assert _normalize(canonical_result) == _normalize(legacy_result)


# ---------------------------------------------------------------------------
# component_contract.validate_agent_run_record
# ---------------------------------------------------------------------------

AGENT_RUN_RECORD_VALID_PAYLOAD: dict[str, Any] = {
    "run_id": "run-1",
    "component_id": "meeting-brief",
    "status": "approval_required",
    "objective": "Draft a meeting brief",
    "approval_required": True,
    "blocked_reason": "awaiting reviewer",
    "output_summary": "Draft ready for review",
    "error_message": "",
    "audit_event_ids": ["event-1"],
}


def test_validate_agent_run_record_valid_payload_canonical_vs_legacy_smoke() -> None:
    canonical, legacy = _pair("omnivia_core.component_contract")
    before = copy.deepcopy(AGENT_RUN_RECORD_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(AGENT_RUN_RECORD_VALID_PAYLOAD)
    legacy_payload = copy.deepcopy(AGENT_RUN_RECORD_VALID_PAYLOAD)

    canonical_result = canonical.validate_agent_run_record(canonical_payload)
    legacy_result = legacy.validate_agent_run_record(legacy_payload)

    assert canonical_payload == before, "canonical validator mutated its input payload"
    assert legacy_payload == before, "legacy validator mutated its input payload"

    assert isinstance(canonical_result, canonical.AgentRunRecord)
    assert isinstance(legacy_result, legacy.AgentRunRecord)
    assert _normalize(canonical_result) == _normalize(legacy_result)


# ---------------------------------------------------------------------------
# module_manifest.validate_module_manifest
# ---------------------------------------------------------------------------

MODULE_MANIFEST_VALID_PAYLOAD: dict[str, Any] = {
    "manifest_version": "1.0.0",
    "module_id": "com.omnivia.dev",
    "display_name": "OmniVia Dev",
    "kind": "dev",
    "version": "1.0.0",
    "compatible_core_versions": [">=0.1.0"],
    "compatible_platform_versions": [">=1.0.0"],
    "entrypoint": {
        "module": "omnivia_apps",
        "class_name": "CustomModule",
        "config_key": "custom_config",
    },
    "integrity": {
        "algorithm": "sha256",
        "digest": "abc123",
        "signature": "sig_xyz789",
    },
    "permissions": [
        {"name": "workspace.read", "description": "Read workspace files"},
        {"name": "workspace.write", "description": ""},
    ],
    "published_targets": [
        {"target_id": "launch.web", "contract_path": "targets/web.json"},
        {"target_id": "launch.cli", "contract_path": "targets/cli.json"},
    ],
}


def test_validate_module_manifest_valid_payload_canonical_vs_legacy_smoke() -> None:
    canonical, legacy = _pair("omnivia_core.module_manifest")
    before = copy.deepcopy(MODULE_MANIFEST_VALID_PAYLOAD)

    canonical_payload = copy.deepcopy(MODULE_MANIFEST_VALID_PAYLOAD)
    legacy_payload = copy.deepcopy(MODULE_MANIFEST_VALID_PAYLOAD)

    canonical_result = canonical.validate_module_manifest(canonical_payload)
    legacy_result = legacy.validate_module_manifest(legacy_payload)

    assert canonical_payload == before, "canonical validator mutated its input payload"
    assert legacy_payload == before, "legacy validator mutated its input payload"

    assert isinstance(canonical_result, canonical.ModuleManifest)
    assert isinstance(legacy_result, legacy.ModuleManifest)
    assert _normalize(canonical_result) == _normalize(legacy_result)
