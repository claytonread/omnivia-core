"""T-0688 acceptance for the durable loop record and its two conversions.

Clean-room: every value below is authored here, and the seam under test is a pure
in-memory oracle, so every assertion is made against its own returned values and
refusals.

The property this whole file is about: *a loop's recovery record survives a process
boundary whole.* Handing a runner's own object graph back to a "restore" proves nothing
-- the objects were never serialized, so every member survives trivially, including the
ones no serializer would have written. So the round trips here go through RFC 8785
canonical bytes, and the resume that follows is given the ledger and nothing else: no
ambient carry, no ambient late result, no ambient scheduling intent, no ambient effect
settlement.

Two conversions, one record:

- :func:`loop_ledger_document` / :func:`loop_ledger_from_document` are the durable form,
  and the round trip is asserted on the bytes rather than on the objects;
- :func:`to_loop_iteration_ledger` / :func:`from_loop_iteration_ledger` are the seam to
  the Core `LoopIterationLedger`, and the record produced is asserted against Core's own
  validator rather than against this file's idea of it.

And two histories a live runner could not have written, which a replay now refuses
rather than resuming from: a cancelled iteration under a policy that never cancels one,
and an in-flight population past the frozen concurrency bound.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any

import pytest
from omnivia_core_runtime.execution.loop import (
    CANCEL_REMAINING,
    COMPLETE_ALL,
    DISPOSITION_CANCELLED_IN_FLIGHT,
    DRAIN_IN_FLIGHT,
    ITERATION_CANCELLED,
    LOOP_LEDGER_SCHEMA_VERSION,
    LOOP_MODE_PARALLEL,
    LOOP_MODE_SEQUENTIAL,
    ORDER_ITERATION_IDENTITY,
    RECORD_AND_CONTINUE,
    SETTLEMENT_COMMITTED,
    SETTLEMENT_UNKNOWN,
    EffectSettlement,
    FrozenLoopPlan,
    IterationLedgerEntry,
    IterationWitness,
    LateResult,
    LoopElement,
    LoopLedger,
    LoopRunner,
    from_loop_iteration_ledger,
    loop_ledger_document,
    loop_ledger_from_document,
    replay_ledger,
    to_loop_iteration_ledger,
)
from omnivia_core_runtime.execution.profile import (
    ExecutionContractError,
    ExecutionRefused,
)
from omnivia_core_runtime.execution.workflow import OUTCOME_FAILED, OUTCOME_SUCCEEDED

from omnivia_core.contracts.v1.canonical_json import canonical_bytes
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.semantics_workflow import validate_loop_iteration_ledger

_LAUNCHED = "2026-09-02T09:00:00Z"
_SETTLED = "2026-09-02T09:00:01Z"
_LOOP_SETTLED = "2026-09-02T09:00:09Z"


def _digest(seed: str) -> str:
    return f"sha256:{sha256(seed.encode('utf-8')).hexdigest()}"


def _plan(**overrides: Any) -> FrozenLoopPlan:
    fields: dict[str, Any] = {
        "loop_stable_id": "loop.berth.sweep",
        "mode": LOOP_MODE_SEQUENTIAL,
        "order_guarantee": ORDER_ITERATION_IDENTITY,
        "maximum_iterations": 4,
        "cancellation_policy": COMPLETE_ALL,
        "partial_success_policy": RECORD_AND_CONTINUE,
    }
    fields.update(overrides)
    return FrozenLoopPlan(**fields)


def _source(*keys: str) -> tuple[LoopElement, ...]:
    return tuple(LoopElement(key, _digest(f"payload.{key}")) for key in keys)


def _loaded_runner() -> tuple[FrozenLoopPlan, tuple[LoopElement, ...], LoopRunner]:
    """A settled carrying loop whose record uses every member the seam has.

    Carry, both settled outcome classes, scheduling intents on the launch, two structured
    effect settlements in two different classes, and one late result. A round trip that
    only exercises the members a minimal loop happens to fill proves nothing about the
    ones it does not.
    """
    plan = _plan(carry_enabled=True)
    source = _source("alpha", "beta")
    runner = LoopRunner(plan, source, carry_digest=_digest("carry.0"))

    first = runner.record_launch(
        runner.plan_launches(scheduling_intents=("intent.warm", "intent.stage"))[0]
    )
    runner.record_effect_settlement(
        first.iteration_identity,
        EffectSettlement(
            "effect.notify",
            SETTLEMENT_COMMITTED,
            "contribution.notify",
            "receipt.notify",
        ),
    )
    runner.record_effect_settlement(
        first.iteration_identity,
        EffectSettlement("effect.invoice", SETTLEMENT_UNKNOWN, "contribution.invoice"),
    )
    runner.settle(
        first.iteration_identity,
        outcome=OUTCOME_SUCCEEDED,
        outputs_digest=_digest("outputs.alpha"),
        resulting_carry_digest=_digest("carry.1"),
    )

    second = runner.record_launch(
        runner.plan_launches(scheduling_intents=("intent.drain",))[0]
    )
    runner.settle(
        second.iteration_identity, outcome=OUTCOME_FAILED, failure_reason="berth.closed"
    )
    assert runner.settled
    runner.record_late_result(first.iteration_identity, evidence_ref="evidence.late.one")
    return plan, source, runner


def _witnesses(ledger: LoopLedger) -> dict[str, IterationWitness]:
    return {
        entry.iteration_identity: IterationWitness(
            launch_bundle_ref=f"bundle.{entry.ordinal}",
            launched_at=_LAUNCHED,
            settled_at=_SETTLED,
        )
        for entry in ledger.entries
    }


# --- 1. the durable document --------------------------------------------------


def test_the_whole_record_survives_a_canonical_json_round_trip() -> None:
    _plan_unused, _source_unused, runner = _loaded_runner()
    ledger = runner.ledger

    document = loop_ledger_document(ledger)
    restored = loop_ledger_from_document(document)

    assert restored == ledger
    # Byte-for-byte, not merely equal-looking: a serializer that dropped a member would
    # still restore an object that compared equal to the one it dropped it from.
    assert canonical_bytes(loop_ledger_document(restored)) == canonical_bytes(document)

    entry = restored.entries[0]
    assert entry.scheduling_intents == ("intent.warm", "intent.stage")
    assert entry.effect_settlements == (
        EffectSettlement(
            "effect.notify",
            SETTLEMENT_COMMITTED,
            "contribution.notify",
            "receipt.notify",
        ),
        EffectSettlement("effect.invoice", SETTLEMENT_UNKNOWN, "contribution.invoice"),
    )
    assert entry.carry_digest == _digest("carry.0")
    assert entry.resulting_carry_digest == _digest("carry.1")
    assert restored.late_results == (
        LateResult(entry.iteration_identity, "evidence.late.one"),
    )


def test_a_resume_reads_the_restored_document_and_nothing_else() -> None:
    """The regression for the ambient side channel.

    The runner is handed the plan, the source and the restored ledger -- no carry, no
    late result, no scheduling intent, no effect settlement. Every one of those was a
    separate thing a caller could forget to carry across the crash.
    """
    plan, source, runner = _loaded_runner()
    restored = loop_ledger_from_document(loop_ledger_document(runner.ledger))

    resumed = replay_ledger(plan, source, restored)

    assert resumed.ledger == runner.ledger
    assert resumed.carry_digest == _digest("carry.1")
    assert resumed.late_results == runner.late_results
    assert resumed.settled and resumed.outcome == runner.outcome
    # And the recorded late result is not admitted a second time as if it were new.
    with pytest.raises(ExecutionRefused, match="duplicate_late_result"):
        resumed.record_late_result(
            runner.ledger.entries[0].iteration_identity, evidence_ref="evidence.late.one"
        )


def test_a_cancelled_posture_survives_the_round_trip_and_launches_nothing() -> None:
    plan = _plan(cancellation_policy=CANCEL_REMAINING)
    source = _source("alpha", "beta", "gamma")
    runner = LoopRunner(plan, source)
    runner.record_launch(runner.plan_launches()[0])
    cancelled = runner.cancel()
    assert [entry.outcome for entry in cancelled] == [ITERATION_CANCELLED]

    restored = loop_ledger_from_document(loop_ledger_document(runner.ledger))
    resumed = replay_ledger(plan, source, restored)

    assert restored.cancellation_requested is True
    assert resumed.cancellation_requested is True
    assert resumed.plan_launches() == ()
    assert resumed.ledger == runner.ledger
    assert (
        resumed.ledger.entries[0].cancellation_disposition
        == DISPOSITION_CANCELLED_IN_FLIGHT
    )


def test_the_document_is_a_closed_shape_read_on_no_guess() -> None:
    document = loop_ledger_document(LoopLedger())

    with pytest.raises(ExecutionContractError, match="unsupported_schema_version"):
        loop_ledger_from_document({**document, "ledgerSchemaVersion": "2.0.0"})
    with pytest.raises(ExecutionContractError, match="invalid_document"):
        loop_ledger_from_document({**document, "replayHint": "reuse"})
    with pytest.raises(ExecutionContractError, match="invalid_document"):
        loop_ledger_from_document(
            {key: value for key, value in document.items() if key != "lateResults"}
        )
    with pytest.raises(ExecutionContractError, match="invalid_document"):
        loop_ledger_from_document({**document, "cancellationRequested": "yes"})
    with pytest.raises(ExecutionContractError, match="invalid_document"):
        loop_ledger_from_document([document])
    assert loop_ledger_from_document(document) == LoopLedger()
    assert document["ledgerSchemaVersion"] == LOOP_LEDGER_SCHEMA_VERSION


def test_a_restored_entry_is_held_to_the_rules_a_produced_one_is() -> None:
    """Restoring is not a way around the entry's own coherence rules.

    A document is bytes somebody wrote, so a member that would have been refused on the
    way out has to be refused on the way in -- otherwise a hand-edited file is a way to
    put an incoherent settlement into a ledger a live runner would never have produced.
    """
    _plan_unused, _source_unused, runner = _loaded_runner()
    document = loop_ledger_document(runner.ledger)

    incoherent = {**document["entries"][0], "outputsDigest": None}  # type: ignore[index]
    with pytest.raises(ExecutionContractError, match="invalid_settlement"):
        loop_ledger_from_document({**document, "entries": [incoherent]})

    unclassified = {
        **document["entries"][0],  # type: ignore[index]
        "effectSettlements": [
            {
                "effectRequestId": "effect.notify",
                "settlementClass": SETTLEMENT_UNKNOWN,
                "completionContribution": "contribution.notify",
                "verifiedReceiptRef": "receipt.notify",
            }
        ],
    }
    with pytest.raises(ExecutionContractError, match="invalid_effect_settlement"):
        loop_ledger_from_document({**document, "entries": [unclassified]})


# --- 2. the seam to the Core record -------------------------------------------


def test_the_core_record_carries_every_runtime_member_and_validates() -> None:
    plan, _source_unused, runner = _loaded_runner()
    ledger = runner.ledger

    record = to_loop_iteration_ledger(
        plan,
        ledger,
        witnesses=_witnesses(ledger),
        loop_settled_at=_LOOP_SETTLED,
    )

    validate_loop_iteration_ledger(record)
    assert from_loop_iteration_ledger(record) == ledger

    entries = record["entries"]
    assert isinstance(entries, list)
    first, second = entries
    assert first["outcomeClass"] == "succeeded"
    assert first["schedulingIntents"] == ["intent.warm", "intent.stage"]
    assert first["resultingCarryDigest"] == _digest("carry.1")
    assert first["carryDigest"] == _digest("carry.0")
    assert first["effectSettlements"] == [
        {
            "effectRequestId": "effect.notify",
            "settlementClass": SETTLEMENT_COMMITTED,
            "completionContribution": {"referenceId": "contribution.notify"},
            "verifiedReceiptRef": {"referenceId": "receipt.notify"},
        },
        {
            "effectRequestId": "effect.invoice",
            "settlementClass": SETTLEMENT_UNKNOWN,
            "completionContribution": {"referenceId": "contribution.invoice"},
        },
    ]
    assert first["lateEvidence"] == {
        "evidenceRef": {"referenceId": "evidence.late.one"},
        "appliedToRunState": False,
    }
    assert second["outcomeClass"] == "failed"
    assert second["failureRef"] == {"referenceId": "berth.closed"}
    assert "outputsDigest" not in second


def test_the_core_record_refuses_an_iteration_still_in_flight() -> None:
    """Every Core entry states an outcome, a settlement sequence and a settled instant.
    There is no shape there for an unsettled iteration, so converting one would mean
    inventing a settlement it never had."""
    plan = _plan()
    source = _source("alpha", "beta")
    runner = LoopRunner(plan, source)
    runner.record_launch(runner.plan_launches()[0])

    with pytest.raises(ExecutionRefused, match="loop_not_settled"):
        to_loop_iteration_ledger(
            plan,
            runner.ledger,
            witnesses=_witnesses(runner.ledger),
            loop_settled_at=_LOOP_SETTLED,
        )


def test_the_core_record_refuses_an_iteration_with_no_witness() -> None:
    """The bundle reference and the two instants are the Core-only members, and this
    oracle holds none of them, so they are required per iteration rather than defaulted:
    a conversion that invented an instant would be making up the durable record."""
    plan, _source_unused, runner = _loaded_runner()
    ledger = runner.ledger
    partial = dict(_witnesses(ledger))
    partial.pop(ledger.entries[1].iteration_identity)

    with pytest.raises(ExecutionContractError, match="missing_witness"):
        to_loop_iteration_ledger(
            plan, ledger, witnesses=partial, loop_settled_at=_LOOP_SETTLED
        )


def test_a_core_record_whose_intents_disown_their_digest_is_refused() -> None:
    """The conversion computes the digest over the intents it wrote, so the pair cannot
    disagree; Core refuses the disagreement independently, which is what makes the
    intents readable rather than decorative."""
    plan, _source_unused, runner = _loaded_runner()
    ledger = runner.ledger
    record = to_loop_iteration_ledger(
        plan, ledger, witnesses=_witnesses(ledger), loop_settled_at=_LOOP_SETTLED
    )
    entries = record["entries"]
    assert isinstance(entries, list)
    entries[0]["schedulingIntents"] = ["intent.stage", "intent.warm"]

    with pytest.raises(ContractSemanticError, match="schedulingIntentsDigest"):
        validate_loop_iteration_ledger(record)


# --- 3. histories a live runner could not have written ------------------------


def test_a_replay_refuses_a_cancellation_its_policy_never_produces() -> None:
    """The regression for the impossible cancellation history.

    `CANCEL_REMAINING` is the one policy that settles an iteration `CANCELLED`:
    `DRAIN_IN_FLIGHT` leaves the in-flight ones to settle normally, `COMPLETE_ALL` lets
    the loop finish, and `settle` refuses `CANCELLED` outright. A ledger carrying one
    under either other policy was not written by this seam, and resuming it replays a
    settlement its own plan cannot account for.
    """
    swept = LoopRunner(_plan(cancellation_policy=CANCEL_REMAINING), _source("alpha"))
    swept.record_launch(swept.plan_launches()[0])
    swept.cancel()
    ledger = swept.ledger

    for policy in (DRAIN_IN_FLIGHT, COMPLETE_ALL):
        with pytest.raises(ExecutionRefused, match="cancellation_not_possible"):
            replay_ledger(
                _plan(cancellation_policy=policy), _source("alpha"), ledger
            )

    # The policy that does produce one still replays, byte for byte.
    resumed = replay_ledger(
        _plan(cancellation_policy=CANCEL_REMAINING), _source("alpha"), ledger
    )
    assert resumed.ledger == ledger


def test_a_replay_refuses_an_in_flight_population_past_the_frozen_bound() -> None:
    """The frozen concurrency bound bounds any ledger a live runner could have written.

    `record_launch` refuses a launch that would exceed it, so a ledger recording more
    unsettled iterations than the bound is a history nothing produced -- and believing
    it resumes a loop already over its bound and lets it launch on from there.
    """
    plan = _plan(
        mode=LOOP_MODE_PARALLEL, maximum_concurrency=2, maximum_iterations=4
    )
    source = _source("alpha", "beta", "gamma")
    runner = LoopRunner(plan, source)
    launched = [runner.record_launch(launch) for launch in runner.plan_launches()]
    assert len(launched) == 2

    over_bound = LoopLedger(
        (
            *runner.ledger.entries,
            IterationLedgerEntry(
                iteration_identity=plan.iteration_identity("gamma"),
                ordinal=2,
                inputs_digest=source[2].payload_digest,
            ),
        )
    )

    with pytest.raises(ExecutionRefused, match="concurrency_exhausted"):
        replay_ledger(plan, source, over_bound)

    # Settling one of them brings the population back inside the bound, and it replays.
    settled = LoopLedger(
        (
            replace(
                over_bound.entries[0],
                outcome=OUTCOME_SUCCEEDED,
                outputs_digest=_digest("outputs.alpha"),
                settlement_sequence=1,
            ),
            *over_bound.entries[1:],
        )
    )
    assert len(replay_ledger(plan, source, settled).in_flight) == 2
