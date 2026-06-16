"""Tests for the graph module.

Tests for the knowledge graph module including entities, relationships,
and graph service operations.

Note: The import order matters due to circular import resolution.
Memory imports must come before graph imports.
"""

import tempfile
from pathlib import Path

import pytest

# Import memory first to resolve circular import chain
from omnivia_memory.memory.service import MemoryService  # noqa: F401
from omnivia_memory.memory.models import Memory  # noqa: F401

# Now import graph modules
from omnivia_memory.graph.models import (
    ApprovalStatus,
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
    EntityCreate,
)
from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository
from omnivia_memory.graph.service import (
    EntityNotFoundError,
    EntityValidationError,
    GraphService,
    GraphServiceError,
    RelationshipNotFoundError,
)
from omnivia_memory.persistence.database import Database, DatabaseConfig


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = DatabaseConfig(db_path=db_path)
        db = Database(config)
        db.connect()
        yield db
        db.close()


@pytest.fixture
def entity_repo(temp_db):
    """Create an entity repository for testing."""
    return EntityRepository(temp_db)


@pytest.fixture
def relationship_repo(temp_db):
    """Create a relationship repository for testing."""
    return RelationshipRepository(temp_db)


@pytest.fixture
def graph_service(entity_repo, relationship_repo):
    """Create a graph service for testing."""
    return GraphService(
        entity_repository=entity_repo,
        relationship_repository=relationship_repo,
    )


# =============================================================================
# Entity Model Tests
# =============================================================================


class TestEntityModel:
    """Tests for Entity model."""

    def test_create_entity_with_defaults(self):
        """Entity has sensible defaults for required fields."""
        entity = Entity(name="Test Entity", entity_type=EntityType.CONCEPT)

        assert entity.name == "Test Entity"
        assert entity.entity_type == EntityType.CONCEPT
        assert entity.approval_status == ApprovalStatus.PROPOSED
        assert entity.id is not None
        assert entity.created_at is not None
        assert entity.updated_at is not None
        assert entity.source_id is None

    def test_create_entity_with_all_fields(self):
        """Entity can be created with all fields specified."""
        entity = Entity(
            name="Full Entity",
            entity_type=EntityType.PERSON,
            source_id="doc-123",
            approval_status=ApprovalStatus.APPROVED,
            id="custom-id",
        )

        assert entity.name == "Full Entity"
        assert entity.entity_type == EntityType.PERSON
        assert entity.source_id == "doc-123"
        assert entity.approval_status == ApprovalStatus.APPROVED
        assert entity.id == "custom-id"

    def test_entity_to_dict(self):
        """Entity serializes to dictionary correctly."""
        entity = Entity(
            name="Test",
            entity_type=EntityType.PROJECT,
            id="test-id",
            approval_status=ApprovalStatus.APPROVED,
        )
        data = entity.to_dict()

        assert data["id"] == "test-id"
        assert data["name"] == "Test"
        assert data["entity_type"] == "project"
        assert data["approval_status"] == "approved"

    def test_entity_from_dict(self):
        """Entity deserializes from dictionary correctly."""
        data = {
            "id": "test-id",
            "name": "Test",
            "entity_type": "system",
            "source_id": "doc-456",
            "approval_status": "approved",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        entity = Entity.from_dict(data)

        assert entity.id == "test-id"
        assert entity.name == "Test"
        assert entity.entity_type == EntityType.SYSTEM
        assert entity.source_id == "doc-456"
        assert entity.approval_status == ApprovalStatus.APPROVED

    def test_entity_approve(self):
        """Entity can be approved."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT)
        assert entity.approval_status == ApprovalStatus.PROPOSED

        entity.approve()

        assert entity.approval_status == ApprovalStatus.APPROVED

    def test_entity_reject(self):
        """Entity can be rejected."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT)
        entity.approve()

        entity.reject()

        assert entity.approval_status == ApprovalStatus.REJECTED

    def test_entity_supersede(self):
        """Entity can be superseded."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT)
        entity.approve()

        entity.supersede()

        assert entity.approval_status == ApprovalStatus.SUPERSEDED

    def test_entity_equality(self):
        """Entities with same ID are equal."""
        entity1 = Entity(name="Test1", entity_type=EntityType.CONCEPT, id="same-id")
        entity2 = Entity(name="Test2", entity_type=EntityType.PERSON, id="same-id")

        assert entity1 == entity2

    def test_entity_hash(self):
        """Entities can be used in sets."""
        entity1 = Entity(name="Test1", entity_type=EntityType.CONCEPT, id="id-1")
        entity2 = Entity(name="Test1", entity_type=EntityType.CONCEPT, id="id-2")

        entity_set = {entity1, entity2}
        assert len(entity_set) == 2


# =============================================================================
# Relationship Model Tests
# =============================================================================


class TestRelationshipModel:
    """Tests for Relationship model."""

    def test_create_relationship_with_defaults(self):
        """Relationship has sensible defaults for required fields."""
        rel = Relationship(
            source_entity_id="source-1",
            target_entity_id="target-1",
            relationship_type=RelationshipType.DEPENDS_ON,
        )

        assert rel.source_entity_id == "source-1"
        assert rel.target_entity_id == "target-1"
        assert rel.relationship_type == RelationshipType.DEPENDS_ON
        assert rel.approval_status == ApprovalStatus.PROPOSED
        assert rel.id is not None
        assert rel.source_id is None

    def test_relationship_to_dict(self):
        """Relationship serializes to dictionary correctly."""
        rel = Relationship(
            source_entity_id="src",
            target_entity_id="tgt",
            relationship_type=RelationshipType.USES,
            id="rel-id",
        )
        data = rel.to_dict()

        assert data["id"] == "rel-id"
        assert data["source_entity_id"] == "src"
        assert data["target_entity_id"] == "tgt"
        assert data["relationship_type"] == "uses"
        assert data["approval_status"] == "proposed"

    def test_relationship_from_dict(self):
        """Relationship deserializes from dictionary correctly."""
        data = {
            "id": "rel-id",
            "source_entity_id": "src-123",
            "target_entity_id": "tgt-456",
            "relationship_type": "part_of",
            "source_id": "doc-789",
            "approval_status": "approved",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        rel = Relationship.from_dict(data)

        assert rel.id == "rel-id"
        assert rel.source_entity_id == "src-123"
        assert rel.target_entity_id == "tgt-456"
        assert rel.relationship_type == RelationshipType.PART_OF
        assert rel.source_id == "doc-789"
        assert rel.approval_status == ApprovalStatus.APPROVED

    def test_relationship_approve(self):
        """Relationship can be approved."""
        rel = Relationship(
            source_entity_id="src",
            target_entity_id="tgt",
            relationship_type=RelationshipType.USES,
        )
        assert rel.approval_status == ApprovalStatus.PROPOSED

        rel.approve()

        assert rel.approval_status == ApprovalStatus.APPROVED

    def test_relationship_reject(self):
        """Relationship can be rejected."""
        rel = Relationship(
            source_entity_id="src",
            target_entity_id="tgt",
            relationship_type=RelationshipType.USES,
        )
        rel.approve()

        rel.reject()

        assert rel.approval_status == ApprovalStatus.REJECTED


# =============================================================================
# Entity Repository Tests
# =============================================================================


class TestEntityRepository:
    """Tests for EntityRepository."""

    def test_create_and_get_entity(self, entity_repo):
        """Entity can be created and retrieved."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT)
        created = entity_repo.create(entity)

        retrieved = entity_repo.get(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == "Test"
        assert retrieved.entity_type == EntityType.CONCEPT

    def test_create_duplicate_id_raises(self, entity_repo):
        """Creating entity with existing ID raises ValueError."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT, id="dup-id")
        entity_repo.create(entity)

        with pytest.raises(ValueError, match="already exists"):
            entity_repo.create(entity)

    def test_get_nonexistent_returns_none(self, entity_repo):
        """Getting non-existent entity returns None."""
        result = entity_repo.get("nonexistent-id")
        assert result is None

    def test_list_entities(self, entity_repo):
        """Entities can be listed."""
        entity1 = Entity(name="Entity 1", entity_type=EntityType.CONCEPT)
        entity2 = Entity(name="Entity 2", entity_type=EntityType.PERSON)
        entity_repo.create(entity1)
        entity_repo.create(entity2)

        entities = entity_repo.list_all()

        assert len(entities) >= 2

    def test_list_entities_by_type(self, entity_repo):
        """Entities can be filtered by type."""
        entity1 = Entity(name="Concept", entity_type=EntityType.CONCEPT)
        entity2 = Entity(name="Person", entity_type=EntityType.PERSON)
        entity_repo.create(entity1)
        entity_repo.create(entity2)

        concepts = entity_repo.list_by_type(EntityType.CONCEPT)

        assert len(concepts) >= 1
        assert all(e.entity_type == EntityType.CONCEPT for e in concepts)

    def test_delete_entity(self, entity_repo):
        """Entity can be deleted."""
        entity = Entity(name="To Delete", entity_type=EntityType.CONCEPT)
        entity_repo.create(entity)

        result = entity_repo.delete(entity.id)

        assert result is True
        assert entity_repo.get(entity.id) is None

    def test_delete_nonexistent_returns_false(self, entity_repo):
        """Deleting non-existent entity returns False."""
        result = entity_repo.delete("nonexistent-id")
        assert result is False


# =============================================================================
# Relationship Repository Tests
# =============================================================================


class TestRelationshipRepository:
    """Tests for RelationshipRepository."""

    def test_create_and_get_relationship(self, relationship_repo):
        """Relationship can be created and retrieved."""
        rel = Relationship(
            source_entity_id="src-1",
            target_entity_id="tgt-1",
            relationship_type=RelationshipType.USES,
        )
        created = relationship_repo.create(rel)

        retrieved = relationship_repo.get(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.source_entity_id == "src-1"
        assert retrieved.target_entity_id == "tgt-1"

    def test_get_by_entity(self, relationship_repo):
        """Relationships can be filtered by entity."""
        rel1 = Relationship(
            source_entity_id="entity-A",
            target_entity_id="entity-B",
            relationship_type=RelationshipType.USES,
        )
        rel2 = Relationship(
            source_entity_id="entity-A",
            target_entity_id="entity-C",
            relationship_type=RelationshipType.DEPENDS_ON,
        )
        relationship_repo.create(rel1)
        relationship_repo.create(rel2)

        relationships = relationship_repo.get_by_entity("entity-A")

        assert len(relationships) >= 2
        assert all(r.source_entity_id == "entity-A" for r in relationships)

    def test_get_neighbors(self, relationship_repo, entity_repo):
        """Neighbors can be retrieved for an entity."""
        # Create entities first with known IDs
        _ = entity_repo.create(
            Entity(name="Center", entity_type=EntityType.CONCEPT, id="center-id")
        )
        _ = entity_repo.create(
            Entity(name="Neighbor1", entity_type=EntityType.CONCEPT, id="neighbor-1-id")
        )
        _ = entity_repo.create(
            Entity(name="Neighbor2", entity_type=EntityType.CONCEPT, id="neighbor-2-id")
        )

        _ = relationship_repo.create(
            Relationship(
                source_entity_id="center-id",
                target_entity_id="neighbor-1-id",
                relationship_type=RelationshipType.USES,
            )
        )
        _ = relationship_repo.create(
            Relationship(
                source_entity_id="neighbor-2-id",
                target_entity_id="center-id",
                relationship_type=RelationshipType.PART_OF,
            )
        )

        neighbors = relationship_repo.get_neighbors("center-id")

        # Should return list of (entity, relationship) tuples
        assert len(neighbors) >= 2


# =============================================================================
# Graph Service Tests
# =============================================================================


class TestGraphService:
    """Tests for GraphService."""

    def test_create_entity_defaults_to_proposed(self, graph_service):
        """Agent-created entities default to proposed state."""
        entity = graph_service.create_entity(
            name="Test Entity",
            entity_type=EntityType.CONCEPT,
        )

        assert entity.approval_status == ApprovalStatus.PROPOSED

    def test_create_entity_with_string_type(self, graph_service):
        """Entity can be created with string type."""
        entity = graph_service.create_entity(
            name="Test",
            entity_type="person",  # string instead of enum
        )

        assert entity.entity_type == EntityType.PERSON

    def test_create_entity_invalid_type_raises(self, graph_service):
        """Creating entity with invalid type raises."""
        with pytest.raises(ValueError):
            graph_service.create_entity(
                name="Test",
                entity_type="invalid_type",
            )

    def test_create_relationship_defaults_to_proposed(self, graph_service):
        """Agent-created relationships default to proposed state."""
        entity1 = graph_service.create_entity(
            name="Source",
            entity_type=EntityType.CONCEPT,
        )
        entity2 = graph_service.create_entity(
            name="Target",
            entity_type=EntityType.CONCEPT,
        )

        rel = graph_service.create_relationship(
            source_entity_id=entity1.id,
            target_entity_id=entity2.id,
            relationship_type=RelationshipType.USES,
        )

        assert rel.approval_status == ApprovalStatus.PROPOSED

    def test_approve_entity(self, graph_service):
        """Entity can be approved through service."""
        entity = graph_service.create_entity(
            name="Test",
            entity_type=EntityType.CONCEPT,
        )

        approved = graph_service.approve_entity(entity.id)

        assert approved.approval_status == ApprovalStatus.APPROVED

    def test_approve_nonexistent_raises(self, graph_service):
        """Approving non-existent entity raises."""
        with pytest.raises(EntityNotFoundError):
            graph_service.approve_entity("nonexistent-id")

    def test_entity_reject_method(self, graph_service):
        """Entity can be rejected through model method."""
        entity = graph_service.create_entity(
            name="Test",
            entity_type=EntityType.CONCEPT,
        )

        # Reject via model method (service doesn't have reject_entity)
        entity.reject()

        assert entity.approval_status == ApprovalStatus.REJECTED

    def test_get_neighbors_returns_entities(self, graph_service):
        """Get neighbors returns (Entity, Relationship) tuples."""
        entity1 = graph_service.create_entity(
            name="Entity 1",
            entity_type=EntityType.CONCEPT,
        )
        entity2 = graph_service.create_entity(
            name="Entity 2",
            entity_type=EntityType.CONCEPT,
        )

        # Create relationship: 1 -> 2
        graph_service.create_relationship(
            source_entity_id=entity1.id,
            target_entity_id=entity2.id,
            relationship_type=RelationshipType.USES,
        )

        neighbors = graph_service.get_neighbors(entity1.id)

        # Should return (Entity, Relationship) tuples
        assert len(neighbors) >= 1
        # Each neighbor should be a tuple of (Entity, Relationship)
        for neighbor_entity, rel in neighbors:
            assert isinstance(neighbor_entity, Entity)
            assert isinstance(rel, Relationship)


# =============================================================================
# Backlinks and Path-Finding Tests
# =============================================================================


class TestBacklinksAndPaths:
    """Tests for first-class backlinks and graph path-finding."""

    @pytest.fixture
    def linked_graph(self, graph_service):
        """A uses B, B depends_on C, D relates_to C, plus an isolated E."""
        a = graph_service.create_entity(name="A", entity_type=EntityType.CONCEPT)
        b = graph_service.create_entity(name="B", entity_type=EntityType.CONCEPT)
        c = graph_service.create_entity(name="C", entity_type=EntityType.CONCEPT)
        d = graph_service.create_entity(name="D", entity_type=EntityType.CONCEPT)
        e = graph_service.create_entity(name="E", entity_type=EntityType.CONCEPT)

        graph_service.create_relationship(
            source_entity_id=a.id,
            target_entity_id=b.id,
            relationship_type=RelationshipType.USES,
        )
        graph_service.create_relationship(
            source_entity_id=b.id,
            target_entity_id=c.id,
            relationship_type=RelationshipType.DEPENDS_ON,
        )
        graph_service.create_relationship(
            source_entity_id=d.id,
            target_entity_id=c.id,
            relationship_type=RelationshipType.RELATES_TO,
        )
        return {"a": a, "b": b, "c": c, "d": d, "e": e}

    def test_get_backlinks_returns_entities_that_link_here(
        self, graph_service, linked_graph
    ):
        """Backlinks surface every entity pointing at the target."""
        backlinks = graph_service.get_backlinks(linked_graph["c"].id)

        assert {entity.id for entity, _rel in backlinks} == {
            linked_graph["b"].id,
            linked_graph["d"].id,
        }

    def test_get_backlinks_pairs_source_entity_with_relationship(
        self, graph_service, linked_graph
    ):
        """Each backlink carries the linking entity and the relationship."""
        backlinks = graph_service.get_backlinks(linked_graph["b"].id)

        assert len(backlinks) == 1
        entity, rel = backlinks[0]
        assert entity.id == linked_graph["a"].id
        assert rel.relationship_type == RelationshipType.USES

    def test_get_backlinks_filters_by_relationship_type(
        self, graph_service, linked_graph
    ):
        """Backlinks can be restricted to a relationship type."""
        backlinks = graph_service.get_backlinks(
            linked_graph["c"].id,
            relationship_type=RelationshipType.RELATES_TO,
        )

        assert {entity.id for entity, _rel in backlinks} == {linked_graph["d"].id}

    def test_get_backlinks_accepts_string_relationship_type(
        self, graph_service, linked_graph
    ):
        """String relationship-type filters are coerced to the enum."""
        backlinks = graph_service.get_backlinks(
            linked_graph["c"].id, relationship_type="depends_on"
        )

        assert {entity.id for entity, _rel in backlinks} == {linked_graph["b"].id}

    def test_get_backlinks_empty_for_unlinked_entity(
        self, graph_service, linked_graph
    ):
        """An entity nothing points at has no backlinks."""
        assert graph_service.get_backlinks(linked_graph["a"].id) == []

    def test_get_backlinks_skips_dangling_source_relationship(
        self, graph_service, linked_graph
    ):
        """Dangling incoming rows do not produce fake backlink entities."""
        graph_service.create_relationship(
            source_entity_id="missing-source",
            target_entity_id=linked_graph["a"].id,
            relationship_type=RelationshipType.RELATES_TO,
            validate_entities=False,
        )

        assert graph_service.get_backlinks(linked_graph["a"].id) == []

    def test_get_backlinks_requires_repository(self):
        """Backlinks fail clearly when repositories are not configured."""
        with pytest.raises(GraphServiceError):
            GraphService().get_backlinks("x")

    def test_find_path_returns_shortest_path(self, graph_service, linked_graph):
        """The shortest hop sequence between two entities is returned."""
        path = graph_service.find_path(linked_graph["a"].id, linked_graph["c"].id)

        assert [entity.id for entity in path] == [
            linked_graph["a"].id,
            linked_graph["b"].id,
            linked_graph["c"].id,
        ]

    def test_find_path_traverses_undirected(self, graph_service, linked_graph):
        """Relationships can be followed from either end."""
        path = graph_service.find_path(linked_graph["c"].id, linked_graph["a"].id)

        assert [entity.id for entity in path] == [
            linked_graph["c"].id,
            linked_graph["b"].id,
            linked_graph["a"].id,
        ]

    def test_find_path_to_self_returns_single_node(self, graph_service, linked_graph):
        """A path from an entity to itself is just that entity."""
        path = graph_service.find_path(linked_graph["a"].id, linked_graph["a"].id)

        assert [entity.id for entity in path] == [linked_graph["a"].id]

    def test_find_path_returns_none_when_unreachable(
        self, graph_service, linked_graph
    ):
        """No path to a disconnected entity yields None."""
        assert (
            graph_service.find_path(linked_graph["a"].id, linked_graph["e"].id) is None
        )

    def test_find_path_respects_max_depth(self, graph_service, linked_graph):
        """A target beyond the hop limit is reported as unreachable."""
        assert (
            graph_service.find_path(
                linked_graph["a"].id, linked_graph["c"].id, max_depth=1
            )
            is None
        )

    def test_find_path_zero_depth_only_matches_self(
        self, graph_service, linked_graph
    ):
        """A zero-hop limit does not silently widen to direct neighbors."""
        assert (
            graph_service.find_path(
                linked_graph["a"].id, linked_graph["b"].id, max_depth=0
            )
            is None
        )
        assert graph_service.find_path(
            linked_graph["a"].id, linked_graph["a"].id, max_depth=0
        ) == [linked_graph["a"]]

    def test_find_path_rejects_negative_depth(self, graph_service, linked_graph):
        """Negative hop limits fail clearly instead of being coerced."""
        with pytest.raises(ValueError, match="max_depth must be non-negative"):
            graph_service.find_path(
                linked_graph["a"].id, linked_graph["b"].id, max_depth=-1
            )
        with pytest.raises(ValueError, match="max_depth must be non-negative"):
            graph_service.find_path("missing", "missing", max_depth=-1)

    def test_find_path_unknown_endpoint_raises(self, graph_service, linked_graph):
        """Missing endpoints fail clearly."""
        with pytest.raises(EntityNotFoundError):
            graph_service.find_path("missing", linked_graph["c"].id)
        with pytest.raises(EntityNotFoundError):
            graph_service.find_path(linked_graph["a"].id, "missing")

    def test_find_path_requires_repository(self):
        """Path-finding fails clearly when repositories are not configured."""
        with pytest.raises(GraphServiceError):
            GraphService().find_path("a", "b")


# =============================================================================
# Governance Tests
# =============================================================================


class TestGovernanceDefaults:
    """Tests for governance defaults on agent-created elements."""

    def test_entity_defaults_to_proposed(self):
        """Entity created without explicit status defaults to proposed."""
        entity = Entity(name="Test", entity_type=EntityType.CONCEPT)
        assert entity.approval_status == ApprovalStatus.PROPOSED

    def test_relationship_defaults_to_proposed(self):
        """Relationship created without explicit status defaults to proposed."""
        rel = Relationship(
            source_entity_id="src",
            target_entity_id="tgt",
            relationship_type=RelationshipType.USES,
        )
        assert rel.approval_status == ApprovalStatus.PROPOSED

    def test_approval_status_is_approved(self):
        """ApprovalStatus.is_approved() returns True only for APPROVED."""
        assert ApprovalStatus.APPROVED.is_approved() is True
        assert ApprovalStatus.PROPOSED.is_approved() is False
        assert ApprovalStatus.REJECTED.is_approved() is False
        assert ApprovalStatus.SUPERSEDED.is_approved() is False

    def test_approval_status_is_active(self):
        """ApprovalStatus.is_active() returns True only for APPROVED."""
        assert ApprovalStatus.APPROVED.is_active() is True
        assert ApprovalStatus.PROPOSED.is_active() is False
        assert ApprovalStatus.REJECTED.is_active() is False
        assert ApprovalStatus.SUPERSEDED.is_active() is False


# =============================================================================
# Source/Provenance Tests
# =============================================================================


class TestSourceProvenance:
    """Tests for source/provenance tracking."""

    def test_entity_can_store_source_id(self):
        """Entity can store source document reference."""
        entity = Entity(
            name="Test",
            entity_type=EntityType.CONCEPT,
            source_id="doc-123",
        )
        assert entity.source_id == "doc-123"

    def test_relationship_can_store_source_id(self):
        """Relationship can store source document reference."""
        rel = Relationship(
            source_entity_id="src",
            target_entity_id="tgt",
            relationship_type=RelationshipType.USES,
            source_id="doc-456",
        )
        assert rel.source_id == "doc-456"

    def test_entity_create_input_model(self):
        """EntityCreate input model works correctly."""
        create_input = EntityCreate(
            name="Test Entity",
            entity_type="concept",
            provenance_source_id="doc-789",
        )
        assert create_input.name == "Test Entity"
        assert create_input.entity_type == "concept"
        assert create_input.provenance_source_id == "doc-789"


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Tests for error handling."""

    def test_graph_service_error_inheritance(self):
        """GraphServiceError is the base exception."""
        assert issubclass(GraphServiceError, Exception)

    def test_entity_not_found_error(self):
        """EntityNotFoundError is raised correctly."""
        error = EntityNotFoundError("test-id")
        assert "test-id" in str(error)

    def test_relationship_not_found_error(self):
        """RelationshipNotFoundError is raised correctly."""
        error = RelationshipNotFoundError("rel-id")
        assert "rel-id" in str(error)

    def test_entity_validation_error(self):
        """EntityValidationError is raised correctly."""
        error = EntityValidationError("Invalid name")
        assert "Invalid name" in str(error)


# =============================================================================
# EntityType and RelationshipType Tests
# =============================================================================


class TestEntityAndRelationshipTypes:
    """Tests for enum types."""

    def test_all_entity_types_valid(self):
        """All EntityType values are valid."""
        for entity_type in EntityType:
            entity = Entity(name="Test", entity_type=entity_type)
            assert entity.entity_type == entity_type

    def test_all_relationship_types_valid(self):
        """All RelationshipType values are valid."""
        for rel_type in RelationshipType:
            rel = Relationship(
                source_entity_id="src",
                target_entity_id="tgt",
                relationship_type=rel_type,
            )
            assert rel.relationship_type == rel_type

    def test_entity_type_string_value(self):
        """EntityType has correct string values."""
        assert EntityType.PERSON.value == "person"
        assert EntityType.CONCEPT.value == "concept"
        assert EntityType.SYSTEM.value == "system"

    def test_relationship_type_string_value(self):
        """RelationshipType has correct string values."""
        assert RelationshipType.USES.value == "uses"
        assert RelationshipType.DEPENDS_ON.value == "depends_on"
        assert RelationshipType.PART_OF.value == "part_of"
