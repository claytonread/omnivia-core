"""C21 trigger parity: the MCP call path and the CLI dispatch path answer alike.

Each surface is handed the same canned runtime envelope over a transport that
records what it was asked. For every trigger operation the MCP result's
`structured_content` must equal the result the CLI prints, both surfaces must
have sent the same operation and input, and an error envelope must surface as
the same error code on both. This file imports the CLI; no MCP runtime module
does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import _mcp_v06_3_fixture as fixture
import pytest
from mcp import types
from omnivia_core_client import Deadline, NegotiatedEndpoint, ServiceClient
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import parse_configuration

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ApiError,
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    GrantedAuthority,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    ServiceEndpointDescriptor,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
)

WORKSPACE = "ws-trigger-parity-01"
PRINCIPAL = "trigger-parity-principal"

#: The four trigger operations, each with the MCP tool it is exposed as and the
#: CLI command path that reaches it.
TOOL_BY_OPERATION = {
    "trigger.declare": "trigger_declare",
    "trigger.lifecycle": "trigger_lifecycle",
    "trigger.ingest": "trigger_ingest",
    "trigger.health": "trigger_health",
}
COMMAND_BY_OPERATION = {
    "trigger.declare": ("trigger", "declare"),
    "trigger.lifecycle": ("trigger", "lifecycle"),
    "trigger.ingest": ("trigger", "ingest"),
    "trigger.health": ("trigger", "health"),
}
OPERATIONS = tuple(TOOL_BY_OPERATION)

#: The same input document for both surfaces. The three mutations carry one
#: idempotency key each; the read carries none.
PAYLOADS: dict[str, dict[str, Any]] = {
    "trigger.declare": {
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        "trigger_id": "trigger-1",
        "trigger_kind": "webhook",
        "workflow_version": "1.0.0",
        "plan_hash": "sha256:" + "a" * 64,
        "event_type": "invoice.received",
        "event_contract_digest": "sha256:" + "c" * 64,
        "configuration_digest": "sha256:" + "d" * 64,
        "subscription_state": "active",
        "subscription_reason": "subscription.created",
    },
    "trigger.lifecycle": {
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        "trigger_id": "trigger-1",
        "subscription_state": "paused",
        "reason": "operator.paused",
    },
    "trigger.ingest": {
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        "trigger_id": "trigger-1",
        "event_id": "event-1",
        "event_idempotency_key": "key-1",
        "event_type": "invoice.received",
        "envelope_digest": "sha256:" + "b" * 64,
        "occurred_at": "2026-10-04T02:59:00.000000Z",
    },
    "trigger.health": {"project_id": "project-1", "workflow_id": "workflow-1", "limit": 1},
}
IDEMPOTENCY_KEYS = {
    "trigger.declare": "parity-declare-1",
    "trigger.lifecycle": "parity-lifecycle-1",
    "trigger.ingest": "parity-ingest-1",
}

#: One canned runtime success result per operation, in the shape its contract
#: answers with.
SUCCESS_RESULTS: dict[str, dict[str, Any]] = {
    "trigger.declare": {
        "trigger_id": "trigger-1",
        "declaration_sequence": 1,
        "subscription_state": "active",
        "subscription_sequence": 1,
        "declared_at": "2026-10-04T03:00:00.000000Z",
    },
    "trigger.lifecycle": {
        "trigger_id": "trigger-1",
        "subscription_state": "paused",
        "subscription_sequence": 2,
        "reason": "operator.paused",
        "observed_at": "2026-10-04T03:05:00.000000Z",
    },
    "trigger.ingest": {
        "trigger_id": "trigger-1",
        "trigger_observation_id": "obs-1",
        "observation_sequence": 1,
        "delivery_status": "accepted",
        "processing": "unlinked",
        "uncertainty": ["processing_unlinked"],
        "observed_at": "2026-10-04T03:10:00.000000Z",
    },
    "trigger.health": {
        "items": [
            {
                "trigger_id": "trigger-1",
                "trigger_kind": "webhook",
                "workflow_version": "1.0.0",
                "declaration_sequence": 1,
                "event_type": "invoice.received",
                "subscription": {
                    "state": "active",
                    "reason": "subscription.created",
                    "observed_at": "2026-10-04T03:00:00.000000Z",
                    "subscription_sequence": 1,
                },
                "observation_total": 0,
                "observations": [],
                "delivery_counts": {
                    "accepted": 0,
                    "duplicate": 0,
                    "dead_lettered": 0,
                    "uncertain": 0,
                },
                "failures": [],
                "uncertainty": [],
            }
        ],
        "page": {"continuation_token": "trigger-1"},
    },
}

#: One canned error per operation, with a code its contract allows for it.
ERRORS: dict[str, tuple[str, str]] = {
    "trigger.declare": (
        "idempotency_conflict",
        "This idempotency key was already used for a different request.",
    ),
    "trigger.lifecycle": (
        "idempotency_conflict",
        "This idempotency key was already used for a different request.",
    ),
    "trigger.ingest": (
        "idempotency_conflict",
        "This idempotency key was already used for a different request.",
    ),
    "trigger.health": ("not_found", "No such trigger in this Project and Workflow."),
}


class Recording:
    """A transport that answers with the envelope `answer` builds, and keeps the requests."""

    def __init__(self, answer: Callable[[RequestEnvelope], ResponseEnvelope]) -> None:
        self.answer = answer
        self.calls: list[RequestEnvelope] = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: Any = None,
    ) -> ResponseEnvelope:
        self.calls.append(request)
        return self.answer(request)

    def probe(self, request: Any, *, deadline: Deadline, cancellation: Any = None) -> Any:
        raise AssertionError("nothing in these call paths probes")


def descriptor() -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor(
        descriptor_version=CONTRACT_VERSION,
        workspace_id=WORKSPACE,
        service_instance_id="svc-trigger-parity",
        installation_id="inst-trigger-parity",
        endpoint_uri="unix:///tmp/omnivia-trigger-parity/s.sock",
        protocol_version="1.0",
        server_version="0.1.0",
        supported_api_versions=VersionWindow(minimum="1.0", maximum=CONTRACT_VERSION),
        supported_workspace_versions=VersionWindow(minimum="1", maximum="1"),
        workspace_format_version="1",
        ready=True,
        lifecycle_state="ready",
        fencing_generation=1,
        published_at="2026-01-01T00:00:00Z",
    )


def client(transport: Recording) -> ServiceClient:
    """The shared client both surfaces use, around the transport this test watches."""
    return ServiceClient(
        transport=transport,
        descriptor=descriptor(),
        negotiated=NegotiatedEndpoint(
            api_version=CONTRACT_VERSION,
            protocol_version="1.0",
            descriptor_version=CONTRACT_VERSION,
        ),
    )


def metadata(request: RequestEnvelope) -> ResponseMetadata:
    """The metadata a service puts on any answer, correlated to this request."""
    refs = (CapabilityRef(id="trigger.read", version="1.0"),)
    return ResponseMetadata(
        version=VersionCapabilityEnvelope(
            api_version=CONTRACT_VERSION,
            server_version="0.1.0",
            workspace_format_version="1",
            compatibility=CompatibilityMetadata(
                selected_api_version=CONTRACT_VERSION,
                selected_workspace_version="1",
                supported_api_versions=VersionWindow(
                    minimum="1.0", maximum=CONTRACT_VERSION
                ),
                supported_workspace_versions=VersionWindow(minimum="1", maximum="1"),
                status="compatible",
                upgrade_state=UpgradeState(value="none"),
                deprecations=(),
            ),
            capabilities=CapabilitySet(supported=refs, granted=refs, effective=refs),
        ),
        authority=GrantedAuthority(principal_id=PRINCIPAL, roles=(), capabilities=refs),
        request_id=request.metadata.request_id,
        correlation_id=request.metadata.correlation_id,
    )


def answer_success(operation: str) -> Callable[[RequestEnvelope], ResponseEnvelope]:
    def answer(request: RequestEnvelope) -> ResponseEnvelope:
        result = json.loads(json.dumps(SUCCESS_RESULTS[operation]))
        return SuccessResponseEnvelope(metadata=metadata(request), result=result)

    return answer


def answer_error(operation: str) -> Callable[[RequestEnvelope], ResponseEnvelope]:
    code, message = ERRORS[operation]

    def answer(request: RequestEnvelope) -> ResponseEnvelope:
        return ErrorResponseEnvelope(
            metadata=metadata(request),
            error=ApiError(code=code, message=message, retry_class="non_retryable"),
        )

    return answer


def mcp_session(transport: Recording) -> server.ConnectedSession:
    """An authoring session, as `connect` freezes one, over the recording transport."""
    configuration = parse_configuration(
        {
            "format": "omnivia.mcp-config.v1",
            "principal_id": PRINCIPAL,
            "allowed_workspace_ids": [WORKSPACE],
            "allowed_purposes": [
                "trigger_observation",
                "trigger_configuration",
                "trigger_ingestion",
            ],
            "mutation_enabled": True,
            "service_mode": "service_client",
            "endpoint": "https://core.example.com",
            "credential_reference": "core-api",
        }
    )
    return server.ConnectedSession(
        configuration=configuration,
        client=client(transport),
        workspace_id=WORKSPACE,
        status="attached",
        credentials=None,
        profile="authoring",
    )


def mcp_call(
    operation: str, answer: Callable[[RequestEnvelope], ResponseEnvelope]
) -> tuple[types.CallToolResult, Recording]:
    """One call through the MCP call path, shaped the way a model would send it."""
    transport = Recording(answer)
    arguments: dict[str, Any] = dict(PAYLOADS[operation])
    if operation in IDEMPOTENCY_KEYS:
        arguments = {"input": arguments, "idempotency_key": IDEMPOTENCY_KEYS[operation]}
    result = server._call_tool(
        types.CallToolRequestParams(
            name=TOOL_BY_OPERATION[operation], arguments=arguments
        ),
        session=mcp_session(transport),
    )
    return result, transport


def cli_call(
    operation: str, answer: Callable[[RequestEnvelope], ResponseEnvelope]
) -> tuple[ResponseEnvelope, Recording]:
    """One call through the CLI's own dispatch helper, over the same transport shape."""
    transport = Recording(answer)
    response = fixture.cli_dispatch(
        client(transport),
        COMMAND_BY_OPERATION[operation],
        payload=PAYLOADS[operation],
        idempotency_key=IDEMPOTENCY_KEYS.get(operation),
    )
    return response, transport


def cli_report(response: ResponseEnvelope, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """What the CLI prints for `response`: its status, stdout and stderr."""
    return fixture.cli_report(response, capsys)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_one_success_envelope_gives_the_same_result_and_request_on_both_surfaces(
    operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result, mcp_transport = mcp_call(operation, answer_success(operation))
    response, cli_transport = cli_call(operation, answer_success(operation))
    status, out, err = cli_report(response, capsys)

    assert result.is_error is False, result.content[0].text
    assert status == 0 and err == ""
    cli_payload = json.loads(out)
    assert result.structured_content == cli_payload == SUCCESS_RESULTS[operation]

    (mcp_request,) = mcp_transport.calls
    (cli_request,) = cli_transport.calls
    assert mcp_request.operation == cli_request.operation == operation
    assert mcp_request.input == cli_request.input == PAYLOADS[operation]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_one_error_envelope_is_reported_with_the_same_code_on_both_surfaces(
    operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = ERRORS[operation]
    result, _ = mcp_call(operation, answer_error(operation))
    response, _ = cli_call(operation, answer_error(operation))
    status, out, err = cli_report(response, capsys)

    assert result.is_error is True and result.structured_content is None
    mcp_code = json.loads(
        result.content[0].text.split("was refused by the service: ", 1)[1]
    )["error"]["code"]
    cli_code = err.split(":", 1)[0]
    assert out == ""
    assert mcp_code == cli_code == code
    assert status == fixture.cli_exit_code(code)
