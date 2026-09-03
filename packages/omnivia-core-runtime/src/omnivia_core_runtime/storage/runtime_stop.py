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
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1.semantics_runtime import (
    RUN_STATUS_CANCELLED,
    is_terminal_run_status,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.agent_runtime import (
    read_run_sequence,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "STOP_OUTCOMES",
    "STOP_OUTCOME_ACCEPTED",
    "STOP_OUTCOME_IGNORED_ALREADY_TERMINAL",
    "STOP_OUTCOME_REJECTED",
    "RunStopOutcome",
    "RunStopRequest",
    "RuntimeStopWriter",
    "read_run_stop_outcome",
    "runtime_stop_writer",
    "stop_run",
    "transaction_local_stop_writer",
]

_REQUESTS: Final = "omnivia_runtime_stop_requests"
_OUTCOMES: Final = "omnivia_runtime_stop_outcomes"

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
class RuntimeStopWriter:
    """The stop writes, issued into a transaction that is already open."""

    connection: sqlite3.Connection
    workspace_id: str

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

        The request and its outcome land together or neither does, because a
        recorded request with no outcome is a run nobody can tell the state of:
        it was asked to stop, and whether it did is unrecorded.

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
            settled = read_run_stop_outcome(
                self.connection,
                workspace_id=self.workspace_id,
                stop_request_id=request.stop_request_id,
            )
            if settled is None:  # pragma: no cover - 0025 forbids reaching this
                # Both rows land in one transaction and 0025 refuses a DELETE on
                # either table, so a request without its outcome is unreachable. If it
                # were reached, the honest answer to "did the run stop?" is a refusal
                # rather than a fresh attempt that would collide with the request row
                # already there.
                raise StorageError(
                    f"stop request {request.stop_request_id!r} is recorded with no "
                    "outcome"
                )
            return settled

        status = _latest_run_status(self.connection, self.workspace_id, request.run_id)
        if status is None:
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
