"""PPTX placeholder applicability: the text a slide's empty slots inherit.

A slide placeholder is a slot, not text. What the slide shows in it is what the
slide itself saved there -- and when it saved nothing, what the applicable
placeholder on its layout saved, or failing that the one on that layout's
master. Which placeholder is applicable is OOXML's own link: ``idx`` *is* the
slot, so a stated index identifies the placeholder on its own and the type the
two ends give that slot need not agree; only at the default index, where every
placeholder written without an ``idx`` sits, is the type what tells one from
another. A bare ``p:ph`` is the body at the default index, which is what each
of the two attributes defaults to.

Resolution runs one hop at a time, exactly as the routing to the two parts
does: a slot no placeholder on the layout names inherits nothing, and is not
looked up on the master behind it either. Nothing non-matching is ever reported
as inherited text. Each record names both ends -- the shape that saved the text
and the slide shape applying it -- and is emitted in the order the slide's own
shape tree declared the slot, after both parts have reported their own
non-placeholder text.

The sibling suite ``test_pptx_inherited_text`` covers the other half of what a
slide shows from behind: the non-placeholder shapes a layout and master draw
themselves.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
MAIN_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_MAIN_NS = "http://purl.oclc.org/ooxml/presentationml/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
LAYOUT_TYPE = REL_NS + "/slideLayout"
STRICT_LAYOUT_TYPE = STRICT_REL_NS + "/slideLayout"
MASTER_TYPE = REL_NS + "/slideMaster"
STRICT_MASTER_TYPE = STRICT_REL_NS + "/slideMaster"
LAYOUT_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"
)
MASTER_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"
)
SLIDE_PART = "ppt/slides/slide1.xml"
LAYOUT_PART = "ppt/slideLayouts/slideLayout1.xml"
MASTER_PART = "ppt/slideMasters/slideMaster1.xml"
LAYOUT_RELATION = (
    f'<Relationship Id="rId2" Type="{LAYOUT_TYPE}"'
    ' Target="../slideLayouts/slideLayout1.xml"/>'
)
MASTER_RELATION = (
    f'<Relationship Id="rId3" Type="{MASTER_TYPE}"'
    ' Target="../slideMasters/slideMaster1.xml"/>'
)
GROUP_PROPERTIES = '<p:nvGrpSpPr><p:cNvPr id="1"/></p:nvGrpSpPr><p:grpSpPr/>'
HOLDERS = {
    "sp": "nvSpPr",
    "cxnSp": "nvCxnSpPr",
    "pic": "nvPicPr",
    "grpSp": "nvGrpSpPr",
    "graphicFrame": "nvGraphicFramePr",
}
# Where each part's own shape sits, in EMU. A slot is one place on the slide and
# another on each part behind it, so every record names two transforms.
SLIDE_AT = (1, 2, 3, 4)
SECOND_AT = (5, 6, 7, 8)
LAYOUT_AT = (10, 20, 30, 40)
MASTER_AT = (50, 60, 70, 80)


def runs(*texts: str) -> str:
    """``a:r`` runs in the order they are written."""
    return "".join(f"<a:r><a:t>{text}</a:t></a:r>" for text in texts)


def body(*paragraphs: str) -> str:
    """One ``p:txBody``: a shape's own text, paragraph by paragraph."""
    return "<p:txBody>" + "".join(f"<a:p>{p}</a:p>" for p in paragraphs) + "</p:txBody>"


def sp(
    shape_id: str,
    *,
    kind: str = "sp",
    text: str | None = None,
    text_body: str | None = None,
    at: tuple[int, int, int, int] | None = None,
    placeholder: str | None = None,
    children: str = "",
) -> str:
    """One shape; ``placeholder`` is the attribute text of a ``p:ph``.

    ``""`` therefore writes the bare reference a shape inheriting the body slot
    carries, and None writes no reference at all. ``text`` defaults to none, so
    a shape built here is an empty slot unless it is given some.
    """
    reference = "" if placeholder is None else f"<p:ph{placeholder}/>"
    holder = HOLDERS[kind]
    transform = (
        ""
        if at is None
        else f'<a:xfrm><a:off x="{at[0]}" y="{at[1]}"/>'
        f'<a:ext cx="{at[2]}" cy="{at[3]}"/></a:xfrm>'
    )
    if kind == "graphicFrame":
        properties = transform.replace("a:xfrm", "p:xfrm")
    else:
        name = "grpSpPr" if kind == "grpSp" else "spPr"
        properties = f"<p:{name}>{transform}</p:{name}>"
    if text_body is None:
        text_body = "" if text is None else body(runs(text))
    return (
        f'<p:{kind}><p:{holder}><p:cNvPr id="{shape_id}"/>'
        f"<p:nvPr>{reference}</p:nvPr></p:{holder}>"
        f"{properties}{text_body}{children}</p:{kind}>"
    )


def part(
    root: str,
    shapes: str,
    *,
    namespace: str = MAIN_NS,
    show_master: str | None = None,
) -> str:
    """One layout or master part holding the given shapes in its own tree."""
    flag = "" if show_master is None else f' showMasterSp="{show_master}"'
    return (
        f'<p:{root} xmlns:p="{namespace}" xmlns:a="{DRAWING_NS}"{flag}><p:cSld>'
        f"<p:spTree>{GROUP_PROPERTIES}{shapes}</p:spTree></p:cSld></p:{root}>"
    )


# The default pair behind every fixture: a layout whose title slot holds the
# prompt a layout writes, and a master whose title slot holds the one a master
# writes. Each part also carries a body slot the other fills in, so the two
# directions of the fallback can be told apart by the text alone.
LAYOUT_TITLE = sp("2", text="Layout title", at=LAYOUT_AT, placeholder=' type="title"')
LAYOUT_BODY = sp("3", at=SECOND_AT, placeholder=' type="body" idx="1"')
MASTER_TITLE = sp("4", at=MASTER_AT, placeholder=' type="title"')
MASTER_BODY = sp("5", text="Master body", at=MASTER_AT, placeholder=' type="body" idx="1"')
LAYOUT = part("sldLayout", LAYOUT_TITLE + LAYOUT_BODY)
MASTER = part("sldMaster", MASTER_TITLE + MASTER_BODY)


def pptx(
    *,
    shapes: str = "",
    layout: str | None = LAYOUT,
    master: str | None = MASTER,
    slide_rels: str | None = LAYOUT_RELATION,
    layout_rels: str | None = MASTER_RELATION,
    show_master: str | None = None,
    hidden: bool = False,
) -> bytes:
    """Build a presentation whose slide sits in front of a layout and a master."""
    parts: dict[str, str] = {}
    if layout is not None:
        parts[LAYOUT_PART] = layout
    if master is not None:
        parts[MASTER_PART] = master
    parts["[Content_Types].xml"] = "<Types>" + "".join(
        f'<Override PartName="/{name}" ContentType="{content_type}"/>'
        for name, content_type in ((LAYOUT_PART, LAYOUT_CT), (MASTER_PART, MASTER_CT))
        if name in parts
    ) + "</Types>"
    shown = ' show="0"' if hidden else ""
    parts["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{MAIN_NS}" xmlns:r="{REL_NS}"><p:sldIdLst>'
        f'<p:sldId id="256" r:id="rId1"{shown}/></p:sldIdLst></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
        "</Relationships>"
    )
    flag = "" if show_master is None else f' showMasterSp="{show_master}"'
    parts[SLIDE_PART] = (
        f'<p:sld xmlns:p="{MAIN_NS}" xmlns:a="{DRAWING_NS}"{flag}><p:cSld>'
        f"<p:spTree>{GROUP_PROPERTIES}{shapes}</p:spTree></p:cSld></p:sld>"
    )
    if slide_rels is not None:
        parts["ppt/slides/_rels/slide1.xml.rels"] = (
            f"<Relationships>{slide_rels}</Relationships>"
        )
    if layout_rels is not None:
        parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
            f"<Relationships>{layout_rels}</Relationships>"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def applied(content: bytes) -> list[ExtractedSegment]:
    """Every segment an applying slide placeholder produced, in emitted order."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if "slideShapeId" in item.source_locator
    ]


def shown(content: bytes) -> list[tuple[str, str, str, str | None]]:
    """The (part, source shape, applying slide shape, text) of each record."""
    return [
        (
            str(item.source_locator["inheritedFrom"]),
            str(item.source_locator["shapeId"]),
            str(item.source_locator["slideShapeId"]),
            item.text,
        )
        for item in applied(content)
    ]


def every(content: bytes) -> list[str | None]:
    """Every body segment's text, in the order extraction emitted it."""
    return [
        item.text
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ]


# The provenance a record sourced on the default fixture's layout carries: both
# ends of the slot, the part and relationship it was read through, and the index
# of the run inside the shape that saved it.
EXPECTED = {
    "captureId": CAPTURE,
    "part": LAYOUT_PART,
    "relationshipId": "rId2",
    "inheritedFrom": "layout",
    "slidePart": SLIDE_PART,
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
    "shapeId": "2",
    "shapeKind": "sp",
    "zOrder": 0,
    # The layout's body slot sits above its title slot, so the title shape is
    # written first and read second: its own part's order, not the slide's.
    "readingOrder": 1,
    "readingPath": [1],
    "geometry": {"x": 10, "y": 20, "cx": 30, "cy": 40},
    "placeholderType": "title",
    "placeholderIndex": None,
    "slideShapeId": "9",
    "slideShapeKind": "sp",
    "slideGroupPath": [],
    "slideZOrder": 0,
    "slideZPath": [0],
    "slideReadingOrder": 0,
    "slideReadingPath": [0],
    "slideGeometry": {"x": 1, "y": 2, "cx": 3, "cy": 4},
    "paragraph": 0,
    "run": 0,
}


def test_an_empty_title_and_body_each_show_the_text_saved_for_their_own_slot() -> None:
    """Two slots, resolved apart: the title on the layout, the body on the master."""
    content = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
        + sp("10", at=SECOND_AT, placeholder=' type="body" idx="1"')
    )

    assert shown(content) == [
        ("layout", "2", "9", "Layout title"),
        ("master", "5", "10", "Master body"),
    ]
    first, second = applied(content)
    assert (first.semantic_role, first.visibility) == ("body", "visible")
    assert (first.extraction_method, first.structured_value) == ("direct", None)
    assert dict(first.source_locator) == EXPECTED
    assert dict(second.source_locator) == EXPECTED | {
        "part": MASTER_PART,
        "relationshipId": "rId3",
        "inheritedFrom": "master",
        "shapeId": "5",
        "zOrder": 1,
        # Both of the master's slots sit at the same point, so the tie falls
        # back to source order and this one is still read second.
        "readingOrder": 1,
        "readingPath": [1],
        "geometry": {"x": 50, "y": 60, "cx": 70, "cy": 80},
        "placeholderType": "body",
        "placeholderIndex": "1",
        "slideShapeId": "10",
        "slideZOrder": 1,
        "slideZPath": [1],
        "slideReadingOrder": 1,
        "slideReadingPath": [1],
        "slideGeometry": {"x": 5, "y": 6, "cx": 7, "cy": 8},
    }


def test_an_applied_record_carries_both_parts_own_reading_order() -> None:
    """The inherited shape's rank and the applying slot's rank, side by side.

    Each is the rank the shape holds among its own part's siblings and neither
    is derived from the other's coordinates. The slide places its title slot
    above its body slot, so the title reads first there; the layout places its
    title shape below its body shape, so the same slot's text reads second
    where it was authored. Both transforms stay the ones their own part wrote.
    """
    content = pptx(
        shapes=sp("9", at=(0, 10, 3, 4), placeholder=' type="title"')
        + sp("10", at=(0, 900, 7, 8), placeholder=' type="body" idx="1"')
    )

    assert [
        (
            item.source_locator["inheritedFrom"],
            item.source_locator["shapeId"],
            item.source_locator["readingOrder"],
            item.source_locator["readingPath"],
            item.source_locator["slideShapeId"],
            item.source_locator["slideReadingOrder"],
            item.source_locator["slideReadingPath"],
        )
        for item in applied(content)
    ] == [
        ("layout", "2", 1, [1], "9", 0, [0]),
        ("master", "5", 1, [1], "10", 1, [1]),
    ]
    assert [
        (item.source_locator["geometry"], item.source_locator["slideGeometry"])
        for item in applied(content)
    ] == [
        ({"x": 10, "y": 20, "cx": 30, "cy": 40}, {"x": 0, "y": 10, "cx": 3, "cy": 4}),
        ({"x": 50, "y": 60, "cx": 70, "cy": 80}, {"x": 0, "y": 900, "cx": 7, "cy": 8}),
    ]


def test_a_bare_reference_is_the_body_slot_at_the_default_index() -> None:
    """Both attributes default, so a bare ``p:ph`` names ``body`` at index 0."""
    layout = part(
        "sldLayout",
        sp("2", text="Default body", at=LAYOUT_AT, placeholder="")
        + sp("3", text="Titled", at=SECOND_AT, placeholder=' type="title"'),
    )

    # A bare reference on the slide, and the same slot written out in full.
    for reference in ("", ' type="body"', ' type="body" idx="0"', ' idx="0"'):
        assert shown(
            pptx(shapes=sp("9", at=SLIDE_AT, placeholder=reference), layout=layout)
        ) == [("layout", "2", "9", "Default body")]

    # The title at the same default index is told apart by its type alone.
    assert shown(
        pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'), layout=layout)
    ) == [("layout", "3", "9", "Titled")]


def test_a_stated_index_is_the_slot_whatever_type_either_end_gives_it() -> None:
    """``idx`` is the link OOXML defines, so it decides before the type does."""
    layout = part(
        "sldLayout",
        sp("2", text="At seven", at=LAYOUT_AT, placeholder=' type="body" idx="7"')
        + sp("3", text="At two", at=SECOND_AT, placeholder=' type="subTitle" idx="2"'),
    )
    content = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="2"'), layout=layout
    )

    assert shown(content) == [("layout", "3", "9", "At two")]

    # The same slot however the index is spelled, and never another one.
    assert shown(
        pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' idx="0007"'), layout=layout)
    ) == [("layout", "2", "9", "At seven")]
    assert shown(
        pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="9"'), layout=layout)
    ) == []


def test_an_empty_layout_slot_falls_through_to_the_master_behind_it() -> None:
    """The chain is one hop at a time, and only an empty hop is passed over."""
    content = pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="1"'))

    assert shown(content) == [("master", "5", "9", "Master body")]

    # A layout that saved text for the slot is what the slide shows, so the
    # master's own text for the same slot is not reported behind it.
    assert shown(pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'))) == [
        ("layout", "2", "9", "Layout title")
    ]

    # Both ends empty is a slot nobody wrote text for, so nothing is invented.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
            layout=part("sldLayout", sp("2", at=LAYOUT_AT, placeholder=' type="title"')),
        )
    ) == []

    # A layout relating no master leaves its own empty slot showing nothing.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="1"'),
            layout_rels=None,
        )
    ) == []


def test_a_slot_the_slide_filled_itself_stays_the_slides_own_segment() -> None:
    """The slide's text is reported once, as its own, with no prompt behind it."""
    content = pptx(
        shapes=sp("9", text="Real title", at=SLIDE_AT, placeholder=' type="title"')
    )

    assert every(content) == ["Real title"]
    assert shown(content) == []
    located = extract_pptx(content, capture_id=CAPTURE).segments[0].source_locator
    assert (located["shapeId"], located["placeholderType"]) == ("9", "title")
    assert "inheritedFrom" not in located

    # A slot holding only empty runs saved no text, so it is still empty.
    assert shown(
        pptx(
            shapes=sp(
                "9", text_body=body(runs("", "")), at=SLIDE_AT, placeholder=' type="title"'
            )
        )
    ) == [("layout", "2", "9", "Layout title")]


def test_a_slot_no_placeholder_matches_inherits_nothing_from_either_part() -> None:
    """Non-matching text was authored for another slot, so none of it is shown."""
    assert shown(pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' idx="4"'))) == []
    assert shown(pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="ftr"'))) == []

    # The chain to the master runs through the layout, so a slot the layout
    # does not name is not looked up behind it either.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="6"'),
            layout=part("sldLayout", sp("2", at=LAYOUT_AT, placeholder=' type="title"')),
            master=part(
                "sldMaster",
                sp("4", text="Deep body", at=MASTER_AT, placeholder=' type="body" idx="6"'),
            ),
        )
    ) == []

    # A slide holding no placeholder at all resolves nothing.
    assert shown(pptx(shapes=sp("9", text="Free", at=SLIDE_AT))) == []
    assert shown(pptx()) == []


def test_more_than_one_applicable_placeholder_is_refused_per_part() -> None:
    """Two leave which placeholder the slot means ambiguous, on either part."""
    doubled = part(
        "sldLayout",
        sp("2", text="One", at=LAYOUT_AT, placeholder=' type="body" idx="2"')
        + sp("3", text="Two", at=SECOND_AT, placeholder=' type="subTitle" idx="2"'),
    )
    with pytest.raises(
        OOXMLInvalid,
        match="PPTX slide layout holds more than one applicable placeholder",
    ):
        extract_pptx(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' idx="2"'), layout=doubled
            ),
            capture_id=CAPTURE,
        )

    # At the default index the type is what matches, so a repeat of one type is
    # what is ambiguous there.
    with pytest.raises(OOXMLInvalid, match="slide layout holds more than one"):
        extract_pptx(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
                layout=part(
                    "sldLayout",
                    sp("2", text="One", at=LAYOUT_AT, placeholder=' type="title"')
                    + sp("3", text="Two", at=SECOND_AT, placeholder=' type="title"'),
                ),
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(
        OOXMLInvalid,
        match="PPTX slide master holds more than one applicable placeholder",
    ):
        extract_pptx(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="1"'),
                master=part(
                    "sldMaster",
                    sp("4", text="One", at=MASTER_AT, placeholder=' idx="1"')
                    + sp("5", text="Two", at=SECOND_AT, placeholder=' type="pic" idx="1"'),
                ),
            ),
            capture_id=CAPTURE,
        )

    # Ambiguity is a property of the slot that asks: a part may hold two
    # placeholders no slide placeholder here resolves against.
    assert shown(
        pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'), layout=doubled)
    ) == []


def test_a_malformed_placeholder_identifier_is_refused_wherever_it_is_written() -> None:
    """Both halves are their own type, on the slide and on each part behind it."""
    for reference in (' type="Body"', ' type="footer"', ' type=""', ' type="body "'):
        with pytest.raises(OOXMLInvalid, match="PPTX placeholder type is malformed"):
            extract_pptx(
                pptx(shapes=sp("9", at=SLIDE_AT, placeholder=reference)),
                capture_id=CAPTURE,
            )
    for reference in (' idx="x"', ' idx=""', ' idx="-1"', ' idx=" 1"', ' idx="1.0"'):
        with pytest.raises(OOXMLInvalid, match="PPTX placeholder index is malformed"):
            extract_pptx(
                pptx(shapes=sp("9", at=SLIDE_AT, placeholder=reference)),
                capture_id=CAPTURE,
            )

    # ST_Index is an unsigned int, so the range is checked as well as the form.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' idx="4294967295"'),
            layout=part(
                "sldLayout",
                sp("2", text="Last", at=LAYOUT_AT, placeholder=' idx="4294967295"'),
            ),
        )
    ) == [("layout", "2", "9", "Last")]
    for reference in (' idx="4294967296"', ' idx="99999999999"'):
        with pytest.raises(OOXMLInvalid, match="PPTX placeholder index is malformed"):
            extract_pptx(
                pptx(shapes=sp("9", at=SLIDE_AT, placeholder=reference)),
                capture_id=CAPTURE,
            )

    # Validated on an inherited part too, whichever one writes it, and whether
    # or not the slot it names is the one being resolved.
    for layout in (
        part("sldLayout", sp("2", at=LAYOUT_AT, placeholder=' type="Title"')),
        part(
            "sldLayout",
            LAYOUT_TITLE + sp("3", at=SECOND_AT, placeholder=' type="obj" idx="x"'),
        ),
    ):
        with pytest.raises(OOXMLInvalid, match="PPTX placeholder (type|index) is malformed"):
            extract_pptx(
                pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'), layout=layout),
                capture_id=CAPTURE,
            )
    with pytest.raises(OOXMLInvalid, match="PPTX placeholder type is malformed"):
        extract_pptx(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
                master=part("sldMaster", sp("4", at=MASTER_AT, placeholder=' type="dgm "')),
            ),
            capture_id=CAPTURE,
        )


def test_the_strict_parts_resolve_the_same_slots_as_the_transitional_ones() -> None:
    """Strict and Transitional name the one layout type, and the one master."""
    strict = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
        + sp("10", at=SECOND_AT, placeholder=' type="body" idx="1"'),
        layout=part("sldLayout", LAYOUT_TITLE + LAYOUT_BODY, namespace=STRICT_MAIN_NS),
        master=part("sldMaster", MASTER_TITLE + MASTER_BODY, namespace=STRICT_MAIN_NS),
        slide_rels=LAYOUT_RELATION.replace(LAYOUT_TYPE, STRICT_LAYOUT_TYPE),
        layout_rels=MASTER_RELATION.replace(MASTER_TYPE, STRICT_MASTER_TYPE),
    )
    transitional = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
        + sp("10", at=SECOND_AT, placeholder=' type="body" idx="1"')
    )

    assert shown(strict) == shown(transitional)
    assert dict(applied(strict)[0].source_locator) == EXPECTED

    # A relationship of another type reaches neither part, so nothing resolves.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
            slide_rels=LAYOUT_RELATION.replace(LAYOUT_TYPE, LAYOUT_TYPE + "Extra"),
        )
    ) == []


def test_a_slide_turning_inherited_shapes_off_resolves_against_neither_part() -> None:
    """Neither part is opened, so there is nothing for a slot to resolve to."""
    for value in ("0", "false", "off"):
        content = pptx(
            show_master=value, shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
        )
        assert shown(content) == []
        assert every(content) == []

    for value in ("1", "true", "on"):
        assert shown(
            pptx(
                show_master=value, shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
            )
        ) == [("layout", "2", "9", "Layout title")]


def test_a_layout_turning_inherited_shapes_off_keeps_its_own_slots_only() -> None:
    """The flag closes the part behind the one writing it, so a fallback stops."""
    for value in ("0", "false", "off"):
        closed = part("sldLayout", LAYOUT_TITLE + LAYOUT_BODY, show_master=value)
        # The layout's own saved text still applies.
        assert shown(
            pptx(shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'), layout=closed)
        ) == [("layout", "2", "9", "Layout title")]
        # Its empty slot no longer falls through to the master's text.
        assert shown(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="1"'),
                layout=closed,
            )
        ) == []

    for value in ("1", "true", "on"):
        assert shown(
            pptx(
                shapes=sp("9", at=SLIDE_AT, placeholder=' type="body" idx="1"'),
                layout=part("sldLayout", LAYOUT_TITLE + LAYOUT_BODY, show_master=value),
            )
        ) == [("master", "5", "9", "Master body")]


def test_applied_text_is_reported_after_each_parts_own_text_in_slide_order() -> None:
    """One stable order: the slide, then each part behind it, then the slots."""
    content = pptx(
        shapes=sp("9", text="Mine", at=SLIDE_AT)
        + sp("10", at=SECOND_AT, placeholder=' type="body" idx="1"')
        + sp("11", at=(9, 9, 9, 9), placeholder=' type="title"'),
        layout=part("sldLayout", LAYOUT_TITLE + LAYOUT_BODY + sp("6", text="Free", at=(7, 7, 7, 7))),
    )

    assert every(content) == ["Mine", "Free", "Master body", "Layout title"]
    assert shown(content) == [
        ("master", "5", "10", "Master body"),
        ("layout", "2", "11", "Layout title"),
    ]


def test_a_slot_inside_a_group_names_the_path_the_slide_shape_sits_on() -> None:
    """The applying shape is named where it actually is, group levels and all."""
    content = pptx(
        shapes=sp(
            "8",
            kind="grpSp",
            at=SECOND_AT,
            children=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
        )
    )

    assert dict(applied(content)[0].source_locator) == EXPECTED | {
        "slideGroupPath": ["8"],
        "slideZPath": [0, 0],
        "slideReadingPath": [0, 0],
    }


def test_a_hidden_slide_marks_the_text_its_slots_show_hidden() -> None:
    """The visibility is the slide's own; the layout has none of its own."""
    shapes = sp("9", at=SLIDE_AT, placeholder=' type="title"')

    assert [item.visibility for item in applied(pptx(shapes=shapes, hidden=True))] == [
        "hidden"
    ]
    assert applied(pptx(shapes=shapes, hidden=True))[0].source_locator["slideHidden"] is True
    assert [item.visibility for item in applied(pptx(shapes=shapes))] == ["visible"]


def test_paragraphs_and_runs_are_numbered_inside_the_shape_that_saved_them() -> None:
    """Indexes are the saving shape's own, and an empty run keeps its position."""
    content = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
        layout=part(
            "sldLayout",
            sp(
                "2",
                text_body=body(runs("One", "", "Two"), runs("Three")),
                at=LAYOUT_AT,
                placeholder=' type="title"',
            ),
        ),
    )

    assert [
        (item.source_locator["paragraph"], item.source_locator["run"], item.text)
        for item in applied(content)
    ] == [(0, 0, "One"), (0, 2, "Two"), (1, 0, "Three")]


def test_an_exact_visual_duplicate_of_what_the_slide_shows_is_dropped() -> None:
    """Role, normalised text and the saving shape's encoded transform, all three."""
    content = pptx(
        shapes=sp("8", text="Layout title", at=LAYOUT_AT)
        + sp("9", at=SLIDE_AT, placeholder=' type="title"')
    )

    assert every(content) == ["Layout title"]
    assert shown(content) == []

    # The same words at another transform are two authored things.
    assert shown(
        pptx(
            shapes=sp("8", text="Layout title", at=SECOND_AT)
            + sp("9", at=SLIDE_AT, placeholder=' type="title"')
        )
    ) == [("layout", "2", "9", "Layout title")]


def test_a_slot_that_holds_no_text_body_inherits_nothing_through_a_frame_or_picture(
) -> None:
    """Only the shape kinds the inherited pass reads take part in a slot."""
    for kind in ("pic", "graphicFrame"):
        assert shown(
            pptx(shapes=sp("9", kind=kind, at=SLIDE_AT, placeholder=' type="title"'))
        ) == []

    # A connector is read like a shape, so an empty one resolves its slot.
    assert shown(
        pptx(shapes=sp("9", kind="cxnSp", at=SLIDE_AT, placeholder=' type="title"'))
    ) == [("layout", "2", "9", "Layout title")]

    # And a placeholder on the inherited part is only read where the pass reads
    # shapes, so a frame there supplies no slot.
    assert shown(
        pptx(
            shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
            layout=part(
                "sldLayout",
                sp(
                    "2",
                    kind="graphicFrame",
                    text="Framed",
                    at=LAYOUT_AT,
                    placeholder=' type="title"',
                ),
            ),
        )
    ) == []


def test_applied_work_counts_against_the_slides_own_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving a slot is the slide's work, wherever the text was saved."""
    content = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"'),
        layout=part(
            "sldLayout",
            sp(
                "2",
                text_body=body(runs("a", "b"), runs("c", "d")),
                at=LAYOUT_AT,
                placeholder=' type="title"',
            ),
        ),
    )

    assert len(applied(content)) == 4

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 1)
    with pytest.raises(OOXMLInvalid, match="PPTX slide has too many paragraphs"):
        extract_pptx(content, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 20_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 3)
    with pytest.raises(OOXMLInvalid, match="PPTX slide has too many runs"):
        extract_pptx(content, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 100_000)

    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 1)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(content, capture_id=CAPTURE)


def test_whether_a_prompt_holds_text_is_read_once_per_prompt_not_once_per_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is the prompt's own property, so it is read where the shape was met.

    Three slide slots naming the same one, so a read per applying slot would
    multiply one part's runs by the number of placeholders the slide writes.
    """
    held = ooxml_extractors._pptx_holds_text
    reads: list[bool] = []

    def recording(shape: ElementTree.Element) -> bool:
        reads.append(held(shape))
        return reads[-1]

    monkeypatch.setattr(ooxml_extractors, "_pptx_holds_text", recording)
    content = pptx(
        shapes=sp("9", at=SLIDE_AT, placeholder=' type="title"')
        + sp("10", at=SECOND_AT, placeholder=' type="title"')
        + sp("11", at=(9, 9, 9, 9), placeholder=' type="title"'),
        layout=part("sldLayout", sp("2", at=LAYOUT_AT, placeholder=' type="title"')),
        master=part(
            "sldMaster",
            sp("4", text="Master title", at=MASTER_AT, placeholder=' type="title"'),
        ),
    )

    # Once for the layout's empty prompt and once for the master's, in the
    # order the two parts were read -- and not again for the second and third
    # slot resolving through the same pair.
    assert shown(content) == [("master", "4", "9", "Master title")]
    assert reads == [False, True]
