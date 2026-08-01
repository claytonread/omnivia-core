import pytest

from omnivia_memory.component_contract import (
    AgentAction,
    AgentRunRecord,
    AgentRunStatus,
    ComponentAIMode,
    ComponentFamily,
    ComponentInput,
    ComponentOutputType,
    ComponentRunMode,
    ComponentSafetyLevel,
    ComponentContract,
    ComponentContractValidationError,
    ProvenanceBehavior,
    ValidationResult,
    validate_agent_run_record,
    validate_component_contract,
)


def minimal_data(**overrides):
    """Return a minimal valid contract dict."""
    return {
        "contract_version": "1.0.0",
        "component_id": "test-component",
        "display_name": "Test Component",
        "family": "ui",
        "version": "1.0.0",
        **overrides,
    }


def agent_backed_data(**overrides):
    """Return a minimal valid agent-backed Component contract dict."""
    agent_backed = {
        "app_compatibility": ["apps.customer-success"],
        "data_sources": [
            {
                "source_id": "meeting-notes",
                "description": "Meeting notes and transcript summary",
            }
        ],
        "graph_scope": {
            "node_types": ["contact", "meeting"],
            "edge_types": ["attended"],
            "allow_write": False,
        },
        "connector_scope": {
            "connector_ids": [],
            "allow_external_action": False,
        },
        "ai_mode": "local_first",
        "run_mode": "manual",
        "agent_behavior": {
            "objective": "Prepare a source-backed meeting brief",
            "allowed_actions": ["read", "suggest", "draft"],
            "max_safety_level": "level_2",
        },
        "output_type": "brief",
        "permission_policy": {
            "required_permissions": ["graph:read", "memory:read"],
            "allowed_actions": ["read", "suggest", "draft"],
        },
        "approval_policy": {
            "human_required": False,
        },
        "safety_level": "level_2",
        "provenance_requirements": {
            "require_sources": True,
            "require_citations": True,
            "require_audit_log": True,
        },
        "audit_requirements": {
            "required": True,
            "event_types": ["started", "completed", "blocked"],
        },
        "error_states": ["missing_source", "approval_required", "route_blocked"],
    }
    agent_backed.update(overrides)
    return minimal_data(
        family="logic",
        provenance_behavior="track",
        agent_backed=agent_backed,
    )


class TestValidateComponentContractValid:
    def test_minimal_valid(self):
        data = minimal_data()
        contract = validate_component_contract(data)
        assert contract.contract_version == "1.0.0"
        assert contract.component_id == "test-component"
        assert contract.display_name == "Test Component"
        assert contract.family == ComponentFamily.UI
        assert contract.version == "1.0.0"
        assert contract.inputs == []
        assert contract.outputs == []
        assert contract.permission_requirements == []
        assert contract.provenance_behavior == ProvenanceBehavior.PASSTHROUGH
        assert contract.validation.is_valid is True
        assert contract.agent_backed is None

    def test_with_inputs(self):
        data = minimal_data(
            inputs=[
                {"name": "text", "description": "Raw text input"},
                {"name": "image"},
            ]
        )
        contract = validate_component_contract(data)
        assert len(contract.inputs) == 2
        assert contract.inputs[0].name == "text"
        assert contract.inputs[0].description == "Raw text input"
        assert contract.inputs[1].name == "image"
        assert contract.inputs[1].description == ""

    def test_with_outputs(self):
        data = minimal_data(
            outputs=[
                {"name": "result", "description": "Processed result"},
                {"name": "metadata"},
            ]
        )
        contract = validate_component_contract(data)
        assert len(contract.outputs) == 2
        assert contract.outputs[0].name == "result"
        assert contract.outputs[0].description == "Processed result"
        assert contract.outputs[1].name == "metadata"
        assert contract.outputs[1].description == ""

    def test_with_permission_requirements(self):
        data = minimal_data(
            permission_requirements=[
                {"name": "filesystem:read", "description": "Read files from disk"},
                {"name": "network:outbound"},
            ]
        )
        contract = validate_component_contract(data)
        assert len(contract.permission_requirements) == 2
        assert contract.permission_requirements[0].name == "filesystem:read"
        assert contract.permission_requirements[0].description == "Read files from disk"
        assert contract.permission_requirements[1].name == "network:outbound"
        assert contract.permission_requirements[1].description == ""

    def test_provenance_behavior_variants(self):
        for value in ("passthrough", "track", "sign", "verify"):
            data = minimal_data(provenance_behavior=value)
            contract = validate_component_contract(data)
            assert contract.provenance_behavior == ProvenanceBehavior(value)

    def test_all_fields_present(self):
        data = minimal_data(
            inputs=[{"name": "in1", "description": "desc"}],
            outputs=[{"name": "out1", "description": "desc"}],
            permission_requirements=[{"name": "perm1", "description": "desc"}],
            provenance_behavior="track",
        )
        contract = validate_component_contract(data)
        assert len(contract.inputs) == 1
        assert len(contract.outputs) == 1
        assert len(contract.permission_requirements) == 1
        assert contract.provenance_behavior == ProvenanceBehavior.TRACK

    def test_agent_backed_contract_valid(self):
        contract = validate_component_contract(agent_backed_data())
        agent_backed = contract.agent_backed
        assert agent_backed is not None
        assert agent_backed.app_compatibility == ["apps.customer-success"]
        assert agent_backed.data_sources[0].source_id == "meeting-notes"
        assert agent_backed.graph_scope.node_types == ["contact", "meeting"]
        assert agent_backed.graph_scope.allow_write is False
        assert agent_backed.connector_scope.allow_external_action is False
        assert agent_backed.ai_mode == ComponentAIMode.LOCAL_FIRST
        assert agent_backed.run_mode == ComponentRunMode.MANUAL
        assert agent_backed.agent_behavior.objective == (
            "Prepare a source-backed meeting brief"
        )
        assert agent_backed.agent_behavior.allowed_actions == [
            AgentAction.READ,
            AgentAction.SUGGEST,
            AgentAction.DRAFT,
        ]
        assert agent_backed.output_type == ComponentOutputType.BRIEF
        assert agent_backed.safety_level == ComponentSafetyLevel.LEVEL_2
        assert agent_backed.permission_policy.required_permissions == [
            "graph:read",
            "memory:read",
        ]
        assert agent_backed.approval_policy.human_required is False
        assert agent_backed.provenance_requirements.require_sources is True
        assert agent_backed.audit_requirements.event_types == [
            "started",
            "completed",
            "blocked",
        ]


class TestValidateComponentContractInvalid:
    @pytest.mark.parametrize(
        "field",
        ["contract_version", "component_id", "display_name", "version"],
    )
    def test_missing_required_field(self, field):
        data = minimal_data()
        data.pop(field)
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert f"'{field}' is required" in exc_info.value.args[0]

    @pytest.mark.parametrize(
        "field",
        ["contract_version", "component_id", "display_name", "version"],
    )
    def test_empty_required_field(self, field):
        data = minimal_data(**{field: "   "})
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert f"'{field}' must not be empty" in exc_info.value.args[0]

    @pytest.mark.parametrize(
        "field",
        ["contract_version", "component_id", "display_name", "version"],
    )
    def test_wrong_type_required_field(self, field):
        data = minimal_data(**{field: 123})
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert f"'{field}' must be a string" in exc_info.value.args[0]

    def test_missing_family(self):
        data = minimal_data()
        data.pop("family")
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "'family' is required" in exc_info.value.args[0]

    @pytest.mark.parametrize("invalid_family", ["widget", "uii", "", 42])
    def test_invalid_family(self, invalid_family):
        data = minimal_data(family=invalid_family)
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        error_msg = str(exc_info.value)
        assert "family" in error_msg
        assert "must be one of" in error_msg

    @pytest.mark.parametrize("invalid_provenance", ["passthru", "log", "", 42])
    def test_invalid_provenance_behavior(self, invalid_provenance):
        data = minimal_data(provenance_behavior=invalid_provenance)
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        error_msg = str(exc_info.value)
        assert "provenance_behavior" in error_msg
        assert "must be one of" in error_msg

    def test_inputs_not_list(self):
        data = minimal_data(inputs="not-a-list")
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "'inputs' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_input",
        ["not-a-dict", 42, None, {"description": "has no name"}],
        ids=["string", "int", "None", "missing-name"],
    )
    def test_invalid_inputs_item(self, invalid_input):
        data = minimal_data(inputs=[invalid_input])
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "inputs[0]" in str(exc_info.value)

    def test_outputs_not_list(self):
        data = minimal_data(outputs={"name": "x"})
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "'outputs' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_output",
        [None, {"description": "no name"}],
        ids=["None", "missing-name"],
    )
    def test_invalid_outputs_item(self, invalid_output):
        data = minimal_data(outputs=[invalid_output])
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "outputs[0]" in str(exc_info.value)

    def test_permission_requirements_not_list(self):
        data = minimal_data(permission_requirements=True)
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "'permission_requirements' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_perm",
        [{"description": "no name"}, 3.14],
        ids=["missing-name", "wrong-type"],
    )
    def test_invalid_permission_requirements_item(self, invalid_perm):
        data = minimal_data(permission_requirements=[invalid_perm])
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "permission_requirements[0]" in str(exc_info.value)

    def test_multiple_errors_collected(self):
        data = {
            "contract_version": "",
            "display_name": "",
            "family": "invalid",
            "inputs": "bad",
            "outputs": 42,
        }
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert len(str(exc_info.value).split(";")) >= 4

    def test_agent_backed_requires_agent_behavior(self):
        data = agent_backed_data(agent_behavior=None)
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "'agent_backed.agent_behavior' is required" in str(exc_info.value)

    def test_agent_backed_rejects_invalid_enum(self):
        data = agent_backed_data(ai_mode="remote_auto")
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "agent_backed.ai_mode" in str(exc_info.value)
        assert "must be one of" in str(exc_info.value)

    def test_agent_backed_rejects_level_5_by_default(self):
        data = agent_backed_data(safety_level="level_5")
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "level_5 is not allowed by default" in str(exc_info.value)

    @pytest.mark.parametrize(
        "action",
        ["local_write", "graph_mutation", "external_action", "send", "publish"],
    )
    def test_agent_backed_write_paths_require_human_approval(self, action):
        data = agent_backed_data(
            agent_behavior={
                "objective": "Draft a follow-up",
                "allowed_actions": ["read", action],
            },
            approval_policy={"human_required": False},
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "human_required" in str(exc_info.value)

    def test_agent_backed_graph_write_requires_human_approval(self):
        data = agent_backed_data(
            graph_scope={"allow_write": True},
            approval_policy={"human_required": False},
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "human_required" in str(exc_info.value)

    def test_agent_backed_external_action_scope_requires_human_approval(self):
        data = agent_backed_data(
            connector_scope={"allow_external_action": True},
            approval_policy={"human_required": False},
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "human_required" in str(exc_info.value)

    def test_agent_backed_approval_policy_can_allow_higher_risk_path(self):
        data = agent_backed_data(
            graph_scope={"allow_write": True},
            approval_policy={"human_required": True, "approval_reason": "graph write"},
        )
        assert validate_component_contract(data).agent_backed is not None

    def test_agent_backed_rejects_invalid_permission_policy_action(self):
        data = agent_backed_data(
            permission_policy={
                "required_permissions": ["graph:read"],
                "allowed_actions": ["read", "teleport"],
            }
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        assert "agent_backed.permission_policy.allowed_actions[1]" in str(exc_info.value)

    def test_agent_backed_rejects_invalid_scope_shapes(self):
        data = agent_backed_data(
            graph_scope={"node_types": ["contact", ""], "allow_write": "yes"},
            connector_scope={"connector_ids": "crm", "allow_external_action": "no"},
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        error_msg = str(exc_info.value)
        assert "agent_backed.graph_scope.node_types[1]" in error_msg
        assert "agent_backed.graph_scope.allow_write" in error_msg
        assert "agent_backed.connector_scope.connector_ids" in error_msg
        assert "agent_backed.connector_scope.allow_external_action" in error_msg

    def test_agent_backed_rejects_invalid_requirement_shapes(self):
        data = agent_backed_data(
            provenance_requirements={"require_sources": "yes"},
            audit_requirements={"required": "yes", "event_types": ["started", ""]},
        )
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_component_contract(data)
        error_msg = str(exc_info.value)
        assert "agent_backed.provenance_requirements.require_sources" in error_msg
        assert "agent_backed.audit_requirements.required" in error_msg
        assert "agent_backed.audit_requirements.event_types[1]" in error_msg


class TestComponentContractModel:
    def test_default_validation_result(self):
        contract = ComponentContract(
            contract_version="1.0.0",
            component_id="id",
            display_name="Name",
            family=ComponentFamily.DATA,
            version="1.0.0",
        )
        assert contract.validation.is_valid is True
        assert contract.validation.errors == []
        assert contract.validation.warnings == []

    def test_mutable_inputs(self):
        contract = ComponentContract(
            contract_version="1.0.0",
            component_id="id",
            display_name="Name",
            family=ComponentFamily.LOGIC,
            version="1.0.0",
        )
        contract.inputs.append(ComponentInput(name="x", description="y"))
        assert len(contract.inputs) == 1

    def test_provenance_behavior_default(self):
        contract = ComponentContract(
            contract_version="1.0.0",
            component_id="id",
            display_name="Name",
            family=ComponentFamily.UI,
            version="1.0.0",
        )
        assert contract.provenance_behavior == ProvenanceBehavior.PASSTHROUGH

    def test_validation_result_mutable(self):
        result = ValidationResult(is_valid=False, errors=["err1"])
        result.errors.append("err2")
        assert result.errors == ["err1", "err2"]

    def test_agent_run_record_valid(self):
        record = validate_agent_run_record(
            {
                "run_id": "run-1",
                "component_id": "meeting-brief",
                "status": "approval_required",
                "objective": "Draft a meeting brief",
                "approval_required": True,
                "audit_event_ids": ["event-1"],
            }
        )
        assert isinstance(record, AgentRunRecord)
        assert record.status == AgentRunStatus.APPROVAL_REQUIRED
        assert record.approval_required is True
        assert record.audit_event_ids == ["event-1"]

    def test_agent_run_record_rejects_invalid_status(self):
        with pytest.raises(ComponentContractValidationError) as exc_info:
            validate_agent_run_record(
                {"run_id": "run-1", "component_id": "component-1", "status": "running"}
            )
        assert "status" in str(exc_info.value)
        assert "must be one of" in str(exc_info.value)


class TestComponentFamily:
    def test_all_values_present(self):
        assert len(ComponentFamily) == 5
        values = {f.value for f in ComponentFamily}
        assert values == {"ui", "data", "logic", "integration", "utility"}


class TestProvenanceBehavior:
    def test_all_values_present(self):
        assert len(ProvenanceBehavior) == 4
        values = {f.value for f in ProvenanceBehavior}
        assert values == {"passthrough", "track", "sign", "verify"}
