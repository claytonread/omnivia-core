"""V06-5 S3 acceptance for migration 0015's immutable job bridges."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    assert_guards_intact,
    fenced_transaction,
    verify_fingerprint,
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
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 15
PREDECESSOR_VERSION = 14
MIGRATION_NAME = "0015_application_job_bridges.sql"
WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = 1_900_000_000_000_000
ERROR_JSON = (
    '{"code":"internal_recoverable","message":"interrupted",'
    '"retry_class":"retryable"}'
)
DIGEST = "sha256:" + "0" * 64

IMPORTS = "omnivia_application_import_claims"
CONTROLS = "omnivia_application_job_controls"
TERMINALS = "omnivia_job_terminal_observations"
TABLES = {IMPORTS, CONTROLS, TERMINALS}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def _apply_through(path: Path, version: int) -> None:
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(version):
        m1.bootstrap_and_migrate(path)


def _upgrade_from_14(path: Path) -> None:
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
        assert [m.version for m in applied] == [MIGRATION_VERSION]
    finally:
        connection.close()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, MIGRATION_VERSION)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def _guarded(holder: m1.Owned):
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def _insert(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def _audit(operation: str, audit_ref: str, at_us: int) -> dict[str, object]:
    return m1.row_for(
        "omnivia_application_audit_events",
        audit_ref=audit_ref,
        operation=operation,
        purpose="ingestion.import" if operation == "import.start" else "job.control",
        request_id=f"request-{audit_ref}",
        correlation_id=f"correlation-{audit_ref}",
        trace_id=f"trace-{audit_ref}",
        recorded_at_us=at_us,
    )


def _seed_running_job(
    holder: m1.Owned,
    *,
    job_id: str = "job-1",
    audit_ref: str = "audit-import",
) -> None:
    with _guarded(holder):
        _insert(
            holder.connection,
            "omnivia_application_audit_events",
            _audit("import.start", audit_ref, BASE_US),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', 'claimed', '{}', ?, ?, ?, ?)",
            (
                job_id,
                "2026-08-12T00:00:00Z",
                "2026-08-12T00:00:00Z",
                holder.generation,
                holder.identity.service_instance_id,
            ),
        )
        _insert(
            holder.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "job_kind": "ingestion.import",
                "originating_operation": "import.start",
                "audit_ref": audit_ref,
                "created_at_us": BASE_US,
                "terminal_result_kind": "import_completion",
                "supports_checkpoint_resume": 1,
                "max_attempts": 6,
            },
        )
        _insert(
            holder.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "attempt_number": 1,
                "started_at_us": BASE_US,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )
        _insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "sequence": 0,
                "occurred_at_us": BASE_US,
                "state": "running",
                "message": "started",
                "details_json": "{}",
            },
        )


def _fail_and_observe(holder: m1.Owned, *, at_us: int = BASE_US + 10) -> None:
    with _guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='failed', updated_at=? "
            "WHERE job_id='job-1'",
            ("2026-08-12T00:00:10Z",),
        )
        holder.connection.execute(
            "UPDATE omnivia_job_attempts SET state='failed', finished_at_us=?, "
            "error_json=? WHERE workspace_id=? AND job_id='job-1' "
            "AND attempt_number=1",
            (at_us, ERROR_JSON, WORKSPACE_ID),
        )
        _insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": at_us,
                "state": "failed",
                "message": "failed",
                "details_json": "{}",
            },
        )
        _insert(
            holder.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_observation_number": 1,
                "attempt_number": 1,
                "terminal_state": "failed",
                "finished_at_us": at_us,
                "result_kind": None,
                "result_json": None,
                "error_json": ERROR_JSON,
                "cancellation_reason": None,
                "provenance_kind": "service_committed",
                "fencing_generation": holder.generation,
            },
        )


def _accepted_retry(holder: m1.Owned, *, at_us: int = BASE_US + 20) -> None:
    with _guarded(holder):
        _insert(
            holder.connection,
            "omnivia_application_audit_events",
            _audit("job.retry", "audit-retry", at_us),
        )
        holder.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='queued', "
            "claimed_by_service_instance=NULL, updated_at=? WHERE job_id='job-1'",
            ("2026-08-12T00:00:20Z",),
        )
        _insert(
            holder.connection,
            CONTROLS,
            {
                "workspace_id": WORKSPACE_ID,
                "control_id": "control-retry",
                "job_id": "job-1",
                "control_kind": "user",
                "operation": "job.retry",
                "disposition": "retry_scheduled",
                "source_state": "failed",
                "resulting_state": "queued",
                "source_terminal_observation_number": 1,
                "audit_ref": "audit-retry",
                "fencing_generation": holder.generation,
                "control_json": '{"job_id":"job-1"}',
                "control_digest": DIGEST,
                "control_byte_length": len('{"job_id":"job-1"}'),
                "settled_at_us": at_us,
            },
        )
        _insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 2,
                "occurred_at_us": at_us,
                "state": "queued",
                "message": "retry scheduled",
                "details_json": "{}",
            },
        )


def _claim_and_start(
    holder: m1.Owned, number: int, *, at_us: int, sequence: int
) -> None:
    with _guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='claimed', "
            "claimed_by_service_instance=?, fencing_generation=?, updated_at=? "
            "WHERE job_id='job-1'",
            (
                holder.identity.service_instance_id,
                holder.generation,
                "2026-08-12T00:00:30Z",
            ),
        )
        _insert(
            holder.connection,
            "omnivia_job_attempts",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "attempt_number": number,
                "started_at_us": at_us,
                "finished_at_us": None,
                "state": "running",
                "error_json": None,
            },
        )
        _insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": sequence,
                "occurred_at_us": at_us,
                "state": "running",
                "message": "started",
                "details_json": "{}",
            },
        )


def test_v06_5_s3_0015_is_consecutive_and_has_exact_three_relations() -> None:
    assert MIGRATION.name == MIGRATION_NAME
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert [
        m.version for m in load_migrations() if m.version <= MIGRATION_VERSION
    ] == list(range(1, MIGRATION_VERSION + 1))
    created = set(
        __import__("re").findall(
            r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(omnivia_\w+)", MIGRATION.sql
        )
    )
    assert {name for name in created if not name.startswith("omnivia_migration_")} == TABLES


def test_v06_5_s3_0015_clean_upgrade_integrity_fingerprint_and_guards(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert applied_migrations(connection)[15] == MIGRATION.checksum
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        assert TABLES <= m1.object_names(connection, "table")
        assert not any(
            name.startswith("omnivia_migration_0015_")
            for kind in ("table", "trigger")
            for name in m1.object_names(connection, kind)
        )
        assert foreign_key_check(connection) == []
        assert integrity_check(connection) == []
        # Both oracles are taken from the accepted prefix through 0015. The guard
        # expectation is derived from whatever migrations are on disk, so read
        # outside this block it would demand the triggers of every *later*
        # migration from a workspace that deliberately stops here.
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            assert_guards_intact(connection)
            expected = canonical_schema_fingerprint()
        assert verify_fingerprint(connection, expected).matches(expected)
    finally:
        connection.close()


@pytest.mark.parametrize("stop_after", range(1, len(MIGRATION_STATEMENTS) + 1))
def test_v06_5_s3_0015_interrupted_apply_rolls_back_and_converges(
    tmp_path: Path, stop_after: int
) -> None:
    path = tmp_path / f"interrupted-{stop_after}.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        crashing = m1.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with (
            m1.migration_catalogue_through(MIGRATION_VERSION),
            pytest.raises(m1.MigrationInterrupted),
        ):
            apply_pending_migrations(
                cast("sqlite3.Connection", crashing),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=m1.GENERATION_ONE,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()

    interrupted = sqlite3.connect(path)
    try:
        assert MIGRATION_VERSION not in applied_migrations(interrupted)
        assert not (TABLES & m1.object_names(interrupted, "table"))
        assert foreign_key_check(interrupted) == []
        assert integrity_check(interrupted) == []
    finally:
        interrupted.close()
    _upgrade_from_14(path)
    converged = open_database(path, OpenMode.READ_ONLY)
    try:
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            expected = canonical_schema_fingerprint()
        assert fingerprint_schema(converged).matches(expected)
    finally:
        converged.close()


def test_v06_5_s3_old_binary_refuses_0015_workspace(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        with m1.migration_catalogue_through(PREDECESSOR_VERSION):
            old_expected = canonical_schema_fingerprint()
            with pytest.raises(SchemaDrift, match="fingerprint differs"):
                verify_fingerprint(connection, old_expected)
    finally:
        connection.close()


def test_v06_5_s3_0015_backfills_legacy_terminal_rows_exactly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-terminal.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    holder = m1.take_ownership(path)
    try:
        _seed_running_job(holder)
        with _guarded(holder):
            holder.connection.execute(
                "UPDATE omnivia_durable_jobs SET state='failed' WHERE job_id='job-1'"
            )
            holder.connection.execute(
                "UPDATE omnivia_job_attempts SET state='failed', finished_at_us=?, "
                "error_json=? WHERE workspace_id=? AND job_id='job-1'",
                (BASE_US + 10, ERROR_JSON, WORKSPACE_ID),
            )
            _insert(
                holder.connection,
                "omnivia_job_events",
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "sequence": 1,
                    "occurred_at_us": BASE_US + 10,
                    "state": "failed",
                    "message": "failed",
                    "details_json": "{}",
                },
            )
            _insert(
                holder.connection,
                "omnivia_job_terminal_results",
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "terminal_state": "failed",
                    "finished_at_us": BASE_US + 10,
                    "result_kind": None,
                    "result_json": None,
                    "error_json": ERROR_JSON,
                    "cancellation_reason": None,
                },
            )
    finally:
        holder.connection.close()

    _upgrade_from_14(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            f"SELECT terminal_observation_number, attempt_number, terminal_state, "
            f"finished_at_us, error_json, provenance_kind, fencing_generation "
            f"FROM {TERMINALS}"
        ).fetchone() == (
            1,
            1,
            "failed",
            BASE_US + 10,
            ERROR_JSON,
            "legacy_unrecorded",
            None,
        )
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_v06_5_s3_0015_freezes_legacy_terminal_inserts_and_provenance(
    owned: m1.Owned,
) -> None:
    _seed_running_job(owned)
    with _guarded(owned):
        with pytest.raises(sqlite3.IntegrityError, match="freezes new legacy"):
            _insert(
                owned.connection,
                "omnivia_job_terminal_results",
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "terminal_state": "failed",
                    "finished_at_us": BASE_US + 10,
                    "result_kind": None,
                    "result_json": None,
                    "error_json": ERROR_JSON,
                    "cancellation_reason": None,
                },
            )
        with pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
            _insert(
                owned.connection,
                TERMINALS,
                {
                    "workspace_id": WORKSPACE_ID,
                    "job_id": "job-1",
                    "terminal_observation_number": 1,
                    "attempt_number": 1,
                    "terminal_state": "failed",
                    "finished_at_us": BASE_US + 10,
                    "result_kind": None,
                    "result_json": None,
                    "error_json": ERROR_JSON,
                    "cancellation_reason": None,
                    "provenance_kind": "legacy_unrecorded",
                    "fencing_generation": None,
                },
            )


def test_v06_5_s3_import_claim_requires_exact_verified_source_and_is_immutable(
    owned: m1.Owned,
) -> None:
    input_json = '{"source":{"staged_source_ref":"stg-0001"}}'
    digest = "sha256:" + hashlib.sha256(input_json.encode()).hexdigest()
    with _guarded(owned):
        m2.insert(
            owned.connection,
            m2.BLOBS,
            m2.row_for(m2.BLOBS, workspace_id=WORKSPACE_ID),
        )
        m2.insert(
            owned.connection,
            m2.STAGED,
            m2.row_for(
                m2.STAGED,
                workspace_id=WORKSPACE_ID,
                blob_workspace_id=WORKSPACE_ID,
            ),
        )
        _insert(
            owned.connection,
            "omnivia_application_audit_events",
            _audit("import.start", "audit-import", BASE_US),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES ('job-1', 'ingestion.import', 'queued', '{}', 'c', 'u', ?, NULL)",
            (owned.generation,),
        )
        _insert(
            owned.connection,
            "omnivia_job_application_metadata",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "job_kind": "ingestion.import",
                "originating_operation": "import.start",
                "audit_ref": "audit-import",
                "created_at_us": BASE_US,
                "terminal_result_kind": "import_completion",
                "supports_checkpoint_resume": 1,
                "max_attempts": 3,
            },
        )
        claim = {
            "workspace_id": WORKSPACE_ID,
            "job_id": "job-1",
            "audit_ref": "audit-import",
            "staged_source_ref": m2.STAGED_DEFAULTS["staged_source_ref"],
            "source_kind": m2.STAGED_DEFAULTS["source_kind"],
            "content_checksum": m2.STAGED_DEFAULTS["declared_checksum"],
            "content_length_bytes": m2.STAGED_DEFAULTS["content_length_bytes"],
            "media_type": m2.STAGED_DEFAULTS["media_type"],
            "source_version": m2.STAGED_DEFAULTS["source_version"],
            "input_json": input_json,
            "input_digest": digest,
            "input_byte_length": len(input_json),
            "settled_at_us": BASE_US,
        }
        bad = dict(claim, content_length_bytes=1025)
        with pytest.raises(sqlite3.IntegrityError, match="exact verified staged"):
            _insert(owned.connection, IMPORTS, bad)
        _insert(owned.connection, IMPORTS, claim)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(
                f"UPDATE {IMPORTS} SET media_type='text/plain'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"DELETE FROM {IMPORTS}")


def test_v06_5_s3_failed_retry_attempt_2_succeeded_is_append_only(
    owned: m1.Owned,
) -> None:
    _seed_running_job(owned)
    _fail_and_observe(owned)
    _accepted_retry(owned)
    _claim_and_start(owned, 2, at_us=BASE_US + 30, sequence=3)
    with _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='succeeded' WHERE job_id='job-1'"
        )
        owned.connection.execute(
            "UPDATE omnivia_job_attempts SET state='succeeded', finished_at_us=? "
            "WHERE workspace_id=? AND job_id='job-1' AND attempt_number=2",
            (BASE_US + 40, WORKSPACE_ID),
        )
        _insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 4,
                "occurred_at_us": BASE_US + 40,
                "state": "succeeded",
                "message": "complete",
                "details_json": "{}",
            },
        )
        _insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_observation_number": 2,
                "attempt_number": 2,
                "terminal_state": "succeeded",
                "finished_at_us": BASE_US + 40,
                "result_kind": "import_completion",
                "result_json": '{"import_run_id":"job-1"}',
                "error_json": None,
                "cancellation_reason": None,
                "provenance_kind": "service_committed",
                "fencing_generation": owned.generation,
            },
        )
    assert owned.connection.execute(
        f"SELECT terminal_state, attempt_number FROM {TERMINALS} "
        "ORDER BY terminal_observation_number"
    ).fetchall() == [("failed", 1), ("succeeded", 2)]
    with (
        _guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        owned.connection.execute(f"DELETE FROM {TERMINALS}")


def test_v06_5_s3_system_recovery_lineage_enables_next_attempt(
    owned: m1.Owned,
) -> None:
    _seed_running_job(owned)
    recovered_at = BASE_US + 10
    with _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='queued', "
            "claimed_by_service_instance=NULL WHERE job_id='job-1'"
        )
        owned.connection.execute(
            "UPDATE omnivia_job_attempts SET state='failed', finished_at_us=?, "
            "error_json=? WHERE workspace_id=? AND job_id='job-1' "
            "AND attempt_number=1",
            (recovered_at, ERROR_JSON, WORKSPACE_ID),
        )
        _insert(
            owned.connection,
            CONTROLS,
            {
                "workspace_id": WORKSPACE_ID,
                "control_id": "control-system-recovery",
                "job_id": "job-1",
                "control_kind": "system",
                "operation": "system.recovery",
                "disposition": "recovery_requeued",
                "source_state": "running",
                "resulting_state": "queued",
                "source_terminal_observation_number": None,
                "audit_ref": None,
                "fencing_generation": owned.generation,
                "control_json": '{"reason":"stranded"}',
                "control_digest": DIGEST,
                "control_byte_length": len('{"reason":"stranded"}'),
                "settled_at_us": recovered_at,
            },
        )
        _insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 1,
                "occurred_at_us": recovered_at,
                "state": "queued",
                "message": "recovered",
                "details_json": "{}",
            },
        )
    _claim_and_start(owned, 2, at_us=BASE_US + 20, sequence=2)
    assert owned.connection.execute(
        "SELECT control_kind, operation, disposition, audit_ref FROM "
        f"{CONTROLS}"
    ).fetchone() == (
        "system",
        "system.recovery",
        "recovery_requeued",
        None,
    )


def test_v06_5_s3_queued_cancel_after_retry_creates_cancelled_next_attempt(
    owned: m1.Owned,
) -> None:
    _seed_running_job(owned)
    _fail_and_observe(owned)
    _accepted_retry(owned)
    cancel_at = BASE_US + 21
    with _guarded(owned):
        _insert(
            owned.connection,
            "omnivia_application_audit_events",
            _audit("job.cancel", "audit-cancel", cancel_at),
        )
        _insert(
            owned.connection,
            CONTROLS,
            {
                "workspace_id": WORKSPACE_ID,
                "control_id": "control-cancel",
                "job_id": "job-1",
                "control_kind": "user",
                "operation": "job.cancel",
                "disposition": "cancellation_requested",
                "source_state": "queued",
                "resulting_state": "queued",
                "source_terminal_observation_number": None,
                "audit_ref": "audit-cancel",
                "fencing_generation": owned.generation,
                "control_json": '{"job_id":"job-1"}',
                "control_digest": DIGEST,
                "control_byte_length": len('{"job_id":"job-1"}'),
                "settled_at_us": cancel_at,
            },
        )
        _insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 3,
                "occurred_at_us": cancel_at,
                "state": "queued",
                "message": "cancellation requested",
                "details_json": "{}",
            },
        )
    _claim_and_start(owned, 2, at_us=BASE_US + 22, sequence=4)
    finish = BASE_US + 22
    with _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='cancelled' WHERE job_id='job-1'"
        )
        owned.connection.execute(
            "UPDATE omnivia_job_attempts SET state='cancelled', finished_at_us=? "
            "WHERE workspace_id=? AND job_id='job-1' AND attempt_number=2",
            (finish, WORKSPACE_ID),
        )
        _insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "sequence": 5,
                "occurred_at_us": finish,
                "state": "cancelled",
                "message": "cancelled",
                "details_json": "{}",
            },
        )
        _insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": "job-1",
                "terminal_observation_number": 2,
                "attempt_number": 2,
                "terminal_state": "cancelled",
                "finished_at_us": finish,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "requested",
                "provenance_kind": "service_committed",
                "fencing_generation": owned.generation,
            },
        )
    assert owned.connection.execute(
        f"SELECT terminal_state, attempt_number FROM {TERMINALS} "
        "ORDER BY terminal_observation_number"
    ).fetchall() == [("failed", 1), ("cancelled", 2)]


@pytest.mark.parametrize("table", sorted(TABLES))
def test_v06_5_s3_new_relations_have_unconditional_append_only_guards(
    owned: m1.Owned, table: str
) -> None:
    sql = {
        str(row[0])
        for row in owned.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    assert any("UPDATE" in item and "append-only" in item for item in sql)
    assert any("DELETE" in item and "append-only" in item for item in sql)
