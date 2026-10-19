"""PPTX inherited text: the non-placeholder shapes a layout and master show.

A slide shows what its own shape tree holds and, behind that, whatever its
layout and that layout's master draw themselves. Routing is one hop at a time
and exact: the layout is the one part the slide relates as a layout, the master
the one that layout relates as a master, each through an internal relationship
carrying one of the two URIs OOXML defines for its type and resolving to a part
declared with that type's own content type and rooted at its own element in a
PresentationML namespace.

Only a ``sp`` or ``cxnSp`` directly below each part's own ``cSld``/``spTree`` is
read, and only when it holds no placeholder reference: what text a slide
inherits *into* a placeholder is placeholder applicability, which
``test_pptx_placeholder_applicability`` covers as the pass of its own that it
is. ``showMasterSp`` is honoured where each part writes it, and an
exact visual duplicate -- same role, same normalised text, same encoded
transform -- of something the slide already showed is not reported twice.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The two PresentationML main namespaces an inherited part may be rooted in.
MAIN_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_MAIN_NS = "http://purl.oclc.org/ooxml/presentationml/main"
# Only local names are read below the root, so the DrawingML namespace the text
# bodies and transforms are qualified in is never checked.
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
# The two relationships namespaces, and the two type URIs OOXML defines for each
# of the parts read here.
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
# Every shape tree opens with the group properties of the tree itself. Neither
# element is a shape, so both are skipped without taking a z-order position.
GROUP_PROPERTIES = '<p:nvGrpSpPr><p:cNvPr id="1"/></p:nvGrpSpPr><p:grpSpPr/>'
# The non-visual properties holder each shape kind writes its ``cNvPr`` in.
HOLDERS = {
    "sp": "nvSpPr",
    "cxnSp": "nvCxnSpPr",
    "pic": "nvPicPr",
    "grpSp": "nvGrpSpPr",
    "graphicFrame": "nvGraphicFramePr",
}
# Where the default fixture puts each part's one shape, in EMU.
LAYOUT_AT = (10, 20, 30, 40)
MASTER_AT = (50, 60, 70, 80)


def runs(*texts: str) -> str:
    """``a:r`` runs in the order they are written."""
    return "".join(f"<a:r><a:t>{text}</a:t></a:r>" for text in texts)


def body(*paragraphs: str) -> str:
    """One ``p:txBody``: a shape's own text, paragraph by paragraph."""
    return "<p:txBody>" + "".join(f"<a:p>{p}</a:p>" for p in paragraphs) + "</p:txBody>"


def sp(
    shape_id: str | None = "2",
    *,
    kind: str = "sp",
    text: str | None = "Behind",
    text_body: str | None = None,
    at: tuple[int, int, int, int] | None = LAYOUT_AT,
    transform: str | None = None,
    placeholder: str | None = None,
    children: str = "",
) -> str:
    """One shape; ``at`` is its transform in EMU and None encodes none at all.

    ``placeholder`` is the attribute text of a ``p:ph``, so ``""`` writes the
    bare reference a shape inheriting a body placeholder carries.
    """
    identity = "" if shape_id is None else f' id="{shape_id}"'
    reference = "" if placeholder is None else f"<p:ph{placeholder}/>"
    holder = HOLDERS[kind]
    if transform is None:
        transform = (
            ""
            if at is None
            else f'<a:xfrm><a:off x="{at[0]}" y="{at[1]}"/>'
            f'<a:ext cx="{at[2]}" cy="{at[3]}"/></a:xfrm>'
        )
    if kind == "graphicFrame":
        # A frame writes its transform directly, not inside shape properties.
        properties = transform.replace("a:xfrm", "p:xfrm")
    else:
        name = "grpSpPr" if kind == "grpSp" else "spPr"
        properties = f"<p:{name}>{transform}</p:{name}>"
    if text_body is None:
        text_body = "" if text is None else body(runs(text))
    return (
        f"<p:{kind}><p:{holder}><p:cNvPr{identity}/><p:nvPr>{reference}</p:nvPr>"
        f"</p:{holder}>{properties}{text_body}{children}</p:{kind}>"
    )


def part(
    root: str,
    shapes: str,
    *,
    namespace: str = MAIN_NS,
    show_master: str | None = None,
    common: str = "cSld",
    tree: str = "spTree",
    deeper: bool = False,
) -> str:
    """One layout or master part holding the given shapes in its own tree.

    ``common`` and ``tree`` rename the two elements the tree is reached through,
    and ``deeper`` buries the tree one level below the common slide data, which
    is how a tree written somewhere the schema does not put it is built.
    """
    flag = "" if show_master is None else f' showMasterSp="{show_master}"'
    inner = f"<p:{tree}>{GROUP_PROPERTIES}{shapes}</p:{tree}>"
    if deeper:
        inner = f"<p:extLst><p:ext>{inner}</p:ext></p:extLst>"
    return (
        f'<p:{root} xmlns:p="{namespace}" xmlns:a="{DRAWING_NS}"{flag}>'
        f"<p:{common}>{inner}</p:{common}></p:{root}>"
    )


LAYOUT = part("sldLayout", sp("2", text="Behind", at=LAYOUT_AT))
MASTER = part("sldMaster", sp("3", text="Beneath", at=MASTER_AT))


def pptx(
    *,
    shapes: str = "",
    layout: str | None = LAYOUT,
    master: str | None = MASTER,
    slide_rels: str | None = LAYOUT_RELATION,
    layout_rels: str | None = MASTER_RELATION,
    show_master: str | None = None,
    hidden: bool = False,
    slides: int = 1,
    overrides: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a presentation whose slide sits in front of a layout and a master.

    A part passed as None is simply not in the package, which together with
    ``overrides`` is how a declared-only phantom and an undeclared member are
    built. ``overrides`` defaults to declaring exactly the two parts that are
    there, each under its own content type.
    """
    parts: dict[str, str] = {}
    if layout is not None:
        parts[LAYOUT_PART] = layout
    if master is not None:
        parts[MASTER_PART] = master
    if overrides is None:
        overrides = "".join(
            f'<Override PartName="/{name}" ContentType="{content_type}"/>'
            for name, content_type in ((LAYOUT_PART, LAYOUT_CT), (MASTER_PART, MASTER_CT))
            if name in parts
        )
    parts["[Content_Types].xml"] = f"<Types>{overrides}</Types>"
    shown = ' show="0"' if hidden else ""
    parts["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{MAIN_NS}" xmlns:r="{REL_NS}"><p:sldIdLst>'
        + "".join(
            f'<p:sldId id="{256 + n}" r:id="rId{n + 1}"{shown}/>'
            for n in range(slides)
        )
        + "</p:sldIdLst></p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        "<Relationships>"
        + "".join(
            f'<Relationship Id="rId{n + 1}" Target="slides/slide{n + 1}.xml"/>'
            for n in range(slides)
        )
        + "</Relationships>"
    )
    flag = "" if show_master is None else f' showMasterSp="{show_master}"'
    for n in range(slides):
        parts[f"ppt/slides/slide{n + 1}.xml"] = (
            f'<p:sld xmlns:p="{MAIN_NS}" xmlns:a="{DRAWING_NS}"{flag}><p:cSld>'
            f"<p:spTree>{GROUP_PROPERTIES}{shapes}</p:spTree></p:cSld></p:sld>"
        )
        if slide_rels is not None:
            parts[f"ppt/slides/_rels/slide{n + 1}.xml.rels"] = (
                f"<Relationships>{slide_rels}</Relationships>"
            )
    if layout_rels is not None:
        parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
            f"<Relationships>{layout_rels}</Relationships>"
        )
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def inherited(content: bytes) -> list[ExtractedSegment]:
    """Every segment the inherited pass produced, in the order it emitted them."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if "inheritedFrom" in item.source_locator
    ]


def behind(content: bytes) -> list[tuple[str, str, str | None]]:
    """The (part it came from, shape id, text) of each inherited record."""
    return [
        (
            str(item.source_locator["inheritedFrom"]),
            str(item.source_locator["shapeId"]),
            item.text,
        )
        for item in inherited(content)
    ]


def every(content: bytes) -> list[str | None]:
    """Every body segment's text, in the order extraction emitted it."""
    return [
        item.text
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ]


# The provenance the default fixture's layout record carries. The master record
# differs from it only in which part, relationship and shape it names.
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
    "readingOrder": 0,
    "readingPath": [0],
    "geometry": {"x": 10, "y": 20, "cx": 30, "cy": 40},
    "paragraph": 0,
    "run": 0,
}
FROM_MASTER = EXPECTED | {
    "part": MASTER_PART,
    "relationshipId": "rId3",
    "inheritedFrom": "master",
    "shapeId": "3",
    "geometry": {"x": 50, "y": 60, "cx": 70, "cy": 80},
}


def test_the_layout_then_the_master_is_reported_behind_the_slides_own_text() -> None:
    """One pass each, in that order, after everything the slide itself holds."""
    content = pptx(shapes=sp("9", text="Mine", at=(1, 2, 3, 4)))

    assert every(content) == ["Mine", "Behind", "Beneath"]
    assert behind(content) == [("layout", "2", "Behind"), ("master", "3", "Beneath")]
    first, second = inherited(content)
    assert (first.semantic_role, first.visibility) == ("body", "visible")
    assert (first.extraction_method, first.structured_value) == ("direct", None)
    assert dict(first.source_locator) == EXPECTED
    assert dict(second.source_locator) == FROM_MASTER


def test_the_strict_relationship_types_reach_the_same_two_parts() -> None:
    """Strict and Transitional each name the one layout type, and the one master."""
    strict = pptx(
        layout=part(
            "sldLayout", sp("2", text="Behind", at=LAYOUT_AT), namespace=STRICT_MAIN_NS
        ),
        master=part(
            "sldMaster", sp("3", text="Beneath", at=MASTER_AT), namespace=STRICT_MAIN_NS
        ),
        slide_rels=LAYOUT_RELATION.replace(LAYOUT_TYPE, STRICT_LAYOUT_TYPE),
        layout_rels=MASTER_RELATION.replace(MASTER_TYPE, STRICT_MASTER_TYPE),
    )

    assert behind(strict) == behind(pptx())
    assert dict(inherited(strict)[0].source_locator) == EXPECTED

    # Scheme and host are case-insensitive, so folding them still qualifies.
    shouted = "HTTP://SCHEMAS.OPENXMLFORMATS.ORG" + LAYOUT_TYPE.partition(".org")[2]
    assert behind(
        pptx(slide_rels=LAYOUT_RELATION.replace(LAYOUT_TYPE, shouted))
    ) == behind(pptx())


def test_a_slide_or_layout_relating_nothing_inherits_nothing_and_is_not_refused() -> (
    None
):
    """Inheriting is what a package offers, not something every slide must."""
    assert behind(pptx(slide_rels=None)) == []
    assert behind(pptx(slide_rels="<Relationship Id='rId2' Target='slide1.xml'/>")) == []

    # A layout that relates no master shows its own text and nothing behind it.
    assert behind(pptx(layout_rels=None)) == [("layout", "2", "Behind")]

    # Neither part is reached by its name, so a member nothing relates is inert.
    assert behind(
        pptx(
            slide_rels=None,
            members={"ppt/slideLayouts/slideLayout9.xml": LAYOUT},
            overrides=f'<Override PartName="/{LAYOUT_PART}" ContentType="{LAYOUT_CT}"/>'
            f'<Override PartName="/{MASTER_PART}" ContentType="{MASTER_CT}"/>'
            '<Override PartName="/ppt/slideLayouts/slideLayout9.xml"'
            f' ContentType="{LAYOUT_CT}"/>',
        )
    ) == []


def test_a_relationship_of_another_type_reaches_neither_part() -> None:
    """Every target below is the real, declared part; only the type fails."""
    for kind in (
        # Same tail, different host.
        "http://layouts.example.invalid/officeDocument/2006/relationships/slideLayout",
        # The defined URI with something appended, and with something prepended.
        LAYOUT_TYPE + "Extra",
        "https://proxy.example.invalid/" + LAYOUT_TYPE,
        # The path is case-sensitive, so a folded path names another type.
        LAYOUT_TYPE.replace("slideLayout", "slidelayout"),
        # The lookalike the sibling suites use for an inert slide relationship.
        "urn:x/officeDocument/relationships/slideLayout",
        # A master type on the slide does not reach the master: a slide relates
        # its layout, and only that layout relates the master.
        MASTER_TYPE,
    ):
        assert behind(pptx(slide_rels=LAYOUT_RELATION.replace(LAYOUT_TYPE, kind))) == []

    for kind in (MASTER_TYPE + "Extra", LAYOUT_TYPE):
        # A layout type on the layout does not reach a second layout either.
        assert behind(
            pptx(layout_rels=MASTER_RELATION.replace(MASTER_TYPE, kind))
        ) == [("layout", "2", "Behind")]


def test_more_than_one_qualifying_relationship_is_refused() -> None:
    """Two leave which part is meant ambiguous, whichever end names them."""
    doubled = LAYOUT_RELATION + LAYOUT_RELATION.replace('Id="rId2"', 'Id="rId4"')
    with pytest.raises(
        OOXMLInvalid, match="PPTX slide names more than one slide layout relationship"
    ):
        extract_pptx(pptx(slide_rels=doubled), capture_id=CAPTURE)

    both = LAYOUT_RELATION + LAYOUT_RELATION.replace(
        LAYOUT_TYPE, STRICT_LAYOUT_TYPE
    ).replace('Id="rId2"', 'Id="rId4"')
    with pytest.raises(OOXMLInvalid, match="more than one slide layout relationship"):
        extract_pptx(pptx(slide_rels=both), capture_id=CAPTURE)

    masters = MASTER_RELATION + MASTER_RELATION.replace('Id="rId3"', 'Id="rId5"')
    with pytest.raises(
        OOXMLInvalid,
        match="PPTX slide layout names more than one slide master relationship",
    ):
        extract_pptx(pptx(layout_rels=masters), capture_id=CAPTURE)


def test_an_external_or_unsafe_relationship_is_refused() -> None:
    """The part has to be in this package, reached by a safe internal target."""
    external = (
        f'<Relationship Id="rId2" Type="{LAYOUT_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/slideLayout1.xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="slide layout relationship is external"):
        extract_pptx(pptx(slide_rels=external), capture_id=CAPTURE)

    external_master = (
        f'<Relationship Id="rId3" Type="{MASTER_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/slideMaster1.xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="slide master relationship is external"):
        extract_pptx(pptx(layout_rels=external_master), capture_id=CAPTURE)

    traversal = LAYOUT_RELATION.replace(
        "../slideLayouts/slideLayout1.xml", "../../../etc/passwd"
    )
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(slide_rels=traversal), capture_id=CAPTURE)


def test_an_undeclared_wrongly_declared_or_missing_part_is_refused() -> None:
    """The target has to be declared the part it is read as, not merely named it."""
    master_only = f'<Override PartName="/{MASTER_PART}" ContentType="{MASTER_CT}"/>'
    for overrides in (
        # Declared as nothing at all.
        master_only,
        # Declared as the master, which is the other part read here.
        master_only + f'<Override PartName="/{LAYOUT_PART}" ContentType="{MASTER_CT}"/>',
        # Declared as a notes slide, which is neither.
        master_only
        + f'<Override PartName="/{LAYOUT_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.notesSlide+xml"/>',
    ):
        with pytest.raises(
            OOXMLInvalid,
            match="slide layout relationship does not name a slide layout part",
        ):
            extract_pptx(pptx(overrides=overrides), capture_id=CAPTURE)

    layout_only = f'<Override PartName="/{LAYOUT_PART}" ContentType="{LAYOUT_CT}"/>'
    for overrides in (
        layout_only,
        layout_only + f'<Override PartName="/{MASTER_PART}" ContentType="{LAYOUT_CT}"/>',
    ):
        with pytest.raises(
            OOXMLInvalid,
            match="slide master relationship does not name a slide master part",
        ):
            extract_pptx(pptx(overrides=overrides), capture_id=CAPTURE)

    # A part that is declared and related but is not in the package at all.
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(layout=None), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(master=None), capture_id=CAPTURE)


def test_a_part_in_another_namespace_or_at_another_root_is_refused() -> None:
    """The declared type says what the part is; the root says so itself."""
    shape = sp("2", text="Behind", at=LAYOUT_AT)
    for namespace in ("urn:p", "http://schemas.openxmlformats.org/drawingml/2006/main"):
        with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
            extract_pptx(
                pptx(layout=part("sldLayout", shape, namespace=namespace)),
                capture_id=CAPTURE,
            )
    # The vocabulary is the root's own namespace however it is bound, so a root
    # carrying it as the default namespace is read by its element name alone.
    with pytest.raises(OOXMLInvalid, match="not rooted at its own sldLayout"):
        extract_pptx(
            pptx(
                layout=f'<notesSlide xmlns="{MAIN_NS}" xmlns:p="{MAIN_NS}"'
                f' xmlns:a="{DRAWING_NS}"><p:cSld><p:spTree>{shape}'
                "</p:spTree></p:cSld></notesSlide>"
            ),
            capture_id=CAPTURE,
        )

    # Rooted in the right vocabulary at the wrong element -- including at the
    # other inherited part's own root, which neither reads as its own.
    for root in ("sld", "sldMaster", "cSld"):
        with pytest.raises(
            OOXMLInvalid, match="not rooted at its own sldLayout"
        ):
            extract_pptx(
                pptx(layout=part(root, shape)), capture_id=CAPTURE
            )
    for root in ("sld", "sldLayout", "notesMaster"):
        with pytest.raises(OOXMLInvalid, match="not rooted at its own sldMaster"):
            extract_pptx(pptx(master=part(root, shape)), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(layout=f'<p:sldLayout xmlns:p="{MAIN_NS}">'), capture_id=CAPTURE
        )


def test_only_the_tree_directly_below_the_parts_own_common_data_is_read() -> None:
    """One hop each way: nothing is found by searching the part for a tree."""
    shape = sp("2", text="Behind", at=LAYOUT_AT)
    for built in (
        # The common slide data under another name, so there is no direct one.
        part("sldLayout", shape, common="ext"),
        # A tree under another name, and one buried below the common data.
        part("sldLayout", shape, tree="spTreeOther"),
        part("sldLayout", shape, deeper=True),
    ):
        with pytest.raises(OOXMLInvalid, match="PPTX slide layout has no shape tree"):
            extract_pptx(pptx(layout=built), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="PPTX slide master has no shape tree"):
        extract_pptx(pptx(master=part("sldMaster", shape, deeper=True)), capture_id=CAPTURE)


def test_a_slide_turning_inherited_shapes_off_shows_neither_part() -> None:
    """Both parts are left closed, so nothing in either is even validated."""
    for value in ("0", "false", "off"):
        content = pptx(show_master=value, shapes=sp("9", text="Mine", at=(1, 2, 3, 4)))
        assert behind(content) == []
        assert every(content) == ["Mine"]

    # A layout that could not be read at all is never opened either.
    assert behind(pptx(show_master="0", layout=part("sldLayout", "", tree="x"))) == []

    for value in ("1", "true", "on"):
        assert behind(pptx(show_master=value)) == behind(pptx())


def test_a_layout_turning_inherited_shapes_off_shows_its_own_text_only() -> None:
    """The flag suppresses what is behind the part that writes it, not the part."""
    for value in ("0", "false", "off"):
        content = pptx(
            layout=part(
                "sldLayout", sp("2", text="Behind", at=LAYOUT_AT), show_master=value
            )
        )
        assert behind(content) == [("layout", "2", "Behind")]

    for value in ("1", "true", "on"):
        assert behind(
            pptx(
                layout=part(
                    "sldLayout", sp("2", text="Behind", at=LAYOUT_AT), show_master=value
                )
            )
        ) == behind(pptx())


def test_a_malformed_inherited_shapes_flag_is_refused() -> None:
    """``showMasterSp`` is a boolean, and an absent one shows the shapes."""
    for value in ("", "True", "yes", "2", " 1"):
        with pytest.raises(OOXMLInvalid, match="inherited shapes flag is malformed"):
            extract_pptx(pptx(show_master=value), capture_id=CAPTURE)
        with pytest.raises(OOXMLInvalid, match="inherited shapes flag is malformed"):
            extract_pptx(
                pptx(layout=part("sldLayout", "", show_master=value)),
                capture_id=CAPTURE,
            )


def test_a_placeholder_shape_is_never_reported_from_an_inherited_part() -> None:
    """A placeholder describes a slot, so it is not this part's own text.

    What a slide shows in that slot is applicability, which resolves against
    the slide's own placeholders rather than reporting the prompt behind every
    slot a slide never fills. No slide here holds one, so nothing applies.
    """
    for reference in ('', ' type="title"', ' type="body" idx="1"', ' idx="4"'):
        content = pptx(
            layout=part(
                "sldLayout",
                sp("2", text="Prompt", at=LAYOUT_AT, placeholder=reference)
                + sp("4", text="Free", at=(5, 6, 7, 8)),
            )
        )
        assert behind(content) == [
            ("layout", "4", "Free"),
            ("master", "3", "Beneath"),
        ]

    # Skipping one never moves another: the free shape keeps the position the
    # part wrote it at.
    locator = inherited(content)[0].source_locator
    assert (locator["zOrder"], locator["shapeId"]) == (1, "4")


def test_only_shapes_and_connectors_in_the_tree_itself_are_read() -> None:
    """A group is not entered and a frame or picture is left alone.

    Each still takes its place in the z-order, so skipping one never shifts
    another's position.
    """
    content = pptx(
        layout=part(
            "sldLayout",
            sp("4", kind="pic", text="Picture", at=(1, 1, 1, 1))
            + sp(
                "5",
                kind="grpSp",
                text=None,
                at=(2, 2, 2, 2),
                children=sp("6", text="Grouped", at=(3, 3, 3, 3)),
            )
            + sp("7", kind="graphicFrame", text="Framed", at=(4, 4, 4, 4))
            + sp("8", kind="cxnSp", text="Connector", at=(5, 5, 5, 5))
            + sp("2", text="Behind", at=LAYOUT_AT),
        )
    )

    assert behind(content) == [
        ("layout", "8", "Connector"),
        ("layout", "2", "Behind"),
        ("master", "3", "Beneath"),
    ]
    connector, shape, _master = inherited(content)
    assert (connector.source_locator["zOrder"], connector.source_locator["shapeKind"]) == (
        3,
        "cxnSp",
    )
    assert shape.source_locator["zOrder"] == 4


def ranked(content: bytes) -> list[tuple[object, ...]]:
    """Each inherited record's part and shape, source position beside its rank."""
    return [
        (
            item.source_locator["inheritedFrom"],
            item.source_locator["shapeId"],
            item.source_locator["zOrder"],
            item.source_locator["readingOrder"],
            item.source_locator["readingPath"],
        )
        for item in inherited(content)
    ]


def test_inherited_shapes_are_ranked_in_their_own_parts_reading_order() -> None:
    """The heuristic runs on each inherited part too, top edge then left edge.

    The layout writes the lowest shape first and then the two along its top
    from right to left. Emission stays the source order the part declared,
    ``zOrder`` still records it, and the rank is the top-to-bottom, then
    left-to-right one. The master is one shape of its own and ranks first in
    its own tree, which is the only tree it is ranked in.
    """
    content = pptx(
        layout=part(
            "sldLayout",
            sp("2", text="lowest", at=(0, 1000, 1, 1))
            + sp("4", text="top right", at=(500, 0, 1, 1))
            + sp("6", text="top left", at=(100, 0, 1, 1)),
        )
    )

    assert behind(content) == [
        ("layout", "2", "lowest"),
        ("layout", "4", "top right"),
        ("layout", "6", "top left"),
        ("master", "3", "Beneath"),
    ]
    assert ranked(content) == [
        ("layout", "2", 0, 2, [2]),
        ("layout", "4", 1, 1, [1]),
        ("layout", "6", 2, 0, [0]),
        ("master", "3", 0, 0, [0]),
    ]


def test_inherited_shapes_at_one_point_keep_their_source_order() -> None:
    """The rank is a total order here too, so a tie falls back to the position."""
    content = pptx(
        layout=part(
            "sldLayout",
            sp("2", text="first", at=LAYOUT_AT)
            + sp("4", text="second", at=LAYOUT_AT)
            + sp("6", text="third", at=LAYOUT_AT),
        )
    )

    assert behind(content) == [
        ("layout", "2", "first"),
        ("layout", "4", "second"),
        ("layout", "6", "third"),
        ("master", "3", "Beneath"),
    ]
    assert [item.source_locator["readingOrder"] for item in inherited(content)] == [
        0,
        1,
        2,
        0,
    ]


def test_an_unplaced_inherited_shape_keeps_source_order_after_the_placed() -> None:
    """An offset is read as a pair, so an extent alone is a size without a place.

    Neither of the two unplaced shapes has a position to rank, so both fall
    back to their source order and sit after the one shape that does -- which
    the part wrote last.
    """
    content = pptx(
        layout=part(
            "sldLayout",
            sp("2", text="no transform", at=None)
            + sp(
                "4",
                text="size only",
                transform='<a:xfrm><a:ext cx="10" cy="10"/></a:xfrm>',
            )
            + sp("6", text="placed", at=(0, 9000, 1, 1)),
        )
    )

    assert [
        (item.source_locator["geometry"], item.source_locator["readingOrder"])
        for item in inherited(content)
    ] == [
        (None, 1),
        ({"cx": 10, "cy": 10}, 2),
        ({"x": 0, "y": 9000, "cx": 1, "cy": 1}, 0),
        ({"x": 50, "y": 60, "cx": 70, "cy": 80}, 0),
    ]


def test_an_inherited_rank_is_never_measured_against_the_slides_own_space() -> None:
    """Each part is ranked in its own coordinate space and in no other.

    The slide's own shape sits above everything the layout holds, and the
    layout's two shapes sit far below it. Were the two spaces compared, the
    slide's shape would take the first rank and push the layout's to second and
    third; were a slide position invented for the layout's shapes, their
    recorded transforms would not be the ones the layout encoded. Neither
    happens: the layout's shapes rank 0 and 1 among themselves, the slide's
    shape ranks 0 among its own, and every transform is the one its own part
    wrote.
    """
    content = pptx(
        shapes=sp("9", text="Mine", at=(0, 5, 1, 1)),
        layout=part(
            "sldLayout",
            sp("2", text="layout lower", at=(0, 900, 1, 1))
            + sp("4", text="layout upper", at=(0, 10, 1, 1)),
        ),
    )

    assert ranked(content) == [
        ("layout", "2", 0, 1, [1]),
        ("layout", "4", 1, 0, [0]),
        ("master", "3", 0, 0, [0]),
    ]
    mine = extract_pptx(content, capture_id=CAPTURE).segments[0].source_locator
    assert (mine["shapeId"], mine["readingOrder"], mine["readingPath"]) == ("9", 0, [0])
    assert [item.source_locator["geometry"] for item in inherited(content)] == [
        {"x": 0, "y": 900, "cx": 1, "cy": 1},
        {"x": 0, "y": 10, "cx": 1, "cy": 1},
        {"x": 50, "y": 60, "cx": 70, "cy": 80},
    ]


def test_a_shape_whose_text_is_not_read_still_holds_a_place_in_both_orders() -> None:
    """A picture, a group and a frame are ranked where they sit, like any shape.

    They take a position in the z-order already, and they take one in the
    reading order for the same reason: they are shapes on the part, and
    skipping what they hold never shifts another shape's place in either order.
    Here the three skipped shapes are the three lowest, so the two reported
    shapes keep the first two ranks while the skipped ones hold the rest.
    """
    content = pptx(
        layout=part(
            "sldLayout",
            sp("4", kind="pic", text="Picture", at=(0, 900, 1, 1))
            + sp(
                "5",
                kind="grpSp",
                text=None,
                at=(0, 800, 1, 1),
                children=sp("6", text="Grouped", at=(0, 1, 1, 1)),
            )
            + sp("7", kind="graphicFrame", text="Framed", at=(0, 700, 1, 1))
            + sp("8", kind="cxnSp", text="Connector", at=(0, 5, 1, 1))
            + sp("2", text="Behind", at=(0, 10, 1, 1)),
        )
    )

    assert ranked(content) == [
        ("layout", "8", 3, 0, [0]),
        ("layout", "2", 4, 1, [1]),
        ("master", "3", 0, 0, [0]),
    ]


def test_an_exact_visual_duplicate_of_the_slides_own_text_is_dropped() -> None:
    """Role, normalised text and the exact encoded transform, all three."""
    content = pptx(shapes=sp("9", text="Behind", at=LAYOUT_AT))

    assert every(content) == ["Behind", "Beneath"]
    assert behind(content) == [("master", "3", "Beneath")]

    # Normalised text, so differing whitespace is the same words.
    spaced = pptx(shapes=sp("9", text="Behind  now", at=LAYOUT_AT))
    assert behind(
        pptx(
            shapes=sp("9", text="Behind  now", at=LAYOUT_AT),
            layout=part("sldLayout", sp("2", text=" Behind\tnow ", at=LAYOUT_AT)),
        )
    ) == [("master", "3", "Beneath")]
    assert behind(spaced) == [("layout", "2", "Behind"), ("master", "3", "Beneath")]

    # A master repeat of what the layout already showed goes the same way.
    assert behind(
        pptx(master=part("sldMaster", sp("3", text="Behind", at=LAYOUT_AT)))
    ) == [("layout", "2", "Behind")]


def test_the_same_words_at_another_transform_are_two_authored_things() -> None:
    """Only the same words in the same place are the one thing seen twice."""
    for at in ((11, 20, 30, 40), (10, 20, 30, 41), None):
        content = pptx(shapes=sp("9", text="Behind", at=at))
        assert behind(content) == [
            ("layout", "2", "Behind"),
            ("master", "3", "Beneath"),
        ]

    # Encoding no transform is its own identity, shared by every shape that
    # encodes none, and never equal to one that does.
    assert behind(
        pptx(
            shapes=sp("9", text="Behind", at=None),
            layout=part("sldLayout", sp("2", text="Behind", at=None)),
        )
    ) == [("master", "3", "Beneath")]


def test_a_slides_own_text_is_reported_however_often_it_repeats() -> None:
    """Suppression is asked for by the inherited pass alone."""
    twice = sp("9", text="Behind", at=LAYOUT_AT) + sp("10", text="Behind", at=LAYOUT_AT)

    assert every(pptx(shapes=twice)) == ["Behind", "Behind", "Beneath"]

    # Two identical shapes on one inherited part are one visual thing, though.
    assert behind(
        pptx(
            layout=part(
                "sldLayout",
                sp("2", text="Behind", at=LAYOUT_AT)
                + sp("4", text="Behind", at=LAYOUT_AT),
            )
        )
    ) == [("layout", "2", "Behind"), ("master", "3", "Beneath")]


def test_every_slide_in_front_of_one_layout_reports_its_text() -> None:
    """What is already shown is the slide's own scope, not the package's."""
    content = pptx(slides=2)

    assert [
        (item.source_locator["slide"], item.source_locator["inheritedFrom"], item.text)
        for item in inherited(content)
    ] == [
        (1, "layout", "Behind"),
        (1, "master", "Beneath"),
        (2, "layout", "Behind"),
        (2, "master", "Beneath"),
    ]


def test_a_hidden_slide_marks_its_inherited_text_hidden() -> None:
    """The visibility is the slide's own; the layout has none of its own."""
    assert [item.visibility for item in inherited(pptx(hidden=True))] == ["hidden"] * 2
    assert inherited(pptx(hidden=True))[0].source_locator["slideHidden"] is True
    assert [item.visibility for item in inherited(pptx())] == ["visible"] * 2


def test_paragraphs_and_runs_are_numbered_inside_the_shape_that_holds_them() -> None:
    """Indexes are local to the shape, and an empty run keeps its position."""
    content = pptx(
        layout=part(
            "sldLayout",
            sp(
                "2",
                text_body=body(runs("One", "", "Two"), runs("Three")),
                at=LAYOUT_AT,
            )
            + sp("4", text="Four", at=(5, 6, 7, 8)),
        )
    )

    assert [
        (
            item.source_locator["shapeId"],
            item.source_locator["paragraph"],
            item.source_locator["run"],
            item.text,
        )
        for item in inherited(content)
    ] == [
        ("2", 0, 0, "One"),
        ("2", 0, 2, "Two"),
        ("2", 1, 0, "Three"),
        ("4", 0, 0, "Four"),
        ("3", 0, 0, "Beneath"),
    ]


def test_a_repeated_or_missing_identifier_is_refused_per_inherited_part() -> None:
    """Ids are validated inside each part, because that is where they are unique."""
    with pytest.raises(
        OOXMLInvalid, match="identifier is repeated on one slide layout"
    ):
        extract_pptx(
            pptx(layout=part("sldLayout", sp("2") + sp("2", at=(1, 2, 3, 4)))),
            capture_id=CAPTURE,
        )
    with pytest.raises(
        OOXMLInvalid, match="identifier is repeated on one slide master"
    ):
        extract_pptx(
            pptx(master=part("sldMaster", sp("3") + sp("3", at=(1, 2, 3, 4)))),
            capture_id=CAPTURE,
        )

    for built in (sp(None), sp("x"), sp(""), sp("12345678901")):
        with pytest.raises(OOXMLInvalid, match="no readable identifier"):
            extract_pptx(pptx(layout=part("sldLayout", built)), capture_id=CAPTURE)

    # An id is validated on a shape whose text is not read, too.
    with pytest.raises(OOXMLInvalid, match="no readable identifier"):
        extract_pptx(
            pptx(layout=part("sldLayout", sp(None, kind="pic", text=None))),
            capture_id=CAPTURE,
        )

    # The three parts are three scopes, so one id across them is no repeat.
    assert behind(
        pptx(
            shapes=sp("2", text="Mine", at=(1, 2, 3, 4)),
            master=part("sldMaster", sp("2", text="Beneath", at=MASTER_AT)),
        )
    ) == [("layout", "2", "Behind"), ("master", "2", "Beneath")]


def test_malformed_inherited_geometry_is_refused_wherever_it_is_written() -> None:
    """A transform is validated on every shape in the tree, read or not."""
    for transform in (
        '<a:xfrm><a:off x="1"/></a:xfrm>',
        '<a:xfrm><a:ext cx="1"/></a:xfrm>',
        '<a:xfrm><a:off x="1" y="2"/><a:ext cx="-3" cy="4"/></a:xfrm>',
        '<a:xfrm><a:off x="1.5" y="2"/></a:xfrm>',
    ):
        with pytest.raises(OOXMLInvalid, match="shape geometry is malformed"):
            extract_pptx(
                pptx(layout=part("sldLayout", sp("2", transform=transform))),
                capture_id=CAPTURE,
            )

    # Including on a picture, whose text the inherited pass never reads.
    with pytest.raises(OOXMLInvalid, match="shape geometry is malformed"):
        extract_pptx(
            pptx(
                layout=part(
                    "sldLayout",
                    sp(
                        "4",
                        kind="pic",
                        text=None,
                        transform='<a:xfrm><a:off x="1"/></a:xfrm>',
                    ),
                )
            ),
            capture_id=CAPTURE,
        )

    # A negative offset is a shape placed off the slide, and is kept as written.
    assert inherited(
        pptx(layout=part("sldLayout", sp("2", text="Behind", at=(-1, -2, 3, 4))))
    )[0].source_locator["geometry"] == {"x": -1, "y": -2, "cx": 3, "cy": 4}


def test_inherited_work_counts_against_the_slides_own_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slide's traversal is one budget, however many parts it spans."""
    wordy = part(
        "sldLayout",
        sp("2", text_body=body(runs("a", "b"), runs("c", "d")), at=LAYOUT_AT)
        + sp("4", text_body=body(runs("e", "f"), runs("g", "h")), at=(5, 6, 7, 8)),
    )

    # One shape on the slide, two on the layout and one on the master.
    crowded = pptx(shapes=sp("9", text="Mine", at=(1, 2, 3, 4)), layout=wordy)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_SHAPES", 3)
    with pytest.raises(OOXMLInvalid, match="PPTX slide has too many shapes"):
        extract_pptx(crowded, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_SHAPES", 4_000)
    assert len(inherited(crowded)) == 9

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 3)
    with pytest.raises(OOXMLInvalid, match="PPTX slide has too many paragraphs"):
        extract_pptx(crowded, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 20_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 8)
    with pytest.raises(OOXMLInvalid, match="PPTX slide has too many runs"):
        extract_pptx(crowded, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 100_000)

    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 1)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(pptx(), capture_id=CAPTURE)
