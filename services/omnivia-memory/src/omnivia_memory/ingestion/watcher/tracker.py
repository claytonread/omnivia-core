"""Source tracking for file watcher integration.

Maintains a mapping between watched paths and ingested sources,
enabling efficient change detection and stale source cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


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


class SourceTracker:
    """Tracks the relationship between watched paths and ingested sources.

    This in-memory tracker provides fast lookups for change detection.
    For persistence, platforms should implement a corresponding repository.
    """

    def __init__(self) -> None:
        self._by_source_path: dict[tuple[str, str], SourceReference] = {}
        self._by_watched_path: dict[str, list[SourceReference]] = {}

    def register(self, watched_path: str, source_reference: SourceReference) -> None:
        """Register a source reference.

        Args:
            watched_path: The root watched directory path.
            source_reference: The source reference to register.
        """
        key = (source_reference.workspace_id, source_reference.source_path)
        self._by_source_path[key] = source_reference

        if watched_path not in self._by_watched_path:
            self._by_watched_path[watched_path] = []
        self._by_watched_path[watched_path].append(source_reference)

    def unregister(self, source_path: str, workspace_id: str) -> bool:
        """Unregister a source reference.

        Args:
            source_path: The source file path.
            workspace_id: The workspace ID.

        Returns:
            True if the reference was found and removed.
        """
        key = (workspace_id, source_path)
        ref = self._by_source_path.pop(key, None)
        if ref:
            if ref.watched_path in self._by_watched_path:
                self._by_watched_path[ref.watched_path] = [
                    r for r in self._by_watched_path[ref.watched_path] if r.source_path != source_path
                ]
            return True
        return False

    def get_reference(self, source_path: str, workspace_id: str) -> SourceReference | None:
        """Get the source reference for a path.

        Args:
            source_path: The source file path.
            workspace_id: The workspace ID.

        Returns:
            The source reference, or None if not found.
        """
        key = (workspace_id, source_path)
        return self._by_source_path.get(key)

    def get_stale_references(
        self, workspace_id: str, before_timestamp: str | None = None
    ) -> list[SourceReference]:
        """Get references that may be stale.

        Args:
            workspace_id: The workspace ID.
            before_timestamp: Only references indexed before this timestamp.

        Returns:
            List of potentially stale references.
        """
        stale = []
        for ref in self._by_source_path.values():
            if ref.workspace_id == workspace_id:
                if before_timestamp and ref.indexed_at > before_timestamp:
                    continue
                stale.append(ref)
        return stale

    def update_hash(self, source_path: str, workspace_id: str, new_hash: str) -> bool:
        """Update the hash for a source reference.

        Args:
            source_path: The source file path.
            workspace_id: The workspace ID.
            new_hash: The new content hash.

        Returns:
            True if the reference was found and updated.
        """
        ref = self.get_reference(source_path, workspace_id)
        if ref:
            ref.last_known_hash = new_hash
            ref.indexed_at = datetime.now(timezone.utc).isoformat()
            return True
        return False

    def list_by_watched_path(self, watched_path: str) -> list[SourceReference]:
        """List all source references under a watched path.

        Args:
            watched_path: The root watched directory path.

        Returns:
            List of source references.
        """
        return list(self._by_watched_path.get(watched_path, []))

    def list_by_workspace(self, workspace_id: str) -> list[SourceReference]:
        """List all source references for a workspace.

        Args:
            workspace_id: The workspace ID.

        Returns:
            List of source references.
        """
        return [
            ref
            for ref in self._by_source_path.values()
            if ref.workspace_id == workspace_id
        ]

    def clear(self, workspace_id: str | None = None) -> None:
        """Clear tracked references.

        Args:
            workspace_id: Specific workspace to clear, or None for all.
        """
        if workspace_id is None:
            self._by_source_path.clear()
            self._by_watched_path.clear()
        else:
            to_remove = [
                key for key, ref in self._by_source_path.items()
                if ref.workspace_id == workspace_id
            ]
            for key in to_remove:
                ref = self._by_source_path.pop(key)
                if ref.watched_path in self._by_watched_path:
                    self._by_watched_path[ref.watched_path] = [
                        r for r in self._by_watched_path[ref.watched_path]
                        if r.source_path != ref.source_path
                    ]

    def count(self, workspace_id: str | None = None) -> int:
        """Count tracked references.

        Args:
            workspace_id: Specific workspace to count, or None for total.

        Returns:
            Number of tracked references.
        """
        if workspace_id is None:
            return len(self._by_source_path)
        return sum(
            1 for ref in self._by_source_path.values()
            if ref.workspace_id == workspace_id
        )