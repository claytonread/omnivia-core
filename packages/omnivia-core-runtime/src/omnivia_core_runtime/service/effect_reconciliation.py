"""Reconciling an uncertain effect, and the late-receipt path (RT-206).

RT-205 ends with three answers and one of them open. A receipt is `committed`, an effect
never handed out is `not_committed`, and one handed out with no receipt back is `unknown`
-- honest, not a failure, and explicitly *not* a licence to retry, because retrying an
effect that may already have landed is how one intended action becomes two real ones.
What RT-205 deliberately did not say is what happens next. This module says it.

**Reconciliation is explicit, and it is never a retry.** Nothing here dispatches, schedules
or re-publishes anything, and there is no path by which an `unknown` effect resolves itself
by acting again. An uncertain effect becomes certain when evidence about it is retained, or
it stays uncertain. :func:`decide_reconciliation` has no branch that produces work.

**The late-receipt path.** 0023 already permits a receipt to arrive after an `unknown`
settlement and retains it -- that is the evidence a reconciliation is owed. This module is
what turns that retained evidence into the final answer: a retained receipt for the intent
is `committed`, and it is the *only* way `committed` is reached here. The receipt is read
from the database rather than supplied, the outcome is derived rather than argued, and
0024's guard plus its foreign key require the named receipt to exist and to be evidence for
this same intent. No caller can assert that an effect landed.

**The RT-205 schema limitation, resolved rather than bypassed.** 0023 keys a settlement on
its intent and refuses UPDATE and DELETE, so an `unknown` settlement cannot be amended into
a `committed` one and must not be: it is what the runtime concluded when it concluded it,
and erasing it would erase the fact that the effect was ever uncertain. 0024 therefore adds
a relation rather than relaxing a constraint. The settlement stays exactly as written, the
reconciliation is a second immutable fact naming the settlement it answers, and the two
together are the auditable chain. Settlement history is preserved *and* the final outcome
is durable; neither is bought with the other.

**What cannot be reconciled here, and why that is the honest answer.** An effect with a
dispatch record and no retained receipt has no evidence either way in this database, and
:func:`decide_reconciliation` refuses it rather than guessing. Establishing what became of
it needs a fact from outside Core -- from whatever actually holds the effect -- and no
accepted contract describes such a fact, so inventing one here would be this module
deciding a question no contract has answered. The effect stays `unknown`, which is exactly
what `unknown` is for.

This module adds no adapter, no transport, no dispatcher and no public wire operation, for
the same reasons :mod:`.effect_transaction` adds none.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from omnivia_core.contracts.v1 import (
    EFFECT_OUTCOME_COMMITTED,
    EFFECT_OUTCOME_NOT_COMMITTED,
    EFFECT_OUTCOME_UNKNOWN,
    EffectIntent,
    EffectReceipt,
    EffectSettlement,
)
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.effect_transaction import EffectTransactionError
from omnivia_core_runtime.storage.agent_runtime import (
    EffectReconciliation,
    read_effect_dispatch_count,
    read_effect_intent,
    read_effect_receipt_for_intent,
    read_effect_reconciliation_for_intent,
    read_effect_settlement_for_intent,
    runtime_timestamp,
    runtime_writer,
)

#: The reason code each reconciled outcome is recorded under, in the same open,
#: dot-namespaced `OpenCode` shape RT-205's settlement reasons use. Fixed here rather than
#: chosen at the call site, for RT-205's reason: a reconciled effect's reason states which
#: branch of :func:`decide_reconciliation` produced it, and a caller cannot reword it.
#: Distinct from the settlement reasons, because "settled committed on a receipt" and
#: "reconciled committed on a receipt that arrived after the effect was already uncertain"
#: are different facts and one code for both would lose the difference.
REASON_RECONCILED_BY_RECEIPT: Final = "effect.reconciled_by_late_receipt"
REASON_RECONCILED_NEVER_DISPATCHED: Final = "effect.reconciled_never_dispatched"


def decide_reconciliation(
    *,
    intent: EffectIntent,
    settlement: EffectSettlement | None,
    receipt: EffectReceipt | None,
    dispatch_count: int,
) -> tuple[str, str]:
    """The outcome and reason one uncertain effect reconciles to. Pure, and fail-closed.

    The same shape as :func:`~.effect_transaction.decide_settlement` and the same
    discipline: no database, no clock, no adapter, no default branch, and an outcome that
    is derived from evidence rather than supplied by a caller.

    Two answers are reachable and everything else refuses:

    * a receipt for this intent is `committed` -- the late-receipt path, and the only way
      `committed` is ever reached here;
    * no receipt and no dispatch request ever produced is `not_committed`, because an
      effect never handed out cannot have landed. An effect with a dispatch record is
      never `not_committed`, however long its receipt fails to arrive: declaring that an
      effect did not happen while this database holds proof it was handed out is a
      contradiction, and silence is not proof of absence.

    Everything else is refused rather than answered. An unsettled effect has no
    uncertainty to reconcile -- what it gets is a settlement. A `committed` or
    `not_committed` settlement is already final, and reconciling one would be overturning
    a concluded answer rather than resolving an open one. A settlement or receipt about a
    different intent is evidence about a different effect. And an effect that was
    dispatched with no receipt retained has, in this database, no evidence either way:
    answering it would mean fabricating either a success or a failure, so it stays
    `unknown` -- which is not a stall but the correct state for a question nobody can yet
    answer.
    """
    if dispatch_count < 0:
        raise EffectTransactionError(
            "a dispatch count is never negative; an effect cannot be reconciled from a "
            "count that is not one"
        )
    if settlement is None:
        raise EffectTransactionError(
            f"effect {intent.effect_intent_id!r} holds no settlement; an unsettled "
            "effect is not an uncertain one and what it is owed is a settlement"
        )
    if settlement.effect_intent_id != intent.effect_intent_id:
        raise EffectTransactionError(
            f"settlement {settlement.effect_settlement_id!r} answers for "
            f"{settlement.effect_intent_id!r}, not for {intent.effect_intent_id!r}"
        )
    if settlement.outcome != EFFECT_OUTCOME_UNKNOWN:
        raise EffectTransactionError(
            f"effect {intent.effect_intent_id!r} is settled {settlement.outcome!r} and "
            "is already final; only an effect settled 'unknown' is reconciled"
        )
    if receipt is None:
        if dispatch_count == 0:
            return EFFECT_OUTCOME_NOT_COMMITTED, REASON_RECONCILED_NEVER_DISPATCHED
        raise EffectTransactionError(
            f"effect {intent.effect_intent_id!r} was dispatched and no receipt is "
            "retained for it; it stays uncertain, because neither outcome can be "
            "established from this record and neither may be assumed"
        )
    if receipt.effect_intent_id != intent.effect_intent_id:
        raise EffectTransactionError(
            f"receipt {receipt.effect_receipt_id!r} is evidence for "
            f"{receipt.effect_intent_id!r}, not for {intent.effect_intent_id!r}"
        )
    return EFFECT_OUTCOME_COMMITTED, REASON_RECONCILED_BY_RECEIPT


def reconcile_effect_transaction(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    effect_intent_id: str,
    effect_reconciliation_id: str,
    reconciled_at_us: int,
    audit_ref: str,
) -> EffectReconciliation:
    """Reconcile one uncertain effect from what this database holds, exactly once.

    The composition :func:`decide_reconciliation` is the decision half of. The intent, its
    settlement, its retained observation and its dispatch count are read inside one fenced
    transaction, the pure rule answers from those four, and the reconciliation is written
    in that same transaction -- so nothing can be observed, dispatched or settled between
    the reading and the answering, and a rollback leaves the effect exactly as uncertain
    as it was.

    Repeating it is idempotent rather than a second answer. A reconciliation already
    stored for this intent is compared against the one this call would produce, and an
    identical request is answered from the store with nothing written -- which is what a
    caller replaying its own command after a crash issues. A request that differs in any
    field is refused: 0024 keys the row on the intent, so a second, *different* final
    answer has nowhere to live, and a caller asking for one is asking this seam to change
    an answer rather than to repeat it.

    The outcome and the reason are never arguments, for RT-205's reason: a caller that
    could state the outcome could state `committed` for an effect with no receipt behind
    it, which is the one thing this seam exists to make impossible.
    """
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        intent = read_effect_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        if intent is None:
            raise EffectTransactionError(
                f"effect intent {effect_intent_id!r} is not committed in this workspace; "
                "there is no effect reconciliation without an effect intent"
            )
        settlement = read_effect_settlement_for_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        receipt = read_effect_receipt_for_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        outcome, reason = decide_reconciliation(
            intent=intent,
            settlement=settlement,
            receipt=receipt,
            dispatch_count=read_effect_dispatch_count(
                connection,
                workspace_id=workspace_id,
                effect_intent_id=effect_intent_id,
            ),
        )
        assert settlement is not None  # decide_reconciliation refuses `None`.
        reconciliation = EffectReconciliation(
            workspace_id=workspace_id,
            effect_reconciliation_id=effect_reconciliation_id,
            run_id=intent.run_id,
            effect_intent_id=effect_intent_id,
            effect_settlement_id=settlement.effect_settlement_id,
            outcome=outcome,
            reconciled_at=runtime_timestamp(reconciled_at_us),
            reason=reason,
            audit_reference=audit_ref,
            effect_receipt_id=(
                None if outcome != EFFECT_OUTCOME_COMMITTED or receipt is None
                else receipt.effect_receipt_id
            ),
        )
        stored = read_effect_reconciliation_for_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        if stored is not None:
            if stored != reconciliation:
                raise EffectTransactionError(
                    f"effect {effect_intent_id!r} is already reconciled "
                    f"{stored.outcome!r}; a second, different final answer about one "
                    "effect is a contradiction"
                )
            return stored
        writer.reconcile_effect(reconciliation)
        return reconciliation


__all__ = [
    "REASON_RECONCILED_BY_RECEIPT",
    "REASON_RECONCILED_NEVER_DISPATCHED",
    "decide_reconciliation",
    "reconcile_effect_transaction",
]
