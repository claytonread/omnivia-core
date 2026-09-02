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

The ledger is the recovery record, and it is the *only* recovery record -- which is
why the loop's own cancellation posture and its late results are members of
:class:`LoopLedger` and not state beside the runner. Replaying a ledger re-derives every
recorded identity, refuses one that is not contiguous in ordinal launch order or whose
settlement sequences are not ``1..n``, refuses a history no live runner could have
written -- a cancelled iteration under a policy that never cancels one, an in-flight
population past the frozen concurrency bound -- never relaunches an iteration it already
records, replays the recorded cancellation through the plan's declared policy, and folds
the carry forward out of the recorded entries themselves. So a resumed runner does not
need an ambient caller to hand it back the carry, the cancellation or the late results it
had before the crash, and it refuses a caller-supplied carry that disagrees with the one
the ledger replays.

And the ledger is durable rather than merely in-process. :func:`loop_ledger_document` and
:func:`loop_ledger_from_document` are its closed I-JSON form, round-tripped through RFC
8785 canonical bytes; :func:`to_loop_iteration_ledger` and
:func:`from_loop_iteration_ledger` are the lossless seam to the Core
`LoopIterationLedger`, which carries the same facts plus the wall-clock members a pure
oracle never holds. Handing a runner its own object graph back and calling that a restore
proves nothing: nothing was ever serialized, so every member survives trivially.

Two deliberate absences. The plan's ``done`` condition is not evaluated here --
a predicate over run values needs the run values, which this seam never holds --
so completion is driven by the bound source and the frozen bounds. And settlement
is ordered by a monotonic sequence rather than a clock, because a pure oracle has
no clock and a replay has to reproduce the order exactly; the wall-clock
``settledAt`` instant belongs to the Core `LoopIterationLedger` record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core.contracts.v1.semantics_workflow import (
    compute_scheduling_intents_digest,
)
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

#: The loop's *own* terminal outcome, which is a different question from any one
#: iteration's. It is stated rather than inferred, because inferring it from a null
#: failure reason is exactly what let a cancelled, truncated loop read as a clean
#: success and hand back a partial gather as if it were the whole one.
LOOP_OUTCOME_SUCCEEDED: Final = "SUCCEEDED"
LOOP_OUTCOME_FAILED: Final = "FAILED"
LOOP_OUTCOME_CANCELLED: Final = "CANCELLED"
LOOP_OUTCOME_INCOMPLETE: Final = "INCOMPLETE"

LOOP_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        LOOP_OUTCOME_SUCCEEDED,
        LOOP_OUTCOME_FAILED,
        LOOP_OUTCOME_CANCELLED,
        LOOP_OUTCOME_INCOMPLETE,
    }
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

#: The three classes one settled effect may be recorded in, spelled exactly as the Core
#: `EffectSettlement` spells them, so the runtime record and the durable one name the
#: same three things rather than two vocabularies that have to be mapped.
SETTLEMENT_COMMITTED: Final = "committed"
SETTLEMENT_NOT_COMMITTED: Final = "not_committed"
SETTLEMENT_UNKNOWN: Final = "unknown"

EFFECT_SETTLEMENT_CLASSES: Final[frozenset[str]] = frozenset(
    {SETTLEMENT_COMMITTED, SETTLEMENT_NOT_COMMITTED, SETTLEMENT_UNKNOWN}
)

#: The exact version this seam writes and reads its canonical ledger document as. A
#: document is refused unless it names this, because a record whose shape is guessed is
#: not a durable record.
LOOP_LEDGER_SCHEMA_VERSION: Final = "1.0.0"

#: The bound on one iteration's recorded effect settlements. The same bound
#: :func:`~omnivia_core_runtime.execution.profile.require_collection` puts on every other
#: declared collection in this seam, restated here because these entries are records
#: rather than identifiers and so cannot go through it.
_MAX_EFFECT_SETTLEMENTS: Final = 64


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
class EffectSettlement:
    """One effect this iteration settled: which effect, in which class, on what evidence.

    The same four members the Core `EffectSettlement` carries, and held to the same rule:
    a verified receipt exists exactly for a `committed` settlement, because a receipt is
    the thing that *makes* a settlement committed, and a `not_committed` or `unknown` one
    carrying a receipt is two contradictory claims about one effect.

    Recording only the effect request id -- which is what this seam used to do -- loses
    exactly the members a durable record needs: replayed from a bare id, every settled
    effect reads as the same unclassified fact, and an `unknown` disposition becomes
    indistinguishable from a committed one with its receipt thrown away.
    """

    effect_request_id: str
    settlement_class: str
    completion_contribution: str
    verified_receipt_ref: str | None = None

    def __post_init__(self) -> None:
        require_identifier("effect_request_id", self.effect_request_id)
        require_vocabulary(
            "settlement_class", self.settlement_class, EFFECT_SETTLEMENT_CLASSES
        )
        require_identifier("completion_contribution", self.completion_contribution)
        committed = self.settlement_class == SETTLEMENT_COMMITTED
        if committed != (self.verified_receipt_ref is not None):
            raise ExecutionContractError(
                "invalid_effect_settlement",
                "a verified receipt is recorded exactly for a committed settlement",
            )
        if self.verified_receipt_ref is not None:
            require_identifier("verified_receipt_ref", self.verified_receipt_ref)


def _require_effect_settlements(
    values: tuple[EffectSettlement, ...],
) -> tuple[EffectSettlement, ...]:
    """Bound one entry's settlements and refuse a second settlement of one effect."""
    if not isinstance(values, tuple) or not all(
        isinstance(value, EffectSettlement) for value in values
    ):
        raise ExecutionContractError(
            "invalid_effect_settlement",
            "effect_settlements is not a tuple of EffectSettlement",
        )
    if len(values) > _MAX_EFFECT_SETTLEMENTS:
        raise ExecutionContractError(
            "collection_too_large", "effect_settlements exceeds its bound"
        )
    requests = [value.effect_request_id for value in values]
    if len(set(requests)) != len(requests):
        raise ExecutionContractError(
            "duplicate_entry", "effect_settlements settles the same effect twice"
        )
    return values


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
    effect_settlements: tuple[EffectSettlement, ...] = ()
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
        _require_effect_settlements(self.effect_settlements)
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


@dataclass(frozen=True, slots=True)
class LoopLedger:
    """The whole durable recovery record: the loop's own posture, and its entries.

    Cancellation is a fact about the *loop* rather than about any one iteration -- a
    ``DRAIN_IN_FLIGHT`` cancellation settles nothing at all, so no entry can carry it --
    which is why it lives here beside the entries and not in the process that requested
    it. A process-only cancellation is lost in the crash it is meant to survive, and the
    resumed loop then plans exactly the launches the cancellation stopped.

    Handing this one value back to :func:`replay_ledger` is the whole of a resume, which
    is what keeps the ledger the only recovery record: there is no second thing a caller
    can forget to carry across the crash.

    Late results live here for exactly that reason. They were a separate argument to the
    runner, which made them a second thing to carry: a resume that dropped them replayed a
    settled loop that had never seen a late result, and admitted the same one again as if
    it were new. The carry is not a member because it is *derived* -- every launch records
    the carry it consumed and every settled iteration the carry it produced, so the loop's
    current carry folds out of the entries and cannot disagree with them.
    """

    entries: tuple[IterationLedgerEntry, ...] = ()
    cancellation_requested: bool = False
    late_results: tuple[LateResult, ...] = ()


#: The record of a loop that has not started: no entries, and nothing cancelled. It is a
#: module-level value because :class:`LoopLedger` is frozen, so one instance is every
#: instance of it, and because a default that is a call is a default nobody can share.
EMPTY_LOOP_LEDGER: Final = LoopLedger()


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

    The ledger carries the loop's own cancellation posture beside its entries, and a
    resume replays it through the plan's declared cancellation policy, so a resumed
    ``CANCEL_REMAINING`` or ``DRAIN_IN_FLIGHT`` loop plans no launch its live runner
    would not have planned, and a resumed ``COMPLETE_ALL`` loop still finishes.

    It carries the late results too, which is why there is no argument for them. They
    were one, and a resume that dropped it replayed a settled loop that had never seen a
    late result -- and then admitted the same one again as if it were new.
    """

    def __init__(
        self,
        plan: FrozenLoopPlan,
        source: tuple[LoopElement, ...],
        *,
        zip_sources: tuple[tuple[LoopElement, ...], ...] = (),
        ledger: LoopLedger = EMPTY_LOOP_LEDGER,
        carry_digest: str | None = None,
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
        elif plan.carry_enabled and not ledger.entries:
            raise ExecutionContractError(
                "invalid_loop_plan",
                "a new carrying loop requires its frozen initial carry digest",
            )

        for entry in ledger.entries:
            self._replay(entry)
        self._require_replayable_ledger(ledger.cancellation_requested)
        if ledger.cancellation_requested:
            # The recorded posture, applied through the same policy the live runner
            # applied it through, so the two cannot drift: a sweep that already settled
            # every in-flight iteration finds nothing left to settle and changes no byte
            # of the ledger, and one interrupted mid-sweep completes rather than resuming
            # as an uncancelled loop.
            self.cancel()
        self._refresh()
        for late in ledger.late_results:
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

    def _require_replayable_ledger(self, cancellation_requested: bool) -> None:
        """Refuse a ledger no live runner could have produced.

        A live runner numbers settlements ``1, 2, 3, ...`` with no gap and no repeat, so
        a ledger carrying a hole, a repeat or a sequence beyond the settlements it
        records is not the order anything settled in -- and under ``SETTLEMENT_ORDER``
        that order *is* the gather order, so believing it would silently reorder results.

        A cancelled iteration is likewise something only a cancelled loop produces, since
        :meth:`settle` refuses ``CANCELLED`` and :meth:`cancel` is the one thing that
        records it. A ledger carrying cancelled entries under a posture that says nothing
        was cancelled is the same fail-open as a lost cancellation and is refused for the
        same reason: believed, it resumes as a running loop and plans exactly the launches
        the cancellation stopped. The Core `LoopIterationLedger` refuses that shape too,
        so the durable record and this replay agree on what a cancellation looks like.

        The frozen concurrency bound is a bound on the live runner, so it is a bound on
        any ledger a live runner could have written. A recorded population of unsettled
        iterations larger than ``maximum_concurrency`` is a history :meth:`record_launch`
        refuses to produce, and believing it would resume a loop already over its bound
        and then let it launch further from there.

        A ``CANCEL_REMAINING`` sweep is the last thing that settles: it cancels every
        iteration still in flight in one pass and the loop launches nothing after it. So
        a ledger that settles anything else after the first cancellation records a
        settlement that happened after the loop stopped, which is not a thing that
        happens.
        """
        if self._sequences != set(range(1, len(self._sequences) + 1)):
            raise ExecutionRefused(
                "settlement_not_contiguous",
                "the ledger's settlement sequences are not 1..n without gap or repeat",
            )
        in_flight = sum(1 for entry in self._entries if not entry.is_settled)
        if in_flight > self.plan.maximum_concurrency:
            raise ExecutionRefused(
                "concurrency_exhausted",
                "the ledger records more iterations in flight than the frozen bound",
            )
        cancelled = [
            entry for entry in self._entries if entry.outcome == ITERATION_CANCELLED
        ]
        if cancelled and not cancellation_requested:
            raise ExecutionRefused(
                "cancellation_not_recorded",
                "the ledger settles an iteration cancelled but records no cancellation",
            )
        if cancelled and self.plan.cancellation_policy != CANCEL_REMAINING:
            # `DRAIN_IN_FLIGHT` leaves the in-flight iterations to settle normally and
            # `COMPLETE_ALL` lets the loop finish, so neither policy has any path that
            # produces a cancelled entry -- :meth:`cancel` only sweeps under
            # `CANCEL_REMAINING` and :meth:`settle` refuses `CANCELLED` outright. A
            # ledger carrying one under either policy was not written by this seam, and
            # resuming it would replay a settlement its own plan cannot account for.
            raise ExecutionRefused(
                "cancellation_not_possible",
                f"a {self.plan.cancellation_policy} plan settles no iteration cancelled",
            )
        swept = [
            entry.settlement_sequence
            for entry in cancelled
            if entry.settlement_sequence is not None
        ]
        if not swept:
            return
        first_cancellation = min(swept)
        for entry in self._entries:
            if (
                entry.settlement_sequence is not None
                and entry.settlement_sequence > first_cancellation
                and entry.outcome != ITERATION_CANCELLED
            ):
                raise ExecutionRefused(
                    "settlement_after_stop",
                    "the ledger settles an iteration after the cancellation swept the loop",
                )

    # -- observation -------------------------------------------------------

    @property
    def ledger(self) -> LoopLedger:
        """The complete recovery record: the loop's posture, and its entries in ordinal
        launch order.

        Handing exactly this value back to :func:`replay_ledger` reconstructs this
        runner, which is the whole of the recovery story.
        """
        return LoopLedger(
            tuple(self._entries), self._cancellation_requested, tuple(self._late)
        )

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

    @property
    def outcome(self) -> str | None:
        """The settled loop's own terminal outcome, or ``None`` while it still runs.

        ``FAILED`` covers both the failure a ``FAIL_LOOP`` plan records and a loop
        truncated by its own frozen iteration bound. ``CANCELLED`` is the declared
        outcome of a cancellation, which is not a failure and does not pretend to be one.
        ``INCOMPLETE`` is the loop that neither failed nor was cancelled and still did
        not run clean: a ``RECORD_AND_STOP_AFTER_CURRENT`` plan that stopped on a failed
        iteration, or a ``RECORD_AND_CONTINUE`` plan that finished with one recorded.
        Their partial-success policies are preserved exactly -- neither loop *failed* --
        but neither is a clean success either, and the gather each hands back is a
        prefix of the answer rather than the answer.

        ``SUCCEEDED`` is therefore the narrow claim it sounds like: every iteration this
        plan could reach was launched, and none of them failed. A caller reads this
        rather than inferring success from a null failure reason, which is what let a
        truncated loop pass for a complete one.
        """
        if self._state != LOOP_SETTLED:
            return None
        if self._failure_reason is not None:
            return LOOP_OUTCOME_FAILED
        if self._cancellation_requested:
            return LOOP_OUTCOME_CANCELLED
        reachable = min(len(self._elements), self.plan.maximum_iterations)
        if len(self._entries) < reachable or any(
            entry.outcome == OUTCOME_FAILED for entry in self._entries
        ):
            return LOOP_OUTCOME_INCOMPLETE
        return LOOP_OUTCOME_SUCCEEDED

    def gather(self, *, partial: bool = False) -> tuple[str, ...]:
        """Return the succeeded iterations' output digests in the guaranteed order.

        A failed loop has no gather to hand out at all. Its succeeded iterations are
        still in the ledger and still readable there, but the gather is the loop's
        *result*, and handing back the surviving prefix of a loop that failed or was
        truncated by its own bound presents an incomplete answer as a complete one.

        An ``INCOMPLETE`` loop is refused too, and ``partial=True`` is how a caller asks
        for its prefix anyway. That keeps ``RECORD_AND_CONTINUE`` and
        ``RECORD_AND_STOP_AFTER_CURRENT`` exactly as declared -- their partial result is
        still consumable -- while making the caller state that a partial is what it is
        taking, which is the whole difference between a declared partial and a truncated
        loop mistaken for a complete one.

        A ``CANCELLED`` loop is neither: a partial gather is precisely what
        ``CANCEL_REMAINING`` and ``DRAIN_IN_FLIGHT`` declare, and :attr:`outcome` says so
        without the caller having to ask. Nor is a loop still running, whose gather is a
        reading of the ledger so far rather than a result.
        """
        if self._failure_reason is not None:
            raise ExecutionRefused(
                "loop_failed",
                f"a loop that failed with {self._failure_reason!r} has no gather",
            )
        if not partial and self.outcome == LOOP_OUTCOME_INCOMPLETE:
            raise ExecutionRefused(
                "loop_incomplete",
                "a loop that did not run every iteration clean has no whole gather",
            )
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
        self, iteration_identity: str, settlement: EffectSettlement
    ) -> None:
        """Record one effect this iteration has already settled.

        Recorded while the iteration is in flight, and never removed afterwards:
        cancellation is not rollback, so an effect already issued stays recorded
        against its iteration whatever the iteration goes on to settle as. The same
        effect request is never recorded twice, because the iteration identity plus
        the effect request id is the idempotency key for that settlement -- so a
        redelivery of the same settlement refuses rather than appending a second one
        that would then disagree with the first about its class or its receipt.
        """
        if not isinstance(settlement, EffectSettlement):
            raise ExecutionContractError(
                "invalid_effect_settlement", "settlement is not an EffectSettlement"
            )
        index = self._index_of(iteration_identity)
        entry = self._entries[index]
        if entry.is_settled:
            raise ExecutionRefused(
                "iteration_already_settled",
                "a settled iteration records no further effect",
            )
        if any(
            recorded.effect_request_id == settlement.effect_request_id
            for recorded in entry.effect_settlements
        ):
            raise ExecutionRefused(
                "duplicate_effect_settlement",
                "the iteration already records this effect settlement",
            )
        self._entries[index] = replace(
            entry, effect_settlements=(*entry.effect_settlements, settlement)
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

        ``CANCELLED`` is not settleable here. An iteration is cancelled by the loop's
        declared cancellation policy through :meth:`cancel` and by nothing else, so that
        a cancelled entry in the ledger is always the record of a cancelled *loop* --
        which is what makes the loop-level posture reconstructible from the ledger
        rather than a claim the ledger cannot support. An iteration deliberately not run
        is ``SKIPPED``.
        """
        require_vocabulary("outcome", outcome, ITERATION_OUTCOMES)
        if outcome == ITERATION_CANCELLED:
            raise ExecutionRefused(
                "cancellation_not_a_settlement",
                "an iteration is cancelled by the loop's cancellation policy, not settled cancelled",
            )
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

        Idempotent, which is what lets a resume reconstruct the recorded posture by
        replaying this same method: a second call over an already-swept ledger settles
        nothing further and leaves every recorded byte where it was.
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
        ):
            # An iteration beyond the frozen bound fails the loop; it never extends it.
            # Independent of cancellation, because the bound is a property of the plan
            # and not of what the caller did afterwards: a source the plan cannot cover
            # was over-long before anyone cancelled anything, and letting a cancellation
            # clear the overflow made a truncated loop settle clean live and fail on
            # replay of its own ledger.
            self._failure_reason = "maximum_iterations_exceeded"


def replay_ledger(
    plan: FrozenLoopPlan,
    source: tuple[LoopElement, ...],
    ledger: LoopLedger,
    *,
    zip_sources: tuple[tuple[LoopElement, ...], ...] = (),
    carry_digest: str | None = None,
) -> LoopRunner:
    """Return a runner resumed from ``ledger``: same identities, same carry, same posture,
    same late results, no relaunch."""
    return LoopRunner(
        plan,
        source,
        zip_sources=zip_sources,
        ledger=ledger,
        carry_digest=carry_digest,
    )


# --------------------------------------------------------------------------
# The canonical document, and the seam to the Core `LoopIterationLedger`
# --------------------------------------------------------------------------
#
# Two conversions, one record. :func:`loop_ledger_document` and
# :func:`loop_ledger_from_document` are the *durable* form: one closed I-JSON document
# that survives a process boundary, holding every member a resume reads and nothing a
# resume has to be handed separately. :func:`to_loop_iteration_ledger` and
# :func:`from_loop_iteration_ledger` are the seam to the Core record, which carries the
# same facts plus the three wall-clock members a clock-free oracle never holds.
#
# Handing a runner's own object graph to a "restore" and calling that durability proves
# nothing: the objects were never serialized, so every member survives trivially,
# including the ones no serializer would have written. Both directions here go through
# RFC 8785 canonical bytes for exactly that reason.


def _member(fields: Mapping[str, object], key: str, label: str) -> object:
    if key not in fields:
        raise ExecutionContractError("invalid_document", f"{label} is missing {key}")
    return fields[key]


def _document(value: object, label: str, allowed: frozenset[str]) -> Mapping[str, object]:
    """Return ``value`` as a closed mapping, refusing an unknown or non-string key."""
    if not isinstance(value, Mapping):
        raise ExecutionContractError("invalid_document", f"{label} is not a mapping")
    unknown = sorted(key for key in value if not isinstance(key, str) or key not in allowed)
    if unknown:
        raise ExecutionContractError(
            "invalid_document", f"{label} carries unknown members {unknown!r}"
        )
    return value


def _text(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _member(fields, key, label)
    if not isinstance(value, str):
        raise ExecutionContractError("invalid_document", f"{label}.{key} is not a string")
    return value


def _optional_text(fields: Mapping[str, object], key: str, label: str) -> str | None:
    value = _member(fields, key, label)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionContractError(
            "invalid_document", f"{label}.{key} is not a string or null"
        )
    return value


def _flag(fields: Mapping[str, object], key: str, label: str) -> bool:
    value = _member(fields, key, label)
    if not isinstance(value, bool):
        raise ExecutionContractError("invalid_document", f"{label}.{key} is not a boolean")
    return value


def _count(fields: Mapping[str, object], key: str, label: str) -> int:
    value = _member(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionContractError("invalid_document", f"{label}.{key} is not an integer")
    return value


def _optional_count(fields: Mapping[str, object], key: str, label: str) -> int | None:
    value = _member(fields, key, label)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionContractError(
            "invalid_document", f"{label}.{key} is not an integer or null"
        )
    return value


def _array(fields: Mapping[str, object], key: str, label: str) -> Sequence[object]:
    value = _member(fields, key, label)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecutionContractError("invalid_document", f"{label}.{key} is not an array")
    return value


def _texts(fields: Mapping[str, object], key: str, label: str) -> tuple[str, ...]:
    values = _array(fields, key, label)
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ExecutionContractError(
                "invalid_document", f"{label}.{key}[{index}] is not a string"
            )
    return tuple(str(value) for value in values)


_LEDGER_MEMBERS: Final[frozenset[str]] = frozenset(
    {"ledgerSchemaVersion", "cancellationRequested", "entries", "lateResults"}
)
_ENTRY_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "iterationIdentity",
        "ordinal",
        "inputsDigest",
        "carryDigest",
        "schedulingIntents",
        "outcome",
        "outputsDigest",
        "failureReason",
        "settlementSequence",
        "effectSettlements",
        "cancellationDisposition",
        "resultingCarryDigest",
    }
)
_SETTLEMENT_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "effectRequestId",
        "settlementClass",
        "completionContribution",
        "verifiedReceiptRef",
    }
)
_LATE_MEMBERS: Final[frozenset[str]] = frozenset(
    {"iterationIdentity", "evidenceRef", "appliedToRunState"}
)


def _settlement_document(settlement: EffectSettlement) -> dict[str, object]:
    return {
        "effectRequestId": settlement.effect_request_id,
        "settlementClass": settlement.settlement_class,
        "completionContribution": settlement.completion_contribution,
        "verifiedReceiptRef": settlement.verified_receipt_ref,
    }


def _entry_document(entry: IterationLedgerEntry) -> dict[str, object]:
    return {
        "iterationIdentity": entry.iteration_identity,
        "ordinal": entry.ordinal,
        "inputsDigest": entry.inputs_digest,
        "carryDigest": entry.carry_digest,
        "schedulingIntents": list(entry.scheduling_intents),
        "outcome": entry.outcome,
        "outputsDigest": entry.outputs_digest,
        "failureReason": entry.failure_reason,
        "settlementSequence": entry.settlement_sequence,
        "effectSettlements": [
            _settlement_document(settlement) for settlement in entry.effect_settlements
        ],
        "cancellationDisposition": entry.cancellation_disposition,
        "resultingCarryDigest": entry.resulting_carry_digest,
    }


def loop_ledger_document(ledger: LoopLedger) -> dict[str, object]:
    """Return the whole recovery record as one closed I-JSON document.

    Every member is written every time, ``null`` included, so the document's shape is a
    property of the schema version rather than of which fields this particular loop
    happened to use -- and so two ledgers that differ only in an absent member serialize
    to two documents rather than to the same one.
    """
    if not isinstance(ledger, LoopLedger):
        raise ExecutionContractError("invalid_ledger", "ledger is not a LoopLedger")
    return {
        "ledgerSchemaVersion": LOOP_LEDGER_SCHEMA_VERSION,
        "cancellationRequested": ledger.cancellation_requested,
        "entries": [_entry_document(entry) for entry in ledger.entries],
        "lateResults": [
            {
                "iterationIdentity": late.iteration_identity,
                "evidenceRef": late.evidence_ref,
                "appliedToRunState": late.applied_to_run_state,
            }
            for late in ledger.late_results
        ],
    }


def loop_ledger_from_document(document: object) -> LoopLedger:
    """Rebuild the recovery record from its canonical document, or refuse it.

    Every value is re-validated on the way in by the same ``__post_init__`` a live runner
    builds through, so a restored ledger is held to exactly the rules a produced one is.
    A document naming another schema version is refused rather than read on a guess.
    """
    label = "LoopLedgerDocument"
    fields = _document(document, label, _LEDGER_MEMBERS)
    version = _text(fields, "ledgerSchemaVersion", label)
    if version != LOOP_LEDGER_SCHEMA_VERSION:
        raise ExecutionContractError(
            "unsupported_schema_version",
            f"{label}.ledgerSchemaVersion {version!r} is not {LOOP_LEDGER_SCHEMA_VERSION}",
        )
    entries = tuple(
        _entry_from_document(entry, f"{label}.entries[{index}]")
        for index, entry in enumerate(_array(fields, "entries", label))
    )
    late_results = tuple(
        _late_from_document(late, f"{label}.lateResults[{index}]")
        for index, late in enumerate(_array(fields, "lateResults", label))
    )
    return LoopLedger(
        entries=entries,
        cancellation_requested=_flag(fields, "cancellationRequested", label),
        late_results=late_results,
    )


def _entry_from_document(value: object, label: str) -> IterationLedgerEntry:
    fields = _document(value, label, _ENTRY_MEMBERS)
    return IterationLedgerEntry(
        iteration_identity=_text(fields, "iterationIdentity", label),
        ordinal=_count(fields, "ordinal", label),
        inputs_digest=_text(fields, "inputsDigest", label),
        carry_digest=_optional_text(fields, "carryDigest", label),
        scheduling_intents=_texts(fields, "schedulingIntents", label),
        outcome=_optional_text(fields, "outcome", label),
        outputs_digest=_optional_text(fields, "outputsDigest", label),
        failure_reason=_optional_text(fields, "failureReason", label),
        settlement_sequence=_optional_count(fields, "settlementSequence", label),
        effect_settlements=tuple(
            _settlement_from_document(entry, f"{label}.effectSettlements[{index}]")
            for index, entry in enumerate(_array(fields, "effectSettlements", label))
        ),
        cancellation_disposition=_optional_text(fields, "cancellationDisposition", label),
        resulting_carry_digest=_optional_text(fields, "resultingCarryDigest", label),
    )


def _settlement_from_document(value: object, label: str) -> EffectSettlement:
    fields = _document(value, label, _SETTLEMENT_MEMBERS)
    return EffectSettlement(
        effect_request_id=_text(fields, "effectRequestId", label),
        settlement_class=_text(fields, "settlementClass", label),
        completion_contribution=_text(fields, "completionContribution", label),
        verified_receipt_ref=_optional_text(fields, "verifiedReceiptRef", label),
    )


def _late_from_document(value: object, label: str) -> LateResult:
    fields = _document(value, label, _LATE_MEMBERS)
    return LateResult(
        iteration_identity=_text(fields, "iterationIdentity", label),
        evidence_ref=_text(fields, "evidenceRef", label),
        applied_to_run_state=_flag(fields, "appliedToRunState", label),
    )


#: Runtime outcome to the Core `LoopIterationLedger` outcome class, and back. The two
#: vocabularies are the same four facts in two spellings, so the mapping is stated once
#: rather than reimplemented at each call.
_CORE_OUTCOME_CLASSES: Final[Mapping[str, str]] = {
    OUTCOME_SUCCEEDED: "succeeded",
    OUTCOME_FAILED: "failed",
    ITERATION_CANCELLED: "cancelled",
    ITERATION_SKIPPED: "skipped",
}
_RUNTIME_OUTCOMES: Final[Mapping[str, str]] = {
    core: runtime for runtime, core in _CORE_OUTCOME_CLASSES.items()
}


@dataclass(frozen=True, slots=True)
class IterationWitness:
    """The three members the Core record carries and this clock-free oracle never holds.

    A pure oracle has no clock and orders settlement by a sequence, and it never writes a
    bundle, so the launch's bundle reference and the two wall-clock instants have to come
    from the writer that did. They are one explicit, required argument per iteration
    rather than an ambient default, because a conversion that invented an instant would be
    making up part of the durable record.
    """

    launch_bundle_ref: str
    launched_at: str
    settled_at: str

    def __post_init__(self) -> None:
        require_identifier("launch_bundle_ref", self.launch_bundle_ref)
        for name in ("launched_at", "settled_at"):
            value: str = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ExecutionContractError(
                    "invalid_witness", f"{name} is not a recorded instant"
                )


def _reference(identity: str) -> dict[str, object]:
    return {"referenceId": identity}


def _settlement_from_record(value: object, label: str) -> EffectSettlement:
    """One Core `EffectSettlement` back as the runtime value it converts to."""
    if not isinstance(value, Mapping):
        raise ExecutionContractError("invalid_document", f"{label} is not a mapping")
    receipt = value.get("verifiedReceiptRef")
    return EffectSettlement(
        effect_request_id=_text(value, "effectRequestId", label),
        settlement_class=_text(value, "settlementClass", label),
        completion_contribution=_referenced(
            value.get("completionContribution"), f"{label}.completionContribution"
        ),
        verified_receipt_ref=(
            None
            if receipt is None
            else _referenced(receipt, f"{label}.verifiedReceiptRef")
        ),
    )


def _referenced(value: object, label: str) -> str:
    reference = value if isinstance(value, Mapping) else None
    identity = None if reference is None else reference.get("referenceId")
    if not isinstance(identity, str):
        raise ExecutionContractError(
            "invalid_reference", f"{label} does not name a referenceId"
        )
    return identity


def to_loop_iteration_ledger(
    plan: FrozenLoopPlan,
    ledger: LoopLedger,
    *,
    witnesses: Mapping[str, IterationWitness],
    loop_settled_at: str,
) -> dict[str, object]:
    """Convert one recovery record into the Core `LoopIterationLedger` record.

    Lossless: every member of every entry crosses, including the two the Core record grew
    an additive home for -- the scheduling intents themselves beside their digest, and the
    carry a succeeded iteration produced. A late result crosses as `lateEvidence` on the
    entry it is about rather than as a second entry, because it always names an iteration
    this ledger already records and the Core record requires unique identities and
    contiguous ordinals.

    Refuses a ledger with an iteration still in flight. Every Core entry states an outcome
    class, a settlement sequence and a settled instant, so there is no shape for an
    unsettled iteration there; converting one would mean inventing a settlement.
    """
    if not isinstance(ledger, LoopLedger):
        raise ExecutionContractError("invalid_ledger", "ledger is not a LoopLedger")
    unsettled = [entry.iteration_identity for entry in ledger.entries if not entry.is_settled]
    if unsettled:
        raise ExecutionRefused(
            "loop_not_settled",
            "a ledger with an iteration still in flight has no Core record",
        )
    evidence = {
        late.iteration_identity: late.evidence_ref for late in ledger.late_results
    }
    entries: list[dict[str, object]] = []
    for entry in ledger.entries:
        witness = witnesses.get(entry.iteration_identity)
        if witness is None:
            raise ExecutionContractError(
                "missing_witness",
                "every recorded iteration needs its launch bundle and instants",
            )
        record: dict[str, object] = {
            "iterationIdentity": entry.iteration_identity,
            "ordinal": entry.ordinal,
            "settlementSequence": entry.settlement_sequence,
            "outcomeClass": _CORE_OUTCOME_CLASSES[str(entry.outcome)],
            "launchedAt": witness.launched_at,
            "launchBundleRef": _reference(witness.launch_bundle_ref),
            "inputsDigest": entry.inputs_digest,
            "schedulingIntents": list(entry.scheduling_intents),
            "schedulingIntentsDigest": compute_scheduling_intents_digest(
                list(entry.scheduling_intents)
            ),
            "settledAt": witness.settled_at,
            "effectSettlements": [
                {
                    "effectRequestId": settlement.effect_request_id,
                    "settlementClass": settlement.settlement_class,
                    "completionContribution": _reference(
                        settlement.completion_contribution
                    ),
                    **(
                        {
                            "verifiedReceiptRef": _reference(
                                settlement.verified_receipt_ref
                            )
                        }
                        if settlement.verified_receipt_ref is not None
                        else {}
                    ),
                }
                for settlement in entry.effect_settlements
            ],
        }
        if entry.carry_digest is not None:
            record["carryDigest"] = entry.carry_digest
        if entry.outputs_digest is not None:
            record["outputsDigest"] = entry.outputs_digest
        if entry.failure_reason is not None:
            record["failureRef"] = _reference(entry.failure_reason)
        if entry.resulting_carry_digest is not None:
            record["resultingCarryDigest"] = entry.resulting_carry_digest
        if entry.cancellation_disposition is not None:
            record["cancellationDisposition"] = entry.cancellation_disposition
        late_ref = evidence.get(entry.iteration_identity)
        if late_ref is not None:
            record["lateEvidence"] = {
                "evidenceRef": _reference(late_ref),
                "appliedToRunState": False,
            }
        entries.append(record)
    return {
        "ledgerSchemaVersion": LOOP_LEDGER_SCHEMA_VERSION,
        "loopStableId": plan.loop_stable_id,
        "cancellationRequested": ledger.cancellation_requested,
        "loopSettledAt": loop_settled_at,
        "entries": entries,
    }


def from_loop_iteration_ledger(record: object) -> LoopLedger:
    """Recover the runtime record from a Core `LoopIterationLedger`.

    The inverse of :func:`to_loop_iteration_ledger` over every runtime member, which is
    what "lossless" is worth stating as a function rather than as a claim: the two
    together are a round trip a test can assert on. The Core-only members -- the bundle
    reference and the two instants -- are read straight off the record and are not
    reconstructed here, because this seam has nowhere to put them.
    """
    label = "LoopIterationLedger"
    if not isinstance(record, Mapping):
        raise ExecutionContractError("invalid_document", f"{label} is not a mapping")
    entries: list[IterationLedgerEntry] = []
    late_results: list[LateResult] = []
    for index, value in enumerate(_array(record, "entries", label)):
        entry_label = f"{label}.entries[{index}]"
        if not isinstance(value, Mapping):
            raise ExecutionContractError(
                "invalid_document", f"{entry_label} is not a mapping"
            )
        outcome_class = _text(value, "outcomeClass", entry_label)
        if outcome_class not in _RUNTIME_OUTCOMES:
            raise ExecutionContractError(
                "invalid_document",
                f"{entry_label}.outcomeClass {outcome_class!r} has no runtime outcome",
            )
        failure = value.get("failureRef")
        settlements = tuple(
            _settlement_from_record(settlement, f"{entry_label}.effectSettlements[{at}]")
            for at, settlement in enumerate(
                _array(value, "effectSettlements", entry_label)
            )
        )
        entries.append(
            IterationLedgerEntry(
                iteration_identity=_text(value, "iterationIdentity", entry_label),
                ordinal=_count(value, "ordinal", entry_label),
                inputs_digest=_text(value, "inputsDigest", entry_label),
                carry_digest=(
                    None if "carryDigest" not in value else _text(value, "carryDigest", entry_label)
                ),
                scheduling_intents=_texts(value, "schedulingIntents", entry_label),
                outcome=_RUNTIME_OUTCOMES[outcome_class],
                outputs_digest=(
                    None
                    if "outputsDigest" not in value
                    else _text(value, "outputsDigest", entry_label)
                ),
                failure_reason=(
                    None
                    if failure is None
                    else _referenced(failure, f"{entry_label}.failureRef")
                ),
                settlement_sequence=_count(value, "settlementSequence", entry_label),
                effect_settlements=settlements,
                cancellation_disposition=(
                    None
                    if "cancellationDisposition" not in value
                    else _text(value, "cancellationDisposition", entry_label)
                ),
                resulting_carry_digest=(
                    None
                    if "resultingCarryDigest" not in value
                    else _text(value, "resultingCarryDigest", entry_label)
                ),
            )
        )
        late = value.get("lateEvidence")
        if late is not None:
            if not isinstance(late, Mapping):
                raise ExecutionContractError(
                    "invalid_document", f"{entry_label}.lateEvidence is not a mapping"
                )
            late_results.append(
                LateResult(
                    iteration_identity=_text(value, "iterationIdentity", entry_label),
                    evidence_ref=_referenced(
                        late.get("evidenceRef"), f"{entry_label}.lateEvidence.evidenceRef"
                    ),
                    applied_to_run_state=_flag(
                        late, "appliedToRunState", f"{entry_label}.lateEvidence"
                    ),
                )
            )
    return LoopLedger(
        entries=tuple(entries),
        cancellation_requested=_flag(record, "cancellationRequested", label),
        late_results=tuple(late_results),
    )


__all__ = [
    "CANCEL_REMAINING",
    "COMPLETE_ALL",
    "DISPOSITION_CANCELLED_IN_FLIGHT",
    "DRAIN_IN_FLIGHT",
    "EFFECT_SETTLEMENT_CLASSES",
    "EMPTY_LOOP_LEDGER",
    "FAIL_LOOP",
    "ITERATION_CANCELLED",
    "ITERATION_OUTCOMES",
    "ITERATION_SKIPPED",
    "LOOP_CANCELLATION_POLICIES",
    "LOOP_LEDGER_SCHEMA_VERSION",
    "LOOP_MODES",
    "LOOP_MODE_PARALLEL",
    "LOOP_MODE_SEQUENTIAL",
    "LOOP_ORDER_GUARANTEES",
    "LOOP_OUTCOMES",
    "LOOP_OUTCOME_CANCELLED",
    "LOOP_OUTCOME_FAILED",
    "LOOP_OUTCOME_INCOMPLETE",
    "LOOP_OUTCOME_SUCCEEDED",
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
    "SETTLEMENT_COMMITTED",
    "SETTLEMENT_NOT_COMMITTED",
    "SETTLEMENT_UNKNOWN",
    "ZIP_PAD_WITH_ABSENT",
    "ZIP_REFUSE",
    "ZIP_TRUNCATE_TO_SHORTEST",
    "EffectSettlement",
    "FrozenLoopPlan",
    "IterationLaunch",
    "IterationLedgerEntry",
    "IterationWitness",
    "LateResult",
    "LoopElement",
    "LoopLedger",
    "LoopRunner",
    "from_loop_iteration_ledger",
    "loop_ledger_document",
    "loop_ledger_from_document",
    "replay_ledger",
    "to_loop_iteration_ledger",
]
