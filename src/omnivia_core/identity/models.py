"""Local principal, optional account session, and Organisation records.

Core is local-first: an installation has a principal before it has an account,
and it must keep working when it never gets one. So the always-present identity
is :class:`LocalPrincipal`, and :class:`AccountSession` is strictly optional
state layered on top of it -- never a precondition for having authority.

Why the account session is modelled *outside* ``ShellContext``
-------------------------------------------------------------

Host Contract v1 is byte-frozen under ``GOV-HOST-CONTRACT-APPROVAL-001``, so
``ShellContext`` gains no field for a signed-in account, and nothing here may
add one. An account session reaches the shell only as a *projection* onto the
two authority fields ``ShellContext`` already declares -- ``principalRef`` and
``entitlementSummaryRef`` -- which is what :class:`IdentityProjection` carries.
Both are already governed rotation triggers in
``omnivia_core.host_contract.v1.codec.CONTEXT_AUTHORITY_FIELDS``, so signing in
or out rotates the context through the existing rule rather than through a new
one. That is the whole seam: Core states *what* identity the context should
carry, and the frozen codec decides whether a rotation carrying it is lawful.

Records validate on construction rather than on demand. The codec validates on
demand because it is decoding hostile wire documents and has to fail closed at
the boundary; these records are built locally by trusted Core code, and an
authority record that can exist in an invalid state is a record some later
caller will read as if it were valid.

Organisation is modelled and given a lifecycle here, and nothing more. It has
no switch verb anywhere in this lane: ``organisationRef`` is deliberately absent
from ``CONTEXT_AUTHORITY_FIELDS``, so an Organisation switch has no invalidation
semantics at the contract layer, and simulating one above that layer would
invent authority the Host Contract does not grant (manifest decision D2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final

#: The shape every authority reference Core mints must satisfy, so that a value
#: produced here is always admissible where the Host Contract expects an
#: ``Identifier``. Transcribed from the frozen contract's own pattern rather
#: than imported, so this module stays readable on its own; the parity of the
#: two spellings is asserted by ``tests/identity/test_principal_and_organisation``.
IDENTITY_REF_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

_REF_RE: Final[re.Pattern[str]] = re.compile(IDENTITY_REF_PATTERN)


class IdentityError(Exception):
    """Raised when an identity record would carry authority it is not entitled to.

    Deliberately distinct from a decode error: the values involved are ordinary
    Python values, and what fails is the trust rule about what a principal,
    session or Organisation record is allowed to say.
    """


class AccountSessionState(str, Enum):
    """Lifecycle state of an optional account session."""

    ACTIVE = "active"
    EXPIRED = "expired"
    SIGNED_OUT = "signed_out"


class OrganisationState(str, Enum):
    """Lifecycle state of an Organisation record."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


def _require_ref(field_name: str, value: object) -> None:
    """Raise unless ``value`` is a reference the Host Contract would accept."""
    if type(value) is not str or _REF_RE.fullmatch(value) is None:
        raise IdentityError(f"{field_name} must be a valid authority reference")


def _require_text(field_name: str, value: object) -> None:
    """Raise unless ``value`` is a non-empty display string."""
    if type(value) is not str or not value.strip():
        raise IdentityError(f"{field_name} must be a non-empty string")


def _require_instant(field_name: str, value: object) -> datetime:
    """Raise unless ``value`` names a fixed instant, and return it.

    A time zone is mandatory, for the same reason the Host Contract requires one:
    a record whose validity window depends on the reader's local clock is not a
    fixed instant, and two machines would disagree about when it lapsed.
    """
    if type(value) is not str:
        raise IdentityError(f"{field_name} must be an RFC 3339 timestamp")
    text = f"{value[:-1]}+00:00" if value[-1:] in ("Z", "z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise IdentityError(f"{field_name} must be an RFC 3339 timestamp") from None
    if parsed.utcoffset() is None:
        raise IdentityError(f"{field_name} must carry a time zone")
    return parsed


def parse_instant(field_name: str, value: str) -> datetime:
    """Public form of the instant rule these records are held to."""
    return _require_instant(field_name, value)


@dataclass(frozen=True)
class LocalPrincipal:
    """The identity a Core installation acts as when no account is involved.

    This record always exists. It carries its own ``entitlement_summary_ref``
    because ``ShellContext.entitlementSummaryRef`` is a required field: there is
    no "no entitlements" spelling to fall back on, and inventing a sentinel here
    would put a value into an authority field that names nothing real. The local
    summary is what an installation is entitled to before it signs in, which is
    a real answer rather than a placeholder.
    """

    principal_ref: str
    entitlement_summary_ref: str
    display_name: str
    created_at: str

    def __post_init__(self) -> None:
        _require_ref("principal_ref", self.principal_ref)
        _require_ref("entitlement_summary_ref", self.entitlement_summary_ref)
        _require_text("display_name", self.display_name)
        _require_instant("created_at", self.created_at)


@dataclass(frozen=True)
class AccountSession:
    """An optional signed-in account session, held outside ``ShellContext``.

    ``local_principal_ref`` binds the session to the :class:`LocalPrincipal` it
    was established from, so signing out has somewhere to fall back to that is
    not a guess. ``principal_ref`` and ``entitlement_summary_ref`` are what the
    session projects into the context while it is active -- they are account
    authority, not local authority, which is exactly why they are separate
    fields rather than a mutation of the local principal.

    ``expires_at`` is the session's own statement of when it stops being
    authority. ``None`` means the session ends with the process rather than with
    a clock; it never means "never expires as long as someone holds it".
    """

    session_id: str
    account_ref: str
    local_principal_ref: str
    principal_ref: str
    entitlement_summary_ref: str
    issued_at: str
    state: AccountSessionState = AccountSessionState.ACTIVE
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _require_ref("session_id", self.session_id)
        _require_ref("account_ref", self.account_ref)
        _require_ref("local_principal_ref", self.local_principal_ref)
        _require_ref("principal_ref", self.principal_ref)
        _require_ref("entitlement_summary_ref", self.entitlement_summary_ref)
        issued = _require_instant("issued_at", self.issued_at)
        if type(self.state) is not AccountSessionState:
            raise IdentityError("state must be an AccountSessionState")
        if self.expires_at is not None:
            expiry = _require_instant("expires_at", self.expires_at)
            if expiry <= issued:
                raise IdentityError("expires_at must be after issued_at")


@dataclass(frozen=True)
class Organisation:
    """An Organisation a principal may belong to.

    Modelled and given a lifecycle, and given no switch verb: see this module's
    docstring for why an Organisation switch is not expressible in this lane.
    """

    organisation_ref: str
    display_name: str
    created_at: str
    state: OrganisationState = OrganisationState.ACTIVE

    def __post_init__(self) -> None:
        _require_ref("organisation_ref", self.organisation_ref)
        _require_text("display_name", self.display_name)
        _require_instant("created_at", self.created_at)
        if type(self.state) is not OrganisationState:
            raise IdentityError("state must be an OrganisationState")


@dataclass(frozen=True)
class IdentityProjection:
    """What identity contributes to a ``ShellContext``: two existing fields.

    This is the entire surface an account session is allowed to reach the shell
    through. It is deliberately not a ``ShellContext``, and deliberately not a
    superset of one: a projection that could name a third field would be the
    first step towards adding one to the frozen record.
    """

    principal_ref: str
    entitlement_summary_ref: str

    def __post_init__(self) -> None:
        _require_ref("principal_ref", self.principal_ref)
        _require_ref("entitlement_summary_ref", self.entitlement_summary_ref)


__all__ = [
    "IDENTITY_REF_PATTERN",
    "AccountSession",
    "AccountSessionState",
    "IdentityError",
    "IdentityProjection",
    "LocalPrincipal",
    "Organisation",
    "OrganisationState",
    "parse_instant",
]
