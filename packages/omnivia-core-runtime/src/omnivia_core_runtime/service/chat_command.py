"""One Chat command, settled through the one mutation seam (W2-S).

The same composition `service/runtime_command.py` is, over the Chat repository
instead of the Runtime one. Everything that makes a command a command already
exists: the caller, workspace, operation and idempotency scope come from
`authorize_application_request`; the canonical request fingerprint comes from the
contract's own `idempotency_equivalence`; the audit event, the claim, the terminal
outcome, the durable grant spend, the fence checks, the result validation and the
proved replay come from :func:`~service.mutation.execute_mutation`. This module adds
exactly two things to that: the Chat writes -- domain rows *and* the transactional
outbox row that announces them -- reach the command *inside the same fenced
transaction*, and the command may state the conversation revision it expects.

**Why a composition rather than a second executor.** A Chat command that opened its
own transaction would settle its claim in one and write its outbox row in another,
which is precisely the dual-write the transactional outbox exists to remove -- and it
would need its own copy of the grant, fence, replay and validation rules, which is the
second authority this repository has spent several slices removing. So there is one
`execute_mutation` call here and no durable statement of this module's own.

**What the callback is handed.** A :class:`~storage.chat.ChatWriter` bound to the
fenced connection the seam opened and to the workspace the *grant* names, and the
seam's own settlement identities. Every Chat row it writes and every outbox entry it
appends therefore commit with the audit, claim, outcome and execution record, or roll
back with them. The workspace is the grant's, never the request's --
`execute_mutation` has already proved the two agree by the time the callback runs, and
taking it from the grant is what keeps that true if they ever stop agreeing.

**What replay does.** Nothing. An equal request under the same caller-scoped
idempotency key is answered from the stored outcome by the seam, which never calls the
callback -- so no message is appended twice, no second outbox cursor is consumed, and
the expected revision is not re-checked. Re-checking it would be wrong rather than
merely redundant: a replay answers for the command that already ran, and the
conversation it changed has moved on since.

**What this module deliberately does not do.** It registers no operation and adds no
public wire surface: the Application Contract catalogue is frozen, and the caller
brings its own authorized context and grant. It owns no Chat domain policy either --
which sequence a message takes, which head version a branch moves to, which outbox
cursor is next and how a conversation's `graph_revision` advances are all the command's
own decisions, held to migration 0029's guarded triggers rather than to a second set of
rules restated here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.chat_contract.v1 import (
    ChatContractDecodeError,
    CommandResultEnvelope,
)
from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_CONFLICT,
    IdempotencyEquivalence,
)
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.authorization import AuthorizedApplicationContext
from omnivia_core_runtime.service.mutation import (
    IdentifierAllocator,
    MutationGrant,
    MutationOutcome,
    MutationSettlementContext,
    PreconditionReader,
    ResultValidator,
    execute_mutation,
)
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.chat import (
    ChatWriter,
    read_conversation,
    transaction_local_writer,
)

_MESSAGE_CONVERSATION_MOVED: Final = (
    "the conversation this command names is not at the revision the request expects"
)


class ChatAggregateConflict(OperationError):
    """The conversation moved: it is not at the revision the command expected.

    An `OperationError` subclass, so a handler consuming this seam lets it out
    unchanged and the dispatcher renders it in the contract's own vocabulary. The code
    is `conflict` rather than `mutation_precondition_failed` for the same reason
    `RuntimeSequenceConflict`'s is: the catalogue's precondition is a record version a
    caller refreshes and retries, and this one says another writer advanced the
    conversation, which is a state the caller has to re-read and re-decide against.

    A conversation this workspace does not hold raises the same conflict rather than a
    separate "not found": an expectation names a revision, and a conversation that is
    absent is not at it.
    """

    def __init__(self, message: str = _MESSAGE_CONVERSATION_MOVED) -> None:
        super().__init__(
            ERROR_CODE_CONFLICT,
            message,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_CONFLICT],
        )


@dataclass(frozen=True, slots=True)
class ChatAggregateExpectation:
    """Which conversation a command changes, and the revision it must be at.

    One value rather than three arguments, because the three are only meaningful
    together: a revision without the conversation it belongs to is checked against
    nothing. A command that genuinely has no expectation -- one whose Chat writes touch
    no existing conversation -- passes no expectation rather than a value with a field
    left empty.

    Both counters, not one. `graph_revision` is the conversation graph's optimistic
    token and `latest_conversation_sequence` is its append position, and 0029 lets them
    move independently -- a command that appends a message advances the sequence while
    deliberately leaving the revision alone, because
    `omnivia_chat_branch_head_events.graph_revision` foreign-keys the conversation row
    non-deferrably and durable head events cite the value they were written under.
    Expecting only one of the two would therefore admit a command whose view of the
    conversation is stale in exactly the half it did not state.
    """

    conversation_id: str
    graph_revision: int
    latest_conversation_sequence: int


#: The Chat half of one command, run inside the mutation seam's fenced transaction with
#: the writes bound to it. Its returned mapping is the operation's result, and is
#: validated and stored by the seam exactly as any other domain mutation's is.
ChatCommand = Callable[[ChatWriter, MutationSettlementContext], Mapping[str, Any]]


def is_governed_command_result(result: Mapping[str, Any]) -> bool:
    """Whether a Chat command's result is a governed `CommandResultEnvelope`.

    A :data:`~service.mutation.ResultValidator` for the Chat commands whose result is
    the contract's own stable command envelope. `strict=True` is the closed decode: an
    unknown top-level field is refused here rather than tolerated, because this is
    emission -- what the server is about to store and later replay -- rather than a
    decode of somebody else's document.

    Returns `False` rather than raising, and says nothing about *why*: the refused
    document is the one most likely to be carrying content this contract does not want
    republished, and a boolean cannot leak it. The seam turns the `False` into its own
    `MutationSeamFault` and rolls the whole transaction back.
    """
    try:
        CommandResultEnvelope.from_wire(result, strict=True)
    except ChatContractDecodeError:
        return False
    return True


def execute_chat_command(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    grant: MutationGrant,
    context: AuthorizedApplicationContext,
    equivalence: IdempotencyEquivalence,
    command: ChatCommand,
    validate_result: ResultValidator,
    clock: Clock,
    expected: ChatAggregateExpectation | None = None,
    precondition: PreconditionReader | None = None,
    allocate_identifier: IdentifierAllocator | None = None,
) -> MutationOutcome:
    """Run one Chat command under a grant, or refuse it, leaving nothing behind.

    Every argument other than `command` and `expected` is passed to
    :func:`~service.mutation.execute_mutation` unchanged, and every rule that function
    states still holds in the order it states them. What happens in the one step it
    delegates -- the domain mutation -- is:

    1. the expected conversation revision, where one was stated, is read from the
       conversation's own row and compared. A mismatch raises
       :class:`ChatAggregateConflict` before the command is invoked, so nothing it would
       have written exists to roll back, and the audit event the seam wrote ahead of it
       rolls back with the transaction;
    2. the command is handed a writer bound to this transaction and to the grant's
       workspace, and its result is returned to the seam to be validated, stored and
       answered from on a later replay.

    The check is inside the transaction rather than before it on purpose. A revision
    read before `BEGIN IMMEDIATE` proves only that the conversation was at that revision
    before this writer held the database, which is the same defect a pre-flight fencing
    check has.
    """

    def mutate(
        fenced: sqlite3.Connection, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        # The grant's workspace, not the context's. `execute_mutation` has already
        # refused a grant that does not cover this request's workspace, so the two agree
        # here; reading the server-issued one is what keeps a request from binding the
        # writer if that ever stops being true.
        workspace_id = grant.workspace_id
        if expected is not None:
            observed = read_conversation(
                fenced,
                workspace_id=workspace_id,
                conversation_id=expected.conversation_id,
            )
            if (
                observed is None
                or observed.graph_revision != expected.graph_revision
                or observed.latest_conversation_sequence
                != expected.latest_conversation_sequence
            ):
                raise ChatAggregateConflict
        return command(
            transaction_local_writer(fenced, workspace_id=workspace_id), settlement
        )

    return execute_mutation(
        connection,
        identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        precondition=precondition,
        mutate=mutate,
        validate_result=validate_result,
        clock=clock,
        allocate_identifier=allocate_identifier,
    )


__all__ = [
    "ChatAggregateConflict",
    "ChatAggregateExpectation",
    "ChatCommand",
    "execute_chat_command",
    "is_governed_command_result",
]
