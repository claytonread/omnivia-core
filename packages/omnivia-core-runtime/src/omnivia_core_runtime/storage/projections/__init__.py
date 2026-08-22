"""Derived read models built from authoritative storage, and nothing else.

A projection in this package owns no truth. Every row it holds is reproducible from
the authoritative tables named by its module, which is what makes a projection safe
to delete and rebuild and what makes a rebuildable claim a fact rather than an
aspiration.

Two projection shapes live here. `fts` is the independently rebuilt and atomically
activated `evidence.search` snapshot whose lifecycle migration 0011 owns.
`runtime_run_summary` is transactional materialised state: it advances in the same
fenced transaction as the canonical RuntimeEvent and uses a per-Run cursor, so giving
it a second activation pointer would make the live status lag by construction.
"""

from __future__ import annotations

from typing import Final

#: The ledger identity of the `evidence.search` FTS5 projection.
#:
#: It is stated here because two modules that cannot import each other both need it:
#: `storage/repository.py` names it as a contributing projection so the freshness gate
#: covers it, and `storage/projections/fts.py` declares, builds, activates and reads
#: under it. `fts` imports `repository`, so the constant cannot live in either without
#: a cycle -- and a second literal is precisely how a gate and a builder come to
#: disagree about which projection a refusal is even about.
EVIDENCE_SEARCH_PROJECTION_ID: Final = "evidence.search.fts5"

__all__ = ["EVIDENCE_SEARCH_PROJECTION_ID"]
