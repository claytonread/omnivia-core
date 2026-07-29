"""Canonical portable contract surface for ingestion chunk and extraction models."""

from __future__ import annotations

from omnivia_core.ingestion.models import (
    Chunk,
    ExtractionResult,
    FileType,
    ParseStatus,
    Source,
)

__all__ = [
    "Chunk",
    "ExtractionResult",
    "FileType",
    "ParseStatus",
    "Source",
]
