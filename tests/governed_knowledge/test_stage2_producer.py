"""Stage 2 producer conformance: exact caller content in, derived report and canonical records out."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.governed_knowledge.assisted import (
    DIAGNOSIS_CAPABILITY,
    EVALUATION_CAPABILITY,
    AssistedWorkerBinding,
    ModelIdentityKind,
    worker_binding_to_content,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.evaluation import (
    EVALUATION_PURPOSE,
    PILOT_CRITICAL_CASE_IDS,
    CandidateOverlay,
    DeterministicCheck,
    DeterministicCheckStatus,
    EvaluationReportStatus,
    EvaluationResultStatus,
    build_renewal_pilot_suite,
    candidate_overlay_to_content,
    compute_evaluation_report_digest,
    evaluation_attempt_to_content,
    evaluation_case_to_content,
    evaluation_report_to_content,
    evaluation_suite_to_content,
    run_evaluation_attempt,
    verify_evaluation_report_digest,
)
from omnivia_core.governed_knowledge.stage2_content import (
    evaluation_report_from_content,
)
from omnivia_core.governed_knowledge.stage2_producer import (
    Stage2Production,
    canonical_record,
    produce_evaluation_report,
)
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _instant(offset: int = 0) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=offset),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.EVIDENCE_ATTESTED,
    )


def _overlay() -> CandidateOverlay:
    return CandidateOverlay(
        overlay_id="overlay-1", workspace_id="ws-1", purpose=EVALUATION_PURPOSE,
        baseline_digest=DIGEST_A, candidate_digest=DIGEST_B,
        candidate_refs=("proposal-1",), authorised_context_refs=("manifest-1",),
        created_at=_instant(), expires_at=_instant(3600),
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
    )


def _binding() -> AssistedWorkerBinding:
    return AssistedWorkerBinding(
        binding_id="binding-1", workspace_id="ws-1", source_id="platform.local",
        executor_id="knowledge.evaluator", executor_version="1.0.0",
        executor_build_hash=DIGEST_C, executor_content_hash=DIGEST_A,
        runtime_profile_ref="profile.stage2-v1", policy_ref="policy.stage2-v1",
        required_capabilities=(DIAGNOSIS_CAPABILITY, EVALUATION_CAPABILITY),
        minimum_isolation=2, resolved_isolation=3, run_ref="run-1", step_ref="step-1",
        attempt_ref="attempt-binding", provider_ref="provider.local",
        model_ref="model-version-1", model_identity_kind=ModelIdentityKind.EXACT,
        observed_at_ref="observation-1",
    )


def _checks() -> tuple[DeterministicCheck, ...]:
    return (DeterministicCheck("security", DeterministicCheckStatus.PASS, "check-evidence-1"),)


def _passing_worker(_case: object, _candidate: object, ordinal: int) -> dict[str, object]:
    return {
        "status": "pass", "output_ref": f"output-{ordinal}", "judgement_ref": f"judge-{ordinal}",
        "safe_error_code": None, "input_tokens": 10, "output_tokens": 5, "cost_microunits": 2,
    }


def _valid_content() -> dict[str, Any]:
    """Caller-held pilot content: every case, every attempt, and the binding, all passing."""
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="commercial-owner", owner_review_ref="review-1"
    )
    attempts = []
    for case in cases:
        count = 3 if case.case_id in PILOT_CRITICAL_CASE_IDS or case.case_id == "PC-06" else 1
        for ordinal in range(1, count + 1):
            attempts.append(
                run_evaluation_attempt(
                    case=case, overlay=_overlay(), binding=_binding(), ordinal=ordinal,
                    evaluated_at=_instant(1), deterministic_checks=_checks(), worker=_passing_worker,
                )
            )
    return {
        "overlay": candidate_overlay_to_content(_overlay()),
        "suite": evaluation_suite_to_content(suite),
        "cases": [evaluation_case_to_content(item) for item in cases],
        "attempts": [evaluation_attempt_to_content(item) for item in attempts],
        "worker_bindings": [worker_binding_to_content(_binding())],
    }


def _attempt(content: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    return next(item for item in content["attempts"] if item["attempt_id"] == attempt_id)


def _produce(content: dict[str, Any], *, triggering: str = "PC-06") -> Stage2Production:
    return produce_evaluation_report(
        content, report_id="report-1", triggering_case_ref=triggering,
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
    )


def test_eligible_pilot_derives_the_report_and_records_every_input() -> None:
    production = _produce(_valid_content())
    report = production.report
    assert report.status is EvaluationReportStatus.ELIGIBLE_FOR_REVIEW
    assert report.coverage_complete
    assert report.worker_binding_refs == ("binding-1",)
    assert report.classification is Classification.INTERNAL
    assert report.retention_class == "evaluation-pilot"
    assert verify_evaluation_report_digest(report)
    kinds = [record.kind for record in production.records]
    assert kinds[:2] == ["candidate_overlay", "evaluation_suite"]
    assert kinds.count("evaluation_case") == 12
    assert kinds.count("evaluation_attempt") == 26
    assert kinds[-2:] == ["worker_binding", "evaluation_report"]
    assert production.records[-1].record_id == "report-1"


def test_record_checksums_are_sha256_over_rfc8785_bytes_and_round_trip() -> None:
    production = _produce(_valid_content())
    for record in production.records:
        assert record.checksum == "sha256:" + hashlib.sha256(record.canonical_json.encode("utf-8")).hexdigest()
        assert canonicalize(json.loads(record.canonical_json)) == record.canonical_json
    report_record = production.records[-1]
    assert report_record.canonical_json == canonicalize(evaluation_report_to_content(production.report))
    assert evaluation_report_from_content(json.loads(report_record.canonical_json)) == production.report
    assert production.report.integrity_digest == compute_evaluation_report_digest(production.report)
    assert verify_evaluation_report_digest(production.report)
    assert not verify_evaluation_report_digest(replace(production.report, integrity_digest=DIGEST_A))


def test_canonical_record_checksum_uses_rfc8785_key_order() -> None:
    record = canonical_record("probe", "p-1", {"b": 1, "a": 2})
    assert record.canonical_json == '{"a":2,"b":1}'
    assert record.checksum == "sha256:" + hashlib.sha256(b'{"a":2,"b":1}').hexdigest()


def test_unreviewed_case_derives_inconclusive_not_eligible() -> None:
    content = _valid_content()
    content["cases"][0]["lifecycle"] = "proposed"
    content["cases"][0]["owner_review_ref"] = None
    report = _produce(content).report
    assert report.status is EvaluationReportStatus.INCONCLUSIVE
    assert "case_not_owner_reviewed" in report.deterministic_finding_refs


def test_failed_critical_attempt_blocks_and_later_passes_do_not_hide_it() -> None:
    content = _valid_content()
    _attempt(content, "PC-02-attempt-1").update(status="fail", output_ref="failed-output", safe_error_code=None)
    report = _produce(content).report
    assert report.status is EvaluationReportStatus.BLOCKED
    pc02 = next(item for item in report.case_summaries if item.case_ref == "PC-02")
    assert pc02.outcomes == (
        EvaluationResultStatus.FAIL, EvaluationResultStatus.PASS, EvaluationResultStatus.PASS,
    )


def test_deterministic_failure_blocks_from_the_check_not_a_caller_status() -> None:
    content = _valid_content()
    attempt = _attempt(content, "PC-05-attempt-1")
    attempt["deterministic_checks"][0]["status"] = "fail"
    attempt.update(status="blocked", safe_error_code="deterministic_gate_blocked")
    report = _produce(content).report
    assert report.status is EvaluationReportStatus.BLOCKED
    assert "check-evidence-1" in report.deterministic_finding_refs


def test_missing_critical_repeat_derives_blocked_with_incomplete_coverage() -> None:
    content = _valid_content()
    content["attempts"] = [item for item in content["attempts"] if item["attempt_id"] != "PC-02-attempt-3"]
    report = _produce(content).report
    assert report.status is EvaluationReportStatus.BLOCKED
    assert not report.coverage_complete
    assert "incomplete_required_attempts" in report.deterministic_finding_refs


def test_case_without_any_attempt_derives_blocked_not_an_error() -> None:
    content = _valid_content()
    content["attempts"] = [item for item in content["attempts"] if item["case_ref"] != "PC-05"]
    report = _produce(content).report
    assert report.status is EvaluationReportStatus.BLOCKED
    assert not report.coverage_complete
    pc05 = next(item for item in report.case_summaries if item.case_ref == "PC-05")
    assert pc05.attempt_refs == ()
    assert pc05.outcomes == ()


@pytest.mark.parametrize(
    "key",
    (
        "status", "coverage_complete", "case_summaries", "deterministic_finding_refs",
        "total_cost_microunits", "worker_binding_refs", "limitation_refs", "integrity_digest",
        "report", "unexpected",
    ),
)
def test_caller_cannot_supply_derived_or_unknown_outer_fields(key: str) -> None:
    content = _valid_content()
    content[key] = "forged"
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_caller_cannot_supply_an_attempt_integrity_digest() -> None:
    content = _valid_content()
    _attempt(content, "PC-01-attempt-1")["integrity_digest"] = DIGEST_A
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_missing_outer_field_is_refused() -> None:
    content = _valid_content()
    del content["worker_bindings"]
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_malformed_profiles_and_shapes_are_refused() -> None:
    content = _valid_content()
    content["overlay"]["profile_version"] = "future"
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["suite"]["profile_version"] = "future"
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    _attempt(content, "PC-01-attempt-1")["profile_version"] = "future"
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["cases"] = {}
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_duplicate_ids_and_case_ordinal_pairs_are_refused() -> None:
    content = _valid_content()
    content["cases"][1] = dict(content["cases"][0])
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["worker_bindings"] = [content["worker_bindings"][0], content["worker_bindings"][0]]
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    _attempt(content, "PC-02-attempt-2")["attempt_id"] = "PC-02-attempt-1"
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    _attempt(content, "PC-02-attempt-2")["ordinal"] = 1
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_missing_extra_and_reordered_cases_are_refused() -> None:
    content = _valid_content()
    content["cases"].pop()
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["cases"].append({**content["cases"][0], "case_id": "PC-13"})
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["cases"][0], content["cases"][1] = content["cases"][1], content["cases"][0]
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_missing_and_extra_worker_bindings_are_refused() -> None:
    content = _valid_content()
    content["worker_bindings"] = []
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)

    content = _valid_content()
    content["worker_bindings"].append({**content["worker_bindings"][0], "binding_id": "binding-2"})
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_binding_without_evaluation_authority_is_refused() -> None:
    content = _valid_content()
    content["worker_bindings"][0]["required_capabilities"] = []
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(content)


def test_cross_workspace_content_is_refused() -> None:
    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda c: c["overlay"].update(workspace_id="ws-2"),
        lambda c: c["suite"].update(workspace_id="ws-2"),
        lambda c: c["cases"][3].update(workspace_id="ws-2"),
        lambda c: _attempt(c, "PC-05-attempt-1").update(workspace_id="ws-2"),
        lambda c: c["worker_bindings"][0].update(workspace_id="ws-2"),
    )
    for mutate in mutations:
        content = _valid_content()
        mutate(content)
        with pytest.raises(GovernedKnowledgeValidationError):
            _produce(content)


def test_inconsistent_refs_are_refused() -> None:
    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda c: _attempt(c, "PC-05-attempt-1").update(overlay_ref="overlay-2"),
        lambda c: _attempt(c, "PC-05-attempt-1").update(case_ref="PC-99"),
        lambda c: _attempt(c, "PC-05-attempt-1").update(worker_binding_ref="binding-missing"),
    )
    for mutate in mutations:
        content = _valid_content()
        mutate(content)
        with pytest.raises(GovernedKnowledgeValidationError):
            _produce(content)
    with pytest.raises(GovernedKnowledgeValidationError):
        _produce(_valid_content(), triggering="PC-99")
