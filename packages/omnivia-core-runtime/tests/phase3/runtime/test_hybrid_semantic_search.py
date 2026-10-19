"""The internal hybrid seam over a real migrated workspace and a model-free fixture embedder.

The lexical `ProjectedFrontier` is built here the way `SearchProjection.project` builds it
(same frontier, verbatim checksum, the identity surface's projection tokens) so these
tests exercise lane composition without an FTS session. The FTS adapter itself is
covered by its own tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.service import semantic_search as hybrid
from omnivia_core_runtime.service.semantic_search import (
    SemanticQuery,
    SemanticSearchResult,
    search_authorized_frontier,
)
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import semantic
from omnivia_core_runtime.storage.repository import read_evidence_candidates
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    AuthorizedFrontier,
    ProjectedCandidate,
    ProjectedFrontier,
    authorized_frontier,
    local_owner_label_grant,
    query_tokens,
)
from omnivia_core_runtime.storage.semantic_vectors import (
    INCOMPATIBLE,
    CompatibilityDecision,
    SemanticRetrievalPolicy,
    SemanticVectorError,
    canonical_model_input,
    encode_vector,
    input_digest,
)
from test_semantic_projection_lifecycle import FixedEmbedder

WORKSPACE_ID = m2.WORKSPACE_ID
RESOLUTION_US = m2.BASE_US + 10_000_000

#: `evd-0001` is the labelled seed artifact: hidden from a non-owner principal, and the
#: exact semantic and lexical match for the probes below if it ever leaked.
VECTORS: dict[bytes, tuple[float, float]] = {
    b"doc-1": (1.0, 0.0),
    b"alpha-notes": (0.0, 1.0),
    b"beta-notes": (0.8, 0.6),
    b"gamma-notes": (0.6, 0.8),
}
QUERY = encode_vector((1.0, 0.0), dimensions=2)
POLICY = SemanticRetrievalPolicy(candidate_budget=10)


class MappedEmbedder(FixedEmbedder):
    def embed(self, canonical_inputs: Sequence[bytes]) -> Sequence[Sequence[float]]:
        return [
            next(vector for key, vector in VECTORS.items() if key in item)
            for item in canonical_inputs
        ]


def _surface(native_id: str) -> str:
    return input_digest(canonical_model_input(f"filesystem.archive {native_id} archive://doc.md"))


def _add_artifact(holder: m2.Owned, evidence_id: str, native_id: str, recorded_at_us: int) -> None:
    m2.write(
        holder,
        m2.EVIDENCE,
        evidence_id=evidence_id,
        source_native_id=native_id,
        blob_content_digest=m2.DIGEST_B,
        staged_source_ref=None,
        recorded_at_us=recorded_at_us,
    )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    m2.write(holder, m2.BLOBS, content_digest=m2.DIGEST_B)
    _add_artifact(holder, "evd-alpha", "alpha-notes", m2.BASE_US + 5)
    _add_artifact(holder, "evd-beta", "beta-notes", m2.BASE_US + 5)
    yield holder
    holder.connection.close()


def _build(holder: m2.Owned, embedder: FixedEmbedder | None = None) -> None:
    semantic.build_semantic_projection(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        embedder=embedder or MappedEmbedder(holder.connection),
        now_us=m2.BASE_US + 20_000_000,
    )


def _frontier(holder: m2.Owned, principal: str = CONFIGURED_LOCAL_OWNER) -> AuthorizedFrontier:
    return authorized_frontier(
        read_evidence_candidates(holder.connection, workspace_id=WORKSPACE_ID),
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=principal, workspace_id=WORKSPACE_ID, granted_workspace=WORKSPACE_ID
        ),
        resolution_time_us=RESOLUTION_US,
    )


def _lexical(frontier: AuthorizedFrontier) -> ProjectedFrontier:
    return ProjectedFrontier(
        workspace_id=frontier.workspace_id,
        checksum=frontier.checksum,
        candidates=tuple(
            ProjectedCandidate(candidate=candidate, terms=query_tokens(candidate.search_text))
            for candidate in frontier.candidates
        ),
    )


def _query(**changes: object) -> SemanticQuery:
    return replace(
        SemanticQuery(QUERY, FixedEmbedder.profile, FixedEmbedder.execution), **changes
    )


def _search(
    connection: sqlite3.Connection,
    frontier: AuthorizedFrontier,
    query: str,
    *,
    mode: str,
    semantic_query: SemanticQuery | None = None,
    policy: SemanticRetrievalPolicy = POLICY,
    generation: semantic.SemanticGeneration | None = None,
) -> SemanticSearchResult:
    return search_authorized_frontier(
        connection,
        frontier,
        _lexical(frontier),
        query,
        mode=mode,
        policy=policy,
        semantic_query=semantic_query if semantic_query is not None else _query(),
        generation=generation,
    )


def _ids(items: Sequence[Any]) -> list[str]:
    return [getattr(item, "candidate", item).evidence_id for item in items]


def _closed() -> sqlite3.Connection:
    """A connection any read would fail on: proof the path read no semantic state."""
    connection = sqlite3.connect(":memory:")
    connection.close()
    return connection


def test_keyword_mode_needs_no_model_and_no_semantic_state(owned: m2.Owned) -> None:
    frontier = _frontier(owned)
    result = search_authorized_frontier(
        _closed(), frontier, _lexical(frontier), "notes", mode=hybrid.MODE_KEYWORD, policy=POLICY
    )
    assert result.status == hybrid.STATUS_COMPLETE
    assert result.lexical == hybrid.LaneOutcome(hybrid.LANE_OK)
    assert result.semantic is None
    assert result.semantic_hits == ()
    assert result.generation_digest is None and result.profile_digest is None
    assert _ids(result.lexical_hits) == ["evd-alpha", "evd-beta"]
    assert _ids(result.hits) == ["evd-alpha", "evd-beta"]
    assert result.authorized_frontier_checksum == frontier.checksum
    assert result.policy_digest == POLICY.digest


def _assert_lexical_fallback(
    connection: sqlite3.Connection, frontier: AuthorizedFrontier, reason: str, **kwargs: object
) -> None:
    keyword = _search(_closed(), frontier, "notes", mode=hybrid.MODE_KEYWORD)
    combined = _search(connection, frontier, "notes", mode=hybrid.MODE_COMBINED, **kwargs)  # type: ignore[arg-type]
    assert combined.status == hybrid.STATUS_PARTIAL
    assert combined.semantic == hybrid.LaneOutcome(hybrid.LANE_UNAVAILABLE, reason)
    assert combined.lexical == hybrid.LaneOutcome(hybrid.LANE_OK)
    assert combined.semantic_hits == ()
    # Exactly the model-free keyword answer, not a degraded or reordered one.
    assert combined.lexical_hits == keyword.lexical_hits != ()
    assert combined.hits == keyword.hits
    assert all(hit.semantic_rank is None for hit in combined.hits)
    # An unavailable lane contributed no semantic result, so it publishes no generation
    # provenance as though that generation had served the request.
    assert combined.generation_digest is None and combined.profile_digest is None

    only = _search(connection, frontier, "notes", mode=hybrid.MODE_SEMANTIC, **kwargs)  # type: ignore[arg-type]
    assert only.status == hybrid.STATUS_UNAVAILABLE
    assert only.semantic == hybrid.LaneOutcome(hybrid.LANE_UNAVAILABLE, reason)
    assert only.lexical is None
    assert only.lexical_hits == () and only.hits == ()
    assert only.generation_digest == combined.generation_digest
    assert only.profile_digest == combined.profile_digest


def test_missing_generation_and_missing_query_embedding_fall_back(owned: m2.Owned) -> None:
    frontier = _frontier(owned)
    _assert_lexical_fallback(owned.connection, frontier, hybrid.REASON_NO_ACTIVE_GENERATION)
    result = search_authorized_frontier(
        owned.connection,
        frontier,
        _lexical(frontier),
        "notes",
        mode=hybrid.MODE_COMBINED,
        policy=POLICY,
    )
    assert result.semantic == hybrid.LaneOutcome(
        hybrid.LANE_UNAVAILABLE, hybrid.REASON_QUERY_NOT_EMBEDDED
    )
    assert result.generation_digest is None


def test_stale_generation_falls_back(owned: m2.Owned) -> None:
    _build(owned)
    _add_artifact(owned, "evd-gamma", "gamma-notes", m2.BASE_US + 6)
    _assert_lexical_fallback(owned.connection, _frontier(owned), hybrid.REASON_STALE_GENERATION)


def test_incompatible_profile_and_execution_fall_back(owned: m2.Owned) -> None:
    _build(owned)
    frontier = _frontier(owned)
    _assert_lexical_fallback(
        owned.connection,
        frontier,
        hybrid.REASON_INCOMPATIBLE_PROFILE,
        semantic_query=_query(profile=replace(FixedEmbedder.profile, weights_id="weights.other")),
    )
    _assert_lexical_fallback(
        owned.connection,
        frontier,
        hybrid.REASON_INCOMPATIBLE_EXECUTION,
        semantic_query=_query(execution=replace(FixedEmbedder.execution, build_id="build.other")),
    )


def test_generation_recorded_incompatible_falls_back(owned: m2.Owned) -> None:
    embedder = MappedEmbedder(owned.connection)
    embedder.execution = replace(
        FixedEmbedder.execution,
        compatibility=CompatibilityDecision(INCOMPATIBLE, "unqualified_runtime"),
    )
    _build(owned, embedder)
    _assert_lexical_fallback(
        owned.connection,
        _frontier(owned),
        hybrid.REASON_INCOMPATIBLE_EXECUTION,
        semantic_query=_query(execution=embedder.execution),
    )


def test_missing_vector_for_an_active_generation_is_incomplete_coverage(owned: m2.Owned) -> None:
    _build(owned)
    frontier = _frontier(owned)
    ghost = replace(frontier.candidates[-1], evidence_id="evd-ghost")
    widened = authorized_frontier(
        (*frontier.candidates, ghost),
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=RESOLUTION_US,
    )
    with pytest.raises(semantic.IncompleteSemanticCoverage):
        semantic.project_semantic(
            owned.connection,
            semantic.current_generation(owned.connection, workspace_id=WORKSPACE_ID),
            widened,
        )
    _assert_lexical_fallback(owned.connection, widened, hybrid.REASON_INCOMPLETE_COVERAGE)


def test_corrupt_stored_vector_falls_back_and_is_named(owned: m2.Owned) -> None:
    _build(owned)
    with authorised(owned.connection, mutations=True, ddl=True) as tamper:
        tamper.execute("DROP TRIGGER omnivia_guard_semantic_vectors_update")
        tamper.execute(
            "UPDATE omnivia_semantic_vectors SET vector = ? WHERE input_digest = ?",
            (bytes(8), _surface("beta-notes")),
        )
    _assert_lexical_fallback(owned.connection, _frontier(owned), hybrid.REASON_CORRUPT_STATE)


def test_unexpected_errors_are_not_hidden_as_unavailability(owned: m2.Owned) -> None:
    _build(owned)
    frontier = _frontier(owned)
    with pytest.raises(SemanticVectorError):
        _search(
            owned.connection,
            frontier,
            "notes",
            mode=hybrid.MODE_COMBINED,
            semantic_query=_query(vector=b"\x00" * 3),
        )
    with pytest.raises(sqlite3.ProgrammingError):
        _search(_closed(), frontier, "notes", mode=hybrid.MODE_COMBINED)
    with pytest.raises(ValueError, match="mode"):
        _search(owned.connection, frontier, "notes", mode="hybrid")


def test_malformed_query_vector_is_rejected_before_empty_or_unavailable_state(
    owned: m2.Owned,
) -> None:
    frontier = _frontier(owned)
    malformed = _query(vector=b"\x00" * 3)
    for query in ("", "notes"):
        with pytest.raises(SemanticVectorError):
            _search(
                owned.connection,
                frontier,
                query,
                mode=hybrid.MODE_COMBINED,
                semantic_query=malformed,
            )


def test_caller_supplied_generation_is_revalidated(owned: m2.Owned) -> None:
    _build(owned)
    frontier = _frontier(owned)
    generation = semantic.current_generation(owned.connection, workspace_id=WORKSPACE_ID)
    result = search_authorized_frontier(
        owned.connection,
        frontier,
        _lexical(frontier),
        "notes",
        mode=hybrid.MODE_COMBINED,
        policy=POLICY,
        semantic_query=_query(),
        generation=generation,
    )
    assert result.semantic == hybrid.LaneOutcome(hybrid.LANE_OK)
    assert result.generation_digest == generation.identity.digest

    _add_artifact(owned, "evd-gamma", "gamma-notes", m2.BASE_US + 6)
    _assert_lexical_fallback(
        owned.connection,
        _frontier(owned),
        hybrid.REASON_STALE_GENERATION,
        generation=generation,
    )


def test_combined_ranks_both_lanes_with_one_based_rrf_and_carries_provenance(
    owned: m2.Owned,
) -> None:
    _build(owned)
    frontier = _frontier(owned)
    result = _search(owned.connection, frontier, "notes", mode=hybrid.MODE_COMBINED)
    generation = semantic.current_generation(owned.connection, workspace_id=WORKSPACE_ID)
    assert result.status == hybrid.STATUS_COMPLETE
    assert result.lexical == result.semantic == hybrid.LaneOutcome(hybrid.LANE_OK)
    assert result.generation_digest == generation.identity.digest
    assert result.profile_digest == FixedEmbedder.profile.digest
    # Semantic lane: the exact top of every member, a zero score kept and ranked.
    assert [(hit.candidate.evidence_id, hit.score, hit.rank) for hit in result.semantic_hits] == [
        ("evd-0001", 1.0, 1),
        ("evd-beta", pytest.approx(0.8), 2),
        ("evd-alpha", 0.0, 3),
    ]
    assert [hit.input_digest for hit in result.semantic_hits] == [
        _surface("doc-1"),
        _surface("beta-notes"),
        _surface("alpha-notes"),
    ]
    # Lexical lane: "notes" matches alpha and beta only, BM25-tied, id order.
    assert _ids(result.lexical_hits) == ["evd-alpha", "evd-beta"]
    assert [
        (hit.candidate.evidence_id, hit.rank_score, hit.lexical_rank, hit.semantic_rank)
        for hit in result.hits
    ] == [
        # 1/61 + 1/63 > 2/62: rank-1 in one lane beats rank-2 in both.
        ("evd-alpha", float(Fraction(1, 61) + Fraction(1, 63)), 1, 3),
        ("evd-beta", float(Fraction(1, 62) + Fraction(1, 62)), 2, 2),
        ("evd-0001", float(Fraction(1, 61)), None, 1),
    ]
    assert [hit.semantic_input_digest for hit in result.hits] == [
        _surface("alpha-notes"),
        _surface("beta-notes"),
        _surface("doc-1"),
    ]


def test_lane_budget_and_final_limit_apply_after_full_ranking(owned: m2.Owned) -> None:
    _build(owned)
    result = _search(
        owned.connection,
        _frontier(owned),
        "notes",
        mode=hybrid.MODE_COMBINED,
        policy=SemanticRetrievalPolicy(candidate_budget=1, result_limit=1),
    )
    assert _ids(result.semantic_hits) == ["evd-0001"]
    assert _ids(result.lexical_hits) == ["evd-alpha"]
    assert _ids(result.hits) == ["evd-0001"]


def test_semantic_mode_reports_no_match_rather_than_lexical_hits(owned: m2.Owned) -> None:
    _build(owned)
    result = _search(
        owned.connection,
        _frontier(owned),
        "notes",
        mode=hybrid.MODE_SEMANTIC,
        policy=SemanticRetrievalPolicy(candidate_budget=10, threshold=1.0),
        semantic_query=_query(vector=encode_vector((0.0, -1.0), dimensions=2)),
    )
    assert result.status == hybrid.STATUS_COMPLETE
    assert result.semantic == hybrid.LaneOutcome(hybrid.LANE_NO_MATCH)
    assert result.lexical is None
    assert result.hits == () and result.lexical_hits == ()


def test_empty_query_and_empty_frontier_are_deterministic_no_match(owned: m2.Owned) -> None:
    frontier = _frontier(owned)
    empty_frontier = authorized_frontier(
        (),
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=RESOLUTION_US,
    )
    for candidates, query in ((frontier, "  　 "), (empty_frontier, "notes")):
        for mode in hybrid.MODES:
            first = _search(_closed(), candidates, query, mode=mode)
            assert first == _search(_closed(), candidates, query, mode=mode)
            assert first.status == hybrid.STATUS_COMPLETE
            assert first.hits == ()
            lanes = [lane for lane in (first.lexical, first.semantic) if lane is not None]
            assert lanes and all(lane == hybrid.LaneOutcome(hybrid.LANE_NO_MATCH) for lane in lanes)
            assert first.generation_digest is None


def test_both_lanes_must_rank_the_same_frozen_frontier(owned: m2.Owned) -> None:
    _build(owned)
    owner = _frontier(owned)
    other = _frontier(owned, principal="someone-else")
    reordered = replace(_lexical(owner), candidates=tuple(reversed(_lexical(owner).candidates)))
    forged = replace(owner, checksum="sha256:" + "0" * 64)
    cases: list[tuple[AuthorizedFrontier, ProjectedFrontier]] = [
        (owner, _lexical(other)),
        (other, _lexical(owner)),
        (owner, reordered),
        (owner, replace(_lexical(owner), workspace_id=m2.OTHER_WORKSPACE_ID)),
        (forged, _lexical(forged)),
    ]
    for frontier, lexical in cases:
        for mode in hybrid.MODES:
            with pytest.raises(ValueError, match="not the authorized frontier"):
                search_authorized_frontier(
                    owned.connection,
                    frontier,
                    lexical,
                    "notes",
                    mode=mode,
                    policy=POLICY,
                    semantic_query=_query(),
                )


def test_hidden_evidence_never_reaches_or_influences_either_lane(owned: m2.Owned) -> None:
    _build(owned)
    visible = _frontier(owned, principal="someone-else")
    assert [candidate.evidence_id for candidate in visible.excluded] == ["evd-0001"]
    # "doc 1" is the hidden artifact's own phrase, and the query vector is its own vector.
    probe = _search(owned.connection, visible, "doc 1", mode=hybrid.MODE_COMBINED)
    assert probe.lexical == hybrid.LaneOutcome(hybrid.LANE_NO_MATCH)
    assert "evd-0001" not in _ids(probe.semantic_hits) + _ids(probe.hits)
    result = _search(owned.connection, visible, "notes", mode=hybrid.MODE_COMBINED)
    assert result.status == hybrid.STATUS_COMPLETE
    assert _ids(result.semantic_hits) == ["evd-beta", "evd-alpha"]
    assert [hit.rank for hit in result.semantic_hits] == [1, 2]
    assert [
        (hit.candidate.evidence_id, hit.rank_score) for hit in result.hits
    ] == [
        ("evd-alpha", float(Fraction(1, 61) + Fraction(1, 62))),
        ("evd-beta", float(Fraction(1, 62) + Fraction(1, 61))),
    ]
    assert result.authorized_frontier_checksum == visible.checksum
    assert result.authorized_frontier_checksum != _frontier(owned).checksum
