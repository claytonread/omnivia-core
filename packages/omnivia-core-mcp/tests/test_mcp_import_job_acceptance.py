"""R004 section 13.D: one staged import, executed and observed through MCP alone.

Section 13.B's journey proves an empty workspace can be authored into. This one
proves the other half of the authoring profile -- the asynchronous half -- on a
workspace that holds exactly one thing it did not create: a staging handle.

**Staging is on the far side of the boundary, deliberately.** R004 section 8.3
puts it outside this milestone: the handle must already have been produced by an
installed, trusted Core path, and `import_start` accepts no archive, path or URL
it could produce one from. So `_mcp_v06_3_fixture.serving(stage=True)` writes the
verified staged source through the suite's trusted fixture boundary -- the one
module allowed to reach the runtime -- and everything below only ever *names* it.
There is no staging tool here, no path or URL input, and no SQL in this file.

**The principal is the installed command's, and the session is a real host's.**
`omnivia mcp configure --host claude-code --profile authoring` mints it, exactly
as it does on a laptop; the client is the official SDK spawning
`python -m omnivia_core_mcp.server --config <path>`; and the handshake is an
explicit 2025-06-18 `InitializeRequest` rather than whatever the SDK currently
prefers. The only thing read out of the protected document is where it is -- the
path the redacted snippet names -- and nothing is read out of it at all.

**Nothing here executes the job, and that is the claim.** `import_start` settles
a durable job and answers; Core's own service-owned executor runs it, on the
service's thread, under the service's identity and fencing generation, with no
call from this file and no state written by it. What this module can therefore
say about execution is only what an MCP client can see: a terminal result whose
accounting adds up, an event stream that says the job ran, and an evidence
artifact bound to that run which `evidence_search` answers with. Every one of
those is read back through a tool.

**The revocation happens while the session is open, which is the only time it
means anything.** A revoked host that had already closed its session proves
nothing; what section 13.D asks is whether a *live* server can keep reading and
keep replaying a settled mutation after the owner has revoked it. So
`omnivia mcp revoke` runs mid-session, between the observations that must succeed
and the ones that must not, and the owner's own `omnivia job get` runs afterwards
to show that the committed work outlived the principal that started it.
Revocation is not cancellation, and this journey is the difference stated twice:
the MCP side stops answering, the job does not stop -- and by then the job has
already finished, under an identity the revocation never touched.

**No assertion message carries anything it could leak.** Every message below is a
fixed sentence: no paths, no bearers, no job identifiers, no service envelopes
and no answer bodies, because a hosted failure log is read by whoever finds it.
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

#: The two job controls R004 section 3.3 excludes from this milestone. Asserted
#: against the advertised inventory *and* called, because "not advertised" and
#: "not reachable" are different claims and a model that guessed a name would be
#: making the second one.
ABSENT_TOOLS = ("job_cancel", "job_retry")

#: One key for the whole journey: the fresh start, the replay that must settle to
#: it, the conflicting descriptor that must not, and the replay after revocation
#: that must no longer be served.
IMPORT_KEY = "ovmcpimport-start-001"

#: The job this workspace must hold exactly one of, spelled as the catalogue
#: spells it, and the terminal result kind `import.start` bound to it.
IMPORT_JOB_KIND = "ingestion.import"
IMPORT_OPERATION = "import.start"
IMPORT_RESULT_KIND = "import_completion"

#: The accounting one staged descriptor must produce. A staged handle names one
#: immutable blob, so the run discovers one item and turns it into one L0 evidence
#: artifact; nothing is skipped and nothing fails, and `partial` follows from that
#: rather than being an independent claim.
EXPECTED_ACCOUNTING = {
    "discovered_items": 1,
    "evidence_records_created": 1,
    "skipped_items": 0,
    "failed_items": 0,
    "partial": False,
}

#: The query the created evidence must answer. The staged source's own kind, taken
#: from the fixture's descriptor rather than spelled again: it is the one part of
#: the evidence's public source identity this journey already knows, and this
#: workspace holds no other artifact for it to be confused with. A query this file
#: invented could only prove that some artifact matched some word.
EVIDENCE_QUERY = str(fixture.STAGED_SOURCE["source_kind"])

#: One event per page, which is what makes the traversal a traversal: the settled
#: job has more events than this, so the first page cannot be the whole snapshot
#: and the continuation token has to be followed to see the rest.
EVENTS_PAGE_LIMIT = 1

#: The whole session's budget, generous enough to contain the installed
#: revocation that runs inside it.
_JOURNEY_TIMEOUT_SECONDS = 600.0


def _configure(installation_state: Path, workspace_id: str) -> Path:
    """Run the installed setup command, and return the path its snippet names.

    The production invocation: the same console entry point, the same host word,
    the minted workspace, and the authoring profile stated explicitly -- the
    separate human act R004 section 9.3 requires before a wider surface may exist
    at all. Only the redacted snippet is parsed, and only for the path, which is
    how the protected document is located without this module knowing where such
    a document lives or reading a byte of what is in it.
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
    flag, path = entry["args"]
    assert flag == "--config", "the snippet did not name a configuration path"
    return Path(path)


def _revoke(installation_state: Path) -> None:
    """`omnivia mcp revoke --host claude-code`, the owner's own command.

    Narrowed to the one host this journey configured, because revoking every host
    would also be revoking ones nothing here set up, and the claim below is about
    what happens to *this* session.
    """
    completed = fixture.installed_cli(
        installation_state, "mcp", "revoke", "--host", fixture.MCP_HOST
    )
    assert completed.returncode == 0, "the installed revoke command refused"
    assert completed.stdout == f"revoked {fixture.MCP_HOST}\n", (
        "the revocation did not report the host it was asked for"
    )


def _owner_job_get(
    installation_state: Path, workspace_id: str, job_id: str
) -> dict[str, Any]:
    """`omnivia job get --json`, the canonical operator path, after revocation.

    The same installed CLI, the same service, and a path that has nothing to do
    with the revoked MCP principal: this is the owner asking Core directly. What
    it answers is the whole of section 13.D's last clause -- committed
    service-owned work is still there, and still observable by whoever owns it.
    """
    completed = fixture.installed_cli(
        installation_state,
        "--workspace-id",
        workspace_id,
        "job",
        "get",
        "--input-json",
        json.dumps({"job_id": job_id}),
        "--json",
    )
    assert completed.returncode == 0, "the owner could not observe the job"
    answered: dict[str, Any] = json.loads(completed.stdout)["result"]
    return answered


def _source(**overrides: Any) -> dict[str, Any]:
    """The staged descriptor the fixture wrote, or a variant of it.

    A copy of the fixture's constant rather than a literal of this module's own:
    `import.start` matches a staged row on every field at once, so a descriptor
    spelled twice would read as Core refusing rather than as the two spellings
    having drifted.
    """
    return {**fixture.STAGED_SOURCE, **overrides}


async def _journey(config: Path, installation_state: Path) -> dict[str, Any]:
    """The whole session: start, replay, conflict, observe, revoke, fail closed.

    One session for all of it, because the revocation is only a claim about a
    session that was already open and already admitted. The child is given this
    interpreter's environment deliberately: the SDK sanitizes it when `env` is
    `None`, which in a worktree drops the `PYTHONPATH` that selects the source
    tree under test.

    Nothing here waits for the job, polls it, or retries a read. The service
    executes committed import work on its own serving thread between requests, so
    by the time it accepts the call after `import_start` the work is done -- and
    an observation that had to be retried until it agreed would be evidence about
    this loop rather than about Core.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE, "--config", str(config)],
        env=dict(os.environ),
    )
    start = {"input": {"source": _source()}, "idempotency_key": IMPORT_KEY}
    # The same key over a descriptor that names more bytes than the staging
    # verified. A different *request*, and the only thing that makes it one is the
    # descriptor, which is what the idempotency fingerprint is taken over.
    conflicting = {
        "input": {"source": _source(content_length_bytes=8192)},
        "idempotency_key": IMPORT_KEY,
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
                "start": await call("import_start", start),
            }
            # The one assertion inside the session, and it is here because every
            # call after it names this job: a refused start would otherwise fail
            # the journey somewhere downstream, as a shape error about an answer
            # that was never given.
            assert observed["start"]["is_error"] is False, (
                "the staged import was not accepted"
            )
            job_id = observed["start"]["structured_content"]["job"]["identity"][
                "job_id"
            ]
            observed["job_id"] = job_id
            observed["replay"] = await call("import_start", start)
            observed["conflict"] = await call("import_start", conflicting)
            # After both, because "the replay settled to the first job" and "the
            # replay enqueued a second one that looks the same" are not
            # distinguishable from the replay's own answer. A second enqueue under
            # this key would show here as a second attempt or a second event.
            observed["get"] = await call("job_get", {"job_id": job_id})

            # The traversal. One event per page, followed by its own continuation
            # token, until the snapshot the first page declared is exhausted.
            pages: list[dict[str, Any]] = []
            page_arguments: dict[str, Any] = {
                "job_id": job_id,
                "limit": EVENTS_PAGE_LIMIT,
            }
            while True:
                page = await call("job_events", page_arguments)
                pages.append(page)
                assert page["is_error"] is False, "an event page was refused"
                token = page["structured_content"]["page"].get("continuation_token")
                if token is None:
                    break
                page_arguments = {
                    "job_id": job_id,
                    "limit": EVENTS_PAGE_LIMIT,
                    "page": {"continuation_token": token},
                }
                assert len(pages) < 16, "the event traversal did not terminate"
            observed["pages"] = pages
            # The same first page again, unpaged and unbounded: a snapshot that
            # moved between the traversal and this read would show as a different
            # count, and a stream that was not ordered would show as a different
            # sequence.
            observed["events"] = await call("job_events", {"job_id": job_id})
            observed["evidence"] = await call(
                "evidence_search", {"query": EVIDENCE_QUERY}
            )
            observed["absent"] = {
                name: await call(name, {"job_id": job_id}) for name in ABSENT_TOOLS
            }

            # The owner revokes while this session is open and admitted. In a
            # thread because the CLI is a blocking subprocess and the session's
            # transport is running in this event loop.
            await anyio.to_thread.run_sync(_revoke, installation_state)

            observed["get_after"] = await call("job_get", {"job_id": job_id})
            observed["events_after"] = await call("job_events", {"job_id": job_id})
            # The same key and the same input that settled a moment ago. R004
            # section 7 is explicit that replay is not an authorization bypass:
            # a revoked principal may not use an old key to recover a result it
            # may no longer observe.
            observed["replay_after"] = await call("import_start", start)
            return observed


def _succeeded(called: dict[str, Any], key: str) -> dict[str, Any]:
    """One successful call's structured content, refusing anything else."""
    assert called["is_error"] is False, f"{key} did not succeed"
    answer = called["structured_content"]
    assert isinstance(answer, dict), f"{key} carried no structured answer"
    return answer


def _answer(observed: dict[str, Any], key: str) -> dict[str, Any]:
    """The same, for one call the journey recorded under a name."""
    return _succeeded(observed[key], key)


def _relayed(called: dict[str, Any], key: str) -> dict[str, Any]:
    """The service's own response document out of one relayed refusal.

    Refuses anything that is not one: an MCP-side refusal never reaches the
    service and carries no envelope, so reading one here is what distinguishes
    "Core decided" from "this adapter decided". The envelope itself is never
    surfaced -- only the fields asserted on.
    """
    assert called["is_error"] is True, f"{key} was not refused"
    assert called["structured_content"] is None, f"{key} carried a structured answer"
    message = called["content"][0]["text"]
    assert "was refused by the service" in message, f"{key} was not the service's own"
    document: dict[str, Any] = json.loads(
        message.split("was refused by the service: ", 1)[1]
    )
    return document


def _blocked(observed: dict[str, Any], key: str) -> None:
    """One call the revoked session could not make, refused before Core saw it.

    The other half of :func:`_relayed`, and the distinction matters: after the
    owner revokes, this installation no longer holds the material the session
    would have to present, so the call is refused here rather than answered
    anywhere. A refusal carrying a service envelope would mean the call had still
    reached Core, which is the thing revocation must stop.
    """
    called = observed[key]
    assert called["is_error"] is True, f"{key} was not refused after revocation"
    assert called["structured_content"] is None, f"{key} answered after revocation"
    message = called["content"][0]["text"]
    assert "was refused by the service" not in message, f"{key} still reached Core"
    assert "could not be called" in message, f"{key} was refused for another reason"


def test_a_staged_import_is_executed_observed_and_survives_revocation() -> None:
    """Section 13.D, executed: one staged handle in, one settled import out.

    The workspace holds exactly one thing when this begins -- the verified staged
    source a trusted installed path left behind -- and no host is configured. In
    order:

    * the installed command configures `claude-code` for the authoring profile,
      and a real SDK client completes a pinned 2025-06-18 handshake against the
      module a host launches;
    * the advertised surface is the authoring eleven, and neither `job_cancel`
      nor `job_retry` is among them or reachable by name;
    * `import_start` over that staged descriptor answers with one durable
      `ingestion.import` job, and Core's own executor carries it to a terminal
      `import_completion` with no call from here;
    * replaying the same key over the same descriptor answers with the settled
      outcome rather than starting a second job, and the job read back afterwards
      is still on attempt one -- which is where a second enqueue would show;
    * the same key over a descriptor naming different bytes is refused as an
      `idempotency_conflict`, and the refusal still names the job the key is
      bound to, so the conflict is the key defending its settled outcome rather
      than the descriptor being rejected on its own;
    * `job_get` observes the terminal result, and its accounting adds up: one item
      discovered, one L0 evidence record created, nothing skipped, nothing failed
      and therefore not partial;
    * `job_events` is traversed one event per page across more than one page, and
      the pages are contiguous, ordered and bounded by the one snapshot the first
      page declared -- a second, unpaged read of the same stream agrees with them;
    * the evidence that import created is retrievable through MCP, bound to this
      run, addressing the bytes the staged descriptor named, and carrying no
      locator -- nothing this workspace holds names a path;
    * the owner revokes the host *while the session is open*, and every later
      read, observation and same-key replay is refused without reaching Core;
    * and the job is still there and still settled when the owner asks through
      the canonical CLI path, because revocation is not cancellation.
    """
    with fixture.serving(seed=False, stage=True, configure=False) as service:
        config = _configure(service.installation_state, service.workspace_id)

        observed = anyio.run(lambda: _journey(config, service.installation_state))

        # After the client's streams are closed and before the fixture stops the
        # service: the committed job outlived the principal that started it, and
        # only the owner's own path can still say so.
        assert service.process.poll() is None, "the service did not outlive the session"
        owned = _owner_job_get(
            service.installation_state, service.workspace_id, observed["job_id"]
        )

    # --- the handshake and the advertised surface -----------------------------

    assert observed["protocol_version"] == PROTOCOL_VERSION, "another revision"
    assert observed["server_name"] == SERVER_NAME, "another server answered"
    authoring = [entry.tool_name for entry in exposure_manifest("authoring")]
    assert observed["tools"] == authoring, "the listing is not the authoring surface"
    for name in ABSENT_TOOLS:
        assert name not in authoring, "a job control reached the authoring manifest"
        refused = observed["absent"][name]
        assert refused["is_error"] is True, "a job control was not refused"
        assert "is not a tool this server exposes" in refused["content"][0]["text"], (
            "a job control was refused for another reason"
        )

    # --- one staged descriptor, one durable job -------------------------------

    started = _answer(observed, "start")["job"]
    identity = started["identity"]
    assert identity["job_kind"] == IMPORT_JOB_KIND, "another kind of job was started"
    assert identity["originating_operation"] == IMPORT_OPERATION, "another operation"
    assert started["latest_attempt"]["attempt_number"] == 1, "not the first attempt"

    # --- the replay settles to that job, and enqueues nothing -----------------

    assert _answer(observed, "replay") == _answer(observed, "start"), (
        "a same-key replay did not answer from the settled outcome"
    )

    read = _answer(observed, "get")
    assert read["job"]["identity"] == identity, "job_get answered about another job"
    assert read["job"]["created_at"] == started["created_at"], "the job was recreated"
    assert read["job"]["latest_attempt"]["attempt_number"] == 1, (
        "the replay opened a second attempt"
    )

    # --- the job reached a terminal import completion -------------------------

    assert read["job"]["state"] == "succeeded", "the started import did not settle"
    assert read["job"]["latest_attempt"]["state"] == "succeeded", "the attempt is open"
    terminal = read["terminal_result"]
    assert terminal["state"] == "succeeded", "the terminal result is not a success"
    assert terminal["result_kind"] == IMPORT_RESULT_KIND, "another result kind"
    completion = terminal["result"]
    assert completion["import_run_id"] == observed["job_id"], (
        "the completion names another run than the job it settled"
    )
    assert completion["source"] == _source(), (
        "the completion reports a descriptor other than the one accepted"
    )
    assert {key: completion[key] for key in EXPECTED_ACCOUNTING} == EXPECTED_ACCOUNTING, (
        "the terminal accounting is not the one this staged source can produce"
    )

    # --- the events paginate, ordered and snapshot-stable ---------------------

    pages = [
        _succeeded(page, f"event page {number}")
        for number, page in enumerate(observed["pages"])
    ]
    assert len(pages) > 1, "the traversal was not more than one page"
    snapshot = pages[0]["snapshot_event_count"]
    sequences = [event["sequence"] for page in pages for event in page["events"]]
    assert sequences == list(range(snapshot)), (
        "the traversal is not one contiguous ordered stream covering its snapshot"
    )
    for page in pages:
        assert page["job_id"] == observed["job_id"], "another job's events"
        assert page["snapshot_event_count"] == snapshot, "the snapshot moved mid-session"
        assert 0 < len(page["events"]) <= EVENTS_PAGE_LIMIT, "a page ignored its limit"
    for page in pages[:-1]:
        assert page["page"].get("continuation_token"), (
            "a page short of the snapshot offered no way to continue"
        )
    assert pages[-1]["page"] == {}, "an exhausted snapshot offered a continuation"
    assert [event["state"] for page in pages for event in page["events"]] == [
        "running",
        "succeeded",
    ], "the stream does not say the job ran and then succeeded"

    # The same stream read again, in one unbounded page, agrees with the walk.
    whole = _answer(observed, "events")
    assert whole["snapshot_event_count"] == snapshot, "the stream moved between reads"
    assert [event["sequence"] for event in whole["events"]] == sequences, (
        "an unpaged read disagrees with the traversal"
    )
    assert whole["page"] == {}, "an exhausted snapshot offered a continuation"

    # --- the same key over a different descriptor conflicts -------------------

    document = _relayed(observed["conflict"], "conflict")
    assert document["error"]["code"] == "idempotency_conflict", "not a conflict"
    assert document["error"]["retry_class"] == "non_retryable", "it invited a retry"
    assert document["metadata"]["job"]["job_id"] == observed["job_id"], (
        "the conflict named another job than the key is bound to"
    )

    # --- the evidence the import created is retrievable through MCP -----------

    (artifact,) = _answer(observed, "evidence")["evidence"]
    assert artifact["import_run_id"] == observed["job_id"], (
        "the artifact is not bound to the import run that created it"
    )
    assert artifact["content_checksum"] == _source()["content_checksum"], (
        "the artifact does not address the bytes the staged descriptor named"
    )
    assert artifact["media_type"] == _source()["media_type"], "another media type"
    assert artifact["source"]["kind"] == _source()["source_kind"], "another source kind"
    assert "locator" not in artifact["source"], "the artifact published a locator"
    assert artifact["provenance_history"], "the artifact carries no provenance"
    assert artifact["provenance_history"][0]["actor_kind"] == "service", (
        "the evidence was not attributed to the Core service"
    )
    # Nothing about the source identity may be a path or a URL, whatever the
    # staging recorded about itself: this is the readable half of that claim.
    for fragment in ("/", "\\", "://"):
        assert fragment not in artifact["source"]["source_id"], (
            "the source identity carries something path-shaped"
        )

    # --- revocation stops the session, and does not stop the job --------------

    for key in ("get_after", "events_after", "replay_after"):
        _blocked(observed, key)

    assert owned["job"]["identity"] == identity, "the owner observed another job"
    assert owned["job"]["state"] == "succeeded", "revocation disturbed committed work"
    assert owned["job"]["latest_attempt"]["attempt_number"] == 1, (
        "revocation disturbed the committed attempt"
    )
    assert owned["terminal_result"]["result"] == completion, (
        "the owner's own path reports a different outcome than MCP was told"
    )
