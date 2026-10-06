"""Analytical result artifact value and encoding protocol.

The encoder is pure, so every test builds real values and real bytes. The properties
proved: encoding is deterministic and the digests are exact; each logical type accepts
only its exact Python type and round-trips through the schema; hostile subclasses and
containers refuse; the caller's row and byte bounds refuse rather than truncate; the
candidate is immutable and re-proves its own identity; and every refusal is one fixed
message that carries no row value, SQL or path. Candidates are built only by the encoder and the validator, never through the public
constructor, the echo binds
the canonical Core lineage, columns are identifiers with an explicit unit, and the byte
bound is charged cell by cell.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import inspect
import itertools
import json
import tracemalloc
import uuid
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
import pytz
from omnivia_core_runtime.analysis import result_artifact as module
from omnivia_core_runtime.analysis.result_artifact import (
    LOGICAL_BOOLEAN,
    LOGICAL_BYTES,
    LOGICAL_DATE,
    LOGICAL_DECIMAL,
    LOGICAL_FLOAT,
    LOGICAL_INTEGER,
    LOGICAL_STRING,
    LOGICAL_TIMESTAMPTZ,
    MAX_RESULT_BYTES_CEILING,
    MAX_RESULT_COLUMNS,
    MAX_RESULT_ROWS_CEILING,
    REFUSE_ANALYSIS_RESULT_ARTIFACT,
    AnalysisExecutionEcho,
    AnalysisResultArtifactCandidate,
    AnalysisResultArtifactRefused,
    AnalysisResultColumn,
    encode_analysis_result_artifact,
    validate_analysis_result_artifact_candidate,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64

ECHO = AnalysisExecutionEcho(
    workspace_id="ws-1",
    run_id="run-1",
    run_step_id="step.1",
    attempt_id="attempt:1",
    plan_digest=DIGEST_A,
    parameters_digest=DIGEST_B,
    final_sql_digest=DIGEST_C,
    input_vector_digest=DIGEST_D,
)

BIG = 1_000_000


def col(
    name: str,
    kind: str,
    *,
    nullable: bool = False,
    precision: int | None = None,
    scale: int | None = None,
    unit: str | None = None,
) -> AnalysisResultColumn:
    return AnalysisResultColumn(name, kind, nullable, precision, scale, unit)


def encode(
    schema: Any,
    rows: Any,
    *,
    echo: Any = ECHO,
    max_rows: Any = 100,
    max_bytes: Any = BIG,
) -> AnalysisResultArtifactCandidate:
    return encode_analysis_result_artifact(
        schema, rows, echo=echo, max_rows=max_rows, max_bytes=max_bytes
    )


def refused(schema: Any, rows: Any, **kwargs: Any) -> AnalysisResultArtifactRefused:
    with pytest.raises(AnalysisResultArtifactRefused) as caught:
        encode(schema, rows, **kwargs)
    return caught.value


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def document(candidate: AnalysisResultArtifactCandidate) -> dict[str, Any]:
    loaded = json.loads(candidate.artifact_bytes)
    assert isinstance(loaded, dict)
    return loaded


def decode_cell(column: AnalysisResultColumn, cell: Any) -> Any:
    """The consumer's reading of one cell: only the schema says what the text means."""
    if cell is None:
        return None
    if column.logical_type == LOGICAL_BOOLEAN:
        assert type(cell) is bool
        return cell
    assert type(cell) is str
    if column.logical_type == LOGICAL_INTEGER:
        return int(cell)
    if column.logical_type == LOGICAL_FLOAT:
        return float(cell)
    if column.logical_type == LOGICAL_DECIMAL:
        return Decimal(cell)
    if column.logical_type == LOGICAL_STRING:
        return cell
    if column.logical_type == LOGICAL_BYTES:
        return base64.b64decode(cell, validate=True)
    if column.logical_type == LOGICAL_DATE:
        return date.fromisoformat(cell)
    assert column.logical_type == LOGICAL_TIMESTAMPTZ
    assert cell.endswith("Z")
    return datetime.fromisoformat(cell[:-1]).replace(tzinfo=UTC)


class StrSub(str):
    pass


class IntSub(int):
    pass


class FloatSub(float):
    pass


class DecimalSub(Decimal):
    pass


class BytesSub(bytes):
    pass


class DateSub(date):
    pass


class DatetimeSub(datetime):
    pass


class ListSub(list):  # type: ignore[type-arg]
    pass


class TupleSub(tuple):  # type: ignore[type-arg]
    pass


class Hostile:
    """Raises from every protocol an encoder might touch and quotes a secret in repr."""

    def __repr__(self) -> str:
        raise RuntimeError("SECRET-REPR")

    def __str__(self) -> str:
        raise RuntimeError("SECRET-STR")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("SECRET-EQ")

    def __hash__(self) -> int:
        raise RuntimeError("SECRET-HASH")

    def __iter__(self) -> Any:
        raise RuntimeError("SECRET-ITER")

    def __len__(self) -> int:
        raise RuntimeError("SECRET-LEN")


# ---------------------------------------------------------------------------
# Determinism and exact digests
# ---------------------------------------------------------------------------


def test_exact_bytes_and_digests_are_pinned() -> None:
    schema = (col("n", LOGICAL_INTEGER), col("s", LOGICAL_STRING, nullable=True))
    candidate = encode(schema, [(1, "a"), (2, None)])

    schema_bytes = (
        b'{"format":"analysis-result-schema/1","schema":{"columns":['
        b'{"logical_type":"integer","name":"n","nullable":false,"precision":null,"scale":null,"unit":null},'
        b'{"logical_type":"string","name":"s","nullable":true,"precision":null,"scale":null,"unit":null}]}}'
    )
    expected_schema_digest = sha(schema_bytes)
    echo_bytes = (
        b'{"attempt_id":"attempt:1","final_sql_digest":"' + DIGEST_C.encode() + b'",'
        b'"input_vector_digest":"' + DIGEST_D.encode() + b'",'
        b'"parameters_digest":"' + DIGEST_B.encode() + b'",'
        b'"plan_digest":"' + DIGEST_A.encode() + b'",'
        b'"run_id":"run-1","run_step_id":"step.1","workspace_id":"ws-1"}'
    )
    artifact_bytes = (
        b'{"echo":' + echo_bytes + b',"format":"analysis-result-artifact/1",'
        b'"row_count":2,"rows":[["1","a"],["2",null]],'
        b'"schema":{"columns":['
        b'{"logical_type":"integer","name":"n","nullable":false,"precision":null,"scale":null,"unit":null},'
        b'{"logical_type":"string","name":"s","nullable":true,"precision":null,"scale":null,"unit":null}]},'
        b'"schema_digest":"' + expected_schema_digest.encode() + b'"}'
    )
    assert candidate.artifact_bytes == artifact_bytes
    assert candidate.schema_digest == expected_schema_digest
    assert candidate.artifact_digest == sha(artifact_bytes)
    assert candidate.row_count == 2
    assert candidate.byte_count == len(artifact_bytes)
    assert candidate.truncated is False
    assert candidate.schema == schema
    assert candidate.echo == ECHO


def test_repeat_encoding_is_deterministic_and_input_form_independent() -> None:
    schema = (col("n", LOGICAL_INTEGER), col("t", LOGICAL_STRING))
    first = encode(schema, [(1, "x"), (2, "y")])
    again = encode(schema, [(1, "x"), (2, "y")])
    as_tuple = encode(schema, ((1, "x"), (2, "y")))
    as_lists = encode(schema, [[1, "x"], [2, "y"]])
    assert first == again == as_tuple == as_lists
    assert first.artifact_bytes == again.artifact_bytes == as_lists.artifact_bytes


def test_schema_digest_binds_every_schema_field() -> None:
    base = (col("a", LOGICAL_INTEGER),)
    digests = {
        encode(base, []).schema_digest,
        encode((col("b", LOGICAL_INTEGER),), []).schema_digest,
        encode((col("a", LOGICAL_STRING),), []).schema_digest,
        encode((col("a", LOGICAL_INTEGER, nullable=True),), []).schema_digest,
        encode((col("a", LOGICAL_DECIMAL, precision=10, scale=2),), []).schema_digest,
        encode((col("a", LOGICAL_DECIMAL, precision=10, scale=3),), []).schema_digest,
        encode((col("a", LOGICAL_DECIMAL, precision=11, scale=2),), []).schema_digest,
        encode(base + (col("c", LOGICAL_INTEGER),), []).schema_digest,
        encode((col("a", LOGICAL_INTEGER, unit="count:row"),), []).schema_digest,
        encode((col("a", LOGICAL_INTEGER, unit="count:other"),), []).schema_digest,
    }
    assert len(digests) == 10
    # Column order is part of the schema.
    two = (col("a", LOGICAL_INTEGER), col("b", LOGICAL_STRING))
    assert (
        encode(two, []).schema_digest != encode(tuple(reversed(two)), []).schema_digest
    )


def test_artifact_digest_binds_rows_echo_and_schema() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    base = encode(schema, [(1,)])
    other_row = encode(schema, [(2,)])
    other_schema = encode((col("z", LOGICAL_INTEGER),), [(1,)])
    other_unit = encode((col("a", LOGICAL_INTEGER, unit="count:row"),), [(1,)])
    for other in (other_row, other_schema, other_unit):
        assert other.artifact_digest != base.artifact_digest
    for field in (
        "workspace_id",
        "run_id",
        "run_step_id",
        "attempt_id",
        "plan_digest",
        "parameters_digest",
        "final_sql_digest",
        "input_vector_digest",
    ):
        value = "other-1" if field.endswith("_id") else "sha256:" + "0" * 64
        changed = dataclasses.replace(ECHO, **{field: value})
        assert (
            encode(schema, [(1,)], echo=changed).artifact_digest != base.artifact_digest
        )
    # The row order is part of the artifact.
    assert (
        encode(schema, [(1,), (2,)]).artifact_digest
        != encode(schema, [(2,), (1,)]).artifact_digest
    )


# ---------------------------------------------------------------------------
# Type fidelity
# ---------------------------------------------------------------------------

AWARE_PLUS = datetime(
    2024, 5, 6, 7, 8, 9, 123456, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
AWARE_UTC = datetime(2024, 5, 6, 1, 38, 9, 123456, tzinfo=UTC)

SUPPORTED: list[tuple[AnalysisResultColumn, Any]] = [
    (col("b", LOGICAL_BOOLEAN), True),
    (col("b", LOGICAL_BOOLEAN), False),
    (col("i", LOGICAL_INTEGER), 0),
    (col("i", LOGICAL_INTEGER), -1),
    (col("i", LOGICAL_INTEGER), 2**127 - 1),
    (col("i", LOGICAL_INTEGER), -(2**127)),
    (col("i", LOGICAL_INTEGER), 2**53 + 1),
    (col("f", LOGICAL_FLOAT), 0.1),
    (col("f", LOGICAL_FLOAT), 1e300),
    (col("f", LOGICAL_FLOAT), 5e-324),
    (col("f", LOGICAL_FLOAT), -2.5),
    (col("d", LOGICAL_DECIMAL, precision=18, scale=3), Decimal("1234.500")),
    (col("d", LOGICAL_DECIMAL, precision=5, scale=0), Decimal(12345)),
    (col("d", LOGICAL_DECIMAL, precision=38, scale=38), Decimal("0." + "1" * 38)),
    (col("d", LOGICAL_DECIMAL, precision=3, scale=2), Decimal("-0.00")),
    (col("s", LOGICAL_STRING), ""),
    (col("s", LOGICAL_STRING), "héllo ☃ \U0001f600 \u0000 \n"),
    (col("y", LOGICAL_BYTES), b""),
    (col("y", LOGICAL_BYTES), b"\x00\xff\x10 binary"),
    (col("t", LOGICAL_DATE), date(2024, 2, 29)),
    (col("t", LOGICAL_DATE), date(1, 1, 1)),
    (col("t", LOGICAL_DATE), date(9999, 12, 31)),
    (col("z", LOGICAL_TIMESTAMPTZ), AWARE_PLUS),
    (col("z", LOGICAL_TIMESTAMPTZ), AWARE_UTC),
    (col("z", LOGICAL_TIMESTAMPTZ), datetime(2024, 1, 1, tzinfo=UTC)),
]


@pytest.mark.parametrize(("column", "value"), SUPPORTED)
def test_supported_scalars_round_trip_with_exact_type(
    column: AnalysisResultColumn, value: Any
) -> None:
    candidate = encode((column,), [(value,)])
    (cell,) = document(candidate)["rows"][0]
    restored = decode_cell(column, cell)
    if column.logical_type == LOGICAL_TIMESTAMPTZ:
        assert restored == value  # the same instant
        assert restored.utcoffset() == timedelta(0)
        assert restored.microsecond == value.microsecond
    else:
        assert type(restored) is type(value)
        assert restored == value
    if column.logical_type == LOGICAL_DECIMAL:
        assert str(restored) == str(value)  # scale and sign of zero survive
    if column.logical_type == LOGICAL_FLOAT:
        assert restored.hex() == value.hex()


def test_negative_zero_float_survives() -> None:
    column = col("f", LOGICAL_FLOAT)
    (cell,) = document(encode((column,), [(-0.0,)]))["rows"][0]
    assert cell == "-0.0"
    assert str(decode_cell(column, cell)) == "-0.0"


def test_every_logical_type_is_covered_by_a_supported_case() -> None:
    covered = {column.logical_type for column, _ in SUPPORTED}
    assert covered == set(module._LOGICAL_TYPES)


def test_nulls_only_in_nullable_columns() -> None:
    for kind in module._LOGICAL_TYPES:
        decimal = {"precision": 5, "scale": 2} if kind == LOGICAL_DECIMAL else {}
        nullable = col("c", kind, nullable=True, **decimal)
        required = col("c", kind, **decimal)
        assert document(encode((nullable,), [(None,)]))["rows"] == [[None]]
        refused((required,), [(None,)])


def test_timestamptz_is_normalised_to_one_utc_instant() -> None:
    column = col("z", LOGICAL_TIMESTAMPTZ)
    a = encode((column,), [(AWARE_PLUS,)])
    b = encode((column,), [(AWARE_UTC,)])
    assert (
        document(a)["rows"] == document(b)["rows"] == [["2024-05-06T01:38:09.123456Z"]]
    )
    midnight = datetime(2024, 1, 1, tzinfo=UTC)
    assert document(encode((column,), [(midnight,)]))["rows"] == [
        ["2024-01-01T00:00:00.000000Z"]
    ]


# ---------------------------------------------------------------------------
# Wrong value / type combinations
# ---------------------------------------------------------------------------

WRONG: list[tuple[AnalysisResultColumn, Any]] = [
    (col("b", LOGICAL_BOOLEAN), 1),
    (col("b", LOGICAL_BOOLEAN), 0),
    (col("b", LOGICAL_BOOLEAN), "true"),
    (col("i", LOGICAL_INTEGER), True),
    (col("i", LOGICAL_INTEGER), False),
    (col("i", LOGICAL_INTEGER), 1.0),
    (col("i", LOGICAL_INTEGER), "1"),
    (col("i", LOGICAL_INTEGER), Decimal(1)),
    (col("i", LOGICAL_INTEGER), 2**127),
    (col("i", LOGICAL_INTEGER), -(2**127) - 1),
    (col("f", LOGICAL_FLOAT), 1),
    (col("f", LOGICAL_FLOAT), True),
    (col("f", LOGICAL_FLOAT), Decimal("1.5")),
    (col("f", LOGICAL_FLOAT), "1.5"),
    (col("d", LOGICAL_DECIMAL, precision=5, scale=2), 1),
    (col("d", LOGICAL_DECIMAL, precision=5, scale=2), 1.5),
    (col("d", LOGICAL_DECIMAL, precision=5, scale=2), "1.50"),
    (col("d", LOGICAL_DECIMAL, precision=5, scale=2), True),
    (col("s", LOGICAL_STRING), b"bytes"),
    (col("s", LOGICAL_STRING), 1),
    (col("s", LOGICAL_STRING), True),
    (col("y", LOGICAL_BYTES), "text"),
    (col("y", LOGICAL_BYTES), bytearray(b"x")),
    (col("y", LOGICAL_BYTES), memoryview(b"x")),
    (col("y", LOGICAL_BYTES), [1, 2]),
    (col("t", LOGICAL_DATE), datetime(2024, 1, 1, tzinfo=UTC)),
    (col("t", LOGICAL_DATE), "2024-01-01"),
    (col("z", LOGICAL_TIMESTAMPTZ), date(2024, 1, 1)),
    (col("z", LOGICAL_TIMESTAMPTZ), "2024-01-01T00:00:00Z"),
    (col("z", LOGICAL_TIMESTAMPTZ), 1_700_000_000),
]


@pytest.mark.parametrize(("column", "value"), WRONG)
def test_incompatible_value_for_logical_type_refuses(
    column: AnalysisResultColumn, value: Any
) -> None:
    refused((column,), [(value,)])


@pytest.mark.parametrize(
    "value",
    [
        uuid.UUID(int=1),
        object(),
        Hostile(),
        (1, 2),
        [1],
        {"a": 1},
        {1},
        frozenset({1}),
        1j,
        complex(1, 2),
        range(3),
        Path("/tmp/workspace/secret"),
        lambda: 1,
    ],
)
@pytest.mark.parametrize("kind", module._LOGICAL_TYPES)
def test_unsupported_objects_refuse_under_every_logical_type(
    kind: str, value: Any
) -> None:
    decimal = {"precision": 5, "scale": 2} if kind == LOGICAL_DECIMAL else {}
    refused((col("c", kind, **decimal),), [(value,)])


def test_bool_int_confusion_is_closed_both_ways() -> None:
    refused((col("i", LOGICAL_INTEGER),), [(True,)])
    refused((col("b", LOGICAL_BOOLEAN),), [(1,)])
    ok = encode((col("i", LOGICAL_INTEGER), col("b", LOGICAL_BOOLEAN)), [(1, True)])
    assert document(ok)["rows"] == [["1", True]]


def test_no_loose_hex_or_isoformat_coercion_in_source() -> None:
    source = inspect.getsource(module)
    assert "hasattr(" not in source
    assert "getattr(" not in source
    # float.hex and UUID.hex are the cases a duck-typed `.hex` check would swallow.
    refused((col("f", LOGICAL_FLOAT),), [(uuid.UUID(int=5),)])
    refused((col("s", LOGICAL_STRING),), [(uuid.UUID(int=5),)])
    refused((col("y", LOGICAL_BYTES),), [(uuid.UUID(int=5),)])


# ---------------------------------------------------------------------------
# Floats, decimals, strings, datetimes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), -float("nan")]
)
def test_non_finite_floats_refuse(value: float) -> None:
    refused((col("f", LOGICAL_FLOAT),), [(value,)])
    refused((col("f", LOGICAL_FLOAT, nullable=True),), [(value,)])


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1.5"),  # scale 1, schema says 2
        Decimal("1.500"),  # scale 3, schema says 2
        Decimal("1E+2"),  # positive exponent
        Decimal("12345.67"),  # six digits against precision five
        Decimal(123),  # exponent 0 against scale 2
    ],
)
def test_decimal_must_match_declared_precision_and_scale(value: Decimal) -> None:
    refused((col("d", LOGICAL_DECIMAL, precision=5, scale=2),), [(value,)])


def test_decimal_representation_is_plain_and_scale_preserving() -> None:
    column = col("d", LOGICAL_DECIMAL, precision=10, scale=7)
    (cell,) = document(encode((column,), [(Decimal("1E-7"),)]))["rows"][0]
    assert cell == "0.0000001"
    assert decode_cell(column, cell) == Decimal("1E-7")
    scaled = col("d", LOGICAL_DECIMAL, precision=10, scale=3)
    (cell,) = document(encode((scaled,), [(Decimal("12.340"),)]))["rows"][0]
    assert cell == "12.340"
    assert str(decode_cell(scaled, cell)) == "12.340"


def test_strings_must_be_unicode_scalar_sequences() -> None:
    refused((col("s", LOGICAL_STRING),), [("\ud800",)])
    refused((col("s", LOGICAL_STRING),), [("ok\udfffbad",)])


def _zone_info(key: str) -> ZoneInfo:
    # An exact stdlib ZoneInfo built from pytz's bundled data, so the test needs no system
    # tz database (or tzdata package) on any platform.
    with pytz.open_resource(key) as handle:
        return ZoneInfo.from_file(handle, key=key)


def test_naive_and_foreign_tzinfo_datetimes_refuse() -> None:
    column = col("z", LOGICAL_TIMESTAMPTZ)
    refused((column,), [(datetime(2024, 1, 1),)])  # noqa: DTZ001
    refused((column,), [(datetime(2024, 1, 1, 0, 0, 0, 1),)])  # noqa: DTZ001
    calls: list[str] = []

    class NoOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            calls.append("utcoffset")
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            calls.append("dst")
            return None

    class RaisingOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            calls.append("utcoffset")
            raise RuntimeError("SECRET-TZ")

        def dst(self, dt: datetime | None) -> timedelta | None:
            calls.append("dst")
            return None

    class FixedLookalike(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            calls.append("utcoffset")
            return timedelta(0)

        def dst(self, dt: datetime | None) -> timedelta | None:
            calls.append("dst")
            return None

    refused((column,), [(datetime(2024, 1, 1, tzinfo=NoOffset()),)])
    error = refused((column,), [(datetime(2024, 1, 1, tzinfo=RaisingOffset()),)])
    assert "SECRET-TZ" not in repr(error)
    refused((column,), [(datetime(2024, 1, 1, tzinfo=FixedLookalike()),)])
    assert calls == []  # no foreign tzinfo method is ever invoked


def test_stateful_tzinfo_cannot_make_repeat_encoding_vary() -> None:
    column = col("z", LOGICAL_TIMESTAMPTZ)
    calls = 0

    class Stateful(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            nonlocal calls
            calls += 1
            return timedelta(hours=calls)

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

    value = datetime(2024, 1, 1, tzinfo=Stateful())
    for _ in range(3):
        refused((column,), [(value,)])
    assert calls == 0
    # The admitted fixed-offset type is stateless, so repeat encoding is identical.
    fixed = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2)))
    first = encode((column,), [(fixed,)])
    assert all(encode((column,), [(fixed,)]) == first for _ in range(3))
    assert document(first)["rows"] == [["2023-12-31T22:00:00.000000Z"]]


def test_datetime_at_the_representable_edge_refuses_instead_of_wrapping() -> None:
    column = col("z", LOGICAL_TIMESTAMPTZ)
    edge = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    refused((column,), [(edge,)])
    low = datetime(1, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    refused((column,), [(low,)])


# ---------------------------------------------------------------------------
# Timezone providers: built-in timezone, exact ZoneInfo, the captured pytz UTC singleton
# ---------------------------------------------------------------------------

TZ_COLUMN = col("z", LOGICAL_TIMESTAMPTZ)


def wall(*fields: int) -> datetime:
    """A naive wall-clock time, for pytz `localize`."""
    return datetime(*fields)  # noqa: DTZ001


def encoded_instant(value: Any) -> str:
    rows = document(encode((TZ_COLUMN,), [(value,)]))["rows"]
    assert len(rows) == 1 and len(rows[0]) == 1
    text = rows[0][0]
    assert isinstance(text, str)
    return text


def duck_value(
    timezone_name: str, sql: str = "SELECT ?::TIMESTAMPTZ", *params: Any
) -> Any:
    connection = duckdb.connect()
    try:
        connection.execute("SET TimeZone = ?", [timezone_name])
        row = connection.execute(sql, list(params)).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def utc_value(sql: str = "SELECT ?::TIMESTAMPTZ", *params: Any) -> Any:
    """A real value from a connection explicitly configured with `SET TimeZone='UTC'`."""
    connection = duckdb.connect()
    try:
        connection.execute("SET TimeZone='UTC'")
        row = connection.execute(sql, list(params)).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def test_duckdb_utc_connection_values_are_the_captured_singleton_and_encode() -> None:
    assert duckdb.__version__ == "1.5.5"
    value = utc_value("SELECT ?::TIMESTAMPTZ", "2024-03-04 05:06:07.123456+00")
    assert type(value) is datetime
    assert value.tzinfo is module._PYTZ_UTC
    first = encoded_instant(value)
    assert first == "2024-03-04T05:06:07.123456Z"
    assert all(encoded_instant(value) == first for _ in range(3))


def test_duckdb_utc_connection_keeps_repeated_dst_wall_times_distinct() -> None:
    edt = utc_value("SELECT ?::TIMESTAMPTZ", "2024-11-03 01:30:00-04")
    est = utc_value("SELECT ?::TIMESTAMPTZ", "2024-11-03 01:30:00-05")
    assert edt.tzinfo is module._PYTZ_UTC and est.tzinfo is module._PYTZ_UTC
    assert encoded_instant(edt) == "2024-11-03T05:30:00.000000Z"
    assert encoded_instant(est) == "2024-11-03T06:30:00.000000Z"


def test_duckdb_utc_now_is_fetched_once_and_encodes_the_same_instant_twice() -> None:
    value = utc_value("SELECT now()")
    assert value.tzinfo is module._PYTZ_UTC
    first = encoded_instant(value)
    assert encoded_instant(value) == first
    expected = value.replace(tzinfo=None).isoformat(timespec="microseconds")
    assert first == expected + "Z"


@pytest.mark.parametrize(
    "zone", ["America/New_York", "Asia/Kolkata", "Etc/GMT+5", "Australia/Lord_Howe"]
)
def test_duckdb_non_utc_connection_values_are_refused(zone: str) -> None:
    value = duck_value(zone, "SELECT ?::TIMESTAMPTZ", "2024-03-04 05:06:07+00")
    assert type(value) is datetime
    assert value.tzinfo is not module._PYTZ_UTC
    for _ in range(2):
        error = refused((TZ_COLUMN,), [(value,)])
        assert error.__cause__ is None and error.__context__ is None


def test_exact_zoneinfo_fold_selects_the_instant() -> None:
    zone = _zone_info("America/New_York")
    assert type(zone) is ZoneInfo
    first = datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1)
    assert encoded_instant(first) == "2024-11-03T05:30:00.000000Z"
    assert encoded_instant(second) == "2024-11-03T06:30:00.000000Z"
    winter = datetime(2024, 1, 1, 12, tzinfo=zone)
    assert encoded_instant(winter) == "2024-01-01T17:00:00.000000Z"


def test_builtin_timezone_behaviour_and_golden_bytes_are_unchanged() -> None:
    assert encoded_instant(AWARE_PLUS) == "2024-05-06T01:38:09.123456Z"
    assert encoded_instant(AWARE_UTC) == "2024-05-06T01:38:09.123456Z"
    assert (
        encoded_instant(datetime(2024, 1, 1, tzinfo=UTC))
        == "2024-01-01T00:00:00.000000Z"
    )
    assert encoded_instant(
        datetime(2024, 1, 1, tzinfo=timezone(-timedelta(hours=3)))
    ) == ("2024-01-01T03:00:00.000000Z")


def test_pytz_named_localized_and_static_zones_are_refused() -> None:
    new_york = pytz.timezone("America/New_York")
    values = [
        new_york.localize(wall(2024, 7, 1, 12)),
        new_york.localize(wall(2024, 1, 1, 12)),
        new_york.localize(wall(2024, 11, 3, 1, 30), is_dst=True),
        new_york.localize(wall(2024, 11, 3, 1, 30), is_dst=False),
        datetime(2024, 1, 1, tzinfo=new_york),
        datetime(2024, 1, 1, tzinfo=pytz.timezone("Etc/GMT+5")),
        pytz.timezone("Asia/Kolkata").localize(wall(2024, 1, 1, 12)),
        datetime(2024, 1, 1, tzinfo=pytz.FixedOffset(330)),
    ]
    for value in values:
        error = refused((TZ_COLUMN,), [(value,)])
        assert error.__cause__ is None and error.__context__ is None


def test_pytz_utc_singleton_encodes_with_a_constant_zero_offset() -> None:
    assert module._PYTZ_UTC is pytz.UTC
    value = datetime(2024, 1, 1, 12, tzinfo=module._PYTZ_UTC)
    assert encoded_instant(value) == "2024-01-01T12:00:00.000000Z"
    assert module._timestamptz_offset(value) == timedelta(0)


def test_pytz_utc_singleton_is_admitted_without_reading_or_calling_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = datetime(2024, 1, 1, 12, tzinfo=module._PYTZ_UTC)
    kind = type(module._PYTZ_UTC)
    reads: list[str] = []

    def counting_getattribute(self: Any, name: str) -> Any:
        reads.append(name)
        return object.__getattribute__(self, name)

    def counting(name: str) -> Any:
        def method(self: Any, *args: Any, **kwargs: Any) -> Any:
            reads.append(name)
            raise AssertionError(name)

        return method

    monkeypatch.setattr(kind, "__getattribute__", counting_getattribute)
    for name in ("utcoffset", "dst", "tzname", "fromutc"):
        monkeypatch.setattr(kind, name, counting(name))
    assert encoded_instant(value) == "2024-01-01T12:00:00.000000Z"
    assert reads == []


def test_replacing_pytz_utc_after_import_cannot_replace_the_captured_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module._PYTZ_UTC
    kind = type(original)
    forged = kind.__new__(kind)
    forged.__dict__["_utcoffset"] = timedelta(hours=9)
    monkeypatch.setattr(pytz, "UTC", forged)
    monkeypatch.setattr(pytz, "utc", forged)
    assert pytz.UTC is forged
    assert module._PYTZ_UTC is original
    assert (
        encoded_instant(datetime(2024, 1, 1, 12, tzinfo=original))
        == "2024-01-01T12:00:00.000000Z"
    )
    for _ in range(2):
        error = refused((TZ_COLUMN,), [(datetime(2024, 1, 1, 12, tzinfo=forged),)])
        assert error.__cause__ is None and error.__context__ is None


def test_mutating_the_captured_singleton_cannot_change_its_encoded_bytes() -> None:
    singleton = module._PYTZ_UTC
    value = datetime(2024, 1, 1, 12, tzinfo=singleton)
    expected = encode((TZ_COLUMN,), [(value,)])
    before = dict(vars(singleton))
    try:
        for name, hostile in (
            ("_utcoffset", timedelta(hours=9)),
            ("zone", "America/New_York"),
            ("_tzname", "EST"),
            ("anything", object()),
        ):
            vars(singleton)[name] = hostile
            assert encode((TZ_COLUMN,), [(value,)]) == expected
        assert encoded_instant(value) == "2024-01-01T12:00:00.000000Z"
    finally:
        vars(singleton).clear()
        vars(singleton).update(before)
    assert vars(singleton) == before
    assert all(vars(singleton)[k] is v for k, v in before.items())
    assert encode((TZ_COLUMN,), [(value,)]) == expected


def test_poisoned_pytz_registries_and_offsets_cannot_admit_or_change_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_york = pytz.timezone("America/New_York")
    summer = new_york.localize(wall(2024, 7, 1, 12))
    period = summer.tzinfo
    assert period is not None
    kind = type(period)
    utc_instant = datetime(2024, 1, 1, 12, tzinfo=module._PYTZ_UTC)
    utc_bytes = encode((TZ_COLUMN,), [(utc_instant,)])

    def assert_unchanged() -> None:
        assert encode((TZ_COLUMN,), [(utc_instant,)]) == utc_bytes
        refused((TZ_COLUMN,), [(summer,)])

    # A replaced or hostile zone cache.
    class SpyDict(dict):  # type: ignore[type-arg]
        def get(self, *args: Any) -> Any:
            raise AssertionError("registry read")

        def __getitem__(self, key: Any) -> Any:
            raise AssertionError("registry read")

    for replacement in (SpyDict(pytz._tzinfo_cache), {}, None, [], "cache"):
        monkeypatch.setattr(pytz, "_tzinfo_cache", replacement)
        assert_unchanged()
    monkeypatch.undo()

    # A forged exact-class period inserted into the genuine canonical `_tzinfos`.
    forged = kind.__new__(kind)
    forged.__dict__.update({**period.__dict__, "_utcoffset": timedelta(hours=9)})
    periods = new_york._tzinfos
    key = ("forged", "period")
    assert key not in periods
    periods[key] = forged
    try:
        refused((TZ_COLUMN,), [(datetime(2024, 7, 1, 12, tzinfo=forged),)])
        assert_unchanged()
    finally:
        del periods[key]
    assert key not in periods

    # A mutated `_utcoffset` on a genuine period.
    had = "_utcoffset" in vars(period)
    before = vars(period).get("_utcoffset")
    vars(period)["_utcoffset"] = timedelta(0)
    try:
        refused((TZ_COLUMN,), [(datetime(2024, 7, 1, 12, tzinfo=period),)])
        assert_unchanged()
    finally:
        if had:
            vars(period)["_utcoffset"] = before
        else:
            del vars(period)["_utcoffset"]
    assert vars(period).get("_utcoffset") is before

    # Forged exact pytz classes, registered under nothing, claiming UTC or a named zone.
    for original in (module._PYTZ_UTC, period):
        fake = type(original).__new__(type(original))
        fake.__dict__.update({"_utcoffset": timedelta(0), "zone": "UTC"})
        refused((TZ_COLUMN,), [(datetime(2024, 1, 1, 12, tzinfo=fake),)])
    assert_unchanged()


class CallCounter:
    calls = 0


def _counting_methods() -> dict[str, Any]:
    def utcoffset(self: Any, dt: Any = None) -> timedelta:
        CallCounter.calls += 1
        return timedelta(hours=9)

    def dst(self: Any, dt: Any = None) -> timedelta:
        CallCounter.calls += 1
        return timedelta(0)

    def tzname(self: Any, dt: Any = None) -> str:
        CallCounter.calls += 1
        return "X"

    def fromutc(self: Any, dt: Any) -> Any:
        CallCounter.calls += 1
        return dt

    return {"utcoffset": utcoffset, "dst": dst, "tzname": tzname, "fromutc": fromutc}


def _untrusted_zones() -> list[tuple[str, tzinfo]]:
    new_york = pytz.timezone("America/New_York")
    period = new_york.localize(wall(2024, 7, 1, 12)).tzinfo
    assert period is not None
    methods = _counting_methods()

    class CustomZone(tzinfo):
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]
        fromutc = methods["fromutc"]

    class ZoneInfoSub(ZoneInfo):
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]

    class PytzSub(type(period)):  # type: ignore[misc]
        zone = "America/New_York"
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]
        fromutc = methods["fromutc"]

    class PytzUtcSub(type(pytz.UTC)):  # type: ignore[misc]
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]
        fromutc = methods["fromutc"]

    class PytzLookalike(tzinfo):
        zone = "America/New_York"
        _tzinfos: dict[Any, Any] = {}  # noqa: RUF012
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]
        fromutc = methods["fromutc"]

    class Meta(type):
        def __getattribute__(cls, name: str) -> Any:
            CallCounter.calls += 1
            return super().__getattribute__(name)

    class MetaZone(tzinfo, metaclass=Meta):
        zone = "America/New_York"
        utcoffset = methods["utcoffset"]
        dst = methods["dst"]
        tzname = methods["tzname"]
        fromutc = methods["fromutc"]

    with pytz.open_resource("America/New_York") as handle:
        zoneinfo_sub = ZoneInfoSub.from_file(handle, key="America/New_York")
    forged_exact = type(period).__new__(
        type(period)
    )  # a real pytz class, never registered
    forged_exact.__dict__.update({"_utcoffset": timedelta(hours=9), "_tzinfos": {}})
    forged_utc = type(pytz.UTC).__new__(type(pytz.UTC))
    forged_period = type(period).__new__(type(period))
    forged_period.__dict__.update(
        period.__dict__
    )  # an equal-looking copy, not the library's
    forged_period.__dict__["_utcoffset"] = timedelta(hours=9)
    return [
        ("custom", CustomZone()),
        ("zoneinfo-subclass", zoneinfo_sub),
        ("pytz-subclass", PytzSub.__new__(PytzSub)),
        ("pytz-utc-subclass", PytzUtcSub.__new__(PytzUtcSub)),
        ("pytz-lookalike", PytzLookalike()),
        ("metaclass", MetaZone()),
        ("forged-exact-class", forged_exact),
        ("forged-utc-class", forged_utc),
        ("forged-period-copy", forged_period),
    ]


@pytest.mark.parametrize("index", range(9))
def test_untrusted_tzinfo_is_refused_without_any_callback(index: int) -> None:
    label, zone = _untrusted_zones()[index]
    CallCounter.calls = 0
    value = datetime(2024, 7, 1, 12, tzinfo=zone)
    for _ in range(2):
        error = refused((TZ_COLUMN,), [(value,)])
        assert error.__cause__ is None and error.__context__ is None, label
    assert CallCounter.calls == 0, label


def test_untrusted_zone_matrix_is_complete() -> None:
    assert len(_untrusted_zones()) == 9


def test_pytz_provenance_is_by_identity_of_the_captured_singleton() -> None:
    period = pytz.timezone("America/New_York").localize(wall(2024, 7, 1, 12)).tzinfo
    assert period is not None
    # A genuine period, even a deep copy of it, is not the captured singleton.
    refused((TZ_COLUMN,), [(datetime(2024, 7, 1, 12, tzinfo=period),)])
    refused((TZ_COLUMN,), [(datetime(2024, 7, 1, 12, tzinfo=copy.deepcopy(period)),)])
    # A datetime subclass stays refused even with the captured singleton.
    refused((TZ_COLUMN,), [(DatetimeSub(2024, 7, 1, 12, tzinfo=module._PYTZ_UTC),)])


def test_naive_datetimes_remain_refused() -> None:
    refused((TZ_COLUMN,), [(datetime(2024, 7, 1, 12),)])  # noqa: DTZ001


def test_pytz_is_declared_and_reviewed_at_one_exact_pin() -> None:
    root = Path(__file__).resolve().parents[5]
    manifest = (root / "packages/omnivia-core-runtime/pyproject.toml").read_text(
        encoding="utf-8"
    )
    declared = [
        line.strip().strip(",").strip('"')
        for line in manifest.splitlines()
        if line.strip().startswith('"pytz')
    ]
    assert declared == ["pytz==2026.5"]
    constraints = [
        line.split("#", 1)[0].strip()
        for line in (root / "scripts/mcp-wheelhouse-constraints.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert constraints.count("pytz==2026.5") == 1
    assert pytz.__version__ == "2026.5"


# ---------------------------------------------------------------------------
# Hostile subclasses and containers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        (col("s", LOGICAL_STRING), StrSub("x")),
        (col("i", LOGICAL_INTEGER), IntSub(1)),
        (col("f", LOGICAL_FLOAT), FloatSub(1.5)),
        (col("d", LOGICAL_DECIMAL, precision=5, scale=2), DecimalSub("1.50")),
        (col("y", LOGICAL_BYTES), BytesSub(b"x")),
        (col("t", LOGICAL_DATE), DateSub(2024, 1, 1)),
        (col("z", LOGICAL_TIMESTAMPTZ), DatetimeSub(2024, 1, 1, tzinfo=UTC)),
    ],
)
def test_cell_subclasses_refuse(column: AnalysisResultColumn, value: Any) -> None:
    refused((column,), [(value,)])


def test_container_subclasses_and_non_containers_refuse() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    refused(schema, ListSub([(1,)]))
    refused(schema, [TupleSub((1,))])
    refused(schema, [ListSub([1])])
    refused(TupleSub(schema), [(1,)])
    refused(list(schema), [(1,)])
    refused(schema, ((1,) for _ in range(1)))  # a generator is not a sequence
    refused(schema, {(1,)})
    refused(schema, "1")
    refused(schema, b"1")
    refused(schema, Hostile())
    refused(schema, [Hostile()])
    refused(Hostile(), [(1,)])
    refused(None, [(1,)])
    refused(schema, None)


def test_row_width_must_match_the_schema() -> None:
    schema = (col("a", LOGICAL_INTEGER), col("b", LOGICAL_INTEGER))
    refused(schema, [(1,)])
    refused(schema, [(1, 2, 3)])
    refused(schema, [(1, 2), (3,)])
    refused(schema, [()])


def test_echo_and_column_subclasses_refuse() -> None:
    class EchoSub(AnalysisExecutionEcho):
        pass

    class ColumnSub(AnalysisResultColumn):
        pass

    @dataclasses.dataclass(frozen=True, slots=True)
    class EchoRedecorated(AnalysisExecutionEcho):
        pass

    # A plain subclass cannot be constructed; a re-decorated or forged one still refuses.
    with pytest.raises(AnalysisResultArtifactRefused):
        EchoSub(**dataclasses.asdict(ECHO))
    with pytest.raises(AnalysisResultArtifactRefused):
        ColumnSub("a", LOGICAL_INTEGER, False)
    redecorated = EchoRedecorated(**dataclasses.asdict(ECHO))
    forged_echo = object.__new__(EchoSub)
    for name, value in dataclasses.asdict(ECHO).items():
        object.__setattr__(forged_echo, name, value)
    forged_column = object.__new__(ColumnSub)
    for name, value in dataclasses.asdict(col("a", LOGICAL_INTEGER)).items():
        object.__setattr__(forged_column, name, value)
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=forged_echo)
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=redecorated)
    refused((forged_column,), [(1,)])
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=dataclasses.asdict(ECHO))
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=None)
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=Hostile())
    refused((dataclasses.asdict(col("a", LOGICAL_INTEGER)),), [(1,)])


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def test_duplicate_column_names_refuse_including_case_variants() -> None:
    refused((col("a", LOGICAL_INTEGER), col("a", LOGICAL_STRING)), [])
    refused((col("a", LOGICAL_INTEGER), col("A", LOGICAL_INTEGER)), [])
    refused((col("Col.1", LOGICAL_INTEGER), col("col.1", LOGICAL_INTEGER)), [])
    ok = encode((col("a", LOGICAL_INTEGER), col("a.b", LOGICAL_INTEGER)), [])
    assert ok.row_count == 0


@pytest.mark.parametrize(
    "build",
    [
        lambda: AnalysisResultColumn("", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("x" * 129, LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a\nb", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a\x00b", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a\ud800", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a ", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn(" a", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a b", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a\n", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("-a", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a;b", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a(b)", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a,b", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a'--", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn('"a"', LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a*", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("a/b", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn("é", LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn(StrSub("a"), LOGICAL_INTEGER, False),
        lambda: AnalysisResultColumn(b"a", LOGICAL_INTEGER, False),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", "varchar", False),
        lambda: AnalysisResultColumn("a", StrSub("integer"), False),
        lambda: AnalysisResultColumn("a", "INTEGER", False),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, 1),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, None),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 5, None),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, None, 2),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 0, 0),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 39, 2),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 5, 6),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 5, -1),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, True, 0),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, IntSub(5), 2),
        lambda: AnalysisResultColumn("a", LOGICAL_DECIMAL, False, 5.0, 2),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, 5, 2),
        lambda: AnalysisResultColumn("a", LOGICAL_STRING, False, 5, None),
        lambda: AnalysisResultColumn("a", LOGICAL_STRING, False, None, 0),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, ""),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, " "),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, "a b"),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, "aud "),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, "$"),
        lambda: AnalysisResultColumn(
            "a", LOGICAL_INTEGER, False, None, None, "x" * 129
        ),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, "a\n"),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, "é"),
        lambda: AnalysisResultColumn(
            "a", LOGICAL_INTEGER, False, None, None, StrSub("count:row")
        ),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, b"u"),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, 1),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, True),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn(
            "a", LOGICAL_INTEGER, False, None, None, Hostile()
        ),  # type: ignore[arg-type]
        lambda: AnalysisResultColumn(Hostile(), LOGICAL_INTEGER, False),  # type: ignore[arg-type]
    ],
)
def test_invalid_columns_refuse_at_construction(build: Any) -> None:
    with pytest.raises(AnalysisResultArtifactRefused):
        build()


def test_columns_accept_identifier_names_and_units() -> None:
    for name in ("a", "A1", "col.name", "ns:col", "a-b_c", "0a", "x" * 128):
        assert col(name, LOGICAL_INTEGER).name == name
    for unit in ("currency:AUD", "count:row", "a", "x" * 128):
        assert col("a", LOGICAL_INTEGER, unit=unit).unit == unit
    assert col("a", LOGICAL_INTEGER).unit is None
    assert col("a", LOGICAL_INTEGER, unit=None).unit is None


def test_unit_is_schema_authority_and_preserves_decimal_precision_and_scale() -> None:
    plain = col("amount", LOGICAL_DECIMAL, precision=12, scale=2)
    aud = col("amount", LOGICAL_DECIMAL, precision=12, scale=2, unit="currency:AUD")
    usd = col("amount", LOGICAL_DECIMAL, precision=12, scale=2, unit="currency:USD")
    rows = [(Decimal("1.50"),)]
    a, b, c = (encode((x,), rows) for x in (plain, aud, usd))
    assert len({a.schema_digest, b.schema_digest, c.schema_digest}) == 3
    assert len({a.artifact_digest, b.artifact_digest, c.artifact_digest}) == 3
    (entry,) = document(b)["schema"]["columns"]
    assert entry == {
        "logical_type": "decimal",
        "name": "amount",
        "nullable": False,
        "precision": 12,
        "scale": 2,
        "unit": "currency:AUD",
    }
    assert document(a)["schema"]["columns"][0]["unit"] is None  # explicit, not omitted
    # Only the unit differs, so the unit alone moved both digests.
    assert b.row_count == a.row_count
    assert document(b)["rows"] == document(a)["rows"]


def test_malformed_column_call_shapes_refuse_with_the_fixed_reason() -> None:
    secret = "SECRET_FIELD"
    shapes: list[Any] = [
        lambda: AnalysisResultColumn(),
        lambda: AnalysisResultColumn("a"),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER),
        lambda: AnalysisResultColumn(name="a", logical_type=LOGICAL_INTEGER),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, None, None, None, 1),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, False, **{secret: 1}),
        lambda: AnalysisResultColumn("a", LOGICAL_INTEGER, nullable=False, name="b"),
        lambda: AnalysisResultColumn(
            "a", LOGICAL_INTEGER, False, **{secret: 1, "scale": None}
        ),
        lambda: AnalysisResultColumn(**{StrSub("name"): "a"}),
    ]
    for build in shapes:
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            build()
        assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert secret not in repr(caught.value)
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None


def test_schema_shape_bounds() -> None:
    refused((), [])
    refused((), [()])
    many = tuple(col(f"c{i}", LOGICAL_INTEGER) for i in range(MAX_RESULT_COLUMNS))
    assert encode(many, []).row_count == 0
    refused(many + (col("extra", LOGICAL_INTEGER),), [])
    longest = col("x" * 128, LOGICAL_INTEGER)
    assert encode((longest,), []).row_count == 0


def test_column_mutated_after_construction_is_reproven_at_encode() -> None:
    column = col("a", LOGICAL_INTEGER)
    object.__setattr__(column, "logical_type", "mystery")
    refused((column,), [(1,)])
    other = col("a", LOGICAL_INTEGER)
    object.__setattr__(other, "precision", 3)
    refused((other,), [(1,)])


def test_echo_mutated_after_construction_is_reproven_at_encode() -> None:
    echo = dataclasses.replace(ECHO)
    object.__setattr__(echo, "plan_digest", "sha256:" + "A" * 64)
    refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=echo)
    for field in ("workspace_id", "run_id", "run_step_id", "attempt_id"):
        bad = dataclasses.replace(ECHO)
        object.__setattr__(bad, field, "bad value")
        refused((col("a", LOGICAL_INTEGER),), [(1,)], echo=bad)
    unit_column = col("a", LOGICAL_INTEGER)
    object.__setattr__(unit_column, "unit", "bad unit")
    refused((unit_column,), [(1,)])


# ---------------------------------------------------------------------------
# Execution echo
# ---------------------------------------------------------------------------

BAD_DIGESTS: list[Any] = [
    "",
    "sha256:",
    "sha256:" + "a" * 63,
    "sha256:" + "a" * 65,
    "sha256:" + "A" * 64,
    "sha256:" + "g" * 64,
    "SHA256:" + "a" * 64,
    "sha512:" + "a" * 64,
    "a" * 64,
    " sha256:" + "a" * 64,
    "sha256:" + "a" * 64 + "\n",
    "sha256:" + "a" * 64 + " ",
    StrSub(DIGEST_A),
    DIGEST_A.encode(),
    None,
    1,
    True,
    ["sha256:" + "a" * 64],
]

BAD_IDENTIFIERS: list[Any] = [
    "",
    " ",
    "run 1",
    "run/1",
    "../run",
    "-leading",
    ".leading",
    "x" * 129,
    "run\n",
    "rué",
    StrSub("run-1"),
    b"run-1",
    None,
    1,
    True,
    ("run-1",),
]


@pytest.mark.parametrize(
    "field",
    ["plan_digest", "parameters_digest", "final_sql_digest", "input_vector_digest"],
)
@pytest.mark.parametrize("bad", BAD_DIGESTS)
def test_echo_digest_fields_are_exact_sha256_lowercase(field: str, bad: Any) -> None:
    values = dataclasses.asdict(ECHO)
    values[field] = bad
    with pytest.raises(AnalysisResultArtifactRefused):
        AnalysisExecutionEcho(**values)


LINEAGE_FIELDS = ["workspace_id", "run_id", "run_step_id", "attempt_id"]


@pytest.mark.parametrize("field", LINEAGE_FIELDS)
@pytest.mark.parametrize("bad", BAD_IDENTIFIERS)
def test_echo_lineage_fields_are_canonical_identifiers(field: str, bad: Any) -> None:
    values = dataclasses.asdict(ECHO)
    values[field] = bad
    with pytest.raises(AnalysisResultArtifactRefused):
        AnalysisExecutionEcho(**values)


def test_echo_lineage_accepts_canonical_identifiers_at_their_bounds() -> None:
    for field in LINEAGE_FIELDS:
        for good in ("a", "A0", "ws.1:x-y_z", "x" * 128):
            echo = dataclasses.replace(ECHO, **{field: good})
            assert dataclasses.asdict(echo)[field] == good
            encode((col("a", LOGICAL_INTEGER),), [], echo=echo)


def test_echo_binds_the_canonical_core_lineage_shape() -> None:
    # The same four fields, in the same order, that ExecutionLineage and HostLineage carry.
    from omnivia_core_runtime.execution.profile import ExecutionLineage
    from omnivia_core_runtime.service.worker_adapter import HostLineage

    names = [f.name for f in dataclasses.fields(AnalysisExecutionEcho)][:4]
    assert names == LINEAGE_FIELDS
    assert names == [f.name for f in dataclasses.fields(ExecutionLineage)]
    assert names == [f.name for f in dataclasses.fields(HostLineage)]
    echo_keys = set(
        document(encode((col("a", LOGICAL_INTEGER),), [], echo=ECHO))["echo"]
    )
    assert {"workspace_id", "run_id", "run_step_id", "attempt_id"} <= echo_keys
    assert "step_id" not in echo_keys


def test_echo_lineage_is_deterministic_and_every_field_changes_the_digest() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    base = encode(schema, [(1,)])
    assert encode(schema, [(1,)]).artifact_digest == base.artifact_digest
    seen = {base.artifact_digest}
    for field in LINEAGE_FIELDS:
        changed = dataclasses.replace(ECHO, **{field: "other-1"})
        digest = encode(schema, [(1,)], echo=changed).artifact_digest
        assert digest == encode(schema, [(1,)], echo=changed).artifact_digest
        assert digest not in seen
        seen.add(digest)
        echo_doc = document(encode(schema, [(1,)], echo=changed))["echo"]
        assert echo_doc[field] == "other-1"


def test_echo_has_exactly_the_bound_fields_and_no_extras() -> None:
    assert [f.name for f in dataclasses.fields(AnalysisExecutionEcho)] == [
        "workspace_id",
        "run_id",
        "run_step_id",
        "attempt_id",
        "plan_digest",
        "parameters_digest",
        "final_sql_digest",
        "input_vector_digest",
    ]
    values = dataclasses.asdict(ECHO)
    secret = "SECRET_FIELD"
    shapes: list[Any] = [
        lambda: AnalysisExecutionEcho(**values, **{secret: "x"}),
        lambda: AnalysisExecutionEcho(**{**values, "step_id": "step.1"}),
        lambda: AnalysisExecutionEcho(*values.values(), "extra"),
        lambda: AnalysisExecutionEcho(),
        lambda: AnalysisExecutionEcho(*list(values.values())[:-1]),
        lambda: AnalysisExecutionEcho(
            **{k: v for k, v in values.items() if k != "plan_digest"}
        ),
        lambda: AnalysisExecutionEcho(
            **{k: v for k, v in values.items() if k != "workspace_id"}
        ),
        lambda: AnalysisExecutionEcho(*values.values(), workspace_id="ws-2"),
    ]
    for build in shapes:
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            build()
        assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert secret not in repr(caught.value)
        assert "plan_digest" not in repr(caught.value)
        assert caught.value.__context__ is None
    max_identifier = dataclasses.replace(ECHO, run_id="x" * 128)
    assert encode((col("a", LOGICAL_INTEGER),), [], echo=max_identifier).row_count == 0


# ---------------------------------------------------------------------------
# Bounds: rows, bytes, truncation
# ---------------------------------------------------------------------------


def test_row_bound_is_exact_and_never_truncates() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    rows = [(i,) for i in range(10)]
    ok = encode(schema, rows, max_rows=10)
    assert ok.row_count == 10
    assert ok.truncated is False
    assert len(document(ok)["rows"]) == 10
    refused(schema, rows, max_rows=9)
    refused(schema, rows, max_rows=0)
    assert encode(schema, [], max_rows=0).row_count == 0


def test_byte_bound_is_exact_and_never_truncates() -> None:
    schema = (col("s", LOGICAL_STRING),)
    rows = [("x" * 50,), ("y" * 50,)]
    exact = encode(schema, rows).byte_count
    ok = encode(schema, rows, max_bytes=exact)
    assert ok.byte_count == exact == len(ok.artifact_bytes)
    assert len(document(ok)["rows"]) == 2
    refused(schema, rows, max_bytes=exact - 1)
    refused(schema, rows, max_bytes=1)


def test_byte_bound_covers_the_envelope_even_for_an_empty_result() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    exact = encode(schema, []).byte_count
    assert exact > 100  # schema, echo and tags are counted, not just rows
    encode(schema, [], max_bytes=exact)
    refused(schema, [], max_bytes=exact - 1)


def test_oversized_single_cells_refuse_before_encoding() -> None:
    envelope = encode((col("s", LOGICAL_STRING),), []).byte_count
    refused((col("s", LOGICAL_STRING),), [("x" * 1000,)], max_bytes=envelope + 999)
    refused((col("y", LOGICAL_BYTES),), [(b"x" * 1000,)], max_bytes=envelope + 999)


def test_escape_expansion_cannot_slip_under_the_byte_bound() -> None:
    schema = (col("s", LOGICAL_STRING),)
    value = "\u0001" * 40  # six output bytes per character
    exact = encode(schema, [(value,)]).byte_count
    refused(schema, [(value,)], max_bytes=exact - 1)


def test_incremental_byte_count_agrees_with_the_exact_artifact_size() -> None:
    wide = (
        col("i", LOGICAL_INTEGER, nullable=True),
        col("s", LOGICAL_STRING, nullable=True),
        col("y", LOGICAL_BYTES),
        col("d", LOGICAL_DECIMAL, precision=10, scale=2),
        col("f", LOGICAL_FLOAT),
        col("b", LOGICAL_BOOLEAN),
        col("t", LOGICAL_DATE),
        col("z", LOGICAL_TIMESTAMPTZ),
    )
    row = (
        None,
        'é\u0001"\\',
        b"\x00\xff",
        Decimal("1.50"),
        -0.0,
        True,
        date(2024, 2, 29),
        AWARE_PLUS,
    )
    other = (
        -(2**100),
        "",
        b"",
        Decimal("-0.01"),
        1e300,
        False,
        date(1, 1, 1),
        AWARE_UTC,
    )
    cases: list[tuple[Any, list[Any]]] = [
        ((col("a", LOGICAL_INTEGER),), []),
        ((col("a", LOGICAL_INTEGER),), [(1,)]),
        ((col("a", LOGICAL_INTEGER),), [(i,) for i in range(12)]),  # row_count digits
        (wide, [row]),
        (wide, [row, other, row]),
        (wide, [list(row), other]),
    ]
    for schema, rows in cases:
        exact = encode(schema, rows).byte_count
        assert encode(schema, rows, max_bytes=exact).byte_count == exact
        refused(schema, rows, max_bytes=exact - 1)


def test_tiny_byte_bound_stops_cell_processing_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (col("n", LOGICAL_INTEGER), col("s", LOGICAL_STRING))
    rows = [(i, "x" * 10) for i in range(200_000)]
    envelope = encode(schema, rows[:0]).byte_count
    cells = 0
    real = module._encode_cell

    def counting(column: Any, value: Any, room: int) -> object:
        nonlocal cells
        cells += 1
        return real(column, value, room)

    monkeypatch.setattr(module, "_encode_cell", counting)
    # Below the fixed envelope, not one cell is looked at.
    refused(schema, rows, max_rows=BIG, max_bytes=envelope)
    assert cells == 0
    # A few bytes of room refuse within the first rows, not after all 400,000 cells.
    refused(schema, rows, max_rows=BIG, max_bytes=envelope + 40)
    assert 0 < cells < 10
    # Nor does the bound materialise rows: the same input within a fitting bound still works.
    cells = 0
    ok = encode(schema, rows[:3], max_bytes=BIG)
    assert ok.row_count == 3
    assert cells == 6


def test_oversized_rows_stop_before_later_rows_are_touched() -> None:
    class Tripwire(list):  # type: ignore[type-arg]
        pass

    schema = (col("s", LOGICAL_STRING),)
    envelope = encode(schema, []).byte_count
    # Only exact lists and tuples are rows, so a later hostile row is never reached once the
    # bound is already spent by the first.
    rows = [("x" * 100,), Hostile()]
    error = refused(schema, rows, max_bytes=envelope + 50)
    assert str(error) == REFUSE_ANALYSIS_RESULT_ARTIFACT


@pytest.mark.parametrize(
    "bad",
    [True, False, 1.0, "10", None, IntSub(10), -1, MAX_RESULT_ROWS_CEILING + 1],
)
def test_max_rows_must_be_an_exact_bounded_int(bad: Any) -> None:
    refused((col("a", LOGICAL_INTEGER),), [], max_rows=bad)


@pytest.mark.parametrize(
    "bad",
    [True, 1.0, "100", None, IntSub(100), 0, -1, MAX_RESULT_BYTES_CEILING + 1],
)
def test_max_bytes_must_be_an_exact_bounded_int(bad: Any) -> None:
    refused((col("a", LOGICAL_INTEGER),), [], max_bytes=bad)


def test_ceilings_themselves_are_admitted_as_bounds() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    ok = encode(
        schema,
        [],
        max_rows=MAX_RESULT_ROWS_CEILING,
        max_bytes=MAX_RESULT_BYTES_CEILING,
    )
    assert ok.row_count == 0


# ---------------------------------------------------------------------------
# Empty and scalar results
# ---------------------------------------------------------------------------


def test_empty_result_is_valid_and_keeps_its_schema() -> None:
    schema = (col("a", LOGICAL_INTEGER), col("b", LOGICAL_STRING, nullable=True))
    candidate = encode(schema, [])
    assert candidate.row_count == 0
    loaded = document(candidate)
    assert loaded["rows"] == []
    assert loaded["row_count"] == 0
    assert [c["name"] for c in loaded["schema"]["columns"]] == ["a", "b"]
    assert encode(schema, ()).artifact_bytes == candidate.artifact_bytes


def test_scalar_result_is_valid() -> None:
    column = col("total", LOGICAL_DECIMAL, precision=12, scale=2)
    candidate = encode((column,), [(Decimal("42.00"),)])
    assert candidate.row_count == 1
    (cell,) = document(candidate)["rows"][0]
    assert decode_cell(column, cell) == Decimal("42.00")
    assert str(decode_cell(column, cell)) == "42.00"


def test_empty_result_is_distinct_from_a_null_scalar() -> None:
    schema = (col("a", LOGICAL_INTEGER, nullable=True),)
    assert (
        encode(schema, []).artifact_digest != encode(schema, [(None,)]).artifact_digest
    )


# ---------------------------------------------------------------------------
# Immutability and self-proof
# ---------------------------------------------------------------------------


def test_values_are_frozen_and_slotted() -> None:
    candidate = encode((col("a", LOGICAL_INTEGER),), [(1,)])
    for value, field in (
        (candidate, "row_count"),
        (candidate, "truncated"),
        (candidate, "artifact_bytes"),
        (candidate, "artifact_digest"),
        (candidate.echo, "run_id"),
        (candidate.schema[0], "name"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, "x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            delattr(value, field)
    for value in (candidate, candidate.echo, candidate.schema[0]):
        assert not hasattr(value, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            value.extra = 1  # type: ignore[attr-defined]
    assert type(candidate.artifact_bytes) is bytes
    assert type(candidate.schema) is tuple
    assert candidate.truncated is False


def test_candidate_is_independent_of_caller_mutation() -> None:
    rows = [[1]]
    schema = (col("a", LOGICAL_INTEGER),)
    candidate = encode(schema, rows)
    before = candidate.artifact_bytes
    rows[0][0] = 99
    rows.append([2])
    assert candidate.artifact_bytes == before
    assert candidate.row_count == 1


def test_candidate_snapshots_schema_and_echo_from_the_caller() -> None:
    column = col("a", LOGICAL_INTEGER, unit="count:row")
    echo = dataclasses.replace(ECHO)
    schema = (column,)
    candidate = encode(schema, [(1,)], echo=echo)
    before = (
        candidate.artifact_bytes,
        candidate.schema_digest,
        candidate.artifact_digest,
    )
    # Hostile mutation of the caller-owned values cannot reach the candidate's metadata.
    object.__setattr__(column, "name", "zzz")
    object.__setattr__(column, "unit", "count:other")
    object.__setattr__(column, "logical_type", "string")
    object.__setattr__(echo, "run_id", "other-run")
    object.__setattr__(echo, "workspace_id", "other-ws")
    assert candidate.schema[0] is not column
    assert candidate.echo is not echo
    assert candidate.schema[0].name == "a"
    assert candidate.schema[0].unit == "count:row"
    assert candidate.echo == ECHO
    assert before == (
        candidate.artifact_bytes,
        candidate.schema_digest,
        candidate.artifact_digest,
    )
    # The metadata still describes the bytes: re-encoding it reproduces them exactly.
    again = encode(candidate.schema, [(1,)], echo=candidate.echo)
    assert again.artifact_bytes == candidate.artifact_bytes
    loaded = document(candidate)
    assert loaded["echo"]["run_id"] == "run-1"
    assert loaded["schema"]["columns"][0]["name"] == "a"


def _fields(candidate: AnalysisResultArtifactCandidate) -> dict[str, Any]:
    return {
        f.name: getattr_of(candidate, f.name) for f in dataclasses.fields(candidate)
    }


def getattr_of(candidate: AnalysisResultArtifactCandidate, name: str) -> Any:
    return {
        f.name: v
        for f, v in zip(
            dataclasses.fields(candidate), dataclasses.astuple(candidate), strict=True
        )
    }[name]


def _expect_fixed_refusal(build: Any) -> None:
    with pytest.raises(AnalysisResultArtifactRefused) as caught:
        build()
    assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_candidate_cannot_be_constructed_directly_at_all() -> None:
    good = encode((col("a", LOGICAL_INTEGER),), [(1,)])
    fields = _fields(good)
    # Even the candidate's own, fully valid fields are refused: only the encoder seals one.
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate(**fields))
    _expect_fixed_refusal(lambda: dataclasses.replace(good))
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate())
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate(*fields.values()))
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate(1, 2, 3))
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate(**fields, extra=1))
    _expect_fixed_refusal(
        lambda: AnalysisResultArtifactCandidate(
            **{k: v for k, v in fields.items() if k != "echo"}
        )
    )
    _expect_fixed_refusal(
        lambda: AnalysisResultArtifactCandidate(**{**fields, "SECRET_FIELD": 1})
    )
    for change in (
        {"artifact_digest": DIGEST_A},
        {"schema_digest": DIGEST_A},
        {"artifact_bytes": good.artifact_bytes + b" "},
        {"byte_count": good.byte_count + 1},
        {"row_count": -1},
        {"truncated": True},
        {"schema": ()},
        {"echo": None},
    ):
        _expect_fixed_refusal(
            lambda change=change: AnalysisResultArtifactCandidate(
                **{**fields, **change}
            )
        )


def test_correlated_tamper_cannot_forge_a_candidate() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    good = encode(schema, [(1,)])
    other_echo = dataclasses.replace(ECHO, run_id="forged-run")
    foreign = (
        encode(schema, [(1,)], echo=other_echo),
        encode(schema, [(2,), (3,)]),
        encode((col("a", LOGICAL_STRING),), [("1",)]),
        encode((col("a", LOGICAL_INTEGER, unit="count:row"),), [(1,)]),
    )
    for other in foreign:
        # Every exposed identity field is recomputed to be internally consistent with the
        # other artifact's bytes while the echo/schema/row_count claim stays the good one.
        forged = {
            **_fields(good),
            "artifact_bytes": other.artifact_bytes,
            "artifact_digest": other.artifact_digest,
            "byte_count": other.byte_count,
            "row_count": other.row_count,
        }
        _expect_fixed_refusal(
            lambda forged=forged: AnalysisResultArtifactCandidate(**forged)
        )
    # Bytes embedding a different format, schema, echo, row_count and rows, all re-digested.
    envelope = document(good)
    envelope.update({"format": "other/1", "row_count": 7, "rows": [["9"]]})
    forged_bytes = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    forged = {
        **_fields(good),
        "artifact_bytes": forged_bytes,
        "artifact_digest": sha(forged_bytes),
        "byte_count": len(forged_bytes),
    }
    _expect_fixed_refusal(lambda: AnalysisResultArtifactCandidate(**forged))


def test_arbitrary_and_noncanonical_bytes_cannot_be_sealed() -> None:
    good = encode((col("a", LOGICAL_INTEGER),), [(1,)])
    spaced = json.dumps(document(good), indent=2).encode()
    reordered = json.dumps(document(good), separators=(",", ":")).encode()
    for blob in (b"", b"not json", b"{}", b"[]", spaced, reordered, b"\xff\xfe"):
        forged = {
            **_fields(good),
            "artifact_bytes": blob,
            "artifact_digest": sha(blob),
            "byte_count": len(blob),
        }
        _expect_fixed_refusal(
            lambda forged=forged: AnalysisResultArtifactCandidate(**forged)
        )


def test_encoder_enforces_the_hard_artifact_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    good = encode(schema, [(1,)])
    monkeypatch.setattr(module, "MAX_RESULT_BYTES_CEILING", good.byte_count)
    assert encode(schema, [(1,)], max_bytes=good.byte_count) == good
    # The ceiling bounds what a caller may ask for, so no larger bound reaches the encoder.
    refused(schema, [(1,)], max_bytes=good.byte_count + 1)
    # The caller's own smaller bound refuses first.
    refused(schema, [(1,)], max_bytes=good.byte_count - 1)
    refused(schema, [(1,)], max_rows=MAX_RESULT_ROWS_CEILING + 1)


def test_only_the_encoder_builds_a_candidate() -> None:
    assert not hasattr(module, "_seal")
    builders = [
        name
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and "AnalysisResultArtifactCandidate" in obj.__code__.co_names
    ]
    # Only `_new_candidate` makes the object; `_validate` names the class to prove it exact.
    assert builders == ["_new_candidate", "_validate"]
    makers = [
        name
        for name, obj in vars(module).items()
        if inspect.isfunction(obj) and "__new__" in obj.__code__.co_names
    ]
    assert makers == ["_new_candidate"]


def test_oversized_bytes_are_refused_before_base64_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column = (col("y", LOGICAL_BYTES),)
    # One row adds its brackets (2) and then the cell: 4 base64 characters + 2 quotes = 6.
    room = encode(column, []).byte_count + 2
    assert encode(column, [(b"abc",)], max_bytes=room + 6).row_count == 1

    def forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("a value that cannot fit was built")

    monkeypatch.setattr(module.base64, "b64encode", forbidden)
    refused(column, [(b"abc",)], max_bytes=room + 5)
    refused(column, [(b"x" * 10_000_000,)], max_bytes=room + 1000)


def test_oversized_strings_are_refused_before_canonicalisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column = (col("s", LOGICAL_STRING),)
    room = encode(column, []).byte_count + 2
    assert encode(column, [("ab",)], max_bytes=room + 4).row_count == 1
    real = module.canonical_bytes
    seen: list[int] = []

    def watching(value: Any) -> bytes:
        seen.append(len(value) if isinstance(value, str) else 0)
        return real(value)

    monkeypatch.setattr(module, "canonical_bytes", watching)
    refused(column, [("ab",)], max_bytes=room + 3)
    refused(column, [("x" * 10_000_000,)], max_bytes=room + 1000)
    assert max(seen) < 10_000_000  # the oversized string was never canonicalised


def _json_size(text: str) -> int:
    """The canonical JSON size of a scalar-value string, from the stdlib's own escaping."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


ESCAPE_STRINGS = [
    "",
    "plain ascii",
    '"',
    "\\",
    '"\\"\\\\',
    "\b\f\n\r\t",  # every short escape
    *(chr(c) for c in range(0x20)),  # every C0 control, short and \u00xx alike
    "\x7f",  # DEL is literal
    "\x80\u07ff",  # two-byte
    "\u0800\uffff\ue000",  # three-byte, the BMP edges
    "\U00010000\U0010ffff\U0001f600",  # supplementary, four-byte
    '\u00e9"\\\x01\u20ac\U0001f600\n',
]


@pytest.mark.parametrize("text", ESCAPE_STRINGS)
def test_string_size_is_exact_at_the_boundary(text: str) -> None:
    column = col("s", LOGICAL_STRING)
    size = _json_size(text)
    assert module._encode_cell(column, text, size) == text
    with pytest.raises(AnalysisResultArtifactRefused):
        module._encode_cell(column, text, size - 1)
    # End to end: the first row costs its brackets (2) and then the cell.
    envelope = encode((column,), []).byte_count
    ok = encode((column,), [(text,)], max_bytes=envelope + 2 + size)
    assert ok.byte_count == envelope + 2 + size
    refused((column,), [(text,)], max_bytes=envelope + 2 + size - 1)


def test_string_size_matches_the_stdlib_for_every_scalar_class() -> None:
    column = col("s", LOGICAL_STRING)
    for code in (
        *range(0x300),
        0x7FF,
        0x800,
        0xD7FF,
        0xE000,
        0xFFFF,
        0x10000,
        0x10FFFF,
    ):
        text = chr(code)
        assert module._fits_json_string(text, _json_size(text))
        assert not module._fits_json_string(text, _json_size(text) - 1)
        assert module._encode_cell(column, text, _json_size(text)) == text


@pytest.mark.parametrize(
    "text",
    [chr(0xD800), chr(0xDFFF), "ok" + chr(0xDC00) + "bad", chr(0xD83D) + chr(0xDE00)],
    ids=["low-edge", "high-edge", "embedded", "utf16-pair"],
)
def test_lone_surrogates_never_fit_whatever_the_room(text: str) -> None:
    column = col("s", LOGICAL_STRING)
    assert not module._fits_json_string(text, 10_000)
    with pytest.raises(AnalysisResultArtifactRefused):
        module._encode_cell(column, text, 10_000)
    refused((column,), [(text,)])


def test_control_heavy_strings_never_reach_canonicalisation_when_oversized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column = (col("s", LOGICAL_STRING),)
    envelope = encode(column, []).byte_count
    seen: list[int] = []
    real = module.canonical_bytes

    def watching(value: Any) -> bytes:
        seen.append(len(value) if isinstance(value, str) else 0)
        return real(value)

    monkeypatch.setattr(module, "canonical_bytes", watching)
    # Six bytes a character: the value is far over any small room long before its end.
    huge = "\x01" * 5_000_000
    quotes = '"' * 5_000_000  # both built before the measurement starts
    tracemalloc.start()
    try:
        refused(column, [(huge,)], max_bytes=envelope + 2 + 100)
        refused(column, [(quotes,)], max_bytes=envelope + 2 + 100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert max(seen) < 1000  # canonical_bytes never saw the oversized string
    assert peak < 1_000_000  # and no escaped copy of it was built
    # The same control character count fits exactly when the room is exact.
    fits = "\x01" * 20
    assert encode(column, [(fits,)], max_bytes=envelope + 2 + 2 + 20 * 6).row_count == 1
    refused(column, [(fits,)], max_bytes=envelope + 2 + 2 + 20 * 6 - 1)


DECIMAL_CASES = [
    ("1.50", 10, 2),
    ("-1.50", 10, 2),
    ("0.00", 10, 2),
    ("-0.00", 10, 2),  # signed zero keeps its sign
    ("0", 5, 0),
    ("-0", 5, 0),
    ("12345", 5, 0),
    ("-12345", 5, 0),
    ("1E-7", 10, 7),
    ("-0.0000001", 10, 7),
    ("0.12", 2, 2),  # all digits fractional
    ("9" * 38, 38, 0),
    ("-" + "9" * 38, 38, 0),
    ("0." + "9" * 38, 38, 38),
    ("-0." + "0" * 38, 38, 38),
    ("1" + "0" * 17 + "." + "5" * 20, 38, 20),
]


@pytest.mark.parametrize(("text", "precision", "scale"), DECIMAL_CASES)
def test_decimal_size_is_exact_at_the_boundary(
    text: str, precision: int, scale: int
) -> None:
    column = col("d", LOGICAL_DECIMAL, precision=precision, scale=scale)
    value = Decimal(text)
    rendered = format(value, "f")
    assert module._encode_decimal(column, value, len(rendered) + 2) == rendered
    with pytest.raises(AnalysisResultArtifactRefused):
        module._encode_decimal(column, value, len(rendered) + 1)
    envelope = encode((column,), []).byte_count
    ok = encode((column,), [(value,)], max_bytes=envelope + 2 + len(rendered) + 2)
    assert document(ok)["rows"] == [[rendered]]
    refused((column,), [(value,)], max_bytes=envelope + 2 + len(rendered) + 1)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1" * 39),  # one digit over the precision-38 ceiling
        Decimal("1" * 7),  # over precision 5
        Decimal("1E-1"),  # wrong exponent
        Decimal("1E+1"),
        Decimal("NaN"),
        Decimal("-Infinity"),
    ],
)
def test_decimal_proofs_refuse_near_the_precision_edge(value: Decimal) -> None:
    refused((col("d", LOGICAL_DECIMAL, precision=5, scale=0),), [(value,)])
    refused((col("d", LOGICAL_DECIMAL, precision=38, scale=0),), [(Decimal("1" * 39),)])


def test_million_digit_decimal_is_refused_before_it_is_copied() -> None:
    # Built first, so only the encoder's own allocation is measured below.
    huge = Decimal("9" * 1_000_000 + ".25")
    negative = Decimal("-" + "9" * 1_000_000 + ".25")  # unary minus would round it
    assert huge.adjusted() == 999_999
    column = col("d", LOGICAL_DECIMAL, precision=38, scale=2)
    tracemalloc.start()
    try:
        with pytest.raises(AnalysisResultArtifactRefused):
            module._encode_decimal(column, huge, 4)
        refused((column,), [(huge,)], max_bytes=encode((column,), []).byte_count + 4)
        refused((column,), [(huge,)])  # and under an ample bound too
        refused((column,), [(negative,)])
        refused((col("d", LOGICAL_DECIMAL, precision=38, scale=0),), [(huge,)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # as_tuple() alone would hold a million entries (about 8 MB) and format() 1 MB.
    assert peak < 200_000
    # The same digits as a fitting wide-precision value are not what is refused: the bound is.
    ok = Decimal("9" * 36 + ".25")
    assert module._encode_decimal(column, ok, 100) == "9" * 36 + ".25"


def test_encoder_call_shapes_refuse_with_the_fixed_reason() -> None:
    schema = (col("a", LOGICAL_INTEGER),)
    kwargs: dict[str, Any] = {"echo": ECHO, "max_rows": 10, "max_bytes": BIG}
    call: Any = encode_analysis_result_artifact
    assert call(schema, [(1,)], **kwargs).row_count == 1
    assert call(schema=schema, rows=[(1,)], **kwargs).row_count == 1
    shapes: list[Any] = [
        lambda: call(),
        lambda: call(schema),
        lambda: call(schema, [(1,)]),
        lambda: call(schema, [(1,)], echo=ECHO, max_rows=10),
        lambda: call(schema, [(1,)], echo=ECHO, max_bytes=BIG),
        lambda: call(schema, [(1,)], max_rows=10, max_bytes=BIG),
        lambda: call(schema, [(1,)], **kwargs, SECRET_FIELD=1),
        lambda: call(schema, [(1,)], ECHO, max_rows=10, max_bytes=BIG),
        lambda: call(schema, [(1,)], ECHO, 10, BIG),
        lambda: call(schema, [(1,)], [(2,)], **kwargs),
        lambda: call(schema, [(1,)], schema=schema, **kwargs),
        lambda: call(schema, [(1,)], **{**kwargs, "extra": 1}),
        lambda: call(rows=[(1,)], **kwargs),
        lambda: call(
            *[schema, [(1,)]], **{StrSub("echo"): ECHO, "max_rows": 1, "max_bytes": 9}
        ),
    ]
    for build in shapes:
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            build()
        assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert "SECRET_FIELD" not in repr(caught.value)
        assert "max_rows" not in repr(caught.value)
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None


# ---------------------------------------------------------------------------
# Bounded, non-leaking refusals
# ---------------------------------------------------------------------------

SECRETS = (
    "SECRET-CELL-VALUE",
    "/Users/someone/workspace/secret.db",
    "SELECT secret FROM private_table WHERE token = 'abc'",
    "Bearer sk-live-credential",
)


@pytest.mark.parametrize("secret", SECRETS)
def test_refusal_carries_only_the_fixed_reason(secret: str) -> None:
    for schema, rows in (
        ((col("i", LOGICAL_INTEGER),), [(secret,)]),
        ((col("s", LOGICAL_STRING),), [(secret, secret)]),
        ((col("i", LOGICAL_INTEGER),), [(StrSub(secret),)]),
        ((col("y", LOGICAL_BYTES),), [(secret,)]),
        ((col("d", LOGICAL_DECIMAL, precision=3, scale=1),), [(secret,)]),
        ((col("z", LOGICAL_TIMESTAMPTZ),), [(secret,)]),
    ):
        error = refused(schema, rows)
        assert error.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert error.reason == REFUSE_ANALYSIS_RESULT_ARTIFACT
        assert str(error) == REFUSE_ANALYSIS_RESULT_ARTIFACT
        assert secret not in repr(error)
        assert error.__cause__ is None
        assert error.__context__ is None


@pytest.mark.parametrize("secret", SECRETS)
def test_invalid_constructor_input_refuses_without_leaking(secret: str) -> None:
    for build in (
        lambda: col(secret + "\n", LOGICAL_INTEGER),
        lambda: col("a", LOGICAL_INTEGER, unit=secret + "\n"),
        lambda: col("a", secret + "\n"),
    ):
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            build()
        assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert secret not in repr(caught.value)
    # Valid identifiers that collide still refuse at encode, with the same fixed reason.
    error = refused((col("dup", LOGICAL_INTEGER), col("DUP", LOGICAL_INTEGER)), [])
    assert error.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
    assert error.__context__ is None


def test_refusal_over_byte_bound_does_not_quote_the_rows() -> None:
    secret = "SECRET-CELL-VALUE-" + "x" * 200
    error = refused((col("s", LOGICAL_STRING),), [(secret,)], max_bytes=50)
    assert str(error) == REFUSE_ANALYSIS_RESULT_ARTIFACT
    assert "SECRET" not in repr(error)
    assert len(repr(error)) < 100


def test_hostile_objects_raise_nothing_but_the_fixed_refusal() -> None:
    schema = (col("s", LOGICAL_STRING),)
    for rows in ([(Hostile(),)], [Hostile()], Hostile()):
        error = refused(schema, rows)
        assert str(error) == REFUSE_ANALYSIS_RESULT_ARTIFACT
        assert error.__cause__ is None
        assert error.__context__ is None
        assert "SECRET" not in repr(error)


def test_constructor_refusals_are_also_fixed() -> None:
    for build in (
        lambda: AnalysisResultColumn("SECRET-NAME\n", LOGICAL_INTEGER, False),
        lambda: AnalysisExecutionEcho(
            "w", "SECRET RUN", "s", "a", DIGEST_A, DIGEST_A, DIGEST_A, DIGEST_A
        ),
        lambda: AnalysisExecutionEcho(
            "SECRET WS", "r", "s", "a", DIGEST_A, DIGEST_A, DIGEST_A, DIGEST_A
        ),
    ):
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            build()
        assert caught.value.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert "SECRET" not in repr(caught.value)


def test_refusal_message_is_a_fixed_bounded_literal() -> None:
    assert REFUSE_ANALYSIS_RESULT_ARTIFACT == "analysis_result_artifact_refused"
    assert len(REFUSE_ANALYSIS_RESULT_ARTIFACT) < 64


def test_artifact_carries_no_path_credential_or_sql_beyond_the_given_cells(
    tmp_path: Path,
) -> None:
    candidate = encode(
        (col("n", LOGICAL_INTEGER), col("s", LOGICAL_STRING)),
        [(1, "hello"), (2, "world")],
    )
    text = candidate.artifact_bytes.decode("utf-8")
    assert str(tmp_path) not in text
    assert "/Users" not in text
    assert "SELECT" not in text.upper()
    assert "token" not in text.lower()
    # Only the echo's own identities and digests describe where the result came from.
    assert set(document(candidate)) == {
        "echo",
        "format",
        "row_count",
        "rows",
        "schema",
        "schema_digest",
    }


def test_module_is_internal_only() -> None:
    package = Path(inspect.getfile(module)).with_name("__init__.py").read_text()
    assert "result_artifact" not in package
    source = inspect.getsource(module)
    for forbidden in ("open(", "pathlib", "sqlite3", "duckdb", "os.path", "import os"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# The mandatory trust-boundary validator
# ---------------------------------------------------------------------------

validate = validate_analysis_result_artifact_candidate
CANDIDATE_FIELDS = tuple(
    f.name for f in dataclasses.fields(AnalysisResultArtifactCandidate)
)


def fields_of(candidate: AnalysisResultArtifactCandidate) -> dict[str, Any]:
    return {n: object.__getattribute__(candidate, n) for n in CANDIDATE_FIELDS}


def forge(**fields: Any) -> AnalysisResultArtifactCandidate:
    """What any same-process caller can do: an exact-class object with arbitrary slots."""
    forged = object.__new__(AnalysisResultArtifactCandidate)
    for name, value in fields.items():
        object.__setattr__(forged, name, value)
    return forged


def forge_from(
    good: AnalysisResultArtifactCandidate, **changes: Any
) -> AnalysisResultArtifactCandidate:
    return forge(**{**fields_of(good), **changes})


def forge_bytes(
    good: AnalysisResultArtifactCandidate, blob: bytes
) -> AnalysisResultArtifactCandidate:
    """Bytes with the artifact digest and byte count recomputed so those two agree."""
    return forge_from(
        good, artifact_bytes=blob, artifact_digest=sha(blob), byte_count=len(blob)
    )


def forge_doc(
    good: AnalysisResultArtifactCandidate, doc: Any
) -> AnalysisResultArtifactCandidate:
    return forge_bytes(good, module.canonical_bytes(doc))


def refuses(candidate: Any) -> None:
    _expect_fixed_refusal(lambda: validate(candidate))


WIDE = (
    col("i", LOGICAL_INTEGER),
    col("s", LOGICAL_STRING),
    col("d", LOGICAL_DECIMAL, nullable=True, precision=5, scale=2, unit="currency:AUD"),
)
WIDE_ROWS = [(1, "a", Decimal("1.00")), (2, "b", None)]


def wide() -> AnalysisResultArtifactCandidate:
    return encode(WIDE, WIDE_ROWS)


def test_normal_candidate_validates_to_an_equal_independent_snapshot() -> None:
    good = wide()
    snapshot = validate(good)
    assert type(snapshot) is AnalysisResultArtifactCandidate
    assert snapshot == good
    assert snapshot is not good
    assert snapshot.artifact_bytes == good.artifact_bytes
    assert snapshot.schema is not good.schema
    assert snapshot.echo is not good.echo
    assert all(a is not b for a, b in zip(snapshot.schema, good.schema, strict=True))
    # Validation is idempotent and the snapshot is itself a valid candidate.
    assert validate(snapshot) == snapshot
    # Later damage to the caller's object cannot reach the snapshot.
    object.__setattr__(good.schema[0], "name", "zzz")
    object.__setattr__(good.echo, "run_id", "other")
    object.__setattr__(good, "artifact_bytes", b"x")
    assert snapshot == validate(snapshot)
    assert snapshot.schema[0].name == "i"
    assert snapshot.echo.run_id == "run-1"
    assert document(snapshot)["rows"][0][0] == "1"


class _CountedSlot:
    """A slot descriptor that counts reads of the tracked source objects only."""

    def __init__(
        self, original: Any, name: str, tracked: list[Any], reads: dict[Any, int]
    ) -> None:
        self.original, self.name = original, name
        self.tracked, self.reads = tracked, reads

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is not None and any(instance is t for t in self.tracked):
            key = (type(instance).__name__, id(instance), self.name)
            self.reads[key] = self.reads.get(key, 0) + 1
        return self.original.__get__(instance, owner)

    def __set__(self, instance: Any, value: Any) -> None:
        self.original.__set__(instance, value)


def test_validator_reads_every_handed_in_field_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = encode((col("a", LOGICAL_INTEGER),), [(1,)])  # a real candidate, built first
    source_column, source_echo = good.schema[0], good.echo
    tracked: list[Any] = [good, source_column, source_echo]
    reads: dict[Any, int] = {}
    for cls in (
        AnalysisResultArtifactCandidate,
        AnalysisResultColumn,
        AnalysisExecutionEcho,
    ):
        for field in dataclasses.fields(cls):
            slot = cls.__dict__[field.name]
            monkeypatch.setattr(
                cls, field.name, _CountedSlot(slot, field.name, tracked, reads)
            )
    snapshot = validate(good)
    expected = {
        (type(obj).__name__, id(obj), field.name)
        for obj in tracked
        for field in dataclasses.fields(type(obj))
    }
    assert len(expected) == 8 + 6 + 8
    assert reads == dict.fromkeys(expected, 1)  # no field missed, none read twice
    _ = good.row_count  # the instrument itself sees a second read
    assert reads[("AnalysisResultArtifactCandidate", id(good), "row_count")] == 2
    monkeypatch.undo()
    assert snapshot is not good
    assert snapshot.schema[0] is not source_column
    assert snapshot.echo is not source_echo
    assert snapshot == good


def test_encoder_returns_the_validators_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = encode((col("a", LOGICAL_INTEGER),), [(9,)])
    seen: list[AnalysisResultArtifactCandidate] = []

    def spy(candidate: AnalysisResultArtifactCandidate) -> Any:
        seen.append(candidate)
        return sentinel

    monkeypatch.setattr(module, "_validate", spy)
    assert encode((col("a", LOGICAL_INTEGER),), [(1,)]) is sentinel
    assert len(seen) == 1 and seen[0] is not sentinel

    def deny(candidate: Any) -> Any:
        raise AnalysisResultArtifactRefused()

    monkeypatch.setattr(module, "_validate", deny)
    refused((col("a", LOGICAL_INTEGER),), [(1,)])


def test_validator_is_not_exported() -> None:
    package = Path(inspect.getfile(module)).with_name("__init__.py").read_text()
    assert "validate_analysis_result_artifact_candidate" not in package


def test_a_candidate_forged_with_its_own_fields_is_the_same_value() -> None:
    good = wide()
    assert validate(forge_from(good)) == good
    assert validate(forge(**fields_of(good))) == good


def test_edge_values_survive_the_validator() -> None:
    schema = (
        col("i", LOGICAL_INTEGER),
        col("f", LOGICAL_FLOAT),
        col("d", LOGICAL_DECIMAL, precision=38, scale=38),
        col("y", LOGICAL_BYTES),
        col("t", LOGICAL_DATE),
        col("z", LOGICAL_TIMESTAMPTZ),
        col("s", LOGICAL_STRING),
        col("b", LOGICAL_BOOLEAN),
    )
    rows = [
        (
            2**127 - 1,
            1.7976931348623157e308,
            Decimal("-0." + "0" * 37 + "1"),
            b"",
            date.min,
            datetime(1, 1, 1, tzinfo=UTC),
            "\u0000é\U0001f600",
            True,
        ),
        (
            -(2**127),
            5e-324,
            Decimal("0." + "0" * 38),
            b"\x00\xff" * 7,
            date.max,
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            "",
            False,
        ),
        (
            0,
            -0.0,
            Decimal("0." + "9" * 38),
            b"abc",
            date(2024, 2, 29),
            datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5))),
            '"\\',
            True,
        ),
    ]
    good = encode(schema, rows)
    assert validate(good) == good


# -- the fields of a real candidate, one at a time and together ----------------------------

OTHER_ECHO = dataclasses.replace(ECHO, run_id="forged-run")

FIELD_MUTATIONS = {
    "schema": [(), None, [], tuple(WIDE[:2]), (WIDE[1], WIDE[0], WIDE[2]), WIDE + WIDE],
    "echo": [None, OTHER_ECHO, object(), ECHO],
    "schema_digest": [DIGEST_A, "", None, "sha256:" + "A" * 64, 5],
    "artifact_digest": [DIGEST_A, "", None, "sha256:" + "A" * 64, 5],
    "row_count": [0, 1, 3, -1, True, IntSub(2), 2.0, None, MAX_RESULT_ROWS_CEILING + 1],
    "byte_count": [0, 1, 10**9, -1, True, None, 2.5],
    "truncated": [True, 0, 1, None, "False"],
    "artifact_bytes": [
        b"",
        b"{}",
        bytearray(b"{}"),
        memoryview(b"{}"),
        "{}",
        None,
        BytesSub(b"{}"),
    ],
}


@pytest.mark.parametrize("field", CANDIDATE_FIELDS)
def test_every_authority_field_is_reproven_when_mutated_alone(field: str) -> None:
    for value in FIELD_MUTATIONS[field]:
        good = wide()
        if (
            value is ECHO
        ):  # an equal-valued echo is not a mutation of the echo's content
            assert validate(forge_from(good, echo=ECHO)) == good
            continue
        object.__setattr__(good, field, value)
        refuses(good)


def test_matching_bytes_digest_and_count_are_still_not_enough() -> None:
    good = wide()
    other = encode((col("a", LOGICAL_INTEGER),), [(1,)])
    refuses(forge_bytes(good, other.artifact_bytes))
    refuses(
        forge_from(
            good,
            artifact_bytes=other.artifact_bytes,
            artifact_digest=other.artifact_digest,
            byte_count=other.byte_count,
            row_count=other.row_count,
        )
    )


def test_mixing_two_real_candidates_never_validates() -> None:
    a = wide()
    b = encode(
        (col("a", LOGICAL_STRING),),
        [("x",), ("y",), ("z",)],
        echo=OTHER_ECHO,
    )
    groups = (
        ("artifact_bytes", "artifact_digest", "byte_count"),
        ("schema", "schema_digest"),
        ("echo",),
        ("row_count",),
    )
    for pick in itertools.product((a, b), repeat=len(groups)):
        mixed: dict[str, Any] = {}
        for source, names in zip(pick, groups, strict=True):
            mixed.update({n: object.__getattribute__(source, n) for n in names})
        mixed["truncated"] = False
        candidate = forge(**mixed)
        if all(p is a for p in pick):
            assert validate(candidate) == a
        elif all(p is b for p in pick):
            assert validate(candidate) == b
        else:
            refuses(candidate)


SCHEMA_FIELD_MUTATIONS = {
    "name": "zzz",
    "logical_type": LOGICAL_STRING,
    "nullable": False,
    "precision": 6,
    "scale": 1,
    "unit": "currency:USD",
}


@pytest.mark.parametrize("field", sorted(SCHEMA_FIELD_MUTATIONS))
def test_candidate_owned_schema_columns_are_reproven_when_mutated(field: str) -> None:
    value = SCHEMA_FIELD_MUTATIONS[field]
    for column_index in range(len(WIDE)):
        good = wide()
        if object.__getattribute__(good.schema[column_index], field) == value:
            continue  # not a mutation of this column
        object.__setattr__(good.schema[column_index], field, value)
        refuses(good)
    good = wide()
    object.__setattr__(good.schema[2], field, None)
    refuses(good)


@pytest.mark.parametrize("field", module._ECHO_FIELDS)
def test_candidate_owned_echo_is_reproven_when_mutated(field: str) -> None:
    alternative = DIGEST_E if field.endswith("_digest") else "other-id"
    good = wide()
    object.__setattr__(good.echo, field, alternative)
    refuses(good)
    for bad in (None, 5, "", StrSub(getattr(ECHO, field))):
        good = wide()
        object.__setattr__(good.echo, field, bad)
        refuses(good)


def _clone_as(cls: type, source: Any) -> Any:
    clone = object.__new__(cls)
    for name in (f.name for f in dataclasses.fields(source)):
        object.__setattr__(clone, name, object.__getattribute__(source, name))
    return clone


def test_equal_valued_subclasses_never_bypass_exact_runtime_types() -> None:
    class ColumnSub(AnalysisResultColumn):
        pass

    class EchoSub(AnalysisExecutionEcho):
        pass

    assert validate(wide()) == wide()  # the unmodified originals are fine
    # Checksum and count fields: an equal string or integer of a subclass is refused.
    for field, subclass in (
        ("schema_digest", StrSub),
        ("artifact_digest", StrSub),
        ("byte_count", IntSub),
        ("row_count", IntSub),
    ):
        good = wide()
        refuses(forge_from(good, **{field: subclass(getattr(good, field))}))
    good = wide()
    refuses(forge_from(good, artifact_bytes=BytesSub(good.artifact_bytes)))
    # Forged exact-content containers and values: equal on every field, wrong exact type.
    good = wide()
    refuses(forge_from(good, schema=TupleSub(good.schema)))
    refuses(
        forge_from(good, schema=tuple(_clone_as(ColumnSub, c) for c in good.schema))
    )
    refuses(forge_from(good, echo=_clone_as(EchoSub, good.echo)))
    for field, subclass in (
        ("name", StrSub),
        ("logical_type", StrSub),
        ("unit", StrSub),
        ("precision", IntSub),
        ("scale", IntSub),
    ):
        good = wide()
        column = good.schema[2]
        object.__setattr__(
            column, field, subclass(object.__getattribute__(column, field))
        )
        refuses(good)
    for field in module._ECHO_FIELDS:
        good = wide()
        object.__setattr__(good.echo, field, StrSub(getattr(good.echo, field)))
        refuses(good)


def test_nested_swaps_correlated_with_the_candidate_are_refused() -> None:
    good = wide()
    # A different but individually valid schema and echo, with the schema digest re-pointed:
    # the bytes still carry the original, so the candidate disagrees with its own bytes.
    other_schema = (col("a", LOGICAL_INTEGER),)
    other = encode(other_schema, [(1,)], echo=OTHER_ECHO)
    refuses(forge_from(good, schema=other.schema, schema_digest=other.schema_digest))
    refuses(forge_from(good, echo=other.echo))
    refuses(forge_from(good, schema=other.schema))
    refuses(forge_from(good, schema_digest=other.schema_digest))


def test_candidate_subclasses_and_foreign_objects_refuse() -> None:
    class Sub(AnalysisResultArtifactCandidate):
        pass

    good = wide()
    forged = object.__new__(Sub)
    for name, value in fields_of(good).items():
        object.__setattr__(forged, name, value)
    for value in (
        forged,
        None,
        1,
        "x",
        object(),
        fields_of(good),
        dataclasses,
        good.echo,
    ):
        refuses(value)


def test_candidate_with_missing_slots_refuses() -> None:
    good = wide()
    for name in CANDIDATE_FIELDS:
        partial = {n: v for n, v in fields_of(good).items() if n != name}
        refuses(forge(**partial))
    refuses(forge())


# -- forged documents --------------------------------------------------------------------


def doc_of(good: AnalysisResultArtifactCandidate) -> dict[str, Any]:
    return document(good)


def test_arbitrary_noncanonical_and_hostile_bytes_cannot_validate() -> None:
    good = wide()
    canonical = good.artifact_bytes.decode()
    duplicate = canonical.replace('"row_count":2', '"row_count":2,"row_count":2')
    assert duplicate != canonical
    first_row = '"rows":[["1"'
    blobs = [
        b"",
        b"not json",
        b"{}",
        b"[]",
        b"null",
        b"\xff\xfe",
        b"\xef\xbb\xbf" + good.artifact_bytes,  # a BOM
        json.dumps(doc_of(good), indent=2).encode(),
        json.dumps(doc_of(good)).encode(),  # spaced separators
        json.dumps(doc_of(good), separators=(",", ":")).encode(),  # unsorted? same keys
        json.dumps(
            dict(reversed(list(doc_of(good).items()))), separators=(",", ":")
        ).encode(),  # reordered members
        duplicate.encode(),
        canonical.replace('"row_count":2', '"row_count":NaN').encode(),
        canonical.replace('"row_count":2', '"row_count":Infinity').encode(),
        canonical.replace('"row_count":2', '"row_count":2.0').encode(),
        canonical.replace('"row_count":2', '"row_count":2e0').encode(),
        canonical.replace('"row_count":2', '"row_count":' + "9" * 5000).encode(),
        good.artifact_bytes + b"\n",
        good.artifact_bytes + good.artifact_bytes,
        good.artifact_bytes[:-1],
        b"[" * 100_000,
        b'{"a":' * 100_000,
        canonical.replace(first_row, '"rows":[["\\ud800"').encode(),
        canonical.replace('"a"', '"\\u0061"').encode(),  # a non-minimal escape
    ]
    for blob in blobs:
        if blob == good.artifact_bytes:
            continue
        refuses(forge_bytes(good, blob))
        # Without recomputing the digest or the count, the disagreement alone refuses.
        refuses(forge_from(good, artifact_bytes=blob))


def test_oversized_artifact_bytes_are_refused_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = wide()
    refuses(forge_bytes(good, b" " * (MAX_RESULT_BYTES_CEILING + 1)))
    monkeypatch.setattr(module, "MAX_RESULT_BYTES_CEILING", good.byte_count)
    assert validate(good) == good
    monkeypatch.setattr(module, "MAX_RESULT_BYTES_CEILING", good.byte_count - 1)
    refuses(good)


def test_row_ceiling_is_a_hard_limit_on_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = wide()
    monkeypatch.setattr(module, "MAX_RESULT_ROWS_CEILING", 2)
    assert validate(good) == good
    monkeypatch.setattr(module, "MAX_RESULT_ROWS_CEILING", 1)
    refuses(good)


def test_envelope_key_set_and_format_are_exact() -> None:
    good = wide()
    keys = list(doc_of(good))
    for key in keys:
        broken = doc_of(good)
        del broken[key]
        refuses(forge_doc(good, broken))
    for extra in ("extra", "truncated", "Rows", ""):
        broken = doc_of(good)
        broken[extra] = 1
        refuses(forge_doc(good, broken))
    for fmt in ("analysis-result-artifact/2", "other/1", "", None, 1, ["x"]):
        broken = doc_of(good)
        broken["format"] = fmt
        refuses(forge_doc(good, broken))
    for shape in ([], "rows", None, 1):
        refuses(forge_doc(good, shape))


def schema_digest_of(schema_document: Any) -> str:
    return sha(
        module.canonical_bytes(
            {"format": module._SCHEMA_FORMAT, "schema": schema_document}
        )
    )


def test_schema_document_must_equal_the_candidate_schema_exactly() -> None:
    good = wide()
    base = doc_of(good)
    column_changes = [
        ("name", "zzz"),
        ("logical_type", LOGICAL_STRING),
        ("nullable", True),
        ("precision", 6),
        ("scale", 1),
        ("unit", "count:row"),
        ("unit", None),
    ]
    for key, value in column_changes:
        for index in range(len(WIDE)):
            broken = copy.deepcopy(base)
            if broken["schema"]["columns"][index][key] == value:
                continue
            broken["schema"]["columns"][index][key] = value
            refuses(forge_doc(good, broken))  # digest left stale
            broken["schema_digest"] = schema_digest_of(broken["schema"])
            refuses(forge_doc(good, broken))  # digest re-pointed to the forged schema
    structural = []
    for mutate in (
        lambda d: d["schema"]["columns"].pop(),
        lambda d: d["schema"]["columns"].append(d["schema"]["columns"][0]),
        lambda d: d["schema"]["columns"].reverse(),
        *(
            lambda d, key=key: d["schema"]["columns"][0].pop(key)
            for key in module._COLUMN_FIELDS
        ),
        lambda d: d["schema"]["columns"][0].update(extra=1),
        lambda d: d["schema"].update(extra=1),
        lambda d: d["schema"].clear(),
        lambda d: d.update(schema=None),
        lambda d: d.update(schema=[]),
    ):
        broken = copy.deepcopy(base)
        mutate(broken)
        structural.append(broken)
    for broken in structural:
        refuses(forge_doc(good, broken))
        if isinstance(broken["schema"], (dict, list)):
            broken["schema_digest"] = schema_digest_of(broken["schema"])
            refuses(forge_doc(good, broken))


def test_schema_digest_must_be_fresh_and_present_in_the_bytes() -> None:
    good = wide()
    for bad in (
        DIGEST_A,
        "",
        None,
        1,
        good.schema_digest.upper(),
        good.artifact_digest,
    ):
        broken = doc_of(good)
        broken["schema_digest"] = bad
        refuses(forge_doc(good, broken))
    # The candidate's own schema digest must be the fresh one as well.
    refuses(forge_from(good, schema_digest=DIGEST_A))


def test_echo_document_must_equal_the_candidate_echo_exactly() -> None:
    good = wide()
    for field in module._ECHO_FIELDS:
        alternative = DIGEST_E if field.endswith("_digest") else "other-id"
        for value in (alternative, None, 5, ""):
            broken = doc_of(good)
            broken["echo"][field] = value
            refuses(forge_doc(good, broken))
        broken = doc_of(good)
        del broken["echo"][field]
        refuses(forge_doc(good, broken))
    broken = doc_of(good)
    broken["echo"]["extra"] = "x"
    refuses(forge_doc(good, broken))
    for shape in (None, [], "echo", 1):
        broken = doc_of(good)
        broken["echo"] = shape
        refuses(forge_doc(good, broken))


def test_row_count_and_row_list_must_agree_with_the_candidate() -> None:
    good = wide()
    for count in (0, 1, 3, -1, True, "2", None, 10**6 + 1):
        broken = doc_of(good)
        broken["row_count"] = count
        refuses(forge_doc(good, broken))
    for rows in (
        [],
        [["1", "a", "1.00"]],
        doc_of(good)["rows"] * 2,
        None,
        "rows",
        {"0": 1},
        [["1", "a", "1.00"], ["2", "b", None], None],
    ):
        broken = doc_of(good)
        broken["rows"] = rows
        refuses(forge_doc(good, broken))
    # A matching document count with a candidate that claims another count.
    refuses(forge_from(good, row_count=1))
    refuses(forge_from(good, row_count=3))


def test_row_width_and_shape_must_match_the_schema() -> None:
    good = wide()
    for row in (
        [],
        ["1"],
        ["1", "a"],
        ["1", "a", "1.00", "x"],
        ["1", "a", None, None],
        None,
        "1a1.00",
        {"i": "1"},
        5,
    ):
        broken = doc_of(good)
        broken["rows"][1] = row
        refuses(forge_doc(good, broken))
    empty = encode(WIDE, [])
    assert validate(empty) == empty
    broken = doc_of(empty)
    broken["rows"] = [[]]
    broken["row_count"] = 1
    refuses(forge_doc(empty, broken))
    refuses(forge_from(empty, row_count=1))


def test_non_nullable_cells_refuse_null() -> None:
    good = wide()
    for column in (0, 1):
        broken = doc_of(good)
        broken["rows"][0][column] = None
        refuses(forge_doc(good, broken))
    broken = doc_of(good)
    assert broken["rows"][1][2] is None  # a nullable cell stays valid
    assert validate(forge_doc(good, broken)) == good


# -- invalid and noncanonical cells for every logical type -----------------------------------

BAD_CELLS = {
    LOGICAL_BOOLEAN: ["true", "false", "True", 1, 0, 1.0, [], {}, "", [True]],
    LOGICAL_INTEGER: [
        1,
        True,
        False,
        1.0,
        [],
        {},
        "",
        "01",
        "-0",
        "+1",
        " 1",
        "1 ",
        "1\n",
        "1_0",
        "0x1",
        "1.0",
        "1e3",
        "-",
        "--1",
        "\uff11",
        "\u0661",
        str(2**127),
        str(-(2**127) - 1),
        "9" * 41,
        "9" * 5000,
        "0" * 50,
        "-" + "0" * 39,
    ],
    LOGICAL_FLOAT: [
        1.5,
        1,
        True,
        [],
        {},
        "",
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        "1e400",
        "-1e400",
        "1",
        "1.50",
        "1.0e0",
        "01.0",
        " 1.0",
        "1.0 ",
        "+1.0",
        "1_0.0",
        ".5",
        "5e-1",
        "1e5",
        "1E5",
        "0x1p0",
        "\uff11.0",
        "1." + "0" * 40,
        "1" * 1000,
        "1e+0",
    ],
    LOGICAL_DECIMAL: [
        1.5,
        1,
        True,
        [],
        {},
        "",
        "1.5",
        "1.500",
        "1",
        "1234.56",
        "+1.00",
        " 1.00",
        "1.00 ",
        "1_0.00",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1E+2",
        "1e2",
        "10000.00",
        ".50",
        "1.",
        "00.50",
        "1.00\n",
        "\uff11.\uff10\uff10",
        "1" * 100,
        "1" * 1_000_000,
        "0." + "0" * 100,
        "1E+999999999",
    ],
    LOGICAL_STRING: [1, True, 1.5, [], {}, ["a"], {"a": 1}],
    LOGICAL_BYTES: [
        1,
        [],
        {},
        "AQ",
        "AQ=",
        "AR==",
        "AQ==\n",
        " AQ==",
        "A Q==",
        "AQ==AQ==",
        "AQ===",
        "!!!!",
        "\u00e9",
        "====",
        "AQ-_",
        "AQ\u0661=",
    ],
    LOGICAL_DATE: [
        1,
        [],
        {},
        "",
        "2024-1-1",
        "20240101",
        "2024-W01-1",
        "2024-02-30",
        "2024-01-01 ",
        " 2024-01-01",
        "0000-01-01",
        "+2024-01-01",
        "2024/01/01",
        "2024-01-01T00:00:00",
        "\uff12024-01-01",
        "2024-001",
        "9" * 5000,
    ],
    LOGICAL_TIMESTAMPTZ: [
        1,
        [],
        {},
        "",
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:00:00.000000",
        "2024-01-01T00:00:00.000000+00:00",
        "2024-01-01T00:00:00.000000z",
        "2024-01-01 00:00:00.000000Z",
        "2024-01-01T00:00:00,000000Z",
        "2024-13-01T00:00:00.000000Z",
        "2024-01-01T24:00:00.000000Z",
        "2024-01-01T00:00:00.00000Z",
        "2024-01-01T00:00:00.0000000Z",
        "2024-01-01T00:00:00.000000ZZ",
        "0000-01-01T00:00:00.000000Z",
        "2024-01-01T00:00:00.000000\u00e9",
        "9" * 5000,
    ],
}

COLUMN_FOR = {
    LOGICAL_BOOLEAN: col("c", LOGICAL_BOOLEAN),
    LOGICAL_INTEGER: col("c", LOGICAL_INTEGER),
    LOGICAL_FLOAT: col("c", LOGICAL_FLOAT),
    LOGICAL_DECIMAL: col("c", LOGICAL_DECIMAL, precision=5, scale=2),
    LOGICAL_STRING: col("c", LOGICAL_STRING),
    LOGICAL_BYTES: col("c", LOGICAL_BYTES),
    LOGICAL_DATE: col("c", LOGICAL_DATE),
    LOGICAL_TIMESTAMPTZ: col("c", LOGICAL_TIMESTAMPTZ),
}

GOOD_CELL = {
    LOGICAL_BOOLEAN: True,
    LOGICAL_INTEGER: 7,
    LOGICAL_FLOAT: 1.5,
    LOGICAL_DECIMAL: Decimal("1.50"),
    LOGICAL_STRING: "x",
    LOGICAL_BYTES: b"\x01",
    LOGICAL_DATE: date(2024, 1, 1),
    LOGICAL_TIMESTAMPTZ: datetime(2024, 1, 1, tzinfo=UTC),
}


def test_cell_vectors_cover_every_logical_type() -> None:
    assert (
        set(BAD_CELLS)
        == set(COLUMN_FOR)
        == set(GOOD_CELL)
        == set(module._LOGICAL_TYPES)
    )


@pytest.mark.parametrize("kind", sorted(BAD_CELLS))
def test_invalid_and_noncanonical_cells_refuse(kind: str) -> None:
    good = encode((COLUMN_FOR[kind],), [(GOOD_CELL[kind],)])
    assert validate(good) == good
    for cell in BAD_CELLS[kind]:
        broken = doc_of(good)
        broken["rows"] = [[cell]]
        refuses(forge_doc(good, broken))
    # A null in a non-nullable column, and a good cell behind a bad one in a later row.
    broken = doc_of(good)
    broken["rows"] = [[doc_of(good)["rows"][0][0]], [None]]
    broken["row_count"] = 2
    refuses(forge_doc(good, broken))
    for cell in BAD_CELLS[kind][:3]:
        broken = doc_of(good)
        broken["rows"] = [doc_of(good)["rows"][0], [cell]]
        broken["row_count"] = 2
        refuses(forge_doc(good, broken))


def test_text_length_is_checked_before_any_number_or_time_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_decimal, real_float = module.Decimal, float

    def spy_decimal(*args: Any) -> Any:
        calls.append("decimal")
        return real_decimal(*args)

    def spy_float(*args: Any) -> Any:
        calls.append("float")
        return real_float(*args)

    monkeypatch.setattr(module, "Decimal", spy_decimal)
    monkeypatch.setattr(module, "float", spy_float, raising=False)
    for kind, cell in (
        (LOGICAL_DECIMAL, "1" * 1_000_000),
        (LOGICAL_DECIMAL, "9" * 49),
        (LOGICAL_FLOAT, "1" * 100_000),
        (LOGICAL_FLOAT, "1" * 33),
    ):
        monkeypatch.undo()
        good = encode((COLUMN_FOR[kind],), [(GOOD_CELL[kind],)])
        monkeypatch.setattr(module, "Decimal", spy_decimal)
        monkeypatch.setattr(module, "float", spy_float, raising=False)
        broken = doc_of(good)
        broken["rows"] = [[cell]]
        refuses(forge_doc(good, broken))
    assert calls == []


def test_every_cell_of_every_row_is_checked() -> None:
    schema = (col("a", LOGICAL_INTEGER), col("b", LOGICAL_DATE))
    rows = [(i, date(2024, 1, 1)) for i in range(5)]
    good = encode(schema, rows)
    for row in range(5):
        for column, bad in ((0, "01"), (1, "2024-1-1")):
            broken = doc_of(good)
            broken["rows"][row][column] = bad
            refuses(forge_doc(good, broken))


# -- one fixed, non-leaking refusal family ------------------------------------------------


class Exploding:
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("SECRET-EQ")

    __hash__ = None  # type: ignore[assignment]

    def __len__(self) -> int:
        raise RuntimeError("SECRET-LEN")

    def __bool__(self) -> bool:
        raise RuntimeError("SECRET-BOOL")

    def __iter__(self) -> Any:
        raise RuntimeError("SECRET-ITER")

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("SECRET-ATTR")


def test_hostile_field_values_stay_inside_the_fixed_refusal() -> None:
    good = wide()
    for name in CANDIDATE_FIELDS:
        for hostile in (Exploding(), Hostile()):
            refuses(forge_from(good, **{name: hostile}))
    for hostile in (Exploding(), Hostile()):
        refuses(hostile)
    columns = (Exploding(),)
    refuses(forge_from(good, schema=columns))


def test_secrets_in_forged_documents_never_reach_the_refusal() -> None:
    good = wide()
    secret = "SECRET-VALUE-9f3c"
    broken = doc_of(good)
    broken["rows"][0][0] = secret
    broken["echo"]["run_id"] = secret
    for forged in (
        forge_doc(good, broken),
        forge_bytes(good, secret.encode()),
        forge_from(good, artifact_digest=secret, schema_digest=secret),
        forge_from(good, artifact_bytes=secret),
    ):
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            validate(forged)
        error = caught.value
        assert error.args == (REFUSE_ANALYSIS_RESULT_ARTIFACT,)
        assert error.__cause__ is None and error.__context__ is None
        assert error.__suppress_context__ is False or error.__context__ is None
        assert secret not in repr(error) and secret not in str(error)
        assert error.__traceback__ is not None
        frames = []
        tb = error.__traceback__
        while tb is not None:
            frames.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        assert frames[-1] == "validate_analysis_result_artifact_candidate"


def test_validator_refuses_from_inside_a_caller_handler_without_chaining_its_own_cause() -> (
    None
):
    good = wide()
    try:
        raise ValueError("caller context")
    except ValueError:
        with pytest.raises(AnalysisResultArtifactRefused) as caught:
            validate(forge_from(good, artifact_bytes=b"{"))
    assert caught.value.__cause__ is None
    assert type(caught.value.__context__) is ValueError


def test_validator_signature_takes_exactly_one_candidate() -> None:
    parameters = inspect.signature(validate).parameters
    assert list(parameters) == ["candidate"]
    with pytest.raises(TypeError):
        validate()  # type: ignore[call-arg]
