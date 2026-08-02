"""Raw Windows named-pipe byte I/O for the ADR-040 OVC1 transport.

This module deliberately uses the Win32 byte-stream API directly.  OVC1 bytes
are never wrapped in a language-runtime message format.  Calls use overlapped
I/O so read and write waits are bounded and shutdown can cancel an active call.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Final, Literal, cast

from omnivia_core_runtime.service.ovc1 import HEADER_BYTES, MAGIC, MAXIMUM_JSON_BYTES

_GENERIC_READ: Final = 0x80000000
_GENERIC_WRITE: Final = 0x40000000
_OPEN_EXISTING: Final = 3
_FILE_FLAG_OVERLAPPED: Final = 0x40000000
_FILE_FLAG_FIRST_PIPE_INSTANCE: Final = 0x00080000
_PIPE_ACCESS_DUPLEX: Final = 0x00000003
_PIPE_TYPE_BYTE: Final = 0x00000000
_PIPE_READMODE_BYTE: Final = 0x00000000
_PIPE_WAIT: Final = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS: Final = 0x00000008
_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_BROKEN_PIPE: Final = 109
_ERROR_SEM_TIMEOUT: Final = 121
_ERROR_NO_DATA: Final = 232
_ERROR_PIPE_BUSY: Final = 231
_ERROR_PIPE_CONNECTED: Final = 535
_ERROR_IO_PENDING: Final = 997
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 258
_INFINITE: Final = 0xFFFFFFFF
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_BUFFER_BYTES: Final = 64 * 1024
_UNARY_BOUNDARY_SECONDS: Final = 0.05
_UNARY_BOUNDARY_POLL_SECONDS: Final = 0.001
_GET_LAST_ERROR: Final = cast(
    Callable[[], int], getattr(ctypes, "get_last_error", lambda: 0)
)


class WindowsPipeError(OSError):
    """A named-pipe operation failed without exposing an address or payload."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _WinApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsPipeError(
                "raw Windows named pipes are unavailable on this platform"
            )
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
        self.CreateNamedPipeW = kernel32.CreateNamedPipeW
        self.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.CreateNamedPipeW.restype = wintypes.HANDLE
        self.ConnectNamedPipe = kernel32.ConnectNamedPipe
        self.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        self.ConnectNamedPipe.restype = wintypes.BOOL
        self.DisconnectNamedPipe = kernel32.DisconnectNamedPipe
        self.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        self.DisconnectNamedPipe.restype = wintypes.BOOL
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


_API: _WinApi | None = None


def _api() -> _WinApi:
    global _API
    if _API is None:
        _API = _WinApi()
    return _API


def _milliseconds(timeout: float) -> int:
    return max(1, min(int(timeout * 1000), _INFINITE - 1))


def _last_error() -> int:
    return _GET_LAST_ERROR()


def _close_handle(handle: int) -> None:
    if handle and handle != _INVALID_HANDLE_VALUE:
        _api().CloseHandle(handle)


def _overlapped_transfer(
    handle: int,
    buffer: ctypes.Array[ctypes.c_char],
    count: int,
    timeout: float,
    *,
    write: bool,
) -> int:
    api = _api()
    event = api.CreateEventW(None, True, False, None)
    if not event:
        raise WindowsPipeError("could not create a pipe I/O event", code=_last_error())
    operation = _OVERLAPPED()
    operation.hEvent = event
    transferred = wintypes.DWORD()
    call = api.WriteFile if write else api.ReadFile
    try:
        if not call(
            handle, buffer, count, ctypes.byref(transferred), ctypes.byref(operation)
        ):
            error = _last_error()
            if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                raise EOFError
            if error != _ERROR_IO_PENDING:
                raise WindowsPipeError("raw pipe I/O failed", code=error)
            waited = api.WaitForSingleObject(event, _milliseconds(timeout))
            if waited == _WAIT_TIMEOUT:
                api.CancelIoEx(handle, ctypes.byref(operation))
                api.WaitForSingleObject(event, _INFINITE)
                raise WindowsPipeError("raw pipe I/O timed out")
            if waited != _WAIT_OBJECT_0:
                raise WindowsPipeError("waiting for raw pipe I/O failed")
            if not api.GetOverlappedResult(
                handle, ctypes.byref(operation), ctypes.byref(transferred), False
            ):
                error = _last_error()
                if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                    raise EOFError
                raise WindowsPipeError("raw pipe I/O completion failed", code=error)
        return int(transferred.value)
    finally:
        _close_handle(event)


@dataclass
class RawPipeChannel:
    """One connected byte-mode named-pipe instance."""

    handle: int
    timeout: float
    server_side: bool = False
    _closed: bool = False

    def write(self, payload: bytes) -> None:
        """Write exactly these raw bytes, adding no prefix or delimiter."""
        view = memoryview(payload)
        offset = 0
        deadline = monotonic() + self.timeout
        while offset < len(view):
            remaining_time = deadline - monotonic()
            if remaining_time <= 0:
                raise WindowsPipeError("raw pipe write timed out")
            chunk = bytes(view[offset : offset + _BUFFER_BYTES])
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = _overlapped_transfer(
                self.handle, buffer, len(chunk), remaining_time, write=True
            )
            if written <= 0:
                raise WindowsPipeError("raw pipe write made no progress")
            offset += written

    def send_frame(self, payload: bytes) -> None:
        self.write(payload)

    def _read_exact(
        self,
        count: int,
        *,
        allow_empty: bool = False,
        deadline: float | None = None,
    ) -> bytes | None:
        chunks: list[bytes] = []
        remaining = count
        if deadline is None:
            deadline = monotonic() + self.timeout
        while remaining:
            remaining_time = deadline - monotonic()
            if remaining_time <= 0:
                raise WindowsPipeError("raw pipe read timed out")
            buffer = ctypes.create_string_buffer(min(remaining, _BUFFER_BYTES))
            try:
                received = _overlapped_transfer(
                    self.handle, buffer, len(buffer), remaining_time, write=False
                )
            except EOFError:
                if allow_empty and remaining == count:
                    return None
                raise WindowsPipeError(
                    "raw pipe closed with a truncated OVC1 frame"
                ) from None
            if received <= 0:
                if allow_empty and remaining == count:
                    return None
                raise WindowsPipeError("raw pipe closed with a truncated OVC1 frame")
            chunks.append(buffer.raw[:received])
            remaining -= received
        return b"".join(chunks)

    def _available(self) -> int:
        available = wintypes.DWORD()
        if not _api().PeekNamedPipe(
            self.handle, None, 0, None, ctypes.byref(available), None
        ):
            error = _last_error()
            if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                return 0
            raise WindowsPipeError("could not inspect raw pipe boundary", code=error)
        return int(available.value)

    def ensure_unary_boundary(self) -> None:
        """Require one bounded quiet window at the current exchange boundary."""
        deadline = monotonic() + _UNARY_BOUNDARY_SECONDS
        while True:
            if self._available():
                raise WindowsPipeError(
                    "raw pipe carries trailing bytes after one OVC1 frame"
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            sleep(min(_UNARY_BOUNDARY_POLL_SECONDS, remaining))

    def read_frame(self, *, limit: int = MAXIMUM_JSON_BYTES) -> bytes | None:
        deadline = monotonic() + self.timeout
        header = self._read_exact(HEADER_BYTES, allow_empty=True, deadline=deadline)
        if header is None:
            return None
        if header[: len(MAGIC)] != MAGIC:
            raise WindowsPipeError("raw pipe does not begin with the OVC1 magic")
        length = int.from_bytes(header[len(MAGIC) :], "big")
        if length == 0:
            raise WindowsPipeError("raw pipe declares a zero-byte OVC1 body")
        if length > limit:
            raise WindowsPipeError(
                f"raw pipe declares more than {limit} OVC1 body bytes"
            )
        body = self._read_exact(length, deadline=deadline)
        assert body is not None
        self.ensure_unary_boundary()
        return header + body

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.server_side:
            _api().DisconnectNamedPipe(self.handle)
        _close_handle(self.handle)


def _create_pipe(address: str, *, first: bool) -> int:
    access = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
    if first:
        access |= _FILE_FLAG_FIRST_PIPE_INSTANCE
    mode = (
        _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS
    )
    handle = _api().CreateNamedPipeW(
        address,
        access,
        mode,
        2,
        _BUFFER_BYTES,
        _BUFFER_BYTES,
        1000,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise WindowsPipeError(
            "could not create raw named-pipe endpoint", code=_last_error()
        )
    return int(handle)


def _connect_server(handle: int) -> None:
    api = _api()
    event = api.CreateEventW(None, True, False, None)
    if not event:
        raise WindowsPipeError(
            "could not create a pipe accept event", code=_last_error()
        )
    operation = _OVERLAPPED()
    operation.hEvent = event
    try:
        if api.ConnectNamedPipe(handle, ctypes.byref(operation)):
            return
        error = _last_error()
        if error == _ERROR_PIPE_CONNECTED:
            return
        if error != _ERROR_IO_PENDING:
            raise WindowsPipeError("raw named-pipe accept failed", code=error)
        if api.WaitForSingleObject(event, _INFINITE) != _WAIT_OBJECT_0:
            raise WindowsPipeError("waiting for raw named-pipe accept failed")
        transferred = wintypes.DWORD()
        if not api.GetOverlappedResult(
            handle, ctypes.byref(operation), ctypes.byref(transferred), False
        ):
            error = _last_error()
            if error not in (_ERROR_PIPE_CONNECTED,):
                raise WindowsPipeError(
                    "raw named-pipe accept completion failed", code=error
                )
    finally:
        _close_handle(event)


@dataclass
class RawPipeListener:
    """Exclusive first-instance listener with a pending raw byte-mode instance."""

    address: str
    timeout: float
    _pending: int
    _closed: bool = False

    @classmethod
    def open(cls, address: str, *, timeout: float) -> RawPipeListener:
        return cls(
            address=address, timeout=timeout, _pending=_create_pipe(address, first=True)
        )

    def accept(self) -> RawPipeChannel:
        _connect_server(self._pending)
        connected = self._pending
        try:
            self._pending = _create_pipe(self.address, first=False)
        except Exception:
            _close_handle(connected)
            raise
        return RawPipeChannel(connected, self.timeout, server_side=True)

    def wake(self) -> None:
        try:
            open_pipe_client(self.address, timeout=min(self.timeout, 0.25)).close()
        except WindowsPipeError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_handle(self._pending)


def open_pipe_listener(address: str, *, timeout: float) -> RawPipeListener:
    return RawPipeListener.open(address, timeout=timeout)


def open_pipe_client(address: str, *, timeout: float) -> RawPipeChannel:
    api = _api()
    if not api.WaitNamedPipeW(address, _milliseconds(timeout)):
        raise WindowsPipeError(
            "raw named-pipe endpoint is unavailable", code=_last_error()
        )
    handle = api.CreateFileW(
        address,
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OVERLAPPED,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise WindowsPipeError(
            "could not open raw named-pipe endpoint", code=_last_error()
        )
    return RawPipeChannel(int(handle), timeout)


def probe_pipe(
    address: str, *, timeout: float
) -> Literal["answering", "refused", "unknown"]:
    try:
        channel = open_pipe_client(address, timeout=timeout)
    except WindowsPipeError as error:
        if error.code == _ERROR_FILE_NOT_FOUND:
            return "refused"
        if error.code in (_ERROR_PIPE_BUSY, _ERROR_SEM_TIMEOUT):
            return "unknown"
        return "unknown"
    channel.close()
    return "answering"


__all__ = [
    "RawPipeChannel",
    "RawPipeListener",
    "WindowsPipeError",
    "open_pipe_client",
    "open_pipe_listener",
    "probe_pipe",
]
