"""Cross-transport conformance (B9 exit condition).

B9 requires in-process and service transports to pass the *same* suite. The
conformance cases below are therefore parametrised over both transports, so a
divergence fails rather than going unnoticed — which is the whole point of having
one dispatcher behind two transports.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_cli.client import build_request
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.ovc1 import MAGIC, OVC1Error
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    EndpointProbe,
    EndpointScheme,
    InProcessTransport,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
    Transport,
    TransportError,
    decode_frame,
    encode_frame,
    endpoint_for_path,
    names_a_local_endpoint,
    parse_endpoint,
    pipe_name_for_path,
    probe_endpoint,
)

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    codec,
)

WORKSPACE = "ws-transport-0001"


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A short directory for the socket.

    pytest's tmp_path is far longer than the 104-byte AF_UNIX limit, so a socket
    cannot live there. A short mkdtemp under the system temp root can.
    """
    directory = Path(tempfile.mkdtemp(prefix="ovs-", dir=tempfile.gettempdir()))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class FakeService:
    def __init__(self) -> None:
        from omnivia_core_runtime.service.lifecycle import (
            ReadinessRequirements,
            ServiceLifecycle,
            ServiceState,
        )

        self.lifecycle = ServiceLifecycle()
        self.lifecycle.transition_to(ServiceState.STARTING)
        self.lifecycle.transition_to(ServiceState.RECOVERING)
        self.lifecycle.publish_readiness(
            ReadinessRequirements(**dict.fromkeys(vars(ReadinessRequirements()), True))
        )
        self.workspace_id = WORKSPACE
        self.generation = 3
        self.identity = type("I", (), {"service_instance_id": "svc-transport"})()


def make_dispatcher() -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal="local-user",
            workspaces=frozenset({WORKSPACE}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        FakeService(),
    )


def send_raw_frame(path: Path, payload: bytes) -> None:
    """Send one deliberately unvalidated frame over this platform's local IPC."""
    endpoint = endpoint_for_path(path)
    if endpoint.scheme is EndpointScheme.PIPE:
        from omnivia_core_runtime.service.windows_pipe import open_pipe_client

        client = open_pipe_client(endpoint.address, timeout=5.0)
        try:
            client.write(payload)
        finally:
            client.close()
        return

    import socket as socket_module

    client = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        client.settimeout(5)
        client.connect(endpoint.address)
        client.sendall(payload)
    finally:
        client.close()


def frame_body(body: bytes) -> bytes:
    """Build one OVC1 frame by hand so malformed bodies reach body admission."""
    return MAGIC + len(body).to_bytes(4, "big") + body


@pytest.fixture(params=["in_process", "local_socket"])
def transport(request: pytest.FixtureRequest, socket_dir: Path) -> Iterator[Transport]:
    dispatcher = make_dispatcher()
    if request.param == "in_process":
        yield InProcessTransport(dispatcher=dispatcher)
        return
    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=dispatcher, path=socket_path):
        yield LocalSocketTransport(path=socket_path)


def request_for(
    operation: str, *, workspace: str = WORKSPACE, principal: str | None = None
):
    return build_request(
        operation,
        workspace_id=workspace,
        request_id=f"req-{operation}",
        principal=principal,
    )


# --- the shared conformance cases --------------------------------------------


def test_health_succeeds_on_every_transport(transport: Transport) -> None:
    response = transport.call(request_for("core.health"))
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["status"] == "alive"


def test_readiness_reports_the_same_facts_on_every_transport(
    transport: Transport,
) -> None:
    response = transport.call(request_for("core.readiness"))
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["ready"] is True
    # Shape-agnostic: the codec normalises JSON arrays to tuples, and both
    # transports now agree on that. test_both_transports_agree_on_value_types
    # pins the agreement itself.
    assert not response.result["unmet"]


def test_discovery_reports_the_same_identity_on_every_transport(
    transport: Transport,
) -> None:
    response = transport.call(request_for("core.discovery"))
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["workspace_id"] == WORKSPACE
    assert response.result["fencing_generation"] == 3


def test_correlation_survives_every_transport(transport: Transport) -> None:
    request = request_for("core.health")
    response = transport.call(request)
    assert response.metadata.request_id == request.metadata.request_id
    assert response.metadata.correlation_id == request.metadata.correlation_id


def test_an_unimplemented_operation_is_refused_on_every_transport(
    transport: Transport,
) -> None:
    response = transport.call(request_for("memory.search"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.operation_not_implemented"


def test_an_ungranted_workspace_is_refused_on_every_transport(
    transport: Transport,
) -> None:
    response = transport.call(request_for("core.health", workspace="ws-elsewhere"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.workspace_not_granted"


def test_a_mismatched_principal_is_refused_on_every_transport(
    transport: Transport,
) -> None:
    response = transport.call(request_for("core.health", principal="someone-else"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.principal_mismatch"


def test_both_transports_agree_on_value_types(socket_dir: Path) -> None:
    """Identical Python types, not merely equal values.

    A handler returning a list yields a list in-process and a tuple over the wire
    unless the in-process transport normalises. Code written and tested in-process
    would then break on a socket, which is exactly what B9's shared suite exists to
    prevent.
    """
    request = request_for("core.readiness")
    in_process = InProcessTransport(dispatcher=make_dispatcher()).call(request)

    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        over_socket = LocalSocketTransport(path=socket_path).call(request)

    assert type(in_process.result["unmet"]) is type(over_socket.result["unmet"])
    assert in_process.result == over_socket.result


def test_the_fast_path_is_an_explicit_opt_out(socket_dir: Path) -> None:
    """Skipping normalisation is available, and its cost is the type difference."""
    request = request_for("core.readiness")
    fast = InProcessTransport(dispatcher=make_dispatcher(), normalise=False).call(
        request
    )
    normalised = InProcessTransport(dispatcher=make_dispatcher()).call(request)

    assert isinstance(fast.result["unmet"], list), "the fast path returns handler types"
    assert isinstance(normalised.result["unmet"], tuple), "normalised matches the wire"


# --- the transports agree, byte for byte -------------------------------------


def test_both_transports_produce_identical_wire_responses(socket_dir: Path) -> None:
    """The strongest form of "the same suite": identical encoded output.

    A behavioural comparison could still hide an encoding difference. Comparing the
    canonical JSON of both responses cannot.
    """
    dispatcher = make_dispatcher()
    request = request_for("core.discovery")

    in_process = InProcessTransport(dispatcher=dispatcher).call(request)

    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        over_socket = LocalSocketTransport(path=socket_path).call(request)

    assert codec.to_canonical_json(codec.encode_response(in_process)) == (
        codec.to_canonical_json(codec.encode_response(over_socket))
    )


# --- socket-specific behaviour ------------------------------------------------


def test_the_socket_is_not_world_accessible(socket_dir: Path) -> None:
    """A local socket any user could connect to would bypass the grant."""
    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path) as server:
        if LOCAL_SCHEME is EndpointScheme.PIPE:
            assert server.endpoint is not None
            assert server.endpoint.path is None
            assert not socket_path.exists()
            response = LocalSocketTransport(path=socket_path).call(
                request_for("core.health")
            )
            assert isinstance(response, SuccessResponseEnvelope)
            return
        mode = socket_path.stat().st_mode & 0o777
    assert mode == 0o600, f"socket mode is {oct(mode)}"


def test_calling_an_absent_socket_reports_it_without_disclosing_the_path(
    socket_dir: Path,
) -> None:
    missing = socket_dir / "credential-hunter2-missing.sock"
    transport = LocalSocketTransport(path=missing)
    with pytest.raises(
        TransportError, match="service endpoint is unavailable"
    ) as caught:
        transport.call(request_for("core.health"))
    assert str(missing) not in str(caught.value)


def test_the_socket_file_is_removed_on_stop(socket_dir: Path) -> None:
    socket_path = socket_dir / "s.sock"
    server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
    server.start()
    endpoint = endpoint_for_path(socket_path)
    if endpoint.scheme is EndpointScheme.UNIX:
        assert socket_path.exists()
    else:
        assert not socket_path.exists()
        assert probe_endpoint(endpoint) is EndpointProbe.ANSWERING
    server.stop()
    assert not socket_path.exists()
    assert probe_endpoint(endpoint) is EndpointProbe.REFUSED


def test_a_leftover_socket_file_does_not_block_startup(socket_dir: Path) -> None:
    """A crashed service leaves a *socket* behind; that carries no state.

    The setup used to write an ordinary text file, so this passed by asserting the
    behaviour that broke the sole-writer invariant: `start()` unlinked whatever it
    found, including a lifetime lock file. A stale socket is the real scenario, and
    the only one where removing the path is safe.
    """
    socket_path = socket_dir / "s.sock"
    if LOCAL_SCHEME is EndpointScheme.UNIX:
        import socket as socket_module

        abandoned = socket_module.socket(
            socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        abandoned.bind(str(socket_path))
        abandoned.close()  # the process dies; the socket file survives
        assert socket_path.is_socket()
    else:
        previous = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
        previous.start()
        previous.stop()
        assert probe_endpoint(endpoint_for_path(socket_path)) is EndpointProbe.REFUSED

    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health")
        )
    assert isinstance(response, SuccessResponseEnvelope)


def test_starting_twice_is_refused(socket_dir: Path) -> None:
    server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_dir / "s.sock")
    server.start()
    try:
        with pytest.raises(TransportError, match="already started"):
            server.start()
    finally:
        server.stop()


def test_a_malformed_frame_does_not_take_the_server_down(socket_dir: Path) -> None:
    """One bad client must not end service for the next one."""
    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        send_raw_frame(socket_path, frame_body(b"{not json at all"))

        # The next well-formed call still succeeds.
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health")
        )
        assert isinstance(response, SuccessResponseEnvelope)


def test_frame_encoding_round_trips() -> None:
    payload = {"b": 2, "a": 1, "unicode": "☃"}
    raw = encode_frame(payload)
    assert raw.startswith(MAGIC)
    assert not raw.endswith(b"\n")
    assert decode_frame(raw) == payload


@pytest.mark.parametrize("bad", [b"not json", b"[]", b'"string"', b"123"])
def test_a_non_object_frame_is_refused(bad: bytes) -> None:
    with pytest.raises(OVC1Error):
        decode_frame(frame_body(bad))


def test_an_over_long_socket_path_is_refused_with_the_numbers(tmp_path: Path) -> None:
    """A deeply nested workspace hits the OS limit; say so, do not raise OSError."""
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40) / "s.sock"
    if LOCAL_SCHEME is EndpointScheme.PIPE:
        with LocalSocketServer(dispatcher=make_dispatcher(), path=deep):
            response = LocalSocketTransport(path=deep).call(request_for("core.health"))
        assert isinstance(response, SuccessResponseEnvelope)
        return
    with pytest.raises(TransportError, match="AF_UNIX limit"):
        LocalSocketTransport(path=deep).call(request_for("core.health"))
    with pytest.raises(TransportError, match="AF_UNIX limit"):
        LocalSocketServer(dispatcher=make_dispatcher(), path=deep).start()


def test_the_transport_module_declares_no_http_dependency() -> None:
    """ADR-036 keeps Core free of transport dependencies."""
    import omnivia_core_runtime.service.transport as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for framework in (
        "fastapi",
        "uvicorn",
        "aiohttp",
        "starlette",
        "flask",
        "requests",
    ):
        assert framework not in source.lower()


# SB-07 regression
@pytest.mark.parametrize(
    ("payload", "description"),
    [
        (encode_frame({}), "valid JSON that is not a request envelope"),
        (frame_body(b"not json at all"), "not JSON"),
        (encode_frame({"operation": 12345}), "a recognised key of the wrong type"),
        (MAGIC + (0).to_bytes(4, "big"), "a zero-length frame"),
    ],
)
def test_sb07_a_malformed_frame_does_not_kill_the_accept_loop(
    socket_dir: Path, payload: bytes, description: str
) -> None:
    """The server must survive any bad frame and still answer the next caller.

    `_serve` caught `TransportError, OSError`, which looked exhaustive and was not:
    `codec.decode_request` raises `ContractDecodeError`, so one `{}` from any local
    client terminated the sole accept loop while the service stayed advertised.
    """
    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path) as server:
        send_raw_frame(socket_path, payload)

        # The accept thread is the thing under test: it must still be alive...
        transport = LocalSocketTransport(path=socket_path)
        response = transport.call(request_for("core.health"))
        assert isinstance(response, SuccessResponseEnvelope), (
            f"server died on {description}: {response}"
        )
        assert response.result["status"] == "alive"

        # ...and repeatedly so, not merely once.
        again = transport.call(request_for("core.health"))
        assert isinstance(again, SuccessResponseEnvelope)
        assert server._thread is not None and server._thread.is_alive()


# SRB-04 regression
def test_srb04_binding_never_unlinks_a_path_that_is_not_a_socket(
    socket_dir: Path,
) -> None:
    """The endpoint may reclaim a stale socket, and nothing else.

    `start()` unlinked whatever it found. Pointed at `locks/storage.lock` that
    destroyed the lifetime lock file while its owner kept an flock on the unlinked
    inode, so once the socket was cleaned up a successor created a fresh lock file
    and acquired it -- two live writers on one workspace.
    """
    from omnivia_core_runtime.ownership.locks import LockRole, create_lock

    lock_path = socket_dir / "storage.lock"
    owner = create_lock(lock_path, LockRole.LIFETIME_STORAGE)
    assert owner.acquire()
    try:
        server = LocalSocketServer(dispatcher=make_dispatcher(), path=lock_path)
        if LOCAL_SCHEME is EndpointScheme.UNIX:
            with pytest.raises(TransportError, match="not a socket"):
                server.start()
        else:
            server.start()
            server.stop()

        # The lock file is untouched, and still exclusive.
        assert lock_path.is_file()
        successor = create_lock(lock_path, LockRole.LIFETIME_STORAGE)
        assert not successor.acquire(), "the lifetime lock became reacquirable"
    finally:
        owner.release()


# SRB-05 regression
def test_srb05_a_live_endpoint_is_never_unlinked(socket_dir: Path) -> None:
    """Being a socket says a service *was* here, not that it has gone.

    Two workspaces pointed at one endpoint path hold separate lifetime locks, so
    nothing else stopped the second from deleting the first's live socket and leaving
    it advertised but unreachable.
    """
    socket_path = socket_dir / "s.sock"
    live = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
    live.start()
    try:
        contender = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
        with pytest.raises(TransportError, match="answering"):
            contender.start()

        # The running service is untouched and still answering.
        if LOCAL_SCHEME is EndpointScheme.UNIX:
            assert socket_path.is_socket()
        else:
            assert (
                probe_endpoint(endpoint_for_path(socket_path))
                is EndpointProbe.ANSWERING
            )
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health")
        )
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        live.stop()


# SRB-10 regression
def test_srb10_the_endpoint_name_never_points_at_nothing(
    socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding replaces the name atomically instead of unlinking and rebinding.

    Observed through the mechanism, not raced: the window between `unlink` and `bind`
    is microseconds wide, so polling cannot catch it. What is deterministic is that
    the endpoint path is never unlinked at all -- it is renamed over, so the name
    refers to the old socket or the new one and never to nothing. A caller connecting
    mid-start therefore cannot find the endpoint missing, and a racing service cannot
    take a freed name and be silently unlinked by this one.
    """
    import os as os_module

    socket_path = socket_dir / "s.sock"
    if LOCAL_SCHEME is EndpointScheme.PIPE:
        unlinked: list[str] = []
        real_unlink = os_module.unlink
        real_path_unlink = Path.unlink

        def record_os_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            unlinked.append(str(path))
            return real_unlink(path, *args, **kwargs)

        def record_path_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            unlinked.append(str(self))
            return real_path_unlink(self, *args, **kwargs)

        monkeypatch.setattr(os_module, "unlink", record_os_unlink)
        monkeypatch.setattr(Path, "unlink", record_path_unlink)
        server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
        server.start()
        monkeypatch.undo()
        try:
            assert unlinked == []
            assert (
                probe_endpoint(endpoint_for_path(socket_path))
                is EndpointProbe.ANSWERING
            )
        finally:
            server.stop()
        assert probe_endpoint(endpoint_for_path(socket_path)) is EndpointProbe.REFUSED
        return

    import socket as socket_module

    abandoned = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    abandoned.bind(str(socket_path))
    abandoned.close()
    stale_inode = socket_path.stat().st_ino

    unlinked: list[str] = []
    real_unlink = os_module.unlink
    real_path_unlink = Path.unlink

    def record_os_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        unlinked.append(str(path))
        return real_unlink(path, *args, **kwargs)

    def record_path_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        unlinked.append(str(self))
        return real_path_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os_module, "unlink", record_os_unlink)
    monkeypatch.setattr(Path, "unlink", record_path_unlink)

    server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
    server.start()
    monkeypatch.undo()
    try:
        assert str(socket_path) not in unlinked, (
            f"the endpoint name was unlinked during start(): {unlinked}"
        )
        # The stale socket really was replaced, and the name is a socket throughout.
        assert socket_path.stat().st_ino != stale_inode
        assert socket_path.is_socket()
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health")
        )
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        server.stop()

    # No staging socket is left behind. The `.bind` lock file is expected: it
    # serialises reclaim of this endpoint name and outlives any one binder.
    leftovers = [p.name for p in socket_dir.iterdir() if p.name.endswith(".binding")]
    assert leftovers == [], leftovers


# SRB-12 regression
def test_srb12_only_one_binder_reclaims_a_stale_endpoint(socket_dir: Path) -> None:
    """Two services reclaiming one stale socket must not both bind.

    Probing before binding is not enough on its own: both racers see `REFUSED`, both
    bind a staging socket, and the second rename replaces the first's endpoint after
    it went live -- the stranding the probe exists to prevent, moved a few
    microseconds later.
    """
    import threading

    socket_path = socket_dir / "s.sock"
    if LOCAL_SCHEME is EndpointScheme.UNIX:
        import socket as socket_module

        abandoned = socket_module.socket(
            socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        abandoned.bind(str(socket_path))
        abandoned.close()

    outcomes: list[str] = []
    bound: list[LocalSocketServer] = []
    lock = threading.Lock()

    def race() -> None:
        server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
        try:
            server.start()
        except TransportError:
            with lock:
                outcomes.append("refused")
        else:
            with lock:
                outcomes.append("bound")
                bound.append(server)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert sorted(outcomes) == ["bound", "refused"], outcomes
        # The winner is reachable at the endpoint.
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health")
        )
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        for server in bound:
            server.stop()


# --- endpoint parsing -----------------------------------------------------


def test_parse_endpoint_recognizes_unix_urls() -> None:
    parsed = parse_endpoint("unix:///var/run/omnivia/s.sock")
    assert parsed == LocalEndpoint(EndpointScheme.UNIX, "/var/run/omnivia/s.sock")


def test_parse_endpoint_recognizes_pipe_urls() -> None:
    parsed = parse_endpoint("pipe://omnivia-deadbeef")
    assert parsed == LocalEndpoint(EndpointScheme.PIPE, "omnivia-deadbeef")


@pytest.mark.parametrize(
    "endpoint",
    [
        "in-process",
        "",
        "unix://",
        "pipe://",
        "http://localhost/socket",
        "pipe://../escape",
        "pipe://has a space",
        "pipe://" + "x" * 201,
    ],
)
def test_parse_endpoint_refuses_anything_it_cannot_address(endpoint: str) -> None:
    assert parse_endpoint(endpoint) is None


def test_names_a_local_endpoint_distinguishes_claims_from_in_process() -> None:
    """An endpoint naming no local scheme is simply not probeable.

    An endpoint that claims one of this runtime's local schemes but fails to
    parse is a different failure -- a claim that cannot be believed -- and the
    two must not be confused, or a malformed `pipe://` URL would be trusted on
    the strength of its pid alone.
    """
    assert not names_a_local_endpoint("in-process")
    assert names_a_local_endpoint("unix:///anything")
    assert names_a_local_endpoint("pipe://anything")
    # Claims a local scheme, but parse_endpoint refuses the malformed name.
    assert names_a_local_endpoint("pipe://../escape")
    assert parse_endpoint("pipe://../escape") is None


def test_a_malformed_pipe_endpoint_is_refused_at_construction() -> None:
    with pytest.raises(TransportError, match="not a usable named-pipe name"):
        LocalEndpoint(EndpointScheme.PIPE, "../escape")


def test_an_empty_unix_endpoint_is_refused_at_construction() -> None:
    with pytest.raises(TransportError, match="needs a socket path"):
        LocalEndpoint(EndpointScheme.UNIX, "")


# --- the platform URL -------------------------------------------------------


def test_endpoint_for_path_serves_this_platforms_scheme(tmp_path: Path) -> None:
    """`endpoint_for_path` never guesses: it always returns `LOCAL_SCHEME`.

    A Windows host advertising `unix://` -- inferring the mechanism at the call
    site instead of asking the platform once -- is exactly the defect this
    module exists to prevent.
    """
    endpoint = endpoint_for_path(tmp_path / "s.sock")
    assert endpoint.scheme is LOCAL_SCHEME
    assert endpoint.url.startswith(f"{LOCAL_SCHEME.value}://")


def test_pipe_name_for_path_is_deterministic_and_bounded(tmp_path: Path) -> None:
    """A launcher and the service it spawned must derive the same pipe name."""
    path = tmp_path / "workspace" / "locks" / "s.sock"
    first = pipe_name_for_path(path)
    second = pipe_name_for_path(path)
    assert first == second
    assert first.startswith("omnivia-")
    assert len(first) <= 200

    other = pipe_name_for_path(tmp_path / "elsewhere" / "s.sock")
    assert other != first


def test_a_pipe_endpoint_carries_no_filesystem_path() -> None:
    """A named pipe lives in a kernel namespace, not on disk."""
    endpoint = LocalEndpoint(EndpointScheme.PIPE, "omnivia-deadbeef")
    assert endpoint.path is None
    assert endpoint.address == "\\\\.\\pipe\\omnivia-deadbeef"


def test_a_unix_endpoint_reports_its_own_path(tmp_path: Path) -> None:
    socket_path = tmp_path / "s.sock"
    endpoint = LocalEndpoint(EndpointScheme.UNIX, str(socket_path))
    assert endpoint.path == socket_path
    assert endpoint.address == str(socket_path)
