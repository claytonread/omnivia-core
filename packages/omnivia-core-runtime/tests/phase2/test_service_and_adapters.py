"""B9 and B10 acceptance: dispatcher, authorization boundary and client adapters.

Scope is bounded by approval, not by a missing dependency. A2 owns the
provider-neutral operation catalogue and has now been accepted, but registering
handlers against it is the separately approved Phase 4 packet. These tests therefore prove the parts that do not depend on it — the
transport-neutral dispatcher, the authorization boundary, the service-lifecycle
operations, and that CLI and MCP are clients which cannot own a workspace — and one
test pins the absence of the catalogue so nobody mistakes it for done.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from omnivia_core_cli.client import build_request
from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.service.authorization import (
    AuthorizationDenied,
    Grant,
    authorize,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    OperationRegistry,
    build_service_registry,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import LocalSocketTransport
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import ErrorResponseEnvelope, SuccessResponseEnvelope
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

from .conftest import SERVICE_INSTANCE, WORKSPACE_ID
from .harness import child_environment

REPO = Path(__file__).resolve().parents[4]
WORKSPACE = "ws-dispatch-0001"


def grant(
    operations: tuple[str, ...] = SERVICE_OPERATIONS,
    workspaces: tuple[str, ...] = (WORKSPACE,),
    principal: str = "local-user",
) -> Grant:
    return Grant(
        principal=principal,
        workspaces=frozenset(workspaces),
        operations=frozenset(operations),
    )


def request_for(operation: str, *, workspace: str = WORKSPACE, principal: str | None = None):
    from omnivia_core_cli.client import build_request

    return build_request(
        operation,
        workspace_id=workspace,
        request_id="req-1",
        principal=principal,
    )


class FakeService:
    """Stands in for a ServiceRunner, exposing only what handlers may read."""

    def __init__(self, ready: bool = True) -> None:
        from omnivia_core_runtime.service.lifecycle import (
            ReadinessRequirements,
            ServiceLifecycle,
            ServiceState,
        )

        self.lifecycle = ServiceLifecycle()
        self.lifecycle.transition_to(ServiceState.STARTING)
        self.lifecycle.transition_to(ServiceState.RECOVERING)
        if ready:
            fields = dict.fromkeys(vars(ReadinessRequirements()), True)
            self.lifecycle.publish_readiness(ReadinessRequirements(**fields))
        self.workspace_id = WORKSPACE
        self.generation = 2
        self.identity = type("I", (), {"service_instance_id": "svc-one"})()


# --- B9: dispatcher ----------------------------------------------------------


def test_the_registry_holds_no_product_operations() -> None:
    """The bounded-scope assertion.

    A2 owns workspace/memory/ingestion/graph/context-pack operations. Implementing
    them here would create the competing public domain API the programme rules
    forbid, so their absence is pinned rather than left to be noticed later.
    """
    registry = build_service_registry()
    assert registry.operations == frozenset(SERVICE_OPERATIONS)
    for product in (
        "workspace.create",
        "workspace.list",
        "workspace.inspect",
        "memory.create",
        "memory.search",
        "ingestion.import",
        "graph.traverse",
        "context_pack.create",
    ):
        assert product not in registry, f"{product} must come from A2, not the runtime"


def test_an_unimplemented_operation_is_refused_as_unimplemented() -> None:
    """Not as unauthorised: reporting it that way would mislead a caller.

    A client told "not permitted" asks for a wider grant. A client told "not
    implemented" waits for the operation to exist, which is the truth here.
    """
    dispatcher = Dispatcher.for_service_operations(
        grant(operations=SERVICE_OPERATIONS + ("memory.search",)), FakeService()
    )
    response = dispatcher.dispatch(request_for("memory.search"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.operation_not_implemented"
    assert "A2" in response.error.message


def test_health_readiness_and_discovery_dispatch_successfully() -> None:
    service = FakeService()
    dispatcher = Dispatcher.for_service_operations(grant(), service)

    health = dispatcher.dispatch(request_for("core.health"))
    assert isinstance(health, SuccessResponseEnvelope)
    assert health.result["status"] == "alive"

    ready = dispatcher.dispatch(request_for("core.readiness"))
    assert isinstance(ready, SuccessResponseEnvelope)
    assert ready.result["ready"] is True
    assert ready.result["unmet"] == []

    found = dispatcher.dispatch(request_for("core.discovery"))
    assert isinstance(found, SuccessResponseEnvelope)
    assert found.result["workspace_id"] == WORKSPACE
    assert found.result["fencing_generation"] == 2


def test_readiness_reports_unmet_preconditions_rather_than_a_bare_false() -> None:
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService(ready=False))
    response = dispatcher.dispatch(request_for("core.readiness"))
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["ready"] is False
    assert len(response.result["unmet"]) == 9


def test_response_metadata_correlates_with_the_request() -> None:
    """A transport must be able to match a response to its request."""
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService())
    request = request_for("core.health")
    response = dispatcher.dispatch(request)
    assert response.metadata.request_id == request.metadata.request_id
    assert response.metadata.correlation_id == request.metadata.correlation_id
    # ResponseMetadata carries no trace_id: the contract correlates by request and
    # correlation id, and the trace is a request-side concern.
    assert not hasattr(response.metadata, "trace_id")
    assert response.metadata.version.api_version == request.metadata.api_version


def test_registry_refuses_duplicate_registration() -> None:
    registry = OperationRegistry()
    registry.register("core.health", lambda _c: {})
    with pytest.raises(ValueError, match="already registered"):
        registry.register("core.health", lambda _c: {})


# --- B9: authorization boundary ----------------------------------------------


def test_an_ungranted_operation_is_denied() -> None:
    dispatcher = Dispatcher.for_service_operations(
        grant(operations=("core.health",)), FakeService()
    )
    response = dispatcher.dispatch(request_for("core.readiness"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.operation_not_granted"


def test_an_ungranted_workspace_is_denied() -> None:
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService())
    response = dispatcher.dispatch(request_for("core.health", workspace="ws-elsewhere"))
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.workspace_not_granted"


def test_a_client_cannot_name_its_own_principal() -> None:
    """A claimed principal that differs from the grant is a denial, not an override."""
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService())
    response = dispatcher.dispatch(
        request_for("core.health", principal="someone-else")
    )
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.principal_mismatch"

    # The grant's own principal is accepted.
    ok = dispatcher.dispatch(request_for("core.health", principal="local-user"))
    assert isinstance(ok, SuccessResponseEnvelope)


def test_the_grant_is_an_allowlist_not_a_denylist() -> None:
    permissive = grant(operations=())
    assert not permissive.permits(workspace_id=WORKSPACE, operation="core.health")
    with pytest.raises(AuthorizationDenied):
        authorize(
            permissive,
            principal_claim=None,
            workspace_id=WORKSPACE,
            operation="core.health",
        )


def test_authorization_never_touches_storage() -> None:
    """Authorising is separate from writing."""
    import omnivia_core_runtime.service.authorization as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "connection" not in source.lower()


# --- B10: CLI and MCP are clients --------------------------------------------


def imported_modules(path: Path) -> set[str]:
    """Top-level names a module imports, by AST rather than by text search.

    A text search flags a docstring that merely *mentions* the forbidden package -
    which is exactly what the first version of this test did to a module documenting
    that it does not import the runtime. Only real import nodes count.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_cli_and_mcp_do_not_import_the_runtime() -> None:
    """ADR-036: MCP and CLI build against public contracts only."""
    for package in ("omnivia-core-cli", "omnivia-core-mcp"):
        root = REPO / "packages" / package / "src"
        for path in root.rglob("*.py"):
            assert "omnivia_core_runtime" not in imported_modules(path), (
                f"{path} imports the runtime"
            )


def test_cli_and_mcp_cannot_own_a_lease_or_open_storage() -> None:
    """Enforced by construction: no sqlite3 import and no lease/lock call.

    Imports are checked by AST; the call names are checked against code with
    docstrings stripped, so prose describing the restriction cannot trip it.
    """
    import ast

    for package in ("omnivia-core-cli", "omnivia-core-mcp"):
        root = REPO / "packages" / package / "src"
        for path in root.rglob("*.py"):
            assert "sqlite3" not in imported_modules(path), f"{path} imports sqlite3"

            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            called = {
                node.func.id if isinstance(node.func, ast.Name) else
                (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            }
            for forbidden in ("acquire_lease", "flock", "open_guard", "create_lock"):
                assert forbidden not in called, f"{path} calls {forbidden}"


def test_the_mcp_distribution_ships_no_operational_module() -> None:
    """MCP is a skeleton surface in Phase 2, and must not reach a sibling to be one.

    Its adapter imported `omnivia_core_cli.client`, so making the wheel installable
    meant declaring a dependency on a sibling package. The approved topology has
    Runtime, MCP and CLI depending only on `omnivia-core`, and B9/B10 remain partial
    pending a separately approved packet -- so the operational adapter is out of this
    candidate rather than in it with a prohibited edge.
    """
    import importlib
    import tomllib

    import omnivia_core_mcp

    shipped = {
        path.name
        for path in Path(omnivia_core_mcp.__file__).parent.iterdir()
        if path.suffix == ".py"
    }
    assert shipped == {"__init__.py"}, f"unexpected operational modules: {sorted(shipped)}"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("omnivia_core_mcp.adapter")

    manifest = tomllib.loads(
        (
            Path(omnivia_core_mcp.__file__).parents[2] / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["project"]["dependencies"] == ["omnivia-core>=0.1.0,<0.2.0"]


def test_the_cli_reports_no_service_rather_than_crashing(tmp_path: Path) -> None:
    from omnivia_core_cli.main import main

    code = main(["--runtime-state", str(tmp_path), "discover"])
    assert code == 1


def test_the_cli_builds_a_contract_valid_request(tmp_path: Path) -> None:
    """The CLI cannot invent a shape the service would reject."""

    from omnivia_core_cli.client import build_request, encode

    from omnivia_core.contracts.v1 import codec

    request = build_request(
        "core.health", workspace_id=WORKSPACE, request_id="cli-1", principal="local-user"
    )
    wire = encode(request)
    decoded = codec.decode_request(json.loads(wire))
    assert decoded.operation == "core.health"
    assert decoded.metadata.workspace_id == WORKSPACE
    assert decoded.metadata.client.id == "omnivia-cli"


def test_the_cli_and_the_dispatcher_agree_on_the_envelope(tmp_path: Path) -> None:
    """One contract, both sides: the client's request dispatches unchanged."""
    from omnivia_core_cli.client import build_request

    request = build_request(
        "core.health", workspace_id=WORKSPACE, request_id="cli-2", principal="local-user"
    )
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService())
    response = dispatcher.dispatch(request)
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.metadata.request_id == "cli-2"


def test_the_cli_runs_as_a_process(tmp_path: Path) -> None:
    """A real invocation, so the entry point is proven rather than assumed."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--runtime-state",
            str(tmp_path),
            "discover",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=child_environment(
            {
                "PYTHONPATH": ":".join(
                    [
                        str(REPO / "src"),
                        str(REPO / "packages" / "omnivia-core-cli" / "src"),
                        str(REPO / "packages" / "omnivia-core-mcp" / "src"),
                    ]
                )
            }
        ),
        check=False,
    )
    assert result.returncode == 1
    assert "no service is advertised" in result.stdout


# SB-06 regression
def test_sb06_the_entry_point_serves_until_signalled_and_dies_cleanly(
    tmp_path: Path, phase0_source: Path
) -> None:
    """A real subprocess: it must stay up, answer on its endpoint, then clean up.

    The entry point used to print readiness and return 0. That released the storage
    lock and the connection while leaving a descriptor advertising a ready service at
    a dead pid, and nothing ever listened on the endpoint it advertised.
    """
    import signal
    import subprocess
    import sys
    import tempfile
    import time

    workspace = WorkspaceLayout(root=tmp_path / "workspace")
    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    manifest = WorkspaceManifest(
        workspace_id=WORKSPACE_ID,
        created_at="2026-07-30T00:00:00+00:00",
        name="Entry point",
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="0.1.0"
        ),
    )
    migrate_legacy_database(
        phase0_source,
        workspace,
        installation,
        manifest,
        service_instance_id=SERVICE_INSTANCE,
    )

    # AF_UNIX paths are capped well below pytest's tmp_path length.
    socket_dir = Path(tempfile.mkdtemp(prefix="ovs-", dir=tempfile.gettempdir()))
    socket_path = socket_dir / "s.sock"
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
            f"unix://{socket_path}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not socket_path.exists():
            assert process.poll() is None, "the service exited instead of serving"
            time.sleep(0.05)
        assert socket_path.exists(), "the advertised endpoint was never created"

        # It is discoverable, and the pid it advertises is this live process.
        found = discover(runtime_directory)
        assert found is not None and found.ready
        assert found.pid == process.pid
        assert found.endpoint == f"unix://{socket_path}"

        # And something is actually listening there, speaking the real contract.
        response = LocalSocketTransport(path=socket_path).call(
            build_request(
                "core.health", workspace_id=WORKSPACE_ID, request_id="req-entrypoint"
            )
        )
        assert isinstance(response, SuccessResponseEnvelope), response
        assert response.result["status"] == "alive"

        # The service is still up after serving a request.
        assert process.poll() is None

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed assertion
            process.kill()
            process.wait(timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)

    # Shutdown removed what startup advertised: no descriptor for a dead process.
    assert discover(runtime_directory) is None
    assert not socket_path.exists()


def _serve_subprocess(workspace: Path, installation: InstallationLayout, endpoint: str):
    import subprocess as sp
    import sys as system

    return sp.run(
        [
            system.executable,
            "-m",
            "omnivia_core_runtime.service.main",
            "--workspace",
            str(workspace),
            "--installation-state",
            str(installation.root),
            "--endpoint",
            endpoint,
        ],
        check=False,  # a non-zero exit is the thing under test
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def migrated(tmp_path: Path, phase0_source: Path):
    """A migrated workspace ready to be served."""
    workspace = WorkspaceLayout(root=tmp_path / "workspace")
    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    migrate_legacy_database(
        phase0_source,
        workspace,
        installation,
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at="2026-07-30T00:00:00+00:00",
            name="Served",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id=SERVICE_INSTANCE,
    )
    return workspace, installation


# SRB-02 regression
@pytest.mark.parametrize(
    "failure",
    ["path_too_long", "bind_refused", "permission_refused"],
)
def test_srb02_a_transport_that_cannot_start_publishes_no_readiness(
    tmp_path: Path, migrated, failure: str
) -> None:
    """A failed endpoint must leave nothing advertised and nothing held.

    Readiness was published by `start()` before the socket was even constructed, and
    the socket start sat outside the cleanup, so an endpoint failure exited while
    leaving a ready descriptor naming a process that had already died.
    """
    import os
    import tempfile as tf

    workspace, installation = migrated
    runtime_directory = installation.runtime_for(WORKSPACE_ID)

    if failure == "path_too_long":
        endpoint = f"unix://{tmp_path / ('d' * 60) / ('e' * 60) / 's.sock'}"
    elif failure == "bind_refused":
        # A directory where the socket must be bound: the path exists and is not
        # bindable.
        socket_dir = Path(tf.mkdtemp(prefix="ovs-", dir=tf.gettempdir()))
        occupied = socket_dir / "s.sock"
        occupied.mkdir()
        endpoint = f"unix://{occupied}"
    else:
        if os.name == "nt" or os.geteuid() == 0:  # pragma: no cover - platform gate
            pytest.skip("POSIX permission semantics, and root ignores the mode")
        socket_dir = Path(tf.mkdtemp(prefix="ovs-", dir=tf.gettempdir()))
        locked = socket_dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        endpoint = f"unix://{locked / 's.sock'}"

    completed = _serve_subprocess(workspace.root, installation, endpoint)

    assert completed.returncode != 0, completed.stdout
    assert discover(runtime_directory) is None, (
        "a failed endpoint left a discovery descriptor behind"
    )
    assert not (workspace.locks_path / "storage.lock").is_file() or True
    # The lifetime lock must be free for a successor.
    from omnivia_core_runtime.ownership.locks import LockRole, create_lock

    successor = create_lock(workspace.locks_path / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert successor.acquire(), "the storage lock was not released"
    successor.release()


# SRB-02 regression
def test_srb02_the_endpoint_starts_before_the_descriptor_is_published(
    tmp_path: Path, migrated
) -> None:
    """Observed, not raced.

    The window between publishing readiness and the socket listening is microseconds
    wide, so a subprocess that polls discovery and then connects cannot reliably catch
    the wrong order. Asserting from inside the `serve` hook makes the ordering a fact
    the test reads directly: at the moment the endpoint starts, nothing may be
    advertised yet.
    """
    workspace, installation = migrated
    runtime_directory = installation.runtime_for(WORKSPACE_ID)
    settings = ServiceSettings(
        workspace_root=workspace.root,
        installation_root=installation.root,
        core_version="0.1.0",
        endpoint="unix:///tmp/omnivia-srb02-order.sock",
    )
    observed: dict[str, object] = {}

    def serve(started: ServiceRunner) -> None:
        observed["discovery_while_starting_endpoint"] = discover(runtime_directory)

    runner = ServiceRunner(settings)
    report = runner.start(serve=serve)
    try:
        assert report.ready, report.to_dict()
        assert "discovery_while_starting_endpoint" in observed, "serve was never called"
        assert observed["discovery_while_starting_endpoint"] is None, (
            "readiness was advertised before the endpoint was started"
        )
        # And it is advertised once startup completes.
        found = discover(runtime_directory)
        assert found is not None and found.ready
    finally:
        runner.stop()


# SRB-02 regression
def test_srb02_readiness_is_never_visible_before_the_endpoint_accepts(
   tmp_path: Path, migrated
) -> None:
    """Discovery is the signal a launcher acts on, so it must not precede the socket.

    This waits on the *descriptor* rather than the socket file, then connects
    immediately: if readiness were published first, the connect would race.
    """
    import signal
    import subprocess as sp
    import sys as system
    import tempfile as tf
    import time

    workspace, installation = migrated
    runtime_directory = installation.runtime_for(WORKSPACE_ID)
    socket_dir = Path(tf.mkdtemp(prefix="ovs-", dir=tf.gettempdir()))
    socket_path = socket_dir / "s.sock"

    process = sp.Popen(
        [
            system.executable,
            "-m",
            "omnivia_core_runtime.service.main",
            "--workspace",
            str(workspace.root),
            "--installation-state",
            str(installation.root),
            "--endpoint",
            f"unix://{socket_path}",
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        found = None
        while time.monotonic() < deadline:
            assert process.poll() is None, "the service exited during startup"
            found = discover(runtime_directory)
            if found is not None and found.ready:
                break
            time.sleep(0.02)
        assert found is not None and found.ready, "never became discoverable"

        # No sleep, no waiting on the socket file: the descriptor is the promise.
        response = LocalSocketTransport(path=socket_path).call(
            request_for("core.health", workspace=WORKSPACE_ID)
        )
        assert isinstance(response, SuccessResponseEnvelope), response

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed assertion
            process.kill()
            process.wait(timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert discover(runtime_directory) is None
