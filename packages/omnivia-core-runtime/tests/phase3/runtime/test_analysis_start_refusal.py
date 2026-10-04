"""`analysis.start` milestone-1 runtime refusal evidence (SPEC-CORE-DATA-001, D-0028).

The contract tests classify the request; this module evidences the *boundary*:
the registered handler renders the typed outcome and does nothing else. The
side-effect proof is structural — the handler's entire body is one pure
classifier call plus one raised `OperationError` — and the tests below pin the
observable behaviour that proves it: the raised code, the frozen retry class,
an untouched request document, and a registry entry that is the pure function
itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, Final

import pytest
from omnivia_core_runtime.service.handlers.analysis import (
    ANALYSIS_START_OPERATION,
    analysis_start,
)
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics_operations import get_operation_metadata

_PRINCIPAL: Final = "principal-1"
_WORKSPACE: Final = "workspace-1"


def _metadata() -> Any:
    return get_operation_metadata("analysis.start")


def _envelope(request_input: dict[str, Any]) -> RequestEnvelope:
    metadata = _metadata()
    return RequestEnvelope(
        operation="analysis.start",
        metadata=RequestMetadata(
            request_id="req-analysis-1",
            correlation_id="cor-analysis-1",
            trace_id="trc-analysis-1",
            api_version="1.0",
            client=ClientIdentity(id="omnivia.cli", version="1.0.0"),
            workspace_id=_WORKSPACE,
            scopes=tuple(metadata.scope.required_scopes),
            purpose="insights_analysis_request",
            required_capabilities=(
                CapabilityRequirement(
                    id=metadata.required_capability.id,
                    minimum_version=metadata.required_capability.minimum_version,
                    required=metadata.required_capability.required,
                ),
            ),
        ),
        input=request_input,
    )


def _context(request_input: dict[str, Any]) -> tuple[OperationContext, dict[str, Any]]:
    envelope = _envelope(request_input)
    context = OperationContext(
        request=envelope,
        principal=_PRINCIPAL,
        workspace_id=_WORKSPACE,
        granted_operations=frozenset({ANALYSIS_START_OPERATION}),
        service=None,
    )
    return context, request_input


def _valid_input() -> dict[str, Any]:
    return {
        "request_version": "1.0",
        "target": {"kind": "metric", "metric_revision_id": "metric-overdue-r1"},
        "as_of_date": "2026-09-30",
        "business_timezone": "Australia/Brisbane",
        "use_class": "current_publication",
        "purpose_reference": "finance-exposure-review",
    }


@pytest.mark.parametrize(
    ("request_input", "code", "retry_class"),
    [
        (_valid_input(), ERROR_CODE_DEPENDENCY_UNAVAILABLE, "retryable_after_delay"),
        (
            dict(_valid_input(), use_class="historical_display"),
            ERROR_CODE_DEPENDENCY_UNAVAILABLE,
            "retryable_after_delay",
        ),
        (
            dict(_valid_input(), request_version="1.5"),
            ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
            "non_retryable",
        ),
        (
            dict(_valid_input(), request_version="3.0"),
            ERROR_CODE_INCOMPATIBLE_VERSION,
            "non_retryable",
        ),
        (
            dict(_valid_input(), use_class="action_input"),
            ERROR_CODE_INVALID_REQUEST,
            "non_retryable",
        ),
        (
            dict(_valid_input(), extra_field=1),
            ERROR_CODE_INVALID_REQUEST,
            "non_retryable",
        ),
        (
            dict(_valid_input(), parameters=None),
            ERROR_CODE_INVALID_REQUEST,
            "non_retryable",
        ),
        (
            dict(_valid_input(), output_bounds={"max_rows": None}),
            ERROR_CODE_INVALID_REQUEST,
            "non_retryable",
        ),
        (
            dict(_valid_input(), parameters=[{"name": "p", "value": None}]),
            ERROR_CODE_INVALID_REQUEST,
            "non_retryable",
        ),
    ],
    ids=[
        "valid-shape-refuses-dependency",
        "historical-display-same-refusal",
        "unsupported-minor-is-its-own-code",
        "unknown-major-is-incompatible",
        "action-input-is-invalid-request",
        "unknown-field-is-invalid-request",
        "null-parameters-is-invalid-request",
        "null-max-rows-is-invalid-request",
        "null-parameter-value-is-invalid-request",
    ],
)
def test_the_handler_renders_exactly_the_classified_outcome(
    request_input: dict[str, Any], code: str, retry_class: str
) -> None:
    context, original = _context(request_input)
    with pytest.raises(OperationError) as raised:
        analysis_start(context)
    assert raised.value.code == code
    assert raised.value.retry_class == retry_class
    assert raised.value.job_reference is None
    # The refusal carries no queue or executor state, and the request document
    # leaves the boundary untouched.
    assert raised.value.audit_reference is None
    assert json.dumps(original, sort_keys=True) == json.dumps(
        context.request.input, sort_keys=True
    )


def test_every_admitted_use_class_receives_the_same_refusal() -> None:
    codes = set()
    for use_class in ("exploration", "historical_display", "current_publication"):
        context, _ = _context(dict(_valid_input(), use_class=use_class))
        with pytest.raises(OperationError) as raised:
            analysis_start(context)
        codes.add((raised.value.code, raised.value.retry_class))
    assert codes == {(ERROR_CODE_DEPENDENCY_UNAVAILABLE, "retryable_after_delay")}


def test_accepting_the_literal_current_publication_grants_nothing() -> None:
    """A decoded `current_publication` request is refused like any other: the
    refusal names no permission, carries no grant object, and the handler
    returns no result that could be read as one."""
    context, _ = _context(dict(_valid_input(), use_class="current_publication"))
    with pytest.raises(OperationError) as raised:
        analysis_start(context)
    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert raised.value.__dict__.get("authorization") is None
    assert not hasattr(raised.value, "result")


class _CustomMapping(Mapping[str, Any]):
    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@pytest.mark.parametrize("mapping", [MappingProxyType, _CustomMapping])
def test_an_abstract_mapping_request_reaches_the_same_refusal(mapping: Any) -> None:
    """Wire transports hand the handler a read-only mapping, not a dict."""
    plain = dict(
        _valid_input(),
        target=mapping({"kind": "metric", "metric_revision_id": "metric-overdue-r1"}),
        parameters=(
            mapping({"name": "currency", "value": mapping({"codes": ("AUD", "NZD")})}),
        ),
    )
    request_input = mapping(plain)
    assert not isinstance(request_input, dict)
    before = repr(sorted(request_input.items()))
    context, _ = _context(request_input)  # type: ignore[arg-type]
    with pytest.raises(OperationError) as raised:
        analysis_start(context)
    assert raised.value.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert raised.value.retry_class == "retryable_after_delay"
    assert raised.value.job_reference is None
    assert raised.value.audit_reference is None
    assert raised.value.__dict__.get("authorization") is None
    assert not hasattr(raised.value, "result")
    assert context.request.input is request_input
    assert repr(sorted(request_input.items())) == before


def test_the_handler_name_matches_the_catalogue_operation() -> None:
    assert ANALYSIS_START_OPERATION == "analysis.start"
    assert get_operation_metadata(ANALYSIS_START_OPERATION).name == "analysis.start"
