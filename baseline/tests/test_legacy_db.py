"""Tests for legacy database backup, capture, checksum, restore, and rollback."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from baseline.legacy_db import (
    FIXTURE_MEMORIES,
    LegacyDatabaseError,
    LegacyDatabaseSafetyError,
    backup_database,
    build_legacy_fixture_database,
    capture_inventory,
    default_legacy_database_path,
    open_read_only,
    redact_row,
    restore_database,
    rollback_from_backup,
    sha256_file,
    verify_legacy_fixture_inventory,
    verify_zero_data_loss,
)


@pytest.fixture()
def legacy_fixture(tmp_path) -> Path:
    return build_legacy_fixture_database(tmp_path / "legacy-memories.db")


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #


def test_the_real_user_database_is_refused() -> None:
    with pytest.raises(LegacyDatabaseSafetyError, match="may hold real"):
        capture_inventory(default_legacy_database_path())


def test_anything_inside_the_omnivia_data_directory_is_refused() -> None:
    with pytest.raises(LegacyDatabaseSafetyError, match="may hold real"):
        capture_inventory(Path.home() / ".omnivia" / "workspaces" / "any.db")


def test_a_legacy_named_database_in_home_is_refused() -> None:
    with pytest.raises(LegacyDatabaseSafetyError, match="legacy database name"):
        capture_inventory(Path.home() / "memories.db")


def test_the_read_path_cannot_write(legacy_fixture) -> None:
    connection = open_read_only(legacy_fixture)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM memories")
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Capture and checksums
# --------------------------------------------------------------------------- #


def test_capture_records_structure_without_any_user_values(legacy_fixture) -> None:
    inventory = capture_inventory(legacy_fixture)
    rendered = str(inventory.to_dict())

    assert inventory.total_rows == len(FIXTURE_MEMORIES) + 1
    for memory in FIXTURE_MEMORIES:
        assert memory["content"] not in rendered


def test_capture_is_stable_across_calls(legacy_fixture) -> None:
    assert capture_inventory(legacy_fixture) == capture_inventory(legacy_fixture)


def test_content_checksum_changes_when_a_value_changes(legacy_fixture, tmp_path) -> None:
    before = capture_inventory(legacy_fixture)

    connection = sqlite3.connect(str(legacy_fixture))
    connection.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("tampered", FIXTURE_MEMORIES[0]["id"]),
    )
    connection.commit()
    connection.close()

    after = capture_inventory(legacy_fixture)

    assert after.total_rows == before.total_rows
    assert verify_zero_data_loss(before, after) == [
        (
            "table 'memories' has the same row count but a different content checksum; "
            "row values changed"
        )
    ]


def test_generated_fixture_matches_the_frozen_inventory() -> None:
    assert verify_legacy_fixture_inventory() == []


# --------------------------------------------------------------------------- #
# Backup, restore, rollback
# --------------------------------------------------------------------------- #


def test_backup_preserves_content_and_leaves_the_source_untouched(
    legacy_fixture, tmp_path
) -> None:
    before = capture_inventory(legacy_fixture)
    source_checksum = sha256_file(legacy_fixture)

    record = backup_database(legacy_fixture, tmp_path / "backups" / "legacy.backup.db")

    assert record.verified
    assert record.checks == []
    assert sha256_file(legacy_fixture) == source_checksum
    assert verify_zero_data_loss(before, capture_inventory(tmp_path / "backups" / "legacy.backup.db")) == []


def test_backup_refuses_to_overwrite_an_existing_backup(legacy_fixture, tmp_path) -> None:
    destination = tmp_path / "legacy.backup.db"
    backup_database(legacy_fixture, destination)

    with pytest.raises(LegacyDatabaseError, match="already exists"):
        backup_database(legacy_fixture, destination)


def test_restore_reproduces_the_backup_byte_for_byte(legacy_fixture, tmp_path) -> None:
    backup = tmp_path / "legacy.backup.db"
    backup_database(legacy_fixture, backup)

    record = restore_database(backup, tmp_path / "restored.db")

    assert record.verified
    assert record.source_file_checksum == record.target_file_checksum


def test_restore_will_not_clobber_without_an_explicit_flag(legacy_fixture, tmp_path) -> None:
    backup = tmp_path / "legacy.backup.db"
    backup_database(legacy_fixture, backup)

    with pytest.raises(LegacyDatabaseError, match="pass overwrite=True"):
        restore_database(backup, legacy_fixture)


def test_rollback_restores_the_pre_migration_state(legacy_fixture, tmp_path) -> None:
    before = capture_inventory(legacy_fixture)
    backup = tmp_path / "legacy.backup.db"
    backup_database(legacy_fixture, backup)

    # Simulate a migration that damages the live database.
    connection = sqlite3.connect(str(legacy_fixture))
    connection.execute("DELETE FROM memories")
    connection.commit()
    connection.close()
    assert verify_zero_data_loss(before, capture_inventory(legacy_fixture))

    record = rollback_from_backup(backup, legacy_fixture)

    assert record.operation == "rollback"
    assert record.verified
    assert verify_zero_data_loss(before, capture_inventory(legacy_fixture)) == []


def test_zero_data_loss_reports_dropped_tables_and_rows(legacy_fixture, tmp_path) -> None:
    before = capture_inventory(legacy_fixture)

    connection = sqlite3.connect(str(legacy_fixture))
    connection.execute("DROP TABLE patterns")
    connection.execute("DELETE FROM memories WHERE id = ?", (FIXTURE_MEMORIES[0]["id"],))
    connection.commit()
    connection.close()

    problems = verify_zero_data_loss(before, capture_inventory(legacy_fixture))

    assert "table 'patterns' is missing after the operation" in problems
    assert any("row count changed: 3 -> 2" in problem for problem in problems)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redaction_summarises_content_and_shortens_paths() -> None:
    secret = "a private memory"

    row = redact_row(
        ("id", "content", "file_path", "lifecycle_state"),
        ("abc", secret, "/Users/someone/vault/note.md", "approved"),
    )

    assert row["content"] == {
        "length": len(secret),
        "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
    }
    assert row["file_path"] == "<redacted>/note.md"
    assert row["lifecycle_state"] == "approved"
    assert secret not in str(row)
    assert "someone" not in str(row)
