"""Tests for KI-04 `ExpertFeedback`, `FeedbackDiagnosis`, `KnowledgeImprovementProposal`.

Covers model-free feedback with and without an original receipt, diagnosis
categories, and proposal pending/no-authority semantics (spec 9.2, 9.4, 9.6-9.7).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.feedback import (
    DiagnosisCategory,
    ExpertFeedback,
    ExpertiseClaim,
    ExpertiseSource,
    FeedbackContextCoverage,
    FeedbackDiagnosis,
    KnowledgeImprovementProposal,
    ProposalDisposition,
    ProposalTransitionKind,
)
from omnivia_core.governed_knowledge.profile_content import (
    diagnosis_from_content,
    diagnosis_to_content,
    feedback_from_content,
    feedback_to_content,
    proposal_from_content,
    proposal_to_content,
)
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant() -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 1, 1, tzinfo=UTC),
        precision=TemporalPrecision.DAY,
        provenance=TemporalProvenance.STATED,
    )


def _feedback(**overrides: object) -> ExpertFeedback:
    fields: dict[str, object] = {
        "feedback_id": "fb-1",
        "workspace_id": "ws-1",
        "submitted_by": "user-1",
        "recorded_at": _instant(),
        "correction_content": "The discount threshold is 12 months, not 6.",
        "classification": Classification.INTERNAL,
        "retention_class": "standard-3y",
    }
    fields.update(overrides)
    return ExpertFeedback(**fields)  # type: ignore[arg-type]


def test_feedback_valid_without_receipt_or_model() -> None:
    feedback = _feedback()
    assert feedback.context_receipt_refs == ()
    assert feedback.context_coverage is FeedbackContextCoverage.ABSENT
    assert feedback_from_content(feedback_to_content(feedback)) == feedback


def test_feedback_with_receipt_can_claim_complete_coverage() -> None:
    feedback = _feedback(
        context_receipt_refs=("receipt-1",),
        context_coverage=FeedbackContextCoverage.COMPLETE,
    )
    assert feedback.context_coverage is FeedbackContextCoverage.COMPLETE


def test_feedback_cannot_claim_complete_coverage_without_a_receipt() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _feedback(context_coverage=FeedbackContextCoverage.COMPLETE)


def test_manually_supplied_correction_without_answer_receipt() -> None:
    feedback = _feedback(
        source_result_ref=None, context_coverage=FeedbackContextCoverage.ABSENT
    )
    assert feedback.source_result_ref is None


def test_verified_role_and_unverified_claim_are_distinct() -> None:
    verified = ExpertiseClaim(
        source=ExpertiseSource.VERIFIED_ROLE, verified_role_ref="role-1"
    )
    unverified = ExpertiseClaim(
        source=ExpertiseSource.UNVERIFIED_CLAIM,
        unverified_claim_text="I used to work in this",
    )
    with pytest.raises(GovernedKnowledgeValidationError):
        ExpertiseClaim(
            source=ExpertiseSource.VERIFIED_ROLE,
            verified_role_ref="role-1",
            unverified_claim_text="also this",
        )
    assert verified.source is ExpertiseSource.VERIFIED_ROLE
    assert unverified.source is ExpertiseSource.UNVERIFIED_CLAIM


def test_16kib_content_limit_applies_to_feedback() -> None:
    from omnivia_core.governed_knowledge.content_limits import OV_CJ1_MAX_CONTENT_BYTES

    with pytest.raises(GovernedKnowledgeValidationError):
        _feedback(correction_content="x" * (OV_CJ1_MAX_CONTENT_BYTES + 1))


def test_diagnosis_categories_distinguish_gap_kinds() -> None:
    retrieval = FeedbackDiagnosis(
        diagnosis_id="diag-1",
        workspace_id="ws-1",
        feedback_ref="fb-1",
        analyst_ref="analyst-1",
        recorded_at=_instant(),
        categories=(DiagnosisCategory.RETRIEVAL_MISS,),
        original_context_reconstructed=True,
        proposed_owner="retrieval_profile",
    )
    consumer_loss = FeedbackDiagnosis(
        diagnosis_id="diag-2",
        workspace_id="ws-1",
        feedback_ref="fb-1",
        analyst_ref="analyst-1",
        recorded_at=_instant(),
        categories=(DiagnosisCategory.CONSUMER_TRANSFORMATION_LOSS,),
        original_context_reconstructed=True,
        proposed_owner="consumer_harness",
    )
    missing = FeedbackDiagnosis(
        diagnosis_id="diag-3",
        workspace_id="ws-1",
        feedback_ref="fb-1",
        analyst_ref="analyst-1",
        recorded_at=_instant(),
        categories=(DiagnosisCategory.MISSING_KNOWLEDGE,),
        original_context_reconstructed=False,
        proposed_owner="pending_knowledge_assertion",
    )
    assert retrieval.categories != consumer_loss.categories != missing.categories
    assert diagnosis_from_content(diagnosis_to_content(retrieval)) == retrieval
    assert not retrieval.is_inconclusive


def test_diagnosis_mixed_inconclusive_cannot_combine_with_other_categories() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        FeedbackDiagnosis(
            diagnosis_id="diag-1",
            workspace_id="ws-1",
            feedback_ref="fb-1",
            analyst_ref="analyst-1",
            recorded_at=_instant(),
            categories=(
                DiagnosisCategory.MIXED_INCONCLUSIVE,
                DiagnosisCategory.MISSING_KNOWLEDGE,
            ),
            original_context_reconstructed=False,
            proposed_owner="triage",
        )


def test_diagnosis_requires_at_least_one_category() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        FeedbackDiagnosis(
            diagnosis_id="diag-1",
            workspace_id="ws-1",
            feedback_ref="fb-1",
            analyst_ref="analyst-1",
            recorded_at=_instant(),
            categories=(),
            original_context_reconstructed=True,
            proposed_owner="triage",
        )


def _proposal(**overrides: object) -> KnowledgeImprovementProposal:
    fields: dict[str, object] = {
        "proposal_id": "prop-1",
        "workspace_id": "ws-1",
        "transition_kind": ProposalTransitionKind.KNOWLEDGE_ADMISSION,
        "base_version_refs": ("pos-1-v1",),
        "risk_policy_ref": "risk-standard",
        "required_reviewer_refs": ("reviewer-1",),
    }
    fields.update(overrides)
    return KnowledgeImprovementProposal(**fields)  # type: ignore[arg-type]


def test_proposal_is_pending_by_default() -> None:
    proposal = _proposal()
    assert proposal.is_pending is True
    assert proposal.disposition is ProposalDisposition.RECEIVED
    assert proposal_from_content(proposal_to_content(proposal)) == proposal


def test_proposal_cannot_self_approve_without_admission_reference() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _proposal(disposition=ProposalDisposition.ADMITTED)


def test_proposal_content_cannot_self_assert_admission() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _proposal(
            disposition=ProposalDisposition.ADMITTED,
            admission_reference="approval-record-1",
        )


def test_admission_reference_only_valid_when_admitted() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _proposal(
            disposition=ProposalDisposition.REVIEW,
            admission_reference="approval-record-1",
        )


def test_proposal_requires_at_least_one_reviewer() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _proposal(required_reviewer_refs=())
