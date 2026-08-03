"""V06-1 M5 acceptance for durable projection lifecycle evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_durable_job_history_migration as m4
from omnivia_core_runtime.ownership.fencing import close_guard, fenced_transaction
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
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 11
MIGRATION_NAME = "0011_projection_lifecycle.sql"
WORKSPACE_ID = m2.WORKSPACE_ID
BASE_US = m4.BASE_US + 1000
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64

TABLES = {
    "omnivia_projection_runs",
    "omnivia_projection_run_checkpoints",
    "omnivia_projection_record_failures",
    "omnivia_projection_validations",
    "omnivia_projection_activations",
}
INDEXES = {
    "omnivia_idx_projection_runs_state_epoch",
    "omnivia_idx_projection_checkpoints_latest",
    "omnivia_idx_projection_failures_record_code",
    "omnivia_idx_projection_validations_outcome",
    "omnivia_idx_projection_activations_history",
}
TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{verb}"
    for table in TABLES
    for verb in ("insert", "update", "delete")
}
TRIGGERS.update(
    {
        "omnivia_apply_projection_activation",
        "omnivia_guard_projection_ledger_active_pointer_update",
    }
)
LEDGER_COLUMNS = {
    "projection_kind",
    "schema_version",
    "profile_version",
    "active_run_id",
    "active_epoch",
    "active_source_checkpoint",
    "active_build_digest",
    "active_validation_digest",
    "activated_at_us",
}
ACCEPTED_PREDECESSOR_HASHES = {
    **m4.ACCEPTED_PREDECESSOR_HASHES,
    10: "e5b4d7a27f5421a35b426e6da0ef8aacfe3f13c59ae2434bc9814b18bcd39dbe",
}


def migration_under_test() -> Any:
    found = [
        migration
        for migration in load_migrations()
        if migration.version == MIGRATION_VERSION
    ]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))

M5_RETRY_CHILD = """
import json
import sys
from pathlib import Path
import omnivia_core_runtime.storage.migrations as migration_module
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations, load_migrations, read_workspace_state,
)

owned = tuple(migration for migration in load_migrations() if migration.version <= 11)
migration_module.load_migrations = lambda: owned
path, service, workspace_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result = {
        "applied": [migration.version for migration in applied],
        "integrity": integrity_check(connection),
        "foreign_keys": foreign_key_check(connection),
    }
finally:
    connection.close()
print(json.dumps(result, sort_keys=True))
"""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    seed_ledger(holder)
    yield holder
    holder.connection.close()


def guarded(holder: m2.Owned):
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def seed_ledger(holder: m2.Owned, projection_id: str = "projection-1") -> None:
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_projection_ledger "
            "(projection_id, version, rebuildable, state, updated_at, "
            "fencing_generation) VALUES (?, 'legacy-v1', 1, 'ready', "
            "'2026-08-03T00:00:00Z', ?)",
            (projection_id, holder.generation),
        )


def run_row(
    run_id: str = "run-1",
    *,
    projection_id: str = "projection-1",
    epoch: int = 1,
    checkpoint: str = "source:1",
    started_at: int = BASE_US,
    schema_version: str = "schema-v1",
    profile_version: str = "profile-v1",
    resumed_from: tuple[str, int] | None = None,
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": projection_id,
        "run_id": run_id,
        "projection_kind": "knowledge.search",
        "schema_version": schema_version,
        "profile_version": profile_version,
        "source_checkpoint": checkpoint,
        "target_epoch": epoch,
        "started_at_us": started_at,
        "validation_started_at_us": None,
        "finished_at_us": None,
        "state": "running",
        "input_record_count": None,
        "output_record_count": None,
        "build_digest": None,
        "error_json": None,
        "resumed_from_run_id": None if resumed_from is None else resumed_from[0],
        "resumed_from_checkpoint_sequence": (
            None if resumed_from is None else resumed_from[1]
        ),
    }


def checkpoint_row(
    run_id: str = "run-1",
    *,
    sequence: int = 0,
    at: int = BASE_US + 1,
    input_count: int = 2,
    output_count: int = 2,
    source_checkpoint: str = "source:1",
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": "projection-1",
        "run_id": run_id,
        "checkpoint_sequence": sequence,
        "created_at_us": at,
        "source_checkpoint": source_checkpoint,
        "cursor_json": json.dumps({"offset": sequence}, separators=(",", ":")),
        "checkpoint_digest": DIGEST_A,
        "input_record_count": input_count,
        "output_record_count": output_count,
    }


def failure_row(
    run_id: str = "run-1", *, sequence: int = 0, at: int = BASE_US + 1
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": "projection-1",
        "run_id": run_id,
        "failure_sequence": sequence,
        "record_id": f"record-{sequence}",
        "phase": "build.parse",
        "error_code": "record.invalid",
        "sanitized_detail": "invalid source record",
        "occurred_at_us": at,
    }


def start_run(holder: m2.Owned, **kwargs: Any) -> None:
    with guarded(holder):
        insert(holder.connection, "omnivia_projection_runs", run_row(**kwargs))


def begin_validation(
    holder: m2.Owned, run_id: str = "run-1", *, at: int = BASE_US + 2
) -> None:
    with guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'validating', "
            "validation_started_at_us = ? WHERE workspace_id = ? "
            "AND projection_id = 'projection-1' AND run_id = ?",
            (at, WORKSPACE_ID, run_id),
        )


def validate(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    accepted: int = 1,
    at: int = BASE_US + 3,
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
    input_count: int = 2,
    output_count: int = 2,
) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_projection_validations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "run_id": run_id,
                "validated_at_us": at,
                "accepted": accepted,
                "input_record_count": input_count,
                "output_record_count": output_count,
                "build_digest": build_digest,
                "report_json": '{"ok":true}' if accepted else '{"ok":false}',
                "validation_digest": validation_digest,
            },
        )


def finish_success(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    at: int = BASE_US + 4,
    build_digest: str = DIGEST_A,
    input_count: int = 2,
    output_count: int = 2,
) -> None:
    with guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'succeeded', "
            "finished_at_us = ?, input_record_count = ?, output_record_count = ?, "
            "build_digest = ? WHERE workspace_id = ? "
            "AND projection_id = 'projection-1' AND run_id = ?",
            (at, input_count, output_count, build_digest, WORKSPACE_ID, run_id),
        )


def complete_run(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    epoch: int = 1,
    checkpoint: str = "source:1",
    start: int = BASE_US,
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
    schema_version: str = "schema-v1",
    profile_version: str = "profile-v1",
) -> None:
    start_run(
        holder,
        run_id=run_id,
        epoch=epoch,
        checkpoint=checkpoint,
        started_at=start,
        schema_version=schema_version,
        profile_version=profile_version,
    )
    begin_validation(holder, run_id, at=start + 2)
    validate(
        holder,
        run_id,
        at=start + 3,
        build_digest=build_digest,
        validation_digest=validation_digest,
    )
    finish_success(holder, run_id, at=start + 4, build_digest=build_digest)


def activate(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    sequence: int = 0,
    epoch: int = 1,
    previous: str | None = None,
    at: int = BASE_US + 5,
    checkpoint: str = "source:1",
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_projection_activations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "activation_sequence": sequence,
                "run_id": run_id,
                "target_epoch": epoch,
                "previous_run_id": previous,
                "activated_at_us": at,
                "source_checkpoint": checkpoint,
                "build_digest": build_digest,
                "validation_digest": validation_digest,
            },
        )


def test_m5_01_lineage_and_immutable_predecessors() -> None:
    migrations = [
        migration for migration in load_migrations() if migration.version <= 11
    ]
    assert [migration.version for migration in migrations] == list(range(1, 12))
    assert migrations[-1].name == MIGRATION_NAME
    assert {m.version: m.checksum for m in migrations[:-1]} == (
        ACCEPTED_PREDECESSOR_HASHES
    )
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode()).hexdigest()


def test_m5_02_pristine_convergence_and_exact_inventory(owned: m2.Owned) -> None:
    assert max(applied_migrations(owned.connection)) == MIGRATION_VERSION
    assert owned.connection.execute("PRAGMA user_version").fetchone() == (11,)
    before = sqlite3.connect(":memory:")
    try:
        before.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version <= 10:
                before.executescript(migration.sql)
        assert (
            m2.object_names(owned.connection, "table")
            - m2.object_names(before, "table")
            == TABLES
        )
        assert (
            m2.object_names(owned.connection, "index")
            - m2.object_names(before, "index")
            == INDEXES
        )
        assert (
            m2.object_names(owned.connection, "trigger")
            - m2.object_names(before, "trigger")
            == TRIGGERS
        )
        assert m2.object_names(owned.connection, "view") == m2.object_names(
            before, "view"
        )
    finally:
        before.close()
    columns = {
        row[1]
        for row in owned.connection.execute(
            "PRAGMA table_info(omnivia_projection_ledger)"
        )
    }
    assert LEDGER_COLUMNS <= columns


def test_m5_03_adopted_rows_are_preserved_without_synthetic_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(10):
        m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    seed_ledger(holder, "legacy-projection")
    excluded = {"omnivia_schema_migrations", "omnivia_migration_attempts"}
    old_tables = m2.object_names(holder.connection, "table") - excluded
    old_columns = {
        table: [
            str(row[1])
            for row in holder.connection.execute(f'PRAGMA table_info("{table}")')
        ]
        for table in old_tables
    }

    def rows(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
        columns = old_columns[table]
        quoted = ", ".join(f'"{column}"' for column in columns)
        return connection.execute(
            f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'
        ).fetchall()

    before = {table: rows(holder.connection, table) for table in old_tables}
    holder.connection.close()
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = m2.read_workspace_state(connection)
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m2.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    assert {table: rows(connection, table) for table in old_tables} == before
    assert (
        connection.execute(
            "SELECT projection_kind, schema_version, profile_version, active_run_id, "
            "active_epoch, active_source_checkpoint, active_build_digest, "
            "active_validation_digest, activated_at_us "
            "FROM omnivia_projection_ledger WHERE projection_id = 'legacy-projection'"
        ).fetchone()
        == (None,) * 9
    )
    for table in TABLES:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    connection.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "bad space"),
        ("projection_id", ""),
        ("run_id", "nul\x00id"),
        ("projection_kind", "Bad.Kind"),
        ("schema_version", "bad space"),
        ("profile_version", ""),
        ("source_checkpoint", ""),
        ("target_epoch", 0),
        ("started_at_us", 0),
        ("build_digest", "SHA256:" + "a" * 64),
    ],
)
def test_m5_04_run_scalar_matrices_fail_closed(
    owned: m2.Owned, field: str, value: object
) -> None:
    row = run_row()
    row[field] = value
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned.connection, "omnivia_projection_runs", row)


@pytest.mark.parametrize(
    "surface",
    [
        "run_projection_kind",
        "run_schema_version",
        "run_profile_version",
        "checkpoint_digest",
        "failure_record_id",
        "failure_phase",
        "failure_error_code",
        "validation_build_digest",
        "validation_digest",
    ],
)
def test_m5_04a_embedded_nul_text_fields_fail_closed(
    owned: m2.Owned, surface: str
) -> None:
    hidden_suffix = "\x00hidden"
    if surface.startswith("run_"):
        field = surface.removeprefix("run_")
        row = run_row()
        row[field] = f"{row[field]}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_runs", row)
        return

    start_run(owned)
    if surface == "checkpoint_digest":
        row = checkpoint_row()
        row["checkpoint_digest"] = f"{DIGEST_A}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_run_checkpoints", row)
        return

    if surface.startswith("failure_"):
        field = surface.removeprefix("failure_")
        row = failure_row()
        row[field] = f"{row[field]}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_record_failures", row)
        return

    begin_validation(owned)
    if surface == "validation_build_digest":
        with pytest.raises(sqlite3.IntegrityError):
            validate(owned, build_digest=f"{DIGEST_A}{hidden_suffix}")
        return
    with pytest.raises(sqlite3.IntegrityError):
        validate(owned, validation_digest=f"{DIGEST_B}{hidden_suffix}")


def test_m5_05_run_insert_shape_and_lifecycle(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="start"):
        row = run_row("run-bad", epoch=2)
        row.update(
            {
                "state": "validating",
                "validation_started_at_us": BASE_US + 1,
            }
        )
        insert(owned.connection, "omnivia_projection_runs", row)
    begin_validation(owned)
    validate(owned)
    finish_success(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="transition"):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "error_json = '{\"code\":\"late\"}' WHERE run_id = 'run-1'"
        )


def test_m5_06_failure_closeout_requires_canonical_error(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "finished_at_us = ?, error_json = ? WHERE run_id = 'run-1'",
            (BASE_US + 1, '{ "code": "bad" }'),
        )
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "finished_at_us = ?, error_json = ? WHERE run_id = 'run-1'",
            (BASE_US + 1, '{"code":"bad"}'),
        )


def test_m5_07_resume_pair_requires_same_projection_checkpoint(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            'finished_at_us = ?, error_json = \'{"code":"retry"}\' '
            "WHERE run_id = 'run-1'",
            (BASE_US + 2,),
        )
    start_run(
        owned,
        run_id="run-2",
        epoch=1,
        started_at=BASE_US + 3,
        resumed_from=("run-1", 0),
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="resume"):
        insert(
            owned.connection,
            "omnivia_projection_runs",
            run_row(
                "run-3",
                epoch=2,
                checkpoint="source:2",
                started_at=BASE_US + 4,
                resumed_from=("run-1", 0),
            ),
        )


def test_m5_08_checkpoint_contiguity_time_and_counts(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(
                sequence=1,
                at=BASE_US + 2,
                input_count=3,
                output_count=2,
            ),
        )
    for bad in (
        checkpoint_row(sequence=3, at=BASE_US + 3),
        checkpoint_row(sequence=2, at=BASE_US, input_count=3),
        checkpoint_row(sequence=2, at=BASE_US + 3, input_count=1),
    ):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_run_checkpoints", bad)


def test_m5_09_record_failures_are_contiguous_and_state_bound(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(owned.connection, "omnivia_projection_record_failures", failure_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=2, at=BASE_US + 2),
        )
    begin_validation(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=1, at=BASE_US + 2),
        )
    validate(owned, accepted=0, at=BASE_US + 3)
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', finished_at_us = ?, "
            "error_json = '{\"code\":\"validation\"}' WHERE run_id = 'run-1'",
            (BASE_US + 4,),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="requires"):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=2, at=BASE_US + 5),
        )


def test_m5_10_validation_timing_uniqueness_and_report(owned: m2.Owned) -> None:
    start_run(owned)
    with pytest.raises(sqlite3.IntegrityError, match="requires"):
        validate(owned)
    begin_validation(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(
            owned.connection,
            "omnivia_projection_validations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "run_id": "run-1",
                "validated_at_us": BASE_US + 3,
                "accepted": 1,
                "input_record_count": 2,
                "output_record_count": 2,
                "build_digest": DIGEST_A,
                "report_json": '{ "ok": true }',
                "validation_digest": DIGEST_B,
            },
        )
    validate(owned)
    with pytest.raises(sqlite3.IntegrityError):
        validate(owned)


def test_m5_11_success_requires_accepted_matching_validation(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    begin_validation(owned)
    validate(owned, accepted=0)
    with pytest.raises(sqlite3.IntegrityError, match="accepted"):
        finish_success(owned)


def test_m5_12_first_activation_populates_exact_pointer(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    assert owned.connection.execute(
        "SELECT projection_kind, schema_version, profile_version, active_run_id, "
        "active_epoch, active_source_checkpoint, active_build_digest, "
        "active_validation_digest, activated_at_us FROM omnivia_projection_ledger "
        "WHERE projection_id = 'projection-1'"
    ).fetchone() == (
        "knowledge.search",
        "schema-v1",
        "profile-v1",
        "run-1",
        1,
        "source:1",
        DIGEST_A,
        DIGEST_B,
        BASE_US + 5,
    )


def test_m5_13_later_activation_atomically_supersedes(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    complete_run(
        owned,
        "run-2",
        epoch=2,
        checkpoint="source:2",
        start=BASE_US + 10,
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
        schema_version="schema-v2",
        profile_version="profile-v2",
    )
    activate(
        owned,
        "run-2",
        sequence=1,
        epoch=2,
        previous="run-1",
        at=BASE_US + 15,
        checkpoint="source:2",
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
    )
    assert owned.connection.execute(
        "SELECT run_id, state FROM omnivia_projection_runs ORDER BY run_id"
    ).fetchall() == [("run-1", "superseded"), ("run-2", "succeeded")]
    assert owned.connection.execute(
        "SELECT active_run_id, active_epoch, schema_version, profile_version "
        "FROM omnivia_projection_ledger"
    ).fetchone() == ("run-2", 2, "schema-v2", "profile-v2")


def test_m5_14_rejected_failed_partial_never_replace_pointer(
    owned: m2.Owned,
) -> None:
    complete_run(owned)
    activate(owned)
    start_run(
        owned,
        run_id="run-2",
        epoch=2,
        checkpoint="source:2",
        started_at=BASE_US + 10,
    )
    begin_validation(owned, "run-2", at=BASE_US + 12)
    validate(owned, "run-2", accepted=0, at=BASE_US + 13)
    with pytest.raises(sqlite3.IntegrityError, match="succeeded"):
        activate(
            owned,
            "run-2",
            sequence=1,
            epoch=2,
            previous="run-1",
            at=BASE_US + 15,
            checkpoint="source:2",
        )
    assert owned.connection.execute(
        "SELECT active_run_id, active_epoch FROM omnivia_projection_ledger"
    ).fetchone() == ("run-1", 1)


def test_m5_15_direct_pointer_and_duplicate_activation_refused(
    owned: m2.Owned,
) -> None:
    complete_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="activation"):
        owned.connection.execute(
            "UPDATE omnivia_projection_ledger SET active_run_id = 'run-1', "
            "active_epoch = 1 WHERE projection_id = 'projection-1'"
        )
    activate(owned)
    with pytest.raises(sqlite3.IntegrityError):
        activate(owned)


def test_m5_16_all_tables_refuse_same_value_update_and_delete(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
        insert(owned.connection, "omnivia_projection_record_failures", failure_row())
    begin_validation(owned)
    validate(owned)
    finish_success(owned)
    activate(owned)
    keys = {
        "omnivia_projection_runs": "run_id = 'run-1'",
        "omnivia_projection_run_checkpoints": "run_id = 'run-1'",
        "omnivia_projection_record_failures": "run_id = 'run-1'",
        "omnivia_projection_validations": "run_id = 'run-1'",
        "omnivia_projection_activations": "run_id = 'run-1'",
    }
    for table, where in keys.items():
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                f"UPDATE {table} SET workspace_id = workspace_id WHERE {where}"
            )
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(f"DELETE FROM {table} WHERE {where}")


def test_m5_17_stock_sqlite_and_closed_guard_fail_closed(
    owned: m2.Owned,
) -> None:
    close_guard(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded"):
        insert(owned.connection, "omnivia_projection_runs", run_row())
    owned.connection.close()
    stock = sqlite3.connect(owned.path)
    stock.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        insert(stock, "omnivia_projection_runs", run_row())
    stock.close()


def test_m5_18_activation_failure_rolls_back_all_effects(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    complete_run(
        owned,
        "run-2",
        epoch=2,
        checkpoint="source:2",
        start=BASE_US + 10,
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
    )
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "CREATE TEMP TRIGGER inject_pointer_failure BEFORE UPDATE ON "
            "omnivia_projection_ledger BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        activate(
            owned,
            "run-2",
            sequence=1,
            epoch=2,
            previous="run-1",
            at=BASE_US + 15,
            checkpoint="source:2",
            build_digest=DIGEST_B,
            validation_digest=DIGEST_C,
        )
    assert owned.connection.execute(
        "SELECT state FROM omnivia_projection_runs WHERE run_id = 'run-1'"
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_projection_activations"
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT active_run_id FROM omnivia_projection_ledger"
    ).fetchone() == ("run-1",)


def test_m5_19_interruption_and_fresh_process_retry(tmp_path: Path) -> None:
    base = tmp_path / "through-0010.sqlite"
    materialise_phase0_baseline(base)
    with m2.migration_catalogue_through(10):
        m2.bootstrap_and_migrate(base)
    for stop_after in range(1, len(MIGRATION_STATEMENTS) + 1):
        path = tmp_path / f"interrupted-{stop_after}.sqlite"
        shutil.copy2(base, path)
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        state = m2.read_workspace_state(connection)
        crashing = m2.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with (
            m2.migration_catalogue_through(MIGRATION_VERSION),
            pytest.raises(m2.MigrationInterrupted),
        ):
            apply_pending_migrations(
                crashing,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
        connection.close()
        check = open_database(path, OpenMode.READ_ONLY)
        assert MIGRATION_VERSION not in applied_migrations(check)
        assert TABLES.isdisjoint(m2.object_names(check, "table"))
        check.close()
        assert m2.run_child(
            M5_RETRY_CHILD, str(path), m2.SERVICE_INSTANCE, WORKSPACE_ID
        ) == {"applied": [11], "foreign_keys": [], "integrity": []}


def test_m5_20_backup_fingerprint_integrity_and_foreign_keys(
    owned: m2.Owned, tmp_path: Path
) -> None:
    complete_run(owned)
    activate(owned)
    source_fingerprint = fingerprint_schema(owned.connection)
    source_rows = {
        table: owned.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in TABLES
    }
    owned.connection.close()
    installation = InstallationLayout(root=tmp_path / "installation")
    installation.create(WORKSPACE_ID)
    result = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert result.verified
    backup = open_database(result.path, OpenMode.READ_ONLY)
    assert fingerprint_schema(backup) == source_fingerprint
    assert integrity_check(backup) == []
    assert foreign_key_check(backup) == []
    for table in TABLES:
        assert backup.execute(f"SELECT * FROM {table}").fetchall() == source_rows[table]
    backup.close()


def test_m5_21_projection_loss_rebuild_oracle_is_deterministic(
    owned: m2.Owned,
) -> None:
    authoritative = [
        {"record_id": "record-1", "version": 1, "payload": "alpha"},
        {"record_id": "record-2", "version": 3, "payload": "beta"},
    ]

    def oracle(rows: list[dict[str, object]]) -> tuple[str, str, int]:
        encoded = json.dumps(
            sorted(rows, key=lambda row: str(row["record_id"])),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "source:2", "sha256:" + hashlib.sha256(encoded).hexdigest(), len(rows)

    checkpoint, digest, count = oracle(authoritative)
    complete_run(
        owned,
        checkpoint=checkpoint,
        build_digest=digest,
        validation_digest=DIGEST_B,
    )
    activate(owned, checkpoint=checkpoint, build_digest=digest)
    rebuilt = [dict(row) for row in reversed(authoritative)]
    assert oracle(rebuilt) == (checkpoint, digest, count)
    assert owned.connection.execute(
        "SELECT active_source_checkpoint, active_build_digest "
        "FROM omnivia_projection_ledger"
    ).fetchone() == (checkpoint, digest)


def test_m5_22_schema_is_healthy(owned: m2.Owned) -> None:
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    assert fingerprint_schema(owned.connection)
