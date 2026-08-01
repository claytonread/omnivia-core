"""Transports over the one dispatcher (B9).

B9's exit condition is that in-process **and** service transports pass the same
conformance suite. One dispatcher with two transports in front of it is what makes
that testable: if the transports shared no implementation, "the same suite" would be
two suites that happen to agree today.

The wire format is the contract's canonical JSON, newline-delimited. A length-prefix
would be marginally faster and considerably harder to debug; newline-delimited JSON
can be read with `nc` while diagnosing a stuck service, which matters more for a
local IPC endpoint than throughput does.

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
from typing import TYPE_CHECKING, Protocol, Self

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope, codec
from omnivia_core_runtime.ownership.locks import IS_WINDOWS, LockRole, create_lock
from omnivia_core_runtime.service.dispatch import Dispatcher

if TYPE_CHECKING:  # pragma: no cover - types only
    # Imported for annotations alone so a POSIX process never pays for
    # `multiprocessing` at import time. The named-pipe code imports it where it
    # runs, the same way the lock module imports `msvcrt` where it runs.
    from multiprocessing.connection import Connection, Listener

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
        return codec.decode_response(json.loads(codec.to_canonical_json(codec.encode_response(response))))


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
            raise TransportError(f"not a usable named-pipe name: {self.name!r}")
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
            f"AF_UNIX limit: {path}. Place the socket under a shorter directory."
        )


# --- frames -------------------------------------------------------------------


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


class _Channel(Protocol):
    """One accepted or dialled connection, framed.

    Narrow on purpose: the dispatcher side reads a frame and writes a frame, and
    nothing above this line knows whether the bytes crossed a socket or a pipe.
    """

    def read_frame(self, *, limit: int = MAX_FRAME_BYTES) -> bytes | None: ...

    def send_frame(self, payload: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass
class _SocketChannel:
    """Newline-delimited frames over a stream socket."""

    connection: socket.socket

    def read_frame(self, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
        """Read one newline-delimited frame, or None when the peer closes."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = self.connection.recv(4096)
            if not chunk:
                return b"".join(chunks) or None
            total += len(chunk)
            if total > limit:
                raise TransportError(f"frame exceeds {limit} bytes")
            chunks.append(chunk)
            if b"\n" in chunk:
                return b"".join(chunks).split(b"\n", 1)[0]

    def send_frame(self, payload: bytes) -> None:
        self.connection.sendall(payload)

    def close(self) -> None:
        self.connection.close()


@dataclass
class _PipeChannel:
    """The same frames over a Windows named pipe.

    `multiprocessing.connection` opens the pipe in message mode and adds no framing
    of its own, so one `send_bytes` puts exactly `encode_frame`'s bytes on the wire
    and one `recv_bytes` takes exactly those bytes off it. The wire format is
    therefore the same document on both platforms, not merely an equivalent one.

    The read is bounded twice. `poll` bounds the wait, because a pipe read otherwise
    blocks forever and one stalled client would take the sole accept loop with it;
    `maxlength` bounds the size, at the limit the socket reader enforces.
    """

    connection: Connection
    timeout: float

    def read_frame(self, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
        if not self.connection.poll(self.timeout):
            raise TransportError(f"no frame within {self.timeout} seconds")
        try:
            raw = self.connection.recv_bytes(maxlength=limit)
        except EOFError:
            return None
        except OSError as error:
            raise TransportError(f"frame exceeds {limit} bytes or is unreadable: {error}") from error
        # Split as the socket reader splits, so a client that packs trailing bytes
        # after the newline is read identically on both platforms.
        return raw.split(b"\n", 1)[0]

    def send_frame(self, payload: bytes) -> None:
        self.connection.send_bytes(payload)

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
    from multiprocessing import connection as pipes

    try:
        client = pipes.Client(endpoint.address, family="AF_PIPE")
    except FileNotFoundError:
        # No instance of this name exists, so nothing is listening on it.
        return EndpointProbe.REFUSED
    except OSError:
        # A pipe of this name exists but would not open for us -- a foreign owner's
        # security descriptor, or every instance busy past the client's own retry.
        # Neither says the endpoint is dead.
        return EndpointProbe.UNKNOWN
    client.close()
    return EndpointProbe.ANSWERING


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

    def accept(self) -> _Channel | None:
        try:
            connection, _ = self.server.accept()
        except TimeoutError:
            return None
        connection.settimeout(DEFAULT_TIMEOUT_SECONDS)
        return _SocketChannel(connection)

    def wake(self) -> None:
        """Nothing to do: the accept times out every 0.2s and re-reads the flag."""

    def close(self) -> None:
        self.server.close()


@dataclass
class _PipeListener:
    listener: Listener
    address: str

    def accept(self) -> _Channel | None:
        return _PipeChannel(self.listener.accept(), DEFAULT_TIMEOUT_SECONDS)

    def wake(self) -> None:
        """Be the client the accept is waiting for.

        A named-pipe accept blocks in `ConnectNamedPipe` with no timeout, and closing
        the listener does not close the handle it is already waiting on -- that handle
        was taken out of the listener's queue before the wait began. Connecting once
        and hanging up is the way to end the wait from another thread; the accept loop
        then sees the stop flag and leaves.

        `OSError` here is the expected case, not a masked failure: by the time a stop
        reaches this the name may already be gone. It is caught on one best-effort
        call, never around anything that serves a client.
        """
        from multiprocessing import connection as pipes

        try:
            pipes.Client(self.address, family="AF_PIPE").close()
        except OSError:
            return

    def close(self) -> None:
        self.listener.close()


def _open_pipe_listener(endpoint: LocalEndpoint) -> _Listener:
    """Create the named pipe, claiming the name exclusively.

    The standard library creates the first instance with
    `FILE_FLAG_FIRST_PIPE_INSTANCE`, so the kernel refuses this call outright when
    any other process already owns the name. That is the guarantee the POSIX side
    buys with a bind lock around probe-and-rename, except the OS makes it: there is no
    lock to take and no window to lose, two services cannot both serve one name, and
    the loser finds out here.
    """
    from multiprocessing import connection as pipes

    try:
        listener = pipes.Listener(endpoint.address, family="AF_PIPE")
    except PermissionError as error:
        raise TransportError(
            f"refusing to bind the endpoint: {endpoint.url} is already owned by "
            "another process, so claiming it could strand a live service."
        ) from error
    except OSError as error:
        raise TransportError(f"could not create the endpoint {endpoint.url}: {error}") from error
    return _PipeListener(listener, endpoint.address)


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

    dispatcher: Dispatcher
    path: Path | None = None
    endpoint: LocalEndpoint | None = None
    _listener: _Listener | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event | None = None

    def __post_init__(self) -> None:
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
        bind_lock = create_lock(path.with_name(f".{path.name}.bind"), LockRole.BOOTSTRAP_MUTEX)
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
        return _SocketListener(server)

    def _bind_pipe(self, endpoint: LocalEndpoint) -> _Listener:
        # Probed first for the reason the socket side probes: a name that answers
        # belongs to a live service, and an ambiguous probe is not evidence that it
        # does not. Creating the pipe is itself exclusive, so this is the informative
        # refusal rather than the safety mechanism -- unlike the socket case there is
        # no window here in which a second binder could also succeed.
        if probe_endpoint(endpoint) is not EndpointProbe.REFUSED:
            raise _refuse_live_endpoint(endpoint)
        return _open_pipe_listener(endpoint)

    def _begin_serving(self, listener: _Listener) -> None:
        self._listener = listener
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="omnivia-ipc", daemon=True)
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

    def _handle(self, channel: _Channel) -> None:
        raw = channel.read_frame()
        if raw is None:
            return
        request = codec.decode_request(decode_frame(raw))
        response = self.dispatcher.dispatch(request)
        channel.send_frame(encode_frame(codec.encode_response(response)))

    def stop(self) -> None:
        served = self._listener is not None
        if self._stop is not None:
            self._stop.set()
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
        if path is not None and path.exists():
            path.unlink()

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
        try:
            channel.send_frame(encode_frame(codec.encode_request(request)))
            raw = channel.read_frame()
        except OSError as error:
            raise TransportError(f"transport failed: {error}") from error
        finally:
            channel.close()

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
            f"this platform cannot open a unix socket: {endpoint.url}. "
            f"Use a {LOCAL_SCHEME.value}:// endpoint."
        )
    path = Path(endpoint.name)
    assert_socket_path_fits(path)
    if not path.exists():
        raise TransportError(f"no service endpoint at {endpoint.url}")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(endpoint.address)
    except OSError as error:
        connection.close()
        raise TransportError(f"transport failed: {error}") from error
    return _SocketChannel(connection)


def _connect_pipe(endpoint: LocalEndpoint, *, timeout: float) -> _Channel:
    from multiprocessing import connection as pipes

    try:
        pipe = pipes.Client(endpoint.address, family="AF_PIPE")
    except FileNotFoundError as error:
        raise TransportError(f"no service endpoint at {endpoint.url}") from error
    except OSError as error:
        raise TransportError(f"transport failed: {error}") from error
    return _PipeChannel(pipe, timeout)


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
