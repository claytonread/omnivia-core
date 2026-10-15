"""Internal composition for exact semantic retrieval over an authorized frontier.

This is intentionally not an application handler. A future transport may resolve and
freeze authority using the existing evidence-search seam, then call this module. The
module never authorizes, widens, or re-reads the candidate set and does not change the
independently available lexical ``evidence.search`` operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from omnivia_core_runtime.storage.projections.semantic import (
    IncompatibleSemanticGeneration,
    SemanticGeneration,
    current_generation,
    project_semantic,
)
from omnivia_core_runtime.storage.retrieval import AuthorizedFrontier, EvidenceCandidate
from omnivia_core_runtime.storage.semantic_vectors import (
    ExecutionProvenance,
    FusedHit,
    SemanticEmbeddingProfile,
    SemanticHit,
    SemanticRetrievalPolicy,
    decide_compatibility,
    fuse_ranked,
    rank_semantic,
)


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    """Exact results and the authorization/generation identities they were derived from."""

    authorized_frontier_checksum: str
    generation_digest: str
    profile_digest: str
    semantic_hits: tuple[SemanticHit, ...]
    fused_hits: tuple[FusedHit, ...] | None


def search_authorized_frontier(
    connection: sqlite3.Connection,
    frontier: AuthorizedFrontier,
    query_vector: bytes,
    *,
    query_profile: SemanticEmbeddingProfile,
    query_execution: ExecutionProvenance,
    policy: SemanticRetrievalPolicy,
    lexical_ranking: Sequence[EvidenceCandidate] | None = None,
    generation: SemanticGeneration | None = None,
) -> SemanticSearchResult:
    """Rank every member of ``frontier`` semantically, optionally fusing lexical ranks.

    Missing, stale, incompatible, or incomplete semantic state is allowed to raise from
    the projection. Callers may continue to invoke lexical search independently; this
    function contains no lexical fallback that could blur lane status.
    """

    resolved = generation or current_generation(connection, workspace_id=frontier.workspace_id)
    if query_profile.digest != resolved.profile.digest:
        raise IncompatibleSemanticGeneration(
            "the query embedding profile differs from the activated generation"
        )
    compatibility = decide_compatibility(resolved.execution, query_execution)
    if not compatibility.compatible:
        raise IncompatibleSemanticGeneration(
            f"the query execution is incompatible with the activated generation: "
            f"{compatibility.reason}"
        )
    projected = project_semantic(connection, resolved, frontier)
    semantic_hits = rank_semantic(
        projected,
        query_vector,
        metric=resolved.representation.metric,
        policy=policy,
    )
    fused = None
    if lexical_ranking is not None:
        frontier_ids = {candidate.evidence_id for candidate in frontier.candidates}
        if any(candidate.evidence_id not in frontier_ids for candidate in lexical_ranking):
            raise ValueError("lexical ranking contains a candidate outside the authorized frontier")
        fused = fuse_ranked(
            lexical_ranking,
            semantic_hits,
            version=policy.fusion_version,
            limit=policy.candidate_budget,
        )
    return SemanticSearchResult(
        authorized_frontier_checksum=frontier.checksum,
        generation_digest=resolved.identity.digest,
        profile_digest=resolved.profile.digest,
        semantic_hits=semantic_hits,
        fused_hits=fused,
    )


__all__ = ["SemanticSearchResult", "search_authorized_frontier"]
