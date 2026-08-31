"""Runtime Execution Planes: the bounded loop runner and iteration-ledger oracle.

This is a provisional in-memory conformance oracle for T-0688 `WEFT-BL-012`, not a
second runtime and not a second scheduler. Like :mod:`workflow` beside it, it holds
no database, no recovery, no transport and no Platform handle, it allocates no
table, and every decision it makes is a value returned or a refusal raised. It
never writes canonical state.

It does not restate :class:`~omnivia_core_runtime.execution.workflow.LoopDefinition`
and its :class:`~omnivia_core_runtime.execution.workflow.LoopController`, which are
a different question about the same word: those two bound a loop's *spend* -- an
iteration cap and a budget -- while everything here bounds and records a loop's
*iterations*. One imports the other's iteration cap and adds no second spelling of
it.

It consumes a :class:`FrozenLoopPlan` -- the small frozen runtime view of the
complete `LoopPlan` the Core contract validates -- and answers four questions and
no others:

- **which iterations exist**, by deriving a stable iteration identity from the
  source element through :func:`~omnivia_core_runtime.execution.profile.derive_id`,
  so the same plan and the same source always name the same iterations, and a loop
  nested inside another names iterations that cannot collide with its parent's;
- **when they may launch**, one at a time for a ``SEQUENTIAL`` plan and at most
  ``maximum_concurrency`` at a time for a ``PARALLEL`` one, never beyond the frozen
  ``maximum_iterations``;
- **what one launch is**, namely :class:`IterationLaunch` -- an identity, an inputs
  digest, a carry digest and the scheduling intents -- whose
  :attr:`IterationLaunch.bundle_unit` is exactly the content of one
  `RuntimeTransitionBundle` and is never split across two;
- **what happened**, in a complete :class:`IterationLedgerEntry` ledger covering
  cancelled, failed and skipped iterations as much as successful ones, and
  preserving every member of the atomic launch, scheduling intents included.

The ledger is the recovery record, and it is the *only* recovery record. Replaying
it re-derives every recorded identity, refuses a ledger that is not contiguous in
ordinal launch order, never relaunches an iteration it already records, and folds
the carry forward out of the recorded entries themselves -- so a resumed runner
does not need an ambient caller to hand it back the carry it had before the crash,
and refuses a caller-supplied carry that disagrees with the one the ledger
replays.

Two deliberate absences. The plan's ``done`` condition is not evaluated here --
a predicate over run values needs the run values, which this seam never holds --
so completion is driven by the bound source and the frozen bounds. And settlement
is ordered by a monotonic sequence rather than a clock, because a pure oracle has
no clock and a replay has to reproduce the order exactly; the wall-clock
``settledAt`` instant belongs to the Core `LoopIterationLedger` record.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from omnivia_core_runtime.execution.profile import (
    ExecutionContractError,
    ExecutionRefused,
    canonical_hash,
    derive_id,
    require_collection,
    require_digest,
    require_identifier,
    require_vocabulary,
)
from omnivia_core_runtime.execution.workflow import (
    _MAX_LOOP_ITERATIONS,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOMES,
)

#: The nesting bound. A namespace entry is one enclosing iteration identity, so
#: this is how deeply loops may be nested before the seam fails closed.
_MAX_LOOP_NESTING: Final = 16

LOOP_MODE_SEQUENTIAL: Final = "SEQUENTIAL"
LOOP_MODE_PARALLEL: Final = "PARALLEL"

LOOP_MODES: Final[frozenset[str]] = frozenset(
    {LOOP_MODE_SEQUENTIAL, LOOP_MODE_PARALLEL}
)

ORDER_ITERATION_IDENTITY: Final = "ITERATION_IDENTITY_ORDER"
ORDER_SETTLEMENT: Final = "SETTLEMENT_ORDER"

LOOP_ORDER_GUARANTEES: Final[frozenset[str]] = frozenset(
    {ORDER_ITERATION_IDENTITY, ORDER_SETTLEMENT}
)

CANCEL_REMAINING: Final = "CANCEL_REMAINING"
DRAIN_IN_FLIGHT: Final = "DRAIN_IN_FLIGHT"
COMPLETE_ALL: Final = "COMPLETE_ALL"

LOOP_CANCELLATION_POLICIES: Final[frozenset[str]] = frozenset(
    {CANCEL_REMAINING, DRAIN_IN_FLIGHT, COMPLETE_ALL}
)

FAIL_LOOP: Final = "FAIL_LOOP"
RECORD_AND_CONTINUE: Final = "RECORD_AND_CONTINUE"
RECORD_AND_STOP_AFTER_CURRENT: Final = "RECORD_AND_STOP_AFTER_CURRENT"

LOOP_PARTIAL_SUCCESS_POLICIES: Final[frozenset[str]] = frozenset(
    {FAIL_LOOP, RECORD_AND_CONTINUE, RECORD_AND_STOP_AFTER_CURRENT}
)

ZIP_REFUSE: Final = "REFUSE"
ZIP_TRUNCATE_TO_SHORTEST: Final = "TRUNCATE_TO_SHORTEST"
ZIP_PAD_WITH_ABSENT: Final = "PAD_WITH_ABSENT"

LOOP_ZIP_MISMATCH_POLICIES: Final[frozenset[str]] = frozenset(
    {ZIP_REFUSE, ZIP_TRUNCATE_TO_SHORTEST, ZIP_PAD_WITH_ABSENT}
)

#: ``CANCELLED`` and ``SKIPPED`` are the two outcome classes an iteration has that
#: a single step does not, so the ledger's vocabulary is the seam's two outcomes
#: plus those. A late result is not an outcome class here: it is a
#: :class:`LateResult`, precisely because it can never become one.
ITERATION_CANCELLED: Final = "CANCELLED"
ITERATION_SKIPPED: Final = "SKIPPED"

ITERATION_OUTCOMES: Final[frozenset[str]] = OUTCOMES | {
    ITERATION_CANCELLED,
    ITERATION_SKIPPED,
}

#: ``STOPPING`` is the honest middle state: no further iteration launches, but the
#: ones already in flight are neither abandoned nor rolled back.
LOOP_RUNNING: Final = "RUNNING"
LOOP_STOPPING: Final = "STOPPING"
LOOP_SETTLED: Final = "SETTLED"

LOOP_STATES: Final[frozenset[str]] = frozenset(
    {LOOP_RUNNING, LOOP_STOPPING, LOOP_SETTLED}
)

#: The disposition recorded against an iteration cancelled in flight. Cancellation
#: is not rollback, so this records *that* the iteration was cancelled and never
#: unwinds what it had already settled.
DISPOSITION_CANCELLED_IN_FLIGHT: Final = "CANCELLED_IN_FLIGHT"

#: The key a padded zip position carries when the primary source has run out of
#: elements. It is an ordinary identifier and can therefore collide with a real
#: element key, which is why every resolved key and identity is checked for
#: duplicates before a single iteration is planned.
_PAD_KEY_PREFIX: Final = "pad-"


# --------------------------------------------------------------------------
# The frozen plan and its source elements
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoopElement:
    """One bound source element: the key its identity derives from, and its digest."""

    key: str
    payload_digest: str

    def __post_init__(self) -> None:
        require_identifier("key", self.key)
        require_digest("payload_digest", self.payload_digest)


@dataclass(frozen=True, slots=True)
class FrozenLoopPlan:
    """The frozen runtime view of one complete `LoopPlan`.

    ``maximum_iterations``, ``maximum_concurrency`` and ``order_guarantee`` are the
    three members frozen at binding time, and this value is frozen too, so a live
    Run cannot acquire a different bound -- including across a resume, where the
    resumed runner is constructed from the same plan value it recorded against.

    ``namespace`` is the enclosing iteration identities, outermost first. It is what
    makes a nested loop's identities namespace-safe: two sibling iterations of an
    outer loop each run an inner loop over the same elements, and the inner
    identities still differ, because the outer identity is part of the preimage.
    """

    loop_stable_id: str
    mode: str
    order_guarantee: str
    maximum_iterations: int
    cancellation_policy: str
    partial_success_policy: str
    maximum_concurrency: int = 1
    zip_mismatch_policy: str | None = None
    carry_enabled: bool = False
    namespace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier("loop_stable_id", self.loop_stable_id)
        require_vocabulary("mode", self.mode, LOOP_MODES)
        require_vocabulary(
            "order_guarantee", self.order_guarantee, LOOP_ORDER_GUARANTEES
        )
        require_vocabulary(
            "cancellation_policy", self.cancellation_policy, LOOP_CANCELLATION_POLICIES
        )
        require_vocabulary(
            "partial_success_policy",
            self.partial_success_policy,
            LOOP_PARTIAL_SUCCESS_POLICIES,
        )
        if not 0 < self.maximum_iterations <= _MAX_LOOP_ITERATIONS:
            raise ExecutionContractError(
                "invalid_loop_bound",
                f"maximum_iterations must be in 1..{_MAX_LOOP_ITERATIONS}",
            )
        if self.mode == LOOP_MODE_PARALLEL:
            if not 0 < self.maximum_concurrency <= self.maximum_iterations:
                raise ExecutionContractError(
                    "invalid_loop_bound",
                    "a parallel plan requires a concurrency bound in 1..maximum_iterations",
                )
        elif self.maximum_concurrency != 1:
            raise ExecutionContractError(
                "invalid_loop_bound", "a sequential plan runs one iteration at a time"
            )
        if self.carry_enabled and self.mode != LOOP_MODE_SEQUENTIAL:
            raise ExecutionContractError(
                "invalid_loop_plan", "carry is permitted only for a sequential plan"
            )
        if self.zip_mismatch_policy is not None:
            require_vocabulary(
                "zip_mismatch_policy",
                self.zip_mismatch_policy,
                LOOP_ZIP_MISMATCH_POLICIES,
            )
        if len(self.namespace) > _MAX_LOOP_NESTING:
            raise ExecutionContractError(
                "invalid_loop_plan", f"namespace nests beyond {_MAX_LOOP_NESTING}"
            )
        for entry in self.namespace:
            require_digest("namespace entry", entry)

    def iteration_identity(self, element_key: str) -> str:
        """Return the stable identity of the iteration over ``element_key``.

        Deterministic and namespace-safe: the preimage is the enclosing identities,
        this loop's stable id and the element key, joined by
        :func:`~omnivia_core_runtime.execution.profile.derive_id`'s separator, which
        no identifier or digest can itself contain.
        """
        return derive_id(*self.namespace, self.loop_stable_id, element_key)

    def nested(self, iteration_identity: str, plan: FrozenLoopPlan) -> FrozenLoopPlan:
        """Return ``plan`` re-namespaced under one of this loop's iterations."""
        require_digest("iteration_identity", iteration_identity)
        return replace(plan, namespace=(*self.namespace, iteration_identity))


# --------------------------------------------------------------------------
# One atomic launch, one ledger entry, one late result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IterationLaunch:
    """One iteration's whole launch: identity, inputs, carry and scheduling intents.

    This is the unit that goes into exactly one `RuntimeTransitionBundle`. There is
    no partial launch to describe, because there is no smaller value here to
    describe one with.
    """

    iteration_identity: str
    ordinal: int
    inputs_digest: str
    carry_digest: str | None
    scheduling_intents: tuple[str, ...]

    def __post_init__(self) -> None:
        require_digest("iteration_identity", self.iteration_identity)
        if self.ordinal < 0:
            raise ExecutionContractError(
                "invalid_ordinal", "ordinal must be non-negative"
            )
        require_digest("inputs_digest", self.inputs_digest)
        if self.carry_digest is not None:
            require_digest("carry_digest", self.carry_digest)
        require_collection(
            "scheduling_intents", self.scheduling_intents, required=False
        )

    @property
    def scheduling_intents_digest(self) -> str:
        """The digest the `LoopIterationLedger` entry carries for these intents."""
        return canonical_hash(list(self.scheduling_intents))

    @property
    def bundle_unit(self) -> dict[str, object]:
        """Return the launch as the single atomic bundle payload it must be written as."""
        return {
            "iterationIdentity": self.iteration_identity,
            "ordinal": self.ordinal,
            "inputsDigest": self.inputs_digest,
            "carryDigest": self.carry_digest,
            "schedulingIntents": list(self.scheduling_intents),
            "schedulingIntentsDigest": self.scheduling_intents_digest,
        }


@dataclass(frozen=True, slots=True)
class IterationLedgerEntry:
    """One iteration's complete record: launched, then settled in exactly one class.

    Every member of the atomic launch is preserved, scheduling intents included, so
    the ledger is a complete record of what was launched and not merely of what came
    back. Every incoherent combination fails closed here rather than at the caller
    that would have to notice it: an unsettled entry carries no settlement, a
    settled one carries a positive settlement sequence, a succeeded one carries
    outputs and no failure, a failed one a failure and no outputs, a cancelled one a
    disposition and neither, and only a succeeded iteration produces a carry.
    """

    iteration_identity: str
    ordinal: int
    inputs_digest: str
    carry_digest: str | None = None
    scheduling_intents: tuple[str, ...] = ()
    outcome: str | None = None
    outputs_digest: str | None = None
    failure_reason: str | None = None
    settlement_sequence: int | None = None
    effect_settlements: tuple[str, ...] = ()
    cancellation_disposition: str | None = None
    resulting_carry_digest: str | None = None

    def __post_init__(self) -> None:
        require_digest("iteration_identity", self.iteration_identity)
        if self.ordinal < 0:
            raise ExecutionContractError(
                "invalid_ordinal", "ordinal must be non-negative"
            )
        require_digest("inputs_digest", self.inputs_digest)
        for name in ("carry_digest", "outputs_digest", "resulting_carry_digest"):
            value: str | None = getattr(self, name)
            if value is not None:
                require_digest(name, value)
        if self.failure_reason is not None:
            require_identifier("failure_reason", self.failure_reason)
        require_collection(
            "scheduling_intents", self.scheduling_intents, required=False
        )
        require_collection(
            "effect_settlements", self.effect_settlements, required=False
        )
        if self.outcome is not None:
            require_vocabulary("outcome", self.outcome, ITERATION_OUTCOMES)
        self._validate_settlement()

    def _validate_settlement(self) -> None:
        """Refuse every outcome/output/failure/sequence/cancellation incoherence."""
        if self.outcome is None:
            unsettled = (
                self.outputs_digest,
                self.failure_reason,
                self.settlement_sequence,
                self.cancellation_disposition,
                self.resulting_carry_digest,
            )
            if any(value is not None for value in unsettled):
                raise ExecutionContractError(
                    "invalid_settlement",
                    "an unsettled iteration records no settlement of any kind",
                )
            return

        if self.settlement_sequence is None or self.settlement_sequence < 1:
            raise ExecutionContractError(
                "invalid_settlement",
                "a settled iteration records a positive settlement sequence",
            )
        if self.outcome == OUTCOME_SUCCEEDED:
            if self.outputs_digest is None or self.failure_reason is not None:
                raise ExecutionContractError(
                    "invalid_settlement",
                    "a succeeded iteration records outputs and no failure",
                )
        elif self.outcome == OUTCOME_FAILED:
            if self.outputs_digest is not None or not self.failure_reason:
                raise ExecutionContractError(
                    "invalid_settlement",
                    "a failed iteration records a failure and no outputs",
                )
        elif self.outputs_digest is not None or self.failure_reason is not None:
            raise ExecutionContractError(
                "invalid_settlement",
                f"a {self.outcome} iteration records neither outputs nor a failure",
            )

        cancelled = self.outcome == ITERATION_CANCELLED
        if cancelled != (self.cancellation_disposition is not None):
            raise ExecutionContractError(
                "invalid_settlement",
                "a cancellation disposition is recorded exactly for a cancelled iteration",
            )
        if cancelled:
            require_vocabulary(
                "cancellation_disposition",
                self.cancellation_disposition or "",
                frozenset({DISPOSITION_CANCELLED_IN_FLIGHT}),
            )
        if (
            self.resulting_carry_digest is not None
            and self.outcome != OUTCOME_SUCCEEDED
        ):
            raise ExecutionContractError(
                "invalid_settlement",
                f"a {self.outcome} iteration produces no carry",
            )

    @property
    def is_settled(self) -> bool:
        return self.outcome is not None

    @property
    def scheduling_intents_digest(self) -> str:
        """The digest the `LoopIterationLedger` entry carries for these intents."""
        return canonical_hash(list(self.scheduling_intents))


@dataclass(frozen=True, slots=True)
class LateResult:
    """A result that arrived after the loop settled: Evidence, and nothing else.

    ``applied_to_run_state`` is a field rather than an assumption so a reader can
    see the answer rather than infer it, and it is refused as anything but ``False``.
    """

    iteration_identity: str
    evidence_ref: str
    applied_to_run_state: bool = False

    def __post_init__(self) -> None:
        require_digest("iteration_identity", self.iteration_identity)
        require_identifier("evidence_ref", self.evidence_ref)
        if self.applied_to_run_state:
            raise ExecutionContractError(
                "late_result_applied",
                "a late result is Evidence and is never applied to Run state",
            )


# --------------------------------------------------------------------------
# Source resolution: the effective element list under the zip mismatch policy
# --------------------------------------------------------------------------


def _resolve_elements(
    plan: FrozenLoopPlan,
    source: tuple[LoopElement, ...],
    zip_sources: tuple[tuple[LoopElement, ...], ...],
) -> tuple[tuple[str, str], ...]:
    """Return the effective ``(element_key, inputs_digest)`` list for one plan.

    Duplicate keys and duplicate derived identities are refused *here*, before a
    single iteration is planned, rather than being left for the second launch of a
    colliding identity to trip over. A padded zip position carries a synthesised
    key, which is an ordinary identifier and can therefore collide with a real
    element key, so padding is checked by exactly the same rule.
    """
    if not zip_sources:
        if plan.zip_mismatch_policy is not None:
            raise ExecutionContractError(
                "invalid_loop_plan",
                "zip_mismatch_policy is declared without zipped sources",
            )
        resolved = tuple((element.key, element.payload_digest) for element in source)
        return _reject_collisions(plan, resolved)

    if plan.zip_mismatch_policy is None:
        raise ExecutionContractError(
            "invalid_loop_plan", "zipped sources require a zip_mismatch_policy"
        )
    lengths = (len(source), *(len(entries) for entries in zip_sources))
    if plan.zip_mismatch_policy == ZIP_REFUSE:
        if len(set(lengths)) != 1:
            raise ExecutionRefused(
                "zip_mismatch", "zipped sources differ in length under REFUSE"
            )
        count = lengths[0]
    elif plan.zip_mismatch_policy == ZIP_TRUNCATE_TO_SHORTEST:
        count = min(lengths)
    else:
        count = max(lengths)

    zipped: list[tuple[str, str]] = []
    for index in range(count):
        digests = [
            entries[index].payload_digest if index < len(entries) else None
            for entries in (source, *zip_sources)
        ]
        key = source[index].key if index < len(source) else f"{_PAD_KEY_PREFIX}{index}"
        zipped.append((key, canonical_hash(digests)))
    return _reject_collisions(plan, tuple(zipped))


def _reject_collisions(
    plan: FrozenLoopPlan, resolved: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Return ``resolved`` unless two positions share a key or a derived identity."""
    keys = [key for key, _ in resolved]
    if len(set(keys)) != len(keys):
        raise ExecutionRefused(
            "duplicate_iteration",
            "the resolved source names the same element key twice",
        )
    identities = [plan.iteration_identity(key) for key in keys]
    if len(set(identities)) != len(identities):
        raise ExecutionRefused(
            "duplicate_iteration",
            "the resolved source derives the same iteration identity twice",
        )
    return resolved


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


class LoopRunner:
    """Plans, records and replays one loop's iterations against a complete ledger.

    Construct it with no ledger to start a loop, or with a recorded ledger to
    resume one. A resume re-derives every recorded identity from the same frozen
    plan and the same source, so a ledger that names an iteration this plan would
    not name is refused as drift rather than silently re-keyed, and it folds the
    carry forward out of the recorded entries, so no caller has to reconstruct it.

    ``carry_digest`` is the loop's *initial* carry. On a resume it is optional: the
    ledger already records the carry every recorded launch consumed and every
    settled iteration produced. Supplying one that disagrees with the ledger is
    refused rather than preferred over the record.
    """

    def __init__(
        self,
        plan: FrozenLoopPlan,
        source: tuple[LoopElement, ...],
        *,
        zip_sources: tuple[tuple[LoopElement, ...], ...] = (),
        ledger: tuple[IterationLedgerEntry, ...] = (),
        carry_digest: str | None = None,
        late_results: tuple[LateResult, ...] = (),
    ) -> None:
        self.plan = plan
        self._elements = _resolve_elements(plan, source, zip_sources)
        self._entries: list[IterationLedgerEntry] = []
        self._late: list[LateResult] = []
        self._carry_digest = carry_digest
        self._carry_asserted = carry_digest is not None
        self._state = LOOP_RUNNING
        self._failure_reason: str | None = None
        self._cancellation_requested = False
        self._sequence = 0
        self._sequences: set[int] = set()

        if carry_digest is not None:
            if not plan.carry_enabled:
                raise ExecutionContractError(
                    "invalid_loop_plan", "a carry digest requires a carrying plan"
                )
            require_digest("carry_digest", carry_digest)
        elif plan.carry_enabled and not ledger:
            raise ExecutionContractError(
                "invalid_loop_plan",
                "a new carrying loop requires its frozen initial carry digest",
            )

        for entry in ledger:
            self._replay(entry)
        self._refresh()
        for late in late_results:
            self._admit_late(late)

    # -- replay ------------------------------------------------------------

    def _replay(self, entry: IterationLedgerEntry) -> None:
        """Re-derive one recorded entry, refusing any drift from the frozen plan.

        The ledger is replayed exactly as given: ordinals must run ``0, 1, 2, ...``
        with no hole, no skipped prefix and no reordering, because a ledger that has
        been re-sorted or has had a launch dropped is not the record of what
        happened, and silently sorting it would hide precisely that.
        """
        if entry.ordinal != len(self._entries):
            raise ExecutionRefused(
                "ledger_not_contiguous",
                "the ledger is not contiguous in ordinal launch order",
            )
        if not 0 <= entry.ordinal < len(self._elements):
            raise ExecutionRefused(
                "iteration_identity_drift",
                "a recorded ordinal is outside the bound source",
            )
        if entry.ordinal >= self.plan.maximum_iterations:
            raise ExecutionRefused(
                "loop_exhausted", "a recorded ordinal is beyond maximum_iterations"
            )
        key, inputs_digest = self._elements[entry.ordinal]
        if entry.iteration_identity != self.plan.iteration_identity(key):
            raise ExecutionRefused(
                "iteration_identity_drift",
                "a recorded iteration identity is not the one this plan derives",
            )
        if entry.inputs_digest != inputs_digest:
            raise ExecutionRefused(
                "iteration_identity_drift",
                "a recorded iteration's inputs digest is not the one this source derives",
            )
        if any(
            recorded.iteration_identity == entry.iteration_identity
            for recorded in self._entries
        ):
            raise ExecutionRefused(
                "duplicate_iteration", "the ledger records the same iteration twice"
            )
        self._replay_carry(entry)
        if entry.settlement_sequence is not None:
            if entry.settlement_sequence in self._sequences:
                raise ExecutionRefused(
                    "duplicate_settlement_sequence",
                    "the ledger settles two iterations at the same sequence",
                )
            self._sequences.add(entry.settlement_sequence)
            self._sequence = max(self._sequence, entry.settlement_sequence)
        self._entries.append(entry)
        if entry.resulting_carry_digest is not None:
            self._carry_digest = entry.resulting_carry_digest
        if entry.outcome == OUTCOME_FAILED:
            self._apply_partial_success()

    def _replay_carry(self, entry: IterationLedgerEntry) -> None:
        """Check one recorded launch's carry against the carry the ledger has folded.

        The first replayed launch of a carrying plan is where the loop's initial
        carry comes from when the caller supplied none -- that is what makes a
        resume independent of an ambient caller. Every launch after it must carry
        exactly what the preceding settled iterations produced.
        """
        if not self.plan.carry_enabled:
            if entry.carry_digest is not None:
                raise ExecutionRefused(
                    "carry_drift", "a non-carrying plan records no carry digest"
                )
            return
        if not self._carry_asserted and not self._entries:
            if entry.carry_digest is None:
                raise ExecutionRefused(
                    "carry_drift",
                    "a carrying ledger must record its frozen initial carry",
                )
            self._carry_digest = entry.carry_digest
            self._carry_asserted = True
        if entry.carry_digest != self._carry_digest:
            raise ExecutionRefused(
                "carry_drift",
                "a recorded launch does not carry the carry the ledger replays",
            )

    # -- observation -------------------------------------------------------

    @property
    def ledger(self) -> tuple[IterationLedgerEntry, ...]:
        """The complete iteration ledger, in ordinal launch order."""
        return tuple(self._entries)

    @property
    def late_results(self) -> tuple[LateResult, ...]:
        return tuple(self._late)

    @property
    def carry_digest(self) -> str | None:
        return self._carry_digest

    @property
    def state(self) -> str:
        return self._state

    @property
    def settled(self) -> bool:
        return self._state == LOOP_SETTLED

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation_requested

    @property
    def in_flight(self) -> tuple[IterationLedgerEntry, ...]:
        return tuple(entry for entry in self._entries if not entry.is_settled)

    def gather(self) -> tuple[str, ...]:
        """Return the succeeded iterations' output digests in the guaranteed order."""
        succeeded = [
            entry for entry in self._entries if entry.outcome == OUTCOME_SUCCEEDED
        ]
        if self.plan.order_guarantee == ORDER_SETTLEMENT:
            succeeded.sort(key=lambda entry: entry.settlement_sequence or 0)
        else:
            succeeded.sort(key=lambda entry: entry.ordinal)
        return tuple(
            entry.outputs_digest for entry in succeeded if entry.outputs_digest
        )

    # -- planning and launching -------------------------------------------

    def plan_launches(
        self, *, scheduling_intents: tuple[str, ...] = ()
    ) -> tuple[IterationLaunch, ...]:
        """Return the next wave of launches: one for sequential, bounded for parallel.

        Deterministic: the wave is the lowest un-launched ordinals in order, capped
        by the concurrency headroom and the frozen bounds. Empty once the loop is
        stopping or settled, once the frozen bounds are reached, or once the
        concurrency bound is saturated. It never returns a launch for an iteration
        the ledger already records.
        """
        if self._state != LOOP_RUNNING:
            return ()
        launched = {entry.ordinal for entry in self._entries}
        available = self.plan.maximum_concurrency - len(self.in_flight)
        if available < 1:
            return ()
        limit = min(len(self._elements), self.plan.maximum_iterations)
        launches: list[IterationLaunch] = []
        for ordinal in range(limit):
            if len(launches) >= available:
                break
            if ordinal in launched:
                continue
            key, inputs_digest = self._elements[ordinal]
            launches.append(
                IterationLaunch(
                    iteration_identity=self.plan.iteration_identity(key),
                    ordinal=ordinal,
                    inputs_digest=inputs_digest,
                    carry_digest=self._carry_digest
                    if self.plan.carry_enabled
                    else None,
                    scheduling_intents=scheduling_intents,
                )
            )
        return tuple(launches)

    def record_launch(self, launch: IterationLaunch) -> IterationLedgerEntry:
        """Record one atomic launch against the ledger, or refuse it.

        Refuses a relaunch of any iteration the ledger already records -- settled or
        in flight -- a launch beyond the frozen bounds, a launch whose identity or
        inputs are not the ones this plan derives for its ordinal, and a launch
        whose carry is not the loop's current carry. The whole launch is recorded,
        scheduling intents included: nothing in the atomic unit is dropped on the
        way into the ledger.
        """
        if self._state != LOOP_RUNNING:
            raise ExecutionRefused(
                "loop_not_launching", f"a {self._state} loop launches no iteration"
            )
        if any(
            entry.ordinal == launch.ordinal
            or entry.iteration_identity == launch.iteration_identity
            for entry in self._entries
        ):
            raise ExecutionRefused(
                "iteration_already_launched",
                "the ledger already records this iteration",
            )
        if launch.ordinal >= self.plan.maximum_iterations:
            raise ExecutionRefused(
                "loop_exhausted", "the launch is beyond maximum_iterations"
            )
        if launch.ordinal >= len(self._elements):
            raise ExecutionRefused(
                "unknown_iteration", "the launch is beyond the bound source"
            )
        if launch.ordinal != len(self._entries):
            raise ExecutionRefused(
                "ledger_not_contiguous",
                "a launch out of ordinal order would leave a hole in the ledger",
            )
        if len(self.in_flight) >= self.plan.maximum_concurrency:
            raise ExecutionRefused(
                "concurrency_exhausted", "the concurrency bound is already saturated"
            )
        key, inputs_digest = self._elements[launch.ordinal]
        if (
            launch.iteration_identity != self.plan.iteration_identity(key)
            or launch.inputs_digest != inputs_digest
        ):
            raise ExecutionRefused(
                "iteration_identity_drift",
                "the launch is not the one this plan derives for its ordinal",
            )
        expected_carry = self._carry_digest if self.plan.carry_enabled else None
        if launch.carry_digest != expected_carry:
            raise ExecutionRefused(
                "carry_drift",
                "the launch does not carry the loop's current carry",
            )
        entry = IterationLedgerEntry(
            iteration_identity=launch.iteration_identity,
            ordinal=launch.ordinal,
            inputs_digest=launch.inputs_digest,
            carry_digest=launch.carry_digest,
            scheduling_intents=launch.scheduling_intents,
        )
        self._entries.append(entry)
        return entry

    # -- settlement --------------------------------------------------------

    def _index_of(self, iteration_identity: str) -> int:
        for index, entry in enumerate(self._entries):
            if entry.iteration_identity == iteration_identity:
                return index
        raise ExecutionRefused(
            "unknown_iteration", "the ledger records no such iteration identity"
        )

    def record_effect_settlement(
        self, iteration_identity: str, effect_request_id: str
    ) -> None:
        """Record one effect this iteration has already settled.

        Recorded while the iteration is in flight, and never removed afterwards:
        cancellation is not rollback, so an effect already issued stays recorded
        against its iteration whatever the iteration goes on to settle as. The same
        effect request is never recorded twice, because the iteration identity plus
        the effect request id is the idempotency key for that settlement.
        """
        require_identifier("effect_request_id", effect_request_id)
        index = self._index_of(iteration_identity)
        entry = self._entries[index]
        if entry.is_settled:
            raise ExecutionRefused(
                "iteration_already_settled",
                "a settled iteration records no further effect",
            )
        if effect_request_id in entry.effect_settlements:
            raise ExecutionRefused(
                "duplicate_effect_settlement",
                "the iteration already records this effect settlement",
            )
        self._entries[index] = replace(
            entry, effect_settlements=(*entry.effect_settlements, effect_request_id)
        )

    def settle(
        self,
        iteration_identity: str,
        *,
        outcome: str,
        outputs_digest: str | None = None,
        failure_reason: str | None = None,
        resulting_carry_digest: str | None = None,
    ) -> IterationLedgerEntry:
        """Settle one in-flight iteration in exactly one outcome class.

        ``resulting_carry_digest`` is the carry this iteration produced. It is stored
        on the settled entry rather than only on the runner, which is what lets a
        resume fold the carry out of the ledger instead of being handed it.
        """
        require_vocabulary("outcome", outcome, ITERATION_OUTCOMES)
        index = self._index_of(iteration_identity)
        entry = self._entries[index]
        if entry.is_settled:
            raise ExecutionRefused(
                "iteration_already_settled", "the iteration is already settled"
            )
        if resulting_carry_digest is not None and not self.plan.carry_enabled:
            raise ExecutionContractError(
                "invalid_settlement", "a carry digest requires a carrying plan"
            )
        # Every remaining coherence rule lives in the entry, so it is checked in one
        # place and a hand-built ledger is held to exactly the same standard.
        settled = replace(
            entry,
            outcome=outcome,
            outputs_digest=outputs_digest,
            failure_reason=failure_reason,
            settlement_sequence=self._sequence + 1,
            resulting_carry_digest=resulting_carry_digest,
            cancellation_disposition=(
                DISPOSITION_CANCELLED_IN_FLIGHT
                if outcome == ITERATION_CANCELLED
                else None
            ),
        )
        self._sequence += 1
        self._sequences.add(self._sequence)
        self._entries[index] = settled
        if resulting_carry_digest is not None:
            self._carry_digest = resulting_carry_digest
        if outcome == OUTCOME_FAILED:
            self._apply_partial_success()
        self._refresh()
        return settled

    def _apply_partial_success(self) -> None:
        policy = self.plan.partial_success_policy
        if policy == RECORD_AND_CONTINUE:
            return
        if self._state == LOOP_RUNNING:
            self._state = LOOP_STOPPING
        if policy == FAIL_LOOP and self._failure_reason is None:
            self._failure_reason = "iteration_failed"

    # -- cancellation ------------------------------------------------------

    def cancel(self) -> tuple[IterationLedgerEntry, ...]:
        """Apply the plan's declared cancellation policy, and return what it settled.

        ``CANCEL_REMAINING`` stops launching and settles every in-flight iteration as
        ``CANCELLED``; ``DRAIN_IN_FLIGHT`` stops launching and leaves the in-flight
        ones to settle normally; ``COMPLETE_ALL`` records the request and lets the
        loop finish. None of the three unwinds a recorded effect settlement:
        cancellation is not rollback.
        """
        self._cancellation_requested = True
        if self.plan.cancellation_policy == COMPLETE_ALL:
            return ()
        if self._state == LOOP_RUNNING:
            self._state = LOOP_STOPPING
        cancelled: list[IterationLedgerEntry] = []
        if self.plan.cancellation_policy == CANCEL_REMAINING:
            for index, entry in enumerate(self._entries):
                if entry.is_settled:
                    continue
                self._sequence += 1
                self._sequences.add(self._sequence)
                self._entries[index] = replace(
                    entry,
                    outcome=ITERATION_CANCELLED,
                    settlement_sequence=self._sequence,
                    cancellation_disposition=DISPOSITION_CANCELLED_IN_FLIGHT,
                )
                cancelled.append(self._entries[index])
        self._refresh()
        return tuple(cancelled)

    # -- late results ------------------------------------------------------

    def record_late_result(
        self, iteration_identity: str, *, evidence_ref: str
    ) -> LateResult:
        """Record a result that arrived after the loop settled, as Evidence only.

        The ledger is not touched and no Run state is written. A late result names
        its iteration identity and its Evidence, and that is the whole of its effect
        on the Run. A result for an iteration this loop never launched, and a second
        result for one that already has one, both fail closed.
        """
        return self._admit_late(
            LateResult(iteration_identity=iteration_identity, evidence_ref=evidence_ref)
        )

    def _admit_late(self, late: LateResult) -> LateResult:
        """Admit one late result against the ledger without mutating it."""
        if not self.settled:
            raise ExecutionRefused(
                "loop_not_settled", "a result before the loop settles is not late"
            )
        self._index_of(late.iteration_identity)
        if any(
            recorded.iteration_identity == late.iteration_identity
            for recorded in self._late
        ):
            raise ExecutionRefused(
                "duplicate_late_result",
                "the iteration already records a late result",
            )
        self._late.append(late)
        return late

    # -- settlement of the loop itself ------------------------------------

    def _refresh(self) -> None:
        if self._state == LOOP_SETTLED or self.in_flight:
            return
        launched = len(self._entries)
        reachable = min(len(self._elements), self.plan.maximum_iterations)
        if self._state == LOOP_STOPPING or launched >= reachable:
            self._state = LOOP_SETTLED
        if self._state != LOOP_SETTLED:
            return
        if (
            self._failure_reason is None
            and len(self._elements) > self.plan.maximum_iterations
            and launched >= self.plan.maximum_iterations
            and not self._cancellation_requested
        ):
            # An iteration beyond the frozen bound fails the loop; it never extends it.
            self._failure_reason = "maximum_iterations_exceeded"


def replay_ledger(
    plan: FrozenLoopPlan,
    source: tuple[LoopElement, ...],
    ledger: tuple[IterationLedgerEntry, ...],
    *,
    zip_sources: tuple[tuple[LoopElement, ...], ...] = (),
    carry_digest: str | None = None,
    late_results: tuple[LateResult, ...] = (),
) -> LoopRunner:
    """Return a runner resumed from ``ledger``: same identities, same carry, no relaunch."""
    return LoopRunner(
        plan,
        source,
        zip_sources=zip_sources,
        ledger=ledger,
        carry_digest=carry_digest,
        late_results=late_results,
    )


__all__ = [
    "CANCEL_REMAINING",
    "COMPLETE_ALL",
    "DISPOSITION_CANCELLED_IN_FLIGHT",
    "DRAIN_IN_FLIGHT",
    "FAIL_LOOP",
    "ITERATION_CANCELLED",
    "ITERATION_OUTCOMES",
    "ITERATION_SKIPPED",
    "LOOP_CANCELLATION_POLICIES",
    "LOOP_MODES",
    "LOOP_MODE_PARALLEL",
    "LOOP_MODE_SEQUENTIAL",
    "LOOP_ORDER_GUARANTEES",
    "LOOP_PARTIAL_SUCCESS_POLICIES",
    "LOOP_RUNNING",
    "LOOP_SETTLED",
    "LOOP_STATES",
    "LOOP_STOPPING",
    "LOOP_ZIP_MISMATCH_POLICIES",
    "ORDER_ITERATION_IDENTITY",
    "ORDER_SETTLEMENT",
    "RECORD_AND_CONTINUE",
    "RECORD_AND_STOP_AFTER_CURRENT",
    "ZIP_PAD_WITH_ABSENT",
    "ZIP_REFUSE",
    "ZIP_TRUNCATE_TO_SHORTEST",
    "FrozenLoopPlan",
    "IterationLaunch",
    "IterationLedgerEntry",
    "LateResult",
    "LoopElement",
    "LoopRunner",
    "replay_ledger",
]
