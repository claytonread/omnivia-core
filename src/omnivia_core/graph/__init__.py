"""Canonical portable contract surface for graph entity and search models."""

from __future__ import annotations

from omnivia_core.graph.models import (
    ApprovalStatus,
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
)
from omnivia_core.graph.search_models import (
    GraphSearchQuery,
    GraphSearchResult,
    GraphSearchResultSet,
)

__all__ = [
    "ApprovalStatus",
    "Entity",
    "EntityType",
    "GraphSearchQuery",
    "GraphSearchResult",
    "GraphSearchResultSet",
    "Relationship",
    "RelationshipType",
]
