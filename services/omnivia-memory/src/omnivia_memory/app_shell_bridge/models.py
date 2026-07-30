"""Compatibility facade for App Shell bridge contract data models.

Deprecated: import these from ``omnivia_core.app_shell_bridge.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.app_shell_bridge.models import (
    AppShellBodyDescriptor,
    AppShellHostContext,
    AppShellRuntimeState,
    AppShellSource,
    Enum,
    List,
    ValidationResult,
    dataclass,
    field,
)
