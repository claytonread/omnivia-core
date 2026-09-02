"""Material-effect dispatch safety for one Attempt (T-0688 IP-03, WEFT-BL-002/003).

One Attempt wants to touch the world. Between it and the world sits exactly one
question this module answers: *may this Attempt issue this external effect right
now, and what is owed if the authoritative record of it goes missing.*

**This is not exactly-once delivery, and nothing here should be read as claiming
it.** An external effect that has left the process cannot be recalled -- not by a
newer fence generation, not by a rollback, not by reconciliation. What this seam
buys is narrower and honest: an effect is never issued a *second* time on the
strength of a commit that failed or became uncertain, and the divergence is held
as an explicit obligation until someone establishes the true disposition.

**The authoritative ledger is the only thing that gates dispatch.** Not the
journal, not process memory, not the absence of a receipt. A missing journal
commit is exactly the condition this seam exists to survive, so reading it as
"the effect did not happen" is the bug being prevented. Only
:data:`DISPATCHABLE_EFFECT_STATES` -- an identity predeclared, or one *proved*
untouched -- admits a dispatch; :data:`EFFECT_ISSUED_UNKNOWN` refuses forever,
because "we do not know" is not "no".

**The latch is durable, not remembered.** A stop is written through
:meth:`AuthoritativeStore.write_latch` before it is returned, and every operation
re-reads it. Constructing a fresh coordinator over the same store therefore
reconstructs the same refusal; a restart is not an amnesty.

**Retries are bounded, fixed at construction and counted.** Failed commits may be
retried up to :attr:`commit_retry_limit`; exhausting that budget latches with
reconciliation owed, because a commit that never landed leaves the disposition
indeterminate. An `uncertain` commit stops on the spot:
retrying uncertainty *is* the duplicate dispatch. Unknown computation classes,
unknown commit outcomes and unknown ledger states all fail closed, and an unknown
commit outcome latches, because a journal answer nobody can classify is at least
as dangerous as one that says `uncertain`.

**Every state-changing ledger write is a compare-and-set, not a read then a
write.** Two contenders on the same fence generation both read `predeclared`,
both conclude the identity is dispatchable, and -- with a blind write -- both
issue the effect. Fencing cannot separate them: neither is stale. So the gate is
:meth:`AuthoritativeStore.compare_and_set_effect_state`, which admits exactly the
one that moved the state, and the loser stops with :data:`STOP_EFFECT_STATE_RACED`
having issued nothing. The same seam is what stops a reconciler holding a stale
read from writing over an answer already given: it moves `issued_unknown` and
only `issued_unknown`, so a `settled_applied` can never be walked back into a
dispatchable state.

**Fencing rejects writes; it does not recall effects.** A stale generation's ledger
write is refused by the store and reported as :data:`STOP_STALE_FENCE`. The
already-issued external effect is untouched by that refusal and stays untouched --
there is no operation here that reaches back out to undo one. A stale generation
refusing the *latch* is reported differently, because it is a different fact: the
Attempt's own refusal is what stands and is what is returned, and
:attr:`DispatchOutcome.latched` is `False` to say nothing made it durable.

Every collaborator -- journal commit, ledger read and write, dispatch, fence
generation -- is an argument. Nothing in this module opens a connection, reads a
clock, sleeps or randomises, which is what makes each rule below testable as a
rule.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

#: The closed set of computation classes. Anything else is refused, always.
COMPUTATION_PURE: Final = "pure"
COMPUTATION_MATERIAL: Final = "material"
COMPUTATION_CLASSES: Final = frozenset({COMPUTATION_PURE, COMPUTATION_MATERIAL})

#: The closed set of authoritative journal commit outcomes.
COMMIT_COMMITTED: Final = "committed"
COMMIT_FAILED: Final = "failed"
COMMIT_UNCERTAIN: Final = "uncertain"
COMMIT_OUTCOMES: Final = frozenset({COMMIT_COMMITTED, COMMIT_FAILED, COMMIT_UNCERTAIN})

#: The closed set of authoritative effect-ledger states.
#:
#: `predeclared`        the stable effect identity exists; nothing has been issued
#: `issued_unknown`     the effect may have reached the world; reconciliation is owed
#: `settled_applied`    the effect is verified applied and settled
#: `proved_not_applied` the effect is proved never to have reached the world
EFFECT_PREDECLARED: Final = "predeclared"
EFFECT_ISSUED_UNKNOWN: Final = "issued_unknown"
EFFECT_SETTLED_APPLIED: Final = "settled_applied"
EFFECT_PROVED_NOT_APPLIED: Final = "proved_not_applied"
EFFECT_STATES: Final = frozenset(
    {
        EFFECT_PREDECLARED,
        EFFECT_ISSUED_UNKNOWN,
        EFFECT_SETTLED_APPLIED,
        EFFECT_PROVED_NOT_APPLIED,
    }
)

#: The only states a *new* material dispatch may start from. `issued_unknown` is
#: absent by design -- that is the poison case -- and so is `settled_applied`,
#: which is an answer already given.
DISPATCHABLE_EFFECT_STATES: Final = frozenset(
    {EFFECT_PREDECLARED, EFFECT_PROVED_NOT_APPLIED}
)

#: The states reconciliation may establish. It never establishes `issued_unknown`
#: (that is the question, not an answer) and never re-opens a predeclaration.
RECONCILED_EFFECT_STATES: Final = frozenset(
    {EFFECT_SETTLED_APPLIED, EFFECT_PROVED_NOT_APPLIED}
)

#: The two authoritative journal commits a dispatch is wrapped in.
JOURNAL_PHASE_INTENT: Final = "intent"
JOURNAL_PHASE_RECEIPT: Final = "receipt"

#: Bounded stop reasons. Non-secret by construction: each is a fixed literal.
STOP_ATTEMPT_LATCHED: Final = "attempt_latched"
STOP_COMMIT_RETRIES_EXHAUSTED: Final = "commit_retries_exhausted"
STOP_COMMIT_UNCERTAIN: Final = "commit_uncertain"
STOP_DISPATCH_UNCERTAIN: Final = "dispatch_uncertain"
STOP_EFFECT_NOT_DECLARED: Final = "effect_not_declared"
STOP_EFFECT_NOT_DISPATCHABLE: Final = "effect_not_dispatchable"
STOP_EFFECT_NOT_RECONCILABLE: Final = "effect_not_reconcilable"
STOP_EFFECT_STATE_RACED: Final = "effect_state_raced"
STOP_PURE_NOT_PERMITTED_WHILE_LATCHED: Final = "pure_not_permitted_while_latched"
STOP_STALE_FENCE: Final = "stale_fence"
STOP_UNKNOWN_COMMIT_OUTCOME: Final = "unknown_commit_outcome"
STOP_UNKNOWN_COMPUTATION_CLASS: Final = "unknown_computation_class"
STOP_UNKNOWN_EFFECT_STATE: Final = "unknown_effect_state"
STOP_UNKNOWN_RECONCILED_STATE: Final = "unknown_reconciled_state"

#: The upper bound on the bound. A retry budget is a safety parameter, so it is
#: fixed at construction and small enough that no caller can turn it into a loop.
MAX_COMMIT_RETRY_LIMIT: Final = 8

#: The shape an Attempt identifier may take before anything is written under it.
#: :class:`AttemptLatch` says it holds bounded, non-secret facts only, and the stop
#: reasons above are fixed literals that satisfy that by construction -- the Attempt
#: identifier is the one member a caller supplies, so it is the one that has to be held
#: to it. Bounded length and a narrow character set, which is what keeps a token, a URL
#: or a free-text detail from arriving in durable evidence as an identifier.
_ATTEMPT_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: The ceiling on a fence generation, matching the bound this repository's own schema
#: puts on a stored counter. A fence is a monotonic generation: a negative one is not a
#: stale generation but a malformed one, and an unbounded one is not a counter.
MAX_FENCE: Final = 1_000_000_000


class StaleFencingWrite(RuntimeError):
    """Raised by an :class:`AuthoritativeStore` when a stale generation writes.

    Refusing the write is all fencing can do. It does not and cannot recall an
    external effect the stale owner already issued.
    """


@dataclass(frozen=True, slots=True)
class AttemptLatch:
    """The durable stop recorded for one Attempt. Bounded, non-secret facts only."""

    attempt_id: str
    stop_reason: str
    reconciliation_required: bool
    effect_id: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """The immutable audit value for one material dispatch decision.

    `dispatched` records whether the external dispatch was *invoked* -- the call
    left this process. It is not a claim that the far side applied it; that is
    what `reconciliation_required` and the ledger state are for.

    `latched` records whether *this call* made the Attempt's stop durable. A stale
    fence is the only way a stop this module decided on fails to be written, so a
    stopping outcome carrying `latched=False` is exactly that case: the refusal in
    `stop_reason` still stands and is still owed, and nothing recorded it.
    """

    effect_id: str
    dispatched: bool
    commit_attempts: int
    reconciliation_required: bool
    stop_reason: str | None = None
    latched: bool = False


@dataclass(frozen=True, slots=True)
class ComputationDecision:
    """The immutable audit value for one computation-class authorization."""

    computation_class: str
    allowed: bool
    stop_reason: str | None = None


class AuthoritativeStore(Protocol):
    """The authoritative evidence seam: effect ledger plus Attempt latch.

    Writes carry the caller's fence generation and raise :class:`StaleFencingWrite`
    when it is stale.
    """

    def read_effect_state(self, effect_id: str) -> str | None: ...

    def write_effect_state(self, effect_id: str, state: str, *, fence: int) -> None: ...

    def compare_and_set_effect_state(
        self, effect_id: str, *, expected: str, desired: str, fence: int
    ) -> bool:
        """Move `effect_id` from `expected` to `desired` atomically. Report the winner.

        Returns `True` when this call performed the transition and `False` when the
        stored state was not `expected` -- including when the identity is missing.
        A `False` return must leave the ledger exactly as it found it.

        This is the gate every state-changing ledger write here goes through, because
        a read followed by a write is two decisions and the world can change between
        them: two contenders on the same fence generation both read `predeclared`,
        both conclude "dispatchable", and both issue the external effect the whole
        module exists to issue once. A stale generation is a different failure with a
        different answer (:class:`StaleFencingWrite`), and this does not replace it --
        fencing rejects an owner who has lost authority, and this rejects an owner who
        still has it and simply lost the race.
        """
        ...

    def read_latch(self, attempt_id: str) -> AttemptLatch | None: ...

    def write_latch(self, latch: AttemptLatch, *, fence: int) -> None: ...


#: `(effect_id, phase) -> outcome`, where phase is one of the `JOURNAL_PHASE_*`
#: literals and outcome is expected to be in :data:`COMMIT_OUTCOMES`.
CommitJournal = Callable[[str, str], str]

#: `(effect_id) -> None`. Raising is treated as *uncertain*, never as "not sent".
DispatchEffect = Callable[[str], None]


class MaterialDispatchCoordinator:
    """Guards material-effect dispatch for exactly one Attempt."""

    def __init__(
        self,
        attempt_id: str,
        store: AuthoritativeStore,
        commit_journal: CommitJournal,
        dispatch_effect: DispatchEffect,
        *,
        fence: int,
        commit_retry_limit: int,
        pure_permitted_while_latched: bool = False,
    ) -> None:
        if not isinstance(commit_retry_limit, int) or isinstance(
            commit_retry_limit, bool
        ):
            raise TypeError("commit_retry_limit must be an int")
        if not 0 <= commit_retry_limit <= MAX_COMMIT_RETRY_LIMIT:
            raise ValueError(
                f"commit_retry_limit must be between 0 and {MAX_COMMIT_RETRY_LIMIT}"
            )
        # Checked here rather than at the write, because a latch is written only on the
        # paths that were already going wrong: an identifier refused at construction
        # cannot become an unwritable stop at the moment the stop matters.
        if not isinstance(attempt_id, str):
            raise TypeError("attempt_id must be a str")
        if _ATTEMPT_ID.fullmatch(attempt_id) is None:
            raise ValueError("attempt_id is not a bounded, non-secret identifier")
        if not isinstance(fence, int) or isinstance(fence, bool):
            raise TypeError("fence must be an int")
        if not 0 <= fence <= MAX_FENCE:
            raise ValueError(f"fence must be between 0 and {MAX_FENCE}")
        self._attempt_id = attempt_id
        self._store = store
        self._commit_journal = commit_journal
        self._dispatch_effect = dispatch_effect
        self._fence = fence
        self._commit_retry_limit = commit_retry_limit
        self._pure_permitted_while_latched = pure_permitted_while_latched

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def commit_retry_limit(self) -> int:
        """The retry budget, fixed at construction and inspectable."""
        return self._commit_retry_limit

    @property
    def max_commit_attempts(self) -> int:
        """Attempts per journal commit: the first one plus the retry budget."""
        return self._commit_retry_limit + 1

    @property
    def pure_permitted_while_latched(self) -> bool:
        return self._pure_permitted_while_latched

    def read_latch(self) -> AttemptLatch | None:
        """The Attempt's durable stop, re-read from authoritative evidence."""
        return self._store.read_latch(self._attempt_id)

    def authorize(self, computation_class: str) -> ComputationDecision:
        """Decide whether a computation of this class may proceed."""
        if computation_class not in COMPUTATION_CLASSES:
            return ComputationDecision(
                computation_class, False, STOP_UNKNOWN_COMPUTATION_CLASS
            )
        latch = self.read_latch()
        if latch is None:
            return ComputationDecision(computation_class, True)
        if computation_class == COMPUTATION_MATERIAL:
            return ComputationDecision(computation_class, False, STOP_ATTEMPT_LATCHED)
        if self._pure_permitted_while_latched:
            return ComputationDecision(computation_class, True)
        return ComputationDecision(
            computation_class, False, STOP_PURE_NOT_PERMITTED_WHILE_LATCHED
        )

    def dispatch_material(self, effect_id: str) -> DispatchOutcome:
        """Commit, dispatch once, and record the receipt -- or refuse and latch."""
        decision = self.authorize(COMPUTATION_MATERIAL)
        if not decision.allowed:
            latch = self.read_latch()
            return DispatchOutcome(
                effect_id,
                dispatched=False,
                commit_attempts=0,
                reconciliation_required=bool(latch and latch.reconciliation_required),
                stop_reason=decision.stop_reason,
            )

        state = self._store.read_effect_state(effect_id)
        if state is None:
            return DispatchOutcome(effect_id, False, 0, False, STOP_EFFECT_NOT_DECLARED)
        if state not in EFFECT_STATES:
            return DispatchOutcome(
                effect_id, False, 0, False, STOP_UNKNOWN_EFFECT_STATE
            )
        if state not in DISPATCHABLE_EFFECT_STATES:
            # `issued_unknown` is the poison case: the effect may already be in the
            # world and no journal absence may argue otherwise.
            return DispatchOutcome(
                effect_id,
                dispatched=False,
                commit_attempts=0,
                reconciliation_required=state == EFFECT_ISSUED_UNKNOWN,
                stop_reason=STOP_EFFECT_NOT_DISPATCHABLE,
            )

        outcome, attempts = self._commit(effect_id, JOURNAL_PHASE_INTENT)
        if outcome != COMMIT_COMMITTED:
            # Failed, uncertain and unclassifiable all leave the disposition
            # indeterminate: a commit that never landed is not evidence of "no".
            return self._latched_outcome(
                effect_id,
                dispatched=False,
                commit_attempts=attempts,
                reconciliation_required=True,
                stop_reason=_stop_for_commit(outcome),
            )

        # The ledger records the possibility of an effect *before* the effect can
        # happen. Dispatching first would leave a window with a live effect and no
        # authoritative trace of it. The claim is compare-and-set from the exact state
        # read above rather than a blind write, so the read that decided "dispatchable"
        # and the write that acts on it are one decision: a second contender on this
        # same fence generation finds the state already moved and never reaches the
        # dispatch below. Losing that race owes no reconciliation -- this Attempt issued
        # nothing, and the winner holds the obligation for what it issued.
        try:
            claimed = self._store.compare_and_set_effect_state(
                effect_id,
                expected=state,
                desired=EFFECT_ISSUED_UNKNOWN,
                fence=self._fence,
            )
        except StaleFencingWrite:
            return DispatchOutcome(effect_id, False, attempts, False, STOP_STALE_FENCE)
        if not claimed:
            return DispatchOutcome(
                effect_id, False, attempts, False, STOP_EFFECT_STATE_RACED
            )

        try:
            self._dispatch_effect(effect_id)
        except Exception:  # noqa: BLE001 - any failure here is uncertainty
            # The call left the process. Whether it landed is exactly the unknown
            # the ledger now holds, so this is never re-dispatched.
            return self._latched_outcome(
                effect_id,
                dispatched=True,
                commit_attempts=attempts,
                reconciliation_required=True,
                stop_reason=STOP_DISPATCH_UNCERTAIN,
            )

        receipt_outcome, receipt_attempts = self._commit(
            effect_id, JOURNAL_PHASE_RECEIPT
        )
        attempts += receipt_attempts
        if receipt_outcome != COMMIT_COMMITTED:
            return self._latched_outcome(
                effect_id,
                dispatched=True,
                commit_attempts=attempts,
                reconciliation_required=True,
                stop_reason=_stop_for_commit(receipt_outcome),
            )

        try:
            settled = self._store.compare_and_set_effect_state(
                effect_id,
                expected=EFFECT_ISSUED_UNKNOWN,
                desired=EFFECT_SETTLED_APPLIED,
                fence=self._fence,
            )
        except StaleFencingWrite:
            return DispatchOutcome(effect_id, True, attempts, True, STOP_STALE_FENCE)
        if not settled:
            # Something else answered the identity while this dispatch was in flight, so
            # the receipt this Attempt holds and the answer the ledger now carries are
            # two claims about one effect. That is exactly a reconciliation, and
            # overwriting the ledger with this Attempt's answer would resolve it by
            # assertion.
            return self._latched_outcome(
                effect_id,
                dispatched=True,
                commit_attempts=attempts,
                reconciliation_required=True,
                stop_reason=STOP_EFFECT_STATE_RACED,
            )
        return DispatchOutcome(effect_id, True, attempts, False)

    def reconcile(self, effect_id: str, resolved_state: str) -> DispatchOutcome:
        """Settle an unknown disposition. Never dispatches, never clears the latch.

        The only transition this performs is `issued_unknown` to one of
        :data:`RECONCILED_EFFECT_STATES`, read from the authoritative ledger rather
        than assumed. A missing, unclassifiable, merely predeclared or already
        settled identity is refused *without writing*: reconciliation answers the
        open question, it never overwrites an answer already given.

        `proved_not_applied` may make a *different* Attempt eligible to act on the
        identity again. It does not unblock this one: the Attempt that produced the
        uncertainty stays stopped, so nothing here reuses it.
        """
        if resolved_state not in RECONCILED_EFFECT_STATES:
            return DispatchOutcome(
                effect_id, False, 0, True, STOP_UNKNOWN_RECONCILED_STATE
            )
        current = self._store.read_effect_state(effect_id)
        if current is None:
            return DispatchOutcome(effect_id, False, 0, True, STOP_EFFECT_NOT_DECLARED)
        if current not in EFFECT_STATES:
            return DispatchOutcome(effect_id, False, 0, True, STOP_UNKNOWN_EFFECT_STATE)
        if current != EFFECT_ISSUED_UNKNOWN:
            return DispatchOutcome(
                effect_id, False, 0, False, STOP_EFFECT_NOT_RECONCILABLE
            )
        try:
            resolved = self._store.compare_and_set_effect_state(
                effect_id,
                expected=EFFECT_ISSUED_UNKNOWN,
                desired=resolved_state,
                fence=self._fence,
            )
        except StaleFencingWrite:
            return DispatchOutcome(effect_id, False, 0, True, STOP_STALE_FENCE)
        if not resolved:
            # The read above said `issued_unknown` and the ledger no longer does, so a
            # concurrent answer already closed this question. Refusing here is what stops
            # a reconciler holding a stale read from writing `proved_not_applied` over a
            # `settled_applied` -- which would put a terminal, verified effect back into
            # `DISPATCHABLE_EFFECT_STATES` and licence a second dispatch of it.
            return DispatchOutcome(
                effect_id, False, 0, False, STOP_EFFECT_NOT_RECONCILABLE
            )
        return DispatchOutcome(effect_id, False, 0, False)

    def _commit(self, effect_id: str, phase: str) -> tuple[str, int]:
        """Run one journal commit under the fixed retry budget."""
        outcome = COMMIT_FAILED
        for attempt in range(1, self.max_commit_attempts + 1):
            outcome = self._commit_journal(effect_id, phase)
            if outcome != COMMIT_FAILED:
                # `committed` is done; `uncertain` and anything unclassifiable stop
                # here rather than being retried as if they were failures.
                return outcome, attempt
        return outcome, self.max_commit_attempts

    def _latched_outcome(
        self,
        effect_id: str,
        *,
        dispatched: bool,
        commit_attempts: int,
        reconciliation_required: bool,
        stop_reason: str,
    ) -> DispatchOutcome:
        latch = AttemptLatch(
            self._attempt_id, stop_reason, reconciliation_required, effect_id
        )
        try:
            self._store.write_latch(latch, fence=self._fence)
        except StaleFencingWrite:
            # The fence refused the write; it did not answer the question the write was
            # about. Overwriting `stop_reason` with `stale_fence` here would lose the one
            # fact this Attempt is stopped for -- an uncertain commit, an uncertain
            # dispatch, an exhausted retry budget -- and leave a caller with a refusal
            # that reads as "somebody else owns this now" and no reconciliation owed.
            # The original stands; `latched` is what says nobody recorded it.
            return DispatchOutcome(
                effect_id,
                dispatched,
                commit_attempts,
                reconciliation_required,
                stop_reason,
            )
        return DispatchOutcome(
            effect_id,
            dispatched,
            commit_attempts,
            reconciliation_required,
            stop_reason,
            latched=True,
        )


def _stop_for_commit(outcome: str) -> str:
    if outcome == COMMIT_FAILED:
        return STOP_COMMIT_RETRIES_EXHAUSTED
    if outcome == COMMIT_UNCERTAIN:
        return STOP_COMMIT_UNCERTAIN
    return STOP_UNKNOWN_COMMIT_OUTCOME


#: The closed vocabularies, the bounds, the collaborator seams and the three audit
#: values -- and nothing else. Declared so the module's surface is a decision rather
#: than whatever happens not to start with an underscore.
__all__ = [
    "COMMIT_COMMITTED",
    "COMMIT_FAILED",
    "COMMIT_OUTCOMES",
    "COMMIT_UNCERTAIN",
    "COMPUTATION_CLASSES",
    "COMPUTATION_MATERIAL",
    "COMPUTATION_PURE",
    "DISPATCHABLE_EFFECT_STATES",
    "EFFECT_ISSUED_UNKNOWN",
    "EFFECT_PREDECLARED",
    "EFFECT_PROVED_NOT_APPLIED",
    "EFFECT_SETTLED_APPLIED",
    "EFFECT_STATES",
    "JOURNAL_PHASE_INTENT",
    "JOURNAL_PHASE_RECEIPT",
    "MAX_COMMIT_RETRY_LIMIT",
    "MAX_FENCE",
    "RECONCILED_EFFECT_STATES",
    "STOP_ATTEMPT_LATCHED",
    "STOP_COMMIT_RETRIES_EXHAUSTED",
    "STOP_COMMIT_UNCERTAIN",
    "STOP_DISPATCH_UNCERTAIN",
    "STOP_EFFECT_NOT_DECLARED",
    "STOP_EFFECT_NOT_DISPATCHABLE",
    "STOP_EFFECT_NOT_RECONCILABLE",
    "STOP_EFFECT_STATE_RACED",
    "STOP_PURE_NOT_PERMITTED_WHILE_LATCHED",
    "STOP_STALE_FENCE",
    "STOP_UNKNOWN_COMMIT_OUTCOME",
    "STOP_UNKNOWN_COMPUTATION_CLASS",
    "STOP_UNKNOWN_EFFECT_STATE",
    "STOP_UNKNOWN_RECONCILED_STATE",
    "AttemptLatch",
    "AuthoritativeStore",
    "CommitJournal",
    "ComputationDecision",
    "DispatchEffect",
    "DispatchOutcome",
    "MaterialDispatchCoordinator",
    "StaleFencingWrite",
]
