"""Derived read models built from authoritative storage, and nothing else.

A projection in this package owns no truth. Every row it holds is reproducible from
the tables 0008 and 0009 already carry, which is what makes a projection safe to
delete and rebuild and what makes `omnivia_projection_ledger.rebuildable` a fact
rather than an aspiration.

One module today: `fts`, the `evidence.search` FTS5 projection. The lifecycle it
drives -- runs, checkpoints, validation, activation -- is migration 0011's and is not
re-implemented here; this package supplies the *content* those runs are about.
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
