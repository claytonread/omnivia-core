"""Production handlers for the Workflow application surface.

The four operations are real Application Contract operations, not probes or
Platform previews. A build with no live workflow runtime dependency still
refuses with ``dependency_unavailable`` so a caller cannot mistake a mock, proof
record or Simulation for a production Runtime ``Run``. A build that explicitly
provides ``service.workflow_runtime`` delegates through that object; this module
does not synthesize Run truth itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from omnivia_core.contracts.v1 import ERROR_CODE_DEPENDENCY_UNAVAILABLE
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    application_refusal,
)

WORKFLOW_START_OPERATION: Final = "workflow.start"
WORKFLOW_INSPECT_OPERATION: Final = "workflow.inspect"
WORKFLOW_CONTROL_OPERATION: Final = "workflow.control"
WORKFLOW_REVIEW_OPERATION: Final = "workflow.review"
WORKFLOW_FAMILY_OPERATIONS: Final = frozenset(
    {
        WORKFLOW_START_OPERATION,
        WORKFLOW_INSPECT_OPERATION,
        WORKFLOW_CONTROL_OPERATION,
        WORKFLOW_REVIEW_OPERATION,
    }
)

_MESSAGE_NO_LIVE_RUNTIME: Final = (
    "this build has not bound the live workflow runtime scheduler"
)


class WorkflowRuntimeDependency(Protocol):
    """The live workflow dependency the production service may install.

    The names intentionally match the Application Contract operation handlers.
    That keeps this seam a pure delegation point: authorization, operation
    routing and envelope handling stay in the application dispatcher, while the
    configured runtime owns repository/scheduler/review truth.
    """

    def workflow_start(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult: ...

    def workflow_inspect(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult: ...

    def workflow_control(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult: ...

    def workflow_review(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult: ...


@dataclass(frozen=True)
class WorkflowHandlers:
    """Workflow application handlers for the current build.

    ``service.workflow_runtime`` is the only live scheduler/repository binding
    point this family recognizes. If it is missing, incomplete or non-callable,
    the family refuses before mutation with ``dependency_unavailable``. This
    means a caller can configure real Workflow Runtime authority later, but
    cannot get success from an incidental service double today.
    """

    service: Any

    def workflow_start(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._dispatch("workflow_start", context)

    def workflow_inspect(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._dispatch("workflow_inspect", context)

    def workflow_control(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._dispatch("workflow_control", context)

    def workflow_review(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        return self._dispatch("workflow_review", context)

    def _dispatch(
        self, method_name: str, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        runtime = getattr(self.service, "workflow_runtime", None)
        method = getattr(runtime, method_name, None)
        if not callable(method):
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_LIVE_RUNTIME
            )
        result = method(context)
        if isinstance(result, AuditedOperationResult):
            return result
        if isinstance(result, Mapping):
            return result
        raise application_refusal(
            ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_LIVE_RUNTIME
        )
