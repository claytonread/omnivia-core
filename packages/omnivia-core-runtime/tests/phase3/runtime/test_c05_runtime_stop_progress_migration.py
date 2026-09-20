"""C05a acceptance for migration 0042's durable stop-progress records.

What 0042 is: three append-only tables and nine statement triggers hanging off the
stop request migration 0025 already records. Migration 0025 stays byte-immutable and
stays the one authority on whether a run was asked to stop; nothing here creates a
stop, settles one, closes a run or removes any history.

What this file holds to.

*It is additive and consecutive.* A workspace already populated at 0041 reaches 0042
with every prior row byte-identical, and the three new tables arrive empty.

*An obligation is evidence, not an assertion.* It may only name a settlement whose
outcome is `unknown` -- a `committed` or `not_committed` settlement is an answer, and
an answered effect is not something a stop waits on -- and only an effect of the very
run its own stop request named.

*The observation sequence is the database's.* Progress numbers are contiguous from 1
per stop request, cannot regress in time, and cannot predate the stop they observe.

*Nothing here may be revised.* UPDATE and DELETE abort on all three tables, for the
current fenced owner as much as for anyone else.
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
import test_rt203_effect_reconciliation_migration as m24
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
from omnivia_core_runtime.storage.runtime_stop import (
    CLEANUP_RECEIPT_OUTCOMES,
    MAX_STOP_OBLIGATIONS,
)

MIGRATION_VERSION = 42
PREDECESSOR_VERSION = 41
MIGRATION_NAME = "0042_runtime_stop_progress.sql"

WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US
AUDIT_REF = m18.audit_ref_for(m18.JOB_ID)

PROGRESS = "omnivia_runtime_stop_progress"
OBLIGATIONS = "omnivia_runtime_stop_obligations"
CLEANUP_RECEIPTS = "omnivia_runtime_stop_cleanup_receipts"
TABLES = (PROGRESS, OBLIGATIONS, CLEANUP_RECEIPTS)
INDEXES = {"omnivia_idx_runtime_stop_cleanup_receipts_request"}
TRIGGERS = {
    f"omnivia_guard_runtime_stop_{subject}_{statement}"
    for subject in ("progress", "obligations", "cleanup_receipts")
    for statement in ("insert", "update", "delete")
}

#: The stop this file records progress against, requested after every effect row the
#: `m23`/`m24` seeds place at `BASE_US + 1 … + 7`.
STOP_REQUEST_ID = "stop-0001"
REQUESTED_AT_US = BASE_US + 100


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


def insert(holder: m1.Owned, table: str, values: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def stop_request_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_request_id": STOP_REQUEST_ID,
        "run_id": RUN_ID,
        "requested_at_us": REQUESTED_AT_US,
        "requested_by": "core-operator",
        "reason": "operator.cancelled",
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def progress_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_progress_id": "stop-progress-0001",
        "stop_request_id": STOP_REQUEST_ID,
        "progress_number": 1,
        "observed_at_us": REQUESTED_AT_US + 1,
        "cleanup_required": 1,
        "reason": "effects.pending",
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def obligation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_progress_id": "stop-progress-0001",
        "effect_intent_id": "effect-intent-0001",
        "effect_settlement_id": "effect-settlement-0001",
    }
    values.update(overrides)
    return values


def cleanup_receipt_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "stop_cleanup_receipt_id": "stop-cleanup-0001",
        "stop_request_id": STOP_REQUEST_ID,
        "resource_kind": "provider.session",
        "outcome": "released",
        "performed_at_us": REQUESTED_AT_US + 2,
        "reason": "cancellation.cleanup",
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def seed_stopped_run_with_unknown_effect(holder: m1.Owned) -> None:
    """A run holding one `unknown` effect settlement, and a stop request against it."""
    m24.seed_unknown_source(holder)
    with guarded(holder):
        insert(holder, "omnivia_runtime_stop_requests", stop_request_row())


def seed_progress(holder: m1.Owned) -> None:
    seed_stopped_run_with_unknown_effect(holder)
    with guarded(holder):
        insert(holder, PROGRESS, progress_row())


# --- the migration itself ------------------------------------------------------------


def test_0042_is_the_unique_consecutive_successor_to_0041() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    """A fresh store migrates to 42 and pins the text it actually applied."""
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_schema_inventory_contains_only_the_expected_new_objects(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                ).fetchall()
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= names["table"]
        assert INDEXES <= names["index"]
        assert TRIGGERS <= names["trigger"]
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_0042_contains_no_dml_and_drops_nothing() -> None:
    """Additive only: no INSERT, UPDATE, DELETE, DROP or ALTER in what executes."""
    executed = MIGRATION.sql.upper()
    body = "\n".join(
        line for line in executed.splitlines() if not line.lstrip().startswith("--")
    )
    for forbidden in ("INSERT INTO", "DROP ", "ALTER "):
        assert forbidden not in body, forbidden


def test_a_populated_0041_head_reaches_0042_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    """The migrated-store case: 0041 workspace carrying real rows advances to 0042."""
    path = tmp_path / "at-0041.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            seed_stopped_run_with_unknown_effect(holder)
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


# --- the guards ----------------------------------------------------------------------


def test_stop_progress_records_insert_only_through_the_fenced_owner(
    owned: m1.Owned,
) -> None:
    seed_stopped_run_with_unknown_effect(owned)
    with pytest.raises(sqlite3.DatabaseError):
        insert(owned, PROGRESS, progress_row())
    with pytest.raises(sqlite3.DatabaseError):
        insert(owned, CLEANUP_RECEIPTS, cleanup_receipt_row())


@pytest.mark.parametrize("table", TABLES)
def test_stop_progress_records_are_append_only(owned: m1.Owned, table: str) -> None:
    seed_progress(owned)
    with guarded(owned):
        insert(owned, OBLIGATIONS, obligation_row())
        insert(owned, CLEANUP_RECEIPTS, cleanup_receipt_row())

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


def test_progress_numbers_must_be_contiguous(owned: m1.Owned) -> None:
    seed_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            PROGRESS,
            progress_row(stop_progress_id="stop-progress-0003", progress_number=3),
        )


def test_progress_cannot_predate_the_stop_it_observes(owned: m1.Owned) -> None:
    seed_stopped_run_with_unknown_effect(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot predate"):
        insert(owned, PROGRESS, progress_row(observed_at_us=REQUESTED_AT_US - 1))


def test_progress_time_must_not_regress(owned: m1.Owned) -> None:
    seed_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not regress"):
        insert(
            owned,
            PROGRESS,
            progress_row(
                stop_progress_id="stop-progress-0002",
                progress_number=2,
                observed_at_us=REQUESTED_AT_US,
            ),
        )


@pytest.mark.parametrize("outcome", ("committed", "not_committed"))
def test_an_obligation_may_not_name_an_answered_settlement(
    owned: m1.Owned, outcome: str
) -> None:
    """A stop waits on unresolved effects. An answered one is not one of them."""
    seed_progress(owned)
    receipt = "effect-receipt-0001" if outcome == "committed" else None
    with guarded(owned):
        if receipt is not None:
            insert(owned, m23.RECEIPTS, m23.receipt_row(observed_at_us=BASE_US + 6))
        insert(
            owned,
            m23.SETTLEMENTS,
            m23.settlement_row(
                effect_settlement_id="effect-settlement-0009",
                outcome=outcome,
                effect_receipt_id=receipt,
                reason="answered",
                settled_at_us=BASE_US + 7,
            ),
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="unresolved settlement"
    ):
        insert(
            owned, OBLIGATIONS, obligation_row(effect_settlement_id="effect-settlement-0009")
        )


def test_an_obligation_must_name_an_effect_of_the_stops_own_run(
    owned: m1.Owned,
) -> None:
    """A stop over one run may not report itself blocked on another run's effect.

    The obligation below is otherwise impeccable -- the settlement really is an
    `unknown` settlement of the intent it names -- and it is still refused, because
    the intent belongs to `run-0001` while the stop that observation hangs off was
    requested against `run-0002`.
    """
    seed_stopped_run_with_unknown_effect(owned)
    other_run, other_job = "run-0002", "job-run-0002"
    m18.seed_job(owned, job_id=other_job)
    with guarded(owned):
        m18.insert_run(owned, run_id=other_run, job_id=other_job)
        insert(
            owned,
            "omnivia_runtime_stop_requests",
            stop_request_row(
                stop_request_id="stop-0002",
                run_id=other_run,
                audit_ref=m18.audit_ref_for(other_job),
            ),
        )
        insert(
            owned,
            PROGRESS,
            progress_row(
                stop_progress_id="stop-progress-0002",
                stop_request_id="stop-0002",
                audit_ref=m18.audit_ref_for(other_job),
            ),
        )

    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="effect of the run its stop named"
    ):
        insert(owned, OBLIGATIONS, obligation_row(stop_progress_id="stop-progress-0002"))


def test_an_observation_may_not_carry_more_than_the_contract_can_state(
    owned: m1.Owned,
) -> None:
    """The 257th obligation is refused, so `pending_effect_count` is always sayable."""
    seed_progress(owned)
    with guarded(owned):
        for index in range(MAX_STOP_OBLIGATIONS):
            intent = f"effect-intent-b{index:04d}"
            settlement = f"effect-settlement-b{index:04d}"
            insert(
                owned,
                m23.INTENTS,
                m23.intent_row(
                    effect_intent_id=intent,
                    idempotency_key=f"effect-key-b{index:04d}",
                    declared_at_us=BASE_US + 3,
                ),
            )
            insert(
                owned,
                m23.SETTLEMENTS,
                m23.settlement_row(
                    effect_settlement_id=settlement,
                    effect_intent_id=intent,
                    outcome="unknown",
                    effect_receipt_id=None,
                    reason="provider_unreachable",
                    settled_at_us=BASE_US + 5,
                ),
            )
            insert(
                owned,
                OBLIGATIONS,
                obligation_row(
                    effect_intent_id=intent, effect_settlement_id=settlement
                ),
            )

    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="more than 256 obligations"
    ):
        insert(owned, OBLIGATIONS, obligation_row())


def test_a_cleanup_receipt_cannot_predate_the_stop_it_accounts_for(
    owned: m1.Owned,
) -> None:
    seed_stopped_run_with_unknown_effect(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot predate"):
        insert(
            owned, CLEANUP_RECEIPTS, cleanup_receipt_row(performed_at_us=REQUESTED_AT_US - 1)
        )


@pytest.mark.parametrize("outcome", CLEANUP_RECEIPT_OUTCOMES)
def test_a_cleanup_receipt_admits_the_four_per_resource_answers(
    owned: m1.Owned, outcome: str
) -> None:
    """Including `unknown`, which is where a rolled-up `uncertain` comes from."""
    seed_stopped_run_with_unknown_effect(owned)
    with guarded(owned):
        insert(owned, CLEANUP_RECEIPTS, cleanup_receipt_row(outcome=outcome))
    stored = owned.connection.execute(
        f"SELECT outcome FROM {CLEANUP_RECEIPTS}"
    ).fetchone()
    assert stored == (outcome,)


def test_a_cleanup_receipt_refuses_an_outcome_outside_that_vocabulary(
    owned: m1.Owned,
) -> None:
    seed_stopped_run_with_unknown_effect(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        insert(owned, CLEANUP_RECEIPTS, cleanup_receipt_row(outcome="probably_fine"))
