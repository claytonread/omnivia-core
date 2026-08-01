"""Tests for the package-level public API."""

import ast
from pathlib import Path

import omnivia_memory
from omnivia_memory import (
    GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_CONTRACT_VERSION,
    GraphConfidence,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphOrigin,
    GraphReviewStatus,
    GraphSensitivity,
    GraphSourceType,
    GraphVisibility,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_source_path,
    normalize_space_id,
    validate_knowledge_space,
)


def test_top_level_exports_portable_knowledge_contracts() -> None:
    """Package root imports construct a valid portable knowledge space."""
    source = KnowledgeSource(
        id="source-01",
        space_id=normalize_space_id("Example Space"),
        source_type=GraphSourceType.NOTE,
        title="Daily Note",
        relative_path=normalize_source_path("notes/daily.md"),
    )
    object_source_ref = SourceRef(
        source_id=source.id,
        source_type=GraphSourceType.NOTE,
        path=source.relative_path,
        confidence=GraphConfidence.EXTRACTED,
    )
    object_one = KnowledgeObject(
        id="daily-note",
        space_id=source.space_id,
        kind="note",
        title="Daily Note",
        tags=["daily-note"],
        source_refs=[object_source_ref],
        confidence=GraphConfidence.EXTRACTED,
        review_status=GraphReviewStatus.REVIEWED,
    )
    graph_fragment = GraphFragment(
        id="fragment-01",
        space_id=source.space_id,
        contract_version=GRAPH_CONTRACT_VERSION,
        origin=GraphOrigin.MANUAL,
        owner="portable",
        nodes=[
            GraphNode(
                id=normalize_graph_node_id("Daily Note"),
                space_id=source.space_id,
                label="Daily Note",
                kind="note",
                object_id=object_one.id,
                source_refs=[object_source_ref],
                confidence=GraphConfidence.EXTRACTED,
            )
        ],
        edges=[
            GraphEdge(
                id=normalize_graph_edge_id("Daily Note self edge"),
                space_id=source.space_id,
                source="daily-note",
                target="daily-note",
                relation="related_to",
                source_refs=[object_source_ref],
                confidence=GraphConfidence.AMBIGUOUS,
                evidence_strength=GraphEvidenceStrength.WEAK,
                visibility=GraphVisibility.PRIVATE,
                sensitivity=GraphSensitivity.INTERNAL,
                missing_evidence=False,
            )
        ],
        source_refs=[object_source_ref],
    )
    space = KnowledgeSpace(
        id=source.space_id,
        title="Example Space",
        space_type="personal vault",
        contract_version=KNOWLEDGE_CONTRACT_VERSION,
        sources=[source],
        objects=[object_one],
        graph_fragments=[graph_fragment],
    )

    result = validate_knowledge_space(space)

    assert result.valid
    assert not result.errors


def test_all_declares_contract_only_root_api() -> None:
    """__all__ records the intentional contract-only root API."""
    expected = {
        "GraphFragment",
        "GraphNode",
        "GraphEdge",
        "GraphConfidence",
        "GraphReviewStatus",
        "KnowledgeObject",
        "KnowledgeSource",
        "KnowledgeSpace",
        "SourceRef",
        "ValidationResult",
        "normalize_space_id",
        "normalize_source_path",
        "validate_knowledge_space",
    }
    forbidden = {
        "MemoryService",
        "IngestionPipeline",
        "FileScanner",
        "SearchService",
        "GraphService",
        "Database",
        "WorkspaceService",
        "get_database",
    }

    assert expected.issubset(set(omnivia_memory.__all__))
    assert forbidden.isdisjoint(set(omnivia_memory.__all__))


FORBIDDEN_IMPORT_PREFIXES = (
    "omnivia_memory.ingestion",
    "omnivia_memory.persistence",
    "omnivia_memory.search",
    "omnivia_memory.workspace",
    "omnivia_memory.app_shell_bridge",
    "omnivia_memory.graph.service",
    "omnivia_memory.graph.repository",
    "omnivia_memory.memory.service",
    "omnivia_memory.ingestion.watcher",
    "mcp",
    "cli",
)


#: The package root's two sanctioned runtime imports, and the one binding each
#: supplies. The root is a compatibility facade: ``Database`` and ``MemoryService``
#: are deliberately still owned by the legacy runtime, stay out of ``__all__``, and
#: are imported *absolutely* -- the root's frozen source policy
#: (``baseline.facade_manifest.root_facade_defects``) rejects relative imports
#: outright, so these two edges are visible to the scan below where they used to be
#: hidden behind ``from .persistence import ...``.
#:
#: They are exempted by exact ``(module, name)`` pair, never by prefix: anything
#: else the root reached into these packages for -- or any other runtime module it
#: reached at all -- still fails. The knowledge surface files get no exemption.
ROOT_RUNTIME_COMPATIBILITY_IMPORTS = frozenset(
    {
        ("omnivia_memory.persistence", "Database"),
        ("omnivia_memory.memory.service", "MemoryService"),
    }
)

#: The same exemption expressed as the module paths ``_absolute_imported_modules``
#: reports for them, so the scan can subtract exactly those and nothing more.
ROOT_RUNTIME_EXEMPT_MODULES = frozenset(
    {module for module, _name in ROOT_RUNTIME_COMPATIBILITY_IMPORTS}
    | {f"{module}.{name}" for module, name in ROOT_RUNTIME_COMPATIBILITY_IMPORTS}
)


def _absolute_imported_modules(path: Path) -> set[str]:
    """Return the absolute module paths a file imports, taken from its AST.

    ``from x import y`` contributes both ``x`` and ``x.y`` because ``y`` may name
    a submodule. Relative imports are skipped: they resolve inside the package,
    and what the root may re-export from them is constrained by
    ``test_all_declares_contract_only_root_api``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_contract_surface_has_no_runtime_import_creep() -> None:
    """Knowledge surface files stay isolated from runtime-oriented modules.

    The package root is held to the same rule with exactly one exemption: the two
    ``(module, name)`` pairs in ``ROOT_RUNTIME_COMPATIBILITY_IMPORTS``. Those are
    the compatibility bindings Core deliberately does not own, and nothing else in
    the root -- not another name from those two modules, and not any other runtime
    module -- is allowed through.
    """
    source_root = Path(__file__).parents[1] / "src" / "omnivia_memory"
    knowledge_files = sorted((source_root / "knowledge").glob("*.py"))
    root_init = source_root / "__init__.py"
    files_to_check = knowledge_files + [root_init]

    for path in files_to_check:
        exempt = ROOT_RUNTIME_EXEMPT_MODULES if path == root_init else frozenset()
        for module in sorted(_absolute_imported_modules(path) - exempt):
            for prefix in FORBIDDEN_IMPORT_PREFIXES:
                assert module != prefix and not module.startswith(f"{prefix}."), (
                    path.name,
                    module,
                    prefix,
                )


def test_root_runtime_compatibility_imports_are_exactly_the_declared_two() -> None:
    """The exemption above is pinned against the root's real source in both
    directions, so it can neither go stale nor quietly widen.

    Every exempted pair must really be imported by the root, and every runtime
    module the root really reaches must be an exempted one. A third runtime import
    would fail here as well as at the frozen root source gate.
    """
    root_init = Path(__file__).parents[1] / "src" / "omnivia_memory" / "__init__.py"
    tree = ast.parse(root_init.read_text(encoding="utf-8"), filename=str(root_init))

    imported: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Import), "the root uses from-imports only"
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the root imports absolutely, never relatively"
            assert node.module is not None
            imported.update((node.module, alias.name) for alias in node.names)

    legacy = {
        (module, name)
        for module, name in imported
        if module == "omnivia_memory" or module.startswith("omnivia_memory.")
    }
    assert legacy == set(ROOT_RUNTIME_COMPATIBILITY_IMPORTS), legacy

    # ...and both are runtime modules the ban above really covers, so the exemption
    # is exercising the rule rather than pointing somewhere it never applied.
    for module, _name in ROOT_RUNTIME_COMPATIBILITY_IMPORTS:
        assert any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        ), module

    # Neither may be advertised: the root's contract-only ``__all__`` is what keeps
    # them compatibility-only.
    for _module, name in ROOT_RUNTIME_COMPATIBILITY_IMPORTS:
        assert name not in omnivia_memory.__all__
        assert hasattr(omnivia_memory, name)
