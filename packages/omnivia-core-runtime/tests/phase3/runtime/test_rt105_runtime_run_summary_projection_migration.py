"""RT-105 acceptance for migration 0020's derived Run summary table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as repository
from omnivia_core_runtime.storage.agent_runtime import admit_run
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
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    rebuild_runtime_run_summaries,
)

MIGRATION_VERSION = 20
PREDECESSOR_VERSION = 19
MIGRATION_NAME = "0020_runtime_run_summary_projection.sql"
TABLE = "omnivia_runtime_run_summaries"
WORKSPACE_ID = m18.WORKSPACE_ID
TRIGGERS = {
    "omnivia_guard_runtime_run_summaries_insert",
    "omnivia_guard_runtime_run_summaries_update",
    "omnivia_guard_runtime_run_summaries_delete",
}


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    record = repository.admission()
    m18.seed_job(holder, job_id=record.job_id)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record,
    )
    yield holder
    holder.connection.close()


def test_migration_is_the_unique_successor_and_declares_only_derived_schema() -> None:
    migrations = [
        item for item in load_migrations() if item.version == MIGRATION_VERSION
    ]
    assert len(migrations) == 1
    migration = migrations[0]
    assert migration.name == MIGRATION_NAME
    assert migration.version == PREDECESSOR_VERSION + 1
    statements = split_sql_statements(migration.sql)
    assert sum(statement.startswith("CREATE TABLE") for statement in statements) == 1
    assert sum(statement.startswith("CREATE TRIGGER") for statement in statements) == 3
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements
    )


def test_table_and_guards_are_part_of_the_canonical_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert TABLE in canonical_schema_tables()
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = ? ORDER BY name",
            (TABLE,),
        ).fetchall()
        assert {str(row[0]) for row in rows} == TRIGGERS
        columns = {
            str(row[1]): (str(row[2]), int(row[3]))
            for row in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
        }
        assert columns["workspace_id"] == ("TEXT", 1)
        assert columns["last_runtime_event_id"] == ("TEXT", 1)
        assert columns["last_sequence"] == ("INTEGER", 1)
        assert columns["terminal_at_us"] == ("INTEGER", 0)
        assert foreign_key_check(connection) == []
        assert integrity_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    finally:
        connection.close()


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_derived_mutation_still_requires_current_fenced_authority(
    owned: m1.Owned, statement: str
) -> None:
    sql = (
        f"UPDATE {TABLE} SET run_status = run_status"
        if statement == "UPDATE"
        else f"DELETE FROM {TABLE}"
    )
    with pytest.raises(
        sqlite3.DatabaseError, match=f"not authorized|unguarded {statement}"
    ):
        owned.connection.execute(sql)

    # The supported guarded replacement path is allowed and converges.
    outcome = rebuild_runtime_run_summaries(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    )
    assert outcome.record_count == 1


def test_populated_0019_head_reaches_0020_without_backfill_or_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "at-0019.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m18.seed_run_with_step(holder)
            before = tuple(
                holder.connection.execute(
                    "SELECT run_id, job_id, logical_key FROM omnivia_runtime_runs"
                ).fetchall()
            )
        finally:
            holder.connection.close()
        head = open_database(path, OpenMode.EPHEMERAL)
        try:
            assert max(applied_migrations(head)) == PREDECESSOR_VERSION
            assert (
                head.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (TABLE,),
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
            after = tuple(
                maintenance.execute(
                    "SELECT run_id, job_id, logical_key FROM omnivia_runtime_runs"
                ).fetchall()
            )
            projected = maintenance.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[
                0
            ]
            assert foreign_key_check(maintenance) == []
            assert integrity_check(maintenance) == []
        finally:
            maintenance.close()

    assert [migration.version for migration in applied] == [MIGRATION_VERSION]
    assert after == before
    assert projected == 0
