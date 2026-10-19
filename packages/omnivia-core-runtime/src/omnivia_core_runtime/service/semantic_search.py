"""Internal hybrid composition over one frozen authorized frontier.

This is intentionally not an application handler, contract operation or transport. A
future caller resolves and freezes authority with the existing evidence-search seam,
projects the lexical lane from that same frontier, then calls this module with both
values. The module never authorizes, widens or re-reads the candidate set, never loads a
model, and does not change the independently available lexical ``evidence.search``.

**One frontier, proven.** The lexical ``ProjectedFrontier`` must be the frozen
``AuthorizedFrontier`` it claims to be: same workspace, same checksum, the checksum
recomputed from the frontier's own members, and the same members in the same order. The
lexical lane is ranked here by ``rank_projected``; a caller cannot hand in a ranking.

**Lane status is typed, never inferred from emptiness.** Each requested lane reports
``ok``, ``no_match`` or ``unavailable`` with a stable reason. ``semantic`` mode never
returns lexical hits; ``combined`` with an unavailable semantic lane returns the lexical
results and status ``partial``. An empty query or an empty frontier is a deterministic
``no_match`` for every requested lane and reads no semantic state.

Only the known semantic-state refusals map to a reason. A storage error, a malformed
query vector or a programming error propagates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import compute_authorized_candidate_set_checksum
from omnivia_core_runtime.storage.projections.semantic import (
    CorruptSemanticState,
    IncompatibleSemanticGeneration,
    IncompleteSemanticCoverage,
    SemanticGeneration,
    SemanticProjectionError,
    SemanticProjectionUnavailable,
    StaleSemanticProjection,
    current_generation,
    project_semantic,
)
from omnivia_core_runtime.storage.retrieval import (
    AuthorizedFrontier,
    EvidenceCandidate,
    ProjectedFrontier,
    candidate_set_manifest,
    normalize_query,
    rank_projected,
)
from omnivia_core_runtime.storage.semantic_vectors import (
    ExecutionProvenance,
    FusedHit,
    SemanticEmbeddingProfile,
    SemanticHit,
    SemanticRetrievalPolicy,
    decide_compatibility,
    decode_vector,
    fuse_ranked,
    rank_semantic,
)

MODE_KEYWORD: Final = "keyword"
MODE_SEMANTIC: Final = "semantic"
MODE_COMBINED: Final = "combined"
MODES: Final[tuple[str, ...]] = (MODE_KEYWORD, MODE_SEMANTIC, MODE_COMBINED)

LANE_OK: Final = "ok"
LANE_NO_MATCH: Final = "no_match"
LANE_UNAVAILABLE: Final = "unavailable"

STATUS_COMPLETE: Final = "complete"
STATUS_PARTIAL: Final = "partial"
STATUS_UNAVAILABLE: Final = "unavailable"

REASON_QUERY_NOT_EMBEDDED: Final = "query_not_embedded"
REASON_NO_ACTIVE_GENERATION: Final = "no_active_generation"
REASON_STALE_GENERATION: Final = "stale_generation"
REASON_INCOMPATIBLE_PROFILE: Final = "incompatible_profile"
REASON_INCOMPATIBLE_EXECUTION: Final = "incompatible_execution"
REASON_INCOMPLETE_COVERAGE: Final = "incomplete_coverage"
REASON_CORRUPT_STATE: Final = "corrupt_state"

#: The known semantic-state refusals. Any other `SemanticProjectionError` propagates.
_REASONS: Final[tuple[tuple[type[SemanticProjectionError], str], ...]] = (
    (SemanticProjectionUnavailable, REASON_NO_ACTIVE_GENERATION),
    (StaleSemanticProjection, REASON_STALE_GENERATION),
    (IncompatibleSemanticGeneration, REASON_INCOMPATIBLE_EXECUTION),
    (IncompleteSemanticCoverage, REASON_INCOMPLETE_COVERAGE),
    (CorruptSemanticState, REASON_CORRUPT_STATE),
)


@dataclass(frozen=True, slots=True)
class LaneOutcome:
    """One requested lane's result. `reason` is set exactly when it is unavailable."""

    status: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in (LANE_OK, LANE_NO_MATCH, LANE_UNAVAILABLE):
            raise ValueError(f"unknown lane status {self.status!r}")
        if (self.reason is not None) != (self.status == LANE_UNAVAILABLE):
            raise ValueError("a reason is required for, and only for, an unavailable lane")


@dataclass(frozen=True, slots=True)
class SemanticQuery:
    """A query already embedded elsewhere, with the identities it was embedded under."""

    vector: bytes
    profile: SemanticEmbeddingProfile
    execution: ExecutionProvenance


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    """The internal result. Not a wire shape.

    A lane that was not requested is `None`. `hits` is the `rrf.v1` fusion of the lanes
    that produced results, truncated to `policy.result_limit`. The generation and profile
    digests name the generation that completed the semantic lane. They are `None` when
    the lane is unavailable, even if a generation was inspected and refused.
    """

    mode: str
    status: str
    authorized_frontier_checksum: str
    policy_digest: str
    lexical: LaneOutcome | None
    semantic: LaneOutcome | None
    lexical_hits: tuple[EvidenceCandidate, ...]
    semantic_hits: tuple[SemanticHit, ...]
    hits: tuple[FusedHit, ...]
    generation_digest: str | None
    profile_digest: str | None


def search_authorized_frontier(
    connection: sqlite3.Connection,
    frontier: AuthorizedFrontier,
    lexical: ProjectedFrontier,
    query: str,
    *,
    mode: str,
    policy: SemanticRetrievalPolicy,
    semantic_query: SemanticQuery | None = None,
    generation: SemanticGeneration | None = None,
) -> SemanticSearchResult:
    """Rank the requested lanes over `frontier` and fuse them.

    `connection` is read only by the semantic lane, and only when it runs: keyword mode,
    an empty query and an empty frontier never touch it.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)!r}")
    _require_same_frontier(frontier, lexical)
    if mode != MODE_KEYWORD and semantic_query is not None:
        # Validate caller input independently of query text and generation availability.
        # A malformed vector is a contract error, never an index-state-dependent fallback.
        decode_vector(semantic_query.vector, dimensions=semantic_query.profile.dimensions)
    empty = not normalize_query(query).strip() or not frontier.candidates

    lexical_outcome: LaneOutcome | None = None
    lexical_hits: tuple[EvidenceCandidate, ...] = ()
    if mode != MODE_SEMANTIC:
        if not empty:
            lexical_hits = rank_projected(lexical, query, limit=policy.candidate_budget)
        lexical_outcome = LaneOutcome(LANE_OK if lexical_hits else LANE_NO_MATCH)

    semantic_outcome: LaneOutcome | None = None
    semantic_hits: tuple[SemanticHit, ...] = ()
    resolved: SemanticGeneration | None = None
    if mode != MODE_KEYWORD:
        if empty:
            semantic_outcome = LaneOutcome(LANE_NO_MATCH)
        else:
            semantic_outcome, semantic_hits, resolved = _semantic_lane(
                connection, frontier, semantic_query, policy, generation
            )

    if semantic_outcome is not None and semantic_outcome.status == LANE_UNAVAILABLE:
        status = STATUS_PARTIAL if lexical_outcome is not None else STATUS_UNAVAILABLE
    else:
        status = STATUS_COMPLETE
    return SemanticSearchResult(
        mode=mode,
        status=status,
        authorized_frontier_checksum=frontier.checksum,
        policy_digest=policy.digest,
        lexical=lexical_outcome,
        semantic=semantic_outcome,
        lexical_hits=lexical_hits,
        semantic_hits=semantic_hits,
        hits=fuse_ranked(
            lexical_hits, semantic_hits, version=policy.fusion_version, limit=policy.result_limit
        ),
        generation_digest=resolved.identity.digest if resolved is not None else None,
        profile_digest=resolved.profile.digest if resolved is not None else None,
    )


def _require_same_frontier(frontier: AuthorizedFrontier, lexical: ProjectedFrontier) -> None:
    recomputed = compute_authorized_candidate_set_checksum(
        candidate_set_manifest(frontier.workspace_id, frontier.candidates)
    )
    if (
        lexical.workspace_id != frontier.workspace_id
        or lexical.checksum != frontier.checksum
        or recomputed != frontier.checksum
        or tuple(item.candidate for item in lexical.candidates) != frontier.candidates
    ):
        raise ValueError("the lexical projection is not the authorized frontier it was given")


def _semantic_lane(
    connection: sqlite3.Connection,
    frontier: AuthorizedFrontier,
    query: SemanticQuery | None,
    policy: SemanticRetrievalPolicy,
    generation: SemanticGeneration | None,
) -> tuple[LaneOutcome, tuple[SemanticHit, ...], SemanticGeneration | None]:
    if query is None:
        return LaneOutcome(LANE_UNAVAILABLE, REASON_QUERY_NOT_EMBEDDED), (), None
    resolved = generation
    try:
        if resolved is None:
            resolved = current_generation(connection, workspace_id=frontier.workspace_id)
        if query.profile.digest != resolved.profile.digest:
            return LaneOutcome(LANE_UNAVAILABLE, REASON_INCOMPATIBLE_PROFILE), (), None
        if not decide_compatibility(resolved.execution, query.execution).compatible:
            return LaneOutcome(LANE_UNAVAILABLE, REASON_INCOMPATIBLE_EXECUTION), (), None
        projected = project_semantic(connection, resolved, frontier)
    except SemanticProjectionError as exc:
        reason = next((reason for kind, reason in _REASONS if isinstance(exc, kind)), None)
        if reason is None:
            raise
        return LaneOutcome(LANE_UNAVAILABLE, reason), (), None
    hits = rank_semantic(
        projected, query.vector, metric=resolved.representation.metric, policy=policy
    )
    return LaneOutcome(LANE_OK if hits else LANE_NO_MATCH), hits, resolved


__all__ = [
    "LANE_NO_MATCH",
    "LANE_OK",
    "LANE_UNAVAILABLE",
    "MODES",
    "MODE_COMBINED",
    "MODE_KEYWORD",
    "MODE_SEMANTIC",
    "REASON_CORRUPT_STATE",
    "REASON_INCOMPATIBLE_EXECUTION",
    "REASON_INCOMPATIBLE_PROFILE",
    "REASON_INCOMPLETE_COVERAGE",
    "REASON_NO_ACTIVE_GENERATION",
    "REASON_QUERY_NOT_EMBEDDED",
    "REASON_STALE_GENERATION",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "LaneOutcome",
    "SemanticQuery",
    "SemanticSearchResult",
    "search_authorized_frontier",
]
