"""Acceptance for migration 0030's Chat recovery and queue projections.

What 0030 is: a unique consecutive successor to 0029, pinned by content
checksum, adding three durable objects the immutable 0029 foundation left it no
room for -- the terminal Generation Attempt outcome fact, durable generation
text chunks, and the reorderable queue projection over 0029's queued
submissions.

What it is not: a rewrite of 0029. Every assertion here that touches a 0029
object asserts it is *unchanged*, and the fresh-install, upgrade and
interrupted-apply cases are what prove a 0029 workspace reaches 0030 with its
records intact or not at all.

The 0029 acceptance module is imported for its seeds rather than copied: the
conversation graph, branch, queued submission, running job and running attempt
these projections hang off are exactly the ones 0029 already proves it accepts,
so a drift there fails there instead of being restated -- and differently --
here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_foundation_migration as chat29
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

MIGRATION_VERSION = 30
PREDECESSOR_VERSION = 29
MIGRATION_NAME = "0030_chat_recovery_and_queue_projections.sql"
WORKSPACE_ID = chat29.WORKSPACE_ID
BASE_US = chat29.BASE_US

OUTCOMES = "omnivia_chat_generation_attempt_outcomes"
CHUNKS = "omnivia_chat_generation_chunks"
QUEUE_ORDER = "omnivia_chat_queued_submission_order"

TABLES = (OUTCOMES, CHUNKS, QUEUE_ORDER)

INDEXES = {
    "omnivia_idx_chat_generation_attempt_outcomes_job",
    "omnivia_idx_chat_generation_chunks_order",
    "omnivia_idx_chat_queued_submission_order_conversation",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

#: The same rule 0029 holds itself to: a durable Chat object never names a
#: provider credential, transport detail or hidden prompt.
FORBIDDEN_STORAGE_NAMES = chat29.FORBIDDEN_STORAGE_NAMES

CONVERSATION_ID = chat29.CONVERSATION_ID
GENERATION_JOB_ID = chat29.GENERATION_JOB_ID
GENERATION_ATTEMPT_ID = chat29.GENERATION_ATTEMPT_ID
QUEUE_ID = chat29.QUEUE_ID
SECOND_QUEUE_ID = "queue-submission-2"


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def _apply_through(path: Path, version: int) -> None:
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(version):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)


def _upgrade_from_29(path: Path) -> None:
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


def guarded(holder: m1.Owned) -> Any:
    return chat29.guarded(holder)


def insert(holder: m1.Owned, table: str, row: dict[str, object]) -> None:
    chat29.insert(holder, table, row)


# --- rows -----------------------------------------------------------------------


def outcome_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "generation_attempt_id": GENERATION_ATTEMPT_ID,
        "outcome": "failed",
        "error_class": "provider.timeout",
        "error_detail": "the provider did not answer before the deadline",
        "schema_version": 1,
        "ended_at_us": BASE_US + 200,
        "recorded_at_us": BASE_US + 201,
    }
    values.update(overrides)
    return values


def chunk_row(ordinal: int, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "generation_attempt_id": GENERATION_ATTEMPT_ID,
        "chunk_ordinal": ordinal,
        "provider_event_id": f"provider-chunk-{ordinal}",
        "text_content": f"chunk {ordinal}",
        "schema_version": 1,
        "created_at_us": BASE_US + 120 + ordinal,
    }
    values.update(overrides)
    return values


def queue_order_row(*members: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "order_json": json.dumps(list(members or (QUEUE_ID,)), separators=(",", ":")),
        "version": 1,
        "created_at_us": BASE_US + 300,
        "updated_at_us": BASE_US + 300,
    }
    values.update(overrides)
    return values


def row_for_table(table: str) -> dict[str, object]:
    return {OUTCOMES: outcome_row(), CHUNKS: chunk_row(1), QUEUE_ORDER: queue_order_row()}[table]


def seed_running_attempt(holder: m1.Owned) -> None:
    """0029's own running-job seed: conversation, branch, queue, job, attempt."""
    chat29.seed_running_generation_job(holder)


def seed_second_queued_submission(holder: m1.Owned) -> None:
    with guarded(holder):
        insert(
            holder,
            chat29.QUEUED,
            chat29.queued_submission_row(
                queued_submission_id=SECOND_QUEUE_ID,
                queue_sequence=2,
                idempotency_key="queue-key-2",
            ),
        )


# --- identity, ledger and inventory -----------------------------------------------


def test_0030_is_the_unique_consecutive_successor_to_0029() -> None:
    versions = [migration.version for migration in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_0030_creates_exactly_its_three_successor_tables() -> None:
    """Additive: 0030 declares no table 0029 already owns, and drops nothing."""
    import re

    created = set(
        re.findall(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(omnivia_\w+)", MIGRATION.sql)
    )
    assert created == set(TABLES)
    assert "DROP " not in MIGRATION.sql.upper()
    assert "ALTER TABLE" not in MIGRATION.sql.upper()


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum


def test_a_fresh_install_carries_every_successor_object_and_every_0029_object(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        assert len(TABLES) == 3
        assert len(INDEXES) == 3
        assert len(TRIGGERS) == 9
        # 0029 is intact, not replaced.
        assert set(chat29.TABLES) <= named["table"]
        assert chat29.INDEXES <= named["index"]
        assert chat29.TRIGGERS <= named["trigger"]
        assert connection.execute("PRAGMA user_version").fetchone() == (MIGRATION_VERSION,)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_a_migrated_workspace_matches_the_canonical_schema_exactly(
    migrated: Path,
) -> None:
    """The readiness requirement `exact_schema_and_trigger_fingerprint` in one place.

    0030 carries a comment inside a `CREATE TABLE` body. The migrator applies these
    through `split_sql_statements`, which strips comments, so the live `sqlite_master`
    holds the stripped text; an oracle that replayed the same file with `executescript`
    would keep the comment and disagree on the digest while every object count matched
    -- the exact shape that refused readiness on a freshly migrated workspace.
    `canonical_schema_fingerprint()` therefore replays the artifacts the way they are
    applied, and this asserts the two agree.
    """
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        live = fingerprint_schema(connection)
    finally:
        connection.close()
    canonical = canonical_schema_fingerprint()
    assert live.digest == canonical.digest, (live, canonical)


def test_no_new_object_is_named_for_forbidden_storage_concerns(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = list(TABLES) + list(INDEXES)
        for table in TABLES:
            names.extend(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
    finally:
        connection.close()

    offending = [
        (name, forbidden)
        for name in names
        for forbidden in FORBIDDEN_STORAGE_NAMES
        if forbidden in name.lower()
    ]
    assert offending == []


# --- upgrade and interruption -----------------------------------------------------


def test_a_populated_0029_workspace_upgrades_with_its_records_intact(
    tmp_path: Path,
) -> None:
    """The upgrade path, on a workspace that already holds Chat truth.

    Records written under 0029 are counted before and after, so an upgrade that
    rebuilt a 0029 table instead of adding beside it fails here rather than in
    whatever later read first noticed the loss.
    """
    path = tmp_path / "upgrade.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)

    holder = m1.take_ownership(path)
    try:
        chat29.seed_every_table(holder)
        before = {
            table: holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in chat29.TABLES
        }
    finally:
        holder.connection.close()
    assert all(count for count in before.values())

    _upgrade_from_29(path)

    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in chat29.TABLES
        }
        assert after == before
        # The successor tables exist and are empty: an upgrade invents no rows.
        for table in TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


@pytest.mark.parametrize("stop_after", range(1, len(MIGRATION_STATEMENTS) + 1))
def test_an_interrupted_apply_rolls_back_and_converges(tmp_path: Path, stop_after: int) -> None:
    """Failing after any statement of 0030 leaves a clean 0029 workspace.

    Not "leaves something that can be repaired": the ledger must not record 30,
    none of the three tables may exist, and a later complete run must still
    reach 0030. A migration is one transaction or it is nothing.
    """
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
        assert not (set(TABLES) & m1.object_names(interrupted, "table"))
        assert set(chat29.TABLES) <= m1.object_names(interrupted, "table")
        assert foreign_key_check(interrupted) == []
        assert integrity_check(interrupted) == []
    finally:
        interrupted.close()

    _upgrade_from_29(path)
    converged = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert applied_migrations(converged)[MIGRATION_VERSION] == MIGRATION.checksum
        assert set(TABLES) <= m1.object_names(converged, "table")
    finally:
        converged.close()


# --- writer identity and no-delete discipline -------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m1.Owned, table: str) -> None:
    seed_running_attempt(owned)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        insert(owned, table, row_for_table(table))


@pytest.mark.parametrize("table", TABLES)
def test_deletes_are_refused_even_for_the_current_owner(owned: m1.Owned, table: str) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, table, row_for_table(table))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"):
        owned.connection.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table", (OUTCOMES, CHUNKS))
def test_the_append_only_facts_refuse_every_update(owned: m1.Owned, table: str) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, table, row_for_table(table))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET schema_version = 1")


# --- the terminal attempt outcome fact --------------------------------------------


def test_an_attempt_admits_exactly_one_terminal_outcome(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, OUTCOMES, outcome_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE|PRIMARY KEY"):
        insert(owned, OUTCOMES, outcome_row(outcome="cancelled", error_class="actor.cancelled"))


def test_an_outcome_is_refused_for_an_attempt_0029_already_wrote_terminal(
    owned: m1.Owned,
) -> None:
    """A pre-0030 Attempt keeps its own terminal statement rather than gaining a
    second one from the successor table."""
    chat29.seed_queued_generation_job(owned)
    with guarded(owned):
        insert(owned, chat29.PROVIDER_INVOCATIONS, chat29.provider_invocation_row())
        insert(
            owned,
            chat29.ATTEMPTS,
            chat29.generation_attempt_row(state="succeeded", ended_at_us=BASE_US + 150),
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="already carries a terminal outcome"
    ):
        insert(owned, OUTCOMES, outcome_row(outcome="succeeded", error_class=None, error_detail=None))


def test_a_terminal_outcome_binds_an_end_that_cannot_precede_the_attempt(
    owned: m1.Owned,
) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot end before it started"):
        insert(owned, OUTCOMES, outcome_row(ended_at_us=BASE_US + 1, recorded_at_us=BASE_US + 2))


def test_an_outcome_cannot_end_before_its_own_durable_chunks(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(1))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="cannot end before its own durable chunks"
    ):
        insert(
            owned,
            OUTCOMES,
            outcome_row(ended_at_us=BASE_US + 115, recorded_at_us=BASE_US + 116),
        )
    with guarded(owned):
        insert(owned, OUTCOMES, outcome_row())


def test_a_failure_classifies_and_a_success_does_not(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert(owned, OUTCOMES, outcome_row(error_class=None))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert(owned, OUTCOMES, outcome_row(outcome="succeeded"))
    with guarded(owned):
        insert(owned, OUTCOMES, outcome_row(outcome="succeeded", error_class=None, error_detail=None))


def test_an_unknown_outcome_or_unsafe_classification_is_refused(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    for row in (
        outcome_row(outcome="retryable"),
        outcome_row(error_class="Provider Timeout"),
        outcome_row(error_detail="x" * 4097),
    ):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            insert(owned, OUTCOMES, row)


def test_an_outcome_must_name_its_own_attempt_conversation_and_job(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="own attempt conversation"):
        insert(owned, OUTCOMES, outcome_row(generation_attempt_id="generation-attempt-absent"))


# --- durable generation chunks -----------------------------------------------------


def test_chunks_are_contiguous_from_one_within_an_attempt(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous from one"):
        insert(owned, CHUNKS, chunk_row(2))
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(1))
        insert(owned, CHUNKS, chunk_row(2))
        insert(owned, CHUNKS, chunk_row(3))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous from one"):
        insert(owned, CHUNKS, chunk_row(5, provider_event_id="provider-chunk-5"))


def test_chunks_dedupe_a_provider_event_and_allow_many_without_one(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(1))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(owned, CHUNKS, chunk_row(2, provider_event_id="provider-chunk-1"))
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(2, provider_event_id=None))
        insert(owned, CHUNKS, chunk_row(3, provider_event_id=None))
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {CHUNKS}").fetchone()[0] == 3


def test_chunk_text_is_bounded_and_never_empty_or_null_bearing(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    for text in ("", "x" * 65537, "a\x00b"):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            insert(owned, CHUNKS, chunk_row(1, text_content=text))
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(1, text_content="é" * 32768))


def test_a_terminated_attempt_admits_no_further_chunk(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, CHUNKS, chunk_row(1))
        insert(owned, OUTCOMES, outcome_row())
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="terminated generation attempt"
    ):
        insert(owned, CHUNKS, chunk_row(2))


def test_a_chunk_cannot_predate_its_attempt(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot predate its attempt"):
        insert(owned, CHUNKS, chunk_row(1, created_at_us=BASE_US + 1))


# --- the queue-order projection ----------------------------------------------------


def test_a_conversation_with_no_projection_row_is_not_disordered(owned: m1.Owned) -> None:
    """The 0029 fallback: creation order is still an order."""
    seed_running_attempt(owned)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {QUEUE_ORDER}").fetchone()[0] == 0
    assert owned.connection.execute(
        f"SELECT queue_sequence FROM {chat29.QUEUED} WHERE workspace_id = ?", (WORKSPACE_ID,)
    ).fetchall() == [(1,)]


def test_a_reorder_is_one_compare_and_set_over_the_whole_order(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    seed_second_queued_submission(owned)
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID, SECOND_QUEUE_ID))

    reorder = (
        f"UPDATE {QUEUE_ORDER} SET order_json = ?, version = ?, updated_at_us = ? "
        "WHERE workspace_id = ? AND conversation_id = ? AND version = ?"
    )
    swapped = json.dumps([SECOND_QUEUE_ID, QUEUE_ID], separators=(",", ":"))
    with guarded(owned):
        cursor = owned.connection.execute(
            reorder, (swapped, 2, BASE_US + 400, WORKSPACE_ID, CONVERSATION_ID, 1)
        )
        assert cursor.rowcount == 1
    assert owned.connection.execute(
        f"SELECT order_json, version FROM {QUEUE_ORDER}"
    ).fetchone() == (swapped, 2)

    # A writer holding the stale version matches no row: no partial reorder.
    with guarded(owned):
        cursor = owned.connection.execute(
            reorder,
            (
                json.dumps([QUEUE_ID, SECOND_QUEUE_ID], separators=(",", ":")),
                2,
                BASE_US + 401,
                WORKSPACE_ID,
                CONVERSATION_ID,
                1,
            ),
        )
        assert cursor.rowcount == 0
    assert owned.connection.execute(
        f"SELECT order_json, version FROM {QUEUE_ORDER}"
    ).fetchone() == (swapped, 2)


def test_a_reorder_that_skips_a_version_is_refused(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="version must advance by one"):
        owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET version = 3, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (BASE_US + 400, WORKSPACE_ID, CONVERSATION_ID),
        )


def test_a_reorder_cannot_move_updated_at_us_backwards(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    seed_second_queued_submission(owned)
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID))
    with guarded(owned):
        cursor = owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET order_json = ?, version = 2, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (
                json.dumps([SECOND_QUEUE_ID, QUEUE_ID], separators=(",", ":")),
                BASE_US + 400,
                WORKSPACE_ID,
                CONVERSATION_ID,
            ),
        )
        assert cursor.rowcount == 1
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="must not move backwards"
    ):
        owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET order_json = ?, version = 3, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (
                json.dumps([QUEUE_ID, SECOND_QUEUE_ID], separators=(",", ":")),
                BASE_US + 350,
                WORKSPACE_ID,
                CONVERSATION_ID,
            ),
        )
    assert owned.connection.execute(
        f"SELECT version, updated_at_us FROM {QUEUE_ORDER}"
    ).fetchone() == (2, BASE_US + 400)


def test_queue_order_identity_and_creation_are_immutable(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="identity and creation"):
        owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET created_at_us = ?, version = version + 1 "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (BASE_US + 1, WORKSPACE_ID, CONVERSATION_ID),
        )


def test_a_reorder_cannot_move_a_claimed_or_terminal_submission(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    seed_second_queued_submission(owned)
    with guarded(owned):
        owned.connection.execute(
            f"UPDATE {chat29.QUEUED} SET state = 'claimed', version = version + 1, "
            "claimed_by = 'worker-1', claim_epoch = 1, claim_expires_at_us = ?, "
            "updated_at_us = ? WHERE workspace_id = ? AND queued_submission_id = ?",
            (BASE_US + 900, BASE_US + 400, WORKSPACE_ID, SECOND_QUEUE_ID),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only queued submissions"):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID, SECOND_QUEUE_ID))
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only queued submissions"):
        owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET order_json = ?, version = 2, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (
                json.dumps([SECOND_QUEUE_ID, QUEUE_ID], separators=(",", ":")),
                BASE_US + 401,
                WORKSPACE_ID,
                CONVERSATION_ID,
            ),
        )


def test_queue_order_refuses_a_duplicate_member_and_a_foreign_conversation(
    owned: m1.Owned,
) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="more than once"):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID, QUEUE_ID))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only queued submissions"):
        insert(owned, QUEUE_ORDER, queue_order_row("queue-submission-absent"))


def test_queue_order_must_be_an_exact_canonical_json_array(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    for payload in ('[ "queue-submission-1" ]', '{"a":1}', "[]", "not json"):
        with guarded(owned), pytest.raises(
            sqlite3.IntegrityError, match="exact canonical JSON array"
        ):
            insert(owned, QUEUE_ORDER, queue_order_row(order_json=payload))


def test_queue_order_refuses_a_non_string_member(owned: m1.Owned) -> None:
    """`json_each` reports a JSON number's `type` as `integer`/`real`, not `text`,
    so a numeric member must be refused explicitly rather than relying on the
    string-comparison joins downstream to reject it by accident."""
    seed_running_attempt(owned)
    seed_second_queued_submission(owned)
    numeric_member = json.dumps([1], separators=(",", ":"))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only string submission identifiers"
    ):
        insert(owned, QUEUE_ORDER, queue_order_row(order_json=numeric_member))
    with guarded(owned):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only string submission identifiers"
    ):
        owned.connection.execute(
            f"UPDATE {QUEUE_ORDER} SET order_json = ?, version = 2, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ?",
            (
                json.dumps([1, QUEUE_ID], separators=(",", ":")),
                BASE_US + 400,
                WORKSPACE_ID,
                CONVERSATION_ID,
            ),
        )


def test_a_queue_order_row_is_inserted_only_at_version_one(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only at version one"):
        insert(owned, QUEUE_ORDER, queue_order_row(QUEUE_ID, version=2))
