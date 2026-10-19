from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.storage import code_analysis_candidates as subject
from omnivia_core_runtime.storage.code_analysis_candidates import (
    CodeAnalysisBatch,
    CodeAnalysisBindingError,
    CodeAnalysisCandidateError,
    CodeEdgeCandidate,
    CodeLocator,
    CodeNodeCandidate,
    SourceBinding,
    UnresolvedEndpoint,
    VerifiedCodeAnalysisBatch,
    revision_instance_id,
    to_memory_create_inputs,
    verify_batch_binding,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.engineering_source import CoveredSnapshot

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _binding(path: str = "src/example.py", digest: str = DIGEST_A) -> SourceBinding:
    return SourceBinding(
        relative_path=path,
        content_digest=digest,
        locator=CodeLocator(start_line=1, start_column=1, end_line=3, end_column=8),
    )


def _batch(
    *,
    digest: str = DIGEST_A,
    snapshot_kind: str = "git_commit",
    profile_digest: str | None = None,
) -> CodeAnalysisBatch:
    first = CodeNodeCandidate(
        semantic_key="module.Example",
        kind="class",
        display_name="Example",
        source=_binding(digest=digest),
    )
    second = CodeNodeCandidate(
        semantic_key="module.helper",
        kind="function",
        display_name="helper",
        source=_binding(path="src/helper.py", digest=DIGEST_B),
    )
    resolved = CodeEdgeCandidate(
        edge_key="edge.Example.helper",
        source_key=first.semantic_key,
        target_key=second.semantic_key,
        unresolved_target=None,
        relationship_type="calls",
        evidence_strength="resolved_static",
        resolution_status="resolved",
        source=first.source,
    )
    unresolved = CodeEdgeCandidate(
        edge_key="edge.Example.dynamic",
        source_key=first.semantic_key,
        target_key=None,
        unresolved_target=UnresolvedEndpoint("runtime-dispatch:plugin"),
        relationship_type="calls",
        evidence_strength="inferred_dynamic",
        resolution_status="ambiguous",
        source=first.source,
    )
    return CodeAnalysisBatch(
        repository_id="repository-1",
        snapshot_id="snapshot-1",
        snapshot_kind=snapshot_kind,
        producer_id="analyzer.test",
        producer_version="1.0.0",
        nodes=(first, second),
        edges=(resolved, unresolved),
        profile_digest=profile_digest,
    )


def _verified(batch: CodeAnalysisBatch) -> VerifiedCodeAnalysisBatch:
    return VerifiedCodeAnalysisBatch(
        batch=batch,
        stream_id="stream-1",
        sequence=4,
        manifest_digest="sha256:" + "c" * 64,
        manifest_evidence_id="evidence-manifest-1",
    )


def _adapted(batch: CodeAnalysisBatch) -> tuple[dict[str, object], ...]:
    return tuple(
        item.content
        for item in to_memory_create_inputs(
            _verified(batch),
            actor_id="service-code-analysis",
            asserted_at="2026-10-18T00:00:00.000Z",
        )
    )


def test_batch_identity_is_order_independent_but_instances_are_revision_bound() -> None:
    batch = _batch()
    reordered = CodeAnalysisBatch(
        repository_id=batch.repository_id,
        snapshot_id=batch.snapshot_id,
        snapshot_kind=batch.snapshot_kind,
        producer_id=batch.producer_id,
        producer_version=batch.producer_version,
        nodes=tuple(reversed(batch.nodes)),
        edges=tuple(reversed(batch.edges)),
    )
    assert reordered.digest == batch.digest
    first = batch.nodes[0]
    assert revision_instance_id(
        batch.repository_id, batch.snapshot_id, first.semantic_key, first.source
    ) != revision_instance_id(
        batch.repository_id, "snapshot-2", first.semantic_key, first.source
    )


def test_edges_fail_closed_for_mixed_or_implicit_resolution() -> None:
    node = _batch().nodes[0]
    with pytest.raises(CodeAnalysisCandidateError):
        CodeEdgeCandidate(
            edge_key="edge.invalid",
            source_key=node.semantic_key,
            target_key="module.target",
            unresolved_target=UnresolvedEndpoint("also-unresolved"),
            relationship_type="calls",
            evidence_strength="resolved_static",
            resolution_status="resolved",
            source=node.source,
        )


def test_verified_batch_maps_only_to_proposed_memory_inputs() -> None:
    batch = _batch()
    verified = VerifiedCodeAnalysisBatch(
        batch=batch,
        stream_id="stream-1",
        sequence=4,
        manifest_digest="sha256:" + "c" * 64,
        manifest_evidence_id="evidence-manifest-1",
    )
    inputs = to_memory_create_inputs(
        verified,
        actor_id="service-code-analysis",
        asserted_at="2026-10-15T00:00:00.000Z",
    )
    assert [item.record_type for item in inputs] == [
        "memory.entity",
        "memory.entity",
        "memory.relation",
        "memory.relation",
    ]
    assert all(item.domain_scope == "engineering.code" for item in inputs)
    assert all(item.content["batch_digest"] == batch.digest for item in inputs)
    assert all(item.assertion.actor_role == "code_analysis_producer" for item in inputs)
    assert all(item.extraction.reconciliation_state == "unreviewed" for item in inputs)
    assert inputs[-1].content["resolution_status"] == "ambiguous"
    assert inputs[-1].content["unresolved_target"] == "runtime-dispatch:plugin"


def test_a_profile_less_batch_keeps_the_identity_code_analysis_v1_always_gave_it() -> None:
    """`profile_digest` is an addition, so absent has to keep meaning absent.

    `preimage` below is the canonical digest document exactly as `code_analysis.v1`
    defined it before the field existed. A batch that carries no profile still has to
    digest to it: a key spelled `null` is still a key, and writing one unconditionally
    would have moved the identity of every batch that predates the field while the format
    version went on claiming nothing had changed. The same rule covers the adapted
    candidate content, which is what a consumer actually stores.
    """
    batch = _batch()
    assert batch.profile_digest is None
    preimage = {
        "format": "code_analysis.v1",
        "repository_id": batch.repository_id,
        "snapshot_id": batch.snapshot_id,
        "snapshot_kind": batch.snapshot_kind,
        "producer_id": batch.producer_id,
        "producer_version": batch.producer_version,
        "nodes": sorted((node.as_document() for node in batch.nodes), key=canonical_document),
        "edges": sorted((edge.as_document() for edge in batch.edges), key=canonical_document),
    }
    assert batch.digest == content_digest(canonical_document(preimage))
    assert all("profile_digest" not in content for content in _adapted(batch))


def test_a_present_profile_digest_is_a_distinct_identity_on_every_candidate() -> None:
    profile = "sha256:" + "e" * 64
    other = "sha256:" + "f" * 64
    profiled = _batch(profile_digest=profile)

    # Same candidates, three identities: none, this profile, that profile.
    assert len({_batch().digest, profiled.digest, _batch(profile_digest=other).digest}) == 3
    assert all(content["profile_digest"] == profile for content in _adapted(profiled))
    with pytest.raises(CodeAnalysisCandidateError, match="profile_digest"):
        _batch(profile_digest="not-a-digest")


def test_binding_requires_one_complete_sealed_snapshot_and_exact_file_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE omnivia_engineering_snapshots ("
        "workspace_id TEXT, snapshot_id TEXT, repository_id TEXT, snapshot_kind TEXT);"
        "CREATE TABLE omnivia_engineering_snapshot_captures ("
        "workspace_id TEXT, snapshot_id TEXT, manifest_evidence_id TEXT);"
        "INSERT INTO omnivia_engineering_snapshots VALUES "
        "('workspace-1', 'snapshot-1', 'repository-1', 'git_commit');"
        "INSERT INTO omnivia_engineering_snapshot_captures VALUES "
        "('workspace-1', 'snapshot-1', 'evidence-manifest-1');"
    )
    covered = CoveredSnapshot(
        repository_id="repository-1",
        stream_id="stream-1",
        sequence=7,
        snapshot_id="snapshot-1",
        capture_status="complete",
        manifest_digest="sha256:" + "c" * 64,
        manifest={},
        representation="captured_v1",
    )
    monkeypatch.setattr(subject, "validate_snapshot_ref", lambda *args, **kwargs: None)
    monkeypatch.setattr(subject, "covered_snapshot", lambda *args, **kwargs: covered)
    monkeypatch.setattr(
        subject,
        "captured_manifest_lookup",
        lambda *args, **kwargs: {"src/example.py": DIGEST_A, "src/helper.py": DIGEST_B},
    )

    verified = verify_batch_binding(connection, workspace_id="workspace-1", batch=_batch())
    assert verified.stream_id == "stream-1"
    assert verified.sequence == 7
    assert verified.manifest_evidence_id == "evidence-manifest-1"

    with pytest.raises(CodeAnalysisBindingError, match="snapshot kind"):
        verify_batch_binding(
            connection,
            workspace_id="workspace-1",
            batch=_batch(snapshot_kind="working_tree"),
        )
    with pytest.raises(CodeAnalysisBindingError, match="source digest"):
        verify_batch_binding(
            connection,
            workspace_id="workspace-1",
            batch=_batch(digest="sha256:" + "d" * 64),
        )


def test_module_has_no_graph_or_governance_writer_imports() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "src/omnivia_core_runtime/storage/code_analysis_candidates.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any("graph" in module or "governance" in module for module in modules)


def test_ecmascript_identifier_characters_are_admitted_without_widening_separators() -> None:
    for key in ("src/a.ts:$store", "src/a.ts:Counter.#count", "#private", "$", "src/a.ts:f@group"):
        CodeNodeCandidate(semantic_key=key, kind="method", display_name=key, source=_binding())
    for key in ("a b", "a\x00b", 'a"b', "a\\b", "-leading", "é́"):
        with pytest.raises(CodeAnalysisCandidateError):
            CodeNodeCandidate(semantic_key=key, kind="method", display_name=key, source=_binding())


def test_an_exact_byte_span_is_optional_paired_and_absent_from_legacy_documents() -> None:
    plain = CodeLocator(start_line=1, start_column=1, end_line=3, end_column=8)
    assert "start_byte" not in plain.as_document()
    spanned = CodeLocator(start_line=1, start_column=1, end_line=3, end_column=8, start_byte=0, end_byte=12)
    assert spanned.as_document()["end_byte"] == 12
    assert _batch().digest != CodeAnalysisBatch(
        **{
            **{field: getattr(_batch(), field) for field in ("repository_id", "snapshot_id", "snapshot_kind", "producer_id", "producer_version", "edges")},
            "nodes": tuple(
                CodeNodeCandidate(
                    semantic_key=node.semantic_key,
                    kind=node.kind,
                    display_name=node.display_name,
                    source=SourceBinding(
                        relative_path=node.source.relative_path,
                        content_digest=node.source.content_digest,
                        locator=spanned,
                    ),
                )
                for node in _batch().nodes
            ),
        }
    ).digest
    for start, end in ((0, None), (None, 4), (5, 4), (-1, 4), (True, 4), (0, 10**10)):
        with pytest.raises(CodeAnalysisCandidateError):
            CodeLocator(start_line=1, start_column=1, end_line=1, end_column=2, start_byte=start, end_byte=end)  # type: ignore[arg-type]
