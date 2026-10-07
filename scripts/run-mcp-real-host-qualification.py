#!/usr/bin/env python3
"""Qualify installed OmniVia Core MCP authoring through a real supported host.

The harness imports no OmniVia package.  It validates and installs a clean
candidate offline, isolates the selected host's configuration and credentials,
observes MCP JSON-RPC through a transparent fail-closed proxy, independently
checks durable Core state, and retains only the closed redacted result record.
Every public failure is a stable reason code; command output, model text,
prompts, paths, credentials and private run identifiers never reach the record.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import enum
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import venv
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

SCRIPT_DIR: Final = Path(__file__).resolve().parent
AUTHORING_SCRIPT: Final = SCRIPT_DIR / "run-mcp-authoring-qualification.py"
RECORD_FORMAT: Final = "omnivia.mcp-real-host-qualification.v1"
PROVENANCE_FORMAT: Final = "omnivia.standard-build-provenance.v1"
MANIFEST_FORMAT: Final = "omnivia.standard-release-manifest.v1"
SERVER_KEY: Final = "omnivia-core"
SUPPORTED_SYSTEM: Final = "darwin"
SUPPORTED_MACHINE: Final = "arm64"
OS_PRODUCT: Final = "macOS"
SUPPORTED_OS_VERSION: Final = "27.0"
SUPPORTED_OS_BUILD: Final = "26A428"
FIRST_PARTY: Final = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-client",
    "omnivia-core-cli",
    "omnivia-core-mcp",
)
SDK_PINS: Final = {"mcp": "2.0.0", "mcp-types": "2.0.0"}
HOST_VERSIONS: Final = {"claude-code": "2.1.289", "codex-cli": "0.146.0"}
SCRIPT_PATH: Final = Path(__file__).resolve()
RESTRICTED_TOOL_COUNT: Final = 14
AUTHORING_TOOL_COUNT: Final = 29
QUALIFICATION_TOKEN: Final = "omnivia-real-host-qualification-v1"
DIRECT_SOURCE_ID: Final = f"{QUALIFICATION_TOKEN}-direct-source"
INTERRUPTED_SOURCE_ID: Final = f"{QUALIFICATION_TOKEN}-interrupted-source"
STAGED_SOURCE_ID: Final = f"{QUALIFICATION_TOKEN}-staged-source"
CAPTURE_KEY: Final = f"{QUALIFICATION_TOKEN}-capture-1"
MEMORY_KEY: Final = f"{QUALIFICATION_TOKEN}-memory-1"
IMPORT_KEY: Final = f"{QUALIFICATION_TOKEN}-import-1"
INTERRUPTED_KEY: Final = f"{QUALIFICATION_TOKEN}-interrupted-1"
HOST_TIMEOUT: Final = 300.0
CORE_TIMEOUT: Final = 60.0
SYSTEM_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
MCP_PROTOCOL_VERSION: Final = "2025-06-18"
#: The modern lifecycle (Claude Code 2.1.289, mcp 2.0.0): a successful ``server/discover``
#: replaces ``initialize``.  Both versions are pinned; neither is negotiated.
MCP_MODERN_PROTOCOL_VERSION: Final = "2026-07-28"
MAX_PROTOCOL_OUTPUT_BYTES: Final = 1_048_576
CLAUDE_TOKEN_VARIABLE: Final = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_TOKEN_FILE_BYTES: Final = 1024
_CLAUDE_TOKEN: Final = re.compile(r"[A-Za-z0-9._~+/=-]{16,512}")

#: The gates of requirements §13.I.  Every subcheck is an independently
#: observed boolean; the ledger starts every one closed.
GATES: Final = {
    "i1": ("candidate_installed", "entrypoints_resolved"),
    "i2": ("restricted_configured", "authoring_configured"),
    "i3": (
        "initialize_verified",
        "restricted_tools_exact",
        "authoring_tools_exact",
        "restricted_excluded_tools_absent",
        "restricted_excluded_tool_undispatchable",
        "authoring_excluded_tools_absent",
        "authoring_excluded_tool_undispatchable",
        "decision_evaluate_refused",
        "decision_owner_observed",
    ),
    "i4": (
        "capture_and_search",
        "proposed_memory",
        "default_invisible",
        "candidate_visible",
        "capture_replay_stable",
        "capture_changed_conflict",
        "memory_replay_stable",
        "memory_changed_conflict",
    ),
    "i5": (
        "staged_import",
        "job_observed",
        "import_replay_stable",
        "import_changed_conflict",
        "job_events_paged",
        "job_events_match_owner",
        "imported_evidence_retrieved",
    ),
    "i6": (
        "commit_observed_before_response",
        "host_stopped_before_response",
        "core_restarted_before_replay",
        "same_key_replayed",
        "single_durable_effect",
        "changed_input_conflict",
    ),
    "i7": ("stdout_protocol_only", "host_restart_observed", "core_restart_observed"),
    "i8": (
        "authoring_revoked",
        "mutation_fail_closed",
        "replay_fail_closed",
        "job_reads_fail_closed",
        "owner_job_observed_after_revoke",
        "core_healthy",
    ),
}
#: Owner paging for the import job: one event per page, so its two events span pages.
IMPORT_PAGE_SIZE: Final = 1
MAX_EVENT_PAGES: Final = 64
#: The bound on the imported-evidence search.  The import workspace holds two
#: artifacts, the staging capture and the import's own, so one page is complete.
IMPORTED_EVIDENCE_LIMIT: Final = 10


class ReasonCode(enum.Enum):
    """The closed vocabulary of outcomes; the only diagnostic a run may emit."""

    NONE = "none"
    CANDIDATE_INVALID = "candidate_invalid"
    CANDIDATE_DIRTY = "candidate_dirty"
    WHEEL_DIGEST_MISMATCH = "wheel_digest_mismatch"
    SDK_PIN_MISMATCH = "sdk_pin_mismatch"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    HOST_VERSION_UNSUPPORTED = "host_version_unsupported"
    HOST_BINARY_UNAVAILABLE = "host_binary_unavailable"
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"
    LIVE_RUNNER_UNAVAILABLE = "live_runner_unavailable"
    HOST_TIMEOUT = "host_timeout"
    HOST_OUTPUT_AMBIGUOUS = "host_output_ambiguous"
    INTERRUPTION_BOUNDARY_UNOBSERVABLE = "interruption_boundary_unobservable"
    MODEL_EVIDENCE_REJECTED = "model_evidence_rejected"
    GATE_FAILED = "gate_failed"
    RECORD_INVALID = "record_invalid"
    INSTALL_FAILED = "install_failed"
    ENTRYPOINT_UNRESOLVED = "entrypoint_unresolved"
    PROTOCOL_VIOLATION = "protocol_violation"
    HOST_LAUNCH_FAILED = "host_launch_failed"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"


class QualificationError(Exception):
    """A fail-closed refusal that carries a reason code and no free text."""

    def __init__(self, code: ReasonCode) -> None:
        super().__init__(code.value)
        self.code = code


# --- inventories -----------------------------------------------------------


def _authoring_tools() -> tuple[str, ...]:
    """Read the accepted authoring inventory without executing its module."""
    tree = ast.parse(AUTHORING_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "AUTHORING_TOOLS"
            and node.value is not None
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, tuple) and all(isinstance(name, str) for name in value):
                return value
    raise QualificationError(ReasonCode.RECORD_INVALID)


AUTHORING_TOOLS: Final = _authoring_tools()
#: The authoring profile is the restricted profile plus fifteen additions, in the
#: manifest's exposure order.
RESTRICTED_TOOLS: Final = AUTHORING_TOOLS[:RESTRICTED_TOOL_COUNT]
PROFILE_TOOLS: Final = {"restricted": RESTRICTED_TOOLS, "authoring": AUTHORING_TOOLS}
SAFE_AUXILIARY_TOOLS: Final = frozenset(
    {"workspace_inspect", "evidence_search", "knowledge_search", "memory_search"}
)
#: The forty-four of the seventy-three catalogue operations that the exposure
#: manifest does not admit (the other twenty-nine are the authoring inventory),
#: under their MCP-facing names.  The manifest
#: stays the authority; ``test_the_unexposed_tools_are_exactly_the_catalogue_outside_the_manifest``
#: in ``tests/package_qualification/test_mcp_real_host_qualification.py`` checks
#: this tuple against the catalogue and the manifest, so it cannot drift silently.
UNEXPOSED_TOOLS: Final = tuple(
    operation.replace(".", "_")
    for operation in (
        "analysis.start", "candidate.approve", "candidate.reject", "chat.command",
        "chat.events", "chat.snapshot", "context.priority.set",
        "continuity.checkpoint.append", "continuity.handoff.read",
        "continuity.session.close", "continuity.session.register",
        "decision.definition.disable", "decision.definition.get",
        "decision.definition.list", "decision.definition.publish",
        "decision.model.activate", "decision.model.install", "decision.model.list",
        "decision.model.remove", "decision.outcome.submit",
        "decision.result_use.evaluate", "decision.settings.get",
        "decision.settings.update", "engineering.repository.register",
        "engineering.review.record", "engineering.source.capture.commit",
        "engineering.source.record", "job.cancel", "job.retry", "knowledge.propose",
        "memory.get", "memory.list", "record.supersede", "skills.install",
        "skills.remove", "skills.resolve", "skills.version.deprecate",
        "skills.version.publish", "workflow.control",
        "workflow.inspect", "workflow.review", "workflow.start", "workspace.create",
        "workspace.list",
    )
)
#: Deterministic qualification sentinels for every section-7 category that has
#: no catalogue operation.  These names are deliberately not catalogue entries:
#: they let the harness prove that no host profile exposes or dispatches the
#: underlying administrative capability class.
SECTION7_SENTINELS: Final = {
    "service_lifecycle_discovery": (
        "service_start", "service_stop", "service_health", "service_readiness",
        "service_status", "service_discovery",
    ),
    "bootstrap_workspace_selection": ("bootstrap", "workspace_select"),
    "grants": ("grant_create", "grant_renew", "grant_revoke", "grant_inspect"),
    "filesystem_path_selection": ("filesystem_path_select",),
    "urls": ("url_set",),
    "credentials": ("credential_set",),
    "connector_configuration": ("connector_configure",),
    "administration_configuration": ("administration_configure",),
    "connector_mutation": ("connector_mutate",),
}
SECTION7_TOOLS: Final = tuple(
    tool for sentinels in SECTION7_SENTINELS.values() for tool in sentinels
)
#: Every excluded name each profile must prove undispatchable.  The restricted
#: profile also excludes the fifteen authoring additions it does not expose.
EXCLUDED_TOOLS: Final = {
    "restricted": (*AUTHORING_TOOLS[RESTRICTED_TOOL_COUNT:], *UNEXPOSED_TOOLS, *SECTION7_TOOLS),
    "authoring": (*UNEXPOSED_TOOLS, *SECTION7_TOOLS),
}
#: The one restricted-profile mutation, submitted with no granted capability.  The
#: installed service refuses it with ``capability_not_granted``; the harness proves
#: only that the decision surface stays disabled and creates no decision record.
DECISION_KEY: Final = f"{QUALIFICATION_TOKEN}-decision-1"
MAX_FRAME_BYTES: Final = 1_048_576

# --- candidate, platform and pin validation --------------------------------

_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _mapping(value: object, code: ReasonCode = ReasonCode.CANDIDATE_INVALID) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise QualificationError(code)
    return value


def require_clean_source(provenance: Mapping[str, Any]) -> str:
    """Return the exact clean source revision or refuse."""
    if provenance.get("format") != PROVENANCE_FORMAT:
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    source = _mapping(provenance.get("source"))
    dirty = source.get("dirty")
    if not isinstance(dirty, bool):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    if dirty:
        raise QualificationError(ReasonCode.CANDIDATE_DIRTY)
    revision = source.get("revision")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    return revision


def require_platform(system: object, machine: object) -> None:
    """Accept only darwin/arm64."""
    if system != SUPPORTED_SYSTEM or machine != SUPPORTED_MACHINE:
        raise QualificationError(ReasonCode.PLATFORM_UNSUPPORTED)


def require_sdk_pins(wheels: Sequence[object]) -> None:
    """Require exactly one wheel for each pinned SDK package, at its pin."""
    versions: dict[str, list[object]] = {name: [] for name in SDK_PINS}
    for entry in wheels:
        item = _mapping(entry)
        name = item.get("name")
        if isinstance(name, str) and _normalized(name) in versions:
            versions[_normalized(name)].append(item.get("version"))
    if any(found != [pin] for found, pin in zip(versions.values(), SDK_PINS.values(), strict=True)):
        raise QualificationError(ReasonCode.SDK_PIN_MISMATCH)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_wheels(
    wheels: Sequence[object], candidate: Path
) -> dict[str, tuple[Path, str]]:
    """Verify the whole wheel closure, first-party and third-party alike.

    Each entry must name one plain file directly inside ``wheels/``, whose size
    and SHA-256 equal the manifest's.  The manifest must account for every
    ``.whl`` the directory holds, so nothing outside the closure can be installed.
    Each first-party distribution appears exactly once and is flagged as such.
    """
    first_party = {_normalized(name) for name in FIRST_PARTY}
    verified: dict[str, tuple[Path, str]] = {}
    for entry in wheels:
        item = _mapping(entry)
        name = item.get("name")
        flagged = item.get("first_party")
        if not isinstance(name, str) or type(flagged) is not bool:
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        normalized = _normalized(name)
        if (normalized in first_party) != flagged or normalized in verified:
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        relative = item.get("path")
        expected = item.get("sha256")
        size = item.get("bytes")
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or type(size) is not int
            or size < 0
        ):
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        parts = PurePosixPath(relative).parts
        if len(parts) != 2 or parts[0] != "wheels" or parts[1] in {"", ".", ".."}:
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        path = candidate / "wheels" / parts[1]
        if path.is_symlink() or not path.is_file():
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        if path.stat().st_size != size or not _SHA256.fullmatch(expected):
            raise QualificationError(ReasonCode.WHEEL_DIGEST_MISMATCH)
        actual = _file_digest(path)
        if actual != expected:
            raise QualificationError(ReasonCode.WHEEL_DIGEST_MISMATCH)
        verified[normalized] = (path, actual)
    if not first_party <= set(verified):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    listed = {path.name for path, _ in verified.values()}
    if {path.name for path in (candidate / "wheels").glob("*.whl")} != listed:
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    return verified


def verify_first_party_wheels(
    wheels: Sequence[object], candidate: Path
) -> dict[str, str]:
    """Return each first-party wheel's SHA-256 after verifying the whole closure."""
    verified = _verified_wheels(wheels, candidate)
    return {name: verified[name][1] for name in FIRST_PARTY}


def hashed_requirements(verified: Mapping[str, tuple[Path, str]]) -> str:
    """One hashed requirement per verified wheel, as file URLs so any path is safe.

    ``pip install --require-hashes`` then refuses any distribution the closure does
    not list and any byte that differs from its recorded digest.
    """
    return "".join(
        f"{path.resolve().as_uri()} --hash=sha256:{digest}\n"
        for path, digest in sorted(verified.values(), key=lambda item: item[0].name)
    )


@dataclass(frozen=True)
class Candidate:
    revision: str
    wheels: Mapping[str, str]
    closure_count: int
    closure_sha256: str
    harness_sha256: str


def _closure_binding(wheels: Sequence[object]) -> tuple[int, str]:
    """Return a deterministic count and digest of the normalized manifest closure."""
    normalized: list[dict[str, object]] = []
    for entry in wheels:
        item = _mapping(entry)
        name = item.get("name")
        version = item.get("version")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("bytes")
        first_party = item.get("first_party")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(path, str)
            or not isinstance(digest, str)
            or type(size) is not int
            or type(first_party) is not bool
        ):
            raise QualificationError(ReasonCode.CANDIDATE_INVALID)
        normalized.append(
            {
                "name": _normalized(name),
                "version": version,
                "path": PurePosixPath(path).as_posix(),
                "sha256": digest,
                "bytes": size,
                "first_party": first_party,
            }
        )
    normalized.sort(key=lambda item: (str(item["name"]), str(item["path"])))
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return len(normalized), hashlib.sha256(encoded).hexdigest()


def _json_document(path: Path) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID) from None


def load_candidate(candidate: Path) -> Candidate:
    """Validate a built candidate directory against its own metadata."""
    provenance = _json_document(candidate / "metadata" / "build-provenance.json")
    manifest = _json_document(candidate / "metadata" / "release-manifest.json")
    revision = require_clean_source(provenance)
    closure = _mapping(provenance.get("dependency_resolution"))
    builder_host = _mapping(provenance.get("host"))
    if (
        manifest.get("format") != MANIFEST_FORMAT
        or manifest.get("source_revision") != revision
        or closure.get("exact_closure_verified") is not True
    ):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    require_platform(builder_host.get("system"), builder_host.get("machine"))
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    require_sdk_pins(wheels)
    first_party = verify_first_party_wheels(wheels, candidate)
    closure_count, closure_sha256 = _closure_binding(wheels)
    return Candidate(
        revision,
        first_party,
        closure_count,
        closure_sha256,
        _file_digest(SCRIPT_PATH),
    )


# --- native host commands and configuration --------------------------------


def mcp_server_entry(mcp_executable: Path, core_config: Path) -> dict[str, Any]:
    """The one stdio entry both hosts launch: the installed MCP entry point."""
    return {"command": str(mcp_executable), "args": ["--config", str(core_config)]}


def claude_tool_name(tool: str) -> str:
    return f"mcp__{SERVER_KEY}__{tool}"


def claude_mcp_config(
    entry: Mapping[str, Any], *, redirect: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The Claude MCP document; its environment overrides any inherited profile or token.

    ``redirect`` is the private layout's home and config directory, given only for an
    existing login, whose host process keeps the real profile.  The empty token
    variable always overrides any inherited token.
    """
    environment = {**(redirect or {}), CLAUDE_TOKEN_VARIABLE: ""}
    return {"mcpServers": {SERVER_KEY: {**entry, "env": environment}}}


def claude_command(
    binary: Path,
    *,
    mcp_config: Path,
    prompt: str,
    tools: Sequence[str],
    existing_login: bool = False,
) -> list[str]:
    """One non-interactive run with named MCP calls pre-authorized and no prompts.

    ``existing_login`` does not change the command: it is identical to token
    mode.  No ``--safe-mode``, ``--restricted`` or ``--tools`` flag is passed.
    ``--safe-mode`` disables every MCP server, and Claude Code 2.1.289 loads
    ``--mcp-config`` asynchronously, so any ``--tools`` filter is evaluated
    before the MCP tools register and the host initialize is never reported
    (``host_initialize_missing``).  The guardrails are the strict exact MCP config,
    project-only setting sources, exact MCP-only ``--allowedTools``, ``dontAsk``
    with no prompts, no session persistence and the private ``TMPDIR`` the caller
    sets; ambient MCP servers and executable built-ins are not pre-authorized.
    """
    allowed = ",".join(claude_tool_name(tool) for tool in tools)
    command = [
        str(binary),
        "-p",
        prompt,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--allowedTools",
        allowed,
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "project",
    ]
    return command


def codex_config_toml(entry: Mapping[str, Any]) -> str:
    """The ``config.toml`` text: no approval prompts, one stdio server (JSON strings are TOML)."""
    arguments = ", ".join(json.dumps(argument) for argument in entry["args"])
    return (
        'approval_policy = "never"\n\n'
        f"[mcp_servers.{SERVER_KEY}]\n"
        f"command = {json.dumps(entry['command'])}\n"
        f"args = [{arguments}]\n"
    )


def codex_mcp_add_command(binary: Path, entry: Mapping[str, Any]) -> list[str]:
    return [str(binary), "mcp", "add", SERVER_KEY, "--", entry["command"], *entry["args"]]


def codex_command(
    binary: Path, *, workspace: Path, prompt: str, last_message: Path
) -> list[str]:
    """One ephemeral, read-only, non-interactive Codex run."""
    return [
        str(binary),
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--config",
        'approval_policy="never"',
        "--json",
        "--output-last-message",
        str(last_message),
        "--cd",
        str(workspace),
        prompt,
    ]


# --- isolated host layout and authentication copy -------------------------


def make_private_directory(path: Path) -> None:
    """Create ``path`` and any missing parents with mode 0700."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)


@dataclass(frozen=True)
class HostLayout:
    root: Path
    home: Path
    config_dir: Path
    workspace: Path
    auth_destination: Path
    config_variable: str

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    def environment(self) -> dict[str, str]:
        """The variables that redirect the host; the caller merges the rest."""
        return {"HOME": str(self.home), self.config_variable: str(self.config_dir)}


def host_layout(root: Path, host: str) -> HostLayout:
    home = root / "home"
    if host == "claude-code":
        config_dir, auth, variable = home / ".claude", ".credentials.json", "CLAUDE_CONFIG_DIR"
    elif host == "codex-cli":
        config_dir, auth, variable = home / ".codex", "auth.json", "CODEX_HOME"
    else:
        raise QualificationError(ReasonCode.RECORD_INVALID)
    return HostLayout(root, home, config_dir, root / "workspace", config_dir / auth, variable)


def create_layout(layout: HostLayout) -> None:
    for directory in (layout.home, layout.config_dir, layout.workspace, layout.temporary):
        make_private_directory(directory)


def _open_auth_source(source: Path) -> int:
    """Open an owner-owned regular file with no group or world bits; else refuse."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError:
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE) from None
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or not status.st_mode & stat.S_IRUSR
        or status.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        os.close(descriptor)
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE)
    return descriptor


def require_auth_file(source: Path) -> None:
    os.close(_open_auth_source(source))


def copy_auth_file(source: Path, destination: Path) -> None:
    """Copy one explicit authentication file by bytes, never parsing it.

    The destination is created exclusively with mode 0600 inside 0700 parents.
    Returns nothing, and every refusal is ``authentication_unavailable``.
    """
    descriptor = _open_auth_source(source)
    try:
        make_private_directory(destination.parent)
        written = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(written, 0o600)
            with os.fdopen(descriptor, "rb", closefd=False) as reader, os.fdopen(
                written, "wb", closefd=False
            ) as writer:
                shutil.copyfileobj(reader, writer)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        finally:
            os.close(written)
    except OSError:
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE) from None
    finally:
        os.close(descriptor)


def read_claude_token(source: Path) -> str:
    """Return the one OAuth token ``claude setup-token`` produced, or refuse.

    The file holds the token alone, optionally followed by one LF.  Its text is
    checked and returned, never echoed, copied or included in any refusal.
    """
    descriptor = _open_auth_source(source)
    try:
        with os.fdopen(descriptor, "rb") as reader:
            raw = reader.read(CLAUDE_TOKEN_FILE_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE) from None
    token = text.removesuffix("\n")
    if len(raw) > CLAUDE_TOKEN_FILE_BYTES or not _CLAUDE_TOKEN.fullmatch(token):
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE)
    return token


@dataclass(frozen=True)
class AuthFile:
    """An owner-protected credential file: Claude's setup token, or Codex's ``auth.json``."""

    path: Path


def _plain_text(value: str) -> bool:
    return value != "" and value.isprintable() and value == value.strip()


@dataclass(frozen=True)
class ExistingLogin:
    """The Claude CLI profile already logged in on this host.  It names no file.

    ``home`` and ``user`` are the invoking process's own values, carried unchanged so
    the host process selects the same login.  They are hidden from ``repr``.
    """

    home: str = field(repr=False)
    user: str = field(repr=False)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ExistingLogin:
        """Capture ``HOME`` and ``USER``, or refuse with a fixed code and no value."""
        home, user = environ.get("HOME", ""), environ.get("USER", "")
        if not (
            _plain_text(home)
            and os.path.isabs(home)
            and _plain_text(user)
        ):
            raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE)
        return cls(home, user)


#: The one credential source a run uses.  Exactly one is ever selected.
AuthSource = AuthFile | ExistingLogin


def auth_source(
    host: str,
    auth_file: Path | None,
    existing_login: bool,
    environ: Mapping[str, str] | None = None,
) -> AuthSource:
    """Select exactly one auth source, or raise ``ValueError`` with a fixed message.

    An existing login also captures the invoking ``HOME`` and ``USER``; a missing or
    malformed value raises ``QualificationError`` (``authentication_unavailable``).
    """
    if existing_login:
        if host != "claude-code" or auth_file is not None:
            raise ValueError("--use-existing-host-auth is Claude only and excludes --auth-file")
        return ExistingLogin.from_environ(os.environ if environ is None else environ)
    if auth_file is None:
        raise ValueError("qualification requires --auth-file or --use-existing-host-auth")
    return AuthFile(auth_file)


def provision_credential(host: str, layout: HostLayout, auth: AuthSource) -> dict[str, str]:
    """Place Codex's ``auth.json``, or return Claude's token for its environment only.

    Claude never gets a credential file: a setup token reaches the host solely as
    ``CLAUDE_CODE_OAUTH_TOKEN``, which overrides any keychain login.  An existing
    login provisions nothing and never reads or copies the real profile.  Codex
    accepts no existing login, so that pairing is refused here too.
    """
    if isinstance(auth, ExistingLogin):
        if host != "claude-code":
            raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE)
        return {}
    if host == "claude-code":
        return {CLAUDE_TOKEN_VARIABLE: read_claude_token(auth.path)}
    copy_auth_file(auth.path, layout.auth_destination)
    return {}


# --- fail-closed gate ledger ----------------------------------------------


class Evidence(enum.Enum):
    """Where an observation came from; only the harness's own inspection counts."""

    INDEPENDENT = "independent"
    MODEL = "model"


class GateStatus(enum.Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class GateLedger:
    """Every subcheck starts closed; only an independent, boolean observation moves it.

    ``False`` always wins: a failed subcheck stays failed whatever follows, and
    a passed one is downgraded by a later failing observation.  Model text,
    markers and non-boolean values are refused, never recorded.
    """

    def __init__(self) -> None:
        self._status = {
            (gate, check): GateStatus.PENDING
            for gate, checks in GATES.items()
            for check in checks
        }

    def record(self, gate: str, check: str, observed: object, *, source: Evidence) -> None:
        if source is not Evidence.INDEPENDENT or type(observed) is not bool:
            raise QualificationError(ReasonCode.MODEL_EVIDENCE_REJECTED)
        key = (gate, check)
        if key not in self._status:
            raise QualificationError(ReasonCode.RECORD_INVALID)
        if observed is False:
            self._status[key] = GateStatus.FAILED
        elif self._status[key] is GateStatus.PENDING:
            self._status[key] = GateStatus.PASSED

    def status(self, gate: str, check: str) -> GateStatus:
        return self._status[(gate, check)]

    def gate_passed(self, gate: str) -> bool:
        return all(self._status[(gate, check)] is GateStatus.PASSED for check in GATES[gate])

    def all_passed(self) -> bool:
        return all(self.gate_passed(gate) for gate in GATES)

    def as_record(self) -> dict[str, dict[str, bool]]:
        return {
            gate: {check: self._status[(gate, check)] is GateStatus.PASSED for check in checks}
            for gate, checks in GATES.items()
        }


# --- record, schema validation and atomic output --------------------------


@dataclass(frozen=True)
class OsIdentity:
    version: str
    build: str
    architecture: str


@dataclass(frozen=True)
class HostIdentity:
    name: str
    version: str


def utc_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise QualificationError(ReasonCode.RECORD_INVALID)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_record(
    *,
    candidate: Candidate,
    schema_sha256: str,
    os_identity: OsIdentity,
    host: HostIdentity,
    ledger: GateLedger,
    started_at: datetime,
    finished_at: datetime,
    reason: ReasonCode | None = None,
) -> dict[str, Any]:
    """Build the closed record from structured observations only.

    The verdict is derived, never supplied: it is ``pass`` only when no reason
    is given, the host is the pinned version and every gate is independently
    observed true.
    """
    started, finished = utc_timestamp(started_at), utc_timestamp(finished_at)
    if (
        host.name not in HOST_VERSIONS
        or finished < started
        or not _SHA256.fullmatch(schema_sha256)
    ):
        raise QualificationError(ReasonCode.RECORD_INVALID)
    if reason is ReasonCode.NONE:
        reason = None
    if reason is None and host.version != HOST_VERSIONS[host.name]:
        reason = ReasonCode.HOST_VERSION_UNSUPPORTED
    if reason is None and not ledger.all_passed():
        reason = ReasonCode.GATE_FAILED
    return {
        "format": RECORD_FORMAT,
        "verdict": "pass" if reason is None else "fail",
        "reason_code": (reason or ReasonCode.NONE).value,
        "source": {"revision": candidate.revision, "clean": True},
        "wheels": {name: candidate.wheels[name] for name in FIRST_PARTY},
        "bindings": {
            "wheel_closure_count": candidate.closure_count,
            "wheel_closure_sha256": candidate.closure_sha256,
            "harness_sha256": candidate.harness_sha256,
            "schema_sha256": schema_sha256,
        },
        "os": {
            "product": OS_PRODUCT,
            "version": os_identity.version,
            "build": os_identity.build,
            "architecture": os_identity.architecture,
        },
        "host": {"name": host.name, "version": host.version},
        "sdk_versions": dict(SDK_PINS),
        "profiles": {
            profile: {"tool_count": len(tools), "tools": list(tools)}
            for profile, tools in PROFILE_TOOLS.items()
        },
        "gates": ledger.as_record(),
        "started_at": started,
        "finished_at": finished,
    }


def build_minimal_failure_record(
    reason: ReasonCode, *, started_at: datetime, finished_at: datetime
) -> dict[str, Any]:
    """Build the closed failure shape when verified run metadata is unavailable."""
    if reason is ReasonCode.NONE:
        raise QualificationError(ReasonCode.RECORD_INVALID)
    started, finished = utc_timestamp(started_at), utc_timestamp(finished_at)
    if finished < started:
        raise QualificationError(ReasonCode.RECORD_INVALID)
    return {
        "format": RECORD_FORMAT,
        "verdict": "fail",
        "reason_code": reason.value,
        "started_at": started,
        "finished_at": finished,
    }


def load_schema(path: Path, expected_sha256: str | None = None) -> Mapping[str, Any]:
    import jsonschema  # lazy: the proxy mode runs on the stdlib alone

    try:
        raw = path.read_bytes()
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise QualificationError(ReasonCode.RECORD_INVALID)
        schema = _mapping(json.loads(raw.decode("utf-8")), ReasonCode.RECORD_INVALID)
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeDecodeError, ValueError, jsonschema.SchemaError):
        raise QualificationError(ReasonCode.RECORD_INVALID) from None
    return schema


def validate_record(record: object, schema: Mapping[str, Any]) -> None:
    import jsonschema

    if not jsonschema.Draft202012Validator(schema).is_valid(record):
        raise QualificationError(ReasonCode.RECORD_INVALID)


def write_record(
    record: Mapping[str, Any],
    schema: Mapping[str, Any],
    output: Path,
    *,
    replace: Callable[[Path, Path], object] = os.replace,
) -> None:
    """Validate, then publish ``output`` atomically; never leave a partial file."""
    validate_record(record, schema)
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".record-", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temporary, output)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise QualificationError(ReasonCode.RECORD_INVALID) from None


# --- installed-candidate bootstrap -----------------------------------------

CANDIDATE_MARKER: Final = "OMNIVIA_REAL_HOST_QUALIFICATION_CANDIDATE"
CONSOLE_SCRIPTS: Final = ("omnivia-core-service", "omnivia", "omnivia-core-mcp")
BOOTSTRAP_TIMEOUT: Final = 900.0

#: Runs one command with an explicit environment and returns its transient
#: output.  Seam: tests replace it, so no pip or wheel is ever executed.
Runner = Callable[[Sequence[str], Mapping[str, str], Path, float], "subprocess.CompletedProcess[bytes]"]
ProtocolRunner = Callable[
    [Sequence[str], bytes, Mapping[str, str], Path, float],
    "subprocess.CompletedProcess[bytes]",
]

#: Executed inside the candidate venv: reports the SDK pins and whether every
#: first-party distribution is a non-editable install under that venv.
_PROBE: Final = """
import importlib.metadata as m, json, sys
from pathlib import Path
prefix = Path(sys.prefix).resolve()
first = json.loads(sys.argv[1])
def inside(name):
    return Path(m.distribution(name).locate_file("")).resolve().is_relative_to(prefix)
def editable(name):
    raw = m.distribution(name).read_text("direct_url.json")
    return raw is not None and "dir_info" in json.loads(raw)
print(json.dumps({
    "versions": {n: m.version(n) for n in ("mcp", "mcp-types")},
    "inside": all(inside(n) for n in first),
    "editable": any(editable(n) for n in first),
}))
"""


def _run_transient(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_protocol(
    argv: Sequence[str],
    payload: bytes,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        input=payload,
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _create_venv(path: Path) -> None:
    venv.EnvBuilder(with_pip=True).create(path)


@dataclass(frozen=True)
class InstalledCandidate:
    venv: Path
    python: Path
    service: Path
    cli: Path
    mcp: Path


def bootstrap_candidate(
    candidate_dir: Path,
    candidate: Candidate,
    root: Path,
    *,
    run: Runner = _run_transient,
    create_venv: Callable[[Path], object] = _create_venv,
) -> InstalledCandidate:
    """Install exactly the candidate's wheels offline into a fresh venv under ``root``.

    Every failure is a stable reason code; pip output and paths are discarded.
    """
    manifest = _json_document(candidate_dir / "metadata" / "release-manifest.json")
    entries = manifest.get("wheels")
    if not isinstance(entries, list):
        raise QualificationError(ReasonCode.CANDIDATE_INVALID)
    verified = _verified_wheels(entries, candidate_dir)
    closure_count, closure_sha256 = _closure_binding(entries)
    if (
        {name: verified[name][1] for name in FIRST_PARTY} != dict(candidate.wheels)
        or closure_count != candidate.closure_count
        or closure_sha256 != candidate.closure_sha256
    ):
        raise QualificationError(ReasonCode.WHEEL_DIGEST_MISMATCH)
    home = root / "bootstrap-home"
    make_private_directory(home)
    environment = {"PATH": SYSTEM_PATH, "HOME": str(home), "PYTHONNOUSERSITE": "1"}
    requirements = root / "closure-requirements.txt"
    try:
        _write_private(requirements, hashed_requirements(verified))
    except OSError:
        raise QualificationError(ReasonCode.INSTALL_FAILED) from None
    environment_directory = root / "candidate-venv"
    python = environment_directory / "bin" / "python"
    try:
        create_venv(environment_directory)
        installed = run(
            [
                str(python), "-I", "-m", "pip", "install",
                "--isolated", "--no-index", "--only-binary=:all:",
                "--require-hashes", "--disable-pip-version-check", "--no-input",
                "--requirement", str(requirements),
            ],
            environment,
            root,
            BOOTSTRAP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationError(ReasonCode.INSTALL_FAILED) from None
    if installed.returncode != 0:
        raise QualificationError(ReasonCode.INSTALL_FAILED)
    resolved_root = environment_directory.resolve()
    scripts: list[Path] = []
    for name in CONSOLE_SCRIPTS:
        script = environment_directory / "bin" / name
        if (
            script.is_symlink()
            or not script.is_file()
            or not os.access(script, os.X_OK)
            or not script.resolve().is_relative_to(resolved_root)
        ):
            raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
        scripts.append(script)
    try:
        probed = run(
            [str(python), "-I", "-c", _PROBE, json.dumps(list(FIRST_PARTY))],
            environment,
            root,
            BOOTSTRAP_TIMEOUT,
        )
        report = json.loads(probed.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise QualificationError(ReasonCode.INSTALL_FAILED) from None
    if probed.returncode != 0 or not isinstance(report, dict):
        raise QualificationError(ReasonCode.INSTALL_FAILED)
    if report.get("versions") != SDK_PINS:
        raise QualificationError(ReasonCode.SDK_PIN_MISMATCH)
    if report.get("inside") is not True or report.get("editable") is not False:
        raise QualificationError(ReasonCode.INSTALL_FAILED)
    return InstalledCandidate(environment_directory, python, *scripts)


def _mcp_origin() -> str | None:
    try:
        spec = importlib.util.find_spec("mcp")
    except (ImportError, ValueError):
        return None
    return None if spec is None else spec.origin


def in_candidate_runtime(
    environ: Mapping[str, str],
    prefix: str,
    *,
    mcp_origin: Callable[[], str | None] = _mcp_origin,
) -> bool:
    """True when re-executed under the candidate venv; refuse a mismatched marker."""
    marker = environ.get(CANDIDATE_MARKER)
    if marker is None:
        return False
    venv_root = Path(prefix).resolve()
    origin = mcp_origin()
    if Path(marker).resolve() != venv_root or origin is None or not Path(origin).resolve().is_relative_to(venv_root):
        raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
    return True


def reexec_under_candidate(
    installed: InstalledCandidate,
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    execve: Callable[[str, list[str], dict[str, str]], object] = os.execve,
) -> None:
    """Replace this process with the harness running on the candidate's Python."""
    environment = dict(os.environ if environ is None else environ)
    environment[CANDIDATE_MARKER] = str(installed.venv)
    python = str(installed.python)
    execve(python, [python, "-I", str(SCRIPT_PATH), *argv], environment)


# --- transparent stdio proxy and its observation stream --------------------

INTERNAL_PROXY: Final = "--internal-proxy"
PROXY_VIOLATION_EXIT: Final = 3
PROXY_WITHHELD_EXIT: Final = 4
#: An observation or pipe failure: the proxy cannot prove what it relayed, so it stops.
PROXY_FAILED_EXIT: Final = 5
#: The longest the proxy keeps the child's input open for unanswered requests.
DRAIN_TIMEOUT: Final = 30.0
#: The longest the withheld seal waits for an in-flight host write before failing closed.
SEAL_WRITE_TIMEOUT: Final = 5.0
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
VIOLATION_KINDS: Final = frozenset(
    {
        "malformed",
        "not_json",
        "not_object",
        "not_jsonrpc_2_0",
        "duplicate_request",
        "invalid_tool_call",
        "invalid_tool_result",
        "invalid_tool_inventory",
        "invalid_initialize",
        "frame_after_withheld",
        "oversized_frame",
    }
)


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _is_name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 10_000


def _is_names(value: object) -> bool:
    return isinstance(value, list) and len(value) <= 256 and all(_is_name(item) for item in value)


#: What a failed call was refused for.  Only these closed classes are observed;
#: the refusal text itself is classified in the relay and never kept.
REFUSALS: Final = frozenset(
    {
        "none",
        "idempotency_conflict",
        "capability_not_granted",
        "not_callable",
        "not_exposed",
        "credential_missing",
        "other",
    }
)


#: The closed observation vocabulary: event type -> required fields.  Nothing
#: else, in particular no argument, content, result, path or process identity,
#: can be written to or read from the stream.
EVENT_SHAPES: Final[dict[str, dict[str, Callable[[object], bool]]]] = {
    "proxy_started": {},
    "initialize_request": {},
    "initialize_response": {"ok": _is_bool},
    "tools_list_request": {},
    "tools_list_response": {"ok": _is_bool, "tool_count": _is_count, "tool_names": _is_names},
    "tool_call_request": {"tool": _is_name, "arguments_digest": _is_digest},
    "tool_call_response": {
        "tool": _is_name,
        "ok": _is_bool,
        "tool_error": _is_bool,
        "result_digest": _is_digest,
        "refusal": lambda value: value in REFUSALS,
    },
    "request_paused": {"tool": _is_name},
    #: The continuation's value never enters the stream; only whether one was handed off.
    "continuation_captured": {"tool": _is_name, "present": _is_bool},
    #: The withheld answer is never forwarded or kept; only its canonical digest is.
    "response_withheld": {"tool": _is_name, "tool_error": _is_bool, "result_digest": _is_digest},
    "protocol_violation": {"kind": lambda value: value in VIOLATION_KINDS},
}


def validate_event(event: object) -> dict[str, Any]:
    """Return ``event`` if it is exactly one closed event; else refuse."""
    if not isinstance(event, dict):
        raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS)
    shape = EVENT_SHAPES.get(event.get("event"))  # type: ignore[arg-type]
    seq = event.get("seq")
    if (
        shape is None
        or type(seq) is not int
        or seq < 1
        or set(event) != {"event", "seq", *shape}
        or not all(check(event[field]) for field, check in shape.items())
    ):
        raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS)
    return event


def arguments_digest(arguments: object) -> str:
    """SHA-256 of the canonical JSON of a tool call's arguments."""
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_result_digest(structured: object) -> str:
    """Digest of one structured result with its continuation token value masked.

    A continuation token is bound to the principal that issued it, so the host's
    and the owner's pages of one snapshot differ in that token alone.  Whether a
    token was present stays in the digest as a marker, so a continuing page and an
    exhausted page differ.  Every other field of the result stays in the digest.
    """
    if isinstance(structured, dict) and isinstance(structured.get("page"), dict):
        position = dict(structured["page"])
        if "continuation_token" in position:
            token = position["continuation_token"]
            position["continuation_token"] = "<present>" if token else "<absent>"
        structured = {**structured, "page": position}
    return arguments_digest(structured)


def refusal_class(result: object) -> str:
    """Classify a call's outcome into a closed refusal class; its text is never kept."""
    if not isinstance(result, dict) or result.get("isError") is not True:
        return "none"
    content = result.get("content")
    texts = [item.get("text") for item in content if isinstance(item, dict)] if isinstance(content, list) else []
    text = re.sub(r"\s+", "", "".join(item for item in texts if isinstance(item, str)))
    if '"code":"idempotency_conflict"' in text:
        return "idempotency_conflict"
    if '"code":"capability_not_granted"' in text:
        return "capability_not_granted"
    if "isnotatoolthisserverexposes" in text:
        return "not_exposed"
    # The fixed sanitized message of the installed-credential store: proof the credential is gone.
    if "thisinstallationholdsnocredentialforthatreference" in text:
        return "credential_missing"
    if "couldnotbecalled" in text:  # "could not be called": a generic refusal, not proof of revocation
        return "not_callable"
    return "other"


@dataclass(frozen=True)
class Interruption:
    """The one call whose response the proxy withholds."""

    tool: str
    arguments_digest: str

    def __post_init__(self) -> None:
        if not _is_name(self.tool) or not _is_digest(self.arguments_digest):
            raise QualificationError(ReasonCode.RECORD_INVALID)


#: The longest a continuation handoff may be; a token is a short opaque string.
HANDOFF_MAX_BYTES: Final = 8192


@dataclass(frozen=True)
class CaptureTarget:
    """The one successful page call whose continuation token the proxy hands off.

    The handoff is a private 0600 file the proxy writes before it forwards the answer,
    so the parent can read it once the host has exited.  Only the token value goes there.
    """

    tool: str
    arguments_digest: str
    handoff: Path

    def __post_init__(self) -> None:
        if not _is_name(self.tool) or not _is_digest(self.arguments_digest):
            raise QualificationError(ReasonCode.RECORD_INVALID)


@dataclass(frozen=True)
class PauseBefore:
    """Pause one exact tool request until the harness publishes ``release``."""

    tool: str
    arguments_digest: str
    release: Path

    def __post_init__(self) -> None:
        if not _is_name(self.tool) or not _is_digest(self.arguments_digest):
            raise QualificationError(ReasonCode.RECORD_INVALID)


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_proxy_spec(
    path: Path,
    *,
    child: Sequence[str],
    observation: Path,
    interruption: Interruption | None = None,
    pause_before: PauseBefore | None = None,
    capture: CaptureTarget | None = None,
) -> None:
    """Write the private spec the proxy reads: its child command, stream and target."""
    document = {
        "child": list(child),
        "observation": str(observation),
        "interrupt": None
        if interruption is None
        else {"tool": interruption.tool, "arguments_digest": interruption.arguments_digest},
        "pause_before": None
        if pause_before is None
        else {
            "tool": pause_before.tool,
            "arguments_digest": pause_before.arguments_digest,
            "release": str(pause_before.release),
        },
        "capture": None
        if capture is None
        else {
            "tool": capture.tool,
            "arguments_digest": capture.arguments_digest,
            "handoff": str(capture.handoff),
        },
    }
    _write_private(path, json.dumps(document))


def proxy_server_entry(python: Path, spec: Path) -> dict[str, Any]:
    """The stdio entry a host launches in place of the MCP entry point."""
    return {"command": str(python), "args": ["-I", str(SCRIPT_PATH), INTERNAL_PROXY, str(spec)]}


def _load_spec(
    path: Path,
) -> tuple[list[str], Path, Interruption | None, PauseBefore | None, CaptureTarget | None]:
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), ReasonCode.RECORD_INVALID)
        if set(document) != {"child", "observation", "interrupt", "pause_before", "capture"}:
            raise ValueError
        child = document["child"]
        observation = document["observation"]
        target = document["interrupt"]
        paused = document["pause_before"]
        handed_off = document["capture"]
        if (
            not isinstance(child, list)
            or not child
            or not all(isinstance(item, str) for item in child)
            or not isinstance(observation, str)
        ):
            raise ValueError
        interruption = None
        if target is not None:
            if not isinstance(target, dict) or set(target) != {"tool", "arguments_digest"}:
                raise ValueError
            interruption = Interruption(target["tool"], target["arguments_digest"])
        pause_before = None
        if paused is not None:
            if (
                not isinstance(paused, dict)
                or set(paused) != {"tool", "arguments_digest", "release"}
                or not isinstance(paused["release"], str)
            ):
                raise ValueError
            pause_before = PauseBefore(
                paused["tool"], paused["arguments_digest"], Path(paused["release"])
            )
        capture = None
        if handed_off is not None:
            if (
                not isinstance(handed_off, dict)
                or set(handed_off) != {"tool", "arguments_digest", "handoff"}
                or not isinstance(handed_off["handoff"], str)
            ):
                raise ValueError
            capture = CaptureTarget(
                handed_off["tool"], handed_off["arguments_digest"], Path(handed_off["handoff"])
            )
    except (OSError, ValueError, KeyError, TypeError, QualificationError):
        raise QualificationError(ReasonCode.RECORD_INVALID) from None
    return child, Path(observation), interruption, pause_before, capture


class _Violation(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _reject_constant(_: str) -> None:
    raise ValueError


def _parse_frame(frame: bytes) -> dict[str, Any]:
    """Parse one newline-terminated JSON-RPC object from a copy of a relayed frame."""
    if not frame.endswith(b"\n") or not frame.strip():
        raise _Violation("malformed")
    try:
        value = json.loads(frame.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Violation("not_json") from None
    if not isinstance(value, dict):
        raise _Violation("not_object")
    if value.get("jsonrpc") != "2.0":
        raise _Violation("not_jsonrpc_2_0")
    return value


def _is_modern_discovery(result: object) -> bool:
    """Whether a discovery result claims the pinned modern version (``True`` is never silent).

    A result that does not list the pinned version is not a modern advertisement and is
    relayed unobserved, so the host may fall back to the legacy ``initialize``.  A result
    that lists it is held to the whole shape and must be valid, else the caller refuses it.
    """
    if not isinstance(result, dict):
        return False
    versions = result.get("supportedVersions")
    return isinstance(versions, list) and MCP_MODERN_PROTOCOL_VERSION in versions


def _valid_implementation(value: object) -> bool:
    """An implementation object: nonempty, bounded ``name`` and ``version`` strings."""
    return isinstance(value, dict) and all(
        isinstance(value.get(field), str) and 0 < len(value[field]) <= 256
        for field in ("name", "version")
    )


def _valid_modern_discovery(result: dict[str, Any]) -> bool:
    """Shape-check a claimed-modern discovery; open extension keys pass and no value is kept."""
    versions = result["supportedVersions"]
    meta = result.get("_meta", {})
    ttl = result.get("ttlMs")
    return (
        len(versions) <= 32
        and all(isinstance(v, str) and 0 < len(v) <= 64 for v in versions)
        and len(set(versions)) == len(versions)
        and isinstance(result.get("capabilities"), dict)
        and result.get("resultType") == "complete"
        and type(ttl) is int
        and ttl >= 0
        and result.get("cacheScope") in ("public", "private")
        and result.get("protocolVersion", MCP_MODERN_PROTOCOL_VERSION)
        == MCP_MODERN_PROTOCOL_VERSION
        and ("serverInfo" not in result or _valid_implementation(result["serverInfo"]))
        and isinstance(meta, dict)
        and (
            "io.modelcontextprotocol/serverInfo" not in meta
            or _valid_implementation(meta["io.modelcontextprotocol/serverInfo"])
        )
        and meta.get("io.modelcontextprotocol/protocolVersion", MCP_MODERN_PROTOCOL_VERSION)
        == MCP_MODERN_PROTOCOL_VERSION
    )


def _request_key(identifier: object) -> str | None:
    if isinstance(identifier, str) or (type(identifier) is int):
        return json.dumps(identifier)
    return None


class _Observer:
    """Append-only private event stream; every event is validated against the closed shapes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor = -1
        self._closed = False
        self._lock = threading.Lock()
        self._sequence = 0

    def _write(self, event: str, **fields: object) -> None:
        if self._closed:
            raise OSError("observation closed")
        record = validate_event({"event": event, "seq": self._sequence + 1, **fields})
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        if self._descriptor < 0:
            # Created only now, so a launch that never emits leaves nothing behind and
            # nothing is ever removed.  An existing path of any kind refuses the launch.
            self._descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(self._descriptor, 0o600)
        self._sequence += 1
        os.write(self._descriptor, line.encode("ascii"))

    def emit(self, event: str, **fields: object) -> None:
        """Write one event, preceded by ``proxy_started`` when it is the launch's first.

        A launch that only relays frames it does not observe, such as the host's own
        protocol-version discovery probe, therefore writes no event and creates no file.
        """
        with self._lock:
            if self._sequence == 0 and event != "proxy_started":
                self._write("proxy_started")
            self._write(event, **fields)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            descriptor, self._descriptor = self._descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)


class _Relay:
    def __init__(
        self,
        observer: _Observer,
        interruption: Interruption | None,
        pause_before: PauseBefore | None = None,
        capture: CaptureTarget | None = None,
    ) -> None:
        self.observer = observer
        self.interruption = interruption
        self.pause_before = pause_before
        self.pause_consumed = False
        self.capture = capture
        self.capture_consumed = False
        self.pending: dict[str, tuple[str, str | None, bool, bool]] = {}
        #: Guards ``pending``, ``closed`` and ``writing``.  Never held across a pipe write.
        self.state = threading.Condition()
        #: True while the one host frame in flight to the child is being written.
        self.writing = False
        #: Once the interruption answer or a violation is observed, no host frame is
        #: admitted and no child frame is relayed again.
        self.closed = False
        self.violated = threading.Event()
        self.released = threading.Event()
        #: Set once the relay stops reading responses; a drain then has nothing to wait for.
        self.finished = threading.Event()

    def drain(self, timeout: float) -> None:
        """Wait, bounded, for every relayed request to be answered.

        The host's end of input must not close the child's stdin while a request is
        unanswered: an MCP server that reads end-of-input can exit before it writes
        the answer, so the answer would be lost.  Only the bound ends the wait.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.finished.is_set():
            with self.state:
                if not self.pending:
                    return
            time.sleep(0.01)

    def seal(self) -> bool:
        """Wait, bounded, for the in-flight host write to finish; False if it cannot.

        Call only after ``response`` has closed the relay, so no new host frame is
        admitted.  Once this returns True no host frame can reach the child.
        """
        with self.state:
            return self.state.wait_for(lambda: not self.writing, SEAL_WRITE_TIMEOUT)

    def request(self, frame: bytes, sink: Any | None = None) -> None:
        """Observe and, when supplied, forward one host frame.

        The request is recorded before its frame is written, so an answer can never
        arrive for an unrecorded request.  The write itself runs outside ``state``:
        a child that stops reading must not block the response thread.
        """
        message = _parse_frame(frame)
        method = message.get("method")
        key = _request_key(message.get("id"))
        entry: tuple[str, str | None, bool, bool] | None = None
        if isinstance(method, str) and key is not None and method == "initialize":
            self.observer.emit("initialize_request")
            entry = (method, None, False, False)
        elif isinstance(method, str) and key is not None and method == "server/discover":
            # Tracked only: a discovery that is not a valid modern answer leaves no evidence.
            entry = (method, None, False, False)
        elif isinstance(method, str) and key is not None and method == "tools/list":
            self.observer.emit("tools_list_request")
            entry = (method, None, False, False)
        elif isinstance(method, str) and key is not None and method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not _is_name(params.get("name")):
                raise _Violation("invalid_tool_call")
            tool = params["name"]
            try:
                digest = arguments_digest(params.get("arguments", {}))
            except (ValueError, RecursionError):
                raise _Violation("not_json") from None
            target = self.interruption
            self.observer.emit("tool_call_request", tool=tool, arguments_digest=digest)
            pause = self.pause_before
            if (
                not self.pause_consumed
                and pause is not None
                and (tool, digest) == (pause.tool, pause.arguments_digest)
            ):
                self.pause_consumed = True
                self.observer.emit("request_paused", tool=tool)
                while not pause.release.is_file() and not self.released.wait(0.01):
                    pass
            # Only the first request that matches the capture target is handed off.
            capture = self.capture
            captured = (
                not self.capture_consumed
                and capture is not None
                and (tool, digest) == (capture.tool, capture.arguments_digest)
            )
            self.capture_consumed = self.capture_consumed or captured
            entry = (
                method,
                tool,
                target is not None and (tool, digest) == (target.tool, target.arguments_digest),
                captured,
            )
        with self.state:
            if self.closed:
                raise _Violation("frame_after_withheld")
            if entry is not None:
                if key in self.pending:
                    raise _Violation("duplicate_request")
                assert key is not None
                self.pending[key] = entry
            # Admission and the in-flight mark are one step, so seal() cannot miss a write.
            self.writing = sink is not None
        if sink is None:
            return
        try:
            sink.write(frame)
            sink.flush()
        finally:
            with self.state:
                self.writing = False
                self.state.notify_all()

    def response(self, frame: bytes) -> bool:
        """Observe a server frame; True means it must be withheld, never forwarded."""
        message = _parse_frame(frame)
        key = _request_key(message.get("id"))
        if "method" in message or key is None:
            return False
        with self.state:
            if self.closed:
                raise _Violation("frame_after_withheld")
            entry = self.pending.pop(key, None)
            if entry is not None and entry[2]:
                self.closed = True
        if entry is None:
            return False
        method, tool, targeted, captured = entry
        result = message.get("result")
        ok = isinstance(result, dict) and "error" not in message
        if method == "server/discover":
            if not ok or not _is_modern_discovery(result):
                return False
            # The modern lifecycle has no ``initialize``; the validated discovery stands
            # for it, recorded before its answer is forwarded.
            assert isinstance(result, dict)
            valid = _valid_modern_discovery(result)
            self.observer.emit("initialize_request")
            self.observer.emit("initialize_response", ok=valid)
            if not valid:
                raise _Violation("invalid_initialize")
        elif method == "initialize":
            # The negotiated version must be the one this harness speaks; a reply
            # without a version string is malformed, not merely unsuccessful.
            protocol = result.get("protocolVersion") if isinstance(result, dict) else None
            if not ok or not isinstance(protocol, str) or protocol != MCP_PROTOCOL_VERSION:
                self.observer.emit("initialize_response", ok=False)
                raise _Violation("invalid_initialize")
            self.observer.emit("initialize_response", ok=True)
        elif method == "tools/list":
            tools = result.get("tools") if isinstance(result, dict) else None
            if (
                not isinstance(tools, list)
                or len(tools) > 256
                or not all(isinstance(item, dict) and _is_name(item.get("name")) for item in tools)
                # Pagination is unsupported: a listing that continues is not the whole inventory.
                or (isinstance(result, dict) and "nextCursor" in result)
            ):
                raise _Violation("invalid_tool_inventory")
            names = [item["name"] for item in tools]
            self.observer.emit(
                "tools_list_response", ok=ok, tool_count=len(tools), tool_names=names
            )
        else:
            failed = not ok or (isinstance(result, dict) and result.get("isError") is True)
            structured = result.get("structuredContent") if isinstance(result, dict) else None
            if failed:
                # A failed call must not carry the field at all, whatever its value.
                if isinstance(result, dict) and "structuredContent" in result:
                    raise _Violation("invalid_tool_result")
            elif not isinstance(structured, dict):
                raise _Violation("invalid_tool_result")
            digest = canonical_result_digest(structured)
            if targeted:
                # What the withheld answer would have said is kept as this digest alone,
                # so a later same-key replay can be held to it.
                self.observer.emit(
                    "response_withheld", tool=tool, tool_error=failed, result_digest=digest
                )
                return True
            self.observer.emit(
                "tool_call_response",
                tool=tool,
                ok=ok,
                tool_error=failed,
                result_digest=digest,
                refusal=refusal_class(result) if ok else "other",
            )
            if captured and not failed and self.capture is not None:
                self._hand_off(self.capture, structured)
        return False

    def _hand_off(self, capture: CaptureTarget, structured: object) -> None:
        """Write the page's continuation token to the private handoff and observe only its presence.

        The answer is forwarded unchanged, so the host sees the token in that result too.
        This copy goes only to the handoff file, which only the parent reads, and it is
        written before the answer is forwarded so the parent finds it once the host has
        exited.  A malformed or oversized token is a protocol violation, checked before
        anything is written or observed.  A write failure stops the child before the answer.
        """
        page = structured.get("page") if isinstance(structured, dict) else None
        if not isinstance(page, dict):
            raise _Violation("invalid_tool_result")
        token = page.get("continuation_token")
        if token is not None and (not isinstance(token, str) or not token):
            raise _Violation("invalid_tool_result")
        document = json.dumps({"continuation_token": token})
        if len(document.encode("utf-8")) > HANDOFF_MAX_BYTES:
            raise _Violation("invalid_tool_result")
        _write_private(capture.handoff, document)
        self.observer.emit("continuation_captured", tool=capture.tool, present=token is not None)

    def violation(self, kind: str) -> None:
        with self.state:
            self.closed = True
        if not self.violated.is_set():
            self.violated.set()
            self.observer.emit("protocol_violation", kind=kind)


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def _read_frame(stream: Any) -> bytes:
    """Read one newline-delimited frame, never more than ``MAX_FRAME_BYTES`` of it.

    The bound applies before any byte is parsed or forwarded: a longer frame is
    a protocol violation, so an oversized line can never exhaust memory.
    """
    frame: bytes = stream.readline(MAX_FRAME_BYTES + 1)
    if len(frame) > MAX_FRAME_BYTES:
        raise _Violation("oversized_frame")
    return frame


def _relay_session(child: subprocess.Popen[bytes], relay: _Relay, observer: _Observer) -> int:
    """Relay frames until either side ends, then stop and reap the child.

    An observation failure (a write or a validation error) is fatal: the frame it
    concerns is not forwarded, the child is stopped, and the exit is
    ``PROXY_FAILED_EXIT``.  A violation is observed once and exits with
    ``PROXY_VIOLATION_EXIT``.
    """
    stdin, stdout = child.stdin, child.stdout
    assert stdin is not None and stdout is not None
    failed = threading.Event()

    def fail_closed() -> None:
        failed.set()
        _stop(child)

    def observe_violation(kind: str) -> None:
        try:
            relay.violation(kind)
        except (OSError, QualificationError):
            failed.set()
        _stop(child)

    def terminate(_signal: int, _frame: object) -> None:
        _stop(child)
        os._exit(143)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)

    def pump_host() -> None:
        try:
            while frame := _read_frame(sys.stdin.buffer):
                relay.request(frame, stdin)
            relay.drain(DRAIN_TIMEOUT)
        except _Violation as violation:
            observe_violation(violation.kind)
        except OSError:
            # A write that fails after a violation is the child being stopped, not a fault.
            if not relay.violated.is_set():
                fail_closed()
        except QualificationError:
            fail_closed()
        finally:
            relay.released.set()
            with contextlib.suppress(OSError):
                stdin.close()

    threading.Thread(target=pump_host, daemon=True).start()
    withheld = False
    try:
        while frame := _read_frame(stdout):
            if relay.response(frame):
                withheld = True
                relay.finished.set()
                if relay.seal():
                    relay.released.wait()
                else:
                    # The in-flight write is stuck behind a child that stopped reading.
                    failed.set()
                break
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
    except _Violation as violation:
        observe_violation(violation.kind)
    except (OSError, QualificationError):
        fail_closed()
    relay.finished.set()
    _stop(child)
    if failed.is_set():
        return PROXY_FAILED_EXIT
    if relay.violated.is_set():
        return PROXY_VIOLATION_EXIT
    return PROXY_WITHHELD_EXIT if withheld else (child.returncode or 0)


def run_proxy(spec_path: Path) -> int:
    """Relay stdin/stdout to the installed MCP entry point, byte for byte.

    Frames are newline-delimited JSON-RPC (the MCP stdio framing), each bounded to
    ``MAX_FRAME_BYTES``.  Each frame is forwarded exactly as read; observation
    parses only a copy.  A frame that is not a JSON object, or is oversized, fails
    closed: it is not forwarded, the child is stopped and the proxy exits
    non-zero.  The child is stopped and reaped on every exit path, including a
    handled qualification failure.  The child's stderr stays on this process's
    stderr, never stdout.  The one configured interruption target's response is
    withheld indefinitely and never replaced; only its canonical digest is observed.
    The one configured capture target's first successful answer has its page's
    continuation token written to a private handoff, and nothing else of it is kept.
    """
    child_command, observation, interruption, pause_before, capture = _load_spec(spec_path)
    observer = _Observer(observation)
    child: subprocess.Popen[bytes] | None = None
    try:
        relay = _Relay(observer, interruption, pause_before, capture)
        try:
            child = subprocess.Popen(child_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError:
            return 2
        return _relay_session(child, relay, observer)
    finally:
        if child is not None:
            _stop(child)
        observer.close()


def proxy_main(arguments: Sequence[str]) -> int:
    try:
        if len(arguments) != 1:
            raise QualificationError(ReasonCode.RECORD_INVALID)
        return run_proxy(Path(arguments[0]))
    except (QualificationError, OSError):
        print("reason_code=record_invalid", file=sys.stderr)
        return 2


def read_observation(path: Path) -> list[dict[str, Any]]:
    """Read and strictly validate the stream; an absent stream is empty."""
    try:
        text = path.read_text(encoding="ascii") if path.exists() else ""
        events = [validate_event(json.loads(line)) for line in text.splitlines()]
    except (OSError, ValueError):
        raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS) from None
    if [event["seq"] for event in events] != list(range(1, len(events) + 1)):
        raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS)
    return events


@dataclass(frozen=True)
class ObservationSummary:
    """What the proxy independently saw; derived from events only."""

    initialized: bool
    listed: bool
    listed_tools: tuple[str, ...]
    called: tuple[str, ...]
    requests: tuple[tuple[str, str], ...]
    responded: tuple[str, ...]
    succeeded: tuple[str, ...]
    tool_errors: tuple[str, ...]
    paused: bool
    withheld: bool
    violation: bool
    #: Per answered call, in order: (tool, refusal class, result digest).
    outcomes: tuple[tuple[str, str, str], ...] = ()
    #: A second MCP initialize after the paused request: a substituted process.
    initialized_after_pause: bool = False
    #: The canonical digest of the withheld answer, when it was a successful
    #: structured result.  The answer itself is never kept.
    withheld_digest: str | None = None
    #: Per handed-off page, in order: (tool, whether a continuation token was present).
    captured: tuple[tuple[str, bool], ...] = ()


def summarize_observation(events: Sequence[Mapping[str, Any]]) -> ObservationSummary:
    listed = [e["tool_names"] for e in events if e["event"] == "tools_list_response" and e["ok"]]
    paused_at = next(
        (index for index, e in enumerate(events) if e["event"] == "request_paused"), len(events)
    )
    return ObservationSummary(
        initialized_after_pause=any(e["event"] == "initialize_request" for e in events[paused_at:]),
        initialized=any(e["event"] == "initialize_response" and e["ok"] for e in events),
        listed=bool(listed),
        listed_tools=tuple(listed[0]) if listed else (),
        called=tuple(e["tool"] for e in events if e["event"] == "tool_call_request"),
        requests=tuple(
            (e["tool"], e["arguments_digest"])
            for e in events
            if e["event"] == "tool_call_request"
        ),
        responded=tuple(e["tool"] for e in events if e["event"] == "tool_call_response"),
        succeeded=tuple(
            e["tool"]
            for e in events
            if e["event"] == "tool_call_response" and e["ok"] and not e["tool_error"]
        ),
        tool_errors=tuple(
            e["tool"]
            for e in events
            if e["event"] == "tool_call_response" and e["tool_error"]
        ),
        paused=any(e["event"] == "request_paused" for e in events),
        withheld=any(e["event"] == "response_withheld" for e in events),
        withheld_digest=next(
            (
                e["result_digest"]
                for e in events
                if e["event"] == "response_withheld" and not e["tool_error"]
            ),
            None,
        ),
        violation=any(e["event"] == "protocol_violation" for e in events),
        outcomes=tuple(
            (e["tool"], e["refusal"], e["result_digest"])
            for e in events
            if e["event"] == "tool_call_response"
        ),
        captured=tuple(
            (e["tool"], e["present"]) for e in events if e["event"] == "continuation_captured"
        ),
    )


# --- isolated host process runner ------------------------------------------


def _write_private_file(path: Path, text: str) -> None:
    try:
        _write_private(path, text)
    except OSError:
        raise QualificationError(ReasonCode.HOST_LAUNCH_FAILED) from None


def write_host_config(
    layout: HostLayout,
    host: str,
    entry: Mapping[str, Any],
    *,
    existing_login: ExistingLogin | None = None,
) -> Path:
    """Write the native per-run MCP configuration for ``host`` and return its path.

    An existing login's Claude MCP child is pointed at the private layout, never the
    real profile; token mode writes the empty token override alone.
    """
    if host == "claude-code":
        path = layout.root / "claude-mcp.json"
        redirect = None if existing_login is None else layout.environment()
        _write_private_file(path, json.dumps(claude_mcp_config(entry, redirect=redirect)))
    elif host == "codex-cli":
        path = layout.config_dir / "config.toml"
        _write_private_file(path, codex_config_toml(entry))
    else:
        raise QualificationError(ReasonCode.RECORD_INVALID)
    return path


def host_environment(
    layout: HostLayout,
    binary: Path,
    credential: Mapping[str, str] | None = None,
    *,
    existing_login: ExistingLogin | None = None,
) -> dict[str, str]:
    """A minimal environment: no operator variable or ambient config reaches the host.

    ``credential`` is the only extra input, given by ``provision_credential``: the
    provisioned portable credential intentionally reaches the host.  An existing
    login keeps the invoking ``HOME`` and ``USER`` so the host selects the same
    profile; it sets no config variable and injects no token.  The per-run
    ``TMPDIR`` is private in both modes.
    """
    if existing_login is None:
        redirect = layout.environment()
        profile: dict[str, str] = {}
    else:
        redirect = {}
        profile = {"HOME": existing_login.home, "USER": existing_login.user}
    return {
        "PATH": f"{binary.parent}:{SYSTEM_PATH}",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(layout.temporary),
        **redirect,
        **profile,
        **(credential or {}),
    }


def host_command(
    host: str,
    binary: Path,
    layout: HostLayout,
    config: Path,
    *,
    prompt: str,
    tools: Sequence[str],
    existing_login: bool = False,
) -> list[str]:
    if host == "claude-code":
        return claude_command(
            binary,
            mcp_config=config,
            prompt=prompt,
            tools=tools,
            existing_login=existing_login,
        )
    if host == "codex-cli":
        return codex_command(
            binary, workspace=layout.workspace, prompt=prompt, last_message=layout.root / "last-message.txt"
        )
    raise QualificationError(ReasonCode.RECORD_INVALID)


_VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")
_IDENTITY: Final = {"claude-code": "claude code", "codex-cli": "codex"}


def host_version(
    host: str, binary: Path, env: Mapping[str, str], cwd: Path, *, run: Runner = _run_transient
) -> str:
    """Run ``--version`` transiently and return only the normalized exact version."""
    try:
        completed = run([str(binary), "--version"], env, cwd, 60.0)
        text = completed.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        raise QualificationError(ReasonCode.HOST_BINARY_UNAVAILABLE) from None
    versions = _VERSION.findall(text)
    if completed.returncode != 0 or len(versions) != 1 or _IDENTITY[host] not in text.lower():
        raise QualificationError(ReasonCode.HOST_VERSION_UNSUPPORTED)
    return str(versions[0])


def require_host_version(
    host: str, binary: Path, env: Mapping[str, str], cwd: Path, *, run: Runner = _run_transient
) -> str:
    version = host_version(host, binary, env, cwd, run=run)
    if version != HOST_VERSIONS[host]:
        raise QualificationError(ReasonCode.HOST_VERSION_UNSUPPORTED)
    return version


def require_host_authentication(
    host: str,
    binary: Path,
    layout: HostLayout,
    auth: AuthSource,
    *,
    run: Runner = _run_transient,
) -> None:
    """Prove the credential works with the same environment and mode a session uses."""
    create_layout(layout)
    existing_login = auth if isinstance(auth, ExistingLogin) else None
    credential = provision_credential(host, layout, auth)
    command = (
        [str(binary), "auth", "status", "--json"]
        if host == "claude-code"
        else [str(binary), "login", "status"]
    )
    try:
        completed = run(
            command,
            host_environment(layout, binary, credential, existing_login=existing_login),
            layout.workspace,
            60.0,
        )
        if host == "claude-code":
            status = json.loads(completed.stdout.decode("utf-8"))
            authenticated = isinstance(status, dict) and status.get("loggedIn") is True
        else:
            # Codex 0.146.0 writes both its PATH-alias warning and the stable
            # login-status sentence to stderr.  Treat either transient channel
            # as status input, then discard both without retaining them.
            authenticated = b"logged in" in (
                completed.stdout + completed.stderr
            ).lower()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        authenticated = False
        completed = subprocess.CompletedProcess([], 1, b"", b"")
    if completed.returncode != 0 or not authenticated:
        raise QualificationError(ReasonCode.AUTHENTICATION_UNAVAILABLE)


@dataclass(frozen=True)
class CapturedContinuation:
    """The continuation token one successful page call returned, as the host saw it.

    Transient: the token is hidden from ``repr`` and must never be logged, recorded or
    retained.  It exists only for the next call of the same paging walk.
    """

    tool: str
    token: str | None = field(repr=False)


@dataclass(frozen=True)
class HostRunResult:
    """Typed outcome of one host run.  It holds no host output and no model text.

    ``marker_seen`` is a transient orchestration signal only; it is never gate
    evidence.  Gates may be fed from ``summary`` and Core's own state only.
    ``continuation`` is set only when the run was asked for a capture and the proxy
    proved it: it is the one transient token a paging walk chains from.
    """

    summary: ObservationSummary
    marker_seen: bool
    exited_cleanly: bool
    interrupted: bool
    paused: bool
    continuation: CapturedContinuation | None = field(default=None, repr=False)


def _kill_group(process: subprocess.Popen[bytes]) -> bool:
    """TERM then KILL the host's whole process group and prove it is absent."""
    group = process.pid
    if not _group_running(group):
        return True
    try:
        os.killpg(group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return not _group_running(group)
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if _wait_group_absent(group):
        return True
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return not _group_running(group)
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    return _wait_group_absent(group)


def run_host(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    observation: Path,
    marker: str,
    timeout: float,
    on_withheld: Callable[[], None] = lambda: None,
    on_paused: Callable[[], None] = lambda: None,
    poll_interval: float = 0.05,
    capture: CaptureTarget | None = None,
) -> HostRunResult:
    """Run one host, then, when asked, take the continuation the proxy handed off.

    The handoff is read only after the host group is gone, and it is unlinked on every
    path, success or failure.  An unlink that cannot be proved is a cleanup failure.
    """
    try:
        result = _run_host_process(
            command,
            env=env,
            cwd=cwd,
            observation=observation,
            marker=marker,
            timeout=timeout,
            on_withheld=on_withheld,
            on_paused=on_paused,
            poll_interval=poll_interval,
        )
        if capture is None:
            return result
        return replace(result, continuation=_take_continuation(capture, result.summary))
    finally:
        if capture is not None and not _unlink_handoff(capture.handoff):
            raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE)


def _take_continuation(
    capture: CaptureTarget, summary: ObservationSummary
) -> CapturedContinuation | None:
    """The handed-off continuation, proven by both the handoff and the observation.

    No observed capture and no handoff means the target never got a successful answer;
    that is ``None``, so the caller can retry a missing call.  Anything partial is refused.
    """
    if not summary.captured and not os.path.lexists(capture.handoff):
        return None
    token = _read_handoff(capture.handoff)
    if summary.captured != ((capture.tool, token is not None),):
        raise QualificationError(ReasonCode.GATE_FAILED)
    return CapturedContinuation(capture.tool, token)


def _read_handoff(path: Path) -> str | None:
    """Read the proxy's handoff; only the closed shape ``{"continuation_token": ...}`` passes."""
    try:
        status = os.lstat(path)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_size > HANDOFF_MAX_BYTES
        ):
            raise ValueError
        document = json.loads(path.read_bytes().decode("utf-8"), parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError):
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    if not isinstance(document, dict) or set(document) != {"continuation_token"}:
        raise QualificationError(ReasonCode.GATE_FAILED)
    token = document["continuation_token"]
    if token is not None and (not isinstance(token, str) or not token):
        raise QualificationError(ReasonCode.GATE_FAILED)
    return token


def _unlink_handoff(path: Path) -> bool:
    """Remove the handoff and prove it is gone; False when that cannot be proved."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return not os.path.lexists(path)


def _run_host_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    observation: Path,
    marker: str,
    timeout: float,
    on_withheld: Callable[[], None],
    on_paused: Callable[[], None],
    poll_interval: float,
) -> HostRunResult:
    """Run one host in its own process group and always kill the group afterwards.

    When the proxy reports a withheld response, ``on_withheld`` runs (the caller
    inspects Core's durable state there) and the group is then killed before
    the host can receive that response.  Host stdout and stderr go to
    unnamed temporary files, are read only for the marker and are discarded.
    """
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError:
            raise QualificationError(ReasonCode.HOST_LAUNCH_FAILED) from None
        interrupted = timed_out = paused = False
        failure: BaseException | None = None
        try:
            while process.poll() is None:
                if not paused and _event_seen(observation, "request_paused"):
                    on_paused()
                    paused = True
                if _withheld_seen(observation):
                    on_withheld()
                    interrupted = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(poll_interval)
        except BaseException as error:  # noqa: BLE001 - cleanup must run before every re-raise
            failure = error
        finally:
            cleaned = _kill_group(process)
        if not cleaned:
            if failure is not None:
                print(f"reason_code={ReasonCode.CLEANUP_INCOMPLETE.value}", file=sys.stderr)
            else:
                raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE)
        if failure is not None:
            raise failure
        if timed_out:
            raise QualificationError(ReasonCode.HOST_TIMEOUT)
        stdout.seek(0)
        marker_seen = marker.encode("utf-8") in stdout.read()
    return HostRunResult(
        summarize_observation(read_observation(observation)),
        marker_seen,
        process.returncode == 0 and not interrupted,
        interrupted,
        paused,
    )


def _withheld_seen(observation: Path) -> bool:
    return _event_seen(observation, "response_withheld")


def _event_seen(observation: Path, event: str) -> bool:
    try:
        needle = f'"event":"{event}"'.encode("ascii")
        return needle in observation.read_bytes()
    except OSError:
        return False


def run_host_session(
    *,
    host: str,
    binary: Path,
    layout: HostLayout,
    installed: InstalledCandidate,
    core_config: Path,
    auth: AuthSource,
    prompt: str,
    marker: str,
    tools: Sequence[str],
    timeout: float,
    interruption: Interruption | None = None,
    pause_before: PauseBefore | None = None,
    capture: CaptureTarget | None = None,
    on_withheld: Callable[[], None] = lambda: None,
    on_paused: Callable[[], None] = lambda: None,
) -> HostRunResult:
    """Lay out an isolated host, point it at the proxy and run it once."""
    create_layout(layout)
    existing_login = auth if isinstance(auth, ExistingLogin) else None
    credential = provision_credential(host, layout, auth)
    observation = layout.root / "observation.jsonl"
    spec = layout.root / "proxy-spec.json"
    child = mcp_server_entry(installed.mcp, core_config)
    try:
        write_proxy_spec(
            spec,
            child=[child["command"], *child["args"]],
            observation=observation,
            interruption=interruption,
            pause_before=pause_before,
            capture=capture,
        )
    except OSError:
        raise QualificationError(ReasonCode.HOST_LAUNCH_FAILED) from None
    config = write_host_config(
        layout,
        host,
        proxy_server_entry(installed.python, spec),
        existing_login=existing_login,
    )
    command = host_command(
        host,
        binary,
        layout,
        config,
        prompt=prompt,
        tools=tools,
        existing_login=existing_login is not None,
    )
    return run_host(
        command,
        env=host_environment(layout, binary, credential, existing_login=existing_login),
        cwd=layout.workspace,
        observation=observation,
        marker=marker,
        timeout=timeout,
        on_withheld=on_withheld,
        on_paused=on_paused,
        capture=capture,
    )


# --- Core lifecycle and independent state inspection ----------------------


def _run_text(
    arguments: Sequence[str], *, timeout: float = CORE_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationError(ReasonCode.GATE_FAILED) from None


def _output_document(completed: subprocess.CompletedProcess[str]) -> Mapping[str, Any]:
    if completed.returncode != 0 or completed.stderr:
        raise QualificationError(ReasonCode.GATE_FAILED)
    try:
        return _mapping(json.loads(completed.stdout), ReasonCode.GATE_FAILED)
    except ValueError:
        raise QualificationError(ReasonCode.GATE_FAILED) from None


@dataclass
class CoreContext:
    root: Path
    workspace: Path
    installation: Path
    workspace_id: str
    process: subprocess.Popen[str] | None = None
    replacement_pid: int | None = None
    #: The process evidence (pid, start time, boot id) of the one Core this run
    #: expects to serve the workspace.  ``start_core`` sets it; only the deliberate
    #: ``restart_core`` moves it, to the replacement.
    expected: dict[str, Any] | None = None
    #: Set when a Core process survived its stop; the run then refuses to pass.
    retained: bool = False

    @property
    def database(self) -> Path:
        return self.workspace / "workspace.sqlite"

    @property
    def descriptor(self) -> Path:
        return self.installation / "runtime" / self.workspace_id / "service.json"


def initialize_core(installed: InstalledCandidate, root: Path) -> CoreContext:
    make_private_directory(root)
    workspace = root / "workspace"
    installation = root / "installation-state"
    document = _output_document(
        _run_text(
            [
                str(installed.service),
                "--workspace",
                str(workspace),
                "--installation-state",
                str(installation),
                "--init",
            ]
        )
    )
    workspace_document = document.get("workspace")
    workspace_id = (
        workspace_document.get("workspace_id")
        if isinstance(workspace_document, dict)
        else None
    )
    if not isinstance(workspace_id, str) or not workspace_id:
        raise QualificationError(ReasonCode.GATE_FAILED)
    return CoreContext(root, workspace, installation, workspace_id)


def _wait_ready(context: CoreContext, process: subprocess.Popen[str]) -> Mapping[str, Any]:
    deadline = time.monotonic() + CORE_TIMEOUT
    while time.monotonic() < deadline:
        if context.descriptor.is_file():
            try:
                document = _mapping(
                    json.loads(context.descriptor.read_text(encoding="utf-8")),
                    ReasonCode.GATE_FAILED,
                )
            except (OSError, ValueError):
                document = {}
            if document.get("ready") is True:
                return document
        if process.poll() is not None:
            raise QualificationError(ReasonCode.GATE_FAILED)
        time.sleep(0.05)
    raise QualificationError(ReasonCode.GATE_FAILED)


def start_core(installed: InstalledCandidate, context: CoreContext) -> int:
    endpoint = f"unix://{context.root / 'core.sock'}"
    try:
        process = subprocess.Popen(
            [
                str(installed.service),
                "--workspace",
                str(context.workspace),
                "--installation-state",
                str(context.installation),
                "--endpoint",
                endpoint,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    context.process = process
    descriptor = _wait_ready(context, process)
    process_document = descriptor.get("process")
    process_id = (
        process_document.get("pid") if isinstance(process_document, dict) else None
    )
    if (
        not isinstance(process_document, dict)
        or type(process_id) is not int
        or process_id != process.pid
    ):
        stop_core(context)
        raise QualificationError(ReasonCode.GATE_FAILED)
    context.expected = dict(process_document)
    return process_id


def _published(context: CoreContext) -> Mapping[str, Any]:
    """The service descriptor as published, or empty when it cannot be read."""
    try:
        document = json.loads(context.descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _ready_process(context: CoreContext) -> dict[str, Any] | None:
    """The process evidence (pid, start time, boot id) a ready descriptor publishes."""
    published = _published(context)
    process = published.get("process")
    return process if published.get("ready") is True and isinstance(process, dict) else None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Permission denied proves neither absence nor safe ownership.  Treat the
        # PID as present so cleanup retains/fails closed instead of claiming it
        # disappeared.
        return True
    return True


def core_alive(context: CoreContext) -> bool:
    """Whether the expected Core still runs and is the ready one the descriptor names.

    ``service health`` cannot show this by itself: a managed-local client answers
    a Core that exited by starting a replacement, and the replacement is healthy.
    The whole process evidence is compared, so a reused pid is not the same Core.
    """
    expected = context.expected
    pid = None if expected is None else expected.get("pid")
    if type(pid) is not int or _ready_process(context) != expected:
        return False
    process = context.process
    if process is not None and process.pid == pid:
        return process.poll() is None
    return _pid_running(pid)


def _process_group(pid: int) -> int | None:
    """Return a safe foreign process group for ``pid``, or fail closed."""
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None
    if group <= 1 or group == os.getpgrp():
        return None
    return group


def _group_running(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group_absent(group: int) -> bool:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _group_running(group):
            return True
        time.sleep(0.05)
    return False


def _terminate_core_group(
    pid: int, process: subprocess.Popen[str] | None = None
) -> bool:
    """TERM then KILL the entire Core process group and prove it is absent."""
    # ``start_core`` creates this child in a new session, so its pid is the
    # process-group id even after the leader exits.  Retain that known identity
    # instead of asking getpgid for a pid that may already have disappeared while
    # descendants in its group remain alive.
    group = pid if process is not None and process.pid == pid else _process_group(pid)
    if group is None:
        return not _pid_running(pid)
    if group <= 1 or group == os.getpgrp():
        return False
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if process is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if _wait_group_absent(group):
        return True
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if process is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    return _wait_group_absent(group)


def _process_identity_matches(evidence: Mapping[str, Any] | None) -> bool | None:
    """Match descriptor evidence before signalling a non-child PID.

    ``True`` means the identity matches. ``False`` means the PID is absent.
    ``None`` means the identity could not be established, including a mismatch
    on a PID that is still running; cleanup then fails closed without signalling
    an uncertain process.
    """
    if not isinstance(evidence, Mapping):
        return None
    pid = evidence.get("pid")
    start_time = evidence.get("start_time")
    boot_id = evidence.get("boot_id")
    if type(pid) is not int or not isinstance(start_time, str) or not isinstance(boot_id, str):
        return None
    if platform.system().lower() != SUPPORTED_SYSTEM:
        return None
    try:
        started_probe = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if started_probe.returncode != 0:
            return False if not _pid_running(pid) else None
        started = started_probe.stdout.strip()
        booted_probe = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if booted_probe.returncode != 0:
            return None
        booted = booted_probe.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not started:
        return False if not _pid_running(pid) else None
    if not booted:
        return None
    if started == start_time and booted == boot_id:
        return True
    # A different identity on a live pid is uncertain, never proof of absence.
    return False if not _pid_running(pid) else None


def stop_core(context: CoreContext) -> None:
    """Stop every Core process serving this context, and record any that survives.

    That is the process this context started, the deliberate replacement, and any
    other running Core the descriptor names: a managed-local client's replacement
    for a Core that exited, which a failed continuity check must not leak.
    Escalation is SIGTERM, then SIGKILL after a bound.  A survivor sets
    ``context.retained``, which the caller turns into ``cleanup_incomplete``; it
    is never reported as a clean stop.
    """
    process = context.process
    running = process is not None and process.poll() is None
    # Once reaped, a live integer may name a reused process.  The known group is
    # then never signalled: only descriptor evidence that proves identity may.
    reused = process.pid if process is not None and not running and _pid_running(process.pid) else None
    if process is not None and reused is None and not _terminate_core_group(process.pid, process):
        context.retained = True
    named = _published(context).get("process")
    pid = named.get("pid") if isinstance(named, dict) else None
    if reused is not None and pid != reused:
        # A live same-PID process that no descriptor evidence names is uncertain.
        context.retained = True
    started = process.pid if process is not None and running else None
    unplanned = (
        pid
        if type(pid) is int and pid not in (started, context.replacement_pid) and _pid_running(pid)
        else None
    )
    candidates = (
        (context.replacement_pid, context.expected),
        (unplanned, named if isinstance(named, dict) else None),
    )
    for survivor, evidence in candidates:
        if survivor is None:
            continue
        identity = _process_identity_matches(evidence)
        if identity is False:
            # Absent by proof, not by assumption. A reaped child's PID that was live
            # and then gone during the probe was reused, so its original group had
            # already ceased (POSIX does not reuse a PID while that group exists).
            continue
        if identity is None:
            context.retained = True
            continue
        if not _terminate_core_group(survivor):
            context.retained = True


def _host_admin_name(host: str) -> str:
    if host == "claude-code":
        return host
    if host == "codex-cli":
        return "codex"
    raise QualificationError(ReasonCode.RECORD_INVALID)


def configure_profile(
    installed: InstalledCandidate, context: CoreContext, host: str, profile: str
) -> Path:
    completed = _run_text(
        [
            str(installed.cli),
            "--installation-state",
            str(context.installation),
            "mcp",
            "configure",
            "--host",
            _host_admin_name(host),
            "--workspace",
            context.workspace_id,
            "--profile",
            profile,
        ],
        timeout=600.0,
    )
    if completed.returncode != 0 or completed.stderr:
        raise QualificationError(ReasonCode.GATE_FAILED)
    try:
        document = (
            _mapping(json.loads(completed.stdout), ReasonCode.GATE_FAILED)
            if host == "claude-code"
            else _mapping(tomllib.loads(completed.stdout), ReasonCode.GATE_FAILED)
        )
    except (ValueError, tomllib.TOMLDecodeError):
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    servers = document.get("mcpServers")
    if host == "codex-cli":
        servers = document.get("mcp_servers")
    entry = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or set(entry) != {"command", "args"}:
        raise QualificationError(ReasonCode.GATE_FAILED)
    arguments = entry.get("args")
    if (
        entry.get("command") not in {str(installed.mcp), installed.mcp.name}
        or not isinstance(arguments, list)
        or len(arguments) != 2
        or arguments[0] != "--config"
        or not isinstance(arguments[1], str)
    ):
        raise QualificationError(ReasonCode.GATE_FAILED)
    config = Path(arguments[1])
    try:
        inside = config.resolve().is_relative_to(context.installation.resolve())
    except OSError:
        inside = False
    if not inside or config.is_symlink() or not config.is_file():
        raise QualificationError(ReasonCode.GATE_FAILED)
    return config


def configuration_principal(config: Path) -> str:
    try:
        document = _mapping(
            json.loads(config.read_text(encoding="utf-8")), ReasonCode.GATE_FAILED
        )
    except (OSError, ValueError):
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    principal = document.get("principal_id")
    if not isinstance(principal, str) or not principal:
        raise QualificationError(ReasonCode.GATE_FAILED)
    return principal


def revoke_authoring(
    installed: InstalledCandidate, context: CoreContext, host: str
) -> None:
    completed = _run_text(
        [
            str(installed.cli),
            "--installation-state",
            str(context.installation),
            "mcp",
            "revoke",
            "--host",
            _host_admin_name(host),
        ]
    )
    expected = f"revoked {_host_admin_name(host)}\n"
    if completed.returncode != 0 or completed.stderr or completed.stdout != expected:
        raise QualificationError(ReasonCode.GATE_FAILED)


def verify_revoked(installed: InstalledCandidate, context: CoreContext, host: str) -> None:
    """Require the installed owner status path to confirm both halves are revoked."""
    admin_host = _host_admin_name(host)
    completed = _run_text(
        [
            str(installed.cli),
            "--installation-state",
            str(context.installation),
            "mcp",
            "status",
            "--host",
            admin_host,
            "--json",
        ]
    )
    try:
        document = _mapping(json.loads(completed.stdout), ReasonCode.GATE_FAILED)
        rows = document.get("hosts")
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    except ValueError:
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    if (
        completed.returncode != 0
        or completed.stderr
        or not isinstance(row, dict)
        or row.get("host") != admin_host
        or row.get("service") != "reachable"
        or row.get("grant") != "revoked"
        or row.get("credential") != "absent"
        or row.get("configuration") != "absent"
    ):
        raise QualificationError(ReasonCode.GATE_FAILED)


def owner_call(
    installed: InstalledCandidate,
    context: CoreContext,
    path: Sequence[str],
    payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    arguments = [
        str(installed.cli),
        "--installation-state",
        str(context.installation),
        "--workspace-id",
        context.workspace_id,
        *path,
    ]
    if payload is not None:
        arguments.extend(
            ["--input-json", json.dumps(payload, sort_keys=True, separators=(",", ":"))]
        )
    if path[0] != "service":
        arguments.extend(["--principal", "local-user"])
    arguments.append("--json")
    envelope = _output_document(_run_text(arguments))
    if path[0] == "service":
        return envelope
    result = envelope.get("result")
    if not isinstance(result, dict) or "error" in envelope:
        raise QualificationError(ReasonCode.GATE_FAILED)
    return result


def owner_rows(
    installed: InstalledCandidate,
    context: CoreContext,
    path: Sequence[str],
    payload: Mapping[str, Any],
    field: str,
) -> list[object]:
    result = owner_call(installed, context, path, payload)
    rows = result.get(field)
    if not isinstance(rows, list):
        raise QualificationError(ReasonCode.GATE_FAILED)
    return rows


def _service_health(installed: InstalledCandidate, context: CoreContext) -> bool:
    return owner_call(installed, context, ("service", "health")).get("status") == "pass"


def core_healthy(installed: InstalledCandidate, context: CoreContext) -> bool:
    """Health of the Core this run expects, never of a replacement for it.

    The probe runs through a managed-local client, which starts a replacement for a
    Core that exited.  So the expected Core must first be alive and named by the
    descriptor, and still be after the probe.
    """
    return core_alive(context) and _service_health(installed, context) and core_alive(context)


def restart_core(installed: InstalledCandidate, context: CoreContext) -> None:
    """Crash the expected Core and let a managed-local client replace it.

    The one deliberate change of the expected Core: it must still be the one this
    context started, and the replacement becomes the expected Core.  I-6 and I-7
    recover from a crash, not a shutdown, so the whole group gets SIGKILL with no
    TERM first, and only a child that died of that signal counts.  A group that
    outlives the bound is retained.
    """
    process = context.process
    if process is None or process.poll() is not None or not core_alive(context):
        raise QualificationError(ReasonCode.GATE_FAILED)
    first_pid = process.pid
    group = _process_group(first_pid)
    if group is None:
        raise QualificationError(ReasonCode.GATE_FAILED)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if not _wait_group_absent(group):
        context.retained = True
        raise QualificationError(ReasonCode.GATE_FAILED)
    if process.poll() != -signal.SIGKILL:
        raise QualificationError(ReasonCode.GATE_FAILED)
    if not _service_health(installed, context):
        raise QualificationError(ReasonCode.GATE_FAILED)
    deadline = time.monotonic() + CORE_TIMEOUT
    while time.monotonic() < deadline:
        replacement = _ready_process(context)
        pid = None if replacement is None else replacement.get("pid")
        if type(pid) is int and pid != first_pid:
            context.replacement_pid = pid
            context.expected = dict(replacement or {})
            if not core_alive(context):
                raise QualificationError(ReasonCode.GATE_FAILED)
            return
        time.sleep(0.05)
    raise QualificationError(ReasonCode.GATE_FAILED)


def read_only_database(database: Path) -> sqlite3.Connection:
    """Open a stopped Core's database for inspection, with no write, lock or side file.

    ``mode=ro`` alone still creates the ``-wal`` and ``-shm`` files of a WAL
    database, so ``immutable=1`` is added: SQLite then reads the main file alone and
    takes no lock.  That is sound only once its writer has stopped cleanly and
    checkpointed, so a ``-wal`` or ``-journal`` still present, whose content an
    immutable read would skip, is refused rather than read around.
    """
    if any(os.path.lexists(f"{database}{suffix}") for suffix in ("-wal", "-journal")):
        raise QualificationError(ReasonCode.GATE_FAILED)
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def stage_source(installed: InstalledCandidate, context: CoreContext) -> Mapping[str, Any]:
    source = context.root / "staged-source.txt"
    source.write_text(f"staged import {QUALIFICATION_TOKEN}\n", encoding="utf-8")
    captured = _output_document(
        _run_text(
            [
                str(installed.service),
                "--workspace",
                str(context.workspace),
                "--installation-state",
                str(context.installation),
                "--capture-source",
                str(source),
                "--source-id",
                STAGED_SOURCE_ID,
                "--media-type",
                "text/plain",
            ]
        )
    )
    if captured.get("status") != "captured":
        raise QualificationError(ReasonCode.GATE_FAILED)
    try:
        with contextlib.closing(read_only_database(context.database)) as connection:
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
    except sqlite3.Error:
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    if row is None:
        raise QualificationError(ReasonCode.GATE_FAILED)
    descriptor: dict[str, Any] = {
        "staged_source_ref": row[0],
        "source_kind": row[1],
        "content_checksum": row[2],
        "content_length_bytes": row[3],
        "media_type": row[4],
    }
    if row[5] is not None:
        descriptor["source_version"] = row[5]
    return descriptor


def imported_job(context: CoreContext) -> str:
    deadline = time.monotonic() + CORE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with contextlib.closing(read_only_database(context.database)) as connection:
                rows = connection.execute(
                    "SELECT j.job_id, j.state FROM omnivia_durable_jobs j "
                    "JOIN omnivia_application_import_claims c ON c.job_id = j.job_id "
                    "AND c.workspace_id = ? ORDER BY j.job_id",
                    (context.workspace_id,),
                ).fetchall()
        except sqlite3.Error:
            rows = []
        if len(rows) == 1 and rows[0][1] == "succeeded" and isinstance(rows[0][0], str):
            return str(rows[0][0])
        if len(rows) > 1:
            break
        time.sleep(0.05)
    raise QualificationError(ReasonCode.GATE_FAILED)


def inspect_settled_import_job(
    installed: InstalledCandidate,
    context: CoreContext,
    progress: Callable[[str], None] = lambda _stage: None,
) -> str:
    """Read the durable import only while Core is not the database owner."""
    stop_core(context)
    if context.retained:
        raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE)
    progress("import_core_stopped_for_inspection")
    job_id = imported_job(context)
    start_core(installed, context)
    progress("import_core_restarted")
    return job_id


# --- deterministic real-host journey --------------------------------------


def _tool_prompt(tool: str, arguments: Mapping[str, Any], marker: str) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return (
        "This is an isolated MCP qualification step. Use only the configured "
        f"{SERVER_KEY} MCP server. Call exactly one tool named {tool}; it may be "
        f"displayed as {claude_tool_name(tool)}. Use it now with exactly the JSON "
        "arguments below; do not add, remove, rewrite or infer values. "
        "Do not call another tool. Whether the tool succeeds or returns an error, "
        f"after it finishes output exactly {marker} and nothing else.\nJSON:{payload}"
    )


def _outcomes(summary: ObservationSummary, tool: str) -> list[tuple[str, str]]:
    """The (refusal class, result digest) of each answered call to ``tool``, in order."""
    return [(refusal, digest) for name, refusal, digest in summary.outcomes if name == tool]


def _single_outcome(result: HostRunResult, tool: str) -> tuple[str, str]:
    outcomes = _outcomes(result.summary, tool)
    if len(outcomes) != 1:
        raise QualificationError(ReasonCode.GATE_FAILED)
    return outcomes[0]


def _excluded_probe_payload(tools: Sequence[str]) -> bytes:
    """Initialize, list, then one ``tools/call`` per excluded name, all deterministic."""
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "omnivia-qualification", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    for number, tool in enumerate(tools, start=3):
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": number,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {}},
            }
        )
    return b"".join(
        json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for message in messages
    )


def probe_excluded_tools(
    installed: InstalledCandidate,
    core_config: Path,
    root: Path,
    host: str,
    tools: tuple[str, ...],
    excluded: Sequence[str],
    *,
    run: ProtocolRunner = _run_protocol,
) -> None:
    """Dispatch every excluded name at the real server and require each allow-list refusal.

    No model is involved: the harness itself issues each ``tools/call``, so a
    profile is proven to refuse every excluded name, not only the ones a model
    happened to try.  The server answers each name ``not_exposed`` with no
    structured result, and the proxy must observe exactly those answers.
    """
    layout = host_layout(root, host)
    create_layout(layout)
    observation = layout.root / "observation.jsonl"
    spec = layout.root / "proxy-spec.json"
    child = mcp_server_entry(installed.mcp, core_config)
    try:
        write_proxy_spec(
            spec,
            child=[child["command"], *child["args"]],
            observation=observation,
            interruption=None,
        )
        proxy = proxy_server_entry(installed.python, spec)
        completed = run(
            [proxy["command"], *proxy["args"]],
            _excluded_probe_payload(excluded),
            host_environment(layout, installed.python),
            layout.workspace,
            CORE_TIMEOUT,
        )
        if (
            completed.returncode != 0
            or not completed.stdout
            or len(completed.stdout) > MAX_PROTOCOL_OUTPUT_BYTES
        ):
            raise QualificationError(ReasonCode.GATE_FAILED)
        summary = summarize_observation(read_observation(observation))
    except (OSError, subprocess.SubprocessError):
        raise QualificationError(ReasonCode.GATE_FAILED) from None
    # Answers may arrive in any order; each name must be seen exactly once.
    expected_requests = sorted((name, arguments_digest({})) for name in excluded)
    expected_outcomes = sorted(
        (name, "not_exposed", canonical_result_digest(None)) for name in excluded
    )
    if (
        summary.violation
        or not summary.initialized
        or not summary.listed
        or summary.listed_tools != tools
        or not set(excluded).isdisjoint(summary.listed_tools)
        or list(summary.called) != list(excluded)
        or sorted(summary.requests) != expected_requests
        or sorted(summary.responded) != sorted(excluded)
        or summary.succeeded
        or sorted(summary.tool_errors) != sorted(excluded)
        or sorted(summary.outcomes) != expected_outcomes
    ):
        raise QualificationError(ReasonCode.GATE_FAILED)


@dataclass
class HostDriver:
    host: str
    binary: Path
    auth: AuthSource
    installed: InstalledCandidate
    core_config: Path
    root: Path
    tools: tuple[str, ...]
    progress: Callable[[str], None] = lambda _stage: None
    #: Core's own health, read after every host process exits.
    healthy: Callable[[], bool] = lambda: True
    sequence: int = 0
    protocol_clean: bool = True

    def _run(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        interrupt: bool = False,
        on_withheld: Callable[[], None] = lambda: None,
        pause_before: bool = False,
        on_paused: Callable[[], None] = lambda: None,
        capture: bool = False,
    ) -> HostRunResult:
        """Run one fresh host process whose prompt asks for one call to ``tool``."""
        self.sequence += 1
        marker = f"OMNIVIA_MCP_QUALIFICATION_STEP_{self.sequence}_DONE"
        layout = host_layout(self.root / f"session-{self.sequence:02d}", self.host)
        digest = arguments_digest(arguments)
        interruption = Interruption(tool, digest) if interrupt else None
        pause: PauseBefore | None = None
        release = layout.root / "release-request"
        if pause_before:
            pause = PauseBefore(tool, digest, release)
        handed_off = CaptureTarget(tool, digest, layout.root / "continuation-handoff.json") if capture else None

        def release_request() -> None:
            on_paused()
            _write_private(release, "release\n")

        result = run_host_session(
            host=self.host,
            binary=self.binary,
            layout=layout,
            installed=self.installed,
            core_config=self.core_config,
            auth=self.auth,
            prompt=_tool_prompt(tool, arguments, marker),
            marker=marker,
            tools=(tool,),
            timeout=HOST_TIMEOUT,
            interruption=interruption,
            pause_before=pause,
            capture=handed_off,
            on_withheld=on_withheld,
            on_paused=release_request if pause_before else on_paused,
        )
        summary = result.summary
        self.protocol_clean = self.protocol_clean and not summary.violation
        if summary.violation:
            self.progress("host_protocol_violation")
            raise QualificationError(ReasonCode.PROTOCOL_VIOLATION)
        if not self.healthy():
            self.progress("core_unhealthy_after_host_exit")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if not summary.initialized:
            self.progress("host_initialize_missing")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if not summary.listed:
            self.progress("host_inventory_missing")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if summary.listed_tools != self.tools:
            self.progress("host_inventory_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        return result

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        expected_error: bool = False,
        refusal: str | None = None,
        interrupt: bool = False,
        on_withheld: Callable[[], None] = lambda: None,
        pause_before: bool = False,
        on_paused: Callable[[], None] = lambda: None,
        capture: bool = False,
        _remaining_missing_retries: int = 2,
    ) -> HostRunResult:
        result = self._run(
            tool,
            arguments,
            interrupt=interrupt,
            on_withheld=on_withheld,
            pause_before=pause_before,
            on_paused=on_paused,
            capture=capture,
        )
        summary = result.summary
        digest = arguments_digest(arguments)
        if tool not in summary.called:
            self.progress("host_target_call_missing")
            if _remaining_missing_retries > 0:
                # The retry re-sends the same arguments, so a chained page keeps its prior token.
                return self.call(
                    tool,
                    arguments,
                    expected_error=expected_error,
                    refusal=refusal,
                    interrupt=interrupt,
                    on_withheld=on_withheld,
                    pause_before=pause_before,
                    on_paused=on_paused,
                    capture=capture,
                    _remaining_missing_retries=_remaining_missing_retries - 1,
                )
            raise QualificationError(ReasonCode.GATE_FAILED)
        if not set(summary.called).issubset({tool, *SAFE_AUXILIARY_TOOLS}):
            self.progress("host_call_set_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        target_requests = [request for request in summary.requests if request[0] == tool]
        if (
            not target_requests
            or any(request != (tool, digest) for request in target_requests)
            or len(summary.requests) != len(summary.called)
        ):
            self.progress("host_arguments_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if interrupt:
            if (
                not result.interrupted
                or not summary.withheld
                or tool in summary.responded
                or result.exited_cleanly
            ):
                self.progress("host_interruption_mismatch")
                raise QualificationError(ReasonCode.INTERRUPTION_BOUNDARY_UNOBSERVABLE)
            return result
        if (
            not result.exited_cleanly
            or not result.marker_seen
            or summary.withheld
            or result.paused != pause_before
            or summary.paused != pause_before
            or len(summary.responded) != len(summary.called)
        ):
            self.progress("host_completion_mismatch")
            raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS)
        if expected_error:
            if (
                summary.tool_errors.count(tool) != len(target_requests)
                or tool in summary.succeeded
            ):
                self.progress("host_expected_error_mismatch")
                raise QualificationError(ReasonCode.GATE_FAILED)
            if refusal is not None and [
                observed for observed, _ in _outcomes(summary, tool)
            ] != [refusal] * len(target_requests):
                self.progress("host_refusal_mismatch")
                raise QualificationError(ReasonCode.GATE_FAILED)
        elif (
            summary.succeeded.count(tool) != len(target_requests)
            or tool in summary.tool_errors
        ):
            self.progress("host_expected_success_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        return result

    def refuse_revoked(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        on_paused: Callable[[], None],
    ) -> HostRunResult:
        """One admitted session makes exactly ``tool(arguments)``, refused as credential_missing.

        The session is paused before its one request.  ``on_paused`` revokes the
        authority while the request is held, then the request is released.  Nothing is
        retried: a process started after revocation cannot initialize, and a retry
        that found no pause would run the request against live authority.
        """
        result = self._run(tool, arguments, pause_before=True, on_paused=on_paused)
        summary = result.summary
        digest = arguments_digest(arguments)
        if (
            not result.paused
            or not summary.paused
            or summary.initialized_after_pause
            or tuple(summary.requests) != ((tool, digest),)
            or tuple(summary.called) != (tool,)
            or [(name, refusal) for name, refusal, _ in summary.outcomes]
            != [(tool, "credential_missing")]
            or summary.succeeded
        ):
            self.progress("host_refusal_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if (
            not result.exited_cleanly
            or not result.marker_seen
            or summary.withheld
            or len(summary.responded) != len(summary.called)
        ):
            self.progress("host_completion_mismatch")
            raise QualificationError(ReasonCode.HOST_OUTPUT_AMBIGUOUS)
        return result


def host_read_pages(
    driver: HostDriver,
    tool: str,
    arguments: Mapping[str, Any],
    expected: Sequence[Mapping[str, Any]],
) -> None:
    """Read each owner page in order, with one fresh host session and one call per page.

    Page one is requested with ``arguments``.  Each later page carries the continuation
    token that the host's own preceding call returned, handed off by the proxy from that
    successful answer.  Owner tokens are bound to the owner principal and are never sent
    to the host; the owner pages say only how many pages there are and where they end.
    Each call must be the only call its session makes, must succeed, and must return
    exactly that owner page's digest.  The host's token must be present exactly when the
    owner page has a next page.  A missing target is retried by ``HostDriver.call``
    within its bound, with the same arguments; any other failure is refused at once.
    """
    request: Mapping[str, Any] = arguments
    for index, page in enumerate(expected):
        result = driver.call(tool, request, capture=True)
        summary = result.summary
        if (
            tuple(summary.called) != (tool,)
            or tuple(summary.requests) != ((tool, arguments_digest(request)),)
            or _single_outcome(result, tool) != ("none", canonical_result_digest(page))
        ):
            driver.progress("host_page_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if result.continuation is None or result.continuation.tool != tool:
            driver.progress("host_capture_missing")
            raise QualificationError(ReasonCode.GATE_FAILED)
        token = result.continuation.token
        if (token is None) != (_continuation(page) is None):
            driver.progress("host_continuation_mismatch")
            raise QualificationError(ReasonCode.GATE_FAILED)
        if index + 1 < len(expected):
            if token is None:
                driver.progress("host_continuation_mismatch")
                raise QualificationError(ReasonCode.GATE_FAILED)
            request = {**arguments, "page": {"continuation_token": token}}


def _capture_arguments(source: str, key: str, text: str) -> dict[str, Any]:
    return {
        "input": {
            "source_native_id": source,
            "media_type": "text/markdown",
            "text": text,
        },
        "idempotency_key": key,
    }


def _memory_arguments(principal: str, fact: str = f"real host fact {QUALIFICATION_TOKEN}") -> dict[str, Any]:
    source = {"kind": "direct_submission", "source_id": DIRECT_SOURCE_ID}
    return {
        "input": {
            "record_type": "memory.fact",
            "domain_scope": "product.core",
            "content": {"fact": fact},
            "evidence_disposition": "available",
            "sources": [source],
            "assertion": {
                "actor_id": principal,
                "actor_kind": "agent",
                "actor_role": "author",
                "asserted_at": "2026-10-03T00:00:00Z",
                "evidence": [{"source": source}],
            },
        },
        "idempotency_key": MEMORY_KEY,
    }


def _record_true(ledger: GateLedger, gate: str, *checks: str) -> None:
    for check in checks:
        ledger.record(gate, check, True, source=Evidence.INDEPENDENT)


AFTER_REVOKE_SOURCE_ID: Final = f"{DIRECT_SOURCE_ID}-after-revoke"
AFTER_REVOKE_KEY: Final = f"{CAPTURE_KEY}-after-revoke"


def _require(condition: bool) -> None:
    if not condition:
        raise QualificationError(ReasonCode.GATE_FAILED)


def _count(
    installed: InstalledCandidate,
    context: CoreContext,
    path: Sequence[str],
    payload: Mapping[str, Any],
    field: str,
) -> int:
    return len(owner_rows(installed, context, path, payload, field))


def _memory_counts(installed: InstalledCandidate, context: CoreContext) -> tuple[int, int]:
    """Owner-observed (default view, candidate view) memory counts for the journey token."""
    default = _count(
        installed, context, ("memory", "search"), {"query": QUALIFICATION_TOKEN}, "records"
    )
    candidates = _count(
        installed,
        context,
        ("memory", "search"),
        {"query": QUALIFICATION_TOKEN, "view": "candidates"},
        "records",
    )
    return default, candidates


def _journey_counts(installed: InstalledCandidate, context: CoreContext) -> tuple[int, ...]:
    """Every durable row the journey may create, as the owner counts them."""

    def evidence(query: str) -> int:
        return _count(installed, context, ("evidence", "search"), {"query": query}, "evidence")

    return (
        evidence(DIRECT_SOURCE_ID),
        evidence(INTERRUPTED_SOURCE_ID),
        evidence(AFTER_REVOKE_SOURCE_ID),
        *_memory_counts(installed, context),
    )


def owner_event_pages(
    installed: InstalledCandidate, context: CoreContext, job_id: str, limit: int
) -> list[Mapping[str, Any]]:
    """Walk every page of a job's events as the owner, following each continuation token."""
    pages: list[Mapping[str, Any]] = []
    payload: dict[str, Any] = {"job_id": job_id, "limit": limit}
    while True:
        page = owner_call(installed, context, ("job", "events"), payload)
        pages.append(page)
        token = _continuation(page)
        if token is None:
            return pages
        _require(len(pages) < MAX_EVENT_PAGES)
        payload = {"job_id": job_id, "limit": limit, "page": {"continuation_token": token}}


def _continuation(page: Mapping[str, Any]) -> str | None:
    position = page.get("page")
    if not isinstance(position, dict):
        raise QualificationError(ReasonCode.GATE_FAILED)
    token = position.get("continuation_token")
    if token is None:
        return None
    if not isinstance(token, str) or not token:
        raise QualificationError(ReasonCode.GATE_FAILED)
    return token


def verified_event_stream(pages: Sequence[Mapping[str, Any]], limit: int) -> list[object]:
    """Return the events of one stable snapshot, contiguous and ordered, or refuse."""
    snapshots = {page.get("snapshot_event_count") for page in pages}
    _require(len(snapshots) == 1)
    snapshot = snapshots.pop()
    _require(type(snapshot) is int)
    events: list[object] = []
    for page in pages:
        rows = page.get("events")
        if not isinstance(rows, list) or len(rows) > limit:
            raise QualificationError(ReasonCode.GATE_FAILED)
        sequences = [row.get("sequence") if isinstance(row, dict) else None for row in rows]
        _require(sequences == list(range(len(events), len(events) + len(rows))))
        events.extend(rows)
    _require(len(events) == snapshot)
    return events


def holds_imported_artifact(
    result: Mapping[str, Any], job_id: str, staged: Mapping[str, Any]
) -> bool:
    """Whether one complete evidence page holds exactly one artifact of import ``job_id``.

    The trusted staging capture is itself evidence of the staged bytes' kind,
    checksum and media type, so only the binding to the run tells the import's
    artifact apart.  The import publishes its own source identity, so the staged
    source id is never assumed.  A page that continues is refused: "exactly one"
    would then say nothing about the pages not read.
    """
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or _continuation(result) is not None:
        return False
    imported = [
        artifact
        for artifact in evidence
        if isinstance(artifact, dict) and artifact.get("import_run_id") == job_id
    ]
    if len(imported) != 1:
        return False
    source = imported[0].get("source")
    return (
        isinstance(source, dict)
        and source.get("kind") == staged["source_kind"]
        and imported[0].get("content_checksum") == staged["content_checksum"]
        and imported[0].get("media_type") == staged["media_type"]
    )


@dataclass(frozen=True)
class ImportJourney:
    context: CoreContext
    driver: HostDriver
    arguments: Mapping[str, Any]
    job_id: str
    events: list[object]


def _direct_mutations(
    installed: InstalledCandidate,
    context: CoreContext,
    authoring: HostDriver,
    principal: str,
    ledger: GateLedger,
) -> None:
    """Capture and memory: exact replays keep one effect; changed payloads conflict.

    A search counts only when the host's canonical result digest equals the digest
    of the owner's observation of the same search.  Only digests are kept.
    """
    capture = _capture_arguments(
        DIRECT_SOURCE_ID, CAPTURE_KEY, f"real host captured note {QUALIFICATION_TOKEN}\n"
    )
    first = _single_outcome(authoring.call("evidence_capture", capture), "evidence_capture")
    evidence = _single_outcome(
        authoring.call("evidence_search", {"query": QUALIFICATION_TOKEN}), "evidence_search"
    )
    authoring.progress("capture_search_host_ok")
    owner_evidence = owner_call(
        installed, context, ("evidence", "search"), {"query": QUALIFICATION_TOKEN}
    )
    _require(evidence == ("none", canonical_result_digest(owner_evidence)))
    _require(
        _count(installed, context, ("evidence", "search"), {"query": DIRECT_SOURCE_ID}, "evidence")
        == 1
    )
    _record_true(ledger, "i4", "capture_and_search")

    replay = _single_outcome(authoring.call("evidence_capture", capture), "evidence_capture")
    _require(replay == first)
    _require(
        _count(installed, context, ("evidence", "search"), {"query": DIRECT_SOURCE_ID}, "evidence")
        == 1
    )
    _record_true(ledger, "i4", "capture_replay_stable")
    changed = _capture_arguments(
        DIRECT_SOURCE_ID, CAPTURE_KEY, f"changed real host captured note {QUALIFICATION_TOKEN}\n"
    )
    authoring.call("evidence_capture", changed, expected_error=True, refusal="idempotency_conflict")
    _require(
        _count(installed, context, ("evidence", "search"), {"query": DIRECT_SOURCE_ID}, "evidence")
        == 1
    )
    _record_true(ledger, "i4", "capture_changed_conflict")

    created = _single_outcome(authoring.call("memory_create", _memory_arguments(principal)), "memory_create")
    default_host = _single_outcome(
        authoring.call("memory_search", {"query": QUALIFICATION_TOKEN}), "memory_search"
    )
    candidate_host = _single_outcome(
        authoring.call("memory_search", {"query": QUALIFICATION_TOKEN, "view": "candidates"}),
        "memory_search",
    )
    authoring.progress("memory_search_hosts_ok")
    default_owner = owner_call(installed, context, ("memory", "search"), {"query": QUALIFICATION_TOKEN})
    candidate_owner = owner_call(
        installed,
        context,
        ("memory", "search"),
        {"query": QUALIFICATION_TOKEN, "view": "candidates"},
    )
    _require(default_host == ("none", canonical_result_digest(default_owner)))
    _require(candidate_host == ("none", canonical_result_digest(candidate_owner)))
    _require(_memory_counts(installed, context) == (0, 1))
    _record_true(ledger, "i4", "proposed_memory", "default_invisible", "candidate_visible")

    replayed = _single_outcome(authoring.call("memory_create", _memory_arguments(principal)), "memory_create")
    _require(replayed == created)
    _require(_memory_counts(installed, context) == (0, 1))
    _record_true(ledger, "i4", "memory_replay_stable")
    authoring.call(
        "memory_create",
        _memory_arguments(principal, f"changed real host fact {QUALIFICATION_TOKEN}"),
        expected_error=True,
        refusal="idempotency_conflict",
    )
    _require(_memory_counts(installed, context) == (0, 1))
    _record_true(ledger, "i4", "memory_changed_conflict")


def _import_journey(
    installed: InstalledCandidate,
    run_root: Path,
    host: str,
    binary: Path,
    auth: AuthSource,
    ledger: GateLedger,
    progress: Callable[[str], None],
) -> ImportJourney:
    """Import one staged source; the import Core stays up for the rest of the run."""
    context = initialize_core(installed, run_root / "import-core")
    try:
        staged = stage_source(installed, context)
        start_core(installed, context)
        config = configure_profile(installed, context, host, "authoring")
        driver = HostDriver(
            host,
            binary,
            auth,
            installed,
            config,
            run_root / "import-host",
            AUTHORING_TOOLS,
            progress,
            healthy=lambda: core_healthy(installed, context),
        )
        arguments = {"input": {"source": dict(staged)}, "idempotency_key": IMPORT_KEY}
        started = _single_outcome(driver.call("import_start", arguments), "import_start")
        replayed = _single_outcome(driver.call("import_start", arguments), "import_start")
        _require(replayed == started)
        _record_true(ledger, "i5", "staged_import", "import_replay_stable")
        changed = {
            "input": {
                "source": {**staged, "content_length_bytes": int(staged["content_length_bytes"]) + 1}
            },
            "idempotency_key": IMPORT_KEY,
        }
        driver.call("import_start", changed, expected_error=True, refusal="idempotency_conflict")
        _record_true(ledger, "i5", "import_changed_conflict")
        # The runtime owns the database while serving; inspection stops Core and
        # also proves exactly one durable job exists.
        job_id = inspect_settled_import_job(installed, context, progress)

        owner_job = owner_call(installed, context, ("job", "get"), {"job_id": job_id})
        job = owner_job.get("job")
        _require(isinstance(job, dict) and job.get("state") == "succeeded")
        read = _single_outcome(driver.call("job_get", {"job_id": job_id}), "job_get")
        _require(read[1] == canonical_result_digest(owner_job))
        _record_true(ledger, "i5", "job_observed")

        pages = owner_event_pages(installed, context, job_id, IMPORT_PAGE_SIZE)
        events = verified_event_stream(pages, IMPORT_PAGE_SIZE)
        _require(len(pages) > 1)
        unpaged = owner_call(installed, context, ("job", "events"), {"job_id": job_id}).get("events")
        _require(unpaged == events)
        host_read_pages(driver, "job_events", {"job_id": job_id, "limit": IMPORT_PAGE_SIZE}, pages)
        _record_true(ledger, "i5", "job_events_paged", "job_events_match_owner")

        # The staged source's kind matches the staging capture and the import's own
        # artifact alike; the bounded page is compared whole, owner against host.
        evidence_query = {"query": str(staged["source_kind"]), "limit": IMPORTED_EVIDENCE_LIMIT}
        owner_evidence = owner_call(installed, context, ("evidence", "search"), evidence_query)
        retrieved = _single_outcome(driver.call("evidence_search", evidence_query), "evidence_search")
        _require(retrieved == ("none", canonical_result_digest(owner_evidence)))
        _require(holds_imported_artifact(owner_evidence, job_id, staged))
        _record_true(ledger, "i5", "imported_evidence_retrieved")
        return ImportJourney(context, driver, arguments, job_id, events)
    except BaseException:
        stop_core(context)
        if context.retained:
            print(f"reason_code={ReasonCode.CLEANUP_INCOMPLETE.value}", file=sys.stderr)
        raise


def _ambiguous_response(
    installed: InstalledCandidate,
    context: CoreContext,
    authoring: HostDriver,
    ledger: GateLedger,
) -> None:
    """A committed response is withheld, the host exits, Core restarts, then the key is replayed."""
    arguments = _capture_arguments(
        INTERRUPTED_SOURCE_ID, INTERRUPTED_KEY, f"interrupted response note {QUALIFICATION_TOKEN}\n"
    )
    committed = False

    def observe_commit() -> None:
        nonlocal committed
        if (
            _count(
                installed,
                context,
                ("evidence", "search"),
                {"query": INTERRUPTED_SOURCE_ID},
                "evidence",
            )
            != 1
        ):
            raise QualificationError(ReasonCode.INTERRUPTION_BOUNDARY_UNOBSERVABLE)
        committed = True

    interrupted = authoring.call(
        "evidence_capture", arguments, interrupt=True, on_withheld=observe_commit
    )
    withheld = interrupted.summary.withheld_digest
    _require(committed and interrupted.interrupted and withheld is not None)
    _record_true(ledger, "i6", "commit_observed_before_response", "host_stopped_before_response")

    restart_core(installed, context)
    authoring.progress("core_restarted")
    _record_true(ledger, "i7", "core_restart_observed")
    _record_true(ledger, "i6", "core_restarted_before_replay")

    first = _single_outcome(authoring.call("evidence_capture", arguments), "evidence_capture")
    second = _single_outcome(authoring.call("evidence_capture", arguments), "evidence_capture")
    # Each replay answers exactly what the withheld response would have said.
    _require(first == second == ("none", withheld))
    _require(
        _count(
            installed,
            context,
            ("evidence", "search"),
            {"query": INTERRUPTED_SOURCE_ID},
            "evidence",
        )
        == 1
    )
    _record_true(ledger, "i6", "same_key_replayed", "single_durable_effect")
    _record_true(ledger, "i7", "host_restart_observed")

    authoring.call(
        "evidence_capture",
        _capture_arguments(
            INTERRUPTED_SOURCE_ID,
            INTERRUPTED_KEY,
            f"changed interrupted response note {QUALIFICATION_TOKEN}\n",
        ),
        expected_error=True,
        refusal="idempotency_conflict",
    )
    _require(
        _count(
            installed,
            context,
            ("evidence", "search"),
            {"query": INTERRUPTED_SOURCE_ID},
            "evidence",
        )
        == 1
    )
    _record_true(ledger, "i6", "changed_input_conflict")


def _revocation(
    installed: InstalledCandidate,
    context: CoreContext,
    host: str,
    authoring: HostDriver,
    principal: str,
    imported: ImportJourney,
    ledger: GateLedger,
) -> None:
    """Revoke each authority while its own request is held, then prove each request fails closed.

    Every post-revocation request runs in its own admitted host session.  The session
    is paused before that one request, the authority is revoked while it is held, and
    only then is the request released and required to be refused as credential_missing.
    Between sessions the same configuration path is granted again under a rotated
    principal, so the next host can initialize before its own pause.  Replayed requests
    keep the principal the owner recorded as their actor.  The final revocation is left
    in force and re-verified for both contexts.
    """
    before = _journey_counts(installed, context)
    # The principal the next paused session is admitted under; each revocation ends it.
    admitted = principal

    def revoke_primary() -> None:
        revoke_authoring(installed, context, host)
        verify_revoked(installed, context, host)

    authoring.refuse_revoked(
        "evidence_capture",
        _capture_arguments(AFTER_REVOKE_SOURCE_ID, AFTER_REVOKE_KEY, "must not settle"),
        on_paused=revoke_primary,
    )
    _require(_journey_counts(installed, context) == before)
    _record_true(ledger, "i8", "mutation_fail_closed")
    admitted = _regrant(installed, context, host, authoring, admitted)
    authoring.refuse_revoked(
        "evidence_capture",
        _capture_arguments(
            INTERRUPTED_SOURCE_ID,
            INTERRUPTED_KEY,
            f"interrupted response note {QUALIFICATION_TOKEN}\n",
        ),
        on_paused=revoke_primary,
    )
    _require(_journey_counts(installed, context) == before)
    admitted = _regrant(installed, context, host, authoring, admitted)
    authoring.refuse_revoked("memory_create", _memory_arguments(principal), on_paused=revoke_primary)
    _require(_journey_counts(installed, context) == before)

    # The import principal is a separate authoring context with its own Core.  Its
    # sessions are admitted before their revocations, which land on each paused request.
    import_admitted = configuration_principal(imported.driver.core_config)

    def revoke_import() -> None:
        revoke_authoring(installed, imported.context, host)
        verify_revoked(installed, imported.context, host)

    imported.driver.refuse_revoked(
        "job_get", {"job_id": imported.job_id}, on_paused=revoke_import
    )
    import_admitted = _regrant(
        installed, imported.context, host, imported.driver, import_admitted
    )
    imported.driver.refuse_revoked(
        "job_events", {"job_id": imported.job_id}, on_paused=revoke_import
    )
    _record_true(ledger, "i8", "job_reads_fail_closed")
    import_admitted = _regrant(
        installed, imported.context, host, imported.driver, import_admitted
    )
    imported.driver.refuse_revoked("import_start", imported.arguments, on_paused=revoke_import)
    # Every same-key replay (the interrupted capture, the memory and the import start) is refused.
    _record_true(ledger, "i8", "replay_fail_closed")

    owner_job = owner_call(installed, imported.context, ("job", "get"), {"job_id": imported.job_id})
    job = owner_job.get("job")
    owner_events = owner_call(
        installed, imported.context, ("job", "events"), {"job_id": imported.job_id}
    ).get("events")
    _require(isinstance(job, dict) and job.get("state") == "succeeded")
    _require(owner_events == imported.events)
    _record_true(ledger, "i8", "owner_job_observed_after_revoke")
    _require(core_healthy(installed, context) and core_healthy(installed, imported.context))
    verify_revoked(installed, context, host)
    verify_revoked(installed, imported.context, host)
    _record_true(ledger, "i8", "authoring_revoked", "core_healthy")


def _regrant(
    installed: InstalledCandidate,
    context: CoreContext,
    host: str,
    driver: HostDriver,
    revoked: str,
) -> str:
    """Install fresh authority for the same authoring profile; return its principal.

    The service rotates the MCP principal on every configure after a revoke: a new
    bearer under a new principal id.  The configuration path is deterministic and must
    not move, and the new principal must differ from the revoked one, which is the
    proof that the authority is fresh rather than the revoked grant reinstated.
    configure prints only after its own handshake through the real server entry point.
    """
    config = configure_profile(installed, context, host, "authoring")
    principal = configuration_principal(config)
    _require(config == driver.core_config and principal != revoked)
    return principal


def _decision_arguments() -> dict[str, Any]:
    """The restricted profile's one mutation, in the shape the installed service admits."""
    subject = f"{QUALIFICATION_TOKEN}-subject"
    return {
        "input": {
            "schema_version": "decision.1",
            "definition_ref": {"id": "core.document_category", "version": "1.0.0"},
            "subject_refs": [{"id": subject, "revision": "r1"}],
            "input": {"source_refs": [{"id": subject, "revision": "r1"}]},
            "execution": {"mode": "advisory", "privacy": "local_only"},
        },
        "idempotency_key": DECISION_KEY,
    }


def _restricted_decision(
    installed: InstalledCandidate,
    context: CoreContext,
    restricted: HostDriver,
    ledger: GateLedger,
) -> None:
    """The host's refusal of ``decision.evaluate`` must match the owner's view of the workspace.

    With no decision capability granted, the service refuses the call with
    ``capability_not_granted``.  The host must observe exactly that refusal, and
    the owner must observe a disabled decision surface and no decision record.
    Audit or other operational bookkeeping is outside this narrowly stated proof.
    """
    restricted.call(
        "decision_evaluate",
        _decision_arguments(),
        expected_error=True,
        refusal="capability_not_granted",
    )
    _record_true(ledger, "i3", "decision_evaluate_refused")
    status = owner_call(installed, context, ("decisions", "status"), {})
    records = owner_call(installed, context, ("decisions", "records"), {}).get("records")
    _require(status.get("enabled") is False and records == [])
    _record_true(ledger, "i3", "decision_owner_observed")


def _probe_excluded(
    installed: InstalledCandidate,
    host: str,
    config: Path,
    profile: str,
    root: Path,
) -> None:
    """Probe one profile's excluded names while its configuration is the current one.

    Each ``configure`` rewrites the host's single MCP configuration, so the probe
    must run before the other profile is configured, never after.  The harness
    issues each ``tools/call`` itself, so the proof needs no model.
    """
    probe_excluded_tools(
        installed,
        config,
        root / profile,
        host,
        PROFILE_TOOLS[profile],
        EXCLUDED_TOOLS[profile],
    )


def qualify_host(
    *,
    host: str,
    binary: Path,
    auth: AuthSource,
    installed: InstalledCandidate,
    run_root: Path,
    progress: Callable[[str], None] = lambda _stage: None,
    ledger: GateLedger | None = None,
) -> GateLedger:
    ledger = GateLedger() if ledger is None else ledger
    _record_true(ledger, "i1", "candidate_installed", "entrypoints_resolved")
    context = initialize_core(installed, run_root / "core")
    imported: ImportJourney | None = None
    failed = False
    try:
        progress("core_initialized")
        start_core(installed, context)
        progress("core_started")
        restricted_config = configure_profile(installed, context, host, "restricted")
        progress("restricted_configured")
        _record_true(ledger, "i2", "restricted_configured")
        restricted = HostDriver(
            host,
            binary,
            auth,
            installed,
            restricted_config,
            run_root / "restricted-host",
            RESTRICTED_TOOLS,
            progress,
            healthy=lambda: core_healthy(installed, context),
        )
        restricted.call("workspace_inspect", {})
        progress("restricted_host_ok")
        _record_true(ledger, "i3", "initialize_verified", "restricted_tools_exact")
        _restricted_decision(installed, context, restricted, ledger)
        progress("restricted_decision_ok")
        _probe_excluded(installed, host, restricted_config, "restricted", run_root / "excluded-probe")
        _record_true(
            ledger,
            "i3",
            "restricted_excluded_tools_absent",
            "restricted_excluded_tool_undispatchable",
        )
        progress("restricted_excluded_refused")

        authoring_config = configure_profile(installed, context, host, "authoring")
        progress("authoring_configured")
        principal = configuration_principal(authoring_config)
        _record_true(ledger, "i2", "authoring_configured")
        authoring = HostDriver(
            host,
            binary,
            auth,
            installed,
            authoring_config,
            run_root / "authoring-host",
            AUTHORING_TOOLS,
            progress,
            healthy=lambda: core_healthy(installed, context),
        )
        if owner_rows(
            installed,
            context,
            ("evidence", "search"),
            {"query": QUALIFICATION_TOKEN},
            "evidence",
        ) or owner_rows(
            installed,
            context,
            ("memory", "search"),
            {"query": QUALIFICATION_TOKEN},
            "records",
        ):
            raise QualificationError(ReasonCode.GATE_FAILED)
        _direct_mutations(installed, context, authoring, principal, ledger)
        _record_true(ledger, "i3", "authoring_tools_exact")
        progress("direct_mutations_ok")

        _probe_excluded(installed, host, authoring_config, "authoring", run_root / "excluded-probe")
        _record_true(
            ledger,
            "i3",
            "authoring_excluded_tools_absent",
            "authoring_excluded_tool_undispatchable",
        )
        progress("excluded_tool_refused")

        imported = _import_journey(
            installed, run_root / "import", host, binary, auth, ledger, progress
        )
        progress("import_journey_ok")
        _ambiguous_response(installed, context, authoring, ledger)
        progress("ambiguous_response_ok")
        _revocation(installed, context, host, authoring, principal, imported, ledger)
        progress("revocation_ok")
        if not (
            restricted.protocol_clean
            and authoring.protocol_clean
            and imported.driver.protocol_clean
        ):
            raise QualificationError(ReasonCode.PROTOCOL_VIOLATION)
        _record_true(ledger, "i7", "stdout_protocol_only")
    except BaseException:
        failed = True
        raise
    finally:
        if imported is not None:
            stop_core(imported.context)
        stop_core(context)
        if failed and (
            context.retained or (imported is not None and imported.context.retained)
        ):
            print(f"reason_code={ReasonCode.CLEANUP_INCOMPLETE.value}", file=sys.stderr)
    # Teardown runs first: a Core process that survived its stop is never a pass.
    if context.retained or (imported is not None and imported.context.retained):
        raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE)
    return ledger


def os_identity(
    *,
    mac_version: Callable[[], tuple[str, tuple[str, ...], str]] = platform.mac_ver,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> OsIdentity:
    version = mac_version()[0]
    try:
        completed = run(
            ["/usr/bin/sw_vers", "-buildVersion"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationError(ReasonCode.PLATFORM_UNSUPPORTED) from None
    build = completed.stdout.strip()
    if (
        completed.returncode != 0
        or not re.fullmatch(r"[0-9]{1,3}[A-Z][0-9]{1,5}[a-z]?", build)
        or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version)
        or version != SUPPORTED_OS_VERSION
        or build != SUPPORTED_OS_BUILD
    ):
        raise QualificationError(ReasonCode.PLATFORM_UNSUPPORTED)
    return OsIdentity(version, build, platform.machine().lower())


def installed_from_prefix(prefix: Path) -> InstalledCandidate:
    scripts = prefix / "bin"
    installed = InstalledCandidate(
        prefix,
        scripts / "python",
        scripts / "omnivia-core-service",
        scripts / "omnivia",
        scripts / "omnivia-core-mcp",
    )
    resolved = prefix.resolve()
    if not installed.python.is_file() or not os.access(installed.python, os.X_OK):
        raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
    for path in (installed.service, installed.cli, installed.mcp):
        if (
            path.is_symlink()
            or not path.is_file()
            or not os.access(path, os.X_OK)
            or not path.resolve().is_relative_to(resolved)
        ):
            raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
    return installed


# --- runtime cleanup -------------------------------------------------------

#: The one parent of every runtime root: short, so Core's socket path stays under
#: the Unix-domain limit, and fixed, so a supplied ``--runtime-root`` can be held to it.
RUNTIME_PARENT: Final = Path("/tmp")
RUNTIME_PREFIX: Final = "ovmcp-real-"
RUNTIME_RECEIPT: Final = "bootstrap-receipt.json"


def require_owned_runtime(root: Path) -> None:
    """Refuse, untouched, a ``--runtime-root`` this harness did not create.

    Cleanup deletes the root recursively, so ownership is proved before any cleanup
    can reach it: an ``ovmcp-real-`` directory directly under the harness parent,
    not a symlink, owner-only, holding the owner-only bootstrap receipt.  Anything
    else is ``entrypoint_unresolved`` and is never deleted.
    """
    if root.parent != RUNTIME_PARENT or not root.name.startswith(RUNTIME_PREFIX):
        raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
    try:
        shapes = (
            (os.lstat(root), stat.S_ISDIR),
            (os.lstat(root / RUNTIME_RECEIPT), stat.S_ISREG),
        )
    except OSError:
        raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED) from None
    for status, kind in shapes:
        if (
            not kind(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)


def remove_runtime(root: Path) -> None:
    """Delete the runtime root and prove it is gone, or refuse ``cleanup_incomplete``.

    The root can hold a copied Codex credential and the candidate install, so a
    partial deletion is never reported as success.  Nothing here can run after a
    SIGKILL of this harness: the root and any Core process it started then survive
    until removed by hand.  That limit is documented, not worked around.
    """
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        pass  # success only if the root itself is absent, checked below
    except OSError:
        raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE) from None
    if os.path.lexists(root):
        raise QualificationError(ReasonCode.CLEANUP_INCOMPLETE)


def discard_runtime(root: Path) -> None:
    """Best-effort removal on a failure path, which keeps the original reason.

    A failed removal is reported on stderr as ``cleanup_incomplete`` so the
    retained state is never silent, but the record still carries the first failure.
    """
    try:
        remove_runtime(root)
    except QualificationError:
        print(f"reason_code={ReasonCode.CLEANUP_INCOMPLETE.value}", file=sys.stderr)


# --- command line ---------------------------------------------------------


def _preflight(arguments: argparse.Namespace, auth: AuthSource) -> tuple[Candidate, str]:
    """Refuse an unusable invocation; return the reloaded candidate and the schema digest."""
    load_candidate(arguments.candidate)
    require_platform(platform.system().lower(), platform.machine().lower())
    binary: Path = arguments.host_binary
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise QualificationError(ReasonCode.HOST_BINARY_UNAVAILABLE)
    if isinstance(auth, AuthFile):
        if arguments.host == "claude-code":
            read_claude_token(auth.path)
        else:
            require_auth_file(auth.path)
    load_schema(arguments.schema)
    candidate = load_candidate(arguments.candidate)
    try:
        return candidate, _file_digest(arguments.schema)
    except OSError:
        raise QualificationError(ReasonCode.RECORD_INVALID) from None


def candidate_receipt(candidate: Candidate, schema_sha256: str) -> dict[str, object]:
    """The closed bootstrap binding carried across the candidate-runtime exec."""
    if not _SHA256.fullmatch(schema_sha256):
        raise QualificationError(ReasonCode.RECORD_INVALID)
    return {
        "revision": candidate.revision,
        "wheels": dict(candidate.wheels),
        "wheel_closure_count": candidate.closure_count,
        "wheel_closure_sha256": candidate.closure_sha256,
        "harness_sha256": candidate.harness_sha256,
        "schema_sha256": schema_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    if arguments_list[:1] == [INTERNAL_PROXY]:  # hidden: launched by a host, not by a person
        return proxy_main(arguments_list[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=sorted(HOST_VERSIONS))
    parser.add_argument("--host-binary", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument(
        "--use-existing-host-auth",
        action="store_true",
        help="Claude only: use the already logged-in Claude CLI profile instead of --auth-file",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--runtime-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic-stages", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--validate-record", type=Path, help="validate an existing record and exit"
    )
    arguments = parser.parse_args(arguments_list)

    def trace(stage: str) -> None:
        if arguments.diagnostic_stages:
            print(f"stage={stage}", file=sys.stderr, flush=True)

    if arguments.schema is None:
        parser.error("--schema is required")
    started_at = datetime.now(UTC)
    candidate: Candidate | None = None
    schema_sha256: str | None = None
    identity: OsIdentity | None = None
    host_identity: HostIdentity | None = None
    ledger = GateLedger()
    try:
        if arguments.validate_record is not None:
            try:
                record = json.loads(arguments.validate_record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise QualificationError(ReasonCode.RECORD_INVALID) from None
            validate_record(record, load_schema(arguments.schema))
            print("record valid")
            return 0
        required = ("host", "host_binary", "candidate", "output")
        if any(getattr(arguments, name) is None for name in required):
            parser.error("qualification requires --host, --host-binary, --candidate and --output")
        try:
            auth = auth_source(arguments.host, arguments.auth_file, arguments.use_existing_host_auth)
        except ValueError as error:
            parser.error(str(error))
        existing_login = auth if isinstance(auth, ExistingLogin) else None
        runtime_root: Path | None = arguments.runtime_root
        if runtime_root is None:
            candidate, schema_sha256 = _preflight(arguments, auth)
            trace("preflight_ok")
            runtime_root = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX, dir=RUNTIME_PARENT))
            try:
                try:
                    runtime_root.chmod(0o700)
                except OSError:
                    raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED) from None
                installed = bootstrap_candidate(
                    arguments.candidate, candidate, runtime_root
                )
                trace("bootstrap_ok")
                _write_private(
                    runtime_root / RUNTIME_RECEIPT,
                    json.dumps(
                        {
                            **candidate_receipt(candidate, schema_sha256),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                environment = {
                    "PATH": SYSTEM_PATH,
                    "LANG": "en_US.UTF-8",
                    "TMPDIR": str(runtime_root),
                }
                if existing_login is not None:
                    # The child re-validates these; they carry no secret, only the profile.
                    environment.update(HOME=existing_login.home, USER=existing_login.user)
                reexec_under_candidate(
                    installed,
                    [*arguments_list, "--runtime-root", str(runtime_root)],
                    environ=environment,
                )
                raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
            except BaseException:
                discard_runtime(runtime_root)
                raise

        # Every failure below deletes the supplied root, so it must first prove the
        # harness created it; a root that cannot is refused and never touched.
        require_owned_runtime(runtime_root)
        try:
            candidate, schema_sha256 = _preflight(arguments, auth)
            trace("preflight_ok")
            if not in_candidate_runtime(os.environ, sys.prefix):
                raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
            trace("candidate_runtime_ok")
            if Path(sys.prefix).resolve() != (runtime_root / "candidate-venv").resolve():
                raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
            receipt = _json_document(runtime_root / RUNTIME_RECEIPT)
            if receipt != candidate_receipt(candidate, schema_sha256):
                raise QualificationError(ReasonCode.ENTRYPOINT_UNRESOLVED)
            installed = installed_from_prefix(Path(sys.prefix))
            trace("candidate_receipt_ok")
            version_layout = host_layout(runtime_root / "version", arguments.host)
            create_layout(version_layout)
            version = require_host_version(
                arguments.host,
                arguments.host_binary,
                host_environment(
                    version_layout, arguments.host_binary, existing_login=existing_login
                ),
                version_layout.workspace,
            )
            host_identity = HostIdentity(arguments.host, version)
            trace("host_version_ok")
            require_host_authentication(
                arguments.host,
                arguments.host_binary,
                host_layout(runtime_root / "authentication", arguments.host),
                auth,
            )
            trace("host_authentication_ok")
            identity = os_identity()
            ledger = qualify_host(
                host=arguments.host,
                binary=arguments.host_binary,
                auth=auth,
                installed=installed,
                run_root=runtime_root / "qualification",
                progress=trace,
                ledger=ledger,
            )
            trace("journey_ok")
            record = build_record(
                candidate=candidate,
                schema_sha256=schema_sha256,
                os_identity=identity,
                host=host_identity,
                ledger=ledger,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
            # Verified removal comes before the pass record: a retained runtime root
            # (it can hold a copied host credential) must never publish a pass.
            remove_runtime(runtime_root)
            trace("runtime_removed")
            write_record(
                record,
                load_schema(arguments.schema, schema_sha256),
                arguments.output,
            )
            trace("record_ok")
            print("qualification pass")
            return 0
        finally:
            discard_runtime(runtime_root)
    except QualificationError as error:
        if (
            arguments.output is not None
            and arguments.schema is not None
            and arguments.validate_record is None
        ):
            try:
                finished_at = datetime.now(UTC)
                record = (
                    build_record(
                        candidate=candidate,
                        schema_sha256=schema_sha256,
                        os_identity=identity,
                        host=host_identity,
                        ledger=ledger,
                        started_at=started_at,
                        finished_at=finished_at,
                        reason=error.code,
                    )
                    if candidate is not None
                    and schema_sha256 is not None
                    and identity is not None
                    and host_identity is not None
                    else build_minimal_failure_record(
                        error.code,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                )
                write_record(
                    record,
                    load_schema(arguments.schema, schema_sha256),
                    arguments.output,
                )
            except QualificationError:
                pass
        print(f"reason_code={error.code.value}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
