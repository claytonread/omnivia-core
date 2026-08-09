"""V06-3 Lane B acceptance for migration 0012, the durable search projection.

Two claims are load-bearing here and neither is provable by reading the SQL:

* **The projection is ordinary guarded storage.** `omnivia_evidence_search_documents`
  carries all three statement guards, is derived into `GUARDED_TABLES` like every other
  persisted table, and refuses every write that is not fenced, leased and
  workspace-scoped. Its content is bound to a *run*, so the pointer decides what a
  reader may see and nothing here writes that pointer.
* **No virtual table is persisted.** The canonical schema after 0012 contains no
  `CREATE VIRTUAL TABLE` and no FTS5 shadow table, which is what keeps
  `test_fencing_mutation.py::test_sb05_every_mutable_table_is_guarded_for_every_statement`
  true: SQLite refuses triggers on virtual tables, so a persisted FTS5 index would be
  six ungovernable tables in the authoritative database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_projection_lifecycle_migration as m5
from omnivia_core_runtime.ownership.fencing import close_guard, guarded_tables
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.projections.fts import DOCUMENTS_TABLE

MIGRATION_VERSION = 12
MIGRATION_NAME = "0012_evidence_search_projection.sql"
WORKSPACE_ID = m2.WORKSPACE_ID
EVIDENCE_ID = str(m2.EVIDENCE_DEFAULTS["evidence_id"])

TABLES = {DOCUMENTS_TABLE}
INDEXES: set[str] = set()
TRIGGERS = {
    f"omnivia_guard_evidence_search_documents_{verb}"
    for verb in ("insert", "update", "delete")
}

#: The reader's only query is "every document of one run, in `evidence_id` order", and
#: the `WITHOUT ROWID` primary key already serves it. Pinned as an empty set so adding
#: an index becomes a reviewed decision rather than a quiet one.
assert INDEXES == set()

INVALID_IDENTIFIERS = (None, "", "x" * 129, "bad space", "nul\x00id", b"id")
#: No integer here, deliberately. The column has TEXT affinity, so SQLite converts `7`
#: to `'7'` before any CHECK runs and `typeof() = 'text'` is satisfied by a value the
#: caller passed as a number. A BLOB has no such conversion and is refused.
INVALID_SEARCH_TEXT = (None, "x" * 4097, "nul\x00text", b"text")


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    """A workspace at 0012 with one evidence artifact and one running projection run."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    m5.seed_ledger(holder)
    m5.start_run(holder)
    yield holder
    holder.connection.close()


def document(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "projection_id": "projection-1",
        "run_id": "run-1",
        "evidence_id": EVIDENCE_ID,
        "search_text": "filesystem.archive doc-1 archive://doc.md",
    }
    row.update(overrides)
    return row


def write_document(holder: m2.Owned, **overrides: object) -> None:
    with m5.guarded(holder):
        m5.insert(holder.connection, DOCUMENTS_TABLE, document(**overrides))


def count(connection: sqlite3.Connection) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE}").fetchone()[0])


# --- lineage and inventory ----------------------------------------------------


def test_lb_m1_0012_is_the_unique_consecutive_successor_to_0011() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, 13))
    assert migrations[-1].name == MIGRATION_NAME
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode()).hexdigest()


def test_lb_m2_every_predecessor_is_byte_for_byte_unchanged() -> None:
    """0012 is additive. Editing an applied migration is a different, refused, act."""
    accepted = {**m5.ACCEPTED_PREDECESSOR_HASHES, 11: m5.MIGRATION.checksum}
    assert {
        m.version: m.checksum for m in load_migrations() if m.version <= 11
    } == accepted


def test_lb_m3_the_migration_adds_exactly_one_table_and_its_three_guards(
    owned: m2.Owned,
) -> None:
    assert max(applied_migrations(owned.connection)) == MIGRATION_VERSION
    assert owned.connection.execute("PRAGMA user_version").fetchone() == (12,)
    before = sqlite3.connect(":memory:")
    try:
        before.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version <= 11:
                before.executescript(migration.sql)
        for kind, expected in (
            ("table", TABLES),
            ("index", INDEXES),
            ("trigger", TRIGGERS),
            ("view", set()),
        ):
            assert (
                m2.object_names(owned.connection, kind) - m2.object_names(before, kind)
                == expected
            ), kind
    finally:
        before.close()


def test_lb_m4_the_projection_is_a_guarded_table_and_no_virtual_table_is_persisted(
    owned: m2.Owned,
) -> None:
    """The design claim, asserted rather than argued.

    A persisted `CREATE VIRTUAL TABLE ... USING fts5` would add the table plus five
    shadow tables, none of which can take a trigger, and SB-05's exemption assertion
    -- unguarded tables are *exactly* the substrate -- would fail. Checking for the
    statement and for the shadow suffixes catches both the direct spelling and a shadow
    table left behind by an earlier one.
    """
    assert DOCUMENTS_TABLE in guarded_tables()
    assert DOCUMENTS_TABLE in canonical_schema_tables()
    assert "VIRTUAL TABLE" not in " ".join(MIGRATION_STATEMENTS).upper()
    for name in canonical_schema_tables():
        assert not name.endswith(("_data", "_idx", "_content", "_docsize", "_config"))
    covered = {
        re.search(r"BEFORE\s+(INSERT|UPDATE|DELETE)\s+ON", str(row[0]), re.IGNORECASE).group(1)
        for row in owned.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (DOCUMENTS_TABLE,),
        )
    }
    assert covered == {"INSERT", "UPDATE", "DELETE"}


def test_lb_m4b_no_statement_carries_a_comment_inside_its_body(
    owned: m2.Owned,
) -> None:
    """A comment inside a `CREATE` body makes the live schema differ from the canonical.

    The migrator applies this file through `split_sql_statements`, which strips
    comments; `canonical_schema_fingerprint()` replays the same text with
    `executescript`, which does not. So an inline comment is stored in `sqlite_master`
    by one path and not the other, and every "the live workspace matches the canonical
    schema" assertion in the suite fails with identical object counts and a different
    digest -- which is a genuinely hard failure to read. Asserted here, at the file that
    would cause it, rather than left to be rediscovered.
    """
    for statement in MIGRATION_STATEMENTS:
        assert "--" not in statement, statement[:120]
    live = {
        (str(row[0]), str(row[1])): " ".join(str(row[2] or "").split())
        for row in owned.connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    canonical = sqlite3.connect(":memory:")
    try:
        canonical.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            canonical.executescript(migration.sql)
        assert live == {
            (str(row[0]), str(row[1])): " ".join(str(row[2] or "").split())
            for row in canonical.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        canonical.close()


def test_lb_m5_integrity_and_foreign_keys_are_clean(owned: m2.Owned) -> None:
    write_document(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- the guards ---------------------------------------------------------------


def test_lb_m6_an_unfenced_writer_cannot_append_or_delete(owned: m2.Owned) -> None:
    write_document(owned)
    close_guard(owned.connection)
    append = (
        f"INSERT INTO {DOCUMENTS_TABLE} (workspace_id, projection_id, run_id, "
        "evidence_id, search_text) VALUES (?, ?, ?, ?, ?)"
    )
    for statement, parameters in (
        (append, (WORKSPACE_ID, "projection-1", "run-1", "evd-other", "x")),
        (f"DELETE FROM {DOCUMENTS_TABLE}", ()),
    ):
        with pytest.raises(sqlite3.DatabaseError, match=m2.REFUSED_EXTERNAL_WRITE):
            owned.connection.execute(statement, parameters)


def test_lb_m7_update_is_never_permitted_even_under_full_authority(
    owned: m2.Owned,
) -> None:
    """Append-only, and the refusal does not depend on *what* the update changes."""
    write_document(owned)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), m5.guarded(owned):
        owned.connection.execute(f"UPDATE {DOCUMENTS_TABLE} SET search_text = 'x'")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"workspace_id": "ws-other-0001"}, "unguarded INSERT"),
        ({"run_id": "run-absent"}, "requires a running projection run"),
        ({"evidence_id": "evd-absent"}, "requires a known evidence artifact"),
    ],
)
def test_lb_m8_the_insert_guard_refuses_each_way_a_document_could_be_wrong(
    owned: m2.Owned, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(sqlite3.DatabaseError, match=message), m5.guarded(owned):
        m5.insert(owned.connection, DOCUMENTS_TABLE, document(**overrides))


def test_lb_m9_a_document_identity_is_declared_once(owned: m2.Owned) -> None:
    write_document(owned)
    with pytest.raises(sqlite3.DatabaseError, match="already exists"):
        write_document(owned)
    assert count(owned.connection) == 1


def test_lb_m10_leaving_running_closes_the_run_to_further_documents(
    owned: m2.Owned,
) -> None:
    """A late row would make the projection differ from what its digest attests to.

    The refusal arrives at the *state* check rather than at a validation-exists check,
    and that is the whole point: content is appended only while a run is `running`, a
    run never returns to `running`, so one condition closes the window that both of
    0011's evidence tables need two conditions to close.
    """
    write_document(owned)
    m5.begin_validation(owned)
    with pytest.raises(sqlite3.DatabaseError, match="requires a running"), m5.guarded(
        owned
    ):
        m5.insert(
            owned.connection,
            DOCUMENTS_TABLE,
            document(evidence_id=str(m2.UNIQUE_IDS[m2.EVIDENCE]["evidence_id"])),
        )
    m5.validate(owned)
    m5.finish_success(owned)
    with pytest.raises(sqlite3.DatabaseError, match="requires a running"), m5.guarded(
        owned
    ):
        m5.insert(
            owned.connection,
            DOCUMENTS_TABLE,
            document(evidence_id=str(m2.UNIQUE_IDS[m2.EVIDENCE]["evidence_id"])),
        )


def test_lb_m11_the_active_build_may_not_be_deleted_and_a_superseded_one_may(
    owned: m2.Owned,
) -> None:
    """Derived content is reclaimable; the content being served from is not."""
    write_document(owned)
    m5.begin_validation(owned)
    m5.validate(owned)
    m5.finish_success(owned)
    m5.activate(owned)

    with pytest.raises(
        sqlite3.DatabaseError, match="active projection build"
    ), m5.guarded(owned):
        owned.connection.execute(f"DELETE FROM {DOCUMENTS_TABLE}")
    assert count(owned.connection) == 1

    m5.complete_run(owned, "run-2", epoch=2, start=m5.BASE_US + 100)
    m5.activate(
        owned, "run-2", sequence=1, epoch=2, previous="run-1", at=m5.BASE_US + 110
    )
    with m5.guarded(owned):
        owned.connection.execute(
            f"DELETE FROM {DOCUMENTS_TABLE} WHERE run_id = 'run-1'"
        )
    assert count(owned.connection) == 0


def test_lb_m12_a_document_cannot_outlive_the_run_it_belongs_to(
    owned: m2.Owned,
) -> None:
    """The foreign key, on its own -- the run table refuses DELETE, so this is the
    other direction: a document naming a run that was never declared."""
    with pytest.raises(sqlite3.DatabaseError), m5.guarded(owned):
        m5.insert(
            owned.connection,
            DOCUMENTS_TABLE,
            document(projection_id="projection-absent"),
        )


# --- the declared domains -----------------------------------------------------


@pytest.mark.parametrize("field", ["workspace_id", "projection_id", "run_id", "evidence_id"])
@pytest.mark.parametrize("value", INVALID_IDENTIFIERS)
def test_lb_m13_every_identifier_column_refuses_its_whole_invalid_domain(
    owned: m2.Owned, field: str, value: object
) -> None:
    with pytest.raises(sqlite3.DatabaseError), m5.guarded(owned):
        m5.insert(owned.connection, DOCUMENTS_TABLE, document(**{field: value}))


@pytest.mark.parametrize("value", INVALID_SEARCH_TEXT)
def test_lb_m14_search_text_refuses_nul_overlength_and_non_text(
    owned: m2.Owned, value: object
) -> None:
    with pytest.raises(sqlite3.DatabaseError), m5.guarded(owned):
        m5.insert(owned.connection, DOCUMENTS_TABLE, document(search_text=value))


def test_lb_m15_an_empty_search_text_is_a_member_not_a_defect(
    owned: m2.Owned,
) -> None:
    """An artifact whose locator is NULL still belongs to the projection.

    Dropping it instead would make the projection's row count disagree with the
    workspace's, which is the discrepancy validation exists to catch.
    """
    write_document(owned, search_text="")
    assert count(owned.connection) == 1


def test_lb_m16_the_migration_declares_storage_and_writes_no_row() -> None:
    """0012 is DDL. A migration that seeded the ledger would be writing workspace data
    from a path that holds no mutation guard."""
    executable = " ".join(" ".join(s.split()).upper() for s in MIGRATION_STATEMENTS)
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        occurrences = executable.count(verb)
        inside_triggers = sum(
            " ".join(s.split()).upper().count(verb)
            for s in MIGRATION_STATEMENTS
            if " ".join(s.split()).upper().startswith("CREATE TRIGGER")
        )
        assert occurrences == inside_triggers, verb


def test_lb_m17_the_reader_query_is_served_without_a_second_index(
    owned: m2.Owned,
) -> None:
    """`WITHOUT ROWID` on `(workspace_id, projection_id, run_id, evidence_id)` is the
    covering index for the only query this table has, so declaring another would be
    storage nothing reads."""
    write_document(owned)
    plan = owned.connection.execute(
        f"EXPLAIN QUERY PLAN SELECT evidence_id, search_text FROM {DOCUMENTS_TABLE} "
        "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
        "ORDER BY evidence_id ASC",
        (WORKSPACE_ID, "projection-1", "run-1"),
    ).fetchall()
    detail = " ".join(str(row[3]) for row in plan)
    assert "SCAN" not in detail, detail
    assert "TEMP B-TREE" not in detail, detail


def test_lb_m18_the_run_binding_is_what_makes_activation_atomic(
    owned: m2.Owned,
) -> None:
    """Two runs' documents coexist, and which one is readable is the pointer's answer.

    This is the property that lets a build append while the previous build still serves
    reads, with no second write at activation time and therefore no window in which a
    reader sees half a projection.
    """
    write_document(owned)
    m5.begin_validation(owned)
    m5.validate(owned)
    m5.finish_success(owned)
    m5.activate(owned)
    m5.start_run(owned, run_id="run-2", epoch=2, started_at=m5.BASE_US + 100)
    write_document(owned, run_id="run-2", search_text="rebuilt")

    served = owned.connection.execute(
        f"SELECT d.search_text FROM {DOCUMENTS_TABLE} d "
        "JOIN omnivia_projection_ledger l ON l.projection_id = d.projection_id "
        "AND l.active_run_id = d.run_id",
    ).fetchall()
    assert [row[0] for row in served] == ["filesystem.archive doc-1 archive://doc.md"]
    assert count(owned.connection) == 2


def test_lb_m19_the_ledger_pointer_still_moves_only_through_activation(
    owned: m2.Owned,
) -> None:
    """0011's rule, re-asserted with 0012 applied: adding the projection's content did
    not add a second way to change which content is active."""
    write_document(owned)
    m5.begin_validation(owned)
    m5.validate(owned)
    m5.finish_success(owned)
    m5.activate(owned)
    with pytest.raises(
        sqlite3.DatabaseError, match="requires matching activation"
    ), m5.guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_ledger SET active_run_id = 'run-2' "
            "WHERE projection_id = 'projection-1'"
        )
    row = owned.connection.execute(
        "SELECT active_run_id FROM omnivia_projection_ledger "
        "WHERE projection_id = 'projection-1'"
    ).fetchone()
    assert row[0] == "run-1"


def test_lb_m20_reapplying_the_migration_is_a_no_op(tmp_path: Path) -> None:
    """`IF NOT EXISTS` throughout, so a replay converges rather than failing."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)

    def schema() -> str:
        return json.dumps(
            sorted(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT name, COALESCE(sql, '') FROM sqlite_master"
                )
            )
        )

    try:
        before = schema()
        connection.executescript(MIGRATION.sql)
        assert schema() == before
    finally:
        connection.close()
