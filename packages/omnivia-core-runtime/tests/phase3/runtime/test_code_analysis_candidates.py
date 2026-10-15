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
from omnivia_core_runtime.storage.engineering_source import CoveredSnapshot

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _binding(path: str = "src/example.py", digest: str = DIGEST_A) -> SourceBinding:
    return SourceBinding(
        relative_path=path,
        content_digest=digest,
        locator=CodeLocator(start_line=1, start_column=1, end_line=3, end_column=8),
    )


def _batch(*, digest: str = DIGEST_A, snapshot_kind: str = "git_commit") -> CodeAnalysisBatch:
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
