"""Compatibility facade for App Shell bridge contract validation.

Deprecated: import ``AppShellBridgeValidationError`` /
``validate_app_shell_host_context`` / ``validate_app_shell_body_descriptor``
from ``omnivia_core.app_shell_bridge.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`TYPE_CHECKING`, `Any`, `Dict`, `List`) -- names the canonical
# module's own imports left at its module scope and that this leaf's historical
# namespace still has to resolve. No other error code is suppressed.
from omnivia_core.app_shell_bridge.validation import (  # type: ignore[attr-defined]
    TYPE_CHECKING,
    Any,
    AppShellBridgeValidationError,
    Dict,
    List,
    validate_app_shell_body_descriptor,
    validate_app_shell_host_context,
)
