"""Fail-closed coverage for the frozen compatibility-facade route registry."""

from __future__ import annotations

import ast
import copy
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from baseline.facade_manifest import (
    MANIFEST_PATH,
    REPO_ROOT,
    SCHEMA_PATH,
    FacadeManifestError,
    MigrationState,
    PairKind,
    Shape,
    discover_package_modules,
    load_manifest,
    transitive_facade_defects,
    validate_checkout,
)

EXPECTED_SUFFIXES = (
    "",
    "_shared",
    "_shared.validation",
    "app_manifest",
    "app_manifest.models",
    "app_manifest.validation",
    "app_shell_bridge",
    "app_shell_bridge.models",
    "app_shell_bridge.validation",
    "component_contract",
    "component_contract.models",
    "component_contract.validation",
    "control_plane",
    "control_plane.imports",
    "control_plane.models",
    "control_plane.validation",
    "graph",
    "graph.models",
    "graph.search_models",
    "ingestion",
    "ingestion.models",
    "ingestion.watcher",
    "ingestion.watcher.models",
    "knowledge",
    "knowledge.models",
    "knowledge.normalize",
    "knowledge.validation",
    "lifecycle",
    "lifecycle.models",
    "lifecycle.rules",
    "memory",
    "memory.models",
    "memory_graph",
    "memory_graph.assembly",
    "memory_graph.fixtures",
    "memory_graph.models",
    "memory_graph.validation",
    "module_manifest",
    "module_manifest.models",
    "module_manifest.validation",
    "provenance",
    "provenance.models",
    "run_ledger",
    "run_ledger.models",
    "run_ledger.validation",
    "workspace",
    "workspace.models",
)

EXPECTED_RUNTIME_ONLY = (
    "omnivia_memory.control_plane.registry",
    "omnivia_memory.graph.repository",
    "omnivia_memory.graph.search_service",
    "omnivia_memory.graph.service",
    "omnivia_memory.ingestion.chunker",
    "omnivia_memory.ingestion.extractors",
    "omnivia_memory.ingestion.pipeline",
    "omnivia_memory.ingestion.repositories",
    "omnivia_memory.ingestion.scanner",
    "omnivia_memory.ingestion.watcher.debouncer",
    "omnivia_memory.ingestion.watcher.tracker",
    "omnivia_memory.memory.service",
    "omnivia_memory.memory_graph.ingestion_adapter",
    "omnivia_memory.memory_graph.store",
    "omnivia_memory.persistence",
    "omnivia_memory.persistence.database",
    "omnivia_memory.persistence.repositories",
    "omnivia_memory.search",
    "omnivia_memory.search.service",
    "omnivia_memory.workspace.repository",
    "omnivia_memory.workspace.service",
)

EXPECTED_HYBRID_SUFFIXES = {
    "graph",
    "ingestion",
    "ingestion.watcher",
    "memory",
    "memory_graph",
    "workspace",
}

EXPECTED_STATE_SUFFIXES = {
    MigrationState.CANONICAL_SUBSET: {"graph.search_models"},
    MigrationState.DIRECT_FACADE: {
        "_shared.validation",
        "app_manifest.models",
        "app_manifest.validation",
        "app_shell_bridge.models",
        "app_shell_bridge.validation",
        "component_contract.models",
        "component_contract.validation",
        "lifecycle.models",
        "lifecycle.rules",
        "memory.models",
        "module_manifest.models",
        "module_manifest.validation",
        "provenance.models",
    },
    MigrationState.TRANSITIVE_FACADE: {
        "_shared",
        "app_manifest",
        "app_shell_bridge",
        "component_contract",
        "lifecycle",
        "module_manifest",
        "provenance",
    },
    MigrationState.PENDING_DIRECT_BARREL: {
        "control_plane",
        "knowledge",
        "run_ledger",
    },
    MigrationState.PENDING_HYBRID: EXPECTED_HYBRID_SUFFIXES,
    MigrationState.PENDING_ROOT: {""},
}
EXPECTED_STATE_SUFFIXES[MigrationState.SOURCE_PARITY] = (
    set(EXPECTED_SUFFIXES)
    - set().union(*EXPECTED_STATE_SUFFIXES.values())
)


def _document() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _route(document: dict[str, Any], suffix: str) -> dict[str, Any]:
    legacy = "omnivia_memory" + (f".{suffix}" if suffix else "")
    return next(route for route in document["routes"] if route["legacy_module"] == legacy)


def _assert_rejected(
    mutate: Callable[[dict[str, Any]], None],
    pattern: str,
) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(FacadeManifestError, match=pattern):
        load_manifest(document)


def test_manifest_exact_route_and_runtime_sets_are_independently_pinned() -> None:
    manifest = load_manifest()
    expected_legacy = tuple(
        "omnivia_memory" + (f".{suffix}" if suffix else "")
        for suffix in EXPECTED_SUFFIXES
    )
    expected_canonical = tuple(
        "omnivia_core" + (f".{suffix}" if suffix else "")
        for suffix in EXPECTED_SUFFIXES
    )
    assert manifest.legacy_modules == tuple(sorted(expected_legacy))
    assert manifest.canonical_modules == tuple(sorted(expected_canonical))
    assert manifest.runtime_only_modules == EXPECTED_RUNTIME_ONLY


def test_manifest_partitions_and_current_states_are_exact() -> None:
    manifest = load_manifest()
    assert manifest.observed_counts().as_dict() == {
        "routes": 47,
        "direct": 40,
        "hybrid_barrel": 6,
        "root": 1,
        "leaf": 30,
        "barrel": 16,
        "runtime_only_modules": 21,
    }
    assert {route.suffix for route in manifest.by_kind(PairKind.HYBRID_BARREL)} == (
        EXPECTED_HYBRID_SUFFIXES
    )
    for state, expected in EXPECTED_STATE_SUFFIXES.items():
        assert {route.suffix for route in manifest.by_state(state)} == expected

    split = manifest.route_for_legacy("omnivia_memory.graph.search_models")
    assert split.pair_kind is PairKind.DIRECT
    assert split.shape is Shape.LEAF
    assert split.migration_state is MigrationState.CANONICAL_SUBSET


def test_manifest_views_are_immutable_and_deterministic() -> None:
    manifest = load_manifest()
    assert manifest.root.legacy_module == "omnivia_memory"
    assert manifest.route_for_canonical("omnivia_core.memory.models").suffix == "memory.models"
    assert manifest.by_shape(Shape.ROOT) == (manifest.root,)
    with pytest.raises(TypeError):
        manifest.by_legacy_module["omnivia_memory.extra"] = manifest.root  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        manifest.format_version = 2  # type: ignore[misc]


def test_loading_registry_does_not_import_either_package() -> None:
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(REPO_ROOT)!r})",
            "from baseline.facade_manifest import load_manifest",
            "load_manifest()",
            "assert not any(n == 'omnivia_core' or n.startswith('omnivia_core.') for n in sys.modules)",
            "assert not any(n == 'omnivia_memory' or n.startswith('omnivia_memory.') for n in sys.modules)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_schema_is_valid_and_accepts_the_committed_document() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    document = _document()
    validator = jsonschema.Draft202012Validator
    validator.check_schema(schema)
    validator(schema).validate(document)


@pytest.mark.parametrize(
    ("mutate", "pattern"),
    [
        (lambda d: d.__setitem__("extra", True), "unknown keys"),
        (lambda d: d.pop("routes"), "missing keys"),
        (lambda d: d.__setitem__("format_version", True), "format_version"),
        (lambda d: d["expected_counts"].__setitem__("routes", True), "expected_counts.routes"),
        (lambda d: _route(d, "workspace.models").__setitem__("extra", 1), "unknown keys"),
        (lambda d: _route(d, "workspace.models").pop("shape"), "missing keys"),
        (lambda d: _route(d, "workspace.models").__setitem__("shape", "wat"), "not one of"),
        (lambda d: _route(d, "workspace.models").__setitem__("pair_kind", 1), "expected a string"),
        (
            lambda d: _route(d, "workspace.models").__setitem__(
                "canonical_module", "other.workspace.models"
            ),
            "not a module",
        ),
        (
            lambda d: _route(d, "workspace.models").__setitem__(
                "canonical_module", "omnivia_core.workspace.other"
            ),
            "suffix mismatch",
        ),
        (lambda d: d["routes"].append(copy.deepcopy(d["routes"][-1])), "duplicate legacy_module"),
        (
            lambda d: d["routes"][1].__setitem__(
                "canonical_module", d["routes"][2]["canonical_module"]
            ),
            "duplicate canonical_module",
        ),
        (
            lambda d: d["runtime_only_modules"].append(d["runtime_only_modules"][-1]),
            "duplicate runtime_only_modules",
        ),
        (
            lambda d: d["runtime_only_modules"].append("other.runtime"),
            "not a module",
        ),
        (
            lambda d: d["runtime_only_modules"].append("omnivia_memory.workspace.models"),
            "also declared as routes",
        ),
        (lambda d: d["routes"].reverse(), "ordered"),
        (lambda d: d["runtime_only_modules"].reverse(), "ordered"),
        (lambda d: d["expected_counts"].__setitem__("routes", 46), "pinned 46"),
        (
            lambda d: _route(d, "").__setitem__("shape", "barrel"),
            "valid combination|exactly one root",
        ),
        (
            lambda d: _route(d, "workspace.models").__setitem__(
                "migration_state", "pending_root"
            ),
            "valid combination",
        ),
        (
            lambda d: _route(d, "workspace.models").__setitem__("shape", "barrel"),
            "no child routes",
        ),
    ],
)
def test_loader_rejects_structural_mutations(
    mutate: Callable[[dict[str, Any]], None],
    pattern: str,
) -> None:
    _assert_rejected(mutate, pattern)


def test_schema_and_loader_both_reject_unknown_route_key() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    document = _document()
    _route(document, "workspace.models")["unexpected"] = True
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(document))
    with pytest.raises(FacadeManifestError):
        load_manifest(document)


def test_checkout_and_route_source_validation_pass() -> None:
    modules = validate_checkout()
    assert len(modules.shared) == 47
    assert len(modules.legacy_only) == 21


@pytest.mark.parametrize(
    ("suffix", "state", "pattern"),
    [
        ("app_shell_bridge.models", "source_parity", "move the state forward"),
        ("app_manifest.models", "source_parity", "move the state forward"),
        ("app_manifest.validation", "source_parity", "move the state forward"),
        ("component_contract.models", "source_parity", "move the state forward"),
        ("component_contract.validation", "source_parity", "move the state forward"),
        ("module_manifest.models", "source_parity", "move the state forward"),
        ("module_manifest.validation", "source_parity", "move the state forward"),
        # The mirror direction: a leaf that is still a duplicated source-parity
        # copy may not be declared as a converted facade either.
        ("knowledge.models", "direct_facade", "declared 'direct_facade'"),
    ],
)
def test_checkout_rejects_source_state_swaps(
    suffix: str,
    state: str,
    pattern: str,
) -> None:
    document = _document()
    _route(document, suffix)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match=pattern):
        validate_checkout(manifest=document)


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        ("class LocalCopy:\n    pass\n", "statements of its own"),
        ("ValidationResult = object()\n", "assignments"),
        (
            "from omnivia_core._shared.validation import ValidationResult\n",
            "unapproved module",
        ),
        (
            "from omnivia_memory.persistence import Database\n",
            "unapproved module",
        ),
    ],
)
def test_transitive_facade_rejects_local_or_unapproved_source(
    extra_source: str,
    pattern: str,
) -> None:
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory._shared")
    child = manifest.route_for_legacy("omnivia_memory._shared.validation")
    source = (
        "from omnivia_memory._shared.validation import ValidationResult\n"
        '__all__ = ["ValidationResult"]\n'
        f"{extra_source}"
    )
    defects = transitive_facade_defects(ast.parse(source), route, [child])
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    "child_suffix",
    [
        "_shared.validation",
        "app_manifest.models",
        "app_manifest.validation",
        "component_contract.models",
        "component_contract.validation",
        "module_manifest.models",
        "module_manifest.validation",
    ],
)
def test_transitive_facade_rejects_an_unconverted_child(child_suffix: str) -> None:
    document = _document()
    _route(document, child_suffix)["migration_state"] = "source_parity"
    with pytest.raises(FacadeManifestError, match="routed children are not converted"):
        validate_checkout(manifest=document)


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        ("from omnivia_core.app_manifest.models import AppManifest\n", "unapproved module"),
        ("from omnivia_memory.knowledge import ValidationResult\n", "unapproved module"),
        ("AppState = object()\n", "assignments"),
        ("def validate_app_manifest(data):\n    return data\n", "statements of its own"),
    ],
)
def test_app_manifest_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The app-manifest barrel earns ``transitive_facade`` by re-exporting only
    its two converted children. Reaching into ``omnivia_core`` itself, pulling a
    third module, or defining anything of its own must be rejected -- each of
    those would make the barrel's identity preservation direct or local rather
    than transitive, while still exporting the same seven names."""
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory.app_manifest")
    children = [
        manifest.route_for_legacy("omnivia_memory.app_manifest.models"),
        manifest.route_for_legacy("omnivia_memory.app_manifest.validation"),
    ]
    source = (
        "from omnivia_memory.app_manifest.models import (\n"
        "    AppState,\n"
        "    AppManifest,\n"
        "    DataSource,\n"
        "    ProvenanceRequirement,\n"
        "    ValidationResult,\n"
        ")\n"
        "from omnivia_memory.app_manifest.validation import (\n"
        "    AppManifestValidationError,\n"
        "    validate_app_manifest,\n"
        ")\n"
        '__all__ = [\n'
        '    "AppManifest",\n'
        '    "AppManifestValidationError",\n'
        '    "AppState",\n'
        '    "DataSource",\n'
        '    "ProvenanceRequirement",\n'
        '    "ValidationResult",\n'
        '    "validate_app_manifest",\n'
        "]\n"
        f"{extra_source}"
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


#: The legacy module-manifest barrel's exact historical export sets, per child,
#: restated here rather than read off the barrel: the fixture below must not be
#: derived from the very file whose mutations it is proving get rejected. Like
#: the app-manifest barrel (and unlike the component-contract one) it re-exports
#: through *absolute* ``omnivia_memory.module_manifest.<leaf>`` imports.
_MODULE_MANIFEST_MODELS_EXPORTS: tuple[str, ...] = (
    "Entrypoint",
    "Integrity",
    "ModuleKind",
    "ModuleManifest",
    "Permission",
    "PublishedTarget",
)
_MODULE_MANIFEST_VALIDATION_EXPORTS: tuple[str, ...] = (
    "ModuleManifestValidationError",
    "validate_module_manifest",
)


def _module_manifest_barrel_source(extra_source: str) -> str:
    def block(module: str, names: tuple[str, ...]) -> str:
        body = "".join(f"    {name},\n" for name in names)
        return f"from omnivia_memory.module_manifest.{module} import (\n{body})\n"

    exported = sorted(
        _MODULE_MANIFEST_MODELS_EXPORTS + _MODULE_MANIFEST_VALIDATION_EXPORTS
    )
    all_body = "".join(f'    "{name}",\n' for name in exported)
    return (
        block("models", _MODULE_MANIFEST_MODELS_EXPORTS)
        + block("validation", _MODULE_MANIFEST_VALIDATION_EXPORTS)
        + f"__all__ = [\n{all_body}]\n"
        + extra_source
    )


def _module_manifest_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.module_manifest"),
        [
            manifest.route_for_legacy("omnivia_memory.module_manifest.models"),
            manifest.route_for_legacy("omnivia_memory.module_manifest.validation"),
        ],
    )


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.module_manifest.models import ModuleManifest\n",
            "unapproved module",
        ),
        ("from omnivia_memory.knowledge import ValidationResult\n", "unapproved module"),
        ("from .persistence import Database\n", "unapproved module"),
        ("ModuleKind = object()\n", "assignments"),
        ("def validate_module_manifest(data):\n    return data\n", "statements of its own"),
    ],
)
def test_module_manifest_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The module-manifest barrel earns ``transitive_facade`` by re-exporting only
    its two converted children. Reaching into ``omnivia_core`` itself, pulling a
    third module by either an absolute or a relative path, or defining anything
    of its own must be rejected -- each of those would make the barrel's identity
    preservation direct or local rather than transitive, while still exporting
    the same eight names."""
    route, children = _module_manifest_barrel_route_and_children()
    source = _module_manifest_barrel_source(extra_source)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_module_manifest_barrel_transitive_form_accepts_its_historical_source() -> None:
    """The same fixture with nothing added must be defect-free, so the rejection
    cases above are proven to fail on what they inject rather than on the
    barrel's absolute-import shape itself."""
    route, children = _module_manifest_barrel_route_and_children()
    source = _module_manifest_barrel_source("")
    assert transitive_facade_defects(ast.parse(source), route, children) == []


#: The legacy component-contract barrel's exact historical export sets, per
#: child, restated here rather than read off the barrel: the fixture below must
#: not be derived from the very file whose mutations it is proving get rejected.
#: Unlike the app-manifest barrel, this one re-exports through *relative*
#: imports, which ``transitive_facade_defects`` has to resolve against the
#: barrel's own package before it can tell an approved child from a reroute.
_COMPONENT_CONTRACT_MODELS_EXPORTS: tuple[str, ...] = (
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
)
_COMPONENT_CONTRACT_VALIDATION_EXPORTS: tuple[str, ...] = (
    "ComponentContractValidationError",
    "validate_agent_run_record",
    "validate_component_contract",
)


def _component_contract_barrel_source(extra_source: str) -> str:
    def block(module: str, names: tuple[str, ...]) -> str:
        body = "".join(f"    {name},\n" for name in names)
        return f"from .{module} import (\n{body})\n"

    exported = _COMPONENT_CONTRACT_MODELS_EXPORTS + _COMPONENT_CONTRACT_VALIDATION_EXPORTS
    all_body = "".join(f'    "{name}",\n' for name in exported)
    return (
        block("models", _COMPONENT_CONTRACT_MODELS_EXPORTS)
        + block("validation", _COMPONENT_CONTRACT_VALIDATION_EXPORTS)
        + f"__all__ = [\n{all_body}]\n"
        + extra_source
    )


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.component_contract.models import ComponentContract\n",
            "unapproved module",
        ),
        ("from omnivia_memory.knowledge import ValidationResult\n", "unapproved module"),
        ("from .persistence import Database\n", "unapproved module"),
        ("ComponentFamily = object()\n", "assignments"),
        ("def validate_component_contract(data):\n    return data\n", "statements of its own"),
    ],
)
def test_component_contract_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The component-contract barrel earns ``transitive_facade`` by re-exporting
    only its two converted children, through the relative imports it has always
    used. Reaching into ``omnivia_core`` itself, pulling a third module by either
    an absolute or a relative path, or defining anything of its own must be
    rejected -- each of those would make the barrel's identity preservation
    direct or local rather than transitive, while still exporting the same 26
    names."""
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory.component_contract")
    children = [
        manifest.route_for_legacy("omnivia_memory.component_contract.models"),
        manifest.route_for_legacy("omnivia_memory.component_contract.validation"),
    ]
    source = _component_contract_barrel_source(extra_source)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_component_contract_barrel_transitive_form_accepts_its_historical_source() -> None:
    """The same fixture with nothing added must be defect-free, so the rejection
    cases above are proven to fail on what they inject rather than on the
    relative-import shape itself."""
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory.component_contract")
    children = [
        manifest.route_for_legacy("omnivia_memory.component_contract.models"),
        manifest.route_for_legacy("omnivia_memory.component_contract.validation"),
    ]
    source = _component_contract_barrel_source("")
    assert transitive_facade_defects(ast.parse(source), route, children) == []


def test_validate_checkout_revalidates_public_dataclass_instances() -> None:
    manifest = load_manifest()
    forged = replace(manifest, routes=(manifest.routes[0], manifest.routes[0]))
    with pytest.raises(FacadeManifestError, match="duplicate legacy_module"):
        validate_checkout(manifest=forged)


def _copy_package_trees(destination: Path) -> None:
    shutil.copytree(
        REPO_ROOT / "src" / "omnivia_core",
        destination / "src" / "omnivia_core",
    )
    shutil.copytree(
        REPO_ROOT / "services" / "omnivia-memory" / "src" / "omnivia_memory",
        destination / "services" / "omnivia-memory" / "src" / "omnivia_memory",
    )


def test_discovery_includes_arbitrary_dunder_modules(tmp_path: Path) -> None:
    package = tmp_path / "src" / "sample"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__probe__.py").write_text("", encoding="utf-8")
    assert discover_package_modules(tmp_path / "src", "sample")["__probe__"] is Shape.LEAF


@pytest.mark.parametrize("drift", ["shared", "legacy-only", "shape"])
def test_checkout_rejects_package_tree_drift(tmp_path: Path, drift: str) -> None:
    _copy_package_trees(tmp_path)
    legacy = tmp_path / "services" / "omnivia-memory" / "src" / "omnivia_memory"
    canonical = tmp_path / "src" / "omnivia_core"
    if drift == "shared":
        (legacy / "__probe__.py").write_text("", encoding="utf-8")
        (canonical / "__probe__.py").write_text("", encoding="utf-8")
        pattern = "shared modules with no frozen route"
    elif drift == "legacy-only":
        (legacy / "__probe__.py").write_text("", encoding="utf-8")
        pattern = "legacy-only modules not declared runtime-only"
    else:
        model = legacy / "workspace" / "models.py"
        model.unlink()
        model_package = legacy / "workspace" / "models"
        model_package.mkdir()
        (model_package / "__init__.py").write_text("", encoding="utf-8")
        pattern = "declared shape"
    with pytest.raises(FacadeManifestError, match=pattern):
        validate_checkout(tmp_path)


def test_checker_is_a_successful_executable_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check-facade-routes.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "routes: 47 (40 direct, 6 hybrid_barrel, 1 root)" in result.stdout
    assert "canonical_subset: 1" in result.stdout
    # The per-state counts this batch moved: the two module-manifest leaves into
    # ``direct_facade``, their barrel from ``pending_direct_barrel`` into
    # ``transitive_facade``.
    assert "direct_facade: 13" in result.stdout
    assert "transitive_facade: 7" in result.stdout
    assert "pending_direct_barrel: 3" in result.stdout
