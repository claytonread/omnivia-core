"""The production application dispatch path (V06-2 Lane A, PM ADR-038/ADR-039).

Before this module there were two accepted, reviewed, green seams that nothing
reached: `authorize_application_request` and `ApplicationOperationRegistry`. The only
live dispatch callable was the probe `Dispatcher`, which answers `core.health`,
`core.readiness` and `core.discovery` under a three-membership-test grant that
predates the catalogue and knows nothing about sessions, scopes, purposes,
capability versions or installation scope.

This module is the second path, and it is a second path rather than a widening of
the first. It exists so that a catalogue operation is decided by
`authorize_application_request` -- all twelve of its checks -- and by nothing else.
The binding clause the owner confirmed is what it implements:

    workspace.inspect must use a new production application-handler path. It must
    not be registered against or routed through the probe Dispatcher.

Selection between the two paths is by *registration*, not by a name written here: an
operation the application registry holds is decided by the application seam, and
everything else stays with the probe dispatcher, unchanged. Nothing can be answered
by both, because the registry is bounded by the frozen catalogue and the catalogue
and the probe operations are disjoint.

What this module deliberately does not do
-----------------------------------------
It builds no credential store, no token format, no account database and no principal
registry. There is exactly one principal in this process -- the one the probe grant
already names -- and this path narrows it rather than adding a second. The session is
constructed once, at startup, from facts the service already holds; no field of it
comes from a request, and there is no branch that skips the seam for any caller.

On what "authenticated" means here, stated as the owner required rather than as it
would flatter
--------------------------------------------------------------------------------
The principal is fixed by trusted installation-local service configuration. It is
**not** a verified operating-system peer identity, and nothing here -- no record
field, no message, no docstring -- claims that it is: this repository holds no
peer-credential primitive at all. What is true is that the caller reached a protected
local endpoint, and that access to that endpoint establishes permission to act as the
configured local-owner principal for this bounded Personal-mode vertical. The socket's
and descriptor's filesystem permissions are *channel trust*, which is why trusting the
configuration is reasonable; they are not proof of who connected, and they are not the
principal's source. Verified peer identity is the recorded deferral
`LOCAL-IPC-PEER-IDENTITY-DEFERRED`, and it is required before shared-host, multi-user
or Organisation-mode local deployment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeAlias, cast, runtime_checkable

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    SCOPE_KIND_INSTALLATION,
    CapabilityRef,
    OperationMetadata,
    RequestEnvelope,
    ResponseEnvelope,
    compare_contract_versions,
    get_operation_metadata,
)
from omnivia_core_runtime.ownership.identity import Clock, SystemClock
from omnivia_core_runtime.service.admission import (
    ALLOW_APPLICATION_REQUEST,
    ApplicationAdmissionPolicy,
)
from omnivia_core_runtime.service.authorization import (
    ApplicationAuthorizationError,
    AuthenticatedSession,
    AuthorizedApplicationContext,
    Grant,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.chat_submit import resolve_chat_command
from omnivia_core_runtime.service.handlers.chat import (
    CHAT_COMMAND_OPERATION,
    CHAT_EVENTS_OPERATION,
    CHAT_FAMILY_OPERATIONS,
    ChatCommandResolver,
    ChatHandlers,
)
from omnivia_core_runtime.service.handlers.context_pack import context_pack_build
from omnivia_core_runtime.service.handlers.evidence import evidence_search
from omnivia_core_runtime.service.handlers.governance import (
    GOVERNANCE_FAMILY_OPERATIONS,
    GovernanceHandlers,
)
from omnivia_core_runtime.service.handlers.graph import graph_traverse
from omnivia_core_runtime.service.handlers.jobs import (
    IMPORT_START_OPERATION,
    JOB_CANCEL_OPERATION,
    JOB_EVENTS_OPERATION,
    JOB_FAMILY_OPERATIONS,
    JOB_GET_OPERATION,
    JOB_RETRY_OPERATION,
    JobHandlers,
)
from omnivia_core_runtime.service.handlers.knowledge import (
    LOCAL_OWNER_VIEW_GRANT,
    knowledge_search,
    memory_search,
)
from omnivia_core_runtime.service.handlers.memory import (
    MEMORY_CREATE_OPERATION,
    MEMORY_FAMILY_OPERATIONS,
    MEMORY_GET_OPERATION,
    MEMORY_LIST_OPERATION,
    ContinuationTokenCodec,
    HmacContinuationTokenCodec,
    MemoryHandlers,
)
from omnivia_core_runtime.service.handlers.workspace import workspace_inspect
from omnivia_core_runtime.service.handlers.workspace_family import (
    InstallationWorkspaceHandlers,
)
from omnivia_core_runtime.service.installation import (
    WORKSPACE_CREATE_OPERATION,
    WORKSPACE_LIST_OPERATION,
    WORKSPACE_LIST_PURPOSE,
    InstallationApplicationService,
    InstallationOperationContext,
)
from omnivia_core_runtime.service.mutation import (
    INSTALLATION_ADMINISTRATOR_ROLE,
    KNOWLEDGE_REVIEWER_ROLE,
    MUTATION_PURPOSES,
    WORKSPACE_CONTRIBUTOR_ROLE,
)
from omnivia_core_runtime.service.operations import (
    ApplicationOperationRegistry,
    AuditedOperationResult,
    OperationContext,
    OperationError,
    OperationHandler,
    failure,
    server_capability_snapshot,
    success,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier
from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

#: The authorised application operations of this vertical, ratified by the owner. Named
#: once here so the session grant, the registry and the evidence all read the same
#: string, and so widening the grant is one visible edit rather than a drift.
WORKSPACE_INSPECT_OPERATION: Final = "workspace.inspect"
EVIDENCE_SEARCH_OPERATION: Final = "evidence.search"
KNOWLEDGE_SEARCH_OPERATION: Final = "knowledge.search"
MEMORY_SEARCH_OPERATION: Final = "memory.search"
GRAPH_TRAVERSE_OPERATION: Final = "graph.traverse"
CONTEXT_PACK_BUILD_OPERATION: Final = "context_pack.build"

#: The purpose each granted operation is served under. There is no purpose registry in
#: the contract -- purposes are pattern-validated at the boundary and then checked
#: against `session.purposes` -- so the session's set *is* the allowlist, and this map
#: is what fixes it.
#:
#: Two entries, and that is the whole V06-3 vocabulary (§20.2): `workspace.inspect`
#: **retains** `workspace_inspection` rather than migrating, and one purpose,
#: `knowledge_retrieval`, covers all five V06-3 read and Context Pack composition
#: operations. Not one purpose per operation -- the owner corrected §6.2's constraint 4
#: to exactly this shape.
#:
#: This is the only knob in this file that is *policy* rather than catalogue-derived,
#: and it is a map rather than a set because a session that granted an operation with
#: no purpose behind it would be refused by check 11 at the first request. A lane
#: widening the grant adds the operation here in the same edit or its own operation
#: cannot be served.
#:
#: This map covers the *read* operations only, and deliberately so: it is consulted by
#: `local_owner_session`, which refuses a side-effecting operation outright. The
#: mutating half of the catalogue is declared by `service.mutation.MUTATION_PURPOSES`,
#: where a purpose is a fact bound into a server-issued grant rather than a session
#: allowlist entry, and where nothing may be served without one.
WORKSPACE_INSPECTION_PURPOSE: Final = "workspace_inspection"
KNOWLEDGE_RETRIEVAL_PURPOSE: Final = "knowledge_retrieval"
OPERATION_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        WORKSPACE_INSPECT_OPERATION: WORKSPACE_INSPECTION_PURPOSE,
        EVIDENCE_SEARCH_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
        KNOWLEDGE_SEARCH_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
        MEMORY_SEARCH_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
        GRAPH_TRAVERSE_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
        CONTEXT_PACK_BUILD_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
    }
)

#: Installation-scoped operations are served under a distinct session.  This is
#: intentionally not folded into ``OPERATION_PURPOSES``: the existing local-owner
#: session is workspace-bound and structurally refuses mutations, while this map
#: includes one read and one S0-authorized mutation.
INSTALLATION_OPERATION_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        WORKSPACE_CREATE_OPERATION: MUTATION_PURPOSES[WORKSPACE_CREATE_OPERATION],
        WORKSPACE_LIST_OPERATION: WORKSPACE_LIST_PURPOSE,
    }
)

MEMORY_FAMILY_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        MEMORY_CREATE_OPERATION: MUTATION_PURPOSES[MEMORY_CREATE_OPERATION],
        MEMORY_GET_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
        MEMORY_LIST_OPERATION: KNOWLEDGE_RETRIEVAL_PURPOSE,
    }
)

JOB_OBSERVATION_PURPOSE: Final = "job_observation"
JOB_FAMILY_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        IMPORT_START_OPERATION: MUTATION_PURPOSES[IMPORT_START_OPERATION],
        JOB_GET_OPERATION: JOB_OBSERVATION_PURPOSE,
        JOB_CANCEL_OPERATION: MUTATION_PURPOSES[JOB_CANCEL_OPERATION],
        JOB_RETRY_OPERATION: MUTATION_PURPOSES[JOB_RETRY_OPERATION],
        JOB_EVENTS_OPERATION: JOB_OBSERVATION_PURPOSE,
    }
)

GOVERNANCE_FAMILY_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {name: MUTATION_PURPOSES[name] for name in GOVERNANCE_FAMILY_OPERATIONS}
)

#: The W2-F2 Chat family. Two purposes rather than one, on the same split the job
#: family uses: the mutation is served under the purpose `MUTATION_PURPOSES` declares
#: for it -- which is the only purpose a grant for it can ever carry -- and the durable
#: event replay is an observation, which is not what a caller authors a conversation
#: under.
CHAT_OBSERVATION_PURPOSE: Final = "chat_observation"
CHAT_FAMILY_PURPOSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        CHAT_COMMAND_OPERATION: MUTATION_PURPOSES[CHAT_COMMAND_OPERATION],
        CHAT_EVENTS_OPERATION: CHAT_OBSERVATION_PURPOSE,
    }
)

#: Where the principal comes from, recorded verbatim as the owner fixed it. Not the
#: operating-system identity this service runs as, and not anything a request carries.
PRINCIPAL_SOURCE: Final = "installation-local service configuration"

#: Why trusting that configuration is reasonable. This is a statement about the
#: *channel*, and it is deliberately the strongest honest one available: the caller
#: reached a protected endpoint. It is not an assertion that the peer was
#: authenticated, because no such check exists here (LOCAL-IPC-PEER-IDENTITY-DEFERRED).
CHANNEL_TRUST: Final = (
    "protected local IPC endpoint and operating-system filesystem permissions"
)

#: The transport adapter this path is served behind, stated at the wiring site rather
#: than inferred, because the dispatch callable is handed a request and nothing else.
LOCAL_TRANSPORT_ADAPTER: Final = "local-ovc1"

#: A build fault, not a caller's mistake: the seam authorized an operation this build
#: has no handler for. Frozen, and carrying no value, for the same reason every
#: refusal message on this path is.
_MESSAGE_NO_HANDLER: Final = "this build cannot serve an operation it authorized"


def _narrow_session(
    configured: AuthenticatedSession,
    caller: AuthenticatedSession,
) -> AuthenticatedSession:
    """Intersect caller authority with the endpoint's configured maximum."""
    if configured.principal_id != caller.principal_id:
        return AuthenticatedSession(principal_id=caller.principal_id)
    configured_capabilities = {ref.id: ref.version for ref in configured.capabilities}
    capabilities = tuple(
        CapabilityRef(
            id=ref.id,
            version=(
                ref.version
                if compare_contract_versions(
                    ref.version, configured_capabilities[ref.id]
                )
                <= 0
                else configured_capabilities[ref.id]
            ),
        )
        for ref in caller.capabilities
        if ref.id in configured_capabilities
    )
    return AuthenticatedSession(
        principal_id=caller.principal_id,
        roles=caller.roles & configured.roles,
        installations=caller.installations & configured.installations,
        workspaces=caller.workspaces & configured.workspaces,
        operations=caller.operations & configured.operations,
        scopes=caller.scopes & configured.scopes,
        purposes=caller.purposes & configured.purposes,
        capabilities=capabilities,
    )


def local_owner_session(
    *,
    principal_id: str,
    installation_id: str,
    workspace_id: str,
    operations: frozenset[str],
) -> AuthenticatedSession:
    """The session this endpoint serves every application request under.

    Built once, at startup, from three facts the service process already holds -- the
    principal fixed by installation-local service configuration, the installation
    identity persisted under the installation-local state root, and the single
    workspace this endpoint was launched to own -- plus the operation set its caller
    names. Nothing here is read from a request, and this function is never called from
    inside a handler.

    **Why `operations` is a parameter, and what it is not.** V06-2 baked
    `workspace.inspect` into this constructor as a module-level `Final`, which was
    correct while the grant was one name and became a false constraint the moment the
    accepted grant was two. The owner accepted the parameterised shape (§20.2) with the
    binding constraint that **the operation set is a literal in the constructor's
    caller** -- never a request field, and never `APPLICATION_OPERATIONS`, which is all
    twenty and would hand a read-only local owner every mutation in the catalogue. The
    production literal is at `service/main.py`'s `serve`, where the grant is visible at
    the wiring site rather than buried one module away.

    The grant is also *incremental per lane* rather than decided once: *"Grant only
    operations whose handlers are implemented and accepted."* A session naming an
    operation this build has no handler for is a grant with nothing behind it.

    Every grant is stated, because `AuthenticatedSession` defaults each one to empty and
    an unstated grant refuses. Every grant is also narrow on purpose:

    * `roles` is empty. There is no role model, so any claimed role is refused --
      claims narrow, and there is nothing here to narrow to.
    * `operations` is exactly what the caller named, after this function has refused
      any name the catalogue does not know and any name with a side effect.
    * `scopes` and `capabilities` are **derived** from each granted operation's frozen
      catalogue entry, never transcribed. A literal `"memory:read"` or `"evidence.read"`
      appearing in this constructor is a stop condition (§20.2), and this is the
      property that makes the session structurally incapable of drifting from the
      catalogue: widening the grant widens the scopes and capabilities with it, in the
      same expression, or not at all.
    * `purposes` is derived from `OPERATION_PURPOSES`, which is policy rather than
      catalogue -- the contract declares no purposes -- and is the one thing here a
      reviewer has to check against the owner's table rather than against the tree.

    The single granted workspace is also stated a second time, independently, on the
    `ServiceBinding` the caller of this function builds. That is deliberate: reaching a
    workspace-specific endpoint and naming a different workspace is refused by the
    binding even if a session grant were ever widened.

    Raises `ValueError` on a grant this function may not build. Both refusals are
    construction-time and neither is reachable through a request: a wiring that names an
    operation with `side_effect != "none"` is refused before the endpoint serves,
    because a read-only local owner holding a mutation is the failure this whole path
    exists to prevent and discovering it at the first mutating request would be too
    late.
    """
    if not operations:
        raise ValueError(
            "a session granting no operation cannot serve anything; state the grant"
        )

    entries = tuple(get_operation_metadata(name) for name in sorted(operations))

    # Asserted from the catalogue, never from the literal list, so this is a property of
    # what was granted rather than a restatement of it (§6.2 constraint 3). One line,
    # and it makes the parameterised shape structurally unable to grant a mutation.
    mutating = tuple(entry.name for entry in entries if entry.scope.side_effect != "none")
    if mutating:
        raise ValueError(
            "a local-owner session grants read-only operations; "
            f"{mutating!r} declare a side effect"
        )

    missing_purpose = tuple(
        entry.name for entry in entries if entry.name not in OPERATION_PURPOSES
    )
    if missing_purpose:
        raise ValueError(
            "every granted operation needs a declared purpose; "
            f"{missing_purpose!r} have none"
        )

    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset(),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=frozenset(entry.name for entry in entries),
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(OPERATION_PURPOSES[entry.name] for entry in entries),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def installation_owner_session(
    *,
    principal_id: str,
    installation_id: str,
) -> AuthenticatedSession:
    """The separate S1 grant for one owned installation catalogue.

    It is intentionally not a widening of :func:`local_owner_session`: the latter
    remains read-only and workspace-bound.  The one mutation here is named
    literally, carries the S0-required installation-administrator role, and is
    executable only through the installation service that owns the matching
    catalogue authority.
    """
    operations = frozenset(INSTALLATION_OPERATION_PURPOSES)
    entries = tuple(get_operation_metadata(name) for name in sorted(operations))
    if any(entry.scope.scope_kind != SCOPE_KIND_INSTALLATION for entry in entries):
        raise ValueError("an installation-owner session may grant only installation scope")
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
        installations=frozenset({installation_id}),
        workspaces=frozenset(),
        operations=operations,
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(INSTALLATION_OPERATION_PURPOSES.values()),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def memory_family_session(
    *, principal_id: str, installation_id: str, workspace_id: str
) -> AuthenticatedSession:
    """The distinct S2 workspace-family grant; the read-only session stays unchanged."""
    entries = tuple(
        get_operation_metadata(name) for name in sorted(MEMORY_FAMILY_OPERATIONS)
    )
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset({WORKSPACE_CONTRIBUTOR_ROLE}),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=MEMORY_FAMILY_OPERATIONS,
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(MEMORY_FAMILY_PURPOSES.values()),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def build_memory_registry(handlers: MemoryHandlers) -> ApplicationOperationRegistry:
    registry = ApplicationOperationRegistry()
    registry.register(
        MEMORY_CREATE_OPERATION, cast(OperationHandler, handlers.memory_create)
    )
    registry.register(MEMORY_GET_OPERATION, cast(OperationHandler, handlers.memory_get))
    registry.register(
        MEMORY_LIST_OPERATION, cast(OperationHandler, handlers.memory_list)
    )
    return registry


def job_family_session(
    *, principal_id: str, installation_id: str, workspace_id: str
) -> AuthenticatedSession:
    """The distinct S3 workspace-family grant for durable import and job control."""
    entries = tuple(get_operation_metadata(name) for name in sorted(JOB_FAMILY_OPERATIONS))
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset({WORKSPACE_CONTRIBUTOR_ROLE}),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=JOB_FAMILY_OPERATIONS,
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(JOB_FAMILY_PURPOSES.values()),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def build_job_registry(handlers: JobHandlers) -> ApplicationOperationRegistry:
    registry = ApplicationOperationRegistry()
    registry.register(IMPORT_START_OPERATION, cast(OperationHandler, handlers.import_start))
    registry.register(JOB_GET_OPERATION, cast(OperationHandler, handlers.job_get))
    registry.register(JOB_CANCEL_OPERATION, cast(OperationHandler, handlers.job_cancel))
    registry.register(JOB_RETRY_OPERATION, cast(OperationHandler, handlers.job_retry))
    registry.register(JOB_EVENTS_OPERATION, cast(OperationHandler, handlers.job_events))
    return registry


def governance_family_session(
    *, principal_id: str, installation_id: str, workspace_id: str
) -> AuthenticatedSession:
    """The S4 contributor/reviewer grant for governed transitions."""
    entries = tuple(
        get_operation_metadata(name) for name in sorted(GOVERNANCE_FAMILY_OPERATIONS)
    )
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset({WORKSPACE_CONTRIBUTOR_ROLE, KNOWLEDGE_REVIEWER_ROLE}),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=GOVERNANCE_FAMILY_OPERATIONS,
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(GOVERNANCE_FAMILY_PURPOSES.values()),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def build_governance_registry(
    handlers: GovernanceHandlers,
) -> ApplicationOperationRegistry:
    registry = ApplicationOperationRegistry()
    registry.register("knowledge.propose", cast(OperationHandler, handlers.knowledge_propose))
    registry.register("candidate.approve", cast(OperationHandler, handlers.candidate_approve))
    registry.register("candidate.reject", cast(OperationHandler, handlers.candidate_reject))
    registry.register("record.supersede", cast(OperationHandler, handlers.record_supersede))
    return registry


def chat_family_session(
    *, principal_id: str, installation_id: str, workspace_id: str
) -> AuthenticatedSession:
    """The W2-F2 contributor grant for one workspace's Chat surface."""
    entries = tuple(
        get_operation_metadata(name) for name in sorted(CHAT_FAMILY_OPERATIONS)
    )
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset({WORKSPACE_CONTRIBUTOR_ROLE}),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=CHAT_FAMILY_OPERATIONS,
        scopes=frozenset(
            scope for entry in entries for scope in entry.scope.required_scopes
        ),
        purposes=frozenset(CHAT_FAMILY_PURPOSES.values()),
        capabilities=tuple(
            sorted(
                {
                    CapabilityRef(
                        id=entry.required_capability.id,
                        version=entry.required_capability.minimum_version,
                    )
                    for entry in entries
                },
                key=lambda ref: (ref.id, ref.version),
            )
        ),
    )


def build_chat_registry(handlers: ChatHandlers) -> ApplicationOperationRegistry:
    registry = ApplicationOperationRegistry()
    registry.register(
        CHAT_COMMAND_OPERATION, cast(OperationHandler, handlers.chat_command)
    )
    registry.register(
        CHAT_EVENTS_OPERATION, cast(OperationHandler, handlers.chat_events)
    )
    return registry


def build_application_registry(
    *, additional: Mapping[str, OperationHandler] | None = None
) -> ApplicationOperationRegistry:
    """The application handlers this build ships.

    Six entries. `ApplicationOperationRegistry` is bounded by the frozen catalogue and
    fails closed on anything else, so this cannot register a name A2 did not freeze,
    and it registers nothing into the probe registry.

    This function and the production session grant have to agree, and they are two
    separate statements rather than one derived from the other on purpose: registering a
    handler is a claim about what this build can *serve*, and the grant is a claim about
    what the local owner may *ask for*. Deriving either from the other would make a
    mistake in one invisible in the other.

    **`additional` is a test seam and is production-inert.** A lane building the
    mutation path needs a registry holding a handler this build does not ship, and the
    alternative -- reaching in and rebinding a module attribute -- makes the production
    result depend on whatever a previous test left behind. Passing the extra
    registrations in keeps the default result exactly the six read handlers, whoever
    called this function before; the production wiring in `service/main.py` states no
    argument and therefore cannot acquire one by accident.

    Nothing is relaxed for an injected handler: it goes through the same `register`,
    so a name outside the frozen catalogue and a name already registered here both fail
    closed, and a caller cannot use this to *replace* a shipped read handler.
    """
    registry = ApplicationOperationRegistry()
    registry.register(WORKSPACE_INSPECT_OPERATION, workspace_inspect)
    registry.register(EVIDENCE_SEARCH_OPERATION, evidence_search)
    registry.register(KNOWLEDGE_SEARCH_OPERATION, knowledge_search)
    registry.register(MEMORY_SEARCH_OPERATION, memory_search)
    registry.register(GRAPH_TRAVERSE_OPERATION, graph_traverse)
    registry.register(CONTEXT_PACK_BUILD_OPERATION, context_pack_build)
    for operation, handler in (additional or {}).items():
        registry.register(operation, handler)
    return registry


InstallationOperationHandler: TypeAlias = Callable[
    [InstallationOperationContext], Mapping[str, Any] | AuditedOperationResult
]


def build_installation_registry(
    *,
    workspace_create: InstallationOperationHandler,
    workspace_list: InstallationOperationHandler,
) -> ApplicationOperationRegistry:
    """The S1 installation family, separate from the workspace read registry.

    Keeping this registry distinct is what preserves the shared-session boundary:
    the workspace application dispatcher cannot acquire the create handler merely
    because the build ships it, and the installation session cannot intercept any
    of the six existing workspace reads.
    """
    registry = ApplicationOperationRegistry()
    # The registry predates installation scope and names its transport-neutral
    # callable with the workspace context type. The application dispatcher selects
    # the context from frozen catalogue scope before invocation, so this cast adapts
    # only that legacy annotation; it does not widen runtime authority.
    registry.register(WORKSPACE_CREATE_OPERATION, cast(OperationHandler, workspace_create))
    registry.register(WORKSPACE_LIST_OPERATION, cast(OperationHandler, workspace_list))
    return registry


@dataclass(frozen=True)
class ApplicationCallRecord:
    """Everything the caller-recording requirement asks be recorded for one request.

    Assembled from the `AuthorizedApplicationContext` where there is one, because that
    value is the seam's own record of what the request actually ran under, and
    re-deriving any of it here would let the record and the decision disagree.

    On a refusal there is no context -- the seam raises before building one -- so the
    caller-supplied fields fall back to what the request *claimed*, read defensively
    and recorded as claims. That is safe and is required: this record is server-side,
    and none of it travels back to the caller in the refusal. The server facts
    (`principal_id`, `principal_source`, `channel_trust`, `transport`, `operation` and
    the catalogue's audit metadata) are the same on both outcomes, because none of
    them was ever the caller's to state.

    No bearer credential can reach this value: `AuthenticatedSession` carries none by
    construction, none reaches the seam that builds the context, and nothing here adds
    one.

    `channel_trust` records how the caller reached this endpoint. It is not, and must
    never become, an assertion that the peer was authenticated.
    """

    transport: str
    operation: str
    principal_id: str
    principal_source: str
    channel_trust: str
    workspace_id: str | None
    capabilities: tuple[CapabilityRef, ...]
    purpose: str | None
    scopes: tuple[str, ...]
    client_id: str | None
    client_version: str | None
    request_id: str | None
    correlation_id: str | None
    trace_id: str | None
    audited: bool
    #: `None` only where the catalogue declares an operation unaudited. It is not
    #: `None` for anything this build registers, and a record whose `audited` is true
    #: and whose category is `None` would be a catalogue defect rather than a gap here.
    audit_category: str | None
    allowed: bool
    error_code: str | None
    retry_class: str | None


#: Where a record goes. A callable rather than a logger: this repository configures no
#: logging at all, and inventing an observability substrate under this packet would be
#: a surface with no design behind it. The wiring states `None` explicitly so the
#: absence is a visible choice rather than a forgotten argument.
ApplicationCallSink: TypeAlias = Callable[[ApplicationCallRecord], None]


class ApplicationFallback(Protocol):
    """The next dispatch layer and its already-cross-checked principal grant."""

    @property
    def grant(self) -> Grant: ...

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope: ...


@runtime_checkable
class SessionApplicationFallback(Protocol):
    """A fallback that can retain transport-resolved caller authority."""

    def dispatch_for_session(
        self,
        request: RequestEnvelope,
        session: AuthenticatedSession,
    ) -> ResponseEnvelope: ...


@dataclass(frozen=True)
class ProductionApplicationSurface:
    """The complete application surface, composed without collapsing authority.

    The five family dispatchers deliberately retain their own server-issued
    sessions and bindings: an installation-scoped request must never inherit a
    workspace grant, and a read must never acquire mutation authority merely
    because both operations ship in one build.  This object is the single
    production routing surface above those boundaries.  Its ``registry`` is the
    exact, duplicate-checked union used for capability and release evidence; a
    request is then routed to the one family that owns that registered handler.

    A handler is registered twice, absent, or outside the frozen catalogue is a
    construction error.  The resulting surface therefore cannot start while it
    is anything other than 22/22 complete.
    """

    registry: ApplicationOperationRegistry
    _routes: Mapping[str, ApplicationDispatcher]
    _principal: str
    probe: ApplicationFallback
    adapters: frozenset[str]

    def __post_init__(self) -> None:
        self.registry.assert_complete()
        routes = dict(self._routes)
        if frozenset(routes) != self.registry.operations:
            raise ValueError(
                "the production application routes do not exactly match the registry"
            )
        distinct_routes = tuple({id(route): route for route in routes.values()}.values())
        if len(distinct_routes) != 6:
            raise ValueError("the production surface requires exactly six authority families")
        if any(route.grant.principal != self._principal for route in distinct_routes):
            raise ValueError("every production application family must act as one principal")
        if any(route.probe.grant.principal != self._principal for route in distinct_routes):
            raise ValueError("every production application fallback must keep one principal")
        if self.probe.grant.principal != self._principal:
            raise ValueError("the production application surface and probe disagree on principal")
        if self.adapters != frozenset({"in_process", "ipc", "http"}):
            raise ValueError(
                "the production application surface requires in_process, ipc and http"
            )
        object.__setattr__(self, "_routes", MappingProxyType(routes))

    @property
    def grant(self) -> Grant:
        """Expose the one acting principal for transport wiring checks."""
        return self.probe.grant

    def session_for(self, operation: str) -> AuthenticatedSession | None:
        """Return the server session for one registered operation.

        HTTP credential resolvers need the authority appropriate to the request,
        not whichever family happened to be composed last.  Returning ``None``
        for an unregistered name keeps that lookup fail-closed.
        """
        route = self._routes.get(operation)
        return None if route is None else route.session

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
        route = self._routes.get(request.operation)
        if route is None:
            return self.probe.dispatch(request)
        return route.dispatch(request)

    def dispatch_for_session(
        self,
        request: RequestEnvelope,
        session: AuthenticatedSession,
    ) -> ResponseEnvelope:
        """Dispatch one request under authority resolved by its transport."""
        route = self._routes.get(request.operation)
        if route is None:
            return self.probe.dispatch(request)
        return route.dispatch_for_session(request, session)


def compose_production_application_surface(
    *,
    installation: ApplicationDispatcher,
    reads: ApplicationDispatcher,
    memory: ApplicationDispatcher,
    jobs: ApplicationDispatcher,
    governance: ApplicationDispatcher,
    chat: ApplicationDispatcher,
    probe: ApplicationFallback,
    adapters: frozenset[str] = frozenset({"in_process", "ipc", "http"}),
) -> ProductionApplicationSurface:
    """Compose all real family handlers into the exact frozen catalogue."""
    families = (installation, reads, memory, jobs, governance, chat)
    registry = ApplicationOperationRegistry()
    routes: dict[str, ApplicationDispatcher] = {}
    for family in families:
        for operation in sorted(family.registry.operations):
            handler = family.registry.get(operation)
            if handler is None:
                raise ValueError(f"production operation {operation!r} has no handler")
            registry.register(operation, handler)
            routes[operation] = family
    registry.assert_complete()
    return ProductionApplicationSurface(
        registry=registry,
        _routes=routes,
        _principal=probe.grant.principal,
        probe=probe,
        adapters=adapters,
    )


def _claimed_text(value: object, name: str) -> str | None:
    """One string field a request claims, or `None` where it claims nothing readable.

    Total by construction. A refusal record is built from a request that was refused
    precisely because it may not be the shape the contract describes, so every read of
    it has to survive an integer request id or a missing client identity rather than
    turning a refusal into a crash inside the recording path.
    """
    claimed = getattr(value, name, None)
    return claimed if isinstance(claimed, str) else None


@dataclass(frozen=True)
class ApplicationDispatcher:
    """The production application path: one request, twelve checks, one handler.

    `probe` is the existing probe dispatcher, held rather than replaced. Anything the
    application registry does not hold is passed to it untouched, so `core.health`,
    `core.readiness` and `core.discovery` keep exactly the grant, the semantics and
    the error codes they had.

    `supported_capabilities` is this server's own snapshot of what this build supports
    and is supplied by the wiring, never computed from a request. That is the whole
    point of it being a field: the per-response `CapabilitySet` the response builder
    fabricates is derived from the caller's own `api_version`, and passing that here
    would make the seam's twelfth check a mirror of the caller's claim.

    `record` is the caller-recording sink, and `service` is the workspace-owning
    service a handler reads its facts from. Neither is authority: nothing on this path
    consults either one to decide anything.
    """

    registry: ApplicationOperationRegistry
    session: AuthenticatedSession
    binding: ServiceBinding
    supported_capabilities: tuple[CapabilityRef, ...]
    transport: str
    probe: ApplicationFallback
    record: ApplicationCallSink | None
    service: Any = None
    admission: ApplicationAdmissionPolicy = ALLOW_APPLICATION_REQUEST

    def __post_init__(self) -> None:
        """Refuse a wiring whose two halves act as different principals.

        The HTTP adapter cross-checks the principal it was declared against the one it
        can read off the dispatch callable's own object, and refuses a disagreement --
        because a wiring that admits sessions for one principal and executes them as
        another silently reopens exactly the hole that check closes. This path is now
        the object that check reads, so the disagreement is refused here, at
        construction, rather than left to be discovered by whichever adapter happens
        to look.
        """
        if self.session.principal_id != self.probe.grant.principal:
            raise ValueError(
                "the application session and the probe grant name different "
                "principals; this service instance acts as one principal, and a "
                "wiring that states two cannot say which a request ran as"
            )

    @property
    def grant(self) -> Grant:
        """The probe grant, so an adapter can still read who this dispatcher acts as.

        Both halves of this object act as the same principal -- `__post_init__` refuses
        any other wiring -- so answering with the probe grant is a true answer for the
        whole object rather than a convenient half of one.
        """
        return self.probe.grant

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
        """Dispatch with this endpoint's server-issued authenticated session."""
        return self._dispatch(request, session=self.session)

    def dispatch_for_session(
        self,
        request: RequestEnvelope,
        session: AuthenticatedSession,
    ) -> ResponseEnvelope:
        """Dispatch under a transport-resolved caller session.

        The configured session remains the server's maximum handler policy; the
        supplied session is the caller authority used by the authorization seam.
        Mutation grants therefore require both rather than inheriting the broader
        configured session merely because the request arrived over HTTP.
        """
        if request.operation not in self.registry:
            if isinstance(self.probe, SessionApplicationFallback):
                return self.probe.dispatch_for_session(request, session)
            return self.probe.dispatch(request)
        return self._dispatch(
            request,
            session=_narrow_session(self.session, session),
        )

    def dispatch_without_session(self, request: RequestEnvelope) -> ResponseEnvelope:
        """Dispatch after a trusted session lookup returned no authenticated caller.

        Transport adapters normally refuse before this point. In-process embedders do
        not necessarily have a separate HTTP-like authentication boundary, so this is
        the explicit fail-closed entry point that preserves the contract's typed
        ``authentication_required`` result without inventing an anonymous session.
        """
        return self._dispatch(request, session=None)

    def _dispatch(
        self,
        request: RequestEnvelope,
        *,
        session: AuthenticatedSession | None,
    ) -> ResponseEnvelope:
        """Answer one request, or refuse it in the contract's own vocabulary.

        Authorization comes first and has no bypass. In particular the handler lookup
        happens *after* the seam, not before: looking one up first is what lets an
        unregistered operation be answered without ever being authorized, which is the
        probe dispatcher's shape and precisely what this path exists not to be.
        """
        operation = request.operation
        if operation not in self.registry:
            # Not this path's operation. The probe seam keeps its own three names and
            # decides them under its own grant; nothing catalogue-shaped reaches it,
            # because the registry is bounded by the catalogue and the two sets are
            # disjoint.
            return self.probe.dispatch(request)

        # Registry membership already proved this name is a frozen catalogue entry, so
        # the catalogue lookup cannot fail and the audit metadata below is a server
        # fact rather than something read out of the request.
        entry = get_operation_metadata(operation)

        try:
            context = authorize_application_request(
                request,
                session=session,
                binding=self.binding,
                supported_capabilities=self.supported_capabilities,
            )
        except ApplicationAuthorizationError as denied:
            self._emit(request, entry, context=None, error=denied)
            # The seam's message is one of its frozen constants and carries no value at
            # all; it is passed through unchanged rather than re-worded, so a caller
            # sees the category the seam named and nothing this path added to it.
            return failure(
                request,
                denied.code,
                denied.message,
                retry_class=denied.retry_class,
                principal=self.probe.grant.principal,
            )

        # Authentication is the first authorization check, so a successful return
        # proves the explicit per-call session exists.
        assert session is not None

        admission_error = self.admission.evaluate(context)
        if admission_error is not None:
            self._emit(request, entry, context=context, error=admission_error)
            return failure(
                request,
                admission_error.code,
                admission_error.message,
                retry_class=admission_error.retry_class,
                principal=context.principal_id,
                audit_reference=admission_error.audit_reference,
                job_reference=admission_error.job_reference,
            )

        handler = self.registry.get(context.operation)
        workspace_id = context.workspace_id
        if handler is None:
            # A registry entry without a handler is an impossible construction state,
            # but it is answered structurally rather than asserted through a request.
            self._emit(
                request,
                entry,
                context=context,
                error=None,
                code=ERROR_CODE_INTERNAL_NON_RECOVERABLE,
            )
            return failure(
                request,
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                _MESSAGE_NO_HANDLER,
                principal=context.principal_id,
            )

        try:
            if entry.scope.scope_kind == SCOPE_KIND_INSTALLATION:
                handler_context: Any = InstallationOperationContext(
                    request=request,
                    principal=context.principal_id,
                    installation_id=context.installation_id,
                    granted_operations=session.operations,
                    authorization=context,
                    service=self.service,
                    authority=context.authority,
                    scopes=context.scopes,
                    purpose=context.purpose,
                )
            elif workspace_id is not None:
                handler_context = OperationContext(
                    request=request,
                    # The authenticated principal and the authorized workspace, not the
                    # claimed ones. A handler cannot see a claim at all.
                    principal=context.principal_id,
                    workspace_id=workspace_id,
                    granted_operations=session.operations,
                    service=self.service,
                    # Amendment 009's pass-through, and nothing more than a pass-through:
                    # these are the values the seam *returned*, carried across unchanged.
                    # Not the session's grant, not the catalogue's requirement, not the
                    # request's claim and not a constant -- each of those is a different
                    # set from the effective one, and `context_pack.build` writes what it
                    # is handed here into a signed artifact. Reconstructing any of them
                    # would attest an authority no seam ever granted.
                    authority=context.authority,
                    scopes=context.scopes,
                    purpose=context.purpose,
                    authorization=context,
                )
            else:
                self._emit(
                    request,
                    entry,
                    context=context,
                    error=None,
                    code=ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                )
                return failure(
                    request,
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                    _MESSAGE_NO_HANDLER,
                    principal=context.principal_id,
                )
            result: Mapping[str, Any] | AuditedOperationResult = handler(
                handler_context
            )
        except OperationError as error:
            self._emit(request, entry, context=context, error=error)
            return failure(
                request,
                error.code,
                error.message,
                retry_class=error.retry_class,
                principal=context.principal_id,
                audit_reference=error.audit_reference,
                job_reference=error.job_reference,
            )

        audit_reference: str | None = None
        canonical_resolution_time: str | None = None
        job_reference = None
        if isinstance(result, AuditedOperationResult):
            audit_reference = result.audit_reference
            canonical_resolution_time = result.canonical_resolution_time
            job_reference = result.job_reference
            result = result.result
        self._emit(request, entry, context=context, error=None)
        return success(
            request,
            result,
            principal=context.principal_id,
            # The *effective* capability refs the seam computed -- at the weaker of
            # granted and supported -- rather than the response builder's default of
            # one fabricated ref per granted operation name. An operation name is not a
            # capability id, and advertising it as one would be a false statement about
            # authority on the first production application response.
            capabilities=context.capabilities,
            audit_reference=audit_reference,
            canonical_resolution_time=canonical_resolution_time,
            job_reference=job_reference,
        )

    def _emit(
        self,
        request: RequestEnvelope,
        entry: OperationMetadata,
        *,
        context: AuthorizedApplicationContext | None,
        error: ApplicationAuthorizationError | OperationError | None,
        code: str | None = None,
    ) -> None:
        """Build this request's record and hand it to the sink, if there is one."""
        if self.record is None:
            return
        metadata = request.metadata
        client = getattr(metadata, "client", None)
        self.record(
            ApplicationCallRecord(
                transport=self.transport,
                operation=entry.name,
                principal_id=self.probe.grant.principal,
                principal_source=PRINCIPAL_SOURCE,
                channel_trust=CHANNEL_TRUST,
                workspace_id=(
                    _claimed_text(metadata, "workspace_id")
                    if context is None
                    else context.workspace_id
                ),
                capabilities=() if context is None else context.capabilities,
                purpose=(
                    _claimed_text(metadata, "purpose")
                    if context is None
                    else context.purpose
                ),
                scopes=() if context is None else context.scopes,
                client_id=(
                    _claimed_text(client, "id")
                    if context is None
                    else context.client.id
                ),
                client_version=(
                    _claimed_text(client, "version")
                    if context is None
                    else context.client.version
                ),
                request_id=(
                    _claimed_text(metadata, "request_id")
                    if context is None
                    else context.request_id
                ),
                correlation_id=(
                    _claimed_text(metadata, "correlation_id")
                    if context is None
                    else context.correlation_id
                ),
                trace_id=(
                    _claimed_text(metadata, "trace_id")
                    if context is None
                    else context.trace_id
                ),
                audited=entry.audit.audited,
                audit_category=entry.audit.audit_category,
                allowed=context is not None,
                error_code=code if error is None else error.code,
                retry_class=None if error is None else error.retry_class,
            )
        )


def build_installation_application_dispatcher(
    *,
    service: InstallationApplicationService,
    principal_id: str,
    fallback: ApplicationFallback,
    transport: str = LOCAL_TRANSPORT_ADAPTER,
    record: ApplicationCallSink | None = None,
) -> ApplicationDispatcher:
    """Compose the production S1 path around one installation-owned service.

    The caller owns the installation catalogue lifecycle and supplies the next
    dispatch layer for names outside the S1 registry. This function owns every
    security-sensitive composition choice: the installation-only session and
    binding, the concrete handlers, the exact two-operation registry, and the
    capability snapshot derived from that registry. It deliberately does not attach
    the installation lifetime lock to a per-workspace service process.
    """
    installation_id = service.authority.installation_id
    session = installation_owner_session(
        principal_id=principal_id,
        installation_id=installation_id,
    )
    binding = ServiceBinding(installation_id=installation_id)
    handlers = InstallationWorkspaceHandlers(
        installation=service,
        session=session,
        binding=binding,
    )
    registry = build_installation_registry(
        workspace_create=handlers.workspace_create,
        workspace_list=handlers.workspace_list,
    )
    return ApplicationDispatcher(
        registry=registry,
        session=session,
        binding=binding,
        supported_capabilities=server_capability_snapshot(registry),
        transport=transport,
        probe=fallback,
        record=record,
        service=service,
    )


def build_memory_application_dispatcher(
    *,
    service: Any,
    principal_id: str,
    installation_id: str,
    workspace_id: str,
    fallback: ApplicationFallback,
    clock: Clock | None = None,
    allocate_identifier: IdentifierAllocator = random_identifier,
    token_codec: ContinuationTokenCodec | None = None,
    transport: str = LOCAL_TRANSPORT_ADAPTER,
    record: ApplicationCallSink | None = None,
) -> ApplicationDispatcher:
    """Compose the exact three-operation S2 family around the existing router."""
    session = memory_family_session(
        principal_id=principal_id,
        installation_id=installation_id,
        workspace_id=workspace_id,
    )
    binding = ServiceBinding(installation_id=installation_id, workspace_id=workspace_id)
    label_grant = local_owner_label_grant(
        principal_id=principal_id,
        workspace_id=workspace_id,
        granted_workspace=workspace_id,
    )
    handlers = MemoryHandlers(
        service=service,
        session=session,
        binding=binding,
        label_grant=label_grant,
        authorized_views=(
            LOCAL_OWNER_VIEW_GRANT if label_grant.all_labels else frozenset()
        ),
        clock=SystemClock() if clock is None else clock,
        allocate_identifier=allocate_identifier,
        token_codec=(
            HmacContinuationTokenCodec.secure() if token_codec is None else token_codec
        ),
    )
    registry = build_memory_registry(handlers)
    return ApplicationDispatcher(
        registry=registry,
        session=session,
        binding=binding,
        supported_capabilities=server_capability_snapshot(registry),
        transport=transport,
        probe=fallback,
        record=record,
        service=service,
    )


def build_job_application_dispatcher(
    *,
    service: Any,
    principal_id: str,
    installation_id: str,
    workspace_id: str,
    fallback: ApplicationFallback,
    clock: Clock | None = None,
    allocate_identifier: IdentifierAllocator = random_identifier,
    token_codec: ContinuationTokenCodec | None = None,
    transport: str = LOCAL_TRANSPORT_ADAPTER,
    record: ApplicationCallSink | None = None,
) -> ApplicationDispatcher:
    """Compose the exact five-operation S3 family around the existing router."""
    session = job_family_session(
        principal_id=principal_id,
        installation_id=installation_id,
        workspace_id=workspace_id,
    )
    binding = ServiceBinding(installation_id=installation_id, workspace_id=workspace_id)
    handlers = JobHandlers(
        service=service,
        session=session,
        binding=binding,
        clock=SystemClock() if clock is None else clock,
        allocate_identifier=allocate_identifier,
        token_codec=(
            HmacContinuationTokenCodec.secure() if token_codec is None else token_codec
        ),
    )
    registry = build_job_registry(handlers)
    return ApplicationDispatcher(
        registry=registry,
        session=session,
        binding=binding,
        supported_capabilities=server_capability_snapshot(registry),
        transport=transport,
        probe=fallback,
        record=record,
        service=service,
    )


def build_governance_application_dispatcher(
    *,
    service: Any,
    principal_id: str,
    installation_id: str,
    workspace_id: str,
    fallback: ApplicationFallback,
    clock: Clock | None = None,
    allocate_identifier: IdentifierAllocator = random_identifier,
    transport: str = LOCAL_TRANSPORT_ADAPTER,
    record: ApplicationCallSink | None = None,
) -> ApplicationDispatcher:
    """Compose the exact four-operation S4 family around the existing router."""
    session = governance_family_session(
        principal_id=principal_id,
        installation_id=installation_id,
        workspace_id=workspace_id,
    )
    binding = ServiceBinding(installation_id=installation_id, workspace_id=workspace_id)
    label_grant = local_owner_label_grant(
        principal_id=principal_id,
        workspace_id=workspace_id,
        granted_workspace=workspace_id,
    )
    handlers = GovernanceHandlers(
        service=service,
        session=session,
        binding=binding,
        label_grant=label_grant,
        clock=SystemClock() if clock is None else clock,
        allocate_identifier=allocate_identifier,
    )
    registry = build_governance_registry(handlers)
    return ApplicationDispatcher(
        registry=registry,
        session=session,
        binding=binding,
        supported_capabilities=server_capability_snapshot(registry),
        transport=transport,
        probe=fallback,
        record=record,
        service=service,
    )


def build_chat_application_dispatcher(
    *,
    service: Any,
    principal_id: str,
    installation_id: str,
    workspace_id: str,
    fallback: ApplicationFallback,
    clock: Clock | None = None,
    allocate_identifier: IdentifierAllocator = random_identifier,
    resolve_command: ChatCommandResolver | None = resolve_chat_command,
    transport: str = LOCAL_TRANSPORT_ADAPTER,
    record: ApplicationCallSink | None = None,
) -> ApplicationDispatcher:
    """Compose the exact two-operation W2-F2 Chat family around the existing router.

    `resolve_command` is the one Chat-domain seam and production now states the Core
    resolver `service/chat_submit.py` ships: a `SubmitMessage` in the variant this build
    can perform is executed into durable Chat storage, and every other Chat command --
    and every `SubmitMessage` variant naming work no authority here does -- still
    refuses at the domain step, after the grant and before any write, rather than being
    absent from the catalogue. Passing `None` restores the resolverless build, which is
    what a test proving that refusal path uses.
    """
    session = chat_family_session(
        principal_id=principal_id,
        installation_id=installation_id,
        workspace_id=workspace_id,
    )
    binding = ServiceBinding(installation_id=installation_id, workspace_id=workspace_id)
    handlers = ChatHandlers(
        service=service,
        session=session,
        binding=binding,
        clock=SystemClock() if clock is None else clock,
        allocate_identifier=allocate_identifier,
        resolve_command=resolve_command,
    )
    registry = build_chat_registry(handlers)
    return ApplicationDispatcher(
        registry=registry,
        session=session,
        binding=binding,
        supported_capabilities=server_capability_snapshot(registry),
        transport=transport,
        probe=fallback,
        record=record,
        service=service,
    )


__all__ = [
    "CHANNEL_TRUST",
    "CONTEXT_PACK_BUILD_OPERATION",
    "EVIDENCE_SEARCH_OPERATION",
    "GOVERNANCE_FAMILY_PURPOSES",
    "GRAPH_TRAVERSE_OPERATION",
    "INSTALLATION_OPERATION_PURPOSES",
    "JOB_FAMILY_PURPOSES",
    "JOB_OBSERVATION_PURPOSE",
    "KNOWLEDGE_RETRIEVAL_PURPOSE",
    "KNOWLEDGE_SEARCH_OPERATION",
    "LOCAL_TRANSPORT_ADAPTER",
    "MEMORY_FAMILY_PURPOSES",
    "MEMORY_SEARCH_OPERATION",
    "OPERATION_PURPOSES",
    "PRINCIPAL_SOURCE",
    "WORKSPACE_INSPECTION_PURPOSE",
    "WORKSPACE_INSPECT_OPERATION",
    "ApplicationCallRecord",
    "ApplicationCallSink",
    "ApplicationDispatcher",
    "ProductionApplicationSurface",
    "build_application_registry",
    "build_governance_application_dispatcher",
    "build_governance_registry",
    "build_installation_application_dispatcher",
    "build_installation_registry",
    "build_job_application_dispatcher",
    "build_job_registry",
    "build_memory_application_dispatcher",
    "build_memory_registry",
    "compose_production_application_surface",
    "governance_family_session",
    "installation_owner_session",
    "job_family_session",
    "local_owner_session",
    "memory_family_session",
]
