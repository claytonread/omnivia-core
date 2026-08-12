"""Server-issued mutation grants, and the one seam a mutation may run through (V06-5 S0).

This module owns the mutation foundation consumed by the bounded S1-S4 handlers. S2 now
registers ``memory.create`` through that foundation; operation registration itself remains
in the application-family wiring rather than here.

Two values and one function are the whole of it.

**The grant is issued, never claimed.** :class:`MutationGrant` binds the authenticated
principal, the workspace, the role the issuer required and the session was proved to
hold, the effective scopes and capabilities, one declared operation purpose, the live
fencing generation, and an issue time and expiry. :func:`issue_mutation_grant` is the
only way to obtain one, and every field it fills comes from server state: the
:class:`AuthenticatedSession` the service built at startup, the
:class:`AuthorizedApplicationContext` the twelve-check seam returned, the live
:class:`MutationGuard` read out of the workspace database, the frozen catalogue entry,
and :data:`MUTATION_PURPOSES`. Not one field is read from request metadata.

That is the property, stated as a rule rather than as an intention: **a request can
narrow this grant and can never widen it.** The claimed principal and claimed roles were
already held to `claims narrow, never add` by `authorize_application_request`; the
purpose is taken from :data:`MUTATION_PURPOSES` and merely *checked* against the one the
request stated; the scopes are intersected with the session's grant; the generation
comes from the guard row rather than from anything a caller could state. There is no
default grant, no module-level grant constant, and no code path that proceeds without
one.

**There is no role model in this repository, and that is load-bearing rather than a
gap.** `AuthenticatedSession.roles` is empty for the local owner, so
:func:`issue_mutation_grant` cannot issue a grant for it -- a mutation is refused at
issuance for the same reason `local_owner_session` refuses to grant one at all. When a
role model arrives, the required role becomes a real policy input at the issuance site;
until then the honest behaviour of asking for a role nobody holds is a refusal.

**The execution seam is generic and owns no domain knowledge.**
:func:`execute_mutation` is handed a domain mutation, a result validator, a server clock
and -- for the operations whose catalogue metadata declares one -- a precondition reader,
and supplies everything that must be true around them: a valid grant for this operation,
workspace and purpose, unexpired against the server's monotonic clock on entry and again
immediately before settlement; the precondition checked *before* the domain mutation
where the catalogue requires one, and no reader called at all where it does not; the
lease and fencing generation validated on entry and again immediately before commit; the
domain mutation and 0007's durable audit, claim and outcome and 0013's execution record
written in one SQLite transaction; a stored answer on honest replay, proved against its
own digest and re-canonicalized before it is served, without re-running the mutation; a
typed conflict when one idempotency scope is reused for a different canonical request;
and one rollback covering all of it on any error.

What this module deliberately does not do: it registers no operation, wires no
production grant and adds no transport. The S2 handler owns its issuance call site and
the application builder owns its server session, while this executor remains generic.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    CONTENT_CHECKSUM_ALGORITHM,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    OPERATION_CATALOGUE,
    RETRY_CLASS_NON_RETRYABLE,
    CapabilityRef,
    ContractSemanticError,
    IdempotencyEquivalence,
    get_operation_metadata,
    to_canonical_json,
)
from omnivia_core.contracts.v1.canonical_json import canonicalize, parse_json_document
from omnivia_core_runtime.ownership.fencing import MutationGuard, fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
)
from omnivia_core_runtime.service.operations import OperationError

#: The purpose each mutating catalogue operation is served under, and the only purposes
#: a mutation grant may ever carry. Five values over nine operations: a purpose names a
#: coherent operation family rather than restating an operation name, which is the same
#: shape `application.OPERATION_PURPOSES` uses for the read operations.
#:
#: Every mutating operation is listed explicitly. There is no prefix rule, no fallback
#: and no default: an operation absent from this map cannot be granted at all, and a
#: request whose stated purpose is not the one this map declares for its operation is
#: refused rather than served under the declared purpose anyway.
WORKSPACE_ADMINISTRATION_PURPOSE: Final = "workspace_administration"
MEMORY_AUTHORING_PURPOSE: Final = "memory_authoring"
CONTENT_INGESTION_PURPOSE: Final = "content_ingestion"
JOB_CONTROL_PURPOSE: Final = "job_control"
KNOWLEDGE_GOVERNANCE_PURPOSE: Final = "knowledge_governance"

MUTATION_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "workspace.create": WORKSPACE_ADMINISTRATION_PURPOSE,
        "memory.create": MEMORY_AUTHORING_PURPOSE,
        "import.start": CONTENT_INGESTION_PURPOSE,
        "job.cancel": JOB_CONTROL_PURPOSE,
        "job.retry": JOB_CONTROL_PURPOSE,
        "knowledge.propose": KNOWLEDGE_GOVERNANCE_PURPOSE,
        "candidate.approve": KNOWLEDGE_GOVERNANCE_PURPOSE,
        "candidate.reject": KNOWLEDGE_GOVERNANCE_PURPOSE,
        "record.supersede": KNOWLEDGE_GOVERNANCE_PURPOSE,
    }
)

#: The role each mutating operation requires, and the only role a grant for it may ever
#: carry. Server-owned policy in exactly the same sense as :data:`MUTATION_PURPOSES`: it
#: is not an issuance argument, not a handler choice and not a request field, so no
#: caller and no future call site can name the role its own mutation is checked against.
#:
#: `workspace.create` is installation-scoped and declared here for completeness only --
#: this workspace lane refuses it at issuance, and the installation authority that would
#: serve it is a later slice.
INSTALLATION_ADMINISTRATOR_ROLE: Final = "installation_administrator"
WORKSPACE_CONTRIBUTOR_ROLE: Final = "workspace_contributor"
KNOWLEDGE_REVIEWER_ROLE: Final = "knowledge_reviewer"

MUTATION_ROLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "workspace.create": INSTALLATION_ADMINISTRATOR_ROLE,
        "memory.create": WORKSPACE_CONTRIBUTOR_ROLE,
        "import.start": WORKSPACE_CONTRIBUTOR_ROLE,
        "job.cancel": WORKSPACE_CONTRIBUTOR_ROLE,
        "job.retry": WORKSPACE_CONTRIBUTOR_ROLE,
        "knowledge.propose": WORKSPACE_CONTRIBUTOR_ROLE,
        "candidate.approve": KNOWLEDGE_REVIEWER_ROLE,
        "candidate.reject": KNOWLEDGE_REVIEWER_ROLE,
        "record.supersede": KNOWLEDGE_REVIEWER_ROLE,
    }
)

#: Every catalogue operation that declares a side effect, derived from the frozen
#: catalogue rather than transcribed. This is the oracle the maps above are held to.
MUTATING_OPERATIONS: Final[frozenset[str]] = frozenset(
    entry.name for entry in OPERATION_CATALOGUE if entry.scope.side_effect != "none"
)

for _map_name, _policy in (
    ("MUTATION_PURPOSES", MUTATION_PURPOSES),
    ("MUTATION_ROLES", MUTATION_ROLES),
):
    if frozenset(_policy) != MUTATING_OPERATIONS:  # pragma: no cover - build fault
        # An import-time refusal, because the alternative is a build that serves some
        # mutations and silently cannot serve others. A catalogue that grew a mutating
        # operation, or a map that lost one, is a defect to fix before this process runs
        # -- not a condition to discover at the first request for the missing name.
        raise ValueError(
            f"{_map_name} must declare exactly the catalogue's mutating operations; "
            f"missing={sorted(MUTATING_OPERATIONS - frozenset(_policy))} "
            f"unexpected={sorted(frozenset(_policy) - MUTATING_OPERATIONS)}"
        )

#: How long an issued grant stays usable. Short on purpose: the grant pins a fencing
#: generation, and a generation that moves invalidates it anyway, so the expiry is a
#: second bound rather than the only one. One minute is comfortably longer than any
#: synchronous mutation in the catalogue and far shorter than a takeover window.
DEFAULT_GRANT_LIFETIME_US: Final = 60_000_000

#: Refusal messages, frozen here rather than built at the raise site, for the reason
#: `authorization.py` freezes its own: a refusal is rendered into a log line, an audit
#: record and a wire `ApiError`, so anything interpolated into one is republished
#: everywhere those go. Naming the category is what a caller needs and all it is owed.
_MESSAGE_NO_GRANT: Final = "this mutation has no server-issued grant behind it"
_MESSAGE_GRANT_NOT_FOR_THIS_REQUEST: Final = (
    "the grant presented was not issued for this operation, workspace and purpose"
)
_MESSAGE_GRANT_EXPIRED: Final = "the grant presented is outside its validity window"
_MESSAGE_NOT_A_MUTATION: Final = (
    "this operation declares no side effect and takes no mutation grant"
)
_MESSAGE_PURPOSE_NOT_DECLARED: Final = (
    "the request states a purpose this operation is not served under"
)
_MESSAGE_ROLE_NOT_HELD: Final = (
    "the session does not hold the role this mutation requires"
)
_MESSAGE_OPERATION_NOT_GRANTED: Final = (
    "the session is not granted the operation this mutation names"
)
_MESSAGE_NO_WORKSPACE: Final = (
    "this mutation seam serves workspace-scoped operations only"
)
_MESSAGE_WORKSPACE_NOT_OWNED: Final = (
    "the workspace this mutation names is not the one this service instance holds"
)
_MESSAGE_IDEMPOTENCY_REQUIRED: Final = (
    "this operation requires an idempotency key and a mutation precondition"
)
_MESSAGE_EQUIVALENCE_MISMATCH: Final = (
    "the idempotency scope presented does not describe this request"
)
_MESSAGE_GRANT_NOT_BOUND_TO_REQUEST: Final = (
    "the grant presented was not issued for this canonical request"
)
_MESSAGE_GRANT_ALREADY_USED: Final = (
    "the server-issued grant has already been consumed by another mutation"
)
_MESSAGE_IDEMPOTENCY_CONFLICT: Final = (
    "this idempotency key was already used for a different request"
)
_MESSAGE_PRECONDITION_FAILED: Final = (
    "the record this mutation names is not at the version the request requires"
)
_MESSAGE_UNSTORABLE_RESULT: Final = (
    "this build cannot durably store an answer of this size"
)
_MESSAGE_UNSETTLED_CLAIM: Final = (
    "a recorded claim has no terminal outcome to answer a replay from"
)
_MESSAGE_PRECONDITION_NOT_SUPPORTED: Final = (
    "this operation declares no mutation precondition and none may be read for it"
)
_MESSAGE_OUTCOME_NOT_REPLAYABLE: Final = (
    "a stored outcome is not the canonical document its digest was taken over"
)
_MESSAGE_OUTCOME_AUDIT_CROSSLINKED: Final = (
    "a stored outcome names an audit event its claim does not"
)
_MESSAGE_RESULT_REJECTED: Final = (
    "this mutation produced a result the server will not serve for this operation"
)


def _monotonic_us(clock: Clock) -> int:
    """The clock's monotonic reading, in microseconds.

    Monotonic because every expiry decision in this module is one: a wall clock that
    steps backwards over an NTP correction would extend a grant, and one that steps
    forward would revoke a live one mid-transaction.
    """
    return int(clock.monotonic() * 1_000_000)


def _wall_us(clock: Clock) -> int:
    """The clock's wall reading, in microseconds since the epoch. Recorded, never judged."""
    return int(clock.wall_time().timestamp() * 1_000_000)


class MutationDenied(OperationError):
    """A mutation may not proceed: no grant, the wrong grant, or an expired one.

    An `OperationError` subclass so that a handler consuming this seam lets it out
    unchanged and the dispatcher renders it in the contract's own vocabulary. It is a
    distinct class so a caller can tell an authority refusal from a precondition
    failure without reading the code string.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            ERROR_CODE_AUTHORIZATION_DENIED,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


class MutationPreconditionFailed(OperationError):
    """The record named by the request is not at the version the request requires."""

    def __init__(self, message: str = _MESSAGE_PRECONDITION_FAILED) -> None:
        super().__init__(
            ERROR_CODE_MUTATION_PRECONDITION_FAILED,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


class MutationIdempotencyConflict(OperationError):
    """One idempotency scope, two different canonical requests."""

    def __init__(
        self,
        message: str = _MESSAGE_IDEMPOTENCY_CONFLICT,
        *,
        audit_reference: str | None = None,
    ) -> None:
        super().__init__(
            ERROR_CODE_IDEMPOTENCY_CONFLICT,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
            audit_reference=audit_reference,
        )


class MutationSeamFault(OperationError):
    """This build cannot complete a mutation it accepted. Never the caller's mistake."""

    def __init__(self, message: str) -> None:
        super().__init__(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


_SERVER_ISSUER_MARK: Final = object()
"""Process-local construction authority for :class:`MutationGrant`.

Request metadata contains only contract values; it can never contain this object.
The mark is also checked again at execution, so an uninitialised instance fabricated
with ``object.__new__`` is a refusal rather than a partially stated grant.
"""


@dataclass(frozen=True, init=False)
class MutationGrant:
    """One server-issued permission to perform one mutation, for a bounded time.

    Every field is a server fact. There is no constructor default and no partially
    stated grant: a value of this type either names all of it or does not exist, so
    "the grant did not say" is not a state anything downstream has to handle.

    Holding one of these is not authority on its own -- :func:`execute_mutation` still
    validates it against the request it is presented with, against the clock, and
    against the live lease -- but nothing can be mutated without one.
    """

    _issuer_mark: object = field(repr=False, compare=False)
    grant_id: str
    principal_id: str
    workspace_id: str
    required_role: str
    scopes: frozenset[str]
    capabilities: tuple[CapabilityRef, ...]
    operation: str
    purpose: str
    idempotency_key: str
    request_fingerprint: str
    required_record_version: str | None
    fencing_generation: int
    issued_at_us: int
    issued_monotonic_us: int
    expires_monotonic_us: int

    def __init__(self, issuer_mark: object | None = None) -> None:
        """Refuse public construction; only this module's issuer holds the mark.

        ``init=False`` prevents dataclasses from publishing every authority field as
        constructor input. A request-derived mapping therefore cannot be expanded into
        a grant, and ``dataclasses.replace`` cannot manufacture a widened copy.
        """
        if issuer_mark is not _SERVER_ISSUER_MARK:
            raise TypeError("MutationGrant values are issued by the server")
        object.__setattr__(self, "_issuer_mark", issuer_mark)

    @property
    def server_issued(self) -> bool:
        """Whether this value came through the module's issuer-only constructor."""
        return getattr(self, "_issuer_mark", None) is _SERVER_ISSUER_MARK

    def covers(
        self,
        *,
        operation: str,
        workspace_id: str | None,
        purpose: str,
        principal_id: str,
        roles: tuple[str, ...],
        scopes: tuple[str, ...],
        capabilities: tuple[CapabilityRef, ...],
    ) -> bool:
        """Whether this grant was issued for exactly this request.

        All four together. A grant for the right operation in the wrong workspace, or
        under a purpose the request did not state, is not a grant for this request.
        """
        return (
            operation == self.operation
            and workspace_id == self.workspace_id
            and purpose == self.purpose
            and principal_id == self.principal_id
            and self.required_role in roles
            and frozenset(scopes) == self.scopes
            and capabilities == self.capabilities
        )

    def binds(
        self,
        *,
        idempotency_key: str,
        fingerprint: str,
        record_version: str | None,
    ) -> bool:
        """Whether this grant was issued against exactly this canonical request.

        The scope's operation, workspace and principal are the grant's own fields and
        are checked by :meth:`covers`; what is left is the key that names the scope, the
        fingerprint of the request body and metadata, and the version the request
        requires the named record to be at. A grant for the same operation carrying a
        different one of those three is a grant for a different mutation.
        """
        return (
            idempotency_key == self.idempotency_key
            and fingerprint == self.request_fingerprint
            and record_version == self.required_record_version
        )

    def is_current(self, now_monotonic_us: int) -> bool:
        """Whether this grant is inside its validity window at a monotonic reading.

        The argument is a monotonic reading and nothing else. `issued_at_us` is a wall
        diagnostic, is never compared here, and could not be: the two clocks share no
        origin, so comparing one to the other is a category error rather than a
        conservative approximation.

        Half-open: issuance is inclusive so a mutation in the same microsecond as its
        grant is served, and expiry is exclusive so the recorded window and the durable
        `settled_monotonic_us < grant_expires_monotonic_us` check agree at the boundary.
        """
        return self.issued_monotonic_us <= now_monotonic_us < self.expires_monotonic_us


def issue_mutation_grant(
    context: AuthorizedApplicationContext,
    *,
    session: AuthenticatedSession,
    binding: ServiceBinding,
    guard: MutationGuard,
    equivalence: IdempotencyEquivalence,
    clock: Clock,
    lifetime_us: int = DEFAULT_GRANT_LIFETIME_US,
) -> MutationGrant:
    """Issue a grant, or refuse. The only way a :class:`MutationGrant` comes into being.

    Every input is server state. `context` is what `authorize_application_request`
    returned, so the claims it carries have already been narrowed against the session;
    `session` is the grant the service built at startup; `guard` is the row read out of
    the workspace database, which is where the fencing generation comes from; `binding`
    is what this service instance was launched to serve; `equivalence` is the accepted
    contract function's own value for this request, which is what the grant is bound to.
    Request metadata reaches none of these.

    The required role is not an argument. It is read from :data:`MUTATION_ROLES` by
    operation, exactly as the purpose is read from :data:`MUTATION_PURPOSES`, so neither
    a caller nor a future handler can name the role its own mutation is checked against.

    Refusals are all of the same class and all fail closed. The order is deliberate:
    what the operation *is* precedes what the session *holds*, so an operation that
    takes no mutation grant is reported as that rather than as a permissions problem a
    caller might answer by asking for a wider grant.
    """
    operation = context.operation
    declared = MUTATION_PURPOSES.get(operation)
    if declared is None:
        raise MutationDenied(_MESSAGE_NOT_A_MUTATION)
    # The catalogue, asked independently. The map above is policy and this is the frozen
    # fact behind it; agreeing with only one of them is how a read operation ends up
    # holding a mutation grant.
    if get_operation_metadata(operation).scope.side_effect == "none":
        raise MutationDenied(_MESSAGE_NOT_A_MUTATION)
    if context.purpose != declared:
        raise MutationDenied(_MESSAGE_PURPOSE_NOT_DECLARED)

    if context.principal_id != session.principal_id:
        raise MutationDenied(_MESSAGE_NO_GRANT)
    if operation not in session.operations:
        raise MutationDenied(_MESSAGE_OPERATION_NOT_GRANTED)
    # The role check the whole read-only posture rests on today. `local_owner_session`
    # holds no roles, so no `required_role` a caller could name is held, so no mutation
    # grant exists for it -- which is the same answer that constructor gives, arrived at
    # independently one layer down.
    required_role = MUTATION_ROLES[operation]
    if required_role not in session.roles or required_role not in context.roles:
        raise MutationDenied(_MESSAGE_ROLE_NOT_HELD)

    workspace_id = context.workspace_id
    if workspace_id is None:
        # Installation-scoped mutations (`workspace.create`) have no workspace-owned
        # durable home: 0007's tables bind the singleton workspace, and fabricating one
        # for an installation-scoped operation is exactly what that migration refused to
        # do. Declared in MUTATION_PURPOSES, refused here, and a later lane owns the
        # installation-scoped path rather than this seam quietly pretending to have one.
        raise MutationDenied(_MESSAGE_NO_WORKSPACE)
    if binding.workspace_id is not None and binding.workspace_id != workspace_id:
        raise MutationDenied(_MESSAGE_WORKSPACE_NOT_OWNED)
    if guard.workspace_id != workspace_id:
        raise MutationDenied(_MESSAGE_WORKSPACE_NOT_OWNED)

    # A grant is issued for one canonical request, not for an operation in general.
    # Every mutating operation in the catalogue takes an idempotency key, so a request
    # without one is malformed rather than a narrower grant. Only some of them take a
    # mutation precondition; where the request states one it is bound here, and where
    # the operation has none there is nothing to bind.
    key = context.idempotency_key
    required_version = context.mutation_precondition
    if key is None:
        raise MutationDenied(_MESSAGE_IDEMPOTENCY_REQUIRED)
    scope = equivalence.scope
    if (
        scope.operation != operation
        or scope.workspace_id != workspace_id
        or scope.principal_id != session.principal_id
        or scope.idempotency_key != key
    ):
        raise MutationDenied(_MESSAGE_EQUIVALENCE_MISMATCH)

    # The clock is read here, by the issuer, from a server-injected source: `now_us` is
    # not an argument, so no caller can name the moment its own grant was issued at and
    # none can hand in a window wider than the one this server would have produced.
    issued_wall_us = _wall_us(clock)
    issued_monotonic_us = _monotonic_us(clock)
    if lifetime_us <= 0 or issued_wall_us <= 0:
        raise MutationSeamFault(_MESSAGE_GRANT_EXPIRED)

    grant = MutationGrant(_SERVER_ISSUER_MARK)
    issued_fields: Mapping[str, object] = {
        "grant_id": f"mgr-{uuid.uuid4()}",
        # The session's principal, never the claim's -- the same rule the seam applies
        # one layer up, restated here because this value outlives the request.
        "principal_id": session.principal_id,
        "workspace_id": workspace_id,
        "required_role": required_role,
        # Intersected, never copied. The context's scopes are already a subset of the
        # session's; the intersection is what keeps that true if either ever changes.
        "scopes": frozenset(context.scopes) & session.scopes,
        "capabilities": context.capabilities,
        "operation": operation,
        # From the map, not from the request. The request's purpose was checked against
        # it above; what the grant carries is the declared one.
        "purpose": declared,
        # The canonical request this grant is for, and nothing else. `fingerprint` is
        # `idempotency_equivalence()`'s own digest of the request, so a grant cannot be
        # carried across to a request that differs anywhere the contract counts.
        "idempotency_key": key,
        "request_fingerprint": equivalence.fingerprint,
        "required_record_version": (
            None if required_version is None else required_version.record_version
        ),
        # From the live guard row. A generation a caller could state would make fencing
        # a value the caller chooses.
        "fencing_generation": guard.fencing_generation,
        # Wall time is a diagnostic and is recorded as one. The validity window is
        # process-local monotonic microseconds, which is the only pair of readings a
        # later expiry decision may be made against.
        "issued_at_us": issued_wall_us,
        "issued_monotonic_us": issued_monotonic_us,
        "expires_monotonic_us": issued_monotonic_us + lifetime_us,
    }
    for name, value in issued_fields.items():
        object.__setattr__(grant, name, value)
    return grant


@dataclass(frozen=True)
class MutationOutcome:
    """The answer one accepted mutation settled on, and whether it was re-served.

    `replayed` is true when this answer came from the stored outcome of an earlier
    identical request rather than from running the domain mutation again. `result` is
    identical either way, because the replayed value is decoded from the canonical
    bytes the first execution stored.
    """

    result: Mapping[str, Any]
    replayed: bool
    claim_id: str
    audit_ref: str


@dataclass(frozen=True)
class MutationSettlementContext:
    """Executor-owned identities and instant supplied to one domain mutation."""

    audit_ref: str
    claim_id: str
    outcome_id: str
    settled_at_us: int


#: Reads the current version token of the record a mutation names, or `None` when there
#: is no such record. Compared against the request's `mutation_precondition`; the seam
#: never interprets the token, so any total ordering-free opaque string works.
PreconditionReader = Callable[[sqlite3.Connection], str | None]

#: The domain mutation itself, run on the fenced connection inside the transaction that
#: also writes the durable facts. Its returned mapping is the operation's result.
DomainMutation = Callable[
    [sqlite3.Connection, MutationSettlementContext], Mapping[str, Any]
]

IdentifierAllocator = Callable[[str], str]

#: Whether a result is one the server will serve for this operation. Supplied by the
#: server at the call site rather than defaulted here: a permissive default validator
#: would mean every operation that forgot to state a shape got one that accepts
#: anything, which is the failure the callback exists to prevent. Run against a freshly
#: computed result before it is stored, and against a replayed one before it is
#: returned, so a stored answer that stopped being servable is refused rather than
#: re-served for as long as the claim survives.
ResultValidator = Callable[[Mapping[str, Any]], bool]

#: The schema bound on `omnivia_idempotency_outcomes.outcome_json`. A result past it is
#: stored byte-exactly in 0014's immutable outcome-document relation and referenced by
#: the ordinary idempotency outcome.
_MAX_INLINE_OUTCOME = 8192

#: The schema bound on `omnivia_application_audit_events.granted_authority_json`.
_MAX_GRANTED_AUTHORITY = 4096


def _allocate_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def execute_mutation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    grant: MutationGrant | None,
    context: AuthorizedApplicationContext,
    equivalence: IdempotencyEquivalence,
    precondition: PreconditionReader | None = None,
    mutate: DomainMutation,
    validate_result: ResultValidator,
    clock: Clock,
    allocate_identifier: IdentifierAllocator | None = None,
) -> MutationOutcome:
    """Run one mutation under a grant, or refuse it, leaving nothing half-written.

    The order below is the specification, not an implementation detail:

    1. the grant is checked against this request and against the server's own monotonic
       clock, before a transaction is opened and therefore before anything can be
       written;
    2. `fenced_transaction` validates the lease and the fencing generation on entry and
       again immediately before COMMIT, so authority lost mid-transaction fails rather
       than commits;
    3. an existing claim in this idempotency scope is resolved first -- an equal digest
       is answered from the stored outcome without running `mutate` at all, and a
       different digest is a typed conflict;
    4. the precondition is read and compared *before* the domain mutation, for the
       operations whose catalogue metadata requires one; for the operations it does not,
       there is no precondition to bind and no reader is called;
    5. the executor allocates the settlement identities, inserts the audit event, then
       calls `mutate` with that settlement context; the result, claim, outcome and
       execution facts follow only after validation, with a final monotonic check before
       commit. The audit is not externally visible unless every later write commits;
    6. any exception at any point leaves the transaction rolled back by
       `fenced_transaction`, taking the domain mutation and every durable settlement
       fact with it.

    The clock is read here rather than supplied as a moment: a `now_us` argument would
    make the deadline something the call site states, and the whole point of the expiry
    is that it is not. Every timestamp written durably is the wall reading taken at
    settlement, so the audit event, the claim, the outcome and the execution record all
    say when the mutation settled rather than when its grant was issued.

    `equivalence` is the accepted contract function's own output for this request, not a
    second opinion computed here: `idempotency_equivalence()` remains the single
    authority for what makes two requests the same request. It is cross-checked against
    the grant and the context below, so an equivalence describing some other request
    cannot be used to reach this one's stored answer.
    """
    allocator = allocate_identifier or _allocate_identifier
    if not isinstance(grant, MutationGrant) or not grant.server_issued:
        raise MutationDenied(_MESSAGE_NO_GRANT)
    if not grant.covers(
        operation=context.operation,
        workspace_id=context.workspace_id,
        purpose=context.purpose,
        principal_id=context.principal_id,
        roles=context.roles,
        scopes=context.scopes,
        capabilities=context.capabilities,
    ):
        raise MutationDenied(_MESSAGE_GRANT_NOT_FOR_THIS_REQUEST)
    if not grant.is_current(_monotonic_us(clock)):
        raise MutationDenied(_MESSAGE_GRANT_EXPIRED)

    key = context.idempotency_key
    stated_version = context.mutation_precondition
    if key is None:
        # Every mutating operation in the catalogue takes an idempotency key, so a
        # request without one is malformed rather than narrower. The grant binds it at
        # issuance, so reaching this refusal means the presented request lost it.
        raise MutationDenied(_MESSAGE_IDEMPOTENCY_REQUIRED)

    # The catalogue decides whether there is a precondition at all, and the call site is
    # held to that decision in both directions. An operation that requires one and was
    # handed no reader -- or no version -- is refused rather than run against whatever
    # the record happens to be; an operation that declares none and was handed either is
    # refused rather than served by a reader the catalogue says nothing about.
    if get_operation_metadata(grant.operation).precondition.required:
        if stated_version is None or precondition is None:
            raise MutationDenied(_MESSAGE_IDEMPOTENCY_REQUIRED)
        required_record_version: str | None = stated_version.record_version
    else:
        if stated_version is not None or precondition is not None:
            raise MutationDenied(_MESSAGE_PRECONDITION_NOT_SUPPORTED)
        required_record_version = None

    scope = equivalence.scope
    if (
        scope.operation != grant.operation
        or scope.workspace_id != grant.workspace_id
        or scope.principal_id != grant.principal_id
        or scope.idempotency_key != key
    ):
        raise MutationDenied(_MESSAGE_EQUIVALENCE_MISMATCH)
    # The grant names the request it was issued for. A grant for the same operation and
    # scope but a different body, key or precondition is refused here -- before the
    # transaction, and therefore before any handler could run.
    if not grant.binds(
        idempotency_key=key,
        fingerprint=equivalence.fingerprint,
        record_version=required_record_version,
    ):
        raise MutationDenied(_MESSAGE_GRANT_NOT_BOUND_TO_REQUEST)

    with fenced_transaction(
        connection,
        identity,
        workspace_id=grant.workspace_id,
        fencing_generation=grant.fencing_generation,
    ) as fenced:
        # One grant, one use -- checked before the claim is read, so a grant that has
        # already answered a replay cannot answer a second one either.
        consumed = fenced.execute(
            "SELECT 1 FROM omnivia_mutation_executions WHERE grant_id = ?",
            (grant.grant_id,),
        ).fetchone()
        if consumed is not None:
            raise MutationDenied(_MESSAGE_GRANT_ALREADY_USED)

        existing = _find_claim(fenced, grant, key)
        if existing is not None:
            claim_id, stored_digest, original_audit_ref = existing
            if stored_digest != equivalence.fingerprint:
                raise MutationIdempotencyConflict(audit_reference=original_audit_ref)
            # An honest replay runs no domain code, but it does spend the fresh grant it
            # was presented with: the row below is what makes that durable, so the grant
            # cannot later authorize a mutation that would actually write something.
            replayed = _replay(fenced, claim_id)
            # Validated on the way out as well as on the way in. A stored answer that
            # this build no longer considers servable is refused rather than re-served
            # from the claim for as long as the claim exists.
            if not validate_result(replayed.result):
                raise MutationSeamFault(_MESSAGE_RESULT_REJECTED)
            settled_monotonic_us = _require_current(grant, clock)
            _record_execution(
                fenced,
                grant,
                kind="replayed",
                claim_id=claim_id,
                audit_ref=replayed.audit_ref,
                settled_at_us=_wall_us(clock),
                settled_monotonic_us=settled_monotonic_us,
            )
            _require_current(grant, clock)
            return replayed

        if precondition is not None:
            observed = precondition(fenced)
            if observed != required_record_version:
                raise MutationPreconditionFailed()

        # Settlement begins only while the grant is current. The executor, never the
        # handler, allocates every identity that binds the domain write to M1.
        settled_monotonic_us = _require_current(grant, clock)
        settled_at_us = _wall_us(clock)
        settlement = MutationSettlementContext(
            audit_ref=allocator("aud"),
            claim_id=allocator("clm"),
            outcome_id=allocator("out"),
            settled_at_us=settled_at_us,
        )
        authority_json = to_canonical_json(context.authority.to_wire())
        if len(authority_json) > _MAX_GRANTED_AUTHORITY:
            raise MutationSeamFault(_MESSAGE_UNSTORABLE_RESULT)

        # The audit precedes the domain callback so an M3 candidate can satisfy its
        # immediate audit foreign key. It is still invisible unless this transaction
        # commits, and every later failure rolls it back with the domain rows.
        fenced.execute(
            "INSERT INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', NULL, ?)",
            (
                settlement.audit_ref,
                grant.workspace_id,
                grant.principal_id,
                grant.operation,
                grant.purpose,
                context.request_id,
                context.correlation_id,
                context.trace_id,
                authority_json,
                settled_at_us,
            ),
        )

        result = dict(mutate(fenced, settlement))
        if not validate_result(result):
            raise MutationSeamFault(_MESSAGE_RESULT_REJECTED)
        outcome_json = to_canonical_json(result)
        outcome_digest = _digest(outcome_json)

        fenced.execute(
            "INSERT INTO omnivia_idempotency_claims "
            "(claim_id, workspace_id, principal_id, operation, idempotency_key, "
            "request_digest, audit_ref, claimed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                settlement.claim_id,
                grant.workspace_id,
                grant.principal_id,
                grant.operation,
                key,
                equivalence.fingerprint,
                settlement.audit_ref,
                settled_at_us,
            ),
        )

        outcome_reference: str | None = None
        inline_outcome: str | None = outcome_json
        if len(outcome_json.encode("utf-8")) > _MAX_INLINE_OUTCOME:
            outcome_reference = allocator("ocr")
            inline_outcome = None
            fenced.execute(
                "INSERT INTO omnivia_application_outcome_documents "
                "(workspace_id, outcome_reference, claim_id, audit_ref, outcome_json, "
                "outcome_digest, outcome_byte_length) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.workspace_id,
                    outcome_reference,
                    settlement.claim_id,
                    settlement.audit_ref,
                    outcome_json,
                    outcome_digest,
                    len(outcome_json.encode("utf-8")),
                ),
            )
        fenced.execute(
            "INSERT INTO omnivia_idempotency_outcomes "
            "(outcome_id, claim_id, workspace_id, outcome_branch, error_code, "
            "outcome_json, outcome_reference, outcome_digest, audit_ref, settled_at_us) "
            "VALUES (?, ?, ?, 'success', NULL, ?, ?, ?, ?, ?)",
            (
                settlement.outcome_id,
                settlement.claim_id,
                grant.workspace_id,
                inline_outcome,
                outcome_reference,
                outcome_digest,
                settlement.audit_ref,
                settled_at_us,
            ),
        )
        _record_execution(
            fenced,
            grant,
            kind="executed",
            claim_id=settlement.claim_id,
            audit_ref=settlement.audit_ref,
            settled_at_us=settled_at_us,
            settled_monotonic_us=settled_monotonic_us,
        )
        _require_current(grant, clock)

        # Returned from inside the block on purpose. `fenced_transaction` resumes on the
        # way out, revalidates the lease and issues the COMMIT, and a failure there
        # raises through this return rather than past it -- so a value is handed back
        # only for a transaction that actually committed.
        return MutationOutcome(
            result=result,
            replayed=False,
            claim_id=settlement.claim_id,
            audit_ref=settlement.audit_ref,
        )


def _require_current(grant: MutationGrant, clock: Clock) -> int:
    """The monotonic settlement reading, or a refusal because the grant expired.

    Called inside the fenced transaction, so the refusal is a rollback of everything the
    transaction holds -- the domain mutation included -- rather than a failure to write
    the evidence for a mutation that already happened.
    """
    settled_monotonic_us = _monotonic_us(clock)
    if not grant.is_current(settled_monotonic_us):
        raise MutationDenied(_MESSAGE_GRANT_EXPIRED)
    return settled_monotonic_us


def _record_execution(
    connection: sqlite3.Connection,
    grant: MutationGrant,
    *,
    kind: str,
    claim_id: str,
    audit_ref: str,
    settled_at_us: int,
    settled_monotonic_us: int,
) -> None:
    """Spend the grant durably, inside the caller's transaction.

    Written for both uses of a grant: the mutation that ran (`executed`) and the honest
    replay that answered from stored bytes (`replayed`). The unique index on `grant_id`
    is what makes one-shot structural rather than conventional, and the partial unique
    index on `claim_id` still admits exactly one `executed` row per idempotency claim.
    """
    # These two durable fields are JSON arrays rather than wire mappings, so use the
    # same canonical encoder settings directly.  ``to_canonical_json`` is deliberately
    # typed for contract mappings only.
    scopes_json = json.dumps(
        sorted(grant.scopes),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    capabilities_json = json.dumps(
        [capability.to_wire() for capability in grant.capabilities],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if (
        len(scopes_json) > _MAX_GRANTED_AUTHORITY
        or len(capabilities_json) > _MAX_GRANTED_AUTHORITY
    ):
        raise MutationSeamFault(_MESSAGE_UNSTORABLE_RESULT)
    connection.execute(
        "INSERT INTO omnivia_mutation_executions "
        "(execution_id, workspace_id, principal_id, operation, purpose, grant_id, "
        "required_role, scopes_json, capabilities_json, execution_kind, "
        "fencing_generation, grant_issued_at_us, grant_issued_monotonic_us, "
        "grant_expires_monotonic_us, settled_monotonic_us, claim_id, audit_ref, "
        "recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"exe-{uuid.uuid4()}",
            grant.workspace_id,
            grant.principal_id,
            grant.operation,
            grant.purpose,
            grant.grant_id,
            grant.required_role,
            scopes_json,
            capabilities_json,
            kind,
            grant.fencing_generation,
            grant.issued_at_us,
            grant.issued_monotonic_us,
            grant.expires_monotonic_us,
            settled_monotonic_us,
            claim_id,
            audit_ref,
            settled_at_us,
        ),
    )


def _find_claim(
    connection: sqlite3.Connection, grant: MutationGrant, key: str
) -> tuple[str, str, str] | None:
    """The claim as `(claim_id, request_digest, audit_ref)`, or `None`.

    The scope is the whole tuple 0007's unique index states -- workspace, validated
    principal, operation, key -- because a key is never global, and reading it any
    narrower would let one caller's recorded answer be served to another.
    """
    row = connection.execute(
        "SELECT claim_id, request_digest, audit_ref FROM omnivia_idempotency_claims "
        "WHERE workspace_id = ? AND principal_id = ? AND operation = ? "
        "AND idempotency_key = ?",
        (grant.workspace_id, grant.principal_id, grant.operation, key),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]))


def _replay(connection: sqlite3.Connection, claim_id: str) -> MutationOutcome:
    """The answer a claim already settled on, proved against the bytes it stored.

    "Deterministic because we wrote it canonically" is an assumption about the past, and
    a replay is exactly the path on which that assumption is worth nothing: the bytes
    have been at rest, and this seam is not the only thing that could have reached them.
    So every step is checked rather than trusted.

    The digest is verified over the exact stored UTF-8 bytes *before* anything decodes
    them, so a corrupted document is refused rather than parsed. The parse is the
    contract's own strict document parser, which refuses duplicate member names and the
    non-standard `NaN`/`Infinity` literals -- both of which `json.loads` would accept and
    silently flatten. The parsed value is then canonicalized under the contract's I-JSON
    and RFC 8785 rules and required to equal the stored text byte for byte, which is what
    catches a document whose whitespace, member order, number rendering or escaping is
    not the canonical one, and what makes a lossy integer or a lone surrogate a refusal.
    Anything other than a JSON object is refused too: an operation result is a mapping.

    A claim with no terminal outcome cannot happen through this seam -- both are written
    in one transaction -- so finding one means the database was written by something
    else. Refusing is the only safe answer; inventing a result would be worse.

    The audit link is read from the claim rather than from the outcome. 0007 foreign-keys
    the outcome's `claim_id` and `audit_ref` independently, by workspace, and never
    requires them to agree, so an outcome carrying this claim's answer under a *different*
    mutation's audit event satisfies every constraint 0007 states -- and its digest, its
    canonical bytes and the two audit events' authority fields can all be honest. 0013's
    INSERT trigger refuses that row from here on; a row already at rest when this build
    upgraded is refused here, because the replayed answer is recorded against an audit ref
    and attributing it to the wrong event is not something to serve.
    """
    row = connection.execute(
        "SELECT o.outcome_json, o.outcome_reference, o.outcome_digest, "
        "       o.audit_ref, c.audit_ref, o.workspace_id "
        "FROM omnivia_idempotency_outcomes o "
        "JOIN omnivia_idempotency_claims c ON c.claim_id = o.claim_id "
        "WHERE o.claim_id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        raise MutationSeamFault(_MESSAGE_UNSETTLED_CLAIM)
    if str(row[3]) != str(row[4]):
        raise MutationSeamFault(_MESSAGE_OUTCOME_AUDIT_CROSSLINKED)
    if row[0] is not None:
        stored = str(row[0])
    elif row[1] is not None:
        document = connection.execute(
            "SELECT outcome_json, outcome_digest, outcome_byte_length, claim_id, audit_ref "
            "FROM omnivia_application_outcome_documents "
            "WHERE workspace_id = ? AND outcome_reference = ?",
            (str(row[5]), str(row[1])),
        ).fetchone()
        if (
            document is None
            or str(document[1]) != str(row[2])
            or str(document[3]) != claim_id
            or str(document[4]) != str(row[3])
        ):
            raise MutationSeamFault(_MESSAGE_OUTCOME_NOT_REPLAYABLE)
        stored = str(document[0])
        if len(stored.encode("utf-8")) != int(document[2]):
            raise MutationSeamFault(_MESSAGE_OUTCOME_NOT_REPLAYABLE)
    else:
        raise MutationSeamFault(_MESSAGE_UNSETTLED_CLAIM)
    if _digest(stored) != str(row[2]):
        raise MutationSeamFault(_MESSAGE_OUTCOME_NOT_REPLAYABLE)
    try:
        decoded = parse_json_document(stored)
        canonical = canonicalize(decoded)
    except ContractSemanticError as error:
        raise MutationSeamFault(_MESSAGE_OUTCOME_NOT_REPLAYABLE) from error
    if canonical != stored or not isinstance(decoded, dict):
        raise MutationSeamFault(_MESSAGE_OUTCOME_NOT_REPLAYABLE)
    return MutationOutcome(
        result=decoded, replayed=True, claim_id=claim_id, audit_ref=str(row[3])
    )


def _digest(canonical: str) -> str:
    """`sha256:<64 hex>` over the exact canonical bytes, as 0007's CHECK requires."""
    return (
        f"{CONTENT_CHECKSUM_ALGORITHM}:{sha256(canonical.encode('utf-8')).hexdigest()}"
    )


__all__ = [
    "CONTENT_INGESTION_PURPOSE",
    "DEFAULT_GRANT_LIFETIME_US",
    "INSTALLATION_ADMINISTRATOR_ROLE",
    "JOB_CONTROL_PURPOSE",
    "KNOWLEDGE_GOVERNANCE_PURPOSE",
    "KNOWLEDGE_REVIEWER_ROLE",
    "MEMORY_AUTHORING_PURPOSE",
    "MUTATING_OPERATIONS",
    "MUTATION_PURPOSES",
    "MUTATION_ROLES",
    "WORKSPACE_ADMINISTRATION_PURPOSE",
    "WORKSPACE_CONTRIBUTOR_ROLE",
    "DomainMutation",
    "MutationDenied",
    "MutationGrant",
    "MutationIdempotencyConflict",
    "MutationOutcome",
    "MutationPreconditionFailed",
    "MutationSeamFault",
    "MutationSettlementContext",
    "PreconditionReader",
    "ResultValidator",
    "execute_mutation",
    "issue_mutation_grant",
]
