"""Phase 2 temporal contract: effective-valid intervals over evidence-backed time.

Implements the frozen decisions of section 4 of the Phase 2 decision record
(`docs/development/omnivia-core-semantic-registry-phase-2-decision-record-2026-09-12.md`):
UTC-canonical instants at one of six precisions, timezone resolution that
fails closed rather than guesses, `effective_from`/`effective_to` resolution
from stated/attested boundaries per the B3 formula, and an explicit, queryable
ingestion-time fallback for record time. Every failure here raises one of the
`semantic_registry` structured exceptions (never a bare stdlib exception), and
no error message echoes untrusted `source_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    TemporalValidationError,
)

TEMPORAL_CONTRACT_VERSION = "effective-valid-interval-v1"

#: Bounds on the auxiliary source metadata an instant may carry. Both are
#: counted in Unicode code points, which is what SQLite's `length()` counts for
#: a TEXT value, so the typed boundary and the storage guard refuse the same
#: inputs rather than two overlapping-but-different sets.
MAX_ORIGINAL_SOURCE_TEXT_CHARS = 2048
MAX_SOURCE_TIMEZONE_CHARS = 255

#: `+HH`, `-HH:MM`, `+HHMM`. Anything else must name UTC or resolve through the
#: IANA database, or be refused -- a timezone is never inferred from free text.
_NUMERIC_UTC_OFFSET = re.compile(r"^[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?$")

#: Spellings of UTC itself that carry no regional rules and so need no lookup.
_UTC_SPELLINGS = frozenset({"UTC", "utc", "Z", "z", "+00:00", "-00:00"})


class TemporalPrecision(str, Enum):
    """Canonical precision vocabulary (decision record section 4)."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"


class TemporalProvenance(str, Enum):
    """Where a temporal instant's value came from."""

    STATED = "stated"
    EVIDENCE_ATTESTED = "evidence_attested"
    INGESTION_FALLBACK = "ingestion_fallback"


class EndBoundaryState(str, Enum):
    """The stated shape of an interval's end (decision record section 4)."""

    STATED = "stated"
    UNKNOWN = "unknown"
    OPEN = "open"


#: Precisions finer than a day. A timezone-less source string cannot be placed on
#: the UTC line at any of them without guessing an offset, so its structured value
#: is deliberately reduced to the source calendar day.
_SUB_DAY_PRECISIONS = frozenset(
    {TemporalPrecision.HOUR, TemporalPrecision.MINUTE, TemporalPrecision.SECOND}
)


def validate_source_timezone(value: str) -> None:
    """Refuse anything that is not UTC, a numeric UTC offset, or an IANA zone.

    A timezone is trusted metadata, not free text: it is either one of the three
    forms that carry a defined offset rule or it does not go into the record.
    The rejected value is never echoed -- it arrived with untrusted source data.
    """
    if not isinstance(value, str) or value == "":
        raise TemporalValidationError(
            SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE,
            "source_timezone must be a non-empty string",
        )
    if len(value) > MAX_SOURCE_TIMEZONE_CHARS:
        raise TemporalValidationError(
            SemanticErrorCode.INVALID_FIELD,
            f"source_timezone exceeds {MAX_SOURCE_TIMEZONE_CHARS} characters",
        )
    if value in _UTC_SPELLINGS or _NUMERIC_UTC_OFFSET.match(value):
        return
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise TemporalValidationError(
            SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE,
            "source_timezone is not UTC, a numeric UTC offset or an IANA timezone",
        ) from error


def _timezone_from_metadata(value: str) -> timezone | ZoneInfo:
    """Resolve already-validated timezone metadata without guessing."""
    if value in _UTC_SPELLINGS:
        return UTC
    if _NUMERIC_UTC_OFFSET.fullmatch(value):
        sign = -1 if value.startswith("-") else 1
        digits = value[1:].replace(":", "")
        hours = int(digits[:2])
        minutes = int(digits[2:]) if len(digits) == 4 else 0
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    return ZoneInfo(value)


def _explicit_timezone_metadata(source_text: str, parsed: datetime) -> str:
    """Return the trusted wire metadata contributed by an explicit offset."""
    if source_text.endswith(("Z", "z")):
        return "UTC"
    offset = parsed.utcoffset()
    if offset is None:  # pragma: no cover - guarded by the caller
        raise TemporalValidationError(
            SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE,
            "source timezone could not be resolved",
        )
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _truncate(value: datetime, precision: TemporalPrecision) -> datetime:
    if precision is TemporalPrecision.YEAR:
        return value.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    if precision is TemporalPrecision.MONTH:
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if precision is TemporalPrecision.DAY:
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if precision is TemporalPrecision.HOUR:
        return value.replace(minute=0, second=0, microsecond=0)
    if precision is TemporalPrecision.MINUTE:
        return value.replace(second=0, microsecond=0)
    if precision is TemporalPrecision.SECOND:
        return value.replace(microsecond=0)
    raise TemporalValidationError(
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"unsupported temporal precision: {precision!r}",
    )


def canonical_utc(value: datetime, precision: TemporalPrecision) -> datetime:
    """Normalize an aware `datetime` to UTC and truncate it to `precision`."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalValidationError(
            SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE,
            "temporal value must be timezone-aware",
        )
    return _truncate(value.astimezone(UTC), precision)


@dataclass(frozen=True)
class TemporalInstant:
    """One canonical UTC point in time at a declared precision and provenance.

    `original_source_text` and `source_timezone` are auxiliary *source* metadata:
    what the source actually said, and the timezone rule that was trusted to place
    it on the UTC line. Both stay optional, because a system-created or manually
    constructed instant is derived from no source string at all; `parse_source_time`
    is the boundary that always records them. `source_timezone` is null whenever no
    timezone was trusted -- it is never a guess, and never a restatement of `value`
    already being UTC.
    """

    value: datetime
    precision: TemporalPrecision
    provenance: TemporalProvenance
    original_source_text: str | None = None
    source_timezone: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.precision, TemporalPrecision):
            raise TemporalValidationError(
                SemanticErrorCode.UNSUPPORTED_VALUE,
                "TemporalInstant.precision must be a TemporalPrecision",
            )
        if not isinstance(self.provenance, TemporalProvenance):
            raise TemporalValidationError(
                SemanticErrorCode.UNSUPPORTED_VALUE,
                "TemporalInstant.provenance must be a TemporalProvenance",
            )
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise TemporalValidationError(
                SemanticErrorCode.TEMPORAL_TIMEZONE_INDETERMINATE,
                "TemporalInstant.value must be timezone-aware",
            )
        if self.value.utcoffset() != timedelta(0):
            raise TemporalValidationError(
                SemanticErrorCode.INVALID_FIELD,
                "TemporalInstant.value must be in UTC",
            )
        canonical = _truncate(self.value, self.precision)
        if canonical != self.value:
            raise TemporalValidationError(
                SemanticErrorCode.INVALID_FIELD,
                "TemporalInstant.value must already be truncated to its precision",
            )
        if self.original_source_text is not None:
            if not isinstance(self.original_source_text, str):
                raise TemporalValidationError(
                    SemanticErrorCode.INVALID_FIELD,
                    "TemporalInstant.original_source_text must be a string",
                )
            if len(self.original_source_text) > MAX_ORIGINAL_SOURCE_TEXT_CHARS:
                raise TemporalValidationError(
                    SemanticErrorCode.INVALID_FIELD,
                    "TemporalInstant.original_source_text exceeds "
                    f"{MAX_ORIGINAL_SOURCE_TEXT_CHARS} characters",
                )
            if self.original_source_text == "":
                raise TemporalValidationError(
                    SemanticErrorCode.INVALID_FIELD,
                    "TemporalInstant.original_source_text must not be empty",
                )
        if self.source_timezone is not None:
            if self.original_source_text is None:
                raise TemporalValidationError(
                    SemanticErrorCode.INVALID_FIELD,
                    "TemporalInstant.source_timezone requires original_source_text",
                )
            validate_source_timezone(self.source_timezone)


@dataclass(frozen=True)
class EffectiveValidInterval:
    """A half-open `[effective_from, effective_to)` interval."""

    effective_from: TemporalInstant
    effective_to: TemporalInstant | None
    end_state: EndBoundaryState
    half_open: bool = field(init=False, default=True)
    contract_version: str = field(init=False, default=TEMPORAL_CONTRACT_VERSION)

    def __post_init__(self) -> None:
        if self.end_state is EndBoundaryState.OPEN:
            if self.effective_to is not None:
                raise TemporalValidationError(
                    SemanticErrorCode.TEMPORAL_INTERVAL_INVALID,
                    "an open-ended interval must not carry a stated effective_to",
                )
            return
        if self.effective_to is None:
            raise TemporalValidationError(
                SemanticErrorCode.TEMPORAL_END_INDETERMINATE,
                f"end_state {self.end_state.value!r} requires an effective_to",
            )
        if self.effective_from.value >= self.effective_to.value:
            raise TemporalValidationError(
                SemanticErrorCode.TEMPORAL_INTERVAL_INVALID,
                "effective_from must be strictly before effective_to",
            )


def parse_source_time(
    source_text: str,
    precision: TemporalPrecision,
    trusted_source_timezone: str | None = None,
    provenance: TemporalProvenance = TemporalProvenance.STATED,
) -> TemporalInstant:
    """Parse ISO clock text into a canonical UTC `TemporalInstant`.

    Timezone resolution order (decision record section 4): (1) an explicit
    offset/`Z` in `source_text`, (2) a recorded trusted timezone, (3) for a
    timezone-less value, preserve the source calendar date while reducing any
    requested sub-day precision to day. The reduced value is not a claim about the
    discarded clock component. `source_text` is preserved but never echoed in
    errors.

    This is the source-derivation boundary, so the returned instant always carries
    its `original_source_text`. Direct `TemporalInstant` construction does not.
    """
    if not isinstance(source_text, str) or source_text == "":
        raise TemporalValidationError(
            SemanticErrorCode.MISSING_FIELD,
            "source_text is required",
        )
    if len(source_text) > MAX_ORIGINAL_SOURCE_TEXT_CHARS:
        raise TemporalValidationError(
            SemanticErrorCode.INVALID_FIELD,
            f"source_text exceeds {MAX_ORIGINAL_SOURCE_TEXT_CHARS} characters",
        )
    try:
        parsed = datetime.fromisoformat(source_text)
    except ValueError as error:
        raise TemporalValidationError(
            SemanticErrorCode.INVALID_FIELD,
            "source_text is not a valid ISO 8601 datetime",
        ) from error

    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        source_timezone = _explicit_timezone_metadata(source_text, parsed)
    elif trusted_source_timezone is not None:
        validate_source_timezone(trusted_source_timezone)
        zone = _timezone_from_metadata(trusted_source_timezone)
        parsed = parsed.replace(tzinfo=zone)
        source_timezone = trusted_source_timezone
    else:
        parsed = parsed.replace(tzinfo=UTC)
        source_timezone = None
        if precision in _SUB_DAY_PRECISIONS:
            precision = TemporalPrecision.DAY

    try:
        canonical = canonical_utc(parsed, precision)
    except TemporalValidationError:
        raise
    except (OverflowError, ValueError) as error:
        raise TemporalValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "source_text could not be normalised to the requested precision",
        ) from error

    return TemporalInstant(
        value=canonical,
        precision=precision,
        provenance=provenance,
        original_source_text=source_text,
        source_timezone=source_timezone,
    )


def resolve_effective_valid_interval(
    *,
    valid_from: TemporalInstant | None,
    attested_from: TemporalInstant | None,
    end_state: EndBoundaryState,
    valid_to: TemporalInstant | None = None,
    attested_to: TemporalInstant | None = None,
) -> EffectiveValidInterval:
    """Resolve `effective_from`/`effective_to` per the B3 formula, failing closed."""
    effective_from = valid_from if valid_from is not None else attested_from
    if effective_from is None:
        raise TemporalValidationError(
            SemanticErrorCode.TEMPORAL_START_INDETERMINATE,
            "no stated or attested start is available",
        )

    if end_state is EndBoundaryState.OPEN:
        return EffectiveValidInterval(
            effective_from=effective_from,
            effective_to=None,
            end_state=end_state,
        )

    if end_state is EndBoundaryState.STATED:
        if valid_to is None:
            raise TemporalValidationError(
                SemanticErrorCode.TEMPORAL_END_INDETERMINATE,
                "end_state stated requires a valid_to",
            )
        effective_to = valid_to
    elif end_state is EndBoundaryState.UNKNOWN:
        if attested_to is None:
            raise TemporalValidationError(
                SemanticErrorCode.TEMPORAL_END_INDETERMINATE,
                "end_state unknown without attested_to fallback",
            )
        effective_to = attested_to
    else:
        raise TemporalValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"unsupported end_state: {end_state!r}",
        )

    return EffectiveValidInterval(
        effective_from=effective_from,
        effective_to=effective_to,
        end_state=end_state,
    )


def select_record_time(
    *,
    authorised_source_time: TemporalInstant | None,
    ingestion_time: TemporalInstant,
) -> TemporalInstant:
    """Prefer an authorised source time, falling back explicitly to ingestion time."""
    if authorised_source_time is not None:
        return authorised_source_time
    return TemporalInstant(
        value=ingestion_time.value,
        precision=ingestion_time.precision,
        provenance=TemporalProvenance.INGESTION_FALLBACK,
        original_source_text=ingestion_time.original_source_text,
        source_timezone=ingestion_time.source_timezone,
    )


def _rfc3339(instant: TemporalInstant) -> str:
    return instant.value.strftime("%Y-%m-%dT%H:%M:%SZ")


def instant_payload(instant: TemporalInstant) -> dict[str, Any]:
    """The canonical digest-bearing content of one instant.

    Auxiliary source metadata is bound into the digest only when it is present.
    Omitting the keys -- rather than emitting explicit nulls -- is what keeps the
    payload of an instant with no source metadata byte-identical to what it was
    before those fields were persisted, so digests recorded then still verify.
    """
    payload: dict[str, Any] = {
        "value": _rfc3339(instant),
        "precision": instant.precision.value,
        "provenance": instant.provenance.value,
    }
    if instant.original_source_text is not None:
        payload["original_source_text"] = instant.original_source_text
    if instant.source_timezone is not None:
        payload["source_timezone"] = instant.source_timezone
    return payload


def effective_interval_projection(interval: EffectiveValidInterval) -> dict[str, Any]:
    """A canonical, JSON-safe projection of `interval` for cross-surface parity."""
    return {
        "effective_from": _rfc3339(interval.effective_from),
        "effective_from_precision": interval.effective_from.precision.value,
        "effective_from_provenance": interval.effective_from.provenance.value,
        "effective_to": (
            "+Infinity"
            if interval.end_state is EndBoundaryState.OPEN
            else _rfc3339(interval.effective_to)  # type: ignore[arg-type]
        ),
        "effective_to_precision": (
            None
            if interval.effective_to is None
            else interval.effective_to.precision.value
        ),
        "effective_to_provenance": (
            None
            if interval.effective_to is None
            else interval.effective_to.provenance.value
        ),
        "end_state": interval.end_state.value,
        "interval_type": "half_open",
        "contract_version": interval.contract_version,
    }


__all__ = [
    "MAX_ORIGINAL_SOURCE_TEXT_CHARS",
    "MAX_SOURCE_TIMEZONE_CHARS",
    "TEMPORAL_CONTRACT_VERSION",
    "EffectiveValidInterval",
    "EndBoundaryState",
    "TemporalInstant",
    "TemporalPrecision",
    "TemporalProvenance",
    "canonical_utc",
    "effective_interval_projection",
    "instant_payload",
    "parse_source_time",
    "resolve_effective_valid_interval",
    "select_record_time",
    "validate_source_timezone",
]
