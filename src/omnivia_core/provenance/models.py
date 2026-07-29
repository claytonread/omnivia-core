"""Source/provenance types for tracking where knowledge comes from.

Every memory in OmniVia tracks its source so AI agents can cite evidence
rather than presenting unverified observations as fact.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class SourceType(str, Enum):
    """The kind of source providing provenance for a memory.

    This allows memories to be traced back to their origin:
    - file: A source file in the project (code, config, docs)
    - url: A web resource
    - adr: An architecture decision record in the repo
    - human: An explicit human assertion or direct knowledge
    """

    FILE = "file"
    URL = "url"
    ADR = "adr"
    HUMAN = "human"


class Source:
    """Provenance reference for a memory.

    Tracks where knowledge came from, enabling source-backed recall
    and citation. AI agents should cite sources when making claims.

    Attributes:
        type: The kind of source (file, url, adr, human)
        reference: The actual reference (file path, URL, or description)
        description: Optional human-readable description of the source
    """

    def __init__(
        self,
        type: SourceType,
        reference: str,
        description: str | None = None,
    ) -> None:
        self.type = type
        self.reference = reference
        self.description = description

    def to_dict(self) -> dict[str, Any]:
        """Convert source to dictionary for serialization."""
        return {
            "type": self.type.value,
            "reference": self.reference,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        """Create source from dictionary."""
        return cls(
            type=SourceType(data["type"]),
            reference=data["reference"],
            description=data.get("description"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Source):
            return NotImplemented
        return (
            self.type == other.type
            and self.reference == other.reference
            and self.description == other.description
        )

    def __repr__(self) -> str:
        return f"Source(type={self.type.value!r}, reference={self.reference!r})"
