"""The issuer that holds a ``ShellContext`` and rotates it lawfully.

What this is, and what it is not
--------------------------------

This is the *holder* of trusted-host authority, not a second implementation of
it. Every rule about whether a rotation is lawful, which fields are rotation
triggers, and which handles survive already exists in the frozen
:mod:`omnivia_core.host_contract.v1.codec` -- :func:`validate_context_rotation`,
:func:`changed_authority_fields` and :func:`is_handle_valid`. This module calls
them. It does not restate them, and a change to the rules belongs there (under
``GOV-HOST-CONTRACT-APPROVAL-001``), never here.

What was missing is the thing the codec deliberately does not provide: something
that *has* a current context, can produce the next one, and refuses to produce
one the codec would reject. Platform will build a session coordinator on top of
this; MCP can use it directly, because it lives under ``src/omnivia_core`` and
depends on nothing outside the standard library.

Why the issuer is immutable
---------------------------

``ShellContext`` is immutable so that a change of authority always has a moment
at which the old authority stopped. A holder that mutated in place would erase
that moment again: two callers sharing one issuer would silently disagree about
which context was current. So rotation returns a *new* issuer, and the caller
decides when to adopt it -- the same discipline, one level up.

Organisation switching is out of scope, and enforced so
-------------------------------------------------------

``organisationRef`` is absent from ``CONTEXT_AUTHORITY_FIELDS``: the Host
Contract grants an Organisation change no invalidation semantics. This issuer
therefore refuses any rotation that changes ``organisationRef`` at all
(:class:`ContextIssuerError`). That refusal is a scope gate, not a contract
rule -- the codec would accept such a rotation, since any difference at all
demands a new ``contextId``. Refusing it here is how Core avoids *simulating*
Organisation authority above the contract layer, which is what manifest
decision D2 defers. When Organisation switching is designed, the invalidation
semantics have to be settled in the contract first, and this gate comes out
then rather than being worked around now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from omnivia_core.host_contract.v1.codec import (
    changed_authority_fields,
    is_handle_valid,
    validate_context_rotation,
)
from omnivia_core.host_contract.v1.generated import ShellContext
from omnivia_core.identity.models import IdentityProjection


class ContextIssuerError(Exception):
    """Raised when a rotation is refused by Core rather than by the codec.

    Kept distinct from :class:`~omnivia_core.host_contract.v1.codec.HostAuthorityError`
    on purpose. A ``HostAuthorityError`` means the frozen contract rejected the
    rotation; a ``ContextIssuerError`` means the contract would have allowed it
    and Core declined, which is a scope decision a reader should be able to tell
    apart from a governed one.
    """


def new_context_id() -> str:
    """Mint an identifier for a context that does not exist yet.

    Random rather than sequential: a context ID that could be guessed from the
    previous one would let a caller name a context it was never issued, and
    ``is_handle_valid`` decides handle validity by comparing exactly this value.
    The ``ctx-`` prefix keeps the value inside the Host Contract's
    ``Identifier`` pattern and readable in an audit trail.
    """
    return f"ctx-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ContextIssuer:
    """Holds the ``ShellContext`` an installation is currently acting under.

    On the rotating methods, an omitted ``context_id`` is minted by
    :func:`new_context_id`, and an omitted ``expires_at`` carries the current
    context's expiry forward rather than clearing it -- see :meth:`_issue`.
    """

    current: ShellContext

    def __post_init__(self) -> None:
        if type(self.current) is not ShellContext:
            raise ContextIssuerError("an issuer must hold a ShellContext")
        self.current.validate()

    # -- reading the current authority -------------------------------------

    def handle_is_valid(self, handle_context_id: str, *, now: datetime | None = None) -> bool:
        """Whether a handle issued against ``handle_context_id`` is still usable.

        Delegates to the codec unchanged, including its treatment of an expired
        context as superseding itself.
        """
        return is_handle_valid(self.current, handle_context_id, now=now)

    def changed_since(self, previous: ShellContext) -> tuple[str, ...]:
        """Which governed rotation triggers differ between ``previous`` and now."""
        return changed_authority_fields(previous, self.current)

    # -- producing the next authority --------------------------------------

    def rotate(self, new: ShellContext) -> ContextIssuer:
        """Adopt ``new`` as the current context, or refuse.

        The codec decides lawfulness. The one thing checked first is the
        Organisation gate, so a caller attempting a deferred capability gets the
        scope answer rather than a governed-looking one.
        """
        if type(new) is not ShellContext:
            raise ContextIssuerError("a rotation requires a ShellContext")
        _require_organisation_unchanged(self.current, new)
        validate_context_rotation(self.current, new)
        return ContextIssuer(current=new)

    def switch_workspace(
        self,
        workspace_ref: str,
        *,
        issued_at: str,
        context_id: str | None = None,
        expires_at: str | None = None,
    ) -> ContextIssuer:
        """Rotate onto ``workspace_ref``.

        ``workspace_ref`` is the portable ``WorkspaceManifest.workspace_id``;
        :func:`omnivia_core.workspace.lifecycle.workspace_ref` is the only place
        that binding is made, and this method expects its result.

        Switching to the Workspace already selected is a no-op and returns this
        same issuer. Minting a new context for it would invalidate every live
        handle -- ``is_handle_valid`` is keyed on ``contextId`` -- in exchange
        for no change of authority at all.
        """
        if type(workspace_ref) is not str:
            raise ContextIssuerError("workspace_ref must be a string")
        if workspace_ref == self.current.workspace_ref:
            return self
        return self._issue(
            issued_at=issued_at,
            context_id=context_id,
            expires_at=expires_at,
            workspace_ref=workspace_ref,
        )

    def apply_identity(
        self,
        projection: IdentityProjection,
        *,
        issued_at: str,
        context_id: str | None = None,
        expires_at: str | None = None,
    ) -> ContextIssuer:
        """Rotate onto the identity ``projection`` names.

        This is the whole path by which an optional account session reaches the
        shell: it lands on ``principalRef`` and ``entitlementSummaryRef``, the
        two fields ``ShellContext`` already declares and
        ``CONTEXT_AUTHORITY_FIELDS`` already treats as rotation triggers. No
        field is added to the frozen record, and no account state is carried in
        it -- the session itself stays in
        :mod:`omnivia_core.identity.models`, outside the context entirely.

        A projection identical to the current authority is a no-op, for the same
        reason a redundant Workspace switch is.
        """
        if type(projection) is not IdentityProjection:
            raise ContextIssuerError("apply_identity requires an IdentityProjection")
        if (
            projection.principal_ref == self.current.principal_ref
            and projection.entitlement_summary_ref == self.current.entitlement_summary_ref
        ):
            return self
        return self._issue(
            issued_at=issued_at,
            context_id=context_id,
            expires_at=expires_at,
            principal_ref=projection.principal_ref,
            entitlement_summary_ref=projection.entitlement_summary_ref,
        )

    def _issue(
        self,
        *,
        issued_at: str,
        context_id: str | None,
        expires_at: str | None,
        **changes: str,
    ) -> ContextIssuer:
        """Build the successor context and put it through :meth:`rotate`.

        ``contextVersion`` advances by one and ``contextId`` is new, because the
        codec requires both of a lawful successor. They are set here rather than
        left to the caller so that no caller can produce a context that only
        fails at the boundary; ``rotate`` still re-checks, because the codec is
        the authority on whether the result holds.

        An omitted ``expires_at`` carries the current context's expiry forward
        rather than clearing it. Clearing it is not something a switch is
        allowed to do incidentally: the previous record stated when its
        authority ends, and a successor that quietly drops that statement would
        extend authority the caller never asked to extend.
        """
        candidate = replace(
            self.current,
            context_id=new_context_id() if context_id is None else context_id,
            context_version=self.current.context_version + 1,
            issued_at=issued_at,
            expires_at=self.current.expires_at if expires_at is None else expires_at,
            **changes,
        )
        return self.rotate(candidate)


def _require_organisation_unchanged(old: ShellContext, new: ShellContext) -> None:
    if old.organisation_ref != new.organisation_ref:
        raise ContextIssuerError(
            "Organisation switching is out of scope: organisationRef is not a "
            "governed rotation trigger, so Core will not invalidate authority on it"
        )


__all__ = [
    "ContextIssuer",
    "ContextIssuerError",
    "new_context_id",
]
