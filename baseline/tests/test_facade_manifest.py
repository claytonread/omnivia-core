"""Fail-closed coverage for the frozen compatibility-facade route registry."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from baseline.facade_manifest import (
    CANONICAL_ROOT_ALL,
    MANIFEST_PATH,
    REPO_ROOT,
    ROOT_FACADE_ALL,
    ROOT_FACADE_CANONICAL_IMPORTS,
    ROOT_FACADE_HIDDEN_BINDINGS,
    ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS,
    ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS,
    ROOT_FACADE_RUNTIME_IMPORTS,
    ROOT_FACADE_VERSION_BINDING,
    ROOT_FACADE_VERSION_MODULE,
    SCHEMA_PATH,
    FacadeManifestError,
    FacadeRoute,
    MigrationState,
    PairKind,
    Shape,
    canonical_root_defects,
    canonical_root_source_path,
    direct_facade_defects,
    discover_package_modules,
    hybrid_facade_defects,
    legacy_source_path,
    load_manifest,
    root_facade_defects,
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
        # ``memory.models`` above: ``memory_graph`` is an accepted
        # ``hybrid_facade`` below -- its portable exports are canonical through
        # these four converted children, while the exports owned by its two
        # runtime-only children stay legacy, so it is not a pure transitive
        # facade.
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
    #: All six hybrid barrels, promoted together. Each is source-unchanged: its
    #: portable half now hops through converted children to canonical objects
    #: while its runtime half still resolves at the declared runtime-only
    #: descendants. They move as one set because they are structurally the same
    #: thing -- promoting some and not others would make the state mean
    #: "whichever hybrids happened to be looked at".
    MigrationState.HYBRID_FACADE: EXPECTED_HYBRID_SUFFIXES,
    # Empty on purpose: ``knowledge`` was the last barrel pending direct
    # conversion, so every ``direct`` barrel is now a ``transitive_facade`` and
    # only the six ``hybrid_barrel`` barrels and the package root are still
    # pending. The state stays listed rather than dropped so the partition below
    # keeps asserting that nothing has quietly re-entered it.
    MigrationState.PENDING_DIRECT_BARREL: set(),
    # Empty on purpose, for the same reason: all six hybrid barrels are now
    # ``hybrid_facade`` above, so nothing is pending conversion at all. The state
    # stays listed so a walk-back is a partition failure here rather than a
    # silent re-entry.
    MigrationState.PENDING_HYBRID: set(),
    #: The package root, and the terminal state of the whole registry: every
    #: route is now converted under its own contract.
    MigrationState.ROOT_FACADE: {""},
    # Empty on purpose, like the other two pending states. The root is the only
    # route that could ever be in it, and it has moved forward.
    MigrationState.PENDING_ROOT: set(),
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
#: re-export of the canonical package -- and it does not have to: the registry
#: now records it as an accepted ``hybrid_facade``. Its portable exports are the
#: canonical objects, reached through those four converted children, while the
#: exports the two runtime-only children own stay the exact legacy objects. That
#: mixed surface is precisely why it is not a ``transitive_facade``.
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


def test_memory_graph_barrel_is_a_hybrid_facade_over_converted_children() -> None:
    """The registry's own record of the split: a ``hybrid_barrel`` pair whose four
    portable children are converted and whose two runtime-only children are not
    routes at all. That is exactly what ``hybrid_facade`` means, and the state now
    says so. Pinned exactly, because every other gate in this section is about what
    may *not* happen to it."""
    manifest = load_manifest()
    route = manifest.route_for_legacy("omnivia_memory.memory_graph")
    assert route.pair_kind is PairKind.HYBRID_BARREL
    assert route.shape is Shape.BARREL
    assert route.migration_state is MigrationState.HYBRID_FACADE
    assert route.is_converted
    assert "memory_graph" in {
        item.suffix for item in manifest.by_state(MigrationState.HYBRID_FACADE)
    }
    assert manifest.by_state(MigrationState.PENDING_HYBRID) == ()

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
def test_memory_graph_barrel_admits_no_state_but_its_two_hybrid_ones(
    state: str,
) -> None:
    """Now that all four portable children are converted, ``transitive_facade``
    looks tempting -- and it is exactly wrong: the barrel would stop being
    source-checked against its two runtime-only children, which are not routes and
    can never be converted children. ``hybrid_facade`` and ``pending_hybrid`` are
    the only two states a ``hybrid_barrel``/``barrel`` pair may be in at all, so
    every other value (including the two leaf-only states and the root's) is
    rejected at load time by the combination rule rather than needing a
    state-specific waiver."""
    document = _document()
    _route(document, "memory_graph")["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="valid combination"):
        load_manifest(document)


def test_memory_graph_barrel_cannot_be_walked_back_to_pending_hybrid() -> None:
    """The one state that *is* still a valid combination for this pair is the one
    it just left, so the combination rule cannot be what stops the walk-back. The
    source gate is: every routed child is converted and the barrel's own unchanged
    source is already an exact hybrid facade, so declaring it pending would be the
    registry understating what the file does."""
    document = _document()
    _route(document, "memory_graph")["migration_state"] = "pending_hybrid"
    load_manifest(document)
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_route_sources(manifest=document)
    joined = "; ".join(error.value.errors)
    assert "omnivia_memory.memory_graph" in joined
    assert "declared 'pending_hybrid'" in joined


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
    ``hybrid_barrel`` that is now a converted ``hybrid_facade`` too -- its two
    portable blocks hop through those leaves, its ``search_service`` block stays
    legacy. Pinned exactly, because every other gate in this section is about what
    may *not* happen to those states."""
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
        MigrationState.HYBRID_FACADE,
    )
    assert barrel.is_converted

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


def test_graph_barrel_cannot_become_a_transitive_facade_or_any_leaf_state() -> None:
    """Both portable children are converted, so ``transitive_facade`` looks
    tempting -- and it is exactly wrong: the barrel also re-exports
    ``GraphSearchError``/``GraphSearchService`` from the runtime-only
    ``search_service`` leaf, which is not a route and can never be a converted
    child. ``hybrid_facade`` is what it is instead, and no other state is even a
    valid combination for the pair."""
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
#: barrel above them can become a pure re-export of the canonical package:
#: fourteen of the ``ingestion`` barrel's nineteen exports come from the
#: runtime-only chunker/extractor/pipeline/repository/scanner leaves, and two of
#: the ``ingestion.watcher`` barrel's twelve come from the runtime-only
#: ``debouncer``/``tracker``. Both are accepted ``hybrid_facade`` routes instead:
#: portable exports canonical through the converted child leaf, runtime exports
#: still the exact legacy objects, and therefore neither is a
#: ``transitive_facade``.
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
            MigrationState.HYBRID_FACADE,
        )
        assert barrel.is_converted

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
def test_ingestion_barrels_admit_no_state_but_their_two_hybrid_ones(
    suffix: str, state: str
) -> None:
    """Each barrel's portable children are converted, so ``transitive_facade``
    looks tempting -- and it is exactly wrong: both barrels also re-export from
    runtime-only leaves that are not routes and can never be converted children.
    ``hybrid_facade`` and ``pending_hybrid`` are the only two states a
    ``hybrid_barrel``/``barrel`` pair may be in at all, so every other value is
    rejected at load time by the combination rule rather than needing a
    state-specific waiver."""
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
#: become a pure re-export of the canonical package, because two of its seven
#: exports (``WorkspaceRepository`` and ``WorkspaceService``) come from the
#: runtime-only ``repository``/``service`` leaves. It is an accepted
#: ``hybrid_facade`` route instead: its five portable exports are canonical
#: through the converted ``workspace.models`` leaf while those two stay the exact
#: legacy objects, which is why it is not a ``transitive_facade``.
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
        MigrationState.HYBRID_FACADE,
    )
    assert barrel.is_converted

    for runtime_only in (
        "omnivia_memory.workspace.repository",
        "omnivia_memory.workspace.service",
    ):
        assert runtime_only in manifest.runtime_only_modules
        with pytest.raises(KeyError):
            manifest.route_for_legacy(runtime_only)


def test_no_unconverted_route_remains_in_the_registry() -> None:
    """``workspace.models`` was the last duplicated leaf, the six hybrid barrels
    were promoted after it, and the package root has now followed: both
    duplicated-source states are empty and every ``leaf``, ``barrel`` and ``root``
    route is converted under its own contract.

    Pinned as its own fact rather than inferred from ``EXPECTED_STATE_SUFFIXES``:
    that map derives ``source_parity`` by subtraction, so it would stay satisfied
    if a route were dropped from the registry altogether. This counts the leaves,
    barrels and roots instead."""
    manifest = load_manifest()
    assert manifest.by_state(MigrationState.SOURCE_PARITY) == ()
    assert manifest.by_state(MigrationState.CANONICAL_SUBSET) == ()

    leaves = manifest.by_shape(Shape.LEAF)
    assert len(leaves) == manifest.expected_counts.leaf == 30
    assert all(route.is_converted for route in leaves)

    barrels = manifest.by_shape(Shape.BARREL)
    assert len(barrels) == manifest.expected_counts.barrel == 16
    assert all(route.is_converted for route in barrels)

    roots = manifest.by_shape(Shape.ROOT)
    assert len(roots) == manifest.expected_counts.root == 1
    assert all(route.is_converted for route in roots)

    assert 30 + 16 + 1 == manifest.expected_counts.routes == 47
    assert [route.legacy_module for route in manifest.routes if not route.is_converted] == []


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
def test_workspace_barrel_admits_no_state_but_its_two_hybrid_ones(state: str) -> None:
    """The barrel's one portable child is converted, so ``transitive_facade`` looks
    tempting -- and it is exactly wrong: the barrel also re-exports from two
    runtime-only leaves that are not routes and can never be converted children.
    ``hybrid_facade`` and ``pending_hybrid`` are the only two states a
    ``hybrid_barrel``/``barrel`` pair may be in at all, so every other value is
    rejected at load time by the combination rule rather than needing a
    state-specific waiver."""
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
    # The per-state counts this batch moved: the package root from ``pending_root``
    # into the new ``root_facade``, which empties ``pending_root`` and leaves every
    # pending state at zero. Nothing else moved -- no leaf and no barrel changed
    # state, so ``source_parity``, ``canonical_subset``, ``direct_facade``,
    # ``split_facade``, ``transitive_facade`` and ``hybrid_facade`` all stay exactly
    # where the previous batches left them.
    assert "canonical_subset: 0" in result.stdout
    assert "source_parity: 0" in result.stdout
    assert "direct_facade: 29" in result.stdout
    assert "split_facade: 1" in result.stdout
    assert "transitive_facade: 10" in result.stdout
    assert "hybrid_facade: 6" in result.stdout
    assert "root_facade: 1" in result.stdout
    assert "pending_direct_barrel: 0" in result.stdout
    assert "pending_hybrid: 0" in result.stdout
    assert "pending_root: 0" in result.stdout
    # The remaining-work line names all three shapes. The root is *not* excluded
    # from it: 29 + 1 + 10 + 6 + 1 == 47 converted routes, so the proof a reviewer
    # reads says zero leaves, zero barrels *and* zero roots are left, rather than
    # printing "nothing remaining" while the one route that publishes the whole
    # advertised surface sat outside the count.
    assert "remaining: 0 leaves, 0 barrels and 0 roots still to convert" in result.stdout
    assert 29 + 1 + 10 + 6 + 1 == 47


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


# ---------------------------------------------------------------------------
# The six ``hybrid_facade`` barrels.
#
# A hybrid facade is a *source-unchanged* legacy barrel that is nonetheless
# converted for compatibility accounting: its portable bindings resolve
# transitively, through already-converted routed children, to the exact
# canonical Core objects, while its remaining bindings stay the exact legacy
# objects imported from descendant modules the registry declares runtime-only.
# Nothing moves into Core. What ``hybrid_facade_defects`` asserts is that the
# barrel reaches *nothing else*, and that the two halves really are both there:
# a barrel reaching only converted children is a ``transitive_facade``, and one
# reaching only runtime-only modules never had a portable half to convert.
#
# The six barrels were promoted in one batch because they are structurally the
# same thing. Splitting them would leave structurally equivalent converted
# hybrids in different states, which is exactly what the state exists to stop.
# ---------------------------------------------------------------------------

#: Each barrel's exact source shape: the ordered import blocks
#: ``(absolute legacy module, imported names in source order)`` and the exact
#: ordered ``__all__`` literal. Restated here rather than read off the six
#: files, because these are the files whose edits these fixtures exist to
#: reject. ``portable`` and ``runtime`` partition the barrel's exports by
#: whether they hop through a converted child or stay legacy-owned.
_HYBRID_BARREL_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "suffix": "graph",
        "blocks": (
            (
                "omnivia_memory.graph.models",
                (
                    "ApprovalStatus",
                    "Entity",
                    "EntityType",
                    "Relationship",
                    "RelationshipType",
                ),
            ),
            (
                "omnivia_memory.graph.search_models",
                ("GraphSearchQuery", "GraphSearchResult", "GraphSearchResultSet"),
            ),
            (
                "omnivia_memory.graph.search_service",
                ("GraphSearchError", "GraphSearchService"),
            ),
        ),
        "all": (
            "ApprovalStatus",
            "Entity",
            "EntityType",
            "GraphSearchError",
            "GraphSearchQuery",
            "GraphSearchResult",
            "GraphSearchResultSet",
            "GraphSearchService",
            "Relationship",
            "RelationshipType",
        ),
        "portable": (
            "ApprovalStatus",
            "Entity",
            "EntityType",
            "GraphSearchQuery",
            "GraphSearchResult",
            "GraphSearchResultSet",
            "Relationship",
            "RelationshipType",
        ),
        "runtime": ("GraphSearchError", "GraphSearchService"),
    },
    {
        "suffix": "ingestion",
        "blocks": (
            (
                "omnivia_memory.ingestion.chunker",
                ("BaseChunker", "CharacterChunker", "ChunkConfig", "ParagraphChunker"),
            ),
            (
                "omnivia_memory.ingestion.extractors",
                ("BaseExtractor", "DOCXExtractor", "MarkdownExtractor", "PDFExtractor"),
            ),
            (
                "omnivia_memory.ingestion.models",
                ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source"),
            ),
            (
                "omnivia_memory.ingestion.pipeline",
                ("IngestResult", "IngestionPipeline"),
            ),
            ("omnivia_memory.ingestion.repositories", ("ChunkRepository",)),
            (
                "omnivia_memory.ingestion.scanner",
                ("FileInfo", "FileScanner", "ScanOptions"),
            ),
        ),
        "all": (
            "BaseChunker",
            "BaseExtractor",
            "CharacterChunker",
            "Chunk",
            "ChunkConfig",
            "ChunkRepository",
            "DOCXExtractor",
            "ExtractionResult",
            "FileInfo",
            "FileScanner",
            "FileType",
            "IngestResult",
            "IngestionPipeline",
            "MarkdownExtractor",
            "PDFExtractor",
            "ParagraphChunker",
            "ParseStatus",
            "ScanOptions",
            "Source",
        ),
        "portable": ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source"),
        "runtime": (
            "BaseChunker",
            "BaseExtractor",
            "CharacterChunker",
            "ChunkConfig",
            "ChunkRepository",
            "DOCXExtractor",
            "FileInfo",
            "FileScanner",
            "IngestResult",
            "IngestionPipeline",
            "MarkdownExtractor",
            "PDFExtractor",
            "ParagraphChunker",
            "ScanOptions",
        ),
    },
    {
        "suffix": "ingestion.watcher",
        "blocks": (
            ("omnivia_memory.ingestion.watcher.debouncer", ("Debouncer",)),
            (
                "omnivia_memory.ingestion.watcher.models",
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
            ("omnivia_memory.ingestion.watcher.tracker", ("SourceTracker",)),
        ),
        "all": (
            "Debouncer",
            "DebounceConfig",
            "FileChange",
            "FileChangeBatch",
            "FileChangeType",
            "IndexerScheduler",
            "IndexerState",
            "IndexerStatus",
            "ScheduledJob",
            "SourceReference",
            "SourceTracker",
            "WatchedPath",
        ),
        "portable": (
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
        "runtime": ("Debouncer", "SourceTracker"),
    },
    {
        "suffix": "memory",
        "blocks": (
            (
                "omnivia_memory.memory.models",
                ("Memory", "MemoryCreate", "MemoryUpdate"),
            ),
            (
                "omnivia_memory.memory.service",
                (
                    "InvalidTransitionError",
                    "MemoryNotFoundError",
                    "MemoryService",
                    "MemoryServiceError",
                ),
            ),
        ),
        # Not sorted, and not the import order either: the service block's four
        # names are imported alphabetically but advertised in their historical
        # order. Both orders are pinned, separately, on purpose.
        "all": (
            "Memory",
            "MemoryCreate",
            "MemoryUpdate",
            "MemoryService",
            "MemoryServiceError",
            "MemoryNotFoundError",
            "InvalidTransitionError",
        ),
        "portable": ("Memory", "MemoryCreate", "MemoryUpdate"),
        "runtime": (
            "MemoryService",
            "MemoryServiceError",
            "MemoryNotFoundError",
            "InvalidTransitionError",
        ),
    },
    {
        "suffix": "memory_graph",
        "blocks": (
            (
                "omnivia_memory.memory_graph.assembly",
                (
                    "assemble_evidence_graph",
                    "assemble_graph_preview",
                    "redact_segment_preview",
                ),
            ),
            (
                "omnivia_memory.memory_graph.fixtures",
                ("FIXTURE_TIME", "MemoryGraphFixture", "build_memory_graph_fixture"),
            ),
            (
                "omnivia_memory.memory_graph.ingestion_adapter",
                (
                    "IngestionGraphAdapterError",
                    "IngestionGraphWriteResult",
                    "chunk_to_memory_segment",
                    "source_to_memory_source",
                    "write_ingestion_records_to_graph",
                ),
            ),
            (
                "omnivia_memory.memory_graph.store",
                ("MemoryGraphStore", "MemoryGraphStoreError"),
            ),
            (
                "omnivia_memory.memory_graph.models",
                (
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
                ),
            ),
            (
                "omnivia_memory.memory_graph.validation",
                (
                    "ValidationResult",
                    "validate_evidence_graph_response",
                    "validate_graph_preview_response",
                    "validate_memory_entity",
                    "validate_memory_fact",
                    "validate_memory_segment",
                    "validate_memory_source",
                ),
            ),
        ),
        "all": (
            "Confidence",
            "EvidenceGraphResponse",
            "FIXTURE_TIME",
            "GraphPreviewEdge",
            "GraphPreviewKind",
            "GraphPreviewNode",
            "GraphPreviewResponse",
            "GraphPreviewState",
            "IngestionGraphAdapterError",
            "IngestionGraphWriteResult",
            "MemoryEntity",
            "MemoryFact",
            "MemoryFactStatus",
            "MemoryGraphFixture",
            "MemoryGraphStore",
            "MemoryGraphStoreError",
            "MemorySegment",
            "MemorySegmentKind",
            "MemorySource",
            "MemorySourceFreshness",
            "MemorySourceStatus",
            "MemorySourceType",
            "RetrievalTrace",
            "SourceRef",
            "ValidationResult",
            "assemble_evidence_graph",
            "assemble_graph_preview",
            "build_memory_graph_fixture",
            "chunk_to_memory_segment",
            "redact_segment_preview",
            "source_to_memory_source",
            "validate_evidence_graph_response",
            "validate_graph_preview_response",
            "validate_memory_entity",
            "validate_memory_fact",
            "validate_memory_segment",
            "validate_memory_source",
            "write_ingestion_records_to_graph",
        ),
        "portable": (
            "Confidence",
            "EvidenceGraphResponse",
            "FIXTURE_TIME",
            "GraphPreviewEdge",
            "GraphPreviewKind",
            "GraphPreviewNode",
            "GraphPreviewResponse",
            "GraphPreviewState",
            "MemoryEntity",
            "MemoryFact",
            "MemoryFactStatus",
            "MemoryGraphFixture",
            "MemorySegment",
            "MemorySegmentKind",
            "MemorySource",
            "MemorySourceFreshness",
            "MemorySourceStatus",
            "MemorySourceType",
            "RetrievalTrace",
            "SourceRef",
            "ValidationResult",
            "assemble_evidence_graph",
            "assemble_graph_preview",
            "build_memory_graph_fixture",
            "redact_segment_preview",
            "validate_evidence_graph_response",
            "validate_graph_preview_response",
            "validate_memory_entity",
            "validate_memory_fact",
            "validate_memory_segment",
            "validate_memory_source",
        ),
        "runtime": (
            "IngestionGraphAdapterError",
            "IngestionGraphWriteResult",
            "MemoryGraphStore",
            "MemoryGraphStoreError",
            "chunk_to_memory_segment",
            "source_to_memory_source",
            "write_ingestion_records_to_graph",
        ),
    },
    {
        "suffix": "workspace",
        "blocks": (
            (
                "omnivia_memory.workspace.models",
                (
                    "ImportSummary",
                    "Workspace",
                    "WorkspaceCreate",
                    "WorkspaceIndexStatus",
                    "WorkspaceUpdate",
                ),
            ),
            ("omnivia_memory.workspace.repository", ("WorkspaceRepository",)),
            ("omnivia_memory.workspace.service", ("WorkspaceService",)),
        ),
        "all": (
            "ImportSummary",
            "Workspace",
            "WorkspaceCreate",
            "WorkspaceIndexStatus",
            "WorkspaceRepository",
            "WorkspaceService",
            "WorkspaceUpdate",
        ),
        "portable": (
            "ImportSummary",
            "Workspace",
            "WorkspaceCreate",
            "WorkspaceIndexStatus",
            "WorkspaceUpdate",
        ),
        "runtime": ("WorkspaceRepository", "WorkspaceService"),
    },
)

#: The aggregate partition the six barrels advertise: 93 legacy exports, 62 of
#: them portable canonical identities and 31 runtime legacy ones.
_HYBRID_TOTALS = (93, 62, 31)


def _hybrid_id(barrel: dict[str, Any]) -> str:
    return str(barrel["suffix"])


def _hybrid_source(
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    exported: tuple[str, ...],
    extra_source: str = "",
    *,
    docstring: str = '"""Barrel."""\n',
) -> str:
    """A synthetic hybrid barrel: docstring, the import blocks, one ``__all__``."""
    source = docstring
    for module, names in blocks:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from {module} import (\n{body})\n"
    all_body = "".join(f'    "{name}",\n' for name in exported)
    return source + f"__all__ = [\n{all_body}]\n" + extra_source


def _hybrid_route_and_children(suffix: str) -> tuple[Any, list[Any]]:
    manifest = load_manifest()
    route = manifest.route_for_legacy(f"omnivia_memory.{suffix}")
    children = [
        item
        for item in manifest.routes
        if item.suffix and item.suffix.rpartition(".")[0] == suffix
    ]
    return route, children


def _hybrid_defects(
    barrel: dict[str, Any],
    blocks: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    exported: tuple[str, ...] | None = None,
    extra_source: str = "",
    *,
    docstring: str = '"""Barrel."""\n',
    children: list[Any] | None = None,
) -> list[str]:
    route, routed_children = _hybrid_route_and_children(barrel["suffix"])
    source = _hybrid_source(
        barrel["blocks"] if blocks is None else blocks,
        barrel["all"] if exported is None else exported,
        extra_source,
        docstring=docstring,
    )
    return hybrid_facade_defects(
        ast.parse(source),
        route,
        routed_children if children is None else children,
        load_manifest().runtime_only_modules,
    )


def _portable_blocks(barrel: dict[str, Any]) -> tuple[str, ...]:
    """The barrel's import blocks that name a routed child, by module."""
    _route, children = _hybrid_route_and_children(barrel["suffix"])
    routed = {child.legacy_module for child in children}
    return tuple(module for module, _names in barrel["blocks"] if module in routed)


def _runtime_blocks(barrel: dict[str, Any]) -> tuple[str, ...]:
    """The barrel's import blocks that name a declared runtime-only module."""
    portable = set(_portable_blocks(barrel))
    return tuple(module for module, _names in barrel["blocks"] if module not in portable)


def test_all_six_hybrid_barrels_are_converted_hybrid_facades() -> None:
    """The registry's own record of this batch, pinned exactly: the same six
    ``hybrid_barrel``/``barrel`` pairs the previous checkpoint held at
    ``pending_hybrid`` are now ``hybrid_facade`` and converted, ``pending_hybrid``
    is empty, and no count moved -- this batch changes six state strings and
    nothing else."""
    manifest = load_manifest()
    assert {route.suffix for route in manifest.by_state(MigrationState.HYBRID_FACADE)} == (
        EXPECTED_HYBRID_SUFFIXES
    )
    assert {barrel["suffix"] for barrel in _HYBRID_BARREL_SOURCES} == (
        EXPECTED_HYBRID_SUFFIXES
    )
    assert manifest.by_state(MigrationState.PENDING_HYBRID) == ()
    for route in manifest.by_state(MigrationState.HYBRID_FACADE):
        assert route.pair_kind is PairKind.HYBRID_BARREL
        assert route.shape is Shape.BARREL
        assert route.is_converted
    assert manifest.observed_counts().as_dict() == {
        "routes": 47,
        "direct": 40,
        "hybrid_barrel": 6,
        "root": 1,
        "leaf": 30,
        "barrel": 16,
        "runtime_only_modules": 21,
    }


def test_hybrid_facade_partition_totals_are_exact() -> None:
    """93 legacy barrel exports across the six: 62 portable and 31 runtime. The
    per-barrel tuples are restated independently above, so this is the one place
    the aggregate is asserted -- a name that moved from one half to the other
    inside a barrel would keep every per-barrel count and fail here."""
    total = sum(len(barrel["all"]) for barrel in _HYBRID_BARREL_SOURCES)
    portable = sum(len(barrel["portable"]) for barrel in _HYBRID_BARREL_SOURCES)
    runtime = sum(len(barrel["runtime"]) for barrel in _HYBRID_BARREL_SOURCES)
    assert (total, portable, runtime) == _HYBRID_TOTALS
    for barrel in _HYBRID_BARREL_SOURCES:
        assert set(barrel["portable"]).isdisjoint(barrel["runtime"])
        assert set(barrel["portable"]) | set(barrel["runtime"]) == set(barrel["all"])
        imported = [name for _module, names in barrel["blocks"] for name in names]
        assert sorted(imported) == sorted(barrel["all"])
        assert len(imported) == len(set(imported))


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_barrel_source_matches_its_frozen_import_table(
    barrel: dict[str, Any],
) -> None:
    """Each barrel's real, unchanged file must be exactly the table above:
    a docstring, the import blocks in that order with those names in that order,
    then the ordered ``__all__`` literal, and nothing else. This is the gate that
    makes the state honest -- reordering a block, renaming an import, or reordering
    ``__all__`` fails here even though every identity check would still pass."""
    route, _children = _hybrid_route_and_children(barrel["suffix"])
    path = legacy_source_path(REPO_ROOT, route)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert ast.get_docstring(tree) is not None
    body = tree.body[1:]
    assert len(body) == len(barrel["blocks"]) + 1, [ast.dump(node) for node in body]

    for node, (module, names) in zip(body, barrel["blocks"], strict=False):
        assert isinstance(node, ast.ImportFrom), ast.dump(node)
        assert node.level == 0
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        assert all(alias.asname is None for alias in node.names)

    assignment = body[-1]
    assert isinstance(assignment, ast.Assign)
    (target,) = assignment.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(assignment.value, ast.List)
    assert (
        tuple(
            element.value
            for element in assignment.value.elts
            if isinstance(element, ast.Constant)
        )
        == barrel["all"]
    )


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_barrel_real_source_has_no_defects(barrel: dict[str, Any]) -> None:
    """The unchanged file itself, not a fixture: zero defects. Read from disk so a
    fixture that had drifted from the real barrel could not make this pass."""
    route, children = _hybrid_route_and_children(barrel["suffix"])
    manifest = load_manifest()
    path = legacy_source_path(REPO_ROOT, route)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert (
        hybrid_facade_defects(tree, route, children, manifest.runtime_only_modules) == []
    )


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_fixture_shape_is_the_defect_free_adversary_base(
    barrel: dict[str, Any],
) -> None:
    """The synthetic fixture built from the table must also be defect-free, so every
    rejection below is proven to fail on the mutation it injects rather than on the
    fixture's shape. A missing docstring is accepted too: the policy is about what
    the module *executes*, and a barrel without one is still an exact hybrid."""
    assert _hybrid_defects(barrel) == []
    assert _hybrid_defects(barrel, docstring="") == []


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_barrel_source_is_not_a_transitive_or_direct_facade(
    barrel: dict[str, Any],
) -> None:
    """The state is not interchangeable with the ones next to it. Mislabelled
    ``transitive_facade``, the barrel's runtime blocks are unapproved modules;
    mislabelled ``direct_facade``, it is not a single canonical re-export at all.
    Both are rejected on source shape, independently of the combination table."""
    route, children = _hybrid_route_and_children(barrel["suffix"])
    tree = ast.parse(
        legacy_source_path(REPO_ROOT, route).read_text(encoding="utf-8")
    )
    converted = [child for child in children if child.is_converted]
    transitive = transitive_facade_defects(tree, route, converted)
    assert any("unapproved module" in defect for defect in transitive), transitive
    for module in _runtime_blocks(barrel):
        assert any(
            "unapproved module" in defect and module in defect
            for defect in transitive
        ), (module, transitive)
    assert direct_facade_defects(tree, route.canonical_module) != []


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_barrel_cannot_be_walked_back_to_pending_hybrid(
    barrel: dict[str, Any],
) -> None:
    """``pending_hybrid`` is still a valid *combination* for these pairs, so the
    load-time table cannot be what stops a walk-back. The source gate is: a pending
    hybrid whose routed children are all converted and whose source already has no
    hybrid defects is understating itself, and must say so."""
    document = _document()
    _route(document, barrel["suffix"])["migration_state"] = "pending_hybrid"
    load_manifest(document)
    with pytest.raises(FacadeManifestError, match="move the state forward") as error:
        validate_route_sources(manifest=document)
    joined = "; ".join(error.value.errors)
    assert f"route 'omnivia_memory.{barrel['suffix']}'" in joined
    assert "declared 'pending_hybrid'" in joined


def _errors_by_route(errors: Sequence[str]) -> dict[str, list[str]]:
    """Group diagnostics by the exact ``route '<module>'`` prefix they carry.

    Substring matching is not safe across this topology: ``ingestion`` is a
    prefix of ``ingestion.watcher``, so ``"omnivia_memory.ingestion" in joined``
    is true whenever *either* route is reported. Splitting on the prefix the
    validator itself emits keeps the two attributable separately.
    """
    grouped: dict[str, list[str]] = {}
    for message in errors:
        where, separator, detail = message.partition(": ")
        assert separator, message
        grouped.setdefault(where, []).append(detail)
    return grouped


def test_pending_hybrid_is_left_alone_while_a_routed_child_is_unconverted() -> None:
    """The other half of the ``pending_hybrid`` gate: it must not fire on a route
    that really is still pending.

    ``ingestion`` is the only barrel in the registry with a routed *barrel* child,
    so it is the only place this can be exercised through the public API. Walk both
    ``ingestion`` and its nested child ``ingestion.watcher`` back to
    ``pending_hybrid`` -- a valid combination for either -- and the parent's
    prerequisite is no longer met: one of its two routed children is unconverted,
    so nothing about its source is being understated yet and no diagnostic may name
    it. The deliberately pending watcher does report itself, because *its* own
    child is converted; the assertions below are keyed on the exact route prefix so
    that the parent's silence is what is actually proven.

    This is the regression for the ``all(child.is_converted ...)`` half of that
    gate specifically. The parent barrel does not re-export ``ingestion.watcher``
    and is not required to (a routed barrel child is a separately routed subtree),
    so its unchanged source has zero hybrid defects here -- asserted below. Drop
    the conversion prerequisite and the parent would immediately be told to move
    its state forward on the strength of that empty defect list, which is exactly
    what this test forbids.
    """
    parent = "route 'omnivia_memory.ingestion'"
    child = "route 'omnivia_memory.ingestion.watcher'"

    document = _document()
    _route(document, "ingestion")["migration_state"] = "pending_hybrid"
    _route(document, "ingestion.watcher")["migration_state"] = "pending_hybrid"
    manifest = load_manifest(document)

    parent_route = manifest.route_for_legacy("omnivia_memory.ingestion")
    routed_children = [
        item
        for item in manifest.routes
        if item.suffix and item.suffix.rpartition(".")[0] == "ingestion"
    ]
    assert parent_route.migration_state is MigrationState.PENDING_HYBRID
    assert sorted(
        (item.legacy_module, item.is_converted) for item in routed_children
    ) == [
        ("omnivia_memory.ingestion.models", True),
        ("omnivia_memory.ingestion.watcher", False),
    ]
    tree = ast.parse(
        legacy_source_path(REPO_ROOT, parent_route).read_text(encoding="utf-8")
    )
    assert (
        hybrid_facade_defects(
            tree, parent_route, routed_children, manifest.runtime_only_modules
        )
        == []
    )

    for validate in (validate_route_sources, validate_checkout):
        with pytest.raises(FacadeManifestError) as error:
            validate(manifest=document)
        grouped = _errors_by_route(error.value.errors)
        # Nothing at all is said about the parent -- in particular not the
        # "move the state forward" verdict its converted sibling topology would
        # otherwise have earned it.
        assert parent not in grouped, grouped
        assert [
            detail for detail in grouped[child] if "move the state forward" in detail
        ], grouped
        assert grouped[child][0].startswith("declared 'pending_hybrid'")


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_facade_accepts_the_committed_registry(barrel: dict[str, Any]) -> None:
    """The whole registry, checked end to end, accepts all six -- both through the
    source-only gate and through the full checkout gate."""
    validate_route_sources()
    validate_checkout()
    assert (
        load_manifest()
        .route_for_legacy(f"omnivia_memory.{barrel['suffix']}")
        .migration_state
        is MigrationState.HYBRID_FACADE
    )


@pytest.mark.parametrize(
    ("suffix", "kind", "shape"),
    [
        ("graph.models", "direct", "leaf"),
        ("knowledge", "direct", "barrel"),
        ("", "root", "root"),
        ("graph", "hybrid_barrel", "leaf"),
        ("graph", "direct", "barrel"),
    ],
    ids=[
        "direct-leaf",
        "direct-barrel",
        "root",
        "hybrid-kind-wrong-shape",
        "barrel-wrong-kind",
    ],
)
def test_hybrid_facade_is_only_valid_for_a_hybrid_barrel(
    suffix: str, kind: str, shape: str
) -> None:
    """``hybrid_facade`` describes a barrel that publishes a declared runtime-only
    half, so only a ``hybrid_barrel``/``barrel`` pair may be in it. A direct leaf, a
    direct barrel, the package root, and either half of the pair changed on its own
    are all rejected at load time by the combination rule."""
    document = _document()
    route = _route(document, suffix)
    route["pair_kind"] = kind
    route["shape"] = shape
    route["migration_state"] = "hybrid_facade"
    with pytest.raises(
        FacadeManifestError, match="valid combination|exactly one root|shape 'root'"
    ):
        load_manifest(document)


def test_hybrid_facade_is_a_converted_state_whose_source_is_checked(
    tmp_path: Path,
) -> None:
    """The whole point of the state: ``is_converted`` is true for it exactly as it
    is for the three facade states before it, and -- unlike a pending state -- its
    source really is read. Proven by editing the barrel in a copied checkout: a
    pending state would not have noticed."""
    manifest = load_manifest()
    converted = {route.suffix for route in manifest.routes if route.is_converted}
    assert EXPECTED_HYBRID_SUFFIXES <= converted
    pending = {
        MigrationState.PENDING_DIRECT_BARREL,
        MigrationState.PENDING_HYBRID,
        MigrationState.PENDING_ROOT,
    }
    assert EXPECTED_HYBRID_SUFFIXES.isdisjoint(
        {route.suffix for route in manifest.routes if route.migration_state in pending}
    )

    _copy_package_trees(tmp_path)
    barrel = (
        tmp_path
        / "services"
        / "omnivia-memory"
        / "src"
        / "omnivia_memory"
        / "workspace"
        / "__init__.py"
    )
    barrel.write_text(
        barrel.read_text(encoding="utf-8") + "\n\ndef __getattr__(name):\n"
        "    raise AttributeError(name)\n",
        encoding="utf-8",
    )
    with pytest.raises(FacadeManifestError, match="dynamic module hook") as error:
        validate_checkout(tmp_path)
    assert "declared 'hybrid_facade'" in "; ".join(error.value.errors)


def test_hybrid_facade_rejects_a_barrel_with_one_unconverted_child() -> None:
    """A hybrid facade's portable half is only canonical because its children are.
    One unconverted routed child and the claim is false -- both at the validator,
    which refuses the import, and at the registry gate, which refuses the state."""
    barrel = _HYBRID_BARREL_SOURCES[0]
    route, children = _hybrid_route_and_children("graph")
    downgraded = [
        replace(child, migration_state=MigrationState.SOURCE_PARITY)
        if child.suffix == "graph.models"
        else child
        for child in children
    ]
    defects = _hybrid_defects(barrel, children=downgraded)
    assert any(
        "routed child 'omnivia_memory.graph.models'" in defect
        and "not converted" in defect
        for defect in defects
    ), defects
    assert route.migration_state is MigrationState.HYBRID_FACADE

    document = _document()
    _route(document, "graph.models")["migration_state"] = "source_parity"
    with pytest.raises(FacadeManifestError) as error:
        validate_route_sources(manifest=document)
    joined = "; ".join(error.value.errors)
    assert "declared 'hybrid_facade' but these routed children are not converted" in (
        joined
    )
    assert "omnivia_memory.graph.models" in joined


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_facade_requires_every_routed_leaf_child(barrel: dict[str, Any]) -> None:
    """Dropping a portable block -- and the names it bound, so the ``__all__``
    still matches -- must fail. Without this a barrel could quietly stop
    re-exporting a converted leaf and keep a perfectly consistent shape.

    ``ingestion`` is the documented exception in the other direction: its routed
    child ``ingestion.watcher`` is a separately routed barrel with its own contract
    and has never been re-exported by its parent, so barrel children are not
    required to be reached."""
    for dropped in _portable_blocks(barrel):
        blocks = tuple(block for block in barrel["blocks"] if block[0] != dropped)
        kept = {name for _module, names in blocks for name in names}
        exported = tuple(name for name in barrel["all"] if name in kept)
        defects = _hybrid_defects(barrel, blocks=blocks, exported=exported)
        assert any(
            "does not import routed children" in defect and dropped in defect
            for defect in defects
        ), (dropped, defects)


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_facade_requires_both_halves(barrel: dict[str, Any]) -> None:
    """Neither half may vanish. With no runtime block left the barrel is a
    transitive facade and must be declared one; with no converted child left there
    is nothing about it that has converted at all."""
    runtime_modules = set(_runtime_blocks(barrel))
    portable_modules = set(_portable_blocks(barrel))

    blocks = tuple(block for block in barrel["blocks"] if block[0] not in runtime_modules)
    kept = {name for _module, names in blocks for name in names}
    defects = _hybrid_defects(
        barrel,
        blocks=blocks,
        exported=tuple(name for name in barrel["all"] if name in kept),
    )
    assert any(
        "imports no declared runtime-only descendant" in defect for defect in defects
    ), defects

    blocks = tuple(block for block in barrel["blocks"] if block[0] not in portable_modules)
    kept = {name for _module, names in blocks for name in names}
    defects = _hybrid_defects(
        barrel,
        blocks=blocks,
        exported=tuple(name for name in barrel["all"] if name in kept),
    )
    assert any(
        "imports no converted routed child" in defect for defect in defects
    ), defects


@pytest.mark.parametrize("barrel", _HYBRID_BARREL_SOURCES, ids=_hybrid_id)
def test_hybrid_facade_rejects_canonicalising_either_half(
    barrel: dict[str, Any],
) -> None:
    """Rerouting *any* block at ``omnivia_core`` is rejected, portable or runtime.
    Canonicalising the portable half would make the barrel a direct facade rather
    than a transitive one; canonicalising the runtime half would claim Core owns a
    module it deliberately does not, and the import would not even resolve."""
    for module, _names in barrel["blocks"]:
        canonical = module.replace("omnivia_memory", "omnivia_core", 1)
        blocks = tuple(
            (canonical if block[0] == module else block[0], block[1])
            for block in barrel["blocks"]
        )
        defects = _hybrid_defects(barrel, blocks=blocks)
        assert any(
            "imports the canonical module" in defect and canonical in defect
            for defect in defects
        ), (module, defects)


@pytest.mark.parametrize(
    ("extra_source", "pattern"),
    [
        # A legacy descendant of the barrel that is not a route and is not in
        # ``runtime_only_modules``: the exact module a future runtime split would
        # add, and it may not enter the barrel without being declared first.
        (
            "from omnivia_memory.graph.undeclared import Helper\n",
            "neither a routed child nor declared runtime-only",
        ),
        # An unrelated sibling domain, converted or not.
        (
            "from omnivia_memory.knowledge.models import KnowledgeItem\n",
            "outside the 'omnivia_memory.graph' subtree",
        ),
        (
            "from omnivia_core.knowledge.models import KnowledgeItem\n",
            "imports the canonical module",
        ),
        # An unrelated runtime-only package: declared runtime-only, but not a
        # descendant of *this* barrel.
        (
            "from omnivia_memory.persistence import Database\n",
            "outside the 'omnivia_memory.graph' subtree",
        ),
        # The package root, and the barrel itself.
        ("from omnivia_memory import Entity\n", "outside the"),
        ("from omnivia_memory.graph import Entity\n", "imports itself"),
        # Relative forms: the policy accepts absolute imports only, so a relative
        # path is rejected before it is even resolved.
        ("from .models import Entity\n", "relative import"),
        ("from .search_service import GraphSearchError\n", "relative import"),
        ("from ..persistence import Database\n", "relative import"),
        # Aliases and stars, on an otherwise approved module.
        (
            "from omnivia_memory.graph.models import Entity as GraphEntity\n",
            "aliases 'Entity' as 'GraphEntity'",
        ),
        ("from omnivia_memory.graph.models import *\n", "star import"),
        # A plain import, of an approved module and of anything else.
        ("import omnivia_memory.graph.models\n", "plain import"),
        ("import sys\n", "plain import"),
        # A second block naming a module the barrel already imports.
        (
            "from omnivia_memory.graph.models import Entity\n",
            "in more than one block",
        ),
        # Statements of its own, of every kind the policy forbids.
        ("Entity = object()\n", "assignments"),
        ("VERSION: str = '1'\n", "statements of its own (AnnAssign)"),
        ("def helper():\n    return None\n", "statements of its own (FunctionDef)"),
        (
            "async def helper():\n    return None\n",
            "statements of its own (AsyncFunctionDef)",
        ),
        ("class Entity:\n    pass\n", "statements of its own (ClassDef)"),
        (
            "if True:\n    Entity = object()\n",
            "statements of its own (If)",
        ),
        (
            "for name in ():\n    pass\n",
            "statements of its own (For)",
        ),
        (
            "try:\n    pass\nexcept Exception:\n    pass\n",
            "statements of its own (Try)",
        ),
        ('"""A second string."""\n', "statements of its own (Expr)"),
        # The two dynamic hooks, named specifically: every export must be a real,
        # statically visible binding decided at import time.
        (
            "def __getattr__(name):\n    raise AttributeError(name)\n",
            "dynamic module hook '__getattr__'",
        ),
        ("def __dir__():\n    return []\n", "dynamic module hook '__dir__'"),
    ],
)
def test_hybrid_facade_rejects_a_reroute_or_local_statement(
    extra_source: str, pattern: str
) -> None:
    """Every way a hybrid barrel could stop being exactly its two declared halves,
    injected into the defect-free ``graph`` fixture so the pattern asserted is the
    one the mutation causes."""
    defects = _hybrid_defects(_HYBRID_BARREL_SOURCES[0], extra_source=extra_source)
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("exported", "pattern"),
    [
        # A name the barrel does not bind, and one it binds but hides.
        (
            (
                "ApprovalStatus",
                "Entity",
                "EntityType",
                "GraphSearchError",
                "GraphSearchQuery",
                "GraphSearchResult",
                "GraphSearchResultSet",
                "GraphSearchService",
                "Relationship",
                "RelationshipType",
                "EntityRepository",
            ),
            "does not exactly match the literal __all__",
        ),
        (
            (
                "ApprovalStatus",
                "Entity",
                "EntityType",
                "GraphSearchError",
                "GraphSearchQuery",
                "GraphSearchResult",
                "GraphSearchResultSet",
                "GraphSearchService",
                "Relationship",
            ),
            "does not exactly match the literal __all__",
        ),
        # A duplicated entry: the same surface, advertised twice.
        (
            (
                "ApprovalStatus",
                "ApprovalStatus",
                "Entity",
                "EntityType",
                "GraphSearchError",
                "GraphSearchQuery",
                "GraphSearchResult",
                "GraphSearchResultSet",
                "GraphSearchService",
                "Relationship",
                "RelationshipType",
            ),
            "__all__ contains duplicate names",
        ),
    ],
    ids=["extra-name", "missing-name", "duplicate-name"],
)
def test_hybrid_facade_requires_all_to_equal_the_bound_names(
    exported: tuple[str, ...], pattern: str
) -> None:
    defects = _hybrid_defects(_HYBRID_BARREL_SOURCES[0], exported=exported)
    assert any(pattern in defect for defect in defects), defects


def test_hybrid_facade_rejects_a_duplicated_binding() -> None:
    """The same name imported from two different approved modules. Both modules are
    allowed and ``__all__`` still lists the name once, so only the duplicate-binding
    rule catches it -- and it must, because the second import silently wins."""
    barrel = _HYBRID_BARREL_SOURCES[0]
    blocks = (
        *barrel["blocks"],
        ("omnivia_memory.graph.repository", ("Entity",)),
    )
    defects = _hybrid_defects(barrel, blocks=blocks)
    assert any(
        "binds the same imported name twice: ['Entity']" in defect for defect in defects
    ), defects


@pytest.mark.parametrize(
    ("all_source", "pattern"),
    [
        ("__all__ = list(__all__)\n", "not a literal list/tuple of strings"),
        ("__all__ = ['Entity', 1]\n", "not a literal list/tuple of strings"),
        ("__all__ = __EXPORTS__ = ['Entity']\n", "assigns a name other than"),
        ("EXPORTS = ['Entity']\n", "assigns a name other than"),
    ],
    ids=["nonliteral", "non-string-element", "two-targets", "wrong-name"],
)
def test_hybrid_facade_requires_one_literal_all(all_source: str, pattern: str) -> None:
    """``__all__`` has to be readable statically: a computed value, a non-string
    element, a chained target, or a differently named list all fail."""
    barrel = _HYBRID_BARREL_SOURCES[0]
    route, children = _hybrid_route_and_children("graph")
    source = ""
    for module, names in barrel["blocks"]:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from {module} import (\n{body})\n"
    defects = hybrid_facade_defects(
        ast.parse(source + all_source),
        route,
        children,
        load_manifest().runtime_only_modules,
    )
    assert any(pattern in defect for defect in defects), defects


def test_hybrid_facade_requires_exactly_one_all_assignment() -> None:
    barrel = _HYBRID_BARREL_SOURCES[0]
    defects = _hybrid_defects(barrel, extra_source='__all__ = ["Entity"]\n')
    assert any("has 2 assignments" in defect for defect in defects), defects

    route, children = _hybrid_route_and_children("graph")
    source = ""
    for module, names in barrel["blocks"]:
        body = "".join(f"    {name},\n" for name in names)
        source += f"from {module} import (\n{body})\n"
    defects = hybrid_facade_defects(
        ast.parse(source),
        route,
        children,
        load_manifest().runtime_only_modules,
    )
    assert any("has 0 assignments" in defect for defect in defects), defects


def test_hybrid_facade_rejects_an_undeclared_runtime_module_at_the_registry() -> None:
    """The runtime half is only legitimate because ``runtime_only_modules`` names
    it. Drop a barrel's runtime module from that list and the same unchanged source
    stops being a hybrid facade -- which is what makes the declaration load-bearing
    rather than descriptive.

    Removing it from the list alone would also fail the checkout gate (the module
    is still on disk and legacy-only), so this exercises the source policy directly
    with a shortened list."""
    manifest = load_manifest()
    route, children = _hybrid_route_and_children("workspace")
    tree = ast.parse(
        legacy_source_path(REPO_ROOT, route).read_text(encoding="utf-8")
    )
    shortened = tuple(
        module
        for module in manifest.runtime_only_modules
        if module != "omnivia_memory.workspace.service"
    )
    defects = hybrid_facade_defects(tree, route, children, shortened)
    assert any(
        "omnivia_memory.workspace.service" in defect
        and "neither a routed child nor declared runtime-only" in defect
        for defect in defects
    ), defects


def test_hybrid_source_inspection_does_not_import_either_package() -> None:
    """The same standard-library-only guarantee the loader has: reading the six
    barrels' sources and judging them must not pull either package into the
    process, so a stale installed copy cannot satisfy or break this gate."""
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(REPO_ROOT)!r})",
            "from baseline.facade_manifest import validate_route_sources",
            "validate_route_sources()",
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


# ---------------------------------------------------------------------------
# The ``root_facade`` package root.
#
# The root is the only route whose advertised surface spans every domain at
# once, and several of the names it publishes are published under the same
# spelling by more than one domain. So "it imports the right names" is not the
# contract: *which owner* each name comes from is, which is why the state has a
# frozen owner-by-owner import table
# (``ROOT_FACADE_CANONICAL_IMPORTS`` / ``ROOT_FACADE_RUNTIME_IMPORTS``) and a
# frozen ordered ``__all__`` (``ROOT_FACADE_ALL``) rather than a shape rule.
#
# It is deliberately not a ``direct_facade``: the canonical root advertises only
# ``__version__``, so there is nothing a single re-export could have re-exported.
# ---------------------------------------------------------------------------

ROOT_SUFFIX = ""
LEGACY_ROOT_MODULE = "omnivia_memory"
CANONICAL_ROOT_MODULE = "omnivia_core"

#: The two legacy runtime owners the converted root may still reach, and the one
#: binding each supplies. Restated here rather than read from the loader, so a
#: widened table in ``baseline.facade_manifest`` fails this test instead of
#: agreeing with it.
EXPECTED_ROOT_RUNTIME_IMPORTS = {
    "omnivia_memory.memory.service": ("MemoryService",),
    "omnivia_memory.persistence": ("Database",),
}

#: Every canonical owner the converted root may import from, with the number of
#: names it must take from each. Counts rather than full name lists: the exact
#: lists are the loader's frozen table, and this is the independent check that the
#: table still describes the eleven-owner, 185-name shape the batch accepted --
#: 182 advertised portable names, ``__version__``, and the two hidden canonical
#: inputs.
EXPECTED_ROOT_CANONICAL_OWNER_SIZES = {
    "omnivia_core": 1,
    "omnivia_core._shared.validation": 1,
    "omnivia_core.app_manifest": 5,
    "omnivia_core.component_contract": 25,
    "omnivia_core.control_plane": 73,
    "omnivia_core.knowledge": 53,
    "omnivia_core.memory.models": 2,
    "omnivia_core.memory_graph": 5,
    "omnivia_core.module_manifest": 8,
    "omnivia_core.provenance": 2,
    "omnivia_core.run_ledger": 10,
}


def _root_route() -> FacadeRoute:
    return load_manifest().root


def _root_source() -> str:
    return legacy_source_path(REPO_ROOT, _root_route()).read_text(encoding="utf-8")


def _canonical_root_tree() -> ast.Module:
    return ast.parse(
        canonical_root_source_path(REPO_ROOT).read_text(encoding="utf-8")
    )


def _root_defects(source: str, **overrides: Any) -> list[str]:
    """``root_facade_defects`` for ``source`` against the committed registry."""
    manifest = load_manifest()
    kwargs: dict[str, Any] = {
        "route": manifest.root,
        "routes": manifest.routes,
        "runtime_only_modules": manifest.runtime_only_modules,
        "canonical_root": _canonical_root_tree(),
    }
    kwargs.update(overrides)
    return root_facade_defects(ast.parse(source), **kwargs)


def test_root_facade_is_the_committed_terminal_state() -> None:
    """The registry's own record: the root pair is ``root_facade``, it counts as
    converted, and it is the only route that can be in that state."""
    manifest = load_manifest()
    root = manifest.root
    assert root.legacy_module == LEGACY_ROOT_MODULE
    assert root.canonical_module == CANONICAL_ROOT_MODULE
    assert root.suffix == ROOT_SUFFIX
    assert root.pair_kind is PairKind.ROOT
    assert root.shape is Shape.ROOT
    assert root.migration_state is MigrationState.ROOT_FACADE
    assert root.is_converted
    assert manifest.by_state(MigrationState.ROOT_FACADE) == (root,)
    assert manifest.by_state(MigrationState.PENDING_ROOT) == ()


def test_root_facade_is_a_converted_state_and_not_a_pending_one() -> None:
    """``root_facade`` joins the four converted states, and stays out of the three
    pending ones. Pinned from both sides so a future edit cannot make it a state
    that counts as done while its source goes unchecked."""
    converted = replace(
        _root_route(), migration_state=MigrationState.ROOT_FACADE
    )
    assert converted.is_converted
    for state in (
        MigrationState.SOURCE_PARITY,
        MigrationState.CANONICAL_SUBSET,
        MigrationState.PENDING_DIRECT_BARREL,
        MigrationState.PENDING_HYBRID,
        MigrationState.PENDING_ROOT,
    ):
        assert not replace(_root_route(), migration_state=state).is_converted
    for state in (
        MigrationState.DIRECT_FACADE,
        MigrationState.SPLIT_FACADE,
        MigrationState.TRANSITIVE_FACADE,
        MigrationState.HYBRID_FACADE,
        MigrationState.ROOT_FACADE,
    ):
        assert replace(_root_route(), migration_state=state).is_converted


def test_schema_and_loader_both_accept_root_facade_only_on_the_root() -> None:
    """The schema knows the value; the loader's combination table is what confines
    it to the ``root``/``root`` pair. Both halves are checked, because the schema
    alone would let any route claim it."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "root_facade" in (
        schema["$defs"]["route"]["properties"]["migration_state"]["enum"]
    )
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(_document())

    for suffix in ("workspace.models", "graph", "knowledge", "memory"):
        document = _document()
        _route(document, suffix)["migration_state"] = "root_facade"
        # The schema accepts the value anywhere -- that is the point of splitting
        # the two gates -- and the loader is what rejects the combination.
        assert not list(validator.iter_errors(document))
        with pytest.raises(FacadeManifestError, match="valid combination"):
            load_manifest(document)


@pytest.mark.parametrize(
    "state",
    [
        "direct_facade",
        "split_facade",
        "transitive_facade",
        "hybrid_facade",
        "source_parity",
        "canonical_subset",
        "pending_direct_barrel",
        "pending_hybrid",
    ],
)
def test_root_admits_no_state_but_its_two_root_ones(state: str) -> None:
    """``root_facade`` and ``pending_root`` are the only two states the root pair
    may ever be in. In particular ``direct_facade`` is rejected structurally, not
    merely discouraged: the root's contract is 13 owners, not one re-export."""
    document = _document()
    _route(document, ROOT_SUFFIX)["migration_state"] = state
    with pytest.raises(FacadeManifestError, match="valid combination"):
        load_manifest(document)


def test_committed_root_source_has_no_defects() -> None:
    """The defect-free base every adversary below is a mutation of."""
    assert _root_defects(_root_source()) == []
    assert canonical_root_defects(_canonical_root_tree()) == []


def test_root_facade_frozen_tables_describe_the_accepted_shape() -> None:
    """The frozen tables, pinned independently of the file they constrain.

    They are hand-maintained data, so an edit that widened them would otherwise
    make every source check below agree with the widening rather than catch it.
    """
    assert dict(ROOT_FACADE_RUNTIME_IMPORTS) == EXPECTED_ROOT_RUNTIME_IMPORTS
    assert {
        module: len(names) for module, names in ROOT_FACADE_CANONICAL_IMPORTS.items()
    } == EXPECTED_ROOT_CANONICAL_OWNER_SIZES

    canonical_names = [
        name for names in ROOT_FACADE_CANONICAL_IMPORTS.values() for name in names
    ]
    runtime_names = [
        name for names in ROOT_FACADE_RUNTIME_IMPORTS.values() for name in names
    ]
    assert len(canonical_names) == len(set(canonical_names)) == 185
    assert sorted(runtime_names) == ["Database", "MemoryService"]
    assert set(canonical_names) | set(runtime_names) == set(ROOT_FACADE_ALL) | set(
        ROOT_FACADE_HIDDEN_BINDINGS
    )

    assert len(ROOT_FACADE_ALL) == len(set(ROOT_FACADE_ALL)) == 183
    assert ROOT_FACADE_ALL.count("__version__") == 1
    assert ROOT_FACADE_HIDDEN_BINDINGS == (
        "Database",
        "MemoryCreate",
        "MemoryService",
        "MemoryUpdate",
    )
    assert set(ROOT_FACADE_HIDDEN_BINDINGS).isdisjoint(ROOT_FACADE_ALL)
    assert ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS == ("MemoryCreate", "MemoryUpdate")
    assert ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS == ("Database", "MemoryService")
    assert ROOT_FACADE_VERSION_MODULE == CANONICAL_ROOT_MODULE
    assert ROOT_FACADE_VERSION_BINDING == "__version__"
    assert CANONICAL_ROOT_ALL == ("__version__",)

    # The two runtime owners must be modules the registry itself declares
    # runtime-only, so the table cannot license a legacy import the registry has
    # not accounted for.
    manifest = load_manifest()
    for module in ROOT_FACADE_RUNTIME_IMPORTS:
        assert module in manifest.runtime_only_modules
        assert module not in manifest.legacy_modules


def test_state_only_edit_of_an_unchanged_root_is_rejected() -> None:
    """The whole point of the source gate: moving the registry forward without
    touching the file must fail. Checked against the *real* pre-conversion root,
    read out of git rather than approximated, so this is the exact walk the batch
    took."""
    historical = subprocess.run(
        ["git", "show", "f3774ae:services/omnivia-memory/src/omnivia_memory/__init__.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert historical.returncode == 0, historical.stderr
    defects = _root_defects(historical.stdout)
    assert defects, "the pre-conversion root must not satisfy root_facade_defects"
    joined = "\n".join(defects)
    # It relied on relative imports of its own subpackages, took its portable
    # names from the legacy tree, and assigned its own version string.
    assert "uses a relative import" in joined
    assert "imports the legacy module 'omnivia_memory.knowledge'" in joined
    assert "must import its version rather than restating it" in joined


def test_root_facade_source_gate_is_wired_into_validate_checkout(tmp_path: Path) -> None:
    """The gate runs from the ordinary checkout validation, not only when called
    directly. Proven by editing the root in a copied tree: a state whose source is
    not read would not have noticed."""
    _copy_package_trees(tmp_path)
    root = (
        tmp_path
        / "services"
        / "omnivia-memory"
        / "src"
        / "omnivia_memory"
        / "__init__.py"
    )
    root.write_text(
        root.read_text(encoding="utf-8").replace(
            "from omnivia_core.provenance import Source, SourceType",
            "from omnivia_core.ingestion import Source, SourceType",
        ),
        encoding="utf-8",
    )
    with pytest.raises(FacadeManifestError, match="declared 'root_facade'"):
        validate_checkout(tmp_path)


def test_root_facade_state_cannot_be_walked_back_to_pending_root(tmp_path: Path) -> None:
    """``pending_root`` is a state whose source is *inspected*, not skipped: the
    moment every other route is converted and the root source is already an exact
    root facade, declaring it pending understates the file and must fail.

    Without this, the terminal state could be quietly reverted in the registry
    while the file stayed converted, and the gate would stop checking the file at
    all."""
    _copy_package_trees(tmp_path)
    document = _document()
    _route(document, ROOT_SUFFIX)["migration_state"] = "pending_root"
    with pytest.raises(FacadeManifestError, match="move the state forward"):
        validate_checkout(tmp_path, document)

    # ...and while a routed child is unconverted, ``pending_root`` is legitimate
    # and nothing is reported: the walk-back check must not fire on a root that
    # really is not eligible yet.
    document = _document()
    _route(document, ROOT_SUFFIX)["migration_state"] = "pending_root"
    _route(document, "memory")["migration_state"] = "pending_hybrid"
    with pytest.raises(FacadeManifestError) as caught:
        validate_checkout(tmp_path, document)
    assert not any("pending_root" in error for error in caught.value.errors), (
        caught.value.errors
    )


def test_root_facade_requires_every_other_route_converted() -> None:
    """The root may not be declared terminal on top of an unconverted child, even
    though its own source would satisfy every other check."""
    manifest = load_manifest()
    routes = tuple(
        replace(route, migration_state=MigrationState.PENDING_HYBRID)
        if route.legacy_module == "omnivia_memory.memory"
        else route
        for route in manifest.routes
    )
    defects = _root_defects(_root_source(), routes=routes)
    assert defects == [
        ("is offered as terminal while these routes are still unconverted: "
        "['omnivia_memory.memory']")
    ]


def test_root_facade_rejects_an_undeclared_runtime_owner() -> None:
    """The two legacy modules the root reaches must be declared runtime-only by the
    registry itself. Dropping either declaration -- which would make the root's
    legacy import an unaccounted-for edge -- fails here."""
    manifest = load_manifest()
    trimmed = tuple(
        module
        for module in manifest.runtime_only_modules
        if module != "omnivia_memory.persistence"
    )
    defects = _root_defects(_root_source(), runtime_only_modules=trimmed)
    assert defects == [
        ("would take a legacy-owned binding from 'omnivia_memory.persistence', "
        "which the registry does not declare runtime-only")
    ]


def test_root_facade_requires_a_version_only_canonical_root() -> None:
    """``root_facade`` exists because the canonical root advertises only
    ``__version__``. If that stopped being true the state's whole justification
    would change, so it is a checked precondition rather than a comment."""
    widened = ast.parse(
        '"""Canonical root."""\n\n'
        "from __future__ import annotations\n\n"
        '__version__ = "0.1.0"\n\n'
        "CONTRACT = 1\n\n"
        '__all__ = ["CONTRACT", "__version__"]\n'
    )
    defects = _root_defects(_root_source(), canonical_root=widened)
    assert defects == [
        ("pairs with a canonical root that assigns ['CONTRACT'] besides its version "
        "and __all__"),
        ("pairs with a canonical root that declares __all__ "
        "['CONTRACT', '__version__'], expected ['__version__']"),
    ]


@pytest.mark.parametrize(
    ("source", "pattern"),
    [
        ('__version__ = "0.1.0"', "declares no literal __all__"),
        ('__all__ = ["__version__"]', "does not assign '__version__'"),
        (
            '__version__ = "0.1.0"\n__version__ = "0.2.0"\n__all__ = ["__version__"]',
            "assigns '__version__' more than once",
        ),
        ('__version__ = 1\n__all__ = ["__version__"]', "non-string"),
        ('__version__ = "0.1.0"\n__all__ = ["__version__", "X"]', "expected"),
        ('__version__ = "0.1.0"\nX = 1\n__all__ = ["__version__"]', "besides its"),
        (
            '__version__ = "0.1.0"\ndef __getattr__(name): ...\n__all__ = ["__version__"]',
            "dynamic module hook",
        ),
        (
            'import os\n__version__ = "0.1.0"\n__all__ = ["__version__"]',
            "statements besides its version",
        ),
        (
            ('from __future__ import annotations as ann\n__version__ = "0.1.0"\n'
            '__all__ = ["__version__"]'),
            "future feature",
        ),
        ('__version__ = __all__ = "x"', "not a single plain name"),
    ],
    ids=[
        "no-all",
        "no-version",
        "duplicate-version",
        "non-string-version",
        "extra-all-entry",
        "extra-assignment",
        "getattr",
        "plain-import",
        "aliased-future",
        "chained-target",
    ],
)
def test_canonical_root_policy_is_fail_closed(source: str, pattern: str) -> None:
    """The canonical-root half of the gate, attacked on its own. Every one of these
    is version-only at a glance and none of them is version-only in fact."""
    defects = canonical_root_defects(ast.parse(f'"""Canonical root."""\n\n{source}\n'))
    assert any(pattern in defect for defect in defects), defects


def test_canonical_root_policy_requires_a_docstring() -> None:
    """A stray module-scope string is a statement of the module's own, and a module
    with no docstring at all is not the accepted shape either."""
    assert any(
        "does not open with a module docstring" in defect
        for defect in canonical_root_defects(
            ast.parse('__version__ = "0.1.0"\n__all__ = ["__version__"]\n')
        )
    )
    assert any(
        "standalone string expression" in defect
        for defect in canonical_root_defects(
            ast.parse(
                '"""Canonical root."""\n\n__version__ = "0.1.0"\n"stray"\n'
                '__all__ = ["__version__"]\n'
            )
        )
    )


#: One mutation of the committed root source per attack: ``(id, old, new, defect
#: fragment, resolvable)``.
#:
#: Each is a *plausible* edit -- it still declares an ``__all__``, still imports
#: named objects from named modules, and changes only which object a consumer of
#: the legacy path ends up holding, or what the advertised contract says. What is
#: *not* claimed of all of them is that they would import cleanly at runtime, and
#: the fifth field says which is which rather than leaving it to a comment:
#:
#: * ``resolvable=True`` -- the rerouted module really exists and really publishes
#:   every name taken from it, so the edit would import successfully and only the
#:   source gate stands between it and a silently wrong facade. That is the
#:   stronger claim, so it is the one made for all but two of these, and
#:   ``test_resolvable_root_mutations_really_import`` proves it by import rather
#:   than asserting it in prose.
#: * ``resolvable=False`` -- the target canonical module deliberately does not
#:   exist, which is exactly the point of the attack: ``Database`` and
#:   ``MemoryService`` are runtime-owned, Core packages no counterpart for either,
#:   and an edit "moving" them into Core must be rejected by the source gate
#:   rather than left to fail at import time in a consumer's environment.
#:   ``test_unresolvable_root_mutations_target_absent_canonical_modules`` pins
#:   that these are exactly two and that their targets really are absent.
_ROOT_MUTATIONS: tuple[tuple[str, str, str, str, bool], ...] = (
    # -- the collisions, each rerouted to a domain that really does publish a
    #    same-named type ------------------------------------------------------
    (
        "collision-validation-result",
        "from omnivia_core._shared.validation import ValidationResult",
        "from omnivia_core.app_manifest import ValidationResult",
        "imports the wrong names from 'omnivia_core.app_manifest'",
        True,
    ),
    # Only ``Source`` is rerouted, and the import is split so ``SourceType`` keeps
    # its real owner: the ingestion barrel publishes a rival ``Source`` but no
    # ``SourceType`` at all, so rerouting the pair together would have been an
    # edit that could not import -- a weaker attack than the one meant here, which
    # is a consumer silently receiving ingestion's unrelated ``Source`` class.
    (
        "collision-source-rerouted-to-ingestion",
        "from omnivia_core.provenance import Source, SourceType",
        ("from omnivia_core.ingestion import Source\n"
        "from omnivia_core.provenance import SourceType"),
        ("imports the canonical module 'omnivia_core.ingestion', which is not an "
        "approved root owner"),
        True,
    ),
    # -- the two hidden runtime bindings, "moved" into Core -------------------
    #    Neither target exists, and that is the attack: Core owns no persistence
    #    module and no memory service, so these must fail the gate rather than
    #    ship a root that only breaks once it is imported.
    (
        "database-from-core",
        "from omnivia_memory.persistence import Database",
        "from omnivia_core.persistence import Database",
        ("imports the canonical module 'omnivia_core.persistence', which is not an "
        "approved root owner"),
        False,
    ),
    (
        "memory-service-from-core",
        "from omnivia_memory.memory.service import MemoryService",
        "from omnivia_core.memory.service import MemoryService",
        ("imports the canonical module 'omnivia_core.memory.service', which is not "
        "an approved root owner"),
        False,
    ),
    # -- the two hidden canonical inputs, rerouted back to the legacy tree ----
    (
        "memory-inputs-from-legacy",
        "from omnivia_core.memory.models import MemoryCreate, MemoryUpdate",
        "from omnivia_memory.memory.models import MemoryCreate, MemoryUpdate",
        "imports the legacy module 'omnivia_memory.memory.models'",
        True,
    ),
    # -- legacy imports the root's own table does not approve -----------------
    #    Neither target is unregistered: ``omnivia_memory.persistence.database``
    #    is a declared ``runtime_only`` module and ``omnivia_memory.memory`` is a
    #    registered ``hybrid_facade`` barrel route. What makes each a defect is
    #    that the root's import table approves exactly two legacy owners --
    #    ``omnivia_memory.persistence`` and ``omnivia_memory.memory.service`` --
    #    and neither of these is one of them.
    (
        "runtime-import-from-an-unapproved-runtime-only-submodule",
        "from omnivia_memory.persistence import Database",
        "from omnivia_memory.persistence.database import Database",
        "does not import the approved owner 'omnivia_memory.persistence'",
        True,
    ),
    # The pre-conversion shape: the frozen Phase 0 baseline recorded the root
    # taking ``MemoryService`` from the ``omnivia_memory.memory`` barrel, which
    # still publishes it. Reverting to that owner is the most plausible edit of
    # the set, and it is not an approved root owner.
    (
        "runtime-import-from-the-pre-conversion-barrel",
        "from omnivia_memory.memory.service import MemoryService",
        "from omnivia_memory.memory import MemoryService",
        "imports the legacy module 'omnivia_memory.memory'",
        True,
    ),
    # -- import syntax -------------------------------------------------------
    (
        "alias",
        "from omnivia_core.provenance import Source, SourceType",
        "from omnivia_core.provenance import Source as Source, SourceType",
        "aliases 'Source' as 'Source'",
        True,
    ),
    (
        "star",
        "from omnivia_core.provenance import Source, SourceType",
        "from omnivia_core.provenance import *",
        "uses a star import",
        True,
    ),
    (
        "relative",
        "from omnivia_memory.persistence import Database",
        "from .persistence import Database",
        "uses a relative import (level 1)",
        True,
    ),
    (
        "second-block-for-one-owner",
        "from omnivia_core.provenance import Source, SourceType",
        ("from omnivia_core.provenance import Source\n"
        "from omnivia_core.provenance import SourceType"),
        "imports from 'omnivia_core.provenance' in more than one block",
        True,
    ),
    (
        "future-import",
        '"""\n\nfrom omnivia_core import __version__',
        '"""\n\nfrom __future__ import annotations\n\nfrom omnivia_core import __version__',
        "extra ['annotations']",
        True,
    ),
    # -- statements of its own ------------------------------------------------
    (
        "plain-import",
        "from omnivia_core import __version__",
        "import sys\nfrom omnivia_core import __version__",
        "has a top-level plain import of ['sys']",
        True,
    ),
    (
        "sys-modules-routing",
        "from omnivia_core import __version__",
        "from sys import modules\nfrom omnivia_core import __version__",
        "imports 'sys', which is outside both packages",
        True,
    ),
    (
        "local-version",
        "\n__all__ = [",
        '\n__version__ = "0.1.0"\n\n__all__ = [',
        "must import its version rather than restating it",
        True,
    ),
    (
        "extra-assignment",
        "\n__all__ = [",
        "\nDEPRECATED = True\n\n__all__ = [",
        "assigns ['DEPRECATED'] of its own",
        True,
    ),
)

#: Mutations appended after the ``__all__`` assignment, so they also break the
#: mandatory statement order.
_ROOT_APPENDED_MUTATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "getattr",
        ("\n\ndef __getattr__(name: str) -> object:\n"
        "    raise AttributeError(name)\n"),
        "defines the dynamic module hook '__getattr__'",
    ),
    (
        "dir",
        "\n\ndef __dir__() -> list[str]:\n    return list(__all__)\n",
        "defines the dynamic module hook '__dir__'",
    ),
    (
        "class",
        "\n\nclass DeprecationShim:\n    pass\n",
        "has statements of its own (ClassDef)",
    ),
    (
        "function",
        "\n\ndef warn() -> None:\n    return None\n",
        "has statements of its own (FunctionDef)",
    ),
)


def _import_targets(source: str) -> list[tuple[str, tuple[str, ...]]]:
    """``(absolute module, imported names)`` for every import statement in ``source``.

    Relative imports are resolved against the compatibility root, which is the
    package this source *is*, so ``from .persistence import Database`` reports
    ``omnivia_memory.persistence``.
    """
    targets: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
            else:
                module = ".".join(
                    filter(None, (LEGACY_ROOT_MODULE, node.module))
                )
            targets.append((module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import):
            targets.extend((alias.name, ()) for alias in node.names)
    return targets


def _unresolvable_imports(source: str) -> list[str]:
    """The import targets in ``source`` that do not exist in this checkout.

    A name is resolvable if the module publishes it or is a package containing a
    submodule of that name -- the two ways ``from x import y`` can succeed.
    """
    problems: list[str] = []
    for module, names in _import_targets(source):
        try:
            imported = importlib.import_module(module)
        except ImportError:
            problems.append(module)
            continue
        for name in names:
            if name == "*" or hasattr(imported, name):
                continue
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                problems.append(f"{module}.{name}")
    return problems


@pytest.mark.parametrize(
    ("old", "new", "pattern"),
    [(old, new, pattern) for _id, old, new, pattern, _clean in _ROOT_MUTATIONS],
    ids=[mutation_id for mutation_id, *_rest in _ROOT_MUTATIONS],
)
def test_root_facade_source_policy_is_fail_closed(
    old: str, new: str, pattern: str
) -> None:
    """Each mutation still declares an ``__all__`` and still imports named objects
    from named modules; only the owner, the syntax, or the statement set changes.
    Whether the edited source would also *import* is a separate claim, made per
    mutation by the ``resolvable`` flag and proved by the two tests below."""
    source = _root_source()
    assert source.count(old) == 1, old
    defects = _root_defects(source.replace(old, new))
    assert any(pattern in defect for defect in defects), defects


@pytest.mark.parametrize(
    ("mutation_id", "old", "new"),
    [
        (mutation_id, old, new)
        for mutation_id, old, new, _pattern, resolvable in _ROOT_MUTATIONS
        if resolvable
    ],
    ids=[
        mutation_id
        for mutation_id, _old, _new, _pattern, resolvable in _ROOT_MUTATIONS
        if resolvable
    ],
)
def test_resolvable_root_mutations_really_import(
    mutation_id: str, old: str, new: str
) -> None:
    """The mutations that claim to be import-clean are proved to be, by import.

    This is what makes those attacks worth gating: the edited root would load
    without error and hand consumers the wrong object, so nothing but the source
    policy stands between it and a silently incorrect compatibility surface. Every
    import statement in the whole mutated source is resolved, not only the changed
    one, so a mutation cannot be import-clean at the edit and broken elsewhere.
    """
    mutated = _root_source().replace(old, new)
    assert _unresolvable_imports(mutated) == [], mutation_id


def test_unresolvable_root_mutations_target_absent_canonical_modules() -> None:
    """The two mutations that do *not* import, named exactly, with the reason.

    ``Database`` and ``MemoryService`` are runtime-owned; Core packages no
    ``omnivia_core.persistence`` and no ``omnivia_core.memory.service`` to move
    them to. An edit that pretends otherwise has to be rejected by the source gate
    -- there is no import-clean version of it to write -- so these two are declared
    ``resolvable=False`` rather than quietly counted with the rest, and the module
    they name is checked to really be absent.
    """
    unresolvable = {
        mutation_id: new
        for mutation_id, _old, new, _pattern, resolvable in _ROOT_MUTATIONS
        if not resolvable
    }
    assert sorted(unresolvable) == ["database-from-core", "memory-service-from-core"]
    for mutation_id, new in sorted(unresolvable.items()):
        (target,) = [module for module, _names in _import_targets(new)]
        assert target in ("omnivia_core.persistence", "omnivia_core.memory.service")
        with pytest.raises(ImportError):
            importlib.import_module(target)
        assert _unresolvable_imports(new) == [target], mutation_id


@pytest.mark.parametrize(
    ("appended", "pattern"),
    [(appended, pattern) for _id, appended, pattern in _ROOT_APPENDED_MUTATIONS],
    ids=[mutation_id for mutation_id, *_rest in _ROOT_APPENDED_MUTATIONS],
)
def test_root_facade_rejects_a_statement_after_its_all(
    appended: str, pattern: str
) -> None:
    """A root that resolves every name correctly and *then* adds a definition, a
    proxy hook or a stray expression is not a pure re-export: the extra statement
    runs at import time."""
    defects = _root_defects(_root_source() + appended)
    assert any(pattern in defect for defect in defects), defects
    assert any(
        "expected the literal __all__ assignment" in defect for defect in defects
    ), defects


def test_root_facade_rejects_a_stray_string_expression() -> None:
    """A trailing standalone string is a statement of the module's own, executed at
    import time, and it is reported *as* a stray string rather than being absorbed
    as a second docstring -- which a blanket docstring filter would have done."""
    defects = _root_defects(_root_source() + '\n\n"a stray statement"\n')
    assert len(defects) == 1
    assert "1 standalone string expression(s) besides the module docstring" in defects[0]


def test_root_facade_requires_a_leading_module_docstring() -> None:
    """Positional, so it fails closed: dropping the docstring entirely is caught,
    not silently accepted as "no strings at module scope"."""
    source = _root_source()
    docstring_end = source.index('"""', source.index('"""') + 3) + 4
    defects = _root_defects(source[docstring_end:].lstrip("\n"))
    assert any(
        "does not open with a module docstring" in defect for defect in defects
    ), defects


def test_root_facade_rejects_an_import_after_the_all_assignment() -> None:
    """Order is checked positionally: the same statements in the wrong sequence are
    still not the accepted shape."""
    source = _root_source()
    moved = source.replace(
        "from omnivia_memory.persistence import Database  # noqa: F401\n", ""
    ) + "\nfrom omnivia_memory.persistence import Database  # noqa: F401\n"
    defects = _root_defects(moved)
    assert any(
        "expected the literal __all__ assignment" in defect for defect in defects
    ), defects


@pytest.mark.parametrize(
    ("mutate", "pattern"),
    [
        # An advertised name dropped from __all__ but still imported.
        (
            lambda names: [name for name in names if name != "AgentGraphContext"],
            "missing ['AgentGraphContext']",
        ),
        # An extra name advertised that nothing supplies.
        (lambda names: [*names, "NotAContract"], "extra ['NotAContract']"),
        # A hidden binding advertised.
        (lambda names: [*names, "Database"], "non-advertised binding(s) ['Database']"),
        # A duplicate entry.
        (lambda names: [*names, names[0]], "duplicate names"),
        # The frozen set, reordered.
        (lambda names: [names[1], names[0], *names[2:]], "different order"),
    ],
    ids=["omitted", "added", "advertises-hidden", "duplicated", "reordered"],
)
def test_root_facade_all_must_be_the_frozen_ordered_literal(
    mutate: Callable[[list[str]], list[str]], pattern: str
) -> None:
    """``__all__`` is compared as an ordered literal, not as a set. Each of these
    keeps it a list of plausible strings and changes what the root advertises."""
    source = _root_source()
    original = "__all__ = [  # noqa: RUF022\n" + "".join(
        f'    "{name}",\n' for name in ROOT_FACADE_ALL
    )
    assert source.count(original) == 1
    replacement = "__all__ = [  # noqa: RUF022\n" + "".join(
        f'    "{name}",\n' for name in mutate(list(ROOT_FACADE_ALL))
    )
    defects = _root_defects(source.replace(original, replacement))
    assert any(pattern in defect for defect in defects), defects


def test_root_facade_rejects_an_undeclared_hidden_binding() -> None:
    """A fifth non-advertised binding is not a free extension point: the imported
    binding set has to be exactly ``__all__`` plus the four declared hidden names,
    so smuggling one in through an approved owner fails."""
    source = _root_source()
    defects = _root_defects(
        source.replace(
            "from omnivia_core.memory.models import MemoryCreate, MemoryUpdate",
            "from omnivia_core.memory.models import Memory, MemoryCreate, MemoryUpdate",
        )
    )
    assert any("extra ['Memory']" in defect for defect in defects), defects


def test_root_facade_rejects_dropping_an_approved_owner_entirely() -> None:
    """Every owner in the table must be reached. A dropped block would leave its
    names unbound at runtime, and the gate says which owner and which names."""
    source = _root_source()
    start = source.index("from omnivia_core.provenance import Source, SourceType")
    end = source.index("\n", start) + 1
    defects = _root_defects(source[:start] + source[end:])
    assert "does not import the approved owner 'omnivia_core.provenance'" in defects
    assert any(
        "does not import ['Source', 'SourceType'] from a canonical owner" in defect
        for defect in defects
    ), defects


def test_root_source_inspection_does_not_import_either_package() -> None:
    """The gate reads files. A root facade whose identities only hold because a
    stale copy of either package was already imported must not be able to satisfy
    it."""
    script = "\n".join(
        [
            "import ast, sys",
            f"sys.path.insert(0, {str(REPO_ROOT)!r})",
            "from baseline.facade_manifest import (",
            "    REPO_ROOT, canonical_root_source_path, legacy_source_path,",
            "    load_manifest, root_facade_defects,",
            ")",
            "manifest = load_manifest()",
            "root = manifest.root",
            "tree = ast.parse(legacy_source_path(REPO_ROOT, root).read_text())",
            "canonical = ast.parse(canonical_root_source_path(REPO_ROOT).read_text())",
            "assert root_facade_defects(",
            "    tree, root, manifest.routes, manifest.runtime_only_modules, canonical",
            ") == []",
            ("assert not any(n == 'omnivia_core' or n.startswith('omnivia_core.')"
            " for n in sys.modules)"),
            ("assert not any(n == 'omnivia_memory' or n.startswith('omnivia_memory.')"
            " for n in sys.modules)"),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
