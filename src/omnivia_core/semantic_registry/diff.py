"""Deterministic typed diff classification (spec 7.4, 15.2).

`VersionImpact` mirrors the version-increment table in spec section 7.4
exactly: patch for a non-colliding label/description/alias addition, minor
for a new optional element that preserves existing consumers, major for
removal, replacement, constraint tightening or an incompatible hierarchy/
action-contract change. `classify_operation` is a pure function of one
operation's kind and payload, so ordering the same operations twice always
yields the same classification.

Kinds this table cannot resolve from the operation alone (`ChangeHierarchy`,
`ChangeDomainRange`, `ChangeEquivalence`, `ChangeDisjointness`,
`ChangeActionContract`, `AddConstraint`) fail closed to `MAJOR`: spec 7.6's
own rule for an axiom Core cannot classify is "MUST fail closed", and the
alternative -- guessing compatible -- is exactly the risk a reviewer-facing
classifier exists to avoid.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum

from omnivia_core.semantic_registry.operations import ChangeOperation, OperationKind
from omnivia_core.semantic_registry.records import CompatibilityClassification


class VersionImpact(IntEnum):
    """Ordered so `max()` over a change set yields its overall increment."""

    PATCH = 0
    MINOR = 1
    MAJOR = 2


_PATCH_KINDS = frozenset(
    {
        OperationKind.ADD_ALIAS,
        OperationKind.CHANGE_LABEL,
        OperationKind.CHANGE_DESCRIPTION,
    }
)

_MINOR_KINDS = frozenset(
    {
        OperationKind.ADD_CONCEPT,
        OperationKind.ADD_PROPERTY,
        OperationKind.ADD_RELATIONSHIP,
        OperationKind.ADD_VOCABULARY_MEMBER,
        OperationKind.ADD_ACTION_TYPE,
        OperationKind.DEPRECATE_ELEMENT,
    }
)

_MAJOR_KINDS = frozenset(
    {
        OperationKind.ADD_CONSTRAINT,
        OperationKind.CHANGE_HIERARCHY,
        OperationKind.CHANGE_DOMAIN_RANGE,
        OperationKind.CHANGE_EQUIVALENCE,
        OperationKind.CHANGE_DISJOINTNESS,
        OperationKind.CHANGE_ACTION_CONTRACT,
        OperationKind.REPLACE_ELEMENT,
        OperationKind.REMOVE_ELEMENT,
    }
)


def _cardinality_impact(operation: ChangeOperation) -> VersionImpact:
    """A cardinality change is breaking only if it tightens an existing bound."""
    before = operation.before or {}
    after = operation.after
    before_min, after_min = before.get("min_cardinality"), after.get("min_cardinality")
    before_max, after_max = before.get("max_cardinality"), after.get("max_cardinality")
    tightens_min = after_min is not None and (
        before_min is None or after_min > before_min
    )
    tightens_max = (
        after_max is not None and before_max is not None and after_max < before_max
    )
    return (
        VersionImpact.MAJOR if (tightens_min or tightens_max) else VersionImpact.MINOR
    )


def classify_operation(operation: ChangeOperation) -> VersionImpact:
    """The version-increment class one operation forces (spec 7.4)."""
    if operation.kind in _PATCH_KINDS:
        return VersionImpact.PATCH
    if operation.kind is OperationKind.CHANGE_CARDINALITY:
        return _cardinality_impact(operation)
    if operation.kind in _MINOR_KINDS:
        return VersionImpact.MINOR
    if operation.kind in _MAJOR_KINDS:
        return VersionImpact.MAJOR
    raise AssertionError(
        f"unclassified operation kind: {operation.kind!r}"
    )  # pragma: no cover


def classify_change_set(operations: Sequence[ChangeOperation]) -> VersionImpact:
    """The overall version increment a change set forces: its worst operation."""
    if not operations:
        return VersionImpact.PATCH
    return max(classify_operation(operation) for operation in operations)


def impact_to_compatibility(impact: VersionImpact) -> CompatibilityClassification:
    """Map a version-increment class onto the consumer compatibility vocabulary (spec 15.2)."""
    if impact is VersionImpact.MAJOR:
        return CompatibilityClassification.BREAKING
    return CompatibilityClassification.COMPATIBLE


__all__ = [
    "VersionImpact",
    "classify_change_set",
    "classify_operation",
    "impact_to_compatibility",
]
