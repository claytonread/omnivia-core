"""Transactional active-Run summaries and deterministic full replay (RT-105).

The canonical rows remain ``omnivia_runtime_runs`` and
``omnivia_runtime_events``.  This module owns only the small product-safe summary
derived from them.  Incremental application is deliberately transaction-local:
``RuntimeWriter`` calls :func:`apply_runtime_run_summary_event` immediately after
the canonical event insert, so both writes commit or roll back together.

The accepted store has a total order per Run (``sequence``), not a partition-wide
``global_position``.  That is sufficient here because one summary has no
cross-Run state.  Full replay orders independent Runs by ``run_id`` and consumes
each Run strictly by sequence; a future cross-aggregate projector must first add
and reconcile a canonical partition-global position rather than treating this
deterministic iteration order as one.

Likewise, the accepted event envelope has no schema-version field.  This reducer
uses only its required, validated ``run_status``; the open ``event_kind`` is
descriptive.  It therefore neither invents schema-version quarantine nor rejects
future event kinds whose explicit status remains valid.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final

from omnivia_core.contracts.v1 import (
    RUN_TERMINAL_STATUSES,
    RunDefinitionRef,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

_TABLE: Final = "omnivia_runtime_run_summaries"


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int):
        raise StorageError(f"a stored Run summary {label} is not an integer")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeRunSummary:
    """The product-safe Run facts migrations 0018 and 0020 can report honestly."""

    workspace_id: str
    run_id: str
    job_id: str
    definition: RunDefinitionRef
    logical_key: str
    originating_operation: str
    audit_reference: str
    status: str
    created_at: str
    updated_at: str
    finished_at: str | None
    last_runtime_event_id: str
    last_sequence: int


@dataclass(frozen=True, slots=True)
class RuntimeRunSummaryRebuild:
    """Deterministic evidence returned by a completed full replay."""

    record_count: int
    build_digest: str


@dataclass(frozen=True, slots=True)
class _RunSource:
    workspace_id: str
    run_id: str
    job_id: str
    definition_kind: str
    definition_id: str
    definition_version: str
    logical_key: str
    originating_operation: str
    audit_ref: str
    created_at_us: int


@dataclass(frozen=True, slots=True)
class _EventSource:
    runtime_event_id: str
    sequence: int
    occurred_at_us: int
    run_status: str


@dataclass(frozen=True, slots=True)
class _StoredSummary:
    run: _RunSource
    run_status: str
    updated_at_us: int
    terminal_at_us: int | None
    last_runtime_event_id: str
    last_sequence: int


def _reduce_event(
    current: _StoredSummary | None,
    run: _RunSource,
    event: _EventSource,
) -> _StoredSummary:
    """Apply one canonical event to one summary, without touching storage.

    Canonical event identity is immutable and unique.  Once a summary has consumed
    sequence ``N``, any canonical event at ``<= N`` is therefore already in that
    exact prefix and is an idempotent replay.  A successor must be exactly
    contiguous; skipping a sequence fails before any projection write.
    """
    if current is None:
        if event.sequence != 0:
            raise StorageError(
                "a run summary cannot start after sequence zero; full replay is required"
            )
    else:
        if (
            current.run.workspace_id != run.workspace_id
            or current.run.run_id != run.run_id
        ):
            raise StorageError(
                "a runtime event cannot be applied to another run summary"
            )
        if current.run != run:
            raise StorageError(
                "a canonical run changed beneath its materialised summary"
            )
        if event.sequence <= current.last_sequence:
            return current
        if event.sequence != current.last_sequence + 1:
            raise StorageError("a run summary event sequence is not contiguous")

    terminal_at_us = (
        event.occurred_at_us if event.run_status in RUN_TERMINAL_STATUSES else None
    )
    return _StoredSummary(
        run=run,
        run_status=event.run_status,
        updated_at_us=event.occurred_at_us,
        terminal_at_us=terminal_at_us,
        last_runtime_event_id=event.runtime_event_id,
        last_sequence=event.sequence,
    )


def _run_source(row: sqlite3.Row | tuple[object, ...]) -> _RunSource:
    return _RunSource(
        workspace_id=str(row[0]),
        run_id=str(row[1]),
        job_id=str(row[2]),
        definition_kind=str(row[3]),
        definition_id=str(row[4]),
        definition_version=str(row[5]),
        logical_key=str(row[6]),
        originating_operation=str(row[7]),
        audit_ref=str(row[8]),
        created_at_us=_integer(row[9], "creation timestamp"),
    )


def _event_source(row: sqlite3.Row | tuple[object, ...], offset: int) -> _EventSource:
    return _EventSource(
        runtime_event_id=str(row[offset]),
        sequence=_integer(row[offset + 1], "event sequence"),
        occurred_at_us=_integer(row[offset + 2], "event timestamp"),
        run_status=str(row[offset + 3]),
    )


def _stored_from_row(row: sqlite3.Row | tuple[object, ...]) -> _StoredSummary:
    return _StoredSummary(
        run=_run_source(row),
        run_status=str(row[10]),
        updated_at_us=_integer(row[11], "update timestamp"),
        terminal_at_us=(
            None if row[12] is None else _integer(row[12], "terminal timestamp")
        ),
        last_runtime_event_id=str(row[13]),
        last_sequence=_integer(row[14], "last sequence"),
    )


_SOURCE_COLUMNS: Final = (
    "workspace_id, run_id, job_id, definition_kind, definition_id, "
    "definition_version, logical_key, originating_operation, audit_ref, created_at_us"
)
_SUMMARY_COLUMNS: Final = (
    f"{_SOURCE_COLUMNS}, run_status, updated_at_us, terminal_at_us, "
    "last_runtime_event_id, last_sequence"
)


def _read_stored(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> _StoredSummary | None:
    row = connection.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM {_TABLE} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    return None if row is None else _stored_from_row(row)


def _insert_stored(connection: sqlite3.Connection, summary: _StoredSummary) -> None:
    run = summary.run
    connection.execute(
        f"INSERT INTO {_TABLE} ({_SUMMARY_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.workspace_id,
            run.run_id,
            run.job_id,
            run.definition_kind,
            run.definition_id,
            run.definition_version,
            run.logical_key,
            run.originating_operation,
            run.audit_ref,
            run.created_at_us,
            summary.run_status,
            summary.updated_at_us,
            summary.terminal_at_us,
            summary.last_runtime_event_id,
            summary.last_sequence,
        ),
    )


def _update_stored(connection: sqlite3.Connection, summary: _StoredSummary) -> None:
    connection.execute(
        f"UPDATE {_TABLE} SET run_status = ?, updated_at_us = ?, terminal_at_us = ?, "
        "last_runtime_event_id = ?, last_sequence = ? "
        "WHERE workspace_id = ? AND run_id = ?",
        (
            summary.run_status,
            summary.updated_at_us,
            summary.terminal_at_us,
            summary.last_runtime_event_id,
            summary.last_sequence,
            summary.run.workspace_id,
            summary.run.run_id,
        ),
    )


def _public(summary: _StoredSummary) -> RuntimeRunSummary:
    run = summary.run
    return RuntimeRunSummary(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        job_id=run.job_id,
        definition=RunDefinitionRef(
            definition_kind=run.definition_kind,
            definition_id=run.definition_id,
            definition_version=run.definition_version,
        ),
        logical_key=run.logical_key,
        originating_operation=run.originating_operation,
        audit_reference=run.audit_ref,
        status=summary.run_status,
        created_at=_timestamp(run.created_at_us),
        updated_at=_timestamp(summary.updated_at_us),
        finished_at=(
            None
            if summary.terminal_at_us is None
            else _timestamp(summary.terminal_at_us)
        ),
        last_runtime_event_id=summary.last_runtime_event_id,
        last_sequence=summary.last_sequence,
    )


def apply_runtime_run_summary_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    runtime_event_id: str,
) -> RuntimeRunSummary:
    """Materialise one already-inserted canonical event in the caller's transaction."""
    row = connection.execute(
        "SELECT r.workspace_id, r.run_id, r.job_id, r.definition_kind, "
        "r.definition_id, r.definition_version, r.logical_key, "
        "r.originating_operation, r.audit_ref, r.created_at_us, "
        "e.runtime_event_id, e.sequence, e.occurred_at_us, e.run_status "
        "FROM omnivia_runtime_runs r JOIN omnivia_runtime_events e "
        "ON e.workspace_id = r.workspace_id AND e.run_id = r.run_id "
        "WHERE r.workspace_id = ? AND r.run_id = ? AND e.runtime_event_id = ?",
        (workspace_id, run_id, runtime_event_id),
    ).fetchone()
    if row is None:
        raise StorageError("a run summary event has no canonical run/event source")
    run = _run_source(row)
    event = _event_source(row, 10)
    current = _read_stored(connection, workspace_id=workspace_id, run_id=run_id)
    reduced = _reduce_event(current, run, event)
    if current is None:
        _insert_stored(connection, reduced)
    elif reduced is not current:
        _update_stored(connection, reduced)
    return _public(reduced)


def read_runtime_run_summary(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> RuntimeRunSummary | None:
    """Read one workspace-scoped summary, never falling back to canonical storage."""
    stored = _read_stored(connection, workspace_id=workspace_id, run_id=run_id)
    return None if stored is None else _public(stored)


def _canonical_document(summaries: tuple[_StoredSummary, ...]) -> str:
    return to_canonical_json(
        {
            "summaries": [
                {
                    "audit_ref": item.run.audit_ref,
                    "created_at_us": item.run.created_at_us,
                    "definition_id": item.run.definition_id,
                    "definition_kind": item.run.definition_kind,
                    "definition_version": item.run.definition_version,
                    "job_id": item.run.job_id,
                    "last_runtime_event_id": item.last_runtime_event_id,
                    "last_sequence": item.last_sequence,
                    "logical_key": item.run.logical_key,
                    "originating_operation": item.run.originating_operation,
                    "run_id": item.run.run_id,
                    "run_status": item.run_status,
                    "terminal_at_us": item.terminal_at_us,
                    "updated_at_us": item.updated_at_us,
                    "workspace_id": item.run.workspace_id,
                }
                for item in summaries
            ]
        }
    )


def runtime_run_summary_projection_bytes(
    connection: sqlite3.Connection, *, workspace_id: str
) -> bytes:
    """Canonical durable projection bytes, ordered independently of SQLite pages."""
    rows = connection.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM {_TABLE} WHERE workspace_id = ? "
        "ORDER BY run_id",
        (workspace_id,),
    ).fetchall()
    summaries = tuple(_stored_from_row(row) for row in rows)
    return _canonical_document(summaries).encode("utf-8")


def runtime_run_summary_projection_digest(
    connection: sqlite3.Connection, *, workspace_id: str
) -> str:
    """SHA-256 over :func:`runtime_run_summary_projection_bytes`."""
    payload = runtime_run_summary_projection_bytes(
        connection, workspace_id=workspace_id
    )
    return f"sha256:{sha256(payload).hexdigest()}"


def rebuild_runtime_run_summaries(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> RuntimeRunSummaryRebuild:
    """Atomically replace one workspace's summaries by replaying canonical events."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        rows = connection.execute(
            "SELECT r.workspace_id, r.run_id, r.job_id, r.definition_kind, "
            "r.definition_id, r.definition_version, r.logical_key, "
            "r.originating_operation, r.audit_ref, r.created_at_us, "
            "e.runtime_event_id, e.sequence, e.occurred_at_us, e.run_status "
            "FROM omnivia_runtime_runs r LEFT JOIN omnivia_runtime_events e "
            "ON e.workspace_id = r.workspace_id AND e.run_id = r.run_id "
            "WHERE r.workspace_id = ? ORDER BY r.run_id, e.sequence",
            (workspace_id,),
        ).fetchall()
        connection.execute(
            f"DELETE FROM {_TABLE} WHERE workspace_id = ?", (workspace_id,)
        )

        by_run: dict[str, _StoredSummary] = {}
        for row in rows:
            if row[10] is None:
                raise StorageError(f"run {row[1]!r} has no event stream to replay")
            run = _run_source(row)
            event = _event_source(row, 10)
            by_run[run.run_id] = _reduce_event(by_run.get(run.run_id), run, event)

        summaries = tuple(by_run[run_id] for run_id in sorted(by_run))
        for summary in summaries:
            _insert_stored(connection, summary)
        payload = _canonical_document(summaries).encode("utf-8")
        outcome = RuntimeRunSummaryRebuild(
            record_count=len(summaries),
            build_digest=f"sha256:{sha256(payload).hexdigest()}",
        )
    return outcome


__all__ = [
    "RuntimeRunSummary",
    "RuntimeRunSummaryRebuild",
    "apply_runtime_run_summary_event",
    "read_runtime_run_summary",
    "rebuild_runtime_run_summaries",
    "runtime_run_summary_projection_bytes",
    "runtime_run_summary_projection_digest",
]
