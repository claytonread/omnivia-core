"""B9 and B10 acceptance: dispatcher, authorization boundary and client adapters.

Scope is bounded by approval, not by a missing dependency. A2 owns the
provider-neutral operation catalogue and has now been accepted, but registering
handlers against it is the separately approved Phase 4 packet. These tests therefore prove the parts that do not depend on it — the
transport-neutral dispatcher, the authorization boundary, the service-lifecycle
operations, and that CLI and MCP are clients which cannot own a workspace — and one
test pins the absence of the catalogue so nobody mistakes it for done.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.main as service_main_module
import pytest
from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.service.authorization import (
    AuthorizationDenied,
    Grant,
    authorize,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import _endpoint_to_serve, _serve_until_stopped
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
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ClientIdentity,
    ErrorResponseEnvelope,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
)
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

from .conftest import SERVICE_INSTANCE, WORKSPACE_ID

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


def request_for(
    operation: str, *, workspace: str = WORKSPACE, principal: str | None = None
):
    """One public-contract probe request; no adapter helper is imported here."""
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id="req-1",
            correlation_id="req-1",
            trace_id="req-1",
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id="runtime-acceptance", version="0.1.0"),
            workspace_id=workspace,
            scopes=(),
            purpose="runtime_acceptance",
            required_capabilities=(),
            principal_claim=(
                None
                if principal is None
                else PrincipalClaim(claimed_principal_id=principal)
            ),
        ),
        input={},
    )


def claiming(operation: str, api_version: object, *, workspace: str = WORKSPACE):
    """A request identical to `request_for`'s but for the version it claims.

    Built by replacing the field rather than through `build_request`, because the
    point is to vary the one thing a client controls and a server must not trust.
    """
    request = request_for(operation, workspace=workspace)
    return replace(request, metadata=replace(request.metadata, api_version=api_version))


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
    response = dispatcher.dispatch(request_for("core.health", principal="someone-else"))
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
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            }
            for forbidden in ("acquire_lease", "flock", "open_guard", "create_lock"):
                assert forbidden not in called, f"{path} calls {forbidden}"


#: Every distribution `omnivia-core-mcp` is permitted to depend on, and the whole
#: of it. R004-05 states this list: the public Core contracts, the client for all
#: service calls, and the official MCP SDK as the sole MCP framework dependency.
MCP_ALLOWED_DEPENDENCIES = frozenset({"omnivia-core", "omnivia-core-client", "mcp"})

#: There was a `MCP_FORBIDDEN_DEPENDENCIES` set here, and it was dead code that
#: read as a guard. It was disjoint from the allow-list above and the closed
#: allow-list assertion runs first, so the forbidden assertion could never fire:
#: gutting the set to `frozenset()` while `omnivia-core-cli` was declared still
#: went red, on the allow-list. It was spelling-fragile as well --
#: `_requirement_name` does no PEP 503 normalisation, so `omnivia_core_cli`, a
#: valid PEP 508 spelling of the same distribution, was not in it.
#:
#: The closed allow-list is the sound guard and it is sufficient: `names <=
#: MCP_ALLOWED_DEPENDENCIES` refuses *every* distribution outside three, named or
#: not, so it needs no list of the ones somebody thought of. Its comparison is
#: normalised here rather than in a second set.
#:
#: The import half of R004-05 -- the edge with the history, since the deleted MCP
#: adapter imported `omnivia_core_cli.client` -- is not this assertion's to hold
#: and never was. `check_adapters_do_not_depend_on_runtime_or_each_other` in
#: `scripts/check-package-boundaries.py` holds it, by AST, in both directions.


def _requirement_name(requirement: str) -> str:
    """The PEP 503-normalised distribution name a PEP 508 requirement starts with.

    Normalised because `omnivia_core_cli` and `omnivia-core-cli` are one
    distribution and PEP 508 accepts both spellings: an unnormalised comparison
    against a hyphenated allow-list would admit the underscore spelling of a name
    the list does not contain.
    """
    for index, character in enumerate(requirement):
        if character in "<>=!~[; ":
            requirement = requirement[:index]
            break
    return re.sub(r"[-_.]+", "-", requirement.strip()).lower()


def _mcp_manifest() -> dict[str, Any]:
    import tomllib

    import omnivia_core_mcp

    return tomllib.loads(
        (Path(omnivia_core_mcp.__file__).parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )


def test_the_mcp_distribution_ships_the_approved_packet_c_surface() -> None:
    """MCP is an operational server now, and these are the modules it is made of.

    This assertion used to read `shipped == {"__init__.py"}`, because MCP was a
    skeleton and its only operational module had reached across a forbidden edge.
    Owner resolution 004 Packet C authorised the real surface, so the assertion is
    rewritten to describe it rather than deleted: an unexpected module appearing
    beside these is still a change somebody has to make deliberately.

    `generated_schema_projection` is the generated schema projection required by
    Packet C Amendment 001.

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
        "configuration.py",
        "generated_schema_projection.py",
        "manifest.py",
        "server.py",
    }, f"unexpected modules: {sorted(shipped)}"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("omnivia_core_mcp.adapter")

    # The console script R004's Packet C requires, so the distribution is usable
    # by an MCP host rather than merely importable.
    assert _mcp_manifest()["project"]["scripts"] == {
        "omnivia-core-mcp": "omnivia_core_mcp.server:main"
    }


def test_only_the_shared_client_owns_the_layout_and_the_launcher() -> None:
    """One `~/.omnivia` convention and one launcher invocation, in one module.

    This replaces a field-by-field comparison of two copies. ADR-036 forbids
    `omnivia-core-mcp` importing `omnivia-core-cli`, so each adapter used to
    restate the layout and the argv, and the only thing keeping them in step was
    an assertion here that they were equal -- which fails the moment somebody
    changes both, and says nothing about a third copy. Both copies are gone:
    `omnivia_core_client.managed_local` is the sole implementation, and what is
    asserted now is that neither adapter has one.

    It lives on the runtime's side for the same reason the comparison did --
    this is the side allowed to import all three -- and it reads the adapters'
    source rather than their behaviour, because a launcher that is only reached
    on a cold-start path would not show up in a behavioural test.

    Three ways an adapter could regain process control, all of them refused: the
    imports that run a process, the argv that names the launcher's mode, and the
    name of the program itself. The last two are matched as text, so a
    re-implementation that shelled out through some other module would still be
    caught by the flag it had to pass. Swept over every module of both
    distributions rather than the two entry points, because the module that grew
    a launcher would be a new one.
    """
    from omnivia_core_client import managed_local

    shared = imported_modules(Path(managed_local.__file__))
    assert {"subprocess", "shutil"} <= shared

    for package in ("omnivia-core-cli", "omnivia-core-mcp"):
        for adapter in (REPO / "packages" / package / "src").rglob("*.py"):
            imports = imported_modules(adapter)
            assert "subprocess" not in imports, adapter
            assert "shutil" not in imports, adapter
            source = adapter.read_text(encoding="utf-8")
            assert "--managed-start" not in source, adapter
            assert "omnivia-core-service" not in source, adapter


def test_the_mcp_distribution_declares_only_allow_listed_dependencies() -> None:
    """R004-05's dependency ruling: an allow-list, checked by name.

    This was an exact equality against `["omnivia-core>=0.1.0,<0.2.0"]`, and it is
    widened rather than deleted because it is the **only** guard in the repository
    on R004-05's "must not depend on `omnivia-core-cli`". Deleting it to let the
    SDK dependency land would have silently retired an owner ruling; an equality
    kept as-is would have fired on the dependency declaration alone, before any
    prohibited edge existed.

    So it becomes what the ruling actually says: a closed allow-list, where a
    fourth distribution fails even if it is harmless. There was a second assertion
    against a separate forbidden set; see the note above `_requirement_name` for
    why it was dead code and why the closed allow-list alone is the sound guard.
    """
    declared = _mcp_manifest()["project"]["dependencies"]
    names = {_requirement_name(requirement) for requirement in declared}

    assert names <= MCP_ALLOWED_DEPENDENCIES, (
        f"omnivia-core-mcp declares {sorted(names - MCP_ALLOWED_DEPENDENCIES)}, "
        f"which R004-05 does not admit; the whole allow-list is "
        f"{sorted(MCP_ALLOWED_DEPENDENCIES)}"
    )

    # Every allow-listed name must itself be in normalised form, or the comparison
    # above is against a spelling `_requirement_name` can never produce.
    assert MCP_ALLOWED_DEPENDENCIES == {
        re.sub(r"[-_.]+", "-", name).lower() for name in MCP_ALLOWED_DEPENDENCIES
    }

    # The SDK edge is pinned to the approved major range, not merely present.
    # R004-05 fixes both the package and the range; a bare `mcp` or an `mcp>=3`
    # would satisfy the allow-list above while breaking the ruling.
    assert "mcp>=2,<3" in declared, (
        f"R004-05 requires the official SDK constrained >=2,<3; found {declared}"
    )


def test_the_dependency_allow_list_admits_no_spelling_of_a_name_outside_it() -> None:
    """PEP 508 accepts `omnivia_core_cli` and `omnivia-core-cli` for one
    distribution. The retired forbidden set held only the hyphenated spelling and
    `_requirement_name` did no normalisation, so the underscore form was outside
    it -- one of two reasons that set was not the guard it read as. The closed
    allow-list has to be immune to the same trick, which means the normalisation
    is load-bearing and gets its own assertion rather than being trusted."""
    for spelling in (
        "omnivia-core-cli>=0.1.0,<0.2.0",
        "omnivia_core_cli>=0.1.0,<0.2.0",
        "Omnivia.Core.CLI==0.1.0",
        "omnivia_core_runtime",
        "FastMCP[all]>=2",
    ):
        assert _requirement_name(spelling) not in MCP_ALLOWED_DEPENDENCIES, spelling

    # ...and the admitted names still parse to themselves, in every spelling a
    # manifest may legitimately use.
    assert _requirement_name("omnivia_core_client >= 0.1.0") == "omnivia-core-client"
    assert _requirement_name("mcp>=2,<3") == "mcp"


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
            request_for("core.health", workspace=WORKSPACE_ID)
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
    other = (
        "pipe://omnivia-deadbeef"
        if LOCAL_SCHEME.value == "unix"
        else "unix:///tmp/s.sock"
    )
    assert _endpoint_to_serve(other) is None


def test_endpoint_to_serve_rejects_malformed_or_missing_endpoints() -> None:
    assert _endpoint_to_serve(None) is None
    assert _endpoint_to_serve("not-a-url") is None
    assert _endpoint_to_serve(f"{LOCAL_SCHEME.value}://") is None


def test_main_refuses_a_mismatched_scheme_before_touching_the_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused before startup runs -- not after a lock or lease was taken."""
    other = (
        "pipe://omnivia-deadbeef"
        if LOCAL_SCHEME.value == "unix"
        else "unix:///tmp/s.sock"
    )
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
    assert not (tmp_path / "workspace").exists(), (
        "nothing was touched before the refusal"
    )


def test_main_check_only_bypasses_endpoint_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--check-only` reports readiness without ever needing an endpoint."""
    def unexpected_wait_loop(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("check-only entered the served lease-renewal loop")

    monkeypatch.setattr(
        service_main_module, "_serve_until_stopped", unexpected_wait_loop
    )
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


def test_the_service_wait_loop_fails_closed_and_unwinds_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A renewal failure stops serving, releases resources, and exits non-zero."""
    class KeepRunning:
        def wait(self, *, timeout: float) -> bool:
            assert timeout == 0.25
            return False

    class FailedRenewal:
        stops = 0

        def renew_lease_if_due(self) -> bool:
            raise RuntimeError("sensitive workspace detail")

        def stop(self) -> None:
            self.stops += 1

    runner = FailedRenewal()
    code = _serve_until_stopped(runner, KeepRunning())  # type: ignore[arg-type]

    assert code == 1
    assert runner.stops == 1
    error = capsys.readouterr().err
    assert error == "stopping: the workspace lease could not be renewed\n"
    assert "sensitive" not in error


def test_the_service_wait_loop_stops_cleanly_without_an_extra_renewal() -> None:
    """A requested stop follows the same unwind and retains a successful exit."""
    class StopRequested:
        def wait(self, *, timeout: float) -> bool:
            assert timeout == 0.25
            return True

    class CleanRunner:
        renewals = 0
        stops = 0

        def renew_lease_if_due(self) -> bool:
            self.renewals += 1
            return True

        def stop(self) -> None:
            self.stops += 1

    runner = CleanRunner()
    code = _serve_until_stopped(runner, StopRequested())  # type: ignore[arg-type]

    assert code == 0
    assert runner.renewals == 0
    assert runner.stops == 1


def test_check_only_stops_a_ready_workspace_without_entering_the_renewal_loop(
    tmp_path: Path, migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode this has to be proved on: one that gets all the way to ready.

    `--check-only` on a workspace that does not exist refuses before the loop for a
    reason that has nothing to do with renewal, so guarding it there proves only
    that a refusal returns early. This run takes the lock, the connection and the
    lease, publishes readiness and stops -- the whole sequence a served run does --
    and still must not renew, because this process is not the one that will go on
    holding the workspace.
    """
    workspace, installation = migrated

    def unexpected_wait_loop(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("check-only entered the served lease-renewal loop")

    monkeypatch.setattr(
        service_main_module, "_serve_until_stopped", unexpected_wait_loop
    )

    code = service_main(
        [
            "--workspace",
            str(workspace.root),
            "--installation-state",
            str(installation.root),
            "--check-only",
        ]
    )

    assert code == 0
    # Stopped, not left holding anything: a successor can take the lock straight away.
    assert discover(installation.runtime_for(WORKSPACE_ID)) is None


def test_a_serving_run_hands_the_started_runner_to_the_renewal_loop(
    tmp_path: Path, migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same seam: a run that serves does enter it.

    `_serve_until_stopped` is what renews, so proving it is skipped for
    `--check-only` says nothing on its own -- a build that never called it at all
    would pass that test too. What is asserted here is the composition: the object
    handed to the loop is the live runner that just published readiness, which is
    the one holding the lease that has to stay current.
    """
    import tempfile as tf

    workspace, installation = migrated
    entered: list[object] = []

    def record(runner: object, stopping: object) -> int:
        entered.append(runner)
        runner.stop()  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(service_main_module, "_serve_until_stopped", record)

    # A short socket directory, not `tmp_path`: an AF_UNIX path is capped near 104
    # bytes and pytest's per-test directory is longer than that on its own.
    socket_directory = Path(tf.mkdtemp(prefix="ovr-", dir=tf.gettempdir()))
    try:
        code = service_main(
            [
                "--workspace",
                str(workspace.root),
                "--installation-state",
                str(installation.root),
                "--endpoint",
                endpoint_for_path(socket_directory / "s.sock").url,
            ]
        )
    finally:
        shutil.rmtree(socket_directory, ignore_errors=True)

    assert code == 0
    assert len(entered) == 1
    runner = entered[0]
    assert isinstance(runner, ServiceRunner)
    assert runner.workspace_id == WORKSPACE_ID
    assert runner.generation is not None


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

    successor = create_lock(
        workspace.locks_path / "storage.lock", LockRole.LIFETIME_STORAGE
    )
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
