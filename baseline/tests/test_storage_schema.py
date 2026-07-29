"""Tests for the frozen local storage schema."""

from __future__ import annotations

from baseline.determinism import load_json
from baseline.storage import (
    STORAGE_SCHEMA_PATH,
    build_storage_schema_inventory,
    verify_storage_schema_inventory,
)

#: Tables Core's Database creates today. Phase 1 must not change this set.
EXPECTED_TABLES = {
    "chunks",
    "context_packs",
    "control_plane_events",
    "control_plane_manifests",
    "control_plane_resources",
    "entity_memory_links",
    "graph_entities",
    "graph_relationships",
    "memories",
    "pattern_occurrences",
    "pattern_relationships",
    "patterns",
    "sources",
    "workspaces",
}


def test_tracked_schema_matches_the_current_database() -> None:
    assert verify_storage_schema_inventory() == []


def test_schema_capture_is_reproducible() -> None:
    assert build_storage_schema_inventory() == build_storage_schema_inventory()


def test_every_expected_table_is_frozen() -> None:
    inventory = load_json(STORAGE_SCHEMA_PATH)

    assert set(inventory["tables"]) == EXPECTED_TABLES


def test_memories_table_records_its_workspace_and_provenance_columns() -> None:
    inventory = load_json(STORAGE_SCHEMA_PATH)
    columns = {column["name"] for column in inventory["tables"]["memories"]["columns"]}

    assert {"workspace_id", "source_type", "source_reference", "lifecycle_state"} <= columns


def test_sources_table_keeps_the_unique_workspace_path_index() -> None:
    """The uniqueness guarantee behind ingestion de-duplication is part of the freeze."""
    inventory = load_json(STORAGE_SCHEMA_PATH)
    indexes = inventory["tables"]["sources"]["indexes"]

    unique = [index for index in indexes if index["unique"]]
    assert any(index["columns"] == ["workspace_id", "file_path"] for index in unique)


def test_drift_check_names_the_column_that_changed() -> None:
    from baseline.determinism import diff_json

    inventory = build_storage_schema_inventory()
    inventory["tables"]["memories"]["columns"].append(
        {"name": "tenant_id", "type": "TEXT", "not_null": False, "default": None, "primary_key": 0}
    )

    differences = diff_json(load_json(STORAGE_SCHEMA_PATH), inventory)

    assert any("tenant_id" in difference for difference in differences)
