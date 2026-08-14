"""A9 layer 3: four negative invariants over every generated sequence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from omnivia_core.agent_host import boundary_conformance
from omnivia_core.agent_host.boundary_conformance import (
    APPROVAL_IS_NOT_GOVERNANCE,
    CORE_GOVERNANCE_OPERATIONS,
    INVARIANTS,
    NO_CORE_RUN_STATE,
    NO_HOST_IDENTITY_IN_METADATA,
    NO_JOB_CONTROL_COMPOSITION,
    BoundaryConformanceError,
    BoundaryObservation,
    BoundaryReport,
    InvariantResult,
    evaluate,
    observe_sequence,
    run_boundary_conformance,
)
from omnivia_core.agent_host.lifecycle_conformance import (
    LIFECYCLE_SEQUENCES,
    run_lifecycle_sequences,
)
from omnivia_core.agent_host.mock import MockProvider
from omnivia_core.agent_host.spi import (
    CORE_JOB_CONTROL_OPERATIONS,
    HOST_IDENTITY_FIELD_NAMES,
    RUN_LEVEL_HOOKS,
    TURN_SCOPED_HOOKS,
    ApprovalKind,
    Disposition,
    Hook,
    HookIntent,
    Reason,
    build_nested_envelope,
)
from omnivia_core.contracts.v1.generated import (
    OPERATION_CATALOGUE,
    ClientIdentity,
    PrincipalClaim,
    RequestMetadata,
)

SEQUENCE_RESULTS = run_lifecycle_sequences()
OBSERVATIONS = tuple(
    observation
    for result in SEQUENCE_RESULTS
    for observation in observe_sequence(result)
)
REPORT = run_boundary_conformance()
INVARIANT_NAMES = tuple(name for name, _, _ in INVARIANTS)


def _one(predicate: Callable[[BoundaryObservation], bool]) -> BoundaryObservation:
    return next(item for item in OBSERVATIONS if predicate(item))


def _grade(observation: BoundaryObservation) -> dict[str, tuple[str, ...]]:
    report = evaluate((observation,), observed_sequences=len(LIFECYCLE_SEQUENCES))
    return {
        result.invariant: tuple(item.detail for item in result.violations)
        for result in report.invariants
    }


def test_report_passes_over_every_generated_sequence_and_step() -> None:
    assert REPORT.passed, REPORT.detail
    assert REPORT.violations == ()
    assert REPORT.observed_sequences == len(LIFECYCLE_SEQUENCES)
    assert REPORT.observed_steps == sum(
        len(sequence.steps) for sequence in LIFECYCLE_SEQUENCES
    )
    assert {item.sequence for item in OBSERVATIONS} == {
        sequence.name for sequence in LIFECYCLE_SEQUENCES
    }


def test_exactly_the_four_accepted_invariants_are_graded() -> None:
    assert INVARIANT_NAMES == (
        NO_CORE_RUN_STATE,
        NO_JOB_CONTROL_COMPOSITION,
        APPROVAL_IS_NOT_GOVERNANCE,
        NO_HOST_IDENTITY_IN_METADATA,
    )
    graded = {item.invariant: item.graded for item in REPORT.invariants}
    assert graded[NO_CORE_RUN_STATE] == len(OBSERVATIONS)
    assert graded[NO_HOST_IDENTITY_IN_METADATA] == len(OBSERVATIONS)
    assert graded[NO_JOB_CONTROL_COMPOSITION] == sum(
        item.request.hook in TURN_SCOPED_HOOKS for item in OBSERVATIONS
    )
    assert graded[APPROVAL_IS_NOT_GOVERNANCE] == sum(
        item.request.approval_kind is ApprovalKind.RUNTIME_TOOL_APPROVAL
        for item in OBSERVATIONS
    )
    assert all(item.graded > 0 for item in REPORT.invariants)


def test_governance_operations_are_derived_from_the_frozen_catalogue() -> None:
    expected = {
        operation.name
        for operation in OPERATION_CATALOGUE
        if operation.required_capability.id == "knowledge.govern"
    }
    assert set(CORE_GOVERNANCE_OPERATIONS) == expected
    # Core durable jobs are deliberately not reclassified as Workflow Run state.
    assert CORE_JOB_CONTROL_OPERATIONS
    assert NO_CORE_RUN_STATE != NO_JOB_CONTROL_COMPOSITION


def test_explicit_run_state_observation_fails_closed() -> None:
    observation = OBSERVATIONS[0]
    mutated = replace(observation, core_run_state_record_written=True)
    assert any(
        "Workflow Run-state record" in detail
        for detail in _grade(mutated)[NO_CORE_RUN_STATE]
    )


def test_admitted_request_for_core_run_state_fails_closed() -> None:
    observation = _one(lambda item: item.outcome.disposition is Disposition.ACCEPTED)
    mutated = replace(
        observation,
        request=replace(
            observation.request,
            intent=HookIntent(request_core_run_state=True),
        ),
    )
    assert any(
        Reason.RUN_STATE_REQUESTED.value in detail
        for detail in _grade(mutated)[NO_CORE_RUN_STATE]
    )


@pytest.mark.parametrize("operation", sorted(CORE_JOB_CONTROL_OPERATIONS))
def test_turn_hook_job_control_composition_is_detected(operation: str) -> None:
    observation = _one(
        lambda item: (
            item.request.hook in TURN_SCOPED_HOOKS
            and item.outcome.disposition is Disposition.ACCEPTED
        )
    )
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, composed_operations=(operation,)),
    )
    assert any(
        operation in detail for detail in _grade(mutated)[NO_JOB_CONTROL_COMPOSITION]
    )


def test_job_control_invariant_is_scoped_to_the_turn_scoped_hooks() -> None:
    """The packet scopes this one to turn-scoped hooks, and the selector says so."""
    observation = _one(
        lambda item: (
            item.request.hook in RUN_LEVEL_HOOKS
            and item.outcome.disposition is not Disposition.REFUSED
        )
    )
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, composed_operations=("job.cancel",)),
    )
    report = evaluate((mutated,), observed_sequences=len(LIFECYCLE_SEQUENCES))
    scoped = next(
        item
        for item in report.invariants
        if item.invariant == NO_JOB_CONTROL_COMPOSITION
    )
    assert scoped.graded == 0
    assert not scoped.passed, "an invariant that graded nothing must not pass"


def test_returned_job_handles_are_not_a_job_control_composition() -> None:
    """Handing a handle back is not job control: the host acts on it, not the SPI."""
    observation = _one(
        lambda item: (
            bool(item.outcome.job_handles)
            and item.outcome.hook in {Hook.TURN_COMPLETE, Hook.TURN_CANCEL}
        )
    )
    assert observation.core_operations == ()
    assert not observation.outcome.implies_core_job_cancel
    assert not observation.outcome.implies_core_job_retry
    assert _grade(observation)[NO_JOB_CONTROL_COMPOSITION] == ()


def test_the_mock_owns_neither_writer_the_two_booleans_report() -> None:
    """Why `observe_sequence` may report both effects absent for the mock.

    The two effects are carried on the observation rather than derived from
    `HookOutcome`, so a real adapter can report them. The mock reports them
    absent, and that is only honest because it structurally owns neither writer:
    it exposes no persistence entry point at all, and everything it holds
    between calls is dropped by `reset`. Both facts are checked here rather than
    taken on trust, because an unchecked `False` is how this invariant would go
    quietly vacuous.
    """
    provider = MockProvider()
    for forbidden in ("persist", "save", "checkpoint", "store", "load", "replay"):
        assert not hasattr(provider, forbidden), forbidden
    assert not any(item.core_run_state_record_written for item in OBSERVATIONS)
    assert not any(item.core_governance_decision_recorded for item in OBSERVATIONS)
    # And the mutation the boolean exists to carry is detected, so reporting it
    # absent is a finding rather than an unfalsifiable default.
    for field_name in (
        "core_run_state_record_written",
        "core_governance_decision_recorded",
    ):
        mutated = replace(_approval(), **{field_name: True})
        assert any(_grade(mutated)[name] for name in INVARIANT_NAMES)


def _approval() -> BoundaryObservation:
    return _one(
        lambda item: (
            item.request.approval_kind is ApprovalKind.RUNTIME_TOOL_APPROVAL
            and item.outcome.disposition is Disposition.ACCEPTED
        )
    )


def test_runtime_approval_governance_decision_is_detected() -> None:
    mutated = replace(_approval(), core_governance_decision_recorded=True)
    assert any(
        "governance decision" in detail
        for detail in _grade(mutated)[APPROVAL_IS_NOT_GOVERNANCE]
    )


def test_runtime_approval_governance_operation_is_detected() -> None:
    operation = min(CORE_GOVERNANCE_OPERATIONS)
    observation = _approval()
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, composed_operations=(operation,)),
    )
    assert any(
        operation in detail for detail in _grade(mutated)[APPROVAL_IS_NOT_GOVERNANCE]
    )


def test_runtime_approval_escalating_to_governance_is_detected() -> None:
    """A runtime approval demanding the governance path has become one."""
    observation = _approval()
    mutated = replace(
        observation,
        outcome=replace(
            observation.outcome,
            disposition=Disposition.ESCALATION_REQUIRED,
            reason=Reason.GOVERNANCE_ESCALATION,
        ),
    )
    details = _grade(mutated)[APPROVAL_IS_NOT_GOVERNANCE]
    assert any("escalation disposition" in detail for detail in details)
    assert any("governance-escalation reason" in detail for detail in details)


def test_runtime_approval_effective_set_outrunning_its_grant_is_detected() -> None:
    observation = _approval()
    assert observation.request.granted_capabilities == ()
    mutated = replace(
        observation,
        outcome=replace(
            observation.outcome, effective_capabilities=("memory.write@1.0",)
        ),
    )
    assert mutated.outcome.capability_expanded
    assert any(
        "exceed the granted set" in detail
        for detail in _grade(mutated)[APPROVAL_IS_NOT_GOVERNANCE]
    )


def test_runtime_approval_capability_expansion_is_detected() -> None:
    observation = _approval()
    mutated = replace(
        observation,
        outcome=replace(
            observation.outcome,
            granted_capabilities=("knowledge.govern@1.0",),
            effective_capabilities=("knowledge.govern@1.0",),
        ),
    )
    details = _grade(mutated)[APPROVAL_IS_NOT_GOVERNANCE]
    assert any("granted" in detail for detail in details)
    assert any("effective" in detail for detail in details)


def test_smuggled_host_provenance_value_is_detected() -> None:
    observation = _one(lambda item: bool(item.outcome.nested_envelopes))
    envelope = observation.outcome.nested_envelopes[0]
    leaking = build_nested_envelope(
        envelope.operation,
        request_id=observation.request.provenance.run,
        api_version="1.0",
        client=ClientIdentity(id="boundary-test", version="0.6.8"),
        workspace_id=observation.request.workspace,
        scopes=("memory",),
        purpose=observation.request.purpose,
        required_capabilities=(),
        principal_claim=PrincipalClaim(claimed_principal_id=observation.request.caller),
        deadline_ms=1_000,
    )
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, nested_envelopes=(leaking,)),
    )
    assert any(
        "SPI provenance value" in detail
        for detail in _grade(mutated)[NO_HOST_IDENTITY_IN_METADATA]
    )


def test_smuggled_turn_ordinal_is_detected() -> None:
    """The turn coordinate is a lifecycle coordinate and never travels to Core."""
    observation = _one(
        lambda item: (
            bool(item.outcome.nested_envelopes)
            and item.request.provenance.turn_ordinal is not None
        )
    )
    turn_ordinal = observation.request.provenance.turn_ordinal
    assert turn_ordinal is not None
    envelope = observation.outcome.nested_envelopes[0]
    smuggled = replace(
        envelope,
        metadata=replace(envelope.metadata, idempotency_key=str(turn_ordinal)),
    )
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, nested_envelopes=(smuggled,)),
    )
    assert any(
        "SPI provenance value" in detail
        for detail in _grade(mutated)[NO_HOST_IDENTITY_IN_METADATA]
    )


def test_frozen_metadata_defines_no_host_identity_field() -> None:
    """The field-name half of invariant 4 has no synthetic case, and this is why.

    `RequestMetadata` is frozen and defines none of the host-identity names, so
    an adapter cannot add one. The check stays because a widened record would
    trip it; this is the structural fact that keeps it quiet today.
    """
    assert not {item.name for item in fields(RequestMetadata)} & (
        HOST_IDENTITY_FIELD_NAMES
    )
    assert not any(
        item.outcome.host_identity_in_request_metadata for item in OBSERVATIONS
    )


def test_report_fails_closed_on_empty_short_or_failed_lifecycle_input() -> None:
    empty = evaluate((), observed_sequences=len(LIFECYCLE_SEQUENCES))
    assert not empty.passed
    assert "graded no observation" in empty.detail
    short = evaluate(
        OBSERVATIONS,
        observed_sequences=len(LIFECYCLE_SEQUENCES) - 1,
    )
    assert not short.passed
    failed = evaluate(
        OBSERVATIONS,
        observed_sequences=len(LIFECYCLE_SEQUENCES),
        failed_sequences=(LIFECYCLE_SEQUENCES[0].name,),
    )
    assert not failed.passed
    assert "lifecycle grading failed" in failed.detail


def test_violation_diagnostics_are_deterministic_and_contextual() -> None:
    observation = _one(
        lambda item: (
            item.request.hook in TURN_SCOPED_HOOKS
            and item.outcome.disposition is Disposition.ACCEPTED
        )
    )
    mutated = replace(
        observation,
        outcome=replace(observation.outcome, composed_operations=("job.cancel",)),
    )
    first = evaluate((mutated,), observed_sequences=len(LIFECYCLE_SEQUENCES))
    second = evaluate((mutated,), observed_sequences=len(LIFECYCLE_SEQUENCES))
    assert first == second
    violation = next(
        item
        for item in first.violations
        if item.invariant == NO_JOB_CONTROL_COMPOSITION
    )
    assert violation.family == observation.family
    assert violation.sequence == observation.sequence
    assert violation.position == observation.position
    assert observation.label in first.detail


def test_values_are_immutable_and_malformed_values_are_refused() -> None:
    with pytest.raises(FrozenInstanceError):
        REPORT.observed_steps = 0  # type: ignore[misc]
    with pytest.raises(BoundaryConformanceError):
        observe_sequence("bad")  # type: ignore[arg-type]
    with pytest.raises(BoundaryConformanceError):
        replace(OBSERVATIONS[0], core_run_state_record_written=1)  # type: ignore[arg-type]
    with pytest.raises(BoundaryConformanceError):
        InvariantResult(invariant=NO_CORE_RUN_STATE, graded=-1, violations=())
    with pytest.raises(BoundaryConformanceError):
        BoundaryReport(
            invariants=(),
            observed_sequences=-1,
            observed_steps=0,
            failed_sequences=(),
        )


def test_layer_selects_no_case_or_generated_sequence_name() -> None:
    source = Path(boundary_conformance.__file__).read_text(encoding="utf-8")
    assert not re.search(r"SPI-V-\d", source)
    for sequence in LIFECYCLE_SEQUENCES:
        assert sequence.name not in source
    for forbidden in (
        "load_corpus",
        "import sqlite3",
        "import socket",
        "import urllib",
        ".write_text",
    ):
        assert forbidden not in source


def test_public_surface_exports_only_the_boundary_verdict() -> None:
    from omnivia_core import agent_host

    exported = set(agent_host.__all__)
    assert {
        "run_boundary_conformance",
        "BoundaryReport",
        "InvariantResult",
        "BoundaryConformanceError",
    } <= exported
    assert not {"BoundaryObservation", "evaluate", "observe_sequence"} & exported
    assert isinstance(agent_host.run_boundary_conformance(), BoundaryReport)
