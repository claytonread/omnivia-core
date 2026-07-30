"""Machine-checkable inventory of Core's public Python exports.

The upcoming package migration renames and moves modules. That is only safe if
we can prove, mechanically, what the public surface looked like beforehand. This
module walks the installed ``omnivia_memory`` package and records:

- the root ``__all__`` and the compatibility symbols that are importable from the
  root but deliberately excluded from ``__all__``;
- for every root export, which module actually defines it and which subpackages
  re-export it, so a move is visible even when the name is unchanged;
- for every module, its ``__all__`` and the public symbols it defines, with
  enough detail (enum members, dataclass fields, callable signatures) to catch a
  silent contract change.

Nothing here imports Core's runtime for effect: the inventory is pure
introspection.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import importlib
import inspect
import pkgutil
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

from baseline import (
    BASELINE_FORMAT_VERSION,
    BASELINE_TASK_ID,
    CORE_PACKAGE,
    CORE_SRC,
    INVENTORY_DIR,
    REPO_ROOT,
)
from baseline.determinism import (
    diff_json,
    format_differences,
    load_json,
    write_artifact,
)

PUBLIC_EXPORTS_PATH = INVENTORY_DIR / "public-exports.json"

_OBJECT_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")

#: The legacy leaves converted into thin compatibility facades over
#: ``omnivia_core`` (see ``tests/canonical_migration/_leaves.py``'s
#: ``FACADE_CANONICAL_TO_LEGACY`` and ``tests/compatibility/test_facade_foundation.py``),
#: and -- for each symbol the frozen inventory once recorded as *defined* by
#: that leaf -- the canonical module that now owns the exact object it routes
#: to. Only these exact, narrow routes are ever normalized away before
#: comparing against the frozen Phase 0 baseline; every other public-export
#: difference still fails ``verify_public_export_inventory``.
#:
#: ``ValidationResult`` appears under several different owners here on purpose:
#: ``omnivia_memory._shared.validation`` routes to the shared primitive, while
#: ``omnivia_memory.app_shell_bridge.models``,
#: ``omnivia_memory.app_manifest.models``, and
#: ``omnivia_memory.component_contract.models``, and
#: ``omnivia_memory.control_plane.models`` each route to their own domain's
#: same-named dataclass. They are distinct objects and each leaf must keep its
#: historical one. ``ProvenanceRequirement`` collides the same way between the
#: App Manifest contract and the Component Contract, and ``LifecycleState``
#: between the lifecycle domain and the control plane's own registry lifecycle
#: enum, and ``SourceRef`` between the knowledge domain and the memory graph. The
#: legacy *root* binds ``ProvenanceRequirement`` from the Component Contract,
#: ``ValidationResult`` from the shared primitive, ``LifecycleState`` from the
#: control plane, and ``SourceRef`` from the knowledge models leaf, so only those
#: four routes ever move a root binding for the colliding names -- the other
#: same-named owners are leaf-local.
#:
#: A route's value is the module that owns the exact *object*, which is not
#: always the module that defines its type. ``RUN_LEDGER_CONTRACT_VERSION`` and
#: ``CONTROL_PLANE_CONTRACT_VERSION`` are ``ContractVersion`` instances their own
#: models leaf builds, so the leaf owns the object while the knowledge leaf owns
#: the class; see ``FACADE_ROOT_BINDING_OWNER_MOVES`` for the separate, exact
#: root-binding deltas that follow from them. The knowledge models leaf is itself
#: routed now, but neither of those two constants is one of *its* routed symbols,
#: so the ordinary exact-route normalization still cannot reach them.
#:
#: Each leaf's entry lists only the symbols that leaf *owns* and publishes. The
#: incidental and cross-leaf-imported bindings a converted leaf's historical
#: namespace also has to keep resolving (``Any``, ``annotations``, a plain
#: ``re``/``unicodedata`` module binding, a sibling leaf's contract class) are
#: deliberately absent: the frozen baseline never recorded them as definitions of
#: that module, so there is no route delta to normalize. They are covered by
#: ``tests/compatibility/test_facade_foundation.py``'s ``LEAF_SYMBOL_SOURCES``
#: instead, which pins their exact owner and identity.
FACADE_ROUTES: dict[str, dict[str, str]] = {
    "omnivia_memory._shared.validation": {
        "SENSITIVE_KEYS": "omnivia_core._shared.validation",
        "ValidationResult": "omnivia_core._shared.validation",
        "scan_sensitive_fields": "omnivia_core._shared.validation",
        "validate_iso_timestamp": "omnivia_core._shared.validation",
        "validate_optional_iso_timestamp": "omnivia_core._shared.validation",
    },
    "omnivia_memory.app_manifest.models": {
        "AppManifest": "omnivia_core.app_manifest.models",
        "AppState": "omnivia_core.app_manifest.models",
        "DataSource": "omnivia_core.app_manifest.models",
        "ProvenanceRequirement": "omnivia_core.app_manifest.models",
        "ValidationResult": "omnivia_core.app_manifest.models",
    },
    "omnivia_memory.app_manifest.validation": {
        "AppManifestValidationError": "omnivia_core.app_manifest.validation",
        "validate_app_manifest": "omnivia_core.app_manifest.validation",
    },
    "omnivia_memory.app_shell_bridge.models": {
        "AppShellBodyDescriptor": "omnivia_core.app_shell_bridge.models",
        "AppShellHostContext": "omnivia_core.app_shell_bridge.models",
        "AppShellRuntimeState": "omnivia_core.app_shell_bridge.models",
        "AppShellSource": "omnivia_core.app_shell_bridge.models",
        "ValidationResult": "omnivia_core.app_shell_bridge.models",
    },
    "omnivia_memory.app_shell_bridge.validation": {
        "AppShellBridgeValidationError": "omnivia_core.app_shell_bridge.validation",
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
        "PermissionPolicy": "omnivia_core.component_contract.models",
        "ProvenanceBehavior": "omnivia_core.component_contract.models",
        "ProvenanceRequirement": "omnivia_core.component_contract.models",
        "ValidationResult": "omnivia_core.component_contract.models",
    },
    "omnivia_memory.component_contract.validation": {
        "ComponentContractValidationError": "omnivia_core.component_contract.validation",
        "validate_agent_run_record": "omnivia_core.component_contract.validation",
        "validate_component_contract": "omnivia_core.component_contract.validation",
    },
    "omnivia_memory.control_plane.imports": {
        "CatalogueArtifactVerification": "omnivia_core.control_plane.imports",
        "ImportSourceChange": "omnivia_core.control_plane.imports",
        "ImportSpecValidation": "omnivia_core.control_plane.imports",
        "ImportedCandidateSet": "omnivia_core.control_plane.imports",
        "detect_import_source_change": "omnivia_core.control_plane.imports",
        "import_asyncapi_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_generated_candidates": "omnivia_core.control_plane.imports",
        "import_mcp_candidates": "omnivia_core.control_plane.imports",
        "import_openapi_candidates": "omnivia_core.control_plane.imports",
        "validate_asyncapi_import_spec": "omnivia_core.control_plane.imports",
        "validate_mcp_import_spec": "omnivia_core.control_plane.imports",
        "validate_openapi_import_spec": "omnivia_core.control_plane.imports",
        "verify_catalogue_artifacts": "omnivia_core.control_plane.imports",
    },
    # ``CONTROL_PLANE_CONTRACT_VERSION`` and ``CONTROL_PLANE_SCHEMA_VERSION`` are
    # the two public constants this leaf owns that ordinary ``__module__``
    # definition detection cannot see (a ``ContractVersion`` instance and a
    # ``str``), so they are routed explicitly -- exactly as ``SENSITIVE_KEYS`` and
    # the run-ledger constants already are.
    "omnivia_memory.control_plane.models": {
        "Agent": "omnivia_core.control_plane.models",
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
        "ValidationResult": "omnivia_core.control_plane.models",
        "WorkspaceRef": "omnivia_core.control_plane.models",
    },
    # ``T`` is a frozen ``TypeVar`` definition this leaf owns even though the
    # barrel above it never re-exported it. The 23 remaining constants are the
    # public bounded-policy constants ``tests/canonical_migration/_strict.py``'s
    # ``EXTRA_MODULE_CONSTANTS`` already tracks for this leaf: ``frozenset``,
    # ``dict``, ``int`` and compiled-pattern values whose runtime ``__module__``
    # does not resolve back to the defining module.
    "omnivia_memory.control_plane.validation": {
        "APPROVAL_ESCALATION_STATES": "omnivia_core.control_plane.validation",
        "ControlPlaneValidationError": "omnivia_core.control_plane.validation",
        "DANGEROUS_SIDE_EFFECTS": "omnivia_core.control_plane.validation",
        "EXTRA_SENSITIVE_KEYS": "omnivia_core.control_plane.validation",
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
        "RRULE_FREQUENCIES": "omnivia_core.control_plane.validation",
        "RRULE_WEEKDAYS": "omnivia_core.control_plane.validation",
        "STABLE_ID_PATTERN": "omnivia_core.control_plane.validation",
        "T": "omnivia_core.control_plane.validation",
        "compile_policy_expression": "omnivia_core.control_plane.validation",
        "manifest_from_dict": "omnivia_core.control_plane.validation",
        "validate_control_plane_manifest": "omnivia_core.control_plane.validation",
    },
    # The three ``BUILTIN_*`` bounded-vocabulary constants are ``frozenset``
    # instances, so ordinary ``__module__`` definition detection cannot see them
    # and they are routed explicitly -- exactly as ``SENSITIVE_KEYS`` and the
    # run-ledger/control-plane constants already are.
    "omnivia_memory.knowledge.models": {
        "AgentGraphContext": "omnivia_core.knowledge.models",
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
    },
    "omnivia_memory.knowledge.normalize": {
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
    },
    # ``MAX_LABEL_LENGTH``, ``MAX_QUOTE_PREVIEW_LENGTH`` and
    # ``SCRIPT_LIKE_MARKERS`` are the three public bounded-policy constants this
    # leaf owns whose ``int``/``tuple`` values have no ``__module__``, so they are
    # routed explicitly alongside its sixteen validators. This leaf's
    # ``ValidationResult`` is deliberately *not* routed: it never owned one -- it
    # imports the shared primitive -- so the frozen baseline recorded no
    # definition to normalize. See ``LEAF_SYMBOL_SOURCES`` for that binding's
    # pinned owner.
    "omnivia_memory.knowledge.validation": {
        "MAX_LABEL_LENGTH": "omnivia_core.knowledge.validation",
        "MAX_QUOTE_PREVIEW_LENGTH": "omnivia_core.knowledge.validation",
        "SCRIPT_LIKE_MARKERS": "omnivia_core.knowledge.validation",
        "check_contract_version_compatibility": "omnivia_core.knowledge.validation",
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
    },
    "omnivia_memory.lifecycle.rules": {
        "CreatedBy": "omnivia_core.lifecycle.rules",
        "LifecycleRules": "omnivia_core.lifecycle.rules",
        "LifecycleState": "omnivia_core.lifecycle.models",
    },
    # ``FIXTURE_TIME`` (a bare ``str``), ``CONFIDENCE_BUCKETS`` (a builtin
    # ``frozenset``) and ``Confidence`` (a ``types.UnionType`` alias) are the
    # three public symbols these leaves own whose runtime ``__module__`` does not
    # resolve back to the defining module, so they are routed explicitly --
    # exactly as ``SENSITIVE_KEYS`` and the run-ledger/control-plane/knowledge
    # constants already are. The ``memory_graph`` barrel above these four leaves
    # stays a hybrid: seven of its exports are owned by the runtime-only
    # ``ingestion_adapter``/``store`` leaves, which are deliberately not routed.
    "omnivia_memory.memory_graph.assembly": {
        "assemble_evidence_graph": "omnivia_core.memory_graph.assembly",
        "assemble_graph_preview": "omnivia_core.memory_graph.assembly",
        "redact_segment_preview": "omnivia_core.memory_graph.assembly",
    },
    "omnivia_memory.memory_graph.fixtures": {
        "FIXTURE_TIME": "omnivia_core.memory_graph.fixtures",
        "MemoryGraphFixture": "omnivia_core.memory_graph.fixtures",
        "build_memory_graph_fixture": "omnivia_core.memory_graph.fixtures",
    },
    # ``SourceRef`` deliberately collides with the knowledge domain's own class
    # of that name. This leaf owns the memory graph's one, and the legacy *root*
    # binds the knowledge one, so this route moves no root binding for it. See
    # ``tests/compatibility/test_facade_foundation.py``'s ``COLLIDING_OWNERS``.
    "omnivia_memory.memory_graph.models": {
        "Confidence": "omnivia_core.memory_graph.models",
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
        "SourceRef": "omnivia_core.memory_graph.models",
    },
    # This leaf's ``ValidationResult`` is deliberately *not* routed: it never
    # owned one -- it imports the shared primitive -- so the frozen baseline
    # recorded no definition to normalize. See ``LEAF_SYMBOL_SOURCES`` for that
    # binding's pinned owner.
    "omnivia_memory.memory_graph.validation": {
        "CONFIDENCE_BUCKETS": "omnivia_core.memory_graph.validation",
        "validate_evidence_graph_response": "omnivia_core.memory_graph.validation",
        "validate_graph_preview_response": "omnivia_core.memory_graph.validation",
        "validate_memory_entity": "omnivia_core.memory_graph.validation",
        "validate_memory_fact": "omnivia_core.memory_graph.validation",
        "validate_memory_segment": "omnivia_core.memory_graph.validation",
        "validate_memory_source": "omnivia_core.memory_graph.validation",
    },
    "omnivia_memory.module_manifest.models": {
        "Entrypoint": "omnivia_core.module_manifest.models",
        "Integrity": "omnivia_core.module_manifest.models",
        "ModuleKind": "omnivia_core.module_manifest.models",
        "ModuleManifest": "omnivia_core.module_manifest.models",
        "Permission": "omnivia_core.module_manifest.models",
        "PublishedTarget": "omnivia_core.module_manifest.models",
    },
    "omnivia_memory.module_manifest.validation": {
        "ModuleManifestValidationError": "omnivia_core.module_manifest.validation",
        "validate_module_manifest": "omnivia_core.module_manifest.validation",
    },
    "omnivia_memory.provenance.models": {
        "Source": "omnivia_core.provenance.models",
        "SourceType": "omnivia_core.provenance.models",
    },
    "omnivia_memory.run_ledger.models": {
        "EvidenceFileRef": "omnivia_core.run_ledger.models",
        "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.run_ledger.models",
        "RUN_LEDGER_PATH_ENV": "omnivia_core.run_ledger.models",
        "RunLedgerEntry": "omnivia_core.run_ledger.models",
        "RunLedgerProvenance": "omnivia_core.run_ledger.models",
        "RunLedgerStatus": "omnivia_core.run_ledger.models",
    },
    "omnivia_memory.run_ledger.validation": {
        "TERMINAL_RUN_STATUSES": "omnivia_core.run_ledger.validation",
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
    },
}


#: Exact descriptor deltas caused solely by a sanctioned ownership move.
#: Keys are ``(legacy module, symbol)`` and values are ``(frozen, normalized)``.
#: This deliberately does not perform a general package-name substitution:
#: package-looking text inside a constant or default argument is contract data,
#: not ownership metadata, and must continue to fail the frozen baseline.
#:
#: Only a descriptor that spells an owning package survives the ownership move
#: with different text, which is why most converted leaves need no entry here.
#: The Component Contract validators, for instance, annotate their return types
#: as string forward references (``-> "ComponentContract"``), so their frozen
#: signatures never named ``omnivia_memory`` and are unchanged by the move. The
#: two entries that do exist are the two validators whose frozen signature
#: annotates a *resolved* class from its own domain's models leaf, so the
#: rendered signature names the owning package and nothing else about the
#: contract changes. The run-ledger and control-plane validators need no entry for
#: the same reason the Component Contract's do not: their modules use
#: ``from __future__ import annotations``, so their frozen signatures spell every
#: annotation as a string forward reference (``'ValidationResult'``,
#: ``'ControlPlaneManifest | Mapping[str, Any]'``) and never named a package at
#: all. The four memory-graph leaves need no entry for that same reason: all four
#: use ``from __future__ import annotations``, so comparing every frozen
#: descriptor they recorded against the live canonical object yields no delta at
#: all.
FACADE_DESCRIPTOR_REWRITES: dict[
    tuple[str, str], tuple[dict[str, Any], dict[str, Any]]
] = {
    (
        "omnivia_memory.app_manifest.validation",
        "validate_app_manifest",
    ): (
        {
            "kind": "function",
            "signature": (
                "(data: Dict[str, Any]) -> "
                "omnivia_memory.app_manifest.models.AppManifest"
            ),
        },
        {
            "kind": "function",
            "signature": (
                "(data: Dict[str, Any]) -> "
                "omnivia_core.app_manifest.models.AppManifest"
            ),
        },
    ),
    (
        "omnivia_memory.module_manifest.validation",
        "validate_module_manifest",
    ): (
        {
            "kind": "function",
            "signature": (
                "(data: Dict[str, Any]) -> "
                "omnivia_memory.module_manifest.models.ModuleManifest"
            ),
        },
        {
            "kind": "function",
            "signature": (
                "(data: Dict[str, Any]) -> "
                "omnivia_core.module_manifest.models.ModuleManifest"
            ),
        },
    ),
}


#: Exact root-binding owner moves caused solely by a sanctioned ownership move
#: whose new owner is *not* the routed leaf. Keys are ``(root binding name,
#: routed legacy module)`` and values are ``(frozen owner, normalized owner)``.
#:
#: ``_normalize_expected_for_facade_routes`` moves a root binding from the routed
#: leaf to that route's canonical module, which covers every routed symbol the
#: leaf itself defines. A routed *instance* is the exception: the leaf owns the
#: object, but ``defined_in`` reports the module that owns its ``type``.
#: ``RUN_LEDGER_CONTRACT_VERSION`` and ``CONTROL_PLANE_CONTRACT_VERSION`` are the
#: two such root bindings -- each a ``ContractVersion`` its own models leaf
#: builds -- so converting either leaf moves that binding's recorded owner from
#: the legacy knowledge leaf to the canonical one. Each delta is named and
#: declared here rather than derived, and ``_facade_root_binding_problems`` proves
#: every entry before any of them may be normalized away.
#:
#: ``omnivia_memory.knowledge.models`` is now a converted facade with routes of
#: its own, but that does not make either entry redundant: neither constant is one
#: of *that* leaf's routed symbols (each belongs to the run-ledger / control-plane
#: leaf that builds it), so the route loop never sees the binding and these two
#: declarations remain the only thing that moves it. Their frozen and new owners
#: are unchanged by the knowledge conversion -- the type's canonical owner is
#: still ``omnivia_core.knowledge.models``.
#:
#: The memory graph adds no entry, and deliberately so. Its five frozen root
#: bindings (``EvidenceGraphResponse``, ``GraphPreviewResponse``,
#: ``RetrievalTrace``, ``MemoryGraphFixture`` and ``build_memory_graph_fixture``)
#: are each a class or a function the routed leaf itself owns, so ``defined_in``
#: already names that leaf and the ordinary exact-route loop above moves all five.
#: Its two constants with no usable ``__module__`` -- ``FIXTURE_TIME`` and
#: ``CONFIDENCE_BUCKETS`` -- and its ``Confidence`` alias are not root bindings at
#: all, so nothing about them needs declaring here either.
#:
#: The two entries are independent: each is keyed by its own ``(binding, routed
#: leaf)`` pair and applied by whole-string equality on that binding alone, so
#: they neither depend on declaration order nor on each other.
FACADE_ROOT_BINDING_OWNER_MOVES: dict[tuple[str, str], tuple[str, str]] = {
    ("RUN_LEDGER_CONTRACT_VERSION", "omnivia_memory.run_ledger.models"): (
        "omnivia_memory.knowledge.models",
        "omnivia_core.knowledge.models",
    ),
    ("CONTROL_PLANE_CONTRACT_VERSION", "omnivia_memory.control_plane.models"): (
        "omnivia_memory.knowledge.models",
        "omnivia_core.knowledge.models",
    ),
}


def _expected_facade_descriptor(
    legacy_module: str,
    symbol: str,
    frozen_descriptor: dict[str, Any],
) -> dict[str, Any]:
    """Return the one exact sanctioned descriptor rewrite, if applicable."""
    rewrite = FACADE_DESCRIPTOR_REWRITES.get((legacy_module, symbol))
    if rewrite is None:
        return frozen_descriptor
    old, new = rewrite
    return new if frozen_descriptor == old else frozen_descriptor


class InventoryError(RuntimeError):
    """Raised when the public export inventory cannot be built or verified."""


def ensure_core_importable() -> ModuleType:
    """Import Core, preferring this checkout over any stale editable install.

    The benchmark runner already guards against importing ``omnivia_memory``
    from a shadow install; the baseline needs the same guarantee, because a
    baseline captured from the wrong tree is worse than no baseline.
    """
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))
    module = __import__(CORE_PACKAGE)
    resolved = Path(module.__file__ or "").resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:  # pragma: no cover - environment guard
        raise InventoryError(
            f"{CORE_PACKAGE} resolved to {resolved}, which is outside this repository "
            f"({REPO_ROOT}). Baseline artifacts must describe this checkout. "
            "Uninstall the shadowing package or run with "
            "PYTHONPATH=services/omnivia-memory/src."
        ) from exc
    return module


def iter_core_modules(root: ModuleType) -> Iterator[ModuleType]:
    """Yield ``omnivia_memory`` and every importable submodule, sorted by name."""
    names = [root.__name__]
    for info in pkgutil.walk_packages(root.__path__, prefix=f"{root.__name__}."):
        names.append(info.name)
    for name in sorted(set(names)):
        yield __import__(name, fromlist=["__name__"])


def build_public_export_inventory() -> dict[str, Any]:
    """Return the full public export inventory for the current checkout."""
    root = ensure_core_importable()
    modules = list(iter_core_modules(root))
    module_by_name = {module.__name__: module for module in modules}

    root_all = sorted(getattr(root, "__all__", []))
    compatibility = _compatibility_exports(root, frozenset(root_all))

    bindings: dict[str, Any] = {}
    for name in [*root_all, *compatibility]:
        if name.startswith("__"):
            continue
        value = getattr(root, name)
        bindings[name] = {
            "defined_in": _defining_module(value),
            "exported_by": _exporting_modules(value, name, module_by_name),
        }

    module_entries: dict[str, Any] = {}
    for module in modules:
        module_entries[module.__name__] = {
            "all": sorted(getattr(module, "__all__", [])) if hasattr(module, "__all__") else None,
            "defines": _module_definitions(module),
        }

    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "package": CORE_PACKAGE,
        "package_version": getattr(root, "__version__", None),
        "package_source": _repo_relative(root.__file__),
        "root": {
            "all": root_all,
            "compatibility_exports": compatibility,
            "bindings": bindings,
        },
        "modules": module_entries,
    }


def write_public_export_inventory() -> Path:
    """Regenerate the tracked public export inventory."""
    write_artifact(PUBLIC_EXPORTS_PATH, build_public_export_inventory())
    return PUBLIC_EXPORTS_PATH


def _facade_route_problems(
    actual: dict[str, Any],
    *,
    routes: dict[str, dict[str, str]] = FACADE_ROUTES,
    frozen: dict[str, Any] | None = None,
) -> list[str]:
    """Verify every facade route in ``routes`` before it may be normalized away.

    Each routed symbol must (a) still exist on the legacy leaf, (b) be the
    *exact* object bound at its declared canonical module (not a lookalike),
    and (c) -- when the frozen baseline once recorded a historical
    ``defines`` descriptor for it -- describe identically to that frozen
    descriptor after applying only an exact route-and-symbol-specific sanctioned
    ownership rewrite (see ``FACADE_DESCRIPTOR_REWRITES``), so a structural
    contract change cannot hide behind the ownership move. Symbols the frozen
    inventory never tracked as a definition (for example a bare constant with
    no ``__module__``) skip only that third check; identity is still required.
    """
    if frozen is None:
        frozen = load_json(PUBLIC_EXPORTS_PATH)
    problems: list[str] = []
    for legacy_module, symbol_routes in routes.items():
        try:
            legacy = importlib.import_module(legacy_module)
        except ImportError as exc:
            problems.append(f"{legacy_module}: cannot import ({exc})")
            continue
        frozen_defines = frozen.get("modules", {}).get(legacy_module, {}).get("defines") or {}
        actual_defines = actual.get("modules", {}).get(legacy_module, {}).get("defines") or {}
        for symbol, canonical_module in symbol_routes.items():
            if not hasattr(legacy, symbol):
                problems.append(f"{legacy_module}.{symbol}: not found on the legacy module")
                continue
            if symbol in actual_defines:
                problems.append(
                    f"{legacy_module}.{symbol}: still locally defined in the legacy module; "
                    "expected a pure import from the canonical owner"
                )
                continue
            live_legacy = getattr(legacy, symbol)
            try:
                canonical = importlib.import_module(canonical_module)
            except ImportError as exc:
                problems.append(f"{canonical_module}: cannot import ({exc})")
                continue
            if not hasattr(canonical, symbol):
                problems.append(
                    f"{legacy_module}.{symbol}: routed canonical module {canonical_module!r} "
                    "has no such symbol"
                )
                continue
            live_canonical = getattr(canonical, symbol)
            if live_legacy is not live_canonical:
                problems.append(
                    f"{legacy_module}.{symbol} is not the exact object bound at "
                    f"{canonical_module}.{symbol}"
                )
                continue
            if symbol in frozen_defines and describe_symbol(
                live_legacy
            ) != _expected_facade_descriptor(
                legacy_module, symbol, frozen_defines[symbol]
            ):
                problems.append(
                    f"{legacy_module}.{symbol}: contract drifted from the frozen historical "
                    "definition"
                )
    return problems


def _facade_root_binding_problems(
    *,
    routes: dict[str, dict[str, str]] = FACADE_ROUTES,
    moves: dict[tuple[str, str], tuple[str, str]] = FACADE_ROOT_BINDING_OWNER_MOVES,
    frozen: dict[str, Any] | None = None,
) -> list[str]:
    """Verify every declared root-binding owner move before normalizing it.

    Each entry must (a) name a symbol that really is routed by that legacy
    module, (b) match the frozen baseline's recorded owner for that root binding
    exactly, (c) still be the exact object the route points at, and (d) actually
    report the declared new owner. A stale entry, a mis-declared owner, or an
    owner that moved somewhere other than where the entry says fails here rather
    than being normalized away.
    """
    if frozen is None:
        frozen = load_json(PUBLIC_EXPORTS_PATH)
    problems: list[str] = []
    try:
        root = importlib.import_module(CORE_PACKAGE)
    except ImportError as exc:  # pragma: no cover - the root import fails earlier
        return [f"{CORE_PACKAGE}: cannot import ({exc})"]
    for (name, legacy_module), (frozen_owner, canonical_owner) in sorted(moves.items()):
        where = f"root binding {name!r} via {legacy_module}"
        canonical_module = routes.get(legacy_module, {}).get(name)
        if canonical_module is None:
            problems.append(f"{where}: {name} is not a declared route of that module")
            continue
        binding = frozen.get("root", {}).get("bindings", {}).get(name)
        if binding is None:
            problems.append(f"{where}: the frozen baseline has no such root binding")
            continue
        if binding.get("defined_in") != frozen_owner:
            problems.append(
                f"{where}: the frozen baseline records owner "
                f"{binding.get('defined_in')!r}, not the declared {frozen_owner!r}"
            )
            continue
        if not hasattr(root, name):
            problems.append(f"{where}: not found on {CORE_PACKAGE}")
            continue
        live = getattr(root, name)
        try:
            canonical = importlib.import_module(canonical_module)
        except ImportError as exc:
            problems.append(f"{canonical_module}: cannot import ({exc})")
            continue
        if getattr(canonical, name, None) is not live:
            problems.append(
                f"{where} is not the exact object bound at {canonical_module}.{name}"
            )
            continue
        if _defining_module(live) != canonical_owner:
            problems.append(
                f"{where}: owner moved to {_defining_module(live)!r}, not the "
                f"declared {canonical_owner!r}"
            )
    return problems


def _normalize_expected_for_facade_routes(
    expected: dict[str, Any],
    *,
    routes: dict[str, dict[str, str]] = FACADE_ROUTES,
    moves: dict[tuple[str, str], tuple[str, str]] = FACADE_ROOT_BINDING_OWNER_MOVES,
) -> dict[str, Any]:
    """Return a copy of ``expected`` with only the sanctioned facade deltas applied.

    Three, and only three, kinds of change are made, all caused directly by the
    routes in ``routes``:

    - a routed leaf's ``defines`` becomes empty once every symbol the frozen
      baseline recorded there is confirmed routed (the symbol is no longer
      *defined* by the legacy module -- it is imported);
    - a root binding's ``defined_in`` moves from the legacy leaf to the
      routed canonical module, for exactly the routed names;
    - each root binding named by ``moves`` has its ``defined_in`` replaced, whole
      and exactly, when it still holds that entry's declared frozen owner. This
      covers the routed *instance* case, where the new owner is the module that
      owns the object's ``type`` rather than the routed leaf itself. The
      replacement is a whole-string equality test and a whole-string
      substitution: no package-prefix rewriting, and no other binding is
      consulted or touched. Entries are therefore independent of each other and
      of declaration order.

    The frozen artifact on disk is never touched; this operates on an
    in-memory copy so every other difference still fails the comparison in
    ``verify_public_export_inventory``.
    """
    normalized = copy.deepcopy(expected)
    for legacy_module, symbol_routes in routes.items():
        module_entry = normalized.get("modules", {}).get(legacy_module)
        if module_entry is None:
            continue
        frozen_defines = module_entry.get("defines") or {}
        if frozen_defines and set(frozen_defines) <= set(symbol_routes):
            module_entry["defines"] = {}
        for name, canonical_module in symbol_routes.items():
            binding = normalized.get("root", {}).get("bindings", {}).get(name)
            if binding is not None and binding.get("defined_in") == legacy_module:
                binding["defined_in"] = canonical_module
    for (name, _legacy_module), (frozen_owner, canonical_owner) in moves.items():
        binding = normalized.get("root", {}).get("bindings", {}).get(name)
        if binding is not None and binding.get("defined_in") == frozen_owner:
            binding["defined_in"] = canonical_owner
    return normalized


def verify_public_export_inventory() -> list[str]:
    """Return precise drift messages, or an empty list when the surface matches."""
    expected = load_json(PUBLIC_EXPORTS_PATH)
    actual = build_public_export_inventory()

    route_problems = _facade_route_problems(actual, frozen=expected)
    if route_problems:
        return [
            (
                "Compatibility-facade route verification failed for one or more of the "
                "leaves converted in tests/canonical_migration/_leaves.py's "
                "FACADE_CANONICAL_TO_LEGACY; refusing to normalize the frozen Phase 0 "
                "baseline for an unverified delta."
            ),
            format_differences(route_problems),
        ]

    root_binding_problems = _facade_root_binding_problems(frozen=expected)
    if root_binding_problems:
        return [
            (
                "Compatibility-facade root-binding owner-move verification failed for "
                "one or more entries in baseline.inventory's "
                "FACADE_ROOT_BINDING_OWNER_MOVES; refusing to normalize the frozen "
                "Phase 0 baseline for an unverified owner move."
            ),
            format_differences(root_binding_problems),
        ]

    normalized_expected = _normalize_expected_for_facade_routes(expected)
    differences = diff_json(normalized_expected, actual)
    if not differences:
        return []
    return [
        "Public Python export inventory drifted from the frozen Phase 0 baseline.",
        f"Artifact: {PUBLIC_EXPORTS_PATH.relative_to(REPO_ROOT)}",
        format_differences(differences),
        (
            "If the change is intended, regenerate with `python -m baseline capture` "
            "and record the reason in the review note."
        ),
    ]


def _compatibility_exports(root: ModuleType, root_all: frozenset[str]) -> list[str]:
    """Public root attributes that are importable but excluded from ``__all__``.

    Core's root docstring calls these out explicitly: runtime-heavy symbols stay
    importable for downstream callers while the advertised API stays
    contract-only. They are part of the baseline because the migration must not
    break them by accident.
    """
    names = []
    for name, value in vars(root).items():
        if name.startswith("_") or name in root_all or isinstance(value, ModuleType):
            continue
        names.append(name)
    return sorted(names)


def _defining_module(value: Any) -> str | None:
    module = getattr(value, "__module__", None)
    return module if isinstance(module, str) else None


def _exporting_modules(
    value: Any,
    name: str,
    module_by_name: dict[str, ModuleType],
) -> list[str]:
    """Modules whose ``__all__`` advertises this exact object under this name."""
    exporters = []
    for module_name, module in module_by_name.items():
        if module_name == CORE_PACKAGE:
            continue
        if name not in getattr(module, "__all__", []):
            continue
        if getattr(module, name, None) is value:
            exporters.append(module_name)
    return sorted(exporters)


def _module_definitions(module: ModuleType) -> dict[str, Any]:
    """Detail for every public symbol that is *defined* in this module."""
    definitions: dict[str, Any] = {}
    for name, value in vars(module).items():
        if name.startswith("_") or isinstance(value, ModuleType):
            continue
        if _defining_module(value) != module.__name__:
            # Imported into the module rather than defined by it. Recording it
            # here would make every re-export look like a definition and hide
            # real moves during the migration.
            continue
        definitions[name] = describe_symbol(value)
    return dict(sorted(definitions.items()))


def describe_symbol(value: Any) -> dict[str, Any]:
    """Return a stable, contract-level description of a public symbol."""
    if inspect.isclass(value):
        return _describe_class(value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return {"kind": "function", "signature": _signature(value)}
    return _describe_constant(value)


def _describe_class(value: type) -> dict[str, Any]:
    if issubclass(value, enum.Enum):
        return {
            "kind": "enum",
            "base": _enum_base(value),
            "members": [f"{member.name}={member.value!r}" for member in value],
        }
    if issubclass(value, BaseException):
        return {"kind": "exception", "bases": _base_names(value)}
    if dataclasses.is_dataclass(value):
        params = getattr(value, "__dataclass_params__", None)
        return {
            "kind": "dataclass",
            "frozen": bool(getattr(params, "frozen", False)),
            "fields": [field.name for field in dataclasses.fields(value)],
            "methods": _public_methods(value),
        }
    if _is_typed_dict(value):
        return {"kind": "typed_dict", "fields": sorted(getattr(value, "__annotations__", {}))}
    return {"kind": "class", "bases": _base_names(value), "methods": _public_methods(value)}


def _describe_constant(value: Any) -> dict[str, Any]:
    return {"kind": "constant", "type": type(value).__name__, "value": _render_value(value)}


def _render_value(value: Any) -> Any:
    """Render a constant's value in a form that is stable across runs."""
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (set, frozenset)):
        return sorted(str(_render_value(item)) for item in value)
    if isinstance(value, (list, tuple)):
        return [_render_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(value[key]) for key in sorted(value, key=str)}
    return f"<{type(value).__name__}>"


def _enum_base(value: type) -> str:
    for base in value.__mro__[1:]:
        if base is enum.Enum:
            continue
        if base in (str, int):
            return base.__name__
    return "Enum"


def _base_names(value: type) -> list[str]:
    return [f"{base.__module__}.{base.__qualname__}" for base in value.__bases__]


def _public_methods(value: type) -> list[str]:
    methods = []
    for name, member in vars(value).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or isinstance(member, (classmethod, staticmethod, property)):
            methods.append(f"{name}{_signature(member)}")
    return sorted(methods)


def _signature(value: Any) -> str:
    target = value
    if isinstance(value, (classmethod, staticmethod)):
        target = value.__func__
    elif isinstance(value, property):
        target = value.fget
    if target is None:
        return "(...)"
    try:
        rendered = str(inspect.signature(target))
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return "(...)"
    # Default values that repr as ``<object at 0x...>`` would change every run.
    return _OBJECT_ADDRESS.sub("0xXXXX", rendered)


def _is_typed_dict(value: type) -> bool:
    return hasattr(value, "__required_keys__") and hasattr(value, "__annotations__")


def _repo_relative(path: str | None) -> str | None:
    if path is None:
        return None
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
