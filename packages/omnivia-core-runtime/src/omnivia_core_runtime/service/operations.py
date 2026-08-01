"""Transport-neutral operation dispatch (B9, ADR-038).

Scope, stated plainly: **this registry contains no product operations.** A2 owns the
provider-neutral operation catalogue — workspace inspect/create/list, memory
create/read/list/search, ingestion, evidence search, graph traversal, Context Pack
creation — and registering them is the separately approved Phase 4 packet, not this
one. The five schemas this runtime builds on are envelope-level
only (`common`, `compatibility`, `errors`, `envelopes`, `application`); there is no
operation or payload schema to implement against.

Defining those payloads here would create exactly the competing public domain API
the programme rules forbid, so the registry ships with only the three
service-lifecycle operations ADR-037 keeps distinct from product operations —
health, readiness and discovery — and **refuses** any operation it does not know.
When A2 freezes the catalogue, handlers register against those frozen contracts
without the runtime having invented them.

The dispatcher itself is transport-neutral: it consumes a `RequestEnvelope` and
returns a `ResponseEnvelope`, so an in-process caller, a local IPC transport, the
CLI and the MCP adapter all share one path and cannot diverge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omnivia_core.contracts.v1 import (
    ApiError,
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    GrantedAuthority,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
)

#: The only operations this runtime implements today. Deliberately not product
#: operations: ADR-037 keeps health, readiness and discovery distinct from them.
SERVICE_OPERATIONS = ("core.health", "core.readiness", "core.discovery")

OperationHandler = Callable[["OperationContext"], Mapping[str, Any]]


@dataclass(frozen=True)
class OperationContext:
    """Everything a handler may see.

    A handler receives the decoded envelope and the authorised principal, never a
    raw connection: storage authority stays with the service, so no handler can
    become a second writer.
    """

    request: RequestEnvelope
    principal: str
    workspace_id: str
    granted_operations: frozenset[str]
    service: Any = None


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


SERVER_VERSION = "0.1.0"

#: The workspace format as a *contract* version. The manifest records the workspace
#: format as "1" (a single ordinal), but the envelope's ContractVersion requires
#: `major.minor`, so the two notations are mapped rather than shared. Using "1" here
#: produced a valid in-process response that failed to encode - a divergence only the
#: serialising transport could catch.
WORKSPACE_FORMAT_CONTRACT_VERSION = "1.0"


def response_metadata(
    request: RequestEnvelope,
    *,
    principal: str,
    granted: tuple[str, ...] = (),
) -> ResponseMetadata:
    """Build the contract's response metadata.

    `ResponseMetadata` requires a fully populated version/capability envelope and a
    granted-authority record. Populating them properly is the point of consuming the
    public contract rather than inventing a thinner one: a caller can negotiate
    versions and see its effective authority from any response.
    """
    api_version = request.metadata.api_version
    window = VersionWindow(minimum=api_version, maximum=api_version)
    # A granted operation is reported as a versioned CapabilityRef, not a bare
    # string: the contract's capability model carries a version so a client can tell
    # which revision of an operation it was granted.
    refs = tuple(
        CapabilityRef(id=operation, version=api_version) for operation in granted
    )
    capabilities = CapabilitySet(supported=refs, granted=refs, effective=refs)
    return ResponseMetadata(
        request_id=request.metadata.request_id,
        correlation_id=request.metadata.correlation_id,
        version=VersionCapabilityEnvelope(
            api_version=api_version,
            server_version=SERVER_VERSION,
            workspace_format_version=WORKSPACE_FORMAT_CONTRACT_VERSION,
            compatibility=CompatibilityMetadata(
                selected_api_version=api_version,
                selected_workspace_version=WORKSPACE_FORMAT_CONTRACT_VERSION,
                supported_api_versions=window,
                supported_workspace_versions=VersionWindow(
                    minimum=WORKSPACE_FORMAT_CONTRACT_VERSION,
                    maximum=WORKSPACE_FORMAT_CONTRACT_VERSION,
                ),
                status="compatible",
                upgrade_state=UpgradeState(value="current"),
                deprecations=(),
            ),
            capabilities=capabilities,
        ),
        authority=GrantedAuthority(principal_id=principal, roles=(), capabilities=refs),
    )


def success(
    request: RequestEnvelope,
    result: Mapping[str, Any],
    *,
    principal: str = "unknown",
    granted: tuple[str, ...] = (),
) -> ResponseEnvelope:
    return SuccessResponseEnvelope(
        metadata=response_metadata(request, principal=principal, granted=granted),
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
    "SERVICE_OPERATIONS",
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
    "success",
]
