"""V06-2 Lane B: the CLI is the first production caller of the authorised vertical.

Two things are proven here, and they need different machinery, which is why one
file holds both.

**The vertical, against a real server.** `omnivia workspace show` runs as a
subprocess against an `omnivia-core-service` started from its own entry point,
over a real Unix domain socket, carrying a real OVC1 frame. Nothing on that path
is stubbed: not the server, not the transport, not the frame, not the workspace.
A stubbed server would prove the CLI can talk to a fixture, which is not the
claim.

**The transport, against a server that misbehaves.** A real listening socket
that answers with bytes the test chooses. The framing and error paths cannot be
reached from a correct server -- a correct server never sends a truncated frame
-- so they get a peer that does, rather than a mock of the socket module. The
bytes are real and the failure is the transport's own.

`packages/omnivia-core-cli/tests` is collected by `core-acceptance.yml`'s
full-suite step, and by nothing else. `phase2-platform.yml` does not name this
tree -- it names `packages/omnivia-core-runtime/tests/phase2` only. It does now
install the client, per packet section 17b.2, but that is so its own CLI install
can resolve; it collects nothing from here either way. The acceptance guard
`tests/test_core_acceptance_workflow.py::test_every_local_distribution_with_tests_is_in_the_broad_pytest_run`
fails closed if this directory exists and that step does not name it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_cli.client import build_request, read_descriptor
from omnivia_core_cli.main import (
    WORKSPACE_INSPECT_OPERATION,
    WORKSPACE_INSPECTION_PURPOSE,
)
from omnivia_core_cli.transport import LocalIpcTransport, socket_path_for
from omnivia_core_client import (
    Deadline,
    DeadlineExceededError,
    ProtocolError,
    TransportError,
    decode_frame,
    encode_frame,
)
from omnivia_core_client.deadline import CancellationToken
from omnivia_core_client.errors import OperationCancelledError

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    codec,
    get_operation_metadata,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

WORKSPACE_ID = "ws-lane-b-vertical-01"
SERVICE_INSTANCE = "svc-lane-b-1"
CALL_TIMEOUT = 10.0


def _short_socket_directory() -> Path:
    """A socket directory outside `tmp_path`.

    R004-15 caps a local endpoint at 86 encoded bytes -- what `sockaddr_un`'s
    104-byte `sun_path` leaves after the NUL terminator and the runtime's
    fixed-width staging name -- and pytest's `tmp_path` nests deep enough to
    exceed it. Only the socket has the limit, so only the socket gets the short
    path.
    """
    return Path(tempfile.mkdtemp(prefix="ovb-", dir=tempfile.gettempdir()))


# ---------------------------------------------------------------------------
# A real service, started from the real entry point
# ---------------------------------------------------------------------------


class LiveService:
    """One running `omnivia-core-service` and the facts a caller needs to reach it."""

    def __init__(self, runtime_directory: Path, endpoint_uri: str) -> None:
        self.runtime_directory = runtime_directory
        self.endpoint_uri = endpoint_uri


@pytest.fixture(scope="module")
def live_service() -> Iterator[LiveService]:
    """A real workspace, owned by a real service process, for the module.

    Module-scoped because starting one costs a migration and a startup sequence,
    and every test here reads the same served workspace without changing it --
    the one operation this vertical exposes has `side_effect: none`, so no test
    can leave the workspace different for the next.

    The runtime is imported *here*, in the test process, to build the workspace
    the service will own. The CLI under test never imports it: it is exercised as
    a separate process, which is the only arrangement in which "the CLI does not
    import the runtime" is proven rather than asserted.
    """
    from omnivia_core_runtime.ownership.discovery import discover
    from omnivia_core_runtime.service.transport import endpoint_for_path
    from omnivia_core_runtime.storage.backup import InstallationLayout
    from omnivia_core_runtime.storage.legacy import migrate_legacy_database
    from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
    from omnivia_core_runtime.workspace.layout import WorkspaceLayout

    from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

    root = Path(tempfile.mkdtemp(prefix="ovb-workspace-"))
    legacy = root / "legacy" / "source.sqlite"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(legacy)

    workspace = WorkspaceLayout(root=root / "workspace")
    installation = InstallationLayout(root=root / "installation-state")
    installation.create(WORKSPACE_ID)
    migrate_legacy_database(
        legacy,
        workspace,
        installation,
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at="2026-07-30T00:00:00+00:00",
            name="Lane B vertical",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id=SERVICE_INSTANCE,
    )

    socket_directory = _short_socket_directory()
    endpoint = endpoint_for_path(socket_directory / "s.sock")
    runtime_directory = installation.runtime_for(WORKSPACE_ID)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnivia_core_runtime.service.main",
            "--workspace",
            str(workspace.root),
            "--installation-state",
            str(installation.root),
            "--endpoint",
            endpoint.url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        found = None
        while time.monotonic() < deadline:
            assert process.poll() is None, "the service exited instead of serving"
            found = discover(runtime_directory)
            if found is not None and found.ready:
                break
            time.sleep(0.05)
        assert found is not None and found.ready, "the service never became ready"
        yield LiveService(runtime_directory, endpoint.url)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
                process.kill()
                process.wait(timeout=10)
        shutil.rmtree(socket_directory, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _run_cli(runtime_directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """The installed CLI, as its own process."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--runtime-state",
            str(runtime_directory),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_workspace_show_answers_from_the_live_service(live_service: LiveService) -> None:
    """Packet 12.2(2): the whole vertical, end to end, over the real transport.

    A real service process, the descriptor it actually published, the real OVC1
    frame, the production application path, and the workspace descriptor coming
    back out of the CLI's own stdout.
    """
    result = _run_cli(live_service.runtime_directory, "workspace", "show")

    assert result.returncode == 0, (result.stdout, result.stderr)
    rendered = json.loads(result.stdout)
    assert rendered["workspace"]["workspace_id"] == WORKSPACE_ID
    assert rendered["workspace"]["display_name"] == "Lane B vertical"
    assert rendered["workspace"]["status"] == "active"


def test_workspace_show_reports_no_service_when_none_is_advertised(
    tmp_path: Path,
) -> None:
    """An absent service is the CLI's ordinary answer, not a traceback."""
    result = _run_cli(tmp_path, "workspace", "show")

    assert result.returncode == 1
    assert "no service is advertised" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_symlinked_runtime_state_answers_the_same_as_a_direct_one(
    live_service: LiveService, tmp_path: Path
) -> None:
    """One flag, one answer. `discover` and `workspace show` must not disagree.

    `workspace show` has to re-derive the installation root to call discovery,
    and deriving it from the *unresolved* path makes a symlinked
    `--runtime-state` land somewhere else -- so `discover` exits 0 and prints the
    endpoint while `workspace show` exits 1 on the same argument. Worse than the
    inconsistency: when the two paths reach different files, discovery runs its
    provenance, mode, scheme and liveness checks on one descriptor while the
    call is dialled from the other.
    """
    linked = tmp_path / "runtime-link"
    linked.symlink_to(live_service.runtime_directory, target_is_directory=True)

    discovered = _run_cli(linked, "discover")
    shown = _run_cli(linked, "workspace", "show")

    assert discovered.returncode == 0, (discovered.stdout, discovered.stderr)
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    assert json.loads(shown.stdout)["workspace"]["workspace_id"] == WORKSPACE_ID


def test_the_subcommands_that_predate_this_lane_still_behave(
    live_service: LiveService,
) -> None:
    """`discover` still makes no call, and now says so in its own output.

    R004-13 keeps `discover` a pure reader of the published descriptor and adds
    an honesty requirement to the machine-readable form: it must carry enough
    context to tell a descriptor observation from a live probe. So the readiness
    key is `advertised_ready`, and `observation` names where it came from.

    The bare `ready` key is asserted absent, not merely renamed. A script that
    kept reading `ready` would otherwise go on getting the stale claim from a
    key that quietly changed meaning; missing is a better failure than wrong.
    """
    result = _run_cli(live_service.runtime_directory, "discover")

    assert result.returncode == 0, (result.stdout, result.stderr)
    reported = json.loads(result.stdout)
    assert reported["endpoint"] == live_service.endpoint_uri
    assert reported["workspace_id"] == WORKSPACE_ID
    assert reported["advertised_ready"] is True
    assert reported["observation"] == "published-descriptor"
    assert "ready" not in reported


# ---------------------------------------------------------------------------
# Negative test 10 -- determinism through the approved local adapter
# ---------------------------------------------------------------------------


def _inspect_request(
    *,
    request_id: str,
    purpose: str = WORKSPACE_INSPECTION_PURPOSE,
    scopes: tuple[str, ...] | None = None,
    principal: str | None = None,
) -> object:
    entry = get_operation_metadata(WORKSPACE_INSPECT_OPERATION)
    required = entry.required_capability
    return build_request(
        WORKSPACE_INSPECT_OPERATION,
        workspace_id=WORKSPACE_ID,
        request_id=request_id,
        principal=principal,
        scopes=tuple(entry.scope.required_scopes) if scopes is None else scopes,
        purpose=purpose,
        required_capabilities=(
            CapabilityRequirement(
                id=required.id,
                minimum_version=required.minimum_version,
                required=required.required,
            ),
        ),
    )


def _authorisation_outcome(endpoint_uri: str, request: object) -> str:
    """What the seam decided, with what it did not decide stripped out.

    The correlation identifiers echo the request, so two runs of the same session
    inputs differ in them by construction; leaving them in would make this
    compare request ids rather than authority. They are the *only* thing removed.

    Everything else the answer carries is compared, including the two a narrower
    projection would have dropped for no reason: `retry_class`, which is what a
    caller branches on to decide whether retrying is even meaningful, and the
    whole `result` body rather than a flag saying one existed. Neither is a
    correlation identifier and both are constant for constant inputs, so
    comparing them costs nothing and closes the gap where a difference could hide.
    """
    transport = LocalIpcTransport(endpoint_uri=endpoint_uri)
    response = transport.call(request, deadline=Deadline.after(CALL_TIMEOUT))  # type: ignore[arg-type]
    document = codec.encode_response(response)
    error = document.get("error", {})
    metadata = document["metadata"]
    return json.dumps(
        {
            "code": error.get("code"),
            "message": error.get("message"),
            "retry_class": error.get("retry_class"),
            "authority": metadata["authority"],
            "capabilities": metadata["version"]["capabilities"],
            "answered": "result" in document,
            "result": document.get("result"),
        },
        sort_keys=True,
    )


def test_ten_the_same_session_inputs_give_the_same_authorisation_outcome(
    live_service: LiveService,
) -> None:
    """Negative test 10, the last of the owner's ten and the only one Lane B carries.

    "The same session inputs produce the same authorisation outcome through the
    approved local adapter." Determinism, not a repeat of refusals 1-9.

    Both halves are asserted, and the second is what stops the first being a
    tautology. Repeating one set of inputs must give one outcome -- but so would
    an adapter that returned a constant, or one that dropped the session inputs
    on the floor before framing them. So three *different* sets of inputs are
    each run twice, and the three outcomes must be pairwise distinct. Together
    that says the outcome is a function of the session inputs: stable in them,
    and varying with them.
    """
    endpoint = live_service.endpoint_uri
    cases = {
        "granted": {},
        "purpose the session may not act for": {"purpose": "cli"},
        "scope the session does not grant": {
            "scopes": ("workspace:read", "workspace:write")
        },
    }

    outcomes: dict[str, str] = {}
    for label, overrides in cases.items():
        first = _authorisation_outcome(
            endpoint, _inspect_request(request_id="cli-det-a", **overrides)  # type: ignore[arg-type]
        )
        second = _authorisation_outcome(
            endpoint, _inspect_request(request_id="cli-det-b", **overrides)  # type: ignore[arg-type]
        )
        assert first == second, f"{label}: the same session inputs gave two outcomes"
        outcomes[label] = first

    distinct = set(outcomes.values())
    assert len(distinct) == len(outcomes), (
        "the outcome does not vary with the session inputs, so its stability "
        f"proves nothing: {outcomes}"
    )

    granted = json.loads(outcomes["granted"])
    assert granted["answered"] is True
    assert granted["code"] is None
    assert granted["authority"]["principal_id"] == "local-user"
    assert granted["authority"]["roles"] == []


def test_a_repeated_principal_claim_is_refused_the_same_way_every_time(
    live_service: LiveService,
) -> None:
    """The determinism of a refusal, on the claim that most matters.

    A request cannot replace the authenticated principal (refusal 4, which Lane A
    proves at the seam). What Lane B adds is that carrying it over the approved
    local adapter does not change the answer, and does not change it between
    runs.
    """
    outcomes = {
        _authorisation_outcome(
            live_service.endpoint_uri,
            _inspect_request(request_id=f"cli-claim-{index}", principal="somebody-else"),
        )
        for index in range(3)
    }

    assert len(outcomes) == 1
    decided = json.loads(outcomes.pop())
    assert decided["code"] == "authorization_denied"
    assert decided["answered"] is False
    # The refusal names no caller-supplied value: the claim is not echoed.
    assert "somebody-else" not in decided["message"]


# ---------------------------------------------------------------------------
# The transport's framing and error paths
# ---------------------------------------------------------------------------


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
        peer = ScriptedPeer(reply, **options)  # type: ignore[arg-type]
        peers.append(peer)
        return peer

    yield make
    for peer in peers:
        peer.close()


def _probe_request(request_id: str = "cli-frame-1") -> object:
    return _inspect_request(request_id=request_id)


def test_a_reply_with_the_wrong_magic_is_a_protocol_error(scripted_peer: object) -> None:
    peer = scripted_peer(b"XXXX" + (4).to_bytes(4, "big") + b"{}\n\n")  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="magic"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(5.0)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(5.0)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(2.0)  # type: ignore[arg-type]
        )
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"the call ran {elapsed:.2f}s past a 2.00s deadline"


def test_a_reply_declaring_a_zero_byte_body_is_a_protocol_error(
    scripted_peer: object,
) -> None:
    peer = scripted_peer(b"OVC1" + (0).to_bytes(4, "big"))  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="zero-byte"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
        )


def test_a_truncated_reply_is_a_transport_error(scripted_peer: object) -> None:
    """The peer declares more body than it sends, then closes."""
    peer = scripted_peer(b"OVC1" + (64).to_bytes(4, "big") + b"{}")  # type: ignore[operator]

    with pytest.raises(TransportError, match="closed after"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
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
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
        )


def test_a_well_framed_reply_that_is_not_a_response_envelope_is_a_protocol_error(
    scripted_peer: object,
) -> None:
    """A frame can be perfect and still not be an answer."""
    peer = scripted_peer(encode_frame({"not": "an envelope"}))  # type: ignore[operator]

    with pytest.raises(ProtocolError, match="response envelope"):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
        )


def test_a_peer_that_answers_nothing_runs_out_of_deadline(scripted_peer: object) -> None:
    peer = scripted_peer(None)  # type: ignore[operator]

    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(0.5)  # type: ignore[arg-type]
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
            request, deadline=Deadline.after(1.0)  # type: ignore[arg-type]
        )

    assert peer.received == encode_frame(codec.encode_request(request))  # type: ignore[arg-type]


def test_an_endpoint_with_no_listener_is_a_transport_error(tmp_path: Path) -> None:
    missing = f"unix://{tmp_path / 'absent.sock'}"

    with pytest.raises(TransportError, match="could not be reached"):
        LocalIpcTransport(endpoint_uri=missing).call(
            _probe_request(), deadline=Deadline.after(CALL_TIMEOUT)  # type: ignore[arg-type]
        )


def test_an_expired_deadline_sends_nothing_at_all(scripted_peer: object) -> None:
    """The precondition is checked before the first byte, not after the connect."""
    peer = scripted_peer(None)  # type: ignore[operator]

    with pytest.raises(DeadlineExceededError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(), deadline=Deadline.after(0.0)  # type: ignore[arg-type]
        )

    assert peer.received == b""


def test_a_cancelled_call_sends_nothing_at_all(scripted_peer: object) -> None:
    """Cancellation is reported as cancellation, and is tested before the clock."""
    peer = scripted_peer(None)  # type: ignore[operator]
    token = CancellationToken()
    token.cancel()

    with pytest.raises(OperationCancelledError):
        LocalIpcTransport(endpoint_uri=peer.endpoint_uri).call(
            _probe_request(),  # type: ignore[arg-type]
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
# The boundary this lane must not cross
# ---------------------------------------------------------------------------


#: Names that turn a module identifier from something a reader can see into
#: something computed at run time. The CLI uses none of them and has no reason to,
#: so their presence anywhere in this tree is a stop condition rather than a thing
#: to inspect -- which is the only form in which a *static* scan can say anything
#: about dynamic loading at all. See the docstring below for what that buys.
DYNAMIC_IMPORT_MACHINERY = (
    "import_module",
    "__import__",
    "load_module",
    "exec_module",
    "spec_from_file_location",
    "exec",
    "eval",
    "compile",
)


def test_the_cli_source_opens_no_authoritative_storage_and_imports_no_runtime() -> None:
    """Refusal 8, restated over the files this lane adds.

    The existing guards in `test_service_and_adapters.py` walk the whole CLI
    source tree and so already cover `transport.py`. This is here because that
    file belongs to another distribution's suite and another lane's manifest: a
    reader of *this* lane should be able to see the boundary held without
    leaving it, and if the CLI tree ever moves out from under that walk, this
    fails too.

    **What this test can and cannot catch.** It reads source and nothing else, so
    it catches exactly what is written down: a literal `import sqlite3` or `import
    omnivia_core_runtime`, a call to one of the four named lock and lease
    primitives, and -- the addition -- any use of the machinery that would let a
    module name be computed rather than written. It cannot catch a computed import
    that reaches the interpreter by some route not named in
    `DYNAMIC_IMPORT_MACHINERY`, it cannot evaluate anything, and it cannot see what
    a C extension does. It also says nothing about *behaviour*: a module can be
    imported and never used, or used and never reach storage.

    That limit is not theoretical. `importlib.import_module("sql" + "ite3")` in
    `lifecycle.py` passed this file at its exact baseline and left an 8 KB database
    on disk, because the name never appears as a name. The three checks below
    would now fail on `import_module`, but the general case does not close: the
    machinery list is a list, and any list of names can be gone round.

    **So the claim itself is proved elsewhere, by execution.**
    `test_init.py::test_no_command_opens_a_database_or_imports_the_runtime_in_this_process`
    runs `init`, `start`, `status` and `stop` under a CPython audit hook and
    asserts no `sqlite3.connect` event was raised by any route. This test is the
    cheap, readable, whole-tree half; that one is the half that is actually
    load-bearing for "the CLI opens no database". Neither is redundant: this one
    reads every module including ones no command exercises, and that one sees
    through any amount of indirection in the four commands it runs.
    """
    import ast

    source_root = Path(__file__).resolve().parents[1] / "src" / "omnivia_core_cli"
    modules = sorted(source_root.rglob("*.py"))
    assert modules, "the CLI source tree was not found"

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)

        assert "omnivia_core_runtime" not in imported, f"{path} imports the runtime"
        assert "sqlite3" not in imported, f"{path} imports sqlite3"
        assert "importlib" not in imported, f"{path} imports importlib"
        for forbidden in ("acquire_lease", "flock", "open_guard", "create_lock"):
            assert forbidden not in called, f"{path} calls {forbidden}"
        for machinery in DYNAMIC_IMPORT_MACHINERY:
            assert machinery not in called, (
                f"{path} calls {machinery}: a computed module name is outside what "
                "any source scan can decide, so it is refused rather than inspected"
            )


def test_the_client_package_still_ships_no_socket() -> None:
    """Why the concrete transport lives here and not in `omnivia-core-client`.

    The client's accepted isolation test names `socket` forbidden and pins its
    module list exactly, so a concrete socket transport cannot be added to that
    distribution without relaxing an accepted boundary. This asserts the premise
    that argument rests on, so the day it stops being true this lane's file
    placement is re-examined rather than silently left where it is.
    """
    import ast

    import omnivia_core_client

    client_root = Path(omnivia_core_client.__file__).resolve().parent
    # `rglob`, not `glob`: a socket transport added to the client as a
    # subpackage would sit under a directory this test must still see.
    for path in sorted(client_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] != "socket", path.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "socket", path.name


#: Peer-credential primitives. Adding one under this packet is a stop condition, and
#: the deferral that covers the gap is `LOCAL-IPC-PEER-IDENTITY-DEFERRED`.
PEER_CREDENTIAL_PRIMITIVES = ("SO_PEERCRED", "LOCAL_PEERCRED", "getpeereid", "getsockopt")

#: Words that would turn a string a caller or an auditor reads into a claim that the
#: peer was authenticated. The list, and the reason it is checked against emitted
#: strings only, are Lane A's: the prose in these modules necessarily quotes the claim
#: in order to prohibit it, so a text scan would fire on the prohibition itself.
FORBIDDEN_IN_EMITTED_STRINGS = ("peer", "authenticat", "verified", "os user")


def _cli_modules() -> list[Path]:
    source_root = Path(__file__).resolve().parents[1] / "src" / "omnivia_core_cli"
    modules = sorted(source_root.rglob("*.py"))
    assert modules, "the CLI source tree was not found"
    # `__init__.py` is included deliberately. Excluding it left the one module
    # these scans never read, and it is exactly where a stale claim survived.
    assert any(path.name == "__init__.py" for path in modules)
    return modules


def test_no_module_this_lane_adds_reaches_for_a_peer_credential_primitive() -> None:
    """The stop condition, as a test."""
    for path in _cli_modules():
        source = path.read_text(encoding="utf-8")
        for primitive in PEER_CREDENTIAL_PRIMITIVES:
            assert primitive not in source, f"{path.name} reaches for {primitive}"


def test_no_string_the_cli_emits_claims_a_verified_operating_system_peer() -> None:
    """The section 17a.3 prohibition, over this lane's own source.

    Lane A enforces this with an AST scan over the three runtime modules it owns.
    Those three are named in that file, so `transport.py` and `main.py` are
    outside it and the scan cannot see them -- the rule is applied here instead,
    by the same method and with the same word list, because a lane that adds a
    caller adds new places for a false claim to be emitted from.

    Every string constant that is not a docstring: the refusal text this CLI
    writes to stderr, the scheme constant, the endpoint diagnostics. Those are
    the values that reach a user, a log and an auditor.
    """
    import ast

    for path in _cli_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        emitted = [
            node.value.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        assert emitted, f"{path.name}: nothing was scanned, so this proves nothing"

        for value in emitted:
            for claim in FORBIDDEN_IN_EMITTED_STRINGS:
                assert claim not in value, f"{path.name} emits {value!r}"


def test_the_cli_reads_no_environment_variable_to_find_a_service() -> None:
    """No caller-selected path, and no *ambient* one either.

    The property is that **no unrestricted ambient filesystem path is accepted**.
    An environment lookup is exactly that -- a path of the caller's choosing
    arriving by another name -- and it stays a stop condition for this packet.

    Two path sources are admitted, and neither is ambient: an explicit argument,
    which the caller states and a reader can see; and a deterministic built-in
    default, which is the same on every machine and which nothing outside this
    source can redirect. Owner resolution 004 R004-11 admits both and keeps the
    environment prohibition, and it also required this docstring corrected --
    it previously said `--runtime-state` was "the only way in", which stopped
    being true when the `~/.omnivia` convention shipped.

    The assertion below is unchanged. R004-11 says so in terms: do not weaken or
    delete the test merely to make the current implementation pass.
    """
    import ast

    source_root = Path(__file__).resolve().parents[1] / "src" / "omnivia_core_cli"
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
                raise AssertionError(f"{path.name} reads the environment")
    assert os.name in {"posix", "nt"}


# ---------------------------------------------------------------------------
# The two guards, in the direction they fire
# ---------------------------------------------------------------------------
#
# Both defend against something a correct service never does, so neither can be
# provoked by a well-behaved peer. That is not a reason to leave them untested --
# it is the reason to be explicit about the seam each test substitutes, and to
# substitute the smallest thing that makes the guard's own scenario real.


def _recv_exact(connection: socket.socket, count: int) -> bytes | None:
    buffer = b""
    while len(buffer) < count:
        chunk = connection.recv(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer


def _recv_frame(connection: socket.socket) -> bytes | None:
    """One whole OVC1 frame as raw bytes, header included."""
    header = _recv_exact(connection, 8)
    if header is None:
        return None
    body = _recv_exact(connection, int.from_bytes(header[4:8], "big"))
    return None if body is None else header + body


class RelayingPeer:
    """A listener that forwards every frame to the real service and back, verbatim.

    The point is that it is *honest on the wire*: because it relays the discovery
    probe unchanged, discovery genuinely succeeds -- provenance, directory modes,
    scheme, version negotiation and the live identity check all pass, against the
    real descriptor. Nothing is stubbed and no check is bypassed. What it changes
    is only which socket the CLI was pointed at.

    It records the operation of every frame it is asked to carry, so a test can
    assert what did and did not reach it.
    """

    def __init__(self, target_path: str) -> None:
        self.directory = _short_socket_directory()
        self.path = self.directory / "relay.sock"
        self.operations: list[str] = []
        self._target = target_path
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(8)
        self._server.settimeout(1.0)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def endpoint_uri(self) -> str:
        return f"unix://{self.path}"

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:  # pragma: no cover - teardown
                return
            threading.Thread(target=self._carry, args=(connection,), daemon=True).start()

    def _carry(self, client: socket.socket) -> None:
        with client:
            client.settimeout(30)
            frame = _recv_frame(client)
            if frame is None:  # pragma: no cover - client gave up
                return
            try:
                document = decode_frame(frame)
                self.operations.append(
                    str(document.get("operation") or document.get("probe") or "?")
                )
            except Exception:  # noqa: BLE001 - a test recorder, not a decoder
                self.operations.append("?")
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.settimeout(30)
            try:
                with upstream:
                    upstream.connect(self._target)
                    upstream.sendall(frame)
                    reply = _recv_frame(upstream)
                if reply is not None:
                    client.sendall(reply)
            except OSError:  # pragma: no cover - service went away
                return

    def close(self) -> None:
        self._stopped.set()
        self._server.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


def test_a_descriptor_discovery_never_validated_is_not_dialled(
    live_service: LiveService,
) -> None:
    """The descriptor-equality refusal, in the direction it fires.

    A second descriptor is planted beside the real one, byte-identical except
    that `endpoint_uri` points at a relay rather than at the service. Pointing
    `--runtime-state` at it splits the two halves of the call apart: discovery
    re-derives its path from the installation root and so reads and validates the
    *real* descriptor, and it succeeds honestly, because the relay forwards its
    probe to the real socket. The transport, meanwhile, was built from the
    descriptor that was actually read.

    That is the whole hazard in one sentence -- discovery vetted descriptor A
    while the transport would dial descriptor B -- and the only thing that stops
    it is comparing the two. Whoever controls the relay would otherwise receive
    an authorised `workspace.inspect` and could answer it with anything.

    Asserting the refusal alone would be weak, because a discovery failure would
    produce the same exit code and the same message. So the relay's own record is
    asserted in both directions: the probe reached it, which is what proves
    discovery really ran and really passed, and no `workspace.inspect` ever did.
    """
    published = json.loads(
        (live_service.runtime_directory / "service.json").read_text(encoding="utf-8")
    )
    relay = RelayingPeer(socket_path_for(live_service.endpoint_uri))
    decoy_directory = live_service.runtime_directory.parent / "decoy"
    try:
        published["endpoint_uri"] = relay.endpoint_uri
        decoy_directory.mkdir(mode=0o700, exist_ok=True)
        decoy = decoy_directory / "service.json"
        decoy.write_text(json.dumps(published), encoding="utf-8")
        decoy.chmod(0o600)

        result = _run_cli(decoy_directory, "workspace", "show")

        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "did not pass its discovery checks" in result.stderr
        assert result.stdout == ""
        # Discovery genuinely ran and genuinely passed -- so the refusal is the
        # equality check, not a discovery failure wearing the same message.
        assert "service.discover" in relay.operations, relay.operations
        # And the authorised request never left this process.
        assert WORKSPACE_INSPECT_OPERATION not in relay.operations, relay.operations
    finally:
        relay.close()
        shutil.rmtree(decoy_directory, ignore_errors=True)


def test_a_response_correlating_to_another_request_is_refused(
    live_service: LiveService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The correlation refusal, in the direction it fires.

    A correct service echoes the correlation id it was sent, and this transport
    opens a fresh connection per call, so nothing a real peer does can produce a
    mismatch -- which is exactly why the substituted seam is the *response* and
    nothing else. The service is real, the socket is real, the frame is real, the
    request is the one the CLI built; only the answer's correlation id is
    rewritten on the way back, which is the one condition the guard exists for.

    Run in-process rather than as a subprocess because the substitution has to
    sit between the transport and the caller, which is inside the CLI.
    """
    from omnivia_core_cli import main as cli_main

    service = read_descriptor(live_service.runtime_directory)
    assert service is not None

    genuine_call = LocalIpcTransport.call

    def answer_a_different_request(
        self: LocalIpcTransport, request: object, **keywords: object
    ) -> object:
        response = genuine_call(self, request, **keywords)  # type: ignore[arg-type]
        document = json.loads(json.dumps(codec.encode_response(response)))
        document["metadata"]["correlation_id"] = "cli-somebody-elses-call"
        return codec.decode_response(document)

    monkeypatch.setattr(LocalIpcTransport, "call", answer_a_different_request)

    scopes, required_capabilities = cli_main._inspect_claims()
    assert (
        cli_main._call(
            live_service.runtime_directory,
            service,
            WORKSPACE_INSPECT_OPERATION,
            scopes=scopes,
            purpose=WORKSPACE_INSPECTION_PURPOSE,
            required_capabilities=required_capabilities,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "answered a different request" in captured.err
    assert captured.out == ""


def test_neither_guard_fires_on_the_paths_a_correct_service_produces(
    live_service: LiveService, tmp_path: Path
) -> None:
    """Both refusals are silent on every legitimate shape, direct and symlinked.

    The companion to the two tests above: a guard that fires when it should is
    only half the property, and a guard that fires when it should not would be
    caught here rather than by a user.
    """
    linked = tmp_path / "runtime-link"
    linked.symlink_to(live_service.runtime_directory, target_is_directory=True)

    for runtime_state in (live_service.runtime_directory, linked):
        result = _run_cli(runtime_state, "workspace", "show")
        assert result.returncode == 0, (runtime_state, result.stdout, result.stderr)
        assert "did not pass its discovery checks" not in result.stderr
        assert "answered a different request" not in result.stderr
        assert json.loads(result.stdout)["workspace"]["workspace_id"] == WORKSPACE_ID


# ---------------------------------------------------------------------------
# `health` and `readiness` answer from the service, or not at all
# ---------------------------------------------------------------------------


def test_health_answers_from_the_live_service(live_service: LiveService) -> None:
    """`health` reports what the service said, not what the CLI would have asked.

    The regression this pins: `health` used to build a `core.health` envelope
    and print it, so its stdout was a *request* -- no `status`, no `state`, and
    exit 0 whether or not anything was listening.
    """
    result = _run_cli(live_service.runtime_directory, "health")

    assert result.returncode == 0, (result.stdout, result.stderr)
    reported = json.loads(result.stdout)
    assert reported["status"] == "alive"
    assert reported["state"] == "ready"
    # The envelope's own keys must not be here: their presence would mean the
    # request was rendered instead of the answer.
    assert "operation" not in reported
    assert "metadata" not in reported


def test_readiness_answers_from_the_live_service(live_service: LiveService) -> None:
    """`readiness` reports the service's own writable-readiness verdict."""
    result = _run_cli(live_service.runtime_directory, "readiness")

    assert result.returncode == 0, (result.stdout, result.stderr)
    reported = json.loads(result.stdout)
    assert reported["ready"] is True
    assert reported["state"] == "ready"
    assert reported["unmet"] == []
    assert "operation" not in reported


@pytest.mark.parametrize("command", ["health", "readiness"])
def test_health_and_readiness_fail_when_the_advertised_service_is_gone(
    live_service: LiveService, tmp_path: Path, command: str
) -> None:
    """The launcher trap, pinned.

    A descriptor advertising `ready: true` outlives the process that published
    it -- a crash, a kill, a stale installation -- and it is the only thing a
    client has to go on. Before the fix both commands parsed that descriptor,
    printed the envelope they would have sent and exited **0**, so a launcher
    polling `readiness` was told a dead service was ready.

    Nothing is listening on the endpoint this descriptor names. The command has
    to fail visibly: a non-zero exit and no success-shaped object on stdout.
    """
    published = json.loads(
        (live_service.runtime_directory / "service.json").read_text(encoding="utf-8")
    )
    assert published["ready"] is True, "the fixture must advertise a ready service"

    # A real, short, and deliberately unbound socket path: the endpoint exists as
    # a claim in the descriptor and as nothing else.
    dead = _short_socket_directory()
    try:
        published["endpoint_uri"] = f"unix://{dead / 'gone.sock'}"
        assert not (dead / "gone.sock").exists()
        (tmp_path / "service.json").write_text(
            json.dumps(published), encoding="utf-8"
        )

        result = _run_cli(tmp_path, command)

        assert result.returncode != 0, (result.stdout, result.stderr)
        assert result.stdout == "", "a service that was never reached was reported on"
        assert result.stderr.strip() != ""
        assert "Traceback" not in result.stderr
    finally:
        shutil.rmtree(dead, ignore_errors=True)


def test_json_prints_the_envelope_without_dialling(live_service: LiveService) -> None:
    """`--json` is the opt-in the flag always advertised, and it is not an answer.

    The flag was declared `emit the request envelope` and never read, while the
    command emitted the envelope unconditionally. Now it is the only way to get
    the envelope, and what comes out is a request: an `operation` and a
    `metadata`, and none of the service's own fields.
    """
    result = _run_cli(live_service.runtime_directory, "health", "--json")

    assert result.returncode == 0, (result.stdout, result.stderr)
    envelope = json.loads(result.stdout)
    assert envelope["operation"] == "core.health"
    assert envelope["metadata"]["workspace_id"] == WORKSPACE_ID
    assert "status" not in envelope
