"""Durable, bounded scheduling state for captured engineering source production.

The immutable capture header and source event remain the evidence.  This module owns
only the mutable queue and keyset cursors that decide which bounded unit a service
tries next.  Every standalone write is fenced; ``enqueue_capture_in_transaction`` is
the one exception because it deliberately joins the capture header's existing fenced
transaction so a seal and its work item commit together or neither does.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

LEGACY_SEED_BATCH: Final = 64
MAX_QUEUE_BATCH: Final = 64
MAX_RETRY_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class SourceQueueItem:
    """One redacted scheduling identity; it never carries a local checkout path."""

    repository_id: str
    snapshot_id: str
    checkout_id: str
    stream_id: str
    expected_frontier: int | None
    expected_predecessor_snapshot_id: str | None
    available_at_us: int


def source_stream_id(
    workspace_id: str,
    repository_id: str,
    installation_id: str,
    checkout_id: str,
) -> str:
    """The stable stream identity shared by capture and scheduling."""

    seed = (
        f"{workspace_id}|{repository_id}|{installation_id}|{checkout_id}"
    ).encode()
    return f"src-stream-{hashlib.sha256(seed).hexdigest()[:40]}"


def _ensure_state(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    installation_id: str,
    now_us: int,
) -> None:
    if connection.execute(
        "SELECT 1 FROM omnivia_engineering_source_producer_state "
        "WHERE workspace_id = ? AND installation_id = ?",
        (workspace_id, installation_id),
    ).fetchone() is not None:
        return
    ceiling = connection.execute(
        "SELECT captured_at_us, snapshot_id "
        "FROM omnivia_engineering_snapshot_captures "
        "WHERE workspace_id = ? AND installation_id = ? "
        "ORDER BY captured_at_us DESC, snapshot_id DESC LIMIT 1",
        (workspace_id, installation_id),
    ).fetchone()
    connection.execute(
        "INSERT INTO omnivia_engineering_source_producer_state "
        "(workspace_id, installation_id, next_lane, "
        "queue_cursor_available_at_us, queue_cursor_snapshot_id, "
        "checkout_cursor, legacy_cursor_captured_at_us, "
        "legacy_cursor_snapshot_id, legacy_seed_through_captured_at_us, "
        "legacy_seed_through_snapshot_id, legacy_seed_complete, updated_at_us) "
        "VALUES (?, ?, 'recovery', NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?) "
        "ON CONFLICT (workspace_id, installation_id) DO NOTHING",
        (
            workspace_id,
            installation_id,
            None if ceiling is None else int(ceiling[0]),
            None if ceiling is None else str(ceiling[1]),
            int(ceiling is None),
            max(1, now_us),
        ),
    )


def enqueue_capture_in_transaction(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    installation_id: str,
    snapshot_id: str,
    repository_id: str,
    checkout_id: str,
    captured_at_us: int,
    expected_frontier: int | None,
    expected_predecessor_snapshot_id: str | None,
) -> None:
    """Enqueue an exact sealed capture inside its caller's fenced transaction.

    A generic maintenance capture has no live-head intent and passes ``None`` for
    both expected values.  If the live producer later observes the same already
    sealed snapshot, it may bind that unresolved row once to its exact frontier.
    Binding preserves an existing retry schedule and attempt count.  An attempted
    second bind or a bind to different intent fails.
    """

    stream_id = source_stream_id(
        workspace_id, repository_id, installation_id, checkout_id
    )
    connection.execute(
        "INSERT INTO omnivia_engineering_source_producer_queue "
        "(workspace_id, installation_id, snapshot_id, repository_id, checkout_id, "
        "stream_id, expected_frontier, expected_predecessor_snapshot_id, state, "
        "available_at_us, attempt_count, enqueued_at_us, last_attempt_at_us, "
        "settled_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, "
        "NULL, NULL) ON CONFLICT (workspace_id, installation_id, snapshot_id) "
        "DO NOTHING",
        (
            workspace_id,
            installation_id,
            snapshot_id,
            repository_id,
            checkout_id,
            stream_id,
            expected_frontier,
            expected_predecessor_snapshot_id,
            captured_at_us,
            captured_at_us,
        ),
    )
    row = connection.execute(
        "SELECT repository_id, checkout_id, stream_id, expected_frontier, "
        "expected_predecessor_snapshot_id, state "
        "FROM omnivia_engineering_source_producer_queue "
        "WHERE workspace_id = ? AND installation_id = ? AND snapshot_id = ?",
        (workspace_id, installation_id, snapshot_id),
    ).fetchone()
    expected_identity = (repository_id, checkout_id, stream_id)
    if row is None or (str(row[0]), str(row[1]), str(row[2])) != expected_identity:
        raise StorageError("source producer queue identity does not match its capture")
    stored_frontier = None if row[3] is None else int(row[3])
    stored_predecessor = None if row[4] is None else str(row[4])
    if (stored_frontier, stored_predecessor) == (
        expected_frontier,
        expected_predecessor_snapshot_id,
    ):
        return
    if (
        expected_frontier is None
        and expected_predecessor_snapshot_id is None
        and stored_frontier is not None
    ):
        # A generic maintenance replay is not a request to weaken producer
        # intent already bound to the same immutable seal.
        return
    if (
        stored_frontier is None
        and stored_predecessor is None
        and expected_frontier is not None
        and str(row[5]) in {"pending", "retry"}
    ):
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_queue "
            "SET expected_frontier = ?, expected_predecessor_snapshot_id = ? "
            "WHERE workspace_id = ? AND installation_id = ? AND snapshot_id = ?",
            (
                expected_frontier,
                expected_predecessor_snapshot_id,
                workspace_id,
                installation_id,
                snapshot_id,
            ),
        )
        return
    raise StorageError("source producer queue intent does not match its capture")


def seed_legacy_captures(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    fencing_generation: int,
    now_us: int,
    limit: int = LEGACY_SEED_BATCH,
) -> int:
    """Seed at most ``limit`` pre-0058 capture headers by a durable keyset.

    The migration copies no history.  This pass reads at most ``limit`` headers,
    performs indexed identity probes for those rows, and commits its cursor with the
    inserts.  New post-0058 captures enqueue themselves and therefore remain safe if
    their timestamp falls behind this one-time legacy cursor.
    """

    if limit <= 0:
        return 0
    limit = min(limit, LEGACY_SEED_BATCH)
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        _ensure_state(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
        )
        state = connection.execute(
            "SELECT legacy_cursor_captured_at_us, legacy_cursor_snapshot_id, "
            "legacy_seed_complete, updated_at_us, "
            "legacy_seed_through_captured_at_us, "
            "legacy_seed_through_snapshot_id "
            "FROM omnivia_engineering_source_producer_state "
            "WHERE workspace_id = ? AND installation_id = ?",
            (workspace_id, installation_id),
        ).fetchone()
        if state is None:  # pragma: no cover - _ensure_state is in this transaction
            raise StorageError("source producer scheduler state is unavailable")
        if int(state[2]) == 1:
            return 0
        if state[4] is None:  # pragma: no cover - empty ceilings start complete
            raise StorageError("source producer seed ceiling is unavailable")
        ceiling = (int(state[4]), str(state[5]))
        if state[0] is None:
            rows = connection.execute(
                "SELECT captured_at_us, snapshot_id, repository_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures "
                "WHERE workspace_id = ? AND installation_id = ? "
                "AND (captured_at_us, snapshot_id) <= (?, ?) "
                "ORDER BY captured_at_us, snapshot_id LIMIT ?",
                (workspace_id, installation_id, *ceiling, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT captured_at_us, snapshot_id, repository_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures "
                "WHERE workspace_id = ? AND installation_id = ? "
                "AND (captured_at_us, snapshot_id) > (?, ?) "
                "AND (captured_at_us, snapshot_id) <= (?, ?) "
                "ORDER BY captured_at_us, snapshot_id LIMIT ?",
                (
                    workspace_id,
                    installation_id,
                    int(state[0]),
                    str(state[1]),
                    *ceiling,
                    limit,
                ),
            ).fetchall()

        for captured_at, snapshot, repository, checkout in rows:
            event = connection.execute(
                "SELECT stream_id, recorded_at_us, manifest_format "
                "FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, str(snapshot)),
            ).fetchone()
            stream = source_stream_id(
                workspace_id,
                str(repository),
                installation_id,
                str(checkout),
            )
            event_matches = (
                event is not None
                and str(event[0]) == stream
                and str(event[2]) == "captured_v1"
            )
            settled_at = (
                max(int(captured_at), int(event[1])) if event_matches else None
            )
            connection.execute(
                "INSERT INTO omnivia_engineering_source_producer_queue "
                "(workspace_id, installation_id, snapshot_id, repository_id, "
                "checkout_id, stream_id, expected_frontier, "
                "expected_predecessor_snapshot_id, state, available_at_us, "
                "attempt_count, enqueued_at_us, last_attempt_at_us, settled_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 0, ?, NULL, ?) "
                "ON CONFLICT (workspace_id, installation_id, snapshot_id) DO NOTHING",
                (
                    workspace_id,
                    installation_id,
                    str(snapshot),
                    str(repository),
                    str(checkout),
                    stream,
                    "settled" if event_matches else "pending",
                    int(captured_at),
                    int(captured_at),
                    settled_at,
                ),
            )

        updated = max(max(1, now_us), int(state[3]))
        if rows:
            last = rows[-1]
            connection.execute(
                "UPDATE omnivia_engineering_source_producer_state "
                "SET legacy_cursor_captured_at_us = ?, "
                "legacy_cursor_snapshot_id = ?, legacy_seed_complete = ?, "
                "updated_at_us = ? WHERE workspace_id = ? AND installation_id = ?",
                (
                    int(last[0]),
                    str(last[1]),
                    int(len(rows) < limit),
                    updated,
                    workspace_id,
                    installation_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE omnivia_engineering_source_producer_state "
                "SET legacy_seed_complete = 1, updated_at_us = ? "
                "WHERE workspace_id = ? AND installation_id = ?",
                (updated, workspace_id, installation_id),
            )
        return len(rows)


def take_lane_turn(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    fencing_generation: int,
    now_us: int,
) -> str:
    """Return the durable next lane and persist its opposite before effects."""

    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        _ensure_state(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
        )
        row = connection.execute(
            "SELECT next_lane, updated_at_us "
            "FROM omnivia_engineering_source_producer_state "
            "WHERE workspace_id = ? AND installation_id = ?",
            (workspace_id, installation_id),
        ).fetchone()
        if row is None:  # pragma: no cover
            raise StorageError("source producer scheduler state is unavailable")
        lane = str(row[0])
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_state "
            "SET next_lane = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND installation_id = ?",
            (
                "checkout" if lane == "recovery" else "recovery",
                max(max(1, now_us), int(row[1])),
                workspace_id,
                installation_id,
            ),
        )
        return lane


def take_next_checkout(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    fencing_generation: int,
    now_us: int,
) -> tuple[str, str, str] | None:
    """Select one checkout and persist the keyset cursor before filesystem work."""

    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        _ensure_state(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
        )
        state = connection.execute(
            "SELECT checkout_cursor, updated_at_us "
            "FROM omnivia_engineering_source_producer_state "
            "WHERE workspace_id = ? AND installation_id = ?",
            (workspace_id, installation_id),
        ).fetchone()
        if state is None:  # pragma: no cover
            raise StorageError("source producer scheduler state is unavailable")
        cursor = None if state[0] is None else str(state[0])
        if cursor is None:
            row = connection.execute(
                "SELECT repository_id, checkout_id, checkout_hint "
                "FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? "
                "ORDER BY checkout_id LIMIT 1",
                (workspace_id, installation_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT repository_id, checkout_id, checkout_hint "
                "FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? "
                "AND checkout_id > ? ORDER BY checkout_id LIMIT 1",
                (workspace_id, installation_id, cursor),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT repository_id, checkout_id, checkout_hint "
                    "FROM omnivia_engineering_checkouts "
                    "WHERE workspace_id = ? AND installation_id = ? "
                    "ORDER BY checkout_id LIMIT 1",
                    (workspace_id, installation_id),
                ).fetchone()
        new_cursor = None if row is None else str(row[1])
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_state "
            "SET checkout_cursor = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND installation_id = ?",
            (
                new_cursor,
                max(max(1, now_us), int(state[1])),
                workspace_id,
                installation_id,
            ),
        )
        if row is None:
            return None
        return str(row[0]), str(row[1]), str(row[2])


def _eligible_rows(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    installation_id: str,
    now_us: int,
    limit: int,
    cursor: tuple[int, str] | None,
    before_or_equal: bool,
) -> list[Any]:
    rows: list[Any] = []
    for state in ("pending", "retry"):
        params: tuple[object, ...]
        boundary = ""
        if cursor is not None:
            boundary = (
                "AND (available_at_us, snapshot_id) <= (?, ?) "
                if before_or_equal
                else "AND (available_at_us, snapshot_id) > (?, ?) "
            )
            params = (
                workspace_id,
                installation_id,
                state,
                now_us,
                cursor[0],
                cursor[1],
                limit,
            )
        else:
            params = (
                workspace_id,
                installation_id,
                state,
                now_us,
                limit,
            )
        rows.extend(
            connection.execute(
                "SELECT repository_id, snapshot_id, checkout_id, stream_id, "
                "expected_frontier, expected_predecessor_snapshot_id, "
                "available_at_us FROM omnivia_engineering_source_producer_queue "
                "WHERE workspace_id = ? AND installation_id = ? AND state = ? "
                "AND available_at_us <= ? "
                + boundary
                + "ORDER BY available_at_us, snapshot_id LIMIT ?",
                params,
            ).fetchall()
        )
    rows.sort(key=lambda row: (int(row[6]), str(row[1])))
    return rows[:limit]


def take_queue_items(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    fencing_generation: int,
    now_us: int,
    limit: int,
) -> tuple[SourceQueueItem, ...]:
    """Select a bounded active batch and persist its keyset before dispatch."""

    if limit <= 0:
        return ()
    limit = min(limit, MAX_QUEUE_BATCH)
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        _ensure_state(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
        )
        state = connection.execute(
            "SELECT queue_cursor_available_at_us, queue_cursor_snapshot_id, "
            "updated_at_us FROM omnivia_engineering_source_producer_state "
            "WHERE workspace_id = ? AND installation_id = ?",
            (workspace_id, installation_id),
        ).fetchone()
        if state is None:  # pragma: no cover
            raise StorageError("source producer scheduler state is unavailable")
        cursor = (
            None
            if state[0] is None
            else (int(state[0]), str(state[1]))
        )
        rows = _eligible_rows(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
            limit=limit,
            cursor=cursor,
            before_or_equal=False,
        )
        if len(rows) < limit and cursor is not None:
            seen = {str(row[1]) for row in rows}
            wrapped = _eligible_rows(
                connection,
                workspace_id=workspace_id,
                installation_id=installation_id,
                now_us=now_us,
                limit=limit,
                cursor=cursor,
                before_or_equal=True,
            )
            rows.extend(row for row in wrapped if str(row[1]) not in seen)
            rows = rows[:limit]
        if rows:
            last = rows[-1]
            cursor_values: tuple[object, object] = (int(last[6]), str(last[1]))
        else:
            cursor_values = (None, None)
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_state "
            "SET queue_cursor_available_at_us = ?, queue_cursor_snapshot_id = ?, "
            "updated_at_us = ? WHERE workspace_id = ? AND installation_id = ?",
            (
                *cursor_values,
                max(max(1, now_us), int(state[2])),
                workspace_id,
                installation_id,
            ),
        )
        return tuple(
            SourceQueueItem(
                repository_id=str(row[0]),
                snapshot_id=str(row[1]),
                checkout_id=str(row[2]),
                stream_id=str(row[3]),
                expected_frontier=None if row[4] is None else int(row[4]),
                expected_predecessor_snapshot_id=(
                    None if row[5] is None else str(row[5])
                ),
                available_at_us=int(row[6]),
            )
            for row in rows
        )


def mark_queue_retry(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    snapshot_id: str,
    fencing_generation: int,
    now_us: int,
) -> None:
    """Record one bounded retry with saturating exponential wall-clock delay."""

    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        _ensure_state(
            connection,
            workspace_id=workspace_id,
            installation_id=installation_id,
            now_us=now_us,
        )
        row = connection.execute(
            "SELECT state, attempt_count, enqueued_at_us, available_at_us "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND installation_id = ? AND snapshot_id = ?",
            (workspace_id, installation_id, snapshot_id),
        ).fetchone()
        if row is None or str(row[0]) == "settled":
            return
        attempts = min(int(row[1]) + 1, 1_000_000)
        attempt_time = max(max(1, now_us), int(row[2]))
        delay_seconds = min(1 << min(attempts - 1, 6), MAX_RETRY_SECONDS)
        available = max(int(row[3]), attempt_time + delay_seconds * 1_000_000)
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_queue "
            "SET state = 'retry', available_at_us = ?, attempt_count = ?, "
            "last_attempt_at_us = ? WHERE workspace_id = ? AND installation_id = ? "
            "AND snapshot_id = ?",
            (
                available,
                attempts,
                attempt_time,
                workspace_id,
                installation_id,
                snapshot_id,
            ),
        )
        # ``available_at_us`` participates in the cyclic queue key.  Moving a
        # poison item forward while leaving the cursor at its old key would let
        # that same item win every post-cursor lookup and starve the wrapped
        # prefix across restarts.  Move the cursor through the item's new key in
        # the same transaction; the next read then wraps to older eligible work.
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_state "
            "SET queue_cursor_available_at_us = ?, queue_cursor_snapshot_id = ?, "
            "updated_at_us = max(updated_at_us, ?) "
            "WHERE workspace_id = ? AND installation_id = ? "
            "AND (queue_cursor_available_at_us IS NULL "
            "OR (queue_cursor_available_at_us, queue_cursor_snapshot_id) < (?, ?))",
            (
                available,
                snapshot_id,
                attempt_time,
                workspace_id,
                installation_id,
                available,
                snapshot_id,
            ),
        )


def mark_queue_settled(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    installation_id: str,
    snapshot_id: str,
    fencing_generation: int,
    now_us: int,
) -> None:
    """Settle exactly one item; replay of an already settled item is a no-op."""

    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        row = connection.execute(
            "SELECT state, attempt_count, enqueued_at_us, available_at_us "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND installation_id = ? AND snapshot_id = ?",
            (workspace_id, installation_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise StorageError("source producer queue item is unavailable")
        if str(row[0]) == "settled":
            return
        attempts = min(int(row[1]) + 1, 1_000_000)
        attempt_time = max(max(1, now_us), int(row[2]))
        connection.execute(
            "UPDATE omnivia_engineering_source_producer_queue "
            "SET state = 'settled', available_at_us = ?, attempt_count = ?, "
            "last_attempt_at_us = ?, settled_at_us = ? "
            "WHERE workspace_id = ? AND installation_id = ? AND snapshot_id = ?",
            (
                max(int(row[3]), attempt_time),
                attempts,
                attempt_time,
                attempt_time,
                workspace_id,
                installation_id,
                snapshot_id,
            ),
        )


__all__ = [
    "LEGACY_SEED_BATCH",
    "MAX_QUEUE_BATCH",
    "SourceQueueItem",
    "enqueue_capture_in_transaction",
    "mark_queue_retry",
    "mark_queue_settled",
    "seed_legacy_captures",
    "source_stream_id",
    "take_lane_turn",
    "take_next_checkout",
    "take_queue_items",
]
