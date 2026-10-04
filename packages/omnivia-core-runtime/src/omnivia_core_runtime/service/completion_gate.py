"""Runtime-owned completion gate for a run's final step (DEV-REQ-137).

Accepts completion only from a decision the Runtime makes itself. Provider success, transport
status, artefact existence and a caller's `result_kind` are not inputs, and nothing here reads them.
The decision needs three things, all supplied by the composition rather than by the caller of
`RuntimeScheduler.complete`:

* an :class:`AcceptedCompletion`, the exact criteria and identities the run must prove, which the
  Runtime holds for that run;
* an :class:`EvidenceReadout` from an :class:`IndependentEvidenceReader`, the boundary that collects
  evidence from outside the implementer's own work; and
* the claim the final step is being settled under.

:func:`decide_completion` is the pure rule. :func:`settle_completion` reads, decides and persists
the decision, and is called inside the scheduler's fenced transaction, so a refusal rolls back the
whole final settlement and the claim stays open for a later, valid proof.

Absence fails closed everywhere: no gate, no accepted criteria, an unavailable reader, a reader that
states an incomplete observation, a stale fence, any identity that does not agree exactly, a criterion
that is missing, extra, duplicated or not proven, and evidence whose collector or reviewer is not
independent of the implementer. The one exception is :class:`SelfEvidenceException`: a single
criterion the policy has explicitly allowed the implementer to collect, attributed to the actor who
allowed it. The reviewer must still be independent, and the attribution is recorded on the decision.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from omnivia_core.contracts.v1 import is_identifier
from omnivia_core_runtime.storage.completion_decisions import (
    CompletionDecision,
    EvidenceReference,
    StoredCompletionDecision,
    record_decision,
)
from omnivia_core_runtime.storage.connection import StorageError

REFUSED_NO_GATE: Final = "no_completion_gate"
REFUSED_NO_CRITERIA: Final = "no_accepted_criteria"
REFUSED_UNAVAILABLE: Final = "evidence_unavailable"
REFUSED_STALE_FENCE: Final = "stale_fence"
REFUSED_INCOMPLETE: Final = "incomplete_observation"
REFUSED_IDENTITY: Final = "identity_mismatch"
REFUSED_CRITERIA: Final = "criteria_mismatch"
REFUSED_UNPROVEN: Final = "criterion_unproven"
REFUSED_MALFORMED: Final = "malformed_evidence"
REFUSED_NON_INDEPENDENT: Final = "non_independent_evidence"
REFUSED_IMPLEMENTER_SELF: Final = "implementer_self_evidence"

OUTCOME_PROVEN: Final = "proven"
OUTCOMES: Final = frozenset({OUTCOME_PROVEN, "failed", "absent"})


class CompletionRefused(StorageError):
    """A final completion was refused, and the whole final settlement rolled back.

    `reason` is one of the ``REFUSED_*`` names. It is a refusal about authority, not corrupt history,
    so a caller can retry later with a valid proof against the same open claim.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{message} ({reason})")
        self.reason = reason


class CompletionEvidenceUnavailable(Exception):
    """The evidence boundary could not reach its evidence. Raised by a reader, never by the gate."""


@dataclass(frozen=True, slots=True)
class SelfEvidenceException:
    """The one criterion whose evidence the implementer may collect, and who allowed it."""

    criterion: str
    attributed_to: str


@dataclass(frozen=True, slots=True)
class AcceptedCompletion:
    """The exact definition a run's completion is accepted against.

    `criteria` is the complete set of criterion names. The identities are the immutable ones the
    evidence must agree with exactly, and `definition_digest` is the accepted definition's own hash.
    """

    run_id: str
    candidate_id: str
    binding_id: str
    definition_digest: str
    aggregate_id: str
    package_id: str
    application_id: str
    implementer_id: str
    criteria: tuple[str, ...]
    self_evidence: SelfEvidenceException | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.run_id,
            self.candidate_id,
            self.binding_id,
            self.aggregate_id,
            self.package_id,
            self.application_id,
            self.implementer_id,
        )
        if not all(is_identifier(value) for value in identifiers):
            raise ValueError("accepted completion identities are outside their closed shape")
        if not _is_digest(self.definition_digest):
            raise ValueError("accepted definition_digest is outside its closed shape")
        if not self.criteria or list(self.criteria) != sorted(set(self.criteria)):
            raise ValueError("accepted criteria must be non-empty, sorted and unique")
        if not all(is_identifier(name) for name in self.criteria):
            raise ValueError("accepted criterion names are outside their closed shape")
        if self.self_evidence is not None and (
            self.self_evidence.criterion not in self.criteria
            or not is_identifier(self.self_evidence.attributed_to)
        ):
            raise ValueError("a self-evidence exception must name an accepted criterion")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One criterion's observed outcome, with the raw evidence identity and its two parties."""

    criterion: str
    outcome: str
    evidence_id: str
    content_digest: str
    collected_by: str
    reviewed_by: str


@dataclass(frozen=True, slots=True)
class EvidenceReadout:
    """What the independent boundary observed for one run, stated against its exact identities.

    `workspace_id` and `run_id` are stated by the boundary, not supplied by the caller, and are
    checked against the settlement's own. `complete` is false for a partial observation. Identity
    fields are `None` when the boundary cannot state them, which is refused rather than defaulted.
    """

    workspace_id: str
    run_id: str
    candidate_id: str | None
    binding_id: str | None
    definition_digest: str | None
    aggregate_id: str | None
    package_id: str | None
    application_id: str | None
    fencing_generation: int
    complete: bool
    items: tuple[EvidenceItem, ...]


class IndependentEvidenceReader(Protocol):
    """The boundary that collects evidence for a run from outside the implementer's work.

    It is given the connection so it can read what it needs inside the settlement's fence, and it must
    only read. It raises :class:`CompletionEvidenceUnavailable` when it cannot reach its evidence.
    """

    def read(
        self, connection: sqlite3.Connection, *, workspace_id: str, run_id: str
    ) -> EvidenceReadout: ...


@dataclass(frozen=True, slots=True)
class CompletionGate:
    """The configured authority for final completion: how to read proof and what proof is accepted.

    `accepted` answers for one run and returns `None` when the Runtime holds no accepted criteria for
    it, which refuses that run's completion.
    """

    reader: IndependentEvidenceReader
    accepted: Callable[[str], AcceptedCompletion | None]


def decide_completion(
    accepted: AcceptedCompletion,
    readout: EvidenceReadout,
    *,
    workspace_id: str,
    job_id: str,
    run_step_id: str,
    runtime_attempt_id: str,
    fencing_generation: int,
) -> CompletionDecision:
    """Apply the completion rule to one readout, or refuse with a named reason.

    Pure: it reads nothing and writes nothing, so every refusal path can be exercised directly.
    """
    if not _is_readout(readout):
        raise CompletionRefused(REFUSED_MALFORMED, "the observation is outside its closed shape")
    if readout.fencing_generation != fencing_generation:
        raise CompletionRefused(REFUSED_STALE_FENCE, "the observation is not under the current fence")
    if readout.complete is not True:
        raise CompletionRefused(REFUSED_INCOMPLETE, "the observation is partial")
    if readout.workspace_id != workspace_id or readout.run_id != accepted.run_id or (
        readout.candidate_id,
        readout.binding_id,
        readout.definition_digest,
        readout.aggregate_id,
        readout.package_id,
        readout.application_id,
    ) != (
        accepted.candidate_id,
        accepted.binding_id,
        accepted.definition_digest,
        accepted.aggregate_id,
        accepted.package_id,
        accepted.application_id,
    ):
        raise CompletionRefused(REFUSED_IDENTITY, "the observation does not name the accepted identities")

    names = [item.criterion for item in readout.items]
    if len(set(names)) != len(names) or set(names) != set(accepted.criteria):
        raise CompletionRefused(
            REFUSED_CRITERIA, "the observation does not cover exactly the accepted criteria"
        )

    attributed: str | None = None
    references: list[EvidenceReference] = []
    for item in sorted(readout.items, key=lambda entry: entry.criterion):
        if item.outcome not in OUTCOMES:
            raise CompletionRefused(REFUSED_MALFORMED, "an evidence outcome is outside its shape")
        if item.outcome != OUTCOME_PROVEN:
            raise CompletionRefused(REFUSED_UNPROVEN, "an accepted criterion is not proven")
        if not (
            is_identifier(item.evidence_id)
            and _is_digest(item.content_digest)
            and is_identifier(item.collected_by)
            and is_identifier(item.reviewed_by)
        ):
            raise CompletionRefused(REFUSED_MALFORMED, "an evidence identity is outside its shape")
        if item.collected_by == item.reviewed_by or item.reviewed_by == accepted.implementer_id:
            raise CompletionRefused(
                REFUSED_NON_INDEPENDENT, "evidence was not reviewed independently of its collector"
            )
        if item.collected_by == accepted.implementer_id:
            exception = accepted.self_evidence
            if exception is None or exception.criterion != item.criterion:
                raise CompletionRefused(
                    REFUSED_IMPLEMENTER_SELF, "the implementer may not collect this evidence"
                )
            attributed = exception.attributed_to
        references.append(
            EvidenceReference(
                criterion=item.criterion,
                evidence_id=item.evidence_id,
                content_digest=item.content_digest,
                collected_by=item.collected_by,
                reviewed_by=item.reviewed_by,
            )
        )
    # The self-evidence exception covers one criterion; it never covers the whole proof. At least one
    # raw item must have been collected by someone other than the implementer, even when it is allowed.
    if all(item.collected_by == accepted.implementer_id for item in readout.items):
        raise CompletionRefused(
            REFUSED_IMPLEMENTER_SELF, "no evidence was collected independently of the implementer"
        )

    return CompletionDecision(
        workspace_id=workspace_id,
        run_id=accepted.run_id,
        job_id=job_id,
        run_step_id=run_step_id,
        runtime_attempt_id=runtime_attempt_id,
        candidate_id=accepted.candidate_id,
        binding_id=accepted.binding_id,
        definition_digest=accepted.definition_digest,
        aggregate_id=accepted.aggregate_id,
        package_id=accepted.package_id,
        application_id=accepted.application_id,
        decided_under_generation=fencing_generation,
        proven_criteria=tuple(accepted.criteria),
        evidence=tuple(references),
        self_evidence_attributed_to=attributed,
    )


def settle_completion(
    connection: sqlite3.Connection,
    gate: CompletionGate | None,
    *,
    workspace_id: str,
    run_id: str,
    job_id: str,
    run_step_id: str,
    runtime_attempt_id: str,
    fencing_generation: int,
    decided_at_us: int,
) -> StoredCompletionDecision:
    """Read proof, decide, and persist the decision. Call only inside the scheduler's fence.

    Any refusal raises :class:`CompletionRefused` (or the reader's own error), which the caller's
    fenced transaction turns into a full rollback.
    """
    if gate is None:
        raise CompletionRefused(REFUSED_NO_GATE, "final completion has no configured completion gate")
    accepted = gate.accepted(run_id)
    if accepted is None or accepted.run_id != run_id:
        raise CompletionRefused(REFUSED_NO_CRITERIA, "the run has no accepted completion criteria")
    try:
        readout = gate.reader.read(connection, workspace_id=workspace_id, run_id=run_id)
    except CompletionEvidenceUnavailable as error:
        raise CompletionRefused(REFUSED_UNAVAILABLE, "the evidence boundary is unavailable") from error
    decision = decide_completion(
        accepted,
        readout,
        workspace_id=workspace_id,
        job_id=job_id,
        run_step_id=run_step_id,
        runtime_attempt_id=runtime_attempt_id,
        fencing_generation=fencing_generation,
    )
    return record_decision(connection, decision=decision, decided_at_us=decided_at_us)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _is_readout(readout: object) -> bool:
    # Checked before any comparison or set operation, so a boundary that returns a malformed value
    # is refused by name rather than raising an incidental TypeError or AttributeError.
    return (
        isinstance(readout, EvidenceReadout)
        and is_identifier(readout.workspace_id)
        and is_identifier(readout.run_id)
        and isinstance(readout.fencing_generation, int)
        and not isinstance(readout.fencing_generation, bool)
        and isinstance(readout.complete, bool)
        and isinstance(readout.items, tuple)
        and all(_is_item(item) for item in readout.items)
    )


def _is_item(item: object) -> bool:
    return (
        isinstance(item, EvidenceItem)
        and is_identifier(item.criterion)
        and isinstance(item.outcome, str)
    )
