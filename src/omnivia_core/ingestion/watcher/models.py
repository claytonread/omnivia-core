"""Models for the file watcher / indexer subsystem.

Provides data structures for watched paths, file change events,
debounce configuration, and indexer status tracking.
"""

# ruff: noqa: UP017, TC005
# This module is a 1:1 port of `omnivia_memory.ingestion.watcher.models`, and
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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class FileChangeType(enum.Enum):
    """Type of file system change event."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


class IndexerState(enum.Enum):
    """Current state of the indexer."""

    IDLE = "idle"
    SCANNING = "scanning"
    WATCHING = "watching"
    DEBOUNCING = "debouncing"
    INDEXING = "indexing"
    ERROR = "error"


@dataclass
class FileChange:
    """A single file system change event."""

    path: str
    event_type: FileChangeType
    old_path: str | None = None  # For MOVED events
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "path": self.path,
            "event_type": self.event_type.value,
            "old_path": self.old_path,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FileChange:  # type: ignore[type-arg]
        return cls(
            path=data["path"],
            event_type=FileChangeType(data["event_type"]),
            old_path=data.get("old_path"),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class FileChangeBatch:
    """A batch of file changes collected during debounce window."""

    changes: list[FileChange]
    debounce_key: str  # e.g., workspace_id for grouping
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __len__(self) -> int:
        return len(self.changes)


@dataclass
class DebounceConfig:
    """Configuration for debounce behavior."""

    initial_delay_ms: int = 500
    max_delay_ms: int = 2000
    min_events: int = 3

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "initial_delay_ms": self.initial_delay_ms,
            "max_delay_ms": self.max_delay_ms,
            "min_events": self.min_events,
        }


@dataclass
class WatchedPath:
    """A path being watched for changes."""

    path: str
    workspace_id: str
    recursive: bool = True
    ignore_patterns: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "path": self.path,
            "workspace_id": self.workspace_id,
            "recursive": self.recursive,
            "ignore_patterns": self.ignore_patterns,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WatchedPath:  # type: ignore[type-arg]
        return cls(
            path=data["path"],
            workspace_id=data["workspace_id"],
            recursive=data.get("recursive", True),
            ignore_patterns=data.get("ignore_patterns", []),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class SourceReference:
    """Maps a watched path to an ingested source."""

    watched_path: str
    source_path: str
    source_id: str
    workspace_id: str
    last_known_hash: str | None = None
    indexed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_stale(self, current_hash: str | None) -> bool:
        """Check if this reference is stale based on hash."""
        if self.last_known_hash is None:
            return True
        if current_hash is None:
            return True
        return self.last_known_hash != current_hash

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "watched_path": self.watched_path,
            "source_path": self.source_path,
            "source_id": self.source_id,
            "workspace_id": self.workspace_id,
            "last_known_hash": self.last_known_hash,
            "indexed_at": self.indexed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SourceReference:  # type: ignore[type-arg]
        return cls(
            watched_path=data["watched_path"],
            source_path=data["source_path"],
            source_id=data["source_id"],
            workspace_id=data["workspace_id"],
            last_known_hash=data.get("last_known_hash"),
            indexed_at=data.get("indexed_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class IndexerStatus:
    """Current status of the indexer for a workspace."""

    state: IndexerState
    workspace_id: str
    active_watched_paths: list[str] = field(default_factory=list)
    pending_changes: int = 0
    last_index_at: str | None = None
    last_error: str | None = None
    indexed_count: int = 0
    deleted_count: int = 0

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "state": self.state.value,
            "workspace_id": self.workspace_id,
            "active_watched_paths": self.active_watched_paths,
            "pending_changes": self.pending_changes,
            "last_index_at": self.last_index_at,
            "last_error": self.last_error,
            "indexed_count": self.indexed_count,
            "deleted_count": self.deleted_count,
        }


@dataclass
class ScheduledJob:
    """A scheduled indexing job."""

    job_id: str
    job_type: str  # "reindex" or "full_scan"
    workspace_id: str
    scheduled_at: str
    delay_seconds: float = 0

    @classmethod
    def create(cls, job_type: str, workspace_id: str, delay_seconds: float = 0) -> ScheduledJob:
        return cls(
            job_id=str(uuid.uuid4()),
            job_type=job_type,
            workspace_id=workspace_id,
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            delay_seconds=delay_seconds,
        )


class IndexerScheduler:
    """Abstract scheduler interface for indexer operations.

    Platforms provide an implementation via their background task system.
    """

    def schedule_reindex(self, workspace_id: str, delay_seconds: float = 0) -> str:
        """Schedule a reindex operation. Returns job_id."""
        raise NotImplementedError

    def schedule_full_scan(self, workspace_id: str, at_timestamp: str | None = None) -> str:
        """Schedule a full directory scan."""
        raise NotImplementedError

    def cancel(self, job_id: str) -> bool:
        """Cancel a scheduled job."""
        raise NotImplementedError

    def list_pending(self) -> list[ScheduledJob]:
        """List all pending scheduled jobs."""
        raise NotImplementedError
