"""A real MCP client, over real pipes, against the real server and a real service.

R004's MCP acceptance evidence, executed rather than asserted about: the stdio
stream carries only protocol, `tools/list` is deterministic and matches the
manifest exactly, every advertised tool answers from a governed workspace,
lifecycle, bootstrap and administrative operations are absent from the callable
surface, and a missing workspace is refused rather than created.

The server under test runs in a subprocess (`_mcp_stdio_probe.py`) and is driven
by the official SDK's own `stdio_client`, so the framing, the handshake and the
transport are all the ones a host would use.

**There is no stand-in left, at either end.** Packet C could only drive the call
path with a `ClientTransport` double, because no concrete local transport existed
outside the CLI, and it could only exercise `workspace.inspect`, because that was
the whole exposed surface. Owner resolution 005 R005-01 moved `LocalIpcTransport`
into `omnivia-core-client`, so the probe passes no `transport_factory` and
`server._default_transport_factory` builds the real one; manifest version `1.1`
exposes six operations, and V06-3 landed the five reads that make five of them
answerable. Every call below therefore travels an OVC1 frame over a Unix domain
socket to an `omnivia-core-service` process this module started, and comes back
as that service's own answer.

**One session calls all six, and "all six" is read off the manifest.**
:data:`ARGUMENTS` is keyed by tool name and is asserted to be exactly
`EXPOSURE_MANIFEST`'s tool names in order, so a seventh tool cannot be exposed
without an end-to-end call for it: the coverage check fails first.

**Nothing here is answerable from an empty workspace.** Every assertion names an
identifier `_mcp_v06_3_fixture` seeded and nothing else in the workspace carries,
so a tool that answered from the wrong place, or with an empty page, fails. The
fixture is also the only module in this package's tests that imports the runtime
-- it seeds through the accepted fenced writer and starts the service -- and
`test_only_the_fixture_reaches_the_runtime` is what holds that to one file.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import _mcp_v06_3_fixture as fixture
import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST, tools

PROBE = Path(__file__).parent / "_mcp_stdio_probe.py"

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

#: One call per advertised tool, keyed by tool name and in manifest order.
#:
#: The arguments are the smallest ones that make each answer *checkable* against
#: a seeded fact rather than merely well-formed. `graph_traverse` names the exact
#: seeded version rather than a record id, because the contract's start points are
#: `RecordVersionReference`s; `context_pack_build` states the only v1 mode and a
#: budget large enough that the pack is bounded by the workspace rather than by
#: the budget, so an omission below is the filter chain's decision and not the
#: token cap's.
ARGUMENTS: dict[str, dict[str, Any]] = {
    "workspace_inspect": {},
    "evidence_search": {"query": fixture.SEEDED_TOKEN},
    "knowledge_search": {"query": fixture.SEEDED_TOKEN},
    "memory_search": {"query": fixture.SEEDED_TOKEN},
    "graph_traverse": {
        "start": [
            {
                "record_id": fixture.SOURCE_RECORD_ID,
                "version": fixture.version_of(fixture.SOURCE_RECORD_ID),
            }
        ],
        "direction": "both",
    },
    "context_pack_build": {
        "query": fixture.SEEDED_TOKEN,
        "mode": "deterministic_view",
        "token_budget": 4000,
    },
}

#: Names that must not resolve to a tool, and the R004-06 boundary each one is on.
#: Literal on purpose: a future edit that exposes one of these has to delete the
#: line that says why it must not.
NEVER_A_TOOL: tuple[tuple[str, str], ...] = (
    ("workspace_create", "bootstrap / workspace initialisation"),
    ("service_start", "service lifecycle"),
    ("service_stop", "service lifecycle"),
    ("core_readiness", "service lifecycle"),
    ("admin_configure", "administrative configuration"),
    ("memory_create", "persistent mutation"),
    ("evidence.search", "the operation identifier is not a tool name"),
    ("context_pack.build", "the operation identifier is not a tool name"),
    ("search", "a plausible guess that names nothing"),
)


@pytest.fixture(scope="module")
def live_service() -> Iterator[fixture.GovernedService]:
    """A seeded governed workspace owned by a real service process, for the module.

    Module-scoped because starting one costs a migration, a seeding pass and a
    startup sequence, and every exposed operation declares `side_effect: none`, so
    no test here can leave the workspace different for the next.

    The runtime is imported by the fixture in *this* process, to build the
    workspace and start the service. The MCP server under test never imports it:
    it runs as a separate process reached only over the socket, which is the
    arrangement in which "MCP does not import the runtime" is proven rather than
    asserted.
    """
    with fixture.serving() as service:
        yield service


def parameters(
    service: fixture.GovernedService, *args: str
) -> StdioServerParameters:
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


async def _session_probe(
    service: fixture.GovernedService, *args: str
) -> dict[str, Any]:
    """Drive one full stdio session and bring back everything the client saw."""
    async with (
        stdio_client(parameters(service, *args)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        calls = {
            name: (await session.call_tool(name, arguments)).model_dump(mode="json")
            for name, arguments in ARGUMENTS.items()
        }
        refusals = {
            guessed: (await session.call_tool(guessed, {})).model_dump(mode="json")
            for guessed, _ in NEVER_A_TOOL
        }
        listed_again = await session.list_tools()
        return {
            "server_name": initialized.server_info.name,
            "tools": [tool.model_dump(mode="json") for tool in listed.tools],
            "tools_again": [
                tool.model_dump(mode="json") for tool in listed_again.tools
            ],
            "calls": calls,
            "refusals": refusals,
        }


def session(service: fixture.GovernedService, *args: str) -> dict[str, Any]:
    return anyio.run(lambda: _session_probe(service, *args))


@pytest.fixture(scope="module")
def observed(live_service: fixture.GovernedService) -> dict[str, Any]:
    """One session, reused: spawning a server per assertion is the slow way."""
    return session(live_service)


def advertised(observed: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """The tool document `tools/list` advertised for one name, as the client saw it.

    Read off the session rather than off `manifest.tools()` on purpose: the schemas
    the assertions below validate against are then the ones that actually crossed
    the wire, not the ones this process happens to hold.
    """
    for tool in observed["tools"]:
        if tool["name"] == tool_name:
            return tool
    raise AssertionError(f"{tool_name} was not advertised")


def structured(observed: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """One successful call's `structuredContent`, refusing anything else.

    A test that read `content[0]` straight would pass on a refusal whose text
    happened to mention the right identifier, so every content assertion starts
    here instead.
    """
    called = observed["calls"][tool_name]
    assert called["is_error"] is False, called
    result = called["structured_content"]
    assert isinstance(result, dict), called
    return result


# --- the protocol works at all ------------------------------------------------


def test_a_real_client_completes_the_handshake(observed: dict[str, Any]) -> None:
    assert observed["server_name"] == "omnivia-core"


# --- tools/list is deterministic and matches the manifest ---------------------


def test_tools_list_matches_the_exposure_manifest_exactly(
    observed: dict[str, Any],
) -> None:
    """"The exposed tools exactly match the approved manifest" -- over the wire.

    Compared as whole documents, not by name: a tool whose schema, annotations or
    provenance drifted from the manifest would pass a name check and fail here.
    """
    listed = observed["tools"]
    assert listed == [tool.model_dump(mode="json") for tool in tools()]
    assert [tool["name"] for tool in listed] == [
        entry.tool_name for entry in EXPOSURE_MANIFEST
    ]


def test_tools_list_is_deterministic_across_calls(observed: dict[str, Any]) -> None:
    assert observed["tools"] == observed["tools_again"]


def test_tools_list_is_deterministic_across_processes(
    live_service: fixture.GovernedService,
) -> None:
    """Two independent server processes advertise byte-identical listings.

    The within-session check above cannot see a listing that varies with the
    environment, the clock, or a dict iteration order that changed at import.
    Two processes can.
    """
    assert session(live_service)["tools"] == session(live_service)["tools"]


def test_every_advertised_tool_is_read_only_and_closed(
    observed: dict[str, Any],
) -> None:
    """Read-only, non-destructive, single-world, and refusing an undeclared key.

    All four are read off the wire rather than off the manifest object, because a
    host decides whether to let a model call a tool from exactly this document.
    """
    for tool in observed["tools"]:
        assert tool["annotations"]["read_only_hint"] is True
        assert tool["annotations"]["destructive_hint"] is False
        assert tool["annotations"]["open_world_hint"] is False
        assert tool["input_schema"]["unevaluatedProperties"] is False
        assert tool["output_schema"]["type"] == "object"
        assert tool["meta"]["omnivia.manifestVersion"] == "1.1"

    inspect = advertised(observed, "workspace_inspect")
    assert inspect["meta"]["omnivia.operation"] == "workspace.inspect"
    assert inspect["input_schema"]["properties"] == {}
    assert inspect["input_schema"]["required"] == []


# --- one session calls all six ------------------------------------------------


def test_the_session_calls_exactly_the_advertised_six(
    observed: dict[str, Any],
) -> None:
    """The coverage check, and the reason a seventh tool cannot land untested.

    Order and membership, against the manifest rather than against a literal, so
    this file cannot drift into calling five of six and passing.
    """
    assert list(ARGUMENTS) == [entry.tool_name for entry in EXPOSURE_MANIFEST]
    assert list(observed["calls"]) == list(ARGUMENTS)
    for name in ARGUMENTS:
        assert observed["calls"][name]["is_error"] is False, observed["calls"][name]


@pytest.mark.parametrize("tool_name", list(ARGUMENTS))
def test_every_request_validates_against_the_advertised_input_schema(
    observed: dict[str, Any], tool_name: str
) -> None:
    """A model may only send what `tools/list` said it may send.

    Validated against the advertised document itself, so this is the check a host
    would run before dispatching -- and it is what makes the calls below evidence
    about the exposed surface rather than about a payload only this file knows.
    """
    Draft202012Validator(advertised(observed, tool_name)["input_schema"]).validate(
        ARGUMENTS[tool_name]
    )


@pytest.mark.parametrize("tool_name", list(ARGUMENTS))
def test_every_structured_result_validates_against_the_advertised_output_schema(
    observed: dict[str, Any], tool_name: str
) -> None:
    """`1.0` advertised no output schema, so a host could not check what came back.

    The schemas are self-contained projections of the canonical contracts, so this
    resolves entirely offline -- which is the only way a host could run it.
    """
    Draft202012Validator(advertised(observed, tool_name)["output_schema"]).validate(
        structured(observed, tool_name)
    )


@pytest.mark.parametrize("tool_name", list(ARGUMENTS))
def test_every_success_carries_one_json_text_item_equal_to_its_structured_content(
    observed: dict[str, Any], tool_name: str
) -> None:
    """The mirror `server._call_tool` writes, checked as a mirror.

    A host that predates structured content still receives the whole answer, and
    one that has both can tell they agree. Parsed rather than compared as text,
    because the claim is that the two carry the same document -- and exactly one
    item, so a second, differently-shaped rendering of the answer cannot appear
    beside it.
    """
    called = observed["calls"][tool_name]
    (item,) = called["content"]
    assert item["type"] == "text"
    assert json.loads(item["text"]) == called["structured_content"]


# --- each tool answers from the seeded workspace ------------------------------


def test_workspace_inspect_returns_the_fixture_workspace(
    observed: dict[str, Any],
) -> None:
    """The workspace id and display name were written into a temporary workspace
    by this module's fixture moments earlier; no stand-in, cache or default in
    either package could produce them."""
    workspace = structured(observed, "workspace_inspect")["workspace"]
    assert workspace["workspace_id"] == fixture.WORKSPACE_ID
    assert workspace["display_name"] == fixture.WORKSPACE_NAME
    assert workspace["status"] == "active"
    assert workspace["created_at"] == fixture.WORKSPACE_CREATED_AT
    # Nested past the top level on purpose: `dict(response.result)` converted
    # only the outer mapping and left this one a `mappingproxy`, which
    # `to_canonical_json` refuses. Reading it here is what keeps that fixed.
    assert workspace["compatibility"]["status"] == "compatible"


def test_evidence_search_returns_the_one_seeded_l0_artifact(
    observed: dict[str, Any],
) -> None:
    """L0, and exactly one of it.

    The workspace holds two artifacts and only this one's locator carries the
    seeded token, so a handler that answered from the wrong index, or ignored the
    query, returns the wrong count. The complete artifact comes back -- capture
    history included -- which is what distinguishes evidence from governed truth.
    """
    result = structured(observed, "evidence_search")
    (artifact,) = result["evidence"]
    assert artifact["evidence_id"] == fixture.EVIDENCE_ID
    assert artifact["source"]["locator"] == fixture.EVIDENCE_LOCATOR
    assert artifact["workspace_id"] == fixture.WORKSPACE_ID
    assert artifact["tombstoned"] is False
    assert [event["action"] for event in artifact["provenance_history"]] == ["captured"]


@pytest.mark.parametrize("tool_name", ["knowledge_search", "memory_search"])
def test_the_governed_searches_return_sealed_current_governed_records(
    observed: dict[str, Any], tool_name: str
) -> None:
    """Governed truth, not evidence: the three seeded records, each accepted,
    canonical and current.

    Both operations default to `current_canonical`, and nothing here asks for
    another view, so a candidate or superseded version reaching this page would be
    the "never by omission" rule breaking.
    """
    records = structured(observed, tool_name)["records"]
    by_id = {record["provenance"]["identity"]["record_id"]: record for record in records}
    assert set(by_id) == {
        fixture.SOURCE_RECORD_ID,
        fixture.TARGET_RECORD_ID,
        fixture.RELATION_RECORD_ID,
    }
    seeded = by_id[fixture.SOURCE_RECORD_ID]
    identity = seeded["provenance"]["identity"]
    assert identity["version"] == fixture.version_of(fixture.SOURCE_RECORD_ID)
    assert identity["governance_state"] == "accepted"
    assert identity["currentness"] == "current"
    assert identity["layer"] == "l2"
    assert seeded["authority_level"] == "canonical"
    assert seeded["record_type"] == fixture.RECORD_TYPE
    assert seeded["domain_scope"] == fixture.DOMAIN_SCOPE
    assert fixture.SEEDED_TOKEN in seeded["content"]["statement"]


def test_graph_traverse_returns_the_seed_and_the_sealed_relation(
    observed: dict[str, Any],
) -> None:
    """The named seed at depth 0, the record the relation reaches at depth 1, and
    the relation itself as an edge.

    Depth 0 is exactly the requested start set, so a traversal that returned the
    seed alone -- or that reached a record no seeded relation joins -- fails on the
    depths rather than on the membership.
    """
    result = structured(observed, "graph_traverse")
    depths = {
        node["reference"]["record_id"]: node["depth"] for node in result["nodes"]
    }
    assert depths == {fixture.SOURCE_RECORD_ID: 0, fixture.TARGET_RECORD_ID: 1}

    (edge,) = result["edges"]
    assert edge["relation_type"] == fixture.RELATION_TYPE
    assert edge["source"] == {
        "record_id": fixture.SOURCE_RECORD_ID,
        "version": fixture.version_of(fixture.SOURCE_RECORD_ID),
    }
    assert edge["target"] == {
        "record_id": fixture.TARGET_RECORD_ID,
        "version": fixture.version_of(fixture.TARGET_RECORD_ID),
    }
    # The edge is the sealed governed relation record itself, not a bare pointer.
    assert edge["relation_reference"]["record_id"] == fixture.RELATION_RECORD_ID
    assert edge["record"]["record_type"] == fixture.RELATION_RECORD_TYPE
    assert result["freshness"]["stale"] is False


def test_context_pack_build_returns_a_cited_pack_needing_fresh_authorization(
    observed: dict[str, Any],
) -> None:
    """Every section carries seeded content and a citation that resolves, the pack
    cites the seeded evidence and both seeded records, and holding it grants
    nothing.

    `fresh_authorization_required` is the last of those and the one a model must
    act on: the pack is a view, not a permission. The authorization context is
    checked too, because it is where the *request's* claim arrives -- the purpose
    below is the exposure manifest's `knowledge_retrieval`, carried from a tool
    call, through the envelope, to the authority the builder attested.
    """
    pack = structured(observed, "context_pack_build")
    assert pack["fresh_authorization_required"] is True
    assert pack["mode"] == "deterministic_view"
    assert pack["query"] == fixture.SEEDED_TOKEN

    citations = {citation["citation_id"]: citation for citation in pack["citations"]}
    assert pack["sections"], "an empty pack cites nothing and proves nothing"
    for section in pack["sections"]:
        assert fixture.SEEDED_TOKEN in section["content"], section
        assert section["citation_ids"], section
        for citation_id in section["citation_ids"]:
            assert citation_id in citations, section

    cited_evidence = {
        citation["evidence_reference"]["evidence_id"]
        for citation in citations.values()
        if "evidence_reference" in citation
    }
    cited_records = {
        citation["record_reference"]["record_id"]
        for citation in citations.values()
        if "record_reference" in citation
    }
    assert cited_evidence == {fixture.EVIDENCE_ID}
    assert {fixture.SOURCE_RECORD_ID, fixture.TARGET_RECORD_ID} <= cited_records

    authorization = pack["reproducibility"]["authorization_context"]
    assert authorization["workspace_id"] == fixture.WORKSPACE_ID
    assert authorization["purpose"] == "knowledge_retrieval"
    assert authorization["pre_ranking_authorization_enforced"] is True


# --- what is not callable, and what the service itself refuses ----------------


@pytest.mark.parametrize(
    ("guessed", "boundary"), NEVER_A_TOOL, ids=[name for name, _ in NEVER_A_TOOL]
)
def test_a_name_absent_from_the_manifest_is_not_callable(
    observed: dict[str, Any], guessed: str, boundary: str
) -> None:
    """R004-06: the allow-list is the only lookup, so absent means uncallable.

    Lifecycle, bootstrap, administrative configuration and mutation are all here,
    and so are the two shapes a model actually guesses: the *operation identifier*
    -- which resolves to a real catalogue entry everywhere except here -- and a
    bare verb.

    The refusal costs no dial, and this test cannot see that: it is over the wire,
    where a refused call and an unreachable service look alike.
    `test_a_guessed_tool_name_refuses_before_a_transport_exists`, in the manifest
    suite, is where a transport factory that raises when constructed proves it.
    """
    refusal = observed["refusals"][guessed]
    assert refusal["is_error"] is True, boundary
    assert refusal["structured_content"] is None
    message = refusal["content"][0]["text"]
    assert guessed in message
    assert "is not a tool this server exposes" in message


async def _one_call(
    service: fixture.GovernedService, workspace_id: str
) -> dict[str, Any]:
    """One tool call against the live service, naming a workspace of our choosing."""
    async with (
        stdio_client(
            StdioServerParameters(
                command=sys.executable,
                args=[
                    str(PROBE),
                    "--endpoint",
                    service.endpoint_uri,
                    "--workspace-id",
                    workspace_id,
                ],
            )
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        called = await session.call_tool("workspace_inspect", {})
        return called.model_dump(mode="json")


def test_a_service_refusal_is_relayed_as_the_services_own_error(
    live_service: fixture.GovernedService,
) -> None:
    """The other live branch: the service answers *no*, and MCP relays that.

    The test above covers a refusal MCP makes for itself, before a request exists.
    This covers the one MCP does not make: a well-formed request the service itself
    declines. The two are different code paths -- the first never reaches a
    transport, this one completes a full round trip and comes back an error
    envelope rather than a success one.

    Untested until R005-01, because a stand-in that returns a
    `SuccessResponseEnvelope` can never produce it. The workspace named below is
    real to the request builder and unknown to the service, so the refusal is the
    service's own judgement, carried back over the same socket as any answer.
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


# --- the boundary this suite is arranged to prove -----------------------------


def test_only_the_fixture_reaches_the_runtime() -> None:
    """One file in this package's tests may import the runtime, and this is it.

    The production boundary itself is asserted in the manifest suite, in a fresh
    interpreter. This is the arrangement that keeps that assertion meaningful: the
    workspace under test has to be *built*, which only the runtime can do, so the
    honest form of "MCP never imports it" is a single seeding module in-process and
    a server subprocess that reaches the answer over a socket. A second test module
    that imported the runtime would not break the server, but it would make this
    suite's topology an accident rather than a design.
    """
    forbidden = re.compile(r"\bomnivia_core_(?:runtime|cli)\b")
    for module in sorted(Path(__file__).parent.glob("*.py")):
        if module.name == "_mcp_v06_3_fixture.py":
            continue
        for line in module.read_text(encoding="utf-8").splitlines():
            statement = line.strip()
            if statement.startswith(("import ", "from ")):
                assert not forbidden.search(statement), f"{module.name}: {statement}"


def test_the_probe_constructs_no_transport_of_its_own(
    observed: dict[str, Any],
) -> None:
    """Every answer above came through `omnivia-core-client`, not through a double.

    Two halves. The probe declares no transport and no operation stand-in -- its
    only factory delegates to `server._default_transport_factory`, which the
    manifest suite pins to the client package's own `LocalIpcTransport` -- and the
    session above brought back workspace-specific values that no double in this
    tree holds. Neither alone is sufficient: source without answers would not show
    the seam was used, and answers without source would not show what carried them.
    """
    source = PROBE.read_text(encoding="utf-8")
    assert "class " not in source, "a transport or operation double needs a class"
    # The call form, not the name: the prose above the code names it twice.
    assert source.count("_default_transport_factory(") == 1
    assert "transport_factory=factory" in source

    assert structured(observed, "workspace_inspect")["workspace"]["workspace_id"] == (
        fixture.WORKSPACE_ID
    )


# --- stdout is protocol-only --------------------------------------------------


def test_the_stdio_stream_carries_only_protocol_even_under_contamination(
    live_service: fixture.GovernedService,
) -> None:
    """R004-07: stdout is protocol-only, proved against a server trying to break it.

    The probe writes to `sys.stdout` twice from inside a live handler, on every
    call -- six of them now. If any reached the wire the session below would fail
    to parse a frame; instead every call completes and the strings are nowhere in
    what the client received.
    """
    contaminated = session(live_service, "--contaminate")
    assert contaminated["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    for name in ARGUMENTS:
        assert contaminated["calls"][name]["is_error"] is False, name
    serialised = json.dumps(contaminated)
    assert "CONTAMINATION-FROM-A-HANDLER" not in serialised
    assert "CONTAMINATION-VIA-PRINT" not in serialised


def test_every_byte_the_server_writes_to_stdout_is_valid_protocol(
    live_service: fixture.GovernedService,
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
