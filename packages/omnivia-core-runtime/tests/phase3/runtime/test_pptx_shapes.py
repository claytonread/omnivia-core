"""PPTX shape-tree traversal, shape provenance and bounds over minimal packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The non-visual holder each shape kind writes its ``cNvPr`` identity inside.
HOLDERS = {
    "sp": "nvSpPr",
    "grpSp": "nvGrpSpPr",
    "graphicFrame": "nvGraphicFramePr",
    "cxnSp": "nvCxnSpPr",
    "pic": "nvPicPr",
}
# A qualified notes slide: the Transitional relationship type, the content type
# the package has to declare for the target, and the part itself.
NOTES_RELATION = (
    '<Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml" Type="http://'
    'schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"/>'
)
# The two relationship-type URIs OOXML defines, and a third that ends the same
# way without being either of them.
TRANSITIONAL_NOTES_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
)
STRICT_NOTES_TYPE = (
    "http://purl.oclc.org/ooxml/officeDocument/relationships/notesSlide"
)
LOOKALIKE_NOTES_TYPE = (
    "http://notes.example.invalid/officeDocument/relationships/notesSlide"
)
NOTES_OVERRIDE = (
    '<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
)
NOTES_MEMBER = {
    "ppt/notesSlides/notesSlide1.xml": '<p:notes xmlns:p="urn:p" xmlns:a="urn:a">'
    "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Speaker detail</a:t></a:r>"
    "</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
}


def pptx(*trees: str, show: tuple[str, ...] = ()) -> bytes:
    """Build a package whose declared slides hold the given shape trees."""
    slides = "".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index + 1}"'
        + (f' show="{show[index]}"' if index < len(show) else "")
        + "/>"
        for index in range(len(trees))
    )
    relations = "".join(
        f'<Relationship Id="rId{index + 1}" Target="slides/slide{index + 1}.xml"/>'
        for index in range(len(trees))
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">'
            f"<p:sldIdLst>{slides}</p:sldIdLst></p:presentation>",
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            f"<Relationships>{relations}</Relationships>",
        )
        for index, tree in enumerate(trees, start=1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld>'
                f"<p:spTree>{tree}</p:spTree></p:cSld></p:sld>",
            )
    return buffer.getvalue()


def paragraphs(*texts: str) -> str:
    """Render each ``|``-separated group of run texts as one paragraph."""
    return "".join(
        "<a:p>" + "".join(f"<a:r><a:t>{run}</a:t></a:r>" for run in text.split("|"))
        + "</a:p>"
        for text in texts
    )


def shape(
    shape_id: str,
    *texts: str,
    kind: str = "sp",
    geometry: str = "",
    placeholder: str = "",
    children: str = "",
) -> str:
    """One shape of ``kind``, with the paragraphs given as plain run text."""
    holder = HOLDERS[kind]
    properties = "grpSpPr" if kind == "grpSp" else "spPr"
    body = paragraphs(*texts)
    return (
        f'<p:{kind}><p:{holder}><p:cNvPr id="{shape_id}"/>'
        + (f"<p:nvPr>{placeholder}</p:nvPr>" if placeholder else "")
        + f"</p:{holder}>"
        + (
            geometry
            if kind == "graphicFrame"
            else f"<p:{properties}>{geometry}</p:{properties}>"
        )
        + (f"<p:txBody>{body}</p:txBody>" if body else "")
        + children
        + f"</p:{kind}>"
    )


def cell(text: str) -> str:
    """One table cell; attributes may lead the text, separated by ``!``."""
    attributes, _, content = text.rpartition("!")
    body = f"<a:txBody>{paragraphs(content)}</a:txBody>" if content else ""
    return f"<a:tc {attributes}>{body}</a:tc>" if attributes else f"<a:tc>{body}</a:tc>"


def table_frame(
    *rows: str,
    shape_id: str = "4",
    columns: int | None = None,
    grid: bool = True,
    extra: str = "",
) -> str:
    """A graphic frame holding one table of ``|``-separated cell texts."""
    width = len(rows[0].split("|")) if columns is None else columns
    declared = (
        "<a:tblGrid>" + '<a:gridCol w="100"/>' * width + "</a:tblGrid>" if grid else ""
    )
    body = "".join(
        "<a:tr>" + "".join(cell(item) for item in row.split("|")) + "</a:tr>"
        for row in rows
    )
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{shape_id}"/>'
        "</p:nvGraphicFramePr><a:graphic><a:graphicData>"
        f"<a:tbl>{declared}{body}</a:tbl>{extra}"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def located(content: bytes) -> list[dict[str, object]]:
    return [
        dict(item.source_locator)
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ]


def texts(content: bytes) -> list[str | None]:
    return [
        item.text
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ]


def cells(content: bytes) -> list[tuple[str | None, dict[str, object]]]:
    return [
        (item.text, dict(item.source_locator))
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "table_cell"
    ]


def grid(content: bytes) -> list[tuple[str | None, object, object, object, object]]:
    """Each cell's text with its position, spans and the anchor covering it."""
    return [
        (
            text,
            (item["tableRow"], item["tableColumn"]),
            item["rowSpan"],
            item["columnSpan"],
            item["mergeAnchor"],
        )
        for text, item in cells(content)
    ]


def test_shapes_are_read_in_declared_tree_order() -> None:
    """Z-order is the sibling index in the tree, and text follows that order."""
    content = pptx(shape("2", "first") + shape("3", "second") + shape("4", "third"))

    assert texts(content) == ["first", "second", "third"]
    assert [(item["shapeId"], item["zOrder"]) for item in located(content)] == [
        ("2", 0),
        ("3", 1),
        ("4", 2),
    ]


def test_group_members_carry_their_group_path_and_per_level_z_order() -> None:
    """A nested member names every group above it and its index at each level."""
    inner = shape("6", kind="grpSp", children=shape("7", "leaf"))
    content = pptx(
        shape("2", "flat")
        + shape("5", kind="grpSp", children=shape("8", "member") + inner)
    )

    assert texts(content) == ["flat", "member", "leaf"]
    assert [
        (item["shapeId"], item["groupPath"], item["zPath"], item["zOrder"])
        for item in located(content)
    ] == [
        ("2", [], [0], 0),
        ("8", ["5"], [1, 0], 0),
        ("7", ["5", "6"], [1, 1, 0], 0),
    ]


def test_group_text_is_reported_once_at_the_member_that_holds_it() -> None:
    """The group itself reports nothing, so no member's text is repeated."""
    content = pptx(
        shape("5", kind="grpSp", children=shape("6", "once") + shape("7", "twice"))
    )

    assert texts(content) == ["once", "twice"]
    assert [item["shapeKind"] for item in located(content)] == ["sp", "sp"]


def test_slide_identity_display_ordinal_and_hidden_flag_ride_every_segment() -> None:
    content = pptx(shape("2", "shown"), shape("3", "skipped"), show=("1", "0"))

    assert [
        (
            item["slideId"],
            item["slide"],
            item["slideHidden"],
            item["part"],
            item["captureId"],
        )
        for item in located(content)
    ] == [
        ("256", 1, False, "ppt/slides/slide1.xml", CAPTURE),
        ("257", 2, True, "ppt/slides/slide2.xml", CAPTURE),
    ]
    assert [
        item.visibility
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ] == ["visible", "hidden"]


def test_paragraph_and_run_order_are_kept_and_an_empty_run_keeps_its_index() -> None:
    """Each run is its own segment; an empty run reports nothing but still counts."""
    content = pptx(shape("2", "one|two", "three") + shape("3", "|four"))

    assert texts(content) == ["one", "two", "three", "four"]
    assert [(item["paragraph"], item["run"]) for item in located(content)] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 1),
    ]


def test_a_field_is_read_as_a_run_in_the_order_it_sits() -> None:
    """A generated value is still a run of the paragraph that declares it."""
    tree = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:spPr/><p:txBody><a:p>'
        "<a:r><a:t>Page </a:t></a:r>"
        '<a:fld id="{1}" type="slidenum"><a:t>3</a:t></a:fld>'
        "</a:p></p:txBody></p:sp>"
    )

    assert texts(pptx(tree)) == ["Page ", "3"]
    assert [item["run"] for item in located(pptx(tree))] == [0, 1]


def test_connector_and_frame_text_is_read_while_a_picture_holds_none() -> None:
    """Every text-bearing kind is named, and a picture only takes a z-order slot.

    A frame that holds no table still reports its own paragraphs in source
    order; a frame that holds one reports table cells instead.
    """
    frame = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4"/></p:nvGraphicFramePr>'
        f"<a:graphic><a:graphicData>{paragraphs('diagram', 'label')}"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )
    content = pptx(shape("2", "label", kind="cxnSp") + shape("3", kind="pic") + frame)

    assert texts(content) == ["label", "diagram", "label"]
    assert [
        (item["shapeKind"], item["zOrder"], item["paragraph"])
        for item in located(content)
    ] == [("cxnSp", 0, 0), ("graphicFrame", 2, 0), ("graphicFrame", 2, 1)]


def test_a_compatibility_wrapper_contributes_its_choice_branch_once() -> None:
    """The wrapper takes one z-order slot and its fallback repeat is not read."""
    wrapped = (
        '<mc:AlternateContent xmlns:mc="urn:mc">'
        f"<mc:Choice Requires=\"a14\">{shape('3', 'chosen')}</mc:Choice>"
        f"<mc:Fallback>{shape('3', 'repeat')}</mc:Fallback>"
        "</mc:AlternateContent>"
    )
    content = pptx(shape("2", "before") + wrapped + shape("4", "after"))

    assert texts(content) == ["before", "chosen", "after"]
    assert [(item["shapeId"], item["zOrder"]) for item in located(content)] == [
        ("2", 0),
        ("3", 1),
        ("4", 2),
    ]


def test_a_fallback_inside_a_shape_does_not_repeat_the_shape_text() -> None:
    tree = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:spPr/><p:txBody>'
        '<mc:AlternateContent xmlns:mc="urn:mc">'
        f"<mc:Choice Requires=\"a14\">{paragraphs('chosen')}</mc:Choice>"
        f"<mc:Fallback>{paragraphs('repeat')}</mc:Fallback>"
        "</mc:AlternateContent></p:txBody></p:sp>"
    )

    assert texts(pptx(tree)) == ["chosen"]


def test_geometry_is_recorded_as_written_and_stays_absent_when_unencoded() -> None:
    """EMU offsets and extents are kept verbatim; nothing is inherited."""
    content = pptx(
        shape(
            "2",
            "placed",
            geometry=(
                '<a:xfrm><a:off x="914400" y="-1000"/>'
                '<a:ext cx="200" cy="300"/></a:xfrm>'
            ),
        )
        + shape("3", "offset only", geometry='<a:xfrm><a:off x="1" y="2"/></a:xfrm>')
        + shape("4", "unplaced")
    )

    assert [item["geometry"] for item in located(content)] == [
        {"x": 914400, "y": -1000, "cx": 200, "cy": 300},
        {"x": 1, "y": 2},
        None,
    ]


def test_a_group_child_space_mapping_is_recorded_but_never_applied() -> None:
    """A member keeps its own offset; the group's child offset resolves nothing."""
    content = pptx(
        shape(
            "5",
            kind="grpSp",
            geometry=(
                '<a:xfrm><a:off x="100" y="100"/><a:ext cx="900" cy="900"/>'
                '<a:chOff x="50" y="50"/><a:chExt cx="450" cy="450"/></a:xfrm>'
            ),
            children=shape(
                "6", "member", geometry='<a:xfrm><a:off x="60" y="70"/></a:xfrm>'
            ),
        )
    )

    assert [item["geometry"] for item in located(content)] == [{"x": 60, "y": 70}]


def test_placeholder_reference_is_recorded_without_being_followed() -> None:
    """The type and index are kept; an unstated type is DrawingML's own default."""
    content = pptx(
        shape("2", "title", placeholder='<p:ph type="ctrTitle"/>')
        + shape("3", "body", placeholder='<p:ph idx="4"/>')
        + shape("4", "free")
    )

    assert [
        (item["placeholderType"], item["placeholderIndex"]) for item in located(content)
    ] == [("ctrTitle", None), ("body", "4"), (None, None)]


def test_a_slide_without_a_shape_tree_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
            '<p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships><Relationship Id="rId1"'
            ' Target="slides/slide1.xml"/></Relationships>',
        )
        archive.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="urn:p"/>')

    with pytest.raises(OOXMLInvalid, match="no shape tree"):
        extract_pptx(buffer.getvalue(), capture_id=CAPTURE)


def test_missing_and_unreadable_shape_identifiers_are_refused() -> None:
    for tree in (
        "<p:sp><p:nvSpPr><p:cNvPr/></p:nvSpPr></p:sp>",
        "<p:sp><p:txBody><a:p/></p:txBody></p:sp>",
        '<p:sp><p:nvSpPr><p:cNvPr id="x1"/></p:nvSpPr></p:sp>',
        '<p:sp><p:nvSpPr><p:cNvPr id="12345678901"/></p:nvSpPr></p:sp>',
        '<p:pic><p:nvPicPr><p:cNvPr name="photo"/></p:nvPicPr></p:pic>',
    ):
        with pytest.raises(OOXMLInvalid, match="no readable identifier"):
            extract_pptx(pptx(tree), capture_id=CAPTURE)


def test_a_repeated_shape_identifier_is_refused_across_group_levels() -> None:
    """Identity is slide-wide, so a group cannot reuse a sibling's identifier."""
    content = pptx(
        shape("2", "first") + shape("5", kind="grpSp", children=shape("2", "again"))
    )

    with pytest.raises(OOXMLInvalid, match="identifier is repeated"):
        extract_pptx(content, capture_id=CAPTURE)
    # The same identifier on another slide names another shape, and is accepted.
    assert texts(pptx(shape("2", "one"), shape("2", "two"))) == ["one", "two"]


def test_malformed_numeric_geometry_is_refused_wherever_it_is_written() -> None:
    for geometry in (
        '<a:xfrm><a:off x="1"/></a:xfrm>',
        '<a:xfrm><a:ext cx="1" cy="wide"/></a:xfrm>',
        '<a:xfrm><a:off x="1.5" y="2"/></a:xfrm>',
        '<a:xfrm><a:ext cx="1" cy=""/></a:xfrm>',
    ):
        with pytest.raises(OOXMLInvalid, match="geometry is malformed"):
            extract_pptx(pptx(shape("2", "t", geometry=geometry)), capture_id=CAPTURE)

    # A group reports no text of its own, and its transform is still read.
    with pytest.raises(OOXMLInvalid, match="geometry is malformed"):
        extract_pptx(
            pptx(
                shape(
                    "5",
                    kind="grpSp",
                    geometry='<a:xfrm><a:off x="1" y="no"/></a:xfrm>',
                    children=shape("6", "inside"),
                )
            ),
            capture_id=CAPTURE,
        )


def test_shape_group_paragraph_run_and_segment_counts_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = shape("90", "deep")
    for level in range(4):
        nested = shape(str(80 - level), kind="grpSp", children=nested)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_SHAPES", 3)
    with pytest.raises(OOXMLInvalid, match="too many shapes"):
        extract_pptx(
            pptx("".join(shape(str(index + 2), "t") for index in range(4))),
            capture_id=CAPTURE,
        )

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_SHAPES", 4_000)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_GROUP_DEPTH", 2)
    with pytest.raises(OOXMLInvalid, match="nested too deeply"):
        extract_pptx(pptx(nested), capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_GROUP_DEPTH", 16)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 2)
    with pytest.raises(OOXMLInvalid, match="too many paragraphs"):
        extract_pptx(pptx(shape("2", "a", "b", "c")), capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 20_000)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 2)
    with pytest.raises(OOXMLInvalid, match="too many runs"):
        extract_pptx(pptx(shape("2", "a|b|c")), capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 100_000)
    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 1)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(pptx(shape("2", "a|b|c")), capture_id=CAPTURE)


def test_paragraph_and_run_bounds_are_counted_across_the_whole_slide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounds are slide-wide, so no shape or paragraph can reset them.

    Each shape below sits inside its own bound, and the empty runs report
    nothing, so neither a per-shape count nor MAX_SEGMENTS would refuse the
    multiplied work these slides ask for.
    """
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 3)
    # Two paragraphs per shape, under a per-shape bound of 3; four in the slide.
    with pytest.raises(OOXMLInvalid, match="slide has too many paragraphs"):
        extract_pptx(
            pptx(shape("2", "a", "b") + shape("3", "c", "d")), capture_id=CAPTURE
        )
    assert texts(pptx(shape("2", "a", "b") + shape("3", "c"))) == ["a", "b", "c"]

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_PARAGRAPHS", 20_000)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_RUNS", 3)
    # Two empty runs per shape: no segment is emitted and no run text is read.
    with pytest.raises(OOXMLInvalid, match="slide has too many runs"):
        extract_pptx(pptx(shape("2", "|") + shape("3", "|")), capture_id=CAPTURE)
    # Two empty runs in each of two paragraphs of one shape trips it the same way.
    with pytest.raises(OOXMLInvalid, match="slide has too many runs"):
        extract_pptx(pptx(shape("2", "|", "|")), capture_id=CAPTURE)
    assert texts(pptx(shape("2", "|") + shape("3", "x"))) == ["x"]


def test_a_negative_extent_is_refused_while_a_negative_offset_is_kept() -> None:
    """An offset is signed EMU; an extent is positive EMU, so it has no sign."""
    for geometry in (
        '<a:xfrm><a:off x="1" y="2"/><a:ext cx="-1" cy="2"/></a:xfrm>',
        '<a:xfrm><a:off x="1" y="2"/><a:ext cx="1" cy="-2"/></a:xfrm>',
        '<a:xfrm><a:ext cx="-0" cy="0"/></a:xfrm>',
    ):
        with pytest.raises(OOXMLInvalid, match="geometry is malformed"):
            extract_pptx(pptx(shape("2", "t", geometry=geometry)), capture_id=CAPTURE)

    content = pptx(
        shape(
            "2",
            "placed",
            geometry='<a:xfrm><a:off x="-1" y="-2"/><a:ext cx="0" cy="3"/></a:xfrm>',
        )
    )

    assert [item["geometry"] for item in located(content)] == [
        {"x": -1, "y": -2, "cx": 0, "cy": 3}
    ]


def test_slides_stay_the_counted_extraction_unit() -> None:
    result = extract_pptx(
        pptx(shape("2", "one"), shape("3", "two")), capture_id=CAPTURE
    )

    assert (result.unit_type, result.expected) == ("slide", 2)


def test_table_cells_carry_their_source_positions_and_grid_dimensions() -> None:
    """Cells are read in row-major source order, each at its own grid position."""
    content = pptx(table_frame("a|b", "c|d"))

    assert [text for text, _ in cells(content)] == ["a", "b", "c", "d"]
    assert [
        (
            item["tableRow"],
            item["tableColumn"],
            item["tableRows"],
            item["tableColumns"],
            item["paragraph"],
            item["run"],
        )
        for _, item in cells(content)
    ] == [
        (0, 0, 2, 2, 0, 0),
        (0, 1, 2, 2, 0, 0),
        (1, 0, 2, 2, 0, 0),
        (1, 1, 2, 2, 0, 0),
    ]
    # A table reports no body text of its own: the cells are where the text is.
    assert texts(content) == []


def test_table_cells_name_their_slide_shape_and_group_position() -> None:
    """A table in a group cites the groups above it exactly as any shape does."""
    content = pptx(
        shape("5", kind="grpSp", children=table_frame("only", shape_id="6")),
        show=("0",),
    )

    ((_, locator),) = cells(content)
    assert (
        locator["captureId"],
        locator["part"],
        locator["slideId"],
        locator["slide"],
        locator["slideHidden"],
        locator["shapeId"],
        locator["shapeKind"],
        locator["groupPath"],
        locator["zPath"],
        locator["zOrder"],
    ) == (
        CAPTURE,
        "ppt/slides/slide1.xml",
        "256",
        1,
        True,
        "6",
        "graphicFrame",
        ["5"],
        [0, 0],
        0,
    )


def test_a_merged_cell_keeps_its_spans_and_a_covered_cell_cites_its_anchor() -> None:
    """Merged text is reported once where it is written, never multiplied out."""
    content = pptx(
        table_frame(
            'gridSpan="2" rowSpan="2"!wide|hMerge="1"!',
            'vMerge="1"!|hMerge="1" vMerge="1"!',
        )
    )

    # Four grid positions, one text: the anchor's. No covered cell repeats it.
    assert grid(content) == [("wide", (0, 0), 2, 2, None)]

    rows = pptx(table_frame('rowSpan="2"!tall|top', 'vMerge="1"!|bottom'))
    assert grid(rows) == [
        ("tall", (0, 0), 2, 1, None),
        ("top", (0, 1), 1, 1, None),
        ("bottom", (1, 1), 1, 1, None),
    ]

    # A covered cell that does hold text keeps it, at its own position.
    kept = pptx(table_frame('gridSpan="2"!left|hMerge="1"!stray'))
    assert grid(kept) == [
        ("left", (0, 0), 1, 2, None),
        ("stray", (0, 1), 1, 1, [0, 0]),
    ]


def test_malformed_overlapping_and_undeclared_merges_are_refused() -> None:
    for rows, reason in (
        # A row that does not fill the declared grid describes no grid.
        (("a|b", "c"), "does not fill its declared grid"),
        # A span that runs past the last column or the last row.
        (('gridSpan="3"!a|b',), "runs past the grid"),
        (('rowSpan="2"!a|b',), "runs past the grid"),
        # Anchoring and continuing the same axis at once.
        (('gridSpan="2" hMerge="1"!a|b',), "both anchors and continues"),
        (
            ('rowSpan="2" vMerge="1"!a|b', 'vMerge="1"!c|d'),
            "both anchors and continues",
        ),
        # Two horizontal merges claiming one position.
        (('gridSpan="2"!a|gridSpan="2"!b|c',), "merges overlap"),
        # An anchor sitting inside another anchor's covered area.
        (('gridSpan="2"!a|rowSpan="2"!b', 'c|vMerge="1"!d'), "merges overlap"),
        # A covered position that declares the wrong axis, or declares nothing.
        (('gridSpan="2"!a|vMerge="1"!',), "not declared by its cell"),
        (('gridSpan="2"!a|b',), "not declared by its cell"),
        # A continuation flag that no merge anchors at all.
        (('a|hMerge="1"!b',), "continues a merge that nothing anchors"),
        (("a|b", 'c|vMerge="1"!d'), "continues a merge that nothing anchors"),
        # Spans and flags that are not readable values.
        (('gridSpan="0"!a|b',), "merge span is malformed"),
        (('gridSpan="two"!a|b',), "merge span is malformed"),
        (('rowSpan="-1"!a|b',), "merge span is malformed"),
        (('hMerge="maybe"!a|b',), "merge flag is malformed"),
    ):
        with pytest.raises(OOXMLInvalid, match=reason):
            extract_pptx(pptx(table_frame(*rows)), capture_id=CAPTURE)


def test_a_table_without_a_grid_or_rows_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="declares no column grid"):
        extract_pptx(pptx(table_frame("a|b", grid=False)), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="column count is outside"):
        extract_pptx(pptx(table_frame("a", columns=0)), capture_id=CAPTURE)

    rowless = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4"/></p:nvGraphicFramePr>'
        "<a:graphic><a:graphicData><a:tbl>"
        '<a:tblGrid><a:gridCol w="100"/></a:tblGrid>'
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    with pytest.raises(OOXMLInvalid, match="row count is outside"):
        extract_pptx(pptx(rowless), capture_id=CAPTURE)


def test_two_tables_below_one_graphic_frame_are_refused() -> None:
    """A frame holds one graphic object, so nothing chooses between two tables."""
    second = (
        '<a:tbl><a:tblGrid><a:gridCol w="100"/></a:tblGrid><a:tr><a:tc/></a:tr></a:tbl>'
    )

    with pytest.raises(OOXMLInvalid, match="more than one table"):
        extract_pptx(pptx(table_frame("a", extra=second)), capture_id=CAPTURE)


def test_table_dimensions_and_cell_work_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grid area is bounded before any cell is read, and across the slide."""
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_TABLE_COLUMNS", 2)
    with pytest.raises(OOXMLInvalid, match="column count is outside"):
        extract_pptx(pptx(table_frame("a|b|c")), capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_TABLE_COLUMNS", 1_024)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_TABLE_ROWS", 2)
    with pytest.raises(OOXMLInvalid, match="row count is outside"):
        extract_pptx(pptx(table_frame("a", "b", "c")), capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_TABLE_ROWS", 4_000)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_TABLE_CELLS", 5)
    # Empty cells hold no text and emit no segment, so only the grid area
    # itself bounds the work two tables on one slide ask for.
    with pytest.raises(OOXMLInvalid, match="too many table cells"):
        extract_pptx(
            pptx(
                table_frame("!|!", "!|!", shape_id="4")
                + table_frame("!|!", "!|!", shape_id="5")
            ),
            capture_id=CAPTURE,
        )
    assert cells(pptx(table_frame("!|!", "!|!"))) == []


def notes_pptx(
    relations: str | None,
    *,
    overrides: str = NOTES_OVERRIDE,
    members: dict[str, str] | None = None,
    tree: str = "",
) -> bytes:
    """One slide carrying the given relationships, with extra members beside it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{overrides}</Types>")
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
            '<p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships><Relationship Id="rId1"'
            ' Target="slides/slide1.xml"/></Relationships>',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree>'
            f"{tree or shape('2', 'slide text')}</p:spTree></p:cSld></p:sld>",
        )
        if relations is not None:
            archive.writestr(
                "ppt/slides/_rels/slide1.xml.rels",
                f"<Relationships>{relations}</Relationships>",
            )
        for name, content in (members or NOTES_MEMBER).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def notes(content: bytes) -> list[tuple[str | None, dict[str, object]]]:
    return [
        (item.text, dict(item.source_locator))
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "note"
    ]


def test_notes_are_read_only_through_the_slides_own_notes_relationship() -> None:
    """The notes part and the slide it belongs to are both named on the segment."""
    content = notes_pptx(NOTES_RELATION)

    ((text, locator),) = notes(content)
    assert text == "Speaker detail"
    assert (
        locator["part"],
        locator["relationshipId"],
        locator["slidePart"],
        locator["slideId"],
        locator["slide"],
        locator["slideHidden"],
        locator["captureId"],
    ) == (
        "ppt/notesSlides/notesSlide1.xml",
        "rId2",
        "ppt/slides/slide1.xml",
        "256",
        1,
        False,
        CAPTURE,
    )
    assert [
        item.visibility
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "note"
    ] == ["annotation"]
    # The slide's own text is still read, unchanged by the notes being there.
    assert texts(content) == ["slide text"]


def test_a_notes_looking_part_nothing_relates_to_is_never_read() -> None:
    """No notes path is derived from the slide's file name, so an orphan is inert.

    Both packages below hold a part at exactly the name a substitution would
    produce, declared with the notes content type, and neither slide relates to
    it.
    """
    unrelated = (
        '<Relationship Id="rId2" Type="urn:x/officeDocument/relationships/slideLayout"'
        ' Target="../slideLayouts/slideLayout1.xml"/>'
    )

    # No relationship part at all, then one that names something else.
    assert notes(notes_pptx(None)) == []
    assert notes(notes_pptx("")) == []
    assert (
        notes(
            notes_pptx(
                unrelated,
                members={
                    **NOTES_MEMBER,
                    "ppt/slideLayouts/slideLayout1.xml": '<p:sldLayout xmlns:p="urn:p"/>',
                },
            )
        )
        == []
    )


def test_a_notes_relationship_of_the_wrong_type_names_no_notes() -> None:
    """Only the notes-slide relationship type reaches a notes part."""
    wrong = (
        '<Relationship Id="rId2" Type="urn:x/officeDocument/relationships/slide"'
        ' Target="../notesSlides/notesSlide1.xml"/>'
    )

    assert notes(notes_pptx(wrong)) == []


def test_both_defined_relationship_uris_reach_the_same_notes_part() -> None:
    """Strict and Transitional each name the one relationship type exactly."""
    for defined in (STRICT_NOTES_TYPE, TRANSITIONAL_NOTES_TYPE):
        relation = (
            '<Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml"'
            f' Type="{defined}"/>'
        )

        assert [text for text, _ in notes(notes_pptx(relation))] == ["Speaker detail"]


def test_a_lookalike_relationship_uri_is_not_a_notes_relationship() -> None:
    """Only the two defined URIs qualify, not any URI with the same tail.

    The lookalike's target is the real, qualified notes part, so the only thing
    keeping it unread is the relationship type failing to qualify.
    """
    lookalike = (
        '<Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml"'
        f' Type="{LOOKALIKE_NOTES_TYPE}"/>'
    )

    assert notes(notes_pptx(lookalike)) == []
    # Not refused either: an unrelated relationship leaves the slide readable.
    assert texts(notes_pptx(lookalike)) == ["slide text"]


def test_a_defined_notes_uri_differing_in_path_case_is_not_the_type() -> None:
    """The path is case-sensitive, so a folded path names a different type."""
    folded = (
        '<Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml"'
        f' Type="{TRANSITIONAL_NOTES_TYPE.lower()}"/>'
    )

    assert notes(notes_pptx(folded)) == []


def test_a_defined_notes_uri_differing_in_host_case_is_the_type() -> None:
    """Scheme and host are case-insensitive, so folding them still qualifies."""
    shouted = (
        '<Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml"'
        ' Type="HTTP://SCHEMAS.OPENXMLFORMATS.ORG'
        '/officeDocument/2006/relationships/notesSlide"/>'
    )

    assert [text for text, _ in notes(notes_pptx(shouted))] == ["Speaker detail"]


def test_a_notes_relationship_to_an_unqualified_part_is_refused() -> None:
    """The target has to be declared a notes part, not merely be named one."""
    with pytest.raises(OOXMLInvalid, match="does not name a notes part"):
        extract_pptx(notes_pptx(NOTES_RELATION, overrides=""), capture_id=CAPTURE)

    layout_type = (
        '<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="does not name a notes part"):
        extract_pptx(
            notes_pptx(NOTES_RELATION, overrides=layout_type), capture_id=CAPTURE
        )


def test_an_external_notes_relationship_is_refused() -> None:
    """A notes slide lives in the package; nothing here resolves a URI."""
    external = (
        f'<Relationship Id="rId2" Type="{TRANSITIONAL_NOTES_TYPE}"'
        ' TargetMode="External" Target="https://example.invalid/notes.xml"/>'
    )

    with pytest.raises(OOXMLInvalid, match="notes relationship is external"):
        extract_pptx(notes_pptx(external), capture_id=CAPTURE)


def test_a_notes_relationship_to_a_missing_part_is_refused() -> None:
    missing = (
        f'<Relationship Id="rId2" Type="{TRANSITIONAL_NOTES_TYPE}"'
        ' Target="../notesSlides/notesSlide9.xml"/>'
    )

    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(notes_pptx(missing), capture_id=CAPTURE)


def test_two_notes_relationships_on_one_slide_are_refused() -> None:
    """Which part holds the notes is ambiguous, so neither is read.

    The second names the Strict URI against the first's Transitional one: both
    qualify, so the slide carries two notes relationships however they spell
    the one type.
    """
    second = (
        f'<Relationship Id="rId3" Type="{STRICT_NOTES_TYPE}"'
        ' Target="../notesSlides/notesSlide2.xml"/>'
    )
    override = NOTES_OVERRIDE + NOTES_OVERRIDE.replace("notesSlide1", "notesSlide2")

    with pytest.raises(OOXMLInvalid, match="more than one notes relationship"):
        extract_pptx(
            notes_pptx(
                NOTES_RELATION + second,
                overrides=override,
                members={
                    **NOTES_MEMBER,
                    "ppt/notesSlides/notesSlide2.xml": NOTES_MEMBER[
                        "ppt/notesSlides/notesSlide1.xml"
                    ],
                },
            ),
            capture_id=CAPTURE,
        )


def test_one_part_declared_twice_is_refused_however_the_two_agree() -> None:
    """An ``Override`` part name is declared once or the package is malformed.

    Two declarations that disagree leave the part with no declared type, and two
    that agree are still a part name written twice, which OPC does not allow.
    Neither is reconciled and neither declaration is picked.
    """
    conflicting = NOTES_OVERRIDE + NOTES_OVERRIDE.replace("notesSlide+xml", "xml")
    # Same part, same type, written twice; then the part name respelled in a
    # case a consumer would fold together.
    repeated = NOTES_OVERRIDE * 2
    refolded = NOTES_OVERRIDE + NOTES_OVERRIDE.replace("notesSlide1", "NOTESSLIDE1")

    for overrides in (conflicting, repeated, refolded):
        with pytest.raises(OOXMLInvalid, match="declares a part twice"):
            extract_pptx(
                notes_pptx(NOTES_RELATION, overrides=overrides), capture_id=CAPTURE
            )


def test_an_empty_notes_part_reports_no_note() -> None:
    empty = {"ppt/notesSlides/notesSlide1.xml": '<p:notes xmlns:p="urn:p"/>'}

    assert notes(notes_pptx(NOTES_RELATION, members=empty)) == []


def test_notes_runs_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    many = {
        "ppt/notesSlides/notesSlide1.xml": '<p:notes xmlns:p="urn:p" xmlns:a="urn:a">'
        + "".join(f"<a:t>{index}</a:t>" for index in range(4))
        + "</p:notes>"
    }

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_NOTE_RUNS", 3)
    with pytest.raises(OOXMLInvalid, match="notes slide has too many runs"):
        extract_pptx(notes_pptx(NOTES_RELATION, members=many), capture_id=CAPTURE)


def test_markup_inside_notes_and_table_cells_stays_inert_text() -> None:
    """Escaped markup is text: nothing is parsed, resolved or executed from it."""
    payload = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;img src=x&gt;"
    expected = "<script>alert(1)</script> & <img src=x>"
    inert = {
        "ppt/notesSlides/notesSlide1.xml": '<p:notes xmlns:p="urn:p" xmlns:a="urn:a">'
        f"<a:t>{payload}</a:t></p:notes>"
    }

    assert [text for text, _ in notes(notes_pptx(NOTES_RELATION, members=inert))] == [
        expected
    ]
    assert [text for text, _ in cells(pptx(table_frame(payload)))] == [expected]
