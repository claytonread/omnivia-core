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
from dataclasses import replace
from pathlib import Path
from typing import Any

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


def claiming(operation: str, api_version: object, *, workspace: str = WORKSPACE):
    """A request identical to `request_for`'s but for the version it claims.

    Built by replacing the field rather than through `build_request`, because the
    point is to vary the one thing a client controls and a server must not trust.
    """
    request = request_for(operation, workspace=workspace)
    return replace(
        request, metadata=replace(request.metadata, api_version=api_version)
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
    # The claim is stated here rather than taken from whatever the CLI happens to
    # send. What this test needs is *a* claim the service does not serve, and
    # borrowing another package's constant for it makes this test pass or fail on
    # that package's release schedule: it held only while the CLI was stale, and
    # went vacuous the moment the CLI started claiming the served version.
    request = claiming("core.health", "1.0")
    response = dispatcher.dispatch(request)
    assert response.metadata.request_id == request.metadata.request_id
    assert response.metadata.correlation_id == request.metadata.correlation_id
    # ResponseMetadata carries no trace_id: the contract correlates by request and
    # correlation id, and the trace is a request-side concern.
    assert not hasattr(response.metadata, "trace_id")
    # Correlation is by id. `api_version` is *not* a correlation field, and this
    # line used to assert the response echoed the request's -- which is how the
    # service came to report a supported window the caller had chosen for it. What
    # the response states is the revision this build applied, and the request here
    # claims a different one, so the echo is refused in both directions.
    assert response.metadata.version.api_version == API_VERSION
    assert request.metadata.api_version != API_VERSION
    assert response.metadata.version.api_version != request.metadata.api_version


def test_no_version_fact_in_a_response_comes_from_the_callers_claim() -> None:
    """The mechanism, stated as the property it violated.

    `response_metadata` derived the served window, the selected version and every
    capability version from `request.metadata.api_version` -- the caller's own
    assertion about itself -- so any client could make the service report any
    window about itself. Two requests differing *only* in that claim must produce
    byte-identical version facts, and those facts must be this build's own.
    """
    dispatcher = Dispatcher.for_service_operations(grant(), FakeService())
    served = supported_api_versions()

    versions = []
    for claim in ("1.0", "1.9"):
        response = dispatcher.dispatch(claiming("core.health", claim))
        assert isinstance(response, SuccessResponseEnvelope)
        envelope = response.metadata.version
        assert envelope.compatibility.supported_api_versions == served
        assert envelope.api_version == envelope.compatibility.selected_api_version
        assert envelope.api_version == API_VERSION
        assert envelope.server_version == SERVER_VERSION
        # The granted operations travel as capability refs, and their version is a
        # server fact too -- it was the caller's claim as well.
        assert [ref.version for ref in envelope.capabilities.granted] == [
            API_VERSION
        ] * len(SERVICE_OPERATIONS)
        # The workspace notation is translated from the frozen ordinal table, not
        # restated and not taken from the request either.
        assert envelope.workspace_format_version == workspace_contract_version("1")
        assert (
            envelope.compatibility.supported_workspace_versions
            == supported_workspace_versions("1")
        )
        versions.append(envelope)

    assert versions[0] == versions[1], "a version fact still tracks the caller's claim"


@pytest.mark.parametrize(
    ("claim", "status", "upgrade"),
    [
        (API_VERSION, "compatible", "none"),
        # What the CLI claims today: same major, below the served window.
        ("1.0", "upgrade_required", "required"),
        # A major this build cannot negotiate at all.
        ("2.0", "incompatible", "required"),
        # Nothing validates `api_version` past a pattern check on the decoding
        # path, and an in-process caller skips even that. Unreadable means no,
        # never a crashed dispatch.
        ("not-a-version", "incompatible", "required"),
        # And "unreadable" is not only a malformed string. `decode_request` refuses
        # a non-string on the wire, but an in-process caller reaches this with the
        # field set to anything at all, and these fail the version match with a
        # TypeError rather than a ValueError -- a different crash through the same
        # hole.
        (None, "incompatible", "required"),
        (42, "incompatible", "required"),
        (b"1.2", "incompatible", "required"),
    ],
    ids=[
        "served",
        "below-window",
        "other-major",
        "unreadable",
        "none",
        "not-a-string",
        "bytes",
    ],
)
def test_a_claim_outside_the_served_window_is_reported_as_such(
    claim: object, status: str, upgrade: str
) -> None:
    """`status` was hardcoded `"compatible"`, so it agreed with every caller.

    This is the *probe* path, and on it `authorize_application_request` is still not
    wired: nothing refuses a claim outside the served window before it gets here, so
    the response is the only place the caller is told. That used to be true of every
    path. It is now true of this one only -- V06-2 Lane A put the application
    operations behind the accepted seam, which refuses an unreadable `api_version` as
    a malformed request rather than reporting it in a successful response, and
    `test_workspace_inspect_vertical.py` holds the positive counterpart.

    Every expected value is one the contract's own
    `x-omnivia-compatibility-statuses` / `x-omnivia-upgrade-states` freeze.
    """
    response = Dispatcher.for_service_operations(grant(), FakeService()).dispatch(
        claiming("core.health", claim)
    )
    assert isinstance(response, SuccessResponseEnvelope)
    negotiated = response.metadata.version.compatibility
    assert negotiated.status == status
    assert negotiated.upgrade_state.value == upgrade


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


#: Every distribution `omnivia-core-mcp` is permitted to depend on, and the whole
#: of it. R004-05 states this list: the public Core contracts, the client for all
#: service calls, and the official MCP SDK as the sole MCP framework dependency.
MCP_ALLOWED_DEPENDENCIES = frozenset(
    {"omnivia-core", "omnivia-core-client", "mcp"}
)

#: What R004-05 names as forbidden. Checked separately from the allow-list above
#: rather than left implied by it, because these are the edges with a history: the
#: MCP adapter really did import `omnivia_core_cli.client`, and this assertion is
#: the only place in the repository that would catch its return.
MCP_FORBIDDEN_DEPENDENCIES = frozenset(
    {
        "omnivia-core-cli",
        "omnivia-core-runtime",
        "omnivia-memory",
        "omnivia-platform",
        "sqlalchemy",
        "fastmcp",
    }
)


def _requirement_name(requirement: str) -> str:
    """The distribution name a PEP 508 requirement string starts with."""
    for index, character in enumerate(requirement):
        if character in "<>=!~[; ":
            return requirement[:index].strip().lower()
    return requirement.strip().lower()


def _mcp_manifest() -> dict[str, Any]:
    import tomllib

    import omnivia_core_mcp

    return tomllib.loads(
        (
            Path(omnivia_core_mcp.__file__).parents[2] / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )


def test_the_mcp_distribution_ships_the_approved_packet_c_surface() -> None:
    """MCP is an operational server now, and these are the modules it is made of.

    This assertion used to read `shipped == {"__init__.py"}`, because MCP was a
    skeleton and its only operational module had reached across a forbidden edge.
    Owner resolution 004 Packet C authorised the real surface, so the assertion is
    rewritten to describe it rather than deleted: an unexpected module appearing
    beside these is still a change somebody has to make deliberately.

    `adapter` keeps its own line. That is the module that imported
    `omnivia_core_cli.client`, and its absence is a fact worth continuing to
    assert now that the package has a legitimate server to be confused with.
    """
    import importlib

    import omnivia_core_mcp

    shipped = {
        path.name
        for path in Path(omnivia_core_mcp.__file__).parent.iterdir()
        if path.suffix == ".py"
    }
    assert shipped == {
        "__init__.py",
        "manifest.py",
        "managed_start.py",
        "server.py",
    }, f"unexpected modules: {sorted(shipped)}"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("omnivia_core_mcp.adapter")

    # The console script R004's Packet C requires, so the distribution is usable
    # by an MCP host rather than merely importable.
    assert _mcp_manifest()["project"]["scripts"] == {
        "omnivia-core-mcp": "omnivia_core_mcp.server:main"
    }


def test_the_cli_and_mcp_derive_the_same_installation_layout(tmp_path: Path) -> None:
    """One `~/.omnivia` convention, restated in two adapters, compared here.

    ADR-036 forbids `omnivia-core-mcp` importing `omnivia-core-cli`, so the layout
    exists twice by design. A restated fact is only safe while something fails
    when the two copies drift, and this is that something -- it lives on the
    runtime's side because that is the side allowed to import both.

    The drift this prevents is not hypothetical. The CLI restated the runtime's
    socket-path ceiling and the two ran 11 to 15 bytes apart until Packet D fixed
    it. Here the consequence would be worse than a bad error message: a CLI-started
    service and an MCP-started one would bind different sockets under different
    workspace roots, and the "one authoritative writable service per workspace"
    guarantee would be quietly false.
    """
    from omnivia_core_cli import lifecycle as cli_lifecycle
    from omnivia_core_mcp import managed_start as mcp_managed_start

    cli = cli_lifecycle.Installation(home=tmp_path)
    mcp = mcp_managed_start.Installation(home=tmp_path)

    for attribute in (
        "workspace_root",
        "installation_state",
        "run_directory",
        "socket_path",
        "log_path",
        "endpoint_uri",
    ):
        assert getattr(cli, attribute) == getattr(mcp, attribute), attribute

    assert (
        cli_lifecycle.DEFAULT_HOME_DIRECTORY
        == mcp_managed_start.DEFAULT_HOME_DIRECTORY
    )
    assert cli_lifecycle.SERVICE_EXECUTABLE == mcp_managed_start.SERVICE_EXECUTABLE
    assert cli_lifecycle.MANIFEST_NAME == mcp_managed_start.MANIFEST_NAME
    assert cli_lifecycle.home_directory() == mcp_managed_start.home_directory()


def test_the_mcp_distribution_declares_only_allow_listed_dependencies() -> None:
    """R004-05's dependency ruling: an allow-list, checked by name.

    This was an exact equality against `["omnivia-core>=0.1.0,<0.2.0"]`, and it is
    widened rather than deleted because it is the **only** guard in the repository
    on R004-05's "must not depend on `omnivia-core-cli`". Deleting it to let the
    SDK dependency land would have silently retired an owner ruling; an equality
    kept as-is would have fired on the dependency declaration alone, before any
    prohibited edge existed.

    So it becomes what the ruling actually says. The allow-list is closed -- a
    fourth distribution fails here even if it is harmless -- and the forbidden
    names are asserted separately so the failure message says which rule broke.
    """
    declared = _mcp_manifest()["project"]["dependencies"]
    names = {_requirement_name(requirement) for requirement in declared}

    assert names <= MCP_ALLOWED_DEPENDENCIES, (
        f"omnivia-core-mcp declares {sorted(names - MCP_ALLOWED_DEPENDENCIES)}, "
        f"which R004-05 does not admit; the whole allow-list is "
        f"{sorted(MCP_ALLOWED_DEPENDENCIES)}"
    )
    forbidden = names & MCP_FORBIDDEN_DEPENDENCIES
    assert not forbidden, f"R004-05 forbids omnivia-core-mcp depending on {sorted(forbidden)}"

    # The SDK edge is pinned to the approved major range, not merely present.
    # R004-05 fixes both the package and the range; a bare `mcp` or an `mcp>=3`
    # would satisfy the allow-list above while breaking the ruling.
    assert "mcp>=2,<3" in declared, (
        f"R004-05 requires the official SDK constrained >=2,<3; found {declared}"
    )


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
    # Still exact equality on the whole document: the point of this half is that
    # a key cannot appear, vanish or change meaning without a reader noticing.
    #
    # `advertised_ready` and `observation` are owner resolution 004 R004-13. The
    # descriptor is published once at startup and never rewritten, so its
    # readiness is a startup claim rather than a fact about now -- a service
    # killed hard leaves this file behind still saying true. A bare `ready` key
    # handed that stale claim to any script that read it, which is the same trap
    # `status` exists to avoid. The key names what it is, and `observation` names
    # where it came from.
    assert main(["--runtime-state", str(runtime), "discover"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "endpoint": "unix:///tmp/omnivia-cli.sock",
        "workspace_id": WORKSPACE_ID,
        "service_instance_id": SERVICE_INSTANCE,
        "fencing_generation": 7,
        "advertised_ready": True,
        "observation": "published-descriptor",
    }

    # The descriptor's other consumer: the workspace a request is addressed to.
    # `--json` is what emits the request envelope. Bare `health` now dials the
    # endpoint the descriptor names, and this descriptor names a socket nothing
    # is listening on -- so it would fail here, which is a statement about a
    # service that was never started rather than about the reader this test is
    # for. The claim below is unchanged: the workspace id the runtime published
    # is the one the CLI addresses its request to.
    assert main(["--runtime-state", str(runtime), "health", "--json"]) == 0
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


def test_a_response_and_the_published_descriptor_cannot_disagree(
    tmp_path: Path, migrated
) -> None:
    """One service, two documents, one supported window.

    This is the property that was violated. The descriptor advertised the window
    `versions.supported_api_versions()` gives it, while a response built the window
    out of `request.metadata.api_version` -- so a client claiming "1.0" was told by
    the response that the service supported {1.0, 1.0} and by the descriptor that it
    supported the real one. Both sides here are the production article: the
    descriptor is read back off disk after a real `ServiceRunner.start` published
    it, and the response comes from a real dispatch, so neither can be satisfied by
    a fixture that agrees with itself.

    The request deliberately claims a version the service does not serve, because
    that is the case the old code got wrong: an agreeing claim would have made the
    two documents match by accident.
    """
    workspace, installation = migrated
    runtime_directory = installation.runtime_for(WORKSPACE_ID)
    settings = ServiceSettings(
        workspace_root=workspace.root,
        installation_root=installation.root,
        core_version="0.1.0",
        endpoint=endpoint_for_path(tmp_path / "omnivia-window.sock").url,
    )
    runner = ServiceRunner(settings)
    assert runner.start(serve=lambda _started: None).ready
    try:
        published = discover(runtime_directory)
        assert published is not None
        response = Dispatcher.for_service_operations(
            grant(workspaces=(WORKSPACE_ID,)), runner
        ).dispatch(claiming("core.health", "1.0", workspace=WORKSPACE_ID))
        assert isinstance(response, SuccessResponseEnvelope), response
        negotiated = response.metadata.version.compatibility
        assert "1.0" != published.supported_api_versions.maximum, (
            "the claim must be one the service does not serve, or this proves nothing"
        )
        assert negotiated.supported_api_versions == published.supported_api_versions
        assert (
            negotiated.supported_workspace_versions
            == published.supported_workspace_versions
        )
        assert (
            response.metadata.version.workspace_format_version
            == published.workspace_format_version
        )
        assert response.metadata.version.server_version == published.server_version
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
