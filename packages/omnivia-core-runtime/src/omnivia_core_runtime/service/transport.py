"""Transports over the one dispatcher (B9).

B9's exit condition is that in-process **and** service transports pass the same
conformance suite. One dispatcher with two transports in front of it is what makes
that testable: if the transports shared no implementation, "the same suite" would be
two suites that happen to agree today.

The wire format is the contract's canonical JSON, newline-delimited. A length-prefix
would be marginally faster and considerably harder to debug; newline-delimited JSON
can be read with `nc` while diagnosing a stuck service, which matters more for a
local IPC socket than throughput does.

No HTTP, no framework. ADR-036 keeps Core free of transport dependencies, and a Unix
domain socket needs none.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Self

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope, codec
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.service.dispatch import Dispatcher

#: Refuse a frame larger than this rather than buffering without limit. A local
#: client that sends an unbounded frame is malfunctioning, and treating it as
#: malfunctioning is safer than growing memory until something else fails.
MAX_FRAME_BYTES = 4 * 1024 * 1024

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Unix domain socket paths are bounded by `sockaddr_un.sun_path`: 104 bytes on
#: macOS and BSD, 108 on Linux. The lower bound is used so a workspace that works on
#: Linux is not silently unservable on macOS. This is an OS limit, not something the
#: runtime can engineer away, so it is reported clearly instead of surfacing as a
#: bare "AF_UNIX path too long".
MAX_SOCKET_PATH_BYTES = 104


class Transport(Protocol):
    """What every transport offers. Deliberately narrow."""

    def call(self, request: RequestEnvelope) -> ResponseEnvelope: ...


class TransportError(Exception):
    """A transport could not carry a request or a response."""


@dataclass
class InProcessTransport:
    """Direct dispatch for callers in the same process.

    `normalise` round-trips the response through the codec so this transport returns
    exactly what the socket transport returns. It defaults to on because skipping it
    produces a real divergence: a handler returning a Python list yields a list
    in-process and a tuple over the wire, so code written and tested in-process can
    break when the same call goes through a socket. B9's exit condition is that both
    transports pass the *same* suite, and identical value types are part of that.

    Setting it to False restores the zero-serialisation fast path, at the cost of
    accepting those type differences. It is an explicit choice rather than a silent
    default.
    """

    dispatcher: Dispatcher
    normalise: bool = True

    def call(self, request: RequestEnvelope) -> ResponseEnvelope:
        response = self.dispatcher.dispatch(request)
        if not self.normalise:
            return response
        return codec.decode_response(json.loads(codec.to_canonical_json(codec.encode_response(response))))


def assert_socket_path_fits(path: Path) -> None:
    """Refuse a socket path the OS cannot represent, with the numbers.

    A deeply nested workspace genuinely hits this, so the message names the limit and
    the actual length rather than leaving a caller to discover the cap by reading
    `man unix`.
    """
    encoded = len(str(path).encode("utf-8"))
    if encoded > MAX_SOCKET_PATH_BYTES:
        raise TransportError(
            f"socket path is {encoded} bytes, over the {MAX_SOCKET_PATH_BYTES}-byte "
            f"AF_UNIX limit: {path}. Place the socket under a shorter directory."
        )


def encode_frame(payload: dict[str, object]) -> bytes:
    return (codec.to_canonical_json(payload) + "\n").encode("utf-8")


def decode_frame(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise TransportError(f"frame is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise TransportError("frame must be a JSON object")
    return value


def _read_frame(connection: socket.socket, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
    """Read one newline-delimited frame, or None when the peer closes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            return b"".join(chunks) or None
        total += len(chunk)
        if total > limit:
            raise TransportError(f"frame exceeds {limit} bytes")
        chunks.append(chunk)
        if b"\n" in chunk:
            return b"".join(chunks).split(b"\n", 1)[0]


class EndpointProbe(str, Enum):
    """What a connect attempt established about an endpoint."""

    ANSWERING = "answering"
    #: Definitively nobody there: nothing is listening, or the path cannot be one.
    REFUSED = "refused"
    #: Could not be established -- a timeout, a permissions refusal, an unexpected
    #: error. Says nothing either way.
    UNKNOWN = "unknown"


def probe_endpoint(path: Path, *, timeout: float = 1.0) -> EndpointProbe:
    """Ask whether anything is listening on this socket right now.

    A connect is the only honest test: `is_socket()` reports what the inode is, not
    whether anyone is behind it, and a crashed service leaves a socket file identical
    to a live one.

    Three outcomes rather than two, because the two callers need opposite defaults on
    an ambiguous error and a boolean forced one of them to be wrong. Deleting a
    stranger's live endpoint is unrecoverable, so `LocalSocketServer.start()` may only
    unlink on `REFUSED`. Trusting a descriptor that answers nothing strands the
    workspace, so coordination may only reuse on `ANSWERING`. Collapsing both into
    "assume live" made a stale descriptor pointing at a regular file -- `ENOTSOCK` --
    read as a running service.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError, NotADirectoryError):
        return EndpointProbe.REFUSED
    except OSError as error:
        # ENOTSOCK: the path exists and is not a socket, so nothing can ever answer
        # on it. That is a fact, not an ambiguity.
        if error.errno == errno.ENOTSOCK:
            return EndpointProbe.REFUSED
        return EndpointProbe.UNKNOWN
    else:
        return EndpointProbe.ANSWERING
    finally:
        probe.close()


@dataclass
class LocalSocketServer:
    """A Unix domain socket in front of the dispatcher.

    The socket lives in the workspace's `locks/` directory rather than a temp path so
    it is discoverable next to the workspace it serves and removed with it. It is
    created with restrictive permissions: a local socket that any user can connect to
    would be an authorization bypass regardless of what the dispatcher checks.
    """

    dispatcher: Dispatcher
    path: Path
    _server: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event | None = None

    @property
    def endpoint(self) -> str:
        return f"unix://{self.path}"

    def start(self) -> str:
        if self._server is not None:
            raise TransportError("transport is already started")
        assert_socket_path_fits(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Probe, bind and rename happen under one lock on the endpoint name.
        # Without it two services reclaiming the same stale socket both see
        # `REFUSED`, both bind, and the second rename replaces the first's endpoint
        # after it went live -- the stranding the probe exists to prevent, moved a
        # few microseconds later. The lock makes the reclaim a single decision: the
        # loser re-probes, finds a live socket, and refuses.
        bind_lock = create_lock(
            self.path.with_name(f".{self.path.name}.bind"), LockRole.BOOTSTRAP_MUTEX
        )
        if not bind_lock.acquire():
            raise TransportError(
                f"refusing to bind the endpoint: another process is claiming "
                f"{self.path} right now."
            )
        try:
            return self._bind_and_serve()
        finally:
            bind_lock.release()

    def _bind_and_serve(self) -> str:
        if not self.path.is_socket() and os.path.lexists(self.path):
            # Anything that is not a socket is refused rather than removed. "It
            # carries no state" is true of a socket and of nothing else: pointed at
            # `locks/storage.lock`, unlinking destroyed the lifetime lock file while
            # its owner kept an flock on the now-unlinked inode, so once the socket
            # was cleaned up a successor created a fresh lock file and acquired it --
            # two live writers on one workspace.
            raise TransportError(
                f"refusing to bind the endpoint: {self.path} already exists and is "
                "not a socket. The endpoint must be a path this service owns."
            )
        if self.path.is_socket() and probe_endpoint(self.path) is not EndpointProbe.REFUSED:
            raise TransportError(
                f"refusing to bind the endpoint: {self.path} is a socket that is "
                "answering or cannot be shown to be dead, so replacing it could "
                "strand a live service."
            )

        # Bound on a private name and renamed into place. Unlinking the target and
        # then binding leaves a window in which the endpoint does not exist -- a
        # caller connecting there fails, and a racing service can bind the free name
        # and then be silently unlinked by this one. `rename` is atomic: the name
        # either refers to the previous socket or to this one, never to nothing.
        staging = self.path.with_name(f".{self.path.name}.{os.getpid()}.binding")
        if os.path.lexists(staging):
            staging.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(staging))
            staging.chmod(0o600)
            server.listen(8)
            os.rename(staging, self.path)
        except BaseException:
            server.close()
            if os.path.lexists(staging):
                staging.unlink()
            raise
        server.settimeout(0.2)

        self._server = server
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="omnivia-ipc", daemon=True)
        self._thread.start()
        return self.endpoint

    def _serve(self) -> None:
        assert self._server is not None
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    self._handle(connection)
                except Exception:  # noqa: BLE001, S112 - see below
                    # One bad client must not take the server down, and the set of
                    # ways a client can be bad is not enumerable from here.
                    # `TransportError, OSError` looked like the complete list and was
                    # not: `codec.decode_request` raises `ContractDecodeError`, so a
                    # single valid-JSON `{}` from any local client killed the sole
                    # accept loop and the service stayed advertised but deaf.
                    #
                    # The property that matters is per-connection containment, so it
                    # is written as that property rather than as a list of the
                    # failures thought of today. `BaseException` is deliberately not
                    # caught: `KeyboardInterrupt` and `SystemExit` are shutdown, not
                    # a bad frame.
                    #
                    # Not logged: this module has no logger, and a library that
                    # writes to stderr on every malformed frame hands any local
                    # client a way to fill the service's output. Containment is the
                    # contract; observability belongs to whoever runs the service.
                    continue

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(DEFAULT_TIMEOUT_SECONDS)
        raw = _read_frame(connection)
        if raw is None:
            return
        request = codec.decode_request(decode_frame(raw))
        response = self.dispatcher.dispatch(request)
        connection.sendall(encode_frame(codec.encode_response(response)))

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self.path.exists():
            self.path.unlink()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


@dataclass
class LocalSocketTransport:
    """Client side of the local socket.

    A fresh connection per call. Connection pooling would be an optimisation with a
    correctness cost here: a pooled socket outliving a service restart would deliver
    a request to a dead endpoint, and the client would have to distinguish that from a
    genuine failure.
    """

    path: Path
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def call(self, request: RequestEnvelope) -> ResponseEnvelope:
        assert_socket_path_fits(self.path)
        if not self.path.exists():
            raise TransportError(f"no service socket at {self.path}")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.path))
            connection.sendall(encode_frame(codec.encode_request(request)))
            raw = _read_frame(connection)
        except OSError as error:
            raise TransportError(f"transport failed: {error}") from error
        finally:
            connection.close()

        if raw is None:
            raise TransportError("service closed the connection without responding")
        return codec.decode_response(decode_frame(raw))


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_FRAME_BYTES",
    "MAX_SOCKET_PATH_BYTES",
    "InProcessTransport",
    "LocalSocketServer",
    "LocalSocketTransport",
    "Transport",
    "TransportError",
    "assert_socket_path_fits",
    "decode_frame",
    "encode_frame",
]
