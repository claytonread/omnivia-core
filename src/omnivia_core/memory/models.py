"""Memory domain model.

A memory is a unit of knowledge with content, provenance, and lifecycle state.
Memories are the primary storage unit in OmniVia's Phase 1 memory core.
"""

# ruff: noqa: UP017
# UP017 would rewrite `from datetime import timezone` / `datetime.now(timezone.utc)`
# to `from datetime import UTC` / `datetime.now(UTC)`. This module is a 1:1 port of
# `omnivia_memory.memory.models` and tests/canonical_migration/test_parity.py gates it
# on two things that rewrite would break:
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
from typing import Any

from omnivia_core.lifecycle.models import LifecycleState
from omnivia_core.lifecycle.rules import CreatedBy
from omnivia_core.provenance.models import Source


@dataclass
class Memory:
    """A unit of knowledge in OmniVia's memory store.

    Memories track project context, decisions, patterns, and facts
    with full provenance and lifecycle governance.

    Attributes:
        id: Unique identifier for this memory
        content: The knowledge content stored in this memory
        source: Provenance reference showing where this knowledge came from
        lifecycle_state: Current approval state (proposed/observed/approved/rejected)
        memory_type: Category of knowledge (general, decision, pattern, constraint)
        created_by: Whether a human or agent created this memory
        created_at: When this memory was created (ISO 8601 timestamp)
        updated_at: When this memory was last updated (ISO 8601 timestamp)
    """

    content: str
    source: Source
    created_by: CreatedBy
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.PROPOSED
    memory_type: str = "general"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert memory to dictionary for serialization.

        Returns:
            Dictionary representation of the memory
        """
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "content": self.content,
            "source": self.source.to_dict(),
            "lifecycle_state": self.lifecycle_state.value,
            "memory_type": self.memory_type,
            "created_by": self.created_by.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Memory:
        """Create a memory from a dictionary.

        Args:
            data: Dictionary with memory fields

        Returns:
            Memory instance
        """
        return cls(
            id=data["id"],
            workspace_id=data.get("workspace_id"),
            content=data["content"],
            source=Source.from_dict(data["source"]),
            lifecycle_state=LifecycleState(data["lifecycle_state"]),
            memory_type=data.get("memory_type", "general"),
            created_by=CreatedBy(data["created_by"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def update_content(self, new_content: str) -> None:
        """Update the memory content.

        Args:
            new_content: The new content to store
        """
        self.content = new_content
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def transition_to(self, new_state: LifecycleState) -> None:
        """Transition the memory to a new lifecycle state.

        This does NOT validate the transition - use LifecycleRules.can_transition
        before calling this method.

        Args:
            new_state: The target state to transition to
        """
        self.lifecycle_state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def touch(self) -> None:
        """Update the updated_at timestamp without changing content."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Memory):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return (
            f"Memory(id={self.id[:8]}..., state={self.lifecycle_state.value}, "
            f"type={self.memory_type})"
        )


@dataclass
class MemoryCreate:
    """Input model for creating a new memory.

    This is the user-facing input that gets converted to a Memory entity.

    Attributes:
        content: The knowledge content to store
        source: Required provenance reference
        memory_type: Optional category (defaults to "general")
        created_by: Whether human or agent (defaults to agent for AI use)
    """

    content: str
    source: Source
    memory_type: str = "general"
    created_by: CreatedBy = CreatedBy.AGENT
    workspace_id: str | None = None

    def to_memory(self) -> Memory:
        """Convert this input to a Memory entity.

        Uses LifecycleRules to determine the correct initial state based
        on whether a human or agent created the memory.

        Returns:
            A new Memory instance with appropriate initial lifecycle state
        """
        from omnivia_core.lifecycle.rules import LifecycleRules

        initial_state = LifecycleRules.get_initial_state(self.created_by)
        return Memory(
            content=self.content,
            source=self.source,
            memory_type=self.memory_type,
            created_by=self.created_by,
            workspace_id=self.workspace_id,
            lifecycle_state=initial_state,
        )


@dataclass
class MemoryUpdate:
    """Input model for updating an existing memory.

    All fields are optional - only provided fields are updated.

    Attributes:
        content: New content (optional)
        memory_type: New type (optional)
        lifecycle_state: New state (optional)
    """

    content: str | None = None
    memory_type: str | None = None
    lifecycle_state: LifecycleState | None = None

    def apply_to(self, memory: Memory) -> bool:
        """Apply this update to a memory.

        Args:
            memory: The memory to update

        Returns:
            True if any field was changed, False if nothing changed
        """
        changed = False

        if self.content is not None and self.content != memory.content:
            memory.content = self.content
            changed = True

        if self.memory_type is not None and self.memory_type != memory.memory_type:
            memory.memory_type = self.memory_type
            changed = True

        if self.lifecycle_state is not None and self.lifecycle_state != memory.lifecycle_state:
            memory.lifecycle_state = self.lifecycle_state
            changed = True

        if changed:
            memory.touch()

        return changed
