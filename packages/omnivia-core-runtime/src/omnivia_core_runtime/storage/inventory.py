"""Database content inventory for data-preservation proof (T-0629C).

The migration exit gate is "legacy rows and values are unchanged". Proving that
needs more than a row count: a migration that copies the right number of rows with
mangled unicode, truncated blobs or reordered JSON passes a count check and still
loses data. So each table contributes a checksum over its actual values, computed
in a defined row and column order.

This deliberately does not import `baseline`. The runtime package may depend only
on `omnivia_core`, and `baseline` is a repository-level development package that is
not part of any distribution. The shapes are similar on purpose; the code is
independent because the dependency direction says it must be.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class TableInventory:
    """Structure and content digest of one table. Holds no user values."""

    name: str
    columns: tuple[str, ...]
    row_count: int
    content_checksum: str


@dataclass(frozen=True)
class DatabaseInventory:
    """Structure and content digest of a whole database."""

    tables: tuple[TableInventory, ...]
    total_rows: int
    content_checksum: str

    def table(self, name: str) -> TableInventory | None:
        for entry in self.tables:
            if entry.name == name:
                return entry
        return None

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.tables)


def _encode(value: object) -> bytes:
    """Encode one cell so distinct values never collide.

    The type tag matters: without it the integer 1, the string "1" and the blob
    b"1" would hash identically, and a migration that changed a column's affinity
    would look like a faithful copy.
    """
    if value is None:
        return b"N\x1f"
    if isinstance(value, bool):
        return b"B\x1f" + (b"1" if value else b"0")
    if isinstance(value, int):
        return b"I\x1f" + str(value).encode("ascii")
    if isinstance(value, float):
        # repr round-trips exactly for float, unlike str on older versions.
        return b"F\x1f" + repr(value).encode("ascii")
    if isinstance(value, bytes):
        return b"X\x1f" + value
    return b"S\x1f" + str(value).encode("utf-8")


def capture_table_inventory(
    connection: sqlite3.Connection, name: str
) -> TableInventory:
    """Inventory one table by reading it in a deterministic order."""
    columns = tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
    )
    if not columns:
        return TableInventory(
            name=name,
            columns=(),
            row_count=0,
            content_checksum=hashlib.sha256(b"").hexdigest(),
        )

    quoted = ", ".join(f'"{column}"' for column in columns)
    # Order by every column so the digest cannot depend on storage order, which
    # SQLite does not guarantee and which a migration legitimately changes.
    rows = connection.execute(
        f'SELECT {quoted} FROM "{name}" ORDER BY {quoted}'
    ).fetchall()

    hasher = hashlib.sha256()
    hasher.update("\x1f".join(columns).encode("utf-8") + b"\x1e")
    for row in rows:
        for cell in row:
            hasher.update(_encode(cell))
        hasher.update(b"\x1e")

    return TableInventory(
        name=name,
        columns=columns,
        row_count=len(rows),
        content_checksum=hasher.hexdigest(),
    )


def capture_inventory(connection: sqlite3.Connection) -> DatabaseInventory:
    """Inventory every user table, in name order."""
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    tables = tuple(capture_table_inventory(connection, name) for name in names)

    overall = hashlib.sha256()
    for entry in tables:
        overall.update(f"{entry.name}:{entry.content_checksum}\n".encode())

    return DatabaseInventory(
        tables=tables,
        total_rows=sum(entry.row_count for entry in tables),
        content_checksum=overall.hexdigest(),
    )


def compare_inventories(
    before: DatabaseInventory,
    after: DatabaseInventory,
    *,
    allow_added_tables: frozenset[str] = frozenset(),
) -> list[str]:
    """Differences that would constitute data loss.

    An empty list is the zero-data-loss signal. `allow_added_tables` permits the
    reserved `omnivia_` substrate a migration legitimately adds; every other new
    table is reported, because an unexplained table means the migration did
    something the plan did not describe.
    """
    problems: list[str] = []
    before_names = set(before.table_names)
    after_names = set(after.table_names)

    for missing in sorted(before_names - after_names):
        problems.append(f"table {missing!r} is missing after the operation")

    for added in sorted(after_names - before_names):
        if added in allow_added_tables or added.startswith("omnivia_"):
            continue
        problems.append(f"table {added!r} appeared unexpectedly")

    for name in sorted(before_names & after_names):
        original = before.table(name)
        current = after.table(name)
        if original is None or current is None:  # pragma: no cover - guarded above
            continue
        if original.columns != current.columns:
            lost = sorted(set(original.columns) - set(current.columns))
            gained = sorted(set(current.columns) - set(original.columns))
            problems.append(
                f"table {name!r} columns changed (lost {lost}, gained {gained})"
            )
            continue
        if original.row_count != current.row_count:
            problems.append(
                f"table {name!r} row count changed from {original.row_count} "
                f"to {current.row_count}"
            )
            continue
        if original.content_checksum != current.content_checksum:
            problems.append(
                f"table {name!r} values changed despite an identical row count"
            )
    return problems


__all__ = [
    "DatabaseInventory",
    "TableInventory",
    "capture_inventory",
    "capture_table_inventory",
    "compare_inventories",
]
