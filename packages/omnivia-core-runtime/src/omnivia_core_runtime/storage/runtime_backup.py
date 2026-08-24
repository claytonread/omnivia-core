"""Restore-time re-verification for RT-701 (destructive failure injection).

`backup.restore_backup` trusts the file handed to it: it stages a copy and swaps it
in atomically, which protects a restore against crashing halfway, but it does not
re-prove the backup is still the file `verify_backup` once certified. Between
verification and restore, a backup is just a file on disk like any other -- bit rot,
a bad copy, or an operator overwriting it by hand can all leave a backup that opens
cleanly but is no longer the data it claims to be. :func:`restore_verified_backup` is
the one function that closes that gap: it re-proves the backup before touching the
restore target, and touches nothing at all when that re-proof fails.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omnivia_core_runtime.storage.backup import (
    BackupError,
    VerifiedBackup,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import capture_inventory


def restore_verified_backup(backup: VerifiedBackup, destination: Path) -> Path:
    """Restore `backup` over `destination`, but only after re-proving it is intact.

    Fails closed: a backup that no longer passes its own integrity check, or whose
    content no longer matches the inventory `verify_backup` recorded for it, is
    refused before `restore_backup` is ever called. `destination` is therefore left
    exactly as it was found -- never silently repaired in place, and never partially
    overwritten from a backup that cannot be trusted. A backup damaged badly enough
    that SQLite itself refuses to read it is the same refusal by a different route:
    `sqlite3.DatabaseError` is caught and reported as the same `BackupError`, not
    left to propagate as a raw driver exception.
    """
    if not backup.verified:
        raise BackupError("refusing to restore a backup that was never verified")

    try:
        connection = open_database(backup.path, OpenMode.READ_ONLY)
        try:
            problems = integrity_check(connection)
            if problems:
                raise BackupError(
                    f"backup at {backup.path} failed re-verification before "
                    f"restore: {problems}"
                )
            current = capture_inventory(connection)
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise BackupError(
            f"backup at {backup.path} failed re-verification before restore: {error}"
        ) from error

    if current.content_checksum != backup.backup_inventory.content_checksum:
        raise BackupError(
            f"backup at {backup.path} no longer matches the state it was verified "
            "against; refusing to restore from it"
        )

    return restore_backup(backup.path, destination)


__all__ = ["restore_verified_backup"]
