"""Compatibility facade for the memory domain model.

Deprecated: import ``Memory`` / ``MemoryCreate`` / ``MemoryUpdate`` from
``omnivia_core.memory.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.memory.models import (
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
