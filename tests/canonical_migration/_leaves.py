"""Shared manifest of canonical leaves and their legacy counterparts.

Phase 1 ports portable models, helpers, and validators from
``services/omnivia-memory`` into ``src/omnivia_core`` leaves, rewriting
internal imports to canonical targets. This module is the single source of
truth the canonical_migration tests share for which canonical module maps to
which legacy module, and for the small set of deliberate, documented
divergences between them.
"""

from __future__ import annotations

#: Every canonical leaf module must be able to import from a fresh
#: process using only ``src`` on the path (no ``omnivia_memory`` present).
CANONICAL_LEAF_MODULES: tuple[str, ...] = (
    "omnivia_core._shared",
    "omnivia_core._shared.validation",
    "omnivia_core.knowledge.models",
    "omnivia_core.knowledge.normalize",
    "omnivia_core.knowledge.validation",
    "omnivia_core.app_manifest.models",
    "omnivia_core.app_manifest.validation",
    "omnivia_core.app_shell_bridge.models",
    "omnivia_core.app_shell_bridge.validation",
    "omnivia_core.component_contract.models",
    "omnivia_core.component_contract.validation",
    "omnivia_core.control_plane.imports",
    "omnivia_core.control_plane.models",
    "omnivia_core.control_plane.validation",
    "omnivia_core.lifecycle.models",
    "omnivia_core.lifecycle.rules",
    "omnivia_core.memory_graph.assembly",
    "omnivia_core.memory_graph.fixtures",
    "omnivia_core.memory_graph.models",
    "omnivia_core.memory_graph.validation",
    "omnivia_core.module_manifest.models",
    "omnivia_core.module_manifest.validation",
    "omnivia_core.provenance.models",
    "omnivia_core.graph.models",
    "omnivia_core.graph.search_models",
    "omnivia_core.ingestion.models",
    "omnivia_core.ingestion.watcher.models",
    "omnivia_core.memory.models",
    "omnivia_core.workspace.models",
    "omnivia_core.run_ledger.models",
    "omnivia_core.run_ledger.validation",
)

#: canonical module -> matching legacy module, for the modules that are a
#: direct 1:1 port (compared symbol-for-symbol by test_parity.py).
#:
#: Empty on purpose: ``workspace.models`` was the last duplicated leaf, and it is
#: now a compatibility facade in ``FACADE_CANONICAL_TO_LEGACY`` below. The map
#: stays (rather than being deleted) because the source-parity oracle it drives
#: is still the policy for any leaf a later batch ports before converting it, and
#: because ``test_ast_gate_covers_every_leaf_but_the_declared_split_facades``
#: keeps asserting that the three maps partition every canonical leaf.
CANONICAL_TO_LEGACY: dict[str, str] = {}

#: canonical module -> matching legacy module, for the modules where the
#: legacy leaf has been converted into a thin compatibility facade that
#: routes its supported symbols to the exact canonical object (``legacy.Foo
#: is canonical.Foo``) rather than holding a duplicated, source-parity copy.
#: These leaves deliberately fail the source-parity oracle ``CANONICAL_TO_LEGACY``
#: exists to enforce -- a facade's source is an import, not a port -- so they
#: are covered instead by tests/compatibility/test_facade_foundation.py, which
#: asserts symbol identity rather than source-level sameness.
#:
#: A converted leaf's *barrel* is not necessarily converted with it: the four
#: ``memory_graph`` leaves, ``graph.models``, ``ingestion.models``,
#: ``ingestion.watcher.models`` and ``workspace.models`` are facades while
#: ``omnivia_memory.memory_graph``, ``omnivia_memory.graph``,
#: ``omnivia_memory.ingestion``, ``omnivia_memory.ingestion.watcher`` and
#: ``omnivia_memory.workspace`` stay hybrid barrels, because some of their
#: exports are owned by runtime-only leaves (``ingestion_adapter``/``store``,
#: ``search_service``, the ingestion chunker/extractor/pipeline/repository/scanner
#: set, the watcher's ``debouncer``/``tracker``, and the workspace
#: ``repository``/``service``) that never enter Core.
#:
#: A leaf that keeps *some* definitions of its own is a ``split_facade`` and lives
#: in ``SPLIT_FACADE_CANONICAL_TO_LEGACY`` below instead, not here: this map is
#: for leaves whose whole body is a single re-export.
FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core._shared.validation": "omnivia_memory._shared.validation",
    "omnivia_core.app_manifest.models": "omnivia_memory.app_manifest.models",
    "omnivia_core.app_manifest.validation": "omnivia_memory.app_manifest.validation",
    "omnivia_core.app_shell_bridge.models": "omnivia_memory.app_shell_bridge.models",
    "omnivia_core.app_shell_bridge.validation": "omnivia_memory.app_shell_bridge.validation",
    "omnivia_core.component_contract.models": "omnivia_memory.component_contract.models",
    "omnivia_core.component_contract.validation": "omnivia_memory.component_contract.validation",
    "omnivia_core.control_plane.imports": "omnivia_memory.control_plane.imports",
    "omnivia_core.control_plane.models": "omnivia_memory.control_plane.models",
    "omnivia_core.control_plane.validation": "omnivia_memory.control_plane.validation",
    "omnivia_core.graph.models": "omnivia_memory.graph.models",
    "omnivia_core.ingestion.models": "omnivia_memory.ingestion.models",
    "omnivia_core.ingestion.watcher.models": (
        "omnivia_memory.ingestion.watcher.models"
    ),
    "omnivia_core.knowledge.models": "omnivia_memory.knowledge.models",
    "omnivia_core.knowledge.normalize": "omnivia_memory.knowledge.normalize",
    "omnivia_core.knowledge.validation": "omnivia_memory.knowledge.validation",
    "omnivia_core.lifecycle.models": "omnivia_memory.lifecycle.models",
    "omnivia_core.lifecycle.rules": "omnivia_memory.lifecycle.rules",
    "omnivia_core.module_manifest.models": "omnivia_memory.module_manifest.models",
    "omnivia_core.module_manifest.validation": "omnivia_memory.module_manifest.validation",
    "omnivia_core.provenance.models": "omnivia_memory.provenance.models",
    "omnivia_core.memory.models": "omnivia_memory.memory.models",
    "omnivia_core.memory_graph.assembly": "omnivia_memory.memory_graph.assembly",
    "omnivia_core.memory_graph.fixtures": "omnivia_memory.memory_graph.fixtures",
    "omnivia_core.memory_graph.models": "omnivia_memory.memory_graph.models",
    "omnivia_core.memory_graph.validation": "omnivia_memory.memory_graph.validation",
    "omnivia_core.run_ledger.models": "omnivia_memory.run_ledger.models",
    "omnivia_core.run_ledger.validation": "omnivia_memory.run_ledger.validation",
    "omnivia_core.workspace.models": "omnivia_memory.workspace.models",
}

#: canonical module -> matching legacy module, for the leaves converted into a
#: *split* compatibility facade: the legacy leaf routes its whole portable
#: namespace to the exact canonical objects, exactly as a facade in
#: ``FACADE_CANONICAL_TO_LEGACY`` does, but additionally keeps a named set of
#: synchronous function definitions that canonical Core deliberately excludes
#: (``SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL`` below).
#:
#: These leaves are held out of both maps above for the same reason: a facade's
#: source is an import, not a port, so the source-parity oracle
#: ``CANONICAL_TO_LEGACY`` exists to enforce cannot apply -- and their body is not
#: a *single* import either, so the pure-facade gates keyed on
#: ``FACADE_CANONICAL_TO_LEGACY`` cannot apply. They are covered instead by
#: tests/compatibility/test_facade_foundation.py, which pins the exact split
#: source shape, the portable half's symbol identity, and the retained half's
#: ownership, signatures and behavior.
SPLIT_FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core.graph.search_models": "omnivia_memory.graph.search_models",
}

#: omnivia_core.graph.search_models canonicalizes only the query/result record
#: definitions. The relevance-scoring helpers stay runtime-owned and must not
#: enter Core: they are the retained, legacy-owned half of that leaf's split
#: facade, still called by ``omnivia_memory.graph.search_service``.
SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL: frozenset[str] = frozenset(
    {
        "score_name_match",
        "score_relationship_count",
        "score_neighbor_overlap",
        "compute_relevance_score",
    }
)
