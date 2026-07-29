"""Compatibility facade for memory lifecycle transition rules.

Deprecated: import ``CreatedBy`` / ``LifecycleRules`` from
``omnivia_core.lifecycle.rules`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.lifecycle.rules import (
    CreatedBy,
    Enum,
    LifecycleRules,
    LifecycleState,
    annotations,
)
