"""XLSX worksheet drawings: an inventory of the objects a sheet anchors.

A worksheet relates one drawing part and that drawing anchors objects --
pictures, shapes, connectors, groups, graphic frames and content parts. The
chart route already reports the one kind that holds values; this is everything
else, as inventory: what each object claims about itself, and the references it
encodes, and nothing behind either.

So the contract here is deliberately narrow. A record names the object's kind
and index, the anchor showing it and where that anchor puts it, the drawing part
and the relationship the sheet reached it by, and the ``cNvPr`` claims its
author wrote. A picture adds the image part it embeds or the target it links,
reported without the part being opened, decoded, OCRed or fetched. A connector
adds the two endpoint claims it wrote, as claims and never as edges. No shape
text is read, no graph is built, and a graphic frame already reported as chart
records is not reported again here.
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
SHEET_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"
CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
DRAWING_PART = "xl/drawings/drawing1.xml"
CHART_PART = "xl/charts/chart1.xml"
IMAGE_PART = "xl/media/image1.png"
# Not a PNG and never read as one: the inventory reports the type the package
# declared and the length it already measured, so a reader that decoded the
# bytes would have nothing to decode.
IMAGE = "eight..."
EXTERNAL_IMAGE = "https://example.invalid/brand/logo.png"


def two_cell(body: str, column: int = 0, row: int = 0) -> str:
    """One two-cell anchor holding ``body``, three columns by four rows."""
    return (
        "<xdr:twoCellAnchor>"
        f"<xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{column + 2}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{row + 3}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        f"{body}<xdr:clientData/></xdr:twoCellAnchor>"
    )


# A picture claiming all four of its author's fields. ``descr`` is written empty
# on purpose: a description declared blank is not a description never declared.
PIC = (
    "<xdr:pic>"
    "<xdr:nvPicPr>"
    '<xdr:cNvPr id="2" name="Picture 2" title="Logo" descr=""/>'
    "<xdr:cNvPicPr/>"
    "</xdr:nvPicPr>"
    '<xdr:blipFill><a:blip r:embed="rIdI"/><a:stretch/></xdr:blipFill>'
    "<xdr:spPr/>"
    "</xdr:pic>"
)
# A shape holding text, so a reader that read shape text would show it here.
SP = (
    "<xdr:sp>"
    '<xdr:nvSpPr><xdr:cNvPr id="3" name="Rounded Rectangle 3"/>'
    "<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr/>"
    "<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>Not read here</a:t></a:r></a:p>"
    "</xdr:txBody>"
    "</xdr:sp>"
)
# A connector attached at both ends, to the shape above and the picture above
# that. The ids stay claims: nothing resolves them to either object.
CXN = (
    "<xdr:cxnSp>"
    '<xdr:nvCxnSpPr><xdr:cNvPr id="4" name="Straight Arrow Connector 4"/>'
    '<xdr:cNvCxnSpPr><a:stCxn id="3" idx="2"/><a:endCxn id="2" idx="0"/>'
    "</xdr:cNvCxnSpPr></xdr:nvCxnSpPr><xdr:spPr/>"
    "</xdr:cxnSp>"
)
# A graphic frame showing a table rather than a chart, so the chart route passes
# it over and this one reports it.
FRAME = (
    '<xdr:graphicFrame macro="">'
    '<xdr:nvGraphicFramePr><xdr:cNvPr id="5" name="Table 5"/>'
    "<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm/>"
    f'<a:graphic><a:graphicData uri="{TABLE_URI}"><a:tbl/></a:graphicData>'
    "</a:graphic></xdr:graphicFrame>"
)
# A group, and a member inside it. The member is not a direct object of the
# anchor, so the group is inventoried and its member is not.
GRP = (
    "<xdr:grpSp>"
    '<xdr:nvGrpSpPr><xdr:cNvPr id="6" name="Group 6"/>'
    "<xdr:cNvGrpSpPr/></xdr:nvGrpSpPr><xdr:grpSpPr/>"
    '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="7" name="Nested 7"/>'
    "<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr/></xdr:sp>"
    "</xdr:grpSp>"
)
# A content part claims nothing about itself and its reference is not resolved,
# which is why no relationship by this id exists in the package at all.
CONTENT = '<xdr:contentPart r:id="rIdNowhere"/>'
# A graphic frame showing a chart, reported by the chart route and not here.
CHART_FRAME = (
    '<xdr:graphicFrame macro="">'
    '<xdr:nvGraphicFramePr><xdr:cNvPr id="8" name="Chart 8"/>'
    "<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm/>"
    f'<a:graphic><a:graphicData uri="{CHART_NS}">'
    f'<c:chart xmlns:c="{CHART_NS}" r:id="rIdC"/>'
    "</a:graphicData></a:graphic></xdr:graphicFrame>"
)
OBJECTS = (PIC, SP, CXN, FRAME, GRP, CONTENT, CHART_FRAME)


def wsDr(anchors: str) -> str:
    return (
        f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}"'
        f' xmlns:r="{RELNS}">{anchors}</xdr:wsDr>'
    )


# Each object on its own anchor, two columns further along than the last, so a
# record's anchor and its placement are visibly its own rather than shared.
DRAWING = wsDr(
    "".join(two_cell(body, column=index * 2) for index, body in enumerate(OBJECTS))
)
CHART = (
    f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}"><c:chart>'
    "<c:title><c:tx><c:rich><a:p><a:r><a:t>Totals</a:t></a:r></a:p>"
    "</c:rich></c:tx></c:title><c:plotArea/></c:chart></c:chartSpace>"
)
SHEET = (
    f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}">'
    '<sheetData/><drawing r:id="rIdD"/></worksheet>'
)
DRAWING_RELS = (
    "<Relationships>"
    f'<Relationship Id="rIdI" Target="../media/image1.png" Type="{RELNS}/image"/>'
    f'<Relationship Id="rIdC" Target="../charts/chart1.xml" Type="{RELNS}/chart"/>'
    "</Relationships>"
)


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def xlsx(
    drawing: str = DRAWING,
    *,
    rels: str = DRAWING_RELS,
    state: str = "",
    extra: dict[str, str] | None = None,
) -> bytes:
    """One sheet relating one drawing that anchors each kind of object once."""
    members = {
        "[Content_Types].xml": (
            "<Types>"
            '<Default Extension="png" ContentType="image/png"/>'
            f'<Override PartName="/{DRAWING_PART}" ContentType="{DRAWING_CT}"/>'
            f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
            f'<sheet name="Sales" sheetId="7" r:id="rId1"{state}/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            "</Relationships>"
        ),
        SHEET_PART: SHEET,
        "xl/worksheets/_rels/sheet1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdD" Target="../drawings/drawing1.xml"'
            f' Type="{RELNS}/drawing"/>'
            "</Relationships>"
        ),
        DRAWING_PART: drawing,
        "xl/drawings/_rels/drawing1.xml.rels": rels,
        CHART_PART: CHART,
        IMAGE_PART: IMAGE,
        **(extra or {}),
    }
    return archive(members)


def inventory(content: bytes) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Every drawing-object inventory record, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and "drawingObjectType" in item.source_locator
    ]


# The route and place every record carries. Each one differs from this only in
# the anchor it sits on, where that anchor is, and which object it is.
WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "sheetPart": SHEET_PART,
    "sheetId": "7",
    "sheet": "Sales",
    "sheetState": "visible",
    "drawingPart": DRAWING_PART,
    "drawingRelationshipId": "rIdD",
    "anchorIndex": 0,
    "anchorType": "twoCell",
    "anchorRange": "A1:C4",
    "drawingObjectIndex": 0,
    "drawingObjectType": "pic",
}
# What a picture naming no image at all reports, so an embed or a link is
# visibly the half of the record it filled in.
NO_IMAGE: dict[str, object] = {
    "imageEmbedRelationshipId": None,
    "imageEmbedRelationshipType": None,
    "imagePart": None,
    "imageContentType": None,
    "imageBytes": None,
    "imageLinkRelationshipId": None,
    "imageLinkRelationshipType": None,
    "imageLinkTarget": None,
    "imageLinkScheme": None,
    "imageLinkExternal": None,
}
EMBEDDED: dict[str, object] = NO_IMAGE | {
    "imageEmbedRelationshipId": "rIdI",
    "imageEmbedRelationshipType": f"{RELNS}/image",
    "imagePart": IMAGE_PART,
    "imageContentType": "image/png",
    "imageBytes": len(IMAGE),
}
NO_ENDS: dict[str, object] = {
    "connectionStartId": None,
    "connectionStartIndex": None,
    "connectionEndId": None,
    "connectionEndIndex": None,
}
# Each object's record, in the order the drawing anchored them. The chart frame
# is absent: it is the only object on the seventh anchor and reported by the
# chart route, so no inventory record is emitted for that anchor.
EXPECTED: list[tuple[dict[str, object], dict[str, object]]] = [
    (
        WHERE,
        {
            "shapeId": "2",
            "shapeName": "Picture 2",
            "altTitle": "Logo",
            "altDescription": "",
            **EMBEDDED,
        },
    ),
    (
        WHERE
        | {
            "anchorIndex": 1,
            "anchorRange": "C1:E4",
            "drawingObjectIndex": 0,
            "drawingObjectType": "sp",
        },
        {
            "shapeId": "3",
            "shapeName": "Rounded Rectangle 3",
            "altTitle": None,
            "altDescription": None,
        },
    ),
    (
        WHERE
        | {
            "anchorIndex": 2,
            "anchorRange": "E1:G4",
            "drawingObjectIndex": 0,
            "drawingObjectType": "cxnSp",
        },
        {
            "shapeId": "4",
            "shapeName": "Straight Arrow Connector 4",
            "altTitle": None,
            "altDescription": None,
            "connectionStartId": "3",
            "connectionStartIndex": "2",
            "connectionEndId": "2",
            "connectionEndIndex": "0",
        },
    ),
    (
        WHERE
        | {
            "anchorIndex": 3,
            "anchorRange": "G1:I4",
            "drawingObjectIndex": 0,
            "drawingObjectType": "graphicFrame",
        },
        {
            "shapeId": "5",
            "shapeName": "Table 5",
            "altTitle": None,
            "altDescription": None,
        },
    ),
    (
        WHERE
        | {
            "anchorIndex": 4,
            "anchorRange": "I1:K4",
            "drawingObjectIndex": 0,
            "drawingObjectType": "grpSp",
        },
        {
            "shapeId": "6",
            "shapeName": "Group 6",
            "altTitle": None,
            "altDescription": None,
        },
    ),
    (
        WHERE
        | {
            "anchorIndex": 5,
            "anchorRange": "K1:M4",
            "drawingObjectIndex": 0,
            "drawingObjectType": "contentPart",
        },
        {
            "shapeId": None,
            "shapeName": None,
            "altTitle": None,
            "altDescription": None,
        },
    ),
]


def test_a_worksheet_drawing_inventories_every_object_it_anchors() -> None:
    """Six objects, in source order, each as the record its own kind reports.

    Every record names the whole route and the object's place on it, and each
    kind adds exactly what its schema route provides and nothing else: the
    picture its image, the connector its two endpoints, the others neither. The
    shape's text and the group's member are both in the fixture and in neither
    record, and the content part claims nothing because it has nowhere to.
    """
    assert inventory(xlsx()) == EXPECTED


def test_every_inventory_record_is_reported_as_inert_inventory() -> None:
    """An object is metadata the sheet shows, so it carries no content."""
    records = [
        item
        for item in extract_xlsx(xlsx(), capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]

    assert [item.extraction_method for item in records] == ["inventory"] * 6
    assert [item.visibility for item in records] == ["visible"] * 6
    assert [item.text for item in records] == [None] * 6


def test_a_chart_frame_is_reported_as_chart_records_and_never_as_inventory() -> None:
    """The chart route and this one divide the frames between them.

    The chart frame is the seventh object on the seventh anchor, so a duplicate
    would show up as a ``graphicFrame`` record on ``anchorIndex`` 6. The chart
    route reports that anchor instead, and the object index the frame consumed
    is simply not reported, so the five indices before it keep the positions
    they had.
    """
    segments = extract_xlsx(xlsx(), capture_id=CAPTURE).segments

    assert [
        item.source_locator["anchorIndex"]
        for item in segments
        if item.semantic_role == "chart"
    ] == [6]
    assert 6 not in {locator["anchorIndex"] for locator, _ in inventory(xlsx())}
    assert [locator["drawingObjectIndex"] for locator, _ in inventory(xlsx())] == [
        0
    ] * 6


def test_a_sized_anchor_reports_its_object_with_the_geometry_it_was_placed_by() -> None:
    """A one-cell anchor names one cell and a size, and the record keeps both."""
    drawing = wsDr(
        "<xdr:oneCellAnchor>"
        "<xdr:from><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff>"
        "<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f'<xdr:ext cx="914400" cy="457200"/>{SP}'
        "</xdr:oneCellAnchor>"
    )

    locator, _ = inventory(xlsx(drawing))[0]
    assert locator == WHERE | {
        "anchorType": "oneCell",
        "anchorRange": "C4",
        "anchorGeometry": {"cx": 914400, "cy": 457200},
        "drawingObjectType": "sp",
    }


def test_an_object_on_a_hidden_sheet_keeps_the_sheet_s_state_and_visibility() -> None:
    """A hidden sheet hides what it shows; the state is reported either way."""
    records = [
        item
        for item in extract_xlsx(
            xlsx(state=' state="hidden"'), capture_id=CAPTURE
        ).segments
        if item.semantic_role == "object_inventory"
    ]

    assert [item.visibility for item in records] == ["hidden"] * 6
    assert {item.source_locator["sheetState"] for item in records} == {"hidden"}


# The Strict profile's spelling of every base URI the fixture's route is written
# on. The content types are shared between the profiles, so none is rewritten.
STRICT_URIS = {
    MAIN: "http://purl.oclc.org/ooxml/spreadsheetml/main",
    RELNS: "http://purl.oclc.org/ooxml/officeDocument/relationships",
    SHEET_DRAWING_NS: "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
    DRAWING_NS: "http://purl.oclc.org/ooxml/drawingml/main",
    CHART_NS: "http://purl.oclc.org/ooxml/drawingml/chart",
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


def test_the_strict_spelling_of_the_fixture_reports_the_same_inventory() -> None:
    """Respelling the whole route changes nothing but the names on it.

    Every namespace this route reads is rewritten at once -- the worksheet's
    own, the relationships attributes, the spreadsheet drawing the objects are
    written in, the DrawingML the blip and the endpoints are, and the chart
    vocabulary the deduplicated frame names -- and the same six records come
    back, relationship types included.
    """
    # The relationship *type* is the one thing on a record that is itself a URI
    # the profile respells, so it is the one expected value that changes.
    expected = [
        (
            locator,
            record
            | (
                {"imageEmbedRelationshipType": f"{STRICT_URIS[RELNS]}/image"}
                if record.get("imageEmbedRelationshipType") is not None
                else {}
            ),
        )
        for locator, record in EXPECTED
    ]

    assert inventory(strict_profile(xlsx())) == expected


LINKED_PIC = (
    "<xdr:pic>"
    '<xdr:nvPicPr><xdr:cNvPr id="2" name="Picture 2"/><xdr:cNvPicPr/>'
    "</xdr:nvPicPr>"
    '<xdr:blipFill><a:blip r:link="rIdL"/></xdr:blipFill><xdr:spPr/>'
    "</xdr:pic>"
)
BOTH_PIC = LINKED_PIC.replace('r:link="rIdL"', 'r:embed="rIdI" r:link="rIdL"')
BARE_PIC = LINKED_PIC.replace('<a:blip r:link="rIdL"/>', "<a:srcRect/>")
LINK_RELS = DRAWING_RELS.replace(
    "</Relationships>",
    f'<Relationship Id="rIdL" Target="{EXTERNAL_IMAGE}" TargetMode="External"'
    f' Type="{RELNS}/image"/></Relationships>',
)
LINKED: dict[str, object] = NO_IMAGE | {
    "imageLinkRelationshipId": "rIdL",
    "imageLinkRelationshipType": f"{RELNS}/image",
    "imageLinkTarget": EXTERNAL_IMAGE,
    "imageLinkScheme": "https",
    "imageLinkExternal": True,
}


@pytest.mark.parametrize(
    ("pic", "image"),
    [
        (LINKED_PIC, LINKED),
        (
            BOTH_PIC,
            EMBEDDED
            | {key: value for key, value in LINKED.items() if value is not None},
        ),
        (BARE_PIC, NO_IMAGE),
    ],
    ids=["linked", "embedded-and-linked", "neither"],
)
def test_a_picture_reports_the_image_it_names_without_reaching_it(
    pic: str, image: dict[str, object]
) -> None:
    """An embed and a link are two declarations, each kept in its own fields.

    The external target is carried exactly as the package wrote it, beside the
    scheme its own text names, and nothing resolves, fetches or probes it -- the
    host does not exist, so a reader that tried would not come back with this.
    A picture may name both, and one that names neither says so rather than
    borrowing the other picture's image.
    """
    assert inventory(xlsx(wsDr(two_cell(pic)), rels=LINK_RELS)) == [
        (
            WHERE,
            {
                "shapeId": "2",
                "shapeName": "Picture 2",
                "altTitle": None,
                "altDescription": None,
                **image,
            },
        )
    ]


BOTH_ENDS = '<a:stCxn id="3" idx="2"/><a:endCxn id="2" idx="0"/>'


@pytest.mark.parametrize(
    ("endpoints", "declared"),
    [
        (
            '<a:stCxn id="3" idx="2"/>',
            {"connectionStartId": "3", "connectionStartIndex": "2"},
        ),
        (
            '<a:endCxn id="2" idx="0"/>',
            {"connectionEndId": "2", "connectionEndIndex": "0"},
        ),
        ("", {}),
        # No connector properties at all, which says as little as empty ones.
        (None, {}),
    ],
    ids=["start-only", "end-only", "neither", "no-properties"],
)
def test_a_connector_keeps_an_endpoint_it_never_declared_as_absent(
    endpoints: str | None, declared: dict[str, object]
) -> None:
    """One end attached and the other loose is a connector, not a defect.

    An absent endpoint stays ``None`` rather than becoming an attachment to
    shape ``0`` at site ``0``, and the end that *was* declared is reported
    whichever of the two it is.
    """
    cxn = (
        CXN.replace(f"<xdr:cNvCxnSpPr>{BOTH_ENDS}</xdr:cNvCxnSpPr>", "", 1)
        if endpoints is None
        else CXN.replace(BOTH_ENDS, endpoints, 1)
    )

    assert inventory(xlsx(wsDr(two_cell(cxn))))[0][1] == {
        "shapeId": "4",
        "shapeName": "Straight Arrow Connector 4",
        "altTitle": None,
        "altDescription": None,
        **NO_ENDS,
        **declared,
    }


FOREIGN = ' xmlns:f="urn:not-spreadsheetdrawing"'
TRANSPARENT = {
    # Written in a vocabulary that merely shares the local name.
    "foreign": SP.replace("<xdr:sp>", f"<f:sp{FOREIGN}>", 1).replace(
        "</xdr:sp>", "</f:sp>", 1
    ),
    # Stripped of its prefix, so nothing qualifies it at all.
    "unqualified": SP.replace("<xdr:sp>", "<sp>", 1).replace("</xdr:sp>", "</sp>", 1),
    # Correctly qualified, but hanging under the anchor's client data rather
    # than beside it, so it is not a direct object of the anchor.
    "nested": f"<xdr:clientData>{SP}</xdr:clientData>",
}


@pytest.mark.parametrize("variant", TRANSPARENT.values(), ids=list(TRANSPARENT))
def test_only_a_qualified_direct_object_of_an_anchor_is_inventoried(
    variant: str,
) -> None:
    """An object is read by its name *and* its place, not by either alone.

    Each variant keeps every descendant exactly as the valid shape wrote it, so
    only the element's name or its position differs. None of the three is the
    object the spreadsheet-drawing grammar declares, so the anchor holds nothing
    this reader reports -- rather than reporting the lookalike as a shape.
    """
    assert inventory(xlsx(wsDr(two_cell(variant)))) == []


# A fill, a blip and an endpoint are each declared once or not at all, and each
# reference resolves or does not. Every case below is written in the valid
# fixture's place, so only the one thing named differs.
REFUSED: dict[str, tuple[str, str, str]] = {
    "two-claims": (
        SP.replace(
            "<xdr:cNvSpPr/>", '<xdr:cNvPr id="9" name="Other 9"/><xdr:cNvSpPr/>', 1
        ),
        DRAWING_RELS,
        "declares its claims twice",
    ),
    "two-fills": (
        PIC.replace(
            "<xdr:spPr/>",
            '<xdr:blipFill><a:blip r:embed="rIdI"/></xdr:blipFill><xdr:spPr/>',
            1,
        ),
        DRAWING_RELS,
        "more than one image reference",
    ),
    "two-blips": (
        PIC.replace('<a:blip r:embed="rIdI"/>', '<a:blip r:embed="rIdI"/>' * 2, 1),
        DRAWING_RELS,
        "more than one image reference",
    ),
    "two-embed-namespaces": (
        PIC.replace(
            '<a:blip r:embed="rIdI"/>',
            f'<a:blip xmlns:s="{STRICT_URIS[RELNS]}" r:embed="rIdI" s:embed="rIdI"/>',
            1,
        ),
        DRAWING_RELS,
        "more than one image relationship",
    ),
    "embed-names-nothing": (
        PIC.replace('r:embed="rIdI"', 'r:embed="rIdMissing"', 1),
        DRAWING_RELS,
        "missing image relationship",
    ),
    "embed-names-another-type": (
        PIC,
        DRAWING_RELS.replace(
            f'Id="rIdI" Target="../media/image1.png" Type="{RELNS}/image"',
            f'Id="rIdI" Target="../media/image1.png" Type="{RELNS}/hyperlink"',
        ),
        "image relationship of another type",
    ),
    "embed-names-an-external-target": (
        PIC.replace('r:embed="rIdI"', 'r:embed="rIdL"', 1),
        LINK_RELS,
        "embedded image relationship is external",
    ),
    "embed-names-a-part-that-is-not-an-image": (
        PIC.replace('r:embed="rIdI"', 'r:embed="rIdC"', 1),
        DRAWING_RELS,
        "image relationship of another type",
    ),
    "two-connector-properties": (
        CXN.replace("</xdr:nvCxnSpPr>", "<xdr:cNvCxnSpPr/></xdr:nvCxnSpPr>", 1),
        DRAWING_RELS,
        "declares its connector properties twice",
    ),
    "two-start-endpoints": (
        CXN.replace('<a:stCxn id="3" idx="2"/>', '<a:stCxn id="3" idx="2"/>' * 2, 1),
        DRAWING_RELS,
        "declares one of its endpoints twice",
    ),
    "endpoint-without-an-index": (
        CXN.replace('<a:stCxn id="3" idx="2"/>', '<a:stCxn id="3"/>', 1),
        DRAWING_RELS,
        "endpoint is malformed",
    ),
    "endpoint-with-an-unreadable-id": (
        CXN.replace('<a:stCxn id="3" idx="2"/>', '<a:stCxn id="three" idx="2"/>', 1),
        DRAWING_RELS,
        "endpoint is malformed",
    ),
}


@pytest.mark.parametrize(
    ("body", "rels", "diagnostic"), REFUSED.values(), ids=list(REFUSED)
)
def test_an_ambiguous_or_unresolvable_object_is_refused(
    body: str, rels: str, diagnostic: str
) -> None:
    """What an object declares is read or refused, never guessed at."""
    with pytest.raises(OOXMLInvalid, match=diagnostic):
        inventory(xlsx(wsDr(two_cell(body)), rels=rels))


@pytest.mark.parametrize(
    ("count", "diagnostic"),
    [(4096, ""), (4097, "too many objects")],
    ids=["at-the-bound", "over-the-bound"],
)
def test_a_drawing_is_held_to_a_bounded_number_of_objects(
    count: int, diagnostic: str
) -> None:
    """One anchor may carry as many objects as a drawing may carry anchors.

    The grammar gives an anchor one object, so a drawing naming more objects
    than it may name anchors is already past the shape the grammar allows and
    is refused there rather than after the work.
    """
    drawing = wsDr(two_cell(SP * count))

    if not diagnostic:
        assert len(inventory(xlsx(drawing))) == count
        return
    with pytest.raises(OOXMLInvalid, match=diagnostic):
        inventory(xlsx(drawing))


def test_a_sheet_that_declares_no_drawing_inventories_nothing() -> None:
    """No drawing, no objects -- and no part reached by looking like one."""
    content = archive(
        {
            "[Content_Types].xml": "<Types/>",
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
            SHEET_PART: f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>',
        }
    )

    assert inventory(content) == []
