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

import os
import re
import sqlite3
import subprocess
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
CATALOGUE_DIR = "catalogue"

#: The installation-owned catalogue database and the lifetime lock that guards it. Both
#: live in `catalogue/`, and neither is per-workspace: every other directory here is
#: keyed by a workspace id because it holds facts *about* one workspace, whereas the
#: catalogue holds the installation's own identity and the inventory of the workspaces
#: it authorised. Keying it by workspace would mint an installation identity per
#: workspace, which is the failure the separation exists to prevent.
INSTALLATION_DATABASE = "installation.sqlite"
INSTALLATION_LOCK = "installation.lock"

_IS_WINDOWS = os.name == "nt"

#: `whoami /user` reports the SID in this form, mixed into a CSV row. The same
#: closed grammar `omnivia_core_client.owner_private` and this package's own
#: `ownership/discovery.py` parse it with.
_SID_RE = re.compile(r"S-1-[0-9-]+")

#: Full control for the owner alone, inherited by anything created beneath the
#: root -- the closest Windows has to leaving a POSIX `mkdir`'s mode untouched.
_WINDOWS_ROOT_RIGHTS = "(OI)(CI)F"

#: What a freshly created installation-state root is made with, at the `mkdir`
#: syscall itself rather than left to its default 0o777 filtered by whatever
#: umask this process happens to run under: a permissive umask (0) would
#: otherwise hand a fresh root group- or world-writable, off Windows, where
#: nothing later in `_ensure_root` tightens it. The same value
#: `omnivia_core_client.owner_private` gives its own freshly created
#: directories, for the same reason.
_ROOT_MODE = 0o700

#: Fixed and path-free, for the one failure `_ensure_root` raises directly.
#: This reaches a caller through `workspace_init`'s public refusal reason, and
#: that surface carries no path, no SID and no subprocess output -- R004-10
#: requires it free of exactly this kind of payload.
_ROOT_RESTRICTION_FAILURE = (
    "could not restrict a newly created installation-state root to its owner"
)


class BackupError(StorageError):
    """A backup could not be created or could not be verified."""


def _system32(program: str) -> str:
    """An absolute path to a Windows system tool, never resolved through PATH."""
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", program))


def _windows_restrict_root(path: Path) -> bool:
    """The Windows mechanism for :func:`_restrict_root_to_owner`, unconditionally.

    `icacls`, not `ctypes`, and the same three-invocation sequence
    `omnivia_core_client.owner_private` and this package's own
    `ownership/discovery.py` use, repeated here rather than imported: this
    package declares a dependency on `omnivia-core` alone, and
    `omnivia-core-client` is a sibling distribution, not one of them --
    reaching into it is exactly the edge `scripts/check-package-boundaries.py`
    exists to keep closed. `/setowner` first, because ownership comes from the
    token rather than the DACL; `/reset` drops explicit entries `/inheritance:r`
    does not touch; `/inheritance:r` with `/grant:r` drops the inherited entries
    too and leaves one allow ACE naming this process's own SID, read from
    `whoami /user`'s closed CSV grammar. Every step runs in order and the first
    failure ends the sequence.
    """
    try:
        identity = subprocess.run(
            [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        found = _SID_RE.search(identity.stdout) if identity.returncode == 0 else None
        if found is None:
            return False
        sid = found.group()
        for arguments in (
            ("/setowner", f"*{sid}"),
            ("/reset",),
            ("/inheritance:r", "/grant:r", f"*{sid}:{_WINDOWS_ROOT_RIGHTS}"),
        ):
            completed = subprocess.run(
                [_system32("icacls.exe"), str(path), *arguments, "/q"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                return False
    except Exception:  # noqa: BLE001 -- platform writer must fail closed.
        return False
    return True


def _restrict_root_to_owner(path: Path) -> bool:
    """Reduce a freshly created installation-state root to an owner-only DACL.

    A no-op success off Windows: `_ensure_root` creates the root at an
    explicit, restrictive mode a permissive umask cannot widen, so there is
    nothing further to enforce there. On Windows a brand new directory
    inherits whatever DACL its parent's inheritance supplies -- routinely
    SYSTEM or the local administrators, on a hosted runner's temp tree -- and
    every store under `runtime/` proves this exact root out with the parent
    policy (owned by this user, writable by nobody else) before it creates
    anything beneath it. Left alone, that first proof is what
    `omnivia_core_client.installed_credentials` fails on, before it ever
    reaches the `runtime/` component publication already restricts.

    Fails closed, including when the native tool itself does not complete: an
    installation root this call could not restrict is never treated as
    restricted merely because nothing has proved otherwise yet.
    """
    if not _IS_WINDOWS:
        return True
    return _windows_restrict_root(path)


@dataclass(frozen=True)
class InstallationLayout:
    """Installation-local state, deliberately outside the portable workspace.

    ```text
    installation-state/
    ├── backups/<workspace-id>/<attempt-id>/
    ├── attempts/<workspace-id>/
    ├── catalogue/installation.sqlite + installation.lock
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

    @property
    def catalogue(self) -> Path:
        return self.root / CATALOGUE_DIR

    @property
    def installation_database(self) -> Path:
        return self.catalogue / INSTALLATION_DATABASE

    @property
    def installation_lock(self) -> Path:
        return self.catalogue / INSTALLATION_LOCK

    def create(self, workspace_id: str) -> None:
        self._ensure_root()
        for path in (
            self.root / BACKUPS_DIR / workspace_id,
            self.attempts_for(workspace_id),
            self.runtime_for(workspace_id),
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _ensure_root(self) -> None:
        """Bring the installation-state root into being, owner-private from the
        instant this call is the one that creates it.

        A root this call *finds* already there is left exactly as it is: it may
        be the caller's own pre-existing directory, and every store that walks
        beneath it proves the parent policy out on every use regardless of who
        made it. A root this call *creates* has no owner yet: it is made at
        `_ROOT_MODE`, explicitly, at the `mkdir` syscall itself -- `mkdir`'s own
        default of 0o777 is filtered by whatever umask this process runs under,
        and a permissive one would otherwise hand a fresh root group- or
        world-writable before anything below gets a chance to narrow it -- and,
        on Windows, where that mode is not a promise the filesystem keeps, then
        reduced to an owner-only DACL before a single child exists: a freshly
        created directory there inherits whatever DACL its parent's inheritance
        supplies, which a hosted runner's temp tree can make writable by SYSTEM
        or the local administrators alongside this user. Restricting it here
        establishes the invariant `InstalledCredentialStore` and
        `InstalledConfigStore` require of this exact root rather than leaving
        them to discover it missing.

        `exist_ok=False` (the default) is the mechanism: a `FileExistsError`
        from this exact call is the only way to learn the root was already
        there rather than just created, since asking first and creating second
        would leave a window in which a concurrent creator's answer is stale.

        Fails closed, including when the native tool itself does not complete:
        an installation root this call could not restrict is never treated as
        restricted merely because nothing has proved otherwise yet. And because
        this call is the one that just created it, with nothing yet made
        inside it, it rolls that creation back before raising -- a plain,
        non-recursive `rmdir` of that exact empty directory -- so the ordinary
        failure leaves the path absent and a retry re-creates and re-restricts
        it, rather than finding a bare root already there, taking that for a
        pre-existing directory of somebody else's, and populating it
        unrestricted while appearing to succeed.

        That rollback is itself best-effort, and the failure path is honest
        about it rather than assuming it: a root this call cannot even remove
        (an `OSError`, the same way a racing writer would produce one) is left
        exactly as the restriction failure made it, bare and unrestricted, and
        the error raised is the same fail-closed one either way -- it does not
        claim the rollback succeeded.
        """
        try:
            self.root.mkdir(parents=True, mode=_ROOT_MODE)
        except OSError:
            if not self.root.is_dir():
                raise
            return
        if _restrict_root_to_owner(self.root):
            return
        try:
            self.root.rmdir()
        except OSError:
            pass
        raise BackupError(_ROOT_RESTRICTION_FAILURE)


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
    "CATALOGUE_DIR",
    "INSTALLATION_DATABASE",
    "INSTALLATION_LOCK",
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
