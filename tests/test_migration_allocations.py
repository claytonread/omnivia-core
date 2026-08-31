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

# The exact T-0660 successor allocation: number, filename, semantic owner, state.
# 0018-0020 are the Agent Runtime lane, accepted at the PR #88 default-branch
# landing. 0021-0022 are the already-replayed FND-F3 diff, candidates pinned to
# their exact introducing commits but not yet accepted. 0023 is the next
# 0024 and 0025 are the next materialized Agent Runtime candidates. 0026 is the
# materialized Context Models candidate. 0027 is the materialized Workflow Runtime
# candidate. 0028 is the materialized Provider Service candidate. 0029 is the
# materialized Chat foundation candidate. 0030 is the materialized Chat Gate B
# successor-state candidate. 0031 is the Chat request-manifest candidate. 0032
# is the durable Chat turn/step/tool lifecycle candidate. 0033 is the durable
# Chat compaction/waits/agent-run candidate. 0034 is the durable Chat
# generation-text/transport-event candidate. 0035 is reserved for the T-0688
# Workflow Runtime hardening migration -- an allocation, not a file.
EXPECTED_ALLOCATION = (
    (18, "0018_agent_runtime_records.sql", "Agent Runtime", "accepted"),
    (19, "0019_artifact_evidence_cleanup_records.sql", "Agent Runtime", "accepted"),
    (20, "0020_runtime_run_summary_projection.sql", "Agent Runtime", "accepted"),
    (21, "0021_runtime_policy_budget_snapshots.sql", "Agent Runtime", "candidate"),
    (22, "0022_runtime_approvals_capability_grants.sql", "Agent Runtime", "candidate"),
    (23, "0023_runtime_effect_transactions.sql", "Agent Runtime", "candidate"),
    (24, "0024_runtime_effect_reconciliations.sql", "Agent Runtime", "candidate"),
    (25, "0025_runtime_stop_and_admission_control.sql", "Agent Runtime", "candidate"),
    (26, "0026_context_models.sql", "Context Models", "candidate"),
    (27, "0027_workflow_runs.sql", "Workflow Runtime", "candidate"),
    (28, "0028_provider_invocations.sql", "Provider Service", "candidate"),
    (29, "0029_chat_foundation.sql", "Chat", "candidate"),
    (30, "0030_chat_gate_b_successor_state.sql", "Chat", "candidate"),
    (31, "0031_chat_request_manifests.sql", "Chat", "candidate"),
    (32, "0032_chat_turn_step_tool_lifecycle.sql", "Chat", "candidate"),
    (33, "0033_chat_compaction_waits_agent_runs.sql", "Chat", "candidate"),
    (34, "0034_chat_generation_text_transport_events.sql", "Chat", "candidate"),
    (35, "0035_t0688_workflow_runtime_hardening.sql", "Workflow Runtime", "reserved"),
)

ACCEPTED_PREDECESSOR = (17, "0017_connector_sync_state.sql")
# PR #88's default-branch merge commit: the frozen landing this authority
# accepts 0018-0020 against.
FROZEN_SOURCE_HEAD = "23c6a82dc8128ceec202fc6202b65abf4e2b2aa3"
ACCEPTED_COMMIT = FROZEN_SOURCE_HEAD
DECISION = "T-0660 / Option B successor / Runtime Execution Planes FND-F3 / Clayton Read"

# The candidates' exact introducing commits, each
# pinned as the commit that first introduced each migration file in the checked head -- not yet accepted, so
# neither carries an accepted_commit.
CANDIDATE_INTRODUCED_COMMITS = {
    21: "0b0d8ba56466debfaa440dcb39ad4f5ebd6077b2",
    22: "0b0d8ba56466debfaa440dcb39ad4f5ebd6077b2",
    23: "44e3ed256c38dadc54203994e209380d6e6f439f",
    24: "e9827ae9f83188f9e9c4fc848597edf04aa67416",
    25: "af25779a66e41a32dc268940caf0abc2d699ffc9",
    26: "40348d38bde2dc3ad098b68cf9637eb8a8445535",
    27: "348bb389f4b5a7b27769ba5224afb43031a6127f",
    28: "0178c4a4aad8e92eeccc22500ab2a9432d099e27",
    29: "192e88b28a89c6740b302cc3646d732f45086c70",
    30: "0c72b6651789f7695fc2afb326f4809654a4c43b",
    31: "dbc23280be010318b6e0d1a2e5ae0fc43a1bbf47",
    32: "73aa21696bfe10d56141d4945475d77dfc631f5d",
    33: "0741a368a39815ee01397980b3da5e6b17ffe4a0",
    34: "84fabceec3f832f7e2e40fe1e6794f98786e134e",
}

# The Agent Runtime lane's three introducing commits, each preserved as a
# the commit that first introduced the migration file in ACCEPTED_COMMIT.
INTRODUCED_COMMITS = {
    18: "a2da96a8fee541b9e18475f2753060f7e5bf6ff5",
    19: "b1c5b43a5e5adbe578f9379d134d6d7c6baefa70",
    20: "29ab1c4da90a8ac80f6711474c171fb0883d7381",
}

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


def test_agent_runtime_migrations_are_accepted_at_the_frozen_landing() -> None:
    """T-0660 accepts 0018-0020 at PR #88's default-branch merge; 0021-0034 are
    candidates pinned to their introducing commits but not accepted."""
    document = _document()
    for entry in document["allocations"]:
        if entry["number"] in INTRODUCED_COMMITS:
            assert entry["accepted_commit"] == ACCEPTED_COMMIT
            assert entry["introduced_commit"] == INTRODUCED_COMMITS[entry["number"]]
        else:
            assert entry["accepted_commit"] is None

    for number, commit in CANDIDATE_INTRODUCED_COMMITS.items():
        entry = _entry(document, number)
        assert entry["state"] == "candidate"
        assert entry["introduced_commit"] == commit
        assert entry["accepted_commit"] is None


def test_reserved_sql_is_absent_from_the_tree() -> None:
    """If this authority carries a reservation, the file is not there."""
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


def _as_candidate(document: dict[str, Any], number: int) -> dict[str, Any]:
    entry = _entry(document, number)
    entry.update({"state": "candidate", "accepted_commit": None})
    return entry


def _append_reserved_30(document: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "number": 30,
        "filename": "0030_reserved_followup.sql",
        "owner": "Future Lane",
        "repository": "omnivia-core",
        "state": "reserved",
        "predecessor": 29,
        "sha256": None,
        "introduced_commit": None,
        "accepted_commit": None,
    }
    document["allocations"].append(entry)
    return entry


def _remove_candidate_file(document: dict[str, Any], present: dict[str, str]) -> None:
    _as_candidate(document, 18)
    present.pop("0018_agent_runtime_records.sql")


def _drift_candidate_file(document: dict[str, Any], present: dict[str, str]) -> None:
    _as_candidate(document, 18)
    present["0018_agent_runtime_records.sql"] = "b" * 64


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
            {"filename": "0021_runtime_policy_budget_snapshots.sql"}
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
        lambda document, present: _entry(document, 21).update({"state": "effective"}),
        "state must be",
    ),
    (
        "candidate pins no hash",
        lambda document, present: _as_candidate(document, 18).update({"sha256": None}),
        "must pin a 64-character sha256",
    ),
    (
        "candidate pins no introducing commit",
        lambda document, present: _as_candidate(document, 18).update(
            {"introduced_commit": None}
        ),
        "must pin a 40-character introducing commit",
    ),
    (
        "accepted allocation pins no hash",
        lambda document, present: _entry(document, 18).update({"sha256": None}),
        "accepted allocation must pin a 64-character sha256",
    ),
    (
        "accepted allocation pins no introducing commit",
        lambda document, present: _entry(document, 18).update({"introduced_commit": None}),
        "accepted allocation must pin a 40-character introducing commit",
    ),
    (
        "accepted allocation pins no accepted commit",
        lambda document, present: _entry(document, 18).update({"accepted_commit": None}),
        "accepted allocation must pin a 40-character accepted commit",
    ),
    (
        "accepted allocation pins malformed accepted commit",
        lambda document, present: _entry(document, 18).update({"accepted_commit": "HEAD"}),
        "accepted allocation must pin a 40-character accepted commit",
    ),
    (
        "reserved entry pins a hash",
        lambda document, present: _append_reserved_30(document).update(
            {"sha256": "0" * 64}
        ),
        "must pin neither sha256 nor introduced_commit",
    ),
    (
        "candidate carries an accepted commit",
        lambda document, present: _entry(document, 18).update({"state": "candidate"}),
        "accepted_commit must be null",
    ),
    (
        "reserved allocation carries an accepted commit",
        lambda document, present: _append_reserved_30(document).update(
            {"accepted_commit": "a" * 40}
        ),
        "accepted_commit must be null",
    ),
    (
        "candidate sql is missing",
        _remove_candidate_file,
        "candidate file is missing",
    ),
    (
        "candidate sql content drifted",
        _drift_candidate_file,
        "content drifted",
    ),
    (
        "accepted sql is missing",
        lambda document, present: present.pop("0018_agent_runtime_records.sql"),
        "accepted file is missing",
    ),
    (
        "accepted sql content drifted",
        lambda document, present: present.update(
            {"0018_agent_runtime_records.sql": "b" * 64}
        ),
        "content drifted",
    ),
    (
        "reserved sql appears early",
        lambda document, present: (
            _append_reserved_30(document),
            present.update({"0030_reserved_followup.sql": "c" * 64}),
        ),
        "already exists; advance this allocation",
    ),
    (
        "unallocated migration beyond the predecessor",
        lambda document, present: present.update({"0030_rogue_lane.sql": "d" * 64}),
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
        if state not in ("candidate", "accepted"):
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
    is a commit this checkout descends from, each accepted allocation's introducing
    commit really carries the pinned content, and its accepted commit preserves it."""
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


def test_an_accepted_commit_that_does_not_exist_is_rejected() -> None:
    document = _document()
    _entry(document, 18)["accepted_commit"] = "2" * 40
    findings = guard.check_history(json.dumps(document))
    assert findings == [
        f"allocations[0]: accepted_commit {'2' * 40} is not a commit in this repository"
    ]


def test_the_introducing_commit_must_ancestor_the_accepted_commit() -> None:
    document = _document()
    accepted_before_introduction = _rev_parse(f"{INTRODUCED_COMMITS[18]}^")
    _entry(document, 18)["accepted_commit"] = accepted_before_introduction
    findings = guard.check_history(json.dumps(document))
    assert findings == [
        (
            f"allocations[0]: introduced_commit {INTRODUCED_COMMITS[18]} is not an "
            f"ancestor of accepted_commit {accepted_before_introduction}"
        )
    ]


def test_the_checked_head_must_descend_from_the_accepted_commit() -> None:
    document = _document()
    head_before_acceptance = _rev_parse(f"{ACCEPTED_COMMIT}^1")
    document["frozen_source_head"] = head_before_acceptance
    findings = guard.check_history(json.dumps(document), head=head_before_acceptance)
    assert len(findings) == 3, findings
    assert all("does not descend from the accepted lineage" in finding for finding in findings)


def test_the_accepted_commit_must_contain_the_migration_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_git = guard._git
    path = f"{guard.MIGRATION_PATH}/0018_agent_runtime_records.sql"

    def missing_accepted_blob(arguments: list[str], cwd: Path) -> tuple[int, bytes]:
        if arguments == ["cat-file", "blob", f"{ACCEPTED_COMMIT}:{path}"]:
            return 128, b""
        return original_git(arguments, cwd)

    monkeypatch.setattr(guard, "_git", missing_accepted_blob)
    findings = guard.check_history(AUTHORITY.read_text(encoding="utf-8"))
    assert findings == [
        f"allocations[0]: accepted_commit {ACCEPTED_COMMIT} does not contain {path}"
    ]


def test_content_at_the_accepted_commit_must_match_the_pinned_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_git = guard._git
    path = f"{guard.MIGRATION_PATH}/0018_agent_runtime_records.sql"

    def mismatched_accepted_blob(arguments: list[str], cwd: Path) -> tuple[int, bytes]:
        if arguments == ["cat-file", "blob", f"{ACCEPTED_COMMIT}:{path}"]:
            return 0, b"-- mismatched accepted content\n"
        return original_git(arguments, cwd)

    monkeypatch.setattr(guard, "_git", mismatched_accepted_blob)
    findings = guard.check_history(AUTHORITY.read_text(encoding="utf-8"))
    assert len(findings) == 1, findings
    assert f"{path} at accepted_commit {ACCEPTED_COMMIT} hashes to" in findings[0]


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
