"""Decoding of an XLSX date serial into the local literal it names.

The helper under test is called directly: it is reached only once a cell's
stored text is known to be a finite number and its format is known to read as a
time, so nothing here builds a package, applies a format or emits a record.
Every case pins the exact literal a serial decodes to, because an off-by-one
day here would misdate every cell under the format that asked for it -- which
is what the 1900 system's phantom 29th of February 1900 is: a day Excel counts
and no calendar has, reported as the anomaly it is rather than quietly moved
onto a real one.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import _xlsx_temporal_serial

# The last whole serial each system can name a date for. The 1900 figure is the
# one OOXML names as the end of the serial space, written out here so the
# derivation behind the constant is pinned to it rather than only to itself.
MAX_1900 = 2958465
MAX_1904 = 2957003


def decode(
    raw: str, kind: str, system: str = "1900", *, serial: float | None = None
) -> dict[str, object]:
    """Decode ``raw`` as the caller would, numeric validation already done."""
    return _xlsx_temporal_serial(
        raw, float(raw) if serial is None else serial, kind, system
    )


def test_the_derived_bound_is_the_serial_space_ooxml_names() -> None:
    """The 1900 bound is derived, and lands on the documented last serial."""
    assert ooxml_extractors._XLSX_MAX_SERIAL == {"1900": MAX_1900, "1904": MAX_1904}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "1899-12-31"),  # the day before serial 1, which Excel calls 0 January
        ("1", "1900-01-01"),
        ("59", "1900-02-28"),  # the last day the two readings agree on
        ("61", "1900-03-01"),  # the first day past the phantom
        ("367", "1901-01-01"),
        (str(MAX_1900), "9999-12-31"),
    ],
)
def test_1900_serials_decode_to_their_dates(raw: str, expected: str) -> None:
    """Each 1900 serial names the day Excel displays for it."""
    assert decode(raw, "date") == {
        "serial": float(raw),
        "decoded": expected,
        "decodedKind": "date",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "1904-01-01"),  # the 1904 epoch is its own serial zero
        ("1", "1904-01-02"),
        ("59", "1904-02-29"),  # a real leap day, counted by a real leap year
        ("60", "1904-03-01"),  # no phantom here, so no shift and no anomaly
        (str(MAX_1904), "9999-12-31"),
    ],
)
def test_1904_serials_decode_to_their_dates(raw: str, expected: str) -> None:
    """The 1904 system counts from its epoch with no phantom day to absorb."""
    assert decode(raw, "date", "1904") == {
        "serial": float(raw),
        "decoded": expected,
        "decodedKind": "date",
    }


def test_the_phantom_day_is_reported_as_itself() -> None:
    """Serial 60 keeps the date Excel means and says it is not a real one.

    Coercing it to the 28th of February or the 1st of March would silently move
    every value stored under it, so the literal is kept and the anomaly carries
    the warning.
    """
    assert decode("60", "date") == {
        "serial": 60.0,
        "decoded": "1900-02-29",
        "decodedKind": "date",
        "temporalDecodeAnomaly": "excel_1900_leap_day",
    }


def test_the_phantom_day_carries_its_time_of_day() -> None:
    """A datetime on the phantom serial still decodes its clock."""
    assert decode("60.5", "datetime") == {
        "serial": 60.5,
        "decoded": "1900-02-29T12:00:00",
        "decodedKind": "datetime",
        "temporalDecodeAnomaly": "excel_1900_leap_day",
    }


def test_the_phantom_serial_is_only_a_date_anomaly() -> None:
    """Read as a clock, serial 60 claims no day and so flags nothing."""
    assert decode("60.5", "time") == {
        "serial": 60.5,
        "decoded": "12:00:00",
        "decodedKind": "time",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "00:00:00"),
        ("0.5", "12:00:00"),
        ("0.75", "18:00:00"),
        ("0.0003125", "00:00:27"),
        # The double a writer stores for 12:34:56 is never the fraction that
        # time is, so rounding to the microsecond is what reports it as itself.
        ("0.5242592592592593", "12:34:56"),
        # Whole days are not a time of day, so the fraction is read modulo one.
        ("1.5", "12:00:00"),
        (str(MAX_1900), "00:00:00"),
    ],
)
def test_a_clock_reads_the_fraction_of_a_day(raw: str, expected: str) -> None:
    """A time names an hour of a day and never the day itself."""
    assert decode(raw, "time") == {
        "serial": float(raw),
        "decoded": expected,
        "decodedKind": "time",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.00000000015625", "00:00:00.000014"),  # 13.5 microseconds, up to even
        ("0.00000000046875", "00:00:00.000040"),  # 40.5 microseconds, down to even
        ("0.000011574074074074073", "00:00:01"),
    ],
)
def test_fractional_seconds_round_half_to_even(raw: str, expected: str) -> None:
    """The clock is the nearest microsecond, with exact halves going to even.

    The fraction is read from the authored text rather than the double, so an
    exact half really is exact here and the tie rule is the one that decides
    it. Microseconds are written only when there are any.
    """
    assert decode(raw, "time")["decoded"] == expected


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        ("61.5", "datetime", "1900-03-01T12:00:00"),
        ("61.00000000015625", "datetime", "1900-03-01T00:00:00.000014"),
        ("61", "datetime", "1900-03-01T00:00:00"),
        ("0.5", "datetime", "1899-12-31T12:00:00"),
    ],
)
def test_a_datetime_joins_its_day_and_its_clock(
    raw: str, kind: str, expected: str
) -> None:
    """A datetime is the local ISO pair, with a ``T`` and no timezone."""
    decoded = decode(raw, kind)["decoded"]
    assert decoded == expected
    assert isinstance(decoded, str)
    assert not decoded.endswith("Z")


@pytest.mark.parametrize("raw", ["61.75", "61.999999999999", "61.0000001"])
def test_a_date_ignores_the_time_of_day(raw: str) -> None:
    """A date names the day the serial stores, whatever fraction it carries.

    Including a fraction that rounds up to a whole day: a date format displays
    the day its serial is on, not the one after it.
    """
    assert decode(raw, "date")["decoded"] == "1900-03-01"


def test_a_clock_rounding_up_to_a_whole_day_carries() -> None:
    """A fraction rounding past the last microsecond is the next midnight."""
    assert decode("61.999999999999", "datetime") == {
        "serial": 61.999999999999,
        "decoded": "1900-03-02T00:00:00",
        "decodedKind": "datetime",
    }
    assert decode("0.999999999999", "time")["decoded"] == "00:00:00"


def test_a_carry_into_the_phantom_day_still_reports_it() -> None:
    """Rounding onto serial 60 lands on the day Excel counts, anomaly and all."""
    assert decode("59.999999999999", "datetime") == {
        "serial": 59.999999999999,
        "decoded": "1900-02-29T00:00:00",
        "decodedKind": "datetime",
        "temporalDecodeAnomaly": "excel_1900_leap_day",
    }


@pytest.mark.parametrize("kind", ["date", "time", "datetime"])
@pytest.mark.parametrize("raw", ["-1", "-0.5", "-2958465"])
def test_a_negative_serial_decodes_to_nothing(raw: str, kind: str) -> None:
    """Nothing is written before an epoch, so the number is all there is.

    Only the anomaly comes back, which leaves the caller's numeric ``decoded``
    in place rather than replacing it with a date the system cannot name.
    """
    assert decode(raw, kind) == {"temporalDecodeAnomaly": "negative"}


@pytest.mark.parametrize(
    ("raw", "system"),
    [
        (str(MAX_1900 + 1), "1900"),  # one day past the calendar
        (str(MAX_1900 + 2), "1900"),  # past the cheap magnitude guard too
        ("3000000", "1900"),
        (str(MAX_1904 + 1), "1904"),
        (str(MAX_1900), "1904"),  # within the 1900 space, past the 1904 one
    ],
)
def test_a_serial_past_the_calendar_decodes_to_nothing(raw: str, system: str) -> None:
    """A magnitude the calendar cannot reach is reported, never wrapped."""
    assert decode(raw, "date", system) == {"temporalDecodeAnomaly": "out_of_range"}


@pytest.mark.parametrize("kind", ["date", "time", "datetime"])
def test_a_huge_serial_is_refused_before_any_arithmetic(kind: str) -> None:
    """The guard reads the number, so absurd text is never parsed or scaled.

    The raw text here names an exponent no calendar arithmetic could survive;
    the decoded magnitude beside it is what the refusal is made from, which is
    why this costs a comparison rather than a conversion.
    """
    assert decode("1e999999999", kind, serial=1e308) == {
        "temporalDecodeAnomaly": "out_of_range"
    }
    assert decode("-1e999999999", kind, serial=-1e308) == {
        "temporalDecodeAnomaly": "negative"
    }


def test_an_integer_serial_keeps_the_number_it_was_decoded_as() -> None:
    """``serial`` is the number the caller already decoded, not a re-reading."""
    assert decode("61", "date", serial=61) == {
        "serial": 61,
        "decoded": "1900-03-01",
        "decodedKind": "date",
    }
