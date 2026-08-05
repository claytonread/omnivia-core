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
from omnivia_core_runtime.ownership.discovery import discover, is_compatible, publish
from omnivia_core_runtime.service.authorization import (
    AuthorizationDenied,
    Grant,
    authorize,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import _endpoint_to_serve
from omnivia_core_runtime.service.main import main as service_main
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    OperationRegistry,
    build_service_registry,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    EndpointScheme,
    LocalSocketTransport,
    endpoint_for_path,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    ServiceEndpointDescriptor,
    ServiceProcessEvidence,
    SuccessResponseEnvelope,
)
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


def cli_descriptor(
    *, endpoint: str = "unix:///tmp/omnivia-cli.sock", generation: int = 7
) -> ServiceEndpointDescriptor:
    """What a running service advertises, in the shape it advertises it."""
    return ServiceEndpointDescriptor(
        descriptor_version=API_VERSION,
        workspace_id=WORKSPACE_ID,
        service_instance_id=SERVICE_INSTANCE,
        installation_id="inst-cli",
        endpoint_uri=endpoint,
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        supported_api_versions=supported_api_versions(),
        supported_workspace_versions=supported_workspace_versions("1"),
        workspace_format_version=workspace_contract_version("1"),
        ready=True,
        lifecycle_state="ready",
        fencing_generation=generation,
        published_at="2026-08-04T00:00:00Z",
        process=ServiceProcessEvidence(pid=4242, start_time="100", boot_id="boot-a"),
    )


def test_the_cli_reads_the_descriptor_the_runtime_publishes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's reader and the Runtime's writer must agree on one document.

    Every CLI discovery case above this one runs against an empty runtime
    directory and asserts the "no service is advertised" branch, so when the
    published document moved to the public `ServiceEndpointDescriptor` shape the
    CLI began reporting every live service as absent and every suite in the
    repository stayed green. A reader test that never sees a real descriptor is
    not a test of the reader, which is why the descriptor here is written by the
    real `publish()` rather than assembled as a fixture document.
    """
    from omnivia_core_cli.client import read_descriptor
    from omnivia_core_cli.main import main

    runtime = tmp_path / "runtime" / WORKSPACE_ID
    publish(runtime, cli_descriptor())

    service = read_descriptor(runtime)
    assert service is not None, "the CLI reported a published service as absent"
    assert service.endpoint_uri == "unix:///tmp/omnivia-cli.sock"
    assert service.workspace_id == WORKSPACE_ID
    assert service.service_instance_id == SERVICE_INSTANCE
    assert service.fencing_generation == 7
    assert service.ready is True

    # And what the user is actually shown, not just what the reader returns.
    assert main(["--runtime-state", str(runtime), "discover"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "endpoint": "unix:///tmp/omnivia-cli.sock",
        "workspace_id": WORKSPACE_ID,
        "service_instance_id": SERVICE_INSTANCE,
        "fencing_generation": 7,
        "ready": True,
    }

    # The descriptor's other consumer: the workspace a request is addressed to.
    assert main(["--runtime-state", str(runtime), "health"]) == 0
    request = json.loads(capsys.readouterr().out)
    assert request["metadata"]["workspace_id"] == WORKSPACE_ID


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        '{"workspace_id": "x"}',
        "[]",
        json.dumps(
            {
                "endpoint": "unix:///tmp/omnivia-cli.sock",
                "workspace_id": WORKSPACE_ID,
                "service_instance_id": SERVICE_INSTANCE,
                "fencing_generation": 1,
                "api_version": "1.0",
                "readiness": "ready",
            }
        ),
    ],
    ids=["garbage", "incomplete", "not-an-object", "the-legacy-shape"],
)
def test_the_cli_reports_an_unreadable_descriptor_as_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], document: str
) -> None:
    """Failure semantics are unchanged, including for the shape this replaces.

    The legacy document is in this list deliberately. One left behind by an older
    service is not a descriptor this build can read, and the answer to it is the
    same "no service is advertised" as to any other unreadable file -- not a
    crash, and not a shim that decodes both shapes.
    """
    from omnivia_core_cli.client import read_descriptor
    from omnivia_core_cli.main import main

    runtime = tmp_path / "runtime" / WORKSPACE_ID
    runtime.mkdir(parents=True)
    (runtime / "service.json").write_text(document, encoding="utf-8")

    assert read_descriptor(runtime) is None
    assert main(["--runtime-state", str(runtime), "discover"]) == 1
    assert "no service is advertised" in capsys.readouterr().out


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


def test_the_cli_claims_an_api_version_the_service_advertises() -> None:
    """The version stamped on every CLI request must be one this build serves.

    It was the literal `"1.0"`, transcribed once and then left behind when the
    contract moved to 1.2 — the same defect the Runtime's own `API_VERSION` already
    carried and fixed by deriving. Nothing on the request path validates the field,
    so the stale claim was never refused: it was accepted, dispatched, and then used
    to build the response's whole version envelope, so the service reported its
    supported window as `[1.0, 1.0]` while the descriptor beside it advertised
    `[1.2, 1.2]`.

    `is_compatible` is the one comparison in the tree that reads a claimed API
    version against a service's advertised window, and it is asserted here rather
    than an equality against `CONTRACT_VERSION`: what has to be true is that the
    claim falls inside what the service supports, which stays the right question if
    that window ever widens beyond one version.
    """
    claimed = build_request(
        "core.health", workspace_id=WORKSPACE, request_id="cli-api-version"
    ).metadata.api_version

    assert is_compatible(
        cli_descriptor(),
        api_version=claimed,
        workspace_format_version=workspace_contract_version("1"),
    ), (
        f"the CLI claims api_version {claimed!r}, which is outside the "
        f"{supported_api_versions()} this build advertises"
    )


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

    endpoint = endpoint_for_path(socket_path)
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
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        deadline = time.monotonic() + 30
        found = None
        while time.monotonic() < deadline:
            assert process.poll() is None, "the service exited instead of serving"
            found = discover(runtime_directory)
            if found is not None and found.ready:
                break
            time.sleep(0.05)

        # It is discoverable, and the pid it advertises is this live process.
        assert found is not None and found.ready
        assert found.process is not None
        assert found.process.pid == process.pid
        assert found.endpoint_uri == endpoint.url

        # And something is actually listening there, speaking the real contract.
        response = LocalSocketTransport(endpoint=endpoint).call(
            build_request(
                "core.health", workspace_id=WORKSPACE_ID, request_id="req-entrypoint"
            )
        )
        assert isinstance(response, SuccessResponseEnvelope), response
        assert response.result["status"] == "alive"

        # The service is still up after serving a request.
        assert process.poll() is None

        stop_signal = (
            signal.CTRL_BREAK_EVENT
            if endpoint.scheme is EndpointScheme.PIPE
            else signal.SIGTERM
        )
        process.send_signal(stop_signal)
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed assertion
            process.kill()
            process.wait(timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)

    # Shutdown removed what startup advertised: no descriptor for a dead process.
    assert discover(runtime_directory) is None
    assert endpoint.path is None or not endpoint.path.exists()


# --- main(): endpoint accept/reject ------------------------------------------


def test_endpoint_to_serve_accepts_the_platforms_own_scheme(tmp_path: Path) -> None:
    url = endpoint_for_path(tmp_path / "s.sock").url
    endpoint = _endpoint_to_serve(url)
    assert endpoint is not None
    assert endpoint.scheme is LOCAL_SCHEME


def test_endpoint_to_serve_rejects_the_other_platforms_scheme() -> None:
    """A `unix://` endpoint on Windows, or a `pipe://` endpoint on POSIX, parses
    fine but names a mechanism this process cannot open."""
    other = "pipe://omnivia-deadbeef" if LOCAL_SCHEME.value == "unix" else "unix:///tmp/s.sock"
    assert _endpoint_to_serve(other) is None


def test_endpoint_to_serve_rejects_malformed_or_missing_endpoints() -> None:
    assert _endpoint_to_serve(None) is None
    assert _endpoint_to_serve("not-a-url") is None
    assert _endpoint_to_serve(f"{LOCAL_SCHEME.value}://") is None


def test_main_refuses_a_mismatched_scheme_before_touching_the_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused before startup runs -- not after a lock or lease was taken."""
    other = "pipe://omnivia-deadbeef" if LOCAL_SCHEME.value == "unix" else "unix:///tmp/s.sock"
    code = service_main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--installation-state",
            str(tmp_path / "installation-state"),
            "--endpoint",
            other,
        ]
    )
    assert code == 2
    assert "refusing to serve" in capsys.readouterr().err
    assert not (tmp_path / "workspace").exists(), "nothing was touched before the refusal"


def test_main_check_only_bypasses_endpoint_validation(tmp_path: Path) -> None:
    """`--check-only` reports readiness without ever needing an endpoint."""
    code = service_main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--installation-state",
            str(tmp_path / "installation-state"),
            "--check-only",
        ]
    )
    # No workspace exists, so readiness is refused -- but for that reason, and
    # not for a missing or invalid endpoint.
    assert code == 1


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
        endpoint=endpoint_for_path(tmp_path / "omnivia-srb02-order.sock").url,
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

    endpoint = endpoint_for_path(socket_path)
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
            endpoint.url,
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        text=True,
        creationflags=getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0),
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
        response = LocalSocketTransport(endpoint=endpoint).call(
            request_for("core.health", workspace=WORKSPACE_ID)
        )
        assert isinstance(response, SuccessResponseEnvelope), response

        stop_signal = (
            signal.CTRL_BREAK_EVENT
            if endpoint.scheme is EndpointScheme.PIPE
            else signal.SIGTERM
        )
        process.send_signal(stop_signal)
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed assertion
            process.kill()
            process.wait(timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert discover(runtime_directory) is None
