"""Deterministic, dependency-aware ordering (spec section 11)."""

from __future__ import annotations

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticConflictError,
    SemanticErrorCode,
)
from omnivia_core.semantic_registry.operations import add_alias, add_concept
from omnivia_core.semantic_registry.ordering import order_operations


def test_order_is_independent_of_input_order() -> None:
    op_a = add_concept("a", "c1", {"label": "A"})
    op_b = add_concept("b", "c2", {"label": "B"})
    op_c = add_alias("c", "al1", {"value": "x", "target_element_id": "c1"})
    assert order_operations((op_a, op_b, op_c)) == order_operations((op_c, op_b, op_a))


def test_dependents_are_ordered_after_their_dependency() -> None:
    dependency = add_concept("base", "c1", {"label": "A"})
    dependent = add_alias(
        "dependent",
        "al1",
        {"value": "x", "target_element_id": "c1"},
        depends_on_operation_ids=("base",),
    )
    ordered = order_operations((dependent, dependency))
    assert [op.operation_id for op in ordered] == ["base", "dependent"]


def test_ties_break_by_kind_then_operation_id() -> None:
    op_1 = add_concept("z", "c1", {"label": "A"})
    op_2 = add_concept("a", "c2", {"label": "B"})
    ordered = order_operations((op_1, op_2))
    assert [op.operation_id for op in ordered] == ["a", "z"]


def test_unknown_dependency_is_rejected() -> None:
    dependent = add_alias(
        "dependent",
        "al1",
        {"value": "x", "target_element_id": "c1"},
        depends_on_operation_ids=("missing",),
    )
    with pytest.raises(SemanticConflictError) as excinfo:
        order_operations((dependent,))
    assert excinfo.value.code is SemanticErrorCode.UNKNOWN_REFERENCE


def test_cyclic_dependency_is_rejected() -> None:
    op_1 = add_alias(
        "a",
        "al1",
        {"value": "x", "target_element_id": "c1"},
        depends_on_operation_ids=("b",),
    )
    op_2 = add_alias(
        "b",
        "al2",
        {"value": "y", "target_element_id": "c1"},
        depends_on_operation_ids=("a",),
    )
    with pytest.raises(SemanticConflictError) as excinfo:
        order_operations((op_1, op_2))
    assert excinfo.value.code is SemanticErrorCode.CYCLIC_DEPENDENCY


def test_duplicate_operation_ids_are_rejected() -> None:
    op_1 = add_concept("dup", "c1", {"label": "A"})
    op_2 = add_concept("dup", "c2", {"label": "B"})
    with pytest.raises(SemanticConflictError) as excinfo:
        order_operations((op_1, op_2))
    assert excinfo.value.code is SemanticErrorCode.DUPLICATE_ID


def test_empty_change_set_orders_to_empty() -> None:
    assert order_operations(()) == ()
