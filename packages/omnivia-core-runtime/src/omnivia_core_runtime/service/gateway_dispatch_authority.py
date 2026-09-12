"""Call-time dispatch authority for one invocation (T-0688 IP-04, WEFT-BL-004).

:func:`~omnivia_core_runtime.service.capability_gateway.authorize_invocation` answers
*may this run invoke this capability* from records that already exist. That answer is a
fact about the past the moment it is returned. This module answers the only question
left before an adapter or a transport becomes reachable: *is that authority still the
current authority, right now, for exactly this process, this worker and this
destination.*

**Possession is not permission.** An `AuthorizedInvocation` is a value; it can be held,
passed, retried and replayed. So it authorizes nothing here on its own: a current
permission decision is resolved on *every* dispatch call, and a revocation that lands
between two calls refuses the second one even though the invocation is unchanged. There
is no cache, no memo and no "already checked this attempt" path -- those are exactly the
shapes that turn a revoked grant into a live one.

**Local mode is not a weaker mode.** Authenticated local IPC and the authenticated Cloud
service transport run the identical logical checks; the transport kind is a bound fact
that must match the decision, never a discount on the checks. Anything else -- an
unauthenticated caller, an unknown kind -- fails closed.

**No ambient authority.** The worker states, on the record, that it holds neither
ambient database authority nor ambient secret authority. Either flag being true refuses
dispatch: authority that arrives from the environment rather than from this decision is
authority this seam cannot bound, and both flags default to the refusing value so an
under-specified context authorizes nothing.

**What a permit carries, and what it must never carry.** The permit is bounded non-secret
facts: who, for what, where, until when. There is no adapter, endpoint credential,
database handle, secret or request body on it, and there must never be one -- the permit
is logged, correlated and passed on, so anything on it is published everywhere those go.
The means of acting stays with dispatch; the permit is only the statement that acting is
currently permitted.

**Destination is a canonical HTTPS origin and nothing more.** `https://host[:port]`,
with no credentials, path, query or fragment, matched exactly against the origin the
decision names. *That is admission of a name, not of an address.* Re-checking the address
actually connected to -- the private-zone rebinding of `FX-WEFT-EGRESS` -- happens at
connect time and belongs to the Platform lane; nothing here resolves a name, and a permit
from this module must not be read as clearing a connection.

Every decision, allowed or refused, is recorded through an injected audit sink before it
is acted on: on the allow path the permit is returned only after the record lands, and a
sink that fails refuses. Reasons are fixed literals, never built from caller input, for
the reason the gateway freezes its own -- a refusal is rendered into logs and audit
records, so anything interpolated into one is republished there.

Local and in-process throughout. No database, network, DNS, clock, sleep, randomness or
credential: `now`, the permission resolver and the audit sink are all arguments, which is
what makes each rule below testable as a rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol
from urllib.parse import urlsplit

# The gateway's own value-domain and credential guards, by reference rather than
# re-spelled. Re-declaring the secret shapes here would mean two lists that drift, and
# the second one is always the one that misses a token shape.
from .capability_gateway import (
    _CAPABILITY_ID_MIN_LENGTH,
    _CAPABILITY_ID_RE,
    _IDENTIFIER_RE,
    AuthorizedInvocation,
    _instant,
    _usable,
)

#: The closed set of authenticated transport kinds. Both run the same checks; an
#: unauthenticated or unrecognised kind is not in the set and so refuses.
TRANSPORT_LOCAL_IPC: Final = "authenticated_local_ipc"
TRANSPORT_CLOUD_SERVICE: Final = "authenticated_cloud_service"
AUTHENTICATED_TRANSPORTS: Final = frozenset(
    {TRANSPORT_LOCAL_IPC, TRANSPORT_CLOUD_SERVICE}
)

#: The longest validity window a permission decision may state. A decision is a
#: statement about *now*; one good for an hour is a possession token by another name.
MAX_PERMISSION_VALIDITY: Final = timedelta(minutes=5)

#: The longest destination string read at all, before any parsing of it.
MAX_DESTINATION_LENGTH: Final = 255

#: Bounded refusal reasons. Non-secret by construction: each is a fixed literal, and no
#: caller-controlled value is ever interpolated into one.
REFUSE_MALFORMED_CONTEXT: Final = "malformed_dispatch_context"
REFUSE_MALFORMED_INVOCATION: Final = "malformed_authorized_invocation"
REFUSE_INSTANT_NOT_USABLE: Final = "instant_not_usable"
REFUSE_TRANSPORT_NOT_AUTHENTICATED: Final = "transport_not_authenticated"
REFUSE_AMBIENT_DATABASE_AUTHORITY: Final = "ambient_database_authority"
REFUSE_AMBIENT_SECRET_AUTHORITY: Final = "ambient_secret_authority"
REFUSE_DESTINATION_NOT_CANONICAL: Final = "destination_not_canonical_https_origin"
REFUSE_PERMISSION_UNAVAILABLE: Final = "current_permission_unavailable"
REFUSE_PERMISSION_MALFORMED: Final = "current_permission_malformed"
REFUSE_PERMISSION_NOT_ALLOWED: Final = "current_permission_not_allowed"
REFUSE_PERMISSION_REVOKED: Final = "current_permission_revoked"
REFUSE_PERMISSION_OUT_OF_WINDOW: Final = "current_permission_out_of_window"
REFUSE_PERMISSION_WINDOW_NOT_SHORT: Final = "current_permission_window_not_short"
REFUSE_PERMISSION_MISMATCH: Final = "current_permission_does_not_match_context"
REFUSE_AUDIT_UNAVAILABLE: Final = "audit_sink_unavailable"


class GatewayDispatchRefusal(Exception):
    """One refusal from this seam, carrying a fixed reason and no caller value.

    The reason is one of the `REFUSE_*` literals above and is the whole message: a
    refusal produced from caller-controlled input is rendered into logs and audit
    records, so the enumerable set is the only safe one.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DispatchContext:
    """The facts a dispatch call is bound to, as this process knows them.

    Not a wire record and not a handle: there is no socket, adapter, connection or
    credential here, only the identities a decision can be checked against.

    Both ambient-authority flags default to `True` -- the refusing value -- so a context
    assembled from partial information dispatches nothing. Stating "I hold no ambient
    database authority" is a claim the worker must make explicitly; silence is not it.
    """

    process_id: str
    worker_id: str
    tenant_id: str
    project_id: str
    run_id: str
    attempt_id: str
    transport_kind: str
    destination: str
    ambient_database_authority: bool = True
    ambient_secret_authority: bool = True


@dataclass(frozen=True, slots=True)
class CurrentPermission:
    """A permission decision taken *now*, by whatever holds current policy.

    Every identity the context binds appears here too, because the decision has to be a
    decision about *this* call: a well-formed, currently-allowed decision for another
    worker, another attempt or another destination is not permission for this one.

    `allowed` defaults to false and `revoked` to true, so a partially populated decision
    permits nothing. The window is stated explicitly and bounded by
    :data:`MAX_PERMISSION_VALIDITY`; timestamps are timezone-aware ISO instants.
    """

    decision_id: str
    permission_revision: str
    process_id: str
    worker_id: str
    tenant_id: str
    project_id: str
    run_id: str
    attempt_id: str
    transport_kind: str
    destination_origin: str
    capability_id: str
    capability_grant_id: str
    policy_snapshot_id: str
    binding_id: str
    not_before: str
    not_after: str
    allowed: bool = False
    revoked: bool = True


@dataclass(frozen=True, slots=True)
class DispatchPermit:
    """The single output of an allowed dispatch: bounded, non-secret, short-lived.

    Its identity *is* the decision's -- one permit for one current decision, so the
    permit and the audit record and the decision are all the same event under one id.

    There is deliberately no adapter, endpoint, credential, database handle, secret or
    request body on this value, and none may be added: it is returned, logged and
    correlated, so a field here is a field published wherever it travels.
    """

    decision_id: str
    process_id: str
    worker_id: str
    tenant_id: str
    project_id: str
    run_id: str
    attempt_id: str
    capability_id: str
    binding_id: str
    destination_origin: str
    permission_revision: str
    authorized_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchAuditRecord:
    """The immutable record of one dispatch decision, allowed or refused.

    Identities and the destination origin are carried only when they are canonical
    values this seam already proved safe; anything else is `None`. A record of a refusal
    must not become the place a malformed or credential-shaped caller value is published.
    """

    allowed: bool
    reason: str
    transport_kind: str | None
    process_id: str | None
    worker_id: str | None
    tenant_id: str | None
    project_id: str | None
    run_id: str | None
    attempt_id: str | None
    capability_id: str | None
    binding_id: str | None
    destination_origin: str | None
    decision_id: str | None = None
    permission_revision: str | None = None


#: The reason recorded on the allow path. A fixed literal like every refusal reason.
ALLOW_REASON: Final = "current_permission_allows_dispatch"


class CurrentPermissionResolver(Protocol):
    """Answers *is this permitted right now*, once per dispatch call.

    It is consulted, never trusted: everything it returns is re-checked below against
    the bound context and the invocation, so a resolver cannot widen authority by
    answering about a different worker, run or destination.
    """

    def resolve(
        self, context: DispatchContext, invocation: AuthorizedInvocation
    ) -> CurrentPermission: ...


class DispatchAuditSink(Protocol):
    """Records one decision durably. Required, not optional: an unrecorded allow refuses."""

    def record(self, entry: DispatchAuditRecord) -> None: ...


def _safe(value: object, *, capability: bool = False) -> str | None:
    """`value` if it is a canonical, bounded, non-credential identifier, else `None`."""
    if not isinstance(value, str):
        return None
    if capability:
        return (
            value
            if _usable(
                value, _CAPABILITY_ID_RE, minimum_length=_CAPABILITY_ID_MIN_LENGTH
            )
            else None
        )
    return value if _usable(value, _IDENTIFIER_RE) else None


def canonical_https_origin(destination: object) -> str | None:
    """`destination` as a canonical `https://host[:port]` origin, or `None`.

    Canonical means the string *is* its own origin: nothing is normalised away and
    nothing is rebuilt for the caller. A trailing slash, a path, a query, a fragment,
    embedded credentials, a non-HTTPS scheme, an upper-case host or an empty host are all
    refusals rather than repairs -- a value that has to be edited before it can be matched
    against a decision is not the value the decision was taken about.
    """
    if not isinstance(destination, str) or not 1 <= len(destination) <= (
        MAX_DESTINATION_LENGTH
    ):
        return None
    # Before parsing at all: no whitespace and nothing unprintable anywhere. `urlsplit`
    # carries a trailing space into the host, which would then rebuild into a string
    # equal to the input and pass the identity check below -- an origin that differs
    # from the decision's by a character nothing renders.
    if not destination.isprintable() or any(char.isspace() for char in destination):
        return None
    try:
        parts = urlsplit(destination)
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    try:
        host, port = parts.hostname, parts.port
    except ValueError:  # a port that is not a number in range
        return None
    if not host:
        return None
    origin = f"https://{host}" if port is None else f"https://{host}:{port}"
    return origin if origin == destination else None


def _well_formed_context(context: object) -> bool:
    return (
        type(context) is DispatchContext
        and all(
            _safe(value) is not None
            for value in (
                context.process_id,
                context.worker_id,
                context.tenant_id,
                context.project_id,
                context.run_id,
                context.attempt_id,
            )
        )
        and type(context.ambient_database_authority) is bool
        and type(context.ambient_secret_authority) is bool
        and isinstance(context.transport_kind, str)
        and isinstance(context.destination, str)
    )


def _well_formed_invocation(invocation: object) -> bool:
    return (
        type(invocation) is AuthorizedInvocation
        and _safe(invocation.capability_id, capability=True) is not None
        and all(
            _safe(value) is not None
            for value in (
                invocation.run_id,
                invocation.binding_id,
                invocation.capability_grant_id,
                invocation.policy_snapshot_id,
            )
        )
    )


def _well_formed_permission(permission: object) -> bool:
    return (
        type(permission) is CurrentPermission
        and _safe(permission.capability_id, capability=True) is not None
        and all(
            _safe(value) is not None
            for value in (
                permission.decision_id,
                permission.permission_revision,
                permission.process_id,
                permission.worker_id,
                permission.tenant_id,
                permission.project_id,
                permission.run_id,
                permission.attempt_id,
                permission.capability_grant_id,
                permission.policy_snapshot_id,
                permission.binding_id,
            )
        )
        and permission.transport_kind in AUTHENTICATED_TRANSPORTS
        and canonical_https_origin(permission.destination_origin) is not None
        and type(permission.allowed) is bool
        and type(permission.revoked) is bool
    )


def _matches(
    permission: CurrentPermission,
    context: DispatchContext,
    invocation: AuthorizedInvocation,
    origin: str,
) -> bool:
    """Whether the decision is about exactly this call.

    Every bound context fact, then the invocation's own authority identities. The
    invocation supplies the run, the capability, the grant, the policy and the binding it
    was authorized under; the decision has to name the same ones, or the two are talking
    about different authority.
    """
    return (
        permission.process_id == context.process_id
        and permission.worker_id == context.worker_id
        and permission.tenant_id == context.tenant_id
        and permission.project_id == context.project_id
        and permission.run_id == context.run_id
        and permission.attempt_id == context.attempt_id
        and permission.transport_kind == context.transport_kind
        and permission.destination_origin == origin
        and permission.run_id == invocation.run_id
        and permission.capability_id == invocation.capability_id
        and permission.capability_grant_id == invocation.capability_grant_id
        and permission.policy_snapshot_id == invocation.policy_snapshot_id
        and permission.binding_id == invocation.binding_id
    )


def authorize_dispatch(
    invocation: AuthorizedInvocation,
    *,
    context: DispatchContext,
    permission_resolver: CurrentPermissionResolver,
    audit_sink: DispatchAuditSink,
    now: datetime,
) -> DispatchPermit:
    """Permit one dispatch, right now, or refuse it -- and record either way.

    In order:

    1. the context is a context, in shape and in every value domain it states;
    2. the invocation is an `AuthorizedInvocation` whose authority identities are
       canonical -- what it *proved* is the gateway's business, not re-decided here;
    3. `now` is an absolute instant;
    4. the transport kind is one of the two authenticated kinds;
    5. the worker holds no ambient database and no ambient secret authority;
    6. the destination is a canonical HTTPS origin;
    7. a current permission decision is resolved -- once, on every call -- and is well
       formed, allowed, unrevoked, inside a short explicit window that contains `now`,
       and about exactly this call;
    8. the decision is recorded; only then is a permit returned.

    Local checks come before the resolver so a malformed call is not sent out as a
    question, and the resolver's answer is checked afterwards so it cannot widen anything.
    Fail-closed throughout: every step is a positive proof and there is no path to the
    return that skips one.
    """
    if not _well_formed_context(context):
        # Whatever of a real context is canonical is still worth recording; anything
        # else -- including something that is not a context at all -- records nothing.
        partial = context if type(context) is DispatchContext else None
        raise _refuse(audit_sink, REFUSE_MALFORMED_CONTEXT, partial, None, None)
    if not _well_formed_invocation(invocation):
        raise _refuse(audit_sink, REFUSE_MALFORMED_INVOCATION, context, None, None)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise _refuse(audit_sink, REFUSE_INSTANT_NOT_USABLE, context, invocation, None)
    if context.transport_kind not in AUTHENTICATED_TRANSPORTS:
        raise _refuse(
            audit_sink, REFUSE_TRANSPORT_NOT_AUTHENTICATED, context, invocation, None
        )
    if context.ambient_database_authority:
        raise _refuse(
            audit_sink, REFUSE_AMBIENT_DATABASE_AUTHORITY, context, invocation, None
        )
    if context.ambient_secret_authority:
        raise _refuse(
            audit_sink, REFUSE_AMBIENT_SECRET_AUTHORITY, context, invocation, None
        )
    origin = canonical_https_origin(context.destination)
    if origin is None:
        raise _refuse(
            audit_sink, REFUSE_DESTINATION_NOT_CANONICAL, context, invocation, None
        )

    # Every dispatch asks again. Nothing about this call is remembered from the last one,
    # which is what makes a revocation take effect on the very next call.
    #
    # The failure is recorded in a sentinel and answered after the handler ends, never
    # raised from inside it: `raise ... from None` would still leave whatever the
    # resolver quoted on `__context__`, one attribute access from anything that logs it.
    permission: CurrentPermission | None = None
    answered = True
    try:
        permission = permission_resolver.resolve(context, invocation)
    except Exception:  # noqa: BLE001 - any failure to answer is "not permitted"
        answered = False
    if not answered:
        raise _refuse(
            audit_sink, REFUSE_PERMISSION_UNAVAILABLE, context, invocation, origin
        )

    if permission is None or not _well_formed_permission(permission):
        raise _refuse(
            audit_sink, REFUSE_PERMISSION_MALFORMED, context, invocation, origin
        )
    if permission.revoked:
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_REVOKED,
            context,
            invocation,
            origin,
            permission,
        )
    if not permission.allowed:
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_NOT_ALLOWED,
            context,
            invocation,
            origin,
            permission,
        )

    not_before = _instant(permission.not_before)
    not_after = _instant(permission.not_after)
    if not_before is None or not_after is None or not_after <= not_before:
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_MALFORMED,
            context,
            invocation,
            origin,
            permission,
        )
    if not_after - not_before > MAX_PERMISSION_VALIDITY:
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_WINDOW_NOT_SHORT,
            context,
            invocation,
            origin,
            permission,
        )
    if not not_before <= now < not_after:
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_OUT_OF_WINDOW,
            context,
            invocation,
            origin,
            permission,
        )
    if not _matches(permission, context, invocation, origin):
        raise _refuse(
            audit_sink,
            REFUSE_PERMISSION_MISMATCH,
            context,
            invocation,
            origin,
            permission,
        )

    # Recorded before it is acted on. An allow nobody can account for is not an allow.
    _record(
        audit_sink,
        DispatchAuditRecord(
            allowed=True,
            reason=ALLOW_REASON,
            transport_kind=context.transport_kind,
            process_id=context.process_id,
            worker_id=context.worker_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            capability_id=invocation.capability_id,
            binding_id=invocation.binding_id,
            destination_origin=origin,
            decision_id=permission.decision_id,
            permission_revision=permission.permission_revision,
        ),
    )
    return DispatchPermit(
        decision_id=permission.decision_id,
        process_id=context.process_id,
        worker_id=context.worker_id,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        run_id=context.run_id,
        attempt_id=context.attempt_id,
        capability_id=invocation.capability_id,
        binding_id=invocation.binding_id,
        destination_origin=origin,
        permission_revision=permission.permission_revision,
        authorized_at=now,
        expires_at=not_after,
    )


def _record(audit_sink: DispatchAuditSink, entry: DispatchAuditRecord) -> None:
    """Write one record, or refuse with the fixed audit reason.

    A sink that cannot record is indistinguishable from one whose records nobody will
    ever see, so both are the same refusal -- on the allow path *and* on a refusal path,
    where a decision that leaves no trace is the one worth keeping.
    """
    recorded = True
    try:
        audit_sink.record(entry)
    except Exception:  # noqa: BLE001 - any failure to record is a fail-closed refusal
        recorded = False
    if not recorded:
        raise GatewayDispatchRefusal(REFUSE_AUDIT_UNAVAILABLE)


def _refuse(
    audit_sink: DispatchAuditSink,
    reason: str,
    context: DispatchContext | None,
    invocation: AuthorizedInvocation | None,
    origin: str | None,
    permission: CurrentPermission | None = None,
) -> GatewayDispatchRefusal:
    """Record the refusal, then hand back the exception for the caller to raise.

    Only values already proved canonical are carried; everything else is `None`. The
    permission's own identities are included when there is a well-formed decision to
    quote, which is what lets a revocation be traced to the revision that revoked it.
    """
    _record(
        audit_sink,
        DispatchAuditRecord(
            allowed=False,
            reason=reason,
            transport_kind=(
                context.transport_kind
                if context is not None
                and context.transport_kind in AUTHENTICATED_TRANSPORTS
                else None
            ),
            process_id=_safe(context.process_id) if context else None,
            worker_id=_safe(context.worker_id) if context else None,
            tenant_id=_safe(context.tenant_id) if context else None,
            project_id=_safe(context.project_id) if context else None,
            run_id=_safe(context.run_id) if context else None,
            attempt_id=_safe(context.attempt_id) if context else None,
            capability_id=(
                _safe(invocation.capability_id, capability=True) if invocation else None
            ),
            binding_id=_safe(invocation.binding_id) if invocation else None,
            destination_origin=origin,
            decision_id=_safe(permission.decision_id) if permission else None,
            permission_revision=(
                _safe(permission.permission_revision) if permission else None
            ),
        ),
    )
    return GatewayDispatchRefusal(reason)


__all__ = [
    "ALLOW_REASON",
    "AUTHENTICATED_TRANSPORTS",
    "MAX_DESTINATION_LENGTH",
    "MAX_PERMISSION_VALIDITY",
    "REFUSE_AMBIENT_DATABASE_AUTHORITY",
    "REFUSE_AMBIENT_SECRET_AUTHORITY",
    "REFUSE_AUDIT_UNAVAILABLE",
    "REFUSE_DESTINATION_NOT_CANONICAL",
    "REFUSE_INSTANT_NOT_USABLE",
    "REFUSE_MALFORMED_CONTEXT",
    "REFUSE_MALFORMED_INVOCATION",
    "REFUSE_PERMISSION_MALFORMED",
    "REFUSE_PERMISSION_MISMATCH",
    "REFUSE_PERMISSION_NOT_ALLOWED",
    "REFUSE_PERMISSION_OUT_OF_WINDOW",
    "REFUSE_PERMISSION_REVOKED",
    "REFUSE_PERMISSION_UNAVAILABLE",
    "REFUSE_PERMISSION_WINDOW_NOT_SHORT",
    "TRANSPORT_CLOUD_SERVICE",
    "TRANSPORT_LOCAL_IPC",
    "CurrentPermission",
    "CurrentPermissionResolver",
    "DispatchAuditRecord",
    "DispatchAuditSink",
    "DispatchContext",
    "DispatchPermit",
    "GatewayDispatchRefusal",
    "authorize_dispatch",
    "canonical_https_origin",
]
