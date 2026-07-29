"""Focused adversarial acceptance coverage for ``omnivia_core.run_ledger``.

``omnivia_core.run_ledger`` is a fresh canonical port of
``omnivia_memory.run_ledger`` (models + validation + barrel). Its owner leaves
are registered in the shared ``_leaves.py`` migration gates; this module adds
focused adversarial coverage for the exact barrel contract, isolated import
closure, and run-ledger behavior.

The one deliberate departure from a plain ``omnivia_memory`` -> ``omnivia_core``
rename is ``validation.py``'s single legacy import of
``omnivia_memory.knowledge`` (``ValidationResult`` and
``check_contract_version_compatibility`` together), which the task package
requires to split into two exact canonical owner-leaf imports:
``omnivia_core._shared.validation.ValidationResult`` and
``omnivia_core.knowledge.validation.check_contract_version_compatibility``.
The shared ``test_parity.py`` sanctioned-rewrite gate performs exactly that
split and nothing else.
"""

from __future__ import annotations
import __future__

import ast
import copy
import dataclasses
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

#: Literal, source-preserving barrel order required by the task package.
CANONICAL_RUN_LEDGER_ALL: tuple[str, ...] = (
    "RUN_LEDGER_CONTRACT_VERSION",
    "RUN_LEDGER_PATH_ENV",
    "EvidenceFileRef",
    "RunLedgerEntry",
    "RunLedgerProvenance",
    "RunLedgerStatus",
    "TERMINAL_RUN_STATUSES",
    "validate_evidence_file_ref",
    "validate_run_ledger_entry",
    "validate_run_ledger_provenance",
)

EXPECTED_OWNER_BY_EXPORT: dict[str, str] = {
    "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.run_ledger.models",
    "RUN_LEDGER_PATH_ENV": "omnivia_core.run_ledger.models",
    "EvidenceFileRef": "omnivia_core.run_ledger.models",
    "RunLedgerEntry": "omnivia_core.run_ledger.models",
    "RunLedgerProvenance": "omnivia_core.run_ledger.models",
    "RunLedgerStatus": "omnivia_core.run_ledger.models",
    "TERMINAL_RUN_STATUSES": "omnivia_core.run_ledger.validation",
    "validate_evidence_file_ref": "omnivia_core.run_ledger.validation",
    "validate_run_ledger_entry": "omnivia_core.run_ledger.validation",
    "validate_run_ledger_provenance": "omnivia_core.run_ledger.validation",
}

#: Frozen Phase 0 export baseline. Function/class owner provenance in
#: ``EXPECTED_OWNER_BY_EXPORT`` is anchored to this inventory's per-leaf
#: ``defines`` rather than hand-maintained, so a symbol silently moving
#: owner leaves is caught structurally, not just by the AST/namespace gates.
BASELINE_EXPORTS_PATH = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"

#: The three exports whose runtime ``__module__`` does not resolve back to
#: the defining module (see ``_strict.EXTRA_MODULE_CONSTANTS``), so they are
#: invisible to ``defines`` in the baseline inventory and must be pinned
#: explicitly instead of derived from it.
EXPLICIT_OWNER_PINS: dict[str, str] = {
    "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.run_ledger.models",
    "RUN_LEDGER_PATH_ENV": "omnivia_core.run_ledger.models",
    "TERMINAL_RUN_STATUSES": "omnivia_core.run_ledger.validation",
}

#: Frozen barrel membership, as a set -- the inventory's own ``all`` is
#: alphabetically sorted, so it can anchor membership but not order. The
#: live legacy ``__all__`` tuple (asserted equal to ``CANONICAL_RUN_LEDGER_ALL``
#: in ``test_run_ledger_barrel_all_matches_live_legacy_all_exactly``) remains
#: the order oracle.
CANONICAL_RUN_LEDGER_MEMBERS: frozenset[str] = frozenset(CANONICAL_RUN_LEDGER_ALL)

#: ``omnivia_core.knowledge.normalize`` is not in the task package's listed
#: closure, but ``omnivia_core.knowledge.validation`` (the direct owner of
#: ``check_contract_version_compatibility``) imports normalization helpers
#: from it at module scope, so it loads transitively and unavoidably any
#: time ``check_contract_version_compatibility`` is reachable -- confirmed by
#: direct source inspection of ``src/omnivia_core/knowledge/validation.py``.
EXPECTED_CANONICAL_MODULE_CLOSURE = {
    "omnivia_core",
    "omnivia_core._shared",
    "omnivia_core._shared.validation",
    "omnivia_core.knowledge",
    "omnivia_core.knowledge.models",
    "omnivia_core.knowledge.normalize",
    "omnivia_core.knowledge.validation",
    "omnivia_core.run_ledger",
    "omnivia_core.run_ledger.models",
    "omnivia_core.run_ledger.validation",
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


def test_run_ledger_status_enum_values_and_order_are_exact() -> None:
    from omnivia_core.run_ledger.models import RunLedgerStatus

    assert [member.value for member in RunLedgerStatus] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
    ]
    assert [member.name for member in RunLedgerStatus] == [
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "BLOCKED",
        "CANCELLED",
    ]
    assert issubclass(RunLedgerStatus, str)


def test_run_ledger_dataclass_fields_order_and_frozen_flags_are_exact() -> None:
    from omnivia_core.run_ledger.models import (
        EvidenceFileRef,
        RunLedgerEntry,
        RunLedgerProvenance,
    )

    assert [f.name for f in dataclasses.fields(EvidenceFileRef)] == [
        "path",
        "kind",
        "description",
        "checksum",
    ]
    assert [f.name for f in dataclasses.fields(RunLedgerProvenance)] == [
        "producer",
        "source_ref",
        "producer_version",
    ]
    assert [f.name for f in dataclasses.fields(RunLedgerEntry)] == [
        "run_id",
        "task_id",
        "target_repo",
        "lane_id",
        "status",
        "started_at",
        "updated_at",
        "evidence_file_refs",
        "provenance",
        "completed_at",
        "contract_version",
    ]
    for cls in (EvidenceFileRef, RunLedgerProvenance, RunLedgerEntry):
        assert dataclasses.is_dataclass(cls)
        dataclass_params: Any = vars(cls)["__dataclass_params__"]
        assert dataclass_params.frozen is True


def test_run_ledger_constants_are_exact() -> None:
    from omnivia_core.run_ledger.models import RUN_LEDGER_PATH_ENV, RunLedgerStatus
    from omnivia_core.run_ledger.validation import TERMINAL_RUN_STATUSES

    assert RUN_LEDGER_PATH_ENV == "OMNIVIA_RUN_LEDGER_PATH"
    assert TERMINAL_RUN_STATUSES == frozenset(
        {
            RunLedgerStatus.SUCCEEDED,
            RunLedgerStatus.FAILED,
            RunLedgerStatus.BLOCKED,
            RunLedgerStatus.CANCELLED,
        }
    )
    assert isinstance(TERMINAL_RUN_STATUSES, frozenset)


# ---------------------------------------------------------------------------
# Layer 4: RUN_LEDGER_CONTRACT_VERSION identity and wire-v1 distinctness.
# ---------------------------------------------------------------------------


def test_run_ledger_contract_version_uses_knowledge_models_contract_version() -> None:
    from omnivia_core.knowledge.models import ContractVersion
    from omnivia_core.run_ledger.models import RUN_LEDGER_CONTRACT_VERSION

    assert isinstance(RUN_LEDGER_CONTRACT_VERSION, ContractVersion)
    assert type(RUN_LEDGER_CONTRACT_VERSION) is ContractVersion
    assert (RUN_LEDGER_CONTRACT_VERSION.major, RUN_LEDGER_CONTRACT_VERSION.minor) == (1, 0)
    assert str(RUN_LEDGER_CONTRACT_VERSION) == "1.0"


def test_run_ledger_contract_version_is_the_knowledge_domain_type_not_a_string_alias() -> None:
    """The run-ledger contract version is a real
    ``knowledge.models.ContractVersion`` dataclass -- not a bare ``str`` wire
    alias -- and the two must not be interchangeable.

    This asserts the knowledge-owner type, its fields, and non-string-ness
    directly; it does not couple to ``omnivia_core.contracts.v1.
    ContractVersion`` (an independent generated wire module) remaining
    exactly ``str``, since that module's shape is not this leaf's contract to
    pin.
    """

    from omnivia_core.knowledge.models import (
        ContractVersion as KnowledgeContractVersion,
    )
    from omnivia_core.run_ledger.models import RUN_LEDGER_CONTRACT_VERSION

    assert type(RUN_LEDGER_CONTRACT_VERSION) is KnowledgeContractVersion
    assert dataclasses.is_dataclass(KnowledgeContractVersion)
    assert {f.name for f in dataclasses.fields(KnowledgeContractVersion)} >= {"major", "minor"}
    assert not isinstance(RUN_LEDGER_CONTRACT_VERSION, str)
    assert not isinstance("1.0", KnowledgeContractVersion)


# ---------------------------------------------------------------------------
# Layer 5: exact barrel contract.
# ---------------------------------------------------------------------------


def test_run_ledger_barrel_all_matches_pinned_ten_name_tuple_exactly() -> None:
    canonical = importlib.import_module("omnivia_core.run_ledger")
    assert isinstance(canonical.__all__, list)
    assert tuple(canonical.__all__) == CANONICAL_RUN_LEDGER_ALL, (
        "omnivia_core.run_ledger.__all__ drifted from the pinned literal order:\n"
        f"actual: {canonical.__all__}\nexpected: {list(CANONICAL_RUN_LEDGER_ALL)}"
    )
    assert len(canonical.__all__) == 10


def test_run_ledger_barrel_all_matches_live_legacy_all_exactly() -> None:
    legacy = importlib.import_module("omnivia_memory.run_ledger")
    assert tuple(legacy.__all__) == CANONICAL_RUN_LEDGER_ALL


def test_run_ledger_barrel_membership_and_owners_match_phase_zero_inventory() -> None:
    inventory: dict[str, Any] = json.loads(BASELINE_EXPORTS_PATH.read_text(encoding="utf-8"))[
        "modules"
    ]
    assert set(inventory["omnivia_memory.run_ledger"]["all"]) == CANONICAL_RUN_LEDGER_MEMBERS

    inventory_owners: dict[str, str] = {}
    for legacy_leaf, canonical_leaf in (
        ("omnivia_memory.run_ledger.models", "omnivia_core.run_ledger.models"),
        ("omnivia_memory.run_ledger.validation", "omnivia_core.run_ledger.validation"),
    ):
        for export_name in inventory[legacy_leaf]["defines"]:
            assert export_name not in inventory_owners
            inventory_owners[export_name] = canonical_leaf
    inventory_owners.update(EXPLICIT_OWNER_PINS)

    assert inventory_owners == EXPECTED_OWNER_BY_EXPORT


def test_run_ledger_barrel_all_has_no_duplicates() -> None:
    canonical = importlib.import_module("omnivia_core.run_ledger")
    assert len(canonical.__all__) == len(set(canonical.__all__))


def test_run_ledger_barrel_star_import_exposes_exactly_the_ten_names() -> None:
    namespace: dict[str, object] = {}
    exec("from omnivia_core.run_ledger import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(CANONICAL_RUN_LEDGER_ALL)


def test_run_ledger_barrel_bindings_are_identical_to_owner_leaf() -> None:
    canonical = importlib.import_module("omnivia_core.run_ledger")
    for name in canonical.__all__:
        owner_name = EXPECTED_OWNER_BY_EXPORT[name]
        owner_module = importlib.import_module(owner_name)
        assert getattr(canonical, name) is getattr(owner_module, name), (
            f"omnivia_core.run_ledger.{name} is not identical to its owner {owner_name}.{name}"
        )


def test_run_ledger_barrel_has_no_getattr_or_dir_escape_hatch() -> None:
    canonical = importlib.import_module("omnivia_core.run_ledger")
    assert "__getattr__" not in vars(canonical)
    assert "__dir__" not in vars(canonical)


def test_run_ledger_barrel_namespace_is_exact() -> None:
    canonical = importlib.import_module("omnivia_core.run_ledger")
    actual = {name for name in vars(canonical) if not name.startswith("_")}
    expected = set(CANONICAL_RUN_LEDGER_ALL) | {"annotations", "models", "validation"}
    assert actual == expected
    assert vars(canonical)["annotations"] is __future__.annotations
    assert canonical.models is importlib.import_module("omnivia_core.run_ledger.models")
    assert canonical.validation is importlib.import_module("omnivia_core.run_ledger.validation")


def test_run_ledger_barrel_source_uses_direct_absolute_owner_imports_only() -> None:
    """The barrel is a docstring, future import, owner imports, and ``__all__``."""

    barrel_path = CORE_SRC / "omnivia_core" / "run_ledger" / "__init__.py"
    tree = ast.parse(barrel_path.read_text(encoding="utf-8"))
    body = tree.body
    assert ast.get_docstring(tree) is not None
    assert len(body) == 5

    future_import = body[1]
    assert isinstance(future_import, ast.ImportFrom)
    assert future_import.module == "__future__" and future_import.level == 0
    assert [(alias.name, alias.asname) for alias in future_import.names] == [
        ("annotations", None)
    ]

    expected_routes: dict[str, set[str]] = {}
    for export_name, owner_name in EXPECTED_OWNER_BY_EXPORT.items():
        expected_routes.setdefault(owner_name, set()).add(export_name)

    actual_routes: dict[str, set[str]] = {}
    imported_names: set[str] = set()
    for statement in body[2:4]:
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

    all_assignment = body[4]
    assert isinstance(all_assignment, ast.Assign)
    assert len(all_assignment.targets) == 1
    assert isinstance(all_assignment.targets[0], ast.Name)
    assert all_assignment.targets[0].id == "__all__"
    assert isinstance(all_assignment.value, ast.List)
    literal_values: list[str] = []
    for element in all_assignment.value.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str)
        literal_values.append(element.value)
    assert tuple(literal_values) == CANONICAL_RUN_LEDGER_ALL

    assert all(
        not isinstance(descendant, ast.Import)
        for descendant in ast.walk(tree)
    )
    assert all(
        not isinstance(descendant, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for descendant in ast.walk(tree)
    )


# ---------------------------------------------------------------------------
# Layer 6: fresh `python -I -S` imports, exact canonical module closure.
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
            'barrel = sys.modules["omnivia_core.run_ledger"]',
            f"expected_all = {CANONICAL_RUN_LEDGER_ALL!r}",
            "if tuple(barrel.__all__) != expected_all:",
            '    raise SystemExit(f"__all__ drifted: {tuple(barrel.__all__)!r}")',
            f"owner_by_export = {EXPECTED_OWNER_BY_EXPORT!r}",
            "for export_name in expected_all:",
            "    owner_module = importlib.import_module(owner_by_export[export_name])",
            "    if getattr(barrel, export_name) is not getattr(owner_module, export_name):",
            '        raise SystemExit(f"{export_name} is not identical to its canonical owner")',
            f"expected_namespace = {set(CANONICAL_RUN_LEDGER_ALL) | {'annotations', 'models', 'validation'}!r}",
            'actual_namespace = {name for name in vars(barrel) if not name.startswith("_")}',
            "if actual_namespace != expected_namespace:",
            '    raise SystemExit(f"barrel namespace drifted: {sorted(actual_namespace)!r}")',
            "import __future__",
            'if vars(barrel)["annotations"] is not __future__.annotations:',
            '    raise SystemExit("barrel annotations future binding drifted")',
            'if barrel.models is not sys.modules["omnivia_core.run_ledger.models"]:',
            '    raise SystemExit("barrel models child-module binding drifted")',
            'if barrel.validation is not sys.modules["omnivia_core.run_ledger.validation"]:',
            '    raise SystemExit("barrel validation child-module binding drifted")',
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
        ("omnivia_core.run_ledger",),
        (
            "omnivia_core.run_ledger.models",
            "omnivia_core.run_ledger.validation",
            "omnivia_core.run_ledger",
        ),
        (
            "omnivia_core.run_ledger.validation",
            "omnivia_core.run_ledger.models",
            "omnivia_core.run_ledger",
        ),
    ),
    ids=("barrel-first", "models-first", "validation-first"),
)
def test_run_ledger_imports_in_isolation_from_src_alone_with_exact_closure(
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
        f"isolated import of omnivia_core.run_ledger failed (order={import_order})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# Layer 7: behavioral parity against the legacy tree.
# ---------------------------------------------------------------------------


def _build_entry(module: ModuleType, **overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "run_id": "run-001",
        "task_id": "T-0103",
        "target_repo": "omnivia-core",
        "lane_id": "L2",
        "status": module.RunLedgerStatus.RUNNING,
        "started_at": "2026-06-10T11:00:00Z",
        "updated_at": "2026-06-10T11:05:00Z",
        "evidence_file_refs": [
            module.EvidenceFileRef(
                path="artifacts/runs/run-001/summary.md",
                kind="summary",
                description="Run summary",
            )
        ],
        "provenance": module.RunLedgerProvenance(
            producer="omnivia-pm",
            source_ref="runs/run-001.json",
            producer_version="1.0.0",
        ),
    }
    defaults.update(overrides)
    return module.RunLedgerEntry(**defaults)


def _pair() -> tuple[ModuleType, ModuleType]:
    return (
        importlib.import_module("omnivia_core.run_ledger"),
        importlib.import_module("omnivia_memory.run_ledger"),
    )


def test_full_nested_round_trip_matches_legacy() -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(canonical)
    legacy_entry = _build_entry(legacy)

    canonical_restored = canonical.RunLedgerEntry.from_dict(canonical_entry.to_dict())
    legacy_restored = legacy.RunLedgerEntry.from_dict(legacy_entry.to_dict())

    assert canonical_restored == canonical_entry
    assert legacy_restored == legacy_entry
    assert canonical_entry.to_dict() == legacy_entry.to_dict()

    canonical_result = canonical.validate_run_ledger_entry(canonical_restored)
    legacy_result = legacy.validate_run_ledger_entry(legacy_restored)
    assert (canonical_result.valid, canonical_result.errors, canonical_result.warnings) == (
        legacy_result.valid,
        legacy_result.errors,
        legacy_result.warnings,
    )
    assert canonical_result.valid
    assert canonical_result.warnings == []
    assert str(canonical_restored.contract_version) == "1.0"


@pytest.mark.parametrize(
    "bad_payload,expected_exception",
    (
        ({"evidence_file_refs": "not-a-list"}, TypeError),
        ({"evidence_file_refs": [1, 2, 3]}, TypeError),
        ({"evidence_file_refs": ["not-a-dict"]}, TypeError),
        ({"provenance": "not-a-mapping"}, TypeError),
        ({"provenance": 42}, TypeError),
    ),
)
def test_invalid_evidence_and_provenance_container_types_match_legacy(
    bad_payload: dict[str, Any], expected_exception: type[Exception]
) -> None:
    canonical, legacy = _pair()
    base = _build_entry(canonical).to_dict()
    base.update(bad_payload)
    legacy_base = _build_entry(legacy).to_dict()
    legacy_base.update(bad_payload)

    with pytest.raises(expected_exception):
        canonical.RunLedgerEntry.from_dict(base)
    with pytest.raises(expected_exception):
        legacy.RunLedgerEntry.from_dict(legacy_base)


def test_evidence_file_refs_entries_that_are_not_objects_are_rejected() -> None:
    canonical, legacy = _pair()
    for module in (canonical, legacy):
        payload = _build_entry(module).to_dict()
        payload["evidence_file_refs"] = [{"path": "ok", "kind": "artifact"}, "bad-entry"]
        with pytest.raises(TypeError):
            module.RunLedgerEntry.from_dict(payload)


@pytest.mark.parametrize(
    "status_value",
    ("queued", "running", "succeeded", "failed", "blocked", "cancelled"),
)
def test_all_status_values_round_trip_and_match_legacy(status_value: str) -> None:
    canonical, legacy = _pair()
    canonical_status = canonical.RunLedgerStatus(status_value)
    legacy_status = legacy.RunLedgerStatus(status_value)
    assert canonical_status.value == legacy_status.value == status_value


def test_terminal_statuses_set_matches_legacy() -> None:
    canonical, legacy = _pair()
    canonical_terminal = {s.value for s in canonical.TERMINAL_RUN_STATUSES}
    legacy_terminal = {s.value for s in legacy.TERMINAL_RUN_STATUSES}
    assert canonical_terminal == legacy_terminal == {"succeeded", "failed", "blocked", "cancelled"}


@pytest.mark.parametrize(
    "missing_field",
    ("run_id", "task_id", "target_repo", "lane_id"),
)
def test_required_identifiers_produce_matching_errors(missing_field: str) -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(canonical, **{missing_field: ""})
    legacy_entry = _build_entry(legacy, **{missing_field: ""})

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)
    assert f"{missing_field} is required" in canonical_result.errors
    assert canonical_result.errors == legacy_result.errors
    assert canonical_result.warnings == legacy_result.warnings


@pytest.mark.parametrize(
    "value,is_valid",
    (
        ("2026-06-10T11:00:00Z", True),
        ("2026-06-10T11:00:00+00:00", True),
        ("not-a-timestamp", False),
        ("", False),
    ),
)
def test_valid_and_invalid_timestamps_match_legacy(value: str, is_valid: bool) -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(canonical, started_at=value)
    legacy_entry = _build_entry(legacy, started_at=value)

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)
    if value == "":
        assert "started_at is required" in canonical_result.errors
    else:
        assert ("started_at must be an ISO timestamp" in canonical_result.errors) == (not is_valid)
    assert canonical_result.errors == legacy_result.errors


def test_terminal_completed_at_is_required_and_matches_legacy() -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(
        canonical, status=canonical.RunLedgerStatus.SUCCEEDED, completed_at=None
    )
    legacy_entry = _build_entry(legacy, status=legacy.RunLedgerStatus.SUCCEEDED, completed_at=None)

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)
    assert "completed_at is required for terminal run statuses" in canonical_result.errors
    assert canonical_result.errors == legacy_result.errors


def test_terminal_missing_evidence_produces_matching_warning() -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(
        canonical,
        status=canonical.RunLedgerStatus.FAILED,
        completed_at="2026-06-10T12:00:00Z",
        evidence_file_refs=[],
    )
    legacy_entry = _build_entry(
        legacy,
        status=legacy.RunLedgerStatus.FAILED,
        completed_at="2026-06-10T12:00:00Z",
        evidence_file_refs=[],
    )

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)
    assert "terminal run has no evidence_file_refs" in canonical_result.warnings
    assert canonical_result.warnings == legacy_result.warnings
    assert canonical_result.valid


def test_nested_evidence_and_provenance_errors_are_prefixed_and_match_legacy() -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(
        canonical,
        status=canonical.RunLedgerStatus.SUCCEEDED,
        completed_at="2026-06-10T12:00:00Z",
        evidence_file_refs=[canonical.EvidenceFileRef(path="", kind="")],
        provenance=canonical.RunLedgerProvenance(producer=""),
    )
    legacy_entry = _build_entry(
        legacy,
        status=legacy.RunLedgerStatus.SUCCEEDED,
        completed_at="2026-06-10T12:00:00Z",
        evidence_file_refs=[legacy.EvidenceFileRef(path="", kind="")],
        provenance=legacy.RunLedgerProvenance(producer=""),
    )

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)

    assert "evidence_file_refs[0].path is required" in canonical_result.errors
    assert "evidence_file_refs[0].kind is required" in canonical_result.errors
    assert "provenance.producer is required" in canonical_result.errors
    assert canonical_result.errors == legacy_result.errors


def test_status_must_be_enum_member_matches_legacy() -> None:
    canonical, legacy = _pair()
    canonical_entry = _build_entry(canonical)
    legacy_entry = _build_entry(legacy)
    object.__setattr__(canonical_entry, "status", "queued")
    object.__setattr__(legacy_entry, "status", "queued")

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)
    assert "status must be a RunLedgerStatus" in canonical_result.errors
    assert canonical_result.errors == legacy_result.errors


@pytest.mark.parametrize(
    "candidate_version,compatible",
    (("1.0", True), ("1.5", True), ("2.0", False), ("0.9", False)),
)
def test_compatible_and_incompatible_contract_versions_match_legacy(
    candidate_version: str, compatible: bool
) -> None:
    canonical, legacy = _pair()
    from omnivia_memory.knowledge.models import ContractVersion as LegacyContractVersion

    from omnivia_core.knowledge.models import (
        ContractVersion as CanonicalContractVersion,
    )

    canonical_entry = _build_entry(
        canonical, contract_version=CanonicalContractVersion.parse(candidate_version)
    )
    legacy_entry = _build_entry(
        legacy, contract_version=LegacyContractVersion.parse(candidate_version)
    )

    canonical_result = canonical.validate_run_ledger_entry(canonical_entry)
    legacy_result = legacy.validate_run_ledger_entry(legacy_entry)

    major_mismatch_error = any("major version" in error or "incompatible" in error for error in canonical_result.errors)
    assert major_mismatch_error == (not compatible)
    assert canonical_result.errors == legacy_result.errors
    assert canonical_result.warnings == legacy_result.warnings


def test_validators_do_not_mutate_their_inputs() -> None:
    canonical, _legacy = _pair()
    entry = _build_entry(
        canonical,
        status=canonical.RunLedgerStatus.SUCCEEDED,
        completed_at="2026-06-10T12:00:00Z",
    )
    before = copy.deepcopy(entry.to_dict())

    canonical.validate_run_ledger_entry(entry)
    canonical.validate_evidence_file_ref(entry.evidence_file_refs[0])
    canonical.validate_run_ledger_provenance(entry.provenance)

    assert entry.to_dict() == before


def test_to_dict_does_not_share_mutable_state_with_the_source_entry() -> None:
    canonical, _legacy = _pair()
    entry = _build_entry(canonical)
    payload = entry.to_dict()

    payload["evidence_file_refs"].append({"path": "x", "kind": "y", "description": None, "checksum": None})
    assert len(entry.evidence_file_refs) == 1, "mutating to_dict() output leaked into the source entry"
