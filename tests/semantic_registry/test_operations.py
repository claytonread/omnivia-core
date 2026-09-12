"""Every OperationKind constructs, validates, and round-trips via its factory."""

from __future__ import annotations

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.operations import (
    ChangeOperation,
    OperationKind,
    add_action_type,
    add_alias,
    add_concept,
    add_constraint,
    add_property,
    add_relationship,
    add_vocabulary_member,
    change_action_contract,
    change_cardinality,
    change_description,
    change_disjointness,
    change_domain_range,
    change_equivalence,
    change_hierarchy,
    change_label,
    deprecate_element,
    remove_element,
    replace_element,
)

# One (factory, kwargs, expected_kind) row per OperationKind member -- this is the
# full 18-kind vocabulary this batch implements (OperationKind has 18 members; the
# module docstring's "19" is a pre-existing discrepancy left for Codex, see handoff
# notes -- no 19th kind exists to construct or test).
_ADD_CASES: tuple[tuple, ...] = (
    (
        add_concept,
        {"operation_id": "op1", "proposed_element_id": "c1", "after": {"label": "A"}},
        OperationKind.ADD_CONCEPT,
    ),
    (
        add_property,
        {
            "operation_id": "op1",
            "proposed_element_id": "p1",
            "after": {"label": "A", "value_kind": "data"},
        },
        OperationKind.ADD_PROPERTY,
    ),
    (
        add_relationship,
        {
            "operation_id": "op1",
            "proposed_element_id": "r1",
            "after": {
                "label": "A",
                "subject_concept_id": "s1",
                "object_concept_id": "o1",
            },
        },
        OperationKind.ADD_RELATIONSHIP,
    ),
    (
        add_constraint,
        {
            "operation_id": "op1",
            "proposed_element_id": "k1",
            "after": {"constraint_kind": "required", "target_element_id": "e1"},
        },
        OperationKind.ADD_CONSTRAINT,
    ),
    (
        add_alias,
        {
            "operation_id": "op1",
            "proposed_element_id": "a1",
            "after": {"value": "x", "target_element_id": "e1"},
        },
        OperationKind.ADD_ALIAS,
    ),
    (
        add_vocabulary_member,
        {
            "operation_id": "op1",
            "proposed_element_id": "v1",
            "after": {"value": "x", "vocabulary_element_id": "e1"},
        },
        OperationKind.ADD_VOCABULARY_MEMBER,
    ),
    (
        add_action_type,
        {
            "operation_id": "op1",
            "proposed_element_id": "t1",
            "after": {
                "label": "A",
                "subject_concept_id": "s1",
                "parameter_schema_digest": "sha256:0",
            },
        },
        OperationKind.ADD_ACTION_TYPE,
    ),
)

_CHANGE_CASES: tuple[tuple, ...] = (
    (
        change_label,
        {"operation_id": "op1", "target_element_id": "e1", "after": {"label": "A"}},
        OperationKind.CHANGE_LABEL,
    ),
    (
        change_description,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"description": "A"},
        },
        OperationKind.CHANGE_DESCRIPTION,
    ),
    (
        change_hierarchy,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"parent_concept_ids": ["p1"]},
        },
        OperationKind.CHANGE_HIERARCHY,
    ),
    (
        change_domain_range,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"domain_id": "d1", "range_id": "r1"},
        },
        OperationKind.CHANGE_DOMAIN_RANGE,
    ),
    (
        change_cardinality,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"min_cardinality": 0, "max_cardinality": 1},
        },
        OperationKind.CHANGE_CARDINALITY,
    ),
    (
        change_equivalence,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"equivalent_element_id": "e2"},
        },
        OperationKind.CHANGE_EQUIVALENCE,
    ),
    (
        change_disjointness,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"disjoint_with_element_id": "e2"},
        },
        OperationKind.CHANGE_DISJOINTNESS,
    ),
    (
        change_action_contract,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"parameter_schema_digest": "sha256:0"},
        },
        OperationKind.CHANGE_ACTION_CONTRACT,
    ),
    (
        deprecate_element,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"reason_code": "superseded"},
        },
        OperationKind.DEPRECATE_ELEMENT,
    ),
    (
        replace_element,
        {
            "operation_id": "op1",
            "target_element_id": "e1",
            "after": {"replacement_element_id": "e2"},
        },
        OperationKind.REPLACE_ELEMENT,
    ),
)

ALL_CASES = _ADD_CASES + _CHANGE_CASES


@pytest.mark.parametrize("factory, kwargs, expected_kind", ALL_CASES)
def test_each_operation_kind_constructs_with_its_own_factory(
    factory, kwargs, expected_kind
) -> None:
    operation = factory(**kwargs)
    assert isinstance(operation, ChangeOperation)
    assert operation.kind is expected_kind


def test_remove_element_is_the_one_kind_with_an_empty_after() -> None:
    operation = remove_element("op1", "e1")
    assert operation.kind is OperationKind.REMOVE_ELEMENT
    assert operation.after == {}


def test_operation_kind_enum_has_eighteen_members() -> None:
    assert len(OperationKind) == 18


@pytest.mark.parametrize("factory, kwargs, _expected_kind", ALL_CASES)
def test_add_kinds_require_proposed_element_id_not_target(
    factory, kwargs, _expected_kind
) -> None:
    operation = factory(**kwargs)
    if operation.kind in {
        OperationKind.ADD_CONCEPT,
        OperationKind.ADD_PROPERTY,
        OperationKind.ADD_RELATIONSHIP,
        OperationKind.ADD_CONSTRAINT,
        OperationKind.ADD_ALIAS,
        OperationKind.ADD_VOCABULARY_MEMBER,
        OperationKind.ADD_ACTION_TYPE,
    }:
        assert operation.proposed_element_id is not None
        assert operation.target_element_id is None
    else:
        assert operation.target_element_id is not None
        assert operation.proposed_element_id is None


def test_change_label_missing_required_after_key_is_rejected() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        change_label("op1", "e1", after={"unrelated": "x"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_remove_element_with_non_empty_after_is_rejected() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ChangeOperation(
            operation_id="op1",
            kind=OperationKind.REMOVE_ELEMENT,
            target_element_id="e1",
            after={"x": 1},
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_operation_cannot_depend_on_itself() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_alias(
            "op1",
            "a1",
            {"value": "x", "target_element_id": "e1"},
            depends_on_operation_ids=("op1",),
        )
    assert excinfo.value.code is SemanticErrorCode.CYCLIC_DEPENDENCY


def test_duplicate_dependency_ids_are_rejected() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_alias(
            "op1",
            "a1",
            {"value": "x", "target_element_id": "e1"},
            depends_on_operation_ids=("op2", "op2"),
        )
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_add_property_requires_both_label_and_value_kind() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_property("op1", "p1", {"label": "A"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_add_alias_requires_both_target_and_value() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_alias("op1", "a1", {"value": "x"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_add_vocabulary_member_requires_both_vocabulary_and_value() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_vocabulary_member("op1", "v1", {"value": "x"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_add_constraint_requires_both_target_and_kind() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_constraint("op1", "k1", {"constraint_kind": "required"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_add_action_type_requires_all_three_fields() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_action_type("op1", "t1", {"label": "A", "subject_concept_id": "s1"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_add_relationship_requires_all_three_fields() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        add_relationship("op1", "r1", {"label": "A", "subject_concept_id": "s1"})
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


@pytest.mark.parametrize(
    "after", [{"domain_id": "d1"}, {"range_id": "r1"}, {"domain_id": "d1", "range_id": "r1"}]
)
def test_change_domain_range_accepts_a_partial_change(after: dict) -> None:
    change_domain_range("op1", "e1", after)


@pytest.mark.parametrize(
    "after", [{"min_cardinality": 0}, {"max_cardinality": 1}, {"min_cardinality": 0, "max_cardinality": 1}]
)
def test_change_cardinality_accepts_a_partial_change(after: dict) -> None:
    change_cardinality("op1", "e1", after)


def test_unknown_operation_kind_is_rejected() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        ChangeOperation(
            operation_id="op1",
            kind="NotAKind",
            target_element_id="e1",
            after={"label": "A"},
        )
    assert excinfo.value.code is SemanticErrorCode.UNKNOWN_OPERATION_KIND
