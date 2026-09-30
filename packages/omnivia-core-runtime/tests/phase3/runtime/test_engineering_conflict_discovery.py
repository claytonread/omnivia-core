"""Durable engineering conflict discovery (migrations 0055-0056).

The suite covers atomic enqueue, indexed resumable processing, exact provenance,
authorization-safe structural and lexical matching, restart/replay, production
execution and expand visibility. Checkout-proven scope classification remains a
later AC-050 slice.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    fenced_transaction,
)
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.engineering_conflict_execution import (
    EngineeringConflictExecutor,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage import engineering_conflicts
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    read_workspace_state,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

from omnivia_core.contracts.v1 import MutationPrecondition

WORKSPACE_ID = esc.WORKSPACE_ID
MIGRATION_VERSION = 55
MIGRATION_NAME = "0055_engineering_conflict_discovery.sql"
TABLES = (
    "omnivia_engineering_discovery_runs",
    "omnivia_engineering_discovery_run_events",
    "omnivia_engineering_relation_candidates",
    "omnivia_engineering_discovery_candidate_observations",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = esc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _fenced(workspace: esc.Workspace) -> Any:
    return fenced_transaction(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    )


def _count(workspace: esc.Workspace, table: str) -> int:
    return int(
        workspace.holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )


def _run_for(workspace: esc.Workspace, record: dict[str, str]) -> engineering_conflicts.DiscoveryRun:
    row = workspace.holder.connection.execute(
        f"SELECT {engineering_conflicts._RUN_COLUMNS} "
        "FROM omnivia_engineering_discovery_runs "
        "WHERE workspace_id = ? AND anchor_record_id = ? AND anchor_version = ?",
        (WORKSPACE_ID, record["record_id"], record["version"]),
    ).fetchone()
    assert row is not None
    return engineering_conflicts._run(row)


def _assembly(workspace: esc.Workspace, record: dict[str, str]) -> str:
    row = workspace.holder.connection.execute(
        "SELECT assembly_id FROM omnivia_governed_version_assemblies "
        "WHERE workspace_id = ? AND governed_record_id = ? "
        "AND governed_record_version_id = ?",
        (WORKSPACE_ID, record["record_id"], record["version"]),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _grant(
    *,
    principal_id: str = esc.PRINCIPAL,
    all_labels: bool = True,
    labels: frozenset[str] = frozenset(),
) -> EvidenceLabelGrant:
    return EvidenceLabelGrant(
        principal_id=principal_id,
        workspace_id=WORKSPACE_ID,
        all_labels=all_labels,
        labels=labels,
    )


def _seed_candidate(
    workspace: esc.Workspace,
) -> tuple[
    engineering_conflicts.DiscoveryRun,
    engineering_conflicts.RelationCandidate,
]:
    earlier = workspace.observe(
        esc._observation(None, title="Provider A", evidence=False)
    )
    anchor = workspace.observe(
        esc._observation(None, title="Provider B", evidence=False)
    )
    run = _run_for(workspace, anchor)
    with _fenced(workspace):
        candidate = engineering_conflicts._append_relation_candidate(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            other_assembly_id=_assembly(workspace, earlier),
            label_grant=_grant(),
            allocate_identifier=lambda prefix: f"{prefix}-focused",
        )
        engineering_conflicts._append_candidate_observation(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            candidate=candidate,
            selected_order=1,
            channel="structural",
            score=1,
            basis={"shared_topic": True},
            recorded_at_us=run.resolution_instant_us,
        )
    return run, candidate


def _identifier_allocator() -> Any:
    sequence = itertools.count(1)
    return lambda prefix: f"{prefix}-processor-{next(sequence)}"


def _discovery_observation(
    marker: str,
    *,
    topic: str | None = None,
    repository_id: str | None = None,
    snapshot_id: str | None = None,
    evidence: bool = False,
) -> dict[str, Any]:
    payload = esc._observation(None, title=f"{marker} title", evidence=evidence)
    payload["content"].update(
        {
            "summary": f"{marker} summary",
            "what": f"{marker} finding",
        }
    )
    if topic is not None:
        payload["content"]["topic_ref"] = {"proposed_key": topic}
    if repository_id is not None:
        applicability: dict[str, str] = {"repository_id": repository_id}
        if snapshot_id is not None:
            applicability["snapshot_id"] = snapshot_id
        payload["content"]["applicability"] = applicability
    return payload


def _finish_before(
    workspace: esc.Workspace,
    target: engineering_conflicts.DiscoveryRun,
    *,
    allocate_identifier: Any,
    label_grant: EvidenceLabelGrant | None = None,
) -> None:
    grant = _grant() if label_grant is None else label_grant
    for _ in range(256):
        queued = engineering_conflicts.read_oldest_queued_run(
            workspace.holder.connection, workspace_id=WORKSPACE_ID
        )
        assert queued is not None
        if queued.discovery_run_id == target.discovery_run_id:
            return
        result = engineering_conflicts.process_oldest_queued_run(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
            label_grant=grant,
            allocate_identifier=allocate_identifier,
            occurred_at_us=2**62,
        )
        assert result is not None
    raise AssertionError("the target discovery run did not reach the queue head")


def _finish_run(
    workspace: esc.Workspace,
    target: engineering_conflicts.DiscoveryRun,
    *,
    allocate_identifier: Any,
    scan_record_budget: int = engineering_conflicts.DEFAULT_SCAN_RECORD_BUDGET,
    label_grant: EvidenceLabelGrant | None = None,
) -> engineering_conflicts.DiscoveryProcessingResult:
    grant = _grant() if label_grant is None else label_grant
    for _ in range(1024):
        result = engineering_conflicts.process_oldest_queued_run(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
            label_grant=grant,
            allocate_identifier=allocate_identifier,
            occurred_at_us=2**62,
            scan_record_budget=scan_record_budget,
        )
        assert result is not None
        if (
            result.run.discovery_run_id == target.discovery_run_id
            and result.scan_complete
        ):
            return result
    raise AssertionError("the target discovery run did not complete")


def test_0055_is_the_additive_successor_and_creates_guarded_tables(
    workspace: esc.Workspace,
) -> None:
    migrations = load_migrations()
    migration = next(item for item in migrations if item.version == MIGRATION_VERSION)
    assert migration.name == MIGRATION_NAME
    assert migrations[migrations.index(migration) - 1].version == 54
    assert "INSERT INTO omnivia_governed" not in migration.sql
    assert "UPDATE omnivia_governed" not in migration.sql
    assert applied_migrations(workspace.holder.connection)[55] == migration.checksum
    present = {
        str(row[0])
        for row in workspace.holder.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
    assert set(TABLES) <= present
    assert_guards_intact(workspace.holder.connection)
    assert fingerprint_schema(workspace.holder.connection).matches(
        canonical_schema_fingerprint()
    )
    assert integrity_check(workspace.holder.connection) == []
    assert foreign_key_check(workspace.holder.connection) == []


def test_candidate_lookup_indexes_follow_the_assembly_id_read_path(
    workspace: esc.Workspace,
) -> None:
    connection = workspace.holder.connection
    plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT relation_candidate_id "
        "FROM omnivia_engineering_relation_candidates "
        "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_a "
        "WHERE workspace_id = ? AND endpoint_a_assembly_id = ? "
        "AND status = 'pending' AND endpoint_b_assembly_id IN (?) "
        "UNION ALL SELECT relation_candidate_id "
        "FROM omnivia_engineering_relation_candidates "
        "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_b "
        "WHERE workspace_id = ? AND endpoint_b_assembly_id = ? "
        "AND status = 'pending' AND endpoint_a_assembly_id IN (?)",
        (
            WORKSPACE_ID,
            "asm-anchor",
            "asm-other",
            WORKSPACE_ID,
            "asm-anchor",
            "asm-other",
        ),
    ).fetchall()
    detail = "\n".join(str(row[3]) for row in plan)
    assert "omnivia_idx_engineering_relation_candidates_endpoint_a" in detail
    assert "omnivia_idx_engineering_relation_candidates_endpoint_b" in detail

    failure_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM omnivia_engineering_relation_candidates "
        "INDEXED BY omnivia_idx_engineering_relation_candidates_first_run "
        "WHERE workspace_id = ? AND first_discovery_run_id = ?",
        (WORKSPACE_ID, "edr-plan"),
    ).fetchall()
    assert any(
        "omnivia_idx_engineering_relation_candidates_first_run" in str(row[3])
        for row in failure_plan
    )


def test_memory_create_atomically_enqueues_exact_engineering_versions(
    workspace: esc.Workspace,
) -> None:
    claim = esc._observation(None, evidence=False)
    first = workspace.ok("memory.create", claim, key="conflict-create")
    replay = workspace.ok("memory.create", claim, key="conflict-create")
    assert replay == first
    identity = first["record"]["provenance"]["identity"]
    row = workspace.holder.connection.execute(
        "SELECT r.anchor_record_id, r.anchor_version, r.anchor_content_digest, "
        "r.principal_id, r.detector_version, r.candidate_budget, e.state "
        "FROM omnivia_engineering_discovery_runs r "
        "JOIN omnivia_engineering_discovery_run_events e "
        "ON e.workspace_id = r.workspace_id AND e.discovery_run_id = r.discovery_run_id "
        "WHERE e.event_sequence = 1"
    ).fetchone()
    assert row is not None
    assert tuple(row[:2]) == (identity["record_id"], identity["version"])
    assert str(row[2]).startswith("sha256:")
    assert tuple(row[3:]) == (
        esc.PRINCIPAL,
        engineering_conflicts.DETECTOR_VERSION,
        engineering_conflicts.DEFAULT_CANDIDATE_BUDGET,
        "queued",
    )
    oldest = engineering_conflicts.read_oldest_queued_run(
        workspace.holder.connection, workspace_id=WORKSPACE_ID
    )
    assert oldest is not None
    assert (oldest.anchor_record_id, oldest.anchor_version) == (
        identity["record_id"],
        identity["version"],
    )
    assert _count(workspace, TABLES[0]) == _count(workspace, TABLES[1]) == 1


def test_non_engineering_versions_do_not_enqueue(workspace: esc.Workspace) -> None:
    fact = {
        **esc._observation(None, evidence=False),
        "record_type": "memory.fact",
        "domain_scope": "engineering.codebase",
        "content": {"fact": "An engineering-domain fact is not an observation"},
    }
    workspace.observe(fact)
    assert _count(workspace, TABLES[0]) == _count(workspace, TABLES[1]) == 0


@pytest.mark.parametrize("budget", (0, 33))
def test_enqueue_rejects_out_of_range_budgets(
    workspace: esc.Workspace, budget: int
) -> None:
    record = workspace.observe(esc._observation(None, evidence=False))
    with pytest.raises(ValueError, match="between 1 and 32"):
        engineering_conflicts.enqueue_discovery(
            workspace.holder.connection,
            # The budget is rejected before the existing run is read.
            settlement=MutationSettlementContext("unused", "unused", "unused", 1),
            workspace_id=WORKSPACE_ID,
            assembly_id=_assembly(workspace, record),
            allocate_identifier=lambda prefix: prefix,
            candidate_budget=budget,
        )


def test_enqueue_rejects_boolean_budget(workspace: esc.Workspace) -> None:
    record = workspace.observe(esc._observation(None, evidence=False))
    with pytest.raises(TypeError, match="must be an integer"):
        engineering_conflicts.enqueue_discovery(
            workspace.holder.connection,
            settlement=MutationSettlementContext("unused", "unused", "unused", 1),
            workspace_id=WORKSPACE_ID,
            assembly_id=_assembly(workspace, record),
            allocate_identifier=lambda prefix: prefix,
            candidate_budget=True,
        )


def test_enqueue_failure_rolls_back_the_whole_memory_mutation(
    workspace: esc.Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = workspace.holder.connection
    before = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "omnivia_application_audit_events",
            "omnivia_governed_records",
            "omnivia_governed_version_assemblies",
            "omnivia_engineering_preview_projection",
            *TABLES,
        )
    }

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("forced discovery enqueue failure")

    monkeypatch.setattr(engineering_conflicts, "enqueue_discovery", fail)
    with pytest.raises(RuntimeError, match="forced discovery"):
        workspace.call(
            "memory.create",
            esc._observation(None, evidence=False),
            key="conflict-rollback",
        )
    after = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in before
    }
    assert after == before


def test_governance_enqueues_proposed_and_accepted_versions_but_not_rejected(
    workspace: esc.Workspace,
) -> None:
    created = workspace.observe(esc._observation(None, evidence=False))
    proposed_result = workspace.ok(
        "knowledge.propose",
        {"record_id": created["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=created["version"]),
    )
    proposed_identity = proposed_result["updated_record"]["provenance"]["identity"]
    proposed = {
        "record_id": proposed_identity["record_id"],
        "version": proposed_identity["version"],
    }
    accepted_result = workspace.ok(
        "candidate.approve",
        {"record_id": proposed["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=proposed["version"]),
    )
    assert accepted_result["updated_record"]["provenance"]["identity"]["version"]
    assert _count(workspace, TABLES[0]) == 3

    rejected_source = workspace.observe(
        esc._observation(None, title="Rejected provider", evidence=False)
    )
    rejected_proposal = workspace.ok(
        "knowledge.propose",
        {
            "record_id": rejected_source["record_id"],
            "rationale": {"reason_code": "review"},
        },
        mutation_precondition=MutationPrecondition(
            record_version=rejected_source["version"]
        ),
    )
    rejected_version = rejected_proposal["updated_record"]["provenance"]["identity"][
        "version"
    ]
    before_reject = _count(workspace, TABLES[0])
    workspace.ok(
        "candidate.reject",
        {
            "record_id": rejected_source["record_id"],
            "rationale": {"reason_code": "review"},
        },
        mutation_precondition=MutationPrecondition(record_version=rejected_version),
    )
    assert _count(workspace, TABLES[0]) == before_reject


def test_record_supersede_does_not_enqueue_its_ineligible_memory_fact(
    workspace: esc.Workspace,
) -> None:
    fact = {
        **esc._observation(None, title="Provider fact", evidence=False),
        "record_type": "memory.fact",
        "content": {"fact": "Provider A is active."},
    }
    accepted = esc._accept(workspace, workspace.observe(fact))
    before = _count(workspace, TABLES[0])
    replacement = {**fact, "content": {"fact": "Provider B is active."}}
    superseding = esc._supersede(workspace, accepted, replacement)
    assert _count(workspace, TABLES[0]) == before == 0
    assert workspace.holder.connection.execute(
        "SELECT 1 FROM omnivia_engineering_discovery_runs "
        "WHERE workspace_id = ? AND anchor_record_id = ? AND anchor_version = ?",
        (WORKSPACE_ID, superseding["record_id"], superseding["version"]),
    ).fetchone() is None


def test_candidate_is_canonical_deduplicated_and_visible_only_with_both_endpoints(
    workspace: esc.Workspace,
) -> None:
    run, candidate = _seed_candidate(workspace)
    assert (candidate.endpoint_a.record_id, candidate.endpoint_a.version) < (
        candidate.endpoint_b.record_id,
        candidate.endpoint_b.version,
    )
    with _fenced(workspace):
        duplicate = engineering_conflicts._append_relation_candidate(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            other_assembly_id=(
                candidate.endpoint_b.assembly_id
                if run.anchor_assembly_id == candidate.endpoint_a.assembly_id
                else candidate.endpoint_a.assembly_id
            ),
            label_grant=_grant(),
            allocate_identifier=lambda prefix: f"{prefix}-unused",
        )
    assert duplicate.relation_candidate_id == candidate.relation_candidate_id
    assert _count(workspace, TABLES[2]) == _count(workspace, TABLES[3]) == 1

    visible = engineering_conflicts.read_authorized_relation_candidates(
        workspace.holder.connection,
        workspace_id=WORKSPACE_ID,
        anchor_assembly_id=run.anchor_assembly_id,
        resolution_instant_us=2**62,
        view="candidates",
        label_grant=_grant(),
    )
    assert [item.relation_candidate_id for item in visible] == [
        candidate.relation_candidate_id
    ]


def test_hidden_endpoint_is_not_selected_from_the_relation_table(
    workspace: esc.Workspace,
) -> None:
    hidden = workspace.observe(
        esc._observation(None, title="Hidden provider", evidence=True)
    )
    anchor = workspace.observe(
        esc._observation(None, title="Open provider", evidence=False)
    )
    run = _run_for(workspace, anchor)
    with _fenced(workspace):
        engineering_conflicts._append_relation_candidate(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            other_assembly_id=_assembly(workspace, hidden),
            label_grant=_grant(),
            allocate_identifier=lambda prefix: f"{prefix}-hidden",
        )

    statements: list[str] = []
    workspace.holder.connection.set_trace_callback(statements.append)
    try:
        visible = engineering_conflicts.read_authorized_relation_candidates(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            anchor_assembly_id=run.anchor_assembly_id,
            resolution_instant_us=2**62,
            view="candidates",
            label_grant=_grant(principal_id="reader", all_labels=False),
        )
    finally:
        workspace.holder.connection.set_trace_callback(None)
    assert visible == ()
    assert not any(
        "FROM omnivia_engineering_relation_candidates" in statement
        for statement in statements
    )


def test_denied_endpoint_cannot_be_persisted(workspace: esc.Workspace) -> None:
    hidden = workspace.observe(
        esc._observation(None, title="Hidden provider", evidence=True)
    )
    anchor = workspace.observe(
        esc._observation(None, title="Open provider", evidence=False)
    )
    run = _run_for(workspace, anchor)
    with pytest.raises(StorageError, match="authorized frontier"), _fenced(workspace):
        engineering_conflicts._append_relation_candidate(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            other_assembly_id=_assembly(workspace, hidden),
            label_grant=_grant(all_labels=False),
            allocate_identifier=lambda prefix: f"{prefix}-denied",
        )
    assert _count(workspace, TABLES[2]) == 0


def test_rejected_endpoint_cannot_be_persisted(workspace: esc.Workspace) -> None:
    rejected = workspace.observe(
        esc._observation(None, title="Rejected provider", evidence=False)
    )
    proposal = workspace.ok(
        "knowledge.propose",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=rejected["version"]),
    )
    proposed_version = proposal["updated_record"]["provenance"]["identity"]["version"]
    rejected_result = workspace.ok(
        "candidate.reject",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=proposed_version),
    )
    rejected_identity = rejected_result["updated_record"]["provenance"]["identity"]
    rejected_assembly = _assembly(
        workspace,
        {
            "record_id": str(rejected_identity["record_id"]),
            "version": str(rejected_identity["version"]),
        },
    )
    anchor = workspace.observe(
        esc._observation(None, title="Current provider", evidence=False)
    )
    run = _run_for(workspace, anchor)
    with pytest.raises(StorageError, match="authorized frontier"), _fenced(workspace):
        engineering_conflicts._append_relation_candidate(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            other_assembly_id=rejected_assembly,
            label_grant=_grant(),
            allocate_identifier=lambda prefix: f"{prefix}-rejected",
        )
    assert _count(workspace, TABLES[2]) == 0


def test_terminal_events_match_durable_observations_and_coverage(
    workspace: esc.Workspace,
) -> None:
    run, _candidate = _seed_candidate(workspace)
    digest = "sha256:" + "a" * 64

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="completed",
            coverage="complete",
            frontier_digest=digest,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=1,
            failure_code=None,
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-bad-coverage",
        )

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="completed",
            coverage="partial",
            frontier_digest=digest,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=0,
            failure_code=None,
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-bad-count",
        )

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="failed",
            coverage="partial",
            frontier_digest=digest,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=0,
            failure_code="worker_failed",
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-bad-failure",
        )

    with _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="completed",
            coverage="partial",
            frontier_digest=digest,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=1,
            failure_code=None,
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-complete",
        )

    unscanned = workspace.observe(
        esc._observation(None, title="Unscanned provider", evidence=False)
    )
    unscanned_run = _run_for(workspace, unscanned)
    with _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=unscanned_run,
            state="failed",
            coverage="not_scanned",
            frontier_digest=None,
            authorized_frontier_size=0,
            structural_considered=0,
            lexical_considered=0,
            selected_count=0,
            failure_code="worker_failed",
            occurred_at_us=unscanned_run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-failed",
        )

    assert workspace.holder.connection.execute(
        "SELECT state, coverage, selected_count FROM "
        "omnivia_engineering_discovery_run_events "
        "WHERE workspace_id = ? AND discovery_run_id = ? AND event_sequence = 2",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == ("completed", "partial", 1)
    assert workspace.holder.connection.execute(
        "SELECT state, coverage, frontier_digest, selected_count FROM "
        "omnivia_engineering_discovery_run_events "
        "WHERE workspace_id = ? AND discovery_run_id = ? AND event_sequence = 2",
        (WORKSPACE_ID, unscanned_run.discovery_run_id),
    ).fetchone() == ("failed", "not_scanned", None, 0)


def test_complete_snapshot_requires_and_records_its_frontier_digest(
    workspace: esc.Workspace,
) -> None:
    run, _candidate = _seed_candidate(workspace)
    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="completed",
            coverage="scan_complete_for_snapshot",
            frontier_digest=None,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=1,
            failure_code=None,
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-missing-digest",
        )

    digest = "sha256:" + "b" * 64
    with _fenced(workspace):
        engineering_conflicts._append_terminal_event(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            run=run,
            state="completed",
            coverage="scan_complete_for_snapshot",
            frontier_digest=digest,
            authorized_frontier_size=2,
            structural_considered=1,
            lexical_considered=0,
            selected_count=1,
            failure_code=None,
            occurred_at_us=run.resolution_instant_us,
            allocate_identifier=lambda prefix: f"{prefix}-complete-snapshot",
        )
    assert workspace.holder.connection.execute(
        "SELECT coverage, frontier_digest FROM "
        "omnivia_engineering_discovery_run_events "
        "WHERE workspace_id = ? AND discovery_run_id = ? AND event_sequence = 2",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == ("scan_complete_for_snapshot", digest)


def test_candidate_and_state_guards_fail_closed(workspace: esc.Workspace) -> None:
    run, candidate = _seed_candidate(workspace)
    connection = workspace.holder.connection

    for table in TABLES:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(f"INSERT INTO {table} SELECT * FROM {table}")

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_discovery_runs "
            "SELECT workspace_id, 'edr-invalid-budget', anchor_assembly_id, "
            "anchor_record_id, anchor_version, anchor_content_digest, principal_id, "
            "'engineering.conflict.invalid', 33, resolution_instant_us, enqueued_at_us, "
            "audit_ref FROM omnivia_engineering_discovery_runs "
            "WHERE discovery_run_id = ?",
            (run.discovery_run_id,),
        )

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_candidates "
            "SELECT workspace_id, 'erc-reversed', endpoint_b_assembly_id, "
            "endpoint_b_record_id, endpoint_b_version, endpoint_b_digest, "
            "endpoint_a_assembly_id, endpoint_a_record_id, endpoint_a_version, "
            "endpoint_a_digest, detector_version, scope_classification, "
            "proposed_relation, status, first_discovery_run_id, recorded_at_us "
            "FROM omnivia_engineering_relation_candidates "
            "WHERE relation_candidate_id = ?",
            (candidate.relation_candidate_id,),
        )

    with pytest.raises(sqlite3.DatabaseError, match="exact sealed"), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_candidates "
            "SELECT workspace_id, 'erc-bad-digest', endpoint_a_assembly_id, "
            "endpoint_a_record_id, endpoint_a_version, 'sha256:' || printf('%064d', 0), "
            "endpoint_b_assembly_id, endpoint_b_record_id, endpoint_b_version, "
            "endpoint_b_digest, detector_version, scope_classification, "
            "proposed_relation, status, first_discovery_run_id, recorded_at_us "
            "FROM omnivia_engineering_relation_candidates "
            "WHERE relation_candidate_id = ?",
            (candidate.relation_candidate_id,),
        )

    with pytest.raises(sqlite3.DatabaseError, match="trusted checkout"), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_candidates "
            "SELECT workspace_id, 'erc-scoped', endpoint_a_assembly_id, "
            "endpoint_a_record_id, endpoint_a_version, endpoint_a_digest, "
            "endpoint_b_assembly_id, endpoint_b_record_id, endpoint_b_version, "
            "endpoint_b_digest, detector_version, 'scoped_difference', "
            "'scoped_difference', status, first_discovery_run_id, recorded_at_us "
            "FROM omnivia_engineering_relation_candidates "
            "WHERE relation_candidate_id = ?",
            (candidate.relation_candidate_id,),
        )

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_discovery_run_events "
            "(workspace_id, discovery_run_id, event_sequence, event_id, state, "
            "coverage, frontier_digest, authorized_frontier_size, "
            "structural_considered, lexical_considered, selected_count, failure_code, "
            "occurred_at_us) VALUES (?, ?, 2, 'ede-invalid', 'queued', NULL, NULL, "
            "NULL, NULL, NULL, NULL, NULL, ?)",
            (WORKSPACE_ID, run.discovery_run_id, run.resolution_instant_us),
        )

    for table in TABLES:
        for statement in (
            f"UPDATE {table} SET workspace_id = workspace_id",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
                connection.execute(statement)


def test_discovery_facts_survive_restart(workspace: esc.Workspace) -> None:
    run, candidate = _seed_candidate(workspace)
    workspace.restart()
    assert workspace.holder.connection.execute(
        "SELECT state FROM omnivia_engineering_discovery_run_events "
        "WHERE discovery_run_id = ? AND event_sequence = 1",
        (run.discovery_run_id,),
    ).fetchone() == ("queued",)
    assert workspace.holder.connection.execute(
        "SELECT endpoint_a_digest, endpoint_b_digest "
        "FROM omnivia_engineering_relation_candidates "
        "WHERE relation_candidate_id = ?",
        (candidate.relation_candidate_id,),
    ).fetchone() == (
        candidate.endpoint_a.content_digest,
        candidate.endpoint_b.content_digest,
    )


def test_0056_adds_an_indexed_append_only_scan_watermark(
    workspace: esc.Workspace,
) -> None:
    migration = next(item for item in load_migrations() if item.version == 56)
    assert migration.name == "0056_engineering_conflict_scan_progress.sql"
    assert applied_migrations(workspace.holder.connection)[56] == migration.checksum
    plan = workspace.holder.connection.execute(
        "EXPLAIN QUERY PLAN SELECT governed_record_id "
        "FROM omnivia_governed_records "
        "INDEXED BY omnivia_idx_engineering_discovery_record_scan "
        "WHERE workspace_id = ? AND domain_scope = ? AND governed_record_id > ? "
        "AND record_type IN (?, ?, ?) AND recorded_at_us <= ? "
        "ORDER BY governed_record_id LIMIT ?",
        (
            WORKSPACE_ID,
            "engineering.codebase",
            "",
            "knowledge.finding",
            "knowledge.risk",
            "knowledge.decision",
            2**62,
            129,
        ),
    ).fetchall()
    assert any(
        "omnivia_idx_engineering_discovery_record_scan" in str(row[3]) for row in plan
    )


def test_frontier_digest_is_independent_of_page_boundaries_and_empty_pages() -> None:
    first = engineering_conflicts.PreviewCandidate(
        "asm-a",
        "rec-a",
        "ver-a",
        1,
        "candidate",
        "unavailable",
        False,
        "sha256:" + "a" * 64,
        "Alpha",
        "Alpha preview",
        False,
        "decision",
        "observed",
        "topic.alpha",
        None,
        None,
    )
    second = engineering_conflicts.PreviewCandidate(
        "asm-b",
        "rec-b",
        "ver-b",
        2,
        "candidate",
        "unavailable",
        False,
        "sha256:" + "b" * 64,
        "Beta",
        "Beta preview",
        False,
        "decision",
        "observed",
        None,
        None,
        None,
    )
    dependencies = {"asm-a": (), "asm-b": ()}
    together = engineering_conflicts._frontier_digest(
        (first, second), dependencies, previous_digest=None
    )
    split = engineering_conflicts._frontier_digest(
        (first,), dependencies, previous_digest=None
    )
    split = engineering_conflicts._frontier_digest((), {}, previous_digest=split)
    split = engineering_conflicts._frontier_digest(
        (second,), dependencies, previous_digest=split
    )
    assert split == together


def test_resumable_processor_prefers_structural_then_authorized_lexical_matches(
    workspace: esc.Workspace,
) -> None:
    structural = workspace.observe(
        _discovery_observation(
            "cobalt lattice",
            topic="auth.provider",
            repository_id="erepo-a",
            snapshot_id="esnap-a",
        )
    )
    lexical = workspace.observe(_discovery_observation("quasarbridge fallback"))
    unrelated = workspace.observe(
        _discovery_observation(
            "unrelated telemetry",
            repository_id="erepo-a",
            snapshot_id="esnap-a",
        )
    )
    disjoint = workspace.observe(
        _discovery_observation(
            "quasarbridge remote",
            repository_id="erepo-b",
            snapshot_id="esnap-b",
        )
    )
    anchor = workspace.observe(
        _discovery_observation(
            "quasarbridge amber",
            topic="auth.provider",
            repository_id="erepo-a",
            snapshot_id="esnap-a",
        )
    )
    run = _run_for(workspace, anchor)
    allocate = _identifier_allocator()
    _finish_before(workspace, run, allocate_identifier=allocate)

    first_page = engineering_conflicts.process_oldest_queued_run(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
        label_grant=_grant(),
        allocate_identifier=allocate,
        occurred_at_us=2**62,
        scan_record_budget=1,
    )
    assert first_page is not None
    assert first_page.coverage == "partial"
    assert first_page.scan_complete is False
    first_cursor = first_page.last_processed_record_id

    workspace.restart()
    completed = _finish_run(
        workspace,
        run,
        allocate_identifier=allocate,
        scan_record_budget=1,
    )
    assert completed.coverage == "scan_complete_for_snapshot"
    assert completed.authorized_frontier_size == 5
    assert completed.structural_considered == 4
    assert completed.lexical_considered == 2
    assert completed.last_processed_record_id > first_cursor

    observations = workspace.holder.connection.execute(
        "SELECT o.channel, o.selected_order, c.endpoint_a_record_id, "
        "c.endpoint_b_record_id FROM omnivia_engineering_discovery_candidate_observations o "
        "JOIN omnivia_engineering_relation_candidates c "
        "ON c.workspace_id = o.workspace_id "
        "AND c.relation_candidate_id = o.relation_candidate_id "
        "WHERE o.workspace_id = ? AND o.discovery_run_id = ? "
        "ORDER BY o.selected_order",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchall()
    assert [str(row[0]) for row in observations] == ["structural", "lexical"]
    named = {str(value) for row in observations for value in row[2:]}
    assert {anchor["record_id"], structural["record_id"], lexical["record_id"]} <= named
    assert unrelated["record_id"] not in named
    assert disjoint["record_id"] not in named
    assert workspace.holder.connection.execute(
        "SELECT coverage, frontier_digest, authorized_frontier_size, "
        "structural_considered, lexical_considered, selected_count "
        "FROM omnivia_engineering_discovery_run_events "
        "WHERE workspace_id = ? AND discovery_run_id = ? AND event_sequence = 2",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == (
        "scan_complete_for_snapshot",
        completed.frontier_digest,
        5,
        4,
        2,
        2,
    )
    progress = workspace.holder.connection.execute(
        "SELECT batch_sequence, cursor_record_id "
        "FROM omnivia_engineering_discovery_scan_progress "
        "WHERE workspace_id = ? AND discovery_run_id = ? ORDER BY batch_sequence",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchall()
    assert [int(row[0]) for row in progress] == list(range(1, 6))
    cursors = [str(row[1]) for row in progress]
    assert cursors == sorted(cursors)
    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        workspace.holder.connection.execute(
            "UPDATE omnivia_engineering_discovery_scan_progress "
            "SET cursor_record_id = cursor_record_id WHERE workspace_id = ? "
            "AND discovery_run_id = ?",
            (WORKSPACE_ID, run.discovery_run_id),
        )


def test_processor_excludes_hidden_rejected_and_off_domain_lexical_inputs(
    workspace: esc.Workspace,
) -> None:
    hidden = workspace.observe(
        _discovery_observation("nebulaindex hidden", evidence=True)
    )
    rejected = workspace.observe(_discovery_observation("nebulaindex rejected"))
    proposal = workspace.ok(
        "knowledge.propose",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=rejected["version"]),
    )
    workspace.ok(
        "candidate.reject",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(
            record_version=proposal["updated_record"]["provenance"]["identity"]["version"]
        ),
    )
    off_domain_payload = {
        **esc._observation(None, evidence=False),
        "record_type": "memory.fact",
        "domain_scope": "workspace.notes",
        "content": {"fact": "nebulaindex off domain"},
    }
    off_domain = workspace.observe(off_domain_payload)
    visible = workspace.observe(_discovery_observation("nebulaindex visible"))
    anchor = workspace.observe(_discovery_observation("nebulaindex anchor"))
    run = _run_for(workspace, anchor)
    grant = _grant(all_labels=False)
    allocate = _identifier_allocator()
    _finish_before(
        workspace,
        run,
        allocate_identifier=allocate,
        label_grant=grant,
    )
    completed = _finish_run(
        workspace,
        run,
        allocate_identifier=allocate,
        label_grant=grant,
    )
    assert len(completed.candidates) == 1
    endpoint_ids = {
        completed.candidates[0].endpoint_a.record_id,
        completed.candidates[0].endpoint_b.record_id,
    }
    assert endpoint_ids == {visible["record_id"], anchor["record_id"]}
    serialized = json.dumps(
        workspace.holder.connection.execute(
            "SELECT c.endpoint_a_record_id, c.endpoint_b_record_id, o.basis_json "
            "FROM omnivia_engineering_discovery_candidate_observations o "
            "JOIN omnivia_engineering_relation_candidates c "
            "ON c.workspace_id = o.workspace_id "
            "AND c.relation_candidate_id = o.relation_candidate_id "
            "WHERE o.workspace_id = ? AND o.discovery_run_id = ?",
            (WORKSPACE_ID, run.discovery_run_id),
        ).fetchall()
    )
    for denied in (hidden, rejected, off_domain):
        assert denied["record_id"] not in serialized


def test_default_result_budget_caps_global_structural_matches_at_eight(
    workspace: esc.Workspace,
) -> None:
    assert (
        engineering_conflicts._candidate_budget(
            engineering_conflicts.MAX_CANDIDATE_BUDGET
        )
        == 32
    )
    records = [
        workspace.observe(
            _discovery_observation(f"unique candidate {index}", topic="budget.topic")
        )
        for index in range(9)
    ]
    anchor = workspace.observe(
        _discovery_observation("budget anchor", topic="budget.topic")
    )
    run = _run_for(workspace, anchor)
    assert run.candidate_budget == engineering_conflicts.DEFAULT_CANDIDATE_BUDGET == 8
    allocate = _identifier_allocator()
    _finish_before(workspace, run, allocate_identifier=allocate)
    completed = _finish_run(workspace, run, allocate_identifier=allocate)
    assert completed.structural_considered == 9
    assert len(completed.candidates) == 8
    assert workspace.holder.connection.execute(
        "SELECT selected_count FROM omnivia_engineering_discovery_run_events "
        "WHERE workspace_id = ? AND discovery_run_id = ? AND event_sequence = 2",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == (8,)
    selected_other_ids = {
        endpoint.record_id
        for candidate in completed.candidates
        for endpoint in (candidate.endpoint_a, candidate.endpoint_b)
        if endpoint.record_id != anchor["record_id"]
    }
    assert len(selected_other_ids) == 8
    assert selected_other_ids < {record["record_id"] for record in records}
    for candidate in completed.candidates:
        for endpoint in (candidate.endpoint_a, candidate.endpoint_b):
            assert workspace.holder.connection.execute(
                "SELECT governed_record_id, governed_record_version_id, content_digest "
                "FROM omnivia_governed_version_assemblies "
                "WHERE workspace_id = ? AND assembly_id = ?",
                (WORKSPACE_ID, endpoint.assembly_id),
            ).fetchone() == (
                endpoint.record_id,
                endpoint.version,
                endpoint.content_digest,
            )


def test_terminal_failure_rolls_back_the_page_and_retry_replays_once(
    workspace: esc.Workspace,
) -> None:
    workspace.observe(_discovery_observation("rollback earlier", topic="rollback.topic"))
    anchor = workspace.observe(
        _discovery_observation("rollback anchor", topic="rollback.topic")
    )
    run = _run_for(workspace, anchor)
    allocate = _identifier_allocator()
    _finish_before(workspace, run, allocate_identifier=allocate)

    def fail_terminal(prefix: str) -> str:
        if prefix == "ede":
            raise RuntimeError("forced terminal interruption")
        return allocate(prefix)

    with pytest.raises(RuntimeError, match="terminal interruption"):
        engineering_conflicts.process_oldest_queued_run(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
            label_grant=_grant(),
            allocate_identifier=fail_terminal,
            occurred_at_us=2**62,
        )
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_discovery_scan_progress "
        "WHERE workspace_id = ? AND discovery_run_id = ?",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == (0,)
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_discovery_candidate_observations "
        "WHERE workspace_id = ? AND discovery_run_id = ?",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == (0,)

    workspace.restart()
    completed = _finish_run(workspace, run, allocate_identifier=allocate)
    assert len(completed.candidates) == 1
    assert (
        engineering_conflicts.process_oldest_queued_run(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
            label_grant=_grant(),
            allocate_identifier=allocate,
            occurred_at_us=2**62,
        )
        is None
    )
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_discovery_candidate_observations "
        "WHERE workspace_id = ? AND discovery_run_id = ?",
        (WORKSPACE_ID, run.discovery_run_id),
    ).fetchone() == (1,)


def test_service_executor_reaches_terminal_runs_and_expand_filters_endpoints(
    workspace: esc.Workspace,
) -> None:
    hidden = workspace.observe(
        _discovery_observation(
            "hidden wording", topic="executor.topic", evidence=True
        )
    )
    visible = workspace.observe(
        _discovery_observation("visible wording", topic="executor.topic")
    )
    anchor = workspace.observe(
        _discovery_observation("anchor wording", topic="executor.topic")
    )
    workspace.restart()
    executor = EngineeringConflictExecutor(
        connection=workspace.holder.connection,
        identity=workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
        clock=SystemClock(),
    )
    advanced = executor.run_pending(budget=32, scan_record_budget=1)
    assert advanced
    assert (
        engineering_conflicts.read_oldest_queued_run(
            workspace.holder.connection, workspace_id=WORKSPACE_ID
        )
        is None
    )

    owner = workspace.ok("engineering.expand", {"anchor": anchor})
    pending = [edge for edge in owner["edges"] if edge["status"] == "pending"]
    assert len(pending) == 2
    assert {node["record_id"] for node in owner["nodes"]} == {
        anchor["record_id"],
        hidden["record_id"],
        visible["record_id"],
    }

    reader = workspace.ok(
        "engineering.expand", {"anchor": anchor}, session=esc._reader()
    )
    assert [edge["status"] for edge in reader["edges"]] == ["pending"]
    assert hidden["record_id"] not in json.dumps(reader)
    assert {node["record_id"] for node in reader["nodes"]} == {
        anchor["record_id"],
        visible["record_id"],
    }

    node_capped = workspace.ok(
        "engineering.expand", {"anchor": anchor, "node_limit": 1}
    )
    assert node_capped["nodes"] == [anchor]
    assert node_capped["edges"] == []
    assert node_capped["truncated"] is True
    edge_capped = workspace.ok(
        "engineering.expand",
        {"anchor": anchor, "node_limit": 3, "edge_limit": 1},
    )
    assert len(edge_capped["edges"]) == 1
    assert edge_capped["truncated"] is True


def test_0055_does_not_backfill_old_versions_and_the_next_write_enqueues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    with (
        monkeypatch.context() as patched,
        m2.migration_catalogue_through(MIGRATION_VERSION - 1),
    ):
        patched.setattr(
            engineering_conflicts, "enqueue_discovery", lambda *_args, **_kwargs: None
        )
        old = esc.Workspace(old_dir)
        old.observe(
            esc._observation(None, title="Pre-migration provider", evidence=False)
        )
        assert 55 not in applied_migrations(old.holder.connection)
        old.holder.connection.close()

    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(old.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert [migration.version for migration in applied] == [MIGRATION_VERSION]
            assert maintenance.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_discovery_runs"
            ).fetchone() == (0,)
        finally:
            maintenance.close()

    old.restart()
    try:
        old.observe(
            esc._observation(None, title="Post-migration provider", evidence=False)
        )
        assert _count(old, TABLES[0]) == 1
    finally:
        old.holder.connection.close()
