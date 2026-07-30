"""Frozen module-level route registry for the ``omnivia_memory`` facade.

``services/omnivia-memory`` is being turned into a compatibility facade over
``src/omnivia_core``: every legacy import path must keep working while the
implementation moves to the canonical package. Doing that leaf by leaf only
stays reviewable if the *set* of paths involved is frozen first, so this module
loads and validates ``compatibility/facade-routes.v1.json`` -- one route per
module suffix shared by both package trees, plus the package root -- and refuses
anything that has drifted.

Three independent things are checked:

1. **Internal consistency.** Exact keys, declared types (a JSON ``true`` is
   never an integer here), enum membership, suffix agreement between the paired
   paths, unique and ordered paths, parent/child topology matching each route's
   declared shape, a single package root, the state/shape/kind combinations that
   are actually meaningful, and ``expected_counts`` agreeing with the routes
   array.
2. **Checkout agreement.** :func:`validate_checkout` rediscovers both package
   trees from source paths and fails if the shared routes or the legacy-only
   modules differ from the frozen registry.
3. **Source agreement.** :func:`validate_checkout` also parses each legacy
   module it routes and fails if the source contradicts the declared
   ``migration_state``: a ``direct_facade`` leaf that is not an exact
   single-source re-export of its paired canonical module, a ``split_facade``
   leaf that is not an exact re-export *plus* an explicitly legacy-owned set of
   synchronous function definitions, a ``transitive_facade`` barrel that still
   has unconverted children or reaches into the canonical package itself, a
   ``hybrid_facade`` barrel that reaches anything but its converted routed
   children and its declared runtime-only descendants, a ``root_facade`` root
   whose imports are not exactly the frozen owner-by-owner table behind its
   frozen ordered ``__all__``, a still-duplicated leaf that has quietly become
   either kind of facade without its state being moved forward, or a
   ``pending_hybrid``/``pending_root`` route that already qualifies as its
   converted state and is therefore understating what it is.

A ``FacadeManifest`` handed to :func:`validate_checkout` is revalidated before
it is trusted: the public dataclass constructor cannot be used to smuggle a
route set past the invariants above.

Nothing here imports ``omnivia_core`` or ``omnivia_memory``: discovery walks
files and reads names off the filesystem, so loading the registry cannot be
affected by -- or accidentally validate against -- a stale installed copy of
either package. The module is standard-library only for the same reason the
rest of ``baseline`` is: it must run before anything is installed.

The registry stays module-level: a route names a module pair, never a symbol.
Per-symbol ownership, export collisions, and typed-consumer coverage of the
facade are enforced by separate baselines and tests in this repository rather
than encoded as fields here.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeVar

#: Repository root, resolved from this file rather than the process CWD.
REPO_ROOT: Final = Path(__file__).resolve().parent.parent

COMPATIBILITY_DIR: Final = REPO_ROOT / "compatibility"
MANIFEST_PATH: Final = COMPATIBILITY_DIR / "facade-routes.v1.json"
SCHEMA_PATH: Final = COMPATIBILITY_DIR / "facade-routes.schema.json"

#: Pinned identity of the registry. These are not conventions to be inferred
#: from the file being read: a document declaring anything else is rejected,
#: because "which two packages is this?" must never be answered by the data
#: under validation.
FORMAT_VERSION: Final = 1
LEGACY_PACKAGE: Final = "omnivia_memory"
CANONICAL_PACKAGE: Final = "omnivia_core"
COMPATIBILITY_DEPENDENCY: Final = "omnivia-core>=0.1.0,<0.2.0"

#: Source roots for the two package trees, relative to a repository root.
LEGACY_SRC_RELATIVE: Final = Path("services") / "omnivia-memory" / "src"
CANONICAL_SRC_RELATIVE: Final = Path("src")

TOP_LEVEL_KEYS: Final = (
    "format_version",
    "legacy_package",
    "canonical_package",
    "compatibility_dependency",
    "expected_counts",
    "routes",
    "runtime_only_modules",
)
ROUTE_KEYS: Final = (
    "legacy_module",
    "canonical_module",
    "pair_kind",
    "shape",
    "migration_state",
)
COUNT_KEYS: Final = (
    "routes",
    "direct",
    "hybrid_barrel",
    "root",
    "leaf",
    "barrel",
    "runtime_only_modules",
)


class FacadeManifestError(ValueError):
    """Raised when the frozen route registry is invalid or has drifted.

    Carries every diagnostic found in one pass (``errors``) rather than only the
    first, so a drifted checkout reports its whole delta in a single run.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__(
            "; ".join(self.errors) if self.errors else "invalid facade route registry"
        )


class PairKind(str, Enum):
    """Why a legacy/canonical pair can or cannot be converted on its own."""

    #: Nothing in this module's own exported surface blocks converting it: a
    #: leaf pairs one-to-one with its canonical module, and a barrel re-exports
    #: only routed children. A runtime-only *descendant* does not change this --
    #: ``control_plane`` stays direct even though ``control_plane.registry`` is
    #: runtime-owned, because the barrel does not re-export it.
    DIRECT = "direct"
    #: A barrel whose own exported surface mixes portable canonical exports with
    #: runtime-owned ones, so it cannot become a pure re-export of the canonical
    #: package while the runtime-owned half still has to resolve locally.
    HYBRID_BARREL = "hybrid_barrel"
    #: The package root itself.
    ROOT = "root"


class Shape(str, Enum):
    """What kind of module a route points at."""

    #: A plain module.
    LEAF = "leaf"
    #: A package ``__init__`` re-exporting its own subtree.
    BARREL = "barrel"
    #: The package root ``__init__``.
    ROOT = "root"


class MigrationState(str, Enum):
    """How far a route has moved from duplicated source to facade."""

    #: Still a duplicated copy of the canonical module, symbol for symbol.
    SOURCE_PARITY = "source_parity"
    #: Still a duplicated copy, but a *strict superset* of the canonical module:
    #: canonical Core deliberately omits some of the legacy module's symbols
    #: because they are runtime-owned. Not source parity, and not convertible to
    #: a facade until those symbols have somewhere else to live.
    CANONICAL_SUBSET = "canonical_subset"
    #: Re-exports the exact canonical objects itself.
    DIRECT_FACADE = "direct_facade"
    #: Imports its whole *portable* namespace from its exact canonical
    #: counterpart while keeping a small set of explicitly tested,
    #: legacy-owned synchronous function definitions that Core deliberately
    #: excludes. Converted -- every portable name is the canonical object --
    #: but not a pure single-statement re-export, so it has its own source
    #: policy (:func:`split_facade_defects`) rather than the direct one.
    SPLIT_FACADE = "split_facade"
    #: Identity-preserving only through already-converted children.
    TRANSITIVE_FACADE = "transitive_facade"
    #: A *source-unchanged* hybrid barrel that is nonetheless converted for
    #: compatibility accounting. Every portable name it publishes resolves
    #: transitively, through an already-converted routed child, to the exact
    #: canonical Core object; every remaining name it publishes is the exact
    #: legacy object imported from a descendant module named in the registry's
    #: own ``runtime_only_modules``, which Core deliberately does not own.
    #:
    #: It is *not* a ``transitive_facade``: those runtime-only descendants are
    #: not routes, can never become converted children, and are still resolved
    #: locally at import time. Nothing about this state moves runtime code into
    #: Core -- it records that the barrel's portable half has finished
    #: converting and that its runtime half is exactly, and only, the declared
    #: runtime-only surface. Its source policy is
    #: :func:`hybrid_facade_defects`.
    HYBRID_FACADE = "hybrid_facade"
    #: The converted package root. Deliberately *not* ``direct_facade``: the
    #: canonical root exposes only ``__version__`` on purpose, so a single
    #: re-export of ``omnivia_core`` could never carry the legacy root's
    #: advertised contract. This state means the legacy root imports every one of
    #: its advertised names directly from the exact canonical owner or barrel that
    #: publishes it, takes ``__version__`` from the canonical root rather than
    #: assigning one of its own, and keeps a small declared set of non-advertised
    #: compatibility bindings -- two canonical Core inputs, and two deliberately
    #: still owned by legacy runtime modules the registry declares runtime-only.
    #:
    #: It is valid only for ``(PairKind.ROOT, Shape.ROOT)``, counts as converted,
    #: and may replace ``pending_root`` only once every other route is converted
    #: *and* the source satisfies :func:`root_facade_defects`.
    ROOT_FACADE = "root_facade"
    #: A direct barrel awaiting conversion.
    PENDING_DIRECT_BARREL = "pending_direct_barrel"
    #: A hybrid barrel awaiting conversion.
    PENDING_HYBRID = "pending_hybrid"
    #: The package root awaiting conversion.
    PENDING_ROOT = "pending_root"


#: State/shape/kind combinations that mean something. Anything outside this
#: table is a bookkeeping mistake, not a state the migration can be in: a leaf
#: cannot be "transitively" converted (it has no children to convert), a barrel
#: cannot be a ``direct_facade`` while its leaves are still copies, and
#: ``canonical_subset``/``split_facade`` describe one module's symbol set, so
#: only a leaf whose pair is direct can be in either, ``hybrid_facade``
#: describes a barrel that publishes a declared runtime-only half, so only a
#: ``hybrid_barrel`` pair can be in it, and ``root_facade`` describes the package
#: root's own frozen export contract, so only the ``root``/``root`` pair can be
#: in it.
_ALLOWED_COMBINATIONS: Final[frozenset[tuple[PairKind, Shape, MigrationState]]] = (
    frozenset(
        {
            (PairKind.DIRECT, Shape.LEAF, MigrationState.SOURCE_PARITY),
            (PairKind.DIRECT, Shape.LEAF, MigrationState.CANONICAL_SUBSET),
            (PairKind.DIRECT, Shape.LEAF, MigrationState.DIRECT_FACADE),
            (PairKind.DIRECT, Shape.LEAF, MigrationState.SPLIT_FACADE),
            (PairKind.DIRECT, Shape.BARREL, MigrationState.TRANSITIVE_FACADE),
            (PairKind.DIRECT, Shape.BARREL, MigrationState.PENDING_DIRECT_BARREL),
            (PairKind.HYBRID_BARREL, Shape.BARREL, MigrationState.HYBRID_FACADE),
            (PairKind.HYBRID_BARREL, Shape.BARREL, MigrationState.PENDING_HYBRID),
            (PairKind.ROOT, Shape.ROOT, MigrationState.ROOT_FACADE),
            (PairKind.ROOT, Shape.ROOT, MigrationState.PENDING_ROOT),
        }
    )
)


#: States that declare a route has *not* moved yet. Membership here is not a
#: blanket exemption from source checking, and the three members are not treated
#: alike:
#:
#: * ``PENDING_DIRECT_BARREL`` has no source shape to contradict -- the file is
#:   whatever it always was, and the state claims nothing about it -- so
#:   :func:`_collect_source_state_errors` skips it.
#: * ``PENDING_HYBRID`` and ``PENDING_ROOT`` stay in this set because they really
#:   do declare an unmoved route, but they are *retained and specially inspected*
#:   rather than skipped: the moment a hybrid barrel's routed prerequisite
#:   children are all converted *and* its unchanged source already qualifies as an
#:   exact hybrid facade over those children and its declared runtime-only
#:   descendants -- or, for the root, the moment every other route is converted
#:   *and* the root source already satisfies :func:`root_facade_defects` -- the
#:   registry is understating the file and the state must move forward. Until both
#:   conditions hold the route is legitimately pending and nothing is reported.
_PENDING_STATES: Final[frozenset[MigrationState]] = frozenset(
    {
        MigrationState.PENDING_DIRECT_BARREL,
        MigrationState.PENDING_HYBRID,
        MigrationState.PENDING_ROOT,
    }
)

#: The pending states whose source is nonetheless read, so a registry that has
#: fallen behind a file it describes fails rather than silently understating it.
_INSPECTED_PENDING_STATES: Final[frozenset[MigrationState]] = frozenset(
    {MigrationState.PENDING_HYBRID, MigrationState.PENDING_ROOT}
)


# --------------------------------------------------------------------------- #
# the frozen root export contract
# --------------------------------------------------------------------------- #

#: The canonical module the legacy root takes ``__version__`` from. It is the
#: canonical *root*, and that is the whole reason the root cannot be a
#: ``direct_facade``: ``omnivia_core`` advertises only this one name.
ROOT_FACADE_VERSION_MODULE: Final = CANONICAL_PACKAGE

#: The one name the canonical root supplies, and the exact literal ``__all__``
#: that root must declare.
ROOT_FACADE_VERSION_BINDING: Final = "__version__"
CANONICAL_ROOT_ALL: Final[tuple[str, ...]] = (ROOT_FACADE_VERSION_BINDING,)

#: Every canonical module the converted legacy root may import from, and the
#: exact set of names it must take from each. This table *is* the frozen root
#: contract: it is hand-maintained and restated here rather than derived from the
#: file it constrains, so a name moved to a different -- but same-named -- owner
#: fails instead of being absorbed.
#:
#: It also pins the six export collisions independently of import order, because
#: each colliding name appears in exactly one module's set:
#:
#: * ``ValidationResult`` -> ``omnivia_core._shared.validation``, never the
#:   knowledge barrel that also republishes it;
#: * ``SourceRef`` -> ``omnivia_core.knowledge``, never the memory graph's
#:   same-named record;
#: * ``ProvenanceRequirement`` -> ``omnivia_core.component_contract``, never the
#:   app manifest's;
#: * ``LifecycleState`` -> ``omnivia_core.control_plane``, never the lifecycle
#:   domain's;
#: * ``Source`` and ``SourceType`` -> ``omnivia_core.provenance``, never
#:   ingestion's same-named ``Source``.
#:
#: ``MemoryCreate`` and ``MemoryUpdate`` are here too, and are canonical Core
#: objects, but they are not advertised: see
#: :data:`ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS`.
ROOT_FACADE_CANONICAL_IMPORTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "omnivia_core": (
            "__version__",
        ),
        "omnivia_core._shared.validation": (
            "ValidationResult",
        ),
        "omnivia_core.app_manifest": (
            "AppManifest",
            "AppManifestValidationError",
            "AppState",
            "DataSource",
            "validate_app_manifest",
        ),
        "omnivia_core.component_contract": (
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
            "ComponentContractValidationError",
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
            "validate_agent_run_record",
            "validate_component_contract",
        ),
        "omnivia_core.control_plane": (
            "CONTROL_PLANE_CONTRACT_VERSION",
            "CONTROL_PLANE_SCHEMA_VERSION",
            "DANGEROUS_SIDE_EFFECTS",
            "Agent",
            "Approval",
            "AuditEvent",
            "Automation",
            "Capability",
            "CapabilityType",
            "CatalogueArtifactVerification",
            "Connection",
            "ConnectionKind",
            "ConsultantAccessGrant",
            "ConsultantGrantStatus",
            "ControlPlaneManifest",
            "ControlPlaneRunStatus",
            "ControlPlaneValidationError",
            "ExecutionMode",
            "ExecutionResult",
            "ImportedCandidateSet",
            "ImportRecord",
            "ImportSourceChange",
            "ImportSourceProtocol",
            "ImportSpecValidation",
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
            "SecretMetadata",
            "SecretReference",
            "SecretResolutionResult",
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
            "WorkspaceRef",
            "compile_policy_expression",
            "detect_import_source_change",
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
        ),
        "omnivia_core.knowledge": (
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
        ),
        "omnivia_core.memory.models": (
            "MemoryCreate",
            "MemoryUpdate",
        ),
        "omnivia_core.memory_graph": (
            "EvidenceGraphResponse",
            "GraphPreviewResponse",
            "MemoryGraphFixture",
            "RetrievalTrace",
            "build_memory_graph_fixture",
        ),
        "omnivia_core.module_manifest": (
            "Entrypoint",
            "Integrity",
            "ModuleKind",
            "ModuleManifest",
            "ModuleManifestValidationError",
            "Permission",
            "PublishedTarget",
            "validate_module_manifest",
        ),
        "omnivia_core.provenance": (
            "Source",
            "SourceType",
        ),
        "omnivia_core.run_ledger": (
            "RUN_LEDGER_CONTRACT_VERSION",
            "RUN_LEDGER_PATH_ENV",
            "TERMINAL_RUN_STATUSES",
            "EvidenceFileRef",
            "RunLedgerEntry",
            "RunLedgerProvenance",
            "RunLedgerStatus",
            "validate_evidence_file_ref",
            "validate_run_ledger_entry",
            "validate_run_ledger_provenance",
        ),
    }
)

#: The only legacy modules the converted root may import from, and the exact
#: names it may take from each. Both modules must also appear in the registry's
#: own ``runtime_only_modules``, so this table cannot license a runtime import
#: from a module the registry has not declared. Core deliberately does not own
#: either object, and converting the root does not move them.
ROOT_FACADE_RUNTIME_IMPORTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "omnivia_memory.memory.service": (
            "MemoryService",
        ),
        "omnivia_memory.persistence": (
            "Database",
        ),
    }
)

#: The frozen, ordered literal ``__all__`` the converted root must declare, exactly:
#: 182 advertised portable names plus ``__version__``. Order is part of the
#: contract, so this is a tuple compared element by element rather than a set.
ROOT_FACADE_ALL: Final[tuple[str, ...]] = (
    "AgentGraphContext",
    "AppManifest",
    "AppManifestValidationError",
    "AppState",
    "AgentAction",
    "AgentBackedComponentContract",
    "AgentBehavior",
    "AgentRunRecord",
    "AgentRunStatus",
    "ApprovalPolicy",
    "AuditRequirement",
    "BUILTIN_GRAPH_NODE_KINDS",
    "BUILTIN_GRAPH_RELATIONS",
    "BUILTIN_OBJECT_KINDS",
    "ComponentAIMode",
    "ComponentConnectorScope",
    "ComponentContract",
    "ComponentContractValidationError",
    "ComponentDataSource",
    "ComponentFamily",
    "ComponentGraphScope",
    "ComponentInput",
    "ComponentOutput",
    "ComponentOutputType",
    "ComponentPermission",
    "ComponentRunMode",
    "ComponentSafetyLevel",
    "ContractVersion",
    "CONTROL_PLANE_CONTRACT_VERSION",
    "CONTROL_PLANE_SCHEMA_VERSION",
    "DANGEROUS_SIDE_EFFECTS",
    "DataSource",
    "Entrypoint",
    "EXTENSION_MANIFEST_CONTRACT_VERSION",
    "EvidenceFileRef",
    "EvidenceGraphResponse",
    "GRAPH_CONTRACT_VERSION",
    "GraphConfidence",
    "GraphPreviewResponse",
    "GraphEdge",
    "GraphEvidenceStrength",
    "GraphFragment",
    "GraphNode",
    "GraphOrigin",
    "GraphReviewStatus",
    "GraphSensitivity",
    "GraphSourceType",
    "GraphVisibility",
    "Integrity",
    "CatalogueArtifactVerification",
    "ImportSpecValidation",
    "ImportedCandidateSet",
    "KNOWLEDGE_CONTRACT_VERSION",
    "KnowledgeClaim",
    "KnowledgeCollection",
    "KnowledgeExtensionManifest",
    "KnowledgeLink",
    "KnowledgeObject",
    "KnowledgeSource",
    "KnowledgeSpace",
    "MemoryGraphFixture",
    "ModuleKind",
    "ModuleManifest",
    "ModuleManifestValidationError",
    "Permission",
    "PermissionPolicy",
    "PublishedTarget",
    "ProvenanceBehavior",
    "ProvenanceRequirement",
    "RUN_LEDGER_CONTRACT_VERSION",
    "RUN_LEDGER_PATH_ENV",
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
    "ExecutionMode",
    "ExecutionResult",
    "ImportRecord",
    "ImportSourceProtocol",
    "LifecycleState",
    "LocalApprovalNotification",
    "LocalApprovalNotificationChannel",
    "LocalApprovalNotificationEvent",
    "LocalApprovalNotificationStatus",
    "LocalObservabilityLogRecord",
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
    "WorkspaceRef",
    "RetrievalTrace",
    "RunLedgerEntry",
    "RunLedgerProvenance",
    "RunLedgerStatus",
    "Source",
    "SourceRef",
    "SourceType",
    "TERMINAL_RUN_STATUSES",
    "ValidationResult",
    "__version__",
    "build_memory_graph_fixture",
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
    "detect_import_source_change",
    "ImportSourceChange",
    "import_asyncapi_candidates",
    "import_catalogue_candidates",
    "import_catalogue_generated_candidates",
    "import_mcp_candidates",
    "import_openapi_candidates",
    "validate_asyncapi_import_spec",
    "validate_mcp_import_spec",
    "validate_openapi_import_spec",
    "LocalModelInvocationRecord",
    "LocalUsageLedgerEntry",
    "compile_policy_expression",
    "manifest_from_dict",
    "summarize_confidence",
    "summarize_review_status",
    "summarize_sensitivity",
    "validate_agent_graph_context",
    "validate_agent_run_record",
    "validate_app_manifest",
    "validate_component_contract",
    "validate_control_plane_manifest",
    "verify_catalogue_artifacts",
    "validate_evidence_file_ref",
    "validate_graph_edge",
    "validate_graph_fragment",
    "validate_graph_node",
    "validate_module_manifest",
    "validate_knowledge_claim",
    "validate_knowledge_collection",
    "validate_knowledge_extension_manifest",
    "validate_knowledge_link",
    "validate_knowledge_object",
    "validate_knowledge_source",
    "validate_knowledge_space",
    "validate_run_ledger_entry",
    "SecretResolutionResult",
    "validate_run_ledger_provenance",
    "validate_source_ref",
)


#: The two non-advertised compatibility bindings whose objects are canonical
#: Core inputs. Importable from the legacy root, absent from ``__all__``.
ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS: Final[tuple[str, ...]] = (
    "MemoryCreate",
    "MemoryUpdate",
)

#: The two non-advertised compatibility bindings that stay legacy-owned on
#: purpose. ``Database`` and ``MemoryService`` are runtime objects Core does not
#: own; the root keeps them importable without advertising them.
ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS: Final[tuple[str, ...]] = (
    "Database",
    "MemoryService",
)

#: All four non-advertised compatibility bindings, sorted. The converted root's
#: complete public namespace is exactly ``ROOT_FACADE_ALL`` plus these.
ROOT_FACADE_HIDDEN_BINDINGS: Final[tuple[str, ...]] = tuple(
    sorted((*ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS, *ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS))
)


@dataclass(frozen=True, slots=True)
class FacadeRoute:
    """One legacy import path and the canonical path it must resolve to."""

    legacy_module: str
    canonical_module: str
    pair_kind: PairKind
    shape: Shape
    migration_state: MigrationState

    @property
    def suffix(self) -> str:
        """Dotted path below the package root; empty string for the root."""
        return _suffix(self.legacy_module, LEGACY_PACKAGE)

    @property
    def is_converted(self) -> bool:
        """Whether this route has completed its accepted migration-state contract.

        "Converted" is per-state, and it is *not* a claim that every name the
        route publishes is a canonical Core object. Each converted state has its
        own contract and this property only says the route has finished it:

        * ``direct_facade`` and ``transitive_facade`` do resolve their whole
          surface to canonical objects, directly or through converted children.
        * ``split_facade`` resolves its whole *portable* namespace to canonical
          objects while keeping an explicitly pinned set of legacy-owned
          function definitions Core deliberately excludes.
        * ``hybrid_facade`` resolves its portable half to canonical objects
          through converted children while *deliberately keeping the exact
          legacy runtime identities* -- 31 exports across the six hybrid
          barrels today -- imported from descendants the registry declares
          runtime-only. Those identities are legacy on purpose and converting
          the barrel does not move them into Core.
        * ``root_facade`` resolves all 182 advertised names and ``__version__``
          to canonical objects imported straight from their owners, while
          keeping four non-advertised compatibility bindings -- two canonical,
          and ``Database``/``MemoryService`` deliberately still legacy-owned.

        So a true value means "this route is done under its own contract", not
        "everything here is canonical".
        """
        return self.migration_state in (
            MigrationState.DIRECT_FACADE,
            MigrationState.SPLIT_FACADE,
            MigrationState.TRANSITIVE_FACADE,
            MigrationState.HYBRID_FACADE,
            MigrationState.ROOT_FACADE,
        )


@dataclass(frozen=True, slots=True)
class ExpectedCounts:
    """Pinned partition sizes for the routes array."""

    routes: int
    direct: int
    hybrid_barrel: int
    root: int
    leaf: int
    barrel: int
    runtime_only_modules: int

    def as_dict(self) -> dict[str, int]:
        """Counts keyed exactly as they appear in the registry file."""
        return {
            "routes": self.routes,
            "direct": self.direct,
            "hybrid_barrel": self.hybrid_barrel,
            "root": self.root,
            "leaf": self.leaf,
            "barrel": self.barrel,
            "runtime_only_modules": self.runtime_only_modules,
        }


@dataclass(frozen=True)
class FacadeManifest:
    """The validated route registry.

    Immutable throughout: ``routes`` and ``runtime_only_modules`` are tuples and
    the lookup indexes are read-only mappings, so a caller holding the manifest
    cannot edit the frozen facts out from under another caller.
    """

    format_version: int
    legacy_package: str
    canonical_package: str
    compatibility_dependency: str
    expected_counts: ExpectedCounts
    routes: tuple[FacadeRoute, ...]
    runtime_only_modules: tuple[str, ...]
    _by_legacy: Mapping[str, FacadeRoute] = field(init=False, repr=False, compare=False)
    _by_canonical: Mapping[str, FacadeRoute] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_by_legacy",
            MappingProxyType({route.legacy_module: route for route in self.routes}),
        )
        object.__setattr__(
            self,
            "_by_canonical",
            MappingProxyType({route.canonical_module: route for route in self.routes}),
        )

    # -- lookup ---------------------------------------------------------------

    @property
    def by_legacy_module(self) -> Mapping[str, FacadeRoute]:
        """Read-only index from legacy import path to route."""
        return self._by_legacy

    @property
    def by_canonical_module(self) -> Mapping[str, FacadeRoute]:
        """Read-only index from canonical import path to route."""
        return self._by_canonical

    def route_for_legacy(self, module: str) -> FacadeRoute:
        """The route for a legacy import path.

        Raises:
            KeyError: if the path is not a frozen route.
        """
        try:
            return self._by_legacy[module]
        except KeyError:
            raise KeyError(f"no facade route for legacy module {module!r}") from None

    def route_for_canonical(self, module: str) -> FacadeRoute:
        """The route for a canonical import path.

        Raises:
            KeyError: if the path is not a frozen route.
        """
        try:
            return self._by_canonical[module]
        except KeyError:
            raise KeyError(f"no facade route for canonical module {module!r}") from None

    # -- filtered views -------------------------------------------------------

    def by_kind(self, kind: PairKind) -> tuple[FacadeRoute, ...]:
        """Routes with the given pair kind, in registry order."""
        return tuple(route for route in self.routes if route.pair_kind is kind)

    def by_shape(self, shape: Shape) -> tuple[FacadeRoute, ...]:
        """Routes with the given shape, in registry order."""
        return tuple(route for route in self.routes if route.shape is shape)

    def by_state(self, state: MigrationState) -> tuple[FacadeRoute, ...]:
        """Routes in the given migration state, in registry order."""
        return tuple(
            route for route in self.routes if route.migration_state is state
        )

    @property
    def root(self) -> FacadeRoute:
        """The single package-root route."""
        return self.by_kind(PairKind.ROOT)[0]

    @property
    def legacy_modules(self) -> tuple[str, ...]:
        """Every routed legacy import path, in registry order."""
        return tuple(route.legacy_module for route in self.routes)

    @property
    def canonical_modules(self) -> tuple[str, ...]:
        """Every routed canonical import path, in registry order."""
        return tuple(route.canonical_module for route in self.routes)

    @property
    def suffixes(self) -> tuple[str, ...]:
        """Every routed suffix, in registry order. The root's is ``""``."""
        return tuple(route.suffix for route in self.routes)

    @property
    def runtime_only_suffixes(self) -> tuple[str, ...]:
        """Suffixes of the legacy-only modules, in registry order."""
        return tuple(
            _suffix(module, LEGACY_PACKAGE) for module in self.runtime_only_modules
        )

    def observed_counts(self) -> ExpectedCounts:
        """Partition sizes actually present, for comparison against the pins."""
        return ExpectedCounts(
            routes=len(self.routes),
            direct=len(self.by_kind(PairKind.DIRECT)),
            hybrid_barrel=len(self.by_kind(PairKind.HYBRID_BARREL)),
            root=len(self.by_kind(PairKind.ROOT)),
            leaf=len(self.by_shape(Shape.LEAF)),
            barrel=len(self.by_shape(Shape.BARREL)),
            runtime_only_modules=len(self.runtime_only_modules),
        )

    def as_document(self) -> dict[str, object]:
        """This manifest as the JSON document it would have been loaded from.

        Round-tripping through :func:`load_manifest` is how a manifest obtained
        from anywhere other than the loader -- the public dataclass constructor
        included -- is put back under the invariants.
        """
        return {
            "format_version": self.format_version,
            "legacy_package": self.legacy_package,
            "canonical_package": self.canonical_package,
            "compatibility_dependency": self.compatibility_dependency,
            "expected_counts": self.expected_counts.as_dict(),
            "routes": [
                {
                    "legacy_module": route.legacy_module,
                    "canonical_module": route.canonical_module,
                    "pair_kind": route.pair_kind.value,
                    "shape": route.shape.value,
                    "migration_state": route.migration_state.value,
                }
                for route in self.routes
            ],
            "runtime_only_modules": list(self.runtime_only_modules),
        }


@dataclass(frozen=True, slots=True)
class CheckoutModules:
    """Modules discovered in a checkout, without importing either package."""

    #: Suffix -> shape, for suffixes present in both trees.
    shared: Mapping[str, Shape]
    #: Suffix -> shape, for suffixes present only in the legacy tree.
    legacy_only: Mapping[str, Shape]
    #: Suffix -> shape, for suffixes present only in the canonical tree.
    canonical_only: Mapping[str, Shape]


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _suffix(module: str, package: str) -> str:
    if module == package:
        return ""
    return module[len(package) + 1 :]


def _is_int(value: object) -> bool:
    """Whether ``value`` is a JSON integer.

    ``bool`` is a subclass of ``int`` in Python, so a JSON ``true`` would
    otherwise sail through an ``isinstance(value, int)`` count check.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _check_keys(
    present: Iterable[str], expected: Sequence[str], where: str
) -> list[str]:
    found = set(present)
    errors: list[str] = []
    missing = sorted(set(expected) - found)
    unknown = sorted(found - set(expected))
    if missing:
        errors.append(f"{where}: missing keys {missing}")
    if unknown:
        errors.append(f"{where}: unknown keys {unknown}")
    return errors


def _is_ascii_identifier(name: str) -> bool:
    """Whether ``name`` is an ASCII Python identifier.

    ``str.isidentifier`` also accepts non-ASCII identifiers, which the
    registry's JSON Schema pattern does not. The two must agree on what a module
    name is, or the schema and the loader would disagree about the same file.
    """
    return name.isidentifier() and name.isascii()


def _module_is_under(module: str, package: str) -> bool:
    if module == package:
        return True
    if not module.startswith(package + "."):
        return False
    parts = module.split(".")
    return all(_is_ascii_identifier(part) for part in parts)


_EnumT = TypeVar("_EnumT", bound=Enum)


def _parse_enum(
    raw: object, enum: type[_EnumT], where: str, errors: list[str]
) -> _EnumT | None:
    if not isinstance(raw, str):
        errors.append(f"{where}: expected a string, got {type(raw).__name__}")
        return None
    for member in enum:
        if member.value == raw:
            return member
    accepted = [str(member.value) for member in enum]
    errors.append(f"{where}: {raw!r} is not one of {accepted}")
    return None


def _parse_route(index: int, raw: object, errors: list[str]) -> FacadeRoute | None:
    where = f"routes[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected an object, got {type(raw).__name__}")
        return None
    key_errors = _check_keys(raw, ROUTE_KEYS, where)
    if key_errors:
        errors.extend(key_errors)
        return None

    raw_legacy = raw["legacy_module"]
    raw_canonical = raw["canonical_module"]
    legacy = raw_legacy if isinstance(raw_legacy, str) else None
    canonical = raw_canonical if isinstance(raw_canonical, str) else None
    if legacy is None:
        errors.append(
            f"{where}.legacy_module: expected a string, got {type(raw_legacy).__name__}"
        )
    if canonical is None:
        errors.append(
            f"{where}.canonical_module: expected a string, "
            f"got {type(raw_canonical).__name__}"
        )

    pair_kind = _parse_enum(raw["pair_kind"], PairKind, f"{where}.pair_kind", errors)
    shape = _parse_enum(raw["shape"], Shape, f"{where}.shape", errors)
    state = _parse_enum(
        raw["migration_state"], MigrationState, f"{where}.migration_state", errors
    )
    if legacy is None or canonical is None:
        return None
    if pair_kind is None or shape is None or state is None:
        return None
    return FacadeRoute(
        legacy_module=legacy,
        canonical_module=canonical,
        pair_kind=pair_kind,
        shape=shape,
        migration_state=state,
    )


def _parse_counts(raw: object, errors: list[str]) -> ExpectedCounts | None:
    if not isinstance(raw, dict):
        errors.append(f"expected_counts: expected an object, got {type(raw).__name__}")
        return None
    key_errors = _check_keys(raw, COUNT_KEYS, "expected_counts")
    if key_errors:
        errors.extend(key_errors)
        return None
    values: dict[str, int] = {}
    for key in COUNT_KEYS:
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(
                f"expected_counts.{key}: expected an integer, "
                f"got {type(value).__name__}"
            )
            continue
        if value < 0:
            errors.append(f"expected_counts.{key}: expected a non-negative integer")
            continue
        values[key] = value
    if len(values) != len(COUNT_KEYS):
        return None
    return ExpectedCounts(**values)


def _validate_route_set(routes: Sequence[FacadeRoute], errors: list[str]) -> None:
    """Cross-route invariants: identity, uniqueness, ordering, topology."""
    for route in routes:
        where = f"route {route.legacy_module!r}"
        legacy_ok = _module_is_under(route.legacy_module, LEGACY_PACKAGE)
        canonical_ok = _module_is_under(route.canonical_module, CANONICAL_PACKAGE)
        if not legacy_ok:
            errors.append(f"{where}: not a module of package {LEGACY_PACKAGE!r}")
        if not canonical_ok:
            errors.append(
                f"{where}: canonical_module {route.canonical_module!r} is not a "
                f"module of package {CANONICAL_PACKAGE!r}"
            )
        if legacy_ok and canonical_ok:
            legacy_suffix = _suffix(route.legacy_module, LEGACY_PACKAGE)
            canonical_suffix = _suffix(route.canonical_module, CANONICAL_PACKAGE)
            if legacy_suffix != canonical_suffix:
                errors.append(
                    f"{where}: suffix mismatch -- legacy {legacy_suffix!r} vs "
                    f"canonical {canonical_suffix!r}"
                )
        combination = (route.pair_kind, route.shape, route.migration_state)
        if combination not in _ALLOWED_COMBINATIONS:
            errors.append(
                f"{where}: pair_kind {route.pair_kind.value!r} / shape "
                f"{route.shape.value!r} / migration_state "
                f"{route.migration_state.value!r} is not a valid combination"
            )

    for label, paths in (
        ("legacy_module", [route.legacy_module for route in routes]),
        ("canonical_module", [route.canonical_module for route in routes]),
    ):
        seen: set[str] = set()
        duplicates: set[str] = set()
        for path in paths:
            if path in seen:
                duplicates.add(path)
            seen.add(path)
        if duplicates:
            errors.append(f"duplicate {label} paths: {sorted(duplicates)}")

    ordered = sorted(route.legacy_module for route in routes)
    if [route.legacy_module for route in routes] != ordered:
        errors.append("routes must be ordered by legacy_module ascending")

    roots = [route for route in routes if route.shape is Shape.ROOT]
    if len(roots) != 1:
        errors.append(
            f"expected exactly one root route, found {len(roots)}: "
            f"{sorted(route.legacy_module for route in roots)}"
        )
    for route in roots:
        if route.legacy_module != LEGACY_PACKAGE:
            errors.append(
                f"route {route.legacy_module!r}: the root route must be the package "
                f"root {LEGACY_PACKAGE!r}"
            )
    for route in routes:
        if route.legacy_module == LEGACY_PACKAGE and route.shape is not Shape.ROOT:
            errors.append(
                f"route {route.legacy_module!r}: the package root must have shape "
                f"'root', not {route.shape.value!r}"
            )

    _validate_topology(routes, errors)


def _validate_topology(routes: Sequence[FacadeRoute], errors: list[str]) -> None:
    """Every declared shape must match the route tree the registry describes.

    A barrel is a package, so it has at least one routed child; a leaf is a
    plain module, so it has none; and every non-root route has a routed parent.
    Without this, a mislabelled shape would still satisfy the count pins.
    """
    by_suffix = {route.suffix: route for route in routes}
    children: dict[str, list[str]] = {suffix: [] for suffix in by_suffix}
    for suffix in sorted(by_suffix):
        if suffix == "":
            continue
        parent = suffix.rpartition(".")[0]
        if parent not in by_suffix:
            errors.append(
                f"route {by_suffix[suffix].legacy_module!r}: parent module "
                f"{parent!r} is not a route"
            )
            continue
        children[parent].append(suffix)

    for suffix, route in sorted(by_suffix.items()):
        kids = children[suffix]
        if route.shape is Shape.LEAF and kids:
            errors.append(
                f"route {route.legacy_module!r}: shape 'leaf' but it has child "
                f"routes {sorted(kids)}"
            )
        if route.shape is Shape.BARREL and not kids:
            errors.append(
                f"route {route.legacy_module!r}: shape 'barrel' but it has no child "
                f"routes"
            )


def _validate(document: Mapping[str, object]) -> FacadeManifest:
    errors: list[str] = []
    key_errors = _check_keys(document, TOP_LEVEL_KEYS, "manifest")
    if key_errors:
        raise FacadeManifestError(key_errors)

    format_version = document["format_version"]
    if not _is_int(format_version) or format_version != FORMAT_VERSION:
        errors.append(
            f"format_version: expected the integer {FORMAT_VERSION}, "
            f"got {format_version!r}"
        )
    for key, expected in (
        ("legacy_package", LEGACY_PACKAGE),
        ("canonical_package", CANONICAL_PACKAGE),
        ("compatibility_dependency", COMPATIBILITY_DEPENDENCY),
    ):
        value = document[key]
        if value != expected:
            errors.append(f"{key}: expected {expected!r}, got {value!r}")

    counts = _parse_counts(document["expected_counts"], errors)

    raw_routes = document["routes"]
    routes: list[FacadeRoute] = []
    if not isinstance(raw_routes, list):
        errors.append(f"routes: expected an array, got {type(raw_routes).__name__}")
    elif not raw_routes:
        errors.append("routes: expected at least one route")
    else:
        for index, raw_route in enumerate(raw_routes):
            parsed = _parse_route(index, raw_route, errors)
            if parsed is not None:
                routes.append(parsed)

    raw_runtime_only = document["runtime_only_modules"]
    runtime_only: list[str] = []
    if not isinstance(raw_runtime_only, list):
        errors.append(
            f"runtime_only_modules: expected an array, "
            f"got {type(raw_runtime_only).__name__}"
        )
    else:
        for index, raw_module in enumerate(raw_runtime_only):
            where = f"runtime_only_modules[{index}]"
            if not isinstance(raw_module, str):
                errors.append(
                    f"{where}: expected a string, got {type(raw_module).__name__}"
                )
                continue
            if not _module_is_under(raw_module, LEGACY_PACKAGE):
                errors.append(
                    f"{where}: {raw_module!r} is not a module of package "
                    f"{LEGACY_PACKAGE!r}"
                )
                continue
            if raw_module == LEGACY_PACKAGE:
                errors.append(f"{where}: the package root cannot be runtime-only")
                continue
            runtime_only.append(raw_module)

    if errors:
        raise FacadeManifestError(errors)

    _validate_route_set(routes, errors)

    if sorted(runtime_only) != runtime_only:
        errors.append("runtime_only_modules must be ordered ascending")
    duplicates = sorted({m for m in runtime_only if runtime_only.count(m) > 1})
    if duplicates:
        errors.append(f"duplicate runtime_only_modules paths: {duplicates}")
    overlap = sorted(set(runtime_only) & {route.legacy_module for route in routes})
    if overlap:
        errors.append(f"runtime_only_modules also declared as routes: {overlap}")

    if errors or counts is None:
        raise FacadeManifestError(errors)

    manifest = FacadeManifest(
        format_version=FORMAT_VERSION,
        legacy_package=LEGACY_PACKAGE,
        canonical_package=CANONICAL_PACKAGE,
        compatibility_dependency=COMPATIBILITY_DEPENDENCY,
        expected_counts=counts,
        routes=tuple(routes),
        runtime_only_modules=tuple(runtime_only),
    )

    observed = manifest.observed_counts().as_dict()
    pinned = counts.as_dict()
    for key in COUNT_KEYS:
        if observed[key] != pinned[key]:
            errors.append(
                f"expected_counts.{key}: pinned {pinned[key]}, found {observed[key]}"
            )
    if errors:
        raise FacadeManifestError(errors)
    return manifest


def load_manifest(source: Path | Mapping[str, object] | None = None) -> FacadeManifest:
    """Load and validate the frozen route registry.

    Args:
        source: A mapping to validate directly, or a path to a JSON file.
            Defaults to :data:`MANIFEST_PATH`. The mapping form exists so
            callers can validate a mutated copy without writing it to disk.

    Raises:
        FacadeManifestError: if the document is unreadable or invalid.
    """
    if isinstance(source, Mapping):
        return _validate(source)
    path = MANIFEST_PATH if source is None else source
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FacadeManifestError([f"cannot read {path}: {exc}"]) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FacadeManifestError([f"{path} is not valid JSON: {exc}"]) from exc
    if not isinstance(document, dict):
        raise FacadeManifestError(
            [f"{path}: expected a JSON object, got {type(document).__name__}"]
        )
    return _validate(document)


# --------------------------------------------------------------------------- #
# checkout discovery
# --------------------------------------------------------------------------- #


def discover_package_modules(src_root: Path, package: str) -> dict[str, Shape]:
    """Map suffix -> shape for one package tree, without importing it.

    Only the filesystem is read: a directory with an ``__init__.py`` is a
    barrel, any other module file is a leaf, and the package root is the empty
    suffix. Directories without an ``__init__.py`` are not importable as
    packages and are skipped.

    Every identifier-named ``.py`` file other than ``__init__.py`` counts as a
    leaf, including dunder names such as ``__main__``. Skipping those would let
    a module be added to either tree without any check noticing, which is
    exactly what this discovery exists to prevent -- ``__main__`` is importable
    (``import pkg.__main__``) and is a public path like any other.
    """
    package_root = src_root / package
    if not (package_root / "__init__.py").is_file():
        raise FacadeManifestError(
            [f"not an importable package root: {package_root / '__init__.py'}"]
        )
    found: dict[str, Shape] = {"": Shape.ROOT}
    for path in sorted(package_root.rglob("*.py")):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root)
        directories = list(relative.parts[:-1])
        if not all(part.isidentifier() for part in directories):
            continue
        if any(
            not (package_root.joinpath(*directories[: depth + 1]) / "__init__.py").is_file()
            for depth in range(len(directories))
        ):
            continue
        name = relative.parts[-1].removesuffix(".py")
        if name == "__init__":
            if directories:
                found[".".join(directories)] = Shape.BARREL
            continue
        if not name.isidentifier():
            continue
        found[".".join([*directories, name])] = Shape.LEAF
    return found


def discover_checkout(repo_root: Path) -> CheckoutModules:
    """Discover both package trees in ``repo_root`` and partition them."""
    legacy = discover_package_modules(repo_root / LEGACY_SRC_RELATIVE, LEGACY_PACKAGE)
    canonical = discover_package_modules(
        repo_root / CANONICAL_SRC_RELATIVE, CANONICAL_PACKAGE
    )
    shared = {
        suffix: shape for suffix, shape in sorted(legacy.items()) if suffix in canonical
    }
    return CheckoutModules(
        shared=MappingProxyType(shared),
        legacy_only=MappingProxyType(
            {
                suffix: shape
                for suffix, shape in sorted(legacy.items())
                if suffix not in canonical
            }
        ),
        canonical_only=MappingProxyType(
            {
                suffix: shape
                for suffix, shape in sorted(canonical.items())
                if suffix not in legacy
            }
        ),
    )


# --------------------------------------------------------------------------- #
# source state
# --------------------------------------------------------------------------- #


def legacy_source_path(repo_root: Path, route: FacadeRoute) -> Path:
    """The file backing ``route``'s legacy module in ``repo_root``."""
    root = repo_root / LEGACY_SRC_RELATIVE / LEGACY_PACKAGE
    parts = route.suffix.split(".") if route.suffix else []
    if route.shape is Shape.LEAF:
        return root.joinpath(*parts[:-1], f"{parts[-1]}.py")
    return root.joinpath(*parts, "__init__.py")


def canonical_root_source_path(repo_root: Path) -> Path:
    """The canonical package root's own ``__init__.py`` in ``repo_root``."""
    return repo_root / CANONICAL_SRC_RELATIVE / CANONICAL_PACKAGE / "__init__.py"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _parse_module(path: Path, where: str, errors: list[str]) -> ast.Module | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{where}: cannot read {path}: {exc}")
        return None
    try:
        return ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        errors.append(f"{where}: {path} is not valid Python: {exc}")
        return None


def direct_facade_defects(tree: ast.Module, canonical_module: str) -> list[str]:
    """Why ``tree`` is not an exact single-source facade over ``canonical_module``.

    An empty list means it is one, and that is an exact *sequence* of two
    top-level statements rather than a bag of permitted kinds:

    1. exactly one module docstring, and it is the first statement. No other
       standalone string expression appears at module scope;
    2. exactly one absolute, unaliased, non-star
       ``from <canonical_module> import ...`` naming every re-exported symbol,
       and nothing else.

    The docstring rule has to be positional to fail closed. Discarding *every*
    module-scope string would accept a wrapper with no docstring at all, and a
    wrapper carrying leading, middle, or trailing stray strings -- each of those
    is a statement of the wrapper's own, executed at import time, and none of
    them is the accepted shape.

    Anything else fails too -- a second import, a relative import, a star
    import, an alias, or any statement of its own (including ``__all__``, a
    plain ``import x``, a definition, or a module ``__getattr__``/``__dir__``):
    the module would no longer merely forward identities, so callers of the
    legacy path could be getting different objects than callers of the canonical
    one. Comments are invisible to :mod:`ast` and are therefore free.
    """
    defects: list[str] = []

    # Only the *leading* string expression is a docstring; every other one is a
    # statement of the module's own that a blanket ``_is_docstring`` filter would
    # otherwise make invisible below.
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    else:
        defects.append("does not open with a module docstring")
    strays = [node for node in body if _is_docstring(node)]
    if strays:
        defects.append(
            f"has {len(strays)} standalone string expression(s) besides the module "
            f"docstring, at line(s) {sorted(node.lineno for node in strays)}"
        )
        body = [node for node in body if not _is_docstring(node)]

    imports = [node for node in body if isinstance(node, ast.ImportFrom)]
    others = [node for node in body if not isinstance(node, ast.ImportFrom)]
    if others:
        kinds = sorted({type(node).__name__ for node in others})
        defects.append(f"has statements of its own ({', '.join(kinds)})")
    if len(imports) != 1:
        defects.append(f"has {len(imports)} from-imports, expected exactly one")
    for node in imports:
        if node.level != 0:
            defects.append(f"uses a relative import (level {node.level})")
        elif node.module != canonical_module:
            defects.append(
                f"imports from {node.module!r}, not {canonical_module!r}"
            )
        for alias in sorted(node.names, key=lambda item: item.name):
            if alias.name == "*":
                defects.append("uses a star import")
            elif alias.asname is not None:
                defects.append(f"aliases {alias.name!r} as {alias.asname!r}")
    return defects


#: Module-level attribute hooks. A split facade must resolve every name it
#: publishes at import time, so defining either would turn it into a proxy.
_MODULE_DYNAMIC_HOOKS: Final[frozenset[str]] = frozenset({"__dir__", "__getattr__"})


def split_facade_defects(tree: ast.Module, canonical_module: str) -> list[str]:
    """Why ``tree`` is not an exact *split* facade over ``canonical_module``.

    An empty list means it is one, and a split facade is an exact *sequence* of
    top-level statements rather than a bag of permitted kinds:

    1. exactly one module docstring, and it is the first statement. No other
       standalone string expression appears at module scope (comments, which
       :mod:`ast` cannot see, remain free);
    2. exactly one ``from __future__ import annotations`` -- absolute,
       unaliased, no star, naming ``annotations`` exactly once and no other
       future feature. The real statement is required rather than an
       ``annotations`` binding imported from the canonical module: the retained
       definitions below are annotated with postponed string annotations, and
       only the real future import makes that so;
    3. exactly one absolute, unaliased, non-star
       ``from <canonical_module> import ...`` naming every portable name the
       module republishes;
    4. one or more synchronous, undecorated top-level ``def``\\ s -- the
       runtime-owned definitions Core deliberately excludes.

    That order is mandatory. Counting kinds is not enough to fail closed: a
    module with no docstring at all, a second string expression bolted on at the
    end, or a retained helper hoisted above the route import all have exactly
    the permitted kinds in the permitted quantities, and none of them is the
    accepted shape.

    Anything else fails too: a plain ``import x``, a second import from any
    other module, an ``async def``, a class, an assignment (``__all__``
    included), a module ``__getattr__``/``__dir__``, a decorated definition, a
    duplicated imported or defined name, or a definition shadowing an imported
    one. Imports *inside* a retained function are invisible here and stay
    allowed: they are part of that function's preserved body, not the module's
    surface.
    """
    defects: list[str] = []

    # Rule 1. Only the *leading* string expression is a docstring; every other
    # one is a statement of the module's own that ``_is_docstring`` would
    # otherwise make invisible everywhere below.
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    else:
        defects.append("does not open with a module docstring")
    strays = [node for node in body if _is_docstring(node)]
    if strays:
        defects.append(
            f"has {len(strays)} standalone string expression(s) besides the module "
            f"docstring, at line(s) {sorted(node.lineno for node in strays)}"
        )
        body = [node for node in body if not _is_docstring(node)]

    future_imports: list[ast.ImportFrom] = []
    portable_imports: list[ast.ImportFrom] = []
    plain_imports: list[ast.Import] = []
    functions: list[ast.FunctionDef] = []
    coroutines: list[ast.AsyncFunctionDef] = []
    others: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                future_imports.append(node)
            else:
                portable_imports.append(node)
        elif isinstance(node, ast.Import):
            plain_imports.append(node)
        elif isinstance(node, ast.FunctionDef):
            functions.append(node)
        elif isinstance(node, ast.AsyncFunctionDef):
            coroutines.append(node)
        else:
            others.append(node)

    if others:
        kinds = sorted({type(node).__name__ for node in others})
        defects.append(f"has statements of its own ({', '.join(kinds)})")
    for node in plain_imports:
        names = sorted(alias.name for alias in node.names)
        defects.append(
            f"has a top-level plain import of {names}; only from-imports are allowed"
        )
    for coroutine in sorted(coroutines, key=lambda item: item.name):
        defects.append(
            f"defines the async function {coroutine.name!r}; retained definitions "
            f"must be synchronous"
        )

    if len(future_imports) != 1:
        defects.append(
            f"has {len(future_imports)} '__future__' imports, expected exactly one "
            f"'from __future__ import annotations'"
        )
    for node in future_imports:
        if node.level != 0:
            defects.append(f"uses a relative __future__ import (level {node.level})")
        accepted: list[str] = []
        for alias in sorted(node.names, key=lambda item: item.name):
            if alias.name == "*":
                defects.append("uses a star __future__ import")
            elif alias.asname is not None:
                defects.append(
                    f"aliases future feature {alias.name!r} as {alias.asname!r}"
                )
            elif alias.name != "annotations":
                defects.append(
                    f"imports future feature {alias.name!r}, expected only 'annotations'"
                )
            else:
                accepted.append(alias.name)
        # ``from __future__ import annotations, annotations`` names nothing this
        # module may not name, and is still not the accepted statement.
        if len(accepted) != 1:
            defects.append(
                f"names 'annotations' {len(accepted)} times in its '__future__' "
                f"import, expected exactly once"
            )

    if len(portable_imports) != 1:
        defects.append(
            f"has {len(portable_imports)} from-imports besides '__future__', "
            f"expected exactly one"
        )
    imported: list[str] = []
    for node in portable_imports:
        if node.level != 0:
            defects.append(f"uses a relative import (level {node.level})")
        elif node.module != canonical_module:
            defects.append(f"imports from {node.module!r}, not {canonical_module!r}")
        for alias in sorted(node.names, key=lambda item: item.name):
            if alias.name == "*":
                defects.append("uses a star import")
            elif alias.asname is not None:
                defects.append(f"aliases {alias.name!r} as {alias.asname!r}")
            else:
                imported.append(alias.name)

    if not functions:
        defects.append(
            "defines no synchronous top-level function, so nothing is legacy-owned "
            "and it is a plain direct facade"
        )
    defined: list[str] = []
    for function in functions:
        if function.name in _MODULE_DYNAMIC_HOOKS:
            defects.append(f"defines the dynamic module hook {function.name!r}")
        if function.decorator_list:
            defects.append(f"decorates the retained definition {function.name!r}")
        defined.append(function.name)

    for label, names in (("imports", imported), ("defines", defined)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            defects.append(f"{label} the same name twice: {duplicates}")
    shadowed = sorted(set(imported) & set(defined))
    if shadowed:
        defects.append(
            f"defines {shadowed}, which it also imports from {canonical_module!r}"
        )

    # The mandatory order, checked positionally so a source with exactly the
    # right statements in the wrong sequence is still rejected. Kind identity
    # only: *which* module the second statement imports from, and whether the
    # first names only ``annotations``, are the checks above.
    for position, node in enumerate(body):
        if position == 0:
            expected = "'from __future__ import annotations'"
            correct = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        elif position == 1:
            expected = f"the single from-import of {canonical_module!r}"
            correct = isinstance(node, ast.ImportFrom) and node.module != "__future__"
        else:
            expected = "a retained synchronous 'def'"
            correct = isinstance(node, ast.FunctionDef)
        if not correct:
            defects.append(
                f"has {type(node).__name__} as top-level statement "
                f"{position + 1}, expected {expected}"
            )
    return defects


def canonical_imports(tree: ast.Module) -> list[str]:
    """Canonical-package modules ``tree`` imports directly, sorted and deduped."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(
                alias.name
                for alias in node.names
                if _module_is_under(alias.name, CANONICAL_PACKAGE)
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and _module_is_under(node.module, CANONICAL_PACKAGE)
        ):
            found.add(node.module)
    return sorted(found)


def transitive_facade_defects(
    tree: ast.Module,
    route: FacadeRoute,
    converted_children: Sequence[FacadeRoute],
) -> list[str]:
    """Why a barrel is not a pure transitive facade over converted children."""
    defects: list[str] = []
    body = [node for node in tree.body if not _is_docstring(node)]
    imports = [node for node in body if isinstance(node, ast.ImportFrom)]
    assignments = [node for node in body if isinstance(node, ast.Assign)]
    other = [
        node
        for node in body
        if not isinstance(node, (ast.ImportFrom, ast.Assign))
    ]
    if other:
        defects.append(
            "has statements of its own "
            f"({', '.join(sorted({type(node).__name__ for node in other}))})"
        )

    allowed_children = {child.legacy_module for child in converted_children}
    reached_children: set[str] = set()
    imported_names: list[str] = []
    for node in imports:
        if node.level == 0:
            imported_module = node.module
        elif node.level == 1 and node.module:
            imported_module = f"{route.legacy_module}.{node.module}"
        else:
            imported_module = None
        if imported_module not in allowed_children:
            defects.append(
                f"imports unapproved module {imported_module!r}; expected one of "
                f"{sorted(allowed_children)}"
            )
        else:
            reached_children.add(imported_module)
        for alias in node.names:
            if alias.name == "*":
                defects.append("uses a star import")
            elif alias.asname is not None:
                defects.append(f"aliases {alias.name!r} as {alias.asname!r}")
            else:
                imported_names.append(alias.name)

    missing_children = sorted(allowed_children - reached_children)
    if missing_children:
        defects.append(f"does not import converted children {missing_children}")

    if len(assignments) != 1:
        defects.append(
            f"has {len(assignments)} assignments, expected exactly one literal __all__"
        )
        exported_names: list[str] = []
    else:
        assignment = assignments[0]
        target_names = [
            target.id for target in assignment.targets if isinstance(target, ast.Name)
        ]
        if target_names != ["__all__"] or len(assignment.targets) != 1:
            defects.append("assigns a name other than the single __all__ target")
        value = assignment.value
        if not isinstance(value, (ast.List, ast.Tuple)) or any(
            not isinstance(element, ast.Constant)
            or not isinstance(element.value, str)
            for element in value.elts
        ):
            defects.append("__all__ is not a literal list/tuple of strings")
            exported_names = []
        else:
            exported_names = []
            for element in value.elts:
                assert isinstance(element, ast.Constant)
                assert isinstance(element.value, str)
                exported_names.append(element.value)

    if len(exported_names) != len(set(exported_names)):
        defects.append("__all__ contains duplicate names")
    if sorted(imported_names) != sorted(exported_names):
        defects.append(
            "the imported binding set does not exactly match the literal __all__"
        )
    return defects


def _literal_all(
    assignments: Sequence[ast.Assign], defects: list[str]
) -> list[str] | None:
    """The names in a single literal ``__all__`` assignment, or ``None``.

    ``None`` means the assignment was not the accepted shape and a defect has
    already been recorded, so the caller must not go on to compare a bogus
    export set against the imported bindings.
    """
    if len(assignments) != 1:
        defects.append(
            f"has {len(assignments)} assignments, expected exactly one literal __all__"
        )
        return None
    assignment = assignments[0]
    target_names = [
        target.id for target in assignment.targets if isinstance(target, ast.Name)
    ]
    if len(assignment.targets) != 1 or target_names != ["__all__"]:
        defects.append("assigns a name other than the single __all__ target")
        return None
    value = assignment.value
    if not isinstance(value, (ast.List, ast.Tuple)) or any(
        not isinstance(element, ast.Constant) or not isinstance(element.value, str)
        for element in value.elts
    ):
        defects.append("__all__ is not a literal list/tuple of strings")
        return None
    exported: list[str] = []
    for element in value.elts:
        assert isinstance(element, ast.Constant)
        assert isinstance(element.value, str)
        exported.append(element.value)
    return exported


def hybrid_facade_defects(
    tree: ast.Module,
    route: FacadeRoute,
    routed_children: Sequence[FacadeRoute],
    runtime_only_modules: Sequence[str],
) -> list[str]:
    """Why ``tree`` is not an exact *hybrid* facade for ``route``.

    An empty list means it is one. A hybrid facade is a **source-unchanged**
    legacy barrel that is nonetheless converted for compatibility accounting:
    its portable bindings resolve transitively through already-converted routed
    children to the exact canonical objects, while its runtime-only bindings
    stay the exact legacy objects imported from descendant modules the registry
    explicitly declares runtime-only. Nothing moves into Core; what is asserted
    is that the barrel reaches *nothing else*.

    The accepted top-level form is:

    1. an optional module docstring (comments are invisible to :mod:`ast` and
       are therefore free);
    2. two or more absolute, unaliased, non-star ``from ... import ...``
       statements, each naming a distinct module that is either an immediate
       routed child of the barrel that is already converted, or a legacy
       descendant of the barrel named exactly in the registry's
       ``runtime_only_modules``;
    3. exactly one assignment, of a literal list/tuple of strings to
       ``__all__``;
    4. nothing else.

    At least one import of each classification is required: a barrel that
    reaches only converted children is a ``transitive_facade``, and one that
    reaches only runtime-only modules has no portable half to have converted.
    Every routed *leaf* child must be reached, so a barrel cannot quietly stop
    re-exporting a converted leaf and still count as converted. A routed child
    that is itself a barrel is a separately routed subtree with its own
    contract, and a parent is not required to re-export it -- ``ingestion``
    deliberately does not re-export ``ingestion.watcher``.

    Anything else fails: an unconverted routed child, a canonical import, a
    relative import, a module outside the barrel's own subtree, an undeclared
    runtime module, a plain ``import x``, an alias, a star, two import blocks
    naming the same module, a duplicated binding or ``__all__`` entry, any
    other statement of the module's own (a definition, a second assignment, an
    annotation, control flow, or a module ``__getattr__``/``__dir__``), or an
    imported binding set that is not exactly the literal ``__all__``.
    """
    defects: list[str] = []

    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]

    imports = [node for node in body if isinstance(node, ast.ImportFrom)]
    assignments = [node for node in body if isinstance(node, ast.Assign)]
    plain_imports = [node for node in body if isinstance(node, ast.Import)]
    others = [
        node
        for node in body
        if not isinstance(node, (ast.ImportFrom, ast.Assign, ast.Import))
    ]
    for node in others:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _MODULE_DYNAMIC_HOOKS
        ):
            defects.append(f"defines the dynamic module hook {node.name!r}")
    if others:
        kinds = sorted({type(node).__name__ for node in others})
        defects.append(f"has statements of its own ({', '.join(kinds)})")
    for node in plain_imports:
        names = sorted(alias.name for alias in node.names)
        defects.append(
            f"has a top-level plain import of {names}; only from-imports are allowed"
        )

    children_by_module = {child.legacy_module: child for child in routed_children}
    declared_runtime = {
        module
        for module in runtime_only_modules
        if module != route.legacy_module
        and _module_is_under(module, route.legacy_module)
    }

    reached_children: set[str] = set()
    reached_runtime: set[str] = set()
    seen_modules: set[str] = set()
    imported_names: list[str] = []
    for node in imports:
        if node.level != 0:
            defects.append(
                f"uses a relative import (level {node.level}) of {node.module!r}"
            )
            module = None
        else:
            module = node.module
        if module is not None:
            if module in seen_modules:
                defects.append(f"imports from {module!r} in more than one block")
            seen_modules.add(module)
            child = children_by_module.get(module)
            if child is not None and child.is_converted:
                reached_children.add(module)
            elif child is not None:
                defects.append(
                    f"imports the routed child {module!r}, which is not converted "
                    f"(migration_state {child.migration_state.value!r})"
                )
            elif module in declared_runtime:
                reached_runtime.add(module)
            elif _module_is_under(module, CANONICAL_PACKAGE):
                defects.append(
                    f"imports the canonical module {module!r} directly; a hybrid "
                    f"facade routes its portable half through its converted children"
                )
            elif module == route.legacy_module:
                defects.append(f"imports itself ({module!r})")
            elif not _module_is_under(module, route.legacy_module):
                defects.append(
                    f"imports {module!r}, which is outside the "
                    f"{route.legacy_module!r} subtree"
                )
            else:
                defects.append(
                    f"imports {module!r}, a descendant module that is neither a "
                    f"routed child nor declared runtime-only"
                )
        for alias in node.names:
            if alias.name == "*":
                defects.append("uses a star import")
            elif alias.asname is not None:
                defects.append(f"aliases {alias.name!r} as {alias.asname!r}")
            else:
                imported_names.append(alias.name)

    required_children = {
        child.legacy_module
        for child in routed_children
        if child.shape is not Shape.BARREL
    }
    missing_children = sorted(required_children - reached_children)
    if missing_children:
        defects.append(f"does not import routed children {missing_children}")
    if not reached_children:
        defects.append(
            "imports no converted routed child, so nothing about it is a facade"
        )
    if not reached_runtime:
        defects.append(
            "imports no declared runtime-only descendant, so it is not a hybrid; "
            "a barrel that reaches only converted children is a transitive facade"
        )

    duplicate_bindings = sorted(
        {name for name in imported_names if imported_names.count(name) > 1}
    )
    if duplicate_bindings:
        defects.append(f"binds the same imported name twice: {duplicate_bindings}")

    exported_names = _literal_all(assignments, defects)
    if exported_names is not None:
        duplicate_exports = sorted(
            {name for name in exported_names if exported_names.count(name) > 1}
        )
        if duplicate_exports:
            defects.append(f"__all__ contains duplicate names: {duplicate_exports}")
        if sorted(set(imported_names)) != sorted(set(exported_names)):
            defects.append(
                "the imported binding set does not exactly match the literal __all__"
            )
    return defects


def canonical_root_defects(tree: ast.Module) -> list[str]:
    """Why the canonical package root is not version-only.

    An empty list means it is. The canonical root deliberately advertises nothing
    but ``__version__`` -- that is the reason the *legacy* root cannot be a
    ``direct_facade`` over it, so it has to be a checked fact rather than a
    remark: if Core's root started exporting contracts of its own, the legacy
    root's 182-owner import table would be one plausible re-export away from
    being replaceable by a single wildcard, and reviewers would stop being told.

    The accepted form is a module docstring, an optional
    ``from __future__ import annotations``, one ``__version__`` assigned a string
    literal, and one literal ``__all__`` equal to
    :data:`CANONICAL_ROOT_ALL`. Any other statement, assignment, definition,
    import, or module ``__getattr__``/``__dir__`` is a defect.
    """
    defects: list[str] = []

    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    else:
        defects.append("does not open with a module docstring")
    strays = [node for node in body if _is_docstring(node)]
    if strays:
        defects.append(
            f"has {len(strays)} standalone string expression(s) besides the module "
            f"docstring, at line(s) {sorted(node.lineno for node in strays)}"
        )
        body = [node for node in body if not _is_docstring(node)]

    assignments: list[ast.Assign] = []
    others: list[ast.stmt] = []
    for node in body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "__future__"
        ):
            for alias in sorted(node.names, key=lambda item: item.name):
                if alias.name != "annotations" or alias.asname is not None:
                    defects.append(
                        f"imports the future feature {alias.name!r} as "
                        f"{alias.asname!r}, expected only 'annotations'"
                    )
            continue
        if isinstance(node, ast.Assign):
            assignments.append(node)
            continue
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _MODULE_DYNAMIC_HOOKS
        ):
            defects.append(f"defines the dynamic module hook {node.name!r}")
        others.append(node)
    if others:
        kinds = sorted({type(node).__name__ for node in others})
        defects.append(
            f"has statements besides its version and __all__ ({', '.join(kinds)})"
        )

    targets: dict[str, ast.Assign] = {}
    for node in assignments:
        names = [
            target.id for target in node.targets if isinstance(target, ast.Name)
        ]
        if len(node.targets) != 1 or len(names) != 1:
            defects.append("has an assignment whose target is not a single plain name")
            continue
        if names[0] in targets:
            defects.append(f"assigns {names[0]!r} more than once")
            continue
        targets[names[0]] = node

    unexpected = sorted(set(targets) - {ROOT_FACADE_VERSION_BINDING, "__all__"})
    if unexpected:
        defects.append(f"assigns {unexpected} besides its version and __all__")

    version = targets.get(ROOT_FACADE_VERSION_BINDING)
    if version is None:
        defects.append(f"does not assign {ROOT_FACADE_VERSION_BINDING!r}")
    elif not (
        isinstance(version.value, ast.Constant) and isinstance(version.value.value, str)
    ):
        defects.append(
            f"assigns a non-string {ROOT_FACADE_VERSION_BINDING!r}"
        )

    all_assignment = targets.get("__all__")
    if all_assignment is None:
        defects.append("declares no literal __all__")
    else:
        exported = _literal_all([all_assignment], defects)
        if exported is not None and tuple(exported) != CANONICAL_ROOT_ALL:
            defects.append(
                f"declares __all__ {exported}, expected {list(CANONICAL_ROOT_ALL)}"
            )
    return defects


def root_facade_defects(
    tree: ast.Module,
    route: FacadeRoute,
    routes: Sequence[FacadeRoute],
    runtime_only_modules: Sequence[str],
    canonical_root: ast.Module | None = None,
) -> list[str]:
    """Why ``tree`` is not an exact *root* facade for ``route``.

    An empty list means it is one. This is the strictest of the source policies
    because the root is the only module whose advertised surface spans every
    domain at once, so "it imports the right names" is not enough: *which owner*
    each name comes from is the contract, and several names are published under
    the same spelling by more than one domain.

    The accepted top-level form is:

    1. exactly one module docstring, and it is the first statement. No other
       standalone string expression appears at module scope (comments are
       invisible to :mod:`ast` and stay free);
    2. one absolute, unaliased, non-star ``from ... import ...`` per approved
       owner -- every key of :data:`ROOT_FACADE_CANONICAL_IMPORTS` and
       :data:`ROOT_FACADE_RUNTIME_IMPORTS`, each reached exactly once and each
       naming exactly that owner's declared name set;
    3. exactly one assignment, of the frozen ordered literal
       :data:`ROOT_FACADE_ALL` to ``__all__``, and it is the last statement.

    What that buys, checked as separate named facts rather than left implied:

    * every route other than the root is already converted, so the root cannot be
      declared terminal on top of unconverted children;
    * every advertised name and ``__version__`` is a canonical import, and
      ``__version__`` comes from the canonical *root* -- not a second version
      string assigned here;
    * the only legacy-runtime imports are
      :data:`ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS`, and only from modules the
      registry itself declares runtime-only;
    * the imported binding set is exactly ``__all__`` plus the four
      :data:`ROOT_FACADE_HIDDEN_BINDINGS`, so the module's whole public namespace
      is accounted for and no extra binding (an ``annotations`` future feature
      included) can appear;
    * the four hidden bindings are absent from ``__all__``;
    * the canonical root is still version-only.

    Anything else fails: a relative or star import, an alias, a second block for
    one owner, a plain ``import x``, an owner that is not in the table, a
    definition, a second assignment, a module ``__getattr__``/``__dir__``, a
    duplicated binding or ``__all__`` entry, a reordered ``__all__``, or a
    statement sitting after the ``__all__`` assignment.
    """
    defects: list[str] = []

    if route.pair_kind is not PairKind.ROOT or route.shape is not Shape.ROOT:
        defects.append(
            f"is not the package root: {route.legacy_module!r} is a "
            f"{route.pair_kind.value}/{route.shape.value} route"
        )

    unconverted = sorted(
        other.legacy_module
        for other in routes
        if other.pair_kind is not PairKind.ROOT and not other.is_converted
    )
    if unconverted:
        defects.append(
            f"is offered as terminal while these routes are still unconverted: "
            f"{unconverted}"
        )

    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    else:
        defects.append("does not open with a module docstring")
    strays = [node for node in body if _is_docstring(node)]
    if strays:
        defects.append(
            f"has {len(strays)} standalone string expression(s) besides the module "
            f"docstring, at line(s) {sorted(node.lineno for node in strays)}"
        )
        body = [node for node in body if not _is_docstring(node)]

    imports = [node for node in body if isinstance(node, ast.ImportFrom)]
    assignments = [node for node in body if isinstance(node, ast.Assign)]
    plain_imports = [node for node in body if isinstance(node, ast.Import)]
    others = [
        node
        for node in body
        if not isinstance(node, (ast.ImportFrom, ast.Assign, ast.Import))
    ]
    for node in others:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _MODULE_DYNAMIC_HOOKS
        ):
            defects.append(f"defines the dynamic module hook {node.name!r}")
    if others:
        kinds = sorted({type(node).__name__ for node in others})
        defects.append(f"has statements of its own ({', '.join(kinds)})")
    for node in plain_imports:
        plain_names = sorted(alias.name for alias in node.names)
        defects.append(
            f"has a top-level plain import of {plain_names}; only from-imports are "
            f"allowed"
        )

    # The mandatory sequence, checked positionally: every import first, then the
    # single ``__all__``. Counting kinds is not enough -- a source with exactly
    # the right statements in the wrong order is still not the accepted shape.
    if body:
        if not isinstance(body[-1], ast.Assign):
            defects.append(
                f"has {type(body[-1]).__name__} as its last top-level statement, "
                f"expected the literal __all__ assignment"
            )
        misplaced = sorted(
            node.lineno for node in body[:-1] if not isinstance(node, ast.ImportFrom)
        )
        if misplaced:
            defects.append(
                f"has non-import statements before its __all__ assignment, at "
                f"line(s) {misplaced}"
            )

    approved: dict[str, tuple[str, ...]] = {
        **ROOT_FACADE_CANONICAL_IMPORTS,
        **ROOT_FACADE_RUNTIME_IMPORTS,
    }
    declared_runtime = set(runtime_only_modules)
    for runtime_owner in sorted(ROOT_FACADE_RUNTIME_IMPORTS):
        if runtime_owner not in declared_runtime:
            defects.append(
                f"would take a legacy-owned binding from {runtime_owner!r}, which the "
                f"registry does not declare runtime-only"
            )
        for name in ROOT_FACADE_RUNTIME_IMPORTS[runtime_owner]:
            if name not in ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS:
                defects.append(
                    f"declares {name!r} as a legacy-runtime import of "
                    f"{runtime_owner!r} without declaring it a hidden runtime binding"
                )

    reached: dict[str, list[str]] = {}
    imported_names: list[str] = []
    for node in imports:
        module: str | None
        if node.level != 0:
            defects.append(
                f"uses a relative import (level {node.level}) of {node.module!r}"
            )
            module = None
        else:
            module = node.module
        names: list[str] = []
        for alias in node.names:
            if alias.name == "*":
                defects.append("uses a star import")
            elif alias.asname is not None:
                defects.append(f"aliases {alias.name!r} as {alias.asname!r}")
            else:
                names.append(alias.name)
                imported_names.append(alias.name)
        if module is None:
            continue
        if module in reached:
            defects.append(f"imports from {module!r} in more than one block")
            reached[module].extend(names)
            continue
        reached[module] = names
        if module in approved:
            continue
        if _module_is_under(module, CANONICAL_PACKAGE):
            defects.append(
                f"imports the canonical module {module!r}, which is not an approved "
                f"root owner"
            )
        elif _module_is_under(module, LEGACY_PACKAGE):
            defects.append(
                f"imports the legacy module {module!r}; only "
                f"{sorted(ROOT_FACADE_RUNTIME_IMPORTS)} may supply a legacy-owned "
                f"compatibility binding"
            )
        else:
            defects.append(f"imports {module!r}, which is outside both packages")

    for module in sorted(approved):
        if module not in reached:
            defects.append(f"does not import the approved owner {module!r}")
            continue
        missing = sorted(set(approved[module]) - set(reached[module]))
        extra = sorted(set(reached[module]) - set(approved[module]))
        if missing or extra:
            defects.append(
                f"imports the wrong names from {module!r}: missing {missing}, "
                f"extra {extra}"
            )

    if ROOT_FACADE_VERSION_BINDING not in reached.get(ROOT_FACADE_VERSION_MODULE, ()):
        defects.append(
            f"does not import {ROOT_FACADE_VERSION_BINDING!r} from "
            f"{ROOT_FACADE_VERSION_MODULE!r}"
        )

    # Observed rather than read off the table: the same two claims, made against
    # what the source actually reaches, so a table edit alone cannot satisfy them.
    from_canonical = {
        name
        for module, names in reached.items()
        if _module_is_under(module, CANONICAL_PACKAGE)
        for name in names
    }
    from_legacy = {
        name
        for module, names in reached.items()
        if _module_is_under(module, LEGACY_PACKAGE)
        for name in names
    }
    not_canonical = sorted(set(ROOT_FACADE_ALL) - from_canonical)
    if not_canonical:
        defects.append(
            f"does not import {not_canonical} from a canonical owner; every "
            f"advertised name and the version must be a canonical import"
        )
    unsanctioned_legacy = sorted(from_legacy - set(ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS))
    if unsanctioned_legacy:
        defects.append(
            f"imports {unsanctioned_legacy} from a legacy runtime module; only "
            f"{list(ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS)} may stay legacy-owned"
        )

    local_targets = sorted(
        {
            target.id
            for node in assignments
            for target in node.targets
            if isinstance(target, ast.Name) and target.id != "__all__"
        }
    )
    if local_targets:
        defects.append(
            f"assigns {local_targets} of its own; the root may only assign __all__, "
            f"and must import its version rather than restating it"
        )

    duplicate_bindings = sorted(
        {name for name in imported_names if imported_names.count(name) > 1}
    )
    if duplicate_bindings:
        defects.append(f"binds the same imported name twice: {duplicate_bindings}")

    expected_bindings = {*ROOT_FACADE_ALL, *ROOT_FACADE_HIDDEN_BINDINGS}
    if set(imported_names) != expected_bindings:
        defects.append(
            f"the imported binding set is not exactly __all__ plus the four hidden "
            f"compatibility bindings: missing "
            f"{sorted(expected_bindings - set(imported_names))}, extra "
            f"{sorted(set(imported_names) - expected_bindings)}"
        )

    exported_names = _literal_all(assignments, defects)
    if exported_names is not None:
        duplicate_exports = sorted(
            {name for name in exported_names if exported_names.count(name) > 1}
        )
        if duplicate_exports:
            defects.append(f"__all__ contains duplicate names: {duplicate_exports}")
        if tuple(exported_names) != ROOT_FACADE_ALL:
            missing = sorted(set(ROOT_FACADE_ALL) - set(exported_names))
            extra = sorted(set(exported_names) - set(ROOT_FACADE_ALL))
            if missing or extra:
                defects.append(
                    f"__all__ drifted from the frozen contract: missing {missing}, "
                    f"extra {extra}"
                )
            else:
                defects.append(
                    "__all__ names the frozen set in a different order; the order is "
                    "part of the contract"
                )
        advertised_hidden = [
            name for name in ROOT_FACADE_HIDDEN_BINDINGS if name in exported_names
        ]
        if advertised_hidden:
            defects.append(
                f"advertises the deliberately non-advertised binding(s) "
                f"{advertised_hidden}"
            )

    if canonical_root is not None:
        for defect in canonical_root_defects(canonical_root):
            defects.append(f"pairs with a canonical root that {defect}")
    return defects


def _collect_source_state_errors(
    repo_root: Path,
    routes: Sequence[FacadeRoute],
    all_routes: Sequence[FacadeRoute],
    runtime_only_modules: Sequence[str],
    errors: list[str],
) -> None:
    """Fail every route in ``routes`` whose source contradicts its state.

    ``all_routes`` supplies the routed children a ``transitive_facade`` or
    ``hybrid_facade`` barrel is judged against, so a subset of routes can be
    checked without losing the topology they sit in.
    ``runtime_only_modules`` supplies the legacy-only modules a
    ``hybrid_facade`` barrel is allowed to reach.

    ``pending_hybrid`` and ``pending_root`` are deliberately *not* skipped with
    ``pending_direct_barrel``. A pending state means "the source is whatever it
    always was, so there is nothing to contradict", and that stops being true the
    moment a hybrid barrel's routed children are all converted and its source
    already satisfies :func:`hybrid_facade_defects` -- or the moment every
    non-root route is converted and the root's source already satisfies
    :func:`root_facade_defects`. Either way the registry is understating the file,
    which is exactly the drift this gate exists to catch.
    """
    children: dict[str, list[FacadeRoute]] = {}
    for route in all_routes:
        if route.suffix:
            children.setdefault(route.suffix.rpartition(".")[0], []).append(route)

    for route in sorted(routes, key=lambda item: item.legacy_module):
        state = route.migration_state
        if state in _PENDING_STATES and state not in _INSPECTED_PENDING_STATES:
            continue
        where = f"route {route.legacy_module!r}"
        tree = _parse_module(legacy_source_path(repo_root, route), where, errors)
        if tree is None:
            continue

        if state is MigrationState.DIRECT_FACADE:
            for defect in direct_facade_defects(tree, route.canonical_module):
                errors.append(
                    f"{where}: declared 'direct_facade' but the source {defect}"
                )
        elif state is MigrationState.SPLIT_FACADE:
            for defect in split_facade_defects(tree, route.canonical_module):
                errors.append(
                    f"{where}: declared 'split_facade' but the source {defect}"
                )
        elif state is MigrationState.TRANSITIVE_FACADE:
            routed_children = children.get(route.suffix, ())
            unconverted = sorted(
                child.legacy_module for child in routed_children if not child.is_converted
            )
            if unconverted:
                errors.append(
                    f"{where}: declared 'transitive_facade' but these routed "
                    f"children are not converted: {unconverted}"
                )
            converted_children = [
                child for child in routed_children if child.is_converted
            ]
            for defect in transitive_facade_defects(
                tree, route, converted_children
            ):
                errors.append(
                    f"{where}: declared 'transitive_facade' but the source {defect}"
                )
        elif state is MigrationState.HYBRID_FACADE:
            routed_children = children.get(route.suffix, ())
            unconverted = sorted(
                child.legacy_module
                for child in routed_children
                if not child.is_converted
            )
            if unconverted:
                errors.append(
                    f"{where}: declared 'hybrid_facade' but these routed children "
                    f"are not converted: {unconverted}"
                )
            for defect in hybrid_facade_defects(
                tree, route, routed_children, runtime_only_modules
            ):
                errors.append(
                    f"{where}: declared 'hybrid_facade' but the source {defect}"
                )
        elif state is MigrationState.ROOT_FACADE:
            canonical_root = _parse_module(
                canonical_root_source_path(repo_root), where, errors
            )
            for defect in root_facade_defects(
                tree, route, all_routes, runtime_only_modules, canonical_root
            ):
                errors.append(
                    f"{where}: declared 'root_facade' but the source {defect}"
                )
        elif state is MigrationState.PENDING_HYBRID:
            routed_children = children.get(route.suffix, ())
            if all(child.is_converted for child in routed_children) and not (
                hybrid_facade_defects(
                    tree, route, routed_children, runtime_only_modules
                )
            ):
                errors.append(
                    f"{where}: declared 'pending_hybrid' but every routed child is "
                    f"converted and the source is an exact hybrid facade over them "
                    f"and its declared runtime-only descendants; move the state "
                    f"forward"
                )
        elif state is MigrationState.PENDING_ROOT:
            canonical_root = _parse_module(
                canonical_root_source_path(repo_root), where, errors
            )
            if canonical_root is not None and not root_facade_defects(
                tree, route, all_routes, runtime_only_modules, canonical_root
            ):
                errors.append(
                    f"{where}: declared 'pending_root' but every other route is "
                    f"converted and the source is already an exact root facade over "
                    f"its approved canonical owners and barrels; move the state "
                    f"forward"
                )
        elif not direct_facade_defects(tree, route.canonical_module):
            errors.append(
                f"{where}: declared {state.value!r} but the source is an exact "
                f"facade over {route.canonical_module!r}; move the state forward"
            )
        elif not split_facade_defects(tree, route.canonical_module):
            errors.append(
                f"{where}: declared {state.value!r} but the source is an exact split "
                f"facade over {route.canonical_module!r}, keeping only legacy-owned "
                f"function definitions of its own; move the state forward"
            )


def validate_route_sources(
    repo_root: Path = REPO_ROOT,
    manifest: FacadeManifest | Path | Mapping[str, object] | None = None,
) -> None:
    """Check every route's legacy source against its declared migration state.

    Raises:
        FacadeManifestError: listing every contradiction found.
    """
    resolved = _resolve_manifest(manifest)
    errors: list[str] = []
    _collect_source_state_errors(
        repo_root,
        resolved.routes,
        resolved.routes,
        resolved.runtime_only_modules,
        errors,
    )
    if errors:
        raise FacadeManifestError(errors)


# --------------------------------------------------------------------------- #
# checkout validation
# --------------------------------------------------------------------------- #


def _resolve_manifest(
    manifest: FacadeManifest | Path | Mapping[str, object] | None,
) -> FacadeManifest:
    """Return a manifest that has been through :func:`load_manifest`.

    A :class:`FacadeManifest` is *not* taken on trust. It is a public frozen
    dataclass, so anything can build one directly -- with duplicate routes,
    counts that do not match its own route array, or a state/shape combination
    the migration cannot be in -- and none of that goes through ``_validate`` on
    construction. Round-tripping it back through the loader means the only
    manifest this module ever validates a checkout against is a validated one.
    """
    if isinstance(manifest, FacadeManifest):
        return load_manifest(manifest.as_document())
    return load_manifest(manifest)


def validate_checkout(
    repo_root: Path = REPO_ROOT,
    manifest: FacadeManifest | Path | Mapping[str, object] | None = None,
) -> CheckoutModules:
    """Check a checkout's package trees against the frozen registry.

    Every module suffix present in both trees must be a frozen route with the
    shape the filesystem shows, and every legacy-only module must be declared
    runtime-only. Routes whose shape agrees are then checked against their own
    source, so a declared ``migration_state`` cannot drift from what the file
    actually does. Canonical-only modules (the contract layer) are out of scope
    for this checkpoint and are reported but not pinned.

    A ``FacadeManifest`` argument is revalidated before use; see
    :func:`_resolve_manifest`.

    Raises:
        FacadeManifestError: on any drift, listing every difference found.
    """
    resolved = _resolve_manifest(manifest)
    modules = discover_checkout(repo_root)
    errors: list[str] = []

    routed = {route.suffix: route for route in resolved.routes}
    missing = sorted(set(modules.shared) - set(routed))
    unexpected = sorted(set(routed) - set(modules.shared))
    if missing:
        errors.append(f"shared modules with no frozen route: {missing}")
    if unexpected:
        errors.append(f"frozen routes not shared by both trees: {unexpected}")
    agreed: list[FacadeRoute] = []
    for suffix in sorted(set(routed) & set(modules.shared)):
        observed = modules.shared[suffix]
        declared = routed[suffix].shape
        if observed is not declared:
            errors.append(
                f"route {routed[suffix].legacy_module!r}: declared shape "
                f"{declared.value!r}, checkout shows {observed.value!r}"
            )
        else:
            agreed.append(routed[suffix])

    # Only routes whose shape agreed: reading a barrel's source at a leaf's path
    # would report a missing file rather than the shape mismatch already above.
    _collect_source_state_errors(
        repo_root, agreed, resolved.routes, resolved.runtime_only_modules, errors
    )

    declared_runtime_only = set(resolved.runtime_only_suffixes)
    missing_runtime_only = sorted(set(modules.legacy_only) - declared_runtime_only)
    stale_runtime_only = sorted(declared_runtime_only - set(modules.legacy_only))
    if missing_runtime_only:
        errors.append(
            f"legacy-only modules not declared runtime-only: {missing_runtime_only}"
        )
    if stale_runtime_only:
        errors.append(
            f"runtime-only modules absent from the legacy tree: {stale_runtime_only}"
        )

    if len(modules.shared) != resolved.expected_counts.routes:
        errors.append(
            f"checkout has {len(modules.shared)} shared modules, "
            f"registry pins {resolved.expected_counts.routes}"
        )
    if len(modules.legacy_only) != resolved.expected_counts.runtime_only_modules:
        errors.append(
            f"checkout has {len(modules.legacy_only)} legacy-only modules, "
            f"registry pins {resolved.expected_counts.runtime_only_modules}"
        )

    if errors:
        raise FacadeManifestError(errors)
    return modules


__all__ = [
    "CANONICAL_PACKAGE",
    "CANONICAL_ROOT_ALL",
    "COMPATIBILITY_DEPENDENCY",
    "COUNT_KEYS",
    "FORMAT_VERSION",
    "LEGACY_PACKAGE",
    "MANIFEST_PATH",
    "REPO_ROOT",
    "ROOT_FACADE_ALL",
    "ROOT_FACADE_CANONICAL_IMPORTS",
    "ROOT_FACADE_HIDDEN_BINDINGS",
    "ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS",
    "ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS",
    "ROOT_FACADE_RUNTIME_IMPORTS",
    "ROOT_FACADE_VERSION_BINDING",
    "ROOT_FACADE_VERSION_MODULE",
    "ROUTE_KEYS",
    "SCHEMA_PATH",
    "TOP_LEVEL_KEYS",
    "CheckoutModules",
    "ExpectedCounts",
    "FacadeManifest",
    "FacadeManifestError",
    "FacadeRoute",
    "MigrationState",
    "PairKind",
    "Shape",
    "canonical_imports",
    "canonical_root_defects",
    "canonical_root_source_path",
    "direct_facade_defects",
    "discover_checkout",
    "discover_package_modules",
    "hybrid_facade_defects",
    "legacy_source_path",
    "load_manifest",
    "root_facade_defects",
    "split_facade_defects",
    "transitive_facade_defects",
    "validate_checkout",
    "validate_route_sources",
]
