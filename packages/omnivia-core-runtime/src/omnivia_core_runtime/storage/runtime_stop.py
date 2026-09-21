"""Run cancellation over migration 0025, and nothing above it.

Migration 0025 landed `omnivia_runtime_stop_requests` and
`omnivia_runtime_stop_outcomes` and then no Python at all: before this module the
two tables appeared in exactly one file in the repository, a migration test. That
made `cancelled` a run status the schema permitted, the contract's transition
table named, and nothing could ever write -- so this module is what makes
cancellation a behaviour rather than a column.

It is generic on purpose. Cancellation is a property of a canonical Runtime run,
not of a Workflow one: `omnivia_runtime_runs` already admits both
`agent_component` and `workflow` under one `definition_kind`, and a stop path
that knew the difference would be a second, quietly divergent answer to a
question 0018 already answers once.

The two refusals worth naming
-----------------------------

*A terminal run is never downgraded.* Asking a finished run to stop is not an
error and not a silent success: it settles as `ignored_already_terminal`, which
0025's own trigger will only accept when the run really is terminal. The run's
event stream is not touched, so a late cancellation cannot reopen a closed run.

*Two contenders do not mutate twice.* A stop request that has already settled
returns its stored outcome and writes nothing. The second contender for the same
run settles as `ignored_already_terminal` instead, because the first one's
`cancelled` event made the run terminal inside the same fence.

Stop progress, over migration 0042
----------------------------------

0025 answers whether a run was asked to stop and how that request settled. It does
not answer what the stop is still *waiting on*, and for a run holding material
effects that is the question a caller actually has. Migration 0042 adds the durable
half of that answer -- numbered progress observations, the unresolved effects each
observation identified, and per-stop cleanup receipts -- and the reads below fold
those rows into the contract's `RuntimeStopProjection`.

Three properties of those reads are worth stating where they are implemented.

*A projection is never invented.* A stop identifier this workspace does not hold,
and a recorded request carrying neither a progress observation nor a settled
outcome, both refuse. "I know nothing about this stop" is not reported as a stop
that is merely early.

*Pending means unresolved at the effect ledger, not unresolved here.* An obligation
row says an effect was unresolved when it was observed. Whether it still is comes
from :mod:`omnivia_core_runtime.storage.runtime_effect_head`, which walks 0024's
reconciliation links -- so a branched chain counts as pending, because a stop is
not clear of an effect nobody can say the outcome of.

*`retry_eligible` is always false here.* The contract is explicit that it reports an
owner-authorized recovery decision and never a grant inferred from cancellation,
from an empty pending-effect count, or from a settled phase. This layer holds no
such authorization, so it reports none.

Requesting a stop without settling it, over migration 0025
----------------------------------------------------------

:meth:`RuntimeStopWriter.stop_run` records a request and settles it in one breath,
which is the whole answer for a run with nothing unresolved behind it. A run holding
material effects has no such answer yet: the no-further-dispatch intent has to become
durable *before* anyone decides what may be released, and the cancellation cannot
settle until the effects do. So the two halves are separable here --
:meth:`RuntimeStopWriter.record_stop_intent` writes the 0025 request alone, and a later
:meth:`RuntimeStopWriter.stop_run` for the same request settles it. 0025 puts no trigger
on a request without an outcome, and 0042's progress rows are what keep such a request
readable rather than dangling: :func:`read_stop_projection` still refuses one carrying
neither.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from omnivia_core.contracts.v1.generated import RuntimeStopProjection
from omnivia_core.contracts.v1.semantics_runtime import (
    RUN_STATUS_CANCELLED,
    RUNTIME_STOP_CLEANUP_STATE_COMPLETED,
    RUNTIME_STOP_CLEANUP_STATE_NOT_REQUIRED,
    RUNTIME_STOP_PHASE_PENDING_RECONCILIATION,
    RUNTIME_STOP_PHASE_REQUESTED,
    RUNTIME_STOP_PHASE_SETTLED,
    RUNTIME_STOP_SETTLED_CLEANUP_STATES,
    is_terminal_run_status,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.agent_runtime import (
    read_run_sequence,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.runtime_effect_head import read_effect_heads

__all__ = [
    "CLEANUP_RECEIPT_OUTCOMES",
    "MAX_STOP_OBLIGATIONS",
    "MAX_STOP_PENDING_EFFECT_IDS",
    "STOP_OUTCOMES",
    "STOP_OUTCOME_ACCEPTED",
    "STOP_OUTCOME_IGNORED_ALREADY_TERMINAL",
    "STOP_OUTCOME_REJECTED",
    "RunStopOutcome",
    "RunStopRequest",
    "RuntimeStopWriter",
    "StopCleanupReceipt",
    "StopObligation",
    "StopProgress",
    "read_run_effect_obligations",
    "read_run_stop_outcome",
    "read_run_stop_requests",
    "read_stop_cleanup_receipts",
    "read_stop_obligations",
    "read_stop_progress",
    "read_stop_projection",
    "read_unsettled_stop_request",
    "runtime_stop_writer",
    "stop_run",
    "transaction_local_stop_writer",
]

_REQUESTS: Final = "omnivia_runtime_stop_requests"
_OUTCOMES: Final = "omnivia_runtime_stop_outcomes"
_INTENTS: Final = "omnivia_runtime_effect_intents"
_SETTLEMENTS: Final = "omnivia_runtime_effect_settlements"
_PROGRESS: Final = "omnivia_runtime_stop_progress"
_OBLIGATIONS: Final = "omnivia_runtime_stop_obligations"
_CLEANUP_RECEIPTS: Final = "omnivia_runtime_stop_cleanup_receipts"

#: The per-resource cleanup answers 0042 admits. The first three are migration
#: 0019's historical `CleanupOutcome` vocabulary; `unknown` is the fourth this
#: table adds, because `RuntimeStopCleanupState` has `uncertain` and a rolled-up
#: `uncertain` needs a resource that was actually recorded as not established.
CLEANUP_RECEIPT_OUTCOMES: Final[tuple[str, ...]] = (
    "released",
    "not_required",
    "failed",
    "unknown",
)

#: Restated from the accepted schema, which caps `pending_effect_count` at 256 and
#: `pending_effect_ids` at 128 items. 0042's own trigger refuses the 257th
#: obligation against one observation, so the count is always expressible.
MAX_STOP_OBLIGATIONS: Final = 256
MAX_STOP_PENDING_EFFECT_IDS: Final = 128

STOP_OUTCOME_ACCEPTED: Final = "accepted"
STOP_OUTCOME_IGNORED_ALREADY_TERMINAL: Final = "ignored_already_terminal"
STOP_OUTCOME_REJECTED: Final = "rejected"

STOP_OUTCOMES: Final[tuple[str, ...]] = (
    STOP_OUTCOME_ACCEPTED,
    STOP_OUTCOME_IGNORED_ALREADY_TERMINAL,
    STOP_OUTCOME_REJECTED,
)

#: The event kind a cancellation appends. Lowercase and dotted, which 0018's
#: `event_kind` guard requires.
EVENT_KIND_RUN_CANCELLED: Final = "run.cancelled"


@dataclass(frozen=True, slots=True)
class RunStopRequest:
    """One request that a run stop, exactly as migration 0025 holds it."""

    stop_request_id: str
    run_id: str
    requested_at_us: int
    requested_by: str
    reason: str
    audit_ref: str


@dataclass(frozen=True, slots=True)
class RunStopOutcome:
    """How one stop request settled.

    `runtime_event_sequence` is the sequence of the `cancelled` event this stop
    appended, and is present for an `accepted` outcome alone -- 0025 refuses an
    accepted outcome that names no event, and refuses any other outcome that
    names one.
    """

    stop_request_id: str
    outcome: str
    completed_at_us: int
    runtime_event_sequence: int | None
    reason: str
    audit_ref: str


@dataclass(frozen=True, slots=True)
class StopObligation:
    """One effect a stop was observed to be blocked on, exactly as 0042 holds it.

    `effect_settlement_id` is the `unknown` settlement that was the effect's answer
    at the moment of observation. It is a starting point for the chain walk, not a
    verdict: 0024 may already have superseded it by the time anyone reads.
    """

    effect_intent_id: str
    effect_settlement_id: str


@dataclass(frozen=True, slots=True)
class StopProgress:
    """One numbered observation of where a stop request stands."""

    stop_progress_id: str
    stop_request_id: str
    progress_number: int
    observed_at_us: int
    cleanup_required: bool
    reason: str
    audit_ref: str


@dataclass(frozen=True, slots=True)
class StopCleanupReceipt:
    """What cleanup for one stop achieved for one resource."""

    stop_cleanup_receipt_id: str
    stop_request_id: str
    resource_kind: str
    outcome: str
    performed_at_us: int
    reason: str
    audit_ref: str


@dataclass(frozen=True, slots=True)
class RuntimeStopWriter:
    """The stop writes, issued into a transaction that is already open."""

    connection: sqlite3.Connection
    workspace_id: str

    def record_progress(
        self,
        *,
        stop_progress_id: str,
        stop_request_id: str,
        observed_at_us: int,
        cleanup_required: bool,
        reason: str,
        audit_ref: str,
        obligations: Sequence[StopObligation] = (),
    ) -> StopProgress:
        """Append one observation of a stop's progress, with what it found pending.

        The observation's number is allocated here rather than taken as an argument.
        0042 requires it to be contiguous from 1 per stop request, and a caller that
        could name its own would be choosing where in a durable history its
        observation sits -- including over the top of one already recorded.

        The observation and its obligations land together or neither does. An
        observation with its obligation rows missing would read as a stop that found
        nothing pending, which is the one reading this ledger exists to prevent.
        """
        recorded = StopProgress(
            stop_progress_id=stop_progress_id,
            stop_request_id=stop_request_id,
            progress_number=_next_progress_number(
                self.connection, self.workspace_id, stop_request_id
            ),
            observed_at_us=observed_at_us,
            cleanup_required=cleanup_required,
            reason=reason,
            audit_ref=audit_ref,
        )
        self.connection.execute(
            f"INSERT INTO {_PROGRESS} (workspace_id, stop_progress_id, stop_request_id, "
            "progress_number, observed_at_us, cleanup_required, reason, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                recorded.stop_progress_id,
                recorded.stop_request_id,
                recorded.progress_number,
                recorded.observed_at_us,
                int(recorded.cleanup_required),
                recorded.reason,
                recorded.audit_ref,
            ),
        )
        for obligation in obligations:
            self.connection.execute(
                f"INSERT INTO {_OBLIGATIONS} (workspace_id, stop_progress_id, "
                "effect_intent_id, effect_settlement_id) VALUES (?, ?, ?, ?)",
                (
                    self.workspace_id,
                    recorded.stop_progress_id,
                    obligation.effect_intent_id,
                    obligation.effect_settlement_id,
                ),
            )
        return recorded

    def record_cleanup_receipt(self, receipt: StopCleanupReceipt) -> StopCleanupReceipt:
        """Append what cleanup for one stop achieved for one resource.

        Written for the attempt rather than for the success, as 0019's receipts are:
        a release that failed, or one whose result could not be established, is a row
        rather than a silence indistinguishable from cleanup that never ran.
        """
        self.connection.execute(
            f"INSERT INTO {_CLEANUP_RECEIPTS} (workspace_id, stop_cleanup_receipt_id, "
            "stop_request_id, resource_kind, outcome, performed_at_us, reason, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                receipt.stop_cleanup_receipt_id,
                receipt.stop_request_id,
                receipt.resource_kind,
                receipt.outcome,
                receipt.performed_at_us,
                receipt.reason,
                receipt.audit_ref,
            ),
        )
        return receipt

    def record_stop_intent(self, request: RunStopRequest) -> RunStopRequest:
        """Record that a run was asked to stop, and settle nothing.

        The durable half of "no further dispatch for this run", written before anyone
        decides what may be released. A caller holding material effects it cannot yet
        account for needs exactly this and nothing more: an outcome written here would
        state how a cancellation ended that has not ended.

        Replay is decided on the stored request in every field, as
        :meth:`stop_run` decides it and for the same reason -- a `stop_request_id` is a
        caller-minted string, and accepting a second, different request under one is a
        stop of some other run wearing this one's name.
        """
        recorded = _read_stop_request(
            self.connection, self.workspace_id, request.stop_request_id
        )
        if recorded is not None:
            if recorded != request:
                raise StorageError(
                    f"stop request {request.stop_request_id!r} was already recorded "
                    "on different terms"
                )
            return recorded
        if _latest_run_status(self.connection, self.workspace_id, request.run_id) is None:
            raise StorageError(f"run {request.run_id!r} is not a run of this workspace")
        self.connection.execute(
            f"INSERT INTO {_REQUESTS} (workspace_id, stop_request_id, run_id, "
            "requested_at_us, requested_by, reason, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                request.stop_request_id,
                request.run_id,
                request.requested_at_us,
                request.requested_by,
                request.reason,
                request.audit_ref,
            ),
        )
        return request

    def stop_run(
        self,
        request: RunStopRequest,
        *,
        runtime_event_id: str,
        occurred_at_us: int,
        completed_at_us: int,
        outcome_audit_ref: str | None = None,
    ) -> RunStopOutcome:
        """Request one run's cancellation and settle it, in the caller's transaction.

        The outcome is decided here rather than taken as an argument. A caller
        that could name its own outcome could report `accepted` for a run that
        never cancelled, which is the one answer this ledger exists to prevent.

        A replay is decided on the stored *request*, not on its identifier. A
        `stop_request_id` is a caller-minted string, and answering the second use of
        one with the first one's outcome would report a cancellation of the run the
        first request named, attributed to the actor and reason the first one gave, to
        a caller who asked about something else. So the recorded request is read back
        and must be the same request in every field; anything else refuses before a
        statement is issued, leaving no new request, no new outcome and no event.

        A request already carrying an outcome returns that outcome and writes nothing,
        which is what makes settlement single-shot however many callers reach it. A
        request recorded by :meth:`record_stop_intent` and *not* yet settled is the
        other case this reaches, and settling it is the whole point: the stop was
        recorded when nobody could say how it would end, and this is the call that says
        so once the obligations behind it have closed.
        """
        recorded = self.record_stop_intent(request)
        settled = read_run_stop_outcome(
            self.connection,
            workspace_id=self.workspace_id,
            stop_request_id=recorded.stop_request_id,
        )
        if settled is not None:
            return settled

        status = _latest_run_status(self.connection, self.workspace_id, request.run_id)
        if status is None:  # pragma: no cover - record_stop_intent proved the run
            raise StorageError(f"run {request.run_id!r} is not a run of this workspace")

        sequence: int | None = None
        if is_terminal_run_status(status):
            outcome = STOP_OUTCOME_IGNORED_ALREADY_TERMINAL
        else:
            outcome = STOP_OUTCOME_ACCEPTED
            sequence = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            ).append_run_event(
                run_id=request.run_id,
                runtime_event_id=runtime_event_id,
                occurred_at_us=occurred_at_us,
                event_kind=EVENT_KIND_RUN_CANCELLED,
                run_status=RUN_STATUS_CANCELLED,
                message=request.reason,
            )

        settlement = RunStopOutcome(
            stop_request_id=request.stop_request_id,
            outcome=outcome,
            completed_at_us=completed_at_us,
            runtime_event_sequence=sequence,
            reason=request.reason,
            audit_ref=outcome_audit_ref or request.audit_ref,
        )
        self.connection.execute(
            f"INSERT INTO {_OUTCOMES} (workspace_id, stop_request_id, outcome, "
            "completed_at_us, runtime_event_sequence, reason, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                settlement.stop_request_id,
                settlement.outcome,
                settlement.completed_at_us,
                settlement.runtime_event_sequence,
                settlement.reason,
                settlement.audit_ref,
            ),
        )
        return settlement


def transaction_local_stop_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> RuntimeStopWriter:
    """The stop writes, for a caller that already holds a fenced transaction."""
    return RuntimeStopWriter(connection, workspace_id)


@contextmanager
def runtime_stop_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[RuntimeStopWriter]:
    """One fenced transaction, and the stop writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_stop_writer(connection, workspace_id=workspace_id)


def stop_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    request: RunStopRequest,
    runtime_event_id: str,
    occurred_at_us: int,
    completed_at_us: int,
    outcome_audit_ref: str | None = None,
) -> RunStopOutcome:
    """Cancel one run in its own fenced transaction."""
    with runtime_stop_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.stop_run(
            request,
            runtime_event_id=runtime_event_id,
            occurred_at_us=occurred_at_us,
            completed_at_us=completed_at_us,
            outcome_audit_ref=outcome_audit_ref,
        )


def read_run_stop_outcome(
    connection: sqlite3.Connection, *, workspace_id: str, stop_request_id: str
) -> RunStopOutcome | None:
    """How one stop request settled, or `None` when it has not."""
    row = connection.execute(
        "SELECT outcome, completed_at_us, runtime_event_sequence, reason, audit_ref "
        f"FROM {_OUTCOMES} WHERE workspace_id = ? AND stop_request_id = ?",
        (workspace_id, stop_request_id),
    ).fetchone()
    if row is None:
        return None
    return RunStopOutcome(
        stop_request_id=stop_request_id,
        outcome=str(row[0]),
        completed_at_us=int(row[1]),
        runtime_event_sequence=None if row[2] is None else int(row[2]),
        reason=str(row[3]),
        audit_ref=str(row[4]),
    )


def read_run_stop_requests(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[RunStopRequest, ...]:
    """Every stop recorded against one run, oldest first."""
    return tuple(
        RunStopRequest(
            stop_request_id=str(row[0]),
            run_id=run_id,
            requested_at_us=int(row[1]),
            requested_by=str(row[2]),
            reason=str(row[3]),
            audit_ref=str(row[4]),
        )
        for row in connection.execute(
            "SELECT stop_request_id, requested_at_us, requested_by, reason, audit_ref "
            f"FROM {_REQUESTS} WHERE workspace_id = ? AND run_id = ? "
            "ORDER BY requested_at_us, stop_request_id",
            (workspace_id, run_id),
        )
    )


def read_unsettled_stop_request(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> RunStopRequest | None:
    """The one stop this run was asked for and that has not settled, if there is one.

    A second cancellation of a run already under an unsettled stop is a *reconciliation*
    of that stop rather than a new one, so the caller needs to find it before minting an
    identifier of its own. Two unsettled stops for one run would be two answers to "is
    this run stopping", so meeting two refuses rather than picking the older.
    """
    open_requests = tuple(
        request
        for request in read_run_stop_requests(
            connection, workspace_id=workspace_id, run_id=run_id
        )
        if read_run_stop_outcome(
            connection,
            workspace_id=workspace_id,
            stop_request_id=request.stop_request_id,
        )
        is None
    )
    if len(open_requests) > 1:
        raise StorageError(
            f"run {run_id!r} carries {len(open_requests)} unsettled stop requests; at "
            "most one may be open"
        )
    return open_requests[0] if open_requests else None


def read_run_effect_obligations(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[StopObligation, ...]:
    """Every material effect of one run that a stop is still blocked on, right now.

    Read from the effect ledger rather than from any stop row, because whether an effect
    is still open is 0023 and 0024's question and they answer it once. Each of this run's
    intents is walked to its current head by
    :mod:`omnivia_core_runtime.storage.runtime_effect_head`, and three readings come back.

    *Settled.* `committed` or `not_committed` is an answer, and an answered effect is not
    an obligation. It is left out.

    *Branched.* Two ends that disagree about whether a real external effect happened. It
    is an obligation and stays one: the head reader never names a head for it, so no
    later read can retire it by picking an arm, and nothing here picks one either.

    *Unresolved.* The chain ends at an `unknown` settlement -- the effect may be in the
    world -- so it is an obligation. An intent carrying *no* settlement at all is the one
    unresolved reading that is not: 0023's intent row is the predeclaration, and the
    `unknown` settlement is what records that something was issued against it, so an
    intent with neither is an effect that was declared and demonstrably never left. It is
    also the one case 0042 could not hold if it were: an obligation row must name an
    `unknown` settlement of its own intent, and this intent has none to name.

    The settlement each obligation names is therefore always an `unknown` one: the head
    itself where the head is unknown, and otherwise the lowest-numbered `unknown`
    settlement the intent carries, which a branch always has -- 0024 supersedes nothing
    else. Chosen by identifier and never by instant, for the reason the head reader
    states.
    """
    intents = tuple(
        str(row[0])
        for row in connection.execute(
            f"SELECT effect_intent_id FROM {_INTENTS} "
            "WHERE workspace_id = ? AND run_id = ? ORDER BY effect_intent_id",
            (workspace_id, run_id),
        )
    )
    if not intents:
        return ()
    heads = read_effect_heads(
        connection, workspace_id=workspace_id, effect_intent_ids=intents
    )
    open_intents = [intent for intent in intents if not heads[intent].settled]
    if not open_intents:
        return ()

    unknown: dict[str, str] = {}
    placeholders = ", ".join("?" for _ in open_intents)
    for row in connection.execute(
        "SELECT effect_intent_id, MIN(effect_settlement_id) "
        f"FROM {_SETTLEMENTS} WHERE workspace_id = ? "
        f"AND effect_intent_id IN ({placeholders}) AND outcome = 'unknown' "
        "GROUP BY effect_intent_id",
        (workspace_id, *open_intents),
    ):
        unknown[str(row[0])] = str(row[1])

    obligations = []
    for intent in open_intents:
        head = heads[intent]
        settlement = (
            head.effect_settlement_id
            if head.outcome == "unknown" and head.effect_settlement_id is not None
            else unknown.get(intent)
        )
        if settlement is None:
            # Predeclared and never issued. See the docstring: not an obligation, and
            # not a row 0042 would admit either.
            continue
        obligations.append(
            StopObligation(effect_intent_id=intent, effect_settlement_id=settlement)
        )
    if len(obligations) > MAX_STOP_OBLIGATIONS:
        # 0042 refuses the 257th obligation against one observation and the contract caps
        # `pending_effect_count` at the same 256. Recording as many as fit would publish a
        # count that understates what this run is actually blocked on, so it refuses.
        raise StorageError(
            f"run {run_id!r} is blocked on {len(obligations)} unresolved effects, which "
            f"is more than the {MAX_STOP_OBLIGATIONS} one stop observation may record"
        )
    return tuple(obligations)


def read_stop_progress(
    connection: sqlite3.Connection, *, workspace_id: str, stop_request_id: str
) -> tuple[StopProgress, ...]:
    """Every observation recorded for one stop, oldest first."""
    return tuple(
        StopProgress(
            stop_progress_id=str(row[0]),
            stop_request_id=stop_request_id,
            progress_number=int(row[1]),
            observed_at_us=int(row[2]),
            cleanup_required=bool(row[3]),
            reason=str(row[4]),
            audit_ref=str(row[5]),
        )
        for row in connection.execute(
            "SELECT stop_progress_id, progress_number, observed_at_us, "
            f"cleanup_required, reason, audit_ref FROM {_PROGRESS} "
            "WHERE workspace_id = ? AND stop_request_id = ? ORDER BY progress_number",
            (workspace_id, stop_request_id),
        )
    )


def read_stop_obligations(
    connection: sqlite3.Connection, *, workspace_id: str, stop_progress_id: str
) -> tuple[StopObligation, ...]:
    """The unresolved effects one observation identified, in identifier order.

    Ordered by `effect_intent_id` rather than by any instant, because this ordering
    decides which identifiers a bounded `pending_effect_ids` carries and which it
    truncates away. A wall-clock ordering would make that selection depend on a
    value no invariant pins.
    """
    return tuple(
        StopObligation(
            effect_intent_id=str(row[0]), effect_settlement_id=str(row[1])
        )
        for row in connection.execute(
            f"SELECT effect_intent_id, effect_settlement_id FROM {_OBLIGATIONS} "
            "WHERE workspace_id = ? AND stop_progress_id = ? ORDER BY effect_intent_id",
            (workspace_id, stop_progress_id),
        )
    )


def read_stop_cleanup_receipts(
    connection: sqlite3.Connection, *, workspace_id: str, stop_request_id: str
) -> tuple[StopCleanupReceipt, ...]:
    """Every cleanup receipt recorded for one stop, oldest first."""
    return tuple(
        StopCleanupReceipt(
            stop_cleanup_receipt_id=str(row[0]),
            stop_request_id=stop_request_id,
            resource_kind=str(row[1]),
            outcome=str(row[2]),
            performed_at_us=int(row[3]),
            reason=str(row[4]),
            audit_ref=str(row[5]),
        )
        for row in connection.execute(
            "SELECT stop_cleanup_receipt_id, resource_kind, outcome, performed_at_us, "
            f"reason, audit_ref FROM {_CLEANUP_RECEIPTS} "
            "WHERE workspace_id = ? AND stop_request_id = ? "
            "ORDER BY performed_at_us, stop_cleanup_receipt_id",
            (workspace_id, stop_request_id),
        )
    )


def read_stop_projection(
    connection: sqlite3.Connection, *, workspace_id: str, stop_request_id: str
) -> RuntimeStopProjection:
    """Truthful progress of one recorded stop, as the contract carries it.

    Two refusals rather than a fabricated projection. A `stop_request_id` this
    workspace holds no request for refuses, because reporting `requested` for it
    would state that a stop exists. A request holding neither a progress
    observation nor a settled 0025 outcome refuses too: nothing in the workspace
    says anything about it beyond its own existence, and the phase `requested`
    means "recorded, and nothing further is yet known" -- a claim that at least
    the recording was completed, which is what the outcome row evidences.

    Everything else is read rather than assumed. The phase is `settled` only where
    no obligation is still pending *at the effect ledger* and cleanup has resolved,
    and `retry_eligible` is false in every case -- see the module docstring.
    """
    request = _read_stop_request(connection, workspace_id, stop_request_id)
    if request is None:
        raise StorageError(
            f"stop request {stop_request_id!r} is not a stop request of this workspace"
        )

    observations = read_stop_progress(
        connection, workspace_id=workspace_id, stop_request_id=stop_request_id
    )
    if not observations and (
        read_run_stop_outcome(
            connection, workspace_id=workspace_id, stop_request_id=stop_request_id
        )
        is None
    ):
        raise StorageError(
            f"stop request {stop_request_id!r} is recorded with neither progress nor "
            "an outcome"
        )

    receipts = read_stop_cleanup_receipts(
        connection, workspace_id=workspace_id, stop_request_id=stop_request_id
    )

    if not observations:
        # Recorded and settled by 0025, and nothing has looked at what it is waiting
        # on. Cleanup is read from whatever receipts exist; with none, nothing about
        # cleanup has been established, which is what `uncertain` is for.
        return RuntimeStopProjection(
            stop_request_id=stop_request_id,
            phase=RUNTIME_STOP_PHASE_REQUESTED,
            requested_at=_timestamp(request.requested_at_us),
            request_audit_ref=request.audit_ref,
            pending_effect_count=0,
            pending_effect_ids=(),
            pending_effects_truncated=False,
            retry_eligible=False,
            cleanup_state=_cleanup_state(None, receipts),
        )

    latest = observations[-1]
    obligations = read_stop_obligations(
        connection, workspace_id=workspace_id, stop_progress_id=latest.stop_progress_id
    )
    heads = read_effect_heads(
        connection,
        workspace_id=workspace_id,
        effect_intent_ids=[o.effect_intent_id for o in obligations],
    )
    pending = tuple(
        obligation.effect_intent_id
        for obligation in obligations
        if not heads[obligation.effect_intent_id].settled
    )
    cleanup_state = _cleanup_state(latest.cleanup_required, receipts)
    resolved = not pending and cleanup_state in RUNTIME_STOP_SETTLED_CLEANUP_STATES

    return RuntimeStopProjection(
        stop_request_id=stop_request_id,
        phase=(
            RUNTIME_STOP_PHASE_SETTLED
            if resolved
            else RUNTIME_STOP_PHASE_PENDING_RECONCILIATION
        ),
        requested_at=_timestamp(request.requested_at_us),
        request_audit_ref=request.audit_ref,
        pending_effect_count=len(pending),
        pending_effect_ids=pending[:MAX_STOP_PENDING_EFFECT_IDS],
        pending_effects_truncated=len(pending) > MAX_STOP_PENDING_EFFECT_IDS,
        retry_eligible=False,
        cleanup_state=cleanup_state,
    )


def _cleanup_state(
    cleanup_required: bool | None, receipts: Sequence[StopCleanupReceipt]
) -> str:
    """One stop's rolled-up cleanup progress, from its receipts and nothing else.

    `cleanup_required` is the latest observation's own answer to "was there anything
    to free", or `None` where no observation has been recorded. It is what keeps an
    empty receipt set readable: nothing to free reads as `not_required`, cleanup
    that was asked for and has reported nothing back reads as `requested`, and a
    stop nobody has looked at reads as `uncertain` rather than as either.

    `unknown` dominates every other receipt. A resource whose release could not be
    established leaves the aggregate unestablished too, and reporting `failed` or
    `partial` over it would state something about that resource which no receipt
    says. That is the same refusal the effect-head reader makes for a branched
    chain, for the same reason.
    """
    outcomes = {receipt.outcome for receipt in receipts}
    if not outcomes:
        if cleanup_required is None:
            return "uncertain"
        return "requested" if cleanup_required else RUNTIME_STOP_CLEANUP_STATE_NOT_REQUIRED
    if "unknown" in outcomes:
        return "uncertain"
    if "failed" in outcomes:
        return "partial" if "released" in outcomes else "failed"
    if "released" in outcomes:
        return RUNTIME_STOP_CLEANUP_STATE_COMPLETED
    return RUNTIME_STOP_CLEANUP_STATE_NOT_REQUIRED


def _next_progress_number(
    connection: sqlite3.Connection, workspace_id: str, stop_request_id: str
) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX(progress_number), 0) + 1 FROM {_PROGRESS} "
        "WHERE workspace_id = ? AND stop_request_id = ?",
        (workspace_id, stop_request_id),
    ).fetchone()
    return int(row[0])


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, which is what the contract's pattern
    accepts and what every other application-facing timestamp this service emits
    already looks like.
    """
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _read_stop_request(
    connection: sqlite3.Connection, workspace_id: str, stop_request_id: str
) -> RunStopRequest | None:
    """The request recorded under one identifier, or `None` when there is none.

    Every column, because every one of them is request identity: which run was asked
    to stop, when, by whom, for what stated reason, and under which audit reference.
    A replay that agreed on the identifier but not on these is a different request
    wearing the same name.
    """
    row = connection.execute(
        f"SELECT run_id, requested_at_us, requested_by, reason, audit_ref "
        f"FROM {_REQUESTS} WHERE workspace_id = ? AND stop_request_id = ?",
        (workspace_id, stop_request_id),
    ).fetchone()
    if row is None:
        return None
    return RunStopRequest(
        stop_request_id=stop_request_id,
        run_id=str(row[0]),
        requested_at_us=int(row[1]),
        requested_by=str(row[2]),
        reason=str(row[3]),
        audit_ref=str(row[4]),
    )


def _latest_run_status(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> str | None:
    """The status this run's stream is at, or `None` when it holds no event.

    Read through `read_run_sequence` rather than a second `MAX(sequence)` of its
    own, so the number this decision rests on is the same one
    `append_run_event` will allocate its successor from.
    """
    sequence = read_run_sequence(connection, workspace_id=workspace_id, run_id=run_id)
    if sequence < 0:
        return None
    row = connection.execute(
        "SELECT run_status FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? AND sequence = ?",
        (workspace_id, run_id, sequence),
    ).fetchone()
    return None if row is None else str(row[0])
