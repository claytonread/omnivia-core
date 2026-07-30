"""The omnivia-memory facade foundation: the converted leaves and their barrels.

Phase 1 ported portable leaves from ``services/omnivia-memory`` into
``src/omnivia_core`` as source-parity copies (see
``tests/canonical_migration``). This slice retires that duplication leaf by
leaf: every leaf listed in ``LEAF_SYMBOL_SOURCES`` below is no longer a copy --
each is a thin compatibility facade whose supported symbols are the *exact*
canonical objects (``omnivia_memory.X.Symbol is omnivia_core.X.Symbol``), not
structurally equal lookalikes. The barrels above them (``BARREL_ALL_ORDER``)
already delegated to their sibling leaves and need no source change, but their
re-exported objects are now canonical too as a result -- the ``app_manifest``,
``app_shell_bridge``, ``component_contract``, ``module_manifest``, and
``run_ledger`` barrels become identity-preserving purely transitively, through
their two converted leaves each.

This module is the dedicated verification for that transition, independent
of the ``tests/canonical_migration`` source-parity gates (which exclude every
converted leaf via ``FACADE_CANONICAL_TO_LEGACY`` -- see
``tests/canonical_migration/_leaves.py`` and
``tests/canonical_migration/test_parity.py``).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"


def _load_leaves_manifest():
    """Load ``tests/canonical_migration/_leaves.py`` by file path.

    That directory has no ``__init__.py`` (it is imported as a bare, unpackaged
    module by ``tests/canonical_migration/test_parity.py`` itself), so it is
    not reliably reachable via a dotted ``tests.canonical_migration`` import --
    whether that resolves depends on which other directories a given pytest
    invocation has already added to ``sys.path`` as import roots. Loading the
    file directly sidesteps that path/collection-order dependency entirely.
    """
    path = REPO_ROOT / "tests" / "canonical_migration" / "_leaves.py"
    spec = importlib.util.spec_from_file_location("_facade_foundation_leaves_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FACADE_CANONICAL_TO_LEGACY = _load_leaves_manifest().FACADE_CANONICAL_TO_LEGACY
MEMORY_SRC = REPO_ROOT / "services" / "omnivia-memory" / "src"
PYTHON = sys.executable

#: The legacy leaves converted into facades, and -- for each supported symbol
#: bound at that leaf's module scope -- the canonical module that owns the
#: exact object it must route to. Declared independently
#: of ``FACADE_CANONICAL_TO_LEGACY`` (rather than derived from it) so this
#: test also catches that manifest silently drifting, per
#: ``test_facade_canonical_to_legacy_manifest_matches_the_expected_pairs``.
#:
#: Each leaf's set covers its full historical module-scope namespace, not
#: just its contract names: these leaves never declared ``__all__``
#: (``test_leaf_wrapper_has_no_all`` below), so every non-private binding a
#: caller could import before this slice -- including the "incidental"
#: names an ``import``/``from ... import`` statement leaves behind, such as
#: ``Any``, ``annotations``, ``Enum``, or a plain module binding like
#: ``uuid`` -- is still part of the public surface today and must still
#: resolve to the exact same object.
LEAF_SYMBOL_SOURCES: dict[str, dict[str, str]] = {
    "omnivia_memory._shared.validation": {
        "SENSITIVE_KEYS": "omnivia_core._shared.validation",
        "ValidationResult": "omnivia_core._shared.validation",
        "scan_sensitive_fields": "omnivia_core._shared.validation",
        "validate_iso_timestamp": "omnivia_core._shared.validation",
        "validate_optional_iso_timestamp": "omnivia_core._shared.validation",
        "Any": "omnivia_core._shared.validation",
        "annotations": "omnivia_core._shared.validation",
        "dataclass": "omnivia_core._shared.validation",
        "datetime": "omnivia_core._shared.validation",
        "field": "omnivia_core._shared.validation",
    },
    "omnivia_memory.app_manifest.models": {
        "AppManifest": "omnivia_core.app_manifest.models",
        "AppState": "omnivia_core.app_manifest.models",
        "DataSource": "omnivia_core.app_manifest.models",
        "Enum": "omnivia_core.app_manifest.models",
        "List": "omnivia_core.app_manifest.models",
        # Not the Component Contract class of the same name: the App Manifest
        # contract historically defined its own ``ProvenanceRequirement``
        # dataclass, and this leaf must keep routing to that one. See
        # ``test_app_manifest_collision_names_keep_their_historical_owners``.
        "ProvenanceRequirement": "omnivia_core.app_manifest.models",
        # Likewise not the ``_shared.validation`` primitive, nor the App Shell
        # bridge's / Component Contract's / control plane's same-named class.
        "ValidationResult": "omnivia_core.app_manifest.models",
        "dataclass": "omnivia_core.app_manifest.models",
        "field": "omnivia_core.app_manifest.models",
    },
    "omnivia_memory.app_manifest.validation": {
        "Any": "omnivia_core.app_manifest.validation",
        "AppManifest": "omnivia_core.app_manifest.models",
        "AppManifestValidationError": "omnivia_core.app_manifest.validation",
        "AppState": "omnivia_core.app_manifest.models",
        "DataSource": "omnivia_core.app_manifest.models",
        "Dict": "omnivia_core.app_manifest.validation",
        "ProvenanceRequirement": "omnivia_core.app_manifest.models",
        "ValidationResult": "omnivia_core.app_manifest.models",
        "validate_app_manifest": "omnivia_core.app_manifest.validation",
    },
    "omnivia_memory.app_shell_bridge.models": {
        "AppShellBodyDescriptor": "omnivia_core.app_shell_bridge.models",
        "AppShellHostContext": "omnivia_core.app_shell_bridge.models",
        "AppShellRuntimeState": "omnivia_core.app_shell_bridge.models",
        "AppShellSource": "omnivia_core.app_shell_bridge.models",
        "Enum": "omnivia_core.app_shell_bridge.models",
        "List": "omnivia_core.app_shell_bridge.models",
        # Not the ``_shared.validation`` primitive of the same name: the App
        # Shell bridge historically defined its own ``ValidationResult``
        # dataclass, and this leaf must keep routing to that one. See
        # ``test_app_shell_validation_result_keeps_its_historical_collision_owner``.
        "ValidationResult": "omnivia_core.app_shell_bridge.models",
        "dataclass": "omnivia_core.app_shell_bridge.models",
        "field": "omnivia_core.app_shell_bridge.models",
    },
    "omnivia_memory.app_shell_bridge.validation": {
        "Any": "omnivia_core.app_shell_bridge.validation",
        "AppShellBridgeValidationError": "omnivia_core.app_shell_bridge.validation",
        "Dict": "omnivia_core.app_shell_bridge.validation",
        "List": "omnivia_core.app_shell_bridge.validation",
        "TYPE_CHECKING": "omnivia_core.app_shell_bridge.validation",
        "validate_app_shell_body_descriptor": "omnivia_core.app_shell_bridge.validation",
        "validate_app_shell_host_context": "omnivia_core.app_shell_bridge.validation",
    },
    "omnivia_memory.component_contract.models": {
        "AgentAction": "omnivia_core.component_contract.models",
        "AgentBackedComponentContract": "omnivia_core.component_contract.models",
        "AgentBehavior": "omnivia_core.component_contract.models",
        "AgentRunRecord": "omnivia_core.component_contract.models",
        "AgentRunStatus": "omnivia_core.component_contract.models",
        "ApprovalPolicy": "omnivia_core.component_contract.models",
        "AuditRequirement": "omnivia_core.component_contract.models",
        "ComponentAIMode": "omnivia_core.component_contract.models",
        "ComponentConnectorScope": "omnivia_core.component_contract.models",
        "ComponentContract": "omnivia_core.component_contract.models",
        "ComponentDataSource": "omnivia_core.component_contract.models",
        "ComponentFamily": "omnivia_core.component_contract.models",
        "ComponentGraphScope": "omnivia_core.component_contract.models",
        "ComponentInput": "omnivia_core.component_contract.models",
        "ComponentOutput": "omnivia_core.component_contract.models",
        "ComponentOutputType": "omnivia_core.component_contract.models",
        "ComponentPermission": "omnivia_core.component_contract.models",
        "ComponentRunMode": "omnivia_core.component_contract.models",
        "ComponentSafetyLevel": "omnivia_core.component_contract.models",
        "Enum": "omnivia_core.component_contract.models",
        "List": "omnivia_core.component_contract.models",
        "Optional": "omnivia_core.component_contract.models",
        "PermissionPolicy": "omnivia_core.component_contract.models",
        "ProvenanceBehavior": "omnivia_core.component_contract.models",
        # Not the App Manifest contract's class of the same name: the Component
        # Contract historically defined its own ``ProvenanceRequirement``
        # dataclass -- and it is the one the legacy root binds -- so this leaf
        # must keep routing to that one. See
        # ``test_component_contract_collision_names_keep_their_historical_owners``.
        "ProvenanceRequirement": "omnivia_core.component_contract.models",
        # Likewise not the ``_shared.validation`` primitive, nor the App
        # Manifest's / App Shell bridge's / control plane's same-named class.
        "ValidationResult": "omnivia_core.component_contract.models",
        "dataclass": "omnivia_core.component_contract.models",
        "field": "omnivia_core.component_contract.models",
    },
    "omnivia_memory.component_contract.validation": {
        "Any": "omnivia_core.component_contract.validation",
        "ComponentContractValidationError": "omnivia_core.component_contract.validation",
        "Dict": "omnivia_core.component_contract.validation",
        "Enum": "omnivia_core.component_contract.validation",
        "List": "omnivia_core.component_contract.validation",
        "Optional": "omnivia_core.component_contract.validation",
        "TYPE_CHECKING": "omnivia_core.component_contract.validation",
        "validate_agent_run_record": "omnivia_core.component_contract.validation",
        "validate_component_contract": "omnivia_core.component_contract.validation",
    },
    "omnivia_memory.lifecycle.models": {
        "LifecycleState": "omnivia_core.lifecycle.models",
        "Enum": "omnivia_core.lifecycle.models",
        "annotations": "omnivia_core.lifecycle.models",
    },
    "omnivia_memory.lifecycle.rules": {
        "CreatedBy": "omnivia_core.lifecycle.rules",
        "LifecycleRules": "omnivia_core.lifecycle.rules",
        "LifecycleState": "omnivia_core.lifecycle.models",
        "Enum": "omnivia_core.lifecycle.rules",
        "annotations": "omnivia_core.lifecycle.rules",
    },
    "omnivia_memory.module_manifest.models": {
        "Entrypoint": "omnivia_core.module_manifest.models",
        "Enum": "omnivia_core.module_manifest.models",
        "Integrity": "omnivia_core.module_manifest.models",
        "List": "omnivia_core.module_manifest.models",
        "ModuleKind": "omnivia_core.module_manifest.models",
        "ModuleManifest": "omnivia_core.module_manifest.models",
        # No cross-domain collision is known in this batch: these Module
        # Manifest contract names have no alternate owner that a route could
        # silently select.
        "Permission": "omnivia_core.module_manifest.models",
        "PublishedTarget": "omnivia_core.module_manifest.models",
        "dataclass": "omnivia_core.module_manifest.models",
        "field": "omnivia_core.module_manifest.models",
    },
    "omnivia_memory.module_manifest.validation": {
        "Any": "omnivia_core.module_manifest.validation",
        "Dict": "omnivia_core.module_manifest.validation",
        # The validation leaf historically imported the six contract classes it
        # constructs from its sibling models leaf, so they are part of its own
        # module-scope namespace too and must route to that same owner.
        "Entrypoint": "omnivia_core.module_manifest.models",
        "Integrity": "omnivia_core.module_manifest.models",
        "ModuleKind": "omnivia_core.module_manifest.models",
        "ModuleManifest": "omnivia_core.module_manifest.models",
        "ModuleManifestValidationError": "omnivia_core.module_manifest.validation",
        "Permission": "omnivia_core.module_manifest.models",
        "PublishedTarget": "omnivia_core.module_manifest.models",
        "validate_module_manifest": "omnivia_core.module_manifest.validation",
    },
    "omnivia_memory.provenance.models": {
        "Source": "omnivia_core.provenance.models",
        "SourceType": "omnivia_core.provenance.models",
        "Any": "omnivia_core.provenance.models",
        "Enum": "omnivia_core.provenance.models",
        "annotations": "omnivia_core.provenance.models",
    },
    "omnivia_memory.run_ledger.models": {
        # The routed ``ContractVersion`` class is owned by the knowledge domain,
        # not by this leaf's canonical counterpart: the canonical run-ledger
        # models module imports it to build the version constant below. Its
        # identity is therefore checked against its real owner.
        "ContractVersion": "omnivia_core.knowledge.models",
        "Enum": "omnivia_core.run_ledger.models",
        "EvidenceFileRef": "omnivia_core.run_ledger.models",
        "Mapping": "omnivia_core.run_ledger.models",
        # A ``ContractVersion`` *instance*, built by (and owned by) the canonical
        # run-ledger models leaf even though its type lives in the knowledge
        # domain. That split is why converting this leaf also moves one frozen
        # root binding -- see ``baseline.inventory``'s
        # ``FACADE_ROOT_BINDING_OWNER_MOVES``.
        "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.run_ledger.models",
        "RUN_LEDGER_PATH_ENV": "omnivia_core.run_ledger.models",
        "RunLedgerEntry": "omnivia_core.run_ledger.models",
        "RunLedgerProvenance": "omnivia_core.run_ledger.models",
        "RunLedgerStatus": "omnivia_core.run_ledger.models",
        "annotations": "omnivia_core.run_ledger.models",
        "cast": "omnivia_core.run_ledger.models",
        "dataclass": "omnivia_core.run_ledger.models",
        "field": "omnivia_core.run_ledger.models",
    },
    "omnivia_memory.run_ledger.validation": {
        "EvidenceFileRef": "omnivia_core.run_ledger.models",
        "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.run_ledger.models",
        "RunLedgerEntry": "omnivia_core.run_ledger.models",
        "RunLedgerProvenance": "omnivia_core.run_ledger.models",
        "RunLedgerStatus": "omnivia_core.run_ledger.models",
        "TERMINAL_RUN_STATUSES": "omnivia_core.run_ledger.validation",
        # This leaf never had a ``ValidationResult`` of its own: it historically
        # imported the shared primitive (through the legacy knowledge barrel's
        # re-export of it), so it must keep routing to that one and not to any of
        # the four domain classes of the same name.
        "ValidationResult": "omnivia_core._shared.validation",
        "annotations": "omnivia_core.run_ledger.validation",
        # Owned by the knowledge domain's validation leaf, which the canonical
        # run-ledger validator imports it from.
        "check_contract_version_compatibility": "omnivia_core.knowledge.validation",
        "datetime": "omnivia_core.run_ledger.validation",
        "validate_evidence_file_ref": "omnivia_core.run_ledger.validation",
        "validate_run_ledger_entry": "omnivia_core.run_ledger.validation",
        "validate_run_ledger_provenance": "omnivia_core.run_ledger.validation",
    },
    "omnivia_memory.memory.models": {
        "Memory": "omnivia_core.memory.models",
        "MemoryCreate": "omnivia_core.memory.models",
        "MemoryUpdate": "omnivia_core.memory.models",
        "LifecycleState": "omnivia_core.lifecycle.models",
        "CreatedBy": "omnivia_core.lifecycle.rules",
        "Source": "omnivia_core.provenance.models",
        "Any": "omnivia_core.memory.models",
        "annotations": "omnivia_core.memory.models",
        "dataclass": "omnivia_core.memory.models",
        "datetime": "omnivia_core.memory.models",
        "field": "omnivia_core.memory.models",
        "timezone": "omnivia_core.memory.models",
        "uuid": "omnivia_core.memory.models",
    },
}

#: Each leaf's entire body must be exactly one ``from <module> import (...)``
#: statement, sourced from this single module. This is stricter than
#: ``LEAF_SYMBOL_SOURCES`` (which records, per symbol, whichever canonical
#: module *owns* the exact object, for the identity check) -- a leaf may
#: legitimately route a name's identity check to a sibling canonical module
#: (``lifecycle.rules.LifecycleState`` owns the same object as
#: ``lifecycle.models.LifecycleState``) while still importing that name from
#: only one place. See ``_assert_leaf_is_exact_route_facade``.
LEAF_IMPORT_SOURCE: dict[str, str] = {
    "omnivia_memory._shared.validation": "omnivia_core._shared.validation",
    "omnivia_memory.app_manifest.models": "omnivia_core.app_manifest.models",
    "omnivia_memory.app_manifest.validation": "omnivia_core.app_manifest.validation",
    "omnivia_memory.app_shell_bridge.models": "omnivia_core.app_shell_bridge.models",
    "omnivia_memory.app_shell_bridge.validation": "omnivia_core.app_shell_bridge.validation",
    "omnivia_memory.component_contract.models": "omnivia_core.component_contract.models",
    "omnivia_memory.component_contract.validation": (
        "omnivia_core.component_contract.validation"
    ),
    "omnivia_memory.lifecycle.models": "omnivia_core.lifecycle.models",
    "omnivia_memory.lifecycle.rules": "omnivia_core.lifecycle.rules",
    "omnivia_memory.module_manifest.models": "omnivia_core.module_manifest.models",
    "omnivia_memory.module_manifest.validation": (
        "omnivia_core.module_manifest.validation"
    ),
    "omnivia_memory.provenance.models": "omnivia_core.provenance.models",
    "omnivia_memory.memory.models": "omnivia_core.memory.models",
    "omnivia_memory.run_ledger.models": "omnivia_core.run_ledger.models",
    "omnivia_memory.run_ledger.validation": "omnivia_core.run_ledger.validation",
}

#: The barrels above the converted leaves, all source-unchanged, and the exact
#: ordered ``__all__`` each must keep (architecture decision: preserve the
#: existing ordered literal list). Keyed by the shared package-relative suffix
#: so both trees (``omnivia_core.<suffix>`` / ``omnivia_memory.<suffix>``) can
#: be checked from one entry.
BARREL_ALL_ORDER: dict[str, list[str]] = {
    "_shared": [
        "SENSITIVE_KEYS",
        "ValidationResult",
        "scan_sensitive_fields",
        "validate_iso_timestamp",
        "validate_optional_iso_timestamp",
    ],
    "app_manifest": [
        "AppManifest",
        "AppManifestValidationError",
        "AppState",
        "DataSource",
        "ProvenanceRequirement",
        "ValidationResult",
        "validate_app_manifest",
    ],
    "app_shell_bridge": [
        "AppShellRuntimeState",
        "AppShellSource",
        "ValidationResult",
        "AppShellHostContext",
        "AppShellBodyDescriptor",
        "AppShellBridgeValidationError",
        "validate_app_shell_host_context",
        "validate_app_shell_body_descriptor",
    ],
    "component_contract": [
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
    ],
    "lifecycle": ["LifecycleState", "LifecycleRules", "CreatedBy"],
    "module_manifest": [
        "Entrypoint",
        "Integrity",
        "ModuleKind",
        "ModuleManifest",
        "ModuleManifestValidationError",
        "Permission",
        "PublishedTarget",
        "validate_module_manifest",
    ],
    "provenance": ["Source", "SourceType"],
    "run_ledger": [
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
    ],
}

#: The barrels whose legacy source is a pure *absolute*-import re-export body,
#: checkable by ``_assert_pure_facade_module``. ``app_shell_bridge`` and
#: ``component_contract`` are deliberately absent: their legacy barrels
#: re-export through the historical ``from .models import ...`` /
#: ``from .validation import ...`` relative form and stay source-unchanged, so
#: they get their own stricter, shape-exact gate
#: (``test_relative_import_barrel_source_is_unchanged_reexport``) rather than a
#: relaxed version of the shared one.
ABSOLUTE_IMPORT_BARRELS: tuple[str, ...] = (
    "_shared",
    "app_manifest",
    "lifecycle",
    "module_manifest",
    "provenance",
    "run_ledger",
)

#: The exact, ordered *absolute* re-export shape the unchanged legacy
#: app-manifest barrel must still have: ``(absolute module, imported names in
#: source order)``. The shared AST gate above only proves the barrel is *some*
#: pure absolute-import re-export; this pins which two modules it reaches and
#: in which order it names all seven exports, which is what makes its
#: identity-preservation purely transitive through the two converted leaves.
#: The name order here is the legacy barrel's own historical source order --
#: deliberately not the canonical barrel's, which sorts its models import.
APP_MANIFEST_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.app_manifest.models",
        (
            "AppState",
            "AppManifest",
            "DataSource",
            "ProvenanceRequirement",
            "ValidationResult",
        ),
    ),
    (
        "omnivia_memory.app_manifest.validation",
        (
            "AppManifestValidationError",
            "validate_app_manifest",
        ),
    ),
)

#: The same, for the unchanged legacy module-manifest barrel. Its name order is
#: its own historical source order -- which here happens to match the canonical
#: barrel's, unlike the app-manifest pair.
MODULE_MANIFEST_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.module_manifest.models",
        (
            "Entrypoint",
            "Integrity",
            "ModuleKind",
            "ModuleManifest",
            "Permission",
            "PublishedTarget",
        ),
    ),
    (
        "omnivia_memory.module_manifest.validation",
        (
            "ModuleManifestValidationError",
            "validate_module_manifest",
        ),
    ),
)

#: The same, for the unchanged legacy run-ledger barrel. Its name order is its
#: own historical source order -- which here matches the canonical barrel's,
#: including the two constants leading the models block ahead of the classes
#: rather than being alphabetized.
RUN_LEDGER_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.run_ledger.models",
        (
            "RUN_LEDGER_CONTRACT_VERSION",
            "RUN_LEDGER_PATH_ENV",
            "EvidenceFileRef",
            "RunLedgerEntry",
            "RunLedgerProvenance",
            "RunLedgerStatus",
        ),
    ),
    (
        "omnivia_memory.run_ledger.validation",
        (
            "TERMINAL_RUN_STATUSES",
            "validate_evidence_file_ref",
            "validate_run_ledger_entry",
            "validate_run_ledger_provenance",
        ),
    ),
)

#: The absolute-import barrels that additionally get the stricter, shape-exact
#: gate below, and the exact shape each must keep. Keyed by barrel suffix so the
#: shape gate and the transitive-identity gate both run over the same declared
#: set -- a barrel cannot be dropped from one without also leaving the other.
#: This is a subset of ``ABSOLUTE_IMPORT_BARRELS`` -- these barrels sit directly
#: above a converted leaf pair, which is what the transitive route has to be
#: pinned for. It grows batch by batch as further barrels get an exact declared
#: shape, so it is not expected to cover every such barrel yet.
ABSOLUTE_IMPORT_BARREL_IMPORTS: dict[
    str, tuple[tuple[str, tuple[str, ...]], ...]
] = {
    "app_manifest": APP_MANIFEST_BARREL_ABSOLUTE_IMPORTS,
    "module_manifest": MODULE_MANIFEST_BARREL_ABSOLUTE_IMPORTS,
    "run_ledger": RUN_LEDGER_BARREL_ABSOLUTE_IMPORTS,
}

#: The exact, ordered relative re-export shape the unchanged legacy app-shell
#: barrel must still have: ``(relative module, imported names in source order)``.
APP_SHELL_BARREL_RELATIVE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "models",
        (
            "AppShellRuntimeState",
            "AppShellSource",
            "ValidationResult",
            "AppShellHostContext",
            "AppShellBodyDescriptor",
        ),
    ),
    (
        "validation",
        (
            "AppShellBridgeValidationError",
            "validate_app_shell_host_context",
            "validate_app_shell_body_descriptor",
        ),
    ),
)

#: The same, for the unchanged legacy component-contract barrel. Its name order
#: is its own historical source order -- which here happens to match the
#: canonical barrel's, unlike the app-manifest pair.
COMPONENT_CONTRACT_BARREL_RELATIVE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "models",
        (
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
        ),
    ),
    (
        "validation",
        (
            "ComponentContractValidationError",
            "validate_agent_run_record",
            "validate_component_contract",
        ),
    ),
)

#: Every barrel held out of ``ABSOLUTE_IMPORT_BARRELS``, and the exact relative
#: re-export shape it must keep. Keyed by barrel suffix so the shape gate and
#: the transitive-identity gate below both run over the same declared set --
#: a barrel cannot be dropped from one without also leaving the other.
RELATIVE_IMPORT_BARREL_IMPORTS: dict[
    str, tuple[tuple[str, tuple[str, ...]], ...]
] = {
    "app_shell_bridge": APP_SHELL_BARREL_RELATIVE_IMPORTS,
    "component_contract": COMPONENT_CONTRACT_BARREL_RELATIVE_IMPORTS,
}

#: Independently declared expectation for the manifest the migration-test
#: suite uses to exclude every converted leaf from its source-parity gates.
EXPECTED_FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core._shared.validation": "omnivia_memory._shared.validation",
    "omnivia_core.app_manifest.models": "omnivia_memory.app_manifest.models",
    "omnivia_core.app_manifest.validation": "omnivia_memory.app_manifest.validation",
    "omnivia_core.app_shell_bridge.models": "omnivia_memory.app_shell_bridge.models",
    "omnivia_core.app_shell_bridge.validation": "omnivia_memory.app_shell_bridge.validation",
    "omnivia_core.component_contract.models": "omnivia_memory.component_contract.models",
    "omnivia_core.component_contract.validation": (
        "omnivia_memory.component_contract.validation"
    ),
    "omnivia_core.lifecycle.models": "omnivia_memory.lifecycle.models",
    "omnivia_core.lifecycle.rules": "omnivia_memory.lifecycle.rules",
    "omnivia_core.module_manifest.models": "omnivia_memory.module_manifest.models",
    "omnivia_core.module_manifest.validation": (
        "omnivia_memory.module_manifest.validation"
    ),
    "omnivia_core.provenance.models": "omnivia_memory.provenance.models",
    "omnivia_core.memory.models": "omnivia_memory.memory.models",
    "omnivia_core.run_ledger.models": "omnivia_memory.run_ledger.models",
    "omnivia_core.run_ledger.validation": "omnivia_memory.run_ledger.validation",
}


#: Contract names that collide across independent domains, and the canonical
#: module that owns each domain's own class of that name. Every positive "is the
#: exact canonical object" assertion in this module would still pass if two of
#: these owners collapsed onto a single object, so their separation is pinned
#: explicitly -- in-process and in a fresh process, in both import orders.
COLLIDING_OWNERS: dict[str, tuple[str, ...]] = {
    "ValidationResult": (
        "omnivia_core._shared.validation",
        "omnivia_core.app_manifest.models",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_core.component_contract.models",
        "omnivia_core.control_plane.models",
    ),
    "ProvenanceRequirement": (
        "omnivia_core.app_manifest.models",
        "omnivia_core.component_contract.models",
    ),
}

#: For each colliding name, the single owner the *legacy package root* has always
#: re-exported it from. The root itself is deliberately unedited by this slice:
#: these two bindings move to the canonical objects transitively, through the
#: converted leaves, and which owner each lands on must not change. See
#: ``test_legacy_root_keeps_its_historical_owner_for_each_colliding_name``.
ROOT_OWNERS: dict[str, str] = {
    "ValidationResult": "omnivia_core._shared.validation",
    "ProvenanceRequirement": "omnivia_core.component_contract.models",
}


def _collision_pairs() -> list[tuple[str, str, str]]:
    """``(name, owner, other owner)`` for every unordered pair of owners."""
    return [
        (name, owners[i], owners[j])
        for name, owners in COLLIDING_OWNERS.items()
        for i in range(len(owners))
        for j in range(i + 1, len(owners))
    ]


def _leaf_symbol_cases() -> list[tuple[str, str, str]]:
    return [
        (legacy_module, symbol, canonical_module)
        for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items()
        for symbol, canonical_module in symbols.items()
    ]


@pytest.mark.parametrize(
    "legacy_module,symbol,canonical_module",
    _leaf_symbol_cases(),
    ids=[f"{m}.{s}" for m, s, _ in _leaf_symbol_cases()],
)
def test_leaf_symbol_is_exact_canonical_object(
    legacy_module: str, symbol: str, canonical_module: str
) -> None:
    legacy = importlib.import_module(legacy_module)
    canonical = importlib.import_module(canonical_module)
    assert getattr(legacy, symbol) is getattr(canonical, symbol), (
        f"{legacy_module}.{symbol} is not the exact same object as "
        f"{canonical_module}.{symbol}"
    )


@pytest.mark.parametrize("barrel,expected_all", sorted(BARREL_ALL_ORDER.items()))
def test_barrel_all_is_exact_ordered_literal_in_both_trees(
    barrel: str, expected_all: list[str]
) -> None:
    legacy = importlib.import_module(f"omnivia_memory.{barrel}")
    canonical = importlib.import_module(f"omnivia_core.{barrel}")
    assert legacy.__all__ == expected_all
    assert canonical.__all__ == expected_all


@pytest.mark.parametrize("barrel,expected_all", sorted(BARREL_ALL_ORDER.items()))
def test_barrel_symbols_are_exact_canonical_objects(
    barrel: str, expected_all: list[str]
) -> None:
    legacy = importlib.import_module(f"omnivia_memory.{barrel}")
    canonical = importlib.import_module(f"omnivia_core.{barrel}")
    for name in expected_all:
        assert getattr(legacy, name) is getattr(canonical, name), (
            f"omnivia_memory.{barrel}.{name} is not the exact same object as "
            f"omnivia_core.{barrel}.{name}"
        )


@pytest.mark.parametrize("leaf_name", sorted(LEAF_SYMBOL_SOURCES))
def test_leaf_wrapper_has_no_all(leaf_name: str) -> None:
    module = importlib.import_module(leaf_name)
    assert not hasattr(module, "__all__"), (
        f"{leaf_name} must not define __all__ -- it never did before becoming a facade"
    )


def _star_import_namespace(module_name: str) -> set[str]:
    namespace: dict[str, object] = {}
    exec(f"from {module_name} import *", namespace)  # noqa: S102
    return {name for name in namespace if name != "__builtins__"}


@pytest.mark.parametrize("leaf_name", sorted(LEAF_SYMBOL_SOURCES))
def test_leaf_star_import_exposes_exactly_the_routed_namespace(leaf_name: str) -> None:
    """A converted leaf declares no ``__all__`` (``test_leaf_wrapper_has_no_all``),
    so ``from <leaf> import *`` exposes exactly its non-underscore module-scope
    bindings -- which is precisely the historical surface
    ``LEAF_SYMBOL_SOURCES`` enumerates. Checking the star surface directly is
    what proves that enumeration is *complete* rather than merely correct: a
    facade that dropped an incidental name, or picked up an extra binding of its
    own, passes every per-symbol identity check above and fails here.
    """
    exported = _star_import_namespace(leaf_name)
    assert exported == set(LEAF_SYMBOL_SOURCES[leaf_name]), (
        f"star import of {leaf_name} exposed {sorted(exported)}, expected exactly "
        f"{sorted(LEAF_SYMBOL_SOURCES[leaf_name])}"
    )


@pytest.mark.parametrize("barrel,expected_all", sorted(BARREL_ALL_ORDER.items()))
def test_barrel_star_import_exposes_exactly_advertised_names_in_both_trees(
    barrel: str, expected_all: list[str]
) -> None:
    """Both trees' barrels advertise ``__all__``, so their star surface must be
    exactly that set -- and each starred name must be the exact canonical
    object, so a star-importing caller of the legacy path gets the same objects
    a direct canonical importer does."""
    canonical_module = importlib.import_module(f"omnivia_core.{barrel}")
    for package in ("omnivia_core", "omnivia_memory"):
        module_name = f"{package}.{barrel}"
        exported = _star_import_namespace(module_name)
        assert exported == set(expected_all), (
            f"star import of {module_name} exposed {sorted(exported)}, expected "
            f"exactly {sorted(expected_all)}"
        )
        module = importlib.import_module(module_name)
        for name in expected_all:
            assert getattr(module, name) is getattr(canonical_module, name), (
                f"{module_name}.{name} is not the exact same object as "
                f"omnivia_core.{barrel}.{name}"
            )


def _module_body_after_docstring(module_name: str) -> list[ast.stmt]:
    module = importlib.import_module(module_name)
    path = getattr(module, "__file__", None)
    assert path is not None, f"{module_name} has no source file to inspect"
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=module_name)
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _assert_pure_facade_module(module_name: str, *, allow_all: bool) -> None:
    """Every statement is a docstring (stripped above), a ``from __future__
    import ...``, a plain absolute ``from omnivia_(core|memory) import ...``
    with no wildcard and no renaming alias, or -- only where ``allow_all`` --
    a single literal ``__all__ = [...]`` assignment of string constants.
    Anything else (a def, a class, a bare ``import``, a conditional, an
    ``__getattr__``, a ``sys.modules`` write) fails the module.
    """
    for node in _module_body_after_docstring(module_name):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{module_name}: relative import is not allowed: {ast.dump(node)}"
            assert node.module is not None, f"{module_name}: bare relative import"
            root = node.module.split(".")[0]
            assert node.module == "__future__" or root in {"omnivia_core", "omnivia_memory"}, (
                f"{module_name}: unexpected import source {node.module!r}"
            )
            for alias in node.names:
                assert alias.name != "*", f"{module_name}: star import is not allowed"
                assert alias.asname is None, (
                    f"{module_name}: {alias.name!r} uses a rename/dynamic alias, not a plain import"
                )
            continue
        if allow_all and isinstance(node, ast.Assign):
            assert len(node.targets) == 1, f"{module_name}: unexpected multi-target assignment"
            target = node.targets[0]
            assert isinstance(target, ast.Name) and target.id == "__all__", (
                f"{module_name}: unexpected assignment target {ast.dump(target)}"
            )
            assert isinstance(node.value, ast.List), f"{module_name}: __all__ is not a literal list"
            for elt in node.value.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
                    f"{module_name}: __all__ contains a non-literal-string element"
                )
            continue
        raise AssertionError(f"{module_name}: disallowed statement {ast.dump(node)}")


def _assert_leaf_is_exact_route_facade(leaf_name: str) -> None:
    """A converted leaf's entire body (after its docstring) must be exactly one
    ``from <canonical route> import (<exact expected name set>)`` statement:
    no defs, no class, no bare ``import``, no conditional, no ``__getattr__``
    or ``sys.modules`` write, no second import statement, and -- within that
    one statement -- no wildcard, no relative import, no rename/alias, no
    name outside the exact expected set for this leaf. This is what makes
    the facade a pure route rather than a proxy: every name it can produce
    is decided at import time, from exactly one source, by the interpreter's
    own import machinery.
    """
    body = _module_body_after_docstring(leaf_name)
    assert len(body) == 1, (
        f"{leaf_name}: expected exactly one statement (a single import), found "
        f"{len(body)}: {[ast.dump(node) for node in body]}"
    )
    (node,) = body
    assert isinstance(node, ast.ImportFrom), (
        f"{leaf_name}: expected a single `from ... import ...` statement, found "
        f"{ast.dump(node)}"
    )
    assert node.level == 0, f"{leaf_name}: relative import is not allowed"
    expected_source = LEAF_IMPORT_SOURCE[leaf_name]
    assert node.module == expected_source, (
        f"{leaf_name}: imports from {node.module!r}, expected exactly {expected_source!r}"
    )
    names: set[str] = set()
    for alias in node.names:
        assert alias.name != "*", f"{leaf_name}: star import is not allowed"
        assert alias.asname is None, (
            f"{leaf_name}: {alias.name!r} uses a rename/dynamic alias, not a plain import"
        )
        names.add(alias.name)
    expected_names = set(LEAF_SYMBOL_SOURCES[leaf_name])
    assert names == expected_names, (
        f"{leaf_name}: imports {sorted(names)} from {expected_source!r}, expected exactly "
        f"{sorted(expected_names)}"
    )


@pytest.mark.parametrize("leaf_name", sorted(LEAF_SYMBOL_SOURCES))
def test_leaf_wrapper_ast_is_pure_facade(leaf_name: str) -> None:
    _assert_leaf_is_exact_route_facade(leaf_name)


@pytest.mark.parametrize("barrel", sorted(f"omnivia_memory.{b}" for b in ABSOLUTE_IMPORT_BARRELS))
def test_barrel_ast_is_pure_facade(barrel: str) -> None:
    _assert_pure_facade_module(barrel, allow_all=True)


def test_absolute_import_barrels_cover_every_barrel_but_the_relative_exceptions() -> None:
    """A barrel may only be held out of the shared absolute-import AST gate by
    being declared in ``RELATIVE_IMPORT_BARREL_IMPORTS``, which subjects it to
    the stricter shape-exact gate below instead. The two sets must partition
    ``BARREL_ALL_ORDER`` exactly, so a barrel cannot slip out of the absolute
    gate without landing in the relative one -- or be listed as relative while
    still being checked as absolute."""
    held_out = set(BARREL_ALL_ORDER) - set(ABSOLUTE_IMPORT_BARRELS)
    assert held_out == set(RELATIVE_IMPORT_BARREL_IMPORTS), (
        "only barrels declared in RELATIVE_IMPORT_BARREL_IMPORTS may be held out of "
        f"the shared absolute-import AST gate; found {sorted(held_out)}"
    )
    assert set(ABSOLUTE_IMPORT_BARRELS).isdisjoint(RELATIVE_IMPORT_BARREL_IMPORTS)


def test_absolute_import_barrel_imports_only_names_barrels_in_the_absolute_gate() -> None:
    """``ABSOLUTE_IMPORT_BARREL_IMPORTS`` holds the absolute-import barrels that
    additionally get the stricter, shape-exact gate. Which barrels carry a
    declared exact shape grows batch by batch, so there is no derivable coverage
    rule to assert here; what must hold is that each declared shape belongs to
    the gate it is filed under.

    A barrel listed here but not in ``ABSOLUTE_IMPORT_BARRELS`` would be
    shape-checked as absolute while the shared absolute AST gate never ran on
    it. A barrel listed in both shape dicts would be asserted to have relative
    *and* absolute imports, so one of the two gates could only pass vacuously.
    And a shape filed under the wrong barrel key would pin the wrong module's
    source while reading as coverage for this one.
    """
    assert set(ABSOLUTE_IMPORT_BARREL_IMPORTS) <= set(ABSOLUTE_IMPORT_BARRELS), (
        "shape-checked absolute barrels must also be in ABSOLUTE_IMPORT_BARRELS; "
        f"stray={sorted(set(ABSOLUTE_IMPORT_BARREL_IMPORTS) - set(ABSOLUTE_IMPORT_BARRELS))}"
    )
    assert set(ABSOLUTE_IMPORT_BARREL_IMPORTS).isdisjoint(RELATIVE_IMPORT_BARREL_IMPORTS)

    for barrel, expected_imports in ABSOLUTE_IMPORT_BARREL_IMPORTS.items():
        assert barrel in BARREL_ALL_ORDER, (
            f"ABSOLUTE_IMPORT_BARREL_IMPORTS[{barrel!r}] is not a declared barrel"
        )
        # Each declared shape must re-export from the converted leaves of its own
        # barrel, so a shape cannot be attached to the wrong barrel key.
        for module, _names in expected_imports:
            assert module.startswith(f"omnivia_memory.{barrel}."), (
                f"ABSOLUTE_IMPORT_BARREL_IMPORTS[{barrel!r}] re-exports from {module}, "
                "which is not one of its own leaves"
            )
            assert module in LEAF_SYMBOL_SOURCES, (
                f"{module} is declared as a barrel source but is not a converted leaf"
            )


@pytest.mark.parametrize("barrel", sorted(ABSOLUTE_IMPORT_BARREL_IMPORTS))
def test_absolute_import_barrel_source_is_unchanged_reexport(barrel: str) -> None:
    """These legacy barrels are *source-unchanged* by this slice: each becomes
    identity-preserving transitively, through its two converted leaves, not by
    being rewritten itself. Pin each one's exact historical shape -- two
    absolute ``from omnivia_memory.<barrel>.<leaf> import (...)`` statements in
    source order with their exact ordered name lists, then the ``__all__``
    literal -- so a future edit that reroutes a barrel directly at
    ``omnivia_core``, adds a ``__getattr__``, or reorders its re-exports fails
    here rather than only shifting which module the identity happens to come
    from.
    """
    expected_imports = ABSOLUTE_IMPORT_BARREL_IMPORTS[barrel]
    module_name = f"omnivia_memory.{barrel}"
    body = _module_body_after_docstring(module_name)
    assert len(body) == len(expected_imports) + 1, (
        f"{module_name}: expected exactly {len(expected_imports)} absolute imports plus "
        f"__all__, found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(body, expected_imports, strict=False):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {ast.dump(node)}"
        assert node.level == 0, f"{module_name}: the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {ast.dump(all_node)}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert [
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ] == BARREL_ALL_ORDER[barrel]

    # Every name the two imports bind is exactly what ``__all__`` advertises:
    # the barrel adds nothing of its own and hides nothing it imported.
    imported = sorted(name for _, names in expected_imports for name in names)
    assert imported == sorted(BARREL_ALL_ORDER[barrel])


@pytest.mark.parametrize("barrel", sorted(ABSOLUTE_IMPORT_BARREL_IMPORTS))
def test_absolute_import_barrel_identity_is_transitive_through_its_leaves(
    barrel: str,
) -> None:
    """Each export must be the exact object bound at the *legacy leaf* it
    re-exports from, and that object must in turn be the canonical one. A barrel
    that started sourcing a name from somewhere else would still pass the
    canonical-identity check alone; requiring the leaf hop too is what pins the
    transitive route.
    """
    barrel_module = importlib.import_module(f"omnivia_memory.{barrel}")
    for legacy_leaf_name, names in ABSOLUTE_IMPORT_BARREL_IMPORTS[barrel]:
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        canonical_leaf = importlib.import_module(LEAF_IMPORT_SOURCE[legacy_leaf_name])
        for name in names:
            assert getattr(barrel_module, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.{barrel}.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            assert getattr(barrel_module, name) is getattr(canonical_leaf, name), (
                f"omnivia_memory.{barrel}.{name} is not the exact object bound at "
                f"{LEAF_IMPORT_SOURCE[legacy_leaf_name]}.{name}"
            )


def test_app_manifest_collision_names_keep_their_historical_owners() -> None:
    """``ValidationResult`` and ``ProvenanceRequirement`` are name collisions
    across independent domains. The App Manifest contract's own dataclasses are
    the ones this leaf historically exposed, so routing either to another
    domain's same-named class would be a silent contract swap that every "is the
    exact canonical object" check above would still pass. Pin the owners, and
    pin that they are *not* the others.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.app_manifest.models")
    legacy_barrel = importlib.import_module("omnivia_memory.app_manifest")
    canonical_leaf = importlib.import_module("omnivia_core.app_manifest.models")

    for name in ("ValidationResult", "ProvenanceRequirement"):
        assert getattr(legacy_leaf, name) is getattr(canonical_leaf, name)
        assert getattr(legacy_barrel, name) is getattr(canonical_leaf, name)

    for other_module in (
        "omnivia_core._shared.validation",
        "omnivia_memory._shared.validation",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_memory.app_shell_bridge.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        for name in ("ValidationResult", "ProvenanceRequirement"):
            if not hasattr(other, name):
                continue
            assert getattr(legacy_leaf, name) is not getattr(other, name), (
                f"omnivia_memory.app_manifest.models.{name} must stay the App Manifest "
                f"contract's own class, not {other_module}.{name}"
            )


@pytest.mark.parametrize(
    "name,owner,other",
    _collision_pairs(),
    ids=[f"{n}-{a.split('.')[-2]}-vs-{b.split('.')[-2]}" for n, a, b in _collision_pairs()],
)
def test_colliding_contract_names_stay_distinct_objects_in_both_trees(
    name: str, owner: str, other: str
) -> None:
    """No two domains' same-named contract classes may become one object.

    Each tree is checked on its own terms: distinctness is a property of the
    domains, not of the migration, so it must hold in the canonical tree and in
    the legacy tree whether the owners involved are facades yet or not.
    """
    for package in ("omnivia_core", "omnivia_memory"):
        left = importlib.import_module(owner.replace("omnivia_core", package, 1))
        right = importlib.import_module(other.replace("omnivia_core", package, 1))
        assert getattr(left, name) is not getattr(right, name), (
            f"{left.__name__}.{name} and {right.__name__}.{name} are the same "
            "object; these are independent domains' contracts"
        )


def test_colliding_owners_cover_every_facade_leaf_that_binds_the_name() -> None:
    """``COLLIDING_OWNERS`` must not fall behind ``LEAF_SYMBOL_SOURCES``: any
    canonical module a converted leaf routes a colliding name to has to be
    listed as one of that name's owners, or its separation from the other
    domains would go unchecked."""
    for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items():
        for symbol, canonical_module in symbols.items():
            if symbol not in COLLIDING_OWNERS:
                continue
            assert canonical_module in COLLIDING_OWNERS[symbol], (
                f"{legacy_module}.{symbol} routes to {canonical_module}, which is not "
                f"listed among COLLIDING_OWNERS[{symbol!r}]"
            )


@pytest.mark.parametrize("barrel", sorted(RELATIVE_IMPORT_BARREL_IMPORTS))
def test_relative_import_barrel_source_is_unchanged_reexport(barrel: str) -> None:
    """These legacy barrels are *source-unchanged*: each becomes
    identity-preserving transitively, through its two converted leaves, not by
    being rewritten itself. Pin each one's exact historical shape -- two
    relative ``from .<leaf> import (...)`` statements in source order with
    their exact ordered name lists, then the ``__all__`` literal -- so a
    future edit that reroutes a barrel directly at ``omnivia_core``, adds a
    ``__getattr__``, or reorders its re-exports fails here.
    """
    expected_imports = RELATIVE_IMPORT_BARREL_IMPORTS[barrel]
    module_name = f"omnivia_memory.{barrel}"
    body = _module_body_after_docstring(module_name)
    assert len(body) == len(expected_imports) + 1, (
        f"{module_name}: expected exactly {len(expected_imports)} relative imports plus "
        f"__all__, found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(body, expected_imports, strict=False):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {ast.dump(node)}"
        assert node.level == 1, f"{module_name}: {module} import is not relative"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {ast.dump(all_node)}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert [
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ] == BARREL_ALL_ORDER[barrel]

    # Every name the two imports bind is exactly what ``__all__`` advertises:
    # the barrel adds nothing of its own and hides nothing it imported.
    imported = sorted(name for _, names in expected_imports for name in names)
    assert imported == sorted(BARREL_ALL_ORDER[barrel])


@pytest.mark.parametrize("barrel", sorted(RELATIVE_IMPORT_BARREL_IMPORTS))
def test_relative_import_barrel_identity_is_transitive_through_its_leaves(
    barrel: str,
) -> None:
    """Each export must be the exact object bound at the *legacy leaf* it
    re-exports from, and that object must in turn be the canonical one. A barrel
    that started sourcing a name from somewhere else would still pass the
    canonical-identity check alone; requiring the leaf hop too is what pins the
    transitive route.
    """
    barrel_module = importlib.import_module(f"omnivia_memory.{barrel}")
    for relative_leaf, names in RELATIVE_IMPORT_BARREL_IMPORTS[barrel]:
        legacy_leaf_name = f"omnivia_memory.{barrel}.{relative_leaf}"
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        canonical_leaf = importlib.import_module(LEAF_IMPORT_SOURCE[legacy_leaf_name])
        for name in names:
            assert getattr(barrel_module, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.{barrel}.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            assert getattr(barrel_module, name) is getattr(canonical_leaf, name), (
                f"omnivia_memory.{barrel}.{name} is not the exact object bound at "
                f"{LEAF_IMPORT_SOURCE[legacy_leaf_name]}.{name}"
            )


def test_component_contract_collision_names_keep_their_historical_owners() -> None:
    """``ValidationResult`` and ``ProvenanceRequirement`` are name collisions
    across independent domains. The Component Contract's own dataclasses are the
    ones this leaf historically exposed -- and its ``ProvenanceRequirement`` is
    additionally the one the legacy package root binds -- so routing either to
    another domain's same-named class would be a silent contract swap that every
    "is the exact canonical object" check above would still pass. Pin the owners,
    and pin that they are *not* the others.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.component_contract.models")
    legacy_barrel = importlib.import_module("omnivia_memory.component_contract")
    canonical_leaf = importlib.import_module("omnivia_core.component_contract.models")

    for name in ("ValidationResult", "ProvenanceRequirement"):
        assert getattr(legacy_leaf, name) is getattr(canonical_leaf, name)
        assert getattr(legacy_barrel, name) is getattr(canonical_leaf, name)

    for other_module in (
        "omnivia_core._shared.validation",
        "omnivia_memory._shared.validation",
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_memory.app_shell_bridge.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        for name in ("ValidationResult", "ProvenanceRequirement"):
            if not hasattr(other, name):
                continue
            assert getattr(legacy_leaf, name) is not getattr(other, name), (
                f"omnivia_memory.component_contract.models.{name} must stay the "
                f"Component Contract's own class, not {other_module}.{name}"
            )


def test_legacy_root_keeps_its_historical_owner_for_each_colliding_name() -> None:
    """The legacy package root re-exports both colliding names, but from only one
    owner each: ``ProvenanceRequirement`` from the Component Contract (via
    ``from .component_contract import ...``) and ``ValidationResult`` from the
    shared primitive (via the knowledge barrel's re-export of it). The root is
    *not* edited by this slice, so those two bindings must still resolve to the
    same objects they always did -- now the canonical ones, reached transitively
    through the converted leaves.

    This is the invariant the leaf-level checks cannot see. ``ROOT_OWNERS``
    below pins one owner per name out of the five/two candidates, and the
    negative half pins that the root did not silently pick up a *different*
    domain's same-named class -- which is exactly what a converted leaf routed
    to the wrong owner would cause, several import hops away from the leaf.
    """
    root = importlib.import_module("omnivia_memory")
    for name, owner in ROOT_OWNERS.items():
        expected = getattr(importlib.import_module(owner), name)
        assert getattr(root, name) is expected, (
            f"omnivia_memory.{name} is no longer the object bound at {owner}.{name}"
        )
        for other in COLLIDING_OWNERS[name]:
            if other == owner:
                continue
            assert getattr(root, name) is not getattr(
                importlib.import_module(other), name
            ), (
                f"omnivia_memory.{name} must stay {owner}.{name}, not {other}.{name}"
            )


def test_root_owners_name_a_single_listed_owner_for_every_colliding_name() -> None:
    """``ROOT_OWNERS`` must stay in step with ``COLLIDING_OWNERS``: every
    colliding name the root re-exports needs exactly one declared owner, and
    that owner has to be one of the candidates the collision test already
    separates -- otherwise the root invariant above could be satisfied by an
    owner whose distinctness from the others is unchecked."""
    assert set(ROOT_OWNERS) == set(COLLIDING_OWNERS)
    for name, owner in ROOT_OWNERS.items():
        assert owner in COLLIDING_OWNERS[name], (
            f"ROOT_OWNERS[{name!r}] is {owner}, which is not one of that name's "
            f"declared colliding owners {COLLIDING_OWNERS[name]}"
        )


def test_app_shell_validation_result_keeps_its_historical_collision_owner() -> None:
    """``ValidationResult`` is a name collision across five independent
    domains. The App Shell bridge's own dataclass is the one this leaf
    historically exposed, so routing it to the shared primitive (or to any
    other domain's same-named result type) would be a silent contract swap
    that every "is the exact canonical object" check above would still pass.
    Pin the owner, and pin that it is *not* the others.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.app_shell_bridge.models")
    legacy_barrel = importlib.import_module("omnivia_memory.app_shell_bridge")
    canonical_leaf = importlib.import_module("omnivia_core.app_shell_bridge.models")

    assert legacy_leaf.ValidationResult is canonical_leaf.ValidationResult
    assert legacy_barrel.ValidationResult is canonical_leaf.ValidationResult

    for other_module in (
        "omnivia_core._shared.validation",
        "omnivia_memory._shared.validation",
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        assert legacy_leaf.ValidationResult is not other.ValidationResult, (
            "omnivia_memory.app_shell_bridge.models.ValidationResult must stay the App "
            f"Shell bridge's own dataclass, not {other_module}.ValidationResult"
        )


def test_facade_canonical_to_legacy_manifest_matches_the_expected_pairs() -> None:
    """``FACADE_CANONICAL_TO_LEGACY`` (imported from the migration-test
    manifest) must be exactly the pairs declared here -- neither manifest may drift
    (grow, shrink, or repoint) without this dedicated test noticing, since
    that shared constant is also what excludes these leaves from the
    canonical_migration source-parity gates."""
    assert FACADE_CANONICAL_TO_LEGACY == EXPECTED_FACADE_CANONICAL_TO_LEGACY
    assert set(LEAF_SYMBOL_SOURCES) == set(EXPECTED_FACADE_CANONICAL_TO_LEGACY.values())


def _run_isolated(script: str) -> None:
    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated subprocess failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_canonical_core_imports_independently_of_omnivia_memory() -> None:
    """Every canonical owner behind this slice's facades must import cleanly
    with only ``src`` on ``sys.path`` -- ``services/omnivia-memory/src`` is
    never added, so an accidental ``import omnivia_memory`` anywhere in the
    canonical chain would surface as a hard failure here, not a silent pass."""
    canonical_modules = sorted(EXPECTED_FACADE_CANONICAL_TO_LEGACY)
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            *(f"import {module}" for module in canonical_modules),
            "assert 'omnivia_memory' not in sys.modules",
        ]
    )
    _run_isolated(script)


def _fresh_process_identity_script(*, canonical_first: bool) -> str:
    canonical_modules = sorted(EXPECTED_FACADE_CANONICAL_TO_LEGACY)
    legacy_modules = sorted(LEAF_SYMBOL_SOURCES)
    first, second = (
        (canonical_modules, legacy_modules) if canonical_first else (legacy_modules, canonical_modules)
    )
    lines = [
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        *(f"import {module}" for module in first),
        *(f"import {module}" for module in second),
    ]
    # Every collision owner, even where it is not itself a facade route: the
    # distinctness assertions below have to be able to reach it.
    collision_modules = sorted(
        {module for owners in COLLIDING_OWNERS.values() for module in owners}
        - set(canonical_modules)
    )
    lines.extend(f"import {module}" for module in collision_modules)
    for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items():
        for symbol, canonical_module in symbols.items():
            lines.append(
                f"assert {legacy_module}.{symbol} is {canonical_module}.{symbol}, "
                f"'{legacy_module}.{symbol} is not {canonical_module}.{symbol} "
                f"(canonical_first={canonical_first})'"
            )
    # The colliding owners must stay distinct in a fresh process too: every
    # positive identity assertion above would still pass if two of these routes
    # had collapsed onto one object.
    for name, owner, other in _collision_pairs():
        lines.append(
            f"assert {owner}.{name} is not {other}.{name}, "
            f"'{owner}.{name} and {other}.{name} collapsed into one object "
            f"(canonical_first={canonical_first})'"
        )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "canonical_first", [True, False], ids=["canonical-first", "facade-first"]
)
def test_fresh_process_import_order_preserves_identity(canonical_first: bool) -> None:
    _run_isolated(_fresh_process_identity_script(canonical_first=canonical_first))
