"""PPTX pictures: the inventory record and the alt text their authors wrote.

Every picture in a slide's shape tree is inventoried where it sits, in the
z-order the tree declared it in, whether or not its author claimed anything
about it. The three ``cNvPr`` claims are kept apart -- ``name`` is what the
authoring tool called the object, ``title`` and ``descr`` are words someone
wrote -- and only the latter two are reported as text, each under ``image_alt``
with the claim that supplied it in the locator's ``altRole``.

Nothing here reaches the image: the blip's relationship is neither resolved nor
opened, so a picture whose image is missing from the package is inventoried
just the same.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The DrawingML main namespace a blip fill's ``a:blip`` and ``a:xfrm`` are
# qualified in, and the relationships namespace its ``r:embed`` is.
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_TYPE = REL_NS + "/image"
# A qualified image relationship, and a part at its target holding bytes that
# are not a decodable image. The package's own relationship loader requires the
# target to be there, so the proof that this slice never reaches the image is
# that these bytes are never decoded and never reported.
IMAGE_RELATION = (
    f'<Relationship Id="rId2" Target="../media/image1.png" Type="{IMAGE_TYPE}"/>'
)
IMAGE_MEMBER = {"ppt/media/image1.png": "not an image anything here decodes"}
BLIP_FILL = (
    f'<p:blipFill><a:blip xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}" r:embed="rId2"/>'
    "</p:blipFill>"
)


def pic(
    shape_id: str = "2",
    *,
    name: str | None = None,
    title: str | None = None,
    descr: str | None = None,
    body: str = "",
    extra: str = "",
) -> str:
    """One ``p:pic``; a claim left None is not written into the markup at all."""
    claims = "".join(
        f' {attribute}="{value}"'
        for attribute, value in (("name", name), ("title", title), ("descr", descr))
        if value is not None
    )
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id}"{claims}/><p:cNvPicPr/>{extra}'
        f"</p:nvPicPr>{body}<p:spPr/></p:pic>"
    )


def group(shape_id: str, children: str) -> str:
    return (
        f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{shape_id}"/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{children}</p:grpSp>"
    )


def pptx(
    tree: str,
    *,
    hidden: bool = False,
    slide_rels: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a one-slide presentation holding the given shape tree."""
    shown = ' show="0"' if hidden else ""
    parts = {
        "[Content_Types].xml": "<Types/>",
        "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">'
        f'<p:sldIdLst><p:sldId id="256" r:id="rId1"{shown}/>'
        "</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": "<Relationships>"
        '<Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld>'
        f"<p:spTree>{tree}</p:spTree></p:cSld></p:sld>",
    }
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


def pictures(content: bytes) -> list[ExtractedSegment]:
    """Every segment the picture route produced, in the order it emitted them."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "image_alt"
        or (item.structured_value or {}).get("kind") == "picture"
    ]


def shape(content: bytes) -> list[tuple[str, str, str | None]]:
    """The (role, shape id, text) of each segment, in source order."""
    return [
        (
            item.semantic_role,
            str(item.source_locator["shapeId"]),
            item.text,
        )
        for item in pictures(content)
    ]


# The provenance the default fixture's inventory record carries. Every other
# record differs from it only in where inside the slide the picture sits.
EXPECTED = {
    "captureId": CAPTURE,
    "part": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
    "shapeId": "2",
    "shapeKind": "pic",
    "groupPath": [],
    "zOrder": 0,
    "zPath": [0],
    "geometry": None,
    "placeholderType": None,
    "placeholderIndex": None,
}


def test_every_picture_is_inventoried_where_it_sits() -> None:
    """One record per picture, carrying the slide and shape identity it sits at."""
    content = pptx(pic("2") + pic("3"))

    assert shape(content) == [
        ("object_inventory", "2", None),
        ("object_inventory", "3", None),
    ]
    first, second = pictures(content)
    assert (first.extraction_method, first.visibility) == ("inventory", "visible")
    assert dict(first.source_locator) == EXPECTED
    assert dict(second.source_locator) == EXPECTED | {
        "shapeId": "3",
        "zOrder": 1,
        "zPath": [1],
    }
    # A picture whose author claimed nothing still reports every field, so one
    # record is shaped like every other's.
    assert first.structured_value == {
        "kind": "picture",
        "shapeName": None,
        "altTitle": None,
        "altDescription": None,
    }


def test_a_hidden_slide_marks_the_record_and_its_alt_text_hidden() -> None:
    content = pptx(pic("2", title="Chart of sales"), hidden=True)

    assert [item.visibility for item in pictures(content)] == ["hidden", "hidden"]
    assert pictures(content)[0].source_locator["slideHidden"] is True
    assert [item.visibility for item in pictures(pptx(pic("2", title="T")))] == [
        "visible",
        "visible",
    ]


def test_a_nested_picture_keeps_the_whole_path_and_its_place_in_source_order() -> None:
    """Group path, per-level index and z-order are the tree's, not rearranged."""
    content = pptx(
        pic("2") + group("5", pic("8", title="Inner") + group("6", pic("7")))
    )

    assert [
        (
            item.source_locator["shapeId"],
            item.source_locator["groupPath"],
            item.source_locator["zPath"],
            item.source_locator["zOrder"],
        )
        for item in pictures(content)
    ] == [
        ("2", [], [0], 0),
        ("8", ["5"], [1, 0], 0),
        # The alt text of the grouped picture rides at the picture's own place.
        ("8", ["5"], [1, 0], 0),
        ("7", ["5", "6"], [1, 1, 0], 0),
    ]


def test_the_three_claims_are_kept_apart_from_one_another() -> None:
    """A generated shape name is not a description, and neither claim is the other."""
    content = pptx(
        pic("2", name="Picture 2", title="Quarterly revenue", descr="A bar chart")
    )

    record, *_alt = pictures(content)
    assert record.structured_value == {
        "kind": "picture",
        "shapeName": "Picture 2",
        "altTitle": "Quarterly revenue",
        "altDescription": "A bar chart",
    }
    # The shape name is never reported as text the author wrote about the image.
    assert [item.text for item in pictures(content)[1:]] == [
        "Quarterly revenue",
        "A bar chart",
    ]


def test_title_then_description_are_reported_under_their_own_alt_role() -> None:
    """Two claims on one picture stay distinguishable by the attribute they came
    from, in one fixed order."""
    content = pptx(
        pic("2", name="Picture 2", title="Title words", descr="Description words")
    )

    _record, title, description = pictures(content)
    assert (title.semantic_role, title.text, title.extraction_method) == (
        "image_alt",
        "Title words",
        "direct",
    )
    assert dict(title.source_locator) == EXPECTED | {"altRole": "title"}
    assert dict(description.source_locator) == EXPECTED | {"altRole": "description"}
    assert description.text == "Description words"
    assert (title.structured_value, description.structured_value) == (None, None)

    # A picture claiming only a description reports that one, under its own role.
    only = pictures(pptx(pic("2", descr="Only a description")))
    assert [item.source_locator.get("altRole") for item in only] == [
        None,
        "description",
    ]


def test_a_missing_claim_and_an_empty_one_are_not_the_same_claim() -> None:
    """A file that declared a description and left it blank said something a file
    that declared none did not, and neither says any text."""
    content = pptx(pic("2", name="", title="", descr=""))

    (record,) = pictures(content)
    assert record.structured_value == {
        "kind": "picture",
        "shapeName": "",
        "altTitle": "",
        "altDescription": "",
    }
    # An empty claim is reported by the record alone; no empty alt segment.
    assert [item.semantic_role for item in pictures(content)] == ["object_inventory"]

    (absent,) = pictures(pptx(pic("2")))
    assert absent.structured_value == {
        "kind": "picture",
        "shapeName": None,
        "altTitle": None,
        "altDescription": None,
    }


def test_two_pictures_claiming_the_same_words_are_both_reported() -> None:
    """Nothing is suppressed as a duplicate: each is an object the author placed."""
    content = pptx(pic("2", descr="Company logo") + pic("3", descr="Company logo"))

    assert shape(content) == [
        ("object_inventory", "2", None),
        ("image_alt", "2", "Company logo"),
        ("object_inventory", "3", None),
        ("image_alt", "3", "Company logo"),
    ]
    assert [
        (item.source_locator["shapeId"], item.source_locator["zOrder"])
        for item in pictures(content)
    ] == [("2", 0), ("2", 0), ("3", 1), ("3", 1)]


def test_a_picture_declaring_its_claims_twice_is_refused() -> None:
    """Two sets of claims leave which are the picture's own ambiguous, and its
    identifier comes from the same element as its author's words."""
    doubled = (
        '<p:pic><p:nvPicPr><p:cNvPr id="2" descr="One"/>'
        '<p:cNvPr id="3" descr="Two"/><p:cNvPicPr/></p:nvPicPr><p:spPr/></p:pic>'
    )
    across = (
        '<p:pic><p:nvPicPr><p:cNvPr id="2" descr="One"/></p:nvPicPr>'
        '<p:nvSpPr><p:cNvPr id="3" descr="Two"/></p:nvSpPr><p:spPr/></p:pic>'
    )

    for tree in (doubled, across):
        with pytest.raises(
            OOXMLInvalid, match="declares its non-visual properties twice"
        ):
            extract_pptx(pptx(tree), capture_id=CAPTURE)

    # One set is read, and a ``cNvPicPr`` beside it is not a second one.
    assert shape(pptx(pic("2", descr="One"))) == [
        ("object_inventory", "2", None),
        ("image_alt", "2", "One"),
    ]


def test_the_image_relationship_is_neither_resolved_nor_opened() -> None:
    """The picture is inventoried from its own properties alone: the image part
    is not decoded, and nothing about the relationship rides on a segment."""
    content = pptx(
        pic("2", descr="A photograph", body=BLIP_FILL),
        slide_rels=IMAGE_RELATION,
        members=IMAGE_MEMBER,
    )

    assert shape(content) == [
        ("object_inventory", "2", None),
        ("image_alt", "2", "A photograph"),
    ]
    # Neither the relationship id nor the part it names rides on any segment,
    # and no text is read off the image: the only text is the author's own.
    for item in pictures(content):
        assert "rId2" not in str(dict(item.source_locator)) + str(item.structured_value)
        assert "media/image1.png" not in str(dict(item.source_locator))

    # A blip naming an id the slide relates to nothing is not resolved either,
    # so it refuses nothing here; and a picture with no blip fill at all is
    # inventoried exactly the same way.
    assert shape(
        pptx(
            pic("2", descr="A photograph", body=BLIP_FILL.replace("rId2", "rId9")),
            slide_rels=IMAGE_RELATION,
            members=IMAGE_MEMBER,
        )
    ) == shape(content)
    assert shape(pptx(pic("2", descr="A photograph"))) == shape(content)


def test_a_claim_longer_than_the_bound_is_clipped_only_in_the_record() -> None:
    """The record rides in a bounded JSON value; the alt text is the content the
    author wrote and is reported whole."""
    long = "w" * 300
    content = pptx(pic("2", descr=long))

    record, alt = pictures(content)
    assert record.structured_value is not None
    assert record.structured_value["altDescription"] == "w" * 256
    assert alt.text == long


def test_markup_inside_a_claim_stays_inert_text() -> None:
    content = pptx(pic("2", descr="&lt;script&gt;alert(1)&lt;/script&gt; &amp; 2"))

    _record, alt = pictures(content)
    assert alt.text == "<script>alert(1)</script> & 2"
