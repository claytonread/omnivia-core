"""Review, change, publication and activation records; findings; history.

These are the append-only accountability records spec section 10 describes
separately from published content itself (`review_decisions`,
`change_sets`, `publication_records`, `semantic_version_activations`,
`validation_findings`, `compatibility_assessments`). Keeping them as their
own types -- rather than folding reviewer comments or publication metadata
into `ModelVersion` -- is what keeps them out of the semantic content digest
by construction (spec 7.5: "No publication time, reviewer comment...").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omnivia_core.semantic_registry.errors import SemanticErrorCode, require
from omnivia_core.semantic_registry.operations import ChangeOperation


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


class ReviewDecision(str, Enum):
    """A `review_decisions` decision value (spec 10.7)."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGE = "request_change"


class Severity(str, Enum):
    """A `validation_findings` severity (spec 10.7)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class CompatibilityClassification(str, Enum):
    """The consumer compatibility vocabulary (spec 15.2)."""

    COMPATIBLE = "compatible"
    CONDITIONALLY_COMPATIBLE = "conditionally_compatible"
    MIGRATION_REQUIRED = "migration_required"
    BREAKING = "breaking"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DecisionExplanation:
    """A typed, structured reason for one decision -- never free text alone."""

    reason_code: str
    factors: tuple[str, ...] = ()
    reason_text: str | None = None

    def __post_init__(self) -> None:
        _require_id("reason_code", self.reason_code)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One `review_decisions` row: an accountable judgement on a change set."""

    review_id: str
    change_set_digest: str
    decision: ReviewDecision
    actor_id: str
    explanation: DecisionExplanation

    def __post_init__(self) -> None:
        _require_id("review_id", self.review_id)
        _require_id("change_set_digest", self.change_set_digest)
        _require_id("actor_id", self.actor_id)


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One `change_sets` row: an immutable, ordered proposal against one base."""

    change_set_id: str
    base_version_id: str
    base_digest: str
    operations: tuple[ChangeOperation, ...]
    draft_sequence: int = 1

    def __post_init__(self) -> None:
        _require_id("change_set_id", self.change_set_id)
        _require_id("base_version_id", self.base_version_id)
        _require_id("base_digest", self.base_digest)
        require(
            isinstance(self.draft_sequence, int) and self.draft_sequence >= 1,
            SemanticErrorCode.INVALID_FIELD,
            "draft_sequence must be a positive integer",
        )
        operation_ids = tuple(operation.operation_id for operation in self.operations)
        require(
            len(operation_ids) == len(set(operation_ids)),
            SemanticErrorCode.DUPLICATE_ID,
            "operations must have unique operation_id values",
        )


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """One `publication_records` row: the atomic creation of an immutable version."""

    publication_id: str
    model_version_id: str
    change_set_digest: str
    approval_digest: str
    validation_digest: str
    actor_id: str
    expected_generation: int
    current_generation: int

    def __post_init__(self) -> None:
        for name in (
            "publication_id",
            "model_version_id",
            "change_set_digest",
            "approval_digest",
            "validation_digest",
            "actor_id",
        ):
            _require_id(name, getattr(self, name))
        require(
            self.expected_generation == self.current_generation,
            SemanticErrorCode.STALE_BASE_DIGEST,
            "expected_generation must match current_generation for compare-and-swap",
        )


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    """One `semantic_version_activations` row: append-only, never overwritten."""

    activation_id: str
    model_version_id: str
    policy_version: str
    actor_id: str
    effective_at: str
    previous_activation_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "activation_id",
            "model_version_id",
            "policy_version",
            "actor_id",
            "effective_at",
        ):
            _require_id(name, getattr(self, name))
        require(
            self.activation_id != self.previous_activation_id,
            SemanticErrorCode.IMMUTABLE_VIOLATION,
            "an activation cannot supersede itself",
        )


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One entry in a model's exact-version history."""

    model_version_id: str
    version_sequence: int
    version_label: str
    content_digest: str
    parent_version_ids: tuple[str, ...] = ()
    activation: ActivationRecord | None = None


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One `validation_findings` row, bound to a change-set digest."""

    finding_id: str
    stage: str
    severity: Severity
    code: str
    message: str
    affected_stable_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id("finding_id", self.finding_id)
        _require_id("stage", self.stage)
        _require_id("code", self.code)


@dataclass(frozen=True, slots=True)
class ConsumerImpactFinding:
    """One `compatibility_assessments` outcome for a single consumer."""

    consumer_id: str
    classification: CompatibilityClassification
    affected_element_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_id("consumer_id", self.consumer_id)
        _require_id("reason", self.reason)


__all__ = [
    "ActivationRecord",
    "ChangeRecord",
    "CompatibilityClassification",
    "ConsumerImpactFinding",
    "DecisionExplanation",
    "HistoryEntry",
    "PublicationRecord",
    "ReviewDecision",
    "ReviewRecord",
    "Severity",
    "ValidationFinding",
]
