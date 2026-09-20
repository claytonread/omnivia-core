"""Acceptance for migration 0027's durable Workflow Runtime records."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 27
PREDECESSOR_VERSION = 26
MIGRATION_NAME = "0027_workflow_runs.sql"
WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US + 1_000

PLANS = "omnivia_workflow_plans"
STEPS = "omnivia_workflow_plan_steps"
RUNS = "omnivia_workflow_runs"
OBSERVATIONS = "omnivia_workflow_run_step_observations"
CORRELATIONS = "omnivia_workflow_child_correlations"
RESULTS = "omnivia_workflow_child_correlation_results"
EVIDENCE = "omnivia_workflow_run_completion_evidence"
COMPLETIONS = "omnivia_workflow_run_completions"

TABLES = (
    PLANS,
    STEPS,
    RUNS,
    OBSERVATIONS,
    CORRELATIONS,
    RESULTS,
    EVIDENCE,
    COMPLETIONS,
)

INDEXES = {
    "omnivia_idx_workflow_plans_identity",
    "omnivia_idx_workflow_plans_definition",
    "omnivia_idx_workflow_plan_steps_sequence",
    "omnivia_idx_workflow_child_correlations_fence",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

WORKFLOW_ID = "workflow-review"
WORKFLOW_VERSION = "1.0.0"
PLAN_HASH = "sha256:" + "b" * 64
DEFINITION_HASH = "sha256:" + "a" * 64
PLAN_STEP_ID = "a-plan"
CHILD_STEP_ID = "b-child"
CHILD_WORKFLOW_ID = "workflow-child"
CHILD_WORKFLOW_HASH = "sha256:" + "e" * 64
STEP_DEFINITION_HASH = "sha256:" + "c" * 64
MATERIALISED_STEP_HASH = "sha256:" + "d" * 64
EVIDENCE_DIGEST = "sha256:" + "f" * 64

BRANCH_JSON = (
    '{"input_key":"decision-kind","operator":"PRESENT","expected_value":null}'
)
CHILD_WORKFLOW_JSON = (
    '{"workflow_id":"workflow-child","version":"1.0.0",'
    f'"workflow_hash":"{CHILD_WORKFLOW_HASH}","budget":10}}'
)


def migration_under_test() -> Any:
    found = [migration for migration in load_migrations() if migration.version == 27]
    assert len(found) == 1, [migration.name for migration in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
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


def insert(holder: m1.Owned, table: str, row: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def insert_or_ignore(holder: m1.Owned, table: str, row: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def audit(holder: m1.Owned, audit_ref: str) -> None:
    holder.connection.execute(
        "INSERT OR IGNORE INTO omnivia_application_audit_events "
        "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
        "correlation_id, trace_id, granted_authority_json, outcome_class, "
        "error_code, recorded_at_us) VALUES "
        "(?, ?, 'core-service', 'workflow.record', 'runtime.execute', ?, ?, ?, "
        "'{}', 'succeeded', NULL, ?)",
        (
            audit_ref,
            WORKSPACE_ID,
            f"req-{audit_ref}",
            f"cor-{audit_ref}",
            f"trc-{audit_ref}",
            BASE_US,
        ),
    )


def plan_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "definition_hash": DEFINITION_HASH,
        "plan_hash": PLAN_HASH,
        "sealed_at_us": BASE_US,
        "audit_ref": "audit-plan",
    }
    values.update(overrides)
    return values


def plan_step_row(step_id: str = PLAN_STEP_ID, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "step_id": step_id,
        "component_id": "component-echo",
        "component_version": WORKFLOW_VERSION,
        "execution_class": "DETERMINISTIC",
        "route": "DETERMINISTIC",
        "sequence_index": 0,
        "depends_on_json": "[]",
        "branch_json": BRANCH_JSON,
        "loop_json": None,
        "child_workflow_json": None,
        "step_definition_hash": STEP_DEFINITION_HASH,
        "materialised_step_hash": MATERIALISED_STEP_HASH,
    }
    values.update(overrides)
    return values


def child_step_row(**overrides: object) -> dict[str, object]:
    values = plan_step_row(
        CHILD_STEP_ID,
        component_id="component-child",
        execution_class="WAIT",
        route="CHILD_WORKFLOW",
        sequence_index=1,
        depends_on_json=f'["{PLAN_STEP_ID}"]',
        branch_json=None,
        child_workflow_json=CHILD_WORKFLOW_JSON,
        step_definition_hash="sha256:" + "1" * 64,
        materialised_step_hash="sha256:" + "2" * 64,
    )
    values.update(overrides)
    return values


def workflow_run_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "plan_hash": PLAN_HASH,
        "bound_at_us": BASE_US + 20,
    }
    values.update(overrides)
    return values


def plan_observation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "step_id": PLAN_STEP_ID,
        "observation_kind": "plan",
        "route": "DETERMINISTIC",
        "sequence_index": 0,
        "branch_outcome": None,
        "branch_reason": None,
        "observed_at_us": BASE_US + 30,
    }
    values.update(overrides)
    return values


def branch_observation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "step_id": PLAN_STEP_ID,
        "observation_kind": "branch",
        "route": None,
        "sequence_index": None,
        "branch_outcome": "MATCHED",
        "branch_reason": "input-present",
        "observed_at_us": BASE_US + 31,
    }
    values.update(overrides)
    return values


def correlation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "correlation_id": "corr-0001",
        "parent_run_id": RUN_ID,
        "parent_step_id": CHILD_STEP_ID,
        "child_workflow_id": CHILD_WORKFLOW_ID,
        "child_version": WORKFLOW_VERSION,
        "child_workflow_hash": CHILD_WORKFLOW_HASH,
        "fence": 1,
        "budget": 10,
        "opened_at_us": BASE_US + 40,
    }
    values.update(overrides)
    return values


def result_row(sequence: int = 1, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "correlation_id": "corr-0001",
        "result_sequence": sequence,
        "outcome": "accepted",
        "fence": 1,
        "child_workflow_id": CHILD_WORKFLOW_ID,
        "child_version": WORKFLOW_VERSION,
        "child_workflow_hash": CHILD_WORKFLOW_HASH,
        "cost": 4,
        "recorded_at_us": BASE_US + 41 + sequence,
    }
    values.update(overrides)
    return values


def evidence_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "evidence_kind": "run-summary",
        "evidence_digest": EVIDENCE_DIGEST,
        "recorded_at_us": BASE_US + 50,
    }
    values.update(overrides)
    return values


def completion_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "outcome": "SUCCEEDED",
        "decided_at_us": BASE_US + 60,
        "audit_ref": "audit-completion",
    }
    values.update(overrides)
    return values


def seed_plan_header(holder: m1.Owned) -> None:
    with guarded(holder):
        audit(holder, "audit-plan")
        insert(holder, PLANS, plan_row())


def seed_plan(holder: m1.Owned) -> None:
    seed_plan_header(holder)
    with guarded(holder):
        insert(holder, STEPS, plan_step_row())
        insert(holder, STEPS, child_step_row())


def seed_runtime_run(
    holder: m1.Owned,
    *,
    definition_kind: str = "workflow",
    workflow_id: str = WORKFLOW_ID,
    workflow_version: str = WORKFLOW_VERSION,
    run_id: str = RUN_ID,
    job_id: str = m18.JOB_ID,
) -> None:
    m18.seed_job(holder, job_id=job_id)
    with guarded(holder):
        m18.insert_run(
            holder,
            run_id=run_id,
            job_id=job_id,
            definition_kind=definition_kind,
            definition_id=workflow_id,
            definition_version=workflow_version,
            created_at_us=BASE_US,
        )


def seed_workflow_run(holder: m1.Owned) -> None:
    seed_plan(holder)
    seed_runtime_run(holder)
    with guarded(holder):
        insert(holder, RUNS, workflow_run_row())


def seed_correlation(holder: m1.Owned) -> None:
    seed_workflow_run(holder)
    with guarded(holder):
        insert(holder, CORRELATIONS, correlation_row())


def seed_every_table(holder: m1.Owned) -> None:
    seed_correlation(holder)
    with guarded(holder):
        insert(holder, OBSERVATIONS, plan_observation_row())
        insert(holder, OBSERVATIONS, branch_observation_row())
        insert(holder, RESULTS, result_row())
        insert(holder, EVIDENCE, evidence_row())
        audit(holder, "audit-completion")
        insert(holder, COMPLETIONS, completion_row())


def test_0027_is_the_unique_consecutive_successor_to_0026() -> None:
    versions = [migration.version for migration in load_migrations()]
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


def test_schema_inventory_contains_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        assert len(TRIGGERS) == 24
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_integrity_and_foreign_keys_stay_clean_once_populated(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m1.Owned, table: str) -> None:
    row: dict[str, object]
    if table == PLANS:
        with guarded(owned):
            audit(owned, "audit-other-plan")
        row = plan_row(workflow_id="workflow-other", audit_ref="audit-other-plan")
    elif table == STEPS:
        seed_plan_header(owned)
        row = plan_step_row()
    elif table == RUNS:
        seed_plan(owned)
        seed_runtime_run(owned)
        row = workflow_run_row()
    elif table == OBSERVATIONS:
        seed_workflow_run(owned)
        row = plan_observation_row()
    elif table == CORRELATIONS:
        seed_workflow_run(owned)
        row = correlation_row()
    elif table == RESULTS:
        seed_correlation(owned)
        row = result_row()
    elif table == EVIDENCE:
        seed_workflow_run(owned)
        row = evidence_row()
    else:
        seed_workflow_run(owned)
        with guarded(owned):
            insert(owned, EVIDENCE, evidence_row())
            audit(owned, "audit-completion")
        row = completion_row()

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        insert(owned, table, row)


def test_hyphenated_workflow_identifiers_are_accepted(owned: m1.Owned) -> None:
    seed_workflow_run(owned)
    assert (
        owned.connection.execute(f"SELECT workflow_id FROM {PLANS}").fetchone()
        == (WORKFLOW_ID,)
    )
    assert (
        owned.connection.execute(f"SELECT step_id FROM {STEPS} ORDER BY sequence_index")
        .fetchall()[0]
        == (PLAN_STEP_ID,)
    )


def test_unknown_execution_route_and_branch_vocabularies_are_refused(
    owned: m1.Owned,
) -> None:
    seed_plan_header(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, STEPS, plan_step_row(execution_class="MAYBE"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, STEPS, plan_step_row(route="MAYBE"))

    with guarded(owned):
        insert(owned, STEPS, plan_step_row())
        insert(owned, STEPS, child_step_row())
    seed_runtime_run(owned)
    with guarded(owned):
        insert(owned, RUNS, workflow_run_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, OBSERVATIONS, branch_observation_row(branch_outcome="MAYBE"))


def test_workflow_run_binding_refuses_a_non_workflow_runtime_run(
    owned: m1.Owned,
) -> None:
    seed_plan(owned)
    seed_runtime_run(owned, definition_kind="agent_component")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="workflow"):
        insert(owned, RUNS, workflow_run_row())


def test_workflow_run_binding_refuses_definition_identity_mismatch(
    owned: m1.Owned,
) -> None:
    seed_plan(owned)
    seed_runtime_run(owned, workflow_id="workflow-other")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="name"):
        insert(owned, RUNS, workflow_run_row())


def test_plan_observations_are_replay_safe_and_conflict_checked(
    owned: m1.Owned,
) -> None:
    seed_workflow_run(owned)
    with guarded(owned):
        insert(owned, OBSERVATIONS, plan_observation_row())
        insert_or_ignore(owned, OBSERVATIONS, plan_observation_row())

    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS}").fetchone()
        == (1,)
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="conflicts"):
        insert_or_ignore(
            owned,
            OBSERVATIONS,
            plan_observation_row(route="WAIT", sequence_index=0),
        )


def test_branch_observations_are_replay_safe_and_conflict_checked(
    owned: m1.Owned,
) -> None:
    seed_workflow_run(owned)
    with guarded(owned):
        insert(owned, OBSERVATIONS, branch_observation_row())
        insert_or_ignore(owned, OBSERVATIONS, branch_observation_row())

    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS}").fetchone()
        == (1,)
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="conflicts"):
        insert_or_ignore(
            owned,
            OBSERVATIONS,
            branch_observation_row(
                branch_outcome="UNMATCHED",
                branch_reason="not-equals",
            ),
        )


def test_child_correlation_results_are_fenced_identity_bound_and_budgeted(
    owned: m1.Owned,
) -> None:
    seed_correlation(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="fence"):
        insert(owned, RESULTS, result_row(fence=2))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="identity"):
        insert(owned, RESULTS, result_row(child_workflow_id="workflow-other"))

    with guarded(owned):
        insert(owned, RESULTS, result_row(cost=4))

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="budget"):
        insert(owned, RESULTS, result_row(2, cost=7))

    with guarded(owned):
        insert(owned, RESULTS, result_row(2, outcome="closed", cost=None))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="closed"):
        insert(owned, RESULTS, result_row(3, cost=1))


def test_completion_without_evidence_is_refused(owned: m1.Owned) -> None:
    seed_workflow_run(owned)
    with guarded(owned):
        audit(owned, "audit-completion")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="evidence"):
        insert(owned, COMPLETIONS, completion_row())


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned,
    table: str,
) -> None:
    seed_every_table(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")
