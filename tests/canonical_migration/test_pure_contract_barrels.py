"""Barrel acceptance coverage for the seven pure-contract canonical packages:
``omnivia_core.lifecycle``, ``.provenance``, ``.graph``, ``.ingestion``,
``.ingestion.watcher``, ``.memory``, and ``.workspace``.

``test_parity.py`` / ``test_behavioral_parity.py`` already prove each owner
leaf (``lifecycle.models``, ``lifecycle.rules``, ``provenance.models``,
``graph.models``, ``graph.search_models``, ``ingestion.models``,
``ingestion.watcher.models``, ``memory.models``, ``workspace.models``) is an
exact port of its legacy counterpart. This module is only concerned with what
those leave uncovered: each barrel's own re-export contract (exact historical
``__all__`` order, owner bindings anchored to the frozen Phase 0 inventory,
star-import surface, no ``__getattr__``/``__dir__`` escape hatch, the
static AST shape of the barrel source itself, the exact runtime-only
exclusions and the modules that must never load, and isolated-import module
closure under several import orders).

The Phase 0 inventory (``baseline/inventories/public-exports.json``) sorts
every barrel's ``all`` list, so it is a **membership** oracle only -- it is
never used here to assert order. Order is instead pinned against the *live*
legacy ``__all__`` tuple (filtered for the mixed barrels' runtime-only
exclusions, preserving legacy order) and against the explicit frozen
source-preserving tuples below.
"""

from __future__ import annotations
import __future__

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PUBLIC_EXPORTS_INVENTORY = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

with PUBLIC_EXPORTS_INVENTORY.open(encoding="utf-8") as _inventory_file:
    FROZEN_MODULES: dict[str, Any] = json.load(_inventory_file)["modules"]

# ---------------------------------------------------------------------------
# Frozen historical ordered __all__ tuples (source-preserving, not sorted).
# The Phase 0 inventory sorts __all__, so it is never used as an order
# oracle anywhere in this module -- only as a membership/provenance oracle.
# ---------------------------------------------------------------------------

LIFECYCLE_ALL: tuple[str, ...] = ("LifecycleState", "LifecycleRules", "CreatedBy")
PROVENANCE_ALL: tuple[str, ...] = ("Source", "SourceType")
GRAPH_ALL: tuple[str, ...] = (
    "ApprovalStatus",
    "Entity",
    "EntityType",
    "GraphSearchQuery",
    "GraphSearchResult",
    "GraphSearchResultSet",
    "Relationship",
    "RelationshipType",
)
INGESTION_ALL: tuple[str, ...] = ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source")
WATCHER_ALL: tuple[str, ...] = (
    "DebounceConfig",
    "FileChange",
    "FileChangeBatch",
    "FileChangeType",
    "IndexerScheduler",
    "IndexerState",
    "IndexerStatus",
    "ScheduledJob",
    "SourceReference",
    "WatchedPath",
)
MEMORY_ALL: tuple[str, ...] = ("Memory", "MemoryCreate", "MemoryUpdate")
WORKSPACE_ALL: tuple[str, ...] = (
    "ImportSummary",
    "Workspace",
    "WorkspaceCreate",
    "WorkspaceIndexStatus",
    "WorkspaceUpdate",
)

#: Owner maps: every barrel export bound to the exact canonical leaf that
#: defines it, per the task packet's explicit owner-identity table.
LIFECYCLE_OWNER_BY_EXPORT: dict[str, str] = {
    "LifecycleState": "omnivia_core.lifecycle.models",
    "LifecycleRules": "omnivia_core.lifecycle.rules",
    "CreatedBy": "omnivia_core.lifecycle.rules",
}
PROVENANCE_OWNER_BY_EXPORT: dict[str, str] = {
    "Source": "omnivia_core.provenance.models",
    "SourceType": "omnivia_core.provenance.models",
}
GRAPH_OWNER_BY_EXPORT: dict[str, str] = {
    "ApprovalStatus": "omnivia_core.graph.models",
    "Entity": "omnivia_core.graph.models",
    "EntityType": "omnivia_core.graph.models",
    "Relationship": "omnivia_core.graph.models",
    "RelationshipType": "omnivia_core.graph.models",
    "GraphSearchQuery": "omnivia_core.graph.search_models",
    "GraphSearchResult": "omnivia_core.graph.search_models",
    "GraphSearchResultSet": "omnivia_core.graph.search_models",
}
INGESTION_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.ingestion.models" for name in INGESTION_ALL
}
WATCHER_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.ingestion.watcher.models" for name in WATCHER_ALL
}
MEMORY_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.memory.models" for name in MEMORY_ALL
}
WORKSPACE_OWNER_BY_EXPORT: dict[str, str] = {
    name: "omnivia_core.workspace.models" for name in WORKSPACE_ALL
}

#: Runtime-only names the legacy barrel exports but the canonical barrel must
#: not -- empty for the two pure barrels (lifecycle, provenance), whose live
#: legacy ``__all__`` is already identical to the frozen tuple.
LIFECYCLE_EXCLUSIONS: frozenset[str] = frozenset()
PROVENANCE_EXCLUSIONS: frozenset[str] = frozenset()
GRAPH_EXCLUSIONS: frozenset[str] = frozenset({"GraphSearchError", "GraphSearchService"})
INGESTION_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "BaseChunker",
        "BaseExtractor",
        "CharacterChunker",
        "ChunkConfig",
        "ChunkRepository",
        "DOCXExtractor",
        "FileInfo",
        "FileScanner",
        "IngestResult",
        "IngestionPipeline",
        "MarkdownExtractor",
        "PDFExtractor",
        "ParagraphChunker",
        "ScanOptions",
    }
)
WATCHER_EXCLUSIONS: frozenset[str] = frozenset({"Debouncer", "SourceTracker"})
MEMORY_EXCLUSIONS: frozenset[str] = frozenset(
    {"MemoryService", "MemoryServiceError", "MemoryNotFoundError", "InvalidTransitionError"}
)
WORKSPACE_EXCLUSIONS: frozenset[str] = frozenset({"WorkspaceRepository", "WorkspaceService"})

#: Tuples that name runtime module paths barrel imports must never load.
#: Module existence is not asserted; a future runtime module may exist
#: without entering the portable barrel via a bare ``import``.
LIFECYCLE_FORBIDDEN_MODULES: tuple[str, ...] = ()
PROVENANCE_FORBIDDEN_MODULES: tuple[str, ...] = ()
GRAPH_FORBIDDEN_MODULES: tuple[str, ...] = (
    "omnivia_core.graph.search_service",
    "omnivia_core.graph.repository",
    "omnivia_core.graph.service",
)
INGESTION_FORBIDDEN_MODULES: tuple[str, ...] = (
    "omnivia_core.ingestion.chunker",
    "omnivia_core.ingestion.extractors",
    "omnivia_core.ingestion.pipeline",
    "omnivia_core.ingestion.repositories",
    "omnivia_core.ingestion.scanner",
)
WATCHER_FORBIDDEN_MODULES: tuple[str, ...] = (
    "omnivia_core.ingestion.watcher.debouncer",
    "omnivia_core.ingestion.watcher.tracker",
)
MEMORY_FORBIDDEN_MODULES: tuple[str, ...] = ("omnivia_core.memory.service",)
WORKSPACE_FORBIDDEN_MODULES: tuple[str, ...] = (
    "omnivia_core.workspace.repository",
    "omnivia_core.workspace.service",
)

#: Modules a canonical pure-contract leaf/barrel must never pull in: the
#: legacy runtime package; SQLAlchemy/SQLite; HTTP runtimes; Core's own
#: runtime/CLI/MCP sibling distributions; the Platform/Dev/Cloud/Apps/Pro
#: Modules. None of these are ever legitimately reached from a pure
#: dataclass/enum + stdlib-typing leaf.
FORBIDDEN_LEAKAGE_MODULES: frozenset[str] = frozenset(
    {
        "omnivia_memory",
        "sqlalchemy",
        "sqlite3",
        "fastapi",
        "starlette",
        "httpx",
        "requests",
        "omnivia_core_runtime",
        "omnivia_core_mcp",
        "omnivia_core_cli",
        "omnivia_platform",
        "omnivia_dev",
        "omnivia_cloud",
        "omnivia_apps",
        "omnivia_pro",
    }
)

BARRELS: tuple[dict[str, Any], ...] = (
    {
        "id": "lifecycle",
        "canonical": "omnivia_core.lifecycle",
        "legacy": "omnivia_memory.lifecycle",
        "frozen_all": LIFECYCLE_ALL,
        "owner_by_export": LIFECYCLE_OWNER_BY_EXPORT,
        "runtime_exclusions": LIFECYCLE_EXCLUSIONS,
        "forbidden_runtime_modules": LIFECYCLE_FORBIDDEN_MODULES,
        "children": {
            "models": "omnivia_core.lifecycle.models",
            "rules": "omnivia_core.lifecycle.rules",
        },
        "incidental_children": {},
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.lifecycle",
                "omnivia_core.lifecycle.models",
                "omnivia_core.lifecycle.rules",
            }
        ),
        "approved_leaves": frozenset(
            {"omnivia_core.lifecycle.models", "omnivia_core.lifecycle.rules"}
        ),
        "order_cases": (
            ("barrel-first", ("omnivia_core.lifecycle",)),
            (
                "models-first",
                (
                    "omnivia_core.lifecycle.models",
                    "omnivia_core.lifecycle.rules",
                    "omnivia_core.lifecycle",
                ),
            ),
            (
                "rules-first",
                (
                    "omnivia_core.lifecycle.rules",
                    "omnivia_core.lifecycle.models",
                    "omnivia_core.lifecycle",
                ),
            ),
        ),
    },
    {
        "id": "provenance",
        "canonical": "omnivia_core.provenance",
        "legacy": "omnivia_memory.provenance",
        "frozen_all": PROVENANCE_ALL,
        "owner_by_export": PROVENANCE_OWNER_BY_EXPORT,
        "runtime_exclusions": PROVENANCE_EXCLUSIONS,
        "forbidden_runtime_modules": PROVENANCE_FORBIDDEN_MODULES,
        "children": {"models": "omnivia_core.provenance.models"},
        "incidental_children": {},
        "expected_closure": frozenset(
            {"omnivia_core", "omnivia_core.provenance", "omnivia_core.provenance.models"}
        ),
        "approved_leaves": frozenset({"omnivia_core.provenance.models"}),
        "order_cases": (
            ("barrel-first", ("omnivia_core.provenance",)),
            ("models-first", ("omnivia_core.provenance.models", "omnivia_core.provenance")),
        ),
    },
    {
        "id": "graph",
        "canonical": "omnivia_core.graph",
        "legacy": "omnivia_memory.graph",
        "frozen_all": GRAPH_ALL,
        "owner_by_export": GRAPH_OWNER_BY_EXPORT,
        "runtime_exclusions": GRAPH_EXCLUSIONS,
        "forbidden_runtime_modules": GRAPH_FORBIDDEN_MODULES,
        "children": {
            "models": "omnivia_core.graph.models",
            "search_models": "omnivia_core.graph.search_models",
        },
        "incidental_children": {},
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.graph",
                "omnivia_core.graph.models",
                "omnivia_core.graph.search_models",
            }
        ),
        "approved_leaves": frozenset(
            {"omnivia_core.graph.models", "omnivia_core.graph.search_models"}
        ),
        "order_cases": (
            ("barrel-first", ("omnivia_core.graph",)),
            (
                "models-first",
                ("omnivia_core.graph.models", "omnivia_core.graph.search_models", "omnivia_core.graph"),
            ),
            (
                "search-models-first",
                ("omnivia_core.graph.search_models", "omnivia_core.graph.models", "omnivia_core.graph"),
            ),
        ),
    },
    {
        "id": "ingestion",
        "canonical": "omnivia_core.ingestion",
        "legacy": "omnivia_memory.ingestion",
        "frozen_all": INGESTION_ALL,
        "owner_by_export": INGESTION_OWNER_BY_EXPORT,
        "runtime_exclusions": INGESTION_EXCLUSIONS,
        "forbidden_runtime_modules": INGESTION_FORBIDDEN_MODULES,
        "children": {"models": "omnivia_core.ingestion.models"},
        #: Importing ``omnivia_core.ingestion.watcher`` always binds a
        #: ``watcher`` attribute on the already-imported parent package as a
        #: side effect of ordinary Python import machinery -- that binding is
        #: not an export of this barrel. None of this barrel's own isolated
        #: order cases import watcher, so it must never appear there; the
        #: in-process namespace gate tolerates it appearing *only* because
        #: other, unrelated test modules in the same pytest process may have
        #: already imported ``omnivia_core.ingestion.watcher`` before this
        #: module's tests run.
        "incidental_children": {"watcher": "omnivia_core.ingestion.watcher"},
        "expected_closure": frozenset(
            {"omnivia_core", "omnivia_core.ingestion", "omnivia_core.ingestion.models"}
        ),
        "approved_leaves": frozenset({"omnivia_core.ingestion.models"}),
        "order_cases": (
            ("barrel-first", ("omnivia_core.ingestion",)),
            ("models-first", ("omnivia_core.ingestion.models", "omnivia_core.ingestion")),
        ),
    },
    {
        "id": "ingestion.watcher",
        "canonical": "omnivia_core.ingestion.watcher",
        "legacy": "omnivia_memory.ingestion.watcher",
        "frozen_all": WATCHER_ALL,
        "owner_by_export": WATCHER_OWNER_BY_EXPORT,
        "runtime_exclusions": WATCHER_EXCLUSIONS,
        "forbidden_runtime_modules": WATCHER_FORBIDDEN_MODULES,
        "children": {"models": "omnivia_core.ingestion.watcher.models"},
        "incidental_children": {},
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.ingestion",
                "omnivia_core.ingestion.models",
                "omnivia_core.ingestion.watcher",
                "omnivia_core.ingestion.watcher.models",
            }
        ),
        "approved_leaves": frozenset({"omnivia_core.ingestion.watcher.models"}),
        "order_cases": (
            ("watcher-first", ("omnivia_core.ingestion.watcher",)),
            (
                "parent-ingestion-then-watcher",
                ("omnivia_core.ingestion", "omnivia_core.ingestion.watcher"),
            ),
            (
                "watcher-models-then-watcher",
                ("omnivia_core.ingestion.watcher.models", "omnivia_core.ingestion.watcher"),
            ),
        ),
    },
    {
        "id": "memory",
        "canonical": "omnivia_core.memory",
        "legacy": "omnivia_memory.memory",
        "frozen_all": MEMORY_ALL,
        "owner_by_export": MEMORY_OWNER_BY_EXPORT,
        "runtime_exclusions": MEMORY_EXCLUSIONS,
        "forbidden_runtime_modules": MEMORY_FORBIDDEN_MODULES,
        "children": {"models": "omnivia_core.memory.models"},
        "incidental_children": {},
        "expected_closure": frozenset(
            {
                "omnivia_core",
                "omnivia_core.memory",
                "omnivia_core.memory.models",
                "omnivia_core.lifecycle",
                "omnivia_core.lifecycle.models",
                "omnivia_core.lifecycle.rules",
                "omnivia_core.provenance",
                "omnivia_core.provenance.models",
            }
        ),
        "approved_leaves": frozenset({"omnivia_core.memory.models"}),
        "order_cases": (
            ("barrel-first", ("omnivia_core.memory",)),
            (
                "dependency-order",
                (
                    "omnivia_core.lifecycle.models",
                    "omnivia_core.lifecycle.rules",
                    "omnivia_core.lifecycle",
                    "omnivia_core.provenance.models",
                    "omnivia_core.provenance",
                    "omnivia_core.memory.models",
                    "omnivia_core.memory",
                ),
            ),
            (
                "reverse-requested-order",
                (
                    "omnivia_core.memory.models",
                    "omnivia_core.provenance",
                    "omnivia_core.provenance.models",
                    "omnivia_core.lifecycle",
                    "omnivia_core.lifecycle.rules",
                    "omnivia_core.lifecycle.models",
                    "omnivia_core.memory",
                ),
            ),
        ),
    },
    {
        "id": "workspace",
        "canonical": "omnivia_core.workspace",
        "legacy": "omnivia_memory.workspace",
        "frozen_all": WORKSPACE_ALL,
        "owner_by_export": WORKSPACE_OWNER_BY_EXPORT,
        "runtime_exclusions": WORKSPACE_EXCLUSIONS,
        "forbidden_runtime_modules": WORKSPACE_FORBIDDEN_MODULES,
        "children": {"models": "omnivia_core.workspace.models"},
        # Phase 2 added two public workspace submodules. Like `ingestion.watcher`,
        # they become attributes of the barrel only once something imports them --
        # which nothing did until the Phase 2 suite ran in the same process, so the
        # default root suite stayed green while the combined run failed.
        "incidental_children": {
            "compatibility": "omnivia_core.workspace.compatibility",
            "manifest": "omnivia_core.workspace.manifest",
            # App Shell G2 Lane 1A adds the Workspace selection authority. Importing
            # `omnivia_core.workspace.lifecycle` binds the attribute on the barrel for
            # the same reason as the two Phase 2 submodules above.
            "lifecycle": "omnivia_core.workspace.lifecycle",
        },
        "expected_closure": frozenset(
            {"omnivia_core", "omnivia_core.workspace", "omnivia_core.workspace.models"}
        ),
        "approved_leaves": frozenset({"omnivia_core.workspace.models"}),
        "order_cases": (
            ("barrel-first", ("omnivia_core.workspace",)),
            ("models-first", ("omnivia_core.workspace.models", "omnivia_core.workspace")),
        ),
    },
)

#: Barrels whose live legacy ``__all__`` is asserted equal to the frozen
#: tuple with no filtering (no runtime-only exclusions exist for them).
PURE_LEGACY_BARRELS: tuple[dict[str, Any], ...] = tuple(
    barrel for barrel in BARRELS if not barrel["runtime_exclusions"]
)
#: Barrels whose live legacy ``__all__`` must be filtered for its documented
#: runtime-only exclusions (preserving legacy order) before comparison.
MIXED_LEGACY_BARRELS: tuple[dict[str, Any], ...] = tuple(
    barrel for barrel in BARRELS if barrel["runtime_exclusions"]
)


def _barrel_id(barrel: dict[str, Any]) -> str:
    return str(barrel["id"])


def _init_file_for(barrel: dict[str, Any]) -> Path:
    relative = Path(*str(barrel["canonical"]).split(".")[1:])
    return CORE_SRC / "omnivia_core" / relative / "__init__.py"


# ---------------------------------------------------------------------------
# Ordered __all__ oracles.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_matches_frozen_historical_tuple_exactly(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert tuple(canonical.__all__) == frozen_all, (
        f"{barrel['canonical']}.__all__ drifted from the frozen historical order:\n"
        f"canonical: {list(canonical.__all__)}\nfrozen: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_is_a_list_with_no_duplicates(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    assert isinstance(canonical.__all__, list), (
        f"{barrel['canonical']}.__all__ must be a list, got {type(canonical.__all__).__name__}"
    )
    assert len(canonical.__all__) == len(set(canonical.__all__)), (
        f"{barrel['canonical']}.__all__ contains duplicates: {canonical.__all__}"
    )


@pytest.mark.parametrize("barrel", PURE_LEGACY_BARRELS, ids=_barrel_id)
def test_live_legacy_all_matches_frozen_tuple_exactly_for_pure_barrels(
    barrel: dict[str, Any],
) -> None:
    legacy = importlib.import_module(barrel["legacy"])
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert isinstance(legacy.__all__, list), (
        f"{barrel['legacy']}.__all__ must be a list, got {type(legacy.__all__).__name__}"
    )
    assert tuple(legacy.__all__) == frozen_all, (
        f"live {barrel['legacy']}.__all__ drifted from the frozen historical order:\n"
        f"legacy: {list(legacy.__all__)}\nfrozen: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", MIXED_LEGACY_BARRELS, ids=_barrel_id)
def test_live_legacy_all_minus_exclusions_matches_frozen_tuple_preserving_order(
    barrel: dict[str, Any],
) -> None:
    """Legacy ``__all__``, with the documented runtime-only exclusions
    filtered out in place (preserving legacy order), must equal the frozen
    canonical tuple exactly -- not merely as a set."""
    legacy = importlib.import_module(barrel["legacy"])
    exclusions: frozenset[str] = barrel["runtime_exclusions"]
    filtered = tuple(name for name in legacy.__all__ if name not in exclusions)
    frozen_all: tuple[str, ...] = barrel["frozen_all"]
    assert filtered == frozen_all, (
        f"live {barrel['legacy']}.__all__ (minus runtime exclusions) drifted from the "
        f"canonical tuple:\nlegacy filtered: {list(filtered)}\ncanonical: {list(frozen_all)}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_all_membership_matches_inventory_plus_exclusions(barrel: dict[str, Any]) -> None:
    """The Phase 0 inventory sorts every barrel's ``all``, so it is a
    membership oracle only, never an order oracle -- confirm it really is
    sorted (so a future reader does not mistake it for an order oracle) and
    that its membership matches the frozen historical tuple plus the
    documented runtime-only exclusions (empty for lifecycle/provenance)."""
    inventory_all: list[str] = FROZEN_MODULES[barrel["legacy"]]["all"]
    assert inventory_all == sorted(inventory_all), (
        f"{barrel['legacy']} inventory 'all' is expected to be sorted by "
        "baseline.inventory; if this fails, the oracle's sorting behavior changed"
    )
    expected_membership = set(barrel["frozen_all"]) | set(barrel["runtime_exclusions"])
    assert expected_membership == set(inventory_all), (
        f"{barrel['canonical']} historical __all__ plus exclusions membership does not "
        f"match the frozen inventory: only in historical+exclusions="
        f"{sorted(expected_membership - set(inventory_all))}, "
        f"only in inventory={sorted(set(inventory_all) - expected_membership)}"
    )


# ---------------------------------------------------------------------------
# Owner identity, anchored to the frozen Phase 0 `defines` provenance.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_owner_map_covers_every_export_exactly(barrel: dict[str, Any]) -> None:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    assert set(owner_by_export) == set(barrel["frozen_all"]), (
        f"{barrel['id']} owner map does not cover the complete barrel: "
        f"missing={sorted(set(barrel['frozen_all']) - set(owner_by_export))}, "
        f"extra={sorted(set(owner_by_export) - set(barrel['frozen_all']))}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_owner_map_matches_frozen_leaf_defines_provenance(barrel: dict[str, Any]) -> None:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    for name, canonical_owner in owner_by_export.items():
        legacy_owner = canonical_owner.replace("omnivia_core", "omnivia_memory", 1)
        assert name in FROZEN_MODULES[legacy_owner]["defines"], (
            f"{name} is mapped to {canonical_owner}, but the frozen Phase 0 "
            f"inventory does not record {legacy_owner} as its defining leaf"
        )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_bindings_match_frozen_owner_map_by_identity(barrel: dict[str, Any]) -> None:
    """Every advertised name is the exact same object as its frozen owner
    binding, in both the canonical and the live legacy tree."""
    canonical = importlib.import_module(barrel["canonical"])
    legacy = importlib.import_module(barrel["legacy"])
    owner_by_export: dict[str, str] = barrel["owner_by_export"]

    for name in barrel["frozen_all"]:
        canonical_owner_name = owner_by_export[name]
        canonical_owner = importlib.import_module(canonical_owner_name)
        assert getattr(canonical, name) is getattr(canonical_owner, name), (
            f"{barrel['canonical']}.{name} is not identical to its frozen owner "
            f"{canonical_owner_name}.{name}"
        )

        legacy_owner_name = canonical_owner_name.replace("omnivia_core", "omnivia_memory", 1)
        legacy_owner = importlib.import_module(legacy_owner_name)
        assert getattr(legacy, name) is getattr(legacy_owner, name), (
            f"{barrel['legacy']}.{name} is not identical to its live owner "
            f"{legacy_owner_name}.{name}"
        )


# ---------------------------------------------------------------------------
# Star import surface, and no __getattr__/__dir__ lazy escape hatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_star_import_exposes_exactly_advertised_names(barrel: dict[str, Any]) -> None:
    namespace: dict[str, object] = {}
    exec(f"from {barrel['canonical']} import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(barrel["frozen_all"]), (
        f"star import of {barrel['canonical']} exposed {sorted(exported)}, "
        f"expected exactly {sorted(barrel['frozen_all'])}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_has_no_getattr_or_dir_escape_hatch(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    module_vars = vars(canonical)
    assert "__getattr__" not in module_vars, (
        f"{barrel['canonical']} must not define module __getattr__ -- every "
        "export must be a real, statically-visible binding"
    )
    assert "__dir__" not in module_vars, (
        f"{barrel['canonical']} must not define a custom module __dir__ -- "
        "no lazy/dynamic export surface"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_namespace_matches_expected_names_exactly(barrel: dict[str, Any]) -> None:
    """The public non-dunder namespace is exactly the exports plus
    ``annotations`` and the barrel's child owner-leaf modules -- nothing else
    is exempted as an arbitrary module-valued name. The one documented
    exception is ``omnivia_core.ingestion`` gaining an incidental ``watcher``
    attribute once ``omnivia_core.ingestion.watcher`` has been imported by
    anything in the same process (ordinary Python import-machinery behavior,
    not an export of this barrel); that name is only ever tolerated here, and
    only when it is identical to the real watcher module.
    """
    canonical = importlib.import_module(barrel["canonical"])
    module_vars = vars(canonical)
    children: dict[str, str] = barrel["children"]
    incidental_children: dict[str, str] = barrel["incidental_children"]

    expected_namespace = set(barrel["frozen_all"]) | {"annotations"} | set(children)
    actual_namespace = {name for name in module_vars if not name.startswith("__")}

    missing = expected_namespace - actual_namespace
    assert not missing, f"{barrel['canonical']} public namespace is missing: {sorted(missing)}"

    extra = actual_namespace - expected_namespace
    unaccounted_extra = extra - set(incidental_children)
    assert not unaccounted_extra, (
        f"{barrel['canonical']} public namespace drifted with unexplained extra names: "
        f"{sorted(unaccounted_extra)}"
    )
    for name in extra:
        expected_module = importlib.import_module(incidental_children[name])
        assert module_vars[name] is expected_module, (
            f"{barrel['canonical']}.{name} is not identical to {incidental_children[name]}"
        )

    assert module_vars["annotations"] is __future__.annotations
    for child_name, child_module_name in children.items():
        assert module_vars[child_name] is importlib.import_module(child_module_name), (
            f"{barrel['canonical']}.{child_name} is not identical to {child_module_name}"
        )


# ---------------------------------------------------------------------------
# Exact runtime exclusions: absent from __all__ and attributes, and the
# named runtime leaf modules must never load.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_runtime_exclusions_absent_from_all_and_attributes(barrel: dict[str, Any]) -> None:
    canonical = importlib.import_module(barrel["canonical"])
    for name in barrel["runtime_exclusions"]:
        assert name not in canonical.__all__, (
            f"{name} is a runtime-only export and must not be in {barrel['canonical']}.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is a runtime-only export and must not be an attribute of {barrel['canonical']}"
        )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_import_does_not_load_forbidden_runtime_modules(barrel: dict[str, Any]) -> None:
    importlib.import_module(barrel["canonical"])
    for forbidden_module in barrel["forbidden_runtime_modules"]:
        assert forbidden_module not in sys.modules, (
            f"importing {barrel['canonical']} must never load {forbidden_module}"
        )


# ---------------------------------------------------------------------------
# Static AST barrel-shape gate: module docstring, `from __future__ import
# annotations`, direct absolute imports from approved owner leaves only, and
# exactly one literal-list `__all__` assignment -- nothing else.
# ---------------------------------------------------------------------------


def _assert_pure_contract_barrel_shape(barrel: dict[str, Any]) -> None:
    init_file = _init_file_for(barrel)
    tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    body = tree.body
    canonical_name = barrel["canonical"]

    assert ast.get_docstring(tree) is not None, f"{canonical_name} barrel is missing a module docstring"
    index = 1 if (body and isinstance(body[0], ast.Expr)) else 0

    assert index < len(body), f"{canonical_name} barrel has no statements after its docstring"
    future_import = body[index]
    assert isinstance(future_import, ast.ImportFrom), (
        f"{canonical_name} barrel's first non-docstring statement must be "
        "`from __future__ import annotations`"
    )
    assert future_import.module == "__future__" and future_import.level == 0, (
        f"{canonical_name} barrel's first non-docstring statement must import from __future__"
    )
    assert {alias.name for alias in future_import.names} == {"annotations"}, (
        f"{canonical_name} barrel must import exactly `annotations` from __future__"
    )
    index += 1

    approved_leaves: frozenset[str] = barrel["approved_leaves"]
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    expected_routes: dict[str, set[str]] = {}
    for export_name, owner_module in owner_by_export.items():
        expected_routes.setdefault(owner_module, set()).add(export_name)

    actual_routes: dict[str, set[str]] = {}
    imported_names: set[str] = set()
    saw_all_assignment = False
    all_literal_values: list[str] = []

    for statement in body[index:]:
        assert not saw_all_assignment, (
            f"{canonical_name} barrel has a statement after its __all__ assignment: "
            f"{ast.dump(statement)}"
        )
        if isinstance(statement, ast.ImportFrom):
            assert statement.level == 0, (
                f"{canonical_name} barrel must use direct absolute imports, not relative "
                f"(level={statement.level})"
            )
            assert statement.module in approved_leaves, (
                f"{canonical_name} barrel imports from unapproved module {statement.module!r}; "
                f"approved owner leaves are {sorted(approved_leaves)}"
            )
            module_bucket = actual_routes.setdefault(statement.module, set())
            for alias in statement.names:
                assert alias.name != "*", f"{canonical_name} barrel must not use a wildcard import"
                assert alias.asname is None, (
                    f"{canonical_name} barrel must not alias imports "
                    f"({alias.name} as {alias.asname})"
                )
                assert alias.name not in imported_names, (
                    f"{canonical_name} barrel imports {alias.name!r} more than once"
                )
                imported_names.add(alias.name)
                module_bucket.add(alias.name)
        elif isinstance(statement, ast.Assign):
            assert len(statement.targets) == 1, (
                f"{canonical_name} barrel's __all__ assignment must have exactly one target"
            )
            target = statement.targets[0]
            assert isinstance(target, ast.Name) and target.id == "__all__", (
                f"{canonical_name} barrel must not have any module-level assignment "
                f"other than __all__: {ast.dump(statement)}"
            )
            assert isinstance(statement.value, ast.List), (
                f"{canonical_name} barrel's __all__ must be a literal list, "
                f"not {type(statement.value).__name__}"
            )
            for element in statement.value.elts:
                assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                    f"{canonical_name} barrel's __all__ list must contain only string literals"
                )
                all_literal_values.append(element.value)
            saw_all_assignment = True
        else:
            pytest.fail(
                f"{canonical_name} barrel contains a forbidden statement kind "
                f"{type(statement).__name__} (only absolute leaf imports and the __all__ "
                f"assignment are allowed -- no functions, classes, other assignments, "
                f"__getattr__, __dir__, wildcard/relative/dynamic imports, or package-barrel "
                f"imports): {ast.dump(statement)}"
            )

    assert saw_all_assignment, f"{canonical_name} barrel is missing its __all__ assignment"
    assert imported_names == set(all_literal_values), (
        f"{canonical_name} barrel's imported names {sorted(imported_names)} do not exactly "
        f"match its __all__ literal {sorted(all_literal_values)}"
    )
    assert actual_routes == expected_routes, (
        f"{canonical_name} barrel's actual import routes do not exactly match the frozen "
        f"owner map: actual={ {m: sorted(n) for m, n in actual_routes.items()} }, "
        f"expected={ {m: sorted(n) for m, n in expected_routes.items()} }"
    )
    assert tuple(all_literal_values) == barrel["frozen_all"], (
        f"{canonical_name} barrel's literal __all__ source order {all_literal_values} drifted "
        f"from the frozen historical tuple {list(barrel['frozen_all'])}"
    )


@pytest.mark.parametrize("barrel", BARRELS, ids=_barrel_id)
def test_barrel_source_matches_pure_contract_ast_shape(barrel: dict[str, Any]) -> None:
    _assert_pure_contract_barrel_shape(barrel)


# ---------------------------------------------------------------------------
# Isolated imports under `python -I -S`, only checkout src on sys.path, a
# cleared environment, barrel-first plus the applicable leaf-first/reverse
# orders. Every case reasserts tuple, owner identity, namespace, exclusions,
# forbidden-module absence, and the exact canonical module closure.
# ---------------------------------------------------------------------------


def _isolated_barrel_import_script(
    barrel: dict[str, Any],
    import_order: tuple[str, ...],
    assert_ingestion_gains_watcher_attr: bool,
) -> str:
    owner_by_export: dict[str, str] = barrel["owner_by_export"]
    expected_all: tuple[str, ...] = barrel["frozen_all"]
    expected_closure: frozenset[str] = barrel["expected_closure"]
    runtime_exclusions: frozenset[str] = barrel["runtime_exclusions"]
    forbidden_runtime_modules: tuple[str, ...] = barrel["forbidden_runtime_modules"]
    children: dict[str, str] = barrel["children"]
    incidental_children: dict[str, str] = barrel["incidental_children"]
    expected_namespace = set(expected_all) | {"annotations"} | set(children)

    lines: list[str] = [
        "import sys",
        "import pathlib",
        "import importlib",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        f"for module_name in {import_order!r}:",
        "    importlib.import_module(module_name)",
        f'barrel = sys.modules[{barrel["canonical"]!r}]',
        f"expected_all = {expected_all!r}",
        "if tuple(barrel.__all__) != expected_all:",
        '    raise SystemExit(f"__all__ drifted: {tuple(barrel.__all__)!r}")',
        f"owner_by_export = {owner_by_export!r}",
        "for export_name in expected_all:",
        "    owner_module = importlib.import_module(owner_by_export[export_name])",
        "    if getattr(barrel, export_name) is not getattr(owner_module, export_name):",
        '        raise SystemExit(f"{export_name} is not identical to its canonical owner")',
        f"children = {children!r}",
        "for child_name, child_module_name in children.items():",
        "    child_module = importlib.import_module(child_module_name)",
        "    if getattr(barrel, child_name, None) is not child_module:",
        '        raise SystemExit(f"{child_name} is not identical to {child_module_name}")',
        f"expected_namespace = {expected_namespace!r}",
        f"incidental_children = {incidental_children!r}",
        'actual_namespace = {name for name in vars(barrel) if not name.startswith("__")}',
        "extra = actual_namespace - expected_namespace",
        "unaccounted_extra = extra - set(incidental_children)",
        "if actual_namespace != expected_namespace:",
        '    if unaccounted_extra or (expected_namespace - actual_namespace):',
        '        raise SystemExit(f"namespace drifted: {sorted(actual_namespace)}")',
        f"runtime_exclusions = {sorted(runtime_exclusions)!r}",
        "for excluded_name in runtime_exclusions:",
        "    if excluded_name in barrel.__all__ or hasattr(barrel, excluded_name):",
        '        raise SystemExit(f"runtime exclusion {excluded_name} leaked into the barrel")',
        'if "__getattr__" in vars(barrel):',
        '    raise SystemExit("barrel must not define __getattr__")',
        'if "__dir__" in vars(barrel):',
        '    raise SystemExit("barrel must not define __dir__")',
        f"forbidden_runtime_modules = {forbidden_runtime_modules!r}",
        "for forbidden_module in forbidden_runtime_modules:",
        "    if forbidden_module in sys.modules:",
        '        raise SystemExit(f"forbidden runtime module {forbidden_module} was loaded")',
        f"core_src = pathlib.Path({str(CORE_SRC)!r}).resolve()",
        "outside = []",
        "for name, module in sorted(sys.modules.items()):",
        '    if name != "omnivia_core" and not name.startswith("omnivia_core."):',
        "        continue",
        '    path = getattr(module, "__file__", None)',
        "    if path is None:",
        "        continue",
        "    resolved = pathlib.Path(path).resolve()",
        "    try:",
        "        resolved.relative_to(core_src)",
        "    except ValueError:",
        '        outside.append(f"{name} -> {resolved}")',
        "if outside:",
        '    raise SystemExit("omnivia_core modules loaded from outside src: " + ", ".join(outside))',
        f"expected_core = {expected_closure!r}",
        'loaded_core = {name for name in sys.modules if name == "omnivia_core" or name.startswith("omnivia_core.")}',
        "if loaded_core != expected_core:",
        '    raise SystemExit(f"unexpected canonical module closure: {sorted(loaded_core)}")',
        f"forbidden_roots = {sorted(FORBIDDEN_LEAKAGE_MODULES)!r}",
        'leaked = sorted(set(forbidden_roots) & {m.split(".")[0] for m in sys.modules})',
        "if leaked:",
        '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
    ]
    if assert_ingestion_gains_watcher_attr:
        lines += [
            'ingestion_module = sys.modules["omnivia_core.ingestion"]',
            'watcher_module = sys.modules["omnivia_core.ingestion.watcher"]',
            'if getattr(ingestion_module, "watcher", None) is not watcher_module:',
            '    raise SystemExit("parent omnivia_core.ingestion did not gain the incidental watcher attribute")',
        ]
    lines.append('print("OK")')
    return "\n".join(lines)


_ISOLATED_CASES: tuple[tuple[dict[str, Any], str, tuple[str, ...]], ...] = tuple(
    (barrel, case_id, import_order)
    for barrel in BARRELS
    for case_id, import_order in barrel["order_cases"]
)


def _isolated_case_id(case: tuple[dict[str, Any], str, tuple[str, ...]]) -> str:
    barrel, case_id, _import_order = case
    return f"{barrel['id']}:{case_id}"


@pytest.mark.parametrize("case", _ISOLATED_CASES, ids=_isolated_case_id)
def test_barrel_imports_in_isolation_from_src_alone(
    case: tuple[dict[str, Any], str, tuple[str, ...]],
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"
    barrel, case_id, import_order = case

    script = _isolated_barrel_import_script(
        barrel,
        import_order,
        assert_ingestion_gains_watcher_attr=(
            barrel["id"] == "ingestion.watcher" and case_id == "parent-ingestion-then-watcher"
        ),
    )
    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", script],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of {barrel['canonical']} ({case_id}) failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
