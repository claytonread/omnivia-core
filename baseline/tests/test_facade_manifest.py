"""Fail-closed coverage for the frozen compatibility-facade route registry."""

from __future__ import annotations

import ast
import copy
import importlib.util
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
    direct_facade_defects,
    discover_package_modules,
    load_manifest,
    split_facade_defects,
    transitive_facade_defects,
    validate_checkout,
    validate_route_sources,
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
    # Empty on purpose: ``graph.search_models`` was the last leaf whose canonical
    # counterpart was a strict subset of it, and it is now the first
    # ``split_facade``. The state stays listed rather than dropped so the partition
    # below keeps asserting that nothing has quietly re-entered it.
    MigrationState.CANONICAL_SUBSET: set(),
    #: The one split facade: canonical records, four legacy-owned scoring helpers.
    MigrationState.SPLIT_FACADE: {"graph.search_models"},
    MigrationState.DIRECT_FACADE: {
        "_shared.validation",
        "app_manifest.models",
        "app_manifest.validation",
        "app_shell_bridge.models",
        "app_shell_bridge.validation",
        "component_contract.models",
        "component_contract.validation",
        "control_plane.imports",
        "control_plane.models",
        "control_plane.validation",
        # The models half of the Graph pair. Its sibling ``search_models`` is a
        # ``split_facade`` above, and their barrel stays a hybrid below.
        "graph.models",
        # The two ingestion leaves. Both barrels above them
        # (``ingestion`` and ``ingestion.watcher``) stay hybrids below, because
        # the rest of each barrel's surface is owned by runtime-only leaves.
        "ingestion.models",
        "ingestion.watcher.models",
        "knowledge.models",
        "knowledge.normalize",
        "knowledge.validation",
        "lifecycle.models",
        "lifecycle.rules",
        "memory.models",
        # The second converted leaf set whose barrel stays a hybrid, after
        # ``memory.models`` above: ``memory_graph`` is still ``pending_hybrid``
        # below, because its other two children are runtime-only.
        "memory_graph.assembly",
        "memory_graph.fixtures",
        "memory_graph.models",
        "memory_graph.validation",
        "module_manifest.models",
        "module_manifest.validation",
        "provenance.models",
        "run_ledger.models",
        "run_ledger.validation",
        # The last leaf of all. Its barrel stays a hybrid below, because
        # ``WorkspaceRepository`` and ``WorkspaceService`` are owned by the
        # runtime-only ``repository``/``service`` leaves.
        "workspace.models",
    },
    MigrationState.TRANSITIVE_FACADE: {
        "_shared",
        "app_manifest",
        "app_shell_bridge",
        "component_contract",
        "control_plane",
        "knowledge",
        "lifecycle",
        "module_manifest",
        "provenance",
        "run_ledger",
    },
    # Empty on purpose: ``knowledge`` was the last barrel pending direct
    # conversion, so every ``direct`` barrel is now a ``transitive_facade`` and
    # only the six ``hybrid_barrel`` barrels and the package root are still
    # pending. The state stays listed rather than dropped so the partition below
    # keeps asserting that nothing has quietly re-entered it.
    MigrationState.PENDING_DIRECT_BARREL: set(),
    MigrationState.PENDING_HYBRID: EXPECTED_HYBRID_SUFFIXES,
    MigrationState.PENDING_ROOT: {""},
}
#: Derived rather than listed, so it is whatever the sets above do not claim.
#: It is empty now -- ``workspace.models`` was the last duplicated leaf -- which
#: is exactly why it stays derived: an entry dropped from ``DIRECT_FACADE``
#: above reappears here and fails the partition, instead of vanishing.
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
    assert split.migration_state is MigrationState.SPLIT_FACADE
    assert split.is_converted

    models = manifest.route_for_legacy("omnivia_memory.graph.models")
    assert models.pair_kind is PairKind.DIRECT
    assert models.shape is Shape.LEAF
    assert models.migration_state is MigrationState.DIRECT_FACADE
    assert models.is_converted


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
        ("control_plane.imports", "source_parity", "move the state forward"),
        ("control_plane.models", "source_parity", "move the state forward"),
        ("control_plane.validation", "source_parity", "move the state forward"),
        # The two ingestion leaves. Like the memory-graph four, their parent
        # barrels stay hybrids, so a relabelling is caught only by their *own*
        # source gate.
        ("ingestion.models", "source_parity", "move the state forward"),
        ("ingestion.watcher.models", "source_parity", "move the state forward"),
        ("knowledge.models", "source_parity", "move the state forward"),
        ("knowledge.normalize", "source_parity", "move the state forward"),
        ("knowledge.validation", "source_parity", "move the state forward"),
        # The four memory-graph leaves. Their parent barrel stays a hybrid, so
        # these are the first converted children whose relabelling is caught only
        # by their *own* source gate -- no ``transitive_facade`` parent above them
        # would notice an unconverted child.
        ("memory_graph.assembly", "source_parity", "move the state forward"),
        ("memory_graph.fixtures", "source_parity", "move the state forward"),
        ("memory_graph.models", "source_parity", "move the state forward"),
        ("memory_graph.validation", "source_parity", "move the state forward"),
        ("module_manifest.models", "source_parity", "move the state forward"),
        ("module_manifest.validation", "source_parity", "move the state forward"),
        ("run_ledger.models", "source_parity", "move the state forward"),
        ("run_ledger.validation", "source_parity", "move the state forward"),
        # The last leaf, and the one whose barrel is the sixth hybrid: like the
        # ingestion pair and the memory-graph four, a relabelling is caught only
        # by its *own* source gate.
        ("workspace.models", "source_parity", "move the state forward"),
        # The mirror direction used to be ``workspace.models`` declared
        # ``direct_facade`` while its source was still a duplicated copy. That
        # leaf is now the facade, and it was the last duplicated one, so no
        # unconverted leaf is left to point at. The rule itself is unchanged and
        # still covered from the other side by ``graph.search_models``, whose
        # split source is rejected when it is declared ``direct_facade``
        # (``test_a_split_source_is_not_a_direct_facade`` and
        # ``test_validate_route_sources_enforces_the_graph_states`` below).
        # Restoring a case
        # here would mean reintroducing a duplicated leaf, which this slice
        # exists to end.
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
        "control_plane.imports",
        "control_plane.models",
        "control_plane.validation",
        "knowledge.models",
        "knowledge.normalize",
        "knowledge.validation",
        "module_manifest.models",
        "module_manifest.validation",
        "run_ledger.models",
        "run_ledger.validation",
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


#: The legacy run-ledger barrel's exact historical export sets, per child,
#: restated here rather than read off the barrel: the fixture below must not be
#: derived from the very file whose mutations it is proving get rejected. Like
#: the app-manifest and module-manifest barrels it re-exports through *absolute*
#: ``omnivia_memory.run_ledger.<leaf>`` imports -- but unlike either of them its
#: ``__all__`` is not alphabetized: the two constants lead the models block, so
#: the fixture below restates that literal order rather than sorting.
_RUN_LEDGER_MODELS_EXPORTS: tuple[str, ...] = (
    "RUN_LEDGER_CONTRACT_VERSION",
    "RUN_LEDGER_PATH_ENV",
    "EvidenceFileRef",
    "RunLedgerEntry",
    "RunLedgerProvenance",
    "RunLedgerStatus",
)
_RUN_LEDGER_VALIDATION_EXPORTS: tuple[str, ...] = (
    "TERMINAL_RUN_STATUSES",
    "validate_evidence_file_ref",
    "validate_run_ledger_entry",
    "validate_run_ledger_provenance",
)


def _run_ledger_barrel_source(extra_source: str) -> str:
    def block(module: str, names: tuple[str, ...]) -> str:
        body = "".join(f"    {name},\n" for name in names)
        return f"from omnivia_memory.run_ledger.{module} import (\n{body})\n"

    exported = _RUN_LEDGER_MODELS_EXPORTS + _RUN_LEDGER_VALIDATION_EXPORTS
    all_body = "".join(f'    "{name}",\n' for name in exported)
    return (
        block("models", _RUN_LEDGER_MODELS_EXPORTS)
        + block("validation", _RUN_LEDGER_VALIDATION_EXPORTS)
        + f"__all__ = [\n{all_body}]\n"
        + extra_source
    )


def _run_ledger_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.run_ledger"),
        [
            manifest.route_for_legacy("omnivia_memory.run_ledger.models"),
            manifest.route_for_legacy("omnivia_memory.run_ledger.validation"),
        ],
    )


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.run_ledger.models import RunLedgerEntry\n",
            "unapproved module",
        ),
        # The barrel's own domain reaches the knowledge leaf for
        # ``ContractVersion``, so a knowledge import is the most plausible
        # accidental third module here -- and still not one of its two children.
        ("from omnivia_memory.knowledge import ContractVersion\n", "unapproved module"),
        ("from .persistence import Database\n", "unapproved module"),
        ("RunLedgerStatus = object()\n", "assignments"),
        (
            "def validate_run_ledger_entry(entry):\n    return entry\n",
            "statements of its own",
        ),
    ],
)
def test_run_ledger_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The run-ledger barrel earns ``transitive_facade`` by re-exporting only its
    two converted children. Reaching into ``omnivia_core`` itself, pulling a
    third module by either an absolute or a relative path, or defining anything
    of its own must be rejected -- each of those would make the barrel's identity
    preservation direct or local rather than transitive, while still exporting
    the same ten names."""
    route, children = _run_ledger_barrel_route_and_children()
    source = _run_ledger_barrel_source(extra_source)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_run_ledger_barrel_transitive_form_accepts_its_historical_source() -> None:
    """The same fixture with nothing added must be defect-free, so the rejection
    cases above are proven to fail on what they inject rather than on the
    barrel's absolute-import shape or its unsorted ``__all__`` itself."""
    route, children = _run_ledger_barrel_route_and_children()
    source = _run_ledger_barrel_source("")
    assert transitive_facade_defects(ast.parse(source), route, children) == []


#: The legacy control-plane barrel's exact historical export sets, per child,
#: restated here rather than read off the barrel: the fixture below must not be
#: derived from the very file whose mutations it is proving get rejected. Unlike
#: every other barrel in this module it has *three* converted children, and its
#: ``__all__`` is neither alphabetized nor a concatenation of its three import
#: blocks -- it leads with the three constants, then interleaves each child's
#: names -- so the literal order is restated here too.
_CONTROL_PLANE_IMPORTS_EXPORTS: tuple[str, ...] = (
    "CatalogueArtifactVerification",
    "ImportSourceChange",
    "ImportSpecValidation",
    "ImportedCandidateSet",
    "detect_import_source_change",
    "import_asyncapi_candidates",
    "import_catalogue_candidates",
    "import_catalogue_generated_candidates",
    "import_mcp_candidates",
    "import_openapi_candidates",
    "validate_asyncapi_import_spec",
    "validate_mcp_import_spec",
    "validate_openapi_import_spec",
    "verify_catalogue_artifacts",
)
_CONTROL_PLANE_MODELS_EXPORTS: tuple[str, ...] = (
    "CONTROL_PLANE_CONTRACT_VERSION",
    "CONTROL_PLANE_SCHEMA_VERSION",
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
)
_CONTROL_PLANE_VALIDATION_EXPORTS: tuple[str, ...] = (
    "DANGEROUS_SIDE_EFFECTS",
    "ControlPlaneValidationError",
    "compile_policy_expression",
    "manifest_from_dict",
    "validate_control_plane_manifest",
)
#: The barrel's own literal ``__all__`` order, which matches none of the three
#: import blocks: three constants first, then the imports leaf's contract names,
#: then the models block with the validation leaf's error/compiler spliced in.
_CONTROL_PLANE_ALL_ORDER: tuple[str, ...] = (
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


def _control_plane_barrel_source(extra_source: str) -> str:
    def block(module: str, names: tuple[str, ...]) -> str:
        body = "".join(f"    {name},\n" for name in names)
        return f"from omnivia_memory.control_plane.{module} import (\n{body})\n"

    all_body = "".join(f'    "{name}",\n' for name in _CONTROL_PLANE_ALL_ORDER)
    return (
        block("imports", _CONTROL_PLANE_IMPORTS_EXPORTS)
        + block("models", _CONTROL_PLANE_MODELS_EXPORTS)
        + block("validation", _CONTROL_PLANE_VALIDATION_EXPORTS)
        + f"__all__ = [\n{all_body}]\n"
        + extra_source
    )


def _control_plane_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.control_plane"),
        [
            manifest.route_for_legacy("omnivia_memory.control_plane.imports"),
            manifest.route_for_legacy("omnivia_memory.control_plane.models"),
            manifest.route_for_legacy("omnivia_memory.control_plane.validation"),
        ],
    )


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.control_plane.models import ControlPlaneManifest\n",
            "unapproved module",
        ),
        # The control plane's own validation leaf reaches the knowledge domain for
        # ``check_contract_version_compatibility``, so a knowledge import is the
        # most plausible accidental extra module here -- and still not one of its
        # three children.
        (
            (
                "from omnivia_memory.knowledge import "
                "check_contract_version_compatibility\n"
            ),
            "unapproved module",
        ),
        # ``control_plane.registry`` is this barrel's own runtime-only sibling, so
        # a relative reach into it is the sharpest local-reroute case: it is a real
        # module of the same package that is deliberately not a route at all.
        ("from .registry import ControlPlaneRegistry\n", "unapproved module"),
        ("LifecycleState = object()\n", "assignments"),
        (
            "def validate_control_plane_manifest(manifest):\n    return manifest\n",
            "statements of its own",
        ),
    ],
)
def test_control_plane_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The control-plane barrel earns ``transitive_facade`` by re-exporting only
    its three converted children. Reaching into ``omnivia_core`` itself, pulling a
    fourth module by either an absolute or a relative path, or defining anything
    of its own must be rejected -- each of those would make the barrel's identity
    preservation direct or local rather than transitive, while still exporting the
    same 74 names."""
    route, children = _control_plane_barrel_route_and_children()
    source = _control_plane_barrel_source(extra_source)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_control_plane_barrel_transitive_form_accepts_its_historical_source() -> None:
    """The same fixture with nothing added must be defect-free, so the rejection
    cases above are proven to fail on what they inject rather than on the barrel's
    three-child absolute-import shape or its interleaved ``__all__`` itself."""
    route, children = _control_plane_barrel_route_and_children()
    source = _control_plane_barrel_source("")
    assert transitive_facade_defects(ast.parse(source), route, children) == []


def test_control_plane_barrel_transitive_form_requires_all_three_children() -> None:
    """Dropping any one child's import block must fail. With three children the
    "does not import converted children" branch is reachable in a way the
    two-child barrels never exercise: a barrel could keep a perfectly valid
    two-block shape and still have stopped re-exporting a converted leaf."""
    route, children = _control_plane_barrel_route_and_children()
    blocks = {
        "imports": _CONTROL_PLANE_IMPORTS_EXPORTS,
        "models": _CONTROL_PLANE_MODELS_EXPORTS,
        "validation": _CONTROL_PLANE_VALIDATION_EXPORTS,
    }
    for dropped in blocks:
        source = ""
        exported: list[str] = []
        for module, names in blocks.items():
            if module == dropped:
                continue
            body = "".join(f"    {name},\n" for name in names)
            source += (
                f"from omnivia_memory.control_plane.{module} import (\n{body})\n"
            )
            exported.extend(names)
        all_body = "".join(f'    "{name}",\n' for name in exported)
        source += f"__all__ = [\n{all_body}]\n"
        defects = transitive_facade_defects(ast.parse(source), route, children)
        assert any(
            "does not import converted children" in defect
            and f"omnivia_memory.control_plane.{dropped}" in defect
            for defect in defects
        ), (dropped, defects)


#: The legacy knowledge barrel's exact historical export sets, per child,
#: restated here rather than read off the barrel: the fixture below must not be
#: derived from the very file whose mutations it is proving get rejected. Like
#: the control-plane barrel it has *three* converted children and re-exports
#: through *absolute* ``omnivia_memory.knowledge.<leaf>`` imports; unlike it, the
#: ``__all__`` is fully sorted rather than interleaved.
#:
#: Note what the normalize block does *not* name: ``normalize_extension_value``
#: is a routed symbol of that leaf which this barrel has never re-exported. A
#: barrel is allowed to publish a subset of its children's surface; what it may
#: not do is publish something it did not import, which is why the imported set
#: and ``__all__`` are compared to each other below.
_KNOWLEDGE_MODELS_EXPORTS: tuple[str, ...] = (
    "BUILTIN_GRAPH_NODE_KINDS",
    "BUILTIN_GRAPH_RELATIONS",
    "BUILTIN_OBJECT_KINDS",
    "EXTENSION_MANIFEST_CONTRACT_VERSION",
    "GRAPH_CONTRACT_VERSION",
    "KNOWLEDGE_CONTRACT_VERSION",
    "AgentGraphContext",
    "ContractVersion",
    "GraphConfidence",
    "GraphEdge",
    "GraphEvidenceStrength",
    "GraphFragment",
    "GraphNode",
    "GraphOrigin",
    "GraphReviewStatus",
    "GraphSensitivity",
    "GraphSourceType",
    "GraphVisibility",
    "KnowledgeClaim",
    "KnowledgeCollection",
    "KnowledgeExtensionManifest",
    "KnowledgeLink",
    "KnowledgeObject",
    "KnowledgeSource",
    "KnowledgeSpace",
    "SourceRef",
)
_KNOWLEDGE_NORMALIZE_EXPORTS: tuple[str, ...] = (
    "normalize_graph_edge_id",
    "normalize_graph_node_id",
    "normalize_graph_node_kind",
    "normalize_graph_relation",
    "normalize_identifier",
    "normalize_label",
    "normalize_object_id",
    "normalize_object_kind",
    "normalize_source_path",
    "normalize_space_id",
    "normalize_tags",
)
#: ``ValidationResult`` leads this block. The knowledge validation leaf never
#: owned a class of that name -- it imports the shared primitive -- so this is the
#: one export in the barrel whose object comes from outside the knowledge domain
#: entirely, and it is the binding the legacy package root has always taken its
#: ``ValidationResult`` from.
_KNOWLEDGE_VALIDATION_EXPORTS: tuple[str, ...] = (
    "ValidationResult",
    "check_contract_version_compatibility",
    "summarize_confidence",
    "summarize_review_status",
    "summarize_sensitivity",
    "validate_agent_graph_context",
    "validate_graph_edge",
    "validate_graph_fragment",
    "validate_graph_node",
    "validate_knowledge_claim",
    "validate_knowledge_collection",
    "validate_knowledge_extension_manifest",
    "validate_knowledge_link",
    "validate_knowledge_object",
    "validate_knowledge_source",
    "validate_knowledge_space",
    "validate_source_ref",
)


def _knowledge_barrel_source(extra_source: str) -> str:
    def block(module: str, names: tuple[str, ...]) -> str:
        body = "".join(f"    {name},\n" for name in names)
        return f"from omnivia_memory.knowledge.{module} import (\n{body})\n"

    exported = sorted(
        _KNOWLEDGE_MODELS_EXPORTS
        + _KNOWLEDGE_NORMALIZE_EXPORTS
        + _KNOWLEDGE_VALIDATION_EXPORTS
    )
    all_body = "".join(f'    "{name}",\n' for name in exported)
    return (
        block("models", _KNOWLEDGE_MODELS_EXPORTS)
        + block("normalize", _KNOWLEDGE_NORMALIZE_EXPORTS)
        + block("validation", _KNOWLEDGE_VALIDATION_EXPORTS)
        + f"__all__ = [\n{all_body}]\n"
        + extra_source
    )


def _knowledge_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.knowledge"),
        [
            manifest.route_for_legacy("omnivia_memory.knowledge.models"),
            manifest.route_for_legacy("omnivia_memory.knowledge.normalize"),
            manifest.route_for_legacy("omnivia_memory.knowledge.validation"),
        ],
    )


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.knowledge.models import KnowledgeSpace\n",
            "unapproved module",
        ),
        # The knowledge validation leaf reaches the shared primitive for
        # ``ValidationResult``, so a ``_shared`` import is the most plausible
        # accidental extra module here -- and still not one of its three children.
        (
            "from omnivia_memory._shared.validation import ValidationResult\n",
            "unapproved module",
        ),
        # An absolute reach at the canonical shared primitive: the same reroute
        # from the other side of the package boundary.
        (
            "from omnivia_core._shared.validation import ValidationResult\n",
            "unapproved module",
        ),
        # A relative reach into a runtime-only sibling that is deliberately not a
        # route at all.
        ("from ..persistence import Database\n", "unapproved module"),
        # A *relative* form of one of its own children. The resolver does
        # approve this module -- it resolves ``.models`` against the barrel's own
        # package -- so what rejects the mutation is the binding/``__all__``
        # mismatch it creates, not the module gate. Pinned here so the two
        # branches are not confused for each other.
        (
            "from .models import KnowledgeSpace\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("ContractVersion = object()\n", "assignments"),
        (
            "def validate_knowledge_space(space):\n    return space\n",
            "statements of its own",
        ),
        ("class KnowledgeSpace:\n    pass\n", "statements of its own"),
    ],
)
def test_knowledge_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The knowledge barrel earns ``transitive_facade`` by re-exporting only its
    three converted children. Reaching into ``omnivia_core`` itself, pulling a
    fourth module by either an absolute or a relative path, or defining anything
    of its own must be rejected -- each of those would make the barrel's identity
    preservation direct or local rather than transitive, while still exporting the
    same 54 names."""
    route, children = _knowledge_barrel_route_and_children()
    source = _knowledge_barrel_source(extra_source)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_knowledge_barrel_transitive_form_accepts_its_historical_source() -> None:
    """The same fixture with nothing added must be defect-free, so the rejection
    cases above are proven to fail on what they inject rather than on the barrel's
    three-child absolute-import shape or its sorted ``__all__`` itself."""
    route, children = _knowledge_barrel_route_and_children()
    source = _knowledge_barrel_source("")
    assert transitive_facade_defects(ast.parse(source), route, children) == []


def test_knowledge_barrel_transitive_form_requires_all_three_children() -> None:
    """Dropping any one child's import block must fail. With three children the
    "does not import converted children" branch is reachable in a way the
    two-child barrels never exercise: a barrel could keep a perfectly valid
    two-block shape and still have stopped re-exporting a converted leaf."""
    route, children = _knowledge_barrel_route_and_children()
    blocks = {
        "models": _KNOWLEDGE_MODELS_EXPORTS,
        "normalize": _KNOWLEDGE_NORMALIZE_EXPORTS,
        "validation": _KNOWLEDGE_VALIDATION_EXPORTS,
    }
    for dropped in blocks:
        source = ""
        exported: list[str] = []
        for module, names in blocks.items():
            if module == dropped:
                continue
            body = "".join(f"    {name},\n" for name in names)
            source += f"from omnivia_memory.knowledge.{module} import (\n{body})\n"
            exported.extend(names)
        all_body = "".join(f'    "{name}",\n' for name in sorted(exported))
        source += f"__all__ = [\n{all_body}]\n"
        defects = transitive_facade_defects(ast.parse(source), route, children)
        assert any(
            "does not import converted children" in defect
            and f"omnivia_memory.knowledge.{dropped}" in defect
            for defect in defects
        ), (dropped, defects)


def test_knowledge_barrel_transitive_form_rejects_an_extra_import_of_a_child() -> None:
    """A second import block for an already-imported child is not a reroute --
    every module named is approved -- but it is still an extra import statement
    the historical source does not have, and it makes the barrel's ``__all__``
    disagree with the set of names it binds. That mismatch is what must fail."""
    route, children = _knowledge_barrel_route_and_children()
    source = _knowledge_barrel_source(
        "from omnivia_memory.knowledge.normalize import normalize_extension_value\n"
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(
        "the imported binding set does not exactly match the literal __all__"
        in defect
        for defect in defects
    ), defects


def test_knowledge_barrel_state_cannot_be_walked_back_to_a_pending_barrel() -> None:
    """``pending_direct_barrel`` is now empty, and the knowledge barrel may not
    quietly re-enter it: a pending state skips the source gate entirely, so
    declaring it would stop checking that the barrel still routes purely through
    its three converted children.

    The registry's own combination rule is what forbids this: a ``direct``
    barrel whose children are all converted has no legitimate pending state
    left, and ``pending_hybrid``/``pending_root`` are not valid for its
    kind/shape pair.
    """
    for state in ("pending_hybrid", "pending_root"):
        document = _document()
        _route(document, "knowledge")["migration_state"] = state
        with pytest.raises(FacadeManifestError, match="valid combination"):
            load_manifest(document)

    # ``pending_direct_barrel`` is structurally valid for this pair, so it loads
    # -- and then the checkout gate has nothing left to check, which is exactly
    # why the state must not be walked back. Pin that it is no longer the state
    # the committed registry declares.
    assert (
        load_manifest().route_for_legacy("omnivia_memory.knowledge").migration_state
        is MigrationState.TRANSITIVE_FACADE
    )
    assert load_manifest().by_state(MigrationState.PENDING_DIRECT_BARREL) == ()


#: The legacy ``memory_graph`` barrel's exact historical export sets, per child,
#: restated here rather than read off the barrel. This is the repository's first
#: barrel with *converted children and a hybrid state*: four of its six children
#: are now ``direct_facade`` leaves, while ``ingestion_adapter`` and ``store`` are
#: runtime-only and are not routes at all. It therefore cannot become a pure
#: re-export of the canonical package, and stays ``pending_hybrid``.
#:
#: The order below is the barrel's own historical source order -- the two
#: runtime-only blocks sit *between* ``fixtures`` and ``models``, so it is neither
#: alphabetical nor portable-first. Its ``__all__`` is fully sorted, so it
#: interleaves all six blocks and matches none of them.
_MEMORY_GRAPH_ASSEMBLY_EXPORTS: tuple[str, ...] = (
    "assemble_evidence_graph",
    "assemble_graph_preview",
    "redact_segment_preview",
)
_MEMORY_GRAPH_FIXTURES_EXPORTS: tuple[str, ...] = (
    "FIXTURE_TIME",
    "MemoryGraphFixture",
    "build_memory_graph_fixture",
)
_MEMORY_GRAPH_INGESTION_ADAPTER_EXPORTS: tuple[str, ...] = (
    "IngestionGraphAdapterError",
    "IngestionGraphWriteResult",
    "chunk_to_memory_segment",
    "source_to_memory_source",
    "write_ingestion_records_to_graph",
)
_MEMORY_GRAPH_STORE_EXPORTS: tuple[str, ...] = (
    "MemoryGraphStore",
    "MemoryGraphStoreError",
)
_MEMORY_GRAPH_MODELS_EXPORTS: tuple[str, ...] = (
    "Confidence",
    "EvidenceGraphResponse",
    "GraphPreviewEdge",
    "GraphPreviewKind",
    "GraphPreviewNode",
    "GraphPreviewResponse",
    "GraphPreviewState",
    "MemoryEntity",
    "MemoryFact",
    "MemoryFactStatus",
    "MemorySegment",
    "MemorySegmentKind",
    "MemorySource",
    "MemorySourceFreshness",
    "MemorySourceStatus",
    "MemorySourceType",
    "RetrievalTrace",
    "SourceRef",
)
#: ``ValidationResult`` leads this block. The memory graph validation leaf never
#: owned a class of that name -- it imports the shared primitive -- so this is the
#: one export in the barrel whose object comes from outside the memory graph
#: domain entirely.
_MEMORY_GRAPH_VALIDATION_EXPORTS: tuple[str, ...] = (
    "ValidationResult",
    "validate_evidence_graph_response",
    "validate_graph_preview_response",
    "validate_memory_entity",
    "validate_memory_fact",
    "validate_memory_segment",
    "validate_memory_source",
)

#: The barrel's six import blocks, in its own historical source order.
_MEMORY_GRAPH_HISTORICAL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("assembly", _MEMORY_GRAPH_ASSEMBLY_EXPORTS),
    ("fixtures", _MEMORY_GRAPH_FIXTURES_EXPORTS),
    ("ingestion_adapter", _MEMORY_GRAPH_INGESTION_ADAPTER_EXPORTS),
    ("store", _MEMORY_GRAPH_STORE_EXPORTS),
    ("models", _MEMORY_GRAPH_MODELS_EXPORTS),
    ("validation", _MEMORY_GRAPH_VALIDATION_EXPORTS),
)

#: The four blocks that reach converted children -- the shape a *hypothetical*
#: promoted barrel would have. It is defect-free under
#: ``transitive_facade_defects``, which is what makes it the honest adversary base
#: below: every rejection then fails on the mutation injected into it rather than
#: on the two runtime blocks the real barrel also has.
_MEMORY_GRAPH_PORTABLE_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    block
    for block in _MEMORY_GRAPH_HISTORICAL_BLOCKS
    if block[0] not in {"ingestion_adapter", "store"}
)


def _memory_graph_barrel_source(
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    extra_source: str = "",
) -> str:
    source = ""
    exported: list[str] = []
    for module, names in blocks:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from omnivia_memory.memory_graph.{module} import (\n{body})\n"
        exported.extend(names)
    all_body = "".join(f'    "{name}",\n' for name in sorted(exported))
    return source + f"__all__ = [\n{all_body}]\n" + extra_source


def _memory_graph_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.memory_graph"),
        [
            manifest.route_for_legacy("omnivia_memory.memory_graph.assembly"),
            manifest.route_for_legacy("omnivia_memory.memory_graph.fixtures"),
            manifest.route_for_legacy("omnivia_memory.memory_graph.models"),
            manifest.route_for_legacy("omnivia_memory.memory_graph.validation"),
        ],
    )


def test_memory_graph_barrel_stays_a_pending_hybrid_over_converted_children() -> None:
    """The registry's own record of the split: a ``hybrid_barrel`` pair whose four
    portable children are converted and whose two runtime-only children are not
    routes at all. Pinned exactly, because every other gate in this section is
    about what may *not* happen to that state."""
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory.memory_graph")
    assert route.pair_kind is PairKind.HYBRID_BARREL
    assert route.shape is Shape.BARREL
    assert route.migration_state is MigrationState.PENDING_HYBRID
    assert not route.is_converted
    assert "memory_graph" in {
        item.suffix for item in manifest.by_state(MigrationState.PENDING_HYBRID)
    }

    for suffix in ("assembly", "fixtures", "models", "validation"):
        child = manifest.route_for_legacy(f"omnivia_memory.memory_graph.{suffix}")
        assert child.migration_state is MigrationState.DIRECT_FACADE
        assert child.is_converted
    for runtime_only in (
        "omnivia_memory.memory_graph.ingestion_adapter",
        "omnivia_memory.memory_graph.store",
    ):
        assert runtime_only in manifest.runtime_only_modules
        with pytest.raises(KeyError):
            manifest.route_for_legacy(runtime_only)


@pytest.mark.parametrize(
    "state",
    [
        "transitive_facade",
        "pending_direct_barrel",
        "direct_facade",
        "source_parity",
        "canonical_subset",
        "pending_root",
    ],
)
def test_memory_graph_barrel_cannot_be_promoted_out_of_pending_hybrid(
    state: str,
) -> None:
    """Now that all four portable children are converted, ``transitive_facade``
    looks tempting -- and it is exactly wrong: the barrel would stop being
    source-checked against its two runtime-only children, which are not routes and
    can never be converted children. ``pending_hybrid`` is the only state a
    ``hybrid_barrel``/``barrel`` pair may be in, so every other value (including
    the two leaf-only states and the root's) is rejected at load time by the
    combination rule rather than needing a state-specific waiver."""
    document = _document()
    _route(document, "memory_graph")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="valid combination"):
        load_manifest(document)


def test_memory_graph_barrel_historical_source_is_not_a_transitive_facade() -> None:
    """The third, independent lock: even with the state gate bypassed, the
    barrel's own unmodified historical source cannot pass the transitive-facade
    source check. Its ``ingestion_adapter`` and ``store`` blocks are not converted
    children, and those are the *only* two defects -- so the failure is
    attributable to the runtime-owned half specifically, not to the barrel's shape,
    its six blocks, or its sorted 38-name ``__all__``."""
    route, children = _memory_graph_barrel_route_and_children()
    source = _memory_graph_barrel_source(_MEMORY_GRAPH_HISTORICAL_BLOCKS)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert len(defects) == 2, defects
    for runtime_only in ("ingestion_adapter", "store"):
        assert any(
            "unapproved module" in defect
            and f"omnivia_memory.memory_graph.{runtime_only}" in defect
            for defect in defects
        ), (runtime_only, defects)


def test_memory_graph_portable_only_shape_is_the_defect_free_adversary_base() -> None:
    """The hypothetical promoted barrel -- its four portable blocks and their
    matching 31-name ``__all__`` -- must be defect-free, so each rejection below
    is proven to fail on what it injects."""
    route, children = _memory_graph_barrel_route_and_children()
    source = _memory_graph_barrel_source(_MEMORY_GRAPH_PORTABLE_BLOCKS)
    assert transitive_facade_defects(ast.parse(source), route, children) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # A direct reroute at the canonical package: the barrel's identity
        # preservation would become direct rather than transitive.
        (
            "from omnivia_core.memory_graph.models import MemorySource\n",
            "unapproved module",
        ),
        # The barrel's own runtime-only siblings, by absolute path. These are the
        # two modules the *real* barrel imports, and they are still not approved
        # children -- which is the whole reason it stays a hybrid.
        (
            "from omnivia_memory.memory_graph.store import MemoryGraphStore\n",
            "unapproved module",
        ),
        (
            (
                "from omnivia_memory.memory_graph.ingestion_adapter import "
                "chunk_to_memory_segment\n"
            ),
            "unapproved module",
        ),
        # The same runtime-only sibling by a *relative* path, which the resolver
        # has to resolve against the barrel's own package before it can tell it
        # apart from an approved child.
        ("from .store import MemoryGraphStore\n", "unapproved module"),
        # The validation leaf reaches the shared primitive for ``ValidationResult``,
        # so a ``_shared`` import is the most plausible accidental extra module --
        # and still not one of the barrel's children, from either tree.
        (
            "from omnivia_memory._shared.validation import ValidationResult\n",
            "unapproved module",
        ),
        (
            "from omnivia_core._shared.validation import ValidationResult\n",
            "unapproved module",
        ),
        # An unrelated runtime-only package, reached relatively.
        ("from ..persistence import Database\n", "unapproved module"),
        # A *relative* form of one of its own children. The resolver does approve
        # this module -- it resolves ``.models`` against the barrel's own package --
        # so what rejects the mutation is the binding/``__all__`` mismatch it
        # creates, not the module gate. Pinned so the two branches are not
        # confused for each other.
        (
            "from .models import SourceRef\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        # A second import block for an already-imported child: every module named
        # is approved, but it is an extra statement the historical source does not
        # have, and it makes the bound names disagree with ``__all__``.
        (
            "from omnivia_memory.memory_graph.models import Confidence\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("Confidence = object()\n", "assignments"),
        (
            "def validate_memory_fact(fact):\n    return fact\n",
            "statements of its own",
        ),
        ("class SourceRef:\n    pass\n", "statements of its own"),
        # A dynamic hook: every export must be a real, statically-visible binding.
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
        (
            "import sys\nsys.modules[__name__].__dict__['probe'] = 1\n",
            "statements of its own",
        ),
    ],
)
def test_memory_graph_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """Even the hypothetical promoted shape may not reach past its four converted
    children, define anything of its own, or install a dynamic hook. Every case
    here is injected into the defect-free portable-only base above, so the pattern
    asserted is the one the mutation causes -- not a leftover from the runtime
    blocks the real barrel also carries."""
    route, children = _memory_graph_barrel_route_and_children()
    source = _memory_graph_barrel_source(
        _MEMORY_GRAPH_PORTABLE_BLOCKS, extra_source=extra_source
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_memory_graph_barrel_transitive_form_requires_all_four_children() -> None:
    """Dropping any one portable block must fail. With four children a barrel
    could keep a perfectly valid three-block shape and still have stopped
    re-exporting a converted leaf."""
    route, children = _memory_graph_barrel_route_and_children()
    for dropped, _names in _MEMORY_GRAPH_PORTABLE_BLOCKS:
        source = _memory_graph_barrel_source(
            tuple(
                block for block in _MEMORY_GRAPH_PORTABLE_BLOCKS if block[0] != dropped
            )
        )
        defects = transitive_facade_defects(ast.parse(source), route, children)
        assert any(
            "does not import converted children" in defect
            and f"omnivia_memory.memory_graph.{dropped}" in defect
            for defect in defects
        ), (dropped, defects)


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


# ---------------------------------------------------------------------------
# The ``graph`` pair: one direct facade leaf, one split facade leaf, under a
# barrel that stays a hybrid.
#
# ``graph.search_models`` is the first ``split_facade``: it re-exports the three
# canonicalized query/result records (and their incidental bindings) from
# ``omnivia_core.graph.search_models`` while keeping the four relevance-scoring
# helpers defined locally, because Core deliberately excludes them and the
# legacy-owned ``graph.search_service`` still calls them. That is a source shape
# no other state describes, so it gets its own fail-closed policy
# (``split_facade_defects``) and its own adversaries here.
# ---------------------------------------------------------------------------

#: The exact portable names the split leaf re-exports, restated here rather than
#: read off the module: this fixture must not be derived from the very file whose
#: mutations it is proving get rejected.
_GRAPH_SEARCH_MODELS_IMPORTS: tuple[str, ...] = (
    "Any",
    "Entity",
    "EntityType",
    "GraphSearchQuery",
    "GraphSearchResult",
    "GraphSearchResultSet",
    "RelationshipType",
    "dataclass",
    "field",
)

#: The four retained, legacy-owned helpers, in the module's historical order.
_GRAPH_SEARCH_MODELS_HELPERS: tuple[str, ...] = (
    "score_name_match",
    "score_relationship_count",
    "score_neighbor_overlap",
    "compute_relevance_score",
)

_GRAPH_SEARCH_MODELS_CANONICAL = "omnivia_core.graph.search_models"


def _split_source(
    *,
    docstring: str = '"""Docstring."""\n\n',
    imports: tuple[str, ...] = _GRAPH_SEARCH_MODELS_IMPORTS,
    helpers: tuple[str, ...] = _GRAPH_SEARCH_MODELS_HELPERS,
    future: str = "from __future__ import annotations\n",
    canonical_module: str = _GRAPH_SEARCH_MODELS_CANONICAL,
    extra_source: str = "",
    helpers_before_route: bool = False,
) -> str:
    """The split leaf's shape: docstring, future import, one canonical import,
    then one synchronous ``def`` per retained helper. Bodies are irrelevant to
    the source policy, so they are stubbed.

    ``docstring`` and ``helpers_before_route`` exist so the *sequence* can be
    attacked as well as the statement kinds: the policy is an exact ordered
    shape, not a multiset of permitted statements.
    """
    body = "".join(f"    {name},\n" for name in imports)
    route = f"from {canonical_module} import (\n{body})\n" if imports else ""
    definitions = "".join(
        f"\n\ndef {name}() -> float:\n    return 0.0\n" for name in helpers
    )
    source = f"{docstring}{future}"
    if helpers_before_route:
        source += f"{definitions}\n\n{route}"
    else:
        source += f"{route}{definitions}"
    return source + extra_source


def _split_defects(source: str) -> list[str]:
    return split_facade_defects(ast.parse(source), _GRAPH_SEARCH_MODELS_CANONICAL)


def test_graph_pair_states_and_shapes_are_exact() -> None:
    """The registry's own record of the split: a direct/leaf pair per module, one
    ``direct_facade`` and one ``split_facade``, both converted, under a
    ``hybrid_barrel`` that is not. Pinned exactly, because every other gate in
    this section is about what may *not* happen to those states."""
    manifest = load_manifest()

    models = manifest.route_for_legacy("omnivia_memory.graph.models")
    assert (models.pair_kind, models.shape, models.migration_state) == (
        PairKind.DIRECT,
        Shape.LEAF,
        MigrationState.DIRECT_FACADE,
    )
    assert models.is_converted

    split = manifest.route_for_legacy("omnivia_memory.graph.search_models")
    assert (split.pair_kind, split.shape, split.migration_state) == (
        PairKind.DIRECT,
        Shape.LEAF,
        MigrationState.SPLIT_FACADE,
    )
    assert split.is_converted
    assert split.canonical_module == _GRAPH_SEARCH_MODELS_CANONICAL

    barrel = manifest.route_for_legacy("omnivia_memory.graph")
    assert (barrel.pair_kind, barrel.shape, barrel.migration_state) == (
        PairKind.HYBRID_BARREL,
        Shape.BARREL,
        MigrationState.PENDING_HYBRID,
    )
    assert not barrel.is_converted

    for runtime_only in (
        "omnivia_memory.graph.repository",
        "omnivia_memory.graph.search_service",
        "omnivia_memory.graph.service",
    ):
        assert runtime_only in manifest.runtime_only_modules
        with pytest.raises(KeyError):
            manifest.route_for_legacy(runtime_only)


def test_split_facade_is_a_converted_state_but_not_a_pending_one() -> None:
    """``split_facade`` counts as converted -- its portable half really is the
    canonical objects -- which is what lets a barrel above it eventually become
    transitive. It is not pending, so its source *is* checked."""
    manifest = load_manifest()
    converted = {route.suffix for route in manifest.routes if route.is_converted}
    assert "graph.search_models" in converted
    assert "graph.models" in converted

    # A pending state is one whose source is not checked at all. Relabelling the
    # split leaf as any barrel/root state is rejected by the combination table, so
    # there is no pending state a leaf can hide in.
    for state in ("pending_direct_barrel", "pending_hybrid", "pending_root"):
        document = _document()
        _route(document, "graph.search_models")["migration_state"] = state
        with pytest.raises(FacadeManifestError, match="valid combination"):
            load_manifest(document)


@pytest.mark.parametrize(
    ("suffix", "kind", "shape"),
    [
        ("graph.search_models", "hybrid_barrel", "leaf"),
        ("graph.search_models", "root", "leaf"),
        ("graph.search_models", "direct", "barrel"),
        ("graph.search_models", "direct", "root"),
        ("graph", "hybrid_barrel", "barrel"),
    ],
    ids=[
        "hybrid-kind-leaf",
        "root-kind-leaf",
        "direct-kind-barrel",
        "direct-kind-root",
        "hybrid-barrel-declared-split",
    ],
)
def test_split_facade_is_only_valid_for_a_direct_leaf(
    suffix: str, kind: str, shape: str
) -> None:
    """``split_facade`` describes one module's symbol set, so only a
    ``direct``/``leaf`` pair may be in it. Every other kind/shape combination --
    including relabelling the hybrid barrel itself -- is rejected at load time."""
    document = _document()
    route = _route(document, suffix)
    route["pair_kind"] = kind
    route["shape"] = shape
    route["migration_state"] = "split_facade"
    with pytest.raises(FacadeManifestError, match="valid combination|exactly one root"):
        load_manifest(document)


def test_split_source_shape_is_the_defect_free_adversary_base() -> None:
    """The real shape must be defect-free, so each rejection below is proven to
    fail on what it injects rather than on the split shape itself."""
    assert (
        split_facade_defects(
            ast.parse(_split_source()), _GRAPH_SEARCH_MODELS_CANONICAL
        )
        == []
    )
    # One retained helper is enough; four is what this leaf happens to have.
    assert (
        split_facade_defects(
            ast.parse(_split_source(helpers=("score_name_match",))),
            _GRAPH_SEARCH_MODELS_CANONICAL,
        )
        == []
    )


@pytest.mark.parametrize(
    ("source", "pattern"),
    [
        # A plain top-level import. ``import math`` is the plausible one here: the
        # real module has exactly that import *inside* ``score_relationship_count``,
        # and hoisting it to module scope would change what the module publishes.
        (_split_source(extra_source="import math\n"), "plain import"),
        # A second from-import, at the canonical package and elsewhere. Either
        # would make the portable namespace come from more than one place.
        (
            _split_source(
                extra_source="from omnivia_core.graph.models import Relationship\n"
            ),
            "from-imports besides '__future__', expected exactly one",
        ),
        (
            _split_source(
                extra_source="from omnivia_memory.graph.models import Relationship\n"
            ),
            "from-imports besides '__future__', expected exactly one",
        ),
        # The wrong canonical module, a relative form of the right one, a star
        # import, and an alias.
        (
            _split_source(canonical_module="omnivia_core.graph.models"),
            "imports from 'omnivia_core.graph.models'",
        ),
        (
            _split_source(imports=())
            + "from .search_models import GraphSearchQuery\n",
            "uses a relative import (level 1)",
        ),
        (
            (
                f'"""Docstring."""\n\nfrom __future__ import annotations\n'
                f"from {_GRAPH_SEARCH_MODELS_CANONICAL} import *\n\n\n"
                f"def score_name_match() -> float:\n    return 0.0\n"
            ),
            "uses a star import",
        ),
        (
            (
                f'"""Docstring."""\n\nfrom __future__ import annotations\n'
                f"from {_GRAPH_SEARCH_MODELS_CANONICAL} import GraphSearchQuery as Query"
                f"\n\n\ndef score_name_match() -> float:\n    return 0.0\n"
            ),
            "aliases 'GraphSearchQuery' as 'Query'",
        ),
        # The future import: missing, doubled, aliased, starred, relative, and
        # carrying a second feature. The real statement is load-bearing -- the
        # retained signatures are compared as postponed string annotations -- so an
        # ``annotations`` binding imported from the canonical module is not a
        # substitute for it.
        (_split_source(future=""), "has 0 '__future__' imports"),
        (
            _split_source(
                future=(
                    "from __future__ import annotations\n"
                    "from __future__ import annotations\n"
                )
            ),
            "has 2 '__future__' imports",
        ),
        (
            _split_source(future="from __future__ import annotations as ann\n"),
            "aliases future feature 'annotations' as 'ann'",
        ),
        (_split_source(future="from __future__ import *\n"), "star __future__ import"),
        (
            _split_source(future="from . import annotations\n"),
            "from-imports besides '__future__', expected exactly one",
        ),
        (
            _split_source(
                future="from __future__ import annotations, generator_stop\n"
            ),
            "imports future feature 'generator_stop'",
        ),
        # Async definitions, classes, assignments and annotated assignments.
        (
            _split_source(
                extra_source="\n\nasync def score_async() -> float:\n    return 0.0\n"
            ),
            "defines the async function 'score_async'",
        ),
        (
            _split_source(extra_source="\n\nclass LocalRecord:\n    pass\n"),
            "has statements of its own (ClassDef)",
        ),
        (
            _split_source(extra_source='__all__ = ["score_name_match"]\n'),
            "has statements of its own (Assign)",
        ),
        (
            _split_source(extra_source="WEIGHT: float = 0.5\n"),
            "has statements of its own (AnnAssign)",
        ),
        # Dynamic hooks, whether installed as a module ``__getattr__``/``__dir__``
        # or by writing to ``sys.modules``.
        (
            _split_source(
                extra_source=(
                    "\n\ndef __getattr__(name: str) -> float:\n"
                    "    raise AttributeError(name)\n"
                )
            ),
            "dynamic module hook '__getattr__'",
        ),
        (
            _split_source(
                extra_source="\n\ndef __dir__() -> list[str]:\n    return []\n"
            ),
            "dynamic module hook '__dir__'",
        ),
        (
            _split_source(
                extra_source="import sys\nsys.modules[__name__].__dict__['probe'] = 1\n"
            ),
            "plain import",
        ),
        # A decorated retained definition: the object published would be whatever
        # the decorator returns, not the preserved function.
        (
            _split_source()
            + "\n\n@staticmethod\ndef score_extra() -> float:\n    return 0.0\n",
            "decorates the retained definition 'score_extra'",
        ),
        # Duplicate imported and duplicate defined names, and a definition that
        # shadows an imported one.
        (
            _split_source(
                imports=(*_GRAPH_SEARCH_MODELS_IMPORTS, "GraphSearchQuery")
            ),
            "imports the same name twice: ['GraphSearchQuery']",
        ),
        (
            _split_source(
                helpers=(*_GRAPH_SEARCH_MODELS_HELPERS, "score_name_match")
            ),
            "defines the same name twice: ['score_name_match']",
        ),
        (
            _split_source(helpers=(*_GRAPH_SEARCH_MODELS_HELPERS, "GraphSearchQuery")),
            "defines ['GraphSearchQuery'], which it also imports",
        ),
    ],
)
def test_split_facade_source_policy_is_fail_closed(source: str, pattern: str) -> None:
    defects = split_facade_defects(ast.parse(source), _GRAPH_SEARCH_MODELS_CANONICAL)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Rule 1, absent. Every *kind* of statement below the (missing) docstring
        # is permitted and correctly ordered, so nothing but the docstring rule
        # itself can reject this.
        (_split_source(docstring=""), ("does not open with a module docstring",)),
        # Rule 1, doubled. A second standalone string expression is a statement
        # of the module's own -- before the future import, or trailing after the
        # last retained def, where it reads as a stray editing artefact.
        (
            _split_source(docstring='"""Docstring."""\n\n"""Second."""\n\n'),
            ("has 1 standalone string expression(s) besides the module docstring",),
        ),
        (
            _split_source(extra_source='\n\n"""Trailing."""\n'),
            ("has 1 standalone string expression(s) besides the module docstring",),
        ),
        (
            _split_source(
                docstring='"""Docstring."""\n\n"""Second."""\n\n',
                extra_source='\n\n"""Trailing."""\n',
            ),
            ("has 2 standalone string expression(s) besides the module docstring",),
        ),
        # The order of rules 3 and 4. The statements are exactly the accepted
        # ones, in exactly the accepted quantities -- only the sequence differs,
        # so every count-based check passes and only the positional walk rejects.
        (
            _split_source(helpers=("score_name_match",), helpers_before_route=True),
            (
                "has FunctionDef as top-level statement 2",
                "has ImportFrom as top-level statement 3",
            ),
        ),
        # Rule 2, naming the one permitted feature twice. Neither the star, alias
        # nor unexpected-feature check sees anything wrong with this.
        (
            _split_source(future="from __future__ import annotations, annotations\n"),
            ("names 'annotations' 2 times in its '__future__' import",),
        ),
    ],
    ids=[
        "no-module-docstring",
        "second-string-expression",
        "trailing-string-expression",
        "multiple-string-expressions",
        "helper-before-route-import",
        "duplicated-annotations-feature",
    ],
)
def test_split_facade_sequence_is_exact(
    source: str, expected: tuple[str, ...]
) -> None:
    """The split policy is an exact ordered shape, not a multiset of permitted
    statement kinds. Each source here injects one defect into the shape
    ``test_split_source_shape_is_the_defect_free_adversary_base`` proves is
    otherwise accepted, and the *whole* defect list is asserted -- so a rejection
    that came from some other malformed detail of the fixture would fail here
    rather than pass as proof of the rule under test.
    """
    defects = _split_defects(source)
    assert len(defects) == len(expected), defects
    for defect, fragment in zip(defects, expected, strict=True):
        assert fragment in defect, defects


def test_split_facade_rejects_a_module_with_no_retained_helpers() -> None:
    """A split facade with nothing legacy-owned is a plain direct facade wearing
    the wrong label: the whole point of the state is the helpers Core excludes.
    Without them the module has no reason not to be ``direct_facade``, so the
    source policy rejects it rather than accepting a state that describes nothing.
    """
    defects = split_facade_defects(
        ast.parse(_split_source(helpers=())), _GRAPH_SEARCH_MODELS_CANONICAL
    )
    assert any("defines no synchronous top-level function" in defect for defect in defects), (
        defects
    )


def test_a_direct_wrapper_is_not_a_split_facade() -> None:
    """The real ``graph.models`` wrapper -- one import, nothing else -- must fail
    the split policy on both counts: it has no future import (its canonical
    counterpart supplies the ``annotations`` binding) and it defines nothing."""
    document = _document()
    _route(document, "graph.models")["migration_state"] = "split_facade"
    with pytest.raises(FacadeManifestError, match="declared 'split_facade'") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert "'__future__' imports" in joined
    assert "defines no synchronous top-level function" in joined


def test_a_split_source_is_not_a_direct_facade() -> None:
    """The mirror direction: the real ``graph.search_models`` source may not be
    declared ``direct_facade`` either. It has statements of its own (the four
    retained ``def``\\ s) and a second from-import (``__future__``), so the direct
    policy rejects it."""
    document = _document()
    _route(document, "graph.search_models")["migration_state"] = "direct_facade"
    with pytest.raises(FacadeManifestError, match="declared 'direct_facade'") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert "statements of its own (FunctionDef)" in joined
    assert "from-imports, expected exactly one" in joined


@pytest.mark.parametrize("state", ["canonical_subset", "source_parity"])
def test_split_facade_state_cannot_be_walked_back(state: str) -> None:
    """Neither duplicated-source state may be re-declared over a leaf that is now
    a split facade: the source no longer duplicates the canonical module at all.
    ``canonical_subset`` is the specific temptation -- it is the state this leaf
    just left, and it is the one that would silently stop the leaf being checked
    as converted."""
    document = _document()
    _route(document, "graph.search_models")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert f"declared {state!r} but the source is an exact split facade" in joined


@pytest.mark.parametrize("state", ["canonical_subset", "source_parity"])
def test_graph_models_direct_facade_state_cannot_be_walked_back(state: str) -> None:
    """The same for its sibling, which is a plain direct facade: a duplicated-source
    state over an exact single-import wrapper is rejected by the pre-existing
    direct branch, not the split one."""
    document = _document()
    _route(document, "graph.models")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert f"declared {state!r} but the source is an exact facade" in joined


@pytest.mark.parametrize(
    ("suffix", "state", "pattern"),
    [
        ("graph.search_models", "direct_facade", "declared 'direct_facade'"),
        ("graph.search_models", "canonical_subset", "move the state forward"),
        ("graph.search_models", "source_parity", "move the state forward"),
        ("graph.models", "split_facade", "declared 'split_facade'"),
        ("graph.models", "canonical_subset", "move the state forward"),
        ("graph.models", "source_parity", "move the state forward"),
    ],
    ids=[
        "split-as-direct",
        "split-as-canonical-subset",
        "split-as-source-parity",
        "direct-as-split",
        "direct-as-canonical-subset",
        "direct-as-source-parity",
    ],
)
def test_validate_route_sources_enforces_the_graph_states(
    suffix: str, state: str, pattern: str
) -> None:
    """``validate_route_sources`` is the source half of the gate on its own -- no
    checkout rediscovery, no shape agreement, just each route's declared state
    against its file. It must reach the same verdicts as ``validate_checkout``:
    accept the real registry, and reject every mislabelling of either Graph leaf.
    """
    validate_route_sources()
    document = _document()
    _route(document, suffix)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match=pattern):
        validate_route_sources(manifest=document)


def test_graph_barrel_cannot_be_promoted_now_that_both_leaves_are_converted() -> None:
    """Both portable children are converted, so ``transitive_facade`` looks
    tempting -- and it is exactly wrong: the barrel also re-exports
    ``GraphSearchError``/``GraphSearchService`` from the runtime-only
    ``search_service`` leaf, which is not a route and can never be a converted
    child. ``pending_hybrid`` stays the only state it may be in."""
    for state in (
        "transitive_facade",
        "pending_direct_barrel",
        "direct_facade",
        "split_facade",
        "source_parity",
        "canonical_subset",
        "pending_root",
    ):
        document = _document()
        _route(document, "graph")["migration_state"] = state
        with pytest.raises(FacadeManifestError, match="valid combination"):
            load_manifest(document)


#: The legacy graph barrel's exact historical import blocks, in source order.
#: Restated rather than read off the barrel, because this is the file whose edits
#: it exists to reject.
_GRAPH_BARREL_HISTORICAL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "models",
        (
            "ApprovalStatus",
            "Entity",
            "EntityType",
            "Relationship",
            "RelationshipType",
        ),
    ),
    (
        "search_models",
        ("GraphSearchQuery", "GraphSearchResult", "GraphSearchResultSet"),
    ),
    ("search_service", ("GraphSearchError", "GraphSearchService")),
)

#: The two portable blocks -- the shape a *hypothetical* promoted barrel would
#: have, which is defect-free and therefore the honest adversary base.
_GRAPH_BARREL_PORTABLE_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    block for block in _GRAPH_BARREL_HISTORICAL_BLOCKS if block[0] != "search_service"
)


def _graph_barrel_source(
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    extra_source: str = "",
) -> str:
    source = ""
    exported: list[str] = []
    for module, names in blocks:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from omnivia_memory.graph.{module} import (\n{body})\n"
        exported.extend(names)
    all_body = "".join(f'    "{name}",\n' for name in sorted(exported))
    return source + f"__all__ = [\n{all_body}]\n" + extra_source


def _graph_barrel_route_and_children() -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.graph"),
        [
            manifest.route_for_legacy("omnivia_memory.graph.models"),
            manifest.route_for_legacy("omnivia_memory.graph.search_models"),
        ],
    )


def test_graph_barrel_historical_source_is_not_a_transitive_facade() -> None:
    """Even with the state gate bypassed, the barrel's own unmodified historical
    source cannot pass the transitive-facade check: its ``search_service`` block is
    not a converted child, and that is the *only* defect -- so the failure is
    attributable to the runtime-owned half specifically, not to the barrel's shape
    or its ten-name ``__all__``."""
    route, children = _graph_barrel_route_and_children()
    source = _graph_barrel_source(_GRAPH_BARREL_HISTORICAL_BLOCKS)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert len(defects) == 1, defects
    assert "unapproved module" in defects[0]
    assert "omnivia_memory.graph.search_service" in defects[0]


def test_graph_barrel_portable_only_shape_is_the_defect_free_adversary_base() -> None:
    route, children = _graph_barrel_route_and_children()
    source = _graph_barrel_source(_GRAPH_BARREL_PORTABLE_BLOCKS)
    assert transitive_facade_defects(ast.parse(source), route, children) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # A direct reroute at the canonical package: the barrel's identity
        # preservation would become direct rather than transitive.
        (
            "from omnivia_core.graph.models import Entity\n",
            "unapproved module",
        ),
        # The runtime-only sibling the *real* barrel imports, absolutely and
        # relatively. It is still not an approved child, which is the whole reason
        # the barrel stays a hybrid.
        (
            "from omnivia_memory.graph.search_service import GraphSearchService\n",
            "unapproved module",
        ),
        ("from .search_service import GraphSearchError\n", "unapproved module"),
        # The other two runtime-only graph leaves, which the barrel has never
        # re-exported and must not start to.
        (
            "from omnivia_memory.graph.repository import EntityRepository\n",
            "unapproved module",
        ),
        ("from ..persistence import Database\n", "unapproved module"),
        # A relative form of one of its own children. The resolver approves the
        # module, so what rejects this is the binding/``__all__`` mismatch it
        # creates -- pinned so the two branches are not confused for each other.
        (
            "from .models import EntityCreate\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        # The four retained scoring helpers are *not* part of the barrel's surface.
        # Re-exporting one from the approved split leaf still fails, on the
        # ``__all__`` mismatch rather than the module gate.
        (
            "from omnivia_memory.graph.search_models import compute_relevance_score\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("Entity = object()\n", "assignments"),
        (
            "def compute_relevance_score(query):\n    return query\n",
            "statements of its own",
        ),
        ("class Entity:\n    pass\n", "statements of its own"),
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
    ],
)
def test_graph_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    route, children = _graph_barrel_route_and_children()
    source = _graph_barrel_source(
        _GRAPH_BARREL_PORTABLE_BLOCKS, extra_source=extra_source
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_graph_barrel_transitive_form_requires_both_children() -> None:
    """Dropping either portable block must fail: a barrel could keep a perfectly
    valid one-block shape and still have stopped re-exporting a converted leaf."""
    route, children = _graph_barrel_route_and_children()
    for dropped, _names in _GRAPH_BARREL_PORTABLE_BLOCKS:
        source = _graph_barrel_source(
            tuple(block for block in _GRAPH_BARREL_PORTABLE_BLOCKS if block[0] != dropped)
        )
        defects = transitive_facade_defects(ast.parse(source), route, children)
        assert any(
            "does not import converted children" in defect
            and f"omnivia_memory.graph.{dropped}" in defect
            for defect in defects
        ), (dropped, defects)


#: --------------------------------------------------------------------------
#: The ``ingestion`` pair: two direct facades under two hybrid barrels.
#:
#: ``ingestion.models`` and ``ingestion.watcher.models`` are plain
#: ``direct_facade`` leaves -- one import each, nothing retained -- but neither
#: barrel above them can follow: fourteen of the ``ingestion`` barrel's nineteen
#: exports come from the runtime-only chunker/extractor/pipeline/repository/
#: scanner leaves, and two of the ``ingestion.watcher`` barrel's twelve come from
#: the runtime-only ``debouncer``/``tracker``. Both stay ``pending_hybrid``.
#: --------------------------------------------------------------------------

_INGESTION_MODELS_CANONICAL = "omnivia_core.ingestion.models"
_WATCHER_MODELS_CANONICAL = "omnivia_core.ingestion.watcher.models"

#: Each leaf's exact 18-name public/star namespace, in the source order its
#: wrapper uses. Only the owned names are routes -- 7 of these 18 for
#: ``ingestion.models`` and 10 for ``ingestion.watcher.models``; the rest are the
#: incidental imports the historical module's own namespace also published.
#: Restated here rather than read off the wrapper: this fixture is the adversary
#: base for mutations of that very file.
_INGESTION_MODELS_NAMESPACE: tuple[str, ...] = (
    "TYPE_CHECKING",
    "Any",
    "Chunk",
    "ExtractionResult",
    "FileInventory",
    "FileType",
    "IngestSource",
    "ParseStatus",
    "Path",
    "Source",
    "annotations",
    "dataclass",
    "datetime",
    "enum",
    "field",
    "hashlib",
    "timezone",
    "uuid",
)
_WATCHER_MODELS_NAMESPACE: tuple[str, ...] = (
    "TYPE_CHECKING",
    "DebounceConfig",
    "FileChange",
    "FileChangeBatch",
    "FileChangeType",
    "IndexerScheduler",
    "IndexerState",
    "IndexerStatus",
    "ScheduledJob",
    "SourceReference",
    "WatchedPath",
    "annotations",
    "dataclass",
    "datetime",
    "enum",
    "field",
    "timezone",
    "uuid",
)

_INGESTION_LEAVES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ingestion.models",
        _INGESTION_MODELS_CANONICAL,
        _INGESTION_MODELS_NAMESPACE,
    ),
    (
        "ingestion.watcher.models",
        _WATCHER_MODELS_CANONICAL,
        _WATCHER_MODELS_NAMESPACE,
    ),
)


def _direct_wrapper_source(
    canonical_module: str,
    names: tuple[str, ...],
    extra_source: str = "",
    *,
    docstring: str = '"""Compatibility facade."""\n',
) -> str:
    """A synthetic pure ``direct_facade`` wrapper: docstring plus one import."""
    body = "".join(f"    {name},\n" for name in names)
    return f"{docstring}from {canonical_module} import (\n{body})\n{extra_source}"


def test_ingestion_pair_states_and_shapes_are_exact() -> None:
    """The registry's own record of this batch: a ``direct``/``leaf`` pair per
    models module, both ``direct_facade`` and both converted, each under a
    ``hybrid_barrel`` that is not. Pinned exactly, because every other gate in
    this section is about what may *not* happen to those states."""
    manifest = load_manifest()

    for suffix, canonical_module, _names in _INGESTION_LEAVES:
        leaf = manifest.route_for_legacy(f"omnivia_memory.{suffix}")
        assert (leaf.pair_kind, leaf.shape, leaf.migration_state) == (
            PairKind.DIRECT,
            Shape.LEAF,
            MigrationState.DIRECT_FACADE,
        )
        assert leaf.is_converted
        assert leaf.canonical_module == canonical_module

    for suffix in ("ingestion", "ingestion.watcher"):
        barrel = manifest.route_for_legacy(f"omnivia_memory.{suffix}")
        assert (barrel.pair_kind, barrel.shape, barrel.migration_state) == (
            PairKind.HYBRID_BARREL,
            Shape.BARREL,
            MigrationState.PENDING_HYBRID,
        )
        assert not barrel.is_converted

    for runtime_only in (
        "omnivia_memory.ingestion.chunker",
        "omnivia_memory.ingestion.extractors",
        "omnivia_memory.ingestion.pipeline",
        "omnivia_memory.ingestion.repositories",
        "omnivia_memory.ingestion.scanner",
        "omnivia_memory.ingestion.watcher.debouncer",
        "omnivia_memory.ingestion.watcher.tracker",
        "omnivia_memory.memory_graph.ingestion_adapter",
    ):
        assert runtime_only in manifest.runtime_only_modules
        with pytest.raises(KeyError):
            manifest.route_for_legacy(runtime_only)


@pytest.mark.parametrize(
    "suffix", ["ingestion.models", "ingestion.watcher.models"]
)
@pytest.mark.parametrize("state", ["canonical_subset", "source_parity"])
def test_ingestion_leaf_direct_facade_state_cannot_be_walked_back(
    suffix: str, state: str
) -> None:
    """Neither duplicated-source state may be re-declared over a leaf that is now
    an exact single-import wrapper: the source no longer duplicates the canonical
    module at all, and relabelling it would silently stop it being checked as
    converted."""
    document = _document()
    _route(document, suffix)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert f"declared {state!r} but the source is an exact facade" in joined


@pytest.mark.parametrize(
    ("suffix", "state", "pattern"),
    [
        ("ingestion.models", "split_facade", "declared 'split_facade'"),
        ("ingestion.models", "canonical_subset", "move the state forward"),
        ("ingestion.models", "source_parity", "move the state forward"),
        ("ingestion.watcher.models", "split_facade", "declared 'split_facade'"),
        ("ingestion.watcher.models", "canonical_subset", "move the state forward"),
        ("ingestion.watcher.models", "source_parity", "move the state forward"),
    ],
    ids=[
        "models-as-split",
        "models-as-canonical-subset",
        "models-as-source-parity",
        "watcher-as-split",
        "watcher-as-canonical-subset",
        "watcher-as-source-parity",
    ],
)
def test_validate_route_sources_enforces_the_ingestion_states(
    suffix: str, state: str, pattern: str
) -> None:
    """``validate_route_sources`` is the source half of the gate on its own -- no
    checkout rediscovery, no shape agreement, just each route's declared state
    against its file. It must reach the same verdicts as ``validate_checkout``:
    accept the real registry, and reject every mislabelling of either leaf.
    ``split_facade`` is the specific temptation here: a wrapper with no retained
    definitions is not a split facade, and declaring it one would swap in a
    source policy that this wrapper cannot satisfy."""
    validate_route_sources()
    document = _document()
    _route(document, suffix)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match=pattern):
        validate_route_sources(manifest=document)


@pytest.mark.parametrize("suffix", ["ingestion", "ingestion.watcher"])
@pytest.mark.parametrize(
    "state",
    [
        "transitive_facade",
        "pending_direct_barrel",
        "direct_facade",
        "split_facade",
        "source_parity",
        "canonical_subset",
        "pending_root",
    ],
)
def test_ingestion_barrels_cannot_be_promoted_out_of_pending_hybrid(
    suffix: str, state: str
) -> None:
    """Each barrel's one portable child is converted, so ``transitive_facade``
    looks tempting -- and it is exactly wrong: both barrels also re-export from
    runtime-only leaves that are not routes and can never be converted children.
    ``pending_hybrid`` is the only state a ``hybrid_barrel``/``barrel`` pair may
    be in, so every other value is rejected at load time by the combination rule
    rather than needing a state-specific waiver."""
    document = _document()
    _route(document, suffix)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="valid combination"):
        load_manifest(document)


@pytest.mark.parametrize(
    ("suffix", "canonical_module", "names"),
    [(suffix, canonical, names) for suffix, canonical, names in _INGESTION_LEAVES],
    ids=["ingestion.models", "ingestion.watcher.models"],
)
def test_ingestion_wrapper_shape_is_the_defect_free_adversary_base(
    suffix: str, canonical_module: str, names: tuple[str, ...]
) -> None:
    """The accepted shape -- a docstring and exactly one absolute, unaliased,
    non-star import of the paired canonical module -- must be defect-free, so each
    rejection below is proven to fail on what it injects rather than on the shape
    itself."""
    source = _direct_wrapper_source(canonical_module, names)
    assert direct_facade_defects(ast.parse(source), canonical_module) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # An ``__all__`` of its own: the leaves never declared one, and adding it
        # would let the wrapper advertise a surface its route does not decide.
        ('__all__ = ["Chunk"]\n', "statements of its own"),
        # A local definition, of either kind.
        ("class Chunk:\n    pass\n", "statements of its own"),
        ("def helper() -> float:\n    return 0.0\n", "statements of its own"),
        # A dynamic hook: every name must be decided at import time.
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
        ("import uuid\n", "statements of its own"),
        (
            "import sys\nsys.modules[__name__].__dict__['probe'] = 1\n",
            "statements of its own",
        ),
        # A second from-import, whether it reaches a sibling canonical leaf, the
        # legacy runtime, or ``__future__``.
        (
            "from omnivia_core.provenance.models import SourceType\n",
            "from-imports, expected exactly one",
        ),
        (
            "from omnivia_memory.ingestion.scanner import FileScanner\n",
            "from-imports, expected exactly one",
        ),
        ("from __future__ import annotations\n", "from-imports, expected exactly one"),
    ],
)
def test_ingestion_wrapper_rejects_a_local_definition_or_second_import(
    extra_source: str,
    pattern: str,
) -> None:
    """Injected into the defect-free base above, so the asserted pattern is the
    one the mutation causes. Parametrized on the ``ingestion.models`` wrapper;
    its watcher sibling is the same shape and is covered by the reroute cases
    below."""
    source = _direct_wrapper_source(
        _INGESTION_MODELS_CANONICAL, _INGESTION_MODELS_NAMESPACE, extra_source
    )
    defects = direct_facade_defects(ast.parse(source), _INGESTION_MODELS_CANONICAL)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("canonical_module", "names", "source", "pattern"),
    [
        # The wrong canonical module -- including the *sibling* leaf, which is the
        # near miss a copy/paste would produce.
        (
            _INGESTION_MODELS_CANONICAL,
            _INGESTION_MODELS_NAMESPACE,
            "from omnivia_core.ingestion.watcher.models import FileChange\n",
            "imports from 'omnivia_core.ingestion.watcher.models'",
        ),
        (
            _WATCHER_MODELS_CANONICAL,
            _WATCHER_MODELS_NAMESPACE,
            "from omnivia_core.ingestion.models import Chunk\n",
            "imports from 'omnivia_core.ingestion.models'",
        ),
        # The legacy path: a wrapper that re-exported from its own tree would keep
        # every name resolving and route nothing.
        (
            _INGESTION_MODELS_CANONICAL,
            _INGESTION_MODELS_NAMESPACE,
            "from omnivia_memory.ingestion.models import Chunk\n",
            "imports from 'omnivia_memory.ingestion.models'",
        ),
        # Relative, star, and aliased forms of the one permitted statement.
        (
            _INGESTION_MODELS_CANONICAL,
            _INGESTION_MODELS_NAMESPACE,
            "from .models import Chunk\n",
            "relative import",
        ),
        (
            _INGESTION_MODELS_CANONICAL,
            _INGESTION_MODELS_NAMESPACE,
            "from omnivia_core.ingestion.models import *\n",
            "star import",
        ),
        (
            _INGESTION_MODELS_CANONICAL,
            _INGESTION_MODELS_NAMESPACE,
            "from omnivia_core.ingestion.models import Source as IngestSource\n",
            "aliases 'Source' as 'IngestSource'",
        ),
        (
            _WATCHER_MODELS_CANONICAL,
            _WATCHER_MODELS_NAMESPACE,
            (
                "from omnivia_core.ingestion.watcher.models import "
                "SourceReference as SourceRef\n"
            ),
            "aliases 'SourceReference' as 'SourceRef'",
        ),
    ],
    ids=[
        "models-imports-watcher-sibling",
        "watcher-imports-models-sibling",
        "models-imports-legacy-self",
        "relative",
        "star",
        "models-aliases-the-identity-alias",
        "watcher-aliases-source-reference",
    ],
)
def test_ingestion_wrapper_rejects_a_reroute_star_or_alias(
    canonical_module: str,
    names: tuple[str, ...],
    source: str,
    pattern: str,
) -> None:
    """Each of these replaces the one permitted statement rather than adding to
    it, so the wrapper still has exactly one from-import and the defect reported
    is the reroute/star/alias itself.

    The two alias cases are the sharpest: ``IngestSource`` and ``SourceReference``
    are real names in these leaves' namespaces, so an aliasing wrapper would
    publish a namespace that looks right and is built by renaming rather than by
    routing."""
    del names  # the mutated source replaces the whole import statement
    defects = direct_facade_defects(ast.parse(f'"""Doc."""\n{source}'), canonical_module)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("docstring", "extra_source", "pattern"),
    [
        # No docstring at all. This is the case a blanket "ignore every module
        # string expression" filter cannot see: the remaining body is exactly one
        # from-import of the right module with no statements of its own, so every
        # other rule is satisfied and only the positional docstring rule rejects
        # it. A wrapper is a documented boundary, not an anonymous re-export.
        ("", "", "does not open with a module docstring"),
        # A stray string after the import: a module-scope expression the wrapper
        # itself executes, and not the docstring.
        (
            '"""Compatibility facade."""\n',
            '"""Trailing note."""\n',
            "standalone string expression(s) besides the module docstring",
        ),
        # A stray string between the docstring and the import.
        (
            '"""Compatibility facade."""\n"""Second string."""\n',
            "",
            "standalone string expression(s) besides the module docstring",
        ),
        # Both at once, so neither rule can be satisfied by the other's string.
        (
            '"""Compatibility facade."""\n"""Second string."""\n',
            '"""Trailing note."""\n',
            "standalone string expression(s) besides the module docstring",
        ),
    ],
    ids=["no-docstring", "trailing-string", "middle-string", "middle-and-trailing"],
)
def test_ingestion_wrapper_requires_exactly_one_leading_module_docstring(
    docstring: str, extra_source: str, pattern: str
) -> None:
    """The accepted direct-facade shape is a *sequence*: one leading docstring, then
    one import. Counting kinds is not enough -- a wrapper with no docstring, or with
    strings bolted on before or after the import, has exactly the permitted kinds in
    the permitted quantities and is not the accepted shape."""
    source = _direct_wrapper_source(
        _INGESTION_MODELS_CANONICAL,
        _INGESTION_MODELS_NAMESPACE,
        extra_source,
        docstring=docstring,
    )
    defects = direct_facade_defects(ast.parse(source), _INGESTION_MODELS_CANONICAL)
    assert any(pattern in defect for defect in defects), defects


def test_ingestion_wrapper_counts_every_stray_string_it_reports() -> None:
    """Strays are reported by count and line, not silently discarded: a wrapper that
    accumulated several must not be describable as one incidental string, and the
    lines say which. The reroute/alias rules still apply to the import underneath,
    so the stray report is the *only* defect here -- proof the strings alone are
    what fail."""
    source = _direct_wrapper_source(
        _INGESTION_MODELS_CANONICAL,
        _INGESTION_MODELS_NAMESPACE,
        extra_source='"""Trailing one."""\n"""Trailing two."""\n',
        docstring='"""Compatibility facade."""\n"""Stray before the import."""\n',
    )
    defects = direct_facade_defects(ast.parse(source), _INGESTION_MODELS_CANONICAL)
    assert len(defects) == 1, defects
    assert "has 3 standalone string expression(s)" in defects[0], defects


#: The two legacy ingestion barrels' exact historical import blocks, in source
#: order. Restated rather than read off the barrels, because those are the files
#: whose edits these gates exist to reject.
_INGESTION_BARREL_HISTORICAL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "chunker",
        ("BaseChunker", "CharacterChunker", "ChunkConfig", "ParagraphChunker"),
    ),
    (
        "extractors",
        ("BaseExtractor", "DOCXExtractor", "MarkdownExtractor", "PDFExtractor"),
    ),
    ("models", ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source")),
    ("pipeline", ("IngestResult", "IngestionPipeline")),
    ("repositories", ("ChunkRepository",)),
    ("scanner", ("FileInfo", "FileScanner", "ScanOptions")),
)
_WATCHER_BARREL_HISTORICAL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("debouncer", ("Debouncer",)),
    (
        "models",
        (
            "DebounceConfig",
            "FileChange",
            "FileChangeBatch",
            "FileChangeType",
            "IndexerScheduler",
            "IndexerState",
            "IndexerStatus",
            "ScheduledJob",
            "SourceReference",
            "WatchedPath",
        ),
    ),
    ("tracker", ("SourceTracker",)),
)


def _ingestion_barrel_source(
    package: str,
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    extra_source: str = "",
) -> str:
    source = ""
    exported: list[str] = []
    for module, names in blocks:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from {package}.{module} import (\n{body})\n"
        exported.extend(names)
    all_body = "".join(f'    "{name}",\n' for name in sorted(exported))
    return source + f"__all__ = [\n{all_body}]\n" + extra_source


def _ingestion_barrel_route_and_children(suffix: str) -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    return (
        manifest.route_for_legacy(f"omnivia_memory.{suffix}"),
        [manifest.route_for_legacy(f"omnivia_memory.{suffix}.models")],
    )


@pytest.mark.parametrize(
    ("suffix", "blocks", "runtime_only"),
    [
        (
            "ingestion",
            _INGESTION_BARREL_HISTORICAL_BLOCKS,
            ("chunker", "extractors", "pipeline", "repositories", "scanner"),
        ),
        (
            "ingestion.watcher",
            _WATCHER_BARREL_HISTORICAL_BLOCKS,
            ("debouncer", "tracker"),
        ),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_barrel_historical_source_is_not_a_transitive_facade(
    suffix: str,
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    runtime_only: tuple[str, ...],
) -> None:
    """Even with the state gate bypassed, neither barrel's historical composition
    can pass the transitive-facade source check: its runtime-only blocks are not
    converted children, and those are the *only* defects -- so the failure is
    attributable to the runtime-owned half specifically, not to the barrel's shape
    or its ``__all__``.

    The input is a synthetic source built from the exact historical import blocks
    and the exact set of names each barrel advertises, not a byte copy of the file:
    ``_ingestion_barrel_source`` emits a sorted ``__all__``, which is not the
    watcher barrel's own order. That is deliberate and harmless here, because this
    check is order-blind -- it compares the imported bindings against ``__all__``
    as sets. Exact source order is pinned separately, against the real files, by
    ``test_ingestion_hybrid_barrel_source_is_unchanged_reexport`` in
    ``tests/compatibility/test_facade_foundation.py``."""
    route, children = _ingestion_barrel_route_and_children(suffix)
    source = _ingestion_barrel_source(f"omnivia_memory.{suffix}", blocks)
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert len(defects) == len(runtime_only), defects
    for module in runtime_only:
        assert any(
            "unapproved module" in defect
            and f"omnivia_memory.{suffix}.{module}" in defect
            for defect in defects
        ), (module, defects)


@pytest.mark.parametrize(
    ("suffix", "blocks"),
    [
        ("ingestion", _INGESTION_BARREL_HISTORICAL_BLOCKS),
        ("ingestion.watcher", _WATCHER_BARREL_HISTORICAL_BLOCKS),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_portable_only_shape_is_the_defect_free_adversary_base(
    suffix: str, blocks: tuple[tuple[str, tuple[str, ...]], ...]
) -> None:
    """The hypothetical promoted barrel -- its single ``models`` block and the
    matching ``__all__`` -- must be defect-free, so each rejection below is proven
    to fail on what it injects."""
    route, children = _ingestion_barrel_route_and_children(suffix)
    portable = tuple(block for block in blocks if block[0] == "models")
    source = _ingestion_barrel_source(f"omnivia_memory.{suffix}", portable)
    assert transitive_facade_defects(ast.parse(source), route, children) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # A direct reroute at the canonical package: the barrel's identity
        # preservation would become direct rather than transitive.
        (
            "from omnivia_core.ingestion.models import Chunk\n",
            "unapproved module",
        ),
        # The runtime-only siblings the *real* barrel imports, absolutely and
        # relatively. They are still not approved children, which is the whole
        # reason the barrel stays a hybrid.
        (
            "from omnivia_memory.ingestion.scanner import FileScanner\n",
            "unapproved module",
        ),
        ("from .chunker import BaseChunker\n", "unapproved module"),
        # The watcher subpackage, which this barrel has never re-exported.
        (
            "from omnivia_memory.ingestion.watcher import Debouncer\n",
            "unapproved module",
        ),
        ("from ..persistence import Database\n", "unapproved module"),
        # A *relative* form of its own child. The resolver approves the module, so
        # what rejects this is the binding/``__all__`` mismatch it creates --
        # pinned so the two branches are not confused for each other.
        (
            "from .models import FileInventory\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        # The two leaf-only names: routed symbols the barrel has never advertised.
        (
            "from omnivia_memory.ingestion.models import IngestSource\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("Chunk = object()\n", "assignments"),
        ("def extract(path):\n    return path\n", "statements of its own"),
        ("class Source:\n    pass\n", "statements of its own"),
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
    ],
)
def test_ingestion_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    route, children = _ingestion_barrel_route_and_children("ingestion")
    portable = tuple(
        block for block in _INGESTION_BARREL_HISTORICAL_BLOCKS if block[0] == "models"
    )
    source = _ingestion_barrel_source(
        "omnivia_memory.ingestion", portable, extra_source=extra_source
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        (
            "from omnivia_core.ingestion.watcher.models import FileChange\n",
            "unapproved module",
        ),
        (
            "from omnivia_memory.ingestion.watcher.tracker import SourceTracker\n",
            "unapproved module",
        ),
        ("from .debouncer import Debouncer\n", "unapproved module"),
        # The parent ingestion barrel: an approved-looking neighbour that is not a
        # child of this one.
        ("from omnivia_memory.ingestion import Chunk\n", "unapproved module"),
        (
            "from .models import SourceReference\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("SourceReference = object()\n", "assignments"),
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
    ],
)
def test_watcher_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    """The watcher barrel's own set. ``tracker`` is the sharp case: it defines a
    *distinct* ``SourceReference`` of its own, so a barrel that started importing
    from it would swap the published contract while every name still resolved."""
    route, children = _ingestion_barrel_route_and_children("ingestion.watcher")
    portable = tuple(
        block for block in _WATCHER_BARREL_HISTORICAL_BLOCKS if block[0] == "models"
    )
    source = _ingestion_barrel_source(
        "omnivia_memory.ingestion.watcher", portable, extra_source=extra_source
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    "suffix", ["ingestion", "ingestion.watcher"]
)
def test_ingestion_barrel_transitive_form_requires_its_converted_child(
    suffix: str,
) -> None:
    """Dropping the one portable block must fail: an empty-bodied barrel is a
    valid shape and has still stopped re-exporting its converted leaf."""
    route, children = _ingestion_barrel_route_and_children(suffix)
    source = _ingestion_barrel_source(f"omnivia_memory.{suffix}", ())
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(
        "does not import converted children" in defect
        and f"omnivia_memory.{suffix}.models" in defect
        for defect in defects
    ), defects


#: --------------------------------------------------------------------------
#: The ``workspace`` pair: the last direct facade, under the sixth hybrid barrel.
#:
#: ``workspace.models`` is a plain ``direct_facade`` leaf -- one import, nothing
#: retained -- and converting it empties ``source_parity`` entirely: no
#: duplicated leaf remains anywhere in the registry. Its barrel still cannot
#: follow, because two of its seven exports (``WorkspaceRepository`` and
#: ``WorkspaceService``) come from the runtime-only ``repository``/``service``
#: leaves. It stays ``pending_hybrid``.
#: --------------------------------------------------------------------------

_WORKSPACE_MODELS_CANONICAL = "omnivia_core.workspace.models"

#: The leaf's exact 14-name public/star namespace, in the source order its
#: wrapper uses. Only the owned names are routes -- 5 of these 14; the rest are
#: the incidental imports the historical module's own namespace also published.
#: Restated here rather than read off the wrapper: this fixture is the adversary
#: base for mutations of that very file.
_WORKSPACE_MODELS_NAMESPACE: tuple[str, ...] = (
    "Any",
    "Enum",
    "ImportSummary",
    "Path",
    "Workspace",
    "WorkspaceCreate",
    "WorkspaceIndexStatus",
    "WorkspaceUpdate",
    "annotations",
    "dataclass",
    "datetime",
    "field",
    "timezone",
    "uuid",
)


def test_workspace_pair_states_and_shapes_are_exact() -> None:
    """The registry's own record of this batch: a ``direct``/``leaf`` pair that is
    ``direct_facade`` and converted, under a ``hybrid_barrel`` that is not.
    Pinned exactly, because every other gate in this section is about what may
    *not* happen to those states."""
    manifest = load_manifest()

    leaf = manifest.route_for_legacy("omnivia_memory.workspace.models")
    assert (leaf.pair_kind, leaf.shape, leaf.migration_state) == (
        PairKind.DIRECT,
        Shape.LEAF,
        MigrationState.DIRECT_FACADE,
    )
    assert leaf.is_converted
    assert leaf.canonical_module == _WORKSPACE_MODELS_CANONICAL

    barrel = manifest.route_for_legacy("omnivia_memory.workspace")
    assert (barrel.pair_kind, barrel.shape, barrel.migration_state) == (
        PairKind.HYBRID_BARREL,
        Shape.BARREL,
        MigrationState.PENDING_HYBRID,
    )
    assert not barrel.is_converted

    for runtime_only in (
        "omnivia_memory.workspace.repository",
        "omnivia_memory.workspace.service",
    ):
        assert runtime_only in manifest.runtime_only_modules
        with pytest.raises(KeyError):
            manifest.route_for_legacy(runtime_only)


def test_no_duplicated_leaf_remains_in_the_registry() -> None:
    """``workspace.models`` was the last of them, so both duplicated-source states
    are now empty and every ``leaf`` route is converted.

    Pinned as its own fact rather than inferred from ``EXPECTED_STATE_SUFFIXES``:
    that map derives ``source_parity`` by subtraction, so it would stay satisfied
    if a leaf were dropped from the registry altogether. This counts the leaves
    instead."""
    manifest = load_manifest()
    assert manifest.by_state(MigrationState.SOURCE_PARITY) == ()
    assert manifest.by_state(MigrationState.CANONICAL_SUBSET) == ()

    leaves = manifest.by_shape(Shape.LEAF)
    assert len(leaves) == manifest.expected_counts.leaf == 30
    assert all(route.is_converted for route in leaves)

    unconverted = [route for route in manifest.routes if not route.is_converted]
    assert {route.shape for route in unconverted} == {Shape.BARREL, Shape.ROOT}
    assert [route.legacy_module for route in unconverted] == [
        "omnivia_memory",
        "omnivia_memory.graph",
        "omnivia_memory.ingestion",
        "omnivia_memory.ingestion.watcher",
        "omnivia_memory.memory",
        "omnivia_memory.memory_graph",
        "omnivia_memory.workspace",
    ]


@pytest.mark.parametrize("state", ["canonical_subset", "source_parity"])
def test_workspace_leaf_direct_facade_state_cannot_be_walked_back(state: str) -> None:
    """Neither duplicated-source state may be re-declared over a leaf that is now
    an exact single-import wrapper: the source no longer duplicates the canonical
    module at all, and relabelling it would silently stop it being checked as
    converted -- and would put the registry back into a ``source_parity`` this
    batch exists to empty."""
    document = _document()
    _route(document, "workspace.models")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_checkout(manifest=document)
    joined = "; ".join(error.value.errors)
    assert f"declared {state!r} but the source is an exact facade" in joined


@pytest.mark.parametrize(
    ("state", "pattern"),
    [
        ("split_facade", "declared 'split_facade'"),
        ("canonical_subset", "move the state forward"),
        ("source_parity", "move the state forward"),
    ],
    ids=["as-split", "as-canonical-subset", "as-source-parity"],
)
def test_validate_route_sources_enforces_the_workspace_state(
    state: str, pattern: str
) -> None:
    """``validate_route_sources`` is the source half of the gate on its own -- no
    checkout rediscovery, no shape agreement, just each route's declared state
    against its file. It must reach the same verdicts as ``validate_checkout``:
    accept the real registry, and reject every mislabelling of the leaf.
    ``split_facade`` is the specific temptation here: a wrapper with no retained
    definitions is not a split facade, and declaring it one would swap in a source
    policy that this wrapper cannot satisfy."""
    validate_route_sources()
    document = _document()
    _route(document, "workspace.models")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match=pattern):
        validate_route_sources(manifest=document)


@pytest.mark.parametrize(
    "state",
    [
        "transitive_facade",
        "pending_direct_barrel",
        "direct_facade",
        "split_facade",
        "source_parity",
        "canonical_subset",
        "pending_root",
    ],
)
def test_workspace_barrel_cannot_be_promoted_out_of_pending_hybrid(state: str) -> None:
    """The barrel's one portable child is converted, so ``transitive_facade`` looks
    tempting -- and it is exactly wrong: the barrel also re-exports from two
    runtime-only leaves that are not routes and can never be converted children.
    ``pending_hybrid`` is the only state a ``hybrid_barrel``/``barrel`` pair may be
    in, so every other value is rejected at load time by the combination rule
    rather than needing a state-specific waiver."""
    document = _document()
    _route(document, "workspace")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="valid combination"):
        load_manifest(document)


def test_workspace_wrapper_shape_is_the_defect_free_adversary_base() -> None:
    """The accepted shape -- a docstring and exactly one absolute, unaliased,
    non-star import of the paired canonical module -- must be defect-free, so each
    rejection below is proven to fail on what it injects rather than on the shape
    itself."""
    source = _direct_wrapper_source(
        _WORKSPACE_MODELS_CANONICAL, _WORKSPACE_MODELS_NAMESPACE
    )
    assert direct_facade_defects(ast.parse(source), _WORKSPACE_MODELS_CANONICAL) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # An ``__all__`` of its own: the leaf never declared one, and adding it
        # would let the wrapper advertise a surface its route does not decide.
        ('__all__ = ["Workspace"]\n', "statements of its own"),
        # A local definition, of either kind.
        ("class Workspace:\n    pass\n", "statements of its own"),
        # The private timestamp helper the canonical module keeps. Redefining it
        # here is the sharpest local-definition case: the canonical dataclasses'
        # ``default_factory`` still points at *their* ``_now``, so a wrapper that
        # grew one would change nothing observable and still not be a facade.
        (
            "def _now() -> str:\n    return ''\n",
            "statements of its own",
        ),
        # A dynamic hook: every name must be decided at import time.
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
        ("import uuid\n", "statements of its own"),
        (
            "import sys\nsys.modules[__name__].__dict__['probe'] = 1\n",
            "statements of its own",
        ),
        # A second from-import, whether it reaches a sibling canonical leaf, the
        # legacy runtime, or ``__future__``.
        (
            "from omnivia_core.provenance.models import SourceType\n",
            "from-imports, expected exactly one",
        ),
        (
            "from omnivia_memory.workspace.repository import WorkspaceRepository\n",
            "from-imports, expected exactly one",
        ),
        ("from __future__ import annotations\n", "from-imports, expected exactly one"),
    ],
)
def test_workspace_wrapper_rejects_a_local_definition_or_second_import(
    extra_source: str,
    pattern: str,
) -> None:
    """Injected into the defect-free base above, so the asserted pattern is the
    one the mutation causes."""
    source = _direct_wrapper_source(
        _WORKSPACE_MODELS_CANONICAL, _WORKSPACE_MODELS_NAMESPACE, extra_source
    )
    defects = direct_facade_defects(ast.parse(source), _WORKSPACE_MODELS_CANONICAL)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("source", "pattern"),
    [
        # The wrong canonical module -- including the barrel one level up, which
        # is the near miss a copy/paste would produce and which really does bind
        # four of the five routed names.
        (
            "from omnivia_core.workspace import Workspace\n",
            "imports from 'omnivia_core.workspace'",
        ),
        (
            "from omnivia_core.memory.models import Memory\n",
            "imports from 'omnivia_core.memory.models'",
        ),
        # The legacy path: a wrapper that re-exported from its own tree would keep
        # every name resolving and route nothing.
        (
            "from omnivia_memory.workspace.models import Workspace\n",
            "imports from 'omnivia_memory.workspace.models'",
        ),
        # Relative, star, and aliased forms of the one permitted statement.
        ("from .models import Workspace\n", "relative import"),
        ("from omnivia_core.workspace.models import *\n", "star import"),
        # The alias case is the sharpest: ``WorkspaceIndexStatus`` is a real name
        # in this leaf's namespace, so an aliasing wrapper would publish a
        # namespace that looks right and is built by renaming rather than routing.
        (
            (
                "from omnivia_core.workspace.models import "
                "WorkspaceIndexStatus as IndexStatus\n"
            ),
            "aliases 'WorkspaceIndexStatus' as 'IndexStatus'",
        ),
        (
            "from omnivia_core.workspace.models import Workspace as WorkspaceCreate\n",
            "aliases 'Workspace' as 'WorkspaceCreate'",
        ),
    ],
    ids=[
        "imports-its-own-barrel",
        "imports-another-domain",
        "imports-legacy-self",
        "relative",
        "star",
        "aliases-the-index-status",
        "aliases-one-routed-name-as-another",
    ],
)
def test_workspace_wrapper_rejects_a_reroute_star_or_alias(
    source: str, pattern: str
) -> None:
    """Each of these replaces the one permitted statement rather than adding to
    it, so the wrapper still has exactly one from-import and the defect reported
    is the reroute/star/alias itself."""
    defects = direct_facade_defects(
        ast.parse(f'"""Doc."""\n{source}'), _WORKSPACE_MODELS_CANONICAL
    )
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("docstring", "extra_source", "pattern"),
    [
        # No docstring at all. This is the case a blanket "ignore every module
        # string expression" filter cannot see: the remaining body is exactly one
        # from-import of the right module with no statements of its own, so every
        # other rule is satisfied and only the positional docstring rule rejects
        # it. A wrapper is a documented boundary, not an anonymous re-export.
        ("", "", "does not open with a module docstring"),
        # A stray string after the import: a module-scope expression the wrapper
        # itself executes, and not the docstring.
        (
            '"""Compatibility facade."""\n',
            '"""Trailing note."""\n',
            "standalone string expression(s) besides the module docstring",
        ),
        # A stray string between the docstring and the import.
        (
            '"""Compatibility facade."""\n"""Second string."""\n',
            "",
            "standalone string expression(s) besides the module docstring",
        ),
        # Both at once, so neither rule can be satisfied by the other's string.
        (
            '"""Compatibility facade."""\n"""Second string."""\n',
            '"""Trailing note."""\n',
            "standalone string expression(s) besides the module docstring",
        ),
    ],
    ids=["no-docstring", "trailing-string", "middle-string", "middle-and-trailing"],
)
def test_workspace_wrapper_requires_exactly_one_leading_module_docstring(
    docstring: str, extra_source: str, pattern: str
) -> None:
    """The accepted direct-facade shape is a *sequence*: one leading docstring, then
    one import. Counting kinds is not enough -- a wrapper with no docstring, or with
    strings bolted on before or after the import, has exactly the permitted kinds in
    the permitted quantities and is not the accepted shape."""
    source = _direct_wrapper_source(
        _WORKSPACE_MODELS_CANONICAL,
        _WORKSPACE_MODELS_NAMESPACE,
        extra_source,
        docstring=docstring,
    )
    defects = direct_facade_defects(ast.parse(source), _WORKSPACE_MODELS_CANONICAL)
    assert any(pattern in defect for defect in defects), defects


#: The legacy ``workspace`` barrel's exact historical import blocks, in source
#: order. Restated rather than read off the barrel, because that is the file
#: whose edits these gates exist to reject.
_WORKSPACE_BARREL_HISTORICAL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "models",
        (
            "ImportSummary",
            "Workspace",
            "WorkspaceCreate",
            "WorkspaceIndexStatus",
            "WorkspaceUpdate",
        ),
    ),
    ("repository", ("WorkspaceRepository",)),
    ("service", ("WorkspaceService",)),
)


def _workspace_barrel_route_and_children() -> tuple[Any, list[Any]]:
    """This barrel's route and its one converted child.

    The synthetic barrel sources below are built with the ingestion section's
    ``_ingestion_barrel_source``: it is a generic ``(module, names)`` -> source
    builder, and its sorted ``__all__`` happens to be exactly this barrel's own
    order.
    """
    manifest = load_manifest()
    return (
        manifest.route_for_legacy("omnivia_memory.workspace"),
        [manifest.route_for_legacy("omnivia_memory.workspace.models")],
    )


def test_workspace_barrel_historical_source_is_not_a_transitive_facade() -> None:
    """Even with the state gate bypassed, the barrel's historical composition
    cannot pass the transitive-facade source check: its two runtime-only blocks
    are not converted children, and those are the *only* defects -- so the failure
    is attributable to the runtime-owned half specifically, not to the barrel's
    shape or its ``__all__``.

    The input is a synthetic source built from the exact historical import blocks
    and the exact set of names the barrel advertises, not a byte copy of the file.
    Exact source order is pinned separately, against the real file, by
    ``test_workspace_hybrid_barrel_source_is_unchanged_reexport`` in
    ``tests/compatibility/test_facade_foundation.py``."""
    route, children = _workspace_barrel_route_and_children()
    source = _ingestion_barrel_source(
        "omnivia_memory.workspace", _WORKSPACE_BARREL_HISTORICAL_BLOCKS
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert len(defects) == 2, defects
    for module in ("repository", "service"):
        assert any(
            "unapproved module" in defect
            and f"omnivia_memory.workspace.{module}" in defect
            for defect in defects
        ), (module, defects)


def test_workspace_portable_only_shape_is_the_defect_free_adversary_base() -> None:
    """The hypothetical promoted barrel -- its single ``models`` block and the
    matching ``__all__`` -- must be defect-free, so each rejection below is proven
    to fail on what it injects."""
    route, children = _workspace_barrel_route_and_children()
    portable = tuple(
        block for block in _WORKSPACE_BARREL_HISTORICAL_BLOCKS if block[0] == "models"
    )
    source = _ingestion_barrel_source("omnivia_memory.workspace", portable)
    assert transitive_facade_defects(ast.parse(source), route, children) == []


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # A direct reroute at the canonical package: the barrel's identity
        # preservation would become direct rather than transitive.
        (
            "from omnivia_core.workspace.models import Workspace\n",
            "unapproved module",
        ),
        # The runtime-only siblings the *real* barrel imports, absolutely and
        # relatively. They are still not approved children, which is the whole
        # reason the barrel stays a hybrid.
        (
            "from omnivia_memory.workspace.service import WorkspaceService\n",
            "unapproved module",
        ),
        ("from .repository import WorkspaceRepository\n", "unapproved module"),
        # A sibling domain the barrel has never re-exported.
        ("from omnivia_memory.persistence import Database\n", "unapproved module"),
        ("from ..memory.models import Memory\n", "unapproved module"),
        # A *relative* form of its own child. The resolver approves the module, so
        # what rejects this is the binding/``__all__`` mismatch it creates --
        # pinned so the two branches are not confused for each other.
        (
            "from .models import Workspace\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        # One of the leaf's incidental bindings: importable from the child, and a
        # name the barrel has never advertised.
        (
            "from omnivia_memory.workspace.models import Path\n",
            "the imported binding set does not exactly match the literal __all__",
        ),
        ("Workspace = object()\n", "assignments"),
        ("def create(name):\n    return name\n", "statements of its own"),
        ("class WorkspaceService:\n    pass\n", "statements of its own"),
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "statements of its own",
        ),
    ],
)
def test_workspace_barrel_transitive_form_rejects_a_reroute_or_local_definition(
    extra_source: str,
    pattern: str,
) -> None:
    route, children = _workspace_barrel_route_and_children()
    portable = tuple(
        block for block in _WORKSPACE_BARREL_HISTORICAL_BLOCKS if block[0] == "models"
    )
    source = _ingestion_barrel_source(
        "omnivia_memory.workspace", portable, extra_source=extra_source
    )
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(pattern in defect for defect in defects), defects


def test_workspace_barrel_transitive_form_requires_its_converted_child() -> None:
    """Dropping the one portable block must fail: an empty-bodied barrel is a
    valid shape and has still stopped re-exporting its converted leaf."""
    route, children = _workspace_barrel_route_and_children()
    source = _ingestion_barrel_source("omnivia_memory.workspace", ())
    defects = transitive_facade_defects(ast.parse(source), route, children)
    assert any(
        "does not import converted children" in defect
        and "omnivia_memory.workspace.models" in defect
        for defect in defects
    ), defects

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
    # The per-state counts this batch moved: ``workspace.models`` from
    # ``source_parity`` into ``direct_facade``, which empties ``source_parity``.
    # Nothing else moved: its barrel is a hybrid, so no barrel changed state with
    # it (``transitive_facade`` and ``pending_hybrid`` both stay where they were),
    # and neither ``canonical_subset`` nor ``split_facade`` is touched. It was the
    # last unconverted leaf, so the remaining-leaf count reaches zero and only the
    # six hybrid barrels and the package root are left.
    assert "canonical_subset: 0" in result.stdout
    assert "source_parity: 0" in result.stdout
    assert "direct_facade: 29" in result.stdout
    assert "split_facade: 1" in result.stdout
    assert "transitive_facade: 10" in result.stdout
    assert "pending_direct_barrel: 0" in result.stdout
    assert "pending_hybrid: 6" in result.stdout
    assert "pending_root: 1" in result.stdout
    assert "remaining: 0 leaves and 6 barrels still to convert" in result.stdout


def _checker_module() -> Any:
    """The checker script, imported by path: its name is not an identifier."""
    path = REPO_ROOT / "scripts" / "check-facade-routes.py"
    spec = importlib.util.spec_from_file_location("_check_facade_routes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 leaves"), (1, "1 leaf"), (2, "2 leaves"), (11, "11 leaves")],
)
def test_checker_pluralizes_its_remaining_counts(count: int, expected: str) -> None:
    """The remaining-work line is read by a human deciding whether the batch landed,
    and the counts walk down through one to none as leaves are converted. Only one
    is singular."""
    assert _checker_module()._counted(count, "leaf", "leaves") == expected
