"""Component Contract validation."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .models import AgentBackedComponentContract, AgentRunRecord, ComponentContract


class ComponentContractValidationError(Exception):
    """Raised when a Component Contract fails validation."""

    pass


def _append_enum_error(
    errors: List[str], field_name: str, enum_type: type, raw_value: object
) -> None:
    valid = ", ".join(item.value for item in enum_type)
    errors.append(f"'{field_name}' must be one of: {valid}, got {raw_value!r}")


def _validate_string_list(errors: List[str], value: object, field_name: str) -> None:
    if not isinstance(value, list):
        errors.append(f"'{field_name}' must be a list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"'{field_name}[{index}]' must be a non-empty string")


def _validate_bool(errors: List[str], value: object, field_name: str) -> None:
    if not isinstance(value, bool):
        errors.append(f"'{field_name}' must be a bool")


def _validate_agent_backed_contract(
    data: Dict[str, Any], errors: List[str]
) -> Optional[Dict[str, Any]]:
    agent_raw = data.get("agent_backed")
    if agent_raw is None:
        return None
    if not isinstance(agent_raw, dict):
        errors.append("'agent_backed' must be a dict")
        return None

    from .models import AgentAction, ComponentAIMode, ComponentOutputType
    from .models import ComponentRunMode, ComponentSafetyLevel

    for field_name in ("app_compatibility", "data_sources"):
        value = agent_raw.get(field_name)
        if value is None:
            errors.append(f"'agent_backed.{field_name}' is required")
        elif field_name == "app_compatibility":
            _validate_string_list(errors, value, f"agent_backed.{field_name}")
        elif not isinstance(value, list):
            errors.append(f"'agent_backed.{field_name}' must be a list")

    for field_name, enum_type in (
        ("ai_mode", ComponentAIMode),
        ("run_mode", ComponentRunMode),
        ("output_type", ComponentOutputType),
        ("safety_level", ComponentSafetyLevel),
    ):
        raw_value = agent_raw.get(field_name)
        if raw_value is None:
            errors.append(f"'agent_backed.{field_name}' is required")
            continue
        try:
            enum_type(raw_value)
        except ValueError:
            _append_enum_error(errors, f"agent_backed.{field_name}", enum_type, raw_value)

    if agent_raw.get("safety_level") == ComponentSafetyLevel.LEVEL_5.value:
        errors.append("'agent_backed.safety_level' level_5 is not allowed by default")

    data_sources_raw = agent_raw.get("data_sources")
    if isinstance(data_sources_raw, list):
        for index, item in enumerate(data_sources_raw):
            if not isinstance(item, dict):
                errors.append(
                    f"'agent_backed.data_sources[{index}]' must be a dict, "
                    f"got {type(item).__name__}"
                )
            elif not item.get("source_id"):
                errors.append(
                    f"'agent_backed.data_sources[{index}]' is missing 'source_id'"
                )

    agent_behavior_raw = agent_raw.get("agent_behavior")
    if agent_behavior_raw is None:
        errors.append("'agent_backed.agent_behavior' is required")
    elif not isinstance(agent_behavior_raw, dict):
        errors.append("'agent_backed.agent_behavior' must be a dict")
    else:
        objective = agent_behavior_raw.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            errors.append("'agent_backed.agent_behavior.objective' is required")
        allowed_actions_raw = agent_behavior_raw.get("allowed_actions", [])
        if not isinstance(allowed_actions_raw, list):
            errors.append("'agent_backed.agent_behavior.allowed_actions' must be a list")
        else:
            for index, item in enumerate(allowed_actions_raw):
                try:
                    AgentAction(item)
                except ValueError:
                    _append_enum_error(
                        errors,
                        f"agent_backed.agent_behavior.allowed_actions[{index}]",
                        AgentAction,
                        item,
                    )
        max_safety_raw = agent_behavior_raw.get("max_safety_level")
        if max_safety_raw is not None:
            try:
                ComponentSafetyLevel(max_safety_raw)
            except ValueError:
                _append_enum_error(
                    errors,
                    "agent_backed.agent_behavior.max_safety_level",
                    ComponentSafetyLevel,
                    max_safety_raw,
                )

    for scope_name in ("graph_scope", "connector_scope"):
        scope_raw = agent_raw.get(scope_name, {})
        if not isinstance(scope_raw, dict):
            errors.append(f"'agent_backed.{scope_name}' must be a dict")
            continue
        if scope_name == "graph_scope":
            for list_field in ("node_types", "edge_types"):
                if list_field in scope_raw:
                    _validate_string_list(
                        errors,
                        scope_raw.get(list_field),
                        f"agent_backed.{scope_name}.{list_field}",
                    )
            if "allow_write" in scope_raw:
                _validate_bool(errors, scope_raw.get("allow_write"), "agent_backed.graph_scope.allow_write")
        else:
            if "connector_ids" in scope_raw:
                _validate_string_list(
                    errors,
                    scope_raw.get("connector_ids"),
                    "agent_backed.connector_scope.connector_ids",
                )
            if "allow_external_action" in scope_raw:
                _validate_bool(
                    errors,
                    scope_raw.get("allow_external_action"),
                    "agent_backed.connector_scope.allow_external_action",
                )

    for policy_name in ("permission_policy", "approval_policy"):
        policy_raw = agent_raw.get(policy_name, {})
        if not isinstance(policy_raw, dict):
            errors.append(f"'agent_backed.{policy_name}' must be a dict")
            continue
        if policy_name == "permission_policy":
            if "required_permissions" in policy_raw:
                _validate_string_list(
                    errors,
                    policy_raw.get("required_permissions"),
                    "agent_backed.permission_policy.required_permissions",
                )
            allowed_policy_actions_raw = policy_raw.get("allowed_actions", [])
            if not isinstance(allowed_policy_actions_raw, list):
                errors.append("'agent_backed.permission_policy.allowed_actions' must be a list")
            else:
                for index, item in enumerate(allowed_policy_actions_raw):
                    try:
                        AgentAction(item)
                    except ValueError:
                        _append_enum_error(
                            errors,
                            f"agent_backed.permission_policy.allowed_actions[{index}]",
                            AgentAction,
                            item,
                        )
        else:
            if "human_required" in policy_raw:
                _validate_bool(
                    errors,
                    policy_raw.get("human_required"),
                    "agent_backed.approval_policy.human_required",
                )

    for requirements_name in ("provenance_requirements", "audit_requirements"):
        requirements_raw = agent_raw.get(requirements_name, {})
        if not isinstance(requirements_raw, dict):
            errors.append(f"'agent_backed.{requirements_name}' must be a dict")
            continue
        if requirements_name == "provenance_requirements":
            for bool_field in ("require_sources", "require_citations", "require_audit_log"):
                if bool_field in requirements_raw:
                    _validate_bool(
                        errors,
                        requirements_raw.get(bool_field),
                        f"agent_backed.{requirements_name}.{bool_field}",
                    )
        else:
            if "required" in requirements_raw:
                _validate_bool(
                    errors,
                    requirements_raw.get("required"),
                    "agent_backed.audit_requirements.required",
                )
            if "event_types" in requirements_raw:
                _validate_string_list(
                    errors,
                    requirements_raw.get("event_types"),
                    "agent_backed.audit_requirements.event_types",
                )

    approval_policy_raw = agent_raw.get("approval_policy", {})
    human_required = (
        approval_policy_raw.get("human_required", False)
        if isinstance(approval_policy_raw, dict)
        else False
    )

    if _requires_human_approval(agent_raw) and human_required is not True:
        errors.append(
            "'agent_backed.approval_policy.human_required' is required for "
            "write, graph mutation, external action, send or publish paths"
        )

    return agent_raw


def validate_component_contract(data: Dict[str, Any]) -> "ComponentContract":
    """Validate a plain dict and return a ComponentContract.

    Args:
        data: A dictionary containing contract data.

    Returns:
        A validated ComponentContract instance.

    Raises:
        ComponentContractValidationError: If validation fails.
    """
    errors = []

    for field_name in ("contract_version", "component_id", "display_name", "version"):
        value = data.get(field_name)
        if value is None:
            errors.append(f"'{field_name}' is required")
        elif not isinstance(value, str):
            errors.append(f"'{field_name}' must be a string, got {type(value).__name__}")
        elif not value.strip():
            errors.append(f"'{field_name}' must not be empty")

    family_raw = data.get("family")
    if family_raw is None:
        errors.append("'family' is required")
    else:
        from .models import ComponentFamily
        try:
            ComponentFamily(family_raw)
        except ValueError:
            valid = ", ".join(f.value for f in ComponentFamily)
            errors.append(f"'family' must be one of: {valid}, got {family_raw!r}")

    provenance_raw = data.get("provenance_behavior")
    if provenance_raw is not None:
        from .models import ProvenanceBehavior
        try:
            ProvenanceBehavior(provenance_raw)
        except ValueError:
            valid = ", ".join(f.value for f in ProvenanceBehavior)
            errors.append(f"'provenance_behavior' must be one of: {valid}, got {provenance_raw!r}")

    inputs_raw = data.get("inputs")
    if inputs_raw is not None:
        if not isinstance(inputs_raw, list):
            errors.append("'inputs' must be a list")
        else:
            for i, item in enumerate(inputs_raw):
                if not isinstance(item, dict):
                    errors.append(f"'inputs[{i}]' must be a dict, got {type(item).__name__}")
                elif not item.get("name"):
                    errors.append(f"'inputs[{i}]' is missing 'name'")

    outputs_raw = data.get("outputs")
    if outputs_raw is not None:
        if not isinstance(outputs_raw, list):
            errors.append("'outputs' must be a list")
        else:
            for i, item in enumerate(outputs_raw):
                if not isinstance(item, dict):
                    errors.append(f"'outputs[{i}]' must be a dict, got {type(item).__name__}")
                elif not item.get("name"):
                    errors.append(f"'outputs[{i}]' is missing 'name'")

    permissions_raw = data.get("permission_requirements")
    if permissions_raw is not None:
        if not isinstance(permissions_raw, list):
            errors.append("'permission_requirements' must be a list")
        else:
            for i, item in enumerate(permissions_raw):
                if not isinstance(item, dict):
                    errors.append(
                        f"'permission_requirements[{i}]' must be a dict, "
                        f"got {type(item).__name__}"
                    )
                elif not item.get("name"):
                    errors.append(f"'permission_requirements[{i}]' is missing 'name'")

    agent_raw = _validate_agent_backed_contract(data, errors)

    if errors:
        raise ComponentContractValidationError("; ".join(errors))

    from .models import ComponentContract, ComponentFamily, ProvenanceBehavior
    from .models import ComponentInput, ComponentOutput, ComponentPermission, ValidationResult

    family = ComponentFamily(data["family"])
    provenance_behavior = ProvenanceBehavior(data.get("provenance_behavior", "passthrough"))

    inputs = [
        ComponentInput(name=item["name"], description=item.get("description", ""))
        for item in (inputs_raw or [])
    ]
    outputs = [
        ComponentOutput(name=item["name"], description=item.get("description", ""))
        for item in (outputs_raw or [])
    ]
    permission_requirements = [
        ComponentPermission(name=item["name"], description=item.get("description", ""))
        for item in (permissions_raw or [])
    ]

    agent_backed = _build_agent_backed_contract(agent_raw) if agent_raw else None

    return ComponentContract(
        contract_version=data["contract_version"],
        component_id=data["component_id"],
        display_name=data["display_name"],
        family=family,
        version=data["version"],
        inputs=inputs,
        outputs=outputs,
        permission_requirements=permission_requirements,
        provenance_behavior=provenance_behavior,
        validation=ValidationResult(is_valid=True),
        agent_backed=agent_backed,
    )


def validate_agent_run_record(data: Dict[str, Any]) -> "AgentRunRecord":
    """Validate a plain dict and return an AgentRunRecord."""
    errors: List[str] = []

    for field_name in ("run_id", "component_id"):
        value = data.get(field_name)
        if value is None:
            errors.append(f"'{field_name}' is required")
        elif not isinstance(value, str):
            errors.append(f"'{field_name}' must be a string, got {type(value).__name__}")
        elif not value.strip():
            errors.append(f"'{field_name}' must not be empty")

    from .models import AgentRunStatus

    status_raw = data.get("status")
    if status_raw is None:
        errors.append("'status' is required")
    else:
        try:
            AgentRunStatus(status_raw)
        except ValueError:
            _append_enum_error(errors, "status", AgentRunStatus, status_raw)

    if "approval_required" in data:
        _validate_bool(errors, data.get("approval_required"), "approval_required")

    audit_ids_raw = data.get("audit_event_ids", [])
    _validate_string_list(errors, audit_ids_raw, "audit_event_ids")

    if errors:
        raise ComponentContractValidationError("; ".join(errors))

    from .models import AgentRunRecord

    return AgentRunRecord(
        run_id=data["run_id"],
        component_id=data["component_id"],
        status=AgentRunStatus(data["status"]),
        objective=data.get("objective", ""),
        approval_required=data.get("approval_required", False),
        blocked_reason=data.get("blocked_reason", ""),
        output_summary=data.get("output_summary", ""),
        error_message=data.get("error_message", ""),
        audit_event_ids=list(audit_ids_raw),
    )


def _requires_human_approval(agent_raw: Dict[str, Any]) -> bool:
    from .models import AgentAction

    approval_actions = {
        AgentAction.LOCAL_WRITE.value,
        AgentAction.GRAPH_MUTATION.value,
        AgentAction.EXTERNAL_ACTION.value,
        AgentAction.SEND.value,
        AgentAction.PUBLISH.value,
    }
    behavior_raw = agent_raw.get("agent_behavior", {})
    allowed_actions = []
    if isinstance(behavior_raw, dict):
        raw_allowed_actions = behavior_raw.get("allowed_actions", [])
        if isinstance(raw_allowed_actions, list):
            allowed_actions = raw_allowed_actions
    graph_scope = agent_raw.get("graph_scope", {})
    connector_scope = agent_raw.get("connector_scope", {})

    return (
        any(action in approval_actions for action in allowed_actions)
        or (isinstance(graph_scope, dict) and graph_scope.get("allow_write") is True)
        or (
            isinstance(connector_scope, dict)
            and connector_scope.get("allow_external_action") is True
        )
    )


def _build_agent_backed_contract(
    agent_raw: Dict[str, Any]
) -> "AgentBackedComponentContract":
    from .models import AgentAction, AgentBackedComponentContract, AgentBehavior
    from .models import ApprovalPolicy, AuditRequirement, ComponentAIMode
    from .models import ComponentConnectorScope, ComponentDataSource
    from .models import ComponentGraphScope, ComponentOutputType, ComponentRunMode
    from .models import ComponentSafetyLevel, PermissionPolicy, ProvenanceRequirement

    behavior_raw = agent_raw["agent_behavior"]
    graph_scope_raw = agent_raw.get("graph_scope", {})
    connector_scope_raw = agent_raw.get("connector_scope", {})
    permission_policy_raw = agent_raw.get("permission_policy", {})
    approval_policy_raw = agent_raw.get("approval_policy", {})
    provenance_raw = agent_raw.get("provenance_requirements", {})
    audit_raw = agent_raw.get("audit_requirements", {})

    return AgentBackedComponentContract(
        app_compatibility=list(agent_raw["app_compatibility"]),
        data_sources=[
            ComponentDataSource(
                source_id=item["source_id"],
                description=item.get("description", ""),
            )
            for item in agent_raw["data_sources"]
        ],
        ai_mode=ComponentAIMode(agent_raw["ai_mode"]),
        run_mode=ComponentRunMode(agent_raw["run_mode"]),
        agent_behavior=AgentBehavior(
            objective=behavior_raw["objective"],
            allowed_actions=[
                AgentAction(item)
                for item in behavior_raw.get("allowed_actions", [])
            ],
            max_safety_level=ComponentSafetyLevel(
                behavior_raw.get("max_safety_level", "level_2")
            ),
        ),
        output_type=ComponentOutputType(agent_raw["output_type"]),
        safety_level=ComponentSafetyLevel(agent_raw["safety_level"]),
        graph_scope=ComponentGraphScope(
            node_types=list(graph_scope_raw.get("node_types", [])),
            edge_types=list(graph_scope_raw.get("edge_types", [])),
            allow_write=graph_scope_raw.get("allow_write", False),
        ),
        connector_scope=ComponentConnectorScope(
            connector_ids=list(connector_scope_raw.get("connector_ids", [])),
            allow_external_action=connector_scope_raw.get(
                "allow_external_action", False
            ),
        ),
        permission_policy=PermissionPolicy(
            required_permissions=list(
                permission_policy_raw.get("required_permissions", [])
            ),
            allowed_actions=[
                AgentAction(item)
                for item in permission_policy_raw.get("allowed_actions", [])
            ],
        ),
        approval_policy=ApprovalPolicy(
            human_required=approval_policy_raw.get("human_required", False),
            approval_reason=approval_policy_raw.get("approval_reason", ""),
            approver_role=approval_policy_raw.get("approver_role", ""),
        ),
        provenance_requirements=ProvenanceRequirement(
            require_sources=provenance_raw.get("require_sources", True),
            require_citations=provenance_raw.get("require_citations", True),
            require_audit_log=provenance_raw.get("require_audit_log", True),
        ),
        audit_requirements=AuditRequirement(
            required=audit_raw.get("required", True),
            event_types=list(audit_raw.get("event_types", [])),
        ),
        error_states=list(agent_raw.get("error_states", [])),
    )
