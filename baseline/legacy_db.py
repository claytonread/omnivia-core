"""Backup, read-only capture, checksum, restore, and rollback for legacy databases.

The legacy local database is the one artifact in this migration that cannot be
regenerated: losing it loses the user's memories. Phase 0's job is to make the
zero-data-loss claim checkable before anything moves.

Everything here is built around three rules:

1. **Never touch the user's real database.** :func:`assert_fixture_database`
   refuses any path inside the OmniVia data directory, and every read path opens
   SQLite in ``mode=ro`` with ``query_only`` set, so even a mistaken call cannot
   write.
2. **Verify content, not bytes, across copies.** SQLite's online backup rewrites
   pages, so a backup is verified by comparing a content checksum computed from
   ordered rows. A byte-for-byte file checksum is used only where a byte copy is
   expected, which is restore and rollback.
3. **Capture no values.** The read-only inventory records table names, column
   names, row counts, and checksums. Memory content never leaves the database.
   :func:`redact_row` exists for the cases where a row must be looked at.

The fixture generator produces a deterministic legacy-shaped database from
synthetic content, so the acceptance criteria can be exercised in tests without
any real data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baseline import BASELINE_FORMAT_VERSION, BASELINE_TASK_ID, INVENTORY_DIR
from baseline.determinism import (
    diff_json,
    format_differences,
    load_json,
    write_artifact,
)

LEGACY_FIXTURE_PATH = INVENTORY_DIR / "legacy-db-fixture.json"

#: The real local database this module must never open.
OMNIVIA_DATA_DIRNAME = ".omnivia"
LEGACY_DATABASE_FILENAME = "memories.db"

#: Columns whose values are user content and are redacted whenever a row is shown.
CONTENT_COLUMNS = frozenset({"content", "text_preview", "description", "evidence"})
#: Columns that hold filesystem locations and are reduced to a basename.
PATH_COLUMNS = frozenset({"file_path", "path", "root_path", "storage_path", "source_reference"})


class LegacyDatabaseSafetyError(RuntimeError):
    """Raised when an operation would touch a database that may hold real user data."""


class LegacyDatabaseError(RuntimeError):
    """Raised when a backup, capture, or restore cannot be completed safely."""


@dataclass(frozen=True)
class TableInventory:
    """Read-only description of one table. Holds no user values."""

    name: str
    columns: tuple[str, ...]
    row_count: int
    content_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "row_count": self.row_count,
            "content_checksum": self.content_checksum,
        }


@dataclass(frozen=True)
class DatabaseInventory:
    """Read-only description of a whole database. Holds no user values."""

    tables: tuple[TableInventory, ...]
    content_checksum: str
    total_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_checksum": self.content_checksum,
            "total_rows": self.total_rows,
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass
class BackupRecord:
    """Evidence that one backup, restore, or rollback happened and verified."""

    operation: str
    source_content_checksum: str
    target_content_checksum: str
    source_file_checksum: str | None = None
    target_file_checksum: str | None = None
    verified: bool = False
    checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "source_content_checksum": self.source_content_checksum,
            "target_content_checksum": self.target_content_checksum,
            "source_file_checksum": self.source_file_checksum,
            "target_file_checksum": self.target_file_checksum,
            "verified": self.verified,
            "checks": list(self.checks),
        }


def default_legacy_database_path() -> Path:
    """The default location of the real local database. Never opened by this module."""
    return Path.home() / OMNIVIA_DATA_DIRNAME / LEGACY_DATABASE_FILENAME


def assert_fixture_database(path: Path) -> Path:
    """Return ``path`` resolved, or refuse if it could be the user's real database.

    The rule is deliberately blunt: anything inside ``~/.omnivia`` is off limits,
    as is any file named ``memories.db`` directly under a home directory. A
    baseline that "usually" avoids real data is not a safety property.
    """
    resolved = path.expanduser().resolve()
    data_dir = (Path.home() / OMNIVIA_DATA_DIRNAME).resolve()
    if resolved == data_dir or _is_relative_to(resolved, data_dir):
        raise LegacyDatabaseSafetyError(
            f"refusing to operate on {resolved}: paths inside {data_dir} may hold real "
            "user data. Baseline tooling runs only against generated fixtures."
        )
    if resolved.name == LEGACY_DATABASE_FILENAME and resolved.parent == Path.home().resolve():
        raise LegacyDatabaseSafetyError(
            f"refusing to operate on {resolved}: it matches the legacy database name in "
            "a home directory."
        )
    return resolved


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open a database read-only, with writes disabled at the connection level."""
    resolved = assert_fixture_database(path)
    if not resolved.exists():
        raise LegacyDatabaseError(f"database {resolved} does not exist")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a file's bytes, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture_inventory(path: Path) -> DatabaseInventory:
    """Read a database without writing to it and return a value-free inventory."""
    connection = open_read_only(path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            if not row[0].startswith("sqlite_")
        ]
        tables = tuple(_table_inventory(connection, name) for name in names)
    finally:
        connection.close()

    overall = hashlib.sha256()
    for table in tables:
        overall.update(f"{table.name}:{table.content_checksum}\n".encode())
    return DatabaseInventory(
        tables=tables,
        content_checksum=overall.hexdigest(),
        total_rows=sum(table.row_count for table in tables),
    )


def backup_database(source: Path, destination: Path) -> BackupRecord:
    """Copy a database with SQLite's online backup and verify content equality.

    The source is opened read-only, so the operation cannot modify the database
    it is protecting.
    """
    source_resolved = assert_fixture_database(source)
    destination_resolved = assert_fixture_database(destination)
    if destination_resolved == source_resolved:
        raise LegacyDatabaseError("backup destination must differ from the source")
    if destination_resolved.exists():
        raise LegacyDatabaseError(
            f"backup destination {destination_resolved} already exists; refusing to "
            "overwrite an existing backup"
        )

    before = capture_inventory(source_resolved)
    destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    read_connection = open_read_only(source_resolved)
    write_connection = sqlite3.connect(str(destination_resolved))
    try:
        read_connection.backup(write_connection)
    finally:
        write_connection.close()
        read_connection.close()

    after = capture_inventory(destination_resolved)
    source_after = capture_inventory(source_resolved)

    record = BackupRecord(
        operation="backup",
        source_content_checksum=before.content_checksum,
        target_content_checksum=after.content_checksum,
        source_file_checksum=sha256_file(source_resolved),
        target_file_checksum=sha256_file(destination_resolved),
    )
    checks = []
    if after.content_checksum != before.content_checksum:
        checks.append("backup content checksum does not match the source")
    if after.total_rows != before.total_rows:
        checks.append(
            f"backup has {after.total_rows} rows, source had {before.total_rows}"
        )
    if source_after.content_checksum != before.content_checksum:
        checks.append("source content changed during backup; the read path is not read-only")
    record.checks = checks
    record.verified = not checks
    if checks:
        raise LegacyDatabaseError("; ".join(checks))
    return record


def restore_database(backup: Path, destination: Path, *, overwrite: bool = False) -> BackupRecord:
    """Restore a backup by byte copy and verify the file and content checksums."""
    backup_resolved = assert_fixture_database(backup)
    destination_resolved = assert_fixture_database(destination)
    if not backup_resolved.exists():
        raise LegacyDatabaseError(f"backup {backup_resolved} does not exist")
    if destination_resolved.exists() and not overwrite:
        raise LegacyDatabaseError(
            f"restore destination {destination_resolved} exists; pass overwrite=True to "
            "replace it deliberately"
        )

    before = capture_inventory(backup_resolved)
    destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_resolved, destination_resolved)
    after = capture_inventory(destination_resolved)

    record = BackupRecord(
        operation="restore",
        source_content_checksum=before.content_checksum,
        target_content_checksum=after.content_checksum,
        source_file_checksum=sha256_file(backup_resolved),
        target_file_checksum=sha256_file(destination_resolved),
    )
    checks = []
    if record.source_file_checksum != record.target_file_checksum:
        checks.append("restored file checksum does not match the backup")
    if after.content_checksum != before.content_checksum:
        checks.append("restored content checksum does not match the backup")
    record.checks = checks
    record.verified = not checks
    if checks:
        raise LegacyDatabaseError("; ".join(checks))
    return record


def rollback_from_backup(backup: Path, live: Path) -> BackupRecord:
    """Roll a migration back by restoring the pre-migration backup over the live file.

    This is :func:`restore_database` with ``overwrite=True`` and a distinct
    operation name, so the evidence record says which of the two happened.
    """
    record = restore_database(backup, live, overwrite=True)
    record.operation = "rollback"
    return record


def verify_zero_data_loss(before: DatabaseInventory, after: DatabaseInventory) -> list[str]:
    """Return the acceptance-criteria failures between two inventories.

    An empty list is the zero-data-loss acceptance signal: same tables, same
    columns, same row counts, same content checksums.
    """
    problems: list[str] = []
    before_tables = {table.name: table for table in before.tables}
    after_tables = {table.name: table for table in after.tables}

    for missing in sorted(set(before_tables) - set(after_tables)):
        problems.append(f"table {missing!r} is missing after the operation")
    for added in sorted(set(after_tables) - set(before_tables)):
        problems.append(f"table {added!r} appeared after the operation")

    for name in sorted(set(before_tables) & set(after_tables)):
        original, current = before_tables[name], after_tables[name]
        if original.columns != current.columns:
            problems.append(
                f"table {name!r} columns changed: {list(original.columns)} -> "
                f"{list(current.columns)}"
            )
        if original.row_count != current.row_count:
            problems.append(
                f"table {name!r} row count changed: {original.row_count} -> "
                f"{current.row_count}"
            )
        elif original.content_checksum != current.content_checksum:
            problems.append(
                f"table {name!r} has the same row count but a different content "
                "checksum; row values changed"
            )

    if not problems and before.content_checksum != after.content_checksum:
        problems.append("database content checksum changed with no per-table difference")
    return problems


def redact_row(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    """Return a row safe to print: content summarised, paths reduced to basenames."""
    redacted: dict[str, Any] = {}
    for column, value in zip(columns, row):
        if value is None:
            redacted[column] = None
        elif column in CONTENT_COLUMNS and isinstance(value, str):
            redacted[column] = {
                "length": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        elif column in PATH_COLUMNS and isinstance(value, str):
            redacted[column] = f"<redacted>/{Path(value).name}"
        else:
            redacted[column] = value
    return redacted


# --------------------------------------------------------------------------- #
# Fixture generation
# --------------------------------------------------------------------------- #

#: Synthetic legacy rows. Every identifier and timestamp is fixed so the fixture
#: database has the same content checksum on every machine.
FIXTURE_MEMORIES: tuple[dict[str, str], ...] = (
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "content": "Core owns portable knowledge contracts.",
        "memory_type": "decision",
        "lifecycle_state": "approved",
        "created_by": "human",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "content": "Every graph edge cites source evidence before approval.",
        "memory_type": "constraint",
        "lifecycle_state": "proposed",
        "created_by": "agent",
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "content": "Workspace imports create one memory per content chunk.",
        "memory_type": "context",
        "lifecycle_state": "observed",
        "created_by": "agent",
    },
)

FIXTURE_WORKSPACE_ID = "44444444-4444-4444-8444-444444444444"
FIXTURE_TIMESTAMP = "2026-01-01T00:00:00+00:00"


def build_legacy_fixture_database(path: Path) -> Path:
    """Create a deterministic, legacy-shaped database from synthetic content.

    The schema comes from Core's own ``Database._init_schema``, so the fixture
    exercises the real table layout rather than a hand-written approximation.
    """
    from omnivia_memory.persistence.database import Database, DatabaseConfig

    resolved = assert_fixture_database(path)
    if resolved.exists():
        raise LegacyDatabaseError(f"fixture database {resolved} already exists")

    database = Database(DatabaseConfig(db_path=resolved))
    database.connect()
    try:
        database.execute(
            """
            INSERT INTO workspaces (
                id, name, root_path, storage_path, description, index_status,
                settings_json, created_at, updated_at, last_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                FIXTURE_WORKSPACE_ID,
                "Legacy Fixture Workspace",
                "<fixture>/workspace",
                "<fixture>/storage",
                "Synthetic workspace for legacy migration rehearsal",
                "indexed",
                "{}",
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
            ),
        )
        for memory in FIXTURE_MEMORIES:
            database.execute(
                """
                INSERT INTO memories (
                    id, workspace_id, content, source_type, source_reference,
                    source_description, lifecycle_state, memory_type, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory["id"],
                    FIXTURE_WORKSPACE_ID,
                    memory["content"],
                    "file",
                    "<fixture>/docs/legacy.md",
                    "Synthetic legacy source",
                    memory["lifecycle_state"],
                    memory["memory_type"],
                    memory["created_by"],
                    FIXTURE_TIMESTAMP,
                    FIXTURE_TIMESTAMP,
                ),
            )
        database.commit()
    finally:
        database.close()
    return resolved


def build_legacy_fixture_inventory() -> dict[str, Any]:
    """Return the tracked inventory of the generated legacy fixture database."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="omnivia-baseline-legacy-") as tmp:
        fixture = build_legacy_fixture_database(Path(tmp) / "legacy-memories.db")
        inventory = capture_inventory(fixture)

    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "source": "baseline.legacy_db.build_legacy_fixture_database",
        "note": (
            "Generated from synthetic content only. The real ~/.omnivia/memories.db is "
            "never opened, copied, or inspected by this tooling."
        ),
        "inventory": inventory.to_dict(),
    }


def write_legacy_fixture_inventory() -> Path:
    """Regenerate the tracked legacy fixture inventory."""
    write_artifact(LEGACY_FIXTURE_PATH, build_legacy_fixture_inventory())
    return LEGACY_FIXTURE_PATH


def verify_legacy_fixture_inventory() -> list[str]:
    """Check the generated fixture still matches the frozen inventory."""
    expected = load_json(LEGACY_FIXTURE_PATH)
    actual = build_legacy_fixture_inventory()
    differences = diff_json(expected, actual)
    if not differences:
        return []
    return [
        "Legacy fixture database inventory drifted from the frozen Phase 0 baseline.",
        format_differences(differences),
        (
            "The fixture is deterministic by construction, so a difference means either the "
            "fixture content or Core's storage schema changed."
        ),
    ]


def _table_inventory(connection: sqlite3.Connection, name: str) -> TableInventory:
    # Table names cannot be bound as parameters. `name` comes from sqlite_master
    # on a read-only connection, never from a caller, and the connection has
    # query_only set, so neither statement below can be turned into a write.
    columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({name})").fetchall())
    rows = connection.execute(f"SELECT * FROM {name}").fetchall()
    # Rows are hashed in a canonical order so the checksum does not depend on
    # SQLite's physical row order, and values never leave this function.
    encoded = sorted(json.dumps(list(row), sort_keys=True, default=str) for row in rows)
    digest = hashlib.sha256()
    digest.update("|".join(columns).encode("utf-8"))
    for item in encoded:
        digest.update(b"\n")
        digest.update(item.encode("utf-8"))
    return TableInventory(
        name=name,
        columns=columns,
        row_count=len(rows),
        content_checksum=digest.hexdigest(),
    )


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True
