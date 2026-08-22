"""One runtime command, settled through the one mutation seam (RT-104).

The idempotent command envelope and the event-append transaction, composed rather
than reimplemented. Everything that makes a command a command already exists: the
caller, workspace, operation and idempotency scope come from
`authorize_application_request`; the canonical request fingerprint comes from the
contract's own `idempotency_equivalence`; the audit event, the claim, the terminal
outcome, the durable grant spend, the fence checks, the result validation and the
proved replay come from :func:`~service.mutation.execute_mutation`. This module adds
exactly two things to that: the runtime writes reach the command *inside the same
fenced transaction*, and the command may state the sequence it expects the run's
stream to be at.

**Why a composition rather than a second executor.** A runtime command that opened
its own transaction would settle its claim in one and append its events in another,
which is the divergence 0018's claim binding exists to prevent -- and it would need
its own copy of the grant, fence, replay and validation rules, which is the second
authority this repository has spent several slices removing. So there is one
`execute_mutation` call here and no durable statement of this module's own.

**What the callback is handed.** A :class:`~storage.agent_runtime.RuntimeWriter`
bound to the fenced connection the seam opened and to the workspace the *grant*
names, and the seam's own settlement identities. Every runtime row it writes and
every event it appends therefore commit with the audit, claim, outcome and execution
record, or roll back with them. The workspace is the grant's, never the request's --
`execute_mutation` has already proved the two agree by the time the callback runs,
and taking it from the grant is what keeps that true if they ever stop agreeing.

**What replay does.** Nothing. An equal request under the same caller-scoped
idempotency key is answered from the stored outcome by the seam, which never calls
the callback -- so no event is appended twice and the expected sequence is not
re-checked. Re-checking it would be wrong rather than merely redundant: a replay
answers for the command that already ran, and the run it changed has moved on since.
`MutationOutcome.replayed` is the only difference, and it is the seam's existing
diagnostic rather than a disposition invented here.

**What this module deliberately does not do.** It registers no operation, adds no
public wire surface, and names no operation of its own: the catalogue is frozen and
the caller brings its own authorized context and grant. It projects nothing (RT-105),
schedules nothing (RT-106) and knows no `ResolveWait` policy (RT-107). And it does
not make a repeated `runtime_event_id` idempotent -- command replay is the
idempotency authority, so a second event claiming an identity the stream already
holds is a conflict the migration raises and this transaction rolls back on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

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
from omnivia_core_runtime.storage.agent_runtime import (
    RuntimeWriter,
    read_run_sequence,
    transaction_local_writer,
)

#: The sequence a run's stream is at before it holds any event, and therefore the
#: expectation a command that is about to admit one states. The same value
#: :func:`~storage.agent_runtime.read_run_sequence` reports for a run this workspace
#: does not hold, because a run and the sequence-zero entry opening its stream are
#: recorded by one statement pair and neither exists without the other.
NO_RUNTIME_EVENTS: Final = -1

_MESSAGE_SEQUENCE_MOVED: Final = (
    "the run this command names is not at the sequence the request expects"
)


class RuntimeSequenceConflict(OperationError):
    """The run moved: its stream is not at the sequence the command expected.

    An `OperationError` subclass, so a handler consuming this seam lets it out
    unchanged and the dispatcher renders it in the contract's own vocabulary. The
    code is `conflict` rather than `mutation_precondition_failed` because the two
    are different preconditions with different answers: the catalogue's is a record
    version a caller refreshes and retries, and this one says another writer advanced
    the run, which is a state the caller has to re-read and re-decide against.
    """

    def __init__(self, message: str = _MESSAGE_SEQUENCE_MOVED) -> None:
        super().__init__(
            ERROR_CODE_CONFLICT,
            message,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_CONFLICT],
        )


@dataclass(frozen=True, slots=True)
class RuntimeAggregateExpectation:
    """Which run a command changes, and the sequence its stream must be at.

    One value rather than two arguments, because the two are only meaningful
    together: a sequence without the run it belongs to is checked against nothing,
    and a run identifier without a sequence states no expectation at all. A command
    that genuinely has no expectation -- one whose runtime writes touch no existing
    stream -- passes no expectation rather than a pair with a field left empty.

    `sequence` is the sequence of the run's *latest* event, not the next one, so a
    run just admitted is at `0` and one that has never existed is at
    :data:`NO_RUNTIME_EVENTS`.
    """

    run_id: str
    sequence: int


#: The runtime half of one command, run inside the mutation seam's fenced transaction
#: with the writes bound to it. Its returned mapping is the operation's result, and is
#: validated and stored by the seam exactly as any other domain mutation's is.
RuntimeCommand = Callable[
    [RuntimeWriter, MutationSettlementContext], Mapping[str, Any]
]


def execute_runtime_command(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    grant: MutationGrant,
    context: AuthorizedApplicationContext,
    equivalence: IdempotencyEquivalence,
    command: RuntimeCommand,
    validate_result: ResultValidator,
    clock: Clock,
    expected: RuntimeAggregateExpectation | None = None,
    precondition: PreconditionReader | None = None,
    allocate_identifier: IdentifierAllocator | None = None,
) -> MutationOutcome:
    """Run one runtime command under a grant, or refuse it, leaving nothing behind.

    Every argument other than `command` and `expected` is passed to
    :func:`~service.mutation.execute_mutation` unchanged, and every rule that
    function states still holds in the order it states them. What happens in the one
    step it delegates -- the domain mutation -- is:

    1. the expected aggregate sequence, where one was stated, is read from the run's
       own stream and compared. A mismatch raises :class:`RuntimeSequenceConflict`
       before the command is invoked, so nothing it would have written exists to roll
       back, and the audit event the seam wrote ahead of it rolls back with the
       transaction;
    2. the command is handed a writer bound to this transaction and to the grant's
       workspace, and its result is returned to the seam to be validated, stored and
       answered from on a later replay.

    The check is inside the transaction rather than before it on purpose. A sequence
    read before `BEGIN IMMEDIATE` proves only that the run was at that sequence
    before this writer held the database, which is the same defect a pre-flight
    fencing check has.
    """

    def mutate(
        fenced: sqlite3.Connection, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        # The grant's workspace, not the context's. `execute_mutation` has already
        # refused a grant that does not cover this request's workspace, so the two
        # agree here; reading the server-issued one is what keeps a request from
        # binding the writer if that ever stops being true.
        workspace_id = grant.workspace_id
        if expected is not None:
            observed = read_run_sequence(
                fenced, workspace_id=workspace_id, run_id=expected.run_id
            )
            if observed != expected.sequence:
                raise RuntimeSequenceConflict
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
    "NO_RUNTIME_EVENTS",
    "RuntimeAggregateExpectation",
    "RuntimeCommand",
    "RuntimeSequenceConflict",
    "execute_runtime_command",
]
