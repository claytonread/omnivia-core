"""Revision-bound source-code candidates for Core's existing proposal path.

The values here are producer output, never graph authority. A batch binds one recorded
repository snapshot, validates every source digest against that snapshot, and adapts to
``memory.create`` inputs whose contract is proposed-only. Nothing in this module submits,
approves, governs, or writes graph endpoints.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    CandidateAssertion,
    CandidateExtractionMetadata,
    EvidenceReference,
    MemoryCreateInput,
    SourceReference,
    decode_memory_create_input,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.engineering_source import (
    captured_manifest_lookup,
    covered_snapshot,
    valid_path,
)
from omnivia_core_runtime.storage.repository_identity import (
    RepositoryNotFound,
    SnapshotNotFound,
    validate_snapshot_ref,
)

FORMAT_VERSION: Final = "code_analysis.v1"
RESOLUTION_STATUSES: Final = frozenset({"resolved", "unresolved", "ambiguous", "external"})
EVIDENCE_STRENGTHS: Final = frozenset({"syntactic", "resolved_static", "inferred_dynamic"})
MAX_CANDIDATES: Final = 10_000
MAX_LOCATOR_VALUE: Final = 1_000_000_000

#: Candidate identity tokens. Widened from `[A-Za-z0-9]` to `\w` so a producer can key a
#: `café` or `日本語` definition -- and a `_private` module -- by the name its own source
#: uses rather than by a mangling of it. `\w` on a `str` pattern is Unicode-aware and still
#: admits no whitespace, no control character, no quote and no separator beyond the `/` the
#: punctuation class already carried, so every value the narrower pattern accepted is still
#: accepted and nothing newly accepted can escape a path, a log line or a JSON string.
_ID = re.compile(r"\w[\w._:/@+-]{0,255}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class CodeAnalysisCandidateError(ValueError):
    """The producer output is malformed or disagrees with captured source."""


class CodeAnalysisBindingError(CodeAnalysisCandidateError):
    """The batch cannot be proven against one complete covered snapshot."""


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CodeAnalysisCandidateError(f"{name} is not a bounded identifier")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise CodeAnalysisCandidateError(f"{name} is not a sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class CodeLocator:
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        values = (self.start_line, self.start_column, self.end_line, self.end_column)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise CodeAnalysisCandidateError("locator coordinates must be integers")
        if not all(1 <= value <= MAX_LOCATOR_VALUE for value in values):
            raise CodeAnalysisCandidateError("locator coordinates are out of bounds")
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise CodeAnalysisCandidateError("locator end precedes its start")

    def as_document(self) -> dict[str, int]:
        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }


@dataclass(frozen=True, slots=True)
class SourceBinding:
    relative_path: str
    content_digest: str
    locator: CodeLocator

    def __post_init__(self) -> None:
        if not valid_path(self.relative_path):
            raise CodeAnalysisCandidateError("source path is not a canonical repository-relative path")
        _digest(self.content_digest, "content_digest")

    def as_document(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "locator": self.locator.as_document(),
        }


def revision_instance_id(
    repository_id: str, snapshot_id: str, semantic_key: str, binding: SourceBinding
) -> str:
    document = {
        "format": FORMAT_VERSION,
        "repository_id": repository_id,
        "snapshot_id": snapshot_id,
        "semantic_key": semantic_key,
        "source": binding.as_document(),
    }
    return "code-instance:" + hashlib.sha256(canonical_document(document).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CodeNodeCandidate:
    semantic_key: str
    kind: str
    display_name: str
    source: SourceBinding

    def __post_init__(self) -> None:
        _identifier(self.semantic_key, "semantic_key")
        _identifier(self.kind, "kind")
        if not isinstance(self.display_name, str) or not self.display_name or len(self.display_name) > 512:
            raise CodeAnalysisCandidateError("display_name must contain 1-512 characters")

    def as_document(self) -> dict[str, object]:
        return {
            "semantic_key": self.semantic_key,
            "kind": self.kind,
            "display_name": self.display_name,
            "source": self.source.as_document(),
        }


@dataclass(frozen=True, slots=True)
class UnresolvedEndpoint:
    descriptor: str

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, str) or not self.descriptor or len(self.descriptor) > 1024:
            raise CodeAnalysisCandidateError("unresolved endpoint descriptor is invalid")


@dataclass(frozen=True, slots=True)
class CodeEdgeCandidate:
    edge_key: str
    source_key: str
    target_key: str | None
    unresolved_target: UnresolvedEndpoint | None
    relationship_type: str
    evidence_strength: str
    resolution_status: str
    source: SourceBinding

    def __post_init__(self) -> None:
        _identifier(self.edge_key, "edge_key")
        _identifier(self.source_key, "source_key")
        _identifier(self.relationship_type, "relationship_type")
        if self.evidence_strength not in EVIDENCE_STRENGTHS:
            raise CodeAnalysisCandidateError("unsupported evidence strength")
        if self.resolution_status not in RESOLUTION_STATUSES:
            raise CodeAnalysisCandidateError("unsupported resolution status")
        if self.resolution_status == "resolved":
            if self.target_key is None or self.unresolved_target is not None:
                raise CodeAnalysisCandidateError("resolved edge requires one local target")
            _identifier(self.target_key, "target_key")
        elif self.target_key is not None or self.unresolved_target is None:
            raise CodeAnalysisCandidateError(
                "unresolved, ambiguous and external edges require only an explicit descriptor"
            )

    def as_document(self) -> dict[str, object]:
        return {
            "edge_key": self.edge_key,
            "source_key": self.source_key,
            "target_key": self.target_key,
            "unresolved_target": (
                None if self.unresolved_target is None else self.unresolved_target.descriptor
            ),
            "relationship_type": self.relationship_type,
            "evidence_strength": self.evidence_strength,
            "resolution_status": self.resolution_status,
            "source": self.source.as_document(),
        }


@dataclass(frozen=True, slots=True)
class CodeAnalysisBatch:
    repository_id: str
    snapshot_id: str
    snapshot_kind: str
    producer_id: str
    producer_version: str
    nodes: tuple[CodeNodeCandidate, ...]
    edges: tuple[CodeEdgeCandidate, ...]
    #: The digest of the immutable producer profile that produced these candidates: the
    #: grammar, the inclusion rules and the limits, not just the producer's name and
    #: version. When it is present it is part of the batch digest below, so a batch cannot
    #: be replayed as the output of a differently configured run of the same producer
    #: version. Optional and defaulted because the profile concept arrived with the first
    #: real producer; a profile-less batch is still a valid batch, and -- see
    #: :attr:`digest` -- still digests to exactly what it digested to before this field
    #: existed.
    profile_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("repository_id", "snapshot_id", "snapshot_kind", "producer_id", "producer_version"):
            _identifier(getattr(self, name), name)
        if self.profile_digest is not None:
            _digest(self.profile_digest, "profile_digest")
        if not self.nodes or len(self.nodes) + len(self.edges) > MAX_CANDIDATES:
            raise CodeAnalysisCandidateError("batch size is outside the admitted bounds")
        node_keys = [node.semantic_key for node in self.nodes]
        edge_keys = [edge.edge_key for edge in self.edges]
        if len(node_keys) != len(set(node_keys)) or len(edge_keys) != len(set(edge_keys)):
            raise CodeAnalysisCandidateError("candidate identities must be unique")
        local = set(node_keys)
        for edge in self.edges:
            if edge.source_key not in local:
                raise CodeAnalysisCandidateError("edge source is not a node in this batch")
            if edge.resolution_status == "resolved" and edge.target_key not in local:
                raise CodeAnalysisCandidateError("resolved edge target is not a node in this batch")

    @property
    def digest(self) -> str:
        """This batch's identity under `code_analysis.v1`.

        `profile_digest` is in the preimage *only when it is present*. A key carrying
        `null` is still a key: adding one unconditionally would have changed the digest of
        every batch that predates the profile concept while leaving the format version
        claiming they were the same. An absent profile is therefore spelled as an absent
        key, so a profile-less batch digests to exactly what `code_analysis.v1` always
        said it digests to, and a batch that does carry a profile has a distinct identity
        from the same candidates produced under a different configuration.
        """
        document: dict[str, object] = {
            "format": FORMAT_VERSION,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_kind": self.snapshot_kind,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "nodes": sorted((node.as_document() for node in self.nodes), key=canonical_document),
            "edges": sorted((edge.as_document() for edge in self.edges), key=canonical_document),
        }
        if self.profile_digest is not None:
            document["profile_digest"] = self.profile_digest
        return content_digest(canonical_document(document))


@dataclass(frozen=True, slots=True)
class VerifiedCodeAnalysisBatch:
    batch: CodeAnalysisBatch
    stream_id: str
    sequence: int
    manifest_digest: str
    manifest_evidence_id: str


def verify_batch_binding(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    batch: CodeAnalysisBatch,
) -> VerifiedCodeAnalysisBatch:
    """Prove every candidate against one complete, covered repository snapshot."""

    try:
        validate_snapshot_ref(
            connection,
            workspace_id=workspace_id,
            repository_id=batch.repository_id,
            snapshot_id=batch.snapshot_id,
        )
    except (RepositoryNotFound, SnapshotNotFound) as exc:
        raise CodeAnalysisBindingError("repository or snapshot binding is unavailable") from exc
    kind_row = connection.execute(
        "SELECT snapshot_kind FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? AND snapshot_id = ? AND repository_id = ?",
        (workspace_id, batch.snapshot_id, batch.repository_id),
    ).fetchone()
    if kind_row is None or str(kind_row[0]) != batch.snapshot_kind:
        raise CodeAnalysisBindingError("snapshot kind does not match the recorded snapshot")
    snapshot = covered_snapshot(
        connection,
        workspace_id=workspace_id,
        repository_id=batch.repository_id,
        snapshot_id=batch.snapshot_id,
    )
    if snapshot is None:
        raise CodeAnalysisBindingError("snapshot is not inside covered source history")
    if snapshot.capture_status != "complete":
        raise CodeAnalysisBindingError("source capture is incomplete")
    if snapshot.representation != "captured_v1":
        raise CodeAnalysisBindingError("code analysis requires a sealed captured snapshot")
    bindings = tuple(node.source for node in batch.nodes) + tuple(edge.source for edge in batch.edges)
    paths = tuple(sorted({binding.relative_path for binding in bindings}))
    manifest = captured_manifest_lookup(
        connection,
        workspace_id=workspace_id,
        snapshot_id=batch.snapshot_id,
        paths=paths,
    )
    for binding in bindings:
        if manifest.get(binding.relative_path) != binding.content_digest:
            raise CodeAnalysisBindingError("candidate source digest is not in the captured manifest")
    row = connection.execute(
        "SELECT manifest_evidence_id FROM omnivia_engineering_snapshot_captures "
        "WHERE workspace_id = ? AND snapshot_id = ?",
        (workspace_id, batch.snapshot_id),
    ).fetchone()
    if row is None:
        raise CodeAnalysisBindingError("snapshot has no sealed manifest evidence")
    return VerifiedCodeAnalysisBatch(
        batch=batch,
        stream_id=snapshot.stream_id,
        sequence=snapshot.sequence,
        manifest_digest=snapshot.manifest_digest,
        manifest_evidence_id=str(row[0]),
    )


def _candidate_content(
    verified: VerifiedCodeAnalysisBatch,
    candidate: CodeNodeCandidate | CodeEdgeCandidate,
    *,
    candidate_kind: str,
) -> dict[str, Any]:
    batch = verified.batch
    base: dict[str, Any] = {
        "profile": FORMAT_VERSION,
        "candidate_kind": candidate_kind,
        "repository_id": batch.repository_id,
        "snapshot_id": batch.snapshot_id,
        "snapshot_kind": batch.snapshot_kind,
        "batch_digest": batch.digest,
        "producer_id": batch.producer_id,
        "producer_version": batch.producer_version,
        "source": candidate.source.as_document(),
    }
    if batch.profile_digest is not None:
        # Absent means absent here too, for the same reason it does in the batch digest:
        # a `null` key would change the content a profile-less batch has always adapted to.
        base["profile_digest"] = batch.profile_digest
    if isinstance(candidate, CodeNodeCandidate):
        base.update(
            semantic_key=candidate.semantic_key,
            instance_id=revision_instance_id(
                batch.repository_id, batch.snapshot_id, candidate.semantic_key, candidate.source
            ),
            node_kind=candidate.kind,
            display_name=candidate.display_name,
        )
    else:
        base.update(candidate.as_document())
        base["instance_id"] = revision_instance_id(
            batch.repository_id, batch.snapshot_id, candidate.edge_key, candidate.source
        )
    return base


def to_memory_create_inputs(
    verified: VerifiedCodeAnalysisBatch,
    *,
    actor_id: str,
    asserted_at: str,
) -> tuple[MemoryCreateInput, ...]:
    """Adapt to existing proposed-only inputs, validating each through the decoder."""

    batch = verified.batch
    source = SourceReference(kind="document", source_id=verified.manifest_evidence_id)
    evidence = EvidenceReference(source=source)
    assertion = CandidateAssertion(
        actor_id=actor_id,
        actor_kind="service",
        actor_role="code_analysis_producer",
        asserted_at=asserted_at,
        evidence=(evidence,),
    )
    extraction = CandidateExtractionMetadata(
        extractor_id=batch.producer_id,
        extractor_version=batch.producer_version,
        extracted_at=asserted_at,
        reconciliation_state="unreviewed",
    )
    inputs: list[MemoryCreateInput] = []
    def append_candidate(
        record_type: str,
        candidate_kind: str,
        candidate: CodeNodeCandidate | CodeEdgeCandidate,
    ) -> None:
        candidate_input = MemoryCreateInput(
            record_type=record_type,
            domain_scope="engineering.code",
            content=_candidate_content(verified, candidate, candidate_kind=candidate_kind),
            evidence_disposition="available",
            sources=(source,),
            assertion=assertion,
            extraction=extraction,
        )
        inputs.append(decode_memory_create_input(candidate_input.to_wire()))
    for node in batch.nodes:
        append_candidate("memory.entity", "node", node)
    for edge in batch.edges:
        append_candidate("memory.relation", "edge", edge)
    return tuple(inputs)


__all__ = [
    "EVIDENCE_STRENGTHS",
    "FORMAT_VERSION",
    "RESOLUTION_STATUSES",
    "CodeAnalysisBatch",
    "CodeAnalysisBindingError",
    "CodeAnalysisCandidateError",
    "CodeEdgeCandidate",
    "CodeLocator",
    "CodeNodeCandidate",
    "SourceBinding",
    "UnresolvedEndpoint",
    "VerifiedCodeAnalysisBatch",
    "revision_instance_id",
    "to_memory_create_inputs",
    "verify_batch_binding",
]
