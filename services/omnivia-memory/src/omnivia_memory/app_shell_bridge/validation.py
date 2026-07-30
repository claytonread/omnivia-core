"""Compatibility facade for App Shell bridge contract validation.

Deprecated: import ``AppShellBridgeValidationError`` /
``validate_app_shell_host_context`` / ``validate_app_shell_body_descriptor``
from ``omnivia_core.app_shell_bridge.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.app_shell_bridge.validation import (
    TYPE_CHECKING,
    Any,
    AppShellBridgeValidationError,
    Dict,
    List,
    validate_app_shell_body_descriptor,
    validate_app_shell_host_context,
)
