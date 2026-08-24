"""RT-203 successor acceptance for migration 0023's canonical effect records.

What 0023 is: a unique consecutive successor to 0022, pinned by content checksum,
whose objects are exactly three tables, two indexes and nine statement triggers; a
slice that applies to a pristine workspace and to a populated 0022 head without
disturbing existing rows; and a schema whose effect facts are append-only.

The rules SQL can state are stated here: an intent belongs to an open attempt of a
running run and an issued grant, an idempotency key cannot be rebound to different
request bytes, a receipt or settlement cannot exist without and before its intent, and
a committed settlement names a receipt for that same intent. Capability membership
inside the grant and provider-reference semantics stay with the contract validators.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt202_policy_budget_snapshot_migration as m202
import test_rt203_approval_capability_grant_migration as m203
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.inventory import compare_inventories, capture_inventory
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 23
PREDECESSOR_VERSION = 22
MIGRATION_NAME = "0023_runtime_effect_transactions.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID
ATTEMPT_ID = m18.ATTEMPT_ID
BASE_US = m18.BASE_US
DIGEST = m18.DIGEST

INTENTS = "omnivia_runtime_effect_intents"
RECEIPTS = "omnivia_runtime_effect_receipts"
SETTLEMENTS = "omnivia_runtime_effect_settlements"
TABLES = (INTENTS, RECEIPTS, SETTLEMENTS)
INDEXES = {
    "omnivia_idx_runtime_effect_intents_run",
    "omnivia_idx_runtime_effect_receipts_intent",
}
TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

ACCEPTED_MIGRATION_CHECKSUMS = {
    **m203.ACCEPTED_MIGRATION_CHECKSUMS,
    m203.MIGRATION_NAME: m203.MIGRATION.checksum,
}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


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


def intent_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_intent_id": "effect-intent-0001",
        "run_id": RUN_ID,
        "run_step_id": STEP_ID,
        "attempt_id": ATTEMPT_ID,
        "capability_id": "cap.runtime.invoke",
        "capability_grant_id": m203.r203.GRANT_ID,
        "effect_kind": "provider.invoke",
        "idempotency_key": "effect-key-0001",
        "request_digest": DIGEST,
        "declared_at_us": BASE_US + 3,
    }
    values.update(overrides)
    return values


def receipt_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_receipt_id": "effect-receipt-0001",
        "run_id": RUN_ID,
        "effect_intent_id": "effect-intent-0001",
        "observed_at_us": BASE_US + 4,
        "response_digest": DIGEST,
        "external_reference_json": None,
        "external_reference_digest": None,
        "external_reference_byte_length": None,
    }
    values.update(overrides)
    return values


def settlement_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "effect_settlement_id": "effect-settlement-0001",
        "run_id": RUN_ID,
        "effect_intent_id": "effect-intent-0001",
        "outcome": "committed",
        "effect_receipt_id": "effect-receipt-0001",
        "settled_at_us": BASE_US + 5,
        "reason": "receipt_observed",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def seed_effect_parents(holder: m1.Owned) -> None:
    m203.seed_parents(holder)
    with guarded(holder):
        m18.insert_step_state(
            holder, state_sequence=1, status="running", observed_at_us=BASE_US + 1
        )
        m18.insert_event(
            holder,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 1,
            event_kind="run_started",
            run_status="running",
            run_step_id=STEP_ID,
        )
        m18.insert_attempt(holder, started_at_us=BASE_US + 2)
        _insert(holder, m203.GRANTS, m203.grant_row(granted_at_us=BASE_US + 1))


def seed_intent(holder: m1.Owned) -> None:
    seed_effect_parents(holder)
    with guarded(holder):
        _insert(holder, INTENTS, intent_row())


def seed_receipt(holder: m1.Owned) -> None:
    seed_intent(holder)
    with guarded(holder):
        _insert(holder, RECEIPTS, receipt_row())


def test_0023_is_the_unique_consecutive_successor_to_0022() -> None:
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


def test_effect_records_insert_only_through_the_fenced_owner(owned: m1.Owned) -> None:
    seed_receipt(owned)
    with guarded(owned):
        _insert(owned, SETTLEMENTS, settlement_row())

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {INTENTS}").fetchone() == (1,)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {RECEIPTS}").fetchone() == (1,)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {SETTLEMENTS}").fetchone() == (1,)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        _insert(owned, INTENTS, intent_row(effect_intent_id="effect-intent-0002"))


def test_intent_requires_running_run_open_attempt_and_issued_grant(owned: m1.Owned) -> None:
    m203.seed_parents(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="running run"):
        _insert(owned, INTENTS, intent_row())

    with guarded(owned):
        m18.insert_step_state(
            owned, state_sequence=1, status="running", observed_at_us=BASE_US + 1
        )
        m18.insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 1,
            event_kind="run_started",
            run_status="running",
            run_step_id=STEP_ID,
        )
        m18.insert_attempt(owned, started_at_us=BASE_US + 2)
        _insert(owned, m203.GRANTS, m203.grant_row(granted_at_us=BASE_US + 1))
    with guarded(owned):
        m18.insert_attempt_outcome(
            owned, status="cancelled", finished_at_us=BASE_US + 10
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="attempt is open"):
        _insert(owned, INTENTS, intent_row(effect_intent_id="effect-intent-0002"))


def test_idempotency_key_cannot_be_rebound_to_different_request_bytes(
    owned: m1.Owned,
) -> None:
    seed_intent(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="different request"):
        _insert(
            owned,
            INTENTS,
            intent_row(
                effect_intent_id="effect-intent-0002",
                idempotency_key="effect-key-0001",
                request_digest="sha256:" + "1" * 64,
            ),
        )


def test_receipt_and_settlement_are_bound_to_their_intent_and_time(
    owned: m1.Owned,
) -> None:
    seed_intent(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="predate"):
        _insert(owned, RECEIPTS, receipt_row(observed_at_us=BASE_US + 2))

    with guarded(owned):
        _insert(owned, RECEIPTS, receipt_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="same intent"):
        _insert(
            owned,
            SETTLEMENTS,
            settlement_row(effect_receipt_id="effect-receipt-missing"),
        )
    with guarded(owned):
        _insert(owned, SETTLEMENTS, settlement_row())


def test_non_committed_settlement_names_no_receipt(owned: m1.Owned) -> None:
    seed_intent(owned)
    with guarded(owned):
        _insert(
            owned,
            SETTLEMENTS,
            settlement_row(
                outcome="unknown",
                effect_receipt_id=None,
                reason="provider_unreachable",
            ),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        _insert(
            owned,
            SETTLEMENTS,
            settlement_row(
                effect_settlement_id="effect-settlement-0002",
                outcome="unknown",
                effect_receipt_id="effect-receipt-0001",
            ),
        )


def test_effect_records_are_append_only(owned: m1.Owned) -> None:
    seed_receipt(owned)
    with guarded(owned):
        _insert(owned, SETTLEMENTS, settlement_row())

    for table in TABLES:
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"DELETE FROM {table}")


def test_a_populated_0022_head_reaches_0023_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "at-0022.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m203.seed_records(holder)
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
    for table in TABLES:
        added = after.table(table)
        assert added is not None and added.row_count == 0, table
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
