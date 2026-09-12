"""T-0688 IP-03 acceptance for material-effect dispatch safety (WEFT-BL-002/003).

Every collaborator of the seam under test is an argument, so each test below is a
rule rather than a scenario: the store, the journal and the world are local fakes
holding dicts and lists, and nothing here opens a socket, a database or a clock,
sleeps, or randomises.

The property the whole file is about: *an external effect is never issued twice on
the strength of a commit that failed or became uncertain.* The counter proving it
is `world.issued` -- a plain list -- and the tests that matter assert its length,
not just a returned flag.

None of this asserts exactly-once delivery. `test_a_stale_fence_rejects_the_write_
without_recalling_the_external_effect` asserts the opposite where it counts: the
effect stays issued after the write is refused, because nothing can take it back.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.service.material_dispatch_safety import (
    COMMIT_COMMITTED,
    COMMIT_FAILED,
    COMMIT_UNCERTAIN,
    COMPUTATION_MATERIAL,
    COMPUTATION_PURE,
    EFFECT_ISSUED_UNKNOWN,
    EFFECT_PREDECLARED,
    EFFECT_PROVED_NOT_APPLIED,
    EFFECT_SETTLED_APPLIED,
    JOURNAL_PHASE_INTENT,
    JOURNAL_PHASE_RECEIPT,
    MAX_COMMIT_RETRY_LIMIT,
    MAX_FENCE,
    STOP_ATTEMPT_LATCHED,
    STOP_COMMIT_RETRIES_EXHAUSTED,
    STOP_COMMIT_UNCERTAIN,
    STOP_DISPATCH_UNCERTAIN,
    STOP_EFFECT_NOT_DECLARED,
    STOP_EFFECT_NOT_DISPATCHABLE,
    STOP_EFFECT_NOT_RECONCILABLE,
    STOP_EFFECT_STATE_RACED,
    STOP_PURE_NOT_PERMITTED_WHILE_LATCHED,
    STOP_STALE_FENCE,
    STOP_UNKNOWN_COMMIT_OUTCOME,
    STOP_UNKNOWN_COMPUTATION_CLASS,
    STOP_UNKNOWN_EFFECT_STATE,
    STOP_UNKNOWN_RECONCILED_STATE,
    DispatchOutcome,
    MaterialDispatchCoordinator,
    StaleFencingWrite,
)

ATTEMPT = "attempt-t0688-a"
OTHER_ATTEMPT = "attempt-t0688-b"
EFFECT = "effect-intent-t0688-harbour-notify"
OTHER_EFFECT = "effect-intent-t0688-harbour-invoice"
FENCE = 8


class FakeStore:
    """An authoritative store: an effect ledger, Attempt latches, one generation."""

    def __init__(self, generation: int = FENCE) -> None:
        self.generation = generation
        self.effects: dict[str, str] = {}
        self.latches: dict[str, object] = {}
        self.rejected_writes: list[tuple[str, int]] = []

    def _check(self, what: str, fence: int) -> None:
        if fence < self.generation:
            self.rejected_writes.append((what, fence))
            raise StaleFencingWrite(
                f"fence {fence} is stale; current {self.generation}"
            )

    def read_effect_state(self, effect_id):
        return self.effects.get(effect_id)

    def write_effect_state(self, effect_id, state, *, fence):
        self._check(effect_id, fence)
        self.effects[effect_id] = state

    def compare_and_set_effect_state(self, effect_id, *, expected, desired, fence):
        # Fencing is asked first: a stale generation is refused whether or not it
        # would have won the race. A losing contender leaves the ledger untouched.
        self._check(effect_id, fence)
        if self.effects.get(effect_id) != expected:
            return False
        self.effects[effect_id] = desired
        return True

    def read_latch(self, attempt_id):
        return self.latches.get(attempt_id)

    def write_latch(self, latch, *, fence):
        self._check(latch.attempt_id, fence)
        self.latches[latch.attempt_id] = latch


class FakeJournal:
    """Scripted commit outcomes per phase; the tail outcome repeats forever."""

    def __init__(self, **phases: object) -> None:
        self.scripts = {
            phase: list(value) if isinstance(value, list) else [value]
            for phase, value in phases.items()
        }
        self.calls: list[tuple[str, str]] = []

    def __call__(self, effect_id: str, phase: str) -> str:
        self.calls.append((effect_id, phase))
        script = self.scripts.get(phase, [COMMIT_COMMITTED])
        return script.pop(0) if len(script) > 1 else script[0]

    def count(self, phase: str) -> int:
        return sum(1 for _, called in self.calls if called == phase)


class FakeWorld:
    """The far side. `issued` is the ledger of record for duplicate dispatch."""

    def __init__(self, raises: bool = False) -> None:
        self.issued: list[str] = []
        self.raises = raises

    def __call__(self, effect_id: str) -> None:
        self.issued.append(effect_id)
        if self.raises:
            raise TimeoutError("no answer from the far side")


def _coordinator(
    store: FakeStore,
    journal: FakeJournal | None = None,
    world: FakeWorld | None = None,
    *,
    attempt_id: str = ATTEMPT,
    fence: int = FENCE,
    commit_retry_limit: int = 2,
    pure_permitted_while_latched: bool = False,
) -> MaterialDispatchCoordinator:
    return MaterialDispatchCoordinator(
        attempt_id,
        store,
        journal or FakeJournal(),
        world or FakeWorld(),
        fence=fence,
        commit_retry_limit=commit_retry_limit,
        pure_permitted_while_latched=pure_permitted_while_latched,
    )


def _predeclared(*effect_ids: str) -> FakeStore:
    store = FakeStore()
    for effect_id in effect_ids:
        store.effects[effect_id] = EFFECT_PREDECLARED
    return store


# --- 1. the settled path ------------------------------------------------------


def test_a_predeclared_effect_commits_dispatches_once_and_settles():
    store = _predeclared(EFFECT, OTHER_EFFECT)
    journal, world = FakeJournal(), FakeWorld()
    coordinator = _coordinator(store, journal, world)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome == DispatchOutcome(EFFECT, True, 2, False, None)
    assert world.issued == [EFFECT]
    assert store.effects[EFFECT] == EFFECT_SETTLED_APPLIED
    assert journal.calls == [
        (EFFECT, JOURNAL_PHASE_INTENT),
        (EFFECT, JOURNAL_PHASE_RECEIPT),
    ]
    assert coordinator.read_latch() is None

    # A settled Attempt is not a stopped one: the next material operation runs.
    assert coordinator.dispatch_material(OTHER_EFFECT).dispatched is True
    assert world.issued == [EFFECT, OTHER_EFFECT]


def test_material_dispatch_requires_a_predeclared_identity():
    store = FakeStore()
    world = FakeWorld()
    coordinator = _coordinator(store, world=world)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_EFFECT_NOT_DECLARED
    assert outcome.dispatched is False
    assert world.issued == []
    assert coordinator.read_latch() is None


def test_a_settled_effect_is_never_dispatched_again():
    store = _predeclared()
    store.effects[EFFECT] = EFFECT_SETTLED_APPLIED
    world = FakeWorld()

    outcome = _coordinator(store, world=world).dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_EFFECT_NOT_DISPATCHABLE
    assert outcome.reconciliation_required is False
    assert world.issued == []


# --- 2/3. bounded failure, immediate uncertainty ------------------------------


def test_exhausted_failed_commits_latch_the_attempt_and_block_later_dispatch():
    store = _predeclared(EFFECT, OTHER_EFFECT)
    journal = FakeJournal(intent=COMMIT_FAILED)
    world = FakeWorld()
    coordinator = _coordinator(store, journal, world, commit_retry_limit=2)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_COMMIT_RETRIES_EXHAUSTED
    assert outcome.dispatched is False
    assert outcome.commit_attempts == 3
    # WEFT-BL-002: a failed commit is as indeterminate as an uncertain one.
    assert outcome.reconciliation_required is True
    assert coordinator.read_latch().reconciliation_required is True
    assert world.issued == []
    assert store.effects[EFFECT] == EFFECT_PREDECLARED

    later = coordinator.dispatch_material(OTHER_EFFECT)
    assert later.stop_reason == STOP_ATTEMPT_LATCHED
    assert world.issued == []


def test_an_uncertain_commit_stops_immediately_and_is_never_retried():
    store = _predeclared(EFFECT, OTHER_EFFECT)
    journal = FakeJournal(intent=COMMIT_UNCERTAIN)
    world = FakeWorld()
    coordinator = _coordinator(store, journal, world, commit_retry_limit=5)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_COMMIT_UNCERTAIN
    assert outcome.commit_attempts == 1, "uncertainty is not a failure to retry"
    assert journal.count(JOURNAL_PHASE_INTENT) == 1
    assert outcome.reconciliation_required is True
    assert world.issued == []

    assert (
        coordinator.dispatch_material(OTHER_EFFECT).stop_reason == STOP_ATTEMPT_LATCHED
    )
    assert world.issued == []


# --- 4/5. the poison case -----------------------------------------------------


def test_a_dispatched_effect_whose_receipt_commit_fails_is_not_redispatched():
    store = _predeclared(EFFECT)
    journal = FakeJournal(intent=COMMIT_COMMITTED, receipt=COMMIT_FAILED)
    world = FakeWorld()
    first = _coordinator(store, journal, world)

    outcome = first.dispatch_material(EFFECT)

    assert outcome.dispatched is True
    assert outcome.stop_reason == STOP_COMMIT_RETRIES_EXHAUSTED
    assert outcome.reconciliation_required is True
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN
    assert world.issued == [EFFECT]

    # A restart is a fresh coordinator over the same authoritative store. The
    # latch is durable evidence, not process memory.
    revived = _coordinator(store, FakeJournal(), world)
    assert revived.read_latch() is not None
    replayed = revived.dispatch_material(EFFECT)

    assert replayed.stop_reason == STOP_ATTEMPT_LATCHED
    assert replayed.reconciliation_required is True
    assert world.issued == [EFFECT], "the effect must not be issued a second time"


def test_journal_absence_does_not_override_issued_unknown_ledger_evidence():
    store = _predeclared()
    store.effects[EFFECT] = EFFECT_ISSUED_UNKNOWN
    # A journal that has no record of the commit at all -- every commit fails.
    journal = FakeJournal(intent=COMMIT_FAILED, receipt=COMMIT_FAILED)
    world = FakeWorld()

    # A different, unlatched Attempt: only the ledger stands between it and a
    # duplicate.
    outcome = _coordinator(
        store, journal, world, attempt_id=OTHER_ATTEMPT
    ).dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_EFFECT_NOT_DISPATCHABLE
    assert outcome.reconciliation_required is True
    assert outcome.commit_attempts == 0, "the ledger refuses before any commit runs"
    assert journal.calls == []
    assert world.issued == []


def test_a_dispatch_that_raises_is_uncertain_rather_than_not_sent():
    store = _predeclared(EFFECT)
    world = FakeWorld(raises=True)
    coordinator = _coordinator(store, FakeJournal(), world)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome.dispatched is True
    assert outcome.reconciliation_required is True
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN
    assert coordinator.read_latch() is not None


# --- 6/7. reconciliation ------------------------------------------------------


def test_reconciliation_settles_without_dispatching():
    store = _predeclared()
    store.effects[EFFECT] = EFFECT_ISSUED_UNKNOWN
    world = FakeWorld()
    journal = FakeJournal()

    outcome = _coordinator(store, journal, world).reconcile(
        EFFECT, EFFECT_SETTLED_APPLIED
    )

    assert outcome == DispatchOutcome(EFFECT, False, 0, False, None)
    assert store.effects[EFFECT] == EFFECT_SETTLED_APPLIED
    assert world.issued == []
    assert journal.calls == []


def test_proved_not_applied_frees_a_new_attempt_but_not_the_uncertain_one():
    store = _predeclared(EFFECT)
    world = FakeWorld(raises=True)
    uncertain = _coordinator(store, FakeJournal(), world, attempt_id=ATTEMPT)
    assert uncertain.dispatch_material(EFFECT).stop_reason == STOP_DISPATCH_UNCERTAIN
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN

    uncertain.reconcile(EFFECT, EFFECT_PROVED_NOT_APPLIED)
    assert store.effects[EFFECT] == EFFECT_PROVED_NOT_APPLIED

    # The Attempt that produced the uncertainty stays stopped and is not reused.
    assert uncertain.dispatch_material(EFFECT).stop_reason == STOP_ATTEMPT_LATCHED
    assert world.issued == [EFFECT], "the stopped Attempt never issues again"

    successor_world = FakeWorld()
    successor = _coordinator(
        store, FakeJournal(), successor_world, attempt_id=OTHER_ATTEMPT
    )
    assert successor.dispatch_material(EFFECT).dispatched is True
    assert successor_world.issued == [EFFECT]


def test_reconciliation_cannot_downgrade_a_settled_effect():
    store = _predeclared()
    store.effects[EFFECT] = EFFECT_SETTLED_APPLIED
    world, journal = FakeWorld(), FakeJournal()

    outcome = _coordinator(store, journal, world).reconcile(
        EFFECT, EFFECT_PROVED_NOT_APPLIED
    )

    assert outcome.stop_reason == STOP_EFFECT_NOT_RECONCILABLE
    assert store.effects[EFFECT] == EFFECT_SETTLED_APPLIED, "an answer already given"
    assert world.issued == []
    assert journal.calls == []


def test_reconciliation_cannot_take_a_predeclared_effect_straight_to_terminal():
    store = _predeclared(EFFECT)
    world, journal = FakeWorld(), FakeJournal()
    coordinator = _coordinator(store, journal, world)

    for resolved in (EFFECT_SETTLED_APPLIED, EFFECT_PROVED_NOT_APPLIED):
        outcome = coordinator.reconcile(EFFECT, resolved)
        assert outcome.stop_reason == STOP_EFFECT_NOT_RECONCILABLE
        assert store.effects[EFFECT] == EFFECT_PREDECLARED

    assert world.issued == []
    assert journal.calls == []


@pytest.mark.parametrize("current", [None, "", "probably_fine"])
def test_reconciliation_refuses_a_missing_or_corrupt_effect_state(current):
    store = FakeStore()
    if current is not None:
        store.effects[EFFECT] = current
    world = FakeWorld()

    outcome = _coordinator(store, world=world).reconcile(EFFECT, EFFECT_SETTLED_APPLIED)

    assert outcome.stop_reason == (
        STOP_EFFECT_NOT_DECLARED if current is None else STOP_UNKNOWN_EFFECT_STATE
    )
    assert outcome.reconciliation_required is True
    assert store.effects == ({} if current is None else {EFFECT: current})
    assert world.issued == []


# --- 8. fencing ---------------------------------------------------------------


def test_a_stale_fence_rejects_the_write_without_recalling_the_external_effect():
    store = _predeclared(EFFECT)
    world = FakeWorld()
    journal = FakeJournal(intent=COMMIT_COMMITTED, receipt=COMMIT_UNCERTAIN)
    assert _coordinator(store, journal, world).dispatch_material(EFFECT).dispatched
    assert world.issued == [EFFECT]
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN

    store.generation = FENCE + 1
    stale = _coordinator(store, FakeJournal(), world, fence=FENCE)

    outcome = stale.reconcile(EFFECT, EFFECT_PROVED_NOT_APPLIED)

    assert outcome.stop_reason == STOP_STALE_FENCE
    assert store.rejected_writes == [(EFFECT, FENCE)]
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN, "no stale rewrite"
    assert world.issued == [EFFECT], "fencing cannot recall an issued effect"


def test_a_stale_fence_refusing_the_latch_does_not_erase_the_refusal_it_carried():
    """The fence answers who may write, never what this Attempt is stopped for.

    An uncertain commit is the stop, and reconciliation is what it owes. Replacing that
    with `stale_fence` would hand a caller a refusal that reads as somebody else's
    ownership, with nothing left saying the disposition is indeterminate -- so the
    original stop stands, and `latched` is what says nothing recorded it.
    """
    store = _predeclared(EFFECT)
    store.generation = FENCE + 1
    journal, world = FakeJournal(intent=COMMIT_UNCERTAIN), FakeWorld()

    outcome = _coordinator(store, journal, world, fence=FENCE).dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_COMMIT_UNCERTAIN
    assert outcome.reconciliation_required is True
    assert outcome.latched is False
    assert store.rejected_writes == [(ATTEMPT, FENCE)]
    assert store.latches == {}
    assert world.issued == []
    assert store.effects[EFFECT] == EFFECT_PREDECLARED


def test_a_recorded_latch_says_so():
    """The other half of the same fact: a current fence writes it, and reports it."""
    store = _predeclared(EFFECT)
    outcome = _coordinator(
        store, FakeJournal(intent=COMMIT_UNCERTAIN), FakeWorld()
    ).dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_COMMIT_UNCERTAIN
    assert outcome.latched is True
    assert store.latches[ATTEMPT].stop_reason == STOP_COMMIT_UNCERTAIN


def test_a_stale_fence_cannot_newly_dispatch():
    store = _predeclared(EFFECT)
    store.generation = FENCE + 1
    world = FakeWorld()

    outcome = _coordinator(store, FakeJournal(), world, fence=FENCE).dispatch_material(
        EFFECT
    )

    assert outcome.stop_reason == STOP_STALE_FENCE
    assert outcome.dispatched is False
    assert world.issued == []
    assert store.effects[EFFECT] == EFFECT_PREDECLARED


# --- 9. pure computation while latched ----------------------------------------


def test_pure_computation_while_latched_requires_the_explicit_policy():
    store = _predeclared(EFFECT)
    journal = FakeJournal(intent=COMMIT_UNCERTAIN)
    refusing = _coordinator(store, journal, FakeWorld())
    refusing.dispatch_material(EFFECT)

    decision = refusing.authorize(COMPUTATION_PURE)
    assert decision.allowed is False
    assert decision.stop_reason == STOP_PURE_NOT_PERMITTED_WHILE_LATCHED

    permitted = _coordinator(store, FakeJournal(), pure_permitted_while_latched=True)
    assert permitted.authorize(COMPUTATION_PURE).allowed is True
    # The policy is about pure recomputation only; it never widens material reach.
    assert permitted.authorize(COMPUTATION_MATERIAL).allowed is False
    assert permitted.dispatch_material(EFFECT).stop_reason == STOP_ATTEMPT_LATCHED


def test_pure_computation_is_allowed_when_the_attempt_is_not_latched():
    coordinator = _coordinator(_predeclared(EFFECT))
    decision = coordinator.authorize(COMPUTATION_PURE)
    assert decision.allowed is True
    assert decision.stop_reason is None


# --- 10. unknown inputs fail closed -------------------------------------------


@pytest.mark.parametrize("unknown", ["", "PURE", "impure", "material ", "effectful"])
def test_an_unknown_computation_class_is_always_refused(unknown):
    decision = _coordinator(_predeclared(EFFECT)).authorize(unknown)
    assert decision.allowed is False
    assert decision.stop_reason == STOP_UNKNOWN_COMPUTATION_CLASS


def test_an_unknown_commit_outcome_latches_rather_than_being_retried():
    store = _predeclared(EFFECT)
    journal = FakeJournal(intent="probably?")
    world = FakeWorld()
    coordinator = _coordinator(store, journal, world)

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_UNKNOWN_COMMIT_OUTCOME
    assert outcome.commit_attempts == 1
    assert outcome.reconciliation_required is True
    assert world.issued == []
    assert coordinator.read_latch() is not None


def test_an_unknown_ledger_state_refuses_dispatch():
    store = FakeStore()
    store.effects[EFFECT] = "probably_fine"
    world = FakeWorld()

    outcome = _coordinator(store, world=world).dispatch_material(EFFECT)

    assert outcome.stop_reason == STOP_UNKNOWN_EFFECT_STATE
    assert world.issued == []


@pytest.mark.parametrize(
    "unknown", ["", EFFECT_ISSUED_UNKNOWN, EFFECT_PREDECLARED, "applied"]
)
def test_reconciliation_refuses_an_unknown_resolved_state(unknown):
    store = FakeStore()
    store.effects[EFFECT] = EFFECT_ISSUED_UNKNOWN

    outcome = _coordinator(store).reconcile(EFFECT, unknown)

    assert outcome.stop_reason == STOP_UNKNOWN_RECONCILED_STATE
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN


# --- 11. determinism and the audit value --------------------------------------


@pytest.mark.parametrize("limit", [0, 1, 3, MAX_COMMIT_RETRY_LIMIT])
def test_the_commit_retry_bound_is_fixed_inspectable_and_exact(limit):
    store = _predeclared(EFFECT)
    journal = FakeJournal(intent=COMMIT_FAILED)
    coordinator = _coordinator(store, journal, commit_retry_limit=limit)

    assert coordinator.commit_retry_limit == limit
    assert coordinator.max_commit_attempts == limit + 1

    outcome = coordinator.dispatch_material(EFFECT)
    assert journal.count(JOURNAL_PHASE_INTENT) == limit + 1
    assert outcome.commit_attempts == limit + 1


def test_repeated_identical_runs_produce_identical_audit_values():
    def run() -> DispatchOutcome:
        return _coordinator(
            _predeclared(EFFECT),
            FakeJournal(intent=[COMMIT_FAILED, COMMIT_COMMITTED]),
            FakeWorld(),
        ).dispatch_material(EFFECT)

    assert run() == run() == DispatchOutcome(EFFECT, True, 3, False, None)


@pytest.mark.parametrize("limit", [-1, MAX_COMMIT_RETRY_LIMIT + 1])
def test_an_unbounded_retry_budget_is_refused_at_construction(limit):
    with pytest.raises(ValueError):
        _coordinator(FakeStore(), commit_retry_limit=limit)


@pytest.mark.parametrize("limit", [2.0, True, "2", None])
def test_a_non_integer_retry_budget_is_refused_at_construction(limit):
    with pytest.raises(TypeError):
        _coordinator(FakeStore(), commit_retry_limit=limit)


@pytest.mark.parametrize(
    "attempt_id",
    [
        "",
        "attempt t0688 a",
        "-attempt-t0688-a",
        "attempt/t0688/a",
        "attempt\nt0688",
        "Bearer sk-live-0123456789abcdef",
        "a" * 129,
    ],
    ids=[
        "empty",
        "spaced",
        "leading punctuation",
        "pathlike",
        "newline",
        "a credential",
        "unbounded",
    ],
)
def test_an_unbounded_or_secret_bearing_attempt_id_is_refused_at_construction(
    attempt_id,
):
    """A latch is durable evidence, so what may be written into one is decided first.

    Refusing here rather than at `write_latch` is the point: a latch is only ever
    written on a path that has already gone wrong, so an identifier that could not be
    stored would first be discovered at the moment the stop had to become durable.
    """
    with pytest.raises(ValueError):
        _coordinator(FakeStore(), attempt_id=attempt_id)


@pytest.mark.parametrize("attempt_id", [None, 7, b"attempt-t0688-a"])
def test_a_non_text_attempt_id_is_refused_at_construction(attempt_id):
    with pytest.raises(TypeError):
        _coordinator(FakeStore(), attempt_id=attempt_id)


@pytest.mark.parametrize("fence", [-1, MAX_FENCE + 1])
def test_a_negative_or_unbounded_fence_is_refused_at_construction(fence):
    with pytest.raises(ValueError):
        _coordinator(FakeStore(), fence=fence)


@pytest.mark.parametrize("fence", [8.0, True, "8", None])
def test_a_non_integer_fence_is_refused_at_construction(fence):
    with pytest.raises(TypeError):
        _coordinator(FakeStore(), fence=fence)


def test_the_audit_value_is_immutable_and_carries_only_bounded_facts():
    outcome = _coordinator(_predeclared(EFFECT)).dispatch_material(EFFECT)

    assert outcome.__dataclass_fields__.keys() == {
        "effect_id",
        "dispatched",
        "commit_attempts",
        "reconciliation_required",
        "stop_reason",
        "latched",
    }
    with pytest.raises(AttributeError):
        outcome.dispatched = False  # type: ignore[misc]


# --- 9. two contenders on one generation --------------------------------------


class _RacingStore(FakeStore):
    """A store whose ledger is moved by somebody else between a read and its write.

    `interlopers` maps an effect identity to the state a concurrent contender has
    already written by the time this Attempt tries to claim it. Exactly the window a
    read-then-write leaves open, made deterministic: no threads, no sleeps, one
    scripted interleaving.
    """

    def __init__(self, **interlopers: str) -> None:
        super().__init__()
        self.interlopers = dict(interlopers)

    def compare_and_set_effect_state(self, effect_id, *, expected, desired, fence):
        moved = self.interlopers.pop(effect_id, None)
        if moved is not None:
            self.effects[effect_id] = moved
        return super().compare_and_set_effect_state(
            effect_id, expected=expected, desired=desired, fence=fence
        )


def test_two_same_generation_contenders_cannot_both_issue_one_effect():
    """The regression for the read-then-write dispatch gate.

    Both contenders read `predeclared` and both concluded the identity was
    dispatchable -- neither is stale, so fencing separates nothing. The one that did
    not move the ledger stops before the world is touched, and owes no reconciliation:
    it issued nothing, and the winner holds the obligation for what it issued.
    """
    store = _RacingStore(**{EFFECT: EFFECT_ISSUED_UNKNOWN})
    store.effects[EFFECT] = EFFECT_PREDECLARED
    journal, world = FakeJournal(), FakeWorld()

    outcome = _coordinator(store, journal, world).dispatch_material(EFFECT)

    assert outcome == DispatchOutcome(EFFECT, False, 1, False, STOP_EFFECT_STATE_RACED)
    assert world.issued == []
    assert store.effects[EFFECT] == EFFECT_ISSUED_UNKNOWN
    assert journal.count(JOURNAL_PHASE_RECEIPT) == 0
    assert store.read_latch(ATTEMPT) is None


def test_an_answer_landing_under_a_live_dispatch_is_reconciled_not_overwritten():
    """The receipt write is a claim about an identity somebody else has answered.

    Two claims about one effect is exactly a reconciliation, so this Attempt latches
    with the obligation rather than settling the ledger by assertion.
    """
    store = FakeStore()
    store.effects[EFFECT] = EFFECT_PREDECLARED
    journal, world = FakeJournal(), FakeWorld()
    coordinator = _coordinator(store, journal, world)
    # The interloper answers only the second compare-and-set: this Attempt's claim to
    # `issued_unknown` wins, and its settlement after the receipt does not.
    original = store.compare_and_set_effect_state

    def racing(effect_id, *, expected, desired, fence):
        if desired == EFFECT_SETTLED_APPLIED:
            store.effects[effect_id] = EFFECT_PROVED_NOT_APPLIED
        return original(effect_id, expected=expected, desired=desired, fence=fence)

    store.compare_and_set_effect_state = racing  # type: ignore[method-assign]

    outcome = coordinator.dispatch_material(EFFECT)

    assert outcome == DispatchOutcome(
        EFFECT, True, 2, True, STOP_EFFECT_STATE_RACED, latched=True
    )
    assert world.issued == [EFFECT]
    assert store.effects[EFFECT] == EFFECT_PROVED_NOT_APPLIED
    latch = store.read_latch(ATTEMPT)
    assert latch is not None and latch.reconciliation_required is True


def test_a_stale_reconcile_cannot_walk_a_settled_effect_back_to_dispatchable():
    """The regression for the read-then-write reconcile gate.

    A reconciler read `issued_unknown`, and by the time it wrote, the effect had been
    verified applied. A blind write of `proved_not_applied` would put a terminal,
    verified effect back into `DISPATCHABLE_EFFECT_STATES` and licence a second
    dispatch of it -- so the write is refused and the terminal answer stands.
    """
    store = _RacingStore(**{EFFECT: EFFECT_SETTLED_APPLIED})
    store.effects[EFFECT] = EFFECT_ISSUED_UNKNOWN
    world = FakeWorld()
    coordinator = _coordinator(store, FakeJournal(), world)

    outcome = coordinator.reconcile(EFFECT, EFFECT_PROVED_NOT_APPLIED)

    assert outcome == DispatchOutcome(EFFECT, False, 0, False, STOP_EFFECT_NOT_RECONCILABLE)
    assert store.effects[EFFECT] == EFFECT_SETTLED_APPLIED
    assert world.issued == []
    # And the effect stays undispatchable, which is the fact the refusal protects.
    assert (
        _coordinator(store, FakeJournal(), world, attempt_id=OTHER_ATTEMPT)
        .dispatch_material(EFFECT)
        .stop_reason
        == STOP_EFFECT_NOT_DISPATCHABLE
    )
    assert world.issued == []
