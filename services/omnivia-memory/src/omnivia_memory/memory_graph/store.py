"""Durable local read storage for memory graph records.

A path-contained JSON store that mirrors the Platform run-ledger persistence
pattern: atomic sibling-temp writes, restrictive permissions and safe file
names. The store keeps Lane 1 read-only - it saves and reads back source,
segment, entity and fact records so Platform read APIs can return live responses
instead of relying only on static fixtures.

The store is deliberately backend-neutral and injectable: callers pass a root
directory (typically a test root), so durable writes stay path-contained and no
absolute UI-facing paths or credentials are persisted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from omnivia_memory.memory_graph.models import (
    MemoryEntity,
    MemoryFact,
    MemorySegment,
    MemorySource,
)
from omnivia_memory.memory_graph.validation import (
    ValidationResult,
    validate_memory_entity,
    validate_memory_fact,
    validate_memory_segment,
    validate_memory_source,
)

_SOURCES = "sources"
_SEGMENTS = "segments"
_ENTITIES = "entities"
_FACTS = "facts"

_SUBDIRS = (_SOURCES, _SEGMENTS, _ENTITIES, _FACTS)

T = TypeVar("T")


class MemoryGraphStoreError(ValueError):
    """Raised when a record cannot be safely persisted."""


class MemoryGraphStore:
    """Filesystem-backed durable store for memory graph records."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        for subdir in _SUBDIRS:
            (self.root / subdir).mkdir(parents=True, exist_ok=True)

    # -- writes ----------------------------------------------------------------

    def save_source(self, source: MemorySource) -> None:
        """Persist a source after rejecting unsafe content."""

        self._guard(validate_memory_source(source), "source", source.id)
        _reject_absolute_uri(source.uri)
        self._write(_SOURCES, source.id, source.to_dict())

    def save_segment(self, segment: MemorySegment) -> None:
        """Persist a segment after rejecting unsafe content."""

        self._guard(validate_memory_segment(segment), "segment", segment.id)
        self._write(_SEGMENTS, segment.id, segment.to_dict())

    def save_entity(self, entity: MemoryEntity) -> None:
        """Persist an entity after rejecting unsafe content."""

        self._guard(validate_memory_entity(entity), "entity", entity.id)
        self._write(_ENTITIES, entity.id, entity.to_dict())

    def save_fact(self, fact: MemoryFact) -> None:
        """Persist a fact after rejecting unsafe content.

        Facts with no source refs are allowed (missing evidence is a warning,
        not an error) so consumers can surface an explicit missing-evidence
        warning rather than silently dropping the fact.
        """

        self._guard(validate_memory_fact(fact), "fact", fact.id)
        self._write(_FACTS, fact.id, fact.to_dict())

    # -- reads -----------------------------------------------------------------

    def get_source(self, workspace_id: str, source_id: str) -> MemorySource | None:
        source = self._read(_SOURCES, source_id, MemorySource.from_dict)
        if source is None or source.workspace_id != workspace_id:
            return None
        return source

    def list_sources(self, workspace_id: str) -> list[MemorySource]:
        return [
            source
            for source in self._read_all(_SOURCES, MemorySource.from_dict)
            if source.workspace_id == workspace_id
        ]

    def list_segments(
        self,
        workspace_id: str,
        *,
        source_id: str | None = None,
    ) -> list[MemorySegment]:
        segments = [
            segment
            for segment in self._read_all(_SEGMENTS, MemorySegment.from_dict)
            if segment.workspace_id == workspace_id
        ]
        if source_id is not None:
            segments = [segment for segment in segments if segment.source_id == source_id]
        return segments

    def list_entities(self, workspace_id: str) -> list[MemoryEntity]:
        return [
            entity
            for entity in self._read_all(_ENTITIES, MemoryEntity.from_dict)
            if entity.workspace_id == workspace_id
        ]

    def list_facts(
        self,
        workspace_id: str,
        *,
        source_id: str | None = None,
    ) -> list[MemoryFact]:
        facts = [
            fact
            for fact in self._read_all(_FACTS, MemoryFact.from_dict)
            if fact.workspace_id == workspace_id
        ]
        if source_id is not None:
            facts = [
                fact
                for fact in facts
                if any(ref.source_id == source_id for ref in fact.source_refs)
            ]
        return facts

    def has_workspace(self, workspace_id: str) -> bool:
        """Return True when any source exists for the workspace."""

        return bool(self.list_sources(workspace_id))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _guard(result: ValidationResult, kind: str, record_id: str) -> None:
        if not result.valid:
            raise MemoryGraphStoreError(
                f"cannot persist {kind} {record_id!r}: {'; '.join(result.errors)}"
            )

    def _write(self, subdir: str, record_id: str, payload: dict[str, Any]) -> None:
        path = self._record_path(subdir, record_id)
        body = json.dumps(payload, sort_keys=True, indent=2)
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(body + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)

    def _read(
        self,
        subdir: str,
        record_id: str,
        builder: Callable[[dict[str, Any]], T],
    ) -> T | None:
        path = self._record_path(subdir, record_id)
        if not path.exists():
            return None
        return builder(json.loads(path.read_text(encoding="utf-8")))

    def _read_all(
        self,
        subdir: str,
        builder: Callable[[dict[str, Any]], T],
    ) -> list[T]:
        records = []
        for path in sorted((self.root / subdir).glob("*.json")):
            records.append(builder(json.loads(path.read_text(encoding="utf-8"))))
        return records

    def _record_path(self, subdir: str, record_id: str) -> Path:
        if not record_id or "/" in record_id or "\\" in record_id:
            raise MemoryGraphStoreError("record id must be a safe file name")
        return self.root / subdir / f"{record_id}.json"


def _reject_absolute_uri(uri: str) -> None:
    """Reject unsafe local paths so UI-facing source URIs stay contained."""

    if uri.startswith("/") or uri.startswith("\\"):
        raise MemoryGraphStoreError("source uri must not be an absolute local path")
    # Windows drive prefixes such as ``C:\`` or ``C:/``.
    if len(uri) >= 3 and uri[1] == ":" and uri[2] in {"/", "\\"}:
        raise MemoryGraphStoreError("source uri must not be an absolute local path")
    if any(part == ".." for part in uri.replace("\\", "/").split("/")):
        raise MemoryGraphStoreError("source uri must not contain parent traversal")
