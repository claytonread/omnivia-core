"""GraphRAG search models for OmniVia.

Provides query and result models for graph-based retrieval augmented
generation. These models support searching the knowledge graph by
entity types, relationship types, and traversal depth.

GraphRAG enables agents to find connected knowledge beyond what
keyword or vector search alone can discover. For example, an agent
can find "all people who depend on the system that handles payments"
in a single query.

Only the query/result record definitions are canonicalized here. The
relevance-scoring helpers (``score_name_match``, ``score_relationship_count``,
``score_neighbor_overlap``, ``compute_relevance_score``) remain runtime-owned
in ``omnivia_memory.graph.search_models`` and must not enter Core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omnivia_core.graph.models import Entity, EntityType, RelationshipType


@dataclass
class GraphSearchQuery:
    """A query specification for searching the knowledge graph.

    GraphRAG queries traverse the graph from matching starting nodes,
    following relationships up to a specified depth. This enables
    finding connected entities that share indirect relationships.

    Example use case: "Find all technologies used by projects that
    depend on the authentication system, up to 2 hops away."

    Attributes:
        query: Natural language text describing what to find.
            This is used for semantic matching against entity names.
        entity_types: Filter to only include entities of these types.
            Empty list means no type filter (search all types).
        relationship_types: Filter to only follow these relationship
            types during traversal. Empty list means follow all types.
        depth: How many relationship hops to traverse from matched
            starting entities. Depth1 means immediate neighbors only.
        limit: Maximum number of results to return. Use None for
            service-defined default limit.
    """

    query: str
    entity_types: list[EntityType] = field(default_factory=list)
    relationship_types: list[RelationshipType] = field(default_factory=list)
    depth: int = 1
    limit: int | None = None

    def __post_init__(self) -> None:
        """Validate query parameters after initialization."""
        if self.depth < 0:
            raise ValueError("Depth must be non-negative")
        if self.limit is not None and self.limit < 1:
            raise ValueError("Limit must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        """Convert query to dictionary for serialization.

        Returns:
            Dictionary representation of the query
        """
        return {
            "query": self.query,
            "entity_types": [t.value for t in self.entity_types],
            "relationship_types": [t.value for t in self.relationship_types],
            "depth": self.depth,
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphSearchQuery:
        """Create a query from a dictionary.

        Args:
            data: Dictionary with query fields

        Returns:
            GraphSearchQuery instance
        """
        return cls(
            query=data["query"],
            entity_types=[EntityType(t) for t in data.get("entity_types", [])],
            relationship_types=[RelationshipType(t) for t in data.get("relationship_types", [])],
            depth=data.get("depth", 1),
            limit=data.get("limit"),
        )


@dataclass
class GraphSearchResult:
    """A single result from a GraphRAG search.

    Contains a matched entity along with relevance scoring and
    contextual information about related entities found during
    graph traversal.

    Attributes:
        entity: The matched entity that satisfied the search query
        score: Relevance score between 0.0 (not relevant) and 1.0
            (perfect match). Based on name match, relationship count,
            and neighbor overlap with the query.
        matched_on: What caused this entity to match. Typically
            the entity name or a relationship path description.
        context_entities: Other entities found during traversal that
            are connected to the matched entity. These provide
            surrounding context for the result.
    """

    entity: Entity
    score: float
    matched_on: str
    context_entities: list[Entity] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate result fields after initialization."""
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("Score must be between 0.0 and 1.0")

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for serialization.

        Returns:
            Dictionary representation of the result
        """
        return {
            "entity": self.entity.to_dict(),
            "score": self.score,
            "matched_on": self.matched_on,
            "context_entities": [e.to_dict() for e in self.context_entities],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphSearchResult:
        """Create a result from a dictionary.

        Args:
            data: Dictionary with result fields

        Returns:
            GraphSearchResult instance
        """
        return cls(
            entity=Entity.from_dict(data["entity"]),
            score=data["score"],
            matched_on=data["matched_on"],
            context_entities=[Entity.from_dict(e) for e in data.get("context_entities", [])],
        )


@dataclass
class GraphSearchResultSet:
    """A collection of GraphRAG search results with query metadata.

    Provides a complete response to a GraphSearchQuery, including
    all matched results, total count information, and a reference
    back to the original query for context.

    Attributes:
        results: The ordered list of results, sorted by score
            (highest relevance first)
        total_count: Total number of entities that matched the query,
            before applying the limit. Useful for pagination UI.
        query: The original search query that produced these results.
            Useful for result context and debugging.
    """

    results: list[GraphSearchResult]
    total_count: int
    query: GraphSearchQuery

    def to_dict(self) -> dict[str, Any]:
        """Convert result set to dictionary for serialization.

        Returns:
            Dictionary representation of the result set
        """
        return {
            "results": [r.to_dict() for r in self.results],
            "total_count": self.total_count,
            "query": self.query.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphSearchResultSet:
        """Create a result set from a dictionary.

        Args:
            data: Dictionary with result set fields

        Returns:
            GraphSearchResultSet instance
        """
        return cls(
            results=[GraphSearchResult.from_dict(r) for r in data["results"]],
            total_count=data["total_count"],
            query=GraphSearchQuery.from_dict(data["query"]),
        )
