"""RT-701 acceptance for runtime integrity, backup/restore, replay and compaction.

This is the M7 production-candidate persistence gate for the Agent Runtime slice.
The proof is deliberately over a real migrated SQLite workspace, not an in-memory
facsimile: a verified backup is taken with the online backup API, the live database
is damaged after the backup, restore replaces that damage, and equality is measured
through inventory, migration checksums, runtime read paths and the Run summary replay
digest. Compaction is a separate copy operation, and failed validation is refusal,
not repair.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt202_policy_budget_snapshot_repository as rt202
from omnivia_core_runtime.ownership.fencing import (
    UNGUARDED_SUBSTRATE_TABLES,
    open_guard,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.storage.agent_runtime import (
    admit_run,
    append_run_event,
    open_wait,
    read_run,
    record_step_status,
)
from omnivia_core_runtime.storage.backup import (
    BackupError,
    InstallationLayout,
    compact_database,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.inventory import (
    DatabaseInventory,
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    rebuild_runtime_run_summaries,
    runtime_run_summary_projection_digest,
)
from omnivia_core_runtime.storage.runtime_backup import restore_verified_backup
from omnivia_core_runtime.storage.runtime_integrity import (
    RuntimeIntegrityError,
    RuntimeIntegrityReport,
    check_runtime_integrity,
)

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US
MS = r102.MS

OPEN_RUN_ID = "run-rt701-open"
OPEN_JOB_ID = "job-rt701-open"
OPEN_STEP_ID = "step-rt701-open"
OPEN_WAIT_ID = "wait-rt701-open"


@dataclass(frozen=True, slots=True)
class RuntimeState:
    inventory: DatabaseInventory
    migration_ledger: tuple[tuple[int, str], ...]
    projection_digest: str
    integrity: RuntimeIntegrityReport


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def _take_successor(
    path: Path,
    *,
    instance: str,
    predecessor: str | None,
) -> m1.Owned:
    identity = m1.make_identity(instance=instance, pid=7010)
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=m1.FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=predecessor,
    )
    open_guard(
        connection,
        identity,
        clock=m1.FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return m1.Owned(
        connection=connection,
        identity=identity,
        generation=lease.fencing_generation,
        path=path,
    )


def _seed_runtime_history(holder: m1.Owned) -> None:
    """One completed run and one durable open wait/nonterminal run."""
    r102.admit(holder)
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=m18.RUN_ID,
        runtime_event_id="evt-rt701-terminal-running",
        occurred_at_us=BASE_US + MS,
        event_kind="run_started",
        run_status="running",
    )
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=m18.RUN_ID,
        runtime_event_id="evt-rt701-terminal-succeeded",
        occurred_at_us=BASE_US + 2 * MS,
        event_kind="run_succeeded",
        run_status="succeeded",
    )

    open_admission = r102.admission(
        run_id=OPEN_RUN_ID,
        job_id=OPEN_JOB_ID,
        logical_key=m18.logical_key_for(OPEN_JOB_ID),
        event_id="evt-rt701-open-admitted",
    )
    m18.seed_job(holder, job_id=OPEN_JOB_ID)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=open_admission,
    )
    r102.add_step(holder, run_id=OPEN_RUN_ID, run_step_id=OPEN_STEP_ID)
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=OPEN_RUN_ID,
        runtime_event_id="evt-rt701-open-running",
        occurred_at_us=BASE_US + 3 * MS,
        event_kind="run_started",
        run_status="running",
        run_step_id=OPEN_STEP_ID,
    )
    record_step_status(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_step_id=OPEN_STEP_ID,
        status="running",
        observed_at_us=BASE_US + 3 * MS,
    )
    open_wait(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        wait_id=OPEN_WAIT_ID,
        run_id=OPEN_RUN_ID,
        run_step_id=OPEN_STEP_ID,
        kind="external_signal",
        created_at_us=BASE_US + 4 * MS,
        resume_digest=m18.DIGEST,
    )
    record_step_status(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_step_id=OPEN_STEP_ID,
        status="waiting",
        observed_at_us=BASE_US + 4 * MS,
    )
    append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=OPEN_RUN_ID,
        runtime_event_id="evt-rt701-open-waiting",
        occurred_at_us=BASE_US + 4 * MS,
        event_kind="wait_opened",
        run_status="waiting",
        run_step_id=OPEN_STEP_ID,
    )


def _runtime_state(connection: sqlite3.Connection) -> RuntimeState:
    return RuntimeState(
        inventory=capture_inventory(connection),
        migration_ledger=tuple(sorted(applied_migrations(connection).items())),
        projection_digest=runtime_run_summary_projection_digest(
            connection, workspace_id=WORKSPACE_ID
        ),
        integrity=check_runtime_integrity(connection, workspace_id=WORKSPACE_ID),
    )


def _canonical_inventory(inventory: DatabaseInventory) -> DatabaseInventory:
    """`inventory` minus ownership-substrate bookkeeping.

    `omnivia_mutation_guard`, `omnivia_workspace_state` and `omnivia_workspace_lease`
    legitimately differ once a successor takes the lease -- a new owner names a
    different service instance and fencing generation by design, per ADR-037 -- so a
    whole-database comparison would fail on session bookkeeping that was never meant
    to survive unchanged. Restored/compacted equality is a claim about the canonical
    Agent Runtime data and the migration ledger, not about which process currently
    holds the lease.
    """
    tables = tuple(
        table for table in inventory.tables if table.name not in UNGUARDED_SUBSTRATE_TABLES
    )
    return DatabaseInventory(
        tables=tables,
        total_rows=sum(table.row_count for table in tables),
        content_checksum="",
    )


def _assert_same_logical_state(before: RuntimeState, after: RuntimeState) -> None:
    assert (
        compare_inventories(
            _canonical_inventory(before.inventory), _canonical_inventory(after.inventory)
        )
        == []
    )
    assert after.migration_ledger == before.migration_ledger
    assert after.projection_digest == before.projection_digest
    assert after.integrity == before.integrity


def _migration_statement(version: int, marker: str) -> str:
    for migration in load_migrations():
        if migration.version == version:
            for statement in split_sql_statements(migration.sql):
                if marker in statement:
                    return statement
    raise AssertionError(f"no migration {version} statement contains {marker!r}")


def _leave_free_pages(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE rt701_compaction_scratch (payload BLOB NOT NULL)"
        )
        payload = sqlite3.Binary(b"x" * 4096)
        connection.executemany(
            "INSERT INTO rt701_compaction_scratch (payload) VALUES (?)",
            [(payload,) for _ in range(256)],
        )
        connection.execute("DROP TABLE rt701_compaction_scratch")
        connection.commit()
    finally:
        connection.close()


def test_verified_backup_restores_runtime_equality_after_live_database_destruction(
    owned: m1.Owned, tmp_path: Path
) -> None:
    _seed_runtime_history(owned)
    baseline = _runtime_state(owned.connection)
    predecessor = owned.identity.service_instance_id

    # `SERVICE_OWNED` holds `PRAGMA locking_mode = EXCLUSIVE`: under WAL that means
    # this connection never releases its lock, so nothing else -- the backup API's
    # own read-only connection included -- may touch the file while it stays open.
    # The owner is closed first, exactly as the production backup path requires
    # (see test_graph_traverse.py's note on the same rule).
    owned.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    owned.connection.close()

    backup = create_verified_backup(
        owned.path,
        InstallationLayout(tmp_path / "installation"),
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
        name="runtime.sqlite",
    )

    owned.path.write_bytes(b"rt701 deliberately destroyed the live sqlite file")
    with pytest.raises((sqlite3.DatabaseError, StorageError)):
        sqlite3.connect(str(owned.path)).execute("PRAGMA integrity_check").fetchall()

    restore_verified_backup(backup, owned.path)
    restored = _take_successor(
        owned.path,
        instance="svc-rt701-restored",
        predecessor=predecessor,
    )
    try:
        _assert_same_logical_state(baseline, _runtime_state(restored.connection))

        rebuilt = rebuild_runtime_run_summaries(
            restored.connection,
            restored.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=restored.generation,
        )
        assert rebuilt.build_digest == baseline.projection_digest
        _assert_same_logical_state(baseline, _runtime_state(restored.connection))

        terminal = read_run(
            restored.connection, workspace_id=WORKSPACE_ID, run_id=m18.RUN_ID
        )
        assert terminal is not None
        assert terminal.status == "succeeded"

        open_run = read_run(
            restored.connection, workspace_id=WORKSPACE_ID, run_id=OPEN_RUN_ID
        )
        assert open_run is not None
        assert open_run.status == "waiting"
        assert open_run.finished_at is None
        assert open_run.waits[0].status == "pending"
    finally:
        restored.connection.close()


def test_compacted_runtime_copy_preserves_logical_state_and_refuses_overwrite(
    owned: m1.Owned, tmp_path: Path
) -> None:
    _seed_runtime_history(owned)
    predecessor = owned.identity.service_instance_id
    owned.connection.close()
    _leave_free_pages(owned.path)

    successor = _take_successor(
        owned.path,
        instance="svc-rt701-compaction-source",
        predecessor=predecessor,
    )
    baseline = _runtime_state(successor.connection)
    # Same locking rule as the backup step: `compact_database` opens its own
    # connection to `owned.path`, and `SERVICE_OWNED`'s `EXCLUSIVE` locking_mode
    # would otherwise block it. The owner is closed first.
    successor.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    successor.connection.close()

    source_bytes = owned.path.read_bytes()
    compacted = tmp_path / "compacted.sqlite"

    compact_database(owned.path, compacted)

    assert compacted.read_bytes() != source_bytes
    compacted_connection = open_database(compacted, OpenMode.READ_ONLY)
    try:
        _assert_same_logical_state(baseline, _runtime_state(compacted_connection))
    finally:
        compacted_connection.close()

    with pytest.raises(BackupError, match="refusing to overwrite"):
        compact_database(owned.path, compacted)


def test_corrupt_backup_is_refused_before_destination_changes(
    owned: m1.Owned, tmp_path: Path
) -> None:
    _seed_runtime_history(owned)
    baseline = _runtime_state(owned.connection)
    # Same locking rule as the other scenarios: close the owner before a second
    # connection -- the backup API's -- opens the same file.
    owned.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    owned.connection.close()

    backup = create_verified_backup(
        owned.path,
        InstallationLayout(tmp_path / "installation"),
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
        name="runtime.sqlite",
    )

    backup.path.write_bytes(b"rt701 deliberately destroyed the backup")
    with pytest.raises(BackupError, match="failed re-verification"):
        restore_verified_backup(backup, owned.path)

    unchanged = open_database(owned.path, OpenMode.READ_ONLY)
    try:
        _assert_same_logical_state(baseline, _runtime_state(unchanged))
    finally:
        unchanged.close()


def test_runtime_integrity_refuses_projection_drift_without_repair(
    owned: m1.Owned,
) -> None:
    _seed_runtime_history(owned)
    baseline = _runtime_state(owned.connection)
    recreate_update_trigger = _migration_statement(
        20, "CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_summaries_update"
    )
    drifted = r102.tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_run_summaries_update",
        f"UPDATE omnivia_runtime_run_summaries SET run_status = 'running' "
        f"WHERE workspace_id = '{WORKSPACE_ID}' AND run_id = '{OPEN_RUN_ID}'",
        recreate_update_trigger,
    )
    try:
        drifted_digest = runtime_run_summary_projection_digest(
            drifted, workspace_id=WORKSPACE_ID
        )
        assert drifted_digest != baseline.projection_digest

        with pytest.raises(RuntimeIntegrityError, match="does not reproduce"):
            check_runtime_integrity(drifted, workspace_id=WORKSPACE_ID)

        assert (
            runtime_run_summary_projection_digest(drifted, workspace_id=WORKSPACE_ID)
            == drifted_digest
        ), "integrity validation must not repair a drifted projection in place"
    finally:
        drifted.close()


def test_runtime_integrity_refuses_stored_runtime_document_digest_drift(
    owned: m1.Owned,
) -> None:
    _seed_runtime_history(owned)
    rt202.add_policy(owned, rt202.policy())
    recreate_update_trigger = _migration_statement(
        21, "CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_policy_snapshots_update"
    )
    drifted = r102.tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_policy_snapshots_update",
        f"UPDATE {rt202.POLICY_TABLE} SET snapshot_digest = 'sha256:{'e' * 64}' "
        f"WHERE workspace_id = '{WORKSPACE_ID}' AND policy_snapshot_id = "
        f"'{rt202.POLICY_ID}'",
        recreate_update_trigger,
    )
    try:
        with pytest.raises(
            RuntimeIntegrityError,
            match="failed canonical runtime document verification",
        ):
            check_runtime_integrity(drifted, workspace_id=WORKSPACE_ID)

        digest = drifted.execute(
            f"SELECT snapshot_digest FROM {rt202.POLICY_TABLE} "
            "WHERE workspace_id = ? AND policy_snapshot_id = ?",
            (WORKSPACE_ID, rt202.POLICY_ID),
        ).fetchone()
        assert digest == (f"sha256:{'e' * 64}",)
    finally:
        drifted.close()
