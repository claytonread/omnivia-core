"""RT-103 acceptance for migration 0019's canonical Artifact, EvidenceItem and
CleanupReceipt metadata records.

What 0019 is: a unique consecutive successor to 0018, pinned by content checksum,
whose objects are exactly three tables, three named indexes and nine statement
triggers; a slice that applies to a pristine workspace, to an exactly-adopted Phase 0
workspace and to a *populated* workspace already at the 0018 head, without disturbing
one existing row; and a schema whose immutability is enforced rather than asserted.

What it is not. It carries no DML. It stores no physical blob and no filesystem path,
URL, bucket or credential: `omnivia_blob_objects` (migration 0008) is the existing
verified-identity catalogue this slice reads from, never duplicates, and never
requires a matching row from before it will accept or return an artifact's own
metadata -- a missing blob degrades to unavailable, not to a corrupted or unreadable
Run. It is not RT-104 (no command handling) and not RT-202+ (no policy, budget,
grant, approval or effect relation), and it makes no public wire change.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    StaleGeneration,
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    verify_fingerprint,
)
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
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

MIGRATION_VERSION = 19
PREDECESSOR_VERSION = 18
MIGRATION_NAME = "0019_artifact_evidence_cleanup_records.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID
DIGEST = m18.DIGEST
OTHER_DIGEST = "sha256:" + "d" * 64

ARTIFACTS = "omnivia_runtime_artifacts"
EVIDENCE = "omnivia_runtime_evidence"
CLEANUP = "omnivia_runtime_cleanup_receipts"

TABLES = (ARTIFACTS, EVIDENCE, CLEANUP)

INDEXES = (
    "omnivia_idx_runtime_artifacts_run",
    "omnivia_idx_runtime_evidence_run",
    "omnivia_idx_runtime_cleanup_receipts_run",
)

#: Every migration this slice builds on, pinned by content, following the same
#: prefix-claim convention 0018 established: this file adds its own predecessor's
#: checksum to the inherited map rather than asserting anything about what follows it.
ACCEPTED_MIGRATION_CHECKSUMS = {
    **m18.ACCEPTED_MIGRATION_CHECKSUMS,
    m18.MIGRATION_NAME: m18.MIGRATION.checksum,
}

ARTIFACT_ID = "art-0001"
EVIDENCE_ID = "evd-0001"
CLEANUP_ID = "cln-0001"

#: A step created strictly after its run, so `BASE_US < BETWEEN_US < STEP_US` names an
#: instant that is late enough for the run and early enough for the step. Without the
#: gap the run guard answers first and the step guard is never the thing under test.
STEP_US = BASE_US + 100
BETWEEN_US = BASE_US + 50


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


#: The composite keys this slice's own foreign keys point at, and nothing else about
#: those parents. SQLite resolves a referenced table whenever `foreign_keys` is on,
#: even for a NULL child column, so a test that switches the pragma on needs all three
#: to exist or it reports `no such table` where it meant to report a key violation.
#: Reduced to the referenced columns on purpose: replaying 0007's and 0018's real
#: definitions would drag in their own parents and prove nothing extra about 0019.
PARENT_KEY_DDL = (
    (
        "CREATE TABLE omnivia_runtime_runs (workspace_id TEXT NOT NULL, "
        "run_id TEXT NOT NULL, PRIMARY KEY (workspace_id, run_id)) WITHOUT ROWID"
    ),
    (
        "CREATE TABLE omnivia_runtime_run_steps (workspace_id TEXT NOT NULL, "
        "run_step_id TEXT NOT NULL, PRIMARY KEY (workspace_id, run_step_id)) "
        "WITHOUT ROWID"
    ),
    (
        "CREATE TABLE omnivia_application_audit_events (audit_ref TEXT NOT NULL, "
        "workspace_id TEXT NOT NULL, PRIMARY KEY (audit_ref, workspace_id)) "
        "WITHOUT ROWID"
    ),
)


def ddl_only_connection() -> sqlite3.Connection:
    """The slice's tables and indexes with none of its triggers, in memory.

    Foreign keys start off, exactly as 0018's own `ddl_only_connection` leaves them, so
    a row can be seeded without replaying every migration behind it. `PARENT_KEY_DDL`
    is there for the one test that switches them on.
    """
    connection = sqlite3.connect(":memory:")
    connection.isolation_level = None
    for statement in PARENT_KEY_DDL:
        connection.execute(statement)
    for statement in MIGRATION_STATEMENTS:
        if not statement.lstrip().upper().startswith("CREATE TRIGGER"):
            connection.execute(statement)
    return connection


# --- fixtures and seeding ---------------------------------------------------------


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


def _insert(holder: m1.Owned, table: str, values: dict[str, object]) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    holder.connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def insert_artifact(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "artifact_id": ARTIFACT_ID,
        "run_id": RUN_ID,
        "run_step_id": None,
        "artifact_kind": "final_output",
        "media_type": "text/plain",
        "content_checksum": DIGEST,
        "content_length_bytes": 128,
        "produced_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, ARTIFACTS, values)


def insert_evidence(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "evidence_item_id": EVIDENCE_ID,
        "run_id": RUN_ID,
        "run_step_id": None,
        "evidence_kind": "tool_call",
        "source_kind": "runtime",
        "source_id": "src-0001",
        "source_workspace_id": WORKSPACE_ID,
        "content_checksum": OTHER_DIGEST,
        "artifact_id": None,
        "captured_at_us": BASE_US,
        "authoritative": 1,
        "retained": 1,
    }
    values.update(overrides)
    _insert(holder, EVIDENCE, values)


def insert_cleanup_receipt(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "cleanup_receipt_id": CLEANUP_ID,
        "run_id": RUN_ID,
        "resource_kind": "sandbox",
        "outcome": "released",
        "reason": "run_completed",
        "performed_at_us": BASE_US,
        "audit_ref": m18.audit_ref_for(JOB_ID),
    }
    values.update(overrides)
    _insert(holder, CLEANUP, values)


def drive_run_to(holder: m1.Owned, status: str, *, event_kind: str) -> None:
    """Take an admitted run to `status` through every transition 0018 permits.

    `admitted -> running -> <terminal>`, because the accepted status machine has no
    direct edge from `admitted` to `succeeded` or `failed`. Writing the terminal event
    on its own would be refused by the transition guard, and a test that read that
    refusal as "terminal runs refuse metadata" would prove the opposite of its name.
    """
    with guarded(holder):
        m18.insert_event(
            holder,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 1,
            event_kind="run_started",
            run_status="running",
        )
        m18.insert_event(
            holder,
            sequence=2,
            runtime_event_id="evt-0003",
            occurred_at_us=BASE_US + 2,
            event_kind=event_kind,
            run_status=status,
        )


def seed_run_with_later_step(holder: m1.Owned) -> None:
    """A run at `BASE_US` with its step at `STEP_US`, so the two time guards separate."""
    m18.seed_admitted_run(holder)
    with guarded(holder):
        m18.insert_step(holder, created_at_us=STEP_US)
        m18.insert_step_state(holder, observed_at_us=STEP_US)


def insert_blob(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "content_digest": DIGEST,
        "content_length_bytes": 128,
        "created_at_us": BASE_US,
        "verified_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, "omnivia_blob_objects", values)


# --- migration identity ------------------------------------------------------------


def test_0019_is_the_unique_consecutive_successor_to_0018() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    # A prefix claim, not a claim about the head: this slice has authority over its
    # own position in the sequence and none over what lands after it.
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


def test_an_edited_migration_is_refused_rather_than_accepted(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        original = migrations_module.load_migrations
        edited = tuple(
            m
            if m.version != MIGRATION_VERSION
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
    package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
    for name, expected in ACCEPTED_MIGRATION_CHECKSUMS.items():
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name
    assert len(ACCEPTED_MIGRATION_CHECKSUMS) == MIGRATION_VERSION


def test_the_migration_carries_no_dml() -> None:
    """No canonical Artifact, EvidenceItem or CleanupReceipt exists before this slice.

    Every relation this migration adds is new, so no backfill is owed and none is
    declared: there is nothing in 0018's tables that is honestly classifiable as one
    of these three records, and manufacturing one would invent facts nobody recorded.
    """
    for statement in MIGRATION_STATEMENTS:
        head = statement.split(None, 1)[0].upper()
        assert head == "CREATE", statement[:80]


def test_no_migration_0019_gate_is_declared(owned: m1.Owned) -> None:
    names = {
        str(row[0])
        for row in owned.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_migration_0019%'"
        )
    }
    assert not names
    assert not any("omnivia_migration_0019" in s for s in MIGRATION_STATEMENTS)


# --- canonical schema and drift -----------------------------------------------------


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
                "AND name LIKE 'omnivia_guard_runtime_%'"
            )
        }
    finally:
        connection.close()
    own_triggers = {
        f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
        for table in TABLES
        for statement in ("insert", "update", "delete")
    }
    assert len(own_triggers) == 9
    assert own_triggers <= triggers


def test_drift_in_any_object_this_slice_adds_is_detected(migrated: Path) -> None:
    expected = canonical_schema_fingerprint()
    connection = sqlite3.connect(str(migrated))
    try:
        connection.executescript(f"CREATE TABLE {ARTIFACTS}_drift (id TEXT PRIMARY KEY);")
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
        assert integrity_check(connection) == []
    finally:
        connection.close()


def test_a_populated_0018_head_reaches_0019_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    """Applied from the exact accepted head, over data, this slice only adds."""
    path = tmp_path / "at-0018.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m18.seed_run_with_step(holder)
            with guarded(holder):
                m18.insert_attempt(holder)
                m18.insert_wait(holder)
            before = capture_inventory(holder.connection)
        finally:
            holder.connection.close()
        head = open_database(path, OpenMode.EPHEMERAL)
        try:
            assert max(applied_migrations(head)) == PREDECESSOR_VERSION
        finally:
            head.close()

    with m1.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            after = capture_inventory(maintenance)
            assert integrity_check(maintenance) == []
            assert foreign_key_check(maintenance) == []
        finally:
            maintenance.close()

    assert [m.version for m in applied] == [MIGRATION_VERSION]
    assert before.total_rows > 0
    ledger = {"omnivia_migration_attempts", "omnivia_schema_migrations"}
    for entry in before.tables:
        if entry.name in ledger:
            continue
        assert after.table(entry.name) == entry, entry.name
    assert set(after.table_names) - set(before.table_names) == set(TABLES)
    for table in TABLES:
        added = after.table(table)
        assert added is not None and added.row_count == 0, table
    differences = compare_inventories(before, after)
    assert differences and all(
        any(name in difference for name in ledger) for difference in differences
    ), differences


def test_a_backup_of_a_populated_workspace_restores_every_new_fact(
    owned: m1.Owned, tmp_path: Path
) -> None:
    m18.seed_run_with_step(owned)
    with guarded(owned):
        insert_artifact(owned)
        insert_evidence(owned, artifact_id=ARTIFACT_ID, content_checksum=DIGEST)
        insert_cleanup_receipt(owned)
    populated = capture_inventory(owned.connection)
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    backup = create_verified_backup(
        owned.path, installation, workspace_id=WORKSPACE_ID, attempt_id=new_attempt_id()
    )
    assert backup.verified

    restored = tmp_path / "restored.sqlite"
    restore_backup(backup.path, restored)
    connection = open_database(restored, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert capture_inventory(connection) == populated
        for table in TABLES:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert row[0] >= 1, table
    finally:
        connection.close()


# --- immutability and authority -----------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned, table: str
) -> None:
    m18.seed_run_with_step(owned)
    with guarded(owned):
        insert_artifact(owned)
        insert_evidence(owned)
        insert_cleanup_receipt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = 'other'")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


COMPLETE_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        ARTIFACTS,
        {
            "workspace_id": WORKSPACE_ID,
            "artifact_id": "art-0002",
            "run_id": RUN_ID,
            "run_step_id": STEP_ID,
            "artifact_kind": "intermediate",
            "media_type": "application/json",
            "content_checksum": OTHER_DIGEST,
            "content_length_bytes": 0,
            "produced_at_us": BASE_US + 40,
        },
    ),
    (
        EVIDENCE,
        {
            "workspace_id": WORKSPACE_ID,
            "evidence_item_id": "evd-0002",
            "run_id": RUN_ID,
            "run_step_id": STEP_ID,
            "evidence_kind": "log_line",
            "source_kind": "external_log",
            "source_id": "src-0002",
            "source_workspace_id": WORKSPACE_ID,
            "content_checksum": OTHER_DIGEST,
            "artifact_id": None,
            "captured_at_us": BASE_US + 40,
            "authoritative": 0,
            "retained": 1,
        },
    ),
    (
        CLEANUP,
        {
            "workspace_id": WORKSPACE_ID,
            "cleanup_receipt_id": "cln-0002",
            "run_id": RUN_ID,
            "resource_kind": "sandbox",
            "outcome": "not_required",
            "reason": "nothing_to_free",
            "performed_at_us": BASE_US + 40,
            "audit_ref": m18.audit_ref_for(JOB_ID),
        },
    ),
)


def seed_every_parent(holder: m1.Owned) -> None:
    m18.seed_run_with_step(holder)


def test_the_complete_rows_the_guard_tests_use_would_otherwise_commit(
    owned: m1.Owned,
) -> None:
    seed_every_parent(owned)
    for table, values in COMPLETE_ROWS:
        with guarded(owned):
            _insert(owned, table, values)
        count = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert count[0] >= 1, table


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_no_open_guard_means_no_metadata_write_at_all(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    seed_every_parent(owned)
    before = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    close_guard(owned.connection)
    with (
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
    ):
        _insert(owned, table, values)
    after = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert after == before


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_a_stale_fencing_generation_cannot_append_metadata(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    """The same row the current owner commits, refused at the fence rather than the row."""
    seed_every_parent(owned)
    with pytest.raises(StaleGeneration), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation + 1,
    ):
        _insert(owned, table, values)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with guarded(owned):
        _insert(owned, table, values)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_a_foreign_workspace_cannot_append_metadata(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    """A row naming another workspace is refused by the guard, under a live fence."""
    seed_every_parent(owned)
    foreign = dict(values, workspace_id=OTHER_WORKSPACE_ID)
    if "source_workspace_id" in foreign:
        # Otherwise the column-level agreement CHECK answers first and this test
        # would report the guard it never reached.
        foreign["source_workspace_id"] = OTHER_WORKSPACE_ID
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
        _insert(owned, table, foreign)


# --- artifact invariants -------------------------------------------------------------


def test_an_artifact_must_name_the_run_of_its_own_step(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        m18.insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        m18.insert_step(
            owned, run_step_id="step-other", run_id="run-0002", ordinal=1
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="name the run of its own step"
    ):
        insert_artifact(owned, run_step_id="step-other")


def test_an_artifact_must_not_predate_its_run(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not predate its run"
    ):
        insert_artifact(owned, produced_at_us=BASE_US - 1)


def test_an_artifact_must_not_predate_its_step(owned: m1.Owned) -> None:
    """A time the run already allows, and the step does not: only the step guard is left."""
    seed_run_with_later_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not predate its step"
    ):
        insert_artifact(owned, run_step_id=STEP_ID, produced_at_us=BETWEEN_US)
    with guarded(owned):
        insert_artifact(owned, run_step_id=STEP_ID, produced_at_us=STEP_US)


def test_an_artifact_agreeing_with_a_verified_blob_is_accepted(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        insert_blob(owned, content_length_bytes=128)
        insert_artifact(owned, content_length_bytes=128)
    row = owned.connection.execute(
        f"SELECT content_length_bytes FROM {ARTIFACTS} WHERE artifact_id = ?",
        (ARTIFACT_ID,),
    ).fetchone()
    assert row[0] == 128


def test_an_artifact_contradicting_a_verified_blob_length_is_refused(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        insert_blob(owned, content_length_bytes=128)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="contradicts a verified blob"
    ):
        insert_artifact(owned, content_length_bytes=999)


def test_an_artifact_with_no_blob_row_is_still_accepted(owned: m1.Owned) -> None:
    """A missing blob degrades safely: no catalogue row is required to record metadata."""
    m18.seed_admitted_run(owned)
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_blob_objects WHERE content_digest = ?",
            (DIGEST,),
        ).fetchone()[0]
        == 0
    )
    with guarded(owned):
        insert_artifact(owned)
    row = owned.connection.execute(
        f"SELECT content_checksum FROM {ARTIFACTS} WHERE artifact_id = ?",
        (ARTIFACT_ID,),
    ).fetchone()
    assert row is not None and row[0] == DIGEST


def test_a_run_holds_at_most_256_artifacts(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        for index in range(256):
            insert_artifact(
                owned,
                artifact_id=f"art-{index:04d}",
                content_checksum=f"sha256:{index:064x}",
            )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most 256"):
        insert_artifact(owned, artifact_id="art-overflow", content_checksum=OTHER_DIGEST)


def test_a_terminal_run_still_admits_a_late_artifact(owned: m1.Owned) -> None:
    """Terminal closure and late artifacts are both legitimate; neither is refused."""
    m18.seed_admitted_run(owned)
    drive_run_to(owned, "failed", event_kind="run_failed")
    with guarded(owned):
        insert_artifact(owned)
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {ARTIFACTS}").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"media_type": "not-a-media-type"},
        {"media_type": "x" * 260 + "/plain"},
        {"artifact_kind": "Not.Lowercase"},
        {"content_checksum": "sha1:" + "a" * 40},
        {"content_length_bytes": -1},
    ),
)
def test_an_artifact_shape_violation_has_no_column_to_live_in(
    owned: m1.Owned, overrides: dict[str, object]
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_artifact(owned, **overrides)


# --- evidence invariants ---------------------------------------------------------


def test_evidence_must_name_the_run_of_its_own_step(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        m18.insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        m18.insert_step(owned, run_step_id="step-other", run_id="run-0002", ordinal=1)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="name the run of its own step"
    ):
        insert_evidence(owned, run_step_id="step-other")


def test_evidence_must_not_predate_its_run(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not predate its run"
    ):
        insert_evidence(owned, captured_at_us=BASE_US - 1)


def test_evidence_must_not_predate_its_step(owned: m1.Owned) -> None:
    """A time the run already allows, and the step does not: only the step guard is left."""
    seed_run_with_later_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not predate its step"
    ):
        insert_evidence(owned, run_step_id=STEP_ID, captured_at_us=BETWEEN_US)
    with guarded(owned):
        insert_evidence(owned, run_step_id=STEP_ID, captured_at_us=STEP_US)


def test_evidence_naming_an_artifact_must_agree_with_its_checksum(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        insert_artifact(owned, content_checksum=DIGEST)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="agree with its checksum"
    ):
        insert_evidence(
            owned, artifact_id=ARTIFACT_ID, content_checksum=OTHER_DIGEST
        )
    with guarded(owned):
        insert_evidence(
            owned,
            evidence_item_id="evd-agree",
            artifact_id=ARTIFACT_ID,
            content_checksum=DIGEST,
        )


def test_evidence_naming_an_artifact_of_another_run_is_refused(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        m18.insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        insert_artifact(
            owned,
            artifact_id="art-elsewhere",
            run_id="run-0002",
            content_checksum=DIGEST,
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="agree with its checksum"
    ):
        insert_evidence(
            owned, artifact_id="art-elsewhere", content_checksum=DIGEST
        )


def test_only_runtime_source_evidence_may_be_authoritative(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="may be authoritative"
    ):
        insert_evidence(owned, source_kind="external_log", authoritative=1)
    with guarded(owned):
        insert_evidence(
            owned,
            evidence_item_id="evd-subordinate",
            source_kind="external_log",
            authoritative=0,
        )


def test_evidence_source_workspace_must_equal_the_evidence_workspace(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_evidence(owned, source_workspace_id=OTHER_WORKSPACE_ID)


def test_a_run_holds_at_most_256_evidence_items(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        for index in range(256):
            insert_evidence(
                owned,
                evidence_item_id=f"evd-{index:04d}",
                content_checksum=f"sha256:{index:064x}",
            )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most 256"):
        insert_evidence(
            owned, evidence_item_id="evd-overflow", content_checksum=DIGEST
        )


def test_a_terminal_run_still_admits_late_retained_evidence(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        m18.insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 1,
            event_kind="run_cancelled",
            run_status="cancelled",
        )
        insert_evidence(owned)
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {EVIDENCE}").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "overrides",
    (
        # Subordinate, so the authority guard has nothing to say and the closed-set
        # CHECK is what refuses the row. `test_only_runtime_source_evidence_may_be
        # _authoritative` keeps the authority rule under its own test.
        {"source_kind": "not_a_known_kind", "authoritative": 0},
        {"source_id": "has space"},
        {"content_checksum": "sha1:" + "a" * 40},
        {"authoritative": 2},
        {"retained": -1},
        {"evidence_kind": "Not.Lowercase"},
    ),
)
def test_an_evidence_shape_violation_has_no_column_to_live_in(
    owned: m1.Owned, overrides: dict[str, object]
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_evidence(owned, **overrides)


# --- cleanup receipt invariants ---------------------------------------------------


def test_a_cleanup_receipt_must_not_predate_its_run(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not predate its run"
    ):
        insert_cleanup_receipt(owned, performed_at_us=BASE_US - 1)


def test_cleanup_outcome_uses_the_closed_set(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_cleanup_receipt(owned, outcome="unknown_outcome")
    with guarded(owned):
        insert_cleanup_receipt(owned, outcome="failed")


def test_a_cleanup_audit_reference_must_belong_to_the_workspace(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert_cleanup_receipt(owned, audit_ref="aud-does-not-exist")


def test_a_run_holds_at_most_64_cleanup_receipts(owned: m1.Owned) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned):
        for index in range(64):
            insert_cleanup_receipt(owned, cleanup_receipt_id=f"cln-{index:04d}")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most 64"):
        insert_cleanup_receipt(owned, cleanup_receipt_id="cln-overflow")


def test_a_terminal_run_states_its_cleanup(owned: m1.Owned) -> None:
    """The ordinary case this table exists for: a closing run records what it released."""
    m18.seed_admitted_run(owned)
    drive_run_to(owned, "succeeded", event_kind="run_succeeded")
    with guarded(owned):
        insert_cleanup_receipt(owned)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {CLEANUP}").fetchone()[0] == 1


@pytest.mark.parametrize(
    "overrides",
    (
        {"resource_kind": "Not.Lowercase"},
        {"reason": "trailing."},
        {"audit_ref": "has space"},
    ),
)
def test_a_cleanup_receipt_shape_violation_has_no_column_to_live_in(
    owned: m1.Owned, overrides: dict[str, object]
) -> None:
    m18.seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_cleanup_receipt(owned, **overrides)


# --- cross-workspace isolation, proved at the key alone -----------------------------


def test_a_cross_workspace_reference_is_refused_by_the_foreign_key_alone() -> None:
    """The composite keys carry workspace scoping without help from the triggers.

    Seeded with foreign keys off, because the artifact's own parents belong to
    migrations outside this slice; switched on for the assertion, so the only thing
    left to refuse a child pointing at another workspace's run is the composite key.
    The same row in the seeded workspace commits first, so the refusal below is the
    workspace half of the key rather than a parent that was never there.
    """
    evidence_insert = (
        f"INSERT INTO {EVIDENCE} (workspace_id, evidence_item_id, run_id, "
        "evidence_kind, source_kind, source_id, source_workspace_id, "
        "content_checksum, artifact_id, captured_at_us, authoritative, "
        "retained) VALUES (?, ?, ?, 'tool_call', 'runtime', 'src', ?, ?, ?, ?, 1, 1)"
    )
    connection = ddl_only_connection()
    try:
        connection.execute(
            "INSERT INTO omnivia_runtime_runs (workspace_id, run_id) VALUES (?, ?)",
            (WORKSPACE_ID, RUN_ID),
        )
        connection.execute(
            f"INSERT INTO {ARTIFACTS} (workspace_id, artifact_id, run_id, "
            "artifact_kind, media_type, content_checksum, content_length_bytes, "
            "produced_at_us) VALUES (?, ?, ?, 'final_output', 'text/plain', ?, 1, ?)",
            (WORKSPACE_ID, ARTIFACT_ID, RUN_ID, DIGEST, BASE_US),
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            evidence_insert,
            (
                WORKSPACE_ID,
                EVIDENCE_ID,
                RUN_ID,
                WORKSPACE_ID,
                OTHER_DIGEST,
                ARTIFACT_ID,
                BASE_US,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                evidence_insert,
                (
                    OTHER_WORKSPACE_ID,
                    EVIDENCE_ID,
                    RUN_ID,
                    OTHER_WORKSPACE_ID,
                    OTHER_DIGEST,
                    ARTIFACT_ID,
                    BASE_US,
                ),
            )
    finally:
        connection.close()


def test_an_unrecognised_source_kind_has_no_column_to_live_in() -> None:
    """The closed schema vocabulary, proved without a trigger answering first."""
    connection = ddl_only_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                f"INSERT INTO {EVIDENCE} (workspace_id, evidence_item_id, run_id, "
                "evidence_kind, source_kind, source_id, source_workspace_id, "
                "content_checksum, captured_at_us, authoritative, retained) "
                "VALUES (?, ?, ?, 'tool_call', 'made_up_kind', 'src', ?, ?, ?, 0, 1)",
                (WORKSPACE_ID, EVIDENCE_ID, RUN_ID, WORKSPACE_ID, OTHER_DIGEST, BASE_US),
            )
    finally:
        connection.close()


def test_a_fractional_microsecond_has_no_column_to_live_in() -> None:
    connection = ddl_only_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                f"INSERT INTO {ARTIFACTS} (workspace_id, artifact_id, run_id, "
                "artifact_kind, media_type, content_checksum, content_length_bytes, "
                "produced_at_us) VALUES (?, ?, ?, 'final_output', 'text/plain', ?, "
                "1, ?)",
                (WORKSPACE_ID, ARTIFACT_ID, RUN_ID, DIGEST, float(BASE_US) + 0.5),
            )
    finally:
        connection.close()
