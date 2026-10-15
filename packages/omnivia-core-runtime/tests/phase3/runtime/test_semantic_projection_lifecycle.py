from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.service.semantic_search import search_authorized_frontier
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import semantic
from omnivia_core_runtime.storage.repository import read_evidence_candidates
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    AuthorizedFrontier,
    authorized_frontier,
    local_owner_label_grant,
)
from omnivia_core_runtime.storage.semantic_vectors import (
    BYTE_ORDER_LITTLE,
    CANONICAL_INPUT_V1,
    COMPATIBLE,
    METRIC_COSINE,
    NORMALIZATION_CORE_L2_UNIT_V1,
    REPRESENTATION_FLOAT32_LE,
    CompatibilityDecision,
    ExecutionProvenance,
    RepresentationIdentity,
    SemanticEmbeddingProfile,
    SemanticRetrievalPolicy,
    encode_vector,
)

WORKSPACE_ID = m2.WORKSPACE_ID


class FixedEmbedder:
    profile = SemanticEmbeddingProfile(
        dimensions=2,
        weights_id="weights.test.v1",
        tokenizer_id="tokenizer.test.v1",
        transform_id="document.test.v1",
        pooling_id="mean.v1",
        normalization_id=NORMALIZATION_CORE_L2_UNIT_V1,
        canonical_input_version=CANONICAL_INPUT_V1,
    )
    execution = ExecutionProvenance(
        provider_id="provider.test",
        runtime_id="runtime.test",
        build_id="build.test",
        hardware_id="cpu.test",
        inference_precision="float32",
        compatibility=CompatibilityDecision(COMPATIBLE, "qualified_test_fixture"),
    )
    representation = RepresentationIdentity(
        scalar_encoding=REPRESENTATION_FLOAT32_LE,
        byte_order=BYTE_ORDER_LITTLE,
        metric=METRIC_COSINE,
        codec_version="float32-le.v1",
        index_version="exact.v1",
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.transaction_states: list[bool] = []

    def embed(self, canonical_inputs: Sequence[bytes]) -> Sequence[Sequence[float]]:
        self.transaction_states.extend(self.connection.in_transaction for _ in canonical_inputs)
        return [(1.0, 0.0) for _ in canonical_inputs]


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    yield holder
    holder.connection.close()


def _frontier(holder: m2.Owned) -> AuthorizedFrontier:
    return authorized_frontier(
        read_evidence_candidates(holder.connection, workspace_id=WORKSPACE_ID),
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=m2.BASE_US + 10_000_000,
    )


def test_build_activate_and_project_only_the_authorized_frontier(owned: m2.Owned) -> None:
    embedder = FixedEmbedder(owned.connection)
    outcome = semantic.build_semantic_projection(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        embedder=embedder,
        now_us=m2.BASE_US + 20_000_000,
    )
    assert outcome.activated
    assert outcome.occurrence_count == 1
    assert outcome.vector_count == 1
    assert embedder.transaction_states == [False]

    generation = semantic.current_generation(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = _frontier(owned)
    projected = semantic.project_semantic(owned.connection, generation, frozen)
    assert [item.candidate.evidence_id for item in projected.candidates] == [
        candidate.evidence_id for candidate in frozen.candidates
    ]
    assert projected.profile_digest == embedder.profile.digest
    assert projected.generation_digest == generation.identity.digest

    result = search_authorized_frontier(
        owned.connection,
        frozen,
        encode_vector((1.0, 0.0), dimensions=2),
        query_profile=embedder.profile,
        query_execution=embedder.execution,
        policy=SemanticRetrievalPolicy(candidate_budget=10),
        generation=generation,
    )
    assert [hit.candidate.evidence_id for hit in result.semantic_hits] == [
        candidate.evidence_id for candidate in frozen.candidates
    ]
    with pytest.raises(semantic.IncompatibleSemanticGeneration, match="profile differs"):
        search_authorized_frontier(
            owned.connection,
            frozen,
            encode_vector((1.0, 0.0), dimensions=2),
            query_profile=replace(embedder.profile, weights_id="weights.other.v1"),
            query_execution=embedder.execution,
            policy=SemanticRetrievalPolicy(candidate_budget=10),
            generation=generation,
        )

    repeat = semantic.build_semantic_projection(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        embedder=embedder,
        now_us=m2.BASE_US + 30_000_000,
    )
    assert not repeat.activated
    assert repeat.run_id == outcome.run_id
    assert embedder.transaction_states == [False]
