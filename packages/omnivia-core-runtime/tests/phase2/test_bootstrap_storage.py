"""T-0629B acceptance: versioned storage and Generation-1 bootstrap.

Covers the bootstrap half of the BD group (BD-01 … BD-06, BD-12) plus the storage
configuration and migration-ledger requirements. The discovery half (BD-07 … BD-11)
needs OS locks and lands with T-0629D/E.
"""

from __future__ import annotations

import ast
import hashlib
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
    split_sql_statements,
    table_names,
)
from omnivia_core_runtime.storage.migrations import (
    BASELINE_ADOPTED,
    BASELINE_PRISTINE,
    GENERATION_ONE,
    applied_migrations,
    apply_pending_migrations,
    bootstrap_generation_one,
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    phase0_fingerprint,
    read_workspace_state,
    record_open_event,
    substrate_present,
)

from .conftest import insert_row

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

#: The 14 tables the frozen Phase 0 artifact defines (test_bd02c pins the count).
PHASE0_TABLES = {
    "chunks",
    "context_packs",
    "control_plane_events",
    "control_plane_manifests",
    "control_plane_resources",
    "entity_memory_links",
    "graph_entities",
    "graph_relationships",
    "memories",
    "pattern_occurrences",
    "pattern_relationships",
    "patterns",
    "sources",
    "workspaces",
}

#: Accepted checksums for the frozen baseline (0000) and every migration (0001+),
#: pinned by the T-0629B/pristine-bootstrap acceptance record.
ACCEPTED_MIGRATION_HASHES = {
    0: "28b2e553cf461aeae6ec93ce9fd2170c59a7efd5d1b0b956ced5cdaec0090de1",
    1: "2ca7c190c8678994a2229a8ee9b2d1c12b259406de6dada108202cf51a8aa87c",
    2: "10807d2ff70ba1e1c4c9b407b9cf1efd7fdb11767726ae10bcb0831de40b1d06",
    3: "bf1a837c16d3e012db0c4fa78071d7198c116f27b75dfd0308fce3f4ec5d3f0f",
    4: "67048d8b1d9cf5dd565f7dff0c2bbd07529a3c18f3b8e05ae4add7d1521c1cde",
    5: "e6cc0d7f6d0ce59c60a35eeaed77780b47af93175bbcb3eea933cc98c7832e89",
    6: "c3c1501b0cf22ccaa3af16d1ea2c77e7c62dd3b94d4e26cf5cd0041caf1a471e",
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


def test_bd01c_pristine_bootstrap_materialises_empty_phase0_scaffolding(
    tmp_path: Path,
) -> None:
    """Permanent cases 1, 2, 3, 4, 6.

    A pristine bootstrap must atomically materialise the frozen Phase 0 tables as
    empty compatibility scaffolding alongside the ownership substrate, record only
    0001 in the ledger, and leave every Phase 0 table empty -- and a second call
    must be a true no-op that neither recreates nor relabels any of it.
    """
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        # Case 1: the database is actually empty before bootstrap.
        assert table_names(connection) == []
        assert not substrate_present(connection)

        first = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=INSTANCE,
        )
        assert first.baseline_state == BASELINE_PRISTINE
        assert first.fencing_generation == GENERATION_ONE

        # Case 2: all 14 frozen Phase 0 tables plus the ownership substrate exist.
        tables = set(table_names(connection))
        assert len(PHASE0_TABLES) == 14
        assert PHASE0_TABLES.issubset(tables)
        assert SUBSTRATE_TABLES.issubset(tables)

        # Case 3: every Phase 0 table has zero rows immediately afterward.
        for table in sorted(PHASE0_TABLES):
            rows = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            assert rows is not None and rows[0] == 0, f"{table} must stay empty"

        # Case 4: the ledger contains 0001 only, with its unchanged checksum; 0000
        # is not a migration and is never recorded.
        substrate = load_migrations()[0]
        assert substrate.version == 1
        assert substrate.checksum == ACCEPTED_MIGRATION_HASHES[1]
        assert applied_migrations(connection) == {substrate.version: substrate.checksum}

        # Case 6: a second call is idempotent -- same state, nothing recreated or
        # relabelled, generation not bumped, Phase 0 tables still empty.
        second = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=INSTANCE,
        )
        assert second == first
        assert second.baseline_state == BASELINE_PRISTINE
        assert second.fencing_generation == GENERATION_ONE
        assert set(table_names(connection)) == tables
        assert applied_migrations(connection) == {substrate.version: substrate.checksum}
        for table in sorted(PHASE0_TABLES):
            rows = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            assert rows is not None and rows[0] == 0
        count = connection.execute(
            "SELECT COUNT(*) FROM omnivia_workspace_state"
        ).fetchone()
        assert count is not None and count[0] == 1
    finally:
        connection.close()


def test_bd01d_pristine_bootstrap_then_pending_migrations_reaches_canonical_schema(
    tmp_path: Path,
) -> None:
    """Permanent cases 5 and 14.

    From a pristine start, bootstrap plus the unchanged `apply_pending_migrations()`
    must reach the highest accepted version with clean integrity and foreign keys,
    matching the canonical schema fingerprint built from the checked-in artifacts --
    the complete pre-M1 migration chain, reproduced without touching M1.
    """
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.SERVICE_OWNED,
            service_instance_id=INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        all_migrations = load_migrations()
        assert [m.version for m in applied] == [
            m.version for m in all_migrations if m.version != GENERATION_ONE
        ]
        assert applied_migrations(connection) == {
            m.version: m.checksum for m in all_migrations
        }

        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None
        assert int(version[0]) == max(m.version for m in all_migrations)

        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    finally:
        connection.close()


def test_bd05e_pristine_bootstrap_refuses_a_structurally_unknown_database(
    tmp_path: Path,
) -> None:
    """Permanent case 8 (structurally-unknown half; BD-05D covers the populated half)."""
    connection = pristine(tmp_path / "workspace.sqlite")
    try:
        connection.execute("CREATE TABLE mystery (id TEXT PRIMARY KEY)")
        with pytest.raises(SchemaCreationRefused, match="empty database"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
            )
        assert not substrate_present(connection)
        assert table_names(connection) == ["mystery"]
    finally:
        connection.close()


# BD-02
def test_bd02_bootstrap_requires_exact_phase0_schema_fingerprint(
    tmp_path: Path,
) -> None:
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


def test_bd02d_bootstrap_tests_do_not_import_a_live_legacy_runtime() -> None:
    """Permanent case 12: this module's own baseline comes from the checked-in
    artifact (`phase0_baseline_sql` / `materialise_phase0_baseline`), never from
    instantiating the legacy runtime package -- mirroring what BD-02B already
    proves about the product code the tests exercise.

    Parsed as imports rather than searched as raw text: a text search of this
    module's own source would trip on the forbidden name appearing in the
    comparison itself.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    forbidden = "omnivia_" + "memory"
    assert forbidden not in imported_roots


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


def _pristine_bootstrap_call_count() -> int:
    """Every `connection.execute()` a successful pristine bootstrap issues.

    The two script sizes come from `split_sql_statements()` -- the same splitter
    `execute_script()` uses -- so a change to either the Phase 0 baseline or the
    0001 substrate keeps this exhaustive automatically, per case 10's requirement
    that failure-injection cardinality not be a fixed count. The remaining 6 are
    the fixed, script-independent calls the transaction always makes: BEGIN
    IMMEDIATE; the attempt, state and ledger inserts; the user_version pragma; and
    COMMIT.
    """
    phase0_statements = len(split_sql_statements(phase0_baseline_sql()))
    substrate_statements = len(split_sql_statements(load_migrations()[0].sql))
    return phase0_statements + substrate_statements + 6


# BD-04 (pristine path)
@pytest.mark.parametrize("fail_after", list(range(1, _pristine_bootstrap_call_count())))
def test_bd04b_crash_at_each_pristine_bootstrap_statement_retries_or_resumes(
    tmp_path: Path, fail_after: int
) -> None:
    """Permanent cases 10 and 11.

    The pristine path runs every Phase 0 statement before the substrate and
    bookkeeping ones, so it has strictly more crash points than BD-04's adopted
    path -- swept here in full rather than a fixed count. After a crash, restart
    must find either an untouched empty database or a complete Generation-1
    bootstrap with every Phase 0 table present and still empty, and a retry from
    an empty database must converge in one more call.
    """
    path = tmp_path / "workspace.sqlite"
    connection = pristine(path)
    try:
        assert table_names(connection) == []
        crashing = CrashAfter(connection, fail_after)
        with pytest.raises(Boom):
            bootstrap_generation_one(
                cast("sqlite3.Connection", crashing),
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
            )
        assert crashing.executed == fail_after
    finally:
        connection.close()

    # Restart: a fresh connection must find a consistent state.
    restarted = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(restarted)
        if state is None:
            # Rolled back: still literally empty, and a retry converges.
            assert table_names(restarted) == []
            retried = bootstrap_generation_one(
                restarted,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
            )
            assert retried.fencing_generation == GENERATION_ONE
            assert retried.baseline_state == BASELINE_PRISTINE
        else:
            assert state.fencing_generation == GENERATION_ONE
            assert state.baseline_state == BASELINE_PRISTINE
            count = restarted.execute(
                "SELECT COUNT(*) FROM omnivia_workspace_state"
            ).fetchone()
            assert count is not None and count[0] == 1

        tables = set(table_names(restarted))
        assert PHASE0_TABLES.issubset(tables)
        assert SUBSTRATE_TABLES.issubset(tables)
        for table in sorted(PHASE0_TABLES):
            rows = restarted.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            assert rows is not None and rows[0] == 0
        assert set(applied_migrations(restarted)) == {1}
        assert integrity_check(restarted) == []
        assert foreign_key_check(restarted) == []
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


def test_bd05b_ordinary_open_of_a_phase0_database_adds_no_tables(
    tmp_path: Path,
) -> None:
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
def test_bd12_baseline_adopted_is_recorded_for_the_phase0_schema(
    tmp_path: Path,
) -> None:
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


def test_bd12b_adopted_bootstrap_preserves_pre_existing_phase0_rows(
    tmp_path: Path,
) -> None:
    """Permanent case 7: exact Phase 0 adoption stays BASELINE_ADOPTED and never
    touches rows a legacy workspace already had. Only the pristine path applies
    the baseline script; the adopted path's schema already matches it exactly and
    bootstrap never re-applies -- let alone re-creates -- it.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    seed = sqlite3.connect(str(path))
    try:
        insert_row(
            seed,
            "workspaces",
            {
                "id": "legacy-workspace-0",
                "name": "Pre-existing legacy workspace",
                "root_path": "/legacy/root",
                "storage_path": "/legacy/storage",
                "index_status": "indexed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        )
        seed.commit()
    finally:
        seed.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
            service_instance_id=INSTANCE,
        )
        assert state.baseline_state == BASELINE_ADOPTED
        row = connection.execute(
            "SELECT id, name FROM workspaces WHERE id = ?", ("legacy-workspace-0",)
        ).fetchone()
        assert row == ("legacy-workspace-0", "Pre-existing legacy workspace")
        count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()
        assert count is not None and count[0] == 1
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
        assert (
            migration.checksum
            == load_migrations()[versions.index(migration.version)].checksum
        )


def _protected_prefix_holds(by_version: dict[int, str]) -> bool:
    """Whether every accepted (non-baseline) version is present with its pinned
    checksum in `by_version`. Extra versions beyond the protected prefix -- a
    legitimate 0007 and later -- are ignored, not required or forbidden.
    """
    return all(
        by_version.get(version) == checksum
        for version, checksum in ACCEPTED_MIGRATION_HASHES.items()
        if version != 0
    )


def test_migration_hashes_are_pinned_to_their_accepted_values() -> None:
    """Permanent case 9: 0000 through 0006 remain pinned to their accepted values.

    Checked as a protected prefix, not exact-set equality with `load_migrations()`:
    a legitimate 0007 (or any later forward migration) must not fail this
    permanent acceptance case. Only a missing or changed protected version may.
    """
    baseline_checksum = hashlib.sha256(
        phase0_baseline_sql().encode("utf-8")
    ).hexdigest()
    assert baseline_checksum == ACCEPTED_MIGRATION_HASHES[0]

    by_version = {m.version: m.checksum for m in load_migrations()}
    protected_versions = {
        version for version in ACCEPTED_MIGRATION_HASHES if version != 0
    }
    assert protected_versions.issubset(by_version), (
        "a protected migration version is missing"
    )
    assert _protected_prefix_holds(by_version)


def test_extra_future_migrations_do_not_invalidate_the_protected_prefix() -> None:
    """Direct regression: a 0007+ migration alongside the unchanged accepted
    versions must still satisfy the protected-prefix assertion above -- proving
    the fix actually stops rejecting legitimate forward migrations.
    """
    by_version = {m.version: m.checksum for m in load_migrations()}
    by_version_with_future = dict(by_version)
    by_version_with_future[max(by_version) + 1] = "f" * 64
    assert _protected_prefix_holds(by_version_with_future)


def test_a_changed_protected_migration_fails_the_protected_prefix() -> None:
    """The other half: an edited protected version must still be caught."""
    by_version = {m.version: m.checksum for m in load_migrations()}
    by_version[1] = "0" * 64
    assert not _protected_prefix_holds(by_version)


def test_a_missing_protected_migration_fails_the_protected_prefix() -> None:
    by_version = {m.version: m.checksum for m in load_migrations()}
    del by_version[1]
    protected_versions = {
        version for version in ACCEPTED_MIGRATION_HASHES if version != 0
    }
    assert not protected_versions.issubset(by_version)
    assert not _protected_prefix_holds(by_version)


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


def test_migration_refuses_a_stale_generation_or_wrong_workspace(
    tmp_path: Path,
) -> None:
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
        count = connection.execute(
            "SELECT COUNT(*) FROM omnivia_durable_jobs"
        ).fetchone()
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
