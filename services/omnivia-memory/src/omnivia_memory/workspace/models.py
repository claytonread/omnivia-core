"""Compatibility facade for the workspace domain models.

Deprecated: import these from ``omnivia_core.workspace.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# The unconverted workspace runtime (`repository`, `service`) takes its models
# from here, the hybrid `workspace` barrel re-exports all five of them, and
# `baseline/scenarios.py` reaches `WorkspaceCreate` through this path, so every
# name has to stay explicitly exported.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve (`Any`, `Enum`,
# `Path`, `annotations`, `dataclass`, `datetime`, `field`, `timezone`, and the
# plain `uuid` module binding). No other error code is suppressed.
from omnivia_core.workspace.models import (  # type: ignore[attr-defined]
    Any,
    Enum,
    ImportSummary,
    Path,
    Workspace,
    WorkspaceCreate,
    WorkspaceIndexStatus,
    WorkspaceUpdate,
    annotations,
    dataclass,
    datetime,
    field,
    timezone,
    uuid,
)
