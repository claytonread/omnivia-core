"""Tests for the public Python export inventory and its drift check."""

from __future__ import annotations

import json
from pathlib import Path

from baseline import CORE_PACKAGE, REPO_ROOT
from baseline.determinism import diff_json, load_json
from baseline.inventory import (
    PUBLIC_EXPORTS_PATH,
    build_public_export_inventory,
    ensure_core_importable,
    verify_public_export_inventory,
)


def test_tracked_inventory_matches_the_current_public_surface() -> None:
    assert verify_public_export_inventory() == []


def test_inventory_is_reproducible() -> None:
    """Two builds in the same checkout must be byte-identical."""
    first = json.dumps(build_public_export_inventory(), sort_keys=True)
    second = json.dumps(build_public_export_inventory(), sort_keys=True)

    assert first == second


def test_core_is_imported_from_this_checkout() -> None:
    """A baseline captured from a shadow install would describe the wrong tree."""
    module = ensure_core_importable()

    assert module.__name__ == CORE_PACKAGE
    assert REPO_ROOT in Path(module.__file__).resolve().parents


def test_inventory_records_where_each_root_export_is_defined() -> None:
    inventory = load_json(PUBLIC_EXPORTS_PATH)
    bindings = inventory["root"]["bindings"]

    assert bindings["KnowledgeSpace"]["defined_in"] == "omnivia_memory.knowledge.models"
    assert "omnivia_memory.knowledge" in bindings["KnowledgeSpace"]["exported_by"]


def test_inventory_records_compatibility_exports_kept_out_of_all() -> None:
    """The runtime symbols the root docstring calls out must stay importable."""
    inventory = load_json(PUBLIC_EXPORTS_PATH)

    compatibility = set(inventory["root"]["compatibility_exports"])
    assert {"Database", "MemoryService", "MemoryCreate", "MemoryUpdate"} <= compatibility
    assert compatibility.isdisjoint(inventory["root"]["all"])


def test_inventory_captures_contract_detail_not_just_names() -> None:
    """Enum members and dataclass fields are part of the frozen contract."""
    inventory = load_json(PUBLIC_EXPORTS_PATH)
    knowledge_models = inventory["modules"]["omnivia_memory.knowledge.models"]["defines"]

    # GraphConfidence uses upper-case values, unlike the lower-case confidence
    # strings the memory graph contracts carry. The baseline records the values
    # as they are so the inconsistency cannot be lost in the migration.
    confidence = knowledge_models["GraphConfidence"]
    assert confidence["kind"] == "enum"
    assert confidence["members"] == [
        "EXTRACTED='EXTRACTED'",
        "INFERRED='INFERRED'",
        "AMBIGUOUS='AMBIGUOUS'",
    ]

    space = knowledge_models["KnowledgeSpace"]
    assert space["kind"] == "dataclass"
    assert "contract_version" in space["fields"]


def test_drift_check_names_the_symbol_that_moved() -> None:
    """A renamed export must fail with the export's name, not a generic mismatch."""
    inventory = build_public_export_inventory()
    inventory["root"]["bindings"]["KnowledgeSpace"]["defined_in"] = "omnivia.contracts.models"

    differences = diff_json(load_json(PUBLIC_EXPORTS_PATH), inventory)

    assert any("KnowledgeSpace" in item and "defined_in" in item for item in differences)
