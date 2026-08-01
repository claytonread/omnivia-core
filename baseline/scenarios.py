"""Golden fixtures captured from real Core code paths.

One ephemeral session drives the whole Core-executable part of the Phase 0
slice: a workspace is created, a small generated document set is imported,
memories are created and searched, a graph preview is assembled, and a small
graph is traversed. Each step is captured, normalised, and written to
``baseline/fixtures``.

Three properties make these fixtures usable as a freeze:

- **Real code paths.** Nothing is hand-written; every payload comes from calling
  the same services Platform calls.
- **Deterministic.** Identifiers, timestamps, and absolute paths are tokenised
  by :mod:`baseline.determinism`, and collections whose order Core does not
  guarantee are sorted with the sort key recorded in the fixture.
- **Public-safe.** The session runs entirely inside a temporary directory with
  generated content. It never reads the user's home directory and never opens
  the default ``~/.omnivia/memories.db``.

Slices whose behaviour lives outside Core get a fixture too, marked
``pending_external_capture`` with the evidence gap that blocks it. Recording the
gap is deliberate: an invented response would be worse than an absent one.

Every fixture states how it was produced in its ``capture_kind`` field. A
captured fixture is ``core_code_path``: a deterministic Core-side equivalent of
the slice, never a live Platform, MCP, or CLI response. The surface inventories
now carry the descriptors for those external operations, but their response
bodies are still uncaptured, and no fixture here claims otherwise.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baseline import (
    BASELINE_FORMAT_VERSION,
    BASELINE_TASK_ID,
    FIXTURE_DIR,
    REPO_ROOT,
)
from baseline.determinism import (
    Normalizer,
    diff_json,
    find_absolute_paths,
    format_differences,
    load_json,
    write_artifact,
)
from baseline.slices import require_known_slice
from baseline.storage import capture_connection_schema

CORE_SURFACE = "core_python"

STATUS_CAPTURED = "captured"
STATUS_PARTIAL = "partial"
STATUS_PENDING = "pending_external_capture"

#: How a fixture was produced. Recorded in every fixture so a Core-side
#: equivalent can never be mistaken for a live external response capture.
CAPTURE_KIND_CORE_CODE_PATH = "core_code_path"
CAPTURE_KIND_NOT_CAPTURED = "not_captured"

CORE_EQUIVALENT_NOTE = (
    "Captured by calling Core in this checkout. It is a deterministic Core-side "
    "equivalent of the slice, not a live Platform, MCP, or CLI response capture."
)

NOT_CAPTURED_NOTE = (
    "No request or response is recorded here. This slice belongs to a surface Core does "
    "not own; its operation descriptor, where one has been reviewed, lives in the surface "
    "inventory rather than in this fixture."
)

#: Generated workspace content. Keeping it here rather than in checked-in files
#: means the content hashes recorded in the fixtures cannot drift away from the
#: text that produced them.
WORKSPACE_DOCUMENTS: dict[str, str] = {
    "docs/adr/0001-portable-knowledge.md": (
        "# ADR 0001: Portable Knowledge Substrate\n"
        "\n"
        "Core owns portable knowledge contracts and keeps them backend neutral.\n"
        "\n"
        "Platform adapts the contracts to a runtime. Dev adapts them to developer tools.\n"
    ),
    "docs/notes/graph-review.md": (
        "# Graph Review\n"
        "\n"
        "Every graph edge must cite source evidence before it is treated as approved.\n"
        "\n"
        "Edges without evidence stay proposed and are surfaced as warnings.\n"
    ),
    "notes/inbox.txt": (
        "Workspace imports create one memory per content chunk.\n"
        "\n"
        "Each memory keeps a provenance reference back to the source file.\n"
    ),
}


@dataclass
class CaptureSession:
    """The ephemeral Core session every golden fixture is captured from."""

    root: Path
    normalizer: Normalizer
    database: Any
    workspace_repository: Any
    source_repository: Any
    chunk_repository: Any
    memory_repository: Any
    entity_repository: Any
    relationship_repository: Any
    workspace_service: Any
    memory_service: Any
    graph_service: Any
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def workspace_root(self) -> Path:
        return self.root / "workspace"

    @property
    def storage_root(self) -> Path:
        return self.root / "storage"


@dataclass(frozen=True)
class Scenario:
    """One golden fixture in the frozen slice."""

    slice_id: str
    title: str
    surface: str
    entrypoints: tuple[str, ...]
    capture: Callable[[CaptureSession], dict[str, Any]] | None = None
    status: str = STATUS_CAPTURED
    evidence_gap: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return f"{self.slice_id.replace('.', '-')}.json"

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.filename


def _document_paths() -> list[str]:
    return sorted(WORKSPACE_DOCUMENTS)


@contextmanager
def open_session() -> Iterator[CaptureSession]:
    """Build the ephemeral Core session, yield it, and tear it down."""
    from omnivia_memory.graph.repository import EntityRepository, RelationshipRepository
    from omnivia_memory.graph.service import GraphService
    from omnivia_memory.ingestion.repositories import ChunkRepository, SourceRepository
    from omnivia_memory.memory.service import MemoryService
    from omnivia_memory.memory_graph.fixtures import FIXTURE_TIME
    from omnivia_memory.persistence.database import Database, DatabaseConfig
    from omnivia_memory.persistence.repositories import MemoryRepository
    from omnivia_memory.workspace.repository import WorkspaceRepository
    from omnivia_memory.workspace.service import WorkspaceService

    with tempfile.TemporaryDirectory(prefix="omnivia-baseline-") as tmp:
        root = Path(tmp)
        workspace_root = root / "workspace"
        for relative, content in sorted(WORKSPACE_DOCUMENTS.items()):
            target = workspace_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (root / "storage").mkdir(parents=True, exist_ok=True)

        # An explicit database path under the temporary root. get_database() would
        # default to ~/.omnivia/memories.db, which the baseline must never open.
        database = Database(DatabaseConfig(db_path=root / "baseline-session.db"))
        database.connect()

        # FIXTURE_TIME is already a fixed constant in Core's in-code memory graph
        # fixture, so it is recorded literally instead of being tokenised. That
        # keeps the temporal fields of the graph preview readable as real data.
        normalizer = Normalizer(preserve_values=frozenset({FIXTURE_TIME}))
        # Longest-first replacement in the normalizer means the nested roots below
        # win over the temporary root they live in.
        normalizer.add_path_root("workspace-root", workspace_root)
        normalizer.add_path_root("workspace-storage", root / "storage")
        normalizer.add_path_root("session-root", root)
        normalizer.add_path_root("home", Path.home())
        normalizer.add_path_root("repo", REPO_ROOT)

        workspace_repository = WorkspaceRepository(database)
        source_repository = SourceRepository(database)
        chunk_repository = ChunkRepository(database)
        memory_repository = MemoryRepository(database)
        entity_repository = EntityRepository(database)
        relationship_repository = RelationshipRepository(database)
        memory_service = MemoryService(repository=memory_repository)

        session = CaptureSession(
            root=root,
            normalizer=normalizer,
            database=database,
            workspace_repository=workspace_repository,
            source_repository=source_repository,
            chunk_repository=chunk_repository,
            memory_repository=memory_repository,
            entity_repository=entity_repository,
            relationship_repository=relationship_repository,
            workspace_service=WorkspaceService(
                repository=workspace_repository,
                source_repository=source_repository,
                chunk_repository=chunk_repository,
                memory_service=memory_service,
            ),
            memory_service=memory_service,
            graph_service=GraphService(
                entity_repository=entity_repository,
                relationship_repository=relationship_repository,
            ),
        )
        try:
            yield session
        finally:
            database.close()


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #


def capture_workspace_create(session: CaptureSession) -> dict[str, Any]:
    from omnivia_memory.workspace.models import WorkspaceCreate

    request = WorkspaceCreate(
        name="Baseline Workspace",
        root_path=session.workspace_root,
        storage_path=session.storage_root,
        description="Ephemeral workspace used to freeze the Phase 0 baseline.",
        settings={"import_profile": "baseline"},
    )
    workspace = session.workspace_service.create(request)
    session.state["workspace"] = workspace
    # Seed the identifier map so the workspace id is the first stable token in
    # every fixture that mentions it.
    session.normalizer.normalize_string(workspace.id)
    return {
        "request": {
            "name": request.name,
            "root_path": str(request.root_path),
            "storage_path": str(request.storage_path),
            "description": request.description,
            "settings": request.settings,
        },
        "response": workspace.to_dict(),
    }


def capture_workspace_list(session: CaptureSession) -> dict[str, Any]:
    workspaces = session.workspace_service.list(limit=100, offset=0)
    return {
        "request": {"limit": 100, "offset": 0},
        "response": {
            "count": len(workspaces),
            "workspaces": [item.to_dict() for item in _sorted_by(workspaces, "name")],
        },
        "ordering": "repository returns created_at DESC; baseline sorts by name",
    }


def capture_workspace_get(session: CaptureSession) -> dict[str, Any]:
    workspace = session.state["workspace"]
    fetched = session.workspace_service.get(workspace.id)
    return {
        "request": {"workspace_id": workspace.id},
        "response": fetched.to_dict(),
    }


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


def capture_ingestion_import_directory(session: CaptureSession) -> dict[str, Any]:
    workspace = session.state["workspace"]
    summary = session.workspace_service.import_path(workspace.id)
    sources = session.source_repository.list_by_workspace(workspace.id)
    ordered_sources = sorted(sources, key=lambda item: item.path)
    source_payloads = []
    for source in ordered_sources:
        chunks = session.chunk_repository.get_by_source_id(source.id)
        source_payloads.append(
            {
                "source": source.to_dict(),
                "chunks": [chunk.to_dict() for chunk in chunks],
            }
        )
    return {
        "request": {
            "workspace_id": workspace.id,
            "documents": _document_paths(),
        },
        "response": {
            "summary": {
                "workspace_id": summary.workspace_id,
                "files_seen": summary.files_seen,
                "sources_created": summary.sources_created,
                "memories_created": summary.memories_created,
                "errors": summary.errors,
            },
            "workspace_after_import": session.workspace_service.get(workspace.id).to_dict(),
            "sources": source_payloads,
        },
        "ordering": (
            "FileScanner walks directories with Path.iterdir(), which is not ordered; "
            "the baseline sorts sources by path and relies on ChunkRepository's "
            "ORDER BY chunk_index for chunks"
        ),
    }


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #


def capture_memory_create(session: CaptureSession) -> dict[str, Any]:
    from omnivia_memory.lifecycle.rules import CreatedBy
    from omnivia_memory.memory.models import MemoryCreate
    from omnivia_memory.provenance.models import Source, SourceType

    request = MemoryCreate(
        content=(
            "Core owns portable knowledge contracts; Platform and Dev adapt them "
            "to their own runtimes."
        ),
        source=Source(
            type=SourceType.ADR,
            reference="docs/adr/0001-portable-knowledge.md",
            description="Baseline ADR reference",
        ),
        memory_type="decision",
        created_by=CreatedBy.HUMAN,
        workspace_id=session.state["workspace"].id,
    )
    memory = session.memory_service.create(request)
    session.state["memory"] = memory
    return {
        "request": {
            "content": request.content,
            "source": request.source.to_dict(),
            "memory_type": request.memory_type,
            "created_by": request.created_by.value,
            "workspace_id": request.workspace_id,
        },
        "response": memory.to_dict(),
        "notes": [
            (
                "Lifecycle state is assigned by LifecycleRules.get_initial_state from "
                "created_by, not by the caller."
            )
        ],
    }


def capture_memory_list(session: CaptureSession) -> dict[str, Any]:
    workspace = session.state["workspace"]
    memories = session.memory_service.list(limit=100, offset=0, workspace_id=workspace.id)
    return {
        "request": {"limit": 100, "offset": 0, "workspace_id": workspace.id},
        "response": {
            "count": len(memories),
            "stats": session.memory_service.get_stats(),
            "memories": [item.to_dict() for item in _sorted_by(memories, "content")],
        },
        "ordering": "repository returns created_at DESC; baseline sorts by content",
    }


def capture_memory_get(session: CaptureSession) -> dict[str, Any]:
    memory = session.state["memory"]
    fetched = session.memory_service.get(memory.id)
    return {
        "request": {"memory_id": memory.id},
        "response": fetched.to_dict(),
    }


def capture_memory_search(session: CaptureSession) -> dict[str, Any]:
    query = "provenance"
    results = session.memory_service.search(query, limit=20)
    return {
        "request": {"query": query, "limit": 20},
        "response": {
            "count": len(results),
            "memories": [item.to_dict() for item in _sorted_by(results, "content")],
        },
        "ordering": "repository returns created_at DESC; baseline sorts by content",
        "notes": [
            (
                "MemoryRepository.search uses SQLite LIKE with % and _ escaped; there is "
                "no relevance ranking in this baseline."
            )
        ],
    }


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


def capture_context_search(session: CaptureSession) -> dict[str, Any]:
    from omnivia_memory.search.service import SearchService

    workspace = session.state["workspace"]
    query = "contracts"
    search_service = SearchService()
    results = session.memory_service.search(query, limit=20, workspace_id=workspace.id)
    ordered = _sorted_by(results, "content")
    return {
        "request": {"query": query, "limit": 20, "workspace_id": workspace.id},
        "response": {
            "count": len(ordered),
            "results": [
                {
                    "memory": item.to_dict(),
                    "excerpt": search_service.highlight_matches(item, query),
                }
                for item in ordered
            ],
        },
        "ordering": "repository returns created_at DESC; baseline sorts by content",
        "notes": [
            (
                "Workspace-scoped keyword search plus SearchService.highlight_matches is "
                "the whole of Core's context retrieval today; SearchService.rank_results "
                "is an identity function in this baseline."
            )
        ],
    }


def capture_context_pack(session: CaptureSession) -> dict[str, Any]:
    schema = capture_connection_schema(session.database.connection)
    return {
        "request": None,
        "response": {
            "storage_schema": {"context_packs": schema["context_packs"]},
            "row_count": _scalar(session, "SELECT COUNT(*) FROM context_packs"),
        },
        "notes": [
            (
                "Core creates the context_packs table but implements no pack assembly, so "
                "only the storage half of this slice can be frozen here. The behavioural "
                "half is recorded as an evidence gap."
            ),
        ],
    }


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


def capture_graph_preview(session: CaptureSession) -> dict[str, Any]:
    from omnivia_memory.memory_graph.assembly import assemble_graph_preview
    from omnivia_memory.memory_graph.fixtures import (
        FIXTURE_TIME,
        build_memory_graph_fixture,
    )

    bundle = build_memory_graph_fixture()
    preview = assemble_graph_preview(
        workspace_id=bundle["source"].workspace_id,
        query="source:source-001",
        source=bundle["source"],
        entities=bundle["entities"],
        facts=bundle["facts"],
        generated_at=FIXTURE_TIME,
        node_limit=100,
        edge_limit=150,
    )
    return {
        "request": {
            "workspace_id": bundle["source"].workspace_id,
            "query": "source:source-001",
            "node_limit": 100,
            "edge_limit": 150,
            "record_counts": {
                "entities": len(bundle["entities"]),
                "facts": len(bundle["facts"]),
                "segments": len(bundle["segments"]),
            },
        },
        "response": preview.to_dict(),
        "notes": [
            (
                "Assembled from the in-code memory graph fixture. Its FIXTURE_TIME constant "
                "is already deterministic, so temporal fields are recorded literally rather "
                "than tokenised."
            ),
            (
                "The third fact in the bundle has no source evidence and is expected to "
                "surface as a warning rather than a silently dropped edge."
            ),
        ],
    }


def capture_graph_traversal(session: CaptureSession) -> dict[str, Any]:
    from omnivia_memory.graph.models import EntityType, RelationshipType

    service = session.graph_service
    core = service.create_entity("OmniVia Core", EntityType.SYSTEM, source_id="source-baseline")
    contracts = service.create_entity(
        "Portable Contracts", EntityType.CONCEPT, source_id="source-baseline"
    )
    platform = service.create_entity(
        "Platform Runtime", EntityType.SYSTEM, source_id="source-baseline"
    )
    shell = service.create_entity("Desktop Shell", EntityType.PRODUCT, source_id="source-baseline")

    # A single chain, so the shortest path between the endpoints is unique and
    # BFS neighbour ordering cannot change the captured result.
    service.create_relationship(contracts.id, core.id, RelationshipType.PART_OF)
    service.create_relationship(platform.id, contracts.id, RelationshipType.DEPENDS_ON)
    service.create_relationship(shell.id, platform.id, RelationshipType.USES)

    neighbours = service.get_neighbors(contracts.id)
    backlinks = service.get_backlinks(contracts.id)
    path = service.find_path(core.id, shell.id, max_depth=5)

    return {
        "request": {
            "entities": [
                {"name": "OmniVia Core", "entity_type": "system"},
                {"name": "Portable Contracts", "entity_type": "concept"},
                {"name": "Platform Runtime", "entity_type": "system"},
                {"name": "Desktop Shell", "entity_type": "product"},
            ],
            "relationships": [
                {"source": "Portable Contracts", "target": "OmniVia Core", "type": "part_of"},
                {
                    "source": "Platform Runtime",
                    "target": "Portable Contracts",
                    "type": "depends_on",
                },
                {"source": "Desktop Shell", "target": "Platform Runtime", "type": "uses"},
            ],
            "neighbors_of": "Portable Contracts",
            "backlinks_of": "Portable Contracts",
            "find_path": {"from": "OmniVia Core", "to": "Desktop Shell", "max_depth": 5},
        },
        "response": {
            "stats": service.get_stats(),
            "neighbors": [
                {"entity": entity.to_dict(), "relationship": relationship.to_dict()}
                for entity, relationship in sorted(
                    neighbours, key=lambda pair: (pair[0].name, pair[1].relationship_type.value)
                )
            ],
            "backlinks": [
                {"entity": entity.to_dict(), "relationship": relationship.to_dict()}
                for entity, relationship in sorted(
                    backlinks, key=lambda pair: (pair[0].name, pair[1].relationship_type.value)
                )
            ],
            "path": [entity.to_dict() for entity in (path or [])],
            "path_found": path is not None,
        },
        "ordering": (
            "GraphService.get_neighbors returns outgoing relationships before incoming "
            "ones; the baseline sorts neighbour and backlink pairs by entity name then "
            "relationship type. The find_path chain has a unique shortest path, so its "
            "order is behaviour, not a sort"
        ),
        "notes": [
            (
                "find_path treats relationships as undirected, so the captured path runs "
                "against edge direction."
            ),
            (
                "Agent-created entities and relationships stay in the proposed approval "
                "state; the baseline records that governance default."
            ),
        ],
    }


# --------------------------------------------------------------------------- #
# Scenario registry
# --------------------------------------------------------------------------- #

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        slice_id="health.health",
        title="Service health probe",
        surface="platform_http",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-004",
        notes=(
            "Core exposes no health primitive, so there is no Core code path to capture.",
            "The response stays null rather than being invented.",
            (
                "GET /health is recorded in the Platform inventory from reviewed source; "
                "its response body is not."
            ),
        ),
    ),
    Scenario(
        slice_id="health.readiness",
        title="Service readiness probe",
        surface="platform_http",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-004",
        notes=(
            "Readiness depends on Platform storage and lifecycle wiring Core does not own.",
            (
                "The reviewed neutral route table has no readiness route, so this slice has "
                "no descriptor either."
            ),
        ),
    ),
    Scenario(
        slice_id="workspace.create",
        title="Create a workspace",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.workspace.service.WorkspaceService.create",),
        capture=capture_workspace_create,
        notes=(
            (
                "storage_path is passed explicitly. Left unset, WorkspaceCreate defaults it "
                "to ~/.omnivia/workspaces/<id>, which the baseline must never write to."
            ),
        ),
    ),
    Scenario(
        slice_id="workspace.list",
        title="List workspaces",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.workspace.service.WorkspaceService.list",),
        capture=capture_workspace_list,
    ),
    Scenario(
        slice_id="workspace.get",
        title="Get a workspace by id",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.workspace.service.WorkspaceService.get",),
        capture=capture_workspace_get,
    ),
    Scenario(
        slice_id="ingestion.import_directory",
        title="Import a directory into a workspace",
        surface=CORE_SURFACE,
        entrypoints=(
            "omnivia_memory.workspace.service.WorkspaceService.import_path",
            "omnivia_memory.ingestion.pipeline.IngestionPipeline.ingest_directory",
        ),
        capture=capture_ingestion_import_directory,
        notes=(
            (
                "Only Markdown and text extractors are exercised. The PDF and DOCX "
                "extractors need optional dependencies and are covered by the existing "
                "ingestion tests instead."
            ),
        ),
    ),
    Scenario(
        slice_id="memory.create",
        title="Create a memory",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.memory.service.MemoryService.create",),
        capture=capture_memory_create,
    ),
    Scenario(
        slice_id="memory.list",
        title="List memories",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.memory.service.MemoryService.list",),
        capture=capture_memory_list,
    ),
    Scenario(
        slice_id="memory.get",
        title="Get a memory by id",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.memory.service.MemoryService.get",),
        capture=capture_memory_get,
    ),
    Scenario(
        slice_id="memory.search",
        title="Keyword-search memories",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.memory.service.MemoryService.search",),
        capture=capture_memory_search,
    ),
    Scenario(
        slice_id="context.search",
        title="Workspace-scoped context retrieval",
        surface=CORE_SURFACE,
        entrypoints=(
            "omnivia_memory.memory.service.MemoryService.search",
            "omnivia_memory.search.service.SearchService.highlight_matches",
        ),
        capture=capture_context_search,
    ),
    Scenario(
        slice_id="context.pack",
        title="Context pack storage schema",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.persistence.database.Database._init_schema",),
        capture=capture_context_pack,
        status=STATUS_PARTIAL,
        evidence_gap="GAP-005",
    ),
    Scenario(
        slice_id="graph.preview",
        title="Bounded graph preview response",
        surface=CORE_SURFACE,
        entrypoints=("omnivia_memory.memory_graph.assembly.assemble_graph_preview",),
        capture=capture_graph_preview,
    ),
    Scenario(
        slice_id="graph.traversal",
        title="Graph neighbours, backlinks, and path finding",
        surface=CORE_SURFACE,
        entrypoints=(
            "omnivia_memory.graph.service.GraphService.get_neighbors",
            "omnivia_memory.graph.service.GraphService.get_backlinks",
            "omnivia_memory.graph.service.GraphService.find_path",
        ),
        capture=capture_graph_traversal,
    ),
    Scenario(
        slice_id="mcp.tools_discovery",
        title="MCP tool discovery",
        surface="mcp_tools",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-008",
        notes=(
            "Core ships no MCP server; tool discovery is a Dev surface.",
            "The tools/list method and the base tool list are recorded in the MCP inventory.",
        ),
    ),
    Scenario(
        slice_id="mcp.read",
        title="One MCP read operation",
        surface="mcp_tools",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-008",
        notes=(
            (
                "The Core behaviour behind an MCP read is frozen by memory.get and "
                "memory.search; only the memory_get response envelope is missing."
            ),
        ),
    ),
    Scenario(
        slice_id="mcp.write",
        title="One MCP write operation",
        surface="mcp_tools",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-008",
        notes=(
            (
                "The Core behaviour behind an MCP write is frozen by memory.create; only "
                "the memory_store response envelope is missing."
            ),
        ),
    ),
    Scenario(
        slice_id="cli.read",
        title="One CLI read operation",
        surface="cli_commands",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-008",
        notes=(
            "The product CLI lives in Dev; Core's only argparse entry points are benchmarks.",
            "The `get` command is recorded in the CLI inventory; its output is not.",
        ),
    ),
    Scenario(
        slice_id="cli.write",
        title="One CLI write operation",
        surface="cli_commands",
        entrypoints=(),
        status=STATUS_PENDING,
        evidence_gap="GAP-008",
        notes=(
            (
                "Mirrors the MCP write so the two surfaces stay comparable: `create` pairs "
                "with memory_store."
            ),
        ),
    ),
)

SCENARIOS_BY_SLICE: dict[str, Scenario] = {item.slice_id: item for item in SCENARIOS}


def build_fixtures() -> dict[str, dict[str, Any]]:
    """Capture every fixture in one session and return them keyed by slice id."""
    for scenario in SCENARIOS:
        require_known_slice(scenario.slice_id, context="baseline.scenarios")

    fixtures: dict[str, dict[str, Any]] = {}
    with open_session() as session:
        for scenario in SCENARIOS:
            if scenario.capture is None:
                fixtures[scenario.slice_id] = _pending_fixture(scenario)
                continue
            captured = scenario.capture(session)
            fixtures[scenario.slice_id] = _captured_fixture(scenario, captured, session)
    return fixtures


def write_fixtures() -> list[Path]:
    """Regenerate every tracked golden fixture."""
    written = []
    for slice_id, document in sorted(build_fixtures().items()):
        path = SCENARIOS_BY_SLICE[slice_id].path
        write_artifact(path, document)
        written.append(path)
    return written


def verify_fixtures() -> list[str]:
    """Return precise drift messages, or an empty list when fixtures match."""
    problems: list[str] = []
    actual = build_fixtures()
    for slice_id, document in sorted(actual.items()):
        scenario = SCENARIOS_BY_SLICE[slice_id]
        try:
            expected = load_json(scenario.path)
        except FileNotFoundError as exc:
            problems.append(str(exc))
            continue
        differences = diff_json(expected, document)
        if differences:
            problems.append(
                f"Golden fixture {scenario.path.relative_to(REPO_ROOT)} drifted from the "
                f"frozen Phase 0 baseline:\n{format_differences(differences)}"
            )
    return problems


def _captured_fixture(
    scenario: Scenario,
    captured: dict[str, Any],
    session: CaptureSession,
) -> dict[str, Any]:
    normalizer = session.normalizer
    normalizer.start_scope()
    document: dict[str, Any] = {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "scenario": scenario.slice_id,
        "baseline_slice": scenario.slice_id,
        "title": scenario.title,
        "surface": scenario.surface,
        "status": scenario.status,
        "capture_kind": CAPTURE_KIND_CORE_CODE_PATH,
        "evidence_gap": scenario.evidence_gap,
        "entrypoints": list(scenario.entrypoints),
        "notes": [*scenario.notes, *captured.get("notes", []), CORE_EQUIVALENT_NOTE],
        "ordering": captured.get("ordering"),
        "request": normalizer.normalize(captured.get("request")),
        "response": normalizer.normalize(captured.get("response")),
    }
    document["normalization"] = {
        "redactions": normalizer.scoped_redactions,
        "configured_path_roots": sorted(f"<{root.token}>" for root in normalizer.path_roots),
    }
    leaks = find_absolute_paths(
        {"request": document["request"], "response": document["response"]}
    )
    if leaks:
        raise ValueError(
            f"fixture {scenario.slice_id} still contains machine-specific absolute "
            f"paths after normalization: {leaks}"
        )
    return document


def _pending_fixture(scenario: Scenario) -> dict[str, Any]:
    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "scenario": scenario.slice_id,
        "baseline_slice": scenario.slice_id,
        "title": scenario.title,
        "surface": scenario.surface,
        "status": STATUS_PENDING,
        "capture_kind": CAPTURE_KIND_NOT_CAPTURED,
        "evidence_gap": scenario.evidence_gap,
        "entrypoints": list(scenario.entrypoints),
        "notes": [*scenario.notes, NOT_CAPTURED_NOTE],
        "ordering": None,
        "request": None,
        "response": None,
        "normalization": {"redactions": [], "configured_path_roots": []},
    }


def _sorted_by(items: list[Any], attribute: str) -> list[Any]:
    return sorted(items, key=lambda item: (getattr(item, attribute), item.id))


def _scalar(session: CaptureSession, query: str) -> Any:
    return session.database.execute(query).fetchone()[0]
