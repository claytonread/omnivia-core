"""App Shell host/body bridge contract for OmniVia Core."""

# ruff: noqa: RUF022
# The legacy bridge barrel's historical export order is compatibility surface.

from omnivia_core.app_shell_bridge.models import (
    AppShellBodyDescriptor,
    AppShellHostContext,
    AppShellRuntimeState,
    AppShellSource,
    ValidationResult,
)
from omnivia_core.app_shell_bridge.validation import (
    AppShellBridgeValidationError,
    validate_app_shell_body_descriptor,
    validate_app_shell_host_context,
)

__all__ = [
    "AppShellRuntimeState",
    "AppShellSource",
    "ValidationResult",
    "AppShellHostContext",
    "AppShellBodyDescriptor",
    "AppShellBridgeValidationError",
    "validate_app_shell_host_context",
    "validate_app_shell_body_descriptor",
]
