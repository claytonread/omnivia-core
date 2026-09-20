"""Map local ingestion records into durable memory graph records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnivia_memory.ingestion.models import Chunk, FileType, ParseStatus, Source
from omnivia_memory.memory_graph.models import (
    MemorySegment,
    MemorySegmentKind,
    MemorySource,
    MemorySourceFreshness,
    MemorySourceStatus,
    MemorySourceType,
)
from omnivia_memory.memory_graph.store import MemoryGraphStore


class IngestionGraphAdapterError(ValueError):
    """Raised when an ingestion record cannot safely become a graph record."""


@dataclass(frozen=True)
class IngestionGraphWriteResult:
    """Counters from writing ingestion records into the graph store."""

    source_id: str
    segments_created: int


def source_to_memory_source(
    source: Source,
    *,
    workspace_root: Path | str,
) -> MemorySource:
    """Return a graph source using a safe workspace-relative URI."""

    workspace_id = _required_workspace_id(source)
    uri = _relative_uri(Path(source.path), Path(workspace_root))
    return MemorySource(
        id=source.id,
        workspace_id=workspace_id,
        type=_source_type(source.file_type),
        uri=uri,
        title=Path(uri).name or uri,
        freshness=MemorySourceFreshness.FRESH,
        status=_source_status(source.status),
        checksum=source.hash,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def chunk_to_memory_segment(
    chunk: Chunk,
    *,
    source: Source,
    workspace_root: Path | str,
) -> MemorySegment:
    """Return a graph segment for an ingestion chunk."""

    workspace_id = _required_workspace_id(source)
    _relative_uri(Path(source.path), Path(workspace_root))
    return MemorySegment(
        id=chunk.id,
        source_id=source.id,
        workspace_id=workspace_id,
        kind=MemorySegmentKind.TEXT,
        label=f"Chunk {chunk.chunk_index + 1}",
        span={"start_offset": chunk.start_offset, "end_offset": chunk.end_offset},
        text_preview=chunk.content,
        checksum=chunk.content_hash,
        parser=source.file_type.value,
        parser_settings_ref="local-ingestion-v1",
        created_at=source.created_at,
    )


def write_ingestion_records_to_graph(
    *,
    store: MemoryGraphStore,
    source: Source,
    chunks: list[Chunk],
    workspace_root: Path | str,
) -> IngestionGraphWriteResult:
    """Persist graph source and segment records derived from ingestion records."""

    graph_source = source_to_memory_source(source, workspace_root=workspace_root)
    store.save_source(graph_source)
    for chunk in chunks:
        store.save_segment(
            chunk_to_memory_segment(chunk, source=source, workspace_root=workspace_root)
        )
    return IngestionGraphWriteResult(source_id=source.id, segments_created=len(chunks))


def _required_workspace_id(source: Source) -> str:
    workspace_id: str | None = source.workspace_id
    if workspace_id is None or not workspace_id.strip():
        raise IngestionGraphAdapterError("source workspace_id is required")
    return workspace_id


def _relative_uri(path: Path, workspace_root: Path) -> str:
    root = workspace_root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise IngestionGraphAdapterError("source path must stay inside workspace root") from exc
    uri = relative.as_posix()
    if not uri or uri.startswith("../") or uri == "..":
        raise IngestionGraphAdapterError("source path must produce a safe relative URI")
    return uri


def _source_type(file_type: FileType) -> MemorySourceType:
    if file_type in {FileType.MARKDOWN, FileType.TEXT}:
        return MemorySourceType.FILE
    return MemorySourceType.UNKNOWN


def _source_status(status: ParseStatus) -> MemorySourceStatus:
    if status in {ParseStatus.SUCCESS, ParseStatus.PARSED}:
        return MemorySourceStatus.READY
    if status == ParseStatus.FAILED:
        return MemorySourceStatus.FAILED
    return MemorySourceStatus.PENDING
