"""Snapshot of the local storage schema Core creates.

Phase 1 must not change storage schemas, which is only enforceable if the
current schema is written down in a form a check can compare against. This
module opens a throwaway database, lets ``Database._init_schema`` build the
schema exactly as it does at runtime, and records the resulting tables, columns,
indexes, and foreign keys.

The probe database is created under a temporary directory and deleted
immediately. It never touches the default ``~/.omnivia/memories.db`` location.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from baseline import BASELINE_FORMAT_VERSION, BASELINE_TASK_ID, INVENTORY_DIR, REPO_ROOT
from baseline.determinism import (
    diff_json,
    format_differences,
    load_json,
    write_artifact,
)

STORAGE_SCHEMA_PATH = INVENTORY_DIR / "storage-schema.json"

#: SQLite's own bookkeeping tables are implementation detail, not Core's schema.
_INTERNAL_TABLE_PREFIX = "sqlite_"


def capture_connection_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return the tables, columns, indexes, and foreign keys of a connection."""
    tables: dict[str, Any] = {}
    cursor = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    for (name,) in cursor.fetchall():
        if name.startswith(_INTERNAL_TABLE_PREFIX):
            continue
        tables[name] = {
            "columns": _columns(connection, name),
            "indexes": _indexes(connection, name),
            "foreign_keys": _foreign_keys(connection, name),
        }
    return tables


def build_storage_schema_inventory() -> dict[str, Any]:
    """Return the canonical storage schema inventory for the current checkout."""
    from omnivia_memory.persistence.database import Database, DatabaseConfig

    with tempfile.TemporaryDirectory(prefix="omnivia-baseline-schema-") as tmp:
        database = Database(DatabaseConfig(db_path=Path(tmp) / "schema-probe.db"))
        database.connect()
        try:
            tables = capture_connection_schema(database.connection)
        finally:
            database.close()

    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "source": "omnivia_memory.persistence.database.Database._init_schema",
        "note": (
            "Captured from a throwaway probe database. SQLite-internal tables are "
            "excluded; auto-created indexes are kept because they encode UNIQUE and "
            "PRIMARY KEY constraints."
        ),
        "tables": tables,
    }


def write_storage_schema_inventory() -> Path:
    """Regenerate the tracked storage schema inventory."""
    write_artifact(STORAGE_SCHEMA_PATH, build_storage_schema_inventory())
    return STORAGE_SCHEMA_PATH


def verify_storage_schema_inventory() -> list[str]:
    """Return precise drift messages, or an empty list when the schema matches."""
    expected = load_json(STORAGE_SCHEMA_PATH)
    actual = build_storage_schema_inventory()
    differences = diff_json(expected, actual)
    if not differences:
        return []
    return [
        "Local storage schema drifted from the frozen Phase 0 baseline.",
        f"Artifact: {STORAGE_SCHEMA_PATH.relative_to(REPO_ROOT)}",
        format_differences(differences),
        (
            "Phase 1 must not change storage schemas. If this change is intended, it needs "
            "an explicit migration decision before the baseline is regenerated."
        ),
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    # PRAGMA arguments cannot be bound. Every table name here comes from
    # sqlite_master on a throwaway probe database, never from a caller.
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [
        {
            "name": row[1],
            "type": row[2],
            "not_null": bool(row[3]),
            "default": row[4],
            "primary_key": int(row[5]),
        }
        for row in rows
    ]
    return sorted(columns, key=lambda column: column["name"])


def _indexes(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(f"PRAGMA index_list({table})").fetchall()
    indexes = []
    for row in rows:
        name = row[1]
        members = connection.execute(f"PRAGMA index_info({name})").fetchall()
        indexes.append(
            {
                "name": name,
                "unique": bool(row[2]),
                "origin": row[3],
                "columns": [member[2] for member in sorted(members, key=lambda item: item[0])],
            }
        )
    return sorted(indexes, key=lambda index: index["name"])


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    keys = [
        {
            "column": row[3],
            "references_table": row[2],
            "references_column": row[4],
            "on_update": row[5],
            "on_delete": row[6],
        }
        for row in rows
    ]
    return sorted(keys, key=lambda key: (key["column"], key["references_table"]))
