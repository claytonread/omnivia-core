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

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    CapabilityRef,
    OperationMetadata,
    RequestEnvelope,
    ResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core_runtime.service.authorization import (
    ApplicationAuthorizationError,
    AuthenticatedSession,
    AuthorizedApplicationContext,
    Grant,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.workspace import workspace_inspect
from omnivia_core_runtime.service.operations import (
    ApplicationOperationRegistry,
    OperationContext,
    OperationError,
    failure,
    success,
)

#: The one authorised application operation of this vertical, ratified by the owner.
#: Named once here so the session grant, the registry and the evidence all read the
#: same string, and so widening the grant is one visible edit rather than a drift.
WORKSPACE_INSPECT_OPERATION: Final = "workspace.inspect"

#: The fixed purpose allowlist. There is no purpose registry in the contract --
#: purposes are pattern-validated at the boundary and then checked against
#: `session.purposes` -- so the session's set *is* the allowlist, and this is it.
WORKSPACE_INSPECTION_PURPOSE: Final = "workspace_inspection"

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


def local_owner_session(
    *, principal_id: str, installation_id: str, workspace_id: str
) -> AuthenticatedSession:
    """The session this endpoint serves every application request under.

    Built once, at startup, from three facts the service process already holds: the
    principal fixed by installation-local service configuration, the installation
    identity persisted under the installation-local state root, and the single
    workspace this endpoint was launched to own. Nothing here is read from a request,
    and this function is never called from inside a handler.

    Every grant is stated, because `AuthenticatedSession` defaults each one to empty
    and an unstated grant refuses. Every grant is also *narrow on purpose*:

    * `roles` is empty. There is no role model, so any claimed role is refused --
      claims narrow, and there is nothing here to narrow to.
    * `operations` holds exactly one name. Not `APPLICATION_OPERATIONS`, which is all
      twenty and would hand a read-only local owner every mutation in the catalogue.
    * `scopes` and `capabilities` are read off the frozen catalogue entry rather than
      transcribed, so they cannot drift from what the operation actually requires,
      and the read-only capability `workspace.read` is reused rather than a new
      identifier being invented.
    * `purposes` is the fixed allowlist of one.

    The single granted workspace is also stated a second time, independently, on the
    `ServiceBinding` the caller of this function builds. That is deliberate: reaching
    a workspace-specific endpoint and naming a different workspace is refused by the
    binding even if a session grant were ever widened.
    """
    entry = get_operation_metadata(WORKSPACE_INSPECT_OPERATION)
    required = entry.required_capability
    return AuthenticatedSession(
        principal_id=principal_id,
        roles=frozenset(),
        installations=frozenset({installation_id}),
        workspaces=frozenset({workspace_id}),
        operations=frozenset({entry.name}),
        scopes=frozenset(entry.scope.required_scopes),
        purposes=frozenset({WORKSPACE_INSPECTION_PURPOSE}),
        capabilities=(CapabilityRef(id=required.id, version=required.minimum_version),),
    )


def build_application_registry() -> ApplicationOperationRegistry:
    """The application handlers this build ships.

    One entry. `ApplicationOperationRegistry` is bounded by the frozen catalogue and
    fails closed on anything else, so this cannot register a name A2 did not freeze,
    and it registers nothing into the probe registry.
    """
    registry = ApplicationOperationRegistry()
    registry.register(WORKSPACE_INSPECT_OPERATION, workspace_inspect)
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
    probe: Dispatcher
    record: ApplicationCallSink | None
    service: Any = None

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
                session=self.session,
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
                principal=self.session.principal_id,
            )

        handler = self.registry.get(context.operation)
        workspace_id = context.workspace_id
        if handler is None or workspace_id is None:
            # Unreachable through the shipped wiring: the only registered operation is
            # workspace-scoped, so the seam always resolves a workspace for it. Stated
            # rather than asserted because the registry accepts installation-scoped
            # catalogue names too, and the handler contract has no workspace-less
            # context to give one -- a successor lane registering one needs a widened
            # handler contract, not a `None` slipped through here.
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
            result = handler(
                OperationContext(
                    request=request,
                    # The authenticated principal and the authorized workspace, not the
                    # claimed ones. A handler cannot see a claim at all.
                    principal=context.principal_id,
                    workspace_id=workspace_id,
                    granted_operations=self.session.operations,
                    service=self.service,
                )
            )
        except OperationError as error:
            self._emit(request, entry, context=context, error=error)
            return failure(
                request,
                error.code,
                error.message,
                retry_class=error.retry_class,
                principal=context.principal_id,
            )

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
                principal_id=self.session.principal_id,
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


__all__ = [
    "CHANNEL_TRUST",
    "LOCAL_TRANSPORT_ADAPTER",
    "PRINCIPAL_SOURCE",
    "WORKSPACE_INSPECTION_PURPOSE",
    "WORKSPACE_INSPECT_OPERATION",
    "ApplicationCallRecord",
    "ApplicationCallSink",
    "ApplicationDispatcher",
    "build_application_registry",
    "local_owner_session",
]
