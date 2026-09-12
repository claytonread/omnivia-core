"""T-0693 acceptance for run cancellation over migration 0025.

Before `storage/runtime_stop.py` the two stop tables appeared in exactly one file
in this repository -- a migration test -- so `cancelled` was a status the schema
allowed, the contract's transition table named, and nothing could write. These
tests hold the writer to the four properties that make it a cancellation rather
than a status assignment.

*It enters its own fenced transaction.* Every write is called on a bare owned
connection, and a stale generation leaves the database exactly as it found it.

*A terminal run is never downgraded.* Asking a finished run to stop settles as
`ignored_already_terminal` and appends no event, so a late cancellation cannot
reopen a closed run.

*Two contenders do not mutate twice.* Replaying one stop request returns its
stored outcome; a second, different request for the same run finds it already
terminal.

*An accepted outcome names the event that made it true.* 0025 refuses an accepted
outcome carrying no `runtime_event_sequence`, so the ledger cannot claim a
cancellation that never reached the run's stream.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    read_run_sequence,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.runtime_stop import (
    STOP_OUTCOME_ACCEPTED,
    STOP_OUTCOME_IGNORED_ALREADY_TERMINAL,
    RunStopRequest,
    read_run_stop_outcome,
    stop_run,
)

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
RUN_ID = m27.RUN_ID
BASE_US = m27.BASE_US

REQUESTS = "omnivia_runtime_stop_requests"
OUTCOMES = "omnivia_runtime_stop_outcomes"
EVENTS = "omnivia_runtime_events"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def event(holder: m1.Owned, sequence: int, kind: str, status: str) -> None:
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=RUN_ID,
        runtime_event_id=f"evt-{sequence:04d}",
        occurred_at_us=BASE_US + 1_000 * (sequence + 1),
        event_kind=kind,
        run_status=status,
    )


def admitted_run(holder: m1.Owned) -> None:
    """A canonical Runtime run with its stream opened, and its audit reference."""
    with m27.guarded(holder):
        m27.audit(holder, "aud-job-run-0001")
    m27.seed_runtime_run(holder)
    event(holder, 0, "run_admitted", "admitted")


def request(**overrides: Any) -> RunStopRequest:
    values: dict[str, Any] = {
        "stop_request_id": "stop-0001",
        "run_id": RUN_ID,
        "requested_at_us": BASE_US + 5_000,
        "requested_by": "core-operator",
        "reason": "operator.cancelled",
        "audit_ref": "aud-job-run-0001",
    }
    values.update(overrides)
    return RunStopRequest(**values)


def stop(
    holder: m1.Owned, record: RunStopRequest | None = None, **overrides: Any
) -> Any:
    values: dict[str, Any] = {
        "runtime_event_id": "evt-stop-0001",
        "occurred_at_us": BASE_US + 6_000,
        "completed_at_us": BASE_US + 7_000,
    }
    values.update(overrides)
    return stop_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        request=record or request(),
        **values,
    )


def count(holder: m1.Owned, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def status_of(holder: m1.Owned) -> str:
    sequence = read_run_sequence(
        holder.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    row = holder.connection.execute(
        f"SELECT run_status FROM {EVENTS} WHERE workspace_id = ? AND run_id = ? "
        "AND sequence = ?",
        (WORKSPACE_ID, RUN_ID, sequence),
    ).fetchone()
    return str(row[0])


# --- an accepted cancellation --------------------------------------------------------


def test_cancelling_a_live_run_appends_the_event_the_outcome_names(
    owned: m1.Owned,
) -> None:
    admitted_run(owned)

    outcome = stop(owned)

    assert outcome.outcome == STOP_OUTCOME_ACCEPTED
    assert outcome.runtime_event_sequence == 1
    assert status_of(owned) == "cancelled"
    assert count(owned, REQUESTS) == 1
    assert count(owned, OUTCOMES) == 1


def test_the_settled_outcome_is_readable_afterwards(owned: m1.Owned) -> None:
    admitted_run(owned)
    outcome = stop(owned)

    assert (
        read_run_stop_outcome(
            owned.connection, workspace_id=WORKSPACE_ID, stop_request_id="stop-0001"
        )
        == outcome
    )


def test_a_running_run_is_cancellable(owned: m1.Owned) -> None:
    admitted_run(owned)
    event(owned, 1, "run_running", "running")

    assert stop(owned).outcome == STOP_OUTCOME_ACCEPTED
    assert status_of(owned) == "cancelled"


# --- terminal runs are never downgraded ----------------------------------------------


@pytest.mark.parametrize(
    "terminal", ("succeeded", "failed", "cancelled", "partially_completed")
)
def test_a_terminal_run_settles_as_ignored_and_its_stream_is_untouched(
    owned: m1.Owned, terminal: str
) -> None:
    admitted_run(owned)
    event(owned, 1, "run_running", "running")
    event(owned, 2, f"run_{terminal}", terminal)
    before = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    outcome = stop(owned)

    assert outcome.outcome == STOP_OUTCOME_IGNORED_ALREADY_TERMINAL
    # 0025 refuses any outcome but `accepted` that names an event, and refuses an
    # `ignored` one unless the run really is terminal. Both are asserted by the
    # write having succeeded at all.
    assert outcome.runtime_event_sequence is None
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == before
    )
    assert status_of(owned) == terminal


def test_an_uncertain_run_is_still_cancellable(owned: m1.Owned) -> None:
    """`uncertain` is not terminal, so a stop is accepted rather than ignored."""
    admitted_run(owned)
    event(owned, 1, "run_running", "running")
    event(owned, 2, "run_uncertain", "uncertain")

    assert stop(owned).outcome == STOP_OUTCOME_ACCEPTED
    assert status_of(owned) == "cancelled"


# --- contenders ----------------------------------------------------------------------


def test_replaying_one_stop_request_returns_its_outcome_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    admitted_run(owned)
    first = stop(owned)

    # A crash-retry reads its own clock and mints its own event id; neither wins
    # over what already settled.
    again = stop(
        owned,
        runtime_event_id="evt-stop-9999",
        occurred_at_us=BASE_US + 9_000,
        completed_at_us=BASE_US + 9_500,
    )

    assert again == first
    assert count(owned, REQUESTS) == 1
    assert count(owned, OUTCOMES) == 1


def test_a_second_contender_for_the_same_run_does_not_cancel_it_twice(
    owned: m1.Owned,
) -> None:
    admitted_run(owned)
    first = stop(owned)
    sequence_after_first = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    second = stop(
        owned,
        request(stop_request_id="stop-0002", requested_at_us=BASE_US + 8_000),
        runtime_event_id="evt-stop-0002",
        occurred_at_us=BASE_US + 8_500,
        completed_at_us=BASE_US + 8_900,
    )

    assert first.outcome == STOP_OUTCOME_ACCEPTED
    assert second.outcome == STOP_OUTCOME_IGNORED_ALREADY_TERMINAL
    assert second.runtime_event_sequence is None
    # Two requests, two outcomes, one cancellation.
    assert count(owned, REQUESTS) == 2
    assert count(owned, OUTCOMES) == 2
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == sequence_after_first
    )


# --- authority and unknown runs ------------------------------------------------------


def test_a_stale_generation_cancels_nothing(owned: m1.Owned) -> None:
    admitted_run(owned)
    before = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    with pytest.raises(StaleGeneration):
        stop_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            request=request(),
            runtime_event_id="evt-stop-0001",
            occurred_at_us=BASE_US + 6_000,
            completed_at_us=BASE_US + 7_000,
        )

    assert count(owned, REQUESTS) == 0
    assert count(owned, OUTCOMES) == 0
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == before
    )
    assert status_of(owned) == "admitted"


def test_a_run_this_workspace_does_not_hold_is_refused_without_mutation(
    owned: m1.Owned,
) -> None:
    admitted_run(owned)

    with pytest.raises(StorageError, match="not a run of this workspace"):
        stop(owned, request(stop_request_id="stop-0003", run_id="run-nobody-admitted"))

    assert count(owned, REQUESTS) == 0
    assert count(owned, OUTCOMES) == 0


def test_a_foreign_workspace_neither_reads_nor_cancels_the_run(
    owned: m1.Owned,
) -> None:
    admitted_run(owned)
    stop(owned)

    assert (
        read_run_stop_outcome(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            stop_request_id="stop-0001",
        )
        is None
    )


def test_an_outcome_may_not_be_amended_after_it_settled(owned: m1.Owned) -> None:
    """0025 is append-only; the ledger records what happened, not what is convenient."""
    admitted_run(owned)
    stop(owned)

    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        m27.guarded(owned),
    ):
        owned.connection.execute(
            f"UPDATE {OUTCOMES} SET outcome = 'rejected' WHERE stop_request_id = ?",
            ("stop-0001",),
        )
    assert (
        read_run_stop_outcome(
            owned.connection, workspace_id=WORKSPACE_ID, stop_request_id="stop-0001"
        ).outcome  # type: ignore[union-attr]
        == STOP_OUTCOME_ACCEPTED
    )


# --- a reused stop request identifier ------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    (
        {"run_id": "run-other-0001"},
        {"requested_by": "someone-else"},
        {"reason": "operator.superseded"},
        {"audit_ref": "aud-job-run-0002"},
        {"requested_at_us": BASE_US + 5_001},
    ),
    ids=("run", "requester", "reason", "audit", "instant"),
)
def test_a_reused_stop_request_id_on_different_terms_writes_nothing(
    owned: m1.Owned, overrides: dict[str, Any]
) -> None:
    """A stop identifier is a caller-minted string, so it is not the request.

    Answering the second use of one with the first one's outcome would report a
    cancellation of the run the first request named, attributed to the actor and
    reason the first one gave, to a caller that asked about something else. Every
    field is identity, the requesting instant included: a replay that reads its own
    clock for `requested_at_us` is stating a different request, not retrying one.
    """
    admitted_run(owned)
    first = stop(owned)
    sequence_after_first = read_run_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )

    with pytest.raises(StorageError, match="different terms"):
        stop(
            owned,
            request(**overrides),
            runtime_event_id="evt-stop-9999",
            occurred_at_us=BASE_US + 9_000,
            completed_at_us=BASE_US + 9_500,
        )

    # No new request, no new outcome, no new Runtime event, and the one outcome
    # that did settle still says exactly what it said.
    assert count(owned, REQUESTS) == 1
    assert count(owned, OUTCOMES) == 1
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == sequence_after_first
    )
    assert (
        read_run_stop_outcome(
            owned.connection, workspace_id=WORKSPACE_ID, stop_request_id="stop-0001"
        )
        == first
    )
