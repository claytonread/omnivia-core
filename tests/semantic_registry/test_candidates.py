"""Phase 2 candidate/aggregation/suppression/reconsideration contract tests.

Spec authority: `SPEC-CORE-SEM-001` v0.2; decision record
`docs/development/omnivia-core-semantic-registry-phase-2-decision-record-2026-09-12.md`
sections 5-7.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnivia_core.semantic_registry.candidates import (
    AGGREGATION_RULE_VERSION,
    NORMALIZATION_RULE_VERSION,
    CandidateBand,
    CandidateContribution,
    CandidateReconsideration,
    CandidateRiskBand,
    CandidateState,
    CandidateSuppression,
    ContributionRole,
    ReconsiderationReason,
    SemanticCandidate,
    aggregate_candidate_features,
    candidate_bundle_digest,
    candidate_bundle_payload,
    candidate_contribution_digest,
    candidate_digest,
    candidate_equivalence_signature,
    reconsideration_digest,
    suppression_active,
    suppression_digest,
)
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.operations import add_concept, change_label
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant(value: datetime) -> TemporalInstant:
    return TemporalInstant(
        value=value, precision=TemporalPrecision.SECOND, provenance=TemporalProvenance.STATED
    )


def _operation(operation_id: str = "op1") -> object:
    return add_concept(operation_id, "concept1", {"label": "Widget"})


def _candidate(
    *,
    candidate_id: str = "cand1",
    workspace_id: str = "ws1",
    state: CandidateState = CandidateState.DRAFT,
    rejection_signature: str | None = None,
    proposed_operation: object | None = None,
) -> SemanticCandidate:
    return SemanticCandidate(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        candidate_kind="ontology_change",
        target_model_id="model1",
        proposed_operation=proposed_operation or _operation(),
        support_band=CandidateBand.LOW,
        novelty_band=CandidateBand.LOW,
        risk_band=CandidateRiskBand.LOW,
        state=state,
        aggregation_version=AGGREGATION_RULE_VERSION,
        normalization_version=NORMALIZATION_RULE_VERSION,
        base_version_id="base1",
        evidence_snapshot_digest="sha256:" + "a" * 64,
        created_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        rejection_signature=rejection_signature,
    )


def _contribution(
    *,
    workspace_id: str = "ws1",
    candidate_id: str = "cand1",
    observation_id: str = "obs1",
    role: ContributionRole = ContributionRole.SUPPORT,
    weight: int = 100,
) -> CandidateContribution:
    return CandidateContribution(
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        observation_id=observation_id,
        role=role,
        weight=weight,
        observation_digest="sha256:" + "b" * 64,
    )


def _suppression(
    *,
    expires_at: TemporalInstant | None = None,
    evidence_snapshot_digest: str = "sha256:" + "a" * 64,
    aggregation_version: str = AGGREGATION_RULE_VERSION,
) -> CandidateSuppression:
    return CandidateSuppression(
        workspace_id="ws1",
        suppression_id="sup1",
        equivalence_signature="sha256:" + "c" * 64,
        rejection_ref="cand1",
        suppression_rule_version="candidate-suppression-v1",
        created_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        evidence_snapshot_digest=evidence_snapshot_digest,
        aggregation_version=aggregation_version,
        expires_at=expires_at,
    )


# --- Candidate invariants ---------------------------------------------------


def test_candidate_requires_rejection_signature_when_rejected() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _candidate(state=CandidateState.REJECTED, rejection_signature=None)
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_candidate_requires_rejection_signature_when_suppressed() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _candidate(state=CandidateState.SUPPRESSED, rejection_signature=None)
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_candidate_rejects_signature_on_non_terminal_state() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _candidate(state=CandidateState.DRAFT, rejection_signature="sig1")
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_candidate_accepts_signature_on_rejected_state() -> None:
    candidate = _candidate(state=CandidateState.REJECTED, rejection_signature="sig1")
    assert candidate.rejection_signature == "sig1"


def test_candidate_has_no_approved_or_published_state() -> None:
    state_values = {state.value for state in CandidateState}
    assert state_values == {
        "draft",
        "active",
        "proposed",
        "suppressed",
        "rejected",
        "reconsidered",
    }
    assert "approved" not in state_values
    assert "published" not in state_values


def test_candidate_rejects_non_change_operation() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _candidate(proposed_operation="not-an-operation")
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_candidate_accepts_an_ordinary_change_label_operation() -> None:
    operation = change_label("op2", "concept1", {"label": "Gadget"})
    candidate = _candidate(proposed_operation=operation)
    assert candidate.proposed_operation is operation


# --- Contributions -----------------------------------------------------------


def test_contribution_rejects_weight_out_of_range() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _contribution(weight=10001)
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_contribution_rejects_negative_weight() -> None:
    with pytest.raises(SemanticValidationError):
        _contribution(weight=-1)


def test_contribution_rejects_non_enum_role() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _contribution(role="support")  # type: ignore[arg-type]
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_contribution_digest_changes_with_role() -> None:
    support = _contribution(role=ContributionRole.SUPPORT)
    contradict = _contribution(role=ContributionRole.CONTRADICT)
    assert candidate_contribution_digest(support) != candidate_contribution_digest(
        contradict
    )


# --- Equivalence signatures ---------------------------------------------------


def test_equivalence_signature_is_deterministic() -> None:
    operation = _operation()
    sig1 = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    sig2 = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    assert sig1 == sig2


def test_equivalence_signature_ignores_candidate_id_timestamp_and_state() -> None:
    operation = _operation()
    signature = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    candidate_a = _candidate(candidate_id="cand-a", proposed_operation=operation)
    candidate_b = _candidate(
        candidate_id="cand-b",
        proposed_operation=operation,
        state=CandidateState.ACTIVE,
    )
    assert candidate_a.candidate_id != candidate_b.candidate_id
    assert candidate_a.state != candidate_b.state
    # both would resolve to the same equivalence signature since the
    # signature never consults candidate_id, created_at or state
    assert signature == candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )


def test_equivalence_signature_differs_by_workspace() -> None:
    operation = _operation()
    sig1 = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    sig2 = candidate_equivalence_signature(
        "ws2", "ontology_change", "model1", operation,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    assert sig1 != sig2


def test_equivalence_signature_differs_by_operation_content() -> None:
    op_a = add_concept("op1", "concept1", {"label": "Widget"})
    op_b = add_concept("op1", "concept1", {"label": "Gadget"})
    sig_a = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", op_a,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    sig_b = candidate_equivalence_signature(
        "ws1", "ontology_change", "model1", op_b,
        NORMALIZATION_RULE_VERSION, AGGREGATION_RULE_VERSION,
    )
    assert sig_a != sig_b


# --- Deterministic aggregation -------------------------------------------------


def test_aggregation_sums_weights_and_counts_per_role() -> None:
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=100),
        _contribution(observation_id="obs2", role=ContributionRole.SUPPORT, weight=200),
        _contribution(observation_id="obs3", role=ContributionRole.CONTRADICT, weight=50),
        _contribution(observation_id="obs4", role=ContributionRole.NOVELTY, weight=10),
    ]
    summary = aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    assert summary.support_count == 2
    assert summary.support_weight == 300
    assert summary.contradict_count == 1
    assert summary.contradict_weight == 50
    assert summary.novelty_count == 1
    assert summary.novelty_weight == 10


def test_aggregation_is_invariant_to_contribution_order() -> None:
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=100),
        _contribution(observation_id="obs2", role=ContributionRole.CONTRADICT, weight=5000),
        _contribution(observation_id="obs3", role=ContributionRole.NOVELTY, weight=10),
    ]
    forward = aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    backward = aggregate_candidate_features(
        list(reversed(contributions)), AGGREGATION_RULE_VERSION
    )
    assert forward == backward


def test_aggregation_bands_support_weight() -> None:
    low = aggregate_candidate_features(
        [_contribution(observation_id="o1", weight=999)], AGGREGATION_RULE_VERSION
    )
    medium = aggregate_candidate_features(
        [_contribution(observation_id="o1", weight=1000)], AGGREGATION_RULE_VERSION
    )
    high = aggregate_candidate_features(
        [_contribution(observation_id="o1", weight=5000)], AGGREGATION_RULE_VERSION
    )
    assert low.support_band is CandidateBand.LOW
    assert medium.support_band is CandidateBand.MEDIUM
    assert high.support_band is CandidateBand.HIGH


def test_aggregation_risk_band_low_only_when_no_contradiction() -> None:
    summary = aggregate_candidate_features(
        [_contribution(observation_id="o1", role=ContributionRole.SUPPORT, weight=100)],
        AGGREGATION_RULE_VERSION,
    )
    assert summary.risk_band is CandidateRiskBand.LOW


def test_aggregation_risk_band_increases_with_any_contradiction() -> None:
    summary = aggregate_candidate_features(
        [_contribution(observation_id="o1", role=ContributionRole.CONTRADICT, weight=1)],
        AGGREGATION_RULE_VERSION,
    )
    assert summary.risk_band is CandidateRiskBand.STANDARD


def test_aggregation_risk_band_reaches_critical() -> None:
    summary = aggregate_candidate_features(
        [_contribution(observation_id="o1", role=ContributionRole.CONTRADICT, weight=5000)],
        AGGREGATION_RULE_VERSION,
    )
    assert summary.risk_band is CandidateRiskBand.CRITICAL


def test_contradictory_contributions_remain_represented_and_increase_risk() -> None:
    baseline = aggregate_candidate_features(
        [_contribution(observation_id="o1", role=ContributionRole.SUPPORT, weight=100)],
        AGGREGATION_RULE_VERSION,
    )
    with_contradiction = aggregate_candidate_features(
        [
            _contribution(observation_id="o1", role=ContributionRole.SUPPORT, weight=100),
            _contribution(observation_id="o2", role=ContributionRole.CONTRADICT, weight=500),
        ],
        AGGREGATION_RULE_VERSION,
    )
    assert with_contradiction.contradict_count == 1
    assert with_contradiction.contradict_weight == 500
    assert baseline.risk_band is CandidateRiskBand.LOW
    assert with_contradiction.risk_band is CandidateRiskBand.STANDARD


def test_aggregation_rejects_non_contribution_entries() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features(["not-a-contribution"], AGGREGATION_RULE_VERSION)  # type: ignore[list-item]
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_aggregation_requires_aggregation_version() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features([], "")
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


# --- Duplicate/cross-workspace fail-closed regressions -------------------------


def test_aggregation_rejects_duplicate_observation_contribution() -> None:
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=100),
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=200),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_aggregation_rejects_duplicate_observation_across_roles() -> None:
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=100),
        _contribution(observation_id="obs1", role=ContributionRole.CONTRADICT, weight=200),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_aggregation_rejects_mixed_candidate_ids() -> None:
    contributions = [
        _contribution(candidate_id="cand1", observation_id="obs1"),
        _contribution(candidate_id="cand2", observation_id="obs2"),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


def test_aggregation_rejects_mixed_workspace_ids() -> None:
    contributions = [
        _contribution(workspace_id="ws1", observation_id="obs1"),
        _contribution(workspace_id="ws2", observation_id="obs2"),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        aggregate_candidate_features(contributions, AGGREGATION_RULE_VERSION)
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


def test_bundle_rejects_contribution_from_a_different_workspace() -> None:
    candidate = _candidate(workspace_id="ws1", candidate_id="cand1")
    foreign_contribution = _contribution(workspace_id="ws2", candidate_id="cand1")
    with pytest.raises(SemanticValidationError) as excinfo:
        candidate_bundle_payload(candidate, [foreign_contribution])
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


def test_bundle_rejects_contribution_from_a_different_candidate() -> None:
    candidate = _candidate(workspace_id="ws1", candidate_id="cand1")
    foreign_contribution = _contribution(workspace_id="ws1", candidate_id="cand-other")
    with pytest.raises(SemanticValidationError) as excinfo:
        candidate_bundle_payload(candidate, [foreign_contribution])
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


# --- Canonical payloads / digests ----------------------------------------------


def test_candidate_digest_is_deterministic() -> None:
    candidate = _candidate()
    assert candidate_digest(candidate) == candidate_digest(candidate)


def test_candidate_digest_changes_with_state() -> None:
    draft = _candidate(state=CandidateState.DRAFT)
    active = _candidate(state=CandidateState.ACTIVE)
    assert candidate_digest(draft) != candidate_digest(active)


def test_candidate_bundle_digest_is_deterministic() -> None:
    candidate = _candidate()
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT),
        _contribution(observation_id="obs2", role=ContributionRole.CONTRADICT),
    ]
    assert candidate_bundle_digest(candidate, contributions) == candidate_bundle_digest(
        candidate, contributions
    )


def test_candidate_bundle_digest_is_invariant_to_contribution_order() -> None:
    candidate = _candidate()
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT, weight=100),
        _contribution(observation_id="obs2", role=ContributionRole.CONTRADICT, weight=50),
        _contribution(observation_id="obs3", role=ContributionRole.NOVELTY, weight=10),
    ]
    forward = candidate_bundle_digest(candidate, contributions)
    backward = candidate_bundle_digest(candidate, list(reversed(contributions)))
    assert forward == backward


def test_candidate_bundle_digest_changes_when_a_contradiction_is_added() -> None:
    candidate = _candidate()
    baseline = [_contribution(observation_id="obs1", role=ContributionRole.SUPPORT)]
    with_contradiction = baseline + [
        _contribution(observation_id="obs2", role=ContributionRole.CONTRADICT, weight=50)
    ]
    assert candidate_bundle_digest(candidate, baseline) != candidate_bundle_digest(
        candidate, with_contradiction
    )


def test_candidate_bundle_payload_retains_contradictory_contributions() -> None:
    candidate = _candidate()
    contributions = [
        _contribution(observation_id="obs1", role=ContributionRole.SUPPORT),
        _contribution(observation_id="obs2", role=ContributionRole.CONTRADICT),
    ]
    payload = candidate_bundle_payload(candidate, contributions)
    roles = {c["observation_id"]: c["role"] for c in payload["contributions"]}
    assert roles == {"obs1": "support", "obs2": "contradict"}


# --- Suppression activity ------------------------------------------------------


def test_suppression_active_when_nothing_has_changed() -> None:
    suppression = _suppression()
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version=suppression.aggregation_version,
    )
    assert activity.active is True
    assert activity.reason is None


def test_suppression_inactive_after_expiry() -> None:
    suppression = _suppression(
        expires_at=_instant(datetime(2026, 2, 1, tzinfo=UTC))
    )
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version=suppression.aggregation_version,
    )
    assert activity.active is False
    assert activity.reason is ReconsiderationReason.EXPIRED


def test_suppression_still_active_before_expiry() -> None:
    suppression = _suppression(
        expires_at=_instant(datetime(2026, 2, 1, tzinfo=UTC))
    )
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2026, 1, 31, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version=suppression.aggregation_version,
    )
    assert activity.active is True


def test_suppression_inactive_when_evidence_digest_changed() -> None:
    suppression = _suppression()
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        evidence_snapshot_digest="sha256:" + "f" * 64,
        aggregation_version=suppression.aggregation_version,
    )
    assert activity.active is False
    assert activity.reason is ReconsiderationReason.NEW_EVIDENCE


def test_suppression_inactive_when_rule_version_changed() -> None:
    suppression = _suppression()
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version="candidate-aggregation-v2",
    )
    assert activity.active is False
    assert activity.reason is ReconsiderationReason.RULE_VERSION_CHANGED


def test_suppression_without_expiry_never_expires_on_its_own() -> None:
    suppression = _suppression(expires_at=None)
    activity = suppression_active(
        suppression,
        at=_instant(datetime(2099, 1, 1, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version=suppression.aggregation_version,
    )
    assert activity.active is True


def test_suppression_requires_expiry_strictly_after_created_at() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateSuppression(
            workspace_id="ws1",
            suppression_id="sup1",
            equivalence_signature="sha256:" + "c" * 64,
            rejection_ref="cand1",
            suppression_rule_version="candidate-suppression-v1",
            created_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            evidence_snapshot_digest="sha256:" + "a" * 64,
            aggregation_version=AGGREGATION_RULE_VERSION,
            expires_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID


def test_suppression_digest_is_deterministic() -> None:
    suppression = _suppression()
    assert suppression_digest(suppression) == suppression_digest(suppression)


def test_suppression_digest_changes_with_evidence_digest() -> None:
    a = _suppression(evidence_snapshot_digest="sha256:" + "a" * 64)
    b = _suppression(evidence_snapshot_digest="sha256:" + "f" * 64)
    assert suppression_digest(a) != suppression_digest(b)


# --- Reconsideration receipts ---------------------------------------------------


def test_new_evidence_reconsideration_requires_changed_evidence_digest() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.NEW_EVIDENCE,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            previous_evidence_digest="sha256:" + "a" * 64,
            new_evidence_digest="sha256:" + "a" * 64,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_new_evidence_reconsideration_rejects_rule_version_fields() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.NEW_EVIDENCE,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            previous_evidence_digest="sha256:" + "a" * 64,
            new_evidence_digest="sha256:" + "b" * 64,
            previous_rule_version="v1",
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_new_evidence_reconsideration_accepts_matching_fields() -> None:
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id="sup1",
        reason=ReconsiderationReason.NEW_EVIDENCE,
        recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        previous_evidence_digest="sha256:" + "a" * 64,
        new_evidence_digest="sha256:" + "b" * 64,
    )
    assert reconsideration.reason is ReconsiderationReason.NEW_EVIDENCE


def test_rule_version_changed_requires_changed_rule_version() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.RULE_VERSION_CHANGED,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            previous_rule_version="v1",
            new_rule_version="v1",
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_rule_version_changed_rejects_evidence_fields() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.RULE_VERSION_CHANGED,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            previous_rule_version="v1",
            new_rule_version="v2",
            previous_evidence_digest="sha256:" + "a" * 64,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_rule_version_changed_accepts_matching_fields() -> None:
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id="sup1",
        reason=ReconsiderationReason.RULE_VERSION_CHANGED,
        recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        previous_rule_version="v1",
        new_rule_version="v2",
    )
    assert reconsideration.new_rule_version == "v2"


def test_expired_reconsideration_rejects_any_change_fields() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.EXPIRED,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            previous_evidence_digest="sha256:" + "a" * 64,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_expired_reconsideration_accepts_no_change_fields() -> None:
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id="sup1",
        reason=ReconsiderationReason.EXPIRED,
        recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
    )
    assert reconsideration.reason is ReconsiderationReason.EXPIRED


def test_human_override_requires_actor_principal_id() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.HUMAN_OVERRIDE,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        )
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_human_override_rejects_blank_actor_principal_id() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        CandidateReconsideration(
            workspace_id="ws1",
            reconsideration_id="recon1",
            suppression_id="sup1",
            reason=ReconsiderationReason.HUMAN_OVERRIDE,
            recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
            actor_principal_id="   ",
        )
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_human_override_accepts_actor_principal_id() -> None:
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id="sup1",
        reason=ReconsiderationReason.HUMAN_OVERRIDE,
        recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        actor_principal_id="user1",
    )
    assert reconsideration.actor_principal_id == "user1"


def test_all_four_reconsideration_triggers_are_represented() -> None:
    reasons = {reason.value for reason in ReconsiderationReason}
    assert reasons == {
        "new_evidence",
        "rule_version_changed",
        "expired",
        "human_override",
    }


def test_reconsideration_digest_is_deterministic() -> None:
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id="sup1",
        reason=ReconsiderationReason.HUMAN_OVERRIDE,
        recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        actor_principal_id="user1",
    )
    assert reconsideration_digest(reconsideration) == reconsideration_digest(
        reconsideration
    )


# --- Suppression persists until an explicit reconsideration --------------------


def test_rejected_equivalent_candidate_stays_suppressed_until_explicit_trigger() -> None:
    """A candidate matching an active suppression's equivalence signature must
    stay suppressed until expiry, evidence-digest change, rule-version change,
    or an explicit human-override receipt -- never merely because it was
    looked at again."""
    suppression = _suppression()
    unchanged_lookup = suppression_active(
        suppression,
        at=_instant(datetime(2026, 6, 1, tzinfo=UTC)),
        evidence_snapshot_digest=suppression.evidence_snapshot_digest,
        aggregation_version=suppression.aggregation_version,
    )
    assert unchanged_lookup.active is True

    # Only an explicit CandidateReconsideration receipt, not the lookup
    # itself, may record why a suppressed candidate is being re-raised.
    reconsideration = CandidateReconsideration(
        workspace_id="ws1",
        reconsideration_id="recon1",
        suppression_id=suppression.suppression_id,
        reason=ReconsiderationReason.HUMAN_OVERRIDE,
        recorded_at=_instant(datetime(2026, 6, 1, tzinfo=UTC)),
        actor_principal_id="user1",
    )
    assert reconsideration.suppression_id == suppression.suppression_id
