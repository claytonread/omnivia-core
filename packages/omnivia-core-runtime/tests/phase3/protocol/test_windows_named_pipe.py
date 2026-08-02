"""Real raw Windows named-pipe OVC1 conformance."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest
from omnivia_core_runtime.service.ovc1 import MAGIC, decode_frame, encode_frame
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    EndpointProbe,
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    TransportError,
    probe_endpoint,
)
from omnivia_core_runtime.service.windows_pipe import (
    RawPipeChannel,
    WindowsPipeError,
    open_pipe_client,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"


def _router() -> DocumentRouter:
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
        dispatch=lambda request: (_ for _ in ()).throw(AssertionError(request)),
    )


def _endpoint() -> LocalEndpoint:
    return LocalEndpoint(EndpointScheme.PIPE, f"omnivia-ovc1-test-{os.getpid()}")


def test_windows_implementation_contains_no_python_private_pipe_framing() -> None:
    import omnivia_core_runtime.service.windows_pipe as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "multiprocessing" not in source
    assert "send_bytes" not in source
    assert "recv_bytes" not in source
    assert "CreateNamedPipeW" in source
    assert "ReadFile" in source
    assert "WriteFile" in source
    assert "get_last_error" in source


def test_pipe_partial_reads_share_one_absolute_header_and_body_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.windows_pipe as module

    request = encode_frame({"probe": PROBE_HEALTH})
    chunks = [request[:8], request[8:]]
    now = [0.0]

    def transfer(handle, buffer, count, timeout, *, write):  # type: ignore[no-untyped-def]
        del handle, timeout
        assert not write
        chunk = chunks.pop(0)
        assert len(chunk) <= count
        ctypes.memmove(buffer, chunk, len(chunk))
        now[0] = 0.15
        return len(chunk)

    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "_overlapped_transfer", transfer)
    channel = RawPipeChannel(handle=1, timeout=0.1)
    monkeypatch.setattr(channel, "_available", lambda: 0)

    with pytest.raises(WindowsPipeError, match="timed out"):
        channel.read_frame()


def test_pipe_unary_boundary_catches_delayed_trailing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.windows_pipe as module

    request = encode_frame({"probe": PROBE_HEALTH})
    chunks = [request[:8], request[8:]]
    now = [0.0]

    def transfer(handle, buffer, count, timeout, *, write):  # type: ignore[no-untyped-def]
        del handle, timeout
        assert not write
        chunk = chunks.pop(0)
        assert len(chunk) <= count
        ctypes.memmove(buffer, chunk, len(chunk))
        return len(chunk)

    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        module,
        "sleep",
        lambda delay: now.__setitem__(0, now[0] + delay),
        raising=False,
    )
    monkeypatch.setattr(module, "_overlapped_transfer", transfer)
    channel = RawPipeChannel(handle=1, timeout=0.5)
    monkeypatch.setattr(channel, "_available", lambda: int(now[0] >= 0.01))

    with pytest.raises(WindowsPipeError, match="trailing bytes"):
        channel.read_frame()


def test_pipe_unary_boundary_is_rechecked_after_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bytes arriving during router work are refused before the response write."""
    import omnivia_core_runtime.service.windows_pipe as module

    request = encode_frame({"probe": PROBE_HEALTH})
    chunks = [request[:8], request[8:]]
    now = [0.0]
    after_dispatch = [False]

    def transfer(handle, buffer, count, timeout, *, write):  # type: ignore[no-untyped-def]
        del handle, timeout
        assert not write
        chunk = chunks.pop(0)
        assert len(chunk) <= count
        ctypes.memmove(buffer, chunk, len(chunk))
        return len(chunk)

    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        module,
        "sleep",
        lambda delay: now.__setitem__(0, now[0] + delay),
        raising=False,
    )
    monkeypatch.setattr(module, "_overlapped_transfer", transfer)
    channel = RawPipeChannel(handle=1, timeout=0.5)
    monkeypatch.setattr(channel, "_available", lambda: int(after_dispatch[0]))

    assert channel.read_frame() == request
    after_dispatch[0] = True
    with pytest.raises(WindowsPipeError, match="trailing bytes"):
        channel.ensure_unary_boundary()


def test_server_response_close_avoids_discard_and_unbounded_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response drains on close: disconnect discards it and flush can block forever."""
    import omnivia_core_runtime.service.windows_pipe as module

    events: list[str] = []

    class Api:
        def DisconnectNamedPipe(self, handle: int) -> int:
            assert handle == 123
            events.append("disconnect")
            return 1

        def CloseHandle(self, handle: int) -> int:
            assert handle == 123
            events.append("close")
            return 1

    monkeypatch.setattr(module, "_API", Api())
    channel = RawPipeChannel(handle=123, timeout=1.0, server_side=True)
    channel._wrote = True

    channel.close()

    assert events == ["close"]


def test_pipe_client_retries_the_wait_create_race_when_instance_turns_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnivia_core_runtime.service.windows_pipe as module

    events: list[tuple[str, int]] = []
    assert module._INVALID_HANDLE_VALUE is not None
    handles = iter([int(module._INVALID_HANDLE_VALUE), 73])

    class FakeApi:
        @staticmethod
        def WaitNamedPipeW(address: str, milliseconds: int) -> bool:
            assert address == r"\\.\pipe\race"
            assert milliseconds > 0
            events.append(("wait", milliseconds))
            return True

        @staticmethod
        def CreateFileW(*args: object) -> int:
            del args
            handle = next(handles)
            events.append(("create", handle))
            return handle

        @staticmethod
        def CloseHandle(handle: int) -> bool:
            events.append(("close", handle))
            return True

    monkeypatch.setattr(module, "_API", FakeApi())
    monkeypatch.setattr(module, "_GET_LAST_ERROR", lambda: module._ERROR_PIPE_BUSY)

    channel = open_pipe_client(r"\\.\pipe\race", timeout=0.1)
    channel.close()

    assert [name for name, _ in events] == [
        "wait",
        "create",
        "wait",
        "create",
        "close",
    ]


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows named pipe")
def test_raw_pipe_partial_writes_start_with_ovc1_and_route_probe() -> None:
    endpoint = _endpoint()
    with LocalSocketServer(router=_router(), endpoint=endpoint, timeout=1.0):
        channel = open_pipe_client(endpoint.address, timeout=1.0)
        try:
            request = encode_frame({"probe": PROBE_HEALTH})
            for byte in request:
                channel.write(bytes((byte,)))
            response = channel.read_frame()
            assert response is not None
            assert response[:4] == MAGIC
            assert decode_frame(response)["status"] == "pass"
        finally:
            channel.close()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows named pipe")
def test_pipe_lifecycle_refuses_second_binder_then_stops_and_rebinds() -> None:
    endpoint = _endpoint()
    first = LocalSocketServer(router=_router(), endpoint=endpoint, timeout=1.0)
    first.start()
    try:
        assert probe_endpoint(endpoint) is EndpointProbe.ANSWERING
        contender = LocalSocketServer(router=_router(), endpoint=endpoint, timeout=1.0)
        with pytest.raises(TransportError):
            contender.start()
    finally:
        first.stop()

    assert probe_endpoint(endpoint) is EndpointProbe.REFUSED
    rebound = LocalSocketServer(router=_router(), endpoint=endpoint, timeout=1.0)
    rebound.start()
    rebound.stop()
