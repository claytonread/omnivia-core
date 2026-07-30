"""Tests for the public Python export inventory and its drift check."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from baseline import CORE_PACKAGE, REPO_ROOT
from baseline.determinism import diff_json, load_json
from baseline.inventory import (
    FACADE_ROUTES,
    PUBLIC_EXPORTS_PATH,
    _expected_facade_descriptor,
    _facade_route_problems,
    _normalize_expected_for_facade_routes,
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


# --------------------------------------------------------------------------
# Compatibility-facade route normalization: the frozen Phase 0 baseline never
# describes converted leaves as facades, so verify_public_export_inventory
# only accepts that specific delta after these checks pass, and only exactly as
# they describe it. Each case below asserts a specific way the delta could be
# wrong still fails closed.
# --------------------------------------------------------------------------


def test_facade_route_problems_is_empty_for_the_real_checkout() -> None:
    assert _facade_route_problems(build_public_export_inventory()) == []


def test_facade_route_problems_reports_a_wrong_canonical_route() -> None:
    actual = build_public_export_inventory()
    bad_routes = copy.deepcopy(FACADE_ROUTES)
    bad_routes["omnivia_memory.provenance.models"]["Source"] = "omnivia_core.lifecycle.models"

    problems = _facade_route_problems(actual, routes=bad_routes)

    assert any("has no such symbol" in problem for problem in problems)


def test_facade_route_problems_rejects_an_app_manifest_collision_owner_swap() -> None:
    """``ValidationResult`` and ``ProvenanceRequirement`` are name collisions
    across independent domains, so a route repointed at another domain's
    same-named class still *resolves*: the routed canonical module has the
    symbol, and only the identity check can tell the swap apart from the real
    owner. It must, or the normalization would silently rewrite the App
    Manifest contract's owner to another domain's."""
    actual = build_public_export_inventory()
    for symbol, wrong_owner in (
        ("ValidationResult", "omnivia_core._shared.validation"),
        ("ProvenanceRequirement", "omnivia_core.component_contract.models"),
    ):
        bad_routes = copy.deepcopy(FACADE_ROUTES)
        bad_routes["omnivia_memory.app_manifest.models"][symbol] = wrong_owner

        problems = _facade_route_problems(actual, routes=bad_routes)

        assert any(
            "is not the exact object bound at" in problem
            and f"app_manifest.models.{symbol}" in problem
            for problem in problems
        ), f"routing {symbol} to {wrong_owner} was accepted: {problems}"


def test_facade_route_problems_rejects_a_component_contract_collision_owner_swap() -> None:
    """The same collision trap from the other side. The Component Contract owns
    its own ``ValidationResult`` and ``ProvenanceRequirement``, and it is the
    owner the legacy *root* binds ``ProvenanceRequirement`` from -- so a route
    repointed at another domain's same-named class would both swap the leaf's
    contract and rewrite that root binding's owner. Only the identity check can
    tell the swap apart from the real owner."""
    actual = build_public_export_inventory()
    for symbol, wrong_owner in (
        ("ValidationResult", "omnivia_core._shared.validation"),
        ("ProvenanceRequirement", "omnivia_core.app_manifest.models"),
    ):
        bad_routes = copy.deepcopy(FACADE_ROUTES)
        bad_routes["omnivia_memory.component_contract.models"][symbol] = wrong_owner

        problems = _facade_route_problems(actual, routes=bad_routes)

        assert any(
            "is not the exact object bound at" in problem
            and f"component_contract.models.{symbol}" in problem
            for problem in problems
        ), f"routing {symbol} to {wrong_owner} was accepted: {problems}"


def test_facade_route_problems_reports_a_non_identical_object(monkeypatch) -> None:
    """A canonical attribute that resolves but is not the exact legacy object (a
    lookalike rebound after both modules already imported) must fail identity."""
    import omnivia_core.provenance.models as canonical_provenance

    class _LookalikeSource:
        pass

    monkeypatch.setattr(canonical_provenance, "Source", _LookalikeSource)

    problems = _facade_route_problems(build_public_export_inventory())

    assert any(
        "is not the exact object bound at" in problem and "provenance.models.Source" in problem
        for problem in problems
    )


def test_facade_route_problems_reports_a_still_locally_defined_symbol() -> None:
    actual = copy.deepcopy(build_public_export_inventory())
    actual["modules"]["omnivia_memory.provenance.models"]["defines"]["Source"] = {
        "kind": "class",
        "bases": ["builtins.object"],
        "methods": [],
    }

    problems = _facade_route_problems(actual)

    assert any(
        "still locally defined" in problem and "provenance.models.Source" in problem
        for problem in problems
    )


def test_facade_route_problems_reports_contract_descriptor_drift() -> None:
    actual = build_public_export_inventory()
    frozen = copy.deepcopy(load_json(PUBLIC_EXPORTS_PATH))
    frozen["modules"]["omnivia_memory.lifecycle.models"]["defines"]["LifecycleState"]["members"] = [
        "PROPOSED='proposed'"
    ]

    problems = _facade_route_problems(actual, frozen=frozen)

    assert any(
        "contract drifted" in problem and "lifecycle.models.LifecycleState" in problem
        for problem in problems
    )


def test_facade_descriptor_rewrite_is_exact_and_symbol_specific() -> None:
    actual = build_public_export_inventory()
    frozen = copy.deepcopy(load_json(PUBLIC_EXPORTS_PATH))
    descriptor = frozen["modules"]["omnivia_memory.app_manifest.validation"]["defines"][
        "validate_app_manifest"
    ]

    # The exact historical descriptor gets the one sanctioned owner rename.
    assert _facade_route_problems(actual, frozen=frozen) == []

    # Package-looking text in a default is contract data, not ownership
    # metadata. A broad recursive replacement would hide this mutation.
    descriptor["signature"] = (
        "(data: Dict[str, Any] = "
        "'omnivia_memory.app_manifest.models.AppManifest') -> "
        "omnivia_memory.app_manifest.models.AppManifest"
    )
    problems = _facade_route_problems(actual, frozen=frozen)
    assert any(
        "contract drifted" in problem
        and "app_manifest.validation.validate_app_manifest" in problem
        for problem in problems
    )

    # Assert the helper itself leaves a near miss byte-for-structure unchanged.
    # The former broad recursive replacement would have altered both
    # package-looking substrings, including the default value.
    near_miss = copy.deepcopy(descriptor)
    assert (
        _expected_facade_descriptor(
            "omnivia_memory.app_manifest.validation",
            "validate_app_manifest",
            near_miss,
        )
        is near_miss
    )
    assert "omnivia_memory" in near_miss["signature"]

    exact_old = copy.deepcopy(
        load_json(PUBLIC_EXPORTS_PATH)["modules"][
            "omnivia_memory.app_manifest.validation"
        ]["defines"]["validate_app_manifest"]
    )
    assert (
        _expected_facade_descriptor(
            "omnivia_memory.app_manifest.validation",
            "another_symbol",
            exact_old,
        )
        is exact_old
    )


def test_normalize_expected_for_facade_routes_does_not_mutate_its_argument() -> None:
    expected = load_json(PUBLIC_EXPORTS_PATH)
    before = copy.deepcopy(expected)

    _normalize_expected_for_facade_routes(expected)

    assert expected == before


def test_normalize_expected_for_facade_routes_empties_defines_for_routed_leaves() -> None:
    expected = load_json(PUBLIC_EXPORTS_PATH)

    normalized = _normalize_expected_for_facade_routes(expected)

    for legacy_module in FACADE_ROUTES:
        assert normalized["modules"][legacy_module]["defines"] == {}


def test_normalize_expected_for_facade_routes_moves_only_routed_root_bindings() -> None:
    expected = load_json(PUBLIC_EXPORTS_PATH)

    normalized = _normalize_expected_for_facade_routes(expected)

    moved = {
        name: binding["defined_in"]
        for name, binding in normalized["root"]["bindings"].items()
        if binding["defined_in"] != expected["root"]["bindings"][name]["defined_in"]
    }
    # ``ProvenanceRequirement`` and ``ValidationResult`` are routed by more than
    # one leaf, but each moves for exactly the one route whose legacy module the
    # root actually bound it from: the Component Contract for the former, the
    # shared primitive for the latter. The App Manifest's and App Shell bridge's
    # same-named classes are leaf-local and must move no root binding at all.
    assert moved == {
        "AgentAction": "omnivia_core.component_contract.models",
        "AgentBackedComponentContract": "omnivia_core.component_contract.models",
        "AgentBehavior": "omnivia_core.component_contract.models",
        "AgentRunRecord": "omnivia_core.component_contract.models",
        "AgentRunStatus": "omnivia_core.component_contract.models",
        "AppManifest": "omnivia_core.app_manifest.models",
        "AppManifestValidationError": "omnivia_core.app_manifest.validation",
        "AppState": "omnivia_core.app_manifest.models",
        "ApprovalPolicy": "omnivia_core.component_contract.models",
        "AuditRequirement": "omnivia_core.component_contract.models",
        "ComponentAIMode": "omnivia_core.component_contract.models",
        "ComponentConnectorScope": "omnivia_core.component_contract.models",
        "ComponentContract": "omnivia_core.component_contract.models",
        "ComponentContractValidationError": "omnivia_core.component_contract.validation",
        "ComponentDataSource": "omnivia_core.component_contract.models",
        "ComponentFamily": "omnivia_core.component_contract.models",
        "ComponentGraphScope": "omnivia_core.component_contract.models",
        "ComponentInput": "omnivia_core.component_contract.models",
        "ComponentOutput": "omnivia_core.component_contract.models",
        "ComponentOutputType": "omnivia_core.component_contract.models",
        "ComponentPermission": "omnivia_core.component_contract.models",
        "ComponentRunMode": "omnivia_core.component_contract.models",
        "ComponentSafetyLevel": "omnivia_core.component_contract.models",
        "DataSource": "omnivia_core.app_manifest.models",
        "MemoryCreate": "omnivia_core.memory.models",
        "MemoryUpdate": "omnivia_core.memory.models",
        "PermissionPolicy": "omnivia_core.component_contract.models",
        "ProvenanceBehavior": "omnivia_core.component_contract.models",
        "ProvenanceRequirement": "omnivia_core.component_contract.models",
        "Source": "omnivia_core.provenance.models",
        "SourceType": "omnivia_core.provenance.models",
        "ValidationResult": "omnivia_core._shared.validation",
        "validate_agent_run_record": "omnivia_core.component_contract.validation",
        "validate_app_manifest": "omnivia_core.app_manifest.validation",
        "validate_component_contract": "omnivia_core.component_contract.validation",
    }


def test_verify_public_export_inventory_fails_closed_on_unverified_route(monkeypatch) -> None:
    """A route that fails verification (here: a symbol that looks locally redefined)
    must refuse to normalize at all, rather than silently passing the diff."""
    real_build = build_public_export_inventory

    def _tampered():
        inventory = copy.deepcopy(real_build())
        inventory["modules"]["omnivia_memory.provenance.models"]["defines"]["Source"] = {
            "kind": "class",
            "bases": ["builtins.object"],
            "methods": [],
        }
        return inventory

    monkeypatch.setattr("baseline.inventory.build_public_export_inventory", _tampered)

    problems = verify_public_export_inventory()

    assert any("still locally defined" in problem for problem in problems)


def test_verify_public_export_inventory_fails_closed_on_unrelated_drift(monkeypatch) -> None:
    """Normalizing the sanctioned facade deltas must not swallow an unrelated difference
    elsewhere in the inventory."""
    real_build = build_public_export_inventory

    def _tampered():
        inventory = copy.deepcopy(real_build())
        inventory["root"]["bindings"]["KnowledgeSpace"]["defined_in"] = "omnivia.contracts.models"
        return inventory

    monkeypatch.setattr("baseline.inventory.build_public_export_inventory", _tampered)

    problems = verify_public_export_inventory()

    assert any("KnowledgeSpace" in problem for problem in problems)
