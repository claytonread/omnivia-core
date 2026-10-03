"""`decision.result_use.evaluate` evidence (T-0716, SPEC-CORE-DATA-001 §13.3).

`evaluate_result_use` is the one strict, fail-closed evaluator the public
operation renders and later checkpoints call directly. These tests pin its
decode order, its three refusal codes, the frozen policy truth table and the
trusted evaluation-instant boundary.
"""

from __future__ import annotations

import copy
import inspect
import itertools
import json
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts import v1
from omnivia_core.contracts.v1 import (
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    ResultUseRequestError,
    evaluate_result_use,
    is_timestamp,
    semantics_result_use,
)
from omnivia_core.contracts.v1.semantics_operations import get_operation_metadata

_SCHEMA_DIR = (
    Path(__file__).resolve().parents[2] / "contracts" / "application" / "v1" / "schemas"
)

_PERFECT: dict[str, Any] = {
    "request_version": "1.0",
    "use_class": "current_publication",
    "subject_digest": "result-digest-1",
    "completeness": "complete",
    "continuity": "verified",
    "freshness_ok": True,
    "schema_compatible": True,
    "evidence_available": True,
    "policy_permits_partial_or_stale": False,
    "authority_epoch": "epoch-1",
}
_FLAGS = (
    "freshness_ok",
    "schema_compatible",
    "evidence_available",
    "policy_permits_partial_or_stale",
)
_VOCABULARY = {
    "use_class": (
        "exploration",
        "historical_display",
        "current_publication",
        "action_input",
    ),
    "completeness": ("complete", "partial", "unknown"),
    "continuity": ("verified", "gap_detected", "unknown", "not_applicable"),
}
_USE_CLASSES = _VOCABULARY["use_class"]

#: A trusted instant ten hours east of UTC, with microseconds, so both the UTC
#: conversion and the precision of `valid_until` are observable.
_AT = datetime(2026, 10, 4, 21, 30, 15, 123456, tzinfo=timezone(timedelta(hours=10)))
_AT_WIRE = "2026-10-04T11:30:15.123456Z"

#: The category each reason belongs to, in the fixed reporting order.
_CATEGORY = {
    "completeness_unknown": 0,
    "completeness_partial_not_permitted": 0,
    "completeness_partial_permitted": 0,
    "continuity_gap_detected": 1,
    "continuity_unknown": 1,
    "freshness_requirement_failed": 2,
    "freshness_stale_not_permitted": 2,
    "freshness_stale_display": 2,
    "freshness_stale_exploration": 2,
    "schema_incompatible": 3,
    "evidence_unavailable": 4,
    "action_input_policy_missing": 5,
    "exploration_non_certifying": 6,
}
_WARNINGS = {
    "completeness_partial_permitted",
    "freshness_stale_display",
    "freshness_stale_exploration",
    "exploration_non_certifying",
}


def _valid(**overrides: Any) -> dict[str, Any]:
    return {**_PERFECT, **overrides}


def _evaluate(document: object) -> dict[str, Any]:
    return evaluate_result_use(document, evaluation_instant=_AT)


def _refusal(document: object) -> str:
    with pytest.raises(ResultUseRequestError) as raised:
        _evaluate(document)
    return raised.value.code


def _labels(use_class: str) -> list[str]:
    """The use-class reasons a decision for `use_class` always ends with."""
    return {
        "action_input": ["action_input_policy_missing"],
        "exploration": ["exploration_non_certifying"],
    }.get(use_class, [])


def _result_validator() -> Draft202012Validator:
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_SCHEMA_DIR.glob("*.schema.json"))
    ]
    return Draft202012Validator(
        {"$ref": get_operation_metadata("decision.result_use.evaluate").result_schema_ref},
        registry=Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in documents
        ),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


# --- the frozen policy truth table ------------------------------------------------

_ROWS: list[tuple[str, dict[str, Any], str, list[str]]] = [
    ("perfect-current", {}, "allow", []),
    ("perfect-historical", {"use_class": "historical_display"}, "allow", []),
    (
        "perfect-exploration",
        {"use_class": "exploration"},
        "allow_with_warning",
        ["exploration_non_certifying"],
    ),
    *[
        (
            f"perfect-action-policy-{flag}",
            {"use_class": "action_input", "policy_permits_partial_or_stale": flag},
            "deny",
            ["action_input_policy_missing"],
        )
        for flag in (False, True)
    ],
    (
        "partial-exploration-permitted",
        {
            "use_class": "exploration",
            "completeness": "partial",
            "policy_permits_partial_or_stale": True,
        },
        "allow_with_warning",
        ["completeness_partial_permitted", "exploration_non_certifying"],
    ),
    (
        "partial-historical-permitted",
        {
            "use_class": "historical_display",
            "completeness": "partial",
            "policy_permits_partial_or_stale": True,
        },
        "allow_with_warning",
        ["completeness_partial_permitted"],
    ),
    (
        "partial-exploration-not-permitted",
        {"use_class": "exploration", "completeness": "partial"},
        "deny",
        ["completeness_partial_not_permitted", "exploration_non_certifying"],
    ),
    (
        "partial-historical-not-permitted",
        {"use_class": "historical_display", "completeness": "partial"},
        "deny",
        ["completeness_partial_not_permitted"],
    ),
    *[
        (
            f"partial-{use_class}-policy-{flag}",
            {
                "use_class": use_class,
                "completeness": "partial",
                "policy_permits_partial_or_stale": flag,
            },
            "deny",
            ["completeness_partial_not_permitted", *_labels(use_class)],
        )
        for use_class in ("current_publication", "action_input")
        for flag in (False, True)
    ],
    *[
        (
            f"stale-{use_class}-policy-{flag}",
            {
                "use_class": use_class,
                "freshness_ok": False,
                "policy_permits_partial_or_stale": flag,
            },
            "deny",
            ["freshness_requirement_failed", *_labels(use_class)],
        )
        for use_class in ("current_publication", "action_input")
        for flag in (False, True)
    ],
    (
        "stale-historical-permitted",
        {
            "use_class": "historical_display",
            "freshness_ok": False,
            "policy_permits_partial_or_stale": True,
        },
        "allow_with_warning",
        ["freshness_stale_display"],
    ),
    (
        "stale-historical-not-permitted",
        {"use_class": "historical_display", "freshness_ok": False},
        "deny",
        ["freshness_stale_not_permitted"],
    ),
    (
        "stale-exploration-permitted",
        {
            "use_class": "exploration",
            "freshness_ok": False,
            "policy_permits_partial_or_stale": True,
        },
        "allow_with_warning",
        ["freshness_stale_exploration", "exploration_non_certifying"],
    ),
    (
        "stale-exploration-not-permitted",
        {"use_class": "exploration", "freshness_ok": False},
        "deny",
        ["freshness_stale_not_permitted", "exploration_non_certifying"],
    ),
    (
        "partial-and-stale-exploration-permitted",
        {
            "use_class": "exploration",
            "completeness": "partial",
            "freshness_ok": False,
            "policy_permits_partial_or_stale": True,
        },
        "allow_with_warning",
        [
            "completeness_partial_permitted",
            "freshness_stale_exploration",
            "exploration_non_certifying",
        ],
    ),
    # Every-use-class denials, under both policy flags: no permission softens
    # them, and evidence unavailability denies even the non-certifying uses.
    *[
        (
            f"{name}-{use_class}-policy-{flag}",
            {"use_class": use_class, field: value, "policy_permits_partial_or_stale": flag},
            "deny",
            [reason, *_labels(use_class)],
        )
        for name, field, value, reason in (
            ("unknown-completeness", "completeness", "unknown", "completeness_unknown"),
            ("gap", "continuity", "gap_detected", "continuity_gap_detected"),
            ("unknown-continuity", "continuity", "unknown", "continuity_unknown"),
            ("incompatible-schema", "schema_compatible", False, "schema_incompatible"),
            ("evidence-unavailable", "evidence_available", False, "evidence_unavailable"),
        )
        for use_class in _USE_CLASSES
        for flag in (False, True)
    ],
]


@pytest.mark.parametrize(
    ("overrides", "outcome", "reasons"),
    [row[1:] for row in _ROWS],
    ids=[row[0] for row in _ROWS],
)
def test_the_frozen_truth_table(
    overrides: dict[str, Any], outcome: str, reasons: list[str]
) -> None:
    assert _evaluate(_valid(**overrides)) == {
        "outcome": outcome,
        "reasons": reasons,
        "subject_digest": "result-digest-1",
        "authority_epoch": "epoch-1",
        "valid_until": _AT_WIRE,
    }


@pytest.mark.parametrize("use_class", ["current_publication", "action_input"])
def test_a_permissive_policy_never_lets_partial_scope_certify(use_class: str) -> None:
    decision = _evaluate(
        _valid(
            use_class=use_class,
            completeness="partial",
            policy_permits_partial_or_stale=True,
        )
    )
    assert decision["outcome"] == "deny"
    assert decision["reasons"][0] == "completeness_partial_not_permitted"


def test_stale_historical_display_follows_the_policy_flag() -> None:
    stale = _valid(use_class="historical_display", freshness_ok=False)
    permitted = _evaluate(dict(stale, policy_permits_partial_or_stale=True))
    refused = _evaluate(dict(stale, policy_permits_partial_or_stale=False))
    assert (permitted["outcome"], permitted["reasons"]) == (
        "allow_with_warning",
        ["freshness_stale_display"],
    )
    assert (refused["outcome"], refused["reasons"]) == (
        "deny",
        ["freshness_stale_not_permitted"],
    )


@pytest.mark.parametrize("use_class", _USE_CLASSES)
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"freshness_ok": False, "policy_permits_partial_or_stale": True},
        {"completeness": "partial", "policy_permits_partial_or_stale": True},
        {"schema_compatible": False},
    ],
    ids=["perfect", "stale-permitted", "partial-permitted", "schema-incompatible"],
)
def test_verified_and_not_applicable_continuity_are_both_neutral(
    use_class: str, overrides: dict[str, Any]
) -> None:
    verified = _evaluate(_valid(use_class=use_class, continuity="verified", **overrides))
    not_applicable = _evaluate(
        _valid(use_class=use_class, continuity="not_applicable", **overrides)
    )
    assert not_applicable == verified
    assert not [r for r in verified["reasons"] if r.startswith("continuity_")]


def test_simultaneous_reasons_are_all_reported_in_category_order() -> None:
    everything_wrong = {
        "completeness": "unknown",
        "continuity": "gap_detected",
        "freshness_ok": False,
        "schema_compatible": False,
        "evidence_available": False,
    }
    action = _evaluate(_valid(use_class="action_input", **everything_wrong))
    assert action["outcome"] == "deny"
    assert action["reasons"] == [
        "completeness_unknown",
        "continuity_gap_detected",
        "freshness_requirement_failed",
        "schema_incompatible",
        "evidence_unavailable",
        "action_input_policy_missing",
    ]
    exploration = _evaluate(
        _valid(
            use_class="exploration",
            completeness="partial",
            continuity="unknown",
            freshness_ok=False,
            schema_compatible=False,
            evidence_available=False,
        )
    )
    assert exploration["outcome"] == "deny"
    assert exploration["reasons"] == [
        "completeness_partial_not_permitted",
        "continuity_unknown",
        "freshness_stale_not_permitted",
        "schema_incompatible",
        "evidence_unavailable",
        "exploration_non_certifying",
    ]
    # A permitted warning is still reported beside a denial, and the denial wins.
    mixed = _evaluate(
        _valid(
            use_class="historical_display",
            completeness="partial",
            freshness_ok=False,
            evidence_available=False,
            policy_permits_partial_or_stale=True,
        )
    )
    assert mixed["outcome"] == "deny"
    assert mixed["reasons"] == [
        "completeness_partial_permitted",
        "freshness_stale_display",
        "evidence_unavailable",
    ]
    # The order is the category order, not the order the request names its fields.
    reordered = dict(
        reversed(list(_valid(use_class="action_input", **everything_wrong).items()))
    )
    assert _evaluate(reordered) == action


def test_every_decision_is_schema_valid_with_complete_unique_ordered_reasons() -> None:
    """The whole request space: 4 use classes x 3 x 4 x 2^4 = 768 decisions."""
    validator = _result_validator()
    requests = [
        _valid(
            use_class=use_class,
            completeness=completeness,
            continuity=continuity,
            **dict(zip(_FLAGS, flags, strict=True)),
        )
        for use_class, completeness, continuity, *flags in itertools.product(
            _USE_CLASSES,
            _VOCABULARY["completeness"],
            _VOCABULARY["continuity"],
            *[(True, False)] * len(_FLAGS),
        )
    ]
    assert len(requests) == 768
    for request in requests:
        decision = _evaluate(request)
        assert not list(validator.iter_errors(decision)), request
        reasons = decision["reasons"]
        # Exactly one reason for each applicable category, none for any other,
        # in the fixed order -- so reasons are complete, unique and ordered.
        applicable = {
            0: request["completeness"] != "complete",
            1: request["continuity"] in ("gap_detected", "unknown"),
            2: not request["freshness_ok"],
            3: not request["schema_compatible"],
            4: not request["evidence_available"],
            5: request["use_class"] == "action_input",
            6: request["use_class"] == "exploration",
        }
        assert [_CATEGORY[reason] for reason in reasons] == [
            category for category, applies in applicable.items() if applies
        ], request
        if not reasons:
            assert decision["outcome"] == "allow", request
        elif _WARNINGS.issuperset(reasons):
            assert decision["outcome"] == "allow_with_warning", request
        else:
            assert decision["outcome"] == "deny", request
        if request["use_class"] in ("current_publication", "action_input"):
            assert not _WARNINGS.intersection(reasons), request
        assert decision["valid_until"] == _AT_WIRE


# --- decoding: version first, then the strict 1.0 body ---------------------------


@pytest.mark.parametrize(
    "document",
    [None, [], [_PERFECT], json.dumps(_PERFECT), "1.0", 1, 1.5, True],
)
def test_a_non_object_is_invalid_request(document: object) -> None:
    assert _refusal(document) == ERROR_CODE_INVALID_REQUEST


def test_a_read_only_mapping_is_a_json_object() -> None:
    """Every wire transport decodes `input` into a read-only mapping."""
    assert _evaluate(MappingProxyType(_valid())) == _evaluate(_valid())


@pytest.mark.parametrize("field", sorted(_PERFECT))
def test_every_field_is_required(field: str) -> None:
    request = _valid()
    del request[field]
    assert _refusal(request) == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(
    "extra", ["unexpected", "decision", "outcome", "reasons", "valid_until"]
)
def test_an_extra_field_is_invalid_request(extra: str) -> None:
    assert _refusal(_valid(**{extra: True})) == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(
    "version",
    [
        # wrong type
        None, 1, 1.0, True, ["1.0"], {"major": 1, "minor": 0},
        # malformed
        "", "1", "1.", ".0", "1.0.0", "v1.0", "1,0", " 1.0", "1.0 ", "1.0\n",
        "1.-1", "+1.0", "one.zero",
        # noncanonical
        "01.0", "1.00", "00.0", "1.01",
        # overlong: canonical digits past the 32-character bound
        "1." + "1" * 31, "1" * 31 + ".0",
    ],
)
def test_a_malformed_version_is_invalid_request(version: object) -> None:
    assert _refusal(_valid(request_version=version)) == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize("version", ["0.0", "0.9", "2.0", "2.5", "10.0", "1" * 30 + ".0"])
def test_a_major_other_than_one_is_incompatible_version(version: str) -> None:
    assert _refusal(_valid(request_version=version)) == ERROR_CODE_INCOMPATIBLE_VERSION


@pytest.mark.parametrize("version", ["1.1", "1.7", "1.10", "1." + "1" * 30])
def test_a_minor_other_than_zero_is_unsupported_minor_version(version: str) -> None:
    assert (
        _refusal(_valid(request_version=version))
        == ERROR_CODE_UNSUPPORTED_MINOR_VERSION
    )


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"use_class": "action_input"},
        _valid(unexpected=True),
        _valid(use_class=7, freshness_ok="false"),
        _valid(subject_digest="-bad", continuity="maybe"),
    ],
    ids=["version-only", "missing-fields", "extra-field", "wrong-types", "bad-values"],
)
@pytest.mark.parametrize(
    ("version", "code"),
    [
        ("2.0", ERROR_CODE_INCOMPATIBLE_VERSION),
        ("1.7", ERROR_CODE_UNSUPPORTED_MINOR_VERSION),
    ],
)
def test_the_version_is_classified_before_the_body(
    document: dict[str, Any], version: str, code: str
) -> None:
    assert _refusal({**document, "request_version": version}) == code


@pytest.mark.parametrize("field", ["subject_digest", "authority_epoch"])
@pytest.mark.parametrize(
    "value",
    [
        "", "-leading-dash", ".leading-dot", "has space", "slash/inside",
        "trailing-newline\n", "x" * 129,
        None, 7, True, ["epoch-1"], {"id": "epoch-1"},
    ],
)
def test_an_invalid_identifier_is_invalid_request(field: str, value: object) -> None:
    assert _refusal(_valid(**{field: value})) == ERROR_CODE_INVALID_REQUEST


def test_identifiers_are_bounded_exactly_as_the_contract_declares() -> None:
    longest = "x" * 128
    decision = _evaluate(_valid(subject_digest=longest, authority_epoch="a:b.c-d_e"))
    assert decision["subject_digest"] == longest
    assert decision["authority_epoch"] == "a:b.c-d_e"


@pytest.mark.parametrize("field", sorted(_VOCABULARY))
@pytest.mark.parametrize(
    "value",
    [
        # outside every vocabulary
        "", "other", "EXPLORATION", "Complete", "verified ", " unknown",
        "not-applicable",
        # wrong type
        None, 0, 1, True, False, 1.5, [], {}, ["exploration"], {"value": "complete"},
    ],
)
def test_a_value_outside_the_vocabulary_is_invalid_request(
    field: str, value: object
) -> None:
    assert _refusal(_valid(**{field: value})) == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("use_class", "complete"), ("use_class", "unknown"),
        ("completeness", "verified"), ("completeness", "exploration"),
        ("completeness", "not_applicable"),
        ("continuity", "partial"), ("continuity", "complete"),
    ],
)
def test_a_value_from_another_vocabulary_is_invalid_request(
    field: str, value: str
) -> None:
    assert _refusal(_valid(**{field: value})) == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize("field", _FLAGS)
@pytest.mark.parametrize(
    "value", ["true", "false", 0, 1, None, [], [True], {}, {"value": True}]
)
def test_a_flag_that_is_not_a_real_boolean_is_invalid_request(
    field: str, value: object
) -> None:
    assert _refusal(_valid(**{field: value})) == ERROR_CODE_INVALID_REQUEST


# --- purity and the trusted instant ------------------------------------------------


def test_evaluation_never_mutates_its_input_and_repeats_exactly() -> None:
    request = _valid(
        use_class="exploration",
        completeness="partial",
        policy_permits_partial_or_stale=True,
    )
    pristine = copy.deepcopy(request)
    first = _evaluate(request)
    first["reasons"].append("tampered")
    repeated = [_evaluate(request) for _ in range(3)]
    assert request == pristine
    assert list(request) == list(pristine)
    assert all(decision == repeated[0] for decision in repeated)
    assert repeated[0]["reasons"] == [
        "completeness_partial_permitted",
        "exploration_non_certifying",
    ]


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (_AT, _AT_WIRE),
        (datetime(2026, 10, 4, tzinfo=UTC), "2026-10-04T00:00:00Z"),
        (
            datetime(2026, 12, 31, 20, tzinfo=timezone(timedelta(hours=-5))),
            "2027-01-01T01:00:00Z",
        ),
    ],
)
def test_valid_until_is_the_evaluation_instant_in_canonical_utc(
    instant: datetime, expected: str
) -> None:
    decision = evaluate_result_use(_valid(), evaluation_instant=instant)
    assert decision["valid_until"] == expected
    assert is_timestamp(expected)
    assert datetime.fromisoformat(expected) == instant


def test_the_evaluation_instant_is_a_required_keyword() -> None:
    parameter = inspect.signature(evaluate_result_use).parameters["evaluation_instant"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        evaluate_result_use(_valid())  # type: ignore[call-arg]


class _NoOffset(tzinfo):
    """A tzinfo that names no offset, which leaves its datetime naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 10, 4, 11, 30),  # noqa: DTZ001 - naive is the case under test
        datetime(2026, 10, 4, tzinfo=_NoOffset()),
        date(2026, 10, 4),
        "2026-10-04T11:30:15Z",
        1_791_000_000,
        None,
    ],
    ids=["naive", "offsetless-tzinfo", "date", "string", "epoch", "none"],
)
@pytest.mark.parametrize(
    "document",
    [
        _valid(),
        None,
        _valid(request_version="x"),
        _valid(request_version="2.0"),
        _valid(request_version="1.7"),
        _valid(use_class="certified"),
    ],
    ids=["valid", "non-object", "malformed", "major", "minor", "body"],
)
def test_an_untrusted_instant_is_a_programmer_error_before_any_request_check(
    instant: object, document: object
) -> None:
    with pytest.raises(TypeError) as raised:
        evaluate_result_use(document, evaluation_instant=instant)  # type: ignore[arg-type]
    assert not isinstance(raised.value, ResultUseRequestError)


# --- the bounded request error and the catalogue posture --------------------------


def test_the_request_error_is_public_and_carries_only_its_code() -> None:
    assert "ResultUseRequestError" in v1.__all__
    assert v1.ResultUseRequestError is semantics_result_use.ResultUseRequestError
    assert issubclass(ResultUseRequestError, ValueError)
    marked = {"subject_digest": "marker-subject-7f3a", "authority_epoch": "marker-epoch"}
    for document, code in (
        (_valid(use_class="marker-use-class", **marked), ERROR_CODE_INVALID_REQUEST),
        (_valid(request_version="9.9", **marked), ERROR_CODE_INCOMPATIBLE_VERSION),
        (_valid(request_version="1.7", **marked), ERROR_CODE_UNSUPPORTED_MINOR_VERSION),
    ):
        with pytest.raises(ResultUseRequestError) as raised:
            _evaluate(document)
        error = raised.value
        assert error.code == code
        assert error.args == (code,)
        assert str(error) == code
        assert repr(error) == f"ResultUseRequestError({code!r})"
        assert error.__cause__ is None
        assert error.__context__ is None
        for value in document.values():
            if isinstance(value, str):
                assert value not in str(error)
                assert value not in repr(error)


def test_the_catalogue_posture_is_a_synchronous_workspace_read() -> None:
    metadata = get_operation_metadata("decision.result_use.evaluate")
    assert metadata.scope.side_effect == "none"
    assert metadata.scope.required_scopes == ("decision:read",)
    assert metadata.required_capability.id == "decision.read"
    assert metadata.job.completion_mode == "synchronous"
