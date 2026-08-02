"""Transport ownership, shutdown and production-router lifecycle regressions."""

from __future__ import annotations

import os
import socket
import tempfile
import time
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.service.lifecycle import (
    ReadinessRequirements,
    ServiceState,
)
from omnivia_core_runtime.service.main import _router_for, main as service_main
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ServiceFacts
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
    TransportError,
)

from omnivia_core.contracts.v1 import RequestEnvelope, SuccessResponseEnvelope

OBSERVED_AT = "2026-08-02T00:00:00Z"


@pytest.fixture
def socket_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ovl-") as directory:
        yield Path(directory) / "s.sock"


class RecordingDispatcher:
    def __init__(self) -> None:
        self.seen: list[RequestEnvelope] = []

    def dispatch(self, request: RequestEnvelope) -> SuccessResponseEnvelope:
        from omnivia_core_runtime.service.operations import success

        self.seen.append(request)
        response = success(request, {"ok": True})
        assert isinstance(response, SuccessResponseEnvelope)
        return response


class ProbeFactsRunner:
    def probe_facts(self) -> ServiceFacts:
        return ServiceFacts(
            observed_at=OBSERVED_AT,
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
        )


def _request_payload() -> dict[str, object]:
    return {
        "input": {},
        "metadata": {
            "api_version": "1.2",
            "client": {"id": "test-client", "version": "1.0.0"},
            "correlation_id": "corr-1",
            "purpose": "test",
            "request_id": "req-1",
            "required_capabilities": [],
            "scopes": [],
            "trace_id": "trace-1",
            "workspace_id": "workspace-1",
        },
        "operation": "memory.get",
    }


def _request() -> RequestEnvelope:
    return RequestEnvelope.from_wire(_request_payload())


def _assert_transport_error_is_non_disclosing(
    error: TransportError, secret: str
) -> None:
    rendered = "".join(
        (
            str(error),
            repr(error.args),
            repr(error.__cause__),
            repr(error.__context__),
            "".join(traceback.format_exception(error)),
        )
    )
    assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_malformed_pipe_name_diagnostic_is_fixed_and_non_disclosing() -> None:
    secret = "credential-hunter2"

    with pytest.raises(TransportError) as caught:
        LocalEndpoint(EndpointScheme.PIPE, f"../{secret}")

    assert str(caught.value) == "named-pipe endpoint name is invalid"
    _assert_transport_error_is_non_disclosing(caught.value, secret)


def test_cli_endpoint_parse_refusal_does_not_disclose_the_supplied_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "credential-hunter2"

    result = service_main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--installation-state",
            str(tmp_path / "installation"),
            "--endpoint",
            f"pipe://../{secret}",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == "refusing to serve: local service endpoint is invalid\n"
    assert secret not in captured.err


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_absent_endpoint_diagnostic_hides_path_and_exception_chain(
    socket_path: Path,
) -> None:
    secret = "credential-hunter2"
    missing = socket_path.with_name(f"{secret}-missing.sock")

    with pytest.raises(TransportError) as caught:
        LocalSocketTransport(path=missing).call(_request())

    _assert_transport_error_is_non_disclosing(caught.value, secret)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_connect_diagnostic_hides_path_and_raw_os_exception_chain(
    socket_path: Path,
) -> None:
    secret = "credential-hunter2"
    refused = socket_path.with_name(f"{secret}-refused.sock")
    abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abandoned.bind(str(refused))
    abandoned.close()
    try:
        with pytest.raises(TransportError) as caught:
            LocalSocketTransport(path=refused).call(_request())
    finally:
        refused.unlink()

    _assert_transport_error_is_non_disclosing(caught.value, secret)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_overlong_endpoint_diagnostics_are_fixed_and_non_disclosing(
    socket_path: Path,
) -> None:
    secret = "credential-hunter2"
    overlong = socket_path.with_name(f"{secret}-" + "x" * 120)

    operations = (
        lambda: LocalSocketTransport(path=overlong).call(_request()),
        lambda: LocalSocketServer(router=_router_for(ProbeFactsRunner(), RecordingDispatcher()), endpoint=LocalEndpoint(EndpointScheme.UNIX, str(overlong))).start(),  # type: ignore[arg-type]
    )
    for operation in operations:
        with pytest.raises(TransportError) as caught:
            operation()
        assert str(caught.value) == "local service endpoint path is too long"
        _assert_transport_error_is_non_disclosing(caught.value, secret)


def test_call_diagnostic_hides_raw_os_error_and_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.transport as module

    secret = "credential=hunter2 /Users/alice/private.sock"

    class FailingChannel:
        def send_frame(self, payload: bytes) -> None:
            del payload
            raise OSError(f"raw transport failure at {secret}")

        def read_frame(self, *, limit: int = 0) -> bytes | None:
            del limit
            raise AssertionError("send_frame must fail first")

        def close(self) -> None:
            pass

    monkeypatch.setattr(module, "_connect", lambda endpoint, timeout: FailingChannel())
    endpoint = LocalEndpoint(EndpointScheme.UNIX, f"/{secret}")

    with pytest.raises(TransportError) as caught:
        LocalSocketTransport(endpoint=endpoint).call(_request())

    _assert_transport_error_is_non_disclosing(caught.value, secret)


def test_call_close_diagnostic_hides_raw_os_error_and_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.transport as module

    secret = "credential=hunter2 /Users/alice/private.sock"

    class FailingCloseChannel:
        def send_frame(self, payload: bytes) -> None:
            del payload

        def read_frame(self, *, limit: int = 0) -> bytes | None:
            del limit
            return None

        def close(self) -> None:
            raise OSError(f"raw transport close failure at {secret}")

    monkeypatch.setattr(
        module, "_connect", lambda endpoint, timeout: FailingCloseChannel()
    )
    endpoint = LocalEndpoint(EndpointScheme.UNIX, f"/{secret}")

    with pytest.raises(TransportError) as caught:
        LocalSocketTransport(endpoint=endpoint).call(_request())

    _assert_transport_error_is_non_disclosing(caught.value, secret)


def test_production_router_keeps_probes_outside_dispatch_and_applications_inside_it() -> (
    None
):
    dispatcher = RecordingDispatcher()
    router = _router_for(ProbeFactsRunner(), dispatcher)  # type: ignore[arg-type]

    probe = router.route({"probe": PROBE_HEALTH})
    assert probe.to_wire()["status"] == "pass"
    assert dispatcher.seen == []

    response = router.route(_request_payload())
    assert isinstance(response, SuccessResponseEnvelope)
    assert [request.operation for request in dispatcher.seen] == ["memory.get"]


def test_runner_projects_live_identity_and_endpoint_into_discovery_probe_facts(
    tmp_path: Path,
) -> None:
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=tmp_path / "workspace",
            installation_root=tmp_path / "installation",
            endpoint="unix:///tmp/omnivia-core.sock",
        ),
        clock=FakeClock(),
    )
    runner.workspace_id = "workspace-1"
    runner.generation = 7
    runner.workspace_format_ordinal = "1"
    runner.identity = ServiceInstanceIdentity(
        service_instance_id="service-instance-1",
        installation_id="installation-1",
        process=ProcessEvidence(
            pid=42,
            start_time="123.4",
            boot_id="boot-1",
            os_principal="local-user",
        ),
    )
    runner.lifecycle.transition_to(ServiceState.STARTING)
    runner.lifecycle.transition_to(ServiceState.RECOVERING)
    runner.lifecycle.publish_readiness(
        ReadinessRequirements(**dict.fromkeys(vars(ReadinessRequirements()), True))
    )

    facts = runner.probe_facts()
    assert facts.health_status == "pass"
    assert facts.readiness_status == "pass"
    assert facts.descriptor is not None
    assert facts.descriptor.endpoint_uri == "unix:///tmp/omnivia-core.sock"
    assert facts.descriptor.workspace_id == "workspace-1"
    assert facts.descriptor.service_instance_id == "service-instance-1"
    assert facts.descriptor.fencing_generation == 7
    assert facts.descriptor.protocol_version == "1.0"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_shutdown_closes_a_partial_client_promptly_and_is_idempotent(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=5.0,
    )
    server.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    client.connect(str(socket_path))
    client.sendall(b"OV")
    try:
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.0
        assert client.recv(1) == b""
        assert not socket_path.exists()
        server.stop()
    finally:
        client.close()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_cleanup_never_unlinks_a_replacement_endpoint_owned_by_another_instance(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )
    server.start()
    assert server._listener is not None
    real_close = server._listener.close
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def close_then_replace() -> None:
        real_close()
        socket_path.unlink()
        replacement.bind(str(socket_path))
        replacement.listen(1)

    server._listener.close = close_then_replace  # type: ignore[method-assign]
    try:
        server.stop()
        assert socket_path.is_socket(), (
            "one instance deleted another instance's endpoint"
        )
    finally:
        replacement.close()
        if os.path.lexists(socket_path):
            socket_path.unlink()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_one_response_closes_the_connection_and_no_second_request_is_served(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    ):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(str(socket_path))
        try:
            client.sendall(encode_frame({"probe": PROBE_HEALTH}))
            header = client.recv(8)
            assert len(header) == 8
            length = int.from_bytes(header[4:], "big")
            body = b""
            while len(body) < length:
                body += client.recv(length - len(body))
            assert decode_frame(header + body)["status"] == "pass"
            assert client.recv(1) == b""
            with pytest.raises((BrokenPipeError, ConnectionResetError, OSError)):
                client.sendall(encode_frame({"probe": PROBE_HEALTH}))
        finally:
            client.close()


def test_failed_start_never_claims_or_removes_a_regular_file(socket_path: Path) -> None:
    socket_path.write_text("owner-data", encoding="utf-8")
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )
    with pytest.raises(TransportError, match="not a socket"):
        server.start()
    server.stop()
    assert socket_path.read_text(encoding="utf-8") == "owner-data"
