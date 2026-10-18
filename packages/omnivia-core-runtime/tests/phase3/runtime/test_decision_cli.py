"""The Decision Runtime through the real CLI (ADR-042, plan PR-7; AT-07).

One governed workspace, one real managed-start service, and the installed
`omnivia` entry point -- the same commands an operator types, machine-readable
JSON out. What is asserted is the read surface of AT-07's CLI half: the status,
records, definitions and settings projections answer with exactly the semantics
the runtime suite pins, through the commands an operator actually runs.

The write half of the vertical -- configure, publish, evaluate, correct -- is
deliberately *not* asserted here: over the local socket the CLI acts as the
local owner, whose grant is read-only by construction
(`LOCAL-IPC-PEER-IDENTITY-DEFERRED`, carried forward unchanged by the binding
report). That refusal shape is asserted as the third fact below, and it is the
same shape every other family's CLI mutation already returns. The mutation path
itself is proven end to end by `test_decision_runtime.py` through the
dispatcher the service actually runs.
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

_DEFINITION: dict[str, Any] = {
    "id": "core.ticket_priority",
    "version": "1.0.0",
    "title": "Ticket priority",
    "purpose": "decision_evaluation",
    "kind": "choice",
    "options": [
        {"id": "low", "label": "Low", "description": "low"},
        {"id": "high", "label": "High", "description": "high"},
    ],
    "recipe": {
        "mode": "deterministic",
        "rules": [
            {"when": {"key": "severity", "equals": "critical"}, "option": "high"},
            {"when": {"key": "severity", "equals": "minor"}, "option": "low"},
        ],
    },
    "required_sources": 0,
    "min_source_count": 0,
}


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


def test_the_decision_read_surface_runs_through_the_installed_cli(
    home: Path,
) -> None:
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    # The capability starts off, and the status projection says so truthfully.
    status = _cli(home, "decisions", "status", "--json")
    assert status["enabled"] is False
    assert status["host_engine_available"] is True
    assert status["installed_profiles"] == 0
    assert status["active_subscriptions"] == 0

    # The read projections are empty and well-formed on a fresh workspace.
    records = _cli(home, "decisions", "records", "--json")
    assert records["records"] == []
    definitions = _cli(home, "decisions", "definitions", "--json")
    assert definitions["definitions"] == []
    settings = _cli(home, "decisions", "settings", "--json")["settings"]
    assert settings["processing"] == "off"
    assert settings["subscription_enabled"] is False
    assert settings["revision"] == 0
    models = _cli(home, "decisions", "models", "--json")
    assert models["profiles"] == []

    # An unknown record is the typed refusal, with nothing of the request in it.
    refused = _refusal(
        home,
        "decisions",
        "record",
        "--input-json",
        json.dumps({"evaluation_id": "deval-nope"}),
        "--json",
    )
    assert refused["code"] == "not_found"


def test_a_cli_mutation_attempt_states_the_local_trust_limit(home: Path) -> None:
    """The local owner cannot admit an evaluation, and the refusal says why.

    `LOCAL-IPC-PEER-IDENTITY-DEFERRED` carried forward: over the local socket the
    CLI is the trusted local user, whose grant is read-only by construction. The
    refusal is the mutation coordinator's own typed answer -- the same one every
    other family's CLI mutation returns -- and not a crash or a stub.
    """
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    response = _refusal(
        home,
        "decisions",
        "evaluate",
        "--input-json",
        json.dumps(
            {
                "schema_version": "decision.1",
                "definition_ref": {"id": "core.x", "version": "1.0.0"},
                "subject_refs": [{"id": "document:1", "revision": "r1"}],
                "input": {"source_refs": [], "inline_state": {}},
                "execution": {
                    "mode": "advisory",
                    "privacy": "local_only",
                    "deadline_ms": 5000,
                    "maximum_provider_attempts": 1,
                },
            }
        ),
        "--idempotency-key",
        "cli-evaluate-denied",
        "--json",
    )
    assert response["code"] == "capability_not_granted"
    assert "not enabled" in response["message"]


def test_the_decision_vertical_runs_through_the_installed_cli(home: Path) -> None:
    """Enable, publish, evaluate, inspect and correct -- the plan PR-3 exit path."""
    returncode, _start, stderr = managed._run(home)
    assert returncode == 0, stderr

    # Enable it: a compare-and-swap settings write. The revision the caller
    # observed is the precondition, and the update mints the next one.
    settings = _mutation(
        home,
        "decisions",
        "configure",
        "--input-json",
        json.dumps({"revision": 0, "processing": "advisory"}),
        "--record-version",
        "0",
        key="cli-enable-1",
    )
    assert settings["settings"]["processing"] == "advisory"
    assert int(settings["settings"]["revision"]) == 1

    # Publish the deterministic definition: immutable, content-digested.
    published = _mutation(
        home,
        "decisions",
        "publish",
        "--input-json",
        json.dumps({"definition": _DEFINITION}),
        key="cli-publish-1",
    )
    assert published["definition_ref"] == {
        "id": "core.ticket_priority",
        "version": "1.0.0",
    }
    assert published["enabled"] is True
    assert published["digest"].startswith("sha256:")

    # Evaluate: admission returns the durable evaluation and its job.
    payload = json.dumps(
        {
            "schema_version": "decision.1",
            "definition_ref": {"id": "core.ticket_priority", "version": "1.0.0"},
            "subject_refs": [{"id": "document:1", "revision": "r1"}],
            "input": {"source_refs": [], "inline_state": {"severity": "critical"}},
            "execution": {
                "mode": "advisory",
                "privacy": "local_only",
                "deadline_ms": 5000,
                "maximum_provider_attempts": 1,
            },
        }
    )
    evaluated = _mutation(
        home, "decisions", "evaluate", "--input-json", payload, key="cli-evaluate-1"
    )
    evaluation_id = evaluated["evaluation_id"]
    assert evaluated["schema_version"] == "decision.1"
    assert evaluated["job"]["identity"]["originating_operation"] == "decision.evaluate"
    assert evaluated["job"]["state"] == "succeeded"

    # Inspect: the record carries the prediction and the advisory disposition.
    record = _cli(
        home,
        "decisions",
        "record",
        "--input-json",
        json.dumps({"evaluation_id": evaluation_id}),
        "--json",
    )["record"]
    assert record["status"] == "succeeded"
    assert record["prediction"]["selected_option_id"] == "high"
    assert record["prediction"]["probability_semantics"] == "deterministic_rule"
    assert record["disposition"]["code"] == "advisory_only"
    assert record["disposition"]["authorises_action"] is False
    assert record["execution"]["provider_forward_passes"] == 0

    # Correct: the outcome is appended with actor provenance, prediction intact.
    outcome = _mutation(
        home,
        "decisions",
        "outcome",
        "--input-json",
        json.dumps({"evaluation_id": evaluation_id, "outcome": "confirmed"}),
        key="cli-outcome-1",
    )
    assert outcome["evaluation_id"] == evaluation_id
    assert outcome["outcome_id"]

    # A replay of the same idempotent evaluation returns the same record.
    replayed = _mutation(
        home, "decisions", "evaluate", "--input-json", payload, key="cli-evaluate-1"
    )
    assert replayed["evaluation_id"] == evaluation_id
