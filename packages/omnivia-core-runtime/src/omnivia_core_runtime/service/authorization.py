"""The authorization boundary (B9, ADR-037/ADR-038).

Three independent gates, all fail-closed: a fixed principal, a workspace allowlist
and a granted operation set. An adapter is confined to what it was granted, so a
compromised or buggy client cannot widen its own authority by asking differently.

Authorising is separate from writing. This module decides whether a request may
proceed; it never touches storage. CONTRACT-000 Section 8.8's separation of
approving from writing is the same principle applied one layer up.
"""

from __future__ import annotations

from dataclasses import dataclass


class AuthorizationDenied(Exception):
    """A request was refused by the authorization boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Grant:
    """What one client may do.

    `operations` is an explicit allowlist rather than a denylist: a new operation is
    unavailable until granted, which is the safe default when the catalogue is still
    growing.
    """

    principal: str
    workspaces: frozenset[str]
    operations: frozenset[str]

    def permits(self, *, workspace_id: str, operation: str) -> bool:
        return workspace_id in self.workspaces and operation in self.operations


def authorize(
    grant: Grant,
    *,
    principal_claim: str | None,
    workspace_id: str,
    operation: str,
) -> None:
    """Refuse anything the grant does not explicitly permit.

    The principal is fixed by the grant, not taken from the request. A client that
    could name its own principal could impersonate another, so a mismatched claim is
    a denial rather than an override.
    """
    if principal_claim is not None and principal_claim != grant.principal:
        raise AuthorizationDenied(
            "core.principal_mismatch",
            f"request claims principal {principal_claim!r}, grant is for "
            f"{grant.principal!r}",
        )
    if workspace_id not in grant.workspaces:
        raise AuthorizationDenied(
            "core.workspace_not_granted",
            f"principal {grant.principal!r} is not granted workspace {workspace_id!r}",
        )
    if operation not in grant.operations:
        raise AuthorizationDenied(
            "core.operation_not_granted",
            f"principal {grant.principal!r} is not granted operation {operation!r}",
        )


__all__ = ["AuthorizationDenied", "Grant", "authorize"]
