"""Server-owned admission decisions for an already-authorized application call.

Authorization answers whether a caller may ask. Admission answers whether this
service instance can execute the authorized request *now*: the workspace lifecycle,
dependency, capacity, deadline, and cancellation facts live on the server and must
never be inferred from request payloads. The default is deliberately the ordinary
ready state; launchers with richer lifecycle state supply a different immutable
snapshot when they compose the application dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_RATE_LIMITED,
    ERROR_CODE_UPGRADE_REQUIRED,
    ERROR_CODE_WORKSPACE_BUSY,
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
)
from omnivia_core_runtime.service.authorization import AuthorizedApplicationContext
from omnivia_core_runtime.service.operations import OperationError, application_refusal

WorkspaceAdmissionState = Literal[
    "ready",
    "busy",
    "lease_unavailable",
    "migration_required",
    "incompatible",
    "upgrade_required",
]
CapacityAdmissionState = Literal["available", "rate_limited"]
DependencyAdmissionState = Literal["available", "unavailable", "recoverable_fault"]

_MESSAGES: Final = {
    ERROR_CODE_CANCELLED: "the caller cancelled this request before execution",
    ERROR_CODE_DEADLINE_EXCEEDED: "the request has no execution time remaining",
    ERROR_CODE_DEPENDENCY_UNAVAILABLE: "a required service dependency is unavailable",
    ERROR_CODE_INCOMPATIBLE_VERSION: "the workspace is incompatible with this service",
    ERROR_CODE_INTERNAL_RECOVERABLE: "the service cannot execute this request yet",
    ERROR_CODE_RATE_LIMITED: "the service has no request capacity available",
    ERROR_CODE_UPGRADE_REQUIRED: "the workspace requires an upgrade before this request",
    ERROR_CODE_WORKSPACE_BUSY: "the workspace is busy with an exclusive operation",
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE: "the authoritative workspace lease is unavailable",
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED: "the workspace requires migration before this request",
}

_WORKSPACE_CODES: Final = {
    "busy": ERROR_CODE_WORKSPACE_BUSY,
    "lease_unavailable": ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    "migration_required": ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    "incompatible": ERROR_CODE_INCOMPATIBLE_VERSION,
    "upgrade_required": ERROR_CODE_UPGRADE_REQUIRED,
}


class ApplicationAdmissionPolicy(Protocol):
    """A server-owned, side-effect-free decision for one authorized request."""

    def evaluate(self, context: AuthorizedApplicationContext) -> OperationError | None:
        """Return a typed refusal, or ``None`` when the handler may execute."""


@dataclass(frozen=True)
class ApplicationAdmission:
    """One immutable snapshot of the service facts that gate execution."""

    workspace: WorkspaceAdmissionState = "ready"
    capacity: CapacityAdmissionState = "available"
    dependency: DependencyAdmissionState = "available"
    cancelled_request_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.workspace not in {"ready", *_WORKSPACE_CODES}:
            raise ValueError("unknown workspace admission state")
        if self.capacity not in {"available", "rate_limited"}:
            raise ValueError("unknown capacity admission state")
        if self.dependency not in {
            "available",
            "unavailable",
            "recoverable_fault",
        }:
            raise ValueError("unknown dependency admission state")
        object.__setattr__(
            self, "cancelled_request_ids", frozenset(self.cancelled_request_ids)
        )

    def evaluate(self, context: AuthorizedApplicationContext) -> OperationError | None:
        if context.request_id in self.cancelled_request_ids:
            return _refusal(ERROR_CODE_CANCELLED)
        if context.deadline_ms == 0:
            return _refusal(ERROR_CODE_DEADLINE_EXCEEDED)
        workspace_code = _WORKSPACE_CODES.get(self.workspace)
        if workspace_code is not None:
            return _refusal(workspace_code)
        if self.capacity == "rate_limited":
            return _refusal(ERROR_CODE_RATE_LIMITED)
        if self.dependency == "unavailable":
            return _refusal(ERROR_CODE_DEPENDENCY_UNAVAILABLE)
        if self.dependency == "recoverable_fault":
            return _refusal(ERROR_CODE_INTERNAL_RECOVERABLE)
        return None


def _refusal(code: str) -> OperationError:
    return application_refusal(code, _MESSAGES[code])


ALLOW_APPLICATION_REQUEST: Final = ApplicationAdmission()


__all__ = [
    "ALLOW_APPLICATION_REQUEST",
    "ApplicationAdmission",
    "ApplicationAdmissionPolicy",
]
