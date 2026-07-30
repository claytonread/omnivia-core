"""Tests for the public Python export inventory and its drift check."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from baseline import CORE_PACKAGE, REPO_ROOT
from baseline.determinism import diff_json, load_json
from baseline.inventory import (
    FACADE_ROOT_BINDING_OWNER_MOVES,
    FACADE_ROUTES,
    PUBLIC_EXPORTS_PATH,
    _expected_facade_descriptor,
    _facade_root_binding_problems,
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


def test_module_manifest_descriptor_rewrite_is_exact_and_symbol_specific() -> None:
    """The second sanctioned rewrite gets its own direct regression.

    ``validate_module_manifest``'s frozen signature annotates a resolved class
    from its own domain's models leaf, so the rendered return annotation named
    ``omnivia_memory.module_manifest.models.ModuleManifest`` and now names the
    canonical owner. That one exact substitution is all the rewrite may do: it
    must apply only to the exact frozen descriptor, under the exact route *and*
    the exact symbol, and it must leave package-looking text that is contract
    data rather than ownership metadata completely alone.
    """
    actual = build_public_export_inventory()
    frozen = copy.deepcopy(load_json(PUBLIC_EXPORTS_PATH))
    descriptor = frozen["modules"]["omnivia_memory.module_manifest.validation"][
        "defines"
    ]["validate_module_manifest"]

    # The exact historical descriptor gets the one sanctioned owner rename, so
    # the converted leaf reports no contract drift.
    assert _facade_route_problems(actual, frozen=frozen) == []

    exact_old = copy.deepcopy(descriptor)
    assert (
        "omnivia_memory.module_manifest.models.ModuleManifest"
        in exact_old["signature"]
    )

    # A near miss: package-looking text in a default is contract data. A broad
    # recursive replacement would rewrite it too and hide this mutation.
    descriptor["signature"] = (
        "(data: Dict[str, Any] = "
        "'omnivia_memory.module_manifest.models.ModuleManifest') -> "
        "omnivia_memory.module_manifest.models.ModuleManifest"
    )
    problems = _facade_route_problems(actual, frozen=frozen)
    assert any(
        "contract drifted" in problem
        and "module_manifest.validation.validate_module_manifest" in problem
        for problem in problems
    ), problems

    # The helper leaves that near miss untouched -- the same object back, with
    # both of its package-looking substrings intact.
    near_miss = copy.deepcopy(descriptor)
    assert (
        _expected_facade_descriptor(
            "omnivia_memory.module_manifest.validation",
            "validate_module_manifest",
            near_miss,
        )
        is near_miss
    )
    assert near_miss["signature"].count("omnivia_memory") == 2

    # Right route, wrong symbol: no rewrite.
    assert (
        _expected_facade_descriptor(
            "omnivia_memory.module_manifest.validation",
            "ModuleManifestValidationError",
            exact_old,
        )
        is exact_old
    )

    # Right symbol, wrong route: no rewrite either. The two frozen rewrites are
    # keyed by the pair, so neither route may borrow the other's substitution.
    assert (
        _expected_facade_descriptor(
            "omnivia_memory.module_manifest.models",
            "validate_module_manifest",
            exact_old,
        )
        is exact_old
    )

    # The exact route+symbol on the exact frozen descriptor does rewrite, and
    # only the return annotation's owner changes.
    rewritten = _expected_facade_descriptor(
        "omnivia_memory.module_manifest.validation",
        "validate_module_manifest",
        exact_old,
    )
    assert rewritten is not exact_old
    assert rewritten == {
        **exact_old,
        "signature": exact_old["signature"].replace(
            "omnivia_memory.module_manifest.models.ModuleManifest",
            "omnivia_core.module_manifest.models.ModuleManifest",
        ),
    }
    assert "omnivia_memory" not in rewritten["signature"]


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
    #
    # ``RUN_LEDGER_CONTRACT_VERSION`` is the one binding that does not land on
    # its own route's canonical module: it is a ``ContractVersion`` *instance*,
    # so ``defined_in`` names the module owning its type. Its move is declared
    # exactly in ``FACADE_ROOT_BINDING_OWNER_MOVES`` and proved by
    # ``_facade_root_binding_problems`` before it may be applied.
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
        "Entrypoint": "omnivia_core.module_manifest.models",
        "EvidenceFileRef": "omnivia_core.run_ledger.models",
        "Integrity": "omnivia_core.module_manifest.models",
        "MemoryCreate": "omnivia_core.memory.models",
        "MemoryUpdate": "omnivia_core.memory.models",
        "ModuleKind": "omnivia_core.module_manifest.models",
        "ModuleManifest": "omnivia_core.module_manifest.models",
        "ModuleManifestValidationError": "omnivia_core.module_manifest.validation",
        "Permission": "omnivia_core.module_manifest.models",
        "PermissionPolicy": "omnivia_core.component_contract.models",
        "PublishedTarget": "omnivia_core.module_manifest.models",
        "ProvenanceBehavior": "omnivia_core.component_contract.models",
        "ProvenanceRequirement": "omnivia_core.component_contract.models",
        "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "RunLedgerEntry": "omnivia_core.run_ledger.models",
        "RunLedgerProvenance": "omnivia_core.run_ledger.models",
        "RunLedgerStatus": "omnivia_core.run_ledger.models",
        "Source": "omnivia_core.provenance.models",
        "SourceType": "omnivia_core.provenance.models",
        "ValidationResult": "omnivia_core._shared.validation",
        "validate_agent_run_record": "omnivia_core.component_contract.validation",
        "validate_app_manifest": "omnivia_core.app_manifest.validation",
        "validate_component_contract": "omnivia_core.component_contract.validation",
        "validate_evidence_file_ref": "omnivia_core.run_ledger.validation",
        "validate_module_manifest": "omnivia_core.module_manifest.validation",
        "validate_run_ledger_entry": "omnivia_core.run_ledger.validation",
        "validate_run_ledger_provenance": "omnivia_core.run_ledger.validation",
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


# ---------------------------------------------------------------------------
# FACADE_ROOT_BINDING_OWNER_MOVES: the exact root-binding owner-move mechanism.
# ---------------------------------------------------------------------------

#: The single declared move, restated here rather than read off the declaration,
#: so the tests below fail if the declaration itself is repointed.
_RUN_LEDGER_VERSION_MOVE_KEY = (
    "RUN_LEDGER_CONTRACT_VERSION",
    "omnivia_memory.run_ledger.models",
)
_RUN_LEDGER_VERSION_FROZEN_OWNER = "omnivia_memory.knowledge.models"
_RUN_LEDGER_VERSION_CANONICAL_OWNER = "omnivia_core.knowledge.models"


def test_root_binding_owner_moves_declaration_is_exactly_the_one_instance_route() -> None:
    """Only one routed symbol is an *instance* whose type another leaf owns, so
    only one root binding may need a declared owner move. A second entry
    appearing here without its own coverage would be normalized away silently."""
    assert FACADE_ROOT_BINDING_OWNER_MOVES == {
        _RUN_LEDGER_VERSION_MOVE_KEY: (
            _RUN_LEDGER_VERSION_FROZEN_OWNER,
            _RUN_LEDGER_VERSION_CANONICAL_OWNER,
        )
    }
    # The declared frozen owner is a module that is *not* itself converted and
    # appears in no route: that is precisely why the move has to be declared
    # rather than derived from FACADE_ROUTES.
    assert _RUN_LEDGER_VERSION_FROZEN_OWNER not in FACADE_ROUTES
    name, legacy_module = _RUN_LEDGER_VERSION_MOVE_KEY
    assert FACADE_ROUTES[legacy_module][name] == "omnivia_core.run_ledger.models"


def test_facade_root_binding_problems_accepts_the_declared_move() -> None:
    assert _facade_root_binding_problems() == []


def test_facade_root_binding_problems_rejects_a_name_that_route_does_not_carry() -> None:
    """A move may only be declared for a symbol the named legacy module really
    routes, so a renamed or dropped route cannot leave a live rewrite behind."""
    problems = _facade_root_binding_problems(
        moves={
            ("RUN_LEDGER_CONTRACT_VERSION", "omnivia_memory.memory.models"): (
                _RUN_LEDGER_VERSION_FROZEN_OWNER,
                _RUN_LEDGER_VERSION_CANONICAL_OWNER,
            )
        }
    )
    assert any(
        "is not a declared route of that module" in problem for problem in problems
    ), problems


def test_facade_root_binding_problems_rejects_a_misdeclared_frozen_owner() -> None:
    """The declared frozen owner must match the baseline exactly. Declaring the
    *post*-move owner would make the entry a no-op rewrite that silently
    tolerated whatever the baseline actually recorded."""
    problems = _facade_root_binding_problems(
        moves={
            _RUN_LEDGER_VERSION_MOVE_KEY: (
                _RUN_LEDGER_VERSION_CANONICAL_OWNER,
                _RUN_LEDGER_VERSION_CANONICAL_OWNER,
            )
        }
    )
    assert any("the frozen baseline records owner" in p for p in problems), problems


def test_facade_root_binding_problems_rejects_a_wrong_destination() -> None:
    """The declared new owner must be where the object's owner really moved. The
    routed leaf is the plausible wrong answer here -- it owns the *object* but
    not its type -- and it must still be rejected."""
    problems = _facade_root_binding_problems(
        moves={
            _RUN_LEDGER_VERSION_MOVE_KEY: (
                _RUN_LEDGER_VERSION_FROZEN_OWNER,
                "omnivia_core.run_ledger.models",
            )
        }
    )
    assert any(
        "owner moved to 'omnivia_core.knowledge.models', not the declared "
        "'omnivia_core.run_ledger.models'" in problem
        for problem in problems
    ), problems


def test_facade_root_binding_problems_rejects_a_missing_root_binding() -> None:
    """A declared move for a name the baseline never bound at the root is stale:
    there is nothing to normalize, and pretending otherwise would let a real
    root binding disappear unnoticed."""
    frozen = copy.deepcopy(load_json(PUBLIC_EXPORTS_PATH))
    del frozen["root"]["bindings"]["RUN_LEDGER_CONTRACT_VERSION"]

    problems = _facade_root_binding_problems(frozen=frozen)

    assert any("has no such root binding" in problem for problem in problems), problems


def test_facade_root_binding_problems_rejects_a_non_identical_routed_object() -> None:
    """The root's object must be the exact object bound at the route's canonical
    module. ``ValidationResult`` is the sharpest case: the App Manifest contract
    really does route a ``ValidationResult``, and the root really does bind one,
    but they are different domains' classes -- so the owner check alone would
    pass while the binding silently changed contract.
    """
    problems = _facade_root_binding_problems(
        moves={
            ("ValidationResult", "omnivia_memory.app_manifest.models"): (
                "omnivia_memory._shared.validation",
                "omnivia_core.app_manifest.models",
            )
        }
    )
    assert any(
        "is not the exact object bound at omnivia_core.app_manifest.models."
        "ValidationResult" in problem
        for problem in problems
    ), problems


def test_verify_public_export_inventory_fails_closed_on_an_unverified_owner_move(
    monkeypatch,
) -> None:
    """The owner-move check runs *before* normalization and refuses outright, so
    an unverified move can never be applied to the frozen baseline."""
    monkeypatch.setattr(
        "baseline.inventory._facade_root_binding_problems",
        lambda **_kwargs: ["synthetic owner-move defect"],
    )

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - asserted unreachable
        raise AssertionError("normalization ran despite an unverified owner move")

    monkeypatch.setattr(
        "baseline.inventory._normalize_expected_for_facade_routes", _must_not_run
    )

    problems = verify_public_export_inventory()

    assert any(
        "root-binding owner-move verification failed" in problem
        for problem in problems
    ), problems
    assert any("synthetic owner-move defect" in problem for problem in problems)


def test_normalize_applies_the_owner_move_by_whole_string_equality_only() -> None:
    """The move is a whole-value equality test and a whole-value substitution --
    never a package-prefix rewrite. Bindings whose ``defined_in`` merely *contains*
    the frozen owner (as a prefix or a suffix), and package-looking text elsewhere
    in the very binding that does move, must come through untouched.
    """
    expected = {
        "root": {
            "bindings": {
                "RUN_LEDGER_CONTRACT_VERSION": {
                    "defined_in": _RUN_LEDGER_VERSION_FROZEN_OWNER,
                    # Contract data that happens to name the frozen owner. A
                    # recursive or substring rewrite would corrupt it.
                    "exported_by": [_RUN_LEDGER_VERSION_FROZEN_OWNER],
                },
                "SuffixDecoy": {
                    "defined_in": f"{_RUN_LEDGER_VERSION_FROZEN_OWNER}.inner"
                },
                "PrefixDecoy": {
                    "defined_in": f"x{_RUN_LEDGER_VERSION_FROZEN_OWNER}"
                },
                "SiblingDecoy": {"defined_in": "omnivia_memory.knowledge.validation"},
            }
        },
        "modules": {},
    }

    normalized = _normalize_expected_for_facade_routes(expected)
    bindings = normalized["root"]["bindings"]

    assert bindings["RUN_LEDGER_CONTRACT_VERSION"]["defined_in"] == (
        _RUN_LEDGER_VERSION_CANONICAL_OWNER
    )
    assert bindings["RUN_LEDGER_CONTRACT_VERSION"]["exported_by"] == [
        _RUN_LEDGER_VERSION_FROZEN_OWNER
    ]
    assert bindings["SuffixDecoy"]["defined_in"] == (
        f"{_RUN_LEDGER_VERSION_FROZEN_OWNER}.inner"
    )
    assert bindings["PrefixDecoy"]["defined_in"] == (
        f"x{_RUN_LEDGER_VERSION_FROZEN_OWNER}"
    )
    assert bindings["SiblingDecoy"]["defined_in"] == "omnivia_memory.knowledge.validation"


def test_normalize_leaves_the_unconverted_frozen_owner_leaf_intact() -> None:
    """``knowledge.models`` supplies the moved binding's new owner but is not
    itself converted: its own frozen module entry -- ``defines`` included -- must
    survive normalization completely unchanged, and no other knowledge-domain
    root binding may move with it."""
    expected = load_json(PUBLIC_EXPORTS_PATH)

    normalized = _normalize_expected_for_facade_routes(expected)

    for leaf in ("omnivia_memory.knowledge.models", "omnivia_memory.knowledge.validation"):
        assert normalized["modules"][leaf] == expected["modules"][leaf]

    frozen_bindings = expected["root"]["bindings"]
    for name, binding in normalized["root"]["bindings"].items():
        if name == "RUN_LEDGER_CONTRACT_VERSION":
            continue
        if frozen_bindings[name]["defined_in"] == _RUN_LEDGER_VERSION_FROZEN_OWNER:
            assert binding["defined_in"] == _RUN_LEDGER_VERSION_FROZEN_OWNER, (
                f"{name} moved owner without a declared entry"
            )
