"""`analysis.start` milestone-1 contract evidence (SPEC-CORE-DATA-001, D-0028).

Milestone 1 is a refusal contract, and this module is the contract-level
evidence for it: the four-way version classification, the strict shape
boundary, the catalogue posture, and the honesty rules. The runtime-side
side-effect boundary is evidenced in
`packages/omnivia-core-runtime/tests/phase3/runtime/test_analysis_start_refusal.py`;
this module stays inside the contract package's own boundary (standard library
plus `omnivia_core.contracts.v1` only).

Every expected value here is stated literally, so an edit that changes the
typed outcomes has to change this file in the same commit and the drift is
reviewable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from enum import IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from omnivia_core.contracts.v1 import (
    ADMITTED_ANALYSIS_USE_CLASSES,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    FROZEN_ERROR_CODES,
    AnalysisStartInput,
    ClientIdentity,
    ContractDecodeError,
    OperationMetadata,
    RequestEnvelope,
    RequestMetadata,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.semantics_analysis import (
    SUPPORTED_ANALYSIS_PAYLOAD_VERSION,
    classify_analysis_start_request,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS_PATH = (
    REPO_ROOT
    / "contracts"
    / "application"
    / "v1"
    / "fixtures"
    / "application-wire-adapter-conformance-v1.json"
)
CONTRACTS_DIR = REPO_ROOT / "src" / "omnivia_core" / "contracts" / "v1"

_DEPENDENCY_DETAIL_MARKER = "no analytical executor is admitted"


def _valid_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "request_version": "1.0",
        "target": {"kind": "metric", "metric_revision_id": "metric-overdue-r1"},
        "as_of_date": "2026-09-30",
        "business_timezone": "Australia/Brisbane",
        "use_class": "exploration",
        "purpose_reference": "finance-exposure-review",
    }
    request.update(overrides)
    for absent in [key for key, value in overrides.items() if value is _ABSENT]:
        request.pop(absent)
    return request


class _Absent:
    pass


class _CustomMapping(Mapping[str, Any]):
    """A read-only mapping that is neither a dict nor a MappingProxyType."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _CustomSequence(Sequence[Any]):
    """A read-only sequence that is neither a list nor a tuple."""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = list(items)

    def __getitem__(self, index: Any) -> Any:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)


_ABSENT = _Absent()


# ---------------------------------------------------------------------------
# CO-3: the four-way version classification
# ---------------------------------------------------------------------------


def test_a_supported_versioned_request_refuses_with_dependency_unavailable() -> None:
    code, detail = classify_analysis_start_request(_valid_request())
    assert code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert _DEPENDENCY_DETAIL_MARKER in detail


def test_both_reference_kinds_and_all_three_use_classes_receive_the_same_refusal() -> (
    None
):
    for use_class in sorted(ADMITTED_ANALYSIS_USE_CLASSES):
        metric = classify_analysis_start_request(_valid_request(use_class=use_class))
        assert metric == (
            ERROR_CODE_DEPENDENCY_UNAVAILABLE,
            classify_analysis_start_request(_valid_request(use_class=use_class))[1],
        )
        data_view = classify_analysis_start_request(
            _valid_request(
                use_class=use_class,
                target={
                    "kind": "data_view",
                    "data_view_revision_id": "dataview-cashflow-r2",
                },
            )
        )
        assert data_view[0] == ERROR_CODE_DEPENDENCY_UNAVAILABLE


def test_a_well_formed_unknown_major_is_incompatible_version() -> None:
    for version in ("2.0", "0.9", "10.0"):
        code, _ = classify_analysis_start_request(
            _valid_request(request_version=version)
        )
        assert code == ERROR_CODE_INCOMPATIBLE_VERSION


def test_a_supported_major_with_an_unsupported_minor_is_its_own_code() -> None:
    for version in ("1.1", "1.9"):
        code, _ = classify_analysis_start_request(
            _valid_request(request_version=version)
        )
        assert code == ERROR_CODE_UNSUPPORTED_MINOR_VERSION


def test_malformed_versions_are_invalid_request_not_a_compatibility_code() -> None:
    for version in ("bogus", "1", "1.0.0", "01.0", "1.0 ", "", "one.point"):
        code, _ = classify_analysis_start_request(
            _valid_request(request_version=version)
        )
        assert code == ERROR_CODE_INVALID_REQUEST
    code, _ = classify_analysis_start_request(_valid_request(request_version=None))
    assert code == ERROR_CODE_INVALID_REQUEST
    code, _ = classify_analysis_start_request(
        {
            key: value
            for key, value in _valid_request().items()
            if key != "request_version"
        }
    )
    assert code == ERROR_CODE_INVALID_REQUEST


# ---------------------------------------------------------------------------
# CO-2 / CO-3: the strict shape boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"unrecognised_field": True},  # unknown top-level field
        {"use_class": "action_input"},  # outside the admitted set
        {"use_class": "currentPublication"},  # wrong casing
        {"use_class": 7},  # wrong type
        {"target": {"kind": "metric"}},  # missing revision id
        {
            "target": {
                "kind": "metric",
                "metric_revision_id": "m-1",
                "data_view_revision_id": "d-1",
            }
        },  # both branches at once
        {"target": {"kind": "metric", "metric_revision_id": ""}},
        {"target": {"kind": "workflow", "workflow_revision_id": "w-1"}},
        {
            "as_of_date": "2026-09-30",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
        },
        {"period_start": "2026-09-01"},  # half a period
        {"period_end": "2026-09-30"},  # the other half
        {"as_of_date": "2026-02-30"},  # not a calendar date
        {"as_of_date": "30-09-2026"},
        {"business_timezone": "Not/AZone"},
        {"business_timezone": "+10:00"},  # an offset is not a timezone
        {"parameters": [{"name": "p", "value": 1}, {"name": "p", "value": 2}]},
        {"parameters": [{"name": "p"}]},  # value required
        {"parameters": "currency=AUD"},  # not a list
        {"output_bounds": {"max_rows": 0}},
        {"output_bounds": {"unrecognised_bound": 5}},
        {"purpose_reference": ""},
        {"purpose_reference": "-leading-punctuation"},
    ],
)
def test_shape_violations_are_invalid_request(override: dict[str, Any]) -> None:
    code, _ = classify_analysis_start_request(_valid_request(**override))
    assert code == ERROR_CODE_INVALID_REQUEST


def test_both_and_neither_temporal_scopes_are_invalid_request() -> None:
    code, _ = classify_analysis_start_request(
        _valid_request(
            as_of_date="2026-09-30",
            period_start="2026-09-01",
            period_end="2026-09-30",
        )
    )
    assert code == ERROR_CODE_INVALID_REQUEST
    neither = _valid_request()
    neither.pop("as_of_date")
    code, _ = classify_analysis_start_request(neither)
    assert code == ERROR_CODE_INVALID_REQUEST


def test_a_period_may_not_end_before_it_starts() -> None:
    code, _ = classify_analysis_start_request(
        _valid_request(period_start="2026-09-30", period_end="2026-09-01")
    )
    assert code == ERROR_CODE_INVALID_REQUEST


_NON_STRING_USE_CLASSES = [
    pytest.param(None, id="null"),
    pytest.param(True, id="bool"),
    pytest.param(7, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param([], id="empty-list"),
    pytest.param(["exploration"], id="list-of-admitted-name"),
    pytest.param({}, id="empty-object"),
    pytest.param({"use_class": "exploration"}, id="object-of-admitted-name"),
    pytest.param(MappingProxyType({}), id="mappingproxy"),
    pytest.param(MappingProxyType({"k": 1}), id="mappingproxy-with-keys"),
    pytest.param((), id="empty-tuple"),
    pytest.param(("exploration",), id="tuple-of-admitted-name"),
    pytest.param((["exploration"],), id="tuple-holding-unhashable"),
    pytest.param(_CustomMapping({}), id="custom-mapping"),
    pytest.param(_CustomSequence(["exploration"]), id="custom-sequence"),
]


@pytest.mark.parametrize("use_class", _NON_STRING_USE_CLASSES)
def test_a_non_string_use_class_is_invalid_request_not_a_hash_error(
    use_class: Any,
) -> None:
    # Set membership hashes its operand; an unhashable JSON container must be
    # refused by the type guard, never raise TypeError out of the classifier.
    code, _ = classify_analysis_start_request(_valid_request(use_class=use_class))
    assert code == ERROR_CODE_INVALID_REQUEST


_OTHER_JSON_VALUES = [
    pytest.param(7, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(None, id="null"),
    pytest.param([], id="list"),
    pytest.param({}, id="dict"),
    pytest.param(MappingProxyType({}), id="mappingproxy"),
    pytest.param((), id="tuple"),
    pytest.param(_CustomMapping({}), id="custom-mapping"),
    pytest.param(_CustomSequence([]), id="custom-sequence"),
    pytest.param("not-a-date", id="invalid-date-string"),
    pytest.param("2026-02-30", id="impossible-date-string"),
]


@pytest.mark.parametrize("other", _OTHER_JSON_VALUES)
@pytest.mark.parametrize("valid_side", ["period_start", "period_end"])
def test_mixed_type_period_bounds_are_invalid_request_not_a_type_error(
    other: Any, valid_side: str
) -> None:
    request = _valid_request(period_start="2026-09-01", period_end="2026-09-30")
    request.pop("as_of_date")
    invalid_side = "period_end" if valid_side == "period_start" else "period_start"
    request[invalid_side] = other
    code, _ = classify_analysis_start_request(request)
    assert code == ERROR_CODE_INVALID_REQUEST
    request[valid_side] = other
    code, _ = classify_analysis_start_request(request)
    assert code == ERROR_CODE_INVALID_REQUEST


def test_non_object_documents_are_invalid_request() -> None:
    for document in (None, [], "analysis.start", 7, True):
        code, _ = classify_analysis_start_request(document)
        assert code == ERROR_CODE_INVALID_REQUEST


def test_deeply_nested_json_bombs_are_invalid_request_not_a_hang() -> None:
    bomb: Any = {"depth": None}
    cursor = bomb
    for _ in range(200):
        cursor["depth"] = {"depth": None}
        cursor = cursor["depth"]
    document = _valid_request(parameters=[{"name": "p", "value": bomb}])
    code, _ = classify_analysis_start_request(document)
    assert code == ERROR_CODE_INVALID_REQUEST


def test_the_input_document_is_never_mutated() -> None:
    request = _valid_request(parameters=[{"name": "p", "value": {"k": 1}}])
    before = json.dumps(request, sort_keys=True)
    classify_analysis_start_request(request)
    assert json.dumps(request, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Container parity: abstract Mapping / non-text Sequence are JSON object / array
# ---------------------------------------------------------------------------


def _abstract(value: Any, mapping: Any, sequence: Any) -> Any:
    """Rebuild every dict/list in ``value`` as the given abstract containers."""
    if isinstance(value, dict):
        return mapping(
            {key: _abstract(item, mapping, sequence) for key, item in value.items()}
        )
    if isinstance(value, list):
        return sequence([_abstract(item, mapping, sequence) for item in value])
    return value


_CONTAINERS = [
    pytest.param(MappingProxyType, tuple, id="mappingproxy-tuple"),
    pytest.param(_CustomMapping, _CustomSequence, id="custom-custom"),
]


@pytest.mark.parametrize(("mapping", "sequence"), _CONTAINERS)
def test_abstract_containers_reach_the_same_refusal_at_every_level(
    mapping: Any, sequence: Any
) -> None:
    request = _valid_request(
        parameters=[
            {"name": "p", "value": {"k": [1, {"deep": [None, 1.5, "x", True]}]}},
            {"name": "q", "value": {"items": []}},
        ],
        output_bounds={"max_rows": 10},
    )
    expected = classify_analysis_start_request(request)
    assert expected[0] == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    abstract = _abstract(request, mapping, sequence)
    assert not isinstance(abstract, dict)
    assert not isinstance(abstract["parameters"], list)
    assert classify_analysis_start_request(abstract) == expected


@pytest.mark.parametrize(("mapping", "sequence"), _CONTAINERS)
@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"request_version": "2.0"}, ERROR_CODE_INCOMPATIBLE_VERSION),
        ({"request_version": "1.7"}, ERROR_CODE_UNSUPPORTED_MINOR_VERSION),
        ({"request_version": "bogus"}, ERROR_CODE_INVALID_REQUEST),
        ({"unrecognised_field": True}, ERROR_CODE_INVALID_REQUEST),
        ({"use_class": "action_input"}, ERROR_CODE_INVALID_REQUEST),
        ({"target": {"kind": "metric"}}, ERROR_CODE_INVALID_REQUEST),
        (
            {
                "target": {
                    "kind": "metric",
                    "metric_revision_id": "m-1",
                    "data_view_revision_id": "d-1",
                }
            },
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": 1}, {"name": "p", "value": 2}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": 1, "extra": 2}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        ({"parameters": [{"name": "p"}]}, ERROR_CODE_INVALID_REQUEST),
        ({"output_bounds": {"max_rows": True}}, ERROR_CODE_INVALID_REQUEST),
        ({"output_bounds": {"max_rows": 0}}, ERROR_CODE_INVALID_REQUEST),
        ({"output_bounds": {"unrecognised_bound": 5}}, ERROR_CODE_INVALID_REQUEST),
        (
            {"parameters": [{"name": "p", "value": float("nan")}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": [float("inf")]}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": {"k": float("-inf")}}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": {1: "non-string key"}}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
        (
            {"parameters": [{"name": "p", "value": {"k": object()}}]},
            ERROR_CODE_INVALID_REQUEST,
        ),
    ],
)
def test_abstract_containers_keep_every_strictness_and_outcome(
    mapping: Any, sequence: Any, override: dict[str, Any], code: str
) -> None:
    request = _valid_request(**override)
    assert classify_analysis_start_request(request)[0] == code
    assert classify_analysis_start_request(_abstract(request, mapping, sequence)) == (
        classify_analysis_start_request(request)
    )


@pytest.mark.parametrize(("mapping", "sequence"), _CONTAINERS)
def test_abstract_temporal_and_timezone_rules_are_unchanged(
    mapping: Any, sequence: Any
) -> None:
    period = _valid_request(period_start="2026-09-01", period_end="2026-09-30")
    period.pop("as_of_date")
    assert (
        classify_analysis_start_request(_abstract(period, mapping, sequence))[0]
        == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )
    for override in (
        {"period_start": "2026-09-30", "period_end": "2026-09-01"},
        {
            "as_of_date": "2026-09-30",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
        },
        {"as_of_date": "2026-02-30"},
        {"business_timezone": "Not/AZone"},
    ):
        request = _valid_request(**override)
        assert (
            classify_analysis_start_request(_abstract(request, mapping, sequence))[0]
            == ERROR_CODE_INVALID_REQUEST
        )


@pytest.mark.parametrize(
    "text_or_bytes", ["currency=AUD", b"bytes", bytearray(b"bytes")]
)
def test_text_and_bytes_are_not_arrays(text_or_bytes: Any) -> None:
    code, _ = classify_analysis_start_request(_valid_request(parameters=text_or_bytes))
    assert code == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize("not_json", [b"\x01\x02", bytearray(b"\x01\x02")])
def test_bytes_never_pass_as_a_parameter_value_or_nested_array(not_json: Any) -> None:
    for value in (not_json, [not_json], {"k": not_json}):
        request = _valid_request(parameters=[{"name": "p", "value": value}])
        assert classify_analysis_start_request(request)[0] == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(("mapping", "sequence"), _CONTAINERS)
def test_abstract_container_nesting_depth_stays_bounded(
    mapping: Any, sequence: Any
) -> None:
    bomb: Any = None
    for _ in range(200):
        bomb = sequence([mapping({"depth": bomb})])
    request = _valid_request(
        parameters=sequence([mapping({"name": "p", "value": bomb})])
    )
    assert classify_analysis_start_request(request)[0] == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(("mapping", "sequence"), _CONTAINERS)
def test_abstract_containers_are_never_mutated(mapping: Any, sequence: Any) -> None:
    plain = _valid_request(parameters=[{"name": "p", "value": {"k": [1, 2]}}])
    request = _abstract(plain, mapping, sequence)
    classify_analysis_start_request(request)
    assert json.dumps(_plain(request), sort_keys=True) == json.dumps(
        plain, sort_keys=True
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_plain(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# CO-1: the catalogue posture
# ---------------------------------------------------------------------------


def test_the_operation_is_a_synchronous_workspace_read_with_its_own_capability() -> (
    None
):
    metadata: OperationMetadata = get_operation_metadata("analysis.start")
    assert metadata.scope.side_effect == "none"
    assert metadata.scope.scope_kind == "workspace"
    assert metadata.scope.required_scopes == ("insights:read",)
    assert metadata.required_capability.id == "insights.analysis"
    assert metadata.job.completion_mode == "synchronous"
    assert metadata.job.job_kind is None
    assert metadata.pagination.paginated is False
    assert metadata.audit.audit_category == "read"


def test_the_allowed_error_vocabulary_names_every_milestone_outcome() -> None:
    metadata = get_operation_metadata("analysis.start")
    for code in (
        ERROR_CODE_INVALID_REQUEST,
        ERROR_CODE_INCOMPATIBLE_VERSION,
        ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
        ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ):
        assert code in metadata.allowed_errors


def test_the_new_error_code_is_registered_with_its_frozen_retry_class() -> None:
    assert ERROR_CODE_UNSUPPORTED_MINOR_VERSION in FROZEN_ERROR_CODES


# ---------------------------------------------------------------------------
# Capability honesty and dependency independence
# ---------------------------------------------------------------------------


def test_the_reserved_result_type_advertises_no_members() -> None:
    """The success result is a reserved empty shape, and the corpus's success
    case reflects exactly that: an adapter reading the contract sees no
    members to populate and no job, queue or executor state anywhere."""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    success = [
        case
        for case in corpus["cases"]
        if case["operation"] == "analysis.start"
        and case["expect"]["branch"] == "success"
    ]
    assert len(success) == 1
    assert success[0]["response"]["result"] == {}
    assert "job" not in success[0]["response"]
    assert "queue" not in json.dumps(success[0]["response"])


def test_the_corpus_covers_the_milestone_outcomes() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    outcomes = {
        case.get("expect", {}).get("error_code")
        for case in corpus["cases"]
        if case["operation"] == "analysis.start"
    }
    assert {"unsupported_minor_version"} <= outcomes
    primary = [
        case["id"]
        for case in corpus["cases"]
        if case["operation"] == "analysis.start"
        and case["expect"]["branch"] == "success"
        and case.get("replay_of") is None
    ]
    assert primary == ["analysis.start/primary-success"]


def test_the_contract_package_imports_no_analytical_dependency() -> None:
    """Dependency independence: the refusal contract is served by the contract
    and service packages alone. No analytical engine appears anywhere in the
    contract source, so the milestone needs no dependency admission."""
    forbidden = ("duckdb", "sqlglot", "pyarrow")
    for path in CONTRACTS_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert f"import {name}" not in source
            assert f"from {name}" not in source


def test_the_supported_version_constant_is_the_only_admitted_payload_version() -> None:
    assert SUPPORTED_ANALYSIS_PAYLOAD_VERSION == "1.0"
    code, _ = classify_analysis_start_request(_valid_request())
    assert code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    code, _ = classify_analysis_start_request(
        _valid_request(request_version="1.0-beta")
    )
    assert code == ERROR_CODE_INVALID_REQUEST


# ---------------------------------------------------------------------------
# Optional-field presence and parameter value shape (generated codec parity)
# ---------------------------------------------------------------------------


def _decoded(request: Mapping[str, Any]) -> Any:
    """The request as it arrives after the generated envelope decoder."""
    envelope = RequestEnvelope(
        operation="analysis.start",
        metadata=RequestMetadata(
            request_id="req-analysis-1",
            correlation_id="cor-analysis-1",
            trace_id="trc-analysis-1",
            api_version="1.0",
            client=ClientIdentity(id="omnivia.cli", version="1.0.0"),
            scopes=(),
            purpose="insights_analysis_request",
            required_capabilities=(),
        ),
        input=dict(request),
    )
    return RequestEnvelope.from_wire(envelope.to_wire()).input


_ACCEPTED_OPTIONAL: list[dict[str, Any]] = [
    {},
    {"parameters": []},
    {"parameters": [{"name": "p", "value": {}}]},
    {
        "parameters": [
            {
                "name": "p",
                "value": {"n": None, "i": 1, "s": "x", "b": True, "l": [1, "a", None]},
            }
        ]
    },
    {"output_bounds": {}},
    {"output_bounds": {"max_rows": 1}},
]


@pytest.mark.parametrize("override", _ACCEPTED_OPTIONAL)
def test_omitted_and_empty_optional_values_are_accepted(
    override: dict[str, Any],
) -> None:
    request = _valid_request(**override)
    AnalysisStartInput.from_wire(request)
    assert classify_analysis_start_request(request)[0] == (
        ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )
    assert classify_analysis_start_request(_decoded(request))[0] == (
        ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )


_REJECTED_PRESENT_NULL: list[dict[str, Any]] = [
    {"parameters": None},
    {"output_bounds": None},
    {"output_bounds": {"max_rows": None}},
    {"request_version": None},
    {"target": None},
    {"target": {"kind": "metric", "metric_revision_id": None}},
    {"target": {"kind": "data_view", "data_view_revision_id": None}},
    {"parameters": [{"name": None, "value": {}}]},
    {"as_of_date": None},
    {"business_timezone": None},
    {"use_class": None},
    {"purpose_reference": None},
    {"as_of_date": _ABSENT, "period_start": None, "period_end": "2026-09-30"},
    {"as_of_date": _ABSENT, "period_start": "2026-09-01", "period_end": None},
]


@pytest.mark.parametrize("override", _REJECTED_PRESENT_NULL)
def test_a_present_null_is_invalid_request_not_an_omitted_field(
    override: dict[str, Any],
) -> None:
    request = _valid_request(**override)
    assert classify_analysis_start_request(request)[0] == ERROR_CODE_INVALID_REQUEST
    assert classify_analysis_start_request(_decoded(request))[0] == (
        ERROR_CODE_INVALID_REQUEST
    )
    with pytest.raises(ContractDecodeError):
        AnalysisStartInput.from_wire(request)


_NON_OBJECT_VALUES: list[Any] = [
    pytest.param(None, id="null"),
    pytest.param(True, id="bool"),
    pytest.param(1, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param("text", id="string"),
    pytest.param([], id="list"),
    pytest.param([1, "a"], id="list-of-scalars"),
    pytest.param((), id="tuple"),
    pytest.param(_CustomSequence([1]), id="custom-sequence"),
]


@pytest.mark.parametrize("value", _NON_OBJECT_VALUES)
def test_a_parameter_value_must_be_a_json_object(value: Any) -> None:
    request = _valid_request(parameters=[{"name": "p", "value": value}])
    assert classify_analysis_start_request(request)[0] == ERROR_CODE_INVALID_REQUEST
    assert classify_analysis_start_request(_decoded(request))[0] == (
        ERROR_CODE_INVALID_REQUEST
    )
    with pytest.raises(ContractDecodeError):
        AnalysisStartInput.from_wire(request)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(MappingProxyType({"k": (1, "a")}), id="mappingproxy-value"),
        pytest.param(_CustomMapping({"k": [None]}), id="custom-mapping-value"),
    ],
)
def test_an_abstract_mapping_value_is_a_json_object(value: Any) -> None:
    request = _valid_request(parameters=[{"name": "p", "value": value}])
    assert classify_analysis_start_request(request)[0] == (
        ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )
    assert classify_analysis_start_request(_decoded(request))[0] == (
        ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Hostile scalars and keys: exact-type gates, no operator ever invoked
# ---------------------------------------------------------------------------

# Every hostile operator raises this text. A classifier that invoked one would
# raise out of the call (failing the test), and a refusal must never echo it.
_OPERATOR_SECRET = "hostile-operator-secret-must-not-surface"


class _HashBombStr(str):
    def __hash__(self) -> int:
        raise RuntimeError(_OPERATOR_SECRET)


class _MethodBombStr(str):
    """A str whose equality, ordering, length and method protocols all raise."""

    def __eq__(self, other: object) -> bool:
        raise RuntimeError(_OPERATOR_SECRET)

    def __ne__(self, other: object) -> bool:
        raise RuntimeError(_OPERATOR_SECRET)

    def __le__(self, other: object) -> bool:
        raise RuntimeError(_OPERATOR_SECRET)

    def __len__(self) -> int:
        raise RuntimeError(_OPERATOR_SECRET)

    def split(self, *args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError(_OPERATOR_SECRET)


class _GeBombInt(int):
    def __ge__(self, other: object) -> bool:
        raise RuntimeError(_OPERATOR_SECRET)


class _EqBombFloat(float):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError(_OPERATOR_SECRET)


class _Tier(IntEnum):
    ONE = 1


class _Mode(StrEnum):
    EXPLORATION = "exploration"


class _PairsMapping(Mapping[Any, Any]):
    """A benign read-only Mapping over a list of pairs. Unlike a dict it never
    hashes its keys, so a hostile key can be yielded without being stored."""

    def __init__(self, pairs: Sequence[tuple[Any, Any]]) -> None:
        self._pairs = list(pairs)

    def __getitem__(self, key: Any) -> Any:
        for stored, value in self._pairs:
            if stored == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[Any]:
        return (stored for stored, _ in self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)


_HOSTILE_SCALAR_CASES = [
    pytest.param({"request_version": _HashBombStr("1.0")}, id="version-hash-bomb"),
    pytest.param({"request_version": _MethodBombStr("1.0")}, id="version-ne-bomb"),
    pytest.param({"request_version": _MethodBombStr("2.0")}, id="major-ne-bomb"),
    pytest.param({"use_class": _MethodBombStr("exploration")}, id="use-class-eq-bomb"),
    pytest.param({"use_class": _HashBombStr("exploration")}, id="use-class-hash-bomb"),
    pytest.param({"use_class": _Mode.EXPLORATION}, id="use-class-strenum"),
    pytest.param(
        {"target": {"kind": _MethodBombStr("metric"), "metric_revision_id": "m-1"}},
        id="target-kind-eq-bomb",
    ),
    pytest.param(
        {"target": {"kind": "metric", "metric_revision_id": _MethodBombStr("m-1")}},
        id="target-revision-len-bomb",
    ),
    pytest.param(
        {"target": {"kind": "metric", "metric_revision_id": _HashBombStr("m-1")}},
        id="target-revision-hash-bomb",
    ),
    pytest.param({"as_of_date": _HashBombStr("2026-09-30")}, id="as-of-hash-bomb"),
    pytest.param({"as_of_date": _MethodBombStr("2026-09-30")}, id="as-of-method-bomb"),
    pytest.param(
        {
            "as_of_date": _ABSENT,
            "period_start": _MethodBombStr("2026-09-01"),
            "period_end": "2026-09-30",
        },
        id="period-start-le-bomb",
    ),
    pytest.param(
        {
            "as_of_date": _ABSENT,
            "period_start": _MethodBombStr("2026-09-30"),
            "period_end": _MethodBombStr("2026-09-01"),
        },
        id="period-order-le-bomb",
    ),
    pytest.param(
        {"business_timezone": _MethodBombStr("Australia/Brisbane")},
        id="timezone-len-bomb",
    ),
    pytest.param(
        {"business_timezone": _HashBombStr("Australia/Brisbane")},
        id="timezone-hash-bomb",
    ),
    pytest.param(
        {"purpose_reference": _MethodBombStr("finance-exposure-review")},
        id="purpose-len-bomb",
    ),
    pytest.param(
        {"parameters": [{"name": _HashBombStr("p"), "value": {}}]},
        id="parameter-name-hash-bomb",
    ),
    pytest.param(
        {
            "parameters": [
                {"name": _HashBombStr("p"), "value": {}},
                {"name": _HashBombStr("p"), "value": {}},
            ]
        },
        id="duplicate-parameter-hash-bomb",
    ),
    pytest.param(
        {"parameters": [{"name": _MethodBombStr("p"), "value": {}}]},
        id="parameter-name-len-bomb",
    ),
    pytest.param(
        {"parameters": [{"name": "p", "value": {"k": _EqBombFloat(1.5)}}]},
        id="nested-float-eq-bomb",
    ),
    pytest.param(
        {"parameters": [{"name": "p", "value": {"k": _MethodBombStr("x")}}]},
        id="nested-str-method-bomb",
    ),
    pytest.param(
        {"parameters": [{"name": "p", "value": {"k": _GeBombInt(1)}}]},
        id="nested-int-ge-bomb",
    ),
    pytest.param({"output_bounds": {"max_rows": _GeBombInt(5)}}, id="max-rows-ge-bomb"),
    pytest.param({"output_bounds": {"max_rows": _Tier.ONE}}, id="max-rows-intenum"),
    pytest.param({"output_bounds": {"max_rows": True}}, id="max-rows-bool"),
]


@pytest.mark.parametrize("override", _HOSTILE_SCALAR_CASES)
def test_hostile_scalars_are_invalid_request_without_invoking_them(
    override: dict[str, Any],
) -> None:
    code, detail = classify_analysis_start_request(_valid_request(**override))
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail


@pytest.mark.parametrize("override", _HOSTILE_SCALAR_CASES)
def test_hostile_scalars_survive_the_decoder_and_are_refused(
    override: dict[str, Any],
) -> None:
    # The generic decoder keeps scalar subclasses as they are; the classifier,
    # not the decoder, must be the gate that refuses them.
    decoded = _decoded(_valid_request(**override))
    code, detail = classify_analysis_start_request(decoded)
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail


def test_a_non_finite_float_subclass_is_rejected_by_the_decoder_first() -> None:
    # The decoder's own finiteness check runs before classification, so a NaN
    # subclass is a ContractDecodeError rather than a classifier outcome.
    request = _valid_request(
        parameters=[{"name": "p", "value": {"k": _EqBombFloat("nan")}}]
    )
    with pytest.raises(ContractDecodeError):
        _decoded(request)


def _with_key(request: Mapping[str, Any], name: str, key: Any) -> _PairsMapping:
    return _PairsMapping(
        [(key if field == name else field, value) for field, value in request.items()]
    )


@pytest.mark.parametrize("hostile_key", [_HashBombStr, _MethodBombStr])
def test_a_hostile_key_at_the_root_is_refused_without_hashing_it(
    hostile_key: Any,
) -> None:
    request = _with_key(_valid_request(), "use_class", hostile_key("use_class"))
    code, detail = classify_analysis_start_request(request)
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail


@pytest.mark.parametrize("hostile_key", [_HashBombStr, _MethodBombStr])
def test_a_hostile_key_in_a_nested_parameter_value_is_refused_without_hashing_it(
    hostile_key: Any,
) -> None:
    value = _PairsMapping([(hostile_key("k"), 1)])
    request = _valid_request(parameters=[{"name": "p", "value": value}])
    code, detail = classify_analysis_start_request(request)
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail


def test_a_benign_mapping_with_only_exact_keys_still_classifies() -> None:
    request = _with_key(_valid_request(), "use_class", "use_class")
    assert classify_analysis_start_request(request)[0] == (
        ERROR_CODE_DEPENDENCY_UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Scalar-ancestry barrier: a str/int/float/bytes hybrid that also mixes in a
# container ABC is a scalar, never a JSON container, so no protocol method runs.
# ---------------------------------------------------------------------------

# Every hostile protocol method records its call here before raising, so a
# classifier that entered a container protocol fails on the call log even if
# the exception were swallowed.
_HYBRID_CALLS: list[str] = []


def _hostile_protocol(self: Any, *args: Any, **kwargs: Any) -> Any:
    _HYBRID_CALLS.append(type(self).__name__)
    raise RuntimeError(_OPERATOR_SECRET)


def _hybrid(scalar: type, container: type) -> type:
    """A scalar subclass mixing in a container ABC, every container method hostile.

    CPython lays out str, int, float, bytes and bytearray as variable-size types,
    but the ABC mixins add no layout, so each combination below is constructible.
    """
    namespace = {
        name: _hostile_protocol
        for name in ("__iter__", "__len__", "__getitem__", "__contains__", "keys")
    }
    return type(
        f"_{scalar.__name__}_{container.__name__}", (scalar, container), namespace
    )


_SCALAR_CONTAINER_HYBRIDS = [
    pytest.param(_hybrid(str, Mapping), id="str-mapping"),
    pytest.param(_hybrid(str, Sequence), id="str-sequence"),
    pytest.param(_hybrid(int, Mapping), id="int-mapping"),
    pytest.param(_hybrid(int, Sequence), id="int-sequence"),
    pytest.param(_hybrid(float, Mapping), id="float-mapping"),
    pytest.param(_hybrid(float, Sequence), id="float-sequence"),
    pytest.param(_hybrid(bytes, Mapping), id="bytes-mapping"),
    pytest.param(_hybrid(bytes, Sequence), id="bytes-sequence"),
    pytest.param(_hybrid(bytearray, Mapping), id="bytearray-mapping"),
    pytest.param(_hybrid(bytearray, Sequence), id="bytearray-sequence"),
]

_HYBRID_POSITIONS = [
    pytest.param(lambda h: {"use_class": h}, id="use-class"),
    pytest.param(lambda h: {"purpose_reference": h}, id="purpose-reference"),
    pytest.param(lambda h: {"business_timezone": h}, id="business-timezone"),
    pytest.param(lambda h: {"as_of_date": h}, id="as-of-date"),
    pytest.param(lambda h: {"target": h}, id="target"),
    pytest.param(
        lambda h: {"target": {"kind": "metric", "metric_revision_id": h}},
        id="target-revision",
    ),
    pytest.param(lambda h: {"parameters": h}, id="parameters"),
    pytest.param(
        lambda h: {"parameters": [{"name": "p", "value": h}]}, id="parameter-value"
    ),
    pytest.param(
        lambda h: {"parameters": [{"name": "p", "value": {"k": h}}]},
        id="nested-mapping-value",
    ),
    pytest.param(
        lambda h: {"parameters": [{"name": "p", "value": {"k": [h]}}]},
        id="nested-array-value",
    ),
    pytest.param(lambda h: {"output_bounds": h}, id="output-bounds"),
]


@pytest.mark.parametrize("hybrid", _SCALAR_CONTAINER_HYBRIDS)
def test_a_scalar_container_hybrid_as_the_root_document_is_invalid_request(
    hybrid: type,
) -> None:
    _HYBRID_CALLS.clear()
    code, detail = classify_analysis_start_request(hybrid())
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail
    assert _HYBRID_CALLS == []


@pytest.mark.parametrize("position", _HYBRID_POSITIONS)
@pytest.mark.parametrize("hybrid", _SCALAR_CONTAINER_HYBRIDS)
def test_a_scalar_container_hybrid_in_any_field_or_nested_position_is_invalid_request(
    hybrid: type, position: Any
) -> None:
    _HYBRID_CALLS.clear()
    request = _valid_request(**position(hybrid()))
    code, detail = classify_analysis_start_request(request)
    assert code == ERROR_CODE_INVALID_REQUEST
    assert _OPERATOR_SECRET not in detail
    assert _HYBRID_CALLS == []
