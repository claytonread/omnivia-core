"""C21-A acceptance for migration 0043's durable trigger telemetry records.

What 0043 is: four append-only tables and twelve statement triggers recording trigger
declarations, their subscription lifecycle, the stimuli declared triggers received, and
the signals delivered to `external_signal` waits. It starts no work and adds no
scheduler; `wait_timer` is deliberately absent.

What this file holds to.

*It is additive and exactly pinned.* A workspace populated at 0042 reaches 0043 with
every prior row byte-identical and the new tables empty, and the text of 0043 is pinned
by hash here and in `contracts/migrations/v1/allocations.json`.

*Writes are fenced.* Nothing is inserted outside the current fenced service writer, and
UPDATE and DELETE abort on every table for the current owner too.

*Sequences and time belong to the database.* Declarations, subscription events and
observations are contiguous from 1 per trigger (per wait for signals) and never regress
in time.

*Equivalence is exact.* One idempotency key is accepted once, and a duplicate must repeat
the accepted observation unchanged.

*Delivery is not processing.* An observation carries no processing column; it may link a
job or run only when accepted, and only a run of the trigger's own Workflow.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_workflow_runs_migration as m27
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

MIGRATION_VERSION = 43
PREDECESSOR_VERSION = 42
MIGRATION_NAME = "0043_runtime_trigger_telemetry.sql"

#: The exact text of 0043, as the runtime and `scripts/check-migration-allocations.py`
#: both hash it. Editing the migration moves this and `allocations.json` together or
#: fails here. The introducing commit cannot be known until the change is committed and
#: is pinned afterwards in `contracts/migrations/v1/allocations.json` and
#: `tests/test_migration_allocations.py`.
PINNED_SHA256 = "b461b50fa2e3e31437e2f483420550096c35c6c71fcd71d2aeca1ad56a485193"

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US + 2_000

DECLARATIONS = "omnivia_runtime_trigger_declarations"
SUBSCRIPTIONS = "omnivia_runtime_trigger_subscription_events"
OBSERVATIONS = "omnivia_runtime_trigger_observations"
WAIT_SIGNALS = "omnivia_runtime_wait_signal_observations"
TABLES = (DECLARATIONS, SUBSCRIPTIONS, OBSERVATIONS, WAIT_SIGNALS)
INDEXES = {
    "omnivia_idx_runtime_trigger_declarations_workflow",
    "omnivia_idx_runtime_trigger_observations_accepted_key",
    "omnivia_idx_runtime_trigger_observations_job",
    "omnivia_idx_runtime_trigger_observations_run",
    "omnivia_idx_runtime_wait_signal_observations_accepted",
}
TRIGGERS = {
    f"omnivia_guard_runtime_{subject}_{statement}"
    for subject in (
        "trigger_declarations",
        "trigger_subscription_events",
        "trigger_observations",
        "wait_signal_observations",
    )
    for statement in ("insert", "update", "delete")
}

PROJECT_ID = "project-alpha"
TRIGGER_ID = "trigger-0001"
WAIT_ID = m18.WAIT_ID
AUDIT_REF = "audit-plan"
DIGEST_CONTRACT = "sha256:" + "1" * 64
DIGEST_CONFIG = "sha256:" + "2" * 64
DIGEST_ENVELOPE = "sha256:" + "3" * 64
EVENT_TYPE = "com.example.order.created"


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


def declaration_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "trigger_declaration_id": "decl-0001",
        "trigger_id": TRIGGER_ID,
        "declaration_sequence": 1,
        "trigger_kind": "webhook",
        "project_id": PROJECT_ID,
        "workflow_id": m27.WORKFLOW_ID,
        "workflow_version": m27.WORKFLOW_VERSION,
        "plan_hash": m27.PLAN_HASH,
        "event_type": EVENT_TYPE,
        "event_contract_digest": DIGEST_CONTRACT,
        "configuration_digest": DIGEST_CONFIG,
        "declared_at_us": BASE_US + 1,
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def subscription_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "subscription_event_id": "sub-0001",
        "trigger_id": TRIGGER_ID,
        "declaration_sequence": 1,
        "subscription_sequence": 1,
        "subscription_state": "active",
        "reason": "subscription.enabled",
        "observed_at_us": BASE_US + 2,
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def observation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "trigger_observation_id": "obs-0001",
        "trigger_id": TRIGGER_ID,
        "declaration_sequence": 1,
        "observation_sequence": 1,
        "event_id": "event-0001",
        "idempotency_key": "key-0001",
        "event_type": EVENT_TYPE,
        "envelope_digest": DIGEST_ENVELOPE,
        "occurred_at_us": BASE_US + 3,
        "observed_at_us": BASE_US + 4,
        "delivery_status": "accepted",
        "delivery_reason": None,
        "duplicate_of_observation_id": None,
        "job_id": None,
        "run_id": None,
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def wait_signal_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "wait_signal_observation_id": "wsig-0001",
        "wait_id": WAIT_ID,
        "observation_sequence": 1,
        "event_id": "signal-0001",
        "envelope_digest": DIGEST_ENVELOPE,
        "occurred_at_us": None,
        "observed_at_us": m18.BASE_US + 10,
        "delivery_status": "accepted",
        "delivery_reason": None,
        "duplicate_of_observation_id": None,
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return values


def seed_plan(holder: m1.Owned) -> None:
    """The sealed Workflow plan every declaration binds to, and its audit event."""
    m27.seed_plan(holder)


def seed_declaration(holder: m1.Owned) -> None:
    seed_plan(holder)
    with guarded(holder):
        insert(holder, DECLARATIONS, declaration_row())


def seed_active(holder: m1.Owned) -> None:
    seed_declaration(holder)
    with guarded(holder):
        insert(holder, SUBSCRIPTIONS, subscription_row())


def seed_accepted(holder: m1.Owned) -> None:
    seed_active(holder)
    with guarded(holder):
        insert(holder, OBSERVATIONS, observation_row())


def seed_signal_wait(holder: m1.Owned, **wait: object) -> None:
    """An admitted run and an `external_signal` wait on it."""
    m18.seed_admitted_run(holder)
    with guarded(holder):
        m18.insert_step(holder)
        m18.insert_step_state(holder)
        m18.insert_wait(holder, **{"kind": "external_signal", **wait})


# --- the migration itself ------------------------------------------------------------


def test_0043_is_the_unique_consecutive_successor_to_0042() -> None:
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
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_the_migration_text_is_pinned_and_matches_the_allocation_authority() -> None:
    assert MIGRATION.checksum == PINNED_SHA256
    authority = json.loads(
        (
            Path(__file__).resolve().parents[5]
            / "contracts"
            / "migrations"
            / "v1"
            / "allocations.json"
        ).read_text(encoding="utf-8")
    )
    entry = next(
        e for e in authority["allocations"] if e["number"] == MIGRATION_VERSION
    )
    assert entry["filename"] == MIGRATION_NAME
    assert entry["owner"] == "Workflow Runtime"
    assert entry["state"] == "candidate"
    assert entry["sha256"] == PINNED_SHA256
    assert entry["accepted_commit"] is None


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


def test_0043_contains_no_dml_drops_nothing_and_has_no_timer_support() -> None:
    body = "\n".join(
        line
        for line in MIGRATION.sql.upper().splitlines()
        if not line.lstrip().startswith("--")
    )
    for forbidden in ("INSERT INTO", "DROP ", "ALTER ", "WAIT_TIMER"):
        assert forbidden not in body, forbidden
    assert "wait_timer" not in MIGRATION.sql.replace("`wait_timer`", "")


def test_a_populated_0042_head_reaches_0043_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "at-0042.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m18.seed_admitted_run(holder)
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
        if entry.name not in ledger:
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
    original = migrations_module.load_migrations
    try:
        state = read_workspace_state(connection)
        assert state is not None
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


# --- fencing and append-only ---------------------------------------------------------


def test_every_table_inserts_only_through_the_fenced_owner(owned: m1.Owned) -> None:
    seed_plan(owned)
    seed_signal_wait(owned)
    with pytest.raises(sqlite3.DatabaseError):
        insert(owned, DECLARATIONS, declaration_row())
    with guarded(owned):
        insert(owned, DECLARATIONS, declaration_row())
        insert(owned, SUBSCRIPTIONS, subscription_row())
        insert(owned, OBSERVATIONS, observation_row())
    for table, row in (
        (
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-0002",
                subscription_sequence=2,
                subscription_state="paused",
            ),
        ),
        (
            OBSERVATIONS,
            observation_row(
                trigger_observation_id="obs-0002",
                observation_sequence=2,
                idempotency_key="key-0002",
            ),
        ),
        (WAIT_SIGNALS, wait_signal_row()),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            insert(owned, table, row)


def test_a_row_for_another_workspace_is_refused_even_under_the_fence(
    owned: m1.Owned,
) -> None:
    seed_plan(owned)
    with guarded(owned), pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT"):
        insert(owned, DECLARATIONS, declaration_row(workspace_id=m1.OTHER_WORKSPACE_ID))


@pytest.mark.parametrize("table", TABLES)
def test_trigger_telemetry_records_are_append_only(owned: m1.Owned, table: str) -> None:
    seed_accepted(owned)
    seed_signal_wait(owned)
    with guarded(owned):
        insert(owned, WAIT_SIGNALS, wait_signal_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


# --- declarations --------------------------------------------------------------------


def test_declaration_sequence_must_be_contiguous(owned: m1.Owned) -> None:
    seed_declaration(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                trigger_declaration_id="decl-0003",
                declaration_sequence=3,
                event_type="com.example.order.updated",
            ),
        )


def test_a_declaration_must_bind_a_sealed_workflow_plan(owned: m1.Owned) -> None:
    seed_plan(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(owned, DECLARATIONS, declaration_row(workflow_version="9.9.9"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(owned, DECLARATIONS, declaration_row(plan_hash="sha256:" + "9" * 64))


@pytest.mark.parametrize(
    "override",
    (
        {"trigger_kind": "manual"},
        {"project_id": "project-beta"},
        {"workflow_id": "other-workflow"},
    ),
)
def test_a_trigger_keeps_its_kind_project_and_workflow(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    seed_declaration(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="keeps its kind"):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                trigger_declaration_id="decl-0002",
                declaration_sequence=2,
                event_type="com.example.order.updated",
                **override,
            ),
        )


def test_a_successor_declaration_must_change_something(owned: m1.Owned) -> None:
    seed_declaration(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must change"):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(trigger_declaration_id="decl-0002", declaration_sequence=2),
        )
    with guarded(owned):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                trigger_declaration_id="decl-0002",
                declaration_sequence=2,
                configuration_digest="sha256:" + "4" * 64,
            ),
        )


def test_declaration_time_must_not_regress(owned: m1.Owned) -> None:
    seed_declaration(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="must not regress"),
    ):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                trigger_declaration_id="decl-0002",
                declaration_sequence=2,
                configuration_digest="sha256:" + "4" * 64,
                declared_at_us=BASE_US,
            ),
        )


@pytest.mark.parametrize(
    "override",
    (
        {"trigger_kind": "wait_timer"},
        {"trigger_kind": "wait_signal"},
        {"trigger_id": "bad id"},
        {"trigger_id": ""},
        {"project_id": "../escape"},
        {"workflow_id": "Upper"},
        {"event_contract_digest": "sha256:abc"},
        {"configuration_digest": "md5:" + "1" * 64},
        {"event_type": ""},
        {"declared_at_us": 0},
        {"declaration_sequence": 0},
    ),
)
def test_a_malformed_declaration_is_refused(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    seed_plan(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned, DECLARATIONS, declaration_row(**override))


# --- subscription lifecycle ----------------------------------------------------------


def test_subscription_sequence_must_be_contiguous(owned: m1.Owned) -> None:
    seed_active(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-0003",
                subscription_sequence=3,
                subscription_state="paused",
            ),
        )


@pytest.mark.parametrize("state", ("unavailable", "disabled"))
def test_a_subscription_cannot_start_unavailable_or_disabled(
    owned: m1.Owned, state: str
) -> None:
    seed_declaration(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="starts active or paused"),
    ):
        insert(owned, SUBSCRIPTIONS, subscription_row(subscription_state=state))


@pytest.mark.parametrize(
    ("path", "illegal"),
    (
        (("active",), "active"),
        (("paused",), "unavailable"),
        (("active", "disabled"), "active"),
        (("active", "disabled"), "paused"),
        (("active", "paused"), "paused"),
    ),
)
def test_invalid_subscription_transitions_are_refused(
    owned: m1.Owned, path: tuple[str, ...], illegal: str
) -> None:
    seed_declaration(owned)
    with guarded(owned):
        for index, state in enumerate(path, start=1):
            insert(
                owned,
                SUBSCRIPTIONS,
                subscription_row(
                    subscription_event_id=f"sub-{index:04d}",
                    subscription_sequence=index,
                    subscription_state=state,
                    observed_at_us=BASE_US + 1 + index,
                ),
            )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="invalid subscription"),
    ):
        insert(
            owned,
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-9999",
                subscription_sequence=len(path) + 1,
                subscription_state=illegal,
                observed_at_us=BASE_US + 50,
            ),
        )


def test_a_subscription_event_names_the_latest_declaration(owned: m1.Owned) -> None:
    seed_active(owned)
    with guarded(owned):
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                trigger_declaration_id="decl-0002",
                declaration_sequence=2,
                configuration_digest="sha256:" + "4" * 64,
                declared_at_us=BASE_US + 3,
            ),
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="latest declaration"),
    ):
        insert(
            owned,
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-0002",
                subscription_sequence=2,
                subscription_state="paused",
                declaration_sequence=1,
                observed_at_us=BASE_US + 5,
            ),
        )


def test_subscription_time_must_not_regress_or_predate_its_declaration(
    owned: m1.Owned,
) -> None:
    seed_declaration(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot predate"):
        insert(owned, SUBSCRIPTIONS, subscription_row(observed_at_us=BASE_US))
    with guarded(owned):
        insert(owned, SUBSCRIPTIONS, subscription_row())
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="must not regress"),
    ):
        insert(
            owned,
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-0002",
                subscription_sequence=2,
                subscription_state="paused",
                observed_at_us=BASE_US + 1,
            ),
        )


@pytest.mark.parametrize(
    "override",
    ({"subscription_state": "deleted"}, {"reason": "Not.Dotted"}, {"reason": ""}),
)
def test_a_malformed_subscription_event_is_refused(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    seed_declaration(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned, SUBSCRIPTIONS, subscription_row(**override))


# --- observations --------------------------------------------------------------------


def test_observation_sequence_must_be_contiguous(owned: m1.Owned) -> None:
    seed_accepted(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                trigger_observation_id="obs-0003",
                observation_sequence=3,
                idempotency_key="key-0003",
            ),
        )


def test_observation_time_is_monotonic_and_follows_its_declaration(
    owned: m1.Owned,
) -> None:
    seed_active(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot predate"):
        insert(owned, OBSERVATIONS, observation_row(observed_at_us=BASE_US))
    with guarded(owned):
        insert(owned, OBSERVATIONS, observation_row())
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="must not regress"),
    ):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                trigger_observation_id="obs-0002",
                observation_sequence=2,
                idempotency_key="key-0002",
                observed_at_us=BASE_US + 3,
            ),
        )


def test_an_observation_names_the_latest_declaration(owned: m1.Owned) -> None:
    seed_active(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="latest declaration"),
    ):
        insert(owned, OBSERVATIONS, observation_row(declaration_sequence=2))


def test_an_accepted_observation_needs_an_active_subscription(owned: m1.Owned) -> None:
    seed_declaration(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="active subscription"),
    ):
        insert(owned, OBSERVATIONS, observation_row())
    with guarded(owned):
        insert(owned, SUBSCRIPTIONS, subscription_row())
        insert(
            owned,
            SUBSCRIPTIONS,
            subscription_row(
                subscription_event_id="sub-0002",
                subscription_sequence=2,
                subscription_state="paused",
                observed_at_us=BASE_US + 3,
            ),
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="active subscription"),
    ):
        insert(owned, OBSERVATIONS, observation_row())
    with guarded(owned):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                delivery_status="dead_lettered", delivery_reason="inactive_trigger"
            ),
        )


def test_an_accepted_observation_matches_the_declared_event_type(
    owned: m1.Owned,
) -> None:
    seed_active(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="declared event type"),
    ):
        insert(owned, OBSERVATIONS, observation_row(event_type="com.example.other"))
    with guarded(owned):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                event_type="com.example.other",
                delivery_status="dead_lettered",
                delivery_reason="event_type_mismatch",
            ),
        )


def test_one_idempotency_key_is_accepted_once(owned: m1.Owned) -> None:
    seed_accepted(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                trigger_observation_id="obs-0002",
                observation_sequence=2,
                observed_at_us=BASE_US + 5,
            ),
        )


def test_a_duplicate_must_repeat_the_accepted_observation_unchanged(
    owned: m1.Owned,
) -> None:
    seed_accepted(owned)
    duplicate = {
        "trigger_observation_id": "obs-0002",
        "observation_sequence": 2,
        "delivery_status": "duplicate",
        "duplicate_of_observation_id": "obs-0001",
        "observed_at_us": BASE_US + 5,
    }
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="repeat an accepted"),
    ):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(**duplicate, envelope_digest="sha256:" + "8" * 64),
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="repeat an accepted"),
    ):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(**duplicate, idempotency_key="key-other"),
        )
    with guarded(owned):
        insert(owned, OBSERVATIONS, observation_row(**duplicate))


@pytest.mark.parametrize(
    "override",
    (
        {"delivery_status": "accepted", "delivery_reason": "inactive_trigger"},
        {"delivery_status": "dead_lettered", "delivery_reason": None},
        {"delivery_status": "dead_lettered", "delivery_reason": "delivery_unconfirmed"},
        {"delivery_status": "uncertain", "delivery_reason": "inactive_trigger"},
        {"delivery_status": "uncertain", "delivery_reason": None},
        {"delivery_status": "processed", "delivery_reason": None},
        {"delivery_status": "duplicate", "duplicate_of_observation_id": None},
        {"duplicate_of_observation_id": "obs-0001"},
        {
            "delivery_status": "dead_lettered",
            "delivery_reason": "inactive_trigger",
            "job_id": m18.JOB_ID,
        },
        {"event_id": "bad id"},
        {"idempotency_key": ""},
        {"envelope_digest": "sha256:" + "G" * 64},
        {"occurred_at_us": 0},
    ),
)
def test_delivery_status_and_reason_vocabulary_is_strict(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    seed_active(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned, OBSERVATIONS, observation_row(**override))


def test_the_source_time_may_be_unknown_but_delivery_may_be_uncertain(
    owned: m1.Owned,
) -> None:
    seed_active(owned)
    with guarded(owned):
        insert(
            owned,
            OBSERVATIONS,
            observation_row(
                occurred_at_us=None,
                delivery_status="uncertain",
                delivery_reason="recovery_interrupted",
            ),
        )
    columns = {
        row[1] for row in owned.connection.execute(f"PRAGMA table_info({OBSERVATIONS})")
    }
    assert not columns & {"processing_status", "status", "failure", "error_json"}


def test_an_observation_links_a_job_and_run_at_most_once(owned: m1.Owned) -> None:
    seed_run_linked_workflow(owned)
    with guarded(owned):
        insert(owned, SUBSCRIPTIONS, subscription_row())
        insert(
            owned, OBSERVATIONS, observation_row(job_id=m18.JOB_ID, run_id=m18.RUN_ID)
        )
    for link in ({"job_id": m18.JOB_ID}, {"run_id": m18.RUN_ID}):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert(
                owned,
                OBSERVATIONS,
                observation_row(
                    trigger_observation_id="obs-0002",
                    observation_sequence=2,
                    idempotency_key="key-0002",
                    observed_at_us=BASE_US + 5,
                    **link,
                ),
            )


def seed_run_linked_workflow(
    holder: m1.Owned, *, workflow_id: str = m27.WORKFLOW_ID
) -> None:
    """A declaration plus a workflow run (and its job) of `workflow_id`."""
    seed_declaration(holder)
    m27.seed_runtime_run(holder, workflow_id=workflow_id)
    with guarded(holder):
        m27.insert(holder, m27.RUNS, m27.workflow_run_row(workflow_id=workflow_id))


def test_a_linked_run_must_belong_to_the_trigger_workflow(owned: m1.Owned) -> None:
    """A trigger bound to one Workflow cannot claim a run of another."""
    seed_plan(owned)
    with guarded(owned):
        m27.insert(
            owned,
            m27.PLANS,
            m27.plan_row(
                workflow_id="other-workflow",
                definition_hash="sha256:" + "5" * 64,
                plan_hash="sha256:" + "6" * 64,
            ),
        )
        insert(
            owned,
            DECLARATIONS,
            declaration_row(
                workflow_id="other-workflow", plan_hash="sha256:" + "6" * 64
            ),
        )
        insert(owned, SUBSCRIPTIONS, subscription_row())
    m27.seed_runtime_run(owned)
    with guarded(owned):
        m27.insert(owned, m27.RUNS, m27.workflow_run_row())
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="run of the trigger workflow"),
    ):
        insert(owned, OBSERVATIONS, observation_row(run_id=m18.RUN_ID))


def test_a_linked_run_must_belong_to_the_linked_job(owned: m1.Owned) -> None:
    seed_run_linked_workflow(owned)
    m18.seed_job(owned, job_id="job-other")
    with guarded(owned):
        insert(owned, SUBSCRIPTIONS, subscription_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="observed job"):
        insert(
            owned, OBSERVATIONS, observation_row(job_id="job-other", run_id=m18.RUN_ID)
        )


def test_a_link_must_name_a_recorded_job_and_run(owned: m1.Owned) -> None:
    seed_active(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(owned, OBSERVATIONS, observation_row(job_id="job-missing"))
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY|trigger workflow"),
    ):
        insert(owned, OBSERVATIONS, observation_row(run_id="run-missing"))


# --- wait signals --------------------------------------------------------------------


@pytest.mark.parametrize("kind", ("timer", "approval"))
def test_only_an_external_signal_wait_takes_signal_observations(
    owned: m1.Owned, kind: str
) -> None:
    extra = {"expires_at_us": m18.BASE_US + 100} if kind == "timer" else {}
    seed_signal_wait(owned, kind=kind, **extra)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="external-signal wait"),
    ):
        insert(owned, WAIT_SIGNALS, wait_signal_row())


def test_wait_signal_sequence_time_and_wait_creation_are_enforced(
    owned: m1.Owned,
) -> None:
    seed_signal_wait(owned)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="predate its wait"),
    ):
        insert(owned, WAIT_SIGNALS, wait_signal_row(observed_at_us=m18.BASE_US - 1))
    with guarded(owned):
        insert(owned, WAIT_SIGNALS, wait_signal_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            WAIT_SIGNALS,
            wait_signal_row(
                wait_signal_observation_id="wsig-0003",
                observation_sequence=3,
                delivery_status="dead_lettered",
                delivery_reason="wait_already_resolved",
            ),
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="must not regress"),
    ):
        insert(
            owned,
            WAIT_SIGNALS,
            wait_signal_row(
                wait_signal_observation_id="wsig-0002",
                observation_sequence=2,
                delivery_status="dead_lettered",
                delivery_reason="wait_already_resolved",
                observed_at_us=m18.BASE_US + 9,
            ),
        )


def test_a_wait_accepts_one_signal_and_a_duplicate_must_repeat_it(
    owned: m1.Owned,
) -> None:
    seed_signal_wait(owned)
    with guarded(owned):
        insert(owned, WAIT_SIGNALS, wait_signal_row())
    second = {
        "wait_signal_observation_id": "wsig-0002",
        "observation_sequence": 2,
        "observed_at_us": m18.BASE_US + 11,
    }
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(owned, WAIT_SIGNALS, wait_signal_row(**second, event_id="signal-0002"))
    duplicate = {
        **second,
        "delivery_status": "duplicate",
        "duplicate_of_observation_id": "wsig-0001",
    }
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="repeat an accepted"),
    ):
        insert(
            owned, WAIT_SIGNALS, wait_signal_row(**duplicate, event_id="signal-0002")
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="repeat an accepted"),
    ):
        insert(
            owned,
            WAIT_SIGNALS,
            wait_signal_row(**duplicate, envelope_digest="sha256:" + "7" * 64),
        )
    with guarded(owned):
        insert(owned, WAIT_SIGNALS, wait_signal_row(**duplicate))


def test_an_accepted_signal_respects_the_deadline_and_the_wait_resolution(
    owned: m1.Owned,
) -> None:
    seed_signal_wait(owned, expires_at_us=m18.BASE_US + 20)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="before the wait deadline"),
    ):
        insert(owned, WAIT_SIGNALS, wait_signal_row(observed_at_us=m18.BASE_US + 21))
    with guarded(owned):
        insert(
            owned,
            WAIT_SIGNALS,
            wait_signal_row(
                observed_at_us=m18.BASE_US + 21,
                delivery_status="dead_lettered",
                delivery_reason="deadline_passed",
            ),
        )


def test_an_accepted_signal_cannot_follow_an_expired_wait(owned: m1.Owned) -> None:
    seed_signal_wait(owned, expires_at_us=m18.BASE_US + 20)
    with guarded(owned):
        m18.insert_wait_resolution(
            owned,
            status="expired",
            resolved_at_us=m18.BASE_US + 30,
            resolution_reason="deadline.expired",
        )
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="expired or cancelled"),
    ):
        insert(owned, WAIT_SIGNALS, wait_signal_row())
