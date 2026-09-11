"""Architecture v0.6 section-21 MCP gates, executed against a real service.

Each test here carries the exact `pending_test_id` the architecture-gate ledger
(`tests/fixtures/service_conformance/architecture-gate-traceability-v1.json`)
assigned to its gate, and the ledger names this file as evidence for that gate
(other gates' evidence lives in other suites the ledger names).
`tests/service_conformance/test_architecture_gate_traceability.py` holds the two
together: a gate recorded as accepted must name a test function that exists and
that no pytest skip can reach -- which is why this module has no `skipif`.

**One service, two transports, one workspace state.** `live_service` is the
seeded governed workspace `_mcp_v06_3_fixture` builds, owned by one real
`omnivia-core-service` process that serves the local socket *and* authenticated
loopback HTTP. The HTTP half is reached through the fixture's test-only embedder
because the standalone service entry point intentionally ships no credential
resolver and so no credential-to-session policy; the embedder supplies that
policy for this test only, and nothing else about the service differs from
production. `managed_local` sessions attach through the published descriptor;
`service_client` is MCP's outbound Core HTTP client mode, dialing that listener
with a bearer the host's injected resolver supplies -- the only way that mode is
ever given a credential.

**The MCP server under test is the production one.** Stdio sessions run the
production server in a subprocess through `_mcp_stdio_probe.py`, which builds it
the way the console path does but bypasses `server.main`; `server.main` itself
is run in a subprocess wherever a refusal before initialization is the claim.
The two-mode comparison drives `server.build_server` in-process through the
official SDK's own `Client`, because the console entry point has no resolver by
design and a host that has one embeds exactly `connect` plus the server built
here. This module imports neither the runtime nor the CLI.
"""

from __future__ import annotations

import copy
import http.client
import json
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import _mcp_v06_3_fixture as fixture
import anyio
import pytest
from mcp import Client, ClientSession
from mcp.client.stdio import stdio_client
from omnivia_core_client import (
    Credential,
    CredentialMissingError,
    CredentialReference,
    EndpointUnavailableError,
)
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import (
    McpConfiguration,
    McpConfigurationError,
    parse_configuration,
)
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST, tools
from test_mcp_stdio_end_to_end import (
    ALL_PURPOSES,
    ARGUMENTS,
    PRINCIPAL_ID,
    configuration_file,
    parameters,
    session,
)

#: The one bearer the embedded HTTP listener accepts. A test value: the service's
#: test embedder takes it on its command line, and the MCP side receives it only
#: through an injected resolver -- never through the MCP configuration, its
#: environment or its argv.
BEARER = "mcp-architecture-gate-bearer"
CREDENTIAL_REFERENCE = "core-http"

#: The only first-party distributions and import roots the base MCP server may
#: reach. Stated positively, so Desktop, Dev, Platform, the runtime and the CLI
#: are all excluded without this file having to guess their names.
MCP_FIRST_PARTY_DISTRIBUTIONS = frozenset(
    {"omnivia-core", "omnivia-core-client", "omnivia-core-mcp"}
)
MCP_FIRST_PARTY_IMPORT_ROOTS = frozenset(
    {"omnivia_core", "omnivia_core_client", "omnivia_core_mcp"}
)

#: Per-call clock facts, as paths into a tool's structured content. Two calls in
#: the *same* mode differ in exactly these and nothing else: the instant a
#: traversal or pack was resolved, and the pack digest computed over a document
#: that carries those instants. Everything else must be identical across modes.
CLOCK_FACTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "graph_traverse": (("freshness", "as_of"),),
    "context_pack_build": (
        ("pack_id",),
        ("reproducibility", "artifact_checksum"),
        ("reproducibility", "canonical_resolution_time"),
        ("reproducibility", "freshness", "as_of"),
        ("reproducibility", "generated_at"),
    ),
}


# --- a live service, and the two ways MCP reaches it ----------------------------


@pytest.fixture(scope="module")
def live_service() -> Iterator[fixture.GovernedService]:
    with fixture.serving(http_credential=BEARER) as service:
        yield service


def _document(service: fixture.GovernedService, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": PRINCIPAL_ID,
        "allowed_workspace_ids": [service.workspace_id],
        "allowed_purposes": list(ALL_PURPOSES),
    }
    document.update(overrides)
    return document


def managed_configuration(service: fixture.GovernedService) -> McpConfiguration:
    return parse_configuration(
        _document(
            service,
            service_mode="managed_local",
            installation_state=str(service.installation_state),
        )
    )


def remote_document(service: fixture.GovernedService) -> dict[str, Any]:
    assert service.http_endpoint is not None
    return _document(
        service,
        service_mode="service_client",
        endpoint=service.http_endpoint,
        credential_reference=CREDENTIAL_REFERENCE,
    )


def resolver(secret: str | None) -> Any:
    """A host's trusted resolver: one secret for the configured reference only."""

    def resolve(reference: CredentialReference, _origin: str) -> Credential | None:
        assert reference == CredentialReference(CREDENTIAL_REFERENCE)
        return None if secret is None else Credential(secret)

    return resolve


async def _in_process(
    connected: server.ConnectedSession, calls: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    async with Client(server.build_server(session=connected)) as client:
        return {
            name: (await client.call_tool(name, arguments)).model_dump(mode="json")
            for name, arguments in calls.items()
        }


def call_all(
    connected: server.ConnectedSession, calls: dict[str, dict[str, Any]] = ARGUMENTS
) -> dict[str, dict[str, Any]]:
    """Every call through the production MCP server, over the official client."""
    try:
        return anyio.run(lambda: _in_process(connected, calls))
    finally:
        connected.clear_credentials()


def _without_clock_facts(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(result)
    for path in CLOCK_FACTS.get(tool_name, ()):
        parent = stripped
        for key in path[:-1]:
            parent = parent[key]
        assert path[-1] in parent, (tool_name, path)
        del parent[path[-1]]
    return stripped


async def _stdio(
    config: Path, calls: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """One real stdio session: the listing, then each call in order."""
    async with (
        stdio_client(parameters(config)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as stdio,
    ):
        await stdio.initialize()
        listed = await stdio.list_tools()
        return {
            "tools": [tool.model_dump(mode="json") for tool in listed.tools],
            "calls": [
                (await stdio.call_tool(name, arguments)).model_dump(mode="json")
                for name, arguments in calls
            ],
        }


def _main(config: Path) -> subprocess.CompletedProcess[str]:
    """`omnivia-core-mcp --config <config>`, the console entry point, as a host runs it."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnivia_core_mcp.server import main; raise SystemExit(main())",
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _listener_status(endpoint: str, bearer: str) -> int:
    """The service listener's status for one bodiless application POST.

    The listener checks the bearer before reading a byte of the body, so `401`
    here is its authentication refusal and anything else means the bearer passed.
    """
    host, port = endpoint.removeprefix("http://").rsplit(":", 1)
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.request(
            "POST",
            "/v1/application",
            body=b"",
            headers={"Authorization": f"Bearer {bearer}", "Content-Length": "0"},
        )
        return connection.getresponse().status
    finally:
        connection.close()


def _private_file(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


# --- the installed dependency and import boundary --------------------------------


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match is not None, requirement
    return re.sub(r"[-_.]+", "-", match.group()).lower()


def _base_requirements(distribution: str) -> list[str]:
    """Declared requirements outside any extra: the base install's own."""
    return [
        requirement
        for requirement in metadata.requires(distribution) or []
        if "extra ==" not in requirement and "extra==" not in requirement
    ]


def _distribution_closure(root: str) -> set[str]:
    """Every distribution `root` requires, transitively, as installed here.

    Markers are ignored rather than evaluated, so the set is a superset of what
    any one platform installs -- the conservative direction for a "never
    depends on" claim. Extras-only requirements are not part of the base install
    and are skipped.
    """
    seen: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requirements = _base_requirements(name)
        except metadata.PackageNotFoundError:
            continue
        pending.extend(_requirement_name(requirement) for requirement in requirements)
    return seen


def _first_party(names: set[str]) -> set[str]:
    return {name for name in names if name.startswith("omnivia")}


def _fresh_import_roots() -> set[str]:
    """Every `omnivia*` import root a fresh, isolated interpreter loads for the server.

    `-I` drops `PYTHONPATH`, the user site and the working directory, so this is
    what an MCP host's own process holds after importing the console module --
    not whatever this pytest process has already collected.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import json, sys; import omnivia_core_mcp.server; "
                "print(json.dumps(sorted({n.split('.')[0] for n in sys.modules "
                "if n.startswith('omnivia')})))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return set(json.loads(completed.stdout))


# --- g07: MCP operates independently of the desktop application ------------------


def test_architecture_gate_mcp_desktop_independence(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """A host starts the MCP server directly and it serves every tool; no Desktop.

    Three facts, each a different way independence could fail. The installed
    dependency closure of `omnivia-core-mcp` holds no first-party distribution
    but Core's contracts and shared client, so nothing Desktop ships can be
    required. A fresh interpreter importing the console module loads no other
    first-party code, so nothing Desktop ships is reached lazily either. And a
    real stdio session -- the SDK host spawning a real subprocess that builds the
    production server through `_mcp_stdio_probe.py` from nothing but a trusted
    configuration path (the probe bypasses `server.main`, not the server's
    construction) -- answers all six tools from a service that was started
    independently of any Desktop process.
    """
    assert _first_party(_distribution_closure("omnivia-core-mcp")) == (
        MCP_FIRST_PARTY_DISTRIBUTIONS
    )
    assert _fresh_import_roots() == MCP_FIRST_PARTY_IMPORT_ROOTS

    config = configuration_file(
        tmp_path,
        installation_state=live_service.installation_state,
        workspace_id=live_service.workspace_id,
    )
    observed = session(config)
    assert [tool["name"] for tool in observed["tools"]] == [
        entry.tool_name for entry in EXPOSURE_MANIFEST
    ]
    for name in ARGUMENTS:
        assert observed["calls"][name]["is_error"] is False, observed["calls"][name]
    assert observed["calls"]["workspace_inspect"]["structured_content"]["workspace"][
        "workspace_id"
    ] == live_service.workspace_id


# --- g08: MCP network mode is authenticated and loopback-safe by default ---------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.0.2.10:8080",  # cleartext off loopback
        "http://core.example.com",  # cleartext to a name
        "http://localhost:8080",  # a name, even a loopback one
        "http://user:secret@127.0.0.1:8080",  # a credential in the endpoint
    ],
)
def test_the_network_endpoint_must_be_loopback_cleartext_or_tls(endpoint: str) -> None:
    """The configuration refuses every endpoint that is not loopback-safe."""
    document = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": PRINCIPAL_ID,
        "allowed_workspace_ids": ["ws-network-01"],
        "allowed_purposes": list(ALL_PURPOSES),
        "service_mode": "service_client",
        "endpoint": endpoint,
        "credential_reference": CREDENTIAL_REFERENCE,
    }
    with pytest.raises(McpConfigurationError):
        parse_configuration(document)


def test_architecture_gate_mcp_network_auth_loopback_default(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """MCP's network mode is `service_client`, and it is closed by default.

    `service_client` is MCP's outbound Core HTTP client mode: the MCP process
    dials a Core service's HTTP endpoint and exposes no HTTP listener of its
    own. Base MCP remains stdio to its host and injects no credential resolver;
    only a trusted embedding host can inject one. That mode is proven closed
    from every side against a real authenticated loopback listener:

    * **By default it cannot start.** The console entry point has no credential
      resolver, so a valid network configuration exits 1 before MCP
      initialization with no byte on stdout.
    * **Missing resolver.** The in-process path raises `StartupError` with the
      same fixed sentence.
    * **Resolver returned no credential.** The shared client raises
      `CredentialMissingError`: nothing is presented to the service.
    * **Wrong bearer.** The service refuses it: the listener answers `401` --
      its authentication-required refusal, a status with no body and no error
      code, by design -- where the correct bearer on the same request passes
      authentication. The client surfaces that as `EndpointUnavailableError`
      with a fixed sentence and no chained cause, and startup fails closed.
      Only the correct bearer connects.
    * **Loopback-safe.** The endpoint that works is a loopback IP literal over
      cleartext; the parametrized test above proves every non-loopback
      cleartext, hostname and credential-bearing endpoint is refused.
    """
    config = _private_file(tmp_path / "remote.json", remote_document(live_service))
    completed = _main(config)
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "credential resolver" in completed.stderr
    assert str(live_service.http_endpoint) not in completed.stderr

    configuration = parse_configuration(remote_document(live_service))
    assert configuration.endpoint is not None
    assert configuration.endpoint.startswith("http://127.0.0.1:")

    with pytest.raises(
        server.StartupError,
        match="^remote service mode requires an injected trusted credential resolver$",
    ):
        server.connect(configuration)

    with pytest.raises(CredentialMissingError) as missing:
        server.connect(configuration, credential_resolver=resolver(None))
    assert type(missing.value) is CredentialMissingError
    assert missing.value.args == ("no credential is held for the live endpoint",)

    # The service's own answer to the wrong bearer, read at the wire: the client
    # collapses it to a payload-free transport failure, so only the listener can
    # say it was an authentication refusal rather than a dead endpoint.
    assert _listener_status(configuration.endpoint, "not-the-bearer") == 401
    assert _listener_status(configuration.endpoint, BEARER) != 401
    with pytest.raises(EndpointUnavailableError) as wrong:
        server.connect(configuration, credential_resolver=resolver("not-the-bearer"))
    assert type(wrong.value) is EndpointUnavailableError
    assert wrong.value.args == ("live discovery call did not complete",)
    assert wrong.value.__cause__ is None
    assert "not-the-bearer" not in repr(wrong.value)

    connected = server.connect(configuration, credential_resolver=resolver(BEARER))
    assert connected.status == "connected"
    inspected = call_all(connected, {"workspace_inspect": {}})["workspace_inspect"]
    assert inspected["is_error"] is False, inspected
    assert inspected["structured_content"]["workspace"]["workspace_id"] == (
        live_service.workspace_id
    )


# --- g17: the base MCP server has no dependency on OmniVia Dev -------------------


def test_architecture_gate_base_mcp_no_dev_dependency() -> None:
    """Declared, installed and imported: Core contracts, the shared client, the SDK.

    The declared dependencies of the installed `omnivia-core-mcp` distribution
    are exactly `omnivia-core`, `omnivia-core-client` and `mcp`; the transitive
    closure adds no first-party distribution; and a fresh interpreter importing
    the server loads no first-party module outside those three roots. A Dev
    Module -- or anything else first-party -- is therefore neither required nor
    reachable, which is stronger than checking for any one name.
    """
    declared = {
        _requirement_name(requirement)
        for requirement in _base_requirements("omnivia-core-mcp")
    }
    assert declared == {"omnivia-core", "omnivia-core-client", "mcp"}
    assert _first_party(_distribution_closure("omnivia-core-mcp")) == (
        MCP_FIRST_PARTY_DISTRIBUTIONS
    )
    assert _fresh_import_roots() == MCP_FIRST_PARTY_IMPORT_ROOTS


# --- g20: managed-local and service-client return equivalent authorized results --


def test_architecture_gate_mcp_mode_authorized_result_equivalence(
    live_service: fixture.GovernedService,
) -> None:
    """The same workspace state answers the same six calls the same way, both modes.

    One real service owns the workspace and serves both transports, so the state
    is literally the same: `managed_local` attaches over the local socket through
    the published descriptor with the production local client transport, and
    `service_client` dials authenticated HTTP with the production HTTP client
    transport. Each session connects to the same service instance, and every
    successful answer is identical after removing the per-call clock facts in
    :data:`CLOCK_FACTS` -- which differ between two calls in one mode too. A
    value-level refusal is the service's own in both, with the same code.

    Scope: the HTTP side's credential-to-session policy is supplied by the
    fixture's test-only embedder, because the standalone service entry point
    intentionally has none. This proves result equivalence given such a policy;
    it does not imply that Core ships a remote authority.
    """
    local = server.connect(managed_configuration(live_service))
    remote = server.connect(
        parse_configuration(remote_document(live_service)),
        credential_resolver=resolver(BEARER),
    )
    assert local.status == "attached"
    assert remote.status == "connected"
    assert local.client.descriptor.service_instance_id == (
        remote.client.descriptor.service_instance_id
    )
    assert local.workspace_id == remote.workspace_id == live_service.workspace_id

    calls = {**ARGUMENTS}
    refusal = {"query": fixture.SEEDED_TOKEN, "limit": 0}
    over_local = call_all(local, calls)
    over_remote = call_all(remote, calls)
    refused_local = call_all(
        server.connect(managed_configuration(live_service)),
        {"knowledge_search": refusal},
    )["knowledge_search"]
    refused_remote = call_all(
        server.connect(
            parse_configuration(remote_document(live_service)),
            credential_resolver=resolver(BEARER),
        ),
        {"knowledge_search": refusal},
    )["knowledge_search"]

    for name in ARGUMENTS:
        assert over_local[name]["is_error"] is False, over_local[name]
        assert over_remote[name]["is_error"] is False, over_remote[name]
        assert _without_clock_facts(name, over_local[name]["structured_content"]) == (
            _without_clock_facts(name, over_remote[name]["structured_content"])
        ), name

    for refused in (refused_local, refused_remote):
        assert refused["is_error"] is True
        assert refused["structured_content"] is None
    codes = [
        json.loads(r["content"][0]["text"].split("refused by the service: ", 1)[1])[
            "error"
        ]["code"]
        for r in (refused_local, refused_remote)
    ]
    assert codes == ["invalid_request", "invalid_request"]


# --- g22 (MCP portion): MCP never owns the workspace service lease ---------------


def test_architecture_gate_clients_never_own_workspace_lease(tmp_path: Path) -> None:
    """An attached stdio MCP session leaves the service the observed lease owner.

    Partial evidence: attached stdio MCP only. It does not cover an MCP-managed
    service launch, and the gate also names Desktop, whose evidence lives in
    omnivia-platform -- which is why the ledger keeps it pending. A dedicated
    service here, because the lease row is only readable once the service has
    stopped: it holds the database in exclusive locking mode for its whole life.

    Before and after a real stdio session that calls every tool, the published
    descriptor names the same service instance at the same fencing generation,
    and the service is still running -- MCP did not stop what it attached to.
    The row the stopped service leaves then names that instance and that
    service's own pid as holder, at the same generation. `acquire_lease` bumps
    the generation on every acquisition, so an unchanged generation means no
    acquisition was observed in between. The claim is equality of those
    descriptor and lease facts and the observed owner, not that any file was
    left byte-for-byte untouched.
    """
    with fixture.serving() as service:
        before = service.descriptor()
        config = configuration_file(
            tmp_path,
            installation_state=service.installation_state,
            workspace_id=service.workspace_id,
        )
        observed = session(config)
        for name in ARGUMENTS:
            assert observed["calls"][name]["is_error"] is False, observed["calls"][name]

        after = service.descriptor()
        assert service.process.poll() is None, "MCP stopped the service it attached to"
        assert after.service_instance_id == before.service_instance_id
        assert after.fencing_generation == before.fencing_generation

        lease = service.stop_and_read_lease()
        assert lease.workspace_id == service.workspace_id
        assert lease.service_instance_id == before.service_instance_id
        assert lease.process_pid == service.process.pid
        assert lease.fencing_generation == before.fencing_generation
        assert lease.takeover_predecessor is None


# --- g29: stdio MCP cannot enumerate ungranted workspaces ------------------------


UNGRANTED_WORKSPACE = "ws-mcp-ungranted-01"


def test_architecture_gate_stdio_mcp_workspace_grants(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """Over real stdio, the granted workspace is the only one MCP can name or see.

    * No advertised tool maps to an enumerating operation (`workspace.list`),
      and asking for one by its tool-shaped name is refused before any call.
    * No tool accepts a workspace selector: naming the ungranted workspace as
      an argument is refused by name.
    * The one workspace-describing answer names exactly the granted workspace.
    * A configuration that grants several workspaces with no default is refused
      at startup rather than choosing, and one granting only a nonexistent
      workspace -- one this installation does not have and no service serves --
      is refused at startup too; both before MCP initialization, and neither
      quotes a workspace identifier.
    """
    config = configuration_file(
        tmp_path,
        installation_state=live_service.installation_state,
        workspace_id=live_service.workspace_id,
    )
    observed = anyio.run(
        lambda: _stdio(
            config,
            [
                ("workspace_list", {}),
                ("workspace_inspect", {"workspace_id": UNGRANTED_WORKSPACE}),
                ("workspace_inspect", {}),
            ],
        )
    )
    operations = {tool["meta"]["omnivia.operation"] for tool in observed["tools"]}
    assert operations == {entry.operation for entry in EXPOSURE_MANIFEST}
    assert "workspace.list" not in operations
    assert observed["tools"] == [tool.model_dump(mode="json") for tool in tools()]

    listing, selecting, inspecting = observed["calls"]
    assert listing["is_error"] is True
    assert "is not a tool this server exposes" in listing["content"][0]["text"]
    assert selecting["is_error"] is True
    assert "'workspace_id'" in selecting["content"][0]["text"]
    assert "trusted configuration" in selecting["content"][0]["text"]
    assert inspecting["is_error"] is False, inspecting
    assert inspecting["structured_content"]["workspace"]["workspace_id"] == (
        live_service.workspace_id
    )
    assert UNGRANTED_WORKSPACE not in json.dumps(observed)

    ambiguous = _private_file(
        tmp_path / "ambiguous.json",
        _document(
            live_service,
            allowed_workspace_ids=[live_service.workspace_id, UNGRANTED_WORKSPACE],
            service_mode="managed_local",
            installation_state=str(live_service.installation_state),
        ),
    )
    ungranted = _private_file(
        tmp_path / "ungranted.json",
        _document(
            live_service,
            allowed_workspace_ids=[UNGRANTED_WORKSPACE],
            service_mode="managed_local",
            installation_state=str(live_service.installation_state),
        ),
    )
    state_before = set(live_service.installation_state.rglob("*"))
    for refused_config, reason in (
        (ambiguous, "unambiguous workspace"),
        (ungranted, "could not be started"),
    ):
        completed = _main(refused_config)
        assert completed.returncode == 1, completed.stderr
        assert completed.stdout == ""
        assert reason in completed.stderr
        assert live_service.workspace_id not in completed.stderr
        assert UNGRANTED_WORKSPACE not in completed.stderr
    assert set(live_service.installation_state.rglob("*")) == state_before
