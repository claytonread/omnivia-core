"""Structural acceptance tests for the Phase 2 semantic registry fixture corpus.

These tests validate the fixture file's shape and coverage guarantees only.
They do not exercise or reimplement the future temporal resolution algorithm.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "semantic_registry"
    / "phase-2-acceptance-v1.json"
)

EXPECTED_TOP_LEVEL_KEYS = {"corpus_id", "schema_version", "description", "coverage", "cases"}

REQUIRED_B1_KEYS = {
    "b1-exact-duplicate-evidence-one-workspace",
    "b1-identical-evidence-two-workspaces",
    "b1-multiple-observations-one-evidence",
    "b1-contradictory-observations-remain-visible",
    "b1-rejected-candidates-and-reconsideration-triggers",
    "b1-permission-filtered-evidence-metadata-and-sensitive-content",
    "b1-temporal-boundary-states",
    "b1-temporal-precision-levels",
    "b1-timezone-handling",
    "b1-historical-backfill-source-time",
    "b1-correction-supersession-no-history-rewrite",
}

REQUIRED_EXIT_CRITERIA = {
    "phase2-exit-1",
    "phase2-exit-2",
    "phase2-exit-3",
    "phase2-exit-4",
    "phase2-exit-5",
    "phase2-exit-6",
}


@pytest.fixture(scope="module")
def raw_text() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def document(raw_text: str) -> dict:
    return json.loads(raw_text)


@pytest.fixture(scope="module")
def cases_by_id(document: dict) -> dict:
    return {case["case_id"]: case for case in document["cases"]}


def test_corpus_id_and_schema_version(document: dict) -> None:
    assert document["corpus_id"] == "semantic-registry-phase-2-acceptance-v1"
    assert document["schema_version"] == "1.0.0"


def test_top_level_keys_exact(document: dict) -> None:
    assert set(document.keys()) == EXPECTED_TOP_LEVEL_KEYS


def test_case_ids_unique(document: dict) -> None:
    ids = [case["case_id"] for case in document["cases"]]
    assert len(ids) == len(set(ids))


def test_case_input_and_description_nonempty(document: dict) -> None:
    for case in document["cases"]:
        assert case["input"], f"{case['case_id']} has empty input"
        assert case["description"].strip(), f"{case['case_id']} has empty description"


def test_case_has_exactly_one_of_expected_result_or_error(document: dict) -> None:
    for case in document["cases"]:
        has_result = "expected_result" in case
        has_error = "expected_error" in case
        assert has_result != has_error, (
            f"{case['case_id']} must have exactly one of expected_result/expected_error"
        )


def test_b1_coverage_references_resolve(document: dict, cases_by_id: dict) -> None:
    for requirement, case_ids in document["coverage"]["b1_requirements"].items():
        for case_id in case_ids:
            assert case_id in cases_by_id, f"{requirement} references missing case {case_id}"


def test_every_case_is_covered_by_b1_requirements(document: dict, cases_by_id: dict) -> None:
    covered = set()
    for case_ids in document["coverage"]["b1_requirements"].values():
        covered.update(case_ids)
    assert covered == set(cases_by_id.keys())


def test_required_b1_coverage_keys_exact(document: dict) -> None:
    assert set(document["coverage"]["b1_requirements"].keys()) == REQUIRED_B1_KEYS


def test_phase2_exit_criteria_exact_and_nonempty(document: dict, cases_by_id: dict) -> None:
    exit_criteria = document["coverage"]["phase2_exit_criteria"]
    assert set(exit_criteria.keys()) == REQUIRED_EXIT_CRITERIA
    for exit_id, entry in exit_criteria.items():
        assert entry["description"].strip(), f"{exit_id} missing description"
        assert entry["cases"], f"{exit_id} has no cases"
        for case_id in entry["cases"]:
            assert case_id in cases_by_id, f"{exit_id} references missing case {case_id}"


def test_temporal_boundary_states_covered(cases_by_id: dict) -> None:
    expected_states = {"stated", "absent", "attested", "unknown", "open"}
    boundary_cases = {
        "case-temporal-boundary-stated": "stated",
        "case-temporal-boundary-absent": "absent",
        "case-temporal-boundary-attested": "attested",
        "case-temporal-boundary-unknown": "unknown",
        "case-temporal-boundary-open": "open",
    }
    assert set(boundary_cases.values()) == expected_states
    for case_id in boundary_cases:
        case = cases_by_id[case_id]
        states = set()
        for field in ("valid_from", "valid_to", "attested_from", "attested_to"):
            value = case["input"].get(field)
            if value is not None:
                states.add(value["state"])
        assert states & expected_states, f"{case_id} does not exercise a boundary state"


def test_temporal_precision_levels_covered(cases_by_id: dict) -> None:
    expected_precisions = {"year", "month", "day", "hour", "minute", "second"}
    precision_case_ids = {
        "case-temporal-precision-year",
        "case-temporal-precision-month",
        "case-temporal-precision-day",
        "case-temporal-precision-hour",
        "case-temporal-precision-minute",
        "case-temporal-precision-second",
    }
    actual_precisions = {
        cases_by_id[case_id]["input"]["valid_from"]["precision"]
        for case_id in precision_case_ids
    }
    assert actual_precisions == expected_precisions


def test_timezone_kinds_covered(cases_by_id: dict) -> None:
    expected_kinds = {"explicit_offset", "trusted_source_default", "none"}
    tz_case_ids = {
        "case-temporal-timezone-explicit-offset",
        "case-temporal-timezone-trusted-source",
        "case-temporal-timezone-less-text",
    }
    actual_kinds = {
        cases_by_id[case_id]["input"]["valid_from"]["timezone_kind"] for case_id in tz_case_ids
    }
    assert actual_kinds == expected_kinds


def test_expected_intervals_half_open(cases_by_id: dict) -> None:
    interval_case_ids = {
        "case-temporal-boundary-stated",
        "case-temporal-boundary-attested",
        "case-temporal-boundary-open",
    }
    for case_id in interval_case_ids:
        result = cases_by_id[case_id]["expected_result"]
        assert result["interval_type"] == "half_open"


def test_positive_infinity_only_for_explicitly_open_ends(document: dict) -> None:
    for case in document["cases"]:
        result = case.get("expected_result")
        if not result:
            continue
        effective_to = result.get("effective_to")
        if effective_to == "+Infinity":
            assert result.get("end_state") == "open", (
                f"{case['case_id']} uses +Infinity without an open end_state"
            )
        if result.get("end_state") == "open" and "effective_to" in result:
            assert effective_to == "+Infinity"


def test_indeterminate_error_codes_stable(cases_by_id: dict) -> None:
    expected_error_codes = {
        "case-temporal-boundary-absent": "TEMPORAL_START_INDETERMINATE",
        "case-temporal-boundary-unknown": "TEMPORAL_END_INDETERMINATE",
        "case-temporal-timezone-less-text": "TEMPORAL_TIMEZONE_INDETERMINATE",
    }
    for case_id, expected_code in expected_error_codes.items():
        error = cases_by_id[case_id]["expected_error"]
        assert error["error_code"] == expected_code
        assert error["reason"].strip()


def test_dedup_within_workspace_suppression(cases_by_id: dict) -> None:
    case = cases_by_id["case-evidence-dedup-workspace-scoped"]
    evidence_items = case["input"]["evidence_items"]
    workspace_ids = {item["workspace_id"] for item in evidence_items}
    assert workspace_ids == {"ws-alpha"}
    result = case["expected_result"]
    assert result["outcome"] == "deduplicated"
    assert result["distinct_evidence_count"] == 1
    assert result["suppressed_evidence_ids"]


def test_dedup_cross_workspace_separation(cases_by_id: dict) -> None:
    case = cases_by_id["case-evidence-cross-workspace-no-dedup"]
    evidence_items = case["input"]["evidence_items"]
    workspace_ids = {item["workspace_id"] for item in evidence_items}
    assert len(workspace_ids) == 2
    content_hashes = {item["content_hash"] for item in evidence_items}
    assert len(content_hashes) == 1
    result = case["expected_result"]
    assert result["outcome"] == "not_deduplicated"
    assert result["distinct_evidence_count"] == 2


def test_contradiction_case_retains_both_observations(cases_by_id: dict) -> None:
    case = cases_by_id["case-observation-contradictory-retained"]
    observations = case["input"]["observations"]
    assert len(observations) == 2
    values = {obs["value_placeholder"] for obs in observations}
    assert len(values) == 2
    result = case["expected_result"]
    assert result["outcome"] == "both_retained"
    assert len(result["visible_observation_ids"]) == 2
    assert result["suppressed_observation_ids"] == []
    assert result["contradiction_flagged"] is True


def test_permission_cases_separate_metadata_from_sensitive_content(cases_by_id: dict) -> None:
    metadata_case = cases_by_id["case-permission-metadata-filtered"]
    metadata_result = metadata_case["expected_result"]
    assert metadata_result["metadata_visible"] is True
    assert metadata_result["sensitive_content_visible"] is False

    denied_case = cases_by_id["case-permission-sensitive-content-denied"]
    denied_result = denied_case["expected_result"]
    assert denied_result["outcome"] == "denied"
    assert denied_result["sensitive_content_visible"] is False
    assert denied_result["error_message_contains_sensitive_content"] is False


def test_reconsideration_triggers_cover_exactly_expected_types(cases_by_id: dict) -> None:
    expected_triggers = {"new_evidence", "rule_version_changed", "expired", "human_override"}
    trigger_case_ids = {
        "case-candidate-reconsideration-new-evidence",
        "case-candidate-reconsideration-rule-version-changed",
        "case-candidate-reconsideration-expired",
        "case-candidate-reconsideration-human-override",
    }
    actual_triggers = {
        cases_by_id[case_id]["input"]["trigger_event"]["trigger_type"]
        for case_id in trigger_case_ids
    }
    assert actual_triggers == expected_triggers
    for case_id in trigger_case_ids:
        result = cases_by_id[case_id]["expected_result"]
        assert result["outcome"] == "reconsideration_triggered"
        assert result["suppression_active"] is False


def test_rejected_candidate_baseline_remains_suppressed(cases_by_id: dict) -> None:
    case = cases_by_id["case-candidate-rejected-suppressed-baseline"]
    result = case["expected_result"]
    assert result["outcome"] == "suppressed"
    assert result["suppression_active"] is True
    assert result["reconsideration_trigger"] is None


def test_correction_case_appends_without_mutating_original(cases_by_id: dict) -> None:
    case = cases_by_id["case-assertion-correction-supersession-no-rewrite"]
    original_id = case["input"]["original_assertion"]["assertion_id"]
    correction_id = case["input"]["correction"]["assertion_id"]
    result = case["expected_result"]
    assert result["original_assertion_id"] == original_id
    assert result["current_assertion_id"] == correction_id
    assert result["original_assertion_mutated"] is False
    assert result["original_assertion_still_queryable"] is True
    assert result["supersession_chain"] == [original_id, correction_id]


def test_json_text_is_canonically_formatted(raw_text: str, document: dict) -> None:
    expected_text = json.dumps(document, indent=4, ensure_ascii=False) + "\n"
    assert raw_text == expected_text
