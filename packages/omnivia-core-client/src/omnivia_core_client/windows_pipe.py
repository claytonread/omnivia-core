"""Raw Windows named-pipe client I/O for the OVC1 local transport.

The Runtime owns the listener.  This module is the dependency-independent
client half: Win32 byte-stream I/O, one absolute call deadline, and the same
OVC1 frame admitted by :mod:`omnivia_core_client.framing`.  It imports no
Runtime module and is safe to import on non-Windows hosts; the native API is
loaded only when a pipe is actually opened.

The client asks for both ``GENERIC_READ`` and ``GENERIC_WRITE``.  The accepted
Runtime pipe grants that duplex open to its creating user boundary, while a
read-only principal cannot invoke an operation.  Reaching the pipe is still not
claimed as cryptographic peer authentication.
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Final, NoReturn, Protocol, cast

from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import (
    DeadlineExceededError,
    OperationCancelledError,
    ProtocolError,
    TransportError,
)
from omnivia_core_client.framing import (
    HEADER_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    decode_frame,
)
from omnivia_core_client.transport import enforce_send_preconditions

__all__ = ["PIPE_SCHEME", "WindowsPipeChannel", "open_pipe_channel", "pipe_address_for"]

PIPE_SCHEME: Final = "pipe://"
_PIPE_NAME = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?\Z")
_PIPE_PREFIX: Final = "\\\\.\\pipe\\"

_GENERIC_READ: Final = 0x80000000
_GENERIC_WRITE: Final = 0x40000000
_OPEN_EXISTING: Final = 3
_FILE_FLAG_OVERLAPPED: Final = 0x40000000
_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_BROKEN_PIPE: Final = 109
_ERROR_SEM_TIMEOUT: Final = 121
_ERROR_NO_DATA: Final = 232
_ERROR_PIPE_BUSY: Final = 231
_ERROR_OPERATION_ABORTED: Final = 995
_ERROR_IO_PENDING: Final = 997
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 258
_INFINITE: Final = 0xFFFFFFFF
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_BUFFER_BYTES: Final = 64 * 1024
_UNARY_BOUNDARY_SECONDS: Final = 0.05


def pipe_address_for(endpoint_uri: str) -> str:
    """Return the native address for one accepted public ``pipe://`` URI."""
    if not isinstance(endpoint_uri, str) or not endpoint_uri.startswith(PIPE_SCHEME):
        raise TransportError("this transport requires an installation-local endpoint")
    name = endpoint_uri[len(PIPE_SCHEME) :]
    if _PIPE_NAME.fullmatch(name) is None:
        raise TransportError("the endpoint names no accepted local pipe")
    return _PIPE_PREFIX + name


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _Api(Protocol):
    def CloseHandle(self, handle: int) -> int: ...

    def CreateEventW(
        self, attributes: object, manual: bool, initial: bool, name: object
    ) -> int | None: ...

    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int: ...

    def CancelIoEx(self, handle: int, operation: object) -> int: ...

    def GetOverlappedResult(
        self, handle: int, operation: object, transferred: object, wait: bool
    ) -> int: ...

    def ReadFile(
        self,
        handle: int,
        buffer: object,
        count: int,
        transferred: object,
        operation: object,
    ) -> int: ...

    def WriteFile(
        self,
        handle: int,
        buffer: object,
        count: int,
        transferred: object,
        operation: object,
    ) -> int: ...

    def PeekNamedPipe(
        self,
        handle: int,
        buffer: object,
        size: int,
        read: object,
        available: object,
        left: object,
    ) -> int: ...

    def WaitNamedPipeW(self, address: str, milliseconds: int) -> int: ...

    def CreateFileW(
        self,
        address: str,
        access: int,
        share: int,
        security: object,
        creation: int,
        flags: int,
        template: object,
    ) -> int: ...


class _WinApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise TransportError("local named pipes are unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL
        self.CreateEventW = kernel32.CreateEventW
        self.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self.CreateEventW.restype = wintypes.HANDLE
        self.WaitForSingleObject = kernel32.WaitForSingleObject
        self.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.WaitForSingleObject.restype = wintypes.DWORD
        self.CancelIoEx = kernel32.CancelIoEx
        self.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        self.CancelIoEx.restype = wintypes.BOOL
        self.GetOverlappedResult = kernel32.GetOverlappedResult
        self.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        self.GetOverlappedResult.restype = wintypes.BOOL
        self.ReadFile = kernel32.ReadFile
        self.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        self.ReadFile.restype = wintypes.BOOL
        self.WriteFile = kernel32.WriteFile
        self.WriteFile.argtypes = list(self.ReadFile.argtypes)
        self.WriteFile.restype = wintypes.BOOL
        self.PeekNamedPipe = kernel32.PeekNamedPipe
        self.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.PeekNamedPipe.restype = wintypes.BOOL
        self.WaitNamedPipeW = kernel32.WaitNamedPipeW
        self.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self.WaitNamedPipeW.restype = wintypes.BOOL
        self.CreateFileW = kernel32.CreateFileW
        self.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.CreateFileW.restype = wintypes.HANDLE


_API: _Api | None = None
_GET_LAST_ERROR = cast(Callable[[], int], getattr(ctypes, "get_last_error", lambda: 0))


def _api() -> _Api:
    global _API
    if _API is None:
        _API = _WinApi()
    return _API


def _last_error() -> int:
    return _GET_LAST_ERROR()


def _noop() -> None:
    return None


def _milliseconds(seconds: float) -> int:
    return max(1, min(int(seconds * 1000), _INFINITE - 1))


def _close(handle: int) -> None:
    if handle and handle != _INVALID_HANDLE_VALUE:
        _api().CloseHandle(handle)


def _raise_connect_deadline() -> NoReturn:
    raise DeadlineExceededError(
        "the deadline passed while connecting to the local endpoint"
    )


def _raise_unreachable() -> NoReturn:
    raise TransportError("the local endpoint could not be reached")


def _raise_send_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while sending the request frame")


def _raise_read_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while reading the response frame")


def _raise_cancelled(operation: str) -> NoReturn:
    raise OperationCancelledError(f"{operation}: cancelled before it completed")


def _raise_dropped_before_send() -> NoReturn:
    raise TransportError("the local endpoint dropped the call before it was sent")


def _raise_dropped_mid_frame() -> NoReturn:
    raise TransportError("the local endpoint dropped the call mid-frame")


def _remaining(
    deadline: Deadline, cancellation: CancellationToken | None, operation: str
) -> float:
    return enforce_send_preconditions(
        deadline=deadline, cancellation=cancellation, operation=operation
    )


def _transfer(
    handle: int,
    buffer: ctypes.Array[ctypes.c_char],
    count: int,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
    operation: str,
    write: bool,
) -> int:
    _remaining(deadline, cancellation, operation)
    api = _api()
    event_result = api.CreateEventW(None, True, False, None)
    if not event_result:
        (_raise_dropped_before_send if write else _raise_dropped_mid_frame)()
    event = int(event_result)
    overlapped = _OVERLAPPED()
    overlapped.hEvent = event
    transferred = wintypes.DWORD()
    call = api.WriteFile if write else api.ReadFile
    outcome = "complete"
    unregister: Callable[[], None] = _noop
    try:
        if not call(
            handle,
            buffer,
            count,
            ctypes.byref(transferred),
            ctypes.byref(overlapped),
        ):
            error = _last_error()
            if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                outcome = "dropped"
            elif error != _ERROR_IO_PENDING:
                outcome = "failed"
            else:
                if cancellation is not None:

                    def abort() -> None:
                        api.CancelIoEx(handle, ctypes.byref(overlapped))

                    unregister = cancellation.register(abort)
                waited = int(
                    api.WaitForSingleObject(
                        event,
                        _milliseconds(_remaining(deadline, cancellation, operation)),
                    )
                )
                unregister()
                unregister = _noop
                if cancellation is not None and cancellation.cancelled:
                    outcome = "cancelled"
                elif waited == _WAIT_TIMEOUT:
                    api.CancelIoEx(handle, ctypes.byref(overlapped))
                    api.WaitForSingleObject(event, _INFINITE)
                    outcome = "deadline"
                elif waited != _WAIT_OBJECT_0:
                    outcome = "failed"
                elif not api.GetOverlappedResult(
                    handle,
                    ctypes.byref(overlapped),
                    ctypes.byref(transferred),
                    False,
                ):
                    error = _last_error()
                    if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                        outcome = "dropped"
                    elif (
                        error == _ERROR_OPERATION_ABORTED
                        and cancellation is not None
                        and cancellation.cancelled
                    ):
                        outcome = "cancelled"
                    else:
                        outcome = "failed"
    finally:
        unregister()
        _close(event)
    if outcome == "cancelled":
        _raise_cancelled(operation)
    if outcome == "deadline":
        (_raise_send_deadline if write else _raise_read_deadline)()
    if outcome == "dropped":
        (_raise_dropped_before_send if write else _raise_dropped_mid_frame)()
    if outcome == "failed":
        (_raise_dropped_before_send if write else _raise_dropped_mid_frame)()
    return int(transferred.value)


@dataclass(slots=True)
class WindowsPipeChannel:
    """One connected byte-mode named-pipe handle, used for one unary call."""

    handle: int
    _closed: bool = False

    def write(
        self,
        payload: bytes,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
    ) -> None:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + _BUFFER_BYTES]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = _transfer(
                self.handle,
                buffer,
                len(chunk),
                deadline=deadline,
                cancellation=cancellation,
                operation=operation,
                write=True,
            )
            if written <= 0:
                _raise_dropped_before_send()
            offset += written

    def _read_exact(
        self,
        count: int,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            buffer = ctypes.create_string_buffer(min(remaining, _BUFFER_BYTES))
            received = _transfer(
                self.handle,
                buffer,
                len(buffer),
                deadline=deadline,
                cancellation=cancellation,
                operation=operation,
                write=False,
            )
            if received <= 0:
                raise TransportError(
                    f"the local endpoint closed after {count - remaining} of {count} bytes"
                )
            chunks.append(buffer.raw[:received])
            remaining -= received
        return b"".join(chunks)

    def _ensure_unary_boundary(
        self,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
    ) -> None:
        window = min(
            _UNARY_BOUNDARY_SECONDS,
            _remaining(deadline, cancellation, operation),
        )
        end = monotonic() + window
        while True:
            available = wintypes.DWORD()
            if not _api().PeekNamedPipe(
                self.handle, None, 0, None, ctypes.byref(available), None
            ):
                if _last_error() in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                    return
                _raise_dropped_mid_frame()
            if int(available.value):
                raise ProtocolError(
                    "the response carries trailing bytes after one OVC1 frame"
                )
            remaining = end - monotonic()
            if remaining <= 0:
                return
            sleep(min(0.001, remaining))

    def read_document(
        self,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
    ) -> dict[str, object]:
        header = self._read_exact(
            HEADER_BYTES,
            deadline=deadline,
            cancellation=cancellation,
            operation=operation,
        )
        if header[: len(MAGIC)] != MAGIC:
            raise ProtocolError("the response does not begin with the OVC1 magic")
        length = int.from_bytes(header[len(MAGIC) :], "big")
        if length > MAXIMUM_JSON_BYTES:
            raise ProtocolError(
                f"the response declares {length} OVC1 body bytes, above the "
                f"{MAXIMUM_JSON_BYTES}-byte maximum"
            )
        body = self._read_exact(
            length,
            deadline=deadline,
            cancellation=cancellation,
            operation=operation,
        )
        document = decode_frame(header + body)
        self._ensure_unary_boundary(deadline, cancellation, operation)
        return document

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close(self.handle)


def open_pipe_channel(
    endpoint_uri: str,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
    operation: str,
) -> WindowsPipeChannel:
    """Open one duplex client handle, containing the wait/create busy race."""
    address = pipe_address_for(endpoint_uri)
    _remaining(deadline, cancellation, operation)
    api = _api()
    while True:
        remaining = _remaining(deadline, cancellation, operation)
        if not api.WaitNamedPipeW(address, _milliseconds(remaining)):
            error = _last_error()
            if error == _ERROR_SEM_TIMEOUT:
                _raise_connect_deadline()
            _raise_unreachable()
        _remaining(deadline, cancellation, operation)
        handle = int(
            api.CreateFileW(
                address,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_OVERLAPPED,
                None,
            )
        )
        if handle != _INVALID_HANDLE_VALUE:
            return WindowsPipeChannel(handle)
        if _last_error() != _ERROR_PIPE_BUSY:
            _raise_unreachable()
