"""Compatibility facade for the memory domain model.

Deprecated: import ``Memory`` / ``MemoryCreate`` / ``MemoryUpdate`` from
``omnivia_core.memory.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`Any`, `annotations`, `dataclass`,
# `datetime`, `field`, `timezone`, `uuid`, and `CreatedBy` / `LifecycleState` /
# `Source`, which the canonical memory model imports from the lifecycle and
# provenance leaves) -- names this leaf's historical namespace still has to
# resolve. No other error code is suppressed.
from omnivia_core.memory.models import (  # type: ignore[attr-defined,unused-ignore]
    Any,
    CreatedBy,
    LifecycleState,
    Memory,
    MemoryCreate,
    MemoryUpdate,
    Source,
    annotations,
    dataclass,
    datetime,
    field,
    timezone,
    uuid,
)
