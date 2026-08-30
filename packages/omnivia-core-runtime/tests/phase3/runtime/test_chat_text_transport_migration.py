"""Acceptance for migration 0034's durable Chat text transport event.

What 0034 is: a unique consecutive successor to 0033 that rebuilds
`omnivia_chat_generation_events` -- SQLite cannot widen a CHECK in place -- so the
one ordered generation lifecycle log also admits `chat.generation.text_appended`,
an event that must name an attempt but never a result message, and whose payload
carries normalized metadata only -- the chunk ordinal and the Provider event
identity. The exact text delta stays solely in
`omnivia_chat_generation_text_chunks`; transport projection joins the two on that
ordinal.

What it is not: a new table, a new guard, a relaxation, or any runtime persistence.
Everything 0029 defined for this table is carried across byte for byte, and the two
tests that pin the recreated DDL against 0029's own text are what makes that
claim checkable rather than asserted.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_foundation_migration as foundation
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    Migration,
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 34
PREDECESSOR_VERSION = 33
MIGRATION_NAME = "0034_chat_generation_text_transport_events.sql"

WORKSPACE_ID = m1.WORKSPACE_ID
EVENTS = foundation.EVENTS
TEXT_APPENDED = "chat.generation.text_appended"
EVENTS_INDEX = "omnivia_idx_chat_generation_events_order"
EVENTS_TRIGGERS = (
    "omnivia_guard_chat_generation_events_insert",
    "omnivia_guard_chat_generation_events_update",
    "omnivia_guard_chat_generation_events_delete",
)

# The only two differences 0034 is allowed to make to 0029's table, as literal text.
# 0031-0033 add tables of their own and neither alter nor reference this one, so
# 0029's text is still exactly what the rebuild must reproduce.
WIDENED_EVENT_TYPES = (
    (
        "    CHECK (event_type IN ('chat.generation.queued', 'chat.generation.started',\n"
        "                          'chat.generation.succeeded'"
    ),
    (
        "    CHECK (event_type IN ('chat.generation.queued', 'chat.generation.started',\n"
        "                          'chat.generation.text_appended',\n"
        "                          'chat.generation.succeeded'"
    ),
)
WIDENED_EVENT_SHAPE = (
    (
        "           OR (event_type = 'chat.generation.started'\n"
        "               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)\n"
    ),
    (
        "           OR (event_type = 'chat.generation.started'\n"
        "               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)\n"
        "           OR (event_type = 'chat.generation.text_appended'\n"
        "               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)\n"
    ),
)


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


def statement(sql: str, opening: str, closing: str) -> str:
    """The one statement of `sql` starting at `opening`, without its terminator."""
    start = sql.index(opening)
    end = sql.index(closing, start) + len(closing)
    return sql[start:end]


def without_if_not_exists(sql: str) -> str:
    """SQLite drops `IF NOT EXISTS` from the DDL it stores; the sources keep it."""
    return sql.replace(" IF NOT EXISTS ", " ", 1)


def stored_sql(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None, name
    return without_if_not_exists(str(row[0]))


def event_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """Every generation event, whole rows in declared column order."""
    return list(
        connection.execute(
            f"SELECT * FROM {EVENTS} ORDER BY generation_job_id, generation_event_sequence"
        )
    )


def apply_remaining_migrations(path: Path) -> list[Migration]:
    """Advance an already-bootstrapped workspace through the real migrator."""
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(maintenance)
        assert state is not None
        return apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m1.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        maintenance.close()


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def text_row(sequence: int, **overrides: object) -> dict[str, object]:
    """A well-formed `text_appended` event: an attempt, no result message.

    The payload carries only normalized metadata. The delta itself lives in
    `omnivia_chat_generation_text_chunks`, keyed by the same chunk ordinal.
    """
    values: dict[str, object] = {
        "generation_attempt_id": foundation.GENERATION_ATTEMPT_ID,
        "provider_event_id": f"provider-event-text-{sequence}",
        "payload_json": (
            f'{{"chunkOrdinal":{sequence - 3},"providerEventType":"text-delta"}}'
        ),
    }
    values.update(overrides)
    event_type = str(values.pop("event_type", TEXT_APPENDED))
    return foundation.generation_event_row(sequence, event_type, **values)  # type: ignore[arg-type]


def seed_streaming_attempt(holder: m1.Owned) -> None:
    """A running job whose attempt has queued (1) and started (2) recorded."""
    foundation.seed_running_generation_job(holder)
    with foundation.guarded(holder):
        foundation.insert(
            holder,
            EVENTS,
            foundation.generation_event_row(
                2,
                "chat.generation.started",
                generation_attempt_id=foundation.GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-started",
            ),
        )


def test_0034_is_the_unique_consecutive_successor_to_0033() -> None:
    """One 0034, no gap and no second claimant on the number.

    0031-0033 landed on the default branch while this migration was in flight, so
    the number it is allocated is what this pins: the catalogue runs 1..34 without
    a gap or a repeat, and exactly one file answers to 34.
    """
    migrations = load_migrations()
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert versions.count(MIGRATION_VERSION) == 1
    assert [m.name for m in migrations if m.version == MIGRATION_VERSION] == [
        MIGRATION_NAME
    ]
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_rebuilt_table_is_0029s_table_plus_exactly_two_widenings(
    owned: m1.Owned,
) -> None:
    original = statement(
        foundation.MIGRATION.sql,
        "CREATE TABLE IF NOT EXISTS omnivia_chat_generation_events (",
        ") WITHOUT ROWID",
    )
    expected = original.replace(
        "CREATE TABLE IF NOT EXISTS omnivia_chat_generation_events",
        'CREATE TABLE "omnivia_chat_generation_events"',
    ).replace(*WIDENED_EVENT_TYPES).replace(*WIDENED_EVENT_SHAPE)

    assert expected != original
    assert stored_sql(owned.connection, EVENTS) == expected


def test_the_index_and_the_three_guards_are_recreated_verbatim_from_0029(
    owned: m1.Owned,
) -> None:
    index = statement(
        foundation.MIGRATION.sql,
        f"CREATE INDEX IF NOT EXISTS {EVENTS_INDEX}",
        "(workspace_id, generation_job_id, generation_event_sequence)",
    )
    assert stored_sql(owned.connection, EVENTS_INDEX) == without_if_not_exists(index)

    for trigger in EVENTS_TRIGGERS:
        expected = statement(
            foundation.MIGRATION.sql, f"CREATE TRIGGER IF NOT EXISTS {trigger}", "\nEND"
        )
        assert stored_sql(owned.connection, trigger) == without_if_not_exists(expected)


def test_upgrading_a_0033_workspace_preserves_every_existing_event(
    tmp_path: Path,
) -> None:
    """A workspace migrated through 0033 -- the real predecessor -- then through 0034.

    0034 is the only migration left to apply, every event row survives the rebuild
    byte for byte, and the successor table's working name is gone from the schema.
    """
    path = tmp_path / "workspace-through-0033.sqlite"
    materialise_phase0_baseline(path)

    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
        holder = m1.take_ownership(path)
        try:
            foundation.seed_every_table(holder)
            before = event_rows(holder.connection)
            assert [row[6] for row in before] == [
                "chat.generation.queued",
                "chat.generation.started",
                "chat.generation.succeeded",
            ]
        finally:
            holder.connection.close()

    applied = apply_remaining_migrations(path)
    assert [migration.version for migration in applied] == [MIGRATION_VERSION]

    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION.checksum
        assert event_rows(connection) == before
        named = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_%'"
            )
        }
        assert {EVENTS, EVENTS_INDEX, *EVENTS_TRIGGERS} <= named
        assert not [name for name in named if name.endswith("_0034")]
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_text_appended_events_are_accepted_in_sequence(owned: m1.Owned) -> None:
    seed_streaming_attempt(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, EVENTS, text_row(3))
        foundation.insert(owned, EVENTS, text_row(4))

    rows = owned.connection.execute(
        f"SELECT generation_event_sequence, event_type, generation_attempt_id, "
        f"result_message_id, payload_json FROM {EVENTS} "
        "ORDER BY generation_event_sequence"
    ).fetchall()
    attempt = foundation.GENERATION_ATTEMPT_ID
    assert rows[2:] == [
        (3, TEXT_APPENDED, attempt, None, '{"chunkOrdinal":0,"providerEventType":"text-delta"}'),
        (4, TEXT_APPENDED, attempt, None, '{"chunkOrdinal":1,"providerEventType":"text-delta"}'),
    ]
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("no attempt", {"generation_attempt_id": None, "provider_event_id": None}),
        ("carries a result message", {"result_message_id": foundation.TRIGGER_MESSAGE_ID}),
        ("unknown event type", {"event_type": "chat.generation.text_delta"}),
    ],
)
def test_wrongly_shaped_text_events_are_rejected(
    owned: m1.Owned, case: str, overrides: dict[str, object]
) -> None:
    seed_streaming_attempt(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        foundation.insert(owned, EVENTS, text_row(3, **overrides))

    assert owned.connection.execute(
        f"SELECT COUNT(*) FROM {EVENTS} WHERE event_type = ?", (TEXT_APPENDED,)
    ).fetchone()[0] == 0


def test_text_appended_must_continue_the_contiguous_sequence(owned: m1.Owned) -> None:
    seed_streaming_attempt(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="contiguous"),
    ):
        foundation.insert(owned, EVENTS, text_row(5))


@pytest.mark.parametrize(
    "payload_json",
    ['{"chunkOrdinal": 0}', '["chunkOrdinal"]', "not json at all"],
)
def test_the_canonical_object_payload_guard_still_holds(
    owned: m1.Owned, payload_json: str
) -> None:
    seed_streaming_attempt(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="canonical JSON object"),
    ):
        foundation.insert(owned, EVENTS, text_row(3, payload_json=payload_json))


def test_text_appended_rows_remain_append_only(owned: m1.Owned) -> None:
    seed_streaming_attempt(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, EVENTS, text_row(3))

    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        owned.connection.execute(
            f"UPDATE {EVENTS} SET payload_json = '{{}}' "
            "WHERE workspace_id = ? AND generation_event_sequence = 3",
            (WORKSPACE_ID,),
        )

    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"),
    ):
        owned.connection.execute(f"DELETE FROM {EVENTS}")

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {EVENTS}").fetchone()[0] == 3


def test_unguarded_text_appended_insert_is_still_refused(owned: m1.Owned) -> None:
    seed_streaming_attempt(owned)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        foundation.insert(owned, EVENTS, text_row(3))
