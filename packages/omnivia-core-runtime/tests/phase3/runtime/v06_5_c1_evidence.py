"""Runtime recorder for the exact V06-5 C1 semantic execution matrix.

Ordinary test runs remain side-effect free. The dedicated C1 runner enables this
module explicitly, and the qualified scenario tests then record the real handler,
adapter, response branch and timing for each frozen corpus case they execute.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from omnivia_core_runtime.service.operations import ApplicationOperationRegistry

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
)

ENABLED_ENVIRONMENT_VARIABLE: Final = "OMNIVIA_V06_5_C1_LEDGER"
ADAPTER_NAMES: Final = {
    "in-process": "in_process",
    "local-ipc": "ipc",
    "http": "http",
}

_REPO_ROOT = Path(__file__).resolve().parents[5]
_CORPUS = (
    _REPO_ROOT
    / "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
)
_MUTEX = threading.Lock()
_OBSERVATIONS: list[dict[str, Any]] = []


class SemanticRoute(Protocol):
    registry: ApplicationOperationRegistry


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _case_operations() -> dict[str, str]:
    document = json.loads(_CORPUS.read_text(encoding="utf-8"))
    return {case["id"]: case["operation"] for case in document["cases"]}


def _handler_identity(route: SemanticRoute, operation: str) -> str:
    handler = route.registry.get(operation)
    if handler is None:
        raise AssertionError(f"no semantic handler is registered for {operation!r}")
    target = getattr(handler, "__func__", handler)
    module = getattr(target, "__module__", type(target).__module__)
    qualified = getattr(target, "__qualname__", type(target).__qualname__)
    return f"{module}.{qualified}"


def semantic_execution(
    *,
    case_id: str | None,
    adapter: str,
    route: SemanticRoute,
    request: RequestEnvelope,
    invoke: Callable[[], ResponseEnvelope],
) -> ResponseEnvelope:
    """Invoke one semantic scenario and record it when the C1 runner is active."""
    if case_id is None or os.environ.get(ENABLED_ENVIRONMENT_VARIABLE) != "1":
        return invoke()

    expected_operation = _case_operations().get(case_id)
    if expected_operation is None:
        raise AssertionError(f"C1 evidence names unknown corpus case {case_id!r}")
    if expected_operation != request.operation:
        raise AssertionError(
            f"C1 case {case_id!r} belongs to {expected_operation!r}, "
            f"not {request.operation!r}"
        )
    try:
        canonical_adapter = ADAPTER_NAMES[adapter]
    except KeyError as error:
        raise AssertionError(f"unknown C1 adapter {adapter!r}") from error

    started_at = _timestamp()
    started_ns = time.monotonic_ns()
    response = invoke()
    ended_ns = time.monotonic_ns()
    ended_at = _timestamp()
    if isinstance(response, SuccessResponseEnvelope):
        response_branch = "success"
    elif isinstance(response, ErrorResponseEnvelope):
        response_branch = f"error/{response.error.code}"
    else:  # pragma: no cover - the transport contract fixes this union
        response_branch = type(response).__name__

    observation = {
        "adapter": canonical_adapter,
        "case_id": case_id,
        "duration_ns": ended_ns - started_ns,
        "ended_at": ended_at,
        "handler": _handler_identity(route, request.operation),
        "operation": request.operation,
        "request_id": request.metadata.request_id,
        "response_branch": response_branch,
        "started_at": started_at,
    }
    with _MUTEX:
        _OBSERVATIONS.append(observation)
    return response


def observations() -> tuple[dict[str, Any], ...]:
    with _MUTEX:
        return tuple(dict(item) for item in _OBSERVATIONS)


__all__ = [
    "ADAPTER_NAMES",
    "ENABLED_ENVIRONMENT_VARIABLE",
    "observations",
    "semantic_execution",
]
