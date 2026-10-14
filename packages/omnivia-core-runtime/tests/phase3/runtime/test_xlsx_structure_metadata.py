"""XLSX sheet visibility, merged ranges and hyperlink inventory over minimal packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
WORKBOOK_RELS = (
    b"<Relationships>"
    b'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    b"</Relationships>"
)


def xlsx(
    worksheet: str,
    *,
    state: str | None = None,
    links: tuple[tuple[str, str, bool], ...] = (),
    extra: dict[str, bytes] | None = None,
) -> bytes:
    attrs = f' state="{state}"' if state is not None else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns:r="urn:r"><sheets>'
            f'<sheet name="Budget" sheetId="7" r:id="rId1"{attrs}/>'
            "</sheets></workbook>",
        )
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns:r="urn:r">{worksheet}</worksheet>',
        )
        if links:
            archive.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                "<Relationships>"
                + "".join(
                    f'<Relationship Id="{rid}" Target="{target}"'
                    f' Type="{REL}hyperlink"'
                    + (' TargetMode="External"/>' if external else "/>")
                    for rid, target, external in links
                )
                + "</Relationships>",
            )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def sheet(cells: str, *, before: str = "", after: str = "") -> str:
    return f"{before}<sheetData>{cells}</sheetData>{after}"


def located(content: bytes) -> list[tuple[str, dict[str, object]]]:
    return [
        (item.semantic_role, dict(item.source_locator))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ]


def cell_state(content: bytes) -> list[tuple[str, object, object, object, str]]:
    return [
        (
            str(item.source_locator["cell"]),
            item.source_locator["sheetState"],
            item.source_locator["rowHidden"],
            item.source_locator["columnHidden"],
            item.visibility,
        )
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ]


def inventory(content: bytes) -> list[dict[str, object]]:
    return [
        item.structured_value
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


def test_sheet_row_and_column_hidden_state_is_kept_on_every_cell() -> None:
    """Each state is retained separately, and any one of them hides the cell."""
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row>'
            '<row r="2" hidden="1"><c r="A2"><v>3</v></c></row>',
            before='<cols><col min="2" max="3" hidden="true"/></cols>',
        )
    )

    assert cell_state(content) == [
        ("A1", "visible", False, False, "visible"),
        ("B1", "visible", False, True, "hidden"),
        ("A2", "visible", True, False, "hidden"),
    ]


def test_very_hidden_sheet_stays_distinguishable_from_a_hidden_one() -> None:
    cells = '<row r="1"><c r="A1"><f>A2</f><v>1</v></c></row>'

    assert [
        (item[1], item[4]) for item in cell_state(xlsx(sheet(cells), state="hidden"))
    ] == [("hidden", "hidden"), ("hidden", "hidden")]
    assert [
        (item[1], item[4])
        for item in cell_state(xlsx(sheet(cells), state="veryHidden"))
    ] == [("veryHidden", "hidden"), ("veryHidden", "hidden")]


def test_unknown_sheet_state_and_unreadable_flag_are_refused() -> None:
    cells = '<row r="1"><c r="A1"><v>1</v></c></row>'

    with pytest.raises(OOXMLInvalid, match="unknown visibility state"):
        extract_xlsx(xlsx(sheet(cells), state="archived"), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="boolean attribute is malformed"):
        extract_xlsx(
            xlsx(sheet('<row r="1" hidden="maybe"><c r="A1"><v>1</v></c></row>')),
            capture_id=CAPTURE,
        )


def test_malformed_column_span_is_refused_only_where_it_is_read() -> None:
    """A hidden span is parsed; one that claims nothing hidden is never read."""
    cells = '<row r="1"><c r="A1"><v>1</v></c></row>'

    with pytest.raises(OOXMLInvalid, match="column span is malformed"):
        extract_xlsx(
            xlsx(sheet(cells, before='<cols><col min="4" max="2" hidden="1"/></cols>')),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="column span is malformed"):
        extract_xlsx(
            xlsx(sheet(cells, before='<cols><col max="2" hidden="1"/></cols>')),
            capture_id=CAPTURE,
        )
    assert (
        len(cell_state(xlsx(sheet(cells, before='<cols><col min="9"/></cols>')))) == 1
    )


def test_merged_membership_is_recorded_without_expanding_absent_cells() -> None:
    """Only the written cells of a merge are reported, each naming the range."""
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Title</t></is></c></row>'
            '<row r="2"><c r="B2"><v>5</v></c></row>',
            after='<mergeCells count="1"><mergeCell ref="A1:B2"/></mergeCells>',
        )
    )

    assert [
        (locator["cell"], locator["mergedRange"], locator["mergedAnchor"])
        for _role, locator in located(content)
    ] == [("A1", "A1:B2", True), ("B2", "A1:B2", False)]


def test_cells_outside_a_merge_carry_no_membership_keys() -> None:
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c><c r="C1"><v>2</v></c></row>',
            after='<mergeCells><mergeCell ref="A1:B1"/></mergeCells>',
        )
    )
    first, second = located(content)

    assert first[1]["mergedRange"] == "A1:B1"
    assert "mergedRange" not in second[1] and "mergedAnchor" not in second[1]


def test_overlapping_and_malformed_merged_ranges_are_refused() -> None:
    cells = '<row r="1"><c r="A1"><v>1</v></c></row>'

    def merged(*refs: str) -> bytes:
        body = "".join(f'<mergeCell ref="{ref}"/>' for ref in refs)
        return xlsx(sheet(cells, after=f"<mergeCells>{body}</mergeCells>"))

    with pytest.raises(OOXMLInvalid, match="merged ranges overlap"):
        extract_xlsx(merged("A1:B2", "B2:C3"), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="cell range is malformed"):
        extract_xlsx(merged("B2:A1"), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="cell coordinate is malformed"):
        extract_xlsx(merged("A1:B"), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="merged range has no reference"):
        extract_xlsx(
            xlsx(sheet(cells, after="<mergeCells><mergeCell/></mergeCells>")),
            capture_id=CAPTURE,
        )


def test_merged_ranges_beyond_the_bound_are_refused_not_expanded() -> None:
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>',
            after='<mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells>',
        )
    )

    with pytest.raises(OOXMLInvalid, match="merged ranges exceed"):
        extract_xlsx(content, capture_id=CAPTURE)


def test_malformed_cell_coordinates_are_refused_rather_than_guessed() -> None:
    for coordinate in ("A0", "a1", "AAAA1", "XFE1", "1A", "A1048577", "$A$1"):
        with pytest.raises(OOXMLInvalid, match="coordinate (is malformed|is outside)"):
            extract_xlsx(
                xlsx(sheet(f'<row r="1"><c r="{coordinate}"><v>1</v></c></row>')),
                capture_id=CAPTURE,
            )


def test_a_lying_dimension_adds_no_cells_and_hides_none() -> None:
    """Extraction stays sparse: only written cells are read, whatever is claimed."""
    content = xlsx(
        sheet(
            '<row r="5"><c r="C5"><v>9</v></c></row>',
            before='<dimension ref="A1:Z100"/>',
        )
    )

    assert [locator["cell"] for _role, locator in located(content)] == ["C5"]


def test_hyperlinks_are_inventoried_inertly_with_their_source_coordinate() -> None:
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>',
            after=(
                "<hyperlinks>"
                '<hyperlink ref="A1" r:id="rId9"/>'
                '<hyperlink ref="B2:C3" r:id="rId8"/>'
                '<hyperlink ref="D4" location="Budget!A1" display="jump"/>'
                "</hyperlinks>"
            ),
        ),
        links=(
            ("rId9", "https://Example.invalid/a?b=1", True),
            ("rId8", "sheet2.xml", False),
        ),
        extra={"xl/worksheets/sheet2.xml": b"<worksheet/>"},
    )

    assert [
        (
            link["cell"],
            link["target"],
            link["external"],
            link["scheme"],
            link["resolved"],
            link["location"],
        )
        for link in inventory(content)
    ] == [
        ("A1", "https://Example.invalid/a?b=1", True, "https", False, None),
        ("B2:C3", "xl/worksheets/sheet2.xml", False, None, True, None),
        # A location-only jump names no relationship, so there is none to carry.
        ("D4", None, False, None, False, "Budget!A1"),
    ]
    first = inventory(content)[0]
    assert first["relationshipId"] == "rId9"
    assert first["relationshipType"] == REL + "hyperlink"
    assert first["targetTruncated"] is False


def test_hyperlink_segments_are_inert_and_follow_their_sheet_visibility() -> None:
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>',
            after='<hyperlinks><hyperlink ref="A1" location="A2"/></hyperlinks>',
        ),
        state="hidden",
    )

    link = extract_xlsx(content, capture_id=CAPTURE).segments[-1]

    assert link.semantic_role == "object_inventory"
    assert link.extraction_method == "inventory"
    assert link.text is None and link.visibility == "hidden"
    assert link.source_locator["cell"] == "A1"
    assert link.source_locator["sheetState"] == "hidden"


def test_malformed_and_unresolvable_hyperlinks_are_refused() -> None:
    def linked(attrs: str, links: tuple[tuple[str, str, bool], ...] = ()) -> bytes:
        return xlsx(
            sheet(
                '<row r="1"><c r="A1"><v>1</v></c></row>',
                after=f"<hyperlinks><hyperlink {attrs}/></hyperlinks>",
            ),
            links=links,
        )

    with pytest.raises(OOXMLInvalid, match="hyperlink has no reference"):
        extract_xlsx(linked('location="A2"'), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="no relationship or location"):
        extract_xlsx(linked('ref="A1"'), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="cell coordinate is malformed"):
        extract_xlsx(linked('ref="A" location="A2"'), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="missing relationship"):
        extract_xlsx(
            linked('ref="A1" r:id="rId9"', (("rId1", "a.invalid", True),)),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="relationship of another type"):
        extract_xlsx(
            xlsx(
                sheet(
                    '<row r="1"><c r="A1"><v>1</v></c></row>',
                    after='<hyperlinks><hyperlink ref="A1" r:id="rId9"/></hyperlinks>',
                ),
                extra={
                    "xl/worksheets/_rels/sheet1.xml.rels": (
                        b"<Relationships>"
                        b'<Relationship Id="rId9" Target="https://a.invalid/"'
                        b' Type="' + REL.encode() + b'image" TargetMode="External"/>'
                        b"</Relationships>"
                    )
                },
            ),
            capture_id=CAPTURE,
        )


def test_cells_outside_sheet_data_rows_are_not_read() -> None:
    content = xlsx(
        '<c r="A1"><v>1</v></c>' + sheet('<row r="1"><c r="B1"><v>2</v></c></row>')
    )

    assert [locator["cell"] for _role, locator in located(content)] == ["B1"]
