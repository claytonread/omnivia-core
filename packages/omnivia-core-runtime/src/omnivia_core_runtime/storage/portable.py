"""Portable workspace export and restore (SPEC-CORE-ENGMEM-001 AC-063, EM-28/EM-29).

A verified backup (`backup.py`) is an installation-local, byte-faithful safety copy:
it keeps every row, including the live continuity sessions, the association
authority, host correlation, installation checkout mappings and the service lease.
That is right for rolling this installation back and wrong for a file that leaves it.
A portable artifact is the same consistent snapshot with the installation-local
authority and mappings *removed*, so the two stay distinct:

```text
artifact/
├── portable.json      format, version, workspace and schema identity, checksums
└── workspace.sqlite   the scrubbed snapshot
```

Retained, unchanged: every record, evidence, version, snapshot, snapshot file index,
source stream and source event, checkpoint and audit identity, with their digests --
checkpoint lineage included, because a checkpoint is digest-identified, immutable and
carries references rather than paths. Session rows and their lifecycle history stay as
history.

Excluded, from the artifact itself rather than at restore:

- installation checkout mappings (`omnivia_engineering_checkouts`) and every row that
  names an installation or checkout: snapshot capture headers, source stream origins
  and the source producer queue and state. They are removed child-first, so the
  artifact has no foreign-key violation, and no placeholder checkout is left behind;
- host correlation and checkout hints of continuity sessions, and every association
  key in the lifecycle history;
- the association authority, workspace lease, mutation guard and open events --
  `acquire_lease` mints the restoring owner's own;
- the legacy baseline's `workspaces` and `sources` path columns, blanked.

A continuity session still `active` at export is made inert by setting its row to
`revoked` and deleting the authority that made it current. No lifecycle event is
appended: the history keeps exactly the audited events it had, and an event with a new
time and the old audit reference would not pass the canonical lifecycle/audit
predicate. The restored session therefore resumes nothing; the caller registers a
fresh session through a fresh local binding.

Consequence of excluding capture headers and origins: a `captured_v1` snapshot keeps
its snapshot row, file index and source event, but its sealed capture header is
installation-bound, so reads that need the header fail closed, and a restored stream
with no origin refuses further captured events as a legacy stream. A new capture opens
a new stream.

Credentials never live in a workspace database (the installation store holds the
credential digests), and the verifier asserts the artifact is the exact canonical
workspace schema, so a table that could carry one cannot ride along.

Verification is deterministic and fails closed: the artifact is a real directory of
exactly two real files (no symbolic links), a closed and strictly typed manifest and
its own checksum, the SQLite integrity and foreign-key checks, the exact canonical schema
and guard triggers, the content checksum of every table, and an inertness proof over
the scrubbed columns and tables. A handcrafted artifact with a consistent checksum and
a live session is still refused.

The scrub drops and re-creates the guard triggers of the tables it rewrites, from their
own stored SQL, inside one transaction on a private staging copy -- never on the source,
which is only read. The schema fingerprint is checked afterwards, so a scrub that left
the schema different from the canonical one never becomes an artifact.

Export and restore build in a uniquely named directory this call creates beside the
destination, remove only that directory, and publish with an exclusive create (`mkdir`
for the artifact directory, a hard link for each file), so a destination that appears
meanwhile is refused and never overwritten or deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    verify_fingerprint,
)
from omnivia_core_runtime.storage.backup import backup_database
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import capture_inventory
from omnivia_core_runtime.storage.migrations import canonical_schema_fingerprint

PORTABLE_FORMAT: Final = "omnivia.workspace-portable"
PORTABLE_FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "portable.json"
DATABASE_NAME: Final = "workspace.sqlite"

_MANIFEST_KEYS: Final = frozenset(
    {
        "format",
        "format_version",
        "workspace_id",
        "schema_fingerprint",
        "exported_at_us",
        "content_checksum",
        "total_rows",
        "sessions_revoked",
        "manifest_checksum",
    }
)
_MANIFEST_MAX_BYTES: Final = 4096
_CHECKSUM: Final = re.compile(r"sha256:[0-9a-f]{64}")
_WORKSPACE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400

_PATH_PLACEHOLDER: Final = "portable"

#: Installation-local operational rows, deleted child-first so no foreign key is left
#: dangling. Each carries an installation or checkout identity (or is that mapping).
_EXCLUDED_TABLES: Final = (
    ("omnivia_engineering_source_producer_queue", "a source producer queue row"),
    ("omnivia_engineering_source_producer_state", "a source producer state row"),
    ("omnivia_engineering_source_stream_origins", "a source stream origin"),
    ("omnivia_engineering_snapshot_captures", "a snapshot capture header"),
    ("omnivia_engineering_checkouts", "an installation checkout"),
    ("omnivia_engineering_session_authority", "association authority"),
    ("omnivia_workspace_lease", "a workspace lease"),
    ("omnivia_mutation_guard", "a mutation guard"),
    ("omnivia_workspace_open_events", "a workspace open event"),
)

#: Tables the scrub rewrites or empties. Their triggers are lifted for the scrub and restored.
_SCRUBBED_TABLES: Final = (
    "omnivia_engineering_sessions",
    "omnivia_engineering_session_lifecycle",
    *(table for table, _ in _EXCLUDED_TABLES),
    "workspaces",
    "sources",
)

#: Each statement leaves its table inert; `_INERTNESS` proves the same facts.
_SCRUB: Final = (
    # Inert, not extended: no lifecycle event is appended for the revocation.
    "UPDATE omnivia_engineering_sessions SET state = 'revoked' WHERE state = 'active'",
    "UPDATE omnivia_engineering_sessions SET host_session_ref = NULL, checkout_hint = NULL",
    "UPDATE omnivia_engineering_session_lifecycle SET association_key = NULL",
    *(f"DELETE FROM {table}" for table, _ in _EXCLUDED_TABLES),
    (
        f"UPDATE workspaces SET root_path = '{_PATH_PLACEHOLDER}', "
        f"storage_path = '{_PATH_PLACEHOLDER}'"
    ),
    "UPDATE sources SET file_path = 'portable:' || id",
)

#: Every query counts rows that are NOT inert; all must be zero.
_INERTNESS: Final = {
    "a live or correlated continuity session": (
        "SELECT COUNT(*) FROM omnivia_engineering_sessions "
        "WHERE state = 'active' OR host_session_ref IS NOT NULL OR checkout_hint IS NOT NULL"
    ),
    "an association key": (
        "SELECT COUNT(*) FROM omnivia_engineering_session_lifecycle "
        "WHERE association_key IS NOT NULL"
    ),
    **{what: f"SELECT COUNT(*) FROM {table}" for table, what in _EXCLUDED_TABLES},
    "a legacy workspace path": (
        "SELECT COUNT(*) FROM workspaces "
        f"WHERE root_path IS NOT '{_PATH_PLACEHOLDER}' "
        f"OR storage_path IS NOT '{_PATH_PLACEHOLDER}'"
    ),
    "a legacy source path": "SELECT COUNT(*) FROM sources WHERE file_path IS NOT 'portable:' || id",
}


class PortableArtifactError(StorageError):
    """A portable artifact could not be created, verified or restored."""


def _manifest_checksum(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _scrub(connection: sqlite3.Connection) -> int:
    """Make the staging copy inert and return how many live sessions it made inert."""
    marks = ",".join("?" * len(_SCRUBBED_TABLES))
    triggers = connection.execute(
        f"SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        f"AND tbl_name IN ({marks}) ORDER BY rowid",
        _SCRUBBED_TABLES,
    ).fetchall()
    revoked = int(
        connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_sessions WHERE state = 'active'"
        ).fetchone()[0]
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        for statement in _SCRUB:
            connection.execute(statement)
        for _, sql in triggers:
            connection.execute(sql)
        problems = foreign_key_check(connection)
        if problems:
            raise PortableArtifactError(f"the scrub left a foreign-key violation: {problems}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    # Rewritten rows leave their old bytes in page slack; secure_delete plus VACUUM
    # rebuilds the file so a removed path or correlation is absent from the artifact,
    # not merely unlinked.
    connection.execute("VACUUM")
    return revoked


def _lstat(path: Path, what: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise PortableArtifactError(f"no {what} at {path}") from error


def _plain(metadata: os.stat_result, *, directory: bool) -> bool:
    """Whether `lstat` metadata is a real directory or regular file, never a link."""
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        kind(metadata.st_mode)
        and getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT == 0
    )


def _publish_directory(staging: Path, destination: Path) -> None:
    """Create `destination` exclusively and link the staged files into it, manifest last.

    `mkdir` is the no-clobber step: it fails if anything -- file, directory or link --
    is already there, so nothing unrelated is overwritten. On failure only what this
    call linked, and the directory it just created, are removed.
    """
    try:
        os.mkdir(destination)
    except FileExistsError as error:
        raise PortableArtifactError(
            f"refusing to overwrite an existing artifact at {destination}"
        ) from error
    linked: list[Path] = []
    try:
        for name in (DATABASE_NAME, MANIFEST_NAME):
            os.link(staging / name, destination / name)
            linked.append(destination / name)
    except BaseException:
        for path in linked:
            path.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


def export_portable(source: Path, destination: Path, *, exported_at_us: int) -> dict[str, Any]:
    """Write a portable artifact for `source` at `destination` and return its manifest.

    `source` is only read, through the online backup API, so the caller quiesces the
    writer exactly as for a verified backup. `destination` must not exist: the
    artifact is built in a uniquely named directory this call creates beside it, and
    is published only if `destination` is still absent, so an interrupted, refused or
    raced export leaves nothing at `destination` that this call did not create.
    """
    if isinstance(exported_at_us, bool) or not isinstance(exported_at_us, int) or exported_at_us <= 0:
        raise PortableArtifactError("exported_at_us must be a positive integer")
    if os.path.lexists(destination):
        raise PortableArtifactError(f"refusing to overwrite an existing artifact at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".portable-export-", dir=destination.parent))
    try:
        database = staging / DATABASE_NAME
        backup_database(source, database)
        connection = open_database(database, OpenMode.EXCLUSIVE_MAINTENANCE, enable_wal=False)
        try:
            # The artifact must not depend on a WAL sidecar travelling with it.
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA secure_delete = ON")
            revoked = _scrub(connection)
            verify_fingerprint(connection, canonical_schema_fingerprint())
            inventory = capture_inventory(connection)
            workspace_id = str(
                connection.execute("SELECT workspace_id FROM omnivia_workspace_state").fetchone()[0]
            )
        finally:
            connection.close()
        manifest: dict[str, Any] = {
            "format": PORTABLE_FORMAT,
            "format_version": PORTABLE_FORMAT_VERSION,
            "workspace_id": workspace_id,
            "schema_fingerprint": "sha256:" + canonical_schema_fingerprint().digest,
            "exported_at_us": exported_at_us,
            "content_checksum": "sha256:" + inventory.content_checksum,
            "total_rows": inventory.total_rows,
            "sessions_revoked": revoked,
        }
        manifest["manifest_checksum"] = _manifest_checksum(manifest)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        verify_portable(staging)
        _publish_directory(staging, destination)
    finally:
        # Only the directory `mkdtemp` created above; the published files are links.
        shutil.rmtree(staging, ignore_errors=True)
    return manifest


def verify_portable(artifact: Path) -> dict[str, Any]:
    """Return the manifest of a sound portable artifact, or raise `PortableArtifactError`.

    Every failure -- a missing, extra or linked file, a malformed or unknown-version
    manifest, a checksum, schema, integrity or foreign-key mismatch, or any row or
    column that is not inert -- refuses.
    """
    try:
        return _verify(artifact)
    except PortableArtifactError:
        raise
    except (sqlite3.Error, StorageError, OSError, ValueError, KeyError, TypeError) as error:
        raise PortableArtifactError(f"portable artifact is unusable: {error}") from error


def _reject_constant(name: str) -> Any:
    raise ValueError(f"malformed JSON primitive {name}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate manifest key")
    return dict(pairs)


def _integer(value: Any, *, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _checksum(value: Any) -> bool:
    return isinstance(value, str) and _CHECKSUM.fullmatch(value) is not None


def _read_manifest(path: Path) -> dict[str, Any]:
    if not _plain(_lstat(path, "portable manifest"), directory=False):
        raise PortableArtifactError(f"{path.name} is not a plain file (symbolic link or other)")
    with path.open("rb") as handle:
        raw = handle.read(_MANIFEST_MAX_BYTES + 1)
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise PortableArtifactError("portable manifest is too large")
    manifest = json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicates,
    )
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise PortableArtifactError("portable manifest does not have exactly the v1 fields")
    if not _checksum(manifest["manifest_checksum"]):
        raise PortableArtifactError("portable manifest checksum is not a sha256 digest")
    if manifest["manifest_checksum"] != _manifest_checksum(manifest):
        raise PortableArtifactError("portable manifest checksum does not match its content")
    workspace_id = manifest["workspace_id"]
    if not (
        isinstance(manifest["format"], str)
        and _integer(manifest["format_version"], minimum=0)
        and isinstance(workspace_id, str)
        and _WORKSPACE_ID.fullmatch(workspace_id)
        and _checksum(manifest["schema_fingerprint"])
        and _checksum(manifest["content_checksum"])
        and _integer(manifest["exported_at_us"], minimum=1)
        and _integer(manifest["total_rows"], minimum=0)
        and _integer(manifest["sessions_revoked"], minimum=0)
    ):
        raise PortableArtifactError("portable manifest has a malformed field")
    if manifest["format"] != PORTABLE_FORMAT or manifest["format_version"] != PORTABLE_FORMAT_VERSION:
        raise PortableArtifactError("portable manifest is not a supported format and version")
    if manifest["schema_fingerprint"] != "sha256:" + canonical_schema_fingerprint().digest:
        raise PortableArtifactError("portable artifact is not the current workspace schema")
    return manifest


def _verify(artifact: Path) -> dict[str, Any]:
    if not _plain(_lstat(artifact, "portable artifact"), directory=True):
        raise PortableArtifactError(f"no portable artifact at {artifact}: not a plain directory")
    names = {entry.name for entry in artifact.iterdir()}
    if names != {MANIFEST_NAME, DATABASE_NAME}:
        raise PortableArtifactError(f"portable artifact holds {sorted(names)}, not its two files")
    manifest = _read_manifest(artifact / MANIFEST_NAME)
    if not _plain(_lstat(artifact / DATABASE_NAME, "portable database"), directory=False):
        raise PortableArtifactError(f"{DATABASE_NAME} is not a plain file (symbolic link or other)")
    _verify_database(artifact / DATABASE_NAME, manifest)
    return manifest


def _verify_database(database: Path, manifest: dict[str, Any]) -> None:
    connection = open_database(database, OpenMode.READ_ONLY)
    try:
        problems = integrity_check(connection)
        if problems:
            raise PortableArtifactError(f"portable database failed its integrity check: {problems}")
        violations = foreign_key_check(connection)
        if violations:
            raise PortableArtifactError(f"portable database has foreign-key violations: {violations}")
        verify_fingerprint(connection, canonical_schema_fingerprint())
        assert_guards_intact(connection)
        if "sha256:" + fingerprint_schema(connection).digest != manifest["schema_fingerprint"]:
            raise PortableArtifactError("portable database schema differs from its manifest")
        inventory = capture_inventory(connection)
        if ("sha256:" + inventory.content_checksum, inventory.total_rows) != (
            manifest["content_checksum"],
            manifest["total_rows"],
        ):
            raise PortableArtifactError("portable database content does not match its manifest")
        workspace = connection.execute("SELECT workspace_id FROM omnivia_workspace_state").fetchone()
        if workspace is None or str(workspace[0]) != manifest["workspace_id"]:
            raise PortableArtifactError("portable database belongs to a different workspace")
        for what, query in _INERTNESS.items():
            if connection.execute(query).fetchone()[0]:
                raise PortableArtifactError(f"portable artifact carries {what}")
    finally:
        connection.close()


def restore_portable(artifact: Path, destination: Path) -> Path:
    """Restore a verified portable artifact to a new `destination` database.

    Refuses an existing destination -- a portable restore never replaces a workspace --
    including a stale `-wal` or `-shm` sidecar beside it. The database is copied into a
    uniquely named directory this call creates beside `destination`, re-proved there
    against the manifest, and published with an exclusive hard link, so a destination
    created meanwhile is refused and left untouched. The restored database holds no
    lease or guard: the opener acquires its own, and no session in it is anything but
    history.
    """
    manifest = verify_portable(artifact)
    sidecars = (destination, *(destination.with_name(destination.name + s) for s in ("-wal", "-shm")))
    if any(os.path.lexists(path) for path in sidecars):
        raise PortableArtifactError(f"refusing to restore over an existing database at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".portable-restore-", dir=destination.parent))
    try:
        staged = staging / DATABASE_NAME
        source = open_database(artifact / DATABASE_NAME, OpenMode.READ_ONLY)
        try:
            target = sqlite3.connect(str(staged))
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
        finally:
            source.close()
        try:
            _verify_database(staged, manifest)
        except PortableArtifactError:
            raise
        except (sqlite3.Error, StorageError, OSError, ValueError, KeyError, TypeError) as error:
            raise PortableArtifactError(f"restored database is unusable: {error}") from error
        try:
            os.link(staged, destination)
        except FileExistsError as error:
            raise PortableArtifactError(
                f"refusing to restore over an existing database at {destination}"
            ) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


__all__ = [
    "DATABASE_NAME",
    "MANIFEST_NAME",
    "PORTABLE_FORMAT",
    "PORTABLE_FORMAT_VERSION",
    "PortableArtifactError",
    "export_portable",
    "restore_portable",
    "verify_portable",
]
