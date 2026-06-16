"""Knowledge graph service layer.

Provides high-level operations for managing entities and relationships
in the knowledge graph. This service orchestrates the entity and relationship
repositories and applies business logic including validation and governance.

Governance:
- Agent-created entities and relationships start in "proposed" state
- Humans must approve proposed knowledge before it influences reasoning
- Source references are required to trace knowledge provenance
"""

from __future__ import annotations

from collections import deque
from typing import Any

from omnivia_memory.graph.models import (
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
)
from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository


class GraphServiceError(Exception):
    """Base exception for graph service errors."""

    pass


class EntityNotFoundError(GraphServiceError):
    """Raised when a requested entity does not exist."""

    pass


class RelationshipNotFoundError(GraphServiceError):
    """Raised when a requested relationship does not exist."""

    pass


class EntityValidationError(GraphServiceError):
    """Raised when entity validation fails."""

    pass


class GraphService:
    """High-level service for knowledge graph operations.

    Provides the main interface for managing graph entities and relationships,
    including creation, retrieval, and validation of connections.

    This service ensures:
    - Entities exist before creating relationships connecting them
    - Agent-created elements default to proposed state for human review
    - Source references are maintained for provenance

    Attributes:
        entity_repository: Repository for entity persistence
        relationship_repository: Repository for relationship persistence
    """

    def __init__(
        self,
        entity_repository: EntityRepository | None = None,
        relationship_repository: RelationshipRepository | None = None,
    ) -> None:
        self.entity_repository = entity_repository
        self.relationship_repository = relationship_repository

    def create_entity(
        self,
        name: str,
        entity_type: str | EntityType,
        source_id: str | None = None,
        created_by: str = "agent",
    ) -> Entity:
        """Create a new entity in the knowledge graph.

        Agent-created entities default to "proposed" state so humans can
        review and approve knowledge before it influences reasoning.

        Args:
            name: Human-readable name for the entity
            entity_type: Category of entity (can be EntityType enum or string)
            source_id: Reference to the source document that created this entity
            created_by: Whether human or agent (defaults to agent)

        Returns:
            The created entity with proposed governance state
        """
        # Convert string to EntityType if needed
        if isinstance(entity_type, str):
            entity_type = EntityType(entity_type)

        entity = Entity(
            name=name,
            entity_type=entity_type,
            source_id=source_id,
        )

        # Persist if repository is configured
        if self.entity_repository:
            self.entity_repository.create(entity)

        return entity

    def get_entity(self, entity_id: str) -> Entity:
        """Retrieve an entity by its ID.

        Args:
            entity_id: The unique identifier of the entity

        Returns:
            The entity

        Raises:
            EntityNotFoundError: If the entity doesn't exist
        """
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")

        entity = self.entity_repository.get(entity_id)
        if entity is None:
            raise EntityNotFoundError(f"Entity {entity_id} not found")
        return entity

    def list_entities(
        self,
        entity_type: str | EntityType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        """List entities with optional filtering.

        Args:
            entity_type: Optional filter by entity type
            limit: Maximum number of entities to return
            offset: Number of entities to skip

        Returns:
            List of entities matching the criteria
        """
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")

        if entity_type:
            # Convert string to EntityType if needed
            if isinstance(entity_type, str):
                entity_type = EntityType(entity_type)
            return self.entity_repository.list_by_type(entity_type)
        return self.entity_repository.list_all(limit=limit, offset=offset)

    def create_relationship(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str | RelationshipType,
        source_id: str | None = None,
        created_by: str = "agent",
        validate_entities: bool = True,
    ) -> Relationship:
        """Create a new relationship between two entities.

        Validates that both entities exist before creating the relationship.
        Agent-created relationships default to "proposed" state.

        Args:
            source_entity_id: ID of the entity where the relationship starts
            target_entity_id: ID of the entity where the relationship ends
            relationship_type: What kind of relationship (depends_on, related_to, etc.)
            source_id: Reference to the source document that created this connection
            created_by: Whether human or agent (defaults to agent)
            validate_entities: If True, verify both entities exist first

        Returns:
            The created relationship with proposed governance state

        Raises:
            EntityNotFoundError: If validate_entities is True and an entity is missing
        """
        # Validate that both entities exist before creating the relationship
        if validate_entities:
            if self.entity_repository:
                source_entity = self.entity_repository.get(source_entity_id)
                target_entity = self.entity_repository.get(target_entity_id)

                if source_entity is None:
                    raise EntityNotFoundError(f"Source entity {source_entity_id} not found")
                if target_entity is None:
                    raise EntityNotFoundError(f"Target entity {target_entity_id} not found")

        # Convert string to RelationshipType if needed
        if isinstance(relationship_type, str):
            relationship_type = RelationshipType(relationship_type)

        relationship = Relationship(
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relationship_type=relationship_type,
            source_id=source_id,
        )

        # Persist if repository is configured
        if self.relationship_repository:
            self.relationship_repository.create(relationship)

        return relationship

    def get_relationship(self, relationship_id: str) -> Relationship:
        """Retrieve a relationship by its ID.

        Args:
            relationship_id: The unique identifier of the relationship

        Returns:
            The relationship

        Raises:
            RelationshipNotFoundError: If the relationship doesn't exist
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")

        relationship = self.relationship_repository.get(relationship_id)
        if relationship is None:
            raise RelationshipNotFoundError(f"Relationship {relationship_id} not found")
        return relationship

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: str | RelationshipType | None = None,
    ) -> list[tuple[Entity, Relationship]]:
        """Get all entities directly connected to a given entity.

        Returns both incoming and outgoing relationships, enabling graph
        traversal for queries like "what does this entity depend on?"

        Args:
            entity_id: The ID of the entity to get neighbors for
            relationship_type: Optional filter by relationship type

        Returns:
            List of (entity, relationship) tuples for all connected entities
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")

        if relationship_type:
            # Convert string to RelationshipType if needed
            if isinstance(relationship_type, str):
                relationship_type = RelationshipType(relationship_type)
            # Filter neighbors by type
            all_neighbors = self.relationship_repository.get_neighbors(entity_id)
            return [
                (entity, rel)
                for entity, rel in all_neighbors
                if rel.relationship_type == relationship_type
            ]
        return self.relationship_repository.get_neighbors(entity_id)

    def get_entity_relationships(self, entity_id: str) -> list[Relationship]:
        """Get all relationships connected to a given entity.

        Returns both relationships where the entity is the source and where
        it is the target.

        Args:
            entity_id: The ID of the entity to get relationships for

        Returns:
            List of relationships connected to the entity
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")

        return self.relationship_repository.get_by_entity(entity_id)

    def get_backlinks(
        self,
        entity_id: str,
        relationship_type: str | RelationshipType | None = None,
    ) -> list[tuple[Entity, Relationship]]:
        """Get the entities that link *to* a given entity (its backlinks).

        Backlinks are the inverse of outgoing links: they answer "what points
        here?". Each result pairs the linking (source) entity with the
        relationship that connects it to ``entity_id``.

        Args:
            entity_id: The ID of the entity to collect backlinks for
            relationship_type: Optional filter by relationship type

        Returns:
            List of (source_entity, relationship) tuples, one per incoming
            relationship whose source entity still exists, in the order returned
            by the repository

        Raises:
            GraphServiceError: If repositories are not configured
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")

        incoming = self.relationship_repository.get_incoming(entity_id)

        if relationship_type is not None:
            # Convert string to RelationshipType if needed
            if isinstance(relationship_type, str):
                relationship_type = RelationshipType(relationship_type)
            incoming = [
                rel for rel in incoming if rel.relationship_type == relationship_type
            ]

        backlinks: list[tuple[Entity, Relationship]] = []
        for rel in incoming:
            source_entity = self.entity_repository.get(rel.source_entity_id)
            if source_entity is not None:
                backlinks.append((source_entity, rel))

        return backlinks

    def find_path(
        self,
        source_entity_id: str,
        target_entity_id: str,
        max_depth: int = 5,
    ) -> list[Entity] | None:
        """Find the shortest path between two entities.

        Performs a breadth-first search over relationships, treating the graph
        as undirected (a relationship can be traversed from either end). This
        answers "how is A connected to B?" — the spine of cross-entity
        reasoning.

        Args:
            source_entity_id: ID of the entity to start from
            target_entity_id: ID of the entity to reach
            max_depth: Maximum number of hops to explore (default 5). A value
                of 0 only allows a source-to-self path.

        Returns:
            The shortest path as an ordered list of entities from source to
            target (inclusive), or None if no path exists within max_depth. A
            path from an entity to itself is a single-element list.

        Raises:
            GraphServiceError: If repositories are not configured
            EntityNotFoundError: If either endpoint entity does not exist
            ValueError: If max_depth is negative
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")

        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")

        source_entity = self.entity_repository.get(source_entity_id)
        if source_entity is None:
            raise EntityNotFoundError(f"Source entity {source_entity_id} not found")
        target_entity = self.entity_repository.get(target_entity_id)
        if target_entity is None:
            raise EntityNotFoundError(f"Target entity {target_entity_id} not found")

        if source_entity_id == target_entity_id:
            return [source_entity]

        # Breadth-first search returns the first path found to the target, which
        # is guaranteed to be a shortest path. Entities are marked visited when
        # enqueued so each node is expanded once.
        queue: deque[list[Entity]] = deque([[source_entity]])
        visited: set[str] = {source_entity_id}

        while queue:
            path = queue.popleft()

            # Number of hops taken so far; stop expanding at the depth limit.
            if len(path) - 1 >= max_depth:
                continue

            current = path[-1]
            for neighbor_entity, _rel in self.relationship_repository.get_neighbors(
                current.id
            ):
                if neighbor_entity.id in visited:
                    continue
                next_path = path + [neighbor_entity]
                if neighbor_entity.id == target_entity_id:
                    return next_path
                visited.add(neighbor_entity.id)
                queue.append(next_path)

        return None

    def link_to_memory(
        self,
        entity_id: str,
        memory_id: str,
        source_id: str,
    ) -> bool:
        """Link an entity to the memory that provided its knowledge.

        This tracks the provenance chain from memory to entity, enabling
        traceability: "what memory led to this entity being created?"

        Args:
            entity_id: The entity to link
            memory_id: The memory to link to
            source_id: Reference to the source document

        Returns:
            True if link was created

        Raises:
            EntityNotFoundError: If the entity doesn't exist
        """
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")

        # Validate entity exists
        entity = self.entity_repository.get(entity_id)
        if entity is None:
            raise EntityNotFoundError(f"Entity {entity_id} not found")

        return self.relationship_repository.link_to_memory(
            entity_id=entity_id,
            memory_id=memory_id,
            source_id=source_id,
        )

    def approve_entity(self, entity_id: str) -> Entity:
        """Approve an entity (transition to approved state).

        Args:
            entity_id: The ID of the entity to approve

        Returns:
            The approved entity

        Raises:
            EntityNotFoundError: If the entity doesn't exist
            EntityValidationError: If the entity cannot be approved
        """
        entity = self.get_entity(entity_id)

        if entity.approval_status.value == "rejected":
            raise EntityValidationError("Cannot approve entity in rejected state")

        if entity.approval_status.value == "approved":
            raise EntityValidationError("Entity is already approved")

        entity.approve()

        if self.entity_repository:
            self.entity_repository.update(entity)

        return entity

    def approve_relationship(self, relationship_id: str) -> Relationship:
        """Approve a relationship (transition to approved state).

        Args:
            relationship_id: The ID of the relationship to approve

        Returns:
            The approved relationship

        Raises:
            RelationshipNotFoundError: If the relationship doesn't exist
        """
        relationship = self.get_relationship(relationship_id)

        relationship.approve()

        if self.relationship_repository:
            self.relationship_repository.update(relationship)

        return relationship

    def reject_entity(self, entity_id: str) -> Entity:
        """Reject an entity (transition to rejected state).

        Args:
            entity_id: The ID of the entity to reject

        Returns:
            The rejected entity

        Raises:
            EntityNotFoundError: If the entity doesn't exist
            EntityValidationError: If the entity cannot be rejected
        """
        entity = self.get_entity(entity_id)

        if entity.approval_status.value == "rejected":
            raise EntityValidationError("Entity is already rejected")

        entity.reject()

        if self.entity_repository:
            self.entity_repository.update(entity)

        return entity

    def reject_relationship(self, relationship_id: str) -> Relationship:
        """Reject a relationship (transition to rejected state).

        Args:
            relationship_id: The ID of the relationship to reject

        Returns:
            The rejected relationship

        Raises:
            RelationshipNotFoundError: If the relationship doesn't exist
        """
        relationship = self.get_relationship(relationship_id)

        relationship.reject()

        if self.relationship_repository:
            self.relationship_repository.update(relationship)

        return relationship

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity and all its relationships.

        Args:
            entity_id: The ID of the entity to delete

        Returns:
            True if the entity was deleted

        Raises:
            EntityNotFoundError: If the entity doesn't exist
        """
        if not self.entity_repository:
            raise GraphServiceError("Entity repository not configured")

        # Verify entity exists
        self.get_entity(entity_id)

        return self.entity_repository.delete(entity_id)

    def delete_relationship(self, relationship_id: str) -> bool:
        """Delete a relationship.

        Args:
            relationship_id: The ID of the relationship to delete

        Returns:
            True if the relationship was deleted

        Raises:
            RelationshipNotFoundError: If the relationship doesn't exist
        """
        if not self.relationship_repository:
            raise GraphServiceError("Relationship repository not configured")

        # Verify relationship exists
        self.get_relationship(relationship_id)

        return self.relationship_repository.delete(relationship_id)

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the knowledge graph.

        Returns:
            Dictionary with counts of entities and relationships
        """
        stats: dict[str, Any] = {"entities": 0, "relationships": 0}

        if self.entity_repository:
            stats["entities"] = self.entity_repository.count()

        if self.relationship_repository:
            stats["relationships"] = self.relationship_repository.count()

        return stats
