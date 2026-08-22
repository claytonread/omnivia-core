"""V06-8 acceptance for migration 0017's durable connector state.

What 0017 is: a unique consecutive successor to 0016, pinned by content
checksum, whose objects are exactly three tables, two named indexes and nine
statement triggers; a slice that applies to a pristine and to an exactly-adopted
Phase 0 workspace without disturbing anything already there; and a schema whose
immutability is enforced rather than asserted.

What it is not: a place a cursor lives. The resumable cursor stays in
`omnivia_job_checkpoints`, and the tests below hold 0017 to adding the connector
-> run edge that makes those checkpoints readable as one connector's history,
rather than a second copy of them.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    assert_guards_intact,
    fenced_transaction,
    verify_fingerprint,
)
from omnivia_core_runtime.storage import migrations as migrations_module
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
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 17
PREDECESSOR_VERSION = 16
MIGRATION_NAME = "0017_connector_sync_state.sql"
WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = 2_100_000_000_000_000

RUNS = "omnivia_connector_sync_runs"
DEAD_LETTERS = "omnivia_connector_dead_letters"
HEALTH = "omnivia_connector_health_events"
TABLES = (RUNS, DEAD_LETTERS, HEALTH)
INDEXES = (
    "omnivia_idx_connector_sync_runs_connector_run",
    "omnivia_idx_connector_dead_letters_run",
)
CONNECTOR_ID = "conn-alpha"
RUN_ID = "job-sync-0001"


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def seed_run(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    connector_id: str = CONNECTOR_ID,
    sequence: int = 1,
    source_kind: str = "document",
    state_version: int = 1,
    job_type: str = "ingestion.import",
    register: bool = True,
) -> None:
    """Create the durable job a sync run needs, and optionally the run itself."""
    audit_ref = f"aud-{run_id}"
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) VALUES "
            "(?, ?, 'core-service', 'system.connector_sync', 'ingestion.sync', ?, ?, "
            "?, '{}', 'succeeded', NULL, ?)",
            (
                audit_ref,
                WORKSPACE_ID,
                f"req-{run_id}",
                f"cor-{run_id}",
                f"trc-{run_id}",
                BASE_US,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, ?, 'claimed', '{}', ?, ?, ?, ?)",
            (
                run_id,
                job_type,
                "2026-08-14T00:00:00Z",
                "2026-08-14T00:00:00Z",
                holder.generation,
                holder.identity.service_instance_id,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_job_application_metadata "
            "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
            "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
            "max_attempts) VALUES (?, ?, ?, 'system.connector_sync', ?, ?, NULL, 1, 8)",
            (WORKSPACE_ID, run_id, job_type, audit_ref, BASE_US),
        )
        if register:
            holder.connection.execute(
                f"INSERT INTO {RUNS} (workspace_id, connector_id, sync_sequence, "
                "run_id, source_kind, state_version, started_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    connector_id,
                    sequence,
                    run_id,
                    source_kind,
                    state_version,
                    BASE_US,
                ),
            )


def insert_run(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "connector_id": CONNECTOR_ID,
        "sync_sequence": 1,
        "run_id": RUN_ID,
        "source_kind": "document",
        "state_version": 1,
        "started_at_us": BASE_US,
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    holder.connection.execute(
        f"INSERT INTO {RUNS} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def insert_dead_letter(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "dead_letter_id": "dlt-0001",
        "workspace_id": WORKSPACE_ID,
        "connector_id": CONNECTOR_ID,
        "run_id": RUN_ID,
        "source_native_id": "doc-1",
        "failure_code": "source_forbidden",
        "retry_class": "non_retryable",
        "message": "no access",
        "attempts": 1,
        "recorded_at_us": BASE_US,
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    holder.connection.execute(
        f"INSERT INTO {DEAD_LETTERS} ({columns}) VALUES ({marks})",
        tuple(values.values()),
    )


def insert_health(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "health_event_id": "hev-0001",
        "workspace_id": WORKSPACE_ID,
        "connector_id": CONNECTOR_ID,
        "health_sequence": 1,
        "health_state": "healthy",
        "detail": None,
        "observed_at_us": BASE_US,
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    holder.connection.execute(
        f"INSERT INTO {HEALTH} ({columns}) VALUES ({marks})", tuple(values.values())
    )


# --- migration identity --------------------------------------------------------


def test_0017_is_the_unique_consecutive_successor_to_0016() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    # Asserted as a prefix rather than as the tail of the catalogue, exactly as the
    # 0016 slice already does. Reading the last two entries made this test a claim
    # about the head, so a legitimate later migration failed a slice that has no
    # authority over it.
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_reapplying_an_unchanged_migration_is_a_no_op(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m1.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        connection.close()
    assert applied == []


def test_an_edited_migration_is_refused_rather_than_accepted(migrated: Path) -> None:
    """The checksum pin is what makes "0017 is applied" mean *this* 0017."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        connection.execute("PRAGMA writable_schema = OFF")
        state = read_workspace_state(connection)
        assert state is not None
        original = migrations_module.load_migrations
        edited = tuple(
            m if m.version != MIGRATION_VERSION
            else migrations_module.Migration(
                version=m.version, name=m.name, sql=m.sql + "\n-- drift\n"
            )
            for m in original()
        )
        migrations_module.load_migrations = lambda: edited  # type: ignore[assignment]
        try:
            with pytest.raises(Exception, match="has changed since it was applied"):
                apply_pending_migrations(
                    connection,
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    service_instance_id=m1.SERVICE_INSTANCE,
                    fencing_generation=state.fencing_generation,
                    workspace_id=WORKSPACE_ID,
                )
        finally:
            migrations_module.load_migrations = original  # type: ignore[assignment]
    finally:
        connection.close()


def test_every_migration_before_this_one_is_byte_for_byte_unchanged() -> None:
    """0017 is additive; nothing at or before 0016 moved to make room for it."""
    for name, expected in m1.ACCEPTED_MIGRATION_CHECKSUMS.items():
        package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name


def test_the_migration_carries_no_dml(migrated: Path) -> None:
    """A schema slice that writes a row would be changing data, not shape."""
    for statement in MIGRATION_STATEMENTS:
        head = statement.split(None, 1)[0].upper()
        assert head == "CREATE", statement[:80]


# --- canonical schema and drift ------------------------------------------------


def test_the_canonical_schema_carries_every_object_this_slice_adds(
    migrated: Path,
) -> None:
    assert set(TABLES) <= canonical_schema_tables()
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert set(TABLES) <= names
        assert set(INDEXES) <= names
        verify_fingerprint(connection, canonical_schema_fingerprint())
        assert_guards_intact(connection)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'omnivia_guard_connector_%'"
            )
        }
    finally:
        connection.close()
    assert len(triggers) == 9
    for table in TABLES:
        stem = table.removeprefix("omnivia_")
        for statement in ("insert", "update", "delete"):
            assert f"omnivia_guard_{stem}_{statement}" in triggers


def test_drift_in_any_object_this_slice_adds_is_detected(migrated: Path) -> None:
    expected = canonical_schema_fingerprint()
    connection = sqlite3.connect(str(migrated))
    try:
        connection.executescript(
            f"CREATE TABLE {RUNS}_drift (id TEXT PRIMARY KEY);"
        )
        connection.commit()
    finally:
        connection.close()
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        with pytest.raises(SchemaDrift):
            verify_fingerprint(connection, expected)
    finally:
        connection.close()


def test_integrity_and_foreign_keys_are_clean_after_the_migration(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    finally:
        connection.close()


def test_a_pristine_workspace_also_reaches_this_migration(tmp_path: Path) -> None:
    path = tmp_path / "pristine.sqlite"
    open_database(path, OpenMode.EPHEMERAL).close()
    m1.bootstrap_and_migrate(path, adopt_phase0=False)
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert MIGRATION_VERSION in applied_migrations(connection)
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


# --- immutability and authority -------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned, table: str
) -> None:
    """The owner may append run history; it may never rewrite it."""
    seed_run(owned)
    with guarded(owned):
        insert_dead_letter(owned)
        insert_health(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = 'other'")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


def test_no_open_guard_means_no_connector_write_at_all(owned: m1.Owned) -> None:
    seed_run(owned, register=False)
    from omnivia_core_runtime.ownership.fencing import close_guard

    close_guard(owned.connection)
    with pytest.raises(sqlite3.DatabaseError):
        insert_run(owned)


# --- run registration rules -----------------------------------------------------


def test_a_run_must_be_an_ingestion_import_durable_job(owned: m1.Owned) -> None:
    seed_run(owned, job_type="maintenance.compaction", register=False)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="ingestion.import durable job"
    ):
        insert_run(owned)


def test_the_sync_sequence_must_be_contiguous(owned: m1.Owned) -> None:
    seed_run(owned, register=False)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="sync sequence must be contiguous"
    ):
        insert_run(owned, sync_sequence=2)


def test_a_connector_state_version_may_not_go_backwards(owned: m1.Owned) -> None:
    seed_run(owned, state_version=3)
    seed_run(owned, run_id="job-sync-0002", register=False)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="state version may not go backwards"
    ):
        insert_run(owned, run_id="job-sync-0002", sync_sequence=2, state_version=2)


def test_a_connector_may_not_change_its_source_kind_between_runs(
    owned: m1.Owned,
) -> None:
    seed_run(owned)
    seed_run(owned, run_id="job-sync-0002", register=False)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="source kind is immutable"
    ):
        insert_run(
            owned, run_id="job-sync-0002", sync_sequence=2, source_kind="email"
        )


def test_one_durable_job_carries_at_most_one_connector_run(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only one"):
        insert_run(owned, connector_id="conn-beta", sync_sequence=1)


# --- dead letters and health -----------------------------------------------------


def test_a_dead_letter_must_name_a_registered_run_of_its_connector(
    owned: m1.Owned,
) -> None:
    seed_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert_dead_letter(owned, connector_id="conn-beta")


def test_one_item_is_dead_lettered_at_most_once_per_run(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        insert_dead_letter(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most once"):
        insert_dead_letter(owned, dead_letter_id="dlt-0002")


def test_a_dead_letter_may_not_predate_the_run_that_recorded_it(
    owned: m1.Owned,
) -> None:
    seed_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="predate the run"):
        insert_dead_letter(owned, recorded_at_us=BASE_US - 1)


def test_an_unclassified_retry_class_has_no_column_to_live_in(
    owned: m1.Owned,
) -> None:
    seed_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_dead_letter(owned, retry_class="probably_fine")


def test_health_observations_are_contiguous_and_never_regress_in_time(
    owned: m1.Owned,
) -> None:
    with guarded(owned):
        insert_health(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert_health(owned, health_event_id="hev-0002", health_sequence=3)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not regress"):
        insert_health(
            owned,
            health_event_id="hev-0002",
            health_sequence=2,
            observed_at_us=BASE_US - 1,
        )


def test_an_unrecognised_health_state_is_refused(owned: m1.Owned) -> None:
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_health(owned, health_state="probably_up")
