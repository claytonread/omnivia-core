"""Strict-mypy consumer fixture for the ingestion models compatibility facade.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for ``omnivia_memory.ingestion.models``: it imports all seven
routed symbols from that leaf (never from ``omnivia_core``) and proves the facade
re-exports usefully typed objects -- the exact canonical types, not ``Any`` --
via ``typing.assert_type``.

The ingestion domain gets its own fixture rather than joining
``accepted_legacy_facade_consumer.py`` because its barrel stays a *hybrid*: only
five of the leaf's seven routed names are re-exported by
``omnivia_memory.ingestion``, while ``FileInventory`` and ``IngestSource`` stay
leaf-only and the other fourteen barrel exports are runtime-owned. A consumer
that reaches the whole routed surface through the leaf's own path is what proves
that split is invisible to a typed caller. The consumer partition is enforced by
``tests/test_typed_facade_consumers.py``.

``IngestSource`` is the sharpest case here: it is an identity alias for
``Source``, so a facade that re-exported it as a separate class -- or degraded it
to ``Any`` -- would still import cleanly. ``assert_type`` on a value annotated as
one and constructed as the other is what rejects that.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If the facade ever stopped
explicitly re-exporting these names or degraded them to ``Any``, strict mypy
would fail here.
"""

from pathlib import Path
from typing import Any, assert_type

from omnivia_memory.ingestion.models import (
    Chunk,
    ExtractionResult,
    FileInventory,
    FileType,
    IngestSource,
    ParseStatus,
    Source,
)


def build_source() -> Source:
    """Construct an ingested-file record through the facade's own types."""
    source = Source(
        path="/workspace/notes.md",
        file_type=FileType.MARKDOWN,
        workspace_id="workspace-1",
        size=5,
        hash="abc123",
        status=ParseStatus.PENDING,
        modified_time="2026-07-30T00:00:00+00:00",
        error=None,
        id="source-1",
    )
    assert_type(source.path, str)
    assert_type(source.file_type, FileType)
    assert_type(source.workspace_id, str | None)
    assert_type(source.size, int)
    assert_type(source.hash, str | None)
    assert_type(source.status, ParseStatus)
    assert_type(source.modified_time, str | None)
    assert_type(source.error, str | None)
    assert_type(source.id, str)
    assert_type(source.created_at, str)
    assert_type(source.updated_at, str)
    assert_type(source.to_dict(), dict[str, Any])
    assert_type(Source.from_dict(source.to_dict()), Source)
    assert_type(source.touch(), None)
    return source


def build_ingest_source() -> IngestSource:
    """The backwards-compatible alias must still be the same type as ``Source``.

    Annotating the return as ``IngestSource`` while constructing a ``Source`` --
    and asserting the alias resolves to ``Source`` -- is what a facade that split
    them into two classes would fail.
    """
    alias: IngestSource = build_source()
    assert_type(alias, Source)
    assert_type(alias.file_type, FileType)
    return alias


def build_chunk(source: Source) -> Chunk:
    """A content chunk, keyed back to its source record."""
    chunk = Chunk(
        source_id=source.id,
        chunk_index=0,
        content="hello",
        start_offset=0,
        end_offset=5,
        content_hash=None,
        id="chunk-1",
    )
    assert_type(chunk.source_id, str)
    assert_type(chunk.chunk_index, int)
    assert_type(chunk.content, str)
    assert_type(chunk.start_offset, int)
    assert_type(chunk.end_offset, int)
    assert_type(chunk.content_hash, str | None)
    assert_type(chunk.id, str)
    assert_type(chunk.to_dict(), dict[str, Any])
    assert_type(Chunk.from_dict(chunk.to_dict()), Chunk)
    return chunk


def extraction_outcomes() -> tuple[ExtractionResult, ExtractionResult]:
    """Both constructors of the extraction result record."""
    ok = ExtractionResult.success("hello")
    failed = ExtractionResult.failure("boom")
    assert_type(ok, ExtractionResult)
    assert_type(failed, ExtractionResult)
    assert_type(ok.content, str | None)
    assert_type(ok.status, ParseStatus)
    assert_type(ok.error, str | None)
    assert_type(ok.hash, str | None)
    return ok, failed


def inventory_from_path(path: Path) -> FileInventory:
    """The leaf-only discovery record, reached through the facade.

    ``FileInventory`` is never re-exported by the hybrid ``ingestion`` barrel, so
    the leaf's own path is the only typed way to reach it.
    """
    inventory = FileInventory.from_path(path)
    assert_type(inventory, FileInventory)
    assert_type(inventory.path, Path)
    assert_type(inventory.extension, str)
    assert_type(inventory.size, int)
    assert_type(inventory.modified_time, str)
    assert_type(inventory.file_type, FileType)
    assert_type(inventory.parse_status, ParseStatus)
    assert_type(inventory.error_message, str | None)
    assert_type(inventory.to_dict(), dict[str, Any])
    assert_type(inventory.mark_success(), None)
    assert_type(inventory.mark_error("boom"), None)
    return inventory


def enum_members() -> tuple[str, str]:
    """Both enumerations keep their ``value`` accessor and their members."""
    file_type = FileType.UNKNOWN
    status = ParseStatus.PARSED
    assert_type(file_type.value, str)
    assert_type(status.value, str)
    return file_type.value, status.value


def summarize(path: Path) -> int:
    """One end-to-end pass through every routed name."""
    source = build_source()
    build_ingest_source()
    chunk = build_chunk(source)
    ok, failed = extraction_outcomes()
    inventory = inventory_from_path(path)
    enum_members()
    total = chunk.end_offset + inventory.size + len(ok.content or "") + len(
        failed.error or ""
    )
    assert_type(total, int)
    return total
