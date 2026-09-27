"""The engineering continuity vertical through the real CLI (plan PR-H2).

One managed-start service and the installed `omnivia` entry point -- the same
commands an operator types, machine-readable JSON out. The vertical is the
spec's §1.1 initial slice, its CLI half: register a session, append a
checkpoint, close with a final checkpoint, read the handoff, search the
working context and build a resume pack -- every call carried by the same real
:class:`~omnivia_core_client.ServiceClient` the dispatch tests pin, against the
service's own authorisation and fencing.

The precondition half is asserted too: an append that names a stale expected
predecessor is the mutation coordinator's own typed precondition refusal, not
a crash -- the CLI half of AC-023.
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


def _register_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "engineering.1",
        "checkout_hint": "/home/dev/app",
        "host_session_ref": "cli-conv-1",
    }
    base.update(overrides)
    return base


def _append_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "payload": {
            "objective": "Investigate the session-restoration failure",
            "checkpoint_kind": "periodic",
            "observations": [
                {"statement": "Retries did not change the failure.", "support": "claimed"}
            ],
            "unresolved_work": ["Why does restore fail after credential validation?"],
            "next_actions": ["Inspect the session-invalidation path"],
        },
    }
    base.update(overrides)
    return base


def _close_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "expected_sequence": 1,
        "final_checkpoint": {
            "objective": "Wrap up the investigation",
            "checkpoint_kind": "session_close",
            "unresolved_work": ["Root cause still unconfirmed"],
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def home() -> Iterator[Path]:
    """One bootstrapped installation per test, with nothing of it left running."""
    root = Path(tempfile.mkdtemp(prefix=managed.HOME_PREFIX, dir="/tmp"))
    managed._bootstrap(root)
    try:
        yield root
    finally:
        for pid in managed._service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(root, ignore_errors=True)


def test_the_continuity_vertical_runs_through_the_installed_cli(home: Path) -> None:
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    # Register: the service issues the binding; the client only asks.
    registered = _mutation(
        home,
        "continuity",
        "register",
        "--input-json",
        json.dumps(_register_input()),
        key="cli-register-1",
    )
    session = registered["session"]
    assert session["state"] == "active"
    session_id = session["session_id"]

    # Append: one durable receipt, fenced on the expected predecessor.
    appended = _mutation(
        home,
        "continuity",
        "checkpoint",
        "--input-json",
        json.dumps(_append_input(session_id)),
        "--record-version",
        "seq-0",
        key="cli-append-1",
    )
    receipt = appended["receipt"]
    assert receipt["sequence"] == 1
    assert receipt["content_digest"].startswith("sha256:")

    # Close: the final checkpoint commits with the close, atomically.
    closed = _mutation(
        home,
        "continuity",
        "close",
        "--input-json",
        json.dumps(_close_input(session_id)),
        "--record-version",
        "seq-1",
        key="cli-close-1",
    )
    assert closed["checkpoint_recorded"] is True
    assert closed["state"] == "closed"
    assert closed["receipt"]["sequence"] == 2
    checkpoint_id = closed["receipt"]["checkpoint_id"]

    # Handoff: a fresh reader sees the acknowledged work, not a fabricated
    # completion.
    handoff = _cli(
        home,
        "continuity",
        "handoff",
        "--input-json",
        json.dumps({"checkpoint_id": checkpoint_id}),
        "--json",
    )
    view = handoff["handoff"]
    assert view["format_version"] == "continuity_handoff.v1"
    assert view["redacted"] is False
    assert "Root cause still unconfirmed" in view["unresolved_work"]

    # Search: the working-context view answers over the real projection.
    found = _cli(
        home,
        "engineering",
        "search",
        "--input-json",
        json.dumps({"query": "session-restoration", "view": "working_context"}),
        "--json",
    )
    assert found["coverage"]["projection"] == "current"
    statements = json.dumps(found["previews"])
    assert "session-restoration failure" in statements

    # Build: the resume pack carries the working context in its own partition,
    # and never labels it accepted knowledge.
    built = _cli(
        home,
        "engineering",
        "context",
        "--input-json",
        json.dumps(
            {
                "query": "session-restoration",
                "targets": [{"snapshot_id": "cli-snap-1", "snapshot_kind": "working_tree"}],
                "profile": "resume",
            }
        ),
        "--json",
    )
    pack = built["pack"]
    assert pack["format_version"] == "engineering_context.v1"
    assert pack["fresh_authorization_required"] is True
    partitions = {section["partition"] for section in pack["sections"]}
    assert "working_context" in partitions
    assert "accepted_knowledge" not in partitions


def test_a_stale_predecessor_is_the_typed_precondition_refusal(home: Path) -> None:
    """The CLI half of AC-023: a competing successor cannot silently replace."""
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    registered = _mutation(
        home,
        "continuity",
        "register",
        "--input-json",
        json.dumps(_register_input()),
        key="cli-register-stale",
    )
    session_id = registered["session"]["session_id"]
    _mutation(
        home,
        "continuity",
        "checkpoint",
        "--input-json",
        json.dumps(_append_input(session_id)),
        "--record-version",
        "seq-0",
        key="cli-append-stale",
    )

    # The session head is sequence 1; a caller still expecting an empty
    # session is refused rather than silently replacing the newer checkpoint.
    refused = _refusal(
        home,
        "continuity",
        "close",
        "--input-json",
        json.dumps(_close_input(session_id, expected_sequence=0)),
        "--record-version",
        "seq-0",
        "--idempotency-key",
        "cli-close-stale",
        "--json",
    )
    assert refused["code"] == "mutation_precondition_failed"
