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
full-suite step, and by nothing else. That step is where the client distribution
is installed; `phase2-platform.yml` does not install it and does not name this
tree. The acceptance guard
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
from omnivia_core_cli.client import build_request
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

    `sockaddr_un` caps a path at 104 bytes on macOS, and pytest's `tmp_path`
    nests deep enough to exceed it. Only the socket has the limit, so only the
    socket gets the short path.
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


def test_the_subcommands_that_predate_this_lane_still_behave(
    live_service: LiveService,
) -> None:
    """`discover` is unchanged: same exit code, same keys, still no call made."""
    result = _run_cli(live_service.runtime_directory, "discover")

    assert result.returncode == 0, (result.stdout, result.stderr)
    reported = json.loads(result.stdout)
    assert reported["endpoint"] == live_service.endpoint_uri
    assert reported["workspace_id"] == WORKSPACE_ID
    assert reported["ready"] is True


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
    compare request ids rather than authority. What is left is the decision: the
    error code and its frozen message where there is one, the granted authority,
    the capability envelope, and whether a result was produced at all.
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
            "authority": metadata["authority"],
            "capabilities": metadata["version"]["capabilities"],
            "answered": "result" in document,
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

    def __init__(self, reply: bytes | None) -> None:
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
                connection.sendall(self._reply)
            except OSError:  # pragma: no cover - client may have gone
                pass

    def close(self) -> None:
        self._released.set()
        self._server.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


@pytest.fixture
def scripted_peer() -> Iterator[object]:
    peers: list[ScriptedPeer] = []

    def make(reply: bytes | None) -> ScriptedPeer:
        peer = ScriptedPeer(reply)
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


def test_the_cli_source_opens_no_authoritative_storage_and_imports_no_runtime() -> None:
    """Refusal 8, restated over the files this lane adds.

    The existing guards in `test_service_and_adapters.py` walk the whole CLI
    source tree and so already cover `transport.py`. This is here because that
    file belongs to another distribution's suite and another lane's manifest: a
    reader of *this* lane should be able to see the boundary held without
    leaving it, and if the CLI tree ever moves out from under that walk, this
    fails too.
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
        for forbidden in ("acquire_lease", "flock", "open_guard", "create_lock"):
            assert forbidden not in called, f"{path} calls {forbidden}"


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
    for path in sorted(client_root.glob("*.py")):
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
    modules = sorted(path for path in source_root.rglob("*.py") if path.stem != "__init__")
    assert modules, "the CLI source tree was not found"
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
    """No caller-selected path, and no ambient one either.

    `--runtime-state` is the only way in. An environment lookup would be an
    unrestricted filesystem path arriving by another name, which is a stop
    condition for this packet.
    """
    import ast

    source_root = Path(__file__).resolve().parents[1] / "src" / "omnivia_core_cli"
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
                raise AssertionError(f"{path.name} reads the environment")
    assert os.name in {"posix", "nt"}
