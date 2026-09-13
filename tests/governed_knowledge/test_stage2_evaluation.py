"""Stage 2 overlay/evaluation conformance: KI-T43..KI-T50."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from omnivia_core.governed_knowledge.assisted import (
    DIAGNOSIS_CAPABILITY,
    EVALUATION_CAPABILITY,
    AssistedWorkerBinding,
    ModelIdentityKind,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.evaluation import (
    EVALUATION_PURPOSE,
    PILOT_CASE_IDS,
    PILOT_CRITICAL_CASE_IDS,
    PILOT_REPEAT_COUNT,
    CandidateOverlay,
    DeterministicCheck,
    DeterministicCheckStatus,
    EvaluationAttempt,
    EvaluationCaseLifecycle,
    EvaluationReportStatus,
    EvaluationResultStatus,
    EvaluationWorkerFailure,
    KnowledgeEvaluationCase,
    aggregate_evaluation,
    build_renewal_pilot_suite,
    compute_evaluation_report_digest,
    run_evaluation_attempt,
    verify_evaluation_report_digest,
)
from omnivia_core.governed_knowledge.stage2_content import (
    candidate_overlay_from_content,
    candidate_overlay_to_content,
    evaluation_attempt_from_content,
    evaluation_attempt_to_content,
    evaluation_case_from_content,
    evaluation_case_to_content,
    evaluation_report_from_content,
    evaluation_report_to_content,
    evaluation_suite_from_content,
    evaluation_suite_to_content,
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


def _overlay(**overrides: object) -> CandidateOverlay:
    fields: dict[str, object] = {
        "overlay_id": "overlay-1",
        "workspace_id": "ws-1",
        "purpose": EVALUATION_PURPOSE,
        "baseline_digest": DIGEST_A,
        "candidate_digest": DIGEST_B,
        "candidate_refs": ("proposal-1",),
        "authorised_context_refs": ("manifest-1",),
        "created_at": _instant(),
        "expires_at": _instant(3600),
        "classification": Classification.INTERNAL,
        "retention_class": "evaluation-pilot",
    }
    fields.update(overrides)
    return CandidateOverlay(**fields)  # type: ignore[arg-type]


def _binding(**overrides: object) -> AssistedWorkerBinding:
    fields: dict[str, object] = {
        "binding_id": "binding-1",
        "workspace_id": "ws-1",
        "source_id": "platform.local",
        "executor_id": "knowledge.evaluator",
        "executor_version": "1.0.0",
        "executor_build_hash": DIGEST_C,
        "executor_content_hash": DIGEST_A,
        "runtime_profile_ref": "profile.stage2-v1",
        "policy_ref": "policy.stage2-v1",
        "required_capabilities": (DIAGNOSIS_CAPABILITY, EVALUATION_CAPABILITY),
        "minimum_isolation": 2,
        "resolved_isolation": 3,
        "run_ref": "run-1",
        "step_ref": "step-1",
        "attempt_ref": "attempt-binding",
        "provider_ref": "provider.local",
        "model_ref": "model-version-1",
        "model_identity_kind": ModelIdentityKind.EXACT,
        "observed_at_ref": "observation-1",
    }
    fields.update(overrides)
    return AssistedWorkerBinding(**fields)  # type: ignore[arg-type]


def _checks(status: DeterministicCheckStatus = DeterministicCheckStatus.PASS) -> tuple[DeterministicCheck, ...]:
    return (DeterministicCheck("security", status, "check-evidence-1"),)


def _passing_worker(
    _case: KnowledgeEvaluationCase, _overlay: CandidateOverlay, ordinal: int
) -> dict[str, object]:
    return {
        "status": "pass",
        "output_ref": f"output-{ordinal}",
        "judgement_ref": f"judge-{ordinal}",
        "safe_error_code": None,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_microunits": 2,
    }


def _passing_attempts(
    cases: tuple[KnowledgeEvaluationCase, ...],
    *, triggering_case_ref: str = "PC-06",
    binding: AssistedWorkerBinding | None = None,
) -> tuple[EvaluationAttempt, ...]:
    selected_binding = binding or _binding()
    attempts = []
    for case in cases:
        count = (
            PILOT_REPEAT_COUNT
            if case.case_id in PILOT_CRITICAL_CASE_IDS
            or case.case_id == triggering_case_ref
            else 1
        )
        for ordinal in range(1, count + 1):
            attempts.append(
                run_evaluation_attempt(
                    case=case,
                    overlay=_overlay(),
                    binding=selected_binding,
                    ordinal=ordinal,
                    evaluated_at=_instant(1),
                    deterministic_checks=_checks(),
                    worker=_passing_worker,
                )
            )
    return tuple(attempts)


def test_pilot_suite_is_exactly_pc01_to_pc12_with_the_fixed_critical_set() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="commercial-owner", owner_review_ref="review-1"
    )
    assert suite.case_refs == PILOT_CASE_IDS
    assert suite.critical_case_refs == PILOT_CRITICAL_CASE_IDS
    assert suite.critical_repeat_count == 3
    assert tuple(item.case_id for item in cases) == PILOT_CASE_IDS
    assert {item.case_id for item in cases if item.critical} == set(PILOT_CRITICAL_CASE_IDS)
    assert evaluation_suite_from_content(evaluation_suite_to_content(suite)) == suite
    assert all(evaluation_case_from_content(evaluation_case_to_content(item)) == item for item in cases)


def test_ki_t43_overlay_is_evaluation_only_expiring_and_non_authoritative() -> None:
    overlay = _overlay()
    assert not hasattr(overlay, "search")
    assert not hasattr(overlay, "admit")
    assert overlay.live_selection_enabled is False
    assert overlay.permission_grant_refs == ()
    assert candidate_overlay_from_content(candidate_overlay_to_content(overlay)) == overlay
    with pytest.raises(GovernedKnowledgeValidationError):
        _overlay(purpose="live_selection")
    with pytest.raises(GovernedKnowledgeValidationError):
        _overlay(live_selection_enabled=True)
    with pytest.raises(GovernedKnowledgeValidationError):
        _overlay(permission_grant_refs=("grant-1",))
    with pytest.raises(GovernedKnowledgeValidationError):
        overlay.require_usable(workspace_id="ws-1", at=_instant(3600))
    with pytest.raises(GovernedKnowledgeValidationError):
        overlay.require_usable(
            workspace_id="ws-1", at=_instant(1), expected_candidate_digest=DIGEST_C
        )


def test_ki_t44_worker_has_no_tool_or_production_effect_channel() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    del suite

    def effect_claim(_case: object, overlay_value: object, _ordinal: int) -> dict[str, object]:
        del overlay_value
        output = _passing_worker(cases[0], _overlay(), 1)
        output["tool_calls"] = [{"name": "charge_customer"}]
        return output

    attempt = run_evaluation_attempt(
        case=cases[0], overlay=_overlay(), binding=_binding(), ordinal=1,
        evaluated_at=_instant(1),
        deterministic_checks=_checks(), worker=effect_claim,
    )
    assert attempt.status is EvaluationResultStatus.BLOCKED
    assert attempt.safe_error_code == "invalid_worker_output"


def test_ki_t45_expected_answer_and_rubric_cannot_leak_into_consumer_input() -> None:
    _, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    case = cases[0]
    with pytest.raises(GovernedKnowledgeValidationError):
        replace(
            case,
            consumer_visible_input_refs=(case.scenario_input_ref, case.expected_outcome_ref),
        )
    with pytest.raises(GovernedKnowledgeValidationError):
        replace(case, consumer_visible_input_refs=(case.rubric_ref,))


def test_ki_t46_deterministic_failure_blocks_without_invoking_worker() -> None:
    _, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    called = False

    def worker(*_args: object) -> dict[str, object]:
        nonlocal called
        called = True
        return _passing_worker(cases[0], _overlay(), 1)

    attempt = run_evaluation_attempt(
        case=cases[0], overlay=_overlay(), binding=_binding(), ordinal=1,
        evaluated_at=_instant(1),
        deterministic_checks=_checks(DeterministicCheckStatus.FAIL), worker=worker,
    )
    assert not called
    assert attempt.status is EvaluationResultStatus.BLOCKED
    with pytest.raises(GovernedKnowledgeValidationError):
        replace(attempt, status=EvaluationResultStatus.PASS, output_ref="output-1", safe_error_code=None)


@pytest.mark.parametrize(
    "status",
    (
        EvaluationResultStatus.INDETERMINATE,
        EvaluationResultStatus.BLOCKED,
        EvaluationResultStatus.NOT_RUN,
        EvaluationResultStatus.CANCELLED,
    ),
)
def test_ki_t47_required_non_result_blocks_the_report(status: EvaluationResultStatus) -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    attempts = list(_passing_attempts(cases))
    attempts[0] = replace(attempts[0], status=status, output_ref=None, safe_error_code="required_case_incomplete")
    report = aggregate_evaluation(
        report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
        attempts=tuple(attempts), triggering_case_ref="PC-06",
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
        worker_bindings=(_binding(),),
    )
    assert report.status is EvaluationReportStatus.BLOCKED


def test_ki_t48_all_attempts_are_retained_and_later_pass_does_not_hide_failure() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    attempts = list(_passing_attempts(cases))
    critical_index = next(index for index, item in enumerate(attempts) if item.case_ref == "PC-02")
    attempts[critical_index] = replace(
        attempts[critical_index], status=EvaluationResultStatus.FAIL,
        output_ref="failed-output", safe_error_code=None,
    )
    report = aggregate_evaluation(
        report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
        attempts=tuple(attempts), triggering_case_ref="PC-06",
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
        worker_bindings=(_binding(),),
    )
    pc02 = next(item for item in report.case_summaries if item.case_ref == "PC-02")
    assert pc02.outcomes == (
        EvaluationResultStatus.FAIL,
        EvaluationResultStatus.PASS,
        EvaluationResultStatus.PASS,
    )
    assert report.status is EvaluationReportStatus.BLOCKED


def test_ki_t49_mutable_model_alias_is_recorded_as_a_limitation() -> None:
    binding = _binding(model_identity_kind=ModelIdentityKind.MUTABLE_ALIAS)
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    attempts = _passing_attempts(cases, binding=binding)
    report = aggregate_evaluation(
        report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
        attempts=attempts, triggering_case_ref="PC-06",
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
        worker_bindings=(binding,),
    )
    assert report.limitation_refs == ("mutable_model_alias",)


def test_ki_t50_feedback_derived_case_stays_proposed_until_owner_review() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    proposed = replace(
        cases[0],
        lifecycle=EvaluationCaseLifecycle.PROPOSED,
        owner_review_ref=None,
        originating_feedback_ref="feedback-1",
    )
    assert proposed.lifecycle is EvaluationCaseLifecycle.PROPOSED
    assert proposed.owner_review_ref is None
    changed_cases = (proposed, *cases[1:])
    report = aggregate_evaluation(
        report_id="report-proposed", suite=suite, cases=changed_cases,
        overlay=_overlay(), attempts=_passing_attempts(changed_cases),
        triggering_case_ref="PC-06", classification=Classification.INTERNAL,
        retention_class="evaluation-pilot", worker_bindings=(_binding(),),
    )
    assert report.status is EvaluationReportStatus.INCONCLUSIVE


def test_complete_pilot_runs_three_attempts_for_critical_and_triggering_cases() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    attempts = _passing_attempts(cases, triggering_case_ref="PC-06")
    counts = {case_id: sum(item.case_ref == case_id for item in attempts) for case_id in PILOT_CASE_IDS}
    assert counts["PC-06"] == 3
    assert all(counts[case_id] == 3 for case_id in PILOT_CRITICAL_CASE_IDS)
    assert all(counts[case_id] == 1 for case_id in set(PILOT_CASE_IDS) - set(PILOT_CRITICAL_CASE_IDS) - {"PC-06"})
    report = aggregate_evaluation(
        report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
        attempts=attempts, triggering_case_ref="PC-06",
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
        worker_bindings=(_binding(),),
    )
    assert report.status is EvaluationReportStatus.ELIGIBLE_FOR_REVIEW
    assert report.coverage_complete
    assert verify_evaluation_report_digest(report)
    assert report.integrity_digest == compute_evaluation_report_digest(report)
    assert evaluation_report_from_content(evaluation_report_to_content(report)) == report
    assert sum(item.cost_microunits or 0 for item in attempts) == report.total_cost_microunits
    repeated = aggregate_evaluation(
        report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
        attempts=attempts, triggering_case_ref="PC-06",
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
        worker_bindings=(_binding(),),
    )
    assert repeated == report
    assert repeated.integrity_digest == report.integrity_digest


def test_evaluation_attempt_codec_and_worker_failure_are_explicit() -> None:
    _, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )

    def unavailable(*_args: object) -> dict[str, object]:
        raise EvaluationWorkerFailure(EvaluationResultStatus.BLOCKED, "provider_unavailable")

    attempt = run_evaluation_attempt(
        case=cases[0], overlay=_overlay(), binding=_binding(), ordinal=1,
        evaluated_at=_instant(1),
        deterministic_checks=_checks(), worker=unavailable,
    )
    assert attempt.status is EvaluationResultStatus.BLOCKED
    assert attempt.safe_error_code == "provider_unavailable"
    assert evaluation_attempt_from_content(evaluation_attempt_to_content(attempt)) == attempt


def test_unexpected_evaluation_worker_exception_is_sanitised() -> None:
    _, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )

    def broken_worker(*_args: object) -> dict[str, object]:
        raise RuntimeError("provider secret must not escape")

    attempt = run_evaluation_attempt(
        case=cases[0], overlay=_overlay(), binding=_binding(), ordinal=1,
        evaluated_at=_instant(1), deterministic_checks=_checks(), worker=broken_worker,
    )
    assert attempt.status is EvaluationResultStatus.BLOCKED
    assert attempt.safe_error_code == "worker_failed"
    assert attempt.output_ref is None


def test_behavioural_fail_requires_retained_output_evidence() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        EvaluationAttempt(
            attempt_id="attempt-1", workspace_id="ws-1", case_ref="PC-01", ordinal=1,
            worker_binding_ref="binding-1", overlay_ref="overlay-1",
            status=EvaluationResultStatus.FAIL, deterministic_checks=_checks(),
        )


def test_aggregate_rejects_attempt_bound_to_non_evaluation_worker() -> None:
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    diagnosis_only = _binding(required_capabilities=(DIAGNOSIS_CAPABILITY,))
    attempts = tuple(
        replace(item, worker_binding_ref=diagnosis_only.binding_id)
        for item in _passing_attempts(cases)
    )
    with pytest.raises(GovernedKnowledgeValidationError):
        aggregate_evaluation(
            report_id="report-1", suite=suite, cases=cases, overlay=_overlay(),
            attempts=attempts, triggering_case_ref="PC-06",
            classification=Classification.INTERNAL, retention_class="evaluation-pilot",
            worker_bindings=(diagnosis_only,),
        )


def test_wrong_versions_and_extra_fields_are_rejected_strictly() -> None:
    overlay_content = candidate_overlay_to_content(_overlay())
    overlay_content["profile_version"] = "future"
    with pytest.raises(GovernedKnowledgeValidationError):
        candidate_overlay_from_content(overlay_content)
    suite, _ = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
    )
    suite_content = evaluation_suite_to_content(suite)
    suite_content["unexpected"] = True
    with pytest.raises(GovernedKnowledgeValidationError):
        evaluation_suite_from_content(suite_content)
    case_content = evaluation_case_to_content(
        build_renewal_pilot_suite(
            workspace_id="ws-1", owner_ref="owner", owner_review_ref="review-1"
        )[1][0]
    )
    case_content["profile_version"] = "future"
    with pytest.raises(GovernedKnowledgeValidationError):
        evaluation_case_from_content(case_content)
