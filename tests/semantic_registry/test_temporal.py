"""Executable tests for the Phase 2 temporal contract (decision record section 4).

Covers `canonical_utc`/`TemporalInstant` invariants, `EffectiveValidInterval`
half-open/contract-version invariants, `parse_source_time` timezone
resolution, `resolve_effective_valid_interval` B3 precedence, record-time
selection, the JSON projection, and executable replay of every temporal case
in the phase-2 acceptance fixture corpus that the current API can represent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    TemporalValidationError,
)
from omnivia_core.semantic_registry.temporal import (
    TEMPORAL_CONTRACT_VERSION,
    EffectiveValidInterval,
    EndBoundaryState,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
    canonical_utc,
    effective_interval_projection,
    parse_source_time,
    resolve_effective_valid_interval,
    select_record_time,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "semantic_registry"
    / "phase-2-acceptance-v1.json"
)


def _instant(
    value: datetime,
    precision: TemporalPrecision = TemporalPrecision.DAY,
    provenance: TemporalProvenance = TemporalProvenance.STATED,
) -> TemporalInstant:
    return TemporalInstant(value=value, precision=precision, provenance=provenance)


# --- canonical_utc ------------------------------------------------------


@pytest.mark.parametrize(
    ("precision", "expected"),
    [
        (TemporalPrecision.YEAR, datetime(2024, 1, 1, tzinfo=UTC)),
        (TemporalPrecision.MONTH, datetime(2024, 5, 1, tzinfo=UTC)),
        (TemporalPrecision.DAY, datetime(2024, 5, 17, tzinfo=UTC)),
        (TemporalPrecision.HOUR, datetime(2024, 5, 17, 9, tzinfo=UTC)),
        (TemporalPrecision.MINUTE, datetime(2024, 5, 17, 9, 30, tzinfo=UTC)),
        (TemporalPrecision.SECOND, datetime(2024, 5, 17, 9, 30, 45, tzinfo=UTC)),
    ],
)
def test_canonical_utc_truncates_for_every_precision(
    precision: TemporalPrecision, expected: datetime
) -> None:
    value = datetime(2024, 5, 17, 9, 30, 45, 123456, tzinfo=UTC)
    assert canonical_utc(value, precision) == expected


def test_canonical_utc_normalizes_offset_to_utc() -> None:
    value = datetime(2024, 5, 17, 9, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert canonical_utc(value, TemporalPrecision.MINUTE) == datetime(
        2024, 5, 17, 14, 30, tzinfo=UTC
    )


def test_canonical_utc_rejects_naive_datetime() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        canonical_utc(datetime(2024, 5, 17), TemporalPrecision.DAY)  # noqa: DTZ001
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE


# --- TemporalInstant -----------------------------------------------------


def test_temporal_instant_accepts_canonical_utc_value() -> None:
    instant = _instant(datetime(2024, 5, 17, tzinfo=UTC))
    assert instant.value == datetime(2024, 5, 17, tzinfo=UTC)


def test_temporal_instant_rejects_naive_value() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        TemporalInstant(
            value=datetime(2024, 5, 17),  # noqa: DTZ001
            precision=TemporalPrecision.DAY,
            provenance=TemporalProvenance.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE


def test_temporal_instant_rejects_non_utc_offset() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        TemporalInstant(
            value=datetime(2024, 5, 17, tzinfo=timezone(timedelta(hours=-5))),
            precision=TemporalPrecision.DAY,
            provenance=TemporalProvenance.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_temporal_instant_rejects_untruncated_value() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        TemporalInstant(
            value=datetime(2024, 5, 17, 9, 30, tzinfo=UTC),
            precision=TemporalPrecision.DAY,
            provenance=TemporalProvenance.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_temporal_instant_rejects_invalid_precision() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        TemporalInstant(
            value=datetime(2024, 5, 17, tzinfo=UTC),
            precision="decade",  # type: ignore[arg-type]
            provenance=TemporalProvenance.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


def test_temporal_instant_rejects_invalid_provenance() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        TemporalInstant(
            value=datetime(2024, 5, 17, tzinfo=UTC),
            precision=TemporalPrecision.DAY,
            provenance="guessed",  # type: ignore[arg-type]
        )
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


# --- EffectiveValidInterval ----------------------------------------------


def test_interval_is_always_half_open_with_exact_contract_version() -> None:
    interval = EffectiveValidInterval(
        effective_from=_instant(datetime(2024, 1, 1, tzinfo=UTC)),
        effective_to=_instant(datetime(2024, 2, 1, tzinfo=UTC)),
        end_state=EndBoundaryState.STATED,
    )
    assert interval.half_open is True
    assert interval.contract_version == TEMPORAL_CONTRACT_VERSION == "effective-valid-interval-v1"


def test_interval_rejects_caller_override_of_half_open() -> None:
    with pytest.raises(TypeError):
        EffectiveValidInterval(  # type: ignore[call-arg]
            effective_from=_instant(datetime(2024, 1, 1, tzinfo=UTC)),
            effective_to=_instant(datetime(2024, 2, 1, tzinfo=UTC)),
            end_state=EndBoundaryState.STATED,
            half_open=False,
        )


def test_interval_rejects_open_with_stated_end() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        EffectiveValidInterval(
            effective_from=_instant(datetime(2024, 1, 1, tzinfo=UTC)),
            effective_to=_instant(datetime(2024, 2, 1, tzinfo=UTC)),
            end_state=EndBoundaryState.OPEN,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID


def test_interval_rejects_non_open_without_end() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        EffectiveValidInterval(
            effective_from=_instant(datetime(2024, 1, 1, tzinfo=UTC)),
            effective_to=None,
            end_state=EndBoundaryState.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_END_INDETERMINATE


def test_interval_rejects_start_at_or_after_end() -> None:
    same = _instant(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(TemporalValidationError) as excinfo:
        EffectiveValidInterval(
            effective_from=same,
            effective_to=same,
            end_state=EndBoundaryState.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID

    with pytest.raises(TemporalValidationError) as excinfo:
        EffectiveValidInterval(
            effective_from=_instant(datetime(2024, 2, 1, tzinfo=UTC)),
            effective_to=_instant(datetime(2024, 1, 1, tzinfo=UTC)),
            end_state=EndBoundaryState.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID


# --- parse_source_time ----------------------------------------------------


def test_parse_source_time_explicit_z() -> None:
    instant = parse_source_time("2024-05-17T09:30:00Z", TemporalPrecision.MINUTE)
    assert instant.value == datetime(2024, 5, 17, 9, 30, tzinfo=UTC)
    assert instant.source_timezone is None
    assert instant.original_source_text == "2024-05-17T09:30:00Z"


def test_parse_source_time_explicit_offset() -> None:
    instant = parse_source_time("2024-05-17T09:30:00-05:00", TemporalPrecision.MINUTE)
    assert instant.value == datetime(2024, 5, 17, 14, 30, tzinfo=UTC)
    assert instant.source_timezone is None


def test_parse_source_time_trusted_iana_source_timezone() -> None:
    instant = parse_source_time(
        "2024-05-17T09:30:00",
        TemporalPrecision.MINUTE,
        trusted_source_timezone="America/Chicago",
    )
    assert instant.value == datetime(2024, 5, 17, 14, 30, tzinfo=UTC)
    assert instant.source_timezone == "America/Chicago"
    assert instant.original_source_text == "2024-05-17T09:30:00"


def test_parse_source_time_invalid_timezone_name() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time(
            "2024-05-17T09:30:00",
            TemporalPrecision.MINUTE,
            trusted_source_timezone="Not/A_Zone",
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE


def test_parse_source_time_timezone_less_without_trusted_zone_fails_closed() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time("2024-05-17T09:30:00", TemporalPrecision.MINUTE)
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE


def test_parse_source_time_invalid_calendar_input() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time("not-a-datetime", TemporalPrecision.DAY)
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_parse_source_time_preserves_original_source_text() -> None:
    source_text = "2024-05-17T09:30:00-05:00"
    instant = parse_source_time(source_text, TemporalPrecision.MINUTE)
    assert instant.original_source_text == source_text


@pytest.mark.parametrize(
    "source_text",
    ["SECRET_UNTRUSTED_MARKER_ABC123", "not-a-datetime"],
)
def test_parse_source_time_error_messages_never_echo_source_text(source_text: str) -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time(source_text, TemporalPrecision.DAY)
    assert source_text not in str(excinfo.value)


def test_parse_source_time_timezone_less_error_does_not_echo_source_text() -> None:
    source_text = "2024-05-17T09:30:00"
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time(source_text, TemporalPrecision.MINUTE)
    assert source_text not in str(excinfo.value)


# --- resolve_effective_valid_interval (B3 precedence) ---------------------


def test_resolve_prefers_valid_from_over_attested_from() -> None:
    valid_from = _instant(datetime(2025, 1, 1, tzinfo=UTC))
    attested_from = _instant(
        datetime(2025, 3, 1, tzinfo=UTC), provenance=TemporalProvenance.EVIDENCE_ATTESTED
    )
    interval = resolve_effective_valid_interval(
        valid_from=valid_from,
        attested_from=attested_from,
        end_state=EndBoundaryState.OPEN,
    )
    assert interval.effective_from is valid_from


def test_resolve_falls_back_to_attested_from_when_valid_from_absent() -> None:
    attested_from = _instant(
        datetime(2025, 2, 15, tzinfo=UTC), provenance=TemporalProvenance.EVIDENCE_ATTESTED
    )
    interval = resolve_effective_valid_interval(
        valid_from=None,
        attested_from=attested_from,
        end_state=EndBoundaryState.OPEN,
    )
    assert interval.effective_from is attested_from


def test_resolve_start_indeterminate_when_both_absent() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        resolve_effective_valid_interval(
            valid_from=None,
            attested_from=None,
            end_state=EndBoundaryState.OPEN,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_START_INDETERMINATE


def test_resolve_stated_end_uses_valid_to() -> None:
    valid_from = _instant(datetime(2025, 1, 1, tzinfo=UTC))
    valid_to = _instant(datetime(2025, 6, 1, tzinfo=UTC))
    interval = resolve_effective_valid_interval(
        valid_from=valid_from,
        attested_from=None,
        end_state=EndBoundaryState.STATED,
        valid_to=valid_to,
    )
    assert interval.effective_to is valid_to
    assert interval.end_state is EndBoundaryState.STATED


def test_resolve_stated_end_without_valid_to_is_indeterminate() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        resolve_effective_valid_interval(
            valid_from=_instant(datetime(2025, 1, 1, tzinfo=UTC)),
            attested_from=None,
            end_state=EndBoundaryState.STATED,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_END_INDETERMINATE


def test_resolve_unknown_end_falls_back_to_attested_to() -> None:
    valid_from = _instant(datetime(2025, 2, 15, tzinfo=UTC))
    attested_to = _instant(
        datetime(2025, 8, 15, tzinfo=UTC), provenance=TemporalProvenance.EVIDENCE_ATTESTED
    )
    interval = resolve_effective_valid_interval(
        valid_from=valid_from,
        attested_from=None,
        end_state=EndBoundaryState.UNKNOWN,
        attested_to=attested_to,
    )
    assert interval.effective_to is attested_to
    assert interval.end_state is EndBoundaryState.UNKNOWN


def test_resolve_unknown_end_without_attested_to_is_indeterminate() -> None:
    with pytest.raises(TemporalValidationError) as excinfo:
        resolve_effective_valid_interval(
            valid_from=_instant(datetime(2025, 3, 1, tzinfo=UTC)),
            attested_from=None,
            end_state=EndBoundaryState.UNKNOWN,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_END_INDETERMINATE


def test_resolve_open_end_produces_none_effective_to() -> None:
    valid_from = _instant(datetime(2025, 4, 1, tzinfo=UTC))
    interval = resolve_effective_valid_interval(
        valid_from=valid_from,
        attested_from=None,
        end_state=EndBoundaryState.OPEN,
    )
    assert interval.effective_to is None
    assert interval.end_state is EndBoundaryState.OPEN


def test_resolve_invalid_interval_start_not_before_end() -> None:
    same = _instant(datetime(2025, 1, 1, tzinfo=UTC))
    with pytest.raises(TemporalValidationError) as excinfo:
        resolve_effective_valid_interval(
            valid_from=same,
            attested_from=None,
            end_state=EndBoundaryState.STATED,
            valid_to=same,
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID


# --- select_record_time ----------------------------------------------------


def test_select_record_time_prefers_authorised_source_time() -> None:
    authorised = _instant(datetime(2019, 6, 15, tzinfo=UTC))
    ingestion = _instant(datetime(2026, 1, 20, tzinfo=UTC))
    result = select_record_time(
        authorised_source_time=authorised, ingestion_time=ingestion
    )
    assert result is authorised


def test_select_record_time_falls_back_explicitly_to_ingestion_time() -> None:
    ingestion = _instant(datetime(2026, 1, 20, tzinfo=UTC))
    result = select_record_time(authorised_source_time=None, ingestion_time=ingestion)
    assert result.value == ingestion.value
    assert result.provenance is TemporalProvenance.INGESTION_FALLBACK
    assert result is not ingestion


# --- effective_interval_projection ----------------------------------------


def test_projection_fields_for_closed_interval() -> None:
    interval = EffectiveValidInterval(
        effective_from=_instant(
            datetime(2025, 1, 1, tzinfo=UTC), provenance=TemporalProvenance.STATED
        ),
        effective_to=_instant(
            datetime(2025, 6, 1, tzinfo=UTC), provenance=TemporalProvenance.STATED
        ),
        end_state=EndBoundaryState.STATED,
    )
    projection = effective_interval_projection(interval)
    assert projection == {
        "effective_from": "2025-01-01T00:00:00Z",
        "effective_from_precision": "day",
        "effective_from_provenance": "stated",
        "effective_to": "2025-06-01T00:00:00Z",
        "effective_to_precision": "day",
        "effective_to_provenance": "stated",
        "end_state": "stated",
        "interval_type": "half_open",
        "contract_version": TEMPORAL_CONTRACT_VERSION,
    }


def test_projection_uses_positive_infinity_only_for_open_end() -> None:
    interval = EffectiveValidInterval(
        effective_from=_instant(datetime(2025, 4, 1, tzinfo=UTC)),
        effective_to=None,
        end_state=EndBoundaryState.OPEN,
    )
    projection = effective_interval_projection(interval)
    assert projection["effective_to"] == "+Infinity"
    assert projection["effective_to_precision"] is None
    assert projection["effective_to_provenance"] is None
    assert projection["end_state"] == "open"


def test_projection_does_not_use_positive_infinity_for_closed_ends() -> None:
    interval = EffectiveValidInterval(
        effective_from=_instant(datetime(2025, 1, 1, tzinfo=UTC)),
        effective_to=_instant(datetime(2025, 6, 1, tzinfo=UTC)),
        end_state=EndBoundaryState.STATED,
    )
    projection = effective_interval_projection(interval)
    assert projection["effective_to"] != "+Infinity"


# --- fixture corpus replay --------------------------------------------------


def _load_cases() -> dict[str, dict]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in document["cases"]}


CASES = _load_cases()

ERROR_CODE_BY_NAME = {code.name: code for code in SemanticErrorCode}


def _boundary_instant(spec: dict, default_precision: str = "day") -> TemporalInstant | None:
    state = spec["state"]
    if state in ("absent", "open", "unknown"):
        return None
    precision = TemporalPrecision(spec.get("precision", default_precision))
    if "source_text" in spec:
        trusted_tz = None
        if spec.get("timezone_kind") == "trusted_source_default":
            trusted_tz = "America/Chicago"
        return parse_source_time(spec["source_text"], precision, trusted_tz)
    value = datetime.fromisoformat(spec["value"])
    return TemporalInstant(
        value=canonical_utc(value, precision),
        precision=precision,
        provenance=TemporalProvenance.STATED
        if state == "stated"
        else TemporalProvenance.EVIDENCE_ATTESTED,
    )


def _end_state_for(spec: dict) -> EndBoundaryState:
    return EndBoundaryState(spec["state"])


def _resolve_boundary_case(case: dict) -> EffectiveValidInterval:
    inputs = case["input"]
    valid_from = _boundary_instant(inputs["valid_from"]) if "valid_from" in inputs else None
    attested_from = (
        _boundary_instant(inputs["attested_from"]) if "attested_from" in inputs else None
    )
    valid_to_spec = inputs.get("valid_to")
    end_state = _end_state_for(valid_to_spec) if valid_to_spec else EndBoundaryState.OPEN
    valid_to = (
        _boundary_instant(valid_to_spec) if valid_to_spec and valid_to_spec["state"] == "stated"
        else None
    )
    attested_to = (
        _boundary_instant(inputs["attested_to"]) if "attested_to" in inputs else None
    )
    return resolve_effective_valid_interval(
        valid_from=valid_from,
        attested_from=attested_from,
        end_state=end_state,
        valid_to=valid_to,
        attested_to=attested_to,
    )


BOUNDARY_RESULT_CASE_IDS = [
    "case-temporal-boundary-stated",
    "case-temporal-boundary-attested",
    "case-temporal-boundary-open",
]

BOUNDARY_ERROR_CASE_IDS = [
    "case-temporal-boundary-absent",
    "case-temporal-boundary-unknown",
]

PRECISION_CASE_IDS = [
    "case-temporal-precision-year",
    "case-temporal-precision-month",
    "case-temporal-precision-day",
    "case-temporal-precision-hour",
    "case-temporal-precision-minute",
    "case-temporal-precision-second",
]

TIMEZONE_RESULT_CASE_IDS = [
    "case-temporal-timezone-explicit-offset",
    "case-temporal-timezone-trusted-source",
]


@pytest.mark.parametrize("case_id", BOUNDARY_RESULT_CASE_IDS)
def test_fixture_boundary_cases_resolve_as_expected(case_id: str) -> None:
    case = CASES[case_id]
    expected = case["expected_result"]
    interval = _resolve_boundary_case(case)
    projection = effective_interval_projection(interval)
    assert projection["effective_from"] == expected["effective_from"]
    assert projection["effective_to"] == expected["effective_to"]
    assert projection["interval_type"] == expected["interval_type"]
    assert projection["end_state"] == expected["end_state"]


@pytest.mark.parametrize("case_id", BOUNDARY_ERROR_CASE_IDS)
def test_fixture_boundary_cases_fail_closed_with_matching_error_code(case_id: str) -> None:
    case = CASES[case_id]
    expected_code = ERROR_CODE_BY_NAME[case["expected_error"]["error_code"]]
    with pytest.raises(TemporalValidationError) as excinfo:
        _resolve_boundary_case(case)
    assert excinfo.value.code is expected_code


@pytest.mark.parametrize("case_id", PRECISION_CASE_IDS)
def test_fixture_precision_cases_truncate_and_preserve_source_text(case_id: str) -> None:
    # The fixture's "source_text" here is an opaque placeholder (not parseable
    # ISO 8601); the actual instant to truncate is carried in "value". We
    # exercise canonical_utc() for truncation and separately assert that a
    # TemporalInstant preserves whatever original_source_text it is given.
    case = CASES[case_id]
    expected = case["expected_result"]
    valid_from_spec = case["input"]["valid_from"]
    precision = TemporalPrecision(valid_from_spec["precision"])
    value = datetime.fromisoformat(valid_from_spec["value"])
    canonical = canonical_utc(value, precision)
    instant = TemporalInstant(
        value=canonical,
        precision=precision,
        provenance=TemporalProvenance.STATED,
        original_source_text=valid_from_spec["source_text"],
    )
    assert instant.value.strftime("%Y-%m-%dT%H:%M:%SZ") == expected["effective_from"]
    assert instant.precision.value == expected["canonical_precision"]
    assert instant.original_source_text == expected["preserved_source_text"]


@pytest.mark.parametrize("case_id", TIMEZONE_RESULT_CASE_IDS)
def test_fixture_timezone_cases_resolve_as_expected(case_id: str) -> None:
    case = CASES[case_id]
    expected = case["expected_result"]
    spec = case["input"]["valid_from"]
    trusted_tz = case["input"].get("trusted_source_timezone")
    instant = parse_source_time(
        spec["source_text"], TemporalPrecision(spec["precision"]), trusted_tz
    )
    assert instant.value.strftime("%Y-%m-%dT%H:%M:%SZ") == expected["effective_from"]
    assert instant.original_source_text == expected["preserved_source_text"]
    if "applied_timezone" in expected:
        assert instant.source_timezone == expected["applied_timezone"]


def test_fixture_timezone_less_text_case_fails_closed() -> None:
    case = CASES["case-temporal-timezone-less-text"]
    expected_code = ERROR_CODE_BY_NAME[case["expected_error"]["error_code"]]
    spec = case["input"]["valid_from"]
    with pytest.raises(TemporalValidationError) as excinfo:
        parse_source_time(
            spec["source_text"],
            TemporalPrecision(spec["precision"]),
            case["input"]["trusted_source_timezone"],
        )
    assert excinfo.value.code is expected_code


def test_fixture_historical_backfill_uses_authorised_source_time() -> None:
    case = CASES["case-temporal-historical-backfill-source-time"]
    expected = case["expected_result"]
    ingestion = TemporalInstant(
        value=datetime.fromisoformat(case["input"]["ingested_at"]),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )
    authorised = TemporalInstant(
        value=datetime.fromisoformat(case["input"]["authorised_source_time"]),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )
    result = select_record_time(
        authorised_source_time=authorised, ingestion_time=ingestion
    )
    assert result.value.strftime("%Y-%m-%dT%H:%M:%SZ") == expected["effective_record_time"]
    assert result is authorised
    assert not expected["ingestion_time_used_as_fallback"]


def test_fixture_historical_backfill_falls_back_to_ingestion_time() -> None:
    case = CASES["case-temporal-historical-backfill-ingestion-fallback"]
    expected = case["expected_result"]
    ingestion = TemporalInstant(
        value=datetime.fromisoformat(case["input"]["ingested_at"]),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )
    result = select_record_time(authorised_source_time=None, ingestion_time=ingestion)
    assert result.value.strftime("%Y-%m-%dT%H:%M:%SZ") == expected["effective_record_time"]
    assert result.provenance is TemporalProvenance.INGESTION_FALLBACK
    assert expected["ingestion_time_used_as_fallback"]
