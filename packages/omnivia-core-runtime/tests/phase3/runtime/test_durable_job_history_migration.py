"""V06-1 M4 acceptance for durable application job history."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import (
    close_guard,
    fenced_transaction,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 10
MIGRATION_NAME = "0010_durable_job_history.sql"
WORKSPACE_ID = m2.WORKSPACE_ID
BASE_US = m3.BASE_US + 1000

TABLES = {
    "omnivia_job_application_metadata",
    "omnivia_job_attempts",
    "omnivia_job_progress_events",
    "omnivia_job_checkpoints",
    "omnivia_job_events",
    "omnivia_job_terminal_results",
}

INDEXES = {
    "omnivia_idx_job_application_metadata_audit",
    "omnivia_idx_job_attempts_latest",
    "omnivia_idx_job_attempts_state",
    "omnivia_idx_job_progress_latest",
    "omnivia_idx_job_progress_attempt",
    "omnivia_idx_job_checkpoints_latest",
    "omnivia_idx_job_events_page",
    "omnivia_idx_job_events_state",
    "omnivia_idx_job_terminal_results_state",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{verb}"
    for table in TABLES
    for verb in ("insert", "update", "delete")
}
TRIGGERS.add("omnivia_guard_durable_jobs_application_metadata_update")

ACCEPTED_PREDECESSOR_HASHES = {
    **m3.ACCEPTED_PREDECESSOR_HASHES,
    9: "dd3af667fb7d59ccb53a59fc46264932220a3af28f118cb4c2a934cd3a5bb387",
}


def migration_under_test() -> Any:
    found = [
        migration
        for migration in load_migrations()
        if migration.version == MIGRATION_VERSION
    ]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))

M4_RETRY_CHILD = """
import json
import sys
from pathlib import Path
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations, read_workspace_state,
)

path, service, workspace_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result = {
        "applied": [migration.version for migration in applied],
        "integrity": integrity_check(connection),
        "foreign_keys": foreign_key_check(connection),
    }
finally:
    connection.close()
print(json.dumps(result, sort_keys=True))
"""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    yield holder
    holder.connection.close()


def guarded(holder: m2.Owned):
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def seed_job(
    holder: m2.Owned,
    *,
    job_id: str = "job-1",
    state: str = "queued",
    resumable: int = 1,
    max_attempts: int = 3,
    event_state: str | None = None,
) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_application_audit_events",
            m3.audit_row(f"audit-{job_id}"),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', ?, '{}', '2026-08-03T00:00:00Z', "
            "'2026-08-03T00:00:00Z', ?, ?)",
            (
                job_id,
                state,
                holder.generation,
                holder.identity.service_instance_id if state == "claimed" else None,
            ),
        )
        insert(
            holder.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "job_kind": "ingestion.import",
                "originating_operation": "import.start",
                "audit_ref": f"audit-{job_id}",
                "created_at_us": BASE_US,
                "terminal_result_kind": "import_completion",
                "supports_checkpoint_resume": resumable,
                "max_attempts": max_attempts,
            },
        )
        insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "sequence": 0,
                "occurred_at_us": BASE_US,
                "state": event_state or ("running" if state == "claimed" else state),
                "message": "created",
                "details_json": "{}",
            },
        )


def start_attempt(holder: m2.Owned, number: int = 1) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": number,
                "started_at_us": BASE_US + number,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )


def set_scheduler_state(holder: m2.Owned, state: str) -> None:
    holder.connection.execute(
        "UPDATE omnivia_durable_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
        (state, f"2026-08-03T00:00:0{len(state)}Z", "job-1"),
    )


def finish_attempt(holder: m2.Owned, state: str, *, finished_at: int) -> None:
    with guarded(holder):
        set_scheduler_state(holder, state)
        holder.connection.execute(
            "UPDATE omnivia_job_attempts SET state = ?, finished_at_us = ?, "
            "error_json = ? WHERE workspace_id = ? AND job_id = ? "
            "AND attempt_number = 1",
            (
                state,
                finished_at,
                '{"code":"failed"}' if state == "failed" else None,
                WORKSPACE_ID,
                "job-1",
            ),
        )


def test_m4_01_consecutive_lineage_and_predecessor_hashes() -> None:
    migrations = load_migrations()
    assert [migration.version for migration in migrations] == list(range(1, 11))
    assert migrations[-1].name == MIGRATION_NAME
    assert {m.version: m.checksum for m in migrations[:-1]} == (
        ACCEPTED_PREDECESSOR_HASHES
    )
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode()).hexdigest()


def test_m4_02_pristine_convergence_and_exact_inventory(owned: m2.Owned) -> None:
    assert max(applied_migrations(owned.connection)) == MIGRATION_VERSION
    assert owned.connection.execute("PRAGMA user_version").fetchone() == (10,)
    before = sqlite3.connect(":memory:")
    try:
        before.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version <= 9:
                before.executescript(migration.sql)
        assert (
            m2.object_names(owned.connection, "table")
            - m2.object_names(before, "table")
            == TABLES
        )
        assert (
            m2.object_names(owned.connection, "index")
            - m2.object_names(before, "index")
            == INDEXES
        )
        assert (
            m2.object_names(owned.connection, "trigger")
            - m2.object_names(before, "trigger")
            == TRIGGERS
        )
        assert m2.object_names(owned.connection, "view") == m2.object_names(
            before, "view"
        )
    finally:
        before.close()
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    assert fingerprint_schema(owned.connection)


def test_m4_03_legacy_job_is_preserved_without_synthetic_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(9):
        m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at) "
            "VALUES ('legacy-job', 'legacy.work', 'queued', '{\"legacy\":true}', "
            "'old-created', 'old-updated')"
        )
    holder.connection.close()
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    excluded = {"omnivia_schema_migrations", "omnivia_migration_attempts"}
    preserved_tables = m2.object_names(connection, "table") - excluded
    before_rows = {
        table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
        for table in preserved_tables
    }
    state = m2.read_workspace_state(connection)
    apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=m2.SERVICE_INSTANCE,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    assert connection.execute(
        "SELECT job_type, state, payload_json, created_at, updated_at "
        "FROM omnivia_durable_jobs WHERE job_id = 'legacy-job'"
    ).fetchone() == (
        "legacy.work",
        "queued",
        '{"legacy":true}',
        "old-created",
        "old-updated",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_application_metadata "
        "WHERE job_id = 'legacy-job'"
    ).fetchone() == (0,)
    assert {
        table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
        for table in preserved_tables
    } == before_rows
    connection.close()


def test_m4_04_metadata_requires_matching_job_and_audit_workspace(
    owned: m2.Owned,
) -> None:
    seed_job(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="kind"):
        insert(
            owned.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "job_kind": "wrong.kind",
                "originating_operation": "import.start",
                "audit_ref": "audit-job-1",
                "created_at_us": BASE_US,
                "terminal_result_kind": None,
                "supports_checkpoint_resume": 0,
                "max_attempts": 1,
            },
        )

    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_application_audit_events",
            m3.audit_row("audit-job-2"),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation) VALUES "
            "('job-2', 'ingestion.import', 'queued', '{}', 'created', 'updated', ?)",
            (owned.generation,),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="audit"):
        insert(
            owned.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-2",
                "job_kind": "ingestion.import",
                "originating_operation": "import.start",
                "audit_ref": "missing-audit",
                "created_at_us": BASE_US,
                "terminal_result_kind": None,
                "supports_checkpoint_resume": 0,
                "max_attempts": 1,
            },
        )

    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_application_audit_events",
            m3.audit_row("bad\nref"),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation) VALUES "
            "('job-3', 'ingestion.import', 'queued', '{}', 'created', 'updated', ?)",
            (owned.generation,),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-3",
                "job_kind": "ingestion.import",
                "originating_operation": "import.start",
                "audit_ref": "bad\nref",
                "created_at_us": BASE_US,
                "terminal_result_kind": None,
                "supports_checkpoint_resume": 0,
                "max_attempts": 1,
            },
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("supports_checkpoint_resume", -1),
        ("supports_checkpoint_resume", 2),
        ("max_attempts", 0),
        ("max_attempts", 257),
        ("created_at_us", 0),
        ("originating_operation", "Bad Operation"),
    ],
)
def test_m4_04b_metadata_scalar_bounds_are_fail_closed(
    owned: m2.Owned, column: str, value: object
) -> None:
    job_id = f"job-invalid-{column}-{str(value).replace(' ', '-')}"
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_application_audit_events",
            m3.audit_row(f"audit-{job_id}"),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation) VALUES (?, 'ingestion.import', 'queued', '{}', "
            "'created', 'updated', ?)",
            (job_id, owned.generation),
        )
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "job_id": job_id,
        "job_kind": "ingestion.import",
        "originating_operation": "import.start",
        "audit_ref": f"audit-{job_id}",
        "created_at_us": BASE_US,
        "terminal_result_kind": None,
        "supports_checkpoint_resume": 1,
        "max_attempts": 3,
    }
    row[column] = value
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned.connection, "omnivia_job_application_metadata", row)


def test_m4_05_attempts_are_contiguous_and_only_retry_failures(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": 2,
                "started_at_us": BASE_US + 1,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )
    start_attempt(owned)
    finish_attempt(owned, "failed", finished_at=BASE_US + 2)
    with guarded(owned):
        set_scheduler_state(owned, "claimed")
        insert(
            owned.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": 2,
                "started_at_us": BASE_US + 3,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )


def test_m4_05b_attempt_insert_shape_and_retry_budget_are_enforced(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed", max_attempts=1)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="running state"):
        insert(
            owned.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": 1,
                "started_at_us": BASE_US + 1,
                "finished_at_us": BASE_US + 2,
                "state": "failed",
                "error_json": '{"code":"failed"}',
            },
        )
    start_attempt(owned)
    finish_attempt(owned, "failed", finished_at=BASE_US + 2)
    with guarded(owned):
        set_scheduler_state(owned, "claimed")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="budget"):
        insert(
            owned.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": 2,
                "started_at_us": BASE_US + 3,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )


def test_m4_06_attempt_terminalization_is_one_way_and_state_bound(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    finish_attempt(owned, "succeeded", finished_at=BASE_US + 2)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="terminalization"):
        owned.connection.execute(
            "UPDATE omnivia_job_attempts SET finished_at_us = ? "
            "WHERE workspace_id = ? AND job_id = ? AND attempt_number = 1",
            (BASE_US + 3, WORKSPACE_ID, "job-1"),
        )


def test_m4_07_progress_is_monotonic_and_bound_to_running_attempt(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    with guarded(owned):
        for sequence, completed in ((0, 1), (1, 2)):
            insert(
                owned.connection,
                "omnivia_job_progress_events",
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "progress_sequence": sequence,
                    "attempt_number": 1,
                    "occurred_at_us": BASE_US + 2 + sequence,
                    "unit": "item",
                    "completed_units": completed,
                    "total_units": 3,
                    "message": None,
                },
            )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="regressed"):
        insert(
            owned.connection,
            "omnivia_job_progress_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "progress_sequence": 2,
                "attempt_number": 1,
                "occurred_at_us": BASE_US + 4,
                "unit": "item",
                "completed_units": 1,
                "total_units": 3,
                "message": None,
            },
        )

    for changed in (
        {"unit": "page", "completed_units": 3, "total_units": 3},
        {"unit": "item", "completed_units": 3, "total_units": 4},
        {"unit": "item", "completed_units": 4, "total_units": 3},
    ):
        row = {
            "workspace_id": WORKSPACE_ID,
            "job_id": "job-1",
            "progress_sequence": 2,
            "attempt_number": 1,
            "occurred_at_us": BASE_US + 4,
            "unit": "item",
            "completed_units": 3,
            "total_units": 3,
            "message": None,
            **changed,
        }
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_job_progress_events", row)


def test_m4_08_checkpoint_requires_resumable_running_attempt(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed", resumable=0)
    start_attempt(owned)
    row = {
        "workspace_id": WORKSPACE_ID,
        "job_id": "job-1",
        "checkpoint_sequence": 0,
        "attempt_number": 1,
        "created_at_us": BASE_US + 2,
        "checkpoint_kind": "import.cursor",
        "checkpoint_json": "{}",
        "checkpoint_digest": "sha256:" + "a" * 64,
    }
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="resumable"):
        insert(owned.connection, "omnivia_job_checkpoints", row)


def test_m4_08b_checkpoint_sequence_is_contiguous_for_resumable_job(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed", resumable=1)
    start_attempt(owned)
    with guarded(owned):
        for sequence in (0, 1):
            insert(
                owned.connection,
                "omnivia_job_checkpoints",
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "checkpoint_sequence": sequence,
                    "attempt_number": 1,
                    "created_at_us": BASE_US + 2 + sequence,
                    "checkpoint_kind": "import.cursor",
                    "checkpoint_json": "{}",
                    "checkpoint_digest": "sha256:" + "a" * 64,
                },
            )
    assert owned.connection.execute(
        "SELECT checkpoint_sequence FROM omnivia_job_checkpoints ORDER BY checkpoint_sequence"
    ).fetchall() == [(0,), (1,)]

    invalid_digest = {
        "workspace_id": WORKSPACE_ID,
        "job_id": "job-1",
        "checkpoint_sequence": 2,
        "attempt_number": 1,
        "created_at_us": BASE_US + 4,
        "checkpoint_kind": "import.cursor",
        "checkpoint_json": "{}",
        "checkpoint_digest": "sha256:" + "A" * 64,
    }
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned.connection, "omnivia_job_checkpoints", invalid_digest)


def test_m4_09_events_are_zero_based_contiguous_and_never_claimed(
    owned: m2.Owned,
) -> None:
    seed_job(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 2,
                "occurred_at_us": BASE_US + 1,
                "state": "queued",
                "message": None,
                "details_json": None,
            },
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": BASE_US + 1,
                "state": "claimed",
                "message": None,
                "details_json": None,
            },
        )


def test_m4_10_terminal_success_is_exclusive_and_final(owned: m2.Owned) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    finish_attempt(owned, "succeeded", finished_at=BASE_US + 2)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": BASE_US + 2,
                "state": "succeeded",
                "message": "done",
                "details_json": "{}",
            },
        )
        insert(
            owned.connection,
            "omnivia_job_terminal_results",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_state": "succeeded",
                "finished_at_us": BASE_US + 2,
                "result_kind": "import_completion",
                "result_json": "{}",
                "error_json": None,
                "cancellation_reason": None,
            },
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="final"):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 2,
                "occurred_at_us": BASE_US + 3,
                "state": "succeeded",
                "message": None,
                "details_json": None,
            },
        )


def test_m4_10b_terminal_failure_repeats_final_attempt_error(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    finish_attempt(owned, "failed", finished_at=BASE_US + 2)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": BASE_US + 2,
                "state": "failed",
                "message": None,
                "details_json": None,
            },
        )
    bad = {
        "workspace_id": WORKSPACE_ID,
        "job_id": "job-1",
        "terminal_state": "failed",
        "finished_at_us": BASE_US + 2,
        "result_kind": None,
        "result_json": None,
        "error_json": '{"code":"different"}',
        "cancellation_reason": None,
    }
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="repeat"):
        insert(owned.connection, "omnivia_job_terminal_results", bad)
    good = {**bad, "error_json": '{"code":"failed"}'}
    with guarded(owned):
        insert(owned.connection, "omnivia_job_terminal_results", good)


def test_m4_10c_queued_job_may_cancel_without_an_attempt(owned: m2.Owned) -> None:
    seed_job(owned, state="cancelled", event_state="cancelled")
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_terminal_results",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_state": "cancelled",
                "finished_at_us": BASE_US,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "caller_requested",
            },
        )


@pytest.mark.parametrize(
    "table",
    [
        "omnivia_job_application_metadata",
        "omnivia_job_progress_events",
        "omnivia_job_checkpoints",
        "omnivia_job_events",
        "omnivia_job_terminal_results",
    ],
)
def test_m4_11_append_only_tables_refuse_update_and_delete(
    owned: m2.Owned, table: str
) -> None:
    if table == "omnivia_job_terminal_results":
        seed_job(owned, state="cancelled", event_state="cancelled")
        with guarded(owned):
            insert(
                owned.connection,
                table,
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "terminal_state": "cancelled",
                    "finished_at_us": BASE_US,
                    "result_kind": None,
                    "result_json": None,
                    "error_json": None,
                    "cancellation_reason": "caller_requested",
                },
            )
    else:
        seed_job(owned, state="claimed")
        if table in {"omnivia_job_progress_events", "omnivia_job_checkpoints"}:
            start_attempt(owned)
            row = (
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "progress_sequence": 0,
                    "attempt_number": 1,
                    "occurred_at_us": BASE_US + 2,
                    "unit": "item",
                    "completed_units": 1,
                    "total_units": 2,
                    "message": None,
                }
                if table == "omnivia_job_progress_events"
                else {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "checkpoint_sequence": 0,
                    "attempt_number": 1,
                    "created_at_us": BASE_US + 2,
                    "checkpoint_kind": "import.cursor",
                    "checkpoint_json": "{}",
                    "checkpoint_digest": "sha256:" + "a" * 64,
                }
            )
            with guarded(owned):
                insert(owned.connection, table, row)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="DELETE"):
        owned.connection.execute(f"DELETE FROM {table}")


def test_m4_11b_attempt_rows_refuse_same_value_update_and_delete(
    owned: m2.Owned,
) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="terminalization"):
        owned.connection.execute(
            "UPDATE omnivia_job_attempts SET state = state WHERE job_id = 'job-1'"
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="DELETE"):
        owned.connection.execute(
            "DELETE FROM omnivia_job_attempts WHERE job_id = 'job-1'"
        )


@pytest.mark.parametrize("table", sorted(TABLES))
def test_m4_12_stock_sqlite_and_closed_guard_fail_closed(
    owned: m2.Owned, table: str
) -> None:
    if table == "omnivia_job_terminal_results":
        seed_job(owned, state="cancelled", event_state="cancelled")
        with guarded(owned):
            insert(
                owned.connection,
                table,
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "terminal_state": "cancelled",
                    "finished_at_us": BASE_US,
                    "result_kind": None,
                    "result_json": None,
                    "error_json": None,
                    "cancellation_reason": "caller_requested",
                },
            )
    else:
        seed_job(owned, state="claimed")
        if table == "omnivia_job_attempts":
            start_attempt(owned)
        elif table == "omnivia_job_progress_events":
            start_attempt(owned)
            with guarded(owned):
                insert(
                    owned.connection,
                    table,
                    {
                        "workspace_id": WORKSPACE_ID,
                        "job_id": "job-1",
                        "progress_sequence": 0,
                        "attempt_number": 1,
                        "occurred_at_us": BASE_US + 2,
                        "unit": "item",
                        "completed_units": 1,
                        "total_units": 2,
                        "message": None,
                    },
                )
        elif table == "omnivia_job_checkpoints":
            start_attempt(owned)
            with guarded(owned):
                insert(
                    owned.connection,
                    table,
                    {
                        "workspace_id": WORKSPACE_ID,
                        "job_id": "job-1",
                        "checkpoint_sequence": 0,
                        "attempt_number": 1,
                        "created_at_us": BASE_US + 2,
                        "checkpoint_kind": "import.cursor",
                        "checkpoint_json": "{}",
                        "checkpoint_digest": "sha256:" + "a" * 64,
                    },
                )
    close_guard(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded"):
        owned.connection.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
    owned.connection.close()
    outsider = sqlite3.connect(owned.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="no such function"):
            outsider.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
    finally:
        outsider.close()


def test_m4_13_failed_multi_table_assembly_rolls_back(owned: m2.Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError), guarded(owned):
        insert(
            owned.connection,
            "omnivia_application_audit_events",
            m3.audit_row("audit-atomic"),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, created_at, updated_at) "
            "VALUES ('job-atomic', 'ingestion.import', 'queued', 'x', 'x')"
        )
        insert(
            owned.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-atomic",
                "job_kind": "wrong.kind",
                "originating_operation": "import.start",
                "audit_ref": "audit-atomic",
                "created_at_us": BASE_US,
                "terminal_result_kind": None,
                "supports_checkpoint_resume": 0,
                "max_attempts": 1,
            },
        )
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_durable_jobs WHERE job_id = 'job-atomic'"
    ).fetchone() == (0,)


def test_m4_13b_job_kind_is_immutable_after_metadata(owned: m2.Owned) -> None:
    seed_job(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="immutable"):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET job_type = 'changed.kind' WHERE job_id = 'job-1'"
        )
    assert owned.connection.execute(
        "SELECT job_type FROM omnivia_durable_jobs WHERE job_id = 'job-1'"
    ).fetchone() == ("ingestion.import",)


def test_m4_14_interruption_rolls_back_every_statement(tmp_path: Path) -> None:
    base = tmp_path / "through-0009.sqlite"
    materialise_phase0_baseline(base)
    with m2.migration_catalogue_through(9):
        m2.bootstrap_and_migrate(base)
    for stop_after in range(1, len(MIGRATION_STATEMENTS) + 1):
        path = tmp_path / f"interrupted-{stop_after}.sqlite"
        shutil.copy2(base, path)
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        state = m2.read_workspace_state(connection)
        crashing = m2.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with pytest.raises(m2.MigrationInterrupted):
            apply_pending_migrations(
                crashing,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
        connection.close()
        check = open_database(path, OpenMode.READ_ONLY)
        assert MIGRATION_VERSION not in applied_migrations(check)
        assert TABLES.isdisjoint(m2.object_names(check, "table"))
        check.close()
        result = m2.run_child(
            M4_RETRY_CHILD,
            str(path),
            m2.SERVICE_INSTANCE,
            WORKSPACE_ID,
        )
        assert result == {
            "applied": [MIGRATION_VERSION],
            "foreign_keys": [],
            "integrity": [],
        }


def test_m4_15_snapshot_count_excludes_later_events(owned: m2.Owned) -> None:
    seed_job(owned)
    snapshot = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_events WHERE workspace_id = ? AND job_id = ?",
        (WORKSPACE_ID, "job-1"),
    ).fetchone()[0]
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": BASE_US + 1,
                "state": "queued",
                "message": "later",
                "details_json": None,
            },
        )
    captured = owned.connection.execute(
        "SELECT sequence FROM omnivia_job_events WHERE workspace_id = ? "
        "AND job_id = ? AND sequence < ? ORDER BY sequence",
        (WORKSPACE_ID, "job-1", snapshot),
    ).fetchall()
    assert snapshot == 1
    assert captured == [(0,)]


def test_m4_16_verified_backup_preserves_every_durable_job_fact(
    owned: m2.Owned, tmp_path: Path
) -> None:
    seed_job(owned, state="claimed")
    start_attempt(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_progress_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "progress_sequence": 0,
                "attempt_number": 1,
                "occurred_at_us": BASE_US + 2,
                "unit": "item",
                "completed_units": 1,
                "total_units": 1,
                "message": "done",
            },
        )
        insert(
            owned.connection,
            "omnivia_job_checkpoints",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "checkpoint_sequence": 0,
                "attempt_number": 1,
                "created_at_us": BASE_US + 2,
                "checkpoint_kind": "import.cursor",
                "checkpoint_json": "{}",
                "checkpoint_digest": "sha256:" + "a" * 64,
            },
        )
    finish_attempt(owned, "succeeded", finished_at=BASE_US + 3)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": BASE_US + 3,
                "state": "succeeded",
                "message": "done",
                "details_json": "{}",
            },
        )
        insert(
            owned.connection,
            "omnivia_job_terminal_results",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_state": "succeeded",
                "finished_at_us": BASE_US + 3,
                "result_kind": "import_completion",
                "result_json": "{}",
                "error_json": None,
                "cancellation_reason": None,
            },
        )
    owned.connection.close()
    installation = m3.InstallationLayout(root=tmp_path / "installation")
    installation.create(WORKSPACE_ID)
    backup = m3.create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=m3.new_attempt_id(),
    )
    assert backup.verified
    restored = open_database(backup.path, OpenMode.READ_ONLY)
    try:
        assert {
            table: restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in TABLES
        } == {
            "omnivia_job_application_metadata": 1,
            "omnivia_job_attempts": 1,
            "omnivia_job_progress_events": 1,
            "omnivia_job_checkpoints": 1,
            "omnivia_job_events": 2,
            "omnivia_job_terminal_results": 1,
        }
        assert integrity_check(restored) == []
        assert foreign_key_check(restored) == []
    finally:
        restored.close()
