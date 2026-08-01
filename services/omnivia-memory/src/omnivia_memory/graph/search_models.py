"""Compatibility facade for the GraphRAG search records, plus legacy scoring.

Deprecated: import ``GraphSearchQuery`` / ``GraphSearchResult`` /
``GraphSearchResultSet`` from ``omnivia_core.graph.search_models`` instead.

This leaf is a *split* facade rather than a plain one. The three query/result
records are canonicalized, so they -- and every incidental binding this module's
historical namespace publishes -- are re-exported as the exact
``omnivia_core.graph.search_models`` objects. The four relevance-scoring helpers
below (``score_name_match``, ``score_relationship_count``,
``score_neighbor_overlap``, ``compute_relevance_score``) are deliberately *not*
in Core: they are search-runtime behaviour, they are consumed by the
legacy-owned ``omnivia_memory.graph.search_service``, and they stay defined here,
unchanged, until that runtime has somewhere else to live.

``from __future__ import annotations`` below is the real future statement, not an
``annotations`` binding re-exported from the canonical module: the retained
helper signatures are compared as postponed string annotations, and only the
real statement makes them so.
"""

from __future__ import annotations

# ruff: noqa: F401 -- the names below are re-exported, not used in this module
# This half of the module *is* the re-export: strict consumers must see every
# name below as explicitly exported from here, exactly as they did before
# conversion. The hybrid `graph` barrel and the legacy-owned
# `graph.search_service` both take their records from here.
# mypy: implicit_reexport = True
# ruff: noqa: C401 -- `score_neighbor_overlap` builds its two comparison sets with
# `set(n.lower() for n in ...)` rather than a set comprehension. That body is
# frozen legacy source preserved verbatim, and
# tests/compatibility/test_facade_foundation.py compares it against the frozen
# baseline descriptor, so modernizing the spelling has to land as its own change
# to the runtime-owned half -- not as a side effect of this conversion.
#
# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings this leaf's historical namespace still has to
# resolve: `Any`, `dataclass`, `field`, and the three `Entity` / `EntityType` /
# `RelationshipType` names the canonical records import from their sibling
# `omnivia_core.graph.models` leaf. No other error code is suppressed.
from omnivia_core.graph.search_models import (  # type: ignore[attr-defined]
    Any,
    Entity,
    EntityType,
    GraphSearchQuery,
    GraphSearchResult,
    GraphSearchResultSet,
    RelationshipType,
    dataclass,
    field,
)

# ---------------------------------------------------------------------------
# Relevance Scoring Helpers
# ---------------------------------------------------------------------------
# These helper functions compute relevance scores for GraphRAG search.
# Scores combine multiple signals: name match quality, relationship
# density, and neighbor overlap with the query intent.


def score_name_match(query: str, entity_name: str) -> float:
    """Score how well an entity name matches the query text.

    Uses case-insensitive substring matching. A perfect match (query
    equals name) scores 1.0, while a partial match scores proportionally
    to the match length relative to the query.

    This provides a baseline signal that can be combined with other
    signals for a composite relevance score.

    Args:
        query: The search query text
        entity_name: The name of the entity to score

    Returns:
        Match score between 0.0 and 1.0
    """
    if not query or not entity_name:
        return 0.0

    query_lower = query.lower()
    name_lower = entity_name.lower()

    # Exact match gets full score
    if query_lower == name_lower:
        return 1.0

    # Substring match gets partial score based on coverage
    if query_lower in name_lower:
        return len(query_lower) / len(name_lower)

    # Check for word-level overlap
    query_words = set(query_lower.split())
    name_words = set(name_lower.split())

    if query_words and name_words:
        overlap = len(query_words & name_words)
        return overlap / len(query_words)

    return 0.0


def score_relationship_count(outgoing: int, incoming: int) -> float:
    """Score based on the number of relationships an entity has.

    Entities with more connections are often more central to the
    knowledge graph and thus potentially more relevant. However,
    we cap the score to avoid overly weighting highly-connected
    "hub" entities that might be too general.

    Args:
        outgoing: Number of outgoing (source) relationships
        incoming: Number of incoming (target) relationships

    Returns:
        Relationship density score between 0.0 and 1.0
    """
    total = outgoing + incoming

    # Score increases logarithmically, capped at 1.0
    # 10 relationships = ~0.5, 100 = ~0.7, 1000 = ~0.8
    if total <= 0:
        return 0.0

    import math

    return min(1.0, math.log2(total + 1) / 10.0)


def score_neighbor_overlap(neighbors: list[str], query_keywords: list[str]) -> float:
    """Score based on overlap between entity neighbors and query keywords.

    When neighboring entities share keywords with the query, the matched
    entity is likely more contextually relevant. This captures the
    "surrounding context" signal in graph traversal.

    Args:
        neighbors: List of names of neighboring entities
        query_keywords: Extracted keywords from the search query

    Returns:
        Neighbor overlap score between 0.0 and 1.0
    """
    if not neighbors or not query_keywords:
        return 0.0

    # Convert neighbors to lowercase for comparison
    neighbor_set = set(n.lower() for n in neighbors)
    keyword_set = set(k.lower() for k in query_keywords)

    overlap = len(neighbor_set & keyword_set)
    return overlap / len(keyword_set) if keyword_set else 0.0


def compute_relevance_score(
    query: str,
    entity_name: str,
    outgoing_relationships: int = 0,
    incoming_relationships: int = 0,
    neighbor_names: list[str] | None = None,
    name_weight: float = 0.5,
    relationship_weight: float = 0.25,
    neighbor_weight: float = 0.25,
) -> float:
    """Compute a composite relevance score for a GraphRAG result.

    Combines multiple signals into a single score that reflects how
    well an entity matches the search query:
    - Name match: How well the entity name matches the query text
    - Relationship count: How connected the entity is in the graph
    - Neighbor overlap: How many neighboring entities share query keywords

    Weights are normalized to sum to 1.0, so the weights parameter
    is only used to distribute importance across signals.

    Args:
        query: The search query text
        entity_name: The name of the entity being scored
        outgoing_relationships: Count of outgoing relationships
        incoming_relationships: Count of incoming relationships
        neighbor_names: Names of neighboring entities for context scoring
        name_weight: Weight for name match signal (default 0.5)
        relationship_weight: Weight for relationship count signal (default 0.25)
        neighbor_weight: Weight for neighbor overlap signal (default 0.25)

    Returns:
        Composite relevance score between 0.0 and 1.0
    """
    # Normalize weights to sum to 1.0
    total_weight = name_weight + relationship_weight + neighbor_weight
    if total_weight <= 0:
        return 0.0

    name_weight /= total_weight
    relationship_weight /= total_weight
    neighbor_weight /= total_weight

    # Extract keywords from query for neighbor matching
    query_keywords = query.lower().split()

    # Compute individual scores
    name_score = score_name_match(query, entity_name)
    rel_score = score_relationship_count(outgoing_relationships, incoming_relationships)
    neighbor_score = score_neighbor_overlap(neighbor_names or [], query_keywords)

    # Weighted combination
    composite = (
        name_score * name_weight
        + rel_score * relationship_weight
        + neighbor_score * neighbor_weight
    )

    return round(composite, 4)
