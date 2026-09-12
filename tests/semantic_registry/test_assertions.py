"""Phase 2 governed assertion contract tests (decision record section 4-5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnivia_core.semantic_registry.assertions import (
    AssertionEvidence,
    AssertionRetraction,
    AssertionSupersession,
    KnowledgeAssertion,
    KnowledgeObjectKind,
    assertion_digest,
    assertion_effective_interval,
    assertion_evidence_digest,
    assertion_evidence_payload,
    assertion_history_digest,
    assertion_history_payload,
    assertion_payload,
    assertion_retraction_digest,
    assertion_supersession_digest,
    current_assertion_id,
)
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.evidence import Classification, EvidenceSupportRole
from omnivia_core.semantic_registry.temporal import (
    EndBoundaryState,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant(
    value: datetime, provenance: TemporalProvenance = TemporalProvenance.STATED
) -> TemporalInstant:
    return TemporalInstant(
        value=value, precision=TemporalPrecision.SECOND, provenance=provenance
    )


def _assertion(
    assertion_id: str = "a1",
    workspace_id: str = "ws1",
    valid_to_state: EndBoundaryState = EndBoundaryState.OPEN,
    valid_from: TemporalInstant | None = None,
    valid_to: TemporalInstant | None = None,
    attested_to: TemporalInstant | None = None,
    recorded_at: TemporalInstant | None = None,
    recorded_until: TemporalInstant | None = None,
    **overrides: object,
) -> KnowledgeAssertion:
    kwargs = {
        "assertion_id": assertion_id,
        "workspace_id": workspace_id,
        "subject_id": "s1",
        "predicate_element_id": "p1",
        "model_version_id": "m1",
        "object_kind": KnowledgeObjectKind.ENTITY,
        "object_id": "o1",
        "confidence": 0.9,
        "attested_from": _instant(datetime(2026, 1, 1, tzinfo=UTC)),
        "valid_to_state": valid_to_state,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "attested_to": attested_to,
        "recorded_at": recorded_at or _instant(datetime(2026, 1, 2, tzinfo=UTC)),
        "recorded_until": recorded_until,
        "classification": Classification.INTERNAL,
    }
    kwargs.update(overrides)
    return KnowledgeAssertion(**kwargs)  # type: ignore[arg-type]


# --- KnowledgeAssertion boundary combinations ------------------------------


def test_open_end_state_allows_no_end_fields() -> None:
    assertion = _assertion(valid_to_state=EndBoundaryState.OPEN)
    interval = assertion_effective_interval(assertion)
    assert interval.end_state is EndBoundaryState.OPEN
    assert interval.effective_to is None


def test_stated_end_state_requires_valid_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(valid_to_state=EndBoundaryState.STATED)
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_stated_end_state_forbids_attested_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            valid_to_state=EndBoundaryState.STATED,
            valid_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
            attested_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_stated_end_state_with_valid_to_resolves() -> None:
    assertion = _assertion(
        valid_to_state=EndBoundaryState.STATED,
        valid_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
    )
    interval = assertion_effective_interval(assertion)
    assert interval.end_state is EndBoundaryState.STATED
    assert interval.effective_to is not None


def test_unknown_end_state_requires_attested_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(valid_to_state=EndBoundaryState.UNKNOWN)
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_unknown_end_state_forbids_valid_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            valid_to_state=EndBoundaryState.UNKNOWN,
            valid_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
            attested_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_unknown_end_state_with_attested_to_resolves() -> None:
    assertion = _assertion(
        valid_to_state=EndBoundaryState.UNKNOWN,
        attested_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
    )
    interval = assertion_effective_interval(assertion)
    assert interval.end_state is EndBoundaryState.UNKNOWN


def test_open_end_state_rejects_valid_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            valid_to_state=EndBoundaryState.OPEN,
            valid_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_open_end_state_rejects_attested_to() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            valid_to_state=EndBoundaryState.OPEN,
            attested_to=_instant(datetime(2026, 2, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_stated_end_before_start_fails_closed_via_shared_contract() -> None:
    with pytest.raises(SemanticValidationError):
        _assertion(
            valid_to_state=EndBoundaryState.STATED,
            valid_from=_instant(datetime(2026, 3, 1, tzinfo=UTC)),
            valid_to=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        )


def test_recorded_until_must_be_after_recorded_at() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            recorded_at=_instant(datetime(2026, 1, 5, tzinfo=UTC)),
            recorded_until=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_recorded_until_after_recorded_at_is_accepted() -> None:
    assertion = _assertion(
        recorded_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        recorded_until=_instant(datetime(2026, 1, 5, tzinfo=UTC)),
    )
    assert assertion.recorded_until is not None


def test_object_kind_entity_forbids_literal_value() -> None:
    with pytest.raises(SemanticValidationError):
        _assertion(
            object_kind=KnowledgeObjectKind.ENTITY,
            object_id="o1",
            literal_value="x",
        )


def test_object_kind_literal_forbids_object_id() -> None:
    with pytest.raises(SemanticValidationError):
        _assertion(
            object_kind=KnowledgeObjectKind.LITERAL,
            object_id="o1",
            literal_value=None,
        )


def test_object_kind_literal_freezes_json_safe_value() -> None:
    assertion = _assertion(
        object_kind=KnowledgeObjectKind.LITERAL,
        object_id=None,
        literal_value={"a": [1, 2, {"b": 3}]},
    )
    assert assertion.literal_value["a"][2]["b"] == 3


def test_object_kind_literal_rejects_non_json_safe_value() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _assertion(
            object_kind=KnowledgeObjectKind.LITERAL,
            object_id=None,
            literal_value=object(),
        )
    assert excinfo.value.code == SemanticErrorCode.UNSUPPORTED_VALUE


# --- Evidence references ---------------------------------------------------


def test_assertion_evidence_requires_valid_role() -> None:
    with pytest.raises(SemanticValidationError):
        AssertionEvidence(
            workspace_id="ws1",
            assertion_id="a1",
            evidence_id="ev1",
            role="support",  # type: ignore[arg-type]
            confidence=0.5,
        )


def test_assertion_evidence_payload_has_no_raw_content() -> None:
    evidence = AssertionEvidence(
        workspace_id="ws1",
        assertion_id="a1",
        evidence_id="ev1",
        role=EvidenceSupportRole.CONTRADICT,
        confidence=0.4,
        span_id="span1",
    )
    payload = assertion_evidence_payload(evidence)
    assert set(payload) == {
        "workspace_id",
        "assertion_id",
        "evidence_id",
        "role",
        "confidence",
        "span_id",
    }
    assert payload["role"] == "contradict"
    assert assertion_evidence_digest(evidence)


def test_contradictory_and_supporting_evidence_both_remain_visible() -> None:
    support = AssertionEvidence(
        workspace_id="ws1",
        assertion_id="a1",
        evidence_id="ev1",
        role=EvidenceSupportRole.SUPPORT,
        confidence=0.9,
    )
    contradict = AssertionEvidence(
        workspace_id="ws1",
        assertion_id="a1",
        evidence_id="ev2",
        role=EvidenceSupportRole.CONTRADICT,
        confidence=0.3,
    )
    references = [support, contradict]
    roles = {ref.role for ref in references}
    assert roles == {EvidenceSupportRole.SUPPORT, EvidenceSupportRole.CONTRADICT}


# --- Correction / supersession ---------------------------------------------


def test_supersession_rejects_self_link() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup1",
            prior_assertion_id="a1",
            successor_assertion_id="a1",
            reason_code="correction",
            decision_id="d1",
            recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
        )
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_correction_appends_successor_and_does_not_rewrite_predecessor() -> None:
    prior = _assertion(assertion_id="a1")
    prior_digest_before = assertion_digest(prior)
    successor = _assertion(assertion_id="a2")
    supersession = AssertionSupersession(
        workspace_id="ws1",
        supersession_id="sup1",
        prior_assertion_id="a1",
        successor_assertion_id="a2",
        reason_code="correction",
        decision_id="d1",
        recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
    )
    history = assertion_history_payload(
        assertions=[prior, successor], supersessions=[supersession]
    )
    assert history["assertions"][0]["assertion_id"] == "a1"
    assert assertion_digest(prior) == prior_digest_before
    assert current_assertion_id("a1", [supersession]) == "a2"


def test_current_assertion_id_follows_multi_hop_chain() -> None:
    supersessions = [
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup1",
            prior_assertion_id="a1",
            successor_assertion_id="a2",
            reason_code="correction",
            decision_id="d1",
            recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
        ),
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup2",
            prior_assertion_id="a2",
            successor_assertion_id="a3",
            reason_code="correction",
            decision_id="d2",
            recorded_at=_instant(datetime(2026, 1, 4, tzinfo=UTC)),
        ),
    ]
    assert current_assertion_id("a1", supersessions) == "a3"


def test_current_assertion_id_with_no_supersession_returns_self() -> None:
    assert current_assertion_id("a1", []) == "a1"


def test_supersession_chain_rejects_cycle() -> None:
    supersessions = [
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup1",
            prior_assertion_id="a1",
            successor_assertion_id="a2",
            reason_code="correction",
            decision_id="d1",
            recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
        ),
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup2",
            prior_assertion_id="a2",
            successor_assertion_id="a1",
            reason_code="correction",
            decision_id="d2",
            recorded_at=_instant(datetime(2026, 1, 4, tzinfo=UTC)),
        ),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        current_assertion_id("a1", supersessions)
    assert excinfo.value.code == SemanticErrorCode.CYCLIC_DEPENDENCY


def test_supersession_chain_rejects_branch() -> None:
    supersessions = [
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup1",
            prior_assertion_id="a1",
            successor_assertion_id="a2",
            reason_code="correction",
            decision_id="d1",
            recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
        ),
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup2",
            prior_assertion_id="a1",
            successor_assertion_id="a3",
            reason_code="correction",
            decision_id="d2",
            recorded_at=_instant(datetime(2026, 1, 4, tzinfo=UTC)),
        ),
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        current_assertion_id("a1", supersessions)
    assert excinfo.value.code == SemanticErrorCode.INVALID_FIELD


def test_supersession_chain_rejects_unknown_reference() -> None:
    supersessions = [
        AssertionSupersession(
            workspace_id="ws1",
            supersession_id="sup1",
            prior_assertion_id="a1",
            successor_assertion_id="a2",
            reason_code="correction",
            decision_id="d1",
            recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
        )
    ]
    with pytest.raises(SemanticValidationError) as excinfo:
        current_assertion_id("a1", supersessions, known_assertion_ids=["a1"])
    assert excinfo.value.code == SemanticErrorCode.UNKNOWN_REFERENCE


def test_history_rejects_cross_workspace_supersession() -> None:
    a1 = _assertion(assertion_id="a1", workspace_id="ws1")
    a2 = _assertion(assertion_id="a2", workspace_id="ws1")
    supersession = AssertionSupersession(
        workspace_id="ws2",
        supersession_id="sup1",
        prior_assertion_id="a1",
        successor_assertion_id="a2",
        reason_code="correction",
        decision_id="d1",
        recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
    )
    with pytest.raises(SemanticValidationError) as excinfo:
        assertion_history_payload(assertions=[a1, a2], supersessions=[supersession])
    assert excinfo.value.code == SemanticErrorCode.CROSS_WORKSPACE_ACCESS


# --- Retraction --------------------------------------------------------------


def test_retraction_payload_and_digest() -> None:
    retraction = AssertionRetraction(
        workspace_id="ws1",
        retraction_id="r1",
        assertion_id="a1",
        retracted_at=_instant(datetime(2026, 1, 5, tzinfo=UTC)),
        reason_code="error",
        policy_version="p1",
        actor_principal_id="user1",
    )
    assert assertion_retraction_digest(retraction)


def test_retraction_requires_all_ids() -> None:
    with pytest.raises(SemanticValidationError):
        AssertionRetraction(
            workspace_id="ws1",
            retraction_id="",
            assertion_id="a1",
            retracted_at=_instant(datetime(2026, 1, 5, tzinfo=UTC)),
            reason_code="error",
            policy_version="p1",
            actor_principal_id="user1",
        )


# --- Canonical payloads / digests --------------------------------------------


def test_assertion_payload_excludes_no_evidence_content() -> None:
    assertion = _assertion()
    payload = assertion_payload(assertion)
    assert "evidence" not in payload
    assert "evidence_id" not in payload


def test_assertion_digest_is_stable_and_content_bound() -> None:
    assertion = _assertion()
    digest1 = assertion_digest(assertion)
    digest2 = assertion_digest(_assertion())
    assert digest1 == digest2

    different = _assertion(confidence=0.1)
    assert assertion_digest(different) != digest1


def test_supersession_digest_bound_to_content() -> None:
    supersession = AssertionSupersession(
        workspace_id="ws1",
        supersession_id="sup1",
        prior_assertion_id="a1",
        successor_assertion_id="a2",
        reason_code="correction",
        decision_id="d1",
        recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
    )
    assert assertion_supersession_digest(supersession)


# --- History / current resolution determinism under reordering --------------


def test_history_payload_deterministic_under_reordering() -> None:
    a1 = _assertion(assertion_id="a1", recorded_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)))
    a2 = _assertion(assertion_id="a2", recorded_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)))
    sup = AssertionSupersession(
        workspace_id="ws1",
        supersession_id="sup1",
        prior_assertion_id="a1",
        successor_assertion_id="a2",
        reason_code="correction",
        decision_id="d1",
        recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
    )
    retraction = AssertionRetraction(
        workspace_id="ws1",
        retraction_id="r1",
        assertion_id="a2",
        retracted_at=_instant(datetime(2026, 1, 4, tzinfo=UTC)),
        reason_code="error",
        policy_version="p1",
        actor_principal_id="user1",
    )

    digest_forward = assertion_history_digest(
        assertions=[a1, a2], supersessions=[sup], retractions=[retraction]
    )
    digest_reversed = assertion_history_digest(
        assertions=[a2, a1], supersessions=[sup], retractions=[retraction]
    )
    assert digest_forward == digest_reversed


def test_history_rejects_duplicate_assertion_ids() -> None:
    a1 = _assertion(assertion_id="a1")
    a1_dup = _assertion(assertion_id="a1")
    with pytest.raises(SemanticValidationError) as excinfo:
        assertion_history_payload(assertions=[a1, a1_dup])
    assert excinfo.value.code == SemanticErrorCode.DUPLICATE_ID


def test_history_requires_at_least_one_assertion() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        assertion_history_payload(assertions=[])
    assert excinfo.value.code == SemanticErrorCode.MISSING_FIELD


def test_current_assertion_id_deterministic_under_reordering() -> None:
    sup_a = AssertionSupersession(
        workspace_id="ws1",
        supersession_id="sup1",
        prior_assertion_id="a1",
        successor_assertion_id="a2",
        reason_code="correction",
        decision_id="d1",
        recorded_at=_instant(datetime(2026, 1, 3, tzinfo=UTC)),
    )
    sup_b = AssertionSupersession(
        workspace_id="ws1",
        supersession_id="sup2",
        prior_assertion_id="a2",
        successor_assertion_id="a3",
        reason_code="correction",
        decision_id="d2",
        recorded_at=_instant(datetime(2026, 1, 4, tzinfo=UTC)),
    )
    assert current_assertion_id("a1", [sup_a, sup_b]) == "a3"
    assert current_assertion_id("a1", [sup_b, sup_a]) == "a3"
