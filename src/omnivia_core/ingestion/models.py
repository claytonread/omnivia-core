"""Data models for ingestion module.

Defines source records, chunks, and related enumerations for
tracking ingested files and their content segments.
"""

# ruff: noqa: UP017, TC005
# This module is a 1:1 port of `omnivia_memory.ingestion.models`, and
# tests/canonical_migration/test_parity.py gates it on module-scope *namespace*
# parity plus normalized *AST* parity (`ast.dump` of this module must equal
# `ast.dump` of the legacy module with only `omnivia_memory` -> `omnivia_core`
# rewritten). Both autofixes below would break one or both gates:
#   - UP017 rewrites `from datetime import timezone` / `datetime.now(timezone.utc)`
#     to `from datetime import UTC` / `datetime.now(UTC)`: `timezone` would drop out
#     of the published namespace, `UTC` would enter it, and the
#     `Attribute(value=Name('timezone'), attr='utc')` nodes would change shape.
#   - TC005 deletes the empty `if TYPE_CHECKING: pass` block, which is present
#     verbatim in the legacy source; removing it drops an `If` node from the AST.
# Comments are the only thing those gates cannot see, so the suppression is the fix;
# both cleanups have to land in the legacy source first.

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class FileType(enum.Enum):
    """Supported file types for ingestion."""

    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    DOCX = "docx"
    UNKNOWN = "unknown"


class ParseStatus(enum.Enum):
    """Status of file parsing operation."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    PARSED = "parsed"


@dataclass
class FileInventory:
    """A discovered file with its metadata."""

    path: Path
    extension: str
    size: int
    modified_time: str
    file_type: FileType
    parse_status: ParseStatus = ParseStatus.PENDING
    error_message: str | None = None

    @classmethod
    def from_path(cls, file_path: Path) -> FileInventory:
        """Create a FileInventory from a Path object."""
        stat_info = file_path.stat()
        extension = file_path.suffix.lower()
        file_type = cls._detect_file_type(extension)

        return cls(
            path=file_path,
            extension=extension,
            size=stat_info.st_size,
            modified_time=datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat(),
            file_type=file_type,
        )

    @staticmethod
    def _detect_file_type(extension: str) -> FileType:
        """Detect the file type from its extension."""
        type_map = {
            ".md": FileType.MARKDOWN,
            ".markdown": FileType.MARKDOWN,
            ".txt": FileType.TEXT,
            ".pdf": FileType.PDF,
            ".docx": FileType.DOCX,
        }
        return type_map.get(extension, FileType.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "path": str(self.path),
            "extension": self.extension,
            "size": self.size,
            "modified_time": self.modified_time,
            "file_type": self.file_type.value,
            "parse_status": self.parse_status.value,
            "error_message": self.error_message,
        }

    def mark_success(self) -> None:
        """Mark this file as successfully parsed."""
        self.parse_status = ParseStatus.SUCCESS
        self.error_message = None

    def mark_error(self, message: str) -> None:
        """Mark this file as having a parse error."""
        self.parse_status = ParseStatus.FAILED
        self.error_message = message


@dataclass
class Source:
    """A source file record for tracking ingested content."""

    path: str
    file_type: FileType
    workspace_id: str | None = None
    size: int = 0
    hash: str | None = None
    status: ParseStatus = ParseStatus.PENDING
    modified_time: str | None = None
    error: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "file_type": self.file_type.value,
            "size": self.size,
            "hash": self.hash,
            "status": self.status.value,
            "modified_time": self.modified_time,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            workspace_id=data.get("workspace_id"),
            path=data["path"],
            file_type=FileType(data.get("file_type", "unknown")),
            size=data.get("size", 0),
            hash=data.get("hash"),
            status=ParseStatus(data.get("status", "pending")),
            modified_time=data.get("modified_time"),
            error=data.get("error"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


@dataclass
class Chunk:
    """A content chunk from an ingested source file."""

    source_id: str
    chunk_index: int
    content: str
    start_offset: int = 0
    end_offset: int = 0
    content_hash: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            source_id=data["source_id"],
            chunk_index=data["chunk_index"],
            content=data["content"],
            start_offset=data.get("start_offset", 0),
            end_offset=data.get("end_offset", 0),
            content_hash=data.get("content_hash"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Chunk):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class ExtractionResult:
    """Result of a content extraction operation."""

    content: str | None
    status: ParseStatus
    error: str | None = None
    hash: str | None = None

    @classmethod
    def success(cls, content: str) -> ExtractionResult:
        return cls(
            content=content,
            status=ParseStatus.SUCCESS,
            hash=hashlib.sha256(content.encode()).hexdigest(),
        )

    @classmethod
    def failure(cls, error: str) -> ExtractionResult:
        return cls(
            content=None,
            status=ParseStatus.FAILED,
            error=error,
        )


# Alias for backwards compatibility
IngestSource = Source
