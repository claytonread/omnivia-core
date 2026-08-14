"""Durable connector synchronisation state (V06-8, migration 0017).

Persistence only. Every function here takes an open connection and expects the
caller to already be inside a `fenced_transaction`, exactly as the durable-job
persistence in `storage/jobs.py` does: the transaction boundary is a property of
the work being done, not of the individual row being written, and a function
that opened its own would make a batch and its checkpoint two commits.

The resumable cursor deliberately has no table of its own. It is an
`omnivia_job_checkpoints` row of kind `connector.cursor` under the run's attempt,
and `read_resume_cursor` is the single deterministic reader over the connector's
whole history of runs.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from typing import Final

from omnivia_core.connector import (
    ConnectorCursor,
    ConnectorFailure,
    DeadLetter,
    HealthState,
    SourceHealth,
)
from omnivia_core.contracts.v1 import to_canonical_json

#: The checkpoint kind that carries a connector cursor. Other kinds may share
#: the table; this is the only one this module reads or writes.
CURSOR_CHECKPOINT_KIND: Final = "connector.cursor"


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def next_sync_sequence(
    connection: sqlite3.Connection, *, workspace_id: str, connector_id: str
) -> int:
    """The sequence number the connector's next run must carry."""
    row = connection.execute(
        "SELECT COALESCE(MAX(sync_sequence), 0) + 1 FROM omnivia_connector_sync_runs "
        "WHERE workspace_id = ? AND connector_id = ?",
        (workspace_id, connector_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def register_sync_run(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    connector_id: str,
    sync_sequence: int,
    run_id: str,
    source_kind: str,
    state_version: int,
    started_at_us: int,
) -> None:
    """Bind one durable job to one connector as that connector's next run."""
    connection.execute(
        "INSERT INTO omnivia_connector_sync_runs "
        "(workspace_id, connector_id, sync_sequence, run_id, source_kind, "
        "state_version, started_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            connector_id,
            sync_sequence,
            run_id,
            source_kind,
            state_version,
            started_at_us,
        ),
    )


def read_last_state_version(
    connection: sqlite3.Connection, *, workspace_id: str, connector_id: str
) -> int | None:
    """The state format the connector's most recent run declared."""
    row = connection.execute(
        "SELECT state_version FROM omnivia_connector_sync_runs "
        "WHERE workspace_id = ? AND connector_id = ? "
        "ORDER BY sync_sequence DESC LIMIT 1",
        (workspace_id, connector_id),
    ).fetchone()
    return None if row is None else int(row[0])


def read_resume_cursor(
    connection: sqlite3.Connection, *, workspace_id: str, connector_id: str
) -> ConnectorCursor | None:
    """The last cursor this connector durably committed, across every run.

    The order is total and derived from two contiguous sequences -- the run's
    and the checkpoint's -- so this answers "where did we get to" with one row
    and no tie for any crash to resolve differently on the next start.
    """
    row = connection.execute(
        "SELECT c.checkpoint_json FROM omnivia_connector_sync_runs r "
        "JOIN omnivia_job_checkpoints c "
        "  ON c.workspace_id = r.workspace_id AND c.job_id = r.run_id "
        "WHERE r.workspace_id = ? AND r.connector_id = ? AND c.checkpoint_kind = ? "
        "ORDER BY r.sync_sequence DESC, c.checkpoint_sequence DESC LIMIT 1",
        (workspace_id, connector_id, CURSOR_CHECKPOINT_KIND),
    ).fetchone()
    if row is None:
        return None
    document = json.loads(str(row[0]))
    if not isinstance(document, dict):
        raise TypeError("stored connector cursor is not an object")
    return ConnectorCursor.from_wire(document)


def next_checkpoint_sequence(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> int:
    """The checkpoint sequence the run's next checkpoint must carry."""
    row = connection.execute(
        "SELECT COALESCE(MAX(checkpoint_sequence), -1) + 1 FROM omnivia_job_checkpoints "
        "WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def write_cursor_checkpoint(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    attempt_number: int,
    cursor: ConnectorCursor,
    created_at_us: int,
) -> int:
    """Commit one connector cursor as the run's next checkpoint.

    Called last inside the transaction that applied the batch the cursor ends,
    so a crash can leave the batch unapplied and the cursor unadvanced, but
    never the cursor advanced past work that was not committed.
    """
    sequence = next_checkpoint_sequence(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    document = to_canonical_json(cursor.to_wire())
    connection.execute(
        "INSERT INTO omnivia_job_checkpoints "
        "(workspace_id, job_id, checkpoint_sequence, attempt_number, created_at_us, "
        "checkpoint_kind, checkpoint_json, checkpoint_digest) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            run_id,
            sequence,
            attempt_number,
            created_at_us,
            CURSOR_CHECKPOINT_KIND,
            document,
            _digest(document),
        ),
    )
    return sequence


def record_dead_letter(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    connector_id: str,
    run_id: str,
    dead_letter: DeadLetter,
    recorded_at_us: int,
) -> str | None:
    """Record one item this run gave up on, with the classification that decided it.

    At most one row per run and item, which is the invariant the schema states
    and this preserves rather than works around. A source may legitimately report
    the same native id on more than one page of one run -- a paginated listing
    that shifts under the reader is enough -- and a second failure of an item
    this run has already given up on is a repeated observation, not a second
    decision. The first row stands, nothing is written, and `None` says so, so a
    caller counts durable dead letters rather than sightings.
    """
    identity = sha256(
        f"{workspace_id}|{run_id}|{dead_letter.native_id}".encode()
    ).hexdigest()[:40]
    dead_letter_id = f"dlt-{identity}"
    already = connection.execute(
        "SELECT 1 FROM omnivia_connector_dead_letters "
        "WHERE workspace_id = ? AND run_id = ? AND source_native_id = ?",
        (workspace_id, run_id, dead_letter.native_id),
    ).fetchone()
    if already is not None:
        return None
    connection.execute(
        "INSERT INTO omnivia_connector_dead_letters "
        "(dead_letter_id, workspace_id, connector_id, run_id, source_native_id, "
        "failure_code, retry_class, message, attempts, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dead_letter_id,
            workspace_id,
            connector_id,
            run_id,
            dead_letter.native_id,
            dead_letter.failure.code,
            dead_letter.failure.retry_class,
            dead_letter.failure.message,
            dead_letter.attempts,
            recorded_at_us,
        ),
    )
    return dead_letter_id


def read_dead_letters(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    connector_id: str,
    run_id: str | None = None,
) -> tuple[DeadLetter, ...]:
    """Every item this connector -- or one of its runs -- gave up on."""
    sql = (
        "SELECT source_native_id, failure_code, message, retry_class, attempts "
        "FROM omnivia_connector_dead_letters "
        "WHERE workspace_id = ? AND connector_id = ?"
    )
    parameters: tuple[object, ...] = (workspace_id, connector_id)
    if run_id is not None:
        sql += " AND run_id = ?"
        parameters += (run_id,)
    sql += " ORDER BY recorded_at_us, dead_letter_id"
    return tuple(
        DeadLetter(
            native_id=str(native_id),
            failure=ConnectorFailure(
                code=str(code), message=str(message), retry_class=str(retry_class)
            ),
            attempts=int(attempts),
        )
        for native_id, code, message, retry_class, attempts in connection.execute(
            sql, parameters
        ).fetchall()
    )


def record_health(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    connector_id: str,
    health: SourceHealth,
    observed_at_us: int,
) -> str:
    """Append one health observation to the connector's stream."""
    row = connection.execute(
        "SELECT COALESCE(MAX(health_sequence), 0) + 1 "
        "FROM omnivia_connector_health_events "
        "WHERE workspace_id = ? AND connector_id = ?",
        (workspace_id, connector_id),
    ).fetchone()
    assert row is not None
    sequence = int(row[0])
    identity = sha256(
        f"{workspace_id}|{connector_id}|{sequence}".encode()
    ).hexdigest()[:40]
    health_event_id = f"hev-{identity}"
    connection.execute(
        "INSERT INTO omnivia_connector_health_events "
        "(health_event_id, workspace_id, connector_id, health_sequence, health_state, "
        "detail, observed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            health_event_id,
            workspace_id,
            connector_id,
            sequence,
            health.state.value,
            health.detail or None,
            observed_at_us,
        ),
    )
    return health_event_id


def read_latest_health(
    connection: sqlite3.Connection, *, workspace_id: str, connector_id: str
) -> SourceHealth | None:
    """The connector's most recent health observation, if it has ever reported."""
    row = connection.execute(
        "SELECT health_state, detail FROM omnivia_connector_health_events "
        "WHERE workspace_id = ? AND connector_id = ? "
        "ORDER BY health_sequence DESC LIMIT 1",
        (workspace_id, connector_id),
    ).fetchone()
    if row is None:
        return None
    return SourceHealth(
        state=HealthState(str(row[0])), detail="" if row[1] is None else str(row[1])
    )


__all__ = [
    "CURSOR_CHECKPOINT_KIND",
    "next_checkpoint_sequence",
    "next_sync_sequence",
    "read_dead_letters",
    "read_last_state_version",
    "read_latest_health",
    "read_resume_cursor",
    "record_dead_letter",
    "record_health",
    "register_sync_run",
    "write_cursor_checkpoint",
]
