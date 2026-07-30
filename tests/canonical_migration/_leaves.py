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
CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core.knowledge.models": "omnivia_memory.knowledge.models",
    "omnivia_core.knowledge.normalize": "omnivia_memory.knowledge.normalize",
    "omnivia_core.knowledge.validation": "omnivia_memory.knowledge.validation",
    "omnivia_core.control_plane.imports": "omnivia_memory.control_plane.imports",
    "omnivia_core.control_plane.models": "omnivia_memory.control_plane.models",
    "omnivia_core.control_plane.validation": "omnivia_memory.control_plane.validation",
    "omnivia_core.memory_graph.assembly": "omnivia_memory.memory_graph.assembly",
    "omnivia_core.memory_graph.fixtures": "omnivia_memory.memory_graph.fixtures",
    "omnivia_core.memory_graph.models": "omnivia_memory.memory_graph.models",
    "omnivia_core.memory_graph.validation": "omnivia_memory.memory_graph.validation",
    "omnivia_core.graph.models": "omnivia_memory.graph.models",
    "omnivia_core.ingestion.models": "omnivia_memory.ingestion.models",
    "omnivia_core.ingestion.watcher.models": "omnivia_memory.ingestion.watcher.models",
    "omnivia_core.workspace.models": "omnivia_memory.workspace.models",
    "omnivia_core.run_ledger.models": "omnivia_memory.run_ledger.models",
    "omnivia_core.run_ledger.validation": "omnivia_memory.run_ledger.validation",
}

#: canonical module -> matching legacy module, for the modules where the
#: legacy leaf has been converted into a thin compatibility facade that
#: routes its supported symbols to the exact canonical object (``legacy.Foo
#: is canonical.Foo``) rather than holding a duplicated, source-parity copy.
#: These leaves deliberately fail the source-parity oracle ``CANONICAL_TO_LEGACY``
#: exists to enforce -- a facade's source is an import, not a port -- so they
#: are covered instead by tests/compatibility/test_facade_foundation.py, which
#: asserts symbol identity rather than source-level sameness.
FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core._shared.validation": "omnivia_memory._shared.validation",
    "omnivia_core.app_manifest.models": "omnivia_memory.app_manifest.models",
    "omnivia_core.app_manifest.validation": "omnivia_memory.app_manifest.validation",
    "omnivia_core.app_shell_bridge.models": "omnivia_memory.app_shell_bridge.models",
    "omnivia_core.app_shell_bridge.validation": "omnivia_memory.app_shell_bridge.validation",
    "omnivia_core.component_contract.models": "omnivia_memory.component_contract.models",
    "omnivia_core.component_contract.validation": "omnivia_memory.component_contract.validation",
    "omnivia_core.lifecycle.models": "omnivia_memory.lifecycle.models",
    "omnivia_core.lifecycle.rules": "omnivia_memory.lifecycle.rules",
    "omnivia_core.module_manifest.models": "omnivia_memory.module_manifest.models",
    "omnivia_core.module_manifest.validation": "omnivia_memory.module_manifest.validation",
    "omnivia_core.provenance.models": "omnivia_memory.provenance.models",
    "omnivia_core.memory.models": "omnivia_memory.memory.models",
}

#: omnivia_core.graph.search_models canonicalizes only the query/result record
#: definitions. The relevance-scoring helpers stay runtime-owned and must not
#: enter Core.
SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL: frozenset[str] = frozenset(
    {
        "score_name_match",
        "score_relationship_count",
        "score_neighbor_overlap",
        "compute_relevance_score",
    }
)
