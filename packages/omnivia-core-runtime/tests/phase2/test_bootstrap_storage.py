"""T-0629B acceptance: versioned storage and Generation-1 bootstrap.

Covers the bootstrap half of the BD group (BD-01 … BD-06, BD-12) plus the storage
configuration and migration-ledger requirements. The discovery half (BD-07 … BD-11)
needs OS locks and lands with T-0629D/E.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    SchemaCreationRefused,
    StorageError,
    fingerprint_schema,
    fingerprint_sql_script,
    foreign_key_check,
    immediate_transaction,
    integrity_check,
    open_database,
    table_names,
)
from omnivia_core_runtime.storage.migrations import (
    BASELINE_ADOPTED,
    BASELINE_PRISTINE,
    GENERATION_ONE,
    applied_migrations,
    apply_pending_migrations,
    bootstrap_generation_one,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    phase0_fingerprint,
    read_workspace_state,
    record_open_event,
    substrate_present,
)

WORKSPACE_ID = "ws-0000000000000001"
INSTANCE = "svc-instance-1"

SUBSTRATE_TABLES = {
    "omnivia_workspace_state",
    "omnivia_schema_migrations",
    "omnivia_migration_attempts",
    "omnivia_workspace_open_events",
    "omnivia_workspace_lease",
    "omnivia_projection_ledger",
    "omnivia_durable_jobs",
}


class Boom(RuntimeError):
    """Simulated crash."""


class CrashAfter:
    """Forwarding connection proxy that raises after N executed statements.

    A proxy rather than a monkeypatch because `sqlite3.Connection.execute` is a
    read-only attribute. Everything except `execute` is delegated to the real
    connection, so the code under test runs against genuine SQLite semantics —
    which matters here, since the property being proven is transactional rollback.
    """

    def __init__(self, connection: sqlite3.Connection, fail_after: int) -> None:
        self._connection = connection
        self._fail_after = fail_after
        self.executed = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self.executed >= self._fail_after:
            raise Boom(f"crash after statement {self._fail_after}")
        self.executed += 1
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def pristine(path: Path) -> sqlite3.Connection:
    return open_database(path, OpenMode.EPHEMERAL)


def adopted(path: Path) -> sqlite3.Connection:
    """A database carrying exactly the frozen Phase 0 schema, opened exclusively."""
    materialise_phase0_baseline(path)
    return open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)


# BD-01
def test_bd01_generation_one_bootstrap_creates_substrate_exactly_once(
    tmp_path: Path,
) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        assert not substrate_present(connection)
        first = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        assert first.fencing_generation == GENERATION_ONE
        assert first.baseline_state == BASELINE_PRISTINE
        assert SUBSTRATE_TABLES.issubset(set(table_names(connection)))

        # BD-06: a second call must not re-bootstrap or bump the generation.
        second = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        assert second == first
        rows = connection.execute(
            "SELECT COUNT(*) FROM omnivia_workspace_state"
        ).fetchone()
        assert rows is not None and rows[0] == 1
    finally:
        connection.close()


def test_bd01b_workspace_state_cannot_hold_a_second_row(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        # Two rows would mean two competing generations for one workspace.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO omnivia_workspace_state (singleton, workspace_id, "
                "fencing_generation, workspace_format_version, baseline_state, "
                "created_at, updated_at) VALUES (2, 'other', 1, '1', 'x', 'n', 'n')"
            )
    finally:
        connection.close()


# BD-02
def test_bd02_bootstrap_requires_exact_phase0_schema_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    connection = adopted(path)
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
        )
        assert state.baseline_state == BASELINE_ADOPTED
    finally:
        connection.close()

    # A database that is *not* the frozen baseline must be refused.
    other = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other)
    tampered = open_database(other, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        tampered.execute("CREATE TABLE surprise (id TEXT PRIMARY KEY)")
        with pytest.raises(StorageError, match="Phase 0 baseline"):
            bootstrap_generation_one(
                tampered,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
            )
        assert not substrate_present(tampered), "a refused adopt creates nothing"
    finally:
        tampered.close()


def test_bd02b_phase0_fingerprint_is_independent_of_live_runtime_code() -> None:
    """The oracle comes from the checked-in artifact, not from omnivia_memory.

    T-0629B requires the Phase 0 oracle to stop importing the live runtime
    database. The B0 review found baseline/storage.py deriving the frozen schema by
    instantiating the legacy Database through a deferred import, which let the
    oracle move with the code it polices.
    """
    script = phase0_baseline_sql()
    assert "CREATE TABLE" in script
    assert phase0_fingerprint() == fingerprint_sql_script(script)

    import omnivia_core_runtime.storage.migrations as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "omnivia_memory" not in source


def test_bd02c_frozen_baseline_holds_the_fourteen_legacy_tables(tmp_path: Path) -> None:
    path = tmp_path / "phase0.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        assert len(table_names(connection)) == 14
        fingerprint = fingerprint_schema(connection)
        assert fingerprint.tables == 14
        assert fingerprint.indexes == 33
        assert fingerprint.triggers == 0
    finally:
        connection.close()


# BD-03
def test_bd03_bootstrap_requires_an_exclusive_connection(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        with pytest.raises(StorageError, match="exclusive connection"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EPHEMERAL,
                expect_phase0_baseline=False,
            )
        assert not substrate_present(connection)
    finally:
        connection.close()


# BD-04
@pytest.mark.parametrize("fail_after", list(range(1, 8)))
def test_bd04_crash_at_each_bootstrap_statement_retries_or_resumes(
    tmp_path: Path, fail_after: int
) -> None:
    """Inject a crash after the Nth statement and prove restart is safe.

    Either the substrate is complete and generation 1 exists exactly once, or the
    database is untouched and a retry succeeds. Nothing in between is observable.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    before = fingerprint_schema(open_database(path, OpenMode.READ_ONLY)).digest

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        crashing = CrashAfter(connection, fail_after)
        with pytest.raises(Boom):
            bootstrap_generation_one(
                cast("sqlite3.Connection", crashing),
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
            )
        assert crashing.executed == fail_after
    finally:
        connection.close()

    # Restart: a fresh connection must find a consistent state.
    restarted = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(restarted)
        if state is None:
            # Rolled back: the Phase 0 schema is intact and a retry works.
            assert fingerprint_schema(restarted).digest == before
            retried = bootstrap_generation_one(
                restarted,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
            )
            assert retried.fencing_generation == GENERATION_ONE
        else:
            assert state.fencing_generation == GENERATION_ONE
            count = restarted.execute(
                "SELECT COUNT(*) FROM omnivia_workspace_state"
            ).fetchone()
            assert count is not None and count[0] == 1
        assert integrity_check(restarted) == []
    finally:
        restarted.close()


# BD-05
def test_bd05_ordinary_open_never_creates_a_database_or_schema(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite"
    for mode in (
        OpenMode.READ_ONLY,
        OpenMode.SERVICE_OWNED,
        OpenMode.EXCLUSIVE_MAINTENANCE,
    ):
        with pytest.raises(StorageError, match="no workspace database"):
            open_database(missing, mode)
        assert not missing.exists(), f"{mode.value} created the database"


def test_bd05b_ordinary_open_of_a_phase0_database_adds_no_tables(tmp_path: Path) -> None:
    """The legacy behaviour this replaces: connect() ran _init_schema() every time."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    before = fingerprint_schema(open_database(path, OpenMode.READ_ONLY)).digest

    for mode in (OpenMode.READ_ONLY, OpenMode.SERVICE_OWNED):
        connection = open_database(path, mode)
        try:
            assert fingerprint_schema(connection).digest == before
            assert not substrate_present(connection)
        finally:
            connection.close()


def test_bd05c_read_only_mode_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE nope (id TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO workspaces (id, name) VALUES ('a', 'b')")
    finally:
        connection.close()


def test_bd05d_pristine_bootstrap_refuses_a_populated_database(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with pytest.raises(SchemaCreationRefused, match="empty database"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
            )
    finally:
        connection.close()


# BD-12
def test_bd12_baseline_adopted_is_recorded_for_the_phase0_schema(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    connection = adopted(path)
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
            service_instance_id=INSTANCE,
        )
        row = connection.execute(
            "SELECT baseline_state FROM omnivia_workspace_state WHERE singleton = 1"
        ).fetchone()
        assert row is not None and row[0] == BASELINE_ADOPTED

        # The bootstrap itself is recorded as a durable attempt and a ledger entry.
        attempts = connection.execute(
            "SELECT outcome, detail FROM omnivia_migration_attempts"
        ).fetchall()
        assert [a[0] for a in attempts] == ["succeeded"]
        assert "generation-1 bootstrap" in str(attempts[0][1])
        assert set(applied_migrations(connection)) == {1}
    finally:
        connection.close()


def test_storage_configuration_enables_wal_foreign_keys_and_busy_timeout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.sqlite"
    connection = open_database(path, OpenMode.EPHEMERAL, busy_timeout_ms=1234)
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()
        assert journal is not None and str(journal[0]).lower() == "wal"
        fk = connection.execute("PRAGMA foreign_keys").fetchone()
        assert fk is not None and int(fk[0]) == 1
        timeout = connection.execute("PRAGMA busy_timeout").fetchone()
        assert timeout is not None and int(timeout[0]) == 1234
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_migrations_are_ordered_and_checksum_pinned() -> None:
    migrations = load_migrations()
    assert migrations, "at least the ownership substrate must exist"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    for migration in migrations:
        assert len(migration.checksum) == 64
        # The checksum is content-derived, so it is stable across runs.
        assert migration.checksum == load_migrations()[versions.index(migration.version)].checksum


def test_a_changed_migration_is_detected_rather_than_skipped(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        # Rewrite the recorded checksum, as an edited migration file would.
        connection.execute(
            "UPDATE omnivia_schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),
        )
        with pytest.raises(StorageError, match="has changed since it was applied"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.SERVICE_OWNED,
                service_instance_id=INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()


def test_migration_refuses_a_stale_generation_or_wrong_workspace(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        with pytest.raises(StorageError, match="stale fencing generation"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.SERVICE_OWNED,
                service_instance_id=INSTANCE,
                fencing_generation=99,
                workspace_id=WORKSPACE_ID,
            )
        with pytest.raises(StorageError, match="workspace mismatch"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.SERVICE_OWNED,
                service_instance_id=INSTANCE,
                fencing_generation=GENERATION_ONE,
                workspace_id="ws-someone-else",
            )
    finally:
        connection.close()


def test_migration_ledger_is_authoritative_over_user_version(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        ledger = applied_migrations(connection)
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None
        assert int(version[0]) == max(ledger)

        # user_version is diagnostic only: corrupting it does not change the ledger.
        connection.execute("PRAGMA user_version = 999")
        assert applied_migrations(connection) == ledger
    finally:
        connection.close()


def test_immediate_transaction_rolls_back_on_failure(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        with pytest.raises(RuntimeError), immediate_transaction(connection):
            connection.execute(
                "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, "
                "created_at, updated_at) VALUES ('j1', 't', 'queued', 'n', 'n')"
            )
            raise RuntimeError("boom")
        count = connection.execute("SELECT COUNT(*) FROM omnivia_durable_jobs").fetchone()
        assert count is not None and count[0] == 0, "the whole block must roll back"
    finally:
        connection.close()


def test_open_events_record_mode_and_authority(tmp_path: Path) -> None:
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        event_id = record_open_event(
            connection,
            open_mode=OpenMode.SERVICE_OWNED,
            service_instance_id=INSTANCE,
            fencing_generation=GENERATION_ONE,
        )
        row = connection.execute(
            "SELECT open_mode, service_instance_id, fencing_generation "
            "FROM omnivia_workspace_open_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row == ("service_owned", INSTANCE, GENERATION_ONE)
    finally:
        connection.close()


def test_open_mode_capabilities_are_explicit() -> None:
    assert not OpenMode.READ_ONLY.writable
    assert OpenMode.EPHEMERAL.may_create
    assert not OpenMode.SERVICE_OWNED.may_create
    assert OpenMode.SERVICE_OWNED.exclusive
    assert OpenMode.EXCLUSIVE_MAINTENANCE.exclusive
    assert not OpenMode.EPHEMERAL.exclusive
