"""Strict-mypy consumer fixture for every accepted legacy facade leaf.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for every accepted facade leaf except the two Module Manifest
leaves, which keep their own fixture
(``tests/typing/module_manifest_facade_consumer.py``).
``tests/test_typed_facade_consumers.py`` enforces that split and that the two
fixtures together cover ``baseline.inventory.FACADE_ROUTES`` exactly.

It imports every domain/API symbol those leaves route -- exactly the
``baseline.inventory.FACADE_ROUTES`` entry for each -- and only ever through the
``omnivia_memory.*`` path, never through ``omnivia_core.*``. Incidental bindings
(``Enum``, ``List``, ``dataclass``, ``annotations``, ...) are not a downstream
surface and are covered by direct strict checking of the wrappers themselves.

Names that legitimately collide across leaves (``ValidationResult``,
``ProvenanceRequirement``, ``LifecycleState``, ``CreatedBy``, ``Source``,
``MemorySource``) are imported under distinct aliases so each leaf's own
historically-owned object is exercised separately -- aliasing the binding here
does not weaken the check, because implicit re-export is decided by the *source*
module either way. ``MemorySource`` is the sharpest of those: this module already
binds that name for the provenance ``Source`` the memory contracts re-export, so
the memory graph's own ingested-source contract is imported as
``MemoryGraphSource``. The memory graph's ``SourceRef`` collides with the
knowledge domain's same-named class too, but that one lives in the dedicated
knowledge fixture, so no alias is needed for it here.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If a facade ever stopped explicitly
re-exporting these names, or degraded them to ``Any``, strict mypy would fail
here.
"""

from typing import Any, assert_type

from omnivia_memory._shared.validation import (
    SENSITIVE_KEYS,
    scan_sensitive_fields,
    validate_iso_timestamp,
    validate_optional_iso_timestamp,
)
from omnivia_memory._shared.validation import (
    ValidationResult as SharedValidationResult,
)
from omnivia_memory.app_manifest.models import (
    AppManifest,
    AppState,
    DataSource,
)
from omnivia_memory.app_manifest.models import (
    ProvenanceRequirement as AppProvenanceRequirement,
)
from omnivia_memory.app_manifest.models import (
    ValidationResult as AppManifestValidationResult,
)
from omnivia_memory.app_manifest.validation import (
    AppManifestValidationError,
    validate_app_manifest,
)
from omnivia_memory.app_shell_bridge.models import (
    AppShellBodyDescriptor,
    AppShellHostContext,
    AppShellRuntimeState,
    AppShellSource,
)
from omnivia_memory.app_shell_bridge.models import (
    ValidationResult as AppShellValidationResult,
)
from omnivia_memory.app_shell_bridge.validation import (
    AppShellBridgeValidationError,
    validate_app_shell_body_descriptor,
    validate_app_shell_host_context,
)
from omnivia_memory.component_contract.models import (
    AgentAction,
    AgentBackedComponentContract,
    AgentBehavior,
    AgentRunRecord,
    AgentRunStatus,
    ApprovalPolicy,
    AuditRequirement,
    ComponentAIMode,
    ComponentConnectorScope,
    ComponentContract,
    ComponentDataSource,
    ComponentFamily,
    ComponentGraphScope,
    ComponentInput,
    ComponentOutput,
    ComponentOutputType,
    ComponentPermission,
    ComponentRunMode,
    ComponentSafetyLevel,
    PermissionPolicy,
    ProvenanceBehavior,
)
from omnivia_memory.component_contract.models import (
    ProvenanceRequirement as ComponentProvenanceRequirement,
)
from omnivia_memory.component_contract.models import (
    ValidationResult as ComponentValidationResult,
)
from omnivia_memory.component_contract.validation import (
    ComponentContractValidationError,
    validate_agent_run_record,
    validate_component_contract,
)
from omnivia_memory.control_plane.imports import (
    CatalogueArtifactVerification,
    ImportedCandidateSet,
    ImportSourceChange,
    ImportSpecValidation,
    detect_import_source_change,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    validate_asyncapi_import_spec,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    verify_catalogue_artifacts,
)
from omnivia_memory.control_plane.models import (
    CONTROL_PLANE_CONTRACT_VERSION,
    CONTROL_PLANE_SCHEMA_VERSION,
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
    ExecutionMode,
    ExecutionResult,
    ImportRecord,
    ImportSourceProtocol,
    LocalApprovalNotification,
    LocalApprovalNotificationChannel,
    LocalApprovalNotificationEvent,
    LocalApprovalNotificationStatus,
    LocalModelInvocationRecord,
    LocalObservabilityLogRecord,
    LocalUsageLedgerEntry,
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
    SecretMetadata,
    SecretReference,
    SecretResolutionResult,
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
)
from omnivia_memory.control_plane.models import (
    LifecycleState as ControlPlaneLifecycleState,
)
from omnivia_memory.control_plane.models import (
    ValidationResult as ControlPlaneValidationResult,
)
from omnivia_memory.control_plane.validation import (
    APPROVAL_ESCALATION_STATES,
    DANGEROUS_SIDE_EFFECTS,
    EXTRA_SENSITIVE_KEYS,
    POLICY_ATTRIBUTE_NUMERIC_OPERATORS,
    POLICY_ATTRIBUTE_OPERATORS,
    POLICY_ATTRIBUTE_PRESENCE_OPERATORS,
    POLICY_ATTRIBUTE_SCOPES,
    POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS,
    POLICY_ATTRIBUTE_VALUE_OPERATORS,
    POLICY_ATTRIBUTE_VALUES_OPERATORS,
    POLICY_EXPRESSION_BOOLEAN_OPS,
    POLICY_EXPRESSION_COMPARISON_OPERATORS,
    POLICY_EXPRESSION_MAX_DEPTH,
    POLICY_EXPRESSION_MAX_NODES,
    POLICY_EXPRESSION_NUMERIC_COMPARISONS,
    POLICY_EXPRESSION_OPS,
    POLICY_EXPRESSION_PARSE_MAX_DEPTH,
    POLICY_EXPRESSION_RAW_FIELDS,
    POLICY_EXPRESSION_SECRET_MARKERS,
    POLICY_EXPRESSION_SOURCE_MAX_LENGTH,
    RRULE_FREQUENCIES,
    RRULE_WEEKDAYS,
    STABLE_ID_PATTERN,
    ControlPlaneValidationError,
    T,
    compile_policy_expression,
    manifest_from_dict,
    validate_control_plane_manifest,
)
from omnivia_memory.lifecycle.models import LifecycleState
from omnivia_memory.lifecycle.rules import CreatedBy, LifecycleRules
from omnivia_memory.lifecycle.rules import LifecycleState as RulesLifecycleState
from omnivia_memory.memory.models import CreatedBy as MemoryCreatedBy
from omnivia_memory.memory.models import LifecycleState as MemoryLifecycleState
from omnivia_memory.memory.models import (
    Memory,
    MemoryCreate,
    MemoryUpdate,
)
from omnivia_memory.memory.models import Source as MemorySource
from omnivia_memory.memory_graph.assembly import (
    assemble_evidence_graph,
    assemble_graph_preview,
    redact_segment_preview,
)
from omnivia_memory.memory_graph.fixtures import (
    FIXTURE_TIME,
    MemoryGraphFixture,
    build_memory_graph_fixture,
)
from omnivia_memory.memory_graph.models import (
    Confidence,
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewKind,
    GraphPreviewNode,
    GraphPreviewResponse,
    GraphPreviewState,
    MemoryEntity,
    MemoryFact,
    MemoryFactStatus,
    MemorySegment,
    MemorySegmentKind,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
    RetrievalTrace,
    SourceRef,
)
from omnivia_memory.memory_graph.models import MemorySource as MemoryGraphSource
from omnivia_memory.memory_graph.validation import (
    CONFIDENCE_BUCKETS,
    validate_evidence_graph_response,
    validate_graph_preview_response,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
)
from omnivia_memory.provenance.models import Source, SourceType
from omnivia_memory.run_ledger.models import (
    RUN_LEDGER_CONTRACT_VERSION,
    RUN_LEDGER_PATH_ENV,
    EvidenceFileRef,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
)
from omnivia_memory.run_ledger.validation import (
    TERMINAL_RUN_STATUSES,
    validate_evidence_file_ref,
    validate_run_ledger_entry,
    validate_run_ledger_provenance,
)

# ---------------------------------------------------------------------------
# omnivia_memory._shared.validation
# ---------------------------------------------------------------------------


def shared_validation() -> SharedValidationResult:
    """The shared primitives keep their precise signatures and result type."""
    assert_type(SENSITIVE_KEYS, frozenset[str])

    errors: list[str] = []
    validate_iso_timestamp("created_at", "2026-07-30T00:00:00+00:00", errors)
    validate_optional_iso_timestamp("updated_at", None, errors)
    scan_sensitive_fields({"api_key": "x"}, errors, prefix="payload")

    # Callable assignment: a degraded `Any` would not be checked against this.
    check: type[SharedValidationResult] = SharedValidationResult
    result = check(valid=not errors, errors=errors)
    assert_type(result.valid, bool)
    assert_type(result.errors, list[str])
    assert_type(result.warnings, list[str])
    return result


# ---------------------------------------------------------------------------
# omnivia_memory.app_manifest.{models,validation}
# ---------------------------------------------------------------------------


def build_app_manifest() -> AppManifest:
    """Typed construction through the leaf's own classes."""
    return AppManifest(
        manifest_version="1.0.0",
        app_id="com.omnivia.apps.crm",
        display_name="CRM",
        app_version="1.0.0",
        required_module_id="com.omnivia.apps",
        required_data_sources=[DataSource(source_id="graph")],
        provenance=AppProvenanceRequirement(require_signed=True),
        state=AppState.ACTIVE,
        validation=AppManifestValidationResult(is_valid=True),
    )


def consume_app_manifest(payload: dict[str, object]) -> str:
    """The validator's return type composes without a cast or an ignore."""
    try:
        manifest = validate_app_manifest(payload)
    except AppManifestValidationError as error:
        return str(error)

    assert_type(manifest, AppManifest)
    assert_type(manifest.app_id, str)
    assert_type(manifest.state, AppState)
    assert_type(manifest.provenance, AppProvenanceRequirement)
    assert_type(manifest.validation, AppManifestValidationResult)
    assert_type(manifest.required_data_sources, list[DataSource])

    sources: list[str] = [source.source_id for source in manifest.required_data_sources]
    return f"{manifest.app_id}:{manifest.state.value}:" + ",".join(sources)


# ---------------------------------------------------------------------------
# omnivia_memory.app_shell_bridge.{models,validation}
# ---------------------------------------------------------------------------


def build_app_shell_host_context() -> AppShellHostContext:
    return AppShellHostContext(
        app_id="com.omnivia.apps.crm",
        app_name="CRM",
        entity_label="Account",
        runtime_state=AppShellRuntimeState.READY,
        sources=[AppShellSource(name="graph")],
        validation=AppShellValidationResult(is_valid=True),
    )


def build_app_shell_body_descriptor() -> AppShellBodyDescriptor:
    return AppShellBodyDescriptor(
        app_id="com.omnivia.apps.crm",
        body_id="overview",
        sources=[AppShellSource(name="graph", description="assembled graph")],
        validation=AppShellValidationResult(is_valid=True),
    )


def consume_app_shell(
    context_payload: dict[str, object],
    body_payload: dict[str, object],
) -> str:
    try:
        context = validate_app_shell_host_context(context_payload)
        body = validate_app_shell_body_descriptor(body_payload)
    except AppShellBridgeValidationError as error:
        return str(error)

    assert_type(context, AppShellHostContext)
    assert_type(context.runtime_state, AppShellRuntimeState)
    assert_type(context.sources, list[AppShellSource])
    assert_type(context.validation, AppShellValidationResult)
    assert_type(body, AppShellBodyDescriptor)
    assert_type(body.source_count, int)
    assert_type(body.degraded_component_ids, list[str])

    names: list[str] = [source.name for source in context.sources]
    return f"{context.runtime_state.value}:{body.body_id}:" + ",".join(names)


# ---------------------------------------------------------------------------
# omnivia_memory.component_contract.{models,validation}
# ---------------------------------------------------------------------------


def build_agent_backed_contract() -> AgentBackedComponentContract:
    return AgentBackedComponentContract(
        app_compatibility=["com.omnivia.apps.crm"],
        data_sources=[ComponentDataSource(source_id="graph")],
        ai_mode=ComponentAIMode.LOCAL_FIRST,
        run_mode=ComponentRunMode.APPROVAL_GATED,
        agent_behavior=AgentBehavior(
            objective="summarise",
            allowed_actions=[AgentAction.READ, AgentAction.SUGGEST],
            max_safety_level=ComponentSafetyLevel.LEVEL_2,
        ),
        output_type=ComponentOutputType.BRIEF,
        safety_level=ComponentSafetyLevel.LEVEL_2,
        graph_scope=ComponentGraphScope(node_types=["Memory"]),
        connector_scope=ComponentConnectorScope(connector_ids=["local"]),
        permission_policy=PermissionPolicy(required_permissions=["graph.read"]),
        approval_policy=ApprovalPolicy(human_required=True, approval_reason="write"),
        provenance_requirements=ComponentProvenanceRequirement(require_sources=True),
        audit_requirements=AuditRequirement(event_types=["agent.run"]),
    )


def build_component_contract() -> ComponentContract:
    return ComponentContract(
        contract_version="1.0.0",
        component_id="com.omnivia.components.brief",
        display_name="Brief",
        family=ComponentFamily.LOGIC,
        version="1.0.0",
        inputs=[ComponentInput(name="entity_id")],
        outputs=[ComponentOutput(name="brief")],
        permission_requirements=[ComponentPermission(name="graph.read")],
        provenance_behavior=ProvenanceBehavior.TRACK,
        validation=ComponentValidationResult(is_valid=True),
        agent_backed=build_agent_backed_contract(),
    )


def consume_component_contract(payload: dict[str, object]) -> str:
    try:
        contract = validate_component_contract(payload)
    except ComponentContractValidationError as error:
        return str(error)

    assert_type(contract, ComponentContract)
    assert_type(contract.family, ComponentFamily)
    assert_type(contract.provenance_behavior, ProvenanceBehavior)
    assert_type(contract.validation, ComponentValidationResult)
    assert_type(contract.inputs, list[ComponentInput])
    assert_type(contract.outputs, list[ComponentOutput])
    assert_type(contract.permission_requirements, list[ComponentPermission])
    assert_type(contract.agent_backed, AgentBackedComponentContract | None)

    agent = contract.agent_backed
    if agent is None:
        return contract.component_id

    assert_type(agent.ai_mode, ComponentAIMode)
    assert_type(agent.run_mode, ComponentRunMode)
    assert_type(agent.agent_behavior, AgentBehavior)
    assert_type(agent.agent_behavior.allowed_actions, list[AgentAction])
    assert_type(agent.output_type, ComponentOutputType)
    assert_type(agent.safety_level, ComponentSafetyLevel)
    assert_type(agent.graph_scope, ComponentGraphScope)
    assert_type(agent.connector_scope, ComponentConnectorScope)
    assert_type(agent.permission_policy, PermissionPolicy)
    assert_type(agent.approval_policy, ApprovalPolicy)
    assert_type(agent.provenance_requirements, ComponentProvenanceRequirement)
    assert_type(agent.audit_requirements, AuditRequirement)
    assert_type(agent.data_sources, list[ComponentDataSource])

    actions: list[str] = [
        action.value for action in agent.agent_behavior.allowed_actions
    ]
    return f"{contract.component_id}:{contract.family.value}:" + ",".join(actions)


def consume_agent_run_record(payload: dict[str, object]) -> AgentRunStatus:
    try:
        record = validate_agent_run_record(payload)
    except ComponentContractValidationError:
        return AgentRunStatus.FAILED

    assert_type(record, AgentRunRecord)
    assert_type(record.run_id, str)
    assert_type(record.status, AgentRunStatus)
    assert_type(record.approval_required, bool)
    assert_type(record.audit_event_ids, list[str])
    return record.status


# ---------------------------------------------------------------------------
# omnivia_memory.lifecycle.{models,rules}
# ---------------------------------------------------------------------------


def lifecycle_transitions() -> LifecycleState:
    """The two lifecycle leaves keep one usefully typed ``LifecycleState``."""
    # `lifecycle.rules` re-exports the same class its sibling `models` owns, so
    # the two bindings must stay mutually assignable, not merely both `Any`.
    state: LifecycleState = RulesLifecycleState.PROPOSED
    assert_type(state, LifecycleState)
    assert_type(state.value, str)

    initial = LifecycleRules.get_initial_state(CreatedBy.HUMAN)
    assert_type(initial, LifecycleState)
    # A typed assignment rather than `assert_type`: the enum member's `.value` is
    # a `Literal[...]`, but it must still be a `str` and not `Any`.
    created_by_value: str = CreatedBy.AGENT.value

    allowed: bool = LifecycleRules.can_transition(
        initial, LifecycleState.APPROVED
    ) and bool(created_by_value)
    return initial if allowed else state


# ---------------------------------------------------------------------------
# omnivia_memory.{memory,provenance}.models
# ---------------------------------------------------------------------------


def build_memory() -> Memory:
    """``memory.models`` re-exports the lifecycle/provenance types it uses."""
    source: MemorySource = Source(type=SourceType.ADR, reference="docs/adr/0001.md")
    assert_type(source, Source)
    assert_type(source.type, SourceType)
    assert_type(source.reference, str)
    assert_type(source.description, str | None)

    created_by: MemoryCreatedBy = CreatedBy.HUMAN
    lifecycle: MemoryLifecycleState = LifecycleState.APPROVED

    memory = Memory(
        content="Core owns the public contracts.",
        source=source,
        created_by=created_by,
        lifecycle_state=lifecycle,
    )
    assert_type(memory.id, str)
    assert_type(memory.source, Source)
    assert_type(memory.created_by, CreatedBy)
    assert_type(memory.lifecycle_state, LifecycleState)
    assert_type(memory.workspace_id, str | None)
    assert_type(memory.created_at, str)
    return memory


def memory_write_shapes() -> tuple[MemoryCreate, MemoryUpdate]:
    create = MemoryCreate(
        content="Apps install as a Module.",
        source=Source(type=SourceType.HUMAN, reference="workshop"),
        created_by=CreatedBy.AGENT,
    )
    update = MemoryUpdate(
        content="Apps ship as a Module.", lifecycle_state=LifecycleState.OBSERVED
    )
    assert_type(create.source, Source)
    assert_type(create.created_by, CreatedBy)
    assert_type(update.lifecycle_state, LifecycleState | None)
    return create, update


# ---------------------------------------------------------------------------
# omnivia_memory.memory_graph.{models,validation,assembly,fixtures}
# ---------------------------------------------------------------------------


def build_memory_graph_records() -> tuple[
    MemoryGraphSource, MemorySegment, MemoryEntity, MemoryFact
]:
    """Typed construction across the four durable memory graph records.

    ``Confidence`` is the routed public type alias (``float | str``), so the
    entity's score and the fact's bucket string must both be accepted by the same
    declared field type -- a degraded ``Any`` would accept them without proving
    anything.
    """
    source = MemoryGraphSource(
        id="source-001",
        workspace_id="workspace-001",
        type=MemorySourceType.FILE,
        uri="docs/adr/001-memory.md",
        title="Memory ADR",
        freshness=MemorySourceFreshness.FRESH,
        status=MemorySourceStatus.READY,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
        checksum="sha256:source",
    )
    segment = MemorySegment(
        id="segment-001",
        source_id=source.id,
        workspace_id=source.workspace_id,
        kind=MemorySegmentKind.TEXT,
        label="Decision",
        parser="markdown",
        parser_settings_ref="parsers/markdown@1",
        created_at=FIXTURE_TIME,
        span={"start": 0, "end": 42},
        text_preview="Core owns the public contracts.",
    )
    reference = SourceRef(
        source_id=source.id,
        segment_id=segment.id,
        span=segment.span,
        quote_preview="Core owns the public contracts.",
        confidence="extracted",
    )
    score: Confidence = 0.94
    bucket: Confidence = "inferred"
    entity = MemoryEntity(
        id="entity-001",
        workspace_id=source.workspace_id,
        type="Decision",
        canonical_name="Core owns the public contracts",
        aliases=["contract ownership"],
        confidence=score,
        source_refs=[reference],
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    fact = MemoryFact(
        id="fact-001",
        workspace_id=source.workspace_id,
        subject_id=entity.id,
        predicate="documented_by",
        confidence=bucket,
        source_refs=[reference],
        status=MemoryFactStatus.APPROVED,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
        object_value="ADR-001",
        valid_from=FIXTURE_TIME,
    )

    assert_type(FIXTURE_TIME, str)
    assert_type(source.type, MemorySourceType)
    assert_type(source.freshness, MemorySourceFreshness)
    assert_type(source.status, MemorySourceStatus)
    assert_type(source.owner_ref, str | None)
    assert_type(segment.kind, MemorySegmentKind)
    assert_type(segment.span, dict[str, Any] | None)
    assert_type(segment.text_preview, str | None)
    assert_type(reference.confidence, Confidence | None)
    assert_type(reference.segment_id, str | None)
    assert_type(entity.aliases, list[str])
    assert_type(entity.confidence, Confidence)
    assert_type(entity.source_refs, list[SourceRef])
    assert_type(fact.status, MemoryFactStatus)
    assert_type(fact.object_value, str | int | float | bool | None)
    assert_type(fact.valid_to, str | None)
    assert_type(fact.source_refs, list[SourceRef])
    return source, segment, entity, fact


def consume_memory_graph_fixture() -> MemoryGraphFixture:
    """The routed ``TypedDict`` must keep its precise per-key value types.

    A degraded ``Any`` bundle would let every subscript below through unchecked,
    so each one is pinned to the record type its key declares.
    """
    fixture = build_memory_graph_fixture()

    assert_type(fixture, MemoryGraphFixture)
    assert_type(fixture["source"], MemoryGraphSource)
    assert_type(fixture["segments"], list[MemorySegment])
    assert_type(fixture["entities"], list[MemoryEntity])
    assert_type(fixture["facts"], list[MemoryFact])
    assert_type(fixture["graph_preview"], GraphPreviewResponse)
    assert_type(fixture["evidence_graph"], EvidenceGraphResponse)

    preview = fixture["graph_preview"]
    assert_type(preview.nodes, list[GraphPreviewNode])
    assert_type(preview.edges, list[GraphPreviewEdge])
    assert_type(preview.limits, dict[str, int])
    assert_type(preview.warnings, list[str])
    assert_type(fixture["evidence_graph"].citations, list[SourceRef])
    assert_type(fixture["evidence_graph"].answer_id, str | None)
    return fixture


def assemble_memory_graph_displays() -> tuple[
    GraphPreviewResponse, EvidenceGraphResponse, dict[str, object]
]:
    """All three assembly helpers, composed from the records built above."""
    source, segment, entity, fact = build_memory_graph_records()

    preview = assemble_graph_preview(
        workspace_id=source.workspace_id,
        query="memory graph",
        source=source,
        entities=[entity],
        facts=[fact],
        generated_at=FIXTURE_TIME,
        node_limit=100,
        edge_limit=150,
    )
    evidence = assemble_evidence_graph(
        workspace_id=source.workspace_id,
        answer_id="answer-001",
        query="memory graph",
        source=source,
        entities=[entity],
        facts=[fact],
        generated_at=FIXTURE_TIME,
    )
    redacted = redact_segment_preview(segment, max_chars=5)

    assert_type(preview, GraphPreviewResponse)
    assert_type(evidence, EvidenceGraphResponse)
    assert_type(redacted, dict[str, object])
    for node in preview.nodes:
        assert_type(node.kind, GraphPreviewKind)
        assert_type(node.state, GraphPreviewState)
        assert_type(node.confidence, Confidence | None)
        assert_type(node.display, dict[str, Any])
    for edge in preview.edges:
        assert_type(edge.state, GraphPreviewState)
        assert_type(edge.valid_from, str | None)
        assert_type(edge.source_refs, list[SourceRef])
    # ``source=None`` is part of both helpers' declared signature, so passing it
    # must stay a type-checked call rather than an ``Any`` pass-through.
    assert_type(
        assemble_graph_preview(
            workspace_id=source.workspace_id,
            query="empty",
            source=None,
            entities=[],
            facts=[],
            generated_at=FIXTURE_TIME,
            node_limit=1,
            edge_limit=1,
        ),
        GraphPreviewResponse,
    )
    return preview, evidence, redacted


def consume_memory_graph_validators() -> SharedValidationResult:
    """All six validators return the *shared* ``ValidationResult`` primitive.

    Not one of the four domain classes of the same name, so pinning it here is
    what would catch this leaf being rerouted to a same-named lookalike. The
    bindings this fixture reuses are the shared leaf's own routed ones, so no
    unrouted incidental name is imported for it.
    """
    source, segment, entity, fact = build_memory_graph_records()
    preview, evidence, _redacted = assemble_memory_graph_displays()

    assert_type(CONFIDENCE_BUCKETS, frozenset[str])
    bucketed: bool = "extracted" in CONFIDENCE_BUCKETS

    results = (
        validate_memory_source(source),
        validate_memory_segment(segment),
        validate_memory_entity(entity),
        validate_memory_fact(fact),
        validate_graph_preview_response(preview),
        validate_evidence_graph_response(evidence),
    )
    for result in results:
        assert_type(result, SharedValidationResult)
        assert_type(result.valid, bool)
        assert_type(result.errors, list[str])
        assert_type(result.warnings, list[str])

    payload = preview.to_dict()
    assert_type(payload, dict[str, Any])
    assert_type(GraphPreviewResponse.from_dict(payload), GraphPreviewResponse)
    assert_type(
        EvidenceGraphResponse.from_dict(evidence.to_dict()), EvidenceGraphResponse
    )
    assert_type(MemoryFact.from_dict(fact.to_dict()), MemoryFact)
    assert_type(SourceRef.from_dict(entity.source_refs[0].to_dict()), SourceRef)

    trace = RetrievalTrace(
        id="trace-001",
        workspace_id=source.workspace_id,
        query="memory graph",
        mode="hybrid",
        matched_fact_ids=[fact.id],
        matched_segment_ids=[segment.id],
        rank_scores={fact.id: 0.98},
        timing={"search_ms": 12},
        resource_indicators={"segments_examined": 1},
        warnings=[],
        generated_at=FIXTURE_TIME,
    )
    assert_type(trace.rank_scores, dict[str, float])
    assert_type(trace.timing, dict[str, int | float])
    assert_type(trace.resource_indicators, dict[str, Any])
    assert_type(RetrievalTrace.from_dict(trace.to_dict()), RetrievalTrace)

    if bucketed and not results[0].valid:
        return results[-1]
    return results[0]


# ---------------------------------------------------------------------------
# omnivia_memory.control_plane.{imports,models,validation}
# ---------------------------------------------------------------------------


def first_or_none(items: list[T]) -> T | None:
    """The routed ``TypeVar`` must still be a type variable, not ``Any``.

    ``T`` is a frozen definition of the control-plane validation leaf that the
    barrel above it never re-exported, so it is routed and checked here. A
    degraded ``Any`` would make the call below return ``Any`` and the
    ``assert_type`` fail.
    """
    return items[0] if items else None


def build_control_plane_registry() -> tuple[
    Connection, Capability, Agent, Trigger, Automation
]:
    """Typed construction across the control plane's core registry contracts."""
    connection = Connection(
        id="conn.local",
        kind=ConnectionKind.INTERNAL,
        display_name="Local",
        lifecycle=ControlPlaneLifecycleState.CANDIDATE,
        secret_refs=[SecretReference(secret_ref="vault://local", provider="local")],
    )
    capability = Capability(
        id="cap.read",
        capability_type=CapabilityType.QUERY,
        connection_id=connection.id,
        side_effect=SideEffect.READ,
        lifecycle=ControlPlaneLifecycleState.CANDIDATE,
    )
    agent = Agent(
        id="agent.brief",
        allowed_capabilities=[capability.id],
        run_mode=RunMode.MANUAL,
    )
    trigger = Trigger(
        id="trigger.manual",
        kind=TriggerKind.MANUAL,
        capability_id=capability.id,
    )
    automation = Automation(
        id="auto.brief",
        agent_id=agent.id,
        capability_id=capability.id,
        trigger_id=trigger.id,
        run_mode=RunMode.APPROVAL_GATED,
    )

    assert_type(connection.kind, ConnectionKind)
    assert_type(connection.lifecycle, ControlPlaneLifecycleState)
    assert_type(connection.secret_refs, list[SecretReference])
    assert_type(capability.capability_type, CapabilityType)
    assert_type(capability.side_effect, SideEffect)
    # ``Any`` is the *declared* value type of the frozen JSON-schema fields, not
    # a degraded annotation: asserting the exact `dict[str, Any]` container still
    # rejects a bare `Any` attribute, which is what a broken re-export produces.
    assert_type(capability.input_schema, dict[str, Any])
    assert_type(agent.allowed_capabilities, list[str])
    assert_type(agent.run_mode, RunMode)
    assert_type(trigger.kind, TriggerKind)
    assert_type(trigger.schedule_rrule, str | None)
    assert_type(automation.policy_ids, list[str])
    assert_type(automation.max_steps, int | None)
    # The control plane's own registry lifecycle enum, not the lifecycle domain's
    # state machine: the two must stay separately typed, so a cross-assignment
    # here is a type error rather than an `Any` pass-through.
    assert_type(first_or_none([connection.lifecycle]), ControlPlaneLifecycleState | None)
    return connection, capability, agent, trigger, automation


def build_control_plane_policies() -> tuple[Policy, PolicyTemplate, PolicyRulePack]:
    """The bounded policy contracts, including the structured expression tree."""
    condition = PolicyAttributeCondition(
        scope="actor",
        key="role",
        operator="equals",
        value="reviewer",
    )
    expression = PolicyAttributeExpression(
        op="all",
        children=[PolicyAttributeExpression(op="condition", condition=condition)],
    )
    policy = Policy(
        id="policy.review",
        decision=PolicyDecision.REQUIRE_APPROVAL,
        capability_ids=["cap.read"],
        attribute_conditions=[condition],
        attribute_expression=expression,
        required_actor_attributes={"role": "reviewer"},
    )
    template = PolicyTemplate(
        id="policy.template.review",
        decision=PolicyDecision.REQUIRE_APPROVAL,
        attribute_conditions=[condition],
        attribute_expression=expression,
        lifecycle=ControlPlaneLifecycleState.CANDIDATE,
    )
    rule_pack = PolicyRulePack(
        id="policy.pack.review",
        template_ids=[template.id],
        lifecycle=ControlPlaneLifecycleState.CANDIDATE,
    )

    assert_type(condition.scope, str)
    assert_type(condition.values, list[str])
    assert_type(expression.children, list[PolicyAttributeExpression])
    assert_type(expression.condition, PolicyAttributeCondition | None)
    assert_type(policy.decision, PolicyDecision)
    assert_type(policy.attribute_conditions, list[PolicyAttributeCondition])
    assert_type(policy.attribute_expression, PolicyAttributeExpression | None)
    assert_type(policy.required_workspace_attributes, dict[str, str])
    assert_type(policy.compliance_blocked, bool)
    assert_type(template.lifecycle, ControlPlaneLifecycleState)
    assert_type(rule_pack.template_ids, list[str])
    return policy, template, rule_pack


def build_control_plane_evidence() -> ExecutionResult:
    """Run/step/observability evidence, all display-safe records."""
    run_record = RunRecord(
        id="cp-run-001",
        automation_id="auto.brief",
        status=ControlPlaneRunStatus.WAITING_FOR_APPROVAL,
        run_ledger_ref="runs/run-001.json",
    )
    step = RunStepRecord(
        id="cp-step-001",
        run_id=run_record.id,
        step_type=RunStepType.APPROVAL_WAIT,
        status=RunStepStatus.WAITING_FOR_APPROVAL,
    )
    decision = PolicyDecisionRecord(
        id="cp-decision-001",
        workspace_id="ws.local",
        decision=PolicyDecision.REQUIRE_APPROVAL,
        reason_code=PolicyDecisionReason.REQUIRE_APPROVAL_POLICY,
        capability_id="cap.read",
    )
    invocation = LocalModelInvocationRecord(
        id="cp-model-001",
        run_id=run_record.id,
        step_id=step.id,
        agent_id="agent.brief",
        trace_id="trace-1",
        span_id="span-1",
    )
    result = ExecutionResult(
        run_record=run_record,
        steps=[step],
        policy_decision=decision,
        model_invocation=invocation,
        paused=True,
    )

    assert_type(run_record.status, ControlPlaneRunStatus)
    assert_type(run_record.retry_count, int)
    assert_type(step.step_type, RunStepType)
    assert_type(step.status, RunStepStatus)
    assert_type(decision.reason_code, PolicyDecisionReason)
    assert_type(decision.policy_ids, list[str])
    assert_type(invocation.prompt_redacted, bool)
    assert_type(result.steps, list[RunStepRecord])
    assert_type(result.policy_decision, PolicyDecisionRecord | None)
    assert_type(result.model_invocation, LocalModelInvocationRecord | None)
    assert_type(result.paused, bool)
    return result


def build_control_plane_governance() -> ControlPlaneManifest:
    """The remaining registry contracts, assembled into a portable manifest."""
    connection, capability, agent, trigger, automation = (
        build_control_plane_registry()
    )
    policy, template, rule_pack = build_control_plane_policies()

    approval = Approval(
        id="approval.review",
        capability_ids=[capability.id],
        approver_role="reviewer",
        escalation_state="pending",
    )
    audit_event = AuditEvent(id="audit.1", event_type="capability.invoked")
    import_record = ImportRecord(
        id="import.mcp.local",
        source_protocol=ImportSourceProtocol.MCP,
        source_ref="mcp://local",
        candidate_capability_ids=[capability.id],
    )
    secret_metadata = SecretMetadata(
        id="secret.local",
        secret_ref="vault://local",
        owner_workspace_id="ws.local",
        storage_scope=SecretStorageScope.LOCAL_ONLY,
    )
    sync_rule = SyncRule(
        id="sync.capability",
        resource_type="capability",
        direction=SyncDirection.LOCAL_TO_CLOUD,
        conflict_strategy=SyncConflictStrategy.NEWEST_VERSION_WINS,
        include_lifecycle_states=[ControlPlaneLifecycleState.ACTIVE],
    )
    isolation = TenantIsolationRule(
        id="tenant.ws",
        tenant_id="tenant.a",
        workspace_id="ws.local",
    )
    grant = ConsultantAccessGrant(
        id="grant.a",
        consultant_id="consultant.a",
        client_workspace_id="ws.local",
        role="reviewer",
        status=ConsultantGrantStatus.ACTIVE,
    )
    notification = LocalApprovalNotification(
        id="notify.1",
        run_id="cp-run-001",
        workspace_id="ws.local",
        channel=LocalApprovalNotificationChannel.LOCAL_INBOX,
        event_type=LocalApprovalNotificationEvent.APPROVAL_REQUESTED,
        status=LocalApprovalNotificationStatus.QUEUED,
    )

    assert_type(approval.escalation_state, str)
    assert_type(approval.assigned_actor_id, str | None)
    assert_type(audit_event.run_id, str | None)
    assert_type(import_record.source_protocol, ImportSourceProtocol)
    assert_type(secret_metadata.storage_scope, SecretStorageScope)
    assert_type(sync_rule.direction, SyncDirection)
    assert_type(sync_rule.conflict_strategy, SyncConflictStrategy)
    assert_type(sync_rule.include_lifecycle_states, list[ControlPlaneLifecycleState])
    assert_type(isolation.cross_client_sharing_allowed, bool)
    assert_type(grant.status, ConsultantGrantStatus)
    assert_type(notification.channel, LocalApprovalNotificationChannel)
    assert_type(notification.event_type, LocalApprovalNotificationEvent)
    assert_type(notification.status, LocalApprovalNotificationStatus)

    return ControlPlaneManifest(
        workspace=WorkspaceRef(id="ws.local", name="Local", max_cost_units=100),
        connections=[connection],
        capabilities=[capability],
        agents=[agent],
        triggers=[trigger],
        automations=[automation],
        policies=[policy],
        policy_templates=[template],
        policy_rule_packs=[rule_pack],
        approvals=[approval],
        run_records=[build_control_plane_evidence().run_record],
        audit_events=[audit_event],
        import_records=[import_record],
        secret_metadata=[secret_metadata],
        sync_rules=[sync_rule],
        tenant_isolation_rules=[isolation],
        consultant_access_grants=[grant],
        local_approval_notifications=[notification],
    )


def consume_control_plane_manifest(
    payload: dict[str, object],
) -> ControlPlaneValidationResult:
    """The validator returns the control plane's *own* ``ValidationResult``.

    Not the shared primitive and not any of the other three domain classes of the
    same name, so pinning it here is what would catch this leaf being rerouted to
    a same-named lookalike.
    """
    manifest = build_control_plane_governance()

    assert_type(manifest.workspace, WorkspaceRef)
    assert_type(manifest.schema_version, str)
    assert_type(manifest.capabilities, list[Capability])
    assert_type(manifest.policy_templates, list[PolicyTemplate])
    assert_type(manifest.local_approval_notifications, list[LocalApprovalNotification])
    # A `ContractVersion` instance, not a `str` wire alias. Its type is owned by
    # the knowledge domain, which is why converting this leaf moves one frozen
    # root binding; the fields are checked rather than the class, so this fixture
    # stays inside the control-plane routes the meta-test pins it to.
    assert_type(manifest.contract_version.major, int)
    assert_type(manifest.contract_version.minor, int)
    assert_type(CONTROL_PLANE_CONTRACT_VERSION.major, int)
    assert_type(CONTROL_PLANE_CONTRACT_VERSION.minor, int)
    assert_type(CONTROL_PLANE_SCHEMA_VERSION, str)

    result = validate_control_plane_manifest(manifest)
    assert_type(result, ControlPlaneValidationResult)
    assert_type(result.valid, bool)
    assert_type(result.errors, list[str])
    assert_type(result.warnings, list[str])

    try:
        coerced = manifest_from_dict(payload)
    except ControlPlaneValidationError as error:
        return ControlPlaneValidationResult(valid=False, errors=[str(error)])
    assert_type(coerced, ControlPlaneManifest)
    assert_type(validate_control_plane_manifest(payload), ControlPlaneValidationResult)
    return result


def consume_control_plane_policy_constants() -> PolicyAttributeExpression:
    """The bounded-policy constants keep their precise element types."""
    assert_type(STABLE_ID_PATTERN.pattern, str)
    assert_type(EXTRA_SENSITIVE_KEYS, frozenset[str])
    assert_type(DANGEROUS_SIDE_EFFECTS, frozenset[SideEffect])
    assert_type(RRULE_FREQUENCIES, frozenset[str])
    assert_type(RRULE_WEEKDAYS, frozenset[str])
    assert_type(APPROVAL_ESCALATION_STATES, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_SCOPES, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_VALUE_OPERATORS, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_VALUES_OPERATORS, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_PRESENCE_OPERATORS, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_NUMERIC_OPERATORS, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS, frozenset[str])
    assert_type(POLICY_ATTRIBUTE_OPERATORS, frozenset[str])
    assert_type(POLICY_EXPRESSION_OPS, frozenset[str])
    assert_type(POLICY_EXPRESSION_BOOLEAN_OPS, frozenset[str])
    assert_type(POLICY_EXPRESSION_RAW_FIELDS, frozenset[str])
    assert_type(POLICY_EXPRESSION_SECRET_MARKERS, frozenset[str])
    assert_type(POLICY_EXPRESSION_NUMERIC_COMPARISONS, frozenset[str])
    assert_type(POLICY_EXPRESSION_COMPARISON_OPERATORS, dict[str, str])
    assert_type(POLICY_EXPRESSION_MAX_DEPTH, int)
    assert_type(POLICY_EXPRESSION_MAX_NODES, int)
    assert_type(POLICY_EXPRESSION_SOURCE_MAX_LENGTH, int)
    assert_type(POLICY_EXPRESSION_PARSE_MAX_DEPTH, int)

    # Genuine uses, so a degraded `Any` could not slip through as an unused name.
    matched: bool = STABLE_ID_PATTERN.match("cap.read") is not None
    dangerous: bool = SideEffect.DESTRUCTIVE in DANGEROUS_SIDE_EFFECTS
    bounded: bool = (
        POLICY_EXPRESSION_MAX_DEPTH < POLICY_EXPRESSION_PARSE_MAX_DEPTH
        and POLICY_EXPRESSION_MAX_NODES <= POLICY_EXPRESSION_SOURCE_MAX_LENGTH
    )
    operator: str = POLICY_EXPRESSION_COMPARISON_OPERATORS[">="]
    known: bool = (
        operator in POLICY_ATTRIBUTE_OPERATORS
        and operator in POLICY_ATTRIBUTE_SINGLE_VALUE_OPERATORS
        and operator in POLICY_ATTRIBUTE_NUMERIC_OPERATORS
        and "equals" in POLICY_ATTRIBUTE_VALUE_OPERATORS
        and "in" in POLICY_ATTRIBUTE_VALUES_OPERATORS
        and "exists" in POLICY_ATTRIBUTE_PRESENCE_OPERATORS
        and "actor" in POLICY_ATTRIBUTE_SCOPES
        and "condition" in POLICY_EXPRESSION_OPS
        and "all" in POLICY_EXPRESSION_BOOLEAN_OPS
        and "expression" in POLICY_EXPRESSION_RAW_FIELDS
        and "token" in POLICY_EXPRESSION_SECRET_MARKERS
        and ">=" in POLICY_EXPRESSION_NUMERIC_COMPARISONS
        and "DAILY" in RRULE_FREQUENCIES
        and "MO" in RRULE_WEEKDAYS
        and "pending" in APPROVAL_ESCALATION_STATES
        and "private_key" in EXTRA_SENSITIVE_KEYS
    )

    compiled = compile_policy_expression("actor.role == 'reviewer'")
    assert_type(compiled, PolicyAttributeExpression)
    assert_type(compiled.op, str)
    if matched and dangerous and bounded and known:
        return compiled
    return PolicyAttributeExpression(op="all")


def consume_control_plane_local_evidence() -> tuple[
    RunObservabilityMetrics,
    LocalUsageLedgerEntry,
    LocalObservabilityLogRecord,
    SecretResolutionResult,
    ExecutionMode,
]:
    """The local-only observability, usage, log and secret-resolution records."""
    metrics = RunObservabilityMetrics(
        workspace_id="ws.local",
        run_count=1,
        completed_count=0,
        failed_count=0,
        waiting_for_approval_count=1,
        success_rate=0.0,
        retry_count=0,
        token_usage=0,
        cost_units=0,
    )
    usage = LocalUsageLedgerEntry(
        workspace_id="ws.local",
        subject_type="actor",
        subject_id="actor.a",
        run_count=1,
        token_usage=0,
        cost_units=0,
    )
    log = LocalObservabilityLogRecord(
        id="log.1",
        workspace_id="ws.local",
        run_id="cp-run-001",
        trace_id="trace-1",
        event_type="run.started",
        message="redacted",
    )
    resolution = SecretResolutionResult(
        secret_ref="vault://local",
        resolved=True,
        storage_scope=SecretStorageScope.CLIENT_OWNED,
    )

    assert_type(metrics.success_rate, float)
    assert_type(metrics.connector_health, dict[str, Any])
    assert_type(usage.run_count, int)
    assert_type(log.run_id, str | None)
    assert_type(log.metadata, dict[str, Any])
    assert_type(resolution.resolved, bool)
    assert_type(resolution.storage_scope, SecretStorageScope)
    return metrics, usage, log, resolution, ExecutionMode.DRY_RUN


def consume_control_plane_triggers() -> TriggerIngestionResult:
    """The trigger envelope/ingestion pair stays precisely typed."""
    envelope = TriggerEventEnvelope(
        id="event.1",
        trigger_id="trigger.manual",
        event_type="capability.requested",
        source="local",
        idempotency_key="event-1",
    )
    ingestion = TriggerIngestionResult(
        accepted=True,
        run_record=build_control_plane_evidence().run_record,
    )

    assert_type(envelope.subject, str | None)
    assert_type(envelope.idempotency_key, str | None)
    assert_type(ingestion.accepted, bool)
    assert_type(ingestion.run_record, RunRecord | None)
    assert_type(ingestion.dead_letter_reason, str | None)
    return ingestion


def consume_control_plane_imports(
    mcp_spec: dict[str, object],
    openapi_spec: dict[str, object],
    asyncapi_spec: dict[str, object],
    catalogue_entry: dict[str, object],
) -> ImportSourceChange:
    """The import adapters and their three validation results compose cleanly."""
    mcp_validation = validate_mcp_import_spec(mcp_spec)
    openapi_validation = validate_openapi_import_spec(openapi_spec)
    asyncapi_validation = validate_asyncapi_import_spec(asyncapi_spec)

    assert_type(mcp_validation, ImportSpecValidation)
    assert_type(openapi_validation, ImportSpecValidation)
    assert_type(asyncapi_validation, ImportSpecValidation)
    assert_type(mcp_validation.valid, bool)
    assert_type(mcp_validation.errors, list[str])
    assert_type(mcp_validation.warnings, list[str])

    mcp = import_mcp_candidates("mcp://local", mcp_spec, connection_id="conn.local")
    openapi = import_openapi_candidates("https://local/openapi.json", openapi_spec)
    asyncapi = import_asyncapi_candidates("https://local/asyncapi.json", asyncapi_spec)
    catalogue = import_catalogue_candidates("catalogue://local", catalogue_entry)

    for candidate_set in (mcp, openapi, asyncapi, catalogue):
        assert_type(candidate_set, ImportedCandidateSet)
        assert_type(candidate_set.import_record, ImportRecord)
        assert_type(candidate_set.connections, list[Connection])
        assert_type(candidate_set.capabilities, list[Capability])
        assert_type(candidate_set.triggers, list[Trigger])

    verification = verify_catalogue_artifacts({}, {}, {})
    assert_type(verification, CatalogueArtifactVerification)
    assert_type(verification.valid, bool)
    assert_type(verification.errors, list[str])

    generated = import_catalogue_generated_candidates(
        "catalogue://generated", {}, {}, {}
    )
    assert_type(generated, list[ImportedCandidateSet])
    assert_type(first_or_none(generated), ImportedCandidateSet | None)

    change = detect_import_source_change(mcp.import_record, mcp)
    assert_type(change, ImportSourceChange)
    assert_type(change.changed, bool)
    assert_type(change.previous_source_hash, str | None)
    assert_type(change.current_source_hash, str | None)
    assert_type(change.reason, str)
    return change


# ---------------------------------------------------------------------------
# omnivia_memory.run_ledger.{models,validation}
# ---------------------------------------------------------------------------


def build_run_ledger_entry() -> RunLedgerEntry:
    """Typed construction through the leaf's own classes.

    ``contract_version`` is deliberately left to its default: the default *is*
    ``RUN_LEDGER_CONTRACT_VERSION``, so accepting it here is what proves the
    routed constant still types as the run-ledger entry's declared field type
    rather than as ``Any``.
    """
    assert_type(RUN_LEDGER_PATH_ENV, str)
    # A `ContractVersion` instance, not a `str` wire alias. Its type is owned by
    # the knowledge domain, which is why converting this leaf moves one frozen
    # root binding; the fields are checked rather than the class, so this fixture
    # stays inside the run-ledger routes the meta-test pins it to.
    assert_type(RUN_LEDGER_CONTRACT_VERSION.major, int)
    assert_type(RUN_LEDGER_CONTRACT_VERSION.minor, int)
    version_label: str = str(RUN_LEDGER_CONTRACT_VERSION)

    return RunLedgerEntry(
        run_id="run-001",
        task_id="T-0103",
        target_repo="omnivia-core",
        lane_id="L2",
        status=RunLedgerStatus.RUNNING,
        started_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:05:00+00:00",
        evidence_file_refs=[
            EvidenceFileRef(
                path="artifacts/runs/run-001/summary.md",
                kind="summary",
                description=version_label,
            )
        ],
        provenance=RunLedgerProvenance(
            producer="omnivia-pm",
            source_ref="runs/run-001.json",
            producer_version="1.0.0",
        ),
    )


def consume_run_ledger_entry() -> SharedValidationResult:
    """The three validators compose without a cast or an ignore.

    They return the *shared* ``ValidationResult`` primitive -- not one of the
    four domain classes of the same name -- so pinning that here is what would
    catch this leaf being rerouted to a same-named lookalike.
    """
    entry = build_run_ledger_entry()

    assert_type(entry.run_id, str)
    assert_type(entry.status, RunLedgerStatus)
    assert_type(entry.started_at, str)
    assert_type(entry.completed_at, str | None)
    assert_type(entry.evidence_file_refs, list[EvidenceFileRef])
    assert_type(entry.provenance, RunLedgerProvenance)

    assert_type(TERMINAL_RUN_STATUSES, frozenset[RunLedgerStatus])
    terminal: bool = entry.status in TERMINAL_RUN_STATUSES

    evidence_result = validate_evidence_file_ref(entry.evidence_file_refs[0])
    provenance_result = validate_run_ledger_provenance(entry.provenance)
    entry_result = validate_run_ledger_entry(entry)

    assert_type(evidence_result, SharedValidationResult)
    assert_type(provenance_result, SharedValidationResult)
    assert_type(entry_result, SharedValidationResult)
    assert_type(entry_result.valid, bool)
    assert_type(entry_result.errors, list[str])
    assert_type(entry_result.warnings, list[str])

    payload = entry.to_dict()
    assert_type(payload, dict[str, object])
    restored = RunLedgerEntry.from_dict(payload)
    assert_type(restored, RunLedgerEntry)

    evidence = entry.evidence_file_refs[0]
    assert_type(evidence.path, str)
    assert_type(evidence.kind, str)
    assert_type(evidence.description, str | None)
    assert_type(evidence.checksum, str | None)
    assert_type(evidence.to_dict(), dict[str, str | None])
    assert_type(entry.provenance.producer, str)
    assert_type(entry.provenance.source_ref, str | None)
    assert_type(entry.provenance.producer_version, str | None)

    if terminal and not entry_result.valid:
        return provenance_result
    return entry_result


def roundtrip() -> str:
    """The leaves compose with each other, not only individually."""
    manifest = build_app_manifest()
    contract = build_component_contract()
    memory = build_memory()
    create, update = memory_write_shapes()
    context = build_app_shell_host_context()
    body = build_app_shell_body_descriptor()

    return ":".join(
        [
            manifest.app_id,
            consume_app_manifest({"app_id": manifest.app_id}),
            contract.component_id,
            consume_component_contract({"component_id": contract.component_id}),
            consume_agent_run_record({"run_id": "r1"}).value,
            consume_app_shell({"app_id": context.app_id}, {"body_id": body.body_id}),
            memory.content,
            create.content,
            update.content or "",
            lifecycle_transitions().value,
            str(shared_validation().valid),
            str(consume_run_ledger_entry().valid),
            build_run_ledger_entry().status.value,
            str(consume_control_plane_manifest({"workspace": {"id": "ws.local"}}).valid),
            consume_control_plane_policy_constants().op,
            consume_control_plane_local_evidence()[4].value,
            str(consume_control_plane_triggers().accepted),
            str(consume_control_plane_imports({}, {}, {}, {}).changed),
            consume_memory_graph_fixture()["source"].id,
            assemble_memory_graph_displays()[0].generated_at,
            str(consume_memory_graph_validators().valid),
        ]
    )
