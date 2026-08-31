"""CP-04A: the WorkflowHandlers delegation seam.

These tests exercise only the seam. They do not bind a scheduler, a repository or
a review store, and they deliberately never let a double stand in for one: a
runtime that answers with something other than a handler result is refused rather
than believed. What is proved here is that the seam delegates the *exact*
authorized context when a live runtime is installed, and fails closed with
``dependency_unavailable`` in every other case.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_runtime.service.handlers.workflow import (
    WORKFLOW_CONTROL_OPERATION,
    WORKFLOW_INSPECT_OPERATION,
    WORKFLOW_REVIEW_OPERATION,
    WORKFLOW_START_OPERATION,
    WorkflowHandlers,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    get_operation_metadata,
)

PRINCIPAL = "principal-cp04a"
WORKSPACE_ID = "ws-cp04a"

#: Every operation in the family, paired with the handler/runtime method name the
#: seam must reach for and the purpose the command surface declares for it. Stated
#: once so a rename cannot leave a case silently untested.
FAMILY = (
    (WORKFLOW_START_OPERATION, "workflow_start", "workflow_run"),
    (WORKFLOW_INSPECT_OPERATION, "workflow_inspect", "workflow_observation"),
    (WORKFLOW_CONTROL_OPERATION, "workflow_control", "workflow_control"),
    (WORKFLOW_REVIEW_OPERATION, "workflow_review", "workflow_review"),
)
PURPOSES = {operation: purpose for operation, _, purpose in FAMILY}
CASES = tuple((operation, method) for operation, method, _ in FAMILY)


def context_for(operation: str) -> OperationContext:
    """One authorized-shape context, built from the frozen catalogue entry."""
    entry = get_operation_metadata(operation)
    required = entry.required_capability
    request = RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id=f"req-{operation}",
            correlation_id=f"cor-{operation}",
            trace_id=f"trc-{operation}",
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id="cp04a-client", version="0.1.0"),
            workspace_id=WORKSPACE_ID,
            scopes=tuple(entry.scope.required_scopes),
            purpose=PURPOSES[operation],
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=True,
                ),
            ),
        ),
        input={},
    )
    return OperationContext(
        request=request,
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({operation}),
        service=None,
    )


def handlers_with(runtime: Any) -> WorkflowHandlers:
    return WorkflowHandlers(service=SimpleNamespace(workflow_runtime=runtime))


def assert_dependency_unavailable(raised: pytest.ExceptionInfo[OperationError]) -> None:
    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    # Read off the contract's frozen table rather than restated here: the seam
    # names the code, the contract owns the retry posture.
    assert (
        raised.value.retry_class
        == DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_DEPENDENCY_UNAVAILABLE]
    )


@pytest.mark.parametrize(("operation", "method"), CASES)
def test_no_workflow_runtime_refuses(operation: str, method: str) -> None:
    """A build that never installed the dependency cannot succeed by omission."""
    handlers = WorkflowHandlers(service=SimpleNamespace())
    with pytest.raises(OperationError) as raised:
        getattr(handlers, method)(context_for(operation))
    assert_dependency_unavailable(raised)


@pytest.mark.parametrize(("operation", "method"), CASES)
@pytest.mark.parametrize("shape", ["missing", "non_callable"])
def test_incomplete_runtime_refuses(operation: str, method: str, shape: str) -> None:
    """A partial runtime is refused per-operation, not accepted wholesale.

    The runtime here answers the other three operations, so the refusal is about
    the one method the seam actually needs rather than about an empty object.
    """
    other: dict[str, Any] = {
        name: (lambda _context: {}) for _, name in CASES if name != method
    }
    runtime = SimpleNamespace(
        **other, **({method: "not a method"} if shape == "non_callable" else {})
    )
    with pytest.raises(OperationError) as raised:
        getattr(handlers_with(runtime), method)(context_for(operation))
    assert_dependency_unavailable(raised)


@pytest.mark.parametrize(("operation", "method"), CASES)
def test_delegates_the_exact_context(operation: str, method: str) -> None:
    """Identity, not equality: nothing between handler and runtime rebuilds the
    authorized context."""
    seen: list[OperationContext] = []
    result: Mapping[str, Any] = {"run": {"id": "run-1"}}

    def record(context: OperationContext) -> Mapping[str, Any]:
        seen.append(context)
        return result

    runtime = SimpleNamespace(**{method: record})
    context = context_for(operation)

    returned = getattr(handlers_with(runtime), method)(context)

    assert seen == [context]
    assert seen[0] is context
    assert returned is result


@pytest.mark.parametrize(("operation", "method"), CASES)
def test_audited_result_passes_through(operation: str, method: str) -> None:
    """Server-owned response metadata survives the seam unchanged."""
    audited = AuditedOperationResult(
        result={"run": {"id": "run-2"}},
        audit_reference="audit-cp04a",
        canonical_resolution_time="2026-08-31T00:00:00Z",
    )
    runtime = SimpleNamespace(**{method: lambda _context: audited})

    returned = getattr(handlers_with(runtime), method)(context_for(operation))

    assert returned is audited
    assert returned.audit_reference == "audit-cp04a"
    assert returned.canonical_resolution_time == "2026-08-31T00:00:00Z"
    assert returned.job_reference is None


@pytest.mark.parametrize(("operation", "method"), CASES)
@pytest.mark.parametrize("answer", [None, "run-3", 7, ["run-3"], object()])
def test_unusable_return_fails_closed(
    operation: str, method: str, answer: object
) -> None:
    """A runtime that answers with something that is not a handler result is a
    dependency this build cannot use, not a success."""
    runtime = SimpleNamespace(**{method: lambda _context: answer})
    with pytest.raises(OperationError) as raised:
        getattr(handlers_with(runtime), method)(context_for(operation))
    assert_dependency_unavailable(raised)
