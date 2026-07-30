"""Strict-mypy consumer fixture for every accepted legacy facade leaf.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for the eleven facade leaves converted before the Module Manifest
batch (that batch has its own fixture,
``tests/typing/module_manifest_facade_consumer.py``).

It imports every domain/API symbol those leaves route -- exactly the
``baseline.inventory.FACADE_ROUTES`` entry for each -- and only ever through the
``omnivia_memory.*`` path, never through ``omnivia_core.*``. Incidental bindings
(``Enum``, ``List``, ``dataclass``, ``annotations``, ...) are not a downstream
surface and are covered by direct strict checking of the wrappers themselves.

Names that legitimately collide across leaves (``ValidationResult``,
``ProvenanceRequirement``, ``LifecycleState``, ``CreatedBy``, ``Source``) are
imported under distinct aliases so each leaf's own historically-owned object is
exercised separately -- aliasing the binding here does not weaken the check,
because implicit re-export is decided by the *source* module either way.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If a facade ever stopped explicitly
re-exporting these names, or degraded them to ``Any``, strict mypy would fail
here.
"""

from typing import assert_type

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
from omnivia_memory.provenance.models import Source, SourceType

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
        ]
    )
