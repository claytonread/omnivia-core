"""V06-5 S4 acceptance for migration 0016's actor-binding correction."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import SchemaDrift, verify_fingerprint
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
)
from test_v06_5_s2_memory_migration import _apply_through

MIGRATION_VERSION = 16
PREDECESSOR_VERSION = 15
MIGRATION_NAME = "0016_governance_transition_actor_binding.sql"


def _migration() -> Any:
    found = [item for item in load_migrations() if item.version == MIGRATION_VERSION]
    assert len(found) == 1
    return found[0]


MIGRATION = _migration()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def _upgrade(path: Path) -> None:
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=m1.GENERATION_ONE,
                workspace_id=m1.WORKSPACE_ID,
            )
        assert [item.version for item in applied] == [MIGRATION_VERSION]
    finally:
        connection.close()


def test_v06_5_s4_0016_is_one_consecutive_trigger_correction() -> None:
    assert MIGRATION.name == MIGRATION_NAME
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    # The accepted prefix through 0016 is contiguous. Asserted as a prefix rather
    # than as the whole catalogue, so a legitimate migration after 0016 is not
    # turned into a failure of a slice that has no authority over it.
    versions = [item.version for item in load_migrations()]
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert "CREATE TABLE" not in MIGRATION.sql.upper()
    assert "candidate.human_proposed" in MIGRATION.sql
    assert "p.actor_id = a.assertion_actor_id" in MIGRATION.sql
    assert "p.actor_id = NEW.actor_id" in MIGRATION.sql


def test_v06_5_s4_0016_clean_upgrade_integrity_and_fingerprint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clean.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    _upgrade(path)
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION.checksum
        assert connection.execute("PRAGMA user_version").fetchone() == (
            MIGRATION_VERSION,
        )
        assert foreign_key_check(connection) == []
        assert integrity_check(connection) == []
        # The oracle is the accepted prefix through 0016, not whatever is on disk:
        # this workspace stops here on purpose.
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            expected = canonical_schema_fingerprint()
        assert fingerprint_schema(connection).matches(expected)
        assert verify_fingerprint(connection, expected).matches(expected)
    finally:
        connection.close()


@pytest.mark.parametrize("stop_after", range(1, len(MIGRATION_STATEMENTS) + 1))
def test_v06_5_s4_0016_interrupted_apply_rolls_back_and_converges(
    tmp_path: Path, stop_after: int
) -> None:
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
                workspace_id=m1.WORKSPACE_ID,
            )
    finally:
        connection.close()

    interrupted = sqlite3.connect(path)
    try:
        assert MIGRATION_VERSION not in applied_migrations(interrupted)
        trigger = interrupted.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='omnivia_guard_application_governance_transitions_consistency'"
        ).fetchone()
        assert trigger is not None
        assert "candidate.human_proposed" not in str(trigger[0])
        assert foreign_key_check(interrupted) == []
        assert integrity_check(interrupted) == []
    finally:
        interrupted.close()

    _upgrade(path)
    converged = open_database(path, OpenMode.READ_ONLY)
    try:
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            expected = canonical_schema_fingerprint()
        assert fingerprint_schema(converged).matches(expected)
    finally:
        converged.close()


def test_v06_5_s4_old_binary_refuses_0016_workspace(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite"
    _apply_through(path, MIGRATION_VERSION)
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        with (
            m1.migration_catalogue_through(PREDECESSOR_VERSION),
            pytest.raises(SchemaDrift, match="fingerprint differs"),
        ):
            verify_fingerprint(connection, canonical_schema_fingerprint())
    finally:
        connection.close()
