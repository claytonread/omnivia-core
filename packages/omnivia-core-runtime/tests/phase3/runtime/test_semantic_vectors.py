from __future__ import annotations

import ast
import importlib.util
import math
import struct
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.storage.retrieval import EvidenceCandidate
from omnivia_core_runtime.storage.semantic_vectors import (
    DEFAULT_CANDIDATE_BUDGET,
    DEFAULT_RESULT_LIMIT,
    FUSION_RRF_V1,
    MAX_CANDIDATE_BUDGET,
    MAX_RESULT_LIMIT,
    METRIC_COSINE,
    ProjectedSemanticCandidate,
    ProjectedSemanticFrontier,
    SemanticHit,
    SemanticRetrievalPolicy,
    SemanticVectorError,
    UnsupportedSemanticVersion,
    canonical_model_input,
    decode_vector,
    encode_vector,
    fuse_ranked,
    input_digest,
    rank_semantic,
)

from omnivia_core.contracts.v1 import (
    EvidenceArtifact,
    RecordTemporalMetadata,
    SourceReference,
)


def _candidate(evidence_id: str) -> EvidenceCandidate:
    checksum = "sha256:" + evidence_id.encode().hex().ljust(64, "0")[:64]
    return EvidenceCandidate(
        evidence_id=evidence_id,
        workspace_id="workspace-test",
        content_checksum=checksum,
        sensitivity="internal",
        permission_labels=(),
        tombstoned=False,
        recorded_at_us=1,
        search_text=evidence_id,
        artifact=EvidenceArtifact(
            evidence_id=evidence_id,
            workspace_id="workspace-test",
            source=SourceReference(kind="filesystem.archive", source_id=evidence_id),
            temporal=RecordTemporalMetadata(
                ingested_at="2023-11-14T22:13:20.000Z",
                recorded_at="2023-11-14T22:13:20.000Z",
            ),
            content_checksum=checksum,
            media_type="text/plain",
            metadata={},
            permission_labels=(),
            sensitivity="internal",
            tombstoned=False,
            parser_status="parsed",
            ingestion_status="ingested",
            provenance_history=(),
        ),
    )


def _input(evidence_id: str) -> str:
    return input_digest(canonical_model_input(f"input {evidence_id}"))


def _frontier(*items: tuple[str, tuple[float, ...]]) -> ProjectedSemanticFrontier:
    return ProjectedSemanticFrontier(
        workspace_id="workspace-test",
        checksum="sha256:" + "1" * 64,
        dimensions=len(items[0][1]),
        profile_digest="sha256:" + "2" * 64,
        generation_digest="sha256:" + "3" * 64,
        candidates=tuple(
            ProjectedSemanticCandidate(
                candidate=_candidate(evidence_id),
                vector=encode_vector(vector, dimensions=len(vector)),
                input_digest=_input(evidence_id),
            )
            for evidence_id, vector in items
        ),
    )


def test_codec_is_exact_and_rejects_noncanonical_values() -> None:
    payload = encode_vector((1.0, 0.0), dimensions=2)
    assert payload == struct.pack("<2f", 1.0, 0.0)
    assert decode_vector(payload, dimensions=2) == (1.0, 0.0)
    for values in ((1.0,), (2.0, 0.0), (math.nan, 0.0), (-0.0, 1.0)):
        with pytest.raises(SemanticVectorError):
            encode_vector(values, dimensions=2)
    with pytest.raises(SemanticVectorError):
        decode_vector(payload[:-1], dimensions=2)


def test_exact_ranking_keeps_zero_and_uses_utf8_id_tie_break() -> None:
    projected = _frontier(
        ("evidence-b", (0.0, 1.0)),
        ("evidence-a", (0.0, 1.0)),
        ("evidence-z", (1.0, 0.0)),
    )
    hits = rank_semantic(
        projected,
        encode_vector((1.0, 0.0), dimensions=2),
        metric=METRIC_COSINE,
        policy=SemanticRetrievalPolicy(candidate_budget=3, threshold=0.0),
    )
    assert [(hit.candidate.evidence_id, hit.score) for hit in hits] == [
        ("evidence-z", 1.0),
        ("evidence-a", 0.0),
        ("evidence-b", 0.0),
    ]


def test_rrf_is_versioned_deterministic_and_preserves_semantic_zero() -> None:
    first = _candidate("evidence-a")
    second = _candidate("evidence-b")
    semantic = (
        SemanticHit(candidate=second, score=0.0, rank=1, input_digest=_input("evidence-b")),
        SemanticHit(candidate=first, score=-1.0, rank=2, input_digest=_input("evidence-a")),
    )
    one = fuse_ranked((first, second), semantic, version=FUSION_RRF_V1, limit=2)
    two = fuse_ranked((first, second), semantic, version=FUSION_RRF_V1, limit=2)
    assert one == two
    assert [hit.candidate.evidence_id for hit in one] == ["evidence-a", "evidence-b"]
    assert one[1].semantic_score == 0.0
    with pytest.raises(UnsupportedSemanticVersion):
        fuse_ranked((first,), semantic, version="rrf.future", limit=1)


def _hit(evidence_id: str, rank: int, score: float = 0.5) -> SemanticHit:
    return SemanticHit(
        candidate=_candidate(evidence_id), score=score, rank=rank, input_digest=_input(evidence_id)
    )


def test_rrf_v1_is_one_based_with_k_60_and_absent_lanes_contribute_zero() -> None:
    a, b, c = (_candidate(name) for name in ("evidence-a", "evidence-b", "evidence-c"))
    fused = fuse_ranked(
        (a, b), (_hit("evidence-b", 1), _hit("evidence-c", 2)), version=FUSION_RRF_V1, limit=10
    )
    assert [
        (hit.candidate.evidence_id, hit.rank_score, hit.lexical_rank, hit.semantic_rank)
        for hit in fused
    ] == [
        ("evidence-b", float(Fraction(1, 62) + Fraction(1, 61)), 2, 1),
        ("evidence-a", float(Fraction(1, 61)), 1, None),
        ("evidence-c", float(Fraction(1, 62)), None, 2),
    ]
    assert [hit.semantic_input_digest for hit in fused] == [
        _input("evidence-b"),
        None,
        _input("evidence-c"),
    ]
    # One lane alone keeps its own order; equal scores break on the UTF-8 evidence id.
    only = fuse_ranked((), (_hit("evidence-z", 1),), version=FUSION_RRF_V1, limit=10)
    assert [(hit.candidate.evidence_id, hit.rank_score) for hit in only] == [
        ("evidence-z", float(Fraction(1, 61)))
    ]
    tie = fuse_ranked((c,), (_hit("evidence-a", 1),), version=FUSION_RRF_V1, limit=10)
    assert [hit.candidate.evidence_id for hit in tie] == ["evidence-a", "evidence-c"]


def test_fusion_refuses_duplicates_gapped_ranks_and_conflicting_artifacts() -> None:
    a = _candidate("evidence-a")
    with pytest.raises(SemanticVectorError, match="lexical result appears twice"):
        fuse_ranked((a, a), (), version=FUSION_RRF_V1, limit=10)
    with pytest.raises(SemanticVectorError, match="semantic result appears twice"):
        fuse_ranked(
            (), (_hit("evidence-a", 1), _hit("evidence-a", 2)), version=FUSION_RRF_V1, limit=10
        )
    with pytest.raises(SemanticVectorError, match="one-based"):
        fuse_ranked((), (_hit("evidence-a", 0),), version=FUSION_RRF_V1, limit=10)
    with pytest.raises(SemanticVectorError, match="one-based"):
        fuse_ranked(
            (), (_hit("evidence-a", 1), _hit("evidence-b", 3)), version=FUSION_RRF_V1, limit=10
        )
    other = replace(a, content_checksum="sha256:" + "f" * 64)
    with pytest.raises(SemanticVectorError, match="two different artifacts"):
        fuse_ranked((other,), (_hit("evidence-a", 1),), version=FUSION_RRF_V1, limit=10)


def test_policy_bounds_lane_budget_at_200_and_result_limit_at_50() -> None:
    assert MAX_CANDIDATE_BUDGET == 200
    assert MAX_RESULT_LIMIT == 50
    assert SemanticRetrievalPolicy().candidate_budget == DEFAULT_CANDIDATE_BUDGET == 50
    policy = SemanticRetrievalPolicy(candidate_budget=200)
    assert policy.result_limit == DEFAULT_RESULT_LIMIT == 10
    assert SemanticRetrievalPolicy(candidate_budget=1, result_limit=50).result_limit == 50
    for bad in ({"candidate_budget": 201}, {"candidate_budget": 0}):
        with pytest.raises(SemanticVectorError, match="candidate_budget"):
            SemanticRetrievalPolicy(**bad)
    for limit in (0, 51, True):
        with pytest.raises(SemanticVectorError, match="result_limit"):
            SemanticRetrievalPolicy(candidate_budget=10, result_limit=limit)
    assert policy.digest != replace(policy, result_limit=11).digest
    many = tuple(_candidate(f"evidence-{index:03d}") for index in range(60))
    assert len(fuse_ranked(many, (), version=FUSION_RRF_V1, limit=50)) == 50
    with pytest.raises(SemanticVectorError, match="limit"):
        fuse_ranked(many, (), version=FUSION_RRF_V1, limit=51)


def test_ranking_scores_the_whole_frontier_before_retaining_the_budget() -> None:
    # 300 members, more than the 200 hard budget, with the single best match last.
    items: list[tuple[str, tuple[float, ...]]] = []
    for index in range(299):
        angle = 0.5 + index / 1000
        items.append((f"evidence-{index:03d}", (math.cos(angle), math.sin(angle))))
    items.append(("evidence-zzz", (1.0, 0.0)))
    projected = _frontier(*items)
    query = encode_vector((1.0, 0.0), dimensions=2)
    top = rank_semantic(
        projected, query, metric=METRIC_COSINE, policy=SemanticRetrievalPolicy(candidate_budget=1)
    )
    assert [(hit.candidate.evidence_id, hit.rank) for hit in top] == [("evidence-zzz", 1)]
    full = rank_semantic(
        projected, query, metric=METRIC_COSINE, policy=SemanticRetrievalPolicy(candidate_budget=200)
    )
    assert len(full) == 200
    expected = sorted(
        projected.candidates,
        key=lambda member: (
            -decode_vector(member.vector, dimensions=2)[0],
            member.candidate.evidence_id.encode(),
        ),
    )[:200]
    assert [hit.candidate.evidence_id for hit in full] == [
        member.candidate.evidence_id for member in expected
    ]
    assert [hit.input_digest for hit in full] == [member.input_digest for member in expected]


def test_projected_candidate_requires_a_digest_shaped_input_identity() -> None:
    with pytest.raises(SemanticVectorError, match="input_digest"):
        ProjectedSemanticCandidate(
            candidate=_candidate("evidence-a"),
            vector=encode_vector((1.0, 0.0), dimensions=2),
            input_digest="not-a-digest",
        )


def _load_oracle() -> Any:
    path = Path(__file__).resolve().parents[5] / "qualification/semantic_index/q1/oracle.py"
    spec = importlib.util.spec_from_file_location("semantic_q1_oracle_product_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_ranking_matches_independent_qualification_oracle() -> None:
    oracle = _load_oracle()
    vectors: list[tuple[str, tuple[float, ...]]] = []
    for index in range(3):
        values = [0.0] * 768
        values[index] = 1.0
        vectors.append((f"q1-record-evidence-{index + 1:06d}", tuple(values)))
    query = encode_vector(vectors[1][1], dimensions=768)
    product = rank_semantic(
        _frontier(*vectors),
        query,
        metric=METRIC_COSINE,
        policy=SemanticRetrievalPolicy(candidate_budget=10),
    )
    reference = oracle.score_candidates(
        query,
        [oracle.Candidate(external_id=name, vector=encode_vector(values, dimensions=768), active=True, eligible=True) for name, values in vectors],
        k=10,
    )
    assert [(hit.candidate.evidence_id, hit.score) for hit in product] == [
        (hit.external_id, hit.score) for hit in reference
    ]


def test_pure_module_has_no_storage_handle_imports() -> None:
    path = Path(__file__).resolve().parents[3] / "src/omnivia_core_runtime/storage/semantic_vectors.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"sqlite3", "connection", "repository", "projections"}
    assert not forbidden.intersection(imported)
