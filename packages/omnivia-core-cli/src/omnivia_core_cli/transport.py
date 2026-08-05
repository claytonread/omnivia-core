"""One concrete :class:`ClientTransport` over the installation-local IPC endpoint.

``omnivia-core-client`` defines the transport contract and deliberately ships no
implementation of it: its accepted import allowlist admits the standard library
subset it needs and the public ``omnivia_core`` contracts, and names ``socket``,
``ssl`` and ``urllib`` as forbidden. A distribution that opened a socket would
stop being a protocol foundation and start being a transport, which is the
boundary that test enforces. So the concrete transport lives here, in the caller
that dials, and satisfies the protocol structurally rather than by inheritance.

**Nothing here is reimplemented.** The frame is
:func:`omnivia_core_client.encode_frame` and :func:`~omnivia_core_client.decode_frame`
-- the accepted OVC1 wire, byte for byte, including its canonical-JSON admission
rule. The envelope is the public ``omnivia_core`` codec. What this module adds is
exactly the part the client package may not hold: a file descriptor, a connect, a
bounded read, and the mapping from an operating-system failure onto the client's
declared error types.

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

Local IPC only. A ``pipe://`` endpoint is refused rather than half-supported:
the Windows named-pipe client is a successor, and answering a Windows caller
with a socket error would misreport why.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from omnivia_core_client import (
    HEADER_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    CancellationToken,
    Deadline,
    DeadlineExceededError,
    ProtocolError,
    TransportError,
    decode_frame,
    encode_frame,
    enforce_send_preconditions,
)

from omnivia_core.contracts.v1 import (
    ContractDecodeError,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
    decode_service_probe_result,
)

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


def socket_path_for(endpoint_uri: str) -> str:
    """The filesystem path a ``unix://`` endpoint URI names.

    Refuses anything else, including the ``pipe://`` form a Windows service
    publishes, so an unsupported platform is reported as unsupported rather
    than as a connection failure.
    """
    if not endpoint_uri.startswith(LOCAL_IPC_SCHEME):
        raise TransportError(
            "this transport dials installation-local "
            f"{LOCAL_IPC_SCHEME} endpoints only"
        )
    path = endpoint_uri[len(LOCAL_IPC_SCHEME) :]
    if not path:
        raise TransportError("the endpoint names no local socket path")
    return path


def _connect(path: str, timeout: float) -> socket.socket:
    """One fresh connected stream socket, or the declared transport failure."""
    if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - POSIX-only suite
        raise TransportError("local socket endpoints are not supported on this platform")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(path)
    except TimeoutError:
        connection.close()
        raise DeadlineExceededError(
            "the deadline passed while connecting to the local endpoint"
        ) from None
    except OSError:
        # The operating system's own message names a local path and is outside
        # the payload-free rule the client's diagnostics keep, so the failure
        # keeps its kind and loses its words.
        connection.close()
        raise TransportError("the local endpoint could not be reached") from None
    return connection


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
        try:
            chunk = connection.recv(min(remaining, _READ_CHUNK))
        except TimeoutError:
            raise DeadlineExceededError(
                "the deadline passed while reading the response frame"
            ) from None
        except OSError:
            raise TransportError("the local endpoint dropped the call mid-frame") from None
        if not chunk:
            raise TransportError(
                f"the local endpoint closed after {count - remaining} of {count} bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(connection: socket.socket, deadline: Deadline) -> dict[str, object]:
    """One complete OVC1 frame, admitted by the client's own decoder.

    Exactly one rule is applied here, and it is the one that cannot be applied
    anywhere else: the declared length is checked against the frozen maximum
    *before* the body is read, so a peer cannot make this process wait on, or
    allocate against, a number it made up.

    Every other admission rule -- the magic, the zero-length body, the trailing
    bytes, the UTF-8, the JSON, the canonical byte form -- belongs to
    :func:`~omnivia_core_client.decode_frame` and is left there. Restating one
    here would be a second copy of the frozen format's rules inside the caller
    that is supposed to be reusing them, free to drift from the accepted decoder
    and answering for it when it did.
    """
    header = _read_exact(connection, HEADER_BYTES, deadline)
    length = int.from_bytes(header[len(MAGIC) :], "big")
    if length > MAXIMUM_JSON_BYTES:
        raise ProtocolError(
            f"the response declares {length} OVC1 body bytes, above the "
            f"{MAXIMUM_JSON_BYTES}-byte maximum"
        )
    return decode_frame(header + _read_exact(connection, length, deadline))


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
        connection = _connect(socket_path_for(self.endpoint_uri), remaining)
        try:
            connection.settimeout(deadline.assert_not_expired(operation=operation))
            try:
                connection.sendall(frame)
            except TimeoutError:
                raise DeadlineExceededError(
                    "the deadline passed while sending the request frame"
                ) from None
            except OSError:
                raise TransportError(
                    "the local endpoint dropped the call before it was sent"
                ) from None
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
        try:
            return codec.decode_response(document)
        except ContractDecodeError:
            raise ProtocolError(
                "the answer is not a well-formed response envelope"
            ) from None

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
        try:
            return decode_service_probe_result(document)
        except ContractDecodeError:
            raise ProtocolError(
                "the answer is not a well-formed probe result"
            ) from None
