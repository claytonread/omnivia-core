"""XLSX DrawingML extension coverage: a worksheet's drawing and its charts.

``test_xlsx_extension_coverage.py`` pins the worksheet and workbook roots and
``test_xlsx_extension_owners.py`` the rest of the SpreadsheetML parts. This file
pins the two DrawingML ones: the drawing part a worksheet relates and the chart
parts that drawing's graphic frames relate. Both are reached by the readers'
existing validated routes -- the sheet's own ``drawing`` relationship, the
frame's own ``c:chart`` relationship, each held to its relationship type,
content type and root -- so a member that merely looks like one is never
scanned.

Their vocabularies recurse, so an owner is named rather than placed and the
structural path says which position a list was found at. The walk covers the
part vocabulary and DrawingML main -- including nested owners such as
``a:bodyPr``, ``a:ln`` and ``a:rPr`` -- while never entering foreign graphics a
frame carries or any extension payload.
"""

from __future__ import annotations

import builtins
import io
import socket
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

ox = ooxml_extractors
CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# The spreadsheet-drawing, DrawingML main and DrawingML chart vocabularies, each
# in both profiles OOXML defines, and Excel's own 2010 drawing extensions.
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
XDR_STRICT = "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
A_STRICT = "http://purl.oclc.org/ooxml/drawingml/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
C_STRICT = "http://purl.oclc.org/ooxml/drawingml/chart"
XDR14 = "http://schemas.microsoft.com/office/excel/2010/spreadsheetDrawing"

DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"
CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
DRAWING_PART = "xl/drawings/drawing1.xml"
CHART_PART = "xl/charts/chart1.xml"

# Everything an extension body carries that a coverage record may never repeat.
SECRETS = (
    "hunter2",
    "Web.Contents",
    "https://example.invalid/secret",
    "token=abc123",
    "=SUM(A1:A9)",
    "Filtered revenue",
    "cached-value-42",
)
# A payload that imitates everything either part's reader looks for: a whole
# anchor with a picture naming an image, a connector naming its endpoints, a
# graphic frame naming a chart relationship, a series with a cache of its own,
# and a run of title text. Every relationship id in it is one no part declares,
# so a reader that resolved one would refuse the package rather than pass.
LEAKY = (
    '<x15:payload xmlns:x15="urn:x15" credential="hunter2">'
    "<x15:f>=SUM(A1:A9)</x15:f><x15:note>Filtered revenue</x15:note>"
    "</x15:payload>"
    '<mashup>let Source = Web.Contents("https://example.invalid/secret")'
    " in Source token=abc123</mashup><v>cached-value-42</v>"
    '<xdr:twoCellAnchor xmlns:xdr="' + XDR + '" xmlns:a="' + A + '">'
    "<xdr:from><xdr:col>9</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>9</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
    "<xdr:to><xdr:col>9</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>9</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="99" name="Filtered revenue"/>'
    "<xdr:cNvPicPr/></xdr:nvPicPr>"
    '<xdr:blipFill><a:blip xmlns:r="' + RELNS + '" r:embed="rIdGhost"/>'
    "</xdr:blipFill></xdr:pic>"
    "<xdr:cxnSp><xdr:nvCxnSpPr><xdr:cNvPr id=\"98\" name=\"c\"/>"
    '<xdr:cNvCxnSpPr><a:stCxn id="1" idx="0"/><a:endCxn id="2" idx="0"/>'
    "</xdr:cNvCxnSpPr></xdr:nvCxnSpPr></xdr:cxnSp>"
    '<xdr:graphicFrame><xdr:nvGraphicFramePr>'
    '<xdr:cNvPr id="97" name="f"/><xdr:cNvGraphicFramePr/>'
    "</xdr:nvGraphicFramePr>"
    '<a:graphic><a:graphicData uri="' + C + '">'
    '<c:chart xmlns:c="' + C + '" xmlns:r="' + RELNS + '" r:id="rIdGhost"/>'
    "</a:graphicData></a:graphic></xdr:graphicFrame>"
    "<xdr:clientData/></xdr:twoCellAnchor>"
    '<c:ser xmlns:c="' + C + '" xmlns:a="' + A + '">'
    '<c:idx val="9"/><c:order val="9"/>'
    "<c:val><c:numRef><c:f>Hidden!$Z$1</c:f>"
    '<c:numCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>cached-value-42</c:v></c:pt>'
    "</c:numCache></c:numRef></c:val></c:ser>"
    '<a:p xmlns:a="' + A + '"><a:r><a:t>Filtered revenue</a:t></a:r></a:p>'
)


def guid(n: int) -> str:
    return f"{{{n:08X}-0000-0000-0000-000000000000}}"


def ext(uri: str | None = None, body: str = "", *, tag: str = "a:ext") -> str:
    attribute = "" if uri is None else f' uri="{uri}"'
    return f"<{tag}{attribute}>{body}</{tag}>"


def alst(*uris: str | None, body: str = "", tag: str = "a") -> str:
    """A DrawingML main extension list: the one a drawing's owners carry."""
    items = "".join(ext(u, body, tag=f"{tag}:ext") for u in uris)
    return f"<{tag}:extLst>{items}</{tag}:extLst>"


def clst(*uris: str | None, body: str = "", tag: str = "c") -> str:
    """A chart extension list: the one most of a chart's own owners carry."""
    items = "".join(ext(u, body, tag=f"{tag}:ext") for u in uris)
    return f"<{tag}:extLst>{items}</{tag}:extLst>"


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def corners(column: int = 0, row: int = 0) -> str:
    return (
        f"<xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{column + 1}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row + 1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    )


def frame(*, claims: str = "", frame_claims: str = "", chart: bool = True) -> str:
    """One anchor holding a graphic frame, which relates the chart part."""
    reference = (
        f'<a:graphic><a:graphicData uri="{C}">'
        f'<c:chart r:id="rIdC"/></a:graphicData></a:graphic>'
        if chart
        else ""
    )
    return (
        f"<xdr:twoCellAnchor>{corners()}"
        "<xdr:graphicFrame><xdr:nvGraphicFramePr>"
        f'<xdr:cNvPr id="2" name="Chart 1">{claims}</xdr:cNvPr>'
        f"<xdr:cNvGraphicFramePr>{frame_claims}</xdr:cNvGraphicFramePr>"
        f"</xdr:nvGraphicFramePr><xdr:xfrm/>{reference}</xdr:graphicFrame>"
        "<xdr:clientData/></xdr:twoCellAnchor>"
    )


def shape(*, claims: str = "", shape_claims: str = "", properties: str = "") -> str:
    """One anchor holding a plain shape, the drawing's other inventoried object."""
    return (
        f"<xdr:twoCellAnchor>{corners(2, 2)}"
        f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="3" name="Shape 1">{claims}</xdr:cNvPr>'
        f"<xdr:cNvSpPr>{shape_claims}</xdr:cNvSpPr></xdr:nvSpPr>"
        f"<xdr:spPr>{properties}</xdr:spPr></xdr:sp>"
        "<xdr:clientData/></xdr:twoCellAnchor>"
    )


def drawing_xml(body: str = "", *, xdr: str = XDR, a: str = A) -> str:
    return (
        f'<xdr:wsDr xmlns:xdr="{xdr}" xmlns:a="{a}" xmlns:c="{C}"'
        f' xmlns:r="{RELNS}" xmlns:xdr14="{XDR14}">{body}</xdr:wsDr>'
    )


# One series with one cached point: enough for the chart reader to report a
# value, so a payload that changed what it read would change the output.
SERIES = (
    '<c:ser><c:idx val="0"/><c:order val="0"/>'
    "<c:val><c:numRef><c:f>S!$B$1</c:f>"
    '<c:numCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>12</c:v></c:pt></c:numCache>'
    "</c:numRef></c:val></c:ser>"
)


def chart_xml(
    *,
    title: str = "",
    series: str = "",
    plot: str = "",
    space: str = "",
    properties: str = "",
    c: str = C,
    a: str = A,
) -> str:
    return (
        f'<c:chartSpace xmlns:c="{c}" xmlns:a="{a}"><c:chart>'
        "<c:title><c:tx><c:rich><a:p><a:r><a:t>Quarterly</a:t></a:r></a:p>"
        f"</c:rich></c:tx>{title}</c:title>"
        f"<c:plotArea><c:barChart>{SERIES}{series}</c:barChart>{plot}</c:plotArea>"
        f"</c:chart><c:spPr>{properties}</c:spPr>{space}</c:chartSpace>"
    )


def build(
    *,
    drawing: str | None = None,
    chart: str | None = None,
    sheets: int = 1,
    states: tuple[str, ...] = (),
    relating: tuple[int, ...] = (1,),
    declare_drawing: bool = True,
    extra: dict[str, str] | None = None,
) -> bytes:
    """A workbook whose sheets in ``relating`` each relate the one drawing part.

    ``drawing`` defaults to a drawing holding a chart frame and a shape;
    ``chart`` to a chart with one cached value. Each part's content type is
    declared outright, as the validated routes require. ``declare_drawing``
    off leaves the drawing part in the package but unnamed by any sheet.
    """
    if drawing is None:
        drawing = drawing_xml(frame() + shape())
    if chart is None:
        chart = chart_xml()
    states = states + ("visible",) * (sheets - len(states))
    declared = "".join(
        f'<sheet name="S{n}" sheetId="{n}" state="{states[n - 1]}" r:id="rId{n}"/>'
        for n in range(1, sheets + 1)
    )
    members = {
        "[Content_Types].xml": (
            "<Types>"
            f'<Override PartName="/{DRAWING_PART}" ContentType="{DRAWING_CT}"/>'
            f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}">'
            f"<sheets>{declared}</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            + "".join(
                f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"'
                f' Type="{RELNS}/worksheet"/>'
                for n in range(1, sheets + 1)
            )
            + "</Relationships>"
        ),
        DRAWING_PART: drawing,
        "xl/drawings/_rels/drawing1.xml.rels": (
            "<Relationships>"
            f'<Relationship Id="rIdC" Target="../charts/chart1.xml"'
            f' Type="{RELNS}/chart"/></Relationships>'
        ),
        CHART_PART: chart,
    }
    for n in range(1, sheets + 1):
        names = '<drawing r:id="rIdD"/>' if n in relating and declare_drawing else ""
        members[f"xl/worksheets/sheet{n}.xml"] = (
            f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}"><sheetData>'
            f'<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
            f"</sheetData>{names}</worksheet>"
        )
        if n in relating:
            members[f"xl/worksheets/_rels/sheet{n}.xml.rels"] = (
                "<Relationships>"
                f'<Relationship Id="rIdD" Target="../drawings/drawing1.xml"'
                f' Type="{RELNS}/drawing"/></Relationships>'
            )
    return archive({**members, **(extra or {})})


def records(content: bytes) -> list[tuple[dict[str, object], dict[str, object], str]]:
    return [
        (dict(item.source_locator), dict(item.structured_value or {}), item.visibility)
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]


def brief(content: bytes) -> list[tuple[object, ...]]:
    return [
        (r["owner"], r["ownerPath"], r["extensionIndex"], r["uri"], r["scope"])
        for _, r, _ in records(content)
    ]


# -- the two routes, with exact provenance -------------------------------------


def test_a_drawing_extension_is_reported_with_exact_provenance() -> None:
    content = build(drawing=drawing_xml(shape(claims=alst(guid(1), body=LEAKY))))
    [(where, record, visibility)] = records(content)

    assert where == {
        "captureId": CAPTURE,
        "part": DRAWING_PART,
        "ownerPath": "/twoCellAnchor[0]/sp[0]/nvSpPr[0]/cNvPr[0]",
        "extensionIndex": 0,
        "sheetPart": SHEET_PART,
        "sheetId": "1",
        "sheet": "S1",
        "sheetState": "visible",
    }
    # Pinned by full equality so a field this route starts reporting -- or stops
    # -- fails here rather than passing unnoticed.
    assert record == {
        "kind": "unsupportedExtension",
        "owner": "drawing",
        "ownerPart": DRAWING_PART,
        "ownerPath": "/twoCellAnchor[0]/sp[0]/nvSpPr[0]/cNvPr[0]",
        "extensionIndex": 0,
        "scope": "sheet",
        "uri": guid(1),
        "uriForm": "guid",
        "payloadInterpreted": False,
        "refreshPerformed": False,
        "externalAccessPerformed": False,
    }
    assert visibility == "visible"
    # The object's own name is source text the author wrote; the path says where
    # the list sits and never what the thing holding it is called.
    assert "Shape 1" not in repr((where, record))


def test_a_chart_extension_is_reported_with_exact_provenance() -> None:
    content = build(chart=chart_xml(space=clst(guid(2), body=LEAKY)))
    [(where, record, visibility)] = records(content)

    assert where == {
        "captureId": CAPTURE,
        "part": CHART_PART,
        "ownerPath": "/",
        "extensionIndex": 0,
        "sheetPart": SHEET_PART,
        "sheetId": "1",
        "sheet": "S1",
        "sheetState": "visible",
    }
    assert record == {
        "kind": "unsupportedExtension",
        "owner": "chart",
        "ownerPart": CHART_PART,
        "ownerPath": "/",
        "extensionIndex": 0,
        "scope": "sheet",
        "uri": guid(2),
        "uriForm": "guid",
        "payloadInterpreted": False,
        "refreshPerformed": False,
        "externalAccessPerformed": False,
    }
    assert visibility == "visible"


# -- the owner matrix of both vocabularies -------------------------------------


def matrix_drawing() -> str:
    """Every owner the drawing vocabulary names, each with one list."""
    return drawing_xml(
        frame(claims=alst(guid(1)), frame_claims=alst(guid(2)))
        + shape(
            claims=alst(guid(3)),
            shape_claims=alst(guid(4)),
            properties=alst(guid(5)),
        )
        + f"<xdr:oneCellAnchor>{corners(4, 4)[: corners(4, 4).find('<xdr:to>')]}"
        '<xdr:ext cx="1" cy="1"/>'
        "<xdr:grpSp><xdr:nvGrpSpPr>"
        f'<xdr:cNvPr id="4" name="G">{alst(guid(6))}</xdr:cNvPr>'
        f"<xdr:cNvGrpSpPr>{alst(guid(7))}</xdr:cNvGrpSpPr></xdr:nvGrpSpPr>"
        f"<xdr:grpSpPr>{alst(guid(8))}</xdr:grpSpPr>"
        # A group holds groups: the same owners recur at any depth.
        "<xdr:pic><xdr:nvPicPr>"
        f'<xdr:cNvPr id="5" name="P">{alst(guid(9))}</xdr:cNvPr>'
        f"<xdr:cNvPicPr>{alst(guid(10))}</xdr:cNvPicPr></xdr:nvPicPr>"
        "</xdr:pic>"
        "<xdr:cxnSp><xdr:nvCxnSpPr>"
        f'<xdr:cNvPr id="6" name="X">{alst(guid(11))}</xdr:cNvPr>'
        f"<xdr:cNvCxnSpPr>{alst(guid(12))}</xdr:cNvCxnSpPr></xdr:nvCxnSpPr>"
        "</xdr:cxnSp>"
        "</xdr:grpSp>"
        f'<xdr:contentPart r:id="rIdC">{alst(guid(13), tag="xdr14")}'
        "</xdr:contentPart>"
        "<xdr:clientData/></xdr:oneCellAnchor>"
    )


DRAWING_PATHS = [
    "/twoCellAnchor[0]/graphicFrame[0]/nvGraphicFramePr[0]/cNvPr[0]",
    "/twoCellAnchor[0]/graphicFrame[0]/nvGraphicFramePr[0]/cNvGraphicFramePr[0]",
    "/twoCellAnchor[1]/sp[0]/nvSpPr[0]/cNvPr[0]",
    "/twoCellAnchor[1]/sp[0]/nvSpPr[0]/cNvSpPr[0]",
    "/twoCellAnchor[1]/sp[0]/spPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/nvGrpSpPr[0]/cNvPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/nvGrpSpPr[0]/cNvGrpSpPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/grpSpPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/pic[0]/nvPicPr[0]/cNvPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/pic[0]/nvPicPr[0]/cNvPicPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/cxnSp[0]/nvCxnSpPr[0]/cNvPr[0]",
    "/oneCellAnchor[0]/grpSp[0]/cxnSp[0]/nvCxnSpPr[0]/cNvCxnSpPr[0]",
    "/oneCellAnchor[0]/contentPart[0]",
]


def matrix_chart() -> str:
    """A chart owner of each kind the reader's own route already passes."""
    return chart_xml(
        title=clst(guid(21)),
        series=(
            f"<c:ser><c:idx val=\"1\"/><c:order val=\"1\"/>"
            "<c:cat><c:multiLvlStrRef><c:f>S!$A$1</c:f>"
            '<c:multiLvlStrCache><c:ptCount val="1"/><c:lvl>'
            '<c:pt idx="0"><c:v>Q1</c:v></c:pt></c:lvl>'
            f"{clst(guid(22))}</c:multiLvlStrCache>"
            f"{clst(guid(23))}</c:multiLvlStrRef></c:cat>"
            "<c:val><c:numLit>"
            f'<c:ptCount val="0"/>{clst(guid(24))}</c:numLit></c:val>'
            f"<c:marker>{clst(guid(25))}</c:marker>"
            f"<c:dPt><c:idx val=\"0\"/>{clst(guid(26))}</c:dPt>"
            f"{clst(guid(27))}</c:ser>"
            f"{clst(guid(28))}"
        ),
        plot=(
            f"<c:catAx><c:scaling>{clst(guid(29))}</c:scaling>"
            f"{clst(guid(30))}</c:catAx>"
            f"<c:dTable>{clst(guid(31))}</c:dTable>{clst(guid(32))}"
        ),
        properties=alst(guid(33)),
        space=f"<c:legend>{clst(guid(34))}</c:legend>{clst(guid(35))}",
    )


CHART_PATHS = [
    "/chart[0]/title[0]",
    (
        "/chart[0]/plotArea[0]/barChart[0]/ser[1]/cat[0]/multiLvlStrRef[0]"
        "/multiLvlStrCache[0]"
    ),
    "/chart[0]/plotArea[0]/barChart[0]/ser[1]/cat[0]/multiLvlStrRef[0]",
    "/chart[0]/plotArea[0]/barChart[0]/ser[1]/val[0]/numLit[0]",
    "/chart[0]/plotArea[0]/barChart[0]/ser[1]/marker[0]",
    "/chart[0]/plotArea[0]/barChart[0]/ser[1]/dPt[0]",
    "/chart[0]/plotArea[0]/barChart[0]/ser[1]",
    "/chart[0]/plotArea[0]/barChart[0]",
    "/chart[0]/plotArea[0]/catAx[0]/scaling[0]",
    "/chart[0]/plotArea[0]/catAx[0]",
    "/chart[0]/plotArea[0]/dTable[0]",
    "/chart[0]/plotArea[0]",
    "/spPr[0]",
    "/legend[0]",
    "/",
]


def test_every_named_owner_of_both_vocabularies_reports_in_source_order() -> None:
    """The drawing's owners, then the chart's; each list where it was authored."""
    content = build(drawing=matrix_drawing(), chart=matrix_chart())

    found = brief(content)
    assert [(r[0], r[1]) for r in found] == [
        *(("drawing", path) for path in DRAWING_PATHS),
        *(("chart", path) for path in CHART_PATHS),
    ]
    # Every list carried exactly one entry, in the order the guids were handed
    # out: a nested owner's list before the owner's own trailing one.
    assert [r[3] for r in found] == [guid(n) for n in (*range(1, 14), *range(21, 36))]
    assert {r[2] for r in found} == {0}
    assert {r[4] for r in found} == {"sheet"}
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == len(
        DRAWING_PATHS
    ) + len(CHART_PATHS)


def test_the_matrix_reports_identically_every_time() -> None:
    first = brief(build(drawing=matrix_drawing(), chart=matrix_chart()))
    assert brief(build(drawing=matrix_drawing(), chart=matrix_chart())) == first


def test_the_drawing_precedes_the_chart_it_reached() -> None:
    """Reach order, not part name: the drawing is opened to find the chart."""
    content = build(
        drawing=drawing_xml(frame(claims=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
    )
    assert [(r[0], r[3]) for r in brief(content)] == [
        ("drawing", guid(1)),
        ("chart", guid(2)),
    ]


# -- exact once per physical part ----------------------------------------------


def test_the_drawing_is_covered_once_though_two_readers_open_it() -> None:
    """The chart route and the object route both resolve the same drawing."""
    content = build(
        drawing=drawing_xml(frame(claims=alst(guid(1))) + shape(claims=alst(guid(2))))
    )
    found = records(content)

    assert [r["uri"] for _, r, _ in found] == [guid(1), guid(2)]
    assert {w["part"] for w, _, _ in found} == {DRAWING_PART}
    # The sheet still reports both of the drawing's objects, so both readers did
    # open the part.
    inventory = [
        item
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and (item.structured_value or {}).get("kind") != "unsupportedExtension"
    ]
    assert len(inventory) == 1
    # The chart route opened it too: the chart's title and its one cached value.
    roles = [i.semantic_role for i in extract_xlsx(content, capture_id=CAPTURE).segments]
    assert roles.count("chart") == 2


def test_a_drawing_and_chart_two_sheets_reach_are_shared_and_reported_once() -> None:
    content = build(
        drawing=drawing_xml(frame(claims=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
        sheets=2,
        relating=(1, 2),
    )
    found = records(content)

    assert [(r["owner"], r["uri"], r["scope"]) for _, r, _ in found] == [
        ("drawing", guid(1), "shared"),
        ("chart", guid(2), "shared"),
    ]
    # A shared part belongs to no one sheet, so no sheet's provenance is claimed.
    for where, _, _ in found:
        assert "sheet" not in where
        assert "sheetPart" not in where


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
def test_a_hidden_sheets_drawing_and_chart_are_hidden(state: str) -> None:
    content = build(
        drawing=drawing_xml(frame(claims=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
        states=(state,),
    )
    found = records(content)

    assert [v for _, _, v in found] == ["hidden", "hidden"]
    assert {w["sheetState"] for w, _, _ in found} == {state}


def test_a_part_one_hidden_and_one_visible_sheet_share_stays_visible() -> None:
    """Shared is hidden only where every sheet reaching it is."""

    def shared(states: tuple[str, ...]) -> list[str]:
        content = build(
            drawing=drawing_xml(frame(claims=alst(guid(1)))),
            chart=chart_xml(space=clst(guid(2))),
            sheets=2,
            relating=(1, 2),
            states=states,
        )
        return [v for _, _, v in records(content)]

    assert shared(("hidden", "visible")) == ["visible", "visible"]
    assert shared(("hidden", "hidden")) == ["hidden", "hidden"]


# -- namespaces: both profiles, and the lookalikes -----------------------------


def test_a_strict_drawing_and_chart_report_what_transitional_ones_do() -> None:
    def shape_of(xdr: str, a: str, c: str) -> list[tuple[object, ...]]:
        return brief(
            build(
                drawing=drawing_xml(
                    frame(claims=alst(guid(1), tag="a")) + shape(properties=alst(guid(2))),
                    xdr=xdr,
                    a=a,
                ),
                chart=chart_xml(space=clst(guid(3)), properties=alst(guid(4)), c=c, a=a),
            )
        )

    transitional = shape_of(XDR, A, C)
    assert len(transitional) == 4
    assert shape_of(XDR_STRICT, A_STRICT, C_STRICT) == transitional


def test_a_drawingml_list_is_taken_in_either_profile() -> None:
    """``a:extLst`` is one vocabulary's whichever profile the part is written in.

    That is the rule every other DrawingML route here already follows -- a
    blip, a graphic, a chart reference -- so a drawing written Transitional
    whose properties carry a Strict list still declares that list.
    """
    content = build(
        drawing=drawing_xml(
            shape(properties=f'<s:extLst xmlns:s="{A_STRICT}"><s:ext uri="{guid(1)}"/></s:extLst>')
        )
    )
    assert [r[3] for r in brief(content)] == [guid(1)]


def test_a_nested_drawingml_owner_is_reported_before_its_parent_owner() -> None:
    content = build(
        drawing=drawing_xml(
            shape(properties=f"<a:ln>{alst(guid(1))}</a:ln>{alst(guid(2))}")
        )
    )

    assert brief(content) == [
        (
            "drawing",
            "/twoCellAnchor[0]/sp[0]/spPr[0]/ln[0]",
            0,
            guid(1),
            "sheet",
        ),
        (
            "drawing",
            "/twoCellAnchor[0]/sp[0]/spPr[0]",
            0,
            guid(2),
            "sheet",
        ),
    ]


def test_a_transform_extent_is_not_an_extension_entry() -> None:
    properties = (
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
    )
    assert records(build(drawing=drawing_xml(shape(properties=properties)))) == []


def test_a_foreign_graphic_payload_is_not_entered() -> None:
    foreign = (
        '<a:graphicData uri="urn:diagram"><dgm:relIds xmlns:dgm="urn:diagram">'
        f'<dgm:extLst><dgm:ext uri="{guid(1)}"/></dgm:extLst>'
        "</dgm:relIds></a:graphicData>"
    )
    properties = f"<a:graphic>{foreign}</a:graphic>"
    assert records(build(drawing=drawing_xml(shape(properties=properties)))) == []


def test_a_nested_drawingml_owner_refuses_a_second_list() -> None:
    text = (
        "<xdr:txBody><a:bodyPr>"
        f"{alst(guid(1))}{alst(guid(2))}"
        "</a:bodyPr><a:lstStyle/><a:p/></xdr:txBody>"
    )
    drawing = drawing_xml(shape().replace("</xdr:sp>", f"{text}</xdr:sp>"))

    with pytest.raises(
        OOXMLInvalid, match="XLSX drawing bodyPr holds more than one extension list"
    ):
        extract_xlsx(build(drawing=drawing), capture_id=CAPTURE)


def test_a_nested_drawingml_owner_requires_its_own_profile() -> None:
    mismatch = (
        f'<a:bodyPr><s:extLst xmlns:s="{A_STRICT}">'
        f'<s:ext uri="{guid(1)}"/></s:extLst></a:bodyPr>'
    )
    assert records(build(drawing=drawing_xml(shape(properties=mismatch)))) == []

    strict = (
        f'<s:bodyPr xmlns:s="{A_STRICT}"><s:extLst>'
        f'<s:ext uri="{guid(2)}"/></s:extLst></s:bodyPr>'
    )
    assert [item[3] for item in brief(build(drawing=drawing_xml(shape(properties=strict))))] == [
        guid(2)
    ]


@pytest.mark.parametrize(
    "properties",
    [
        # A list of a foreign vocabulary at a real owner.
        f'<f:extLst xmlns:f="urn:foreign"><f:ext uri="{guid(1)}"/></f:extLst>',
        # Stripped of its namespace altogether.
        f'<extLst xmlns=""><ext uri="{guid(1)}"/></extLst>',
        # The spreadsheet-drawing namespace where the schema types the list from
        # DrawingML main: an own-namespace lookalike, never entered either.
        f'<xdr:extLst><xdr:ext uri="{guid(1)}"/></xdr:extLst>',
        # The chart vocabulary, which types no list at a drawing owner.
        f'<c:extLst><c:ext uri="{guid(1)}"/></c:extLst>',
        # The right list holding an entry of another vocabulary.
        f'<a:extLst><f:ext xmlns:f="urn:foreign" uri="{guid(1)}"/></a:extLst>',
        # The right list holding the other profile's entry.
        f'<a:extLst><s:ext xmlns:s="{A_STRICT}" uri="{guid(1)}"/></a:extLst>',
        # A list nested one step below the owner rather than directly under it.
        f'<a:xfrm>{alst(guid(1))}</a:xfrm>',
    ],
)
def test_drawing_lookalikes_declare_nothing(properties: str) -> None:
    assert records(build(drawing=drawing_xml(shape(properties=properties)))) == []


@pytest.mark.parametrize(
    "space",
    [
        f'<f:extLst xmlns:f="urn:foreign"><f:ext uri="{guid(1)}"/></f:extLst>',
        f'<extLst xmlns=""><ext uri="{guid(1)}"/></extLst>',
        # The other chart profile than the root's: a chart's own list is its
        # root's namespace, as a SpreadsheetML part's is.
        f'<s:extLst xmlns:s="{C_STRICT}"><s:ext uri="{guid(1)}"/></s:extLst>',
        # DrawingML main at an owner the schema gives a chart list.
        f'<a:extLst><a:ext uri="{guid(1)}"/></a:extLst>',
        f'<c:extLst><f:ext xmlns:f="urn:foreign" uri="{guid(1)}"/></c:extLst>',
        f'<c:extLst><s:ext xmlns:s="{C_STRICT}" uri="{guid(1)}"/></c:extLst>',
    ],
)
def test_chart_lookalikes_declare_nothing(space: str) -> None:
    assert records(build(chart=chart_xml(space=space))) == []


@pytest.mark.parametrize(
    ("where", "body"),
    [
        # The drawing root: ``CT_Drawing`` has no extension point at all.
        ("drawing-root", alst(guid(1))),
        # An anchor, and a corner inside one: neither is an owner.
        ("anchor", None),
        ("chart-point", None),
    ],
)
def test_a_list_at_a_position_the_schema_gives_none_declares_nothing(
    where: str, body: str | None
) -> None:
    if where == "drawing-root":
        content = build(drawing=drawing_xml(f"{shape()}{body}"))
    elif where == "anchor":
        content = build(
            drawing=drawing_xml(
                f"<xdr:twoCellAnchor>{corners()}{alst(guid(1))}{shape()[len('<xdr:twoCellAnchor>') + len(corners(2, 2)) :]}"
            )
        )
    else:
        # ``c:pt`` and ``c:v`` carry no extension point; neither does ``c:f``.
        content = build(
            chart=chart_xml().replace(
                "<c:v>12</c:v>", f"<c:v>12</c:v>{clst(guid(1))}"
            )
        )
    assert records(content) == []


def test_a_list_inside_an_entrys_own_body_is_payload_not_a_second_entry() -> None:
    nested = alst(guid(1), body=alst(guid(2), guid(3)))
    assert [r[3] for r in brief(build(drawing=drawing_xml(shape(properties=nested))))] == [
        guid(1)
    ]
    in_chart = clst(guid(1), body=clst(guid(2)) + alst(guid(3)))
    assert [r[3] for r in brief(build(chart=chart_xml(space=in_chart)))] == [guid(1)]


def test_a_foreign_root_namespace_is_refused_rather_than_covered() -> None:
    """The root check is the readers' own and comes first, so nothing is read."""
    with pytest.raises(OOXMLInvalid, match="drawing part uses an unsupported namespace"):
        extract_xlsx(
            build(drawing=drawing_xml(shape(properties=alst(guid(1))), xdr="urn:foreign")),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="chart part uses an unsupported namespace"):
        extract_xlsx(
            build(chart=chart_xml(space=clst(guid(1)), c="urn:foreign")),
            capture_id=CAPTURE,
        )


# -- only the parts the readers actually open -----------------------------------


def test_a_drawing_no_sheet_names_is_never_scanned() -> None:
    content = build(
        drawing=drawing_xml(shape(properties=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
        declare_drawing=False,
    )
    assert records(content) == []


def test_a_chart_no_frame_relates_is_never_scanned() -> None:
    """The drawing is opened; its one object relates no chart, so none is."""
    content = build(
        drawing=drawing_xml(frame(chart=False, claims=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
    )
    assert [r[3] for r in brief(content)] == [guid(1)]


def test_a_member_that_merely_looks_like_a_drawing_is_not_scanned() -> None:
    content = build(
        extra={
            "xl/drawings/drawing9.xml": drawing_xml(shape(properties=alst(guid(9)))),
            "xl/charts/chart9.xml": chart_xml(space=clst(guid(8))),
        }
    )
    assert records(content) == []


# -- one list per position ------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "drawing", "chart"),
    [
        (
            "XLSX drawing cNvPr holds more than one extension list",
            lambda: drawing_xml(shape(claims=alst(guid(1)) + alst(guid(2)))),
            None,
        ),
        (
            "XLSX drawing spPr holds more than one extension list",
            lambda: drawing_xml(shape(properties="<a:extLst/>" + alst(guid(2)))),
            None,
        ),
        (
            "XLSX chart holds more than one extension list",
            None,
            lambda: chart_xml(space=clst(guid(1)) + clst(guid(2))),
        ),
        (
            "XLSX chart ser holds more than one extension list",
            None,
            lambda: chart_xml(
                series="<c:ser><c:idx val=\"1\"/><c:order val=\"1\"/>"
                + clst(guid(1))
                + clst(guid(2))
                + "</c:ser>"
            ),
        ),
    ],
    ids=["drawing-cNvPr", "drawing-spPr", "chart-root", "chart-series"],
)
def test_a_second_list_at_one_position_is_refused(
    message: str, drawing: object, chart: object
) -> None:
    content = build(
        drawing=drawing() if drawing is not None else None,  # type: ignore[operator]
        chart=chart() if chart is not None else None,  # type: ignore[operator]
    )
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


def test_a_list_below_a_list_is_no_second_list_at_the_same_position() -> None:
    """Each position is counted on its own, as in the SpreadsheetML walk."""
    content = build(
        drawing=drawing_xml(
            shape(claims=alst(guid(1)), shape_claims=alst(guid(2)), properties=alst(guid(3)))
        )
    )
    assert [r[3] for r in brief(content)] == [guid(1), guid(2), guid(3)]


# -- bounds, every one of them before a record exists ---------------------------


def watched(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every call that would build a coverage record."""
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    return built


def test_a_list_over_the_per_list_bound_is_refused_before_any_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = watched(monkeypatch)
    monkeypatch.setattr(ox, "_MAX_XLSX_EXTENSIONS", 2)

    with pytest.raises(OOXMLInvalid, match="XLSX drawing declares too many extensions"):
        extract_xlsx(
            build(drawing=drawing_xml(shape(properties=alst(guid(1), guid(2), guid(3))))),
            capture_id=CAPTURE,
        )
    assert built == []

    with pytest.raises(OOXMLInvalid, match="XLSX chart declares too many extensions"):
        extract_xlsx(
            build(chart=chart_xml(space=clst(guid(1), guid(2), guid(3)))),
            capture_id=CAPTURE,
        )
    assert built == []


def test_the_per_part_bound_spans_every_owner_of_one_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = watched(monkeypatch)
    monkeypatch.setattr(ox, "_MAX_XLSX_PART_EXTENSIONS", 3)
    # Four lists of one entry each: inside the per-list bound, over the part's.
    with pytest.raises(OOXMLInvalid, match="XLSX drawing declares too many extensions"):
        extract_xlsx(
            build(
                drawing=drawing_xml(
                    shape(
                        claims=alst(guid(1)),
                        shape_claims=alst(guid(2)),
                        properties=alst(guid(3)),
                    )
                    + frame(claims=alst(guid(4)))
                )
            ),
            capture_id=CAPTURE,
        )
    assert built == []

    with pytest.raises(OOXMLInvalid, match="XLSX chart declares too many extensions"):
        extract_xlsx(
            build(
                chart=chart_xml(
                    title=clst(guid(1)),
                    plot=clst(guid(2)),
                    properties=alst(guid(3)),
                    space=clst(guid(4)),
                )
            ),
            capture_id=CAPTURE,
        )
    assert built == []


def test_the_global_bound_counts_the_drawing_and_the_chart_with_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = watched(monkeypatch)
    content = build(
        drawing=drawing_xml(frame(claims=alst(guid(1)))),
        chart=chart_xml(space=clst(guid(2))),
        # The worksheet's own list too, so all three vocabularies are summed.
        extra={
            SHEET_PART: (
                f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}"><sheetData>'
                '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
                f'</sheetData><drawing r:id="rIdD"/><extLst><ext uri="{guid(3)}"/>'
                "</extLst></worksheet>"
            )
        },
    )
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 2)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_xlsx(content, capture_id=CAPTURE)
    assert built == []

    # One cell, one chart value, one drawing object and the three entries.
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 6)
    assert len(records(content)) == 3


# -- the payload is inert -------------------------------------------------------


def loaded(*, payload: str) -> bytes:
    """Both parts, each owner carrying the same imitating payload."""
    return build(
        drawing=drawing_xml(
            frame(claims=alst(guid(1), body=payload))
            + shape(properties=alst(guid(2), body=payload))
        ),
        chart=chart_xml(
            title=clst(guid(3), body=payload),
            plot=clst(guid(4), body=payload),
            properties=alst(guid(5), body=payload),
            space=clst(guid(6), body=payload),
        ),
    )


def test_an_extension_payload_changes_nothing_but_the_coverage() -> None:
    plain = extract_xlsx(loaded(payload=""), capture_id=CAPTURE)
    carrying = extract_xlsx(loaded(payload=LEAKY), capture_id=CAPTURE)

    def same(item: object) -> tuple[object, ...]:
        return (
            item.semantic_role,  # type: ignore[attr-defined]
            dict(item.source_locator),  # type: ignore[attr-defined]
            item.text,  # type: ignore[attr-defined]
            item.structured_value,  # type: ignore[attr-defined]
            item.visibility,  # type: ignore[attr-defined]
        )

    def rest(result: object) -> list[tuple[object, ...]]:
        return [
            same(item)
            for item in result.segments  # type: ignore[attr-defined]
            if (item.structured_value or {}).get("kind") != "unsupportedExtension"
        ]

    # No anchor, object, image reference, connector endpoint, chart value,
    # series or title run came out of the payload: the chart's title and its one
    # cached value, and the drawing's one object, are all there is either way.
    assert rest(carrying) == rest(plain)
    assert [r[0] for r in rest(plain)] == [
        "table_cell",
        "chart",
        "chart",
        "object_inventory",
    ]
    assert carrying.unit_type == plain.unit_type == "workbook_cell"
    assert carrying.expected == plain.expected == 1
    assert {k: v for k, v in carrying.limitations.items() if k != "extensions"} == {
        k: v for k, v in plain.limitations.items() if k != "extensions"
    }
    assert plain.limitations["extensions"] == carrying.limitations["extensions"] == 6


def test_no_payload_body_attribute_or_secret_is_ever_reported() -> None:
    content = loaded(payload=LEAKY)
    reported = repr(records(content))

    assert len(records(content)) == 6
    for secret in SECRETS:
        assert secret not in reported


def test_a_filtered_series_parked_in_an_extension_is_not_a_plotted_value() -> None:
    """A real producer keeps filtered series in ``c:extLst``; none is plotted."""
    filtered = (
        '<c15:filteredBarSeries xmlns:c15="urn:c15">'
        f'<c15:ser xmlns:c="{C}">{SERIES[len("<c:ser>") : -len("</c:ser>")]}'
        "</c15:ser>"
        f'{SERIES.replace("<c:v>12</c:v>", "<c:v>cached-value-42</c:v>")}'
        "</c15:filteredBarSeries>"
    )
    content = build(chart=chart_xml(space=clst(guid(1), body=filtered)))
    result = extract_xlsx(content, capture_id=CAPTURE)

    values = [item.text for item in result.segments if item.semantic_role == "chart"]
    assert values == ["Quarterly", "12"]
    assert "cached-value-42" not in repr([i.text for i in result.segments])
    assert result.limitations["extensions"] == 1


def test_a_title_run_inside_an_extension_is_no_part_of_the_title() -> None:
    content = build(
        chart=chart_xml().replace(
            "</c:rich>",
            f'<a:p><a:pPr>{alst(guid(1), body="<a:r><a:t>LEAK</a:t></a:r>")}</a:pPr>'
            "</a:p></c:rich>",
        )
    )
    result = extract_xlsx(content, capture_id=CAPTURE)

    titles = [
        item.text
        for item in result.segments
        if item.source_locator.get("chartRole") == "chart_title"
    ]
    assert titles == ["Quarterly"]
    [(where, record, _visibility)] = records(content)
    assert where["ownerPath"] == "/chart[0]/title[0]/tx[0]/rich[0]/p[1]/pPr[0]"
    assert record["uri"] == guid(1)
    assert result.limitations["extensions"] == 1
    assert "LEAK" not in repr((where, record))


def test_nothing_outside_the_package_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof by refusal: every way out of the process is made to raise."""
    import subprocess
    import urllib.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the drawing extension coverage reached outside")

    content = loaded(payload=LEAKY)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    assert len(records(content)) == 6


# -- the record's own shape -----------------------------------------------------


def test_coverage_is_inert_inventory_and_never_a_cell_or_a_chart_value() -> None:
    result = extract_xlsx(
        build(drawing=matrix_drawing(), chart=matrix_chart()), capture_id=CAPTURE
    )
    entries = [
        item
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]

    assert len(entries) == len(DRAWING_PATHS) + len(CHART_PATHS)
    for item in entries:
        assert item.semantic_role == "object_inventory"
        assert item.extraction_method == "inventory"
        assert item.text is None
        assert "cell" not in item.source_locator
        assert "chartRole" not in item.source_locator
        assert "anchorIndex" not in item.source_locator
        value = item.structured_value or {}
        assert value["payloadInterpreted"] is False
        assert value["refreshPerformed"] is False
        assert value["externalAccessPerformed"] is False


def test_an_entry_keeps_its_authored_place_and_its_uri_is_classified() -> None:
    content = build(
        drawing=drawing_xml(
            shape(
                properties=alst(
                    "https://example.invalid/secret?token=abc123",
                    None,
                    guid(1),
                    f"  {guid(1).lower()}\n",
                )
            )
        )
    )
    found = records(content)

    assert [w["extensionIndex"] for w, _, _ in found] == [0, 1, 2, 3]
    assert [(r["uri"], r["uriForm"]) for _, r, _ in found] == [
        (None, "other"),
        (None, None),
        (guid(1), "guid"),
        (guid(1).lower(), "guid"),
    ]
    assert "token=abc123" not in repr(found)


@pytest.mark.parametrize(
    "properties", ["", "<a:extLst/>", "<a:extLst></a:extLst>", "<a:extLst><!-- --></a:extLst>"]
)
def test_an_absent_or_empty_list_reports_nothing(properties: str) -> None:
    content = build(drawing=drawing_xml(shape(properties=properties)))
    result = extract_xlsx(content, capture_id=CAPTURE)

    assert records(content) == []
    assert result.limitations["extensions"] == 0
