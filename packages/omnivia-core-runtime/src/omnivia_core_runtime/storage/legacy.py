"""Copy-only legacy migration (T-0629C, ADR-037).

The source is never migrated. It is opened read-only, fingerprinted, backed up,
and then a *copy* is migrated in staging and published atomically. A failure at
any point leaves the legacy database byte-identical and at least one verified
recovery copy in place.

The source path is always explicit. `~/.omnivia/memories.db` is never inferred,
and is refused outright even when passed deliberately, because a migration that
can find the implicit home database by accident is one command-line typo away
from migrating the wrong thing.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from omnivia_core.workspace.manifest import MigrationSummary, WorkspaceManifest
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    VerifiedBackup,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import (
    DatabaseInventory,
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    phase0_fingerprint,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import write_manifest

#: Names that must never be accepted as a migration source, however they arrive.
FORBIDDEN_SOURCE_NAMES = frozenset({"memories.db"})
FORBIDDEN_SOURCE_PARENTS = frozenset({".omnivia"})


class MigrationOutcome(str, Enum):
    """Terminal state of one migration attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class LegacyMigrationError(StorageError):
    """A legacy migration was refused or failed."""


@dataclass
class AttemptJournal:
    """Durable record of one migration attempt, outside the portable workspace.

    Written before each step rather than after, so an interrupted migration is
    discoverable on restart. A journal that records only completed steps cannot
    distinguish "never started" from "died half way".
    """

    path: Path
    attempt_id: str
    workspace_id: str
    source: Path
    steps: list[dict[str, str]] = field(default_factory=list)
    outcome: str | None = None

    def record(self, step: str, detail: str = "") -> None:
        self.steps.append(
            {
                "step": step,
                "detail": detail,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        self.flush()

    def finish(self, outcome: MigrationOutcome, detail: str = "") -> None:
        self.outcome = outcome.value
        self.record(f"outcome:{outcome.value}", detail)

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "attempt_id": self.attempt_id,
            "workspace_id": self.workspace_id,
            # The source path is installation-local state, which is exactly why the
            # journal lives outside the portable workspace and the manifest records
            # only a fingerprint.
            "source": str(self.source),
            "outcome": self.outcome,
            "steps": self.steps,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> AttemptJournal:
        data = json.loads(path.read_text(encoding="utf-8"))
        journal = cls(
            path=path,
            attempt_id=str(data["attempt_id"]),
            workspace_id=str(data["workspace_id"]),
            source=Path(str(data["source"])),
            steps=list(data.get("steps") or []),
        )
        journal.outcome = data.get("outcome")
        return journal


@dataclass(frozen=True)
class MigrationResult:
    """What a completed migration did."""

    attempt_id: str
    workspace_id: str
    backup: VerifiedBackup
    source_inventory: DatabaseInventory
    published_inventory: DatabaseInventory
    journal_path: Path
    published_to: Path

    @property
    def data_preserved(self) -> bool:
        return not compare_inventories(self.source_inventory, self.published_inventory)


def assert_explicit_source(source: Path) -> Path:
    """Refuse an implicit or home-directory source.

    ADR-037's legacy-migration safety list starts with "never infer or access a
    home-directory database". The legacy `get_database()` created and opened
    `~/.omnivia/memories.db` as a side effect of a getter; nothing here may reach
    that path even when asked to.
    """
    if not source.is_absolute():
        raise LegacyMigrationError(
            f"legacy source must be an explicit absolute path, got {source}"
        )
    resolved = source.resolve()
    if resolved.name in FORBIDDEN_SOURCE_NAMES:
        raise LegacyMigrationError(
            f"refusing the implicit legacy database name {resolved.name!r}; "
            "copy it to an explicit path first"
        )
    if any(part in FORBIDDEN_SOURCE_PARENTS for part in resolved.parts):
        raise LegacyMigrationError(
            f"refusing a source inside {sorted(FORBIDDEN_SOURCE_PARENTS)}: {resolved}"
        )
    if not resolved.is_file():
        raise LegacyMigrationError(f"no legacy database at {resolved}")
    return resolved


def inspect_legacy_source(source: Path) -> tuple[DatabaseInventory, str]:
    """Read-only inspection of the legacy source, returning its inventory and fingerprint."""
    resolved = assert_explicit_source(source)
    before = _file_signature(resolved)

    connection = open_database(resolved, OpenMode.READ_ONLY)
    try:
        problems = integrity_check(connection)
        if problems:
            raise LegacyMigrationError(f"legacy source fails integrity check: {problems}")
        inventory = capture_inventory(connection)
        fingerprint = fingerprint_schema(connection).digest
    finally:
        connection.close()

    if _file_signature(resolved) != before:
        raise LegacyMigrationError(
            "inspection modified the legacy source; this must never happen"
        )
    return inventory, fingerprint


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def migrate_legacy_database(
    source: Path,
    layout: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    *,
    service_instance_id: str,
    require_phase0_fingerprint: bool = True,
    attempt_id: str | None = None,
) -> MigrationResult:
    """Migrate a copy of `source` into `layout`, never touching the source.

    Sequence, in order, each step journalled before it runs:

    1. refuse an implicit source; inspect read-only and fingerprint it
    2. create and verify a complete backup
    3. copy the backup into staging — never the source, never the sole backup
    4. bootstrap generation 1 on staging, adopting the Phase 0 baseline
    5. apply pending migrations under the current authority tuple
    6. prove tables, columns, row counts and values are unchanged
    7. integrity-check staging, then publish it atomically
    8. write the manifest recording the migration by fingerprint
    """
    workspace_id = manifest.workspace_id
    attempt = attempt_id or new_attempt_id()
    installation.create(workspace_id)
    journal = AttemptJournal(
        path=installation.attempts_for(workspace_id) / f"{attempt}.json",
        attempt_id=attempt,
        workspace_id=workspace_id,
        source=source,
    )
    journal.record("started")

    try:
        journal.record("inspect-source")
        source_inventory, fingerprint = inspect_legacy_source(source)
        resolved_source = assert_explicit_source(source)
        source_before = _file_signature(resolved_source)

        if require_phase0_fingerprint and fingerprint != phase0_fingerprint():
            raise LegacyMigrationError(
                "legacy source is not the frozen Phase 0 schema "
                f"(expected {phase0_fingerprint()[:12]}…, got {fingerprint[:12]}…)"
            )

        journal.record("backup")
        backup = create_verified_backup(
            resolved_source,
            installation,
            workspace_id=workspace_id,
            attempt_id=attempt,
        )

        journal.record("stage")
        staging = installation.backups_for(workspace_id, attempt) / "staging.sqlite"
        if staging.exists():
            staging.unlink()
        # Copy the *backup*, so neither the source nor the sole recovery copy is
        # ever the thing being mutated.
        shutil.copy2(backup.path, staging)

        journal.record("bootstrap")
        staged = open_database(staging, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = bootstrap_generation_one(
                staged,
                workspace_id=workspace_id,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=require_phase0_fingerprint,
                service_instance_id=service_instance_id,
            )
            journal.record("migrate", f"generation={state.fencing_generation}")
            apply_pending_migrations(
                staged,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=service_instance_id,
                fencing_generation=state.fencing_generation,
                workspace_id=workspace_id,
            )

            journal.record("verify")
            staged_inventory = capture_inventory(staged)
            differences = compare_inventories(source_inventory, staged_inventory)
            if differences:
                raise LegacyMigrationError(
                    "migration would lose data: " + "; ".join(differences)
                )
            problems = integrity_check(staged)
            if problems:
                raise LegacyMigrationError(f"staged database fails integrity: {problems}")
        finally:
            staged.close()

        journal.record("publish")
        layout.create_directories()
        _publish(staging, layout.database_path)

        published = open_database(layout.database_path, OpenMode.READ_ONLY)
        try:
            published_inventory = capture_inventory(published)
        finally:
            published.close()

        journal.record("manifest")
        write_manifest(
            layout,
            WorkspaceManifest(
                workspace_id=manifest.workspace_id,
                created_at=manifest.created_at,
                compatibility=manifest.compatibility,
                name=manifest.name,
                projections=manifest.projections,
                encryption=manifest.encryption,
                migration=MigrationSummary(
                    from_format_version="0",
                    to_format_version=manifest.compatibility.workspace_format_version,
                    migrated_at=datetime.now(UTC).isoformat(),
                    attempt_id=attempt,
                    source_fingerprint=fingerprint,
                ),
            ),
        )

        if _file_signature(resolved_source) != source_before:
            raise LegacyMigrationError(
                "the legacy source changed during migration; this must never happen"
            )

        journal.finish(MigrationOutcome.SUCCEEDED)
        return MigrationResult(
            attempt_id=attempt,
            workspace_id=workspace_id,
            backup=backup,
            source_inventory=source_inventory,
            published_inventory=published_inventory,
            journal_path=journal.path,
            published_to=layout.database_path,
        )
    except Exception as exc:
        journal.finish(MigrationOutcome.FAILED, str(exc)[:500])
        raise


def _publish(staging: Path, destination: Path) -> None:
    """Atomically move a staged database into place."""
    for suffix in ("-wal", "-shm"):
        sidecar = staging.with_name(staging.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    temporary = destination.with_name(f".{destination.name}.publish")
    shutil.copy2(staging, temporary)
    temporary.replace(destination)


def rollback_migration(
    backup: VerifiedBackup,
    destination: Path,
    journal: AttemptJournal | None = None,
) -> Path:
    """Restore the verified backup, returning the database to its pre-migration state."""
    restored = restore_backup(backup.path, destination)
    connection = open_database(restored, OpenMode.READ_ONLY)
    try:
        problems = integrity_check(connection)
        if problems:
            raise LegacyMigrationError(f"restored database fails integrity: {problems}")
        differences = compare_inventories(
            backup.source_inventory, capture_inventory(connection)
        )
        if differences:
            raise LegacyMigrationError(
                "rollback did not restore the original exactly: " + "; ".join(differences)
            )
    finally:
        connection.close()

    if journal is not None:
        journal.finish(MigrationOutcome.ROLLED_BACK, str(restored))
    return restored


def find_incomplete_attempts(
    installation: InstallationLayout, workspace_id: str
) -> list[AttemptJournal]:
    """Attempt journals that never reached a terminal outcome."""
    directory = installation.attempts_for(workspace_id)
    if not directory.is_dir():
        return []
    incomplete: list[AttemptJournal] = []
    for path in sorted(directory.glob("*.json")):
        try:
            journal = AttemptJournal.load(path)
        except (OSError, ValueError, KeyError):
            continue
        if journal.outcome is None:
            incomplete.append(journal)
    return incomplete


def new_workspace_id() -> str:
    """A fresh workspace identifier."""
    return f"ws-{uuid.uuid4()}"


__all__ = [
    "FORBIDDEN_SOURCE_NAMES",
    "FORBIDDEN_SOURCE_PARENTS",
    "AttemptJournal",
    "LegacyMigrationError",
    "MigrationOutcome",
    "MigrationResult",
    "assert_explicit_source",
    "find_incomplete_attempts",
    "inspect_legacy_source",
    "migrate_legacy_database",
    "new_workspace_id",
    "rollback_migration",
]
