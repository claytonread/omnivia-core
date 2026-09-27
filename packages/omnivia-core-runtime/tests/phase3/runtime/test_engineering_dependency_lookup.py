"""Exact-version dependency reads bound by the version index (migration 0052).

Every read of one exact version's dependency rows -- the service's carry and
evaluator reads, and every read in the dependency-set INSERT guard -- seeks 0050's
`(workspace_id, record_id, version)` index on all three columns. Without
statistics SQLite otherwise answers such a read through the primary key on
`workspace_id` alone: a walk of the workspace's whole dependency table. The plans
asserted here are SQLite's own, in a workspace built by the real migrator, for the
SQL the service actually issues and the guard actually installed. The replaced
guard still admits exactly a version's own consistent rows, and 0052 changes
nothing else.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_dependency_carry as carry
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import assert_guards_intact
from omnivia_core_runtime.storage import engineering_preview, engineering_source
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    read_workspace_state,
)

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID
FILES_A = esc.FILES_A

MIGRATION_VERSION = 52
MIGRATION_NAME = "0052_engineering_dependency_lookup.sql"
GUARD = carry.GUARD
INDEX = "omnivia_idx_engineering_dependencies_version"
#: The plan detail of a read that seeks the version index on all three columns.
VERSION_SEEK = f"{INDEX} (workspace_id=? AND record_id=? AND version=?)"
DEPENDENCY_READ = re.compile(r"\bFROM\s+omnivia_engineering_dependencies\b")
SEAL_REFUSAL = "a dependency set seals exactly its recorded dependencies"


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _version_bounded(connection: sqlite3.Connection, sql: str, params: Any) -> int:
    """Assert SQLite's plan for `sql` seeks the version index once per dependency
    read, and return how many reads that was.

    Each table reference plans as exactly one line and only a dependency read can
    use the version index, so equal counts mean no read of the table takes any
    other path.
    """
    plan = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
    reads = len(DEPENDENCY_READ.findall(sql))
    assert sum(VERSION_SEEK in line for line in plan) == reads, (sql, plan)
    return reads


class _Recorder:
    """The real connection, noting each statement that reads dependency rows."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.reads: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        if DEPENDENCY_READ.search(sql):
            self.reads.append((sql, params))
        return self.connection.execute(sql, params)


# --- plans -------------------------------------------------------------------------


def test_the_service_reads_one_version_through_the_version_index(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carry read inside a real `knowledge.propose` and the evaluator read,
    recorded as issued, each seek the version index. Two versions of one record and
    another record's set share the workspace, and each still reads as its own."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    other = workspace.observe(esc._observation(esc._manifest(), title="Other provider"))
    created = workspace.observe(esc._observation(esc._manifest()))
    connection = workspace.holder.connection
    reads: list[tuple[str, Any]] = []
    original = engineering_source.carry_dependency_set

    def recorded(connection: sqlite3.Connection, *args: Any, **kwargs: Any) -> bool:
        recorder = _Recorder(connection)
        try:
            return original(recorder, *args, **kwargs)  # type: ignore[arg-type]
        finally:
            reads.extend(recorder.reads)

    with monkeypatch.context() as patched:
        patched.setattr(engineering_source, "carry_dependency_set", recorded)
        proposed = carry._transition(workspace, "knowledge.propose", created)
    assert carry._set(workspace, proposed) is not None

    target = engineering_source.covered_snapshot(
        connection, workspace_id=WORKSPACE_ID, snapshot_id="esnap-a"
    )
    assert target is not None
    for record in (created, proposed, other):
        recorder = _Recorder(connection)
        assert (
            engineering_source.evaluate_applicability(
                recorder,  # type: ignore[arg-type]
                workspace_id=WORKSPACE_ID,
                record_id=record["record_id"],
                version=record["version"],
                evidence_available=True,
                target=target,
            )
            == "matched"
        )
        reads.extend(recorder.reads)

    assert len(reads) == 4  # the carry's one read and the evaluator's three
    for sql, params in reads:
        assert _version_bounded(connection, sql, params) == 1


def test_the_installed_guard_reads_one_version_through_the_version_index(
    workspace: Workspace,
) -> None:
    """Each statement of the dependency-set INSERT guard as the migrated workspace
    stores it, compiled on its own with every `NEW` value bound as a parameter
    (SQLite plans a trigger's `NEW` values like bound ones), seeks the version
    index for all seven dependency reads: five in the carry comparison and two in
    the seal."""
    connection = workspace.holder.connection
    (sql,) = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (GUARD,)
    ).fetchone()
    body = sql[sql.index("\nBEGIN\n") + len("\nBEGIN\n") : sql.rindex("END")]
    reads = 0
    for statement in split_sql_statements(body):
        values = {name: None for name in re.findall(r"\bNEW\.(\w+)", statement)}
        compiled = re.sub(r"RAISE\(ABORT, '(?:[^']|'')*'\)", "1", statement)
        compiled = re.sub(r"\bNEW\.(\w+)", r":\1", compiled)
        reads += _version_bounded(connection, compiled, values)
    assert reads == 7


# --- behaviour ---------------------------------------------------------------------


def test_the_seal_still_admits_exactly_a_versions_own_consistent_rows(
    workspace: Workspace,
) -> None:
    """The real guard refuses a set whose count, audit or whole-file digest
    disagrees with its version's stored rows, and admits the consistent one, which
    then matches. Throughout, unsealed rows of the same record under another
    version, and of the same version under another record, carry every defect the
    seal looks for: none of them is counted or checked."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    other = workspace.observe(esc._observation(esc._manifest(), title="Other provider"))
    bare = workspace.observe(esc._observation(None, title="Bare provider"))
    connection = workspace.holder.connection
    own_audit, settled_at_us = carry._settlement(workspace, bare["version"])
    other_audit, _ = carry._settlement(workspace, other["version"])
    identities = itertools.count()

    def write(
        record_id: str, version: str, rows: list[tuple[str, str | None, str]], sealed: int
    ) -> None:
        for selector, digest, audit_ref in rows:
            connection.execute(
                "INSERT INTO omnivia_engineering_dependencies "
                "(workspace_id, dependency_id, record_id, version, selector_type, selector, "
                "meaning, producer, expected_digest, recorded_at_us, audit_ref) "
                "VALUES (?, ?, ?, ?, 'whole_file', ?, 'must_match', 'omnivia-dev-indexer', "
                "?, ?, ?)",
                (
                    WORKSPACE_ID,
                    f"edep-direct-{next(identities)}",
                    record_id,
                    version,
                    selector,
                    digest,
                    settled_at_us,
                    audit_ref,
                ),
            )
        if sealed:
            connection.execute(
                "INSERT INTO omnivia_engineering_dependency_sets "
                "(workspace_id, record_id, version, repository_id, stream_id, snapshot_id, "
                "producer, producer_version, coverage, dependency_count, recorded_at_us, "
                "audit_ref) VALUES (?, ?, ?, ?, ?, 'esnap-a', 'omnivia-dev-indexer', "
                "'1.0.0', 'complete', ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    record_id,
                    version,
                    esc.REPOSITORY,
                    esc.STREAM,
                    sealed,
                    settled_at_us,
                    own_audit,
                ),
            )

    own = [(path, digest, own_audit) for path, digest in FILES_A.items()]
    defective = [(path, None, other_audit) for path in FILES_A]
    with esc._fenced(workspace):
        write(bare["record_id"], "evneighbour", defective, 0)
        write("erneighbour", bare["version"], defective, 0)

    (auth, util, readme) = own
    for name, rows, sealed in (
        ("fewer sealed than stored", own, 2),
        ("more sealed than stored", own, 4),
        ("a row under another audit", [auth, util, (*readme[:2], other_audit)], 3),
        ("a whole-file row without its digest", [auth, util, (readme[0], None, own_audit)], 3),
    ):
        with pytest.raises(sqlite3.DatabaseError, match=SEAL_REFUSAL), esc._fenced(workspace):
            write(bare["record_id"], bare["version"], rows, sealed)
        assert carry._set(workspace, bare) is None, name
    with esc._fenced(workspace):
        write(bare["record_id"], bare["version"], own, 3)
    assert carry._set(workspace, bare) is not None
    assert workspace.status(bare, "esnap-a") == "matched"


# --- the migration -----------------------------------------------------------------


def test_0052_differs_from_the_guard_it_replaces_only_by_index_hints() -> None:
    """0052 drops and recreates 0051's dependency-set INSERT guard under its own
    name, and the new body is 0051's once the version-index hints are set aside, so
    what the guard admits and refuses, and every message, stay as they were. No
    comment sits inside the body, so the migrator's statement splitter and
    `executescript` store the same trigger."""
    migrations = {m.version: m for m in load_migrations()}
    assert migrations[MIGRATION_VERSION].name == MIGRATION_NAME

    def statements(version: int) -> list[str]:
        return [" ".join(s.split()) for s in split_sql_statements(migrations[version].sql)]

    drop, create = statements(MIGRATION_VERSION)
    assert drop == f"DROP TRIGGER {GUARD}"
    hint = f" INDEXED BY {INDEX}"
    assert create.replace(hint, "") == statements(MIGRATION_VERSION - 1)[1].replace(hint, "")
    assert "--" not in migrations[MIGRATION_VERSION].sql.split("CREATE TRIGGER", 1)[1]


def test_0052_fresh_and_upgraded_workspaces_reach_one_canonical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]

    def verified(connection: sqlite3.Connection) -> None:
        assert applied_migrations(connection)[MIGRATION_VERSION] == migration.checksum
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert_guards_intact(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []

    fresh = tmp_path / "fresh.sqlite"
    m2.materialise_phase0_baseline(fresh)
    m2.bootstrap_and_migrate(fresh)
    connection = open_database(fresh, OpenMode.READ_ONLY)
    try:
        verified(connection)
    finally:
        connection.close()

    # Upgraded: a 0051 workspace already holding a sealed set.
    (tmp_path / "upgraded").mkdir()
    with (
        monkeypatch.context() as older_release,
        m2.migration_catalogue_through(MIGRATION_VERSION - 1),
    ):
        # The release that wrote this workspace predates the preview projection
        # (0053), so its writers projected nothing.
        older_release.setattr(engineering_preview, "record_preview", lambda *_a, **_k: None)
        upgraded = Workspace(tmp_path / "upgraded")
        upgraded.record(esc._source(1, "esnap-a", FILES_A))
        created = upgraded.observe(esc._observation(esc._manifest()))
        upgraded.holder.connection.close()
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(upgraded.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
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
            assert [m.version for m in applied] == [MIGRATION_VERSION]
            verified(maintenance)
        finally:
            maintenance.close()

    # A real start migrates the workspace to head before it serves a read or a write,
    # so 0053's preview projection reaches the pre-upgrade proposal before the
    # service restarts on it.
    head = open_database(upgraded.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(head)
        assert state is not None
        applied_to_head = apply_pending_migrations(
            head,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m2.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert [m.version for m in applied_to_head] == [
            m.version for m in load_migrations() if m.version > MIGRATION_VERSION
        ]
    finally:
        head.close()

    # The restarted service seals, carries and evaluates through the replaced guard.
    upgraded.restart()
    try:
        assert upgraded.status(created, "esnap-a") == "matched"
        accepted = esc._accept(upgraded, created)
        assert upgraded.status(accepted, "esnap-a") == "matched"
        later = upgraded.observe(esc._observation(esc._manifest(), title="Later provider"))
        assert upgraded.status(later, "esnap-a") == "matched"
    finally:
        upgraded.holder.connection.close()
