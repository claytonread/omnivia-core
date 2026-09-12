#!/usr/bin/env python3
"""Qualify the installed OmniVia Core MCP artifacts against real Claude Code and Codex.

R004 section 13.I is the one acceptance section nothing in this tree can satisfy
by itself: "SDK-only simulations are supplemental and cannot satisfy this gate."
`scripts/run-standard-journey.py` is such a simulation and stays one -- it drives
the official Python SDK's own client against the installed server, which proves
the server and proves nothing about a host. This program is the other thing: it
starts the actual `claude` and `codex` binaries, lets their models drive the
tools, and decides the verdict from the JSON events those binaries emit and from
what Core durably holds afterwards -- never from what a model said it did.

**Two phases, and only one of them is offline.** `--acquire` builds the five
first-party wheels and stages the reviewed third-party closure from the
configured index, exactly as `scripts/check-package-builds.sh` does and for the
same recorded reason: a wheelhouse is *sufficient* for an index-free install,
which is not the same claim as the wheels being obtainable offline. Everything
after acquisition installs with `--no-index --only-binary=:all: --find-links`
and reaches no index. A caller who already has a wheelhouse passes it and the
online phase never runs.

**What "installed" is proved to mean.** The qualification virtual environment is
built outside the repository, every subprocess runs from a temporary working
directory with `PYTHONPATH`/`PYTHONHOME` removed and `PYTHONNOUSERSITE` set, and
before anything else happens a probe asserts that `omnivia_core`,
`omnivia_core_runtime`, `omnivia_core_client`, `omnivia_core_cli`,
`omnivia_core_mcp` and `mcp` all resolve beneath that environment and none of
them beneath this checkout. `mcp` and `mcp-types` must report exactly 2.0.0. A
run that reached the source tree fails here rather than producing evidence about
the wrong code.

**What is never touched.** No `~/.claude`, `~/.codex`, project `.mcp.json`,
user settings file, production workspace or production grant is written. Claude
Code is given the qualification server through `--mcp-config` with
`--strict-mcp-config`, `--restricted` (which ignores user, project and local
settings), `--no-session-persistence` and a temporary working directory; Codex
is given it through `-c mcp_servers.*` with `--ephemeral`,
`--ignore-user-config` and `--ignore-rules`. Account authentication is inherited
and nothing else is: no credential is read, copied, recorded or printed. The
configuration surfaces both hosts own are hashed before the run and re-hashed
after it, and a difference fails the run.

**The host prompt is untrusted input.** A model can claim anything, so no claim
of a model's is evidence here. Each step is checked three ways: the host's own
structured events must show the exact tool name and the exact arguments; the
tool result carried by those events must be the canonical Core result; and the
durable consequence must be independently visible to the owner's installed CLI,
which is a different principal on a different code path. Raw transcripts are
deleted in every case, including failure.

**The relay, disclosed.** Section 13.F asks for an outcome where Core commits
and the answer never arrives. The product has no drop switch and must not grow
one, so the loss is staged in the pipe, by
`packages/omnivia-core-mcp/tests/_mcp_interrupted_relay.py` -- a test-tree
module, not part of any wheel -- which forwards the installed executable
verbatim and withholds exactly one `tools/call` response after the server has
produced it. Every ordinary journey call connects straight to the installed
executable. The same module is also used, without withholding anything, to
observe the `tools/list` names Codex receives, because Codex publishes no
inventory event of its own; Claude Code's inventory is read from its own
`system`/`init` event and no relay is involved. Both uses are recorded in the
evidence as what they are.

**The record.** A passing run writes one compact JSON document whose schema
admits no free text at all: every string in it is either drawn from a closed
vocabulary or matched against a digest, version or timestamp pattern, so a
prompt, a path, an endpoint, a credential, a grant, a job identifier, a content
body or an exception message has nowhere to land even by accident. Result
identities appear only as truncated digests, which is enough to prove two
answers named the same artifact and not enough to name it. A failing run writes
nothing over a passing record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- the baseline this lane qualifies -----------------------------------------
#
# R004 section 10 and Appendix D name these exact versions. A different installed
# version is not a soft warning: it fails, unless the caller states the approved
# replacement on the command line, and the replacement is then recorded in the
# evidence beside the fact that it was approved rather than observed.

REQUIRED_HOST_VERSIONS: dict[str, str] = {
    "claude-code": "2.1.269",
    "codex": "0.146.0",
}
REQUIRED_OS = {"version": "26.5.2", "build": "25F84", "arch": "arm64"}
REQUIRED_SDK_PINS = {"mcp": "2.0.0", "mcp-types": "2.0.0"}
MCP_PROTOCOL_VERSION = "2025-06-18"

#: The five first-party distributions, and the operational imports that must
#: resolve from the qualification environment rather than from this checkout.
DISTRIBUTIONS = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-mcp",
    "omnivia-core-cli",
    "omnivia-core-client",
)
INSTALLED_MODULES = (
    "omnivia_core",
    "omnivia_core_runtime",
    "omnivia_core_client",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "mcp",
)

# --- the advertised inventory --------------------------------------------------

RESTRICTED_TOOLS = (
    "context_pack_build",
    "evidence_search",
    "graph_traverse",
    "knowledge_search",
    "memory_search",
    "workspace_inspect",
)
AUTHORING_TOOLS = tuple(
    sorted(
        RESTRICTED_TOOLS
        + ("evidence_capture", "import_start", "job_events", "job_get", "memory_create")
    )
)
#: Section 3.3 excludes these by name. They must be absent from the inventory and
#: refused by the server when a caller names one directly.
EXCLUDED_TOOLS = ("job_cancel", "job_retry", "workspace_create", "grant_issue")

HOSTS = ("claude-code", "codex")
MCP_SERVER_NAME = "omniviacore"

# --- fixed marker data ---------------------------------------------------------
#
# Every value the hosts are asked to submit is a constant here. Nothing is
# derived from the machine, the account, the clock or the environment, so a
# transcript, a log line or an evidence field cannot carry anything private out
# of the run, and two runs submit byte-identical content.

CAPTURE_SOURCE_ID = "hostqual-capture-1"
CAPTURE_TERM = "zmarkerzalpha"
CAPTURE_TEXT = f"The qualification marker is {CAPTURE_TERM}.\n"
CAPTURE_TEXT_CHANGED = f"The qualification marker is {CAPTURE_TERM} restated.\n"
CAPTURE_KEY = "hostqual-capture-key-1"
CAPTURE_INTERRUPTED_SOURCE_ID = "hostqual-capture-2"
CAPTURE_INTERRUPTED_TERM = "zmarkerzbeta"
CAPTURE_INTERRUPTED_TEXT = (
    f"The interrupted qualification marker is {CAPTURE_INTERRUPTED_TERM}.\n"
)
CAPTURE_INTERRUPTED_KEY = "hostqual-capture-key-2"
MEMORY_KEY = "hostqual-memory-key-1"
MEMORY_FACT = f"The qualification marker {CAPTURE_TERM} was submitted."
MEMORY_FACT_CHANGED = f"The qualification marker {CAPTURE_TERM} was restated."
IMPORT_KEY = "hostqual-import-key-1"

# --- the case register ---------------------------------------------------------
#
# Every case the lane must decide, for every host. The register is the
# specification of the run, not a summary of it: the record is rejected unless a
# verdict exists for each of these under each qualified host, so a step that
# silently stops running fails the guard instead of shrinking the evidence.

CASE_IDS = (
    "inventory_eleven_tools",
    "inventory_excluded_absent",
    "excluded_undispatchable",
    "capture_created",
    "capture_searchable",
    "memory_proposed_only",
    "memory_default_view_hidden",
    "memory_candidate_view_visible",
    "capture_replay_stable",
    "memory_replay_stable",
    "capture_changed_input_conflict",
    "memory_changed_input_conflict",
    "service_healthy_after_session",
    "import_started_one_job",
    "import_replay_same_job",
    "import_changed_input_conflict",
    "job_get_terminal",
    "job_events_paginated_stable",
    "import_evidence_retrievable",
    "ambiguous_capture_recovered",
    "stdout_protocol_only",
    "restart_read_observation",
    "revocation_blocks_host",
    "revocation_preserves_committed_job",
    "owner_cli_observes_job",
)

DISPOSITIONS = (
    "created",
    "already_captured",
    "idempotency_conflict",
    "stable_replay",
    "proposed",
    "candidate",
    "absent",
    "refused",
    "succeeded",
    "healthy",
    "observed",
    "failed_closed",
    "recovered",
)

EVIDENCE_FORMAT = "omnivia.mcp-real-host-qualification.v1"
DEFAULT_EVIDENCE_PATH = (
    REPO_ROOT
    / "docs"
    / "development"
    / "qualification-evidence"
    / "mcp-real-host-qualification.json"
)

#: How the inventory reached the record, per host. Claude Code publishes its own
#: connected-server tool list; Codex does not, so its inventory is read off the
#: `tools/list` response at the host's transport boundary by the disclosed relay.
INVENTORY_OBSERVATIONS = ("host_init_event", "relay_observed_tools_list")


class QualificationError(RuntimeError):
    """A refusal. Its text reaches the operator's terminal and never the record."""


# --- the evidence schema -------------------------------------------------------
#
# Closed in both directions. Every object forbids additional properties and every
# string is constrained by `enum`, `const` or `pattern`, which is what makes the
# redaction rule structural rather than a review habit: there is no place in this
# document where a prompt, a path, an endpoint, a credential, a grant, a process
# id, an account name or an exception message could be written even by a careless
# change, because no string position accepts free text.

_DIGEST = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
_COMMIT = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
_VERSION = {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+){0,3}$"}
_TIMESTAMP = {
    "type": "string",
    "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
}

EVIDENCE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "format",
        "recorded_at",
        "commit",
        "specification",
        "environment",
        "hosts",
        "installation",
        "inventories",
        "cases",
        "verdict",
    ],
    "properties": {
        "format": {"const": EVIDENCE_FORMAT},
        "recorded_at": _TIMESTAMP,
        "commit": _COMMIT,
        "specification": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requirements", "sections"],
            "properties": {
                "requirements": {"const": "R004-v1.3"},
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "pattern": "^13\\.[A-I]$|^10$"},
                },
            },
        },
        "environment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["os_product", "os_version", "os_build", "arch", "approved"],
            "properties": {
                "os_product": {"const": "macOS"},
                "os_version": _VERSION,
                "os_build": {"type": "string", "pattern": "^[0-9A-Z]{4,8}$"},
                "arch": {"enum": ["arm64", "x86_64"]},
                "approved": {"type": "boolean"},
            },
        },
        "hosts": {
            "type": "object",
            "additionalProperties": False,
            "required": list(HOSTS),
            "properties": {
                host: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["version", "required_version", "approved", "executed"],
                    "properties": {
                        "version": _VERSION,
                        "required_version": _VERSION,
                        "approved": {"type": "boolean"},
                        "executed": {"type": "boolean"},
                    },
                }
                for host in HOSTS
            },
        },
        "installation": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "mode",
                "protocol_version",
                "sdk_pins",
                "distributions",
                "wheelhouse",
                "installed_not_source",
                "index_used_during_install",
            ],
            "properties": {
                "mode": {"const": "wheelhouse_no_index"},
                "protocol_version": {"const": MCP_PROTOCOL_VERSION},
                "sdk_pins": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(REQUIRED_SDK_PINS),
                    "properties": {name: _VERSION for name in REQUIRED_SDK_PINS},
                },
                "distributions": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(DISTRIBUTIONS),
                    "properties": {name: _VERSION for name in DISTRIBUTIONS},
                },
                "wheelhouse": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["wheel_count", "digest"],
                    "properties": {
                        "wheel_count": {"type": "integer", "minimum": 1},
                        "digest": _DIGEST,
                    },
                },
                "installed_not_source": {"const": True},
                "index_used_during_install": {"const": False},
            },
        },
        "inventories": {
            "type": "object",
            "additionalProperties": False,
            "required": list(HOSTS),
            "properties": {
                host: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["profile", "observation", "tools", "count"],
                    "properties": {
                        "profile": {"const": "authoring"},
                        "observation": {"enum": list(INVENTORY_OBSERVATIONS)},
                        "tools": {
                            "type": "array",
                            "minItems": 11,
                            "maxItems": 11,
                            "uniqueItems": True,
                            "items": {"enum": list(AUTHORING_TOOLS)},
                        },
                        "count": {"const": 11},
                    },
                }
                for host in HOSTS
            },
        },
        "cases": {
            "type": "array",
            "minItems": len(CASE_IDS),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["case_id", "host", "tools", "disposition", "verdict"],
                "properties": {
                    "case_id": {"enum": list(CASE_IDS)},
                    "host": {"enum": list(HOSTS)},
                    "tools": {
                        "type": "array",
                        "items": {"enum": list(AUTHORING_TOOLS + EXCLUDED_TOOLS)},
                    },
                    "disposition": {"enum": list(DISPOSITIONS)},
                    "identity_digest": _DIGEST,
                    "matches_identity_digest": _DIGEST,
                    "count": {"type": "integer", "minimum": 0},
                    "pages": {"type": "integer", "minimum": 0},
                    "relayed": {"type": "boolean"},
                    "verdict": {"enum": ["pass", "fail"]},
                },
            },
        },
        "verdict": {"enum": ["pass", "fail"]},
    },
}


def verify_record(document: Any, *, commit: str | None = None) -> list[str]:
    """Everything that must hold before a `HOST` row may be called green.

    Returned findings are for an operator, not for the record. An empty list
    means: the document is this schema's shape, it was produced by the
    repository state the caller names, both hosts ran at a version this lane
    accepts, the installed closure carried the reviewed pins and came from the
    wheelhouse rather than the source tree, both inventories are the eleven
    authoring tools, every case in the register has a passing verdict under both
    hosts, and nothing in the document reads like a secret. Absence, staleness,
    failure and incompleteness each produce a finding, which is why this is the
    same function the traceability guard calls.
    """
    from jsonschema import Draft202012Validator

    findings: list[str] = []
    if document is None:
        return ["no qualification record"]
    errors = sorted(
        Draft202012Validator(EVIDENCE_SCHEMA).iter_errors(document),
        key=lambda error: list(error.path),
    )
    if errors:
        return [
            f"record does not match the schema at {list(e.path)}" for e in errors[:8]
        ]

    if document["verdict"] != "pass":
        findings.append("record verdict is not a pass")
    if commit is not None and document["commit"] != commit:
        findings.append("record was produced at a different commit")

    for host in HOSTS:
        facts = document["hosts"][host]
        if not facts["executed"]:
            findings.append(f"{host} did not execute")
        if facts["version"] != facts["required_version"] and not facts["approved"]:
            findings.append(f"{host} ran at an unapproved version")
        if facts["required_version"] != REQUIRED_HOST_VERSIONS[host]:
            findings.append(f"{host} records a required version this lane does not set")
        inventory = document["inventories"][host]
        if tuple(inventory["tools"]) != AUTHORING_TOOLS:
            findings.append(f"{host} did not observe the authoring inventory")

    environment = document["environment"]
    if not environment["approved"] and (
        environment["os_version"] != REQUIRED_OS["version"]
        or environment["os_build"] != REQUIRED_OS["build"]
        or environment["arch"] != REQUIRED_OS["arch"]
    ):
        findings.append("the operating-system baseline is neither matched nor approved")

    installation = document["installation"]
    for name, pin in REQUIRED_SDK_PINS.items():
        if installation["sdk_pins"][name] != pin:
            findings.append(f"{name} was not the reviewed pin")

    decided = {(case["case_id"], case["host"]) for case in document["cases"]}
    failed = {
        case["case_id"] for case in document["cases"] if case["verdict"] != "pass"
    }
    for case_id in CASE_IDS:
        for host in HOSTS:
            if (case_id, host) not in decided:
                findings.append(f"{case_id} has no verdict under {host}")
    if failed:
        findings.append(f"{len(failed)} case(s) did not pass")

    findings.extend(_redaction_findings(document))
    return findings


#: Shapes that must never appear anywhere in the record. The schema already makes
#: them unreachable; this is the independent second opinion, because a schema is
#: only as closed as its last edit.
_FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"/"),
    re.compile(r"\\"),
    re.compile(r"@"),
    re.compile(r"\s"),
    re.compile(r"(?i)bearer|token|secret|credential|grant|passwd|key="),
    re.compile(r"(?i)unix:|pipe:|https?:"),
)


def _is_known_safe_tool_name(value: str, trail: str) -> bool:
    """Admit fixed inventory literals without weakening the generic leak scan."""
    parts = trail.rsplit("/", 2)
    return (
        len(parts) == 3
        and parts[-2] == "tools"
        and parts[-1].isdigit()
        and value in EXCLUDED_TOOLS
    )
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "stdout",
        "stderr",
        "transcript",
        "endpoint",
        "path",
        "paths",
        "credential",
        "grant",
        "pid",
        "account",
        "workspace_id",
        "principal_id",
        "evidence_id",
        "job_id",
        "record_id",
        "content",
        "text",
        "message",
        "error",
        "exception",
    }
)


def _redaction_findings(document: Any, trail: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            if key in _FORBIDDEN_KEYS:
                findings.append(f"record carries a forbidden field at {trail}/{key}")
            findings.extend(_redaction_findings(value, f"{trail}/{key}"))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            findings.extend(_redaction_findings(value, f"{trail}/{index}"))
    elif isinstance(document, str):
        if _is_known_safe_tool_name(document, trail):
            return findings
        for pattern in _FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(document):
                findings.append(f"record carries an unredacted value at {trail}")
                break
    return findings


def digest(value: str) -> str:
    """A stable 16-hex identity for a Core-issued identifier.

    Enough to prove two answers named the same artifact; not enough to name it.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# --- process plumbing ----------------------------------------------------------


def scrubbed_environment() -> dict[str, str]:
    """The environment every qualification subprocess inherits.

    `PYTHONPATH` is the specific hazard this lane exists to rule out: with the
    worktree on it, an installed console script imports the source tree and the
    run qualifies code that was never packaged. `PYTHONHOME` and user site
    packages are removed for the same reason.
    """
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 300.0,
    check: bool = True,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """One subprocess, with no inherited stdin and a bounded lifetime."""
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd),
        env=scrubbed_environment(),
        input=stdin_bytes.decode("utf-8") if stdin_bytes is not None else "",
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise QualificationError(
            f"{Path(argv[0]).name} exited {completed.returncode}: "
            f"{completed.stderr.strip()[:400]}"
        )
    return completed


def json_document(text: str, what: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except ValueError:
        raise QualificationError(f"{what} did not answer with JSON") from None
    if not isinstance(document, dict):
        raise QualificationError(f"{what} did not answer with a JSON object")
    return document


# --- host and platform identity ------------------------------------------------


def host_executable(host: str) -> Path:
    name = "claude" if host == "claude-code" else "codex"
    found = shutil.which(name)
    if found is None:
        raise QualificationError(f"{name} is not on PATH")
    return Path(found)


def host_version(host: str) -> str:
    """The installed version, read from the binary and nothing else."""
    completed = run(
        [str(host_executable(host)), "--version"], cwd=Path.cwd(), timeout=60
    )
    match = re.search(r"(\d+\.\d+\.\d+)", completed.stdout)
    if match is None:
        raise QualificationError(f"{host} did not report a version")
    return match.group(1)


def operating_system() -> dict[str, str]:
    if sys.platform != "darwin":
        raise QualificationError("this lane qualifies the macOS baseline only")
    product = run(["/usr/bin/sw_vers", "-productVersion"], cwd=Path.cwd(), timeout=30)
    build = run(["/usr/bin/sw_vers", "-buildVersion"], cwd=Path.cwd(), timeout=30)
    return {
        "os_product": "macOS",
        "os_version": product.stdout.strip(),
        "os_build": build.stdout.strip(),
        "arch": platform.machine(),
    }


# --- the configuration surfaces this lane must leave alone ---------------------


def config_surfaces() -> dict[str, str]:
    """A hash of every host configuration surface the run promises not to change.

    Claude Code keeps global bookkeeping in `~/.claude.json` -- caches, counters,
    notices -- that changes whenever it runs and has nothing to do with MCP, so
    hashing the whole file would fail every run for the wrong reason. What is
    hashed instead is the part this lane could plausibly damage: the MCP server
    map, the set of known project keys, and each project's MCP approval lists.
    Everything else is compared byte for byte.
    """
    surfaces: dict[str, str] = {}
    for path in (
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / ".mcp.json",
        Path.home() / ".codex" / "config.toml",
        REPO_ROOT / ".mcp.json",
    ):
        surfaces[
            path.name if path.parent.name != ".claude" else f"claude/{path.name}"
        ] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else "absent"
        )
    settings = Path.home() / ".claude.json"
    if settings.is_file():
        try:
            document = json.loads(settings.read_text(encoding="utf-8"))
        except ValueError:
            surfaces["claude.json"] = "unparsed"
        else:
            projects = document.get("projects")
            projection = {
                "mcpServers": document.get("mcpServers"),
                "projects": sorted(projects) if isinstance(projects, dict) else None,
                "approvals": {
                    name: {
                        key: entry.get(key)
                        for key in (
                            "mcpServers",
                            "enabledMcpjsonServers",
                            "disabledMcpjsonServers",
                        )
                    }
                    for name, entry in sorted(projects.items())
                    if isinstance(entry, dict)
                }
                if isinstance(projects, dict)
                else None,
            }
            surfaces["claude.json"] = hashlib.sha256(
                json.dumps(projection, sort_keys=True).encode("utf-8")
            ).hexdigest()
    else:
        surfaces["claude.json"] = "absent"
    return surfaces


# --- acquisition (online) and installation (offline) ---------------------------


def acquire(wheelhouse: Path, python: Path) -> None:
    """Phase 1. Reaches the configured index on purpose; nothing after this does.

    The same two commands `scripts/check-package-builds.sh` uses, for the same
    recorded reason: `--only-binary=:all:` refuses a source distribution and
    `--constraint` resolves the closure against the reviewed pins rather than
    against whatever the index holds today.
    """
    wheelhouse.mkdir(parents=True, exist_ok=True)
    print("=== acquisition phase (index access permitted) ===", file=sys.stderr)
    for project in (
        REPO_ROOT,
        *(REPO_ROOT / "packages" / name for name in DISTRIBUTIONS[1:]),
    ):
        run(
            [
                str(python),
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheelhouse),
                str(project),
            ],
            cwd=REPO_ROOT,
            timeout=900,
        )
    mcp_wheel = max(wheelhouse.glob("omnivia_core_mcp-*.whl"))
    runtime_wheel = max(wheelhouse.glob("omnivia_core_runtime-*.whl"))
    run(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--constraint",
            str(REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt"),
            "--dest",
            str(wheelhouse),
            "--find-links",
            str(wheelhouse),
            str(mcp_wheel),
            str(runtime_wheel),
        ],
        cwd=REPO_ROOT,
        timeout=900,
    )
    print(
        "=== acquisition complete; every later phase is index-free ===", file=sys.stderr
    )


#: Run inside the qualification environment, from a temporary working directory,
#: with `PYTHONPATH` removed: where each module actually came from, what version
#: each distribution reports, and the import path that produced both. Every
#: answer here is a fact about the installation, not about this checkout.
_INSTALLED_PROBE = """
import importlib, importlib.metadata, json, sys

modules, distributions = json.loads(sys.argv[1]), json.loads(sys.argv[2])
print(
    json.dumps(
        {
            "modules": {
                name: getattr(importlib.import_module(name), "__file__", "")
                for name in modules
            },
            "distributions": {
                name: importlib.metadata.version(name) for name in distributions
            },
            "path": sys.path,
        }
    )
)
"""


@dataclass(frozen=True)
class Installation:
    """The qualification environment, and the proof it is not this checkout."""

    prefix: Path
    python: Path
    omnivia: Path
    service: Path
    mcp_server: Path
    facts: dict[str, Any]


def install(wheelhouse: Path, work: Path, python: Path) -> Installation:
    """Phase 2. Build the environment from the wheelhouse alone, then prove it."""
    non_wheels = sorted(p.name for p in wheelhouse.iterdir() if p.suffix != ".whl")
    if non_wheels:
        raise QualificationError("the wheelhouse holds a non-wheel artifact")
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise QualificationError("the wheelhouse is empty")

    prefix = work / "qualification-venv"
    run([str(python), "-m", "venv", str(prefix)], cwd=work, timeout=300)
    run(
        [
            str(prefix / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--only-binary=:all:",
            "--find-links",
            str(wheelhouse),
            *DISTRIBUTIONS,
        ],
        cwd=work,
        timeout=900,
    )

    probe = work / "installed-probe-cwd"
    probe.mkdir()
    origins = json_document(
        run(
            [
                str(prefix / "bin" / "python"),
                "-c",
                _INSTALLED_PROBE,
                json.dumps(list(INSTALLED_MODULES)),
                json.dumps(list(DISTRIBUTIONS) + list(REQUIRED_SDK_PINS)),
            ],
            cwd=probe,
            timeout=120,
        ).stdout,
        "the installed-import probe",
    )

    resolved_prefix = prefix.resolve()
    for name, origin in origins["modules"].items():
        if not origin:
            raise QualificationError(f"{name} has no file origin in the environment")
        location = Path(origin).resolve()
        if resolved_prefix not in location.parents:
            raise QualificationError(f"{name} did not resolve inside the environment")
        if REPO_ROOT in location.parents:
            raise QualificationError(f"{name} resolved inside this checkout")
    for entry in origins["path"]:
        if entry and REPO_ROOT in (
            Path(entry).resolve(),
            *Path(entry).resolve().parents,
        ):
            raise QualificationError(
                "this checkout is on the environment's import path"
            )
    for name, pin in REQUIRED_SDK_PINS.items():
        if origins["distributions"][name] != pin:
            raise QualificationError(f"{name} is not {pin} in the environment")

    executables = {}
    for name in ("omnivia", "omnivia-core-service", "omnivia-core-mcp"):
        executable = prefix / "bin" / name
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise QualificationError(f"{name} was not installed as an executable")
        executables[name] = executable

    wheelhouse_digest = hashlib.sha256()
    for wheel in wheels:
        wheelhouse_digest.update(hashlib.sha256(wheel.read_bytes()).digest())
    facts = {
        "mode": "wheelhouse_no_index",
        "protocol_version": MCP_PROTOCOL_VERSION,
        "sdk_pins": {
            name: origins["distributions"][name] for name in REQUIRED_SDK_PINS
        },
        "distributions": {
            name: origins["distributions"][name] for name in DISTRIBUTIONS
        },
        "wheelhouse": {
            "wheel_count": len(wheels),
            "digest": wheelhouse_digest.hexdigest()[:16],
        },
        "installed_not_source": True,
        "index_used_during_install": False,
    }
    return Installation(
        prefix=prefix,
        python=prefix / "bin" / "python",
        omnivia=executables["omnivia"],
        service=executables["omnivia-core-service"],
        mcp_server=executables["omnivia-core-mcp"],
        facts=facts,
    )


# --- the Core installation under qualification ---------------------------------


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    """SIGTERM, then SIGKILL if it will not go."""
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=60)


#: Where a server-minted workspace lands: the sibling of the trusted root, which
#: is where `service.main` points its own installation authority. Not a choice
#: this file makes -- a workspace written anywhere else would belong to a second
#: installation beside the one that minted it.
WORKSPACE_STORAGE_DIRECTORY = "workspaces"


@dataclass
class Core:
    """The installed Core this lane qualifies against, in two deliberate parts.

    **Why there are two workspaces.** `omnivia-core-service --init` bootstraps an
    installation and a first workspace, but a bootstrapped workspace is not in
    the installation's *authorised inventory* -- `omnivia workspace list` answers
    empty -- and `mcp configure` refuses a workspace that is not in it. Only
    `workspace.create` puts one there. So the bootstrap workspace exists to give
    the installation a service that can answer `workspace create`, and the
    workspace the whole journey runs in is the one that command mints: genuinely
    empty, registered, and never written to by anything but the host.

    **And why only one of them is serving by the end.** Once the qualification
    workspace has its own service, the bootstrap one is stopped: an
    administration control is answered by whichever of this installation's
    services is up, so one is enough, and "the independently owned Core service"
    the journey must outlive is then unambiguous.
    """

    installed: Installation
    root: Path
    state: Path
    workspace_id: str = ""
    workspace: Path = field(init=False)
    bootstrap_workspace: Path = field(init=False)
    bootstrap_process: subprocess.Popen[bytes] | None = None
    process: subprocess.Popen[bytes] | None = None
    log: Path = field(init=False)

    def __post_init__(self) -> None:
        self.log = self.root / "service.log"
        self.bootstrap_workspace = self.root / "bootstrap-workspace"
        self.workspace = self.root / WORKSPACE_STORAGE_DIRECTORY

    def _endpoint(self, name: str) -> str:
        return f"unix://{self.root / name}"

    def _serve(self, workspace: Path, endpoint: str) -> subprocess.Popen[bytes]:
        with self.log.open("ab") as handle:
            return subprocess.Popen(
                [
                    str(self.installed.service),
                    "--workspace",
                    str(workspace),
                    "--installation-state",
                    str(self.state),
                    "--endpoint",
                    endpoint,
                ],
                cwd=str(self.root),
                env=scrubbed_environment(),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
            )

    def _await(self, process: subprocess.Popen[bytes], workspace_id: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise QualificationError("the installed service exited during startup")
            if self.healthy(workspace_id):
                return
            time.sleep(0.2)
        raise QualificationError("the installed service did not become reachable")

    def bootstrap(self) -> str:
        """Initialise the installation, serve it, and mint the empty workspace."""
        initialized = json_document(
            run(
                [
                    str(self.installed.service),
                    "--workspace",
                    str(self.bootstrap_workspace),
                    "--installation-state",
                    str(self.state),
                    "--init",
                ],
                cwd=self.root,
                timeout=300,
            ).stdout,
            "workspace initialization",
        )
        bootstrap_id = initialized.get("workspace", {}).get("workspace_id")
        if not isinstance(bootstrap_id, str):
            raise QualificationError("workspace initialization omitted its identity")
        self.bootstrap_process = self._serve(
            self.bootstrap_workspace, self._endpoint("bootstrap.sock")
        )
        self._await(self.bootstrap_process, bootstrap_id)

        created = json_document(
            run(
                [
                    str(self.installed.omnivia),
                    "--installation-state",
                    str(self.state),
                    "--workspace-id",
                    bootstrap_id,
                    "workspace",
                    "create",
                    "--input-json",
                    json.dumps({"display_name": "Host qualification"}),
                    "--idempotency-key",
                    "hostqual-workspace-1",
                    "--json",
                ],
                cwd=self.root,
                timeout=300,
            ).stdout,
            "workspace create",
        )
        minted = created.get("result", {}).get("workspace", {}).get("workspace_id")
        if not isinstance(minted, str):
            raise QualificationError("workspace create answered without an identity")
        self.workspace_id = minted
        self.workspace = self.root / WORKSPACE_STORAGE_DIRECTORY / minted
        if not self.workspace.is_dir():
            raise QualificationError("the minted workspace is not where it belongs")
        return minted

    def start(self) -> None:
        if self.process is not None:
            raise QualificationError("the service is already running")
        self.process = self._serve(self.workspace, self._endpoint("s.sock"))
        self._await(self.process, self.workspace_id)

    def stop(self) -> None:
        _terminate(self.process)
        self.process = None

    def stop_bootstrap(self) -> None:
        _terminate(self.bootstrap_process)
        self.bootstrap_process = None

    def healthy(self, workspace_id: str | None = None) -> bool:
        """The installed probe, asked as the owner. Never a socket poke."""
        completed = self.cli(
            "service", "health", "--json", workspace_id=workspace_id, check=False
        )
        if completed.returncode != 0:
            return False
        return bool(json.loads(completed.stdout or "{}").get("status") == "pass")

    def cli(
        self, *argv: str, workspace_id: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return run(
            [
                str(self.installed.omnivia),
                "--installation-state",
                str(self.state),
                "--workspace-id",
                workspace_id or self.workspace_id,
                *argv,
            ],
            cwd=self.root,
            timeout=180,
            check=check,
        )

    def admin(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            [
                str(self.installed.omnivia),
                "--installation-state",
                str(self.state),
                *argv,
            ],
            cwd=self.root,
            timeout=180,
            check=check,
        )

    def read(self, group: str, leaf: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One owner-CLI read. The independent second opinion on every mutation."""
        completed = self.cli(
            group, leaf, "--input-json", json.dumps(payload, sort_keys=True), "--json"
        )
        answered = json_document(completed.stdout, f"owner {group} {leaf}")
        result = answered.get("result")
        if not isinstance(result, dict):
            raise QualificationError(f"owner {group} {leaf} returned no result")
        return result

    def configure(self, host: str, profile: str = "authoring") -> Path:
        """The installed owner command, and the owner-private file it writes."""
        self.admin(
            "mcp",
            "configure",
            "--host",
            host,
            "--workspace",
            self.workspace_id,
            "--profile",
            profile,
        )
        config = self.state / "runtime" / ".installed-mcp" / f"{host}.json"
        if not config.is_file():
            raise QualificationError("configure did not write the protected document")
        mode = config.stat().st_mode & 0o777
        if mode & 0o077:
            raise QualificationError("the protected document is not owner-private")
        return config

    def advertised_tool_count(self, host: str) -> int | None:
        status = json_document(
            self.admin("mcp", "status", "--json", check=False).stdout, "mcp status"
        )
        for row in status.get("hosts", []):
            if row.get("host") == host:
                count = row.get("advertised_tool_count")
                return count if isinstance(count, int) else None
        return None


# --- host lanes ----------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One tool call as the host itself reported it.

    `result` is the canonical operation result, and its absence is the product's
    own definition of failure: section 5.3 requires a success to carry both
    `structuredContent` and the canonical JSON text of the same document, and a
    failure to carry neither. Deciding success that way rather than from each
    host's error flag means both lanes are judged by one rule, and by the rule
    the specification states.

    `text` is the mirror on success and the sanitized refusal sentence on
    failure. It is read for one thing only -- the canonical error code -- and
    never recorded.
    """

    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    text: str

    @property
    def failed(self) -> bool:
        return self.result is None


@dataclass(frozen=True)
class Step:
    case_id: str
    instruction: str
    max_turns: int = 8


def _mutation(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {"input": dict(payload), "idempotency_key": key}


def instruction(tool: str, arguments: Mapping[str, Any]) -> str:
    """The one prompt shape this lane uses.

    Deliberately mechanical. The model is asked for a single named call with a
    verbatim argument object and nothing else; what it says afterwards is never
    read. A model that improvises fails the step, because the step is decided by
    the tool-call events, not by the reply.
    """
    return (
        f"Call the MCP tool named `{tool}` on the `{MCP_SERVER_NAME}` server exactly "
        "once, passing this JSON argument object verbatim with no addition, removal "
        "or reformatting of any field:\n"
        f"{json.dumps(arguments, sort_keys=True)}\n"
        "Call no other tool. Do not retry the call, and do not call it a second "
        "time even if it returns an error. When it has returned, reply with the "
        "single word DONE and stop."
    )


class Host:
    """What both lanes have in common: run one step, report the calls observed."""

    host_id = ""

    def __init__(self, installed: Installation, work: Path, config: Path) -> None:
        self.installed = installed
        self.work = work
        self.config = config
        self.cwd = work / f"{self.host_id}-cwd"
        self.cwd.mkdir(mode=0o700, exist_ok=True)

    def server_command(self) -> list[str]:
        return [str(self.installed.mcp_server), "--config", str(self.config)]

    def run_step(
        self, step: Step, *, server: Sequence[str] | None = None
    ) -> list[Call]:
        raise NotImplementedError

    @staticmethod
    def _structured(payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except ValueError:
                return None
            return decoded if isinstance(decoded, dict) else None
        return None


class ClaudeCode(Host):
    """Claude Code, driven with an explicit MCP config and no persistence.

    `--strict-mcp-config` is what makes the explicit config the *only* config:
    without it the user's own servers would join the session. `--restricted`
    drops the code-running tools and ignores the user, project and local
    settings files; `--permission-prompts none` denies anything that would
    otherwise ask, so the eleven named MCP tools are the whole of what this
    session can do. `--no-session-persistence` keeps the transcript off disk,
    and the working directory is a temporary one, so no project entry is
    recorded either.
    """

    host_id = "claude-code"

    def mcp_config(self, server: Sequence[str]) -> Path:
        path = self.work / "claude-mcp-config.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        MCP_SERVER_NAME: {
                            "type": "stdio",
                            "command": server[0],
                            "args": list(server[1:]),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def run_step(
        self, step: Step, *, server: Sequence[str] | None = None
    ) -> list[Call]:
        config = self.mcp_config(server or self.server_command())
        completed = subprocess.run(
            [
                str(host_executable(self.host_id)),
                "-p",
                step.instruction,
                "--mcp-config",
                str(config),
                "--strict-mcp-config",
                "--restricted",
                "--permission-prompts",
                "none",
                "--no-session-persistence",
                "--allowedTools",
                " ".join(f"mcp__{MCP_SERVER_NAME}__{tool}" for tool in AUTHORING_TOOLS),
                "--disallowedTools",
                "Read Glob Grep Write Edit TodoWrite Task WebFetch WebSearch",
                "--max-turns",
                str(step.max_turns),
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            cwd=str(self.cwd),
            env=scrubbed_environment(),
            input="",
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        return self._parse(completed.stdout)

    def inventory(self) -> list[str]:
        """The connected server's tools, from the host's own session header."""
        calls, tools = self._session(
            Step("inventory_eleven_tools", "Reply with the single word DONE.", 1)
        )
        del calls
        return tools

    def _session(self, step: Step) -> tuple[list[Call], list[str]]:
        config = self.mcp_config(self.server_command())
        completed = subprocess.run(
            [
                str(host_executable(self.host_id)),
                "-p",
                step.instruction,
                "--mcp-config",
                str(config),
                "--strict-mcp-config",
                "--restricted",
                "--permission-prompts",
                "none",
                "--no-session-persistence",
                "--max-turns",
                str(step.max_turns),
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            cwd=str(self.cwd),
            env=scrubbed_environment(),
            input="",
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        tools: list[str] = []
        prefix = f"mcp__{MCP_SERVER_NAME}__"
        for event in self._events(completed.stdout):
            if event.get("type") == "system" and event.get("subtype") == "init":
                connected = {
                    server.get("name"): server.get("status")
                    for server in event.get("mcp_servers", [])
                }
                if connected.get(MCP_SERVER_NAME) != "connected":
                    raise QualificationError(
                        "Claude Code did not connect to the server"
                    )
                tools = sorted(
                    name[len(prefix) :]
                    for name in event.get("tools", [])
                    if isinstance(name, str) and name.startswith(prefix)
                )
        return self._parse(completed.stdout), tools

    @staticmethod
    def _events(stream: str) -> Iterable[dict[str, Any]]:
        for line in stream.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    def _parse(self, stream: str) -> list[Call]:
        prefix = f"mcp__{MCP_SERVER_NAME}__"
        pending: dict[str, tuple[str, dict[str, Any]]] = {}
        calls: list[Call] = []
        for event in self._events(stream):
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and str(
                        block.get("name", "")
                    ).startswith(prefix):
                        pending[block["id"]] = (
                            block["name"][len(prefix) :],
                            block.get("input") or {},
                        )
            elif event.get("type") == "user":
                content = event.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    found = pending.pop(block.get("tool_use_id"), None)
                    if found is None:
                        continue
                    tool, arguments = found
                    payload = block.get("content")
                    if isinstance(payload, list):
                        payload = "".join(
                            part.get("text", "")
                            for part in payload
                            if isinstance(part, dict)
                        )
                    text = payload if isinstance(payload, str) else ""
                    calls.append(
                        Call(
                            tool=tool,
                            arguments=arguments,
                            result=self._structured(text),
                            text=text,
                        )
                    )
        return calls


class Codex(Host):
    """Codex CLI, driven ephemerally with the server named only on the command line.

    `--ignore-user-config` keeps `~/.codex/config.toml` out of the session while
    leaving authentication where it is, `--ignore-rules` keeps user and project
    execpolicy files out, `--ephemeral` keeps the session off disk, and `-C` puts
    the working root in a temporary directory. The server is declared with `-c
    mcp_servers.*` overrides, so nothing is written to the user's configuration
    to declare it. `default_tools_approval_mode = "approve"` is what makes a
    non-interactive session able to complete an MCP call at all -- without it
    Codex records every call as cancelled, because there is nobody to ask.
    """

    host_id = "codex"

    def _overrides(self, server: Sequence[str]) -> list[str]:
        return [
            "-c",
            f"mcp_servers.{MCP_SERVER_NAME}.command={json.dumps(server[0])}",
            "-c",
            f"mcp_servers.{MCP_SERVER_NAME}.args={json.dumps(list(server[1:]))}",
            "-c",
            f'mcp_servers.{MCP_SERVER_NAME}.default_tools_approval_mode="approve"',
            "-c",
            f"mcp_servers.{MCP_SERVER_NAME}.startup_timeout_sec=60",
            "-c",
            f"mcp_servers.{MCP_SERVER_NAME}.tool_timeout_sec=180",
        ]

    def run_step(
        self, step: Step, *, server: Sequence[str] | None = None
    ) -> list[Call]:
        completed = subprocess.run(
            [
                str(host_executable(self.host_id)),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self.cwd),
                *self._overrides(server or self.server_command()),
                "--json",
                step.instruction,
            ],
            cwd=str(self.cwd),
            env=scrubbed_environment(),
            input="",
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        return self._parse(completed.stdout)

    def _parse(self, stream: str) -> list[Call]:
        calls: list[Call] = []
        for line in stream.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            if item.get("server") != MCP_SERVER_NAME:
                continue
            result = item.get("result")
            structured = None
            text = ""
            if isinstance(result, dict):
                structured = self._structured(result.get("structured_content"))
                content = result.get("content")
                if isinstance(content, list):
                    text = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                if structured is None:
                    structured = self._structured(text)
            calls.append(
                Call(
                    tool=str(item.get("tool", "")),
                    arguments=item.get("arguments") or {},
                    result=structured,
                    text=text,
                )
            )
        return calls


# --- the relay, used only where the section asks for it ------------------------

RELAY = (
    REPO_ROOT / "packages" / "omnivia-core-mcp" / "tests" / "_mcp_interrupted_relay.py"
)

#: The qualification-only staging helper. Named once, here, so the guard tests
#: can assert that nothing installed refers to it.
STAGING_HELPER = "qualification-stage-import-source.py"


def relay_command(
    installed: Installation,
    config: Path,
    *,
    withhold: str | None = None,
    marker: Path | None = None,
    tools_observed: Path | None = None,
) -> list[str]:
    command = [
        str(installed.python),
        str(RELAY),
        "--config",
        str(config),
        "--server-executable",
        str(installed.mcp_server),
    ]
    if withhold is not None and marker is not None:
        command += ["--withhold", withhold, "--marker", str(marker)]
    if tools_observed is not None:
        command += ["--tools-observed", str(tools_observed)]
    return command


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "omnivia-host-qualification", "version": "1"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _speak(
    installed: Installation, config: Path, cwd: Path, request: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Initialize, send one request, read until it is answered, then close.

    Stdin is held open until the answer arrives, deliberately. Closing it after
    writing -- which `subprocess.run(input=...)` does -- ends the session while
    the call is still in flight, and the resulting silence is indistinguishable
    from a refusal. A test that cannot tell those apart is not a test.

    Returns the answer, and every line stdout carried, so the caller can decide
    both what the server said and whether it said anything that was not protocol.
    """
    process = subprocess.Popen(
        [str(installed.mcp_server), "--config", str(config)],
        cwd=str(cwd),
        env=scrubbed_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None, "no pipes"
    answer: dict[str, Any] | None = None
    lines: list[str] = []
    try:
        for document in (_INITIALIZE, _INITIALIZED, request):
            process.stdin.write((json.dumps(document) + "\n").encode("utf-8"))
            process.stdin.flush()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            raw = process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == request.get("id"):
                answer = message
                break
    finally:
        process.stdin.close()
        process.kill()
        process.wait(timeout=60)
    return answer, lines


def undispatchable(installed: Installation, config: Path, cwd: Path, tool: str) -> bool:
    """Whether the installed server refuses `tool` outright.

    Spoken at the protocol, not through a host: an excluded tool is absent from
    the inventory, so no model can be asked to call it. This is the other half of
    the claim -- that naming it directly reaches no business handler -- and it is
    recorded as a protocol observation rather than as a host one.
    """
    answer, _ = _speak(
        installed,
        config,
        cwd,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        },
    )
    if answer is None:
        return False
    if "error" in answer:
        return True
    result = answer.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


def stdout_is_protocol_only(installed: Installation, config: Path, cwd: Path) -> bool:
    """Every line the server writes to stdout parses as one JSON-RPC message."""
    answer, lines = _speak(
        installed,
        config,
        cwd,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    if answer is None or not lines:
        return False
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            return False
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return False
    return True


# --- the journey ---------------------------------------------------------------


@dataclass
class Verdicts:
    """The case register, filled in as the run decides each one."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        case_id: str,
        host: str,
        *,
        tools: Sequence[str],
        disposition: str,
        passed: bool,
        **extra: Any,
    ) -> None:
        if case_id not in CASE_IDS:
            raise QualificationError(f"unknown case {case_id}")
        row = {
            "case_id": case_id,
            "host": host,
            "tools": list(tools),
            "disposition": disposition,
            "verdict": "pass" if passed else "fail",
        }
        row.update(extra)
        self.rows.append(row)
        print(
            f"  {'ok  ' if passed else 'FAIL'} {host} {case_id} [{disposition}]",
            file=sys.stderr,
        )


def one_call(calls: Sequence[Call], tool: str) -> Call:
    """The single call the step asked for, or a refusal naming what was seen."""
    matching = [call for call in calls if call.tool == tool]
    if len(matching) != 1:
        raise QualificationError(
            f"the host made {len(matching)} `{tool}` call(s) where one was required"
        )
    return matching[0]


def require_arguments(call: Call, expected: Mapping[str, Any]) -> None:
    """The model may not edit what it was told to send."""
    if call.arguments != dict(expected):
        raise QualificationError(f"the host altered the `{call.tool}` argument object")


def error_code(call: Call) -> str | None:
    """The canonical error code out of a refusal, and nothing else out of it.

    The adapter answers a refused call with one sanitized sentence carrying the
    encoded canonical response envelope. Only `error.code` is read from it; the
    sentence itself is never recorded, logged or returned.
    """
    start = call.text.find("{")
    if start < 0:
        return None
    try:
        envelope = json.loads(call.text[start:])
    except ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    for candidate in (envelope.get("error"), envelope):
        if isinstance(candidate, dict) and isinstance(candidate.get("code"), str):
            return str(candidate["code"])
    return None


def journey(
    host: Host,
    core: Core,
    installed: Installation,
    config: Path,
    descriptor: Mapping[str, Any],
    verdicts: Verdicts,
    work: Path,
) -> None:
    """Sections 13.B, 13.D, 13.F and 13.I, in the order a host would meet them."""
    host_id = host.host_id
    print(f"=== {host_id}: authoring journey ===", file=sys.stderr)

    # 13.B.3-4 -- capture, then find it.
    capture_input = {
        "source_native_id": CAPTURE_SOURCE_ID,
        "media_type": "text/markdown",
        "text": CAPTURE_TEXT,
    }
    capture_arguments = _mutation(capture_input, CAPTURE_KEY)
    calls = host.run_step(
        Step("capture_created", instruction("evidence_capture", capture_arguments))
    )
    captured = one_call(calls, "evidence_capture")
    require_arguments(captured, capture_arguments)
    if captured.failed or captured.result is None:
        raise QualificationError("the host's capture did not return a canonical result")
    evidence_id = captured.result["evidence_id"]
    verdicts.record(
        "capture_created",
        host_id,
        tools=["evidence_capture"],
        disposition=captured.result["capture_disposition"],
        passed=captured.result["capture_disposition"] == "created",
        identity_digest=digest(evidence_id),
    )

    search_arguments = {"query": CAPTURE_TERM, "limit": 20}
    calls = host.run_step(
        Step("capture_searchable", instruction("evidence_search", search_arguments))
    )
    searched = one_call(calls, "evidence_search")
    found = [
        artifact["evidence_id"]
        for artifact in (searched.result or {}).get("evidence", [])
    ]
    owner_found = [
        artifact["evidence_id"]
        for artifact in core.read("evidence", "search", search_arguments).get(
            "evidence", []
        )
    ]
    verdicts.record(
        "capture_searchable",
        host_id,
        tools=["evidence_search"],
        disposition="observed",
        passed=evidence_id in found and evidence_id in owner_found,
        identity_digest=digest(evidence_id),
        count=len(found),
    )

    # 13.B.5-8 -- an evidence-backed proposal, and where it may and may not be seen.
    source_reference = {"kind": "direct_submission", "source_id": CAPTURE_SOURCE_ID}
    memory_input = {
        "record_type": "memory.fact",
        "domain_scope": "product.core",
        "content": {"fact": MEMORY_FACT},
        "evidence_disposition": "available",
        "sources": [source_reference],
        "assertion": {
            "actor_id": "mcp-author",
            "actor_kind": "agent",
            "actor_role": "author",
            "asserted_at": "2026-01-01T00:00:00Z",
            "evidence": [{"source": source_reference}],
        },
    }
    memory_arguments = _mutation(memory_input, MEMORY_KEY)
    calls = host.run_step(
        Step("memory_proposed_only", instruction("memory_create", memory_arguments))
    )
    created = one_call(calls, "memory_create")
    require_arguments(created, memory_arguments)
    if created.failed or created.result is None:
        raise QualificationError("the host's memory_create did not return a result")
    record = created.result["record"]
    identity = record["provenance"]["identity"]
    verdicts.record(
        "memory_proposed_only",
        host_id,
        tools=["memory_create"],
        disposition="proposed",
        passed=(
            record.get("authority_level") == "proposed"
            and identity.get("governance_state") == "proposed"
            and identity.get("layer") == "l1"
        ),
        identity_digest=digest(identity["record_id"]),
    )

    default_arguments = {"query": CAPTURE_TERM, "limit": 20}
    calls = host.run_step(
        Step(
            "memory_default_view_hidden",
            instruction("memory_search", default_arguments),
        )
    )
    defaulted = one_call(calls, "memory_search")
    default_records = (defaulted.result or {}).get("records", [])
    verdicts.record(
        "memory_default_view_hidden",
        host_id,
        tools=["memory_search"],
        disposition="absent",
        passed=default_records == [],
        count=len(default_records),
    )

    candidate_arguments = {"query": CAPTURE_TERM, "view": "candidates", "limit": 20}
    calls = host.run_step(
        Step(
            "memory_candidate_view_visible",
            instruction("memory_search", candidate_arguments),
        )
    )
    candidates = one_call(calls, "memory_search")
    candidate_records = (candidates.result or {}).get("records", [])
    matched = [
        entry
        for entry in candidate_records
        if entry["provenance"]["identity"]["record_id"] == identity["record_id"]
    ]
    verdicts.record(
        "memory_candidate_view_visible",
        host_id,
        tools=["memory_search"],
        disposition="candidate",
        passed=len(matched) == 1,
        count=len(candidate_records),
        identity_digest=digest(identity["record_id"]),
    )

    # 13.B.9 -- the same key and the same input settle to the same answer.
    calls = host.run_step(
        Step(
            "capture_replay_stable", instruction("evidence_capture", capture_arguments)
        )
    )
    replayed = one_call(calls, "evidence_capture")
    replayed_all = core.read("evidence", "search", search_arguments).get("evidence", [])
    verdicts.record(
        "capture_replay_stable",
        host_id,
        tools=["evidence_capture"],
        disposition="stable_replay",
        passed=(
            replayed.result == captured.result
            and len([a for a in replayed_all if a["evidence_id"] == evidence_id]) == 1
        ),
        matches_identity_digest=digest(evidence_id),
        count=len(replayed_all),
    )

    calls = host.run_step(
        Step("memory_replay_stable", instruction("memory_create", memory_arguments))
    )
    replayed_memory = one_call(calls, "memory_create")
    owner_candidates = core.read("memory", "search", candidate_arguments).get(
        "records", []
    )
    verdicts.record(
        "memory_replay_stable",
        host_id,
        tools=["memory_create"],
        disposition="stable_replay",
        passed=(
            replayed_memory.result == created.result
            and len(
                [
                    entry
                    for entry in owner_candidates
                    if entry["provenance"]["identity"]["record_id"]
                    == identity["record_id"]
                ]
            )
            == 1
        ),
        matches_identity_digest=digest(identity["record_id"]),
        count=len(owner_candidates),
    )

    # 13.B.10 -- the same key and a different input do not.
    changed_capture = _mutation(
        {**capture_input, "text": CAPTURE_TEXT_CHANGED}, CAPTURE_KEY
    )
    calls = host.run_step(
        Step(
            "capture_changed_input_conflict",
            instruction("evidence_capture", changed_capture),
        )
    )
    conflicted = one_call(calls, "evidence_capture")
    verdicts.record(
        "capture_changed_input_conflict",
        host_id,
        tools=["evidence_capture"],
        disposition="idempotency_conflict",
        passed=conflicted.failed and error_code(conflicted) == "idempotency_conflict",
    )

    changed_memory = _mutation(
        {**memory_input, "content": {"fact": MEMORY_FACT_CHANGED}}, MEMORY_KEY
    )
    calls = host.run_step(
        Step(
            "memory_changed_input_conflict",
            instruction("memory_create", changed_memory),
        )
    )
    conflicted_memory = one_call(calls, "memory_create")
    verdicts.record(
        "memory_changed_input_conflict",
        host_id,
        tools=["memory_create"],
        disposition="idempotency_conflict",
        passed=conflicted_memory.failed
        and error_code(conflicted_memory) == "idempotency_conflict",
    )

    # 13.B.11 -- the service is independently owned and outlives the session.
    verdicts.record(
        "service_healthy_after_session",
        host_id,
        tools=[],
        disposition="healthy",
        passed=core.healthy(),
    )

    # 13.D -- one staged descriptor, started and observed through the host alone.
    print(f"=== {host_id}: import journey ===", file=sys.stderr)
    import_arguments = _mutation({"source": dict(descriptor)}, IMPORT_KEY)
    calls = host.run_step(
        Step("import_started_one_job", instruction("import_start", import_arguments))
    )
    started = one_call(calls, "import_start")
    require_arguments(started, import_arguments)
    if started.failed or started.result is None:
        raise QualificationError("the host's import_start did not return a job")
    job_id = started.result["job"]["identity"]["job_id"]
    verdicts.record(
        "import_started_one_job",
        host_id,
        tools=["import_start"],
        disposition="created",
        passed=True,
        identity_digest=digest(job_id),
    )

    calls = host.run_step(
        Step("import_replay_same_job", instruction("import_start", import_arguments))
    )
    replayed_import = one_call(calls, "import_start")
    verdicts.record(
        "import_replay_same_job",
        host_id,
        tools=["import_start"],
        disposition="stable_replay",
        passed=(replayed_import.result or {})
        .get("job", {})
        .get("identity", {})
        .get("job_id")
        == job_id,
        matches_identity_digest=digest(job_id),
    )

    changed_import = _mutation(
        {"source": {**dict(descriptor), "source_kind": "document"}}, IMPORT_KEY
    )
    calls = host.run_step(
        Step(
            "import_changed_input_conflict",
            instruction("import_start", changed_import),
        )
    )
    conflicted_import = one_call(calls, "import_start")
    verdicts.record(
        "import_changed_input_conflict",
        host_id,
        tools=["import_start"],
        disposition="idempotency_conflict",
        passed=conflicted_import.failed
        and error_code(conflicted_import) == "idempotency_conflict",
    )

    # The owner waits for the terminal state; the host observes it. Waiting is a
    # read, and 13.B permits the harness to read. Deciding is not: the terminal
    # fact below is the one the host itself was given.
    terminal = _await_terminal(core, job_id)
    calls = host.run_step(
        Step("job_get_terminal", instruction("job_get", {"job_id": job_id}))
    )
    observed = one_call(calls, "job_get")
    observed_result = observed.result or {}
    verdicts.record(
        "job_get_terminal",
        host_id,
        tools=["job_get"],
        disposition="succeeded",
        passed=(
            observed_result.get("job", {}).get("state") == "succeeded"
            and observed_result.get("terminal_result", {}).get("state") == "succeeded"
            and terminal["job"]["state"] == "succeeded"
        ),
        identity_digest=digest(job_id),
    )

    pages, ordered, stable = _paginate(host, job_id)
    verdicts.record(
        "job_events_paginated_stable",
        host_id,
        tools=["job_events"],
        disposition="observed",
        passed=pages >= 2 and ordered and stable,
        pages=pages,
    )

    # The accounting the host was given must add up, and the artifact that
    # accounting claims must be retrievable through a tool. The query is the
    # staged descriptor's own `source_kind` rather than a word this file invented:
    # a word of our own could only prove that some artifact matched something.
    accounting = observed_result.get("terminal_result", {}).get("result", {})
    query = {"query": str(descriptor["source_kind"]), "limit": 50}
    calls = host.run_step(
        Step("import_evidence_retrievable", instruction("evidence_search", query))
    )
    retrieved = one_call(calls, "evidence_search")
    retrieved_ids = {
        artifact["evidence_id"]
        for artifact in (retrieved.result or {}).get("evidence", [])
    }
    owner_ids = {
        artifact["evidence_id"]
        for artifact in core.read("evidence", "search", query).get("evidence", [])
    }
    verdicts.record(
        "import_evidence_retrievable",
        host_id,
        tools=["evidence_search"],
        disposition="observed",
        passed=(
            accounting.get("evidence_records_created") == 1
            and accounting.get("failed_items") == 0
            and accounting.get("discovered_items") == 1
            and len(retrieved_ids) == 1
            and retrieved_ids == owner_ids
        ),
        count=len(retrieved_ids),
    )

    # 13.D -- the excluded operations, absent and undispatchable.
    verdicts.record(
        "excluded_undispatchable",
        host_id,
        tools=list(EXCLUDED_TOOLS[:2]),
        disposition="refused",
        passed=all(
            undispatchable(installed, config, host.cwd, tool)
            for tool in EXCLUDED_TOOLS[:2]
        ),
    )
    verdicts.record(
        "stdout_protocol_only",
        host_id,
        tools=[],
        disposition="observed",
        passed=stdout_is_protocol_only(installed, config, host.cwd),
    )

    # 13.F -- Core commits, the answer never arrives, and the key recovers it.
    _ambiguous(host, core, installed, config, verdicts, work)

    # 13.I -- restart both sides, then observe again.
    core.stop()
    core.start()
    calls = host.run_step(
        Step("restart_read_observation", instruction("job_get", {"job_id": job_id}))
    )
    after = one_call(calls, "job_get")
    verdicts.record(
        "restart_read_observation",
        host_id,
        tools=["job_get"],
        disposition="observed",
        passed=(after.result or {}).get("job", {}).get("state") == "succeeded",
        identity_digest=digest(job_id),
    )

    # 13.I -- revoke, and prove what revocation is and is not.
    core.admin("mcp", "revoke", "--host", host.host_id)
    calls = host.run_step(
        Step("revocation_blocks_host", instruction("job_get", {"job_id": job_id}))
    )
    blocked = [call for call in calls if call.tool == "job_get"]
    verdicts.record(
        "revocation_blocks_host",
        host_id,
        tools=["job_get"],
        disposition="failed_closed",
        passed=not blocked or all(call.failed for call in blocked),
    )
    owned = core.read("job", "get", {"job_id": job_id})
    verdicts.record(
        "revocation_preserves_committed_job",
        host_id,
        tools=[],
        disposition="succeeded",
        passed=owned["job"]["state"] == "succeeded",
        identity_digest=digest(job_id),
    )
    verdicts.record(
        "owner_cli_observes_job",
        host_id,
        tools=[],
        disposition="observed",
        passed=owned["job"]["identity"]["job_id"] == job_id,
        identity_digest=digest(job_id),
    )


def _await_terminal(core: Core, job_id: str, timeout: float = 300.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answered = core.read("job", "get", {"job_id": job_id})
        if answered["job"]["state"] in ("succeeded", "failed", "cancelled"):
            return answered
        time.sleep(1.0)
    raise QualificationError("the import job did not reach a terminal state")


#: One event per page, which is what makes the traversal a traversal: a settled
#: import has more events than this, so the first page cannot be the whole
#: snapshot and the continuation token has to be followed to see the rest. The
#: ceiling bounds a host session per page, not the contract's 1,000.
_EVENT_PAGE_LIMIT = 1
_MAXIMUM_PAGES = 8


def _paginate(host: Host, job_id: str) -> tuple[int, bool, bool]:
    """One event per page until the stream is exhausted, traversed twice.

    Ordering is checked across pages, and the whole traversal is then repeated
    from the start: a stream that is ordered but not snapshot-stable passes the
    first pass and fails the second, which is the point of doing it twice.
    """
    traversals: list[list[tuple[int, str]]] = []
    page_counts: list[int] = []
    for _ in range(2):
        collected: list[tuple[int, str]] = []
        page: dict[str, Any] = {}
        pages = 0
        while pages < _MAXIMUM_PAGES:
            arguments: dict[str, Any] = {"job_id": job_id, "limit": _EVENT_PAGE_LIMIT}
            if page:
                arguments["page"] = page
            calls = host.run_step(
                Step(
                    "job_events_paginated_stable", instruction("job_events", arguments)
                )
            )
            answered = one_call(calls, "job_events").result or {}
            pages += 1
            for event in answered.get("events", []):
                collected.append(
                    (event.get("event_sequence", 0), str(event.get("event_kind", "")))
                )
            page = answered.get("page") or {}
            if not page.get("continuation_token"):
                break
        traversals.append(collected)
        page_counts.append(pages)
    first, second = traversals
    sequences = [entry[0] for entry in first]
    ordered = sequences == sorted(sequences) and len(first) >= 2
    return page_counts[0], ordered, first == second


def _ambiguous(
    host: Host,
    core: Core,
    installed: Installation,
    config: Path,
    verdicts: Verdicts,
    work: Path,
) -> None:
    """Section 13.F, staged where a real interruption happens: in the pipe.

    The relay forwards the installed executable and drops exactly one
    `evidence_capture` response *after* the server produced it, so Core has
    committed and the host has not been told. Both sides are then restarted and
    the identical key is replayed directly against the installed executable. The
    claim is not that nothing was written -- something was -- but that exactly
    one artifact exists and the canonical answer is the same one.
    """
    marker = work / f"{host.host_id}-withheld.marker"
    arguments = _mutation(
        {
            "source_native_id": CAPTURE_INTERRUPTED_SOURCE_ID,
            "media_type": "text/markdown",
            "text": CAPTURE_INTERRUPTED_TEXT,
        },
        CAPTURE_INTERRUPTED_KEY,
    )
    host.run_step(
        Step("ambiguous_capture_recovered", instruction("evidence_capture", arguments)),
        server=relay_command(
            installed, config, withhold="evidence_capture", marker=marker
        ),
    )
    if not marker.is_file():
        raise QualificationError("the interruption never happened")

    core.stop()
    core.start()
    calls = host.run_step(
        Step("ambiguous_capture_recovered", instruction("evidence_capture", arguments))
    )
    recovered = one_call(calls, "evidence_capture")
    if recovered.failed or recovered.result is None:
        raise QualificationError("the same-key replay did not return a result")
    artifacts = [
        artifact["evidence_id"]
        for artifact in core.read(
            "evidence", "search", {"query": CAPTURE_INTERRUPTED_TERM, "limit": 20}
        ).get("evidence", [])
    ]
    verdicts.record(
        "ambiguous_capture_recovered",
        host.host_id,
        tools=["evidence_capture"],
        disposition="recovered",
        passed=(
            artifacts.count(recovered.result["evidence_id"]) == 1
            and len(artifacts) == 1
            and recovered.result["source"]["source_id"] == CAPTURE_INTERRUPTED_SOURCE_ID
        ),
        identity_digest=digest(recovered.result["evidence_id"]),
        count=len(artifacts),
        relayed=True,
    )


def inventory_for(
    host: Host, installed: Installation, config: Path, work: Path
) -> tuple[list[str], str]:
    """The eleven tools, observed the way each host makes observable."""
    if isinstance(host, ClaudeCode):
        return host.inventory(), "host_init_event"
    observed = work / "codex-tools-observed.json"
    host.run_step(
        Step("inventory_eleven_tools", "Reply with the single word DONE.", 2),
        server=relay_command(installed, config, tools_observed=observed),
    )
    if not observed.is_file():
        raise QualificationError("Codex never requested the tool inventory")
    names = json.loads(observed.read_text(encoding="utf-8"))
    return sorted(names), "relay_observed_tools_list"


# --- the record ----------------------------------------------------------------


def write_record(document: Mapping[str, Any], path: Path) -> None:
    """Write only when the run passed, and never over a better record.

    A failing run that overwrote a passing one would destroy the only evidence
    the repository holds and replace it with evidence of nothing.
    """
    existing = None
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = None
    if (
        document["verdict"] != "pass"
        and isinstance(existing, dict)
        and existing.get("verdict") == "pass"
    ):
        raise QualificationError(
            "this run failed and a passing record already exists; nothing was written"
        )
    findings = verify_record(document, commit=document["commit"])
    if document["verdict"] == "pass" and findings:
        raise QualificationError(f"the record would not pass its own guard: {findings}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def repository_commit() -> str:
    completed = run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, timeout=60)
    return completed.stdout.strip()


# --- entry point ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        required=True,
        help="prepared wheelhouse to install from, or where --acquire builds one",
    )
    parser.add_argument(
        "--acquire",
        action="store_true",
        help="run the online acquisition phase first; without it nothing reaches an index",
    )
    parser.add_argument(
        "--acquire-only",
        action="store_true",
        help="run the online acquisition phase and stop, so the qualification run "
        "itself can be started later, elsewhere, or under a different approval",
    )
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--host", choices=[*HOSTS, "both"], default="both")
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="interpreter used to build wheels and create the qualification venv",
    )
    for host in HOSTS:
        parser.add_argument(
            f"--approved-{host}-version",
            default=None,
            help=f"approved release replacement for {host}; recorded with the results",
        )
    parser.add_argument("--approved-os-version", default=None)
    parser.add_argument("--approved-os-build", default=None)
    return parser


def resolve_hosts(arguments: argparse.Namespace) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    for host in HOSTS:
        observed = host_version(host)
        approved = getattr(arguments, f"approved_{host.replace('-', '_')}_version")
        required = REQUIRED_HOST_VERSIONS[host]
        if observed != required and approved != observed:
            raise QualificationError(
                f"{host} is {observed}, this lane qualifies {required}. Re-run with "
                f"--approved-{host}-version {observed} to record an approved "
                "replacement."
            )
        facts[host] = {
            "version": observed,
            "required_version": required,
            "approved": observed != required,
            "executed": False,
        }
    return facts


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    selected = HOSTS if arguments.host == "both" else (arguments.host,)
    # Absolute but not resolved: `.venv/bin/python` is a symlink to the base
    # interpreter, and following it would build wheels with an interpreter that
    # has neither `build` nor `hatchling` and no virtual environment at all.
    interpreter = (
        arguments.python
        if arguments.python.is_absolute()
        else Path.cwd() / arguments.python
    )

    try:
        if arguments.acquire_only:
            acquire(arguments.wheelhouse.resolve(), interpreter)
            return 0
        hosts = resolve_hosts(arguments)
        system = operating_system()
        approved_os = False
        if (
            system["os_version"] != REQUIRED_OS["version"]
            or system["os_build"] != REQUIRED_OS["build"]
            or system["arch"] != REQUIRED_OS["arch"]
        ):
            if (
                arguments.approved_os_version != system["os_version"]
                or arguments.approved_os_build != system["os_build"]
            ):
                raise QualificationError(
                    f"this host is macOS {system['os_version']} build "
                    f"{system['os_build']} {system['arch']}; the lane qualifies "
                    f"{REQUIRED_OS['version']} build {REQUIRED_OS['build']} "
                    f"{REQUIRED_OS['arch']}. Approve a replacement explicitly."
                )
            approved_os = True

        if arguments.acquire:
            acquire(arguments.wheelhouse.resolve(), interpreter)

        before = config_surfaces()
        verdicts = Verdicts()
        inventories: dict[str, Any] = {}
        # A short temporary parent: a unix-domain endpoint has a small encoded
        # path ceiling and macOS's ambient TMPDIR is nested deeply enough to
        # exceed it.
        parent = "/tmp" if Path("/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="omnivia-hostqual-", dir=parent) as tmp:
            work = Path(tmp)
            work.chmod(0o700)
            installed = install(arguments.wheelhouse.resolve(), work, interpreter)
            for host_id in selected:
                root = work / host_id
                root.mkdir(mode=0o700)
                core = Core(
                    installed=installed, root=root, state=root / "installation-state"
                )
                try:
                    core.bootstrap()
                    # Staged while nothing owns the workspace: the minted workspace
                    # has no service yet, so this process is briefly its only writer,
                    # which is the same window the runtime's own tests stage in.
                    descriptor = json_document(
                        run(
                            [
                                str(installed.python),
                                str(REPO_ROOT / "scripts" / STAGING_HELPER),
                                "--workspace",
                                str(core.workspace),
                                "--installed-prefix",
                                str(installed.prefix),
                            ],
                            cwd=work,
                            timeout=180,
                        ).stdout,
                        "qualification staging",
                    )
                    core.start()
                    config = core.configure(host_id, "authoring")
                    # One service from here on: the administration controls the CLI
                    # still needs are answered by whichever of this installation's
                    # services is up, and the journey's "the service outlived the
                    # session" claim is about exactly one process.
                    core.stop_bootstrap()
                    host: Host = (
                        ClaudeCode(installed, work, config)
                        if host_id == "claude-code"
                        else Codex(installed, work, config)
                    )
                    tools, observation = inventory_for(host, installed, config, work)
                    inventories[host_id] = {
                        "profile": "authoring",
                        "observation": observation,
                        "tools": tools,
                        "count": len(tools),
                    }
                    verdicts.record(
                        "inventory_eleven_tools",
                        host_id,
                        tools=list(AUTHORING_TOOLS),
                        disposition="observed",
                        passed=tuple(tools) == AUTHORING_TOOLS
                        and core.advertised_tool_count(host_id) == 11,
                        count=len(tools),
                    )
                    verdicts.record(
                        "inventory_excluded_absent",
                        host_id,
                        tools=list(EXCLUDED_TOOLS),
                        disposition="absent",
                        passed=not set(EXCLUDED_TOOLS) & set(tools),
                    )
                    journey(host, core, installed, config, descriptor, verdicts, work)
                    hosts[host_id]["executed"] = True
                finally:
                    core.stop()
                    core.stop_bootstrap()

        after = config_surfaces()
        if before != after:
            changed = sorted(k for k in before if before[k] != after.get(k))
            raise QualificationError(f"the run changed host configuration: {changed}")

        passed = all(row["verdict"] == "pass" for row in verdicts.rows) and len(
            selected
        ) == len(HOSTS)
        document = {
            "format": EVIDENCE_FORMAT,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "commit": repository_commit(),
            "specification": {
                "requirements": "R004-v1.3",
                "sections": ["10", "13.B", "13.D", "13.F", "13.I"],
            },
            "environment": {**system, "approved": approved_os},
            "hosts": hosts,
            "installation": installed.facts,
            "inventories": inventories,
            "cases": verdicts.rows,
            "verdict": "pass" if passed else "fail",
        }
        write_record(document, arguments.evidence)
        print(
            f"qualification {'passed' if passed else 'FAILED'}; "
            f"record at {arguments.evidence}",
            file=sys.stderr,
        )
        return 0 if passed else 1
    except QualificationError as refusal:
        print(f"host qualification refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
