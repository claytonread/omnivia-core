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

    A frame's text is read in source order as the frame's own paragraphs: the
    table structure around it belongs to a later slice, not to this one.
    """
    frame = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4"/></p:nvGraphicFramePr>'
        "<a:graphic><a:graphicData><a:tbl>"
        f"<a:tr><a:tc><p:txBody>{paragraphs('left')}</p:txBody></a:tc>"
        f"<a:tc><p:txBody>{paragraphs('right')}</p:txBody></a:tc></a:tr>"
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    content = pptx(shape("2", "label", kind="cxnSp") + shape("3", kind="pic") + frame)

    assert texts(content) == ["label", "left", "right"]
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
