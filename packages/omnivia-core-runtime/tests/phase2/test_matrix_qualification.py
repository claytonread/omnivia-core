"""Phase 2 adversarial matrix qualification (B8).

The T-0629 packet requires the accepted matrix to be "converted into named tests
rather than treated as advisory prose". This module is the check on that
conversion: it reads the Phase 2 test suite and asserts every required case id from
every group is present as a named test.

It deliberately fails when a case is missing rather than reporting a percentage. A
matrix that is 90% converted is a matrix with unqualified rows in it, and the exit
gate is "every applicable row passes".
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

PHASE2 = Path(__file__).parent

#: Required case counts per group, from the T-0629 packet.
REQUIRED_GROUPS: dict[str, int] = {
    "WM": 12,
    "BD": 12,
    "LE": 22,
    "FL": 8,
    "FM": 22,
    "MB": 28,
    "LC": 12,
}

TOTAL_REQUIRED = 116

#: Cases covered by a differently-prefixed test, with the reason. Recorded
#: explicitly so a gap cannot hide behind a naming convention.
COVERED_ELSEWHERE: dict[str, str] = {
    "BD-07": "test_simultaneous_acquisition_has_exactly_one_winner (real two-process race)",
    "BD-08": "test_lc12_clients_and_launchers_expose_no_lease_ownership_api",
    "LE-05": "test_simultaneous_acquisition_has_exactly_one_winner and test_le05_*",
    "LE-08": "test_le07_takeover_requires_storage_lock_availability (endpoint evidence)",
    "LE-17": "test_le15_graceful_handover_keeps_generations_monotonic (shutdown_state)",
    "LE-19": "test_le18_readiness_requires_the_exact_current_lease_tuple",
    "LE-21": "test_le06_le11_expired_heartbeat_alone_does_not_permit_takeover",
    "FM-14": "test_fl02_windows_two_process_exclusion (Windows CI)",
    "MB-11": "test_staging_copy_is_not_the_verified_backup",
    "MB-22": "test_mb21_mb22_rollback_restores_the_exact_schema_counts_and_values",
}


def phase2_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PHASE2.glob("test_*.py"))
        if path.name != Path(__file__).name
    }


def mentioned_case_ids() -> set[str]:
    """Every matrix case id referenced by a test name or a comment."""
    found: set[str] = set()
    # `\b` would not match inside a snake_case identifier: in `test_mb13_mb14`
    # there is no word boundary before `mb`, because `_` is itself a word
    # character. Explicit non-alphanumeric lookaround is required.
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(WM|BD|LE|FL|FM|MB|LC)[-_ ]?(\d{2})(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for text in phase2_sources().values():
        for group, number in pattern.findall(text):
            found.add(f"{group.upper()}-{number}")
    return found


def test_every_required_case_id_is_present() -> None:
    """All 116 rows are named somewhere in the Phase 2 suite."""
    present = mentioned_case_ids() | set(COVERED_ELSEWHERE)
    missing: list[str] = []
    for group, count in REQUIRED_GROUPS.items():
        for index in range(1, count + 1):
            case = f"{group}-{index:02d}"
            if case not in present:
                missing.append(case)
    assert not missing, f"matrix rows with no named test: {missing}"


def test_the_group_counts_match_the_packet() -> None:
    assert sum(REQUIRED_GROUPS.values()) == TOTAL_REQUIRED
    assert REQUIRED_GROUPS == {
        "WM": 12,
        "BD": 12,
        "LE": 22,
        "FL": 8,
        "FM": 22,
        "MB": 28,
        "LC": 12,
    }


def test_every_group_has_a_dedicated_test_module() -> None:
    """Each group's cases live in a module named for that group's concern."""
    modules = set(phase2_sources())
    for expected in (
        "test_workspace_manifest.py",
        "test_bootstrap_storage.py",
        "test_lease_owner_evidence.py",
        "test_filesystem_locking.py",
        "test_fencing_mutation.py",
        "test_migration_backup_restore.py",
        "test_lifecycle_cleanup.py",
    ):
        assert expected in modules, expected


def test_cases_covered_elsewhere_name_a_real_test() -> None:
    """A redirect must point at a test that exists.

    Without this, `COVERED_ELSEWHERE` would be a place to silence a missing case by
    asserting it is covered somewhere unnamed.
    """
    sources = "\n".join(phase2_sources().values())
    for case, note in COVERED_ELSEWHERE.items():
        target = note.split()[0].rstrip("*")
        assert target.startswith("test_"), f"{case}: redirect must name a test"
        stem = target.rstrip("*_")
        assert stem in sources, f"{case}: redirect names {target!r}, which does not exist"


def test_multi_process_cases_use_real_processes() -> None:
    """The cases that claim OS-level exclusion must spawn real children.

    Threads share the SQLite cache and the GIL, so a threaded version of these would
    pass while proving nothing about a second process.
    """
    locking = (PHASE2 / "test_filesystem_locking.py").read_text(encoding="utf-8")
    fencing = (PHASE2 / "test_fencing_mutation.py").read_text(encoding="utf-8")

    assert "spawn(" in locking and "run_child(" in locking
    assert "run_child(" in fencing
    assert "threading" not in locking
    assert "threading" not in fencing

    # And the stock-SQLite child must not import the runtime.
    child = (PHASE2 / "harness" / "children" / "stock_sqlite_writer.py").read_text(
        encoding="utf-8"
    )
    assert "omnivia_core_runtime" not in child


def test_no_case_claims_to_prevent_same_principal_tampering() -> None:
    """ADR-037 disclaims that guarantee, so no test may assert it.

    Triggers are fail-closed defence against ordinary unregistered DML, not a
    boundary against code running as the workspace-owning OS principal. A test
    named or documented as preventing that would be asserting something the
    architecture explicitly does not promise.
    """
    forbidden = re.compile(
        r"prevents?_(same_principal|hostile)|blocks?_the_owning_principal", re.IGNORECASE
    )
    for name, text in phase2_sources().items():
        assert not forbidden.search(text), f"{name} claims a disclaimed guarantee"


def test_skips_are_declared_rather_than_silent() -> None:
    """Every skip states a reason, so an unrun case is visibly unrun.

    The plan's own risk table warns against describing work as qualified when its
    gate did not execute.
    """
    for name, text in phase2_sources().items():
        for match in re.finditer(r"pytest\.mark\.skipif\((.*?)\)\n", text, re.DOTALL):
            assert "reason=" in match.group(1), f"{name} has a skipif without a reason"
        for match in re.finditer(r"pytest\.skip\((.*?)\)", text, re.DOTALL):
            assert match.group(1).strip(), f"{name} has a bare pytest.skip"


def report_group_coverage() -> Counter[str]:
    counts: Counter[str] = Counter()
    for case in mentioned_case_ids():
        counts[case.split("-")[0]] += 1
    return counts


def test_coverage_report_is_printable() -> None:
    """Emit the per-group tally so a run reports what it qualified."""
    counts = report_group_coverage()
    lines = [
        f"{group}: {counts.get(group, 0)} referenced / {required} required"
        for group, required in sorted(REQUIRED_GROUPS.items())
    ]
    assert lines
    print("\nPhase 2 matrix coverage:\n  " + "\n  ".join(lines))


@pytest.mark.parametrize("group", sorted(REQUIRED_GROUPS))
def test_each_group_references_at_least_its_required_count(group: str) -> None:
    referenced = {c for c in mentioned_case_ids() | set(COVERED_ELSEWHERE) if c.startswith(f"{group}-")}
    assert len(referenced) >= REQUIRED_GROUPS[group], (
        f"{group}: {len(referenced)} referenced, {REQUIRED_GROUPS[group]} required; "
        f"present={sorted(referenced)}"
    )
