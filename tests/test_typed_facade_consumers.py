"""Keep strict-mypy consumer fixtures synchronized with accepted facades."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from baseline.inventory import FACADE_ROUTES

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_MANIFEST_LEAVES = {
    "omnivia_memory.module_manifest.models",
    "omnivia_memory.module_manifest.validation",
}
KNOWLEDGE_LEAVES = {
    "omnivia_memory.knowledge.models",
    "omnivia_memory.knowledge.normalize",
    "omnivia_memory.knowledge.validation",
}
GRAPH_LEAVES = {
    "omnivia_memory.graph.models",
    "omnivia_memory.graph.search_models",
}
# The two ingestion leaves are separate routes under separate hybrid barrels, so
# each gets its own fixture rather than sharing one: the partition below is
# exact, and a shared fixture would let either leaf's imports drift unnoticed
# behind the other's.
INGESTION_MODELS_LEAVES = {"omnivia_memory.ingestion.models"}
WATCHER_MODELS_LEAVES = {"omnivia_memory.ingestion.watcher.models"}
# The workspace leaf is the portable half of a hybrid barrel, so it gets a
# fixture of its own for the same reason the ingestion pair do.
WORKSPACE_MODELS_LEAVES = {"omnivia_memory.workspace.models"}
DEDICATED_LEAVES = (
    MODULE_MANIFEST_LEAVES
    | KNOWLEDGE_LEAVES
    | GRAPH_LEAVES
    | INGESTION_MODELS_LEAVES
    | WATCHER_MODELS_LEAVES
    | WORKSPACE_MODELS_LEAVES
)
CONSUMER_ROUTES = {
    "tests/typing/accepted_legacy_facade_consumer.py": (
        set(FACADE_ROUTES) - DEDICATED_LEAVES
    ),
    "tests/typing/graph_facade_consumer.py": GRAPH_LEAVES,
    "tests/typing/ingestion_models_facade_consumer.py": INGESTION_MODELS_LEAVES,
    "tests/typing/knowledge_facade_consumer.py": KNOWLEDGE_LEAVES,
    "tests/typing/module_manifest_facade_consumer.py": MODULE_MANIFEST_LEAVES,
    "tests/typing/watcher_models_facade_consumer.py": WATCHER_MODELS_LEAVES,
    "tests/typing/workspace_models_facade_consumer.py": WORKSPACE_MODELS_LEAVES,
}

#: The one consumer that is *not* keyed on ``FACADE_ROUTES``. The six hybrid
#: barrels are module routes in ``compatibility/facade-routes.v1.json``, not
#: symbol routes, so they have no entry in ``FACADE_ROUTES`` and cannot join the
#: partition above -- adding them there would invent per-symbol routes the
#: registry does not declare. Their typed contract is the six ``__all__`` tuples
#: instead, audited exactly by the tests at the end of this module.
HYBRID_BARREL_CONSUMER = "tests/typing/hybrid_barrel_consumer.py"

#: Each hybrid barrel's exact ordered ``__all__``. Restated here rather than read
#: off the barrels: this is the oracle the fixture is audited against, and
#: deriving it from the very modules the fixture imports would make the audit
#: agree with any drift instead of catching it.
HYBRID_BARREL_ALL: dict[str, tuple[str, ...]] = {
    "omnivia_memory.graph": (
        "ApprovalStatus",
        "Entity",
        "EntityType",
        "GraphSearchError",
        "GraphSearchQuery",
        "GraphSearchResult",
        "GraphSearchResultSet",
        "GraphSearchService",
        "Relationship",
        "RelationshipType",
    ),
    "omnivia_memory.ingestion": (
        "BaseChunker",
        "BaseExtractor",
        "CharacterChunker",
        "Chunk",
        "ChunkConfig",
        "ChunkRepository",
        "DOCXExtractor",
        "ExtractionResult",
        "FileInfo",
        "FileScanner",
        "FileType",
        "IngestResult",
        "IngestionPipeline",
        "MarkdownExtractor",
        "PDFExtractor",
        "ParagraphChunker",
        "ParseStatus",
        "ScanOptions",
        "Source",
    ),
    "omnivia_memory.ingestion.watcher": (
        "Debouncer",
        "DebounceConfig",
        "FileChange",
        "FileChangeBatch",
        "FileChangeType",
        "IndexerScheduler",
        "IndexerState",
        "IndexerStatus",
        "ScheduledJob",
        "SourceReference",
        "SourceTracker",
        "WatchedPath",
    ),
    "omnivia_memory.memory": (
        "Memory",
        "MemoryCreate",
        "MemoryUpdate",
        "MemoryService",
        "MemoryServiceError",
        "MemoryNotFoundError",
        "InvalidTransitionError",
    ),
    "omnivia_memory.memory_graph": (
        "Confidence",
        "EvidenceGraphResponse",
        "FIXTURE_TIME",
        "GraphPreviewEdge",
        "GraphPreviewKind",
        "GraphPreviewNode",
        "GraphPreviewResponse",
        "GraphPreviewState",
        "IngestionGraphAdapterError",
        "IngestionGraphWriteResult",
        "MemoryEntity",
        "MemoryFact",
        "MemoryFactStatus",
        "MemoryGraphFixture",
        "MemoryGraphStore",
        "MemoryGraphStoreError",
        "MemorySegment",
        "MemorySegmentKind",
        "MemorySource",
        "MemorySourceFreshness",
        "MemorySourceStatus",
        "MemorySourceType",
        "RetrievalTrace",
        "SourceRef",
        "ValidationResult",
        "assemble_evidence_graph",
        "assemble_graph_preview",
        "build_memory_graph_fixture",
        "chunk_to_memory_segment",
        "redact_segment_preview",
        "source_to_memory_source",
        "validate_evidence_graph_response",
        "validate_graph_preview_response",
        "validate_memory_entity",
        "validate_memory_fact",
        "validate_memory_segment",
        "validate_memory_source",
        "write_ingestion_records_to_graph",
    ),
    "omnivia_memory.workspace": (
        "ImportSummary",
        "Workspace",
        "WorkspaceCreate",
        "WorkspaceIndexStatus",
        "WorkspaceRepository",
        "WorkspaceService",
        "WorkspaceUpdate",
    ),
}

#: Definitions a split facade keeps owning: they are not routes, so a consumer
#: must not name them in a legacy ``from`` import, and the exact-route audit below
#: would reject it if one did. The Graph consumer exercises them through
#: ``import omnivia_memory.graph.search_models as runtime_search_models`` instead,
#: which the audit records as no route at all.
NON_ROUTED_RETAINED_HELPERS: dict[str, frozenset[str]] = {
    "omnivia_memory.graph.search_models": frozenset(
        {
            "compute_relevance_score",
            "score_name_match",
            "score_neighbor_overlap",
            "score_relationship_count",
        }
    ),
}


#: Every ``as`` rename any consumer fixture is allowed to use, as
#: ``(legacy module, routed name, local binding)``.
#:
#: A rename is normally a defect in these fixtures: it type-checks the alias
#: rather than the name Core's contract says the legacy path publishes. The
#: shared consumer is the exception and cannot avoid it -- it imports every route
#: that has no dedicated fixture, and five of those publish ``ValidationResult``,
#: four ``LifecycleState``, two ``Source``, two ``ProvenanceRequirement``, two
#: ``CreatedBy``. One module scope cannot bind those unaliased, so each is
#: renamed per owner. ``MemorySource`` is the second-order case: nothing else
#: routes that name, but ``memory.models.Source`` is already bound to it, so the
#: memory graph's own ``MemorySource`` has to move too.
#:
#: Declaring them as exact triples keeps the audit fail-closed: a rename of any
#: other name, or a different local binding for one of these, is rejected rather
#: than absorbed. ``test_only_the_declared_disambiguating_aliases_are_used`` pins
#: this set against the fixtures in both directions, so no declared alias can go
#: stale and no undeclared one can slip in. The binding-uniqueness check below
#: does not prove each rename is minimally necessary; it rejects any fixture
#: imports that still land on -- and so shadow -- the same local binding.
DISAMBIGUATING_ALIASES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("omnivia_memory._shared.validation", "ValidationResult", "SharedValidationResult"),
        (
            "omnivia_memory.app_manifest.models",
            "ProvenanceRequirement",
            "AppProvenanceRequirement",
        ),
        (
            "omnivia_memory.app_manifest.models",
            "ValidationResult",
            "AppManifestValidationResult",
        ),
        (
            "omnivia_memory.app_shell_bridge.models",
            "ValidationResult",
            "AppShellValidationResult",
        ),
        (
            "omnivia_memory.component_contract.models",
            "ProvenanceRequirement",
            "ComponentProvenanceRequirement",
        ),
        (
            "omnivia_memory.component_contract.models",
            "ValidationResult",
            "ComponentValidationResult",
        ),
        (
            "omnivia_memory.control_plane.models",
            "LifecycleState",
            "ControlPlaneLifecycleState",
        ),
        (
            "omnivia_memory.control_plane.models",
            "ValidationResult",
            "ControlPlaneValidationResult",
        ),
        ("omnivia_memory.lifecycle.rules", "LifecycleState", "RulesLifecycleState"),
        ("omnivia_memory.memory.models", "CreatedBy", "MemoryCreatedBy"),
        ("omnivia_memory.memory.models", "LifecycleState", "MemoryLifecycleState"),
        ("omnivia_memory.memory.models", "Source", "MemorySource"),
        ("omnivia_memory.memory_graph.models", "MemorySource", "MemoryGraphSource"),
    }
)


def _is_canonical_module(module: str) -> bool:
    return module == "omnivia_core" or module.startswith("omnivia_core.")


def _legacy_imports_from_source(
    source: str, source_label: str
) -> dict[str, set[str]]:
    """The routed legacy names each fixture imports, as ``{module: {name, ...}}``.

    The syntax of each routed ``from`` import is audited on the way, because the
    exact-route comparison below cannot see it. A set of names records *which*
    routes a fixture reaches, and every form rejected here reaches the same set
    while exercising something other than the route:

    * a relative import does not name the legacy module at all;
    * ``import *`` binds whatever the wrapper happens to publish, not the routed
      names, so mypy would check nothing this audit believes it checked;
    * ``Source as Renamed`` type-checks the alias, not the name Core's contract
      says the legacy path publishes. Only the exact renames declared in
      ``DISAMBIGUATING_ALIASES`` are allowed, and only because one module scope
      cannot bind a name two routes both publish;
    * a name repeated inside one block, repeated across the blocks for one
      module, or a second *unaliased* block for the same module all collapse into
      the same set -- so the fixture would look complete while silently having
      dropped one of its routes' symbols in the edit that duplicated another;
    * two imports landing on the same local binding shadow one another, so one of
      them is not being checked at all.

    A plain ``import omnivia_memory.<module>`` stays allowed and records no
    route: that is how the Graph fixture exercises the retained, non-routed
    helpers of a split facade (see ``NON_ROUTED_RETAINED_HELPERS``).
    """
    tree = ast.parse(source)
    # Per module, one entry per ``from`` block: its routed names, and whether the
    # block binds any of them unaliased.
    blocks: dict[str, list[tuple[list[str], bool]]] = {}
    bindings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not _is_canonical_module(alias.name), (
                    f"{source_label} must exercise legacy paths, not {alias.name}"
                )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        assert node.level == 0, (
            f"{source_label} uses a relative import (level {node.level}) on line "
            f"{node.lineno}; a routed legacy module must be named absolutely"
        )
        # A level-0 ``ImportFrom`` always names a module.
        assert node.module is not None
        assert not _is_canonical_module(node.module), (
            f"{source_label} must exercise legacy paths, not {node.module}"
        )
        if not node.module.startswith("omnivia_memory."):
            continue
        names: list[str] = []
        unaliased = False
        for alias in node.names:
            assert alias.name != "*", (
                f"{source_label} star-imports {node.module}; name every routed "
                f"symbol explicitly"
            )
            if alias.asname is None:
                unaliased = True
            else:
                assert (node.module, alias.name, alias.asname) in DISAMBIGUATING_ALIASES, (
                    f"{source_label} imports {node.module}.{alias.name} as "
                    f"{alias.asname!r} on line {node.lineno}; only the declared "
                    f"disambiguating aliases may rename a routed symbol"
                )
            assert alias.name not in names, (
                f"{source_label} imports {node.module}.{alias.name} twice in one block"
            )
            names.append(alias.name)
            bindings.append(alias.asname or alias.name)
        blocks.setdefault(node.module, []).append((names, unaliased))

    shadowed = sorted({name for name in bindings if bindings.count(name) > 1})
    assert not shadowed, (
        f"{source_label} binds {shadowed} more than once; one of each pair is "
        f"shadowed and type-checks nothing"
    )

    imports: dict[str, set[str]] = {}
    for module, module_blocks in blocks.items():
        unaliased_blocks = sum(1 for _names, unaliased in module_blocks if unaliased)
        assert unaliased_blocks <= 1, (
            f"{source_label} has {unaliased_blocks} unaliased "
            f"`from {module} import ...` blocks; a routed module gets one, plus one "
            f"block per declared disambiguating alias"
        )
        names = [name for block_names, _unaliased in module_blocks for name in block_names]
        repeated = sorted({name for name in names if names.count(name) > 1})
        assert not repeated, (
            f"{source_label} imports {module} names {repeated} in more than one block"
        )
        imports[module] = set(names)
    return imports


def _legacy_imports(relative_path: str) -> dict[str, set[str]]:
    return _legacy_imports_from_source(
        (REPO_ROOT / relative_path).read_text(encoding="utf-8"),
        relative_path,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import omnivia_core",
        "import omnivia_core.memory",
        "from omnivia_core import memory",
        "from omnivia_core.memory import Memory",
    ],
)
def test_typed_consumer_audit_rejects_every_canonical_import_form(
    source: str,
) -> None:
    with pytest.raises(AssertionError, match="must exercise legacy paths"):
        _legacy_imports_from_source(source, "mutated consumer")


def test_typed_consumer_audit_uses_segment_aware_package_matching() -> None:
    assert _legacy_imports_from_source("import omnivia_corex", "near miss") == {}


@pytest.mark.parametrize(
    ("source", "pattern"),
    [
        (
            "from omnivia_memory.ingestion.models import Source as Renamed",
            "declared disambiguating aliases",
        ),
        # A *declared* alias, but for the wrong module: the triple is exact.
        (
            "from omnivia_memory.ingestion.models import Source as MemorySource",
            "declared disambiguating aliases",
        ),
        ("from omnivia_memory.ingestion.models import *", "star-imports"),
        (
            "from omnivia_memory.ingestion.models import Source, Source",
            "twice in one block",
        ),
        (
            (
                "from omnivia_memory.ingestion.models import Source\n"
                "from omnivia_memory.ingestion.models import Chunk"
            ),
            "unaliased",
        ),
        (
            (
                "from omnivia_memory.ingestion.models import Source\n"
                "from omnivia_memory.ingestion.models import Source"
            ),
            "more than once",
        ),
        # The same routed name twice for one module, once plainly and once under a
        # rename that *is* declared: distinct bindings, so only the cross-block
        # repeat check catches it.
        (
            (
                "from omnivia_memory.memory.models import Source\n"
                "from omnivia_memory.memory.models import Source as MemorySource"
            ),
            "in more than one block",
        ),
        # Two routes' symbols landing on one binding: legal Python, and one of
        # them is shadowed away from mypy entirely.
        (
            (
                "from omnivia_memory.ingestion.models import Source\n"
                "from omnivia_memory.memory.models import Source as MemorySource\n"
                "from omnivia_memory.memory_graph.models import MemorySource"
            ),
            "more than once",
        ),
        ("from .models import Source", "relative import"),
    ],
    ids=[
        "aliased",
        "declared-alias-wrong-module",
        "star",
        "duplicate-name-in-block",
        "second-block-other-names",
        "second-block-same-names",
        "same-name-plain-and-aliased",
        "shadowed-binding",
        "relative",
    ],
)
def test_typed_consumer_audit_rejects_inexact_legacy_import_syntax(
    source: str, pattern: str
) -> None:
    """Each of these records the same route set as the accepted forms below, so the
    set comparison cannot tell them apart; the syntax audit has to."""
    with pytest.raises(AssertionError, match=pattern):
        _legacy_imports_from_source(source, "mutated consumer")


def test_typed_consumer_audit_accepts_a_declared_disambiguating_alias() -> None:
    """The shape the shared fixture really has: one unaliased block per module plus
    one block per declared rename, recorded under the *routed* name so the exact
    comparison below still sees the contract's own symbol."""
    assert _legacy_imports_from_source(
        "from omnivia_memory.memory.models import CreatedBy as MemoryCreatedBy\n"
        "from omnivia_memory.memory.models import Source as MemorySource\n"
        "from omnivia_memory.memory_graph.models import MemorySource as MemoryGraphSource\n",
        "shared consumer",
    ) == {
        "omnivia_memory.memory.models": {"CreatedBy", "Source"},
        "omnivia_memory.memory_graph.models": {"MemorySource"},
    }


def test_only_the_declared_disambiguating_aliases_are_used() -> None:
    """Pin the allowlist against the fixtures in both directions. Left unpinned it
    could grow stale in either: an entry kept after the rename it covered was
    removed would silently license a future one, and the audit above already
    rejects an alias that is not declared."""
    used: set[tuple[str, str, str]] = set()
    for relative_path in CONSUMER_ROUTES:
        tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("omnivia_memory."):
                continue
            used.update(
                (node.module, alias.name, alias.asname)
                for alias in node.names
                if alias.asname is not None
            )
    assert used == set(DISAMBIGUATING_ALIASES), (
        f"declared but unused={sorted(set(DISAMBIGUATING_ALIASES) - used)}, "
        f"used but undeclared={sorted(used - set(DISAMBIGUATING_ALIASES))}"
    )


def test_typed_consumer_audit_accepts_an_exact_legacy_from_import() -> None:
    """The defect-free base for the rejections above: absolute, unaliased, unique."""
    assert _legacy_imports_from_source(
        "from omnivia_memory.ingestion.models import Chunk, Source",
        "accepted consumer",
    ) == {"omnivia_memory.ingestion.models": {"Chunk", "Source"}}


def test_typed_consumer_audit_allows_a_plain_legacy_module_import() -> None:
    """The Graph fixture's route into the retained, non-routed helpers: allowed,
    aliased or not, and recorded as no route at all."""
    assert (
        _legacy_imports_from_source(
            "import omnivia_memory.graph.search_models as runtime_search_models",
            "graph consumer",
        )
        == {}
    )


def test_typed_consumers_cover_every_accepted_facade_route_exactly() -> None:
    """A newly accepted route must add its API symbols to a typed consumer."""
    assert set().union(*CONSUMER_ROUTES.values()) == set(FACADE_ROUTES)
    assert DEDICATED_LEAVES <= set(FACADE_ROUTES)
    # The fixtures must *partition* the routes, not merely cover them: a leaf
    # named by two consumers would let one of them drop its imports unnoticed.
    assert sum(len(routes) for routes in CONSUMER_ROUTES.values()) == len(FACADE_ROUTES)
    dedicated_groups = (
        MODULE_MANIFEST_LEAVES,
        KNOWLEDGE_LEAVES,
        GRAPH_LEAVES,
        INGESTION_MODELS_LEAVES,
        WATCHER_MODELS_LEAVES,
        WORKSPACE_MODELS_LEAVES,
    )
    for index, first in enumerate(dedicated_groups):
        for second in dedicated_groups[index + 1 :]:
            assert first.isdisjoint(second)

    for relative_path, expected_modules in CONSUMER_ROUTES.items():
        imports = _legacy_imports(relative_path)
        expected = {
            module: set(FACADE_ROUTES[module]) for module in expected_modules
        }
        assert imports == expected, (
            f"{relative_path} legacy imports drifted from FACADE_ROUTES: "
            f"actual={imports!r}, expected={expected!r}"
        )


def test_typed_consumers_never_from_import_a_non_routed_retained_helper() -> None:
    """A split facade's retained definitions are not routes. A consumer that named
    one in a legacy ``from`` import would break the exact-route audit above, so the
    only way to exercise them is through a module import -- which that audit records
    as no route at all. Pin both halves of that arrangement: the helpers really are
    absent from the route map, and no consumer ``from``-imports one.
    """
    for legacy_module, helpers in NON_ROUTED_RETAINED_HELPERS.items():
        assert legacy_module in FACADE_ROUTES
        assert helpers.isdisjoint(FACADE_ROUTES[legacy_module])
        for relative_path in CONSUMER_ROUTES:
            imported = _legacy_imports(relative_path).get(legacy_module, set())
            assert imported.isdisjoint(helpers), (
                f"{relative_path} from-imports the non-routed {legacy_module} helpers "
                f"{sorted(imported & helpers)}; use a module import instead"
            )


# ---------------------------------------------------------------------------
# The hybrid-barrel consumer, audited against the six ``__all__`` tuples.
# ---------------------------------------------------------------------------


def test_hybrid_barrel_consumer_is_outside_the_facade_routes_partition() -> None:
    """The six barrels are module routes, not symbol routes: they have no
    ``FACADE_ROUTES`` entry, and their consumer is deliberately not part of the
    partition above. Pinned in both directions so neither side can absorb the
    other -- adding the barrels to ``FACADE_ROUTES`` would invent per-symbol
    routes the frozen registry does not declare, and adding this fixture to
    ``CONSUMER_ROUTES`` would break the exact partition the leaves depend on.
    """
    assert HYBRID_BARREL_CONSUMER not in CONSUMER_ROUTES
    for barrel in HYBRID_BARREL_ALL:
        assert barrel not in FACADE_ROUTES
    assert (REPO_ROOT / HYBRID_BARREL_CONSUMER).is_file()


def test_hybrid_barrel_consumer_imports_exactly_the_six_advertised_surfaces() -> None:
    """The fixture's legacy imports are exactly the six barrels, and exactly each
    barrel's whole ``__all__`` -- no leaf path, no canonical path, nothing missing
    and nothing extra. The same syntax audit the leaf consumers get applies, so a
    star import, an alias, a relative import, or a duplicated name is rejected
    before the set comparison is even reached.
    """
    imports = _legacy_imports(HYBRID_BARREL_CONSUMER)
    assert imports == {
        barrel: set(names) for barrel, names in HYBRID_BARREL_ALL.items()
    }
    total = sum(len(names) for names in HYBRID_BARREL_ALL.values())
    assert total == 93
    assert sum(len(names) for names in imports.values()) == total


def test_hybrid_barrel_all_tuples_are_pairwise_disjoint() -> None:
    """Why the fixture needs no ``as`` rename at all, stated as a checked fact
    rather than left to luck: no name is advertised by two of the six barrels, so
    one module scope can bind all ninety-three unaliased. A future export that
    collided would make this fail here -- and the alias audit above would then
    have to be extended deliberately, rather than a shadowed binding silently
    type-checking nothing."""
    seen: dict[str, str] = {}
    for barrel, names in HYBRID_BARREL_ALL.items():
        assert len(names) == len(set(names)), barrel
        for name in names:
            assert name not in seen, (name, seen.get(name), barrel)
            seen[name] = barrel
    assert len(seen) == 93

    tree = ast.parse((REPO_ROOT / HYBRID_BARREL_CONSUMER).read_text(encoding="utf-8"))
    aliased = [
        (node.module, alias.name, alias.asname)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname is not None
    ]
    assert aliased == []


def test_hybrid_barrel_consumer_never_reaches_a_leaf_or_canonical_path() -> None:
    """The whole point of this fixture: it type-checks the *barrel* paths. An
    import that dropped down to a converted leaf would exercise a route the leaf
    consumers already cover and stop proving the barrel republishes it usefully
    typed; one that reached ``omnivia_core`` would prove nothing about the legacy
    surface at all (and the shared audit rejects it outright).
    """
    tree = ast.parse((REPO_ROOT / HYBRID_BARREL_CONSUMER).read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    legacy = {module for module in modules if module.startswith("omnivia_")}
    assert legacy == set(HYBRID_BARREL_ALL)
    assert legacy.isdisjoint(FACADE_ROUTES)
