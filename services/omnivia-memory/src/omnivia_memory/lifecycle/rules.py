"""Compatibility facade for memory lifecycle transition rules.

Deprecated: import ``CreatedBy`` / ``LifecycleRules`` from
``omnivia_core.lifecycle.rules`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and sibling-imported bindings (`Enum`, `annotations`, and `LifecycleState`,
# which the canonical rules module imports from its sibling `models`) -- names
# this leaf's historical namespace still has to resolve. No other code is
# suppressed.
from omnivia_core.lifecycle.rules import (  # type: ignore[attr-defined]
    CreatedBy,
    Enum,
    LifecycleRules,
    LifecycleState,
    annotations,
)
