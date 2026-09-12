"""Executable acceptance coverage for the Phase 2 deterministic aggregation rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from omnivia_core.semantic_registry import (
    AGGREGATION_RULE_VERSION,
    NORMALIZATION_RULE_VERSION,
    CandidateState,
    CandidateSuppression,
    Classification,
    Concept,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSupportRole,
    ModelVersion,
    ObservationBundle,
    ObservationGeneration,
    ObservationValueKind,
    ReconsiderationReason,
    SemanticObservation,
    SemanticValidationError,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
    add_concept,
    build_candidate,
    build_reconsideration,
    candidate_bundle_digest,
    decide_suppression,
    deduplicate_evidence,
    evidence_snapshot_digest,
    group_equivalent_observations,
    normalize_identifier,
    normalize_text,
    require_supported_versions,
)

WORKSPACE = "ws-aggregation"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def instant(offset: int = 0) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )


def source(source_id: str = "source-1") -> EvidenceSource:
    return EvidenceSource(
        source_id=source_id,
        kind=EvidenceSourceKind.DOCUMENT,
        locator_scheme=EvidenceLocatorScheme.HTTPS,
        locator=f"https://example.test/{source_id}",
        version="v1",
    )


def evidence(
    evidence_id: str,
    *,
    source_id: str = "source-1",
    digest: str = DIGEST_A,
    workspace_id: str = WORKSPACE,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        source=source(source_id),
        content_ref=f"blob://{evidence_id}",
        content_digest=digest,
        integrity_digest=digest,
        mime_type="text/plain",
        classification=Classification.INTERNAL,
        retention_class="standard",
        captured_at=instant(),
    )


def observation(observation_id: str, original: str = "Caf\u00e9") -> SemanticObservation:
    return SemanticObservation(
        observation_id=observation_id,
        workspace_id=WORKSPACE,
        kind="concept.label",
        value_kind=ObservationValueKind.TEXT,
        original_form=original,
        normalized_form=normalize_text(original),
        proposed_semantic_role="concept",
        classification=Classification.INTERNAL,
        generation=ObservationGeneration.MANUAL,
        recorded_at=instant(),
    )


def bundle(
    observation_id: str,
    evidence_id: str,
    *,
    role: EvidenceSupportRole = EvidenceSupportRole.SUPPORT,
    original: str = "Caf\u00e9",
) -> ObservationBundle:
    return ObservationBundle(
        observation=observation(observation_id, original),
        evidence_links=(
            EvidenceLink(
                workspace_id=WORKSPACE,
                observation_id=observation_id,
                evidence_id=evidence_id,
                role=role,
            ),
        ),
    )


def base() -> ModelVersion:
    return ModelVersion(
        model_version_id="version-1",
        model_id="model-1",
        version_sequence=1,
        version_label="1.0.0",
        content_digest=DIGEST_A,
        elements=(Concept(element_id="existing", label="Existing"),),
    )


def suppression(*, expires_at: TemporalInstant | None = None) -> CandidateSuppression:
    return CandidateSuppression(
        workspace_id=WORKSPACE,
        suppression_id="suppression-1",
        equivalence_signature=DIGEST_A,
        rejection_ref="candidate-rejected",
        suppression_rule_version="candidate-suppression-v1",
        created_at=instant(),
        evidence_snapshot_digest=DIGEST_A,
        aggregation_version=AGGREGATION_RULE_VERSION,
        expires_at=expires_at,
    )


def test_normalization_is_versioned_unicode_stable_and_fail_closed() -> None:
    assert normalize_text("  CAFE\u0301  Team ") == "caf\u00e9 team"
    assert normalize_identifier(" Org__Unit--North ") == "org-unit-north"
    require_supported_versions(AGGREGATION_RULE_VERSION, NORMALIZATION_RULE_VERSION)
    with pytest.raises(SemanticValidationError):
        normalize_text("value", "future-normalizer")
    with pytest.raises(SemanticValidationError):
        require_supported_versions("future-aggregator", NORMALIZATION_RULE_VERSION)


def test_evidence_dedup_is_order_invariant_and_workspace_scoped() -> None:
    duplicate_high = evidence("ev-z")
    duplicate_low = evidence("ev-a")
    other_workspace = evidence("ev-other", workspace_id="ws-other")
    forward = deduplicate_evidence((duplicate_high, other_workspace, duplicate_low))
    reverse = deduplicate_evidence((duplicate_low, other_workspace, duplicate_high))
    assert tuple(item.evidence_id for item in forward) == ("ev-a", "ev-other")
    assert forward == reverse
    assert evidence_snapshot_digest(forward) == evidence_snapshot_digest(reverse)


def test_observation_equivalence_normalizes_unicode_and_input_order() -> None:
    composed = observation("obs-b", "Caf\u00e9")
    decomposed = observation("obs-a", " CAFE\u0301 ")
    forward = group_equivalent_observations((composed, decomposed))
    reverse = group_equivalent_observations((decomposed, composed))
    assert forward == reverse
    assert len(forward) == 1
    assert tuple(value.observation_id for value in forward[0][1]) == ("obs-a", "obs-b")


def test_candidate_aggregation_is_replayable_and_keeps_contradictions() -> None:
    evidence_by_id = {
        "ev-1": evidence("ev-1", source_id="source-1", digest=DIGEST_A),
        "ev-2": evidence("ev-2", source_id="source-2", digest=DIGEST_B),
    }
    bundles = (
        bundle("obs-support", "ev-1"),
        bundle("obs-contradict", "ev-2", role=EvidenceSupportRole.CONTRADICT),
    )
    operation = add_concept("operation-1", "new-concept", {"label": "Cafe"})
    first = build_candidate(
        candidate_id="candidate-1",
        candidate_kind="Ontology Change",
        workspace_id=WORKSPACE,
        base=base(),
        proposed_operation=operation,
        bundles=bundles,
        evidence=evidence_by_id,
        created_at=instant(1),
    )
    second = build_candidate(
        candidate_id="candidate-1",
        candidate_kind="Ontology Change",
        workspace_id=WORKSPACE,
        base=base(),
        proposed_operation=operation,
        bundles=tuple(reversed(bundles)),
        evidence=dict(reversed(tuple(evidence_by_id.items()))),
        created_at=instant(1),
    )
    assert first == second
    assert first.candidate.state is CandidateState.DRAFT
    assert first.candidate.base_version_id == "version-1"
    assert first.features.independent_evidence_count == 2
    assert first.features.independent_source_count == 2
    assert {item.role.value for item in first.contributions} == {"novelty", "contradict"}
    assert candidate_bundle_digest(first.candidate, first.contributions) == (
        candidate_bundle_digest(second.candidate, second.contributions)
    )


@pytest.mark.parametrize(
    ("at", "evidence_digest", "rule_version", "reason"),
    [
        (instant(1), DIGEST_B, AGGREGATION_RULE_VERSION, ReconsiderationReason.NEW_EVIDENCE),
        (instant(1), DIGEST_A, "candidate-aggregation-v2", ReconsiderationReason.RULE_VERSION_CHANGED),
        (instant(2), DIGEST_A, AGGREGATION_RULE_VERSION, ReconsiderationReason.EXPIRED),
    ],
)
def test_suppression_ends_only_on_recorded_machine_conditions(
    at: TemporalInstant,
    evidence_digest: str,
    rule_version: str,
    reason: ReconsiderationReason,
) -> None:
    record = suppression(expires_at=instant(2))
    decision = decide_suppression(
        DIGEST_A,
        (record,),
        at=at,
        evidence_snapshot_digest=evidence_digest,
        aggregation_version=rule_version,
    )
    assert not decision.suppressed
    assert decision.reason is reason
    receipt = build_reconsideration(
        reconsideration_id=f"reconsider-{reason.value}",
        suppression=record,
        decision=decision,
        recorded_at=at,
        evidence_snapshot_digest=evidence_digest,
        aggregation_version=rule_version,
    )
    assert receipt.reason is reason


def test_unchanged_suppression_stays_active_and_human_override_is_explicit() -> None:
    record = suppression(expires_at=instant(2))
    decision = decide_suppression(
        DIGEST_A,
        (record,),
        at=instant(1),
        evidence_snapshot_digest=DIGEST_A,
    )
    assert decision.suppressed
    assert decision.reason is None
    with pytest.raises(SemanticValidationError):
        build_reconsideration(
            reconsideration_id="implicit-human-override",
            suppression=record,
            decision=replace(decision, suppressed=False, reason=ReconsiderationReason.HUMAN_OVERRIDE),
            recorded_at=instant(1),
            evidence_snapshot_digest=DIGEST_A,
        )
