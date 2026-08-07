"""`LocalIpcTransport`, against peers that misbehave.

Owner resolution 005 R005-01 moved the transport into this package, and these
tests came with it from `packages/omnivia-core-cli/tests/test_workspace_show.py`
unchanged in substance: the implementation is now primarily tested where it
lives. The CLI and MCP keep adapter-level integration coverage against a real
service; the framing and error paths are here, because they belong to the
transport rather than to either caller.

**A real listening socket, never a mock of `socket`.** These paths cannot be
reached from a correct server -- a correct server never sends a truncated frame,
two frames, or a non-canonical body -- so they get a peer that does. The bytes
are real, the file descriptor is real, and the failure is the transport's own.

This is the only test module in this package that opens a socket, which is the
mirror of `local_ipc.py` being the only source module that imports one.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_client import (
    CLIENT_API_VERSION,
    CancellationToken,
    Deadline,
    DeadlineExceededError,
    LocalIpcTransport,
    OperationCancelledError,
    ProtocolError,
    TransportError,
    encode_frame,
    socket_path_for,
)

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    codec,
    get_operation_metadata,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

WORKSPACE_ID = "ws-local-ipc-01"
OPERATION = "workspace.inspect"
CALL_TIMEOUT = 10.0


def _short_socket_directory() -> Path:
    """A socket directory outside `tmp_path`.

    R004-15 caps a local endpoint at 86 encoded bytes -- what `sockaddr_un`'s
    104-byte `sun_path` leaves after the NUL terminator and the runtime's
    fixed-width staging name -- and pytest's `tmp_path` nests deep enough to
    exceed it. Only the socket has the limit, so only the socket gets the short
    path.
    """
    return Path(tempfile.mkdtemp(prefix="ovc-", dir=tempfile.gettempdir()))


def _probe_request(request_id: str = "client-frame-1") -> RequestEnvelope:
    """One valid request envelope, built from the public contract alone.

    Built here rather than borrowed from a caller: this package must not import
    the CLI or MCP, and the transport does not care which of them is dialling.
    What matters to these tests is only that the envelope encodes, so the frame
    on the wire is a real one.
    """
    entry = get_operation_metadata(OPERATION)
    required = entry.required_capability
    return RequestEnvelope(
        operation=OPERATION,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=CLIENT_API_VERSION,
            client=ClientIdentity(id="omnivia-core-client-tests", version="0.1.0"),
            workspace_id=WORKSPACE_ID,
            scopes=tuple(entry.scope.required_scopes),
            purpose="workspace_inspection",
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=required.required,
                ),
            ),
        ),
        input={},
    )


class ScriptedPeer:
    """A real listening socket that answers one connection with chosen bytes.

    Records what it was sent, so the unary discipline the server relies on can be
    asserted rather than assumed.
    """

    def __init__(
        self,
        reply: bytes | None,
        *,
        hold: bool = False,
        dribble_seconds: float | None = None,
    ) -> None:
        self.hold = hold
        self.dribble_seconds = dribble_seconds
        self.directory = _short_socket_directory()
        self.path = self.directory / "s.sock"
        self.received = b""
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(1)
        self._server.settimeout(30)
        self._reply = reply
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def endpoint_uri(self) -> str:
        return f"unix://{self.path}"

    def _serve(self) -> None:
        try:
            connection, _ = self._server.accept()
        except OSError:  # pragma: no cover - only when the test tears down early
            return
        with connection:
            try:
                # Read until the client stops sending, so a client that wrote
                # more than one frame is visible in `received` rather than
                # invisible. The timeout is the "stopped sending" signal; the
                # client is local and has already written before this runs.
                connection.settimeout(0.5)
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    self.received += chunk
            except OSError:
                pass
            if self._reply is None:
                # Hold the connection open until teardown. Closing here would
                # give the client an end-of-stream, and a test that means to
                # observe a deadline would observe a dropped call instead --
                # a race between this thread's read timeout and the client's
                # budget, decided differently on a loaded machine.
                self._released.wait(timeout=30)
                return
            try:
                if self.dribble_seconds is None:
                    connection.sendall(self._reply)
                else:
                    # One byte at a time, slowly. Every individual read succeeds,
                    # so only a budget that spans the whole call can end this.
                    for index in range(len(self._reply)):
                        connection.sendall(self._reply[index : index + 1])
                        time.sleep(self.dribble_seconds)
            except OSError:  # pragma: no cover - client may have gone
                pass
            if self.hold:
                # Stay open after answering. A close would hand the client an
                # end-of-stream, which is a different thing from a peer that has
                # simply not said any more yet -- and it is the second one that
                # a bounded quiet window has to be able to tell apart.
                self._released.wait(timeout=30)

    def close(self) -> None:
        self._released.set()
        self._server.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


@pytest.fixture
def scripted_peer() -> Iterator[object]:
    peers: list[ScriptedPeer] = []

    def make(reply: bytes | None, **options: object) -> ScriptedPeer:
        peer = ScriptedPeer(reply, **options)
        peers.append(peer)
        return peer

    yield make
    for peer in peers:
        peer.close()




def test_a_reply_with_the_wrong_magic_is_a_protocol_error(scripted_peer: object) -> None:
    peer = scripted_peer(b"XXXX" + (4).to_bytes(4, "big") + b"{}\n\n")  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="magic"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_wrong_protocol_listener_is_named_at_once_and_not_waited_out(
    scripted_peer: object,
) -> None:
    """The magic is checked before the length is used, or a squatter wins.

    The peer sends eight bytes that are not an OVC1 header and then says nothing
    more, holding the connection open. Those four length bytes are a foreign
    protocol's payload, not a byte count -- and a transport that reads them as
    one blocks for the whole budget and then reports a deadline, telling an
    operator the service was slow when a wrong-protocol listener was squatting
    the endpoint.

    The test above cannot catch this: it *sends* the body, so the read completes
    and `decode_frame` is reached whatever the order. Withholding the body is
    what makes the ordering observable.

    The elapsed assertion is the whole point. A generous ceiling, because it has
    to separate "refused immediately" from "waited out a 5-second budget", not
    measure anything finer.
    """
    peer = scripted_peer(b"HELO" + (1024).to_bytes(4, "big"), hold=True)  # type: ignore[operator]

    started = time.monotonic()
    with pytest.raises(ProtocolError, match="magic"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(5.0)
        )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"refusal waited {elapsed:.3f}s instead of being immediate"


def test_no_diagnostic_renders_bytes_from_a_foreign_stream(
    scripted_peer: object,
) -> None:
    """A refusal quotes no part of a header it did not recognise.

    `b"HTTP/1.1"` is eight bytes of somebody else's protocol. Read as a header it
    yields a declared length of 791752241, and reporting that number puts four
    bytes of peer material into a diagnostic in a module whose stated rule is
    that peer material is discarded -- and calls them "OVC1 body bytes" of a
    frame that is not one.
    """
    peer = scripted_peer(b"HTTP/1.1", hold=True)  # type: ignore[operator]

    with pytest.raises(ProtocolError) as raised:
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(5.0)
        )

    message = str(raised.value)
    assert "791752241" not in message
    assert "HTTP" not in message
    assert "magic" in message


def test_a_second_frame_after_the_answer_is_refused_rather_than_ignored(
    scripted_peer: object,
) -> None:
    """A stale first frame must not win by arriving first.

    `decode_frame` owns the trailing-byte rule, but it only ever sees the bytes
    this transport hands it -- exactly one declared frame -- so that branch can
    never fire from here. Without a check on the stream itself, a peer replying
    with two frames has the first accepted and the second silently dropped, and
    a caller is handed a stale answer with no indication anything was discarded.
    """
    stale = encode_frame({"answer": "STALE"})
    fresh = encode_frame({"answer": "real"})
    peer = scripted_peer(stale + fresh, hold=True)  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="trailing bytes"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_peer_that_dribbles_a_legal_frame_is_cut_off_by_the_whole_call_budget(
    scripted_peer: object,
) -> None:
    """Every wait is bounded by what is *left*, not by a fresh budget each time.

    The frame is legal and every individual byte arrives well inside any
    per-read timeout, so nothing but a deadline re-read on each pass can end
    this. Hoisting the deadline check out of the read loop -- the one refactor
    the loop's docstring warns against -- leaves every other test in this file
    green and lets a peer hold the process for as long as it keeps dribbling.
    """
    body = b'{"a":"bb"}'
    frame = b"OVC1" + len(body).to_bytes(4, "big") + body
    peer = scripted_peer(frame, dribble_seconds=0.6, hold=True)  # type: ignore[operator]

    started = time.monotonic()
    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(2.0)
        )
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"the call ran {elapsed:.2f}s past a 2.00s deadline"


def test_a_reply_declaring_a_zero_byte_body_is_a_protocol_error(
    scripted_peer: object,
) -> None:
    peer = scripted_peer(b"OVC1" + (0).to_bytes(4, "big"))  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="zero-byte"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_reply_declaring_more_than_the_frozen_maximum_is_refused_before_it_is_read(
    scripted_peer: object,
) -> None:
    """The length is judged against the frozen bound before the body is read.

    The peer sends the header and nothing else. If the bound were checked after
    the read, this would block until the deadline instead of refusing at once.
    """
    oversized = (4 * 1024 * 1024 + 1).to_bytes(4, "big")
    peer = scripted_peer(b"OVC1" + oversized)  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="maximum"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_truncated_reply_is_a_transport_error(scripted_peer: object) -> None:
    """The peer declares more body than it sends, then closes."""
    peer = scripted_peer(b"OVC1" + (64).to_bytes(4, "big") + b"{}")  # type: ignore[operator]

    with pytest.raises(TransportError, match="closed after"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_reply_whose_json_is_not_canonical_is_a_protocol_error(
    scripted_peer: object,
) -> None:
    """OVC1 admits canonical bytes only, and the client's decoder is what says so.

    The refusal is matched on `canonical` rather than merely on the exception
    type. These bytes are perfectly good JSON, so a transport that parsed the
    body itself instead of routing it through the accepted decoder would still
    fail this call -- later, on the envelope, for the wrong reason. Pinning the
    reason is what makes this test about admission rather than about luck.
    """
    body = b'{"b": 1, "a": 2}'
    peer = scripted_peer(b"OVC1" + len(body).to_bytes(4, "big") + body)  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="canonical"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_well_framed_reply_that_is_not_a_response_envelope_is_a_protocol_error(
    scripted_peer: object,
) -> None:
    """A frame can be perfect and still not be an answer."""
    peer = scripted_peer(encode_frame({"not": "an envelope"}))  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="response envelope"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_a_peer_that_answers_nothing_runs_out_of_deadline(scripted_peer: object) -> None:
    peer = scripted_peer(None)  # type: ignore[operator]

    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(0.5)
        )


def test_exactly_one_frame_is_written_and_nothing_follows_it(
    scripted_peer: object,
) -> None:
    """The server refuses a connection carrying trailing bytes, so this must hold.

    The peer reads until the client stops sending and keeps every byte. What it
    holds must be exactly the one encoded request frame -- not a prefix of it,
    and not one byte more.
    """
    request = _probe_request()
    peer = scripted_peer(None)  # type: ignore[operator]

    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            request, deadline=Deadline.after(1.0)
        )

    assert peer.received == encode_frame(codec.encode_request(request))


def test_an_endpoint_with_no_listener_is_a_transport_error(tmp_path: Path) -> None:
    missing = f"unix://{tmp_path / 'absent.sock'}"

    with pytest.raises(TransportError, match="could not be reached"):
        LocalIpcTransport(endpoint_uri=missing).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
        )


def test_an_expired_deadline_sends_nothing_at_all(scripted_peer: object) -> None:
    """The precondition is checked before the first byte, not after the connect."""
    peer = scripted_peer(None)  # type: ignore[operator]

    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(0.0)
        )

    assert peer.received == b""


def test_a_cancelled_call_sends_nothing_at_all(scripted_peer: object) -> None:
    """Cancellation is reported as cancellation, and is tested before the clock."""
    peer = scripted_peer(None)  # type: ignore[operator]
    token = CancellationToken()
    token.cancel()

    with pytest.raises(OperationCancelledError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(),
            deadline=Deadline.after(CALL_TIMEOUT),
            cancellation=token,
        )

    assert peer.received == b""


def test_a_non_local_endpoint_is_refused_rather_than_dialled() -> None:
    """Only the installation-local scheme, and a Windows pipe says so in its own words."""
    for endpoint in ("pipe://omnivia-core", "http://127.0.0.1:9999", "/bare/path.sock"):
        with pytest.raises(TransportError, match="unix://"):
            socket_path_for(endpoint)


def test_an_endpoint_naming_no_path_is_refused() -> None:
    with pytest.raises(TransportError, match="no local socket path"):
        socket_path_for("unix://")


def test_the_socket_path_is_the_endpoints_own_path() -> None:
    assert socket_path_for("unix:///run/omnivia/s.sock") == "/run/omnivia/s.sock"


# ---------------------------------------------------------------------------
# The failure carries no exception it was translated from
# ---------------------------------------------------------------------------


def test_a_refused_dial_leaves_no_operating_system_error_on_the_exception() -> None:
    """`__context__` is `None`, not merely a quiet traceback.

    The module's rule is that a failure "keeps its kind and loses its words".
    `raise X from None` inside the handler would have satisfied a rendered
    traceback and still left `__context__` pointing at an `OSError` whose message
    names this socket path -- one attribute access from anything that logs or
    serialises the error a caller caught. `scripts/check-raise-discipline.py`
    enforces the shape; this asserts the property that shape exists for.

    The path is asserted absent from every string the exception offers, because
    the point is disclosure, not tidiness.

    `_short_socket_directory`, not `tmp_path`: under `tmp_path` the connect fails
    with "AF_UNIX path too long" before it ever reaches the endpoint, so this
    would exercise the 86-byte ceiling while claiming to exercise a refused dial.
    Both land in the same `except OSError`, which is what made the substitution
    invisible -- and both leak, so the assertion held either way and the name did
    not.
    """
    directory = _short_socket_directory()
    missing = directory / "absent.sock"
    try:
        with pytest.raises(TransportError) as raised:
            LocalIpcTransport(endpoint_uri=f"unix://{missing}").call(
                _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)
            )
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    error = raised.value
    assert error.__context__ is None
    assert error.__cause__ is None
    assert str(missing) not in str(error)
    assert str(missing) not in repr(error)

