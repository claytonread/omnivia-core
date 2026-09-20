"""Acceptance for migration 0041's canonical evidence source identity.

What 0041 is: one named UNIQUE index over
`(workspace_id, source_kind, source_native_id, source_locator, source_retrieved_at_us)`
and nothing else -- no table, no column, no trigger, no DML. 0008 left that tuple
non-unique, which was tolerable while capture was the one writer and read before it
wrote; `evidence.capture` makes it a public operation, and "read, decide, insert" is not
an invariant two concurrent callers can both satisfy.

What this file holds to.

*The nullable members are in the key.* A direct submission carries no locator and no
retrieval time, and under plain SQL NULL semantics every such row is distinct from every
other, so the uniqueness that matters most would be the one that enforced nothing. The
index puts each nullable member in through a sentinel of a different storage class from
any value 0008's own CHECKs admit for it, and the tests below insist both that a repeat
is refused and that a genuinely different locator or retrieval time is still allowed.

*It fails closed on legacy collisions.* A workspace already holding two artifacts under
one identity does not migrate. It stays at 0040 with both rows intact and the refusal
recorded, because deciding which of two conflicting captures was the real one is not
something a schema change may do silently.

*It is immutable once applied.* Pinned by content here, and an edited 0041 is refused by
the migrator rather than re-applied.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    Migration,
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 41
PREDECESSOR_VERSION = 40
MIGRATION_NAME = "0041_evidence_source_identity.sql"

#: Pinned by content. An edit to the accepted SQL is a defect this file reports rather
#: than something a later reader has to notice in a diff.
MIGRATION_CHECKSUM = "174f07a6a4de996ef24943b9f6a0f2e67c3b084bf1032eadd00cf92c9e8d4341"

INDEX = "omnivia_idx_evidence_artifacts_source_identity"
EVIDENCE = m2.EVIDENCE

WORKSPACE_ID = m2.WORKSPACE_ID
OTHER_WORKSPACE_ID = m2.OTHER_WORKSPACE_ID
BASE_US = m2.BASE_US

#: The shape this slice exists for: a submitted document, which names no locator and no
#: retrieval time, so its identity is the three-column half of the tuple plus two NULLs.
DIRECT_SUBMISSION = "direct_submission"

#: The refusal SQLite raises when the tuple repeats. It names the index rather than a
#: column list because the key carries expressions, which is the more useful message:
#: the index name states the invariant that was broken.
COLLISION = f"UNIQUE constraint failed: index '{INDEX}'"


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    """A workspace adopted from the frozen Phase 0 artifact and fully migrated."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    return path


def submission(**overrides: object) -> dict[str, object]:
    """One evidence row in the direct-submission shape: no locator, no retrieval time.

    `staged_source_ref` is NULL because submitted bytes pass through no staging row, and
    0008's guard binds a staging reference, when there is one, to the artifact's own
    source kind -- which the M2 fixture's staging does not share.
    """
    values: dict[str, object] = {
        "source_kind": DIRECT_SUBMISSION,
        "source_native_id": "doc-1",
        "source_locator": None,
        "source_retrieved_at_us": None,
        "staged_source_ref": None,
    }
    values.update(overrides)
    return values


def unguarded() -> sqlite3.Connection:
    """An empty evidence table with 0041's index and none of 0008's triggers before it.

    In a real workspace the INSERT guard is consulted first, so a claim about what the
    index refuses would never reach the index. Here it is the only thing standing. Only
    the rows the artifact's own foreign keys need are seeded, so every artifact below is
    one this test wrote.
    """
    connection = m2.replay_without_m2_triggers()
    connection.executescript(MIGRATION.sql)
    for job in m2.JOB_ROWS:
        m2.insert(connection, m2.DURABLE_JOBS, dict(job))
    for table in (m2.BLOBS, m2.STAGED):
        m2.insert(connection, table, m2.row_for(table))
    m2.insert(
        connection,
        m2.BLOBS,
        m2.row_for(m2.BLOBS, workspace_id=OTHER_WORKSPACE_ID),
    )
    return connection


def add(connection: sqlite3.Connection, evidence_id: str, **identity: object) -> None:
    m2.insert(
        connection,
        EVIDENCE,
        m2.row_for(EVIDENCE, evidence_id=evidence_id, **identity),
    )


# --- the migration itself -----------------------------------------------------------


def test_0041_applies_cleanly_as_the_consecutive_successor(migrated: Path) -> None:
    """A pristine catalogue reaches 41, records it, and stays internally consistent.

    A prefix claim, not a claim about the head, as every sibling slice test makes:
    41 must be the 41st of a gapless sequence and must be applied and recorded, and
    a later slice appending 42 is not this file's business.
    """
    versions = [m.version for m in load_migrations()]
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.name == MIGRATION_NAME

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        recorded = applied_migrations(connection)
        assert MIGRATION_VERSION in recorded
        assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
        assert recorded[PREDECESSOR_VERSION] != MIGRATION.checksum
        assert connection.execute("PRAGMA user_version").fetchone() == (
            max(m.version for m in load_migrations()),
        )
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert m2.fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert INDEX in m2.object_names(connection, "index")
    finally:
        connection.close()


def test_0041_adds_one_index_and_touches_nothing_else() -> None:
    """One statement, one new object, and no DML anywhere in what executes."""
    assert len(MIGRATION_STATEMENTS) == 1
    statement = " ".join(MIGRATION_STATEMENTS[0].split())
    assert statement.upper().startswith("CREATE UNIQUE INDEX IF NOT EXISTS")
    executed = statement.upper()
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE TABLE"):
        assert forbidden not in executed, forbidden

    without = sqlite3.connect(":memory:")
    with_41 = sqlite3.connect(":memory:")
    try:
        for connection in (without, with_41):
            connection.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version < MIGRATION_VERSION:
                without.executescript(migration.sql)
            if migration.version <= MIGRATION_VERSION:
                with_41.executescript(migration.sql)

        before = m2.fingerprint_schema(without)
        after = m2.fingerprint_schema(with_41)
        assert after.tables == before.tables
        assert after.triggers == before.triggers
        assert after.indexes - before.indexes == 1
        assert after.digest != before.digest
        assert m2.object_names(with_41, "index") - m2.object_names(without, "index") == {
            INDEX
        }
    finally:
        without.close()
        with_41.close()


def test_0041_index_key_is_the_whole_source_identity_tuple(migrated: Path) -> None:
    """Five key members, in the declared order, unique and unconditional."""
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        listed = {
            str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
            for row in connection.execute(f"PRAGMA index_list('{EVIDENCE}')")
        }
        # unique, declared by CREATE INDEX, and not partial: the invariant holds for
        # every artifact rather than for whichever subset a WHERE clause admitted.
        assert listed[INDEX] == (1, "c", 0)

        key = [
            (row[1], row[2])
            for row in connection.execute(f"PRAGMA index_xinfo('{INDEX}')")
            if int(row[5]) == 1
        ]
        # The three stored columns by name; the two nullable members enter as
        # expressions, which SQLite reports as `cid = -2` with no column name.
        assert key == [
            (1, "workspace_id"),
            (2, "source_kind"),
            (3, "source_native_id"),
            (-2, None),
            (-2, None),
        ]

        # SQLite stores the statement without its `IF NOT EXISTS`, and nothing else.
        stored = " ".join(m2.object_sql(connection, INDEX).split())
        assert stored == " ".join(MIGRATION_STATEMENTS[0].split()).replace(
            "IF NOT EXISTS ", "", 1
        )
        # The sentinels are what make the nullable members comparable, and they are of
        # a storage class no admissible value of either column can carry.
        assert "COALESCE(source_locator, X'00')" in stored
        assert "COALESCE(source_retrieved_at_us, X'00')" in stored
    finally:
        connection.close()


# --- what the index refuses, and what it must not refuse ----------------------------


def test_0041_refuses_a_repeated_identity_whose_nullable_members_are_null() -> None:
    """`(workspace, 'direct_submission', native_id, NULL, NULL)` occurs at most once."""
    connection = unguarded()
    try:
        add(connection, "evd-first", **submission())
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            add(connection, "evd-second", **submission())
        assert m2.count(connection, EVIDENCE) == 1

        # And with only one of the two absent, in each direction.
        add(connection, "evd-located", **submission(source_locator="mcp://doc-1"))
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            add(connection, "evd-located-again", **submission(source_locator="mcp://doc-1"))
        add(connection, "evd-timed", **submission(source_retrieved_at_us=BASE_US))
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            add(connection, "evd-timed-again", **submission(source_retrieved_at_us=BASE_US))
        assert m2.count(connection, EVIDENCE) == 3
    finally:
        connection.close()


def test_0041_keeps_genuinely_different_identities_apart() -> None:
    """Every member of the tuple distinguishes on its own, and the full tuple repeats."""
    connection = unguarded()
    try:
        add(connection, "evd-base", **submission())
        distinct = {
            "evd-kind": submission(source_kind="connector.filesystem"),
            "evd-native": submission(source_native_id="doc-2"),
            "evd-locator": submission(source_locator="mcp://doc-1"),
            "evd-locator-2": submission(source_locator="mcp://doc-1-v2"),
            "evd-retrieved": submission(source_retrieved_at_us=BASE_US),
            "evd-retrieved-2": submission(source_retrieved_at_us=BASE_US + 1),
            "evd-both": submission(
                source_locator="mcp://doc-1", source_retrieved_at_us=BASE_US
            ),
            "evd-workspace": submission(workspace_id=OTHER_WORKSPACE_ID),
        }
        for evidence_id, identity in distinct.items():
            add(connection, evidence_id, **identity)
        assert m2.count(connection, EVIDENCE) == len(distinct) + 1

        # Two revisions of one connector document are different evidence and stay so;
        # a repeat of either exact revision is not.
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            add(
                connection,
                "evd-both-again",
                **submission(
                    source_locator="mcp://doc-1", source_retrieved_at_us=BASE_US
                ),
            )
        assert m2.count(connection, EVIDENCE) == len(distinct) + 1
    finally:
        connection.close()


def test_0041_refuses_the_repeat_in_a_live_guarded_workspace(migrated: Path) -> None:
    """The same refusal reaches a real fenced writer, and leaves the first row alone."""
    holder = m2.take_ownership(migrated)
    try:
        m2.seed_chain(holder)
        m2.write(holder, EVIDENCE, evidence_id="evd-submitted", **submission())
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            m2.write(holder, EVIDENCE, evidence_id="evd-submitted-again", **submission())

        rows = holder.connection.execute(
            f"SELECT evidence_id FROM {EVIDENCE} "
            "WHERE source_kind = ? ORDER BY evidence_id",
            (DIRECT_SUBMISSION,),
        ).fetchall()
        assert rows == [("evd-submitted",)]
        assert integrity_check(holder.connection) == []
        assert foreign_key_check(holder.connection) == []
    finally:
        holder.connection.close()


# --- applying it to a workspace that already collides -------------------------------


def test_0041_refuses_to_apply_over_colliding_legacy_rows(tmp_path: Path) -> None:
    """Fail closed: the workspace stays at 0040 with both artifacts untouched."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m2.bootstrap_and_migrate(path)
        holder = m2.take_ownership(path)
        try:
            m2.seed_chain(holder)
            # Legal at 0040, and exactly what 0041 exists to forbid.
            m2.write(holder, EVIDENCE, evidence_id="evd-legacy-a", **submission())
            m2.write(holder, EVIDENCE, evidence_id="evd-legacy-b", **submission())
        finally:
            holder.connection.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        with pytest.raises(sqlite3.IntegrityError, match=COLLISION):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )

        # Nothing merged, nothing rewritten, nothing tombstoned: both artifacts are
        # still there, exactly as they were.
        assert connection.execute(
            f"SELECT evidence_id FROM {EVIDENCE} WHERE source_kind = ? "
            "ORDER BY evidence_id",
            (DIRECT_SUBMISSION,),
        ).fetchall() == [("evd-legacy-a",), ("evd-legacy-b",)]

        # The head did not move, and the refusal is recorded where an operator looks.
        recorded = applied_migrations(connection)
        assert MIGRATION_VERSION not in recorded
        assert max(recorded) == PREDECESSOR_VERSION
        assert connection.execute("PRAGMA user_version").fetchone() == (
            PREDECESSOR_VERSION,
        )
        assert INDEX not in m2.object_names(connection, "index")

        attempts = connection.execute(
            "SELECT outcome, detail FROM omnivia_migration_attempts "
            "WHERE version = ? ORDER BY started_at",
            (MIGRATION_VERSION,),
        ).fetchall()
        assert [str(row[0]) for row in attempts] == ["failed"]
        assert INDEX in str(attempts[0][1])
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


# --- immutability and repeatability -------------------------------------------------


def test_0041_is_pinned_by_content_and_never_applied_twice(migrated: Path) -> None:
    """The accepted text, applied once; a second pass is a no-op and an edit is refused."""
    assert MIGRATION.checksum == MIGRATION_CHECKSUM

    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        arguments: dict[str, Any] = {
            "mode": OpenMode.EXCLUSIVE_MAINTENANCE,
            "service_instance_id": m2.SERVICE_INSTANCE,
            "fencing_generation": state.fencing_generation,
            "workspace_id": WORKSPACE_ID,
        }
        assert apply_pending_migrations(connection, **arguments) == []
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION_CHECKSUM
    finally:
        connection.close()


def test_0041_cannot_be_edited_after_it_has_been_applied(
    migrated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed 0041 is detected, not silently accepted as already done."""
    edited = Migration(
        version=MIGRATION_VERSION,
        name=MIGRATION_NAME,
        sql=MIGRATION.sql + "\n-- an edit after the fact\n",
    )
    catalogue = tuple(
        edited if m.version == MIGRATION_VERSION else m for m in load_migrations()
    )
    assert edited.checksum != MIGRATION_CHECKSUM

    import omnivia_core_runtime.storage.migrations as migrations_module

    monkeypatch.setattr(migrations_module, "load_migrations", lambda: catalogue)
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        with pytest.raises(StorageError, match="has changed since it was applied"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()
