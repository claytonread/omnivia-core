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
        "control_plane.imports",
        "control_plane.models",
        "control_plane.validation",
        "knowledge.models",
        "knowledge.normalize",
        "knowledge.validation",
        "lifecycle.models",
        "lifecycle.rules",
        "memory.models",
        "module_manifest.models",
        "module_manifest.validation",
        "provenance.models",
        "run_ledger.models",
        "run_ledger.validation",
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
        ("control_plane.imports", "source_parity", "move the state forward"),
        ("control_plane.models", "source_parity", "move the state forward"),
        ("control_plane.validation", "source_parity", "move the state forward"),
        ("knowledge.models", "source_parity", "move the state forward"),
        ("knowledge.normalize", "source_parity", "move the state forward"),
        ("knowledge.validation", "source_parity", "move the state forward"),
        ("module_manifest.models", "source_parity", "move the state forward"),
        ("module_manifest.validation", "source_parity", "move the state forward"),
        ("run_ledger.models", "source_parity", "move the state forward"),
        ("run_ledger.validation", "source_parity", "move the state forward"),
        # The mirror direction: a leaf that is still a duplicated source-parity
        # copy may not be declared as a converted facade either.
        ("workspace.models", "direct_facade", "declared 'direct_facade'"),
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
    # The per-state counts this batch moved: the three knowledge leaves into
    # ``direct_facade``, their barrel from ``pending_direct_barrel`` into
    # ``transitive_facade``. That empties ``pending_direct_barrel``: only the six
    # hybrid barrels and the package root are still pending.
    assert "source_parity: 8" in result.stdout
    assert "direct_facade: 21" in result.stdout
    assert "transitive_facade: 10" in result.stdout
    assert "pending_direct_barrel: 0" in result.stdout
    assert "pending_hybrid: 6" in result.stdout
    assert "pending_root: 1" in result.stdout
    assert "remaining: 9 leaves and 6 barrels still to convert" in result.stdout
