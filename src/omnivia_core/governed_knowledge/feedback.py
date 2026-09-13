"""KI-04: `ExpertFeedback`, `FeedbackDiagnosis` and `KnowledgeImprovementProposal`.

Spec section 9. Feedback must be valid without a complete answer receipt and
without any model involvement (9.2); diagnosis is a manual, versioned,
evidence-backed finding, never itself canonical knowledge (9.4-9.5); a proposal
remains pending data that never approves itself (9.6-9.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from omnivia_core.governed_knowledge.applicability import ApplicabilityExpression
from omnivia_core.governed_knowledge.content_limits import (
    enforce_ov_cj1_content_limit,
)
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.governed_knowledge.wire import encode_instant, encode_span
from omnivia_core.semantic_registry.evidence import Classification, EvidenceSpan
from omnivia_core.semantic_registry.temporal import TemporalInstant

FEEDBACK_PROFILE_VERSION = "governed-knowledge-feedback-v1"
DIAGNOSIS_PROFILE_VERSION = "governed-knowledge-diagnosis-v1"
PROPOSAL_PROFILE_VERSION = "governed-knowledge-proposal-v1"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_str_tuple(field_name: str, value: tuple[str, ...]) -> None:
    require(
        isinstance(value, tuple),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a tuple",
    )
    for index, item in enumerate(value):
        require(
            isinstance(item, str) and item.strip() != "",
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{field_name}[{index}] must be a non-empty string",
        )


def _require_unit_interval(field_name: str, value: float) -> None:
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a number",
    )
    require(
        0.0 <= float(value) <= 1.0,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be between 0 and 1",
    )


# --------------------------------------------------------------------------
# ExpertFeedback
# --------------------------------------------------------------------------


class FeedbackContextCoverage(str, Enum):
    """How completely the original answer's context is known (spec 9.2)."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ABSENT = "absent"


class FeedbackDisposition(str, Enum):
    """Retention disposition of one feedback revision (spec 9.2)."""

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class ExpertiseSource(str, Enum):
    """A verified role assignment and an unverified claim remain distinct (spec 9.2)."""

    VERIFIED_ROLE = "verified_role"
    UNVERIFIED_CLAIM = "unverified_claim"


@dataclass(frozen=True, slots=True)
class ExpertiseClaim:
    """Optional expertise metadata attached to one submitter (spec 9.2).

    Evidence that a person corrected an answer is not evidence that every part
    of their correction is authoritative -- this type only records which kind of
    claim was made, never an authority conclusion.
    """

    source: ExpertiseSource
    verified_role_ref: str | None = None
    unverified_claim_text: str | None = None

    def __post_init__(self) -> None:
        require(
            isinstance(self.source, ExpertiseSource),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "ExpertiseClaim.source must be an ExpertiseSource",
        )
        if self.source is ExpertiseSource.VERIFIED_ROLE:
            _require_id("verified_role_ref", self.verified_role_ref or "")
            require(
                self.unverified_claim_text is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "verified_role claims must not carry unverified_claim_text",
            )
        else:
            _require_id("unverified_claim_text", self.unverified_claim_text or "")
            require(
                self.verified_role_ref is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "unverified_claim claims must not carry verified_role_ref",
            )


@dataclass(frozen=True, slots=True)
class ExpertFeedback:
    """One immutable feedback revision (spec 9.2).

    Valid without `source_result_ref` and without any `context_receipt_refs`:
    both are optional, and when `context_receipt_refs` is empty,
    `context_coverage` must not claim `COMPLETE` (`__post_init__` enforces this
    directly) -- an honest "missing original context" is a structural
    guarantee here, not a documentation promise.
    """

    feedback_id: str
    workspace_id: str
    submitted_by: str
    recorded_at: TemporalInstant
    correction_content: str
    classification: Classification
    retention_class: str
    source_result_ref: str | None = None
    affected_output_spans: tuple[EvidenceSpan, ...] = ()
    proposed_applicability_scope_refs: tuple[str, ...] = ()
    supporting_evidence_refs: tuple[str, ...] = ()
    contradicting_evidence_refs: tuple[str, ...] = ()
    context_receipt_refs: tuple[str, ...] = ()
    context_coverage: FeedbackContextCoverage = FeedbackContextCoverage.ABSENT
    expertise_claim: ExpertiseClaim | None = None
    revision_of_feedback_ref: str | None = None
    disposition: FeedbackDisposition = FeedbackDisposition.ACTIVE
    profile_version: str = field(init=False, default=FEEDBACK_PROFILE_VERSION)

    def __post_init__(self) -> None:
        _require_id("feedback_id", self.feedback_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("submitted_by", self.submitted_by)
        require(
            isinstance(self.recorded_at, TemporalInstant),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "recorded_at must be a TemporalInstant",
        )
        _require_id("correction_content", self.correction_content)
        require(
            isinstance(self.classification, Classification),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "classification must be a Classification",
        )
        _require_id("retention_class", self.retention_class)
        for index, span in enumerate(self.affected_output_spans):
            require(
                isinstance(span, EvidenceSpan),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"affected_output_spans[{index}] must be an EvidenceSpan",
            )
        for name in (
            "proposed_applicability_scope_refs",
            "supporting_evidence_refs",
            "contradicting_evidence_refs",
            "context_receipt_refs",
        ):
            _require_str_tuple(name, getattr(self, name))
        require(
            isinstance(self.context_coverage, FeedbackContextCoverage),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "context_coverage must be a FeedbackContextCoverage",
        )
        if not self.context_receipt_refs:
            require(
                self.context_coverage is not FeedbackContextCoverage.COMPLETE,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "context_coverage must not claim complete without a linked "
                "context receipt",
            )
        if self.expertise_claim is not None:
            require(
                isinstance(self.expertise_claim, ExpertiseClaim),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "expertise_claim must be an ExpertiseClaim",
            )
        require(
            isinstance(self.disposition, FeedbackDisposition),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "disposition must be a FeedbackDisposition",
        )
        if self.revision_of_feedback_ref is not None:
            _require_id("revision_of_feedback_ref", self.revision_of_feedback_ref)
        enforce_ov_cj1_content_limit(
            {
                "profile_version": self.profile_version,
                "feedback_id": self.feedback_id,
                "workspace_id": self.workspace_id,
                "submitted_by": self.submitted_by,
                "recorded_at": encode_instant(self.recorded_at),
                "correction_content": self.correction_content,
                "classification": self.classification.value,
                "retention_class": self.retention_class,
                "source_result_ref": self.source_result_ref,
                "affected_output_spans": [
                    encode_span(span) for span in self.affected_output_spans
                ],
                "proposed_applicability_scope_refs": list(
                    self.proposed_applicability_scope_refs
                ),
                "supporting_evidence_refs": list(self.supporting_evidence_refs),
                "contradicting_evidence_refs": list(self.contradicting_evidence_refs),
                "context_receipt_refs": list(self.context_receipt_refs),
                "context_coverage": self.context_coverage.value,
                "expertise_claim": (
                    None
                    if self.expertise_claim is None
                    else {
                        "source": self.expertise_claim.source.value,
                        "verified_role_ref": self.expertise_claim.verified_role_ref,
                        "unverified_claim_text": (
                            self.expertise_claim.unverified_claim_text
                        ),
                    }
                ),
                "revision_of_feedback_ref": self.revision_of_feedback_ref,
                "disposition": self.disposition.value,
            }
        )


# --------------------------------------------------------------------------
# FeedbackDiagnosis
# --------------------------------------------------------------------------


class DiagnosisCategory(str, Enum):
    """Diagnosis categories and their evidence qualifications (spec 9.4)."""

    MISSING_KNOWLEDGE = "missing_knowledge"
    INCORRECT_KNOWLEDGE = "incorrect_knowledge"
    RETRIEVAL_MISS = "retrieval_miss"
    APPLICABILITY_ERROR = "applicability_error"
    TEMPORAL_FRESHNESS_ERROR = "temporal_freshness_error"
    ACCESS_CONTEXT_UNAVAILABLE = "access_context_unavailable"
    CONSUMER_TRANSFORMATION_LOSS = "consumer_transformation_loss"
    PROCEDURE_INSTRUCTION_DEFECT = "procedure_instruction_defect"
    UNSUPPORTED_INFERENCE_CITATION = "unsupported_inference_citation"
    EXPERT_DISAGREEMENT = "expert_disagreement"
    PREFERENCE_STYLE = "preference_style"
    MIXED_INCONCLUSIVE = "mixed_inconclusive"


@dataclass(frozen=True, slots=True)
class FeedbackDiagnosis:
    """A versioned, evidence-backed finding over one feedback item (spec 9.4-9.5).

    Never itself canonical knowledge: nothing here can mark a proposal admitted,
    only feed one (`governed_knowledge.feedback.KnowledgeImprovementProposal`).
    `categories` distinguishes selection/profile gaps
    (`RETRIEVAL_MISS`/`APPLICABILITY_ERROR`) from consumer transformation loss
    (`CONSUMER_TRANSFORMATION_LOSS`) and incomplete knowledge
    (`MISSING_KNOWLEDGE`), per spec 9.4's table.
    """

    diagnosis_id: str
    workspace_id: str
    feedback_ref: str
    analyst_ref: str
    recorded_at: TemporalInstant
    categories: tuple[DiagnosisCategory, ...]
    original_context_reconstructed: bool
    proposed_owner: str
    evidence_for: tuple[str, ...] = ()
    evidence_against: tuple[str, ...] = ()
    coverage_note: str = ""
    confidence: float | None = None
    profile_version: str = field(init=False, default=DIAGNOSIS_PROFILE_VERSION)

    def __post_init__(self) -> None:
        _require_id("diagnosis_id", self.diagnosis_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("feedback_ref", self.feedback_ref)
        _require_id("analyst_ref", self.analyst_ref)
        require(
            isinstance(self.recorded_at, TemporalInstant),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "recorded_at must be a TemporalInstant",
        )
        require(
            isinstance(self.categories, tuple) and len(self.categories) >= 1,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "categories must be a non-empty tuple of DiagnosisCategory",
        )
        for index, category in enumerate(self.categories):
            require(
                isinstance(category, DiagnosisCategory),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"categories[{index}] must be a DiagnosisCategory",
            )
        is_inconclusive = DiagnosisCategory.MIXED_INCONCLUSIVE in self.categories
        if is_inconclusive:
            require(
                len(self.categories) == 1,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "mixed_inconclusive must not be combined with another category",
            )
        _require_str_tuple("evidence_for", self.evidence_for)
        _require_str_tuple("evidence_against", self.evidence_against)
        _require_id("proposed_owner", self.proposed_owner)
        if self.confidence is not None:
            _require_unit_interval("confidence", self.confidence)

    @property
    def is_inconclusive(self) -> bool:
        return DiagnosisCategory.MIXED_INCONCLUSIVE in self.categories


# --------------------------------------------------------------------------
# KnowledgeImprovementProposal
# --------------------------------------------------------------------------


class ProposalTransitionKind(str, Enum):
    """A compound issue's separately owned, linked proposal kinds (spec 9.6).

    Each kind names a distinct approval authority. A single proposal never
    spans more than one: a compound issue creates one linked proposal per
    owner rather than one implicit multi-system transaction.
    """

    KNOWLEDGE_ADMISSION = "knowledge_admission"
    SEMANTIC_CHANGE = "semantic_change"
    RETRIEVAL_CONFIGURATION = "retrieval_configuration"
    WORKFLOW_METHOD = "workflow_method"
    PERMISSION_POLICY = "permission_policy"


class ProposalDisposition(str, Enum):
    """The proposal lifecycle (spec 9.7):
    `received -> triage/analysis -> needs_information | proposal_prepared |
    no_change_proposed -> review -> admitted/applied | rejected | withdrawn`.
    """

    RECEIVED = "received"
    ANALYSIS = "analysis"
    NEEDS_INFORMATION = "needs_information"
    PROPOSAL_PREPARED = "proposal_prepared"
    NO_CHANGE_PROPOSED = "no_change_proposed"
    REVIEW = "review"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


_ADMITTED_LIKE = frozenset({ProposalDisposition.ADMITTED})


@dataclass(frozen=True, slots=True)
class KnowledgeImprovementProposal:
    """A pending, version-bound repair proposal (spec 9.6-9.7).

    Never approves itself: `disposition` may be constructed as `ADMITTED` only
    together with a non-empty `admission_reference` naming the separate,
    existing governance record that actually admitted it -- there is no path
    that lets a proposal assert its own admission.
    """

    proposal_id: str
    workspace_id: str
    transition_kind: ProposalTransitionKind
    base_version_refs: tuple[str, ...]
    risk_policy_ref: str
    required_reviewer_refs: tuple[str, ...]
    feedback_ref: str | None = None
    diagnosis_ref: str | None = None
    supporting_evidence_refs: tuple[str, ...] = ()
    contradicting_evidence_refs: tuple[str, ...] = ()
    applicability_changes: ApplicabilityExpression | None = None
    temporal_changes_note: str = ""
    expected_consumer_impact_refs: tuple[str, ...] = ()
    required_evaluation_case_refs: tuple[str, ...] = ()
    disposition: ProposalDisposition = ProposalDisposition.RECEIVED
    admission_reference: str | None = None
    profile_version: str = field(init=False, default=PROPOSAL_PROFILE_VERSION)

    def __post_init__(self) -> None:
        _require_id("proposal_id", self.proposal_id)
        _require_id("workspace_id", self.workspace_id)
        require(
            isinstance(self.transition_kind, ProposalTransitionKind),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "transition_kind must be a ProposalTransitionKind",
        )
        require(
            isinstance(self.base_version_refs, tuple)
            and len(self.base_version_refs) >= 1,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "base_version_refs must be a non-empty tuple",
        )
        for name in (
            "base_version_refs",
            "required_reviewer_refs",
            "supporting_evidence_refs",
            "contradicting_evidence_refs",
            "expected_consumer_impact_refs",
            "required_evaluation_case_refs",
        ):
            _require_str_tuple(name, getattr(self, name))
        require(
            len(self.required_reviewer_refs) >= 1,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "required_reviewer_refs must be a non-empty tuple",
        )
        _require_id("risk_policy_ref", self.risk_policy_ref)
        if self.feedback_ref is not None:
            _require_id("feedback_ref", self.feedback_ref)
        if self.diagnosis_ref is not None:
            _require_id("diagnosis_ref", self.diagnosis_ref)
        if self.applicability_changes is not None:
            require(
                isinstance(self.applicability_changes, ApplicabilityExpression),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "applicability_changes must be an ApplicabilityExpression",
            )
        require(
            isinstance(self.disposition, ProposalDisposition),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "disposition must be a ProposalDisposition",
        )
        require(
            self.disposition not in _ADMITTED_LIKE,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "proposal content cannot assert admission; bind trusted outer governance state",
        )
        require(
            self.admission_reference is None,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "proposal content cannot carry an admission reference",
        )

    @property
    def is_pending(self) -> bool:
        """Whether this proposal is still pending data, not an accepted change."""
        return True


__all__ = [
    "DIAGNOSIS_PROFILE_VERSION",
    "FEEDBACK_PROFILE_VERSION",
    "PROPOSAL_PROFILE_VERSION",
    "DiagnosisCategory",
    "ExpertFeedback",
    "ExpertiseClaim",
    "ExpertiseSource",
    "FeedbackContextCoverage",
    "FeedbackDiagnosis",
    "FeedbackDisposition",
    "KnowledgeImprovementProposal",
    "ProposalDisposition",
    "ProposalTransitionKind",
]
