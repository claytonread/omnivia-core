from __future__ import annotations

import ast
import importlib.util
import math
import struct
import sys
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.storage.retrieval import EvidenceCandidate
from omnivia_core_runtime.storage.semantic_vectors import (
    FUSION_RRF_V1,
    METRIC_COSINE,
    ProjectedSemanticCandidate,
    ProjectedSemanticFrontier,
    SemanticHit,
    SemanticRetrievalPolicy,
    SemanticVectorError,
    UnsupportedSemanticVersion,
    decode_vector,
    encode_vector,
    fuse_ranked,
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
        SemanticHit(candidate=second, score=0.0, rank=1),
        SemanticHit(candidate=first, score=-1.0, rank=2),
    )
    one = fuse_ranked((first, second), semantic, version=FUSION_RRF_V1, limit=2)
    two = fuse_ranked((first, second), semantic, version=FUSION_RRF_V1, limit=2)
    assert one == two
    assert [hit.candidate.evidence_id for hit in one] == ["evidence-a", "evidence-b"]
    assert one[1].semantic_score == 0.0
    with pytest.raises(UnsupportedSemanticVersion):
        fuse_ranked((first,), semantic, version="rrf.future", limit=1)


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
