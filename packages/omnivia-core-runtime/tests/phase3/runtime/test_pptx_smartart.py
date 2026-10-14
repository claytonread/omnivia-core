"""PPTX SmartArt: frame admission, stored point text and connection inventory.

Only the diagram data reference a slide's own graphic frame carries is followed,
and only what that data model part already stored is read -- each point's own
``dgm:t``, run by run, and the identity each connection encodes. The layout,
quick-style, colour and drawing parts a ``dgm:relIds`` names beside the data are
never opened, so no label, hierarchy or reading order is inferred from how the
diagram would be drawn.

Point text rides under the ``body`` role, each run at its own point, paragraph
and run; a connection is an inert ``object_inventory`` record. ``semantic_role``
is closed twice over -- ``SEGMENT_ROLES`` in ``storage/document_intake.py`` and
a ``CHECK (semantic_role IN (...))`` on both segment tables in the
already-applied ``0069_document_intake.sql`` -- so the structural detail rides
in the locator's ``diagramRole`` instead, as ``chartRole`` already does.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The two DrawingML diagram namespaces. A graphic frame's ``uri`` names one of
# them to say its graphic is SmartArt, and the data model part is rooted in the
# same one.
TRANSITIONAL_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
STRICT_NS = "http://purl.oclc.org/ooxml/drawingml/diagram"
# The DrawingML main namespace ``a:graphic`` and ``a:graphicData`` are qualified
# in, and the paragraphs and runs inside a point's text body with them.
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
# The two relationships namespaces every ``r:dm`` in the package is written in.
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
# The two relationship-type URIs OOXML defines for a diagram data part.
DIAGRAM_TYPE = REL_NS + "/diagramData"
STRICT_DIAGRAM_TYPE = STRICT_REL_NS + "/diagramData"
DIAGRAM_CT = "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml"
DIAGRAM_PART = "ppt/diagrams/data1.xml"
DIAGRAM_RELATION = (
    f'<Relationship Id="rId2" Target="../diagrams/data1.xml" Type="{DIAGRAM_TYPE}"/>'
)


def runs(*texts: str) -> str:
    """``a:r`` runs in the order they are written."""
    return "".join(f"<a:r><a:t>{text}</a:t></a:r>" for text in texts)


def text(*paragraphs: str) -> str:
    """One ``dgm:t``: a point's own text body, paragraph by paragraph."""
    return "<dgm:t>" + "".join(f"<a:p>{body}</a:p>" for body in paragraphs) + "</dgm:t>"


def pt(
    model_id: str | None = "1",
    *,
    kind: str | None = None,
    body: str = "",
    connection: str | None = None,
) -> str:
    """One ``dgm:pt``; a None attribute is left out of the markup entirely."""
    attributes = "" if model_id is None else f' modelId="{model_id}"'
    if kind is not None:
        attributes += f' type="{kind}"'
    if connection is not None:
        attributes += f' cxnId="{connection}"'
    return f"<dgm:pt{attributes}>{body}</dgm:pt>"


def cxn(
    model_id: str | None = "3",
    *,
    kind: str | None = None,
    source: str | None = "1",
    destination: str | None = "2",
    source_order: str | None = "0",
    destination_order: str | None = "0",
    parent: str | None = None,
    sibling: str | None = None,
) -> str:
    """One ``dgm:cxn``; a None attribute is left out of the markup entirely."""
    attributes = "".join(
        f' {name}="{value}"'
        for name, value in (
            ("modelId", model_id),
            ("type", kind),
            ("srcId", source),
            ("destId", destination),
            ("srcOrd", source_order),
            ("destOrd", destination_order),
            ("parTransId", parent),
            ("sibTransId", sibling),
        )
        if value is not None
    )
    return f"<dgm:cxn{attributes}/>"


def lists(points: str | None, connections: str | None = None) -> str:
    """A data model's point list and, when there is one, its connection list."""
    body = "" if points is None else f"<dgm:ptLst>{points}</dgm:ptLst>"
    if connections is not None:
        body += f"<dgm:cxnLst>{connections}</dgm:cxnLst>"
    return body


# Two points carrying one run of text each, and the one connection between
# them. Nothing in the model says which point is above the other: that is the
# layout's business, and the layout is never opened.
POINTS = pt("1", body=text(runs("First"))) + pt("2", body=text(runs("Second")))
DEFAULT = lists(POINTS, cxn())


def model(
    body: str = DEFAULT,
    *,
    namespace: str = TRANSITIONAL_NS,
    root: str = "dataModel",
) -> str:
    """One diagram data part holding the given lists."""
    return (
        f'<dgm:{root} xmlns:dgm="{namespace}" xmlns:a="{DRAWING_NS}">'
        f"{body}</dgm:{root}>"
    )


def relids(
    *,
    dm: str | None = "rId2",
    namespace: str = TRANSITIONAL_NS,
    relationships: str = REL_NS,
    extra: str = "",
) -> str:
    """One ``dgm:relIds``, always naming the layout, style and colour parts too.

    Those three are the ids nothing here opens: the data reference is the only
    one read off the element.
    """
    named = "" if dm is None else f' r:dm="{dm}"'
    return (
        f'<dgm:relIds xmlns:dgm="{namespace}" xmlns:r="{relationships}"'
        f'{named} r:lo="rId3" r:qs="rId4" r:cs="rId5"{extra}/>'
    )


def graphic(
    body: str,
    *,
    uri: str | None = TRANSITIONAL_NS,
    namespace: str = DRAWING_NS,
) -> str:
    """The ``a:graphic``/``a:graphicData`` route a frame declares a graphic on."""
    attribute = "" if uri is None else f' uri="{uri}"'
    return (
        f'<a:graphic xmlns:a="{namespace}">'
        f"<a:graphicData{attribute}>{body}</a:graphicData></a:graphic>"
    )


def pptx(
    *,
    models: dict[str, str] | None = None,
    slide_rels: str | None = DIAGRAM_RELATION,
    reference: str | None = None,
    grouped: bool = False,
    hidden: bool = False,
    overrides: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a one-slide presentation whose graphic frame refers to SmartArt.

    ``models`` maps part name to the data model each holds; a part left out of
    it is simply not in the package, which is how a phantom declared-only part
    is built. ``overrides`` defaults to declaring exactly the parts that are
    there.
    """
    parts = {DIAGRAM_PART: model()} if models is None else dict(models)
    if overrides is None:
        overrides = "".join(
            f'<Override PartName="/{name}" ContentType="{DIAGRAM_CT}"/>'
            for name in parts
        )
    frame = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3"/></p:nvGraphicFramePr>'
        f"{graphic(relids()) if reference is None else reference}</p:graphicFrame>"
    )
    if grouped:
        frame = (
            '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9"/></p:nvGrpSpPr><p:grpSpPr/>'
            f"{frame}</p:grpSp>"
        )
    parts["[Content_Types].xml"] = f"<Types>{overrides}</Types>"
    shown = ' show="0"' if hidden else ""
    parts["ppt/presentation.xml"] = (
        '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
        f'<p:sldId id="256" r:id="rId1"{shown}/></p:sldIdLst></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/slides/slide1.xml"] = (
        '<p:sld xmlns:p="urn:p"><p:cSld><p:spTree>'
        f"{frame}</p:spTree></p:cSld></p:sld>"
    )
    if slide_rels is not None:
        parts["ppt/slides/_rels/slide1.xml.rels"] = (
            f"<Relationships>{slide_rels}</Relationships>"
        )
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def diagram(content: bytes) -> list[ExtractedSegment]:
    """Every segment the SmartArt route produced, in the order it emitted them."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if "diagramRole" in item.source_locator
    ]


def shape(content: bytes) -> list[tuple[str, str, str | None]]:
    """The (role, model id, text) of each record, in source order."""
    return [
        (
            str(item.source_locator["diagramRole"]),
            str(item.source_locator["modelId"]),
            item.text,
        )
        for item in diagram(content)
    ]


# The provenance the default fixture's first point-text record carries. Every
# other record differs from it only in where inside the diagram it sits.
EXPECTED = {
    "captureId": CAPTURE,
    "part": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
    "shapeId": "3",
    "shapeKind": "graphicFrame",
    "groupPath": [],
    "zOrder": 0,
    "zPath": [0],
    "readingOrder": 0,
    "readingPath": [0],
    "geometry": None,
    "placeholderType": None,
    "placeholderIndex": None,
    "diagramPart": DIAGRAM_PART,
    "relationshipId": "rId2",
    "diagramRole": "point_text",
    "modelId": "1",
    "pointType": "node",
    "pointIndex": 0,
    "connectionModelId": None,
    "paragraph": 0,
    "run": 0,
}
# The connection record's locator holds only its identity: a connection sits at
# no paragraph or run, so none of those keys is on it.
CONNECTED = {
    key: value
    for key, value in EXPECTED.items()
    if key
    not in {"diagramRole", "modelId", "pointType", "pointIndex", "connectionModelId",
            "paragraph", "run"}
} | {"diagramRole": "connection", "modelId": "3", "connectionIndex": 0}


def test_a_qualified_frame_reports_every_point_text_in_source_order() -> None:
    """Each point's stored text, at the point, paragraph and run holding it."""
    content = pptx()

    assert shape(content) == [
        ("point_text", "1", "First"),
        ("point_text", "2", "Second"),
        ("connection", "3", None),
    ]
    first, second, _link = diagram(content)
    assert (first.semantic_role, first.visibility) == ("body", "visible")
    assert dict(first.source_locator) == EXPECTED
    assert dict(second.source_locator) == EXPECTED | {
        "modelId": "2",
        "pointIndex": 1,
    }


def test_a_connection_is_the_identity_it_encodes_and_no_hierarchy() -> None:
    """Source, destination, orders and resolution; nothing walked or numbered."""
    content = pptx()

    (_first, _second, link) = diagram(content)
    assert (link.semantic_role, link.extraction_method) == (
        "object_inventory",
        "inventory",
    )
    assert dict(link.source_locator) == CONNECTED
    assert link.structured_value == {
        "kind": "diagramConnection",
        "modelId": "3",
        "connectionIndex": 0,
        "connectionType": "parOf",
        "sourceModelId": "1",
        "destinationModelId": "2",
        "sourceOrder": "0",
        "destinationOrder": "0",
        "parentTransitionModelId": None,
        "siblingTransitionModelId": None,
        "sourceResolved": True,
        "destinationResolved": True,
    }
    # Nothing derived rides beside it: no parent, child, depth or ordinal.
    assert link.text is None


def test_a_connection_citing_a_point_nothing_defines_stays_unresolved() -> None:
    """The claim is kept as the file made it; no identity is invented for it."""
    content = pptx(
        models={DIAGRAM_PART: model(lists(POINTS, cxn(destination="9")))}
    )

    (_first, _second, link) = diagram(content)
    assert link.structured_value is not None
    assert (
        link.structured_value["destinationModelId"],
        link.structured_value["sourceResolved"],
        link.structured_value["destinationResolved"],
    ) == ("9", True, False)


def test_a_transition_point_and_its_citations_are_recorded_not_followed() -> None:
    """``pt/@cxnId`` and the transition ids on a connection ride as written."""
    content = pptx(
        models={
            DIAGRAM_PART: model(
                lists(
                    pt("1", kind="sibTrans", body=text(runs("Between")), connection="3"),
                    cxn(kind="presOf", parent="7", sibling="8", destination="1"),
                )
            )
        }
    )

    (point, link) = diagram(content)
    assert (
        point.source_locator["pointType"],
        point.source_locator["connectionModelId"],
    ) == ("sibTrans", "3")
    assert link.structured_value is not None
    assert (
        link.structured_value["connectionType"],
        link.structured_value["parentTransitionModelId"],
        link.structured_value["siblingTransitionModelId"],
    ) == ("presOf", "7", "8")


def test_a_layout_point_is_reported_as_the_kind_it_says_it_is() -> None:
    """The kind rides beside the text; it does not decide whether text is read."""
    content = pptx(
        models={
            DIAGRAM_PART: model(lists(pt("1", kind="pres", body=text(runs("Shown")))))
        }
    )

    assert shape(content) == [("point_text", "1", "Shown")]
    ((item,)) = diagram(content)
    assert item.source_locator["pointType"] == "pres"


def test_paragraphs_and_runs_are_numbered_inside_the_point_that_holds_them() -> None:
    """Indexes restart per point, because they are local to its own body."""
    content = pptx(
        models={
            DIAGRAM_PART: model(
                lists(
                    pt("1", body=text(runs("One", "", "Two"), runs("Three")))
                    + pt("2", body=text(runs("Four"))),
                    None,
                )
            )
        }
    )

    assert [
        (
            item.source_locator["modelId"],
            item.source_locator["paragraph"],
            item.source_locator["run"],
            item.text,
        )
        for item in diagram(content)
    ] == [
        # The empty run at index 1 keeps its position and reports nothing.
        ("1", 0, 0, "One"),
        ("1", 0, 2, "Two"),
        ("1", 1, 0, "Three"),
        ("2", 0, 0, "Four"),
    ]


def test_a_point_with_no_text_body_reports_nothing_and_is_not_refused() -> None:
    """A point that stored no text is a point; it simply has nothing to report."""
    content = pptx(
        models={
            DIAGRAM_PART: model(
                lists(pt("1", kind="doc") + pt("2", body=text(runs("Only"))), None)
            )
        }
    )

    assert shape(content) == [("point_text", "2", "Only")]


def test_the_strict_namespaces_reach_the_same_diagram() -> None:
    """Strict and Transitional each name the one type, and the one vocabulary."""
    strict = pptx(
        models={DIAGRAM_PART: model(namespace=STRICT_NS)},
        slide_rels=DIAGRAM_RELATION.replace(DIAGRAM_TYPE, STRICT_DIAGRAM_TYPE),
        reference=graphic(
            relids(namespace=STRICT_NS, relationships=STRICT_REL_NS),
            uri=STRICT_NS,
            namespace=STRICT_DRAWING_NS,
        ),
    )

    assert shape(strict) == shape(pptx())
    assert dict(diagram(strict)[0].source_locator) == EXPECTED


def test_a_diagram_inside_a_group_keeps_the_whole_path_to_its_frame() -> None:
    content = pptx(grouped=True)

    locator = diagram(content)[0].source_locator
    assert (locator["groupPath"], locator["zPath"], locator["shapeId"]) == (
        ["9"],
        [0, 0],
        "3",
    )


def test_a_hidden_slide_marks_every_diagram_segment_hidden() -> None:
    """Point text and connection records alike carry the slide's own visibility."""
    assert [item.visibility for item in diagram(pptx(hidden=True))] == ["hidden"] * 3
    assert [item.visibility for item in diagram(pptx())] == ["visible"] * 3
    assert diagram(pptx(hidden=True))[0].source_locator["slideHidden"] is True


def test_an_unrelated_foreign_or_nested_graphic_route_produces_no_smartart() -> None:
    """Only qualified graphic data on the frame's own route declares SmartArt."""
    nested = graphic(graphic(relids()), uri=None)
    for reference in (
        # Graphic data that names no vocabulary, and one naming another.
        graphic(relids(), uri=None),
        graphic(relids(), uri="http://schemas.openxmlformats.org/presentationml/2006/ole"),
        # The defined URI with something appended, and with something prepended.
        graphic(relids(), uri=TRANSITIONAL_NS + "Extra"),
        graphic(relids(), uri="https://proxy.example.invalid/" + TRANSITIONAL_NS),
        # Graphic and graphic data written in a vocabulary that is not DrawingML.
        graphic(relids(), namespace="urn:a"),
        # A qualified lookalike nested below the route rather than on it.
        nested,
        (
            f"<mc:AlternateContent xmlns:mc='urn:mc'><mc:Fallback>{graphic(relids())}"
            "</mc:Fallback></mc:AlternateContent>"
        ),
    ):
        assert diagram(pptx(reference=reference)) == []

    # The route is the frame's own child, so a graphic one level deeper than the
    # shape tree expects declares nothing either.
    assert diagram(pptx(reference=f"<p:ext xmlns:p='urn:p'>{graphic(relids())}</p:ext>")) == []


def test_a_diagram_part_nothing_relates_to_is_never_read() -> None:
    """No diagram path is derived from the slide's name, so an orphan is inert."""
    content = pptx(
        reference='<a:pic xmlns:a="urn:a"/>',
        models={DIAGRAM_PART: model(), "ppt/diagrams/data9.xml": model()},
    )

    assert diagram(content) == []


def test_a_missing_or_unqualified_data_reference_is_refused() -> None:
    """SmartArt is advertised, so the reference has to be there and qualified."""
    for body, reason in (
        # Advertised and then naming nothing at all.
        ("", "does not name exactly one diagram reference"),
        # Two references leave which diagram the frame shows ambiguous.
        (relids() * 2, "does not name exactly one diagram reference"),
        # A ``relIds`` written in another vocabulary is not this one's reference.
        (relids(namespace=STRICT_NS), "does not name exactly one diagram reference"),
        (relids(namespace="urn:dgm"), "does not name exactly one diagram reference"),
        # An id in no namespace, or in another one, names no relationship.
        (relids(dm=None), "SmartArt reference names no relationship"),
        (relids(dm=None, extra=' dm="rId2"'), "SmartArt reference names no relationship"),
        (relids(relationships="urn:r"), "SmartArt reference names no relationship"),
    ):
        with pytest.raises(OOXMLInvalid, match=reason):
            extract_pptx(pptx(reference=graphic(body)), capture_id=CAPTURE)

    # One ``dm`` in each relationships namespace is two ids, not a repeat.
    doubled = relids(extra=f' xmlns:r2="{STRICT_REL_NS}" r2:dm="rId2"')
    with pytest.raises(OOXMLInvalid, match="names more than one relationship"):
        extract_pptx(pptx(reference=graphic(doubled)), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="more than one SmartArt reference"):
        extract_pptx(
            pptx(
                reference='<a:graphic xmlns:a="' + DRAWING_NS + '">'
                f'<a:graphicData uri="{TRANSITIONAL_NS}">{relids()}</a:graphicData>'
                f'<a:graphicData uri="{TRANSITIONAL_NS}">{relids()}</a:graphicData>'
                "</a:graphic>"
            ),
            capture_id=CAPTURE,
        )


def test_a_frame_claiming_smartart_and_another_graphic_is_refused() -> None:
    table = (
        "<a:tbl><a:tblGrid><a:gridCol/></a:tblGrid>"
        "<a:tr><a:tc><a:txBody><a:p/></a:txBody></a:tc></a:tr></a:tbl>"
    )
    chart = '<c:chart xmlns:c="urn:c" xmlns:r="urn:r" r:id="rId2"/>'

    for other in (table, chart):
        with pytest.raises(OOXMLInvalid, match="more than one graphic"):
            extract_pptx(pptx(reference=graphic(relids() + other)), capture_id=CAPTURE)


def test_a_relationship_of_another_type_is_refused() -> None:
    """The frame names this id as its data model, so a lookalike cannot stand in.

    Every target below is the real, qualified data part; only the relationship
    type fails to qualify.
    """
    for kind in (
        # Same tail, different host.
        "http://diagrams.example.invalid/officeDocument/2006/relationships/diagramData",
        # The defined URI with something appended, and with something prepended.
        DIAGRAM_TYPE + "Extra",
        "https://proxy.example.invalid/" + DIAGRAM_TYPE,
        # The path is case-sensitive, so a folded path names another type.
        DIAGRAM_TYPE.replace("diagramData", "diagramdata"),
        # The layout, quick-style and colour types name parts nothing here opens.
        REL_NS + "/diagramLayout",
        REL_NS + "/diagramQuickStyle",
        REL_NS + "/diagramColors",
        REL_NS + "/diagramDrawing",
    ):
        with pytest.raises(OOXMLInvalid, match="relationship of another type"):
            extract_pptx(
                pptx(slide_rels=DIAGRAM_RELATION.replace(DIAGRAM_TYPE, kind)),
                capture_id=CAPTURE,
            )

    # Scheme and host are case-insensitive, so folding them still qualifies.
    shouted = "HTTP://SCHEMAS.OPENXMLFORMATS.ORG" + DIAGRAM_TYPE.partition(".org")[2]
    assert shape(
        pptx(slide_rels=DIAGRAM_RELATION.replace(DIAGRAM_TYPE, shouted))
    ) == shape(pptx())


def test_a_missing_or_external_relationship_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        extract_pptx(pptx(slide_rels=None), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        extract_pptx(
            pptx(slide_rels=DIAGRAM_RELATION.replace('Id="rId2"', 'Id="rId7"')),
            capture_id=CAPTURE,
        )

    external = (
        f'<Relationship Id="rId2" Type="{DIAGRAM_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/data1.xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="diagram data relationship is external"):
        extract_pptx(pptx(slide_rels=external), capture_id=CAPTURE)


def test_an_undeclared_wrongly_declared_or_unsafe_target_is_refused() -> None:
    """The target has to be declared a data part, not merely be named one."""
    layout = (
        f'<Override PartName="/{DIAGRAM_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.drawingml.diagramLayout+xml"/>'
    )
    for overrides in ("", layout):
        with pytest.raises(
            OOXMLInvalid, match="does not name a diagram data part"
        ):
            extract_pptx(pptx(overrides=overrides), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(
            pptx(
                models={},
                overrides=f'<Override PartName="/{DIAGRAM_PART}"'
                f' ContentType="{DIAGRAM_CT}"/>',
            ),
            capture_id=CAPTURE,
        )

    traversal = DIAGRAM_RELATION.replace("../diagrams/data1.xml", "../../../etc/passwd")
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(slide_rels=traversal), capture_id=CAPTURE)


def test_a_data_part_in_another_namespace_or_root_is_refused() -> None:
    """The declared type says what the part is; the root says so itself."""
    with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(namespace="urn:dgm")}), capture_id=CAPTURE
        )

    # Rooted in the right vocabulary at the wrong element, and at the layout
    # part's own root, which is a different part entirely.
    for root in ("ptLst", "layoutDef", "relIds"):
        with pytest.raises(OOXMLInvalid, match="not rooted at its own data model"):
            extract_pptx(
                pptx(models={DIAGRAM_PART: model(root=root)}), capture_id=CAPTURE
            )

    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: f'<dgm:dataModel xmlns:dgm="{TRANSITIONAL_NS}">'}),
            capture_id=CAPTURE,
        )


def test_a_missing_doubled_or_polluted_list_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="declares no point list"):
        extract_pptx(pptx(models={DIAGRAM_PART: model("")}), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="declares no point list"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(lists(None, cxn()))}), capture_id=CAPTURE
        )

    for body in (lists(POINTS) * 2, lists(POINTS, cxn()) + lists(None, cxn("4"))):
        with pytest.raises(OOXMLInvalid, match="more than one of a list"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)

    # A point list holds points, and a connection list holds connections: an
    # element of some other kind is not read for whatever text is in it.
    with pytest.raises(OOXMLInvalid, match="point list holds an unsupported element"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(lists(POINTS + cxn()))}),
            capture_id=CAPTURE,
        )
    with pytest.raises(
        OOXMLInvalid, match="connection list holds an unsupported element"
    ):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(lists(POINTS, pt("4")))}),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="more than one text body"):
        extract_pptx(
            pptx(
                models={
                    DIAGRAM_PART: model(
                        lists(pt("1", body=text(runs("One")) + text(runs("Two"))), None)
                    )
                }
            ),
            capture_id=CAPTURE,
        )


def test_a_repeated_model_id_inside_one_list_is_refused() -> None:
    """Two points, or two connections, under one id leave every citation of it
    ambiguous."""
    with pytest.raises(OOXMLInvalid, match="model id is repeated"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(lists(pt("1") + pt("1"), None))}),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="model id is repeated"):
        extract_pptx(
            pptx(models={DIAGRAM_PART: model(lists(POINTS, cxn() + cxn("3")))}),
            capture_id=CAPTURE,
        )

    # A GUID's hex digits vary in case, so a refolded one is the same id.
    guid = "{0DA1B2C3-4455-6677-8899-AABBCCDDEEFF}"
    with pytest.raises(OOXMLInvalid, match="model id is repeated"):
        extract_pptx(
            pptx(
                models={
                    DIAGRAM_PART: model(lists(pt(guid) + pt(guid.casefold()), None))
                }
            ),
            capture_id=CAPTURE,
        )

    # Points and connections are separate collections, so one id in each is two
    # records rather than a repeat.
    shared = pptx(
        models={DIAGRAM_PART: model(lists(pt("1", body=text(runs("Both"))), cxn("1")))}
    )
    assert shape(shared) == [("point_text", "1", "Both"), ("connection", "1", None)]


def test_a_malformed_or_missing_model_id_is_refused() -> None:
    """``ST_ModelId`` is a GUID or an unsigned int, and it is read verbatim."""
    for body in (
        lists(pt(None), None),
        lists(POINTS, cxn(model_id=None)),
        lists(POINTS, cxn(source=None)),
        lists(POINTS, cxn(destination=None)),
    ):
        with pytest.raises(OOXMLInvalid, match="model id is missing"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)

    for body in (
        lists(pt("not-an-id"), None),
        lists(pt("-1"), None),
        lists(pt("4294967296"), None),
        lists(pt("1.0"), None),
        lists(pt(" 1"), None),
        # A GUID missing its braces is not the ``ST_Guid`` lexical form.
        lists(pt("0DA1B2C3-4455-6677-8899-AABBCCDDEEFF"), None),
        lists(POINTS, cxn(connection := "x")),
        lists(POINTS, cxn(source="x")),
        lists(POINTS, cxn(parent="x")),
        lists(POINTS, cxn(sibling="x")),
        lists(pt("1", connection="x"), None),
    ):
        with pytest.raises(OOXMLInvalid, match="model id is malformed"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)
    assert connection == "x"

    # A braced GUID and an unsigned int are both read, exactly as written.
    guid = "{0dA1b2C3-4455-6677-8899-aabbCCddEEff}"
    content = pptx(
        models={DIAGRAM_PART: model(lists(pt(guid, body=text(runs("Kept"))), None))}
    )
    assert shape(content) == [("point_text", guid, "Kept")]


def test_an_undefined_point_or_connection_kind_is_refused() -> None:
    """``ST_PtType`` and ``ST_CxnType`` are closed, so an unknown kind is refused."""
    for body in (
        lists(pt("1", kind="widget"), None),
        lists(pt("1", kind=""), None),
        lists(pt("1", kind="Node"), None),
    ):
        with pytest.raises(OOXMLInvalid, match="point type is not a defined kind"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)

    for body in (
        lists(POINTS, cxn(kind="widget")),
        lists(POINTS, cxn(kind="ParOf")),
    ):
        with pytest.raises(OOXMLInvalid, match="connection type is not a defined kind"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)

    # Every kind the schema does define is read, under its own name.
    for kind in ("node", "parTrans", "sibTrans", "pres", "doc", "asst"):
        content = pptx(
            models={
                DIAGRAM_PART: model(lists(pt("1", kind=kind, body=text(runs("K"))), None))
            }
        )
        assert diagram(content)[0].source_locator["pointType"] == kind

    for kind in ("parOf", "presOf", "presParOf", "unknownRelationship"):
        content = pptx(models={DIAGRAM_PART: model(lists(POINTS, cxn(kind=kind)))})
        value = diagram(content)[-1].structured_value
        assert value is not None and value["connectionType"] == kind


def test_a_malformed_or_missing_connection_order_is_refused() -> None:
    """An order is the position the connection encodes; it is never renumbered."""
    for body in (
        lists(POINTS, cxn(source_order=None)),
        lists(POINTS, cxn(destination_order=None)),
        lists(POINTS, cxn(source_order="x")),
        lists(POINTS, cxn(source_order="-1")),
        lists(POINTS, cxn(destination_order="4294967296")),
        lists(POINTS, cxn(destination_order="1.0")),
    ):
        with pytest.raises(OOXMLInvalid, match="connection order is malformed"):
            extract_pptx(pptx(models={DIAGRAM_PART: model(body)}), capture_id=CAPTURE)

    kept = pptx(
        models={
            DIAGRAM_PART: model(lists(POINTS, cxn(source_order="7", destination_order="3")))
        }
    )
    value = diagram(kept)[-1].structured_value
    assert value is not None
    assert (value["sourceOrder"], value["destinationOrder"]) == ("7", "3")


def test_markup_inside_point_text_stays_inert_text() -> None:
    payload = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; 2"
    content = pptx(
        models={DIAGRAM_PART: model(lists(pt("1", body=text(runs(payload))), None))}
    )

    assert [item.text for item in diagram(content)] == [
        "<script>alert(1)</script> & 2"
    ]


def test_the_layout_style_colour_and_drawing_parts_are_never_opened() -> None:
    """Only the data reference is followed, so the rest of the set is inert."""
    others = "".join(
        f'<Relationship Id="{rid}" Target="../diagrams/{name}.xml"'
        f' Type="{REL_NS}/{kind}"/>'
        for rid, name, kind in (
            ("rId3", "layout1", "diagramLayout"),
            ("rId4", "quickStyle1", "diagramQuickStyle"),
            ("rId5", "colors1", "diagramColors"),
            ("rId6", "drawing1", "diagramDrawing"),
        )
    )
    members = {
        f"ppt/diagrams/{name}.xml": "not a data model anything here reads"
        for name in ("layout1", "quickStyle1", "colors1", "drawing1")
    }

    assert shape(
        pptx(slide_rels=DIAGRAM_RELATION + others, members=members)
    ) == shape(pptx())


def test_diagram_counts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each bound is counted across the one data part it belongs to."""
    many_points = pptx(
        models={DIAGRAM_PART: model(lists("".join(pt(str(n)) for n in range(4)), None))}
    )
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_POINTS", 3)
    with pytest.raises(OOXMLInvalid, match="too many points"):
        extract_pptx(many_points, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_POINTS", 10_000)

    many_links = pptx(
        models={
            DIAGRAM_PART: model(
                lists(POINTS, "".join(cxn(str(n)) for n in range(10, 14)))
            )
        }
    )
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_CONNECTIONS", 3)
    with pytest.raises(OOXMLInvalid, match="too many connections"):
        extract_pptx(many_links, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_CONNECTIONS", 20_000)

    # Paragraphs and runs are counted across the part, not inside one point, so
    # two points of two paragraphs each cross a bound of three.
    wordy = pptx(
        models={
            DIAGRAM_PART: model(
                lists(
                    pt("1", body=text(runs("a", "b"), runs("c", "d")))
                    + pt("2", body=text(runs("e", "f"), runs("g", "h"))),
                    None,
                )
            )
        }
    )
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_PARAGRAPHS", 3)
    with pytest.raises(OOXMLInvalid, match="too many paragraphs"):
        extract_pptx(wordy, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_PARAGRAPHS", 20_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_RUNS", 7)
    with pytest.raises(OOXMLInvalid, match="too many runs"):
        extract_pptx(wordy, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_RUNS", 100_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_CHARS", 7)
    with pytest.raises(OOXMLInvalid, match="text exceeds the accepted bound"):
        extract_pptx(wordy, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_DIAGRAM_CHARS", 100_000)

    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 2)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(pptx(), capture_id=CAPTURE)
