"""Strict-mypy consumer fixture for the Graph compatibility facades.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for the two Graph leaves: it imports all eleven routed symbols from
``omnivia_memory.graph.models`` and ``omnivia_memory.graph.search_models`` (never
from ``omnivia_core``) and proves the facades re-export usefully typed objects --
the exact canonical types, not ``Any`` -- via ``typing.assert_type``.

The Graph domain gets its own fixture rather than joining
``accepted_legacy_facade_consumer.py`` because ``graph.search_models`` is the
migration's first *split* facade: three of its records are canonicalized while
four relevance-scoring helpers stay defined on the legacy module. A consumer that
sees both halves through one legacy import path is what proves the split is
invisible to a typed caller. The consumer partition is enforced by
``tests/test_typed_facade_consumers.py``.

The four helpers are deliberately exercised through a *module* import
(``runtime_search_models``) rather than a ``from ... import``: that audit compares
each fixture's legacy ``from`` imports against ``baseline.inventory``'s
``FACADE_ROUTES`` for an exact match, and the helpers are not routed -- they are
still owned by the legacy module. Naming them in a ``from`` import would make this
fixture claim four routes that do not exist.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If a facade ever stopped explicitly
re-exporting these names, degraded them to ``Any``, or moved a retained helper
into Core, strict mypy would fail here.
"""

from typing import Any, assert_type

import omnivia_memory.graph.search_models as runtime_search_models
from omnivia_memory.graph.models import (
    ApprovalStatus,
    Entity,
    EntityCreate,
    EntityMemoryLink,
    EntityType,
    Relationship,
    RelationshipCreate,
    RelationshipType,
)
from omnivia_memory.graph.search_models import (
    GraphSearchQuery,
    GraphSearchResult,
    GraphSearchResultSet,
)


def build_entity() -> Entity:
    """Construct a graph node through the facade's own types."""
    entity = Entity(
        name="Alice",
        entity_type=EntityType.PERSON,
        source_id="source-1",
        id="entity-1",
        approval_status=ApprovalStatus.PROPOSED,
    )
    assert_type(entity.name, str)
    assert_type(entity.entity_type, EntityType)
    assert_type(entity.source_id, str | None)
    assert_type(entity.id, str)
    assert_type(entity.approval_status, ApprovalStatus)
    assert_type(entity.created_at, str)
    assert_type(entity.updated_at, str)
    assert_type(entity.to_dict(), dict[str, Any])
    assert_type(entity.approve(), None)
    assert_type(entity.reject(), None)
    assert_type(entity.supersede(), None)
    assert_type(entity.touch(), None)
    return entity


def build_relationship() -> Relationship:
    """The same, for a directed edge."""
    relationship = Relationship(
        source_entity_id="entity-1",
        target_entity_id="entity-2",
        relationship_type=RelationshipType.DEPENDS_ON,
        source_id="source-1",
        id="relationship-1",
    )
    assert_type(relationship.source_entity_id, str)
    assert_type(relationship.target_entity_id, str)
    assert_type(relationship.relationship_type, RelationshipType)
    assert_type(relationship.source_id, str | None)
    assert_type(relationship.approval_status, ApprovalStatus)
    assert_type(relationship.to_dict(), dict[str, Any])
    return relationship


def approval_status_predicates() -> tuple[bool, bool]:
    """The status enum keeps its two predicate methods and its ``str`` mixin."""
    status = ApprovalStatus.APPROVED
    assert_type(status.value, str)
    assert_type(status.is_approved(), bool)
    assert_type(status.is_active(), bool)
    return status.is_approved(), status.is_active()


def build_inputs() -> tuple[EntityCreate, RelationshipCreate, EntityMemoryLink]:
    """The three provenance-carrying input records."""
    entity_input = EntityCreate(
        name="Alice",
        entity_type=EntityType.PERSON.value,
        provenance_source_id="source-1",
        properties={"role": "maintainer"},
    )
    relationship_input = RelationshipCreate(
        source_entity_id="entity-1",
        target_entity_id="entity-2",
        relationship_type=RelationshipType.KNOWS.value,
        properties={"since": 2026},
    )
    link = EntityMemoryLink(
        entity_id="entity-1",
        memory_id="memory-1",
        source_id="source-1",
    )
    assert_type(entity_input.entity_type, str)
    assert_type(entity_input.provenance_source_id, str | None)
    assert_type(entity_input.properties, dict[str, Any])
    assert_type(relationship_input.relationship_type, str)
    assert_type(relationship_input.properties, dict[str, Any])
    assert_type(link.entity_id, str)
    assert_type(link.memory_id, str)
    assert_type(link.source_id, str | None)
    assert_type(link.created_at, str)
    return entity_input, relationship_input, link


def build_query() -> GraphSearchQuery:
    """The split facade's query record, composed out of the sibling leaf's enums."""
    query = GraphSearchQuery(
        query="alice",
        entity_types=[EntityType.PERSON, EntityType.ORGANIZATION],
        relationship_types=[RelationshipType.KNOWS],
        depth=2,
        limit=10,
    )
    assert_type(query.query, str)
    assert_type(query.entity_types, list[EntityType])
    assert_type(query.relationship_types, list[RelationshipType])
    assert_type(query.depth, int)
    assert_type(query.limit, int | None)
    assert_type(query.to_dict(), dict[str, Any])
    assert_type(GraphSearchQuery.from_dict(query.to_dict()), GraphSearchQuery)
    return query


def build_result_set() -> GraphSearchResultSet:
    """The other two records, round-tripped through the facade."""
    entity = build_entity()
    result = GraphSearchResult(
        entity=entity,
        score=0.5,
        matched_on="name",
        context_entities=[entity],
    )
    result_set = GraphSearchResultSet(
        results=[result],
        total_count=1,
        query=build_query(),
    )
    assert_type(result.entity, Entity)
    assert_type(result.score, float)
    assert_type(result.matched_on, str)
    assert_type(result.context_entities, list[Entity])
    assert_type(result.to_dict(), dict[str, Any])
    assert_type(GraphSearchResult.from_dict(result.to_dict()), GraphSearchResult)
    assert_type(result_set.results, list[GraphSearchResult])
    assert_type(result_set.total_count, int)
    assert_type(result_set.query, GraphSearchQuery)
    assert_type(result_set.to_dict(), dict[str, Any])
    assert_type(
        GraphSearchResultSet.from_dict(result_set.to_dict()), GraphSearchResultSet
    )
    return result_set


def score_through_the_retained_runtime_half() -> float:
    """The four retained helpers, reached through the module binding.

    These are the legacy module's own definitions, not routes, so they are used
    here through ``runtime_search_models`` -- keeping this fixture's ``from``
    imports exactly the eleven routed names while still proving the split leaf
    presents both halves as usefully typed to a strict caller.
    """
    name = runtime_search_models.score_name_match("alice", "Alice Smith")
    relationships = runtime_search_models.score_relationship_count(3, 2)
    neighbors = runtime_search_models.score_neighbor_overlap(["Bob"], ["alice"])
    composite = runtime_search_models.compute_relevance_score(
        "alice",
        "Alice Smith",
        outgoing_relationships=3,
        incoming_relationships=2,
        neighbor_names=["Bob"],
        name_weight=0.5,
        relationship_weight=0.25,
        neighbor_weight=0.25,
    )
    assert_type(name, float)
    assert_type(relationships, float)
    assert_type(neighbors, float)
    assert_type(composite, float)

    # The split is invisible to a typed caller: the record types the module
    # publishes are the same ones imported directly above.
    assert_type(runtime_search_models.GraphSearchQuery, type[GraphSearchQuery])
    assert_type(runtime_search_models.GraphSearchResult, type[GraphSearchResult])
    assert_type(runtime_search_models.GraphSearchResultSet, type[GraphSearchResultSet])
    return composite


def score_a_result_set() -> float:
    """One end-to-end pass: build the records, score them, and keep the floats."""
    result_set = build_result_set()
    build_relationship()
    build_inputs()
    approval_status_predicates()
    total = 0.0
    for result in result_set.results:
        total += runtime_search_models.compute_relevance_score(
            result_set.query.query,
            result.entity.name,
            outgoing_relationships=len(result.context_entities),
        )
    assert_type(total, float)
    return total + score_through_the_retained_runtime_half()
