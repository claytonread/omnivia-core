"""The effect transaction: intent, outbox, receipt, settlement (RT-205).

Two things live here and nothing else: the *typed pure port* a run's authorized action
crosses on its way out of Core, and the one rule the schema cannot state -- **no dispatch
request may be produced before its intent is durably committed.**

Why that rule needs a module. Migration 0023 makes every other half of the ordering
structural: a dispatch record, a receipt and a settlement each name their `EffectIntent`
by foreign key, so none of them can exist without a committed intent row. What SQL cannot
see is *when* the intent's transaction committed. A caller holding an open write
transaction can read its own uncommitted intent back, build a request from it, send it,
and then crash before commit -- leaving an effect in the world with no record that anyone
ever intended it. That is the exact failure "no effect before intent" exists to prevent,
and it is invisible to every constraint in the database.

:func:`publish_dispatch_request` closes it in one line of policy: it refuses to run while
the caller still holds a transaction, then opens its *own* fenced transaction and reads
the intent there. An intent readable in a transaction this function opened is an intent
some earlier transaction committed -- so a rollback leaves nothing to publish, and a
request cannot be built from a write that never landed. The refusal is a refusal, not a
wait: a caller that has not committed yet has a bug, and blocking would hide it.

*Uncertainty is not failure*, and :func:`decide_settlement` is where that is enforced
rather than asserted. It is pure -- no database, no clock, no adapter -- and it answers
from exactly three facts: the intent, the observation if there is one, and how many times
a dispatch request was produced. A receipt is `committed`. No receipt and no dispatch
ever produced is `not_committed`, because an effect never handed out cannot have landed.
No receipt but a dispatch produced is `unknown`: the runtime could not establish either,
which is the honest third answer and must not be reported as a failed effect or licence a
blind retry. Anything contradictory or unrecognized raises. There is no fourth branch and
no default, so no path here can fabricate a success.

What this module deliberately is not. It is **not a Platform adapter and performs no
dispatch**: :class:`DispatchRequest` carries authority and correlation and never an
endpoint, socket, credential, adapter handle or request payload -- exactly the boundary
`AuthorizedInvocation` holds one seam earlier. It registers no operation and adds no
public wire surface; the frozen application catalogue is untouched and there is no
`effect.dispatch` operation. It reconciles nothing: what to do about an `unknown`
settlement is a later milestone, and inventing a rule for it here would be this module
deciding a question no accepted contract has answered.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
from omnivia_core_runtime.storage.agent_runtime import (
    read_effect_dispatch_count,
    read_effect_intent,
    read_effect_receipt_for_intent,
    read_effect_settlement_for_intent,
    runtime_timestamp,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError

#: The reason code each settlement outcome is recorded under. Open, dot-namespaced codes
#: in the accepted `OpenCode` shape, fixed here rather than chosen at the call site so a
#: settled effect's reason states which of :func:`decide_settlement`'s three branches
#: produced it and cannot be overwritten with a caller's own wording.
REASON_RECEIPT_OBSERVED: Final = "effect.receipt_observed"
REASON_NEVER_DISPATCHED: Final = "effect.never_dispatched"
REASON_DISPATCHED_WITHOUT_RECEIPT: Final = "effect.dispatched_without_receipt"


class EffectTransactionError(StorageError):
    """One refusal from this seam, in the storage vocabulary the repository already uses.

    A `StorageError` subclass rather than a new hierarchy: every caller of the runtime
    repository already handles that type, and a refusal here means the same thing those
    do -- the durable record does not permit what was asked, and nothing was written.
    """


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """What a committed intent entitles a caller to invoke, and nothing more.

    Every field is read back from the durable intent rather than supplied, so a request
    cannot describe an effect that differs from the one recorded. `dispatch_number`
    counts the deliveries of *this* intent: a redelivery after a crash carries the same
    `idempotency_key` and `request_digest` and a higher number, which is what makes an
    at-least-once channel safe to retry over.

    There is no adapter handle, endpoint, credential or request payload here, and there
    must never be one -- the same boundary `AuthorizedInvocation` holds one seam earlier.
    This value states *what may be invoked and under whose authority*; the means of
    invoking it is Platform's, and the bytes stay with the caller that hashed them.
    """

    workspace_id: str
    run_id: str
    effect_intent_id: str
    capability_id: str
    capability_grant_id: str
    effect_kind: str
    idempotency_key: str
    request_digest: str
    dispatch_number: int


def publish_dispatch_request(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    effect_intent_id: str,
    requested_at_us: int,
) -> DispatchRequest:
    """Produce the one dispatch request a durably committed intent entitles, or refuse.

    The ordering rule, enforced in the only place it can be. In order:

    1. a caller still inside its own transaction is refused. Its intent is not committed
       yet, and a request built from a write that may still roll back is precisely the
       effect nobody would be able to reconcile;
    2. a fenced transaction is opened here, and the intent is read *in it*. An intent
       readable now is one an earlier transaction committed, so a rolled-back declaration
       leaves nothing to publish and no request is returned;
    3. a settled effect is refused. Its answer is already final, and dispatching again
       would be acting after the record said the acting was over;
    4. the dispatch is recorded and the request is returned. Both happen in that one
       transaction, so a request this function returns is a request the outbox has
       already committed a record of -- never the other way round.
    """
    if connection.in_transaction:
        raise EffectTransactionError(
            "a dispatch request is produced only outside an open transaction; an intent "
            "the caller has not committed yet is not an intent the world may act on"
        )
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
                "there is no dispatch request without a durable intent"
            )
        settled = read_effect_settlement_for_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        if settled is not None:
            raise EffectTransactionError(
                f"effect intent {effect_intent_id!r} is already settled "
                f"{settled.outcome!r}; a settled effect is never dispatched again"
            )
        number = writer.record_effect_dispatch(
            effect_intent_id=effect_intent_id, requested_at_us=requested_at_us
        )
        return DispatchRequest(
            workspace_id=workspace_id,
            run_id=intent.run_id,
            effect_intent_id=intent.effect_intent_id,
            capability_id=intent.capability_id,
            capability_grant_id=intent.capability_grant_id,
            effect_kind=intent.effect_kind,
            idempotency_key=intent.idempotency_key,
            request_digest=intent.request_digest,
            dispatch_number=number,
        )


def decide_settlement(
    *, intent: EffectIntent, receipt: EffectReceipt | None, dispatch_count: int
) -> tuple[str, str]:
    """The outcome and reason one intended effect settles as. Pure, and fail-closed.

    Three facts in, one answer out, with no fourth branch and no default -- which is what
    makes "no fabricated success" a property of the function rather than a convention its
    callers follow:

    * an observation of this intent is `committed`;
    * no observation and no dispatch request ever produced is `not_committed`, because an
      effect never handed out cannot have landed;
    * no observation but a dispatch request produced is `unknown` -- the runtime could
      not establish either. Uncertainty, not failure: it must not be reported as a failed
      effect and must not licence a blind retry of a logically identical effect.

    A receipt for some other intent, or a dispatch count that is not a count, is a
    contradiction rather than a settlement and raises. Neither can be answered honestly,
    and answering either would mean settling this effect on evidence about a different
    one.
    """
    if dispatch_count < 0:
        raise EffectTransactionError(
            "a dispatch count is never negative; an effect cannot be settled from a "
            "count that is not one"
        )
    if receipt is None:
        if dispatch_count == 0:
            return EFFECT_OUTCOME_NOT_COMMITTED, REASON_NEVER_DISPATCHED
        return EFFECT_OUTCOME_UNKNOWN, REASON_DISPATCHED_WITHOUT_RECEIPT
    if receipt.effect_intent_id != intent.effect_intent_id:
        raise EffectTransactionError(
            f"receipt {receipt.effect_receipt_id!r} is evidence for "
            f"{receipt.effect_intent_id!r}, not for {intent.effect_intent_id!r}"
        )
    return EFFECT_OUTCOME_COMMITTED, REASON_RECEIPT_OBSERVED


def settle_effect_transaction(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    effect_intent_id: str,
    effect_settlement_id: str,
    settled_at_us: int,
    audit_ref: str,
) -> EffectSettlement:
    """Settle one effect from what this database holds about it, exactly once.

    The composition :func:`decide_settlement` is the decision half of: the intent, its
    observation and its dispatch count are read inside one fenced transaction, the pure
    rule answers from those three, and the settlement is written in that same transaction
    -- so nothing can be observed or dispatched between the reading and the answering.

    The outcome and the reason are never arguments. A caller that could state the outcome
    could state `committed` for an effect with no receipt behind it, which is the one
    thing this whole seam exists to make impossible.
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
                "there is no effect settlement without an effect intent"
            )
        receipt = read_effect_receipt_for_intent(
            connection, workspace_id=workspace_id, effect_intent_id=effect_intent_id
        )
        outcome, reason = decide_settlement(
            intent=intent,
            receipt=receipt,
            dispatch_count=read_effect_dispatch_count(
                connection,
                workspace_id=workspace_id,
                effect_intent_id=effect_intent_id,
            ),
        )
        settlement = EffectSettlement(
            workspace_id=workspace_id,
            effect_settlement_id=effect_settlement_id,
            run_id=intent.run_id,
            effect_intent_id=effect_intent_id,
            outcome=outcome,
            settled_at=runtime_timestamp(settled_at_us),
            reason=reason,
            audit_reference=audit_ref,
            effect_receipt_id=(
                None if outcome != EFFECT_OUTCOME_COMMITTED or receipt is None
                else receipt.effect_receipt_id
            ),
        )
        writer.settle_effect(settlement)
        return settlement


__all__ = [
    "REASON_DISPATCHED_WITHOUT_RECEIPT",
    "REASON_NEVER_DISPATCHED",
    "REASON_RECEIPT_OBSERVED",
    "DispatchRequest",
    "EffectTransactionError",
    "decide_settlement",
    "publish_dispatch_request",
    "settle_effect_transaction",
]
