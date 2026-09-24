"""A real MCP client, over real pipes, against the real server and a real service.

R004's MCP acceptance evidence, executed rather than asserted about: the stdio
stream carries only protocol, `tools/list` is deterministic and matches the
manifest exactly, every advertised tool answers from a governed workspace,
lifecycle, bootstrap and administrative operations are absent from the callable
surface, and a missing workspace is refused rather than created.

The server under test runs in a subprocess (`_mcp_stdio_probe.py`) and is driven
by the official SDK's own `stdio_client`, so the framing, the handshake and the
transport are all the ones a host would use.

**There is no stand-in left, at either end, and no endpoint on a command line.**
V06-6 made the trusted `omnivia.mcp-config.v1` document the only thing the
server is told: this module writes one owner-private file naming the principal,
the single allow-listed workspace, the allowed purposes, the installation state
root and the credential reference, and `server.connect` composes
`omnivia-core-client`'s `ServiceClient` from it -- descriptor read, transport
choice, version negotiation and liveness probe included. Every call below
therefore travels an OVC1 frame over a Unix domain socket to an
`omnivia-core-service` process this module started, reached through the shared
client, and comes back as that service's own answer.

**Every call is made as a dedicated principal this installation issued.** A
managed-local server presents its own bearer or does not start, so the
configurations below are built from `_mcp_v06_3_fixture`'s live setup: the
reference the service filed the bearer under and the principal that bearer
resolves to. Neither is chosen here, and the bearer itself appears nowhere in
this module -- the server reads it from the installation's protected store for
itself.

**The child process gets this interpreter's environment, deliberately.** The SDK
sanitizes a child's environment when `StdioServerParameters.env` is `None`, which
in a worktree drops the `PYTHONPATH` that shadows the installed distributions --
the probe then runs whichever `omnivia_core_mcp` is installed rather than the one
under test, and the failure looks like a stale one-tool manifest rather than like
a harness bug. `_environment()` is what stops that.

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

import ast
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
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
from omnivia_core_client import Deadline, InstallationServiceConfig, stop_managed_local
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST, exposure_manifest, tools

from omnivia_core.contracts.v1 import to_canonical_json

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
    "decision_evaluate": {
        "input": {
            "schema_version": "decision.1",
            "definition_ref": {"id": "core.document_category", "version": "1.0.0"},
            "subject_refs": [{"id": fixture.SOURCE_RECORD_ID, "revision": "r1"}],
            "input": {
                "source_refs": [{"id": fixture.SOURCE_RECORD_ID, "revision": "r1"}]
            },
            "execution": {"mode": "advisory", "privacy": "local_only"},
        },
        "idempotency_key": "decision-evaluate-e2e-1",
    },
    "decision_record_get": {"evaluation_id": "eval-e2e-1"},
    "decision_record_list": {},
    "decision_status": {},
}

#: The four decision tools this contracts slice advertises. The runtime still
#: answers them with its stub refusals -- `not_implemented` for the reads, and
#: `authorization_denied` for the mutation, whose dispatch grant lands with the
#: runtime slice (PR-3) -- so the coverage checks below accept exactly those
#: refusals and nothing else. When the real handlers land, delete this map and
#: the calls become ordinary success assertions.
DECISION_STUB_REFUSALS: dict[str, str] = {
    "decision_evaluate": "authorization_denied",
    "decision_record_get": "not_implemented",
    "decision_record_list": "not_implemented",
    "decision_status": "not_implemented",
}

#: The tools a live session must answer successfully.
SUCCESSFUL_TOOLS: tuple[str, ...] = tuple(
    name for name in ARGUMENTS if name not in DECISION_STUB_REFUSALS
)


def assert_call_outcome(observed: dict[str, Any], name: str) -> None:
    """One call succeeded, or a decision tool was refused by exactly its stub."""
    called = observed["calls"][name]
    if name in DECISION_STUB_REFUSALS:
        assert called["is_error"] is True, called
        assert f'"code":"{DECISION_STUB_REFUSALS[name]}"' in called["content"][0][
            "text"
        ], called
    else:
        assert called["is_error"] is False, called

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


#: The purposes the exposure manifest claims. A configuration that allow-lists
#: exactly these is the one under which all ten tools are callable; the
#: adversarial suite is where a narrower one refuses.
ALL_PURPOSES = (
    "workspace_inspection",
    "knowledge_retrieval",
    "decision_evaluation",
    "decision_record",
    "decision_status",
)


#: What an authoring installation allows: the restricted purposes plus the three
#: the wider profile's tools claim. Every one is the service's own.
AUTHORING_PURPOSES = (
    *ALL_PURPOSES,
    "memory_authoring",
    "content_ingestion",
    "job_observation",
)


#: The principal a configuration claims when nothing has issued it one: the
#: service's own, which is what the HTTP lane in the architecture-gate suite
#: serves under. A managed-local configuration built by :func:`live_configuration`
#: never uses it -- an installed server calls as its own dedicated principal, and
#: `test_the_request_carries_the_configured_principal_claim` in the manifest
#: suite is where the claim is proven at the envelope.
PRINCIPAL_ID = "local-user"


def configuration_file(
    directory: Path,
    *,
    installation_state: Path,
    workspace_id: str,
    credential_reference: str | None = None,
    principal_id: str = PRINCIPAL_ID,
    purposes: tuple[str, ...] = ALL_PURPOSES,
    name: str = "omnivia-mcp.json",
    mutation_enabled: bool = False,
) -> Path:
    """One trusted `omnivia.mcp-config.v1` file, written owner-private.

    The mode matters: the reader proves owner-only from the open descriptor and
    refuses anything else, so a fixture that wrote 0644 would be testing the
    refusal rather than the server.

    `credential_reference` is the opaque *name* the installation filed this
    host's bearer under -- never the bearer, which is not reachable from this
    module at all. It is written only when it is given, because a document
    without one is exactly the shape every installation had before the installed
    setup path existed. This probe calls :func:`server.connect` directly and is
    therefore refused on that shape; the production entry point's automatic
    restricted migration is covered separately.

    `mutation_enabled` is written only when it is true, for the same reason: the
    default file is the one an existing installation already has -- the field
    absent entirely -- rather than a file that states the safe value and would
    pass a check the upgrade rule is about.
    """
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": principal_id,
        "allowed_workspace_ids": [workspace_id],
        "allowed_purposes": list(purposes),
        "service_mode": "managed_local",
        "installation_state": str(installation_state),
    }
    if credential_reference is not None:
        document["credential_reference"] = credential_reference
    if mutation_enabled:
        document["mutation_enabled"] = True
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def live_configuration(
    directory: Path, service: fixture.GovernedService, **overrides: Any
) -> Path:
    """The configuration an installed setup would have written for `service`.

    All four authority-shaped members come off the live setup rather than out of
    this module: the installation root, the one allow-listed workspace, the
    reference the service filed its bearer under, and the dedicated principal
    that bearer resolves to. A configuration naming any other principal is
    refused by the service on the first call -- a claim it did not grant -- so
    this is not merely tidy.
    """
    return configuration_file(
        directory,
        installation_state=service.installation_state,
        workspace_id=service.workspace_id,
        credential_reference=service.credential_reference,
        principal_id=service.principal_id,
        **overrides,
    )


def _environment() -> dict[str, str]:
    """This interpreter's environment, copied for the child.

    See the module docstring: `StdioServerParameters.env=None` makes the SDK
    sanitize the child's environment, which drops the `PYTHONPATH` that selects
    the source tree under test.
    """
    return dict(os.environ)


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


def parameters(config: Path, *args: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(PROBE), "--config", str(config), *args],
        env=_environment(),
    )


def production_parameters(config: Path) -> StdioServerParameters:
    """The installed entry point, with no test admission or transport seam."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "omnivia_core_mcp.server", "--config", str(config)],
        env=_environment(),
    )


async def _session_probe(config: Path, *args: str) -> dict[str, Any]:
    """Drive one full stdio session and bring back everything the client saw."""
    async with (
        stdio_client(parameters(config, *args)) as (read_stream, write_stream),
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


def session(config: Path, *args: str) -> dict[str, Any]:
    return anyio.run(lambda: _session_probe(config, *args))


@pytest.fixture(scope="module")
def live_config(
    live_service: fixture.GovernedService, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """The trusted configuration every session below is started from."""
    return live_configuration(tmp_path_factory.mktemp("mcp-config"), live_service)


@pytest.fixture(scope="module")
def observed(live_config: Path) -> dict[str, Any]:
    """One session, reused: spawning a server per assertion is the slow way."""
    return session(live_config)


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
    """ "The exposed tools exactly match the approved manifest" -- over the wire.

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


def test_tools_list_is_deterministic_across_processes(live_config: Path) -> None:
    """Two independent server processes advertise byte-identical listings.

    The within-session check above cannot see a listing that varies with the
    environment, the clock, or a dict iteration order that changed at import.
    Two processes can.
    """
    assert session(live_config)["tools"] == session(live_config)["tools"]


def test_every_advertised_tool_is_read_only_and_closed(
    observed: dict[str, Any],
) -> None:
    """Read-only, non-destructive, single-world, and refusing an undeclared key.

    All four are read off the wire rather than off the manifest object, because a
    host decides whether to let a model call a tool from exactly this document.
    """
    for tool in observed["tools"]:
        if tool["name"] == "decision_evaluate":
            assert tool["annotations"]["read_only_hint"] is False
            assert tool["input_schema"]["additionalProperties"] is False
        else:
            assert tool["annotations"]["read_only_hint"] is True
            assert tool["input_schema"]["unevaluatedProperties"] is False
        assert tool["annotations"]["destructive_hint"] is False
        assert tool["annotations"]["open_world_hint"] is False
        assert tool["output_schema"]["type"] == "object"
        assert tool["meta"]["omnivia.manifestVersion"] == "2.0"

    inspect = advertised(observed, "workspace_inspect")
    assert inspect["meta"]["omnivia.operation"] == "workspace.inspect"
    assert inspect["input_schema"]["properties"] == {}
    assert inspect["input_schema"]["required"] == []


# --- one session calls all six ------------------------------------------------


def test_the_session_calls_exactly_the_advertised_ten(
    observed: dict[str, Any],
) -> None:
    """The coverage check, and the reason a seventh tool cannot land untested.

    Order and membership, against the manifest rather than against a literal, so
    this file cannot drift into calling nine of ten and passing.
    """
    assert list(ARGUMENTS) == [entry.tool_name for entry in EXPOSURE_MANIFEST]
    assert list(observed["calls"]) == list(ARGUMENTS)
    for name in ARGUMENTS:
        assert_call_outcome(observed, name)


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


@pytest.mark.parametrize("tool_name", SUCCESSFUL_TOOLS)
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


@pytest.mark.parametrize("tool_name", SUCCESSFUL_TOOLS)
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
    observed: dict[str, Any], live_service: fixture.GovernedService
) -> None:
    """The workspace the installation minted for this module moments earlier.

    The identifier is not a literal anywhere: the installation service chose it
    when `_mcp_v06_3_fixture` dispatched `workspace.create`, so no stand-in,
    cache or default in either package could produce it."""
    workspace = structured(observed, "workspace_inspect")["workspace"]
    assert workspace["workspace_id"] == live_service.workspace_id
    assert workspace["display_name"] == fixture.WORKSPACE_NAME
    assert workspace["status"] == "active"
    # The manifest on disk stores the offset spelling `datetime.isoformat()`
    # writes; the wire carries the contract's canonical one. Both halves are
    # asserted, so a handler that passed the stored string through -- which is
    # what the official client refused against the advertised output schema --
    # fails here rather than only under a host.
    assert workspace["created_at"] == live_service.created_at_canonical
    assert workspace["created_at"] != live_service.created_at
    # Nested past the top level on purpose: `dict(response.result)` converted
    # only the outer mapping and left this one a `mappingproxy`, which
    # `to_canonical_json` refuses. Reading it here is what keeps that fixed.
    assert workspace["compatibility"]["status"] == "compatible"


def test_evidence_search_returns_the_one_seeded_l0_artifact(
    observed: dict[str, Any], live_service: fixture.GovernedService
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
    assert artifact["workspace_id"] == live_service.workspace_id
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
    by_id = {
        record["provenance"]["identity"]["record_id"]: record for record in records
    }
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
    depths = {node["reference"]["record_id"]: node["depth"] for node in result["nodes"]}
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
    observed: dict[str, Any], live_service: fixture.GovernedService
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
    assert authorization["workspace_id"] == live_service.workspace_id
    assert authorization["purpose"] == "knowledge_retrieval"
    assert authorization["pre_ranking_authorization_enforced"] is True
    # The authority the service actually applied, and it is the dedicated MCP
    # principal rather than the service's own: a managed-local server presents
    # its installed bearer on every call, so what the pack records is who that
    # bearer resolved to.
    assert authorization["authority"]["principal_id"] == live_service.principal_id


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
    config: Path, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One tool call over a fresh stdio session started from `config`."""
    async with (
        stdio_client(parameters(config)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        called = await session.call_tool(tool_name, arguments)
        return called.model_dump(mode="json")


def test_a_service_refusal_is_relayed_as_the_services_own_error(
    live_config: Path,
) -> None:
    """The other live branch: the service answers *no*, and MCP relays that.

    The tests above cover refusals MCP makes for itself, before a request exists.
    This covers the one MCP does not make: a request whose *keys* the advertised
    schema declares -- so the pre-flight refusal has nothing to say about it --
    and whose *value* the operation contract does not admit. The two are
    different code paths: the first never reaches the client, this one completes
    a full round trip through `ServiceClient` and comes back an error envelope
    rather than a success one.

    Value-level validation being the service's own is the property here. MCP
    checks that a key is advertised and stops; what a `limit` may be is stated
    in the contract the service enforces, and the answer is that service's own
    typed refusal relayed intact.
    """
    called = anyio.run(
        lambda: _one_call(
            live_config, "knowledge_search", {"query": fixture.SEEDED_TOKEN, "limit": 0}
        )
    )

    assert called["is_error"] is True
    assert called["structured_content"] is None
    message = called["content"][0]["text"]
    assert "was refused by the service" in message
    # The service's own error contract, relayed intact rather than flattened to a
    # string: a model can branch on `retry_class` exactly as the CLI does.
    refusal = json.loads(message.split("was refused by the service: ", 1)[1])
    assert refusal["error"]["code"] == "invalid_request"
    assert refusal["error"]["retry_class"] == "non_retryable"


def test_a_root_nobody_configured_refuses_before_anything_is_started(
    tmp_path: Path,
) -> None:
    """V06-6: a root nobody configured cannot reach a service at all.

    The state root below is a bare directory, not the `installation-state` of an
    installation this server understands. **The refusal now lands one step
    earlier than it used to**, and that ordering is the production rule rather
    than an accident of this test: `_connect_managed_local` resolves the
    dedicated principal's credential *before* it asks the shared client to reach
    or start anything, because starting a service this process is about to
    refuse to talk to buys a cold start for a refusal. A bare directory holds no
    protected credential store, so there is nothing to resolve, and the
    configuration below names no reference either -- which is what every
    installation looked like before the installed setup path existed.

    Either way the outcome is the one the rule is about: `--managed-start` is not
    invoked against a root nobody sanctioned, the refusal is before MCP
    initialization -- exit 1, a payload-free sentence on stderr, not one byte on
    stdout -- and nothing is created. The *other* half, an installation that is
    real and simply has no such workspace, is
    `test_the_server_refuses_a_missing_workspace_and_creates_nothing`.
    """
    state = tmp_path / "somebody-elses-state"
    state.mkdir()
    config = configuration_file(
        tmp_path, installation_state=state, workspace_id="ws-not-published-here"
    )
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode != 0
    assert completed.stdout == "", "a refused start must write no protocol"
    assert "ws-not-published-here" not in completed.stderr
    assert str(state) not in completed.stderr
    assert list(state.rglob("*")) == [], "a refused start created state"


def test_a_purpose_outside_the_configuration_refuses_over_the_wire(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """The same six tools are listed; only the granted purpose is callable.

    `tools/list` stays deterministic -- it is not filtered by authority, which
    would make one host's listing differ from another's -- so the model can see
    `knowledge_search` and is refused when it calls it. The refusal is the
    server's own, before the client is asked for anything, and it carries no
    `structuredContent`.
    """
    config = live_configuration(
        tmp_path, live_service, purposes=("workspace_inspection",)
    )
    observed = session(config)

    assert observed["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    assert observed["calls"]["workspace_inspect"]["is_error"] is False
    for name in ARGUMENTS:
        if name == "workspace_inspect":
            continue
        refusal = observed["calls"][name]
        assert refusal["is_error"] is True, name
        assert refusal["structured_content"] is None, name
        assert "purpose" in refusal["content"][0]["text"], name


# --- the authoring profile, over the same real stdio --------------------------
#
# The profile is raised by the protected admission seam and by nothing else, so
# these two tests are the same configuration file run twice: once as production
# runs it, and once with `--authoring` standing in for the installed record the
# setup path writes. Nothing about the document differs between them, which is
# the property being shown. The installed record itself is real now, and
# `test_mcp_standalone_authoring_acceptance` runs that path end to end -- the
# real `omnivia mcp configure`, then the production entry point with no injected
# seam at all.

#: One call per tool the authoring profile adds, in the order one session makes
#: them, plus the replay.
#:
#: **Every input here is one the canonical contract accepts**, because the
#: adapter now decodes an authoring input through `omnivia_core.contracts.v1`'s
#: own decoder before it sends anything: a payload that is merely key-shaped is
#: refused on this side and never becomes evidence about Core at all. So the two
#: writes are real writes -- `evidence_capture` first, then the `memory_create`
#: that cites the artifact it just wrote -- and what the other three prove is the
#: relay: a valid request, a workspace that has nothing matching it, and Core's
#: own domain answer coming back. `import_start` names a staged source that is
#: well-formed and absent; the two job reads name a job this workspace has never
#: run.
#:
#: The order is this list's, not the manifest's: `memory_create` resolves its
#: declared source against a captured evidence artifact, so the capture has to
#: have happened. `test_the_authoring_calls_cover_every_tool_the_profile_adds`
#: is what keeps the set complete while the order is free.
CAPTURED_NOTE = "A note captured through MCP, carried in the call itself.\n"
CAPTURE_KEY = "mcp-authoring-capture-001"
CAPTURED_SOURCE = "mcp-authoring-note-1"


def authoring_calls(principal_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The five calls, bound to the dedicated principal the installation issued.

    A function rather than a constant because one of them names an actor, and the
    only actor an installed session may name is the principal its bearer resolves
    to -- which the service mints at `mcp.configure` time and nothing here can
    know in advance.
    """
    return [
        (
            "evidence_capture",
            {
                "input": {
                    "source_native_id": CAPTURED_SOURCE,
                    "media_type": "text/markdown",
                    "text": CAPTURED_NOTE,
                },
                "idempotency_key": CAPTURE_KEY,
            },
        ),
        (
            "memory_create",
            {
                "input": {
                    "record_type": "memory.fact",
                    "domain_scope": "product.core",
                    "content": {"fact": "a fact proposed through MCP"},
                    "evidence_disposition": "available",
                    "sources": [
                        {"kind": "direct_submission", "source_id": CAPTURED_SOURCE}
                    ],
                    "assertion": {
                        "actor_id": principal_id,
                        "actor_kind": "agent",
                        "actor_role": "author",
                        # Fixed and firmly in the past: the runtime refuses a claim
                        # asserted after the instant it settles at, and "today at
                        # midnight UTC" is a date that is briefly in the future.
                        "asserted_at": "2026-01-01T00:00:00Z",
                        "evidence": [
                            {
                                "source": {
                                    "kind": "direct_submission",
                                    "source_id": CAPTURED_SOURCE,
                                }
                            }
                        ],
                    },
                },
                "idempotency_key": "mcp-authoring-memory-001",
            },
        ),
        (
            "import_start",
            {
                "input": {
                    "source": {
                        "staged_source_ref": "stg-0001",
                        "source_kind": "archive",
                        "content_checksum": "sha256:" + "a" * 64,
                        "content_length_bytes": 1024,
                        "media_type": "application/zip",
                    }
                },
                "idempotency_key": "mcp-authoring-import-001",
            },
        ),
        ("job_get", {"job_id": "job-not-in-this-workspace"}),
        ("job_events", {"job_id": "job-not-in-this-workspace"}),
    ]


def service_error(called: dict[str, Any]) -> dict[str, Any]:
    """The service's own error document out of one relayed refusal.

    Refuses anything that is not one: an MCP-side refusal -- a name that does not
    resolve, a purpose that is not allowed, a wrapper that is the wrong shape --
    never reaches the service and carries no error envelope, so reading one here
    is what distinguishes "Core decided" from "this adapter decided".
    """
    assert called["is_error"] is True, called
    assert called["structured_content"] is None
    message = called["content"][0]["text"]
    assert "was refused by the service" in message, message
    relayed: dict[str, Any] = json.loads(
        message.split("was refused by the service: ", 1)[1]
    )
    return relayed["error"]


async def _authoring_probe(config: Path, principal_id: str) -> dict[str, Any]:
    """One admitted stdio session: the listing, the authoring calls, the replay."""
    wanted = authoring_calls(principal_id)
    async with (
        stdio_client(parameters(config, "--authoring")) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        calls = {
            name: (await session.call_tool(name, arguments)).model_dump(mode="json")
            for name, arguments in wanted
        }
        capture = dict(wanted[0][1])
        replay = await session.call_tool("evidence_capture", capture)
        found = await session.call_tool("evidence_search", {"query": CAPTURED_SOURCE})
        return {
            "tools": [tool.model_dump(mode="json") for tool in listed.tools],
            "calls": calls,
            "replay": replay.model_dump(mode="json"),
            "found": found.model_dump(mode="json"),
        }


def test_the_ceiling_alone_leaves_the_server_restricted_over_the_wire(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """`mutation_enabled: true` in the trusted file, and still the restricted ten.

    This is the upgrade rule and the security property together: the public
    configuration is a ceiling, not a switch, and the probe here is started the
    way production starts one -- no admission injected. A model sees the same ten
    tools it saw before, and `memory_create` is not merely absent from the
    listing but unresolvable at the call.
    """
    config = live_configuration(
        tmp_path, live_service, purposes=AUTHORING_PURPOSES, mutation_enabled=True
    )
    observed = session(config)

    assert observed["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    assert [tool["name"] for tool in observed["tools"]] == [
        entry.tool_name for entry in EXPOSURE_MANIFEST
    ]
    for name in ARGUMENTS:
        assert_call_outcome(observed, name)
    refusal = observed["refusals"]["memory_create"]
    assert refusal["is_error"] is True
    assert "is not a tool this server exposes" in refusal["content"][0]["text"]


def test_an_admitted_authoring_session_lists_fifteen_and_calls_every_new_tool(
    tmp_path: Path,
) -> None:
    """The whole authoring surface, over real pipes, against a real service.

    A service of its own rather than the module's, and one configured for the
    wider profile: one of these calls writes, and the shared workspace is the
    thing every other test in this file asserts exact counts against. The
    installation records authoring intent for the dedicated principal it issues
    here -- `serving(profile="authoring")` is `mcp.configure` with that intent,
    which is the separate explicit act R004 section 9.3 requires -- and the
    document below states the matching `mutation_enabled: true` ceiling.

    What each call proves, in one session:

    * the listing is the eleven, in manifest order, and the three mutations
      advertise the closed wrapper with the read hints inverted;
    * `evidence_capture` writes -- the content travels in the call, with no path,
      URL or credential anywhere in it -- and the artifact is then findable
      through `evidence_search`, which is the same synchronous guarantee the
      runtime lane proves from the inside;
    * replaying that call with the same key and the same input answers from the
      settled outcome: the same evidence id, not a second artifact;
    * `memory_create` writes too, and cites the artifact the call before it
      captured -- so the wider profile's two writing tools compose into the thing
      an agent would actually do, rather than each being proved alone;
    * `import_start`, `job_get` and `job_events` carry valid canonical requests
      this workspace has no answer for, and come back with *Core's* typed domain
      refusal rather than this adapter's. That is what shows the wrapper was
      unwrapped, the envelope built, and the decision left where it belongs:
      nothing matching that staged source, and no such job. The adapter's own
      contract check is not what produced either -- it is exercised against
      invalid input in `test_mcp_server_authority`, where a transport that
      refuses to be used proves such a call never leaves this process.
    """
    with fixture.serving(profile="authoring") as service:
        assert service.profile == "authoring"
        principal = service.principal_id
        config = live_configuration(
            tmp_path, service, purposes=AUTHORING_PURPOSES, mutation_enabled=True
        )
        observed = anyio.run(lambda: _authoring_probe(config, principal))

    assert [tool["name"] for tool in observed["tools"]] == [
        entry.tool_name for entry in exposure_manifest("authoring")
    ]
    assert observed["tools"] == [
        tool.model_dump(mode="json") for tool in tools("authoring")
    ]
    for name in ("memory_create", "evidence_capture", "import_start"):
        advertised = next(tool for tool in observed["tools"] if tool["name"] == name)
        assert set(advertised["input_schema"]["properties"]) == {
            "input",
            "idempotency_key",
        }
        assert advertised["input_schema"]["additionalProperties"] is False
        assert advertised["annotations"]["read_only_hint"] is False
        assert advertised["annotations"]["destructive_hint"] is False
        assert advertised["annotations"]["idempotent_hint"] is False

    captured = observed["calls"]["evidence_capture"]
    assert captured["is_error"] is False, captured
    written = captured["structured_content"]
    assert written["capture_disposition"] == "created"
    assert written["media_type"] == "text/markdown"
    assert written["content_length_bytes"] == len(CAPTURED_NOTE.encode("utf-8"))
    assert written["source"] == {
        "kind": "direct_submission",
        "source_id": CAPTURED_SOURCE,
    }
    Draft202012Validator(
        advertised_for(observed, "evidence_capture")["output_schema"]
    ).validate(written)

    replayed = observed["replay"]
    assert replayed["is_error"] is False, replayed
    assert replayed["structured_content"] == written, "a same-key replay wrote again"

    (found,) = observed["found"]["structured_content"]["evidence"]
    assert found["evidence_id"] == written["evidence_id"]
    assert found["source"]["kind"] == "direct_submission"

    proposed = observed["calls"]["memory_create"]
    assert proposed["is_error"] is False, proposed
    record = proposed["structured_content"]["record"]
    assert record["record_type"] == "memory.fact"
    assert record["content"] == {"fact": "a fact proposed through MCP"}
    # The actor the write was recorded under is the dedicated principal this
    # installation issued, not a name this module chose: the service refuses a
    # claim it did not grant, so a write that landed is a write made as that
    # principal.
    assert record["provenance"]["assertion"]["actor_id"] == principal
    assert record["provenance"]["sources"] == [
        {"kind": "direct_submission", "source_id": CAPTURED_SOURCE}
    ]
    Draft202012Validator(
        advertised_for(observed, "memory_create")["output_schema"]
    ).validate(proposed["structured_content"])

    assert service_error(observed["calls"]["job_get"])["code"] == "not_found"
    assert service_error(observed["calls"]["job_events"])["code"] == "not_found"
    assert service_error(observed["calls"]["import_start"])["code"] == (
        "dependency_unavailable"
    )


RECOVERY_SOURCE = "mcp-post-dispatch-recovery-note"
RECOVERY_KEY = "mcp-post-dispatch-recovery-001"
RECOVERY_TOKEN = "postdispatchrecoverytoken"
RECOVERY_TEXT = (
    f"A response-loss fixture whose unique lexical witness is {RECOVERY_TOKEN}.\n"
)


def recovery_capture() -> dict[str, Any]:
    return {
        "input": {
            "source_native_id": RECOVERY_SOURCE,
            "media_type": "text/markdown",
            "text": RECOVERY_TEXT,
        },
        "idempotency_key": RECOVERY_KEY,
    }


async def _drop_committed_capture_response(
    config: Path, service_pid: int
) -> dict[str, Any]:
    """Call once through a transport that discards Core's decoded reply."""
    async with (
        stdio_client(
            parameters(config, "--drop-after-reply-pid", str(service_pid))
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        assert "evidence_capture" in {tool.name for tool in listed.tools}
        result = await session.call_tool("evidence_capture", recovery_capture())
        return result.model_dump(mode="json")


async def _replay_capture_in_production_session(config: Path) -> dict[str, Any]:
    """Start the production entry point, replay, then observe lexical readiness."""
    async with (
        stdio_client(production_parameters(config)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        replay = await session.call_tool("evidence_capture", recovery_capture())
        found = await session.call_tool("evidence_search", {"query": RECOVERY_TOKEN})
        return {
            "server_name": initialized.server_info.name,
            "tools": [tool.name for tool in listed.tools],
            "replay": replay.model_dump(mode="json"),
            "found": found.model_dump(mode="json"),
        }


def _read_one(
    database: Path, statement: str, values: tuple[Any, ...]
) -> tuple[Any, ...]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(statement, values).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, rows
    return tuple(rows[0])


def test_a_lost_capture_response_replays_after_a_real_service_restart(
    tmp_path: Path,
) -> None:
    """CQ-T10 / F-2 / F-6 / F-8: one effect across a hard restart.

    The first MCP child uses the real installed authority and sends the real
    capture. Its test transport waits for Core's complete decoded response -- so
    the transaction and lexical barrier have finished -- then SIGKILLs that Core
    process and raises instead of handing the reply to MCP. The host therefore
    observes an ambiguous failure, not success.

    After the old process is reaped, the database's canonical stored outcome is
    read as the evidence of what committed. The installed managed-start command
    starts a new Core process, then a *new production MCP process* takes the
    ordinary managed-local attach path, obtains a fresh authenticated session,
    and replays the same caller key and input. The replay must equal those stored
    canonical bytes, be searchable, and leave exactly one artifact, blob, claim
    and outcome.
    """
    with fixture.serving(
        profile="authoring", seed=False, managed_endpoint=True
    ) as service:
        assert service.process.pid > 0
        first_descriptor = service.descriptor()
        config = live_configuration(
            tmp_path,
            service,
            purposes=AUTHORING_PURPOSES,
            mutation_enabled=True,
        )
        installation = InstallationServiceConfig(
            installation_state=service.installation_state,
            workspace_id=service.workspace_id,
        )
        replacement_may_be_running = False
        try:
            lost = anyio.run(
                lambda: _drop_committed_capture_response(config, service.process.pid)
            )
            assert lost["is_error"] is True
            assert lost["structured_content"] is None
            assert "response was lost" in lost["content"][0]["text"]

            first_lease = service.stop_and_read_lease()
            (stored_outcome,) = _read_one(
                service.database,
                "SELECT o.outcome_json "
                "FROM omnivia_idempotency_outcomes AS o "
                "JOIN omnivia_idempotency_claims AS c "
                "ON c.claim_id = o.claim_id AND c.workspace_id = o.workspace_id "
                "WHERE c.workspace_id = ? AND c.principal_id = ? "
                "AND c.operation = 'evidence.capture' AND c.idempotency_key = ?",
                (service.workspace_id, service.principal_id, RECOVERY_KEY),
            )
            assert isinstance(stored_outcome, str)

            executable = Path(sys.executable).parent / "omnivia-core-service"
            restarted = subprocess.run(
                [
                    str(executable),
                    "--managed-start",
                    "--workspace",
                    str(
                        service.installation_state.parent
                        / "workspaces"
                        / service.workspace_id
                    ),
                    "--installation-state",
                    str(service.installation_state),
                    "--endpoint",
                    first_descriptor.endpoint_uri,
                    "--expected-manifest-digest",
                    "sha256:"
                    + hashlib.sha256(
                        (
                            service.installation_state.parent
                            / "workspaces"
                            / service.workspace_id
                            / "workspace.json"
                        ).read_bytes()
                    ).hexdigest(),
                    "--managed-start-log",
                    str(
                        service.installation_state.parent
                        / "run"
                        / "workspaces"
                        / service.workspace_id
                        / "service.log"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            replacement_may_be_running = restarted.returncode == 0
            assert restarted.returncode == 0, restarted.stderr
            restart_result = json.loads(restarted.stdout)
            assert restart_result["status"] == "started", restart_result

            try:
                observed = anyio.run(
                    lambda: _replay_capture_in_production_session(config)
                )
            except BaseException as failure:
                managed_log = service.installation_state.parent / "run" / "service.log"
                said = (
                    managed_log.read_text(encoding="utf-8", errors="replace")
                    if managed_log.is_file()
                    else "<no managed-start service log>"
                )
                raise AssertionError(
                    f"the post-crash managed start failed; its service wrote {said!r}"
                ) from failure
            replacement_may_be_running = True
            replacement = service.descriptor()
            assert replacement.service_instance_id != (
                first_descriptor.service_instance_id
            )
            assert replacement.process is not None
            assert first_descriptor.process is not None
            assert replacement.process.pid != first_descriptor.process.pid
            assert replacement.fencing_generation > first_lease.fencing_generation

            assert observed["server_name"] == "omnivia-core"
            assert observed["tools"] == [
                entry.tool_name for entry in exposure_manifest("authoring")
            ]
            replay = observed["replay"]
            assert replay["is_error"] is False, replay
            replayed_result = replay["structured_content"]
            assert to_canonical_json(replayed_result) == stored_outcome

            found = observed["found"]
            assert found["is_error"] is False, found
            evidence = found["structured_content"]["evidence"]
            assert [entry["evidence_id"] for entry in evidence] == [
                replayed_result["evidence_id"]
            ]

            stopped = stop_managed_local(installation, deadline=Deadline.after(30.0))
            replacement_may_be_running = False
            assert stopped.status == "stopped"

            assert _read_one(
                service.database,
                "SELECT COUNT(*), MIN(content_checksum) "
                "FROM omnivia_evidence_artifacts "
                "WHERE workspace_id = ? AND source_kind = 'direct_submission' "
                "AND source_native_id = ?",
                (service.workspace_id, RECOVERY_SOURCE),
            ) == (1, replayed_result["content_checksum"])
            assert _read_one(
                service.database,
                "SELECT COUNT(*) FROM omnivia_blob_objects WHERE content_digest = ?",
                (replayed_result["content_checksum"],),
            ) == (1,)
            assert _read_one(
                service.database,
                "SELECT COUNT(*) FROM omnivia_idempotency_claims "
                "WHERE workspace_id = ? AND principal_id = ? "
                "AND operation = 'evidence.capture' AND idempotency_key = ?",
                (service.workspace_id, service.principal_id, RECOVERY_KEY),
            ) == (1,)
            assert _read_one(
                service.database,
                "SELECT COUNT(*) FROM omnivia_idempotency_outcomes AS o "
                "JOIN omnivia_idempotency_claims AS c "
                "ON c.claim_id = o.claim_id AND c.workspace_id = o.workspace_id "
                "WHERE c.workspace_id = ? AND c.principal_id = ? "
                "AND c.operation = 'evidence.capture' AND c.idempotency_key = ?",
                (service.workspace_id, service.principal_id, RECOVERY_KEY),
            ) == (1,)
        finally:
            if replacement_may_be_running:
                stop_managed_local(installation, deadline=Deadline.after(30.0))


def test_the_authoring_calls_cover_every_tool_the_profile_adds() -> None:
    """The coverage check for the wider profile, matching the one the six have.

    By set rather than by order, because :func:`authoring_calls` is ordered by
    what the calls depend on -- the capture before the memory that cites it --
    and not by the manifest. A twelfth tool still cannot land without an
    end-to-end call.
    """
    assert {name for name, _ in authoring_calls("mcp-coverage-principal")} == {
        entry.tool_name for entry in exposure_manifest("authoring")
    } - {entry.tool_name for entry in EXPOSURE_MANIFEST}


def advertised_for(observed: dict[str, Any], tool_name: str) -> dict[str, Any]:
    for tool in observed["tools"]:
        if tool["name"] == tool_name:
            return tool
    raise AssertionError(f"{tool_name} was not advertised")


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


def test_the_ordinary_probe_uses_no_double_and_is_told_only_a_config_path(
    observed: dict[str, Any], live_service: fixture.GovernedService
) -> None:
    """Every answer above came through production code, not through a double.

    The only class in the probe is the explicit post-dispatch fault injector used
    by the restart test, and the ordinary path that produced ``observed`` never
    enables it. The probe takes no endpoint and no workspace argument: it is
    handed one configuration path and `server.connect` derives the rest, which
    is the whole V06-6 change. The session above also brought back
    workspace-specific values that no double in this tree holds.
    """
    source = PROBE.read_text(encoding="utf-8")
    classes = [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
    ]
    assert classes == ["_DropAfterReplyTransport"]
    assert "--endpoint" not in source
    assert "--workspace-id" not in source
    assert source.count("server.connect(") == 1
    assert "read_configuration(" in source

    assert structured(observed, "workspace_inspect")["workspace"]["workspace_id"] == (
        live_service.workspace_id
    )


# --- stdout is protocol-only --------------------------------------------------


def test_the_stdio_stream_carries_only_protocol_even_under_contamination(
    live_service: fixture.GovernedService, live_config: Path
) -> None:
    """R004-07: stdout is protocol-only, proved against a server trying to break it.

    The probe writes to `sys.stdout` twice from inside a live handler, on every
    call -- six of them now. If any reached the wire the session below would fail
    to parse a frame; instead every call completes and the strings are nowhere in
    what the client received.

    A failing call quotes the whole answer and the service's own state, because
    the two ways this can fail are not distinguishable from the tool name. The
    claim here is about the *stream*, so an erroring call is only evidence
    against it if the call reached a service that was answering at all -- and
    this is the last test in the module to use the shared service, which is
    exactly where "the service stopped answering everyone" arrives disguised as
    "one tool returned an error".
    """
    contaminated = session(live_config, "--contaminate")
    assert contaminated["tools"] == [tool.model_dump(mode="json") for tool in tools()]
    for name in ARGUMENTS:
        assert_call_outcome(contaminated, name)
    serialised = json.dumps(contaminated)
    assert "CONTAMINATION-FROM-A-HANDLER" not in serialised
    assert "CONTAMINATION-VIA-PRINT" not in serialised


def test_every_byte_the_server_writes_to_stdout_is_valid_protocol(
    live_service: fixture.GovernedService, live_config: Path
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
    try:
        completed = subprocess.run(
            [sys.executable, str(PROBE), "--config", str(live_config), "--contaminate"],
            input=request,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as hang:
        # Deliberately below `server.connect`'s own
        # `MANAGED_START_TIMEOUT_SECONDS`, so a probe that cannot reach the
        # service is killed here rather than allowed to spend its whole startup
        # budget three times over: this is a hang guard, and the run it guards
        # has a job timeout of its own. That makes the *expiry* uninformative on
        # its own -- it fires before the probe can refuse in its own words --
        # which is why the service's state is attached rather than the guard
        # relaxed. "Still running, still advertising ready, answering nobody" and
        # "stopped, and here is the sentence it stopped with" are different
        # causes, and a bare `TimeoutExpired` names neither.
        raise AssertionError(
            f"the probe never finished; it wrote {hang.stderr!r}; "
            f"{live_service.diagnosis()}"
        ) from hang
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, (
        f"the server wrote nothing; stderr was {completed.stderr!r}; "
        f"{live_service.diagnosis()}"
    )
    for line in lines:
        message = json.loads(line)  # a non-protocol line fails here
        assert message["jsonrpc"] == "2.0", line
    assert "CONTAMINATION" not in completed.stdout
    # Not merely absent from stdout: both writes are accounted for on stderr, so
    # this cannot pass by the handler having quietly stopped running.
    assert "CONTAMINATION-FROM-A-HANDLER" in completed.stderr
    assert "CONTAMINATION-VIA-PRINT" in completed.stderr


# --- refusing rather than creating --------------------------------------------


def _main(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """`omnivia-core-mcp`'s console entry point, in a subprocess."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnivia_core_mcp.server import main; raise SystemExit(main())",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        **kwargs,
    )


def test_the_server_refuses_a_missing_workspace_and_creates_nothing(
    live_service: fixture.GovernedService, tmp_path: Path
) -> None:
    """R004-07 and R004-10, end to end through the console entry point.

    **The precondition this claim needs is a real, configured installation**, and
    it is the live one rather than an empty directory: the credential check comes
    first now, so a root with no protected store is refused before a managed
    start is ever considered -- which is a different rule, proved in
    `test_a_root_nobody_configured_refuses_before_anything_is_started`. Here the
    installation is real, the dedicated principal's bearer resolves, and the
    workspace named is simply one this installation does not have. Nothing is
    published for it, so `--managed-start` is invoked once and the launcher's own
    refusal comes back: run `omnivia init`. `main()` writes it to stderr and not
    one byte to stdout, which is what makes the failure protocol-safe.

    Nothing is created, and that is checked where it could now happen: under the
    installation state root the refused start was pointed at, which must hold
    exactly what it held before.
    """
    config = configuration_file(
        tmp_path,
        installation_state=live_service.installation_state,
        workspace_id="ws-nothing-here",
        credential_reference=live_service.credential_reference,
        principal_id=live_service.principal_id,
    )
    before = set(live_service.installation_state.rglob("*"))
    completed = _main("--config", str(config))

    assert completed.returncode == 1
    assert completed.stdout == "", "a failed start must write no protocol"
    assert "omnivia init" in completed.stderr
    assert "creates none" in completed.stderr
    assert set(live_service.installation_state.rglob("*")) == before, (
        "a refused start created state"
    )


def test_the_entry_point_requires_an_explicit_absolute_configuration_path() -> None:
    """No `--home`, no default, no ambient configuration, and no relative path.

    Three outcomes and they stay distinct. A missing `--config` is an argparse
    usage error and exits 2; `--home` no longer exists and is the same; a
    relative path is a *trusted configuration* refusal and exits 1 with a
    fixed sentence. None of them writes protocol.
    """
    for usage in ([], ["--home", "/tmp/omnivia"]):
        completed = _main(*usage)
        assert completed.returncode == 2, completed.stderr
        assert completed.stdout == ""

    relative = _main("--config", "omnivia-mcp.json")
    assert relative.returncode == 1, relative.stderr
    assert relative.stdout == ""
    assert "trusted owner-only file" in relative.stderr
