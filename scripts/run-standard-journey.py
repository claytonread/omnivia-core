#!/usr/bin/env python3
"""Run the V06-7 standalone Standard-profile journey.

The caller supplies only an evidence directory.  This program finds the three
installed console scripts beside its interpreter, creates a temporary workspace,
starts the real service, writes and approves one source-cited record through the
CLI, then reads it and builds a Context Pack through the installed MCP server.
It finally kills the first service, proves managed-local crash recovery, and
stops the replacement.  Only a redacted result transcript is retained.

The program intentionally imports no OmniVia package.  Running from an isolated
virtual environment therefore proves the public executables and their wire
contracts, rather than a source checkout or an in-process test seam.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

TIMEOUT_SECONDS: Final = 60
QUERY_TOKEN: Final = "omnivia-standalone-journey-token"
PRINCIPAL: Final = "local-user"

#: `whoami /user` reports the SID in this form, mixed into a CSV row.
_SID_RE: Final = re.compile(r"S-1-[0-9-]+")

#: Fixed, because the tools that fail here quote the configuration path, the SID
#: and their own environment, and this journey retains only a redacted transcript.
_RESTRICTION_FAILED: Final = "could not restrict the MCP configuration to its owner"

# A module-level platform flag rather than `os.name` at each site: the Windows
# branch has to be exercised from a POSIX host, and a test that mutates `os.name`
# globally changes far more than the restriction under test.
_IS_WINDOWS = os.name == "nt"


class JourneyError(RuntimeError):
    """The installed standalone path failed one of its required assertions."""


def _console(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    # Keep the virtual-environment path. On POSIX its Python is often a symlink;
    # resolving it would jump to the base interpreter and look for entry points
    # beside the wrong executable.
    beside = Path(sys.executable).parent / f"{name}{suffix}"
    found = shutil.which(name)
    path = beside if beside.is_file() else None if found is None else Path(found)
    if path is None or not path.is_file():
        raise JourneyError(f"the installed {name} executable is absent")
    return path


def _run(
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: int = TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _document(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise JourneyError(f"{label} did not emit one JSON document") from error
    if not isinstance(value, dict):
        raise JourneyError(f"{label} emitted a non-object document")
    return value


def _require_status(
    completed: subprocess.CompletedProcess[str], status: int, label: str
) -> None:
    if completed.returncode != status:
        raise JourneyError(
            f"{label} exited {completed.returncode}, expected {status}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )


def _success(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    _require_status(completed, 0, label)
    if completed.stderr:
        raise JourneyError(f"{label} wrote an unexpected diagnostic")
    envelope = _document(completed.stdout, label)
    result = envelope.get("result")
    if not isinstance(result, dict) or "error" in envelope:
        raise JourneyError(f"{label} did not return a success envelope")
    return result


def _probe_success(
    completed: subprocess.CompletedProcess[str], label: str
) -> dict[str, Any]:
    """One successful service-probe document; probes are not envelopes."""
    _require_status(completed, 0, label)
    if completed.stderr:
        raise JourneyError(f"{label} wrote an unexpected diagnostic")
    result = _document(completed.stdout, label)
    if not isinstance(result.get("status"), str):
        raise JourneyError(f"{label} omitted its status")
    return result


def _identity(record: Mapping[str, Any], label: str) -> tuple[str, str]:
    provenance = record.get("provenance")
    identity = provenance.get("identity") if isinstance(provenance, dict) else None
    if not isinstance(identity, dict):
        raise JourneyError(f"{label} omitted the record identity")
    record_id, version = identity.get("record_id"), identity.get("version")
    if not isinstance(record_id, str) or not isinstance(version, str):
        raise JourneyError(f"{label} returned an invalid record identity")
    return record_id, version


def _endpoint(directory: Path) -> str:
    if os.name == "nt":
        return f"pipe://omnivia-standard-{uuid.uuid4().hex}"
    return f"unix://{directory / 'core.sock'}"


def _start_service(
    executable: Path, workspace: Path, installation: Path, endpoint: str
) -> subprocess.Popen[str]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [
            str(executable),
            "--workspace",
            str(workspace),
            "--installation-state",
            str(installation),
            "--endpoint",
            endpoint,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
        creationflags=flags,
    )


def _stop_pid(pid: int, *, graceful: bool) -> None:
    try:
        if os.name == "nt" and graceful:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        elif graceful:
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return


def _wait_for_exit(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)


def _wait_for_descriptor(path: Path, process: subprocess.Popen[str]) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file():
            document = _document(path.read_text(encoding="utf-8"), "service descriptor")
            if document.get("ready") is True:
                return document
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise JourneyError(
                f"the service exited before readiness; stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.05)
    raise JourneyError("the service did not publish readiness before the deadline")


def _cli(
    executable: Path,
    installation: Path,
    workspace_id: str,
    path: Sequence[str],
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    record_version: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        str(executable),
        "--installation-state",
        str(installation),
        "--workspace-id",
        workspace_id,
        *path,
    ]
    if payload is not None:
        arguments.extend(
            ["--input-json", json.dumps(payload, sort_keys=True, separators=(",", ":"))]
        )
    if path[0] != "service":
        arguments.extend(["--principal", PRINCIPAL])
    if idempotency_key is not None:
        arguments.extend(["--idempotency-key", idempotency_key])
    if record_version is not None:
        arguments.extend(["--record-version", record_version])
    arguments.append("--json")
    return _run(arguments)


def _system32(program: str) -> str:
    """An absolute path to a Windows system tool.

    Resolving through `PATH` would let any directory earlier on it supply the
    program that sets the configuration's ACL, which is the wrong way round for a
    restriction.  `SystemRoot` is where these live and is not always `C:\\Windows`.
    """
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", program))


def _windows_owner_only(path: Path) -> bool:
    """Reduce `path` to an owner-only DACL, reporting whether every step held.

    `os.chmod` is close to a no-op on Windows -- it toggles the read-only
    attribute and nothing else -- so the written configuration carries whatever
    the inherited DACL grants, which on a shared machine is every local user, and
    the installed MCP server refuses to read it.

    The three invocations mirror this repository's runtime implementation.
    `/setowner` first, because the reader compares the object's owner against its
    own `TokenUser` and ownership comes from the token, not the DACL: an elevated
    administrator creates objects owned by `BUILTIN\\Administrators`.  `/reset`
    then drops the *explicit* entries, which `/inheritance:r` does not touch.
    `/grant:r` with `/inheritance:r` drops the inherited ones, marks the DACL
    protected so a permissive parent cannot re-supply them, and leaves one allow
    ACE naming this process's own SID -- the SID `whoami /user` reports, not an
    account name, which is a second lookup that can resolve to another principal.

    Short-circuits at the first failing step: a partly applied sequence is not a
    restriction, and the caller fails closed on the `False`.
    """
    identity = _run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], timeout=15
    )
    found = _SID_RE.search(identity.stdout) if identity.returncode == 0 else None
    if found is None:
        return False
    sid = found.group()
    return all(
        _run(
            [_system32("icacls.exe"), str(path), *arguments, "/q"], timeout=60
        ).returncode
        == 0
        for arguments in (
            ("/setowner", f"*{sid}"),
            ("/reset",),
            ("/inheritance:r", "/grant:r", f"*{sid}:F"),
        )
    )


def _restrict(path: Path) -> None:
    """Owner-only access, in whatever form this platform has one.

    Fails closed.  A configuration naming the installation state and readable by
    every local user is worse than no journey at all, and the MCP server would
    refuse it anyway.
    """
    if not _IS_WINDOWS:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return
    # Suppressed rather than caught and re-raised: raising inside a handler keeps
    # the original on `__cause__`/`__context__`, and its text quotes the
    # configuration path, the SID and the tool's own command line, which every
    # traceback renderer then prints.  The raise below runs with the handler
    # already unwound, so the fixed message is the whole of the failure.
    restricted = False
    with contextlib.suppress(OSError, subprocess.SubprocessError, UnicodeError):
        restricted = _windows_owner_only(path)
    if not restricted:
        raise JourneyError(_RESTRICTION_FAILED)


def _write_mcp_configuration(path: Path, installation: Path, workspace_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "omnivia.mcp-config.v1",
                "principal_id": PRINCIPAL,
                "allowed_workspace_ids": [workspace_id],
                "default_workspace_id": workspace_id,
                "allowed_purposes": [
                    "workspace_inspection",
                    "knowledge_retrieval",
                ],
                "mutation_enabled": False,
                "service_mode": "managed_local",
                "installation_state": str(installation),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _restrict(path)


def _exception_leaves(error: BaseException) -> list[BaseException]:
    """Every non-group exception, flattening nested BaseExceptionGroup leaves."""
    leaves: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        else:
            leaves.append(current)
    return leaves


def _exception_class_names(error: BaseException) -> list[str]:
    """Sorted, unique class names, flattening nested BaseExceptionGroup leaves."""
    return sorted({type(leaf).__name__ for leaf in _exception_leaves(error)})


def _mcp_error_codes(error: BaseException) -> list[int]:
    """Sorted, unique JSON-RPC codes from MCPError leaves.

    Only the numeric code is read.  The MCP message and data carry server text
    and are never inspected, so the reported failure stays free of workspace
    content, paths and credentials.
    """
    codes: set[int] = set()
    for leaf in _exception_leaves(error):
        if isinstance(leaf, MCPError) and isinstance(leaf.code, int):
            codes.add(int(leaf.code))
    return sorted(codes)


def _mcp_failure_message(stage: str, error: BaseException, server_diagnostic: bool) -> str:
    classes = _exception_class_names(error)
    codes = _mcp_error_codes(error)
    return (
        f"MCP standalone session did not complete: stage={stage} "
        f"errors={classes} codes={codes} "
        f"server_diagnostic={'true' if server_diagnostic else 'false'}"
    )


async def _mcp_session(
    executable: Path, config: Path, diagnostic: TextIO, stage: list[str]
) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=str(executable), args=["--config", str(config)]
    )
    stage[0] = "transport_entry"
    async with contextlib.AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(parameters, errlog=diagnostic)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        stage[0] = "initialize"
        initialized = await session.initialize()
        stage[0] = "list_tools"
        listed = await session.list_tools()
        stage[0] = "knowledge_search"
        knowledge = await session.call_tool(
            "knowledge_search", {"query": QUERY_TOKEN}
        )
        stage[0] = "context_pack_build"
        context = await session.call_tool(
            "context_pack_build",
            {
                "query": QUERY_TOKEN,
                "mode": "deterministic_view",
                "token_budget": 4000,
            },
        )
        stage[0] = "shutdown"
    return {
        "server": initialized.server_info.name,
        "tools": [tool.model_dump(mode="json") for tool in listed.tools],
        "knowledge_search": knowledge.model_dump(mode="json"),
        "context_pack_build": context.model_dump(mode="json"),
    }


def _mcp_journey(executable: Path, config: Path) -> dict[str, Any]:
    stage: list[str] = ["transport_entry"]
    with tempfile.TemporaryDirectory(prefix="omnivia-mcp-diagnostic-") as diagnostic_dir:
        diagnostic_path = Path(diagnostic_dir) / "mcp-server-stderr.txt"
        try:
            with open(diagnostic_path, "w", encoding="utf-8") as diagnostic:
                observed = anyio.run(_mcp_session, executable, config, diagnostic, stage)
        except Exception as error:
            server_diagnostic = (
                diagnostic_path.is_file() and diagnostic_path.stat().st_size > 0
            )
            raise JourneyError(
                _mcp_failure_message(stage[0], error, server_diagnostic)
            ) from error
    tools = observed.get("tools")
    if not isinstance(tools, list) or len(tools) != 6:
        raise JourneyError("MCP did not advertise the accepted six-tool manifest")
    called: dict[str, Mapping[str, Any]] = {}
    for name in ("knowledge_search", "context_pack_build"):
        result = observed.get(name)
        if not isinstance(result, dict) or result.get("is_error") is True:
            raise JourneyError(f"MCP {name} did not return a success")
        structured = result.get("structured_content")
        if not isinstance(structured, dict):
            raise JourneyError(f"MCP {name} omitted structuredContent")
        called[name] = structured
    records = called["knowledge_search"].get("records")
    if not isinstance(records, list) or not records:
        raise JourneyError("MCP knowledge search did not return the approved record")
    pack = called["context_pack_build"]
    if not isinstance(pack.get("records"), list) or not pack["records"]:
        raise JourneyError("MCP Context Pack did not contain an approved record")
    return {
        "server": observed["server"],
        "tool_count": len(tools),
        "knowledge_records": len(records),
        "context_records": len(pack["records"]),
        "stdio_session_completed": True,
    }


def _replacement_pid(descriptor: Path) -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if descriptor.is_file():
            document = _document(descriptor.read_text(encoding="utf-8"), "replacement descriptor")
            process = document.get("process")
            pid = process.get("pid") if isinstance(process, dict) else None
            if isinstance(pid, int) and document.get("ready") is True:
                return pid
        time.sleep(0.05)
    raise JourneyError("managed-local recovery did not publish a replacement service")


def run(output: Path) -> dict[str, Any]:
    service = _console("omnivia-core-service")
    cli = _console("omnivia")
    mcp = _console("omnivia-core-mcp")
    if output.exists() and any(output.iterdir()):
        raise JourneyError("the evidence directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)

    # Unix-domain endpoints have a small encoded-path ceiling. macOS's ambient
    # TMPDIR is deeply nested, so use the stable short alias when it exists.
    temporary_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix="omnivia-standard-journey-", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        installation = root / "installation-state"
        endpoint = _endpoint(root)

        initialized = _run(
            [
                str(service),
                "--workspace",
                str(workspace),
                "--installation-state",
                str(installation),
                "--init",
            ]
        )
        _require_status(initialized, 0, "workspace initialization")
        init_result = _document(initialized.stdout, "workspace initialization")
        initialized_workspace = init_result.get("workspace")
        workspace_id = (
            initialized_workspace.get("workspace_id")
            if isinstance(initialized_workspace, dict)
            else None
        )
        if not isinstance(workspace_id, str):
            raise JourneyError("workspace initialization omitted its identity")

        source = root / "standalone-source.txt"
        source.write_text(
            f"This source proves the installed Standard journey: {QUERY_TOKEN}\n",
            encoding="utf-8",
        )
        captured = _run(
            [
                str(service),
                "--workspace",
                str(workspace),
                "--installation-state",
                str(installation),
                "--capture-source",
                str(source),
                "--source-id",
                "standalone-source-1",
                "--media-type",
                "text/plain",
            ]
        )
        _require_status(captured, 0, "local source capture")
        if captured.stderr:
            raise JourneyError("local source capture wrote an unexpected diagnostic")
        capture_result = _document(captured.stdout, "local source capture")
        if (
            capture_result.get("status") != "captured"
            or not isinstance(capture_result.get("evidence_id"), str)
            or not isinstance(capture_result.get("content_digest"), str)
        ):
            raise JourneyError("local source capture did not preserve evidence")

        descriptor = installation / "runtime" / workspace_id / "service.json"
        process = _start_service(service, workspace, installation, endpoint)
        replacement_pid: int | None = None
        try:
            first_descriptor = _wait_for_descriptor(descriptor, process)
            protocol_version = first_descriptor.get("protocol_version")
            server_version = first_descriptor.get("server_version")
            api_versions = first_descriptor.get("supported_api_versions")
            workspace_contract_version = first_descriptor.get(
                "workspace_format_version"
            )
            if (
                not isinstance(protocol_version, str)
                or not isinstance(server_version, str)
                or not isinstance(api_versions, dict)
                or not isinstance(api_versions.get("minimum"), str)
                or not isinstance(api_versions.get("maximum"), str)
                or not isinstance(workspace_contract_version, str)
            ):
                raise JourneyError("the service descriptor omitted version evidence")
            health = _probe_success(
                _cli(cli, installation, workspace_id, ("service", "health")),
                "CLI health",
            )

            failed_start = _run(
                [
                    str(service),
                    "--workspace",
                    str(workspace),
                    "--installation-state",
                    str(installation),
                    "--check-only",
                ]
            )
            _require_status(failed_start, 1, "second-owner failed startup")
            failed_report = _document(failed_start.stdout, "failed startup")
            released = failed_report.get("released")
            if (
                failed_report.get("state") != "failed"
                or not isinstance(released, list)
                or failed_report.get("reason")
                != "another service holds the lifetime storage lock"
            ):
                raise JourneyError(
                    f"failed startup did not report bounded cleanup: {failed_report!r}"
                )

            created = _success(
                _cli(
                    cli,
                    installation,
                    workspace_id,
                    ("memory", "create"),
                    payload={
                        "assertion": {
                            "actor_id": "standalone-owner",
                            "actor_kind": "user",
                            "actor_role": "owner",
                            "asserted_at": "2026-08-13T00:00:00Z",
                            "evidence": [
                                {
                                    "source": {
                                        "kind": "document",
                                        "source_id": "standalone-source-1",
                                    }
                                }
                            ],
                        },
                        "content": {"fact": QUERY_TOKEN},
                        "domain_scope": "standalone.qualification",
                        "evidence_disposition": "available",
                        "record_type": "memory.fact",
                        "sources": [
                            {"kind": "document", "source_id": "standalone-source-1"}
                        ],
                    },
                    idempotency_key="standard-memory-create-1",
                ),
                "CLI memory create",
            )
            record = created.get("record")
            if not isinstance(record, dict):
                raise JourneyError("memory.create omitted its record")
            record_id, record_version = _identity(record, "memory.create")

            proposed = _success(
                _cli(
                    cli,
                    installation,
                    workspace_id,
                    ("governance", "propose"),
                    payload={
                        "record_id": record_id,
                        "rationale": {"reason_code": "standalone_proposal_ready"},
                    },
                    idempotency_key="standard-candidate-propose-1",
                    record_version=record_version,
                ),
                "CLI knowledge propose",
            )
            proposed_record = proposed.get("updated_record")
            if not isinstance(proposed_record, dict):
                raise JourneyError("knowledge.propose omitted its updated record")
            proposed_id, proposed_version = _identity(
                proposed_record, "knowledge.propose"
            )
            if proposed_id != record_id:
                raise JourneyError("proposal changed the record identity")

            approved = _success(
                _cli(
                    cli,
                    installation,
                    workspace_id,
                    ("governance", "approve"),
                    payload={
                        "record_id": record_id,
                        "rationale": {"reason_code": "standalone_review_passed"},
                    },
                    idempotency_key="standard-candidate-approve-1",
                    record_version=proposed_version,
                ),
                "CLI candidate approve",
            )
            updated = approved.get("updated_record")
            if not isinstance(updated, dict):
                raise JourneyError("candidate.approve omitted its updated record")
            approved_id, _approved_version = _identity(updated, "candidate.approve")
            if approved_id != record_id:
                raise JourneyError("approval changed the record identity")

            searched = _success(
                _cli(
                    cli,
                    installation,
                    workspace_id,
                    ("knowledge", "search"),
                    payload={"query": QUERY_TOKEN},
                ),
                "CLI knowledge search",
            )
            records = searched.get("records")
            if not isinstance(records, list) or not records:
                raise JourneyError("CLI knowledge search did not find the approved record")

            config = root / "omnivia-mcp.json"
            _write_mcp_configuration(config, installation, workspace_id)
            mcp_result = _mcp_journey(mcp, config)

            first_pid = first_descriptor.get("process", {}).get("pid")
            if not isinstance(first_pid, int):
                raise JourneyError("the first descriptor omitted process evidence")
            process.kill()
            process.wait(timeout=10)
            recovered = _probe_success(
                _cli(cli, installation, workspace_id, ("service", "health")),
                "managed-local crash recovery",
            )
            replacement_pid = _replacement_pid(descriptor)
            if replacement_pid == first_pid:
                raise JourneyError("crash recovery reused the dead service process")

            return {
                "format": "omnivia.standard-journey-result.v1",
                "verdict": "pass",
                "profile": [
                    "omnivia-core",
                    "omnivia-core-runtime",
                    "omnivia-core-client",
                    "omnivia-core-mcp",
                    "omnivia-core-cli",
                ],
                "workspace": {
                    "initialized": True,
                    "format": (
                        initialized_workspace.get("workspace_format_version")
                        if isinstance(initialized_workspace, dict)
                        else None
                    ),
                    "identity_preserved": approved_id == record_id,
                },
                "versions": {
                    "server": server_version,
                    "protocol": protocol_version,
                    "api": {
                        "minimum": api_versions["minimum"],
                        "maximum": api_versions["maximum"],
                    },
                    "workspace_contract": workspace_contract_version,
                },
                "service": {
                    "health": health.get("status"),
                    "failed_start_cleanup": failed_report.get("released"),
                    "crash_recovery": recovered.get("status"),
                    "replacement_process": True,
                },
                "cli": {
                    "source_captured": capture_result.get("status") == "captured",
                    "source_reference_preserved": True,
                    "record_created": True,
                    "record_proposed": proposed_id == record_id,
                    "record_reviewed_and_approved": True,
                    "authorized_search_records": len(records),
                },
                "mcp": mcp_result,
                "secrets_recorded": False,
                "private_paths_recorded": False,
            }
        finally:
            if process.poll() is None:
                _stop_pid(process.pid, graceful=True)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if replacement_pid is not None:
                _stop_pid(replacement_pid, graceful=True)
                _wait_for_exit(replacement_pid)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.output)
    except JourneyError as error:
        print(f"standalone journey failed: {error}", file=sys.stderr)
        return 1
    path = args.output / "standalone-journey-result.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
