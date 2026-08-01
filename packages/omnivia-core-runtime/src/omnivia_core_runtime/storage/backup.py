"""Verified backups and the installation-local state layout (T-0629C).

Backups use SQLite's online backup API rather than a file copy. A file copy of a
database with an active WAL can capture a torn state — the main file without its
pending WAL frames — which produces a backup that verifies as a file and fails as
a database. The backup API takes a consistent snapshot.

Every backup is verified before it is relied on, by integrity-checking it and
comparing its content inventory against the source. An unverified backup is not a
backup; it is a file that might be one.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import (
    DatabaseInventory,
    capture_inventory,
    compare_inventories,
)

BACKUPS_DIR = "backups"
RUNTIME_DIR = "runtime"
ATTEMPTS_DIR = "attempts"


class BackupError(StorageError):
    """A backup could not be created or could not be verified."""


@dataclass(frozen=True)
class InstallationLayout:
    """Installation-local state, deliberately outside the portable workspace.

    ```text
    installation-state/
    ├── backups/<workspace-id>/<attempt-id>/
    ├── attempts/<workspace-id>/
    └── runtime/<workspace-id>/
    ```

    Backups and attempt journals live here rather than inside the workspace for two
    reasons: copying a workspace must not copy its machine's backup history, and a
    migration attempt journal must survive the workspace being replaced.
    """

    root: Path

    def backups_for(self, workspace_id: str, attempt_id: str) -> Path:
        return self.root / BACKUPS_DIR / workspace_id / attempt_id

    def attempts_for(self, workspace_id: str) -> Path:
        return self.root / ATTEMPTS_DIR / workspace_id

    def runtime_for(self, workspace_id: str) -> Path:
        return self.root / RUNTIME_DIR / workspace_id

    def create(self, workspace_id: str) -> None:
        for path in (
            self.root / BACKUPS_DIR / workspace_id,
            self.attempts_for(workspace_id),
            self.runtime_for(workspace_id),
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class VerifiedBackup:
    """A backup that has been integrity-checked against its source."""

    path: Path
    source_inventory: DatabaseInventory
    backup_inventory: DatabaseInventory
    attempt_id: str

    @property
    def verified(self) -> bool:
        return (
            self.source_inventory.content_checksum
            == self.backup_inventory.content_checksum
        )


def new_attempt_id() -> str:
    """A fresh attempt identifier."""
    return f"attempt-{uuid.uuid4()}"


def backup_database(source: Path, destination: Path) -> Path:
    """Take a consistent snapshot of `source` at `destination`.

    Refuses to overwrite an existing destination: a backup routine that clobbers
    is a backup routine that can destroy the only remaining copy.
    """
    if not source.is_file():
        raise BackupError(f"no database to back up at {source}")
    if destination.exists():
        raise BackupError(f"refusing to overwrite an existing backup at {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Read-only source: backing up must never modify what it is protecting.
    source_connection = open_database(source, OpenMode.READ_ONLY)
    try:
        target = sqlite3.connect(str(destination))
        try:
            source_connection.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source_connection.close()
    return destination


def verify_backup(source: Path, backup: Path, attempt_id: str) -> VerifiedBackup:
    """Integrity-check a backup and compare its contents against the source."""
    source_connection = open_database(source, OpenMode.READ_ONLY)
    try:
        source_inventory = capture_inventory(source_connection)
    finally:
        source_connection.close()

    backup_connection = open_database(backup, OpenMode.READ_ONLY)
    try:
        problems = integrity_check(backup_connection)
        if problems:
            raise BackupError(f"backup failed its integrity check: {problems}")
        backup_inventory = capture_inventory(backup_connection)
    finally:
        backup_connection.close()

    differences = compare_inventories(source_inventory, backup_inventory)
    if differences:
        raise BackupError("backup does not match its source: " + "; ".join(differences))

    result = VerifiedBackup(
        path=backup,
        source_inventory=source_inventory,
        backup_inventory=backup_inventory,
        attempt_id=attempt_id,
    )
    if not result.verified:  # pragma: no cover - compare_inventories covers this
        raise BackupError("backup checksum does not match its source")
    return result


def create_verified_backup(
    source: Path,
    installation: InstallationLayout,
    *,
    workspace_id: str,
    attempt_id: str,
    name: str = "source.sqlite",
) -> VerifiedBackup:
    """Back up `source` into installation-local state and verify it."""
    directory = installation.backups_for(workspace_id, attempt_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    backup_database(source, destination)
    return verify_backup(source, destination, attempt_id)


def restore_backup(backup: Path, destination: Path) -> Path:
    """Restore a verified backup over `destination`.

    Written to a temporary sibling and renamed, so an interrupted restore cannot
    leave a half-written database where a working one used to be.
    """
    if not backup.is_file():
        raise BackupError(f"no backup to restore at {backup}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.restore")
    if staging.exists():
        staging.unlink()

    backup_connection = open_database(backup, OpenMode.READ_ONLY)
    try:
        target = sqlite3.connect(str(staging))
        try:
            backup_connection.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        backup_connection.close()

    # Remove WAL sidecars belonging to the database being replaced; leaving them
    # would let stale frames be replayed over the restored file.
    for suffix in ("-wal", "-shm"):
        sidecar = destination.with_name(destination.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    staging.replace(destination)
    return destination


__all__ = [
    "ATTEMPTS_DIR",
    "BACKUPS_DIR",
    "RUNTIME_DIR",
    "BackupError",
    "InstallationLayout",
    "VerifiedBackup",
    "backup_database",
    "create_verified_backup",
    "new_attempt_id",
    "restore_backup",
    "verify_backup",
]
