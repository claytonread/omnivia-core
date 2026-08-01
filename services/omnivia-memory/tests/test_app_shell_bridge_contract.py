import pytest

from omnivia_memory.app_shell_bridge import (
    AppShellRuntimeState,
    AppShellSource,
    ValidationResult,
    AppShellHostContext,
    AppShellBodyDescriptor,
    AppShellBridgeValidationError,
    validate_app_shell_host_context,
    validate_app_shell_body_descriptor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def minimal_host_context(**overrides):
    """Return a minimal valid host context dict."""
    return {
        "app_id": "client-360",
        "app_name": "Client 360",
        "entity_label": "Acme Corp",
        "runtime_state": "ready",
        **overrides,
    }


def minimal_body_descriptor(**overrides):
    """Return a minimal valid body descriptor dict."""
    return {
        "app_id": "client-360",
        "body_id": "client-360-body",
        **overrides,
    }


# ---------------------------------------------------------------------------
# AppShellRuntimeState
# ---------------------------------------------------------------------------

class TestAppShellRuntimeState:
    def test_all_values_present(self):
        assert len(AppShellRuntimeState) == 7
        values = {s.value for s in AppShellRuntimeState}
        assert values == {
            "loading",
            "empty",
            "error",
            "missing-connector",
            "missing-permission",
            "ai-running",
            "ready",
        }


# ---------------------------------------------------------------------------
# Valid host context tests
# ---------------------------------------------------------------------------

class TestValidateHostContextValid:
    def test_minimal_valid(self):
        context = validate_app_shell_host_context(minimal_host_context())
        assert context.app_id == "client-360"
        assert context.app_name == "Client 360"
        assert context.entity_label == "Acme Corp"
        assert context.runtime_state == AppShellRuntimeState.READY
        assert context.permissions_summary == ""
        assert context.sources == []
        assert context.last_updated == ""
        assert context.host_command_ids == []
        assert context.validation.is_valid is True

    def test_runtime_state_variants(self):
        for value in (
            "loading",
            "empty",
            "error",
            "missing-connector",
            "missing-permission",
            "ai-running",
            "ready",
        ):
            context = validate_app_shell_host_context(
                minimal_host_context(runtime_state=value)
            )
            assert context.runtime_state == AppShellRuntimeState(value)

    def test_with_sources(self):
        context = validate_app_shell_host_context(
            minimal_host_context(
                sources=[
                    {"name": "crm", "description": "CRM connector"},
                    {"name": "billing"},
                ]
            )
        )
        assert len(context.sources) == 2
        assert context.sources[0].name == "crm"
        assert context.sources[0].description == "CRM connector"
        assert context.sources[1].name == "billing"
        assert context.sources[1].description == ""

    def test_all_fields_present(self):
        context = validate_app_shell_host_context(
            minimal_host_context(
                permissions_summary="2 sources permitted",
                sources=[{"name": "crm"}],
                last_updated="Updated 2 min ago",
                host_command_ids=["host.refresh", "host.sources", "host.permissions"],
            )
        )
        assert context.permissions_summary == "2 sources permitted"
        assert len(context.sources) == 1
        assert context.last_updated == "Updated 2 min ago"
        assert context.host_command_ids == [
            "host.refresh",
            "host.sources",
            "host.permissions",
        ]


# ---------------------------------------------------------------------------
# Invalid host context tests
# ---------------------------------------------------------------------------

class TestValidateHostContextInvalid:
    @pytest.mark.parametrize("field", ["app_id", "app_name", "entity_label"])
    def test_missing_required_field(self, field):
        data = minimal_host_context()
        data.pop(field)
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert f"'{field}' is required" in exc_info.value.args[0]

    @pytest.mark.parametrize("field", ["app_id", "app_name", "entity_label"])
    def test_empty_required_field(self, field):
        data = minimal_host_context(**{field: "   "})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert f"'{field}' must not be empty" in exc_info.value.args[0]

    @pytest.mark.parametrize("field", ["app_id", "app_name", "entity_label"])
    def test_wrong_type_required_field(self, field):
        data = minimal_host_context(**{field: 123})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert f"'{field}' must be a string" in exc_info.value.args[0]

    def test_missing_runtime_state(self):
        data = minimal_host_context()
        data.pop("runtime_state")
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert "'runtime_state' is required" in exc_info.value.args[0]

    @pytest.mark.parametrize("invalid_state", ["running", "draft", "disabled", "", 42])
    def test_invalid_runtime_state(self, invalid_state):
        data = minimal_host_context(runtime_state=invalid_state)
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        error_msg = str(exc_info.value)
        assert "runtime_state" in error_msg
        assert "must be one of" in error_msg

    @pytest.mark.parametrize("field", ["permissions_summary", "last_updated"])
    def test_wrong_type_optional_string(self, field):
        data = minimal_host_context(**{field: 42})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert f"'{field}' must be a string" in exc_info.value.args[0]

    def test_sources_not_list(self):
        data = minimal_host_context(sources="not-a-list")
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert "'sources' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_source",
        ["not-a-dict", 42, None, {"description": "has no name"}],
        ids=["string", "int", "None", "missing-name"],
    )
    def test_invalid_sources_item(self, invalid_source):
        data = minimal_host_context(sources=[invalid_source])
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert "sources[0]" in str(exc_info.value)

    def test_host_command_ids_not_list(self):
        data = minimal_host_context(host_command_ids="host.refresh")
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert "'host_command_ids' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize(
        "invalid_command",
        [42, None, "   "],
        ids=["int", "None", "blank"],
    )
    def test_invalid_host_command_ids_item(self, invalid_command):
        data = minimal_host_context(host_command_ids=[invalid_command])
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        assert "host_command_ids[0]" in str(exc_info.value)

    def test_multiple_errors_collected(self):
        data = {
            "app_id": "",
            "entity_label": 42,
            "runtime_state": "invalid",
            "sources": "bad",
        }
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_host_context(data)
        error_msg = str(exc_info.value)
        assert len(error_msg.split(";")) >= 4


# ---------------------------------------------------------------------------
# Valid body descriptor tests
# ---------------------------------------------------------------------------

class TestValidateBodyDescriptorValid:
    def test_minimal_valid(self):
        descriptor = validate_app_shell_body_descriptor(minimal_body_descriptor())
        assert descriptor.app_id == "client-360"
        assert descriptor.body_id == "client-360-body"
        assert descriptor.source_count == 0
        assert descriptor.sources == []
        assert descriptor.components == []
        assert descriptor.citations == []
        assert descriptor.degraded_component_ids == []
        assert descriptor.validation.is_valid is True

    def test_source_count_derived_from_sources(self):
        descriptor = validate_app_shell_body_descriptor(
            minimal_body_descriptor(sources=[{"name": "crm"}, {"name": "billing"}])
        )
        assert descriptor.source_count == 2
        assert descriptor.sources[0].name == "crm"
        assert descriptor.sources[1].name == "billing"

    def test_explicit_source_count_can_differ_from_connector_list(self):
        descriptor = validate_app_shell_body_descriptor(
            minimal_body_descriptor(
                source_count=9,
                sources=[
                    {"name": "Gmail"},
                    {"name": "Calendar"},
                    {"name": "Drive"},
                    {"name": "Invoices"},
                ],
            )
        )
        assert descriptor.source_count == 9
        assert [source.name for source in descriptor.sources] == [
            "Gmail",
            "Calendar",
            "Drive",
            "Invoices",
        ]

    def test_source_count_without_sources(self):
        descriptor = validate_app_shell_body_descriptor(
            minimal_body_descriptor(source_count=3)
        )
        assert descriptor.source_count == 3
        assert descriptor.sources == []

    def test_all_fields_present(self):
        descriptor = validate_app_shell_body_descriptor(
            minimal_body_descriptor(
                source_count=1,
                sources=[{"name": "crm", "description": "CRM connector"}],
                components=["summary-card", "timeline", "contact-list"],
                citations=["crm:account-record"],
                degraded_component_ids=["timeline"],
            )
        )
        assert descriptor.source_count == 1
        assert descriptor.components == ["summary-card", "timeline", "contact-list"]
        assert descriptor.citations == ["crm:account-record"]
        assert descriptor.degraded_component_ids == ["timeline"]


# ---------------------------------------------------------------------------
# Invalid body descriptor tests
# ---------------------------------------------------------------------------

class TestValidateBodyDescriptorInvalid:
    @pytest.mark.parametrize("field", ["app_id", "body_id"])
    def test_missing_required_field(self, field):
        data = minimal_body_descriptor()
        data.pop(field)
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert f"'{field}' is required" in exc_info.value.args[0]

    @pytest.mark.parametrize("field", ["app_id", "body_id"])
    def test_empty_required_field(self, field):
        data = minimal_body_descriptor(**{field: ""})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert f"'{field}' is required" in exc_info.value.args[0] or (
            f"'{field}' must not be empty" in exc_info.value.args[0]
        )

    @pytest.mark.parametrize(
        "invalid_count",
        ["3", 3.5, True],
        ids=["string", "float", "bool"],
    )
    def test_source_count_wrong_type(self, invalid_count):
        data = minimal_body_descriptor(source_count=invalid_count)
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert "'source_count' must be an integer" in str(exc_info.value)

    def test_source_count_negative(self):
        data = minimal_body_descriptor(source_count=-1)
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert "'source_count' must not be negative" in str(exc_info.value)

    def test_sources_not_list(self):
        data = minimal_body_descriptor(sources={"name": "crm"})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert "'sources' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize("field", ["components", "citations", "degraded_component_ids"])
    def test_string_list_not_list(self, field):
        data = minimal_body_descriptor(**{field: "not-a-list"})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert f"'{field}' must be a list" in str(exc_info.value)

    @pytest.mark.parametrize("field", ["components", "citations"])
    def test_string_list_invalid_item(self, field):
        data = minimal_body_descriptor(**{field: [42]})
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert f"'{field}[0]' must be a string" in str(exc_info.value)

    def test_degraded_component_ids_not_subset(self):
        data = minimal_body_descriptor(
            components=["summary-card"],
            degraded_component_ids=["timeline"],
        )
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        error_msg = str(exc_info.value)
        assert "'degraded_component_ids' must be a subset of 'components'" in error_msg
        assert "timeline" in error_msg

    def test_degraded_component_ids_without_components(self):
        data = minimal_body_descriptor(degraded_component_ids=["timeline"])
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        assert "'degraded_component_ids' must be a subset of 'components'" in str(
            exc_info.value
        )

    def test_multiple_errors_collected(self):
        data = {
            "app_id": "",
            "source_count": -2,
            "components": 42,
            "citations": [None],
        }
        with pytest.raises(AppShellBridgeValidationError) as exc_info:
            validate_app_shell_body_descriptor(data)
        error_msg = str(exc_info.value)
        assert len(error_msg.split(";")) >= 4


# ---------------------------------------------------------------------------
# Model instantiation tests
# ---------------------------------------------------------------------------

class TestAppShellHostContextModel:
    def test_default_validation_result(self):
        context = AppShellHostContext(
            app_id="client-360",
            app_name="Client 360",
            entity_label="Acme Corp",
            runtime_state=AppShellRuntimeState.LOADING,
        )
        assert context.validation.is_valid is True
        assert context.validation.errors == []
        assert context.validation.warnings == []

    def test_mutable_sources(self):
        context = AppShellHostContext(
            app_id="client-360",
            app_name="Client 360",
            entity_label="Acme Corp",
            runtime_state=AppShellRuntimeState.READY,
        )
        context.sources.append(AppShellSource(name="crm"))
        assert len(context.sources) == 1


class TestAppShellBodyDescriptorModel:
    def test_default_validation_result(self):
        descriptor = AppShellBodyDescriptor(
            app_id="client-360",
            body_id="client-360-body",
        )
        assert descriptor.source_count == 0
        assert descriptor.validation.is_valid is True

    def test_validation_result_mutable(self):
        result = ValidationResult(is_valid=False, errors=["err1"])
        result.errors.append("err2")
        assert result.errors == ["err1", "err2"]
