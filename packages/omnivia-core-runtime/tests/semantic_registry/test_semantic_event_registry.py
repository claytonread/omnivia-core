"""Contract coverage for logical labels and canonical semantic wire events."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from omnivia_core_runtime.service.semantic_phase2 import (
    CANDIDATE_CREATED_EVENT,
    CANDIDATE_RECONSIDERED_EVENT,
    CANDIDATE_SUPPRESSED_EVENT,
    OBSERVATION_RECORDED_EVENT,
)
from omnivia_core_runtime.storage.semantic_events import (
    REGISTERED_SEMANTIC_OUTBOX_EVENTS,
    SECTION_19_LOGICAL_EVENTS,
    SEMANTIC_EVENT_LOGICAL_BY_WIRE,
    SEMANTIC_EVENT_WIRE_BY_LOGICAL,
    semantic_event_logical_label,
    semantic_event_wire_value,
)
from omnivia_core_runtime.storage.semantic_registry import PUBLICATION_EVENT_KIND

WIRE_GRAMMAR = re.compile(r"^semantic(?:\.[a-z][a-z0-9]*)+\.v[1-9][0-9]*$")
WIRE_LITERAL = re.compile(r'["\'](semantic(?:\.[a-z][a-z0-9]*)+\.v[1-9][0-9]*)["\']')
RUNTIME_SOURCE = Path(__file__).resolve().parents[2] / "src/omnivia_core_runtime"
EXPECTED_SECTION_19 = {
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


def test_section_19_mapping_is_exhaustive_one_to_one_and_versioned() -> None:
    assert SECTION_19_LOGICAL_EVENTS == EXPECTED_SECTION_19
    assert SECTION_19_LOGICAL_EVENTS <= set(SEMANTIC_EVENT_WIRE_BY_LOGICAL)
    assert len(SEMANTIC_EVENT_WIRE_BY_LOGICAL) == len(
        set(SEMANTIC_EVENT_WIRE_BY_LOGICAL.values())
    )
    assert REGISTERED_SEMANTIC_OUTBOX_EVENTS == set(
        SEMANTIC_EVENT_WIRE_BY_LOGICAL.values()
    )
    assert all(WIRE_GRAMMAR.fullmatch(value) for value in REGISTERED_SEMANTIC_OUTBOX_EVENTS)
    assert all(
        SEMANTIC_EVENT_LOGICAL_BY_WIRE[wire] == logical
        for logical, wire in SEMANTIC_EVENT_WIRE_BY_LOGICAL.items()
    )


def test_runtime_producer_constants_are_derived_from_the_registry() -> None:
    assert OBSERVATION_RECORDED_EVENT == semantic_event_wire_value(
        "SemanticObservationRecorded.v1"
    )
    assert CANDIDATE_CREATED_EVENT == semantic_event_wire_value(
        "SemanticCandidateCreated.v1"
    )
    assert CANDIDATE_SUPPRESSED_EVENT == semantic_event_wire_value(
        "SemanticCandidateSuppressed.v1"
    )
    assert CANDIDATE_RECONSIDERED_EVENT == semantic_event_wire_value(
        "SemanticCandidateReconsidered.v1"
    )
    assert PUBLICATION_EVENT_KIND == semantic_event_wire_value(
        "SemanticVersionPublished.v1"
    )


def test_runtime_has_no_versioned_semantic_wire_literal_outside_the_registry() -> None:
    discovered = {
        match.group(1)
        for path in RUNTIME_SOURCE.rglob("*.py")
        for match in WIRE_LITERAL.finditer(path.read_text(encoding="utf-8"))
    }
    assert discovered <= REGISTERED_SEMANTIC_OUTBOX_EVENTS


@pytest.mark.parametrize(
    "wire_value",
    [
        "semantic.version.published",
        "semantic.version.published.v2",
        "SemanticVersionPublished.v1",
    ],
)
def test_unregistered_aliases_and_versions_fail_closed(wire_value: str) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        semantic_event_logical_label(wire_value)
