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
    ConnectorContractError,
    ConnectorCursor,
    ConnectorFailure,
    DeadLetter,
    HealthState,
    SourceHealth,
)
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_STATE_INVALID,
    ConnectorRefused,
    CursorBinding,
    CursorRecord,
    CursorState,
)
from omnivia_core.contracts.v1 import to_canonical_json

#: The checkpoint kind that carries a connector cursor. Other kinds may share
#: the table; this is the only one this module reads or writes.
CURSOR_CHECKPOINT_KIND: Final = "connector.cursor"

#: The exact key set a legacy two-field `ConnectorCursor` checkpoint document
#: carries. A four-field SPI `CursorRecord` document never matches this set
#: (it carries `binding` and `state`, not `state_version` and `token` at the
#: top level), which is what lets one checkpoint kind hold both shapes and a
#: reader tell them apart deterministically rather than by guessing.
_LEGACY_CURSOR_KEYS: Final[frozenset[str]] = frozenset({"state_version", "token"})
_SPI_CURSOR_KEYS: Final[frozenset[str]] = frozenset({"binding", "state"})
_SPI_CURSOR_KEYS_WITH_MIGRATION: Final[frozenset[str]] = frozenset(
    {"binding", "state", "migration_evidence"}
)
_SPI_BINDING_KEYS: Final[frozenset[str]] = frozenset({"workspace_id", "connector_id"})
_SPI_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {"state_version", "payload", "witness_seq", "predecessor_digest"}
)
_SPI_MIGRATION_KEYS: Final[frozenset[str]] = frozenset(
    {"before_digest", "after_digest"}
)


def _spi_state_to_wire(state: CursorState) -> dict[str, object]:
    return {
        "state_version": state.state_version,
        "payload": state.payload.decode("ascii"),
        "witness_seq": state.witness_seq,
        "predecessor_digest": (
            None if state.predecessor_digest is None else state.predecessor_digest.hex()
        ),
    }


def _spi_state_from_wire(document: dict[str, object]) -> CursorState:
    if frozenset(document) != _SPI_STATE_KEYS:
        raise ValueError("SPI state has an unexpected shape")
    state_version = document["state_version"]
    payload = document["payload"]
    witness_seq = document["witness_seq"]
    predecessor = document["predecessor_digest"]
    if (
        not isinstance(state_version, int)
        or isinstance(state_version, bool)
        or not isinstance(payload, str)
        or not isinstance(witness_seq, int)
        or isinstance(witness_seq, bool)
        or (predecessor is not None and not isinstance(predecessor, str))
    ):
        raise ValueError("SPI state fields have invalid types")
    encoded_payload = payload.encode("ascii")
    predecessor_bytes: bytes | None = None
    if predecessor is not None:
        if len(predecessor) != 64 or any(character not in "0123456789abcdef" for character in predecessor):
            raise ValueError("SPI predecessor digest is not lowercase SHA-256 hex")
        predecessor_bytes = bytes.fromhex(predecessor)
    return CursorState(
        state_version=state_version,
        payload=encoded_payload,
        witness_seq=witness_seq,
        predecessor_digest=predecessor_bytes,
    )


def write_spi_cursor_checkpoint(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    attempt_number: int,
    record: CursorRecord,
    created_at_us: int,
    migration_evidence: dict[str, str] | None = None,
) -> int:
    """Commit one SPI `CursorRecord` -- binding, and all four state fields.

    Shares `omnivia_job_checkpoints` and `CURSOR_CHECKPOINT_KIND` with the
    legacy two-field cursor rather than a table of its own: the resumable
    cursor has no table by design (module docstring), and a second one would
    just be a second copy of the same history.
    """
    sequence = next_checkpoint_sequence(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    document: dict[str, object] = {
        "binding": {
            "workspace_id": record.binding.workspace_id,
            "connector_id": record.binding.connector_id,
        },
        "state": _spi_state_to_wire(record.state),
    }
    if migration_evidence is not None:
        document["migration_evidence"] = migration_evidence
    payload = to_canonical_json(document)
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
            payload,
            _digest(payload),
        ),
    )
    return sequence


def read_spi_resume_cursor(
    connection: sqlite3.Connection, *, workspace_id: str, connector_id: str
) -> CursorRecord | None:
    """The last SPI `CursorRecord` this connector durably committed, if any.

    A legacy two-field checkpoint under the same connector id reads back as
    `None` here -- restored without an SPI cursor, which is an explicit resync
    rather than a guess at reinterpreting the older shape (candidate's
    unmigratable/restored-without-cursor rule).
    """
    row = connection.execute(
        "SELECT c.checkpoint_json, c.checkpoint_digest "
        "FROM omnivia_connector_sync_runs r "
        "JOIN omnivia_job_checkpoints c "
        "  ON c.workspace_id = r.workspace_id AND c.job_id = r.run_id "
        "WHERE r.workspace_id = ? AND r.connector_id = ? AND c.checkpoint_kind = ? "
        "ORDER BY r.sync_sequence DESC, c.checkpoint_sequence DESC LIMIT 1",
        (workspace_id, connector_id, CURSOR_CHECKPOINT_KIND),
    ).fetchone()
    if row is None:
        return None
    payload = str(row[0])
    if str(row[1]) != _digest(payload):
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID, "persisted cursor checkpoint digest does not verify"
        )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID, "persisted cursor checkpoint is not JSON"
        ) from error
    if isinstance(document, dict) and frozenset(document) == _LEGACY_CURSOR_KEYS:
        return None
    try:
        if not isinstance(document, dict) or frozenset(document) not in {
            _SPI_CURSOR_KEYS,
            _SPI_CURSOR_KEYS_WITH_MIGRATION,
        }:
            raise ValueError("SPI checkpoint has an unexpected shape")
        binding = document["binding"]
        state = document["state"]
        if (
            not isinstance(binding, dict)
            or frozenset(binding) != _SPI_BINDING_KEYS
            or not all(isinstance(binding[key], str) for key in _SPI_BINDING_KEYS)
            or not isinstance(state, dict)
        ):
            raise ValueError("SPI checkpoint binding or state has an invalid shape")
        migration = document.get("migration_evidence")
        if migration is not None and (
            not isinstance(migration, dict)
            or frozenset(migration) != _SPI_MIGRATION_KEYS
            or not all(
                isinstance(migration[key], str)
                and len(migration[key]) == 64
                and all(character in "0123456789abcdef" for character in migration[key])
                for key in _SPI_MIGRATION_KEYS
            )
        ):
            raise ValueError("SPI migration evidence has an invalid shape")
        return CursorRecord(
            binding=CursorBinding(
                workspace_id=binding["workspace_id"],
                connector_id=binding["connector_id"],
            ),
            state=_spi_state_from_wire(state),
        )
    except (ConnectorContractError, KeyError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID, "persisted SPI cursor checkpoint is malformed"
        ) from error


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
    "read_spi_resume_cursor",
    "record_dead_letter",
    "record_health",
    "register_sync_run",
    "write_cursor_checkpoint",
    "write_spi_cursor_checkpoint",
]
