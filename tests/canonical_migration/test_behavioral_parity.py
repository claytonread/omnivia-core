"""Legacy-vs-canonical behavior must match, not just shape.

``test_parity.py`` proves the *signatures* line up (same fields, same
annotation spellings, same generated dunders). It cannot prove a method
*body* was ported correctly -- two methods with identical signatures can
still disagree on what they compute. This module runs the same
deterministic inputs through both trees and compares outputs: construction
and defaults, ``to_dict``/``from_dict`` round trips where available,
validation/error paths, mutation methods, and equality/hash/repr where
defined.

Timestamps and UUIDs are never compared directly across the two trees --
each side generates its own independently, so equality would be either
flaky (real timestamps) or a false negative (real UUIDs). Every case below
either injects an explicit value for those fields or asserts a
format/type/invariant instead (e.g. "is a valid ISO 8601 string", "changed
after calling touch()").
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pair(canonical_name: str) -> tuple[Any, Any]:
    """Import a canonical leaf and its legacy counterpart as (canonical, legacy)."""
    legacy_name = canonical_name.replace("omnivia_core", "omnivia_memory", 1)
    return importlib.import_module(canonical_name), importlib.import_module(legacy_name)


# ---------------------------------------------------------------------------
# _shared.validation
# ---------------------------------------------------------------------------


def test_shared_validation_scan_sensitive_fields_matches() -> None:
    canonical, legacy = _pair("omnivia_core._shared.validation")
    payload = {
        "name": "ok",
        "password": "hunter2",
        "nested": {"api_key": "x", "safe": 1},
        "items": [{"token": "y"}, {"safe": True}],
    }
    canonical_errors: list[str] = []
    legacy_errors: list[str] = []
    canonical.scan_sensitive_fields(payload, canonical_errors)
    legacy.scan_sensitive_fields(payload, legacy_errors)
    assert canonical_errors == legacy_errors
    assert canonical_errors  # sanity: the fixture actually triggers findings


@pytest.mark.parametrize(
    "value", ["2024-01-01T00:00:00Z", "2024-01-01T00:00:00+00:00", "not-a-timestamp", ""]
)
def test_shared_validation_iso_timestamp_matches(value: str) -> None:
    canonical, legacy = _pair("omnivia_core._shared.validation")
    canonical_errors: list[str] = []
    legacy_errors: list[str] = []
    canonical.validate_iso_timestamp("field", value, canonical_errors)
    legacy.validate_iso_timestamp("field", value, legacy_errors)
    assert canonical_errors == legacy_errors


def test_shared_validation_result_defaults_match() -> None:
    canonical, legacy = _pair("omnivia_core._shared.validation")
    canonical_result = canonical.ValidationResult(valid=True)
    legacy_result = legacy.ValidationResult(valid=True)
    assert (canonical_result.valid, canonical_result.errors, canonical_result.warnings) == (
        legacy_result.valid,
        legacy_result.errors,
        legacy_result.warnings,
    )


# ---------------------------------------------------------------------------
# knowledge.models
# ---------------------------------------------------------------------------


def test_knowledge_contract_version_parse_matches() -> None:
    canonical, legacy = _pair("omnivia_core.knowledge.models")
    canonical_version = canonical.ContractVersion.parse("2.5")
    legacy_version = legacy.ContractVersion.parse("2.5")
    assert (canonical_version.major, canonical_version.minor) == (legacy_version.major, legacy_version.minor)
    assert str(canonical_version) == str(legacy_version)

    # parse() is idempotent on an already-parsed instance, in both trees.
    assert canonical.ContractVersion.parse(canonical_version) is canonical_version
    assert legacy.ContractVersion.parse(legacy_version) is legacy_version


def test_knowledge_source_ref_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.knowledge.models")
    payload = {
        "source_id": "src-1",
        "source_type": "file",
        "path": "a/b.py",
        "confidence": "EXTRACTED",
        "missing_evidence": False,
        "metadata": {"k": "v"},
    }
    canonical_ref = canonical.SourceRef.from_dict(payload)
    legacy_ref = legacy.SourceRef.from_dict(payload)
    assert canonical_ref.to_dict() == legacy_ref.to_dict() == payload | {
        "locator": None,
        "quote_preview": None,
    }


def test_knowledge_object_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.knowledge.models")
    payload = {
        "id": "obj-1",
        "space_id": "space-1",
        "kind": "note",
        "title": "Title",
        "source_refs": [{"source_id": "s1", "source_type": "note", "confidence": 0.5}],
        "confidence": 0.5,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_obj = canonical.KnowledgeObject.from_dict(payload)
    legacy_obj = legacy.KnowledgeObject.from_dict(payload)
    assert canonical_obj.to_dict() == legacy_obj.to_dict()


# ---------------------------------------------------------------------------
# app_manifest / app_shell_bridge / component_contract / module_manifest
# (plain dataclasses, no to_dict/from_dict -- defaults and dataclass-generated
# equality are the behavior worth pinning)
# ---------------------------------------------------------------------------


def test_app_manifest_defaults_match() -> None:
    canonical, legacy = _pair("omnivia_core.app_manifest.models")
    canonical_manifest = canonical.AppManifest(
        manifest_version="1.0",
        app_id="app",
        display_name="App",
        app_version="1.0",
        required_module_id="mod",
    )
    legacy_manifest = legacy.AppManifest(
        manifest_version="1.0",
        app_id="app",
        display_name="App",
        app_version="1.0",
        required_module_id="mod",
    )
    assert canonical_manifest.state.value == legacy_manifest.state.value == "draft"
    assert canonical_manifest.required_data_sources == legacy_manifest.required_data_sources == []
    assert canonical_manifest.validation.is_valid == legacy_manifest.validation.is_valid is True
    # Two independently-constructed instances with the same field values are
    # equal within their own tree (dataclass-generated __eq__).
    assert canonical_manifest == canonical.AppManifest(
        manifest_version="1.0",
        app_id="app",
        display_name="App",
        app_version="1.0",
        required_module_id="mod",
    )


def test_app_shell_bridge_defaults_match() -> None:
    canonical, legacy = _pair("omnivia_core.app_shell_bridge.models")
    canonical_context = canonical.AppShellHostContext(
        app_id="app",
        app_name="App",
        entity_label="label",
        runtime_state=canonical.AppShellRuntimeState.READY,
    )
    legacy_context = legacy.AppShellHostContext(
        app_id="app",
        app_name="App",
        entity_label="label",
        runtime_state=legacy.AppShellRuntimeState.READY,
    )
    assert canonical_context.sources == legacy_context.sources == []
    assert canonical_context.host_command_ids == legacy_context.host_command_ids == []
    assert canonical_context.validation.is_valid == legacy_context.validation.is_valid is True


def test_component_contract_optional_agent_backed_defaults_to_none() -> None:
    canonical, legacy = _pair("omnivia_core.component_contract.models")
    canonical_contract = canonical.ComponentContract(
        contract_version="1.0",
        component_id="c1",
        display_name="Comp",
        family=canonical.ComponentFamily.LOGIC,
        version="1.0",
    )
    legacy_contract = legacy.ComponentContract(
        contract_version="1.0",
        component_id="c1",
        display_name="Comp",
        family=legacy.ComponentFamily.LOGIC,
        version="1.0",
    )
    assert canonical_contract.agent_backed is None
    assert legacy_contract.agent_backed is None
    assert canonical_contract.provenance_behavior.value == legacy_contract.provenance_behavior.value


def test_module_manifest_defaults_match() -> None:
    canonical, legacy = _pair("omnivia_core.module_manifest.models")
    canonical_manifest = canonical.ModuleManifest(
        manifest_version="1.0",
        module_id="mod",
        display_name="Module",
        kind=canonical.ModuleKind.APPS,
        version="1.0",
        entrypoint=canonical.Entrypoint(module="pkg.mod"),
        integrity=canonical.Integrity(algorithm="sha256", digest="deadbeef"),
    )
    legacy_manifest = legacy.ModuleManifest(
        manifest_version="1.0",
        module_id="mod",
        display_name="Module",
        kind=legacy.ModuleKind.APPS,
        version="1.0",
        entrypoint=legacy.Entrypoint(module="pkg.mod"),
        integrity=legacy.Integrity(algorithm="sha256", digest="deadbeef"),
    )
    assert canonical_manifest.permissions == legacy_manifest.permissions == []
    assert canonical_manifest.published_targets == legacy_manifest.published_targets == []


# ---------------------------------------------------------------------------
# lifecycle.models + lifecycle.rules
# ---------------------------------------------------------------------------


def test_lifecycle_state_transition_matrix_matches() -> None:
    canonical, legacy = _pair("omnivia_core.lifecycle.models")
    state_names = [s.name for s in canonical.LifecycleState]
    assert state_names == [s.name for s in legacy.LifecycleState]
    for from_name in state_names:
        for to_name in state_names:
            canonical_from = canonical.LifecycleState[from_name]
            canonical_to = canonical.LifecycleState[to_name]
            legacy_from = legacy.LifecycleState[from_name]
            legacy_to = legacy.LifecycleState[to_name]
            assert canonical_from.can_transition_to(canonical_to) == legacy_from.can_transition_to(
                legacy_to
            ), f"{from_name} -> {to_name} drifted"
        assert canonical_from.is_terminal() == legacy_from.is_terminal()
        assert canonical_from.is_approved() == legacy_from.is_approved()
        assert (
            canonical_from.requires_human_approval_to_approve()
            == legacy_from.requires_human_approval_to_approve()
        )


def test_lifecycle_rules_get_initial_state_matches() -> None:
    canonical, legacy = _pair("omnivia_core.lifecycle.rules")
    for created_by_name in ("HUMAN", "AGENT"):
        canonical_state = canonical.LifecycleRules.get_initial_state(
            canonical.CreatedBy[created_by_name]
        )
        legacy_state = legacy.LifecycleRules.get_initial_state(legacy.CreatedBy[created_by_name])
        assert canonical_state.value == legacy_state.value


def test_lifecycle_rules_delegating_helpers_match() -> None:
    canonical, legacy = _pair("omnivia_core.lifecycle.rules")
    canonical_models = importlib.import_module("omnivia_core.lifecycle.models")
    legacy_models = importlib.import_module("omnivia_memory.lifecycle.models")
    for state_name in ("PROPOSED", "OBSERVED", "APPROVED", "REJECTED"):
        canonical_state = canonical_models.LifecycleState[state_name]
        legacy_state = legacy_models.LifecycleState[state_name]
        assert canonical.LifecycleRules.requires_human_approval(
            canonical_state
        ) == legacy.LifecycleRules.requires_human_approval(legacy_state)
        assert canonical.LifecycleRules.is_approved_state(
            canonical_state
        ) == legacy.LifecycleRules.is_approved_state(legacy_state)
        assert canonical.LifecycleRules.is_terminal_state(
            canonical_state
        ) == legacy.LifecycleRules.is_terminal_state(legacy_state)


# ---------------------------------------------------------------------------
# memory_graph.models
# ---------------------------------------------------------------------------


def test_memory_graph_source_ref_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.memory_graph.models")
    payload = {"source_id": "s1", "quote_preview": "hello", "confidence": 0.75}
    canonical_ref = canonical.SourceRef.from_dict(payload)
    legacy_ref = legacy.SourceRef.from_dict(payload)
    assert canonical_ref.to_dict() == legacy_ref.to_dict()

    payload_str_confidence = {"source_id": "s2", "confidence": "high"}
    canonical_ref2 = canonical.SourceRef.from_dict(payload_str_confidence)
    legacy_ref2 = legacy.SourceRef.from_dict(payload_str_confidence)
    assert canonical_ref2.to_dict() == legacy_ref2.to_dict()


def test_memory_graph_memory_entity_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.memory_graph.models")
    payload = {
        "id": "e1",
        "workspace_id": "w1",
        "type": "person",
        "canonical_name": "Alice",
        "aliases": ["A."],
        "confidence": 0.9,
        "source_refs": [{"source_id": "s1"}],
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_entity = canonical.MemoryEntity.from_dict(payload)
    legacy_entity = legacy.MemoryEntity.from_dict(payload)
    assert canonical_entity.to_dict() == legacy_entity.to_dict()


# ---------------------------------------------------------------------------
# provenance.models
# ---------------------------------------------------------------------------


def test_provenance_source_round_trip_and_equality_match() -> None:
    canonical, legacy = _pair("omnivia_core.provenance.models")
    payload = {"type": "file", "reference": "src/foo.py", "description": "the foo module"}
    canonical_source = canonical.Source.from_dict(payload)
    legacy_source = legacy.Source.from_dict(payload)
    assert canonical_source.to_dict() == legacy_source.to_dict() == payload

    canonical_other = canonical.Source.from_dict(payload)
    legacy_other = legacy.Source.from_dict(payload)
    assert canonical_source == canonical_other
    assert legacy_source == legacy_other
    assert repr(canonical_source) == repr(legacy_source)


# ---------------------------------------------------------------------------
# graph.models
# ---------------------------------------------------------------------------


def test_graph_entity_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.graph.models")
    payload = {
        "id": "ent-1",
        "name": "Alice",
        "entity_type": "person",
        "source_id": "src-1",
        "approval_status": "proposed",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_entity = canonical.Entity.from_dict(payload)
    legacy_entity = legacy.Entity.from_dict(payload)
    assert canonical_entity.to_dict() == legacy_entity.to_dict() == payload


def test_graph_entity_mutation_methods_match() -> None:
    canonical, legacy = _pair("omnivia_core.graph.models")
    for method_name in ("approve", "reject", "supersede"):
        canonical_entity = canonical.Entity(name="A", entity_type=canonical.EntityType.PERSON)
        legacy_entity = legacy.Entity(name="A", entity_type=legacy.EntityType.PERSON)
        getattr(canonical_entity, method_name)()
        getattr(legacy_entity, method_name)()
        assert canonical_entity.approval_status.value == legacy_entity.approval_status.value

    canonical_entity = canonical.Entity(name="A", entity_type=canonical.EntityType.PERSON)
    before = canonical_entity.updated_at
    canonical_entity.touch()
    assert canonical_entity.updated_at >= before


def test_graph_entity_equality_and_hash_are_id_only_in_both_trees() -> None:
    """Behavioral quirk worth pinning: __eq__/__hash__ compare only ``id``,
    so two entities with the same id but different names are still equal."""
    canonical, legacy = _pair("omnivia_core.graph.models")
    canonical_a = canonical.Entity(id="same", name="A", entity_type=canonical.EntityType.PERSON)
    canonical_b = canonical.Entity(id="same", name="B", entity_type=canonical.EntityType.PERSON)
    legacy_a = legacy.Entity(id="same", name="A", entity_type=legacy.EntityType.PERSON)
    legacy_b = legacy.Entity(id="same", name="B", entity_type=legacy.EntityType.PERSON)
    assert (canonical_a == canonical_b) == (legacy_a == legacy_b) is True
    assert (hash(canonical_a) == hash(canonical_b)) == (hash(legacy_a) == hash(legacy_b)) is True


# ---------------------------------------------------------------------------
# graph.search_models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "q", "depth": -1},
        {"query": "q", "limit": 0},
    ],
)
def test_graph_search_query_validation_errors_match(kwargs: dict[str, Any]) -> None:
    canonical, legacy = _pair("omnivia_core.graph.search_models")
    with pytest.raises(ValueError) as canonical_exc:
        canonical.GraphSearchQuery(**kwargs)
    with pytest.raises(ValueError) as legacy_exc:
        legacy.GraphSearchQuery(**kwargs)
    assert str(canonical_exc.value) == str(legacy_exc.value)


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_graph_search_result_validation_errors_match(score: float) -> None:
    canonical, legacy = _pair("omnivia_core.graph.search_models")
    canonical_graph = importlib.import_module("omnivia_core.graph.models")
    legacy_graph = importlib.import_module("omnivia_memory.graph.models")
    canonical_entity = canonical_graph.Entity(name="A", entity_type=canonical_graph.EntityType.PERSON)
    legacy_entity = legacy_graph.Entity(name="A", entity_type=legacy_graph.EntityType.PERSON)
    with pytest.raises(ValueError) as canonical_exc:
        canonical.GraphSearchResult(entity=canonical_entity, score=score, matched_on="name")
    with pytest.raises(ValueError) as legacy_exc:
        legacy.GraphSearchResult(entity=legacy_entity, score=score, matched_on="name")
    assert str(canonical_exc.value) == str(legacy_exc.value)


def test_graph_search_result_set_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.graph.search_models")
    payload = {
        "results": [
            {
                "entity": {
                    "id": "e1",
                    "name": "A",
                    "entity_type": "person",
                    "source_id": None,
                    "approval_status": "proposed",
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "updated_at": "2024-01-01T00:00:00+00:00",
                },
                "score": 0.5,
                "matched_on": "name",
                "context_entities": [],
            }
        ],
        "total_count": 1,
        "query": {"query": "q", "entity_types": [], "relationship_types": [], "depth": 1, "limit": None},
    }
    canonical_set = canonical.GraphSearchResultSet.from_dict(payload)
    legacy_set = legacy.GraphSearchResultSet.from_dict(payload)
    assert canonical_set.to_dict() == legacy_set.to_dict() == payload


def test_graph_search_models_ast_has_only_three_permitted_record_classes() -> None:
    """The canonical AST must contain exactly the three query/result record
    classes and no copied/renamed top-level scoring function -- guards
    against a scoring helper sneaking back in under a different name, which
    the by-name comparison in test_parity.py cannot catch."""
    path = REPO_ROOT / "src" / "omnivia_core" / "graph" / "search_models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top_level_classes = {
        node.name for node in ast.iter_child_nodes(tree) if isinstance(node, ast.ClassDef)
    }
    top_level_functions = {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert top_level_classes == {"GraphSearchQuery", "GraphSearchResult", "GraphSearchResultSet"}
    assert top_level_functions == set()


# ---------------------------------------------------------------------------
# ingestion.models
# ---------------------------------------------------------------------------


def test_ingestion_extraction_result_success_and_failure_match() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.models")
    canonical_success = canonical.ExtractionResult.success("hello world")
    legacy_success = legacy.ExtractionResult.success("hello world")
    assert canonical_success.status.value == legacy_success.status.value
    # hashlib.sha256 is deterministic, so the digest itself is safe to compare.
    assert canonical_success.hash == legacy_success.hash

    canonical_failure = canonical.ExtractionResult.failure("boom")
    legacy_failure = legacy.ExtractionResult.failure("boom")
    assert (canonical_failure.status.value, canonical_failure.error) == (
        legacy_failure.status.value,
        legacy_failure.error,
    )


def test_ingestion_source_round_trip_and_mutation_match() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.models")
    payload = {
        "id": "src-1",
        "path": "a.md",
        "file_type": "markdown",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_source = canonical.Source.from_dict(payload)
    legacy_source = legacy.Source.from_dict(payload)
    assert canonical_source.to_dict() == legacy_source.to_dict()

    before = canonical_source.updated_at
    canonical_source.touch()
    assert canonical_source.updated_at >= before


def test_ingestion_file_inventory_mutation_methods_match() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.models")
    canonical_inv = canonical.FileInventory(
        path=canonical.Path("a.md"),
        extension=".md",
        size=10,
        modified_time="2024-01-01T00:00:00+00:00",
        file_type=canonical.FileType.MARKDOWN,
    )
    legacy_inv = legacy.FileInventory(
        path=legacy.Path("a.md"),
        extension=".md",
        size=10,
        modified_time="2024-01-01T00:00:00+00:00",
        file_type=legacy.FileType.MARKDOWN,
    )
    canonical_inv.mark_error("boom")
    legacy_inv.mark_error("boom")
    assert (canonical_inv.parse_status.value, canonical_inv.error_message) == (
        legacy_inv.parse_status.value,
        legacy_inv.error_message,
    )
    canonical_inv.mark_success()
    legacy_inv.mark_success()
    assert (canonical_inv.parse_status.value, canonical_inv.error_message) == (
        legacy_inv.parse_status.value,
        legacy_inv.error_message,
    )


# ---------------------------------------------------------------------------
# ingestion.watcher.models
# ---------------------------------------------------------------------------


def test_watcher_source_reference_is_stale_matrix_matches() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.watcher.models")
    cases = [(None, None), (None, "abc"), ("abc", None), ("abc", "abc"), ("abc", "def")]
    for last_known, current in cases:
        canonical_ref = canonical.SourceReference(
            watched_path="w",
            source_path="s",
            source_id="id",
            workspace_id="ws",
            last_known_hash=last_known,
        )
        legacy_ref = legacy.SourceReference(
            watched_path="w",
            source_path="s",
            source_id="id",
            workspace_id="ws",
            last_known_hash=last_known,
        )
        assert canonical_ref.is_stale(current) == legacy_ref.is_stale(current), (last_known, current)


def test_watcher_file_change_round_trip_matches() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.watcher.models")
    payload = {
        "path": "a.md",
        "event_type": "modified",
        "old_path": None,
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    canonical_change = canonical.FileChange.from_dict(payload)
    legacy_change = legacy.FileChange.from_dict(payload)
    assert canonical_change.to_dict() == legacy_change.to_dict() == payload


def test_watcher_debounce_config_defaults_match() -> None:
    canonical, legacy = _pair("omnivia_core.ingestion.watcher.models")
    assert canonical.DebounceConfig().to_dict() == legacy.DebounceConfig().to_dict()


# ---------------------------------------------------------------------------
# memory.models
# ---------------------------------------------------------------------------


def test_memory_create_to_memory_delegates_to_lifecycle_rules_in_both_trees() -> None:
    """Regression coverage for the exact bug this repair fixes: MemoryCreate
    must derive its initial lifecycle state from LifecycleRules, not an
    inlined copy of the same logic."""
    canonical, legacy = _pair("omnivia_core.memory.models")
    canonical_provenance = importlib.import_module("omnivia_core.provenance.models")
    legacy_provenance = importlib.import_module("omnivia_memory.provenance.models")
    canonical_source = canonical_provenance.Source(type=canonical_provenance.SourceType.HUMAN, reference="r")
    legacy_source = legacy_provenance.Source(type=legacy_provenance.SourceType.HUMAN, reference="r")

    for created_by_name in ("HUMAN", "AGENT"):
        canonical_create = canonical.MemoryCreate(
            content="c", source=canonical_source, created_by=canonical.CreatedBy[created_by_name]
        )
        legacy_create = legacy.MemoryCreate(
            content="c", source=legacy_source, created_by=legacy.CreatedBy[created_by_name]
        )
        canonical_memory = canonical_create.to_memory()
        legacy_memory = legacy_create.to_memory()
        assert canonical_memory.lifecycle_state.value == legacy_memory.lifecycle_state.value


def test_memory_round_trip_and_mutation_methods_match() -> None:
    canonical, legacy = _pair("omnivia_core.memory.models")
    canonical_provenance = importlib.import_module("omnivia_core.provenance.models")
    legacy_provenance = importlib.import_module("omnivia_memory.provenance.models")
    payload = {
        "id": "mem-1",
        "workspace_id": "ws",
        "content": "hello",
        "source": {"type": "human", "reference": "r", "description": None},
        "lifecycle_state": "proposed",
        "memory_type": "general",
        "created_by": "agent",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_memory = canonical.Memory.from_dict(payload)
    legacy_memory = legacy.Memory.from_dict(payload)
    assert canonical_memory.to_dict() == legacy_memory.to_dict() == payload

    canonical_memory.transition_to(canonical.LifecycleState.APPROVED)
    legacy_memory.transition_to(legacy.LifecycleState.APPROVED)
    assert canonical_memory.lifecycle_state.value == legacy_memory.lifecycle_state.value == "approved"

    canonical_memory.update_content("new content")
    legacy_memory.update_content("new content")
    assert canonical_memory.content == legacy_memory.content == "new content"
    assert canonical_provenance is not None and legacy_provenance is not None  # both imported, used above


def test_memory_update_apply_to_matches() -> None:
    canonical, legacy = _pair("omnivia_core.memory.models")
    canonical_provenance = importlib.import_module("omnivia_core.provenance.models")
    legacy_provenance = importlib.import_module("omnivia_memory.provenance.models")
    canonical_memory = canonical.Memory(
        content="old",
        source=canonical_provenance.Source(type=canonical_provenance.SourceType.HUMAN, reference="r"),
        created_by=canonical.CreatedBy.HUMAN,
    )
    legacy_memory = legacy.Memory(
        content="old",
        source=legacy_provenance.Source(type=legacy_provenance.SourceType.HUMAN, reference="r"),
        created_by=legacy.CreatedBy.HUMAN,
    )

    canonical_changed = canonical.MemoryUpdate(content="old").apply_to(canonical_memory)
    legacy_changed = legacy.MemoryUpdate(content="old").apply_to(legacy_memory)
    assert canonical_changed == legacy_changed is False

    canonical_changed = canonical.MemoryUpdate(content="new").apply_to(canonical_memory)
    legacy_changed = legacy.MemoryUpdate(content="new").apply_to(legacy_memory)
    assert canonical_changed == legacy_changed is True
    assert canonical_memory.content == legacy_memory.content == "new"


# ---------------------------------------------------------------------------
# workspace.models
# ---------------------------------------------------------------------------


def test_workspace_round_trip_and_mutation_methods_match() -> None:
    canonical, legacy = _pair("omnivia_core.workspace.models")
    payload = {
        "id": "ws-1",
        "name": "My Workspace",
        "root_path": "/tmp/root",
        "storage_path": "/tmp/storage",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    canonical_ws = canonical.Workspace.from_dict(payload)
    legacy_ws = legacy.Workspace.from_dict(payload)
    canonical_dict = canonical_ws.to_dict()
    legacy_dict = legacy_ws.to_dict()
    assert canonical_dict == legacy_dict
    assert canonical_dict["index_status"] == "unindexed"

    canonical_ws.mark_indexed()
    legacy_ws.mark_indexed()
    assert canonical_ws.index_status.value == legacy_ws.index_status.value == "indexed"
    assert (canonical_ws.last_indexed_at is not None) == (legacy_ws.last_indexed_at is not None) is True

    canonical_ws.mark_error()
    legacy_ws.mark_error()
    assert canonical_ws.index_status.value == legacy_ws.index_status.value == "error"


def test_workspace_update_apply_to_matches() -> None:
    canonical, legacy = _pair("omnivia_core.workspace.models")
    canonical_ws = canonical.Workspace(name="A", root_path="/r", storage_path="/s")
    legacy_ws = legacy.Workspace(name="A", root_path="/r", storage_path="/s")

    canonical_changed = canonical.WorkspaceUpdate(name="A").apply_to(canonical_ws)
    legacy_changed = legacy.WorkspaceUpdate(name="A").apply_to(legacy_ws)
    assert canonical_changed == legacy_changed is False

    canonical_changed = canonical.WorkspaceUpdate(name="B").apply_to(canonical_ws)
    legacy_changed = legacy.WorkspaceUpdate(name="B").apply_to(legacy_ws)
    assert canonical_changed == legacy_changed is True
    assert canonical_ws.name == legacy_ws.name == "B"


def test_workspace_create_to_workspace_matches_given_explicit_storage_path() -> None:
    """``storage_path`` defaults through ``Path.home() / ... / <random id>``,
    which is UUID-derived and therefore never compared directly; supplying
    it explicitly keeps the rest of the conversion deterministic."""
    canonical, legacy = _pair("omnivia_core.workspace.models")
    canonical_create = canonical.WorkspaceCreate(
        name="A", root_path=canonical.Path("/tmp/root"), storage_path=canonical.Path("/tmp/storage")
    )
    legacy_create = legacy.WorkspaceCreate(
        name="A", root_path=legacy.Path("/tmp/root"), storage_path=legacy.Path("/tmp/storage")
    )
    canonical_ws = canonical_create.to_workspace()
    legacy_ws = legacy_create.to_workspace()
    assert (canonical_ws.root_path, canonical_ws.storage_path, canonical_ws.name) == (
        legacy_ws.root_path,
        legacy_ws.storage_path,
        legacy_ws.name,
    )
