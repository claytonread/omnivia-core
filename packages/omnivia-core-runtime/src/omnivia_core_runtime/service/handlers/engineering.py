"""The `engineering.*` / `context.priority.*` refusals
(SPEC-CORE-ENGMEM-001, plan PR-B).

Four of the nine engineering-memory operations are durable in
`handlers.continuity` (session register/append/close, handoff read). The five
here remain the honest `dependency_unavailable` refusals: the preview
projections, the pack builder, the preference store and the review-attestation
path are later P0 packages of the implementation plan and land behind this
registration one vertical at a time.

The refusal is `dependency_unavailable`, the same posture the decision family's
model lifecycle holds until its backend exists (§28.4): an intentionally
unavailable state is correct when the producer is absent, and fabricating a
result would be worse than refusing. No message names caller input; every
message is a frozen module constant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import ERROR_CODE_DEPENDENCY_UNAVAILABLE
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)

_MESSAGE_SEARCH: Final = (
    "engineering search ships contracts first; the preview projection lands in a "
    "later engineering-memory package"
)
_MESSAGE_EXPAND: Final = (
    "engineering expansion ships contracts first; the relation projection lands "
    "in a later engineering-memory package"
)
_MESSAGE_CONTEXT_BUILD: Final = (
    "the engineering context pack ships contracts first; the pack builder lands "
    "in a later engineering-memory package"
)
_MESSAGE_PRIORITY: Final = (
    "context priority ships contracts first; the preference store lands in a "
    "later engineering-memory package"
)
_MESSAGE_REVIEW: Final = (
    "engineering review recording ships contracts first; the attestation "
    "producer lands in a later engineering-memory package"
)


class EngineeringHandlers:
    """The five not-yet-durable engineering-memory operations."""

    def engineering_search(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_SEARCH)

    def engineering_expand(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_EXPAND)

    def engineering_context_build(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_CONTEXT_BUILD)

    def context_priority_set(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_PRIORITY)

    def engineering_review_record(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_REVIEW)
