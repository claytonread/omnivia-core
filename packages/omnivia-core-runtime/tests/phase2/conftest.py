"""Shared Phase 2 fixtures.

Includes the tier-2 preservation corpus from the T-0629 preparation pack. The
frozen Phase 0 fixture holds 4 rows across 14 tables, which proves almost nothing
about value preservation: twelve tables are empty, so no foreign-key fan-out, no
unicode, no NULL/empty-string distinction and no large value is exercised. This
fixture fills every table with the value classes a naive row copy or a
string-interpolated INSERT actually loses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

WORKSPACE_ID = "ws-preservation-0001"
SERVICE_INSTANCE = "svc-instance-1"

#: Value classes that must survive a migration unchanged.
AWKWARD_TEXT = [
    "",
    "plain ascii",
    "unicode ☃ 🌍 émoji",
    "quote ' double \" semi ; backslash \\",
    "json-ish {\"key\": [1, 2]}",
    "x" * 4096,
]


@pytest.fixture
def installation(tmp_path: Path) -> InstallationLayout:
    layout = InstallationLayout(root=tmp_path / "installation-state")
    layout.create(WORKSPACE_ID)
    return layout


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(root=tmp_path / "workspace")


@pytest.fixture
def manifest() -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id=WORKSPACE_ID,
        created_at="2026-07-30T00:00:00+00:00",
        name="Preservation corpus",
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="0.1.0"
        ),
    )


@pytest.fixture
def phase0_source(tmp_path: Path) -> Path:
    """A legacy database with the frozen Phase 0 schema and a rich data corpus."""
    path = tmp_path / "legacy" / "explicit-source.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(path)
    populate_preservation_corpus(path)
    return path


@pytest.fixture
def empty_phase0_source(tmp_path: Path) -> Path:
    """A legacy database with the frozen schema and no rows."""
    path = tmp_path / "legacy" / "empty-source.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(path)
    return path


def populate_preservation_corpus(path: Path) -> None:
    """Fill every legacy table, with uneven fan-out and awkward values.

    Deterministic: no clock, no randomness, fixed identifiers. Two builds produce
    byte-identical databases, so the inventory digest is a stable oracle.
    """
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            _populate(connection)
    finally:
        connection.close()


def legacy_rows(inventory: object) -> int:
    """Rows in the 14 legacy tables only.

    A published workspace also holds the reserved `omnivia_` substrate, whose rows
    are per-attempt bookkeeping - state, ledger and attempt records. Counting them
    would make a faithful migration look like it had invented rows.
    """
    return sum(
        entry.row_count
        for entry in inventory.tables
        if not entry.name.startswith("omnivia_")
    )


def insert_row(
    connection: sqlite3.Connection, table: str, values: dict[str, object], seed: str = ""
) -> None:
    """Schema-driven insert, exposed for tests that extend the corpus."""
    _insert(connection, table, values, seed)


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _placeholder(table: str, column: str, declared_type: str, seed: str) -> object:
    """A deterministic non-null value appropriate to a declared column type.

    `seed` makes the value unique per row. Several frozen tables carry UNIQUE
    constraints - sources(workspace_id, file_path) among them - so a constant
    placeholder would collide on the second row and the fixture would fail for a
    reason that has nothing to do with what is being tested.
    """
    kind = declared_type.upper()
    if "INT" in kind:
        return abs(hash(seed)) % 1_000_000 if seed else 0
    if "REAL" in kind or "FLOA" in kind or "DOUB" in kind:
        return 0.0
    if "BLOB" in kind:
        return seed.encode("utf-8")
    return f"{table}.{column}.{seed}" if seed else f"{table}.{column}"


def _insert(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, object],
    seed: str = "",
) -> None:
    """Insert against the frozen schema, driven by the schema rather than by guesses.

    Two adjustments make the corpus robust to the real frozen DDL instead of my
    assumptions about it. Keys the table does not have are dropped, and NOT NULL
    columns without a default that the caller did not supply are filled with a
    deterministic placeholder. Hard-coding each table's required columns would make
    the fixture fail for the wrong reason whenever the frozen schema disagreed.
    """
    info = list(connection.execute(f'PRAGMA table_info("{table}")'))
    available = {str(row[1]): row for row in info}

    usable: dict[str, object] = {k: v for k, v in values.items() if k in available}

    # An INTEGER PRIMARY KEY is a rowid alias and only accepts integers. Supplying
    # a string identifier for one is a datatype mismatch, so it is dropped and
    # AUTOINCREMENT assigns the value instead.
    for name, row in available.items():
        if (
            int(row[5])
            and "INT" in str(row[2]).upper()
            and name in usable
            and not isinstance(usable[name], int)
        ):
            del usable[name]

    for name, row in available.items():
        declared_type, not_null, default, is_pk = str(row[2]), int(row[3]), row[4], int(row[5])
        supplied = name in usable
        if supplied and not (usable[name] is None and not_null):
            continue
        # `pk` is the 1-based position within the primary key, so it is non-zero for
        # every member of a composite key. Only an INTEGER primary key is a rowid
        # alias that fills itself; a TEXT key column still needs a value, and
        # treating all PK columns as self-filling left composite keys empty.
        autofills_itself = bool(is_pk) and "INT" in declared_type.upper()
        if autofills_itself:
            continue
        # A NULL is only meaningful preservation coverage on a nullable column;
        # on a NOT NULL one it is simply invalid, so it is replaced rather than
        # silently dropping the row from the corpus.
        if not_null and default is None:
            usable[name] = _placeholder(table, name, declared_type, seed)
        elif supplied and usable[name] is None and not_null:
            del usable[name]

    names = ", ".join(f'"{k}"' for k in usable)
    marks = ", ".join("?" for _ in usable)
    connection.execute(
        f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(usable.values())
    )


def _populate(connection: sqlite3.Connection) -> None:
    now = "2026-07-30T00:00:00+00:00"

    _insert(
        connection,
        "workspaces",
        {
            "id": WORKSPACE_ID,
            "name": "Preservation corpus",
            "root_path": "/explicit/root",
            "storage_path": "/explicit/storage",
            "description": None,
            "index_status": "indexed",
            "settings": '{"b": 2, "a": 1}',
            "created_at": now,
            "updated_at": now,
        },
    )

    # memories: one row per awkward value class, plus numeric edges.
    for index, text in enumerate(AWKWARD_TEXT):
        _insert(
            connection,
            "memories",
            {
                "id": f"mem-{index:03d}",
                "workspace_id": WORKSPACE_ID,
                "content": text,
                "memory_type": "note",
                "lifecycle_state": "approved",
                "created_at": now,
                "updated_at": now,
                "importance": index,
                "metadata": None if index % 2 else '{"n": null}',
            },
            seed=f"mem-{index:03d}",
        )
    _insert(
        connection,
        "memories",
        {
            "id": "mem-edge",
            "workspace_id": WORKSPACE_ID,
            "content": "numeric edges",
            "memory_type": "note",
            "lifecycle_state": "proposed",
            "created_at": now,
            "updated_at": now,
            "importance": -1,
        },
    )

    # sources -> chunks with uneven fan-out: 0, 1 and many children.
    for source_index, child_count in enumerate((0, 1, 3)):
        source_id = f"src-{source_index}"
        _insert(
            connection,
            "sources",
            {
                "id": source_id,
                "workspace_id": WORKSPACE_ID,
                "path": f"/explicit/doc-{source_index}.md",
                "content_hash": f"{source_index:064x}",
                "status": "ingested",
                "created_at": now,
                "updated_at": now,
                "source_type": "markdown",
                "size_bytes": source_index * 1024,
            },
            seed=source_id,
        )
        for chunk_index in range(child_count):
            _insert(
                connection,
                "chunks",
                {
                    "id": f"chk-{source_index}-{chunk_index}",
                    "source_id": source_id,
                    "chunk_index": chunk_index,
                    "content": AWKWARD_TEXT[chunk_index % len(AWKWARD_TEXT)],
                    "start_offset": chunk_index * 100,
                    "end_offset": chunk_index * 100 + 99,
                    "content_hash": f"{chunk_index:064x}",
                },
                seed=f"chk-{source_index}-{chunk_index}",
            )

    # graph entities and relationships, plus provenance links back to memories.
    for entity_index in range(3):
        _insert(
            connection,
            "graph_entities",
            {
                "id": f"ent-{entity_index}",
                "workspace_id": WORKSPACE_ID,
                "name": AWKWARD_TEXT[entity_index],
                "entity_type": "concept",
                "created_at": now,
                "updated_at": now,
                "attributes": '{"z": 1, "a": 2}',
            },
            seed=f"ent-{entity_index}",
        )
    _insert(
        connection,
        "graph_relationships",
        {
            "id": "rel-0",
            "workspace_id": WORKSPACE_ID,
            "source_entity_id": "ent-0",
            "target_entity_id": "ent-1",
            "relationship_type": "relates_to",
            "created_at": now,
            "updated_at": now,
            "weight": 0.5,
            "attributes": None,
        },
    )
    _insert(
        connection,
        "entity_memory_links",
        {
            "id": "link-0",
            "entity_id": "ent-0",
            "memory_id": "mem-000",
            "created_at": now,
        },
    )

    # patterns and their occurrences/relationships.
    for pattern_index in range(2):
        _insert(
            connection,
            "patterns",
            {
                "id": f"pat-{pattern_index}",
                "workspace_id": WORKSPACE_ID,
                "name": f"pattern-{pattern_index}",
                "pattern_type": "recurring",
                "created_at": now,
                "updated_at": now,
                "confidence": 0.25 * (pattern_index + 1),
                "description": None,
            },
            seed=f"pat-{pattern_index}",
        )
    _insert(
        connection,
        "pattern_occurrences",
        {
            "id": "occ-0",
            "pattern_id": "pat-0",
            "memory_id": "mem-001",
            "created_at": now,
        },
    )
    _insert(
        connection,
        "pattern_relationships",
        {
            "id": "prel-0",
            "workspace_id": WORKSPACE_ID,
            "source_pattern_id": "pat-0",
            "target_pattern_id": "pat-1",
            "relationship_type": "implies",
            "created_at": now,
            "updated_at": now,
        },
    )

    # context packs and the control-plane trio.
    _insert(
        connection,
        "context_packs",
        {
            "id": "pack-0",
            "workspace_id": WORKSPACE_ID,
            "name": "pack",
            "purpose": "review",
            "query": "unicode ☃",
            "memory_ids_json": '["mem-000","mem-001"]',
            "source_references_json": "[]",
            "format": "markdown",
            "content": AWKWARD_TEXT[-1],
            "created_at": now,
        },
    )
    _insert(
        connection,
        "control_plane_manifests",
        {
            "workspace_id": WORKSPACE_ID,
            "schema_version": "omnivia.control_plane_manifest.v1",
            "payload_json": '{"resources": []}',
            "created_at": now,
            "updated_at": now,
        },
    )
    _insert(
        connection,
        "control_plane_resources",
        {
            "id": "res-0",
            "workspace_id": WORKSPACE_ID,
            "resource_type": "mcp.tool",
            "name": "tool",
            "payload_json": '{"a": 1}',
            "created_at": now,
            "updated_at": now,
        },
    )
    _insert(
        connection,
        "control_plane_events",
        {
            "id": "evt-0",
            "workspace_id": WORKSPACE_ID,
            "event_type": "resource.created",
            "resource_type": "mcp.tool",
            "resource_id": "res-0",
            "payload_json": "{}",
            "created_at": now,
        },
    )
