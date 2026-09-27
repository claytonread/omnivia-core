"""Engineering Memory captured-source storage foundation
(SPEC-CORE-ENGMEM-001, plan P0-04; spec §6.3, §15; migration 0056).

Proves the additive migration, its guards, legacy `flat_v1` compatibility and
captured-index reads. Rows are assembled directly against the migrated schema
under the same fenced mutation transaction the service opens, so this suite can
exercise individual database guards independently of the accepted application
operation and installed producer covered by `test_working_tree_snapshot.py`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    fenced_transaction,
)
from omnivia_core_runtime.storage import engineering_source, repository_identity
from omnivia_core_runtime.storage.connection import (
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    canonical_schema_fingerprint,
    load_migrations,
)

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID

MIGRATION_VERSION = 56
MIGRATION_NAME = "0056_engineering_captured_source_coverage.sql"
NEW_TABLES = (
    "omnivia_engineering_snapshot_files",
    "omnivia_engineering_snapshot_captures",
    "omnivia_engineering_source_stream_origins",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _fenced(workspace: esc.Workspace) -> Any:
    return fenced_transaction(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    principal_id: str,
    operation: str,
    now_us: int,
    ref: str | None = None,
) -> str:
    ref = ref or f"aud-{operation.replace('.', '-')}-{now_us}-{principal_id}"
    connection.execute(
        "INSERT INTO omnivia_application_audit_events (audit_ref, workspace_id, "
        "principal_id, operation, purpose, request_id, correlation_id, trace_id, "
        "granted_authority_json, outcome_class, error_code, recorded_at_us) "
        "VALUES (?, ?, ?, ?, 'p', ?, ?, ?, '{}', 'succeeded', NULL, ?)",
        (ref, WORKSPACE_ID, principal_id, operation, ref, ref, ref, now_us),
    )
    return ref


def _files(count: int) -> dict[str, str]:
    return {
        f"src/module_{i:05d}.py": content_digest(f"content {i}") for i in range(count)
    }


def _seal(
    workspace: esc.Workspace,
    *,
    repository_id: str,
    stream_id: str,
    principal_id: str,
    checkout_id: str,
    snapshot_id: str,
    files: dict[str, str],
    capture_status: str = "complete",
    base_us: int,
) -> SimpleNamespace:
    """Assemble one sealed captured snapshot, its stream origin and its
    `captured_v1` event -- directly, under the real guard triggers, exactly as
    the later Stage 2 writer will, minus the operation that does not exist yet.
    """
    connection = workspace.holder.connection
    installation_id = "inst-1"
    # Keyed on the actual file->digest mapping, not just a count, so two
    # snapshots whose byte content differs never collide on the same digest --
    # exactly as a real working-tree manifest digest would behave.
    rich_manifest = {"mode": "captured_v1", "file_count": len(files), "files": files}
    rich_digest = content_digest(canonical_document(rich_manifest))
    coverage_digest = engineering_source.captured_coverage_digest(files)
    with _fenced(workspace):
        reg_audit = _audit(
            connection,
            principal_id="core-service",
            operation="engineering.repository.register",
            now_us=base_us,
            ref=f"aud-reg-{repository_id}-{stream_id}-{base_us}",
        )
        settlement = SimpleNamespace(audit_ref=reg_audit)
        if (
            repository_identity.resolve_repository(
                connection, workspace_id=WORKSPACE_ID, repository_id=repository_id
            )
            is None
        ):
            repository_identity.register_repository(
                connection,
                settlement,
                workspace_id=WORKSPACE_ID,
                repository_id=repository_id,
                display_name=repository_id,
                provider_hint=None,
                registered_at_us=base_us,
            )
        repository_identity.register_checkout(
            connection,
            settlement,
            workspace_id=WORKSPACE_ID,
            checkout_id=checkout_id,
            repository_id=repository_id,
            installation_id=installation_id,
            checkout_hint=f"/checkouts/{checkout_id}",
            registered_at_us=base_us,
        )

        capture_audit = _audit(
            connection,
            principal_id="core-service",
            operation="engineering.snapshot.capture",
            now_us=base_us + 1,
            ref=f"aud-capture-{snapshot_id}",
        )
        if (
            connection.execute(
                "SELECT 1 FROM omnivia_blob_objects WHERE workspace_id = ? "
                "AND content_digest = ?",
                (WORKSPACE_ID, rich_digest),
            ).fetchone()
            is None
        ):
            connection.execute(
                "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
                "content_length_bytes, created_at_us, verified_at_us) "
                "VALUES (?, ?, ?, ?, ?)",
                (WORKSPACE_ID, rich_digest, 2, base_us + 1, base_us + 1),
            )
        evidence_id = f"evd-{snapshot_id}"
        connection.execute(
            "INSERT INTO omnivia_evidence_artifacts "
            "(evidence_id, workspace_id, source_kind, source_native_id, source_locator, "
            "source_retrieved_at_us, event_at_us, observed_at_us, ingested_at_us, "
            "recorded_at_us, content_checksum, blob_content_digest, media_type, "
            "original_metadata_json, original_metadata_digest, sensitivity, "
            "parser_status, ingestion_status, staged_source_ref, import_run_id) "
            "VALUES (?, ?, 'document', ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, "
            "'application/json', '{}', ?, 'internal', 'not_applicable', 'complete', "
            "NULL, NULL)",
            (
                evidence_id,
                WORKSPACE_ID,
                f"working-tree-manifest.{snapshot_id}",
                base_us + 1,
                base_us + 1,
                rich_digest,
                rich_digest,
                content_digest("{}"),
            ),
        )

        recorded_digest = repository_identity.record_snapshot(
            connection,
            SimpleNamespace(audit_ref=capture_audit),
            workspace_id=WORKSPACE_ID,
            snapshot_id=snapshot_id,
            repository_id=repository_id,
            snapshot_kind="working_tree",
            manifest=rich_manifest,
            base_commit=None,
            capture_status=capture_status,
            captured_at_us=base_us + 1,
        )
        assert recorded_digest == rich_digest

        for path, digest in files.items():
            connection.execute(
                "INSERT INTO omnivia_engineering_snapshot_files "
                "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (WORKSPACE_ID, snapshot_id, path, digest, capture_audit),
            )

        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures "
            "(workspace_id, snapshot_id, repository_id, installation_id, checkout_id, "
            "manifest_evidence_id, rich_manifest_digest, coverage_digest, file_count, "
            "capture_status, captured_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                snapshot_id,
                repository_id,
                installation_id,
                checkout_id,
                evidence_id,
                rich_digest,
                coverage_digest,
                len(files),
                capture_status,
                base_us + 1,
                capture_audit,
            ),
        )

        commit_audit = _audit(
            connection,
            principal_id=principal_id,
            operation="engineering.source.capture.commit",
            now_us=base_us + 2,
            ref=f"aud-commit-{snapshot_id}",
        )
        existing_stream = connection.execute(
            "SELECT covered_sequence FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (WORKSPACE_ID, stream_id),
        ).fetchone()
        if existing_stream is None:
            connection.execute(
                "INSERT INTO omnivia_engineering_source_streams "
                "(workspace_id, stream_id, repository_id, principal_id, "
                "announced_sequence, covered_sequence, registered_at_us, "
                "updated_at_us, audit_ref) VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    stream_id,
                    repository_id,
                    principal_id,
                    base_us + 2,
                    base_us + 2,
                    commit_audit,
                ),
            )
        # Chain a second-and-later captured event on a reused stream by its
        # actual next sequence, never a hardcoded 1 -- `_seal` is called
        # repeatedly on the same stream to build a covered history.
        next_sequence = 1 if existing_stream is None else existing_stream[0] + 1
        predecessor_sequence: int | None = None
        predecessor_snapshot_id: str | None = None
        if next_sequence > 1:
            predecessor_sequence = next_sequence - 1
            predecessor_snapshot_id = connection.execute(
                "SELECT snapshot_id FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND stream_id = ? AND sequence = ?",
                (WORKSPACE_ID, stream_id, predecessor_sequence),
            ).fetchone()[0]
            connection.execute(
                "UPDATE omnivia_engineering_source_streams SET "
                "announced_sequence = ?, updated_at_us = ?, audit_ref = ? "
                "WHERE workspace_id = ? AND stream_id = ? AND announced_sequence < ?",
                (
                    next_sequence,
                    base_us + 2,
                    commit_audit,
                    WORKSPACE_ID,
                    stream_id,
                    next_sequence,
                ),
            )
        if (
            connection.execute(
                "SELECT 1 FROM omnivia_engineering_source_stream_origins "
                "WHERE workspace_id = ? AND stream_id = ?",
                (WORKSPACE_ID, stream_id),
            ).fetchone()
            is None
        ):
            connection.execute(
                "INSERT INTO omnivia_engineering_source_stream_origins "
                "(workspace_id, stream_id, repository_id, installation_id, "
                "checkout_id, bound_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    stream_id,
                    repository_id,
                    installation_id,
                    checkout_id,
                    base_us + 2,
                    commit_audit,
                ),
            )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_events "
            "(workspace_id, stream_id, sequence, snapshot_id, predecessor_sequence, "
            "predecessor_snapshot_id, manifest_json, manifest_digest, "
            "manifest_entry_count, event_digest, recorded_at_us, audit_ref, "
            "manifest_format) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 0, ?, ?, ?, "
            "'captured_v1')",
            (
                WORKSPACE_ID,
                stream_id,
                next_sequence,
                snapshot_id,
                predecessor_sequence,
                predecessor_snapshot_id,
                rich_digest,
                "sha256:" + "0" * 64,
                base_us + 2,
                commit_audit,
            ),
        )
        connection.execute(
            "UPDATE omnivia_engineering_source_streams SET covered_sequence = ?, "
            "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? AND stream_id = ?",
            (next_sequence, base_us + 2, commit_audit, WORKSPACE_ID, stream_id),
        )
    return SimpleNamespace(
        repository_id=repository_id,
        stream_id=stream_id,
        principal_id=principal_id,
        checkout_id=checkout_id,
        installation_id=installation_id,
        snapshot_id=snapshot_id,
        files=files,
        rich_manifest_digest=rich_digest,
        coverage_digest=coverage_digest,
        capture_status=capture_status,
        file_count=len(files),
        capture_audit=capture_audit,
        commit_audit=commit_audit,
        evidence_id=evidence_id,
        captured_at_us=base_us + 1,
    )


# --- the migration itself -----------------------------------------------------------


def test_0056_is_the_additive_successor_and_creates_guarded_tables(
    workspace: esc.Workspace,
) -> None:
    migrations = load_migrations()
    migration = next(item for item in migrations if item.version == MIGRATION_VERSION)
    assert migration.name == MIGRATION_NAME
    assert migrations[migrations.index(migration) - 1].version == 55
    assert applied_migrations(workspace.holder.connection)[56] == migration.checksum
    present = {
        str(row[0])
        for row in workspace.holder.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
    assert set(NEW_TABLES) <= present
    assert_guards_intact(workspace.holder.connection)
    assert fingerprint_schema(workspace.holder.connection).matches(
        canonical_schema_fingerprint()
    )
    assert integrity_check(workspace.holder.connection) == []
    assert foreign_key_check(workspace.holder.connection) == []


def test_the_0056_alter_backfills_existing_events_and_defaults_new_ones_to_flat_v1(
    workspace: esc.Workspace,
) -> None:
    """The exact 0056 ALTER covers rows written before and after the upgrade."""
    migration = next(item for item in load_migrations() if item.version == 56)
    alter_sql = migration.sql.split(
        "CREATE TABLE IF NOT EXISTS omnivia_engineering_snapshot_files", 1
    )[0]
    before_upgrade = sqlite3.connect(":memory:")
    try:
        before_upgrade.execute(
            "CREATE TABLE omnivia_engineering_source_events "
            "(event_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)"
        )
        before_upgrade.execute(
            "INSERT INTO omnivia_engineering_source_events VALUES ('old', '{}')"
        )
        before_upgrade.executescript(alter_sql)
        before_upgrade.execute(
            "INSERT INTO omnivia_engineering_source_events "
            "(event_id, manifest_json) VALUES ('new', '{}')"
        )
        assert before_upgrade.execute(
            "SELECT event_id, manifest_format FROM omnivia_engineering_source_events "
            "ORDER BY event_id"
        ).fetchall() == [("new", "flat_v1"), ("old", "flat_v1")]
    finally:
        before_upgrade.close()

    # The unchanged production writer also retains 0050's canonical manifest,
    # digest and entry-count behavior after the full migrated schema is present.
    first = workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    assert first["disposition"] == "recorded"
    row = workspace.holder.connection.execute(
        "SELECT manifest_format, manifest_json, manifest_digest, manifest_entry_count "
        "FROM omnivia_engineering_source_events WHERE stream_id = ? AND sequence = 1",
        (esc.STREAM,),
    ).fetchone()
    assert row[0] == "flat_v1"
    assert row[2] == first["manifest_digest"]
    assert content_digest(row[1]) == row[2]
    assert row[3] == len(esc.FILES_A)
    # `covered_snapshot` reports the same legacy representation and manifest.
    covered = engineering_source.covered_snapshot(
        workspace.holder.connection, workspace_id=WORKSPACE_ID, snapshot_id="esnap-a"
    )
    assert covered is not None
    assert covered.representation == "flat_v1"
    assert covered.manifest == esc.FILES_A


# --- assembling a valid captured chain -----------------------------------------------


def test_a_valid_captured_index_header_origin_and_event_can_be_assembled(
    workspace: esc.Workspace,
) -> None:
    files = _files(257)  # a 257-row fixture: the legacy 256 cap is not imposed here
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-a",
        files=files,
        base_us=10_000,
    )
    assert (
        workspace.holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_snapshot_files "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (WORKSPACE_ID, chain.snapshot_id),
        ).fetchone()[0]
        == 257
    )
    covered = engineering_source.covered_snapshot(
        workspace.holder.connection,
        workspace_id=WORKSPACE_ID,
        snapshot_id=chain.snapshot_id,
    )
    assert covered is not None
    assert covered.representation == "captured_v1"
    # The rich manifest is never hydrated: the sentinel is not read as an
    # empty repository, and no captured row becomes `matched` because of it.
    assert covered.manifest == {}
    assert covered.manifest_digest == chain.rich_manifest_digest
    assert covered.capture_status == "complete"
    assert_guards_intact(workspace.holder.connection)
    assert integrity_check(workspace.holder.connection) == []
    assert foreign_key_check(workspace.holder.connection) == []


def test_a_captured_snapshot_participates_in_applicability_without_an_inline_manifest(
    workspace: esc.Workspace,
) -> None:
    """A >256-file captured index proves `matched`/`potentially_stale`/`invalid`
    through the bounded per-path lookup, never the inline manifest."""
    files = _files(620)
    baseline = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-base",
        files=files,
        base_us=20_000,
    )
    changed = dict(files)
    changed["src/module_00000.py"] = content_digest("changed")
    target_stale = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-target-stale",
        files=changed,
        base_us=30_000,
    )
    missing = dict(files)
    del missing["src/module_00001.py"]
    target_invalid = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-target-invalid",
        files=missing,
        base_us=40_000,
    )
    target_matched = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-target-matched",
        files=files,
        base_us=50_000,
    )

    connection = workspace.holder.connection
    dependencies = [
        (
            "whole_file",
            "src/module_00000.py",
            "must_match",
            files["src/module_00000.py"],
        ),
        (
            "whole_file",
            "src/module_00001.py",
            "must_match",
            files["src/module_00001.py"],
        ),
    ]
    baseline_snapshot = engineering_source.covered_snapshot(
        connection, workspace_id=WORKSPACE_ID, snapshot_id=baseline.snapshot_id
    )
    assert baseline_snapshot is not None

    def _target(snapshot_id: str) -> engineering_source.CoveredSnapshot:
        target = engineering_source.covered_snapshot(
            connection, workspace_id=WORKSPACE_ID, snapshot_id=snapshot_id
        )
        assert target is not None
        return target

    assert (
        engineering_source.decide(
            dependencies,
            baseline=baseline_snapshot,
            target=_target(target_matched.snapshot_id),
            qualified=True,
        )
        == "unknown"
    )
    # `decide` alone cannot resolve a captured pair: neither side's manifest is
    # hydrated yet. `evaluate_applicability` resolves exactly the required
    # paths first -- proven directly through the bounded lookup helper here.
    resolved_baseline = engineering_source.captured_manifest_lookup(
        connection,
        workspace_id=WORKSPACE_ID,
        snapshot_id=baseline.snapshot_id,
        paths=[dep[1] for dep in dependencies],
    )
    assert resolved_baseline == {
        "src/module_00000.py": files["src/module_00000.py"],
        "src/module_00001.py": files["src/module_00001.py"],
    }
    from dataclasses import replace

    assert (
        engineering_source.decide(
            dependencies,
            baseline=replace(baseline_snapshot, manifest=resolved_baseline),
            target=replace(
                _target(target_matched.snapshot_id),
                manifest=engineering_source.captured_manifest_lookup(
                    connection,
                    workspace_id=WORKSPACE_ID,
                    snapshot_id=target_matched.snapshot_id,
                    paths=[dep[1] for dep in dependencies],
                ),
            ),
            qualified=True,
        )
        == "matched"
    )
    assert (
        engineering_source.decide(
            dependencies,
            baseline=replace(baseline_snapshot, manifest=resolved_baseline),
            target=replace(
                _target(target_stale.snapshot_id),
                manifest=engineering_source.captured_manifest_lookup(
                    connection,
                    workspace_id=WORKSPACE_ID,
                    snapshot_id=target_stale.snapshot_id,
                    paths=[dep[1] for dep in dependencies],
                ),
            ),
            qualified=True,
        )
        == "potentially_stale"
    )
    assert (
        engineering_source.decide(
            dependencies,
            baseline=replace(baseline_snapshot, manifest=resolved_baseline),
            target=replace(
                _target(target_invalid.snapshot_id),
                manifest=engineering_source.captured_manifest_lookup(
                    connection,
                    workspace_id=WORKSPACE_ID,
                    snapshot_id=target_invalid.snapshot_id,
                    paths=[dep[1] for dep in dependencies],
                ),
            ),
            qualified=True,
        )
        == "invalid"
    )

    # Exercise the installed evaluator too. It reads the sealed dependency set,
    # then resolves only those two whole-file paths from each captured index.
    record = workspace.observe(
        esc._observation(
            esc._manifest(
                snapshot_id=baseline.snapshot_id,
                stream=baseline.stream_id,
                repository=baseline.repository_id,
                dependencies=[
                    esc._dependency(
                        "src/module_00000.py", files["src/module_00000.py"]
                    ),
                    esc._dependency(
                        "src/module_00001.py", files["src/module_00001.py"]
                    ),
                ],
            )
        )
    )

    def _verdict(snapshot_id: str) -> str:
        target = _target(snapshot_id)
        return engineering_source.evaluate_applicability(
            connection,
            workspace_id=WORKSPACE_ID,
            record_id=record["record_id"],
            version=record["version"],
            evidence_available=True,
            target=target,
        )

    assert _verdict(target_matched.snapshot_id) == "matched"
    assert _verdict(target_stale.snapshot_id) == "potentially_stale"
    assert _verdict(target_invalid.snapshot_id) == "invalid"


def test_incomplete_captured_baselines_and_targets_stay_unknown(
    workspace: esc.Workspace,
) -> None:
    files = _files(257)

    def _record_for(baseline: SimpleNamespace) -> dict[str, str]:
        return workspace.observe(
            esc._observation(
                esc._manifest(
                    snapshot_id=baseline.snapshot_id,
                    stream=baseline.stream_id,
                    repository=baseline.repository_id,
                    dependencies=[
                        esc._dependency(
                            "src/module_00000.py", files["src/module_00000.py"]
                        )
                    ],
                )
            )
        )

    complete_baseline = _seal(
        workspace,
        repository_id="erepo-incomplete-target",
        stream_id="estream-incomplete-target",
        principal_id="capture-owner",
        checkout_id="co-incomplete-target",
        snapshot_id="csnap-complete-baseline",
        files=files,
        base_us=55_000,
    )
    incomplete_target = _seal(
        workspace,
        repository_id=complete_baseline.repository_id,
        stream_id=complete_baseline.stream_id,
        principal_id=complete_baseline.principal_id,
        checkout_id=complete_baseline.checkout_id,
        snapshot_id="csnap-incomplete-target",
        files=files,
        capture_status="incomplete",
        base_us=56_000,
    )
    complete_record = _record_for(complete_baseline)

    incomplete_baseline = _seal(
        workspace,
        repository_id="erepo-incomplete-baseline",
        stream_id="estream-incomplete-baseline",
        principal_id="capture-owner",
        checkout_id="co-incomplete-baseline",
        snapshot_id="csnap-incomplete-baseline",
        files=files,
        capture_status="incomplete",
        base_us=57_000,
    )
    complete_target = _seal(
        workspace,
        repository_id=incomplete_baseline.repository_id,
        stream_id=incomplete_baseline.stream_id,
        principal_id=incomplete_baseline.principal_id,
        checkout_id=incomplete_baseline.checkout_id,
        snapshot_id="csnap-complete-target",
        files=files,
        base_us=58_000,
    )
    incomplete_record = _record_for(incomplete_baseline)

    connection = workspace.holder.connection

    def _status(record: dict[str, str], target_id: str) -> str:
        target = engineering_source.covered_snapshot(
            connection, workspace_id=WORKSPACE_ID, snapshot_id=target_id
        )
        assert target is not None
        return engineering_source.evaluate_applicability(
            connection,
            workspace_id=WORKSPACE_ID,
            record_id=record["record_id"],
            version=record["version"],
            evidence_available=True,
            target=target,
        )

    assert _status(complete_record, incomplete_target.snapshot_id) == "unknown"
    assert _status(incomplete_record, complete_target.snapshot_id) == "unknown"


def test_the_bounded_lookup_seeks_the_files_table_primary_key(
    workspace: esc.Workspace,
) -> None:
    files = _files(300)
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-plan",
        files=files,
        base_us=60_000,
    )
    connection = workspace.holder.connection
    plan = [
        str(row[3])
        for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT path, content_digest "
            "FROM omnivia_engineering_snapshot_files "
            "WHERE workspace_id = ? AND snapshot_id = ? AND path IN (?, ?)",
            (WORKSPACE_ID, chain.snapshot_id, "src/module_00000.py", "src/module_00001.py"),
        )
    ]
    detail = "\n".join(plan).upper()
    assert "SEARCH OMNIVIA_ENGINEERING_SNAPSHOT_FILES" in detail
    assert "PRIMARY KEY" in detail
    lookup = engineering_source.captured_manifest_lookup(
        connection,
        workspace_id=WORKSPACE_ID,
        snapshot_id=chain.snapshot_id,
        paths=["src/module_00000.py", "src/module_00001.py", "src/module_00299.py"],
    )
    assert lookup == {
        "src/module_00000.py": files["src/module_00000.py"],
        "src/module_00001.py": files["src/module_00001.py"],
        "src/module_00299.py": files["src/module_00299.py"],
    }


# --- immutability and insert-after-seal ----------------------------------------------


def test_file_insert_after_sealing_is_refused(workspace: esc.Workspace) -> None:
    files = _files(3)
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-sealed",
        files=files,
        base_us=70_000,
    )
    connection = workspace.holder.connection
    with pytest.raises(sqlite3.DatabaseError, match="already sealed"), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_files "
            "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
            "VALUES (?, ?, 'src/late.py', ?, ?)",
            (WORKSPACE_ID, chain.snapshot_id, content_digest("late"), chain.capture_audit),
        )


def test_update_and_delete_are_always_refused_on_the_three_new_tables(
    workspace: esc.Workspace,
) -> None:
    _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-immutable",
        files=_files(2),
        base_us=80_000,
    )
    connection = workspace.holder.connection
    for table in NEW_TABLES:
        for statement in (
            f"UPDATE {table} SET workspace_id = workspace_id",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
                connection.execute(statement)


def test_unguarded_and_wrong_workspace_writes_are_refused(workspace: esc.Workspace) -> None:
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-fence",
        files=_files(2),
        base_us=90_000,
    )
    connection = workspace.holder.connection
    for table in NEW_TABLES:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(f"INSERT INTO {table} SELECT * FROM {table}")

    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures "
            "SELECT 'another-workspace', snapshot_id, repository_id, installation_id, "
            "checkout_id, manifest_evidence_id, rich_manifest_digest, coverage_digest, "
            "file_count, capture_status, captured_at_us, audit_ref "
            "FROM omnivia_engineering_snapshot_captures WHERE snapshot_id = ?",
            (chain.snapshot_id,),
        )


def test_a_stale_fence_cannot_append_to_the_captured_index(
    workspace: esc.Workspace,
) -> None:
    chain = _seal(
        workspace,
        repository_id="erepo-stale-fence",
        stream_id="estream-stale-fence",
        principal_id="capture-owner",
        checkout_id="co-stale-fence",
        snapshot_id="csnap-stale-fence",
        files=_files(2),
        base_us=95_000,
    )
    connection = workspace.holder.connection
    before = connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_snapshot_files"
    ).fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError, match="unguarded"), _fenced(workspace):
        connection.execute(
            "UPDATE omnivia_workspace_state "
            "SET fencing_generation = fencing_generation + 1 WHERE singleton = 1"
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_files "
            "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
            "VALUES (?, ?, 'src/stale.py', ?, ?)",
            (
                WORKSPACE_ID,
                chain.snapshot_id,
                content_digest("stale"),
                chain.capture_audit,
            ),
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_snapshot_files"
    ).fetchone()[0] == before


def test_wrong_audit_operation_writes_are_refused(workspace: esc.Workspace) -> None:
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-captured",
        principal_id="capture-owner",
        checkout_id="co-captured",
        snapshot_id="csnap-wrong-audit",
        files=_files(2),
        base_us=100_000,
    )
    connection = workspace.holder.connection
    with pytest.raises(
        sqlite3.DatabaseError, match="its own audited engineering.snapshot.capture"
    ), _fenced(workspace):
        # `commit_audit` is a real, fenced audit event, but under the wrong operation.
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_files "
            "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
            "VALUES (?, ?, 'src/wrong-audit.py', ?, ?)",
            (
                WORKSPACE_ID,
                chain.snapshot_id,
                content_digest("wrong-audit"),
                chain.commit_audit,
            ),
        )


# --- mismatched repository/installation/checkout/evidence/digest/status/count -------


def test_mismatched_header_facts_are_refused(workspace: esc.Workspace) -> None:
    files = _files(2)
    connection = workspace.holder.connection
    base_us = 110_000
    installation_id = "inst-1"

    def _prepare(snapshot_id: str) -> SimpleNamespace:
        # Assemble everything up to (not including) the header, so each case
        # can mutate exactly one header fact. `snapshot_id` is part of the rich
        # manifest so every case gets its own distinct blob digest.
        rich_manifest = {
            "mode": "captured_v1",
            "file_count": len(files),
            "snapshot_id": snapshot_id,
        }
        rich_digest = content_digest(canonical_document(rich_manifest))
        with _fenced(workspace):
            reg_audit = _audit(
                connection,
                principal_id="core-service",
                operation="engineering.repository.register",
                now_us=base_us,
                ref=f"aud-reg-{snapshot_id}",
            )
            settlement = SimpleNamespace(audit_ref=reg_audit)
            if (
                repository_identity.resolve_repository(
                    connection, workspace_id=WORKSPACE_ID, repository_id="erepo-mismatch"
                )
                is None
            ):
                repository_identity.register_repository(
                    connection,
                    settlement,
                    workspace_id=WORKSPACE_ID,
                    repository_id="erepo-mismatch",
                    display_name="erepo-mismatch",
                    provider_hint=None,
                    registered_at_us=base_us,
                )
            checkout_id = f"co-{snapshot_id}"
            repository_identity.register_checkout(
                connection,
                settlement,
                workspace_id=WORKSPACE_ID,
                checkout_id=checkout_id,
                repository_id="erepo-mismatch",
                installation_id=installation_id,
                checkout_hint=f"/checkouts/{snapshot_id}",
                registered_at_us=base_us,
            )
            capture_audit = _audit(
                connection,
                principal_id="core-service",
                operation="engineering.snapshot.capture",
                now_us=base_us + 1,
                ref=f"aud-capture-{snapshot_id}",
            )
            connection.execute(
                "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
                "content_length_bytes, created_at_us, verified_at_us) "
                "VALUES (?, ?, 2, ?, ?)",
                (WORKSPACE_ID, rich_digest, base_us + 1, base_us + 1),
            )
            evidence_id = f"evd-{snapshot_id}"
            connection.execute(
                "INSERT INTO omnivia_evidence_artifacts "
                "(evidence_id, workspace_id, source_kind, source_native_id, "
                "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
                "ingested_at_us, recorded_at_us, content_checksum, blob_content_digest, "
                "media_type, original_metadata_json, original_metadata_digest, "
                "sensitivity, parser_status, ingestion_status, staged_source_ref, "
                "import_run_id) VALUES (?, ?, 'document', ?, NULL, NULL, NULL, NULL, "
                "?, ?, ?, ?, 'application/json', '{}', ?, 'internal', 'not_applicable', "
                "'complete', NULL, NULL)",
                (
                    evidence_id,
                    WORKSPACE_ID,
                    f"working-tree-manifest.{snapshot_id}",
                    base_us + 1,
                    base_us + 1,
                    rich_digest,
                    rich_digest,
                    content_digest("{}"),
                ),
            )
            repository_identity.record_snapshot(
                connection,
                SimpleNamespace(audit_ref=capture_audit),
                workspace_id=WORKSPACE_ID,
                snapshot_id=snapshot_id,
                repository_id="erepo-mismatch",
                snapshot_kind="working_tree",
                manifest=rich_manifest,
                base_commit=None,
                capture_status="complete",
                captured_at_us=base_us + 1,
            )
            for path, digest in files.items():
                connection.execute(
                    "INSERT INTO omnivia_engineering_snapshot_files "
                    "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE_ID, snapshot_id, path, digest, capture_audit),
                )
        return SimpleNamespace(
            checkout_id=checkout_id,
            evidence_id=evidence_id,
            rich_digest=rich_digest,
            coverage_digest=engineering_source.captured_coverage_digest(files),
            capture_audit=capture_audit,
        )

    def _try_header(snapshot_id: str, prepared: SimpleNamespace, **overrides: Any) -> None:
        header: dict[str, Any] = {
            "workspace_id": WORKSPACE_ID,
            "snapshot_id": snapshot_id,
            "repository_id": "erepo-mismatch",
            "installation_id": installation_id,
            "checkout_id": prepared.checkout_id,
            "manifest_evidence_id": prepared.evidence_id,
            "rich_manifest_digest": prepared.rich_digest,
            "coverage_digest": prepared.coverage_digest,
            "file_count": len(files),
            "capture_status": "complete",
            "captured_at_us": base_us + 1,
            "audit_ref": prepared.capture_audit,
        }
        header.update(overrides)
        with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
            connection.execute(
                "INSERT INTO omnivia_engineering_snapshot_captures "
                "(workspace_id, snapshot_id, repository_id, installation_id, "
                "checkout_id, manifest_evidence_id, rich_manifest_digest, "
                "coverage_digest, file_count, capture_status, captured_at_us, "
                "audit_ref) VALUES (:workspace_id, :snapshot_id, :repository_id, "
                ":installation_id, :checkout_id, :manifest_evidence_id, "
                ":rich_manifest_digest, :coverage_digest, :file_count, "
                ":capture_status, :captured_at_us, :audit_ref)",
                header,
            )

    other_digest = "sha256:" + "b" * 64

    prepared = _prepare("csnap-mismatch-repo")
    _try_header("csnap-mismatch-repo", prepared, repository_id="erepo-other")

    prepared = _prepare("csnap-mismatch-installation")
    _try_header(
        "csnap-mismatch-installation", prepared, installation_id="inst-other"
    )

    prepared = _prepare("csnap-mismatch-checkout")
    _try_header("csnap-mismatch-checkout", prepared, checkout_id="co-nonexistent")

    prepared = _prepare("csnap-mismatch-evidence")
    _try_header(
        "csnap-mismatch-evidence", prepared, manifest_evidence_id="evd-nonexistent"
    )

    prepared = _prepare("csnap-mismatch-digest")
    _try_header("csnap-mismatch-digest", prepared, rich_manifest_digest=other_digest)

    prepared = _prepare("csnap-mismatch-status")
    _try_header("csnap-mismatch-status", prepared, capture_status="incomplete")

    prepared = _prepare("csnap-mismatch-count")
    _try_header("csnap-mismatch-count", prepared, file_count=len(files) + 1)

    prepared = _prepare("csnap-mismatch-source-stream")
    # A repository match with no bound stream at all still seals: this case
    # instead proves the *event* refuses without a matching source stream, so
    # exercise it in the event-level guard tests below. Here we only assert
    # the correct header still succeeds as the control.
    with _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures "
            "(workspace_id, snapshot_id, repository_id, installation_id, checkout_id, "
            "manifest_evidence_id, rich_manifest_digest, coverage_digest, file_count, "
            "capture_status, captured_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                "csnap-mismatch-source-stream",
                "erepo-mismatch",
                installation_id,
                prepared.checkout_id,
                prepared.evidence_id,
                prepared.rich_digest,
                prepared.coverage_digest,
                len(files),
                "complete",
                base_us + 1,
                prepared.capture_audit,
            ),
        )
    assert_guards_intact(workspace.holder.connection)


# --- captured events need both a header and a stream origin --------------------------


def test_a_captured_event_without_its_sealed_header_is_refused(
    workspace: esc.Workspace,
) -> None:
    files = _files(2)
    connection = workspace.holder.connection
    base_us = 120_000
    with _fenced(workspace):
        reg_audit = _audit(
            connection,
            principal_id="core-service",
            operation="engineering.repository.register",
            now_us=base_us,
        )
        settlement = SimpleNamespace(audit_ref=reg_audit)
        repository_identity.register_repository(
            connection,
            settlement,
            workspace_id=WORKSPACE_ID,
            repository_id="erepo-no-header",
            display_name="erepo-no-header",
            provider_hint=None,
            registered_at_us=base_us,
        )
        repository_identity.register_checkout(
            connection,
            settlement,
            workspace_id=WORKSPACE_ID,
            checkout_id="co-no-header",
            repository_id="erepo-no-header",
            installation_id="inst-1",
            checkout_hint="/checkouts/no-header",
            registered_at_us=base_us,
        )
        rich_manifest = {"mode": "captured_v1", "file_count": len(files)}
        rich_digest = content_digest(canonical_document(rich_manifest))
        capture_audit = _audit(
            connection,
            principal_id="core-service",
            operation="engineering.snapshot.capture",
            now_us=base_us + 1,
        )
        repository_identity.record_snapshot(
            connection,
            SimpleNamespace(audit_ref=capture_audit),
            workspace_id=WORKSPACE_ID,
            snapshot_id="csnap-no-header",
            repository_id="erepo-no-header",
            snapshot_kind="working_tree",
            manifest=rich_manifest,
            base_commit=None,
            capture_status="complete",
            captured_at_us=base_us + 1,
        )
        commit_audit = _audit(
            connection,
            principal_id="no-header-owner",
            operation="engineering.source.capture.commit",
            now_us=base_us + 2,
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_streams "
            "(workspace_id, stream_id, repository_id, principal_id, "
            "announced_sequence, covered_sequence, registered_at_us, updated_at_us, "
            "audit_ref) VALUES (?, 'estream-no-header', 'erepo-no-header', "
            "'no-header-owner', 1, 0, ?, ?, ?)",
            (WORKSPACE_ID, base_us + 2, base_us + 2, commit_audit),
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_stream_origins "
            "(workspace_id, stream_id, repository_id, installation_id, checkout_id, "
            "bound_at_us, audit_ref) VALUES (?, 'estream-no-header', "
            "'erepo-no-header', 'inst-1', 'co-no-header', ?, ?)",
            (WORKSPACE_ID, base_us + 2, commit_audit),
        )
        with pytest.raises(sqlite3.DatabaseError, match="captured_v1"):
            connection.execute(
                "INSERT INTO omnivia_engineering_source_events "
                "(workspace_id, stream_id, sequence, snapshot_id, "
                "predecessor_sequence, predecessor_snapshot_id, manifest_json, "
                "manifest_digest, manifest_entry_count, event_digest, recorded_at_us, "
                "audit_ref, manifest_format) VALUES (?, 'estream-no-header', 1, "
                "'csnap-no-header', NULL, NULL, '{}', ?, 0, ?, ?, ?, 'captured_v1')",
                (
                    WORKSPACE_ID,
                    rich_digest,
                    "sha256:" + "1" * 64,
                    base_us + 2,
                    commit_audit,
                ),
            )


def test_a_captured_event_without_its_stream_origin_is_refused(
    workspace: esc.Workspace,
) -> None:
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-header-only",
        principal_id="capture-owner",
        checkout_id="co-header-only",
        snapshot_id="csnap-header-only",
        files=_files(2),
        base_us=130_000,
    )
    connection = workspace.holder.connection
    # Remove only what this test proves matters: bind a *second*, unbound
    # stream to the same sealed header and try to append the event there.
    with _fenced(workspace):
        commit_audit = _audit(
            connection,
            principal_id="capture-owner",
            operation="engineering.source.capture.commit",
            now_us=140_000,
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_streams "
            "(workspace_id, stream_id, repository_id, principal_id, "
            "announced_sequence, covered_sequence, registered_at_us, updated_at_us, "
            "audit_ref) VALUES (?, 'estream-unbound', ?, 'capture-owner', 1, 0, ?, ?, ?)",
            (WORKSPACE_ID, chain.repository_id, 140_000, 140_000, commit_audit),
        )
        with pytest.raises(sqlite3.DatabaseError, match="captured_v1"):
            connection.execute(
                "INSERT INTO omnivia_engineering_source_events "
                "(workspace_id, stream_id, sequence, snapshot_id, "
                "predecessor_sequence, predecessor_snapshot_id, manifest_json, "
                "manifest_digest, manifest_entry_count, event_digest, "
                "recorded_at_us, audit_ref, manifest_format) VALUES (?, "
                "'estream-unbound', 1, ?, NULL, NULL, '{}', ?, 0, ?, ?, ?, "
                "'captured_v1')",
                (
                    WORKSPACE_ID,
                    chain.snapshot_id,
                    chain.rich_manifest_digest,
                    "sha256:" + "2" * 64,
                    140_000,
                    commit_audit,
                ),
            )


# --- the joined proof rejects a checkout/installation that disagrees -----------------


def test_a_captured_event_whose_origin_disagrees_with_its_header_checkout_or_installation_is_refused(
    workspace: esc.Workspace,
) -> None:
    """The captured_v1 guard's joined proof requires the stream's origin and its
    sealed capture header to agree on repository, installation and checkout, not
    merely that a header and *some* origin each exist somewhere. A stream bound
    to a different checkout -- whether under the same installation or a wholly
    different one -- can never borrow another checkout's sealed header."""
    victim = _seal(
        workspace,
        repository_id="erepo-cross",
        stream_id="estream-victim",
        principal_id="capture-owner",
        checkout_id="co-victim",
        snapshot_id="csnap-victim",
        files=_files(2),
        base_us=200_000,
    )
    connection = workspace.holder.connection

    def _attempt(
        *, checkout_id: str, installation_id: str, stream_id: str, base_us: int
    ) -> None:
        with _fenced(workspace):
            reg_audit = _audit(
                connection,
                principal_id="core-service",
                operation="engineering.repository.register",
                now_us=base_us,
                ref=f"aud-reg-{checkout_id}",
            )
            repository_identity.register_checkout(
                connection,
                SimpleNamespace(audit_ref=reg_audit),
                workspace_id=WORKSPACE_ID,
                checkout_id=checkout_id,
                repository_id=victim.repository_id,
                installation_id=installation_id,
                checkout_hint=f"/checkouts/{checkout_id}",
                registered_at_us=base_us,
            )
            commit_audit = _audit(
                connection,
                principal_id="capture-owner",
                operation="engineering.source.capture.commit",
                now_us=base_us + 1,
                ref=f"aud-commit-{stream_id}",
            )
            connection.execute(
                "INSERT INTO omnivia_engineering_source_streams "
                "(workspace_id, stream_id, repository_id, principal_id, "
                "announced_sequence, covered_sequence, registered_at_us, "
                "updated_at_us, audit_ref) VALUES (?, ?, ?, 'capture-owner', 1, 0, "
                "?, ?, ?)",
                (
                    WORKSPACE_ID,
                    stream_id,
                    victim.repository_id,
                    base_us + 1,
                    base_us + 1,
                    commit_audit,
                ),
            )
            connection.execute(
                "INSERT INTO omnivia_engineering_source_stream_origins "
                "(workspace_id, stream_id, repository_id, installation_id, "
                "checkout_id, bound_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    stream_id,
                    victim.repository_id,
                    installation_id,
                    checkout_id,
                    base_us + 1,
                    commit_audit,
                ),
            )
            with pytest.raises(sqlite3.DatabaseError, match="joined proof"):
                connection.execute(
                    "INSERT INTO omnivia_engineering_source_events "
                    "(workspace_id, stream_id, sequence, snapshot_id, "
                    "predecessor_sequence, predecessor_snapshot_id, manifest_json, "
                    "manifest_digest, manifest_entry_count, event_digest, "
                    "recorded_at_us, audit_ref, manifest_format) VALUES (?, ?, 1, "
                    "?, NULL, NULL, '{}', ?, 0, ?, ?, ?, 'captured_v1')",
                    (
                        WORKSPACE_ID,
                        stream_id,
                        victim.snapshot_id,
                        victim.rich_manifest_digest,
                        f"sha256:{'9' * 64}",
                        base_us + 1,
                        commit_audit,
                    ),
                )

    # Same installation as the victim's sealed header, a different checkout.
    _attempt(
        checkout_id="co-attacker-checkout",
        installation_id=victim.installation_id,
        stream_id="estream-attacker-checkout",
        base_us=210_000,
    )
    # A different installation entirely (necessarily its own, different checkout).
    _attempt(
        checkout_id="co-attacker-installation",
        installation_id="inst-other",
        stream_id="estream-attacker-installation",
        base_us=220_000,
    )
    assert_guards_intact(workspace.holder.connection)


# --- flat/captured mixing is refused in both directions -------------------------------


def test_flat_and_captured_stream_mixing_is_refused_in_both_directions(
    workspace: esc.Workspace,
) -> None:
    chain = _seal(
        workspace,
        repository_id="erepo-captured",
        stream_id="estream-mixing",
        principal_id="capture-owner",
        checkout_id="co-mixing",
        snapshot_id="csnap-mixing-1",
        files=_files(2),
        base_us=150_000,
    )
    connection = workspace.holder.connection
    # A flat event is refused on a stream already bound to a captured origin.
    with _fenced(workspace):
        connection.execute(
            "UPDATE omnivia_engineering_source_streams SET announced_sequence = 2, "
            "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? AND stream_id = ?",
            (
                160_000,
                _audit(
                    connection,
                    principal_id=chain.principal_id,
                    operation="engineering.source.capture.commit",
                    now_us=160_000,
                    ref="aud-advance-mixing",
                ),
                WORKSPACE_ID,
                chain.stream_id,
            ),
        )
        record_audit = _audit(
            connection,
            principal_id=chain.principal_id,
            operation="engineering.source.record",
            now_us=160_001,
        )
        manifest = {"a.py": content_digest("a")}
        manifest_json = canonical_document(manifest)
        repository_identity.record_snapshot(
            connection,
            SimpleNamespace(audit_ref=record_audit),
            workspace_id=WORKSPACE_ID,
            snapshot_id="csnap-flat-on-captured",
            repository_id=chain.repository_id,
            snapshot_kind="working_tree",
            manifest=manifest,
            base_commit=None,
            capture_status="complete",
            captured_at_us=160_001,
        )
        with pytest.raises(sqlite3.DatabaseError, match="refused on a stream already bound"):
            connection.execute(
                "INSERT INTO omnivia_engineering_source_events "
                "(workspace_id, stream_id, sequence, snapshot_id, "
                "predecessor_sequence, predecessor_snapshot_id, manifest_json, "
                "manifest_digest, manifest_entry_count, event_digest, "
                "recorded_at_us, audit_ref, manifest_format) VALUES (?, ?, 2, "
                "'csnap-flat-on-captured', 1, ?, ?, ?, 1, ?, ?, ?, 'flat_v1')",
                (
                    WORKSPACE_ID,
                    chain.stream_id,
                    chain.snapshot_id,
                    manifest_json,
                    content_digest(manifest_json),
                    "sha256:" + "3" * 64,
                    160_001,
                    record_audit,
                ),
            )

    # A captured event is refused on a legacy flat stream (no origin at all).
    flat = workspace.record(esc._source(1, "esnap-flat-only", esc.FILES_A))
    assert flat["disposition"] == "recorded"
    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        commit_audit = _audit(
            connection,
            principal_id=esc.PRINCIPAL,
            operation="engineering.source.capture.commit",
            now_us=170_000,
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_events "
            "(workspace_id, stream_id, sequence, snapshot_id, predecessor_sequence, "
            "predecessor_snapshot_id, manifest_json, manifest_digest, "
            "manifest_entry_count, event_digest, recorded_at_us, audit_ref, "
            "manifest_format) VALUES (?, ?, 2, 'csnap-captured-on-flat', 1, "
            "'esnap-flat-only', '{}', ?, 0, ?, ?, ?, 'captured_v1')",
            (
                WORKSPACE_ID,
                esc.STREAM,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                170_000,
                commit_audit,
            ),
        )


# --- an old rich snapshot with no origin/header cannot be promoted --------------------


def test_an_old_rich_snapshot_without_origin_or_header_cannot_be_promoted(
    workspace: esc.Workspace,
) -> None:
    """A pre-0056 snapshot recorded through the existing flat path has no stored
    checkout origin. It stays permanently ineligible for `captured_v1`: nothing
    here backfills or infers one."""
    flat = workspace.record(esc._source(1, "esnap-legacy", esc.FILES_A))
    assert flat["disposition"] == "recorded"
    connection = workspace.holder.connection
    legacy_digest = flat["manifest_digest"]
    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace):
        commit_audit = _audit(
            connection,
            principal_id=esc.PRINCIPAL,
            operation="engineering.source.capture.commit",
            now_us=180_000,
        )
        connection.execute(
            "UPDATE omnivia_engineering_source_streams SET announced_sequence = 2, "
            "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? AND stream_id = ?",
            (180_000, commit_audit, WORKSPACE_ID, esc.STREAM),
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_events "
            "(workspace_id, stream_id, sequence, snapshot_id, predecessor_sequence, "
            "predecessor_snapshot_id, manifest_json, manifest_digest, "
            "manifest_entry_count, event_digest, recorded_at_us, audit_ref, "
            "manifest_format) VALUES (?, ?, 2, 'esnap-legacy-promoted', 1, "
            "'esnap-legacy', '{}', ?, 0, ?, ?, ?, 'captured_v1')",
            (
                WORKSPACE_ID,
                esc.STREAM,
                legacy_digest,
                "sha256:" + "6" * 64,
                180_000,
                commit_audit,
            ),
        )


# --- the file-count bound -------------------------------------------------------------


def test_the_10000_file_count_bound_accepts_the_bound_and_rejects_10001(
    workspace: esc.Workspace,
) -> None:
    connection = workspace.holder.connection
    base_us = 190_000

    def _index(snapshot_id: str, count: int) -> SimpleNamespace:
        rich_manifest = {"mode": "captured_v1", "file_count": count}
        rich_digest = content_digest(canonical_document(rich_manifest))
        with _fenced(workspace):
            reg_audit = _audit(
                connection,
                principal_id="core-service",
                operation="engineering.repository.register",
                now_us=base_us,
                ref=f"aud-reg-{snapshot_id}",
            )
            settlement = SimpleNamespace(audit_ref=reg_audit)
            if (
                repository_identity.resolve_repository(
                    connection, workspace_id=WORKSPACE_ID, repository_id="erepo-bound"
                )
                is None
            ):
                repository_identity.register_repository(
                    connection,
                    settlement,
                    workspace_id=WORKSPACE_ID,
                    repository_id="erepo-bound",
                    display_name="erepo-bound",
                    provider_hint=None,
                    registered_at_us=base_us,
                )
                repository_identity.register_checkout(
                    connection,
                    settlement,
                    workspace_id=WORKSPACE_ID,
                    checkout_id="co-bound",
                    repository_id="erepo-bound",
                    installation_id="inst-1",
                    checkout_hint="/checkouts/bound",
                    registered_at_us=base_us,
                )
            capture_audit = _audit(
                connection,
                principal_id="core-service",
                operation="engineering.snapshot.capture",
                now_us=base_us + 1,
                ref=f"aud-capture-{snapshot_id}",
            )
            connection.execute(
                "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
                "content_length_bytes, created_at_us, verified_at_us) "
                "VALUES (?, ?, 2, ?, ?)",
                (WORKSPACE_ID, rich_digest, base_us + 1, base_us + 1),
            )
            evidence_id = f"evd-{snapshot_id}"
            connection.execute(
                "INSERT INTO omnivia_evidence_artifacts "
                "(evidence_id, workspace_id, source_kind, source_native_id, "
                "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
                "ingested_at_us, recorded_at_us, content_checksum, "
                "blob_content_digest, media_type, original_metadata_json, "
                "original_metadata_digest, sensitivity, parser_status, "
                "ingestion_status, staged_source_ref, import_run_id) VALUES "
                "(?, ?, 'document', ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, "
                "'application/json', '{}', ?, 'internal', 'not_applicable', "
                "'complete', NULL, NULL)",
                (
                    evidence_id,
                    WORKSPACE_ID,
                    f"working-tree-manifest.{snapshot_id}",
                    base_us + 1,
                    base_us + 1,
                    rich_digest,
                    rich_digest,
                    content_digest("{}"),
                ),
            )
            repository_identity.record_snapshot(
                connection,
                SimpleNamespace(audit_ref=capture_audit),
                workspace_id=WORKSPACE_ID,
                snapshot_id=snapshot_id,
                repository_id="erepo-bound",
                snapshot_kind="working_tree",
                manifest=rich_manifest,
                base_commit=None,
                capture_status="complete",
                captured_at_us=base_us + 1,
            )
            connection.executemany(
                "INSERT INTO omnivia_engineering_snapshot_files "
                "(workspace_id, snapshot_id, path, content_digest, audit_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        WORKSPACE_ID,
                        snapshot_id,
                        f"f/{i:06d}.py",
                        content_digest(f"f{i}"),
                        capture_audit,
                    )
                    for i in range(count)
                ),
            )
        return SimpleNamespace(
            evidence_id=evidence_id,
            rich_digest=rich_digest,
            capture_audit=capture_audit,
        )

    at_bound = _index("csnap-10000", 10_000)
    with _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures "
            "(workspace_id, snapshot_id, repository_id, installation_id, checkout_id, "
            "manifest_evidence_id, rich_manifest_digest, coverage_digest, file_count, "
            "capture_status, captured_at_us, audit_ref) "
            "VALUES (?, 'csnap-10000', 'erepo-bound', 'inst-1', 'co-bound', ?, ?, ?, "
            "10000, 'complete', ?, ?)",
            (
                WORKSPACE_ID,
                at_bound.evidence_id,
                at_bound.rich_digest,
                "sha256:" + "7" * 64,
                base_us + 1,
                at_bound.capture_audit,
            ),
        )

    over_bound = _index("csnap-10001", 10_001)
    with pytest.raises(sqlite3.DatabaseError, match="CHECK"), _fenced(workspace):
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures "
            "(workspace_id, snapshot_id, repository_id, installation_id, checkout_id, "
            "manifest_evidence_id, rich_manifest_digest, coverage_digest, file_count, "
            "capture_status, captured_at_us, audit_ref) "
            "VALUES (?, 'csnap-10001', 'erepo-bound', 'inst-1', 'co-bound', ?, ?, ?, "
            "10001, 'complete', ?, ?)",
            (
                WORKSPACE_ID,
                over_bound.evidence_id,
                over_bound.rich_digest,
                "sha256:" + "8" * 64,
                base_us + 1,
                over_bound.capture_audit,
            ),
        )


# --- a missing/inconsistent header fails closed and never produces `matched` ---------


def _raw_covered_snapshot_schema() -> sqlite3.Connection:
    """The exact columns `covered_snapshot` reads, with no guard triggers at all.

    The real, guarded schema cannot reach a `captured_v1` event with a missing or
    inconsistent header: the events-insert guard itself requires a matching
    sealed header before the event can exist. `covered_snapshot`'s own
    fail-closed branch is therefore defense in depth, tested directly here
    against a minimal, ungoverned copy of the four tables it joins.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE omnivia_engineering_source_streams (
            workspace_id TEXT, stream_id TEXT, repository_id TEXT,
            covered_sequence INTEGER
        );
        CREATE TABLE omnivia_engineering_source_events (
            workspace_id TEXT, stream_id TEXT, sequence INTEGER, snapshot_id TEXT,
            manifest_json TEXT, manifest_digest TEXT, manifest_format TEXT
        );
        CREATE TABLE omnivia_engineering_snapshots (
            workspace_id TEXT, snapshot_id TEXT, repository_id TEXT,
            capture_status TEXT
        );
        CREATE TABLE omnivia_engineering_snapshot_captures (
            workspace_id TEXT, snapshot_id TEXT, repository_id TEXT,
            rich_manifest_digest TEXT, capture_status TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO omnivia_engineering_source_streams VALUES "
        "('ws', 'stream-1', 'repo-1', 1)"
    )
    connection.execute(
        "INSERT INTO omnivia_engineering_source_events VALUES "
        "('ws', 'stream-1', 1, 'csnap-1', '{}', ?, 'captured_v1')",
        ("sha256:" + "a" * 64,),
    )
    connection.execute(
        "INSERT INTO omnivia_engineering_snapshots VALUES "
        "('ws', 'csnap-1', 'repo-1', 'complete')"
    )
    return connection


def test_a_missing_captured_header_fails_closed() -> None:
    connection = _raw_covered_snapshot_schema()
    assert (
        engineering_source.covered_snapshot(
            connection, workspace_id="ws", snapshot_id="csnap-1"
        )
        is None
    )


def test_an_inconsistent_captured_header_fails_closed_and_never_matches() -> None:
    for header in (
        # digest disagrees with the event's own manifest_digest
        ("ws", "csnap-1", "repo-1", "sha256:" + "b" * 64, "complete"),
        # repository disagrees with the stream's
        ("ws", "csnap-1", "repo-other", "sha256:" + "a" * 64, "complete"),
        # capture status disagrees with the snapshot's own
        ("ws", "csnap-1", "repo-1", "sha256:" + "a" * 64, "incomplete"),
    ):
        connection = _raw_covered_snapshot_schema()
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures VALUES (?, ?, ?, ?, ?)",
            header,
        )
        assert (
            engineering_source.covered_snapshot(
                connection, workspace_id="ws", snapshot_id="csnap-1"
            )
            is None
        ), header


def test_a_consistent_captured_header_resolves_with_no_hydrated_manifest() -> None:
    connection = _raw_covered_snapshot_schema()
    connection.execute(
        "INSERT INTO omnivia_engineering_snapshot_captures VALUES "
        "('ws', 'csnap-1', 'repo-1', ?, 'complete')",
        ("sha256:" + "a" * 64,),
    )
    covered = engineering_source.covered_snapshot(
        connection, workspace_id="ws", snapshot_id="csnap-1"
    )
    assert covered is not None
    assert covered.representation == "captured_v1"
    assert covered.manifest == {}
    assert covered.capture_status == "complete"
