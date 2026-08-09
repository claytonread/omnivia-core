"""A real MCP client, over real pipes, against the real server.

R004's MCP acceptance evidence, executed rather than asserted about: the stdio
stream carries only protocol, `tools/list` is deterministic and matches the
manifest exactly, lifecycle and bootstrap operations are absent from the callable
surface, and a missing workspace is refused rather than created.

The server under test runs in a subprocess (`_mcp_stdio_probe.py`) and is driven
by the official SDK's own `stdio_client`, so the framing, the handshake and the
transport are all the ones a host would use. The single stand-in is the
`ClientTransport` double described in that module -- there is no concrete local
transport to use instead, and that is the packet's one open dependency.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import _mcp_stdio_probe as probe
import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST, tools

PROBE = Path(__file__).parent / "_mcp_stdio_probe.py"


def parameters(*args: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=[str(PROBE), *args])


async def _session_probe(*args: str) -> dict[str, object]:
    """Drive one full stdio session and bring back what the client saw."""
    async with (
        stdio_client(parameters(*args)) as (read_stream, write_stream),
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


def session(*args: str) -> dict[str, object]:
    return anyio.run(lambda: _session_probe(*args))


@pytest.fixture(scope="module")
def observed() -> dict[str, object]:
    """One session, reused: spawning a server per assertion is the slow way."""
    return session()


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


def test_tools_list_is_deterministic_across_processes() -> None:
    """Two independent server processes advertise byte-identical listings.

    The within-session check above cannot see a listing that varies with the
    environment, the clock, or a dict iteration order that changed at import.
    Two processes can.
    """
    assert session()["tools"] == session()["tools"]


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


def test_an_allow_listed_tool_calls_its_catalogue_operation(
    observed: dict[str, object],
) -> None:
    """The tool name maps to the operation the manifest says, and to no other."""
    called = observed["called"]
    assert isinstance(called, dict)
    assert not called["is_error"], called
    payload = json.loads(called["content"][0]["text"])
    assert payload["echoed_operation"] == "workspace.inspect"
    assert payload["workspace_id"] == probe.INSPECT_RESULT["workspace_id"]


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


def test_the_stdio_stream_carries_only_protocol_even_under_contamination() -> None:
    """R004-07: stdout is protocol-only, proved against a server trying to break it.

    The probe writes to `sys.stdout` twice from inside a live handler. If either
    reached the wire the session below would fail to parse a frame; instead every
    call completes and the strings are nowhere in what the client received.
    """
    contaminated = session("--contaminate")
    assert contaminated["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    called = contaminated["called"]
    assert isinstance(called, dict)
    assert not called["is_error"]
    serialised = json.dumps(contaminated)
    assert "CONTAMINATION-FROM-A-HANDLER" not in serialised
    assert "CONTAMINATION-VIA-PRINT" not in serialised


def test_every_byte_the_server_writes_to_stdout_is_valid_protocol() -> None:
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
        [sys.executable, str(PROBE), "--contaminate"],
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
