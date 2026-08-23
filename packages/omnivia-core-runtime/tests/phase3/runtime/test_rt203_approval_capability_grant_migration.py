"""RT-203 acceptance for migration 0022's canonical Approval and CapabilityGrant records.

What 0022 is: a unique consecutive successor to 0021, pinned by content checksum, whose
objects are exactly four tables and twelve statement triggers; a slice that applies to a
pristine workspace, to an exactly-adopted Phase 0 workspace and to a *populated*
workspace already at the 0021 head, without disturbing one existing row; and a schema
whose immutability is enforced rather than asserted.

Four tables, and the reason there are four rather than two. An `Approval` is one record
with two halves written at different instants, so a single row would have to be *edited*
when the decision arrives -- and a row that can be updated is an approval that can be
re-decided. Split, the rule stops being a rule: the decision table is keyed by the
approval alone, so a second decision has nowhere to live under any spelling. The comment
is the third for the same reason and no other -- the accepted contract lets a *pending*
approval carry one and lets a decision add one later, and a column on either half would
need an UPDATE to represent the second of those. The grant is the fourth and follows
0021's document-plus-digest convention exactly rather than inventing a second one.

What it is not. It carries no DML, so it is not a backfill -- no fact in this database
is honestly classifiable as an approval or a grant, and manufacturing one would invent a
decision nobody made. It adds no requester identity and no approval-to-grant edge,
because accepted v1 states neither. It does not edit 0018: the forward reference that
migration's comment anticipated cannot be added to an existing table in SQLite without
rebuilding it, so the reference runs the other way as `Approval.wait_id`, and the exact
`approval_id` a resolution quotes stays validated where `ResolveWait` already checks it.

The rules SQL can state are stated here: identity, bounds, digest shape, the byte length
that must equal the stored document's, one approval per wait, one decision and one
comment per approval, the exact run, wait, approval, policy and audit a row may name,
and the instants a request and a decision must fall between. Whether a granted
capability is in its policy's `granted_capabilities` rather than only its
`discovered_capabilities` is the accepted contract's rule, and the repository tests are
where that is held.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt202_policy_budget_snapshot_migration as m202
import test_rt202_policy_budget_snapshot_repository as r202
import test_rt203_approval_capability_grant_repository as r203
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

MIGRATION_VERSION = 22
PREDECESSOR_VERSION = 21
MIGRATION_NAME = "0022_runtime_approvals_capability_grants.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
WAIT_ID = m18.WAIT_ID
MS = r202.MS

APPROVALS = r203.APPROVALS
DECISIONS = r203.DECISIONS
COMMENTS = r203.COMMENTS
GRANTS = r203.GRANTS
TABLES = (APPROVALS, DECISIONS, COMMENTS, GRANTS)

APPROVAL_ID = r203.APPROVAL_ID

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

#: Every migration this slice builds on, pinned by content, following the prefix-claim
#: convention 0018 established: this file adds its predecessor's checksum to the
#: inherited map rather than asserting anything about what lands after it.
ACCEPTED_MIGRATION_CHECKSUMS = {
    **m202.ACCEPTED_MIGRATION_CHECKSUMS,
    m202.MIGRATION_NAME: m202.MIGRATION.checksum,
}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def ddl_only_connection() -> sqlite3.Connection:
    """The slice's four tables with none of its triggers, in memory.

    A guard trigger fires before the column constraints behind it, so on the real schema
    a trigger's message is the one a bad value gets. Replaying the DDL alone leaves the
    typed `CHECK`s and the key constraints as the sole defence, which is how a test
    proves each stands on its own.
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


def approval_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "approval_id": APPROVAL_ID,
        "run_id": RUN_ID,
        "wait_id": WAIT_ID,
        "requested_at_us": BASE_US,
        "approver_role": "runtime_approver",
        "assigned_to": None,
        "escalated_to": None,
        "expires_at_us": None,
    }
    values.update(overrides)
    return values


def decision_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "approval_id": APPROVAL_ID,
        "decision": "approved",
        "decided_at_us": BASE_US + MS,
        "decided_by": "principal-approver",
        "audit_ref": m18.audit_ref_for(JOB_ID),
    }
    values.update(overrides)
    return values


def comment_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "approval_id": APPROVAL_ID,
        "comment": "please review the export step",
    }
    values.update(overrides)
    return values


def grant_row(**overrides: object) -> dict[str, object]:
    """One complete grant row, carrying the same canonical bytes the repository writes."""
    record = r203.grant()
    document = to_canonical_json(record.to_wire())
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "capability_grant_id": record.capability_grant_id,
        "run_id": record.run_id,
        "policy_snapshot_id": record.policy_snapshot_id,
        "granted_at_us": BASE_US,
        "grant_json": document,
        "grant_digest": r202.digest_of(document),
        "grant_byte_length": len(document.encode("utf-8")),
    }
    values.update(overrides)
    return values


def seed_parents(holder: m1.Owned) -> None:
    """The run, step, pending approval wait and pinned policy every row here points at."""
    m18.seed_run_with_step(holder)
    with guarded(holder):
        m18.insert_wait(holder)
        _insert(holder, m202.POLICIES, m202.policy_row())


def seed_requested(holder: m1.Owned) -> None:
    """The same, plus the approval request a decision or a comment answers."""
    seed_parents(holder)
    with guarded(holder):
        _insert(holder, APPROVALS, approval_row())


COMPLETE_ROWS: dict[str, tuple[dict[str, object], Callable[[m1.Owned], None]]] = {
    APPROVALS: (approval_row(), seed_parents),
    DECISIONS: (decision_row(), seed_requested),
    COMMENTS: (comment_row(), seed_requested),
    GRANTS: (grant_row(), seed_parents),
}


def seed_records(holder: m1.Owned) -> None:
    """One of each of the four facts this slice adds."""
    seed_requested(holder)
    with guarded(holder):
        _insert(holder, DECISIONS, decision_row())
        _insert(holder, COMMENTS, comment_row())
        _insert(holder, GRANTS, grant_row())


def ddl_insert(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


# --- migration identity ------------------------------------------------------------


def test_0022_is_the_unique_consecutive_successor_to_0021() -> None:
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
    """0018 included: the forward foreign key it anticipated is not added by editing it."""
    package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
    for name, expected in ACCEPTED_MIGRATION_CHECKSUMS.items():
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name
    # The baseline artifact plus every migration up to this slice's predecessor.
    assert len(ACCEPTED_MIGRATION_CHECKSUMS) == MIGRATION_VERSION


def test_the_migration_carries_no_dml_and_declares_only_four_tables() -> None:
    """No approval or grant exists before this slice, so no backfill is owed."""
    for statement in MIGRATION_STATEMENTS:
        assert statement.split(None, 1)[0].upper() == "CREATE", statement[:80]
    assert sum(s.startswith("CREATE TABLE") for s in MIGRATION_STATEMENTS) == 4
    assert sum(s.startswith("CREATE TRIGGER") for s in MIGRATION_STATEMENTS) == 12


def test_no_migration_0022_gate_is_declared(owned: m1.Owned) -> None:
    names = {
        str(row[0])
        for row in owned.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_migration_0022%'"
        )
    }
    assert not names
    assert not any("omnivia_migration_0022" in s for s in MIGRATION_STATEMENTS)


def test_no_requester_or_grant_column_is_invented(owned: m1.Owned) -> None:
    """Accepted v1 has neither, and a column for a field the contract lacks is a fiction."""
    columns = {
        table: {
            str(row[1])
            for row in owned.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for table in TABLES
    }
    assert columns[APPROVALS] == {
        "workspace_id",
        "approval_id",
        "run_id",
        "wait_id",
        "requested_at_us",
        "approver_role",
        "assigned_to",
        "escalated_to",
        "expires_at_us",
    }
    assert columns[DECISIONS] == {
        "workspace_id",
        "approval_id",
        "decision",
        "decided_at_us",
        "decided_by",
        "audit_ref",
    }
    assert columns[COMMENTS] == {"workspace_id", "approval_id", "comment"}
    assert "capability_grant_id" not in columns[APPROVALS] | columns[DECISIONS]
    assert not any(
        "requested_by" in names or "requester" in names for names in columns.values()
    )


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
                "AND tbl_name IN (?, ?, ?, ?)",
                TABLES,
            )
        }
        columns = {
            str(row[1]): (str(row[2]), int(row[3]))
            for row in connection.execute(f"PRAGMA table_info({GRANTS})").fetchall()
        }
    finally:
        connection.close()
    assert triggers == TRIGGERS
    assert columns["workspace_id"] == ("TEXT", 1)
    assert columns["capability_grant_id"] == ("TEXT", 1)
    assert columns["run_id"] == ("TEXT", 1)
    assert columns["policy_snapshot_id"] == ("TEXT", 1)
    assert columns["granted_at_us"] == ("INTEGER", 1)
    assert columns["grant_json"] == ("TEXT", 1)
    assert columns["grant_digest"] == ("TEXT", 1)
    assert columns["grant_byte_length"] == ("INTEGER", 1)


def test_drift_in_any_object_this_slice_adds_is_detected(migrated: Path) -> None:
    expected = canonical_schema_fingerprint()
    connection = sqlite3.connect(str(migrated))
    try:
        connection.executescript(f"CREATE TABLE {APPROVALS}_drift (id TEXT PRIMARY KEY);")
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


def test_a_populated_0021_head_reaches_0022_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    """Applied from the exact accepted head, over data, this slice only adds."""
    path = tmp_path / "at-0021.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m18.seed_run_with_step(holder)
            with guarded(holder):
                m18.insert_attempt(holder)
                m18.insert_wait(holder)
                _insert(holder, m202.POLICIES, m202.policy_row())
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
    seed_records(owned)
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
    """A request is a request and a decision is a decision, for their own owner too."""
    seed_records(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("table", TABLES)
def test_the_complete_rows_the_guard_tests_use_would_otherwise_commit(
    owned: m1.Owned, table: str
) -> None:
    values, seed = COMPLETE_ROWS[table]
    seed(owned)
    with guarded(owned):
        _insert(owned, table, values)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("table", TABLES)
def test_no_open_guard_means_no_write_at_all(owned: m1.Owned, table: str) -> None:
    values, seed = COMPLETE_ROWS[table]
    seed(owned)
    close_guard(owned.connection)
    with (
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
    ):
        _insert(owned, table, values)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("table", TABLES)
def test_a_stale_fencing_generation_cannot_append(owned: m1.Owned, table: str) -> None:
    """The same row the current owner commits, refused at the fence rather than the row."""
    values, seed = COMPLETE_ROWS[table]
    seed(owned)
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


@pytest.mark.parametrize("table", TABLES)
def test_a_foreign_workspace_cannot_append(owned: m1.Owned, table: str) -> None:
    """A row naming another workspace is refused by the guard, under a live fence."""
    values, seed = COMPLETE_ROWS[table]
    seed(owned)
    foreign = dict(values, workspace_id=OTHER_WORKSPACE_ID)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
        _insert(owned, table, foreign)


# --- one approval, one decision, one comment ------------------------------------------


def test_one_wait_carries_at_most_one_approval() -> None:
    """A second approval on one wait has nowhere to live, under any identifier.

    Proved on the DDL alone: the unique constraint has to stand on its own, or a path
    that somehow bypassed the guard would leave one wait with two approvals to answer.
    """
    connection = ddl_only_connection()
    try:
        ddl_insert(connection, APPROVALS, approval_row())
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            ddl_insert(connection, APPROVALS, approval_row(approval_id="apr-0002"))
    finally:
        connection.close()


def test_one_approval_receives_at_most_one_decision() -> None:
    """The primary key is what makes a second decision structurally impossible.

    Not a trigger, and not a rule the repository is trusted to remember: the decision is
    keyed by the approval alone, so `approved` then `rejected` -- or the same answer
    twice -- has no row to occupy.
    """
    connection = ddl_only_connection()
    try:
        ddl_insert(connection, DECISIONS, decision_row())
        for second in (decision_row(decision="rejected"), decision_row()):
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                ddl_insert(connection, DECISIONS, second)
        assert (
            connection.execute(f"SELECT COUNT(*) FROM {DECISIONS}").fetchone()[0] == 1
        )
    finally:
        connection.close()


def test_one_approval_carries_at_most_one_comment() -> None:
    """One authoritative copy of the text, and no second answer about which one wins."""
    connection = ddl_only_connection()
    try:
        ddl_insert(connection, COMMENTS, comment_row())
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            ddl_insert(connection, COMMENTS, comment_row(comment="a different note"))
    finally:
        connection.close()


# --- an approval is bound to a pending approval wait of its own run --------------------


def test_an_approval_is_requested_only_on_an_approval_wait(owned: m1.Owned) -> None:
    """A timer that claims an approver is describing a decision nobody was asked for."""
    m18.seed_run_with_step(owned)
    with guarded(owned):
        m18.insert_wait(owned, kind="timer")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="approval wait of its own run"
    ):
        _insert(owned, APPROVALS, approval_row())


def test_an_approval_is_requested_only_on_a_wait_of_its_own_run(
    owned: m1.Owned,
) -> None:
    """Both identifiers exist; the pairing is what is refused."""
    seed_parents(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        m18.insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        m18.insert_event(owned, run_id="run-0002", runtime_event_id="evt-0002")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="approval wait of its own run"
    ):
        _insert(owned, APPROVALS, approval_row(run_id="run-0002"))


def test_an_approval_must_not_be_requested_before_its_wait(owned: m1.Owned) -> None:
    seed_parents(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="before its wait"
    ):
        _insert(owned, APPROVALS, approval_row(requested_at_us=BASE_US - 1))


def test_neither_half_lands_on_a_wait_that_is_no_longer_pending(
    owned: m1.Owned,
) -> None:
    """An approval decides a wait still waiting for it, or it decides nothing."""
    seed_requested(owned)
    with guarded(owned):
        m18.insert_wait_resolution(owned, status="cancelled", resolution_reason="run_cancelled")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only while its wait is still pending"
    ):
        _insert(owned, DECISIONS, decision_row())
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only on a wait still pending"
    ):
        _insert(owned, APPROVALS, approval_row(approval_id="apr-0002"))


def test_a_decision_must_not_predate_the_approval_it_answers(owned: m1.Owned) -> None:
    seed_requested(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="must not predate the approval"
    ):
        _insert(owned, DECISIONS, decision_row(decided_at_us=BASE_US - 1))


def test_a_decision_past_the_approval_deadline_has_expired(owned: m1.Owned) -> None:
    seed_parents(owned)
    with guarded(owned):
        _insert(owned, APPROVALS, approval_row(expires_at_us=BASE_US + MS))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="past its deadline"
    ):
        _insert(owned, DECISIONS, decision_row(decided_at_us=BASE_US + 2 * MS))
    with guarded(owned):
        _insert(owned, DECISIONS, decision_row(decided_at_us=BASE_US + MS))


def test_a_decision_past_the_wait_deadline_has_expired(owned: m1.Owned) -> None:
    """The wait's own deadline binds the decision: it is the thing being waited on."""
    m18.seed_run_with_step(owned)
    with guarded(owned):
        m18.insert_wait(owned, expires_at_us=BASE_US + MS)
        _insert(owned, m202.POLICIES, m202.policy_row())
        _insert(owned, APPROVALS, approval_row())
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="past its wait deadline"
    ):
        _insert(owned, DECISIONS, decision_row(decided_at_us=BASE_US + 2 * MS))


FOREIGN_KEYS = {
    "an approval naming no run": (APPROVALS, {"run_id": "run-nowhere"}),
    "an approval naming no wait": (APPROVALS, {"wait_id": "wait-nowhere"}),
    "a decision naming no approval": (DECISIONS, {"approval_id": "apr-nowhere"}),
    "a decision naming no audit event": (DECISIONS, {"audit_ref": "aud-nowhere"}),
    "a comment naming no approval": (COMMENTS, {"approval_id": "apr-nowhere"}),
    "a grant naming no run": (GRANTS, {"run_id": "run-nowhere"}),
    "a grant naming no policy": (GRANTS, {"policy_snapshot_id": "policy-nowhere"}),
}


@pytest.mark.parametrize("case", sorted(FOREIGN_KEYS))
def test_a_record_may_not_float_free_of_what_it_names(
    owned: m1.Owned, case: str
) -> None:
    """The composite foreign keys, and the guards that narrow them further."""
    table, override = FOREIGN_KEYS[case]
    values, seed = COMPLETE_ROWS[table]
    seed(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        _insert(owned, table, dict(values, **override))
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_a_grant_must_name_a_policy_pinned_for_its_own_run(owned: m1.Owned) -> None:
    """Both rows exist and the foreign key is satisfied; the pairing is what is refused."""
    seed_parents(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        m18.insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        m18.insert_event(owned, run_id="run-0002", runtime_event_id="evt-0002")
        _insert(
            owned,
            m202.POLICIES,
            m202.policy_row(policy_snapshot_id="policy-0002", run_id="run-0002"),
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="pinned for its own run"
    ):
        _insert(owned, GRANTS, grant_row(policy_snapshot_id="policy-0002"))


def test_a_grant_must_not_be_issued_before_the_policy_that_backs_it(
    owned: m1.Owned,
) -> None:
    seed_parents(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="before the policy that backs it"
    ):
        _insert(owned, GRANTS, grant_row(granted_at_us=BASE_US - 1))


# --- shapes with no column to live in ---------------------------------------------------


SHAPE_VIOLATIONS = {
    "an unbounded approval id": (APPROVALS, {"approval_id": "-leading-punctuation"}),
    "an empty approval id": (APPROVALS, {"approval_id": ""}),
    "a workspace with spaces": (APPROVALS, {"workspace_id": "ws with spaces"}),
    "a request instant of zero": (APPROVALS, {"requested_at_us": 0}),
    "a fractional request instant": (APPROVALS, {"requested_at_us": 1.5}),
    "a deadline before its request": (APPROVALS, {"expires_at_us": BASE_US - 1}),
    "an empty approver role": (APPROVALS, {"approver_role": ""}),
    "an unbounded assignee": (APPROVALS, {"assigned_to": "-nobody"}),
    "a decision outside the vocabulary": (DECISIONS, {"decision": "maybe"}),
    "a decision that is not text": (DECISIONS, {"decision": 1}),
    "an empty decider": (DECISIONS, {"decided_by": ""}),
    "a decision instant of zero": (DECISIONS, {"decided_at_us": 0}),
    "an unbounded audit reference": (DECISIONS, {"audit_ref": "-aud"}),
    # A blob rather than an integer: the column has TEXT affinity, so an integer is
    # converted to text before the CHECK sees it and a blob is what actually tests it.
    "a comment that is not text": (COMMENTS, {"comment": b"\x01\x02"}),
    "an uppercase grant digest": (GRANTS, {"grant_digest": "sha256:" + "F" * 64}),
    "a short grant digest": (GRANTS, {"grant_digest": "sha256:" + "a" * 63}),
    "an unprefixed grant digest": (GRANTS, {"grant_digest": "a" * 71}),
    "a grant length that is not the document's": (GRANTS, {"grant_byte_length": 1}),
    "a grant instant of zero": (GRANTS, {"granted_at_us": 0}),
}

ROW_BUILDERS: dict[str, Callable[..., dict[str, object]]] = {
    APPROVALS: approval_row,
    DECISIONS: decision_row,
    COMMENTS: comment_row,
    GRANTS: grant_row,
}


@pytest.mark.parametrize("case", sorted(SHAPE_VIOLATIONS))
def test_a_shape_violation_has_no_column_to_live_in(case: str) -> None:
    """The typed CHECKs stand on their own, with no guard trigger in front of them."""
    table, override = SHAPE_VIOLATIONS[case]
    connection = ddl_only_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            ddl_insert(connection, table, ROW_BUILDERS[table](**override))
    finally:
        connection.close()


def test_the_comment_bound_is_the_contract_s_own_and_adds_no_ceiling() -> None:
    """`Message` allows 2048 characters, empty included, and this column allows exactly that."""
    connection = ddl_only_connection()
    try:
        ddl_insert(connection, COMMENTS, comment_row(comment="n" * 2048))
        ddl_insert(connection, COMMENTS, comment_row(approval_id="apr-0002", comment=""))
        with pytest.raises(sqlite3.IntegrityError):
            ddl_insert(
                connection,
                COMMENTS,
                comment_row(approval_id="apr-0003", comment="n" * 2049),
            )
        assert connection.execute(f"SELECT COUNT(*) FROM {COMMENTS}").fetchone()[0] == 2
    finally:
        connection.close()


def test_the_largest_grant_the_contract_accepts_fits_and_no_smaller_bound_is_imposed() -> None:
    """64 scopes of 128 characters, with every bounded field at its own maximum."""
    connection = ddl_only_connection()
    try:
        largest = r203.grant(
            scopes=tuple(f"s{index:03d}" + "_" * 124 for index in range(64)),
            purpose="p" * 128,
            expires_at=r203.at(600_000),
        )
        document = to_canonical_json(largest.to_wire())
        assert len(document.encode("utf-8")) < 65_536
        ddl_insert(
            connection,
            GRANTS,
            grant_row(
                grant_json=document,
                grant_digest=r202.digest_of(document),
                grant_byte_length=len(document.encode("utf-8")),
            ),
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0] == 1
    finally:
        connection.close()


def test_sql_bounds_the_stored_grant_and_does_not_pretend_to_decode_it() -> None:
    """SQL is not the layer that decides whether a document is a `CapabilityGrant`.

    A CHECK that tried would be a second, weaker copy of the generated decoder. The
    bytes are bounded, typed and tied to their length and digest shape here; the
    repository's reader is what refuses a document that decodes to nothing usable, and
    `test_rt203_approval_capability_grant_repository` is where that is held.
    """
    connection = ddl_only_connection()
    try:
        document = '{"not":"a grant"}'
        ddl_insert(
            connection,
            GRANTS,
            grant_row(
                grant_json=document,
                grant_digest=r202.digest_of(document),
                grant_byte_length=len(document.encode("utf-8")),
            ),
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            ddl_insert(
                connection,
                GRANTS,
                grant_row(
                    capability_grant_id="cap-0002",
                    grant_json="x" * 65_537,
                    grant_byte_length=65_537,
                ),
            )
    finally:
        connection.close()
