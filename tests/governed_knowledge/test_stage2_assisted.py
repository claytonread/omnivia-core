"""Stage 2 assisted-diagnosis conformance: KI-T27..33 and KI-T39."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from omnivia_core.governed_knowledge.assisted import (
    DIAGNOSIS_CAPABILITY,
    AssistedDiagnosisRequest,
    AssistedWorkerBinding,
    AssistedWorkerCancelled,
    AssistedWorkerFailure,
    AssistedWorkerOutput,
    AssistedWorkerTimedOut,
    DiagnosisAttemptStatus,
    FeedbackIssue,
    ModelIdentityKind,
    assisted_worker_output_from_mapping,
    assisted_worker_output_to_mapping,
    feedback_issue_from_content,
    feedback_issue_to_content,
    run_assisted_diagnosis,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.feedback import (
    DiagnosisCategory,
    ExpertFeedback,
    FeedbackDiagnosis,
    KnowledgeImprovementProposal,
    ProposalDisposition,
    ProposalTransitionKind,
)
from omnivia_core.governed_knowledge.stage2_content import (
    diagnosis_attempt_from_content,
    diagnosis_attempt_to_content,
    worker_binding_from_content,
    worker_binding_to_content,
)
from omnivia_core.semantic_registry.evidence import Classification, EvidenceSpan
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _instant() -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 9, 13, tzinfo=UTC),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.EVIDENCE_ATTESTED,
    )


def _feedback() -> ExpertFeedback:
    return ExpertFeedback(
        feedback_id="fb-1",
        workspace_id="ws-1",
        submitted_by="reviewer-1",
        recorded_at=_instant(),
        correction_content="The monthly plan is not eligible; two issues are present.",
        classification=Classification.INTERNAL,
        retention_class="feedback-3y",
    )


def _binding(**overrides: object) -> AssistedWorkerBinding:
    fields: dict[str, object] = {
        "binding_id": "binding-1",
        "workspace_id": "ws-1",
        "source_id": "platform.local",
        "executor_id": "knowledge.diagnosis",
        "executor_version": "1.0.0",
        "executor_build_hash": DIGEST_A,
        "executor_content_hash": DIGEST_B,
        "runtime_profile_ref": "profile.stage2-v1",
        "policy_ref": "policy.stage2-v1",
        "required_capabilities": (DIAGNOSIS_CAPABILITY,),
        "minimum_isolation": 2,
        "resolved_isolation": 2,
        "run_ref": "run-1",
        "step_ref": "step-1",
        "attempt_ref": "attempt-1",
        "provider_ref": "provider.local",
        "model_ref": "model-version-1",
        "model_identity_kind": ModelIdentityKind.EXACT,
        "observed_at_ref": "observation-1",
    }
    fields.update(overrides)
    return AssistedWorkerBinding(**fields)  # type: ignore[arg-type]


def _request(**overrides: object) -> AssistedDiagnosisRequest:
    fields: dict[str, object] = {
        "request_id": "request-1",
        "workspace_id": "ws-1",
        "feedback_ref": "fb-1",
        "worker_binding": _binding(),
        "authorised_context_refs": ("receipt-1", "manifest-1"),
        "deadline_ms": 10_000,
    }
    fields.update(overrides)
    return AssistedDiagnosisRequest(**fields)  # type: ignore[arg-type]


def _issue(issue_id: str = "issue-1", start: int = 0, end: int = 7) -> FeedbackIssue:
    return FeedbackIssue(
        issue_id=issue_id,
        workspace_id="ws-1",
        feedback_ref="fb-1",
        affected_claim="monthly eligible",
        proposed_correction="monthly is excluded",
        feedback_spans=(EvidenceSpan(f"span-{issue_id}", start, end),),
        supporting_evidence_refs=("evidence-1",),
        task_context_refs=("receipt-1",),
        source_lineage_refs=("human-feedback", "source-decision"),
    )


def _diagnosis(
    diagnosis_id: str = "diagnosis-1",
    category: DiagnosisCategory = DiagnosisCategory.RETRIEVAL_MISS,
) -> FeedbackDiagnosis:
    return FeedbackDiagnosis(
        diagnosis_id=diagnosis_id,
        workspace_id="ws-1",
        feedback_ref="fb-1",
        analyst_ref="binding-1",
        recorded_at=_instant(),
        categories=(category,),
        original_context_reconstructed=True,
        proposed_owner="retrieval-owner",
        evidence_for=("original-checkpoint",),
        evidence_against=("current-checkpoint",),
        coverage_note="original and current timelines examined separately",
    )


def _proposal() -> KnowledgeImprovementProposal:
    return KnowledgeImprovementProposal(
        proposal_id="proposal-1",
        workspace_id="ws-1",
        transition_kind=ProposalTransitionKind.RETRIEVAL_CONFIGURATION,
        base_version_refs=("profile-v1",),
        risk_policy_ref="policy-1",
        required_reviewer_refs=("retrieval-owner",),
        feedback_ref="fb-1",
        diagnosis_ref="diagnosis-1",
        required_evaluation_case_refs=("PC-06",),
        disposition=ProposalDisposition.PROPOSAL_PREPARED,
    )


def _output() -> AssistedWorkerOutput:
    return AssistedWorkerOutput(
        (_issue(),),
        (_diagnosis(),),
        _proposal(),
        input_tokens=11,
        output_tokens=7,
        cost_microunits=3,
    )


def test_ki_t27_multiple_issues_preserve_independent_exact_spans() -> None:
    first = _issue("issue-1", 0, 7)
    second = _issue("issue-2", 8, 18)
    output = AssistedWorkerOutput(
        (first, second),
        (_diagnosis("diagnosis-1"), _diagnosis("diagnosis-2")),
    )
    assert [item.feedback_spans[0].start_offset for item in output.issues] == [0, 8]
    assert feedback_issue_from_content(feedback_issue_to_content(first)) == first


@pytest.mark.parametrize(
    ("category", "expected_owner"),
    (
        (DiagnosisCategory.RETRIEVAL_MISS, "retrieval-owner"),
        (DiagnosisCategory.CONSUMER_TRANSFORMATION_LOSS, "consumer-owner"),
        (DiagnosisCategory.MISSING_KNOWLEDGE, "knowledge-owner"),
    ),
)
def test_ki_t28_t29_diagnosis_categories_do_not_collapse_repairs(
    category: DiagnosisCategory, expected_owner: str
) -> None:
    diagnosis = FeedbackDiagnosis(
        diagnosis_id="diagnosis-category",
        workspace_id="ws-1",
        feedback_ref="fb-1",
        analyst_ref="binding-1",
        recorded_at=_instant(),
        categories=(category,),
        original_context_reconstructed=True,
        proposed_owner=expected_owner,
    )
    assert diagnosis.categories == (category,)
    assert diagnosis.proposed_owner == expected_owner


def test_ki_t30_t31_t32_t33_timeline_access_disagreement_and_lineage_are_explicit() -> None:
    issue = _issue()
    diagnosis = _diagnosis(category=DiagnosisCategory.EXPERT_DISAGREEMENT)
    assert diagnosis.evidence_for != diagnosis.evidence_against
    assert "original and current" in diagnosis.coverage_note
    assert issue.task_context_refs == ("receipt-1",)
    assert issue.source_lineage_refs == ("human-feedback", "source-decision")
    assert diagnosis.categories == (DiagnosisCategory.EXPERT_DISAGREEMENT,)


def test_successful_worker_output_is_validated_and_proposal_remains_pending() -> None:
    expected = _output()
    outcome = run_assisted_diagnosis(
        _request(), _feedback(), lambda _request: assisted_worker_output_to_mapping(expected)
    )
    assert outcome.attempt.status is DiagnosisAttemptStatus.SUCCEEDED
    assert outcome.output == expected
    assert outcome.output is not None and outcome.output.proposal is not None
    assert outcome.output.proposal.is_pending
    assert outcome.attempt.input_tokens == 11
    assert outcome.attempt.output_tokens == 7
    assert outcome.attempt.cost_microunits == 3
    assert diagnosis_attempt_from_content(
        diagnosis_attempt_to_content(outcome.attempt)
    ) == outcome.attempt


@pytest.mark.parametrize("forbidden", ("approved", "tool_calls", "permissions"))
def test_worker_authority_and_effect_fields_are_rejected(forbidden: str) -> None:
    content = assisted_worker_output_to_mapping(_output())
    content[forbidden] = True
    with pytest.raises(GovernedKnowledgeValidationError):
        assisted_worker_output_from_mapping(content)
    outcome = run_assisted_diagnosis(_request(), _feedback(), lambda _request: content)
    assert outcome.attempt.status is DiagnosisAttemptStatus.INVALID_OUTPUT
    assert outcome.output is None


@pytest.mark.parametrize(
    ("worker", "expected"),
    (
        (lambda _request: (_ for _ in ()).throw(AssistedWorkerFailure()), DiagnosisAttemptStatus.FAILED),
        (lambda _request: (_ for _ in ()).throw(AssistedWorkerTimedOut()), DiagnosisAttemptStatus.TIMED_OUT),
        (lambda _request: (_ for _ in ()).throw(AssistedWorkerCancelled()), DiagnosisAttemptStatus.CANCELLED),
    ),
)
def test_ki_t39_feedback_survives_every_worker_failure(
    worker: object, expected: DiagnosisAttemptStatus
) -> None:
    feedback = _feedback()
    outcome = run_assisted_diagnosis(_request(), feedback, worker)  # type: ignore[arg-type]
    assert feedback.feedback_id == "fb-1"
    assert outcome.attempt.feedback_ref == feedback.feedback_id
    assert outcome.attempt.status is expected
    assert outcome.attempt.proposal_ref is None
    assert outcome.output is None


def test_unexpected_worker_exception_is_sanitised_and_cannot_create_a_proposal() -> None:
    def broken_worker(_request: AssistedDiagnosisRequest) -> dict[str, object]:
        raise RuntimeError("provider secret must not escape")

    outcome = run_assisted_diagnosis(_request(), _feedback(), broken_worker)
    assert outcome.attempt.status is DiagnosisAttemptStatus.FAILED
    assert outcome.attempt.safe_error_code == "worker_failed"
    assert outcome.attempt.proposal_ref is None
    assert outcome.output is None


def test_combined_worker_output_is_bounded_by_canonical_bytes() -> None:
    oversized = replace(_diagnosis(), coverage_note="x" * (17 * 1024))
    with pytest.raises(GovernedKnowledgeValidationError):
        AssistedWorkerOutput((_issue(),), (oversized,), None)


def test_pre_cancelled_request_never_calls_worker() -> None:
    called = False

    def worker(_request: AssistedDiagnosisRequest) -> dict[str, object]:
        nonlocal called
        called = True
        return assisted_worker_output_to_mapping(_output())

    outcome = run_assisted_diagnosis(
        _request(cancellation_requested=True), _feedback(), worker
    )
    assert not called
    assert outcome.attempt.status is DiagnosisAttemptStatus.CANCELLED


def test_worker_binding_is_exact_bounded_and_round_trips() -> None:
    binding = _binding(model_identity_kind=ModelIdentityKind.MUTABLE_ALIAS)
    assert worker_binding_from_content(worker_binding_to_content(binding)) == binding
    with pytest.raises(GovernedKnowledgeValidationError):
        _binding(required_capabilities=(DIAGNOSIS_CAPABILITY, "tool.call"))
    with pytest.raises(GovernedKnowledgeValidationError):
        _binding(resolved_isolation=1)


def test_wrong_stage2_profile_version_and_extra_fields_are_rejected() -> None:
    content = worker_binding_to_content(_binding())
    content["profile_version"] = "future-version"
    with pytest.raises(GovernedKnowledgeValidationError):
        worker_binding_from_content(content)
    issue_content = feedback_issue_to_content(_issue())
    issue_content["profile_version"] = "future-version"
    with pytest.raises(GovernedKnowledgeValidationError):
        feedback_issue_from_content(issue_content)
    failed = run_assisted_diagnosis(
        _request(cancellation_requested=True), _feedback(), lambda _request: {}
    ).attempt
    attempt_content = diagnosis_attempt_to_content(failed)
    attempt_content["unexpected"] = True
    with pytest.raises(GovernedKnowledgeValidationError):
        diagnosis_attempt_from_content(attempt_content)
    content = assisted_worker_output_to_mapping(_output())
    nested = dict(content["issues"][0])
    nested["action"] = "publish"
    content["issues"] = [nested]
    with pytest.raises(GovernedKnowledgeValidationError):
        assisted_worker_output_from_mapping(content)
