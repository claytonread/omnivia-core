"""RT-202 acceptance for migration 0021's canonical PolicySnapshot and BudgetSnapshot
records.

What 0021 is: a unique consecutive successor to 0020, pinned by content checksum, whose
objects are exactly two tables and six statement triggers; a slice that applies to a
pristine workspace, to an exactly-adopted Phase 0 workspace and to a *populated*
workspace already at the 0020 head, without disturbing one existing row; and a schema
whose immutability is enforced rather than asserted.

What it is not. It carries no DML, so it is not a backfill -- no fact in this database
is honestly classifiable as a policy or budget decision, and manufacturing one would
invent a grant nobody issued and a ceiling nobody accepted. It is not a second ledger,
policy taxonomy or blob store: the stored document is the accepted v1 wire record, and
the digest beside it addresses exactly the bytes stored. It adds no capability, grant,
approval or effect relation, and makes no public wire change.

The rules SQL can state are stated here: identity, bounds, digest shape, the byte length
that must equal the stored document's, one snapshot per run per revision, a revision
that advances, an instant that does not go backwards, and a snapshot that cannot predate
its run. Whether a later revision's *content* is a legal successor is the accepted
contract's rule, and the repository tests are where that is held.
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
import test_rt103_artifact_evidence_cleanup_migration as m19
import test_rt202_policy_budget_snapshot_repository as r202
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

from omnivia_core.contracts.v1 import to_canonical_json

MIGRATION_VERSION = 21
PREDECESSOR_VERSION = 20
MIGRATION_NAME = "0021_runtime_policy_budget_snapshots.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
MS = r202.MS

POLICIES = "omnivia_runtime_policy_snapshots"
BUDGETS = "omnivia_runtime_budget_snapshots"
TABLES = (POLICIES, BUDGETS)

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

#: Every migration this slice builds on, pinned by content, following the prefix-claim
#: convention 0018 established: this file adds its predecessors' checksums to the
#: inherited map rather than asserting anything about what lands after it.
ACCEPTED_MIGRATION_CHECKSUMS = {
    **m19.ACCEPTED_MIGRATION_CHECKSUMS,
    m19.MIGRATION_NAME: m19.MIGRATION.checksum,
    "0020_runtime_run_summary_projection.sql": (
        "3451223fe623848d8d0db42a3a72f23f1948b276bd89cd0a127c257820d36841"
    ),
}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def ddl_only_connection() -> sqlite3.Connection:
    """The slice's two tables with none of its triggers, in memory.

    A guard trigger fires before the column constraints behind it, so on the real schema
    a trigger's message is the one a bad value gets. Replaying the DDL alone leaves the
    typed `CHECK`s as the sole defence, which is how a test proves one stands on its own.
    """
    connection = sqlite3.connect(":memory:")
    connection.isolation_level = None
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


def policy_row(**overrides: object) -> dict[str, object]:
    """One complete policy row, carrying the same canonical bytes the repository writes."""
    record = r202.policy()
    document = to_canonical_json(record.to_wire())
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "policy_snapshot_id": record.policy_snapshot_id,
        "run_id": record.run_id,
        "revision": record.revision,
        "pinned_at_us": BASE_US,
        "snapshot_json": document,
        "snapshot_digest": r202.digest_of(document),
        "snapshot_byte_length": len(document.encode("utf-8")),
    }
    values.update(overrides)
    return values


def budget_row(**overrides: object) -> dict[str, object]:
    record = r202.budget()
    document = to_canonical_json(record.to_wire())
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "budget_snapshot_id": record.budget_snapshot_id,
        "run_id": record.run_id,
        "revision": record.revision,
        "pinned_at_us": BASE_US,
        "snapshot_json": document,
        "snapshot_digest": r202.digest_of(document),
        "snapshot_byte_length": len(document.encode("utf-8")),
    }
    values.update(overrides)
    return values


COMPLETE_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    (POLICIES, policy_row()),
    (BUDGETS, budget_row()),
)


def seed_snapshots(holder: m1.Owned) -> None:
    m18.seed_admitted_run(holder)
    with guarded(holder):
        _insert(holder, POLICIES, policy_row())
        _insert(holder, BUDGETS, budget_row())


# --- migration identity ------------------------------------------------------------


def test_0021_is_the_unique_consecutive_successor_to_0020() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    # A prefix claim, not a claim about the head: this slice has authority over its own
    # position in the sequence and none over what lands after it.
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
    # The baseline artifact plus every migration up to this slice's predecessor.
    assert len(ACCEPTED_MIGRATION_CHECKSUMS) == MIGRATION_VERSION


def test_the_migration_carries_no_dml_and_declares_only_two_tables() -> None:
    """No policy or budget decision exists before this slice, so no backfill is owed."""
    for statement in MIGRATION_STATEMENTS:
        assert statement.split(None, 1)[0].upper() == "CREATE", statement[:80]
    assert sum(s.startswith("CREATE TABLE") for s in MIGRATION_STATEMENTS) == 2
    assert sum(s.startswith("CREATE TRIGGER") for s in MIGRATION_STATEMENTS) == 6


def test_no_migration_0021_gate_is_declared(owned: m1.Owned) -> None:
    names = {
        str(row[0])
        for row in owned.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_migration_0021%'"
        )
    }
    assert not names
    assert not any("omnivia_migration_0021" in s for s in MIGRATION_STATEMENTS)


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
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(TABLES) <= names
        verify_fingerprint(connection, canonical_schema_fingerprint())
        assert_guards_intact(connection)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN (?, ?)",
                TABLES,
            )
        }
        columns = {
            str(row[1]): (str(row[2]), int(row[3]))
            for row in connection.execute(f"PRAGMA table_info({POLICIES})").fetchall()
        }
    finally:
        connection.close()
    assert triggers == TRIGGERS
    assert columns["workspace_id"] == ("TEXT", 1)
    assert columns["policy_snapshot_id"] == ("TEXT", 1)
    assert columns["run_id"] == ("TEXT", 1)
    assert columns["revision"] == ("INTEGER", 1)
    assert columns["pinned_at_us"] == ("INTEGER", 1)
    assert columns["snapshot_json"] == ("TEXT", 1)
    assert columns["snapshot_digest"] == ("TEXT", 1)
    assert columns["snapshot_byte_length"] == ("INTEGER", 1)


def test_drift_in_any_object_this_slice_adds_is_detected(migrated: Path) -> None:
    expected = canonical_schema_fingerprint()
    connection = sqlite3.connect(str(migrated))
    try:
        connection.executescript(f"CREATE TABLE {POLICIES}_drift (id TEXT PRIMARY KEY);")
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


def test_a_populated_0020_head_reaches_0021_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    """Applied from the exact accepted head, over data, this slice only adds."""
    path = tmp_path / "at-0020.sqlite"
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
            for table in TABLES:
                assert (
                    head.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    is None
                )
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
    seed_snapshots(owned)
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
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        connection.close()


# --- immutability and authority -----------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned, table: str
) -> None:
    """An accepted decision cannot be rewritten or erased, not even by its own owner."""
    seed_snapshots(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET revision = revision + 1")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_the_complete_rows_the_guard_tests_use_would_otherwise_commit(
    owned: m1.Owned,
) -> None:
    m18.seed_admitted_run(owned)
    for table, values in COMPLETE_ROWS:
        with guarded(owned):
            _insert(owned, table, values)
        assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_no_open_guard_means_no_snapshot_write_at_all(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    m18.seed_admitted_run(owned)
    close_guard(owned.connection)
    with (
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
    ):
        _insert(owned, table, values)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_a_stale_fencing_generation_cannot_append_a_snapshot(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    """The same row the current owner commits, refused at the fence rather than the row."""
    m18.seed_admitted_run(owned)
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
def test_a_foreign_workspace_cannot_append_a_snapshot(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    """A row naming another workspace is refused by the guard, under a live fence."""
    m18.seed_admitted_run(owned)
    foreign = dict(values, workspace_id=OTHER_WORKSPACE_ID)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
        _insert(owned, table, foreign)


# --- history invariants ---------------------------------------------------------------


def test_a_run_holds_one_snapshot_per_revision() -> None:
    """A second decision at an accepted revision has nowhere to live, under any id.

    Proved on the DDL alone, because the insert guard's advancing-revision rule answers
    first on the real schema: the unique constraint has to stand on its own, or a path
    that somehow bypassed the trigger would leave two decisions at one revision.
    """
    connection = ddl_only_connection()
    try:
        first = policy_row()
        connection.execute(
            f"INSERT INTO {POLICIES} ({', '.join(first)}) "
            f"VALUES ({', '.join('?' for _ in first)})",
            tuple(first.values()),
        )
        second = policy_row(policy_snapshot_id="policy-0002")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO {POLICIES} ({', '.join(second)}) "
                f"VALUES ({', '.join('?' for _ in second)})",
                tuple(second.values()),
            )
    finally:
        connection.close()


@pytest.mark.parametrize("table", TABLES)
def test_a_revision_must_advance_on_the_latest_stored_one(
    owned: m1.Owned, table: str
) -> None:
    seed_snapshots(owned)
    identifier = "policy_snapshot_id" if table == POLICIES else "budget_snapshot_id"
    row = policy_row() if table == POLICIES else budget_row()
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must advance"):
        _insert(
            owned,
            table,
            dict(row, revision=1, **{identifier: "snap-0002"}),
        )


@pytest.mark.parametrize("table", TABLES)
def test_a_snapshot_must_not_predate_its_run(owned: m1.Owned, table: str) -> None:
    m18.seed_admitted_run(owned)
    row = policy_row() if table == POLICIES else budget_row()
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not predate"):
        _insert(owned, table, dict(row, pinned_at_us=BASE_US - 1))


@pytest.mark.parametrize("table", TABLES)
def test_a_revision_must_not_be_pinned_before_an_earlier_one(
    owned: m1.Owned, table: str
) -> None:
    """History that runs backwards is unreadable in order, whatever the values say."""
    m18.seed_admitted_run(owned)
    identifier = "policy_snapshot_id" if table == POLICIES else "budget_snapshot_id"
    row = policy_row() if table == POLICIES else budget_row()
    with guarded(owned):
        _insert(owned, table, dict(row, pinned_at_us=BASE_US + 2 * MS))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="not be pinned before an earlier one"
    ):
        _insert(
            owned,
            table,
            dict(
                row,
                revision=2,
                pinned_at_us=BASE_US + MS,
                **{identifier: "snap-0002"},
            ),
        )


@pytest.mark.parametrize("table", TABLES)
def test_a_snapshot_must_name_a_run_this_workspace_holds(
    owned: m1.Owned, table: str
) -> None:
    """The composite foreign key, so a decision cannot float free of its run."""
    m18.seed_admitted_run(owned)
    row = policy_row() if table == POLICIES else budget_row()
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        _insert(owned, table, dict(row, run_id="run-nowhere"))


# --- shapes with no column to live in ---------------------------------------------------


@pytest.mark.parametrize(
    "override",
    (
        {"revision": 0},
        {"revision": -1},
        {"revision": "one"},
        {"pinned_at_us": 0},
        {"pinned_at_us": 1.5},
        {"snapshot_digest": "sha256:" + "F" * 64},
        {"snapshot_digest": "sha256:" + "a" * 63},
        {"snapshot_digest": "a" * 71},
        {"snapshot_byte_length": 1},
        {"policy_snapshot_id": "-leading-punctuation"},
        {"policy_snapshot_id": ""},
        {"workspace_id": "ws with spaces"},
    ),
)
def test_a_shape_violation_has_no_column_to_live_in(override: dict[str, object]) -> None:
    """The typed CHECKs stand on their own, with no guard trigger in front of them."""
    connection = ddl_only_connection()
    try:
        values = policy_row(**override)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {POLICIES} ({', '.join(values)}) "
                f"VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )
    finally:
        connection.close()


def test_a_revision_carries_no_arbitrary_upper_bound() -> None:
    """The accepted contract requires a revision of at least 1 and caps it nowhere.

    A column that refused a revision the contract accepts would be a bound this database
    has no authority to impose, exactly as 0021's own comment says of the document size.
    """
    connection = ddl_only_connection()
    try:
        values = policy_row(revision=1000)
        connection.execute(
            f"INSERT INTO {POLICIES} ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {POLICIES}").fetchone()[0] == 1
    finally:
        connection.close()


def test_sql_bounds_the_stored_document_and_does_not_pretend_to_decode_it() -> None:
    """SQL is not the layer that decides whether a document is a `PolicySnapshot`.

    A CHECK that tried would be a second, weaker copy of the generated decoder. The
    bytes are bounded, typed and tied to their length and digest shape here; the
    repository's reader is what refuses a document that decodes to nothing usable, and
    `test_rt202_policy_budget_snapshot_repository` is where that is held.
    """
    connection = ddl_only_connection()
    try:
        document = '{"not":"a snapshot"}'
        values = policy_row(
            snapshot_json=document,
            snapshot_digest=r202.digest_of(document),
            snapshot_byte_length=len(document.encode("utf-8")),
        )
        connection.execute(
            f"INSERT INTO {POLICIES} ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {POLICIES}").fetchone()[0] == 1
        oversized = policy_row(snapshot_json="x" * 131_073, snapshot_byte_length=131_073)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {POLICIES} ({', '.join(oversized)}) "
                f"VALUES ({', '.join('?' for _ in oversized)})",
                tuple(oversized.values()),
            )
    finally:
        connection.close()
