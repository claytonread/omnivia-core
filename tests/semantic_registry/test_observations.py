"""Phase 2 observation domain contract tests (spec section 10.5; decision record section 1-3)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceLink,
    EvidenceSupportRole,
)
from omnivia_core.semantic_registry.observations import (
    ObservationBundle,
    ObservationFeature,
    ObservationGeneration,
    ObservationStatus,
    ObservationValueKind,
    SemanticObservation,
    effective_observation_classification,
    normalise_text,
    observation_bundle_digest,
    observation_bundle_payload,
    observation_digest,
    observation_equivalence_signature,
    observation_feature_digest,
    observation_feature_payload,
    observation_payload,
)
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant(value: datetime) -> TemporalInstant:
    return TemporalInstant(
        value=value,
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )


def _observation(
    observation_id: str = "obs1",
    workspace_id: str = "ws1",
    generation: ObservationGeneration = ObservationGeneration.MANUAL,
    rule_version: str | None = None,
) -> SemanticObservation:
    return SemanticObservation(
        observation_id=observation_id,
        workspace_id=workspace_id,
        kind="employment.title",
        value_kind=ObservationValueKind.TEXT,
        original_form="Senior  Engineer",
        normalized_form="senior engineer",
        proposed_semantic_role="job_title",
        classification=Classification.INTERNAL,
        generation=generation,
        recorded_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        rule_version=rule_version,
    )


def _link(
    observation_id: str = "obs1",
    workspace_id: str = "ws1",
    evidence_id: str = "ev1",
    role: EvidenceSupportRole = EvidenceSupportRole.SUPPORT,
    span_id: str | None = None,
) -> EvidenceLink:
    return EvidenceLink(
        workspace_id=workspace_id,
        observation_id=observation_id,
        evidence_id=evidence_id,
        role=role,
        span_id=span_id,
    )


def _feature(
    observation_id: str = "obs1",
    workspace_id: str = "ws1",
    feature_name: str = "seniority_score",
    value: object = 3,
) -> ObservationFeature:
    return ObservationFeature(
        workspace_id=workspace_id,
        observation_id=observation_id,
        feature_name=feature_name,
        value=value,
        policy_version="policy-v1",
        calculation_version="calc-v1",
    )


# --- SemanticObservation invariants ----------------------------------------------


def test_manual_observation_rejects_rule_version() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _observation(generation=ObservationGeneration.MANUAL, rule_version="r1")
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_rule_generated_observation_requires_rule_version() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _observation(generation=ObservationGeneration.DETERMINISTIC_RULE)
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_observation_cannot_supersede_itself() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        SemanticObservation(
            observation_id="obs1",
            workspace_id="ws1",
            kind="k",
            value_kind=ObservationValueKind.TEXT,
            original_form="a",
            normalized_form="a",
            proposed_semantic_role="role",
            classification=Classification.INTERNAL,
            generation=ObservationGeneration.MANUAL,
            recorded_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
            supersedes_observation_id="obs1",
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_observation_status_defaults_to_recorded() -> None:
    observation = _observation()
    assert observation.status is ObservationStatus.RECORDED


def test_observation_rejects_non_enum_value_kind() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        SemanticObservation(
            observation_id="obs1",
            workspace_id="ws1",
            kind="k",
            value_kind="text",  # type: ignore[arg-type]
            original_form="a",
            normalized_form="a",
            proposed_semantic_role="role",
            classification=Classification.INTERNAL,
            generation=ObservationGeneration.MANUAL,
            recorded_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


# --- normalise_text / equivalence signature --------------------------------------


def test_normalise_text_collapses_whitespace_and_casefolds() -> None:
    assert normalise_text("  Senior   Engineer\n") == "senior engineer"


def test_equivalence_signature_is_deterministic() -> None:
    observation = _observation()
    sig1 = observation_equivalence_signature(observation, normalization_version="n1")
    sig2 = observation_equivalence_signature(observation, normalization_version="n1")
    assert sig1 == sig2


def test_equivalence_signature_never_crosses_workspaces() -> None:
    obs_ws1 = _observation(workspace_id="ws1")
    obs_ws2 = _observation(workspace_id="ws2")
    sig1 = observation_equivalence_signature(obs_ws1, normalization_version="n1")
    sig2 = observation_equivalence_signature(obs_ws2, normalization_version="n1")
    assert sig1 != sig2


def test_equivalence_signature_changes_with_normalization_version() -> None:
    observation = _observation()
    sig1 = observation_equivalence_signature(observation, normalization_version="n1")
    sig2 = observation_equivalence_signature(observation, normalization_version="n2")
    assert sig1 != sig2


def test_equivalence_signature_ignores_original_form_casing_and_spacing() -> None:
    obs_a = dataclasses.replace(_observation(), original_form="  Senior   Engineer ")
    obs_b = dataclasses.replace(_observation(), original_form="senior engineer")
    sig_a = observation_equivalence_signature(obs_a, normalization_version="n1")
    sig_b = observation_equivalence_signature(obs_b, normalization_version="n1")
    assert sig_a == sig_b


# --- ObservationFeature: fixed-point integers, not floats ------------------------


def test_feature_accepts_integer_value() -> None:
    feature = _feature(value=42)
    assert feature.value == 42


@pytest.mark.parametrize("value", [1.5, 0.0, -3.25, float("nan"), float("inf")])
def test_feature_rejects_float_values(value: float) -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _feature(value=value)
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_feature_rejects_float_nested_in_mapping() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _feature(value={"score": 1.5})
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_feature_rejects_float_nested_in_list() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _feature(value=[1, 2, 3.0])
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_feature_requires_string_policy_version_not_float() -> None:
    with pytest.raises(SemanticValidationError):
        ObservationFeature(
            workspace_id="ws1",
            observation_id="obs1",
            feature_name="f1",
            value=1,
            policy_version=1.0,  # type: ignore[arg-type]
            calculation_version="calc-v1",
        )


def test_feature_requires_string_calculation_version_not_float() -> None:
    with pytest.raises(SemanticValidationError):
        ObservationFeature(
            workspace_id="ws1",
            observation_id="obs1",
            feature_name="f1",
            value=1,
            policy_version="policy-v1",
            calculation_version=1.0,  # type: ignore[arg-type]
        )


def test_feature_freezes_nested_mapping_and_list_values() -> None:
    feature = _feature(value={"tags": [1, 2], "nested": {"a": 1}})
    assert feature.value["tags"] == (1, 2)
    assert not hasattr(feature.value["tags"], "append")
    with pytest.raises(TypeError):
        feature.value["nested"]["a"] = 2  # type: ignore[index]


# --- ObservationBundle: workspace mismatch / duplicate keys fail closed ---------


def test_bundle_requires_at_least_one_evidence_link() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(observation=_observation(), evidence_links=())
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_bundle_rejects_evidence_link_workspace_mismatch() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(
            observation=_observation(workspace_id="ws1"),
            evidence_links=(_link(workspace_id="ws2"),),
        )
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


def test_bundle_rejects_evidence_link_observation_id_mismatch() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(
            observation=_observation(observation_id="obs1"),
            evidence_links=(_link(observation_id="other"),),
        )
    assert excinfo.value.code is SemanticErrorCode.UNKNOWN_REFERENCE


def test_bundle_rejects_duplicate_evidence_link_key() -> None:
    link = _link()
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(observation=_observation(), evidence_links=(link, link))
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_bundle_allows_same_evidence_with_different_span_or_role() -> None:
    bundle = ObservationBundle(
        observation=_observation(),
        evidence_links=(
            _link(role=EvidenceSupportRole.SUPPORT),
            _link(role=EvidenceSupportRole.CONTRADICT),
            _link(role=EvidenceSupportRole.SUPPORT, span_id="s1"),
        ),
    )
    assert len(bundle.evidence_links) == 3


def test_bundle_rejects_feature_workspace_mismatch() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(
            observation=_observation(workspace_id="ws1"),
            evidence_links=(_link(workspace_id="ws1"),),
            features=(_feature(workspace_id="ws2"),),
        )
    assert excinfo.value.code is SemanticErrorCode.CROSS_WORKSPACE_ACCESS


def test_bundle_rejects_feature_observation_id_mismatch() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(
            observation=_observation(observation_id="obs1"),
            evidence_links=(_link(observation_id="obs1"),),
            features=(_feature(observation_id="other"),),
        )
    assert excinfo.value.code is SemanticErrorCode.UNKNOWN_REFERENCE


def test_bundle_rejects_duplicate_feature_semantic_key() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ObservationBundle(
            observation=_observation(),
            evidence_links=(_link(),),
            features=(
                _feature(feature_name="seniority_score", value=1),
                _feature(feature_name="seniority_score", value=2),
            ),
        )
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


# --- Contradictory evidence remains linked and visible ---------------------------


def test_bundle_retains_contradictory_evidence_link() -> None:
    bundle = ObservationBundle(
        observation=_observation(),
        evidence_links=(
            _link(evidence_id="ev-support", role=EvidenceSupportRole.SUPPORT),
            _link(evidence_id="ev-contradict", role=EvidenceSupportRole.CONTRADICT),
        ),
    )
    roles = {link.evidence_id: link.role for link in bundle.evidence_links}
    assert roles["ev-support"] is EvidenceSupportRole.SUPPORT
    assert roles["ev-contradict"] is EvidenceSupportRole.CONTRADICT

    payload = observation_bundle_payload(bundle)
    payload_roles = {
        link["evidence_id"]: link["role"] for link in payload["evidence_links"]
    }
    assert payload_roles["ev-contradict"] == "contradict"


# --- Canonical payloads / digests -------------------------------------------------


def test_observation_digest_is_deterministic() -> None:
    observation = _observation()
    assert observation_digest(observation) == observation_digest(observation)


def test_observation_digest_changes_with_normalized_form() -> None:
    obs_a = _observation()
    obs_b = dataclasses.replace(_observation(), normalized_form="junior engineer")
    assert observation_digest(obs_a) != observation_digest(obs_b)


def test_observation_payload_never_embeds_raw_evidence_content() -> None:
    observation = _observation()
    payload = observation_payload(observation)
    assert "evidence" not in payload
    assert "content" not in payload
    for key in payload:
        assert "raw" not in key


def test_feature_digest_is_deterministic() -> None:
    feature = _feature()
    assert observation_feature_digest(feature) == observation_feature_digest(feature)


def test_feature_digest_changes_with_value() -> None:
    feature_a = _feature(value=1)
    feature_b = _feature(value=2)
    assert observation_feature_digest(feature_a) != observation_feature_digest(
        feature_b
    )


def test_feature_payload_never_embeds_raw_evidence_content() -> None:
    feature = _feature()
    payload = observation_feature_payload(feature)
    for key in payload:
        assert "content" not in key and "raw" not in key


# --- Bundle digest/equivalence invariant to input order --------------------------


def test_bundle_digest_is_invariant_to_evidence_link_order() -> None:
    link_a = _link(evidence_id="ev-a", role=EvidenceSupportRole.SUPPORT)
    link_b = _link(evidence_id="ev-b", role=EvidenceSupportRole.CONTRADICT)
    bundle_1 = ObservationBundle(
        observation=_observation(), evidence_links=(link_a, link_b)
    )
    bundle_2 = ObservationBundle(
        observation=_observation(), evidence_links=(link_b, link_a)
    )
    assert observation_bundle_digest(bundle_1) == observation_bundle_digest(bundle_2)


def test_bundle_digest_is_invariant_to_feature_order() -> None:
    feature_a = _feature(feature_name="a", value=1)
    feature_b = _feature(feature_name="b", value=2)
    bundle_1 = ObservationBundle(
        observation=_observation(),
        evidence_links=(_link(),),
        features=(feature_a, feature_b),
    )
    bundle_2 = ObservationBundle(
        observation=_observation(),
        evidence_links=(_link(),),
        features=(feature_b, feature_a),
    )
    assert observation_bundle_digest(bundle_1) == observation_bundle_digest(bundle_2)


def test_bundle_payload_sorts_evidence_links_and_features_deterministically() -> None:
    link_a = _link(evidence_id="ev-a")
    link_b = _link(evidence_id="ev-b")
    feature_a = _feature(feature_name="a", value=1)
    feature_b = _feature(feature_name="b", value=2)
    bundle = ObservationBundle(
        observation=_observation(),
        evidence_links=(link_b, link_a),
        features=(feature_b, feature_a),
    )
    payload = observation_bundle_payload(bundle)
    assert [link["evidence_id"] for link in payload["evidence_links"]] == [
        "ev-a",
        "ev-b",
    ]
    assert [feature["feature_name"] for feature in payload["features"]] == [
        "a",
        "b",
    ]


def test_bundle_digest_changes_when_evidence_link_set_changes() -> None:
    bundle_1 = ObservationBundle(
        observation=_observation(), evidence_links=(_link(evidence_id="ev-a"),)
    )
    bundle_2 = ObservationBundle(
        observation=_observation(), evidence_links=(_link(evidence_id="ev-b"),)
    )
    assert observation_bundle_digest(bundle_1) != observation_bundle_digest(bundle_2)


# --- Effective classification -----------------------------------------------------


def test_effective_observation_classification_folds_in_linked_evidence() -> None:
    result = effective_observation_classification(
        workspace_floor=Classification.PUBLIC,
        source=Classification.PUBLIC,
        observation=_observation(),
        linked_evidence=(Classification.RESTRICTED,),
    )
    assert result is Classification.RESTRICTED


def test_effective_observation_classification_defaults_to_observation_classification() -> (
    None
):
    result = effective_observation_classification(
        workspace_floor=Classification.PUBLIC,
        source=Classification.PUBLIC,
        observation=_observation(),
    )
    assert result is Classification.INTERNAL
