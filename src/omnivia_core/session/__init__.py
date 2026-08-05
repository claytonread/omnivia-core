"""Canonical session surface: the holder and rotator of trusted-host authority.

Named ``session`` for the *lifecycle* sense of the word -- the authority an
installation is currently acting under, and how it moves. It is unrelated to
``omnivia_core_runtime``'s ``AuthenticatedSession``, which is a per-request
server-side record living in a distribution MCP cannot depend on. Everything
here is standard-library-only and lives under ``src/omnivia_core``.
"""

from __future__ import annotations

from omnivia_core.session.context import (
    ContextIssuer,
    ContextIssuerError,
    new_context_id,
)

__all__ = [
    "ContextIssuer",
    "ContextIssuerError",
    "new_context_id",
]
