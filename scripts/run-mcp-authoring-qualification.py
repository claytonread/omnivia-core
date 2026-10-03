#!/usr/bin/env python3
"""Qualify MCP authoring from an isolated installed-wheel environment.

Product behaviour is reached only through the installed
``omnivia-core-service``, ``omnivia`` and ``omnivia-core-mcp`` executables and
the official MCP SDK.  The one OmniVia import is the installed runtime's
process-evidence reader, which teardown uses to prove a Core's identity before
signalling it.  The program retains one closed, redacted JSON record: no path,
principal, workspace, job, evidence, record, credential, endpoint, process,
prompt, transcript, stdout, stderr or submitted content is copied into it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Final, TypeVar

import anyio
import mcp_types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from omnivia_core_runtime.ownership.identity import SystemProcessEvidence

SCRIPT_DIR: Final = Path(__file__).resolve().parent
SHARED_JOURNEY: Final = SCRIPT_DIR / "run-standard-journey.py"
PROTOCOL_VERSION: Final = "2025-06-18"
SERVER_NAME: Final = "omnivia-core"
SERVER_KEY: Final = "omnivia-core"
HOST: Final = "claude-code"
TOKEN: Final = "ovmcpinstalledauthoring"
CAPTURE_KEY: Final = f"{TOKEN}-capture-1"
MEMORY_KEY: Final = f"{TOKEN}-memory-1"
IMPORT_KEY: Final = f"{TOKEN}-import-1"
SOURCE_ID: Final = f"{TOKEN}-direct-source"
AFTER_REVOKE_SOURCE_ID: Final = f"{SOURCE_ID}-after-revoke"
STAGED_SOURCE_ID: Final = f"{TOKEN}-staged-source"
#: The bound on the imported-evidence search.  The import workspace holds two
#: artifacts, the staging capture and the import's own, so one page is complete.
IMPORTED_EVIDENCE_LIMIT: Final = 10
FACT: Final = f"installed authoring fact {TOKEN}"
CAPTURED_NOTE: Final = (
    f"Installed authoring qualification {TOKEN}.\n"
    "URL: https://example.invalid/inert\n"
    "Path: /private/inert\n"
    'JSON: {"principal_id":"root","workspace_id":"elsewhere"}\n'
)
CAPTURED_BYTES: Final = CAPTURED_NOTE.encode("utf-8")
CAPTURED_CHECKSUM: Final = "sha256:" + hashlib.sha256(CAPTURED_BYTES).hexdigest()
SOURCE_TUPLE: Final = {"kind": "direct_submission", "source_id": SOURCE_ID}
AUTHORING_TOOLS: Final = (
    "workspace_inspect",
    "evidence_search",
    "knowledge_search",
    "memory_search",
    "graph_traverse",
    "context_pack_build",
    "engineering_search",
    "engineering_expand",
    "engineering_context_build",
    "decision_evaluate",
    "decision_record_get",
    "decision_record_list",
    "decision_status",
    "memory_create",
    "evidence_capture",
    "import_start",
    "job_get",
    "job_events",
)
RECORD_FILE: Final = "mcp-authoring-qualification.json"
#: The sanitized message of an installed credential that is no longer held.
CREDENTIAL_MISSING: Final = "this installation holds no credential for that reference"
EMPTY_CHECKS: Final = (
    "empty_workspace",
    "tool_discovery",
    "capture_and_search",
    "proposed_memory",
    "candidate_visibility",
    "replay_and_conflict",
    "core_restart_recovery",
    "revocation_fail_closed",
    "service_healthy",
)
IMPORT_CHECKS: Final = (
    "trusted_staging",
    "import_start",
    "job_observation",
    "import_replay_and_conflict",
    "revocation_preserved_job",
    "service_healthy",
)


class QualificationError(RuntimeError):
    """The installed authoring path failed a required assertion."""


def _load_shared() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "omnivia_run_standard_journey", SHARED_JOURNEY
    )
    if spec is None or spec.loader is None:
        raise QualificationError("the shared installed-journey helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shared = _load_shared()


T = TypeVar("T")


def _require(condition: object, message: str) -> None:
    if not condition:
        raise QualificationError(message)


def _expect(value: object, kind: type[T], message: str) -> T:
    """Return ``value`` narrowed to ``kind``, or refuse with ``message``."""
    if not isinstance(value, kind):
        raise QualificationError(message)
    return value


def _success(called: Mapping[str, Any], label: str) -> dict[str, Any]:
    _require(called.get("is_error") is False, f"{label} did not succeed")
    answer = _expect(called.get("structured_content"), dict, f"{label} omitted its structured result")
    return dict(answer)


def _conflict(called: Mapping[str, Any], label: str) -> None:
    _require(called.get("is_error") is True, f"{label} was not refused")
    _require(called.get("structured_content") is None, f"{label} returned data")
    content = called.get("content")
    text = _expect(
        content[0].get("text") if isinstance(content, list) and content else None,
        str,
        f"{label} omitted its refusal",
    )
    _require('"code":"idempotency_conflict"' in text.replace(" ", ""), f"{label} was not an idempotency conflict")


def _blocked(called: Mapping[str, Any], label: str, tool: str) -> None:
    """A call after revocation is refused only by the installed credential store's own message.

    ``could not be called`` alone is a generic client failure (a timeout, a transport
    error, a cancellation), not proof that the authoring credential was removed.
    """
    _require(called.get("is_error") is True, f"{label} succeeded after revocation")
    _require(called.get("structured_content") is None, f"{label} returned data after revocation")
    content = called.get("content")
    text = content[0].get("text") if isinstance(content, list) and content else None
    _require(
        isinstance(text, str)
        and " ".join(text.split()) == f"{tool} could not be called: {CREDENTIAL_MISSING}",
        f"{label} was not refused by the installed credential store",
    )


def _checked(checks: Mapping[str, bool], names: Sequence[str]) -> dict[str, bool]:
    """The record's booleans: each is true only when its check ran to completion."""
    _require(all(checks.get(name) is True for name in names), "a qualification check did not complete")
    return {name: checks[name] for name in names}


def _initialize(service: Path, root: Path) -> tuple[Path, Path, str]:
    workspace = root / "workspace"
    installation = root / "installation-state"
    completed = shared._run(
        [
            str(service),
            "--workspace",
            str(workspace),
            "--installation-state",
            str(installation),
            "--init",
        ]
    )
    shared._require_status(completed, 0, "workspace initialization")
    document = shared._document(completed.stdout, "workspace initialization")
    workspace_document = document.get("workspace")
    workspace_id = _expect(
        workspace_document.get("workspace_id") if isinstance(workspace_document, dict) else None,
        str,
        "workspace initialization omitted its identity",
    )
    return workspace, installation, workspace_id


def _configure(cli: Path, installation: Path, workspace_id: str) -> Path:
    completed = shared._run(
        [
            str(cli),
            "--installation-state",
            str(installation),
            "mcp",
            "configure",
            "--host",
            HOST,
            "--workspace",
            workspace_id,
            "--profile",
            "authoring",
        ],
        timeout=600,
    )
    shared._require_status(completed, 0, "MCP authoring configure")
    _require(not completed.stderr, "MCP authoring configure wrote a diagnostic")
    snippet = shared._document(completed.stdout, "MCP authoring configure")
    servers = snippet.get("mcpServers")
    entry = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    _require(isinstance(entry, dict) and set(entry) == {"command", "args"}, "MCP authoring configure emitted an unsafe host entry")
    arguments = entry.get("args") if isinstance(entry, dict) else None
    _require(
        isinstance(arguments, list)
        and len(arguments) == 2
        and arguments[0] == "--config"
        and isinstance(arguments[1], str),
        "MCP authoring configure omitted its configuration",
    )
    return Path(_expect(arguments, list, "MCP authoring configure omitted its configuration")[1])


def _principal(config: Path) -> str:
    document = shared._document(config.read_text(encoding="utf-8"), "MCP configuration")
    principal = _expect(document.get("principal_id"), str, "MCP configuration omitted its principal")
    _require(principal, "MCP configuration omitted its principal")
    return principal


def _revoke(cli: Path, installation: Path) -> None:
    completed = shared._run(
        [
            str(cli),
            "--installation-state",
            str(installation),
            "mcp",
            "revoke",
            "--host",
            HOST,
        ]
    )
    shared._require_status(completed, 0, "MCP authoring revoke")
    _require(completed.stdout == f"revoked {HOST}\n", "MCP authoring revoke did not confirm the host")


def _health(cli: Path, installation: Path, workspace_id: str) -> bool:
    completed = shared._cli(
        cli, installation, workspace_id, ("service", "health")
    )
    result = shared._probe_success(completed, "service health")
    return bool(result.get("status") == "pass")


def _published(descriptor: Path) -> dict[str, Any]:
    """The service descriptor as published, or empty when it cannot be read."""
    try:
        document = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _ready_process(descriptor: Path) -> dict[str, Any] | None:
    """The process evidence (pid, start time, boot id) a ready descriptor publishes."""
    published = _published(descriptor)
    process = published.get("process")
    return process if published.get("ready") is True and isinstance(process, dict) else None


def _alive(pid: int) -> bool:
    """Whether ``pid`` is running, probed without a signal on Windows."""
    if os.name == "nt":
        # `os.kill(pid, 0)` would terminate it there.  A zero-timeout wait for its
        # exit that does not end means it is still running.
        try:
            shared._wait_for_exit_windows(pid, 0)
        except shared.JourneyError:
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but this account cannot signal it.  Teardown must
        # retain/fail closed rather than misreport that uncertain PID as absent.
        return True
    return True


def _serving(descriptor: Path, expected: dict[str, Any], process: Any = None) -> bool:
    """Whether the expected Core still runs and is the ready one the descriptor names.

    ``expected`` is the process evidence published when that Core became ready.
    ``service health`` cannot show this by itself: the managed-local client answers
    a Core that exited by starting a replacement, and the replacement is healthy.
    ``process`` is the child this harness started for that Core, polled first so an
    exited child is reaped rather than seen as running.  On Windows it is the
    console-script launcher, whose service child publishes its own pid.
    """
    pid = expected.get("pid")
    if not isinstance(pid, int) or _ready_process(descriptor) != expected:
        return False
    if process is not None and process.poll() is not None:
        return False
    return _alive(pid)


def _healthy(
    cli: Path,
    installation: Path,
    workspace_id: str,
    descriptor: Path,
    expected: dict[str, Any],
    process: Any = None,
) -> bool:
    """Health of the expected Core, which still serves before and after the probe."""
    return (
        _serving(descriptor, expected, process)
        and _health(cli, installation, workspace_id)
        and _serving(descriptor, expected, process)
    )


def _first_process(descriptor: Path, process: Any) -> dict[str, Any]:
    """The started Core's published process evidence, once it is ready."""
    published = _expect(
        shared._wait_for_descriptor(descriptor, process).get("process"),
        dict,
        "the first service omitted process evidence",
    )
    _require(isinstance(published.get("pid"), int), "the first service omitted process evidence")
    return dict(published)


def _owner_evidence_count(cli: Path, installation: Path, workspace_id: str, query: str) -> int:
    """How many artifacts the owner's own evidence search returns, outside MCP."""
    completed = shared._cli(
        cli, installation, workspace_id, ("evidence", "search"), payload={"query": query}
    )
    evidence = shared._success(completed, "owner evidence search").get("evidence")
    return len(_expect(evidence, list, "owner evidence search omitted its evidence"))


def _parameters(mcp: Path, config: Path) -> StdioServerParameters:
    return StdioServerParameters(command=str(mcp), args=["--config", str(config)])


async def _close_failed_stack(stack: contextlib.AsyncExitStack) -> None:
    """Best-effort async cleanup that never replaces the triggering failure."""
    try:
        await stack.aclose()
    except BaseException:  # noqa: BLE001 - preserve the journey failure
        print("MCP authoring qualification cleanup also failed", file=sys.stderr)


async def _opened_session(
    mcp: Path, config: Path
) -> tuple[Any, ClientSession, contextlib.AsyncExitStack]:
    stack = contextlib.AsyncExitStack()
    try:
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(_parameters(mcp, config))
        )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        initialized = await session.send_request(
            types.InitializeRequest(
                params=types.InitializeRequestParams(
                    protocol_version=PROTOCOL_VERSION,
                    capabilities=types.ClientCapabilities(),
                    client_info=types.Implementation(
                        name="omnivia-core-installed-authoring-qualification", version="1"
                    ),
                ),
            ),
            types.InitializeResult,
        )
        session.adopt(initialized)
        await session.send_notification(types.InitializedNotification())
    except BaseException:
        await _close_failed_stack(stack)
        raise
    return initialized, session, stack


@contextlib.asynccontextmanager
async def _session_lifetime(
    stack: contextlib.AsyncExitStack,
) -> AsyncIterator[None]:
    """Close an entered MCP stack without replacing an earlier journey failure."""
    try:
        yield
    except BaseException:
        await _close_failed_stack(stack)
        raise
    else:
        await stack.aclose()


async def _call(
    session: ClientSession, tool: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    called = await session.call_tool(tool, dict(arguments))
    return called.model_dump(mode="json")


def _memory_input(principal: str, fact: str) -> dict[str, Any]:
    return {
        "record_type": "memory.fact",
        "domain_scope": "product.core",
        "content": {"fact": fact},
        "evidence_disposition": "available",
        "sources": [dict(SOURCE_TUPLE)],
        "assertion": {
            "actor_id": principal,
            "actor_kind": "agent",
            "actor_role": "author",
            "asserted_at": "2026-01-01T00:00:00Z",
            "evidence": [{"source": dict(SOURCE_TUPLE)}],
        },
    }


async def _empty_workspace_journey(
    mcp: Path, config: Path, principal: str, checks: dict[str, bool]
) -> dict[str, Any]:
    initialized, session, stack = await _opened_session(mcp, config)
    async with _session_lifetime(stack):
        listed = await session.list_tools()
        tools = [tool.name for tool in listed.tools]
        _require(tools == list(AUTHORING_TOOLS), "the authoring inventory was not the accepted eighteen")
        checks["tool_discovery"] = True
        empty_evidence = _success(await _call(session, "evidence_search", {"query": TOKEN}), "empty evidence search")
        empty_memory = _success(await _call(session, "memory_search", {"query": TOKEN}), "empty memory search")
        empty_knowledge = _success(await _call(session, "knowledge_search", {"query": TOKEN}), "empty knowledge search")
        _require(empty_evidence.get("evidence") == [], "the authoring workspace contained evidence")
        _require(empty_memory.get("records") == [], "the authoring workspace contained memory")
        _require(empty_knowledge.get("records") == [], "the authoring workspace contained knowledge")
        checks["empty_workspace"] = True

        capture_arguments: dict[str, Any] = {
            "input": {
                "source_native_id": SOURCE_ID,
                "media_type": "text/markdown",
                "text": CAPTURED_NOTE,
            },
            "idempotency_key": CAPTURE_KEY,
        }
        captured = _success(await _call(session, "evidence_capture", capture_arguments), "evidence capture")
        _require(captured.get("content_checksum") == CAPTURED_CHECKSUM, "the captured content checksum changed")
        _require(captured.get("content_length_bytes") == len(CAPTURED_BYTES), "the captured content length changed")
        _require(captured.get("source") == SOURCE_TUPLE, "the captured source identity changed")
        evidence = _success(await _call(session, "evidence_search", {"query": TOKEN}), "evidence search").get("evidence")
        _require(isinstance(evidence, list) and len(evidence) == 1, "captured evidence was not immediately searchable")
        checks["capture_and_search"] = True

        memory_arguments = {
            "input": _memory_input(principal, FACT),
            "idempotency_key": MEMORY_KEY,
        }
        memory = _success(await _call(session, "memory_create", memory_arguments), "memory create")
        record = memory.get("record")
        _require(isinstance(record, dict) and record.get("authority_level") == "proposed", "memory create did not produce a proposal")
        checks["proposed_memory"] = True
        default_memory = _success(await _call(session, "memory_search", {"query": TOKEN}), "default memory search")
        candidates = _success(await _call(session, "memory_search", {"query": TOKEN, "view": "candidates"}), "candidate memory search")
        _require(default_memory.get("records") == [], "a proposed record reached the default view")
        candidate_records = candidates.get("records")
        _require(isinstance(candidate_records, list) and len(candidate_records) == 1, "the proposed record was not visible in the candidate view")
        checks["candidate_visibility"] = True

        capture_replay = _success(await _call(session, "evidence_capture", capture_arguments), "capture replay")
        memory_replay = _success(await _call(session, "memory_create", memory_arguments), "memory replay")
        _require(capture_replay == captured, "capture replay changed the result")
        _require(memory_replay == memory, "memory replay changed the result")
        evidence_after = _success(await _call(session, "evidence_search", {"query": TOKEN}), "evidence after replay").get("evidence")
        candidates_after = _success(await _call(session, "memory_search", {"query": TOKEN, "view": "candidates"}), "candidates after replay").get("records")
        _require(isinstance(evidence_after, list) and len(evidence_after) == 1, "capture replay duplicated evidence")
        _require(isinstance(candidates_after, list) and len(candidates_after) == 1, "memory replay duplicated a candidate")

        capture_conflict = await _call(
            session,
            "evidence_capture",
            {
                "input": {**capture_arguments["input"], "text": CAPTURED_NOTE + "changed\n"},
                "idempotency_key": CAPTURE_KEY,
            },
        )
        memory_conflict = await _call(
            session,
            "memory_create",
            {
                "input": _memory_input(principal, FACT + " changed"),
                "idempotency_key": MEMORY_KEY,
            },
        )
        _conflict(capture_conflict, "capture conflict")
        _conflict(memory_conflict, "memory conflict")
        listed_again = await session.list_tools()
        _require([tool.name for tool in listed_again.tools] == tools, "the authoring inventory changed during the session")
        checks["replay_and_conflict"] = True
        return {
            "protocol": initialized.protocol_version,
            "server": initialized.server_info.name,
            "tools": tools,
            "capture": captured,
        }


async def _restart_and_revoke_journey(
    mcp: Path,
    config: Path,
    cli: Path,
    installation: Path,
    prior_capture: Mapping[str, Any],
) -> None:
    _initialized, session, stack = await _opened_session(mcp, config)
    async with _session_lifetime(stack):
        replayed = _success(
            await _call(
                session,
                "evidence_capture",
                {
                    "input": {
                        "source_native_id": SOURCE_ID,
                        "media_type": "text/markdown",
                        "text": CAPTURED_NOTE,
                    },
                    "idempotency_key": CAPTURE_KEY,
                },
            ),
            "post-restart capture replay",
        )
        _require(replayed == dict(prior_capture), "post-restart replay changed the canonical result")
        await anyio.to_thread.run_sync(_revoke, cli, installation)
        blocked = await _call(
            session,
            "evidence_capture",
            {
                "input": {
                    "source_native_id": AFTER_REVOKE_SOURCE_ID,
                    "media_type": "text/markdown",
                    "text": "must not settle",
                },
                "idempotency_key": f"{CAPTURE_KEY}-after-revoke",
            },
        )
        _blocked(blocked, "capture after revoke", "evidence_capture")


def _read_only(database: Path) -> sqlite3.Connection:
    """Open a stopped writer's database for inspection, with no write, lock or side file.

    ``mode=ro`` alone still creates the ``-wal`` and ``-shm`` files of a WAL
    database, so ``immutable=1`` is added: SQLite then reads the main file alone and
    takes no lock.  That is sound only once its writer has stopped cleanly and
    checkpointed, so a ``-wal`` or ``-journal`` still present, whose content an
    immutable read would skip, is refused rather than read around.
    """
    _require(
        not any(os.path.lexists(f"{database}{suffix}") for suffix in ("-wal", "-journal")),
        "the workspace database was not closed cleanly before inspection",
    )
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _stage_source(
    service: Path, workspace: Path, installation: Path
) -> dict[str, Any]:
    source = workspace.parent / "staged-source.txt"
    source.write_text(f"staged import {TOKEN}\n", encoding="utf-8")
    completed = shared._run(
        [
            str(service),
            "--workspace",
            str(workspace),
            "--installation-state",
            str(installation),
            "--capture-source",
            str(source),
            "--source-id",
            STAGED_SOURCE_ID,
            "--media-type",
            "text/plain",
        ]
    )
    shared._require_status(completed, 0, "trusted source staging")
    captured = shared._document(completed.stdout, "trusted source staging")
    _require(captured.get("status") == "captured", "trusted source staging did not capture")
    with contextlib.closing(_read_only(workspace / "workspace.sqlite")) as connection:
        row = connection.execute(
            "SELECT s.staged_source_ref, s.source_kind, s.declared_checksum, "
            "s.content_length_bytes, s.media_type, s.source_version "
            "FROM omnivia_staged_sources s "
            "JOIN omnivia_evidence_artifacts e "
            "ON e.workspace_id = s.workspace_id "
            "AND e.staged_source_ref = s.staged_source_ref "
            "WHERE e.source_native_id = ? AND s.staging_outcome = 'verified'",
            (STAGED_SOURCE_ID,),
        ).fetchone()
    _require(row is not None, "trusted source staging omitted its verified descriptor")
    descriptor = {
        "staged_source_ref": row[0],
        "source_kind": row[1],
        "content_checksum": row[2],
        "content_length_bytes": row[3],
        "media_type": row[4],
    }
    if row[5] is not None:
        descriptor["source_version"] = row[5]
    return descriptor


def _imported_artifact(found: Mapping[str, Any], job_id: str, source: Mapping[str, Any]) -> None:
    """Require exactly one artifact of run ``job_id``, over the staged bytes, on one whole page.

    The trusted staging capture is itself evidence of the staged bytes' kind,
    checksum and media type, so only the binding to the run tells the import's
    artifact apart.  The import publishes its own source identity, so the staged
    source id is never assumed.  A page that continues would leave "exactly one"
    unproven for the pages not read.
    """
    evidence = _expect(found.get("evidence"), list, "the imported evidence search omitted its evidence")
    page = found.get("page")
    _require(
        isinstance(page, dict) and page.get("continuation_token") is None,
        "the imported evidence search was not one complete page",
    )
    imported = [
        artifact
        for artifact in evidence
        if isinstance(artifact, dict) and artifact.get("import_run_id") == job_id
    ]
    _require(len(imported) == 1, "the import did not publish exactly one artifact bound to its run")
    published = imported[0].get("source")
    _require(
        isinstance(published, dict)
        and published.get("kind") == source["source_kind"]
        and imported[0].get("content_checksum") == source["content_checksum"]
        and imported[0].get("media_type") == source["media_type"],
        "the imported artifact does not address the staged bytes",
    )


async def _import_journey(
    mcp: Path,
    config: Path,
    cli: Path,
    installation: Path,
    source: Mapping[str, Any],
    checks: dict[str, bool],
) -> str:
    _initialized, session, stack = await _opened_session(mcp, config)
    async with _session_lifetime(stack):
        listed = await session.list_tools()
        _require([tool.name for tool in listed.tools] == list(AUTHORING_TOOLS), "the import session did not expose the authoring inventory")
        arguments = {"input": {"source": dict(source)}, "idempotency_key": IMPORT_KEY}
        started = _success(await _call(session, "import_start", arguments), "import start")
        job = started.get("job")
        identity = job.get("identity") if isinstance(job, dict) else None
        job_id = _expect(
            identity.get("job_id") if isinstance(identity, dict) else None,
            str,
            "import start omitted its job identity",
        )
        checks["import_start"] = True
        replayed = _success(await _call(session, "import_start", arguments), "import replay")
        _require(replayed == started, "import replay changed the canonical result")
        conflicting_source = dict(source)
        conflicting_source["content_length_bytes"] = int(source["content_length_bytes"]) + 1
        conflict = await _call(
            session,
            "import_start",
            {"input": {"source": conflicting_source}, "idempotency_key": IMPORT_KEY},
        )
        _conflict(conflict, "import conflict")
        checks["import_replay_and_conflict"] = True
        observed = _success(await _call(session, "job_get", {"job_id": job_id}), "job get")
        observed_job = observed.get("job")
        _require(isinstance(observed_job, dict) and observed_job.get("state") == "succeeded", "the import job did not succeed")
        events = _success(await _call(session, "job_events", {"job_id": job_id}), "job events")
        event_rows = events.get("events")
        _require(isinstance(event_rows, list) and len(event_rows) >= 2, "the import event stream was incomplete")
        checks["job_observation"] = True
        found = _success(
            await _call(
                session,
                "evidence_search",
                {"query": str(source["source_kind"]), "limit": IMPORTED_EVIDENCE_LIMIT},
            ),
            "import evidence search",
        )
        _imported_artifact(found, job_id, source)
        await anyio.to_thread.run_sync(_revoke, cli, installation)
        _blocked(await _call(session, "job_get", {"job_id": job_id}), "job get after revoke", "job_get")
        _blocked(await _call(session, "job_events", {"job_id": job_id}), "job events after revoke", "job_events")
        _blocked(await _call(session, "import_start", arguments), "import replay after revoke", "import_start")
        return job_id


def _owner_job(cli: Path, installation: Path, workspace_id: str, job_id: str) -> bool:
    completed = shared._run(
        [
            str(cli),
            "--installation-state",
            str(installation),
            "--workspace-id",
            workspace_id,
            "job",
            "get",
            "--input-json",
            json.dumps({"job_id": job_id}),
            "--json",
        ]
    )
    shared._require_status(completed, 0, "owner job observation")
    document = shared._document(completed.stdout, "owner job observation")
    result = document.get("result")
    job = result.get("job") if isinstance(result, dict) else None
    return isinstance(job, dict) and job.get("state") == "succeeded"


def _identity(evidence: Mapping[str, Any]) -> bool | None:
    """Whether the process a descriptor names is that very process, running now.

    ``True`` only when its published pid, start time and boot id all equal what the
    installed runtime reads for that pid now, with the same evidence the Core
    published.  ``False`` when nothing runs at that pid.  ``None`` when the published
    identity is absent or differs, or the evidence cannot be read: the pid may then
    belong to an unrelated process.
    """
    pid, start_time, boot_id = (evidence.get(key) for key in ("pid", "start_time", "boot_id"))
    if type(pid) is not int:
        return None
    current = SystemProcessEvidence().for_pid(pid)
    if current is None:
        return None if _alive(pid) else False
    if (current.pid, current.start_time, current.boot_id) != (pid, start_time, boot_id):
        return None
    return True


def _stop(process: Any, descriptor: Path, replacement: Mapping[str, Any] | None = None) -> None:
    """Stop the started Core, the deliberate replacement, and any other Core still named.

    The last is a managed-local client's replacement for a Core that exited, which a
    failed continuity check must not leave running.  Neither replacement is this
    harness's child, so each is signalled only once ``_identity`` proves it.  One it
    cannot prove is never signalled, and teardown then fails rather than claim a
    clean stop.
    """
    if process.poll() is None:
        shared._stop_pid(process.pid, graceful=True)
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - bounded best-effort cleanup
            process.kill()
    named = _published(descriptor).get("process")
    survivors = [] if replacement is None else [replacement]
    planned = None if replacement is None else replacement.get("pid")
    if isinstance(named, dict) and named.get("pid") not in (None, process.pid, planned):
        survivors.append(named)
    unproved = False
    for evidence in survivors:
        identity = _identity(evidence)
        if identity is None:
            unproved = True
        elif identity:
            shared._stop_replacement(evidence["pid"])
            shared._wait_for_exit(evidence["pid"])
    if unproved:
        raise QualificationError("teardown left a named Core unsignalled: its identity was not proved")


def _after_failure(cleanup: Callable[[], object]) -> None:
    """Clean up on a failure path without ever replacing that failure.

    A cleanup that also fails is reported in fixed words; the original failure is
    the one that propagates.
    """
    try:
        cleanup()
    except Exception:  # noqa: BLE001 - the first failure is the one reported
        print("MCP authoring qualification cleanup also failed", file=sys.stderr)


def _run_empty(
    service: Path, cli: Path, mcp: Path, root: Path
) -> dict[str, Any]:
    workspace, installation, workspace_id = _initialize(service, root)
    endpoint = shared._endpoint(root)
    process = shared._start_service(service, workspace, installation, endpoint)
    descriptor = installation / "runtime" / workspace_id / "service.json"
    replacement: dict[str, Any] | None = None
    checks: dict[str, bool] = {}
    try:
        first = _first_process(descriptor, process)
        config = _configure(cli, installation, workspace_id)
        principal = _principal(config)
        observed = anyio.run(_empty_workspace_journey, mcp, config, principal, checks)
        _require(observed["protocol"] == PROTOCOL_VERSION, "the installed session negotiated another protocol")
        _require(observed["server"] == SERVER_NAME, "another MCP server answered")

        # Only this deliberate kill may change which Core serves the workspace, and
        # it must stop the Core that has served it since startup.
        _require(_serving(descriptor, first, process), "Core exited before the deliberate restart")
        process.kill()
        process.wait(timeout=10)
        _require(_health(cli, installation, workspace_id), "managed-local restart did not recover health")
        replacement_pid = shared._replacement_pid(descriptor)
        _require(replacement_pid != first["pid"], "managed-local restart reused the dead process")
        replacement = _expect(_ready_process(descriptor), dict, "the replacement omitted process evidence")
        _require(replacement.get("pid") == replacement_pid, "another replacement serves the workspace")
        before = _owner_evidence_count(cli, installation, workspace_id, AFTER_REVOKE_SOURCE_ID)
        anyio.run(
            _restart_and_revoke_journey,
            mcp,
            config,
            cli,
            installation,
            observed["capture"],
        )
        checks["core_restart_recovery"] = True
        _require(_serving(descriptor, replacement), "Core exited during the post-restart journey")
        # The owner's own count, outside MCP: the refused capture wrote nothing.
        _require(
            _owner_evidence_count(cli, installation, workspace_id, AFTER_REVOKE_SOURCE_ID) == before,
            "the capture attempted after revocation settled",
        )
        checks["revocation_fail_closed"] = True
        _require(
            _healthy(cli, installation, workspace_id, descriptor, replacement),
            "Core was not healthy after authoring revoke",
        )
        checks["service_healthy"] = True
        result = {**_checked(checks, EMPTY_CHECKS), "tools": list(observed["tools"])}
    except BaseException:
        _after_failure(lambda: _stop(process, descriptor, replacement))
        raise
    _stop(process, descriptor, replacement)
    return result


def _run_import(
    service: Path, cli: Path, mcp: Path, root: Path
) -> dict[str, bool]:
    workspace, installation, workspace_id = _initialize(service, root)
    checks: dict[str, bool] = {}
    source = _stage_source(service, workspace, installation)
    checks["trusted_staging"] = True
    endpoint = shared._endpoint(root)
    process = shared._start_service(service, workspace, installation, endpoint)
    descriptor = installation / "runtime" / workspace_id / "service.json"
    try:
        first = _first_process(descriptor, process)
        config = _configure(cli, installation, workspace_id)
        job_id = anyio.run(_import_journey, mcp, config, cli, installation, source, checks)
        _require(_serving(descriptor, first, process), "Core exited during the import journey")
        _require(_owner_job(cli, installation, workspace_id, job_id), "owner observation did not survive MCP revocation")
        checks["revocation_preserved_job"] = True
        _require(
            _healthy(cli, installation, workspace_id, descriptor, first, process),
            "Core was not healthy after import revoke",
        )
        checks["service_healthy"] = True
        result = _checked(checks, IMPORT_CHECKS)
    except BaseException:
        _after_failure(lambda: _stop(process, descriptor))
        raise
    _stop(process, descriptor)
    return result


def run(output: Path) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise QualificationError("the qualification directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    service = shared._console("omnivia-core-service")
    cli = shared._console("omnivia")
    mcp = shared._console("omnivia-core-mcp")
    temporary_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    temporary = tempfile.TemporaryDirectory(
        prefix="omnivia-mcp-authoring-qualification-", dir=temporary_parent
    )
    try:
        root = Path(temporary.name)
        empty = _run_empty(service, cli, mcp, root / "empty")
        imported = _run_import(service, cli, mcp, root / "import")
    except BaseException:
        _after_failure(temporary.cleanup)
        raise
    temporary.cleanup()
    tools = empty.pop("tools")
    return {
        "format": "omnivia.mcp-authoring-qualification.v1",
        "verdict": "pass",
        "profile": "authoring",
        "protocol_version": PROTOCOL_VERSION,
        "tool_count": len(tools),
        "tools": tools,
        "sdk_versions": {
            "mcp": importlib.metadata.version("mcp"),
            "mcp-types": importlib.metadata.version("mcp-types"),
        },
        "environment": {
            "system": platform.system().lower(),
            "machine": platform.machine().lower(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "journeys": {"empty_workspace": empty, "import": imported},
        "redaction": {
            "credentials_recorded": False,
            "private_paths_recorded": False,
            "private_identifiers_recorded": False,
            "submitted_content_recorded": False,
            "prompts_or_transcripts_recorded": False,
            "endpoints_or_processes_recorded": False,
            "stdio_recorded": False,
            "model_responses_recorded": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = run(arguments.output)
    except QualificationError as error:
        print(f"MCP authoring qualification failed: {error}", file=sys.stderr)
        return 1
    path = arguments.output / RECORD_FILE
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
