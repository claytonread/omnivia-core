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

from omnivia_core_runtime.service.ooxml_extractors import extract_xlsx

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# The spreadsheet-drawing vocabulary a worksheet's drawing part is rooted in,
# and the DrawingML chart vocabulary the chart part itself is written in.
SHEET_DRAWING_NS = (
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
)
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


def xlsx(sheet: str = SHEET) -> bytes:
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
        DRAWING_PART: DRAWING,
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
        f'{head}<x:drawing xmlns:x="urn:not-spreadsheetml" r:id="rIdD"/>'
        "</worksheet>"
    )
    nested = f'{head}<oleObjects><drawing r:id="rIdD"/></oleObjects></worksheet>'

    for sheet in (foreign, nested):
        assert records(xlsx(sheet)) == []


# The same route written wholly in the Strict profile: every namespace and
# every relationship type is the ``purl.oclc.org`` spelling OOXML defines
# beside the ``openxmlformats.org`` one. The content types are shared between
# the profiles, so they are the fixture's own unchanged, and a Strict workbook
# reaches its chart on the Strict names alone.
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
STRICT_SHEET_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
STRICT_CHART_NS = "http://purl.oclc.org/ooxml/drawingml/chart"
# Columns 1..2 and rows 1..3 are the A1 range ``B2:C4``.
STRICT_ANCHOR = (
    "<xdr:twoCellAnchor>"
    "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
    "<xdr:to><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    "<xdr:graphicFrame><xdr:nvGraphicFramePr>"
    '<xdr:cNvPr id="2" name="Chart 1"/><xdr:cNvGraphicFramePr/>'
    "</xdr:nvGraphicFramePr><xdr:xfrm/>"
    f'<a:graphic><a:graphicData uri="{STRICT_CHART_NS}">'
    f'<c:chart xmlns:c="{STRICT_CHART_NS}" xmlns:r="{STRICT_RELNS}" r:id="rIdC"/>'
    "</a:graphicData></a:graphic></xdr:graphicFrame>"
    "<xdr:clientData/></xdr:twoCellAnchor>"
)
STRICT_CHART = (
    f'<c:chartSpace xmlns:c="{STRICT_CHART_NS}" xmlns:a="{STRICT_DRAWING_NS}">'
    "<c:chart><c:title><c:tx><c:rich><a:p>"
    "<a:r><a:t>Strict revenue</a:t></a:r>"
    "</a:p></c:rich></c:tx></c:title>"
    "<c:plotArea><c:barChart>"
    '<c:ser><c:idx val="0"/><c:order val="0"/>'
    "<c:val><c:numRef><c:f>Strict!$B$2</c:f>"
    '<c:numCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>7</c:v></c:pt></c:numCache>'
    "</c:numRef></c:val></c:ser>"
    "</c:barChart></c:plotArea></c:chart></c:chartSpace>"
)


def strict_xlsx() -> bytes:
    """One Strict sheet relating one drawing, anchoring one chart, over one value."""
    members = {
        "[Content_Types].xml": (
            "<Types>"
            f'<Override PartName="/{DRAWING_PART}" ContentType="{DRAWING_CT}"/>'
            f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{STRICT_MAIN}" xmlns:r="{STRICT_RELNS}"><sheets>'
            '<sheet name="Strict" sheetId="3" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{STRICT_RELNS}/worksheet"/>'
            "</Relationships>"
        ),
        SHEET_PART: (
            f'<worksheet xmlns="{STRICT_MAIN}" xmlns:r="{STRICT_RELNS}">'
            '<sheetData/><drawing r:id="rIdD"/></worksheet>'
        ),
        "xl/worksheets/_rels/sheet1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdD" Target="../drawings/drawing1.xml"'
            f' Type="{STRICT_RELNS}/drawing"/>'
            "</Relationships>"
        ),
        DRAWING_PART: (
            f'<xdr:wsDr xmlns:xdr="{STRICT_SHEET_DRAWING_NS}"'
            f' xmlns:a="{STRICT_DRAWING_NS}">{STRICT_ANCHOR}</xdr:wsDr>'
        ),
        "xl/drawings/_rels/drawing1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdC" Target="../charts/chart1.xml"'
            f' Type="{STRICT_RELNS}/chart"/>'
            "</Relationships>"
        ),
        CHART_PART: STRICT_CHART,
    }
    return archive(members)


def test_a_strict_profile_workbook_reaches_its_chart_on_strict_names() -> None:
    """The Strict route reports the same records as the Transitional one.

    Nothing about a record is profile-specific: the route is walked on the
    Strict namespaces and relationship types throughout, and what comes back
    is the Transitional fixture's own provenance differing only in the sheet
    it is on and the cells its anchor covers.
    """
    strict = EXPECTED | {"sheetId": "3", "sheet": "Strict", "anchorRange": "B2:C4"}

    assert records(strict_xlsx()) == [
        ("Strict revenue", strict),
        (
            "7",
            strict
            | SERIES_PLACE
            | {
                "chartRole": "chart_value",
                "pointIndex": "0",
                "formula": "Strict!$B$2",
            },
        ),
    ]


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
            name: package.read(name).decode("utf-8")
            for name in package.namelist()
        }
    for name, member in members.items():
        for transitional, strict in STRICT_URIS.items():
            member = member.replace(transitional, strict)
        members[name] = member
    return archive(members)


def test_the_strict_spelling_of_the_fixture_reports_the_same_records() -> None:
    """Respelling the whole route changes nothing that comes out of it.

    The Transitional fixture is rewritten member by member -- workbook,
    worksheet, both relationship parts, the drawing and the chart -- and the
    records read back off it are the Transitional ones exactly, so the Strict
    names carry the route end to end and reach the same cached chart output.
    """
    assert records(strict_profile(xlsx())) == records(xlsx()) != []
