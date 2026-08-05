"""Canonical identity surface: local principal, account session, Organisation.

Everything here is standard-library-only and lives under ``src/omnivia_core``,
so an MCP consumer reaches it without ``omnivia-core-runtime`` on the path.
"""

from __future__ import annotations

from omnivia_core.identity.lifecycle import (
    ORGANISATION_TRANSITIONS,
    SESSION_TRANSITIONS,
    archive,
    bind_session,
    expire_session,
    is_session_active,
    project_identity,
    restore,
    sign_out,
    suspend,
)
from omnivia_core.identity.models import (
    IDENTITY_REF_PATTERN,
    AccountSession,
    AccountSessionState,
    IdentityError,
    IdentityProjection,
    LocalPrincipal,
    Organisation,
    OrganisationState,
)

__all__ = [
    "IDENTITY_REF_PATTERN",
    "ORGANISATION_TRANSITIONS",
    "SESSION_TRANSITIONS",
    "AccountSession",
    "AccountSessionState",
    "IdentityError",
    "IdentityProjection",
    "LocalPrincipal",
    "Organisation",
    "OrganisationState",
    "archive",
    "bind_session",
    "expire_session",
    "is_session_active",
    "project_identity",
    "restore",
    "sign_out",
    "suspend",
]
