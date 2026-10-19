"""XLSX number-format and date-system provenance over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
TRANSITIONAL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STYLES_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
)
STYLES_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
PROVENANCE = ("styleIndex", "numberFormatId", "numberFormatCode", "dateSystem")
# One custom format, cited by the second of two declared cell formats, so a
# resolved style index is distinguishable from the 0 an absent one defaults to.
CUSTOM_STYLES = (
    '<numFmts><numFmt numFmtId="164" formatCode="dd/mm/yyyy;@"/></numFmts>'
    '<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>'
)


def xlsx(
    cells: str,
    *,
    properties: str = "",
    styles: str | None = None,
    namespace: str = TRANSITIONAL,
    styles_type: str = STYLES_TYPE,
    root: str = "styleSheet",
    extra_related: str = "",
) -> bytes:
    """Build one sheet, optionally with a qualifying styles part.

    The styles part is declared with its own content type and reached by the
    workbook's own qualifying relationship, which is the only route the reader
    takes to it; ``styles=None`` leaves the package without one entirely.

    ``styles_type``, ``root`` and ``extra_related`` vary that route: what the
    package declares the part to be, what the part roots itself at, and which
    further relationships the workbook names beside the qualifying one.
    """
    declared = (
        f'<Override PartName="/xl/styles.xml" ContentType="{styles_type}"/>'
        if styles is not None
        else ""
    )
    related = (
        f'<Relationship Id="rId2" Target="styles.xml" Type="{STYLES_RELATIONSHIP}"/>'
        if styles is not None
        else ""
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{declared}</Types>")
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
            f"{related}{extra_related}</Relationships>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
        )
        if styles is not None:
            archive.writestr(
                "xl/styles.xml",
                f'<{root} xmlns="{namespace}">{styles}</{root}>',
            )
    return buffer.getvalue()


def formats(content: bytes) -> list[dict[str, object]]:
    """The format provenance of every segment, in order."""
    return [
        {key: item.structured_value[key] for key in PROVENANCE}
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ]


def test_cell_without_a_style_keeps_its_values_and_general_provenance() -> None:
    """The provenance is added beside the keys a cell already reported."""
    segments = extract_xlsx(xlsx('<c r="A1"><v>5</v></c>'), capture_id=CAPTURE).segments

    assert [item.semantic_role for item in segments] == ["table_cell"]
    assert segments[0].structured_value == {
        "raw": "5",
        "decoded": 5,
        "cellType": None,
        "styleIndex": None,
        "numberFormatId": 0,
        "numberFormatCode": None,
        "dateSystem": "1900",
    }


def test_custom_format_and_1904_epoch_reach_both_records_of_one_cell() -> None:
    """The formula and its saved cache describe one cell, so they agree."""
    content = xlsx(
        '<c r="A1" s="1"><f>TODAY()</f><v>45000</v></c>',
        properties=f'<workbookPr xmlns="{TRANSITIONAL}" date1904="true"/>',
        styles=CUSTOM_STYLES,
    )
    expected = {
        "styleIndex": 1,
        "numberFormatId": 164,
        "numberFormatCode": "dd/mm/yyyy;@",
        "dateSystem": "1904",
    }

    assert [
        item.semantic_role
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ] == ["formula", "saved_value"]
    assert formats(content) == [expected, expected]


def test_strict_namespaces_declare_the_same_provenance_as_transitional() -> None:
    """Both profiles are the same declaration, so neither reads differently."""
    cell = '<c r="A1" s="1"><v>45000</v></c>'

    assert formats(
        xlsx(
            cell,
            properties=f'<workbookPr xmlns="{STRICT}" date1904="true"/>',
            styles=CUSTOM_STYLES,
            namespace=STRICT,
        )
    ) == formats(
        xlsx(
            cell,
            properties=f'<workbookPr xmlns="{TRANSITIONAL}" date1904="true"/>',
            styles=CUSTOM_STYLES,
        )
    )


def test_builtin_format_id_carries_no_code_the_package_never_wrote() -> None:
    """A built-in's code is the consumer's locale table, not a declaration."""
    assert formats(
        xlsx(
            '<c r="A1" s="0"><v>45000</v></c>',
            styles='<cellXfs><xf numFmtId="14"/></cellXfs>',
        )
    ) == [
        {
            "styleIndex": 0,
            "numberFormatId": 14,
            "numberFormatCode": None,
            "dateSystem": "1900",
        }
    ]


@pytest.mark.parametrize(
    ("cells", "properties", "styles", "message"),
    [
        pytest.param(
            '<c r="A1"><v>1</v></c>',
            f'<workbookPr xmlns="{TRANSITIONAL}" date1904="yes"/>',
            None,
            "boolean attribute is malformed",
            id="malformed-date1904",
        ),
        pytest.param(
            '<c r="A1"><v>1</v></c>',
            f'<workbookPr xmlns="{TRANSITIONAL}"/><workbookPr xmlns="{STRICT}"/>',
            None,
            "properties more than once",
            id="duplicate-qualified-workbookPr",
        ),
        pytest.param(
            '<c r="A1" s="2"><v>1</v></c>',
            "",
            '<cellXfs><xf numFmtId="0"/></cellXfs>',
            "outside the declared cell formats",
            id="style-index-out-of-range",
        ),
        pytest.param(
            '<c r="A1" s="0"><v>1</v></c>',
            "",
            None,
            "declares no cell formats",
            id="style-without-a-styles-part",
        ),
    ],
)
def test_unreadable_format_declarations_are_refused(
    cells: str, properties: str, styles: str | None, message: str
) -> None:
    """Each case leaves a cell's format or epoch unresolvable, not defaultable."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(
            xlsx(cells, properties=properties, styles=styles), capture_id=CAPTURE
        )


def test_lookalike_declarations_supply_no_epoch_and_no_format() -> None:
    """Only a qualified declaration counts, and a lookalike takes no index."""
    content = xlsx(
        '<c r="A1" s="0"><v>1</v></c>',
        # Neither says which epoch the workbook counts from, so the 1900
        # default SpreadsheetML gives an absent declaration stands.
        properties='<workbookPr date1904="true"/>'
        '<workbookPr xmlns="urn:not-spreadsheetml" date1904="true"/>',
        # The unqualified entry is not an ``xf``, so it occupies no style index
        # and ``s="0"`` is the qualified entry following it; the unqualified
        # format list is not ``numFmts`` and supplies no code for its id.
        styles='<numFmts xmlns=""><numFmt numFmtId="164" formatCode="LOOKALIKE"/></numFmts>'
        '<cellXfs><xf xmlns="" numFmtId="9"/><xf numFmtId="164"/></cellXfs>',
    )

    assert formats(content) == [
        {
            "styleIndex": 0,
            "numberFormatId": 164,
            "numberFormatCode": None,
            "dateSystem": "1900",
        }
    ]


PLAIN = '<c r="A1"><v>1</v></c>'
TWO_FORMATS = (
    '<numFmts><numFmt numFmtId="164" formatCode="0"/>'
    '<numFmt numFmtId="165" formatCode="0.0"/></numFmts>'
)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(
            xlsx(
                PLAIN,
                styles=CUSTOM_STYLES,
                extra_related='<Relationship Id="rId3" Target="styles.xml" '
                f'Type="{STYLES_RELATIONSHIP}"/>',
            ),
            "more than one styles relationship",
            id="two-qualifying-relationships",
        ),
        pytest.param(
            # Nothing in the package answers to this target, so a reader that
            # refuses the relationship cannot have gone looking for one.
            xlsx(
                PLAIN,
                extra_related='<Relationship Id="rId2" TargetMode="External" '
                'Target="https://example.invalid/styles.xml" '
                f'Type="{STYLES_RELATIONSHIP}"/>',
            ),
            "styles relationship is external",
            id="external-qualifying-relationship",
        ),
        pytest.param(
            xlsx(PLAIN, styles=CUSTOM_STYLES, styles_type="application/xml"),
            "does not name a styles part",
            id="qualifying-route-to-another-type",
        ),
        pytest.param(
            xlsx(PLAIN, styles=CUSTOM_STYLES, namespace="urn:not-spreadsheetml"),
            "styles part uses an unsupported namespace",
            id="styles-root-in-a-foreign-namespace",
        ),
        pytest.param(
            xlsx(PLAIN, styles=CUSTOM_STYLES, root="numbering"),
            "not rooted at its own style sheet",
            id="styles-part-rooted-elsewhere",
        ),
        pytest.param(
            xlsx(PLAIN, styles='<numFmts><numFmt numFmtId="x" formatCode="0"/></numFmts>'),
            "number format number-format id is malformed",
            id="malformed-custom-format-id",
        ),
        pytest.param(
            xlsx(
                PLAIN,
                styles='<numFmts><numFmt numFmtId="164" formatCode="0"/>'
                '<numFmt numFmtId="164" formatCode="0.0"/></numFmts>',
            ),
            "number format id is repeated",
            id="duplicate-custom-format-id",
        ),
    ],
)
def test_unresolvable_styles_routes_and_declarations_are_refused(
    content: bytes, message: str
) -> None:
    """Each case leaves which styles part, or which format, is meant unsettled."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("constant", "bound", "styles", "message"),
    [
        pytest.param(
            "_MAX_XLSX_FORMAT_CODE_CHARS",
            4,
            CUSTOM_STYLES,
            "format code exceeds the accepted bound",
            id="format-code-chars",
        ),
        pytest.param(
            "_MAX_XLSX_CELL_FORMATS",
            1,
            CUSTOM_STYLES,
            "too many cell formats",
            id="cell-format-count",
        ),
        pytest.param(
            "_MAX_XLSX_NUMBER_FORMATS",
            1,
            TWO_FORMATS,
            "too many number formats",
            id="number-format-count",
        ),
    ],
)
def test_styles_parts_past_their_bounds_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    bound: int,
    styles: str,
    message: str,
) -> None:
    """The bounds hold on the part's own work, not on the sheet that cites it."""
    monkeypatch.setattr(ooxml_extractors, constant, bound)

    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(xlsx(PLAIN, styles=styles), capture_id=CAPTURE)
