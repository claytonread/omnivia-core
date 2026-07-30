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
``app_shell_bridge``, ``component_contract``, ``control_plane``, ``knowledge``,
``module_manifest``, and ``run_ledger`` barrels become identity-preserving purely
transitively, through their converted leaves (two each, except ``control_plane``'s
and ``knowledge``'s three).

``memory_graph`` is the second converted leaf set whose barrel is *not* one of
those -- ``memory``, converted before it, was the first -- and it is a
**hybrid**. Thirty-one of its thirty-eight exports now hop through its four
converted children to canonical objects, while the other seven are owned by the
runtime-only ``ingestion_adapter``/``store`` leaves that never enter Core. Its
legacy and canonical ``__all__`` are therefore different sizes, so it is
deliberately excluded from ``BARREL_ALL_ORDER`` and every gate built on it, and
gets its own section at the end of this module instead.

``graph`` is the third hybrid barrel, and its ``search_models`` leaf is the first
**split** facade: it routes its whole portable namespace to the canonical objects
like any other facade, but additionally keeps the four relevance-scoring helpers
Core deliberately excludes defined locally, because the unconverted, legacy-owned
``omnivia_memory.graph.search_service`` still calls them. Its body is therefore
not a *single* import, so it is held out of ``LEAF_SYMBOL_SOURCES`` -- and every
gate keyed on it -- and declared in ``SPLIT_LEAF_SYMBOL_SOURCES`` /
``SPLIT_LEAF_RETAINED_HELPERS`` instead. Both halves get their own gates in the
``graph`` section of this module.

``ingestion`` is the fourth and fifth hybrid barrel, and the first pair of them
in one domain: ``ingestion.models`` and ``ingestion.watcher.models`` are plain
direct facades, but fourteen of the ``ingestion`` barrel's nineteen exports and
two of the ``ingestion.watcher`` barrel's twelve are owned by runtime-only leaves
that never enter Core. Both barrels therefore stay out of ``BARREL_ALL_ORDER``
too, and get their own section at the end of this module -- together with the two
name collisions that section exists to keep separate: ``Source`` (ingestion vs.
provenance, where the legacy root binds the provenance one) and
``SourceReference`` (the watcher models record vs. the distinct dataclass the
runtime-only ``watcher.tracker`` defines for itself).

``workspace`` is a hybrid barrel too, and its leaf is the last of all:
``workspace.models`` is a plain direct facade, but two of its barrel's seven
exports (``WorkspaceRepository`` and ``WorkspaceService``) are owned by the
runtime-only ``repository``/``service`` leaves, which reach SQLite and the
ingestion pipeline and never enter Core. That barrel therefore stays out of
``BARREL_ALL_ORDER`` too and gets its own section at the end of this module. It
is the sixth and last of the six barrels the registry records as
``hybrid_facade`` -- ``graph``, ``ingestion``, ``ingestion.watcher``,
``memory``, ``memory_graph``, ``workspace``. Those six are gated together in a
final batch section of their own, after the per-domain ones.

Unlike every other hybrid here it brings no *cross-domain* name collision with
it: no other domain owns a distinct contract under any of its five routed
names, and neither package root re-exports any of the barrel's seven exports.
Those five names are of course rebound -- by the two ``workspace`` barrels and
by the runtime ``repository``/``service`` consumers -- but every one of those
bindings is the routed canonical object itself, which is what the gates below
pin. The legacy root's ``WorkspaceRef`` is a separate control-plane contract
(``omnivia_core.control_plane.models.WorkspaceRef``), not one of these names.

This module is the dedicated verification for that transition, independent
of the ``tests/canonical_migration`` source-parity gates (which exclude every
converted leaf via ``FACADE_CANONICAL_TO_LEGACY`` -- see
``tests/canonical_migration/_leaves.py`` and
``tests/canonical_migration/test_parity.py``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from baseline.facade_manifest import (
    MigrationState,
    canonical_imports,
    hybrid_facade_defects,
    load_manifest,
    split_facade_defects,
)
from baseline.inventory import (
    FACADE_DESCRIPTOR_REWRITES,
    FACADE_ROOT_BINDING_OWNER_MOVES,
    FACADE_ROUTES,
    describe_symbol,
)

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


_LEAVES_MANIFEST = _load_leaves_manifest()
FACADE_CANONICAL_TO_LEGACY = _LEAVES_MANIFEST.FACADE_CANONICAL_TO_LEGACY
SPLIT_FACADE_CANONICAL_TO_LEGACY = _LEAVES_MANIFEST.SPLIT_FACADE_CANONICAL_TO_LEGACY
SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL = (
    _LEAVES_MANIFEST.SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL
)
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
    "omnivia_memory.control_plane.imports": {
        "Any": "omnivia_core.control_plane.imports",
        # The ten contract classes the canonical imports leaf imports from its
        # sibling ``models`` to build candidate resources. They are part of this
        # leaf's own historical module-scope namespace too, so their identity is
        # checked against their real owner rather than against this leaf.
        "Capability": "omnivia_core.control_plane.models",
        "CapabilityType": "omnivia_core.control_plane.models",
        "CatalogueArtifactVerification": "omnivia_core.control_plane.imports",
        "Connection": "omnivia_core.control_plane.models",
        "ConnectionKind": "omnivia_core.control_plane.models",
        "ImportRecord": "omnivia_core.control_plane.models",
        "ImportSourceChange": "omnivia_core.control_plane.imports",
        "ImportSourceProtocol": "omnivia_core.control_plane.models",
        "ImportSpecValidation": "omnivia_core.control_plane.imports",
        "ImportedCandidateSet": "omnivia_core.control_plane.imports",
        # The control plane's *own* registry lifecycle enum, not the lifecycle
        # domain's same-named class. See
        # ``test_control_plane_collision_names_keep_their_historical_owners``.
        "LifecycleState": "omnivia_core.control_plane.models",
        "SideEffect": "omnivia_core.control_plane.models",
        "Trigger": "omnivia_core.control_plane.models",
        "TriggerKind": "omnivia_core.control_plane.models",
        "annotations": "omnivia_core.control_plane.imports",
        "dataclass": "omnivia_core.control_plane.imports",
        "detect_import_source_change": "omnivia_core.control_plane.imports",
        "field": "omnivia_core.control_plane.imports",
        # Plain module bindings an ``import`` statement leaves behind. They were
        # importable from this leaf before conversion, so they still must be.
        "hashlib": "omnivia_core.control_plane.imports",
        "import_asyncapi_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_generated_candidates": "omnivia_core.control_plane.imports",
        "import_mcp_candidates": "omnivia_core.control_plane.imports",
        "import_openapi_candidates": "omnivia_core.control_plane.imports",
        "json": "omnivia_core.control_plane.imports",
        "re": "omnivia_core.control_plane.imports",
        "validate_asyncapi_import_spec": "omnivia_core.control_plane.imports",
        "validate_mcp_import_spec": "omnivia_core.control_plane.imports",
        "validate_openapi_import_spec": "omnivia_core.control_plane.imports",
        "verify_catalogue_artifacts": "omnivia_core.control_plane.imports",
    },
    "omnivia_memory.control_plane.models": {
        "Agent": "omnivia_core.control_plane.models",
        "Any": "omnivia_core.control_plane.models",
        "Approval": "omnivia_core.control_plane.models",
        "AuditEvent": "omnivia_core.control_plane.models",
        "Automation": "omnivia_core.control_plane.models",
        # A ``ContractVersion`` *instance*, built by (and owned by) the canonical
        # control-plane models leaf even though its type lives in the knowledge
        # domain. That split is why converting this leaf also moves one frozen
        # root binding -- see ``baseline.inventory``'s
        # ``FACADE_ROOT_BINDING_OWNER_MOVES``.
        "CONTROL_PLANE_CONTRACT_VERSION": "omnivia_core.control_plane.models",
        "CONTROL_PLANE_SCHEMA_VERSION": "omnivia_core.control_plane.models",
        "Capability": "omnivia_core.control_plane.models",
        "CapabilityType": "omnivia_core.control_plane.models",
        "Connection": "omnivia_core.control_plane.models",
        "ConnectionKind": "omnivia_core.control_plane.models",
        "ConsultantAccessGrant": "omnivia_core.control_plane.models",
        "ConsultantGrantStatus": "omnivia_core.control_plane.models",
        # The routed ``ContractVersion`` class is owned by the knowledge domain,
        # not by this leaf's canonical counterpart: the canonical control-plane
        # models module imports it to build the version constant above. Its
        # identity is therefore checked against its real owner.
        "ContractVersion": "omnivia_core.knowledge.models",
        "ControlPlaneManifest": "omnivia_core.control_plane.models",
        "ControlPlaneRunStatus": "omnivia_core.control_plane.models",
        "Enum": "omnivia_core.control_plane.models",
        "ExecutionMode": "omnivia_core.control_plane.models",
        "ExecutionResult": "omnivia_core.control_plane.models",
        "ImportRecord": "omnivia_core.control_plane.models",
        "ImportSourceProtocol": "omnivia_core.control_plane.models",
        # The control plane's own registry lifecycle enum, not the lifecycle
        # domain's same-named class -- and it is the one the legacy root binds.
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
        "SecretMetadata": "omnivia_core.control_plane.models",
        "SecretReference": "omnivia_core.control_plane.models",
        "SecretResolutionResult": "omnivia_core.control_plane.models",
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
        # Not the ``_shared.validation`` primitive, nor the App Manifest's / App
        # Shell bridge's / Component Contract's same-named class: the control
        # plane historically defined its own ``ValidationResult`` dataclass and
        # this leaf must keep routing to that one.
        "ValidationResult": "omnivia_core.control_plane.models",
        "WorkspaceRef": "omnivia_core.control_plane.models",
        "annotations": "omnivia_core.control_plane.models",
        "dataclass": "omnivia_core.control_plane.models",
        "field": "omnivia_core.control_plane.models",
    },
    "omnivia_memory.control_plane.validation": {
        "APPROVAL_ESCALATION_STATES": "omnivia_core.control_plane.validation",
        # The 41 contract names below are owned by this leaf's sibling ``models``,
        # which the canonical validator imports them from; they were bound at
        # this leaf's module scope historically too.
        "Agent": "omnivia_core.control_plane.models",
        "Any": "omnivia_core.control_plane.validation",
        "Approval": "omnivia_core.control_plane.models",
        "AuditEvent": "omnivia_core.control_plane.models",
        "Automation": "omnivia_core.control_plane.models",
        "CONTROL_PLANE_CONTRACT_VERSION": "omnivia_core.control_plane.models",
        "CONTROL_PLANE_SCHEMA_VERSION": "omnivia_core.control_plane.models",
        "Capability": "omnivia_core.control_plane.models",
        "CapabilityType": "omnivia_core.control_plane.models",
        "Connection": "omnivia_core.control_plane.models",
        "ConnectionKind": "omnivia_core.control_plane.models",
        "ConsultantAccessGrant": "omnivia_core.control_plane.models",
        "ConsultantGrantStatus": "omnivia_core.control_plane.models",
        "ControlPlaneManifest": "omnivia_core.control_plane.models",
        "ControlPlaneRunStatus": "omnivia_core.control_plane.models",
        "ControlPlaneValidationError": "omnivia_core.control_plane.validation",
        "DANGEROUS_SIDE_EFFECTS": "omnivia_core.control_plane.validation",
        "EXTRA_SENSITIVE_KEYS": "omnivia_core.control_plane.validation",
        "Enum": "omnivia_core.control_plane.validation",
        "ImportRecord": "omnivia_core.control_plane.models",
        "ImportSourceProtocol": "omnivia_core.control_plane.models",
        "LifecycleState": "omnivia_core.control_plane.models",
        "LocalApprovalNotification": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationChannel": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationEvent": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationStatus": "omnivia_core.control_plane.models",
        "Mapping": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_NUMERIC_OPERATORS": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_OPERATORS": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_PRESENCE_OPERATORS": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_SCOPES": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS": (
            "omnivia_core.control_plane.validation"
        ),
        "POLICY_ATTRIBUTE_VALUES_OPERATORS": "omnivia_core.control_plane.validation",
        "POLICY_ATTRIBUTE_VALUE_OPERATORS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_BOOLEAN_OPS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_COMPARISON_OPERATORS": (
            "omnivia_core.control_plane.validation"
        ),
        "POLICY_EXPRESSION_MAX_DEPTH": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_MAX_NODES": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_NUMERIC_COMPARISONS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_OPS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_PARSE_MAX_DEPTH": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_RAW_FIELDS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_SECRET_MARKERS": "omnivia_core.control_plane.validation",
        "POLICY_EXPRESSION_SOURCE_MAX_LENGTH": "omnivia_core.control_plane.validation",
        "Policy": "omnivia_core.control_plane.models",
        "PolicyAttributeCondition": "omnivia_core.control_plane.models",
        "PolicyAttributeExpression": "omnivia_core.control_plane.models",
        "PolicyDecision": "omnivia_core.control_plane.models",
        "PolicyRulePack": "omnivia_core.control_plane.models",
        "PolicyTemplate": "omnivia_core.control_plane.models",
        "RRULE_FREQUENCIES": "omnivia_core.control_plane.validation",
        "RRULE_WEEKDAYS": "omnivia_core.control_plane.validation",
        "RunMode": "omnivia_core.control_plane.models",
        "RunRecord": "omnivia_core.control_plane.models",
        "STABLE_ID_PATTERN": "omnivia_core.control_plane.validation",
        "SecretMetadata": "omnivia_core.control_plane.models",
        "SecretReference": "omnivia_core.control_plane.models",
        "SecretStorageScope": "omnivia_core.control_plane.models",
        "SideEffect": "omnivia_core.control_plane.models",
        "SyncConflictStrategy": "omnivia_core.control_plane.models",
        "SyncDirection": "omnivia_core.control_plane.models",
        "SyncRule": "omnivia_core.control_plane.models",
        # A frozen ``TypeVar`` definition this leaf owns. The barrel above it
        # never re-exported it, but it has always been importable from here.
        "T": "omnivia_core.control_plane.validation",
        "TenantIsolationRule": "omnivia_core.control_plane.models",
        "Trigger": "omnivia_core.control_plane.models",
        "TriggerKind": "omnivia_core.control_plane.models",
        "TypeVar": "omnivia_core.control_plane.validation",
        # The control plane's own dataclass, which its sibling ``models`` owns --
        # deliberately not the shared primitive the run-ledger validator routes
        # to, nor any of the other three domain classes of the same name.
        "ValidationResult": "omnivia_core.control_plane.models",
        "WorkspaceRef": "omnivia_core.control_plane.models",
        "annotations": "omnivia_core.control_plane.validation",
        # Owned by the knowledge domain's validation leaf, which the canonical
        # control-plane validator imports it from.
        "check_contract_version_compatibility": "omnivia_core.knowledge.validation",
        "compile_policy_expression": "omnivia_core.control_plane.validation",
        "datetime": "omnivia_core.control_plane.validation",
        "manifest_from_dict": "omnivia_core.control_plane.validation",
        "re": "omnivia_core.control_plane.validation",
        # Owned by the shared validation primitive, which the canonical
        # control-plane validator imports it from.
        "scan_sensitive_fields": "omnivia_core._shared.validation",
        "validate_control_plane_manifest": "omnivia_core.control_plane.validation",
    },
    # Every name in this leaf's historical namespace resolves from its canonical
    # counterpart, incidental bindings included: the knowledge models leaf imports
    # nothing from another Core leaf.
    # Every name in this leaf's historical namespace resolves from its canonical
    # counterpart, incidental bindings and the plain ``uuid`` module binding
    # included: the graph models leaf imports nothing from another Core leaf. Its
    # sibling ``search_models`` is a *split* facade and lives in
    # ``SPLIT_LEAF_SYMBOL_SOURCES`` instead; their barrel stays a hybrid.
    "omnivia_memory.graph.models": {
        "Any": "omnivia_core.graph.models",
        "ApprovalStatus": "omnivia_core.graph.models",
        "Entity": "omnivia_core.graph.models",
        "EntityCreate": "omnivia_core.graph.models",
        "EntityMemoryLink": "omnivia_core.graph.models",
        "EntityType": "omnivia_core.graph.models",
        "Enum": "omnivia_core.graph.models",
        "Relationship": "omnivia_core.graph.models",
        "RelationshipCreate": "omnivia_core.graph.models",
        "RelationshipType": "omnivia_core.graph.models",
        "annotations": "omnivia_core.graph.models",
        "dataclass": "omnivia_core.graph.models",
        "datetime": "omnivia_core.graph.models",
        "field": "omnivia_core.graph.models",
        "timezone": "omnivia_core.graph.models",
        "uuid": "omnivia_core.graph.models",
    },
    # Every name in this leaf's historical namespace resolves from its canonical
    # counterpart, incidental bindings and the plain ``enum``/``hashlib``/``uuid``
    # module bindings included: the ingestion models leaf imports nothing from
    # another Core leaf. ``IngestSource`` is its own ``Source`` under a second
    # name, so both must land on the one canonical dataclass; ``Source`` itself
    # deliberately collides with the provenance domain's record, and the legacy
    # root binds *that* one. Its barrel stays a hybrid.
    "omnivia_memory.ingestion.models": {
        "Any": "omnivia_core.ingestion.models",
        "Chunk": "omnivia_core.ingestion.models",
        "ExtractionResult": "omnivia_core.ingestion.models",
        "FileInventory": "omnivia_core.ingestion.models",
        "FileType": "omnivia_core.ingestion.models",
        "IngestSource": "omnivia_core.ingestion.models",
        "ParseStatus": "omnivia_core.ingestion.models",
        "Path": "omnivia_core.ingestion.models",
        "Source": "omnivia_core.ingestion.models",
        "TYPE_CHECKING": "omnivia_core.ingestion.models",
        "annotations": "omnivia_core.ingestion.models",
        "dataclass": "omnivia_core.ingestion.models",
        "datetime": "omnivia_core.ingestion.models",
        "enum": "omnivia_core.ingestion.models",
        "field": "omnivia_core.ingestion.models",
        "hashlib": "omnivia_core.ingestion.models",
        "timezone": "omnivia_core.ingestion.models",
        "uuid": "omnivia_core.ingestion.models",
    },
    # The same, for the watcher models leaf: nothing here comes from another Core
    # leaf. Its ``SourceReference`` deliberately collides with the runtime-only
    # ``ingestion.watcher.tracker``'s own dataclass of that name, which is not
    # routed and stays legacy-owned. Its barrel stays a hybrid too.
    "omnivia_memory.ingestion.watcher.models": {
        "DebounceConfig": "omnivia_core.ingestion.watcher.models",
        "FileChange": "omnivia_core.ingestion.watcher.models",
        "FileChangeBatch": "omnivia_core.ingestion.watcher.models",
        "FileChangeType": "omnivia_core.ingestion.watcher.models",
        "IndexerScheduler": "omnivia_core.ingestion.watcher.models",
        "IndexerState": "omnivia_core.ingestion.watcher.models",
        "IndexerStatus": "omnivia_core.ingestion.watcher.models",
        "ScheduledJob": "omnivia_core.ingestion.watcher.models",
        "SourceReference": "omnivia_core.ingestion.watcher.models",
        "TYPE_CHECKING": "omnivia_core.ingestion.watcher.models",
        "WatchedPath": "omnivia_core.ingestion.watcher.models",
        "annotations": "omnivia_core.ingestion.watcher.models",
        "dataclass": "omnivia_core.ingestion.watcher.models",
        "datetime": "omnivia_core.ingestion.watcher.models",
        "enum": "omnivia_core.ingestion.watcher.models",
        "field": "omnivia_core.ingestion.watcher.models",
        "timezone": "omnivia_core.ingestion.watcher.models",
        "uuid": "omnivia_core.ingestion.watcher.models",
    },
    "omnivia_memory.knowledge.models": {
        "AgentGraphContext": "omnivia_core.knowledge.models",
        "Any": "omnivia_core.knowledge.models",
        "BUILTIN_GRAPH_NODE_KINDS": "omnivia_core.knowledge.models",
        "BUILTIN_GRAPH_RELATIONS": "omnivia_core.knowledge.models",
        "BUILTIN_OBJECT_KINDS": "omnivia_core.knowledge.models",
        "ContractVersion": "omnivia_core.knowledge.models",
        "EXTENSION_MANIFEST_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "Enum": "omnivia_core.knowledge.models",
        "GRAPH_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "GraphConfidence": "omnivia_core.knowledge.models",
        "GraphEdge": "omnivia_core.knowledge.models",
        "GraphEvidenceStrength": "omnivia_core.knowledge.models",
        "GraphFragment": "omnivia_core.knowledge.models",
        "GraphNode": "omnivia_core.knowledge.models",
        "GraphOrigin": "omnivia_core.knowledge.models",
        "GraphReviewStatus": "omnivia_core.knowledge.models",
        "GraphSensitivity": "omnivia_core.knowledge.models",
        "GraphSourceType": "omnivia_core.knowledge.models",
        "GraphVisibility": "omnivia_core.knowledge.models",
        "KNOWLEDGE_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "KnowledgeClaim": "omnivia_core.knowledge.models",
        "KnowledgeCollection": "omnivia_core.knowledge.models",
        "KnowledgeExtensionManifest": "omnivia_core.knowledge.models",
        "KnowledgeLink": "omnivia_core.knowledge.models",
        "KnowledgeObject": "omnivia_core.knowledge.models",
        "KnowledgeSource": "omnivia_core.knowledge.models",
        "KnowledgeSpace": "omnivia_core.knowledge.models",
        "SourceRef": "omnivia_core.knowledge.models",
        "annotations": "omnivia_core.knowledge.models",
        "dataclass": "omnivia_core.knowledge.models",
        "field": "omnivia_core.knowledge.models",
    },
    "omnivia_memory.knowledge.normalize": {
        # The three bounded-vocabulary frozensets the canonical normalizer imports
        # from its sibling ``models`` to validate against. They are part of this
        # leaf's own historical module-scope namespace too, so their identity is
        # checked against their real owner rather than against this leaf.
        "BUILTIN_GRAPH_NODE_KINDS": "omnivia_core.knowledge.models",
        "BUILTIN_GRAPH_RELATIONS": "omnivia_core.knowledge.models",
        "BUILTIN_OBJECT_KINDS": "omnivia_core.knowledge.models",
        "PurePosixPath": "omnivia_core.knowledge.normalize",
        "annotations": "omnivia_core.knowledge.normalize",
        "normalize_extension_value": "omnivia_core.knowledge.normalize",
        "normalize_graph_edge_id": "omnivia_core.knowledge.normalize",
        "normalize_graph_node_id": "omnivia_core.knowledge.normalize",
        "normalize_graph_node_kind": "omnivia_core.knowledge.normalize",
        "normalize_graph_relation": "omnivia_core.knowledge.normalize",
        "normalize_identifier": "omnivia_core.knowledge.normalize",
        "normalize_label": "omnivia_core.knowledge.normalize",
        "normalize_object_id": "omnivia_core.knowledge.normalize",
        "normalize_object_kind": "omnivia_core.knowledge.normalize",
        "normalize_source_path": "omnivia_core.knowledge.normalize",
        "normalize_space_id": "omnivia_core.knowledge.normalize",
        "normalize_tags": "omnivia_core.knowledge.normalize",
        # Plain module bindings an ``import`` statement leaves behind. They were
        # importable from this leaf before conversion, so they still must be.
        "re": "omnivia_core.knowledge.normalize",
        "unicodedata": "omnivia_core.knowledge.normalize",
    },
    "omnivia_memory.knowledge.validation": {
        # Twenty-three of the names in this dict are owned by this leaf's sibling
        # ``models``, which the canonical validator imports them from; they were
        # bound at this leaf's module scope historically too. They are interleaved
        # below in sorted order rather than grouped.
        "AgentGraphContext": "omnivia_core.knowledge.models",
        "Any": "omnivia_core.knowledge.validation",
        "BUILTIN_GRAPH_NODE_KINDS": "omnivia_core.knowledge.models",
        "BUILTIN_GRAPH_RELATIONS": "omnivia_core.knowledge.models",
        "BUILTIN_OBJECT_KINDS": "omnivia_core.knowledge.models",
        "ContractVersion": "omnivia_core.knowledge.models",
        "EXTENSION_MANIFEST_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "GRAPH_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "GraphConfidence": "omnivia_core.knowledge.models",
        "GraphEdge": "omnivia_core.knowledge.models",
        "GraphEvidenceStrength": "omnivia_core.knowledge.models",
        "GraphFragment": "omnivia_core.knowledge.models",
        "GraphNode": "omnivia_core.knowledge.models",
        "GraphReviewStatus": "omnivia_core.knowledge.models",
        "GraphSensitivity": "omnivia_core.knowledge.models",
        "KNOWLEDGE_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "KnowledgeClaim": "omnivia_core.knowledge.models",
        "KnowledgeCollection": "omnivia_core.knowledge.models",
        "KnowledgeExtensionManifest": "omnivia_core.knowledge.models",
        "KnowledgeLink": "omnivia_core.knowledge.models",
        "KnowledgeObject": "omnivia_core.knowledge.models",
        "KnowledgeSource": "omnivia_core.knowledge.models",
        "KnowledgeSpace": "omnivia_core.knowledge.models",
        "MAX_LABEL_LENGTH": "omnivia_core.knowledge.validation",
        "MAX_QUOTE_PREVIEW_LENGTH": "omnivia_core.knowledge.validation",
        "SCRIPT_LIKE_MARKERS": "omnivia_core.knowledge.validation",
        "SourceRef": "omnivia_core.knowledge.models",
        # This leaf never had a ``ValidationResult`` of its own: it historically
        # imported the shared primitive, so it must keep routing to that one and
        # not to any of the four domain classes of the same name. It is also the
        # binding the legacy *root* takes its ``ValidationResult`` from, through
        # the knowledge barrel's re-export of it. See
        # ``test_knowledge_validation_result_keeps_its_historical_collision_owner``.
        "ValidationResult": "omnivia_core._shared.validation",
        "annotations": "omnivia_core.knowledge.validation",
        "check_contract_version_compatibility": "omnivia_core.knowledge.validation",
        # The nine normalizers the canonical validator imports from its sibling
        # ``normalize``. ``normalize_extension_value`` is one of them even though
        # the barrel above never re-exported it.
        "normalize_extension_value": "omnivia_core.knowledge.normalize",
        "normalize_graph_edge_id": "omnivia_core.knowledge.normalize",
        "normalize_graph_node_id": "omnivia_core.knowledge.normalize",
        "normalize_identifier": "omnivia_core.knowledge.normalize",
        "normalize_label": "omnivia_core.knowledge.normalize",
        "normalize_object_id": "omnivia_core.knowledge.normalize",
        "normalize_source_path": "omnivia_core.knowledge.normalize",
        "normalize_space_id": "omnivia_core.knowledge.normalize",
        "normalize_tags": "omnivia_core.knowledge.normalize",
        # Owned by the shared validation primitive, which the canonical knowledge
        # validator imports it from.
        "scan_sensitive_fields": "omnivia_core._shared.validation",
        "summarize_confidence": "omnivia_core.knowledge.validation",
        "summarize_review_status": "omnivia_core.knowledge.validation",
        "summarize_sensitivity": "omnivia_core.knowledge.validation",
        "validate_agent_graph_context": "omnivia_core.knowledge.validation",
        "validate_graph_edge": "omnivia_core.knowledge.validation",
        "validate_graph_fragment": "omnivia_core.knowledge.validation",
        "validate_graph_node": "omnivia_core.knowledge.validation",
        "validate_knowledge_claim": "omnivia_core.knowledge.validation",
        "validate_knowledge_collection": "omnivia_core.knowledge.validation",
        "validate_knowledge_extension_manifest": "omnivia_core.knowledge.validation",
        "validate_knowledge_link": "omnivia_core.knowledge.validation",
        "validate_knowledge_object": "omnivia_core.knowledge.validation",
        "validate_knowledge_source": "omnivia_core.knowledge.validation",
        "validate_knowledge_space": "omnivia_core.knowledge.validation",
        "validate_source_ref": "omnivia_core.knowledge.validation",
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
    "omnivia_memory.memory_graph.assembly": {
        # The thirteen contract names below are owned by this leaf's sibling
        # ``models``, which the canonical assembler imports them from; they were
        # bound at this leaf's module scope historically too.
        "EvidenceGraphResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewEdge": "omnivia_core.memory_graph.models",
        "GraphPreviewKind": "omnivia_core.memory_graph.models",
        "GraphPreviewNode": "omnivia_core.memory_graph.models",
        "GraphPreviewResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewState": "omnivia_core.memory_graph.models",
        "MemoryEntity": "omnivia_core.memory_graph.models",
        "MemoryFact": "omnivia_core.memory_graph.models",
        "MemoryFactStatus": "omnivia_core.memory_graph.models",
        "MemorySegment": "omnivia_core.memory_graph.models",
        "MemorySource": "omnivia_core.memory_graph.models",
        "MemorySourceStatus": "omnivia_core.memory_graph.models",
        # The memory graph's own evidence reference, not the knowledge domain's
        # same-named class. See
        # ``test_memory_graph_source_ref_keeps_its_historical_collision_owner``.
        "SourceRef": "omnivia_core.memory_graph.models",
        "annotations": "omnivia_core.memory_graph.assembly",
        "assemble_evidence_graph": "omnivia_core.memory_graph.assembly",
        "assemble_graph_preview": "omnivia_core.memory_graph.assembly",
        "redact_segment_preview": "omnivia_core.memory_graph.assembly",
    },
    "omnivia_memory.memory_graph.fixtures": {
        "EvidenceGraphResponse": "omnivia_core.memory_graph.models",
        # A bare ``str`` constant, so the frozen inventory's ``defines``
        # provenance omits it; its owner is pinned here and routed explicitly in
        # ``baseline.inventory``'s ``FACADE_ROUTES``.
        "FIXTURE_TIME": "omnivia_core.memory_graph.fixtures",
        "GraphPreviewEdge": "omnivia_core.memory_graph.models",
        "GraphPreviewKind": "omnivia_core.memory_graph.models",
        "GraphPreviewNode": "omnivia_core.memory_graph.models",
        "GraphPreviewResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewState": "omnivia_core.memory_graph.models",
        "MemoryEntity": "omnivia_core.memory_graph.models",
        "MemoryFact": "omnivia_core.memory_graph.models",
        "MemoryFactStatus": "omnivia_core.memory_graph.models",
        "MemoryGraphFixture": "omnivia_core.memory_graph.fixtures",
        "MemorySegment": "omnivia_core.memory_graph.models",
        "MemorySegmentKind": "omnivia_core.memory_graph.models",
        "MemorySource": "omnivia_core.memory_graph.models",
        "MemorySourceFreshness": "omnivia_core.memory_graph.models",
        "MemorySourceStatus": "omnivia_core.memory_graph.models",
        "MemorySourceType": "omnivia_core.memory_graph.models",
        "SourceRef": "omnivia_core.memory_graph.models",
        "TypedDict": "omnivia_core.memory_graph.fixtures",
        "annotations": "omnivia_core.memory_graph.fixtures",
        "build_memory_graph_fixture": "omnivia_core.memory_graph.fixtures",
    },
    # Every name in this leaf's historical namespace resolves from its canonical
    # counterpart, incidental bindings included: the memory graph models leaf
    # imports nothing from another Core leaf.
    "omnivia_memory.memory_graph.models": {
        "Any": "omnivia_core.memory_graph.models",
        # A ``types.UnionType`` alias (``float | str``), so its runtime
        # ``__module__`` is not this leaf and the frozen inventory's ``defines``
        # provenance omits it. It is a public type alias downstream code
        # annotates with, so it is routed explicitly and pinned here.
        "Confidence": "omnivia_core.memory_graph.models",
        "Enum": "omnivia_core.memory_graph.models",
        "EvidenceGraphResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewEdge": "omnivia_core.memory_graph.models",
        "GraphPreviewKind": "omnivia_core.memory_graph.models",
        "GraphPreviewNode": "omnivia_core.memory_graph.models",
        "GraphPreviewResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewState": "omnivia_core.memory_graph.models",
        "MemoryEntity": "omnivia_core.memory_graph.models",
        "MemoryFact": "omnivia_core.memory_graph.models",
        "MemoryFactStatus": "omnivia_core.memory_graph.models",
        "MemorySegment": "omnivia_core.memory_graph.models",
        "MemorySegmentKind": "omnivia_core.memory_graph.models",
        "MemorySource": "omnivia_core.memory_graph.models",
        "MemorySourceFreshness": "omnivia_core.memory_graph.models",
        "MemorySourceStatus": "omnivia_core.memory_graph.models",
        "MemorySourceType": "omnivia_core.memory_graph.models",
        "RetrievalTrace": "omnivia_core.memory_graph.models",
        # The memory graph's own evidence reference, not the knowledge domain's
        # same-named class -- and the legacy root binds the knowledge one, so this
        # leaf's route moves no root binding for it.
        "SourceRef": "omnivia_core.memory_graph.models",
        "TypeAlias": "omnivia_core.memory_graph.models",
        "annotations": "omnivia_core.memory_graph.models",
        "dataclass": "omnivia_core.memory_graph.models",
        "field": "omnivia_core.memory_graph.models",
    },
    "omnivia_memory.memory_graph.validation": {
        # A builtin ``frozenset`` instance, so ordinary ``__module__`` definition
        # detection cannot see it; routed explicitly and pinned here.
        "CONFIDENCE_BUCKETS": "omnivia_core.memory_graph.validation",
        # The ``Confidence`` alias and the ten contract names below are owned by
        # this leaf's sibling ``models``, which the canonical validator imports
        # them from; they were bound at this leaf's module scope historically too.
        "Confidence": "omnivia_core.memory_graph.models",
        "EvidenceGraphResponse": "omnivia_core.memory_graph.models",
        "GraphPreviewEdge": "omnivia_core.memory_graph.models",
        "GraphPreviewNode": "omnivia_core.memory_graph.models",
        "GraphPreviewResponse": "omnivia_core.memory_graph.models",
        "MemoryEntity": "omnivia_core.memory_graph.models",
        "MemoryFact": "omnivia_core.memory_graph.models",
        "MemorySegment": "omnivia_core.memory_graph.models",
        "MemorySource": "omnivia_core.memory_graph.models",
        "SourceRef": "omnivia_core.memory_graph.models",
        # This leaf never had a ``ValidationResult`` of its own: it historically
        # imported the shared primitive, so it must keep routing to that one and
        # not to any of the four domain classes of the same name. Both the
        # runtime-only ``memory_graph.store`` leaf and the hybrid ``memory_graph``
        # barrel take their ``ValidationResult`` from here. See
        # ``test_memory_graph_validation_result_keeps_its_historical_collision_owner``.
        "ValidationResult": "omnivia_core._shared.validation",
        "annotations": "omnivia_core.memory_graph.validation",
        "validate_evidence_graph_response": "omnivia_core.memory_graph.validation",
        "validate_graph_preview_response": "omnivia_core.memory_graph.validation",
        "validate_memory_entity": "omnivia_core.memory_graph.validation",
        "validate_memory_fact": "omnivia_core.memory_graph.validation",
        "validate_memory_segment": "omnivia_core.memory_graph.validation",
        "validate_memory_source": "omnivia_core.memory_graph.validation",
    },
    # Every name in this leaf's historical namespace resolves from its canonical
    # counterpart, incidental bindings and the plain ``uuid`` module binding
    # included: the workspace models leaf imports nothing from another Core leaf.
    # None of its five owned names collides with another domain's contract, and
    # neither package root has ever bound one of them. Its barrel stays a hybrid:
    # ``WorkspaceRepository`` and ``WorkspaceService`` are owned by the
    # runtime-only ``repository``/``service`` leaves.
    "omnivia_memory.workspace.models": {
        "Any": "omnivia_core.workspace.models",
        "Enum": "omnivia_core.workspace.models",
        "ImportSummary": "omnivia_core.workspace.models",
        "Path": "omnivia_core.workspace.models",
        "Workspace": "omnivia_core.workspace.models",
        "WorkspaceCreate": "omnivia_core.workspace.models",
        "WorkspaceIndexStatus": "omnivia_core.workspace.models",
        "WorkspaceUpdate": "omnivia_core.workspace.models",
        "annotations": "omnivia_core.workspace.models",
        "dataclass": "omnivia_core.workspace.models",
        "datetime": "omnivia_core.workspace.models",
        "field": "omnivia_core.workspace.models",
        "timezone": "omnivia_core.workspace.models",
        "uuid": "omnivia_core.workspace.models",
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
    "omnivia_memory.control_plane.imports": "omnivia_core.control_plane.imports",
    "omnivia_memory.control_plane.models": "omnivia_core.control_plane.models",
    "omnivia_memory.control_plane.validation": (
        "omnivia_core.control_plane.validation"
    ),
    "omnivia_memory.graph.models": "omnivia_core.graph.models",
    "omnivia_memory.ingestion.models": "omnivia_core.ingestion.models",
    "omnivia_memory.ingestion.watcher.models": (
        "omnivia_core.ingestion.watcher.models"
    ),
    "omnivia_memory.knowledge.models": "omnivia_core.knowledge.models",
    "omnivia_memory.knowledge.normalize": "omnivia_core.knowledge.normalize",
    "omnivia_memory.knowledge.validation": "omnivia_core.knowledge.validation",
    "omnivia_memory.lifecycle.models": "omnivia_core.lifecycle.models",
    "omnivia_memory.lifecycle.rules": "omnivia_core.lifecycle.rules",
    "omnivia_memory.module_manifest.models": "omnivia_core.module_manifest.models",
    "omnivia_memory.module_manifest.validation": (
        "omnivia_core.module_manifest.validation"
    ),
    "omnivia_memory.provenance.models": "omnivia_core.provenance.models",
    "omnivia_memory.memory.models": "omnivia_core.memory.models",
    "omnivia_memory.memory_graph.assembly": "omnivia_core.memory_graph.assembly",
    "omnivia_memory.memory_graph.fixtures": "omnivia_core.memory_graph.fixtures",
    "omnivia_memory.memory_graph.models": "omnivia_core.memory_graph.models",
    "omnivia_memory.memory_graph.validation": (
        "omnivia_core.memory_graph.validation"
    ),
    "omnivia_memory.run_ledger.models": "omnivia_core.run_ledger.models",
    "omnivia_memory.run_ledger.validation": "omnivia_core.run_ledger.validation",
    "omnivia_memory.workspace.models": "omnivia_core.workspace.models",
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
    "control_plane": [
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
    ],
    "knowledge": [
        "AgentGraphContext",
        "BUILTIN_GRAPH_NODE_KINDS",
        "BUILTIN_GRAPH_RELATIONS",
        "BUILTIN_OBJECT_KINDS",
        "ContractVersion",
        "EXTENSION_MANIFEST_CONTRACT_VERSION",
        "GRAPH_CONTRACT_VERSION",
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
        "KNOWLEDGE_CONTRACT_VERSION",
        "KnowledgeClaim",
        "KnowledgeCollection",
        "KnowledgeExtensionManifest",
        "KnowledgeLink",
        "KnowledgeObject",
        "KnowledgeSource",
        "KnowledgeSpace",
        "SourceRef",
        "ValidationResult",
        "check_contract_version_compatibility",
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
    "control_plane",
    "knowledge",
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

#: The same, for the unchanged legacy control-plane barrel -- the first barrel in
#: this set with *three* converted children rather than two, and the only one
#: whose ``__all__`` is neither alphabetized nor a concatenation of its import
#: blocks: it leads with the three constants, then interleaves each child's names
#: in its own historical order. Both the block order/name order below and that
#: ``__all__`` order are restated exactly rather than derived.
CONTROL_PLANE_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.control_plane.imports",
        (
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
        ),
    ),
    (
        "omnivia_memory.control_plane.models",
        (
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
        ),
    ),
    (
        "omnivia_memory.control_plane.validation",
        (
            "DANGEROUS_SIDE_EFFECTS",
            "ControlPlaneValidationError",
            "compile_policy_expression",
            "manifest_from_dict",
            "validate_control_plane_manifest",
        ),
    ),
)

#: The same, for the unchanged legacy knowledge barrel -- the second barrel in
#: this set with *three* converted children. Its ``__all__`` is fully sorted,
#: unlike the control-plane barrel's interleaved literal, and it deliberately
#: publishes a *subset* of its children's routed surface: the normalize block
#: below never named ``normalize_extension_value``, which is a routed symbol of
#: that leaf. Its name order is its own historical source order -- constants
#: leading the models block ahead of the classes rather than being alphabetized.
#:
#: ``ValidationResult`` leads the validation block: the knowledge validation leaf
#: never owned a class of that name, so this is the one export whose object comes
#: from outside the knowledge domain -- and it is the binding the legacy package
#: root has always taken its ``ValidationResult`` from.
KNOWLEDGE_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.knowledge.models",
        (
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
        ),
    ),
    (
        "omnivia_memory.knowledge.normalize",
        (
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
        ),
    ),
    (
        "omnivia_memory.knowledge.validation",
        (
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
    "control_plane": CONTROL_PLANE_BARREL_ABSOLUTE_IMPORTS,
    "knowledge": KNOWLEDGE_BARREL_ABSOLUTE_IMPORTS,
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
    "omnivia_core.control_plane.imports": "omnivia_memory.control_plane.imports",
    "omnivia_core.control_plane.models": "omnivia_memory.control_plane.models",
    "omnivia_core.control_plane.validation": (
        "omnivia_memory.control_plane.validation"
    ),
    "omnivia_core.graph.models": "omnivia_memory.graph.models",
    "omnivia_core.ingestion.models": "omnivia_memory.ingestion.models",
    "omnivia_core.ingestion.watcher.models": (
        "omnivia_memory.ingestion.watcher.models"
    ),
    "omnivia_core.knowledge.models": "omnivia_memory.knowledge.models",
    "omnivia_core.knowledge.normalize": "omnivia_memory.knowledge.normalize",
    "omnivia_core.knowledge.validation": "omnivia_memory.knowledge.validation",
    "omnivia_core.lifecycle.models": "omnivia_memory.lifecycle.models",
    "omnivia_core.lifecycle.rules": "omnivia_memory.lifecycle.rules",
    "omnivia_core.module_manifest.models": "omnivia_memory.module_manifest.models",
    "omnivia_core.module_manifest.validation": (
        "omnivia_memory.module_manifest.validation"
    ),
    "omnivia_core.provenance.models": "omnivia_memory.provenance.models",
    "omnivia_core.memory.models": "omnivia_memory.memory.models",
    "omnivia_core.memory_graph.assembly": "omnivia_memory.memory_graph.assembly",
    "omnivia_core.memory_graph.fixtures": "omnivia_memory.memory_graph.fixtures",
    "omnivia_core.memory_graph.models": "omnivia_memory.memory_graph.models",
    "omnivia_core.memory_graph.validation": (
        "omnivia_memory.memory_graph.validation"
    ),
    "omnivia_core.run_ledger.models": "omnivia_memory.run_ledger.models",
    "omnivia_core.run_ledger.validation": "omnivia_memory.run_ledger.validation",
    "omnivia_core.workspace.models": "omnivia_memory.workspace.models",
}

#: The same, for the leaves converted into a *split* facade. Declared
#: independently of ``_leaves.py``'s ``SPLIT_FACADE_CANONICAL_TO_LEGACY`` so this
#: module also catches that manifest drifting, and kept separate from the map
#: above because the two carry different source policies: a plain facade's whole
#: body is one import, a split facade additionally keeps a named set of
#: legacy-owned definitions.
EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core.graph.search_models": "omnivia_memory.graph.search_models",
}

#: For each split leaf, the *portable* half of its historical module-scope
#: namespace: the names that must now be the exact canonical objects, and the
#: canonical module that owns each. The retained half is
#: ``SPLIT_LEAF_RETAINED_HELPERS`` below; together the two must be exactly what a
#: star import of the leaf exposes, which is what proves neither enumeration has
#: quietly lost or gained a name.
#:
#: ``Entity``, ``EntityType`` and ``RelationshipType`` are owned by the sibling
#: ``omnivia_core.graph.models`` leaf, which the canonical records import them
#: from; they were bound at this leaf's module scope historically too.
#: ``annotations`` is the one name here whose owner is *not* a canonical module:
#: see ``SPLIT_LEAF_FUTURE_BINDING``.
SPLIT_LEAF_SYMBOL_SOURCES: dict[str, dict[str, str]] = {
    "omnivia_memory.graph.search_models": {
        "Any": "omnivia_core.graph.search_models",
        "Entity": "omnivia_core.graph.models",
        "EntityType": "omnivia_core.graph.models",
        "GraphSearchQuery": "omnivia_core.graph.search_models",
        "GraphSearchResult": "omnivia_core.graph.search_models",
        "GraphSearchResultSet": "omnivia_core.graph.search_models",
        "RelationshipType": "omnivia_core.graph.models",
        "dataclass": "omnivia_core.graph.search_models",
        "field": "omnivia_core.graph.search_models",
    },
}

#: The name each split leaf binds from its own ``from __future__ import
#: annotations`` statement, and the exact object it must be. This is deliberately
#: *not* in ``SPLIT_LEAF_SYMBOL_SOURCES``: the split leaf must carry the real
#: future statement rather than import an ``annotations`` binding from its
#: canonical counterpart, because its retained definitions' signatures are
#: postponed string annotations and only the real statement makes them so. The
#: object is the same ``__future__._Feature`` either way, which is why the source
#: policy in ``baseline.facade_manifest.split_facade_defects`` -- not an identity
#: check -- is what actually holds the statement in place.
SPLIT_LEAF_FUTURE_BINDING: dict[str, str] = {
    "omnivia_memory.graph.search_models": "annotations",
}

#: For each split leaf, the definitions it deliberately keeps owning, in the
#: module's historical source order, with the exact frozen signature each must
#: still have. These are the whole reason the leaf is a split facade rather than a
#: plain one: canonical Core excludes them, and the unconverted, legacy-owned
#: ``omnivia_memory.graph.search_service`` still calls them.
#:
#: The signatures are spelled as postponed string annotations because the module
#: carries ``from __future__ import annotations``; pinning them here is what
#: catches a "helpful" rewrite that resolved or modernized them.
SPLIT_LEAF_RETAINED_HELPERS: dict[str, tuple[tuple[str, str], ...]] = {
    "omnivia_memory.graph.search_models": (
        ("score_name_match", "(query: 'str', entity_name: 'str') -> 'float'"),
        ("score_relationship_count", "(outgoing: 'int', incoming: 'int') -> 'float'"),
        (
            "score_neighbor_overlap",
            "(neighbors: 'list[str]', query_keywords: 'list[str]') -> 'float'",
        ),
        (
            "compute_relevance_score",
            (
                "(query: 'str', entity_name: 'str', outgoing_relationships: 'int' = 0, "
                "incoming_relationships: 'int' = 0, "
                "neighbor_names: 'list[str] | None' = None, "
                "name_weight: 'float' = 0.5, relationship_weight: 'float' = 0.25, "
                "neighbor_weight: 'float' = 0.25) -> 'float'"
            ),
        ),
    ),
}

#: SHA-256 of the exact accepted source segment of each retained helper, per split
#: leaf. Every other gate on the retained half checks a *property* of it -- owner,
#: signature, source order, descriptor shape, sampled behavior -- and a preserved
#: runtime body can drift in ways none of those can see: a reworded docstring, a
#: dropped explanatory comment, a reflowed expression, a rewritten branch that
#: happens to agree on the sampled inputs. This mapping is the byte-level pin that
#: closes that gap.
#:
#: Derivation (done once, by hand, and deliberately *not* repeated at runtime --
#: this test invokes no Git and reads no history, so it holds in an exported
#: tarball or a shallow clone as much as in this repository):
#:
#: 1. ``git show d3c959b:services/omnivia-memory/src/omnivia_memory/graph/\
#:    search_models.py`` -- the accepted checkpoint, i.e. the leaf *before* the
#:    split-facade conversion, so the expected values cannot have been taken from
#:    an edited working tree;
#: 2. ``ast.parse`` that text and, for each top-level ``FunctionDef``, take
#:    ``ast.get_source_segment(text, node)``;
#: 3. ``hashlib.sha256(segment.encode("utf-8")).hexdigest()``.
#:
#: The segment spans ``def`` through the function's last line, so it covers the
#: signature and its defaults, the docstring, the body, the comments *inside* the
#: body, and the exact whitespace of all of it. It deliberately does not cover the
#: comments *between* helpers, which are module-level furniture rather than part of
#: any preserved definition.
SPLIT_LEAF_RETAINED_HELPER_SOURCE_SHA256: dict[str, dict[str, str]] = {
    "omnivia_memory.graph.search_models": {
        "score_name_match": (
            "417a2689b164eec49293a372dbb45368f03961504804f722c13bb290b22473b2"
        ),
        "score_relationship_count": (
            "92661a26612a2cafe6dc5d32d872fe93a4b0f0b6aa6007a75346e60bec2ac83a"
        ),
        "score_neighbor_overlap": (
            "49407a7d674d034dd559128071607dac4ffcedb753f0bcb6c156696ac8f91ec7"
        ),
        "compute_relevance_score": (
            "4fbb82857919e631bb65cfafc6f44430f13273427a6c3deea9b9b770cbfff358"
        ),
    },
}

#: Each split leaf's single canonical import source -- the exact module its one
#: non-``__future__`` from-import must name. Separate from
#: ``SPLIT_LEAF_SYMBOL_SOURCES`` for the same reason ``LEAF_IMPORT_SOURCE`` is
#: separate from ``LEAF_SYMBOL_SOURCES``: a name's *owner* may be a sibling
#: canonical leaf while the leaf still imports it from only one place.
SPLIT_LEAF_IMPORT_SOURCE: dict[str, str] = {
    "omnivia_memory.graph.search_models": "omnivia_core.graph.search_models",
}

#: The one split leaf and its canonical counterpart, named once for the gates that
#: assert about it directly rather than parametrizing over the maps above.
SPLIT_LEAF = "omnivia_memory.graph.search_models"
SPLIT_CANONICAL = "omnivia_core.graph.search_models"


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
    # The control plane defines its own registry lifecycle enum, distinct from
    # the lifecycle domain's state machine. Three converted leaves route this
    # name to the lifecycle owner and three to the control-plane owner, and the
    # legacy root binds the control-plane one, so the separation is pinned here.
    "LifecycleState": (
        "omnivia_core.lifecycle.models",
        "omnivia_core.control_plane.models",
    ),
    # The knowledge domain's evidence reference and the memory graph's are
    # independent contracts with different fields that happen to share a name.
    # Four converted leaves route this name to the knowledge owner and four to the
    # memory-graph owner; the legacy root binds the knowledge one, so the
    # separation decides both a leaf contract and a root binding.
    "SourceRef": (
        "omnivia_core.knowledge.models",
        "omnivia_core.memory_graph.models",
    ),
    # The provenance domain's source record and the ingestion domain's ingested
    # file record are independent contracts that happen to share a name. Two
    # converted leaves route this name to the provenance owner
    # (``provenance.models`` and ``memory.models``) and one to the ingestion owner;
    # the legacy root binds the provenance one, so the ingestion route moves no
    # root binding for it. ``ingestion.models`` additionally publishes the same
    # object under its ``IngestSource`` alias, which is why the separation has to
    # be pinned rather than left to the per-symbol identity checks.
    "Source": (
        "omnivia_core.provenance.models",
        "omnivia_core.ingestion.models",
    ),
}

#: For each colliding name, the single owner the *legacy package root* has always
#: re-exported it from. The root itself is deliberately unedited by this slice:
#: these bindings move to the canonical objects transitively, through the
#: converted leaves, and which owner each lands on must not change. See
#: ``test_legacy_root_keeps_its_historical_owner_for_each_colliding_name``.
ROOT_OWNERS: dict[str, str] = {
    "ValidationResult": "omnivia_core._shared.validation",
    "ProvenanceRequirement": "omnivia_core.component_contract.models",
    # The root's ``LifecycleState`` has always been the control plane's registry
    # enum, not the lifecycle domain's -- which is why converting the control
    # plane moves that frozen root binding's owner (see
    # ``baseline.inventory.FACADE_ROUTES``) while the three lifecycle-domain
    # routes for the same name move nothing at the root.
    "LifecycleState": "omnivia_core.control_plane.models",
    # The root's ``SourceRef`` has always been the knowledge domain's, reached
    # through the knowledge barrel -- not the memory graph's, even though the
    # ``memory_graph`` barrel re-exports one under that name too and the root
    # imports from that barrel as well. Which of the two the root lands on is
    # decided by import order in the root's own unedited source, so pinning it
    # here is what would catch a memory-graph route silently taking it over.
    "SourceRef": "omnivia_core.knowledge.models",
    # The root's ``Source`` has always been the provenance domain's record,
    # reached through the provenance barrel -- not the ingestion domain's, whose
    # barrel the root has never imported from at all. Pinning it here is what
    # would catch the ingestion route silently taking it over.
    "Source": "omnivia_core.provenance.models",
}

#: The full legacy surface of the ``SourceRef`` collision: for each of its two
#: canonical owners, every legacy module that must expose *that* domain's class.
#: Two of these consumers are unreachable from ``LEAF_SYMBOL_SOURCES`` -- the
#: hybrid ``memory_graph`` barrel and the legacy package root -- yet they are the
#: ones whose owner is decided by import order in unedited legacy source, so they
#: are the ones an isolated, two-order proof most needs to cover. Declared once
#: here and consumed by both the fresh-process harness
#: (``_fresh_process_identity_script``) and the shared-process gate
#: (``test_memory_graph_source_ref_keeps_its_historical_collision_owner``), so the
#: two cannot drift apart. Exactly two owners: both consumers unpack the other
#: one as ``the`` alternative and would fail loudly if a third appeared.
SOURCE_REF_LEGACY_OWNERS: dict[str, tuple[str, ...]] = {
    "omnivia_core.memory_graph.models": (
        "omnivia_memory.memory_graph",
        "omnivia_memory.memory_graph.assembly",
        "omnivia_memory.memory_graph.fixtures",
        "omnivia_memory.memory_graph.models",
        "omnivia_memory.memory_graph.validation",
    ),
    "omnivia_core.knowledge.models": (
        "omnivia_memory",
        "omnivia_memory.knowledge",
        "omnivia_memory.knowledge.models",
        "omnivia_memory.knowledge.validation",
    ),
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
    identity-preserving transitively, through its converted leaves, not by
    being rewritten itself. Pin each one's exact historical shape -- one
    absolute ``from omnivia_memory.<barrel>.<leaf> import (...)`` statement per
    declared leaf, in source order with their exact ordered name lists, then the
    ``__all__`` literal -- so a future edit that reroutes a barrel directly at
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
    transitive route -- for ``control_plane`` that means all three hops, one per
    converted child.
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


def test_control_plane_collision_names_keep_their_historical_owners() -> None:
    """``ValidationResult`` and ``LifecycleState`` are name collisions across
    independent domains. The control plane's own dataclass and its own registry
    lifecycle enum are the ones these three leaves historically exposed -- and its
    ``LifecycleState`` is additionally the one the legacy package root binds -- so
    routing either to another domain's same-named class would be a silent contract
    swap that every "is the exact canonical object" check above would still pass.
    Pin the owners on all three converted leaves and the barrel, and pin that they
    are *not* the others.
    """
    canonical_leaf = importlib.import_module("omnivia_core.control_plane.models")
    legacy_barrel = importlib.import_module("omnivia_memory.control_plane")

    # ``imports`` binds only ``LifecycleState`` of the two; the other two leaves
    # bind both.
    for legacy_leaf_name, names in (
        ("omnivia_memory.control_plane.imports", ("LifecycleState",)),
        (
            "omnivia_memory.control_plane.models",
            ("ValidationResult", "LifecycleState"),
        ),
        (
            "omnivia_memory.control_plane.validation",
            ("ValidationResult", "LifecycleState"),
        ),
    ):
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        for name in names:
            assert getattr(legacy_leaf, name) is getattr(canonical_leaf, name), (
                f"{legacy_leaf_name}.{name} is not the exact object bound at "
                f"omnivia_core.control_plane.models.{name}"
            )
    for name in ("ValidationResult", "LifecycleState"):
        assert getattr(legacy_barrel, name) is getattr(canonical_leaf, name)

    for other_module in (
        "omnivia_core._shared.validation",
        "omnivia_memory._shared.validation",
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_memory.app_shell_bridge.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.lifecycle.models",
        "omnivia_memory.lifecycle.models",
        "omnivia_core.lifecycle.rules",
        "omnivia_memory.lifecycle.rules",
        "omnivia_core.memory.models",
        "omnivia_memory.memory.models",
        "omnivia_core.run_ledger.validation",
        "omnivia_memory.run_ledger.validation",
    ):
        other = importlib.import_module(other_module)
        for name in ("ValidationResult", "LifecycleState"):
            if not hasattr(other, name):
                continue
            assert getattr(canonical_leaf, name) is not getattr(other, name), (
                f"omnivia_core.control_plane.models.{name} must stay the control "
                f"plane's own class, not {other_module}.{name}"
            )


def test_legacy_root_keeps_its_historical_owner_for_each_colliding_name() -> None:
    """The legacy package root re-exports all three colliding names, but from only
    one owner each: ``ProvenanceRequirement`` from the Component Contract (via
    ``from .component_contract import ...``), ``ValidationResult`` from the
    shared primitive (via the knowledge barrel's re-export of it -- a hop that is
    now itself two converted facades deep, the knowledge validation leaf and the
    shared validation leaf), and ``LifecycleState`` from the control plane. The
    root is
    *not* edited by this slice, so those three bindings must still resolve to the
    same objects they always did -- now the canonical ones, reached transitively
    through the converted leaves.

    This is the invariant the leaf-level checks cannot see. ``ROOT_OWNERS``
    below pins one owner per name out of the five/two/two candidates, and the
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


def test_knowledge_validation_result_keeps_its_historical_collision_owner() -> None:
    """``ValidationResult`` is a name collision across five independent domains,
    and the knowledge validation leaf is the one converted leaf that owns *none*
    of them: it has always imported the shared primitive. Routing it to any
    domain's same-named dataclass would be a silent contract swap that every "is
    the exact canonical object" check above would still pass -- and it would
    change what the legacy package *root* binds, because the root takes its
    ``ValidationResult`` through this leaf and the knowledge barrel above it.

    Pin the owner on the leaf, on the barrel and at the root, and pin that it is
    *not* any of the four domain classes.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.knowledge.validation")
    legacy_barrel = importlib.import_module("omnivia_memory.knowledge")
    legacy_root = importlib.import_module("omnivia_memory")
    shared = importlib.import_module("omnivia_core._shared.validation")

    assert legacy_leaf.ValidationResult is shared.ValidationResult
    assert legacy_barrel.ValidationResult is shared.ValidationResult
    assert legacy_root.ValidationResult is shared.ValidationResult

    for other_module in (
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_memory.app_shell_bridge.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        assert legacy_leaf.ValidationResult is not other.ValidationResult, (
            "omnivia_memory.knowledge.validation.ValidationResult must stay the "
            f"shared primitive, not {other_module}.ValidationResult"
        )


def test_knowledge_barrel_publishes_a_subset_of_its_children_routed_surface() -> None:
    """The knowledge barrel is source-unchanged, and its historical source names
    only a subset of what its children now route.

    ``normalize_extension_value`` is the concrete case: it is a routed symbol of
    the normalize leaf and importable from it, but the barrel has never
    re-exported it, so it must stay absent from the barrel's ``__all__`` and from
    the barrel module itself. A "helpful" edit that added it would widen the
    barrel's advertised surface beyond what Phase 0 froze, and every identity
    check in this module would still pass.
    """
    barrel = importlib.import_module("omnivia_memory.knowledge")
    canonical_barrel = importlib.import_module("omnivia_core.knowledge")
    leaf = importlib.import_module("omnivia_memory.knowledge.normalize")

    assert hasattr(leaf, "normalize_extension_value")
    for module in (barrel, canonical_barrel):
        assert "normalize_extension_value" not in module.__all__
        assert not hasattr(module, "normalize_extension_value"), (
            f"{module.__name__} must not publish normalize_extension_value; the "
            "barrel's historical source never imported it"
        )

    # And the barrel's surface really is a subset, not a different set: every name
    # it advertises is bound at the leaf it re-exports from.
    for legacy_leaf_name, names in KNOWLEDGE_BARREL_ABSOLUTE_IMPORTS:
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        for name in names:
            assert hasattr(legacy_leaf, name)


def test_facade_canonical_to_legacy_manifest_matches_the_expected_pairs() -> None:
    """``FACADE_CANONICAL_TO_LEGACY`` and ``SPLIT_FACADE_CANONICAL_TO_LEGACY``
    (imported from the migration-test manifest) must be exactly the pairs declared
    here -- no manifest may drift (grow, shrink, or repoint) without this dedicated
    test noticing, since those shared constants are also what excludes these leaves
    from the canonical_migration source-parity gates. The two must stay disjoint:
    a leaf filed under both would be asserted to be a single-import facade *and* a
    split one, so one of the two source gates could only pass vacuously."""
    assert FACADE_CANONICAL_TO_LEGACY == EXPECTED_FACADE_CANONICAL_TO_LEGACY
    assert set(LEAF_SYMBOL_SOURCES) == set(EXPECTED_FACADE_CANONICAL_TO_LEGACY.values())

    assert SPLIT_FACADE_CANONICAL_TO_LEGACY == EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY
    assert set(SPLIT_LEAF_SYMBOL_SOURCES) == set(
        EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY.values()
    )
    # The migration-test manifest's record of what Core excludes must be exactly the
    # retained half declared here -- the same set, reached from the other side.
    assert SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL == frozenset(
        name for name, _signature in SPLIT_LEAF_RETAINED_HELPERS[SPLIT_LEAF]
    )
    assert set(SPLIT_LEAF_RETAINED_HELPERS) == set(SPLIT_LEAF_SYMBOL_SOURCES)
    assert set(SPLIT_LEAF_IMPORT_SOURCE) == set(SPLIT_LEAF_SYMBOL_SOURCES)
    assert set(SPLIT_LEAF_FUTURE_BINDING) == set(SPLIT_LEAF_SYMBOL_SOURCES)
    assert set(SPLIT_LEAF_RETAINED_HELPER_SOURCE_SHA256) == set(
        SPLIT_LEAF_SYMBOL_SOURCES
    )

    assert set(EXPECTED_FACADE_CANONICAL_TO_LEGACY).isdisjoint(
        EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY
    )
    assert set(LEAF_SYMBOL_SOURCES).isdisjoint(SPLIT_LEAF_SYMBOL_SOURCES)


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
    canonical_modules = sorted(
        {*EXPECTED_FACADE_CANONICAL_TO_LEGACY, *EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY}
    )
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
    canonical_modules = sorted(
        {*EXPECTED_FACADE_CANONICAL_TO_LEGACY, *EXPECTED_SPLIT_FACADE_CANONICAL_TO_LEGACY}
    )
    # The legacy side is more than the converted leaves: the hybrid
    # ``memory_graph`` barrel and the legacy package root resolve their
    # ``SourceRef`` through unedited legacy import order, so they must be imported
    # on the legacy side of whichever of the two orders is under test -- not pulled
    # in incidentally as a parent package of some leaf.
    legacy_modules = sorted(
        set(LEAF_SYMBOL_SOURCES)
        | set(SPLIT_LEAF_SYMBOL_SOURCES)
        | {
            module
            for modules in SOURCE_REF_LEGACY_OWNERS.values()
            for module in modules
        }
    )
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
    for legacy_module, symbols in (
        *LEAF_SYMBOL_SOURCES.items(),
        *SPLIT_LEAF_SYMBOL_SOURCES.items(),
    ):
        for symbol, canonical_module in symbols.items():
            lines.append(
                f"assert {legacy_module}.{symbol} is {canonical_module}.{symbol}, "
                f"'{legacy_module}.{symbol} is not {canonical_module}.{symbol} "
                f"(canonical_first={canonical_first})'"
            )
    # A split leaf's retained half must stay owned by the leaf itself, in either
    # order: an import order that let the canonical module supply one of these
    # would satisfy every identity assertion above and still be wrong.
    for legacy_module, helpers in SPLIT_LEAF_RETAINED_HELPERS.items():
        canonical_module = SPLIT_LEAF_IMPORT_SOURCE[legacy_module]
        for helper, _signature in helpers:
            lines.append(
                f"assert {legacy_module}.{helper}.__module__ == {legacy_module!r}, "
                f"'{legacy_module}.{helper} is no longer legacy-owned "
                f"(canonical_first={canonical_first})'"
            )
            lines.append(
                f"assert not hasattr({canonical_module}, {helper!r}), "
                f"'{canonical_module} acquired {helper} "
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
    # ``SourceRef`` is the one collision whose routes decide a hybrid barrel's and
    # the legacy root's binding as well as four leaves', and neither of those two
    # consumers appears in ``LEAF_SYMBOL_SOURCES``. Pin both owners' whole legacy
    # surface here so the isolated two-order proof reaches them too, rather than
    # leaving them to the shared-process gate at the end of this module -- where a
    # collision could already have been settled by an earlier test's imports.
    for canonical_module, legacy_owned in SOURCE_REF_LEGACY_OWNERS.items():
        (other_canonical,) = set(SOURCE_REF_LEGACY_OWNERS) - {canonical_module}
        for legacy_module in legacy_owned:
            lines.append(
                f"assert {legacy_module}.SourceRef is {canonical_module}.SourceRef, "
                f"'{legacy_module}.SourceRef is not {canonical_module}.SourceRef "
                f"(canonical_first={canonical_first})'"
            )
            lines.append(
                f"assert {legacy_module}.SourceRef is not "
                f"{other_canonical}.SourceRef, "
                f"'{legacy_module}.SourceRef was taken over by "
                f"{other_canonical}.SourceRef (canonical_first={canonical_first})'"
            )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "canonical_first", [True, False], ids=["canonical-first", "facade-first"]
)
def test_fresh_process_import_order_preserves_identity(canonical_first: bool) -> None:
    _run_isolated(_fresh_process_identity_script(canonical_first=canonical_first))


# ---------------------------------------------------------------------------
# The ``memory_graph`` hybrid barrel.
#
# Most converted leaf sets in this module sit under a barrel whose whole
# advertised surface became canonical. ``memory_graph`` is the second that does
# not -- ``memory``, converted before it, is the earlier counterexample: seven of
# its thirty-eight exports are owned by the runtime-only
# ``ingestion_adapter``/``store`` leaves, which never enter Core, so the barrel
# cannot become a pure re-export of the canonical package and is recorded as a
# ``hybrid_facade`` in ``compatibility/facade-routes.v1.json``. Its legacy and
# canonical ``__all__`` are different sizes as a result, which is exactly why it
# must stay out of ``BARREL_ALL_ORDER`` and the equal-``__all__`` gates above.
#
# What has to hold instead is split cleanly in two: the portable half is
# identity-preserving *through the child facades*, and the runtime half is still
# legacy-owned and still absent from the canonical barrel.
# ---------------------------------------------------------------------------

#: The exact, ordered *absolute* re-export shape the unchanged legacy
#: ``memory_graph`` barrel must still have: ``(absolute module, imported names in
#: source order)``. Six blocks, in the barrel's own historical order -- which is
#: neither alphabetical nor portable-first: the two runtime-only leaves sit
#: between ``fixtures`` and ``models``. Restated here rather than read off the
#: barrel, because this is the file whose edits it exists to reject.
MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
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
        (
            "FIXTURE_TIME",
            "MemoryGraphFixture",
            "build_memory_graph_fixture",
        ),
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
        (
            "MemoryGraphStore",
            "MemoryGraphStoreError",
        ),
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
)

#: The barrel's exact ordered 38-name ``__all__`` literal, restated rather than
#: derived: it is sorted, so it interleaves all six blocks' names and matches none
#: of them.
MEMORY_GRAPH_BARREL_ALL: tuple[str, ...] = (
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
)

#: The barrel's two runtime-only children, which are declared runtime-only in the
#: frozen route registry and are deliberately *not* facades.
MEMORY_GRAPH_RUNTIME_ONLY_LEAVES: tuple[str, ...] = (
    "omnivia_memory.memory_graph.ingestion_adapter",
    "omnivia_memory.memory_graph.store",
)

#: The barrel's exact seven runtime-only exports: they must stay legacy-owned and
#: must never appear on the canonical barrel.
MEMORY_GRAPH_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
        "IngestionGraphAdapterError",
        "IngestionGraphWriteResult",
        "MemoryGraphStore",
        "MemoryGraphStoreError",
        "chunk_to_memory_segment",
        "source_to_memory_source",
        "write_ingestion_records_to_graph",
    }
)


def _memory_graph_block(module: str) -> tuple[str, ...]:
    (names,) = [
        names for name, names in MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS if name == module
    ]
    return names


def test_memory_graph_hybrid_barrel_is_held_out_of_the_equal_all_gates() -> None:
    """The barrel's two trees advertise *different* surfaces, so every gate keyed
    on ``BARREL_ALL_ORDER`` (which asserts ``legacy.__all__ == canonical.__all__``)
    would be wrong for it. Pin that it is absent from those gates, and pin the
    inequality that is the reason -- so a future edit that "helpfully" added
    ``memory_graph`` to ``BARREL_ALL_ORDER`` fails here with the reason rather
    than as a confusing list mismatch.
    """
    assert "memory_graph" not in BARREL_ALL_ORDER
    assert "memory_graph" not in ABSOLUTE_IMPORT_BARRELS
    assert "memory_graph" not in ABSOLUTE_IMPORT_BARREL_IMPORTS
    assert "memory_graph" not in RELATIVE_IMPORT_BARREL_IMPORTS

    legacy = importlib.import_module("omnivia_memory.memory_graph")
    canonical = importlib.import_module("omnivia_core.memory_graph")
    assert tuple(legacy.__all__) == MEMORY_GRAPH_BARREL_ALL
    assert len(legacy.__all__) == 38
    assert len(canonical.__all__) == 31
    assert set(canonical.__all__) == set(MEMORY_GRAPH_BARREL_ALL) - (
        MEMORY_GRAPH_RUNTIME_EXPORTS
    )


def test_memory_graph_hybrid_barrel_source_is_unchanged_reexport() -> None:
    """The hybrid barrel is *source-unchanged* by this slice: its portable half
    becomes identity-preserving transitively, through its four converted leaves,
    and its runtime half keeps resolving locally. Pin its exact historical shape --
    six absolute ``from omnivia_memory.memory_graph.<leaf> import (...)``
    statements in source order with their exact ordered name lists, then the
    ``__all__`` literal -- so an edit that reroutes it at ``omnivia_core``, drops
    the runtime blocks, adds a ``__getattr__``, or reorders its re-exports fails
    here.
    """
    module_name = "omnivia_memory.memory_graph"
    body = _module_body_after_docstring(module_name)
    assert len(body) == len(MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS) + 1, (
        f"{module_name}: expected exactly "
        f"{len(MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS)} absolute imports plus "
        f"__all__, found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(
        body, MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS, strict=False
    ):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {node!r}"
        assert node.level == 0, f"{module_name}: the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {all_node!r}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert tuple(
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ) == MEMORY_GRAPH_BARREL_ALL

    # Every name the six imports bind is exactly what ``__all__`` advertises: the
    # barrel adds nothing of its own and hides nothing it imported.
    imported = sorted(
        name for _, names in MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS for name in names
    )
    assert imported == sorted(MEMORY_GRAPH_BARREL_ALL)
    assert "__getattr__" not in vars(importlib.import_module(module_name))


def test_memory_graph_hybrid_barrel_portable_exports_hop_through_their_facades() -> None:
    """The barrel's 31 portable exports must each be the exact object bound at the
    *legacy child facade* it re-exports from, and that object must in turn be the
    canonical one. A barrel that started sourcing a name from somewhere else would
    still pass the canonical-identity check alone; requiring the leaf hop too is
    what pins the transitive route through all four converted children.
    """
    barrel = importlib.import_module("omnivia_memory.memory_graph")
    portable = 0
    for legacy_leaf_name, names in MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS:
        if legacy_leaf_name in MEMORY_GRAPH_RUNTIME_ONLY_LEAVES:
            continue
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        owners = LEAF_SYMBOL_SOURCES[legacy_leaf_name]
        for name in names:
            canonical_owner = importlib.import_module(owners[name])
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.memory_graph.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            assert getattr(barrel, name) is getattr(canonical_owner, name), (
                f"omnivia_memory.memory_graph.{name} is not the exact object bound "
                f"at {owners[name]}.{name}"
            )
            portable += 1
    assert portable == 31


def test_memory_graph_hybrid_barrel_runtime_exports_stay_legacy_owned() -> None:
    """The other seven exports are the whole reason this barrel is a hybrid, so
    their *non*-conversion is as much a contract as the portable half's
    conversion. Each must still be the exact object bound at its legacy
    ``ingestion_adapter``/``store`` owner, and each of those owners must still be
    a real legacy module backed by a file in the compatibility tree -- not a
    facade that quietly acquired a canonical counterpart.
    """
    barrel = importlib.import_module("omnivia_memory.memory_graph")
    covered: set[str] = set()
    for legacy_leaf_name in MEMORY_GRAPH_RUNTIME_ONLY_LEAVES:
        assert legacy_leaf_name not in LEAF_SYMBOL_SOURCES, (
            f"{legacy_leaf_name} is runtime-owned and must not become a facade"
        )
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        leaf_path = Path(legacy_leaf.__file__ or "").resolve()
        assert leaf_path.is_relative_to(MEMORY_SRC), (
            f"{legacy_leaf_name} resolved to {leaf_path}, outside the legacy tree"
        )
        for name in _memory_graph_block(legacy_leaf_name):
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.memory_graph.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            covered.add(name)
    assert covered == set(MEMORY_GRAPH_RUNTIME_EXPORTS)


def test_memory_graph_runtime_exports_are_absent_from_the_canonical_barrel() -> None:
    """None of the seven may leak into Core -- not into its ``__all__`` and not as
    an attribute. This is what keeps the runtime-owned half out of the canonical
    package rather than merely un-advertised there.
    """
    canonical = importlib.import_module("omnivia_core.memory_graph")
    for name in sorted(MEMORY_GRAPH_RUNTIME_EXPORTS):
        assert name not in canonical.__all__, (
            f"{name} is runtime-owned and must not be in "
            "omnivia_core.memory_graph.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is runtime-owned and must not be an attribute of "
            "omnivia_core.memory_graph"
        )


def test_memory_graph_source_ref_keeps_its_historical_collision_owner() -> None:
    """``SourceRef`` is a name collision between two independent domains. The
    memory graph's own evidence reference is the one these four leaves and the
    hybrid barrel historically exposed, while the legacy package *root* has always
    taken its ``SourceRef`` from the knowledge domain -- even though it imports
    from both barrels. Routing either side to the other's class would be a silent
    contract swap that every "is the exact canonical object" check above would
    still pass.

    The module lists live in ``SOURCE_REF_LEGACY_OWNERS`` rather than inline,
    because ``_fresh_process_identity_script`` asserts the same pairs in two
    isolated import orders and the two must not drift apart.
    """
    assert set(SOURCE_REF_LEGACY_OWNERS) == set(COLLIDING_OWNERS["SourceRef"])
    canonical_leaf = importlib.import_module("omnivia_core.memory_graph.models")
    knowledge_leaf = importlib.import_module("omnivia_core.knowledge.models")
    assert canonical_leaf.SourceRef is not knowledge_leaf.SourceRef

    for canonical_module, legacy_owned in SOURCE_REF_LEGACY_OWNERS.items():
        (other_name,) = set(SOURCE_REF_LEGACY_OWNERS) - {canonical_module}
        canonical = importlib.import_module(canonical_module)
        other = importlib.import_module(other_name)
        for legacy_module in legacy_owned:
            module = importlib.import_module(legacy_module)
            assert module.SourceRef is canonical.SourceRef, (
                f"{legacy_module}.SourceRef is not {canonical_module}'s own class"
            )
            assert module.SourceRef is not other.SourceRef, (
                f"{legacy_module}.SourceRef was taken over by "
                f"{other_name}.SourceRef"
            )


def test_memory_graph_validation_result_keeps_its_historical_collision_owner() -> None:
    """The memory graph validation leaf owns *none* of the five same-named
    ``ValidationResult`` classes: it has always imported the shared primitive.
    Routing it to any domain's dataclass would be a silent contract swap -- and it
    would change what the hybrid barrel publishes, because the barrel takes its
    ``ValidationResult`` from this leaf.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.memory_graph.validation")
    legacy_barrel = importlib.import_module("omnivia_memory.memory_graph")
    canonical_barrel = importlib.import_module("omnivia_core.memory_graph")
    shared = importlib.import_module("omnivia_core._shared.validation")

    assert legacy_leaf.ValidationResult is shared.ValidationResult
    assert legacy_barrel.ValidationResult is shared.ValidationResult
    assert canonical_barrel.ValidationResult is shared.ValidationResult

    for other_module in (
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.app_shell_bridge.models",
        "omnivia_memory.app_shell_bridge.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        assert legacy_leaf.ValidationResult is not other.ValidationResult, (
            "omnivia_memory.memory_graph.validation.ValidationResult must stay the "
            f"shared primitive, not {other_module}.ValidationResult"
        )


def test_memory_graph_pinned_values_survive_the_route() -> None:
    """Three routed symbols carry a *value*, not just a type, so identity alone
    would not catch a canonical owner that rebuilt them differently.
    """
    legacy_fixtures = importlib.import_module("omnivia_memory.memory_graph.fixtures")
    legacy_models = importlib.import_module("omnivia_memory.memory_graph.models")
    legacy_validation = importlib.import_module(
        "omnivia_memory.memory_graph.validation"
    )
    canonical_models = importlib.import_module("omnivia_core.memory_graph.models")

    assert legacy_fixtures.FIXTURE_TIME == "2026-06-07T00:00:00+00:00"
    assert isinstance(legacy_validation.CONFIDENCE_BUCKETS, frozenset)
    assert legacy_validation.CONFIDENCE_BUCKETS == frozenset(
        {"extracted", "inferred", "ambiguous"}
    )
    assert all(isinstance(item, str) for item in legacy_validation.CONFIDENCE_BUCKETS)
    # ``Confidence`` is a ``types.UnionType`` alias; a rebuilt one would compare
    # equal, so identity against the canonical object is the assertion that
    # matters -- and it is the same alias the models leaf annotates with.
    assert legacy_models.Confidence is canonical_models.Confidence
    assert legacy_models.Confidence == (float | str)


def test_memory_graph_runtime_consumers_hold_the_canonical_objects() -> None:
    """``memory_graph.store`` and ``memory_graph.ingestion_adapter`` are
    unconverted runtime leaves that import their contracts *from the converted
    facades*, so they are now the first in-repo consumers holding canonical model
    and validation objects while staying legacy-owned themselves. Pin that hop:
    the runtime half of the hybrid barrel has to keep working against exactly the
    objects Core owns.
    """
    store = importlib.import_module("omnivia_memory.memory_graph.store")
    adapter = importlib.import_module("omnivia_memory.memory_graph.ingestion_adapter")
    canonical_models = importlib.import_module("omnivia_core.memory_graph.models")
    canonical_validation = importlib.import_module(
        "omnivia_core.memory_graph.validation"
    )
    shared = importlib.import_module("omnivia_core._shared.validation")

    for name in ("MemoryEntity", "MemoryFact", "MemorySegment", "MemorySource"):
        assert getattr(store, name) is getattr(canonical_models, name)
    for name in (
        "validate_memory_entity",
        "validate_memory_fact",
        "validate_memory_segment",
        "validate_memory_source",
    ):
        assert getattr(store, name) is getattr(canonical_validation, name)
    assert store.ValidationResult is shared.ValidationResult

    for name in (
        "MemorySegment",
        "MemorySegmentKind",
        "MemorySource",
        "MemorySourceFreshness",
        "MemorySourceStatus",
        "MemorySourceType",
    ):
        assert getattr(adapter, name) is getattr(canonical_models, name)


def test_canonical_memory_graph_does_not_load_the_runtime_only_leaves() -> None:
    """A canonical-only import must not reach ``omnivia_memory`` at all -- and in
    particular not the two runtime-only leaves this barrel's other seven exports
    come from, which pull in the ingestion runtime and a filesystem-writing store.
    Only ``src`` goes on the path, so any such reach is a hard failure here.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.memory_graph",
            "import omnivia_core.memory_graph.assembly",
            "import omnivia_core.memory_graph.fixtures",
            "import omnivia_core.memory_graph.models",
            "import omnivia_core.memory_graph.validation",
            "assert 'omnivia_memory' not in sys.modules",
            "leaked = sorted(",
            "    name for name in sys.modules",
            "    if name.endswith(('.store', '.ingestion_adapter'))",
            ")",
            "assert not leaked, leaked",
        ]
    )
    _run_isolated(script)


# ---------------------------------------------------------------------------
# The ``graph`` pair: one direct facade, one split facade, one hybrid barrel.
#
# ``graph.search_models`` is the first *split* facade in the migration. Its three
# query/result records are canonicalized, so the whole portable half of its
# historical namespace is now the exact ``omnivia_core`` objects -- but the four
# relevance-scoring helpers stay defined here, because they are search-runtime
# behaviour Core deliberately excludes and the unconverted, legacy-owned
# ``omnivia_memory.graph.search_service`` still calls them.
#
# That makes it the one converted leaf whose body is *not* a single import, so it
# is held out of ``LEAF_SYMBOL_SOURCES`` and every gate keyed on it, and gets the
# equivalent set here instead: exact source shape, portable identity and owner,
# complete namespace, retained-half ownership/signature/behavior, and the barrel
# above it -- which stays a hybrid, because two of its ten exports come from the
# runtime-only ``search_service`` leaf.
# ---------------------------------------------------------------------------

#: The exact, ordered *absolute* re-export shape the unchanged legacy ``graph``
#: barrel must still have: ``(absolute module, imported names in source order)``.
#: Three blocks, in the barrel's own historical order. Restated here rather than
#: read off the barrel, because this is the file whose edits it exists to reject.
GRAPH_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
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
        (
            "GraphSearchQuery",
            "GraphSearchResult",
            "GraphSearchResultSet",
        ),
    ),
    (
        "omnivia_memory.graph.search_service",
        (
            "GraphSearchError",
            "GraphSearchService",
        ),
    ),
)

#: The barrel's exact ordered ten-name ``__all__`` literal, restated rather than
#: derived: it is sorted, so it interleaves all three blocks' names and matches
#: none of them.
GRAPH_BARREL_ALL: tuple[str, ...] = (
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
)

#: The barrel's one runtime-only child, declared runtime-only in the frozen route
#: registry and deliberately not a facade. ``repository`` and ``service`` are
#: runtime-only too, but the barrel has never re-exported them.
GRAPH_RUNTIME_ONLY_LEAF = "omnivia_memory.graph.search_service"

#: The barrel's exact two runtime-only exports: they must stay legacy-owned and
#: must never appear on the canonical barrel.
GRAPH_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
        "GraphSearchError",
        "GraphSearchService",
    }
)

#: The barrel's exact eight portable exports: everything else, all of which must
#: hop through a converted child to a canonical object.
GRAPH_PORTABLE_EXPORTS: frozenset[str] = frozenset(GRAPH_BARREL_ALL) - GRAPH_RUNTIME_EXPORTS

#: The graph runtime modules that stay legacy-owned and unedited by this batch.
#: ``search_service`` is the one the barrel re-exports from; all three consume the
#: converted models leaf, so all three now hold canonical objects.
GRAPH_RUNTIME_MODULES: tuple[str, ...] = (
    "omnivia_memory.graph.repository",
    "omnivia_memory.graph.search_service",
    "omnivia_memory.graph.service",
)

#: The exact canonical closure a canonical-only Graph import may produce. Anything
#: else -- a sibling domain, a runtime leaf -- is a leak.
GRAPH_CANONICAL_MODULE_CLOSURE: frozenset[str] = frozenset(
    {
        "omnivia_core",
        "omnivia_core.graph",
        "omnivia_core.graph.models",
        "omnivia_core.graph.search_models",
    }
)

#: Module roots a canonical-only Graph import must never load. The graph runtime
#: reaches SQLite through ``omnivia_memory.persistence``, so its absence is part of
#: what "the canonical contract layer stands alone" means here.
GRAPH_FORBIDDEN_MODULE_ROOTS: tuple[str, ...] = (
    "omnivia_cloud",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "omnivia_core_runtime",
    "omnivia_dev",
    "omnivia_memory",
    "omnivia_platform",
    "sqlalchemy",
    "sqlite3",
)

def _split_leaf_helper_names(leaf_name: str) -> tuple[str, ...]:
    return tuple(name for name, _signature in SPLIT_LEAF_RETAINED_HELPERS[leaf_name])


def _split_leaf_symbol_cases() -> list[tuple[str, str, str]]:
    return [
        (legacy_module, symbol, canonical_module)
        for legacy_module, symbols in SPLIT_LEAF_SYMBOL_SOURCES.items()
        for symbol, canonical_module in symbols.items()
    ]


def _graph_barrel_block(module: str) -> tuple[str, ...]:
    (names,) = [names for name, names in GRAPH_BARREL_ABSOLUTE_IMPORTS if name == module]
    return names


@pytest.mark.parametrize(
    "legacy_module,symbol,canonical_module",
    _split_leaf_symbol_cases(),
    ids=[f"{m}.{s}" for m, s, _ in _split_leaf_symbol_cases()],
)
def test_split_leaf_portable_symbol_is_exact_canonical_object(
    legacy_module: str, symbol: str, canonical_module: str
) -> None:
    """The portable half of a split facade is held to exactly the same standard as
    a plain facade's whole surface: the *exact* canonical object, not a
    structurally equal lookalike."""
    legacy = importlib.import_module(legacy_module)
    canonical = importlib.import_module(canonical_module)
    assert getattr(legacy, symbol) is getattr(canonical, symbol), (
        f"{legacy_module}.{symbol} is not the exact same object as "
        f"{canonical_module}.{symbol}"
    )


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_SYMBOL_SOURCES))
def test_split_leaf_has_no_all(leaf_name: str) -> None:
    module = importlib.import_module(leaf_name)
    assert not hasattr(module, "__all__"), (
        f"{leaf_name} must not define __all__ -- it never did before becoming a facade"
    )


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_SYMBOL_SOURCES))
def test_split_leaf_star_import_exposes_the_portable_and_retained_halves(
    leaf_name: str,
) -> None:
    """A split leaf declares no ``__all__``, so ``from <leaf> import *`` exposes
    exactly its non-underscore module-scope bindings -- which must be exactly the
    portable half plus the retained half plus its ``annotations`` binding, and
    nothing else. Checking the star surface directly is what proves those
    enumerations are *complete* rather than merely correct: a leaf that dropped an
    incidental name, kept a fifth helper, or picked up a binding of its own passes
    every per-symbol check above and fails here.
    """
    exported = _star_import_namespace(leaf_name)
    expected = (
        set(SPLIT_LEAF_SYMBOL_SOURCES[leaf_name])
        | set(_split_leaf_helper_names(leaf_name))
        | {SPLIT_LEAF_FUTURE_BINDING[leaf_name]}
    )
    assert exported == expected, (
        f"star import of {leaf_name} exposed {sorted(exported)}, expected exactly "
        f"{sorted(expected)}"
    )


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_SYMBOL_SOURCES))
def test_split_leaf_future_binding_is_the_real_future_feature(leaf_name: str) -> None:
    """``annotations`` must be the ``__future__`` feature object, and it must come
    from this module's own future statement rather than from the canonical module.
    The identity is the same object either way -- which is exactly why the source
    gate below, not this assertion, is what holds the statement in place."""
    module = importlib.import_module(leaf_name)
    binding = SPLIT_LEAF_FUTURE_BINDING[leaf_name]
    assert getattr(module, binding) is getattr(__import__("__future__"), binding)
    assert binding not in SPLIT_LEAF_SYMBOL_SOURCES[leaf_name], (
        f"{binding!r} must not be declared as a routed portable name: the split "
        "leaf carries the real future statement"
    )


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_RETAINED_HELPERS))
def test_split_leaf_retained_helpers_stay_legacy_owned(leaf_name: str) -> None:
    """The retained half is the whole reason the leaf is a split facade, so its
    *non*-conversion is as much a contract as the portable half's conversion. Each
    helper must be a real function whose ``__module__`` is this legacy module,
    backed by a file in the compatibility tree, and absent from the canonical
    module and its barrel."""
    module = importlib.import_module(leaf_name)
    canonical = importlib.import_module(SPLIT_LEAF_IMPORT_SOURCE[leaf_name])
    canonical_barrel = importlib.import_module(
        SPLIT_LEAF_IMPORT_SOURCE[leaf_name].rpartition(".")[0]
    )
    leaf_path = Path(module.__file__ or "").resolve()
    assert leaf_path.is_relative_to(MEMORY_SRC), (
        f"{leaf_name} resolved to {leaf_path}, outside the legacy tree"
    )

    for helper, signature in SPLIT_LEAF_RETAINED_HELPERS[leaf_name]:
        function = getattr(module, helper)
        assert isinstance(function, types.FunctionType), (
            f"{leaf_name}.{helper} must stay a plain function, not "
            f"{type(function).__name__}"
        )
        assert not inspect.iscoroutinefunction(function)
        assert function.__module__ == leaf_name, (
            f"{leaf_name}.{helper} reports owner {function.__module__!r}; the "
            "retained helpers must stay legacy-owned"
        )
        assert str(inspect.signature(function)) == signature, (
            f"{leaf_name}.{helper} signature drifted:\n"
            f"actual:   {inspect.signature(function)}\nexpected: {signature}"
        )
        assert not hasattr(canonical, helper), (
            f"{SPLIT_LEAF_IMPORT_SOURCE[leaf_name]} must not define {helper!r}; "
            "relevance scoring stays runtime-owned"
        )
        assert not hasattr(canonical_barrel, helper), (
            f"{canonical_barrel.__name__} must not expose {helper!r}"
        )
        assert helper not in getattr(canonical_barrel, "__all__", ())


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_RETAINED_HELPERS))
def test_split_leaf_retained_helpers_keep_their_historical_source_order(
    leaf_name: str,
) -> None:
    """The retained definitions must appear in the module in their declared
    historical order. Reordering them changes nothing any identity or signature
    check can see, and it is precisely the kind of "tidy-up" a preserved runtime
    half must not receive."""
    body = _module_body_after_docstring(leaf_name)
    defined = tuple(
        node.name for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert defined == _split_leaf_helper_names(leaf_name)


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_RETAINED_HELPER_SOURCE_SHA256))
def test_split_leaf_retained_helper_source_is_the_accepted_bytes(leaf_name: str) -> None:
    """The retained half must be the *accepted source*, not merely a working
    equivalent of it.

    Every other retained-half gate checks a property: the owner, the signature,
    the source order, the frozen descriptor, a handful of sampled scores. A
    preserved runtime body can drift past all of them -- a reworded docstring, a
    deleted "why" comment, a reflowed expression, a rewritten branch that agrees
    on the inputs the behavior tests happen to try. So each helper's exact source
    segment is hashed and compared against the value frozen in
    ``SPLIT_LEAF_RETAINED_HELPER_SOURCE_SHA256``, which was derived from the
    accepted checkpoint rather than from this working tree (see that constant for
    the derivation).

    No Git at runtime: the pin is a literal, so this gate is as valid in an
    exported tarball as in a full clone. ``Path.read_text`` normalizes line
    endings, so a CRLF checkout hashes the same as an LF one.
    """
    pinned = SPLIT_LEAF_RETAINED_HELPER_SOURCE_SHA256[leaf_name]
    declared = {name for name, _signature in SPLIT_LEAF_RETAINED_HELPERS[leaf_name]}
    assert set(pinned) == declared, (
        f"{leaf_name}: the pinned source set {sorted(pinned)} is not exactly the "
        f"declared retained helpers {sorted(declared)}"
    )

    # The file this gate hashes must be the one in this checkout's compatibility
    # tree, exactly as ``test_split_leaf_retained_helpers_stay_legacy_owned``
    # requires. Without this a shadowing installed copy of ``omnivia_memory``
    # would be hashed instead, and a byte-level pin that can silently pin the
    # wrong bytes is worse than none.
    path = Path(importlib.import_module(leaf_name).__file__ or "").resolve()
    assert path.is_relative_to(MEMORY_SRC), (
        f"{leaf_name} resolved to {path}, outside the legacy tree ({MEMORY_SRC}); "
        "the retained-source pin would be hashing a shadowing installed copy"
    )
    text = path.read_text(encoding="utf-8")
    segments = {
        node.name: ast.get_source_segment(text, node)
        for node in ast.parse(text, filename=leaf_name).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # All *and only* the four declared helpers: a fifth retained definition would
    # be unpinned source shipping in the compatibility tree.
    assert set(segments) == set(pinned), (
        f"{leaf_name} defines {sorted(segments)} at top level, but exactly "
        f"{sorted(pinned)} are pinned"
    )

    for helper in sorted(pinned):
        segment = segments[helper]
        assert segment is not None, f"{leaf_name}.{helper} has no source segment"
        digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()
        assert digest == pinned[helper], (
            f"{leaf_name}.{helper} is no longer the accepted source: its segment "
            f"hashes to {digest}, pinned {pinned[helper]}. The retained helpers are "
            "preserved legacy bodies -- docstring, comments and formatting "
            "included -- so any edit to one has to land as its own reviewed change "
            "to the runtime-owned half, with this pin updated in the same commit."
        )


@pytest.mark.parametrize("leaf_name", sorted(SPLIT_LEAF_SYMBOL_SOURCES))
def test_split_leaf_source_is_exactly_a_future_import_one_route_and_the_helpers(
    leaf_name: str,
) -> None:
    """A split leaf's body (after its docstring) must be exactly: one
    ``from __future__ import annotations``, one absolute
    ``from <canonical route> import (<exact expected portable name set>)``, and one
    synchronous ``def`` per retained helper. No plain ``import``, no third
    from-import, no class, no assignment (``__all__`` included), no
    ``__getattr__``, no decorator, no ``async def`` -- and within the route
    statement no wildcard, no relative import, no alias, and no name outside the
    exact expected set.

    This is the local restatement of ``baseline.facade_manifest``'s
    ``split_facade_defects``, which the frozen route registry enforces for the
    declared ``split_facade`` state. Asserting it here too means the shape is
    pinned by the test suite even for a leaf the registry had not yet reached.
    """
    body = _module_body_after_docstring(leaf_name)
    helpers = _split_leaf_helper_names(leaf_name)
    assert len(body) == 2 + len(helpers), (
        f"{leaf_name}: expected a future import, one route import and "
        f"{len(helpers)} retained defs, found {[ast.dump(node) for node in body]}"
    )

    future_node, route_node = body[0], body[1]
    assert isinstance(future_node, ast.ImportFrom)
    assert future_node.level == 0 and future_node.module == "__future__"
    assert [alias.name for alias in future_node.names] == [
        SPLIT_LEAF_FUTURE_BINDING[leaf_name]
    ]
    assert all(alias.asname is None for alias in future_node.names)

    assert isinstance(route_node, ast.ImportFrom), (
        f"{leaf_name}: expected the route import second, found {ast.dump(route_node)}"
    )
    assert route_node.level == 0, f"{leaf_name}: relative import is not allowed"
    expected_source = SPLIT_LEAF_IMPORT_SOURCE[leaf_name]
    assert route_node.module == expected_source, (
        f"{leaf_name}: imports from {route_node.module!r}, expected exactly "
        f"{expected_source!r}"
    )
    names: set[str] = set()
    for alias in route_node.names:
        assert alias.name != "*", f"{leaf_name}: star import is not allowed"
        assert alias.asname is None, (
            f"{leaf_name}: {alias.name!r} uses a rename/dynamic alias, not a plain import"
        )
        names.add(alias.name)
    assert names == set(SPLIT_LEAF_SYMBOL_SOURCES[leaf_name]), (
        f"{leaf_name}: imports {sorted(names)} from {expected_source!r}, expected "
        f"exactly {sorted(SPLIT_LEAF_SYMBOL_SOURCES[leaf_name])}"
    )

    for node, helper in zip(body[2:], helpers, strict=True):
        assert isinstance(node, ast.FunctionDef), (
            f"{leaf_name}: expected the retained def {helper!r}, found {ast.dump(node)}"
        )
        assert node.name == helper
        assert not node.decorator_list, f"{leaf_name}: {helper!r} must not be decorated"

    module = importlib.import_module(leaf_name)
    assert "__getattr__" not in vars(module)


def test_split_leaf_source_satisfies_the_frozen_registry_source_policy() -> None:
    """The registry's own fail-closed policy, run against the real file: the state
    declared in ``compatibility/facade-routes.v1.json`` and the shape asserted
    above must be the same claim, checked by the same code the acceptance gate
    runs."""
    manifest = load_manifest()
    for leaf_name in sorted(SPLIT_LEAF_SYMBOL_SOURCES):
        route = manifest.route_for_legacy(leaf_name)
        assert route.migration_state is MigrationState.SPLIT_FACADE
        assert route.is_converted
        assert route.canonical_module == SPLIT_LEAF_IMPORT_SOURCE[leaf_name]
        source = Path(importlib.import_module(leaf_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        assert (
            split_facade_defects(ast.parse(source), route.canonical_module) == []
        ), split_facade_defects(ast.parse(source), route.canonical_module)


def test_split_leaf_descriptors_match_the_frozen_baseline_exactly() -> None:
    """Frozen descriptor comparison, both halves at once. Every routed portable
    definition must describe identically to the frozen Phase 0 baseline once the
    ownership move is applied, and every retained helper must describe identically
    on its unchanged legacy owner -- so a structural contract change cannot hide
    behind either half of the split.
    """
    frozen = json.loads(
        (REPO_ROOT / "baseline" / "inventories" / "public-exports.json").read_text(
            encoding="utf-8"
        )
    )["modules"]

    for legacy_module, routed in sorted(FACADE_ROUTES.items()):
        if not legacy_module.startswith("omnivia_memory.graph."):
            continue
        frozen_defines = frozen[legacy_module]["defines"]
        for symbol, canonical_module in sorted(routed.items()):
            canonical = importlib.import_module(canonical_module)
            assert describe_symbol(getattr(canonical, symbol)) == frozen_defines[symbol], (
                f"{canonical_module}.{symbol} no longer describes as the frozen "
                f"{legacy_module}.{symbol} did"
            )

    for leaf_name, helpers in sorted(SPLIT_LEAF_RETAINED_HELPERS.items()):
        module = importlib.import_module(leaf_name)
        frozen_defines = frozen[leaf_name]["defines"]
        for helper, _signature in helpers:
            assert describe_symbol(getattr(module, helper)) == frozen_defines[helper], (
                f"{leaf_name}.{helper} drifted from its frozen historical definition"
            )
        # The frozen leaf recorded exactly the routed records plus these helpers.
        assert set(frozen_defines) == set(FACADE_ROUTES[leaf_name]) | {
            helper for helper, _signature in helpers
        }


def test_graph_routes_cover_exactly_the_portable_owned_definitions() -> None:
    """The eleven routed names, restated: eight on the models leaf and three on the
    split leaf. The four retained helpers must be absent from the route map -- a
    routed symbol is one the legacy module no longer defines, and these four still
    do."""
    assert FACADE_ROUTES["omnivia_memory.graph.models"] == {
        name: "omnivia_core.graph.models"
        for name in (
            "ApprovalStatus",
            "Entity",
            "EntityCreate",
            "EntityMemoryLink",
            "EntityType",
            "Relationship",
            "RelationshipCreate",
            "RelationshipType",
        )
    }
    assert FACADE_ROUTES[SPLIT_LEAF] == {
        name: SPLIT_CANONICAL
        for name in ("GraphSearchQuery", "GraphSearchResult", "GraphSearchResultSet")
    }
    routed = {*FACADE_ROUTES["omnivia_memory.graph.models"], *FACADE_ROUTES[SPLIT_LEAF]}
    assert len(routed) == 11
    assert routed.isdisjoint(_split_leaf_helper_names(SPLIT_LEAF))


def test_graph_hybrid_barrel_is_held_out_of_the_equal_all_gates() -> None:
    """The barrel's two trees advertise *different* surfaces -- the legacy one adds
    the two ``search_service`` exports -- so every gate keyed on
    ``BARREL_ALL_ORDER`` (which asserts ``legacy.__all__ == canonical.__all__``)
    would be wrong for it. Pin that it is absent from those gates, and pin the
    inequality that is the reason.
    """
    assert "graph" not in BARREL_ALL_ORDER
    assert "graph" not in ABSOLUTE_IMPORT_BARRELS
    assert "graph" not in ABSOLUTE_IMPORT_BARREL_IMPORTS
    assert "graph" not in RELATIVE_IMPORT_BARREL_IMPORTS

    legacy = importlib.import_module("omnivia_memory.graph")
    canonical = importlib.import_module("omnivia_core.graph")
    assert tuple(legacy.__all__) == GRAPH_BARREL_ALL
    assert len(legacy.__all__) == 10
    assert len(canonical.__all__) == 8
    assert set(canonical.__all__) == GRAPH_PORTABLE_EXPORTS
    assert len(GRAPH_PORTABLE_EXPORTS) == 8
    assert len(GRAPH_RUNTIME_EXPORTS) == 2


def test_graph_hybrid_barrel_source_is_unchanged_reexport() -> None:
    """The hybrid barrel is *source-unchanged* by this batch: its portable half
    becomes identity-preserving transitively, through its two converted leaves, and
    its runtime half keeps resolving locally. Pin its exact historical shape --
    three absolute ``from omnivia_memory.graph.<leaf> import (...)`` statements in
    source order with their exact ordered name lists, then the ``__all__`` literal
    -- so an edit that reroutes it at ``omnivia_core``, drops the runtime block,
    adds a ``__getattr__``, or reorders its re-exports fails here.
    """
    module_name = "omnivia_memory.graph"
    body = _module_body_after_docstring(module_name)
    assert len(body) == len(GRAPH_BARREL_ABSOLUTE_IMPORTS) + 1, (
        f"{module_name}: expected exactly {len(GRAPH_BARREL_ABSOLUTE_IMPORTS)} absolute "
        f"imports plus __all__, found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(body, GRAPH_BARREL_ABSOLUTE_IMPORTS, strict=False):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {node!r}"
        assert node.level == 0, f"{module_name}: the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {all_node!r}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert tuple(
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ) == GRAPH_BARREL_ALL

    # Every name the three imports bind is exactly what ``__all__`` advertises: the
    # barrel adds nothing of its own and hides nothing it imported. In particular
    # it never re-exported the four retained scoring helpers.
    imported = sorted(name for _, names in GRAPH_BARREL_ABSOLUTE_IMPORTS for name in names)
    assert imported == sorted(GRAPH_BARREL_ALL)
    assert set(imported).isdisjoint(_split_leaf_helper_names(SPLIT_LEAF))
    assert "__getattr__" not in vars(importlib.import_module(module_name))


def test_graph_hybrid_barrel_portable_exports_hop_through_their_facades() -> None:
    """The barrel's eight portable exports must each be the exact object bound at
    the *legacy child* it re-exports from, and that object must in turn be the
    canonical one. A barrel that started sourcing a name from somewhere else would
    still pass the canonical-identity check alone; requiring the leaf hop too is
    what pins the transitive route through both converted children -- one a plain
    facade, one a split facade.
    """
    barrel = importlib.import_module("omnivia_memory.graph")
    canonical_barrel = importlib.import_module("omnivia_core.graph")
    owners_by_leaf = {
        "omnivia_memory.graph.models": LEAF_SYMBOL_SOURCES["omnivia_memory.graph.models"],
        SPLIT_LEAF: SPLIT_LEAF_SYMBOL_SOURCES[SPLIT_LEAF],
    }
    covered: set[str] = set()
    for legacy_leaf_name, names in GRAPH_BARREL_ABSOLUTE_IMPORTS:
        if legacy_leaf_name == GRAPH_RUNTIME_ONLY_LEAF:
            continue
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        owners = owners_by_leaf[legacy_leaf_name]
        for name in names:
            canonical_owner = importlib.import_module(owners[name])
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.graph.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            assert getattr(barrel, name) is getattr(canonical_owner, name), (
                f"omnivia_memory.graph.{name} is not the exact object bound at "
                f"{owners[name]}.{name}"
            )
            assert getattr(barrel, name) is getattr(canonical_barrel, name), (
                f"omnivia_memory.graph.{name} is not the object the canonical barrel "
                "advertises"
            )
            covered.add(name)
    assert covered == GRAPH_PORTABLE_EXPORTS


def test_graph_hybrid_barrel_runtime_exports_stay_legacy_owned() -> None:
    """The other two exports are the whole reason this barrel is a hybrid. Each must
    still be the exact object bound at its legacy ``search_service`` owner, and that
    owner must still be a real legacy module backed by a file in the compatibility
    tree -- not a facade that quietly acquired a canonical counterpart.
    """
    barrel = importlib.import_module("omnivia_memory.graph")
    assert GRAPH_RUNTIME_ONLY_LEAF not in LEAF_SYMBOL_SOURCES
    assert GRAPH_RUNTIME_ONLY_LEAF not in SPLIT_LEAF_SYMBOL_SOURCES
    legacy_leaf = importlib.import_module(GRAPH_RUNTIME_ONLY_LEAF)
    leaf_path = Path(legacy_leaf.__file__ or "").resolve()
    assert leaf_path.is_relative_to(MEMORY_SRC), (
        f"{GRAPH_RUNTIME_ONLY_LEAF} resolved to {leaf_path}, outside the legacy tree"
    )
    covered: set[str] = set()
    for name in _graph_barrel_block(GRAPH_RUNTIME_ONLY_LEAF):
        assert getattr(barrel, name) is getattr(legacy_leaf, name), (
            f"omnivia_memory.graph.{name} no longer comes from "
            f"{GRAPH_RUNTIME_ONLY_LEAF}.{name}"
        )
        assert getattr(barrel, name).__module__ == GRAPH_RUNTIME_ONLY_LEAF
        covered.add(name)
    assert covered == set(GRAPH_RUNTIME_EXPORTS)


def test_graph_runtime_exports_and_helpers_are_absent_from_the_canonical_barrel() -> None:
    """Neither the two runtime exports nor the four retained helpers may leak into
    Core -- not into its ``__all__`` and not as an attribute. This is what keeps the
    runtime-owned half out of the canonical package rather than merely
    un-advertised there.
    """
    canonical = importlib.import_module("omnivia_core.graph")
    for name in sorted(GRAPH_RUNTIME_EXPORTS | set(_split_leaf_helper_names(SPLIT_LEAF))):
        assert name not in canonical.__all__, (
            f"{name} is runtime-owned and must not be in omnivia_core.graph.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is runtime-owned and must not be an attribute of omnivia_core.graph"
        )


def test_graph_runtime_modules_hold_canonical_objects_and_legacy_helpers() -> None:
    """``graph.repository``, ``graph.service`` and ``graph.search_service`` are
    unconverted runtime leaves that import their contracts *from the converted
    facades*. Pin both halves of that hop: every model and record they hold is the
    canonical object, and the four scoring helpers the search service calls are
    still the legacy module's own.
    """
    repository = importlib.import_module("omnivia_memory.graph.repository")
    service = importlib.import_module("omnivia_memory.graph.service")
    search_service = importlib.import_module("omnivia_memory.graph.search_service")
    canonical_models = importlib.import_module("omnivia_core.graph.models")
    canonical_records = importlib.import_module(SPLIT_CANONICAL)
    split_leaf = importlib.import_module(SPLIT_LEAF)

    for name in ("ApprovalStatus", "Entity", "EntityType", "Relationship", "RelationshipType"):
        assert getattr(repository, name) is getattr(canonical_models, name)
    for name in ("Entity", "EntityType", "Relationship", "RelationshipType"):
        assert getattr(service, name) is getattr(canonical_models, name)
    for name in ("ApprovalStatus", "Entity", "EntityType"):
        assert getattr(search_service, name) is getattr(canonical_models, name)
    for name in ("GraphSearchQuery", "GraphSearchResult", "GraphSearchResultSet"):
        assert getattr(search_service, name) is getattr(canonical_records, name)

    for helper in ("compute_relevance_score", "score_name_match"):
        assert getattr(search_service, helper) is getattr(split_leaf, helper), (
            f"omnivia_memory.graph.search_service.{helper} is no longer the legacy "
            "module's own helper"
        )
        assert getattr(search_service, helper).__module__ == SPLIT_LEAF

    # Their source is unchanged: each still reaches only its own legacy siblings and
    # never the canonical package directly.
    for module_name in GRAPH_RUNTIME_MODULES:
        source = Path(importlib.import_module(module_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        assert canonical_imports(ast.parse(source)) == [], (
            f"{module_name} now imports omnivia_core directly; the runtime must keep "
            "reaching its contracts through the legacy facades"
        )


def test_graph_helper_name_match_scoring_edges() -> None:
    """The exact-match, substring-coverage, word-overlap and empty-input branches
    of ``score_name_match``, reached through the legacy module that still owns it.
    """
    module = importlib.import_module(SPLIT_LEAF)
    assert module.score_name_match("Alice", "alice") == 1.0
    assert module.score_name_match("ALICE", "Alice") == 1.0
    # Substring: coverage of the query within the name.
    assert module.score_name_match("ali", "alice") == 3 / 5
    # Word overlap: neither exact nor a substring, but one of two query words hits.
    assert module.score_name_match("alice bob", "carol alice") == 0.5
    assert module.score_name_match("alice bob", "carol dave") == 0.0
    for query, name in (("", "alice"), ("alice", ""), ("", "")):
        assert module.score_name_match(query, name) == 0.0


def test_graph_helper_relationship_count_scoring_edges() -> None:
    """``score_relationship_count`` is zero at and below zero total, positive and
    logarithmic above it, capped at 1.0, and keeps its function-local ``import
    math`` -- which is part of the preserved body, not the module's surface."""
    module = importlib.import_module(SPLIT_LEAF)
    assert module.score_relationship_count(0, 0) == 0.0
    assert module.score_relationship_count(-1, 0) == 0.0
    assert module.score_relationship_count(1, 0) == pytest.approx(0.1)
    assert module.score_relationship_count(0, 1) == pytest.approx(0.1)
    ten = module.score_relationship_count(6, 4)
    assert 0.3 < ten < 0.4
    assert ten == pytest.approx(math.log2(11) / 10.0)
    assert module.score_relationship_count(10**40, 0) == 1.0
    assert "math" not in vars(module), (
        "the `import math` inside score_relationship_count must stay function-local"
    )


def test_graph_helper_neighbor_overlap_scoring_edges() -> None:
    """``score_neighbor_overlap`` is zero when either side is empty, and otherwise
    the case-insensitive overlap as a fraction of the *keywords*."""
    module = importlib.import_module(SPLIT_LEAF)
    assert module.score_neighbor_overlap([], ["alice"]) == 0.0
    assert module.score_neighbor_overlap(["alice"], []) == 0.0
    assert module.score_neighbor_overlap(["Alice", "Bob"], ["alice", "carol"]) == 0.5
    assert module.score_neighbor_overlap(["Alice"], ["ALICE"]) == 1.0
    # Denominator is the keyword set, so duplicate keywords collapse.
    assert module.score_neighbor_overlap(["alice"], ["alice", "Alice"]) == 1.0


def test_graph_helper_composite_score_weighting_and_rounding() -> None:
    """``compute_relevance_score`` normalizes its weights, returns 0.0 on a
    non-positive total weight, rounds to four places, and composes exactly the three
    component helpers -- so a reweighting or a dropped signal is visible."""
    module = importlib.import_module(SPLIT_LEAF)

    # Weights are normalized, so scaling them all changes nothing.
    default = module.compute_relevance_score("alice", "Alice Smith", 3, 2, ["bob"])
    assert default == module.compute_relevance_score(
        "alice",
        "Alice Smith",
        3,
        2,
        ["bob"],
        name_weight=5.0,
        relationship_weight=2.5,
        neighbor_weight=2.5,
    )

    # ...and it is exactly the weighted combination of the three helpers, rounded.
    name = module.score_name_match("alice", "Alice Smith")
    relationships = module.score_relationship_count(3, 2)
    neighbors = module.score_neighbor_overlap(["bob"], ["alice"])
    assert default == round(0.5 * name + 0.25 * relationships + 0.25 * neighbors, 4)
    assert default == round(default, 4)

    # A single signal can be isolated by zeroing the other two weights.
    assert module.compute_relevance_score(
        "alice",
        "Alice",
        name_weight=1.0,
        relationship_weight=0.0,
        neighbor_weight=0.0,
    ) == 1.0

    # A non-positive total weight short-circuits to 0.0 rather than dividing by it.
    for weights in ((0.0, 0.0, 0.0), (-1.0, 0.5, 0.5), (-1.0, 0.0, 0.0)):
        name_weight, relationship_weight, neighbor_weight = weights
        assert module.compute_relevance_score(
            "alice",
            "Alice",
            name_weight=name_weight,
            relationship_weight=relationship_weight,
            neighbor_weight=neighbor_weight,
        ) == 0.0

    # Neighbour names default to none, and the query's own words are the keywords.
    assert module.compute_relevance_score("alice", "Alice") == round(0.5 * 1.0, 4)
    assert module.compute_relevance_score("alice", "Alice", neighbor_names=["alice"]) == (
        round(0.5 * 1.0 + 0.25 * 1.0, 4)
    )


def test_graph_records_behave_identically_through_both_import_paths() -> None:
    """The routed records are the same objects, so this is not a cross-tree
    comparison: it is proof that those exact objects still round-trip and validate
    correctly when reached through the legacy split leaf and the hybrid barrel --
    the two paths no per-symbol identity check exercises."""
    barrel = importlib.import_module("omnivia_memory.graph")
    leaf = importlib.import_module(SPLIT_LEAF)
    models = importlib.import_module("omnivia_memory.graph.models")

    entity = models.Entity(id="e1", name="Alice", entity_type=models.EntityType.PERSON)
    query = barrel.GraphSearchQuery(query="alice", depth=2, limit=5)
    result = leaf.GraphSearchResult(entity=entity, score=0.5, matched_on="name")
    result_set = barrel.GraphSearchResultSet(results=[result], total_count=1, query=query)

    payload = result_set.to_dict()
    assert leaf.GraphSearchResultSet.from_dict(payload).to_dict() == payload
    assert payload["query"] == {
        "query": "alice",
        "entity_types": [],
        "relationship_types": [],
        "depth": 2,
        "limit": 5,
    }

    with pytest.raises(ValueError, match="Depth must be non-negative"):
        barrel.GraphSearchQuery(query="alice", depth=-1)
    with pytest.raises(ValueError, match="Limit must be at least 1"):
        leaf.GraphSearchQuery(query="alice", limit=0)
    with pytest.raises(ValueError, match="Score must be between 0.0 and 1.0"):
        barrel.GraphSearchResult(entity=entity, score=1.5, matched_on="name")


#: Fresh-process import orders for the Graph pair. Each is a full order, not a
#: prefix: whichever module is named first is the one that gets to define the
#: shared objects, so an order that only works because something else was imported
#: earlier fails here.
GRAPH_IMPORT_ORDERS: dict[str, tuple[str, ...]] = {
    "canonical-first": (
        "omnivia_core.graph.models",
        "omnivia_core.graph.search_models",
        "omnivia_memory.graph.models",
        SPLIT_LEAF,
    ),
    "legacy-leaf-first": (
        "omnivia_memory.graph.models",
        SPLIT_LEAF,
        "omnivia_core.graph.models",
        "omnivia_core.graph.search_models",
    ),
    "canonical-barrel-first": (
        "omnivia_core.graph",
        "omnivia_memory.graph",
    ),
    "legacy-barrel-first": (
        "omnivia_memory.graph",
        "omnivia_core.graph",
    ),
    "search-service-first": (
        "omnivia_memory.graph.search_service",
        "omnivia_core.graph",
        "omnivia_core.graph.search_models",
    ),
    "reverse": (
        SPLIT_LEAF,
        "omnivia_memory.graph.models",
        "omnivia_memory.graph",
        "omnivia_core.graph",
        "omnivia_core.graph.search_models",
        "omnivia_core.graph.models",
    ),
    "repeated": (
        "omnivia_core.graph.search_models",
        SPLIT_LEAF,
        "omnivia_core.graph.search_models",
        SPLIT_LEAF,
        "omnivia_memory.graph",
        "omnivia_core.graph",
        "omnivia_memory.graph",
        "omnivia_core.graph",
    ),
}


def _graph_identity_script(import_order: tuple[str, ...]) -> str:
    lines = [
        "import importlib",
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        f"for module_name in {import_order!r}:",
        "    importlib.import_module(module_name)",
        # Everything asserted below must be reachable regardless of the order under
        # test, so pull in whatever that order did not name.
        "for module_name in (",
        "    'omnivia_core.graph',",
        "    'omnivia_core.graph.models',",
        "    'omnivia_core.graph.search_models',",
        "    'omnivia_memory.graph',",
        "    'omnivia_memory.graph.models',",
        f"    {SPLIT_LEAF!r},",
        "):",
        "    importlib.import_module(module_name)",
        "import omnivia_core.graph",
        "import omnivia_core.graph.models",
        "import omnivia_core.graph.search_models",
        "import omnivia_memory.graph",
        "import omnivia_memory.graph.models",
        f"import {SPLIT_LEAF}",
    ]
    for legacy_module, symbols in (
        ("omnivia_memory.graph.models", LEAF_SYMBOL_SOURCES["omnivia_memory.graph.models"]),
        (SPLIT_LEAF, SPLIT_LEAF_SYMBOL_SOURCES[SPLIT_LEAF]),
    ):
        for symbol, canonical_module in symbols.items():
            lines.append(
                f"assert {legacy_module}.{symbol} is {canonical_module}.{symbol}, "
                f"'{legacy_module}.{symbol} is not {canonical_module}.{symbol}'"
            )
    for name in sorted(GRAPH_PORTABLE_EXPORTS):
        lines.append(
            f"assert omnivia_memory.graph.{name} is omnivia_core.graph.{name}, "
            f"'the hybrid barrel stopped publishing the canonical {name}'"
        )
    for name in sorted(GRAPH_RUNTIME_EXPORTS):
        lines.append(
            f"assert not hasattr(omnivia_core.graph, {name!r}), "
            f"'{name} leaked into the canonical barrel'"
        )
    for helper in _split_leaf_helper_names(SPLIT_LEAF):
        lines.append(
            f"assert {SPLIT_LEAF}.{helper}.__module__ == {SPLIT_LEAF!r}, "
            f"'{helper} is no longer legacy-owned'"
        )
        lines.append(
            f"assert not hasattr({SPLIT_CANONICAL}, {helper!r}), "
            f"'{SPLIT_CANONICAL} acquired {helper}'"
        )
    # No duplicate class or helper objects anywhere in the loaded closure: exactly
    # one object per contract name across both trees, and exactly one per helper.
    lines.extend(
        [
            "records = {}",
            "for module_name, module in sorted(sys.modules.items()):",
            "    if not (module_name == 'omnivia_core' or module_name.startswith('omnivia_')):",
            "        continue",
            f"    for name in {sorted(GRAPH_PORTABLE_EXPORTS)!r}:",
            "        value = getattr(module, name, None)",
            "        if value is None or getattr(value, '__module__', '').startswith('omnivia_') is False:",
            "            continue",
            "        records.setdefault(name, set()).add(id(value))",
            "duplicated = sorted(name for name, ids in records.items() if len(ids) != 1)",
            "assert not duplicated, f'duplicate objects for {duplicated}'",
            f"assert set(records) == set({sorted(GRAPH_PORTABLE_EXPORTS)!r})",
            "helpers = {}",
            f"for name in {list(_split_leaf_helper_names(SPLIT_LEAF))!r}:",
            "    for module_name, module in sorted(sys.modules.items()):",
            "        if not (module_name == 'omnivia_core' or module_name.startswith('omnivia_')):",
            "            continue",
            "        value = getattr(module, name, None)",
            "        if value is None:",
            "            continue",
            "        helpers.setdefault(name, set()).add(id(value))",
            "duplicated = sorted(name for name, ids in helpers.items() if len(ids) != 1)",
            "assert not duplicated, f'duplicate helper objects for {duplicated}'",
            f"assert set(helpers) == set({list(_split_leaf_helper_names(SPLIT_LEAF))!r})",
        ]
    )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "order_name", sorted(GRAPH_IMPORT_ORDERS), ids=sorted(GRAPH_IMPORT_ORDERS)
)
def test_graph_fresh_process_import_orders_preserve_identity(order_name: str) -> None:
    """Seven fresh processes, one per order. A shared process would hide an order
    that only works because an earlier test's imports had already settled which
    module defines what -- which is exactly the failure mode a split facade over a
    hybrid barrel could introduce.
    """
    _run_isolated(_graph_identity_script(GRAPH_IMPORT_ORDERS[order_name]))


def test_canonical_graph_closure_loads_neither_the_runtime_nor_omnivia_memory() -> None:
    """A canonical-only Graph import must reach ``omnivia_memory`` not at all --
    and in particular not the three graph runtime leaves, whose repository reaches
    SQLite through ``omnivia_memory.persistence``. The exact canonical closure is
    pinned too, so a canonical leaf that started importing a sibling domain fails
    here rather than growing the closure quietly. Only ``src`` goes on the path.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.graph",
            "import omnivia_core.graph.models",
            "import omnivia_core.graph.search_models",
            "assert 'omnivia_memory' not in sys.modules",
            "loaded = {",
            "    name for name in sys.modules",
            "    if name == 'omnivia_core' or name.startswith('omnivia_core.')",
            "}",
            f"expected = set({sorted(GRAPH_CANONICAL_MODULE_CLOSURE)!r})",
            "assert loaded == expected, sorted(loaded ^ expected)",
            f"forbidden = set({list(GRAPH_FORBIDDEN_MODULE_ROOTS)!r})",
            "leaked = sorted(forbidden & {name.split('.')[0] for name in sys.modules})",
            "assert not leaked, leaked",
            "runtime = sorted(",
            "    name for name in sys.modules",
            "    if name.endswith(('.repository', '.service', '.search_service'))",
            ")",
            "assert not runtime, runtime",
        ]
    )
    _run_isolated(script)


def test_neither_package_root_exposes_any_graph_symbol() -> None:
    """Both roots are deliberately unedited by this batch, and neither has ever
    re-exported a Graph name -- the legacy root imports from the ``knowledge`` and
    ``memory_graph`` barrels, not ``graph``. That is what makes this the first
    converted leaf set that moves *no* frozen root binding, so it is pinned rather
    than left implicit: a root that started re-exporting one of these would need a
    declared root-binding owner move, and there is none.
    """
    graph_names = sorted(
        set(GRAPH_BARREL_ALL)
        | {"EntityCreate", "EntityMemoryLink", "RelationshipCreate"}
        | set(_split_leaf_helper_names(SPLIT_LEAF))
    )
    assert len(graph_names) == 17
    for root_name in ("omnivia_memory", "omnivia_core"):
        root = importlib.import_module(root_name)
        present = [name for name in graph_names if hasattr(root, name)]
        assert present == [], f"{root_name} now re-exports Graph symbols {present}"
        advertised = [name for name in graph_names if name in getattr(root, "__all__", ())]
        assert advertised == []

    # ...and the graph barrel is not one of the packages either root imports from.
    for root_name in ("omnivia_memory", "omnivia_core"):
        source = Path(importlib.import_module(root_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        reached = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert f"{root_name}.graph" not in reached


def test_graph_conversion_declares_no_descriptor_rewrite_or_root_owner_move() -> None:
    """Both Graph leaves use ``from __future__ import annotations``, so every frozen
    signature they recorded is already a string forward reference that never named a
    package -- there is no descriptor text for the ownership move to change. And
    neither leaf owns a root binding (see the test above), so no root-binding owner
    move follows either. Pin that the two declaration maps carry nothing for Graph,
    and that the two unrelated entries they do carry are untouched.
    """
    assert not any(
        legacy_module.startswith("omnivia_memory.graph")
        for legacy_module, _symbol in FACADE_DESCRIPTOR_REWRITES
    )
    assert not any(
        legacy_module.startswith("omnivia_memory.graph")
        for _binding, legacy_module in FACADE_ROOT_BINDING_OWNER_MOVES
    )
    assert set(FACADE_ROOT_BINDING_OWNER_MOVES) == {
        ("RUN_LEDGER_CONTRACT_VERSION", "omnivia_memory.run_ledger.models"),
        ("CONTROL_PLANE_CONTRACT_VERSION", "omnivia_memory.control_plane.models"),
    }
    assert set(FACADE_DESCRIPTOR_REWRITES) == {
        ("omnivia_memory.app_manifest.validation", "validate_app_manifest"),
        ("omnivia_memory.module_manifest.validation", "validate_module_manifest"),
    }


# ---------------------------------------------------------------------------
# The ``ingestion`` pair: two direct facades under two hybrid barrels.
#
# ``ingestion.models`` and ``ingestion.watcher.models`` are plain direct facades
# -- one import each, nothing retained -- but neither barrel above them can
# follow. Fourteen of the ``ingestion`` barrel's nineteen exports are owned by
# the runtime-only ``chunker``/``extractors``/``pipeline``/``repositories``/
# ``scanner`` leaves, and two of the ``ingestion.watcher`` barrel's twelve by the
# runtime-only ``debouncer``/``tracker``. Both are recorded as ``hybrid_facade``
# in ``compatibility/facade-routes.v1.json``, both trees' ``__all__`` differ in
# sizes as a result, and both barrels therefore stay out of ``BARREL_ALL_ORDER``
# and every gate built on it.
#
# Two collisions are load-bearing here and are pinned rather than left implicit:
# ``Source`` (the ingestion record vs. the provenance one the legacy root binds)
# and ``SourceReference`` (the watcher models record vs. the *distinct* dataclass
# the runtime-only ``watcher.tracker`` defines for itself).
# ---------------------------------------------------------------------------

INGESTION_MODELS_LEAF = "omnivia_memory.ingestion.models"
INGESTION_MODELS_CANONICAL = "omnivia_core.ingestion.models"
WATCHER_MODELS_LEAF = "omnivia_memory.ingestion.watcher.models"
WATCHER_MODELS_CANONICAL = "omnivia_core.ingestion.watcher.models"

#: The exact, ordered *absolute* re-export shape the unchanged legacy
#: ``ingestion`` barrel must still have: ``(absolute module, imported names in
#: source order)``. Six blocks, in the barrel's own historical order -- which is
#: alphabetical by module, so the single portable block sits third, between the
#: two extractor/chunker runtime blocks and the three pipeline/repository/scanner
#: ones. Restated here rather than read off the barrel, because this is the file
#: whose edits it exists to reject.
INGESTION_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.ingestion.chunker",
        (
            "BaseChunker",
            "CharacterChunker",
            "ChunkConfig",
            "ParagraphChunker",
        ),
    ),
    (
        "omnivia_memory.ingestion.extractors",
        (
            "BaseExtractor",
            "DOCXExtractor",
            "MarkdownExtractor",
            "PDFExtractor",
        ),
    ),
    (
        INGESTION_MODELS_LEAF,
        (
            "Chunk",
            "ExtractionResult",
            "FileType",
            "ParseStatus",
            "Source",
        ),
    ),
    (
        "omnivia_memory.ingestion.pipeline",
        (
            "IngestResult",
            "IngestionPipeline",
        ),
    ),
    (
        "omnivia_memory.ingestion.repositories",
        ("ChunkRepository",),
    ),
    (
        "omnivia_memory.ingestion.scanner",
        (
            "FileInfo",
            "FileScanner",
            "ScanOptions",
        ),
    ),
)

#: The barrel's exact ordered 19-name ``__all__`` literal, restated rather than
#: derived: it is sorted, so it interleaves all six blocks' names and matches
#: none of them.
INGESTION_BARREL_ALL: tuple[str, ...] = (
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
)

#: The barrel's five runtime-only children, each declared runtime-only in the
#: frozen route registry and deliberately not a facade.
INGESTION_RUNTIME_ONLY_LEAVES: tuple[str, ...] = (
    "omnivia_memory.ingestion.chunker",
    "omnivia_memory.ingestion.extractors",
    "omnivia_memory.ingestion.pipeline",
    "omnivia_memory.ingestion.repositories",
    "omnivia_memory.ingestion.scanner",
)

#: The barrel's exact fourteen runtime-only exports: they must stay legacy-owned
#: and must never appear on the canonical barrel.
INGESTION_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
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
    }
)

#: The barrel's exact five portable exports: everything else, all of which must
#: hop through the converted models child to a canonical object.
INGESTION_PORTABLE_EXPORTS: frozenset[str] = frozenset(INGESTION_BARREL_ALL) - (
    INGESTION_RUNTIME_EXPORTS
)

#: Routed symbols of the models leaf that the barrel has never re-exported, and
#: must not start to. ``FileInventory`` is a scanner-facing record and
#: ``IngestSource`` is the leaf's own alias for ``Source``; both are importable
#: from the leaf and absent from both trees' barrels.
INGESTION_LEAF_ONLY_ROUTES: tuple[str, ...] = ("FileInventory", "IngestSource")

#: The exact, ordered *absolute* re-export shape the unchanged legacy
#: ``ingestion.watcher`` barrel must still have. Three blocks, in the barrel's own
#: historical order: the runtime ``debouncer``, the portable ``models``, then the
#: runtime ``tracker``.
WATCHER_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "omnivia_memory.ingestion.watcher.debouncer",
        ("Debouncer",),
    ),
    (
        WATCHER_MODELS_LEAF,
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
    (
        "omnivia_memory.ingestion.watcher.tracker",
        ("SourceTracker",),
    ),
)

#: The barrel's exact ordered 12-name ``__all__`` literal. Unlike the ``ingestion``
#: barrel's, this one is *not* sorted -- ``Debouncer`` leads, ahead of
#: ``DebounceConfig`` -- which is exactly why it is restated rather than derived.
WATCHER_BARREL_ALL: tuple[str, ...] = (
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
)

#: The watcher barrel's two runtime-only children and its two runtime-only
#: exports.
WATCHER_RUNTIME_ONLY_LEAVES: tuple[str, ...] = (
    "omnivia_memory.ingestion.watcher.debouncer",
    "omnivia_memory.ingestion.watcher.tracker",
)
WATCHER_RUNTIME_EXPORTS: frozenset[str] = frozenset({"Debouncer", "SourceTracker"})
WATCHER_PORTABLE_EXPORTS: frozenset[str] = frozenset(WATCHER_BARREL_ALL) - (
    WATCHER_RUNTIME_EXPORTS
)

#: The ingestion runtime modules that stay legacy-owned and unedited by this
#: batch, and the canonical names each must now hold. ``memory_graph``'s
#: ``ingestion_adapter`` is included: it is a runtime-only leaf of *another*
#: domain that consumes this one's contracts, so this batch changes what it holds
#: without changing its source.
INGESTION_RUNTIME_CONSUMERS: dict[str, tuple[str, ...]] = {
    "omnivia_memory.ingestion.chunker": ("Chunk",),
    "omnivia_memory.ingestion.extractors": ("ExtractionResult",),
    "omnivia_memory.ingestion.pipeline": ("Chunk", "FileType", "ParseStatus", "Source"),
    "omnivia_memory.ingestion.repositories": (
        "Chunk",
        "FileType",
        "ParseStatus",
        "Source",
    ),
    "omnivia_memory.ingestion.scanner": ("FileType",),
    "omnivia_memory.memory_graph.ingestion_adapter": (
        "Chunk",
        "FileType",
        "ParseStatus",
        "Source",
    ),
}
WATCHER_RUNTIME_CONSUMERS: dict[str, tuple[str, ...]] = {
    "omnivia_memory.ingestion.watcher.debouncer": (
        "DebounceConfig",
        "FileChange",
        "FileChangeBatch",
    ),
}

#: The exact canonical closure a canonical-only ingestion import may produce.
#: Anything else -- a sibling domain, a runtime leaf -- is a leak.
INGESTION_CANONICAL_MODULE_CLOSURE: frozenset[str] = frozenset(
    {
        "omnivia_core",
        "omnivia_core.ingestion",
        "omnivia_core.ingestion.models",
        "omnivia_core.ingestion.watcher",
        "omnivia_core.ingestion.watcher.models",
    }
)

#: Module roots a canonical-only ingestion import must never load. The ingestion
#: runtime reaches SQLite through ``omnivia_memory.persistence`` and PDF/DOCX
#: extraction through ``fitz``/``docx``, so their absence is part of what "the
#: canonical contract layer stands alone" means here.
INGESTION_FORBIDDEN_MODULE_ROOTS: tuple[str, ...] = (
    "docx",
    "fitz",
    "omnivia_cloud",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "omnivia_core_runtime",
    "omnivia_dev",
    "omnivia_memory",
    "omnivia_platform",
    "sqlalchemy",
    "sqlite3",
)


def test_ingestion_hybrid_barrels_are_held_out_of_the_equal_all_gates() -> None:
    """Both barrels' two trees advertise *different* surfaces, so every gate keyed
    on ``BARREL_ALL_ORDER`` (which asserts ``legacy.__all__ == canonical.__all__``)
    would be wrong for them. Pin that they are absent from those gates, and pin the
    inequality that is the reason -- so a future edit that "helpfully" added either
    to ``BARREL_ALL_ORDER`` fails here with the reason rather than as a confusing
    list mismatch.
    """
    for barrel in ("ingestion", "ingestion.watcher"):
        assert barrel not in BARREL_ALL_ORDER
        assert barrel not in ABSOLUTE_IMPORT_BARRELS
        assert barrel not in ABSOLUTE_IMPORT_BARREL_IMPORTS
        assert barrel not in RELATIVE_IMPORT_BARREL_IMPORTS

    legacy = importlib.import_module("omnivia_memory.ingestion")
    canonical = importlib.import_module("omnivia_core.ingestion")
    assert tuple(legacy.__all__) == INGESTION_BARREL_ALL
    assert len(legacy.__all__) == 19
    assert len(canonical.__all__) == 5
    assert set(canonical.__all__) == set(INGESTION_BARREL_ALL) - (
        INGESTION_RUNTIME_EXPORTS
    )

    legacy_watcher = importlib.import_module("omnivia_memory.ingestion.watcher")
    canonical_watcher = importlib.import_module("omnivia_core.ingestion.watcher")
    assert tuple(legacy_watcher.__all__) == WATCHER_BARREL_ALL
    assert len(legacy_watcher.__all__) == 12
    assert len(canonical_watcher.__all__) == 10
    assert set(canonical_watcher.__all__) == set(WATCHER_BARREL_ALL) - (
        WATCHER_RUNTIME_EXPORTS
    )


@pytest.mark.parametrize(
    ("module_name", "blocks", "expected_all"),
    [
        (
            "omnivia_memory.ingestion",
            INGESTION_BARREL_ABSOLUTE_IMPORTS,
            INGESTION_BARREL_ALL,
        ),
        (
            "omnivia_memory.ingestion.watcher",
            WATCHER_BARREL_ABSOLUTE_IMPORTS,
            WATCHER_BARREL_ALL,
        ),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_hybrid_barrel_source_is_unchanged_reexport(
    module_name: str,
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    expected_all: tuple[str, ...],
) -> None:
    """Both hybrid barrels are *source-unchanged* by this slice: each one's
    portable half becomes identity-preserving transitively, through its converted
    ``models`` child, and its runtime half keeps resolving locally. Pin their exact
    historical shape -- the absolute ``from omnivia_memory.<pkg>.<leaf> import
    (...)`` statements in source order with their exact ordered name lists, then
    the ``__all__`` literal -- so an edit that reroutes either at ``omnivia_core``,
    drops the runtime blocks, adds a ``__getattr__``, or reorders its re-exports
    fails here.
    """
    body = _module_body_after_docstring(module_name)
    assert len(body) == len(blocks) + 1, (
        f"{module_name}: expected exactly {len(blocks)} absolute imports plus "
        f"__all__, found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(body, blocks, strict=False):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {node!r}"
        assert node.level == 0, f"{module_name}: the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {all_node!r}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert tuple(
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ) == expected_all

    # Every name the imports bind is exactly what ``__all__`` advertises: the
    # barrel adds nothing of its own and hides nothing it imported.
    imported = sorted(name for _, names in blocks for name in names)
    assert imported == sorted(expected_all)
    assert "__getattr__" not in vars(importlib.import_module(module_name))


@pytest.mark.parametrize(
    ("barrel_name", "blocks", "runtime_only", "portable_count"),
    [
        (
            "omnivia_memory.ingestion",
            INGESTION_BARREL_ABSOLUTE_IMPORTS,
            INGESTION_RUNTIME_ONLY_LEAVES,
            5,
        ),
        (
            "omnivia_memory.ingestion.watcher",
            WATCHER_BARREL_ABSOLUTE_IMPORTS,
            WATCHER_RUNTIME_ONLY_LEAVES,
            10,
        ),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_hybrid_barrel_portable_exports_hop_through_their_facade(
    barrel_name: str,
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    runtime_only: tuple[str, ...],
    portable_count: int,
) -> None:
    """Each barrel's portable exports must be the exact object bound at the
    *legacy child facade* it re-exports from, and that object must in turn be the
    canonical one. A barrel that started sourcing a name from somewhere else would
    still pass the canonical-identity check alone; requiring the leaf hop too is
    what pins the transitive route through the converted child.
    """
    barrel = importlib.import_module(barrel_name)
    portable = 0
    for legacy_leaf_name, names in blocks:
        if legacy_leaf_name in runtime_only:
            continue
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        owners = LEAF_SYMBOL_SOURCES[legacy_leaf_name]
        for name in names:
            canonical_owner = importlib.import_module(owners[name])
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"{barrel_name}.{name} no longer comes from {legacy_leaf_name}.{name}"
            )
            assert getattr(barrel, name) is getattr(canonical_owner, name), (
                f"{barrel_name}.{name} is not the exact object bound at "
                f"{owners[name]}.{name}"
            )
            portable += 1
    assert portable == portable_count


@pytest.mark.parametrize(
    ("barrel_name", "blocks", "runtime_only", "runtime_exports"),
    [
        (
            "omnivia_memory.ingestion",
            INGESTION_BARREL_ABSOLUTE_IMPORTS,
            INGESTION_RUNTIME_ONLY_LEAVES,
            INGESTION_RUNTIME_EXPORTS,
        ),
        (
            "omnivia_memory.ingestion.watcher",
            WATCHER_BARREL_ABSOLUTE_IMPORTS,
            WATCHER_RUNTIME_ONLY_LEAVES,
            WATCHER_RUNTIME_EXPORTS,
        ),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_hybrid_barrel_runtime_exports_stay_legacy_owned(
    barrel_name: str,
    blocks: tuple[tuple[str, tuple[str, ...]], ...],
    runtime_only: tuple[str, ...],
    runtime_exports: frozenset[str],
) -> None:
    """The runtime exports are the whole reason these barrels are hybrids, so
    their *non*-conversion is as much a contract as the portable half's
    conversion. Each must still be the exact object bound at its legacy owner, and
    each of those owners must still be a real legacy module backed by a file in
    the compatibility tree -- not a facade that quietly acquired a canonical
    counterpart.
    """
    barrel = importlib.import_module(barrel_name)
    by_module = dict(blocks)
    covered: set[str] = set()
    for legacy_leaf_name in runtime_only:
        assert legacy_leaf_name not in LEAF_SYMBOL_SOURCES, (
            f"{legacy_leaf_name} is runtime-owned and must not become a facade"
        )
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        leaf_path = Path(legacy_leaf.__file__ or "").resolve()
        assert leaf_path.is_relative_to(MEMORY_SRC), (
            f"{legacy_leaf_name} resolved to {leaf_path}, outside the legacy tree"
        )
        for name in by_module[legacy_leaf_name]:
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"{barrel_name}.{name} no longer comes from {legacy_leaf_name}.{name}"
            )
            covered.add(name)
    assert covered == set(runtime_exports)


@pytest.mark.parametrize(
    ("canonical_barrel", "runtime_exports"),
    [
        ("omnivia_core.ingestion", INGESTION_RUNTIME_EXPORTS),
        ("omnivia_core.ingestion.watcher", WATCHER_RUNTIME_EXPORTS),
    ],
    ids=["ingestion", "ingestion.watcher"],
)
def test_ingestion_runtime_exports_are_absent_from_the_canonical_barrel(
    canonical_barrel: str, runtime_exports: frozenset[str]
) -> None:
    """None of the runtime-owned names may leak into Core -- not into its
    ``__all__`` and not as an attribute. This is what keeps the runtime-owned half
    out of the canonical package rather than merely un-advertised there.
    """
    canonical = importlib.import_module(canonical_barrel)
    for name in sorted(runtime_exports):
        assert name not in canonical.__all__, (
            f"{name} is runtime-owned and must not be in {canonical_barrel}.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is runtime-owned and must not be an attribute of "
            f"{canonical_barrel}"
        )


def test_ingestion_barrel_publishes_a_subset_of_its_models_leaf_routed_surface() -> None:
    """The ingestion barrel is source-unchanged, and its historical source names
    only five of the seven symbols its models child now routes.

    ``FileInventory`` and ``IngestSource`` are the concrete cases: both are routed
    symbols of the leaf and importable from it, but the barrel has never
    re-exported either, so both must stay absent from both trees' ``__all__`` and
    from both barrel modules. A "helpful" edit that added one would widen the
    barrel's advertised surface beyond what Phase 0 froze, and every identity
    check in this module would still pass.
    """
    leaf = importlib.import_module(INGESTION_MODELS_LEAF)
    for name in INGESTION_LEAF_ONLY_ROUTES:
        assert name in FACADE_ROUTES[INGESTION_MODELS_LEAF]
        assert hasattr(leaf, name)
    for barrel_name in ("omnivia_memory.ingestion", "omnivia_core.ingestion"):
        barrel = importlib.import_module(barrel_name)
        for name in INGESTION_LEAF_ONLY_ROUTES:
            assert name not in barrel.__all__
            assert not hasattr(barrel, name), (
                f"{barrel_name} must not publish {name}; the barrel's historical "
                "source never imported it"
            )

    # And the barrel's surface really is a subset, not a different set: every name
    # it advertises is bound at the leaf it re-exports from.
    for legacy_leaf_name, names in INGESTION_BARREL_ABSOLUTE_IMPORTS:
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        for name in names:
            assert hasattr(legacy_leaf, name)


def test_ingestion_routes_cover_exactly_the_owned_definitions() -> None:
    """The two leaves' route sets are exactly the symbols the frozen baseline
    recorded them as *defining* -- seven and ten -- and nothing else. The
    incidental bindings their historical namespaces also keep resolving
    (``Any``, ``Path``, ``enum``, ``hashlib``, ``uuid`` and the rest) are
    deliberately absent from ``FACADE_ROUTES``: the baseline never recorded them
    as definitions, so there is no route delta to normalize. They are covered by
    ``LEAF_SYMBOL_SOURCES`` instead.
    """
    assert FACADE_ROUTES[INGESTION_MODELS_LEAF] == {
        "Chunk": INGESTION_MODELS_CANONICAL,
        "ExtractionResult": INGESTION_MODELS_CANONICAL,
        "FileInventory": INGESTION_MODELS_CANONICAL,
        "FileType": INGESTION_MODELS_CANONICAL,
        "IngestSource": INGESTION_MODELS_CANONICAL,
        "ParseStatus": INGESTION_MODELS_CANONICAL,
        "Source": INGESTION_MODELS_CANONICAL,
    }
    assert FACADE_ROUTES[WATCHER_MODELS_LEAF] == {
        "DebounceConfig": WATCHER_MODELS_CANONICAL,
        "FileChange": WATCHER_MODELS_CANONICAL,
        "FileChangeBatch": WATCHER_MODELS_CANONICAL,
        "FileChangeType": WATCHER_MODELS_CANONICAL,
        "IndexerScheduler": WATCHER_MODELS_CANONICAL,
        "IndexerState": WATCHER_MODELS_CANONICAL,
        "IndexerStatus": WATCHER_MODELS_CANONICAL,
        "ScheduledJob": WATCHER_MODELS_CANONICAL,
        "SourceReference": WATCHER_MODELS_CANONICAL,
        "WatchedPath": WATCHER_MODELS_CANONICAL,
    }
    for leaf_name in (INGESTION_MODELS_LEAF, WATCHER_MODELS_LEAF):
        routed = set(FACADE_ROUTES[leaf_name])
        namespace = set(LEAF_SYMBOL_SOURCES[leaf_name])
        assert routed < namespace
        assert len(namespace) == 18


def test_ingest_source_is_the_same_object_through_every_path() -> None:
    """``IngestSource`` is an identity *alias* for this leaf's own ``Source``, not
    a second class. Both trees have to keep it that way, and both names have to
    land on the one canonical dataclass -- an equal-but-distinct copy would pass
    every structural comparison and silently break ``isinstance`` for callers that
    mix the two spellings.
    """
    legacy = importlib.import_module(INGESTION_MODELS_LEAF)
    canonical = importlib.import_module(INGESTION_MODELS_CANONICAL)

    assert canonical.IngestSource is canonical.Source
    assert legacy.IngestSource is legacy.Source
    assert legacy.IngestSource is canonical.Source
    assert legacy.Source is canonical.Source

    # ...and the barrel's ``Source`` is that same object, reached through the leaf.
    barrel = importlib.import_module("omnivia_memory.ingestion")
    canonical_barrel = importlib.import_module("omnivia_core.ingestion")
    assert barrel.Source is canonical.Source
    assert canonical_barrel.Source is canonical.Source


def test_ingestion_source_keeps_its_historical_collision_owner() -> None:
    """``Source`` is a name collision between two independent domains. The
    ingestion domain's ingested-file record is the one this leaf and its barrel
    historically exposed, while the legacy package *root* has always taken its
    ``Source`` from the provenance domain -- and the root does not import from the
    ingestion barrel at all. Routing either side to the other's class would be a
    silent contract swap that every "is the exact canonical object" check above
    would still pass.
    """
    ingestion = importlib.import_module(INGESTION_MODELS_CANONICAL)
    provenance = importlib.import_module("omnivia_core.provenance.models")
    assert ingestion.Source is not provenance.Source

    for legacy_module in (
        INGESTION_MODELS_LEAF,
        "omnivia_memory.ingestion",
        *INGESTION_RUNTIME_CONSUMERS,
    ):
        module = importlib.import_module(legacy_module)
        if not hasattr(module, "Source"):
            continue
        assert module.Source is ingestion.Source, (
            f"{legacy_module}.Source is not the ingestion domain's own record"
        )
        assert module.Source is not provenance.Source, (
            f"{legacy_module}.Source was taken over by the provenance record"
        )

    for legacy_module in (
        "omnivia_memory",
        "omnivia_memory.provenance",
        "omnivia_memory.provenance.models",
        "omnivia_memory.memory.models",
    ):
        module = importlib.import_module(legacy_module)
        assert module.Source is provenance.Source, (
            f"{legacy_module}.Source is not the provenance domain's own record"
        )
        assert module.Source is not ingestion.Source, (
            f"{legacy_module}.Source was taken over by the ingestion record"
        )


def test_watcher_source_reference_keeps_its_historical_collision_owner() -> None:
    """``SourceReference`` collides between the watcher models leaf and the
    runtime-only ``watcher.tracker``, which defines a *distinct* dataclass of its
    own with the same fields. The barrel publishes the models one; the tracker
    keeps using its own for every annotation and container it builds. Neither is a
    root binding, and the tracker's must never enter Core.
    """
    models = importlib.import_module(WATCHER_MODELS_LEAF)
    canonical = importlib.import_module(WATCHER_MODELS_CANONICAL)
    tracker = importlib.import_module("omnivia_memory.ingestion.watcher.tracker")
    barrel = importlib.import_module("omnivia_memory.ingestion.watcher")
    canonical_barrel = importlib.import_module("omnivia_core.ingestion.watcher")

    assert models.SourceReference is canonical.SourceReference
    assert barrel.SourceReference is canonical.SourceReference
    assert canonical_barrel.SourceReference is canonical.SourceReference

    assert tracker.SourceReference is not canonical.SourceReference, (
        "the tracker's private SourceReference was taken over by the models one"
    )
    assert tracker.SourceReference.__module__ == (
        "omnivia_memory.ingestion.watcher.tracker"
    )
    assert not hasattr(
        importlib.import_module("omnivia_core.ingestion.watcher.models"),
        "SourceTracker",
    )

    # The tracker's own instances are its own type, not the routed one: a caller
    # holding a reference out of ``SourceTracker`` gets the tracker's class.
    reference = tracker.SourceReference(
        watched_path="/w",
        source_path="/w/a.md",
        source_id="s1",
        workspace_id="ws1",
    )
    store = tracker.SourceTracker()
    store.register("/w", reference)
    fetched = store.get_reference("/w/a.md", "ws1")
    assert fetched is reference
    assert isinstance(fetched, tracker.SourceReference)
    assert not isinstance(fetched, canonical.SourceReference)


def test_ingestion_models_behave_identically_through_both_import_paths() -> None:
    """The routed models are the same objects, so this is not a cross-tree
    comparison: it is proof that those exact objects still construct, round-trip
    and classify correctly when reached through the legacy leaf and the hybrid
    barrel -- the two paths no per-symbol identity check exercises.
    """
    barrel = importlib.import_module("omnivia_memory.ingestion")
    leaf = importlib.import_module(INGESTION_MODELS_LEAF)

    assert [member.value for member in barrel.FileType] == [
        "markdown",
        "text",
        "pdf",
        "docx",
        "unknown",
    ]
    assert [member.value for member in leaf.ParseStatus] == [
        "pending",
        "success",
        "failed",
        "parsed",
    ]

    source = barrel.Source(path="/w/a.md", file_type=barrel.FileType.MARKDOWN)
    assert source.status is leaf.ParseStatus.PENDING
    assert source.workspace_id is None
    assert source.size == 0
    payload = source.to_dict()
    assert payload["file_type"] == "markdown"
    assert payload["status"] == "pending"
    restored = leaf.Source.from_dict(payload)
    assert restored.to_dict() == payload
    assert isinstance(restored, barrel.Source)
    created = restored.updated_at
    restored.touch()
    assert restored.updated_at >= created

    chunk = leaf.Chunk(source_id=source.id, chunk_index=0, content="hello")
    assert barrel.Chunk.from_dict(chunk.to_dict()) == chunk
    # ``Chunk`` compares and hashes by id only, so two different contents under one
    # id are the same chunk -- a behaviour identity alone would not prove survived.
    assert leaf.Chunk(source_id="s", chunk_index=9, content="other", id=chunk.id) == (
        chunk
    )
    assert len({chunk, barrel.Chunk.from_dict(chunk.to_dict())}) == 1

    ok = barrel.ExtractionResult.success("hello")
    assert ok.status is leaf.ParseStatus.SUCCESS
    assert ok.error is None
    assert ok.hash == hashlib.sha256(b"hello").hexdigest()
    failed = leaf.ExtractionResult.failure("boom")
    assert failed.status is barrel.ParseStatus.FAILED
    assert failed.content is None
    assert failed.hash is None
    assert failed.error == "boom"


def test_ingestion_file_inventory_behaves_through_the_leaf_only_route(
    tmp_path: Path,
) -> None:
    """``FileInventory`` is leaf-only -- no barrel publishes it -- so its behaviour
    has to be exercised through the facade directly. ``from_path`` reads the real
    filesystem, detects the type from the extension, and starts out pending.
    """
    leaf = importlib.import_module(INGESTION_MODELS_LEAF)
    canonical = importlib.import_module(INGESTION_MODELS_CANONICAL)
    assert leaf.FileInventory is canonical.FileInventory

    target = tmp_path / "note.md"
    target.write_text("hello", encoding="utf-8")
    inventory = leaf.FileInventory.from_path(target)
    assert isinstance(inventory, canonical.FileInventory)
    assert inventory.extension == ".md"
    assert inventory.size == 5
    assert inventory.file_type is canonical.FileType.MARKDOWN
    assert inventory.parse_status is canonical.ParseStatus.PENDING
    assert inventory.error_message is None
    assert inventory.to_dict()["file_type"] == "markdown"

    inventory.mark_error("boom")
    assert inventory.parse_status is canonical.ParseStatus.FAILED
    assert inventory.error_message == "boom"
    inventory.mark_success()
    assert inventory.parse_status is canonical.ParseStatus.SUCCESS
    assert inventory.error_message is None

    unknown = leaf.FileInventory.from_path(tmp_path)
    assert unknown.file_type is canonical.FileType.UNKNOWN


def test_watcher_models_behave_identically_through_both_import_paths() -> None:
    """The same, for the ten watcher records: constructed and round-tripped
    through the legacy leaf and the hybrid barrel, which are the two paths no
    per-symbol identity check exercises."""
    barrel = importlib.import_module("omnivia_memory.ingestion.watcher")
    leaf = importlib.import_module(WATCHER_MODELS_LEAF)

    assert [member.value for member in barrel.FileChangeType] == [
        "created",
        "modified",
        "deleted",
        "moved",
    ]
    assert [member.value for member in leaf.IndexerState] == [
        "idle",
        "scanning",
        "watching",
        "debouncing",
        "indexing",
        "error",
    ]

    change = barrel.FileChange(path="/w/a.md", event_type=leaf.FileChangeType.MODIFIED)
    assert change.old_path is None
    payload = change.to_dict()
    assert payload["event_type"] == "modified"
    assert leaf.FileChange.from_dict(payload).to_dict() == payload

    batch = leaf.FileChangeBatch(changes=[change, change], debounce_key="ws1")
    assert len(batch) == 2
    assert batch.debounce_key == "ws1"

    config = barrel.DebounceConfig()
    assert config.to_dict() == {
        "initial_delay_ms": 500,
        "max_delay_ms": 2000,
        "min_events": 3,
    }

    watched = leaf.WatchedPath(path="/w", workspace_id="ws1")
    assert watched.recursive is True
    assert watched.ignore_patterns == []
    assert barrel.WatchedPath.from_dict(watched.to_dict()).to_dict() == (
        watched.to_dict()
    )

    reference = barrel.SourceReference(
        watched_path="/w",
        source_path="/w/a.md",
        source_id="s1",
        workspace_id="ws1",
    )
    assert reference.is_stale(None) is True
    assert reference.is_stale("h1") is True
    stamped = leaf.SourceReference.from_dict({**reference.to_dict(), "last_known_hash": "h1"})
    assert stamped.is_stale("h1") is False
    assert stamped.is_stale("h2") is True
    assert stamped.is_stale(None) is True

    status = leaf.IndexerStatus(state=barrel.IndexerState.WATCHING, workspace_id="ws1")
    assert status.to_dict() == {
        "state": "watching",
        "workspace_id": "ws1",
        "active_watched_paths": [],
        "pending_changes": 0,
        "last_index_at": None,
        "last_error": None,
        "indexed_count": 0,
        "deleted_count": 0,
    }

    job = barrel.ScheduledJob.create("reindex", "ws1", delay_seconds=1.5)
    assert isinstance(job, leaf.ScheduledJob)
    assert job.job_type == "reindex"
    assert job.delay_seconds == 1.5
    assert job.job_id

    # ``IndexerScheduler`` is an abstract interface: every method must still raise,
    # so a platform implementation cannot silently inherit a working no-op.
    scheduler = leaf.IndexerScheduler()
    assert isinstance(scheduler, barrel.IndexerScheduler)
    for call in (
        lambda: scheduler.schedule_reindex("ws1"),
        lambda: scheduler.schedule_full_scan("ws1"),
        lambda: scheduler.cancel("job-1"),
        scheduler.list_pending,
    ):
        with pytest.raises(NotImplementedError):
            call()


def test_ingestion_runtime_consumers_hold_the_canonical_objects() -> None:
    """The ingestion and watcher runtime leaves are unconverted modules that
    import their contracts *from the converted facades*, so they now hold
    canonical model objects while staying legacy-owned themselves. Pin that hop --
    including ``memory_graph.ingestion_adapter``, a runtime leaf of another domain
    that consumes this one's records -- and pin that none of their sources reaches
    ``omnivia_core`` directly.
    """
    canonical_models = importlib.import_module(INGESTION_MODELS_CANONICAL)
    canonical_watcher = importlib.import_module(WATCHER_MODELS_CANONICAL)

    for module_name, names in INGESTION_RUNTIME_CONSUMERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(module, name) is getattr(canonical_models, name), (
                f"{module_name}.{name} is not the exact canonical object"
            )
    for module_name, names in WATCHER_RUNTIME_CONSUMERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(module, name) is getattr(canonical_watcher, name), (
                f"{module_name}.{name} is not the exact canonical object"
            )

    for module_name in (
        *INGESTION_RUNTIME_CONSUMERS,
        *WATCHER_RUNTIME_CONSUMERS,
        "omnivia_memory.ingestion.watcher.tracker",
    ):
        source = Path(importlib.import_module(module_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        assert canonical_imports(ast.parse(source)) == [], (
            f"{module_name} now imports omnivia_core directly; the runtime must keep "
            "reaching its contracts through the legacy facades"
        )


#: Fresh-process import orders for the ingestion pair. Each is a full order, not a
#: prefix: whichever module is named first is the one that gets to define the
#: shared objects, so an order that only works because something else was imported
#: earlier fails here.
INGESTION_IMPORT_ORDERS: dict[str, tuple[str, ...]] = {
    "canonical-first": (
        INGESTION_MODELS_CANONICAL,
        WATCHER_MODELS_CANONICAL,
        INGESTION_MODELS_LEAF,
        WATCHER_MODELS_LEAF,
    ),
    "facade-first": (
        INGESTION_MODELS_LEAF,
        WATCHER_MODELS_LEAF,
        INGESTION_MODELS_CANONICAL,
        WATCHER_MODELS_CANONICAL,
    ),
    "canonical-barrel-first": (
        "omnivia_core.ingestion",
        "omnivia_core.ingestion.watcher",
        "omnivia_memory.ingestion",
        "omnivia_memory.ingestion.watcher",
    ),
    "legacy-barrel-first": (
        "omnivia_memory.ingestion",
        "omnivia_memory.ingestion.watcher",
        "omnivia_core.ingestion",
        "omnivia_core.ingestion.watcher",
    ),
    "runtime-first": (
        "omnivia_memory.ingestion.pipeline",
        "omnivia_memory.ingestion.watcher.debouncer",
        "omnivia_memory.ingestion.watcher.tracker",
        "omnivia_core.ingestion",
        "omnivia_core.ingestion.watcher",
    ),
    "reverse": (
        WATCHER_MODELS_LEAF,
        INGESTION_MODELS_LEAF,
        "omnivia_memory.ingestion.watcher",
        "omnivia_memory.ingestion",
        "omnivia_core.ingestion.watcher",
        "omnivia_core.ingestion",
        WATCHER_MODELS_CANONICAL,
        INGESTION_MODELS_CANONICAL,
    ),
    "repeated": (
        INGESTION_MODELS_CANONICAL,
        INGESTION_MODELS_LEAF,
        INGESTION_MODELS_CANONICAL,
        INGESTION_MODELS_LEAF,
        "omnivia_memory.ingestion.watcher",
        "omnivia_core.ingestion.watcher",
        "omnivia_memory.ingestion.watcher",
        "omnivia_core.ingestion.watcher",
    ),
}


def _ingestion_identity_script(import_order: tuple[str, ...]) -> str:
    always = (
        "omnivia_core.ingestion",
        INGESTION_MODELS_CANONICAL,
        "omnivia_core.ingestion.watcher",
        WATCHER_MODELS_CANONICAL,
        "omnivia_memory.ingestion",
        INGESTION_MODELS_LEAF,
        "omnivia_memory.ingestion.watcher",
        WATCHER_MODELS_LEAF,
        "omnivia_memory.ingestion.watcher.tracker",
        "omnivia_core.provenance.models",
        "omnivia_memory.provenance.models",
    )
    lines = [
        "import importlib",
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        f"for module_name in {import_order!r}:",
        "    importlib.import_module(module_name)",
        # Everything asserted below must be reachable regardless of the order under
        # test, so pull in whatever that order did not name.
        f"for module_name in {always!r}:",
        "    importlib.import_module(module_name)",
        *(f"import {module}" for module in always),
    ]
    for legacy_module in (INGESTION_MODELS_LEAF, WATCHER_MODELS_LEAF):
        for symbol, canonical_module in LEAF_SYMBOL_SOURCES[legacy_module].items():
            lines.append(
                f"assert {legacy_module}.{symbol} is {canonical_module}.{symbol}, "
                f"'{legacy_module}.{symbol} is not {canonical_module}.{symbol}'"
            )
    for barrel, canonical_barrel, portable, runtime in (
        (
            "omnivia_memory.ingestion",
            "omnivia_core.ingestion",
            INGESTION_PORTABLE_EXPORTS,
            INGESTION_RUNTIME_EXPORTS,
        ),
        (
            "omnivia_memory.ingestion.watcher",
            "omnivia_core.ingestion.watcher",
            WATCHER_PORTABLE_EXPORTS,
            WATCHER_RUNTIME_EXPORTS,
        ),
    ):
        for name in sorted(portable):
            lines.append(
                f"assert {barrel}.{name} is {canonical_barrel}.{name}, "
                f"'the hybrid barrel stopped publishing the canonical {name}'"
            )
        for name in sorted(runtime):
            lines.append(
                f"assert not hasattr({canonical_barrel}, {name!r}), "
                f"'{name} leaked into the canonical barrel'"
            )
    # The two collisions, in whichever order this process loaded them.
    tracker_reference = "omnivia_memory.ingestion.watcher.tracker.SourceReference"
    provenance_source = "omnivia_core.provenance.models.Source"
    lines.extend(
        [
            (
                f"assert {INGESTION_MODELS_CANONICAL}.Source is not "
                f"{provenance_source}, "
                "'the ingestion and provenance Source records collapsed'"
            ),
            (
                f"assert omnivia_memory.Source is {provenance_source}, "
                "'the legacy root Source was taken over by the ingestion record'"
            ),
            (
                f"assert {tracker_reference} is not "
                f"{WATCHER_MODELS_CANONICAL}.SourceReference, "
                "'the tracker SourceReference collapsed onto the models one'"
            ),
            (
                f"assert {tracker_reference}.__module__ == "
                "'omnivia_memory.ingestion.watcher.tracker', "
                "'the tracker SourceReference is no longer legacy-owned'"
            ),
            (
                f"assert {INGESTION_MODELS_LEAF}.IngestSource is "
                f"{INGESTION_MODELS_CANONICAL}.Source, "
                "'the IngestSource alias no longer points at the canonical Source'"
            ),
        ]
    )
    # No duplicate class objects anywhere in the loaded closure: exactly one object
    # per routed contract name across both trees.
    #
    # ``Source`` and its ``IngestSource`` alias are deliberately held out of this
    # scan: ``Source`` is a live collision, so the provenance domain's own class is
    # legitimately a *second* object of that name in the closure, and the alias
    # names the ingestion one twice. Both are asserted explicitly above instead.
    # ``.tracker`` is skipped for the same reason: its ``SourceReference`` is the
    # other half of the second collision, and is asserted explicitly above too.
    routed = sorted(
        (
            set(FACADE_ROUTES[INGESTION_MODELS_LEAF])
            | set(FACADE_ROUTES[WATCHER_MODELS_LEAF])
        )
        - {"IngestSource", "Source"}
    )
    lines.extend(
        [
            "records = {}",
            "for module_name, module in sorted(sys.modules.items()):",
            "    if not (module_name == 'omnivia_core' or module_name.startswith('omnivia_')):",
            "        continue",
            "    if module_name.endswith('.tracker'):",
            "        continue",
            f"    for name in {routed!r}:",
            "        value = getattr(module, name, None)",
            "        if value is None or getattr(value, '__module__', '').startswith('omnivia_') is False:",
            "            continue",
            "        records.setdefault(name, set()).add(id(value))",
            "duplicated = sorted(name for name, ids in records.items() if len(ids) != 1)",
            "assert not duplicated, f'duplicate objects for {duplicated}'",
            f"assert set(records) == set({routed!r})",
        ]
    )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "order_name", sorted(INGESTION_IMPORT_ORDERS), ids=sorted(INGESTION_IMPORT_ORDERS)
)
def test_ingestion_fresh_process_import_orders_preserve_identity(
    order_name: str,
) -> None:
    """Seven fresh processes, one per order. A shared process would hide an order
    that only works because an earlier test's imports had already settled which
    module defines what -- which is exactly the failure mode two facades under two
    hybrid barrels, with two live name collisions, could introduce.
    """
    _run_isolated(_ingestion_identity_script(INGESTION_IMPORT_ORDERS[order_name]))


def test_canonical_ingestion_closure_loads_neither_the_runtime_nor_omnivia_memory() -> None:
    """A canonical-only ingestion import must reach ``omnivia_memory`` not at all
    -- and in particular not the ingestion/watcher runtime, the Memory Graph, the
    persistence layer, or any private package. The ingestion runtime reaches
    SQLite through ``omnivia_memory.persistence`` and PDF/DOCX extraction through
    third-party readers, so their absence is part of what "the canonical contract
    layer stands alone" means. The exact canonical closure is pinned too, so a
    canonical leaf that started importing a sibling domain fails here rather than
    growing the closure quietly. Only ``src`` goes on the path.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.ingestion",
            "import omnivia_core.ingestion.models",
            "import omnivia_core.ingestion.watcher",
            "import omnivia_core.ingestion.watcher.models",
            "assert 'omnivia_memory' not in sys.modules",
            "loaded = {",
            "    name for name in sys.modules",
            "    if name == 'omnivia_core' or name.startswith('omnivia_core.')",
            "}",
            f"expected = set({sorted(INGESTION_CANONICAL_MODULE_CLOSURE)!r})",
            "assert loaded == expected, sorted(loaded ^ expected)",
            f"forbidden = set({list(INGESTION_FORBIDDEN_MODULE_ROOTS)!r})",
            "leaked = sorted(forbidden & {name.split('.')[0] for name in sys.modules})",
            "assert not leaked, leaked",
            "runtime = sorted(",
            "    name for name in sys.modules",
            "    if name.endswith((",
            "        '.chunker', '.debouncer', '.extractors', '.ingestion_adapter',",
            "        '.pipeline', '.repositories', '.scanner', '.store', '.tracker',",
            "    ))",
            ")",
            "assert not runtime, runtime",
            "private = sorted(",
            "    name for name in sys.modules",
            "    if name.startswith('omnivia_core._')",
            "    or name.startswith('omnivia_core.memory_graph')",
            "    or name.startswith('omnivia_core.persistence')",
            ")",
            "assert not private, private",
        ]
    )
    _run_isolated(script)


def test_neither_package_root_exposes_any_ingestion_symbol() -> None:
    """Both roots are deliberately unedited by this batch. Neither has ever
    re-exported an ingestion or watcher name -- the legacy root's ``Source`` comes
    from the provenance barrel, which is why the ingestion ``Source`` route moves
    no frozen root binding. That is what makes this batch move *no* root binding
    at all, so it is pinned rather than left implicit: a root that started
    re-exporting one of these would need a declared root-binding owner move, and
    there is none.
    """
    ingestion_names = sorted(
        (set(INGESTION_BARREL_ALL) | set(INGESTION_LEAF_ONLY_ROUTES) | set(WATCHER_BARREL_ALL))
        - {"Source"}
    )
    assert len(ingestion_names) == 32
    for root_name in ("omnivia_memory", "omnivia_core"):
        root = importlib.import_module(root_name)
        present = [name for name in ingestion_names if hasattr(root, name)]
        assert present == [], f"{root_name} now re-exports ingestion symbols {present}"
        advertised = [
            name for name in ingestion_names if name in getattr(root, "__all__", ())
        ]
        assert advertised == []

    # ...and neither ingestion barrel is a package either root imports from.
    for root_name in ("omnivia_memory", "omnivia_core"):
        source = Path(importlib.import_module(root_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        reached = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert f"{root_name}.ingestion" not in reached
        assert f"{root_name}.ingestion.watcher" not in reached


def test_ingestion_conversion_declares_no_descriptor_rewrite_or_root_owner_move() -> None:
    """Both ingestion leaves use ``from __future__ import annotations``, so every
    frozen signature they recorded is already a string forward reference that never
    named a package -- there is no descriptor text for the ownership move to
    change. And neither leaf owns a root binding (see the test above), so no
    root-binding owner move follows either. Pin that the two declaration maps carry
    nothing for ingestion, and that the two unrelated entries they do carry are
    untouched.
    """
    assert not any(
        legacy_module.startswith("omnivia_memory.ingestion")
        for legacy_module, _symbol in FACADE_DESCRIPTOR_REWRITES
    )
    assert not any(
        legacy_module.startswith("omnivia_memory.ingestion")
        for _binding, legacy_module in FACADE_ROOT_BINDING_OWNER_MOVES
    )
    assert set(FACADE_ROOT_BINDING_OWNER_MOVES) == {
        ("RUN_LEDGER_CONTRACT_VERSION", "omnivia_memory.run_ledger.models"),
        ("CONTROL_PLANE_CONTRACT_VERSION", "omnivia_memory.control_plane.models"),
    }
    assert set(FACADE_DESCRIPTOR_REWRITES) == {
        ("omnivia_memory.app_manifest.validation", "validate_app_manifest"),
        ("omnivia_memory.module_manifest.validation", "validate_module_manifest"),
    }


# ---------------------------------------------------------------------------
# The ``workspace`` pair: the last direct facade, under the sixth hybrid barrel.
#
# ``workspace.models`` is a plain direct facade -- one import, nothing retained
# -- and it is the last leaf of the whole conversion: no ``source_parity`` route
# remains after it. Its barrel still cannot follow. Two of the barrel's seven
# exports (``WorkspaceRepository`` and ``WorkspaceService``) are owned by the
# runtime-only ``repository``/``service`` leaves, which reach SQLite through
# ``omnivia_memory.persistence`` and files through the ingestion pipeline, so
# ``workspace`` is recorded as a ``hybrid_facade`` in
# ``compatibility/facade-routes.v1.json``, the two trees' ``__all__`` are
# different sizes, and the barrel stays out of ``BARREL_ALL_ORDER`` and every
# gate built on it.
#
# Unlike the ``memory_graph``/``graph``/``ingestion`` hybrids this one brings no
# cross-domain name collision: no other domain owns a distinct contract under any
# of its five routed names, and neither package root re-exports any of the
# barrel's seven exports. The five names *are* rebound -- by the two ``workspace``
# barrels and by the runtime ``repository``/``service`` consumers -- but each of
# those bindings is the routed canonical object itself, not a second owner. The
# legacy root's ``WorkspaceRef`` is the distinct control-plane contract, not one
# of these names. All of that is pinned below rather than assumed.
# ---------------------------------------------------------------------------

WORKSPACE_MODELS_LEAF = "omnivia_memory.workspace.models"
WORKSPACE_MODELS_CANONICAL = "omnivia_core.workspace.models"

#: The exact, ordered *absolute* re-export shape the unchanged legacy
#: ``workspace`` barrel must still have: ``(absolute module, imported names in
#: source order)``. Three blocks, in the barrel's own historical order -- the
#: portable ``models`` block first, then the two runtime ones. Restated here
#: rather than read off the barrel, because this is the file whose edits it
#: exists to reject.
WORKSPACE_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        WORKSPACE_MODELS_LEAF,
        (
            "ImportSummary",
            "Workspace",
            "WorkspaceCreate",
            "WorkspaceIndexStatus",
            "WorkspaceUpdate",
        ),
    ),
    (
        "omnivia_memory.workspace.repository",
        ("WorkspaceRepository",),
    ),
    (
        "omnivia_memory.workspace.service",
        ("WorkspaceService",),
    ),
)

#: The barrel's exact ordered 7-name ``__all__`` literal, restated rather than
#: derived: it is sorted, so the two runtime names interleave with the portable
#: five and it matches none of the blocks above.
WORKSPACE_BARREL_ALL: tuple[str, ...] = (
    "ImportSummary",
    "Workspace",
    "WorkspaceCreate",
    "WorkspaceIndexStatus",
    "WorkspaceRepository",
    "WorkspaceService",
    "WorkspaceUpdate",
)

#: The barrel's two runtime-only children, each declared runtime-only in the
#: frozen route registry and deliberately not a facade.
WORKSPACE_RUNTIME_ONLY_LEAVES: tuple[str, ...] = (
    "omnivia_memory.workspace.repository",
    "omnivia_memory.workspace.service",
)

#: The barrel's exact two runtime-only exports: they must stay legacy-owned and
#: must never appear on the canonical barrel.
WORKSPACE_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {"WorkspaceRepository", "WorkspaceService"}
)

#: The barrel's exact five portable exports: everything else, all of which must
#: hop through the converted models child to a canonical object.
WORKSPACE_PORTABLE_EXPORTS: frozenset[str] = frozenset(WORKSPACE_BARREL_ALL) - (
    WORKSPACE_RUNTIME_EXPORTS
)

#: The workspace runtime modules that stay legacy-owned and unedited by this
#: batch, and the canonical names each must now hold.
WORKSPACE_RUNTIME_CONSUMERS: dict[str, tuple[str, ...]] = {
    "omnivia_memory.workspace.repository": ("Workspace", "WorkspaceIndexStatus"),
    "omnivia_memory.workspace.service": (
        "ImportSummary",
        "Workspace",
        "WorkspaceCreate",
        "WorkspaceUpdate",
    ),
}

#: The exact canonical closure a canonical-only workspace import may produce.
#: Anything else -- a sibling domain, a runtime leaf -- is a leak.
WORKSPACE_CANONICAL_MODULE_CLOSURE: frozenset[str] = frozenset(
    {
        "omnivia_core",
        "omnivia_core.workspace",
        "omnivia_core.workspace.models",
    }
)

#: Module roots a canonical-only workspace import must never load. The workspace
#: runtime reaches SQLite through ``omnivia_memory.persistence`` and file content
#: through the ingestion pipeline's PDF/DOCX readers, so their absence is part of
#: what "the canonical contract layer stands alone" means here.
WORKSPACE_FORBIDDEN_MODULE_ROOTS: tuple[str, ...] = (
    "docx",
    "fitz",
    "omnivia_cloud",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "omnivia_core_runtime",
    "omnivia_dev",
    "omnivia_memory",
    "omnivia_platform",
    "sqlalchemy",
    "sqlite3",
)


def test_workspace_hybrid_barrel_is_held_out_of_the_equal_all_gates() -> None:
    """The barrel's two trees advertise *different* surfaces, so every gate keyed
    on ``BARREL_ALL_ORDER`` (which asserts ``legacy.__all__ == canonical.__all__``)
    would be wrong for it. Pin that it is absent from those gates, and pin the
    inequality that is the reason -- so a future edit that "helpfully" added it to
    ``BARREL_ALL_ORDER`` fails here with the reason rather than as a confusing list
    mismatch.
    """
    assert "workspace" not in BARREL_ALL_ORDER
    assert "workspace" not in ABSOLUTE_IMPORT_BARRELS
    assert "workspace" not in ABSOLUTE_IMPORT_BARREL_IMPORTS
    assert "workspace" not in RELATIVE_IMPORT_BARREL_IMPORTS

    legacy = importlib.import_module("omnivia_memory.workspace")
    canonical = importlib.import_module("omnivia_core.workspace")
    assert tuple(legacy.__all__) == WORKSPACE_BARREL_ALL
    assert len(legacy.__all__) == 7
    assert len(canonical.__all__) == 5
    assert set(canonical.__all__) == set(WORKSPACE_BARREL_ALL) - (
        WORKSPACE_RUNTIME_EXPORTS
    )


def test_workspace_hybrid_barrel_source_is_unchanged_reexport() -> None:
    """The hybrid barrel is *source-unchanged* by this slice: its portable half
    becomes identity-preserving transitively, through its converted ``models``
    child, and its runtime half keeps resolving locally. Pin its exact historical
    shape -- the absolute ``from omnivia_memory.workspace.<leaf> import (...)``
    statements in source order with their exact ordered name lists, then the
    ``__all__`` literal -- so an edit that reroutes it at ``omnivia_core``, drops
    the runtime blocks, adds a ``__getattr__``, or reorders its re-exports fails
    here.
    """
    body = _module_body_after_docstring("omnivia_memory.workspace")
    assert len(body) == len(WORKSPACE_BARREL_ABSOLUTE_IMPORTS) + 1, (
        "omnivia_memory.workspace: expected exactly "
        f"{len(WORKSPACE_BARREL_ABSOLUTE_IMPORTS)} absolute imports plus __all__, "
        f"found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(
        body, WORKSPACE_BARREL_ABSOLUTE_IMPORTS, strict=False
    ):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {node!r}"
        assert node.level == 0, f"the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {all_node!r}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert tuple(
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ) == WORKSPACE_BARREL_ALL

    # Every name the imports bind is exactly what ``__all__`` advertises: the
    # barrel adds nothing of its own and hides nothing it imported.
    imported = sorted(
        name for _, names in WORKSPACE_BARREL_ABSOLUTE_IMPORTS for name in names
    )
    assert imported == sorted(WORKSPACE_BARREL_ALL)
    assert "__getattr__" not in vars(importlib.import_module("omnivia_memory.workspace"))


def test_workspace_hybrid_barrel_portable_exports_hop_through_their_facade() -> None:
    """The barrel's five portable exports must be the exact object bound at the
    *legacy child facade* it re-exports from, and that object must in turn be the
    canonical one. A barrel that started sourcing a name from somewhere else would
    still pass the canonical-identity check alone; requiring the leaf hop too is
    what pins the transitive route through the converted child.
    """
    barrel = importlib.import_module("omnivia_memory.workspace")
    portable = 0
    for legacy_leaf_name, names in WORKSPACE_BARREL_ABSOLUTE_IMPORTS:
        if legacy_leaf_name in WORKSPACE_RUNTIME_ONLY_LEAVES:
            continue
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        owners = LEAF_SYMBOL_SOURCES[legacy_leaf_name]
        for name in names:
            canonical_owner = importlib.import_module(owners[name])
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.workspace.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            assert getattr(barrel, name) is getattr(canonical_owner, name), (
                f"omnivia_memory.workspace.{name} is not the exact object bound at "
                f"{owners[name]}.{name}"
            )
            portable += 1
    assert portable == 5


def test_workspace_hybrid_barrel_runtime_exports_stay_legacy_owned() -> None:
    """The runtime exports are the whole reason this barrel is a hybrid, so their
    *non*-conversion is as much a contract as the portable half's conversion. Each
    must still be the exact object bound at its legacy owner, and each of those
    owners must still be a real legacy module backed by a file in the
    compatibility tree -- not a facade that quietly acquired a canonical
    counterpart.
    """
    barrel = importlib.import_module("omnivia_memory.workspace")
    by_module = dict(WORKSPACE_BARREL_ABSOLUTE_IMPORTS)
    covered: set[str] = set()
    for legacy_leaf_name in WORKSPACE_RUNTIME_ONLY_LEAVES:
        assert legacy_leaf_name not in LEAF_SYMBOL_SOURCES, (
            f"{legacy_leaf_name} is runtime-owned and must not become a facade"
        )
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        leaf_path = Path(legacy_leaf.__file__ or "").resolve()
        assert leaf_path.is_relative_to(MEMORY_SRC), (
            f"{legacy_leaf_name} resolved to {leaf_path}, outside the legacy tree"
        )
        for name in by_module[legacy_leaf_name]:
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.workspace.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            covered.add(name)
    assert covered == set(WORKSPACE_RUNTIME_EXPORTS)


def test_workspace_runtime_exports_are_absent_from_the_canonical_barrel() -> None:
    """Neither runtime-owned name may leak into Core -- not into its ``__all__``
    and not as an attribute. This is what keeps the runtime-owned half out of the
    canonical package rather than merely un-advertised there.
    """
    canonical = importlib.import_module("omnivia_core.workspace")
    for name in sorted(WORKSPACE_RUNTIME_EXPORTS):
        assert name not in canonical.__all__, (
            f"{name} is runtime-owned and must not be in omnivia_core.workspace.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is runtime-owned and must not be an attribute of "
            "omnivia_core.workspace"
        )


def test_workspace_barrel_publishes_exactly_its_models_leaf_routed_surface() -> None:
    """Unlike the ``ingestion`` barrel, this one advertises *every* symbol its
    models child routes -- all five, no leaf-only names. Pin that equality: an
    edit that dropped one from the barrel, or added a routed-looking name the
    barrel never had, would leave every identity check in this module passing.
    """
    leaf = importlib.import_module(WORKSPACE_MODELS_LEAF)
    routed = set(FACADE_ROUTES[WORKSPACE_MODELS_LEAF])
    assert routed == set(WORKSPACE_PORTABLE_EXPORTS)
    for name in sorted(routed):
        assert hasattr(leaf, name)
    for barrel_name in ("omnivia_memory.workspace", "omnivia_core.workspace"):
        barrel = importlib.import_module(barrel_name)
        assert routed <= set(barrel.__all__)
        for name in sorted(routed):
            assert hasattr(barrel, name)

    # And the barrel's surface really is its blocks' bindings, not a different
    # set: every name it advertises is bound at the leaf it re-exports from.
    for legacy_leaf_name, names in WORKSPACE_BARREL_ABSOLUTE_IMPORTS:
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        for name in names:
            assert hasattr(legacy_leaf, name)


def test_workspace_routes_cover_exactly_the_owned_definitions() -> None:
    """The leaf's route set is exactly the five symbols the frozen baseline
    recorded it as *defining*, and nothing else. The incidental bindings its
    historical namespace also keeps resolving (``Any``, ``Enum``, ``Path``,
    ``uuid`` and the rest) are deliberately absent from ``FACADE_ROUTES``: the
    baseline never recorded them as definitions, so there is no route delta to
    normalize. They are covered by ``LEAF_SYMBOL_SOURCES`` instead.
    """
    assert FACADE_ROUTES[WORKSPACE_MODELS_LEAF] == {
        "ImportSummary": WORKSPACE_MODELS_CANONICAL,
        "Workspace": WORKSPACE_MODELS_CANONICAL,
        "WorkspaceCreate": WORKSPACE_MODELS_CANONICAL,
        "WorkspaceIndexStatus": WORKSPACE_MODELS_CANONICAL,
        "WorkspaceUpdate": WORKSPACE_MODELS_CANONICAL,
    }
    routed = set(FACADE_ROUTES[WORKSPACE_MODELS_LEAF])
    namespace = set(LEAF_SYMBOL_SOURCES[WORKSPACE_MODELS_LEAF])
    assert routed < namespace
    assert len(namespace) == 14


def test_workspace_routed_names_collide_with_no_other_domain() -> None:
    """Every previous hybrid batch brought a live name collision with it; this one
    does not, and that is a checked fact rather than an omission.

    None of the five routed names appears in ``COLLIDING_OWNERS``, and none is
    bound by any other converted leaf's namespace -- so ``LEAF_SYMBOL_SOURCES``
    routing them all to the one canonical module cannot be quietly hiding a second
    owner the way ``Source`` or ``SourceRef`` would.
    """
    routed = set(FACADE_ROUTES[WORKSPACE_MODELS_LEAF])
    assert routed.isdisjoint(COLLIDING_OWNERS)
    for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items():
        if legacy_module == WORKSPACE_MODELS_LEAF:
            continue
        assert routed.isdisjoint(symbols), (
            f"{legacy_module} also binds {sorted(routed & set(symbols))}; the "
            "workspace routes are no longer collision-free"
        )
    for legacy_module, symbols in SPLIT_LEAF_SYMBOL_SOURCES.items():
        assert routed.isdisjoint(symbols), (
            f"{legacy_module} also binds {sorted(routed & set(symbols))}"
        )


def test_workspace_models_behave_identically_through_both_import_paths() -> None:
    """The routed models are the same objects, so this is not a cross-tree
    comparison: it is proof that those exact objects still construct, round-trip,
    mutate and apply correctly when reached through the legacy leaf and the hybrid
    barrel -- the two paths no per-symbol identity check exercises.
    """
    barrel = importlib.import_module("omnivia_memory.workspace")
    leaf = importlib.import_module(WORKSPACE_MODELS_LEAF)

    assert [member.value for member in barrel.WorkspaceIndexStatus] == [
        "unindexed",
        "indexing",
        "indexed",
        "error",
        "stale",
    ]

    payload = {
        "id": "ws-1",
        "name": "My Workspace",
        "root_path": "/tmp/root",
        "storage_path": "/tmp/storage",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    workspace = barrel.Workspace.from_dict(payload)
    assert isinstance(workspace, leaf.Workspace)
    assert workspace.index_status is leaf.WorkspaceIndexStatus.UNINDEXED
    assert workspace.description is None
    assert workspace.settings == {}
    assert workspace.last_indexed_at is None
    restored = leaf.Workspace.from_dict(workspace.to_dict())
    assert restored.to_dict() == workspace.to_dict()
    assert restored.to_dict()["index_status"] == "unindexed"

    # ``touch`` and the two marker methods are the mutation surface the runtime
    # calls; ``_now`` stays private to the canonical module, so this also proves
    # the facade did not have to import it.
    stamped = workspace.updated_at
    workspace.touch()
    assert workspace.updated_at >= stamped
    workspace.mark_indexed()
    assert workspace.index_status is barrel.WorkspaceIndexStatus.INDEXED
    assert workspace.last_indexed_at == workspace.updated_at
    workspace.mark_error()
    assert workspace.index_status is leaf.WorkspaceIndexStatus.ERROR
    assert not hasattr(leaf, "_now")

    created = barrel.WorkspaceCreate(
        name="A",
        root_path=leaf.Path("/tmp/root"),
        storage_path=leaf.Path("/tmp/storage"),
    ).to_workspace()
    assert isinstance(created, leaf.Workspace)
    assert created.root_path == str(leaf.Path("/tmp/root").expanduser().resolve())
    assert created.storage_path == str(leaf.Path("/tmp/storage").expanduser().resolve())
    assert created.name == "A"

    # ``WorkspaceCreate`` with no ``storage_path`` derives one under the home
    # directory from the new workspace's own id -- behaviour no identity check
    # reaches, and the only place the leaf's ``Path`` binding is load-bearing.
    assert not hasattr(barrel, "Path"), (
        "the workspace barrel has never re-exported the leaf's incidental Path "
        "binding, so the leaf's own path is the only way to reach it"
    )
    derived = leaf.WorkspaceCreate(name="B", root_path=leaf.Path("/tmp/root"))
    derived_workspace = derived.to_workspace()
    assert derived_workspace.storage_path == str(
        leaf.Path.home() / ".omnivia" / "workspaces" / derived_workspace.id
    )

    assert barrel.WorkspaceUpdate(name="A").apply_to(created) is False
    assert leaf.WorkspaceUpdate(
        name="B",
        description="d",
        index_status=barrel.WorkspaceIndexStatus.STALE,
        settings={"k": 1},
    ).apply_to(created) is True
    assert (created.name, created.description, created.settings) == ("B", "d", {"k": 1})
    assert created.index_status is leaf.WorkspaceIndexStatus.STALE

    summary = barrel.ImportSummary(
        workspace_id="ws-1",
        files_seen=2,
        sources_created=1,
        memories_created=3,
    )
    assert isinstance(summary, leaf.ImportSummary)
    assert summary.errors == []
    assert summary == leaf.ImportSummary(
        workspace_id="ws-1", files_seen=2, sources_created=1, memories_created=3
    )


def test_workspace_default_factories_survive_the_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CANONICAL_TO_LEGACY`` is empty now, so the source-parity gate that used to
    compare ``field(default_factory=...)`` expressions between the two copies no
    longer covers any leaf. Default factories are exactly the kind of detail a
    hand-written facade can drop silently -- a shared mutable default, an id that
    stopped being generated, or a timestamp field that stopped being stamped -- so
    the last leaf pins them here directly, fail-closed, on the canonical objects
    the facade re-exports.

    The assertions are identity/independence ones on purpose, and the two
    generative factories (``Workspace.id`` and ``_now``) are pinned by
    substituting deterministic sentinels for the canonical module's own ``uuid``
    and ``datetime`` bindings: no timestamp comparison and no sleep, so this
    cannot go green -- or flaky -- on clock resolution. Both substitutions go
    through ``monkeypatch`` and are undone (and the undo asserted) before the
    test ends, so nothing leaks into later tests in this module.
    """
    canonical = importlib.import_module(WORKSPACE_MODELS_CANONICAL)
    leaf = importlib.import_module(WORKSPACE_MODELS_LEAF)
    barrel = importlib.import_module("omnivia_memory.workspace")

    # Everything below is exercised through the identities the rest of this
    # module already pinned, so a drifting facade cannot route around it.
    assert barrel.Workspace is leaf.Workspace is canonical.Workspace
    assert barrel.WorkspaceCreate is leaf.WorkspaceCreate is canonical.WorkspaceCreate
    assert barrel.ImportSummary is leaf.ImportSummary is canonical.ImportSummary

    # 1. Both timestamp fields are still stamped by the canonical module's own
    #    ``_now`` -- the same function object, not merely something callable.
    workspace_fields = {
        field.name: field for field in dataclasses.fields(canonical.Workspace)
    }
    for name in ("created_at", "updated_at"):
        assert workspace_fields[name].default_factory is canonical._now, (
            f"Workspace.{name} no longer defaults through the canonical _now"
        )
    assert callable(canonical._now)
    assert canonical._now.__module__ == WORKSPACE_MODELS_CANONICAL
    assert isinstance(canonical._now(), str)

    # 2. ...and ``_now`` stays private to canonical: the facade never had to
    #    import it, and neither legacy path publishes it.
    assert not hasattr(leaf, "_now")
    assert not hasattr(barrel, "_now")

    # 3. The three mutable defaults are still per-instance factories, proven by
    #    mutating one instance and checking the other is untouched.
    mutable_defaults = {
        (canonical.Workspace, "settings"): dict,
        (canonical.WorkspaceCreate, "settings"): dict,
        (canonical.ImportSummary, "errors"): list,
    }
    for (cls, name), factory in mutable_defaults.items():
        field = {f.name: f for f in dataclasses.fields(cls)}[name]
        assert field.default_factory is factory, (
            f"{cls.__name__}.{name} no longer defaults through {factory.__name__}"
        )
        assert field.default is dataclasses.MISSING, (
            f"{cls.__name__}.{name} now carries a shared class-level default"
        )

    first_workspace = barrel.Workspace(
        name="A", root_path="/tmp/root", storage_path="/tmp/storage"
    )
    second_workspace = leaf.Workspace(
        name="B", root_path="/tmp/root", storage_path="/tmp/storage"
    )
    assert first_workspace.settings is not second_workspace.settings
    first_workspace.settings["k"] = 1
    assert second_workspace.settings == {}

    first_create = barrel.WorkspaceCreate(name="A", root_path=leaf.Path("/tmp/root"))
    second_create = leaf.WorkspaceCreate(name="B", root_path=leaf.Path("/tmp/root"))
    assert first_create.settings is not second_create.settings
    first_create.settings["k"] = 1
    assert second_create.settings == {}

    summary_kwargs = {
        "workspace_id": "ws-1",
        "files_seen": 0,
        "sources_created": 0,
        "memories_created": 0,
    }
    first_summary = barrel.ImportSummary(**summary_kwargs)
    second_summary = leaf.ImportSummary(**summary_kwargs)
    assert first_summary.errors is not second_summary.errors
    first_summary.errors.append("boom")
    assert second_summary.errors == []

    # 4. ``Workspace.id`` is a generative factory, and the one whose loss would
    #    be quietest: a facade that dropped it would still hand out ids through
    #    ``from_dict``/``to_workspace``, and "the id is a nonempty string" would
    #    stay true for almost any replacement. So pin the *delegation* instead --
    #    swap the canonical module's own ``uuid.uuid4`` for a two-value sequence
    #    and prove the field stringifies exactly those, in order.
    id_field = workspace_fields["id"]
    assert id_field.default is dataclasses.MISSING, (
        "Workspace.id now carries a shared class-level default"
    )
    assert callable(id_field.default_factory), (
        "Workspace.id no longer defaults through a factory"
    )

    real_uuid4 = canonical.uuid.uuid4
    sentinel_uuids = [
        canonical.uuid.UUID("00000000-0000-4000-8000-000000000001"),
        canonical.uuid.UUID("00000000-0000-4000-8000-000000000002"),
    ]
    assert sentinel_uuids[0] != sentinel_uuids[1]
    issued: list[object] = []

    def _sequenced_uuid4() -> object:
        value = sentinel_uuids[len(issued)]
        issued.append(value)
        return value

    monkeypatch.setattr(canonical.uuid, "uuid4", _sequenced_uuid4)

    # Reached through the identities pinned above, so the facade cannot route
    # around this either.
    first_id_workspace = barrel.Workspace(
        name="A", root_path="/tmp/root", storage_path="/tmp/storage"
    )
    second_id_workspace = leaf.Workspace(
        name="B", root_path="/tmp/root", storage_path="/tmp/storage"
    )
    assert issued == sentinel_uuids, (
        "Workspace.id did not draw exactly one value per instance from the "
        "canonical module's uuid.uuid4"
    )
    assert isinstance(first_id_workspace.id, str)
    assert isinstance(second_id_workspace.id, str)
    assert first_id_workspace.id == str(sentinel_uuids[0])
    assert second_id_workspace.id == str(sentinel_uuids[1])
    assert first_id_workspace.id != second_id_workspace.id
    assert canonical.uuid.UUID(first_id_workspace.id) == sentinel_uuids[0]
    assert canonical.uuid.UUID(second_id_workspace.id) == sentinel_uuids[1]

    # 5. ``_now`` itself, pinned without reading a clock: replace the canonical
    #    module's ``datetime`` binding with a fake that certifies both hops --
    #    that ``now`` is called with the canonical ``timezone.utc`` object, and
    #    that the result is rendered through ``isoformat``.
    real_datetime = canonical.datetime
    stamp_calls: list[str] = []
    sentinel_stamp = "2024-01-01T00:00:00+00:00"

    class _FakeStamp:
        def isoformat(self) -> str:
            stamp_calls.append("isoformat")
            return sentinel_stamp

    class _FakeDatetime:
        @staticmethod
        def now(tz: object) -> _FakeStamp:
            assert tz is canonical.timezone.utc, (
                "_now no longer stamps through the canonical timezone.utc object"
            )
            stamp_calls.append("now")
            return _FakeStamp()

    monkeypatch.setattr(canonical, "datetime", _FakeDatetime)
    assert canonical._now() == sentinel_stamp, (
        "_now no longer returns what its datetime binding's isoformat produced"
    )
    assert stamp_calls == ["now", "isoformat"]

    # 6. Both substitutions come back out, so the rest of this module -- which
    #    constructs plenty of workspaces and compares real timestamps -- runs
    #    against the real bindings again.
    monkeypatch.undo()
    assert canonical.uuid.uuid4 is real_uuid4
    assert canonical.datetime is real_datetime
    restored = leaf.Workspace(name="C", root_path="/tmp/root", storage_path="/tmp/storage")
    assert restored.id not in {first_id_workspace.id, second_id_workspace.id}
    assert restored.created_at != sentinel_stamp


def test_workspace_runtime_consumers_hold_the_canonical_objects() -> None:
    """The workspace repository and service are unconverted modules that import
    their contracts *from the converted facade*, so they now hold canonical model
    objects while staying legacy-owned themselves. Pin that hop, and pin that
    neither source reaches ``omnivia_core`` directly.
    """
    canonical = importlib.import_module(WORKSPACE_MODELS_CANONICAL)
    for module_name, names in WORKSPACE_RUNTIME_CONSUMERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(module, name) is getattr(canonical, name), (
                f"{module_name}.{name} is not the exact canonical object"
            )

    for module_name in WORKSPACE_RUNTIME_CONSUMERS:
        source = Path(importlib.import_module(module_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        assert canonical_imports(ast.parse(source)) == [], (
            f"{module_name} now imports omnivia_core directly; the runtime must keep "
            "reaching its contracts through the legacy facade"
        )


#: Fresh-process import orders for the workspace pair. Each is a full order, not
#: a prefix: whichever module is named first is the one that gets to define the
#: shared objects, so an order that only works because something else was
#: imported earlier fails here.
WORKSPACE_IMPORT_ORDERS: dict[str, tuple[str, ...]] = {
    "canonical-first": (WORKSPACE_MODELS_CANONICAL, WORKSPACE_MODELS_LEAF),
    "facade-first": (WORKSPACE_MODELS_LEAF, WORKSPACE_MODELS_CANONICAL),
    "canonical-barrel-first": ("omnivia_core.workspace", "omnivia_memory.workspace"),
    "legacy-barrel-first": ("omnivia_memory.workspace", "omnivia_core.workspace"),
    "runtime-first": (
        "omnivia_memory.workspace.repository",
        "omnivia_memory.workspace.service",
        "omnivia_core.workspace",
    ),
    "reverse": (
        "omnivia_memory.workspace",
        WORKSPACE_MODELS_LEAF,
        "omnivia_core.workspace",
        WORKSPACE_MODELS_CANONICAL,
    ),
    "repeated": (
        WORKSPACE_MODELS_CANONICAL,
        WORKSPACE_MODELS_LEAF,
        WORKSPACE_MODELS_CANONICAL,
        WORKSPACE_MODELS_LEAF,
        "omnivia_memory.workspace",
        "omnivia_core.workspace",
    ),
}


def _workspace_identity_script(import_order: tuple[str, ...]) -> str:
    always = (
        "omnivia_core.workspace",
        WORKSPACE_MODELS_CANONICAL,
        "omnivia_memory.workspace",
        WORKSPACE_MODELS_LEAF,
        "omnivia_memory.workspace.repository",
        "omnivia_memory.workspace.service",
    )
    lines = [
        "import importlib",
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        f"for module_name in {import_order!r}:",
        "    importlib.import_module(module_name)",
        # Everything asserted below must be reachable regardless of the order under
        # test, so pull in whatever that order did not name.
        f"for module_name in {always!r}:",
        "    importlib.import_module(module_name)",
        *(f"import {module}" for module in always),
    ]
    for symbol, canonical_module in LEAF_SYMBOL_SOURCES[WORKSPACE_MODELS_LEAF].items():
        lines.append(
            f"assert {WORKSPACE_MODELS_LEAF}.{symbol} is {canonical_module}.{symbol}, "
            f"'{WORKSPACE_MODELS_LEAF}.{symbol} is not {canonical_module}.{symbol}'"
        )
    for name in sorted(WORKSPACE_PORTABLE_EXPORTS):
        lines.append(
            f"assert omnivia_memory.workspace.{name} is omnivia_core.workspace.{name}, "
            f"'the hybrid barrel stopped publishing the canonical {name}'"
        )
    for name in sorted(WORKSPACE_RUNTIME_EXPORTS):
        lines.append(
            f"assert not hasattr(omnivia_core.workspace, {name!r}), "
            f"'{name} leaked into the canonical barrel'"
        )
    for module_name, names in WORKSPACE_RUNTIME_CONSUMERS.items():
        for name in names:
            lines.append(
                f"assert {module_name}.{name} is {WORKSPACE_MODELS_CANONICAL}.{name}, "
                f"'{module_name}.{name} is not the canonical object'"
            )
    # No duplicate class objects anywhere in the loaded closure: exactly one object
    # per routed contract name across both trees. Unlike the ingestion pair this
    # scan needs no exclusions -- none of these five names collides with another
    # domain's contract, which is the property
    # ``test_workspace_routed_names_collide_with_no_other_domain`` pins in-process.
    routed = sorted(FACADE_ROUTES[WORKSPACE_MODELS_LEAF])
    lines.extend(
        [
            "records = {}",
            "for module_name, module in sorted(sys.modules.items()):",
            "    if not (module_name == 'omnivia_core' or module_name.startswith('omnivia_')):",
            "        continue",
            f"    for name in {routed!r}:",
            "        value = getattr(module, name, None)",
            "        if value is None or getattr(value, '__module__', '').startswith('omnivia_') is False:",
            "            continue",
            "        records.setdefault(name, set()).add(id(value))",
            "duplicated = sorted(name for name, ids in records.items() if len(ids) != 1)",
            "assert not duplicated, f'duplicate objects for {duplicated}'",
            f"assert set(records) == set({routed!r})",
        ]
    )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "order_name", sorted(WORKSPACE_IMPORT_ORDERS), ids=sorted(WORKSPACE_IMPORT_ORDERS)
)
def test_workspace_fresh_process_import_orders_preserve_identity(
    order_name: str,
) -> None:
    """Seven fresh processes, one per order. A shared process would hide an order
    that only works because an earlier test's imports had already settled which
    module defines what -- and the ``runtime-first`` order is the one that most
    needs it here, because the workspace service pulls in the ingestion and memory
    runtimes before anything canonical is named.
    """
    _run_isolated(_workspace_identity_script(WORKSPACE_IMPORT_ORDERS[order_name]))


def test_canonical_workspace_closure_loads_neither_the_runtime_nor_omnivia_memory() -> None:
    """A canonical-only workspace import must reach ``omnivia_memory`` not at all
    -- and in particular not the workspace repository/service, the ingestion
    pipeline they drive, the persistence layer, or any private package. The
    workspace runtime reaches SQLite through ``omnivia_memory.persistence`` and
    file content through third-party readers, so their absence is part of what
    "the canonical contract layer stands alone" means. The exact canonical closure
    is pinned too, so a canonical leaf that started importing a sibling domain
    fails here rather than growing the closure quietly. Only ``src`` goes on the
    path.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.workspace",
            "import omnivia_core.workspace.models",
            "assert 'omnivia_memory' not in sys.modules",
            "loaded = {",
            "    name for name in sys.modules",
            "    if name == 'omnivia_core' or name.startswith('omnivia_core.')",
            "}",
            f"expected = set({sorted(WORKSPACE_CANONICAL_MODULE_CLOSURE)!r})",
            "assert loaded == expected, sorted(loaded ^ expected)",
            f"forbidden = set({list(WORKSPACE_FORBIDDEN_MODULE_ROOTS)!r})",
            "leaked = sorted(forbidden & {name.split('.')[0] for name in sys.modules})",
            "assert not leaked, leaked",
            "runtime = sorted(",
            "    name for name in sys.modules",
            "    if name.endswith(('.repository', '.service', '.pipeline', '.database'))",
            ")",
            "assert not runtime, runtime",
            "private = sorted(",
            "    name for name in sys.modules",
            "    if name.startswith('omnivia_core._')",
            "    or name.startswith('omnivia_core.ingestion')",
            "    or name.startswith('omnivia_core.persistence')",
            ")",
            "assert not private, private",
        ]
    )
    _run_isolated(script)


def test_neither_root_exposes_any_of_the_workspace_barrel_exports() -> None:
    """Both roots are deliberately unedited by this batch, and neither exposes any
    of the ``workspace`` barrel's seven model/runtime exports. That is what makes
    this batch move *no* root binding at all, so it is pinned rather than left
    implicit: a root that started re-exporting one of these would need a declared
    root-binding owner move, and there is none.

    This is the exact invariant, and deliberately not the broader "no workspace
    name anywhere" claim: the legacy root *does* export ``WorkspaceRef``, the
    separate control-plane contract. That binding is asserted below so the
    narrower claim cannot silently widen back out again.
    """
    workspace_names = sorted(WORKSPACE_BARREL_ALL)
    assert len(workspace_names) == 7
    for root_name in ("omnivia_memory", "omnivia_core"):
        root = importlib.import_module(root_name)
        present = [name for name in workspace_names if hasattr(root, name)]
        assert present == [], f"{root_name} now re-exports workspace symbols {present}"
        advertised = [
            name for name in workspace_names if name in getattr(root, "__all__", ())
        ]
        assert advertised == []

    # ...and the one workspace-*shaped* root binding that does exist is the
    # control-plane contract, owned by a different domain entirely.
    legacy_root = importlib.import_module("omnivia_memory")
    control_plane = importlib.import_module("omnivia_core.control_plane.models")
    assert "WorkspaceRef" not in workspace_names
    assert "WorkspaceRef" in legacy_root.__all__
    assert legacy_root.WorkspaceRef is control_plane.WorkspaceRef

    # ...and the workspace barrel is not a package either root imports from.
    for root_name in ("omnivia_memory", "omnivia_core"):
        source = Path(importlib.import_module(root_name).__file__ or "").read_text(
            encoding="utf-8"
        )
        reached = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert f"{root_name}.workspace" not in reached


def test_workspace_conversion_declares_no_descriptor_rewrite_or_root_owner_move() -> None:
    """The workspace leaf uses ``from __future__ import annotations``, so every
    frozen signature it recorded is already a string forward reference that never
    named a package -- there is no descriptor text for the ownership move to
    change. And it owns no root binding (see the test above), so no root-binding
    owner move follows either. Pin that the two declaration maps carry nothing for
    workspace, and that the entries they do carry are untouched -- which, this
    being the last leaf, is also the final state of both maps.
    """
    assert not any(
        legacy_module.startswith("omnivia_memory.workspace")
        for legacy_module, _symbol in FACADE_DESCRIPTOR_REWRITES
    )
    assert not any(
        legacy_module.startswith("omnivia_memory.workspace")
        for _binding, legacy_module in FACADE_ROOT_BINDING_OWNER_MOVES
    )
    assert set(FACADE_ROOT_BINDING_OWNER_MOVES) == {
        ("RUN_LEDGER_CONTRACT_VERSION", "omnivia_memory.run_ledger.models"),
        ("CONTROL_PLANE_CONTRACT_VERSION", "omnivia_memory.control_plane.models"),
    }
    assert set(FACADE_DESCRIPTOR_REWRITES) == {
        ("omnivia_memory.app_manifest.validation", "validate_app_manifest"),
        ("omnivia_memory.module_manifest.validation", "validate_module_manifest"),
    }


def test_workspace_is_the_last_source_parity_leaf_to_convert() -> None:
    """The workspace batch emptied ``source_parity``: every routed leaf in the
    frozen registry is converted. The six hybrid barrels above them were promoted
    to ``hybrid_facade`` next, and the package root has since become a
    ``root_facade``, so no unconverted route is left at all.

    Pinned here, next to the leaf that did it, so a later batch that reintroduced
    a duplicated leaf -- or that quietly left this one behind -- has to say so.
    """
    manifest = load_manifest()
    assert manifest.by_state(MigrationState.SOURCE_PARITY) == ()
    assert manifest.by_state(MigrationState.CANONICAL_SUBSET) == ()
    workspace = manifest.route_for_legacy(WORKSPACE_MODELS_LEAF)
    assert workspace.migration_state is MigrationState.DIRECT_FACADE
    assert workspace.canonical_module == WORKSPACE_MODELS_CANONICAL

    unconverted = [route for route in manifest.routes if not route.is_converted]
    assert [route.legacy_module for route in unconverted] == []


# ---------------------------------------------------------------------------
# The ``memory`` pair: the first converted leaf set under a hybrid barrel, and
# the one hybrid barrel that never got a section of its own.
#
# ``memory.models`` is a plain direct facade. Its barrel cannot follow, because
# four of its seven exports (``MemoryService`` and its three error types) are
# owned by the runtime-only ``memory.service`` leaf, which drives SQLite through
# ``omnivia_memory.persistence`` and the legacy search service. The barrel is
# therefore a ``hybrid_facade``: portable half canonical, runtime half legacy.
#
# It is the one barrel whose import-block order and ``__all__`` order disagree in
# a way neither sorting nor source order explains: the service block imports its
# four names alphabetically, and ``__all__`` advertises them in the historical
# order ``MemoryService, MemoryServiceError, MemoryNotFoundError,
# InvalidTransitionError``. Both are pinned separately below, on purpose.
# ---------------------------------------------------------------------------

MEMORY_MODELS_LEAF = "omnivia_memory.memory.models"
MEMORY_MODELS_CANONICAL = "omnivia_core.memory.models"

#: The exact, ordered *absolute* re-export shape the unchanged legacy ``memory``
#: barrel must still have: ``(absolute module, imported names in source order)``.
#: Restated here rather than read off the barrel, because this is the file whose
#: edits it exists to reject.
MEMORY_BARREL_ABSOLUTE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MEMORY_MODELS_LEAF, ("Memory", "MemoryCreate", "MemoryUpdate")),
    (
        "omnivia_memory.memory.service",
        (
            "InvalidTransitionError",
            "MemoryNotFoundError",
            "MemoryService",
            "MemoryServiceError",
        ),
    ),
)

#: The barrel's exact ordered seven-name ``__all__`` literal. Not sorted, and not
#: the import order either -- see the section note above.
MEMORY_BARREL_ALL: tuple[str, ...] = (
    "Memory",
    "MemoryCreate",
    "MemoryUpdate",
    "MemoryService",
    "MemoryServiceError",
    "MemoryNotFoundError",
    "InvalidTransitionError",
)

#: The barrel's one runtime-only child, declared runtime-only in the frozen route
#: registry and deliberately not a facade.
MEMORY_RUNTIME_ONLY_LEAVES: tuple[str, ...] = ("omnivia_memory.memory.service",)

#: The barrel's exact four runtime-only exports: they must stay legacy-owned and
#: must never appear on the canonical barrel.
MEMORY_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
        "InvalidTransitionError",
        "MemoryNotFoundError",
        "MemoryService",
        "MemoryServiceError",
    }
)

#: The barrel's exact three portable exports.
MEMORY_PORTABLE_EXPORTS: frozenset[str] = frozenset(MEMORY_BARREL_ALL) - (
    MEMORY_RUNTIME_EXPORTS
)

#: The memory runtime module that stays legacy-owned and unedited, and the
#: canonical names it must now hold. It reaches lifecycle and memory contracts
#: through their converted facades, so all five are canonical objects.
MEMORY_RUNTIME_CONSUMERS: dict[str, tuple[str, ...]] = {
    "omnivia_memory.memory.service": ("Memory", "MemoryCreate", "MemoryUpdate"),
}

#: The exact canonical closure a canonical-only memory import may produce. The
#: memory record composes lifecycle and provenance contracts, so those two
#: canonical domains are part of it -- and nothing else is.
MEMORY_CANONICAL_MODULE_CLOSURE: frozenset[str] = frozenset(
    {
        "omnivia_core",
        "omnivia_core.memory",
        "omnivia_core.memory.models",
        "omnivia_core.lifecycle",
        "omnivia_core.lifecycle.models",
        "omnivia_core.lifecycle.rules",
        "omnivia_core.provenance",
        "omnivia_core.provenance.models",
    }
)

#: Module roots a canonical-only memory import must never load. The memory
#: service reaches SQLite through ``omnivia_memory.persistence`` and the legacy
#: search service, so their absence is part of what "the canonical contract layer
#: stands alone" means here.
MEMORY_FORBIDDEN_MODULE_ROOTS: tuple[str, ...] = (
    "omnivia_cloud",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "omnivia_core_runtime",
    "omnivia_dev",
    "omnivia_memory",
    "omnivia_platform",
    "sqlalchemy",
    "sqlite3",
)


def test_memory_hybrid_barrel_is_held_out_of_the_equal_all_gates() -> None:
    """The barrel's two trees advertise *different* surfaces, so every gate keyed
    on ``BARREL_ALL_ORDER`` (which asserts ``legacy.__all__ == canonical.__all__``)
    would be wrong for it. Pin that it is absent from those gates, and pin the
    inequality that is the reason.
    """
    assert "memory" not in BARREL_ALL_ORDER
    assert "memory" not in ABSOLUTE_IMPORT_BARRELS
    assert "memory" not in ABSOLUTE_IMPORT_BARREL_IMPORTS
    assert "memory" not in RELATIVE_IMPORT_BARREL_IMPORTS

    legacy = importlib.import_module("omnivia_memory.memory")
    canonical = importlib.import_module("omnivia_core.memory")
    assert tuple(legacy.__all__) == MEMORY_BARREL_ALL
    assert len(legacy.__all__) == 7
    assert len(canonical.__all__) == 3
    assert set(canonical.__all__) == set(MEMORY_BARREL_ALL) - MEMORY_RUNTIME_EXPORTS


def test_memory_hybrid_barrel_source_is_unchanged_reexport() -> None:
    """The hybrid barrel is *source-unchanged*: its portable half is identity
    preserving transitively, through its converted ``models`` child, and its
    runtime half keeps resolving locally. Pin its exact historical shape -- the two
    absolute import statements in source order with their exact ordered name lists,
    then the ``__all__`` literal in its own, different order -- so an edit that
    reroutes it at ``omnivia_core``, drops the runtime block, adds a
    ``__getattr__``, or sorts either list fails here.
    """
    body = _module_body_after_docstring("omnivia_memory.memory")
    assert len(body) == len(MEMORY_BARREL_ABSOLUTE_IMPORTS) + 1, (
        "omnivia_memory.memory: expected exactly "
        f"{len(MEMORY_BARREL_ABSOLUTE_IMPORTS)} absolute imports plus __all__, "
        f"found {[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(
        body, MEMORY_BARREL_ABSOLUTE_IMPORTS, strict=False
    ):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {node!r}"
        assert node.level == 0, f"the {module} import must stay absolute"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {all_node!r}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert tuple(
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ) == MEMORY_BARREL_ALL

    imported = sorted(
        name for _, names in MEMORY_BARREL_ABSOLUTE_IMPORTS for name in names
    )
    assert imported == sorted(MEMORY_BARREL_ALL)
    # ...and the two orders really are different, which is the fact the two
    # separate pins above exist to keep.
    assert tuple(
        name for _, names in MEMORY_BARREL_ABSOLUTE_IMPORTS for name in names
    ) != MEMORY_BARREL_ALL
    assert MEMORY_BARREL_ALL != tuple(sorted(MEMORY_BARREL_ALL))
    assert "__getattr__" not in vars(importlib.import_module("omnivia_memory.memory"))


def test_memory_hybrid_barrel_portable_exports_hop_through_their_facade() -> None:
    """The barrel's three portable exports must be the exact object bound at the
    *legacy child facade* it re-exports from, and that object must in turn be the
    canonical one. Requiring the leaf hop as well as the canonical identity is what
    pins the transitive route through the converted child.
    """
    barrel = importlib.import_module("omnivia_memory.memory")
    legacy_leaf = importlib.import_module(MEMORY_MODELS_LEAF)
    owners = LEAF_SYMBOL_SOURCES[MEMORY_MODELS_LEAF]
    portable = 0
    for name in sorted(MEMORY_PORTABLE_EXPORTS):
        canonical_owner = importlib.import_module(owners[name])
        assert getattr(barrel, name) is getattr(legacy_leaf, name), (
            f"omnivia_memory.memory.{name} no longer comes from "
            f"{MEMORY_MODELS_LEAF}.{name}"
        )
        assert getattr(barrel, name) is getattr(canonical_owner, name), (
            f"omnivia_memory.memory.{name} is not the exact object bound at "
            f"{owners[name]}.{name}"
        )
        portable += 1
    assert portable == 3


def test_memory_hybrid_barrel_runtime_exports_stay_legacy_owned() -> None:
    """The four service exports are the whole reason this barrel is a hybrid, so
    their *non*-conversion is as much a contract as the portable half's conversion.
    Each must still be the exact object bound at ``memory.service``, and that owner
    must still be a real legacy module backed by a file in the compatibility tree.
    """
    barrel = importlib.import_module("omnivia_memory.memory")
    by_module = dict(MEMORY_BARREL_ABSOLUTE_IMPORTS)
    covered: set[str] = set()
    for legacy_leaf_name in MEMORY_RUNTIME_ONLY_LEAVES:
        assert legacy_leaf_name not in LEAF_SYMBOL_SOURCES, (
            f"{legacy_leaf_name} is runtime-owned and must not become a facade"
        )
        legacy_leaf = importlib.import_module(legacy_leaf_name)
        leaf_path = Path(legacy_leaf.__file__ or "").resolve()
        assert leaf_path.is_relative_to(MEMORY_SRC), (
            f"{legacy_leaf_name} resolved to {leaf_path}, outside the legacy tree"
        )
        for name in by_module[legacy_leaf_name]:
            assert getattr(barrel, name) is getattr(legacy_leaf, name), (
                f"omnivia_memory.memory.{name} no longer comes from "
                f"{legacy_leaf_name}.{name}"
            )
            covered.add(name)
    assert covered == set(MEMORY_RUNTIME_EXPORTS)


def test_memory_runtime_exports_are_absent_from_the_canonical_barrel() -> None:
    """None of the four runtime-owned names may leak into Core -- not into its
    ``__all__`` and not as an attribute -- and Core must not have grown a
    ``memory.service`` module for them to come from either.
    """
    canonical = importlib.import_module("omnivia_core.memory")
    for name in sorted(MEMORY_RUNTIME_EXPORTS):
        assert name not in canonical.__all__, (
            f"{name} is runtime-owned and must not be in omnivia_core.memory.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is runtime-owned and must not be an attribute of "
            "omnivia_core.memory"
        )
    assert not (CORE_SRC / "omnivia_core" / "memory" / "service.py").exists()


def test_memory_barrel_publishes_exactly_its_models_leaf_routed_surface() -> None:
    """The barrel advertises every symbol its models child routes -- all three, no
    leaf-only names. Pin that equality: an edit that dropped one from the barrel,
    or added a routed-looking name the barrel never had, would leave every identity
    check in this section passing.
    """
    leaf = importlib.import_module(MEMORY_MODELS_LEAF)
    routed = set(FACADE_ROUTES[MEMORY_MODELS_LEAF])
    # The leaf routes six names; the barrel advertises the three the memory
    # domain owns. ``LifecycleState``, ``CreatedBy`` and ``Source`` are the
    # sibling domains' contracts the memory record composes with -- routed at the
    # leaf because the leaf binds them, deliberately not barrel exports.
    assert set(MEMORY_PORTABLE_EXPORTS) < routed
    assert sorted(routed - set(MEMORY_PORTABLE_EXPORTS)) == [
        "CreatedBy",
        "LifecycleState",
        "Source",
    ]
    for name in sorted(routed):
        assert hasattr(leaf, name)
    for barrel_name in ("omnivia_memory.memory", "omnivia_core.memory"):
        barrel = importlib.import_module(barrel_name)
        assert set(MEMORY_PORTABLE_EXPORTS) <= set(barrel.__all__)
        assert set(barrel.__all__).isdisjoint(routed - set(MEMORY_PORTABLE_EXPORTS))
        for name in sorted(MEMORY_PORTABLE_EXPORTS):
            assert hasattr(barrel, name)


def test_memory_runtime_consumers_hold_the_canonical_objects() -> None:
    """``memory.service`` is an unconverted module that imports its contracts *from
    the converted facade*, so it now holds canonical model objects while staying
    legacy-owned itself. Pin that hop, and pin that its source does not reach
    ``omnivia_core`` directly.
    """
    canonical = importlib.import_module(MEMORY_MODELS_CANONICAL)
    for module_name, names in MEMORY_RUNTIME_CONSUMERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(module, name) is getattr(canonical, name), (
                f"{module_name}.{name} is not the exact canonical object"
            )
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert canonical_imports(ast.parse(source)) == [], (
            f"{module_name} now imports omnivia_core directly; the runtime must keep "
            "reaching its contracts through the legacy facade"
        )

    # ...and the lifecycle contracts the service composes with are canonical too,
    # through their own converted facades.
    service = importlib.import_module("omnivia_memory.memory.service")
    assert service.LifecycleState is importlib.import_module(
        "omnivia_core.lifecycle.models"
    ).LifecycleState
    assert service.LifecycleRules is importlib.import_module(
        "omnivia_core.lifecycle.rules"
    ).LifecycleRules


def test_canonical_memory_closure_loads_neither_the_runtime_nor_omnivia_memory() -> None:
    """A canonical-only memory import must reach ``omnivia_memory`` not at all --
    and in particular not the memory service, the persistence layer it drives, or
    the legacy search service. The exact canonical closure is pinned too, so a
    canonical leaf that started importing a sibling domain fails here rather than
    growing the closure quietly. Only ``src`` goes on the path.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.memory",
            "import omnivia_core.memory.models",
            "assert 'omnivia_memory' not in sys.modules",
            "loaded = {",
            "    name for name in sys.modules",
            "    if name == 'omnivia_core' or name.startswith('omnivia_core.')",
            "}",
            f"expected = set({sorted(MEMORY_CANONICAL_MODULE_CLOSURE)!r})",
            "assert loaded == expected, sorted(loaded ^ expected)",
            f"forbidden = set({list(MEMORY_FORBIDDEN_MODULE_ROOTS)!r})",
            "leaked = sorted(forbidden & {name.split('.')[0] for name in sys.modules})",
            "assert not leaked, leaked",
            "runtime = sorted(",
            "    name for name in sys.modules",
            "    if name.endswith(('.repository', '.repositories', '.service',",
            "                      '.pipeline', '.database'))",
            ")",
            "assert not runtime, runtime",
        ]
    )
    _run_isolated(script)


# ---------------------------------------------------------------------------
# The six ``hybrid_facade`` barrels, as one batch.
#
# Each of the six is a *source-unchanged* legacy barrel whose portable bindings
# resolve transitively -- through already-converted routed children -- to exact
# canonical Core objects, while its remaining bindings stay the exact legacy
# objects imported from the descendant modules the frozen registry declares
# runtime-only. The registry now says so: all six moved from ``pending_hybrid``
# to ``hybrid_facade`` together, because they are structurally the same thing and
# promoting some but not others would make the state mean "whichever hybrids were
# looked at".
#
# The per-domain sections above already gate each barrel's own domain in depth.
# This section is the batch-level view: one table, the same gates for all six, so
# a barrel cannot be quietly held to a weaker standard than its peers -- and the
# cross-domain import orders, which no single-domain section can express.
# ---------------------------------------------------------------------------

#: The six barrels: legacy path, canonical path, ordered import blocks, ordered
#: ``__all__``, and the exact portable/runtime partition of that ``__all__``.
#: Every value is one of the independently restated constants above, never read
#: off the barrels themselves.
HYBRID_FACADE_BARRELS: tuple[dict[str, Any], ...] = (
    {
        "suffix": "graph",
        "blocks": GRAPH_BARREL_ABSOLUTE_IMPORTS,
        "all": GRAPH_BARREL_ALL,
        "portable": GRAPH_PORTABLE_EXPORTS,
        "runtime": GRAPH_RUNTIME_EXPORTS,
        "runtime_only_leaves": (GRAPH_RUNTIME_ONLY_LEAF,),
    },
    {
        "suffix": "ingestion",
        "blocks": INGESTION_BARREL_ABSOLUTE_IMPORTS,
        "all": INGESTION_BARREL_ALL,
        "portable": INGESTION_PORTABLE_EXPORTS,
        "runtime": INGESTION_RUNTIME_EXPORTS,
        "runtime_only_leaves": INGESTION_RUNTIME_ONLY_LEAVES,
    },
    {
        "suffix": "ingestion.watcher",
        "blocks": WATCHER_BARREL_ABSOLUTE_IMPORTS,
        "all": WATCHER_BARREL_ALL,
        "portable": WATCHER_PORTABLE_EXPORTS,
        "runtime": WATCHER_RUNTIME_EXPORTS,
        "runtime_only_leaves": WATCHER_RUNTIME_ONLY_LEAVES,
    },
    {
        "suffix": "memory",
        "blocks": MEMORY_BARREL_ABSOLUTE_IMPORTS,
        "all": MEMORY_BARREL_ALL,
        "portable": MEMORY_PORTABLE_EXPORTS,
        "runtime": MEMORY_RUNTIME_EXPORTS,
        "runtime_only_leaves": MEMORY_RUNTIME_ONLY_LEAVES,
    },
    {
        "suffix": "memory_graph",
        "blocks": MEMORY_GRAPH_BARREL_ABSOLUTE_IMPORTS,
        "all": MEMORY_GRAPH_BARREL_ALL,
        "portable": frozenset(MEMORY_GRAPH_BARREL_ALL) - MEMORY_GRAPH_RUNTIME_EXPORTS,
        "runtime": MEMORY_GRAPH_RUNTIME_EXPORTS,
        "runtime_only_leaves": MEMORY_GRAPH_RUNTIME_ONLY_LEAVES,
    },
    {
        "suffix": "workspace",
        "blocks": WORKSPACE_BARREL_ABSOLUTE_IMPORTS,
        "all": WORKSPACE_BARREL_ALL,
        "portable": WORKSPACE_PORTABLE_EXPORTS,
        "runtime": WORKSPACE_RUNTIME_EXPORTS,
        "runtime_only_leaves": WORKSPACE_RUNTIME_ONLY_LEAVES,
    },
)

#: The aggregate exact partition: 93 legacy barrel exports, 62 portable canonical
#: identities and 31 runtime legacy ones.
HYBRID_FACADE_TOTALS: tuple[int, int, int] = (93, 62, 31)

#: The byte-exact identity of each of the six legacy hybrid barrels: legacy
#: module name -> (path components below ``services/omnivia-memory/src``, SHA-256
#: of the file's bytes as accepted when the six were promoted to
#: ``hybrid_facade``).
#:
#: "Source-unchanged" is the load-bearing claim of the whole state: the barrels
#: became converted without a single edit, purely because their children did. The
#: AST gates above prove the *shape* is still right, which is the useful
#: diagnostic; this table proves the *file* is still the exact file that claim was
#: accepted for, which the AST gates cannot -- they are deliberately blind to
#: comments, to module-docstring wording, and to whitespace. Both module names and
#: paths are written out literally rather than resolved from the registry or from
#: ``importlib``: an expectation derived from the tree it constrains would agree
#: with any move of these files, and one derived from the registry would agree
#: with any reroute of them.
#:
#: Updating a hash is a deliberate act. If one of these barrels is genuinely
#: edited, that edit changes what "source-unchanged" means for its
#: ``hybrid_facade`` state, so the new bytes must be reviewed on their own terms
#: and the new digest recorded here in the same commit.
HYBRID_BARREL_SOURCE_SHA256: dict[str, tuple[tuple[str, ...], str]] = {
    "omnivia_memory.graph": (
        ("omnivia_memory", "graph", "__init__.py"),
        "6c707416f1172d3eae44684bf87b66f13effc0210e352ee6fca58e7e51a9cfe4",
    ),
    "omnivia_memory.ingestion": (
        ("omnivia_memory", "ingestion", "__init__.py"),
        "f50de2755fd3edc845cae5b905cde804908c80a72c6610bd0460c7248b6e4fb8",
    ),
    "omnivia_memory.ingestion.watcher": (
        ("omnivia_memory", "ingestion", "watcher", "__init__.py"),
        "81f463a0723cf308d1904f995dcbf6a8bff285bef64ca71da461e021b826d52b",
    ),
    "omnivia_memory.memory": (
        ("omnivia_memory", "memory", "__init__.py"),
        "3e797ccd47b7b8e5cedc82e9a5a2959d4f43b57e719ea6d6b1189c8bc3f15c8c",
    ),
    "omnivia_memory.memory_graph": (
        ("omnivia_memory", "memory_graph", "__init__.py"),
        "34db1978794dd15c13849f5954aa20240849cae1da812cb8f42288671e2c4aff",
    ),
    "omnivia_memory.workspace": (
        ("omnivia_memory", "workspace", "__init__.py"),
        "c4ae8323fc5ff50e05a2d8e71b25c30a7e843f7d40819709ee63c818c2a35512",
    ),
}


def _hybrid_barrel_id(barrel: dict[str, Any]) -> str:
    return str(barrel["suffix"])


def _legacy(barrel: dict[str, Any]) -> str:
    return f"omnivia_memory.{barrel['suffix']}"


def _canonical(barrel: dict[str, Any]) -> str:
    return f"omnivia_core.{barrel['suffix']}"


def test_every_hybrid_barrel_is_a_converted_hybrid_facade() -> None:
    """The registry's own record of that batch, pinned from this side too: the six
    ``hybrid_barrel`` pairs are ``hybrid_facade`` and converted, nothing is left at
    ``pending_hybrid``, and -- now that the package root has become a
    ``root_facade`` -- no unconverted route is left in the whole registry."""
    manifest = load_manifest()
    suffixes = {barrel["suffix"] for barrel in HYBRID_FACADE_BARRELS}
    assert suffixes == {
        route.suffix for route in manifest.by_state(MigrationState.HYBRID_FACADE)
    }
    assert manifest.by_state(MigrationState.PENDING_HYBRID) == ()
    for barrel in HYBRID_FACADE_BARRELS:
        route = manifest.route_for_legacy(_legacy(barrel))
        assert route.migration_state is MigrationState.HYBRID_FACADE
        assert route.is_converted
        assert route.canonical_module == _canonical(barrel)
    assert [
        route.legacy_module for route in manifest.routes if not route.is_converted
    ] == []


def test_hybrid_facade_batch_partition_totals_are_exact() -> None:
    """93 legacy exports across the six barrels: 62 portable canonical identities
    and 31 runtime legacy ones. The per-barrel constants are restated
    independently, so this aggregate is the one place a name that moved from one
    half to the other -- keeping both per-barrel counts intact -- is caught."""
    total = sum(len(barrel["all"]) for barrel in HYBRID_FACADE_BARRELS)
    portable = sum(len(barrel["portable"]) for barrel in HYBRID_FACADE_BARRELS)
    runtime = sum(len(barrel["runtime"]) for barrel in HYBRID_FACADE_BARRELS)
    assert (total, portable, runtime) == HYBRID_FACADE_TOTALS
    for barrel in HYBRID_FACADE_BARRELS:
        assert set(barrel["portable"]).isdisjoint(barrel["runtime"])
        assert set(barrel["portable"]) | set(barrel["runtime"]) == set(barrel["all"])


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_all_and_star_surface_are_exact(barrel: dict[str, Any]) -> None:
    """The legacy barrel's ordered ``__all__`` and its star-import surface are the
    same 93-name contract seen two ways. ``__all__`` order is pinned because it is
    the historical source order; the star namespace is pinned because ``__all__``
    alone would not catch a name that stopped being bound."""
    legacy = importlib.import_module(_legacy(barrel))
    assert isinstance(legacy.__all__, list)
    assert tuple(legacy.__all__) == barrel["all"]
    assert len(legacy.__all__) == len(set(legacy.__all__))

    namespace: dict[str, object] = {}
    exec(f"from {_legacy(barrel)} import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(barrel["all"])
    for name in barrel["all"]:
        assert namespace[name] is getattr(legacy, name)


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_has_no_getattr_or_dir_widening(barrel: dict[str, Any]) -> None:
    """Neither tree may resolve a name lazily. Every export has to be a real,
    statically visible binding decided at import time, or the identity gates below
    would be asserting about whatever the hook chose to return this time."""
    for module_name in (_legacy(barrel), _canonical(barrel)):
        module_vars = vars(importlib.import_module(module_name))
        assert "__getattr__" not in module_vars, module_name
        assert "__dir__" not in module_vars, module_name


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_static_source_matches_its_frozen_block_table(
    barrel: dict[str, Any],
) -> None:
    """Every one of the six is held to the same static shape: a docstring, its
    import blocks in exactly the frozen order with exactly the frozen names in
    order, then the ordered ``__all__`` literal, and nothing else. This is what
    "source-unchanged" means as a checked fact rather than a claim."""
    body = _module_body_after_docstring(_legacy(barrel))
    assert len(body) == len(barrel["blocks"]) + 1, [ast.dump(node) for node in body]
    for node, (module, names) in zip(body, barrel["blocks"], strict=False):
        assert isinstance(node, ast.ImportFrom), ast.dump(node)
        assert node.level == 0
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        assert all(alias.asname is None for alias in node.names)
        assert all(alias.name != "*" for alias in node.names)

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign)
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert (
        tuple(
            element.value
            for element in all_node.value.elts
            if isinstance(element, ast.Constant)
        )
        == barrel["all"]
    )
    assert sorted(name for _module, names in barrel["blocks"] for name in names) == (
        sorted(barrel["all"])
    )


def test_hybrid_barrel_source_table_covers_exactly_the_six_barrels() -> None:
    """The hash table and the batch table are two independently written constants
    naming the same six barrels, so neither can be extended -- or quietly trimmed
    -- without the other. A seventh hybrid barrel that skipped the hash pin, or a
    pinned path that no longer belongs to the batch, fails here."""
    assert set(HYBRID_BARREL_SOURCE_SHA256) == {
        _legacy(barrel) for barrel in HYBRID_FACADE_BARRELS
    }
    assert len(HYBRID_BARREL_SOURCE_SHA256) == 6
    digests = [digest for _parts, digest in HYBRID_BARREL_SOURCE_SHA256.values()]
    assert len(set(digests)) == 6
    for module_name, (parts, digest) in HYBRID_BARREL_SOURCE_SHA256.items():
        assert parts[0] == "omnivia_memory"
        assert parts[-1] == "__init__.py"
        assert ".".join(parts[:-1]) == module_name
        assert len(digest) == 64
        assert digest == digest.lower()


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_source_bytes_match_their_accepted_digest(
    barrel: dict[str, Any],
) -> None:
    """The strictest form of "source-unchanged": the exact bytes of each legacy
    barrel, hashed and compared to the digest accepted when the six were promoted
    to ``hybrid_facade``.

    This is not a duplicate of the AST gates. Those are blind to everything the
    parser discards -- a reworded module docstring, an added comment, reflowed
    whitespace -- and a barrel whose *documentation* now describes a different
    contract than the one its state was accepted for has changed, even though its
    shape has not. Anything at all touching these six files has to come past this
    test and be reviewed as the deliberate change it is.
    """
    module_name = _legacy(barrel)
    parts, expected_digest = HYBRID_BARREL_SOURCE_SHA256[module_name]
    path = MEMORY_SRC.joinpath(*parts)
    assert path.is_file(), path
    source_bytes = path.read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest() == expected_digest, (
        f"{module_name}: {path} no longer has its accepted bytes. If the edit is "
        f"deliberate, review what it does to the barrel's 'hybrid_facade' "
        f"contract and record the new digest in HYBRID_BARREL_SOURCE_SHA256."
    )

    # The pin really is byte-level, and really does cover what the AST gates
    # cannot: a comment-only and a docstring-only edit each change the digest
    # while leaving the parsed body the gates check identical.
    parsed = ast.parse(source_bytes)
    for mutated_bytes in (
        source_bytes + b"\n# a trailing comment the AST never sees\n",
        source_bytes.replace(b'"""', b'"""Reworded. ', 1),
    ):
        assert hashlib.sha256(mutated_bytes).hexdigest() != expected_digest
        mutated = ast.parse(mutated_bytes)
        assert [ast.dump(node) for node in mutated.body[1:]] == [
            ast.dump(node) for node in parsed.body[1:]
        ]


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_source_satisfies_the_frozen_registry_source_policy(
    barrel: dict[str, Any],
) -> None:
    """The same file, judged by the registry's own generic policy rather than by
    this module's table. Zero defects is what earns the ``hybrid_facade`` state, so
    the two independent descriptions of "unchanged and correct" must agree."""
    manifest = load_manifest()
    route = manifest.route_for_legacy(_legacy(barrel))
    children = [
        item
        for item in manifest.routes
        if item.suffix and item.suffix.rpartition(".")[0] == route.suffix
    ]
    path = Path(importlib.import_module(_legacy(barrel)).__file__ or "")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert (
        hybrid_facade_defects(tree, route, children, manifest.runtime_only_modules)
        == []
    )


@pytest.mark.parametrize(
    "barrel", HYBRID_FACADE_BARRELS, ids=_hybrid_barrel_id
)
def test_hybrid_barrel_portable_exports_are_canonical_and_runtime_exports_are_not(
    barrel: dict[str, Any],
) -> None:
    """The partition, asserted as identities rather than as name sets. Every
    portable export is the exact object the canonical barrel binds; every runtime
    export is the exact object its declared legacy owner binds, is absent from the
    canonical barrel entirely, and has no canonical module to have come from."""
    legacy = importlib.import_module(_legacy(barrel))
    canonical = importlib.import_module(_canonical(barrel))
    by_module = dict(barrel["blocks"])

    for name in sorted(barrel["portable"]):
        assert getattr(legacy, name) is getattr(canonical, name), (
            f"{_legacy(barrel)}.{name} is not the canonical object"
        )

    covered: set[str] = set()
    for legacy_leaf_name in barrel["runtime_only_leaves"]:
        owner = importlib.import_module(legacy_leaf_name)
        assert Path(owner.__file__ or "").resolve().is_relative_to(MEMORY_SRC)
        canonical_twin = legacy_leaf_name.replace("omnivia_memory", "omnivia_core", 1)
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(canonical_twin)
        for name in by_module[legacy_leaf_name]:
            assert getattr(legacy, name) is getattr(owner, name)
            assert not hasattr(canonical, name), (
                f"{name} is runtime-owned and leaked into {_canonical(barrel)}"
            )
            assert name not in canonical.__all__
            covered.add(name)
    assert covered == set(barrel["runtime"])


def test_no_hybrid_runtime_export_is_reachable_anywhere_in_core() -> None:
    """The 31 runtime-owned names, checked against the whole canonical package
    rather than one barrel at a time: none of them is bound at any ``omnivia_core``
    module already loaded, and no canonical module file defines one. A name that
    was quietly ported into a *different* canonical module would pass every
    per-barrel gate above and fail here."""
    runtime_names = {
        name for barrel in HYBRID_FACADE_BARRELS for name in barrel["runtime"]
    }
    assert len(runtime_names) == HYBRID_FACADE_TOTALS[2]

    for barrel in HYBRID_FACADE_BARRELS:
        importlib.import_module(_canonical(barrel))
    for module_name, module in sorted(sys.modules.items()):
        if module_name != "omnivia_core" and not module_name.startswith("omnivia_core."):
            continue
        present = sorted(name for name in runtime_names if hasattr(module, name))
        assert present == [], f"{module_name} exposes runtime-owned {present}"

    defined: dict[str, list[str]] = {}
    for path in sorted((CORE_SRC / "omnivia_core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in runtime_names
            ):
                defined.setdefault(node.name, []).append(str(path))
    assert defined == {}, defined


#: Cross-domain fresh-process import orders. The runtime halves of these barrels
#: cross domains -- the workspace service drives the ingestion pipeline and the
#: memory graph adapter, and the memory service drives persistence and search --
#: so an order that settles one domain first can settle objects another domain
#: then re-imports. Each entry is a *full* order: whichever module is named first
#: is the one that gets to define the shared objects.
HYBRID_CROSS_DOMAIN_ORDERS: dict[str, tuple[str, ...]] = {
    "forward": (
        "omnivia_memory.ingestion",
        "omnivia_memory.memory_graph",
        "omnivia_memory.memory",
        "omnivia_memory.workspace",
    ),
    "reverse": (
        "omnivia_memory.workspace",
        "omnivia_memory.memory",
        "omnivia_memory.memory_graph",
        "omnivia_memory.ingestion",
    ),
    "canonical-first": (
        "omnivia_core.ingestion",
        "omnivia_core.memory_graph",
        "omnivia_core.memory",
        "omnivia_core.workspace",
        "omnivia_memory.ingestion",
        "omnivia_memory.memory_graph",
        "omnivia_memory.memory",
        "omnivia_memory.workspace",
    ),
    "runtime-first": (
        "omnivia_memory.workspace.service",
        "omnivia_memory.memory.service",
        "omnivia_memory.memory_graph.store",
        "omnivia_memory.ingestion.pipeline",
    ),
    "watcher-and-parent": (
        "omnivia_memory.ingestion.watcher",
        "omnivia_memory.ingestion",
        "omnivia_memory.graph",
        "omnivia_memory.memory_graph",
    ),
    "repeated": (
        "omnivia_memory.memory",
        "omnivia_memory.memory",
        "omnivia_core.memory",
        "omnivia_memory.memory",
        "omnivia_memory.workspace",
        "omnivia_core.workspace",
        "omnivia_memory.workspace",
    ),
}


#: The modules the cross-domain duplicate-object scan looks at: the six barrels,
#: their canonical counterparts, every module they import from, and the canonical
#: counterpart of each portable one. Scoped deliberately -- a scan over every
#: loaded ``omnivia_*`` module would sweep in sibling domains that legitimately
#: own a same-named contract and turn the check into a list of exemptions.
HYBRID_IDENTITY_SCAN_SCOPE: tuple[str, ...] = tuple(
    sorted(
        {
            *(f"omnivia_memory.{barrel['suffix']}" for barrel in HYBRID_FACADE_BARRELS),
            *(f"omnivia_core.{barrel['suffix']}" for barrel in HYBRID_FACADE_BARRELS),
            *(
                module
                for barrel in HYBRID_FACADE_BARRELS
                for module, _names in barrel["blocks"]
            ),
            *(
                module.replace("omnivia_memory", "omnivia_core", 1)
                for barrel in HYBRID_FACADE_BARRELS
                for module, _names in barrel["blocks"]
                if module not in barrel["runtime_only_leaves"]
            ),
        }
    )
)

#: Portable barrel-export names that resolve to more than one object *within that
#: scope*, with the exact number of distinct objects each must have. Both are
#: long-standing, deliberate separations, not drift:
#:
#: * ``Source`` -- the ingestion domain's ingested-file record and the provenance
#:   domain's source record are independent contracts sharing a name. The
#:   ``ingestion`` barrel publishes the first; ``memory.models``, a routed child in
#:   this scope, binds the second.
#: * ``SourceReference`` -- the watcher models record the ``ingestion.watcher``
#:   barrel publishes, and the distinct dataclass the runtime-only
#:   ``watcher.tracker`` defines for its own use. The tracker's must never replace
#:   the barrel's export, which is what pinning two objects (rather than skipping
#:   the name) keeps true in both directions.
HYBRID_CROSS_DOMAIN_COLLISIONS: dict[str, int] = {
    "Source": 2,
    "SourceReference": 2,
}


def _hybrid_cross_domain_script(import_order: tuple[str, ...]) -> str:
    always = tuple(
        module
        for barrel in HYBRID_FACADE_BARRELS
        for module in (_legacy(barrel), _canonical(barrel))
    )
    portable_pairs = sorted(
        (barrel["suffix"], name)
        for barrel in HYBRID_FACADE_BARRELS
        for name in barrel["portable"]
    )
    runtime_pairs = sorted(
        (barrel["suffix"], name, owner)
        for barrel in HYBRID_FACADE_BARRELS
        for owner in barrel["runtime_only_leaves"]
        for name in dict(barrel["blocks"])[owner]
    )
    lines = [
        "import importlib",
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        f"for module_name in {import_order!r}:",
        "    importlib.import_module(module_name)",
        f"for module_name in {always!r}:",
        "    importlib.import_module(module_name)",
        f"portable = {portable_pairs!r}",
        "for suffix, name in portable:",
        "    legacy = sys.modules['omnivia_memory.' + suffix]",
        "    canonical = sys.modules['omnivia_core.' + suffix]",
        "    assert getattr(legacy, name) is getattr(canonical, name), (suffix, name)",
        f"runtime = {runtime_pairs!r}",
        "for suffix, name, owner_name in runtime:",
        "    legacy = sys.modules['omnivia_memory.' + suffix]",
        "    canonical = sys.modules['omnivia_core.' + suffix]",
        "    owner = importlib.import_module(owner_name)",
        "    assert getattr(legacy, name) is getattr(owner, name), (suffix, name)",
        "    assert not hasattr(canonical, name), (suffix, name)",
        # Exactly one object per portable contract name across the six barrels,
        # their routed children in both trees, and their runtime owners: an order
        # that produced a second class object for any of them would satisfy every
        # pairwise identity above and still be a duplicate. The two documented
        # collisions are pinned at exactly two objects rather than skipped, so a
        # future order that *collapsed* them would fail here too.
        f"scope = {HYBRID_IDENTITY_SCAN_SCOPE!r}",
        "for module_name in scope:",
        "    importlib.import_module(module_name)",
        "records = {}",
        "for module_name in scope:",
        "    module = sys.modules[module_name]",
        "    for _suffix, name in portable:",
        "        value = getattr(module, name, None)",
        "        if value is None:",
        "            continue",
        "        records.setdefault(name, set()).add(id(value))",
        f"expected_objects = {HYBRID_CROSS_DOMAIN_COLLISIONS!r}",
        "wrong = sorted(",
        "    name for name, ids in records.items()",
        "    if len(ids) != expected_objects.get(name, 1)",
        ")",
        "assert not wrong, wrong",
        "assert set(records) == {name for _suffix, name in portable}",
    ]
    return "\n".join(lines)


@pytest.mark.parametrize(
    "order_name",
    sorted(HYBRID_CROSS_DOMAIN_ORDERS),
    ids=sorted(HYBRID_CROSS_DOMAIN_ORDERS),
)
def test_hybrid_barrels_preserve_identity_across_domains_in_fresh_processes(
    order_name: str,
) -> None:
    """One fresh process per order. The per-domain sections already cover each
    barrel's own leaf/barrel/runtime orders; these cross the four domains whose
    runtime services and adapters reach into each other, in both directions, plus
    the runtime-first and canonical-first variants of the same sweep.
    """
    _run_isolated(_hybrid_cross_domain_script(HYBRID_CROSS_DOMAIN_ORDERS[order_name]))


def test_canonical_hybrid_barrels_load_no_legacy_or_runtime_module() -> None:
    """All six canonical barrels together, in one fresh process with only ``src``
    on the path: no ``omnivia_memory``, no persistence or SQLite, no HTTP runtime,
    none of Core's runtime/CLI/MCP siblings, no Platform/Dev/Cloud/Apps/Pro, and
    none of the twenty-one declared legacy runtime-only modules -- which cannot
    even be present, since the legacy tree is not on the path at all.
    """
    canonical_barrels = [_canonical(barrel) for barrel in HYBRID_FACADE_BARRELS]
    forbidden = sorted(
        {
            "docx",
            "fitz",
            "httpx",
            "fastapi",
            "starlette",
            "requests",
            "omnivia_apps",
            "omnivia_cloud",
            "omnivia_core_cli",
            "omnivia_core_mcp",
            "omnivia_core_runtime",
            "omnivia_dev",
            "omnivia_memory",
            "omnivia_platform",
            "omnivia_pro",
            "sqlalchemy",
            "sqlite3",
        }
    )
    runtime_only = sorted(load_manifest().runtime_only_modules)
    script = "\n".join(
        [
            "import importlib",
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {canonical_barrels!r}:",
            "    importlib.import_module(module_name)",
            f"forbidden = set({forbidden!r})",
            "leaked = sorted(forbidden & {name.split('.')[0] for name in sys.modules})",
            "assert not leaked, leaked",
            f"runtime_only = {runtime_only!r}",
            "present = sorted(name for name in runtime_only if name in sys.modules)",
            "assert not present, present",
            "assert not any(name.startswith('omnivia_core.persistence') for name in sys.modules)",
        ]
    )
    _run_isolated(script)


def test_legacy_hybrid_barrels_load_exactly_their_declared_runtime_modules() -> None:
    """The other side of the same fact. A legacy hybrid import *does* load its
    exact historical runtime modules -- that is what makes it a hybrid -- so pin
    that each required one loads, and that importing all six brings in no legacy
    module beyond the declared runtime-only set and the routes themselves. This
    state-only batch must not have widened the runtime surface by a single module.
    """
    manifest = load_manifest()
    declared = set(manifest.runtime_only_modules)
    routed = set(manifest.legacy_modules)
    legacy_barrels = [_legacy(barrel) for barrel in HYBRID_FACADE_BARRELS]
    required = sorted(
        {
            owner
            for barrel in HYBRID_FACADE_BARRELS
            for owner in barrel["runtime_only_leaves"]
        }
    )
    script = "\n".join(
        [
            "import importlib",
            "import sys",
            f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {legacy_barrels!r}:",
            "    importlib.import_module(module_name)",
            f"required = {required!r}",
            "missing = sorted(name for name in required if name not in sys.modules)",
            "assert not missing, missing",
            f"allowed = set({sorted(declared | routed)!r})",
            "loaded = {",
            "    name for name in sys.modules",
            "    if name == 'omnivia_memory' or name.startswith('omnivia_memory.')",
            "}",
            "unexpected = sorted(loaded - allowed)",
            "assert not unexpected, unexpected",
        ]
    )
    _run_isolated(script)
