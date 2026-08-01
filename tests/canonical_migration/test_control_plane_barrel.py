"""Focused adversarial acceptance coverage for ``omnivia_core.control_plane``.

``omnivia_core.control_plane`` is now the sole implementation of the barrel at
``omnivia_memory.control_plane`` (imports + models + validation). It began as a
canonical port of it; all three legacy owner leaves have since been converted
into thin compatibility facades, so they are registered in ``_leaves.py``'s
``FACADE_CANONICAL_TO_LEGACY`` rather than its source-parity map, and the legacy
barrel -- itself source-unchanged -- became identity-preserving transitively
through them. Identity, not source sameness, is what
``tests/compatibility/test_facade_foundation.py`` proves for that hop.

This module adds focused adversarial coverage for the exact barrel contract --
literal order, owner provenance, static shape, and isolated-process import
closure -- and does not re-derive owner-leaf behavior.
"""

from __future__ import annotations
import __future__

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

#: Literal, source-preserving barrel order required by the task package --
#: taken verbatim from the live legacy barrel's ``__all__``.
CANONICAL_CONTROL_PLANE_ALL: tuple[str, ...] = (
    "CONTROL_PLANE_CONTRACT_VERSION",
    "CONTROL_PLANE_SCHEMA_VERSION",
    "DANGEROUS_SIDE_EFFECTS",
    "CatalogueArtifactVerification",
    "ImportSourceChange",
    "ImportSpecValidation",
    "ImportedCandidateSet",
    "detect_import_source_change",
    "Agent",
    "Approval",
    "AuditEvent",
    "Automation",
    "Capability",
    "CapabilityType",
    "Connection",
    "ConnectionKind",
    "ConsultantAccessGrant",
    "ConsultantGrantStatus",
    "ControlPlaneManifest",
    "ControlPlaneRunStatus",
    "ControlPlaneValidationError",
    "compile_policy_expression",
    "ExecutionMode",
    "ExecutionResult",
    "ImportRecord",
    "ImportSourceProtocol",
    "LifecycleState",
    "LocalApprovalNotification",
    "LocalApprovalNotificationChannel",
    "LocalApprovalNotificationEvent",
    "LocalApprovalNotificationStatus",
    "LocalModelInvocationRecord",
    "LocalObservabilityLogRecord",
    "LocalUsageLedgerEntry",
    "Policy",
    "PolicyAttributeCondition",
    "PolicyAttributeExpression",
    "PolicyDecision",
    "PolicyDecisionReason",
    "PolicyDecisionRecord",
    "PolicyRulePack",
    "PolicyTemplate",
    "RunMode",
    "RunObservabilityMetrics",
    "RunRecord",
    "RunStepRecord",
    "RunStepStatus",
    "RunStepType",
    "SecretResolutionResult",
    "SecretReference",
    "SecretMetadata",
    "SecretStorageScope",
    "SideEffect",
    "SyncConflictStrategy",
    "SyncDirection",
    "SyncRule",
    "TenantIsolationRule",
    "Trigger",
    "TriggerEventEnvelope",
    "TriggerIngestionResult",
    "TriggerKind",
    "ValidationResult",
    "WorkspaceRef",
    "import_asyncapi_candidates",
    "import_catalogue_candidates",
    "import_catalogue_generated_candidates",
    "import_mcp_candidates",
    "import_openapi_candidates",
    "manifest_from_dict",
    "validate_asyncapi_import_spec",
    "validate_control_plane_manifest",
    "validate_mcp_import_spec",
    "validate_openapi_import_spec",
    "verify_catalogue_artifacts",
)

#: Owner leaf for every barrel export, by exact name. 55 models, 14 imports,
#: 5 validation.
EXPECTED_OWNER_BY_EXPORT: dict[str, str] = {
    "CONTROL_PLANE_CONTRACT_VERSION": "omnivia_core.control_plane.models",
    "CONTROL_PLANE_SCHEMA_VERSION": "omnivia_core.control_plane.models",
    "DANGEROUS_SIDE_EFFECTS": "omnivia_core.control_plane.validation",
    "CatalogueArtifactVerification": "omnivia_core.control_plane.imports",
    "ImportSourceChange": "omnivia_core.control_plane.imports",
    "ImportSpecValidation": "omnivia_core.control_plane.imports",
    "ImportedCandidateSet": "omnivia_core.control_plane.imports",
    "detect_import_source_change": "omnivia_core.control_plane.imports",
    "Agent": "omnivia_core.control_plane.models",
    "Approval": "omnivia_core.control_plane.models",
    "AuditEvent": "omnivia_core.control_plane.models",
    "Automation": "omnivia_core.control_plane.models",
    "Capability": "omnivia_core.control_plane.models",
    "CapabilityType": "omnivia_core.control_plane.models",
    "Connection": "omnivia_core.control_plane.models",
    "ConnectionKind": "omnivia_core.control_plane.models",
    "ConsultantAccessGrant": "omnivia_core.control_plane.models",
    "ConsultantGrantStatus": "omnivia_core.control_plane.models",
    "ControlPlaneManifest": "omnivia_core.control_plane.models",
    "ControlPlaneRunStatus": "omnivia_core.control_plane.models",
    "ControlPlaneValidationError": "omnivia_core.control_plane.validation",
    "compile_policy_expression": "omnivia_core.control_plane.validation",
    "ExecutionMode": "omnivia_core.control_plane.models",
    "ExecutionResult": "omnivia_core.control_plane.models",
    "ImportRecord": "omnivia_core.control_plane.models",
    "ImportSourceProtocol": "omnivia_core.control_plane.models",
    "LifecycleState": "omnivia_core.control_plane.models",
    "LocalApprovalNotification": "omnivia_core.control_plane.models",
    "LocalApprovalNotificationChannel": "omnivia_core.control_plane.models",
    "LocalApprovalNotificationEvent": "omnivia_core.control_plane.models",
    "LocalApprovalNotificationStatus": "omnivia_core.control_plane.models",
    "LocalModelInvocationRecord": "omnivia_core.control_plane.models",
    "LocalObservabilityLogRecord": "omnivia_core.control_plane.models",
    "LocalUsageLedgerEntry": "omnivia_core.control_plane.models",
    "Policy": "omnivia_core.control_plane.models",
    "PolicyAttributeCondition": "omnivia_core.control_plane.models",
    "PolicyAttributeExpression": "omnivia_core.control_plane.models",
    "PolicyDecision": "omnivia_core.control_plane.models",
    "PolicyDecisionReason": "omnivia_core.control_plane.models",
    "PolicyDecisionRecord": "omnivia_core.control_plane.models",
    "PolicyRulePack": "omnivia_core.control_plane.models",
    "PolicyTemplate": "omnivia_core.control_plane.models",
    "RunMode": "omnivia_core.control_plane.models",
    "RunObservabilityMetrics": "omnivia_core.control_plane.models",
    "RunRecord": "omnivia_core.control_plane.models",
    "RunStepRecord": "omnivia_core.control_plane.models",
    "RunStepStatus": "omnivia_core.control_plane.models",
    "RunStepType": "omnivia_core.control_plane.models",
    "SecretResolutionResult": "omnivia_core.control_plane.models",
    "SecretReference": "omnivia_core.control_plane.models",
    "SecretMetadata": "omnivia_core.control_plane.models",
    "SecretStorageScope": "omnivia_core.control_plane.models",
    "SideEffect": "omnivia_core.control_plane.models",
    "SyncConflictStrategy": "omnivia_core.control_plane.models",
    "SyncDirection": "omnivia_core.control_plane.models",
    "SyncRule": "omnivia_core.control_plane.models",
    "TenantIsolationRule": "omnivia_core.control_plane.models",
    "Trigger": "omnivia_core.control_plane.models",
    "TriggerEventEnvelope": "omnivia_core.control_plane.models",
    "TriggerIngestionResult": "omnivia_core.control_plane.models",
    "TriggerKind": "omnivia_core.control_plane.models",
    "ValidationResult": "omnivia_core.control_plane.models",
    "WorkspaceRef": "omnivia_core.control_plane.models",
    "import_asyncapi_candidates": "omnivia_core.control_plane.imports",
    "import_catalogue_candidates": "omnivia_core.control_plane.imports",
    "import_catalogue_generated_candidates": "omnivia_core.control_plane.imports",
    "import_mcp_candidates": "omnivia_core.control_plane.imports",
    "import_openapi_candidates": "omnivia_core.control_plane.imports",
    "manifest_from_dict": "omnivia_core.control_plane.validation",
    "validate_asyncapi_import_spec": "omnivia_core.control_plane.imports",
    "validate_control_plane_manifest": "omnivia_core.control_plane.validation",
    "validate_mcp_import_spec": "omnivia_core.control_plane.imports",
    "validate_openapi_import_spec": "omnivia_core.control_plane.imports",
    "verify_catalogue_artifacts": "omnivia_core.control_plane.imports",
}

#: Frozen Phase 0 export baseline. Owner provenance in
#: ``EXPECTED_OWNER_BY_EXPORT`` is anchored to this inventory's per-leaf
#: ``defines`` rather than hand-maintained, so a symbol silently moving owner
#: leaves is caught structurally, not just by the AST/namespace gates.
BASELINE_EXPORTS_PATH = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"

#: The three module-level constants whose runtime ``__module__`` does not
#: resolve back to the defining module (frozenset/str constants are invisible
#: to ``defines`` in the baseline inventory -- see
#: ``_strict.EXTRA_MODULE_CONSTANTS``), so they are pinned explicitly instead
#: of derived from it.
EXPLICIT_OWNER_PINS: dict[str, str] = {
    "CONTROL_PLANE_CONTRACT_VERSION": "omnivia_core.control_plane.models",
    "CONTROL_PLANE_SCHEMA_VERSION": "omnivia_core.control_plane.models",
    "DANGEROUS_SIDE_EFFECTS": "omnivia_core.control_plane.validation",
}

#: Frozen barrel membership, as a set -- the inventory's own ``all`` is
#: alphabetically sorted, so it can anchor membership but not order. The live
#: legacy ``__all__`` tuple (asserted equal to ``CANONICAL_CONTROL_PLANE_ALL``
#: in ``test_control_plane_barrel_all_matches_live_legacy_all_exactly``)
#: remains the order oracle.
CANONICAL_CONTROL_PLANE_MEMBERS: frozenset[str] = frozenset(CANONICAL_CONTROL_PLANE_ALL)

EXPECTED_CANONICAL_MODULE_CLOSURE = {
    "omnivia_core",
    "omnivia_core._shared",
    "omnivia_core._shared.validation",
    "omnivia_core.control_plane",
    "omnivia_core.control_plane.imports",
    "omnivia_core.control_plane.models",
    "omnivia_core.control_plane.validation",
    "omnivia_core.knowledge",
    "omnivia_core.knowledge.models",
    "omnivia_core.knowledge.normalize",
    "omnivia_core.knowledge.validation",
}

FORBIDDEN_MODULE_ROOTS = {
    "omnivia_memory",
    "sqlalchemy",
    "sqlite3",
    "fastapi",
    "starlette",
    "httpx",
    "requests",
    "omnivia_core_runtime",
    "omnivia_core_mcp",
    "omnivia_core_cli",
    "omnivia_platform",
    "omnivia_apps",
    "omnivia_dev",
    "omnivia_pro",
    "omnivia_cloud",
}


# ---------------------------------------------------------------------------
# Exact barrel contract.
# ---------------------------------------------------------------------------


def test_control_plane_barrel_all_matches_pinned_74_name_tuple_exactly() -> None:
    canonical = importlib.import_module("omnivia_core.control_plane")
    assert isinstance(canonical.__all__, list)
    assert tuple(canonical.__all__) == CANONICAL_CONTROL_PLANE_ALL, (
        "omnivia_core.control_plane.__all__ drifted from the pinned literal order:\n"
        f"actual: {canonical.__all__}\nexpected: {list(CANONICAL_CONTROL_PLANE_ALL)}"
    )
    assert len(canonical.__all__) == 74


def test_control_plane_barrel_all_matches_live_legacy_all_exactly() -> None:
    legacy = importlib.import_module("omnivia_memory.control_plane")
    assert tuple(legacy.__all__) == CANONICAL_CONTROL_PLANE_ALL


def test_control_plane_barrel_all_has_no_duplicates() -> None:
    canonical = importlib.import_module("omnivia_core.control_plane")
    assert len(canonical.__all__) == len(set(canonical.__all__))


def test_control_plane_barrel_owner_map_has_exact_leaf_counts() -> None:
    by_leaf: dict[str, int] = {}
    for owner in EXPECTED_OWNER_BY_EXPORT.values():
        by_leaf[owner] = by_leaf.get(owner, 0) + 1
    assert by_leaf == {
        "omnivia_core.control_plane.models": 55,
        "omnivia_core.control_plane.imports": 14,
        "omnivia_core.control_plane.validation": 5,
    }
    assert set(EXPECTED_OWNER_BY_EXPORT) == CANONICAL_CONTROL_PLANE_MEMBERS


def test_control_plane_barrel_membership_and_owners_match_phase_zero_inventory() -> None:
    inventory: dict[str, Any] = json.loads(BASELINE_EXPORTS_PATH.read_text(encoding="utf-8"))[
        "modules"
    ]
    assert set(inventory["omnivia_memory.control_plane"]["all"]) == CANONICAL_CONTROL_PLANE_MEMBERS

    inventory_owners: dict[str, str] = {}
    for legacy_leaf, canonical_leaf in (
        ("omnivia_memory.control_plane.imports", "omnivia_core.control_plane.imports"),
        ("omnivia_memory.control_plane.models", "omnivia_core.control_plane.models"),
        ("omnivia_memory.control_plane.validation", "omnivia_core.control_plane.validation"),
    ):
        for export_name in inventory[legacy_leaf]["defines"]:
            # A leaf's ``defines`` may list internal helpers (e.g. the
            # validation leaf's ``T`` TypeVar) that are not barrel exports;
            # those are irrelevant to owner provenance and are skipped here
            # rather than pinned, since they are not "constants omitted from
            # defines" -- they are simply not exported.
            if export_name not in CANONICAL_CONTROL_PLANE_MEMBERS:
                continue
            assert export_name not in inventory_owners
            inventory_owners[export_name] = canonical_leaf
    inventory_owners.update(EXPLICIT_OWNER_PINS)

    assert inventory_owners == EXPECTED_OWNER_BY_EXPORT


def test_control_plane_barrel_star_import_exposes_exactly_the_74_names() -> None:
    namespace: dict[str, object] = {}
    exec("from omnivia_core.control_plane import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(CANONICAL_CONTROL_PLANE_ALL)


def test_control_plane_barrel_bindings_are_identical_to_owner_leaf() -> None:
    canonical = importlib.import_module("omnivia_core.control_plane")
    for name in canonical.__all__:
        owner_name = EXPECTED_OWNER_BY_EXPORT[name]
        owner_module = importlib.import_module(owner_name)
        assert getattr(canonical, name) is getattr(owner_module, name), (
            f"omnivia_core.control_plane.{name} is not identical to its owner {owner_name}.{name}"
        )


def test_control_plane_barrel_has_no_getattr_or_dir_escape_hatch() -> None:
    canonical = importlib.import_module("omnivia_core.control_plane")
    assert "__getattr__" not in vars(canonical)
    assert "__dir__" not in vars(canonical)


def test_control_plane_barrel_namespace_is_exact() -> None:
    canonical = importlib.import_module("omnivia_core.control_plane")
    actual = {name for name in vars(canonical) if not name.startswith("_")}
    expected = set(CANONICAL_CONTROL_PLANE_ALL) | {"annotations", "imports", "models", "validation"}
    assert actual == expected
    assert vars(canonical)["annotations"] is __future__.annotations
    assert canonical.imports is importlib.import_module("omnivia_core.control_plane.imports")
    assert canonical.models is importlib.import_module("omnivia_core.control_plane.models")
    assert canonical.validation is importlib.import_module("omnivia_core.control_plane.validation")


def test_control_plane_barrel_source_uses_direct_absolute_owner_imports_only() -> None:
    """The barrel is a docstring, future import, three owner imports, and ``__all__``."""

    barrel_path = CORE_SRC / "omnivia_core" / "control_plane" / "__init__.py"
    tree = ast.parse(barrel_path.read_text(encoding="utf-8"))
    body = tree.body
    assert ast.get_docstring(tree) is not None
    assert len(body) == 6

    future_import = body[1]
    assert isinstance(future_import, ast.ImportFrom)
    assert future_import.module == "__future__" and future_import.level == 0
    assert [(alias.name, alias.asname) for alias in future_import.names] == [
        ("annotations", None)
    ]

    expected_routes: dict[str, set[str]] = {}
    for export_name, owner_name in EXPECTED_OWNER_BY_EXPORT.items():
        expected_routes.setdefault(owner_name, set()).add(export_name)
    assert set(expected_routes) == {
        "omnivia_core.control_plane.imports",
        "omnivia_core.control_plane.models",
        "omnivia_core.control_plane.validation",
    }

    actual_routes: dict[str, set[str]] = {}
    imported_names: set[str] = set()
    for statement in body[2:5]:
        assert isinstance(statement, ast.ImportFrom)
        assert statement.level == 0
        assert statement.module in expected_routes
        assert statement.module not in actual_routes
        route_names: set[str] = set()
        for alias in statement.names:
            assert alias.name != "*"
            assert alias.asname is None
            assert alias.name not in imported_names
            imported_names.add(alias.name)
            route_names.add(alias.name)
        actual_routes[statement.module] = route_names
    assert actual_routes == expected_routes

    all_assignment = body[5]
    assert isinstance(all_assignment, ast.Assign)
    assert len(all_assignment.targets) == 1
    assert isinstance(all_assignment.targets[0], ast.Name)
    assert all_assignment.targets[0].id == "__all__"
    assert isinstance(all_assignment.value, ast.List)
    literal_values: list[str] = []
    for element in all_assignment.value.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str)
        literal_values.append(element.value)
    assert tuple(literal_values) == CANONICAL_CONTROL_PLANE_ALL

    assert all(not isinstance(descendant, ast.Import) for descendant in ast.walk(tree))
    assert all(
        not isinstance(descendant, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for descendant in ast.walk(tree)
    )


# ---------------------------------------------------------------------------
# Isolated `python -I -S` imports, exact canonical module closure.
# ---------------------------------------------------------------------------


def _isolated_import_script(import_order: tuple[str, ...]) -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            "import importlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {import_order!r}:",
            "    importlib.import_module(module_name)",
            'barrel = sys.modules["omnivia_core.control_plane"]',
            f"expected_all = {CANONICAL_CONTROL_PLANE_ALL!r}",
            "if tuple(barrel.__all__) != expected_all:",
            '    raise SystemExit(f"__all__ drifted: {tuple(barrel.__all__)!r}")',
            f"owner_by_export = {EXPECTED_OWNER_BY_EXPORT!r}",
            "for export_name in expected_all:",
            "    owner_module = importlib.import_module(owner_by_export[export_name])",
            "    if getattr(barrel, export_name) is not getattr(owner_module, export_name):",
            '        raise SystemExit(f"{export_name} is not identical to its canonical owner")',
            f"expected_namespace = {set(CANONICAL_CONTROL_PLANE_ALL) | {'annotations', 'imports', 'models', 'validation'}!r}",
            'actual_namespace = {name for name in vars(barrel) if not name.startswith("_")}',
            "if actual_namespace != expected_namespace:",
            '    raise SystemExit(f"barrel namespace drifted: {sorted(actual_namespace)!r}")',
            "import __future__",
            'if vars(barrel)["annotations"] is not __future__.annotations:',
            '    raise SystemExit("barrel annotations future binding drifted")',
            'for child in ("imports", "models", "validation"):',
            '    expected_child = sys.modules[f"omnivia_core.control_plane.{child}"]',
            "    if getattr(barrel, child) is not expected_child:",
            '        raise SystemExit(f"barrel {child} child-module binding drifted")',
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
            f"expected_core = {EXPECTED_CANONICAL_MODULE_CLOSURE!r}",
            'loaded_core = {name for name in sys.modules if name == "omnivia_core" or name.startswith("omnivia_core.")}',
            "if loaded_core != expected_core:",
            '    raise SystemExit(f"unexpected canonical module closure: {sorted(loaded_core)}")',
            f"forbidden = {sorted(FORBIDDEN_MODULE_ROOTS)!r}",
            'leaked = sorted(set(forbidden) & {m.split(".")[0] for m in sys.modules})',
            "if leaked:",
            '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
            'print("OK")',
        ]
    )


@pytest.mark.parametrize(
    "import_order",
    (
        ("omnivia_core.control_plane",),
        (
            "omnivia_core.control_plane.models",
            "omnivia_core.control_plane.imports",
            "omnivia_core.control_plane.validation",
            "omnivia_core.control_plane",
        ),
        (
            "omnivia_core.control_plane.validation",
            "omnivia_core.control_plane.imports",
            "omnivia_core.control_plane.models",
            "omnivia_core.control_plane",
        ),
    ),
    ids=("barrel-first", "leaves-first", "leaves-first-reversed"),
)
def test_control_plane_imports_in_isolation_from_src_alone_with_exact_closure(
    import_order: tuple[str, ...],
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_import_script(import_order)],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of omnivia_core.control_plane failed (order={import_order})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
