"""A real MCP client, over real pipes, against the real server and a real service.

R004's MCP acceptance evidence, executed rather than asserted about: the stdio
stream carries only protocol, `tools/list` is deterministic and matches the
manifest exactly, lifecycle and bootstrap operations are absent from the callable
surface, and a missing workspace is refused rather than created.

The server under test runs in a subprocess (`_mcp_stdio_probe.py`) and is driven
by the official SDK's own `stdio_client`, so the framing, the handshake and the
transport are all the ones a host would use.

**There is no stand-in left.** Packet C could only drive the call path with a
`ClientTransport` double, because no concrete local transport existed outside the
CLI. Owner resolution 005 R005-01 moved `LocalIpcTransport` into
`omnivia-core-client`, so the probe passes no `transport_factory` and
`server._default_transport_factory` builds the real one. Every call below
therefore travels an OVC1 frame over a Unix domain socket to an
`omnivia-core-service` process this module started, and comes back as that
service's own answer. That is R005-01's acceptance item "MCP's default transport
factory reaches a live local service end to end".
"""

from __future__ import annotations

import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST, tools

PROBE = Path(__file__).parent / "_mcp_stdio_probe.py"

WORKSPACE_ID = "ws-mcp-end-to-end-01"
SERVICE_INSTANCE = "svc-mcp-1"

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)


class LiveService:
    """One running `omnivia-core-service` and the facts a caller needs to reach it."""

    def __init__(self, endpoint_uri: str, workspace_id: str) -> None:
        self.endpoint_uri = endpoint_uri
        self.workspace_id = workspace_id


@pytest.fixture(scope="module")
def live_service() -> Iterator[LiveService]:
    """A real workspace owned by a real service process, for the module.

    Module-scoped because starting one costs a migration and a startup sequence,
    and the single exposed operation has `side_effect: none`, so no test here can
    leave the workspace different for the next.

    The runtime is imported in *this* process to build the workspace. The MCP
    server under test never imports it: it runs as a separate process reached
    only over the socket, which is the arrangement in which "MCP does not import
    the runtime" is proven rather than asserted.
    """
    from omnivia_core_runtime.ownership.discovery import discover
    from omnivia_core_runtime.service.transport import endpoint_for_path
    from omnivia_core_runtime.storage.backup import InstallationLayout
    from omnivia_core_runtime.storage.legacy import migrate_legacy_database
    from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
    from omnivia_core_runtime.workspace.layout import WorkspaceLayout

    from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

    root = Path(tempfile.mkdtemp(prefix="ovm-workspace-"))
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
            created_at="2026-08-07T00:00:00+00:00",
            name="MCP end to end",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id=SERVICE_INSTANCE,
    )

    # Outside `tmp_path`: R004-15 caps a local endpoint at 86 encoded bytes and
    # pytest's `tmp_path` nests deep enough to exceed it.
    socket_directory = Path(tempfile.mkdtemp(prefix="ovm-", dir=tempfile.gettempdir()))
    endpoint = endpoint_for_path(socket_directory / "s.sock")

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
            found = discover(installation.runtime_for(WORKSPACE_ID))
            if found is not None and found.ready:
                break
            time.sleep(0.05)
        assert found is not None and found.ready, "the service never became ready"
        yield LiveService(endpoint.url, WORKSPACE_ID)
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


def parameters(service: LiveService, *args: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            str(PROBE),
            "--endpoint",
            service.endpoint_uri,
            "--workspace-id",
            service.workspace_id,
            *args,
        ],
    )


async def _session_probe(service: LiveService, *args: str) -> dict[str, object]:
    """Drive one full stdio session and bring back what the client saw."""
    async with (
        stdio_client(parameters(service, *args)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        called = await session.call_tool("workspace_inspect", {})
        unknown = await session.call_tool("workspace_create", {})
        listed_again = await session.list_tools()
        return {
            "server_name": initialized.server_info.name,
            "tools": [tool.model_dump(mode="json") for tool in listed.tools],
            "tools_again": [
                tool.model_dump(mode="json") for tool in listed_again.tools
            ],
            "called": called.model_dump(mode="json"),
            "unknown": unknown.model_dump(mode="json"),
        }


def session(service: LiveService, *args: str) -> dict[str, object]:
    return anyio.run(lambda: _session_probe(service, *args))


@pytest.fixture(scope="module")
def observed(live_service: LiveService) -> dict[str, object]:
    """One session, reused: spawning a server per assertion is the slow way."""
    return session(live_service)


# --- the protocol works at all ------------------------------------------------


def test_a_real_client_completes_the_handshake(observed: dict[str, object]) -> None:
    assert observed["server_name"] == "omnivia-core"


# --- tools/list is deterministic and matches the manifest ---------------------


def test_tools_list_matches_the_exposure_manifest_exactly(
    observed: dict[str, object],
) -> None:
    """"The exposed tools exactly match the approved manifest" -- over the wire.

    Compared as whole documents, not by name: a tool whose schema, annotations or
    provenance drifted from the manifest would pass a name check and fail here.
    """
    listed = observed["tools"]
    assert isinstance(listed, list)
    assert listed == [tool.model_dump(mode="json") for tool in tools()]
    assert [tool["name"] for tool in listed] == [
        entry.tool_name for entry in EXPOSURE_MANIFEST
    ]


def test_tools_list_is_deterministic_across_calls(observed: dict[str, object]) -> None:
    assert observed["tools"] == observed["tools_again"]


def test_tools_list_is_deterministic_across_processes(live_service: LiveService) -> None:
    """Two independent server processes advertise byte-identical listings.

    The within-session check above cannot see a listing that varies with the
    environment, the clock, or a dict iteration order that changed at import.
    Two processes can.
    """
    assert session(live_service)["tools"] == session(live_service)["tools"]


def test_the_advertised_tool_is_read_only_and_takes_no_arguments(
    observed: dict[str, object],
) -> None:
    listed = observed["tools"]
    assert isinstance(listed, list)
    (tool,) = listed
    assert tool["annotations"]["read_only_hint"] is True
    assert tool["annotations"]["destructive_hint"] is False
    assert tool["input_schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert tool["meta"]["omnivia.operation"] == "workspace.inspect"


# --- calling ------------------------------------------------------------------


async def _one_call(service: LiveService, workspace_id: str) -> dict[str, object]:
    """One tool call against the live service, naming a workspace of our choosing."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(PROBE),
            "--endpoint",
            service.endpoint_uri,
            "--workspace-id",
            workspace_id,
        ],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        called = await session.call_tool("workspace_inspect", {})
        return called.model_dump(mode="json")


def test_a_service_refusal_is_relayed_as_the_services_own_error(
    live_service: LiveService,
) -> None:
    """The other live branch: the service answers *no*, and MCP relays that.

    `test_a_tool_absent_from_the_manifest_is_not_callable` covers a refusal MCP
    makes for itself, before a request exists. This covers the one MCP does not
    make: a well-formed request the service itself declines. The two are different
    code paths -- the first never reaches a transport, this one completes a full
    round trip and comes back an error envelope rather than a success one.

    Untested until now, because a stand-in that returns a `SuccessResponseEnvelope`
    can never produce it. The workspace named below is real to the request builder
    and unknown to the service, so the refusal is the service's own judgement,
    carried back over the same socket as any answer.
    """
    called = anyio.run(
        lambda: _one_call(live_service, "ws-a-workspace-this-service-does-not-own")
    )

    assert called["is_error"] is True
    message = called["content"][0]["text"]
    assert "was refused by the service" in message
    # The service's own error contract, relayed intact rather than flattened to a
    # string: a model can branch on `retry_class` exactly as the CLI does.
    refusal = json.loads(message.split("was refused by the service: ", 1)[1])
    assert refusal["error"]["code"] == "workspace_not_granted"
    assert refusal["error"]["retry_class"] == "non_retryable"


def test_an_allow_listed_tool_calls_its_catalogue_operation(
    observed: dict[str, object],
) -> None:
    """R005-01: the default transport factory reaches a live local service.

    This is the acceptance item Packet C could not execute. Nothing here is a
    fixture: the answer below was produced by an `omnivia-core-service` process
    this module started, encoded as an OVC1 frame, carried over a Unix domain
    socket by the `LocalIpcTransport` that `server._default_transport_factory`
    constructed, and decoded through the public contract.

    The two asserted values are what makes it live rather than plausible. The
    workspace id and display name were written into a temporary workspace by this
    module's fixture moments earlier; no stand-in, cache or default in either
    package could produce them.
    """
    called = observed["called"]
    assert isinstance(called, dict)
    assert not called["is_error"], called
    payload = json.loads(called["content"][0]["text"])
    assert payload["workspace"]["workspace_id"] == WORKSPACE_ID
    assert payload["workspace"]["display_name"] == "MCP end to end"
    assert payload["workspace"]["status"] == "active"
    # Nested past the top level on purpose: `dict(response.result)` converted
    # only the outer mapping and left this one a `mappingproxy`, which
    # `to_canonical_json` refuses. Reading it here is what keeps that fixed.
    assert payload["workspace"]["compatibility"]["status"] == "compatible"


def test_a_tool_absent_from_the_manifest_is_not_callable(
    observed: dict[str, object],
) -> None:
    """R004-06: the allow-list is the only lookup, so absent means uncallable.

    `workspace.create` is in the catalogue and is exactly the bootstrap operation
    R004-06 forbids exposing. Asking for it by its tool name is refused before a
    request envelope is built, let alone sent.
    """
    unknown = observed["unknown"]
    assert isinstance(unknown, dict)
    assert unknown["is_error"] is True
    message = unknown["content"][0]["text"]
    assert "workspace_create" in message
    assert "is not a tool this server exposes" in message


# --- stdout is protocol-only --------------------------------------------------


def test_the_stdio_stream_carries_only_protocol_even_under_contamination(
    live_service: LiveService,
) -> None:
    """R004-07: stdout is protocol-only, proved against a server trying to break it.

    The probe writes to `sys.stdout` twice from inside a live handler. If either
    reached the wire the session below would fail to parse a frame; instead every
    call completes and the strings are nowhere in what the client received.
    """
    contaminated = session(live_service, "--contaminate")
    assert contaminated["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    called = contaminated["called"]
    assert isinstance(called, dict)
    assert not called["is_error"]
    serialised = json.dumps(contaminated)
    assert "CONTAMINATION-FROM-A-HANDLER" not in serialised
    assert "CONTAMINATION-VIA-PRINT" not in serialised


def test_every_byte_the_server_writes_to_stdout_is_valid_protocol(
    live_service: LiveService,
) -> None:
    """Read the raw pipe, not the parsed session: every line must be JSON-RPC.

    The client above would have failed on a torn frame, but it would not notice a
    well-formed line the server had no business sending -- nor one sent *after*
    the session closed, which the client has stopped reading by then.

    This found a real leak. The SDK's descriptor claim diverts fd 1 to stderr, so
    a flushed write from a handler misses the wire; an unflushed `print` sat in
    `sys.stdout`'s block buffer until interpreter shutdown, by which time the
    claim was released, and arrived on the real stdout as trailing garbage. The
    `redirect_stdout` in `serve()` is what closes it, and this is the assertion
    that fails if it is ever removed.
    """
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "raw-probe", "version": "0"},
                },
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
        # The call is what runs the contaminating handler; a listing alone would
        # never reach it, and this test would then prove nothing about stray
        # output at all.
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workspace_inspect", "arguments": {}},
            }
        )
        + "\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--endpoint",
            live_service.endpoint_uri,
            "--workspace-id",
            live_service.workspace_id,
            "--contaminate",
        ],
        input=request,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"the server wrote nothing; stderr was {completed.stderr!r}"
    for line in lines:
        message = json.loads(line)  # a non-protocol line fails here
        assert message["jsonrpc"] == "2.0", line
    assert "CONTAMINATION" not in completed.stdout
    # Not merely absent from stdout: both writes are accounted for on stderr, so
    # this cannot pass by the handler having quietly stopped running.
    assert "CONTAMINATION-FROM-A-HANDLER" in completed.stderr
    assert "CONTAMINATION-VIA-PRINT" in completed.stderr


# --- refusing rather than creating --------------------------------------------


def test_the_server_refuses_a_missing_workspace_and_creates_nothing(
    tmp_path: Path,
) -> None:
    """R004-07 and R004-10, end to end through the console entry point.

    `main()` resolves the home, fails managed start, and exits -- writing the
    instruction to stderr and not one byte to stdout, which is what makes the
    failure protocol-safe. Nothing is created under the empty home it was given.
    """
    home = tmp_path / "empty-home"
    home.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnivia_core_mcp.server import main; raise SystemExit(main())",
            "--home",
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == "", "a failed start must write no protocol"
    assert "omnivia init" in completed.stderr
    assert "creates none" in completed.stderr
    assert list(home.rglob("*")) == [], "a refused start created state"
