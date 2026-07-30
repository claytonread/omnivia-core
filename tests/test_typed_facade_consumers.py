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
DEDICATED_LEAVES = MODULE_MANIFEST_LEAVES | KNOWLEDGE_LEAVES | GRAPH_LEAVES
CONSUMER_ROUTES = {
    "tests/typing/accepted_legacy_facade_consumer.py": (
        set(FACADE_ROUTES) - DEDICATED_LEAVES
    ),
    "tests/typing/graph_facade_consumer.py": GRAPH_LEAVES,
    "tests/typing/knowledge_facade_consumer.py": KNOWLEDGE_LEAVES,
    "tests/typing/module_manifest_facade_consumer.py": MODULE_MANIFEST_LEAVES,
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


def _is_canonical_module(module: str) -> bool:
    return module == "omnivia_core" or module.startswith("omnivia_core.")


def _legacy_imports_from_source(
    source: str, source_label: str
) -> dict[str, set[str]]:
    tree = ast.parse(source)
    imports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not _is_canonical_module(alias.name), (
                    f"{source_label} must exercise legacy paths, not {alias.name}"
                )
            continue
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        assert not _is_canonical_module(node.module), (
            f"{source_label} must exercise legacy paths, not {node.module}"
        )
        if node.module.startswith("omnivia_memory."):
            imports.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
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


def test_typed_consumers_cover_every_accepted_facade_route_exactly() -> None:
    """A newly accepted route must add its API symbols to a typed consumer."""
    assert set().union(*CONSUMER_ROUTES.values()) == set(FACADE_ROUTES)
    assert DEDICATED_LEAVES <= set(FACADE_ROUTES)
    # The fixtures must *partition* the routes, not merely cover them: a leaf
    # named by two consumers would let one of them drop its imports unnoticed.
    assert sum(len(routes) for routes in CONSUMER_ROUTES.values()) == len(FACADE_ROUTES)
    for first, second in (
        (MODULE_MANIFEST_LEAVES, KNOWLEDGE_LEAVES),
        (MODULE_MANIFEST_LEAVES, GRAPH_LEAVES),
        (KNOWLEDGE_LEAVES, GRAPH_LEAVES),
    ):
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
