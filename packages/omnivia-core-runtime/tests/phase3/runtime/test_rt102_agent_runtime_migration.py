"""RT-102 acceptance for migration 0018's canonical Agent Runtime records.

What 0018 is: a unique consecutive successor to 0017, pinned by content checksum,
whose objects are exactly eight tables, four named indexes and twenty-four statement
triggers; a slice that applies to a pristine workspace, to an exactly-adopted Phase 0
workspace and to a *populated* workspace already at the 0017 head, without disturbing
one existing row; and a schema whose immutability is enforced rather than asserted.

What it is not. It carries no DML, so it is not a backfill -- and it is not one on
purpose. Every relation is new, and no fact in this database is honestly classifiable
as a canonical `Run`, `RunStep`, `Attempt`, `Wait` or `RuntimeEvent`: the durable job
substrate is what a run *binds to* rather than a losing representation to be copied,
and the control-plane `RunRecord` is not in this database at all. So no
`omnivia_migration_0018_*_gate` is declared, and `test_no_backfill_is_owed_and_none_is
_declared` is the durable statement of why one would have had to be invented.

It is also not RT-103, RT-104 or RT-202: no artifact, evidence or cleanup store, no
command handling, no policy, budget, grant, approval or effect relation, no second
scheduler, no second queue, no second idempotency mechanism, and no public wire change.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    StaleGeneration,
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    verify_fingerprint,
)
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
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

from omnivia_core.contracts.v1 import RUN_STATUS_TRANSITIONS, RUN_STATUSES

MIGRATION_VERSION = 18
PREDECESSOR_VERSION = 17
MIGRATION_NAME = "0018_agent_runtime_records.sql"
WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = m1.OTHER_WORKSPACE_ID
BASE_US = 2_200_000_000_000_000

RUNS = "omnivia_runtime_runs"
STEPS = "omnivia_runtime_run_steps"
STEP_STATES = "omnivia_runtime_run_step_states"
ATTEMPTS = "omnivia_runtime_attempts"
ATTEMPT_OUTCOMES = "omnivia_runtime_attempt_outcomes"
WAITS = "omnivia_runtime_waits"
WAIT_RESOLUTIONS = "omnivia_runtime_wait_resolutions"
EVENTS = "omnivia_runtime_events"

TABLES = (
    RUNS,
    STEPS,
    STEP_STATES,
    ATTEMPTS,
    ATTEMPT_OUTCOMES,
    WAITS,
    WAIT_RESOLUTIONS,
    EVENTS,
)

INDEXES = (
    "omnivia_idx_runtime_run_step_states_latest",
    "omnivia_idx_runtime_attempts_run",
    "omnivia_idx_runtime_waits_run",
    "omnivia_idx_runtime_waits_step",
)

#: Every migration this slice builds on, pinned by content. Each is immutable once
#: applied, so an edit to any of them is a defect this file reports rather than
#: something a later reader has to notice in a diff. `m1` pins only 0000-0006, which
#: was the accepted corpus when it was written.
ACCEPTED_MIGRATION_CHECKSUMS = {
    **m1.ACCEPTED_MIGRATION_CHECKSUMS,
    "0007_application_audit_and_idempotency.sql": (
        "1a315bb097092c1981bf9c3175d8a8c69fb5436006f7a4e6145f9ef9ca42891a"
    ),
    "0008_blobs_staged_sources_and_evidence.sql": (
        "5a725ba9fb9bd04f8295b6730abe5010ca5a5b1df8e0f8168010998fad7417bc"
    ),
    "0009_governed_truth_and_relations.sql": (
        "dd3af667fb7d59ccb53a59fc46264932220a3af28f118cb4c2a934cd3a5bb387"
    ),
    "0010_durable_job_history.sql": (
        "e5b4d7a27f5421a35b426e6da0ef8aacfe3f13c59ae2434bc9814b18bcd39dbe"
    ),
    "0011_projection_lifecycle.sql": (
        "1bf1c0730667bb6ffb705a130e994b28a9b641a8a6f6ccae8cf4df5c15329b15"
    ),
    "0012_evidence_search_projection.sql": (
        "4dc45b839e7381ed85ae6b0fc1af2772306d3863c3ce3479aa41d775bb8d1a0b"
    ),
    "0013_mutation_execution_records.sql": (
        "5594eb2fe0a48f866d0338dc1c8e232d372be15c3c5270fde7a6c1e5b0a4ae91"
    ),
    "0014_application_governed_bridges.sql": (
        "d699ef1f978709e902948c277ac4d63834960e18c650b7c032d6456f9c180f8e"
    ),
    "0015_application_job_bridges.sql": (
        "1a9262ff3c89e8eb6010ba70e2c53756531cd452c4a3ff877d96f96f0c92f666"
    ),
    "0016_governance_transition_actor_binding.sql": (
        "82a205437ca5cd5cc4bc87898af342e8ab3e328939fa67fe678d749e6a3db87b"
    ),
    "0017_connector_sync_state.sql": (
        "b383cedd52e6f381424e3b7f5020aa49db139306e0e6acbd315fc8c6896f241e"
    ),
}

JOB_ID = "job-run-0001"
RUN_ID = "run-0001"
STEP_ID = "step-0001"
ATTEMPT_ID = "att-0001"
WAIT_ID = "wait-0001"
DIGEST = "sha256:" + "c" * 64


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def ddl_only_connection() -> sqlite3.Connection:
    """The slice's tables and indexes with none of its triggers, in memory.

    A guard trigger fires before the column constraints behind it, so on the real
    schema a trigger's message is the one a bad value gets. Replaying the DDL alone
    leaves the typed `CHECK`s as the sole defence, which is how a test proves one
    stands on its own rather than being carried by the layer in front of it.
    """
    connection = sqlite3.connect(":memory:")
    # Autocommit, so `PRAGMA foreign_keys` is never issued inside an implicit
    # transaction -- where SQLite silently ignores it.
    connection.isolation_level = None
    for statement in MIGRATION_STATEMENTS:
        if not statement.lstrip().upper().startswith("CREATE TRIGGER"):
            connection.execute(statement)
    return connection


# --- fixtures and seeding -------------------------------------------------------


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


def audit_ref_for(job_id: str) -> str:
    return f"aud-{job_id}"


def claim_id_for(job_id: str) -> str:
    return f"clm-{job_id}"


def logical_key_for(job_id: str) -> str:
    return f"lk-{job_id}"


def insert_claim(
    holder: m1.Owned,
    *,
    claim_id: str,
    audit_ref: str,
    idempotency_key: str,
    principal_id: str = "core-service",
    operation: str = "runtime.admit",
    workspace_id: str = WORKSPACE_ID,
) -> None:
    """One scoped application idempotency claim, unsettled.

    Unsettled on purpose: the claim is made when the command is admitted, and the run
    it admits is what eventually produces the answer that settles it. Nothing here
    writes `omnivia_idempotency_outcomes`.
    """
    holder.connection.execute(
        "INSERT INTO omnivia_idempotency_claims "
        "(claim_id, workspace_id, principal_id, operation, idempotency_key, "
        "request_digest, audit_ref, claimed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim_id,
            workspace_id,
            principal_id,
            operation,
            idempotency_key,
            DIGEST,
            audit_ref,
            BASE_US,
        ),
    )


def seed_job(
    holder: m1.Owned,
    *,
    job_id: str = JOB_ID,
    state: str = "claimed",
    workspace_id: str = WORKSPACE_ID,
    audit_ref: str | None = None,
) -> str:
    """The audited durable job, and the idempotency claim, a canonical run binds to.

    All four facts are the ones migrations 0007, 0010 and 0001 already own: the audit
    event, the scheduler row, the job's application metadata and the scoped claim. 0018
    adds no fifth, and the run's admission guard checks that it agrees with all of them.
    """
    audit_ref = audit_ref or audit_ref_for(job_id)
    with guarded(holder):
        holder.connection.execute(
            "INSERT OR IGNORE INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) VALUES "
            "(?, ?, 'core-service', 'runtime.admit', 'runtime.execute', ?, ?, ?, "
            "'{}', 'succeeded', NULL, ?)",
            (
                audit_ref,
                workspace_id,
                f"req-{job_id}",
                f"cor-{job_id}",
                f"trc-{job_id}",
                BASE_US,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', ?, '{}', ?, ?, ?, ?)",
            (
                job_id,
                state,
                "2026-08-23T00:00:00Z",
                "2026-08-23T00:00:00Z",
                holder.generation,
                holder.identity.service_instance_id,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_job_application_metadata "
            "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
            "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
            "max_attempts) VALUES (?, ?, 'ingestion.import', 'runtime.admit', ?, ?, "
            "NULL, 1, 8)",
            (workspace_id, job_id, audit_ref, BASE_US),
        )
        insert_claim(
            holder,
            claim_id=claim_id_for(job_id),
            audit_ref=audit_ref,
            idempotency_key=logical_key_for(job_id),
            workspace_id=workspace_id,
        )
    return audit_ref


def _insert(holder: m1.Owned, table: str, values: dict[str, object]) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    holder.connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def insert_run(holder: m1.Owned, **overrides: object) -> None:
    """One canonical run, defaulting to the exact facts `seed_job` recorded for its job.

    The claim, logical key and audit reference are derived from the effective `job_id`
    rather than fixed, because the admission guard requires all three to agree with the
    claim and the job metadata. A test that wants one of them to disagree overrides it
    and says so.
    """
    job_id = str(overrides.get("job_id", JOB_ID))
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "job_id": job_id,
        "claim_id": claim_id_for(job_id),
        "definition_kind": "agent_component",
        "definition_id": "component.triage",
        "definition_version": "1.4.0",
        "logical_key": logical_key_for(job_id),
        "originating_operation": "runtime.admit",
        "audit_ref": audit_ref_for(job_id),
        "created_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, RUNS, values)


def insert_step(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_step_id": STEP_ID,
        "run_id": RUN_ID,
        "ordinal": 1,
        "step_kind": "plan",
        "created_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, STEPS, values)


def insert_step_state(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_step_id": STEP_ID,
        "state_sequence": 0,
        "status": "pending",
        "observed_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, STEP_STATES, values)


def insert_attempt(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "attempt_id": ATTEMPT_ID,
        "run_id": RUN_ID,
        "run_step_id": STEP_ID,
        "attempt_number": 1,
        "started_at_us": BASE_US,
    }
    values.update(overrides)
    _insert(holder, ATTEMPTS, values)


def insert_attempt_outcome(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "attempt_id": ATTEMPT_ID,
        "status": "cancelled",
        "finished_at_us": BASE_US + 10,
        "failure_json": None,
        "failure_digest": None,
        "failure_byte_length": None,
    }
    values.update(overrides)
    _insert(holder, ATTEMPT_OUTCOMES, values)


def insert_wait(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "wait_id": WAIT_ID,
        "run_id": RUN_ID,
        "run_step_id": STEP_ID,
        "kind": "approval",
        "created_at_us": BASE_US,
        "expires_at_us": None,
        "resume_digest": DIGEST,
    }
    values.update(overrides)
    _insert(holder, WAITS, values)


def insert_wait_resolution(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "wait_id": WAIT_ID,
        "status": "resolved",
        "resolved_at_us": BASE_US + 5,
        "resolution_reason": "approved",
        "approval_id": None,
    }
    values.update(overrides)
    _insert(holder, WAIT_RESOLUTIONS, values)


def insert_event(holder: m1.Owned, **overrides: object) -> None:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "sequence": 0,
        "runtime_event_id": "evt-0001",
        "occurred_at_us": BASE_US,
        "event_kind": "run_admitted",
        "run_status": "admitted",
        "run_step_id": None,
        "message": None,
        "details_json": None,
        "details_digest": None,
        "details_byte_length": None,
    }
    values.update(overrides)
    _insert(holder, EVENTS, values)


def seed_admitted_run(holder: m1.Owned) -> None:
    seed_job(holder)
    with guarded(holder):
        insert_run(holder)
        insert_event(holder)


def seed_run_with_step(holder: m1.Owned) -> None:
    seed_admitted_run(holder)
    with guarded(holder):
        insert_step(holder)
        insert_step_state(holder)


# --- migration identity ----------------------------------------------------------


def test_0018_is_the_unique_consecutive_successor_to_0017() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    # A prefix claim, not a claim about the head: a slice has authority over its own
    # position in the sequence and none over what lands after it, so RT-103's
    # migration must not turn this file red.
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


def test_an_edited_migration_is_refused_rather_than_accepted(migrated: Path) -> None:
    """The checksum pin is what makes "0018 is applied" mean *this* 0018."""
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
            for m in original()
        )
        migrations_module.load_migrations = lambda: edited  # type: ignore[assignment]
        try:
            with pytest.raises(Exception, match="has changed since it was applied"):
                apply_pending_migrations(
                    connection,
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    service_instance_id=m1.SERVICE_INSTANCE,
                    fencing_generation=state.fencing_generation,
                    workspace_id=WORKSPACE_ID,
                )
        finally:
            migrations_module.load_migrations = original  # type: ignore[assignment]
    finally:
        connection.close()


def test_every_migration_before_this_one_is_byte_for_byte_unchanged() -> None:
    """0018 is additive; nothing at or before 0017 moved to make room for it."""
    package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
    for name, expected in ACCEPTED_MIGRATION_CHECKSUMS.items():
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name
    assert len(ACCEPTED_MIGRATION_CHECKSUMS) == MIGRATION_VERSION


def test_the_migration_carries_no_dml() -> None:
    """A schema slice that writes a row would be changing data, not shape."""
    for statement in MIGRATION_STATEMENTS:
        head = statement.split(None, 1)[0].upper()
        assert head == "CREATE", statement[:80]


def test_no_backfill_is_owed_and_none_is_declared(owned: m1.Owned) -> None:
    """Nothing already stored is honestly one of these records, so nothing is copied.

    The proof is in two halves. No `omnivia_migration_0018_*` gate object exists,
    because there is no copy to verify; and a workspace holding a complete durable
    job -- audit event, scheduler row, application metadata, attempt, event -- reaches
    the 0018 head with every new relation still empty. A backfill from that job would
    have had to invent a definition kind, a definition version, a logical key, a
    policy snapshot and a budget snapshot that no row carries.
    """
    seed_job(owned)
    with guarded(owned):
        owned.connection.execute(
            "INSERT INTO omnivia_job_attempts "
            "(workspace_id, job_id, attempt_number, started_at_us, state) "
            "VALUES (?, ?, 1, ?, 'running')",
            (WORKSPACE_ID, JOB_ID, BASE_US),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_job_events "
            "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
            "VALUES (?, ?, 0, ?, 'running', 'import started')",
            (WORKSPACE_ID, JOB_ID, BASE_US),
        )
    for table in TABLES:
        count = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert count[0] == 0, table
    declared = owned.connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_migration_0018%'"
    ).fetchall()
    assert declared == []
    assert not any("omnivia_migration_0018" in s for s in MIGRATION_STATEMENTS)


# --- application, preservation and drift -----------------------------------------


def test_the_canonical_schema_carries_every_object_this_slice_adds(
    migrated: Path,
) -> None:
    assert set(TABLES) <= canonical_schema_tables()
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert set(TABLES) <= names
        assert set(INDEXES) <= names
        verify_fingerprint(connection, canonical_schema_fingerprint())
        assert_guards_intact(connection)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'omnivia_guard_runtime_%'"
            )
        }
    finally:
        connection.close()
    # A subset claim, not a claim about the whole `omnivia_guard_runtime_%` namespace:
    # RT-103 names its own guards in it too, and this slice has authority over its own
    # eight tables' twenty-four triggers and none over what lands beside them.
    own_triggers = {
        f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
        for table in TABLES
        for statement in ("insert", "update", "delete")
    }
    assert len(own_triggers) == 24
    assert own_triggers <= triggers


def test_drift_in_any_object_this_slice_adds_is_detected(migrated: Path) -> None:
    expected = canonical_schema_fingerprint()
    connection = sqlite3.connect(str(migrated))
    try:
        connection.executescript(f"CREATE TABLE {RUNS}_drift (id TEXT PRIMARY KEY);")
        connection.commit()
    finally:
        connection.close()
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        with pytest.raises(SchemaDrift):
            verify_fingerprint(connection, expected)
    finally:
        connection.close()


def test_integrity_and_foreign_keys_are_clean_after_the_migration(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    finally:
        connection.close()


def test_a_pristine_workspace_also_reaches_this_migration(tmp_path: Path) -> None:
    path = tmp_path / "pristine.sqlite"
    open_database(path, OpenMode.EPHEMERAL).close()
    m1.bootstrap_and_migrate(path, adopt_phase0=False)
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert MIGRATION_VERSION in applied_migrations(connection)
        assert foreign_key_check(connection) == []
        assert integrity_check(connection) == []
    finally:
        connection.close()


def test_a_populated_0017_head_reaches_0018_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    """Applied from the exact accepted head, over data, this slice only adds."""
    path = tmp_path / "at-0017.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            seed_job(holder)
            with guarded(holder):
                holder.connection.execute(
                    "INSERT INTO omnivia_job_attempts "
                    "(workspace_id, job_id, attempt_number, started_at_us, state) "
                    "VALUES (?, ?, 1, ?, 'running')",
                    (WORKSPACE_ID, JOB_ID, BASE_US),
                )
            before = capture_inventory(holder.connection)
        finally:
            holder.connection.close()
        head = open_database(path, OpenMode.EPHEMERAL)
        try:
            assert max(applied_migrations(head)) == PREDECESSOR_VERSION
        finally:
            head.close()

    # Scoped to exactly this slice's own version: a prefix claim about 0018 landing
    # cleanly on a populated 0017 head, not a claim that no migration ever follows it.
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
    assert before.total_rows > 0
    # The two ledger tables record that 0018 ran, which is the migrator doing its job
    # rather than this slice touching data. Everything else must be identical, row
    # count and content digest alike.
    ledger = {"omnivia_migration_attempts", "omnivia_schema_migrations"}
    for entry in before.tables:
        if entry.name in ledger:
            continue
        assert after.table(entry.name) == entry, entry.name
    assert set(after.table_names) - set(before.table_names) == set(TABLES)
    for table in TABLES:
        added = after.table(table)
        assert added is not None and added.row_count == 0, table
    # `allow_added_tables` is not passed: `compare_inventories` already exempts every
    # `omnivia_`-prefixed table, so naming these eight would have read as a permission
    # this comparison needed and did not.
    differences = compare_inventories(before, after)
    assert differences and all(
        any(name in difference for name in ledger) for difference in differences
    ), differences


def test_a_backup_of_a_populated_workspace_restores_every_new_fact(
    owned: m1.Owned, tmp_path: Path
) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_attempt_outcome(owned, status="succeeded")
        insert_wait(owned)
        insert_wait_resolution(owned)
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 15,
            event_kind="run_started",
            run_status="running",
        )
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0003",
            occurred_at_us=BASE_US + 20,
            event_kind="run_succeeded",
            run_status="succeeded",
        )
    populated = capture_inventory(owned.connection)
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    backup = create_verified_backup(
        owned.path, installation, workspace_id=WORKSPACE_ID, attempt_id=new_attempt_id()
    )
    assert backup.verified

    restored = tmp_path / "restored.sqlite"
    from omnivia_core_runtime.storage.backup import restore_backup

    restore_backup(backup.path, restored)
    connection = open_database(restored, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert capture_inventory(connection) == populated
        for table in TABLES:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert row[0] >= 1, table
    finally:
        connection.close()


# --- immutability and authority ---------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned, table: str
) -> None:
    """The owner may append run history; it may never rewrite it."""
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_attempt_outcome(owned)
        insert_wait(owned)
        insert_wait_resolution(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = 'other'")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


#: One complete, otherwise-valid row for each table, written against a seeded run whose
#: two steps, unfinished attempt and unresolved wait already exist. Each is the row that
#: *would* commit under an open guard, so a test using them isolates the guard as the
#: only reason a write is refused -- a partial row is refused by `NOT NULL` and proves
#: nothing about the guard at all.
COMPLETE_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        RUNS,
        {
            "workspace_id": WORKSPACE_ID,
            "run_id": "run-0002",
            "job_id": "job-run-0002",
            "claim_id": "clm-job-run-0002",
            "definition_kind": "workflow",
            "definition_id": "flow.triage",
            "definition_version": "1.0.0",
            "logical_key": "lk-job-run-0002",
            "originating_operation": "runtime.admit",
            "audit_ref": "aud-job-run-0002",
            "created_at_us": BASE_US,
        },
    ),
    (
        STEPS,
        {
            "workspace_id": WORKSPACE_ID,
            "run_step_id": "step-0003",
            "run_id": RUN_ID,
            "ordinal": 3,
            "step_kind": "act",
            "created_at_us": BASE_US,
        },
    ),
    (
        STEP_STATES,
        {
            "workspace_id": WORKSPACE_ID,
            "run_step_id": "step-0002",
            "state_sequence": 1,
            "status": "running",
            "observed_at_us": BASE_US + 40,
        },
    ),
    (
        ATTEMPTS,
        {
            "workspace_id": WORKSPACE_ID,
            "attempt_id": "att-0002",
            "run_id": RUN_ID,
            "run_step_id": "step-0002",
            "attempt_number": 1,
            "started_at_us": BASE_US + 40,
        },
    ),
    (
        ATTEMPT_OUTCOMES,
        {
            "workspace_id": WORKSPACE_ID,
            "attempt_id": ATTEMPT_ID,
            "status": "succeeded",
            "finished_at_us": BASE_US + 40,
        },
    ),
    (
        WAITS,
        {
            "workspace_id": WORKSPACE_ID,
            "wait_id": "wait-0002",
            "run_id": RUN_ID,
            "run_step_id": "step-0002",
            "kind": "timer",
            "created_at_us": BASE_US + 40,
            "expires_at_us": None,
            "resume_digest": DIGEST,
        },
    ),
    (
        WAIT_RESOLUTIONS,
        {
            "workspace_id": WORKSPACE_ID,
            "wait_id": WAIT_ID,
            "status": "cancelled",
            "resolved_at_us": BASE_US + 40,
            "resolution_reason": "run_cancelled",
        },
    ),
    (
        EVENTS,
        {
            "workspace_id": WORKSPACE_ID,
            "run_id": RUN_ID,
            "sequence": 1,
            "runtime_event_id": "evt-0009",
            "occurred_at_us": BASE_US + 40,
            "event_kind": "run_started",
            "run_status": "running",
        },
    ),
)


def seed_every_parent(holder: m1.Owned) -> None:
    """Two steps, an unfinished attempt, an unresolved wait, and a spare durable job.

    Everything the complete rows above point at, and nothing they would collide with:
    the second step carries no attempt, no wait and no terminal state, so the rows
    naming it are refused only by the guard under test.
    """
    seed_run_with_step(holder)
    seed_job(holder, job_id="job-run-0002")
    with guarded(holder):
        insert_step(holder, run_step_id="step-0002", ordinal=2)
        insert_step_state(holder, run_step_id="step-0002")
        insert_attempt(holder)
        insert_wait(holder)


def test_the_complete_rows_the_guard_tests_use_would_otherwise_commit(
    owned: m1.Owned,
) -> None:
    """The control for the refusal below: under an open guard, every one of them lands.

    Without this, "the write was refused" could mean the row was malformed rather than
    the guard being what stopped it -- which is exactly what a partial row proves.
    """
    seed_every_parent(owned)
    for table, values in COMPLETE_ROWS:
        with guarded(owned):
            _insert(owned, table, values)
        count = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert count[0] >= 1, table


@pytest.mark.parametrize("table,values", COMPLETE_ROWS)
def test_no_open_guard_means_no_runtime_write_at_all(
    owned: m1.Owned, table: str, values: dict[str, object]
) -> None:
    """The persisted trigger is the refusal, not the connection authorizer.

    Mutations are widened deliberately, so the authorizer is out of the way and the
    only thing left between a complete valid row and the table is the guard predicate
    the migration installed -- which is the layer that also covers connections this
    runtime did not create.
    """
    seed_every_parent(owned)
    before = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    close_guard(owned.connection)
    with (
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
    ):
        _insert(owned, table, values)
    after = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert after == before


def test_a_stale_fencing_generation_cannot_append_run_history(owned: m1.Owned) -> None:
    seed_job(owned)
    with pytest.raises(StaleGeneration), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation + 1,
    ):
        insert_run(owned)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {RUNS}").fetchone()[0] == 0


def test_a_foreign_workspace_cannot_append_run_history(owned: m1.Owned) -> None:
    seed_job(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
        insert_run(owned, workspace_id=OTHER_WORKSPACE_ID)


# --- run admission and its binding to the scheduler --------------------------------


def test_a_run_binds_an_existing_open_durable_job(owned: m1.Owned) -> None:
    seed_job(owned)
    with guarded(owned):
        insert_run(owned)
    row = owned.connection.execute(
        f"SELECT job_id FROM {RUNS} WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone()
    assert row[0] == JOB_ID
    scheduler_rows = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_durable_jobs"
    ).fetchone()
    assert scheduler_rows[0] == 1


def test_a_run_without_a_durable_job_has_nothing_to_bind_to(owned: m1.Owned) -> None:
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert_run(owned)


def test_a_run_may_not_be_admitted_onto_a_finished_job(owned: m1.Owned) -> None:
    seed_job(owned, state="succeeded")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="open durable job"
    ):
        insert_run(owned)


def test_one_durable_job_carries_at_most_one_canonical_run(owned: m1.Owned) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="only one"):
        insert_run(owned, run_id="run-0002")


def test_one_logical_key_names_at_most_one_run(owned: m1.Owned) -> None:
    """Two honest claims can carry one key string; only one run may bear it.

    The 0007 claim scope is `(workspace, principal, operation, key)`, so a second
    principal claiming the same key is a legitimately distinct claim. The run's logical
    key is not scoped that way -- it is the identity of the work -- so the second
    admission is refused here rather than by borrowing the claim's uniqueness.
    """
    seed_admitted_run(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        insert_claim(
            owned,
            claim_id="clm-second-principal",
            audit_ref=audit_ref_for("job-run-0002"),
            idempotency_key=logical_key_for(JOB_ID),
            principal_id="core-service-2",
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="logical key already names a run"
    ):
        insert_run(
            owned,
            run_id="run-0002",
            job_id="job-run-0002",
            claim_id="clm-second-principal",
            logical_key=logical_key_for(JOB_ID),
        )


def test_a_run_audit_reference_must_belong_to_this_workspace(owned: m1.Owned) -> None:
    seed_job(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert_run(owned, audit_ref="aud-absent")


# --- the existing application idempotency authority ----------------------------------


def test_a_run_is_admitted_under_an_existing_scoped_claim(owned: m1.Owned) -> None:
    """The binding is to 0007's relation, not to a new one this slice invented."""
    seed_admitted_run(owned)
    row = owned.connection.execute(
        f"SELECT c.claim_id, c.operation, c.idempotency_key, c.audit_ref "
        f"FROM {RUNS} r JOIN omnivia_idempotency_claims c "
        "ON c.claim_id = r.claim_id AND c.workspace_id = r.workspace_id "
        "WHERE r.workspace_id = ? AND r.run_id = ?",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone()
    assert row == (
        claim_id_for(JOB_ID),
        "runtime.admit",
        logical_key_for(JOB_ID),
        audit_ref_for(JOB_ID),
    )
    # No second idempotency relation was added, and none is required to settle.
    added = {
        str(name[0])
        for name in owned.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE '%idempot%'"
        )
    }
    assert added == {"omnivia_idempotency_claims", "omnivia_idempotency_outcomes"}
    settled = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_idempotency_outcomes"
    ).fetchone()
    assert settled[0] == 0


def test_a_run_may_not_be_admitted_under_a_claim_that_is_not_its_own(
    owned: m1.Owned,
) -> None:
    """Every duplicated contract field is checked against the authority carrying it.

    The originating operation is carried by both the claim and the job metadata, and
    the metadata guard is the one that answers first; either refusal is the same fact,
    which is the point of requiring both to agree.
    """
    seed_job(owned)
    for field, value, expected in (
        ("claim_id", "clm-absent", "idempotency claim admitting it"),
        ("logical_key", "lk-something-else", "idempotency claim admitting it"),
        ("audit_ref", audit_ref_for("job-run-0002"), "run audit workspace mismatch"),
        ("originating_operation", "runtime.resolve", "application metadata of its job"),
    ):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match=expected):
            insert_run(owned, **{field: value})
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {RUNS}").fetchone()[0] == 0


def test_one_idempotency_claim_admits_at_most_one_run(owned: m1.Owned) -> None:
    """A claim is one admitted command, so it cannot have admitted two runs.

    The second job is seeded against the *same* audit event, because a run's audit
    reference must be the claim's; that is the only shape in which a second run could
    otherwise reach this rule at all.
    """
    seed_admitted_run(owned)
    seed_job(owned, job_id="job-run-0002", audit_ref=audit_ref_for(JOB_ID))
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="claim already admitted a run"
    ):
        insert_run(
            owned,
            run_id="run-0002",
            job_id="job-run-0002",
            claim_id=claim_id_for(JOB_ID),
            logical_key=logical_key_for(JOB_ID),
            audit_ref=audit_ref_for(JOB_ID),
        )


def test_a_run_may_not_contradict_the_application_metadata_of_its_job(
    owned: m1.Owned,
) -> None:
    """The duplicated operation and audit reference must agree with 0010's row too.

    Seeded so that the claim agrees and only the job disagrees: the second job carries
    its own audit reference, and a run quoting the first job's claim and audit reference
    while binding the second job is describing two different commands at once.
    """
    seed_job(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="application metadata of its job"
    ):
        insert_run(
            owned,
            job_id="job-run-0002",
            claim_id=claim_id_for(JOB_ID),
            logical_key=logical_key_for(JOB_ID),
            audit_ref=audit_ref_for(JOB_ID),
        )


def test_the_claim_binding_is_immutable_like_the_rest_of_the_run(
    owned: m1.Owned,
) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {RUNS} SET claim_id = 'clm-other'")


# --- steps and their status history -------------------------------------------------


def test_step_ordinals_must_be_contiguous_within_their_run(owned: m1.Owned) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="ordinals must be contiguous"
    ):
        insert_step(owned, ordinal=2)


def test_a_step_of_another_run_is_refused(owned: m1.Owned) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert_step(owned, run_id="run-absent")


def test_a_step_may_not_predate_its_run(owned: m1.Owned) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="predate the run"):
        insert_step(owned, created_at_us=BASE_US - 1)


def test_step_state_sequences_are_contiguous_and_never_regress_in_time(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="state sequence must be contiguous"
    ):
        insert_step_state(owned, state_sequence=2, status="running")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="predate the step"):
        insert_step_state(
            owned, state_sequence=1, status="running", observed_at_us=BASE_US - 1
        )
    with guarded(owned):
        insert_step_state(
            owned, state_sequence=1, status="running", observed_at_us=BASE_US + 10
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not regress"):
        insert_step_state(
            owned, state_sequence=2, status="waiting", observed_at_us=BASE_US + 9
        )


def test_a_step_state_that_restates_its_predecessor_records_nothing(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="restates its predecessor"
    ):
        insert_step_state(owned, state_sequence=1, status="pending")


def test_a_terminal_step_state_is_final(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_step_state(
            owned, state_sequence=1, status="succeeded", observed_at_us=BASE_US + 5
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="terminal step state is final"
    ):
        insert_step_state(
            owned, state_sequence=2, status="running", observed_at_us=BASE_US + 6
        )


def test_a_waiting_step_must_name_the_wait_holding_it(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="must name the wait holding it"
    ):
        insert_step_state(
            owned, state_sequence=1, status="waiting", observed_at_us=BASE_US + 1
        )
    with guarded(owned):
        insert_wait(owned)
        insert_step_state(
            owned, state_sequence=1, status="waiting", observed_at_us=BASE_US + 1
        )


def test_an_unrecognised_step_status_has_no_column_to_live_in(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_step_state(owned, state_sequence=1, status="probably_done")


# --- attempts -----------------------------------------------------------------------


def test_attempt_numbers_must_be_contiguous_within_their_step(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="attempt number must be contiguous"
    ):
        insert_attempt(owned, attempt_number=2)


def test_an_attempt_must_name_the_run_of_its_own_step(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        insert_event(
            owned,
            run_id="run-0002",
            runtime_event_id="evt-r2",
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="run of its own step"
    ):
        insert_attempt(owned, run_id="run-0002")


def test_only_a_failed_cancelled_or_uncertain_attempt_may_be_followed_by_another(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_attempt_outcome(owned, status="succeeded")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="may be followed by another"
    ):
        insert_attempt(
            owned, attempt_id="att-0002", attempt_number=2, started_at_us=BASE_US + 20
        )


def test_an_unfinished_attempt_may_not_be_followed_by_another(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="may be followed by another"
    ):
        insert_attempt(
            owned, attempt_id="att-0002", attempt_number=2, started_at_us=BASE_US + 20
        )


def test_attempts_of_one_step_must_not_overlap(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_attempt_outcome(owned, status="uncertain", finished_at_us=BASE_US + 30)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not overlap"):
        insert_attempt(
            owned, attempt_id="att-0002", attempt_number=2, started_at_us=BASE_US + 29
        )
    with guarded(owned):
        insert_attempt(
            owned, attempt_id="att-0002", attempt_number=2, started_at_us=BASE_US + 30
        )


def test_an_attempt_terminalizes_exactly_once(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_attempt_outcome(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert_attempt_outcome(owned, status="succeeded")


def test_an_attempt_cannot_finish_before_it_started(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="finish before it started"
    ):
        insert_attempt_outcome(owned, finished_at_us=BASE_US - 1)


def test_an_uncertain_attempt_carries_no_failure(owned: m1.Owned) -> None:
    """Uncertainty is not failure; an error on it would licence the forbidden retry."""
    seed_run_with_step(owned)
    document = '{"code":"internal_recoverable"}'
    with guarded(owned):
        insert_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_attempt_outcome(
            owned,
            status="uncertain",
            failure_json=document,
            failure_digest=DIGEST,
            failure_byte_length=len(document.encode()),
        )


def test_a_failed_attempt_must_state_its_failure(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_attempt_outcome(owned, status="failed")


def test_a_failure_document_states_the_length_of_what_was_hashed(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    document = '{"code":"internal_recoverable"}'
    with guarded(owned):
        insert_attempt(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_attempt_outcome(
            owned,
            status="failed",
            failure_json=document,
            failure_digest=DIGEST,
            failure_byte_length=len(document.encode()) + 1,
        )


# --- waits -------------------------------------------------------------------------


def test_a_wait_must_name_the_run_of_its_own_step(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        insert_event(owned, run_id="run-0002", runtime_event_id="evt-r2")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="run of its own step"
    ):
        insert_wait(owned, run_id="run-0002")


def test_a_step_may_hold_only_one_unresolved_wait(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only one unresolved wait"
    ):
        insert_wait(owned, wait_id="wait-0002")
    with guarded(owned):
        insert_wait_resolution(owned)
        insert_wait(owned, wait_id="wait-0002", created_at_us=BASE_US + 6)


def test_a_wait_stops_being_pending_exactly_once(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned)
        insert_wait_resolution(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert_wait_resolution(owned, status="cancelled", resolution_reason="run_cancelled")


def test_only_an_approval_wait_names_an_approval(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned, kind="timer", expires_at_us=BASE_US + 100)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="resolved approval wait"
    ):
        insert_wait_resolution(owned, approval_id="apr-0001")


def test_an_unresolved_approval_is_never_named_by_a_cancellation(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_wait_resolution(
            owned,
            status="cancelled",
            resolution_reason="run_cancelled",
            approval_id="apr-0001",
        )


def test_a_wait_resolved_past_its_deadline_has_expired(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned, expires_at_us=BASE_US + 10)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="has expired, not resolved"
    ):
        insert_wait_resolution(owned, resolved_at_us=BASE_US + 11)


def test_only_a_time_bounded_wait_expires_and_never_before_its_deadline(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="only a time-bounded wait expires"
    ):
        insert_wait_resolution(
            owned, status="expired", resolution_reason="deadline_exceeded"
        )


def test_a_wait_cannot_be_resolved_before_it_was_created(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_wait(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="before it was created"
    ):
        insert_wait_resolution(owned, resolved_at_us=BASE_US - 1)


def test_a_resume_digest_that_is_not_a_content_checksum_is_refused(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_wait(owned, resume_digest="sha256:not-hex")


def test_a_run_holds_at_most_the_contract_s_sixty_four_waits(owned: m1.Owned) -> None:
    """The contract bounds a run's waits at 64, and so does the relation storing them.

    Resolved waits still count: the contract's `Run.waits` carries the whole history,
    so a run that could accumulate a thousand resolved waits would be a run no reader
    could assemble. Each wait here is resolved before the next opens, because a step
    holds only one unresolved wait at a time.
    """
    seed_run_with_step(owned)
    for index in range(64):
        with guarded(owned):
            insert_wait(
                owned,
                wait_id=f"wait-{index:04d}",
                kind="timer",
                created_at_us=BASE_US + index,
            )
            insert_wait_resolution(
                owned,
                wait_id=f"wait-{index:04d}",
                status="cancelled",
                resolution_reason="run_cancelled",
                resolved_at_us=BASE_US + index,
            )
    held = owned.connection.execute(
        f"SELECT COUNT(*) FROM {WAITS} WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone()
    assert held[0] == 64
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most 64 waits"):
        insert_wait(
            owned, wait_id="wait-0064", kind="timer", created_at_us=BASE_US + 64
        )


# --- terminal closure, and what a terminal run and a terminal step still admit --------


def terminalize_run(holder: m1.Owned, *, status: str = "cancelled") -> None:
    """Take the seeded run to a terminal status through a legal transition."""
    with guarded(holder):
        insert_event(
            holder,
            sequence=1,
            runtime_event_id="evt-run-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
        )
        insert_event(
            holder,
            sequence=2,
            runtime_event_id="evt-run-0003",
            run_status=status,
            event_kind=f"run_{status}",
            occurred_at_us=BASE_US + 2,
        )


def test_a_terminal_run_admits_terminal_step_closure_and_nothing_else(
    owned: m1.Owned,
) -> None:
    """Cancellation has to be able to close a step it stopped; it may not restart one.

    Without the second half a cancelled run could still record a step going `running`
    or back to `pending`, which is a contradictory history the surrounding guards --
    written about steps, attempts and waits rather than step states -- never looked at.
    """
    seed_run_with_step(owned)
    with guarded(owned):
        # The wait exists so that `waiting` reaches the rule under test rather than the
        # older guard about naming the wait holding it; the step is `running` so that
        # `pending` is a genuine regression rather than a restatement.
        insert_wait(owned)
        insert_step_state(
            owned, state_sequence=1, status="running", observed_at_us=BASE_US + 1
        )
    terminalize_run(owned)
    for refused in ("pending", "waiting"):
        with guarded(owned), pytest.raises(
            sqlite3.IntegrityError, match="only terminal step closure"
        ):
            insert_step_state(
                owned, state_sequence=2, status=refused, observed_at_us=BASE_US + 3
            )
    with guarded(owned):
        insert_step_state(
            owned, state_sequence=2, status="cancelled", observed_at_us=BASE_US + 3
        )


def test_a_terminal_run_still_admits_the_closure_of_work_already_in_flight(
    owned: m1.Owned,
) -> None:
    """An attempt that was running and a wait that was pending are closed honestly.

    Refusing these would leave a cancelled run holding an attempt that never finished
    and a wait that never resolved -- a record that says the work is still going.
    """
    seed_run_with_step(owned)
    with guarded(owned):
        insert_attempt(owned)
        insert_wait(owned)
    terminalize_run(owned)
    with guarded(owned):
        insert_attempt_outcome(owned, status="cancelled", finished_at_us=BASE_US + 3)
        insert_wait_resolution(
            owned,
            status="cancelled",
            resolution_reason="run_cancelled",
            resolved_at_us=BASE_US + 3,
        )
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {ATTEMPT_OUTCOMES}").fetchone()[0]
        == 1
    )
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {WAIT_RESOLUTIONS}").fetchone()[0]
        == 1
    )


def test_a_terminal_step_admits_no_further_attempt_or_wait(owned: m1.Owned) -> None:
    """A step that is done is done, whatever the run around it is still doing.

    The run stays `running` here on purpose: the terminal-run guards would otherwise be
    what refuses these, and the rule under test is about the step.
    """
    seed_run_with_step(owned)
    with guarded(owned):
        insert_step_state(
            owned, state_sequence=1, status="succeeded", observed_at_us=BASE_US + 1
        )
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="terminal step admits no further attempt"
    ):
        insert_attempt(owned, started_at_us=BASE_US + 2)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="terminal step admits no further wait"
    ):
        insert_wait(owned, created_at_us=BASE_US + 2)


# --- the event stream ----------------------------------------------------------------


def test_a_run_stream_opens_at_sequence_zero_with_the_admitted_status(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    with guarded(owned):
        insert_run(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="opens at sequence zero"
    ):
        insert_event(owned, run_status="running")
    with guarded(owned):
        insert_event(owned)
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="opens at sequence zero"
    ):
        insert_event(
            owned, sequence=1, runtime_event_id="evt-0002", occurred_at_us=BASE_US + 1
        )


def test_run_event_sequences_are_contiguous_and_never_regress_in_time(
    owned: m1.Owned,
) -> None:
    seed_admitted_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="predate its run"):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US - 1,
        )
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 10,
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="must not regress"):
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0003",
            run_status="running",
            event_kind="step_started",
            occurred_at_us=BASE_US + 9,
        )


#: How to drive a fresh run's stream to each status, as the events after sequence zero.
#: Every terminal status is a sink, so the four terminal ones are reached last and admit
#: nothing after them.
_PATH_TO_STATUS: dict[str, tuple[str, ...]] = {
    "admitted": (),
    "running": ("running",),
    "waiting": ("running", "waiting"),
    "uncertain": ("running", "uncertain"),
    "succeeded": ("running", "succeeded"),
    "partially_completed": ("running", "partially_completed"),
    "failed": ("running", "failed"),
    "cancelled": ("running", "cancelled"),
}


def drive_run_to(holder: m1.Owned, run_id: str, job_id: str, status: str) -> int:
    """Admit one fresh run and walk its stream to `status`; return the next sequence."""
    seed_job(holder, job_id=job_id)
    with guarded(holder):
        insert_run(holder, run_id=run_id, job_id=job_id)
        insert_event(holder, run_id=run_id, runtime_event_id=f"evt-{run_id}-0")
        for index, step in enumerate(_PATH_TO_STATUS[status], start=1):
            insert_event(
                holder,
                run_id=run_id,
                sequence=index,
                runtime_event_id=f"evt-{run_id}-{index}",
                run_status=step,
                event_kind=f"run_{step}",
                occurred_at_us=BASE_US + index,
            )
    return len(_PATH_TO_STATUS[status]) + 1


def test_the_sql_transition_map_is_the_contract_s_transition_map(
    owned: m1.Owned,
) -> None:
    """Every ordered pair of run statuses, decided by the schema and by the contract.

    A single invalid pair proved only that *some* transition is refused. The map lives
    in two places -- `semantics_runtime.RUN_STATUS_TRANSITIONS` and the CASE-free
    predicate in 0018's event guard -- and the only way they cannot drift is for the
    full grid to be walked against the real relation.

    Identity pairs are excluded deliberately: the contract's map answers "which status
    may *follow* this one", and restating the current status is an ordinary event
    rather than a transition, which
    `test_repeating_the_current_status_is_an_ordinary_event` proves separately.
    """
    checked = 0
    for index, (origin, target) in enumerate(
        (a, b) for a in RUN_STATUSES for b in RUN_STATUSES if a != b
    ):
        run_id = f"run-t{index:04d}"
        sequence = drive_run_to(owned, run_id, f"job-t{index:04d}", origin)
        arguments: dict[str, object] = {
            "run_id": run_id,
            "sequence": sequence,
            "runtime_event_id": f"evt-{run_id}-x",
            "run_status": target,
            "event_kind": f"run_{target}",
            "occurred_at_us": BASE_US + 100,
        }
        if target in RUN_STATUS_TRANSITIONS[origin]:
            with guarded(owned):
                insert_event(owned, **arguments)
        else:
            # Three guards say "not in the map", each for its own reason: a sink is
            # refused as a terminal event, `admitted` after sequence zero is refused as
            # a reopened stream, and everything else by the transition predicate.
            with guarded(owned), pytest.raises(
                sqlite3.IntegrityError,
                match="transition is not permitted|event is final|opens at sequence zero",
            ):
                insert_event(owned, **arguments)
        checked += 1
    assert checked == len(RUN_STATUSES) * (len(RUN_STATUSES) - 1)


def test_repeating_the_current_status_is_an_ordinary_event(owned: m1.Owned) -> None:
    seed_admitted_run(owned)
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
        )
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0003",
            run_status="running",
            event_kind="step_started",
            occurred_at_us=BASE_US + 2,
        )
    count = owned.connection.execute(f"SELECT COUNT(*) FROM {EVENTS}").fetchone()
    assert count[0] == 3


def test_a_terminal_run_event_is_final_and_closes_the_run_to_new_history(
    owned: m1.Owned,
) -> None:
    seed_run_with_step(owned)
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
        )
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0003",
            run_status="cancelled",
            event_kind="run_cancelled",
            occurred_at_us=BASE_US + 2,
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="event is final"):
        insert_event(
            owned,
            sequence=3,
            runtime_event_id="evt-0004",
            run_status="cancelled",
            event_kind="run_cancelled",
            occurred_at_us=BASE_US + 3,
        )
    for table, insert in (
        (STEPS, lambda: insert_step(owned, run_step_id="step-0002", ordinal=2)),
        (
            ATTEMPTS,
            lambda: insert_attempt(owned, started_at_us=BASE_US + 3),
        ),
        (WAITS, lambda: insert_wait(owned, created_at_us=BASE_US + 3)),
    ):
        with guarded(owned), pytest.raises(
            sqlite3.IntegrityError, match="admits no further history"
        ):
            insert()


def test_an_uncertain_run_is_not_terminal_and_may_still_move(owned: m1.Owned) -> None:
    """An unreconciled effect is an open question, not an outcome."""
    seed_admitted_run(owned)
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
        )
        insert_event(
            owned,
            sequence=2,
            runtime_event_id="evt-0003",
            run_status="uncertain",
            event_kind="effect_unsettled",
            occurred_at_us=BASE_US + 2,
        )
        insert_event(
            owned,
            sequence=3,
            runtime_event_id="evt-0004",
            run_status="succeeded",
            event_kind="run_succeeded",
            occurred_at_us=BASE_US + 3,
        )
    statuses = [
        row[0]
        for row in owned.connection.execute(
            f"SELECT run_status FROM {EVENTS} ORDER BY sequence"
        )
    ]
    assert statuses == ["admitted", "running", "uncertain", "succeeded"]


def test_a_runtime_event_identifier_is_used_at_most_once_per_workspace(
    owned: m1.Owned,
) -> None:
    seed_admitted_run(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        insert_run(owned, run_id="run-0002", job_id="job-run-0002")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="at most once"):
        insert_event(owned, run_id="run-0002")


def test_a_run_event_may_only_name_a_step_of_its_own_run(owned: m1.Owned) -> None:
    seed_run_with_step(owned)
    seed_job(owned, job_id="job-run-0002")
    with guarded(owned):
        insert_run(owned, run_id="run-0002", job_id="job-run-0002")
        insert_event(owned, run_id="run-0002", runtime_event_id="evt-r2")
    with guarded(owned), pytest.raises(
        sqlite3.IntegrityError, match="step of its own run"
    ):
        insert_event(
            owned,
            run_id="run-0002",
            sequence=1,
            runtime_event_id="evt-r2b",
            run_status="running",
            event_kind="step_started",
            run_step_id=STEP_ID,
            occurred_at_us=BASE_US + 1,
        )


def test_an_event_detail_states_the_digest_and_length_of_what_was_hashed(
    owned: m1.Owned,
) -> None:
    seed_admitted_run(owned)
    document = '{"reason":"policy_narrowed"}'
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
            details_json=document,
        )
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
            details_json=document,
            details_digest=DIGEST,
            details_byte_length=len(document.encode()),
        )


def test_an_event_message_is_bounded_but_may_be_empty(owned: m1.Owned) -> None:
    """The contract states no `minLength`, so the column states none either.

    An empty message is a message the writer chose not to fill in, and refusing it here
    would make a payload the contract accepts unstorable. The bounds that do exist --
    2048 characters, and no NUL -- still hold.
    """
    seed_admitted_run(owned)
    with guarded(owned):
        insert_event(
            owned,
            sequence=1,
            runtime_event_id="evt-0002",
            run_status="running",
            event_kind="run_started",
            occurred_at_us=BASE_US + 1,
            message="",
        )
    stored = owned.connection.execute(
        f"SELECT message FROM {EVENTS} WHERE sequence = 1"
    ).fetchone()
    assert stored[0] == ""
    for refused in ("x" * 2049, "still\x00here"):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_event(
                owned,
                sequence=2,
                runtime_event_id="evt-0003",
                run_status="running",
                event_kind="step_started",
                occurred_at_us=BASE_US + 2,
                message=refused,
            )


def test_an_unrecognised_run_status_has_no_column_to_live_in() -> None:
    """The closed schema vocabulary, proved without a trigger answering first."""
    connection = ddl_only_connection()
    try:
        for status in ("probably_fine", "admitted "):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(
                    f"INSERT INTO {EVENTS} (workspace_id, run_id, sequence, "
                    "runtime_event_id, occurred_at_us, event_kind, run_status) "
                    "VALUES (?, ?, 0, 'evt-0001', ?, 'run_admitted', ?)",
                    (WORKSPACE_ID, RUN_ID, BASE_US, status),
                )
        connection.execute(
            f"INSERT INTO {EVENTS} (workspace_id, run_id, sequence, "
            "runtime_event_id, occurred_at_us, event_kind, run_status) "
            "VALUES (?, ?, 0, 'evt-0001', ?, 'run_admitted', 'admitted')",
            (WORKSPACE_ID, RUN_ID, BASE_US),
        )
    finally:
        connection.close()


def test_a_fractional_microsecond_has_no_column_to_live_in() -> None:
    """`typeof(...) = 'integer'` is what makes normalisation exact rather than rounded."""
    connection = ddl_only_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                f"INSERT INTO {RUNS} (workspace_id, run_id, job_id, claim_id, "
                "definition_kind, definition_id, definition_version, logical_key, "
                "originating_operation, audit_ref, created_at_us) "
                "VALUES (?, ?, ?, 'clm-0001', 'workflow', 'flow.triage', '1.0.0', "
                "'lk-0001', 'runtime.admit', 'aud-0001', ?)",
                (WORKSPACE_ID, RUN_ID, JOB_ID, float(BASE_US) + 0.5),
            )
    finally:
        connection.close()


def test_a_cross_workspace_reference_is_refused_by_the_foreign_key_alone() -> None:
    """The composite keys carry workspace scoping without help from the triggers.

    Seeded with foreign keys off, because the run's own parents belong to migrations
    outside this slice; switched on for the assertion, so the only thing left to
    refuse a child pointing at another workspace's step is the composite key itself.
    """
    connection = ddl_only_connection()
    try:
        connection.execute(
            f"INSERT INTO {STEPS} (workspace_id, run_step_id, run_id, ordinal, "
            "step_kind, created_at_us) VALUES (?, ?, ?, 1, 'plan', ?)",
            (WORKSPACE_ID, STEP_ID, RUN_ID, BASE_US),
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"INSERT INTO {STEP_STATES} (workspace_id, run_step_id, state_sequence, "
            "status, observed_at_us) VALUES (?, ?, 0, 'pending', ?)",
            (WORKSPACE_ID, STEP_ID, BASE_US),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                f"INSERT INTO {STEP_STATES} (workspace_id, run_step_id, "
                "state_sequence, status, observed_at_us) "
                "VALUES (?, ?, 0, 'pending', ?)",
                (OTHER_WORKSPACE_ID, STEP_ID, BASE_US),
            )
    finally:
        connection.close()
