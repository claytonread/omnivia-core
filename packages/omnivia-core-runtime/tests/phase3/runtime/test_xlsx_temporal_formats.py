"""Classification of XLSX number formats as date, time or elapsed readings.

The classifier under test is a reading of a format alone: nothing here decodes
a serial, applies a format or emits a record, so every case pins the kind a
format resolves to and nothing else. The cases that matter most are the ones
that resolve to nothing -- a literal ``d``, a colour directive, a bare ``mm``,
a locale-dependent implied id -- because reading one of those as temporal would
misdate every cell under it.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import _xlsx_temporal_kind

# Every implied format that is demonstrably temporal, with the kind it reads
# as, as OOXML spells them: mm-dd-yy, d-mmm-yy, d-mmm, mmm-yy, h:mm AM/PM,
# h:mm:ss AM/PM, h:mm, h:mm:ss, m/d/yy h:mm, mm:ss, [h]:mm:ss and mmss.0.
BUILTIN_TEMPORAL = {
    14: "date",
    15: "date",
    16: "date",
    17: "date",
    18: "time",
    19: "time",
    20: "time",
    21: "time",
    22: "datetime",
    45: "time",
    46: "elapsed_duration",
    47: "time",
}


@pytest.mark.parametrize(("format_id", "kind"), sorted(BUILTIN_TEMPORAL.items()))
def test_supported_builtin_formats_classify(format_id: int, kind: str) -> None:
    """Each implied date, time and elapsed format reads as the kind it is."""
    assert _xlsx_temporal_kind(format_id, None) == kind


@pytest.mark.parametrize(
    "format_id",
    [
        0,  # General
        1,  # 0
        2,  # 0.00
        3,  # #,##0
        9,  # 0%
        11,  # 0.00E+00
        12,  # # ?/?
        37,  # accounting
        40,  # accounting
        48,  # ##0.0E+0
        49,  # @
        27,  # reserved, locale-dependent
        30,
        36,
        50,
        58,
        164,  # the first custom id, declaring no code here
        9999,
    ],
)
def test_other_builtin_formats_are_not_temporal(format_id: int) -> None:
    """Numeric, text, locale-dependent and undeclared ids claim nothing.

    The reserved 27-36 and 50-58 block is the point: its format code is
    whatever the reading locale supplies, so a serial under one of those ids is
    not demonstrably a date and is not reported as one.
    """
    assert _xlsx_temporal_kind(format_id, None) is None


def test_declared_code_decides_over_the_implied_id() -> None:
    """A workbook that spells a code is read by that code, not by the table."""
    assert _xlsx_temporal_kind(14, "0.00") is None
    assert _xlsx_temporal_kind(0, "yyyy-mm-dd") == "date"


@pytest.mark.parametrize(
    "code",
    [
        "dd/mm/yyyy",
        "yyyy-mm-dd",
        "DD/MM/YYYY",  # format codes are case-insensitive
        "m/d/yy",
        "mmm-yy",
        "d-mmm",
        "mmmm d, yyyy",  # a month name needs no neighbour to disambiguate it
        "mmmmm",
        "dddd",  # a weekday name is still a date field
        "yy",
        "[$-409]mmmm d, yyyy",  # the locale directive is skipped, not poisonous
        "[$-411]yyyy/mm/dd",
        'dd/mm/yyyy;@',  # the text section formats no serial
        "dd\\.mm\\.yyyy",
        '"on "dd/mm/yyyy',
        "yyyy[Red]-mm-dd",
        # Only the first section governs a positive serial, and ``mmm`` is an
        # unambiguous month name, so the bare ``mm`` behind the semicolon is
        # never read.
        "mmm;mm",
    ],
)
def test_date_codes(code: str) -> None:
    """A code writing only date fields reads as a date."""
    assert _xlsx_temporal_kind(164, code) == "date"


@pytest.mark.parametrize(
    "code",
    [
        "h:mm",
        "hh:mm:ss",
        "h",
        "ss.000",
        "mm:ss",  # the minute is fixed by the second beside it
        "mmss.0",
        "h:mm AM/PM",
        "h:mm:ss am/pm",
        "h:mm A/P",
        "hh:mm:ss.00",
    ],
)
def test_time_codes(code: str) -> None:
    """A code writing only clock fields reads as a time."""
    assert _xlsx_temporal_kind(164, code) == "time"


@pytest.mark.parametrize(
    "code",
    [
        "m/d/yy h:mm",
        "yyyy-mm-dd hh:mm:ss",
        "dd/mm/yyyy h:mm AM/PM",
        'yyyy-mm-dd"T"hh:mm:ss',
    ],
)
def test_datetime_codes(code: str) -> None:
    """One code writing both a date field and a clock field reads as both.

    Each ``m`` here is settled by its own neighbours, so the month in the date
    and the minute in the clock are read as the different fields they are.
    """
    assert _xlsx_temporal_kind(164, code) == "datetime"


@pytest.mark.parametrize(
    "code",
    [
        '"day"0',
        '"Monday, May 1"',
        '0" days"',
        "\\d",
        "\\m\\y\\d0",
        '"m"mm',  # the only unquoted field is the ambiguous one
        "_d0",  # the character after an underscore is spacing, not a field
        "*d0",  # nor is the one after an asterisk fill
        "[Red]0.00",
        "[Blue]#,##0;[Red]-#,##0",
        "[>100]0;0",
        "[DBNum1]0",
        "[$-409]0.00",
        "[$€-407]#,##0.00",
        '[Red]"dmy"0',
    ],
)
def test_literal_and_directive_letters_are_not_fields(code: str) -> None:
    """Quoted, escaped, spaced and bracketed letters claim no date or time.

    ``[Red]`` is the case worth naming: it carries a ``d``, and a reader that
    looked inside a colour directive would read a serial formatted as a plain
    number as a day.
    """
    assert _xlsx_temporal_kind(164, code) is None


@pytest.mark.parametrize(
    "code",
    [
        "",
        "General",
        "general",
        "0.00",
        "#,##0.00_);(#,##0.00)",
        "0.00E+00",
        "# ?/?",
        "0%",
        "@",
        "Standard",  # an unrecognised letter leaves the code unaccounted for
        "mm",  # month or minute, with nothing beside it to say which
        "m",
        "mm;mmm",  # only the first section is read, and its own ``m`` is bare
        "mm:mm",
        "0;dd/mm/yyyy",  # the positive section formats the serial
        ";dd/mm/yyyy",
        '"unterminated dd',
        "[h",
        "yyyy[mm",
    ],
)
def test_ambiguous_and_non_temporal_codes(code: str) -> None:
    """Anything not demonstrably temporal, ambiguity included, claims nothing."""
    assert _xlsx_temporal_kind(164, code) is None


@pytest.mark.parametrize(
    "code",
    [
        "[h]:mm:ss",
        "[hh]:mm",
        "[mm]:ss",
        "[s]",
        "[ss].0",
        "[H]:MM",
        "[h]:mm:ss;@",
    ],
)
def test_elapsed_durations(code: str) -> None:
    """A bracketed unit is a count of that unit, never a clock field."""
    assert _xlsx_temporal_kind(164, code) == "elapsed_duration"


@pytest.mark.parametrize("code", ["[h] dd/mm/yyyy", "dd/mm/yyyy [h]", "[mm] yyyy"])
def test_elapsed_outranks_the_fields_written_with_it(code: str) -> None:
    """An elapsed unit decides the whole code it appears in.

    A duration is not a point in time, so a code carrying one is never read as
    a date or a clock whatever else it writes -- including a code whose other
    fields would have been ambiguous on their own.
    """
    assert _xlsx_temporal_kind(164, code) == "elapsed_duration"


@pytest.mark.parametrize("inside", ["hm", "mh", "h:m", "hh ", "", "0"])
def test_mixed_brackets_are_not_elapsed_units(inside: str) -> None:
    """A bracket holding anything but one unit run is an ordinary directive."""
    assert _xlsx_temporal_kind(164, f"[{inside}]0") is None


def test_an_oversized_code_is_not_classified() -> None:
    """A code past the bound the styles part is read under is not parsed.

    The same text inside the bound classifies, so the refusal is the length and
    not the content.
    """
    bound = ooxml_extractors._MAX_XLSX_FORMAT_CODE_CHARS
    assert _xlsx_temporal_kind(164, "d" * bound) == "date"
    assert _xlsx_temporal_kind(164, "d" * (bound + 1)) is None
