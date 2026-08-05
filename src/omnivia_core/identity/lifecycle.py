"""Transition rules for the optional account session and for Organisation.

Every function here is pure: it takes records and an explicit instant and
returns a new record. Nothing reads a clock it was not given, because a
lifecycle decision that depends on an ambient clock cannot be replayed, and an
authority decision that cannot be replayed cannot be audited.

The transitions are stated as data (:data:`SESSION_TRANSITIONS`,
:data:`ORGANISATION_TRANSITIONS`) and the verbs are thin wrappers over them, so
"which move is legal" is one readable table rather than a set of ``if``
branches that can disagree with each other.

Scope note: there is no Organisation *switch* verb, and there must not be one.
``organisationRef`` is absent from the Host Contract's
``CONTEXT_AUTHORITY_FIELDS``, so an Organisation change has no invalidation
semantics at the contract layer. Providing a switch here would mean Core
inventing invalidation the frozen contract does not grant, which is precisely
what manifest decision D2 defers. Workspace is the only switchable authority in
this lane; see :mod:`omnivia_core.workspace.lifecycle`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Final

from omnivia_core.identity.models import (
    AccountSession,
    AccountSessionState,
    IdentityError,
    IdentityProjection,
    LocalPrincipal,
    Organisation,
    OrganisationState,
    parse_instant,
)

#: Legal account-session moves. ``ACTIVE`` is the only state with anywhere to
#: go: expiry and sign-out are both terminal, and a session that could be
#: reactivated would make "signed out" a suggestion rather than a fact.
SESSION_TRANSITIONS: Final[Mapping[AccountSessionState, frozenset[AccountSessionState]]] = {
    AccountSessionState.ACTIVE: frozenset(
        {AccountSessionState.EXPIRED, AccountSessionState.SIGNED_OUT}
    ),
    AccountSessionState.EXPIRED: frozenset(),
    AccountSessionState.SIGNED_OUT: frozenset(),
}

#: Legal Organisation moves. Suspension is reversible because it describes a
#: temporary administrative state; archival is not, because an archived
#: Organisation that can come back is not archived.
ORGANISATION_TRANSITIONS: Final[Mapping[OrganisationState, frozenset[OrganisationState]]] = {
    OrganisationState.ACTIVE: frozenset(
        {OrganisationState.SUSPENDED, OrganisationState.ARCHIVED}
    ),
    OrganisationState.SUSPENDED: frozenset(
        {OrganisationState.ACTIVE, OrganisationState.ARCHIVED}
    ),
    OrganisationState.ARCHIVED: frozenset(),
}


def bind_session(principal: LocalPrincipal, session: AccountSession) -> AccountSession:
    """Return ``session`` once it is confirmed to belong to ``principal``.

    A session is only ever layered onto the local principal it was established
    from. Accepting one bound to a different principal would let a signed-in
    account's entitlements be projected onto an installation that never
    established it, which is authority transfer by mismatched paperwork.
    """
    if type(principal) is not LocalPrincipal or type(session) is not AccountSession:
        raise IdentityError("bind_session requires a LocalPrincipal and an AccountSession")
    if session.local_principal_ref != principal.principal_ref:
        raise IdentityError("account session is not bound to this local principal")
    return session


def is_session_active(session: AccountSession, *, now: datetime) -> bool:
    """Whether ``session`` still carries authority at ``now``.

    Both halves matter. A session whose state is not ``ACTIVE`` is finished, and
    an ``ACTIVE`` session past its own ``expires_at`` is finished too -- expiry
    that a still-held record could outlive would not be expiry at all. The
    boundary is exclusive of the instant itself, matching the Host Contract's
    reading of ``expiresAt`` as the first instant the record is no longer valid.
    """
    if type(session) is not AccountSession:
        raise IdentityError("is_session_active requires an AccountSession")
    _require_instant_argument(now)
    if session.state is not AccountSessionState.ACTIVE:
        return False
    if session.expires_at is None:
        return True
    return parse_instant("expires_at", session.expires_at) > now


def expire_session(session: AccountSession) -> AccountSession:
    """Move a session to ``EXPIRED``."""
    return _transition_session(session, AccountSessionState.EXPIRED)


def sign_out(session: AccountSession) -> AccountSession:
    """Move a session to ``SIGNED_OUT``."""
    return _transition_session(session, AccountSessionState.SIGNED_OUT)


def _transition_session(
    session: AccountSession, target: AccountSessionState
) -> AccountSession:
    if type(session) is not AccountSession:
        raise IdentityError("a session transition requires an AccountSession")
    if target not in SESSION_TRANSITIONS[session.state]:
        raise IdentityError(
            f"account session cannot move from {session.state.value!r} "
            f"to {target.value!r}"
        )
    return replace(session, state=target)


def suspend(organisation: Organisation) -> Organisation:
    """Move an Organisation to ``SUSPENDED``."""
    return _transition_organisation(organisation, OrganisationState.SUSPENDED)


def restore(organisation: Organisation) -> Organisation:
    """Move a suspended Organisation back to ``ACTIVE``."""
    return _transition_organisation(organisation, OrganisationState.ACTIVE)


def archive(organisation: Organisation) -> Organisation:
    """Move an Organisation to ``ARCHIVED``, which is terminal."""
    return _transition_organisation(organisation, OrganisationState.ARCHIVED)


def _transition_organisation(
    organisation: Organisation, target: OrganisationState
) -> Organisation:
    if type(organisation) is not Organisation:
        raise IdentityError("an Organisation transition requires an Organisation")
    if target not in ORGANISATION_TRANSITIONS[organisation.state]:
        raise IdentityError(
            f"Organisation cannot move from {organisation.state.value!r} "
            f"to {target.value!r}"
        )
    return replace(organisation, state=target)


def project_identity(
    principal: LocalPrincipal,
    session: AccountSession | None = None,
    *,
    now: datetime,
) -> IdentityProjection:
    """Return the two ``ShellContext`` fields identity is allowed to decide.

    With no session, or with a session that is no longer active at ``now``, the
    projection is the local principal's own pair. That is the fail-safe
    direction: losing an account session drops an installation back to local
    authority rather than leaving it holding account entitlements it can no
    longer prove.

    The result is not applied to anything here. Handing it to
    ``omnivia_core.session.context.ContextIssuer.apply_identity`` is what turns
    it into a rotation, and the frozen codec is what decides whether that
    rotation is lawful.
    """
    if type(principal) is not LocalPrincipal:
        raise IdentityError("project_identity requires a LocalPrincipal")
    _require_instant_argument(now)
    if session is None:
        return _local_projection(principal)
    bind_session(principal, session)
    if not is_session_active(session, now=now):
        return _local_projection(principal)
    return IdentityProjection(
        principal_ref=session.principal_ref,
        entitlement_summary_ref=session.entitlement_summary_ref,
    )


def _local_projection(principal: LocalPrincipal) -> IdentityProjection:
    return IdentityProjection(
        principal_ref=principal.principal_ref,
        entitlement_summary_ref=principal.entitlement_summary_ref,
    )


def _require_instant_argument(now: object) -> None:
    """Raise unless ``now`` is a time-zone-aware instant.

    Matching the Host Contract's authority checks: a naive ``datetime`` compares
    against a floating local clock, so a caller pinning an instant for a
    deterministic decision would silently not be pinning anything.
    """
    if type(now) is not datetime or now.utcoffset() is None:
        raise IdentityError("a lifecycle decision requires a time-zone-aware instant")


__all__ = [
    "ORGANISATION_TRANSITIONS",
    "SESSION_TRANSITIONS",
    "archive",
    "bind_session",
    "expire_session",
    "is_session_active",
    "project_identity",
    "restore",
    "sign_out",
    "suspend",
]
