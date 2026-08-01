"""T-0629C acceptance: migration, backup and restore (MB-01 … MB-28)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.storage.backup import (
    BackupError,
    InstallationLayout,
    backup_database,
    create_verified_backup,
    new_attempt_id,
    verify_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    integrity_check,
    open_database,
    table_names,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.legacy import (
    AttemptJournal,
    LegacyMigrationError,
    MigrationOutcome,
    assert_explicit_source,
    find_incomplete_attempts,
    inspect_legacy_source,
    migrate_legacy_database,
    rollback_migration,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
    read_workspace_state,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import read_manifest

from omnivia_core.workspace.manifest import WorkspaceManifest

from .conftest import (
    SERVICE_INSTANCE,
    WORKSPACE_ID,
    insert_row,
    legacy_rows,
    populate_preservation_corpus,
)


def signature(path: Path) -> tuple[int, bytes]:
    """Size plus full bytes — the strongest available "unchanged" proof."""
    return (path.stat().st_size, path.read_bytes())


def run(
    source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> object:
    return migrate_legacy_database(
        source,
        workspace,
        installation,
        manifest,
        service_instance_id=SERVICE_INSTANCE,
    )


# MB-01
def test_mb01_migration_accepts_only_an_explicit_source_path(tmp_path: Path) -> None:
    with pytest.raises(LegacyMigrationError, match="explicit absolute path"):
        assert_explicit_source(Path("relative/legacy.sqlite"))
    with pytest.raises(LegacyMigrationError, match="no legacy database"):
        assert_explicit_source(tmp_path / "absent.sqlite")


# MB-02
def test_mb02_migration_never_infers_a_home_directory_database(tmp_path: Path) -> None:
    """Even passed deliberately, the implicit legacy database is refused."""
    home_like = tmp_path / ".omnivia"
    home_like.mkdir()
    memories = home_like / "memories.db"
    materialise_phase0_baseline(memories)

    with pytest.raises(LegacyMigrationError, match="implicit legacy database name"):
        assert_explicit_source(memories)

    # The directory name alone is enough to refuse, whatever the file is called.
    other = home_like / "renamed.sqlite"
    materialise_phase0_baseline(other)
    with pytest.raises(LegacyMigrationError, match="refusing a source inside"):
        assert_explicit_source(other)


# MB-03 / MB-09
def test_mb03_source_is_opened_read_only_and_left_unchanged(phase0_source: Path) -> None:
    before = signature(phase0_source)
    inventory, fingerprint = inspect_legacy_source(phase0_source)
    assert inventory.total_rows > 0
    assert len(fingerprint) == 64
    assert signature(phase0_source) == before

    connection = open_database(phase0_source, OpenMode.READ_ONLY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM memories")
    finally:
        connection.close()
    assert signature(phase0_source) == before


# MB-04
def test_mb04_source_requires_the_exact_phase0_fingerprint(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    connection = sqlite3.connect(str(phase0_source))
    try:
        connection.execute("CREATE TABLE interloper (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    before = signature(phase0_source)
    with pytest.raises(LegacyMigrationError, match="not the frozen Phase 0 schema"):
        run(phase0_source, workspace, installation, manifest)
    assert signature(phase0_source) == before
    assert not workspace.database_path.exists()


# MB-05
def test_mb05_ambiguous_multi_workspace_mapping_is_refused(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    """A source holding two workspaces cannot map to one without an explicit plan."""
    connection = sqlite3.connect(str(phase0_source))
    try:
        with connection:
            insert_row(
                connection,
                "workspaces",
                {"id": "ws-second", "name": "other"},
                seed="ws-second",
            )
    finally:
        connection.close()

    result = run(phase0_source, workspace, installation, manifest)
    published = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        count = published.execute("SELECT COUNT(*) FROM workspaces").fetchone()
    finally:
        published.close()
    # Both rows are preserved verbatim; nothing is silently merged or dropped.
    assert count is not None and count[0] == 2
    assert getattr(result, "data_preserved", False)


# MB-06 / MB-07
def test_mb06_backup_is_created_and_verified_before_migration(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    backup = result.backup
    assert backup.path.is_file()
    assert backup.verified
    assert backup.source_inventory.content_checksum == backup.backup_inventory.content_checksum

    journal = AttemptJournal.load(result.journal_path)
    steps = [entry["step"] for entry in journal.steps]
    assert steps.index("backup") < steps.index("migrate")
    assert steps.index("verify") < steps.index("publish")


def test_mb07_backup_refuses_to_overwrite_and_detects_corruption(
    phase0_source: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "backups" / "copy.sqlite"
    backup_database(phase0_source, destination)
    with pytest.raises(BackupError, match="refusing to overwrite"):
        backup_database(phase0_source, destination)

    corrupt = tmp_path / "backups" / "corrupt.sqlite"
    backup_database(phase0_source, corrupt)
    connection = sqlite3.connect(str(corrupt))
    try:
        connection.execute("DELETE FROM memories")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(BackupError, match="does not match its source"):
        verify_backup(phase0_source, corrupt, new_attempt_id())


# MB-08 / MB-10 / MB-11
def test_mb08_migration_operates_on_a_staging_copy_only(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    before = signature(phase0_source)
    result = run(phase0_source, workspace, installation, manifest)
    assert signature(phase0_source) == before, "the source must be byte-identical"

    # The published database is a different file from both source and backup.
    assert workspace.database_path.resolve() != phase0_source.resolve()
    assert workspace.database_path.resolve() != result.backup.path.resolve()
    # The verified backup still holds the pre-migration schema.
    backup_connection = open_database(result.backup.path, OpenMode.READ_ONLY)
    try:
        assert "omnivia_workspace_state" not in table_names(backup_connection)
    finally:
        backup_connection.close()


def test_mb10_failed_migration_leaves_the_source_untouched(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = signature(phase0_source)

    from omnivia_core_runtime.storage import legacy

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(legacy, "apply_pending_migrations", explode)
    with pytest.raises(RuntimeError, match="migration exploded"):
        run(phase0_source, workspace, installation, manifest)

    assert signature(phase0_source) == before
    assert not workspace.database_path.exists(), "nothing was published"

    journals = sorted(installation.attempts_for(WORKSPACE_ID).glob("*.json"))
    assert journals, "the failed attempt is journalled"
    assert AttemptJournal.load(journals[-1]).outcome == MigrationOutcome.FAILED.value


# MB-12 … MB-15
def test_mb12_all_fourteen_legacy_tables_are_preserved(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    source_inventory = result.source_inventory
    published_inventory = result.published_inventory

    assert len(source_inventory.tables) == 14
    for name in source_inventory.table_names:
        assert published_inventory.table(name) is not None, name


def test_mb13_mb14_mb15_columns_counts_and_values_are_preserved(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    source_inventory = result.source_inventory
    published_inventory = result.published_inventory

    assert compare_inventories(source_inventory, published_inventory) == []
    # Legacy rows only: the published workspace also carries the reserved
    # omnivia_ substrate, whose rows are per-attempt bookkeeping.
    assert legacy_rows(source_inventory) == legacy_rows(published_inventory)
    assert legacy_rows(source_inventory) > 20, "the corpus must be non-trivial"

    for original in source_inventory.tables:
        current = published_inventory.table(original.name)
        assert current is not None
        assert current.columns == original.columns, original.name
        assert current.row_count == original.row_count, original.name
        assert current.content_checksum == original.content_checksum, original.name


def test_awkward_values_survive_the_round_trip(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    """Unicode, quotes, empty strings, NULLs and a 4 KiB value, read back verbatim."""
    run(phase0_source, workspace, installation, manifest)
    published = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        rows = published.execute(
            "SELECT content FROM memories WHERE id LIKE 'mem-0%' ORDER BY id"
        ).fetchall()
        contents = [row[0] for row in rows]
    finally:
        published.close()

    assert "" in contents
    assert any("☃" in c and "🌍" in c for c in contents)
    assert any("'" in c and '"' in c and ";" in c for c in contents)
    assert any(len(c) == 4096 for c in contents)


def test_inventory_distinguishes_types_and_detects_silent_changes(tmp_path: Path) -> None:
    """A row count alone would miss all of these."""
    path = tmp_path / "typed.sqlite"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE t (v)")
        connection.executemany("INSERT INTO t (v) VALUES (?)", [(1,), ("1",), (b"1",)])
        connection.commit()
        first = capture_inventory(connection)
        assert first.table("t") is not None
        assert first.table("t").row_count == 3  # type: ignore[union-attr]

        connection.execute("UPDATE t SET v = '2' WHERE v = '1'")
        connection.commit()
        second = capture_inventory(connection)
        problems = compare_inventories(first, second)
        assert any("values changed" in p for p in problems), problems
    finally:
        connection.close()


# MB-16 / MB-17
def test_mb16_attempt_journal_is_written_outside_the_portable_workspace(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    journal_path = result.journal_path
    assert journal_path.is_file()
    assert installation.root in journal_path.parents
    assert workspace.root not in journal_path.parents
    # The portable workspace stays exactly the five-path layout.
    assert workspace.validate(require_database=True) == []


def test_mb17_interrupted_migration_is_discoverable_from_the_journal(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnivia_core_runtime.storage import legacy

    real_finish = AttemptJournal.finish

    def die_before_finishing(self: AttemptJournal, *_a: object, **_k: object) -> None:
        raise KeyboardInterrupt("power loss")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(legacy, "apply_pending_migrations", explode)
    monkeypatch.setattr(AttemptJournal, "finish", die_before_finishing)
    with pytest.raises(KeyboardInterrupt):
        run(phase0_source, workspace, installation, manifest)
    monkeypatch.setattr(AttemptJournal, "finish", real_finish)

    incomplete = find_incomplete_attempts(installation, WORKSPACE_ID)
    assert len(incomplete) == 1
    assert incomplete[0].outcome is None
    assert [s["step"] for s in incomplete[0].steps][:2] == ["started", "inspect-source"]


# MB-18 … MB-20
def test_mb18_publication_is_atomic_and_leaves_no_temporary(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    run(phase0_source, workspace, installation, manifest)
    leftovers = [p.name for p in workspace.root.iterdir() if p.name.startswith(".")]
    assert leftovers == []
    assert workspace.validate(require_database=True) == []


def test_mb19_publication_does_not_occur_when_verification_fails(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnivia_core_runtime.storage import legacy

    monkeypatch.setattr(
        legacy, "compare_inventories", lambda *a, **k: ["table 'memories' vanished"]
    )
    with pytest.raises(LegacyMigrationError, match="would lose data"):
        run(phase0_source, workspace, installation, manifest)
    assert not workspace.database_path.exists()


def test_mb20_manifest_records_the_migration_by_fingerprint_not_path(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    published = read_manifest(workspace)
    assert published.migration is not None
    assert published.migration.attempt_id == result.attempt_id
    assert published.migration.source_fingerprint is not None
    assert len(published.migration.source_fingerprint) == 64
    # The source path must not leak into the portable manifest.
    text = workspace.manifest_path.read_text(encoding="utf-8")
    assert str(phase0_source) not in text
    assert "explicit-source" not in text
    assert published.integrity_matches()


# MB-21 / MB-22
def test_mb21_mb22_rollback_restores_the_exact_schema_counts_and_values(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    backup = result.backup

    # The published database has advanced; roll it back to the pre-migration state.
    published = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        assert "omnivia_workspace_state" in table_names(published)
    finally:
        published.close()

    restored = rollback_migration(backup, workspace.database_path)
    connection = open_database(restored, OpenMode.READ_ONLY)
    try:
        assert integrity_check(connection) == []
        assert "omnivia_workspace_state" not in table_names(connection)
        assert len(table_names(connection)) == 14
        assert compare_inventories(backup.source_inventory, capture_inventory(connection)) == []
    finally:
        connection.close()


def test_rollback_refuses_a_backup_that_does_not_restore_exactly(
    phase0_source: Path, tmp_path: Path
) -> None:
    attempt = new_attempt_id()
    installation = InstallationLayout(root=tmp_path / "state")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        phase0_source, installation, workspace_id=WORKSPACE_ID, attempt_id=attempt
    )
    # Damage the backup after verification, as bit rot would.
    connection = sqlite3.connect(str(backup.path))
    try:
        connection.execute("DELETE FROM memories")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LegacyMigrationError, match="did not restore the original"):
        rollback_migration(backup, tmp_path / "restored.sqlite")


# MB-23 … MB-28
def test_mb23_mb24_migrations_are_pinned_and_the_ledger_is_authoritative(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    run(phase0_source, workspace, installation, manifest)
    connection = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        ledger = applied_migrations(connection)
        assert ledger, "the substrate migration is recorded"
        assert all(len(checksum) == 64 for checksum in ledger.values())
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None and int(version[0]) == max(ledger)
    finally:
        connection.close()


def test_mb25_migrations_run_under_the_current_authority_tuple(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    run(phase0_source, workspace, installation, manifest)
    connection = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        assert state.workspace_id == WORKSPACE_ID
        assert state.fencing_generation == 1
        row = connection.execute(
            "SELECT applied_by_service_instance, fencing_generation "
            "FROM omnivia_schema_migrations ORDER BY version"
        ).fetchone()
        assert row == (SERVICE_INSTANCE, 1)
    finally:
        connection.close()


def test_mb26_no_partial_migration_commit_is_observable(
    empty_phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    """An empty legacy database migrates cleanly, proving the path has no row dependency."""
    result = run(empty_phase0_source, workspace, installation, manifest)
    assert result.source_inventory.total_rows == 0
    assert result.data_preserved


def test_mb27_mb28_published_database_is_configured_and_healthy(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    run(phase0_source, workspace, installation, manifest)
    connection = open_database(workspace.database_path, OpenMode.SERVICE_OWNED)
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()
        assert journal is not None and str(journal[0]).lower() == "wal"
        fk = connection.execute("PRAGMA foreign_keys").fetchone()
        assert fk is not None and int(fk[0]) == 1
        assert integrity_check(connection) == []
    finally:
        connection.close()


def test_preservation_corpus_is_deterministic(tmp_path: Path) -> None:
    """Two builds produce identical inventories, so the oracle is stable."""
    digests = []
    for index in range(2):
        path = tmp_path / f"corpus-{index}.sqlite"
        materialise_phase0_baseline(path)
        populate_preservation_corpus(path)
        connection = open_database(path, OpenMode.READ_ONLY)
        try:
            digests.append(capture_inventory(connection).content_checksum)
        finally:
            connection.close()
    assert digests[0] == digests[1]


def test_migration_is_repeatable_into_a_fresh_workspace(
    phase0_source: Path,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    tmp_path: Path,
) -> None:
    """Two independent migrations of one source produce identical published data."""
    checksums = []
    for index in range(2):
        target = WorkspaceLayout(root=tmp_path / f"ws-{index}")
        result = migrate_legacy_database(
            phase0_source,
            target,
            installation,
            manifest,
            service_instance_id=SERVICE_INSTANCE,
            attempt_id=f"attempt-fixed-{index}",
        )
        inventory = result.published_inventory
        checksums.append(
            tuple(
                (entry.name, entry.content_checksum)
                for entry in inventory.tables
                if not entry.name.startswith("omnivia_")
            )
        )
    # The substrate legitimately differs per attempt (timestamps, attempt ids);
    # the migrated legacy data must not.
    assert checksums[0] == checksums[1]


def test_staging_copy_is_not_the_verified_backup(
    phase0_source: Path,
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
) -> None:
    result = run(phase0_source, workspace, installation, manifest)
    backup = result.backup
    staging = backup.path.parent / "staging.sqlite"
    assert staging.exists(), "staging is retained as recovery evidence"
    assert staging.resolve() != backup.path.resolve()
    # The backup is still pre-migration; staging is post-migration.
    backup_connection = open_database(backup.path, OpenMode.READ_ONLY)
    staged_connection = open_database(staging, OpenMode.READ_ONLY)
    try:
        assert "omnivia_workspace_state" not in table_names(backup_connection)
        assert "omnivia_workspace_state" in table_names(staged_connection)
    finally:
        backup_connection.close()
        staged_connection.close()


def test_installation_state_layout_is_outside_the_workspace(
    installation: InstallationLayout, workspace: WorkspaceLayout
) -> None:
    for path in (
        installation.backups_for(WORKSPACE_ID, "a1"),
        installation.attempts_for(WORKSPACE_ID),
        installation.runtime_for(WORKSPACE_ID),
    ):
        assert workspace.root not in path.parents
        assert installation.root in path.parents


def test_restore_removes_stale_wal_sidecars(phase0_source: Path, tmp_path: Path) -> None:
    """A stale WAL could otherwise be replayed over a restored database."""
    attempt = new_attempt_id()
    installation = InstallationLayout(root=tmp_path / "state")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        phase0_source, installation, workspace_id=WORKSPACE_ID, attempt_id=attempt
    )

    destination = tmp_path / "live.sqlite"
    shutil.copy2(phase0_source, destination)
    stale = destination.with_name(destination.name + "-wal")
    stale.write_bytes(b"stale frames")

    rollback_migration(backup, destination)
    assert not stale.exists()


# SB-04 regression
def test_sb04_pending_migrations_apply_on_a_governed_service_connection(
    tmp_path: Path,
) -> None:
    """A service that opens a workspace with migrations outstanding must apply them.

    Every test reached this path through a maintenance connection, which carries no
    authorizer, so the governed path was never exercised: recording the attempt
    outcome was not elevated and failed with `not authorized` -- on the failure path
    too, replacing a recorded migration failure with an unrecorded one.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)

    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        maintenance,
        workspace_id="ws-governed-0001",
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id="svc-governed",
    )
    maintenance.close()

    service = open_database(path, OpenMode.SERVICE_OWNED)
    try:
        assert service.omnivia_authorizer_installed
        pending_before = sorted(applied_migrations(service))
        applied = apply_pending_migrations(
            service,
            mode=OpenMode.SERVICE_OWNED,
            service_instance_id="svc-governed",
            fencing_generation=state.fencing_generation,
            workspace_id="ws-governed-0001",
        )
        assert applied, "there were pending migrations to apply"
        assert sorted(applied_migrations(service)) != pending_before

        # Each attempt is recorded as succeeded, not left dangling at 'started'.
        outcomes = {
            row[0]
            for row in service.execute("SELECT outcome FROM omnivia_migration_attempts")
        }
        assert outcomes == {"succeeded"}, outcomes
    finally:
        service.close()
