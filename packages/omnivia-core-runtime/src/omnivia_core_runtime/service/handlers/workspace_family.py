"""Installation-scoped workspace handlers for V06-5 S1.

These handlers are small adapters around the S0 installation authority service.
They decode operation payloads and then hand the *exact* authorized context to the
server-owned installation service.  Session and binding facts are closed over when
the handler object is built; no request can replace them, and no handler can issue a
mutation grant directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    WorkspaceCreateInput,
    WorkspaceListInput,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
)
from omnivia_core_runtime.service.installation import InstallationApplicationService
from omnivia_core_runtime.service.operations import (
    InstallationOperationContext,
    OperationError,
)

_MESSAGE_INVALID_CREATE: Final = (
    "the workspace create input is not valid for this operation"
)
_MESSAGE_INVALID_LIST: Final = "the workspace list input is not valid for this operation"
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


__all__ = ["InstallationWorkspaceHandlers"]
