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
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1 import (
    ADMITTED_ANALYSIS_USE_CLASSES,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    FROZEN_ERROR_CODES,
    OperationMetadata,
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
