"""The WP-15 qualification ledger guard, and proof that it fails closed.

Three things are checked here:

1. **The committed ledger holds.** It passes its schema, it carries every gate
   in section 20 exactly once, its statuses obey the fail-closed rules, and
   every repository path and symbol it cites still exists.
2. **The guard rejects each defect it exists to catch.** Every mutation in
   ``MUTATIONS`` corrupts exactly one thing in a deep copy of the real ledger
   and asserts a diagnostic naming it. ``test_the_unmutated_baseline_is_clean``
   is the control that shows the mutations are what fail, not the baseline.
3. **The report agrees with the ledger.** The committed status document equals
   what the generator renders, so a ledger edit cannot leave a stale document
   claiming different counts.

The mutations matter more than the positive case. A checker that passes is
worth nothing unless it can be shown to fail: these cases are what establish
that a missing gate, a duplicate gate, an unknown status, a stale evidence
reference and a passed gate without supporting evidence are all rejected rather
than overlooked.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts/check-wp15-qualification.py"
LEDGER = REPO_ROOT / "qualification/wp15/qualification-ledger.v1.json"
SCHEMA = REPO_ROOT / "qualification/wp15/qualification-ledger.schema.json"
REPORT = REPO_ROOT / "docs/quality/wp15-qualification-status.md"


def _load_guard() -> Any:
    spec = importlib.util.spec_from_file_location("check_wp15_qualification", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _ledger() -> dict[str, Any]:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


# A second, independently written copy of the gate set the design defines. If
# this disagrees with the guard's own EXPECTED_FAMILIES, one of the two was
# edited without the other, which is exactly the drift worth failing on.
EXPECTED_TOTALS = {"AT": 50, "NAT": 17, "DOC": 19, "MON": 38, "END": 6}
EXPECTED_SECTIONS = {"AT": "20", "NAT": "20.1", "DOC": "20.2", "MON": "20.3", "END": "20.4"}


def _passed(document: dict[str, Any]) -> dict[str, Any]:
    """The first gate recorded as passed, for mutations that need one."""
    for gate in document["gates"]:
        if gate["status"] == "passed":
            return gate
    pytest.fail("the ledger records no passed gate, so these mutations cannot apply")


def _unmet(document: dict[str, Any]) -> dict[str, Any]:
    """The first gate whose evidence class this repository cannot produce."""
    for gate in document["gates"]:
        if gate["evidence_type"] not in guard.REPOSITORY_PRODUCIBLE:
            return gate
    pytest.fail("the ledger records no externally blocked gate")


# ---------------------------------------------------------------------------
# The committed ledger
# ---------------------------------------------------------------------------


def test_the_unmutated_baseline_is_clean() -> None:
    assert guard.check(_ledger(), _schema()) == []


def test_the_guard_runs_clean_as_a_process() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(GUARD)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "The release is not qualified." in result.stdout


def test_every_design_gate_is_present_exactly_once() -> None:
    gates = [gate["gate_id"] for gate in _ledger()["gates"]]
    assert len(gates) == len(set(gates)) == sum(EXPECTED_TOTALS.values()) == 130
    for family, total in EXPECTED_TOTALS.items():
        found = sorted(
            int(gate.split("-")[1]) for gate in gates if gate.startswith(f"{family}-")
        )
        assert found == list(range(1, total + 1)), family


def test_the_guards_own_gate_set_matches_the_design() -> None:
    assert {family: count for family, count, _ in guard.EXPECTED_FAMILIES} == (
        EXPECTED_TOTALS
    )
    assert {family: section for family, _, section in guard.EXPECTED_FAMILIES} == (
        EXPECTED_SECTIONS
    )
    assert len(guard.expected_gate_ids()) == 130


def test_the_status_and_evidence_vocabularies_are_the_declared_ones() -> None:
    assert set(guard.STATUSES) == {
        "passed",
        "failed",
        "blocked_external",
        "not_executed",
        "not_implemented",
    }
    assert guard.REPOSITORY_PRODUCIBLE == {
        "automated_repository_test",
        "repository_checker",
    }
    schema_statuses = _schema()["$defs"]["gate"]["properties"]["status"]["enum"]
    assert set(schema_statuses) == set(guard.STATUSES)


def test_no_passed_gate_rests_on_evidence_this_repository_cannot_produce() -> None:
    """The one rule that makes fabricated qualification impossible to record.

    Stated here as well as in the guard, over the committed data, so the claim
    is visible in the suite rather than only inside the checker.
    """
    for gate in _ledger()["gates"]:
        if gate["status"] == "passed":
            assert gate["evidence_type"] in guard.REPOSITORY_PRODUCIBLE, gate["gate_id"]
            assert gate["evidence"], gate["gate_id"]
            assert gate["missing_condition"] is None, gate["gate_id"]
        else:
            assert gate["missing_condition"], gate["gate_id"]


def test_every_unmet_gate_names_a_concrete_missing_condition() -> None:
    for gate in _ledger()["gates"]:
        if gate["status"] != "passed":
            assert len(gate["missing_condition"]) >= 20, gate["gate_id"]


def test_every_gate_records_its_scope_limits() -> None:
    for gate in _ledger()["gates"]:
        assert len(gate["limitations"]) >= 20, gate["gate_id"]


def test_the_ledger_claims_no_signing_notarisation_or_measured_evidence() -> None:
    """No gate needing evidence this repository has never produced is passed.

    The design names signing, notarisation, Intel, older macOS, VoiceOver,
    visual, fixture, performance and real-host evidence. Each maps to an
    evidence class outside REPOSITORY_PRODUCIBLE, so this asserts over the
    classes rather than over wording.
    """
    unproducible = set(guard.EVIDENCE_TYPES) - guard.REPOSITORY_PRODUCIBLE
    assert unproducible == {
        "installed_signed_build",
        "real_host_measurement",
        "accessibility_review",
        "external_fixture_corpus",
    }
    for gate in _ledger()["gates"]:
        if gate["evidence_type"] in unproducible:
            assert gate["status"] != "passed", gate["gate_id"]


def test_every_cited_path_and_symbol_resolves_in_this_checkout() -> None:
    for gate in _ledger()["gates"]:
        for binding in gate["source_bindings"]:
            assert (REPO_ROOT / binding).exists(), f"{gate['gate_id']}: {binding}"
        for reference in gate["evidence"]:
            target = REPO_ROOT / reference["path"]
            assert target.is_file(), f"{gate['gate_id']}: {reference['path']}"
            if "symbol" in reference:
                body = target.read_text(encoding="utf-8")
                assert reference["symbol"] in body, (
                    f"{gate['gate_id']}: {reference['symbol']}"
                )


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------

Mutation = Callable[[dict[str, Any]], None]


def _drop_gate(document: dict[str, Any]) -> None:
    document["gates"] = [
        gate for gate in document["gates"] if gate["gate_id"] != "MON-20"
    ]
    document["expected_counts"]["MON"] = 37
    document["expected_counts"]["gates"] = 129


def _duplicate_gate(document: dict[str, Any]) -> None:
    document["gates"].append(copy.deepcopy(document["gates"][0]))
    document["gates"].pop(1)


def _unknown_gate(document: dict[str, Any]) -> None:
    extra = copy.deepcopy(document["gates"][0])
    extra["gate_id"] = "AT-51"
    document["gates"].append(extra)
    document["gates"].pop(1)


def _reorder_gates(document: dict[str, Any]) -> None:
    document["gates"][3], document["gates"][7] = (
        document["gates"][7],
        document["gates"][3],
    )


def _unknown_status(document: dict[str, Any]) -> None:
    document["gates"][0]["status"] = "qualified"


def _unknown_evidence_type(document: dict[str, Any]) -> None:
    document["gates"][0]["evidence_type"] = "vibes"


def _passed_without_evidence(document: dict[str, Any]) -> None:
    _passed(document)["evidence"] = []


def _passed_on_unproducible_evidence(document: dict[str, Any]) -> None:
    _passed(document)["evidence_type"] = "installed_signed_build"


def _fabricated_signed_pass(document: dict[str, Any]) -> None:
    gate = _unmet(document)
    gate["status"] = "passed"
    gate["missing_condition"] = None


def _passed_still_claiming_a_gap(document: dict[str, Any]) -> None:
    _passed(document)["missing_condition"] = "something is still missing here, truly"


def _unmet_without_a_missing_condition(document: dict[str, Any]) -> None:
    _unmet(document)["missing_condition"] = None


def _failed_without_evidence(document: dict[str, Any]) -> None:
    gate = document["gates"][0]
    gate["status"] = "failed"
    gate["evidence"] = []
    gate["evidence_type"] = "automated_repository_test"
    gate["missing_condition"] = "a long enough sentence to satisfy the schema bound"


def _nonexistent_evidence_path(document: dict[str, Any]) -> None:
    document["gates"][0]["evidence"][0]["path"] = "tests/test_does_not_exist_at_all.py"


def _stale_evidence_symbol(document: dict[str, Any]) -> None:
    document["gates"][0]["evidence"][0]["symbol"] = "testThisWasRenamedLongAgo"


def _nonexistent_source_binding(document: dict[str, Any]) -> None:
    document["gates"][0]["source_bindings"][0] = "apps/core-status-menu-macos/Gone.swift"


def _empty_source_bindings(document: dict[str, Any]) -> None:
    document["gates"][0]["source_bindings"] = []


def _drifted_family_count(document: dict[str, Any]) -> None:
    document["expected_counts"]["MON"] = 37


def _wrong_design_section(document: dict[str, Any]) -> None:
    document["gates"][0]["design_section"] = "20.3"


def _extra_top_level_key(document: dict[str, Any]) -> None:
    document["release_qualified"] = True


def _extra_gate_key(document: dict[str, Any]) -> None:
    document["gates"][0]["waiver"] = "signed off verbally"


MUTATIONS: tuple[tuple[str, Mutation, str], ...] = (
    # The schema's pinned counts catch these two first, which is why the
    # expectation is the schema's wording. The checker's own gate-set logic is
    # exercised directly below, so both layers are proven rather than only
    # whichever one happens to run first.
    ("a_missing_gate", _drop_gate, "is too short"),
    ("a_drifted_family_count_at_the_schema", _drifted_family_count, "expected_counts"),
    ("a_duplicate_gate", _duplicate_gate, "appears 2 times"),
    ("a_gate_not_in_the_design", _unknown_gate, "not a gate in section 20"),
    ("gates_out_of_order", _reorder_gates, "not in family then ordinal order"),
    ("an_unknown_status", _unknown_status, "status"),
    ("an_unknown_evidence_type", _unknown_evidence_type, "evidence_type"),
    ("a_passed_gate_with_no_evidence", _passed_without_evidence, "no evidence is cited"),
    (
        "a_passed_gate_on_unproducible_evidence",
        _passed_on_unproducible_evidence,
        "this repository cannot produce",
    ),
    (
        "a_fabricated_signed_build_pass",
        _fabricated_signed_pass,
        "this repository cannot produce",
    ),
    (
        "a_passed_gate_still_claiming_a_gap",
        _passed_still_claiming_a_gap,
        "a passed gate has nothing missing",
    ),
    (
        "an_unmet_gate_with_no_missing_condition",
        _unmet_without_a_missing_condition,
        "must name the exact condition",
    ),
    ("a_failed_gate_with_no_evidence", _failed_without_evidence, "no evidence is cited"),
    (
        "a_nonexistent_evidence_path",
        _nonexistent_evidence_path,
        "which is not a file in this checkout",
    ),
    ("a_stale_evidence_symbol", _stale_evidence_symbol, "the reference is stale"),
    (
        "a_nonexistent_source_binding",
        _nonexistent_source_binding,
        "which does not exist in this checkout",
    ),
    (
        "empty_source_bindings_on_an_implemented_gate",
        _empty_source_bindings,
        "only honest when the status is not_implemented",
    ),
    ("a_wrong_design_section", _wrong_design_section, "belongs to section 20"),
    ("an_extra_top_level_key", _extra_top_level_key, "release_qualified"),
    ("an_extra_gate_key", _extra_gate_key, "waiver"),
)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=name) for name, mutate, expected in MUTATIONS],
)
def test_the_guard_rejects(mutate: Mutation, expected: str) -> None:
    document = _ledger()
    mutate(document)
    findings = guard.check(document, _schema())
    assert findings, "the mutation was accepted"
    assert any(expected in finding for finding in findings), findings


def test_completeness_catches_a_missing_gate_without_the_schemas_help() -> None:
    """The checker's own gate-set logic, not the schema's pinned array length.

    ``check_completeness`` is called directly because the schema's ``minItems``
    rejects a 129-gate array before ``check`` reaches it. Both layers have to
    work: the schema knows only the count, while this knows which id is gone.
    """
    gates = [gate for gate in _ledger()["gates"] if gate["gate_id"] != "MON-20"]
    findings = guard.check_completeness(gates)
    assert any("MON-20 is in the design but absent" in f for f in findings), findings


def test_completeness_catches_a_duplicate_and_names_its_count() -> None:
    gates = _ledger()["gates"]
    gates[1] = copy.deepcopy(gates[0])
    findings = guard.check_completeness(gates)
    assert any("AT-01 appears 2 times" in f for f in findings), findings


def test_completeness_catches_a_gate_the_design_does_not_define() -> None:
    gates = _ledger()["gates"]
    gates[0]["gate_id"] = "AT-00"
    findings = guard.check_completeness(gates)
    assert any("AT-00 is not a gate in section 20" in f for f in findings), findings


def test_completeness_accepts_the_committed_gate_set() -> None:
    assert guard.check_completeness(_ledger()["gates"]) == []


def test_counts_catches_a_drifted_family_total_without_the_schemas_help() -> None:
    document = _ledger()
    document["expected_counts"]["MON"] = 37
    findings = guard.check_counts(document)
    assert any("expected_counts.MON" in f for f in findings), findings


def test_counts_catches_a_ledger_holding_the_wrong_number_of_gates() -> None:
    document = _ledger()
    document["gates"] = document["gates"][:100]
    findings = guard.check_counts(document)
    assert any("holds 100 gates" in f for f in findings), findings


def test_a_broken_schema_is_reported_rather_than_ignored() -> None:
    schema = _schema()
    schema["type"] = 17
    findings = guard.check(_ledger(), schema)
    assert any("not a valid Draft 2020-12 schema" in finding for finding in findings)


def test_an_unreadable_ledger_is_a_failure_not_an_empty_pass(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(guard.LedgerError):
        guard._read_json(missing)


# ---------------------------------------------------------------------------
# The generated report
# ---------------------------------------------------------------------------


def test_the_committed_report_matches_the_ledger() -> None:
    assert REPORT.read_text(encoding="utf-8") == guard.render_report(_ledger()), (
        "regenerate with: python scripts/check-wp15-qualification.py --write"
    )


def test_the_report_carries_counts_by_family_and_status() -> None:
    report = REPORT.read_text(encoding="utf-8")
    assert "GENERATED FILE - DO NOT EDIT" in report
    assert "## Counts by status" in report
    assert "## Counts by family and status" in report
    for family, section in EXPECTED_SECTIONS.items():
        assert f"| {family} | {section} | {EXPECTED_TOTALS[family]} |" in report
    for status in guard.STATUSES:
        assert status in report


def test_the_report_does_not_claim_the_release_is_qualified() -> None:
    report = REPORT.read_text(encoding="utf-8")
    assert "It is not a claim that the release is qualified." in report
    lowered = report.lower()
    for claim in ("release is qualified", "fully qualified", "qualification complete"):
        assert claim not in lowered.replace("not a claim that the release is qualified", "")


def test_the_tally_accounts_for_every_gate_exactly_once() -> None:
    document = _ledger()
    counts = guard.tally(document["gates"])
    assert sum(sum(row.values()) for row in counts.values()) == len(document["gates"])
    for family, total in EXPECTED_TOTALS.items():
        assert sum(counts[family].values()) == total


def test_write_regenerates_the_report_from_a_changed_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--write is what updates the document, and it writes the ledger's counts.

    The real report path is redirected so the test cannot touch the committed
    file, and the ledger is left alone.
    """
    target = tmp_path / "status.md"
    monkeypatch.setattr(guard, "REPORT_PATH", target)
    assert guard.main([]) == 1  # the redirected path is absent, so drift fails
    assert guard.main(["--write"]) == 0
    assert target.read_text(encoding="utf-8") == guard.render_report(_ledger())
    assert guard.main([]) == 0
