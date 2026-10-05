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

The reader is handed the workspace, the run and the fence it must answer under, and nothing else. It
is never handed the Runtime's database connection, so it has no route to the settlement's transaction:
it cannot commit, roll back or write it. Its evidence comes from the source the composition gave it.

Inputs are snapshotted once, into exact-typed records, before any check or comparison. A subclass of
`int`, `str`, `tuple`, or any of the Runtime's dataclasses is refused by type before it is read, so no
user code it carries runs during the rule.

Absence fails closed everywhere: no gate, no accepted criteria, an unavailable reader, a stale fence,
any identity that does not agree exactly, a criterion that is missing, extra, duplicated or not proven,
and evidence whose collector or reviewer is not independent of the implementer. The one exception is
:class:`SelfEvidenceException`: a single criterion the policy has explicitly allowed the implementer to
collect, attributed to the actor who allowed it. The reviewer must still be independent, and the
attribution is recorded on the decision.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, cast

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
        if not _text(self.definition_digest, _is_digest):
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

    It is given the identifiers and the fence it must answer under, and no connection: it reads from
    the source the composition configured for it, never from the settlement's transaction. It raises
    :class:`CompletionEvidenceUnavailable` when it cannot reach its evidence.
    """

    def read(
        self, *, workspace_id: str, run_id: str, fencing_generation: int
    ) -> EvidenceReadout: ...


@dataclass(frozen=True, slots=True)
class CompletionGate:
    """The configured authority for final completion: how to read proof and what proof is accepted.

    `accepted` answers for one run and returns `None` when the Runtime holds no accepted criteria for
    it, which refuses that run's completion.
    """

    reader: IndependentEvidenceReader
    accepted: Callable[[str], AcceptedCompletion | None]


@dataclass(frozen=True, slots=True)
class _Accepted:
    """A snapshot of one :class:`AcceptedCompletion`, read once and held as exact plain values."""

    run_id: str
    candidate_id: str
    binding_id: str
    definition_digest: str
    aggregate_id: str
    package_id: str
    application_id: str
    implementer_id: str
    criteria: tuple[str, ...]
    self_evidence: tuple[str, str] | None


@dataclass(frozen=True, slots=True)
class _Item:
    criterion: str
    outcome: str
    evidence_id: object
    content_digest: object
    collected_by: object
    reviewed_by: object


@dataclass(frozen=True, slots=True)
class _Observed:
    """A snapshot of one :class:`EvidenceReadout`, read once. Its values are checked before use."""

    workspace_id: object
    run_id: object
    candidate_id: object
    binding_id: object
    definition_digest: object
    aggregate_id: object
    package_id: object
    application_id: object
    fencing_generation: int
    complete: bool
    items: tuple[_Item, ...]


def decide_completion(
    accepted: AcceptedCompletion,
    readout: EvidenceReadout,
    *,
    workspace_id: str,
    job_id: str,
    run_step_id: str,
    runtime_attempt_id: str,
    application_attempt_number: int,
    fencing_generation: int,
    settled_sequence: int,
) -> CompletionDecision:
    """Apply the completion rule to one readout, or refuse with a named reason.

    Pure: it reads nothing and writes nothing, so every refusal path can be exercised directly.
    """
    return _decide(
        _snapshot_accepted(accepted),
        _snapshot_readout(readout),
        workspace_id=workspace_id,
        job_id=job_id,
        run_step_id=run_step_id,
        runtime_attempt_id=runtime_attempt_id,
        application_attempt_number=application_attempt_number,
        fencing_generation=fencing_generation,
        settled_sequence=settled_sequence,
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
    application_attempt_number: int,
    fencing_generation: int,
    decided_at_us: int,
) -> StoredCompletionDecision:
    """Read proof, decide, and persist the decision. Call only inside the scheduler's fence.

    Any refusal raises :class:`CompletionRefused` (or the reader's own error), which the caller's
    fenced transaction turns into a full rollback.
    """
    if not (
        _text(workspace_id, is_identifier)
        and _text(run_id, is_identifier)
        and _text(job_id, is_identifier)
        and _text(run_step_id, is_identifier)
        and _text(runtime_attempt_id, is_identifier)
        and _exact_int(application_attempt_number)
        and _exact_int(fencing_generation)
    ):
        raise CompletionRefused(REFUSED_MALFORMED, "the settlement is outside its closed shape")
    if gate is None:
        raise CompletionRefused(REFUSED_NO_GATE, "final completion has no configured completion gate")
    accepted_value = gate.accepted(run_id)
    if accepted_value is None:
        raise CompletionRefused(REFUSED_NO_CRITERIA, "the run has no accepted completion criteria")
    accepted = _snapshot_accepted(accepted_value)
    if accepted.run_id != run_id:
        raise CompletionRefused(REFUSED_NO_CRITERIA, "the run has no accepted completion criteria")
    try:
        readout = gate.reader.read(
            workspace_id=workspace_id, run_id=run_id, fencing_generation=fencing_generation
        )
    except CompletionEvidenceUnavailable as error:
        raise CompletionRefused(REFUSED_UNAVAILABLE, "the evidence boundary is unavailable") from error
    decision = _decide(
        accepted,
        _snapshot_readout(readout),
        workspace_id=workspace_id,
        job_id=job_id,
        run_step_id=run_step_id,
        runtime_attempt_id=runtime_attempt_id,
        application_attempt_number=application_attempt_number,
        fencing_generation=fencing_generation,
        settled_sequence=_next_event_sequence(connection, workspace_id=workspace_id, run_id=run_id),
    )
    return record_decision(connection, decision=decision, decided_at_us=decided_at_us)


def _decide(
    accepted: _Accepted,
    observed: _Observed,
    *,
    workspace_id: str,
    job_id: str,
    run_step_id: str,
    runtime_attempt_id: str,
    application_attempt_number: int,
    fencing_generation: int,
    settled_sequence: int,
) -> CompletionDecision:
    """The rule itself, over snapshots only. Every value compared here is an exact, checked type."""
    if not (
        _text(workspace_id, is_identifier)
        and _text(job_id, is_identifier)
        and _text(run_step_id, is_identifier)
        and _text(runtime_attempt_id, is_identifier)
        and _exact_int(application_attempt_number)
        and _exact_int(fencing_generation)
        and _exact_int(settled_sequence)
    ):
        raise CompletionRefused(REFUSED_MALFORMED, "the settlement is outside its closed shape")
    if observed.fencing_generation != fencing_generation:
        raise CompletionRefused(REFUSED_STALE_FENCE, "the observation is not under the current fence")
    if observed.complete is not True:
        raise CompletionRefused(REFUSED_INCOMPLETE, "the observation is partial")
    if observed.workspace_id != workspace_id or observed.run_id != accepted.run_id or (
        observed.candidate_id,
        observed.binding_id,
        observed.definition_digest,
        observed.aggregate_id,
        observed.package_id,
        observed.application_id,
    ) != (
        accepted.candidate_id,
        accepted.binding_id,
        accepted.definition_digest,
        accepted.aggregate_id,
        accepted.package_id,
        accepted.application_id,
    ):
        raise CompletionRefused(REFUSED_IDENTITY, "the observation does not name the accepted identities")

    names = [item.criterion for item in observed.items]
    if len(set(names)) != len(names) or set(names) != set(accepted.criteria):
        raise CompletionRefused(
            REFUSED_CRITERIA, "the observation does not cover exactly the accepted criteria"
        )

    attributed: str | None = None
    references: list[EvidenceReference] = []
    for item in sorted(observed.items, key=lambda entry: entry.criterion):
        if item.outcome not in OUTCOMES:
            raise CompletionRefused(REFUSED_MALFORMED, "an evidence outcome is outside its shape")
        if item.outcome != OUTCOME_PROVEN:
            raise CompletionRefused(REFUSED_UNPROVEN, "an accepted criterion is not proven")
        if not (
            _text(item.evidence_id, is_identifier)
            and _text(item.content_digest, _is_digest)
            and _text(item.collected_by, is_identifier)
            and _text(item.reviewed_by, is_identifier)
        ):
            raise CompletionRefused(REFUSED_MALFORMED, "an evidence identity is outside its shape")
        if item.collected_by == item.reviewed_by or item.reviewed_by == accepted.implementer_id:
            raise CompletionRefused(
                REFUSED_NON_INDEPENDENT, "evidence was not reviewed independently of its collector"
            )
        if item.collected_by == accepted.implementer_id:
            if accepted.self_evidence is None or accepted.self_evidence[0] != item.criterion:
                raise CompletionRefused(
                    REFUSED_IMPLEMENTER_SELF, "the implementer may not collect this evidence"
                )
            attributed = accepted.self_evidence[1]
        references.append(
            EvidenceReference(
                criterion=item.criterion,
                # Each identity was checked as an exact `str` above, before any of them was compared.
                evidence_id=cast(str, item.evidence_id),
                content_digest=cast(str, item.content_digest),
                collected_by=cast(str, item.collected_by),
                reviewed_by=cast(str, item.reviewed_by),
            )
        )
    # The self-evidence exception covers one criterion; it never covers the whole proof. At least one
    # raw item must have been collected by someone other than the implementer, even when it is allowed.
    if all(item.collected_by == accepted.implementer_id for item in observed.items):
        raise CompletionRefused(
            REFUSED_IMPLEMENTER_SELF, "no evidence was collected independently of the implementer"
        )

    return CompletionDecision(
        workspace_id=workspace_id,
        run_id=accepted.run_id,
        job_id=job_id,
        run_step_id=run_step_id,
        runtime_attempt_id=runtime_attempt_id,
        application_attempt_number=application_attempt_number,
        candidate_id=accepted.candidate_id,
        binding_id=accepted.binding_id,
        definition_digest=accepted.definition_digest,
        aggregate_id=accepted.aggregate_id,
        package_id=accepted.package_id,
        application_id=accepted.application_id,
        decided_under_generation=fencing_generation,
        settled_sequence=settled_sequence,
        proven_criteria=accepted.criteria,
        evidence=tuple(references),
        self_evidence_attributed_to=attributed,
    )


def _snapshot_accepted(accepted: object) -> _Accepted:
    """Read one accepted completion exactly once, refusing any field outside its exact type."""
    if type(accepted) is not AcceptedCompletion:
        raise CompletionRefused(REFUSED_MALFORMED, "the accepted completion is outside its closed shape")
    run_id = accepted.run_id
    candidate_id = accepted.candidate_id
    binding_id = accepted.binding_id
    definition_digest = accepted.definition_digest
    aggregate_id = accepted.aggregate_id
    package_id = accepted.package_id
    application_id = accepted.application_id
    implementer_id = accepted.implementer_id
    raw_criteria = accepted.criteria
    raw_self = accepted.self_evidence
    self_evidence: tuple[str, str] | None = None
    if raw_self is not None:
        if type(raw_self) is not SelfEvidenceException:
            raise CompletionRefused(REFUSED_MALFORMED, "the accepted completion is outside its closed shape")
        self_evidence = (raw_self.criterion, raw_self.attributed_to)
    if not (
        all(
            _text(value, is_identifier)
            for value in (
                run_id,
                candidate_id,
                binding_id,
                aggregate_id,
                package_id,
                application_id,
                implementer_id,
            )
        )
        and _text(definition_digest, _is_digest)
        and type(raw_criteria) is tuple
        and all(_text(name, is_identifier) for name in raw_criteria)
        and (self_evidence is None or all(_text(value, is_identifier) for value in self_evidence))
    ):
        raise CompletionRefused(REFUSED_MALFORMED, "the accepted completion is outside its closed shape")
    return _Accepted(
        run_id=run_id,
        candidate_id=candidate_id,
        binding_id=binding_id,
        definition_digest=definition_digest,
        aggregate_id=aggregate_id,
        package_id=package_id,
        application_id=application_id,
        implementer_id=implementer_id,
        criteria=raw_criteria,
        self_evidence=self_evidence,
    )


def _snapshot_readout(readout: object) -> _Observed:
    """Read one observation exactly once, into plain values, before anything compares them."""
    if type(readout) is not EvidenceReadout:
        raise CompletionRefused(REFUSED_MALFORMED, "the observation is outside its closed shape")
    workspace_id = readout.workspace_id
    run_id = readout.run_id
    candidate_id = readout.candidate_id
    binding_id = readout.binding_id
    definition_digest = readout.definition_digest
    aggregate_id = readout.aggregate_id
    package_id = readout.package_id
    application_id = readout.application_id
    fencing_generation = readout.fencing_generation
    complete = readout.complete
    raw_items = readout.items
    if not (
        _text(workspace_id, is_identifier)
        and _text(run_id, is_identifier)
        and all(
            _stated(value, is_identifier)
            for value in (candidate_id, binding_id, aggregate_id, package_id, application_id)
        )
        and _stated(definition_digest, _is_digest)
        and _exact_int(fencing_generation)
        and type(complete) is bool
        and type(raw_items) is tuple
    ):
        raise CompletionRefused(REFUSED_MALFORMED, "the observation is outside its closed shape")
    items: list[_Item] = []
    for raw in raw_items:
        if type(raw) is not EvidenceItem:
            raise CompletionRefused(REFUSED_MALFORMED, "the observation is outside its closed shape")
        criterion = raw.criterion
        outcome = raw.outcome
        if not (_text(criterion, is_identifier) and type(outcome) is str):
            raise CompletionRefused(REFUSED_MALFORMED, "the observation is outside its closed shape")
        items.append(
            _Item(
                criterion=criterion,
                outcome=outcome,
                evidence_id=raw.evidence_id,
                content_digest=raw.content_digest,
                collected_by=raw.collected_by,
                reviewed_by=raw.reviewed_by,
            )
        )
    return _Observed(
        workspace_id=workspace_id,
        run_id=run_id,
        candidate_id=candidate_id,
        binding_id=binding_id,
        definition_digest=definition_digest,
        aggregate_id=aggregate_id,
        package_id=package_id,
        application_id=application_id,
        fencing_generation=fencing_generation,
        complete=complete,
        items=tuple(items),
    )


def _next_event_sequence(connection: sqlite3.Connection, *, workspace_id: str, run_id: str) -> int:
    """The sequence the run's next event takes, which is the one a settlement's event must take."""
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    return int(row[0])


def _text(value: object, check: Callable[[str], bool]) -> bool:
    # Exact `str` only: a subclass can carry its own `__eq__`, which is user code.
    return type(value) is str and check(value)


def _stated(value: object, check: Callable[[str], bool]) -> bool:
    return value is None or _text(value, check)


def _exact_int(value: object) -> bool:
    # Exact `int` only: a subclass of int compares equal to an ordinary integer and runs its own code.
    return type(value) is int


def _is_digest(value: str) -> bool:
    # Called only on an exact `str`, through `_text`.
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )
