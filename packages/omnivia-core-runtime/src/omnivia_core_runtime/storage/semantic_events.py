"""Canonical logical-to-wire registry for Semantic Model outbox events.

PascalCase names are specification labels. Lowercase dotted values are the only
wire representation. Dispatch and compatibility checks use this explicit table;
they never derive one representation from the other at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

SECTION_19_LOGICAL_EVENTS: Final = frozenset(
    {
        "SemanticModelCreated.v1",
        "SemanticObservationRecorded.v1",
        "SemanticCandidateCreated.v1",
        "SemanticCandidateSuppressed.v1",
        "SemanticChangeProposed.v1",
        "ChangeSetValidationCompleted.v1",
        "SemanticReviewRequested.v1",
        "SemanticChangeApproved.v1",
        "SemanticChangeRejected.v1",
        "SemanticApprovalInvalidated.v1",
        "SemanticVersionPublished.v1",
        "SemanticVersionActivated.v1",
        "ConsumerBindingChanged.v1",
        "ProjectionRebuildCompleted.v1",
        "ProjectionRebuildFailed.v1",
        "MigrationCompleted.v1",
        "MigrationFailed.v1",
        "KnowledgeAssertionRetracted.v1",
        "MutationAutomationConfigured.v1",
        "AutomatedMutationEvaluated.v1",
        "AutomatedMutationApplied.v1",
        "AutomatedMutationProposed.v1",
        "AutomatedMutationReverted.v1",
        "MutationAutomationFuseTripped.v1",
    }
)

# Candidate reconsideration is a reviewed Phase 2 runtime event outside the initial
# Section 19 list. Keeping it in the same registry makes the reverse mapping total
# over every semantic outbox value the current runtime can emit.
_EVENT_PAIRS: Final = (
    ("SemanticModelCreated.v1", "semantic.model.created.v1"),
    ("SemanticObservationRecorded.v1", "semantic.observation.recorded.v1"),
    ("SemanticCandidateCreated.v1", "semantic.candidate.created.v1"),
    ("SemanticCandidateSuppressed.v1", "semantic.candidate.suppressed.v1"),
    ("SemanticCandidateReconsidered.v1", "semantic.candidate.reconsidered.v1"),
    ("SemanticChangeProposed.v1", "semantic.change.proposed.v1"),
    ("ChangeSetValidationCompleted.v1", "semantic.change.set.validation.completed.v1"),
    ("SemanticReviewRequested.v1", "semantic.review.requested.v1"),
    ("SemanticChangeApproved.v1", "semantic.change.approved.v1"),
    ("SemanticChangeRejected.v1", "semantic.change.rejected.v1"),
    ("SemanticApprovalInvalidated.v1", "semantic.approval.invalidated.v1"),
    ("SemanticVersionPublished.v1", "semantic.version.published.v1"),
    ("SemanticVersionActivated.v1", "semantic.version.activated.v1"),
    ("ConsumerBindingChanged.v1", "semantic.consumer.binding.changed.v1"),
    ("ProjectionRebuildCompleted.v1", "semantic.projection.rebuild.completed.v1"),
    ("ProjectionRebuildFailed.v1", "semantic.projection.rebuild.failed.v1"),
    ("MigrationCompleted.v1", "semantic.migration.completed.v1"),
    ("MigrationFailed.v1", "semantic.migration.failed.v1"),
    ("KnowledgeAssertionRetracted.v1", "semantic.knowledge.assertion.retracted.v1"),
    ("MutationAutomationConfigured.v1", "semantic.mutation.automation.configured.v1"),
    ("AutomatedMutationEvaluated.v1", "semantic.automated.mutation.evaluated.v1"),
    ("AutomatedMutationApplied.v1", "semantic.automated.mutation.applied.v1"),
    ("AutomatedMutationProposed.v1", "semantic.automated.mutation.proposed.v1"),
    ("AutomatedMutationReverted.v1", "semantic.automated.mutation.reverted.v1"),
    (
        "MutationAutomationFuseTripped.v1",
        "semantic.mutation.automation.fuse.tripped.v1",
    ),
)

SEMANTIC_EVENT_WIRE_BY_LOGICAL: Final[Mapping[str, str]] = MappingProxyType(
    dict(_EVENT_PAIRS)
)
SEMANTIC_EVENT_LOGICAL_BY_WIRE: Final[Mapping[str, str]] = MappingProxyType(
    {wire: logical for logical, wire in _EVENT_PAIRS}
)
REGISTERED_SEMANTIC_OUTBOX_EVENTS: Final = frozenset(
    SEMANTIC_EVENT_LOGICAL_BY_WIRE
)

SEMANTIC_OBSERVATION_RECORDED_V1: Final = SEMANTIC_EVENT_WIRE_BY_LOGICAL[
    "SemanticObservationRecorded.v1"
]
SEMANTIC_CANDIDATE_CREATED_V1: Final = SEMANTIC_EVENT_WIRE_BY_LOGICAL[
    "SemanticCandidateCreated.v1"
]
SEMANTIC_CANDIDATE_SUPPRESSED_V1: Final = SEMANTIC_EVENT_WIRE_BY_LOGICAL[
    "SemanticCandidateSuppressed.v1"
]
SEMANTIC_CANDIDATE_RECONSIDERED_V1: Final = SEMANTIC_EVENT_WIRE_BY_LOGICAL[
    "SemanticCandidateReconsidered.v1"
]
SEMANTIC_VERSION_PUBLISHED_V1: Final = SEMANTIC_EVENT_WIRE_BY_LOGICAL[
    "SemanticVersionPublished.v1"
]


def semantic_event_wire_value(logical_label: str) -> str:
    """Resolve one registered specification label, failing closed on aliases."""
    try:
        return SEMANTIC_EVENT_WIRE_BY_LOGICAL[logical_label]
    except KeyError as error:
        raise ValueError("unsupported semantic event logical label") from error


def semantic_event_logical_label(wire_value: str) -> str:
    """Resolve one registered wire value, failing closed on versions and aliases."""
    try:
        return SEMANTIC_EVENT_LOGICAL_BY_WIRE[wire_value]
    except KeyError as error:
        raise ValueError("unsupported semantic event wire value") from error


__all__ = [
    "REGISTERED_SEMANTIC_OUTBOX_EVENTS",
    "SECTION_19_LOGICAL_EVENTS",
    "SEMANTIC_CANDIDATE_CREATED_V1",
    "SEMANTIC_CANDIDATE_RECONSIDERED_V1",
    "SEMANTIC_CANDIDATE_SUPPRESSED_V1",
    "SEMANTIC_EVENT_LOGICAL_BY_WIRE",
    "SEMANTIC_EVENT_WIRE_BY_LOGICAL",
    "SEMANTIC_OBSERVATION_RECORDED_V1",
    "SEMANTIC_VERSION_PUBLISHED_V1",
    "semantic_event_logical_label",
    "semantic_event_wire_value",
]
