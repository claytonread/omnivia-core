"""R004 section 13.F: an interrupted response, a real restart, and a second session.

Section 13.B's journey proves an empty workspace can be authored into and that a
same-key replay settles to the same answer *inside one session against one live
service*. The three recovery bullets this module answers are the ones that
outlived it, because none of them is a storage question:

* **F-2** -- terminate the service after commit but before the MCP response,
  restart, replay the same key, assert the same canonical result;
* **F-6** -- timeout or connection loss after possible dispatch; and
* **F-8** -- same-key recovery from a new MCP session.

**The interruption is staged in the pipe, not in the product.** Nothing in the
adapter may drop its own answer, so `_mcp_interrupted_relay` sits between the
client and an unmodified `python -m omnivia_core_mcp.server --config <path>`
child, forwards every byte, and withholds exactly one response: the one to the
`evidence_capture` call. There was an answer to withhold, so the call reached
Core and was dispatched; the client receives nothing at all and then a closed
stream. That is the ambiguous outcome, and it is arranged by an event -- the
reply arriving at the relay -- rather than by a sleep. That the call also
*committed* is not claimed here; it is proved further down, by a later session
finding the artifact before it has written anything.

**Then the service really stops and really starts again.** `restart()` sends
`SIGTERM`, waits for the process to be gone, and starts a new one on the same
workspace; the new process's published `fencing_generation` is the acquisition
after the one that stopped, which is how the test can say this was a restart and
not a reconnection. Only then does a *new* MCP session open, against the
production entry point, with its own pinned 2025-06-18 handshake.

**What the recovery has to prove is that the effect is already there.** The first
thing the second session does is search -- before it replays anything. Finding
the artifact then is the whole of F-6's point: the host was told nothing, so a
host that assumed nothing had happened would have been wrong about a workspace
that had already changed. The key it would have invented is then sent too, with
byte-identical input, so the cost of guessing is measured rather than argued: a
different canonical answer, and a second claim, outcome, audit event and
execution for one effect. The same key is what recovers it, and this adapter
never chooses a key or retries a mutation on its own initiative
(`test_mcp_server_authority.py` holds that half against a transport that invites
both).

**No credential, path, service envelope or captured content is read or carried.**
The relay records one thing -- the name of the tool whose answer it withheld --
and never reads a withheld message past its JSON-RPC id. Every answer the
recovering session receives, the refusal included, is searched for the note's own
bytes, for the trusted document's path, for the service endpoint and for the
opaque reference the installed setup filed this host's bearer under. And every
assertion message is a fixed sentence, because a hosted failure log is read by
whoever finds it.

**The journey's vocabulary is imported, not restated.** The note, its checksum,
its source tuple, the installed configure call and the answer/refusal readers all
come from `test_mcp_standalone_authoring_acceptance`, so this module is about
recovery and nothing else, and a change to what the 13.B journey captures cannot
leave this one asserting against a stale copy of it.
"""

from __future__ import annotations

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
from test_mcp_standalone_authoring_acceptance import (
    AUTHORING_TOOLS,
    CAPTURED_BYTES,
    CAPTURED_CHECKSUM,
    CAPTURED_NOTE,
    CAPTURED_SOURCE,
    CAPTURED_SOURCE_TUPLE,
    PROTOCOL_VERSION,
    SERVER_MODULE,
    SERVER_NAME,
    TOKEN,
    _answer,
    _configure,
    _memory_input,
    _refusal,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

#: The test-only relay, launched by path. It spawns the production server module
#: as its own child, so what a host launches is still what is under test.
RELAY = Path(__file__).with_name("_mcp_interrupted_relay.py")

#: The tool whose answer is withheld, spelled as the manifest advertises it.
INTERRUPTED_TOOL = "evidence_capture"

#: One key per mutation per test. Each test serves its own freshly created
#: workspace, so these need only be distinct from each other within a journey.
INTERRUPTED_KEY = f"{TOKEN}-interrupted-001"
#: The key a host would have to invent if it decided the interrupted call had not
#: happened. Sent with byte-identical input, so what it costs is attributable to
#: the key alone.
INVENTED_KEY = f"{TOKEN}-invented-001"
SESSION_A_CAPTURE_KEY = f"{TOKEN}-crosssession-capture-001"
SESSION_A_MEMORY_KEY = f"{TOKEN}-crosssession-memory-001"

PROPOSED_FACT = f"a cross-session fact {TOKEN}"

#: Two fragments of the captured note that no answer may contain. One is content
#: in a script nothing else here writes, the other is the absolute path the note
#: is shaped to look like a configuration for. `TOKEN` itself is deliberately not
#: on this list: it is the source identifier the journey submitted, so it is
#: *supposed* to come back.
NOTE_FRAGMENTS = ("日本語", "/etc/omnivia/")

#: The whole budget for one MCP session, dialling and handshake included. The
#: bound is what keeps a server that never answers from hanging the suite rather
#: than failing it.
_SESSION_TIMEOUT_SECONDS = 300.0

#: What one settled mutation looks like in the coordinator's ledger, before any
#: replay: one claim, one stored outcome, one M1 audit event, one run of the
#: domain code. `replayed` is counted separately because it is expected to grow.
ONE_SETTLEMENT = {"claims": 1, "outcomes": 1, "audit_events": 1, "executed": 1}


def _capture_call(text: str = CAPTURED_NOTE, *, key: str) -> dict[str, Any]:
    """One `evidence_capture` argument object: the wrapped input and its key."""
    return {
        "input": {
            "source_native_id": CAPTURED_SOURCE,
            "media_type": "text/markdown",
            "text": text,
        },
        "idempotency_key": key,
    }


async def _open(session: ClientSession) -> types.InitializeResult:
    """The pinned handshake: an explicit 2025-06-18 request, then `initialized`.

    Stated rather than negotiated to whatever the SDK currently prefers, so the
    protocol revision these recoveries are evidence about is the one named here.
    """
    initialized = await session.send_request(
        types.InitializeRequest(
            params=types.InitializeRequestParams(
                protocol_version=PROTOCOL_VERSION,
                capabilities=types.ClientCapabilities(),
                client_info=types.Implementation(
                    name="omnivia-core-recovery", version="0"
                ),
            )
        ),
        types.InitializeResult,
    )
    session.adopt(initialized)
    await session.send_notification(types.InitializedNotification())
    return initialized


def _production(config: Path) -> StdioServerParameters:
    """The server a host launches, spawned the way a host launches it.

    The child is given this interpreter's environment deliberately: the SDK
    sanitizes it when `env` is `None`, which in a worktree drops the
    `PYTHONPATH` that selects the source tree under test.
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE, "--config", str(config)],
        env=dict(os.environ),
    )


def _relayed(config: Path, marker: Path) -> StdioServerParameters:
    """The same server, behind the relay that withholds one answer."""
    return StdioServerParameters(
        command=sys.executable,
        args=[
            str(RELAY),
            "--config",
            str(config),
            "--withhold",
            INTERRUPTED_TOOL,
            "--marker",
            str(marker),
        ],
        env=dict(os.environ),
    )


async def _interrupted_session(config: Path, marker: Path) -> dict[str, Any]:
    """Handshake, then one capture whose answer never arrives.

    `answered` is the claim: `False` means the call raised rather than returning
    a result, so no disposition, no identifier and no refusal document reached
    this side -- the host knows nothing about what became of its mutation. The
    teardown around it is allowed to fail in its own right, because by then the
    relay has exited and the SDK is closing pipes to a process that is gone;
    what matters is recorded before that and asserted at the call site.
    """
    observed: dict[str, Any] = {}
    with anyio.fail_after(_SESSION_TIMEOUT_SECONDS):
        try:
            async with (
                stdio_client(_relayed(config, marker)) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                initialized = await _open(session)
                observed["server_name"] = initialized.server_info.name
                listed = await session.list_tools()
                observed["tools"] = [tool.name for tool in listed.tools]
                try:
                    await session.call_tool(
                        INTERRUPTED_TOOL, _capture_call(key=INTERRUPTED_KEY)
                    )
                except Exception:  # noqa: BLE001 - any failure is "no result"
                    observed["answered"] = False
                else:
                    observed["answered"] = True
        except Exception:  # noqa: BLE001 - the torn-down transport, not a result
            observed.setdefault("answered", False)
    return observed


async def _recovery_session(config: Path) -> dict[str, Any]:
    """A new session against the restarted service: look, then replay, then look.

    The search *before* the replay is the load-bearing one. A replay's own answer
    cannot distinguish "settled earlier" from "written just now", so what proves
    the interrupted call committed is that the artifact is already there when
    this session has issued no mutation at all.
    """
    observed: dict[str, Any] = {}
    with anyio.fail_after(_SESSION_TIMEOUT_SECONDS):
        async with (
            stdio_client(_production(config)) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await _open(session)

            async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                called = await session.call_tool(tool, arguments)
                return called.model_dump(mode="json")

            observed["protocol_version"] = initialized.protocol_version
            observed["server_name"] = initialized.server_info.name
            observed["tools"] = [tool.name for tool in (await session.list_tools()).tools]
            observed["before_replay"] = await call("evidence_search", {"query": TOKEN})
            observed["replay"] = await call(
                INTERRUPTED_TOOL, _capture_call(key=INTERRUPTED_KEY)
            )
            observed["after_replay"] = await call("evidence_search", {"query": TOKEN})
            # The other half of "the host may not invent its own recovery": the
            # settled key does not accept a changed payload, and says so without
            # quoting one.
            observed["conflict"] = await call(
                INTERRUPTED_TOOL,
                _capture_call(CAPTURED_NOTE + "recovered\n", key=INTERRUPTED_KEY),
            )
            observed["after_conflict"] = await call(
                "evidence_search", {"query": TOKEN}
            )
            # And what inventing a key would actually have cost, measured rather
            # than argued: the same bytes under a key nothing settled.
            observed["invented"] = await call(
                INTERRUPTED_TOOL, _capture_call(key=INVENTED_KEY)
            )
            observed["after_invented"] = await call(
                "evidence_search", {"query": TOKEN}
            )
    return observed


async def _settling_session(config: Path, principal_id: str) -> dict[str, Any]:
    """Session A of the cross-session case: settle both mutations, then close."""
    observed: dict[str, Any] = {}
    with anyio.fail_after(_SESSION_TIMEOUT_SECONDS):
        async with (
            stdio_client(_production(config)) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await _open(session)

            async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                called = await session.call_tool(tool, arguments)
                return called.model_dump(mode="json")

            observed["capture"] = await call(
                INTERRUPTED_TOOL, _capture_call(key=SESSION_A_CAPTURE_KEY)
            )
            observed["memory"] = await call(
                "memory_create",
                {
                    "input": _memory_input(principal_id, PROPOSED_FACT),
                    "idempotency_key": SESSION_A_MEMORY_KEY,
                },
            )
    return observed


async def _replaying_session(config: Path, principal_id: str) -> dict[str, Any]:
    """Session B: replay both of session A's keys, then read the workspace back."""
    observed: dict[str, Any] = {}
    with anyio.fail_after(_SESSION_TIMEOUT_SECONDS):
        async with (
            stdio_client(_production(config)) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await _open(session)

            async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                called = await session.call_tool(tool, arguments)
                return called.model_dump(mode="json")

            observed["protocol_version"] = initialized.protocol_version
            observed["capture_replay"] = await call(
                INTERRUPTED_TOOL, _capture_call(key=SESSION_A_CAPTURE_KEY)
            )
            observed["memory_replay"] = await call(
                "memory_create",
                {
                    "input": _memory_input(principal_id, PROPOSED_FACT),
                    "idempotency_key": SESSION_A_MEMORY_KEY,
                },
            )
            # After both replays, because "the replay answered from the settled
            # outcome" and "the replay wrote a second row that happened to look
            # the same" are indistinguishable from the replay's own answer.
            observed["evidence"] = await call("evidence_search", {"query": TOKEN})
            observed["candidates"] = await call(
                "memory_search", {"query": TOKEN, "view": "candidates"}
            )
            observed["default_memory"] = await call("memory_search", {"query": TOKEN})
    return observed


def _carries_nothing_it_should_not(observed: dict[str, Any]) -> None:
    """No answer in `observed` quotes the captured note or a filesystem path.

    The first assertion is what keeps the second from rotting into a no-op: a
    fragment that stopped being in the submission would be trivially absent from
    every answer, and this guard would pass for the wrong reason.
    """
    rendered = json.dumps(observed, ensure_ascii=False)
    for fragment in NOTE_FRAGMENTS:
        assert fragment in CAPTURED_NOTE, "the guarded fragment is not submitted"
        assert fragment not in rendered, "an answer quoted the submitted content"


def test_an_interrupted_capture_is_recovered_by_the_same_key_after_a_restart(
    tmp_path: Path,
) -> None:
    """Section 13.F, F-2 and F-6: the answer is lost, the effect is not.

    In order:

    * the installed command configures `claude-code` for the authoring profile
      on a workspace the installation minted and nothing has written to;
    * a real SDK client completes a pinned 2025-06-18 handshake against the
      production server module -- reached through a relay that forwards every
      byte -- and is advertised the authoring eleven;
    * it calls `evidence_capture`; Core commits and answers; the relay withholds
      that answer and closes the streams, so the call raises and the host is left
      with no result, no refusal and no identifier;
    * the independently owned Core service is stopped and started again, and the
      descriptor the new process publishes is a later fencing generation from a
      different process, so this is a restart rather than a reconnection;
    * a second MCP session, on the production entry point, searches before it
      writes anything -- and finds exactly one artifact, byte-exact and cited by
      the source tuple the lost call declared. The effect survived both the
      interrupted response and the restart, which is why assuming it had not
      happened would have been the unsafe move;
    * replaying the same key returns that same canonical result and leaves the
      artifact exactly as it was, while the same key with a changed payload is
      refused as an idempotency conflict that quotes neither the submission nor
      the trusted document's path;
    * the key a host would have had to invent is then sent with byte-identical
      input, so what it costs is attributable to the key alone -- and it costs
      two things. It answers `already_captured` rather than the `created` the
      lost call settled, so a host that guessed would have recorded a different
      canonical result for its own write; and it lands a second claim, outcome,
      M1 audit event and execution for one effect. The artifact itself is not
      duplicated, because capture identity is content-addressed rather than
      key-addressed -- which is the honest shape of this hazard and the reason
      the requirement asks for the original effect to be found rather than for
      zero writes; and
    * the coordinator's own ledger holds, for the interrupted key, one claim, one
      outcome, one audit event and one execution of the domain code, with the
      replay recorded as a replay and nothing else added.
    """
    # Outside the installation, because the fixture removes the whole workspace
    # tree on the way out and the marker is read after that.
    marker = tmp_path / "withheld"
    with fixture.serving(seed=False, configure=False) as service:
        config = _configure(service.installation_state, service.workspace_id)
        # The opaque name the installed setup filed this host's bearer under,
        # read here because the trusted document lives inside the installation
        # this fixture removes on the way out. Not credential material -- the
        # document holds no bearer and has no field one could be in -- but it is
        # the nearest thing to one that exists on this side, and no answer has
        # any business repeating it.
        reference = json.loads(config.read_text(encoding="utf-8"))[
            "credential_reference"
        ]

        interrupted = anyio.run(lambda: _interrupted_session(config, marker))

        before = service.descriptor()
        stopped = service.process.pid
        after = service.restart()

        recovered = anyio.run(lambda: _recovery_session(config))

        assert service.process.poll() is None, "the service did not outlive the replay"
        service.stop()
        settlement = fixture.settlement(service.database, INTERRUPTED_KEY)
        invented = fixture.settlement(service.database, INVENTED_KEY)

    # --- the first session was real, and it was told nothing --------------------

    assert interrupted["server_name"] == SERVER_NAME, "another server answered"
    assert interrupted["tools"] == list(AUTHORING_TOOLS), "the listing is not the eleven"
    assert interrupted["answered"] is False, "the interrupted call returned a result"
    # The relay only writes this once it has a reply in hand, so its presence is
    # what separates "the answer was withheld" from "the call never got that far".
    assert marker.is_file(), "no answer was withheld"
    assert marker.read_text(encoding="utf-8") == INTERRUPTED_TOOL, (
        "another call was interrupted"
    )

    # --- the service was stopped and started, not merely redialled --------------

    assert after.fencing_generation > before.fencing_generation, "no new acquisition"
    assert service.process.pid != stopped, "the same process served both sessions"

    # --- the committed effect was already there, before any replay --------------

    assert recovered["protocol_version"] == PROTOCOL_VERSION, "another revision"
    assert recovered["server_name"] == SERVER_NAME, "another server answered"
    assert recovered["tools"] == list(AUTHORING_TOOLS), "the surface changed"

    (survived,) = _answer(recovered, "before_replay")["evidence"]
    assert survived["content_checksum"] == CAPTURED_CHECKSUM, "the stored bytes differ"
    assert survived["source"] == CAPTURED_SOURCE_TUPLE, "the stored source differs"
    assert survived["tombstoned"] is False, "the artifact is not live"
    assert [event["action"] for event in survived["provenance_history"]] == [
        "captured"
    ], "the capture history is not one capture"

    # --- the same key answers with that same canonical result -------------------

    replayed = _answer(recovered, "replay")
    assert replayed["evidence_id"] == survived["evidence_id"], "another artifact"
    assert replayed["capture_disposition"] == "created", "the settled outcome changed"
    assert replayed["media_type"] == "text/markdown", "the media type changed"
    assert replayed["content_length_bytes"] == len(CAPTURED_BYTES), "the length changed"
    assert replayed["content_checksum"] == CAPTURED_CHECKSUM, "the content changed"
    assert replayed["source"] == CAPTURED_SOURCE_TUPLE, "the source tuple changed"
    assert _answer(recovered, "after_replay")["evidence"] == [survived], (
        "the replay left a second artifact"
    )

    # --- a changed payload under that key is refused, and quotes nothing --------

    error = _refusal(recovered, "conflict")
    assert error["code"] == "idempotency_conflict", "the conflict was not a conflict"
    assert error["retry_class"] == "non_retryable", "the conflict invited a retry"
    assert _answer(recovered, "after_conflict")["evidence"] == [survived], (
        "the refused call wrote something"
    )
    _carries_nothing_it_should_not(recovered)
    rendered = json.dumps(recovered, ensure_ascii=False)
    assert str(config) not in rendered, "an answer named the trusted document"
    assert service.endpoint_uri not in rendered, "an answer named the endpoint"
    assert reference and reference not in rendered, "an answer named the credential"

    # --- what inventing a key would have cost, in the two ways it costs ---------

    guessed = _answer(recovered, "invented")
    assert guessed["evidence_id"] == survived["evidence_id"], (
        "the invented key answered about another artifact"
    )
    assert guessed["capture_disposition"] == "already_captured", (
        "the invented key did not report the effect as pre-existing"
    )
    assert guessed["capture_disposition"] != replayed["capture_disposition"], (
        "the invented key answered with the result the interrupted call settled"
    )
    assert _answer(recovered, "after_invented")["evidence"] == [survived], (
        "the invented key left a second artifact"
    )

    # --- one settled mutation, whatever the transport did -----------------------

    assert {name: settlement[name] for name in ONE_SETTLEMENT} == ONE_SETTLEMENT, (
        "the interrupted call did not settle exactly once"
    )
    assert settlement["replayed"] == 1, "the replay did not settle as a replay"
    # The invented key's own claim, outcome, audit event and execution -- a whole
    # second settlement for one effect, which the same key never produced.
    assert {name: invented[name] for name in ONE_SETTLEMENT} == ONE_SETTLEMENT, (
        "the invented key did not settle independently"
    )
    assert invented["replayed"] == 0, "the invented key was served as a replay"


def test_a_second_mcp_session_replays_the_key_the_first_one_settled() -> None:
    """Section 13.F, F-8: a key minted in one session, recovered in the next.

    Not the same-session replay section 13.B already runs, and not the restart
    above: one service, serving throughout, and two MCP sessions that never
    overlap. Session A captures and proposes, settles both, and closes its
    streams. Session B is a new server process with its own pinned handshake,
    which has seen neither call and holds none of session A's state; it replays
    both keys with byte-identical input and must receive both canonical results
    back unchanged.

    The duplicate a bad replay would leave is looked for in both places it could
    be: through the tools, where it would be a second artifact, a second
    candidate or a proposal that reached the default view; and in the
    coordinator's ledger, where it would be a second claim, outcome, audit event
    or execution of the domain code. The one count that does grow is `replayed`,
    and it is supposed to -- an honest replay runs no domain code but does
    durably spend the fresh grant it was re-authorized with.
    """
    with fixture.serving(seed=False, configure=False) as service:
        config = _configure(service.installation_state, service.workspace_id)
        principal_id = json.loads(config.read_text(encoding="utf-8"))["principal_id"]

        settled = anyio.run(lambda: _settling_session(config, principal_id))
        between = (service.process.pid, service.descriptor().fencing_generation)
        replayed = anyio.run(lambda: _replaying_session(config, principal_id))

        assert service.process.poll() is None, "the service did not outlive both"
        after = (service.process.pid, service.descriptor().fencing_generation)
        service.stop()
        capture_settlement = fixture.settlement(
            service.database, SESSION_A_CAPTURE_KEY
        )
        memory_settlement = fixture.settlement(service.database, SESSION_A_MEMORY_KEY)

    # --- one service answered both sessions -------------------------------------

    assert between == after, "the service was replaced or reacquired between them"

    # --- both replays answered with session A's own canonical results ------------

    assert replayed["protocol_version"] == PROTOCOL_VERSION, "another revision"
    captured = _answer(settled, "capture")
    record = _answer(settled, "memory")["record"]
    assert _answer(replayed, "capture_replay") == captured, "a capture replay rewrote"
    assert _answer(replayed, "memory_replay")["record"] == record, (
        "a memory replay rewrote"
    )

    # --- and left exactly one of each thing they could have duplicated -----------

    (artifact,) = _answer(replayed, "evidence")["evidence"]
    assert artifact["evidence_id"] == captured["evidence_id"], "a second artifact"
    assert artifact["content_checksum"] == CAPTURED_CHECKSUM, "the stored bytes differ"
    (candidate,) = _answer(replayed, "candidates")["records"]
    assert (
        candidate["provenance"]["identity"]["record_id"]
        == record["provenance"]["identity"]["record_id"]
    ), "a second candidate"
    assert candidate["content"] == {"fact": PROPOSED_FACT}, "the candidate differs"
    assert _answer(replayed, "default_memory")["records"] == [], (
        "a cross-session replay published the proposal"
    )
    _carries_nothing_it_should_not(replayed)

    # --- and one settlement each in the ledger no tool exposes -------------------

    for counts in (capture_settlement, memory_settlement):
        assert {name: counts[name] for name in ONE_SETTLEMENT} == ONE_SETTLEMENT, (
            "a cross-session replay settled a second time"
        )
        assert counts["replayed"] == 1, "the replay did not settle as a replay"
