"""Engineering Memory through the real CLI (SPEC-CORE-ENGMEM-001, P0-07).

One governed workspace, one real managed-start service, and the installed
`omnivia` entry point -- the same commands an operator types, machine-readable
JSON out. What is proven is that the CLI's grant genuinely reaches the
engineering-memory surface end to end: a continuity session is registered,
checkpointed and closed, its final checkpoint answers a handoff read that
survives a real stop-and-restart of the service, a proposed engineering
observation is written through `memory.create`, it appears under the
`candidates` view and never under `accepted` -- because nothing has passed
governance -- and it is reachable by `engineering.expand` and folded into an
`engineering.context.build` resume pack alongside the caller's own continuity
checkpoints.

The one thing this file deliberately does *not* claim as a success: a
`current_safe`-scored search naming a repository snapshot this workspace never
registered coverage for through `engineering.source.record`. That is refused
by the real handler, honestly and before any frontier read, and the refusal --
not a fabricated result -- is what this file asserts.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_managed_start as managed

WORKSPACE_ID = managed.WORKSPACE_ID


def _run_cli(home: Path, *arguments: str) -> tuple[int, dict[str, Any]]:
    """One installed `omnivia` invocation, answered with its JSON envelope."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            WORKSPACE_ID,
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    return completed.returncode, dict(json.loads(completed.stdout))


def _cli(home: Path, *arguments: str) -> dict[str, Any]:
    """One successful invocation, answered with its JSON result."""
    returncode, envelope = _run_cli(home, *arguments)
    assert returncode == 0, (returncode, envelope)
    return dict(envelope["result"])


def _refusal(home: Path, *arguments: str) -> dict[str, Any]:
    """One refused invocation, answered with its typed error document."""
    returncode, envelope = _run_cli(home, *arguments)
    assert returncode != 0, envelope
    return dict(envelope["error"])


def _mutation(home: Path, *arguments: str, key: str) -> dict[str, Any]:
    return _cli(home, *arguments, "--idempotency-key", key, "--json")


def _lifecycle(home: Path, *arguments: str) -> dict[str, Any]:
    """One `service` administration command, answered with its safe document."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            WORKSPACE_ID,
            *arguments,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


@pytest.fixture
def home() -> Iterator[Path]:
    """One bootstrapped installation per test, with nothing of it left running."""
    root = Path(tempfile.mkdtemp(prefix=managed.HOME_PREFIX, dir="/tmp"))
    managed._bootstrap(root)
    # This fixture's legacy-migration bootstrap predates the shared client's
    # owner-private descriptor provenance rule (see `test_managed_start.py`'s
    # own installed-CLI test). Reproduce that precondition before crossing the
    # installed client boundary below.
    for directory in (
        root / "installation-state",
        root / "installation-state" / "runtime",
        managed._runtime_directory(root),
    ):
        directory.chmod(0o700)
    try:
        yield root
    finally:
        for pid in managed._service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(root, ignore_errors=True)


_OBSERVATION_CONTENT: dict[str, Any] = {
    "schema_version": "1.0",
    "kind": "failed_approach",
    "title": "Continuity restore retries do not fix stale session state",
    "summary": "Session restoration keeps failing even after a credential retry.",
    "what": "Adding credential retries did not change the reproduced failure.",
    "assertion_basis": "derived",
}


def test_the_engineering_memory_vertical_runs_through_the_installed_cli(
    home: Path,
) -> None:
    """Register, checkpoint, close, handoff, restart, propose, search, expand.

    The continuity half of the vertical is a real, durable CLI mutation
    sequence, closed with a real stop-and-restart of the owning service. The
    memory half writes one proposed engineering observation and proves it
    reads back as a candidate and never as accepted knowledge -- nothing here
    has been through governance. `engineering.expand` and the `resume` pack of
    `engineering.context.build` then read that same authorised frontier and
    the caller's own continuity checkpoints, entirely through the installed
    entry point.
    """
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr
    (initial_pid,) = managed._service_pids(home)

    # --- continuity.session.register --------------------------------------
    registered = _mutation(
        home,
        "continuity",
        "register",
        "--input-json",
        json.dumps(
            {
                "schema_version": "engineering.1",
                "checkout_hint": "/work/engmem-cli-test",
                "host_session_ref": "cli-qualification-session",
            }
        ),
        key="cli-continuity-register-1",
    )
    session = registered["session"]
    assert session["state"] == "active"
    session_id = session["session_id"]

    # --- continuity.checkpoint.append, and its idempotent retry -------------
    append_payload = {
        "session_id": session_id,
        "payload": {
            "objective": "Investigate CLI-driven checkpoint durability",
            "checkpoint_kind": "periodic",
            "observations": [
                {"statement": "The CLI reached the real service.", "support": "claimed"}
            ],
            "unresolved_work": ["Confirm the checkpoint survives a restart"],
            "next_actions": ["Stop and restart the service, then re-read the handoff"],
        },
    }
    appended = _mutation(
        home,
        "continuity",
        "checkpoint",
        "--input-json",
        json.dumps(append_payload),
        "--record-version",
        "seq-0",
        key="cli-checkpoint-1",
    )
    receipt = appended["receipt"]
    assert receipt["sequence"] == 1
    assert receipt["content_digest"].startswith("sha256:")

    # A lost-reply retry of the exact same mutation, through a fresh process,
    # returns the original receipt rather than a second checkpoint.
    replayed = _mutation(
        home,
        "continuity",
        "checkpoint",
        "--input-json",
        json.dumps(append_payload),
        "--record-version",
        "seq-0",
        key="cli-checkpoint-1",
    )
    assert replayed == appended

    # --- continuity.session.close -------------------------------------------
    close_payload = {
        "session_id": session_id,
        "expected_sequence": 1,
        "final_checkpoint": {
            "objective": "Wrap up the CLI-driven continuity check",
            "checkpoint_kind": "session_close",
            "unresolved_work": ["None -- durability is confirmed below"],
        },
    }
    closed = _mutation(
        home,
        "continuity",
        "close",
        "--input-json",
        json.dumps(close_payload),
        "--record-version",
        "seq-1",
        key="cli-close-1",
    )
    assert closed["state"] == "closed"
    assert closed["checkpoint_recorded"] is True
    final_checkpoint_id = closed["receipt"]["checkpoint_id"]

    # --- continuity.handoff.read, before the restart -------------------------
    handoff_input = json.dumps({"checkpoint_id": final_checkpoint_id})
    handoff = _cli(home, "continuity", "handoff", "--input-json", handoff_input, "--json")[
        "handoff"
    ]
    assert handoff["format_version"] == "continuity_handoff.v1"
    assert handoff["redacted"] is False
    assert "Wrap up the CLI-driven continuity check" == handoff["objective"]

    # --- a real stop, then a real restart, of the owning service -------------
    stopped = _lifecycle(home, "service", "stop")
    assert stopped["code"] == "stop_stopped"
    assert managed._service_pids(home) == []

    # The next call auto-starts a fresh service through the same
    # `connect_managed_local` path every other command uses; nothing here is a
    # test-only restart hook. The acknowledged checkpoint answers identically.
    reread = _cli(home, "continuity", "handoff", "--input-json", handoff_input, "--json")[
        "handoff"
    ]
    assert reread == handoff
    (restarted_pid,) = managed._service_pids(home)
    assert restarted_pid != initial_pid

    # --- memory.create: a proposed engineering observation --------------------
    created = _mutation(
        home,
        "memory",
        "create",
        "--input-json",
        json.dumps(
            {
                "record_type": "knowledge.finding",
                "domain_scope": "engineering.codebase",
                "content": dict(_OBSERVATION_CONTENT),
                "evidence_disposition": "unavailable",
                "sources": [],
                "assertion": {
                    "actor_id": "cli-qualification-agent",
                    "actor_kind": "agent",
                    "actor_role": "contributor",
                    "asserted_at": "2026-09-27T00:00:00Z",
                    "evidence": [],
                },
            }
        ),
        key="cli-memory-create-1",
    )
    identity = created["record"]["provenance"]["identity"]
    record_id, version = identity["record_id"], identity["version"]

    # --- engineering.search: a candidate, and never accepted before governance
    candidates = _cli(
        home,
        "engineering",
        "search",
        "--input-json",
        json.dumps({"query": "session restoration", "view": "candidates"}),
        "--json",
    )
    matching = [p for p in candidates["previews"] if p["record_id"] == record_id]
    assert len(matching) == 1
    assert matching[0]["governance_state"] == "candidate"

    accepted = _cli(
        home,
        "engineering",
        "search",
        "--input-json",
        json.dumps({"query": "session restoration", "view": "accepted"}),
        "--json",
    )
    assert accepted["previews"] == []

    # --- engineering.expand: the anchor resolves, at the exact version --------
    expanded = _cli(
        home,
        "engineering",
        "expand",
        "--input-json",
        json.dumps({"anchor": {"record_id": record_id, "version": version}}),
        "--json",
    )
    assert expanded["nodes"][0]["record_id"] == record_id
    assert expanded["truncated"] is False

    # --- engineering.context.build: the resume pack reads the caller's own ----
    # continuity checkpoints, closed session included, alongside the frontier.
    pack = _cli(
        home,
        "engineering",
        "context",
        "--input-json",
        json.dumps(
            {
                "query": "continuity checkpoint durability",
                "targets": [
                    {"snapshot_id": "esnap-cli-qualification", "snapshot_kind": "git_commit"}
                ],
                "profile": "resume",
            }
        ),
        "--json",
    )["pack"]
    working_context = [
        section for section in pack["sections"] if section["partition"] == "working_context"
    ]
    rendered = json.dumps(working_context)
    assert "Wrap up the CLI-driven continuity check" in rendered
    assert "Investigate CLI-driven checkpoint durability" in rendered


def test_current_safe_search_without_registered_coverage_is_refused(home: Path) -> None:
    """`current_safe` needs coverage this workspace never recorded, and says so.

    Nothing in this test ever calls `engineering.source.record`, so no
    repository snapshot has authoritative coverage in this workspace. A
    `current_safe`-scored search naming one is refused before any frontier
    read or ranking -- `dependency_unavailable` / `applicability_pending`, the
    same signal the runtime's own applicability suite pins -- rather than
    silently downgraded to `diagnostic` or reported as a match. This is not a
    missing authorisation grant: the local CLI owner is fully granted the
    engineering family (there is no capability toggle here, unlike the
    Decision family's `decisions configure`). What is missing is the trusted
    source coverage `current_safe` requires as its prerequisite, and the CLI
    cannot supply that for a snapshot nobody has registered.
    """
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    refused = _refusal(
        home,
        "engineering",
        "search",
        "--input-json",
        json.dumps(
            {
                "query": "session restoration",
                "view": "candidates",
                "applicability_mode": "current_safe",
                "repository_target": {"snapshot_id": "esnap-cli-unregistered"},
            }
        ),
        "--json",
    )
    assert refused["code"] == "dependency_unavailable"
    assert refused["message"] == "applicability_pending"
