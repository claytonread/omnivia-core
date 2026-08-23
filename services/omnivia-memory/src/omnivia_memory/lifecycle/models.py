"""Compatibility facade for memory lifecycle state.

Deprecated: import ``LifecycleState`` from ``omnivia_core.lifecycle.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`Enum`, `annotations`) -- names the canonical module's own imports
# left at its module scope and that this leaf's historical namespace still has
# to resolve. No other error code is suppressed.
from omnivia_core.lifecycle.models import (  # type: ignore[attr-defined,unused-ignore]
    Enum,
    LifecycleState,
    annotations,
)
