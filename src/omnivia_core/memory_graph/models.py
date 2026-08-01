"""Backend-neutral memory graph contracts.

These models describe source-grounded memory graph records and bounded display
responses. They intentionally avoid persistence and graph-database concerns so
Platform and Dev can share one public-safe contract surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias

Confidence: TypeAlias = float | str


class MemorySourceType(str, Enum):
    """Kinds of sources that can be ingested into memory."""

    FILE = "file"
    URL = "url"
    CONNECTOR = "connector"
    NOTE = "note"
    APP_RECORD = "app_record"
    UNKNOWN = "unknown"


class MemorySourceFreshness(str, Enum):
    """Freshness state for an ingested source."""

    FRESH = "fresh"
    STALE = "stale"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class MemorySourceStatus(str, Enum):
    """Processing state for an ingested source."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class MemorySegmentKind(str, Enum):
    """Kinds of source spans that can support graph evidence."""

    TEXT = "text"
    TABLE = "table"
    CODE = "code"
    METADATA = "metadata"
    UNKNOWN = "unknown"


class MemoryFactStatus(str, Enum):
    """Governance state for source-grounded facts."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class GraphPreviewKind(str, Enum):
    """Display node kind for bounded graph previews."""

    SOURCE = "source"
    SEGMENT = "segment"
    ENTITY = "entity"
    FACT = "fact"
    RUN = "run"
    WARNING = "warning"


class GraphPreviewState(str, Enum):
    """Display state for graph preview nodes and edges."""

    READY = "ready"
    PROPOSED = "proposed"
    STALE = "stale"
    FAILED = "failed"
    MISSING_EVIDENCE = "missing_evidence"
    SELECTED = "selected"


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else value


@dataclass(frozen=True)
class SourceRef:
    """Evidence reference for an entity, fact or preview element."""

    source_id: str
    segment_id: str | None = None
    span: dict[str, Any] | None = None
    quote_preview: str | None = None
    confidence: Confidence | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "segment_id": self.segment_id,
            "span": self.span,
            "quote_preview": self.quote_preview,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRef:
        return cls(
            source_id=data["source_id"],
            segment_id=data.get("segment_id"),
            span=data.get("span"),
            quote_preview=data.get("quote_preview"),
            confidence=data.get("confidence"),
        )


@dataclass(frozen=True)
class MemorySource:
    """An ingested source available to the memory graph."""

    id: str
    workspace_id: str
    type: MemorySourceType
    uri: str
    title: str
    freshness: MemorySourceFreshness
    status: MemorySourceStatus
    created_at: str
    updated_at: str
    owner_ref: str | None = None
    connector_ref: str | None = None
    checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "type": self.type.value,
            "uri": self.uri,
            "title": self.title,
            "owner_ref": self.owner_ref,
            "connector_ref": self.connector_ref,
            "freshness": self.freshness.value,
            "status": self.status.value,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemorySource:
        return cls(
            id=data["id"],
            workspace_id=data["workspace_id"],
            type=MemorySourceType(data["type"]),
            uri=data["uri"],
            title=data["title"],
            owner_ref=data.get("owner_ref"),
            connector_ref=data.get("connector_ref"),
            freshness=MemorySourceFreshness(data["freshness"]),
            status=MemorySourceStatus(data["status"]),
            checksum=data.get("checksum"),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class MemorySegment:
    """A source span used as evidence."""

    id: str
    source_id: str
    workspace_id: str
    kind: MemorySegmentKind
    label: str
    parser: str
    parser_settings_ref: str
    created_at: str
    span: dict[str, Any] | None = None
    text_preview: str | None = None
    checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "workspace_id": self.workspace_id,
            "kind": self.kind.value,
            "label": self.label,
            "span": self.span,
            "text_preview": self.text_preview,
            "checksum": self.checksum,
            "parser": self.parser,
            "parser_settings_ref": self.parser_settings_ref,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemorySegment:
        return cls(
            id=data["id"],
            source_id=data["source_id"],
            workspace_id=data["workspace_id"],
            kind=MemorySegmentKind(data["kind"]),
            label=data["label"],
            span=data.get("span"),
            text_preview=data.get("text_preview"),
            checksum=data.get("checksum"),
            parser=data["parser"],
            parser_settings_ref=data["parser_settings_ref"],
            created_at=data["created_at"],
        )


@dataclass(frozen=True)
class MemoryEntity:
    """An extracted or registered entity in the memory graph."""

    id: str
    workspace_id: str
    type: str
    canonical_name: str
    aliases: list[str]
    confidence: Confidence
    source_refs: list[SourceRef]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "type": self.type,
            "canonical_name": self.canonical_name,
            "aliases": self.aliases,
            "confidence": self.confidence,
            "source_refs": [source_ref.to_dict() for source_ref in self.source_refs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntity:
        return cls(
            id=data["id"],
            workspace_id=data["workspace_id"],
            type=data["type"],
            canonical_name=data["canonical_name"],
            aliases=list(data.get("aliases", [])),
            confidence=data["confidence"],
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class MemoryFact:
    """A source-grounded relationship or property assertion."""

    id: str
    workspace_id: str
    subject_id: str
    predicate: str
    confidence: Confidence
    source_refs: list[SourceRef]
    status: MemoryFactStatus
    created_at: str
    updated_at: str
    object_id: str | None = None
    object_value: str | int | float | bool | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    supersedes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "object_value": self.object_value,
            "confidence": self.confidence,
            "source_refs": [source_ref.to_dict() for source_ref in self.source_refs],
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "supersedes": self.supersedes,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryFact:
        return cls(
            id=data["id"],
            workspace_id=data["workspace_id"],
            subject_id=data["subject_id"],
            predicate=data["predicate"],
            object_id=data.get("object_id"),
            object_value=data.get("object_value"),
            confidence=data["confidence"],
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            supersedes=data.get("supersedes"),
            status=MemoryFactStatus(data["status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class GraphPreviewNode:
    """Display node for bounded memory graph previews."""

    id: str
    label: str
    type: str
    kind: GraphPreviewKind
    state: GraphPreviewState
    source_refs: list[SourceRef] = field(default_factory=list)
    entity_ref: str | None = None
    source_ref: str | None = None
    segment_ref: str | None = None
    confidence: Confidence | None = None
    display: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity_ref": self.entity_ref,
            "source_ref": self.source_ref,
            "segment_ref": self.segment_ref,
            "label": self.label,
            "type": self.type,
            "kind": self.kind.value,
            "state": self.state.value,
            "confidence": self.confidence,
            "source_refs": [source_ref.to_dict() for source_ref in self.source_refs],
            "display": self.display,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphPreviewNode:
        return cls(
            id=data["id"],
            entity_ref=data.get("entity_ref"),
            source_ref=data.get("source_ref"),
            segment_ref=data.get("segment_ref"),
            label=data["label"],
            type=data["type"],
            kind=GraphPreviewKind(data["kind"]),
            state=GraphPreviewState(data["state"]),
            confidence=data.get("confidence"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            display=dict(data.get("display", {})),
        )


@dataclass(frozen=True)
class GraphPreviewEdge:
    """Display edge for bounded memory graph previews."""

    id: str
    source: str
    target: str
    label: str
    type: str
    state: GraphPreviewState
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: Confidence | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    display: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "type": self.type,
            "state": self.state.value,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_refs": [source_ref.to_dict() for source_ref in self.source_refs],
            "display": self.display,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphPreviewEdge:
        return cls(
            id=data["id"],
            source=data["source"],
            target=data["target"],
            label=data["label"],
            type=data["type"],
            state=GraphPreviewState(data["state"]),
            confidence=data.get("confidence"),
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            display=dict(data.get("display", {})),
        )


@dataclass(frozen=True)
class GraphPreviewResponse:
    """Bounded graph preview response for read-only consumers."""

    workspace_id: str
    query: str
    nodes: list[GraphPreviewNode]
    edges: list[GraphPreviewEdge]
    warnings: list[str]
    limits: dict[str, int]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "query": self.query,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "warnings": self.warnings,
            "limits": self.limits,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphPreviewResponse:
        return cls(
            workspace_id=data["workspace_id"],
            query=data["query"],
            nodes=[GraphPreviewNode.from_dict(item) for item in data.get("nodes", [])],
            edges=[GraphPreviewEdge.from_dict(item) for item in data.get("edges", [])],
            warnings=list(data.get("warnings", [])),
            limits=dict(data.get("limits", {})),
            generated_at=data["generated_at"],
        )


@dataclass(frozen=True)
class EvidenceGraphResponse:
    """Read-only evidence graph explaining grounded answers or retrievals."""

    workspace_id: str
    query: str
    nodes: list[GraphPreviewNode]
    edges: list[GraphPreviewEdge]
    citations: list[SourceRef]
    warnings: list[str]
    generated_at: str
    answer_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "answer_id": self.answer_id,
            "query": self.query,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "citations": [citation.to_dict() for citation in self.citations],
            "warnings": self.warnings,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceGraphResponse:
        return cls(
            workspace_id=data["workspace_id"],
            answer_id=data.get("answer_id"),
            query=data["query"],
            nodes=[GraphPreviewNode.from_dict(item) for item in data.get("nodes", [])],
            edges=[GraphPreviewEdge.from_dict(item) for item in data.get("edges", [])],
            citations=[SourceRef.from_dict(item) for item in data.get("citations", [])],
            warnings=list(data.get("warnings", [])),
            generated_at=data["generated_at"],
        )


@dataclass(frozen=True)
class RetrievalTrace:
    """Dev-only retrieval diagnostic contract."""

    id: str
    workspace_id: str
    query: str
    mode: str
    matched_fact_ids: list[str]
    matched_segment_ids: list[str]
    rank_scores: dict[str, float]
    timing: dict[str, int | float]
    resource_indicators: dict[str, Any]
    warnings: list[str]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "query": self.query,
            "mode": self.mode,
            "matched_fact_ids": self.matched_fact_ids,
            "matched_segment_ids": self.matched_segment_ids,
            "rank_scores": self.rank_scores,
            "timing": self.timing,
            "resource_indicators": self.resource_indicators,
            "warnings": self.warnings,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetrievalTrace:
        return cls(
            id=data["id"],
            workspace_id=data["workspace_id"],
            query=data["query"],
            mode=data["mode"],
            matched_fact_ids=list(data.get("matched_fact_ids", [])),
            matched_segment_ids=list(data.get("matched_segment_ids", [])),
            rank_scores=dict(data.get("rank_scores", {})),
            timing=dict(data.get("timing", {})),
            resource_indicators=dict(data.get("resource_indicators", {})),
            warnings=list(data.get("warnings", [])),
            generated_at=data["generated_at"],
        )
