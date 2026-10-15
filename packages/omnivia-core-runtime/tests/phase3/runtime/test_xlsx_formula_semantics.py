"""XLSX formula and cached-value semantics over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
# The cell-format provenance every record below carries: these packages declare
# no styles part and no date system, so each cell authors no style index, keeps
# the General format a cell without one has, and counts from the 1900 epoch.
# Spelled once because the formula, saved-cache and ordinary records of one cell
# all have to say the same thing about the format it authored.
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
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
        )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def values(cells: str) -> list[tuple[str, object]]:
    result = extract_xlsx(xlsx(cells), capture_id=CAPTURE)
    return [(item.semantic_role, item.structured_value) for item in result.segments]


def test_expression_and_saved_cache_stay_separate_and_uncalculated() -> None:
    """The cache is reported beside the expression, never checked against it."""
    assert values('<c r="A1"><f>1+1</f><v>7</v></c>') == [
        (
            "formula",
            {
                "expression": "1+1",
                "kind": "ordinary",
                "sharedIndex": None,
                "ref": None,
                "cacheState": "present",
                "cacheFreshness": "unknown",
                **DEFAULT_FORMAT,
            },
        ),
        (
            "saved_value",
            {
                "raw": "7",
                "cellType": None,
                "error": False,
                "freshness": "unknown",
                "decoded": 7,
                **DEFAULT_FORMAT,
            },
        ),
    ]


def test_formula_kinds_carry_their_index_and_range() -> None:
    cells = (
        '<c r="A1"><f t="array" ref="A1:A3">TRANSPOSE(C1:C3)</f><v>1</v></c>'
        '<c r="B1"><f t="shared" si="4" ref="B1:B2">SUM(C1:C2)</f><v>2</v></c>'
        '<c r="B2"><f t="shared" si="4"/><v>3</v></c>'
        '<c r="C1"><f t="dataTable" ref="C1:C2" dt2D="0" r1="A1"/><v>4</v></c>'
    )

    assert [
        (value["kind"], value["sharedIndex"], value["ref"], value["expression"])
        for role, value in values(cells)
        if role == "formula"
    ] == [
        ("array", None, "A1:A3", "TRANSPOSE(C1:C3)"),
        ("shared", "4", "B1:B2", "SUM(C1:C2)"),
        # A follower's own expression is empty, and translating the master's
        # references into this cell would be a calculation.
        ("shared", "4", None, ""),
        ("dataTable", None, "C1:C2", ""),
    ]


def test_absent_cache_is_recorded_as_missing_without_a_saved_value() -> None:
    assert values('<c r="A1"><f>NOW()</f></c>') == [
        (
            "formula",
            {
                "expression": "NOW()",
                "kind": "ordinary",
                "sharedIndex": None,
                "ref": None,
                "cacheState": "missing",
                "cacheFreshness": "unknown",
                **DEFAULT_FORMAT,
            },
        ),
    ]


def test_empty_cache_element_is_present_and_not_missing() -> None:
    """``<v/>`` is a saved cache of the empty string, not an absent one."""
    roles = values('<c r="A1"><f>A2</f><v/></c>')

    assert roles[0][1]["cacheState"] == "present"
    assert roles[1] == (
        "saved_value",
        {
            "raw": "",
            "cellType": None,
            "error": False,
            "freshness": "unknown",
            **DEFAULT_FORMAT,
        },
    )


def test_error_typed_cache_is_preserved_as_an_error_not_as_text() -> None:
    assert values('<c r="A1" t="e"><f>1/0</f><v>#DIV/0!</v></c>')[1] == (
        "saved_value",
        {
            "raw": "#DIV/0!",
            "cellType": "e",
            "error": True,
            "freshness": "unknown",
            **DEFAULT_FORMAT,
        },
    )


def test_shared_string_cache_is_not_decoded_against_the_string_table() -> None:
    """A formula's cache is kept raw; only a plain cell resolves its index."""
    package = xlsx(
        '<c r="A1" t="s"><f>B1</f><v>0</v></c><c r="B1" t="s"><v>0</v></c>',
        extra={
            "xl/sharedStrings.xml": b"<sst><si><t>Ada</t></si></sst>",
        },
    )

    segments = extract_xlsx(package, capture_id=CAPTURE).segments

    assert segments[1].structured_value["raw"] == "0"
    assert "decoded" not in segments[1].structured_value
    assert segments[2].structured_value["decoded"] == "Ada"


def test_orphan_shared_follower_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="no master"):
        values('<c r="A1"><f t="shared" si="9"/><v>1</v></c>')


def test_shared_master_without_an_index_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="no master"):
        values('<c r="A1"><f t="shared" ref="A1:A2">SUM(B1:B2)</f><v>1</v></c>')


def test_duplicate_shared_masters_are_refused() -> None:
    cells = (
        '<c r="A1"><f t="shared" si="1" ref="A1:A2">SUM(B1:B2)</f><v>1</v></c>'
        '<c r="A2"><f t="shared" si="1" ref="A2:A3">SUM(C1:C2)</f><v>2</v></c>'
    )

    with pytest.raises(OOXMLInvalid, match="more than one master"):
        values(cells)


def test_unknown_formula_type_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="unknown type"):
        values('<c r="A1"><f t="macro">RUN()</f><v>1</v></c>')
