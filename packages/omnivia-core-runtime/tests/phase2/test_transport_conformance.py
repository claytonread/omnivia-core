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
from omnivia_core_runtime.service.transport import (
    InProcessTransport,
    LocalSocketServer,
    LocalSocketTransport,
    Transport,
    TransportError,
    decode_frame,
    encode_frame,
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


@pytest.fixture(params=["in_process", "local_socket"])
def transport(
    request: pytest.FixtureRequest, socket_dir: Path
) -> Iterator[Transport]:
    dispatcher = make_dispatcher()
    if request.param == "in_process":
        yield InProcessTransport(dispatcher=dispatcher)
        return
    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=dispatcher, path=socket_path):
        yield LocalSocketTransport(path=socket_path)


def request_for(operation: str, *, workspace: str = WORKSPACE, principal: str | None = None):
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
    fast = InProcessTransport(dispatcher=make_dispatcher(), normalise=False).call(request)
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
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        mode = socket_path.stat().st_mode & 0o777
    assert mode == 0o600, f"socket mode is {oct(mode)}"


def test_calling_an_absent_socket_reports_it_clearly(socket_dir: Path) -> None:
    transport = LocalSocketTransport(path=socket_dir / "missing.sock")
    with pytest.raises(TransportError, match="no service socket"):
        transport.call(request_for("core.health"))


def test_the_socket_file_is_removed_on_stop(socket_dir: Path) -> None:
    socket_path = socket_dir / "s.sock"
    server = LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path)
    server.start()
    assert socket_path.exists()
    server.stop()
    assert not socket_path.exists()


def test_a_leftover_socket_file_does_not_block_startup(socket_dir: Path) -> None:
    """A crashed service leaves a *socket* behind; that carries no state.

    The setup used to write an ordinary text file, so this passed by asserting the
    behaviour that broke the sole-writer invariant: `start()` unlinked whatever it
    found, including a lifetime lock file. A stale socket is the real scenario, and
    the only one where removing the path is safe.
    """
    import socket as socket_module

    socket_path = socket_dir / "s.sock"
    abandoned = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    abandoned.bind(str(socket_path))
    abandoned.close()  # the process dies; the socket file survives
    assert socket_path.is_socket()

    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        response = LocalSocketTransport(path=socket_path).call(request_for("core.health"))
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
    import socket as socket_module

    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path):
        bad = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        bad.settimeout(5)
        bad.connect(str(socket_path))
        bad.sendall(b"{not json at all\n")
        bad.close()

        # The next well-formed call still succeeds.
        response = LocalSocketTransport(path=socket_path).call(request_for("core.health"))
        assert isinstance(response, SuccessResponseEnvelope)


def test_frame_encoding_round_trips() -> None:
    payload = {"b": 2, "a": 1, "unicode": "☃"}
    raw = encode_frame(payload)
    assert raw.endswith(b"\n")
    assert decode_frame(raw.rstrip(b"\n")) == payload


@pytest.mark.parametrize("bad", [b"not json", b"[]", b'"string"', b"123"])
def test_a_non_object_frame_is_refused(bad: bytes) -> None:
    with pytest.raises(TransportError):
        decode_frame(bad)


def test_an_over_long_socket_path_is_refused_with_the_numbers(tmp_path: Path) -> None:
    """A deeply nested workspace hits the OS limit; say so, do not raise OSError."""
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40) / "s.sock"
    with pytest.raises(TransportError, match="AF_UNIX limit"):
        LocalSocketTransport(path=deep).call(request_for("core.health"))
    with pytest.raises(TransportError, match="AF_UNIX limit"):
        LocalSocketServer(dispatcher=make_dispatcher(), path=deep).start()


def test_the_transport_module_declares_no_http_dependency() -> None:
    """ADR-036 keeps Core free of transport dependencies."""
    import omnivia_core_runtime.service.transport as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for framework in ("fastapi", "uvicorn", "aiohttp", "starlette", "flask", "requests"):
        assert framework not in source.lower()


# SB-07 regression
@pytest.mark.parametrize(
    ("payload", "description"),
    [
        (b"{}\n", "valid JSON that is not a request envelope"),
        (b"not json at all\n", "not JSON"),
        (b'{"operation": 12345}\n', "a recognised key of the wrong type"),
        (b"\n", "an empty frame"),
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
    import socket as socket_module

    socket_path = socket_dir / "s.sock"
    with LocalSocketServer(dispatcher=make_dispatcher(), path=socket_path) as server:
        client = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(socket_path))
        client.sendall(payload)
        client.close()

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
def test_srb04_binding_never_unlinks_a_path_that_is_not_a_socket(socket_dir: Path) -> None:
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
        with pytest.raises(TransportError, match="not a socket"):
            server.start()

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
        assert socket_path.is_socket()
        response = LocalSocketTransport(path=socket_path).call(request_for("core.health"))
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
    import socket as socket_module

    socket_path = socket_dir / "s.sock"
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
        response = LocalSocketTransport(path=socket_path).call(request_for("core.health"))
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
    import socket as socket_module
    import threading

    socket_path = socket_dir / "s.sock"
    abandoned = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
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
        response = LocalSocketTransport(path=socket_path).call(request_for("core.health"))
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        for server in bound:
            server.stop()
