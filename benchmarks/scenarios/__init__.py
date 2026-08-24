"""Benchmark scenarios for OmniVia Core memory operations.

Each scenario is a benchmark function that measures performance of a specific
operation. Scenarios use existing OmniVia Memory APIs where practical.

NOTE: Some scenarios marked with PLACEHOLDER are stubs when the exact Core API
is absent. They log a warning but keep the runner operational.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from benchmarks.dataset import generate_memory_items
from benchmarks.registry import scenario
from benchmarks.thresholds import (
    RuntimeSloThresholds,
    evaluate_runtime_slo,
    percentile,
)

from omnivia_memory.control_plane import (
    CONTROL_PLANE_SCHEMA_VERSION,
    ControlPlaneRunStatus,
    ExecutionMode,
)
from omnivia_memory.control_plane.registry import ControlPlaneRegistry
from omnivia_memory.memory.models import MemoryCreate, MemoryUpdate
from omnivia_memory.memory.service import MemoryService, MemoryServiceError
from omnivia_memory.persistence.database import Database, DatabaseConfig
from omnivia_memory.persistence.repositories import MemoryRepository
from omnivia_memory.provenance.models import Source, SourceType


def _create_temp_db() -> tuple[str, Database]:
    """Create a temporary database for benchmarks.

    Returns:
        Tuple of (db_path, database instance)
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)  # Remove the file, Database will create it

    config = DatabaseConfig(db_path=Path(db_path))
    db = Database(config)
    db.connect()
    return db_path, db


def _cleanup_temp_db(db_path: str, db: Database) -> None:
    """Clean up a temporary database."""
    try:
        db.close()
    except Exception:
        pass
    try:
        os.unlink(db_path)
    except Exception:
        pass


def _default_source() -> Source:
    """Create a default source for test memories."""
    return Source(
        type=SourceType.HUMAN,
        reference="benchmark:synthetic",
        description="Deterministic synthetic benchmark fixture data",
    )


@scenario(
    "create_memory",
    "Benchmark memory creation operations",
    tags=["memory", "crud", "create"],
    estimated_time_per_item_ms=0.5,
)
def create_memory(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark memory creation.

    Measures the throughput of creating memories in batch.

    Args:
        db_path: Database path (used to create services)
        item_count: Number of memories to create

    Returns:
        Dictionary with benchmark results
    """
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    start = time.perf_counter()
    created_ids = []

    for item in list(generate_memory_items("tiny", "create_memory"))[:item_count]:
        memory = service.create(
            MemoryCreate(
                content=item["content"],
                source=source,
                memory_type=item["memory_type"],
            )
        )
        created_ids.append(memory.id)

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = item_count / duration if duration > 0 else 0

    return {
        "item_count": item_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "created_count": len(created_ids),
        "error": None,
    }


@scenario(
    "retrieve_memory",
    "Benchmark memory retrieval by ID",
    tags=["memory", "crud", "read"],
    estimated_time_per_item_ms=0.1,
)
def retrieve_memory(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark memory retrieval by ID.

    Measures the throughput of retrieving existing memories by their IDs.

    Args:
        db_path: Database path
        item_count: Number of memories to pre-create and then retrieve

    Returns:
        Dictionary with benchmark results
    """
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories
    created_ids = []
    for item in list(generate_memory_items("tiny", "retrieve_memory"))[:item_count]:
        memory = service.create(
            MemoryCreate(content=item["content"], source=source)
        )
        created_ids.append(memory.id)

    # Benchmark retrieval
    start = time.perf_counter()
    retrieved_count = 0

    for memory_id in created_ids:
        try:
            service.get(memory_id)
            retrieved_count += 1
        except MemoryServiceError:
            pass

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = item_count / duration if duration > 0 else 0

    return {
        "item_count": item_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "retrieved_count": retrieved_count,
        "error": None,
    }


@scenario(
    "update_memory",
    "Benchmark memory update operations",
    tags=["memory", "crud", "update"],
    estimated_time_per_item_ms=0.5,
)
def update_memory(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark memory updates.

    Measures the throughput of updating existing memories.

    Args:
        db_path: Database path
        item_count: Number of memories to pre-create and update

    Returns:
        Dictionary with benchmark results
    """
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories
    created_ids = []
    for item in list(generate_memory_items("tiny", "update_memory"))[:item_count]:
        memory = service.create(
            MemoryCreate(content=item["content"], source=source)
        )
        created_ids.append(memory.id)

    # Benchmark updates
    start = time.perf_counter()
    updated_count = 0

    for i, memory_id in enumerate(created_ids):
        try:
            new_content = f"Updated content {i}: benchmark memory update test"
            service.update(
                memory_id,
                MemoryUpdate(content=new_content),
            )
            updated_count += 1
        except MemoryServiceError:
            pass

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = item_count / duration if duration > 0 else 0

    return {
        "item_count": item_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "updated_count": updated_count,
        "error": None,
    }


@scenario(
    "delete_memory",
    "Benchmark memory deletion operations",
    tags=["memory", "crud", "delete"],
    estimated_time_per_item_ms=0.5,
)
def delete_memory(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark memory deletion.

    Measures the throughput of deleting memories.

    Args:
        db_path: Database path
        item_count: Number of memories to pre-create and delete

    Returns:
        Dictionary with benchmark results
    """
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories
    created_ids = []
    for item in list(generate_memory_items("tiny", "delete_memory"))[:item_count]:
        memory = service.create(
            MemoryCreate(content=item["content"], source=source)
        )
        created_ids.append(memory.id)

    # Benchmark deletion
    start = time.perf_counter()
    deleted_count = 0

    for memory_id in created_ids:
        try:
            service.delete(memory_id)
            deleted_count += 1
        except MemoryServiceError:
            pass

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = item_count / duration if duration > 0 else 0

    return {
        "item_count": item_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "deleted_count": deleted_count,
        "error": None,
    }


@scenario(
    "keyword_search",
    "Benchmark keyword search operations",
    tags=["search", "keyword"],
    estimated_time_per_item_ms=2.0,
)
def keyword_search(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark keyword search.

    Measures the throughput of searching memories by keyword.
    Note: Creates smaller dataset to avoid expensive full-text operations.

    Args:
        db_path: Database path
        item_count: Number of memories to create for search

    Returns:
        Dictionary with benchmark results
    """
    # Limit item count for search benchmarks to avoid long test times
    actual_count = min(item_count, 500)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories with searchable content
    for i, item in enumerate(
        list(generate_memory_items("tiny", "keyword_search"))[:actual_count]
    ):
        # Embed searchable keywords in content
        search_term = f"searchable_keyword_{i % 10}"
        content = f"{item['content']} Contains {search_term} for testing."
        service.create(MemoryCreate(content=content, source=source))

    # Benchmark search
    start = time.perf_counter()
    search_count = 0
    query = "searchable_keyword_5"

    # Run multiple searches
    for _ in range(10):
        _results = service.search(query, limit=20)
        search_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = search_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "search_count": search_count,
        "error": None,
    }


@scenario(
    "tag_filter",
    "Benchmark tag-based filtering",
    tags=["filter", "tag"],
    estimated_time_per_item_ms=1.0,
)
def tag_filter(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark tag-based filtering.

    NOTE: PLACEHOLDER - OmniVia Core does not have a dedicated tag filter API.
    This scenario measures list/filter by iterating and checking tags.

    Args:
        db_path: Database path
        item_count: Number of memories to create

    Returns:
        Dictionary with benchmark results
    """
    import warnings
    warnings.warn(
        "tag_filter scenario: OmniVia Core does not have a dedicated tag filter API. "
        "Using list + filter as workaround.",
        UserWarning,
        stacklevel=2,
    )

    actual_count = min(item_count, 500)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories
    for item in list(generate_memory_items("tiny", "tag_filter"))[:actual_count]:
        service.create(
            MemoryCreate(
                content=item["content"],
                source=source,
                memory_type=item["memory_type"],
            )
        )

    # Benchmark filter: list all and filter by type
    start = time.perf_counter()
    filter_count = 0
    matched_count = 0

    for _ in range(20):
        all_memories = service.list(limit=1000)
        filtered = [m for m in all_memories if m.memory_type == "decision"]
        matched_count += len(filtered)
        filter_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = filter_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "filter_count": filter_count,
        "matched_count": matched_count,
        "error": None,
    }


@scenario(
    "source_filter",
    "Benchmark source-based filtering",
    tags=["filter", "source"],
    estimated_time_per_item_ms=1.0,
)
def source_filter(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark source-based filtering.

    Measures performance of filtering memories by source reference.

    Args:
        db_path: Database path
        item_count: Number of memories to create

    Returns:
        Dictionary with benchmark results
    """
    actual_count = min(item_count, 500)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories with different sources
    source_refs = [f"source_{i % 5}" for i in range(actual_count)]
    for i, item in enumerate(
        list(generate_memory_items("tiny", "source_filter"))[:actual_count]
    ):
        mem_source = Source(
            type=source.type,
            reference=f"benchmark_source_{source_refs[i]}",
        )
        service.create(
            MemoryCreate(content=item["content"], source=mem_source)
        )

    # Benchmark source filter
    start = time.perf_counter()
    filter_count = 0
    matched_count = 0

    for _ in range(20):
        all_memories = service.list(limit=1000)
        filtered = [
            m for m in all_memories
            if "source_2" in m.source.reference
        ]
        matched_count += len(filtered)
        filter_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = filter_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "filter_count": filter_count,
        "matched_count": matched_count,
        "error": None,
    }


@scenario(
    "graph_linking",
    "Benchmark graph entity creation and relationship linking",
    tags=["graph", "link"],
    estimated_time_per_item_ms=1.0,
)
def graph_linking(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark graph entity creation and relationship linking.

    Measures the throughput of creating entities and linking them.

    Args:
        db_path: Database path
        item_count: Number of entity pairs to create and link

    Returns:
        Dictionary with benchmark results
    """
    from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository
    from omnivia_memory.graph.service import GraphService
    from omnivia_memory.graph.models import EntityType, RelationshipType

    actual_count = min(item_count, 200)
    db_path, db = _create_temp_db()

    entity_repo = EntityRepository(db)
    rel_repo = RelationshipRepository(db)
    graph_service = GraphService(
        entity_repository=entity_repo,
        relationship_repository=rel_repo,
    )

    # Benchmark entity and relationship creation
    start = time.perf_counter()
    link_count = 0

    for i in range(actual_count):
        try:
            # Create two entities
            entity1 = graph_service.create_entity(
                name=f"Entity_A_{i}",
                entity_type=EntityType.CONCEPT,
            )
            entity2 = graph_service.create_entity(
                name=f"Entity_B_{i}",
                entity_type=EntityType.CONCEPT,
            )

            # Link them
            graph_service.create_relationship(
                source_entity_id=entity1.id,
                target_entity_id=entity2.id,
                relationship_type=RelationshipType.RELATES_TO,
                validate_entities=False,  # Skip validation for speed
            )
            link_count += 1
        except Exception:
            pass

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = link_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "link_count": link_count,
        "error": None,
    }


@scenario(
    "graph_traversal_1_hop",
    "Benchmark 1-hop graph traversal",
    tags=["graph", "traversal"],
    estimated_time_per_item_ms=2.0,
)
def graph_traversal_1_hop(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark 1-hop graph traversal (neighbors).

    Measures the throughput of retrieving direct neighbors of entities.

    Args:
        db_path: Database path
        item_count: Number of entities to traverse from

    Returns:
        Dictionary with benchmark results
    """
    from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository
    from omnivia_memory.graph.service import GraphService
    from omnivia_memory.graph.models import EntityType, RelationshipType

    actual_count = min(item_count, 100)
    db_path, db = _create_temp_db()

    entity_repo = EntityRepository(db)
    rel_repo = RelationshipRepository(db)
    graph_service = GraphService(
        entity_repository=entity_repo,
        relationship_repository=rel_repo,
    )

    # Create a star graph: central entity connected to many leaves
    central = graph_service.create_entity(
        name="Central_Entity",
        entity_type=EntityType.CONCEPT,
    )

    leaf_ids = []
    for i in range(actual_count):
        leaf = graph_service.create_entity(
            name=f"Leaf_{i}",
            entity_type=EntityType.CONCEPT,
        )
        leaf_ids.append(leaf.id)
        graph_service.create_relationship(
            source_entity_id=central.id,
            target_entity_id=leaf.id,
            relationship_type=RelationshipType.DEPENDS_ON,
            validate_entities=False,
        )

    # Benchmark traversal
    start = time.perf_counter()
    traversal_count = 0

    for _ in range(50):
        _neighbors = graph_service.get_neighbors(central.id)
        traversal_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = traversal_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "traversal_count": traversal_count,
        "neighbors_per_query": actual_count,
        "error": None,
    }


@scenario(
    "graph_traversal_2_hop",
    "Benchmark 2-hop graph traversal",
    tags=["graph", "traversal"],
    estimated_time_per_item_ms=5.0,
)
def graph_traversal_2_hop(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark 2-hop graph traversal.

    NOTE: PLACEHOLDER - This scenario measures chained 1-hop traversals
    as OmniVia Core does not have a native multi-hop traversal API.

    Args:
        db_path: Database path
        item_count: Number of 2-hop paths to traverse

    Returns:
        Dictionary with benchmark results
    """
    import warnings
    warnings.warn(
        "graph_traversal_2_hop scenario: OmniVia Core does not have native multi-hop "
        "traversal. Using chained 1-hop queries as workaround.",
        UserWarning,
        stacklevel=2,
    )

    from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository
    from omnivia_memory.graph.service import GraphService
    from omnivia_memory.graph.models import EntityType, RelationshipType

    actual_count = min(item_count, 50)
    db_path, db = _create_temp_db()

    entity_repo = EntityRepository(db)
    rel_repo = RelationshipRepository(db)
    graph_service = GraphService(
        entity_repository=entity_repo,
        relationship_repository=rel_repo,
    )

    # Create a chain: A -> B -> C for each path
    chain_ids = []
    for i in range(actual_count):
        a = graph_service.create_entity(
            name=f"Chain_A_{i}",
            entity_type=EntityType.CONCEPT,
        )
        b = graph_service.create_entity(
            name=f"Chain_B_{i}",
            entity_type=EntityType.CONCEPT,
        )
        c = graph_service.create_entity(
            name=f"Chain_C_{i}",
            entity_type=EntityType.CONCEPT,
        )
        graph_service.create_relationship(
            source_entity_id=a.id,
            target_entity_id=b.id,
            relationship_type=RelationshipType.DEPENDS_ON,
            validate_entities=False,
        )
        graph_service.create_relationship(
            source_entity_id=b.id,
            target_entity_id=c.id,
            relationship_type=RelationshipType.DEPENDS_ON,
            validate_entities=False,
        )
        chain_ids.append((a.id, b.id, c.id))

    # Benchmark 2-hop traversal
    start = time.perf_counter()
    traversal_count = 0

    for start_id, _, end_id in chain_ids:
        # Get 1-hop from start (should give us B)
        first_hop = graph_service.get_neighbors(start_id)
        if first_hop:
            # Get 1-hop from first result's entity (should give us C)
            second_entity = first_hop[0][0]
            second_hop = graph_service.get_neighbors(second_entity.id)
            if second_hop:
                _reached_end = any(entity.id == end_id for entity, _rel in second_hop)
            traversal_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = traversal_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "traversal_count": traversal_count,
        "error": None,
    }


@scenario(
    "import_json",
    "Benchmark JSON import operations",
    tags=["import", "json"],
    estimated_time_per_item_ms=5.0,
)
def import_json(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark JSON import of memories.

    Measures the throughput of importing memories from JSON format.

    Args:
        db_path: Database path
        item_count: Number of memories to import

    Returns:
        Dictionary with benchmark results
    """
    actual_count = min(item_count, 200)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Generate JSON data to import
    json_data = []
    for item in list(generate_memory_items("tiny", "import_json"))[:actual_count]:
        json_data.append({
            "content": item["content"],
            "memory_type": item["memory_type"],
            "source": source.to_dict(),
        })

    # Benchmark import
    start = time.perf_counter()
    imported_count = 0

    for data in json_data:
        try:
            source_obj = Source.from_dict(data["source"])
            service.create(
                MemoryCreate(
                    content=data["content"],
                    source=source_obj,
                    memory_type=data["memory_type"],
                )
            )
            imported_count += 1
        except Exception:
            pass

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = imported_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "imported_count": imported_count,
        "error": None,
    }


@scenario(
    "export_json",
    "Benchmark JSON export operations",
    tags=["export", "json"],
    estimated_time_per_item_ms=2.0,
)
def export_json(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark JSON export of memories.

    Measures the throughput of exporting memories to JSON format.

    Args:
        db_path: Database path
        item_count: Number of memories to export

    Returns:
        Dictionary with benchmark results
    """
    actual_count = min(item_count, 500)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create memories
    for item in list(generate_memory_items("tiny", "export_json"))[:actual_count]:
        service.create(
            MemoryCreate(content=item["content"], source=source)
        )

    # Benchmark export
    start = time.perf_counter()
    export_count = 0

    for _ in range(20):
        all_memories = service.list(limit=1000)
        exported = [m.to_dict() for m in all_memories]
        _json_str = json.dumps(exported)
        export_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = export_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "export_count": export_count,
        "error": None,
    }


@scenario(
    "mixed_workload",
    "Benchmark mixed read/write workload",
    tags=["mixed", "workload"],
    estimated_time_per_item_ms=3.0,
)
def mixed_workload(db_path: str, item_count: int) -> dict[str, Any]:
    """Benchmark mixed read/write workload.

    Measures performance under realistic mixed operations.

    Args:
        db_path: Database path
        item_count: Base number of operations

    Returns:
        Dictionary with benchmark results
    """
    actual_count = min(item_count, 200)
    db_path, db = _create_temp_db()
    repository = MemoryRepository(db)
    service = MemoryService(repository=repository)
    source = _default_source()

    # Pre-create some memories
    created_ids = []
    for item in list(generate_memory_items("tiny", "mixed_workload_pre"))[:actual_count // 2]:
        memory = service.create(
            MemoryCreate(content=item["content"], source=source)
        )
        created_ids.append(memory.id)

    # Benchmark mixed operations
    start = time.perf_counter()
    op_count = 0

    for i in range(actual_count):
        op_type = i % 4
        if op_type == 0:
            # Create
            service.create(
                MemoryCreate(
                    content=f"Mixed workload {i}",
                    source=source,
                )
            )
            op_count += 1
        elif op_type == 1 and created_ids:
            # Read
            idx = i % len(created_ids)
            service.get(created_ids[idx])
            op_count += 1
        elif op_type == 2 and created_ids:
            # Update
            idx = i % len(created_ids)
            service.update(
                created_ids[idx],
                MemoryUpdate(content=f"Updated {i}"),
            )
            op_count += 1
        else:
            # Search
            service.search("Mixed", limit=10)
            op_count += 1

    duration = time.perf_counter() - start
    _cleanup_temp_db(db_path, db)

    ops_per_second = op_count / duration if duration > 0 else 0

    return {
        "item_count": actual_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "mixed_op_count": op_count,
        "error": None,
    }


# The load/soak scenario rewrites the whole workspace manifest per run, so its
# cost grows with the run count. Cap it so large profiles stay bounded; the
# gate is about growth per run, not absolute volume.
MAX_RUNTIME_LOAD_RUNS = 200

CONTROL_PLANE_BENCHMARK_WORKSPACE = "workspace.benchmark.runtime"

# Values that must never reach the redacted OTel projection.
_FORBIDDEN_PROJECTION_TERMS = (
    "secret://",
    "connection.benchmark",
    "mcp",
    "openapi",
)


def _control_plane_benchmark_manifest() -> dict[str, Any]:
    """Return an all-active manifest with one query capability and trigger."""
    workspace_id = CONTROL_PLANE_BENCHMARK_WORKSPACE
    return {
        "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
        "workspace": {"id": workspace_id, "name": "Runtime Load Benchmark"},
        "connections": [
            {
                "id": "connection.benchmark",
                "kind": "app",
                "lifecycle": "active",
                "secret_refs": [
                    {"secret_ref": f"secret://{workspace_id}/benchmark/oauth"}
                ],
            }
        ],
        "capabilities": [
            {
                "id": "capability.benchmark.read",
                "capability_type": "query",
                "connection_id": "connection.benchmark",
                "side_effect": "read",
                "lifecycle": "active",
                "input_schema": {
                    "type": "object",
                    "properties": {"item_id": {"type": "string"}},
                    "required": ["item_id"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            }
        ],
        "agents": [
            {
                "id": "agent.benchmark",
                "allowed_capabilities": ["capability.benchmark.read"],
            }
        ],
        "triggers": [
            {
                "id": "trigger.benchmark.item.created",
                "kind": "cloudevent",
                "capability_id": "capability.benchmark.read",
                "event_type": "com.omnivia.benchmark.item.created",
                "lifecycle": "active",
            }
        ],
        "automations": [
            {
                "id": "automation.benchmark",
                "agent_id": "agent.benchmark",
                "capability_id": "capability.benchmark.read",
                "trigger_id": "trigger.benchmark.item.created",
                "lifecycle": "active",
                "max_steps": 4,
                "max_cost_units": 0,
                "max_token_usage": 0,
                "max_retries": 1,
            }
        ],
    }


def _projection_redaction_violations(projection: dict[str, Any]) -> list[str]:
    """Return descriptions of disallowed content found in an OTel projection."""
    violations: list[str] = []
    if not projection.get("redacted"):
        violations.append("projection is not marked redacted")

    rendered = json.dumps(projection).lower()
    for term in _FORBIDDEN_PROJECTION_TERMS:
        if term in rendered:
            violations.append(f"projection leaked forbidden term '{term}'")

    # The scenario feeds distinctive payload values in; none may be projected.
    if "benchmark-item-" in rendered or "benchmark summary" in rendered:
        violations.append("projection leaked capability input/output payload values")

    return violations


@scenario(
    "control_plane_runtime_load_soak",
    "Benchmark control-plane trigger ingestion and dry-run execution under load",
    tags=["control_plane", "runtime", "load", "soak", "slo"],
    estimated_time_per_item_ms=15.0,
)
def control_plane_runtime_load_soak(db_path: str, item_count: int) -> dict[str, Any]:
    """Drive real control-plane runs and record bounded SLO evidence.

    Stores one active manifest, ingests ``item_count`` unique trigger events,
    executes every accepted run through the real dry-run runtime path with
    schema-valid input/output payloads, then reads the Core observability
    summary and the redacted OTel projection. No network, no sleeps, no model
    or provider invocation: the dry-run path simulates steps locally.

    Args:
        db_path: Ignored; the scenario creates its own temporary database.
        item_count: Requested run count (capped at ``MAX_RUNTIME_LOAD_RUNS``).

    Returns:
        Dictionary with benchmark results plus an ``slo`` evidence block.
    """
    run_count = max(1, min(item_count, MAX_RUNTIME_LOAD_RUNS))
    workspace_id = CONTROL_PLANE_BENCHMARK_WORKSPACE

    db_path, db = _create_temp_db()
    registry = ControlPlaneRegistry(db)
    registry.store_manifest(_control_plane_benchmark_manifest())

    latency_samples: list[float] = []
    completed_count = 0
    error_count = 0
    first_error: str | None = None

    start = time.perf_counter()
    for index in range(run_count):
        operation_start = time.perf_counter()
        ingestion = registry.ingest_trigger_event(
            workspace_id,
            {
                "id": f"evt.benchmark.{index}",
                "trigger_id": "trigger.benchmark.item.created",
                "event_type": "com.omnivia.benchmark.item.created",
                "idempotency_key": f"benchmark-run-{index}",
            },
        )
        if not ingestion.accepted or ingestion.run_record is None:
            error_count += 1
            first_error = first_error or (
                f"trigger event {index} rejected: {ingestion.dead_letter_reason}"
            )
            continue

        execution = registry.execute_run(
            workspace_id,
            ingestion.run_record.id,
            mode=ExecutionMode.DRY_RUN,
            input_payload={"item_id": f"benchmark-item-{index}"},
            output_payload={"summary": "benchmark summary"},
        )
        latency_samples.append((time.perf_counter() - operation_start) * 1000)

        if execution.run_record.status in {
            ControlPlaneRunStatus.COMPLETED,
            ControlPlaneRunStatus.SUCCEEDED,
        }:
            completed_count += 1
        else:
            error_count += 1
            first_error = first_error or (
                f"run {index} ended {execution.run_record.status.value}: "
                f"{execution.error}"
            )

    duration = time.perf_counter() - start

    observability_start = time.perf_counter()
    metrics = registry.summarize_observability_metrics(workspace_id)
    projection = registry.project_redacted_otel_observability(workspace_id)
    observability_ms = (time.perf_counter() - observability_start) * 1000

    database_bytes = os.path.getsize(db_path)
    _cleanup_temp_db(db_path, db)

    ops_per_second = run_count / duration if duration > 0 else 0.0
    projection_metrics = projection.get("metrics") or {}

    slo_evidence: dict[str, Any] = {
        "operation_count": run_count,
        "completed_count": completed_count,
        "total_duration_ms": duration * 1000,
        "mean_latency_ms": (
            statistics.fmean(latency_samples) if latency_samples else 0.0
        ),
        "p95_latency_ms": percentile(latency_samples, 95),
        "p99_latency_ms": percentile(latency_samples, 99),
        "throughput_ops_per_second": ops_per_second,
        "database_bytes": database_bytes,
        "storage_bytes_per_run": (
            database_bytes / completed_count if completed_count else float(database_bytes)
        ),
        "observability_summary_ms": observability_ms,
        "metrics_run_count": metrics.run_count,
        "metrics_completed_count": metrics.completed_count,
        "metrics_failed_count": metrics.failed_count,
        "projection_span_count": len(projection.get("spans") or []),
        "projection_metrics_completed_count": int(
            projection_metrics.get("completed_count", -2)
        ),
        "redaction_violations": _projection_redaction_violations(projection),
        "thresholds": asdict(RuntimeSloThresholds()),
    }
    breaches = evaluate_runtime_slo(slo_evidence)
    slo_evidence["breaches"] = breaches
    slo_evidence["status"] = "fail" if breaches else "pass"

    error: str | None = first_error
    if breaches and error is None:
        error = "; ".join(breaches)

    return {
        "item_count": run_count,
        "duration": duration,
        "ops_per_second": ops_per_second,
        "operation_count": run_count,
        "control_plane_runtime_load_soak_count": completed_count,
        "latency_ms_samples": latency_samples,
        "database_size_mb": database_bytes / (1024 * 1024),
        "error_count": error_count,
        "slo": slo_evidence,
        "error": error,
    }
