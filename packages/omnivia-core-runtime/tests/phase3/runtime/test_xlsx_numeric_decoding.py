"""XLSX stored-number decoding over minimal in-memory packages.

Only the number a cell stored is read here: no number format is applied, no
serial becomes a date, and nothing is evaluated. Every case pins the exact
lexical text the file wrote alongside whatever was decoded from it, because the
decode is a reading of that text and never a replacement for it.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from omnivia_core_runtime.service.ooxml_extractors import extract_xlsx

CAPTURE = "dcap-" + "a" * 64
# These packages declare no styles part and no date system, so every cell keeps
# the provenance a cell without a style index has.
DEFAULT_FORMAT = {
    "styleIndex": None,
    "numberFormatId": 0,
    "numberFormatCode": None,
    "dateSystem": "1900",
}
WORKBOOK = (
    b'<workbook xmlns:r="urn:r"><sheets>'
    b'<sheet name="Budget" sheetId="7" r:id="rId1"/>'
    b"</sheets></workbook>"
)
RELS = (
    b"<Relationships>"
    b'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    b"</Relationships>"
)


def xlsx(cells: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
        )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(cells: str, *, extra: dict[str, bytes] | None = None) -> list[Any]:
    result = extract_xlsx(xlsx(cells, extra=extra), capture_id=CAPTURE)
    return [item.structured_value for item in result.segments]


def read(stored: str, *, typed: str = "") -> tuple[object, object, object]:
    """Decode one ordinary cell holding ``stored``, as raw, decoded, anomaly."""
    value = records(f'<c r="A1"{typed}><v>{stored}</v></c>')[0]
    return value["raw"], value["decoded"], value.get("numericAnomaly")


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("0", 0),
        ("5", 5),
        ("-12", -12),
        ("+12", 12),
        ("-12.75", -12.75),
        ("+3.5", 3.5),
        # A decimal may omit either side of the point.
        (".5", 0.5),
        ("-.5", -0.5),
        ("5.", 5.0),
        ("1e3", 1000.0),
        ("1.5E-3", 0.0015),
        ("-2.5e+2", -250.0),
        # An exponent form of an integral value is still the double it spells,
        # not the integer the digits would have decoded to.
        ("2e0", 2.0),
        # Surrounding whitespace is not part of the number, and not part of
        # what makes one malformed either.
        (" 5 ", 5),
    ],
)
def test_stored_numbers_decode_to_the_value_the_file_wrote(
    stored: str, expected: object
) -> None:
    """Both numeric spellings decode alike, and neither rewrites the text."""
    for typed in ("", ' t="n"'):
        raw, decoded, anomaly = read(stored, typed=typed)

        assert raw == stored
        assert anomaly is None
        assert (decoded, type(decoded)) == (expected, type(expected))


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # The largest magnitude that still round-trips stays an exact integer.
        ("9007199254740992", 9007199254740992),
        ("-9007199254740992", -9007199254740992),
        # One past it does not, so it is the double the file stored instead.
        ("9007199254740993", 9007199254740992.0),
        ("-9007199254740993", -9007199254740992.0),
        ("100000000000000000000", 1e20),
    ],
)
def test_integers_past_the_representable_range_decode_as_doubles(
    stored: str, expected: object
) -> None:
    raw, decoded, anomaly = read(stored)

    assert (raw, anomaly) == (stored, None)
    assert (decoded, type(decoded)) == (expected, type(expected))


@pytest.mark.parametrize(
    "stored",
    [
        "abc",
        "1,5",
        "1 2",
        ".",
        "e5",
        "1e",
        "1e+",
        "--1",
        "1.2.3",
        "0x10",
        # Spellings of the non-finite values that this lexical space does not
        # have: the type is case-sensitive and names no ``Infinity``.
        "Infinity",
        "nan",
        "inf",
    ],
)
def test_malformed_numeric_text_decodes_nothing(stored: str) -> None:
    """Text that is not a number is an anomaly, never a decoded number."""
    assert read(stored) == (stored, None, "malformed")


def test_a_long_run_of_digits_ending_in_text_is_refused_in_one_pass() -> None:
    """The refusal costs one pass over the token, not one per digit.

    A hundred thousand digits followed by a character the lexical space does not
    have is the shape that an ambiguous integral form retries once per split of
    its digits; the test is here to hold the admission to a single pass. It
    asserts the classification rather than a clock, because the cost is a
    property of the expression and not of the machine it runs on: a reading that
    retries per split does not finish this call at all.
    """
    stored = "9" * 100_000 + "x"

    assert read(stored) == (stored, None, "malformed")


@pytest.mark.parametrize("stored", ["INF", "+INF", "-INF", "NaN"])
def test_nonfinite_spellings_are_reported_as_nonfinite(stored: str) -> None:
    """The file wrote a double; it wrote one no consumer can hold."""
    assert read(stored) == (stored, None, "nonfinite")


@pytest.mark.parametrize(
    "stored",
    [
        "1e10000",
        "-1e10000",
        "1E99999999",
        # A lexically valid literal far past any digit count a double needs.
        "1" + "0" * 5_000,
    ],
)
def test_a_magnitude_no_double_can_hold_is_out_of_range_not_malformed(
    stored: str,
) -> None:
    assert read(stored) == (stored, None, "nonfinite")


def test_an_exponent_below_the_smallest_double_decodes_to_the_zero_stored() -> None:
    """Underflow lands on a finite double, so it is a value and not an anomaly."""
    assert read("1e-10000") == ("1e-10000", 0.0, None)


@pytest.mark.parametrize("stored", ["", " "])
def test_a_blank_value_stored_no_number_to_decode(stored: str) -> None:
    """``<v/>`` is a blank, not a number that failed to parse."""
    assert read(stored) == (stored, stored, None)


def test_formula_cache_decodes_the_saved_number_and_leaves_the_formula_alone() -> None:
    assert records('<c r="A1"><f>1+1</f><v>7.5</v></c>') == [
        {
            "expression": "1+1",
            "kind": "ordinary",
            "sharedIndex": None,
            "ref": None,
            "cacheState": "present",
            "cacheFreshness": "unknown",
            **DEFAULT_FORMAT,
        },
        {
            "raw": "7.5",
            "cellType": None,
            "error": False,
            "freshness": "unknown",
            "decoded": 7.5,
            **DEFAULT_FORMAT,
        },
    ]


def test_formula_cache_of_text_no_double_holds_records_only_the_anomaly() -> None:
    """A cache record has no lexical value to blank, so none is invented."""
    assert records('<c r="A1"><f>A2</f><v>abc</v></c>')[1] == {
        "raw": "abc",
        "cellType": None,
        "error": False,
        "freshness": "unknown",
        "numericAnomaly": "malformed",
        **DEFAULT_FORMAT,
    }


def test_nonnumeric_cells_keep_the_values_they_already_reported() -> None:
    """Every other cell kind is left alone, including one spelling a number."""
    cells = (
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="inlineStr"><is><t>5</t></is></c>'
        '<c r="C1" t="b"><v>1</v></c>'
        '<c r="D1" t="e"><v>#REF!</v></c>'
        '<c r="E1" t="str"><v>5</v></c>'
    )

    shared = {"xl/sharedStrings.xml": b"<sst><si><t>5</t></si></sst>"}
    values = records(cells, extra=shared)

    assert [(value["decoded"], type(value["decoded"])) for value in values] == [
        ("5", str),
        ("5", str),
        (True, bool),
        ("#REF!", str),
        ("5", str),
    ]
    assert not [value for value in values if "numericAnomaly" in value]
