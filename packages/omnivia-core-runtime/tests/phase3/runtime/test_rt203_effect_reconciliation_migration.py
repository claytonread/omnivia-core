"""RT-203 successor acceptance for migration 0024's effect reconciliation records."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt203_effect_transaction_migration as m23
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import compare_inventories, capture_inventory
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 24
PREDECESSOR_VERSION = 23
MIGRATION_NAME = "0024_runtime_effect_reconciliations.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US
DIGEST = m18.DIGEST

RECONCILIATIONS = "omnivia_runtime_effect_reconciliations"
TABLES = (RECONCILIATIONS,)
INDEXES = {"omnivia_idx_runtime_effect_reconciliations_source"}
TRIGGERS = {
    f"omnivia_guard_runtime_effect_reconciliations_{statement}"
    for statement in ("insert", "update", "delete")
}

ACCEPTED_MIGRATION_CHECKSUMS = {
    **m23.ACCEPTED_MIGRATION_CHECKSUMS,
    m23.MIGRATION_NAME: m23.MIGRATION.checksum,
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


def reconciliation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_reconciliation_id": "effect-reconcile-0001",
        "run_id": RUN_ID,
        "effect_intent_id": "effect-intent-0001",
        "source_effect_settlement_id": "effect-settlement-0001",
        "outcome": "NOT_APPLIED",
        "effect_receipt_id": None,
        "resulting_effect_settlement_id": "effect-settlement-0002",
        "reconciled_at_us": BASE_US + 6,
        "reconciled_by": "runtime-reconciler",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def seed_unknown_source(holder: m1.Owned) -> None:
    m23.seed_intent(holder)
    with guarded(holder):
        _insert(
            holder,
            m23.SETTLEMENTS,
            m23.settlement_row(
                outcome="unknown",
                effect_receipt_id=None,
                reason="provider_unreachable",
                settled_at_us=BASE_US + 5,
            ),
        )


def seed_not_applied_result(holder: m1.Owned) -> None:
    seed_unknown_source(holder)
    with guarded(holder):
        _insert(
            holder,
            m23.SETTLEMENTS,
            m23.settlement_row(
                effect_settlement_id="effect-settlement-0002",
                outcome="not_committed",
                effect_receipt_id=None,
                reason="absence_proven",
                settled_at_us=BASE_US + 6,
            ),
        )


def seed_applied_result(holder: m1.Owned) -> None:
    seed_unknown_source(holder)
    with guarded(holder):
        _insert(holder, m23.RECEIPTS, m23.receipt_row(observed_at_us=BASE_US + 6))
        _insert(
            holder,
            m23.SETTLEMENTS,
            m23.settlement_row(
                effect_settlement_id="effect-settlement-0002",
                outcome="committed",
                effect_receipt_id="effect-receipt-0001",
                settled_at_us=BASE_US + 7,
            ),
        )


def test_0024_is_the_unique_consecutive_successor_to_0023() -> None:
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


def test_schema_inventory_contains_only_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert set(TABLES) <= tables
        assert INDEXES <= indexes
        assert TRIGGERS <= triggers
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_not_applied_reconciliation_links_unknown_to_not_committed(
    owned: m1.Owned,
) -> None:
    seed_not_applied_result(owned)
    with guarded(owned):
        _insert(owned, RECONCILIATIONS, reconciliation_row())

    assert owned.connection.execute(f"SELECT outcome FROM {RECONCILIATIONS}").fetchone() == (
        "NOT_APPLIED",
    )


def test_applied_reconciliation_requires_matching_receipt_and_committed_result(
    owned: m1.Owned,
) -> None:
    seed_applied_result(owned)
    with guarded(owned):
        _insert(
            owned,
            RECONCILIATIONS,
            reconciliation_row(
                outcome="APPLIED",
                effect_receipt_id="effect-receipt-0001",
                reconciled_at_us=BASE_US + 7,
            ),
        )

    seed_unknown_source_id = "effect-settlement-0001"
    assert (
        owned.connection.execute(
            f"SELECT source_effect_settlement_id FROM {RECONCILIATIONS}"
        ).fetchone()
        == (seed_unknown_source_id,)
    )


def test_reconciliation_refuses_closed_source_or_mismatched_result(
    owned: m1.Owned,
) -> None:
    m23.seed_intent(owned)
    with guarded(owned):
        _insert(owned, m23.RECEIPTS, m23.receipt_row())
        _insert(owned, m23.SETTLEMENTS, m23.settlement_row())
        _insert(
            owned,
            m23.SETTLEMENTS,
            m23.settlement_row(
                effect_settlement_id="effect-settlement-0002",
                outcome="not_committed",
                effect_receipt_id=None,
                reason="absence_proven",
                settled_at_us=BASE_US + 6,
            ),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="unknown settlement"):
        _insert(owned, RECONCILIATIONS, reconciliation_row())


def test_reconciliation_result_must_match_outcome(owned: m1.Owned) -> None:
    seed_not_applied_result(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="outcome"):
        _insert(
            owned,
            RECONCILIATIONS,
            reconciliation_row(outcome="UNKNOWN"),
        )


def test_reconciliations_are_append_only(owned: m1.Owned) -> None:
    seed_not_applied_result(owned)
    with guarded(owned):
        _insert(owned, RECONCILIATIONS, reconciliation_row())

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(
            f"UPDATE {RECONCILIATIONS} SET workspace_id = workspace_id"
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {RECONCILIATIONS}")


def test_a_populated_0023_head_reaches_0024_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "at-0023.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m23.seed_receipt(holder)
            with m23.guarded(holder):
                _insert(holder, m23.SETTLEMENTS, m23.settlement_row())
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
    added = after.table(RECONCILIATIONS)
    assert added is not None and added.row_count == 0
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
