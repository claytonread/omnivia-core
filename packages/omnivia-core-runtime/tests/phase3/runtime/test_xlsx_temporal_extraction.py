"""XLSX temporal decoding as extraction reports it, over in-memory packages.

The classifier and the serial helper are tested on their own elsewhere; what is
pinned here is the wiring between them -- which cells reach a temporal reading
at all, and what the record says once one does. A successful decode moves the
stored number to ``serial`` and reports the literal it names, an anomaly leaves
the number exactly where the numeric decode put it, and the exact text the file
wrote stays in ``raw`` either way.
"""

from __future__ import annotations

import io
import zipfile

from omnivia_core_runtime.service.ooxml_extractors import extract_xlsx

CAPTURE = "dcap-" + "a" * 64
TRANSITIONAL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STYLES_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
)
STYLES_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
# One cell-format table every case below cites by index, so a style index names
# a known format rather than a per-test one. Index 0 is General, which is what
# a cell writing no index resolves to.
GENERAL, DATE, TIME, DATETIME, ELAPSED, AMBIGUOUS = range(6)
STYLES = (
    "<numFmts>"
    '<numFmt numFmtId="164" formatCode="hh:mm:ss"/>'
    '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm:ss"/>'
    # An elapsed hour count, and the one ambiguous field on its own: neither is
    # a point in time, so neither reaches a serial decode.
    '<numFmt numFmtId="166" formatCode="[h]:mm"/>'
    '<numFmt numFmtId="167" formatCode="m"/>'
    "</numFmts>"
    "<cellXfs>"
    '<xf numFmtId="0"/>'
    '<xf numFmtId="14"/>'
    '<xf numFmtId="164"/>'
    '<xf numFmtId="165"/>'
    '<xf numFmtId="166"/>'
    '<xf numFmtId="167"/>'
    "</cellXfs>"
)
# The resolved provenance of each style index, which every record keeps
# untouched whatever the temporal reading turns out to be.
FORMATS = {
    GENERAL: {"numberFormatId": 0, "numberFormatCode": None},
    DATE: {"numberFormatId": 14, "numberFormatCode": None},
    TIME: {"numberFormatId": 164, "numberFormatCode": "hh:mm:ss"},
    DATETIME: {"numberFormatId": 165, "numberFormatCode": "yyyy-mm-dd hh:mm:ss"},
    ELAPSED: {"numberFormatId": 166, "numberFormatCode": "[h]:mm"},
    AMBIGUOUS: {"numberFormatId": 167, "numberFormatCode": "m"},
}
TEMPORAL_KEYS = ("serial", "decodedKind", "temporalDecodeAnomaly")


def xlsx(
    cells: str,
    *,
    system: str = "1900",
    namespace: str = TRANSITIONAL,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """Build one sheet over the shared styles part, in the given profile."""
    properties = (
        f'<workbookPr xmlns="{namespace}" date1904="true"/>' if system == "1904" else ""
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<Types><Override PartName="/xl/styles.xml" '
            f'ContentType="{STYLES_TYPE}"/></Types>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns:r="urn:r">{properties}<sheets>'
            '<sheet name="Budget" sheetId="7" r:id="rId1"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Target="styles.xml" '
            f'Type="{STYLES_RELATIONSHIP}"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
        )
        archive.writestr(
            "xl/styles.xml", f'<styleSheet xmlns="{namespace}">{STYLES}</styleSheet>'
        )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(cells: str, **kwargs: object) -> list[dict[str, object]]:
    content = xlsx(cells, **kwargs)  # type: ignore[arg-type]
    return [
        dict(item.structured_value or {})
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ]


def cell(
    stored: str, style: int, *, typed: str = "", system: str = "1900"
) -> dict[str, object]:
    """One ordinary cell storing ``stored`` under ``style``, as its record."""
    return records(f'<c r="A1" s="{style}"{typed}><v>{stored}</v></c>', system=system)[
        0
    ]


def expected(
    stored: str, style: int, *, system: str = "1900", **rest: object
) -> dict[str, object]:
    """The whole record a cell under ``style`` reports, bar its decoding."""
    return {
        "raw": stored,
        "cellType": None,
        "styleIndex": style,
        "dateSystem": system,
        **FORMATS[style],
        **rest,
    }


def test_builtin_date_decodes_the_days_the_two_readings_disagree_over() -> None:
    """Serials 59, 60 and 61 are the phantom day and the days either side.

    Serial 60 is the 29th of February 1900, which Excel counts and no calendar
    has: it keeps the literal Excel means and says so, rather than being moved
    onto a real day.
    """
    assert cell("59", DATE) == expected(
        "59", DATE, serial=59, decoded="1900-02-28", decodedKind="date"
    )
    assert cell("60", DATE) == expected(
        "60",
        DATE,
        serial=60,
        decoded="1900-02-29",
        decodedKind="date",
        temporalDecodeAnomaly="excel_1900_leap_day",
    )
    assert cell("61", DATE) == expected(
        "61", DATE, serial=61, decoded="1900-03-01", decodedKind="date"
    )


def test_the_1904_epoch_decodes_its_own_serial_zero() -> None:
    """The workbook's declared epoch is the one its serials are counted from."""
    assert cell("0", DATE, system="1904") == expected(
        "0",
        DATE,
        system="1904",
        serial=0,
        decoded="1904-01-01",
        decodedKind="date",
    )


def test_custom_clock_and_datetime_formats_decode_their_fractions() -> None:
    """A declared code decides on its own, fraction of a day and all."""
    assert cell("0.5242592592592593", TIME) == expected(
        "0.5242592592592593",
        TIME,
        serial=0.5242592592592593,
        decoded="12:34:56",
        decodedKind="time",
    )
    assert cell("61.5", DATETIME) == expected(
        "61.5",
        DATETIME,
        serial=61.5,
        decoded="1900-03-01T12:00:00",
        decodedKind="datetime",
    )


def test_an_anomalous_serial_keeps_the_number_the_numeric_decode_read() -> None:
    """A serial no calendar names decodes to nothing, so the number stands.

    The anomaly is added beside it rather than replacing it: there is no date
    to report, and the file still stored a number.
    """
    assert cell("-1", DATE) == expected(
        "-1", DATE, decoded=-1, temporalDecodeAnomaly="negative"
    )
    assert cell("2958466", DATE) == expected(
        "2958466", DATE, decoded=2958466, temporalDecodeAnomaly="out_of_range"
    )
    # Within the 1900 serial space, past the 1904 one.
    assert cell("2958465", DATE, system="1904") == expected(
        "2958465",
        DATE,
        system="1904",
        decoded=2958465,
        temporalDecodeAnomaly="out_of_range",
    )


def test_formats_that_name_no_point_in_time_stay_numeric() -> None:
    """An elapsed count and an ambiguous field are not dates, and not guessed.

    ``[h]`` is a count of hours rather than an hour of a day, and a lone ``m``
    is a month in a date and a minute in a clock with nothing to say which, so
    neither reaches a decode at all.
    """
    for style in (ELAPSED, AMBIGUOUS, GENERAL):
        assert cell("45000.5", style) == expected("45000.5", style, decoded=45000.5)


def test_values_that_are_not_stored_numbers_are_untouched_under_a_date() -> None:
    """The format says to read a serial; these cells stored none to read."""
    cells = (
        f'<c r="A1" s="{DATE}" t="s"><v>0</v></c>'
        f'<c r="B1" s="{DATE}" t="inlineStr"><is><t>61</t></is></c>'
        f'<c r="C1" s="{DATE}" t="b"><v>1</v></c>'
        f'<c r="D1" s="{DATE}" t="e"><v>#REF!</v></c>'
        f'<c r="E1" s="{DATE}" t="str"><v>61</v></c>'
        f'<c r="F1" s="{DATE}"><v>abc</v></c>'
        f'<c r="G1" s="{DATE}"><v>INF</v></c>'
        f'<c r="H1" s="{DATE}"><v/></c>'
    )
    shared = {"xl/sharedStrings.xml": b"<sst><si><t>61</t></si></sst>"}

    values = records(cells, extra=shared)

    assert [value["decoded"] for value in values] == [
        "61",
        "61",
        True,
        "#REF!",
        "61",
        None,
        None,
        "",
    ]
    assert [value.get("numericAnomaly") for value in values] == (
        [None] * 5 + ["malformed", "nonfinite", None]
    )
    assert not [value for value in values if any(key in value for key in TEMPORAL_KEYS)]


def test_a_formula_cache_decodes_and_leaves_the_expression_alone() -> None:
    """The saved number is a stored serial; the expression is not a number."""
    assert records(f'<c r="A1" s="{DATE}"><f>TODAY()</f><v>61</v></c>') == [
        {
            "expression": "TODAY()",
            "kind": "ordinary",
            "sharedIndex": None,
            "ref": None,
            "cacheState": "present",
            "cacheFreshness": "unknown",
            "styleIndex": DATE,
            "dateSystem": "1900",
            **FORMATS[DATE],
        },
        {
            "raw": "61",
            "cellType": None,
            "error": False,
            "freshness": "unknown",
            "serial": 61,
            "decoded": "1900-03-01",
            "decodedKind": "date",
            "styleIndex": DATE,
            "dateSystem": "1900",
            **FORMATS[DATE],
        },
    ]


def test_an_anomalous_formula_cache_keeps_its_number_and_its_freshness() -> None:
    assert records(f'<c r="A1" s="{DATE}"><f>A2-1</f><v>-1</v></c>')[1] == {
        "raw": "-1",
        "cellType": None,
        "error": False,
        "freshness": "unknown",
        "decoded": -1,
        "temporalDecodeAnomaly": "negative",
        "styleIndex": DATE,
        "dateSystem": "1900",
        **FORMATS[DATE],
    }


def test_surrounding_whitespace_is_read_but_never_rewritten() -> None:
    """The decode is a reading of the authored text, never a replacement."""
    assert cell(" 61 ", DATE) == expected(
        " 61 ", DATE, serial=61, decoded="1900-03-01", decodedKind="date"
    )


def test_strict_namespaces_decode_the_same_as_transitional() -> None:
    """Both profiles are the same declaration, so neither reads differently."""
    cells = (
        f'<c r="A1" s="{DATE}"><v>61</v></c>'
        f'<c r="B1" s="{DATETIME}"><v>61.5</v></c>'
        f'<c r="C1" s="{TIME}"><v>0.5</v></c>'
        f'<c r="D1" s="{ELAPSED}"><v>61.5</v></c>'
        f'<c r="E1" s="{DATE}"><f>TODAY()</f><v>60</v></c>'
    )

    assert records(cells, system="1904", namespace=STRICT) == records(
        cells, system="1904"
    )
