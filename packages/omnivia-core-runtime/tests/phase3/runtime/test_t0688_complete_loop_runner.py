"""Conformance oracles for the T-0688 bounded loop runner and iteration ledger.

The seam is a pure in-memory oracle: no database, no scheduler, no recovery, no
transport and no Platform handle, so every test here is built from frozen values
and the runner's own returned values and refusals. What is asserted is the
seam's own contract:

- the loop names live beside the legacy ``LoopDefinition``/``LoopController``
  spend bound without colliding with it, and are exported once;
- a ``FrozenLoopPlan`` validates its mode, order guarantee, bounds, policies,
  carry and nesting eagerly, and derives namespace-safe iteration identities;
- a sequential plan launches one iteration at a time and a parallel plan a wave
  bounded by ``maximum_concurrency``, both deterministically and both never
  beyond the frozen ``maximum_iterations``;
- one launch is one atomic ``bundle_unit`` with exactly six members and a
  scheduling-intents digest, and is never split;
- ``record_launch`` refuses identity, inputs, carry, scheduling-order,
  duplicate and concurrency drift;
- a replayed ledger preserves every recorded identity, relaunches nothing it
  records, refuses holes, reordering and drift, and folds the carry forward out
  of the recorded result carry rather than out of an ambient caller;
- the zip mismatch policies answer REFUSE/TRUNCATE/PAD and a padded key that
  collides with a real element key is refused;
- an iteration beyond ``maximum_iterations`` settles the loop failed and never
  extends the bound;
- every cancellation and partial-success policy behaves as declared, and a
  recorded effect settlement survives cancellation because cancellation is not
  rollback;
- an ``IterationLedgerEntry`` refuses every outcome/output/failure/sequence/
  cancellation incoherence;
- a late result is Evidence only: the ledger is unchanged, and an unknown or
  duplicate late result fails closed.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
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
    LOOP_CANCELLATION_POLICIES,
    LOOP_MODE_PARALLEL,
    LOOP_MODE_SEQUENTIAL,
    LOOP_MODES,
    LOOP_ORDER_GUARANTEES,
    LOOP_OUTCOME_CANCELLED,
    LOOP_OUTCOME_FAILED,
    LOOP_OUTCOME_INCOMPLETE,
    LOOP_OUTCOME_SUCCEEDED,
    LOOP_PARTIAL_SUCCESS_POLICIES,
    LOOP_RUNNING,
    LOOP_SETTLED,
    LOOP_STATES,
    LOOP_STOPPING,
    LOOP_ZIP_MISMATCH_POLICIES,
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
    LoopController,
    LoopDefinition,
    LoopElement,
    LoopLedger,
    LoopRunner,
    canonical_hash,
    replay_ledger,
)
from omnivia_core_runtime.execution import __all__ as package_exports
from omnivia_core_runtime.execution import loop as loop_module
from omnivia_core_runtime.execution import workflow as workflow_module

# --------------------------------------------------------------------------
# Fixtures-by-construction: frozen values, no fixtures, no I/O
# --------------------------------------------------------------------------


def _digest(label: str) -> str:
    """Return a well-formed ``sha256:`` digest deterministically derived from ``label``."""
    return canonical_hash(label)


def _elements(*keys: str) -> tuple[LoopElement, ...]:
    return tuple(
        LoopElement(key=key, payload_digest=_digest(f"payload:{key}")) for key in keys
    )


def _sequential(**overrides: object) -> FrozenLoopPlan:
    base: dict[str, object] = {
        "loop_stable_id": "loop.review",
        "mode": LOOP_MODE_SEQUENTIAL,
        "order_guarantee": ORDER_ITERATION_IDENTITY,
        "maximum_iterations": 8,
        "cancellation_policy": DRAIN_IN_FLIGHT,
        "partial_success_policy": RECORD_AND_CONTINUE,
    }
    base.update(overrides)
    return FrozenLoopPlan(**base)  # type: ignore[arg-type]


def _parallel(**overrides: object) -> FrozenLoopPlan:
    base: dict[str, object] = {
        "loop_stable_id": "loop.fanout",
        "mode": LOOP_MODE_PARALLEL,
        "order_guarantee": ORDER_SETTLEMENT,
        "maximum_iterations": 8,
        "cancellation_policy": CANCEL_REMAINING,
        "partial_success_policy": RECORD_AND_CONTINUE,
        "maximum_concurrency": 2,
    }
    base.update(overrides)
    return FrozenLoopPlan(**base)  # type: ignore[arg-type]


def _launch_all(
    runner: LoopRunner, **kwargs: object
) -> tuple[IterationLedgerEntry, ...]:
    """Record every launch of the next wave, in the order the runner planned it."""
    return tuple(
        runner.record_launch(launch)
        for launch in runner.plan_launches(**kwargs)  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Exports and module hygiene
# --------------------------------------------------------------------------


def test_the_loop_runner_is_exported_and_does_not_collide_with_the_legacy_loop_names() -> (
    None
):
    """Two loop questions, two vocabularies: the seam never conflates spend with iterations."""
    for name in (
        "FrozenLoopPlan",
        "IterationLaunch",
        "IterationLedgerEntry",
        "LateResult",
        "LoopElement",
        "LoopRunner",
        "replay_ledger",
    ):
        assert name in package_exports

    # The legacy spend bound is still importable and is a different type.
    assert LoopDefinition is not FrozenLoopPlan
    assert LoopController is not LoopRunner
    assert not hasattr(loop_module, "LoopDefinition")
    assert not hasattr(loop_module, "LoopController")

    # No export is claimed by both modules, so the package re-export is unambiguous.
    assert set(loop_module.__all__).isdisjoint(workflow_module.__all__)


def test_the_package_export_list_is_unique_and_covers_every_loop_name() -> None:
    assert len(package_exports) == len(set(package_exports))
    assert len(loop_module.__all__) == len(set(loop_module.__all__))
    assert set(loop_module.__all__) <= set(package_exports)


def test_the_loop_oracle_imports_no_storage_scheduler_recovery_or_platform() -> None:
    """An allowlist, not a denylist: a new coupling has to be added here first."""
    allowed = {"__future__", "dataclasses", "typing", "omnivia_core_runtime"}
    source = Path(loop_module.__file__ or "")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    assert imported <= allowed, imported - allowed
    text = source.read_text(encoding="utf-8")
    assert "omnivia_core_runtime.storage" not in text
    assert "omnivia_core_runtime.service" not in text
    assert "never writes canonical state" in text


def test_the_closed_loop_vocabularies_are_exactly_their_declared_members() -> None:
    assert LOOP_MODES == {LOOP_MODE_SEQUENTIAL, LOOP_MODE_PARALLEL}
    assert LOOP_ORDER_GUARANTEES == {ORDER_ITERATION_IDENTITY, ORDER_SETTLEMENT}
    assert LOOP_CANCELLATION_POLICIES == {
        CANCEL_REMAINING,
        DRAIN_IN_FLIGHT,
        COMPLETE_ALL,
    }
    assert LOOP_PARTIAL_SUCCESS_POLICIES == {
        FAIL_LOOP,
        RECORD_AND_CONTINUE,
        RECORD_AND_STOP_AFTER_CURRENT,
    }
    assert LOOP_ZIP_MISMATCH_POLICIES == {
        ZIP_REFUSE,
        ZIP_TRUNCATE_TO_SHORTEST,
        ZIP_PAD_WITH_ABSENT,
    }
    assert LOOP_STATES == {LOOP_RUNNING, LOOP_STOPPING, LOOP_SETTLED}
    assert ITERATION_OUTCOMES == {
        OUTCOME_SUCCEEDED,
        OUTCOME_FAILED,
        ITERATION_CANCELLED,
        ITERATION_SKIPPED,
    }
    # Every member is the exact uppercase internal spelling, never a wire spelling.
    for vocabulary in (
        LOOP_MODES,
        LOOP_ORDER_GUARANTEES,
        LOOP_CANCELLATION_POLICIES,
        LOOP_PARTIAL_SUCCESS_POLICIES,
        LOOP_ZIP_MISMATCH_POLICIES,
        LOOP_STATES,
        ITERATION_OUTCOMES,
    ):
        for member in vocabulary:
            assert member == member.upper()


# --------------------------------------------------------------------------
# FrozenLoopPlan validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"loop_stable_id": "Loop.Review"}, "invalid_identifier"),
        ({"mode": "sequential"}, "unknown_vocabulary_member"),
        ({"order_guarantee": "ANY_ORDER"}, "unknown_vocabulary_member"),
        ({"cancellation_policy": "STOP"}, "unknown_vocabulary_member"),
        ({"partial_success_policy": "IGNORE"}, "unknown_vocabulary_member"),
        ({"zip_mismatch_policy": "PAD"}, "unknown_vocabulary_member"),
        ({"maximum_iterations": 0}, "invalid_loop_bound"),
        ({"maximum_iterations": -1}, "invalid_loop_bound"),
        ({"maximum_iterations": 10_001}, "invalid_loop_bound"),
        ({"maximum_concurrency": 2}, "invalid_loop_bound"),
        ({"maximum_concurrency": 0}, "invalid_loop_bound"),
        ({"carry_enabled": True, "mode": LOOP_MODE_PARALLEL}, "invalid_loop_plan"),
        ({"namespace": ("not-a-digest",)}, "invalid_digest"),
        ({"namespace": tuple(_digest(str(n)) for n in range(17))}, "invalid_loop_plan"),
    ],
)
def test_a_frozen_loop_plan_refuses_an_invalid_member(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as error:
        _sequential(**overrides)
    assert error.value.reason == reason


def test_a_parallel_plan_requires_a_concurrency_bound_within_its_iteration_bound() -> (
    None
):
    with pytest.raises(ExecutionContractError) as error:
        _parallel(maximum_iterations=2, maximum_concurrency=3)
    assert error.value.reason == "invalid_loop_bound"

    plan = _parallel(maximum_iterations=2, maximum_concurrency=2)
    assert plan.maximum_concurrency == 2


def test_a_sequential_plan_runs_exactly_one_iteration_at_a_time() -> None:
    assert _sequential().maximum_concurrency == 1


def test_the_frozen_plan_is_immutable_so_a_live_run_cannot_acquire_a_new_bound() -> (
    None
):
    plan = _sequential()
    with pytest.raises(FrozenInstanceError):
        plan.maximum_iterations = 99  # type: ignore[misc]


# --------------------------------------------------------------------------
# Iteration identity, including nesting
# --------------------------------------------------------------------------


def test_an_iteration_identity_is_deterministic_and_element_scoped() -> None:
    plan = _sequential()
    assert plan.iteration_identity("alpha") == plan.iteration_identity("alpha")
    assert plan.iteration_identity("alpha") != plan.iteration_identity("beta")
    assert plan.iteration_identity("alpha") != _sequential(
        loop_stable_id="loop.other"
    ).iteration_identity("alpha")


def test_a_nested_loop_names_iterations_that_cannot_collide_with_a_sibling_iteration() -> (
    None
):
    outer = _sequential(loop_stable_id="loop.outer")
    inner = _sequential(loop_stable_id="loop.inner")

    first = outer.nested(outer.iteration_identity("a"), inner)
    second = outer.nested(outer.iteration_identity("b"), inner)

    assert first.namespace != second.namespace
    assert first.iteration_identity("x") != second.iteration_identity("x")
    assert first.iteration_identity("x") != inner.iteration_identity("x")
    # Namespacing changes nothing else about the nested plan.
    assert replace(first, namespace=()) == inner


def test_nesting_two_deep_stays_namespace_safe_and_refuses_a_non_digest_namespace() -> (
    None
):
    outer = _sequential(loop_stable_id="loop.outer")
    middle = outer.nested(
        outer.iteration_identity("a"), _sequential(loop_stable_id="loop.mid")
    )
    innermost = middle.nested(
        middle.iteration_identity("m"), _sequential(loop_stable_id="loop.inner")
    )

    assert len(innermost.namespace) == 2
    assert innermost.iteration_identity("x") != middle.iteration_identity("x")

    with pytest.raises(ExecutionContractError) as error:
        outer.nested("outer-iteration-1", _sequential())
    assert error.value.reason == "invalid_digest"


# --------------------------------------------------------------------------
# Sequential and parallel launching
# --------------------------------------------------------------------------


def test_a_sequential_plan_launches_one_deterministic_iteration_at_a_time() -> None:
    plan = _sequential()
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)

    first = runner.plan_launches()
    assert len(first) == 1
    assert runner.plan_launches() == first, "planning is a pure function of the ledger"
    assert first[0].ordinal == 0
    assert first[0].iteration_identity == plan.iteration_identity("a")
    assert first[0].inputs_digest == source[0].payload_digest

    runner.record_launch(first[0])
    assert runner.plan_launches() == (), "the concurrency bound of one is saturated"

    runner.settle(
        first[0].iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("out-a"),
    )
    second = runner.plan_launches()
    assert [launch.ordinal for launch in second] == [1]
    assert second[0].iteration_identity == plan.iteration_identity("b")


def test_a_parallel_plan_launches_a_wave_bounded_by_maximum_concurrency() -> None:
    plan = _parallel(maximum_concurrency=2, maximum_iterations=5)
    source = _elements("a", "b", "c", "d", "e")
    runner = LoopRunner(plan, source)

    wave = runner.plan_launches()
    assert [launch.ordinal for launch in wave] == [0, 1]
    _launch_all(runner)
    assert runner.plan_launches() == ()
    assert len(runner.in_flight) == 2

    runner.settle(
        wave[0].iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )
    refilled = runner.plan_launches()
    assert [launch.ordinal for launch in refilled] == [2]
    assert refilled[0].iteration_identity == plan.iteration_identity("c")


def test_a_wave_is_capped_by_the_remaining_source_and_the_frozen_iteration_bound() -> (
    None
):
    runner = LoopRunner(
        _parallel(maximum_concurrency=4, maximum_iterations=8), _elements("a", "b")
    )
    assert [launch.ordinal for launch in runner.plan_launches()] == [0, 1]

    bounded = LoopRunner(
        _parallel(maximum_concurrency=3, maximum_iterations=3),
        _elements("a", "b", "c", "d", "e"),
    )
    assert [launch.ordinal for launch in bounded.plan_launches()] == [0, 1, 2]


def test_a_stopping_or_settled_loop_plans_no_further_launch() -> None:
    runner = LoopRunner(
        _sequential(cancellation_policy=DRAIN_IN_FLIGHT), _elements("a", "b")
    )
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)
    runner.cancel()

    assert runner.state == LOOP_STOPPING
    assert runner.plan_launches() == ()
    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o"),
    )
    assert runner.state == LOOP_SETTLED
    assert runner.plan_launches() == ()


# --------------------------------------------------------------------------
# The atomic launch unit
# --------------------------------------------------------------------------


def test_one_launch_is_exactly_one_atomic_bundle_unit() -> None:
    plan = _sequential(carry_enabled=True)
    source = _elements("a")
    carry = _digest("carry-0")
    runner = LoopRunner(plan, source, carry_digest=carry)

    launch = runner.plan_launches(scheduling_intents=("lease.worker", "budget.hold"))[0]
    unit = launch.bundle_unit

    assert unit == {
        "iterationIdentity": plan.iteration_identity("a"),
        "ordinal": 0,
        "inputsDigest": source[0].payload_digest,
        "carryDigest": carry,
        "schedulingIntents": ["lease.worker", "budget.hold"],
        "schedulingIntentsDigest": canonical_hash(["lease.worker", "budget.hold"]),
    }
    assert set(unit) == {
        "iterationIdentity",
        "ordinal",
        "inputsDigest",
        "carryDigest",
        "schedulingIntents",
        "schedulingIntentsDigest",
    }
    assert launch.scheduling_intents_digest == unit["schedulingIntentsDigest"]
    # The digest is order-sensitive, because the intents are an ordered list.
    assert launch.scheduling_intents_digest != canonical_hash(
        ["budget.hold", "lease.worker"]
    )


def test_a_non_carrying_launch_still_carries_the_carry_member_explicitly() -> None:
    runner = LoopRunner(_sequential(), _elements("a"))
    unit = runner.plan_launches()[0].bundle_unit

    assert unit["carryDigest"] is None
    assert unit["schedulingIntents"] == []
    assert unit["schedulingIntentsDigest"] == canonical_hash([])


def test_a_launch_refuses_a_malformed_member() -> None:
    good = _digest("ok")
    with pytest.raises(ExecutionContractError) as identity:
        IterationLaunch("not-a-digest", 0, good, None, ())
    assert identity.value.reason == "invalid_digest"

    with pytest.raises(ExecutionContractError) as ordinal:
        IterationLaunch(good, -1, good, None, ())
    assert ordinal.value.reason == "invalid_ordinal"

    with pytest.raises(ExecutionContractError) as intents:
        IterationLaunch(good, 0, good, None, ("lease.worker", "lease.worker"))
    assert intents.value.reason == "duplicate_entry"

    with pytest.raises(ExecutionContractError) as spelling:
        IterationLaunch(good, 0, good, None, ("LEASE",))
    assert spelling.value.reason == "invalid_identifier"


# --------------------------------------------------------------------------
# record_launch: the six drifts
# --------------------------------------------------------------------------


def test_record_launch_preserves_every_member_of_the_atomic_unit() -> None:
    plan = _sequential(carry_enabled=True)
    carry = _digest("carry-0")
    runner = LoopRunner(plan, _elements("a"), carry_digest=carry)
    launch = runner.plan_launches(scheduling_intents=("lease.worker",))[0]

    entry = runner.record_launch(launch)

    assert entry.iteration_identity == launch.iteration_identity
    assert entry.ordinal == launch.ordinal
    assert entry.inputs_digest == launch.inputs_digest
    assert entry.carry_digest == carry
    assert entry.scheduling_intents == ("lease.worker",)
    assert entry.scheduling_intents_digest == launch.scheduling_intents_digest
    assert not entry.is_settled
    assert runner.ledger.entries == (entry,)
    assert runner.in_flight == (entry,)


def test_record_launch_refuses_identity_drift() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]

    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(replace(launch, iteration_identity=_digest("elsewhere")))
    assert error.value.reason == "iteration_identity_drift"
    assert runner.ledger.entries == ()


def test_record_launch_refuses_inputs_drift() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]

    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(replace(launch, inputs_digest=_digest("other-payload")))
    assert error.value.reason == "iteration_identity_drift"


def test_record_launch_refuses_carry_drift_in_both_directions() -> None:
    carrying = LoopRunner(
        _sequential(carry_enabled=True), _elements("a"), carry_digest=_digest("carry-0")
    )
    launch = carrying.plan_launches()[0]
    with pytest.raises(ExecutionRefused) as stale:
        carrying.record_launch(replace(launch, carry_digest=_digest("stale-carry")))
    assert stale.value.reason == "carry_drift"

    with pytest.raises(ExecutionRefused) as dropped:
        carrying.record_launch(replace(launch, carry_digest=None))
    assert dropped.value.reason == "carry_drift"

    plain = LoopRunner(_sequential(), _elements("a"))
    with pytest.raises(ExecutionRefused) as invented:
        plain.record_launch(
            replace(plain.plan_launches()[0], carry_digest=_digest("invented"))
        )
    assert invented.value.reason == "carry_drift"


def test_record_launch_refuses_a_scheduling_order_that_would_leave_a_hole() -> None:
    """Launches are scheduled in ordinal order; a gap is refused before it exists."""
    plan = _parallel(maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    second = runner.plan_launches()[1]

    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(second)
    assert error.value.reason == "ledger_not_contiguous"
    assert runner.ledger.entries == ()


def test_record_launch_refuses_a_duplicate_relaunch_by_ordinal_or_identity() -> None:
    runner = LoopRunner(_parallel(maximum_concurrency=2), _elements("a", "b"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as in_flight:
        runner.record_launch(launch)
    assert in_flight.value.reason == "iteration_already_launched"

    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o"),
    )
    with pytest.raises(ExecutionRefused) as settled:
        runner.record_launch(launch)
    assert settled.value.reason == "iteration_already_launched"
    assert len(runner.ledger.entries) == 1


def test_record_launch_refuses_a_launch_beyond_the_saturated_concurrency_bound() -> (
    None
):
    plan = _parallel(maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    _launch_all(runner)

    third = IterationLaunch(
        iteration_identity=plan.iteration_identity("c"),
        ordinal=2,
        inputs_digest=source[2].payload_digest,
        carry_digest=None,
        scheduling_intents=(),
    )
    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(third)
    assert error.value.reason == "concurrency_exhausted"


def test_record_launch_refuses_a_launch_beyond_the_bound_source() -> None:
    """The source bound, with no other guard able to have produced the refusal.

    A plan whose iteration bound is larger than its source, with an iteration still in
    flight, keeps the loop `RUNNING` with concurrency headroom to spare and the ledger
    contiguous at the refused ordinal -- so `unknown_iteration` is the only guard left,
    and the frozen bound is nowhere near. That bound has its own reachable case in
    `test_the_maximum_iterations_guard_is_reachable_on_its_own`; a launch past a settled
    loop has one in `test_a_settled_loop_launches_nothing_further`.
    """
    plan = _parallel(maximum_iterations=8, maximum_concurrency=3)
    source = _elements("a", "b")
    runner = LoopRunner(plan, source)
    for launch in runner.plan_launches():
        runner.record_launch(launch)

    assert runner.state == LOOP_RUNNING
    assert len(runner.in_flight) == 2 < plan.maximum_concurrency
    beyond_source = IterationLaunch(
        iteration_identity=plan.iteration_identity("c"),
        ordinal=2,
        inputs_digest=_digest("payload:c"),
        carry_digest=None,
        scheduling_intents=(),
    )
    assert beyond_source.ordinal == len(runner.ledger.entries), "the ledger stays contiguous"
    assert beyond_source.ordinal < plan.maximum_iterations, "the frozen bound is not reached"

    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(beyond_source)

    assert error.value.reason == "unknown_iteration"
    assert len(runner.ledger.entries) == 2


def test_a_settled_loop_launches_nothing_further() -> None:
    runner = LoopRunner(_sequential(), _elements("a"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)
    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o"),
    )

    assert runner.settled
    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(launch)
    assert error.value.reason == "loop_not_launching"


# --------------------------------------------------------------------------
# Crash and replay
# --------------------------------------------------------------------------


def _run_prefix(
    plan: FrozenLoopPlan, source: tuple[LoopElement, ...], settled: int, launched: int
) -> LoopLedger:
    """Return a ledger with ``settled`` succeeded entries then ``launched`` in flight."""
    runner = LoopRunner(plan, source)
    for index in range(settled):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"out-{index}"),
        )
    for _ in range(launched):
        runner.record_launch(runner.plan_launches()[0])
    return runner.ledger


def test_a_replay_preserves_every_recorded_identity_and_relaunches_nothing() -> None:
    plan = _sequential()
    source = _elements("a", "b", "c")
    ledger = _run_prefix(plan, source, settled=1, launched=1)

    resumed = replay_ledger(plan, source, ledger)

    assert resumed.ledger == ledger
    assert [entry.iteration_identity for entry in resumed.ledger.entries] == [
        plan.iteration_identity("a"),
        plan.iteration_identity("b"),
    ]
    assert resumed.state == LOOP_RUNNING
    assert len(resumed.in_flight) == 1
    # The one in flight is not relaunched, and neither is the settled one.
    assert resumed.plan_launches() == ()

    resumed.settle(
        plan.iteration_identity("b"),
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("out-b"),
    )
    assert [launch.ordinal for launch in resumed.plan_launches()] == [2]


def test_a_parallel_replay_refills_only_the_free_concurrency_headroom() -> None:
    plan = _parallel(maximum_concurrency=2)
    source = _elements("a", "b", "c", "d")
    ledger = _run_prefix(plan, source, settled=1, launched=1)

    resumed = replay_ledger(plan, source, ledger)

    assert len(resumed.in_flight) == 1
    assert [launch.ordinal for launch in resumed.plan_launches()] == [2]


def test_a_replay_refuses_a_hole_a_reordering_and_a_truncated_prefix() -> None:
    plan = _sequential()
    source = _elements("a", "b", "c")
    ledger = _run_prefix(plan, source, settled=3, launched=0)

    for broken, reason in (
        ((ledger.entries[0], ledger.entries[2]), "ledger_not_contiguous"),
        ((ledger.entries[1], ledger.entries[0], ledger.entries[2]), "ledger_not_contiguous"),
        ((ledger.entries[1], ledger.entries[2]), "ledger_not_contiguous"),
        ((ledger.entries[0], ledger.entries[1], ledger.entries[1]), "ledger_not_contiguous"),
    ):
        with pytest.raises(ExecutionRefused) as error:
            replay_ledger(plan, source, LoopLedger(broken))
        assert error.value.reason == reason


def test_a_replay_refuses_identity_and_inputs_drift_from_the_frozen_plan_and_source() -> (
    None
):
    plan = _sequential()
    source = _elements("a", "b")
    ledger = _run_prefix(plan, source, settled=1, launched=0)

    with pytest.raises(ExecutionRefused) as renamed:
        replay_ledger(_sequential(loop_stable_id="loop.renamed"), source, ledger)
    assert renamed.value.reason == "iteration_identity_drift"

    rekeyed = (LoopElement("z", source[0].payload_digest), source[1])
    with pytest.raises(ExecutionRefused) as identity:
        replay_ledger(plan, rekeyed, ledger)
    assert identity.value.reason == "iteration_identity_drift"

    repayloaded = (LoopElement("a", _digest("payload:changed")), source[1])
    with pytest.raises(ExecutionRefused) as inputs:
        replay_ledger(plan, repayloaded, ledger)
    assert inputs.value.reason == "iteration_identity_drift"


def test_a_replay_refuses_a_ledger_longer_than_the_bound_source_or_the_frozen_bound() -> (
    None
):
    plan = _sequential()
    source = _elements("a", "b", "c")
    ledger = _run_prefix(plan, source, settled=3, launched=0)

    with pytest.raises(ExecutionRefused) as shrunk:
        replay_ledger(plan, source[:2], ledger)
    assert shrunk.value.reason == "iteration_identity_drift"

    with pytest.raises(ExecutionRefused) as bound:
        replay_ledger(_sequential(maximum_iterations=2), source, ledger)
    assert bound.value.reason == "loop_exhausted"


def test_a_replay_refuses_two_iterations_settled_at_the_same_sequence() -> None:
    plan = _sequential()
    source = _elements("a", "b")
    ledger = _run_prefix(plan, source, settled=2, launched=0)
    collided = (ledger.entries[0], replace(ledger.entries[1], settlement_sequence=1))

    with pytest.raises(ExecutionRefused) as error:
        replay_ledger(plan, source, LoopLedger(collided))
    assert error.value.reason == "duplicate_settlement_sequence"


def test_a_replay_derives_the_next_launch_carry_from_the_ledger_result_carry() -> None:
    """No ambient caller: the carry the resumed runner uses comes out of the record."""
    plan = _sequential(carry_enabled=True)
    source = _elements("a", "b", "c")
    initial, produced = _digest("carry-0"), _digest("carry-1")

    runner = LoopRunner(plan, source, carry_digest=initial)
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("out-a"),
        resulting_carry_digest=produced,
    )
    ledger = runner.ledger

    resumed = replay_ledger(plan, source, ledger)

    assert resumed.carry_digest == produced
    next_launch = resumed.plan_launches()[0]
    assert next_launch.ordinal == 1
    assert next_launch.carry_digest == produced
    assert resumed.record_launch(next_launch).carry_digest == produced


def test_a_replay_refuses_a_caller_carry_that_disagrees_with_the_ledger() -> None:
    plan = _sequential(carry_enabled=True)
    source = _elements("a", "b")
    runner = LoopRunner(plan, source, carry_digest=_digest("carry-0"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as error:
        replay_ledger(
            plan, source, runner.ledger, carry_digest=_digest("carry-elsewhere")
        )
    assert error.value.reason == "carry_drift"

    # The agreeing carry replays, and so does no carry at all.
    assert replay_ledger(
        plan, source, runner.ledger, carry_digest=_digest("carry-0")
    ).carry_digest == _digest("carry-0")
    assert replay_ledger(plan, source, runner.ledger).carry_digest == _digest("carry-0")


def test_a_replay_refuses_a_launch_that_does_not_carry_what_the_ledger_folded() -> None:
    plan = _sequential(carry_enabled=True)
    source = _elements("a", "b")
    runner = LoopRunner(plan, source, carry_digest=_digest("carry-0"))
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("out-a"),
        resulting_carry_digest=_digest("carry-1"),
    )
    second = runner.plan_launches()[0]
    runner.record_launch(second)

    stale = (
        runner.ledger.entries[0],
        replace(runner.ledger.entries[1], carry_digest=_digest("carry-0")),
    )
    with pytest.raises(ExecutionRefused) as error:
        replay_ledger(plan, source, LoopLedger(stale))
    assert error.value.reason == "carry_drift"


def test_a_non_carrying_plan_refuses_a_ledger_or_a_caller_that_records_a_carry() -> (
    None
):
    plan = _sequential()
    source = _elements("a", "b")
    ledger = _run_prefix(plan, source, settled=0, launched=1)
    carried = (replace(ledger.entries[0], carry_digest=_digest("carry-0")),)

    with pytest.raises(ExecutionRefused) as recorded:
        replay_ledger(plan, source, LoopLedger(carried))
    assert recorded.value.reason == "carry_drift"

    with pytest.raises(ExecutionContractError) as supplied:
        LoopRunner(plan, source, carry_digest=_digest("carry-0"))
    assert supplied.value.reason == "invalid_loop_plan"


def test_a_new_carrying_loop_requires_its_frozen_initial_carry_digest() -> None:
    """Only a resume may omit it, because only a resume can fold it out of the record."""
    plan = _sequential(carry_enabled=True)
    source = _elements("a", "b")

    with pytest.raises(ExecutionContractError) as error:
        LoopRunner(plan, source)
    assert error.value.reason == "invalid_loop_plan"

    ledger = LoopRunner(plan, source, carry_digest=_digest("carry-0")).ledger
    assert ledger == LoopLedger()
    assert LoopRunner(
        plan, source, carry_digest=_digest("carry-0")
    ).carry_digest == _digest("carry-0")


def test_a_replay_of_a_failed_ledger_reapplies_the_partial_success_policy() -> None:
    plan = _sequential(partial_success_policy=FAIL_LOOP)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)
    runner.settle(
        launch.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="step_failed"
    )

    resumed = replay_ledger(plan, source, runner.ledger)

    assert resumed.state == LOOP_SETTLED
    assert resumed.failure_reason == "iteration_failed"
    assert resumed.plan_launches() == ()


# --------------------------------------------------------------------------
# Zip mismatch policies
# --------------------------------------------------------------------------


def test_zip_refuse_admits_equal_lengths_and_refuses_a_mismatch() -> None:
    plan = _sequential(zip_mismatch_policy=ZIP_REFUSE)
    primary = _elements("a", "b")
    secondary = _elements("x", "y")

    runner = LoopRunner(plan, primary, zip_sources=(secondary,))
    launch = runner.plan_launches()[0]
    assert launch.iteration_identity == plan.iteration_identity("a")
    assert launch.inputs_digest == canonical_hash(
        [primary[0].payload_digest, secondary[0].payload_digest]
    )

    with pytest.raises(ExecutionRefused) as error:
        LoopRunner(plan, primary, zip_sources=(_elements("x"),))
    assert error.value.reason == "zip_mismatch"


def test_zip_truncate_to_shortest_plans_only_the_common_prefix() -> None:
    plan = _sequential(zip_mismatch_policy=ZIP_TRUNCATE_TO_SHORTEST)
    primary = _elements("a", "b", "c")
    secondary = _elements("x", "y")

    runner = LoopRunner(plan, primary, zip_sources=(secondary,))
    identities = [
        runner.plan_launches()[0].iteration_identity,
    ]
    launched = _launch_all(runner)
    assert len(launched) == 1
    runner.settle(
        identities[0], outcome=OUTCOME_SUCCEEDED, outputs_digest=_digest("o0")
    )
    second = runner.plan_launches()[0]
    assert second.ordinal == 1
    runner.record_launch(second)
    runner.settle(
        second.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o1"),
    )

    assert len(runner.ledger.entries) == 2, "the third primary element is truncated away"
    assert runner.settled
    assert runner.failure_reason is None


def test_zip_pad_with_absent_plans_the_longest_and_marks_the_absent_position() -> None:
    plan = _sequential(zip_mismatch_policy=ZIP_PAD_WITH_ABSENT)
    primary = _elements("a")
    secondary = _elements("x", "y")

    runner = LoopRunner(plan, primary, zip_sources=(secondary,))
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )
    padded = runner.plan_launches()[0]

    assert padded.ordinal == 1
    assert padded.iteration_identity == plan.iteration_identity("pad-1")
    assert padded.inputs_digest == canonical_hash([None, secondary[1].payload_digest])


def test_a_padded_key_that_collides_with_a_real_element_key_is_refused() -> None:
    plan = _sequential(zip_mismatch_policy=ZIP_PAD_WITH_ABSENT)

    with pytest.raises(ExecutionRefused) as error:
        LoopRunner(plan, _elements("pad-1"), zip_sources=(_elements("x", "y"),))
    assert error.value.reason == "duplicate_iteration"


def test_a_duplicate_element_key_is_refused_before_a_single_iteration_is_planned() -> (
    None
):
    with pytest.raises(ExecutionRefused) as error:
        LoopRunner(_sequential(), _elements("a") + _elements("a"))
    assert error.value.reason == "duplicate_iteration"


def test_zip_sources_and_the_zip_policy_are_declared_together_or_not_at_all() -> None:
    with pytest.raises(ExecutionContractError) as orphan_policy:
        LoopRunner(_sequential(zip_mismatch_policy=ZIP_REFUSE), _elements("a"))
    assert orphan_policy.value.reason == "invalid_loop_plan"

    with pytest.raises(ExecutionContractError) as orphan_sources:
        LoopRunner(_sequential(), _elements("a"), zip_sources=(_elements("x"),))
    assert orphan_sources.value.reason == "invalid_loop_plan"


# --------------------------------------------------------------------------
# The frozen iteration bound
# --------------------------------------------------------------------------


def test_an_overflowing_source_settles_the_loop_failed_and_never_extends_the_bound() -> (
    None
):
    plan = _sequential(maximum_iterations=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)

    for index in range(2):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"o{index}"),
        )

    assert len(runner.ledger.entries) == 2
    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason == "maximum_iterations_exceeded"
    assert runner.plan_launches() == ()


def test_a_source_within_the_bound_settles_the_loop_without_a_failure() -> None:
    plan = _sequential(maximum_iterations=2)
    source = _elements("a", "b")
    runner = LoopRunner(plan, source)
    for index in range(2):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"o{index}"),
        )

    assert runner.settled
    assert runner.failure_reason is None


# --------------------------------------------------------------------------
# Cancellation policies
# --------------------------------------------------------------------------


def _cancellable(policy: str) -> tuple[LoopRunner, IterationLaunch, IterationLaunch]:
    """Return a parallel runner with two iterations in flight and one effect settled."""
    plan = _parallel(
        cancellation_policy=policy, maximum_concurrency=2, maximum_iterations=4
    )
    runner = LoopRunner(plan, _elements("a", "b", "c", "d"))
    first, second = runner.plan_launches()
    runner.record_launch(first)
    runner.record_launch(second)
    runner.record_effect_settlement(first.iteration_identity, "effect.request.1")
    return runner, first, second


def test_cancel_remaining_settles_every_in_flight_iteration_as_cancelled() -> None:
    runner, first, second = _cancellable(CANCEL_REMAINING)

    cancelled = runner.cancel()

    assert runner.cancellation_requested
    assert {entry.iteration_identity for entry in cancelled} == {
        first.iteration_identity,
        second.iteration_identity,
    }
    for entry in cancelled:
        assert entry.outcome == ITERATION_CANCELLED
        assert entry.cancellation_disposition == DISPOSITION_CANCELLED_IN_FLIGHT
        assert entry.settlement_sequence is not None and entry.settlement_sequence > 0
        assert entry.outputs_digest is None and entry.failure_reason is None
    # Cancellation is not rollback: the already-settled effect stays recorded.
    assert runner.ledger.entries[0].effect_settlements == ("effect.request.1",)
    assert runner.in_flight == ()
    assert runner.state == LOOP_SETTLED
    assert runner.plan_launches() == ()


def test_drain_in_flight_stops_launching_and_lets_the_in_flight_iterations_settle() -> (
    None
):
    runner, first, second = _cancellable(DRAIN_IN_FLIGHT)

    assert runner.cancel() == ()
    assert runner.state == LOOP_STOPPING
    assert runner.plan_launches() == ()
    assert len(runner.in_flight) == 2

    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )
    assert runner.state == LOOP_STOPPING
    runner.settle(
        second.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o1"),
    )

    assert runner.state == LOOP_SETTLED
    assert len(runner.ledger.entries) == 2, "no further iteration is launched after the request"
    assert runner.ledger.entries[0].effect_settlements == ("effect.request.1",)


def test_complete_all_records_the_request_and_lets_the_loop_finish() -> None:
    runner, first, second = _cancellable(COMPLETE_ALL)

    assert runner.cancel() == ()
    assert runner.cancellation_requested
    assert runner.state == LOOP_RUNNING

    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )
    runner.settle(
        second.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o1"),
    )
    remaining = _launch_all(runner)

    assert [entry.ordinal for entry in remaining] == [2, 3]
    for entry in remaining:
        runner.settle(
            entry.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"o{entry.ordinal}"),
        )
    assert len(runner.ledger.entries) == 4
    assert runner.state == LOOP_SETTLED
    assert runner.ledger.entries[0].effect_settlements == ("effect.request.1",)


def test_an_effect_settlement_is_recorded_once_and_only_while_in_flight() -> None:
    runner, first, _second = _cancellable(CANCEL_REMAINING)

    with pytest.raises(ExecutionRefused) as duplicate:
        runner.record_effect_settlement(first.iteration_identity, "effect.request.1")
    assert duplicate.value.reason == "duplicate_effect_settlement"

    with pytest.raises(ExecutionRefused) as unknown:
        runner.record_effect_settlement(_digest("nowhere"), "effect.request.2")
    assert unknown.value.reason == "unknown_iteration"

    runner.cancel()
    with pytest.raises(ExecutionRefused) as settled:
        runner.record_effect_settlement(first.iteration_identity, "effect.request.2")
    assert settled.value.reason == "iteration_already_settled"


# --------------------------------------------------------------------------
# Partial success policies
# --------------------------------------------------------------------------


def test_record_and_continue_keeps_launching_after_a_failed_iteration() -> None:
    plan = _sequential(partial_success_policy=RECORD_AND_CONTINUE)
    source = _elements("a", "b")
    runner = LoopRunner(plan, source)
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="step_failed"
    )

    assert runner.state == LOOP_RUNNING
    assert runner.failure_reason is None
    second = runner.plan_launches()[0]
    assert second.ordinal == 1
    runner.record_launch(second)
    runner.settle(
        second.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o1"),
    )
    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason is None


def test_record_and_stop_after_current_stops_launching_but_records_no_loop_failure() -> (
    None
):
    plan = _parallel(
        partial_success_policy=RECORD_AND_STOP_AFTER_CURRENT, maximum_concurrency=2
    )
    runner = LoopRunner(plan, _elements("a", "b", "c"))
    first, second = runner.plan_launches()
    runner.record_launch(first)
    runner.record_launch(second)

    runner.settle(
        first.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="step_failed"
    )

    assert runner.state == LOOP_STOPPING
    assert runner.failure_reason is None
    assert runner.plan_launches() == ()

    runner.settle(
        second.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o1"),
    )
    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason is None
    assert len(runner.ledger.entries) == 2


def test_fail_loop_stops_launching_and_records_the_loop_failure() -> None:
    plan = _sequential(partial_success_policy=FAIL_LOOP)
    runner = LoopRunner(plan, _elements("a", "b", "c"))
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="step_failed"
    )

    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason == "iteration_failed"
    assert runner.plan_launches() == ()
    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(first)
    assert error.value.reason == "loop_not_launching"


def test_a_skipped_iteration_settles_without_outputs_failure_or_carry() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    entry = runner.settle(launch.iteration_identity, outcome=ITERATION_SKIPPED)

    assert entry.outcome == ITERATION_SKIPPED
    assert entry.settlement_sequence == 1
    assert entry.outputs_digest is None
    assert entry.failure_reason is None
    assert entry.cancellation_disposition is None
    assert entry.resulting_carry_digest is None
    assert runner.gather() == ()


def test_gather_returns_the_succeeded_outputs_in_the_declared_order() -> None:
    identity_ordered = LoopRunner(
        _parallel(order_guarantee=ORDER_ITERATION_IDENTITY, maximum_concurrency=2),
        _elements("a", "b"),
    )
    settlement_ordered = LoopRunner(
        _parallel(order_guarantee=ORDER_SETTLEMENT, maximum_concurrency=2),
        _elements("a", "b"),
    )
    for runner in (identity_ordered, settlement_ordered):
        first, second = runner.plan_launches()
        runner.record_launch(first)
        runner.record_launch(second)
        # Settled out of ordinal order, on purpose.
        runner.settle(
            second.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o1"),
        )
        runner.settle(
            first.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o0"),
        )

    assert identity_ordered.gather() == (_digest("o0"), _digest("o1"))
    assert settlement_ordered.gather() == (_digest("o1"), _digest("o0"))


def test_settle_refuses_an_unknown_a_repeated_and_an_uncarryable_settlement() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as unknown:
        runner.settle(
            _digest("nowhere"), outcome=OUTCOME_SUCCEEDED, outputs_digest=_digest("o")
        )
    assert unknown.value.reason == "unknown_iteration"

    with pytest.raises(ExecutionContractError) as vocabulary:
        runner.settle(launch.iteration_identity, outcome="DONE")
    assert vocabulary.value.reason == "unknown_vocabulary_member"

    with pytest.raises(ExecutionContractError) as carry:
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o"),
            resulting_carry_digest=_digest("carry"),
        )
    assert carry.value.reason == "invalid_settlement"

    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o"),
    )
    with pytest.raises(ExecutionRefused) as repeated:
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o"),
        )
    assert repeated.value.reason == "iteration_already_settled"


def test_a_refused_settlement_consumes_no_settlement_sequence() -> None:
    runner = LoopRunner(_parallel(maximum_concurrency=2), _elements("a", "b"))
    first, second = runner.plan_launches()
    runner.record_launch(first)
    runner.record_launch(second)

    with pytest.raises(ExecutionContractError):
        runner.settle(first.iteration_identity, outcome=OUTCOME_SUCCEEDED)

    assert (
        runner.settle(
            first.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o0"),
        ).settlement_sequence
        == 1
    )
    assert (
        runner.settle(
            second.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest("o1"),
        ).settlement_sequence
        == 2
    )


# --------------------------------------------------------------------------
# IterationLedgerEntry: complete negative validation
# --------------------------------------------------------------------------

_GOOD = _digest("good")
_ENTRY: dict[str, object] = {
    "iteration_identity": _GOOD,
    "ordinal": 0,
    "inputs_digest": _GOOD,
}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        # Shape.
        ({"iteration_identity": "iteration-1"}, "invalid_digest"),
        ({"inputs_digest": "inputs"}, "invalid_digest"),
        ({"ordinal": -1}, "invalid_ordinal"),
        ({"carry_digest": "carry"}, "invalid_digest"),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": "outputs",
                "settlement_sequence": 1,
            },
            "invalid_digest",
        ),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": _GOOD,
                "resulting_carry_digest": "carry",
                "settlement_sequence": 1,
            },
            "invalid_digest",
        ),
        (
            {
                "outcome": OUTCOME_FAILED,
                "failure_reason": "Step Failed",
                "settlement_sequence": 1,
            },
            "invalid_identifier",
        ),
        ({"outcome": "DONE"}, "unknown_vocabulary_member"),
        ({"scheduling_intents": ("lease.a", "lease.a")}, "duplicate_entry"),
        ({"effect_settlements": ("effect.a", "effect.a")}, "duplicate_entry"),
        ({"effect_settlements": ("EFFECT",)}, "invalid_identifier"),
        # An unsettled entry records no settlement of any kind.
        ({"outputs_digest": _GOOD}, "invalid_settlement"),
        ({"failure_reason": "step_failed"}, "invalid_settlement"),
        ({"settlement_sequence": 1}, "invalid_settlement"),
        (
            {"cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT},
            "invalid_settlement",
        ),
        ({"resulting_carry_digest": _GOOD}, "invalid_settlement"),
        # A settled entry records a positive settlement sequence.
        ({"outcome": OUTCOME_SUCCEEDED, "outputs_digest": _GOOD}, "invalid_settlement"),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": _GOOD,
                "settlement_sequence": 0,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": _GOOD,
                "settlement_sequence": -1,
            },
            "invalid_settlement",
        ),
        # Outcome / output / failure coherence.
        (
            {"outcome": OUTCOME_SUCCEEDED, "settlement_sequence": 1},
            "invalid_settlement",
        ),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": _GOOD,
                "failure_reason": "step_failed",
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        ({"outcome": OUTCOME_FAILED, "settlement_sequence": 1}, "invalid_settlement"),
        (
            {
                "outcome": OUTCOME_FAILED,
                "failure_reason": "step_failed",
                "outputs_digest": _GOOD,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": ITERATION_CANCELLED,
                "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
                "outputs_digest": _GOOD,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": ITERATION_SKIPPED,
                "failure_reason": "step_failed",
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        # Cancellation disposition coherence.
        (
            {"outcome": ITERATION_CANCELLED, "settlement_sequence": 1},
            "invalid_settlement",
        ),
        (
            {
                "outcome": OUTCOME_SUCCEEDED,
                "outputs_digest": _GOOD,
                "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": ITERATION_CANCELLED,
                "cancellation_disposition": "ROLLED_BACK",
                "settlement_sequence": 1,
            },
            "unknown_vocabulary_member",
        ),
        # Only a succeeded iteration produces a carry.
        (
            {
                "outcome": OUTCOME_FAILED,
                "failure_reason": "step_failed",
                "resulting_carry_digest": _GOOD,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": ITERATION_CANCELLED,
                "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
                "resulting_carry_digest": _GOOD,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
        (
            {
                "outcome": ITERATION_SKIPPED,
                "resulting_carry_digest": _GOOD,
                "settlement_sequence": 1,
            },
            "invalid_settlement",
        ),
    ],
)
def test_an_iteration_ledger_entry_refuses_every_incoherent_combination(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as error:
        IterationLedgerEntry(**{**_ENTRY, **overrides})  # type: ignore[arg-type]
    assert error.value.reason == reason


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"carry_digest": _GOOD, "scheduling_intents": ("lease.a",)},
        {
            "outcome": OUTCOME_SUCCEEDED,
            "outputs_digest": _GOOD,
            "settlement_sequence": 1,
        },
        {
            "outcome": OUTCOME_SUCCEEDED,
            "outputs_digest": _GOOD,
            "resulting_carry_digest": _GOOD,
            "settlement_sequence": 3,
        },
        {
            "outcome": OUTCOME_FAILED,
            "failure_reason": "step_failed",
            "settlement_sequence": 2,
        },
        {
            "outcome": ITERATION_CANCELLED,
            "cancellation_disposition": DISPOSITION_CANCELLED_IN_FLIGHT,
            "settlement_sequence": 4,
            "effect_settlements": ("effect.a", "effect.b"),
        },
        {"outcome": ITERATION_SKIPPED, "settlement_sequence": 5},
    ],
)
def test_an_iteration_ledger_entry_admits_every_coherent_combination(
    overrides: dict[str, object],
) -> None:
    entry = IterationLedgerEntry(**{**_ENTRY, **overrides})  # type: ignore[arg-type]
    assert entry.is_settled == (overrides.get("outcome") is not None)
    assert entry.scheduling_intents_digest == canonical_hash(
        list(entry.scheduling_intents)
    )


# --------------------------------------------------------------------------
# Late results: Evidence, and nothing else
# --------------------------------------------------------------------------


def _settled_runner() -> tuple[LoopRunner, str]:
    runner = LoopRunner(_sequential(), _elements("a"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)
    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o"),
    )
    return runner, launch.iteration_identity


def test_a_late_result_is_evidence_only_and_leaves_the_ledger_unchanged() -> None:
    runner, identity = _settled_runner()
    before = runner.ledger

    late = runner.record_late_result(identity, evidence_ref="evidence.late.1")

    assert late == LateResult(identity, "evidence.late.1", applied_to_run_state=False)
    assert late.applied_to_run_state is False
    assert runner.late_results == (late,)
    assert runner.ledger == before, "the ledger is not touched by a late result"
    assert runner.ledger.entries[0].outputs_digest == _digest("o")
    assert runner.gather() == (_digest("o"),)


def test_a_late_result_is_refused_for_an_unknown_iteration_and_a_second_time() -> None:
    runner, identity = _settled_runner()
    runner.record_late_result(identity, evidence_ref="evidence.late.1")

    with pytest.raises(ExecutionRefused) as duplicate:
        runner.record_late_result(identity, evidence_ref="evidence.late.2")
    assert duplicate.value.reason == "duplicate_late_result"

    with pytest.raises(ExecutionRefused) as unknown:
        runner.record_late_result(_digest("nowhere"), evidence_ref="evidence.late.3")
    assert unknown.value.reason == "unknown_iteration"
    assert len(runner.late_results) == 1


def test_a_result_before_the_loop_settles_is_not_late() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as error:
        runner.record_late_result(
            launch.iteration_identity, evidence_ref="evidence.late.1"
        )
    assert error.value.reason == "loop_not_settled"


def test_a_late_result_can_never_be_declared_applied_to_run_state() -> None:
    with pytest.raises(ExecutionContractError) as error:
        LateResult(_digest("i"), "evidence.late.1", applied_to_run_state=True)
    assert error.value.reason == "late_result_applied"

    with pytest.raises(ExecutionContractError) as shape:
        LateResult("iteration-1", "evidence.late.1")
    assert shape.value.reason == "invalid_digest"


def test_a_replayed_runner_admits_the_late_results_it_is_constructed_with() -> None:
    plan = _sequential()
    source = _elements("a")
    ledger = _run_prefix(plan, source, settled=1, launched=0)
    late = LateResult(plan.iteration_identity("a"), "evidence.late.1")

    resumed = replay_ledger(plan, source, ledger, late_results=(late,))

    assert resumed.settled
    assert resumed.late_results == (late,)
    assert resumed.ledger == ledger


# --------------------------------------------------------------------------
# T-0691: cancellation survives the crash, and truncation fails closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [CANCEL_REMAINING, DRAIN_IN_FLIGHT])
def test_a_replayed_cancellation_plans_no_launch_the_live_runner_stopped(
    policy: str,
) -> None:
    """The defect this pins: cancellation used to live only in the process that crashed.

    A cancelled loop replayed from its own ledger came back `RUNNING`, uncancelled, and
    planned exactly the launches the cancellation had stopped -- against a Run whose
    ledger is its only recovery record. Both runners are compared whole, not sampled:
    the state, the posture, the terminal outcome and every recorded byte.
    """
    plan = _sequential(cancellation_policy=policy, maximum_iterations=5)
    source = _elements("a", "b", "c", "d", "e")
    runner = LoopRunner(plan, source)
    runner.record_launch(runner.plan_launches()[0])
    runner.cancel()

    resumed = replay_ledger(plan, source, runner.ledger)

    assert resumed.ledger == runner.ledger
    assert resumed.ledger.cancellation_requested is True
    assert resumed.cancellation_requested is True
    assert resumed.state == runner.state
    assert resumed.outcome == runner.outcome
    assert resumed.plan_launches() == ()
    assert runner.plan_launches() == ()


def test_a_cancellation_interrupted_mid_sweep_completes_on_replay() -> None:
    """A ledger written between the request and the sweep resumes swept, not running."""
    plan = _parallel(cancellation_policy=CANCEL_REMAINING, maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    interrupted = LoopLedger(runner.ledger.entries, cancellation_requested=True)

    resumed = replay_ledger(plan, source, interrupted)

    assert [entry.outcome for entry in resumed.ledger.entries] == [
        ITERATION_CANCELLED,
        ITERATION_CANCELLED,
    ]
    assert [entry.settlement_sequence for entry in resumed.ledger.entries] == [1, 2]
    assert resumed.state == LOOP_SETTLED
    assert resumed.outcome == LOOP_OUTCOME_CANCELLED
    assert resumed.plan_launches() == ()


def test_a_ledger_recording_cancelled_entries_under_no_cancellation_is_refused() -> None:
    """The posture is a fact the entries have to support, in both directions.

    :meth:`LoopRunner.settle` refuses `CANCELLED` so nothing but `cancel()` can write a
    cancelled entry, which is what makes `cancellation_requested` reconstructible. The
    other half is here: a record whose entries were swept but whose posture says nothing
    was cancelled is the same fail-open as a cancellation that never survived the crash,
    and believing it resumes the loop `RUNNING` and plans the launch the sweep stopped.
    The Core `LoopIterationLedger` refuses the same shape, so the durable record and this
    replay agree on what a cancellation looks like.
    """
    plan = _parallel(cancellation_policy=CANCEL_REMAINING, maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    runner.cancel()
    assert runner.state == LOOP_SETTLED and runner.plan_launches() == ()

    erased = LoopLedger(runner.ledger.entries, cancellation_requested=False)
    with pytest.raises(ExecutionRefused) as error:
        replay_ledger(plan, source, erased)

    assert error.value.reason == "cancellation_not_recorded"
    # The record as it actually stands still replays to the same settled, swept loop.
    resumed = replay_ledger(plan, source, runner.ledger)
    assert resumed.ledger == runner.ledger
    assert resumed.plan_launches() == ()


def test_an_iteration_is_cancelled_by_the_policy_and_never_settled_cancelled() -> None:
    """Otherwise a cancelled entry could stand in a ledger that records no cancellation.

    That is the combination the durable posture has to be able to rule out: it is what
    makes `cancellation_requested` a fact the record supports rather than a claim.
    """
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)

    with pytest.raises(ExecutionRefused) as error:
        runner.settle(launch.iteration_identity, outcome=ITERATION_CANCELLED)

    assert error.value.reason == "cancellation_not_a_settlement"
    assert runner.ledger.entries[0].outcome is None
    assert runner.ledger.cancellation_requested is False
    # The refusal consumed nothing: the next real settlement is still sequence one.
    settled = runner.settle(launch.iteration_identity, outcome=ITERATION_SKIPPED)
    assert settled.settlement_sequence == 1


@pytest.mark.parametrize(
    "policy", [CANCEL_REMAINING, DRAIN_IN_FLIGHT, COMPLETE_ALL]
)
def test_overflow_fails_the_loop_whatever_the_cancellation_policy_did(
    policy: str,
) -> None:
    """The second defect: a cancellation used to clear the overflow that truncated a loop.

    A five-element source under a bound of three settled with no failure reason at all
    once `cancel()` had been called -- and replaying that same ledger reported
    `maximum_iterations_exceeded`, so the live runner and its own record disagreed about
    whether the Run failed. The bound is a property of the plan, not of what the caller
    did afterwards.
    """
    plan = _sequential(cancellation_policy=policy, maximum_iterations=3)
    source = _elements("a", "b", "c", "d", "e")
    runner = LoopRunner(plan, source)
    # The cancellation lands where the loop still reaches its bound: before the first
    # launch for a policy that lets the loop finish, and on the last one for the two
    # that stop it. Either way the source outran the bound, and that is the finding.
    if policy == COMPLETE_ALL:
        runner.cancel()
    for index in range(plan.maximum_iterations):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        if policy != COMPLETE_ALL and index == plan.maximum_iterations - 1:
            runner.cancel()
        if not runner.ledger.entries[launch.ordinal].is_settled:
            runner.settle(
                launch.iteration_identity,
                outcome=OUTCOME_SUCCEEDED,
                outputs_digest=_digest(f"o{launch.ordinal}"),
            )

    resumed = replay_ledger(plan, source, runner.ledger)

    for settled in (runner, resumed):
        assert settled.state == LOOP_SETTLED
        assert settled.failure_reason == "maximum_iterations_exceeded"
        assert settled.outcome == LOOP_OUTCOME_FAILED
        with pytest.raises(ExecutionRefused) as refused:
            settled.gather()
        assert refused.value.reason == "loop_failed"
    assert resumed.ledger == runner.ledger


def test_a_cancelled_loop_within_its_bound_is_cancelled_and_not_failed() -> None:
    """Cancellation is a declared policy outcome, so its partial gather is the answer."""
    plan = _sequential(cancellation_policy=DRAIN_IN_FLIGHT, maximum_iterations=5)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    launch = runner.plan_launches()[0]
    runner.record_launch(launch)
    runner.cancel()
    runner.settle(
        launch.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )

    assert runner.state == LOOP_SETTLED
    assert runner.failure_reason is None
    assert runner.outcome == LOOP_OUTCOME_CANCELLED
    assert runner.gather() == (_digest("o0"),)


def test_a_complete_run_within_its_bound_settles_succeeded() -> None:
    runner = LoopRunner(_sequential(maximum_iterations=2), _elements("a", "b"))
    for index in range(2):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        runner.settle(
            launch.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"o{index}"),
        )

    assert runner.outcome == LOOP_OUTCOME_SUCCEEDED
    assert runner.failure_reason is None
    assert runner.gather() == (_digest("o0"), _digest("o1"))


def test_a_running_loop_has_no_terminal_outcome_yet() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    runner.record_launch(runner.plan_launches()[0])
    assert runner.outcome is None


def test_the_maximum_iterations_guard_is_reachable_on_its_own() -> None:
    """The bound refusal, with no other guard that could have produced it.

    An in-flight parallel iteration keeps the loop `RUNNING` past the point where the
    bound is reached, and the concurrency headroom and the ledger's contiguity are both
    satisfied at the refused ordinal -- so `loop_exhausted` is the only guard left, and
    the assertion names it exactly rather than accepting either of two diagnostics.
    """
    plan = _parallel(maximum_iterations=2, maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    first = runner.plan_launches()[0]
    runner.record_launch(first)
    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("o0"),
    )
    runner.record_launch(runner.plan_launches()[0])

    assert runner.state == LOOP_RUNNING
    assert len(runner.in_flight) == 1 < plan.maximum_concurrency
    beyond = IterationLaunch(
        iteration_identity=plan.iteration_identity("c"),
        ordinal=2,
        inputs_digest=source[2].payload_digest,
        carry_digest=None,
        scheduling_intents=(),
    )
    assert beyond.ordinal == len(runner.ledger.entries), "the ledger stays contiguous"

    with pytest.raises(ExecutionRefused) as error:
        runner.record_launch(beyond)

    assert error.value.reason == "loop_exhausted"
    assert len(runner.ledger.entries) == 2


def test_a_replayed_settlement_order_must_be_one_a_live_runner_could_produce() -> None:
    """Settlement order is the gather order, so a forged one reorders real results."""
    plan = _parallel(order_guarantee=ORDER_SETTLEMENT, maximum_concurrency=3)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    for entry in runner.ledger.entries:
        runner.settle(
            entry.iteration_identity,
            outcome=OUTCOME_SUCCEEDED,
            outputs_digest=_digest(f"o{entry.ordinal}"),
        )
    entries = runner.ledger.entries

    for broken in (
        (entries[0], entries[1], replace(entries[2], settlement_sequence=5)),
        (entries[0], entries[1], replace(entries[2], settlement_sequence=9)),
    ):
        with pytest.raises(ExecutionRefused) as error:
            replay_ledger(plan, source, LoopLedger(broken))
        assert error.value.reason == "settlement_not_contiguous"

    # A settlement order that is not the launch order is not a broken one.
    reordered = (
        replace(entries[0], settlement_sequence=3),
        replace(entries[1], settlement_sequence=1),
        replace(entries[2], settlement_sequence=2),
    )
    resumed = replay_ledger(plan, source, LoopLedger(reordered))
    assert resumed.gather() == (_digest("o1"), _digest("o2"), _digest("o0"))


def test_a_replayed_ledger_settling_past_the_cancellation_sweep_is_refused() -> None:
    """`CANCEL_REMAINING` sweeps once and launches nothing after it, so nothing follows."""
    plan = _parallel(cancellation_policy=CANCEL_REMAINING, maximum_concurrency=2)
    source = _elements("a", "b", "c")
    runner = LoopRunner(plan, source)
    for launch in runner.plan_launches():
        runner.record_launch(launch)
    runner.cancel()
    swept = runner.ledger.entries

    impossible = LoopLedger(
        (
            swept[0],
            replace(
                swept[1],
                outcome=OUTCOME_SUCCEEDED,
                outputs_digest=_digest("o1"),
                cancellation_disposition=None,
            ),
        ),
        cancellation_requested=True,
    )
    with pytest.raises(ExecutionRefused) as error:
        replay_ledger(plan, source, impossible)

    assert error.value.reason == "settlement_after_stop"
    # The sweep as it actually happened replays without complaint.
    assert replay_ledger(plan, source, runner.ledger).ledger == runner.ledger


def test_a_truncated_or_partially_failed_loop_is_incomplete_and_not_succeeded() -> None:
    """Neither loop failed, and neither ran clean: `SUCCEEDED` is the narrow claim.

    `RECORD_AND_STOP_AFTER_CURRENT` stops on a failed iteration without failing the
    loop, and `RECORD_AND_CONTINUE` finishes with one recorded. Both declared policies
    are preserved exactly -- no failure reason appears -- but a caller reading only
    `failure_reason is None` would take a prefix of the answer for the answer.
    """
    stopped = LoopRunner(
        _sequential(partial_success_policy=RECORD_AND_STOP_AFTER_CURRENT),
        _elements("a", "b", "c"),
    )
    launch = stopped.plan_launches()[0]
    stopped.record_launch(launch)
    stopped.settle(
        launch.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="step_failed"
    )

    assert stopped.state == LOOP_SETTLED
    assert stopped.failure_reason is None
    assert stopped.outcome == LOOP_OUTCOME_INCOMPLETE
    assert len(stopped.ledger.entries) == 1
    # One of three elements ran, and the one that ran failed. Nothing here is an answer.
    with pytest.raises(ExecutionRefused) as truncated:
        stopped.gather()
    assert truncated.value.reason == "loop_incomplete"
    assert stopped.gather(partial=True) == ()

    recorded = LoopRunner(
        _sequential(partial_success_policy=RECORD_AND_CONTINUE), _elements("a", "b")
    )
    for index in range(2):
        entry = recorded.plan_launches()[0]
        recorded.record_launch(entry)
        if index == 0:
            recorded.settle(
                entry.iteration_identity,
                outcome=OUTCOME_FAILED,
                failure_reason="step_failed",
            )
        else:
            recorded.settle(
                entry.iteration_identity,
                outcome=OUTCOME_SUCCEEDED,
                outputs_digest=_digest("o1"),
            )

    assert recorded.failure_reason is None
    assert recorded.outcome == LOOP_OUTCOME_INCOMPLETE
    # The declared partial is still consumable; what changed is that the caller has to
    # say a partial is what it is taking. An ordinary gather would have handed one
    # succeeded output of two elements back as though it were the loop's result.
    with pytest.raises(ExecutionRefused) as partial:
        recorded.gather()
    assert partial.value.reason == "loop_incomplete"
    assert recorded.gather(partial=True) == (_digest("o1"),)


def test_a_skipped_iteration_is_a_settlement_and_not_an_incompletion() -> None:
    runner = LoopRunner(_sequential(), _elements("a", "b"))
    for _ in range(2):
        launch = runner.plan_launches()[0]
        runner.record_launch(launch)
        runner.settle(launch.iteration_identity, outcome=ITERATION_SKIPPED)

    assert runner.outcome == LOOP_OUTCOME_SUCCEEDED
    assert runner.gather() == ()


def test_the_terminal_outcome_is_one_of_the_closed_vocabulary() -> None:
    assert loop_module.LOOP_OUTCOMES == {
        LOOP_OUTCOME_SUCCEEDED,
        LOOP_OUTCOME_FAILED,
        LOOP_OUTCOME_CANCELLED,
        LOOP_OUTCOME_INCOMPLETE,
    }
