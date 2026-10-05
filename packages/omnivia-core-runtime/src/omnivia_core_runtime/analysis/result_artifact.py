"""Pure analytical result artifact value and encoding protocol (WP03 worker, WP07 writer).

One schema, the rows and the worker's execution echo are encoded into one bounded RFC 8785
canonical document, and the candidate that comes back is an immutable value carrying the
exact bytes with their schema digest, artifact digest, row count and byte count.

A candidate is built internally, by the encoder at the end of a successful encode and by the
validator's independent reconstruction. Its public constructor always refuses, so no caller
can pair bytes with an identity the bytes do not have through it. The schema and echo a
candidate carries are rebuilt from the caller's values, so later mutation of the caller's
objects cannot desynchronise them from the frozen bytes.

Python cannot make `object.__new__` and `object.__setattr__` unavailable, so a constructor
that refuses is not proof of authenticity: any same-process caller can still populate the
slots of an exact-class candidate, or alter a real one. The proof is therefore
`validate_analysis_result_artifact_candidate`, the one mandatory boundary. A consumer calls it
immediately before it uses a candidate and uses only the independent snapshot it returns,
never the object it was handed. It snapshots every field once, re-parses the bytes, and
proves the whole envelope, every digest and count and every cell against the schema before
it rebuilds a fresh candidate. The encoder runs the same proof before it returns.

Type fidelity is deliberate and closed. Each column declares one logical type, and a cell is
accepted only when its exact Python type is the one that logical type names. Nothing is
coerced or stringified on a guess:

- `boolean` is `bool`; `integer` is exact `int` within signed 128 bits (never `bool`);
- `float` is a finite `float`, written as `repr` so `-0.0` and the shortest round trip survive;
- `decimal` is a finite `Decimal` whose digit count fits the declared precision and whose
  exponent is exactly minus the declared scale, written in plain notation;
- `string` is `str`; `bytes` is `bytes`, written as base64;
- `date` is `date` (never `datetime`); `timestamptz` is an exact `datetime` whose `tzinfo` is
  the built-in fixed-offset `timezone`, an exact stdlib `ZoneInfo`, or the pytz UTC singleton
  captured once at import, written as the same instant in UTC with microseconds. The pytz
  singleton is the only pytz support: it is what a DuckDB connection materialises
  `TIMESTAMPTZ` as when its TimeZone is UTC, a trusted UTC-normalised seam. It is admitted by
  identity and its offset is a constant zero, so no pytz registry, cache or object state is
  read; arbitrary or non-UTC pytz zones are refused, and no other `tzinfo` is ever asked a
  question.

Decimal interpretation lives in the schema (precision and scale), and the artifact stores no
display formatting. A column's optional unit is an exact `Identifier` (for example
`currency:AUD`); `None` means no unit. Every candidate is whole: a result over either caller
bound refuses instead of truncating, so `truncated` is always false. The byte bound is
accounted cell by cell against the exact canonical size, so an oversized input stops early.

Every refusal is one fixed reason with no row value, SQL text, field name or path in it. The
failing cause is dropped before the refusal is raised, so nothing it quoted reaches
`__context__`; malformed call shapes refuse the same way instead of raising `TypeError`.

Internal only: nothing here is exported from the analysis package, and nothing here touches
the filesystem, storage, the clock or the public API. It is not canonical
publication and it does not activate `analysis.start`.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final, TypeGuard, overload
from zoneinfo import ZoneInfo

# pytz ships no inline types; the ignore is tolerant of types-pytz being installed too.
import pytz  # type: ignore[import-untyped,unused-ignore]

from omnivia_core.contracts.v1 import (
    CONTENT_CHECKSUM_ALGORITHM,
    ContentChecksum,
    Identifier,
    WorkspaceId,
    is_content_checksum,
    is_identifier,
    is_workspace_id,
)
from omnivia_core.contracts.v1.canonical_json import (
    canonical_bytes,
    parse_json_document,
)

LOGICAL_BOOLEAN: Final = "boolean"
LOGICAL_INTEGER: Final = "integer"
LOGICAL_FLOAT: Final = "float"
LOGICAL_DECIMAL: Final = "decimal"
LOGICAL_STRING: Final = "string"
LOGICAL_BYTES: Final = "bytes"
LOGICAL_DATE: Final = "date"
LOGICAL_TIMESTAMPTZ: Final = "timestamptz"

_LOGICAL_TYPES: Final = (
    LOGICAL_BOOLEAN,
    LOGICAL_INTEGER,
    LOGICAL_FLOAT,
    LOGICAL_DECIMAL,
    LOGICAL_STRING,
    LOGICAL_BYTES,
    LOGICAL_DATE,
    LOGICAL_TIMESTAMPTZ,
)

#: Hard ceilings on what a caller may ask for; the caller's own bounds sit at or below them.
MAX_RESULT_ROWS_CEILING: Final = 1_000_000
MAX_RESULT_BYTES_CEILING: Final = 64 * 1024 * 1024
MAX_RESULT_COLUMNS: Final = 256
_MAX_DECIMAL_PRECISION: Final = 38
_SHORT_CONTROLS: Final = "\b\f\n\r\t"  # the controls RFC 8785 writes as two characters
_INT_MIN: Final = -(2**127)
_INT_MAX: Final = 2**127 - 1

# Each text test checks a short length before anything is parsed, so a forged cell cannot make
# int, float, Decimal or datetime allocate more than a few dozen characters' worth.
_MAX_INTEGER_TEXT: Final = 40  # sign and the 39 digits of 2**127
_MAX_FLOAT_TEXT: Final = 32  # repr of the widest double is 24
_MAX_DECIMAL_TEXT: Final = 48  # sign, 38 digits and a point at most
_INTEGER_TEXT: Final = re.compile(r"-?(?:0|[1-9][0-9]*)")
_DATE_TEXT: Final = 10  # YYYY-MM-DD
_TIMESTAMPTZ_TEXT: Final = 27  # YYYY-MM-DDTHH:MM:SS.ffffffZ
_ONE_DAY: Final = timedelta(days=1)  # a UTC offset is strictly inside it
_PYTZ_UTC: Final = pytz.UTC  # captured once at import; never re-read from pytz
_ARTIFACT_KEYS: Final = frozenset(
    ("format", "schema", "schema_digest", "echo", "row_count", "rows")
)

#: Internal format tags that domain-separate the two digests. Not a public wire contract.
_SCHEMA_FORMAT: Final = "analysis-result-schema/1"
_ARTIFACT_FORMAT: Final = "analysis-result-artifact/1"

#: The one refusal reason. A fixed literal: no row, SQL or path is ever interpolated.
REFUSE_ANALYSIS_RESULT_ARTIFACT: Final = "analysis_result_artifact_refused"

_COLUMN_FIELDS: Final = (
    "name",
    "logical_type",
    "nullable",
    "precision",
    "scale",
    "unit",
)
_COLUMN_DEFAULTS: Final = {"precision": None, "scale": None, "unit": None}
_ECHO_FIELDS: Final = (
    "workspace_id",
    "run_id",
    "run_step_id",
    "attempt_id",
    "plan_digest",
    "parameters_digest",
    "final_sql_digest",
    "input_vector_digest",
)
_ENCODE_FIELDS: Final = ("schema", "rows", "echo", "max_rows", "max_bytes")
_ENCODE_POSITIONAL: Final = 2  # echo and both bounds are keyword-only


class AnalysisResultArtifactRefused(Exception):
    """The single refusal this protocol raises, carrying only its fixed reason."""

    def __init__(self) -> None:
        super().__init__(REFUSE_ANALYSIS_RESULT_ARTIFACT)
        self.reason = REFUSE_ANALYSIS_RESULT_ARTIFACT


@dataclass(frozen=True, slots=True, init=False)
class AnalysisResultColumn:
    """One result column: an identifier name, an explicit logical type, decimal precision
    and scale, and an optional unit (`None` means no unit)."""

    name: Identifier
    logical_type: str
    nullable: bool
    precision: int | None
    scale: int | None
    unit: Identifier | None

    if TYPE_CHECKING:

        def __init__(
            self,
            name: Identifier,
            logical_type: str,
            nullable: bool,
            precision: int | None = None,
            scale: int | None = None,
            unit: Identifier | None = None,
        ) -> None: ...

    else:

        def __init__(self, *args: object, **kwargs: object) -> None:
            _assign(
                self,
                _bind(
                    _COLUMN_FIELDS, _COLUMN_DEFAULTS, args, kwargs, len(_COLUMN_FIELDS)
                ),
            )
            if not _valid_column(self):
                raise AnalysisResultArtifactRefused()


@dataclass(frozen=True, slots=True, init=False)
class AnalysisExecutionEcho:
    """The worker's echo: the canonical Core lineage plus the four exact digests."""

    workspace_id: WorkspaceId
    run_id: Identifier
    run_step_id: Identifier
    attempt_id: Identifier
    plan_digest: ContentChecksum
    parameters_digest: ContentChecksum
    final_sql_digest: ContentChecksum
    input_vector_digest: ContentChecksum

    if TYPE_CHECKING:

        def __init__(
            self,
            workspace_id: WorkspaceId,
            run_id: Identifier,
            run_step_id: Identifier,
            attempt_id: Identifier,
            plan_digest: ContentChecksum,
            parameters_digest: ContentChecksum,
            final_sql_digest: ContentChecksum,
            input_vector_digest: ContentChecksum,
        ) -> None: ...

    else:

        def __init__(self, *args: object, **kwargs: object) -> None:
            _assign(self, _bind(_ECHO_FIELDS, {}, args, kwargs, len(_ECHO_FIELDS)))
            if not _valid_echo(self):
                raise AnalysisResultArtifactRefused()


@dataclass(frozen=True, slots=True, init=False)
class AnalysisResultArtifactCandidate:
    """One whole, immutable encoded result: exact bytes with their identity.

    Only the encoder and the validator's reconstruction build one. The public constructor
    refuses every call shape, because a hand-built candidate could pair bytes with a format,
    schema, echo, row count or digest they do not carry. `artifact_bytes` is the authority a consumer decodes. The constructor
    cannot stop `object.__new__`, so a consumer never trusts an object it was handed: it
    passes it through `validate_analysis_result_artifact_candidate` and uses the snapshot.
    """

    schema: tuple[AnalysisResultColumn, ...]
    echo: AnalysisExecutionEcho
    schema_digest: ContentChecksum
    artifact_digest: ContentChecksum
    row_count: int
    byte_count: int
    truncated: bool
    artifact_bytes: bytes

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AnalysisResultArtifactRefused()


@overload
def encode_analysis_result_artifact(
    schema: tuple[AnalysisResultColumn, ...],
    rows: list[tuple[object, ...]],
    *,
    echo: AnalysisExecutionEcho,
    max_rows: int,
    max_bytes: int,
) -> AnalysisResultArtifactCandidate: ...


@overload
def encode_analysis_result_artifact(
    schema: tuple[AnalysisResultColumn, ...],
    rows: tuple[tuple[object, ...], ...],
    *,
    echo: AnalysisExecutionEcho,
    max_rows: int,
    max_bytes: int,
) -> AnalysisResultArtifactCandidate: ...


def encode_analysis_result_artifact(
    *args: object, **kwargs: object
) -> AnalysisResultArtifactCandidate:
    """Encode `rows` under `schema` and `echo` into one candidate, or refuse.

    The typed shape is `(schema, rows, *, echo, max_rows, max_bytes)`; any other call shape
    refuses like any other bad input. Exceeding `max_rows` or `max_bytes` refuses; the
    result is never truncated.
    """
    candidate: AnalysisResultArtifactCandidate | None = None
    try:
        candidate = _encode(_bind(_ENCODE_FIELDS, {}, args, kwargs, _ENCODE_POSITIONAL))
    except Exception:  # noqa: BLE001
        candidate = None  # every cause collapses into the one fixed refusal
    # Raised outside the handler so no cause (and no value it quoted) reaches __context__.
    if candidate is None:
        raise AnalysisResultArtifactRefused()
    return candidate


def _encode(values: Mapping[str, object]) -> AnalysisResultArtifactCandidate:
    # The annotations are the contract; every argument is still proven at runtime.
    max_rows, max_bytes, rows = values["max_rows"], values["max_bytes"], values["rows"]
    if not (
        _bounded_int(max_rows, 0, MAX_RESULT_ROWS_CEILING)
        and _bounded_int(max_bytes, 1, MAX_RESULT_BYTES_CEILING)
        and _is_sequence(rows)
        and len(rows) <= max_rows
    ):
        raise AnalysisResultArtifactRefused()
    schema = _snapshot_columns(values["schema"])
    echo = _snapshot_echo(values["echo"])
    schema_digest = _schema_digest(schema)
    row_count = len(rows)
    # The envelope is fixed once the row count is, so its exact size is known up front and
    # every row and cell after it is charged against what the caller's bound has left.
    envelope = len(
        canonical_bytes(_artifact_document(schema, schema_digest, echo, row_count, []))
    )
    room = max_bytes - envelope
    if room < 0:
        raise AnalysisResultArtifactRefused()
    width = len(schema)
    encoded_rows: list[list[object]] = []
    used = 0
    for row in rows:
        if not _is_sequence(row) or len(row) != width:
            raise AnalysisResultArtifactRefused()
        used += (
            (1 if encoded_rows else 0) + width + 1
        )  # row separator, brackets, commas
        if used > room:
            raise AnalysisResultArtifactRefused()
        cells: list[object] = []
        for column, value in zip(schema, row, strict=True):
            cell = _encode_cell(column, value, room - used)
            used += len(canonical_bytes(cell))
            if used > room:
                raise AnalysisResultArtifactRefused()
            cells.append(cell)
        encoded_rows.append(cells)
    artifact = canonical_bytes(
        _artifact_document(schema, schema_digest, echo, row_count, encoded_rows)
    )
    if len(encoded_rows) != row_count or len(artifact) != envelope + used:
        raise AnalysisResultArtifactRefused()  # the incremental count and the bytes disagree
    # Even a fresh candidate is proven by the same boundary every consumer must cross.
    return _validate(
        _new_candidate(
            schema, echo, schema_digest, _checksum(artifact), row_count, artifact
        )
    )


def _new_candidate(
    schema: tuple[AnalysisResultColumn, ...],
    echo: AnalysisExecutionEcho,
    schema_digest: ContentChecksum,
    artifact_digest: ContentChecksum,
    row_count: int,
    artifact: bytes,
) -> AnalysisResultArtifactCandidate:
    """The one place a candidate object is made; never a proof that its fields agree."""
    candidate = object.__new__(AnalysisResultArtifactCandidate)
    _assign(
        candidate,
        {
            "schema": schema,
            "echo": echo,
            "schema_digest": schema_digest,
            "artifact_digest": artifact_digest,
            "row_count": row_count,
            "byte_count": len(artifact),
            "truncated": False,
            "artifact_bytes": artifact,
        },
    )
    return candidate


def validate_analysis_result_artifact_candidate(
    candidate: object,
) -> AnalysisResultArtifactCandidate:
    """Prove `candidate` whole and return a new, independent snapshot, or refuse.

    The mandatory boundary for every consumer: call it immediately before staging and use
    only the returned candidate (and its `artifact_bytes`), never the argument. Nothing the
    caller holds is retained, so later mutation of the argument or its nested schema and
    echo cannot reach the snapshot. Every failure is the one fixed refusal.
    """
    result: AnalysisResultArtifactCandidate | None = None
    try:
        result = _validate(candidate)
    except Exception:  # noqa: BLE001
        result = (
            None  # every parser, type and conversion failure collapses into the refusal
        )
    # Raised outside the handler so no cause (and no value it quoted) reaches __context__.
    if result is None:
        raise AnalysisResultArtifactRefused()
    return result


def _validate(candidate: object) -> AnalysisResultArtifactCandidate:
    if type(candidate) is not AnalysisResultArtifactCandidate:
        raise AnalysisResultArtifactRefused()
    # Every exposed field is read exactly once; nothing below touches the argument again.
    raw_schema, raw_echo = candidate.schema, candidate.echo
    schema_digest, artifact_digest = candidate.schema_digest, candidate.artifact_digest
    row_count, byte_count = candidate.row_count, candidate.byte_count
    truncated, data = candidate.truncated, candidate.artifact_bytes
    if not (
        type(data) is bytes
        and len(data) <= MAX_RESULT_BYTES_CEILING
        and _bounded_int(byte_count, 0, MAX_RESULT_BYTES_CEILING)
        and byte_count == len(data)
        and _bounded_int(row_count, 0, MAX_RESULT_ROWS_CEILING)
        and truncated is False
        and type(schema_digest) is str
        and is_content_checksum(schema_digest)
        and type(artifact_digest) is str
        and is_content_checksum(artifact_digest)
    ):
        raise AnalysisResultArtifactRefused()
    schema = _snapshot_columns(raw_schema)
    echo = _snapshot_echo(raw_echo)
    expected_schema_digest = _schema_digest(schema)
    if schema_digest != expected_schema_digest or artifact_digest != _checksum(data):
        raise AnalysisResultArtifactRefused()
    document = parse_json_document(data)
    if (
        canonical_bytes(document) != data
    ):  # whitespace, ordering or any noncanonical form
        raise AnalysisResultArtifactRefused()
    if not (type(document) is dict and set(document) == _ARTIFACT_KEYS):
        raise AnalysisResultArtifactRefused()
    rows = document["rows"]
    if not (
        document["format"] == _ARTIFACT_FORMAT
        and document["schema_digest"] == expected_schema_digest
        and _same_json(document["schema"], _schema_document(schema))
        and _same_json(document["echo"], _echo_document(echo))
        and type(document["row_count"]) is int
        and document["row_count"] == row_count
        and type(rows) is list
        and len(rows) == row_count
    ):
        raise AnalysisResultArtifactRefused()
    width = len(schema)
    for row in rows:
        if type(row) is not list or len(row) != width:
            raise AnalysisResultArtifactRefused()
        for column, cell in zip(schema, row, strict=True):
            _check_cell(column, cell)
    # Every part is proven; the bytes must be exactly what a fresh encode of it would write.
    if (
        canonical_bytes(
            _artifact_document(schema, expected_schema_digest, echo, row_count, rows)
        )
        != data
    ):
        raise AnalysisResultArtifactRefused()
    return _new_candidate(
        schema, echo, expected_schema_digest, artifact_digest, row_count, data
    )


def _same_json(left: object, right: object) -> bool:
    # Byte equality of the canonical forms: `==` would let `True` equal `1`.
    return canonical_bytes(left) == canonical_bytes(right)


def _check_cell(column: AnalysisResultColumn, cell: object) -> None:
    """Refuse unless `cell` is exactly what the encoder writes for this column's type."""
    if cell is None:
        if column.nullable:
            return
        raise AnalysisResultArtifactRefused()
    kind = column.logical_type
    if kind == LOGICAL_BOOLEAN:
        ok = type(cell) is bool
    elif type(cell) is not str:
        ok = False  # every other logical type is written as text
    elif kind == LOGICAL_INTEGER:
        ok = _canonical_integer(cell)
    elif kind == LOGICAL_FLOAT:
        ok = _canonical_float(cell)
    elif kind == LOGICAL_DECIMAL:
        ok = _canonical_decimal(column, cell)
    elif kind == LOGICAL_STRING:
        ok = True
    elif kind == LOGICAL_BYTES:
        ok = _canonical_base64(cell)
    elif kind == LOGICAL_DATE:
        ok = _canonical_date(cell)
    else:
        ok = _canonical_timestamptz(cell)
    if not ok:
        raise AnalysisResultArtifactRefused()


def _canonical_integer(cell: str) -> bool:
    return (
        len(cell) <= _MAX_INTEGER_TEXT
        and _INTEGER_TEXT.fullmatch(cell) is not None
        and cell != "-0"
        and _INT_MIN <= int(cell) <= _INT_MAX
    )


def _canonical_float(cell: str) -> bool:
    if len(cell) > _MAX_FLOAT_TEXT:
        return False
    value = float(
        cell
    )  # accepts spellings repr never writes; repr equality closes them
    return math.isfinite(value) and repr(value) == cell


def _canonical_decimal(column: AnalysisResultColumn, cell: str) -> bool:
    return len(cell) <= _MAX_DECIMAL_TEXT and (
        _encode_decimal(column, Decimal(cell), _MAX_DECIMAL_TEXT + 2) == cell
    )


def _canonical_base64(cell: str) -> bool:
    raw = base64.b64decode(cell, validate=True)
    return base64.b64encode(raw).decode("ascii") == cell


def _canonical_date(cell: str) -> bool:
    return len(cell) == _DATE_TEXT and date.fromisoformat(cell).isoformat() == cell


def _canonical_timestamptz(cell: str) -> bool:
    if len(cell) != _TIMESTAMPTZ_TEXT or cell[-1] != "Z":
        return False
    instant = datetime.fromisoformat(cell[:-1])
    return instant.isoformat(timespec="microseconds") + "Z" == cell


def _encode_cell(column: AnalysisResultColumn, value: object, room: int) -> object:
    if value is None:
        if column.nullable:
            return None
        raise AnalysisResultArtifactRefused()
    kind = column.logical_type
    # Every branch proves the exact type first, so a subclass never reaches a method call.
    if kind == LOGICAL_BOOLEAN and type(value) is bool:
        return value
    if kind == LOGICAL_INTEGER and type(value) is int and _INT_MIN <= value <= _INT_MAX:
        return str(value)
    if kind == LOGICAL_FLOAT and type(value) is float and math.isfinite(value):
        return repr(value)
    if kind == LOGICAL_DECIMAL and type(value) is Decimal:
        return _encode_decimal(column, value, room)
    # A string's exact canonical size is counted before anything is built from it; base64
    # is exactly 4 bytes per 3. A value that cannot fit refuses first.
    if kind == LOGICAL_STRING and type(value) is str and _fits_json_string(value, room):
        return value
    if (
        kind == LOGICAL_BYTES
        and type(value) is bytes
        and -(-len(value) // 3) * 4 + 2 <= room
    ):
        return base64.b64encode(value).decode("ascii")
    if kind == LOGICAL_DATE and type(value) is date:
        return value.isoformat()
    if kind == LOGICAL_TIMESTAMPTZ and type(value) is datetime:
        return _encode_timestamptz(value)
    raise AnalysisResultArtifactRefused()


def _fits_json_string(value: str, room: int) -> bool:
    """Whether the exact canonical JSON size of `value`, quotes included, is within `room`.

    Counted one character at a time against the RFC 8785 escaping, and abandoned as soon as
    the running size passes `room`, so the work is bounded by `room` and nothing is built.
    A lone surrogate has no canonical form and never fits.
    """
    size = 2
    for char in value:
        code = ord(char)
        if code < 0x20:
            size += 2 if char in _SHORT_CONTROLS else 6  # \b \f \n \r \t, else \u00xx
        elif char == '"' or char == "\\":
            size += 2
        elif code < 0x80:
            size += 1
        elif code < 0x800:
            size += 2
        elif 0xD800 <= code <= 0xDFFF:
            return False
        elif code < 0x10000:
            size += 3
        else:
            size += 4
        if size > room:
            return False
    return size <= room


def _encode_decimal(column: AnalysisResultColumn, value: Decimal, room: int) -> str:
    precision, scale = column.precision, column.scale
    # Every test below reads the exponent and digit count in constant time: `as_tuple()` and
    # `format` copy the whole coefficient, so neither runs before the digits are proven few.
    if (
        precision is None
        or scale is None
        or precision > _MAX_DECIMAL_PRECISION
        or not value.is_finite()
        or not value.same_quantum(Decimal((0, (1,), -scale)))  # exponent is -scale
        or value.adjusted() + scale + 1 > precision  # coefficient digit count
    ):
        raise AnalysisResultArtifactRefused()
    text = format(value, "f")  # at most 38 digits, a point and a sign
    if len(text) + 2 > room:
        raise AnalysisResultArtifactRefused()
    return text


def _timestamptz_offset(value: datetime) -> timedelta:
    zone = value.tzinfo
    # Only closed, library-owned providers are consulted: a custom, stateful or subclassed
    # tzinfo is never called, so the same value always encodes to the same instant.
    offset: object
    if type(zone) is timezone:
        offset = zone.utcoffset(None)
    elif type(zone) is ZoneInfo:
        offset = zone.utcoffset(value)  # honours `fold`
    elif zone is _PYTZ_UTC:
        # Identity only: nothing is read or called on the singleton or on pytz, so poisoned
        # registries and instance state cannot move this offset.
        return timedelta(0)
    else:
        raise AnalysisResultArtifactRefused()
    if type(offset) is not timedelta or not -_ONE_DAY < offset < _ONE_DAY:
        raise AnalysisResultArtifactRefused()
    return offset


def _encode_timestamptz(value: datetime) -> str:
    # The wall clock is shifted by hand so no datetime conversion method is involved.
    instant = value.replace(tzinfo=None) - _timestamptz_offset(value)
    return instant.isoformat(timespec="microseconds") + "Z"


def _valid_column(column: object) -> bool:
    if type(column) is not AnalysisResultColumn:
        return False
    name, kind, nullable = column.name, column.logical_type, column.nullable
    precision, scale, unit = column.precision, column.scale, column.unit
    if not (
        type(name) is str
        and is_identifier(name)
        and type(kind) is str
        and kind in _LOGICAL_TYPES
        and type(nullable) is bool
        and (unit is None or (type(unit) is str and is_identifier(unit)))
    ):
        return False
    if kind == LOGICAL_DECIMAL:
        return (
            type(precision) is int
            and type(scale) is int
            and 1 <= precision <= _MAX_DECIMAL_PRECISION
            and 0 <= scale <= precision
        )
    return precision is None and scale is None


def _valid_echo(echo: object) -> bool:
    if type(echo) is not AnalysisExecutionEcho:
        return False
    workspace = echo.workspace_id
    identities = (echo.run_id, echo.run_step_id, echo.attempt_id)
    digests = (
        echo.plan_digest,
        echo.parameters_digest,
        echo.final_sql_digest,
        echo.input_vector_digest,
    )
    return (
        type(workspace) is str
        and is_workspace_id(workspace)
        and all(type(v) is str and is_identifier(v) for v in identities)
        and all(type(v) is str and is_content_checksum(v) for v in digests)
    )


def _snapshot_columns(columns: object) -> tuple[AnalysisResultColumn, ...]:
    """Rebuild every column from one read of its fields, proving each on construction."""
    if type(columns) is not tuple or not 1 <= len(columns) <= MAX_RESULT_COLUMNS:
        raise AnalysisResultArtifactRefused()
    snapshot = []
    for column in columns:
        if type(column) is not AnalysisResultColumn:
            raise AnalysisResultArtifactRefused()
        snapshot.append(
            AnalysisResultColumn(
                column.name,
                column.logical_type,
                column.nullable,
                column.precision,
                column.scale,
                column.unit,
            )
        )
    if len({column.name.casefold() for column in snapshot}) != len(snapshot):
        raise AnalysisResultArtifactRefused()
    return tuple(snapshot)


def _snapshot_echo(echo: object) -> AnalysisExecutionEcho:
    if type(echo) is not AnalysisExecutionEcho:
        raise AnalysisResultArtifactRefused()
    return AnalysisExecutionEcho(
        echo.workspace_id,
        echo.run_id,
        echo.run_step_id,
        echo.attempt_id,
        echo.plan_digest,
        echo.parameters_digest,
        echo.final_sql_digest,
        echo.input_vector_digest,
    )


def _bind(
    names: tuple[str, ...],
    defaults: Mapping[str, object],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    positional: int,
) -> dict[str, object]:
    """Bind a call shape to `names`, refusing missing, extra and duplicate fields alike."""
    values: dict[str, object] | None = None
    try:
        values = _bind_values(names, defaults, args, kwargs, positional)
    except Exception:  # noqa: BLE001
        values = None
    if values is None:
        raise AnalysisResultArtifactRefused()
    return values


def _bind_values(
    names: tuple[str, ...],
    defaults: Mapping[str, object],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    positional: int,
) -> dict[str, object] | None:
    if len(args) > positional:
        return None
    values: dict[str, object] = dict(zip(names, args, strict=False))
    for key, value in kwargs.items():
        if type(key) is not str or key not in names or key in values:
            return None
        values[key] = value
    for name in names:
        if name not in values:
            if name not in defaults:
                return None
            values[name] = defaults[name]
    return values


def _assign(target: object, values: Mapping[str, object]) -> None:
    for name, value in values.items():
        object.__setattr__(target, name, value)


def _bounded_int(value: object, low: int, high: int) -> TypeGuard[int]:
    # Exact `int` only: `bool` and every subclass fail the proof before any comparison.
    return type(value) is int and low <= value <= high


def _is_sequence(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return type(value) is list or type(value) is tuple


def _schema_document(schema: tuple[AnalysisResultColumn, ...]) -> dict[str, object]:
    return {
        "columns": [
            {
                "name": column.name,
                "logical_type": column.logical_type,
                "nullable": column.nullable,
                "precision": column.precision,
                "scale": column.scale,
                "unit": column.unit,
            }
            for column in schema
        ]
    }


def _echo_document(echo: AnalysisExecutionEcho) -> dict[str, object]:
    return {
        "workspace_id": echo.workspace_id,
        "run_id": echo.run_id,
        "run_step_id": echo.run_step_id,
        "attempt_id": echo.attempt_id,
        "plan_digest": echo.plan_digest,
        "parameters_digest": echo.parameters_digest,
        "final_sql_digest": echo.final_sql_digest,
        "input_vector_digest": echo.input_vector_digest,
    }


def _artifact_document(
    schema: tuple[AnalysisResultColumn, ...],
    schema_digest: ContentChecksum,
    echo: AnalysisExecutionEcho,
    row_count: int,
    rows: list[list[object]],
) -> dict[str, object]:
    return {
        "format": _ARTIFACT_FORMAT,
        "schema": _schema_document(schema),
        "schema_digest": schema_digest,
        "echo": _echo_document(echo),
        "row_count": row_count,
        "rows": rows,
    }


def _schema_digest(schema: tuple[AnalysisResultColumn, ...]) -> ContentChecksum:
    return _checksum(
        canonical_bytes({"format": _SCHEMA_FORMAT, "schema": _schema_document(schema)})
    )


def _checksum(data: bytes) -> ContentChecksum:
    # sha256 is the implementation of the one accepted content checksum algorithm.
    return f"{CONTENT_CHECKSUM_ALGORITHM}:{hashlib.sha256(data).hexdigest()}"
