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

The two effectful seams are covered separately, because they cannot be pure:
`migration_files()` reads bytes off disk (exercised against a temporary
directory, including a CRLF copy and invalid UTF-8), and `check_history()` asks
Git about the authority's commit pins (exercised against this repository's real
history, read-only, with no network and no rewriting).

The gate registration is *not* re-checked here. `GATE_STEPS` in
`test_core_acceptance_workflow.py` is the ordered contract for the merge-blocking
job, and it pins this step's exact name, exact command, place in the order and
fail-closed shape structurally. A second, weaker substring test beside it would
only be able to pass where that one already fails.
"""

from __future__ import annotations

import hashlib
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
        "accepted predecessor filename does not name its own number",
        lambda document, present: document["accepted_predecessor"].update({"number": 20}),
        "does not name migration 0020",
    ),
    (
        "accepted predecessor filename is not an ordered migration name",
        lambda document, present: document["accepted_predecessor"].update(
            {"filename": "connector_sync_state.sql"}
        ),
        "not of the form NNNN_name.sql",
    ),
    (
        "migration file with no ordered prefix",
        lambda document, present: present.update({"rogue_lane.sql": "f" * 64}),
        "no ordered numeric prefix",
    ),
    (
        "migration file whose prefix is not four digits",
        lambda document, present: present.update({"21_context_models.sql": "f" * 64}),
        "no ordered numeric prefix",
    ),
    (
        "migration file whose prefix is not separated from its name",
        lambda document, present: present.update({"0021context-models.sql": "f" * 64}),
        "no ordered numeric prefix",
    ),
    (
        "duplicate historical number at the predecessor",
        lambda document, present: present.update({"0017_connector_sync.sql": "f" * 64}),
        "migration 0017 is claimed by more than one file",
    ),
    (
        "duplicate historical number below the predecessor",
        lambda document, present: present.update({"0009_governed_truth.sql": "f" * 64}),
        "migration 0009 is claimed by more than one file",
    ),
    (
        "duplicate number above the predecessor",
        lambda document, present: present.update({"0018_agent_runtime.sql": "f" * 64}),
        "migration 0018 is claimed by more than one file",
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


def test_raising_the_accepted_predecessor_cannot_orphan_pinned_migrations() -> None:
    """The anchor has to name its own number, not merely exist.

    While the guard only checked that `accepted_predecessor.filename` was present
    in the tree, this exact mutation passed it: move `number` from 17 to 20, drop
    the three allocations that now fall below the base, and the authority is still
    internally consistent -- the sequence starts at base + 1, every predecessor
    link holds, the named file is still in the migration directory. What is gone
    is the enforcement. 0018, 0019 and 0020 are pinned by nothing, and the orphan
    scan (`number > base`) no longer asks who owns them, so their content could
    then drift or be replaced freely.

    One finding, and it is the anchor: everything else about the mutated document
    is consistent, which is precisely why the naming rule is the only thing that
    can catch it.
    """
    document = _document()
    document["accepted_predecessor"]["number"] = 20
    for number in (18, 19, 20):
        _drop_entry(document, number)

    findings = _findings(document, _present())
    assert findings == [
        (
            "accepted_predecessor: filename '0017_connector_sync_state.sql' does not name "
            "migration 0020"
        )
    ]


# ---------------------------------------------------------------------------
# Content digests: the runtime's semantics, not the bytes on disk.
# ---------------------------------------------------------------------------


def test_the_pinned_hashes_are_the_runtime_checksum_of_the_migration_text() -> None:
    """`Migration.checksum` is `sha256(read_text(encoding="utf-8").encode("utf-8"))`.

    The authority pins what the runtime will compare against, so it has to be the
    same function of the same content. Recomputed here from the files rather than
    restated, so a pin that is merely *self-consistent* with a differently-derived
    digest fails.
    """
    document = _document()
    for number, filename, _owner, state in EXPECTED_ALLOCATION:
        if state != "candidate":
            continue
        text = (guard.MIGRATION_DIR / filename).read_text(encoding="utf-8")
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert _entry(document, number)["sha256"] == expected, filename


def test_the_digest_is_newline_normalized_so_a_crlf_checkout_still_matches(
    tmp_path: Path,
) -> None:
    """Same text, three line endings, one digest -- and it is the LF digest.

    Git can hand a Windows checkout CRLF, and the runtime never sees that: it
    reads the file in text mode, which translates CRLF and CR to LF before it
    hashes. A guard that digested raw bytes would call every pinned hash wrong on
    that checkout while the runtime's own checksums stayed right, which is a
    cross-platform false failure rather than a caught drift.
    """
    body = "-- ordered migration\nCREATE TABLE t (id TEXT NOT NULL);\n"
    (tmp_path / "0001_lf.sql").write_bytes(body.encode("utf-8"))
    (tmp_path / "0002_crlf.sql").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    (tmp_path / "0003_cr.sql").write_bytes(body.replace("\n", "\r").encode("utf-8"))

    lf_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert guard.migration_files(tmp_path) == {
        "0001_lf.sql": lf_digest,
        "0002_crlf.sql": lf_digest,
        "0003_cr.sql": lf_digest,
    }


def test_a_migration_that_is_not_valid_utf8_fails_rather_than_hashing_anyway(
    tmp_path: Path,
) -> None:
    """The runtime would fail to read it. The guard says so, by name, instead of
    falling back to a byte digest that would quietly pass a file the runtime
    cannot execute."""
    (tmp_path / "0001_broken.sql").write_bytes(b"CREATE TABLE t (id TEXT);\n\xff\xfe")
    with pytest.raises(guard.GuardError) as error:
        guard.migration_files(tmp_path)
    assert "0001_broken.sql" in str(error.value)
    assert "not valid UTF-8" in str(error.value)


# ---------------------------------------------------------------------------
# Commit pins: real commits, real ancestry, real content.
# ---------------------------------------------------------------------------


def _rev_parse(revision: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", revision],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    return completed.stdout.strip()


def test_the_commit_pins_hold_against_this_repository() -> None:
    """The control for the rejections below, and the fact itself: `frozen_source_head`
    is a commit this checkout descends from, and each candidate's introducing commit
    really carries that pathname with the pinned content."""
    assert guard.check_history(AUTHORITY.read_text(encoding="utf-8")) == []


def test_a_frozen_source_head_that_is_not_a_commit_is_rejected() -> None:
    document = _document()
    document["frozen_source_head"] = "0" * 40
    findings = guard.check_history(json.dumps(document))
    assert findings == [
        f"authority: frozen_source_head {'0' * 40} is not a commit in this repository"
    ]


def test_a_frozen_source_head_this_checkout_does_not_descend_from_is_rejected() -> None:
    """Syntax is not ancestry. Checked against the frozen head's own parent as the
    head revision -- a real commit in this repository -- rather than by rewriting
    any history."""
    parent = _rev_parse(f"{FROZEN_SOURCE_HEAD}~1")
    findings = guard.check_history(AUTHORITY.read_text(encoding="utf-8"), head=parent)
    assert any("is not an ancestor of" in finding for finding in findings), findings


def test_an_introducing_commit_that_does_not_exist_is_rejected() -> None:
    document = _document()
    _entry(document, 18)["introduced_commit"] = "1" * 40
    findings = guard.check_history(json.dumps(document))
    assert findings == [
        f"allocations[0]: introduced_commit {'1' * 40} is not a commit in this repository"
    ]


def test_an_introducing_commit_that_never_carried_the_file_is_rejected() -> None:
    """The pin has to be the commit that introduced *this pathname*, not merely a
    commit that exists somewhere in the history."""
    document = _document()
    _entry(document, 18)["filename"] = "0018_never_written.sql"
    findings = guard.check_history(json.dumps(document))
    assert len(findings) == 1, findings
    assert "does not contain" in findings[0]
    assert findings[0].endswith("migration_files/0018_never_written.sql")


def test_content_at_the_introducing_commit_must_match_the_pinned_digest() -> None:
    document = _document()
    _entry(document, 18)["sha256"] = "0" * 64
    findings = guard.check_history(json.dumps(document))
    assert len(findings) == 1, findings
    assert "the authority pins 000000000000" in findings[0]


def test_history_findings_reach_the_process_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main()` runs the history seam, not only `check()`. Substituted rather than
    provoked, because provoking it for real would mean writing a false pin into the
    repository's own authority file."""
    monkeypatch.setattr(
        guard, "check_history", lambda text: ["authority: frozen_source_head is a fiction"]
    )
    assert guard.main() == 1
    assert "frozen_source_head is a fiction" in capsys.readouterr().err


def test_a_fact_the_guard_cannot_establish_fails_the_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No best-effort path: an unreadable file or an unrunnable `git` is a failure
    with a message, not a check that is quietly skipped."""

    def unavailable(*_arguments: object, **_keywords: object) -> dict[str, str]:
        raise guard.GuardError("git could not be run: [Errno 2] No such file or directory")

    monkeypatch.setattr(guard, "migration_files", unavailable)
    assert guard.main() == 1
    assert "git could not be run" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Registration: the local half.
#
# The merge-blocking half is pinned by `GATE_STEPS` and
# `test_the_migration_allocation_gate_step_is_fail_closed` in
# `test_core_acceptance_workflow.py`, structurally and in order. Only preflight
# is asserted here, so the two files do not hold two versions of one claim.
# ---------------------------------------------------------------------------


def test_the_guard_is_registered_in_preflight() -> None:
    """A guard that only runs on the gate is first discovered on the pull request.

    Active lines only: preflight documents its steps in comments directly above
    them, so a plain substring search over the raw text would pass on the
    explanation after the command itself had been deleted.
    """
    active = "\n".join(
        line
        for line in PREFLIGHT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert GUARD_COMMAND in active
