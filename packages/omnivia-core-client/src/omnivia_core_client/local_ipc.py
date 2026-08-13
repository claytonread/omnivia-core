"""The one concrete :class:`ClientTransport` over the installation-local IPC endpoint.

Owner resolution 005 R005-01 places this here. It was filed in ``omnivia-core-cli``
on the reasoning that a distribution which opens a socket stops being a protocol
foundation -- but the code was never CLI-coupled, and leaving it there meant every
other caller either imported the CLI or wrote a second dial loop. It belongs beside
the :class:`~omnivia_core_client.ClientTransport` protocol it satisfies, the OVC1
framing it carries, the discovery that hands it an endpoint, and the
:class:`~omnivia_core_client.TransportError` it raises. The CLI and MCP both
construct it from here; neither owns a copy.

This is the only module in this package permitted to import ``socket``, and
``tests/test_package_isolation.py`` pins that to this file by name. The rest of
the package remains socket-free: the boundary that matters is the *distribution*
dependency edge -- still exactly ``omnivia-core`` -- not the standard library.

**Nothing here is reimplemented.** The frame is
:func:`omnivia_core_client.encode_frame` and :func:`~omnivia_core_client.decode_frame`
-- the accepted OVC1 wire, byte for byte, including its canonical-JSON admission
rule. The envelope is the public ``omnivia_core`` codec. What this module adds is
exactly the part the rest of the package does not hold: a file descriptor, a
connect, a bounded read, and the mapping from an operating-system failure onto the
package's declared error types.

The connection model matches the server's, which is strictly unary: one
connection carries one frame out and one frame back, and is then closed. The
server actively refuses a connection that carries trailing bytes after its
request frame, so this transport writes exactly once and then only reads. No
pooling, no reuse, no second frame -- a fresh connection per call.

**What reaching the endpoint does and does not establish.** The endpoint is
protected by operating-system filesystem permissions. Connecting to it proves
that this process could open that path; it is not a verification of peer
identity, and no diagnostic, field or docstring in this module states or implies
that the peer was authenticated. The deferral recorded for that gap is
``LOCAL-IPC-PEER-IDENTITY-DEFERRED``.

Both accepted local transports are selected here: ``unix://`` uses the POSIX
stream socket below and ``pipe://`` uses the raw Win32 byte-stream client in
:mod:`omnivia_core_client.windows_pipe`.  Both carry the same OVC1 frame and
return through the same public codec, so callers have one local transport rather
than platform-specific glue.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import NoReturn

from omnivia_core.contracts.v1 import (
    ContractDecodeError,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
    decode_service_probe_result,
)
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import (
    DeadlineExceededError,
    ProtocolError,
    TransportError,
)
from omnivia_core_client.framing import (
    HEADER_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    decode_frame,
    encode_frame,
)
from omnivia_core_client.transport import enforce_send_preconditions
from omnivia_core_client.windows_pipe import PIPE_SCHEME, open_pipe_channel

__all__ = [
    "LOCAL_IPC_SCHEME",
    "LocalIpcTransport",
    "socket_path_for",
]

#: The one endpoint scheme this transport dials, spelled as the public
#: ``ServiceEndpointUri`` pattern spells it. Narrowing that policy, never
#: restating it: no scheme is admitted here that the public descriptor decoder
#: did not already admit.
LOCAL_IPC_SCHEME = "unix://"

#: Bytes requested per ``recv``. Only a ceiling on one syscall's copy -- every
#: read below is bounded by an exact remaining count, never by this.
_READ_CHUNK = 64 * 1024

#: The quiet window that has to follow one frame. The same value the server's own
#: ``ensure_unary_boundary`` uses, because it is the same invariant seen from the
#: other end of the connection.
_UNARY_BOUNDARY_SECONDS = 0.05


def socket_path_for(endpoint_uri: str) -> str:
    """The filesystem path a ``unix://`` endpoint URI names.

    Refuses anything else, including the ``pipe://`` form a Windows service
    publishes, so an unsupported platform is reported as unsupported rather
    than as a connection failure.
    """
    if not endpoint_uri.startswith(LOCAL_IPC_SCHEME):
        raise TransportError(
            f"this transport dials installation-local {LOCAL_IPC_SCHEME} endpoints only"
        )
    path = endpoint_uri[len(LOCAL_IPC_SCHEME) :]
    if not path:
        raise TransportError("the endpoint names no local socket path")
    return path


#: Raised from outside the ``except`` blocks that decide on them, never inside.
#:
#: ``raise X from None`` inside a handler clears ``__cause__`` but leaves
#: ``__context__`` pointing at the exception being handled -- and here that is an
#: ``OSError`` whose message names a local socket path. This module's stated rule
#: is that a failure "keeps its kind and loses its words"; one attribute access on
#: ``__context__`` would have recovered the words. Raising after the handler ends
#: means there is no exception being handled, so ``__context__`` is genuinely
#: ``None``. Same shape as ``discovery.py``'s ``_raise_*`` helpers, and
#: ``scripts/check-raise-discipline.py`` is what keeps it that way.


def _raise_connect_deadline() -> NoReturn:
    raise DeadlineExceededError(
        "the deadline passed while connecting to the local endpoint"
    )


def _raise_unreachable() -> NoReturn:
    raise TransportError("the local endpoint could not be reached")


def _raise_read_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while reading the response frame")


def _raise_dropped_mid_frame() -> NoReturn:
    raise TransportError("the local endpoint dropped the call mid-frame")


def _raise_send_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while sending the request frame")


def _raise_dropped_before_send() -> NoReturn:
    raise TransportError("the local endpoint dropped the call before it was sent")


def _raise_not_a_response_envelope() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed response envelope")


def _raise_not_a_probe_result() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed probe result")


def _connect(path: str, timeout: float) -> socket.socket:
    """One fresh connected stream socket, or the declared transport failure."""
    if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - POSIX-only suite
        raise TransportError(
            "local socket endpoints are not supported on this platform"
        )
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    timed_out = False
    try:
        connection.settimeout(timeout)
        connection.connect(path)
    except TimeoutError:
        connection.close()
        timed_out = True
    except OSError:
        # The operating system's own message names a local path and is outside
        # the payload-free rule the client's diagnostics keep, so the failure
        # keeps its kind and loses its words.
        connection.close()
    else:
        return connection
    if timed_out:
        _raise_connect_deadline()
    _raise_unreachable()


def _read_exact(connection: socket.socket, count: int, deadline: Deadline) -> bytes:
    """Exactly ``count`` bytes, or the reason there are not that many.

    Every wait is bounded by what is *left* of the whole-call deadline at the
    moment it is about to block, re-read here rather than carried in from
    before the write -- so a slow first byte cannot buy the rest of the frame a
    fresh budget.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        connection.settimeout(deadline.assert_not_expired(operation="read"))
        chunk = b""
        failure = ""
        try:
            chunk = connection.recv(min(remaining, _READ_CHUNK))
        except TimeoutError:
            failure = "deadline"
        except OSError:
            failure = "dropped"
        if failure == "deadline":
            _raise_read_deadline()
        if failure == "dropped":
            _raise_dropped_mid_frame()
        if not chunk:
            raise TransportError(
                f"the local endpoint closed after {count - remaining} of {count} bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _ensure_nothing_follows(connection: socket.socket, deadline: Deadline) -> None:
    """Require one bounded quiet window after the frame, as the server does.

    :func:`~omnivia_core_client.decode_frame` owns the trailing-byte rule, but it
    can only apply it to bytes it is *given*, and this transport gives it exactly
    the frame it declared -- so that branch could never fire from here. Without
    this check a peer that replies with two frames has the first one accepted and
    the second silently dropped, which is a stale answer being returned as a
    fresh one.

    The server enforces the same invariant in the request direction with
    ``ensure_unary_boundary``; this is its mirror in the response direction, with
    the same 50 ms window and the same `MSG_PEEK` so the byte is not consumed.
    """
    window = min(_UNARY_BOUNDARY_SECONDS, deadline.remaining_seconds())
    connection.settimeout(window)
    try:
        trailing = connection.recv(1, socket.MSG_PEEK)
    except TimeoutError:
        return
    except OSError:
        return
    if trailing:
        raise ProtocolError("the response carries trailing bytes after one OVC1 frame")


def _read_frame(connection: socket.socket, deadline: Deadline) -> dict[str, object]:
    """One complete OVC1 frame, admitted by the client's own decoder.

    Three rules are applied here, and each is here because it is about *order* or
    about the stream, which is precisely what a decoder handed a finished buffer
    cannot reach:

    1. the magic is checked before the length is used, so four bytes of a foreign
       protocol are never read as a byte count -- a wrong-protocol listener
       squatting the endpoint is named immediately instead of holding this
       process until the deadline and then being reported as a slow service;
    2. the declared length is checked against the frozen maximum before the body
       is read, so a peer cannot make this process wait on, or allocate against,
       a number it made up;
    3. nothing may follow the frame -- see :func:`_ensure_nothing_follows`.

    Everything else -- the zero-length body, the UTF-8, the JSON, the canonical
    byte form, and the trailing-byte rule as it applies to a finished buffer --
    belongs to :func:`~omnivia_core_client.decode_frame` and is left there.
    """
    header = _read_exact(connection, HEADER_BYTES, deadline)
    if header[: len(MAGIC)] != MAGIC:
        # No part of the header is quoted. Four bytes of an unknown stream are
        # peer material like any other, and rendering them -- as a decimal, as
        # text, or as hex -- would put them somewhere they can be logged.
        raise ProtocolError("the response does not begin with the OVC1 magic")
    length = int.from_bytes(header[len(MAGIC) :], "big")
    if length > MAXIMUM_JSON_BYTES:
        raise ProtocolError(
            f"the response declares {length} OVC1 body bytes, above the "
            f"{MAXIMUM_JSON_BYTES}-byte maximum"
        )
    frame = decode_frame(header + _read_exact(connection, length, deadline))
    _ensure_nothing_follows(connection, deadline)
    return frame


@dataclass(frozen=True, slots=True)
class LocalIpcTransport:
    """A :class:`~omnivia_core_client.ClientTransport` over one local endpoint.

    Frozen and holding only the endpoint: there is no connection to keep, so
    there is no state to get wrong. Structural typing is what makes it a
    ``ClientTransport``; nothing here inherits from the protocol.
    """

    endpoint_uri: str

    def _exchange(
        self,
        document: dict[str, object],
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
    ) -> dict[str, object]:
        """One frame out, one frame back, on a connection used for nothing else."""
        remaining = enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=operation
        )
        frame = encode_frame(document)
        if self.endpoint_uri.startswith(PIPE_SCHEME):
            channel = open_pipe_channel(
                self.endpoint_uri,
                deadline=deadline,
                cancellation=cancellation,
                operation=operation,
            )
            try:
                channel.write(
                    frame,
                    deadline=deadline,
                    cancellation=cancellation,
                    operation=operation,
                )
                return channel.read_document(
                    deadline=deadline,
                    cancellation=cancellation,
                    operation=operation,
                )
            finally:
                channel.close()
        connection = _connect(socket_path_for(self.endpoint_uri), remaining)
        try:
            connection.settimeout(deadline.assert_not_expired(operation=operation))
            failure = ""
            try:
                connection.sendall(frame)
            except TimeoutError:
                failure = "deadline"
            except OSError:
                failure = "dropped"
            if failure == "deadline":
                _raise_send_deadline()
            if failure == "dropped":
                _raise_dropped_before_send()
            # Nothing further is written on this connection. The server refuses
            # a connection carrying trailing bytes after one frame, so a
            # speculative second write would be read as pipelining and refused.
            return _read_frame(connection, deadline)
        finally:
            connection.close()

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        """Send one application request and return the peer's response envelope.

        An application error is an answer, so it comes back as an error
        response envelope rather than as an exception. The reply is decoded
        through the public codec, not merely parsed: the codec is what checks
        the version window, the granted authority and the error retry class, and
        a reply that is structurally JSON but semantically impossible must not
        reach a caller as if it were valid.
        """
        document = self._exchange(
            codec.encode_request(request),
            deadline=deadline,
            cancellation=cancellation,
            operation=request.operation,
        )
        decoded = False
        try:
            response = codec.decode_response(document)
            decoded = True
        except ContractDecodeError:
            pass
        if not decoded:
            _raise_not_a_response_envelope()
        return response

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        """Send one runtime probe and return the peer's probe result."""
        document = self._exchange(
            request.to_wire(),
            deadline=deadline,
            cancellation=cancellation,
            operation=str(request.probe),
        )
        decoded = False
        try:
            result = decode_service_probe_result(document)
            decoded = True
        except ContractDecodeError:
            pass
        if not decoded:
            _raise_not_a_probe_result()
        return result
