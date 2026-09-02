"""Conformance oracles for the T-0688 bounded loop runner and its iteration ledger.

The seam under test is a provisional in-memory oracle holding no database, no
scheduler, no recovery, no transport and no Platform handle, so every test here is
built from frozen values. What is asserted is DOC-004 §AD.5 / REF-004 §D.4
behaviour as the runtime seam owes it:

- one :class:`IterationLaunch` is the atomic `RuntimeTransitionBundle` description
  and is never split, and every one of its members -- identity, ordinal, inputs,
  carry and scheduling intents -- survives into the ledger;
- a ledger entry fails closed on every incoherent combination it can be built
  with, rather than leaving the incoherence for a caller to notice;
- carry replays out of the ledger itself, so a crash and resume launches the next
  iteration with exactly the carry the crash interrupted, with no ambient caller
  reconstructing it, and a caller-supplied carry that disagrees is refused;
- a replayed ledger is contiguous in ordinal launch order -- no hole, no skipped
  prefix, no reordering -- never relaunches a recorded iteration, and refuses
  identity, input and carry drift;
- duplicate resolved identities and source keys, padded zip collisions included,
  are refused up front rather than at the second colliding launch;
- sequential and bounded-parallel planning is deterministic, the three zip
  mismatch policies behave as declared, and iteration overflow fails the loop
  instead of extending it;
- a nested loop's identities are namespace-safe against a sibling's;
- all three cancellation policies and all three partial-success policies behave as
  declared, and cancellation never rolls back an effect settlement;
- a late result is Evidence only: it never mutates the ledger, is never applied to
  Run state, and an unknown or duplicate one fails closed.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from omnivia_core_runtime.execution import (
    CANCEL_REMAINING,
    COMPLETE_ALL,
    DISPOSITION_CANCELLED_IN_FLIGHT,
    DRAIN_IN_FLIGHT,
    FAIL_LOOP,
    ITERATION_CANCELLED,
    ITERATION_OUTCOMES,
    ITERATION_SKIPPED,
    LOOP_MODE_PARALLEL,
    LOOP_MODE_SEQUENTIAL,
    LOOP_OUTCOME_INCOMPLETE,
    LOOP_RUNNING,
    LOOP_SETTLED,
    LOOP_STOPPING,
    ORDER_ITERATION_IDENTITY,
    ORDER_SETTLEMENT,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    RECORD_AND_CONTINUE,
    RECORD_AND_STOP_AFTER_CURRENT,
    ZIP_PAD_WITH_ABSENT,
    ZIP_REFUSE,
    ZIP_TRUNCATE_TO_SHORTEST,
    ExecutionContractError,
    ExecutionRefused,
    FrozenLoopPlan,
    IterationLaunch,
    IterationLedgerEntry,
    LateResult,
    LoopElement,
    LoopLedger,
    LoopRunner,
    canonical_hash,
    replay_ledger,
)
from omnivia_core_runtime.execution import loop as loop_module
from omnivia_core_runtime.execution import planes as planes_module
from omnivia_core_runtime.execution import profile as profile_module
from omnivia_core_runtime.execution import registry as registry_module
from omnivia_core_runtime.execution import workflow as workflow_module

# --------------------------------------------------------------------------
# Fixtures: frozen values only
# --------------------------------------------------------------------------

CARRY_ZERO = canonical_hash("carry-0")
CARRY_ONE = canonical_hash("carry-1")
CARRY_TWO = canonical_hash("carry-2")


def element(key: str) -> LoopElement:
    return LoopElement(key=key, payload_digest=canonical_hash(f"payload:{key}"))


def source(*keys: str) -> tuple[LoopElement, ...]:
    return tuple(element(key) for key in keys)


def sequential_plan(**overrides: object) -> FrozenLoopPlan:
    fields: dict[str, object] = {
        "loop_stable_id": "loop-outer",
        "mode": LOOP_MODE_SEQUENTIAL,
        "order_guarantee": ORDER_ITERATION_IDENTITY,
        "maximum_iterations": 8,
        "cancellation_policy": COMPLETE_ALL,
        "partial_success_policy": RECORD_AND_CONTINUE,
    }
    fields.update(overrides)
    return FrozenLoopPlan(**fields)  # type: ignore[arg-type]


def parallel_plan(**overrides: object) -> FrozenLoopPlan:
    fields: dict[str, object] = {
        "loop_stable_id": "loop-outer",
        "mode": LOOP_MODE_PARALLEL,
        "order_guarantee": ORDER_ITERATION_IDENTITY,
        "maximum_iterations": 8,
        "maximum_concurrency": 2,
        "cancellation_policy": COMPLETE_ALL,
        "partial_success_policy": RECORD_AND_CONTINUE,
    }
    fields.update(overrides)
    return FrozenLoopPlan(**fields)  # type: ignore[arg-type]


def launch_next(
    runner: LoopRunner, *, scheduling_intents: tuple[str, ...] = ()
) -> IterationLedgerEntry:
    """Record the first launch of the next wave, and return its ledger entry."""
    launch = runner.plan_launches(scheduling_intents=scheduling_intents)[0]
    return runner.record_launch(launch)


def succeed(
    runner: LoopRunner,
    entry: IterationLedgerEntry,
    *,
    resulting_carry_digest: str | None = None,
) -> IterationLedgerEntry:
    return runner.settle(
        entry.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=canonical_hash(f"out:{entry.ordinal}"),
        resulting_carry_digest=resulting_carry_digest,
    )


# --------------------------------------------------------------------------
# Planning: deterministic, bounded, and never a relaunch
# --------------------------------------------------------------------------


def test_a_sequential_plan_launches_exactly_one_iteration_at_a_time() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b", "c"))

    wave = runner.plan_launches()

    assert len(wave) == 1
    assert wave[0].ordinal == 0
    runner.record_launch(wave[0])
    assert runner.plan_launches() == ()


def test_a_parallel_plan_launches_up_to_its_concurrency_bound_and_no_further() -> None:
    runner = LoopRunner(
        parallel_plan(maximum_concurrency=2), source("a", "b", "c", "d")
    )

    wave = runner.plan_launches()

    assert [launch.ordinal for launch in wave] == [0, 1]
    for launch in wave:
        runner.record_launch(launch)
    assert runner.plan_launches() == ()


def test_planning_is_deterministic_for_two_runners_over_the_same_plan_and_source() -> (
    None
):
    """The wave is the lowest un-launched ordinals in order, and nothing else."""
    keys = ("delta", "alpha", "charlie", "bravo")
    first = LoopRunner(parallel_plan(maximum_concurrency=3), source(*keys))
    second = LoopRunner(parallel_plan(maximum_concurrency=3), source(*keys))

    assert first.plan_launches() == second.plan_launches()
    assert [launch.ordinal for launch in first.plan_launches()] == [0, 1, 2]


def test_planning_never_returns_an_iteration_the_ledger_already_records() -> None:
    plan = parallel_plan(maximum_concurrency=2)
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    first, second = runner.plan_launches()
    runner.record_launch(first)
    runner.record_launch(second)
    succeed(runner, runner.ledger.entries[0])

    wave = runner.plan_launches()

    assert [launch.ordinal for launch in wave] == [2]


def test_a_stopping_or_settled_loop_plans_no_launch() -> None:
    plan = sequential_plan(partial_success_policy=FAIL_LOOP)
    runner = LoopRunner(plan, source("a", "b", "c"))
    entry = launch_next(runner)
    runner.settle(
        entry.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="boom"
    )

    assert runner.state == LOOP_SETTLED
    assert runner.plan_launches() == ()
    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(
            IterationLaunch(
                iteration_identity=plan.iteration_identity("b"),
                ordinal=1,
                inputs_digest=canonical_hash("payload:b"),
                carry_digest=None,
                scheduling_intents=(),
            )
        )
    assert refusal.value.reason == "loop_not_launching"


# --------------------------------------------------------------------------
# Identity: stable, and namespace-safe under nesting
# --------------------------------------------------------------------------


def test_iteration_identity_is_stable_for_the_same_plan_and_element() -> None:
    plan = sequential_plan()

    assert plan.iteration_identity("a") == sequential_plan().iteration_identity("a")
    assert plan.iteration_identity("a") != plan.iteration_identity("b")


def test_a_nested_loop_names_identities_that_cannot_collide_with_a_siblings() -> None:
    """Two sibling outer iterations run the same inner loop over the same elements."""
    outer = sequential_plan(loop_stable_id="loop-outer")
    inner = sequential_plan(loop_stable_id="loop-inner")
    first_sibling = outer.iteration_identity("a")
    second_sibling = outer.iteration_identity("b")

    under_first = outer.nested(first_sibling, inner)
    under_second = outer.nested(second_sibling, inner)

    assert under_first.namespace == (first_sibling,)
    assert under_second.namespace == (second_sibling,)
    assert under_first.iteration_identity("x") != under_second.iteration_identity("x")
    assert under_first.iteration_identity("x") != inner.iteration_identity("x")
    # And the nested identities are just as stable as the un-nested ones.
    assert under_first.iteration_identity("x") == outer.nested(
        first_sibling, inner
    ).iteration_identity("x")


def test_two_nested_loops_run_side_by_side_never_share_an_iteration_identity() -> None:
    outer = sequential_plan(loop_stable_id="loop-outer")
    inner = sequential_plan(loop_stable_id="loop-inner")
    elements = source("x", "y")
    runners = [
        LoopRunner(outer.nested(outer.iteration_identity(key), inner), elements)
        for key in ("a", "b")
    ]

    identities = [
        launch.iteration_identity
        for runner in runners
        for launch in runner.plan_launches()
    ]

    assert len(set(identities)) == len(identities)


def test_a_namespace_nesting_beyond_the_bound_is_refused() -> None:
    depth = 17
    with pytest.raises(ExecutionContractError) as error:
        sequential_plan(namespace=tuple(canonical_hash(str(i)) for i in range(depth)))
    assert error.value.reason == "invalid_loop_plan"


def test_a_namespace_entry_that_is_not_a_digest_is_refused() -> None:
    with pytest.raises(ExecutionContractError) as error:
        sequential_plan(namespace=("not-a-digest",))
    assert error.value.reason == "invalid_digest"


# --------------------------------------------------------------------------
# Zip mismatch policies
# --------------------------------------------------------------------------


def test_zip_refuse_refuses_sources_of_different_lengths() -> None:
    with pytest.raises(ExecutionRefused) as refusal:
        LoopRunner(
            sequential_plan(zip_mismatch_policy=ZIP_REFUSE),
            source("a", "b", "c"),
            zip_sources=(source("p", "q"),),
        )
    assert refusal.value.reason == "zip_mismatch"


def test_zip_refuse_admits_sources_of_equal_length() -> None:
    runner = LoopRunner(
        sequential_plan(zip_mismatch_policy=ZIP_REFUSE),
        source("a", "b"),
        zip_sources=(source("p", "q"),),
    )

    assert len(runner.plan_launches()) == 1
    assert runner.plan_launches()[0].ordinal == 0


def test_zip_truncate_to_shortest_stops_at_the_shortest_source() -> None:
    runner = LoopRunner(
        sequential_plan(
            mode=LOOP_MODE_PARALLEL,
            maximum_concurrency=8,
            zip_mismatch_policy=ZIP_TRUNCATE_TO_SHORTEST,
        ),
        source("a", "b", "c"),
        zip_sources=(source("p", "q"),),
    )

    assert [launch.ordinal for launch in runner.plan_launches()] == [0, 1]


def test_zip_pad_with_absent_extends_to_the_longest_source() -> None:
    runner = LoopRunner(
        sequential_plan(
            mode=LOOP_MODE_PARALLEL,
            maximum_concurrency=8,
            zip_mismatch_policy=ZIP_PAD_WITH_ABSENT,
        ),
        source("a", "b"),
        zip_sources=(source("p", "q", "r"),),
    )

    assert [launch.ordinal for launch in runner.plan_launches()] == [0, 1, 2]


def test_a_zipped_input_digest_covers_every_zipped_source() -> None:
    """Two zip runs differing only in a zipped payload derive different inputs."""
    plan = sequential_plan(zip_mismatch_policy=ZIP_REFUSE)
    first = LoopRunner(plan, source("a"), zip_sources=(source("p"),))
    second = LoopRunner(plan, source("a"), zip_sources=(source("q"),))

    assert (
        first.plan_launches()[0].inputs_digest
        != second.plan_launches()[0].inputs_digest
    )
    assert (
        first.plan_launches()[0].iteration_identity
        == second.plan_launches()[0].iteration_identity
    )


def test_zipped_sources_without_a_policy_are_refused() -> None:
    with pytest.raises(ExecutionContractError) as error:
        LoopRunner(sequential_plan(), source("a"), zip_sources=(source("p"),))
    assert error.value.reason == "invalid_loop_plan"


def test_a_zip_policy_without_zipped_sources_is_refused() -> None:
    with pytest.raises(ExecutionContractError) as error:
        LoopRunner(sequential_plan(zip_mismatch_policy=ZIP_REFUSE), source("a"))
    assert error.value.reason == "invalid_loop_plan"


# --------------------------------------------------------------------------
# Duplicate identities are refused up front, not at the second launch
# --------------------------------------------------------------------------


def test_a_duplicate_source_key_is_refused_before_a_single_iteration_is_planned() -> (
    None
):
    with pytest.raises(ExecutionRefused) as refusal:
        LoopRunner(sequential_plan(), source("a", "b", "a"))
    assert refusal.value.reason == "duplicate_iteration"


def test_a_padded_zip_key_colliding_with_a_real_element_key_is_refused_up_front() -> (
    None
):
    """``pad-2`` is an ordinary identifier, so padding can collide with a real key."""
    with pytest.raises(ExecutionRefused) as refusal:
        LoopRunner(
            sequential_plan(zip_mismatch_policy=ZIP_PAD_WITH_ABSENT),
            source("a", "pad-2"),
            zip_sources=(source("p", "q", "r"),),
        )
    assert refusal.value.reason == "duplicate_iteration"


def test_a_padded_zip_without_a_collision_is_admitted() -> None:
    runner = LoopRunner(
        sequential_plan(zip_mismatch_policy=ZIP_PAD_WITH_ABSENT),
        source("a", "b"),
        zip_sources=(source("p", "q", "r"),),
    )

    assert runner.plan_launches()[0].ordinal == 0


# --------------------------------------------------------------------------
# One atomic launch: nothing in it is split, and nothing in it is discarded
# --------------------------------------------------------------------------


def test_the_bundle_unit_carries_every_member_of_the_atomic_launch() -> None:
    runner = LoopRunner(
        sequential_plan(carry_enabled=True), source("a"), carry_digest=CARRY_ZERO
    )

    launch = runner.plan_launches(scheduling_intents=("wake-timer", "budget-hold"))[0]
    unit = launch.bundle_unit

    assert unit == {
        "iterationIdentity": launch.iteration_identity,
        "ordinal": 0,
        "inputsDigest": launch.inputs_digest,
        "carryDigest": CARRY_ZERO,
        "schedulingIntents": ["wake-timer", "budget-hold"],
        "schedulingIntentsDigest": launch.scheduling_intents_digest,
    }
    assert launch.scheduling_intents_digest == canonical_hash(
        ["wake-timer", "budget-hold"]
    )


def test_record_launch_preserves_the_scheduling_intents_in_the_ledger() -> None:
    runner = LoopRunner(sequential_plan(), source("a"))

    entry = launch_next(runner, scheduling_intents=("wake-timer", "budget-hold"))

    assert entry.scheduling_intents == ("wake-timer", "budget-hold")
    assert entry.scheduling_intents_digest == canonical_hash(
        ["wake-timer", "budget-hold"]
    )
    assert runner.ledger.entries[0].scheduling_intents == ("wake-timer", "budget-hold")


def test_the_scheduling_intents_survive_a_settlement_and_a_replay() -> None:
    plan = sequential_plan()
    runner = LoopRunner(plan, source("a", "b"))
    entry = launch_next(runner, scheduling_intents=("wake-timer",))
    succeed(runner, entry)

    resumed = replay_ledger(plan, source("a", "b"), runner.ledger)

    assert resumed.ledger.entries[0].scheduling_intents == ("wake-timer",)


def test_a_launch_with_a_malformed_or_duplicated_scheduling_intent_is_refused() -> None:
    for intents in (("NOT-LOWERCASE",), ("wake-timer", "wake-timer")):
        with pytest.raises(ExecutionContractError):
            IterationLaunch(
                iteration_identity=canonical_hash("i"),
                ordinal=0,
                inputs_digest=canonical_hash("in"),
                carry_digest=None,
                scheduling_intents=intents,
            )


def test_a_relaunch_carrying_different_scheduling_intents_is_still_refused() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    launch = runner.plan_launches(scheduling_intents=("wake-timer",))[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(replace(launch, scheduling_intents=("budget-hold",)))

    assert refusal.value.reason == "iteration_already_launched"
    assert runner.ledger.entries[0].scheduling_intents == ("wake-timer",)


def test_a_launch_out_of_ordinal_order_is_refused_rather_than_leaving_a_hole() -> None:
    plan = parallel_plan(maximum_concurrency=2)
    runner = LoopRunner(plan, source("a", "b", "c"))
    _, second = runner.plan_launches()

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(second)

    assert refusal.value.reason == "ledger_not_contiguous"


# --------------------------------------------------------------------------
# The ledger entry fails closed on every incoherent combination
# --------------------------------------------------------------------------


def in_flight_entry(**overrides: object) -> IterationLedgerEntry:
    fields: dict[str, object] = {
        "iteration_identity": canonical_hash("identity"),
        "ordinal": 0,
        "inputs_digest": canonical_hash("inputs"),
    }
    fields.update(overrides)
    return IterationLedgerEntry(**fields)  # type: ignore[arg-type]


def test_a_ledger_entry_refuses_a_negative_ordinal() -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(ordinal=-1)
    assert error.value.reason == "invalid_ordinal"


@pytest.mark.parametrize(
    "field", ["carry_digest", "outputs_digest", "resulting_carry_digest"]
)
def test_a_ledger_entry_refuses_a_malformed_optional_digest(field: str) -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(**{field: "sha256:not-hex"})
    assert error.value.reason == "invalid_digest"


def test_a_ledger_entry_refuses_a_malformed_effect_settlement() -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(effect_settlements=("NOT-LOWERCASE",))
    assert error.value.reason == "invalid_identifier"


def test_a_ledger_entry_refuses_a_duplicate_effect_settlement() -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(effect_settlements=("effect-a", "effect-a"))
    assert error.value.reason == "duplicate_entry"


def test_a_ledger_entry_refuses_an_unknown_outcome() -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(outcome="LATE")
    assert error.value.reason == "unknown_vocabulary_member"


@pytest.mark.parametrize(
    "overrides",
    [
        # A settled entry with no settlement sequence, or a non-positive one.
        {"outcome": OUTCOME_SUCCEEDED, "outputs_digest": canonical_hash("o")},
        {
            "outcome": OUTCOME_SUCCEEDED,
            "outputs_digest": canonical_hash("o"),
            "settlement_sequence": 0,
        },
        # Succeeded without outputs, or with a failure alongside them.
        {"outcome": OUTCOME_SUCCEEDED, "settlement_sequence": 1},
        {
            "outcome": OUTCOME_SUCCEEDED,
            "outputs_digest": canonical_hash("o"),
            "failure_reason": "boom",
            "settlement_sequence": 1,
        },
        # Failed without a failure, or with outputs alongside it.
        {"outcome": OUTCOME_FAILED, "settlement_sequence": 1},
        {
            "outcome": OUTCOME_FAILED,
            "failure_reason": "boom",
            "outputs_digest": canonical_hash("o"),
            "settlement_sequence": 1,
        },
        # Cancelled or skipped carrying outputs or a failure.
        {
            "outcome": ITERATION_CANCELLED,
            "outputs_digest": canonical_hash("o"),
            "settlement_sequence": 1,
            "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
        },
        {
            "outcome": ITERATION_SKIPPED,
            "failure_reason": "boom",
            "settlement_sequence": 1,
        },
        # A cancellation disposition on a non-cancelled outcome, and a cancelled
        # iteration without one.
        {
            "outcome": ITERATION_SKIPPED,
            "settlement_sequence": 1,
            "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
        },
        {"outcome": ITERATION_CANCELLED, "settlement_sequence": 1},
        # A carry produced by an iteration that did not succeed.
        {
            "outcome": ITERATION_SKIPPED,
            "settlement_sequence": 1,
            "resulting_carry_digest": CARRY_ONE,
        },
        # An unsettled entry carrying any settlement member at all.
        {"outputs_digest": canonical_hash("o")},
        {"failure_reason": "boom"},
        {"settlement_sequence": 1},
        {"cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT},
        {"resulting_carry_digest": CARRY_ONE},
    ],
)
def test_a_ledger_entry_refuses_an_incoherent_settlement(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ExecutionContractError) as error:
        in_flight_entry(**overrides)
    # Named exactly: every case above is an incoherent settlement, and accepting a
    # second reason here would let one that failed on its vocabulary pass as one.
    assert error.value.reason == "invalid_settlement"


def test_a_ledger_entry_admits_the_coherent_settlements() -> None:
    coherent = [
        in_flight_entry(),
        in_flight_entry(
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=canonical_hash("o"),
            settlement_sequence=1,
            resulting_carry_digest=CARRY_ONE,
        ),
        in_flight_entry(
            outcome=OUTCOME_FAILED, failure_reason="boom", settlement_sequence=2
        ),
        in_flight_entry(
            outcome=ITERATION_CANCELLED,
            settlement_sequence=3,
            cancellation_disposition=DISPOSITION_CANCELLED_IN_FLIGHT,
        ),
        in_flight_entry(outcome=ITERATION_SKIPPED, settlement_sequence=4),
    ]

    assert [entry.is_settled for entry in coherent] == [False, True, True, True, True]
    assert {entry.outcome for entry in coherent[1:]} <= ITERATION_OUTCOMES


def test_a_settlement_refusal_leaves_the_settlement_sequence_untouched() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    entry = launch_next(runner)

    with pytest.raises(ExecutionContractError):
        runner.settle(entry.iteration_identity, outcome=OUTCOME_SUCCEEDED)

    settled = succeed(runner, runner.ledger.entries[0])
    assert settled.settlement_sequence == 1


# --------------------------------------------------------------------------
# Carry: threaded forward, and replayed out of the ledger itself
# --------------------------------------------------------------------------


def test_carry_threads_from_one_settled_iteration_into_the_next_launch() -> None:
    plan = sequential_plan(carry_enabled=True)
    runner = LoopRunner(plan, source("a", "b"), carry_digest=CARRY_ZERO)

    first = launch_next(runner)
    assert first.carry_digest == CARRY_ZERO
    succeed(runner, first, resulting_carry_digest=CARRY_ONE)

    assert runner.carry_digest == CARRY_ONE
    assert runner.plan_launches()[0].carry_digest == CARRY_ONE
    assert runner.ledger.entries[0].resulting_carry_digest == CARRY_ONE


def test_a_new_carrying_loop_requires_its_frozen_initial_carry() -> None:
    with pytest.raises(ExecutionContractError) as refusal:
        LoopRunner(sequential_plan(carry_enabled=True), source("a"))

    assert refusal.value.reason == "invalid_loop_plan"


def test_a_replayed_carrying_ledger_must_record_its_initial_carry() -> None:
    plan = sequential_plan(carry_enabled=True)
    entry = IterationLedgerEntry(
        iteration_identity=plan.iteration_identity("a"),
        ordinal=0,
        inputs_digest=element("a").payload_digest,
    )

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, source("a"), LoopLedger((entry,)))

    assert refusal.value.reason == "carry_drift"


def test_a_crash_and_resume_launches_the_next_iteration_with_the_carry_before_it() -> (
    None
):
    """The whole point: the ledger alone is enough, with no ambient caller."""
    plan = sequential_plan(carry_enabled=True)
    runner = LoopRunner(plan, source("a", "b", "c"), carry_digest=CARRY_ZERO)
    succeed(runner, launch_next(runner), resulting_carry_digest=CARRY_ONE)
    succeed(runner, launch_next(runner), resulting_carry_digest=CARRY_TWO)
    crashed_ledger = runner.ledger

    resumed = replay_ledger(plan, source("a", "b", "c"), crashed_ledger)

    assert resumed.carry_digest == CARRY_TWO
    next_launch = resumed.plan_launches()[0]
    assert next_launch.ordinal == 2
    assert next_launch.carry_digest == CARRY_TWO
    resumed.record_launch(next_launch)
    assert resumed.ledger.entries[2].carry_digest == CARRY_TWO


def test_a_resume_refuses_a_caller_supplied_carry_that_disagrees_with_the_ledger() -> (
    None
):
    plan = sequential_plan(carry_enabled=True)
    runner = LoopRunner(plan, source("a", "b"), carry_digest=CARRY_ZERO)
    succeed(runner, launch_next(runner), resulting_carry_digest=CARRY_ONE)

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, source("a", "b"), runner.ledger, carry_digest=CARRY_TWO)

    assert refusal.value.reason == "carry_drift"
    # The one the ledger actually launched against is accepted.
    agreed = replay_ledger(
        plan, source("a", "b"), runner.ledger, carry_digest=CARRY_ZERO
    )
    assert agreed.carry_digest == CARRY_ONE


def test_a_replay_refuses_a_recorded_launch_whose_carry_is_not_the_folded_one() -> None:
    plan = sequential_plan(carry_enabled=True)
    runner = LoopRunner(plan, source("a", "b"), carry_digest=CARRY_ZERO)
    succeed(runner, launch_next(runner), resulting_carry_digest=CARRY_ONE)
    launch_next(runner)
    drifted = (runner.ledger.entries[0], replace(runner.ledger.entries[1], carry_digest=CARRY_TWO))

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, source("a", "b"), LoopLedger(drifted))

    assert refusal.value.reason == "carry_drift"


def test_record_launch_refuses_a_launch_that_is_not_carrying_the_current_carry() -> (
    None
):
    plan = sequential_plan(carry_enabled=True)
    runner = LoopRunner(plan, source("a", "b"), carry_digest=CARRY_ZERO)
    launch = runner.plan_launches()[0]

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(replace(launch, carry_digest=CARRY_TWO))

    assert refusal.value.reason == "carry_drift"


def test_a_non_carrying_plan_refuses_a_launch_and_a_ledger_that_carry_one() -> None:
    plan = sequential_plan()
    runner = LoopRunner(plan, source("a", "b"))
    launch = runner.plan_launches()[0]
    assert launch.carry_digest is None

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(replace(launch, carry_digest=CARRY_ZERO))
    assert refusal.value.reason == "carry_drift"

    recorded = launch_next(runner)
    with pytest.raises(ExecutionRefused) as replayed:
        replay_ledger(
            plan,
            source("a", "b"),
            LoopLedger((replace(recorded, carry_digest=CARRY_ZERO),)),
        )
    assert replayed.value.reason == "carry_drift"


def test_a_carry_requires_a_carrying_plan_at_construction_and_at_settlement() -> None:
    with pytest.raises(ExecutionContractError) as error:
        LoopRunner(sequential_plan(), source("a"), carry_digest=CARRY_ZERO)
    assert error.value.reason == "invalid_loop_plan"

    runner = LoopRunner(sequential_plan(), source("a"))
    entry = launch_next(runner)
    with pytest.raises(ExecutionContractError) as settlement:
        succeed(runner, entry, resulting_carry_digest=CARRY_ONE)
    assert settlement.value.reason == "invalid_settlement"


def test_a_carrying_plan_must_be_sequential() -> None:
    with pytest.raises(ExecutionContractError) as error:
        parallel_plan(carry_enabled=True)
    assert error.value.reason == "invalid_loop_plan"


# --------------------------------------------------------------------------
# Replay: contiguous, no relaunch, no drift
# --------------------------------------------------------------------------


def three_element_ledger() -> tuple[
    FrozenLoopPlan, tuple[LoopElement, ...], LoopLedger
]:
    plan = parallel_plan(maximum_concurrency=3)
    elements = source("a", "b", "c")
    runner = LoopRunner(plan, elements)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    for entry in runner.ledger.entries:
        succeed(runner, entry)
    return plan, elements, runner.ledger


def test_a_replayed_ledger_is_reproduced_entry_for_entry() -> None:
    plan, elements, ledger = three_element_ledger()

    resumed = replay_ledger(plan, elements, ledger)

    assert resumed.ledger == ledger
    assert resumed.state == LOOP_SETTLED
    assert resumed.plan_launches() == ()


def test_a_ledger_with_a_hole_is_refused() -> None:
    plan, elements, ledger = three_element_ledger()

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, LoopLedger((ledger.entries[0], ledger.entries[2])))

    assert refusal.value.reason == "ledger_not_contiguous"


def test_a_ledger_with_an_arbitrary_skipped_prefix_is_refused() -> None:
    plan, elements, ledger = three_element_ledger()

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, LoopLedger(ledger.entries[1:]))

    assert refusal.value.reason == "ledger_not_contiguous"


def test_a_reordered_ledger_is_refused_rather_than_silently_sorted() -> None:
    plan, elements, ledger = three_element_ledger()

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(
            plan,
            elements,
            LoopLedger((ledger.entries[1], ledger.entries[0], ledger.entries[2])),
        )

    assert refusal.value.reason == "ledger_not_contiguous"


def test_a_replay_never_relaunches_a_settled_or_in_flight_iteration() -> None:
    plan = parallel_plan(maximum_concurrency=2)
    elements = source("a", "b", "c", "d")
    runner = LoopRunner(plan, elements)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    succeed(runner, runner.ledger.entries[0])

    resumed = replay_ledger(plan, elements, runner.ledger)

    # Ordinal 1 is still in flight, so exactly one slot of headroom remains and it
    # goes to the first ordinal the ledger does not record.
    assert [launch.ordinal for launch in resumed.plan_launches()] == [2]
    for recorded in resumed.ledger.entries:
        with pytest.raises(ExecutionRefused) as refusal:
            resumed.record_launch(
                IterationLaunch(
                    iteration_identity=recorded.iteration_identity,
                    ordinal=recorded.ordinal,
                    inputs_digest=recorded.inputs_digest,
                    carry_digest=None,
                    scheduling_intents=(),
                )
            )
        assert refusal.value.reason == "iteration_already_launched"


def test_a_replay_refuses_a_renamed_iteration_identity() -> None:
    plan, elements, ledger = three_element_ledger()
    renamed = (replace(ledger.entries[0], iteration_identity=canonical_hash("elsewhere")),)

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, LoopLedger(renamed))

    assert refusal.value.reason == "iteration_identity_drift"


def test_a_replay_refuses_a_drifted_inputs_digest() -> None:
    plan, elements, ledger = three_element_ledger()
    drifted = (replace(ledger.entries[0], inputs_digest=canonical_hash("elsewhere")),)

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, LoopLedger(drifted))

    assert refusal.value.reason == "iteration_identity_drift"


def test_a_replay_refuses_a_ledger_recorded_against_a_different_source() -> None:
    plan, _, ledger = three_element_ledger()

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, source("x", "y", "z"), ledger)

    assert refusal.value.reason == "iteration_identity_drift"


def test_a_replay_refuses_two_iterations_settled_at_the_same_sequence() -> None:
    plan, elements, ledger = three_element_ledger()
    collided = (
        ledger.entries[0],
        replace(ledger.entries[1], settlement_sequence=ledger.entries[0].settlement_sequence),
        ledger.entries[2],
    )

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, LoopLedger(collided))

    assert refusal.value.reason == "duplicate_settlement_sequence"


def test_a_replay_refuses_an_ordinal_beyond_the_frozen_iteration_bound() -> None:
    plan = parallel_plan(maximum_iterations=2, maximum_concurrency=2)
    elements = source("a", "b", "c")
    generous = parallel_plan(maximum_iterations=8, maximum_concurrency=3)
    runner = LoopRunner(generous, elements)
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(plan, elements, runner.ledger)

    assert refusal.value.reason == "loop_exhausted"


# --------------------------------------------------------------------------
# Frozen bounds: overflow fails the loop, it never extends it
# --------------------------------------------------------------------------


def test_maximum_iteration_overflow_fails_the_loop_rather_than_extending_it() -> None:
    plan = sequential_plan(maximum_iterations=2)
    runner = LoopRunner(plan, source("a", "b", "c"))

    succeed(runner, launch_next(runner))
    succeed(runner, launch_next(runner))

    assert runner.state == LOOP_SETTLED
    assert runner.plan_launches() == ()
    assert runner.failure_reason == "maximum_iterations_exceeded"
    assert len(runner.ledger.entries) == 2


def test_a_source_within_the_frozen_bound_settles_without_a_failure() -> None:
    runner = LoopRunner(sequential_plan(maximum_iterations=2), source("a", "b"))

    succeed(runner, launch_next(runner))
    succeed(runner, launch_next(runner))

    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason is None


def test_a_launch_beyond_the_frozen_bound_is_refused() -> None:
    """The frozen bound itself, and no other guard standing in for it.

    A sequential plan settles at its bound, so the refusal a settled loop gives is
    `loop_not_launching` and the bound is never reached. Keeping an iteration in flight
    under a parallel plan holds the loop `RUNNING` with concurrency headroom left and the
    ledger contiguous at the refused ordinal, so `loop_exhausted` is the only guard the
    launch can reach -- and the assertion names it rather than accepting either.
    """
    plan = parallel_plan(maximum_iterations=2, maximum_concurrency=2)
    runner = LoopRunner(plan, source("a", "b", "c"))
    succeed(runner, launch_next(runner))
    launch_next(runner)

    assert runner.state == LOOP_RUNNING
    assert len(runner.in_flight) == 1 < plan.maximum_concurrency

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_launch(
            IterationLaunch(
                iteration_identity=plan.iteration_identity("c"),
                ordinal=2,
                inputs_digest=canonical_hash("payload:c"),
                carry_digest=None,
                scheduling_intents=(),
            )
        )

    assert refusal.value.reason == "loop_exhausted"
    assert len(runner.ledger.entries) == 2


def test_an_invalid_frozen_bound_is_refused_at_the_plan() -> None:
    for overrides in (
        {"maximum_iterations": 0},
        {"maximum_iterations": 10_001},
        {"maximum_concurrency": 2},
    ):
        with pytest.raises(ExecutionContractError) as error:
            sequential_plan(**overrides)
        assert error.value.reason == "invalid_loop_bound"

    with pytest.raises(ExecutionContractError) as parallel_error:
        parallel_plan(maximum_iterations=2, maximum_concurrency=3)
    assert parallel_error.value.reason == "invalid_loop_bound"


# --------------------------------------------------------------------------
# Partial success policies
# --------------------------------------------------------------------------


def test_record_and_continue_keeps_launching_after_a_failed_iteration() -> None:
    plan = parallel_plan(
        maximum_concurrency=2, partial_success_policy=RECORD_AND_CONTINUE
    )
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    runner.settle(
        runner.ledger.entries[0].iteration_identity,
        outcome=OUTCOME_FAILED,
        failure_reason="boom",
    )

    assert runner.state == LOOP_RUNNING
    assert runner.failure_reason is None
    assert [launch.ordinal for launch in runner.plan_launches()] == [2]


def test_record_and_stop_after_current_stops_future_launch_after_the_current_wave() -> (
    None
):
    plan = parallel_plan(
        maximum_concurrency=2, partial_success_policy=RECORD_AND_STOP_AFTER_CURRENT
    )
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    runner.settle(
        runner.ledger.entries[0].iteration_identity,
        outcome=OUTCOME_FAILED,
        failure_reason="boom",
    )

    # The current wave is neither abandoned nor rolled back, and no wave follows it.
    assert runner.state == LOOP_STOPPING
    assert runner.plan_launches() == ()
    assert len(runner.in_flight) == 1

    succeed(runner, runner.ledger.entries[1])
    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason is None
    assert len(runner.ledger.entries) == 2


def test_fail_loop_records_the_failure_and_stops_the_loop() -> None:
    plan = parallel_plan(maximum_concurrency=2, partial_success_policy=FAIL_LOOP)
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    runner.settle(
        runner.ledger.entries[0].iteration_identity,
        outcome=OUTCOME_FAILED,
        failure_reason="boom",
    )

    assert runner.state == LOOP_STOPPING
    assert runner.failure_reason == "iteration_failed"
    assert runner.plan_launches() == ()

    succeed(runner, runner.ledger.entries[1])
    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason == "iteration_failed"


def test_a_replayed_failure_re_applies_the_declared_partial_success_policy() -> None:
    plan = sequential_plan(partial_success_policy=FAIL_LOOP)
    elements = source("a", "b", "c")
    runner = LoopRunner(plan, elements)
    entry = launch_next(runner)
    runner.settle(
        entry.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="boom"
    )

    resumed = replay_ledger(plan, elements, runner.ledger)

    assert resumed.state == LOOP_SETTLED
    assert resumed.failure_reason == "iteration_failed"
    assert resumed.plan_launches() == ()


# --------------------------------------------------------------------------
# Cancellation policies. Cancellation is never rollback.
# --------------------------------------------------------------------------


def test_cancel_remaining_settles_every_in_flight_iteration_as_cancelled() -> None:
    plan = parallel_plan(maximum_concurrency=2, cancellation_policy=CANCEL_REMAINING)
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    cancelled = runner.cancel()

    assert [entry.ordinal for entry in cancelled] == [0, 1]
    assert {entry.outcome for entry in cancelled} == {ITERATION_CANCELLED}
    assert {entry.cancellation_disposition for entry in cancelled} == {
        DISPOSITION_CANCELLED_IN_FLIGHT
    }
    assert runner.in_flight == ()
    assert runner.state == LOOP_SETTLED
    assert runner.cancellation_requested is True
    assert runner.plan_launches() == ()


def test_drain_in_flight_stops_launching_and_lets_the_in_flight_settle_normally() -> (
    None
):
    plan = parallel_plan(maximum_concurrency=2, cancellation_policy=DRAIN_IN_FLIGHT)
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    assert runner.cancel() == ()
    assert runner.state == LOOP_STOPPING
    assert len(runner.in_flight) == 2
    assert runner.plan_launches() == ()

    succeed(runner, runner.ledger.entries[0])
    succeed(runner, runner.ledger.entries[1])

    assert runner.state == LOOP_SETTLED
    assert [entry.outcome for entry in runner.ledger.entries] == [
        OUTCOME_SUCCEEDED,
        OUTCOME_SUCCEEDED,
    ]


def test_complete_all_records_the_request_and_lets_the_loop_finish() -> None:
    plan = parallel_plan(maximum_concurrency=2, cancellation_policy=COMPLETE_ALL)
    runner = LoopRunner(plan, source("a", "b", "c", "d"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    assert runner.cancel() == ()
    assert runner.cancellation_requested is True
    assert runner.state == LOOP_RUNNING

    succeed(runner, runner.ledger.entries[0])
    succeed(runner, runner.ledger.entries[1])
    assert [launch.ordinal for launch in runner.plan_launches()] == [2, 3]
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    succeed(runner, runner.ledger.entries[2])
    succeed(runner, runner.ledger.entries[3])

    assert runner.state == LOOP_SETTLED
    assert len(runner.ledger.entries) == 4


def test_cancellation_never_rolls_back_a_recorded_effect_settlement() -> None:
    plan = parallel_plan(maximum_concurrency=2, cancellation_policy=CANCEL_REMAINING)
    runner = LoopRunner(plan, source("a", "b", "c"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    runner.record_effect_settlement(runner.ledger.entries[0].iteration_identity, "effect-a")
    runner.record_effect_settlement(runner.ledger.entries[0].iteration_identity, "effect-b")

    runner.cancel()

    assert runner.ledger.entries[0].outcome == ITERATION_CANCELLED
    assert runner.ledger.entries[0].effect_settlements == ("effect-a", "effect-b")


def test_a_settled_iteration_records_no_further_effect_settlement() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    entry = launch_next(runner)
    succeed(runner, entry)

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_effect_settlement(entry.iteration_identity, "effect-a")

    assert refusal.value.reason == "iteration_already_settled"


def test_the_same_effect_settlement_is_never_recorded_twice() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    entry = launch_next(runner)
    runner.record_effect_settlement(entry.iteration_identity, "effect-a")

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_effect_settlement(entry.iteration_identity, "effect-a")

    assert refusal.value.reason == "duplicate_effect_settlement"
    assert runner.ledger.entries[0].effect_settlements == ("effect-a",)


def test_an_effect_settlement_against_an_unknown_iteration_is_refused() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    launch_next(runner)

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_effect_settlement(canonical_hash("elsewhere"), "effect-a")

    assert refusal.value.reason == "unknown_iteration"


def test_an_iteration_settles_exactly_once() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    entry = launch_next(runner)
    succeed(runner, entry)

    with pytest.raises(ExecutionRefused) as refusal:
        runner.settle(entry.iteration_identity, outcome=ITERATION_SKIPPED)

    assert refusal.value.reason == "iteration_already_settled"


# --------------------------------------------------------------------------
# Gather follows the declared order guarantee
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order_guarantee", "expected_ordinals"),
    [(ORDER_ITERATION_IDENTITY, [0, 1]), (ORDER_SETTLEMENT, [1, 0])],
)
def test_gather_follows_the_declared_order_guarantee(
    order_guarantee: str, expected_ordinals: list[int]
) -> None:
    plan = parallel_plan(maximum_concurrency=2, order_guarantee=order_guarantee)
    runner = LoopRunner(plan, source("a", "b"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    # Settled out of ordinal order on purpose.
    succeed(runner, runner.ledger.entries[1])
    succeed(runner, runner.ledger.entries[0])

    assert runner.gather() == tuple(
        canonical_hash(f"out:{ordinal}") for ordinal in expected_ordinals
    )


def test_gather_omits_every_iteration_that_did_not_succeed() -> None:
    plan = parallel_plan(maximum_concurrency=3)
    runner = LoopRunner(plan, source("a", "b", "c"))
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    succeed(runner, runner.ledger.entries[0])
    runner.settle(
        runner.ledger.entries[1].iteration_identity,
        outcome=OUTCOME_FAILED,
        failure_reason="boom",
    )
    runner.settle(runner.ledger.entries[2].iteration_identity, outcome=ITERATION_SKIPPED)

    # A recorded failure makes the loop `INCOMPLETE`, so the prefix is handed back only
    # to a caller that asked for a partial one -- the omission itself is unchanged.
    assert runner.outcome == LOOP_OUTCOME_INCOMPLETE
    with pytest.raises(ExecutionRefused) as refusal:
        runner.gather()
    assert refusal.value.reason == "loop_incomplete"
    assert runner.gather(partial=True) == (canonical_hash("out:0"),)


# --------------------------------------------------------------------------
# Late results: Evidence, and nothing else
# --------------------------------------------------------------------------


def settled_runner() -> LoopRunner:
    runner = LoopRunner(sequential_plan(), source("a"))
    succeed(runner, launch_next(runner))
    assert runner.settled
    return runner


def test_a_late_result_is_evidence_only_and_never_mutates_the_ledger() -> None:
    runner = settled_runner()
    before = runner.ledger

    late = runner.record_late_result(
        before.entries[0].iteration_identity, evidence_ref="evidence-1"
    )

    assert runner.ledger == before
    assert runner.late_results == (late,)
    assert late.applied_to_run_state is False
    assert late.iteration_identity == before.entries[0].iteration_identity
    # Nothing about the settled iteration moved.
    assert runner.ledger.entries[0].outcome == OUTCOME_SUCCEEDED
    assert runner.ledger.entries[0].outputs_digest == canonical_hash("out:0")
    assert runner.gather() == (canonical_hash("out:0"),)


def test_a_late_result_can_never_be_marked_applied_to_run_state() -> None:
    with pytest.raises(ExecutionContractError) as error:
        LateResult(
            iteration_identity=canonical_hash("i"),
            evidence_ref="evidence-1",
            applied_to_run_state=True,
        )
    assert error.value.reason == "late_result_applied"


def test_a_result_before_the_loop_settles_is_not_late() -> None:
    runner = LoopRunner(sequential_plan(), source("a", "b"))
    entry = launch_next(runner)

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_late_result(entry.iteration_identity, evidence_ref="evidence-1")

    assert refusal.value.reason == "loop_not_settled"
    assert runner.late_results == ()


def test_a_late_result_for_an_unknown_iteration_is_refused() -> None:
    runner = settled_runner()

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_late_result(
            canonical_hash("elsewhere"), evidence_ref="evidence-1"
        )

    assert refusal.value.reason == "unknown_iteration"
    assert runner.late_results == ()


def test_a_duplicate_late_result_for_one_iteration_is_refused() -> None:
    runner = settled_runner()
    identity = runner.ledger.entries[0].iteration_identity
    runner.record_late_result(identity, evidence_ref="evidence-1")

    with pytest.raises(ExecutionRefused) as refusal:
        runner.record_late_result(identity, evidence_ref="evidence-2")

    assert refusal.value.reason == "duplicate_late_result"
    assert len(runner.late_results) == 1


def test_a_replayed_late_result_is_admitted_against_the_replayed_ledger() -> None:
    runner = settled_runner()
    identity = runner.ledger.entries[0].iteration_identity
    late = runner.record_late_result(identity, evidence_ref="evidence-1")

    resumed = replay_ledger(
        sequential_plan(), source("a"), runner.ledger, late_results=(late,)
    )

    assert resumed.late_results == (late,)
    assert resumed.ledger == runner.ledger


def test_a_replayed_late_result_for_an_unknown_iteration_is_refused() -> None:
    runner = settled_runner()
    stray = LateResult(
        iteration_identity=canonical_hash("elsewhere"), evidence_ref="evidence-1"
    )

    with pytest.raises(ExecutionRefused) as refusal:
        replay_ledger(
            sequential_plan(), source("a"), runner.ledger, late_results=(stray,)
        )

    assert refusal.value.reason == "unknown_iteration"


# --------------------------------------------------------------------------
# The public API, and the seam's posture
# --------------------------------------------------------------------------


def test_the_public_runner_api_is_exported_from_the_package() -> None:
    package = __import__("omnivia_core_runtime.execution", fromlist=["__all__"])

    for name in loop_module.__all__:
        assert name in package.__all__, name
        assert getattr(package, name) is getattr(loop_module, name), name


def test_the_exported_runner_api_collides_with_no_other_module_in_the_seam() -> None:
    """One ontology per name: no other seam module may export any of these."""
    others = (planes_module, profile_module, registry_module, workflow_module)
    exported = set(loop_module.__all__)

    for module in others:
        assert exported.isdisjoint(set(module.__all__)), module.__name__


def test_the_package_all_is_exactly_what_the_package_exports() -> None:
    package = __import__("omnivia_core_runtime.execution", fromlist=["__all__"])

    assert len(package.__all__) == len(set(package.__all__))
    for name in package.__all__:
        assert hasattr(package, name), name


def test_the_loop_oracle_imports_no_storage_scheduler_recovery_or_platform() -> None:
    """An allowlist, not a denylist: a new coupling has to be added here first."""
    allowed = {
        "__future__",
        "dataclasses",
        "typing",
        "omnivia_core_runtime",
    }
    source_path = Path(loop_module.__file__ or "")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    assert imported <= allowed, imported - allowed
    text = source_path.read_text(encoding="utf-8")
    assert "omnivia_core_runtime.storage" not in text
    assert "omnivia_core_runtime.service" not in text


def test_the_loop_oracle_is_documented_as_provisional_and_non_authoritative() -> None:
    """The "not a second runtime" statement is load-bearing and must not be dropped."""
    text = " ".join(
        Path(loop_module.__file__ or "").read_text(encoding="utf-8").split()
    )

    assert "provisional in-memory conformance oracle" in text
    assert "not a second runtime and not a second scheduler" in text
    assert "never writes canonical state" in text
