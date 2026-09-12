"""Public record invariants (spec section 10: accountability records)."""

from __future__ import annotations

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.operations import add_alias
from omnivia_core.semantic_registry.records import (
    ActivationRecord,
    ChangeRecord,
    DecisionExplanation,
    PublicationRecord,
    ReviewDecision,
    ReviewRecord,
)


def test_publication_record_requires_matching_generation_for_compare_and_swap() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        PublicationRecord(
            publication_id="pub1",
            model_version_id="mv1",
            change_set_digest="sha256:" + "0" * 64,
            approval_digest="sha256:" + "0" * 64,
            validation_digest="sha256:" + "0" * 64,
            actor_id="actor1",
            expected_generation=1,
            current_generation=2,
        )
    assert excinfo.value.code is SemanticErrorCode.STALE_BASE_DIGEST


def test_publication_record_accepts_matching_generation() -> None:
    record = PublicationRecord(
        publication_id="pub1",
        model_version_id="mv1",
        change_set_digest="sha256:" + "0" * 64,
        approval_digest="sha256:" + "0" * 64,
        validation_digest="sha256:" + "0" * 64,
        actor_id="actor1",
        expected_generation=1,
        current_generation=1,
    )
    assert record.expected_generation == record.current_generation


def test_activation_record_cannot_supersede_itself() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ActivationRecord(
            activation_id="act1",
            model_version_id="mv1",
            policy_version="1.0.0",
            actor_id="actor1",
            effective_at="2026-01-01T00:00:00Z",
            previous_activation_id="act1",
        )
    assert excinfo.value.code is SemanticErrorCode.IMMUTABLE_VIOLATION


def test_review_record_requires_a_change_set_digest() -> None:
    with pytest.raises(SemanticValidationError):
        ReviewRecord(
            review_id="rev1",
            change_set_digest="",
            decision=ReviewDecision.APPROVE,
            actor_id="actor1",
            explanation=DecisionExplanation(reason_code="looks_good"),
        )


def test_change_record_rejects_duplicate_operation_ids() -> None:
    op_1 = add_alias("op1", "a1", {"value": "x", "target_element_id": "c1"})
    op_2 = add_alias("op1", "a2", {"value": "y", "target_element_id": "c1"})
    with pytest.raises(SemanticValidationError) as excinfo:
        ChangeRecord(
            change_set_id="cs1",
            base_version_id="mv1",
            base_digest="sha256:" + "0" * 64,
            operations=(op_1, op_2),
        )
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_change_record_requires_a_positive_draft_sequence() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ChangeRecord(
            change_set_id="cs1",
            base_version_id="mv1",
            base_digest="sha256:" + "0" * 64,
            operations=(),
            draft_sequence=0,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD
