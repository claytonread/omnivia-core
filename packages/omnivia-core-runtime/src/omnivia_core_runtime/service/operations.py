"""Transport-neutral operation dispatch (B9, ADR-038).

Scope, stated plainly: **this slice registers no application handlers.** The
accepted generated application catalogue now exists, and this module represents it
as the catalogue-backed application registry boundary — the accepted operation
names are derived from that catalogue rather than transcribed here, so the boundary
cannot drift from what was frozen.

Representing the boundary is all this slice does: it changes no dispatch,
authorization, storage or advertised support behaviour. The three service-lifecycle
operations ADR-037 keeps distinct from application operations — health, readiness
and discovery — remain the only implemented handlers, and the registry still
**refuses** any operation it does not know. Later, separately approved slices add
the real application handlers against those frozen contracts, without the runtime
having invented the payloads itself.

The dispatcher itself is transport-neutral: it consumes a `RequestEnvelope` and
returns a `ResponseEnvelope`, so an in-process caller, a local IPC transport, the
CLI and the MCP adapter all share one path and cannot diverge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnivia_core.contracts.v1 import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    OPERATION_CATALOGUE,
    UPGRADE_STATE_NONE,
    UPGRADE_STATE_REQUIRED,
    ApiError,
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    GrantedAuthority,
    Purpose,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    Scope,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    classify_version_compatibility,
    compare_contract_versions,
    get_operation_metadata,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    SERVER_VERSION,
    build_version_window,
    supported_api_versions,
    workspace_contract_version,
)

#: The only operations this runtime implements today. Deliberately not product
#: operations: ADR-037 keeps health, readiness and discovery distinct from them.
SERVICE_OPERATIONS = ("core.health", "core.readiness", "core.discovery")

#: The accepted application operation names, derived from the frozen A2 catalogue
#: rather than transcribed, so this set cannot drift from what A2 froze.
APPLICATION_OPERATIONS: frozenset[str] = frozenset(
    entry.name for entry in OPERATION_CATALOGUE
)

OperationHandler = Callable[["OperationContext"], Mapping[str, Any]]


@dataclass(frozen=True)
class OperationContext:
    """Everything a handler may see.

    A handler receives the decoded envelope and the authorised principal, never a
    raw connection: storage authority stays with the service, so no handler can
    become a second writer.

    **The three effective-authority fields are Amendment 009's pass-through, approved
    2026-08-10, and they are pass-through in the strict sense.** They are the exact
    values `authorize_application_request` already produced -- the effective authority
    at the weaker of the session's grant and this server's snapshot, the narrowed scope
    set, and the purpose the seam checked against the session's allowlist -- carried to
    the handler unchanged. Nothing here widens, re-authorizes, re-derives or repairs any
    of them, and no handler may reconstruct one from the session, the catalogue, the
    request or a constant: a value reconstructed downstream is an authority statement
    nobody granted, and `context_pack.build` writes all three into a signed artifact.

    They are optional and default to `None` because every direct-handler construction
    that predates the amendment states five fields and no more, and because `service`
    keeps its fifth positional slot. `None` is *absence*, not a permissive default: the
    one handler that needs them refuses outright rather than proceeding without them,
    which is why an absent value can never become a quietly weaker authority claim. An
    empty-but-valid value -- no roles, no capabilities -- is a real answer and is not
    absence; only `None` is.
    """

    request: RequestEnvelope
    principal: str
    workspace_id: str
    granted_operations: frozenset[str]
    service: Any = None
    authority: GrantedAuthority | None = None
    scopes: tuple[Scope, ...] | None = None
    purpose: Purpose | None = None


class OperationError(Exception):
    """A handler failed in a way that maps to a contract error code."""

    def __init__(self, code: str, message: str, *, retry_class: str = "non_retryable") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_class = retry_class


@dataclass
class OperationRegistry:
    """Named handlers, with unregistered operations refused rather than ignored."""

    _handlers: dict[str, OperationHandler] = field(default_factory=dict)

    def register(self, operation: str, handler: OperationHandler) -> None:
        if operation in self._handlers:
            raise ValueError(f"operation {operation!r} is already registered")
        self._handlers[operation] = handler

    def get(self, operation: str) -> OperationHandler | None:
        return self._handlers.get(operation)

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def __contains__(self, operation: object) -> bool:
        return operation in self._handlers


@dataclass(frozen=True)
class ApplicationRegistryCompleteness:
    """Whether an application registry has exactly one handler per catalogue name.

    Both lists are sorted so a report is deterministic and diffable regardless of
    registration order.
    """

    missing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.unexpected


@dataclass
class ApplicationOperationRegistry:
    """Handlers for the accepted application operation catalogue, bounded by
    :data:`APPLICATION_OPERATIONS`.

    Registering a name outside that set, or registering the same name twice, fails
    closed. Holding zero handlers, or fewer than all of them, is a valid
    construction state -- later slices own filling handlers in -- but nothing here
    calls a partial registry complete or supported; only `assert_complete` makes
    that claim, and only when it is true.
    """

    _handlers: dict[str, OperationHandler] = field(default_factory=dict)

    def register(self, operation: str, handler: OperationHandler) -> None:
        if operation not in APPLICATION_OPERATIONS:
            raise ValueError(
                f"operation {operation!r} is not part of the accepted application "
                "operation catalogue"
            )
        if operation in self._handlers:
            raise ValueError(f"operation {operation!r} is already registered")
        self._handlers[operation] = handler

    def get(self, operation: str) -> OperationHandler | None:
        return self._handlers.get(operation)

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def __contains__(self, operation: object) -> bool:
        return operation in self._handlers

    def completeness(self) -> ApplicationRegistryCompleteness:
        registered = frozenset(self._handlers)
        return ApplicationRegistryCompleteness(
            missing=tuple(sorted(APPLICATION_OPERATIONS - registered)),
            unexpected=tuple(sorted(registered - APPLICATION_OPERATIONS)),
        )

    def assert_complete(self) -> None:
        report = self.completeness()
        if not report.is_complete:
            raise ValueError(
                "application registry is incomplete: "
                f"missing={list(report.missing)} unexpected={list(report.unexpected)}"
            )


def server_capability_snapshot(
    registry: ApplicationOperationRegistry,
) -> tuple[CapabilityRef, ...]:
    """What this build supports, derived from the handlers it actually registered.

    This is the server-side capability fact `authorize_application_request` has never
    had a source for, and the reason it needs one is specific: `response_metadata`
    below fabricates a `CapabilitySet` per response *from the caller's own claimed
    `api_version`*. That is a response decoration. Passed to the seam as
    `supported_capabilities` it would make the seam's twelfth check compare the
    caller's claim against itself, which is not a check.

    Support is derived from registration rather than from the catalogue at large,
    because a capability this build declares but implements no operation for is not
    support -- it is an advertisement. An empty registry therefore supports nothing and
    every application request is refused, which is the correct fail-closed answer for a
    build that ships no handlers.

    A capability may appear at most once in a capability set regardless of version, and
    the seam refuses a repeated id outright rather than resolving it, so where two
    registered operations require the same capability the *highest* floor wins: a build
    implementing both implements at least the stricter of the two.
    """
    versions: dict[str, str] = {}
    for name in sorted(registry.operations):
        required = get_operation_metadata(name).required_capability
        current = versions.get(required.id)
        if (
            current is None
            or compare_contract_versions(required.minimum_version, current) > 0
        ):
            versions[required.id] = required.minimum_version
    return tuple(
        CapabilityRef(id=capability_id, version=version)
        for capability_id, version in sorted(versions.items())
    )


#: The workspace format as a *contract* version. The manifest records the workspace
#: format as "1" (a single ordinal), but the envelope's ContractVersion requires
#: `major.minor`, so the two notations are mapped rather than shared. Using "1" here
#: produced a valid in-process response that failed to encode - a divergence only the
#: serialising transport could catch.
#:
#: Translated through `versions.workspace_contract_version` rather than written out,
#: so this and the descriptor `runner.py` publishes read the same frozen table. The
#: ordinal is named here because a response is built without a service handle: the
#: dispatcher hands `response_metadata` a request and a grant and nothing else, so
#: the live workspace's own ordinal is not reachable on this path. Ordinal `1` is the
#: only workspace format this build translates at all, so naming it asserts nothing
#: the descriptor could contradict today - but see the module residual: once a second
#: ordinal exists, this must come from the served workspace rather than from here.
WORKSPACE_FORMAT_CONTRACT_VERSION = workspace_contract_version(1)

#: The workspace-format window this build serves, built with the same helper and the
#: same bounds `versions.supported_workspace_versions` gives the descriptor.
_SUPPORTED_WORKSPACE_VERSIONS = build_version_window(
    WORKSPACE_FORMAT_CONTRACT_VERSION, WORKSPACE_FORMAT_CONTRACT_VERSION
)


def response_metadata(
    request: RequestEnvelope,
    *,
    principal: str,
    granted: tuple[str, ...] = (),
    capabilities: tuple[CapabilityRef, ...] | None = None,
) -> ResponseMetadata:
    """Build the contract's response metadata.

    `ResponseMetadata` requires a fully populated version/capability envelope and a
    granted-authority record. Populating them properly is the point of consuming the
    public contract rather than inventing a thinner one: a caller can negotiate
    versions and see its effective authority from any response.

    **Every version fact in here is the server's own.** It used to derive the served
    window, the selected version, each capability version and the compatibility
    status from `request.metadata.api_version` -- the caller's claim about itself --
    so a client asking in "1.0" was told the service supported exactly {1.0, 1.0}
    while the descriptor `runner.py` publishes advertised the real window. Two
    documents about one service, disagreeing, with the client's own assertion
    deciding which. The claim is still read, because a server that never looked at
    it could not tell the caller whether it was usable -- but only ever *through*
    the served window: it is classified against that window, and it is selected only
    when the window contains it. A claim outside what this build serves reaches no
    field of the response.
    """
    served = supported_api_versions()
    # The one question the caller's claim is allowed to decide: is it inside what
    # this build serves? `classify_version_compatibility` is the contract's own
    # answer to that, built on the `version_in_window` that `ownership.discovery`
    # asks the mirror-image question with, and it returns only the statuses
    # `x-omnivia-compatibility-statuses` freezes. Nothing here invents a status.
    #
    # A claim that is not a contract version at all is refused the same way a
    # descriptor that cannot be compared is: unreadable means no. Nothing validates
    # `api_version` beyond a pattern check on the decoding path, and an in-process
    # caller skips even that, so this is reachable -- and letting the error out would
    # turn a bad claim into a crashed dispatch instead of a truthful response.
    #
    # Both exception types, because there are two ways to not be a version and only
    # catching one leaves the crash this guard exists to prevent: a malformed string
    # fails the pattern match (`ValueError`), while a value that is not a string at
    # all -- `None`, an int, bytes, a list -- fails inside `re.fullmatch` with a
    # `TypeError`. `ownership.discovery._within` catches the same pair for the same
    # reason.
    claimed = request.metadata.api_version
    try:
        status = classify_version_compatibility(claimed, served)
    except (TypeError, ValueError):
        status = COMPATIBILITY_STATUS_INCOMPATIBLE

    # Negotiation, in the only form this path has one, and *not* an echo: the
    # revision the server applied is the claimed one when the server actually serves
    # it, and otherwise the best revision it does serve. The claim can only survive
    # this by passing through the served window, so nothing outside what this build
    # implements can ever be selected -- which is the whole difference from the
    # value this used to publish.
    #
    # Both frozen negotiation fixtures state exactly this rule.
    # `compatible-negotiation.json` selects the caller's `1.2` from a `[1.0, 1.3]`
    # window; `incompatible-major.json` answers a caller asking for `2.0` with
    # `selected_api_version: "1.3"` -- the top of the same window -- alongside
    # `status: "incompatible"`. A response says which revision produced it even when
    # it is refusing the caller's.
    #
    # Membership is read off `status` rather than asked again: that keeps this and
    # `ownership.discovery.is_compatible` answering one question through one helper.
    selected = claimed if status == COMPATIBILITY_STATUS_COMPATIBLE else served.maximum
    # A granted operation is reported as a versioned CapabilityRef, not a bare
    # string: the contract's capability model carries a version so a client can tell
    # which revision of an operation it was granted. That version is a server fact --
    # `authorize_application_request` takes it from what the server supports, never
    # from the request -- and the only version this build holds for its own
    # service-lifecycle operations is the contract revision it implements them under.
    #
    # ponytail: API_VERSION stands in for a per-capability version table, and it stands
    # in only where nothing better was passed. `capabilities` is that better thing, and
    # it is what the residual above asked for: an application handler's refs are the
    # *effective* ones `authorize_application_request` computed, at the weaker of the
    # session's grant and this server's snapshot, read off the frozen catalogue entry
    # rather than built from an operation name. An operation name is not a capability
    # id, so the fallback below is right only for the probe operations, which have no
    # catalogue capability at all.
    refs = (
        tuple(CapabilityRef(id=operation, version=API_VERSION) for operation in granted)
        if capabilities is None
        else capabilities
    )
    capability_set = CapabilitySet(supported=refs, granted=refs, effective=refs)
    return ResponseMetadata(
        request_id=request.metadata.request_id,
        correlation_id=request.metadata.correlation_id,
        version=VersionCapabilityEnvelope(
            # The revision in force on this document is the revision negotiation
            # selected; the contract requires the two to agree, so there is one
            # value and it is stated twice, not two values that could drift.
            #
            # This is also where `validate_version_capability_envelope` starts
            # earning its keep. Its rule -- selected must fall inside the published
            # supported window -- was unfalsifiable while both sides were computed
            # from the caller's one claimed value: they agreed by construction no
            # matter what was claimed, so a forged "9.9" passed it. `selected` now
            # comes from negotiating a claim against the window and the window comes
            # from what this build serves, so the check finally compares two things
            # that can disagree, and it fails closed if the negotiation above is
            # ever changed to prefer the claim over the window.
            api_version=selected,
            server_version=SERVER_VERSION,
            workspace_format_version=WORKSPACE_FORMAT_CONTRACT_VERSION,
            compatibility=CompatibilityMetadata(
                selected_api_version=selected,
                selected_workspace_version=WORKSPACE_FORMAT_CONTRACT_VERSION,
                supported_api_versions=served,
                supported_workspace_versions=_SUPPORTED_WORKSPACE_VERSIONS,
                status=status,
                # Moves with `status` or the response contradicts itself: a caller
                # told `upgrade_required` cannot also be told it needs no upgrade.
                # The previous `"current"` was in neither this vocabulary nor the
                # contract's `x-omnivia-upgrade-states`.
                upgrade_state=UpgradeState(
                    value=UPGRADE_STATE_NONE
                    if status == COMPATIBILITY_STATUS_COMPATIBLE
                    else UPGRADE_STATE_REQUIRED
                ),
                deprecations=(),
            ),
            capabilities=capability_set,
        ),
        authority=GrantedAuthority(principal_id=principal, roles=(), capabilities=refs),
    )


def success(
    request: RequestEnvelope,
    result: Mapping[str, Any],
    *,
    principal: str = "unknown",
    granted: tuple[str, ...] = (),
    capabilities: tuple[CapabilityRef, ...] | None = None,
) -> ResponseEnvelope:
    return SuccessResponseEnvelope(
        metadata=response_metadata(
            request, principal=principal, granted=granted, capabilities=capabilities
        ),
        result=dict(result),
    )


def failure(
    request: RequestEnvelope,
    code: str,
    message: str,
    *,
    retry_class: str = "non_retryable",
    principal: str = "unknown",
    granted: tuple[str, ...] = (),
) -> ResponseEnvelope:
    return ErrorResponseEnvelope(
        metadata=response_metadata(request, principal=principal, granted=granted),
        error=ApiError(code=code, message=message, retry_class=retry_class),
    )


# --- the three service operations --------------------------------------------


def health(context: OperationContext) -> Mapping[str, Any]:
    """Liveness. Answerable without owning the workspace, by design."""
    service = context.service
    return {
        "status": "alive",
        "state": getattr(getattr(service, "lifecycle", None), "state", None)
        and service.lifecycle.state.value,
    }


def readiness(context: OperationContext) -> Mapping[str, Any]:
    """Writable readiness, reported as the nine facts rather than one boolean."""
    service = context.service
    lifecycle = getattr(service, "lifecycle", None)
    if lifecycle is None:
        raise OperationError("core.unavailable", "no service is attached")
    return {
        "ready": lifecycle.state.advertises_writable,
        "state": lifecycle.state.value,
        "unmet": lifecycle.readiness.unmet(),
    }


def discovery(context: OperationContext) -> Mapping[str, Any]:
    """What this instance advertises. Never a product query."""
    service = context.service
    return {
        "workspace_id": getattr(service, "workspace_id", None),
        "service_instance_id": (
            None
            if getattr(service, "identity", None) is None
            else service.identity.service_instance_id
        ),
        "fencing_generation": getattr(service, "generation", None),
        "operations": sorted(context.granted_operations),
    }


def build_service_registry() -> OperationRegistry:
    """A registry holding only the service-lifecycle operations."""
    registry = OperationRegistry()
    registry.register("core.health", health)
    registry.register("core.readiness", readiness)
    registry.register("core.discovery", discovery)
    return registry


__all__ = [
    "APPLICATION_OPERATIONS",
    "SERVICE_OPERATIONS",
    "ApplicationOperationRegistry",
    "ApplicationRegistryCompleteness",
    "OperationContext",
    "OperationError",
    "OperationHandler",
    "OperationRegistry",
    "build_service_registry",
    "discovery",
    "failure",
    "health",
    "readiness",
    "response_metadata",
    "server_capability_snapshot",
    "success",
]
