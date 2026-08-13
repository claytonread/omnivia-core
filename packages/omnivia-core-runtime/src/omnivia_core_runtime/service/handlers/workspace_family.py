"""Installation-scoped workspace handlers for V06-5 S1.

These handlers are small adapters around the S0 installation authority service.
They decode operation payloads and then hand the *exact* authorized context to the
server-owned installation service.  Session and binding facts are closed over when
the handler object is built; no request can replace them, and no handler can issue a
mutation grant directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    WorkspaceCreateInput,
    WorkspaceListInput,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
)
from omnivia_core_runtime.service.installation import (
    InstallationApplicationService,
    InstallationOperationContext,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationError,
)

_MESSAGE_INVALID_CREATE: Final = (
    "the workspace create input is not valid for this operation"
)
_MESSAGE_INVALID_LIST: Final = (
    "the workspace list input is not valid for this operation"
)
_MESSAGE_NO_INSTALLATION_CONTEXT: Final = (
    "this build cannot serve an installation operation without authorized context"
)


@dataclass(frozen=True)
class InstallationWorkspaceHandlers:
    """The two S1 handlers bound to one owned installation catalogue."""

    installation: InstallationApplicationService
    session: AuthenticatedSession
    binding: ServiceBinding

    def workspace_create(
        self, context: InstallationOperationContext
    ) -> Mapping[str, Any]:
        authorization = self._authorization(context)
        input_: WorkspaceCreateInput | None
        try:
            input_ = WorkspaceCreateInput.from_wire(context.request.input)
        except ContractDecodeError:
            input_ = None
        if input_ is None or not 1 <= len(input_.display_name) <= 256:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_CREATE)
        execution = self.installation.create_workspace_from_authorized_request(
            authorization,
            session=self.session,
            binding=self.binding,
            input_=input_,
        )
        return execution.result

    def workspace_list(
        self, context: InstallationOperationContext
    ) -> Mapping[str, Any]:
        authorization = self._authorization(context)
        input_: WorkspaceListInput | None
        try:
            input_ = WorkspaceListInput.from_wire(context.request.input)
        except ContractDecodeError:
            input_ = None
        if input_ is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_LIST)
        return self.installation.list_workspaces(
            authorization,
            session=self.session,
            binding=self.binding,
            input_=input_,
        )

    @staticmethod
    def _authorization(
        context: InstallationOperationContext,
    ) -> AuthorizedApplicationContext:
        authorization = context.authorization
        if (
            not isinstance(authorization, AuthorizedApplicationContext)
            or authorization.workspace_id is not None
            or authorization.installation_id != context.installation_id
            or authorization.principal_id != context.principal
        ):
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                _MESSAGE_NO_INSTALLATION_CONTEXT,
            )
        return authorization


InstallationForwarder = Callable[[RequestEnvelope], ResponseEnvelope]


@dataclass(frozen=True)
class RemoteInstallationWorkspaceHandlers:
    """Production proxy handlers for the single installation authority owner.

    The outer :class:`ApplicationDispatcher` has already authorized the request
    under the transport-resolved session before either method runs. The exact
    request is then sent over the installation-local authority channel, where the
    owning service applies its own installation session and durable fencing. No
    caller-supplied authority is introduced by the hop.
    """

    forward: InstallationForwarder

    def workspace_create(
        self, context: InstallationOperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._forward(context.request)

    def workspace_list(
        self, context: InstallationOperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._forward(context.request)

    def _forward(
        self, request: RequestEnvelope
    ) -> Mapping[str, Any] | AuditedOperationResult:
        response = self.forward(request)
        if isinstance(response, ErrorResponseEnvelope):
            raise OperationError(
                response.error.code,
                response.error.message,
                retry_class=response.error.retry_class,
                audit_reference=response.metadata.audit_reference,
                job_reference=response.metadata.job,
            )
        if not isinstance(response, SuccessResponseEnvelope):  # pragma: no cover
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                _MESSAGE_NO_INSTALLATION_CONTEXT,
            )
        return AuditedOperationResult(
            result=response.result,
            audit_reference=response.metadata.audit_reference,
            canonical_resolution_time=response.metadata.canonical_resolution_time,
            job_reference=response.metadata.job,
        )


__all__ = [
    "InstallationForwarder",
    "InstallationWorkspaceHandlers",
    "RemoteInstallationWorkspaceHandlers",
]
