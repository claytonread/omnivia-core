"""The transport-neutral dispatcher (B9).

One path from `RequestEnvelope` to `ResponseEnvelope`, shared by every transport, so
an in-process caller, a local IPC transport, the CLI and the MCP adapter cannot
diverge in semantics. ADR-038's requirement that transport adapters cannot define a
competing domain API is enforced structurally: an adapter can only reach operations
through this dispatcher, and the dispatcher only knows what the registry holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope
from omnivia_core_runtime.service.authorization import (
    AuthorizationDenied,
    Grant,
    authorize,
)
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
    OperationRegistry,
    build_service_registry,
    failure,
    success,
)


@dataclass
class Dispatcher:
    """Routes one decoded request to its handler, or refuses it."""

    registry: OperationRegistry
    grant: Grant
    service: Any = None

    @classmethod
    def for_service_operations(cls, grant: Grant, service: Any = None) -> Dispatcher:
        return cls(registry=build_service_registry(), grant=grant, service=service)

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
        operation = request.operation
        workspace_id = request.metadata.workspace_id

        handler = self.registry.get(operation)
        if handler is None:
            # Unknown before unauthorised: an operation this runtime does not
            # implement is not a permissions problem, and reporting it as one would
            # mislead a caller into requesting a wider grant.
            return failure(
                request,
                "core.operation_not_implemented",
                f"operation {operation!r} is not implemented by this runtime; the "
                "product operation catalogue is owned by A2 and is not registered in this "
                "runtime yet",
                principal=self.grant.principal,
                granted=tuple(sorted(self.grant.operations)),
            )

        if workspace_id is None:
            # `authorize` already denied this -- `None` is in no grant's workspace set
            # -- but only incidentally, and the type said the field could not be
            # absent. Refusing it here states the rule instead of relying on a
            # membership test to be wrong in the right direction.
            return failure(
                request,
                "core.workspace_not_granted",
                "request does not name a workspace",
                principal=self.grant.principal,
                granted=tuple(sorted(self.grant.operations)),
            )

        try:
            authorize(
                self.grant,
                principal_claim=(
                    None
                    if request.metadata.principal_claim is None
                    else request.metadata.principal_claim.claimed_principal_id
                ),
                workspace_id=workspace_id,
                operation=operation,
            )
        except AuthorizationDenied as denied:
            return failure(
                request,
                denied.code,
                denied.message,
                principal=self.grant.principal,
                granted=tuple(sorted(self.grant.operations)),
            )

        context = OperationContext(
            request=request,
            principal=self.grant.principal,
            workspace_id=workspace_id,
            granted_operations=self.grant.operations,
            service=self.service,
        )
        try:
            result = handler(context)
        except OperationError as error:
            return failure(
                request,
                error.code,
                error.message,
                retry_class=error.retry_class,
                principal=self.grant.principal,
                granted=tuple(sorted(self.grant.operations)),
            )
        return success(
            request,
            result,
            principal=self.grant.principal,
            granted=tuple(sorted(self.grant.operations)),
        )


__all__ = ["Dispatcher"]
