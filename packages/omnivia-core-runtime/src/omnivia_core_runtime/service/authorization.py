"""The authorization boundary (B9, ADR-037/ADR-038).

Two seams live here, deliberately side by side.

The first is the Phase 2 probe boundary: three independent gates, all fail-closed --
a fixed principal, a workspace allowlist and a granted operation set. An adapter is
confined to what it was granted, so a compromised or buggy client cannot widen its
own authority by asking differently. It stays exactly as it was; the service
lifecycle probes are the only thing it governs.

The second is the R2a *application* authority seam, which the accepted operation
catalogue made possible: an authenticated session, a service binding, and one
catalogue-driven decision function that turns a request into effective authority or
into a canonical refusal. It is a separate seam rather than a widening of the first
because the two answer different questions -- the probe grant predates the catalogue
and knows nothing about scopes, purposes, capability versions or installation scope.

Authorising is separate from writing. This module decides whether a request may
proceed; it never touches storage, a transport, a lease, or a conformance harness.
CONTRACT-000 Section 8.8's separation of approving from writing is the same
principle applied one layer up.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

from omnivia_core.contracts.v1 import (
    CAPABILITY_ID_PATTERN,
    CONTRACT_VERSION_PATTERN,
    CORRELATION_ID_PATTERN,
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    IDENTIFIER_PATTERN,
    PURPOSE_PATTERN,
    RELEASE_VERSION_PATTERN,
    REQUEST_ID_PATTERN,
    RETRY_CLASS_NON_RETRYABLE,
    SCOPE_KIND_INSTALLATION,
    SCOPE_KIND_WORKSPACE,
    SCOPE_PATTERN,
    TRACE_ID_PATTERN,
    WORKSPACE_ID_PATTERN,
    ApiError,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ContractDecodeError,
    ContractSemanticError,
    GrantedAuthority,
    MutationPrecondition,
    OperationMetadata,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    compare_contract_versions,
    decode_request,
    encode_request,
    get_operation_metadata,
    validate_operation_request_metadata,
)


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


# --- the application authority seam (R2a) -------------------------------------


class ApplicationAuthorizationError(Exception):
    """A catalogue operation was refused, in the contract's own error vocabulary.

    The code and the retry class are the contract; the message is not. The retry
    class is read from the frozen classification rather than chosen here, so a
    refusal cannot advertise a retry posture the taxonomy does not give it, and an
    unclassified code fails safe as non-retryable.

    The message is always one of the frozen `_MESSAGE_*` constants below -- never a
    string built from a value this seam was handed. A refusal is produced from a
    request the caller controls end to end, and is then rendered into a log line, an
    audit record and a wire `ApiError`; anything interpolated into one is republished
    everywhere those go. Naming the *category* is what a caller needs to act, and it
    is all a caller is owed: which particular scope, purpose, workspace, capability
    or credential-shaped string was involved is either something the caller already
    knows or something about this server it must not learn.

    Deliberately *not* a subclass of :class:`AuthorizationDenied`. The probe boundary
    raises `core.*`-prefixed codes that existing callers already map; a shared base
    class would let a canonical application refusal be caught and re-reported in that
    older vocabulary, which is precisely the drift this seam exists to avoid.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_class = DEFAULT_RETRY_CLASSIFICATION.get(
            code, RETRY_CLASS_NON_RETRYABLE
        )

    def as_api_error(self) -> ApiError:
        """This refusal as the contract's own error DTO."""
        return ApiError(
            code=self.code, message=self.message, retry_class=self.retry_class
        )


# --- refusal messages ---------------------------------------------------------
#
# Every message this seam can produce, frozen here as a constant rather than built at
# the raise site. Two properties follow from that and neither survives an f-string:
# no caller value and no server value can reach a message by accident, and the whole
# set a client may ever be shown is enumerable -- by a reader, and by a test.

_MESSAGE_NO_SESSION: Final = "no authenticated session established this request"
_MESSAGE_MALFORMED_REQUEST: Final = (
    "the request is not a well-formed application request"
)
_MESSAGE_UNCANONICAL_VALUE: Final = (
    "the request states a value outside the contract's value domain for its field"
)
_MESSAGE_LIST_TOO_LONG: Final = (
    "the request states more list entries than the contract permits"
)
_MESSAGE_DEADLINE_OUT_OF_RANGE: Final = (
    "the request states a deadline outside the contract's permitted duration range"
)
_MESSAGE_UNKNOWN_OPERATION: Final = (
    "the requested operation is not in the accepted application catalogue"
)
_MESSAGE_METADATA_NOT_WELL_FORMED: Final = (
    "the request metadata is not well formed for the operation it names"
)
_MESSAGE_UNSUPPORTED_SCOPE_KIND: Final = (
    "the operation declares a scope kind this build cannot apply"
)
_MESSAGE_PRINCIPAL_MISMATCH: Final = (
    "the request claims a principal the session did not authenticate"
)
_MESSAGE_ROLES_NOT_HELD: Final = "the request claims a role the session does not hold"
_MESSAGE_INSTALLATION_NOT_GRANTED: Final = (
    "the session holds no authority over this service instance's installation"
)
_MESSAGE_WORKSPACE_NOT_GRANTED: Final = (
    "the session is not granted the workspace this request names"
)
_MESSAGE_WORKSPACE_ENDPOINT_MISMATCH: Final = (
    "this endpoint serves one workspace only, and the request names another"
)
_MESSAGE_OPERATION_NOT_GRANTED: Final = (
    "the session is not granted the operation this request names"
)
_MESSAGE_SCOPES_NOT_GRANTED: Final = (
    "the request claims a scope the session does not grant"
)
_MESSAGE_REQUIRED_SCOPE_NOT_EFFECTIVE: Final = (
    "the operation requires a scope that is not effective for this request"
)
_MESSAGE_PURPOSE_NOT_ALLOWED: Final = (
    "the session may not act for the purpose this request states"
)
_MESSAGE_CAPABILITY_NOT_GRANTED: Final = (
    "a capability this operation requires is not granted at the required version"
)
_MESSAGE_CAPABILITY_NOT_SUPPORTED: Final = (
    "a capability this operation requires is not supported at the required version"
)
_MESSAGE_SERVER_CAPABILITY_STATE: Final = (
    "this server's declared capability support cannot be applied"
)


# --- the contract's canonical value domains -----------------------------------
#
# Structural decoding proves a value's *type* and stops there: `from_wire` checks that
# a request id is a string, never that it is a `RequestId`. That is a deliberate split
# -- decoding and validation are separate concerns in the contract package -- which
# leaves this seam holding the other half for every value it decides from or hands
# back. The wire patterns are published for exactly this purpose and are used by
# reference, never re-spelled. The `minLength`/`maxLength` bounds beside them in the
# schema are not published, so they are restated below as named constants; a bound is
# load-bearing on its own, since a value can match its pattern for any length at all.

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
"""The shared schema `maxLength` of the bounded scalars this seam reads.

`RequestId`, `CorrelationId`, `TraceId`, `WorkspaceId`, `Identifier`, `Scope`,
`Purpose`, `CapabilityId` and `ReleaseVersion` all carry it.
"""

_CONTRACT_VERSION_MAX_LENGTH: Final = 32
"""`ContractVersion`'s own, shorter, schema `maxLength`."""

_CAPABILITY_ID_MIN_LENGTH: Final = 3
"""`CapabilityId`'s schema `minLength`: a namespace, a dot and a name at minimum."""

_MAX_LIST_ITEMS: Final = 64
"""The schema `maxItems` on `scopes`, `required_capabilities` and `claimed_roles`."""

_MAX_DEADLINE_MS: Final = 86_400_000
"""`DurationMs`'s schema `maximum`: one day, in milliseconds."""


@dataclass(frozen=True)
class _ValueDomain:
    """One canonical scalar domain: its wire pattern and its schema length bounds."""

    pattern: re.Pattern[str]
    maximum_length: int
    minimum_length: int = 1

    def admits(self, value: object) -> bool:
        """Whether `value` is a string this domain accepts.

        Type, length and spelling together, because any one of them alone lets
        something through: a non-string reaches here from a hand-built dataclass, an
        unbounded string matches most of these patterns at any length, and a bounded
        string of the wrong shape is not the value the contract names.

        `fullmatch`, never `match`. Every published pattern ends in `$`, which in
        Python matches immediately before a final newline, so a value spelled with a
        trailing newline would otherwise be accepted here while strict schema
        validation refuses it.
        """
        return (
            isinstance(value, str)
            and self.minimum_length <= len(value) <= self.maximum_length
            and self.pattern.fullmatch(value) is not None
        )


_REQUEST_ID: Final = _ValueDomain(
    re.compile(REQUEST_ID_PATTERN), _BOUNDED_VALUE_MAX_LENGTH
)
_CORRELATION_ID: Final = _ValueDomain(
    re.compile(CORRELATION_ID_PATTERN), _BOUNDED_VALUE_MAX_LENGTH
)
_TRACE_ID: Final = _ValueDomain(re.compile(TRACE_ID_PATTERN), _BOUNDED_VALUE_MAX_LENGTH)
_WORKSPACE_ID: Final = _ValueDomain(
    re.compile(WORKSPACE_ID_PATTERN), _BOUNDED_VALUE_MAX_LENGTH
)
_IDENTIFIER: Final = _ValueDomain(
    re.compile(IDENTIFIER_PATTERN), _BOUNDED_VALUE_MAX_LENGTH
)
_SCOPE: Final = _ValueDomain(re.compile(SCOPE_PATTERN), _BOUNDED_VALUE_MAX_LENGTH)
_PURPOSE: Final = _ValueDomain(re.compile(PURPOSE_PATTERN), _BOUNDED_VALUE_MAX_LENGTH)
_RELEASE_VERSION: Final = _ValueDomain(
    re.compile(RELEASE_VERSION_PATTERN), _BOUNDED_VALUE_MAX_LENGTH
)
_CAPABILITY_ID: Final = _ValueDomain(
    re.compile(CAPABILITY_ID_PATTERN),
    _BOUNDED_VALUE_MAX_LENGTH,
    _CAPABILITY_ID_MIN_LENGTH,
)
_CONTRACT_VERSION: Final = _ValueDomain(
    re.compile(CONTRACT_VERSION_PATTERN), _CONTRACT_VERSION_MAX_LENGTH
)


def _usable_capability(value: object) -> TypeGuard[CapabilityRef]:
    """Whether `value` is a `CapabilityRef` naming a canonical id at a canonical version.

    Both halves or neither. An id outside `CapabilityId` names nothing this contract
    defines, and a version outside `ContractVersion` cannot be compared against a
    floor -- either one leaves a reference that no version decision can be made from.
    """
    return (
        isinstance(value, CapabilityRef)
        and _CAPABILITY_ID.admits(value.id)
        and _CONTRACT_VERSION.admits(value.version)
    )


#: The session's authority collections, each a set of strings once normalized. Named
#: once so construction cannot normalize five of them and forget the sixth.
_AUTHORITY_SETS = (
    "roles",
    "installations",
    "workspaces",
    "operations",
    "scopes",
    "purposes",
)


def _owned_string_set(value: object, field: str) -> frozenset[str]:
    """`value` as a set of strings this session owns, or a construction failure.

    A frozen dataclass freezes the *binding*, never the object bound to it. A caller
    holding the set it passed in can still mutate it after the session refused a
    request, and the identical session then authorizes -- the authority changed with no
    session ever being built. Copying at construction is what makes a grant a fact
    rather than a view onto somebody else's state.

    A bare string is refused rather than iterated. `frozenset("admin")` is five
    single-character grants, which is not what a caller passing `"admin"` meant, and a
    silent conversion is the wrong way to decide that. Members are checked, never
    coerced, for the same reason: `str(...)` of a non-string would invent a grant.

    A shape that is not a set of strings is a `TypeError`, as it would be anywhere else
    in Python; `ValueError` is reserved below for a grant that is the right shape and
    still says something unusable.
    """
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{field} is a single string, not a collection of strings")
    if not isinstance(value, Iterable):
        raise TypeError(f"{field} must be a collection of strings")
    members: list[str] = []
    for member in value:
        if not isinstance(member, str):
            raise TypeError(f"{field} must contain only strings")
        members.append(member)
    return frozenset(members)


def _owned_capabilities(value: object) -> tuple[CapabilityRef, ...]:
    """`value` as a capability grant this session owns, or a construction failure.

    The same ownership rule as :func:`_owned_string_set`, plus the three things a grant
    has to be unambiguous about before anything can be decided from it: every entry is
    a `CapabilityRef`, its id and version are inside the contract's own `CapabilityId`
    and `ContractVersion` domains -- spelling *and* bounds, since a `CapabilityRef` is
    a plain dataclass and holds whatever it was handed -- and no id appears twice.

    All of these are construction-time failures rather than request refusals. The server
    builds its own sessions, so an unusable grant is a bug in that build; resolving one
    by picking a version, or by reading a malformed version as "not granted", would
    silently decide an authority question nothing stands behind. Being a build failure
    is also why these messages may name the offending grant: they are read by whoever
    is assembling this server, and are never rendered into a refusal a caller sees.
    """
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError(
            "capabilities is a single string, not a collection of CapabilityRef"
        )
    if not isinstance(value, Iterable):
        raise TypeError("capabilities must be a collection of CapabilityRef")
    refs: list[CapabilityRef] = []
    for ref in value:
        if not isinstance(ref, CapabilityRef):
            raise TypeError("capabilities must contain only CapabilityRef values")
        if not isinstance(ref.id, str):
            raise TypeError("a granted capability names an id that is not a string")
        if not isinstance(ref.version, str):
            raise TypeError(
                "a granted capability states a version that is not a string"
            )
        # Id first, so the version failure below can quote an id already proved to be
        # one the contract defines.
        if not _CAPABILITY_ID.admits(ref.id):
            raise ValueError(
                "a capability is granted under an id this contract cannot read"
            )
        if not _CONTRACT_VERSION.admits(ref.version):
            raise ValueError(
                f"capability {ref.id!r} is granted at a version this contract cannot read"
            )
        refs.append(ref)

    ids = [ref.id for ref in refs]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"capability grant names {duplicates} more than once; nothing could "
            "say which granted version is in force"
        )
    return tuple(refs)


@dataclass(frozen=True)
class AuthenticatedSession:
    """What the *server* established about a caller, before the request said anything.

    Every field is a server-side fact: the principal authentication resolved to, the
    roles it holds, and the installation, workspace, operation, scope, purpose and
    capability grants attached to it. A request can narrow any of these; nothing in a
    request can add to them.

    There is no bearer credential here, and there must never be one. Authentication
    happens before this value exists and is transport-owned; what survives into
    authorization is the *result* of that check, never the secret that produced it.
    Carrying the credential forward would put it inside every context, log line and
    refusal message this seam produces.

    Every grant defaults to empty. An under-specified session therefore refuses
    everything rather than permitting anything.

    Every grant is also *copied* at construction. The annotations below say what a
    session holds once built; they do not enforce it, and a frozen dataclass does not
    stop a caller mutating a collection it still has a reference to. What makes this a
    server-side fact is the copy, not the annotation.
    """

    principal_id: str
    roles: frozenset[str] = frozenset()
    installations: frozenset[str] = frozenset()
    workspaces: frozenset[str] = frozenset()
    operations: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    purposes: frozenset[str] = frozenset()
    capabilities: tuple[CapabilityRef, ...] = ()

    def __post_init__(self) -> None:
        """Take an owned, checked copy of every grant before the session can be used.

        `object.__setattr__` is how a frozen dataclass normalizes its own fields during
        construction; it is confined to this method, and nothing later in the module
        writes to a session.
        """
        for field in _AUTHORITY_SETS:
            object.__setattr__(
                self, field, _owned_string_set(getattr(self, field), field)
            )
        object.__setattr__(self, "capabilities", _owned_capabilities(self.capabilities))


@dataclass(frozen=True)
class ServiceBinding:
    """What this service instance is bound to, as the server knows it.

    `installation_id` is the installation this instance serves. `workspace_id` is set
    only when the endpoint is workspace-specific -- a per-workspace socket or pipe --
    and left `None` when one endpoint fronts many workspaces. Where it *is* set it is
    a second, independent constraint: reaching a workspace-specific endpoint and then
    naming a different workspace is a refusal even when the session holds a grant for
    the workspace named.
    """

    installation_id: str
    workspace_id: str | None = None


@dataclass(frozen=True)
class AuthorizedApplicationContext:
    """The effective authority for one authorized request, plus the request's own facts.

    Everything here is either a server fact (the session's grants, the binding, the
    frozen catalogue entry) or a request fact the server has already validated. The
    caller's claims appear only where they *narrowed* something: `principal_id` is the
    authenticated principal rather than the claimed one, and `roles` and `scopes` are
    the narrowed sets. No bearer credential reaches this value, because none reaches
    the seam that builds it.

    "Already validated" is the whole of what makes the request facts usable: every one
    of them was held to its own canonical value domain by :func:`_normalized_request`
    before this value was built, so a consumer reading `request_id` for an audit trail
    or `deadline_ms` for a timeout is reading something the contract admits rather than
    whatever a caller handed in.

    `idempotency_key` and `mutation_precondition` are carried through exactly as the
    request stated them. Their presence rules *and their value domains* belong to the
    catalogue validator and have already been applied; this seam neither re-checks nor
    re-shapes them.

    `client` is the caller's self-declared identity. It is here because everything that
    audits or diagnoses one request needs it, and the contract is explicit that it is
    diagnostic and compatibility input only -- it appears in no decision above.
    """

    operation: str
    operation_metadata: OperationMetadata
    principal_id: str
    roles: tuple[str, ...]
    installation_id: str
    workspace_id: str | None
    scopes: tuple[str, ...]
    purpose: str
    capabilities: tuple[CapabilityRef, ...]
    request_id: str
    correlation_id: str
    trace_id: str
    api_version: str
    client: ClientIdentity
    deadline_ms: int | None = None
    idempotency_key: str | None = None
    mutation_precondition: MutationPrecondition | None = None

    @property
    def authority(self) -> GrantedAuthority:
        """This context as the contract's server-produced authority statement.

        Derived rather than stored, so the authority a client is told about cannot
        drift from the authority the request actually ran under.
        """
        return GrantedAuthority(
            principal_id=self.principal_id,
            roles=self.roles,
            capabilities=self.capabilities,
        )


def _invalid_request(message: str) -> ApplicationAuthorizationError:
    return ApplicationAuthorizationError(ERROR_CODE_INVALID_REQUEST, message)


def _denied(message: str) -> ApplicationAuthorizationError:
    return ApplicationAuthorizationError(ERROR_CODE_AUTHORIZATION_DENIED, message)


#: Metadata the contract states as a list, which `RequestMetadata.to_wire` renders with
#: `list()`. A bare string in one of these is not a decode failure but a silent
#: reshaping into a list of its characters, so it has to be caught before the re-decode
#: rather than by it. `principal_claim.claimed_roles` is the third, one level down.
_LIST_VALUED_METADATA = ("scopes", "required_capabilities")


def _require_canonical(value: object, domain: _ValueDomain) -> None:
    """Refuse the request unless `value` is inside `domain`."""
    if not domain.admits(value):
        raise _invalid_request(_MESSAGE_UNCANONICAL_VALUE)


def _require_bounded_list(items: Sequence[object]) -> None:
    """Refuse the request unless `items` is within the contract's `maxItems`.

    Cardinality is part of the value domain too. Every list on this metadata is
    caller-controlled and every one of them is read into a decision or copied into the
    context, so an unbounded list is a caller choosing how much work this seam does and
    how much state it hands on.
    """
    if len(items) > _MAX_LIST_ITEMS:
        raise _invalid_request(_MESSAGE_LIST_TOO_LONG)


def _require_canonical_metadata(metadata: RequestMetadata) -> None:
    """Refuse a request stating any value outside the domain the contract gives it.

    Structural decoding has already run when this is reached, so every field below is
    the right *type*; what is decided here is whether it is the right *value*. The
    distinction is the whole point: `request_id="Bearer hunter2"` and
    `deadline_ms=10**12` and a 4096-character client version are all perfectly typed
    and none of them is a value the contract admits. Left unchecked they become an
    audit identifier, a timeout, and a compatibility fact -- and, before the messages
    above were frozen, a refusal quoting them straight back out.

    Everything the decision reads or the context carries is covered, with two
    deliberate exceptions. `operation` is not pattern-checked because catalogue
    membership is strictly stronger and is checked instead. `idempotency_key` and the
    precondition's `record_version` are not checked because
    `validate_operation_request_metadata` already holds both to `IdempotencyKey` and
    `OpaqueToken`; restating them here would be a second opinion about a rule this seam
    deliberately delegates, and the delegation is what keeps the two from drifting.
    """
    _require_canonical(metadata.request_id, _REQUEST_ID)
    _require_canonical(metadata.correlation_id, _CORRELATION_ID)
    _require_canonical(metadata.trace_id, _TRACE_ID)
    _require_canonical(metadata.api_version, _CONTRACT_VERSION)
    _require_canonical(metadata.purpose, _PURPOSE)
    _require_canonical(metadata.client.id, _IDENTIFIER)
    _require_canonical(metadata.client.version, _RELEASE_VERSION)
    if metadata.workspace_id is not None:
        _require_canonical(metadata.workspace_id, _WORKSPACE_ID)

    _require_bounded_list(metadata.scopes)
    for scope in metadata.scopes:
        _require_canonical(scope, _SCOPE)

    # Every declared requirement, not only the ones marked `required`: an optional
    # declaration is still a value the request states, and one that cannot be read is
    # a malformed request rather than a requirement to quietly skip.
    _require_bounded_list(metadata.required_capabilities)
    for requirement in metadata.required_capabilities:
        _require_canonical(requirement.id, _CAPABILITY_ID)
        _require_canonical(requirement.minimum_version, _CONTRACT_VERSION)

    claim = metadata.principal_claim
    if claim is not None:
        if claim.claimed_principal_id is not None:
            _require_canonical(claim.claimed_principal_id, _IDENTIFIER)
        if claim.claimed_roles is not None:
            _require_bounded_list(claim.claimed_roles)
            for role in claim.claimed_roles:
                _require_canonical(role, _IDENTIFIER)

    # `DurationMs` is a *bounded* non-negative duration and structural decoding
    # enforces neither end. A negative deadline makes every downstream timeout already
    # expired; one past the maximum is a caller asking this server to hold a request
    # open longer than the contract allows anything to be held.
    deadline = metadata.deadline_ms
    if deadline is not None and not 0 <= deadline <= _MAX_DEADLINE_MS:
        raise _invalid_request(_MESSAGE_DEADLINE_OUT_OF_RANGE)


def _normalized_request(request: object) -> RequestEnvelope:
    """The request as the public wire decoder would have produced it, or a refusal.

    A request reaches this seam two ways. Over a transport, `codec.decode_request` has
    already held every field to the contract's structure. In process, a caller builds
    the frozen dataclasses itself and is held to nothing -- `RequestMetadata` is a plain
    dataclass, so an integer request id, a `ClientIdentity` whose version is a float or
    a string where a deadline belongs all construct without complaint and would be
    copied straight into an audit or deadline fact.

    So the whole request goes back through the *same* public decoder before anything
    semantic reads it, and both entry paths end up equally validated. Nothing narrower
    is applied than what the wire accepts: this re-decodes, it does not re-specify.

    Two things sit outside that round trip, and neither is a narrowing either. `to_wire`
    renders the contract's list-valued fields with `list()`, which turns a bare string
    into a list of its characters instead of failing -- a malformed request laundered
    into a well-formed one -- so those are checked first. And decoding is structural by
    design, so it proves types and never value domains;
    :func:`_require_canonical_metadata` applies the patterns and bounds the schema
    states, which is the difference between a request that is *shaped* like the
    contract's and one that *is* one.
    """
    if not isinstance(request, RequestEnvelope):
        raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)
    metadata = request.metadata
    if not isinstance(metadata, RequestMetadata):
        raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)
    for field in _LIST_VALUED_METADATA:
        if isinstance(getattr(metadata, field), (str, bytes, bytearray)):
            raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)
    claim = metadata.principal_claim
    if isinstance(claim, PrincipalClaim) and isinstance(
        claim.claimed_roles, (str, bytes, bytearray)
    ):
        raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)

    # The two halves fail differently and are caught separately. `to_wire` only reads
    # attributes and builds lists and mappings, so an object of the wrong class fails it
    # with `AttributeError` and a non-iterable where the contract states a list fails it
    # with `TypeError`; those are the whole of what the encode can raise once the two
    # types above are established. Everything that survives to be decoded is refused by
    # the decoder's own error.
    #
    # Neither original is chained on. What they say today is narrower than it once read
    # here -- decoding is structural, so it names a field path and a type rather than a
    # value -- but a refusal at this seam does not rest on another package's choice of
    # wording, and `from None` would not be the way to rest on it: it clears `__cause__`
    # and suppresses the *display* of `__context__` while leaving the original, and the
    # frames that produced it, one attribute access away on the error a caller catches.
    # So the sentinel is set inside the handler and the refusal raised after it exits,
    # where there is no exception being handled and neither attribute is set at all.
    wire: dict[str, Any] | None
    try:
        wire = encode_request(request)
    except (AttributeError, TypeError):
        wire = None
    if wire is None:
        raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)

    normalized: RequestEnvelope | None
    try:
        normalized = decode_request(wire)
    except ContractDecodeError:
        normalized = None
    if normalized is None:
        raise _invalid_request(_MESSAGE_MALFORMED_REQUEST)

    _require_canonical_metadata(normalized.metadata)
    return normalized


def _claimed_identity(
    metadata: RequestMetadata,
) -> tuple[str | None, frozenset[str] | None]:
    """The principal and roles a request *claims*, or `None` where it claims nothing.

    `PrincipalClaim` is documented UNTRUSTED, so this only reads it; the structure and
    the value domains it reads were both established by :func:`_normalized_request`,
    which is why there is no second opinion about either here. A claim that is present
    but malformed was already refused there rather than ignored -- silently dropping one
    would let a caller believe it acted as someone it never named.
    """
    claim = metadata.principal_claim
    if claim is None:
        return None, None
    roles = None if claim.claimed_roles is None else frozenset(claim.claimed_roles)
    return claim.claimed_principal_id, roles


def _server_capability_state() -> ApplicationAuthorizationError:
    """The one refusal for unusable server capability state, saying nothing about it.

    Ambiguous or malformed capability state -- this server's declared support, or the
    catalogue entry this build was generated from -- is a fault in this build, not
    something the caller did or could fix, so it is reported in the contract's own
    internal-error vocabulary rather than as a capability the caller was not granted,
    which would be false and would send a client off to ask for a wider grant that
    could never help. Every catalogue operation is permitted to fail this way.

    The message carries no part of the offending state: a caller must not be able to
    read what this server declares out of a refusal.
    """
    return ApplicationAuthorizationError(
        ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_SERVER_CAPABILITY_STATE
    )


def _supported_versions(supported: object) -> dict[str, str]:
    """An owned, checked snapshot of what this server supports: id -> version.

    Taken before the decision and never read from the caller's object again, so a list
    handed in and mutated afterwards cannot change what this request was answered with.

    Every entry is held to the same `CapabilityId` and `ContractVersion` domains a grant
    is, for the same reason: support stated under an id the contract does not define, or
    at a version nothing can compare, is not support this server can act on.

    Every failure is the same refusal, and none of them depends on iteration order. A
    repeated id is refused rather than resolved: scanning for the first match made
    `(0.9, 1.4)` and `(1.4, 0.9)` two different answers to the same question, and there
    is no version that scan could have picked which anything stands behind.
    """
    if isinstance(supported, (str, bytes, bytearray)) or not isinstance(
        supported, Iterable
    ):
        raise _server_capability_state()
    versions: dict[str, str] = {}
    for ref in supported:
        if not _usable_capability(ref):
            raise _server_capability_state()
        if ref.id in versions:
            raise _server_capability_state()
        versions[ref.id] = ref.version
    return versions


def _capability_floors(
    catalogue_entry: OperationMetadata, metadata: RequestMetadata
) -> dict[str, str]:
    """Capability id -> the lowest version that will satisfy this request.

    The catalogue's own requirement always applies. A request may declare further
    `required` capabilities, and may demand a *higher* minimum than the catalogue
    does; both can only narrow what satisfies the request, so both are honoured and
    the strictest floor wins. Nothing here can lower the catalogue's floor.

    Both sides of that comparison arrive already proved canonical, and from different
    directions. The request's declarations were held to `CapabilityId` and
    `ContractVersion` by :func:`_require_canonical_metadata`, so a floor a caller cannot
    state was refused as a malformed request before any authority question was asked.
    The catalogue's own requirement is server state and is checked here as such: a build
    whose frozen entry names an unreadable id or minimum cannot answer a capability
    question at all, and saying so is not something to report as the caller's mistake.
    """
    required = catalogue_entry.required_capability
    if not _CAPABILITY_ID.admits(required.id) or not _CONTRACT_VERSION.admits(
        required.minimum_version
    ):
        raise _server_capability_state()

    requirements: list[CapabilityRequirement] = [required]
    requirements.extend(
        requirement
        for requirement in metadata.required_capabilities
        if requirement.required is True
    )

    floors: dict[str, str] = {}
    for requirement in requirements:
        minimum = requirement.minimum_version
        current = floors.get(requirement.id)
        if current is None or compare_contract_versions(minimum, current) > 0:
            floors[requirement.id] = minimum
    return floors


def _lower(left: str, right: str) -> str:
    """The weaker of two versions already known to parse."""
    return left if compare_contract_versions(left, right) <= 0 else right


def authorize_application_request(
    request: RequestEnvelope,
    *,
    session: AuthenticatedSession | None,
    binding: ServiceBinding,
    supported_capabilities: tuple[CapabilityRef, ...],
) -> AuthorizedApplicationContext:
    """Authorize one catalogue operation, or refuse it in the contract's vocabulary.

    The catalogue drives the decision. Which scope kind an operation has, which scope
    it requires, which capability at which minimum version, and which metadata a
    request must carry are all read from the frozen `OPERATION_CATALOGUE` entry and
    checked by `validate_operation_request_metadata`; none of that policy is restated
    here, so this seam cannot drift from what A2 froze.

    What is decided here is the part a request cannot establish about itself:

    1. that a server-authenticated session exists at all;
    2. that this server's own declared capability support is usable at all;
    3. that the request is the request the contract describes -- in structure, and in
       every value domain the contract states for the fields it carries;
    4. that the named operation is one the catalogue defines;
    5. that the request metadata is well formed for it;
    6. that the claimed principal is the authenticated one, and the claimed roles are
       a subset of the held roles -- claims narrow, never add;
    7. that an installation-scoped operation runs under authority over the bound
       installation;
    8. that a workspace-scoped operation names a granted workspace, and that a
       workspace-specific endpoint was not used to reach a different workspace;
    9. that the operation is on the session's allowlist;
    10. that every claimed scope is granted, and the catalogue's required scope is
        effective;
    11. that the stated purpose is one the session may act for;
    12. that every required capability is granted *and* supported at the floor.

    Order matters and is deliberate. Authentication precedes everything, because
    nothing else is answerable without a principal. The server checks its own state
    next: whether it can honour a capability at all is not a question about the caller,
    and answering a request from state this build cannot read would be worse than
    refusing. Normalization, catalogue resolution and metadata validation then precede
    every authority question, so a malformed request is reported as malformed rather
    than as a permissions problem a caller might respond to by asking for a wider
    grant. Identity precedes scope, and scope precedes the allowlist, capabilities and
    purpose, so the narrowest true statement about the refusal is the one that surfaces.

    Refusal messages name a category and carry no value at all -- not the caller's, not
    the catalogue's, and not this server's. Each is one of the frozen `_MESSAGE_*`
    constants, so no token, request id, idempotency key, precondition record version,
    scope, purpose, workspace id, capability id or version, raw exception text,
    filesystem path, SQL, lease or fencing value can reach one, by construction rather
    than by inspection of each raise site. Refusals from the catalogue validator and
    from the wire decoder are dropped rather than wrapped for the same reason: their
    messages quote the very values a refusal must not repeat.

    Dropped means dropped. Each is caught, recorded in a sentinel, and answered with
    this seam's own error *after* the handler has exited, so the original is attached
    as neither `__cause__` nor `__context__`. `raise ... from None` inside the handler
    is not the same thing and was what stood here: it clears `__cause__` and sets
    `__suppress_context__`, which quiets a rendered traceback while leaving the
    original exception -- with the value it quoted -- reachable on `__context__` for
    anything that logs, serializes or reports the error a caller catches.
    """
    # 1. A session is the precondition for every other question.
    if session is None:
        raise ApplicationAuthorizationError(
            ERROR_CODE_AUTHENTICATION_REQUIRED, _MESSAGE_NO_SESSION
        )

    # 2. This server's own support, snapshotted and checked before it is relied on, so
    # the decision below reads a value nothing outside this call can still change.
    supported = _supported_versions(supported_capabilities)

    # 3. The request, put back through the public wire decoder and then held to the
    # contract's own value domains, so an in-process caller is held to exactly what a
    # transport caller is and neither is held only to a shape.
    request = _normalized_request(request)

    # 4. Catalogue resolution. The contract's own refusal quotes the operation the
    # caller named, so it is dropped rather than chained -- see the note in step 5.
    catalogue_entry: OperationMetadata | None
    try:
        catalogue_entry = get_operation_metadata(request.operation)
    except ContractSemanticError:
        catalogue_entry = None
    if catalogue_entry is None:
        raise _invalid_request(_MESSAGE_UNKNOWN_OPERATION)

    # 5. Metadata agreement, delegated whole. Workspace-id agreement with the scope
    # kind, the required scopes, the required-capability declaration and the
    # idempotency/precondition presence rules and value domains all belong to the
    # catalogue validator, and are not restated or tightened here.
    #
    # The validator quotes the offending value -- a malformed idempotency key or
    # record version arrives here already embedded in the exception's own message --
    # so nothing of it is carried on. The sentinel is set inside the handler and the
    # refusal raised after it exits: `from None` would clear `__cause__` and silence
    # the rendering of `__context__` while leaving the quoted value one attribute
    # access away on the error a caller catches.
    well_formed = True
    try:
        validate_operation_request_metadata(catalogue_entry.name, request.metadata)
    except ContractSemanticError:
        well_formed = False
    if not well_formed:
        raise _invalid_request(_MESSAGE_METADATA_NOT_WELL_FORMED)
    metadata = request.metadata

    # 6. Identity. The claim can only narrow.
    claimed_principal, claimed_roles = _claimed_identity(metadata)
    if claimed_principal is not None and claimed_principal != session.principal_id:
        raise _denied(_MESSAGE_PRINCIPAL_MISMATCH)
    if claimed_roles is None:
        effective_roles = session.roles
    else:
        if not claimed_roles <= session.roles:
            raise _denied(_MESSAGE_ROLES_NOT_HELD)
        effective_roles = claimed_roles

    # 7/8. Scope kind. A kind outside the frozen pair fails closed; the validator
    # already refuses one, and this is the second lock rather than a second rule.
    scope_kind = catalogue_entry.scope.scope_kind
    workspace_id: str | None
    if scope_kind == SCOPE_KIND_INSTALLATION:
        # An installation-scoped operation carrying a workspace_id was already
        # refused in step 5, so authority over the bound installation is all that
        # remains to decide.
        if binding.installation_id not in session.installations:
            raise _denied(_MESSAGE_INSTALLATION_NOT_GRANTED)
        workspace_id = None
    elif scope_kind == SCOPE_KIND_WORKSPACE:
        selected = metadata.workspace_id
        if selected not in session.workspaces:
            raise ApplicationAuthorizationError(
                ERROR_CODE_WORKSPACE_NOT_GRANTED, _MESSAGE_WORKSPACE_NOT_GRANTED
            )
        if binding.workspace_id is not None and selected != binding.workspace_id:
            raise ApplicationAuthorizationError(
                ERROR_CODE_WORKSPACE_NOT_GRANTED, _MESSAGE_WORKSPACE_ENDPOINT_MISMATCH
            )
        workspace_id = selected
    else:
        raise _invalid_request(_MESSAGE_UNSUPPORTED_SCOPE_KIND)

    # 9. The operation allowlist.
    if catalogue_entry.name not in session.operations:
        raise _denied(_MESSAGE_OPERATION_NOT_GRANTED)

    # 10. Scopes. A claimed scope the session does not hold is a denial, not something
    # to quietly drop: the effective set is the claim, so dropping one would let a
    # request run under an authority it believed it had stated.
    effective_scopes = frozenset(metadata.scopes)
    if not effective_scopes <= session.scopes:
        raise _denied(_MESSAGE_SCOPES_NOT_GRANTED)
    # The validator proved the catalogue's required scopes are *claimed*; this proves
    # they are *effective*. Implied today by the check above, and stated anyway: it is
    # the load-bearing invariant, and it must not become conditional on how the
    # effective set happens to be computed.
    if not frozenset(catalogue_entry.scope.required_scopes) <= effective_scopes:
        raise _denied(_MESSAGE_REQUIRED_SCOPE_NOT_EFFECTIVE)

    # 11. Purpose.
    purpose = metadata.purpose
    if purpose not in session.purposes:
        raise ApplicationAuthorizationError(
            ERROR_CODE_INVALID_PURPOSE, _MESSAGE_PURPOSE_NOT_ALLOWED
        )

    # 12. Capabilities. Both halves are required: a grant the server cannot honour is
    # not a capability, and server support the session was never granted is not one
    # either. Absent is "does not reach the floor", so an id neither side names fails
    # closed. The effective version is the weaker of the two, because that is the
    # version the request can actually rely on. Every id and version compared or
    # emitted here was proved canonical by the session, by the snapshot, by the
    # catalogue check, or by the request normalization.
    granted_versions = {ref.id: ref.version for ref in session.capabilities}
    effective_capabilities: list[CapabilityRef] = []
    for capability_id, floor in sorted(
        _capability_floors(catalogue_entry, metadata).items()
    ):
        granted = granted_versions.get(capability_id)
        if granted is None or compare_contract_versions(granted, floor) < 0:
            raise ApplicationAuthorizationError(
                ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_CAPABILITY_NOT_GRANTED
            )
        server = supported.get(capability_id)
        if server is None or compare_contract_versions(server, floor) < 0:
            raise ApplicationAuthorizationError(
                ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_CAPABILITY_NOT_SUPPORTED
            )
        effective_capabilities.append(
            CapabilityRef(id=capability_id, version=_lower(granted, server))
        )

    # 13. Effective authority, built only from what the server established, what the
    # request narrowed, the binding, and what this server supports. The principal is
    # the session's, never the claim's.
    return AuthorizedApplicationContext(
        operation=catalogue_entry.name,
        operation_metadata=catalogue_entry,
        principal_id=session.principal_id,
        roles=tuple(sorted(effective_roles)),
        installation_id=binding.installation_id,
        workspace_id=workspace_id,
        scopes=tuple(sorted(effective_scopes)),
        purpose=purpose,
        capabilities=tuple(effective_capabilities),
        request_id=metadata.request_id,
        correlation_id=metadata.correlation_id,
        trace_id=metadata.trace_id,
        api_version=metadata.api_version,
        client=metadata.client,
        deadline_ms=metadata.deadline_ms,
        idempotency_key=metadata.idempotency_key,
        mutation_precondition=metadata.mutation_precondition,
    )


__all__ = [
    "ApplicationAuthorizationError",
    "AuthenticatedSession",
    "AuthorizationDenied",
    "AuthorizedApplicationContext",
    "Grant",
    "ServiceBinding",
    "authorize",
    "authorize_application_request",
]
