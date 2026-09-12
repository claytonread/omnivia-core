"""Canonical bytes/digest tests: NFC, ordering, exclusion, and rejection rules."""

from __future__ import annotations

import math

import pytest

from omnivia_core.semantic_registry.canonical import (
    canonical_bytes,
    change_set_digest,
    content_digest,
    model_version_digest,
    model_version_payload,
    operation_payload,
)
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticRegistryError,
)
from omnivia_core.semantic_registry.models import Concept, ModelVersion, Relationship
from omnivia_core.semantic_registry.operations import add_alias, add_concept

_E_ACUTE_PRECOMPOSED = "é"
_E_ACUTE_DECOMPOSED = "é"


def test_canonical_bytes_is_independent_of_mapping_key_order() -> None:
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


def test_canonical_bytes_folds_composed_and_decomposed_unicode_keys() -> None:
    composed = {_E_ACUTE_PRECOMPOSED: 1}
    decomposed = {_E_ACUTE_DECOMPOSED: 1}
    assert canonical_bytes(composed) == canonical_bytes(decomposed)


def test_canonical_bytes_folds_composed_and_decomposed_unicode_values() -> None:
    assert canonical_bytes({"k": _E_ACUTE_PRECOMPOSED}) == canonical_bytes(
        {"k": _E_ACUTE_DECOMPOSED}
    )


def test_canonical_bytes_rejects_keys_that_collide_after_nfc_normalisation() -> None:
    with pytest.raises(SemanticRegistryError) as excinfo:
        canonical_bytes({_E_ACUTE_PRECOMPOSED: 1, _E_ACUTE_DECOMPOSED: 2})
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_canonical_bytes_rejects_non_string_mapping_keys() -> None:
    with pytest.raises(SemanticRegistryError) as excinfo:
        canonical_bytes({1: "x"})
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_bytes_rejects_non_finite_floats(bad: float) -> None:
    with pytest.raises(SemanticRegistryError) as excinfo:
        canonical_bytes({"x": bad})
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_canonical_bytes_rejects_unsupported_value_types() -> None:
    with pytest.raises(SemanticRegistryError) as excinfo:
        canonical_bytes({"x": {1, 2, 3}})
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_content_digest_is_a_sha256_prefixed_hex_string() -> None:
    digest = content_digest({"a": 1})
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    int(digest.removeprefix("sha256:"), 16)  # must parse as hex


def test_domain_dataclass_canonicalization_is_stable_under_field_order() -> None:
    concept_a = Concept(element_id="c1", label="Widget", classifications=("x", "y"))
    concept_b = Concept(element_id="c1", label="Widget", classifications=("y", "x"))
    assert canonical_bytes(concept_a) == canonical_bytes(concept_b)


def test_set_like_tuple_fields_sort_independent_of_construction_order() -> None:
    relationship_a = Relationship(
        element_id="r1",
        label="rel",
        subject_concept_id="s1",
        object_concept_id="o1",
        characteristics=("transitive", "functional"),
    )
    relationship_b = Relationship(
        element_id="r1",
        label="rel",
        subject_concept_id="s1",
        object_concept_id="o1",
        characteristics=("functional", "transitive"),
    )
    assert canonical_bytes(relationship_a) == canonical_bytes(relationship_b)


def test_list_content_inside_arbitrary_mapping_keeps_its_given_order() -> None:
    ordered = {"values": ["z", "a", "m"]}
    reversed_order = {"values": ["m", "a", "z"]}
    assert canonical_bytes(ordered) != canonical_bytes(reversed_order)
    assert canonical_bytes(ordered) == canonical_bytes({"values": ["z", "a", "m"]})


def _make_version(*, elements: tuple = ()) -> ModelVersion:
    return ModelVersion(
        model_version_id="mv1",
        model_id="m1",
        version_sequence=1,
        version_label="1.0.0",
        content_digest="sha256:" + "0" * 64,
        elements=elements,
    )


def test_semantic_elements_are_ordered_by_element_id_in_the_payload() -> None:
    concept_b = Concept(element_id="b", label="B")
    concept_a = Concept(element_id="a", label="A")
    version = _make_version(elements=(concept_b, concept_a))
    payload = model_version_payload(version)
    assert [element["element_id"] for element in payload["elements"]] == ["a", "b"]


def test_model_version_payload_excludes_its_own_content_digest() -> None:
    version = _make_version()
    payload = model_version_payload(version)
    assert "content_digest" not in payload


def test_model_version_digest_is_independent_of_the_stored_content_digest_value() -> (
    None
):
    version_1 = _make_version()
    version_2 = ModelVersion(
        model_version_id="mv1",
        model_id="m1",
        version_sequence=1,
        version_label="1.0.0",
        content_digest="sha256:" + "f" * 64,
    )
    assert model_version_digest(version_1) == model_version_digest(version_2)


def test_model_version_digest_ignores_version_identity_and_framing() -> None:
    """Same model, same meta-model version, same elements: same digest --
    regardless of version_id, sequence, label or parent lineage (spec 7.5)."""
    concept = Concept(element_id="c1", label="Widget")
    version_1 = ModelVersion(
        model_version_id="mv1",
        model_id="m1",
        version_sequence=1,
        version_label="1.0.0",
        content_digest="sha256:" + "0" * 64,
        elements=(concept,),
    )
    version_2 = ModelVersion(
        model_version_id="mv2",
        model_id="m1",
        version_sequence=2,
        version_label="1.1.0",
        content_digest="sha256:" + "f" * 64,
        parent_version_ids=("mv1",),
        elements=(concept,),
    )
    assert model_version_digest(version_1) == model_version_digest(version_2)


def test_model_version_digest_changes_with_the_model_identity_or_elements() -> None:
    concept = Concept(element_id="c1", label="Widget")
    same_model_other_content = _make_version(elements=(concept,))
    other_model = ModelVersion(
        model_version_id="mv1",
        model_id="m2",
        version_sequence=1,
        version_label="1.0.0",
        content_digest="sha256:" + "0" * 64,
        elements=(concept,),
    )
    assert model_version_digest(_make_version()) != model_version_digest(
        same_model_other_content
    )
    assert model_version_digest(same_model_other_content) != model_version_digest(
        other_model
    )


def test_operation_payload_excludes_rationale() -> None:
    operation_with = add_concept("op1", "c1", {"label": "A"}, rationale="because")
    operation_without = add_concept("op1", "c1", {"label": "A"}, rationale=None)
    assert operation_payload(operation_with) == operation_payload(operation_without)


def test_change_set_digest_is_unaffected_by_rationale() -> None:
    base_operation = add_alias("op1", "a1", {"value": "x", "target_element_id": "c1"})
    other_operation = add_alias(
        "op1", "a1", {"value": "x", "target_element_id": "c1"}, rationale="editorial"
    )
    digest_1 = change_set_digest("mv0", "sha256:" + "0" * 64, (base_operation,))
    digest_2 = change_set_digest("mv0", "sha256:" + "0" * 64, (other_operation,))
    assert digest_1 == digest_2


def test_change_set_digest_changes_when_semantic_content_changes() -> None:
    operation_1 = add_alias("op1", "a1", {"value": "x", "target_element_id": "c1"})
    operation_2 = add_alias("op1", "a1", {"value": "y", "target_element_id": "c1"})
    digest_1 = change_set_digest("mv0", "sha256:" + "0" * 64, (operation_1,))
    digest_2 = change_set_digest("mv0", "sha256:" + "0" * 64, (operation_2,))
    assert digest_1 != digest_2


def test_isfinite_sanity_for_non_finite_test_inputs() -> None:
    assert not math.isfinite(float("nan"))
    assert not math.isfinite(float("inf"))
