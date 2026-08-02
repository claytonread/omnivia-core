"""In-process and ADR-040 raw local-IPC transports.

B9's exit condition is that in-process **and** service transports pass the same
conformance suite. One dispatcher with two transports in front of it is what makes
that testable: if the transports shared no implementation, "the same suite" would be
two suites that happen to agree today.

The advertised local endpoint carries one OVC1 request and one OVC1 response on a
fresh connection.  Unix sockets and Windows named pipes carry the eight-byte OVC1
header and canonical body as raw bytes: no newline delimiter and no Python-private
message prefix is part of the protocol.

No HTTP, no framework. ADR-036 keeps Core free of transport dependencies, and local
IPC needs none. Two mechanisms carry the same frames: a Unix domain socket on POSIX,
a Windows named pipe on Windows.

The named pipe is not decoration. CPython does not expose `socket.AF_UNIX` on
Windows at all, so every reference to it there raises `AttributeError` -- which took
the entire Phase 2 service suite down on the hosted Windows row rather than failing
one case. The mechanism is therefore chosen once, behind `LocalEndpoint`, instead of
being decided again at each call site; and the endpoint carries the scheme it
actually serves, so a descriptor can never advertise a named pipe as `unix://`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Protocol, Self

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope, codec
from omnivia_core_runtime.ownership.locks import IS_WINDOWS, LockRole, create_lock
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    decode_frame,
    encode_frame,
)
from omnivia_core_runtime.service.protocol import DocumentRouter

#: Refuse a frame larger than this rather than buffering without limit. A local
#: client that sends an unbounded frame is malfunctioning, and treating it as
#: malfunctioning is safer than growing memory until something else fails.
MAX_FRAME_BYTES = MAXIMUM_JSON_BYTES

DEFAULT_TIMEOUT_SECONDS = 10.0

# OVC1 is unary on a fresh connection.  A stream has no packet boundary, so an
# immediate non-blocking peek can only reject bytes already scheduled by the kernel;
# a second write a few milliseconds later would otherwise arrive after dispatch and
# receive a valid response.  Hold the response for one small, fixed quiet window so
# separately written trailing traffic is deterministically refused without changing
# OVC1 framing or waiting without a bound.
UNARY_BOUNDARY_SECONDS = 0.05

#: Unix domain socket paths are bounded by `sockaddr_un.sun_path`: 104 bytes on
#: macOS and BSD, 108 on Linux. The lower bound is used so a workspace that works on
#: Linux is not silently unservable on macOS. This is an OS limit, not something the
#: runtime can engineer away, so it is reported clearly instead of surfacing as a
#: bare "AF_UNIX path too long".
MAX_SOCKET_PATH_BYTES = 104

#: The local machine's named-pipe namespace. Written escaped rather than as a raw
#: string because a raw string cannot end in a backslash. The `.` is the local host
#: specifically: a `\\<host>\pipe\` address would be a pipe on another machine over
#: SMB, which this is not and must never silently become.
PIPE_ADDRESS_PREFIX = "\\\\.\\pipe\\"

PIPE_NAME_PREFIX = "omnivia-"

#: Half a SHA-256, which is ample to keep two workspaces on one machine apart and
#: holds the whole address to 49 characters -- far inside the 256-character cap on a
#: pipe name, and bounded however deep the workspace is.
PIPE_NAME_DIGEST_CHARS = 32

#: A pipe name is one component in a kernel namespace, so it is validated as one
#: rather than trusted. `..`, a backslash or a colon in an advertised endpoint would
#: address something other than a pipe -- `\\.\pipe\..\C:` is a device path -- so
#: anything outside this alphabet is refused instead of being handed to the OS.
_SAFE_PIPE_NAME = re.compile(r"\A[A-Za-z0-9._-]{1,200}\Z")

#: CPython exposes `AF_UNIX` only where the platform has it. Read once, so its
#: absence becomes a clear refusal instead of an `AttributeError` from inside a
#: connect.
_HAS_AF_UNIX = hasattr(socket, "AF_UNIX")


class Transport(Protocol):
    """What every transport offers. Deliberately narrow."""

    def call(self, request: RequestEnvelope) -> ResponseEnvelope: ...


class TransportError(Exception):
    """A transport could not carry a request or a response."""


@dataclass
class InProcessTransport:
    """Direct dispatch for callers in the same process.

    `normalise` round-trips the response through the codec so this transport returns
    exactly what the local IPC transport returns. It defaults to on because skipping
    it produces a real divergence: a handler returning a Python list yields a list
    in-process and a tuple over the wire, so code written and tested in-process can
    break when the same call goes through a socket or a pipe. B9's exit condition is
    that both transports pass the *same* suite, and identical value types are part of
    that.

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
        return codec.decode_response(
            json.loads(codec.to_canonical_json(codec.encode_response(response)))
        )


# --- endpoints ----------------------------------------------------------------


class EndpointScheme(str, Enum):
    """The local IPC mechanisms this runtime can address."""

    UNIX = "unix"
    PIPE = "pipe"


@dataclass(frozen=True)
class LocalEndpoint:
    """Where a local service listens, and by which mechanism.

    One value carries both halves because they cannot be separated safely. A bare
    path says nothing about how to reach it, and the code this replaces inferred the
    mechanism from the platform at every call site -- which is how a Windows host
    came to advertise `unix://` for something no Windows client can open.

    `name` is the scheme's own address form: the filesystem path of a Unix socket, or
    the single-component name of a named pipe.
    """

    scheme: EndpointScheme
    name: str

    def __post_init__(self) -> None:
        if self.scheme is EndpointScheme.PIPE and not _SAFE_PIPE_NAME.match(self.name):
            raise TransportError("named-pipe endpoint name is invalid")
        if self.scheme is EndpointScheme.UNIX and not self.name:
            raise TransportError("a unix endpoint needs a socket path")

    @property
    def url(self) -> str:
        """The advertised form. This is what a discovery descriptor carries."""
        return f"{self.scheme.value}://{self.name}"

    @property
    def address(self) -> str:
        """The address the OS takes."""
        if self.scheme is EndpointScheme.UNIX:
            return self.name
        return PIPE_ADDRESS_PREFIX + self.name

    @property
    def path(self) -> Path | None:
        """The filesystem path this endpoint occupies, if it occupies one.

        A named pipe occupies none: it lives in a kernel namespace, not on disk.
        Callers that clean up or inspect files have to be told that rather than
        assume every endpoint has a path.
        """
        if self.scheme is EndpointScheme.UNIX:
            return Path(self.name)
        return None


#: The mechanism this platform serves. Windows has no `AF_UNIX` in CPython, so this
#: is the platform's answer, not a preference.
LOCAL_SCHEME = EndpointScheme.PIPE if IS_WINDOWS else EndpointScheme.UNIX


def pipe_name_for_path(path: Path) -> str:
    """A deterministic, bounded pipe name for the endpoint a workspace asked for.

    The path is hashed rather than embedded. A pipe name is a single component in a
    kernel namespace: a raw `C:\\Users\\...\\locks\\s.sock` is not one, and folding it
    into one would either collide or produce an address the OS reads as something
    else. Hashing the case-normalised absolute path gives one name per endpoint,
    stable across processes -- a launcher and the service it spawned derive the same
    name from the same path -- and bounded however deep the workspace is.
    """
    key = os.path.normcase(os.path.abspath(str(path)))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:PIPE_NAME_DIGEST_CHARS]
    return f"{PIPE_NAME_PREFIX}{digest}"


def endpoint_for_path(path: Path) -> LocalEndpoint:
    """The endpoint this platform would serve for a caller that named a path.

    Callers that only know "the service for this workspace lives here" come through
    here; callers holding an advertised URL come through `parse_endpoint`.
    """
    if LOCAL_SCHEME is EndpointScheme.UNIX:
        return LocalEndpoint(EndpointScheme.UNIX, str(path))
    return LocalEndpoint(EndpointScheme.PIPE, pipe_name_for_path(path))


def names_a_local_endpoint(endpoint: str) -> bool:
    """Whether an advertised endpoint claims a local IPC scheme at all.

    Separate from `parse_endpoint` because the two failures need opposite handling.
    An endpoint naming no local scheme -- `in-process` -- is simply not probeable. An
    endpoint claiming one that this runtime refuses to address is a claim that cannot
    be believed, and treating the two alike would let a malformed `pipe://` URL be
    trusted on the strength of its pid alone.
    """
    return endpoint.startswith(tuple(f"{scheme.value}://" for scheme in EndpointScheme))


def parse_endpoint(endpoint: str) -> LocalEndpoint | None:
    """The endpoint an advertised URL names, or None when it names none this runtime
    can address."""
    scheme, separator, name = endpoint.partition("://")
    if not separator or not name:
        return None
    if scheme == EndpointScheme.UNIX.value:
        return LocalEndpoint(EndpointScheme.UNIX, name)
    if scheme == EndpointScheme.PIPE.value and _SAFE_PIPE_NAME.match(name):
        return LocalEndpoint(EndpointScheme.PIPE, name)
    return None


def assert_socket_path_fits(path: Path) -> None:
    """Refuse a socket path the OS cannot represent, with the numbers.

    A deeply nested workspace genuinely hits this, so the message names the limit and
    the actual length rather than leaving a caller to discover the cap by reading
    `man unix`. Unix domain sockets only: a pipe name is derived and bounded by
    construction, so there is no equivalent cap to hit.
    """
    encoded = len(str(path).encode("utf-8"))
    if encoded > MAX_SOCKET_PATH_BYTES:
        raise TransportError(
            f"socket path is {encoded} bytes, over the {MAX_SOCKET_PATH_BYTES}-byte "
            "AF_UNIX limit. Place the socket under a shorter directory."
        )


class _Channel(Protocol):
    """One accepted or dialled connection, framed.

    Narrow on purpose: the dispatcher side reads a frame and writes a frame, and
    nothing above this line knows whether the bytes crossed a socket or a pipe.
    """

    def read_frame(self, *, limit: int = MAX_FRAME_BYTES) -> bytes | None: ...

    def ensure_unary_boundary(self) -> None: ...

    def send_frame(self, payload: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass
class _SocketChannel:
    """Raw OVC1 bytes over one stream connection."""

    connection: socket.socket

    def read_frame(self, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
        timeout = self.connection.gettimeout()
        deadline = None if timeout is None else monotonic() + timeout
        try:
            header = self._read_exact(HEADER_BYTES, allow_empty=True, deadline=deadline)
            if header is None:
                return None
            if header[: len(MAGIC)] != MAGIC:
                raise TransportError("connection does not begin with the OVC1 magic")
            length = int.from_bytes(header[len(MAGIC) :], "big")
            if length == 0:
                raise TransportError("connection declares a zero-byte OVC1 body")
            if length > limit:
                raise TransportError(
                    f"connection declares more than {limit} OVC1 body bytes"
                )
            body = self._read_exact(length, deadline=deadline)
            assert body is not None
            self.ensure_unary_boundary()
            return header + body
        finally:
            self.connection.settimeout(timeout)

    def _read_exact(
        self,
        count: int,
        *,
        allow_empty: bool = False,
        deadline: float | None = None,
    ) -> bytes | None:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            if deadline is not None:
                remaining_time = deadline - monotonic()
                if remaining_time <= 0:
                    raise TransportError(
                        "connection timed out while reading an OVC1 frame"
                    )
                self.connection.settimeout(remaining_time)
            try:
                chunk = self.connection.recv(remaining)
            except TimeoutError:
                chunk = None
            if chunk is None:
                raise TransportError("connection timed out while reading an OVC1 frame")
            if not chunk:
                if allow_empty and remaining == count:
                    return None
                raise TransportError("connection closed with a truncated OVC1 frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def ensure_unary_boundary(self) -> None:
        """Require one bounded quiet window at the current exchange boundary."""
        self.connection.settimeout(UNARY_BOUNDARY_SECONDS)
        try:
            trailing = self.connection.recv(1, socket.MSG_PEEK)
        except TimeoutError:
            return
        if trailing:
            raise TransportError(
                "connection carries trailing bytes after one OVC1 frame"
            )

    def send_frame(self, payload: bytes) -> None:
        self.connection.sendall(payload)

    def close(self) -> None:
        self.connection.close()


# --- probing ------------------------------------------------------------------


class EndpointProbe(str, Enum):
    """What a connect attempt established about an endpoint."""

    ANSWERING = "answering"
    #: Definitively nobody there: nothing is listening, or the name cannot answer.
    REFUSED = "refused"
    #: Could not be established -- a timeout, a permissions refusal, an unexpected
    #: error, or a scheme this platform cannot open. Says nothing either way.
    UNKNOWN = "unknown"


def probe_endpoint(endpoint: LocalEndpoint, *, timeout: float = 1.0) -> EndpointProbe:
    """Ask whether anything is listening on this endpoint right now.

    A connect is the only honest test: `is_socket()` reports what the inode is, not
    whether anyone is behind it, and a crashed service leaves a socket file identical
    to a live one. A named pipe has no inode at all, so there is nothing else to
    consult there either.

    Three outcomes rather than two, because the two callers need opposite defaults on
    an ambiguous error and a boolean forced one of them to be wrong. Replacing a
    stranger's live endpoint is unrecoverable, so `LocalSocketServer.start()` may only
    claim a name on `REFUSED`. Trusting a descriptor that answers nothing strands the
    workspace, so coordination may only reuse on `ANSWERING`. Collapsing both into
    "assume live" made a stale descriptor pointing at a regular file -- `ENOTSOCK` --
    read as a running service.

    `timeout` bounds the Unix connect. The named-pipe client's wait is bounded by the
    standard library's own connect retry, which takes no timeout; that path is
    reached only while every instance of the pipe is momentarily busy.
    """
    if endpoint.scheme is EndpointScheme.UNIX:
        return _probe_unix(endpoint, timeout=timeout)
    return _probe_pipe(endpoint)


def _probe_unix(endpoint: LocalEndpoint, *, timeout: float) -> EndpointProbe:
    if not _HAS_AF_UNIX:
        # A Windows host cannot open a Unix socket, so it can show this endpoint to
        # be neither live nor dead. `UNKNOWN` is the fail-closed answer: coordination
        # will not reuse it, and a binder will not replace it.
        return EndpointProbe.UNKNOWN
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(endpoint.address)
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


def _probe_pipe(endpoint: LocalEndpoint) -> EndpointProbe:
    from omnivia_core_runtime.service.windows_pipe import probe_pipe

    return EndpointProbe(probe_pipe(endpoint.address, timeout=1.0))


# --- listeners ----------------------------------------------------------------


class _Listener(Protocol):
    """The accept side of one mechanism."""

    def accept(self) -> _Channel | None:
        """The next connection, or None when the wait expired and nobody came."""
        ...

    def wake(self) -> None:
        """Unblock an accept that is waiting, so a stopping server can join it."""
        ...

    def close(self) -> None: ...


@dataclass
class _SocketListener:
    server: socket.socket
    timeout: float

    def accept(self) -> _Channel | None:
        try:
            connection, _ = self.server.accept()
        except TimeoutError:
            return None
        connection.settimeout(self.timeout)
        return _SocketChannel(connection)

    def wake(self) -> None:
        """Nothing to do: the accept times out every 0.2s and re-reads the flag."""

    def close(self) -> None:
        self.server.close()


def _open_pipe_listener(endpoint: LocalEndpoint, *, timeout: float) -> _Listener:
    """Create an exclusive first-instance raw byte-mode named pipe."""
    from omnivia_core_runtime.service.windows_pipe import (
        WindowsPipeError,
        open_pipe_listener,
    )

    try:
        return open_pipe_listener(endpoint.address, timeout=timeout)
    except WindowsPipeError:
        raise TransportError("could not create the raw named-pipe endpoint") from None


def _refuse_live_endpoint(endpoint: LocalEndpoint) -> TransportError:
    return TransportError(
        f"refusing to bind the endpoint: {endpoint.url} is answering or cannot be "
        "shown to be dead, so replacing it could strand a live service."
    )


# --- the service side ---------------------------------------------------------


@dataclass
class LocalSocketServer:
    """Local IPC in front of the dispatcher.

    On POSIX the endpoint is a Unix domain socket, placed in the workspace's `locks/`
    directory rather than a temp path so it is discoverable next to the workspace it
    serves and removed with it. It is created with restrictive permissions: a local
    socket any user could connect to would be an authorization bypass regardless of
    what the dispatcher checks.

    On Windows the endpoint is a named pipe whose name is derived from that same
    path. The pipe carries the default security descriptor: full control for the
    creating user, administrators and LocalSystem, and read access for everyone else.
    Read access alone invokes nothing, because the service answers only a client that
    has written a frame first, and creating a further instance of the name is not
    granted to everyone, so the name cannot be taken either.

    Constructed from a `path` -- the historical form, which selects this platform's
    mechanism for that path -- or from an `endpoint` the caller already holds, such
    as one parsed out of a discovery descriptor.
    """

    dispatcher: Dispatcher | None = None
    router: DocumentRouter | None = None
    path: Path | None = None
    endpoint: LocalEndpoint | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    _listener: _Listener | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event | None = None
    _active_channel: _Channel | None = None
    _owned_socket_identity: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if (self.dispatcher is None) == (self.router is None):
            raise TransportError("the server needs exactly one dispatcher= or router=")
        if self.timeout <= 0:
            raise TransportError("the server timeout must be positive")
        if self.endpoint is not None and self.path is not None:
            raise TransportError("the server takes path= or endpoint=, not both")
        if self.endpoint is None:
            if self.path is None:
                raise TransportError("the server needs a path= or an endpoint=")
            self.endpoint = endpoint_for_path(self.path)

    def _endpoint(self) -> LocalEndpoint:
        assert self.endpoint is not None  # established by __post_init__
        return self.endpoint

    @property
    def url(self) -> str:
        """The endpoint to advertise. Names the mechanism actually served."""
        return self._endpoint().url

    def start(self) -> str:
        if self._listener is not None:
            raise TransportError("transport is already started")
        endpoint = self._endpoint()
        if endpoint.scheme is EndpointScheme.UNIX:
            self._begin_serving(self._bind_unix(endpoint))
        else:
            self._begin_serving(self._bind_pipe(endpoint))
        return endpoint.url

    def _bind_unix(self, endpoint: LocalEndpoint) -> _Listener:
        if not _HAS_AF_UNIX:
            raise TransportError(
                f"this platform cannot serve a unix socket: {endpoint.url}. "
                f"Use a {LOCAL_SCHEME.value}:// endpoint."
            )
        path = Path(endpoint.name)
        assert_socket_path_fits(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Probe, bind and rename happen under one lock on the endpoint name.
        # Without it two services reclaiming the same stale socket both see
        # `REFUSED`, both bind, and the second rename replaces the first's endpoint
        # after it went live -- the stranding the probe exists to prevent, moved a
        # few microseconds later. The lock makes the reclaim a single decision: the
        # loser re-probes, finds a live socket, and refuses.
        bind_lock = create_lock(
            path.with_name(f".{path.name}.bind"), LockRole.BOOTSTRAP_MUTEX
        )
        if not bind_lock.acquire():
            raise TransportError(
                f"refusing to bind the endpoint: another process is claiming "
                f"{path} right now."
            )
        try:
            return self._bind_socket(endpoint, path)
        finally:
            bind_lock.release()

    def _bind_socket(self, endpoint: LocalEndpoint, path: Path) -> _Listener:
        if not path.is_socket() and os.path.lexists(path):
            # Anything that is not a socket is refused rather than removed. "It
            # carries no state" is true of a socket and of nothing else: pointed at
            # `locks/storage.lock`, unlinking destroyed the lifetime lock file while
            # its owner kept an flock on the now-unlinked inode, so once the socket
            # was cleaned up a successor created a fresh lock file and acquired it --
            # two live writers on one workspace.
            raise TransportError(
                f"refusing to bind the endpoint: {path} already exists and is "
                "not a socket. The endpoint must be a path this service owns."
            )
        if path.is_socket() and probe_endpoint(endpoint) is not EndpointProbe.REFUSED:
            raise _refuse_live_endpoint(endpoint)

        # Bound on a private name and renamed into place. Unlinking the target and
        # then binding leaves a window in which the endpoint does not exist -- a
        # caller connecting there fails, and a racing service can bind the free name
        # and then be silently unlinked by this one. `rename` is atomic: the name
        # either refers to the previous socket or to this one, never to nothing.
        staging = path.with_name(f".{path.name}.{os.getpid()}.binding")
        if os.path.lexists(staging):
            staging.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(staging))
            staging.chmod(0o600)
            server.listen(8)
            os.rename(staging, path)
        except BaseException:
            server.close()
            if os.path.lexists(staging):
                staging.unlink()
            raise
        server.settimeout(0.2)
        identity = path.stat()
        self._owned_socket_identity = (identity.st_dev, identity.st_ino)
        return _SocketListener(server, self.timeout)

    def _bind_pipe(self, endpoint: LocalEndpoint) -> _Listener:
        # Probed first for the reason the socket side probes: a name that answers
        # belongs to a live service, and an ambiguous probe is not evidence that it
        # does not. Creating the pipe is itself exclusive, so this is the informative
        # refusal rather than the safety mechanism -- unlike the socket case there is
        # no window here in which a second binder could also succeed.
        if probe_endpoint(endpoint) is not EndpointProbe.REFUSED:
            raise _refuse_live_endpoint(endpoint)
        return _open_pipe_listener(endpoint, timeout=self.timeout)

    def _begin_serving(self, listener: _Listener) -> None:
        self._listener = listener
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="omnivia-ipc", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._listener is not None
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                channel = self._listener.accept()
            except OSError:
                break
            if channel is None:
                continue
            if self._stop.is_set():
                # The wake-up connection, or a client that arrived as the service was
                # going down. Either way it is closed rather than served.
                channel.close()
                break
            self._active_channel = channel
            try:
                self._handle(channel)
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
            finally:
                channel.close()
                self._active_channel = None

    def _handle(self, channel: _Channel) -> None:
        raw = channel.read_frame()
        if raw is None:
            return
        document = decode_frame(raw)
        if self.router is not None:
            result = self.router.route(document)
            payload = result.to_wire()
        else:
            assert self.dispatcher is not None
            request = codec.decode_request(document)
            response = self.dispatcher.dispatch(request)
            payload = codec.encode_response(response)
        # Reading the frame checked the initial unary boundary, but dispatch may run
        # longer than that quiet window.  Recheck after all router/dispatcher work and
        # immediately before the response write so traffic pipelined during dispatch
        # cannot receive a valid response.
        channel.ensure_unary_boundary()
        channel.send_frame(encode_frame(payload))

    def stop(self) -> None:
        served = self._listener is not None
        if self._stop is not None:
            self._stop.set()
        if self._active_channel is not None:
            self._active_channel.close()
        if self._listener is not None:
            # Woken in bounded attempts rather than joined once for a long time: the
            # wake has to land after the stop flag is set, and on a slow start the
            # accept it unblocks may not have reached its wait when the first arrives.
            for _ in range(3):
                if self._thread is None or not self._thread.is_alive():
                    break
                self._listener.wake()
                self._thread.join(timeout=2)
        self._thread = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if served:
            self._remove_socket_file()

    def _remove_socket_file(self) -> None:
        """Remove what this instance created, and only that.

        A named pipe leaves nothing to remove: its instances die with the handles the
        listener just closed, which are this instance's and no other's. Called only
        when this instance actually served, so stopping a server that never started
        cannot delete a file it does not own.
        """
        path = self._endpoint().path
        if path is None or self._owned_socket_identity is None:
            return
        try:
            identity = path.stat()
        except FileNotFoundError:
            return
        if (identity.st_dev, identity.st_ino) == self._owned_socket_identity:
            path.unlink()
        self._owned_socket_identity = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


# --- the client side ----------------------------------------------------------


@dataclass
class LocalSocketTransport:
    """Client side of the local endpoint.

    A fresh connection per call. Connection pooling would be an optimisation with a
    correctness cost here: a pooled connection outliving a service restart would
    deliver a request to a dead endpoint, and the client would have to distinguish
    that from a genuine failure.

    Takes a `path` -- resolved to this platform's mechanism -- or an `endpoint` the
    caller already holds, such as one parsed out of a discovery descriptor.
    """

    path: Path | None = None
    endpoint: LocalEndpoint | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.endpoint is not None and self.path is not None:
            raise TransportError("the transport takes path= or endpoint=, not both")
        if self.endpoint is None:
            if self.path is None:
                raise TransportError("the transport needs a path= or an endpoint=")
            self.endpoint = endpoint_for_path(self.path)

    def _endpoint(self) -> LocalEndpoint:
        assert self.endpoint is not None  # established by __post_init__
        return self.endpoint

    def call(self, request: RequestEnvelope) -> ResponseEnvelope:
        channel = _connect(self._endpoint(), timeout=self.timeout)
        raw: bytes | None = None
        failed = False
        try:
            channel.send_frame(encode_frame(codec.encode_request(request)))
            raw = channel.read_frame()
        except OSError:
            failed = True
        finally:
            try:
                channel.close()
            except OSError:
                failed = True

        if failed:
            raise TransportError("local service transport call failed")
        if raw is None:
            raise TransportError("service closed the connection without responding")
        return codec.decode_response(decode_frame(raw))


def _connect(endpoint: LocalEndpoint, *, timeout: float) -> _Channel:
    if endpoint.scheme is EndpointScheme.UNIX:
        return _connect_unix(endpoint, timeout=timeout)
    return _connect_pipe(endpoint, timeout=timeout)


def _connect_unix(endpoint: LocalEndpoint, *, timeout: float) -> _Channel:
    if not _HAS_AF_UNIX:
        raise TransportError(
            "this platform cannot open the requested local service endpoint"
        )
    path = Path(endpoint.name)
    assert_socket_path_fits(path)
    if not path.exists():
        raise TransportError("local service endpoint is unavailable")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    failed = False
    try:
        connection.connect(endpoint.address)
    except OSError:
        failed = True
    if failed:
        connection.close()
        raise TransportError("could not connect to local service endpoint")
    return _SocketChannel(connection)


def _connect_pipe(endpoint: LocalEndpoint, *, timeout: float) -> _Channel:
    from omnivia_core_runtime.service.windows_pipe import (
        WindowsPipeError,
        open_pipe_client,
    )

    channel: _Channel | None = None
    missing = False
    failed = False
    try:
        channel = open_pipe_client(endpoint.address, timeout=timeout)
    except WindowsPipeError as error:
        missing = error.code == 2
        failed = not missing
    if missing:
        raise TransportError("local service endpoint is unavailable")
    if failed:
        raise TransportError("could not connect to local service endpoint")
    assert channel is not None
    return channel


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LOCAL_SCHEME",
    "MAX_FRAME_BYTES",
    "MAX_SOCKET_PATH_BYTES",
    "PIPE_ADDRESS_PREFIX",
    "EndpointProbe",
    "EndpointScheme",
    "InProcessTransport",
    "LocalEndpoint",
    "LocalSocketServer",
    "LocalSocketTransport",
    "Transport",
    "TransportError",
    "assert_socket_path_fits",
    "decode_frame",
    "encode_frame",
    "endpoint_for_path",
    "names_a_local_endpoint",
    "parse_endpoint",
    "pipe_name_for_path",
    "probe_endpoint",
]
