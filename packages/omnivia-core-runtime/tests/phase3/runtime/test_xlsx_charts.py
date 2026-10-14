"""XLSX drawing charts: the stored values a worksheet's chart already holds.

A workbook reaches a chart the long way round -- the worksheet relates a
drawing, the drawing anchors a graphic frame, and the frame's ``c:chart``
relates the chart part -- so a record's locator names every step of that route
rather than only its end: the sheet it is on, the drawing part and the
relationship id the worksheet reached it by, the anchor showing it and the
cells that anchor covers, then the chart part and the relationship id the
drawing reached *it* by.

The route is the only new part. What is read at the end of it is the shared
chart reader already serving PPTX graphic frames and DOCX drawings, so the
record vocabulary is theirs unchanged: ``chart_title``, ``chart_series_name``,
``chart_category`` and ``chart_value`` under the ``chart`` semantic role, each
carrying ``seriesIndex``, ``seriesOrder``, ``categoryLevel``, ``pointIndex``,
a source ``formula`` as inert metadata, and ``freshness`` of ``unknown``.

The sheet is named with ``sheetPart`` rather than ``part``, as an XLSX comment
already does, because the content of a chart record lives in the chart part and
the worksheet is provenance for it.

Nothing behind the values is opened: the fixture's chart relates an embedded
workbook that is not a readable package at all, so a reader that opened one
would fail here rather than quietly return fresher numbers.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# The spreadsheet-drawing vocabulary a worksheet's drawing part is rooted in,
# and the DrawingML chart vocabulary the chart part itself is written in.
SHEET_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"
CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
DRAWING_PART = "xl/drawings/drawing1.xml"
CHART_PART = "xl/charts/chart1.xml"
EMBEDDED_PART = "xl/embeddings/Microsoft_Excel_Sheet1.xlsx"

# One series: a cached name, two cached categories and two cached values, each
# beside the formula that named its source.
SERIES = (
    '<c:ser><c:idx val="0"/><c:order val="0"/>'
    "<c:tx><c:strRef><c:f>Sales!$B$1</c:f>"
    '<c:strCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>Revenue</c:v></c:pt></c:strCache>'
    "</c:strRef></c:tx>"
    "<c:cat><c:strRef><c:f>Sales!$A$2:$A$3</c:f>"
    '<c:strCache><c:ptCount val="2"/>'
    '<c:pt idx="0"><c:v>Q1</c:v></c:pt>'
    '<c:pt idx="1"><c:v>Q2</c:v></c:pt></c:strCache>'
    "</c:strRef></c:cat>"
    "<c:val><c:numRef><c:f>Sales!$B$2:$B$3</c:f>"
    '<c:numCache><c:ptCount val="2"/>'
    '<c:pt idx="0"><c:v>12</c:v></c:pt>'
    '<c:pt idx="1"><c:v>15.5</c:v></c:pt></c:numCache>'
    "</c:numRef></c:val></c:ser>"
)
CHART = (
    f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}"><c:chart>'
    "<c:title><c:tx><c:rich><a:p>"
    "<a:r><a:t>Quarterly</a:t></a:r><a:r><a:t> revenue</a:t></a:r>"
    "</a:p></c:rich></c:tx></c:title>"
    f"<c:plotArea><c:barChart>{SERIES}</c:barChart></c:plotArea>"
    "</c:chart></c:chartSpace>"
)
# ``xdr:from``/``xdr:to`` are zero-based, so columns 0..5 and rows 0..10 are
# the A1 range ``A1:F11``.
ANCHOR = (
    '<xdr:twoCellAnchor editAs="oneCell">'
    "<xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
    "<xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>10</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    '<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr>'
    '<xdr:cNvPr id="2" name="Chart 1"/><xdr:cNvGraphicFramePr/>'
    "</xdr:nvGraphicFramePr><xdr:xfrm/>"
    f'<a:graphic><a:graphicData uri="{CHART_NS}">'
    f'<c:chart xmlns:c="{CHART_NS}" xmlns:r="{RELNS}" r:id="rIdC"/>'
    "</a:graphicData></a:graphic></xdr:graphicFrame>"
    "<xdr:clientData/></xdr:twoCellAnchor>"
)
DRAWING = (
    f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}">'
    f"{ANCHOR}</xdr:wsDr>"
)
SHEET = (
    f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}">'
    '<sheetData><row r="1">'
    '<c r="B1" t="inlineStr"><is><t>Revenue</t></is></c>'
    "</row></sheetData>"
    '<drawing r:id="rIdD"/></worksheet>'
)


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def xlsx(sheet: str = SHEET, drawing: str = DRAWING) -> bytes:
    """One sheet relating one drawing, anchoring one chart, over one series.

    The chart relates an embedded workbook whose member is not an archive at
    all: it is there to be left alone, and a reader that opened it could not
    read it.
    """
    members = {
        "[Content_Types].xml": (
            "<Types>"
            f'<Override PartName="/{DRAWING_PART}" ContentType="{DRAWING_CT}"/>'
            f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
            '<sheet name="Sales" sheetId="7" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            "</Relationships>"
        ),
        SHEET_PART: sheet,
        "xl/worksheets/_rels/sheet1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdD" Target="../drawings/drawing1.xml"'
            f' Type="{RELNS}/drawing"/>'
            "</Relationships>"
        ),
        DRAWING_PART: drawing,
        "xl/drawings/_rels/drawing1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdC" Target="../charts/chart1.xml"'
            f' Type="{RELNS}/chart"/>'
            "</Relationships>"
        ),
        CHART_PART: CHART,
        "xl/charts/_rels/chart1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdW" Target="../embeddings/'
            f'Microsoft_Excel_Sheet1.xlsx" Type="{RELNS}/package"/>'
            "</Relationships>"
        ),
        EMBEDDED_PART: "not a workbook",
    }
    return archive(members)


def records(content: bytes) -> list[tuple[str | None, dict[str, object]]]:
    return [
        (item.text, dict(item.source_locator))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ]


# The provenance every record carries. Each one differs from this only in the
# place inside the chart: the role, the series it sits in and the point it is.
EXPECTED: dict[str, object] = {
    "captureId": CAPTURE,
    "sheetPart": SHEET_PART,
    "sheetId": "7",
    "sheet": "Sales",
    "sheetState": "visible",
    "drawingPart": DRAWING_PART,
    "drawingRelationshipId": "rIdD",
    "anchorIndex": 0,
    "anchorType": "twoCell",
    "anchorRange": "A1:F11",
    "chartPart": CHART_PART,
    "relationshipId": "rIdC",
    "chartRole": "chart_title",
    "seriesIndex": None,
    "seriesOrder": None,
    "categoryLevel": None,
    "pointIndex": None,
    "formula": None,
    "freshness": "unknown",
}
SERIES_PLACE: dict[str, object] = {"seriesIndex": "0", "seriesOrder": "0"}


def test_a_worksheet_chart_reports_its_stored_title_and_cached_points() -> None:
    """Title, series name, categories and values, in the order written.

    Values come through exactly as cached -- ``15.5`` is the string the chart
    stored, not a parsed number -- and each one names the formula its source
    was read from without that formula being resolved.
    """
    content = xlsx()

    assert records(content) == [
        ("Quarterly revenue", EXPECTED),
        (
            "Revenue",
            EXPECTED
            | SERIES_PLACE
            | {
                "chartRole": "chart_series_name",
                "pointIndex": "0",
                "formula": "Sales!$B$1",
            },
        ),
        (
            "Q1",
            EXPECTED
            | SERIES_PLACE
            | {
                "chartRole": "chart_category",
                "pointIndex": "0",
                "formula": "Sales!$A$2:$A$3",
            },
        ),
        (
            "Q2",
            EXPECTED
            | SERIES_PLACE
            | {
                "chartRole": "chart_category",
                "pointIndex": "1",
                "formula": "Sales!$A$2:$A$3",
            },
        ),
        (
            "12",
            EXPECTED
            | SERIES_PLACE
            | {
                "chartRole": "chart_value",
                "pointIndex": "0",
                "formula": "Sales!$B$2:$B$3",
            },
        ),
        (
            "15.5",
            EXPECTED
            | SERIES_PLACE
            | {
                "chartRole": "chart_value",
                "pointIndex": "1",
                "formula": "Sales!$B$2:$B$3",
            },
        ),
    ]
    assert [
        item.visibility
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ] == ["visible"] * 6


def test_only_a_qualified_direct_drawing_child_reaches_a_chart() -> None:
    """A drawing reference is read for its name *and* its place, not either.

    Two near misses carry the fixture's own relationship id, so each would
    reach the same chart if the route were taken: one is a ``drawing``
    element in a foreign vocabulary, the other is correctly qualified
    SpreadsheetML but hangs under another worksheet child rather than beside
    it. Neither is the reference a worksheet declares, so neither emits.
    """
    head = f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}">'
    foreign = (
        f'{head}<x:drawing xmlns:x="urn:not-spreadsheetml" r:id="rIdD"/></worksheet>'
    )
    nested = f'{head}<oleObjects><drawing r:id="rIdD"/></oleObjects></worksheet>'

    for sheet in (foreign, nested):
        assert records(xlsx(sheet)) == []


def test_only_qualified_direct_anchor_and_frame_elements_are_read() -> None:
    """The anchor and the frame are each read by name *and* place, not shape.

    Both variants keep every descendant of the renamed element exactly as the
    valid fixture wrote it, so only the element's own vocabulary differs: a
    direct ``twoCellAnchor`` moved into a foreign namespace, and a correctly
    qualified anchor whose direct ``graphicFrame`` is moved into one. Neither
    is the element the spreadsheet-drawing grammar declares, so neither route
    reaches the chart.
    """
    foreign = ' xmlns:f="urn:not-spreadsheetdrawing"'
    anchor = ANCHOR.replace(
        "<xdr:twoCellAnchor ", f"<f:twoCellAnchor{foreign} ", 1
    ).replace("</xdr:twoCellAnchor>", "</f:twoCellAnchor>")
    frame = ANCHOR.replace(
        "<xdr:graphicFrame ", f"<f:graphicFrame{foreign} ", 1
    ).replace("</xdr:graphicFrame>", "</f:graphicFrame>")

    for variant in (anchor, frame):
        drawing = (
            f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}">'
            f"{variant}</xdr:wsDr>"
        )
        assert records(xlsx(drawing=drawing)) == []


def test_an_anchor_corner_and_its_coordinates_are_read_by_name_and_vocabulary() -> None:
    """A corner and a coordinate are spreadsheet-drawing names, not any names.

    Each variant renames exactly one element into a foreign vocabulary and
    leaves everything under it as the valid fixture wrote it: the direct
    ``xdr:from`` corner, and the first ``xdr:col`` under a corner that is
    itself correctly qualified. Neither renamed element is the one the grammar
    declares, so the anchor no longer names that corner or that coordinate.
    """
    foreign = ' xmlns:f="urn:not-spreadsheetdrawing"'
    corner = DRAWING.replace("<xdr:from>", f"<f:from{foreign}>", 1).replace(
        "</xdr:from>", "</f:from>", 1
    )
    coordinate = DRAWING.replace(
        "<xdr:col>0</xdr:col>", f"<f:col{foreign}>0</f:col>", 1
    )

    for variant, diagnostic in (
        (corner, "does not name exactly one of its corners"),
        (coordinate, "corner is malformed"),
    ):
        with pytest.raises(OOXMLInvalid, match=diagnostic):
            records(xlsx(drawing=variant))


# The Strict profile's spelling of each base URI: the ``purl.oclc.org`` name
# OOXML defines beside the ``openxmlformats.org`` one.
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
STRICT_SHEET_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
STRICT_CHART_NS = "http://purl.oclc.org/ooxml/drawingml/chart"

# Every base URI the fixture's route is written on, beside its Strict spelling.
# The content types are shared between the profiles, so none of them appears
# here and the rewritten package declares exactly the fixture's own.
STRICT_URIS = {
    MAIN: STRICT_MAIN,
    RELNS: STRICT_RELNS,
    SHEET_DRAWING_NS: STRICT_SHEET_DRAWING_NS,
    DRAWING_NS: STRICT_DRAWING_NS,
    CHART_NS: STRICT_CHART_NS,
}


def strict_profile(content: bytes) -> bytes:
    """The same package with every Transitional base URI respelt as Strict."""
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        members = {
            name: package.read(name).decode("utf-8") for name in package.namelist()
        }
    for name, member in members.items():
        for transitional, strict in STRICT_URIS.items():
            member = member.replace(transitional, strict)
        members[name] = member
    return archive(members)


# The whole of the valid fixture's anchor from the frame onwards -- graphic
# frame, graphic data, ``c:chart`` reference and client data -- so the other
# two anchor kinds reach the chart over the route the valid one proved.
FRAME = ANCHOR[ANCHOR.index("<xdr:graphicFrame") : ANCHOR.index("</xdr:twoCellAnchor>")]
# The extent both sized anchors carry: one inch by half an inch in EMUs.
EXT = '<xdr:ext cx="914400" cy="457200"/>'
# ``xdr:from`` is zero-based, so column 2 row 3 is the single cell ``C4``.
ONE_CELL_ANCHOR = (
    "<xdr:oneCellAnchor>"
    "<xdr:from><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
    f"{EXT}{FRAME}</xdr:oneCellAnchor>"
)
# An absolute anchor is placed on the sheet, not in its grid, so it covers no
# cells at all -- and a negative offset is a position like any other.
ABSOLUTE_ANCHOR = (
    f'<xdr:absoluteAnchor><xdr:pos x="-100" y="200"/>{EXT}{FRAME}</xdr:absoluteAnchor>'
)


def anchored(anchor: str) -> bytes:
    return xlsx(
        drawing=(
            f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}">'
            f"{anchor}</xdr:wsDr>"
        )
    )


@pytest.mark.parametrize(
    ("anchor", "anchor_type", "anchor_range", "geometry"),
    [
        (
            ONE_CELL_ANCHOR,
            "oneCell",
            "C4",
            {"cx": 914400, "cy": 457200},
        ),
        (
            ABSOLUTE_ANCHOR,
            "absolute",
            None,
            {"x": -100, "y": 200, "cx": 914400, "cy": 457200},
        ),
    ],
    ids=["oneCell", "absolute"],
)
def test_a_sized_or_placed_anchor_reports_the_same_chart_records(
    anchor: str,
    anchor_type: str,
    anchor_range: str | None,
    geometry: dict[str, int],
) -> None:
    """The anchor kind changes where the chart sits, not what it reports.

    Each variant anchors the valid fixture's own frame, so every record is the
    two-cell record with only its placement rewritten: the anchor kind, the
    cells it covers -- a single cell for ``oneCellAnchor``, none at all for
    ``absoluteAnchor`` -- and the stored geometry that placement is made of.
    """
    assert records(anchored(anchor)) == [
        (
            text,
            locator
            | {
                "anchorType": anchor_type,
                "anchorRange": anchor_range,
                "anchorGeometry": geometry,
            },
        )
        for text, locator in records(xlsx())
    ]


def test_the_strict_spelling_of_the_fixture_reports_the_same_records() -> None:
    """Respelling the whole route changes nothing that comes out of it.

    The Transitional fixture is rewritten member by member -- workbook,
    worksheet, both relationship parts, the drawing and the chart -- and the
    records read back off it are the Transitional ones exactly, so the Strict
    names carry the route end to end and reach the same cached chart output.
    """
    assert records(strict_profile(xlsx())) == records(xlsx()) != []


@pytest.mark.parametrize(
    "anchor", [ONE_CELL_ANCHOR, ABSOLUTE_ANCHOR], ids=["oneCell", "absolute"]
)
def test_the_strict_spelling_of_a_sized_anchor_reports_the_same_records(
    anchor: str,
) -> None:
    """The sized kinds' own elements are respelt by the profile too.

    A sized anchor reads two more spreadsheet-drawing names than a two-cell one
    -- ``xdr:ext``, and ``xdr:pos`` for the absolute kind -- so each is held to
    the drawing's namespace and each has a Strict spelling of its own. Rewriting
    the whole package reaches the same records, placement included.
    """
    assert records(strict_profile(anchored(anchor))) == records(anchored(anchor)) != []


# ``xdr:pos`` as the valid absolute anchor writes it, so a variant can be put in
# its place the way one can in ``EXT``'s.
POS = '<xdr:pos x="-100" y="200"/>'
FOREIGN = ' xmlns:f="urn:not-spreadsheetdrawing"'


def lookalikes(element: str) -> dict[str, str]:
    """The four ways an anchor stops naming exactly one of ``element``.

    Each is written in the valid element's place: gone altogether, written
    twice, stripped of its prefix so nothing qualifies it, and renamed into a
    foreign vocabulary. Only the first spelling is the one the drawing's
    grammar declares, so the last two name as little as the first.
    """
    head, _, attributes = element.partition(" ")
    local = head.removeprefix("<xdr:")
    return {
        "missing": "",
        "duplicate": element * 2,
        "unqualified": f"<{local} {attributes}",
        "foreign": f"<f:{local}{FOREIGN} {attributes}",
    }


# Every way a sized anchor leaves its own placement unanswerable, by the element
# it is written on: the extent both sized kinds carry, and the position only the
# absolute kind does. An extent is a size and has no signed form, so a negative
# one names none, while the valid absolute anchor's own negative ``x`` is a
# coordinate like any other and is read as it stands.
REFUSED: dict[str, tuple[str, str]] = {
    **{
        f"extent-{case}": (
            ONE_CELL_ANCHOR.replace(EXT, variant, 1),
            "does not name exactly one extent",
        )
        for case, variant in lookalikes(EXT).items()
    },
    **{
        f"position-{case}": (
            ABSOLUTE_ANCHOR.replace(POS, variant, 1),
            "does not name exactly one position",
        )
        for case, variant in lookalikes(POS).items()
    },
    "extent-without-cy": (
        ONE_CELL_ANCHOR.replace(EXT, '<xdr:ext cx="914400"/>', 1),
        "extent is malformed",
    ),
    "extent-not-a-number": (
        ONE_CELL_ANCHOR.replace(EXT, '<xdr:ext cx="wide" cy="457200"/>', 1),
        "extent is malformed",
    ),
    "extent-negative": (
        ONE_CELL_ANCHOR.replace(EXT, '<xdr:ext cx="-914400" cy="457200"/>', 1),
        "extent is malformed",
    ),
    "position-without-y": (
        ABSOLUTE_ANCHOR.replace(POS, '<xdr:pos x="-100"/>', 1),
        "position is malformed",
    ),
    "position-not-a-number": (
        ABSOLUTE_ANCHOR.replace(POS, '<xdr:pos x="left" y="200"/>', 1),
        "position is malformed",
    ),
}


@pytest.mark.parametrize(("anchor", "diagnostic"), REFUSED.values(), ids=list(REFUSED))
def test_a_sized_anchor_without_one_well_formed_placement_is_refused(
    anchor: str, diagnostic: str
) -> None:
    """Where a sized frame sits is read or refused, never guessed."""
    with pytest.raises(OOXMLInvalid, match=diagnostic):
        records(anchored(anchor))


def test_an_absolute_position_keeps_both_its_signed_coordinates() -> None:
    """A frame placed off the top-left of the sheet keeps the sign it was given.

    Both coordinates are negative here, so neither is clamped nor read as the
    unsigned value an extent would be held to.
    """
    anchor = ABSOLUTE_ANCHOR.replace(POS, '<xdr:pos x="-100" y="-200"/>', 1)

    assert [locator["anchorGeometry"] for _, locator in records(anchored(anchor))] == [
        {"x": -100, "y": -200, "cx": 914400, "cy": 457200}
    ] * len(records(xlsx()))


@pytest.mark.parametrize(
    ("anchor", "element", "other"),
    [
        (ONE_CELL_ANCHOR, EXT, '<xdr:ext cx="1" cy="2"/>'),
        (ABSOLUTE_ANCHOR, POS, '<xdr:pos x="3" y="4"/>'),
    ],
    ids=["extent", "position"],
)
def test_a_foreign_lookalike_beside_one_valid_placement_is_transparent(
    anchor: str, element: str, other: str
) -> None:
    """A lookalike is neither a second placement nor the one that is read.

    The foreign element carries different numbers from the valid one it sits
    beside, so an anchor counting placements by shape would refuse this as
    naming two, and one reading them by shape could report the lookalike's.
    """
    variant = anchor.replace(element, f"{element}{lookalikes(other)['foreign']}", 1)

    assert records(anchored(variant)) == records(anchored(anchor)) != []
