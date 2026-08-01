"""Compatibility facade for the knowledge graph domain models.

Deprecated: import these from ``omnivia_core.graph.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# The unconverted graph runtime (`repository`, `service`, `search_service`) and
# the hybrid `graph` barrel all take their models from here, so every name has to
# stay explicitly exported.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve (`Any`, `Enum`,
# `annotations`, `dataclass`, `datetime`, `field`, `timezone`, and the plain
# `uuid` module binding). No other error code is suppressed.
from omnivia_core.graph.models import (  # type: ignore[attr-defined]
    Any,
    ApprovalStatus,
    Entity,
    EntityCreate,
    EntityMemoryLink,
    EntityType,
    Enum,
    Relationship,
    RelationshipCreate,
    RelationshipType,
    annotations,
    dataclass,
    datetime,
    field,
    timezone,
    uuid,
)
