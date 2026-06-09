"""Portable knowledge substrate contracts for OmniVia Core."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GraphSourceType(str, Enum):
    """Portable source categories for knowledge evidence."""

    NOTE = "note"
    FILE = "file"
    DOCUMENT = "document"
    CODE = "code"
    CONVERSATION = "conversation"
    CONNECTOR = "connector"
    MEMORY = "memory"
    API = "api"
    DATABASE = "database"
    IMPORT = "import"
    MANUAL_ENTRY = "manual_entry"


class GraphConfidence(str, Enum):
    """Confidence buckets kept distinct for agent-safe use."""

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


class GraphReviewStatus(str, Enum):
    """Review state for claims, links, and graph facts."""

    UNREVIEWED = "UNREVIEWED"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class GraphEvidenceStrength(str, Enum):
    """Evidence quality markers for portable knowledge."""

    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    WEAK = "WEAK"
    MISSING = "MISSING"


class GraphOrigin(str, Enum):
    """Ownership-neutral origin markers for portable knowledge."""

    OMNIVIA = "omnivia"
    MANUAL = "manual"
    IMPORT = "import"
    CONNECTOR = "connector"
    AGENT = "agent"
    THIRD_PARTY = "third_party"


class GraphVisibility(str, Enum):
    """Audience visibility for portable knowledge."""

    PRIVATE = "private"
    TEAM = "team"
    PUBLIC = "public"


class GraphSensitivity(str, Enum):
    """Sensitivity levels for human- and agent-safe surfaces."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


BUILTIN_OBJECT_KINDS = frozenset(
    {
        "note",
        "document",
        "code",
        "concept",
        "decision",
        "task",
        "workflow",
        "agent_memory",
        "system",
    }
)
BUILTIN_GRAPH_NODE_KINDS = frozenset(
    {
        "note",
        "document",
        "code",
        "concept",
        "task",
        "decision",
        "source",
        "system",
    }
)
BUILTIN_GRAPH_RELATIONS = frozenset(
    {
        "links_to",
        "references",
        "contains",
        "depends_on",
        "calls",
        "imports",
        "supports_claim",
        "derived_backlink",
        "related_to",
    }
)


def _enum_value(value: Enum | float | None) -> str | float | None:
    if isinstance(value, Enum):
        return value.value
    return value


def _serialize_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: _serialize(item) for key, item in value.items()}


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, ContractVersion):
        return str(value)
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return _serialize_dict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class ContractVersion:
    """Semantic contract version used by portable knowledge shapes."""

    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, value: str | ContractVersion) -> ContractVersion:
        if isinstance(value, ContractVersion):
            return value
        major_text, minor_text = value.split(".", maxsplit=1)
        return cls(major=int(major_text), minor=int(minor_text))


GRAPH_CONTRACT_VERSION = ContractVersion(1, 0)
KNOWLEDGE_CONTRACT_VERSION = ContractVersion(1, 0)
EXTENSION_MANIFEST_CONTRACT_VERSION = ContractVersion(1, 0)


@dataclass(frozen=True)
class SourceRef:
    """Portable evidence reference for graph and knowledge facts."""

    source_id: str
    source_type: GraphSourceType
    path: str | None = None
    locator: str | None = None
    quote_preview: str | None = None
    confidence: GraphConfidence | float | None = None
    missing_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type.value,
            "path": self.path,
            "locator": self.locator,
            "quote_preview": self.quote_preview,
            "confidence": _enum_value(self.confidence),
            "missing_evidence": self.missing_evidence,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRef:
        confidence = data.get("confidence")
        return cls(
            source_id=data["source_id"],
            source_type=GraphSourceType(data["source_type"]),
            path=data.get("path"),
            locator=data.get("locator"),
            quote_preview=data.get("quote_preview"),
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            missing_evidence=bool(data.get("missing_evidence", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeSource:
    """A portable source record available to a knowledge space."""

    id: str
    space_id: str
    source_type: GraphSourceType
    title: str
    uri: str | None = None
    relative_path: str | None = None
    origin: GraphOrigin = GraphOrigin.MANUAL
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "source_type": self.source_type.value,
            "title": self.title,
            "uri": self.uri,
            "relative_path": self.relative_path,
            "origin": self.origin.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeSource:
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            source_type=GraphSourceType(data["source_type"]),
            title=data["title"],
            uri=data.get("uri"),
            relative_path=data.get("relative_path"),
            origin=GraphOrigin(data.get("origin", GraphOrigin.MANUAL.value)),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeObject:
    """A portable knowledge object within a knowledge space."""

    id: str
    space_id: str
    kind: str
    title: str
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: GraphConfidence | float | None = None
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    supersedes_object_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "source_refs": [_serialize(item) for item in self.source_refs],
            "confidence": _enum_value(self.confidence),
            "review_status": self.review_status.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "supersedes_object_id": self.supersedes_object_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeObject:
        confidence = data.get("confidence")
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            kind=data["kind"],
            title=data["title"],
            summary=data.get("summary"),
            tags=list(data.get("tags", [])),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            supersedes_object_id=data.get("supersedes_object_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeCollection:
    """A reviewable collection of knowledge objects."""

    id: str
    space_id: str
    title: str
    member_ids: list[str]
    tags: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "title": self.title,
            "member_ids": list(self.member_ids),
            "tags": list(self.tags),
            "source_refs": [_serialize(item) for item in self.source_refs],
            "review_status": self.review_status.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeCollection:
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            title=data["title"],
            member_ids=list(data.get("member_ids", [])),
            tags=list(data.get("tags", [])),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeLink:
    """A source-backed relation between two knowledge objects."""

    id: str
    space_id: str
    source_object_id: str
    target_object_id: str
    relation: str
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: GraphConfidence | float | None = None
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    evidence_strength: GraphEvidenceStrength = GraphEvidenceStrength.SUPPORTING
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    influences_decision: bool = False
    missing_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "source_object_id": self.source_object_id,
            "target_object_id": self.target_object_id,
            "relation": self.relation,
            "source_refs": [_serialize(item) for item in self.source_refs],
            "confidence": _enum_value(self.confidence),
            "review_status": self.review_status.value,
            "evidence_strength": self.evidence_strength.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "influences_decision": self.influences_decision,
            "missing_evidence": self.missing_evidence,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeLink:
        confidence = data.get("confidence")
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            source_object_id=data["source_object_id"],
            target_object_id=data["target_object_id"],
            relation=data["relation"],
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            evidence_strength=GraphEvidenceStrength(
                data.get("evidence_strength", GraphEvidenceStrength.SUPPORTING.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            influences_decision=bool(data.get("influences_decision", False)),
            missing_evidence=bool(data.get("missing_evidence", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeClaim:
    """A reviewable, provenance-backed claim in a knowledge space."""

    id: str
    space_id: str
    subject_object_id: str
    predicate: str
    object_object_id: str | None = None
    object_value: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: GraphConfidence | float | None = None
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    evidence_strength: GraphEvidenceStrength = GraphEvidenceStrength.SUPPORTING
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    influences_decision: bool = True
    missing_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "subject_object_id": self.subject_object_id,
            "predicate": self.predicate,
            "object_object_id": self.object_object_id,
            "object_value": self.object_value,
            "source_refs": [_serialize(item) for item in self.source_refs],
            "confidence": _enum_value(self.confidence),
            "review_status": self.review_status.value,
            "evidence_strength": self.evidence_strength.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "influences_decision": self.influences_decision,
            "missing_evidence": self.missing_evidence,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeClaim:
        confidence = data.get("confidence")
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            subject_object_id=data["subject_object_id"],
            predicate=data["predicate"],
            object_object_id=data.get("object_object_id"),
            object_value=data.get("object_value"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            evidence_strength=GraphEvidenceStrength(
                data.get("evidence_strength", GraphEvidenceStrength.SUPPORTING.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            influences_decision=bool(data.get("influences_decision", True)),
            missing_evidence=bool(data.get("missing_evidence", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class GraphNode:
    """A portable graph node that can represent knowledge outside OmniVia."""

    id: str
    space_id: str
    label: str
    kind: str
    object_id: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: GraphConfidence | float | None = None
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "label": self.label,
            "kind": self.kind,
            "object_id": self.object_id,
            "source_refs": [_serialize(item) for item in self.source_refs],
            "confidence": _enum_value(self.confidence),
            "review_status": self.review_status.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphNode:
        confidence = data.get("confidence")
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            label=data["label"],
            kind=data["kind"],
            object_id=data.get("object_id"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class GraphEdge:
    """A portable graph edge with provenance and review state."""

    id: str
    space_id: str
    source: str
    target: str
    relation: str
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: GraphConfidence | float | None = None
    review_status: GraphReviewStatus = GraphReviewStatus.UNREVIEWED
    evidence_strength: GraphEvidenceStrength = GraphEvidenceStrength.SUPPORTING
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    influences_decision: bool = False
    missing_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "source_refs": [_serialize(item) for item in self.source_refs],
            "confidence": _enum_value(self.confidence),
            "review_status": self.review_status.value,
            "evidence_strength": self.evidence_strength.value,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "influences_decision": self.influences_decision,
            "missing_evidence": self.missing_evidence,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphEdge:
        confidence = data.get("confidence")
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            source=data["source"],
            target=data["target"],
            relation=data["relation"],
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            confidence=(
                GraphConfidence(confidence)
                if isinstance(confidence, str) and confidence in GraphConfidence._value2member_map_
                else confidence
            ),
            review_status=GraphReviewStatus(
                data.get("review_status", GraphReviewStatus.UNREVIEWED.value)
            ),
            evidence_strength=GraphEvidenceStrength(
                data.get("evidence_strength", GraphEvidenceStrength.SUPPORTING.value)
            ),
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            influences_decision=bool(data.get("influences_decision", False)),
            missing_evidence=bool(data.get("missing_evidence", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class GraphFragment:
    """A backend-neutral graph fragment for portable knowledge."""

    id: str
    space_id: str
    contract_version: ContractVersion
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    origin: GraphOrigin = GraphOrigin.THIRD_PARTY
    owner: str = "portable"
    producer: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "contract_version": str(self.contract_version),
            "nodes": [_serialize(item) for item in self.nodes],
            "edges": [_serialize(item) for item in self.edges],
            "origin": self.origin.value,
            "owner": self.owner,
            "producer": self.producer,
            "source_refs": [_serialize(item) for item in self.source_refs],
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphFragment:
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            contract_version=ContractVersion.parse(data["contract_version"]),
            nodes=[GraphNode.from_dict(item) for item in data.get("nodes", [])],
            edges=[GraphEdge.from_dict(item) for item in data.get("edges", [])],
            origin=GraphOrigin(data.get("origin", GraphOrigin.THIRD_PARTY.value)),
            owner=data.get("owner", "portable"),
            producer=data.get("producer"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeExtensionManifest:
    """Declares safe extension kinds and relations for a namespace."""

    id: str
    contract_version: ContractVersion
    namespace: str
    title: str
    version: str
    object_kinds: list[str] = field(default_factory=list)
    node_kinds: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    official: bool = False
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_version": str(self.contract_version),
            "namespace": self.namespace,
            "title": self.title,
            "version": self.version,
            "object_kinds": list(self.object_kinds),
            "node_kinds": list(self.node_kinds),
            "relations": list(self.relations),
            "official": self.official,
            "description": self.description,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeExtensionManifest:
        return cls(
            id=data["id"],
            contract_version=ContractVersion.parse(data["contract_version"]),
            namespace=data["namespace"],
            title=data["title"],
            version=data["version"],
            object_kinds=list(data.get("object_kinds", [])),
            node_kinds=list(data.get("node_kinds", [])),
            relations=list(data.get("relations", [])),
            official=bool(data.get("official", False)),
            description=data.get("description"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class AgentGraphContext:
    """A bounded agent-facing context summary that preserves uncertainty."""

    id: str
    space_id: str
    summary: str
    object_ids: list[str] = field(default_factory=list)
    link_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    confidence_summary: dict[str, int] = field(default_factory=dict)
    review_summary: dict[str, int] = field(default_factory=dict)
    sensitivity_summary: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    missing_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "summary": self.summary,
            "object_ids": list(self.object_ids),
            "link_ids": list(self.link_ids),
            "claim_ids": list(self.claim_ids),
            "confidence_summary": dict(self.confidence_summary),
            "review_summary": dict(self.review_summary),
            "sensitivity_summary": dict(self.sensitivity_summary),
            "warnings": list(self.warnings),
            "source_refs": [_serialize(item) for item in self.source_refs],
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "missing_evidence": self.missing_evidence,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentGraphContext:
        return cls(
            id=data["id"],
            space_id=data["space_id"],
            summary=data["summary"],
            object_ids=list(data.get("object_ids", [])),
            link_ids=list(data.get("link_ids", [])),
            claim_ids=list(data.get("claim_ids", [])),
            confidence_summary=dict(data.get("confidence_summary", {})),
            review_summary=dict(data.get("review_summary", {})),
            sensitivity_summary=dict(data.get("sensitivity_summary", {})),
            warnings=list(data.get("warnings", [])),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            missing_evidence=bool(data.get("missing_evidence", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class KnowledgeSpace:
    """A portable knowledge space for personal, team, and agent contexts."""

    id: str
    title: str
    space_type: str
    contract_version: ContractVersion
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: list[KnowledgeSource] = field(default_factory=list)
    objects: list[KnowledgeObject] = field(default_factory=list)
    collections: list[KnowledgeCollection] = field(default_factory=list)
    links: list[KnowledgeLink] = field(default_factory=list)
    claims: list[KnowledgeClaim] = field(default_factory=list)
    graph_fragments: list[GraphFragment] = field(default_factory=list)
    extension_manifests: list[KnowledgeExtensionManifest] = field(default_factory=list)
    agent_contexts: list[AgentGraphContext] = field(default_factory=list)
    visibility: GraphVisibility = GraphVisibility.PRIVATE
    sensitivity: GraphSensitivity = GraphSensitivity.INTERNAL
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "space_type": self.space_type,
            "contract_version": str(self.contract_version),
            "summary": self.summary,
            "tags": list(self.tags),
            "sources": [_serialize(item) for item in self.sources],
            "objects": [_serialize(item) for item in self.objects],
            "collections": [_serialize(item) for item in self.collections],
            "links": [_serialize(item) for item in self.links],
            "claims": [_serialize(item) for item in self.claims],
            "graph_fragments": [_serialize(item) for item in self.graph_fragments],
            "extension_manifests": [_serialize(item) for item in self.extension_manifests],
            "agent_contexts": [_serialize(item) for item in self.agent_contexts],
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity.value,
            "metadata": _serialize_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeSpace:
        return cls(
            id=data["id"],
            title=data["title"],
            space_type=data["space_type"],
            contract_version=ContractVersion.parse(data["contract_version"]),
            summary=data.get("summary"),
            tags=list(data.get("tags", [])),
            sources=[KnowledgeSource.from_dict(item) for item in data.get("sources", [])],
            objects=[KnowledgeObject.from_dict(item) for item in data.get("objects", [])],
            collections=[
                KnowledgeCollection.from_dict(item) for item in data.get("collections", [])
            ],
            links=[KnowledgeLink.from_dict(item) for item in data.get("links", [])],
            claims=[KnowledgeClaim.from_dict(item) for item in data.get("claims", [])],
            graph_fragments=[GraphFragment.from_dict(item) for item in data.get("graph_fragments", [])],
            extension_manifests=[
                KnowledgeExtensionManifest.from_dict(item)
                for item in data.get("extension_manifests", [])
            ],
            agent_contexts=[
                AgentGraphContext.from_dict(item) for item in data.get("agent_contexts", [])
            ],
            visibility=GraphVisibility(data.get("visibility", GraphVisibility.PRIVATE.value)),
            sensitivity=GraphSensitivity(
                data.get("sensitivity", GraphSensitivity.INTERNAL.value)
            ),
            metadata=dict(data.get("metadata", {})),
        )
