"""RT-205 acceptance for the effect transaction: intent, outbox, receipt, settlement.

The one invariant this slice exists for is an *ordering* one, and ordering is what a
schema cannot see. Migration 0023 makes every other half structural -- a dispatch record,
a receipt and a settlement each name their `EffectIntent` by foreign key, so none can
exist without a committed intent row -- and this file holds what is left: that a dispatch
request is never produced from an intent the declaring transaction has not committed, and
never at all from one that rolled back.

The crash windows are modelled explicitly rather than assumed away.

*Declare, then crash before commit.* The intent and everything the same transaction wrote
disappear together, and there is nothing left to dispatch. Not "the request is discarded"
-- the request was never producible, because the only seam that produces one refuses to
run inside the caller's open transaction and then reads the intent in a transaction of
its own.

*Declare, dispatch, then crash before the receipt.* The outbox row survives, and that is
the whole reason it is a table: an intent with no receipt and no dispatch record was never
handed out and settles `not_committed`, while one with a dispatch record and no receipt
settles `unknown`. Uncertainty is not failure, and it is not a licence to retry blindly.

*Deliver twice.* The same logical key over the same request is one effect however often
it is declared, and over a *different* request it is a conflict rather than a replay. The
same observation delivered twice is retained once; a second, different observation of one
effect is a contradiction and is refused.

*Observe late.* A receipt arriving after an `unknown` settlement is retained -- it is the
evidence a reconciliation is owed, and discarding it would destroy the only record of
what happened. Arriving after a `not_committed` settlement it is refused, because proof
it happened beside proof it did not is a contradiction this seam fails closed on rather
than choosing a half to believe.

*No fabricated success.* `decide_settlement` has three branches and no default, the
outcome is never an argument a caller supplies, and a `committed` settlement must name a
stored receipt for its own intent -- checked by the accepted contract's semantics, by
0023's guard, and by its foreign key.

No public wire surface is touched: the contract version is asserted unchanged, and no
`effect.dispatch` operation exists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt202_policy_budget_snapshot_repository as r202
import test_rt203_approval_capability_grant_repository as r203
from omnivia_core_runtime.service.effect_transaction import (
    REASON_DISPATCHED_WITHOUT_RECEIPT,
    REASON_NEVER_DISPATCHED,
    REASON_RECEIPT_OBSERVED,
    DispatchRequest,
    EffectTransactionError,
    decide_settlement,
    publish_dispatch_request,
    settle_effect_transaction,
)
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    declare_effect_intent,
    read_effect_dispatch_count,
    read_effect_intent,
    read_effect_receipt_for_intent,
    read_effect_settlement_for_intent,
    read_run,
    read_run_effect_intents,
    record_effect_receipt,
    record_step_status,
    runtime_timestamp,
    runtime_writer,
    settle_effect,
    start_attempt,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import (
    canonical_schema_tables,
    materialise_phase0_baseline,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractSemanticError,
    EffectIntent,
    EffectReceipt,
    EffectSettlement,
    ExternalReference,
)

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID
MS = r202.MS

INTENTS = "omnivia_runtime_effect_intents"
DISPATCHES = "omnivia_runtime_effect_dispatches"
RECEIPTS = "omnivia_runtime_effect_receipts"
SETTLEMENTS = "omnivia_runtime_effect_settlements"
TABLES = (INTENTS, DISPATCHES, RECEIPTS, SETTLEMENTS)

ATTEMPT_ID = "att-rt205-0001"
INTENT_ID = "eff-0001"
RECEIPT_ID = "rcp-0001"
SETTLEMENT_ID = "stl-0001"
STARTED_EVENT_ID = "evt-rt205-started"

IDEMPOTENCY_KEY = "effect-rt205-0001"
REQUEST_DIGEST = m18.DIGEST
RESPONSE_DIGEST = "sha256:" + "d" * 64

#: The instant the run starts and its attempt begins. Everything an effect states falls
#: at or after it, because 0023 refuses an intent declared before its attempt started.
RUNNING_US = BASE_US + MS
DECLARED_US = RUNNING_US + MS


def at(instant_us: int) -> str:
    """One microsecond instant as the canonical timestamp a read renders it as.

    The repository's own renderer rather than a second one here: an effect is
    materialised from microsecond columns, so a hand-spelled instant would differ from
    the read for no reason a test should have to know about.
    """
    return runtime_timestamp(instant_us)


class Boom(RuntimeError):
    """An injected failure, distinguishable from any refusal a seam raises."""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def admitted(owned: m1.Owned) -> m1.Owned:
    """One admitted run with a step, a pinned policy and one issued grant.

    Admitted and not yet running, so the run in this state may declare nothing: it is
    the fixture the `permits_new_effect` refusal is proved against.
    """
    r102.admit(owned)
    r102.add_step(owned)
    r202.add_policy(owned)
    r203.issue(owned)
    return owned


@pytest.fixture
def acting(admitted: m1.Owned) -> m1.Owned:
    """The same run, running, with one running attempt to declare effects from."""
    append_run_event(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_id=RUN_ID,
        runtime_event_id=STARTED_EVENT_ID,
        occurred_at_us=RUNNING_US,
        event_kind="run_started",
        run_status="running",
        run_step_id=STEP_ID,
    )
    record_step_status(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_step_id=STEP_ID,
        status="running",
        observed_at_us=RUNNING_US,
    )
    start_attempt(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=RUNNING_US,
    )
    return admitted


def intent(**overrides: Any) -> EffectIntent:
    """One effect the run's issued grant actually authorizes."""
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "effect_intent_id": INTENT_ID,
        "run_id": RUN_ID,
        "run_step_id": STEP_ID,
        "attempt_id": ATTEMPT_ID,
        "capability_id": "memory.read",
        "capability_grant_id": r203.GRANT_ID,
        "effect_kind": "memory.read",
        "idempotency_key": IDEMPOTENCY_KEY,
        "request_digest": REQUEST_DIGEST,
        "declared_at": at(DECLARED_US),
    }
    values.update(overrides)
    return EffectIntent(**values)


def receipt(**overrides: Any) -> EffectReceipt:
    """One observation of the effect `intent()` declares."""
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "effect_receipt_id": RECEIPT_ID,
        "run_id": RUN_ID,
        "effect_intent_id": INTENT_ID,
        "observed_at": at(DECLARED_US + MS),
        "response_digest": RESPONSE_DIGEST,
    }
    values.update(overrides)
    return EffectReceipt(**values)


def declare(holder: m1.Owned, record: EffectIntent | None = None) -> EffectIntent:
    return declare_effect_intent(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        intent=record or intent(),
    )


def publish(holder: m1.Owned, *, at_us: int = DECLARED_US + MS) -> DispatchRequest:
    return publish_dispatch_request(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        effect_intent_id=INTENT_ID,
        requested_at_us=at_us,
    )


def observe(holder: m1.Owned, record: EffectReceipt | None = None) -> EffectReceipt:
    return record_effect_receipt(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        receipt=record or receipt(),
    )


def settle(
    holder: m1.Owned,
    *,
    settlement_id: str = SETTLEMENT_ID,
    at_us: int = DECLARED_US + 2 * MS,
) -> EffectSettlement:
    return settle_effect_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        effect_intent_id=INTENT_ID,
        effect_settlement_id=settlement_id,
        settled_at_us=at_us,
        audit_ref=m18.audit_ref_for(JOB_ID),
    )


def counts(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in TABLES}


# --- 1: atomic commit ordering, and no dispatch before commit ----------------------


def test_an_intent_commits_with_everything_its_transaction_wrote(
    acting: m1.Owned,
) -> None:
    """One fenced transaction, one commit: the intent and the event land together."""
    with runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.declare_effect_intent(intent())
        writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id="evt-rt205-intended",
            occurred_at_us=DECLARED_US,
            event_kind="effect_intended",
            run_status="running",
            run_step_id=STEP_ID,
        )

    stored = read_effect_intent(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    assert stored == intent()
    assert counts(acting) == {INTENTS: 1, DISPATCHES: 0, RECEIPTS: 0, SETTLEMENTS: 0}
    events = read_run(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert events is not None
    assert events.events[-1].event_kind == "effect_intended"


def test_a_transaction_that_fails_after_declaring_leaves_nothing_to_dispatch(
    acting: m1.Owned,
) -> None:
    """The crash-before-commit window: no intent, and therefore no dispatch, ever."""
    with pytest.raises(Boom), runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.declare_effect_intent(intent())
        writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id="evt-rt205-intended",
            occurred_at_us=DECLARED_US,
            event_kind="effect_intended",
            run_status="running",
        )
        raise Boom("the command refused after declaring")

    assert counts(acting) == {INTENTS: 0, DISPATCHES: 0, RECEIPTS: 0, SETTLEMENTS: 0}
    assert (
        read_effect_intent(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        is None
    )
    with pytest.raises(EffectTransactionError, match="not committed"):
        publish(acting)
    assert counts(acting)[DISPATCHES] == 0


def test_a_dispatch_request_is_refused_while_the_declaring_transaction_is_open(
    acting: m1.Owned,
) -> None:
    """The rule 0023 cannot state: an uncommitted intent is not one the world may act on.

    The caller can read its own uncommitted intent back -- that is exactly why the
    refusal has to exist -- so the seam refuses on the *transaction*, not on the row.
    """
    with runtime_writer(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
    ) as writer:
        writer.declare_effect_intent(intent())
        assert acting.connection.in_transaction
        # Visible to this transaction, and still not publishable.
        assert (
            read_effect_intent(
                acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
            )
            is not None
        )
        with pytest.raises(EffectTransactionError, match="outside an open transaction"):
            publish(acting)

    assert counts(acting) == {INTENTS: 1, DISPATCHES: 0, RECEIPTS: 0, SETTLEMENTS: 0}


def test_a_committed_intent_publishes_exactly_what_it_recorded(
    acting: m1.Owned,
) -> None:
    """The request is read back from the durable intent, never rebuilt by the caller."""
    declare(acting)
    request = publish(acting)
    assert request == DispatchRequest(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        effect_intent_id=INTENT_ID,
        capability_id="memory.read",
        capability_grant_id=r203.GRANT_ID,
        effect_kind="memory.read",
        idempotency_key=IDEMPOTENCY_KEY,
        request_digest=REQUEST_DIGEST,
        dispatch_number=1,
    )
    assert counts(acting)[DISPATCHES] == 1


def test_a_redelivery_repeats_the_key_and_digest_under_a_higher_number(
    acting: m1.Owned,
) -> None:
    """Crash after dispatch, before the receipt: retrying is safe over the same key."""
    declare(acting)
    first = publish(acting)
    second = publish(acting, at_us=DECLARED_US + 2 * MS)
    assert (second.idempotency_key, second.request_digest) == (
        first.idempotency_key,
        first.request_digest,
    )
    assert (first.dispatch_number, second.dispatch_number) == (1, 2)
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == 2
    )


def test_a_settled_effect_is_never_dispatched_again(acting: m1.Owned) -> None:
    declare(acting)
    publish(acting)
    observe(acting)
    settle(acting)
    with pytest.raises(EffectTransactionError, match="already settled"):
        publish(acting, at_us=DECLARED_US + 3 * MS)
    assert counts(acting)[DISPATCHES] == 1


# --- 2: retry and idempotency -----------------------------------------------------


def test_the_same_key_over_the_same_request_is_one_effect_however_often_declared(
    acting: m1.Owned,
) -> None:
    """A retry after a crash acts on the effect that was declared, not on a new one."""
    first = declare(acting)
    replayed = declare(acting, intent(effect_intent_id="eff-0002"))
    assert replayed == first
    assert counts(acting)[INTENTS] == 1
    assert read_run_effect_intents(
        acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ) == (first,)


def test_the_same_key_over_a_different_request_is_a_conflict_not_a_replay(
    acting: m1.Owned,
) -> None:
    """Two different effects claiming one identity are refused, never merged."""
    declare(acting)
    with pytest.raises(StorageError, match="conflict, never a replay"):
        declare(
            acting,
            intent(
                effect_intent_id="eff-0002", request_digest="sha256:" + "e" * 64
            ),
        )
    assert counts(acting)[INTENTS] == 1
    stored = read_effect_intent(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    assert stored is not None and stored.request_digest == REQUEST_DIGEST


def test_only_a_running_run_declares_a_new_effect(admitted: m1.Owned) -> None:
    """An admitted run has not started; `permits_new_effect` grants it nothing."""
    with pytest.raises(StorageError, match="may declare no new effect"):
        declare(admitted, intent(attempt_id=ATTEMPT_ID))
    assert m1.count(admitted.connection, INTENTS) == 0


def test_an_intent_must_act_through_a_grant_for_the_capability_it_invokes(
    acting: m1.Owned,
) -> None:
    """A grant for something else is not authorization for this effect."""
    with pytest.raises(ContractSemanticError):
        declare(acting, intent(capability_id="memory.write", effect_kind="memory.write"))
    assert counts(acting)[INTENTS] == 0


# --- 3: receipts, duplicated, late, malformed and foreign --------------------------


def test_the_same_observation_delivered_twice_is_retained_once(
    acting: m1.Owned,
) -> None:
    declare(acting)
    publish(acting)
    first = observe(acting)
    again = observe(acting)
    assert again == first
    assert counts(acting)[RECEIPTS] == 1


def test_a_second_different_observation_of_one_effect_is_refused(
    acting: m1.Owned,
) -> None:
    """Two disagreeing answers about one effect is a contradiction, not an amendment."""
    declare(acting)
    publish(acting)
    observe(acting)
    with pytest.raises(StorageError, match="already observed"):
        observe(
            acting,
            receipt(
                effect_receipt_id="rcp-0002", response_digest="sha256:" + "f" * 64
            ),
        )
    stored = read_effect_receipt_for_intent(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    assert stored is not None and stored.response_digest == RESPONSE_DIGEST
    assert counts(acting)[RECEIPTS] == 1


def test_a_late_receipt_after_an_unknown_settlement_is_still_retained(
    acting: m1.Owned,
) -> None:
    """`unknown` says a reconciliation is owed; discarding its evidence would end that.

    The settlement itself does not move: it is made once, and what reconciles an
    `unknown` one is a later milestone this slice invents no rule for.
    """
    declare(acting)
    publish(acting)
    settled = settle(acting)
    assert settled.outcome == "unknown"

    late = observe(acting, receipt(observed_at=at(DECLARED_US + 3 * MS)))
    assert late.effect_intent_id == INTENT_ID
    assert counts(acting)[RECEIPTS] == 1
    still = read_effect_settlement_for_intent(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    assert still == settled


def test_a_late_receipt_contradicting_a_not_committed_settlement_is_refused(
    acting: m1.Owned,
) -> None:
    """Proof it happened beside proof it did not: fail closed rather than pick a half.

    `decide_settlement` never reaches this pair on its own -- an effect it settles
    `not_committed` is one that was never dispatched, and a receipt for that is refused a
    guard earlier. This is the shape a caller reaching past the composition produces: a
    dispatched effect declared not to have landed, and then a delivery that says it did.
    """
    declare(acting)
    publish(acting)
    settle_effect(
        acting.connection,
        acting.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=acting.generation,
        settlement=EffectSettlement(
            workspace_id=WORKSPACE_ID,
            effect_settlement_id=SETTLEMENT_ID,
            run_id=RUN_ID,
            effect_intent_id=INTENT_ID,
            outcome="not_committed",
            settled_at=at(DECLARED_US + 2 * MS),
            reason=REASON_NEVER_DISPATCHED,
            audit_reference=m18.audit_ref_for(JOB_ID),
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="settled not_committed"):
        observe(acting, receipt(observed_at=at(DECLARED_US + 3 * MS)))
    assert counts(acting)[RECEIPTS] == 0


def test_an_observation_of_an_effect_never_dispatched_is_refused(
    acting: m1.Owned,
) -> None:
    """No dispatch, no observation: nothing was handed out for anyone to answer."""
    declare(acting)
    with pytest.raises(sqlite3.IntegrityError, match="never dispatched"):
        observe(acting)
    assert counts(acting) == {INTENTS: 1, DISPATCHES: 0, RECEIPTS: 0, SETTLEMENTS: 0}


def test_a_foreign_receipt_names_no_declared_intent_and_is_refused(
    acting: m1.Owned,
) -> None:
    """*No effect before intent*, from the reading end: refused, not stored as evidence."""
    declare(acting)
    publish(acting)
    with pytest.raises(ContractSemanticError, match="names no declared intent"):
        observe(acting, receipt(effect_intent_id="eff-someone-elses"))
    assert counts(acting)[RECEIPTS] == 0


def test_a_receipt_observed_before_its_intent_was_declared_is_refused(
    acting: m1.Owned,
) -> None:
    declare(acting)
    publish(acting)
    with pytest.raises(ContractSemanticError, match="observed before it was intended"):
        observe(acting, receipt(observed_at=at(RUNNING_US)))
    assert counts(acting)[RECEIPTS] == 0


def test_a_malformed_receipt_is_refused_before_a_statement_is_issued(
    acting: m1.Owned,
) -> None:
    """A digest that is not one, refused as the contract's own semantic error."""
    declare(acting)
    publish(acting)
    with pytest.raises(ContractSemanticError):
        observe(acting, receipt(response_digest="not-a-digest"))
    assert counts(acting)[RECEIPTS] == 0


def test_a_subordinate_provider_reference_round_trips_beside_the_receipt(
    acting: m1.Owned,
) -> None:
    """Correlation is retained; it never becomes the authority for what happened here."""
    reference = ExternalReference(
        source_kind="external_log",
        source_id="provider-request-77",
        workspace_id=WORKSPACE_ID,
    )
    declare(acting)
    publish(acting)
    observe(acting, receipt(external_reference=reference))
    stored = read_effect_receipt_for_intent(
        acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
    )
    assert stored is not None and stored.external_reference == reference


# --- 4: settlement outcomes -------------------------------------------------------


def test_an_effect_never_dispatched_settles_not_committed(acting: m1.Owned) -> None:
    """It was never handed out, so it cannot have landed. Deterministic, not uncertain."""
    declare(acting)
    settled = settle(acting)
    assert (settled.outcome, settled.reason) == (
        "not_committed",
        REASON_NEVER_DISPATCHED,
    )
    assert settled.effect_receipt_id is None


def test_an_effect_dispatched_without_a_receipt_settles_unknown(
    acting: m1.Owned,
) -> None:
    """The crash-after-dispatch window: uncertainty, and it is not failure."""
    declare(acting)
    publish(acting)
    settled = settle(acting)
    assert (settled.outcome, settled.reason) == (
        "unknown",
        REASON_DISPATCHED_WITHOUT_RECEIPT,
    )
    assert settled.effect_receipt_id is None


def test_an_observed_effect_settles_committed_naming_the_receipt_that_proves_it(
    acting: m1.Owned,
) -> None:
    declare(acting)
    publish(acting)
    observe(acting)
    settled = settle(acting)
    assert (settled.outcome, settled.reason, settled.effect_receipt_id) == (
        "committed",
        REASON_RECEIPT_OBSERVED,
        RECEIPT_ID,
    )


def test_a_settlement_is_made_once_and_never_re_answered(acting: m1.Owned) -> None:
    """Keyed on the intent, so a second answer of any outcome has nowhere to live."""
    declare(acting)
    first = settle(acting)
    with pytest.raises(sqlite3.IntegrityError):
        settle(acting, settlement_id="stl-0002", at_us=DECLARED_US + 4 * MS)
    assert counts(acting)[SETTLEMENTS] == 1
    assert (
        read_effect_settlement_for_intent(
            acting.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT_ID
        )
        == first
    )


def test_committed_cannot_be_asserted_without_a_stored_observation(
    acting: m1.Owned,
) -> None:
    """No fabricated success, proved against the write seam rather than the composition.

    `settle_effect_transaction` never takes an outcome, so this is the shape a caller
    reaching past it would have to use -- and the accepted contract refuses it.
    """
    declare(acting)
    publish(acting)
    with pytest.raises(ContractSemanticError, match="names no observed receipt"):
        settle_effect(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            settlement=EffectSettlement(
                workspace_id=WORKSPACE_ID,
                effect_settlement_id=SETTLEMENT_ID,
                run_id=RUN_ID,
                effect_intent_id=INTENT_ID,
                outcome="committed",
                settled_at=at(DECLARED_US + 2 * MS),
                reason=REASON_RECEIPT_OBSERVED,
                audit_reference=m18.audit_ref_for(JOB_ID),
                effect_receipt_id=RECEIPT_ID,
            ),
        )
    assert counts(acting)[SETTLEMENTS] == 0


def test_not_committed_cannot_be_asserted_over_an_observation(
    acting: m1.Owned,
) -> None:
    """The other half of the contradiction rule, held by 0023's own guard."""
    declare(acting)
    publish(acting)
    observe(acting)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be settled not_committed"):
        settle_effect(
            acting.connection,
            acting.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=acting.generation,
            settlement=EffectSettlement(
                workspace_id=WORKSPACE_ID,
                effect_settlement_id=SETTLEMENT_ID,
                run_id=RUN_ID,
                effect_intent_id=INTENT_ID,
                outcome="not_committed",
                settled_at=at(DECLARED_US + 2 * MS),
                reason=REASON_NEVER_DISPATCHED,
                audit_reference=m18.audit_ref_for(JOB_ID),
            ),
        )
    assert counts(acting)[SETTLEMENTS] == 0


def test_decide_settlement_is_pure_and_has_no_fourth_answer() -> None:
    """Three facts in, one of three answers out, with no default and no clock."""
    declared = intent()
    observed = receipt()
    assert decide_settlement(
        intent=declared, receipt=None, dispatch_count=0
    ) == ("not_committed", REASON_NEVER_DISPATCHED)
    assert decide_settlement(
        intent=declared, receipt=None, dispatch_count=3
    ) == ("unknown", REASON_DISPATCHED_WITHOUT_RECEIPT)
    assert decide_settlement(
        intent=declared, receipt=observed, dispatch_count=1
    ) == ("committed", REASON_RECEIPT_OBSERVED)


def test_decide_settlement_refuses_evidence_about_a_different_effect() -> None:
    with pytest.raises(EffectTransactionError, match="is evidence for"):
        decide_settlement(
            intent=intent(),
            receipt=replace(receipt(), effect_intent_id="eff-0002"),
            dispatch_count=1,
        )
    with pytest.raises(EffectTransactionError, match="never negative"):
        decide_settlement(intent=intent(), receipt=None, dispatch_count=-1)


# --- 5: the run reads back, and nothing public moved -------------------------------


def test_read_run_reports_the_effect_family_it_holds(acting: m1.Owned) -> None:
    declare(acting)
    publish(acting)
    observed = observe(acting)
    settled = settle(acting)
    snapshot = read_run(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.effect_intents == (intent(),)
    assert snapshot.effect_receipts == (observed,)
    assert snapshot.effect_settlements == (settled,)


def test_a_run_with_no_effects_reports_three_empty_tuples(acting: m1.Owned) -> None:
    snapshot = read_run(acting.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.effect_intents == ()
    assert snapshot.effect_receipts == ()
    assert snapshot.effect_settlements == ()


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    acting: m1.Owned,
) -> None:
    declare(acting)
    publish(acting)
    observe(acting)
    other = m18.OTHER_WORKSPACE_ID
    assert (
        read_effect_intent(
            acting.connection, workspace_id=other, effect_intent_id=INTENT_ID
        )
        is None
    )
    assert (
        read_effect_receipt_for_intent(
            acting.connection, workspace_id=other, effect_intent_id=INTENT_ID
        )
        is None
    )
    assert read_run_effect_intents(
        acting.connection, workspace_id=other, run_id=RUN_ID
    ) == ()
    assert (
        read_effect_dispatch_count(
            acting.connection, workspace_id=other, effect_intent_id=INTENT_ID
        )
        == 0
    )


@pytest.mark.parametrize("table", TABLES)
def test_every_effect_relation_is_append_only(acting: m1.Owned, table: str) -> None:
    """UPDATE and DELETE abort for the current fenced owner too."""
    declare(acting)
    publish(acting)
    observe(acting)
    settle(acting)
    assert table in canonical_schema_tables()
    for statement in (
        f"UPDATE {table} SET workspace_id = workspace_id",
        f"DELETE FROM {table}",
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


def test_rt205_adds_no_public_wire_surface() -> None:
    """A private service seam: no operation, no catalogue entry, no version move."""
    assert CONTRACT_VERSION == "1.3"
