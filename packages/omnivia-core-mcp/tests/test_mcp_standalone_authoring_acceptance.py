"""R004 section 13.B: the whole standalone authoring journey, nothing pre-arranged.

One test, one journey, and every step of it the production one. The other modules
in this package start from a workspace `_mcp_v06_3_fixture` seeded and an MCP
principal that fixture provisioned through the client; this one starts from the
state section 13.B requires and forbids improving on -- an empty workspace on an
installation where no host has been configured -- and the first thing that
happens to it is the installed command a human would run.

**The setup is the real CLI, in a subprocess, and its snippet is the only thing
read back.** `omnivia mcp configure --host claude-code --workspace <minted>
--profile authoring` mints the dedicated principal, files its bearer in this
installation's protected store and writes the protected configuration, exactly as
it does on a laptop. What this module parses is the redacted host snippet that
command prints -- a command and a `--config` path and nothing else -- which is
how the protected document is located without this test knowing where such a
document lives. The only thing read out of that document is the non-secret
`principal_id`, because one call below has to name the actor the service will
accept; no credential material is read, asserted on or carried.

**The client is the official SDK and the handshake is pinned.** `stdio_client`
spawns `python -m omnivia_core_mcp.server --config <path>` -- the module a host
launches -- and the `initialize` below is an explicit 2025-06-18
`InitializeRequest` followed by the `initialized` notification, so the protocol
version this journey is evidence about is stated rather than negotiated to
whatever the SDK currently prefers.

**Nothing here writes to the workspace except through the exposed tools.** There
is no runtime import, no SQL, and no stand-in server: the workspace is empty when
the session opens, so every identifier asserted below is one the journey itself
created, and a tool that answered from somewhere else would have nothing to
answer with.

**The captured note is hostile-looking and must stay inert.** It carries
multi-script Unicode, a URL, an absolute filesystem path, and a JSON object whose
keys are the trusted configuration's own (`principal_id`, `workspace_id`,
`mutation_enabled`). None of it is configuration: it is stored byte-exact -- the
checksum over its UTF-8 bytes and the byte count both come back -- the session
keeps calling as the principal the installation minted rather than the `root` the
body names, every answer is scoped to the minted workspace rather than the
`ws-elsewhere` the body names, and the advertised surface after the writes is the
same eleven it was before.

**No assertion message carries anything it could leak.** Every message below is a
fixed sentence: no paths, no bearers, no references, no service envelopes and no
answer bodies, because a hosted failure log is read by whoever finds it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import _mcp_v06_3_fixture as fixture
import anyio
import mcp_types as types
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from omnivia_core_mcp.manifest import exposure_manifest

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

#: The MCP revision this journey is evidence about, stated in the request rather
#: than left to the SDK's current preference.
PROTOCOL_VERSION = "2025-06-18"

#: The server identity a host sees, and the module it launches.
SERVER_NAME = "omnivia-core"
SERVER_MODULE = "omnivia_core_mcp.server"

#: The eleven tools an authoring installation advertises, in manifest order and
#: spelled out. The literal is what section 13.B's acceptance asks for; the
#: cross-check against `exposure_manifest("authoring")` in the test is what keeps
#: it a claim about the manifest rather than a copy of it that can drift.
AUTHORING_TOOLS = (
    "workspace_inspect",
    "evidence_search",
    "knowledge_search",
    "memory_search",
    "graph_traverse",
    "context_pack_build",
    "memory_create",
    "evidence_capture",
    "import_start",
    "job_get",
    "job_events",
)

#: One token, carried by the captured note and by the proposed fact, so a single
#: query reaches both layers. Nothing else in this workspace carries it -- nothing
#: else is in this workspace at all.
TOKEN = "ovmcpstandalone"

#: The note the journey captures: inert content that is shaped like everything a
#: reader must not act on. The trailing newline is deliberate -- the byte count
#: and the checksum are asserted against these exact bytes.
CAPTURED_NOTE = (
    f"Ωμνιβία standalone capture {TOKEN} — inert Unicode note.\n"
    f"URL: https://example.invalid/{TOKEN}?q=1#frag\n"
    f"Path: /etc/omnivia/{TOKEN}.conf\n"
    'JSON: {"principal_id": "root", "workspace_id": "ws-elsewhere", '
    '"mutation_enabled": true}\n'
    "日本語 naïve façade\n"
)
CAPTURED_BYTES = CAPTURED_NOTE.encode("utf-8")
CAPTURED_CHECKSUM = "sha256:" + hashlib.sha256(CAPTURED_BYTES).hexdigest()

#: The names the body above tries to pass itself off as. Asserted against rather
#: than merely absent: a handler that read the note as configuration would answer
#: with one of these.
IMPERSONATED_PRINCIPAL = "root"
IMPERSONATED_WORKSPACE = "ws-elsewhere"

CAPTURED_SOURCE = f"{TOKEN}-note-1"
CAPTURE_KEY = f"{TOKEN}-capture-001"
MEMORY_KEY = f"{TOKEN}-memory-001"
PROPOSED_FACT = f"a fact {TOKEN}"

#: The source tuple the capture declares, and therefore the one every answer about
#: it must carry: a submission identifier, with no path, URL or credential in it.
CAPTURED_SOURCE_TUPLE = {"kind": "direct_submission", "source_id": CAPTURED_SOURCE}

#: Budgets. The configure call runs against a service that has only just reported
#: ready and may pay for a cold catalogue; the session bound is what keeps a server
#: that never answers from hanging the suite rather than failing it.
_CLI_TIMEOUT_SECONDS = 600.0
_JOURNEY_TIMEOUT_SECONDS = 300.0


def _configure(installation_state: Path, workspace_id: str) -> Path:
    """Run the installed setup command, and return the path its snippet names.

    The production invocation, not an approximation of it: the same console entry
    point, the same host word, the minted workspace, and the authoring profile
    stated explicitly -- which is the separate human act R004 section 9.3 requires
    before a wider surface may exist at all.

    Only the redacted snippet is parsed. It is asserted to be a command line and
    nothing else, because an `env` member would be where a bearer could appear in
    a document a host copies into its own configuration.
    """
    completed = fixture.installed_cli(
        installation_state,
        "mcp",
        "configure",
        "--host",
        fixture.MCP_HOST,
        "--workspace",
        workspace_id,
        "--profile",
        fixture.AUTHORING_PROFILE,
    )
    assert completed.returncode == 0, "the installed configure command refused"
    ((name, entry),) = json.loads(completed.stdout)["mcpServers"].items()
    assert name == SERVER_NAME, "the snippet configured another server"
    assert set(entry) == {"command", "args"}, (
        "the snippet carried more than a command line"
    )
    flag, path = entry["args"]
    assert flag == "--config", "the snippet did not name a configuration path"
    return Path(path)


def _health(installation_state: Path, workspace_id: str) -> dict[str, Any]:
    """`omnivia service health --json`, through the ordinary client path.

    The probe a human runs, and the reason it is a probe rather than a look at the
    published descriptor: a descriptor is a file a stopped service can leave
    behind saying `ready`, while this dials the endpoint through the shared
    client and makes the service itself answer. Paired with the liveness check at
    its call site, which is what stops a service the probe started for itself
    from standing in for the one the journey used.
    """
    completed = fixture.installed_cli(
        installation_state,
        "--workspace-id",
        workspace_id,
        "service",
        "health",
        "--json",
    )
    assert completed.returncode == 0, "the health probe could not reach the service"
    probed: dict[str, Any] = json.loads(completed.stdout)
    return probed


def _memory_input(principal_id: str, fact: str) -> dict[str, Any]:
    """One `memory.create` input citing the artifact the journey captured.

    The actor is the dedicated principal the installation minted: the service
    refuses a claim it did not grant, so nothing this module could choose would be
    accepted. The evidence is the capture's own source tuple, which is what makes
    the proposal evidence-backed rather than merely well-formed.
    """
    return {
        "record_type": "memory.fact",
        "domain_scope": "product.core",
        "content": {"fact": fact},
        "evidence_disposition": "available",
        "sources": [dict(CAPTURED_SOURCE_TUPLE)],
        "assertion": {
            "actor_id": principal_id,
            "actor_kind": "agent",
            "actor_role": "author",
            # Fixed and firmly in the past: the runtime refuses a claim asserted
            # after the instant it settles at.
            "asserted_at": "2026-01-01T00:00:00Z",
            "evidence": [{"source": dict(CAPTURED_SOURCE_TUPLE)}],
        },
    }


async def _journey(config: Path, principal_id: str) -> dict[str, Any]:
    """The whole session: handshake, listing, the writes, the reads, the replays.

    One session for all of it, because that is the unit the journey is about -- a
    host opens one and an agent works inside it -- and because the replays only
    mean anything against the outcomes the same session settled.

    The child is given this interpreter's environment deliberately: the SDK
    sanitizes it when `env` is `None`, which in a worktree drops the `PYTHONPATH`
    that selects the source tree under test.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE, "--config", str(config)],
        env=dict(os.environ),
    )
    capture = {
        "input": {
            "source_native_id": CAPTURED_SOURCE,
            "media_type": "text/markdown",
            "text": CAPTURED_NOTE,
        },
        "idempotency_key": CAPTURE_KEY,
    }
    memory = {
        "input": _memory_input(principal_id, PROPOSED_FACT),
        "idempotency_key": MEMORY_KEY,
    }
    with anyio.fail_after(_JOURNEY_TIMEOUT_SECONDS):
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.send_request(
                types.InitializeRequest(
                    params=types.InitializeRequestParams(
                        protocol_version=PROTOCOL_VERSION,
                        capabilities=types.ClientCapabilities(),
                        client_info=types.Implementation(
                            name="omnivia-core-acceptance", version="0"
                        ),
                    )
                ),
                types.InitializeResult,
            )
            session.adopt(initialized)
            await session.send_notification(types.InitializedNotification())
            listed = await session.list_tools()

            async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                called = await session.call_tool(tool, arguments)
                return called.model_dump(mode="json")

            observed: dict[str, Any] = {
                "protocol_version": initialized.protocol_version,
                "server_name": initialized.server_info.name,
                "tools": [tool.name for tool in listed.tools],
                # Before the first write, and only meaningful here: section 13.B
                # forbids pre-seeded application data, and this is that rule read
                # back through the exposed tools rather than trusted to a flag on
                # the fixture. Every identifier asserted later is therefore one
                # this session created.
                "empty_evidence": await call("evidence_search", {"query": TOKEN}),
                "empty_memory": await call("memory_search", {"query": TOKEN}),
                "empty_knowledge": await call("knowledge_search", {"query": TOKEN}),
                "capture": await call("evidence_capture", capture),
                "evidence": await call("evidence_search", {"query": TOKEN}),
                "memory": await call("memory_create", memory),
                "default_memory": await call("memory_search", {"query": TOKEN}),
                "default_knowledge": await call("knowledge_search", {"query": TOKEN}),
                "candidates": await call(
                    "memory_search", {"query": TOKEN, "view": "candidates"}
                ),
                "capture_replay": await call("evidence_capture", capture),
                "memory_replay": await call("memory_create", memory),
                # Read back *after* both replays, because "the replay answered
                # from the settled outcome" and "the replay wrote a second row
                # that happened to look the same" are indistinguishable from the
                # replay's own answer. These three are where a duplicate would
                # show up: a second artifact, a second candidate, or a proposal
                # that reached the default view on the way through.
                "evidence_after_replay": await call(
                    "evidence_search", {"query": TOKEN}
                ),
                "candidates_after_replay": await call(
                    "memory_search", {"query": TOKEN, "view": "candidates"}
                ),
                "default_memory_after_replay": await call(
                    "memory_search", {"query": TOKEN}
                ),
                "capture_conflict": await call(
                    "evidence_capture",
                    {
                        "input": {**capture["input"], "text": CAPTURED_NOTE + "more\n"},
                        "idempotency_key": CAPTURE_KEY,
                    },
                ),
                "memory_conflict": await call(
                    "memory_create",
                    {
                        "input": _memory_input(principal_id, f"a different {TOKEN}"),
                        "idempotency_key": MEMORY_KEY,
                    },
                ),
            }
            listed_again = await session.list_tools()
            observed["tools_again"] = [tool.name for tool in listed_again.tools]
            return observed


def _answer(observed: dict[str, Any], key: str) -> dict[str, Any]:
    """One successful call's structured content, refusing anything else.

    A test that read `content[0]` straight would pass on a refusal whose text
    happened to mention the right identifier.
    """
    called = observed[key]
    assert called["is_error"] is False, f"{key} did not succeed"
    answer = called["structured_content"]
    assert isinstance(answer, dict), f"{key} carried no structured answer"
    return answer


def _refusal(observed: dict[str, Any], key: str) -> dict[str, Any]:
    """The service's own error document out of one relayed refusal.

    Refuses anything that is not one: an MCP-side refusal never reaches the
    service and carries no error envelope, so reading one here is what
    distinguishes "Core decided" from "this adapter decided". The envelope itself
    is never surfaced -- only the two fields asserted on.
    """
    called = observed[key]
    assert called["is_error"] is True, f"{key} was not refused"
    assert called["structured_content"] is None, f"{key} carried a structured answer"
    message = called["content"][0]["text"]
    assert "was refused by the service" in message, f"{key} was not the service's own"
    relayed = json.loads(message.split("was refused by the service: ", 1)[1])
    error: dict[str, Any] = relayed["error"]
    return error


def test_the_standalone_authoring_journey_runs_on_an_empty_workspace() -> None:
    """Section 13.B, executed: configure, then author, then read it back.

    The workspace is empty and no host is configured when this begins, so there
    is no step below that some earlier arrangement already took. In order:

    * the installed command configures `claude-code` for the authoring profile on
      the minted workspace, and prints a snippet that is a command line and a
      configuration path;
    * a real SDK client completes a pinned 2025-06-18 handshake against the module
      a host launches, and is advertised exactly the eleven the authoring manifest
      declares;
    * the three searches it makes before writing anything answer with nothing, so
      the emptiness the rest of this rests on is read rather than assumed;
    * `evidence_capture` writes the note -- content in the call, no path, no URL,
      no credential -- and it comes back with the checksum over its own UTF-8
      bytes, its byte count and the source tuple it declared;
    * `evidence_search` finds that artifact, so the write is readable in the same
      session rather than eventually;
    * `memory_create` proposes a fact citing that artifact, recorded as the
      principal the installation minted;
    * the default governed views answer with nothing, because a proposal is not
      governed truth, and the candidate view answers with exactly it -- visibility
      is asked for, never inherited;
    * replaying both writes with the same key and the same input answers from the
      settled outcome, and the searches after those replays still find exactly one
      artifact and exactly one candidate, which is where a second row would show;
      replaying either key with changed input is refused as an idempotency
      conflict;
    * the hostile-looking body changed nothing: the actor is still the minted
      principal, every answer is scoped to the minted workspace, and the surface
      is still the same eleven;
    * and the service that answered all of it is the same process afterwards and
      still answers its own health probe, both checked after the session closes
      and before the fixture is allowed to tear it down.
    """
    with fixture.serving(seed=False, configure=False) as service:
        config = _configure(service.installation_state, service.workspace_id)
        # The one non-secret member a call below must name. Nothing else is read
        # out of the protected document, and no credential material is.
        principal_id = json.loads(config.read_text(encoding="utf-8"))["principal_id"]
        assert principal_id != IMPERSONATED_PRINCIPAL, "the minted principal is wrong"

        observed = anyio.run(lambda: _journey(config, principal_id))

        # After the client's streams are closed and before the fixture stops the
        # service: a session that ended cleanly must leave Core answering, and
        # only this window can tell that from a service that died mid-journey.
        assert service.process.poll() is None, "the service did not outlive the session"
        assert service.descriptor().ready is True, "the service stopped reporting ready"
        # And it still answers, rather than merely still existing: the ordinary
        # probe the CLI runs, over the endpoint the shared client dials.
        probed = _health(service.installation_state, service.workspace_id)

    # --- the service the journey attached to is still serving -----------------

    assert probed["probe"] == "service.health", "another probe answered"
    assert probed["status"] == "pass", "the service did not stay healthy"

    # --- the handshake and the advertised surface -----------------------------

    assert observed["protocol_version"] == PROTOCOL_VERSION, "another revision"
    assert observed["server_name"] == SERVER_NAME, "another server answered"
    assert observed["tools"] == list(AUTHORING_TOOLS), "the listing is not the eleven"
    assert AUTHORING_TOOLS == tuple(
        entry.tool_name for entry in exposure_manifest("authoring")
    ), "the expected eleven drifted from the manifest"

    # --- the workspace this began on held nothing -----------------------------

    assert _answer(observed, "empty_evidence")["evidence"] == [], "evidence was seeded"
    assert _answer(observed, "empty_memory")["records"] == [], "memory was seeded"
    assert _answer(observed, "empty_knowledge")["records"] == [], "knowledge was seeded"

    # --- the capture is stored byte-exact, and cited by its own source tuple ---

    captured = _answer(observed, "capture")
    assert captured["capture_disposition"] == "created", "the note was not captured"
    assert captured["media_type"] == "text/markdown", "the media type changed"
    assert captured["content_length_bytes"] == len(CAPTURED_BYTES), "the length changed"
    assert captured["content_checksum"] == CAPTURED_CHECKSUM, "the content changed"
    assert captured["source"] == CAPTURED_SOURCE_TUPLE, "the source tuple changed"

    (artifact,) = _answer(observed, "evidence")["evidence"]
    assert artifact["evidence_id"] == captured["evidence_id"], "another artifact"
    assert artifact["content_checksum"] == CAPTURED_CHECKSUM, "the stored bytes differ"
    assert artifact["source"] == CAPTURED_SOURCE_TUPLE, "the stored source differs"
    assert artifact["tombstoned"] is False, "the artifact is not live"
    assert [event["action"] for event in artifact["provenance_history"]] == [
        "captured"
    ], "the capture history is not one capture"

    # --- the proposal is evidence-backed, and visible only as a candidate ------

    record = _answer(observed, "memory")["record"]
    assert record["record_type"] == "memory.fact", "another record type"
    assert record["content"] == {"fact": PROPOSED_FACT}, "the proposed fact changed"
    assert record["authority_level"] == "proposed", "a proposal was not proposed"
    provenance = record["provenance"]
    assert provenance["sources"] == [CAPTURED_SOURCE_TUPLE], "the citation changed"
    assert provenance["assertion"]["evidence"] == [{"source": CAPTURED_SOURCE_TUPLE}], (
        "the asserted evidence changed"
    )
    assert provenance["identity"]["layer"] == "l1", "a proposal landed outside l1"
    assert provenance["identity"]["governance_state"] == "proposed", "it was governed"
    # The citation is the canonical source tuple and nothing else. The evidence
    # identifier is the one value this journey never sent, so a private
    # `evidence_id` shortcut into the record would appear here as the only place
    # it could have come from.
    assert captured["evidence_id"] not in json.dumps(record), (
        "the record carries an evidence identifier nothing cited"
    )

    assert _answer(observed, "default_memory")["records"] == [], (
        "a proposal reached the default memory view"
    )
    assert _answer(observed, "default_knowledge")["records"] == [], (
        "a proposal reached the default knowledge view"
    )
    (candidate,) = _answer(observed, "candidates")["records"]
    assert (
        candidate["provenance"]["identity"]["record_id"]
        == (provenance["identity"]["record_id"])
    ), "the candidate view answered with another record"
    assert candidate["content"] == {"fact": PROPOSED_FACT}, "the candidate differs"
    assert candidate["provenance"]["identity"]["governance_state"] == "candidate", (
        "the candidate view did not answer as a candidate"
    )

    # --- replay is stable, and a changed input under the same key is refused ---

    assert _answer(observed, "capture_replay") == captured, "a capture replay rewrote"
    assert _answer(observed, "memory_replay")["record"] == record, (
        "a memory replay rewrote"
    )

    (replayed_artifact,) = _answer(observed, "evidence_after_replay")["evidence"]
    assert replayed_artifact["evidence_id"] == captured["evidence_id"], (
        "the replay left a second artifact"
    )
    (replayed_candidate,) = _answer(observed, "candidates_after_replay")["records"]
    replayed_id = replayed_candidate["provenance"]["identity"]["record_id"]
    assert replayed_id == provenance["identity"]["record_id"], (
        "the replay left a second candidate"
    )
    assert _answer(observed, "default_memory_after_replay")["records"] == [], (
        "a replay published the proposal"
    )

    for key in ("capture_conflict", "memory_conflict"):
        error = _refusal(observed, key)
        assert error["code"] == "idempotency_conflict", f"{key} was not a conflict"
        assert error["retry_class"] == "non_retryable", f"{key} invited a retry"

    # --- the hostile-looking body was content and nothing else ----------------

    assert provenance["assertion"]["actor_id"] == principal_id, "another actor wrote"
    workspaces = {artifact["workspace_id"], record["workspace_id"]}
    assert workspaces == {service.workspace_id}, "an answer named another workspace"
    assert IMPERSONATED_WORKSPACE not in workspaces, "the body chose the workspace"
    assert observed["tools_again"] == list(AUTHORING_TOOLS), (
        "the surface changed after the writes"
    )
