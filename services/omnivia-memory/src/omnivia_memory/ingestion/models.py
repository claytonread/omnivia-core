"""Compatibility facade for the ingestion domain models.

Deprecated: import these from ``omnivia_core.ingestion.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# The unconverted ingestion runtime (`chunker`, `extractors`, `pipeline`,
# `repositories`, `scanner`) and `memory_graph.ingestion_adapter` all take their
# models from here, and the hybrid `ingestion` barrel re-exports five of them, so
# every name has to stay explicitly exported.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve (`Any`, `Path`,
# `TYPE_CHECKING`, `annotations`, `dataclass`, `datetime`, `field`, `timezone`,
# and the plain `enum`, `hashlib` and `uuid` module bindings). No other error
# code is suppressed.
from omnivia_core.ingestion.models import (  # type: ignore[attr-defined]
    TYPE_CHECKING,
    Any,
    Chunk,
    ExtractionResult,
    FileInventory,
    FileType,
    IngestSource,
    ParseStatus,
    Path,
    Source,
    annotations,
    dataclass,
    datetime,
    enum,
    field,
    hashlib,
    timezone,
    uuid,
)
