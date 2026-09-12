"""Version-impact classification (spec 7.4, 15.2)."""

from __future__ import annotations

from omnivia_core.semantic_registry.diff import (
    VersionImpact,
    classify_change_set,
    classify_operation,
    impact_to_compatibility,
)
from omnivia_core.semantic_registry.operations import (
    add_alias,
    add_concept,
    change_cardinality,
    remove_element,
)
from omnivia_core.semantic_registry.records import CompatibilityClassification


def test_alias_addition_classifies_patch_and_compatible() -> None:
    operation = add_alias("op1", "a1", {"value": "x", "target_element_id": "c1"})
    impact = classify_operation(operation)
    assert impact is VersionImpact.PATCH
    assert impact_to_compatibility(impact) is CompatibilityClassification.COMPATIBLE


def test_new_optional_element_classifies_minor() -> None:
    operation = add_concept("op1", "c1", {"label": "A"})
    assert classify_operation(operation) is VersionImpact.MINOR


def test_tightened_min_cardinality_classifies_major_and_breaking() -> None:
    operation = change_cardinality(
        "op1",
        "e1",
        before={"min_cardinality": 0, "max_cardinality": None},
        after={"min_cardinality": 1, "max_cardinality": None},
    )
    impact = classify_operation(operation)
    assert impact is VersionImpact.MAJOR
    assert impact_to_compatibility(impact) is CompatibilityClassification.BREAKING


def test_tightened_max_cardinality_classifies_major() -> None:
    operation = change_cardinality(
        "op1",
        "e1",
        before={"min_cardinality": None, "max_cardinality": 5},
        after={"min_cardinality": None, "max_cardinality": 1},
    )
    assert classify_operation(operation) is VersionImpact.MAJOR


def test_widened_cardinality_classifies_minor_not_major() -> None:
    operation = change_cardinality(
        "op1",
        "e1",
        before={"min_cardinality": 1, "max_cardinality": 1},
        after={"min_cardinality": 0, "max_cardinality": 5},
    )
    assert classify_operation(operation) is VersionImpact.MINOR


def test_removal_classifies_major_and_breaking() -> None:
    operation = remove_element("op1", "e1")
    impact = classify_operation(operation)
    assert impact is VersionImpact.MAJOR
    assert impact_to_compatibility(impact) is CompatibilityClassification.BREAKING


def test_change_set_impact_is_the_worst_operation() -> None:
    patch_op = add_alias("op1", "a1", {"value": "x", "target_element_id": "c1"})
    major_op = remove_element("op2", "e1")
    assert classify_change_set((patch_op, major_op)) is VersionImpact.MAJOR


def test_empty_change_set_classifies_patch() -> None:
    assert classify_change_set(()) is VersionImpact.PATCH
