"""Raw Unix-socket OVC1 transport conformance and router integration."""

from __future__ import annotations

import errno
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_runtime.service.operations import success
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    MAGIC,
    decode_frame,
    encode_frame,
)
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    UNARY_BOUNDARY_SECONDS,
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
)

from omnivia_core.contracts.v1 import (
    RequestEnvelope,
    ServiceProbeResult,
    SuccessResponseEnvelope,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"


@pytest.fixture
def socket_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ovc-") as directory:
        yield Path(directory) / "s.sock"


class ApplicationDispatch:
    def __init__(self) -> None:
        self.seen: list[RequestEnvelope] = []

    def __call__(self, request: RequestEnvelope) -> SuccessResponseEnvelope:
        self.seen.append(request)
        response = success(request, {"accepted": True})
        assert isinstance(response, SuccessResponseEnvelope)
        return response


def _router(dispatch: ApplicationDispatch | None = None) -> DocumentRouter:
    return DocumentRouter(
        probes=ProbeRouter(
            facts=lambda: ServiceFacts(
                observed_at=OBSERVED_AT,
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            ),
            capabilities=tuple,
            clock=lambda: 0,
        ),
        dispatch=dispatch or ApplicationDispatch(),
    )


def _connect(path: Path, *, timeout: float = 2.0) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(path))
    return client


def _read_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    while count:
        chunk = connection.recv(count)
        if not chunk:
            raise AssertionError("connection closed before the complete frame")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _receive_frame(connection: socket.socket) -> bytes:
    header = _read_exact(connection, HEADER_BYTES)
    length = int.from_bytes(header[4:], "big")
    return header + _read_exact(connection, length)


def _assert_no_valid_response(connection: socket.socket) -> None:
    try:
        response = connection.recv(1)
    except ConnectionResetError:
        # Linux may reset when the server closes with unread inbound bytes; this is
        # the same protocol outcome as EOF: no valid OVC1 response was emitted.
        return
    assert response == b"", "invalid traffic received a valid OVC1 response"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_partial_raw_unix_reads_route_a_probe_and_response_starts_with_ovc1(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(router=_router(), endpoint=endpoint, timeout=1.0):
        client = _connect(socket_path)
        try:
            request = encode_frame({"probe": PROBE_HEALTH, "request_id": "req-probe-1"})
            for byte in request:
                client.sendall(bytes((byte,)))
            raw = _receive_frame(client)
            assert raw[:4] == MAGIC
            result = ServiceProbeResult.from_wire(decode_frame(raw))
            assert result.probe == PROBE_HEALTH
            assert result.status == "pass"
        finally:
            client.close()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_application_requests_use_the_existing_document_router_and_dispatcher(
    socket_path: Path,
) -> None:
    dispatch = ApplicationDispatch()
    payload = {
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
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(router=_router(dispatch), endpoint=endpoint):
        client = _connect(socket_path)
        try:
            client.sendall(encode_frame(payload))
            response = SuccessResponseEnvelope.from_wire(
                decode_frame(_receive_frame(client))
            )
        finally:
            client.close()

    assert response.result == {"accepted": True}
    assert [request.operation for request in dispatch.seen] == ["memory.get"]


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
@pytest.mark.parametrize(
    "bad",
    [
        b'{"probe":"service.health"}\n',
        b"OVC1\x00\x00\x00\x02{",
        encode_frame({"probe": PROBE_HEALTH}) + b"\n",
        encode_frame({"probe": PROBE_HEALTH}) * 2,
    ],
)
def test_newline_truncated_trailing_and_pipelined_traffic_get_no_response(
    socket_path: Path, bad: bytes
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(router=_router(), endpoint=endpoint, timeout=0.15):
        client = _connect(socket_path)
        try:
            client.sendall(bad)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as error:
                if error.errno != errno.ENOTCONN:
                    raise
            _assert_no_valid_response(client)
        finally:
            client.close()

        healthy = _connect(socket_path)
        try:
            healthy.sendall(encode_frame({"probe": PROBE_HEALTH}))
            assert decode_frame(_receive_frame(healthy))["status"] == "pass"
        finally:
            healthy.close()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
@pytest.mark.parametrize("repetition", range(10))
def test_second_frame_sent_during_dispatch_gets_no_valid_response(
    socket_path: Path, repetition: int
) -> None:
    """The pre-dispatch quiet window is not the unary exchange boundary.

    The second frame arrives after that window has completed but while routing is
    still in progress.  The server must check the stream again after routing and
    immediately before writing any response.
    """
    del repetition

    class DelayedRouter:
        def route(self, document: dict[str, object]):
            time.sleep(0.15)
            return _router().route(document)

    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(
        router=DelayedRouter(),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=0.5,
    ):
        client = _connect(socket_path, timeout=1.0)
        request = encode_frame({"probe": PROBE_HEALTH})
        try:
            client.sendall(request)
            time.sleep(0.07)
            client.sendall(request)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as error:
                if error.errno != errno.ENOTCONN:
                    raise
            _assert_no_valid_response(client)
        finally:
            client.close()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_slowloris_partial_header_is_bounded_and_does_not_block_shutdown_or_next_call(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    server = LocalSocketServer(router=_router(), endpoint=endpoint, timeout=0.1)
    server.start()
    slow = _connect(socket_path)
    slow.sendall(b"OV")
    try:
        time.sleep(0.15)
        _assert_no_valid_response(slow)

        healthy = _connect(socket_path)
        try:
            healthy.sendall(encode_frame({"probe": PROBE_HEALTH}))
            assert decode_frame(_receive_frame(healthy))["status"] == "pass"
        finally:
            healthy.close()
    finally:
        slow.close()
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.0


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_partial_reads_share_one_absolute_header_and_body_deadline(
    socket_path: Path,
) -> None:
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(router=_router(), endpoint=endpoint, timeout=0.12):
        client = _connect(socket_path, timeout=1.0)
        request = encode_frame({"probe": PROBE_HEALTH})
        try:
            client.sendall(request[:HEADER_BYTES])
            time.sleep(0.09)
            client.sendall(request[HEADER_BYTES : HEADER_BYTES + 1])
            time.sleep(0.06)
            try:
                client.sendall(request[HEADER_BYTES + 1 :])
            except (BrokenPipeError, ConnectionResetError):
                pass
            _assert_no_valid_response(client)
        finally:
            client.close()


class _LargeResult:
    def __init__(self, size: int) -> None:
        self._size = size

    def to_wire(self) -> dict[str, object]:
        return {"data": "a" * self._size}


class _LargeResponseRouter:
    """Answers every request with a response too large to fit in a socket buffer."""

    def __init__(self, size: int) -> None:
        self._size = size

    def route(self, document: dict[str, object]) -> _LargeResult:
        del document
        return _LargeResult(self._size)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_response_write_is_bound_by_the_configured_timeout_not_the_unary_boundary(
    socket_path: Path,
) -> None:
    """The response write must not inherit the 50ms unary-boundary quiet window.

    `_handle` rechecks `ensure_unary_boundary` immediately before writing the
    response, and that check sets a 50ms socket timeout of its own. A response
    too large to fit in the kernel's socket buffers, to a reader that stalls
    partway through, proves whether that 50ms leaked onto the write: the pause
    starts only once the header has actually arrived -- proof the write is under
    way -- so this does not race against however long routing and encoding the
    large payload itself take. A pause comfortably longer than
    `UNARY_BOUNDARY_SECONDS` but comfortably shorter than the server's
    configured timeout must still let the write complete.
    """
    response_bytes = 3 * 1024 * 1024  # far larger than any default socket buffer
    stall_seconds = UNARY_BOUNDARY_SECONDS * 4
    configured_timeout = 2.0
    assert stall_seconds < configured_timeout

    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    with LocalSocketServer(
        router=_LargeResponseRouter(response_bytes),  # type: ignore[arg-type]
        endpoint=endpoint,
        timeout=configured_timeout,
    ):
        client = _connect(socket_path, timeout=5.0)
        try:
            # Shrink the receive buffer so the server's write cannot complete
            # inside the kernel's socket buffers regardless of host defaults.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
            client.sendall(encode_frame({"probe": PROBE_HEALTH}))
            header = _read_exact(client, HEADER_BYTES)
            length = int.from_bytes(header[len(MAGIC) :], "big")
            # The header is a tiny fraction of the response, so its arrival is
            # proof the write is in progress and still has almost everything
            # left to send. Stalling here, rather than after sending the
            # request, means the wait always lands mid-write regardless of how
            # long routing and canonicalizing the large payload took.
            time.sleep(stall_seconds)
            body = _read_exact(client, length)
        finally:
            client.close()

    assert decode_frame(header + body)["data"] == "a" * response_bytes
