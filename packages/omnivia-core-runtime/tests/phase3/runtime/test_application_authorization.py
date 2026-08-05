"""R2a: the authenticated application authority seam.

Every one of the twenty accepted catalogue operations goes through this file, and
the properties proved are the ones a later slice could silently break: that a claim
can only ever narrow what the server already granted, that installation and
workspace scope rules and the endpoint binding are all enforced, that capability
version floors are checked against *both* the session's grants and this server's
support, that refusals use the frozen error and retry taxonomy, that the metadata
presence rules stay delegated to the catalogue validator rather than restated here,
and that no secret or raw exception reaches a refusal message.

Three of those properties are about what the seam *holds*, not what it decides, and
each was a real defect before it was a test. A session copies every grant it is given,
so a caller mutating the collection it passed in cannot widen a session that already
refused it. This server's declared capability support is snapshotted and checked once,
so it cannot mean two different things depending on the order it happens to be in.
And an in-process caller who builds the request dataclasses by hand is held to the
same public decoder a transport caller goes through, so a structurally invalid fact
cannot be copied into an audit or deadline fact instead of being refused.

Two further properties are about the gap decoding leaves open at either end of the
seam. Structural decoding proves a value's *type* and deliberately stops there, so a
capability id spelled `BAD`, a version 33 characters long, a deadline of a year and a
client version that is not a release version are all perfectly typed and none of them
is a value the contract admits; each is held here to the pattern and the bounds the
schema states. And at the other end, every refusal names a category and carries no
value at all -- a caller's or this server's -- so a credential pasted into any field
of a request cannot come back out through a message, a `str()` or an `ApiError`.
"""

from __future__ import annotations

import ast
import inspect
import re
import traceback

import pytest
from omnivia_core_runtime.service import authorization
from omnivia_core_runtime.service.authorization import (
    ApplicationAuthorizationError,
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    OPERATION_CATALOGUE,
    RETRY_CLASS_NON_RETRYABLE,
    SCOPE_KIND_INSTALLATION,
    SCOPE_KIND_WORKSPACE,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ContractDecodeError,
    ContractSemanticError,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    decode_request,
    encode_request,
    get_operation_metadata,
    validate_operation_error,
)

CATALOGUE = tuple(OPERATION_CATALOGUE)
INSTALLATION_ENTRIES = tuple(
    entry for entry in CATALOGUE if entry.scope.scope_kind == SCOPE_KIND_INSTALLATION
)
WORKSPACE_ENTRIES = tuple(
    entry for entry in CATALOGUE if entry.scope.scope_kind == SCOPE_KIND_WORKSPACE
)

PRINCIPAL = "principal-1"
INSTALLATION_ID = "inst-0001"
WORKSPACE_ID = "ws-0001"
OTHER_WORKSPACE_ID = "ws-0002"
PURPOSE = "operations.read"
ROLES = frozenset({"reader", "writer"})
BINDING = ServiceBinding(installation_id=INSTALLATION_ID)
CLIENT = ClientIdentity(id="test-client", version="0.1.0")

#: The session's authority collections, in the order the seam normalizes them.
AUTHORITY_SETS = (
    "roles",
    "installations",
    "workspaces",
    "operations",
    "scopes",
    "purposes",
)

#: A key and a record version that must never appear in a refusal message. Distinct
#: literals so a leak is attributable to the exact value that leaked.
SECRET_IDEMPOTENCY_KEY = "idem-do-not-echo-9f2c"
SECRET_RECORD_VERSION = "rv-do-not-echo-4a71"

#: The schema bounds this seam restates, named once here so a test asserts against the
#: contract's number rather than against whatever the module happens to hold.
BOUNDED_VALUE_MAX_LENGTH = 128
CONTRACT_VERSION_MAX_LENGTH = 32
MAX_LIST_ITEMS = 64
MAX_DEADLINE_MS = 86_400_000

#: Every catalogue capability, supported by this notional server *above* the
#: catalogue floor, so the effective version in the common case is the session's
#: grant rather than the server's support.
SUPPORTED = tuple(
    CapabilityRef(id=capability_id, version="1.4")
    for capability_id in sorted({entry.required_capability.id for entry in CATALOGUE})
)

_DEFAULT = object()


def catalogue_requirement(entry):
    """The capability declaration the catalogue requires a request to carry."""
    required = entry.required_capability
    return CapabilityRequirement(
        id=required.id, minimum_version=required.minimum_version, required=True
    )


def metadata_for(entry, **overrides):
    """A request metadata the catalogue validator accepts for `entry`.

    Built from the catalogue entry itself -- scope kind, required scopes, required
    capability and the idempotency/precondition postures -- so a test never encodes
    a second opinion about what a well-formed request for an operation looks like.
    """
    workspace_id = overrides.pop("workspace_id", _DEFAULT)
    if workspace_id is _DEFAULT:
        workspace_id = (
            WORKSPACE_ID if entry.scope.scope_kind == SCOPE_KIND_WORKSPACE else None
        )
    fields = {
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "trace_id": "trace-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": workspace_id,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": PURPOSE,
        "required_capabilities": (catalogue_requirement(entry),),
        "idempotency_key": (
            SECRET_IDEMPOTENCY_KEY
            if entry.idempotency.supports_idempotency_key
            else None
        ),
        "mutation_precondition": (
            MutationPrecondition(record_version=SECRET_RECORD_VERSION)
            if entry.precondition.supports_mutation_precondition
            else None
        ),
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestMetadata(**fields)


def envelope_for(entry, **overrides):
    operation = overrides.pop("operation", entry.name)
    return RequestEnvelope(
        operation=operation, metadata=metadata_for(entry, **overrides), input={}
    )


def session_for(entry, **overrides):
    """A session granting exactly what `entry` needs, and nothing more by accident."""
    required = entry.required_capability
    fields = {
        "principal_id": PRINCIPAL,
        "roles": ROLES,
        "installations": frozenset({INSTALLATION_ID}),
        "workspaces": frozenset({WORKSPACE_ID}),
        "operations": frozenset({entry.name}),
        "scopes": frozenset(entry.scope.required_scopes),
        "purposes": frozenset({PURPOSE}),
        "capabilities": (
            CapabilityRef(id=required.id, version=required.minimum_version),
        ),
    }
    fields.update(overrides)
    return AuthenticatedSession(**fields)


def authorize(
    entry, *, session=_DEFAULT, binding=BINDING, supported=SUPPORTED, **overrides
):
    if session is _DEFAULT:
        session = session_for(entry)
    return authorize_application_request(
        envelope_for(entry, **overrides),
        session=session,
        binding=binding,
        supported_capabilities=supported,
    )


def refusal(entry, **kwargs) -> ApplicationAuthorizationError:
    """Authorize, requiring a refusal, and hand back the error itself."""
    with pytest.raises(ApplicationAuthorizationError) as raised:
        authorize(entry, **kwargs)
    return raised.value


def by_name(name):
    return next(entry for entry in CATALOGUE if entry.name == name)


def assert_not_echoed(error, *values) -> None:
    """Assert that no `value` reaches any rendering of `error`.

    All three renderings, because they are three ways to the same string and code
    downstream reaches for whichever is nearest: `.message` is what a log line takes,
    `str(error)` is what a bare `except` prints, and `as_api_error().message` is the
    one that crosses the wire.

    An empty probe is skipped. `"" in anything` is true, so asserting on one would say
    nothing about the message and everything about Python; the refusal assertion beside
    each of these is what proves the empty value was rejected.
    """
    rendered = (error.message, str(error), error.as_api_error().message)
    for value in values:
        probe = str(value).strip()
        if not probe:
            continue
        for rendering in rendered:
            assert probe not in rendering, (error.code, probe)


ALL = pytest.mark.parametrize(
    "entry", CATALOGUE, ids=[entry.name for entry in CATALOGUE]
)
INSTALLATION = pytest.mark.parametrize(
    "entry", INSTALLATION_ENTRIES, ids=[entry.name for entry in INSTALLATION_ENTRIES]
)
WORKSPACE = pytest.mark.parametrize(
    "entry", WORKSPACE_ENTRIES, ids=[entry.name for entry in WORKSPACE_ENTRIES]
)

# A workspace-scoped operation with a mutation posture, and a read-only one, named
# once so the single-case tests below stay readable.
MUTATION = "knowledge.propose"
READ = "memory.get"

READ_ENTRY = by_name(READ)
INSTALLATION_ENTRY = INSTALLATION_ENTRIES[0]


# --- the whole catalogue authorizes -------------------------------------------


def test_the_catalogue_under_test_is_the_whole_accepted_catalogue() -> None:
    assert len(CATALOGUE) == 20
    assert len(INSTALLATION_ENTRIES) + len(WORKSPACE_ENTRIES) == len(CATALOGUE)


@ALL
def test_every_catalogue_operation_authorizes_under_a_sufficient_session(entry) -> None:
    context = authorize(entry)

    assert isinstance(context, AuthorizedApplicationContext)
    assert context.operation == entry.name
    assert context.operation_metadata is entry
    assert context.principal_id == PRINCIPAL
    assert context.roles == tuple(sorted(ROLES))
    assert context.installation_id == INSTALLATION_ID
    assert context.scopes == tuple(sorted(entry.scope.required_scopes))
    assert context.purpose == PURPOSE
    # The grant is at the catalogue floor and this server supports a higher version,
    # so the effective version is the weaker of the two: what was granted.
    assert context.capabilities == (
        CapabilityRef(
            id=entry.required_capability.id,
            version=entry.required_capability.minimum_version,
        ),
    )
    assert context.authority.principal_id == PRINCIPAL
    assert context.authority.capabilities == context.capabilities


# --- installation and workspace scope rules, and the endpoint binding ---------


@INSTALLATION
def test_installation_operations_carry_no_workspace(entry) -> None:
    context = authorize(entry)
    assert context.workspace_id is None


@INSTALLATION
def test_installation_operations_require_authority_over_the_bound_installation(
    entry,
) -> None:
    error = refusal(entry, session=session_for(entry, installations=frozenset()))
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED

    # Holding *an* installation is not holding *this* one.
    error = refusal(
        entry, session=session_for(entry, installations=frozenset({"inst-9999"}))
    )
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


@INSTALLATION
def test_installation_operations_refuse_a_selected_workspace(entry) -> None:
    """Delegated to the catalogue validator, and therefore an `invalid_request`.

    An installation-scoped operation has no use for a workspace selection, so one is
    a malformed request rather than a permissions problem.
    """
    error = refusal(entry, workspace_id=WORKSPACE_ID)
    assert error.code == ERROR_CODE_INVALID_REQUEST


@WORKSPACE
def test_workspace_operations_require_a_granted_workspace(entry) -> None:
    context = authorize(entry)
    assert context.workspace_id == WORKSPACE_ID

    error = refusal(entry, session=session_for(entry, workspaces=frozenset()))
    assert error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED

    elsewhere = session_for(entry, workspaces=frozenset({OTHER_WORKSPACE_ID}))
    assert refusal(entry, session=elsewhere).code == ERROR_CODE_WORKSPACE_NOT_GRANTED


@WORKSPACE
def test_workspace_specific_endpoints_require_exact_agreement(entry) -> None:
    bound = ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID)
    assert authorize(entry, binding=bound).workspace_id == WORKSPACE_ID

    # Granted the workspace it names, but reached through an endpoint that fronts a
    # different one: the binding is an independent constraint, not a fallback.
    elsewhere = ServiceBinding(
        installation_id=INSTALLATION_ID, workspace_id=OTHER_WORKSPACE_ID
    )
    session = session_for(
        entry, workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
    )
    error = refusal(entry, binding=elsewhere, session=session)
    assert error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED


@WORKSPACE
def test_an_unbound_endpoint_serves_any_granted_workspace(entry) -> None:
    session = session_for(
        entry, workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
    )
    context = authorize(entry, session=session, workspace_id=OTHER_WORKSPACE_ID)
    assert context.workspace_id == OTHER_WORKSPACE_ID


# --- authentication, principal, roles, allowlist ------------------------------


@ALL
def test_an_unauthenticated_request_is_refused(entry) -> None:
    error = refusal(entry, session=None)
    assert error.code == ERROR_CODE_AUTHENTICATION_REQUIRED


def test_authentication_is_decided_before_anything_else() -> None:
    """A request that is *also* malformed still reports the missing session.

    Order is the property: reporting the malformation first would tell an
    unauthenticated caller which requests are well formed.
    """
    entry = by_name(READ)
    error = refusal(
        entry,
        session=None,
        operation="not.a.catalogue.operation",
        purpose="never.allowed",
        scopes=(),
    )
    assert error.code == ERROR_CODE_AUTHENTICATION_REQUIRED


@ALL
def test_a_matching_principal_claim_is_accepted_and_a_mismatched_one_denied(
    entry,
) -> None:
    claim = PrincipalClaim(claimed_principal_id=PRINCIPAL)
    assert authorize(entry, principal_claim=claim).principal_id == PRINCIPAL

    imposter = PrincipalClaim(claimed_principal_id="principal-2")
    error = refusal(entry, principal_claim=imposter)
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


@ALL
def test_claimed_roles_may_narrow_but_never_widen(entry) -> None:
    narrowed = PrincipalClaim(claimed_roles=("reader",))
    assert authorize(entry, principal_claim=narrowed).roles == ("reader",)

    # Narrowing all the way to no roles is a claim, not an error.
    assert (
        authorize(entry, principal_claim=PrincipalClaim(claimed_roles=())).roles == ()
    )

    widened = PrincipalClaim(claimed_roles=("reader", "admin"))
    error = refusal(entry, principal_claim=widened)
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


@ALL
def test_the_operation_allowlist_is_required(entry) -> None:
    error = refusal(entry, session=session_for(entry, operations=frozenset()))
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED

    # Every *other* catalogue operation granted is still not this one.
    others = frozenset(other.name for other in CATALOGUE) - {entry.name}
    error = refusal(entry, session=session_for(entry, operations=others))
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_an_unknown_operation_is_an_invalid_request() -> None:
    entry = by_name(READ)
    error = refusal(entry, operation="not.a.catalogue.operation")
    assert error.code == ERROR_CODE_INVALID_REQUEST

    # Including the service-lifecycle probes, which are a separate contract.
    assert refusal(entry, operation="core.health").code == ERROR_CODE_INVALID_REQUEST


def test_the_catalogues_refusal_of_an_unknown_operation_is_not_chained_on() -> None:
    """The catalogue quotes the operation it was handed; this seam does not.

    An operation name is caller-supplied, so a client that puts a credential there --
    by templating one in, or by sending the wrong field -- must not have it read back
    out of the refusal. `raise ... from None` would keep it out of a printed traceback
    and leave `ContractSemanticError`, with the name embedded, on `__context__`.
    """
    spoofed = f"memory.{SECRET_CREDENTIAL}"
    with pytest.raises(ContractSemanticError) as nested:
        get_operation_metadata(spoofed)
    assert spoofed in str(nested.value)

    error = refusal(by_name(READ), operation=spoofed)
    assert error.code == ERROR_CODE_INVALID_REQUEST
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for surface in (str(error), error.message, repr(error.args), rendered):
        assert spoofed not in surface
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_malformed_principal_claim_is_an_invalid_request() -> None:
    entry = by_name(READ)
    assert (
        refusal(entry, principal_claim=PrincipalClaim(claimed_principal_id=17)).code
        == ERROR_CODE_INVALID_REQUEST
    )
    assert (
        refusal(entry, principal_claim=PrincipalClaim(claimed_roles="reader")).code
        == ERROR_CODE_INVALID_REQUEST
    )
    assert (
        refusal(entry, principal_claim=PrincipalClaim(claimed_roles=("reader", 3))).code
        == ERROR_CODE_INVALID_REQUEST
    )
    assert (
        refusal(entry, principal_claim="principal-1").code == ERROR_CODE_INVALID_REQUEST
    )


# --- scopes and purpose --------------------------------------------------------


@ALL
def test_a_claimed_scope_the_session_does_not_grant_is_denied(entry) -> None:
    claimed = (*entry.scope.required_scopes, "admin:everything")
    error = refusal(entry, scopes=claimed)
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


@ALL
def test_the_catalogue_required_scope_must_be_granted_not_merely_claimed(entry) -> None:
    """Claiming the required scope is not holding it.

    The catalogue validator already refuses a request that does not *claim* the
    required scope; what this proves is the authorization half -- a claim the session
    never granted does not make the scope effective.
    """
    error = refusal(
        entry, session=session_for(entry, scopes=frozenset({"unrelated:scope"}))
    )
    assert error.code == ERROR_CODE_AUTHORIZATION_DENIED


@ALL
def test_extra_granted_scopes_do_not_reach_effective_authority(entry) -> None:
    generous = session_for(
        entry,
        scopes=frozenset({*entry.scope.required_scopes, "memory:write", "job:control"}),
    )
    context = authorize(entry, session=generous)
    assert context.scopes == tuple(sorted(entry.scope.required_scopes))


@ALL
def test_the_purpose_must_be_one_the_session_allows(entry) -> None:
    error = refusal(entry, purpose="unapproved.purpose")
    assert error.code == ERROR_CODE_INVALID_PURPOSE

    error = refusal(entry, session=session_for(entry, purposes=frozenset()))
    assert error.code == ERROR_CODE_INVALID_PURPOSE

    allowed = session_for(entry, purposes=frozenset({PURPOSE, "audit.review"}))
    assert (
        authorize(entry, session=allowed, purpose="audit.review").purpose
        == "audit.review"
    )


# --- capabilities: grants and server support, both versioned -------------------


@ALL
def test_a_capability_the_session_was_never_granted_is_refused(entry) -> None:
    error = refusal(entry, session=session_for(entry, capabilities=()))
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


@ALL
def test_a_grant_below_the_catalogue_floor_is_refused(entry) -> None:
    stale = (CapabilityRef(id=entry.required_capability.id, version="0.9"),)
    error = refusal(entry, session=session_for(entry, capabilities=stale))
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


@ALL
def test_a_capability_this_server_does_not_support_is_refused(entry) -> None:
    error = refusal(entry, supported=())
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED

    below = (CapabilityRef(id=entry.required_capability.id, version="0.9"),)
    error = refusal(entry, supported=below)
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


@ALL
def test_a_request_may_raise_the_floor_above_the_catalogue_minimum(entry) -> None:
    """A stricter declared minimum narrows what satisfies the request.

    The catalogue validator accepts the stricter declaration; this seam then holds
    the grant and the server support to it rather than to the catalogue's floor.
    """
    required = entry.required_capability
    stricter = (
        CapabilityRequirement(id=required.id, minimum_version="1.3", required=True),
    )
    error = refusal(entry, required_capabilities=stricter)
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED

    generous = session_for(
        entry, capabilities=(CapabilityRef(id=required.id, version="1.3"),)
    )
    context = authorize(entry, session=generous, required_capabilities=stricter)
    assert context.capabilities == (CapabilityRef(id=required.id, version="1.3"),)


@ALL
def test_the_effective_version_is_the_weaker_of_grant_and_server_support(entry) -> None:
    required = entry.required_capability
    generous = session_for(
        entry, capabilities=(CapabilityRef(id=required.id, version="2.0"),)
    )
    modest = (CapabilityRef(id=required.id, version="1.4"),)

    context = authorize(entry, session=generous, supported=modest)
    assert context.capabilities == (CapabilityRef(id=required.id, version="1.4"),)


def test_an_extra_required_capability_must_also_be_granted_and_supported() -> None:
    entry = by_name(READ)
    required = entry.required_capability
    declared = (
        catalogue_requirement(entry),
        CapabilityRequirement(id="graph.read", minimum_version="1.0", required=True),
    )

    error = refusal(entry, required_capabilities=declared)
    assert error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED

    session = session_for(
        entry,
        capabilities=(
            CapabilityRef(id=required.id, version=required.minimum_version),
            CapabilityRef(id="graph.read", version="1.1"),
        ),
    )
    context = authorize(entry, session=session, required_capabilities=declared)
    assert context.capabilities == (
        CapabilityRef(id="graph.read", version="1.1"),
        CapabilityRef(id=required.id, version=required.minimum_version),
    )


def test_an_optional_extra_capability_is_neither_required_nor_effective() -> None:
    entry = by_name(READ)
    required = entry.required_capability
    declared = (
        catalogue_requirement(entry),
        CapabilityRequirement(id="graph.read", minimum_version="9.0", required=False),
    )

    context = authorize(entry, required_capabilities=declared)
    assert context.capabilities == (
        CapabilityRef(id=required.id, version=required.minimum_version),
    )


def test_a_capability_grant_naming_one_id_twice_fails_at_construction() -> None:
    """Ambiguous server state is a build bug, not a request refusal."""
    with pytest.raises(ValueError, match="more than once"):
        AuthenticatedSession(
            principal_id=PRINCIPAL,
            capabilities=(
                CapabilityRef(id="memory.read", version="1.0"),
                CapabilityRef(id="memory.read", version="2.0"),
            ),
        )


def test_an_unusable_granted_version_fails_at_construction() -> None:
    """A version nothing can compare is as unusable a grant as a duplicated id.

    Reading it as "not granted" at request time was fail-closed but wrong in kind: it
    turned a build that cannot state its own grants into a refusal aimed at the caller,
    who has nothing to change. The session is server-built, so this is a build failure.
    """
    with pytest.raises(ValueError, match="cannot read"):
        session_for(
            READ_ENTRY,
            capabilities=(
                CapabilityRef(id=READ_ENTRY.required_capability.id, version="latest"),
            ),
        )


def test_an_unusable_declared_minimum_is_an_invalid_request() -> None:
    entry = by_name(READ)
    declared = (
        catalogue_requirement(entry),
        CapabilityRequirement(id="graph.read", minimum_version="latest", required=True),
    )
    assert (
        refusal(entry, required_capabilities=declared).code
        == ERROR_CODE_INVALID_REQUEST
    )


# --- a session owns its grants: no caller can widen one after the fact ---------
#
# `@dataclass(frozen=True)` freezes the *binding*, never the object bound to it. Before
# the copy below existed, a caller could hand in a mutable set, be refused, add to that
# same set, and have the identical session authorize the identical request -- authority
# widened with no session ever rebuilt and nothing to audit.

#: One authority collection at a time: the operation it gates, what the session holds
#: to start with, what a caller would add to its own collection to widen the session,
#: the refusal that must survive that mutation, and any request the attack needs.
#: `roles` is reached through a claim, since holding fewer roles is not itself a
#: refusal -- claiming one the session does not hold is.
ALIAS_ATTACKS = (
    ("operations", READ_ENTRY, set(), READ, ERROR_CODE_AUTHORIZATION_DENIED, {}),
    (
        "workspaces",
        READ_ENTRY,
        set(),
        WORKSPACE_ID,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
        {},
    ),
    (
        "scopes",
        READ_ENTRY,
        set(),
        READ_ENTRY.scope.required_scopes[0],
        ERROR_CODE_AUTHORIZATION_DENIED,
        {},
    ),
    ("purposes", READ_ENTRY, set(), PURPOSE, ERROR_CODE_INVALID_PURPOSE, {}),
    (
        "installations",
        INSTALLATION_ENTRY,
        set(),
        INSTALLATION_ID,
        ERROR_CODE_AUTHORIZATION_DENIED,
        {},
    ),
    (
        "roles",
        READ_ENTRY,
        {"reader"},
        "admin",
        ERROR_CODE_AUTHORIZATION_DENIED,
        {"principal_claim": PrincipalClaim(claimed_roles=("admin",))},
    ),
)


@pytest.mark.parametrize(
    ("field", "entry", "held", "addition", "expected", "request_overrides"),
    ALIAS_ATTACKS,
    ids=[attack[0] for attack in ALIAS_ATTACKS],
)
def test_mutating_a_supplied_authority_set_cannot_widen_a_session(
    field, entry, held, addition, expected, request_overrides
) -> None:
    supplied = set(held)
    session = session_for(entry, **{field: supplied})
    assert refusal(entry, session=session, **request_overrides).code == expected

    supplied.add(addition)

    assert getattr(session, field) == frozenset(held)
    assert addition not in getattr(session, field)
    assert refusal(entry, session=session, **request_overrides).code == expected


def test_mutating_a_supplied_capability_list_cannot_widen_a_session() -> None:
    """The same attack, through the one grant that is a sequence rather than a set."""
    required = READ_ENTRY.required_capability
    supplied = [CapabilityRef(id=required.id, version="0.9")]
    session = session_for(READ_ENTRY, capabilities=supplied)

    assert (
        refusal(READ_ENTRY, session=session).code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    )

    supplied.append(CapabilityRef(id="graph.read", version="9.9"))
    supplied[0] = CapabilityRef(id=required.id, version="9.9")

    assert session.capabilities == (CapabilityRef(id=required.id, version="0.9"),)
    assert (
        refusal(READ_ENTRY, session=session).code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    )


@pytest.mark.parametrize("field", AUTHORITY_SETS)
def test_every_authority_set_becomes_an_owned_frozenset(field) -> None:
    supplied = {"granted-value"}
    session = session_for(READ_ENTRY, **{field: supplied})

    held = getattr(session, field)
    assert isinstance(held, frozenset)
    assert held == frozenset({"granted-value"})

    supplied.add("added-after-the-fact")
    assert getattr(session, field) == frozenset({"granted-value"})


def test_a_supplied_capability_sequence_becomes_an_owned_tuple() -> None:
    required = READ_ENTRY.required_capability
    supplied = [CapabilityRef(id=required.id, version=required.minimum_version)]
    session = session_for(READ_ENTRY, capabilities=supplied)

    assert isinstance(session.capabilities, tuple)
    assert session.capabilities == tuple(supplied)


@pytest.mark.parametrize("field", AUTHORITY_SETS)
@pytest.mark.parametrize(
    "value",
    [
        "admin",
        b"admin",
        bytearray(b"admin"),
        17,
        None,
        ("reader", 3),
        ("reader", True),
        (None,),
    ],
    ids=[
        "str",
        "bytes",
        "bytearray",
        "int",
        "none",
        "int-member",
        "bool-member",
        "none-member",
    ],
)
def test_an_authority_set_must_be_a_collection_of_strings(field, value) -> None:
    """A string is not a set of strings, and a non-string is not a grant.

    `frozenset("admin")` is five single-character grants rather than one; coercing a
    member with `str(...)` would invent a grant that was never made. Both are refused
    at construction rather than silently converted.
    """
    with pytest.raises(TypeError):
        session_for(READ_ENTRY, **{field: value})


#: Capability grants a session must refuse to be built from, and what kind of failure
#: each one is: the wrong shape is a `TypeError`, the right shape saying something
#: unusable is a `ValueError`.
UNUSABLE_GRANTS = (
    ("str", "memory.read", TypeError),
    ("bytes", b"memory.read", TypeError),
    ("int", 17, TypeError),
    ("str-member", ("memory.read",), TypeError),
    (
        "requirement-member",
        (
            CapabilityRequirement(
                id="memory.read", minimum_version="1.0", required=True
            ),
        ),
        TypeError,
    ),
    ("non-str-id", (CapabilityRef(id=17, version="1.0"),), TypeError),
    ("non-str-version", (CapabilityRef(id="memory.read", version=1.0),), TypeError),
    (
        "unparseable-version",
        (CapabilityRef(id="memory.read", version="latest"),),
        ValueError,
    ),
    ("empty-version", (CapabilityRef(id="memory.read", version=""),), ValueError),
    (
        "noncanonical-version",
        (CapabilityRef(id="memory.read", version="01.0"),),
        ValueError,
    ),
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(value, expected) for _, value, expected in UNUSABLE_GRANTS],
    ids=[label for label, _, _ in UNUSABLE_GRANTS],
)
def test_a_capability_grant_must_be_usable_capability_refs(value, expected) -> None:
    with pytest.raises(expected):
        session_for(READ_ENTRY, capabilities=value)


# --- this server's declared capability support ---------------------------------


def test_duplicate_server_capability_support_is_refused_in_either_order() -> None:
    """The same ambiguous support must not mean two different things.

    Scanning for the first entry with a matching id made this order-dependent: listed
    low-then-high the request was refused, listed high-then-low the identical request
    authorized. There is no version that scan could have picked which anything stands
    behind, so a repeated id is refused rather than resolved.
    """
    required = READ_ENTRY.required_capability
    low = CapabilityRef(id=required.id, version="0.9")
    high = CapabilityRef(id=required.id, version="1.4")

    ascending = refusal(READ_ENTRY, supported=(low, high))
    descending = refusal(READ_ENTRY, supported=(high, low))

    assert ascending.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert descending.code == ascending.code
    assert descending.message == ascending.message


@pytest.mark.parametrize(
    "supported",
    [
        "memory.read",
        b"memory.read",
        17,
        ("memory.read",),
        (
            CapabilityRequirement(
                id="memory.read", minimum_version="1.0", required=True
            ),
        ),
        (CapabilityRef(id=17, version="1.4"),),
        (CapabilityRef(id="memory.read", version=1.4),),
        (CapabilityRef(id="memory.read", version="latest"),),
    ],
    ids=[
        "str",
        "bytes",
        "int",
        "str-member",
        "requirement-member",
        "non-str-id",
        "non-str-version",
        "unparseable-version",
    ],
)
def test_unusable_server_capability_support_is_refused_the_same_way(supported) -> None:
    """Every unusable shape produces one refusal, and it discloses no server state."""
    error = refusal(READ_ENTRY, supported=supported)
    assert error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert "memory.read" not in error.message
    assert "latest" not in error.message


def test_server_support_is_snapshotted_before_the_decision() -> None:
    """A list mutated afterwards cannot change what this request was answered with."""
    required = READ_ENTRY.required_capability
    supported = [CapabilityRef(id=required.id, version="1.4")]

    context = authorize(READ_ENTRY, supported=supported)
    assert context.capabilities == (
        CapabilityRef(id=required.id, version=required.minimum_version),
    )

    supported.clear()
    assert context.capabilities == (
        CapabilityRef(id=required.id, version=required.minimum_version),
    )

    # And the mutation is honoured on the *next* request rather than remembered from
    # this one: the snapshot is per-decision, not a cache.
    assert (
        refusal(READ_ENTRY, supported=supported).code
        == ERROR_CODE_CAPABILITY_NOT_GRANTED
    )


def test_server_support_below_the_floor_is_still_a_capability_refusal() -> None:
    """Usable-but-insufficient support is the caller's problem, not a build fault."""
    below = (CapabilityRef(id=READ_ENTRY.required_capability.id, version="0.9"),)
    assert (
        refusal(READ_ENTRY, supported=below).code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    )
    assert refusal(READ_ENTRY, supported=()).code == ERROR_CODE_CAPABILITY_NOT_GRANTED


# --- a direct request is held to the public wire decoder ------------------------
#
# `RequestMetadata` is a plain frozen dataclass, so an in-process caller can build one
# holding an integer request id or a `ClientIdentity` whose version is a float. Over a
# transport `codec.decode_request` would have refused all of it; built directly, it used
# to be copied straight into an audit or deadline fact.

#: Direct metadata the public wire decoder itself refuses, with the fact each one
#: corrupts. Every value here is distinctive enough that a leak into a refusal message
#: would be attributable.
MALFORMED_DIRECT_FACTS = (
    ("request_id", {"request_id": 170001}),
    ("correlation_id", {"correlation_id": 170002}),
    ("trace_id", {"trace_id": 170003}),
    ("api_version", {"api_version": 170004}),
    ("client", {"client": 170005}),
    ("client_id", {"client": ClientIdentity(id=170006, version="0.1.0")}),
    ("client_version", {"client": ClientIdentity(id="test-client", version=170007)}),
    ("purpose", {"purpose": 170008}),
    ("workspace_id", {"workspace_id": 170009}),
    ("deadline_str", {"deadline_ms": "170010"}),
    ("deadline_bool", {"deadline_ms": True}),
    ("deadline_float", {"deadline_ms": 170011.5}),
    ("scope_member", {"scopes": ("memory:read", 170012)}),
    (
        "claimed_principal",
        {"principal_claim": PrincipalClaim(claimed_principal_id=170013)},
    ),
    (
        "claimed_role_member",
        {"principal_claim": PrincipalClaim(claimed_roles=("reader", 170014))},
    ),
    ("principal_claim", {"principal_claim": "principal-1"}),
    ("mutation_precondition", {"mutation_precondition": "rv-170015"}),
    ("required_capabilities_member", {"required_capabilities": ("memory.read",)}),
)


def wire_decoder_refuses(envelope) -> bool:
    """True when the public codec cannot round-trip this envelope through its wire form.

    Both halves count. A wrongly typed value fails the decode with the codec's own
    error; an object of the wrong class entirely fails the encode before it ever gets
    there. Either way the public path will not produce this request.
    """
    try:
        decode_request(encode_request(envelope))
    except (ContractDecodeError, AttributeError, TypeError):
        return True
    return False


def refuse_envelope(envelope) -> ApplicationAuthorizationError:
    """Authorize `envelope` under a session sufficient for `READ`, requiring a refusal."""
    with pytest.raises(ApplicationAuthorizationError) as raised:
        authorize_application_request(
            envelope,
            session=session_for(READ_ENTRY),
            binding=BINDING,
            supported_capabilities=SUPPORTED,
        )
    return raised.value


@pytest.mark.parametrize(
    "overrides",
    [overrides for _, overrides in MALFORMED_DIRECT_FACTS],
    ids=[label for label, _ in MALFORMED_DIRECT_FACTS],
)
def test_a_malformed_direct_fact_is_refused_exactly_as_the_wire_decoder_refuses_it(
    overrides,
) -> None:
    envelope = envelope_for(READ_ENTRY, **overrides)
    assert wire_decoder_refuses(envelope)

    error = refuse_envelope(envelope)
    assert error.code == ERROR_CODE_INVALID_REQUEST
    # Neither the offending value nor the decoder's own message reaches the refusal --
    # and the decoder's exception is not reachable *from* the refusal either. Both
    # attributes are asserted `None` rather than tolerated-and-suppressed:
    # `raise ... from None` would satisfy the second half by hiding the exception from
    # a printed traceback, and leave it on `__context__` for everything else.
    assert "170" not in error.message
    assert error.__cause__ is None
    assert error.__context__ is None


def test_metadata_that_is_not_request_metadata_is_an_invalid_request() -> None:
    """The one malformed fact that is not a field: the metadata object itself."""
    envelope = RequestEnvelope(operation=READ, metadata="metadata-170016", input={})
    assert wire_decoder_refuses(envelope)

    error = refuse_envelope(envelope)
    assert error.code == ERROR_CODE_INVALID_REQUEST
    assert "170" not in error.message


@pytest.mark.parametrize(
    "overrides",
    [
        {"scopes": "memory:read"},
        {"principal_claim": PrincipalClaim(claimed_roles="reader")},
    ],
    ids=["scopes", "claimed_roles"],
)
def test_a_single_value_where_the_contract_states_a_list_is_refused(overrides) -> None:
    """The two fields a wire round trip would launder rather than refuse.

    `to_wire` renders the contract's list-valued fields with `list()`, which turns
    `"memory:read"` into eleven single-character scopes and re-decodes as a well-formed
    list. A round trip alone would therefore accept a malformed request and answer it as
    if the caller had asked for something else entirely, so these are caught first.
    """
    envelope = envelope_for(READ_ENTRY, **overrides)
    assert not wire_decoder_refuses(
        envelope
    )  # exactly why the round trip is not enough

    assert refuse_envelope(envelope).code == ERROR_CODE_INVALID_REQUEST


def test_a_negative_deadline_is_refused_and_a_valid_one_reaches_the_context() -> None:
    """`DurationMs` is a bounded non-negative duration, which decoding does not enforce.

    Structural decoding proves only that a deadline is an integer, so a negative one
    arrives intact and would become the deadline fact every downstream timeout is
    computed from.
    """
    assert refusal(READ_ENTRY, deadline_ms=-1).code == ERROR_CODE_INVALID_REQUEST
    assert refusal(READ_ENTRY, deadline_ms=-170017).code == ERROR_CODE_INVALID_REQUEST

    assert authorize(READ_ENTRY, deadline_ms=0).deadline_ms == 0
    assert authorize(READ_ENTRY, deadline_ms=30_000).deadline_ms == 30_000
    assert authorize(READ_ENTRY).deadline_ms is None


@ALL
def test_the_client_identity_reaches_the_context_unchanged(entry) -> None:
    context = authorize(entry)
    assert context.client == CLIENT

    other = ClientIdentity(id="omnivia.desktop", version="2.10.3-rc.1+build.7")
    assert authorize(entry, client=other).client == other


def test_a_valid_request_is_not_reshaped_by_being_revalidated() -> None:
    """Re-decoding must not become a second, narrower opinion about a valid request."""
    entry = by_name(MUTATION)
    context = authorize(
        entry,
        idempotency_key="a.b:c-d_e",
        mutation_precondition=MutationPrecondition(record_version="!~<>|"),
        deadline_ms=1,
        principal_claim=PrincipalClaim(
            claimed_principal_id=PRINCIPAL, claimed_roles=("reader",)
        ),
    )

    assert context.idempotency_key == "a.b:c-d_e"
    assert context.mutation_precondition == MutationPrecondition(record_version="!~<>|")
    assert context.deadline_ms == 1
    assert context.roles == ("reader",)
    assert context.client == CLIENT
    assert context.scopes == tuple(sorted(entry.scope.required_scopes))


# --- claims never widen effective authority ------------------------------------


@ALL
def test_claims_never_widen_effective_authority(entry) -> None:
    """Everything a caller can assert is checked against, not added to, the session."""
    session = session_for(entry)
    context = authorize(
        entry,
        session=session,
        principal_claim=PrincipalClaim(
            claimed_principal_id=PRINCIPAL, claimed_roles=("reader",)
        ),
    )

    assert context.principal_id == session.principal_id
    assert set(context.roles) <= session.roles
    assert set(context.scopes) <= session.scopes
    granted_versions = {ref.id: ref.version for ref in session.capabilities}
    for ref in context.capabilities:
        assert ref.id in granted_versions
        assert ref.version == granted_versions[ref.id]
    # The authority statement handed back to a client says the same thing.
    assert context.authority.roles == context.roles
    assert context.authority.principal_id == session.principal_id

    # Provenance, not merely equality. A valid claim is *required* to equal the
    # authenticated principal, so equality alone cannot tell a context built from the
    # session apart from one built from the claim -- identity can, and the difference
    # is what stops the claim becoming the source the moment anything upstream relaxes.
    echoed = "".join(PRINCIPAL)  # the same characters, deliberately not the same object
    assert echoed == PRINCIPAL and echoed is not PRINCIPAL
    context = authorize(
        entry,
        session=session,
        principal_claim=PrincipalClaim(claimed_principal_id=echoed),
    )
    assert context.principal_id is session.principal_id


def test_neither_the_session_nor_the_context_carries_a_bearer_credential() -> None:
    forbidden = ("token", "secret", "credential", "password", "bearer", "auth_header")
    for holder in (AuthenticatedSession, AuthorizedApplicationContext, ServiceBinding):
        names = tuple(holder.__dataclass_fields__)
        assert not [
            name for name in names if any(word in name for word in forbidden)
        ], names


# --- the frozen error and retry taxonomy ---------------------------------------

CANONICAL_CODES = frozenset(
    {
        ERROR_CODE_AUTHENTICATION_REQUIRED,
        ERROR_CODE_AUTHORIZATION_DENIED,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
        ERROR_CODE_INVALID_PURPOSE,
        ERROR_CODE_INVALID_REQUEST,
        ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    }
)


def refusals_for(entry):
    """One refusal of each kind this seam can produce for `entry`."""
    empty = frozenset()
    errors = [
        refusal(entry, session=None),
        refusal(
            entry, principal_claim=PrincipalClaim(claimed_principal_id="principal-2")
        ),
        refusal(entry, session=session_for(entry, operations=empty)),
        refusal(entry, purpose="unapproved.purpose"),
        refusal(entry, session=session_for(entry, capabilities=())),
        refusal(entry, operation="not.a.catalogue.operation"),
        refusal(entry, supported=(*SUPPORTED, SUPPORTED[0])),
        refusal(entry, client=170018),
    ]
    if entry.scope.scope_kind == SCOPE_KIND_WORKSPACE:
        errors.append(refusal(entry, session=session_for(entry, workspaces=empty)))
    else:
        errors.append(refusal(entry, session=session_for(entry, installations=empty)))
    return errors


@ALL
def test_every_refusal_uses_the_frozen_error_and_retry_taxonomy(entry) -> None:
    for error in refusals_for(entry):
        assert error.code in CANONICAL_CODES
        assert error.retry_class == DEFAULT_RETRY_CLASSIFICATION[error.code]
        assert error.retry_class == RETRY_CLASS_NON_RETRYABLE

        api_error = error.as_api_error()
        assert (api_error.code, api_error.retry_class) == (
            error.code,
            error.retry_class,
        )
        assert api_error.message == error.message


@ALL
def test_every_refusal_is_an_error_the_operation_is_allowed_to_raise(entry) -> None:
    """A code outside the operation's `allowed_errors` would be unreportable.

    `workspace_not_granted` is not in an installation-scoped operation's allow-list,
    and this seam structurally cannot raise it for one -- the workspace branch is
    never entered.
    """
    for error in refusals_for(entry):
        validate_operation_error(entry.name, error.as_api_error())


@INSTALLATION
def test_installation_operations_never_raise_workspace_not_granted(entry) -> None:
    assert ERROR_CODE_WORKSPACE_NOT_GRANTED not in entry.allowed_errors
    for error in refusals_for(entry):
        assert error.code != ERROR_CODE_WORKSPACE_NOT_GRANTED


@ALL
def test_no_secret_or_raw_exception_reaches_a_refusal_message(entry) -> None:
    """Refusals say why, never with what.

    The catalogue validator quotes the offending value in its own message, so a
    malformed idempotency key or precondition record version arrives at this boundary
    already embedded in an exception. Re-raising rather than wrapping is what keeps it
    from being republished -- these are the two cases where a leak is not theoretical.
    """
    leaky = [refusal(entry, scopes=("not:the:required:scope",))]
    if entry.idempotency.supports_idempotency_key:
        # A space is outside `IdempotencyKey`'s frozen pattern, so the validator
        # refuses this key -- with the key itself in the refusal.
        leaky.append(
            refusal(entry, idempotency_key=f"bad key {SECRET_IDEMPOTENCY_KEY}")
        )
    if entry.precondition.supports_mutation_precondition:
        broken = MutationPrecondition(
            record_version=f"bad version {SECRET_RECORD_VERSION}"
        )
        leaky.append(refusal(entry, mutation_precondition=broken))
    assert all(error.code == ERROR_CODE_INVALID_REQUEST for error in leaky)

    for error in [*leaky, *refusals_for(entry)]:
        assert SECRET_IDEMPOTENCY_KEY not in error.message
        assert SECRET_RECORD_VERSION not in error.message
        assert error.__cause__ is None
        # Nor on `__context__`. The validator's exception quotes the key it refused,
        # so leaving it attached would republish it to anything that logs or
        # serializes the refusal -- which `raise ... from None` does, since it only
        # suppresses the *rendering* of a chained exception.
        assert error.__context__ is None


# --- presence semantics stay delegated, and stay untightened -------------------


@ALL
def test_metadata_presence_requirements_remain_delegated_to_the_validator(
    entry,
) -> None:
    if entry.idempotency.required:
        assert refusal(entry, idempotency_key=None).code == ERROR_CODE_INVALID_REQUEST
    if not entry.idempotency.supports_idempotency_key:
        assert (
            refusal(entry, idempotency_key="idem-1").code == ERROR_CODE_INVALID_REQUEST
        )

    precondition = MutationPrecondition(record_version="rv-1")
    if entry.precondition.required:
        assert (
            refusal(entry, mutation_precondition=None).code
            == ERROR_CODE_INVALID_REQUEST
        )
    if not entry.precondition.supports_mutation_precondition:
        assert (
            refusal(entry, mutation_precondition=precondition).code
            == ERROR_CODE_INVALID_REQUEST
        )


@ALL
def test_idempotency_and_precondition_formats_are_not_tightened(entry) -> None:
    """Anything the frozen value domains accept still authorizes, unchanged.

    A single character, a maximum-length key and a record version of pure punctuation
    are all valid under the frozen patterns. Narrowing them here would refuse
    requests the accepted contract permits.
    """
    if entry.idempotency.supports_idempotency_key:
        for key in ("A", "9" * 128, "a.b:c-d_e"):
            assert authorize(entry, idempotency_key=key).idempotency_key == key

    if entry.precondition.supports_mutation_precondition:
        for version in ("!", "~", "!~<>|", "v" * 512):
            precondition = MutationPrecondition(record_version=version)
            context = authorize(entry, mutation_precondition=precondition)
            assert context.mutation_precondition == precondition


@ALL
def test_request_facts_reach_the_context_unchanged(entry) -> None:
    context = authorize(entry)
    metadata = metadata_for(entry)
    assert context.request_id == metadata.request_id
    assert context.correlation_id == metadata.correlation_id
    assert context.trace_id == metadata.trace_id
    assert context.api_version == metadata.api_version
    assert context.client == metadata.client
    assert context.deadline_ms == metadata.deadline_ms
    assert context.idempotency_key == metadata.idempotency_key
    assert context.mutation_precondition == metadata.mutation_precondition


# --- the contract's value domains, not merely its types ------------------------
#
# `from_wire` checks that a capability id is a string and stops; the pattern and the
# bounds beside it in the schema are a separate concern the contract package leaves to
# whoever decides from the value. Every value below is perfectly typed and outside the
# domain its field names -- which is the whole distance between a request that is
# *shaped* like the contract's and one that *is* one.

#: The longest `CapabilityId` the schema admits, and one character more. Both match
#: `CAPABILITY_ID_PATTERN`, so only the bound tells them apart.
CAPABILITY_ID_AT_LIMIT = "memory." + "a" * 121
CAPABILITY_ID_OVER_LIMIT = "memory." + "a" * 122

#: The same pair for `ContractVersion`, whose own bound is 32 rather than 128. Both
#: parse as `major.minor`; a version parser alone would accept either.
CONTRACT_VERSION_AT_LIMIT = "1." + "9" * 30
CONTRACT_VERSION_OVER_LIMIT = "1." + "9" * 31

#: And for `ReleaseVersion`, which a client identity states.
RELEASE_VERSION_AT_LIMIT = "1.0.0+" + "b" * 122
RELEASE_VERSION_OVER_LIMIT = "1.0.0+" + "b" * 124

#: Ids the contract does not define, with what is wrong with each. `BAD` is the plain
#: case: uppercase is outside the domain, and nothing but the pattern says so.
UNCANONICAL_CAPABILITY_IDS = (
    ("bad", "BAD"),
    ("upper-namespace", "Memory.read"),
    ("no-namespace", "memory"),
    ("empty-segment", "memory..read"),
    ("leading-dot", ".memory.read"),
    ("trailing-dot", "memory.read."),
    ("space", "memory read"),
    ("empty", ""),
    ("below-minimum-length", "a."),
    ("trailing-newline", "memory.read\n"),
    ("overlength", CAPABILITY_ID_OVER_LIMIT),
)

#: Versions nothing can compare, or can compare but must not accept.
UNCANONICAL_CONTRACT_VERSIONS = (
    ("bad", "BAD"),
    ("unparseable", "latest"),
    ("empty", ""),
    ("noncanonical", "01.0"),
    ("three-part", "1.0.0"),
    ("space", "1. 0"),
    ("trailing-newline", "1.0\n"),
    ("overlength", CONTRACT_VERSION_OVER_LIMIT),
)

CAPABILITY_IDS = pytest.mark.parametrize(
    "capability_id",
    [value for _, value in UNCANONICAL_CAPABILITY_IDS],
    ids=[label for label, _ in UNCANONICAL_CAPABILITY_IDS],
)
CONTRACT_VERSIONS = pytest.mark.parametrize(
    "version",
    [value for _, value in UNCANONICAL_CONTRACT_VERSIONS],
    ids=[label for label, _ in UNCANONICAL_CONTRACT_VERSIONS],
)


def test_the_frozen_catalogue_states_canonical_capability_values() -> None:
    """The premise of the seam's server-state check, asserted rather than assumed.

    The seam refuses to answer a capability question from a catalogue entry it cannot
    read. That branch is only ever dead code while this holds, and it is exactly the
    kind of thing that stops holding quietly when the catalogue is regenerated.
    """
    for entry in CATALOGUE:
        required = entry.required_capability
        assert authorization._CAPABILITY_ID.admits(required.id), entry.name
        assert authorization._CONTRACT_VERSION.admits(required.minimum_version), (
            entry.name
        )


@CAPABILITY_IDS
def test_a_grant_naming_an_uncanonical_capability_id_fails_at_construction(
    capability_id,
) -> None:
    """A grant under an id the contract does not define grants nothing.

    Type-checking the id was never enough: `BAD` and `memory` and a 129-character id
    are all strings, and a session built from one would go on to compare versions
    against an identifier no catalogue entry, and no server declaration, can name.
    """
    with pytest.raises(ValueError, match="cannot read"):
        session_for(
            READ_ENTRY, capabilities=(CapabilityRef(id=capability_id, version="1.0"),)
        )


@CONTRACT_VERSIONS
def test_a_grant_at_an_uncanonical_version_fails_at_construction(version) -> None:
    required = READ_ENTRY.required_capability
    with pytest.raises(ValueError, match="cannot read"):
        session_for(
            READ_ENTRY, capabilities=(CapabilityRef(id=required.id, version=version),)
        )


@CAPABILITY_IDS
def test_server_support_naming_an_uncanonical_capability_id_is_server_state(
    capability_id,
) -> None:
    """Unusable support is this build's fault, and says nothing about itself."""
    supported = (*SUPPORTED, CapabilityRef(id=capability_id, version="1.4"))
    error = refusal(READ_ENTRY, supported=supported)

    assert error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert_not_echoed(error, capability_id)


@CONTRACT_VERSIONS
def test_server_support_at_an_uncanonical_version_is_server_state(version) -> None:
    supported = (*SUPPORTED, CapabilityRef(id="graph.write", version=version))
    error = refusal(READ_ENTRY, supported=supported)

    assert error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert_not_echoed(error, version)


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
@CAPABILITY_IDS
def test_a_request_declaring_an_uncanonical_capability_id_is_an_invalid_request(
    capability_id, required
) -> None:
    """Every declaration is checked, not only the ones the request marks `required`.

    An optional declaration is still a value the request states. Reading an unusable
    one as "not required after all" would let a caller put anything it liked in a
    field this seam then carries past, which is not a narrowing the contract offers.
    """
    declared = (
        catalogue_requirement(READ_ENTRY),
        CapabilityRequirement(
            id=capability_id, minimum_version="1.0", required=required
        ),
    )
    error = refusal(READ_ENTRY, required_capabilities=declared)

    assert error.code == ERROR_CODE_INVALID_REQUEST
    assert_not_echoed(error, capability_id)


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
@CONTRACT_VERSIONS
def test_a_request_declaring_an_uncanonical_minimum_version_is_an_invalid_request(
    version, required
) -> None:
    declared = (
        catalogue_requirement(READ_ENTRY),
        CapabilityRequirement(
            id="graph.read", minimum_version=version, required=required
        ),
    )
    error = refusal(READ_ENTRY, required_capabilities=declared)

    assert error.code == ERROR_CODE_INVALID_REQUEST
    assert_not_echoed(error, version)


def test_capability_values_at_the_maximum_length_are_still_accepted() -> None:
    """The bound is a maximum, not a narrowing.

    Refusing the longest admissible id or version would reject requests the accepted
    contract permits, which is the mirror-image defect of accepting overlength ones.
    """
    required = READ_ENTRY.required_capability
    at_limit = CapabilityRef(
        id=CAPABILITY_ID_AT_LIMIT, version=CONTRACT_VERSION_AT_LIMIT
    )
    declared = (
        catalogue_requirement(READ_ENTRY),
        CapabilityRequirement(
            id=CAPABILITY_ID_AT_LIMIT,
            minimum_version=CONTRACT_VERSION_AT_LIMIT,
            required=True,
        ),
    )
    session = session_for(
        READ_ENTRY,
        capabilities=(
            CapabilityRef(id=required.id, version=required.minimum_version),
            at_limit,
        ),
    )

    context = authorize(
        READ_ENTRY,
        session=session,
        supported=(*SUPPORTED, at_limit),
        required_capabilities=declared,
    )

    assert len(CAPABILITY_ID_AT_LIMIT) == BOUNDED_VALUE_MAX_LENGTH
    assert len(CONTRACT_VERSION_AT_LIMIT) == CONTRACT_VERSION_MAX_LENGTH
    assert at_limit in context.capabilities


# --- the request's own scalar domains ------------------------------------------

#: Client identities outside `Identifier` / `ReleaseVersion`. The identity is diagnostic
#: input the contract says is never an authorization input, and it is copied into the
#: context verbatim -- which is precisely why it has to be a value the contract admits
#: before it gets there.
UNCANONICAL_CLIENTS = (
    ("id-empty", ClientIdentity(id="", version="0.1.0")),
    ("id-leading-dash", ClientIdentity(id="-omnivia.desktop", version="0.1.0")),
    ("id-space", ClientIdentity(id="omnivia desktop", version="0.1.0")),
    ("id-slash", ClientIdentity(id="omnivia/desktop", version="0.1.0")),
    ("id-newline", ClientIdentity(id="omnivia.desktop\n", version="0.1.0")),
    ("id-overlength", ClientIdentity(id="c" * 129, version="0.1.0")),
    ("version-two-part", ClientIdentity(id="omnivia.desktop", version="1.0")),
    ("version-v-prefixed", ClientIdentity(id="omnivia.desktop", version="v1.0.0")),
    ("version-empty", ClientIdentity(id="omnivia.desktop", version="")),
    ("version-space", ClientIdentity(id="omnivia.desktop", version="1.0.0 rc1")),
    ("version-newline", ClientIdentity(id="omnivia.desktop", version="1.0.0\n")),
    (
        "version-overlength",
        ClientIdentity(id="omnivia.desktop", version=RELEASE_VERSION_OVER_LIMIT),
    ),
)

#: Every other request scalar the decision reads or the context carries, malformed and
#: overlength. Each value is distinctive enough that a leak would be attributable.
UNCANONICAL_REQUEST_VALUES = (
    ("request_id-space", {"request_id": "req 1"}),
    ("request_id-empty", {"request_id": ""}),
    ("request_id-overlength", {"request_id": "r" * 129}),
    ("correlation_id-slash", {"correlation_id": "corr/1"}),
    ("correlation_id-overlength", {"correlation_id": "c" * 129}),
    ("trace_id-newline", {"trace_id": "trace-1\n"}),
    ("trace_id-overlength", {"trace_id": "t" * 129}),
    ("api_version-unparseable", {"api_version": "latest"}),
    ("api_version-noncanonical", {"api_version": "01.2"}),
    ("api_version-overlength", {"api_version": CONTRACT_VERSION_OVER_LIMIT}),
    ("workspace_id-space", {"workspace_id": "ws 0001"}),
    ("workspace_id-overlength", {"workspace_id": "w" * 129}),
    ("purpose-upper", {"purpose": "Operations.Read"}),
    ("purpose-overlength", {"purpose": "ops." + "r" * 125}),
    ("scope-upper", {"scopes": ("memory:read", "Admin:Everything")}),
    ("scope-overlength", {"scopes": ("memory:read", "memory:" + "r" * 122)}),
    (
        "claimed_principal-space",
        {"principal_claim": PrincipalClaim(claimed_principal_id="principal 1")},
    ),
    (
        "claimed_principal-overlength",
        {"principal_claim": PrincipalClaim(claimed_principal_id="p" * 129)},
    ),
    (
        "claimed_role-space",
        {"principal_claim": PrincipalClaim(claimed_roles=("reader", "read er"))},
    ),
    (
        "claimed_role-overlength",
        {"principal_claim": PrincipalClaim(claimed_roles=("r" * 129,))},
    ),
)


@pytest.mark.parametrize(
    "client",
    [value for _, value in UNCANONICAL_CLIENTS],
    ids=[label for label, _ in UNCANONICAL_CLIENTS],
)
def test_an_uncanonical_client_identity_is_an_invalid_request(client) -> None:
    error = refusal(READ_ENTRY, client=client)

    assert error.code == ERROR_CODE_INVALID_REQUEST
    assert_not_echoed(error, client.id, client.version)


def test_a_client_identity_at_the_maximum_lengths_is_accepted() -> None:
    client = ClientIdentity(id="c" * 128, version=RELEASE_VERSION_AT_LIMIT)
    assert len(client.id) == BOUNDED_VALUE_MAX_LENGTH
    assert len(client.version) == BOUNDED_VALUE_MAX_LENGTH

    assert authorize(READ_ENTRY, client=client).client == client


@pytest.mark.parametrize(
    "overrides",
    [value for _, value in UNCANONICAL_REQUEST_VALUES],
    ids=[label for label, _ in UNCANONICAL_REQUEST_VALUES],
)
def test_an_uncanonical_request_value_is_refused_and_never_echoed(overrides) -> None:
    """A well-typed value outside its own domain is a malformed request, not a fact.

    Before this, each of these decoded cleanly and was copied straight into the audit,
    compatibility or deadline fact the context hands on -- or, for the claim fields,
    was compared against a grant it could never have matched and reported back with
    the offending value inside the refusal.
    """
    error = refusal(READ_ENTRY, **overrides)
    assert error.code == ERROR_CODE_INVALID_REQUEST
    assert_not_echoed(error, *overrides.values())


def test_a_deadline_outside_the_contract_range_is_refused() -> None:
    """`DurationMs` is bounded at both ends, and decoding enforces neither.

    Below zero every downstream timeout is already expired. Above the maximum a caller
    is asking this server to hold a request open longer than the contract allows
    anything to be held -- a year, or a millennium, arrives as a plain integer.
    """
    for deadline in (-1, -170017, -86_400_001, 86_400_001, 170_000_000, 10**12):
        error = refusal(READ_ENTRY, deadline_ms=deadline)
        assert error.code == ERROR_CODE_INVALID_REQUEST
        assert_not_echoed(error, deadline)


def test_a_deadline_at_either_end_of_the_range_is_accepted() -> None:
    """The bounds are inclusive; narrowing them would refuse a conforming request."""
    assert authorize(READ_ENTRY, deadline_ms=0).deadline_ms == 0
    assert (
        authorize(READ_ENTRY, deadline_ms=MAX_DEADLINE_MS).deadline_ms
        == MAX_DEADLINE_MS
    )


def test_a_list_longer_than_the_contract_permits_is_refused() -> None:
    """`maxItems` is part of the domain too, on every list a caller controls."""
    entry = READ_ENTRY
    scopes = tuple(f"extra:scope_{index}" for index in range(MAX_LIST_ITEMS + 1))
    roles = tuple(f"role-{index}" for index in range(MAX_LIST_ITEMS + 1))
    requirements = (
        catalogue_requirement(entry),
        *(
            CapabilityRequirement(
                id=f"extra.cap_{index}", minimum_version="1.0", required=False
            )
            for index in range(MAX_LIST_ITEMS)
        ),
    )

    assert refusal(entry, scopes=scopes).code == ERROR_CODE_INVALID_REQUEST
    assert (
        refusal(entry, principal_claim=PrincipalClaim(claimed_roles=roles)).code
        == ERROR_CODE_INVALID_REQUEST
    )
    assert (
        refusal(entry, required_capabilities=requirements).code
        == ERROR_CODE_INVALID_REQUEST
    )


def test_a_list_at_the_permitted_length_still_authorizes() -> None:
    """And again the bound is a maximum: exactly `maxItems` is a conforming request."""
    entry = READ_ENTRY
    required = entry.required_capability
    extra_scopes = tuple(
        f"extra:scope_{index}"
        for index in range(MAX_LIST_ITEMS - len(entry.scope.required_scopes))
    )
    scopes = (*entry.scope.required_scopes, *extra_scopes)
    roles = tuple(f"role-{index}" for index in range(MAX_LIST_ITEMS))
    requirements = (
        catalogue_requirement(entry),
        *(
            CapabilityRequirement(
                id=f"extra.cap_{index}", minimum_version="1.0", required=False
            )
            for index in range(MAX_LIST_ITEMS - 1)
        ),
    )
    assert len(scopes) == len(roles) == len(requirements) == MAX_LIST_ITEMS

    session = session_for(entry, scopes=frozenset(scopes), roles=frozenset(roles))
    context = authorize(
        entry,
        session=session,
        scopes=scopes,
        required_capabilities=requirements,
        principal_claim=PrincipalClaim(claimed_roles=roles),
    )

    assert context.scopes == tuple(sorted(scopes))
    assert context.roles == tuple(sorted(roles))
    assert context.capabilities == (
        CapabilityRef(id=required.id, version=required.minimum_version),
    )


# --- refusals name a category and carry no value -------------------------------
#
# A refusal is produced from a request the caller controls end to end and is then
# rendered into a log line, an audit record and a wire `ApiError`. Anything
# interpolated into one is republished everywhere those go, so the message says which
# *kind* of refusal this is and nothing else.

#: One credential-shaped value per field a request can carry, each canonical for its
#: own field so it survives normalization and reaches the decision that refuses it.
#: Distinct literals, so a leak is attributable to the exact field that leaked it.
SECRET_REQUEST_ID = "Bearer-topsecret-request"
SECRET_CORRELATION_ID = "Bearer-topsecret-correlation"
SECRET_TRACE_ID = "Bearer-topsecret-trace"
SECRET_CLIENT_ID = "Bearer-topsecret-client"
SECRET_PRINCIPAL = "Bearer-topsecret-principal"
SECRET_ROLE = "Bearer-topsecret-role"
SECRET_WORKSPACE = "Bearer-topsecret-workspace"
SECRET_SCOPE = "bearer:topsecret_scope"
SECRET_PURPOSE = "bearer.topsecret_purpose"
SECRET_CAPABILITY = "bearer.topsecret_capability"

#: The one that is canonical for nothing: a bearer credential pasted verbatim into a
#: field. It is turned away by a different refusal from every family above, and must
#: not survive into that one either.
SECRET_CREDENTIAL = "Bearer topsecret"

#: Server-side values a refusal must not disclose either. A caller learning which
#: installation this instance serves, or which workspace this endpoint fronts, has
#: read server state out of a denial.
SECRET_INSTALLATION = "Bearer-topsecret-installation"
SECRET_BOUND_WORKSPACE = "Bearer-topsecret-endpoint"

ALL_SECRETS = (
    SECRET_REQUEST_ID,
    SECRET_CORRELATION_ID,
    SECRET_TRACE_ID,
    SECRET_CLIENT_ID,
    SECRET_PRINCIPAL,
    SECRET_ROLE,
    SECRET_WORKSPACE,
    SECRET_SCOPE,
    SECRET_PURPOSE,
    SECRET_CAPABILITY,
    SECRET_CREDENTIAL,
    SECRET_INSTALLATION,
    SECRET_BOUND_WORKSPACE,
    SECRET_IDEMPOTENCY_KEY,
    SECRET_RECORD_VERSION,
    "topsecret",
)

#: Metadata every refusal below carries: four caller-controlled identifiers that are
#: valid for their fields, so they reach the context-building step of every family and
#: would be in scope for any message that named a request fact.
SECRET_METADATA = {
    "request_id": SECRET_REQUEST_ID,
    "correlation_id": SECRET_CORRELATION_ID,
    "trace_id": SECRET_TRACE_ID,
    "client": ClientIdentity(id=SECRET_CLIENT_ID, version="0.1.0"),
}


def secret_refusals(entry):
    """One refusal of every family this seam can produce, all of them secret-laden.

    Each family is driven by the smallest thing that reaches it, with a credential-
    shaped value in the field that family reads: the purpose it rejects, the scope it
    does not grant, the capability it cannot honour, the workspace it does not serve.
    """
    required = entry.required_capability
    scoped = entry.scope.scope_kind == SCOPE_KIND_WORKSPACE
    secret_declaration = (
        catalogue_requirement(entry),
        CapabilityRequirement(
            id=SECRET_CAPABILITY, minimum_version="1.0", required=True
        ),
    )
    errors = [
        # No session at all.
        refusal(entry, session=None, **SECRET_METADATA),
        # Structurally malformed, then uncanonical, then out of range, then oversized.
        refusal(entry, **{**SECRET_METADATA, "client": 170018}),
        refusal(entry, **{**SECRET_METADATA, "purpose": SECRET_CREDENTIAL}),
        refusal(entry, **{**SECRET_METADATA, "trace_id": SECRET_CREDENTIAL}),
        refusal(entry, deadline_ms=MAX_DEADLINE_MS + 1, **SECRET_METADATA),
        refusal(
            entry,
            scopes=tuple(f"extra:scope_{index}" for index in range(MAX_LIST_ITEMS + 1)),
            **SECRET_METADATA,
        ),
        # Not in the catalogue, then not well formed for the operation it names.
        refusal(entry, operation="not.a.catalogue.operation", **SECRET_METADATA),
        refusal(entry, required_capabilities=(), **SECRET_METADATA),
        # Identity: a claimed principal and a claimed role the session does not hold.
        refusal(
            entry,
            principal_claim=PrincipalClaim(claimed_principal_id=SECRET_PRINCIPAL),
            **SECRET_METADATA,
        ),
        refusal(
            entry,
            principal_claim=PrincipalClaim(claimed_roles=(SECRET_ROLE,)),
            **SECRET_METADATA,
        ),
        # The allowlist, the scope set and the purpose.
        refusal(
            entry, session=session_for(entry, operations=frozenset()), **SECRET_METADATA
        ),
        refusal(
            entry,
            scopes=(*entry.scope.required_scopes, SECRET_SCOPE),
            **SECRET_METADATA,
        ),
        refusal(entry, purpose=SECRET_PURPOSE, **SECRET_METADATA),
        # A capability neither granted nor supported, then granted but not supported.
        refusal(entry, required_capabilities=secret_declaration, **SECRET_METADATA),
        refusal(
            entry,
            session=session_for(
                entry,
                capabilities=(
                    CapabilityRef(id=required.id, version=required.minimum_version),
                    CapabilityRef(id=SECRET_CAPABILITY, version="1.4"),
                ),
            ),
            required_capabilities=secret_declaration,
            **SECRET_METADATA,
        ),
        # This server's own state, ambiguous and therefore unusable.
        refusal(entry, supported=(*SUPPORTED, SUPPORTED[0]), **SECRET_METADATA),
    ]
    if scoped:
        errors.append(refusal(entry, workspace_id=SECRET_WORKSPACE, **SECRET_METADATA))
        errors.append(
            refusal(
                entry,
                binding=ServiceBinding(
                    installation_id=INSTALLATION_ID, workspace_id=SECRET_BOUND_WORKSPACE
                ),
                **SECRET_METADATA,
            )
        )
    else:
        errors.append(
            refusal(
                entry,
                binding=ServiceBinding(installation_id=SECRET_INSTALLATION),
                **SECRET_METADATA,
            )
        )
    if entry.idempotency.supports_idempotency_key:
        errors.append(
            refusal(entry, idempotency_key=SECRET_CREDENTIAL, **SECRET_METADATA)
        )
    if entry.precondition.supports_mutation_precondition:
        errors.append(
            refusal(
                entry,
                mutation_precondition=MutationPrecondition(
                    record_version=SECRET_CREDENTIAL
                ),
                **SECRET_METADATA,
            )
        )
    return errors


@ALL
def test_no_caller_or_server_value_reaches_any_refusal_family(entry) -> None:
    """Every family, every projection, every secret-shaped value.

    `str(error)` matters as much as `.message`: the two are set from the same argument
    and a logger will reach for whichever is nearer. `as_api_error()` matters most of
    all, since that one crosses the wire.
    """
    for error in secret_refusals(entry):
        assert_not_echoed(error, *ALL_SECRETS)

        # And nothing arrives by the other route either: a chained exception would
        # carry the validator's or the decoder's own message, which quotes what a
        # refusal must not repeat. Neither attribute is set, rather than one of them
        # being set and merely hidden from a printed traceback.
        assert error.__cause__ is None
        assert error.__context__ is None


#: Every message the seam publishes, read off the module rather than restated here, so
#: a message added without a category to belong to shows up as an unfrozen one below.
FROZEN_MESSAGES = frozenset(
    value
    for name, value in vars(authorization).items()
    if name.startswith("_MESSAGE_") and isinstance(value, str)
)


def test_the_seam_publishes_one_frozen_message_per_refusal_category() -> None:
    assert len(FROZEN_MESSAGES) >= 15
    for message in FROZEN_MESSAGES:
        # No unfilled placeholder, in any of the three spellings Python offers -- a
        # message carrying one is a message that was meant to be interpolated.
        assert "{" not in message and "}" not in message
        assert "%s" not in message and "%r" not in message
        for secret in ALL_SECRETS:
            assert secret not in message


@ALL
def test_every_refusal_message_is_one_of_the_frozen_constants(entry) -> None:
    """The behavioural half of "no value reaches a message".

    Absence tests prove no *listed* value leaked. This proves the message was never
    built at all: it is one of a fixed set, so there was nothing for a value to be
    interpolated into.
    """
    for error in (*secret_refusals(entry), *refusals_for(entry)):
        assert error.message in FROZEN_MESSAGES, error.message
        assert error.as_api_error().message in FROZEN_MESSAGES


#: The three ways a refusal is constructed in this module.
REFUSAL_CONSTRUCTORS = ("ApplicationAuthorizationError", "_invalid_request", "_denied")

#: The AST nodes that build a string out of something else.
INTERPOLATING_NODES = (ast.JoinedStr, ast.FormattedValue, ast.BinOp)


def test_no_refusal_is_constructed_from_an_interpolated_message() -> None:
    """The static half, covering the raise sites a test cannot reach.

    Some refusals guard state this build cannot currently produce -- an unreadable
    catalogue entry, a scope kind outside the frozen pair. They are unreachable, not
    absent, and an f-string in one of them would leak the first time it fired.
    """
    tree = ast.parse(inspect.getsource(authorization))
    offending = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in REFUSAL_CONSTRUCTORS:
            continue
        for argument in node.args:
            for inner in ast.walk(argument):
                if isinstance(inner, INTERPOLATING_NODES):
                    offending.append((node.func.id, node.lineno))
                if isinstance(inner, ast.Attribute) and inner.attr in (
                    "format",
                    "join",
                ):
                    offending.append((node.func.id, node.lineno))
    assert offending == [], offending


# --- the module's own dependency boundary --------------------------------------

FORBIDDEN_IMPORT_MARKERS = (
    "storage",
    "transport",
    "lease",
    "conformance",
    "fencing",
    "locks",
    "sqlite3",
)


def imported_modules(module) -> list[str]:
    """Every module name the source imports, however it spells the import."""
    tree = ast.parse(inspect.getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


def import_name_words(name: str) -> set[str]:
    """The `.`- and `_`-delimited words of one import name.

    Matching a marker as a whole word rather than as a bare substring is the difference
    between catching `omnivia_core_runtime.storage` and flagging the contract's
    `RELEASE_VERSION_PATTERN` for containing the letters of "lease". The property is
    about which modules this seam depends on, and a module named for one of these
    things is named for it as a word.
    """
    return set(re.split(r"[._]", name.lower()))


def test_authorization_imports_no_storage_transport_lease_or_conformance_module() -> (
    None
):
    """Deciding is separate from writing, and must stay separately testable.

    An import of storage, a transport, the lease machinery or a conformance helper
    would make this boundary impossible to reason about on its own -- and would put a
    path, a connection or a fencing value within reach of a refusal message.
    """
    imports = imported_modules(authorization)
    offending = [
        name
        for name in imports
        if import_name_words(name) & frozenset(FORBIDDEN_IMPORT_MARKERS)
    ]
    assert offending == [], offending

    # What it *may* import is the public contract package and the standard library.
    runtime_imports = [
        name for name in imports if name.startswith("omnivia_core_runtime")
    ]
    assert runtime_imports == [], runtime_imports
