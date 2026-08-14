"""Generated negative boundary conformance for the provider SPI (V06-8, A9-P5).

This third conformance layer consumes every generated lifecycle sequence and
grades the four reconciled A8/A9 invariants.  It adds no operation, adapter,
storage, migration, run-state API, or network surface.

Two boundary effects are not fields on :class:`HookOutcome`: whether a Core
Workflow Run-state record was written and whether a Core governance decision
was recorded.  They are explicit fields on :class:`BoundaryObservation`, so a
real adapter or a mutation test can report them.  The in-process mock supplies
``False`` at the integration boundary because it structurally owns neither
writer.  The invariant functions themselves never return a hard-coded clean
answer.

Core durable jobs are deliberately not classified as Workflow Run state.  A8
fixes those as distinct concepts; job control is checked by its own invariant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from omnivia_core.agent_host.lifecycle_conformance import (
    LIFECYCLE_SEQUENCES,
    SequenceResult,
    run_lifecycle_sequences,
)
from omnivia_core.agent_host.spi import (
    CORE_JOB_CONTROL_OPERATIONS,
    TURN_SCOPED_HOOKS,
    ApprovalKind,
    Disposition,
    HookOutcome,
    Reason,
    SpiRequest,
    envelope_carries_host_identity,
    envelope_leaks_provenance,
)
from omnivia_core.contracts.v1.generated import OPERATION_CATALOGUE

NO_CORE_RUN_STATE: Final = "no-core-run-state"
NO_JOB_CONTROL_COMPOSITION: Final = "no-job-control-composition"
APPROVAL_IS_NOT_GOVERNANCE: Final = "approval-is-not-governance"
NO_HOST_IDENTITY_IN_METADATA: Final = "no-host-identity-in-metadata"

# The frozen operations that can record a governance decision.  This set is
# derived from the catalogue; no parallel operation vocabulary is maintained.
CORE_GOVERNANCE_OPERATIONS: Final[frozenset[str]] = frozenset(
    operation.name
    for operation in OPERATION_CATALOGUE
    if operation.required_capability.id == "knowledge.govern"
)


class BoundaryConformanceError(ValueError):
    """A malformed observation, result, or report input."""


def _non_empty(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise BoundaryConformanceError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class BoundaryObservation:
    """Everything observed for one generated step at the Core boundary."""

    family: str
    sequence: str
    position: int
    label: str
    request: SpiRequest
    outcome: HookOutcome
    core_run_state_record_written: bool
    core_governance_decision_recorded: bool

    def __post_init__(self) -> None:
        for name in ("family", "sequence", "label"):
            _non_empty(name, getattr(self, name))
        if type(self.position) is not int or self.position < 0:
            raise BoundaryConformanceError("position must be a non-negative integer")
        if not isinstance(self.request, SpiRequest):
            raise BoundaryConformanceError("request must be an SpiRequest")
        if not isinstance(self.outcome, HookOutcome):
            raise BoundaryConformanceError("outcome must be a HookOutcome")
        for name in (
            "core_run_state_record_written",
            "core_governance_decision_recorded",
        ):
            if type(getattr(self, name)) is not bool:
                raise BoundaryConformanceError(f"{name} must be a boolean")

    @property
    def core_operations(self) -> tuple[str, ...]:
        """All operations observed through either SPI outcome channel."""
        operations = list(self.outcome.composed_operations)
        for envelope in self.outcome.nested_envelopes:
            if envelope.operation not in operations:
                operations.append(envelope.operation)
        return tuple(operations)


def observe_sequence(result: SequenceResult) -> tuple[BoundaryObservation, ...]:
    """Build one clean mock-boundary observation per generated step."""
    if not isinstance(result, SequenceResult):
        raise BoundaryConformanceError("result must be a SequenceResult")
    sequence = result.sequence
    return tuple(
        BoundaryObservation(
            family=sequence.family,
            sequence=sequence.name,
            position=position,
            label=step.step.label,
            request=step.step.request,
            outcome=step.outcome,
            # MockProvider has no run-state or governance-decision writer.
            core_run_state_record_written=False,
            core_governance_decision_recorded=False,
        )
        for position, step in enumerate(result.steps)
    )


def _no_core_run_state(observation: BoundaryObservation) -> tuple[str, ...]:
    faults: list[str] = []
    if observation.core_run_state_record_written:
        faults.append("wrote a Core Workflow Run-state record")
    if observation.request.intent.request_core_run_state and (
        observation.outcome.disposition is not Disposition.REFUSED
        or observation.outcome.reason is not Reason.RUN_STATE_REQUESTED
    ):
        faults.append(
            "a request for Core run state was not refused "
            f"{Reason.RUN_STATE_REQUESTED.value!r}"
        )
    return tuple(faults)


def _no_job_control(observation: BoundaryObservation) -> tuple[str, ...]:
    faults = [
        f"composed Core job control {operation!r}"
        for operation in observation.core_operations
        if operation in CORE_JOB_CONTROL_OPERATIONS
    ]
    if observation.outcome.implies_core_job_cancel:
        faults.append("outcome implies Core job cancel")
    if observation.outcome.implies_core_job_retry:
        faults.append("outcome implies Core job retry")
    return tuple(faults)


def _approval_is_not_governance(
    observation: BoundaryObservation,
) -> tuple[str, ...]:
    outcome = observation.outcome
    faults: list[str] = []
    if observation.core_governance_decision_recorded:
        faults.append("recorded a Core governance decision")
    faults.extend(
        f"composed Core governance operation {operation!r}"
        for operation in observation.core_operations
        if operation in CORE_GOVERNANCE_OPERATIONS
    )
    if outcome.disposition is Disposition.ESCALATION_REQUIRED:
        faults.append("runtime approval produced an escalation disposition")
    if outcome.reason is Reason.GOVERNANCE_ESCALATION:
        faults.append("runtime approval produced a governance-escalation reason")
    granted = set(observation.request.granted_capabilities)
    if outcome.capability_expanded:
        faults.append("outcome capabilities exceed the granted set")
    widened = tuple(sorted(set(outcome.granted_capabilities) - granted))
    if widened:
        faults.append(f"runtime approval granted {widened!r}")
    effective = tuple(sorted(set(outcome.effective_capabilities) - granted))
    if effective:
        faults.append(f"runtime approval made {effective!r} effective")
    return tuple(faults)


def _no_host_identity(observation: BoundaryObservation) -> tuple[str, ...]:
    faults: list[str] = []
    for envelope in observation.outcome.nested_envelopes:
        if envelope_carries_host_identity(envelope):
            faults.append(
                f"{envelope.operation!r} metadata declares a host-identity field"
            )
        if envelope_leaks_provenance(envelope, observation.request.provenance):
            faults.append(
                f"{envelope.operation!r} metadata carries an SPI provenance value"
            )
    if observation.outcome.host_identity_in_request_metadata:
        faults.append("outcome reports host identity in request metadata")
    return tuple(faults)


Invariant = tuple[
    str,
    Callable[[BoundaryObservation], bool],
    Callable[[BoundaryObservation], tuple[str, ...]],
]

INVARIANTS: Final[tuple[Invariant, ...]] = (
    (NO_CORE_RUN_STATE, lambda observation: True, _no_core_run_state),
    (
        NO_JOB_CONTROL_COMPOSITION,
        lambda observation: observation.request.hook in TURN_SCOPED_HOOKS,
        _no_job_control,
    ),
    (
        APPROVAL_IS_NOT_GOVERNANCE,
        lambda observation: (
            observation.request.approval_kind is ApprovalKind.RUNTIME_TOOL_APPROVAL
        ),
        _approval_is_not_governance,
    ),
    (NO_HOST_IDENTITY_IN_METADATA, lambda observation: True, _no_host_identity),
)


@dataclass(frozen=True, slots=True)
class BoundaryViolation:
    """One invariant failure with generated sequence and step context."""

    invariant: str
    family: str
    sequence: str
    position: int
    label: str
    detail: str

    def __post_init__(self) -> None:
        for name in ("invariant", "family", "sequence", "label", "detail"):
            _non_empty(name, getattr(self, name))
        if type(self.position) is not int or self.position < 0:
            raise BoundaryConformanceError("position must be a non-negative integer")

    def __str__(self) -> str:
        return (
            f"{self.invariant} @ {self.sequence}[{self.position}] "
            f"{self.label}: {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """One invariant across every observation selected for it."""

    invariant: str
    graded: int
    violations: tuple[BoundaryViolation, ...]

    def __post_init__(self) -> None:
        _non_empty("invariant", self.invariant)
        if type(self.graded) is not int or self.graded < 0:
            raise BoundaryConformanceError("graded must be a non-negative integer")
        if not isinstance(self.violations, Sequence) or isinstance(
            self.violations, str
        ):
            raise BoundaryConformanceError("violations must be a sequence")
        violations = tuple(self.violations)
        if not all(isinstance(item, BoundaryViolation) for item in violations):
            raise BoundaryConformanceError("violations must contain BoundaryViolation")
        object.__setattr__(self, "violations", violations)

    @property
    def passed(self) -> bool:
        return self.graded > 0 and not self.violations


@dataclass(frozen=True, slots=True)
class BoundaryReport:
    """All four invariants over every generated lifecycle sequence."""

    invariants: tuple[InvariantResult, ...]
    observed_sequences: int
    observed_steps: int
    failed_sequences: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.invariants, Sequence) or isinstance(
            self.invariants, str
        ):
            raise BoundaryConformanceError("invariants must be a sequence")
        invariants = tuple(self.invariants)
        if not all(isinstance(item, InvariantResult) for item in invariants):
            raise BoundaryConformanceError("invariants must contain InvariantResult")
        for name in ("observed_sequences", "observed_steps"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise BoundaryConformanceError(f"{name} must be non-negative")
        failed = tuple(self.failed_sequences)
        if not all(isinstance(item, str) and item for item in failed):
            raise BoundaryConformanceError("failed_sequences must contain names")
        object.__setattr__(self, "invariants", invariants)
        object.__setattr__(self, "failed_sequences", failed)

    @property
    def covered_every_sequence(self) -> bool:
        return self.observed_sequences == len(LIFECYCLE_SEQUENCES)

    @property
    def covered_every_invariant(self) -> bool:
        return tuple(item.invariant for item in self.invariants) == tuple(
            name for name, _, _ in INVARIANTS
        )

    @property
    def violations(self) -> tuple[BoundaryViolation, ...]:
        return tuple(
            violation
            for invariant in self.invariants
            for violation in invariant.violations
        )

    @property
    def passed(self) -> bool:
        return (
            self.covered_every_sequence
            and self.covered_every_invariant
            and not self.failed_sequences
            and all(item.passed for item in self.invariants)
        )

    @property
    def detail(self) -> str:
        parts = [str(item) for item in self.violations]
        parts.extend(
            f"{item.invariant}: graded no observation"
            for item in self.invariants
            if item.graded == 0
        )
        if not self.covered_every_sequence:
            parts.append(
                f"sequences: expected {len(LIFECYCLE_SEQUENCES)}, "
                f"observed {self.observed_sequences}"
            )
        if not self.covered_every_invariant:
            parts.append("invariants: the four checks did not all report")
        parts.extend(
            f"{name}: lifecycle grading failed" for name in self.failed_sequences
        )
        return "; ".join(parts)


def evaluate(
    observations: Sequence[BoundaryObservation],
    *,
    observed_sequences: int,
    failed_sequences: Sequence[str] = (),
) -> BoundaryReport:
    """Evaluate the four invariants in fixed order."""
    if type(observed_sequences) is not int or observed_sequences < 0:
        raise BoundaryConformanceError("observed_sequences must be non-negative")
    items = tuple(observations)
    if not all(isinstance(item, BoundaryObservation) for item in items):
        raise BoundaryConformanceError("observations must contain BoundaryObservation")
    results: list[InvariantResult] = []
    for name, applies, check in INVARIANTS:
        selected = tuple(item for item in items if applies(item))
        violations = tuple(
            BoundaryViolation(
                invariant=name,
                family=item.family,
                sequence=item.sequence,
                position=item.position,
                label=item.label,
                detail=detail,
            )
            for item in selected
            for detail in check(item)
        )
        results.append(
            InvariantResult(
                invariant=name,
                graded=len(selected),
                violations=violations,
            )
        )
    return BoundaryReport(
        invariants=tuple(results),
        observed_sequences=observed_sequences,
        observed_steps=len(items),
        failed_sequences=tuple(failed_sequences),
    )


def run_boundary_conformance() -> BoundaryReport:
    """Run and evaluate every generated lifecycle sequence."""
    results = run_lifecycle_sequences()
    observations = tuple(
        observation for result in results for observation in observe_sequence(result)
    )
    return evaluate(
        observations,
        observed_sequences=len(results),
        failed_sequences=tuple(
            result.sequence.name for result in results if not result.passed
        ),
    )


__all__ = [
    "APPROVAL_IS_NOT_GOVERNANCE",
    "CORE_GOVERNANCE_OPERATIONS",
    "INVARIANTS",
    "NO_CORE_RUN_STATE",
    "NO_HOST_IDENTITY_IN_METADATA",
    "NO_JOB_CONTROL_COMPOSITION",
    "BoundaryConformanceError",
    "BoundaryObservation",
    "BoundaryReport",
    "BoundaryViolation",
    "InvariantResult",
    "evaluate",
    "observe_sequence",
    "run_boundary_conformance",
]
