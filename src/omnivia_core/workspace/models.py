"""Workspace domain models for OmniVia Local."""

# ruff: noqa: UP017
# UP017 would rewrite `from datetime import timezone` / `datetime.now(timezone.utc)`
# to `from datetime import UTC` / `datetime.now(UTC)`. This module is a 1:1 port of
# `omnivia_memory.workspace.models` and tests/canonical_migration/test_parity.py gates
# it on two things that rewrite would break:
#   - module-scope *namespace* parity: `timezone` would disappear from, and `UTC`
#     appear in, the set of public names this module binds.
#   - normalized *AST* parity: `ast.dump` of this module must equal `ast.dump` of the
#     legacy module with only `omnivia_memory` -> `omnivia_core` rewritten, so the
#     `Attribute(value=Name('timezone'), attr='utc')` nodes must survive verbatim.
# Comments are the only thing those gates cannot see, so the suppression is the fix;
# modernizing the spelling has to land in the legacy source first.

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class WorkspaceIndexStatus(str, Enum):
    """Indexing state for a local workspace."""

    UNINDEXED = "unindexed"
    INDEXING = "indexing"
    INDEXED = "indexed"
    ERROR = "error"
    STALE = "stale"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Workspace:
    """A local product boundary for sources, memories, and graph knowledge."""

    name: str
    root_path: str
    storage_path: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str | None = None
    index_status: WorkspaceIndexStatus = WorkspaceIndexStatus.UNINDEXED
    settings: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_indexed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert workspace to a serializable dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "root_path": self.root_path,
            "storage_path": self.storage_path,
            "description": self.description,
            "index_status": self.index_status.value,
            "settings": self.settings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_indexed_at": self.last_indexed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workspace:
        """Create workspace from a dictionary."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data["name"],
            root_path=data["root_path"],
            storage_path=data["storage_path"],
            description=data.get("description"),
            index_status=WorkspaceIndexStatus(data.get("index_status", "unindexed")),
            settings=data.get("settings", {}),
            created_at=data.get("created_at", _now()),
            updated_at=data.get("updated_at", _now()),
            last_indexed_at=data.get("last_indexed_at"),
        )

    def touch(self) -> None:
        """Update the updated timestamp."""
        self.updated_at = _now()

    def mark_indexed(self) -> None:
        """Mark workspace indexing as successful."""
        now = _now()
        self.index_status = WorkspaceIndexStatus.INDEXED
        self.last_indexed_at = now
        self.updated_at = now

    def mark_error(self) -> None:
        """Mark workspace indexing as failed."""
        self.index_status = WorkspaceIndexStatus.ERROR
        self.touch()


@dataclass
class WorkspaceCreate:
    """Input for creating a workspace."""

    name: str
    root_path: Path
    storage_path: Path | None = None
    description: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    def to_workspace(self) -> Workspace:
        """Convert input into a workspace entity."""
        root_path = self.root_path.expanduser().resolve()
        workspace_id = str(uuid.uuid4())
        storage_path = (
            self.storage_path.expanduser().resolve()
            if self.storage_path is not None
            else Path.home() / ".omnivia" / "workspaces" / workspace_id
        )
        return Workspace(
            id=workspace_id,
            name=self.name,
            root_path=str(root_path),
            storage_path=str(storage_path),
            description=self.description,
            settings=self.settings,
        )


@dataclass
class WorkspaceUpdate:
    """Input for updating workspace metadata."""

    name: str | None = None
    root_path: Path | None = None
    storage_path: Path | None = None
    description: str | None = None
    index_status: WorkspaceIndexStatus | None = None
    settings: dict[str, Any] | None = None

    def apply_to(self, workspace: Workspace) -> bool:
        """Apply changed fields to a workspace."""
        changed = False
        if self.name is not None and self.name != workspace.name:
            workspace.name = self.name
            changed = True
        if self.root_path is not None:
            root_path = str(self.root_path.expanduser().resolve())
            if root_path != workspace.root_path:
                workspace.root_path = root_path
                changed = True
        if self.storage_path is not None:
            storage_path = str(self.storage_path.expanduser().resolve())
            if storage_path != workspace.storage_path:
                workspace.storage_path = storage_path
                changed = True
        if self.description is not None and self.description != workspace.description:
            workspace.description = self.description
            changed = True
        if self.index_status is not None and self.index_status != workspace.index_status:
            workspace.index_status = self.index_status
            changed = True
        if self.settings is not None and self.settings != workspace.settings:
            workspace.settings = self.settings
            changed = True
        if changed:
            workspace.touch()
        return changed


@dataclass
class ImportSummary:
    """Summary of a workspace import run."""

    workspace_id: str
    files_seen: int
    sources_created: int
    memories_created: int
    errors: list[str] = field(default_factory=list)
