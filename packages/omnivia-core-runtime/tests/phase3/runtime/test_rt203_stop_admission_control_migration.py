"""Acceptance for migration 0025's runtime stop and admission-control records."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt203_effect_reconciliation_migration as m24
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 25
PREDECESSOR_VERSION = 24
MIGRATION_NAME = "0025_runtime_stop_and_admission_control.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US

ADMISSIONS = "omnivia_runtime_admission_decisions"
STOP_REQUESTS = "omnivia_runtime_stop_requests"
STOP_OUTCOMES = "omnivia_runtime_stop_outcomes"
TABLES = (ADMISSIONS, STOP_REQUESTS, STOP_OUTCOMES)
TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

ACCEPTED_MIGRATION_CHECKSUMS = {
    **m24.ACCEPTED_MIGRATION_CHECKSUMS,
    m24.MIGRATION_NAME: m24.MIGRATION.checksum,
}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


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
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def admission_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "admission_decision_id": "admission-0001",
        "logical_key": m18.logical_key_for(m18.JOB_ID),
        "requested_operation": "runtime.admit",
        "decision": "admitted",
        "resulting_run_id": RUN_ID,
        "decided_at_us": BASE_US,
        "reason": "accepted",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def stop_request_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_request_id": "stop-0001",
        "run_id": RUN_ID,
        "requested_at_us": BASE_US + 1,
        "requested_by": "principal-user",
        "reason": "user_cancelled",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def stop_outcome_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_request_id": "stop-0001",
        "outcome": "accepted",
        "completed_at_us": BASE_US + 2,
        "runtime_event_sequence": 1,
        "reason": "cancelled",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def seed_run(holder: m1.Owned) -> None:
    m18.seed_admitted_run(holder)


def test_0025_is_the_unique_consecutive_successor_to_0024() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
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
    assert hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum


def test_schema_inventory_contains_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert set(TABLES) <= tables
        assert TRIGGERS <= triggers
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_admitted_decision_must_agree_with_the_resulting_run(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        _insert(owned, ADMISSIONS, admission_row())

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="agree"):
        _insert(
            owned,
            ADMISSIONS,
            admission_row(
                admission_decision_id="admission-0002",
                logical_key="different-key",
            ),
        )


def test_rejected_admission_names_no_run(owned: m1.Owned) -> None:
    m18.seed_job(owned)
    with guarded(owned):
        _insert(
            owned,
            ADMISSIONS,
            admission_row(
                decision="rejected",
                resulting_run_id=None,
                reason="policy_denied",
            ),
        )

    assert owned.connection.execute(f"SELECT decision FROM {ADMISSIONS}").fetchone() == (
        "rejected",
    )


def test_accepted_stop_outcome_names_the_cancelled_runtime_event(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        _insert(owned, STOP_REQUESTS, stop_request_row())
        m18.insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 2,
            event_kind="run_cancelled",
            run_status="cancelled",
            run_step_id=None,
        )
        _insert(owned, STOP_OUTCOMES, stop_outcome_row())

    assert owned.connection.execute(f"SELECT outcome FROM {STOP_OUTCOMES}").fetchone() == (
        "accepted",
    )


def test_stop_outcome_refuses_missing_cancelled_event(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        _insert(owned, STOP_REQUESTS, stop_request_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cancelled event"):
        _insert(owned, STOP_OUTCOMES, stop_outcome_row())


def test_ignored_stop_requires_already_terminal_run(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        _insert(owned, STOP_REQUESTS, stop_request_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="already terminal"):
        _insert(
            owned,
            STOP_OUTCOMES,
            stop_outcome_row(
                outcome="ignored_already_terminal",
                runtime_event_sequence=None,
                reason="already_terminal",
            ),
        )


def test_control_records_are_append_only(owned: m1.Owned) -> None:
    seed_run(owned)
    with guarded(owned):
        _insert(owned, ADMISSIONS, admission_row())
        _insert(owned, STOP_REQUESTS, stop_request_row())
        m18.insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 2,
            event_kind="run_cancelled",
            run_status="cancelled",
            run_step_id=None,
        )
        _insert(owned, STOP_OUTCOMES, stop_outcome_row())

    for table in TABLES:
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"DELETE FROM {table}")


def test_a_populated_0024_head_reaches_0025_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "at-0024.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m24.seed_not_applied_result(holder)
            with m24.guarded(holder):
                _insert(holder, m24.RECONCILIATIONS, m24.reconciliation_row())
            before = capture_inventory(holder.connection)
        finally:
            holder.connection.close()

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
    ledger = {"omnivia_migration_attempts", "omnivia_schema_migrations"}
    for entry in before.tables:
        if entry.name in ledger:
            continue
        assert after.table(entry.name) == entry, entry.name
    assert set(after.table_names) - set(before.table_names) == set(TABLES)
    differences = compare_inventories(before, after)
    assert differences and all(
        any(name in difference for name in ledger) for difference in differences
    ), differences


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
            for m in load_migrations()
        )
        migrations_module.load_migrations = lambda: edited
        with pytest.raises(StorageError, match="has changed"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        migrations_module.load_migrations = original
        connection.close()
