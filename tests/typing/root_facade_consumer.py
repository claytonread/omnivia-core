"""Strict-mypy consumer fixture for the converted ``omnivia_memory`` package root.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for the *root* path -- the one import surface that spans every
domain at once -- and it is deliberately exhaustive: the single ``from
omnivia_memory import (...)`` block below names all 183 entries of the root's
frozen ``__all__`` plus the four non-advertised compatibility bindings, 187 in
total, and nothing else. Importing every one of them is what forces mypy to
resolve the complete root surface; the ``assert_type`` calls and typed
assignments below then prove a representative slice of each domain, all six
export collisions, and both runtime bindings keep their precise canonical types
rather than degrading to ``Any``.

There is no ``omnivia_core`` import anywhere in this file, by design: a canonical
import would prove nothing about the legacy path.

The one ``type: ignore[attr-defined]`` is on the import statement and is exactly
as narrow as it looks. ``mypy --strict`` implies ``--no-implicit-reexport``, so a
name a module imports without listing in ``__all__`` is not part of its
*advertised* typed surface -- which is precisely, and deliberately, what
``Database``, ``MemoryCreate``, ``MemoryService`` and ``MemoryUpdate`` are. The
suppression admits that rather than hiding it: mypy still resolves all four to
their real classes, which is why the ``type[...]`` assertions on them below are
meaningful. It is not a ``cast``, not ``Any``, and it does not weaken
``follow_imports``; ``--strict`` also enables ``--warn-unused-ignores``, so it
cannot go stale.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``), and its import block is audited
name-by-name against the frozen contract by
``tests/test_typed_facade_consumers.py``.
"""

# The ``omnivia_memory`` block below is in the root's own frozen ``__all__``
# order, which is part of the contract this fixture audits, so isort's
# alphabetical preference is suppressed rather than the order being changed.
from typing import assert_type  # noqa: I001

from omnivia_memory import (  # type: ignore[attr-defined]
    AgentGraphContext,
    AppManifest,
    AppManifestValidationError,
    AppState,
    AgentAction,
    AgentBackedComponentContract,
    AgentBehavior,
    AgentRunRecord,
    AgentRunStatus,
    ApprovalPolicy,
    AuditRequirement,
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    ComponentAIMode,
    ComponentConnectorScope,
    ComponentContract,
    ComponentContractValidationError,
    ComponentDataSource,
    ComponentFamily,
    ComponentGraphScope,
    ComponentInput,
    ComponentOutput,
    ComponentOutputType,
    ComponentPermission,
    ComponentRunMode,
    ComponentSafetyLevel,
    ContractVersion,
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
    DANGEROUS_SIDE_EFFECTS,
    DataSource,
    Entrypoint,
    EXTENSION_MANIFEST_CONTRACT_VERSION,
    EvidenceFileRef,
    EvidenceGraphResponse,
    GRAPH_CONTRACT_VERSION,
    GraphConfidence,
    GraphPreviewResponse,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphOrigin,
    GraphReviewStatus,
    GraphSensitivity,
    GraphSourceType,
    GraphVisibility,
    Integrity,
    CatalogueArtifactVerification,
    ImportSpecValidation,
    ImportedCandidateSet,
    KNOWLEDGE_CONTRACT_VERSION,
    KnowledgeClaim,
    KnowledgeCollection,
    KnowledgeExtensionManifest,
    KnowledgeLink,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    MemoryGraphFixture,
    ModuleKind,
    ModuleManifest,
    ModuleManifestValidationError,
    Permission,
    PermissionPolicy,
    PublishedTarget,
    ProvenanceBehavior,
    ProvenanceRequirement,
    RUN_LEDGER_CONTRACT_VERSION,
    RUN_LEDGER_PATH_ENV,
    Agent,
    Approval,
    AuditEvent,
    Automation,
    Capability,
    CapabilityType,
    Connection,
    ConnectionKind,
    ConsultantAccessGrant,
    ConsultantGrantStatus,
    ControlPlaneManifest,
    ControlPlaneRunStatus,
    ControlPlaneValidationError,
    ExecutionMode,
    ExecutionResult,
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    LocalObservabilityLogRecord,
    Policy,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
    PolicyDecision,
    PolicyDecisionReason,
    PolicyDecisionRecord,
    PolicyRulePack,
    PolicyTemplate,
    RunMode,
    RunObservabilityMetrics,
    RunRecord,
    RunStepRecord,
    RunStepStatus,
    RunStepType,
    SecretReference,
    SecretMetadata,
    SecretStorageScope,
    SideEffect,
    SyncConflictStrategy,
    SyncDirection,
    SyncRule,
    TenantIsolationRule,
    Trigger,
    TriggerEventEnvelope,
    TriggerIngestionResult,
    TriggerKind,
    WorkspaceRef,
    RetrievalTrace,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
    Source,
    SourceRef,
    SourceType,
    TERMINAL_RUN_STATUSES,
    ValidationResult,
    __version__,
    build_memory_graph_fixture,
    check_contract_version_compatibility,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_graph_node_kind,
    normalize_graph_relation,
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_object_kind,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
    detect_import_source_change,
    ImportSourceChange,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    validate_asyncapi_import_spec,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    LocalModelInvocationRecord,
    LocalUsageLedgerEntry,
    compile_policy_expression,
    manifest_from_dict,
    summarize_confidence,
    summarize_review_status,
    summarize_sensitivity,
    validate_agent_graph_context,
    validate_agent_run_record,
    validate_app_manifest,
    validate_component_contract,
    validate_control_plane_manifest,
    verify_catalogue_artifacts,
    validate_evidence_file_ref,
    validate_graph_edge,
    validate_graph_fragment,
    validate_graph_node,
    validate_module_manifest,
    validate_knowledge_claim,
    validate_knowledge_collection,
    validate_knowledge_extension_manifest,
    validate_knowledge_link,
    validate_knowledge_object,
    validate_knowledge_source,
    validate_knowledge_space,
    validate_run_ledger_entry,
    SecretResolutionResult,
    validate_run_ledger_provenance,
    validate_source_ref,
    Database,
    MemoryCreate,
    MemoryService,
    MemoryUpdate,
)

# ---------------------------------------------------------------------------
# The version binding: imported, not copied, and still a ``str``.
# ---------------------------------------------------------------------------

assert_type(__version__, str)
_VERSION: str = __version__


# ---------------------------------------------------------------------------
# The six export collisions, pinned by *type*.
#
# Each of these names is published under the same spelling by more than one
# domain, so a wrong owner would still import cleanly and still be a class. What
# it would not be is the same static type as the objects it has to compose with,
# which is what the code below checks: ``SourceRef`` has to be the knowledge
# record a ``KnowledgeObject`` holds, ``ProvenanceRequirement`` the
# component-contract dataclass with the three ``require_*`` flags rather than the
# app manifest's same-named one, ``LifecycleState`` the control-plane enum rather
# than the lifecycle domain's, ``Source``/``SourceType`` the provenance pair
# rather than ingestion's or memory's, and ``ValidationResult`` the shared record
# rather than any of the four domain-local ones.
# ---------------------------------------------------------------------------

assert_type(SourceRef, type[SourceRef])
assert_type(ProvenanceRequirement, type[ProvenanceRequirement])
assert_type(LifecycleState, type[LifecycleState])
assert_type(Source, type[Source])
assert_type(SourceType, type[SourceType])
assert_type(ValidationResult, type[ValidationResult])


def collisions() -> tuple[str, str, str, bool]:
    """Compose each colliding name with the domain that must own it."""
    reference = SourceRef(
        source_id="source-01",
        source_type=GraphSourceType.NOTE,
        path="notes/example.md",
        confidence=GraphConfidence.EXTRACTED,
    )
    note = KnowledgeObject(
        id="note-01",
        space_id="space-01",
        kind="note",
        title="Example",
        source_refs=[reference],
    )
    # The knowledge object really holds *this* SourceRef, not the memory graph's.
    assert_type(note.source_refs, list[SourceRef])
    assert_type(validate_source_ref(reference), ValidationResult)

    # The provenance pair: ``Source(type=..., reference=...)``, which is neither
    # ingestion's ``Source(path=..., file_type=...)`` nor memory's.
    provenance_source = Source(type=SourceType.ADR, reference="ADR-036")
    assert_type(provenance_source.type, SourceType)
    assert_type(provenance_source.reference, str)

    # The control-plane lifecycle enum, not the lifecycle domain's.
    lifecycle: LifecycleState = LifecycleState.ACTIVE
    # The component-contract provenance dataclass, not the app manifest's.
    requirement = ProvenanceRequirement(require_citations=False)
    assert_type(requirement.require_sources, bool)
    assert_type(requirement.require_citations, bool)
    assert_type(requirement.require_audit_log, bool)

    return (
        note.source_refs[0].source_id,
        provenance_source.reference,
        lifecycle.value,
        requirement.require_sources,
    )


# ---------------------------------------------------------------------------
# One representative slice per advertised domain.
# ---------------------------------------------------------------------------


def knowledge_domain() -> ValidationResult:
    """Knowledge contracts, normalizers and validators, plus the shared result."""
    assert_type(KNOWLEDGE_CONTRACT_VERSION, ContractVersion)
    assert_type(GRAPH_CONTRACT_VERSION, ContractVersion)
    assert_type(EXTENSION_MANIFEST_CONTRACT_VERSION, ContractVersion)
    assert_type(BUILTIN_GRAPH_NODE_KINDS, frozenset[str])
    assert_type(BUILTIN_GRAPH_RELATIONS, frozenset[str])
    assert_type(BUILTIN_OBJECT_KINDS, frozenset[str])
    assert_type(normalize_identifier("A B"), str)
    assert_type(normalize_tags(["b", "a"]), list[str])
    assert_type(normalize_label(" x "), str)
    assert_type(normalize_object_id("x"), str)
    assert_type(normalize_object_kind("note"), str)
    assert_type(normalize_source_path("a/b.md"), str)
    assert_type(normalize_space_id("s"), str)
    assert_type(normalize_graph_edge_id("e"), str)
    assert_type(normalize_graph_node_id("n"), str)
    assert_type(normalize_graph_node_kind("k"), str)
    assert_type(normalize_graph_relation("r"), str)
    assert_type(summarize_confidence([GraphConfidence.EXTRACTED]), dict[str, int])
    assert_type(summarize_review_status([GraphReviewStatus.REVIEWED]), dict[str, int])
    assert_type(summarize_sensitivity([GraphSensitivity.INTERNAL]), dict[str, int])
    assert_type(
        check_contract_version_compatibility(
            KNOWLEDGE_CONTRACT_VERSION, KNOWLEDGE_CONTRACT_VERSION
        ),
        ValidationResult,
    )

    node = GraphNode(
        id="node-01",
        space_id="space-01",
        label="Example",
        kind="concept",
        review_status=GraphReviewStatus.REVIEWED,
        visibility=GraphVisibility.PRIVATE,
        sensitivity=GraphSensitivity.INTERNAL,
        confidence=GraphConfidence.EXTRACTED,
    )
    assert_type(node.review_status, GraphReviewStatus)
    assert_type(node.sensitivity, GraphSensitivity)
    result = validate_graph_node(node)
    assert_type(result, ValidationResult)
    assert_type(result.valid, bool)
    assert_type(result.errors, list[str])

    edge = GraphEdge(
        id="edge-01",
        space_id="space-01",
        source="node-01",
        target="node-02",
        relation="relates_to",
        evidence_strength=GraphEvidenceStrength.SUPPORTING,
    )
    assert_type(edge.evidence_strength, GraphEvidenceStrength)
    assert_type(validate_graph_edge(edge), ValidationResult)
    fragment = GraphFragment(
        id="fragment-01",
        space_id="space-01",
        contract_version=GRAPH_CONTRACT_VERSION,
        nodes=[node],
        edges=[edge],
        origin=GraphOrigin.MANUAL,
    )
    assert_type(fragment.nodes, list[GraphNode])
    assert_type(fragment.edges, list[GraphEdge])
    assert_type(fragment.origin, GraphOrigin)
    assert_type(validate_graph_fragment(fragment), ValidationResult)
    return result


def knowledge_space_domain() -> KnowledgeSpace:
    """The knowledge aggregate roots, still precisely typed through the root."""
    knowledge_source = KnowledgeSource(
        id="source-01",
        space_id="space-01",
        source_type=GraphSourceType.NOTE,
        title="Example Note",
        relative_path="notes/example.md",
    )
    space = KnowledgeSpace(
        id="space-01",
        title="Example Space",
        space_type="personal vault",
        contract_version=KNOWLEDGE_CONTRACT_VERSION,
        sources=[knowledge_source],
    )
    assert_type(space.sources, list[KnowledgeSource])
    assert_type(space.objects, list[KnowledgeObject])
    assert_type(space.claims, list[KnowledgeClaim])
    assert_type(space.links, list[KnowledgeLink])
    assert_type(space.collections, list[KnowledgeCollection])
    assert_type(space.extension_manifests, list[KnowledgeExtensionManifest])
    assert_type(space.agent_contexts, list[AgentGraphContext])
    assert_type(space.contract_version, ContractVersion)
    assert_type(validate_knowledge_space(space), ValidationResult)
    assert_type(validate_knowledge_source(knowledge_source), ValidationResult)
    return space


def memory_graph_domain() -> MemoryGraphFixture:
    """The five portable memory-graph names the root advertises."""
    fixture = build_memory_graph_fixture()
    assert_type(fixture, MemoryGraphFixture)
    assert_type(MemoryGraphFixture, type[MemoryGraphFixture])
    assert_type(EvidenceGraphResponse, type[EvidenceGraphResponse])
    assert_type(GraphPreviewResponse, type[GraphPreviewResponse])
    assert_type(RetrievalTrace, type[RetrievalTrace])
    return fixture


def app_manifest_domain(payload: dict[str, object]) -> str:
    """The app manifest domain, including its own validation error type."""
    assert_type(AppManifest, type[AppManifest])
    assert_type(AppState, type[AppState])
    assert_type(DataSource, type[DataSource])
    try:
        manifest = validate_app_manifest(dict(payload))
    except AppManifestValidationError as error:
        return str(error)
    assert_type(manifest, AppManifest)
    assert_type(manifest.state, AppState)
    assert_type(manifest.required_data_sources, list[DataSource])
    return manifest.app_id


def component_contract_domain(payload: dict[str, object]) -> str:
    """The component contract domain, 25 names of which this is the spine."""
    assert_type(ComponentContract, type[ComponentContract])
    assert_type(ComponentFamily, type[ComponentFamily])
    assert_type(AgentBackedComponentContract, type[AgentBackedComponentContract])
    assert_type(ComponentAIMode, type[ComponentAIMode])
    assert_type(ComponentRunMode, type[ComponentRunMode])
    assert_type(ComponentSafetyLevel, type[ComponentSafetyLevel])
    try:
        contract = validate_component_contract(dict(payload))
    except ComponentContractValidationError as error:
        return str(error)
    assert_type(contract, ComponentContract)
    assert_type(contract.family, ComponentFamily)
    assert_type(contract.inputs, list[ComponentInput])
    assert_type(contract.outputs, list[ComponentOutput])
    assert_type(contract.permission_requirements, list[ComponentPermission])
    assert_type(contract.provenance_behavior, ProvenanceBehavior)
    return f"{contract.component_id}:{contract.family.value}"


def control_plane_domain(payload: dict[str, object]) -> ControlPlaneManifest:
    """The largest advertised block: 73 control-plane names."""
    assert_type(CONTROL_PLANE_CONTRACT_VERSION, ContractVersion)
    assert_type(CONTROL_PLANE_SCHEMA_VERSION, str)
    assert_type(DANGEROUS_SIDE_EFFECTS, frozenset[SideEffect])
    manifest = manifest_from_dict(dict(payload))
    assert_type(manifest, ControlPlaneManifest)
    assert_type(compile_policy_expression("true"), PolicyAttributeExpression)
    assert_type(validate_control_plane_manifest(manifest).valid, bool)
    assert_type(Agent, type[Agent])
    assert_type(Policy, type[Policy])
    assert_type(RunRecord, type[RunRecord])
    assert_type(WorkspaceRef, type[WorkspaceRef])
    assert_type(TriggerKind, type[TriggerKind])
    assert_type(SecretStorageScope, type[SecretStorageScope])
    assert_type(ExecutionMode, type[ExecutionMode])
    return manifest


def module_manifest_domain(payload: dict[str, object]) -> str:
    """The module manifest domain."""
    assert_type(ModuleManifest, type[ModuleManifest])
    try:
        manifest = validate_module_manifest(dict(payload))
    except ModuleManifestValidationError as error:
        return str(error)
    assert_type(manifest, ModuleManifest)
    assert_type(manifest.kind, ModuleKind)
    assert_type(manifest.entrypoint, Entrypoint)
    assert_type(manifest.integrity, Integrity)
    assert_type(manifest.permissions, list[Permission])
    assert_type(manifest.published_targets, list[PublishedTarget])
    return f"{manifest.module_id}:{manifest.kind.value}"


def run_ledger_domain() -> RunLedgerEntry:
    """The run ledger domain, including its three module-level constants."""
    assert_type(RUN_LEDGER_CONTRACT_VERSION, ContractVersion)
    assert_type(RUN_LEDGER_PATH_ENV, str)
    assert_type(TERMINAL_RUN_STATUSES, frozenset[RunLedgerStatus])
    entry = RunLedgerEntry(
        run_id="run-01",
        task_id="T-0001",
        target_repo="omnivia-core",
        lane_id="lane-01",
        status=RunLedgerStatus.SUCCEEDED,
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
        provenance=RunLedgerProvenance(producer="codex"),
    )
    assert_type(entry.status, RunLedgerStatus)
    assert_type(entry.provenance, RunLedgerProvenance)
    assert_type(entry.evidence_file_refs, list[EvidenceFileRef])
    assert_type(entry.contract_version, ContractVersion)
    assert_type(validate_run_ledger_entry(entry).valid, bool)
    assert_type(validate_run_ledger_provenance(entry.provenance).valid, bool)
    return entry


# ---------------------------------------------------------------------------
# The four non-advertised compatibility bindings.
#
# Two are canonical Core inputs; two are deliberately still owned by the legacy
# runtime. All four keep their precise classes through the root, which is the
# whole reason they stay importable at all.
# ---------------------------------------------------------------------------

assert_type(MemoryCreate, type[MemoryCreate])
assert_type(MemoryUpdate, type[MemoryUpdate])
assert_type(Database, type[Database])
assert_type(MemoryService, type[MemoryService])

_DATABASE_TYPE: type[Database] = Database
_MEMORY_SERVICE_TYPE: type[MemoryService] = MemoryService
_MEMORY_CREATE_TYPE: type[MemoryCreate] = MemoryCreate
_MEMORY_UPDATE_TYPE: type[MemoryUpdate] = MemoryUpdate


def hidden_compatibility_input() -> MemoryUpdate:
    """The canonical update input, constructed through the legacy root path.

    ``MemoryCreate`` is deliberately only asserted as a type above:
    constructing one needs ``omnivia_core.memory.models.Source``, which is *not*
    the ``Source`` this root advertises (that one is provenance's), and importing
    the memory one here would mean a canonical import this fixture must not have.
    """
    updated = MemoryUpdate(content="goodbye")
    assert_type(updated, MemoryUpdate)
    assert_type(updated.content, str | None)
    return updated


# ---------------------------------------------------------------------------
# Every imported name, referenced once.
#
# The point of this fixture is that mypy resolves the *whole* root surface, so it
# imports all 187 bindings -- and most of them are not otherwise mentioned in the
# slices above. This tuple is what makes each one a real use site rather than an
# unused import: it is annotated ``tuple[object, ...]``, not ``Any``, so nothing
# here loosens what strict mypy already proved about any individual name.
#
# ``__version__`` is exercised by its own ``assert_type`` above instead: naming a
# module dunder here would shadow this module's own.
# ---------------------------------------------------------------------------

_ROOT_SURFACE: tuple[object, ...] = (
    AgentGraphContext,
    AppManifest,
    AppManifestValidationError,
    AppState,
    AgentAction,
    AgentBackedComponentContract,
    AgentBehavior,
    AgentRunRecord,
    AgentRunStatus,
    ApprovalPolicy,
    AuditRequirement,
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    ComponentAIMode,
    ComponentConnectorScope,
    ComponentContract,
    ComponentContractValidationError,
    ComponentDataSource,
    ComponentFamily,
    ComponentGraphScope,
    ComponentInput,
    ComponentOutput,
    ComponentOutputType,
    ComponentPermission,
    ComponentRunMode,
    ComponentSafetyLevel,
    ContractVersion,
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
    DANGEROUS_SIDE_EFFECTS,
    DataSource,
    Entrypoint,
    EXTENSION_MANIFEST_CONTRACT_VERSION,
    EvidenceFileRef,
    EvidenceGraphResponse,
    GRAPH_CONTRACT_VERSION,
    GraphConfidence,
    GraphPreviewResponse,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphOrigin,
    GraphReviewStatus,
    GraphSensitivity,
    GraphSourceType,
    GraphVisibility,
    Integrity,
    CatalogueArtifactVerification,
    ImportSpecValidation,
    ImportedCandidateSet,
    KNOWLEDGE_CONTRACT_VERSION,
    KnowledgeClaim,
    KnowledgeCollection,
    KnowledgeExtensionManifest,
    KnowledgeLink,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    MemoryGraphFixture,
    ModuleKind,
    ModuleManifest,
    ModuleManifestValidationError,
    Permission,
    PermissionPolicy,
    PublishedTarget,
    ProvenanceBehavior,
    ProvenanceRequirement,
    RUN_LEDGER_CONTRACT_VERSION,
    RUN_LEDGER_PATH_ENV,
    Agent,
    Approval,
    AuditEvent,
    Automation,
    Capability,
    CapabilityType,
    Connection,
    ConnectionKind,
    ConsultantAccessGrant,
    ConsultantGrantStatus,
    ControlPlaneManifest,
    ControlPlaneRunStatus,
    ControlPlaneValidationError,
    ExecutionMode,
    ExecutionResult,
    ImportRecord,
    ImportSourceProtocol,
    LifecycleState,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    LocalObservabilityLogRecord,
    Policy,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
    PolicyDecision,
    PolicyDecisionReason,
    PolicyDecisionRecord,
    PolicyRulePack,
    PolicyTemplate,
    RunMode,
    RunObservabilityMetrics,
    RunRecord,
    RunStepRecord,
    RunStepStatus,
    RunStepType,
    SecretReference,
    SecretMetadata,
    SecretStorageScope,
    SideEffect,
    SyncConflictStrategy,
    SyncDirection,
    SyncRule,
    TenantIsolationRule,
    Trigger,
    TriggerEventEnvelope,
    TriggerIngestionResult,
    TriggerKind,
    WorkspaceRef,
    RetrievalTrace,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
    Source,
    SourceRef,
    SourceType,
    TERMINAL_RUN_STATUSES,
    ValidationResult,
    build_memory_graph_fixture,
    check_contract_version_compatibility,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_graph_node_kind,
    normalize_graph_relation,
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_object_kind,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
    detect_import_source_change,
    ImportSourceChange,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    validate_asyncapi_import_spec,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    LocalModelInvocationRecord,
    LocalUsageLedgerEntry,
    compile_policy_expression,
    manifest_from_dict,
    summarize_confidence,
    summarize_review_status,
    summarize_sensitivity,
    validate_agent_graph_context,
    validate_agent_run_record,
    validate_app_manifest,
    validate_component_contract,
    validate_control_plane_manifest,
    verify_catalogue_artifacts,
    validate_evidence_file_ref,
    validate_graph_edge,
    validate_graph_fragment,
    validate_graph_node,
    validate_module_manifest,
    validate_knowledge_claim,
    validate_knowledge_collection,
    validate_knowledge_extension_manifest,
    validate_knowledge_link,
    validate_knowledge_object,
    validate_knowledge_source,
    validate_knowledge_space,
    validate_run_ledger_entry,
    SecretResolutionResult,
    validate_run_ledger_provenance,
    validate_source_ref,
    Database,
    MemoryCreate,
    MemoryService,
    MemoryUpdate,
)
