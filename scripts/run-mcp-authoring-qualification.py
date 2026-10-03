#!/usr/bin/env python3
"""Qualify MCP authoring from an isolated installed-wheel environment.

The program imports no OmniVia package.  Product behaviour is reached only
through the installed ``omnivia-core-service``, ``omnivia`` and
``omnivia-core-mcp`` executables and the official MCP SDK.  It retains one
closed, redacted JSON record: no path, principal, workspace, job, evidence,
record, credential, endpoint, process, prompt, transcript, stdout, stderr or
submitted content is copied into it.
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
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import anyio
import mcp_types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
STAGED_SOURCE_ID: Final = f"{TOKEN}-staged-source"
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


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationError(message)


def _success(called: Mapping[str, Any], label: str) -> dict[str, Any]:
    _require(called.get("is_error") is False, f"{label} did not succeed")
    answer = called.get("structured_content")
    _require(isinstance(answer, dict), f"{label} omitted its structured result")
    return dict(answer)


def _conflict(called: Mapping[str, Any], label: str) -> None:
    _require(called.get("is_error") is True, f"{label} was not refused")
    _require(called.get("structured_content") is None, f"{label} returned data")
    content = called.get("content")
    text = content[0].get("text") if isinstance(content, list) and content else None
    _require(isinstance(text, str), f"{label} omitted its refusal")
    _require('"code":"idempotency_conflict"' in text.replace(" ", ""), f"{label} was not an idempotency conflict")


def _blocked(called: Mapping[str, Any], label: str) -> None:
    _require(called.get("is_error") is True, f"{label} succeeded after revocation")
    _require(called.get("structured_content") is None, f"{label} returned data after revocation")
    content = called.get("content")
    text = content[0].get("text") if isinstance(content, list) and content else None
    _require(isinstance(text, str) and "could not be called" in text, f"{label} did not fail closed")


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
    workspace_id = (
        workspace_document.get("workspace_id")
        if isinstance(workspace_document, dict)
        else None
    )
    _require(isinstance(workspace_id, str), "workspace initialization omitted its identity")
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
    arguments = entry.get("args")
    _require(
        isinstance(arguments, list)
        and len(arguments) == 2
        and arguments[0] == "--config"
        and isinstance(arguments[1], str),
        "MCP authoring configure omitted its configuration",
    )
    return Path(arguments[1])


def _principal(config: Path) -> str:
    document = shared._document(config.read_text(encoding="utf-8"), "MCP configuration")
    principal = document.get("principal_id")
    _require(isinstance(principal, str) and principal, "MCP configuration omitted its principal")
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
    return result.get("status") == "pass"


def _parameters(mcp: Path, config: Path) -> StdioServerParameters:
    return StdioServerParameters(command=str(mcp), args=["--config", str(config)])


async def _opened_session(
    mcp: Path, config: Path
) -> tuple[Any, ClientSession, contextlib.AsyncExitStack]:
    stack = contextlib.AsyncExitStack()
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
            )
        ),
        types.InitializeResult,
    )
    session.adopt(initialized)
    await session.send_notification(types.InitializedNotification())
    return initialized, session, stack


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
    mcp: Path, config: Path, principal: str
) -> dict[str, Any]:
    initialized, session, stack = await _opened_session(mcp, config)
    async with stack:
        listed = await session.list_tools()
        tools = [tool.name for tool in listed.tools]
        _require(tools == list(AUTHORING_TOOLS), "the authoring inventory was not the accepted eighteen")
        empty_evidence = _success(await _call(session, "evidence_search", {"query": TOKEN}), "empty evidence search")
        empty_memory = _success(await _call(session, "memory_search", {"query": TOKEN}), "empty memory search")
        empty_knowledge = _success(await _call(session, "knowledge_search", {"query": TOKEN}), "empty knowledge search")
        _require(empty_evidence.get("evidence") == [], "the authoring workspace contained evidence")
        _require(empty_memory.get("records") == [], "the authoring workspace contained memory")
        _require(empty_knowledge.get("records") == [], "the authoring workspace contained knowledge")

        capture_arguments = {
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

        memory_arguments = {
            "input": _memory_input(principal, FACT),
            "idempotency_key": MEMORY_KEY,
        }
        memory = _success(await _call(session, "memory_create", memory_arguments), "memory create")
        record = memory.get("record")
        _require(isinstance(record, dict) and record.get("authority_level") == "proposed", "memory create did not produce a proposal")
        default_memory = _success(await _call(session, "memory_search", {"query": TOKEN}), "default memory search")
        candidates = _success(await _call(session, "memory_search", {"query": TOKEN, "view": "candidates"}), "candidate memory search")
        _require(default_memory.get("records") == [], "a proposed record reached the default view")
        candidate_records = candidates.get("records")
        _require(isinstance(candidate_records, list) and len(candidate_records) == 1, "the proposed record was not visible in the candidate view")

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
    async with stack:
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
                    "source_native_id": f"{SOURCE_ID}-after-revoke",
                    "media_type": "text/markdown",
                    "text": "must not settle",
                },
                "idempotency_key": f"{CAPTURE_KEY}-after-revoke",
            },
        )
        _blocked(blocked, "capture after revoke")


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
    with contextlib.closing(
        sqlite3.connect(workspace / "workspace.sqlite")
    ) as connection:
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


async def _import_journey(
    mcp: Path,
    config: Path,
    cli: Path,
    installation: Path,
    source: Mapping[str, Any],
) -> str:
    _initialized, session, stack = await _opened_session(mcp, config)
    async with stack:
        listed = await session.list_tools()
        _require([tool.name for tool in listed.tools] == list(AUTHORING_TOOLS), "the import session did not expose the authoring inventory")
        arguments = {"input": {"source": dict(source)}, "idempotency_key": IMPORT_KEY}
        started = _success(await _call(session, "import_start", arguments), "import start")
        job = started.get("job")
        identity = job.get("identity") if isinstance(job, dict) else None
        job_id = identity.get("job_id") if isinstance(identity, dict) else None
        _require(isinstance(job_id, str), "import start omitted its job identity")
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
        observed = _success(await _call(session, "job_get", {"job_id": job_id}), "job get")
        observed_job = observed.get("job")
        _require(isinstance(observed_job, dict) and observed_job.get("state") == "succeeded", "the import job did not succeed")
        events = _success(await _call(session, "job_events", {"job_id": job_id}), "job events")
        event_rows = events.get("events")
        _require(isinstance(event_rows, list) and len(event_rows) >= 2, "the import event stream was incomplete")
        evidence = _success(await _call(session, "evidence_search", {"query": str(source["source_kind"])}), "import evidence search").get("evidence")
        _require(isinstance(evidence, list) and evidence, "the import evidence was not searchable")
        await anyio.to_thread.run_sync(_revoke, cli, installation)
        _blocked(await _call(session, "job_get", {"job_id": job_id}), "job get after revoke")
        _blocked(await _call(session, "job_events", {"job_id": job_id}), "job events after revoke")
        _blocked(await _call(session, "import_start", arguments), "import replay after revoke")
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


def _stop(process: Any, replacement_pid: int | None = None) -> None:
    if process.poll() is None:
        shared._stop_pid(process.pid, graceful=True)
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - bounded best-effort cleanup
            process.kill()
    if replacement_pid is not None:
        shared._stop_replacement(replacement_pid)
        shared._wait_for_exit(replacement_pid)


def _run_empty(
    service: Path, cli: Path, mcp: Path, root: Path
) -> dict[str, bool | str | list[str]]:
    workspace, installation, workspace_id = _initialize(service, root)
    endpoint = shared._endpoint(root)
    process = shared._start_service(service, workspace, installation, endpoint)
    replacement_pid: int | None = None
    try:
        descriptor = installation / "runtime" / workspace_id / "service.json"
        first = shared._wait_for_descriptor(descriptor, process)
        first_pid = first.get("process", {}).get("pid")
        _require(isinstance(first_pid, int), "the first service omitted process evidence")
        config = _configure(cli, installation, workspace_id)
        principal = _principal(config)
        observed = anyio.run(_empty_workspace_journey, mcp, config, principal)
        _require(observed["protocol"] == PROTOCOL_VERSION, "the installed session negotiated another protocol")
        _require(observed["server"] == SERVER_NAME, "another MCP server answered")

        process.kill()
        process.wait(timeout=10)
        _require(_health(cli, installation, workspace_id), "managed-local restart did not recover health")
        replacement_pid = shared._replacement_pid(descriptor)
        _require(replacement_pid != first_pid, "managed-local restart reused the dead process")
        anyio.run(
            _restart_and_revoke_journey,
            mcp,
            config,
            cli,
            installation,
            observed["capture"],
        )
        _require(_health(cli, installation, workspace_id), "Core was not healthy after authoring revoke")
        return {
            "empty_workspace": True,
            "tool_discovery": True,
            "capture_and_search": True,
            "proposed_memory": True,
            "candidate_visibility": True,
            "replay_and_conflict": True,
            "core_restart_recovery": True,
            "revocation_fail_closed": True,
            "service_healthy": True,
            "tools": list(observed["tools"]),
        }
    finally:
        _stop(process, replacement_pid)


def _run_import(
    service: Path, cli: Path, mcp: Path, root: Path
) -> dict[str, bool]:
    workspace, installation, workspace_id = _initialize(service, root)
    source = _stage_source(service, workspace, installation)
    endpoint = shared._endpoint(root)
    process = shared._start_service(service, workspace, installation, endpoint)
    try:
        descriptor = installation / "runtime" / workspace_id / "service.json"
        shared._wait_for_descriptor(descriptor, process)
        config = _configure(cli, installation, workspace_id)
        job_id = anyio.run(_import_journey, mcp, config, cli, installation, source)
        _require(_owner_job(cli, installation, workspace_id, job_id), "owner observation did not survive MCP revocation")
        _require(_health(cli, installation, workspace_id), "Core was not healthy after import revoke")
        return {
            "trusted_staging": True,
            "import_start": True,
            "job_observation": True,
            "import_replay_and_conflict": True,
            "revocation_preserved_job": True,
            "service_healthy": True,
        }
    finally:
        _stop(process)


def run(output: Path) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise QualificationError("the qualification directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    service = shared._console("omnivia-core-service")
    cli = shared._console("omnivia")
    mcp = shared._console("omnivia-core-mcp")
    temporary_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix="omnivia-mcp-authoring-qualification-", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        empty = _run_empty(service, cli, mcp, root / "empty")
        imported = _run_import(service, cli, mcp, root / "import")
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
