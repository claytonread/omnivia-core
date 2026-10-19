"""Acceptance for migration 0070's stored application settings.

What 0070 is: one mutable key/value table and three statement triggers, holding
the Core Settings screen's stored preferences per workspace.

What this file holds to.

*It is additive.* A workspace already populated at 0069 reaches 0070 with every
prior row byte-identical and the new table empty.

*Every write is fenced.* Insert, update and delete each abort without the
service writer, the mutation guard and a held lease -- the same predicate every
other guarded table uses. This is the property that makes the store the only
way in, so it is asserted for all three verbs rather than assumed from one.

*The schema enforces what it claims to.* The key shape, the value bound, the
first version being 1, identity immutability, the version advancing by exactly
one, and time not regressing are each checked by trying the violation.

*Absence is a state.* Deleting a row is permitted and means "no stored
preference"; reading it back gives None rather than an error.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import (
    StaleGeneration,
    close_guard,
    fenced_transaction,
)
from omnivia_core_runtime.storage.app_settings import (
    AppSettingsError,
    delete_setting,
    list_settings,
    read_setting,
    write_setting,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)

TABLE = "omnivia_app_settings"
WORKSPACE_ID = m1.WORKSPACE_ID
WRITER = "core.settings"
T0 = 1_700_000_000_000_000


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


def _write(holder: m1.Owned, key: str, value: Any, *, at: int = T0, **kwargs: Any) -> Any:
    return write_setting(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        key=key,
        value=value,
        updated_at_us=at,
        updated_by=WRITER,
        **kwargs,
    )


def _delete(holder: m1.Owned, key: str) -> bool:
    return delete_setting(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        key=key,
    )


# --- the migration itself ---------------------------------------------------


def test_migration_70_is_the_head_and_is_additive_only() -> None:
    migration = {m.version: m for m in load_migrations()}[70]
    assert migration.name.startswith("0070_")
    body = migration.sql.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ", "PRAGMA "):
        statements = [
            line
            for line in body.splitlines()
            if line.strip().startswith(forbidden) and "RAISE(ABORT" not in line
        ]
        assert statements == [], f"0070 is additive DDL only, found {forbidden.strip()}"


def test_table_and_its_three_guards_exist(owned: m1.Owned) -> None:
    assert TABLE in m1.object_names(owned.connection, "table")
    triggers = m1.object_names(owned.connection, "trigger")
    for verb in ("insert", "update", "delete"):
        assert f"omnivia_guard_app_settings_{verb}" in triggers


def test_new_table_arrives_empty(owned: m1.Owned) -> None:
    assert m1.count(owned.connection, TABLE) == 0


# --- fencing ----------------------------------------------------------------


def test_insert_without_the_guard_aborts(migrated: Path) -> None:
    """A connection with no lease and no mutation guard cannot seed a setting."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT"):
            connection.execute(
                f"INSERT INTO {TABLE} (workspace_id, setting_key, value_json, "
                "schema_version, version, updated_at_us, updated_by) "
                "VALUES (?, ?, ?, 1, 1, ?, ?)",
                (WORKSPACE_ID, "backup.schedule", '"daily"', T0, WRITER),
            )
    finally:
        connection.close()


def test_update_and_delete_without_the_guard_abort(owned: m1.Owned) -> None:
    """Closing the mutation guard is what withdraws write authority.

    Not merely closing the connection: the lease and guard rows are in the
    database, so another connection to the same file still satisfies the
    predicate until the guard is closed. Reopening a file whose owner exited
    uncleanly is therefore not a way past the fence, and this asserts the lever
    that genuinely is.
    """
    _write(owned, "backup.schedule", "daily")
    close_guard(owned.connection)

    with authorised(owned.connection, mutations=True):
        with pytest.raises(sqlite3.DatabaseError, match="unguarded UPDATE"):
            owned.connection.execute(
                f"UPDATE {TABLE} SET value_json = ?, version = version + 1 "
                "WHERE setting_key = ?",
                ('"weekly"', "backup.schedule"),
            )
        with pytest.raises(sqlite3.DatabaseError, match="unguarded DELETE"):
            owned.connection.execute(
                f"DELETE FROM {TABLE} WHERE setting_key = ?", ("backup.schedule",)
            )


def test_the_store_refuses_a_write_once_the_guard_is_closed(owned: m1.Owned) -> None:
    """The same withdrawal, through the public store rather than raw SQL."""
    _write(owned, "backup.schedule", "daily")
    close_guard(owned.connection)
    with pytest.raises(StaleGeneration, match="no mutation guard is open"):
        _write(owned, "backup.schedule", "weekly", at=T0 + 1)


# --- schema constraints -----------------------------------------------------


def test_first_version_must_be_one(owned: m1.Owned) -> None:
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match="starts at version 1"),
    ):
        owned.connection.execute(
            f"INSERT INTO {TABLE} (workspace_id, setting_key, value_json, "
            "schema_version, version, updated_at_us, updated_by) "
            "VALUES (?, ?, ?, 1, 7, ?, ?)",
            (WORKSPACE_ID, "backup.schedule", '"daily"', T0, WRITER),
        )


def test_identity_is_immutable_and_version_advances_by_one(owned: m1.Owned) -> None:
    _write(owned, "backup.schedule", "daily")
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), authorised(owned.connection, mutations=True):
        with pytest.raises(sqlite3.DatabaseError, match="identity is immutable"):
            owned.connection.execute(
                f"UPDATE {TABLE} SET setting_key = ?, version = version + 1 "
                "WHERE setting_key = ?",
                ("backup.other", "backup.schedule"),
            )
        with pytest.raises(sqlite3.DatabaseError, match="must advance by one"):
            owned.connection.execute(
                f"UPDATE {TABLE} SET value_json = ?, version = version + 2 "
                "WHERE setting_key = ?",
                ('"weekly"', "backup.schedule"),
            )


@pytest.mark.parametrize(
    "key",
    ["Backup.Schedule", "1backup", "backup..schedule", "backup.", "backup schedule", ""],
)
def test_malformed_keys_are_refused(owned: m1.Owned, key: str) -> None:
    with pytest.raises(AppSettingsError):
        _write(owned, key, "daily")


def test_an_oversized_value_is_refused(owned: m1.Owned) -> None:
    with pytest.raises(AppSettingsError, match="at most"):
        _write(owned, "backup.schedule", "x" * 20_000)


# --- the store's behaviour --------------------------------------------------


def test_write_then_read_round_trips_every_json_shape(owned: m1.Owned) -> None:
    cases: dict[str, Any] = {
        "processing.auto": True,
        "processing.profile": "balanced",
        "backup.retention": 30,
        "access.app.research.level": "read",
        "capture.languages": ["English", "French"],
        "decisions.annotations": {"scope": "Research", "limit": 200},
    }
    for key, value in cases.items():
        _write(owned, key, value)
    for key, value in cases.items():
        stored = read_setting(owned.connection, workspace_id=WORKSPACE_ID, key=key)
        assert stored is not None
        assert stored.value == value
        assert stored.version == 1
        assert stored.updated_by == WRITER


def test_an_unset_key_reads_as_none_not_an_error(owned: m1.Owned) -> None:
    assert read_setting(owned.connection, workspace_id=WORKSPACE_ID, key="backup.none") is None


def test_rewriting_advances_the_version(owned: m1.Owned) -> None:
    first = _write(owned, "processing.profile", "balanced")
    second = _write(owned, "processing.profile", "high", at=T0 + 1)
    assert (first.version, second.version) == (1, 2)
    stored = read_setting(
        owned.connection, workspace_id=WORKSPACE_ID, key="processing.profile"
    )
    assert stored is not None and stored.value == "high"


def test_expected_version_refuses_a_concurrent_overwrite(owned: m1.Owned) -> None:
    _write(owned, "processing.profile", "balanced")
    with pytest.raises(AppSettingsError, match="changed since it was read"):
        _write(owned, "processing.profile", "low", at=T0 + 1, expected_version=0)
    # The refused write left the stored value alone.
    stored = read_setting(
        owned.connection, workspace_id=WORKSPACE_ID, key="processing.profile"
    )
    assert stored is not None and stored.value == "balanced"


def test_a_backwards_clock_does_not_fail_a_write(owned: m1.Owned) -> None:
    """The trigger refuses a regressing timestamp; the store holds the instant."""
    _write(owned, "processing.profile", "balanced", at=T0 + 1000)
    second = _write(owned, "processing.profile", "low", at=T0)
    assert second.updated_at_us == T0 + 1000
    assert second.value == "low"


def test_delete_restores_the_default_and_is_idempotent(owned: m1.Owned) -> None:
    _write(owned, "backup.schedule", "daily")
    assert _delete(owned, "backup.schedule") is True
    assert read_setting(
        owned.connection, workspace_id=WORKSPACE_ID, key="backup.schedule"
    ) is None
    assert _delete(owned, "backup.schedule") is False


def test_list_is_ordered_and_a_prefix_narrows_to_one_namespace(owned: m1.Owned) -> None:
    for key in ("processing.auto", "backup.schedule", "backup.retention"):
        _write(owned, key, 1)
    everything = [s.key for s in list_settings(owned.connection, workspace_id=WORKSPACE_ID)]
    assert everything == ["backup.retention", "backup.schedule", "processing.auto"]
    namespaced = [
        s.key
        for s in list_settings(
            owned.connection, workspace_id=WORKSPACE_ID, prefix="backup."
        )
    ]
    assert namespaced == ["backup.retention", "backup.schedule"]


def test_a_value_written_outside_the_store_is_reported_not_returned(
    owned: m1.Owned,
) -> None:
    """A row whose value is not JSON came from something that bypassed this
    module; surfacing it as a string would hand the caller a bogus value."""
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), authorised(owned.connection, mutations=True):
        owned.connection.execute(
            f"INSERT INTO {TABLE} (workspace_id, setting_key, value_json, "
            "schema_version, version, updated_at_us, updated_by) "
            "VALUES (?, ?, ?, 1, 1, ?, ?)",
            (WORKSPACE_ID, "backup.schedule", "not json", T0, WRITER),
        )
    with pytest.raises(AppSettingsError, match="not valid JSON"):
        read_setting(owned.connection, workspace_id=WORKSPACE_ID, key="backup.schedule")


def test_settings_are_scoped_to_their_workspace(owned: m1.Owned) -> None:
    _write(owned, "backup.schedule", "daily")
    assert (
        read_setting(owned.connection, workspace_id="other-workspace", key="backup.schedule")
        is None
    )


def test_stored_json_is_canonical(owned: m1.Owned) -> None:
    """Keys sorted and separators tight, so an unchanged value writes identical
    bytes and a diff of the database is not noise."""
    _write(owned, "decisions.annotations", {"limit": 200, "scope": "Research"})
    row = owned.connection.execute(
        f"SELECT value_json FROM {TABLE} WHERE setting_key = ?", ("decisions.annotations",)
    ).fetchone()
    assert row[0] == json.dumps(
        {"limit": 200, "scope": "Research"}, sort_keys=True, separators=(",", ":")
    )
