"""Tests for the public Python export inventory and its drift check."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

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


def test_knowledge_route_repointed_at_a_reexporting_sibling_still_fails_closed() -> None:
    """The sharpest case this batch introduces: a route repointed at a module
    that re-exports the *identical* object.

    ``omnivia_core.run_ledger.models`` imports the knowledge domain's
    ``ContractVersion`` to build its own version constant, and
    ``omnivia_core.knowledge.validation`` imports ``normalize_label`` from its
    sibling ``normalize``. Both hold the exact same object as the real owner, so
    ``_facade_route_problems``'s identity check cannot discriminate -- and it
    reports nothing, which this test asserts rather than papers over.

    What still fails closed is the normalization it feeds: the root binding would
    be rewritten to the wrong owner, and the diff against the live surface names
    that binding. Without the diff layer, a route could silently relabel a public
    symbol's owning module while every identity assertion passed.
    """
    actual = build_public_export_inventory()
    expected = load_json(PUBLIC_EXPORTS_PATH)
    for legacy_module, symbol, reexporting_owner, real_owner in (
        (
            "omnivia_memory.knowledge.models",
            "ContractVersion",
            "omnivia_core.run_ledger.models",
            "omnivia_core.knowledge.models",
        ),
        (
            "omnivia_memory.knowledge.normalize",
            "normalize_label",
            "omnivia_core.knowledge.validation",
            "omnivia_core.knowledge.normalize",
        ),
    ):
        bad_routes = copy.deepcopy(FACADE_ROUTES)
        bad_routes[legacy_module][symbol] = reexporting_owner

        # Identity holds: the re-exporting module really does bind the same object.
        assert _facade_route_problems(actual, routes=bad_routes) == []

        normalized = _normalize_expected_for_facade_routes(
            expected, routes=bad_routes
        )
        differences = diff_json(normalized, actual)
        assert any(
            f"root.bindings.{symbol}.defined_in" in difference
            and reexporting_owner in difference
            and real_owner in difference
            for difference in differences
        ), (symbol, differences)


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
    # ``ProvenanceRequirement``, ``ValidationResult`` and ``LifecycleState`` are
    # routed by more than one leaf, but each moves for exactly the one route whose
    # legacy module the root actually bound it from: the Component Contract for the
    # first, the shared primitive for the second, the control plane for the third.
    # The App Manifest's and App Shell bridge's same-named result classes, the
    # lifecycle domain's own ``LifecycleState``, and -- now that the knowledge
    # leaves are routed too -- the knowledge validation leaf's imported
    # ``ValidationResult`` are leaf-local here and must move no root binding at
    # all. The knowledge leaf never owned a class of that name, so it routes none.
    #
    # ``RUN_LEDGER_CONTRACT_VERSION`` and ``CONTROL_PLANE_CONTRACT_VERSION`` are the
    # two bindings that do not land on their own route's canonical module: each is a
    # ``ContractVersion`` *instance*, so ``defined_in`` names the module owning its
    # type. Both moves are declared exactly in
    # ``FACADE_ROOT_BINDING_OWNER_MOVES`` and proved by
    # ``_facade_root_binding_problems`` before either may be applied -- and they
    # land on the same new owner without interfering, which is what makes the
    # mechanism's independence visible here. They land on
    # ``omnivia_core.knowledge.models`` -- the same destination the knowledge
    # models leaf's own 26 routes move their bindings to -- yet by a different
    # mechanism: neither constant is one of that leaf's routed symbols.
    #
    # The memory graph moves exactly five bindings, all by the ordinary route
    # loop: three classes its models leaf owns
    # (``EvidenceGraphResponse``, ``GraphPreviewResponse``, ``RetrievalTrace``)
    # and the ``TypedDict``/builder pair its fixtures leaf owns. It needs no
    # declared owner move: unlike the two version constants, every one of the five
    # is a class or a function whose ``defined_in`` already named the routed leaf.
    # Its ``SourceRef`` route moves nothing at the root -- that binding is the
    # knowledge domain's same-named class and stays there.
    assert moved == {
        "Agent": "omnivia_core.control_plane.models",
        "AgentAction": "omnivia_core.component_contract.models",
        "AgentBackedComponentContract": "omnivia_core.component_contract.models",
        "AgentBehavior": "omnivia_core.component_contract.models",
        "AgentGraphContext": "omnivia_core.knowledge.models",
        "AgentRunRecord": "omnivia_core.component_contract.models",
        "AgentRunStatus": "omnivia_core.component_contract.models",
        "AppManifest": "omnivia_core.app_manifest.models",
        "AppManifestValidationError": "omnivia_core.app_manifest.validation",
        "AppState": "omnivia_core.app_manifest.models",
        "Approval": "omnivia_core.control_plane.models",
        "ApprovalPolicy": "omnivia_core.component_contract.models",
        "AuditEvent": "omnivia_core.control_plane.models",
        "AuditRequirement": "omnivia_core.component_contract.models",
        "Automation": "omnivia_core.control_plane.models",
        "CONTROL_PLANE_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "Capability": "omnivia_core.control_plane.models",
        "CapabilityType": "omnivia_core.control_plane.models",
        "CatalogueArtifactVerification": "omnivia_core.control_plane.imports",
        "ComponentAIMode": "omnivia_core.component_contract.models",
        "ComponentConnectorScope": "omnivia_core.component_contract.models",
        "ComponentContract": "omnivia_core.component_contract.models",
        "ComponentContractValidationError": (
            "omnivia_core.component_contract.validation"
        ),
        "ComponentDataSource": "omnivia_core.component_contract.models",
        "ComponentFamily": "omnivia_core.component_contract.models",
        "ComponentGraphScope": "omnivia_core.component_contract.models",
        "ComponentInput": "omnivia_core.component_contract.models",
        "ComponentOutput": "omnivia_core.component_contract.models",
        "ComponentOutputType": "omnivia_core.component_contract.models",
        "ComponentPermission": "omnivia_core.component_contract.models",
        "ComponentRunMode": "omnivia_core.component_contract.models",
        "ComponentSafetyLevel": "omnivia_core.component_contract.models",
        "Connection": "omnivia_core.control_plane.models",
        "ConnectionKind": "omnivia_core.control_plane.models",
        "ConsultantAccessGrant": "omnivia_core.control_plane.models",
        "ConsultantGrantStatus": "omnivia_core.control_plane.models",
        "ContractVersion": "omnivia_core.knowledge.models",
        "ControlPlaneManifest": "omnivia_core.control_plane.models",
        "ControlPlaneRunStatus": "omnivia_core.control_plane.models",
        "ControlPlaneValidationError": "omnivia_core.control_plane.validation",
        "DataSource": "omnivia_core.app_manifest.models",
        "EXTENSION_MANIFEST_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "Entrypoint": "omnivia_core.module_manifest.models",
        "EvidenceFileRef": "omnivia_core.run_ledger.models",
        "EvidenceGraphResponse": "omnivia_core.memory_graph.models",
        "ExecutionMode": "omnivia_core.control_plane.models",
        "ExecutionResult": "omnivia_core.control_plane.models",
        "GRAPH_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "GraphConfidence": "omnivia_core.knowledge.models",
        "GraphEdge": "omnivia_core.knowledge.models",
        "GraphEvidenceStrength": "omnivia_core.knowledge.models",
        "GraphFragment": "omnivia_core.knowledge.models",
        "GraphNode": "omnivia_core.knowledge.models",
        "GraphOrigin": "omnivia_core.knowledge.models",
        "GraphPreviewResponse": "omnivia_core.memory_graph.models",
        "GraphReviewStatus": "omnivia_core.knowledge.models",
        "GraphSensitivity": "omnivia_core.knowledge.models",
        "GraphSourceType": "omnivia_core.knowledge.models",
        "GraphVisibility": "omnivia_core.knowledge.models",
        "ImportRecord": "omnivia_core.control_plane.models",
        "ImportSourceChange": "omnivia_core.control_plane.imports",
        "ImportSourceProtocol": "omnivia_core.control_plane.models",
        "ImportSpecValidation": "omnivia_core.control_plane.imports",
        "ImportedCandidateSet": "omnivia_core.control_plane.imports",
        "Integrity": "omnivia_core.module_manifest.models",
        "KNOWLEDGE_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "KnowledgeClaim": "omnivia_core.knowledge.models",
        "KnowledgeCollection": "omnivia_core.knowledge.models",
        "KnowledgeExtensionManifest": "omnivia_core.knowledge.models",
        "KnowledgeLink": "omnivia_core.knowledge.models",
        "KnowledgeObject": "omnivia_core.knowledge.models",
        "KnowledgeSource": "omnivia_core.knowledge.models",
        "KnowledgeSpace": "omnivia_core.knowledge.models",
        "LifecycleState": "omnivia_core.control_plane.models",
        "LocalApprovalNotification": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationChannel": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationEvent": "omnivia_core.control_plane.models",
        "LocalApprovalNotificationStatus": "omnivia_core.control_plane.models",
        "LocalModelInvocationRecord": "omnivia_core.control_plane.models",
        "LocalObservabilityLogRecord": "omnivia_core.control_plane.models",
        "LocalUsageLedgerEntry": "omnivia_core.control_plane.models",
        "MemoryCreate": "omnivia_core.memory.models",
        "MemoryGraphFixture": "omnivia_core.memory_graph.fixtures",
        "MemoryUpdate": "omnivia_core.memory.models",
        "ModuleKind": "omnivia_core.module_manifest.models",
        "ModuleManifest": "omnivia_core.module_manifest.models",
        "ModuleManifestValidationError": "omnivia_core.module_manifest.validation",
        "Permission": "omnivia_core.module_manifest.models",
        "PermissionPolicy": "omnivia_core.component_contract.models",
        "Policy": "omnivia_core.control_plane.models",
        "PolicyAttributeCondition": "omnivia_core.control_plane.models",
        "PolicyAttributeExpression": "omnivia_core.control_plane.models",
        "PolicyDecision": "omnivia_core.control_plane.models",
        "PolicyDecisionReason": "omnivia_core.control_plane.models",
        "PolicyDecisionRecord": "omnivia_core.control_plane.models",
        "PolicyRulePack": "omnivia_core.control_plane.models",
        "PolicyTemplate": "omnivia_core.control_plane.models",
        "ProvenanceBehavior": "omnivia_core.component_contract.models",
        "ProvenanceRequirement": "omnivia_core.component_contract.models",
        "PublishedTarget": "omnivia_core.module_manifest.models",
        "RUN_LEDGER_CONTRACT_VERSION": "omnivia_core.knowledge.models",
        "RetrievalTrace": "omnivia_core.memory_graph.models",
        "RunLedgerEntry": "omnivia_core.run_ledger.models",
        "RunLedgerProvenance": "omnivia_core.run_ledger.models",
        "RunLedgerStatus": "omnivia_core.run_ledger.models",
        "RunMode": "omnivia_core.control_plane.models",
        "RunObservabilityMetrics": "omnivia_core.control_plane.models",
        "RunRecord": "omnivia_core.control_plane.models",
        "RunStepRecord": "omnivia_core.control_plane.models",
        "RunStepStatus": "omnivia_core.control_plane.models",
        "RunStepType": "omnivia_core.control_plane.models",
        "SecretMetadata": "omnivia_core.control_plane.models",
        "SecretReference": "omnivia_core.control_plane.models",
        "SecretResolutionResult": "omnivia_core.control_plane.models",
        "SecretStorageScope": "omnivia_core.control_plane.models",
        "SideEffect": "omnivia_core.control_plane.models",
        "Source": "omnivia_core.provenance.models",
        "SourceRef": "omnivia_core.knowledge.models",
        "SourceType": "omnivia_core.provenance.models",
        "SyncConflictStrategy": "omnivia_core.control_plane.models",
        "SyncDirection": "omnivia_core.control_plane.models",
        "SyncRule": "omnivia_core.control_plane.models",
        "TenantIsolationRule": "omnivia_core.control_plane.models",
        "Trigger": "omnivia_core.control_plane.models",
        "TriggerEventEnvelope": "omnivia_core.control_plane.models",
        "TriggerIngestionResult": "omnivia_core.control_plane.models",
        "TriggerKind": "omnivia_core.control_plane.models",
        "ValidationResult": "omnivia_core._shared.validation",
        "WorkspaceRef": "omnivia_core.control_plane.models",
        "build_memory_graph_fixture": "omnivia_core.memory_graph.fixtures",
        "check_contract_version_compatibility": "omnivia_core.knowledge.validation",
        "compile_policy_expression": "omnivia_core.control_plane.validation",
        "detect_import_source_change": "omnivia_core.control_plane.imports",
        "import_asyncapi_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_candidates": "omnivia_core.control_plane.imports",
        "import_catalogue_generated_candidates": "omnivia_core.control_plane.imports",
        "import_mcp_candidates": "omnivia_core.control_plane.imports",
        "import_openapi_candidates": "omnivia_core.control_plane.imports",
        "manifest_from_dict": "omnivia_core.control_plane.validation",
        "normalize_graph_edge_id": "omnivia_core.knowledge.normalize",
        "normalize_graph_node_id": "omnivia_core.knowledge.normalize",
        "normalize_graph_node_kind": "omnivia_core.knowledge.normalize",
        "normalize_graph_relation": "omnivia_core.knowledge.normalize",
        "normalize_identifier": "omnivia_core.knowledge.normalize",
        "normalize_label": "omnivia_core.knowledge.normalize",
        "normalize_object_id": "omnivia_core.knowledge.normalize",
        "normalize_object_kind": "omnivia_core.knowledge.normalize",
        "normalize_source_path": "omnivia_core.knowledge.normalize",
        "normalize_space_id": "omnivia_core.knowledge.normalize",
        "normalize_tags": "omnivia_core.knowledge.normalize",
        "summarize_confidence": "omnivia_core.knowledge.validation",
        "summarize_review_status": "omnivia_core.knowledge.validation",
        "summarize_sensitivity": "omnivia_core.knowledge.validation",
        "validate_agent_graph_context": "omnivia_core.knowledge.validation",
        "validate_agent_run_record": "omnivia_core.component_contract.validation",
        "validate_app_manifest": "omnivia_core.app_manifest.validation",
        "validate_asyncapi_import_spec": "omnivia_core.control_plane.imports",
        "validate_component_contract": "omnivia_core.component_contract.validation",
        "validate_control_plane_manifest": "omnivia_core.control_plane.validation",
        "validate_evidence_file_ref": "omnivia_core.run_ledger.validation",
        "validate_graph_edge": "omnivia_core.knowledge.validation",
        "validate_graph_fragment": "omnivia_core.knowledge.validation",
        "validate_graph_node": "omnivia_core.knowledge.validation",
        "validate_knowledge_claim": "omnivia_core.knowledge.validation",
        "validate_knowledge_collection": "omnivia_core.knowledge.validation",
        "validate_knowledge_extension_manifest": "omnivia_core.knowledge.validation",
        "validate_knowledge_link": "omnivia_core.knowledge.validation",
        "validate_knowledge_object": "omnivia_core.knowledge.validation",
        "validate_knowledge_source": "omnivia_core.knowledge.validation",
        "validate_knowledge_space": "omnivia_core.knowledge.validation",
        "validate_mcp_import_spec": "omnivia_core.control_plane.imports",
        "validate_module_manifest": "omnivia_core.module_manifest.validation",
        "validate_openapi_import_spec": "omnivia_core.control_plane.imports",
        "validate_run_ledger_entry": "omnivia_core.run_ledger.validation",
        "validate_run_ledger_provenance": "omnivia_core.run_ledger.validation",
        "validate_source_ref": "omnivia_core.knowledge.validation",
        "verify_catalogue_artifacts": "omnivia_core.control_plane.imports",
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

#: The declared moves, restated here rather than read off the declaration, so the
#: tests below fail if the declaration itself is repointed. Both routed instances
#: are ``ContractVersion`` values, so both share one frozen and one canonical
#: owner -- which is exactly why their independence has to be proved rather than
#: assumed.
_INSTANCE_FROZEN_OWNER = "omnivia_memory.knowledge.models"
_INSTANCE_CANONICAL_OWNER = "omnivia_core.knowledge.models"
_RUN_LEDGER_VERSION_MOVE_KEY = (
    "RUN_LEDGER_CONTRACT_VERSION",
    "omnivia_memory.run_ledger.models",
)
_CONTROL_PLANE_VERSION_MOVE_KEY = (
    "CONTROL_PLANE_CONTRACT_VERSION",
    "omnivia_memory.control_plane.models",
)
#: ``(move key, the canonical module its own route points at)``. The second
#: element is the *plausible wrong destination* for that move: the leaf owns the
#: object but not its type, so declaring it would look right and be wrong.
_DECLARED_MOVES: tuple[tuple[tuple[str, str], str], ...] = (
    (_RUN_LEDGER_VERSION_MOVE_KEY, "omnivia_core.run_ledger.models"),
    (_CONTROL_PLANE_VERSION_MOVE_KEY, "omnivia_core.control_plane.models"),
)
_MOVE_IDS = [name for (name, _legacy), _routed in _DECLARED_MOVES]


def test_root_binding_owner_moves_declaration_is_exactly_the_instance_routes() -> None:
    """Exactly two routed symbols are *instances* whose type another leaf owns, so
    exactly two root bindings may need a declared owner move. A third entry
    appearing here without its own coverage would be normalized away silently."""
    assert FACADE_ROOT_BINDING_OWNER_MOVES == {
        _RUN_LEDGER_VERSION_MOVE_KEY: (
            _INSTANCE_FROZEN_OWNER,
            _INSTANCE_CANONICAL_OWNER,
        ),
        _CONTROL_PLANE_VERSION_MOVE_KEY: (
            _INSTANCE_FROZEN_OWNER,
            _INSTANCE_CANONICAL_OWNER,
        ),
    }
    # The declared frozen owner is itself a converted, routed leaf now -- but
    # neither moved constant is one of *its* routed symbols, so the ordinary
    # exact-route normalization still cannot reach either binding. That is
    # precisely why the moves have to be declared rather than derived from
    # FACADE_ROUTES.
    assert _INSTANCE_FROZEN_OWNER in FACADE_ROUTES
    for (name, legacy_module), routed_canonical in _DECLARED_MOVES:
        assert name not in FACADE_ROUTES[_INSTANCE_FROZEN_OWNER], (
            f"{name} became a route of {_INSTANCE_FROZEN_OWNER}; the declared "
            "owner move is now redundant and must be re-derived deliberately"
        )
        assert FACADE_ROUTES[legacy_module][name] == routed_canonical
    # The frozen owner's own routes all point at the same canonical module the
    # two declared moves name, so a reader cannot tell the two mechanisms apart
    # by destination alone -- only by which symbols each covers.
    assert set(FACADE_ROUTES[_INSTANCE_FROZEN_OWNER].values()) == {
        _INSTANCE_CANONICAL_OWNER
    }
    # Both moves are declared, and both land on the same new owner. Declaring one
    # must not be mistaken for covering the other.
    assert len({key for key, _routed in _DECLARED_MOVES}) == 2


def test_facade_root_binding_problems_accepts_the_declared_moves() -> None:
    assert _facade_root_binding_problems() == []


@pytest.mark.parametrize(
    "move_key", [key for key, _routed in _DECLARED_MOVES], ids=_MOVE_IDS
)
def test_facade_root_binding_problems_rejects_a_name_that_route_does_not_carry(
    move_key: tuple[str, str],
) -> None:
    """A move may only be declared for a symbol the named legacy module really
    routes, so a renamed or dropped route cannot leave a live rewrite behind."""
    name, _legacy_module = move_key
    problems = _facade_root_binding_problems(
        moves={
            (name, "omnivia_memory.memory.models"): (
                _INSTANCE_FROZEN_OWNER,
                _INSTANCE_CANONICAL_OWNER,
            )
        }
    )
    assert any(
        "is not a declared route of that module" in problem for problem in problems
    ), problems


@pytest.mark.parametrize(
    "move_key", [key for key, _routed in _DECLARED_MOVES], ids=_MOVE_IDS
)
def test_facade_root_binding_problems_rejects_a_misdeclared_frozen_owner(
    move_key: tuple[str, str],
) -> None:
    """The declared frozen owner must match the baseline exactly. Declaring the
    *post*-move owner would make the entry a no-op rewrite that silently
    tolerated whatever the baseline actually recorded."""
    problems = _facade_root_binding_problems(
        moves={move_key: (_INSTANCE_CANONICAL_OWNER, _INSTANCE_CANONICAL_OWNER)}
    )
    assert any("the frozen baseline records owner" in p for p in problems), problems


@pytest.mark.parametrize(
    ("move_key", "routed_canonical"), _DECLARED_MOVES, ids=_MOVE_IDS
)
def test_facade_root_binding_problems_rejects_a_wrong_destination(
    move_key: tuple[str, str],
    routed_canonical: str,
) -> None:
    """The declared new owner must be where the object's owner really moved. The
    routed leaf is the plausible wrong answer for each move -- it owns the
    *object* but not its type -- and it must still be rejected."""
    problems = _facade_root_binding_problems(
        moves={move_key: (_INSTANCE_FROZEN_OWNER, routed_canonical)}
    )
    assert any(
        f"owner moved to {_INSTANCE_CANONICAL_OWNER!r}, not the declared "
        f"{routed_canonical!r}" in problem
        for problem in problems
    ), problems


@pytest.mark.parametrize(
    "move_key", [key for key, _routed in _DECLARED_MOVES], ids=_MOVE_IDS
)
def test_facade_root_binding_problems_rejects_a_missing_root_binding(
    move_key: tuple[str, str],
) -> None:
    """A declared move for a name the baseline never bound at the root is stale:
    there is nothing to normalize, and pretending otherwise would let a real
    root binding disappear unnoticed."""
    name, _legacy_module = move_key
    frozen = copy.deepcopy(load_json(PUBLIC_EXPORTS_PATH))
    del frozen["root"]["bindings"][name]

    problems = _facade_root_binding_problems(frozen=frozen)

    assert any(
        "has no such root binding" in problem and name in problem
        for problem in problems
    ), problems


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


def _decoy_document() -> dict:
    """Both moved bindings side by side, plus every near-miss shape.

    The two entries share a frozen owner, so this is also where their
    coexistence is proved: each has to move on its own account, and neither may
    be reached by the other's substitution.
    """
    return {
        "root": {
            "bindings": {
                "RUN_LEDGER_CONTRACT_VERSION": {
                    "defined_in": _INSTANCE_FROZEN_OWNER,
                    # Contract data that happens to name the frozen owner. A
                    # recursive or substring rewrite would corrupt it.
                    "exported_by": [_INSTANCE_FROZEN_OWNER],
                },
                "CONTROL_PLANE_CONTRACT_VERSION": {
                    "defined_in": _INSTANCE_FROZEN_OWNER,
                    "exported_by": [_INSTANCE_FROZEN_OWNER],
                },
                "SuffixDecoy": {"defined_in": f"{_INSTANCE_FROZEN_OWNER}.inner"},
                "PrefixDecoy": {"defined_in": f"x{_INSTANCE_FROZEN_OWNER}"},
                "SiblingDecoy": {"defined_in": "omnivia_memory.knowledge.validation"},
                # A name neither move declares, sitting on the very owner both
                # moves rewrite. It must not be carried along.
                "UndeclaredSameOwner": {"defined_in": _INSTANCE_FROZEN_OWNER},
            }
        },
        "modules": {},
    }


def test_normalize_applies_the_owner_moves_by_whole_string_equality_only() -> None:
    """Each move is a whole-value equality test and a whole-value substitution --
    never a package-prefix rewrite. Bindings whose ``defined_in`` merely *contains*
    the frozen owner (as a prefix or a suffix), an undeclared binding that happens
    to sit on the same owner, and package-looking text elsewhere in the very
    bindings that do move, must all come through untouched.
    """
    normalized = _normalize_expected_for_facade_routes(_decoy_document())
    bindings = normalized["root"]["bindings"]

    for name in ("RUN_LEDGER_CONTRACT_VERSION", "CONTROL_PLANE_CONTRACT_VERSION"):
        assert bindings[name]["defined_in"] == _INSTANCE_CANONICAL_OWNER
        assert bindings[name]["exported_by"] == [_INSTANCE_FROZEN_OWNER]

    assert bindings["SuffixDecoy"]["defined_in"] == f"{_INSTANCE_FROZEN_OWNER}.inner"
    assert bindings["PrefixDecoy"]["defined_in"] == f"x{_INSTANCE_FROZEN_OWNER}"
    assert bindings["SiblingDecoy"]["defined_in"] == "omnivia_memory.knowledge.validation"
    assert bindings["UndeclaredSameOwner"]["defined_in"] == _INSTANCE_FROZEN_OWNER


def test_normalize_owner_moves_are_order_independent() -> None:
    """Reversing the declaration order must produce a byte-identical result: the
    two entries neither chain (one's output feeding the other's input) nor race
    for the same binding."""
    forward = _normalize_expected_for_facade_routes(_decoy_document())
    reversed_moves = dict(reversed(list(FACADE_ROOT_BINDING_OWNER_MOVES.items())))
    assert list(reversed_moves) != list(FACADE_ROOT_BINDING_OWNER_MOVES)
    backward = _normalize_expected_for_facade_routes(
        _decoy_document(), moves=reversed_moves
    )

    assert forward == backward


def test_normalize_owner_moves_apply_independently_of_each_other() -> None:
    """Declaring only one move must move only that binding. With both entries
    sharing a frozen owner, a substitution that keyed off the owner rather than
    the name would move both -- and pass every other test in this section."""
    for (name, legacy_module), _routed in _DECLARED_MOVES:
        other = next(
            key[0] for key, _r in _DECLARED_MOVES if key[0] != name
        )
        normalized = _normalize_expected_for_facade_routes(
            _decoy_document(),
            moves={
                (name, legacy_module): (
                    _INSTANCE_FROZEN_OWNER,
                    _INSTANCE_CANONICAL_OWNER,
                )
            },
        )
        bindings = normalized["root"]["bindings"]
        assert bindings[name]["defined_in"] == _INSTANCE_CANONICAL_OWNER
        assert bindings[other]["defined_in"] == _INSTANCE_FROZEN_OWNER, (
            f"declaring {name} also moved {other}"
        )


def test_declared_moves_are_the_only_thing_that_moves_the_instance_bindings() -> None:
    """``knowledge.models`` is now a converted, routed leaf itself, and every one
    of its routes lands on the same canonical module the two declared moves name.
    That makes the two mechanisms indistinguishable by destination, so this pins
    them apart by *cause*: with the moves dropped and every route still applied,
    both instance bindings must stay on the legacy owner.

    A substitution that keyed off the owner module rather than the exact routed
    symbol -- or a broad package-prefix rewrite -- would move them anyway, and
    would pass if this test only checked the fully-normalized result.
    """
    expected = load_json(PUBLIC_EXPORTS_PATH)
    declared = {name for (name, _legacy), _routed in _DECLARED_MOVES}

    routes_only = _normalize_expected_for_facade_routes(expected, moves={})
    for name in declared:
        assert routes_only["root"]["bindings"][name]["defined_in"] == (
            _INSTANCE_FROZEN_OWNER
        ), f"{name} moved without its declared entry"

    normalized = _normalize_expected_for_facade_routes(expected)
    for name in declared:
        assert normalized["root"]["bindings"][name]["defined_in"] == (
            _INSTANCE_CANONICAL_OWNER
        )

    # Every *other* root binding the frozen baseline recorded on a knowledge leaf
    # moves for an ordinary route, and each lands on that leaf's own canonical
    # counterpart -- never on some other domain's module.
    frozen_bindings = expected["root"]["bindings"]
    for name, binding in normalized["root"]["bindings"].items():
        if name in declared:
            continue
        frozen_owner = frozen_bindings[name]["defined_in"]
        if not str(frozen_owner).startswith("omnivia_memory.knowledge"):
            continue
        assert binding["defined_in"] == FACADE_ROUTES[frozen_owner][name], (
            f"{name} did not move to the canonical owner its route declares"
        )
