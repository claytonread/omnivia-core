"""Transport ownership, shutdown and production-router lifecycle regressions."""

from __future__ import annotations

import errno
import os
import socket
import tempfile
import threading
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from omnivia_core_runtime.service.main import _router_for
from omnivia_core_runtime.service.main import main as service_main
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    MAGIC,
    decode_frame,
    encode_frame,
)
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ServiceFacts
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    _MAX_CONSECUTIVE_ACCEPT_FAILURES,
    EndpointProbe,
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
    TransportError,
    probe_endpoint,
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
        lambda: LocalSocketServer(
            router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),
            endpoint=LocalEndpoint(EndpointScheme.UNIX, str(overlong)),
        ).start(),  # type: ignore[arg-type]
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


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
@pytest.mark.parametrize(
    "failure_site", ["path_inspection", "socket_create", "connect_close"]
)
def test_endpoint_open_failures_are_fixed_and_non_disclosing(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    import omnivia_core_runtime.service.transport as module

    secret = "hunter2"
    endpoint_path = socket_path.with_name(f"{secret}.sock")

    class FailingConnection:
        def settimeout(self, timeout: float) -> None:
            del timeout

        def connect(self, address: str) -> None:
            del address
            raise OSError(f"raw connect failure at {secret}")

        def close(self) -> None:
            raise OSError(f"raw close failure at {secret}")

    if failure_site == "path_inspection":
        monkeypatch.setattr(
            Path,
            "exists",
            lambda self: (_ for _ in ()).throw(
                OSError(f"raw path inspection failure at {secret}")
            ),
        )
    else:
        monkeypatch.setattr(Path, "exists", lambda self: True)
        if failure_site == "socket_create":
            monkeypatch.setattr(
                module.socket,
                "socket",
                lambda *args: (_ for _ in ()).throw(
                    OSError(f"raw socket creation failure at {secret}")
                ),
            )
        else:
            monkeypatch.setattr(
                module.socket, "socket", lambda *args: FailingConnection()
            )

    with pytest.raises(TransportError) as caught:
        LocalSocketTransport(path=endpoint_path).call(_request())

    assert str(caught.value) == "could not access local service endpoint"
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


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_malformed_server_response_is_translated_to_transport_error(
    socket_path: Path,
) -> None:
    """OVC1Error must not escape `call()`: it is not a `TransportError`.

    A well-framed but structurally inadmissible response -- here, non-canonical
    JSON -- makes `decode_frame` raise `OVC1Error` inside `call()`. That is not
    one of this module's own transport failures, and letting it escape
    unconverted would be a taxonomy change a caller catching `TransportError`
    would not expect. The fixed refusal must also carry nothing of the frame
    it refused.
    """
    secret = "credential-hunter2-payload-marker"
    # Well-framed (correct magic and declared length) but not canonical JSON:
    # RFC 8785 forbids the space after the colon, so `decode_frame` refuses it.
    malformed_body = f'{{"marker": "{secret}"}}'.encode()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    server.settimeout(5.0)

    def respond() -> None:
        connection, _ = server.accept()
        try:
            connection.settimeout(5.0)
            header = _recv_exact(connection, HEADER_BYTES)
            length = int.from_bytes(header[len(MAGIC) :], "big")
            _recv_exact(connection, length)
            connection.sendall(
                MAGIC + len(malformed_body).to_bytes(4, "big") + malformed_body
            )
        finally:
            connection.close()

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    try:
        with pytest.raises(TransportError) as caught:
            LocalSocketTransport(path=socket_path).call(_request())
    finally:
        thread.join(timeout=5.0)
        server.close()

    assert str(caught.value) == "service response was not a valid OVC1 frame"
    _assert_transport_error_is_non_disclosing(caught.value, secret)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
@pytest.mark.parametrize("failure_site", ["parent_mkdir", "bind_lock_create"])
def test_bind_path_and_lock_operation_failures_are_fixed_and_non_disclosing(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    import omnivia_core_runtime.service.transport as module

    secret = "hunter2"
    endpoint_path = socket_path.with_name(f"{secret}.sock")

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(f"raw {failure_site} failure at {secret}")

    if failure_site == "parent_mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    else:
        monkeypatch.setattr(module, "create_lock", fail)

    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=LocalEndpoint(EndpointScheme.UNIX, str(endpoint_path)),
    )
    with pytest.raises(TransportError) as caught:
        server.start()

    assert str(caught.value) == "local service transport start failed"
    _assert_transport_error_is_non_disclosing(caught.value, secret)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
@pytest.mark.parametrize("failure_site", ["bind", "listen", "rename"])
def test_raw_socket_start_failures_are_fixed_and_non_disclosing(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    import omnivia_core_runtime.service.transport as module

    secret = "hunter2"
    endpoint_path = socket_path.with_name(f"{secret}.sock")

    class FailingSocket:
        def bind(self, address: str) -> None:
            del address
            if failure_site == "bind":
                raise OSError(f"raw bind failure at {secret}")

        def listen(self, backlog: int) -> None:
            del backlog
            if failure_site == "listen":
                raise OSError(f"raw listen failure at {secret}")

        def close(self) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            del timeout

    monkeypatch.setattr(module.socket, "socket", lambda *args: FailingSocket())
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    if failure_site == "rename":
        monkeypatch.setattr(
            module.os,
            "rename",
            lambda source, target: (_ for _ in ()).throw(
                OSError(f"raw rename failure at {secret}")
            ),
        )

    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=LocalEndpoint(EndpointScheme.UNIX, str(endpoint_path)),
    )
    with pytest.raises(TransportError) as caught:
        server.start()

    assert str(caught.value) == "local service transport start failed"
    _assert_transport_error_is_non_disclosing(caught.value, secret)


def test_pipe_listener_failure_is_fixed_and_non_disclosing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.transport as module
    import omnivia_core_runtime.service.windows_pipe as pipe_module

    secret = "credential-hunter2"
    endpoint = LocalEndpoint(EndpointScheme.PIPE, f"omnivia-{secret}")
    monkeypatch.setattr(
        module,
        "probe_endpoint",
        lambda endpoint: module.EndpointProbe.REFUSED,
    )
    monkeypatch.setattr(
        pipe_module,
        "open_pipe_listener",
        lambda address, timeout: (_ for _ in ()).throw(
            pipe_module.WindowsPipeError(f"raw pipe listener failure at {secret}")
        ),
    )
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )

    with pytest.raises(TransportError) as caught:
        server.start()

    assert str(caught.value) == "local named-pipe listener start failed"
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


def test_runner_start_report_redacts_unexpected_transport_hook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "credential=hunter2 /Users/alice/private.sock [Errno 13]"
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=tmp_path / "workspace",
            installation_root=tmp_path / "installation",
            endpoint="unix:///tmp/omnivia-core.sock",
        )
    )

    def fail_start(*, serve: object | None = None) -> object:
        del serve
        return runner._start_transport(
            lambda started: (_ for _ in ()).throw(OSError(secret))
        )

    monkeypatch.setattr(runner, "_start", fail_start)
    report = runner.start()

    assert not report.ready
    assert report.reason == "local service transport start failed"
    assert secret not in repr(report.to_dict())


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
        try:
            closed = client.recv(1)
        except ConnectionResetError:
            closed = b""
        assert closed == b""
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
        assert not os.path.lexists(socket_path), (
            "the server closed its listener before unlinking its owned endpoint"
        )
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
def test_cleanup_failure_still_closes_listener_and_is_non_disclosing(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )
    server.start()
    secret = "credential=hunter2 /private/replacement.sock"

    def fail_cleanup() -> None:
        raise PermissionError(secret)

    monkeypatch.setattr(server, "_remove_socket_file", fail_cleanup)
    with pytest.raises(TransportError) as caught:
        server.stop()

    assert str(caught.value) == "local service transport cleanup failed"
    _assert_transport_error_is_non_disclosing(caught.value, secret)
    assert server._listener is None
    assert probe_endpoint(endpoint) is EndpointProbe.REFUSED


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
    secret = "hunter2"
    occupied = socket_path.with_name(secret)
    occupied.write_text("owner-data", encoding="utf-8")
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(occupied))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )
    with pytest.raises(TransportError) as caught:
        server.start()
    assert str(caught.value) == "local service endpoint path is already occupied"
    _assert_transport_error_is_non_disclosing(caught.value, secret)
    server.stop()
    assert occupied.read_text(encoding="utf-8") == "owner-data"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_live_endpoint_refusal_is_fixed_and_non_disclosing(socket_path: Path) -> None:
    secret = "hunter2"
    live_path = socket_path.with_name(f"{secret}.sock")
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(live_path))
    live = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
    )
    live.start()
    try:
        contender = LocalSocketServer(
            router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
            endpoint=endpoint,
        )
        with pytest.raises(TransportError) as caught:
            contender.start()
        assert str(caught.value) == "local service endpoint is already in use"
        _assert_transport_error_is_non_disclosing(caught.value, secret)
    finally:
        live.stop()


def _flaky_listener(server: LocalSocketServer, failures: int) -> threading.Event:
    """Make the running server's next `failures` accepts raise, then behave.

    `ECONNABORTED` because that is the real one: a peer that connected and went
    away again before the service got round to accepting it. The listener is
    untouched -- the same object serves the client below -- so what the tests
    either side of this exercise is the accept *loop*'s reaction and nothing else.
    """
    listener = server._listener
    assert listener is not None
    remaining = iter(range(failures))
    exhausted = threading.Event()
    real_accept = listener.accept

    def flaky_accept() -> Any:
        if next(remaining, None) is None:
            exhausted.set()
            return real_accept()
        raise OSError(errno.ECONNABORTED, "software caused connection abort")

    listener.accept = flaky_accept  # type: ignore[method-assign]
    return exhausted


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_a_transient_accept_failure_leaves_the_service_answering(
    socket_path: Path,
) -> None:
    """R004: an `accept()` that raises must not take the sole accept loop with it.

    The loop used to `break` on any `OSError`, which is not a shutdown: the thread
    ended while the process kept the workspace lease and the storage lock and kept
    advertising the endpoint as ready. Every later client then connected to a name
    nobody was accepting on and waited out its own timeout -- alive, ready,
    answering nobody, which is exactly what a hosted run reported as two MCP
    sessions that timed out against a service still running.

    So the assertion is not that the loop survived; it is that a *client* is
    served afterwards, over the same endpoint, by the same listener.
    """
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=5.0,
    )
    server.start()
    try:
        exhausted = _flaky_listener(server, failures=1)
        deadline = time.monotonic() + 5.0
        while not exhausted.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert exhausted.is_set(), "the accept loop stopped at the first failure"

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(str(socket_path))
        try:
            client.sendall(encode_frame({"probe": PROBE_HEALTH}))
            header = client.recv(HEADER_BYTES)
            assert len(header) == HEADER_BYTES
            body = b""
            length = int.from_bytes(header[len(MAGIC) :], "big")
            while len(body) < length:
                body += client.recv(length - len(body))
            assert decode_frame(header + body)["status"] == "pass"
        finally:
            client.close()
    finally:
        server.stop()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_a_listener_that_never_accepts_again_is_given_up_on_not_spun_on(
    socket_path: Path,
) -> None:
    """The other half of the retry: it is bounded.

    A descriptor that is genuinely gone fails every accept, so the retry above has
    to end somewhere -- otherwise the fix for a deaf service is a thread burning a
    core until the process exits. The bound is consecutive failures, and this pins
    it: exactly `_MAX_CONSECUTIVE_ACCEPT_FAILURES` attempts, then the thread ends.

    `stop()` still has to complete promptly on that already-ended thread, unlink
    the endpoint and raise nothing.
    """
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=5.0,
    )
    server.start()
    try:
        attempts = 0
        listener = server._listener
        assert listener is not None

        def always_fails() -> Any:
            nonlocal attempts
            attempts += 1
            raise OSError(errno.EBADF, "bad file descriptor")

        listener.accept = always_fails  # type: ignore[method-assign]
        thread = server._thread
        assert thread is not None
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "a dead listener was retried without bound"
        assert attempts == _MAX_CONSECUTIVE_ACCEPT_FAILURES
    finally:
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.0
    assert not socket_path.exists()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_shutdown_still_terminates_a_serve_thread_that_is_mid_retry(
    socket_path: Path,
) -> None:
    """A stop asked for between two retries is answered, not queued behind them.

    The retry pauses on the stop event rather than sleeping, and this is what that
    buys: `stop()` here lands inside the retry window -- the loop has failed once
    and is waiting to try again -- and the serving thread has to end on that
    signal, not after working through the rest of its attempts. A retry that
    slept would make every shutdown wait out a pause it has no reason to.
    """
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(
        router=_router_for(ProbeFactsRunner(), RecordingDispatcher()),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=5.0,
    )
    server.start()
    listener = server._listener
    assert listener is not None
    retrying = threading.Event()

    def always_fails() -> Any:
        retrying.set()
        raise OSError(errno.ECONNABORTED, "software caused connection abort")

    listener.accept = always_fails  # type: ignore[method-assign]
    thread = server._thread
    assert thread is not None
    assert retrying.wait(timeout=5.0), "the accept loop never reached a retry"

    server.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "shutdown did not reach a retrying accept loop"
    assert not socket_path.exists()
