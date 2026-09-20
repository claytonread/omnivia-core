"""RT-206 acceptance for the uncertain-effect reconciler and the late-receipt path.

RT-205 leaves exactly one question open, on purpose: an effect that was handed out and
never answered settles `unknown`, and `unknown` is neither a failure nor a licence to try
again. This file holds what RT-206 adds -- how that question is closed when it can be
closed, and that it stays open when it cannot.

*An unknown effect is never automatically retried.* Nothing in this slice dispatches,
schedules or re-publishes anything. Reconciling an effect leaves its dispatch count
exactly where it was, and an effect whose evidence never arrives is refused rather than
resolved: neither outcome can be established from the record, and neither may be assumed.

*A late receipt is authoritative settlement evidence.* 0023 already retains a receipt that
arrives after an `unknown` settlement. RT-206 is what makes that retention mean something:
the retained receipt is the *only* route to a `committed` reconciliation, it is read from
this database rather than supplied, and the outcome is derived rather than argued.

*The settlement history is not overwritten.* 0023 keys a settlement on its intent and
aborts UPDATE and DELETE, which is why an `unknown` settlement could never be amended into
the answer that later arrived. 0024 resolves that by adding a relation, not by relaxing a
constraint: the `unknown` row stays byte-for-byte as written, the reconciliation is a
second immutable fact naming it, and both are readable. Every test here that reconciles an
effect asserts the settlement it reconciled is still exactly as it was.

*Contradictions fail closed.* A `committed` reconciliation with no retained receipt for
its own intent, a `not_committed` one over an effect this database holds a receipt or a
dispatch record for, a reconciliation of an effect that is not uncertain, evidence about
some other effect, an instant that precedes the settlement or the receipt it rests on, a
second and different final answer -- each is refused by the contract, the pure rule or
0024's guards, and none of them leaves a row behind.

*Repeating is idempotent.* A caller replaying its own command after a crash gets the
stored answer and writes nothing; a caller asking for a different answer is refused.

No public wire surface is touched: the contract version is asserted unchanged, and there is
still no `effect.dispatch` or `effect.reconcile` operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt202_policy_budget_snapshot_repository as r202
import test_rt203_approval_capability_grant_repository as r203
import test_rt205_effect_transaction as t205
from omnivia_core_runtime.service.effect_reconciliation import (
    REASON_RECONCILED_BY_RECEIPT,
    REASON_RECONCILED_NEVER_DISPATCHED,
    decide_reconciliation,
    reconcile_effect_transaction,
)
from omnivia_core_runtime.service.effect_transaction import (
    EffectTransactionError,
    publish_dispatch_request,
)
from omnivia_core_runtime.storage.agent_runtime import (
    EffectReconciliation,
    append_run_event,
    read_effect_dispatch_count,
    read_effect_receipt_for_intent,
    read_effect_reconciliation_for_intent,
    read_effect_settlement_for_intent,
    read_run,
    reconcile_effect,
    record_step_status,
    runtime_writer,
    settle_effect,
    start_attempt,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import (
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractSemanticError,
    EffectSettlement,
)

WORKSPACE_ID = t205.WORKSPACE_ID
RUN_ID = t205.RUN_ID
STEP_ID = t205.STEP_ID
JOB_ID = t205.JOB_ID
ATTEMPT_ID = t205.ATTEMPT_ID
INTENT_ID = t205.INTENT_ID
RECEIPT_ID = t205.RECEIPT_ID
SETTLEMENT_ID = t205.SETTLEMENT_ID
MS = t205.MS
RUNNING_US = t205.RUNNING_US
DECLARED_US = t205.DECLARED_US
at = t205.at

RECONCILIATIONS = "omnivia_runtime_effect_reconciliations"
TABLES = (*t205.TABLES, RECONCILIATIONS)

MIGRATION_VERSION = 24
RECONCILIATION_ID = "rec-0001"

#: One instant per fact, in the order the facts occur. 0024 refuses a reconciliation that
#: precedes the settlement it answers or the receipt it rests on, so these are not
#: interchangeable and a test that needs a stale one states it.
DISPATCHED_US = DECLARED_US + MS
SETTLED_US = DECLARED_US + 2 * MS
OBSERVED_US = DECLARED_US + 3 * MS
RECONCILED_US = DECLARED_US + 4 * MS

AUDIT_REF = m18.audit_ref_for(JOB_ID)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def acting(owned: m1.Owned) -> m1.Owned:
    """One running run with a running attempt, a pinned policy and one issued grant.

    The same state RT-205's `acting` builds, from the same helpers: an effect declared
    here is one the run's own grant authorizes, which is what every refusal below has to
    be distinguishable from.
    """
    r102.admit(owned)
    r102.add_step(owned)
    r202.add_policy(owned)
    r203.issue(owned)
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-rt206-started",
        occurred_at_us=RUNNING_US,
        event_kind="run_started",
        run_status="running",
        run_step_id=STEP_ID,
    )
    record_step_status(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_step_id=STEP_ID,
        status="running",
        observed_at_us=RUNNING_US,
    )
    start_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=RUNNING_US,
    )
    return owned


def counts(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in TABLES}


def settlement_of(holder: m1.Owned) -> EffectSettlement | None:
    return read_effect_settlement_for_intent(
        holder.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )


def reconciliation_of(holder: m1.Owned) -> EffectReconciliation | None:
    return read_effect_reconciliation_for_intent(
        holder.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )


def uncertain(holder: m1.Owned) -> EffectSettlement:
    """Declare, dispatch, and settle -- the crash window that produces an `unknown`.

    Dispatched and never answered: the intent is durable, the outbox holds one row, no
    receipt came back, and RT-205's rule therefore settles it `unknown`. This is the state
    RT-206 exists to close, built through the real seams rather than inserted.
    """
    t205.declare(holder)
    t205.publish(holder, at_us=DISPATCHED_US)
    settled = t205.settle(holder, at_us=SETTLED_US)
    assert settled.outcome == "unknown"
    return settled


def settle_unknown_undispatched(holder: m1.Owned) -> EffectSettlement:
    """An `unknown` settlement over an effect that was never handed out.

    `settle_effect_transaction` never produces this pair -- an effect with no dispatch
    record settles `not_committed` -- so it is written through the repository seam, which
    is the shape a caller reaching past the composition leaves behind. It is the one
    uncertain state that *can* honestly reconcile to `not_committed`, because nothing was
    ever handed out for anyone to have executed.
    """
    t205.declare(holder)
    settlement = EffectSettlement(
        workspace_id=WORKSPACE_ID,
        effect_settlement_id=SETTLEMENT_ID,
        run_id=RUN_ID,
        effect_intent_id=INTENT_ID,
        outcome="unknown",
        settled_at=at(SETTLED_US),
        reason="effect.uncertain",
        audit_reference=AUDIT_REF,
    )
    settle_effect(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        settlement=settlement,
    )
    return settlement


def reconcile(
    holder: m1.Owned,
    *,
    reconciliation_id: str = RECONCILIATION_ID,
    at_us: int = RECONCILED_US,
) -> EffectReconciliation:
    return reconcile_effect_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        effect_intent_id=INTENT_ID,
        effect_reconciliation_id=reconciliation_id,
        reconciled_at_us=at_us,
        audit_ref=AUDIT_REF,
    )


def publish(holder: m1.Owned, *, effect_intent_id: str, at_us: int) -> None:
    """One dispatch of any intent, not only the one RT-205's helper hardcodes."""
    publish_dispatch_request(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        effect_intent_id=effect_intent_id,
        requested_at_us=at_us,
    )


def handmade(**overrides: object) -> EffectReconciliation:
    """One reconciliation built by a caller rather than derived from the record.

    Every guard 0024 states has to be provable against a writer that reached past
    `reconcile_effect_transaction`, because the composition never produces the shapes the
    guards refuse -- that is the point of the guards.
    """
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_reconciliation_id": RECONCILIATION_ID,
        "run_id": RUN_ID,
        "effect_intent_id": INTENT_ID,
        "effect_settlement_id": SETTLEMENT_ID,
        "outcome": "not_committed",
        "reconciled_at": at(RECONCILED_US),
        "reason": REASON_RECONCILED_NEVER_DISPATCHED,
        "audit_reference": AUDIT_REF,
        "effect_receipt_id": None,
    }
    values.update(overrides)
    return EffectReconciliation(**values)  # type: ignore[arg-type]


def write(holder: m1.Owned, record: EffectReconciliation) -> None:
    reconcile_effect(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        reconciliation=record,
    )


# --- 1: the migration is additive and append-only ---------------------------------


def test_migration_0024_adds_one_relation_and_takes_nothing_away() -> None:
    """Additive: one new table, and every relation 0023 defined still present."""
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert [m.name for m in found] == ["0024_runtime_effect_reconciliations.sql"]
    tables = canonical_schema_tables()
    assert RECONCILIATIONS in tables
    for table in t205.TABLES:
        assert table in tables


def test_the_reconciliation_relation_is_append_only(acting: m1.Owned) -> None:
    """UPDATE and DELETE abort for the current fenced owner too.

    A final answer that could be edited is not a final answer, and one that could be
    deleted would take the audit trail of the uncertainty with it.
    """
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconcile(acting)
    for statement in (
        f"UPDATE {RECONCILIATIONS} SET workspace_id = workspace_id",
        f"DELETE FROM {RECONCILIATIONS}",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            runtime_writer(
                acting.connection,
                acting.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=acting.generation,
            ) as writer,
        ):
            writer.connection.execute(statement)
    assert counts(acting)[RECONCILIATIONS] == 1


# --- 2: classifying the uncertain effect, and never retrying it -------------------


def test_a_dispatched_effect_with_no_receipt_is_uncertain_and_owes_reconciliation(
    acting: m1.Owned,
) -> None:
    """The classification RT-206 acts on, and the state it starts from."""
    settled = uncertain(acting)
    assert (settled.outcome, settled.effect_receipt_id) == ("unknown", None)
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == 1
    )
    assert reconciliation_of(acting) is None


def test_an_uncertain_effect_with_no_evidence_is_refused_and_never_retried(
    acting: m1.Owned,
) -> None:
    """Silence is not proof of absence, and it is not a licence to act again.

    The refusal is the whole point: neither outcome can be established from this record,
    so the effect stays `unknown`. Nothing is dispatched, and the outbox is untouched.
    """
    settled = uncertain(acting)
    before = counts(acting)
    with pytest.raises(EffectTransactionError, match="stays uncertain"):
        reconcile(acting)
    assert counts(acting) == before
    assert counts(acting)[RECONCILIATIONS] == 0
    assert settlement_of(acting) == settled
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == 1
    )


def test_reconciling_produces_no_further_dispatch(acting: m1.Owned) -> None:
    """Reconciliation is an answer about work already done, never more work."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconcile(acting)
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == 1
    )
    assert counts(acting)[t205.DISPATCHES] == 1


# --- 3: the late-receipt path ------------------------------------------------------


def test_a_late_receipt_is_retained_and_reconciles_the_effect_committed(
    acting: m1.Owned,
) -> None:
    """The path this slice exists for, end to end.

    The receipt arrives after the effect was already uncertain, 0023 retains it, and the
    reconciliation names it. The `unknown` settlement does not move.
    """
    settled = uncertain(acting)
    late = t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))

    reconciled = reconcile(acting)
    assert reconciled == EffectReconciliation(
        workspace_id=WORKSPACE_ID,
        effect_reconciliation_id=RECONCILIATION_ID,
        run_id=RUN_ID,
        effect_intent_id=INTENT_ID,
        effect_settlement_id=SETTLEMENT_ID,
        outcome="committed",
        reconciled_at=at(RECONCILED_US),
        reason=REASON_RECONCILED_BY_RECEIPT,
        audit_reference=AUDIT_REF,
        effect_receipt_id=RECEIPT_ID,
    )
    assert reconciliation_of(acting) == reconciled
    assert settlement_of(acting) == settled
    assert (
        read_effect_receipt_for_intent(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == late
    )


def test_a_committed_reconciliation_names_the_receipt_that_proves_it(
    acting: m1.Owned,
) -> None:
    """No fabricated success: the outcome is derived from a stored observation."""
    uncertain(acting)
    late = t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconciled = reconcile(acting)
    assert reconciled.effect_receipt_id == late.effect_receipt_id


def test_an_uncertain_effect_never_handed_out_reconciles_not_committed(
    acting: m1.Owned,
) -> None:
    """The other reachable answer, and the only evidence that supports it.

    Nothing was dispatched, so nothing can have executed. The reconciliation names no
    receipt, because carrying one would claim and deny the same observation.
    """
    settlement = settle_unknown_undispatched(acting)
    reconciled = reconcile(acting)
    assert (reconciled.outcome, reconciled.effect_receipt_id) == ("not_committed", None)
    assert reconciled.reason == REASON_RECONCILED_NEVER_DISPATCHED
    assert reconciled.effect_settlement_id == SETTLEMENT_ID
    assert settlement_of(acting) == settlement


# --- 4: the settlement history survives, and both facts read back -----------------


def test_the_unknown_settlement_is_still_readable_after_reconciliation(
    acting: m1.Owned,
) -> None:
    """Two facts about one intent, not one fact overwritten.

    This is the RT-205 limitation resolved rather than bypassed: the original
    settlement remains immutable, and 0024 appends a resulting settlement beside the
    reconciliation bridge rather than rewriting it.
    """
    settled = uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconcile(acting)
    assert settlement_of(acting) == settled
    assert counts(acting)[t205.SETTLEMENTS] == 2
    assert counts(acting)[RECONCILIATIONS] == 1


def test_read_run_reports_the_settlement_and_the_reconciliation_it_received(
    acting: m1.Owned,
) -> None:
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconciled = reconcile(acting)
    snapshot = read_run(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert len(snapshot.effect_settlements) == 2
    assert snapshot.effect_settlements[0].outcome == "unknown"
    assert snapshot.effect_settlements[1].outcome == "committed"
    assert snapshot.effect_reconciliations == (reconciled,)


def test_a_run_with_no_reconciliation_reports_an_empty_tuple(acting: m1.Owned) -> None:
    uncertain(acting)
    snapshot = read_run(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.effect_reconciliations == ()


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    acting: m1.Owned,
) -> None:
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconcile(acting)
    other = m18.OTHER_WORKSPACE_ID
    assert (
        read_effect_reconciliation_for_intent(
            acting.connection, workspace_id=other, effect_intent_id=INTENT_ID
        )
        is None
    )


# --- 5: repeating is idempotent, differing is refused -----------------------------


def test_repeating_the_same_reconciliation_answers_from_the_store(
    acting: m1.Owned,
) -> None:
    """What a caller replaying its own command after a crash issues."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    first = reconcile(acting)
    again = reconcile(acting)
    assert again == first
    assert counts(acting)[RECONCILIATIONS] == 1


def test_a_second_reconciliation_under_a_different_identifier_is_refused(
    acting: m1.Owned,
) -> None:
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    first = reconcile(acting)
    with pytest.raises(EffectTransactionError, match="already reconciled"):
        reconcile(acting, reconciliation_id="rec-0002")
    assert reconciliation_of(acting) == first
    assert counts(acting)[RECONCILIATIONS] == 1


def test_a_second_reconciliation_at_a_different_instant_is_refused(
    acting: m1.Owned,
) -> None:
    """A repeat that differs in any field is a different answer, not a repeat."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    first = reconcile(acting)
    with pytest.raises(EffectTransactionError, match="already reconciled"):
        reconcile(acting, at_us=RECONCILED_US + MS)
    assert reconciliation_of(acting) == first


def test_a_second_final_answer_has_nowhere_to_live(acting: m1.Owned) -> None:
    """Keyed on the intent by 0024, so a writer reaching past the seam is refused too."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconcile(acting)
    with pytest.raises((sqlite3.IntegrityError, ContractSemanticError)):
        write(
            acting,
            handmade(
                effect_reconciliation_id="rec-0002",
                outcome="committed",
                effect_receipt_id=RECEIPT_ID,
                reason=REASON_RECONCILED_BY_RECEIPT,
            ),
        )
    assert counts(acting)[RECONCILIATIONS] == 1


# --- 6: invalid and contradictory transitions ------------------------------------


def test_an_unsettled_effect_is_not_an_uncertain_one(acting: m1.Owned) -> None:
    """What an unsettled effect is owed is a settlement, not a reconciliation."""
    t205.declare(acting)
    t205.publish(acting, at_us=DISPATCHED_US)
    with pytest.raises(EffectTransactionError, match="holds no settlement"):
        reconcile(acting)
    assert counts(acting)[RECONCILIATIONS] == 0


def test_an_effect_settled_committed_is_already_final(acting: m1.Owned) -> None:
    t205.declare(acting)
    t205.publish(acting, at_us=DISPATCHED_US)
    t205.observe(acting)
    settled = t205.settle(acting, at_us=SETTLED_US)
    assert settled.outcome == "committed"
    with pytest.raises(EffectTransactionError, match="already final"):
        reconcile(acting)
    assert counts(acting)[RECONCILIATIONS] == 0


def test_an_effect_settled_not_committed_is_already_final(acting: m1.Owned) -> None:
    t205.declare(acting)
    settled = t205.settle(acting, at_us=SETTLED_US)
    assert settled.outcome == "not_committed"
    with pytest.raises(EffectTransactionError, match="already final"):
        reconcile(acting)
    assert counts(acting)[RECONCILIATIONS] == 0


def test_an_intent_this_workspace_never_declared_is_refused(acting: m1.Owned) -> None:
    with pytest.raises(EffectTransactionError, match="not committed in this workspace"):
        reconcile_effect_transaction(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            effect_intent_id="eff-someone-elses",
            effect_reconciliation_id=RECONCILIATION_ID,
            reconciled_at_us=RECONCILED_US,
            audit_ref=AUDIT_REF,
        )
    assert counts(acting)[RECONCILIATIONS] == 0


def test_an_observed_effect_cannot_be_reconciled_not_committed(
    acting: m1.Owned,
) -> None:
    """Proof it happened beside proof it did not: 0024 fails closed on the pair."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    with pytest.raises(sqlite3.IntegrityError, match="observed effect"):
        write(acting, handmade())
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_dispatched_effect_cannot_be_reconciled_not_committed(
    acting: m1.Owned,
) -> None:
    """An effect that reached the world is not one anybody may declare away."""
    uncertain(acting)
    with pytest.raises(sqlite3.IntegrityError, match="dispatched effect"):
        write(acting, handmade())
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_committed_reconciliation_without_a_retained_receipt_is_refused(
    acting: m1.Owned,
) -> None:
    """The named receipt has to exist; the foreign key sees to that."""
    uncertain(acting)
    with pytest.raises((sqlite3.IntegrityError, ContractSemanticError)):
        write(
            acting,
            handmade(
                outcome="committed",
                effect_receipt_id="rcp-invented",
                reason=REASON_RECONCILED_BY_RECEIPT,
            ),
        )
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_committed_reconciliation_cannot_borrow_another_effects_receipt(
    acting: m1.Owned,
) -> None:
    """A second effect, observed; its receipt is not evidence about the first one."""
    uncertain(acting)
    other_intent = t205.intent(
        effect_intent_id="eff-0002",
        idempotency_key="effect-rt206-0002",
    )
    t205.declare(acting, other_intent)
    publish(acting, effect_intent_id="eff-0002", at_us=DISPATCHED_US)
    t205.observe(
        acting,
        t205.receipt(
            effect_receipt_id="rcp-0002",
            effect_intent_id="eff-0002",
            observed_at=at(OBSERVED_US),
        ),
    )
    with pytest.raises((sqlite3.IntegrityError, ContractSemanticError)):
        write(
            acting,
            handmade(
                outcome="committed",
                effect_receipt_id="rcp-0002",
                reason=REASON_RECONCILED_BY_RECEIPT,
            ),
        )
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_reconciliation_naming_a_settlement_of_another_effect_is_refused(
    acting: m1.Owned,
) -> None:
    """The chain has to be one chain: this settlement, of this intent."""
    uncertain(acting)
    other_intent = t205.intent(
        effect_intent_id="eff-0002",
        idempotency_key="effect-rt206-0002",
    )
    t205.declare(acting, other_intent)
    settle_effect(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
        settlement=EffectSettlement(
            workspace_id=WORKSPACE_ID,
            effect_settlement_id="stl-0002",
            run_id=RUN_ID,
            effect_intent_id="eff-0002",
            outcome="unknown",
            settled_at=at(SETTLED_US),
            reason="effect.uncertain",
            audit_reference=AUDIT_REF,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="unknown settlement of its own"):
        write(acting, handmade(effect_settlement_id="stl-0002"))
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_reconciliation_of_a_settlement_that_is_not_unknown_is_refused(
    acting: m1.Owned,
) -> None:
    """0024's guard, proved against a writer reaching past the pure rule."""
    t205.declare(acting)
    t205.settle(acting, at_us=SETTLED_US)
    with pytest.raises(sqlite3.IntegrityError, match="unknown settlement of its own"):
        write(acting, handmade())
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_reconciliation_before_the_settlement_it_answers_is_refused(
    acting: m1.Owned,
) -> None:
    """Stale: an effect is never reconciled before it was settled."""
    settle_unknown_undispatched(acting)
    with pytest.raises(sqlite3.IntegrityError, match="before it was settled"):
        write(acting, handmade(reconciled_at=at(SETTLED_US - MS)))
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_reconciliation_resting_on_a_receipt_it_predates_is_refused(
    acting: m1.Owned,
) -> None:
    """Stale the other way: the evidence has to have arrived before the answer."""
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    with pytest.raises(sqlite3.IntegrityError, match="APPLIED reconciliation"):
        write(
            acting,
            handmade(
                outcome="committed",
                effect_receipt_id=RECEIPT_ID,
                reason=REASON_RECONCILED_BY_RECEIPT,
                reconciled_at=at(OBSERVED_US - MS),
            ),
        )
    assert counts(acting)[RECONCILIATIONS] == 0


def test_a_reconciliation_of_an_intent_belonging_to_another_run_is_refused(
    acting: m1.Owned,
) -> None:
    uncertain(acting)
    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    with pytest.raises((sqlite3.IntegrityError, ContractSemanticError)):
        write(
            acting,
            handmade(
                run_id="run-not-this-one",
                outcome="committed",
                effect_receipt_id=RECEIPT_ID,
                reason=REASON_RECONCILED_BY_RECEIPT,
            ),
        )
    assert counts(acting)[RECONCILIATIONS] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("effect_reconciliation_id", "-not-an-identifier"),
        ("reason", "Not An Open Code"),
        ("outcome", "not-a-reconciliation-outcome"),
        ("audit_reference", "not a reference"),
    ],
)
def test_a_malformed_reconciliation_is_refused(
    acting: m1.Owned, field: str, value: str
) -> None:
    """Shape, vocabulary and correlation, each refused before a row exists.

    `unknown` among them on purpose: reconciling an effect to uncertainty is not a
    reconciliation, and recording one would let a caller close the question by restating
    it.
    """
    settle_unknown_undispatched(acting)
    with pytest.raises((sqlite3.IntegrityError, ContractSemanticError, StorageError)):
        write(acting, handmade(**{field: value}))
    assert counts(acting)[RECONCILIATIONS] == 0


# --- 7: the pure rule ------------------------------------------------------------


def test_decide_reconciliation_has_two_answers_and_no_default() -> None:
    """Pure, fail-closed, and reachable only through evidence."""
    declared = t205.intent()
    unknown = EffectSettlement(
        workspace_id=WORKSPACE_ID,
        effect_settlement_id=SETTLEMENT_ID,
        run_id=RUN_ID,
        effect_intent_id=INTENT_ID,
        outcome="unknown",
        settled_at=at(SETTLED_US),
        reason="effect.uncertain",
        audit_reference=AUDIT_REF,
    )
    assert decide_reconciliation(
        intent=declared, settlement=unknown, receipt=None, dispatch_count=0
    ) == ("not_committed", REASON_RECONCILED_NEVER_DISPATCHED)
    assert decide_reconciliation(
        intent=declared,
        settlement=unknown,
        receipt=t205.receipt(),
        dispatch_count=1,
    ) == ("committed", REASON_RECONCILED_BY_RECEIPT)
    with pytest.raises(EffectTransactionError, match="stays uncertain"):
        decide_reconciliation(
            intent=declared, settlement=unknown, receipt=None, dispatch_count=1
        )
    with pytest.raises(EffectTransactionError, match="never negative"):
        decide_reconciliation(
            intent=declared, settlement=unknown, receipt=None, dispatch_count=-1
        )


def test_decide_reconciliation_refuses_evidence_about_a_different_effect() -> None:
    declared = t205.intent()
    unknown = EffectSettlement(
        workspace_id=WORKSPACE_ID,
        effect_settlement_id=SETTLEMENT_ID,
        run_id=RUN_ID,
        effect_intent_id=INTENT_ID,
        outcome="unknown",
        settled_at=at(SETTLED_US),
        reason="effect.uncertain",
        audit_reference=AUDIT_REF,
    )
    with pytest.raises(EffectTransactionError, match="not for"):
        decide_reconciliation(
            intent=declared,
            settlement=replace(unknown, effect_intent_id="eff-someone-elses"),
            receipt=None,
            dispatch_count=1,
        )
    with pytest.raises(EffectTransactionError, match="evidence for"):
        decide_reconciliation(
            intent=declared,
            settlement=unknown,
            receipt=t205.receipt(effect_intent_id="eff-someone-elses"),
            dispatch_count=1,
        )


# --- 8: crash and rollback --------------------------------------------------------


def test_a_transaction_that_fails_after_reconciling_leaves_the_effect_uncertain(
    acting: m1.Owned,
) -> None:
    """Nothing half-answered: the reconciliation goes with everything else it wrote."""
    settled = uncertain(acting)
    late = t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))

    with pytest.raises(t205.Boom), runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.reconcile_effect(
            handmade(
                outcome="committed",
                effect_receipt_id=RECEIPT_ID,
                reason=REASON_RECONCILED_BY_RECEIPT,
            )
        )
        raise t205.Boom("the command refused after reconciling")

    assert counts(acting)[RECONCILIATIONS] == 0
    assert reconciliation_of(acting) is None
    assert settlement_of(acting) == settled
    assert (
        read_effect_receipt_for_intent(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == late
    )


def test_a_refused_reconciliation_writes_nothing_and_the_retry_still_works(
    acting: m1.Owned,
) -> None:
    """The refusal leaves the record untouched, so the evidence can still arrive."""
    uncertain(acting)
    with pytest.raises(EffectTransactionError, match="stays uncertain"):
        reconcile(acting)
    assert counts(acting)[RECONCILIATIONS] == 0

    t205.observe(acting, t205.receipt(observed_at=at(OBSERVED_US)))
    reconciled = reconcile(acting)
    assert reconciled.outcome == "committed"
    assert counts(acting)[RECONCILIATIONS] == 1


# --- 9: nothing public moved ------------------------------------------------------


def test_rt206_adds_no_public_wire_surface() -> None:
    """A private service seam: no operation, no catalogue entry, no version move."""
    assert CONTRACT_VERSION == "1.3"
