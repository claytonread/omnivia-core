"""The transport-neutral dispatcher (B9).

One path from `RequestEnvelope` to `ResponseEnvelope`, shared by every transport, so
an in-process caller, a local IPC transport, the CLI and the MCP adapter cannot
diverge in semantics. ADR-038's requirement that transport adapters cannot define a
competing domain API is enforced structurally: an adapter can only reach operations
through this dispatcher, and the dispatcher only knows what the registry holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    RequestEnvelope,
    ResponseEnvelope,
)
from omnivia_core_runtime.service.authorization import (
    AuthorizationDenied,
    Grant,
    authorize,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    OperationRegistry,
    build_service_registry,
    failure,
    success,
)

#: The probe boundary's legacy `core.*` denial codes, stated in the accepted v1
#: wire vocabulary. `authorize` keeps its own codes -- in-process callers already
#: map them -- but a dotted code is not a well-formed v1 `ErrorCode`, so it is not
#: something this dispatcher may put in a response envelope.
#:
#: Closed by construction, and read with an explicit miss branch rather than a
#: default: a legacy code this map does not name fails closed to
#: `internal_non_recoverable` instead of crossing the wire unchanged.
_WIRE_AUTHORIZATION_CODES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "core.workspace_not_granted": ERROR_CODE_WORKSPACE_NOT_GRANTED,
        "core.principal_mismatch": ERROR_CODE_AUTHORIZATION_DENIED,
        "core.operation_not_granted": ERROR_CODE_AUTHORIZATION_DENIED,
    }
)
_WIRE_AUTHORIZATION_MESSAGES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "core.workspace_not_granted": "the authenticated principal is not granted this workspace",
        "core.principal_mismatch": "the authenticated principal is not authorized for this request",
        "core.operation_not_granted": "the authenticated principal is not authorized for this operation",
    }
)

#: The fallback refusal for an unmapped legacy denial. Fixed and bounded: nothing
#: of the request, the grant or the original refusal reaches it, because what
#: produced an unrecognised code is by definition not something this boundary can
#: describe safely.
_MESSAGE_UNTRANSLATABLE_DENIAL: Final = (
    "the authorization boundary refused this request in a vocabulary this "
    "transport cannot state"
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
            # mislead a caller into requesting a wider grant. A catalogue operation
            # missing from this runtime's registry is a gap in this build rather
            # than anything the caller did, so it is reported in the contract's own
            # internal-error vocabulary; the message carries the non-authorisation
            # semantics the code no longer spells out.
            return failure(
                request,
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                "the requested operation is not implemented by this runtime",
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
                ERROR_CODE_WORKSPACE_NOT_GRANTED,
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
            wire_code = _WIRE_AUTHORIZATION_CODES.get(denied.code)
            wire_message = _WIRE_AUTHORIZATION_MESSAGES.get(denied.code)
            if wire_code is None or wire_message is None:
                return failure(
                    request,
                    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                    _MESSAGE_UNTRANSLATABLE_DENIAL,
                    principal=self.grant.principal,
                    granted=tuple(sorted(self.grant.operations)),
                )
            return failure(
                request,
                wire_code,
                wire_message,
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
        if isinstance(result, AuditedOperationResult):
            result = result.result
        return success(
            request,
            result,
            principal=self.grant.principal,
            granted=tuple(sorted(self.grant.operations)),
        )


__all__ = ["Dispatcher"]
