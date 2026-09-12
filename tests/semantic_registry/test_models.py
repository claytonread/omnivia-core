"""Semantic model/element/version construction invariants."""

from __future__ import annotations

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.models import Concept, ModelVersion, Relationship


def test_concept_cannot_be_its_own_parent() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        Concept(element_id="c1", label="A", parent_concept_ids=("c1",))
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_concept_rejects_duplicate_parent_ids() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        Concept(element_id="c1", label="A", parent_concept_ids=("p1", "p1"))
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_relationship_requires_subject_and_object() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        Relationship(
            element_id="r1", label="rel", subject_concept_id="", object_concept_id="o1"
        )
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_model_version_requires_major_minor_patch_label() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ModelVersion(
            model_version_id="mv1",
            model_id="m1",
            version_sequence=1,
            version_label="1.0",
            content_digest="sha256:" + "0" * 64,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_model_version_cannot_be_its_own_parent() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ModelVersion(
            model_version_id="mv1",
            model_id="m1",
            version_sequence=1,
            version_label="1.0.0",
            content_digest="sha256:" + "0" * 64,
            parent_version_ids=("mv1",),
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_model_version_rejects_duplicate_element_ids() -> None:
    concept_1 = Concept(element_id="dup", label="A")
    concept_2 = Concept(element_id="dup", label="B")
    with pytest.raises(SemanticValidationError) as excinfo:
        ModelVersion(
            model_version_id="mv1",
            model_id="m1",
            version_sequence=1,
            version_label="1.0.0",
            content_digest="sha256:" + "0" * 64,
            elements=(concept_1, concept_2),
        )
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID
