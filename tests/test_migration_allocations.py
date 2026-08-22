"""Mutation tests for the T-0660 migration allocation authority and its guard.

Two halves.

The first is the pinned allocation itself. `scripts/check-migration-allocations.py`
deliberately does not restate which lane owns which number -- doing so would make
the authority file decorative -- so the exact Option B table lives here instead,
as the independently reviewed second copy. Reallocating a reserved number in the
authority keeps that file internally consistent and fails this module, the same
way `REQUIRED_RUFF_TARGETS` in `test_core_acceptance_workflow.py` fails when the
workflow's lint scope moves.

The second is the guard. Every failure class it claims to close is exercised by
mutating a copy of the real authority and the real migration directory listing,
because a checker whose rejection paths are never executed is a checker that
reports "passed" for reasons nobody has verified. The mutations go through the
pure `check()` seam rather than the filesystem, so nothing here writes to the
tree the guard is protecting.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "check-migration-allocations.py"
AUTHORITY = REPO_ROOT / "contracts" / "migrations" / "v1" / "allocations.json"

# The exact T-0660 Option B allocation: number, filename, semantic owner, state.
# 0018-0020 are the Agent Runtime lane's present candidates; 0021-0024 are
# reserved for lanes whose SQL does not exist yet and must not appear early.
EXPECTED_ALLOCATION = (
    (18, "0018_agent_runtime_records.sql", "Agent Runtime", "candidate"),
    (19, "0019_artifact_evidence_cleanup_records.sql", "Agent Runtime", "candidate"),
    (20, "0020_runtime_run_summary_projection.sql", "Agent Runtime", "candidate"),
    (21, "0021_context_models.sql", "Context Models", "reserved"),
    (22, "0022_workflow_runs.sql", "Workflow Runtime", "reserved"),
    (23, "0023_provider_invocations.sql", "Provider Service", "reserved"),
    (24, "0024_chat_foundation.sql", "Chat", "reserved"),
)

ACCEPTED_PREDECESSOR = (17, "0017_connector_sync_state.sql")
FROZEN_SOURCE_HEAD = "9b4fb2fa78328823e9579e04cc725135265f7029"
DECISION = "T-0660 / Option B / Clayton Read"

GUARD_COMMAND = "python scripts/check-migration-allocations.py"
PREFLIGHT = REPO_ROOT / "scripts" / "preflight"
ACCEPTANCE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "core-acceptance.yml"


def _load_guard() -> Any:
    """Import the hyphen-named script as a module."""
    spec = importlib.util.spec_from_file_location("check_migration_allocations", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _document() -> dict[str, Any]:
    return json.loads(AUTHORITY.read_text(encoding="utf-8"))


def _present() -> dict[str, str]:
    return guard.migration_files()


def _findings(document: dict[str, Any], present: dict[str, str]) -> list[str]:
    return guard.check(json.dumps(document), present)


# ---------------------------------------------------------------------------
# The allocation itself.
# ---------------------------------------------------------------------------


def test_the_authority_records_the_option_b_decision_and_frozen_head() -> None:
    document = _document()
    assert document["schema_version"] == 1
    assert document["decision"] == DECISION
    assert document["frozen_source_head"] == FROZEN_SOURCE_HEAD
    assert (
        document["accepted_predecessor"]["number"],
        document["accepted_predecessor"]["filename"],
    ) == ACCEPTED_PREDECESSOR
    # The rule reserved numbers are held to, stated in the authority rather than
    # only in the guard that enforces it.
    assert "reserved" in document["reserved_rule"]
    assert "absent" in document["reserved_rule"]


def test_the_exact_option_b_allocation_is_pinned() -> None:
    actual = tuple(
        (entry["number"], entry["filename"], entry["owner"], entry["state"])
        for entry in _document()["allocations"]
    )
    assert actual == EXPECTED_ALLOCATION


def test_every_allocation_belongs_to_this_repository() -> None:
    assert {entry["repository"] for entry in _document()["allocations"]} == {"omnivia-core"}


def test_nothing_is_accepted_yet() -> None:
    """T-0660 freezes numbers. It accepts no branch, so no entry may claim one."""
    assert all(entry["accepted_commit"] is None for entry in _document()["allocations"])


def test_reserved_sql_is_absent_from_the_tree() -> None:
    """The point of a reservation: the number is taken, the file is not there."""
    directory = guard.MIGRATION_DIR
    for number, filename, _owner, state in EXPECTED_ALLOCATION:
        if state == "reserved":
            assert not (directory / filename).exists(), f"{number} is reserved, not written"


def test_the_repository_authority_passes_the_guard() -> None:
    assert guard.check(AUTHORITY.read_text(encoding="utf-8"), _present()) == []


def test_the_guard_runs_clean_as_a_process() -> None:
    """The `main()` path -- canonical paths, exit code and all -- not just `check()`."""
    completed = subprocess.run(
        [sys.executable, str(GUARD)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "passed" in completed.stdout


def test_the_guard_takes_no_authority_path_from_the_command_line() -> None:
    """An oracle supplied by the caller certifies whatever it is handed.

    The script reads the repository's canonical authority and nothing else, so it
    parses no arguments at all.
    """
    source = GUARD.read_text(encoding="utf-8")
    assert "argparse" not in source
    assert "sys.argv" not in source


# ---------------------------------------------------------------------------
# The guard's failure classes.
# ---------------------------------------------------------------------------

Mutation = Callable[[dict[str, Any], dict[str, str]], None]


def _drop_entry(document: dict[str, Any], number: int) -> None:
    document["allocations"] = [
        entry for entry in document["allocations"] if entry["number"] != number
    ]


def _entry(document: dict[str, Any], number: int) -> dict[str, Any]:
    return next(entry for entry in document["allocations"] if entry["number"] == number)


MUTATIONS: tuple[tuple[str, Mutation, str], ...] = (
    (
        "extra top-level field",
        lambda document, present: document.update({"note": "handy"}),
        "field drift",
    ),
    (
        "missing top-level field",
        lambda document, present: document.pop("reserved_rule"),
        "field drift",
    ),
    (
        "extra allocation field",
        lambda document, present: _entry(document, 21).update({"eta": "soon"}),
        "field drift",
    ),
    (
        "missing allocation field",
        lambda document, present: _entry(document, 21).pop("owner"),
        "field drift",
    ),
    (
        "wrong schema version",
        lambda document, present: document.update({"schema_version": 2}),
        "schema_version",
    ),
    (
        "frozen source head is not a commit id",
        lambda document, present: document.update({"frozen_source_head": "HEAD"}),
        "frozen_source_head",
    ),
    (
        "empty decision identity",
        lambda document, present: document.update({"decision": "  "}),
        "decision must be a non-empty string",
    ),
    (
        "duplicate number",
        lambda document, present: _entry(document, 22).update({"number": 21}),
        "expected migration 22, got 21",
    ),
    (
        "duplicate filename",
        lambda document, present: _entry(document, 22).update(
            {"filename": "0021_context_models.sql"}
        ),
        "is already allocated to migration 21",
    ),
    (
        "gap in the sequence",
        lambda document, present: _drop_entry(document, 21),
        "expected migration 21, got 22",
    ),
    (
        "sequence does not start at the accepted predecessor",
        lambda document, present: _drop_entry(document, 18),
        "expected migration 18, got 19",
    ),
    (
        "broken predecessor link",
        lambda document, present: _entry(document, 20).update({"predecessor": 17}),
        "must follow 19",
    ),
    (
        "filename does not name its number",
        lambda document, present: _entry(document, 21).update(
            {"filename": "0031_context_models.sql"}
        ),
        "does not name migration 0021",
    ),
    (
        "filename is not an ordered migration name",
        lambda document, present: _entry(document, 21).update({"filename": "context_models.sql"}),
        "not of the form NNNN_name.sql",
    ),
    (
        "semantic owner erased",
        lambda document, present: _entry(document, 24).update({"owner": ""}),
        "owner must be a non-empty string",
    ),
    (
        "repository erased",
        lambda document, present: _entry(document, 24).update({"repository": None}),
        "repository must be a non-empty string",
    ),
    (
        "unknown state",
        lambda document, present: _entry(document, 21).update({"state": "accepted"}),
        "state must be",
    ),
    (
        "candidate pins no hash",
        lambda document, present: _entry(document, 18).update({"sha256": None}),
        "must pin a 64-character sha256",
    ),
    (
        "candidate pins no introducing commit",
        lambda document, present: _entry(document, 18).update({"introduced_commit": None}),
        "must pin a 40-character introducing commit",
    ),
    (
        "reserved entry pins a hash",
        lambda document, present: _entry(document, 21).update({"sha256": "0" * 64}),
        "must pin neither sha256 nor introduced_commit",
    ),
    (
        "accepted commit claimed before acceptance",
        lambda document, present: _entry(document, 18).update({"accepted_commit": "a" * 40}),
        "accepted_commit must be null",
    ),
    (
        "candidate sql is missing",
        lambda document, present: present.pop("0018_agent_runtime_records.sql"),
        "candidate file is missing",
    ),
    (
        "candidate sql content drifted",
        lambda document, present: present.update(
            {"0018_agent_runtime_records.sql": "b" * 64}
        ),
        "content drifted",
    ),
    (
        "reserved sql appears early",
        lambda document, present: present.update({"0021_context_models.sql": "c" * 64}),
        "already exists; advance this allocation",
    ),
    (
        "unallocated migration beyond the predecessor",
        lambda document, present: present.update({"0025_rogue_lane.sql": "d" * 64}),
        "allocated to nobody",
    ),
    (
        "colliding file under an unallocated name",
        lambda document, present: present.update({"0021_chat_foundation.sql": "e" * 64}),
        "allocated to nobody",
    ),
    (
        "accepted predecessor is not in the tree",
        lambda document, present: present.pop("0017_connector_sync_state.sql"),
        "anchored to a migration that does not exist",
    ),
    (
        "accepted predecessor filename is not a name",
        lambda document, present: document["accepted_predecessor"].update({"filename": ["x"]}),
        "filename must be a non-empty string",
    ),
    (
        "accepted predecessor number is not an integer",
        lambda document, present: document["accepted_predecessor"].update({"number": "17"}),
        "number must be an integer",
    ),
    (
        "migration file with no ordered prefix",
        lambda document, present: present.update({"rogue_lane.sql": "f" * 64}),
        "no ordered numeric prefix",
    ),
)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=name) for name, mutate, expected in MUTATIONS],
)
def test_the_guard_rejects(mutate: Mutation, expected: str) -> None:
    document = _document()
    present = _present()
    mutate(document, present)
    findings = _findings(document, present)
    assert any(expected in finding for finding in findings), (
        f"expected a finding containing {expected!r}, got: {findings}"
    )


def test_malformed_json_is_rejected_rather_than_parsed_leniently() -> None:
    findings = guard.check('{"schema_version": 1,}', _present())
    assert findings and "not valid JSON" in findings[0]


def test_an_authority_that_is_not_an_object_is_rejected() -> None:
    findings = guard.check("[]", _present())
    assert findings and "expected an object" in findings[0]


def test_the_unmutated_baseline_is_clean() -> None:
    """The control for every rejection above: the mutations are what fail, not the
    round-trip through `json.dumps` or the directory listing."""
    assert _findings(_document(), _present()) == []


# ---------------------------------------------------------------------------
# Registration: local and merge-blocking runs invoke the same command.
# ---------------------------------------------------------------------------


def _active(path: Path) -> str:
    """A file's active lines, with blanks and whole-line comments dropped.

    A commented-out gate is not a gate, and both files document their steps in
    comments directly above them -- so a plain substring search over the raw text
    passes on the explanation after the command itself has been deleted.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_the_guard_is_registered_in_preflight_and_on_the_acceptance_gate() -> None:
    """One command, both places. A guard that only runs locally is advisory, and a
    guard that only runs on the gate is first discovered on the pull request."""
    assert GUARD_COMMAND in _active(PREFLIGHT)
    assert GUARD_COMMAND in _active(ACCEPTANCE_WORKFLOW)


def test_the_acceptance_gate_step_is_named_and_fail_closed() -> None:
    workflow = _active(ACCEPTANCE_WORKFLOW)
    assert "- name: Check migration allocations" in workflow
    # The stable required check and its matrix-free single job are unchanged.
    assert "name: Core acceptance" in workflow
