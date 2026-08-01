"""Knowledge graph domain models for OmniVia.

Provides entity and relationship models for representing structured
knowledge as a graph. Entities are nodes; relationships are directed edges
connecting entities with typed relationships.

Agent-created graph elements default to "proposed" status so human review
is required before they influence agent reasoning. This prevents an AI
from silently creating authoritative connections in the knowledge graph.
"""

# ruff: noqa: UP017
# UP017 would rewrite `from datetime import timezone` / `datetime.now(timezone.utc)`
# to `from datetime import UTC` / `datetime.now(UTC)`. This module is a 1:1 port of
# `omnivia_memory.graph.models` and tests/canonical_migration/test_parity.py gates it
# on two things that rewrite would break:
#   - module-scope *namespace* parity: `timezone` would disappear from, and `UTC`
#     appear in, the set of public names this module binds.
#   - normalized *AST* parity: `ast.dump` of this module must equal `ast.dump` of the
#     legacy module with only `omnivia_memory` -> `omnivia_core` rewritten, so the
#     `Attribute(value=Name('timezone'), attr='utc')` nodes must survive verbatim.
# Comments are the only thing those gates cannot see, so the suppression is the fix;
# modernizing the spelling has to land in the legacy source first.

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    """Approval state for graph entities and relationships.

    AI-created graph elements start as "proposed" so humans must verify
    connections before they become authoritative. This prevents an agent
    from silently turning unverified observations into approved knowledge.

    State transitions:
        proposed -> approved (human verifies the connection is correct)
        proposed -> rejected (human determines connection is wrong)
        proposed -> superseded (a better relationship replaces this one)
        approved -> superseded (newer knowledge renders this outdated)
        approved -> rejected (later evidence disproves the connection)
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

    def is_approved(self) -> bool:
        """Check if this status represents verified knowledge."""
        return self == ApprovalStatus.APPROVED

    def is_active(self) -> bool:
        """Check if this element should be considered in queries.

        Only approved elements are authoritative. Proposed elements
        are pending review, and superseded/rejected are inactive.
        """
        return self == ApprovalStatus.APPROVED


class EntityType(str, Enum):
    """Categories for knowledge graph entities.

    Entity types help structure the graph and enable typed queries.
    When unsure, use CONCEPT for abstract ideas or PERSON for people.
    """

    PERSON = "person"
    ORGANIZATION = "organization"
    CONCEPT = "concept"
    DOCUMENT = "document"
    PROJECT = "project"
    SYSTEM = "system"
    PRODUCT = "product"
    LOCATION = "location"
    TECHNOLOGY = "technology"
    EVENT = "event"
    CUSTOM = "custom"


class RelationshipType(str, Enum):
    """Types of connections between entities.

    Relationships are directed (source -> target). Choose the most
    specific type that accurately describes the connection.

    Hierarchy guide:
        - DEPENDS_ON: A requires B to function (technical)
        - PART_OF: A is contained within B (containment)
        - IMPLEMENTS: A realizes B (realization)
        - USES: A leverages B for its purpose (usage)
        - KNOWS: A is aware of B (awareness)
        - RELATES_TO: general connection (fallback)
    """

    RELATES_TO = "relates_to"
    DEPENDS_ON = "depends_on"
    PART_OF = "part_of"
    USES = "uses"
    IMPLEMENTS = "implements"
    CREATED_BY = "created_by"
    MAINTAINS = "maintains"
    KNOWS = "knows"
    LEADS = "leads"
    OCCURS_IN = "occurs_in"


@dataclass
class Entity:
    """A node in the knowledge graph.

    Entities are the nouns of the graph - people, projects, concepts,
    systems or any thing worth tracking. Each entity must have a name
    and type, with optional source evidence and governance status.

    Agent-created entities default to "proposed" status so human review
    is required before they become authoritative knowledge.

    Attributes:
        id: Unique identifier for this entity
        name: Human-readable name (required)
        entity_type: Category for filtering and typed queries
        source_id: Reference to the evidence/source document
        approval_status: Governance state (proposed/approved/rejected/superseded)
        created_at: When this entity was created (ISO 8601 timestamp)
        updated_at: When this entity was last modified (ISO 8601 timestamp)
    """

    name: str
    entity_type: EntityType
    source_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    approval_status: ApprovalStatus = ApprovalStatus.PROPOSED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert entity to dictionary for serialization.

        Returns:
            Dictionary representation of the entity
        """
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type.value,
            "source_id": self.source_id,
            "approval_status": self.approval_status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """Create an entity from a dictionary.

        Args:
            data: Dictionary with entity fields

        Returns:
            Entity instance
        """
        return cls(
            id=data["id"],
            name=data["name"],
            entity_type=EntityType(data["entity_type"]),
            source_id=data.get("source_id"),
            approval_status=ApprovalStatus(data["approval_status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def approve(self) -> None:
        """Mark this entity as approved by human review."""
        self.approval_status = ApprovalStatus.APPROVED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def reject(self) -> None:
        """Mark this entity as rejected."""
        self.approval_status = ApprovalStatus.REJECTED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def supersede(self) -> None:
        """Mark this entity as superseded by newer knowledge."""
        self.approval_status = ApprovalStatus.SUPERSEDED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def touch(self) -> None:
        """Update the updated_at timestamp without changing content."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Entity):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return (
            f"Entity(id={self.id[:8]}..., name={self.name}, "
            f"type={self.entity_type.value}, status={self.approval_status.value})"
        )


@dataclass
class Relationship:
    """A typed connection between two entities in the knowledge graph.

    Relationships are directed edges connecting a source entity to a target
    entity with a specific relationship type. For example, "Person A" KNOWS
    "Person B" or "Project X" DEPENDS_ON "System Y".

    Agent-created relationships default to "proposed" so human review
    validates connections before they influence agent reasoning.

    Attributes:
        id: Unique identifier for this relationship
        source_entity_id: ID of the entity where this connection originates
        target_entity_id: ID of the entity being connected to
        relationship_type: Semantic meaning of this connection
        source_id: Reference to the evidence/source document
        approval_status: Governance state (proposed/approved/rejected/superseded)
        created_at: When this relationship was created (ISO 8601 timestamp)
        updated_at: When this relationship was last modified (ISO 8601 timestamp)
    """

    source_entity_id: str
    target_entity_id: str
    relationship_type: RelationshipType
    source_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    approval_status: ApprovalStatus = ApprovalStatus.PROPOSED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert relationship to dictionary for serialization.

        Returns:
            Dictionary representation of the relationship
        """
        return {
            "id": self.id,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "relationship_type": self.relationship_type.value,
            "source_id": self.source_id,
            "approval_status": self.approval_status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Relationship:
        """Create a relationship from a dictionary.

        Args:
            data: Dictionary with relationship fields

        Returns:
            Relationship instance
        """
        return cls(
            id=data["id"],
            source_entity_id=data["source_entity_id"],
            target_entity_id=data["target_entity_id"],
            relationship_type=RelationshipType(data["relationship_type"]),
            source_id=data.get("source_id"),
            approval_status=ApprovalStatus(data["approval_status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def approve(self) -> None:
        """Mark this relationship as approved by human review."""
        self.approval_status = ApprovalStatus.APPROVED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def reject(self) -> None:
        """Mark this relationship as rejected."""
        self.approval_status = ApprovalStatus.REJECTED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def supersede(self) -> None:
        """Mark this relationship as superseded by newer knowledge."""
        self.approval_status = ApprovalStatus.SUPERSEDED
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def touch(self) -> None:
        """Update the updated_at timestamp without changing content."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Relationship):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return (
            f"Relationship(id={self.id[:8]}..., "
            f"{self.relationship_type.value}, status={self.approval_status.value})"
        )


@dataclass
class EntityCreate:
    """Input model for creating a new entity.

    Attributes:
        name: Human-readable name for the entity
        entity_type: Category of entity
        source_id: Reference to the evidence/source document
        properties: Optional metadata dictionary
    """

    name: str
    entity_type: str
    provenance_source_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationshipCreate:
    """Input model for creating a new relationship.

    Attributes:
        source_entity_id: ID of the source entity
        target_entity_id: ID of the target entity
        relationship_type: Type of relationship
        source_id: Reference to the evidence/source document
        properties: Optional metadata dictionary
    """

    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    provenance_source_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityMemoryLink:
    """Links an entity to the memory that provided its knowledge.

    Tracks the provenance chain from memory to entity, enabling
    traceability: "what memory led to this entity being created?"

    Attributes:
        entity_id: The linked entity
        memory_id: The linked memory
        source_id: Reference to the source document
        created_at: When the link was created
    """

    entity_id: str
    memory_id: str
    source_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
