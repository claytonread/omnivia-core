from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ComponentFamily(Enum):
    """Component family/classification."""
    UI = "ui"
    DATA = "data"
    LOGIC = "logic"
    INTEGRATION = "integration"
    UTILITY = "utility"


class ProvenanceBehavior(Enum):
    """How the Component handles source/provenance data."""
    PASSTHROUGH = "passthrough"
    TRACK = "track"
    SIGN = "sign"
    VERIFY = "verify"


class ComponentAIMode(Enum):
    """AI routing mode for an agent-backed Component."""
    LOCAL_FIRST = "local_first"
    CONFIGURED_ROUTE = "configured_route"
    EXTERNAL_APPROVED = "external_approved"


class ComponentRunMode(Enum):
    """How an agent-backed Component may be started."""
    MANUAL = "manual"
    APPROVAL_GATED = "approval_gated"


class AgentAction(Enum):
    """Public-safe action category an agent-backed Component may request."""
    READ = "read"
    SUGGEST = "suggest"
    DRAFT = "draft"
    LOCAL_WRITE = "local_write"
    GRAPH_MUTATION = "graph_mutation"
    EXTERNAL_ACTION = "external_action"
    SEND = "send"
    PUBLISH = "publish"


class ComponentOutputType(Enum):
    """Output category for an agent-backed Component."""
    BRIEF = "brief"
    SUGGESTIONS = "suggestions"
    DRAFT = "draft"
    EVIDENCE = "evidence"
    REPORT = "report"


class ComponentSafetyLevel(Enum):
    """Safety level for an agent-backed Component."""
    LEVEL_0 = "level_0"
    LEVEL_1 = "level_1"
    LEVEL_2 = "level_2"
    LEVEL_3 = "level_3"
    LEVEL_4 = "level_4"
    LEVEL_5 = "level_5"


class AgentRunStatus(Enum):
    """Status for an agent-backed Component run record."""
    PENDING = "pending"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ComponentInput:
    """An input port for a Component."""
    name: str
    description: str = ""


@dataclass
class ComponentOutput:
    """An output port for a Component."""
    name: str
    description: str = ""


@dataclass
class ComponentPermission:
    """A permission required by a Component."""
    name: str
    description: str = ""


@dataclass
class ComponentDataSource:
    """A source an agent-backed Component may use."""
    source_id: str
    description: str = ""


@dataclass
class ComponentGraphScope:
    """Graph scope available to an agent-backed Component."""
    node_types: List[str] = field(default_factory=list)
    edge_types: List[str] = field(default_factory=list)
    allow_write: bool = False


@dataclass
class ComponentConnectorScope:
    """Connector scope available to an agent-backed Component."""
    connector_ids: List[str] = field(default_factory=list)
    allow_external_action: bool = False


@dataclass
class AgentBehavior:
    """Bounded behavior contract for an agent-backed Component."""
    objective: str
    allowed_actions: List[AgentAction] = field(default_factory=list)
    max_safety_level: ComponentSafetyLevel = ComponentSafetyLevel.LEVEL_2


@dataclass
class PermissionPolicy:
    """Permission policy for an agent-backed Component."""
    required_permissions: List[str] = field(default_factory=list)
    allowed_actions: List[AgentAction] = field(default_factory=list)


@dataclass
class ApprovalPolicy:
    """Human approval policy for higher-risk agent-backed Component actions."""
    human_required: bool = False
    approval_reason: str = ""
    approver_role: str = ""


@dataclass
class ProvenanceRequirement:
    """Source and provenance requirements for an agent-backed Component."""
    require_sources: bool = True
    require_citations: bool = True
    require_audit_log: bool = True


@dataclass
class AuditRequirement:
    """Audit requirements for an agent-backed Component."""
    required: bool = True
    event_types: List[str] = field(default_factory=list)


@dataclass
class AgentBackedComponentContract:
    """Contract shape for a Component with bounded agentic behavior."""
    app_compatibility: List[str]
    data_sources: List[ComponentDataSource]
    ai_mode: ComponentAIMode
    run_mode: ComponentRunMode
    agent_behavior: AgentBehavior
    output_type: ComponentOutputType
    safety_level: ComponentSafetyLevel
    graph_scope: ComponentGraphScope = field(default_factory=lambda: ComponentGraphScope())
    connector_scope: ComponentConnectorScope = field(
        default_factory=lambda: ComponentConnectorScope()
    )
    permission_policy: PermissionPolicy = field(default_factory=lambda: PermissionPolicy())
    approval_policy: ApprovalPolicy = field(default_factory=lambda: ApprovalPolicy())
    provenance_requirements: ProvenanceRequirement = field(
        default_factory=lambda: ProvenanceRequirement()
    )
    audit_requirements: AuditRequirement = field(default_factory=lambda: AuditRequirement())
    error_states: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Result of validating a Component contract."""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AgentRunRecord:
    """Public-safe record for an agent-backed Component run."""
    run_id: str
    component_id: str
    status: AgentRunStatus
    objective: str = ""
    approval_required: bool = False
    blocked_reason: str = ""
    output_summary: str = ""
    error_message: str = ""
    audit_event_ids: List[str] = field(default_factory=list)


@dataclass
class ComponentContract:
    """Contract definition for a reusable Component.

    Attributes:
        contract_version: Semantic version of the contract schema.
        component_id: Unique identifier for the Component.
        display_name: Human-readable name.
        family: Component family/classification.
        version: Semantic version of the Component.
        inputs: List of input ports.
        outputs: List of output ports.
        permission_requirements: List of permissions required.
        provenance_behavior: How the Component handles provenance.
        validation: Validation result metadata.
        agent_backed: Optional agent-backed Component contract.
    """
    contract_version: str
    component_id: str
    display_name: str
    family: ComponentFamily
    version: str
    inputs: List[ComponentInput] = field(default_factory=list)
    outputs: List[ComponentOutput] = field(default_factory=list)
    permission_requirements: List[ComponentPermission] = field(default_factory=list)
    provenance_behavior: ProvenanceBehavior = ProvenanceBehavior.PASSTHROUGH
    validation: ValidationResult = field(default_factory=lambda: ValidationResult(is_valid=True))
    agent_backed: Optional[AgentBackedComponentContract] = None
