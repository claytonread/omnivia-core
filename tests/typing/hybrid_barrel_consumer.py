"""Strict-mypy consumer fixture for the six ``hybrid_facade`` barrels.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. Every other
fixture in this directory consumes a *leaf* route -- one converted module and the
symbols ``baseline.inventory.FACADE_ROUTES`` records for it. This one consumes
the six **barrels** above those leaves, which are module routes rather than
symbol routes and so are deliberately absent from ``FACADE_ROUTES``:

    omnivia_memory.graph
    omnivia_memory.ingestion
    omnivia_memory.ingestion.watcher
    omnivia_memory.memory
    omnivia_memory.memory_graph
    omnivia_memory.workspace

It imports all ninety-three names those barrels advertise -- every entry of every
``__all__``, and nothing else -- through the legacy barrel paths only, never
through ``omnivia_core`` and never through the leaves. That is the point: a
hybrid barrel publishes a *mixed* surface, and a typed caller must see precise
types on both halves.

* The sixty-two **portable** names resolve, transitively through the barrels'
  converted children, to the canonical Core objects. The assertions below pin
  their canonical field and return types, so a facade that degraded one to
  ``Any`` -- or re-exported a structurally similar lookalike -- fails here.
* The thirty-one **runtime** names stay legacy-owned: Core deliberately never
  gains them. They are exercised through *parameters* rather than constructed,
  because several of their ``__init__`` methods are unannotated legacy code and
  calling one from strict code would fail on the call rather than on the type
  this fixture is about. What is asserted instead is the precise type each
  runtime member yields -- which is, in almost every case, one of the canonical
  portable types, so these assertions double as the runtime-composition gate.

No name needs an alias: the six ``__all__`` tuples are pairwise disjoint, which
``tests/test_typed_facade_consumers.py`` pins as its own fact. If a future barrel
export ever collided, this fixture would need a rename and that audit would say
so rather than letting one binding silently shadow another.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``).
"""

from pathlib import Path
from typing import Any, assert_type

from omnivia_memory.graph import (
    ApprovalStatus,
    Entity,
    EntityType,
    GraphSearchError,
    GraphSearchQuery,
    GraphSearchResult,
    GraphSearchResultSet,
    GraphSearchService,
    Relationship,
    RelationshipType,
)
from omnivia_memory.ingestion import (
    BaseChunker,
    BaseExtractor,
    CharacterChunker,
    Chunk,
    ChunkConfig,
    ChunkRepository,
    DOCXExtractor,
    ExtractionResult,
    FileInfo,
    FileScanner,
    FileType,
    IngestionPipeline,
    IngestResult,
    MarkdownExtractor,
    ParagraphChunker,
    ParseStatus,
    PDFExtractor,
    ScanOptions,
    Source,
)
from omnivia_memory.ingestion.watcher import (
    DebounceConfig,
    Debouncer,
    FileChange,
    FileChangeBatch,
    FileChangeType,
    IndexerScheduler,
    IndexerState,
    IndexerStatus,
    ScheduledJob,
    SourceReference,
    SourceTracker,
    WatchedPath,
)
from omnivia_memory.memory import (
    InvalidTransitionError,
    Memory,
    MemoryCreate,
    MemoryNotFoundError,
    MemoryService,
    MemoryServiceError,
    MemoryUpdate,
)
from omnivia_memory.memory_graph import (
    FIXTURE_TIME,
    Confidence,
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewKind,
    GraphPreviewNode,
    GraphPreviewResponse,
    GraphPreviewState,
    IngestionGraphAdapterError,
    IngestionGraphWriteResult,
    MemoryEntity,
    MemoryFact,
    MemoryFactStatus,
    MemoryGraphFixture,
    MemoryGraphStore,
    MemoryGraphStoreError,
    MemorySegment,
    MemorySegmentKind,
    MemorySource,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
    RetrievalTrace,
    SourceRef,
    ValidationResult,
    assemble_evidence_graph,
    assemble_graph_preview,
    build_memory_graph_fixture,
    chunk_to_memory_segment,
    redact_segment_preview,
    source_to_memory_source,
    validate_evidence_graph_response,
    validate_graph_preview_response,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
    write_ingestion_records_to_graph,
)
from omnivia_memory.workspace import (
    ImportSummary,
    Workspace,
    WorkspaceCreate,
    WorkspaceIndexStatus,
    WorkspaceRepository,
    WorkspaceService,
    WorkspaceUpdate,
)

# ---------------------------------------------------------------------------
# graph: eight portable records, two runtime names.
# ---------------------------------------------------------------------------


def graph_records() -> GraphSearchResultSet:
    """The portable half of ``omnivia_memory.graph``, built through the barrel."""
    entity = Entity(name="Alice", entity_type=EntityType.PERSON)
    assert_type(entity.name, str)
    assert_type(entity.entity_type, EntityType)
    assert_type(entity.approval_status, ApprovalStatus)
    assert_type(entity.source_id, str | None)
    assert_type(entity.to_dict(), dict[str, Any])
    assert_type(Entity.from_dict(entity.to_dict()), Entity)

    relationship = Relationship(
        source_entity_id=entity.id,
        target_entity_id="entity-2",
        relationship_type=RelationshipType.RELATES_TO,
    )
    assert_type(relationship.relationship_type, RelationshipType)
    assert_type(relationship.approval_status, ApprovalStatus)

    query = GraphSearchQuery(query="alice", entity_types=[EntityType.PERSON])
    assert_type(query.entity_types, list[EntityType])
    assert_type(query.relationship_types, list[RelationshipType])
    assert_type(query.limit, int | None)

    result = GraphSearchResult(entity=entity, score=0.5, matched_on="name")
    assert_type(result.entity, Entity)
    assert_type(result.score, float)
    assert_type(result.context_entities, list[Entity])

    results = GraphSearchResultSet(results=[result], total_count=1, query=query)
    assert_type(results.results, list[GraphSearchResult])
    assert_type(results.query, GraphSearchQuery)
    return results


def graph_runtime(service: GraphSearchService) -> GraphSearchResultSet:
    """The runtime half: legacy-owned, and composing canonical graph records."""
    assert_type(service.search_entities("alice"), list[Entity])
    assert_type(service.search_with_context("alice"), GraphSearchResultSet)
    assert_type(service.get_entity_context("entity-1"), list[tuple[Entity, list[Entity]]])
    try:
        return service.search_with_context("alice")
    except GraphSearchError as error:
        assert_type(error, GraphSearchError)
        raise


# ---------------------------------------------------------------------------
# ingestion: five portable records, fourteen runtime names.
# ---------------------------------------------------------------------------


def ingestion_records() -> Chunk:
    """The portable half of ``omnivia_memory.ingestion``."""
    source = Source(path="/workspace/a.md", file_type=FileType.MARKDOWN)
    assert_type(source.file_type, FileType)
    assert_type(source.status, ParseStatus)
    assert_type(source.workspace_id, str | None)
    assert_type(Source.from_dict(source.to_dict()), Source)

    extraction = ExtractionResult(content="hello", status=ParseStatus.SUCCESS)
    assert_type(extraction.content, str | None)
    assert_type(extraction.status, ParseStatus)

    chunk = Chunk(source_id=source.id, chunk_index=0, content="hello")
    assert_type(chunk.chunk_index, int)
    assert_type(chunk.content_hash, str | None)
    return chunk


def ingestion_runtime_inputs(root: Path) -> tuple[ChunkConfig, ScanOptions, IngestResult]:
    """The runtime half's annotated input records, which Core never gains."""
    config = ChunkConfig(source_id="source-1")
    assert_type(config.chunk_size, int)
    assert_type(config.max_chunks, int | None)

    options = ScanOptions(root_path=root)
    assert_type(options.root_path, Path)
    assert_type(options.ignore_patterns, list[str])
    assert_type(options.max_depth, int | None)

    result = IngestResult()
    # The runtime result composes the canonical ingestion records.
    assert_type(result.source, Source | None)
    assert_type(result.chunks, list[Chunk])
    return config, options, result


def ingestion_runtime(
    chunker: BaseChunker,
    paragraph_chunker: ParagraphChunker,
    character_chunker: CharacterChunker,
    extractor: BaseExtractor,
    markdown: MarkdownExtractor,
    pdf: PDFExtractor,
    docx: DOCXExtractor,
    scanner: FileScanner,
    pipeline: IngestionPipeline,
    chunks: ChunkRepository,
    info: FileInfo,
    options: ScanOptions,
    path: Path,
) -> IngestResult:
    """Every runtime name, exercised through its annotated members only.

    Each assertion lands on a *canonical* ingestion type, which is what makes
    this the runtime-composition gate as well as a typing gate: the legacy
    runtime consumes the converted contracts.
    """
    assert_type(chunker.chunk("hello"), list[Chunk])
    assert_type(paragraph_chunker.chunk("hello"), list[Chunk])
    assert_type(character_chunker.chunk("hello"), list[Chunk])
    assert_type(extractor.extract(path), ExtractionResult)
    assert_type(markdown.extract(path), ExtractionResult)
    assert_type(pdf.extract(path), ExtractionResult)
    assert_type(docx.extract(path), ExtractionResult)
    assert_type(scanner.scan(options), list[FileInfo])
    assert_type(info.path, Path)
    assert_type(info.file_type, FileType)
    assert_type(chunks.get_by_source_id("source-1"), list[Chunk])
    assert_type(chunks.get_by_id("chunk-1"), Chunk | None)
    return pipeline.ingest_file(path)


# ---------------------------------------------------------------------------
# ingestion.watcher: ten portable records, two runtime names.
# ---------------------------------------------------------------------------


def watcher_records() -> FileChangeBatch:
    """The portable half of ``omnivia_memory.ingestion.watcher``."""
    change = FileChange(path="/workspace/a.md", event_type=FileChangeType.MODIFIED)
    assert_type(change.event_type, FileChangeType)
    assert_type(change.old_path, str | None)

    batch = FileChangeBatch(changes=[change], debounce_key="workspace-1")
    assert_type(batch.changes, list[FileChange])

    config = DebounceConfig()
    assert_type(config.initial_delay_ms, int)
    assert_type(config.min_events, int)

    watched = WatchedPath(path="/workspace", workspace_id="workspace-1")
    assert_type(watched.recursive, bool)
    assert_type(watched.ignore_patterns, list[str])

    # The watcher models' own ``SourceReference``, which is *not* the distinct
    # dataclass the runtime-only tracker defines under the same name.
    reference = SourceReference(
        watched_path="/workspace",
        source_path="/workspace/a.md",
        source_id="source-1",
        workspace_id="workspace-1",
    )
    assert_type(reference.last_known_hash, str | None)

    status = IndexerStatus(state=IndexerState.IDLE, workspace_id="workspace-1")
    assert_type(status.state, IndexerState)
    assert_type(status.active_watched_paths, list[str])

    job = ScheduledJob(
        job_id="job-1",
        job_type="reindex",
        workspace_id="workspace-1",
        scheduled_at="2026-07-30T00:00:00+00:00",
    )
    assert_type(job.delay_seconds, float)
    return batch


def watcher_scheduler(scheduler: IndexerScheduler) -> str:
    """The scheduler interface Core owns and platforms implement."""
    assert_type(scheduler.schedule_reindex("workspace-1"), str)
    return scheduler.schedule_full_scan("workspace-1")


def watcher_runtime(
    debouncer: Debouncer, tracker: SourceTracker, change: FileChange
) -> list[FileChangeBatch]:
    """The runtime half: the debouncer consumes the canonical watcher models,
    while the tracker keeps its own same-named reference record -- which is why
    ``list_by_workspace`` below is deliberately *not* asserted to be a list of
    the canonical ``SourceReference``."""
    assert_type(debouncer.push(change), None)
    assert_type(debouncer.pending_count(), int)
    assert_type(tracker.count(), int)
    assert_type(tracker.update_hash("/workspace/a.md", "workspace-1", "abc"), bool)
    return debouncer.flush_all()


# ---------------------------------------------------------------------------
# memory: three portable records, four runtime names.
# ---------------------------------------------------------------------------


def memory_records(memory: Memory, create: MemoryCreate) -> MemoryUpdate:
    """The portable half of ``omnivia_memory.memory``.

    ``Memory``/``MemoryCreate`` are taken as parameters rather than built here:
    both carry a ``source`` field typed as the *provenance* domain's ``Source``,
    and the only ``Source`` this fixture may import is the ingestion record the
    ``ingestion`` barrel publishes under the same name. Constructing one here
    would type-check the wrong contract; the field assertions below pin the
    right one without naming it.
    """
    assert_type(memory.content, str)
    assert_type(memory.memory_type, str)
    assert_type(memory.workspace_id, str | None)
    assert_type(memory.to_dict(), dict[str, Any])
    assert_type(Memory.from_dict(memory.to_dict()), Memory)
    assert_type(create.content, str)
    assert_type(create.workspace_id, str | None)

    update = MemoryUpdate(content="a better fact")
    assert_type(update.content, str | None)
    assert_type(update.memory_type, str | None)
    return update


def memory_runtime(service: MemoryService, create: MemoryCreate) -> Memory:
    """The runtime half: legacy-owned, composing the canonical memory records."""
    assert_type(service.create(create), Memory)
    assert_type(service.get("memory-1"), Memory)
    assert_type(service.approve("memory-1"), Memory)
    assert_type(service.delete("memory-1"), bool)
    assert_type(service.get_stats(), dict[str, Any])
    try:
        return service.create(create)
    except MemoryNotFoundError as missing:
        assert_type(missing, MemoryNotFoundError)
        raise
    except InvalidTransitionError as invalid:
        assert_type(invalid, InvalidTransitionError)
        raise
    except MemoryServiceError as failed:
        assert_type(failed, MemoryServiceError)
        raise


# ---------------------------------------------------------------------------
# memory_graph: thirty-one portable names, seven runtime names.
# ---------------------------------------------------------------------------


def memory_graph_records() -> MemoryGraphFixture:
    """The portable half of ``omnivia_memory.memory_graph``."""
    assert_type(FIXTURE_TIME, str)

    reference: SourceRef = SourceRef(source_id="source-001")
    assert_type(reference.confidence, Confidence | None)
    assert_type(reference.span, dict[str, Any] | None)

    source = MemorySource(
        id="source-001",
        workspace_id="workspace-001",
        type=MemorySourceType.FILE,
        uri="notes/a.md",
        title="A",
        freshness=MemorySourceFreshness.FRESH,
        status=MemorySourceStatus.READY,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    assert_type(source.type, MemorySourceType)
    assert_type(source.freshness, MemorySourceFreshness)
    assert_type(source.status, MemorySourceStatus)

    segment = MemorySegment(
        id="segment-001",
        source_id=source.id,
        workspace_id=source.workspace_id,
        kind=MemorySegmentKind.TEXT,
        label="A",
        parser="markdown",
        parser_settings_ref="settings-1",
        created_at=FIXTURE_TIME,
    )
    assert_type(segment.kind, MemorySegmentKind)

    entity = MemoryEntity(
        id="entity-001",
        workspace_id=source.workspace_id,
        type="person",
        canonical_name="Alice",
        aliases=[],
        confidence=0.9,
        source_refs=[reference],
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    assert_type(entity.source_refs, list[SourceRef])
    assert_type(entity.confidence, Confidence)

    fact = MemoryFact(
        id="fact-001",
        workspace_id=source.workspace_id,
        subject_id=entity.id,
        predicate="works_at",
        confidence=0.8,
        source_refs=[reference],
        status=MemoryFactStatus.APPROVED,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
    assert_type(fact.status, MemoryFactStatus)
    assert_type(fact.object_value, str | int | float | bool | None)

    preview: GraphPreviewResponse = assemble_graph_preview(
        workspace_id=source.workspace_id,
        query="alice",
        source=source,
        entities=[entity],
        facts=[fact],
        generated_at=FIXTURE_TIME,
        node_limit=50,
        edge_limit=50,
    )
    assert_type(preview.nodes, list[GraphPreviewNode])
    assert_type(preview.edges, list[GraphPreviewEdge])
    assert_type(preview.limits, dict[str, int])

    evidence: EvidenceGraphResponse = assemble_evidence_graph(
        workspace_id=source.workspace_id,
        answer_id=None,
        query="alice",
        source=source,
        entities=[entity],
        facts=[fact],
        generated_at=FIXTURE_TIME,
    )
    assert_type(evidence.citations, list[SourceRef])
    assert_type(evidence.answer_id, str | None)

    node = GraphPreviewNode(
        id="node-1",
        label="Alice",
        type="person",
        kind=GraphPreviewKind.ENTITY,
        state=GraphPreviewState.READY,
    )
    assert_type(node.kind, GraphPreviewKind)
    assert_type(node.state, GraphPreviewState)
    assert_type(node.display, dict[str, Any])

    edge = GraphPreviewEdge(
        id="edge-1",
        source="node-1",
        target="node-2",
        label="works_at",
        type="fact",
        state=GraphPreviewState.READY,
    )
    assert_type(edge.state, GraphPreviewState)

    trace = RetrievalTrace(
        id="trace-1",
        workspace_id=source.workspace_id,
        query="alice",
        mode="hybrid",
        matched_fact_ids=[fact.id],
        matched_segment_ids=[segment.id],
        rank_scores={"fact-001": 0.8},
        timing={"total_ms": 1},
        resource_indicators={},
        warnings=[],
        generated_at=FIXTURE_TIME,
    )
    assert_type(trace.rank_scores, dict[str, float])
    assert_type(trace.timing, dict[str, int | float])

    assert_type(redact_segment_preview(segment), dict[str, object])
    return build_memory_graph_fixture()


def memory_graph_validation(fixture: MemoryGraphFixture) -> ValidationResult:
    """``ValidationResult`` is the exact shared primitive, reached through the
    memory graph barrel rather than through ``_shared``."""
    source_result = validate_memory_source(fixture["source"])
    assert_type(source_result, ValidationResult)
    assert_type(source_result.valid, bool)
    assert_type(source_result.errors, list[str])
    assert_type(source_result.warnings, list[str])
    assert_type(validate_memory_segment(fixture["segments"][0]), ValidationResult)
    assert_type(validate_memory_entity(fixture["entities"][0]), ValidationResult)
    assert_type(validate_memory_fact(fixture["facts"][0]), ValidationResult)
    assert_type(
        validate_graph_preview_response(fixture["graph_preview"]), ValidationResult
    )
    assert_type(
        validate_evidence_graph_response(fixture["evidence_graph"]), ValidationResult
    )
    return source_result


def memory_graph_runtime(
    store: MemoryGraphStore,
    source: Source,
    chunk: Chunk,
    workspace_root: Path,
) -> IngestionGraphWriteResult:
    """The runtime half: the store and the ingestion adapter stay legacy-owned,
    and both compose the canonical memory-graph *and* canonical ingestion
    records -- which is exactly what these assertions pin."""
    assert_type(store.get_source("workspace-001", "source-001"), MemorySource | None)
    assert_type(store.list_sources("workspace-001"), list[MemorySource])
    assert_type(store.list_segments("workspace-001"), list[MemorySegment])
    assert_type(store.list_entities("workspace-001"), list[MemoryEntity])
    assert_type(store.list_facts("workspace-001"), list[MemoryFact])
    assert_type(store.has_workspace("workspace-001"), bool)

    memory_source = source_to_memory_source(source, workspace_root=workspace_root)
    assert_type(memory_source, MemorySource)
    segment = chunk_to_memory_segment(
        chunk, source=source, workspace_root=workspace_root
    )
    assert_type(segment, MemorySegment)

    try:
        written = write_ingestion_records_to_graph(
            store=store,
            source=source,
            chunks=[chunk],
            workspace_root=workspace_root,
        )
    except IngestionGraphAdapterError as adapter_error:
        assert_type(adapter_error, IngestionGraphAdapterError)
        raise
    except MemoryGraphStoreError as store_error:
        assert_type(store_error, MemoryGraphStoreError)
        raise
    assert_type(written, IngestionGraphWriteResult)
    return written


# ---------------------------------------------------------------------------
# workspace: five portable records, two runtime names.
# ---------------------------------------------------------------------------


def workspace_records(root: Path) -> WorkspaceCreate:
    """The portable half of ``omnivia_memory.workspace``."""
    workspace = Workspace(
        name="My Workspace",
        root_path=str(root),
        storage_path=str(root / ".omnivia"),
    )
    assert_type(workspace.index_status, WorkspaceIndexStatus)
    assert_type(workspace.settings, dict[str, Any])
    assert_type(workspace.last_indexed_at, str | None)

    update = WorkspaceUpdate(name="Renamed")
    assert_type(update.root_path, Path | None)
    assert_type(update.index_status, WorkspaceIndexStatus | None)
    assert_type(update.apply_to(workspace), bool)

    summary = ImportSummary(
        workspace_id=workspace.id,
        files_seen=1,
        sources_created=1,
        memories_created=1,
    )
    assert_type(summary.errors, list[str])

    # The create input is ``Path``-typed where the record it builds is ``str``.
    create = WorkspaceCreate(name="My Workspace", root_path=root)
    assert_type(create.root_path, Path)
    assert_type(create.storage_path, Path | None)
    return create


def workspace_runtime(
    repository: WorkspaceRepository,
    service: WorkspaceService,
    create: WorkspaceCreate,
    root: Path,
) -> ImportSummary:
    """The runtime half: legacy-owned, composing the canonical workspace records
    and returning the canonical ``ImportSummary``."""
    assert_type(repository.get_by_id("workspace-1"), Workspace | None)
    assert_type(repository.list_all(), list[Workspace])
    assert_type(repository.delete("workspace-1"), bool)
    assert_type(service.create(create), Workspace)
    assert_type(service.get("workspace-1"), Workspace)
    assert_type(service.list(), list[Workspace])
    return service.import_path("workspace-1", root)
