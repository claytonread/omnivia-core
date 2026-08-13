"""Deterministic client-side Windows named-pipe transport tests.

The Win32 calls are replaced at the module boundary so their race, access mask,
partial transfer, cancellation and error mappings run on every platform.  The
real client/server exchange remains a Windows-hosted qualification test in the
Runtime suite; these tests do not pretend a fake kernel is that evidence.
"""

from __future__ import annotations

import ctypes
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import omnivia_core_client.windows_pipe as pipe
import pytest
from omnivia_core_client import (
    CancellationToken,
    ClientTransport,
    Deadline,
    DeadlineExceededError,
    LocalIpcTransport,
    OperationCancelledError,
    ProtocolError,
    TransportError,
    encode_frame,
    local_ipc,
    pipe_address_for,
)

from omnivia_core.contracts.v1 import ServiceProbeRequest

MANIFEST: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "ovc1-v1.json").read_text(
        encoding="utf-8"
    )
)


def _vector(vector_id: str) -> dict[str, object]:
    return next(
        vector["payload"] for vector in MANIFEST["vectors"] if vector["id"] == vector_id
    )


def _deadline(clock: Any = lambda: 0.0) -> Deadline:
    return Deadline.after(10.0, clock=clock)


@pytest.mark.parametrize(
    ("name", "native"),
    [
        ("a", r"\\.\pipe\a"),
        ("omnivia-core", r"\\.\pipe\omnivia-core"),
        ("Core_01.test", r"\\.\pipe\Core_01.test"),
        ("a" * 200, "\\\\.\\pipe\\" + "a" * 200),
    ],
)
def test_pipe_uri_maps_to_the_one_local_native_namespace(
    name: str, native: str
) -> None:
    assert pipe_address_for(f"pipe://{name}") == native


@pytest.mark.parametrize(
    "endpoint",
    [
        "pipe://",
        "pipe://a/child",
        "pipe://../escape",
        "pipe://a%2Fb",
        "pipe://a b",
        "pipe://a\n",
        "pipe://-leading",
        "pipe://trailing-",
        "pipe://" + "a" * 201,
        "unix:///tmp/core.sock",
        None,
    ],
)
def test_a_refused_pipe_uri_is_not_quoted(endpoint: object) -> None:
    with pytest.raises(TransportError) as caught:
        pipe_address_for(endpoint)  # type: ignore[arg-type]

    rendered = repr(caught.value)
    if isinstance(endpoint, str):
        assert endpoint not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_the_module_imports_without_loading_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipe, "_API", None)
    monkeypatch.setattr(pipe.os, "name", "posix")

    with pytest.raises(TransportError, match="unavailable") as caught:
        pipe.open_pipe_channel(
            "pipe://omnivia-core",
            deadline=_deadline(),
            cancellation=None,
            operation="service.health",
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


class _ConnectApi:
    def __init__(self, handles: Iterator[int], now: list[float]) -> None:
        self._handles = handles
        self.now = now
        self.waits: list[int] = []
        self.creates: list[tuple[int, int, int, int]] = []
        self.closed: list[int] = []

    def WaitNamedPipeW(self, address: str, milliseconds: int) -> int:
        assert address == r"\\.\pipe\race"
        self.waits.append(milliseconds)
        self.now[0] += 0.1
        return 1

    def CreateFileW(
        self,
        address: str,
        access: int,
        share: int,
        security: object,
        creation: int,
        flags: int,
        template: object,
    ) -> int:
        del address, security, template
        self.creates.append((access, share, creation, flags))
        self.now[0] += 0.1
        return next(self._handles)

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1


def test_wait_create_busy_race_retries_under_one_decreasing_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pipe._INVALID_HANDLE_VALUE is not None
    now = [0.0]
    api = _ConnectApi(iter([int(pipe._INVALID_HANDLE_VALUE), 73]), now)
    monkeypatch.setattr(pipe, "_API", api)
    monkeypatch.setattr(pipe, "_GET_LAST_ERROR", lambda: pipe._ERROR_PIPE_BUSY)

    channel = pipe.open_pipe_channel(
        "pipe://race",
        deadline=Deadline.after(1.0, clock=lambda: now[0]),
        cancellation=None,
        operation="workspace.inspect",
    )
    channel.close()

    assert len(api.waits) == 2
    assert api.waits[1] < api.waits[0]
    assert api.creates == [
        (
            pipe._GENERIC_READ | pipe._GENERIC_WRITE,
            0,
            pipe._OPEN_EXISTING,
            pipe._FILE_FLAG_OVERLAPPED,
        ),
        (
            pipe._GENERIC_READ | pipe._GENERIC_WRITE,
            0,
            pipe._OPEN_EXISTING,
            pipe._FILE_FLAG_OVERLAPPED,
        ),
    ]
    assert api.closed == [73]


def test_a_non_busy_open_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pipe._INVALID_HANDLE_VALUE is not None
    now = [0.0]
    api = _ConnectApi(iter([int(pipe._INVALID_HANDLE_VALUE)]), now)
    monkeypatch.setattr(pipe, "_API", api)
    monkeypatch.setattr(pipe, "_GET_LAST_ERROR", lambda: pipe._ERROR_FILE_NOT_FOUND)

    with pytest.raises(TransportError) as caught:
        pipe.open_pipe_channel(
            "pipe://race",
            deadline=Deadline.after(1.0, clock=lambda: now[0]),
            cancellation=None,
            operation="workspace.inspect",
        )

    assert len(api.waits) == 1
    assert len(api.creates) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_call_cancelled_before_dial_reaches_no_native_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = CancellationToken()
    token.cancel()
    monkeypatch.setattr(
        pipe,
        "_api",
        lambda: (_ for _ in ()).throw(AssertionError("native API reached")),
    )

    with pytest.raises(OperationCancelledError):
        pipe.open_pipe_channel(
            "pipe://omnivia-core",
            deadline=_deadline(),
            cancellation=token,
            operation="workspace.inspect",
        )


def test_partial_writes_make_forward_progress_under_the_same_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []
    deadlines: list[Deadline] = []

    def transfer(
        handle: int,
        buffer: ctypes.Array[ctypes.c_char],
        count: int,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
        write: bool,
    ) -> int:
        del handle, cancellation, operation
        assert write
        take = min(2, count)
        seen.append(buffer.raw[:take])
        deadlines.append(deadline)
        return take

    monkeypatch.setattr(pipe, "_transfer", transfer)
    deadline = _deadline()
    pipe.WindowsPipeChannel(73).write(
        b"abcdef",
        deadline=deadline,
        cancellation=None,
        operation="workspace.inspect",
    )

    assert b"".join(seen) == b"abcdef"
    assert deadlines == [deadline, deadline, deadline]


class _PeekApi:
    def __init__(self, available: int = 0) -> None:
        self.available = available

    def PeekNamedPipe(
        self,
        handle: int,
        buffer: object,
        size: int,
        read: object,
        available: object,
        left: object,
    ) -> int:
        del handle, buffer, size, read, left
        available._obj.value = self.available  # type: ignore[attr-defined]
        return 1


def _script_reads(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    *,
    available: int = 0,
) -> pipe.WindowsPipeChannel:
    cursor = [0]

    def transfer(
        handle: int,
        buffer: ctypes.Array[ctypes.c_char],
        count: int,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
        write: bool,
    ) -> int:
        del handle, deadline, cancellation, operation
        assert not write
        if cursor[0] == len(payload):
            return 0
        take = min(count, 3, len(payload) - cursor[0])
        chunk = payload[cursor[0] : cursor[0] + take]
        ctypes.memmove(buffer, chunk, take)
        cursor[0] += take
        return take

    now = [0.0]
    monkeypatch.setattr(pipe, "_transfer", transfer)
    monkeypatch.setattr(pipe, "_API", _PeekApi(available))
    monkeypatch.setattr(pipe, "monotonic", lambda: now[0])
    monkeypatch.setattr(pipe, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return pipe.WindowsPipeChannel(73)


def test_partial_reads_decode_the_exact_ovc1_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {"answer": "yes"}
    channel = _script_reads(monkeypatch, encode_frame(document))

    assert (
        channel.read_document(
            deadline=_deadline(), cancellation=None, operation="workspace.inspect"
        )
        == document
    )


@pytest.mark.parametrize(
    ("payload", "failure"),
    [
        (b"NOPE" + (1).to_bytes(4, "big") + b"x", ProtocolError),
        (
            pipe.MAGIC + (pipe.MAXIMUM_JSON_BYTES + 1).to_bytes(4, "big"),
            ProtocolError,
        ),
        (pipe.MAGIC + (4).to_bytes(4, "big") + b"{}", TransportError),
    ],
)
def test_malformed_or_truncated_pipe_frames_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    failure: type[Exception],
) -> None:
    channel = _script_reads(monkeypatch, payload)

    with pytest.raises(failure):
        channel.read_document(
            deadline=_deadline(), cancellation=None, operation="workspace.inspect"
        )


def test_trailing_bytes_after_one_pipe_frame_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _script_reads(monkeypatch, encode_frame({"answer": "yes"}), available=1)

    with pytest.raises(ProtocolError, match="trailing bytes"):
        channel.read_document(
            deadline=_deadline(), cancellation=None, operation="workspace.inspect"
        )


class _PendingApi:
    def __init__(self, *, waited: int, token: CancellationToken | None = None) -> None:
        self.waited = waited
        self.token = token
        self.cancelled = 0
        self.closed: list[int] = []

    def CreateEventW(self, *args: object) -> int:
        del args
        return 81

    def ReadFile(self, *args: object) -> int:
        del args
        return 0

    WriteFile = ReadFile

    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int:
        del handle
        if milliseconds != pipe._INFINITE and self.token is not None:
            self.token.cancel()
        return pipe._WAIT_OBJECT_0 if milliseconds == pipe._INFINITE else self.waited

    def CancelIoEx(self, handle: int, operation: object) -> int:
        del handle, operation
        self.cancelled += 1
        return 1

    def GetOverlappedResult(self, *args: object) -> int:
        del args
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1


def test_pending_read_timeout_is_a_deadline_failure_and_cancels_native_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _PendingApi(waited=pipe._WAIT_TIMEOUT)
    monkeypatch.setattr(pipe, "_API", api)
    monkeypatch.setattr(pipe, "_GET_LAST_ERROR", lambda: pipe._ERROR_IO_PENDING)

    with pytest.raises(DeadlineExceededError) as caught:
        pipe._transfer(
            73,
            ctypes.create_string_buffer(1),
            1,
            deadline=_deadline(),
            cancellation=None,
            operation="workspace.inspect",
            write=False,
        )

    assert api.cancelled == 1
    assert api.closed == [81]
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_cancellation_interrupts_pending_native_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = CancellationToken()
    api = _PendingApi(waited=pipe._WAIT_OBJECT_0, token=token)
    monkeypatch.setattr(pipe, "_API", api)
    monkeypatch.setattr(pipe, "_GET_LAST_ERROR", lambda: pipe._ERROR_IO_PENDING)

    with pytest.raises(OperationCancelledError):
        pipe._transfer(
            73,
            ctypes.create_string_buffer(1),
            1,
            deadline=_deadline(),
            cancellation=token,
            operation="workspace.inspect",
            write=False,
        )

    assert api.cancelled == 1
    assert api.closed == [81]


@pytest.mark.parametrize(
    ("write", "message"),
    [
        (True, "before it was sent"),
        (False, "mid-frame"),
    ],
)
def test_native_transfer_failure_keeps_the_unix_error_mapping(
    monkeypatch: pytest.MonkeyPatch, write: bool, message: str
) -> None:
    api = _PendingApi(waited=pipe._WAIT_OBJECT_0)
    monkeypatch.setattr(pipe, "_API", api)
    monkeypatch.setattr(pipe, "_GET_LAST_ERROR", lambda: 5)

    with pytest.raises(TransportError, match=message) as caught:
        pipe._transfer(
            73,
            ctypes.create_string_buffer(1),
            1,
            deadline=_deadline(),
            cancellation=None,
            operation="workspace.inspect",
            write=write,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


class _LocalChannel:
    def __init__(
        self, answer: dict[str, object], token: CancellationToken | None = None
    ):
        self.answer = answer
        self.token = token
        self.written = b""
        self.closed = False

    def write(self, payload: bytes, **kwargs: object) -> None:
        del kwargs
        self.written = payload
        if self.token is not None:
            self.token.cancel()

    def read_document(self, **kwargs: object) -> dict[str, object]:
        cancellation = kwargs["cancellation"]
        operation = kwargs["operation"]
        if isinstance(cancellation, CancellationToken):
            cancellation.raise_if_cancelled(operation=str(operation))
        return self.answer

    def close(self) -> None:
        self.closed = True


def test_public_local_transport_routes_pipe_through_the_shared_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _LocalChannel(_vector("probe.health.result"))
    monkeypatch.setattr(local_ipc, "open_pipe_channel", lambda *args, **kwargs: channel)
    transport = LocalIpcTransport("pipe://omnivia-core")

    result = transport.probe(
        ServiceProbeRequest(probe="service.health"), deadline=_deadline()
    )

    assert result.probe == "service.health"
    assert channel.written.startswith(pipe.MAGIC)
    assert channel.closed is True
    assert isinstance(transport, ClientTransport)


def test_pipe_cancellation_between_write_and_read_closes_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = CancellationToken()
    channel = _LocalChannel(_vector("probe.health.result"), token)
    monkeypatch.setattr(local_ipc, "open_pipe_channel", lambda *args, **kwargs: channel)

    with pytest.raises(OperationCancelledError):
        LocalIpcTransport("pipe://omnivia-core").probe(
            ServiceProbeRequest(probe="service.health"),
            deadline=_deadline(),
            cancellation=token,
        )

    assert channel.closed is True
