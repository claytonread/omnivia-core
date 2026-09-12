"""Deterministic, dependency-aware ordering of a change set's operations.

Spec section 11: "Dependencies are explicit through `depends_on_operation_ids`.
Canonical ordering is a deterministic topological sort with operation class
and stable ID as tie-breakers." A plain Kahn's-algorithm topological sort
already produces *a* valid order; picking the lexicographically smallest
`(kind.value, operation_id)` ready node at every step is what makes that
order the *same* order for every caller, independent of input order --
which is exactly what a heap of ready nodes gives for free.

Pure function: it returns a new ordered tuple and never mutates the operations
or any authoritative model.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence

from omnivia_core.semantic_registry.errors import (
    SemanticConflictError,
    SemanticErrorCode,
)
from omnivia_core.semantic_registry.operations import ChangeOperation


def order_operations(
    operations: Sequence[ChangeOperation],
) -> tuple[ChangeOperation, ...]:
    """Return `operations` in canonical dependency-aware order.

    Raises :class:`SemanticConflictError` if an operation depends on an id
    outside `operations`, or if the dependency graph has a cycle.
    """
    by_id = {op.operation_id: op for op in operations}
    if len(by_id) != len(operations):
        raise SemanticConflictError(
            SemanticErrorCode.DUPLICATE_ID,
            "operations must have unique operation_id values",
        )
    for op in operations:
        for dependency_id in op.depends_on_operation_ids:
            if dependency_id not in by_id:
                raise SemanticConflictError(
                    SemanticErrorCode.UNKNOWN_REFERENCE,
                    f"{op.operation_id} depends on unknown operation {dependency_id!r}",
                )

    dependents: dict[str, list[str]] = {op.operation_id: [] for op in operations}
    remaining_deps: dict[str, int] = {}
    for op in operations:
        remaining_deps[op.operation_id] = len(op.depends_on_operation_ids)
        for dependency_id in op.depends_on_operation_ids:
            dependents[dependency_id].append(op.operation_id)

    def sort_key(operation_id: str) -> tuple[str, str]:
        return (by_id[operation_id].kind.value, operation_id)

    ready = [sort_key(op_id) for op_id, count in remaining_deps.items() if count == 0]
    heapq.heapify(ready)

    ordered: list[ChangeOperation] = []
    while ready:
        _, operation_id = heapq.heappop(ready)
        ordered.append(by_id[operation_id])
        for dependent_id in dependents[operation_id]:
            remaining_deps[dependent_id] -= 1
            if remaining_deps[dependent_id] == 0:
                heapq.heappush(ready, sort_key(dependent_id))

    if len(ordered) != len(operations):
        stuck = sorted(op_id for op_id, count in remaining_deps.items() if count > 0)
        raise SemanticConflictError(
            SemanticErrorCode.CYCLIC_DEPENDENCY,
            f"depends_on_operation_ids forms a cycle among {stuck}",
        )
    return tuple(ordered)


__all__ = ["order_operations"]
