"""PPTX pictures: the inventory record and the alt text their authors wrote.

Every picture in a slide's shape tree is inventoried where it sits, in the
z-order the tree declared it in, whether or not its author claimed anything
about it. The three ``cNvPr`` claims are kept apart -- ``name`` is what the
authoring tool called the object, ``title`` and ``descr`` are words someone
wrote -- and only the latter two are reported as text, each under ``image_alt``
with the claim that supplied it in the locator's ``altRole``.

The image a picture names is inventoried too, and only inventoried: the embedded
part's relationship, name, declared content type and already-validated byte
length, or a linked reference's target, scheme and external flag. Nothing is
opened, decoded, rendered, OCRed or fetched, and no image ever contributes text.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The DrawingML main namespace a blip fill's ``a:blip`` and ``a:xfrm`` are
# qualified in, and the relationships namespace its ``r:embed`` is. The Strict
# pair of each is what a package written to that profile uses instead.
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
IMAGE_TYPE = REL_NS + "/image"
STRICT_IMAGE_TYPE = STRICT_REL_NS + "/image"
# A qualified image relationship, and a part at its target holding bytes that
# are not a decodable image. The proof that this slice never reaches the image
# is that these bytes are never decoded and never reported: only their length,
# which the package measured while validating the member, ever is.
IMAGE_BYTES = "not an image anything here decodes"
IMAGE_MEMBER = {"ppt/media/image1.png": IMAGE_BYTES}
PNG_DEFAULT = '<Default Extension="png" ContentType="image/png"/>'


def relation(
    relation_id: str = "rId2",
    *,
    target: str = "../media/image1.png",
    kind: str = IMAGE_TYPE,
    external: bool = False,
) -> str:
    """One ``Relationship`` of the slide's own relationship part."""
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{relation_id}" Target="{target}" Type="{kind}"{mode}/>'


IMAGE_RELATION = relation()


def blip(
    *,
    embed: str | None = "rId2",
    link: str | None = None,
    extra: str = "",
    drawing: str = DRAWING_NS,
    rel: str = REL_NS,
) -> str:
    """One ``p:blipFill`` holding a qualified ``a:blip``."""
    named = "".join(
        f' r:{attribute}="{value}"'
        for attribute, value in (("embed", embed), ("link", link))
        if value is not None
    )
    return (
        f'<p:blipFill><a:blip xmlns:a="{drawing}" xmlns:r="{rel}"'
        f"{named}{extra}/></p:blipFill>"
    )


BLIP_FILL = blip()
# What the default fixture's record says about the image it embeds. Every other
# embed record differs from it only in the part, type or length it resolved to.
EMBEDDED = {
    "imageEmbedRelationshipId": "rId2",
    "imageEmbedRelationshipType": IMAGE_TYPE,
    "imagePart": "ppt/media/image1.png",
    "imageContentType": "image/png",
    "imageBytes": len(IMAGE_BYTES),
    "imageLinkRelationshipId": None,
    "imageLinkRelationshipType": None,
    "imageLinkTarget": None,
    "imageLinkScheme": None,
    "imageLinkExternal": None,
}
# What a picture naming no image at all records: every key, each empty.
NO_IMAGE = {key: None for key in EMBEDDED}
# The claim half of a record whose author claimed nothing about the picture.
UNCLAIMED = {
    "kind": "picture",
    "shapeName": None,
    "altTitle": None,
    "altDescription": None,
}


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
    content_types: str = PNG_DEFAULT,
) -> bytes:
    """Build a one-slide presentation holding the given shape tree."""
    shown = ' show="0"' if hidden else ""
    parts = {
        "[Content_Types].xml": f"<Types>{content_types}</Types>",
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
        **NO_IMAGE,
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
        **NO_IMAGE,
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
        **NO_IMAGE,
    }
    # An empty claim is reported by the record alone; no empty alt segment.
    assert [item.semantic_role for item in pictures(content)] == ["object_inventory"]

    (absent,) = pictures(pptx(pic("2")))
    assert absent.structured_value == {
        "kind": "picture",
        "shapeName": None,
        "altTitle": None,
        "altDescription": None,
        **NO_IMAGE,
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


def test_an_embedded_image_is_recorded_as_the_part_it_names() -> None:
    """The relationship, the part it resolves to, that part's declared type and
    the length the package already measured -- and no byte of its content."""
    content = pptx(
        pic("2", descr="A photograph", body=BLIP_FILL)
        + pic("3", body=blip(embed="rId3")),
        slide_rels=IMAGE_RELATION + relation("rId3", target="../media/image2.png"),
        members=IMAGE_MEMBER | {"ppt/media/image2.png": "x" * 100},
    )

    first, alt, second = pictures(content)
    assert first.structured_value == UNCLAIMED | EMBEDDED | {
        "altDescription": "A photograph"
    }
    assert (alt.semantic_role, alt.text) == ("image_alt", "A photograph")
    assert second.structured_value is not None
    # Each length is the part that picture named, so the figure is a
    # measurement of that member rather than anything the fixture shares.
    assert first.structured_value is not None
    assert (
        first.structured_value["imageBytes"],
        second.structured_value["imageBytes"],
    ) == (34, 100)
    assert second.structured_value["imagePart"] == "ppt/media/image2.png"
    # The bytes at image1.png are not a decodable image and nothing here
    # decodes them, and nothing about the image rides on a locator.
    for item in pictures(content):
        assert "rId" not in str(dict(item.source_locator))
        assert "media/image" not in str(dict(item.source_locator))


def test_a_linked_image_records_its_target_and_scheme_without_resolving_it() -> None:
    """A link is carried exactly as written, beside the scheme its own text
    names. Nothing opens, fetches, normalises or probes it."""
    content = pptx(
        pic("2", body=blip(embed=None, link="rId2")),
        slide_rels=relation(target="HTTPS://example.test/Logo.PNG", external=True),
    )

    (record,) = pictures(content)
    assert record.structured_value == UNCLAIMED | NO_IMAGE | {
        "imageLinkRelationshipId": "rId2",
        "imageLinkRelationshipType": IMAGE_TYPE,
        "imageLinkTarget": "HTTPS://example.test/Logo.PNG",
        "imageLinkScheme": "https",
        "imageLinkExternal": True,
    }

    # A link the package declared internal is recorded as internal, which is
    # not the ``None`` a picture naming no link at all carries.
    (internal,) = pictures(
        pptx(
            pic("2", body=blip(embed=None, link="rId2")),
            slide_rels=IMAGE_RELATION,
            members=IMAGE_MEMBER,
        )
    )
    assert internal.structured_value == UNCLAIMED | NO_IMAGE | {
        "imageLinkRelationshipId": "rId2",
        "imageLinkRelationshipType": IMAGE_TYPE,
        "imageLinkTarget": "ppt/media/image1.png",
        "imageLinkScheme": None,
        "imageLinkExternal": False,
    }


def test_a_picture_that_both_embeds_and_links_records_each_on_its_own_fields() -> None:
    """Both halves may be written at once, and an embedded part is never read as
    a link or the reverse."""
    content = pptx(
        pic("2", body=blip(embed="rId2", link="rId3")),
        slide_rels=IMAGE_RELATION
        + relation("rId3", target="https://example.test/logo.png", external=True),
        members=IMAGE_MEMBER,
    )

    (record,) = pictures(content)
    assert record.structured_value == UNCLAIMED | EMBEDDED | {
        "imageLinkRelationshipId": "rId3",
        "imageLinkRelationshipType": IMAGE_TYPE,
        "imageLinkTarget": "https://example.test/logo.png",
        "imageLinkScheme": "https",
        "imageLinkExternal": True,
    }


def test_a_strict_package_names_its_image_the_same_way() -> None:
    """The Strict drawing, relationships and image-type URIs qualify exactly as
    the Transitional ones do; another vocabulary qualifies nothing."""
    content = pptx(
        pic("2", body=blip(drawing=STRICT_DRAWING_NS, rel=STRICT_REL_NS)),
        slide_rels=relation(kind=STRICT_IMAGE_TYPE),
        members=IMAGE_MEMBER,
    )

    (record,) = pictures(content)
    assert record.structured_value == UNCLAIMED | EMBEDDED | {
        "imageEmbedRelationshipType": STRICT_IMAGE_TYPE
    }

    # A blip in some other vocabulary names no image, so the picture is
    # inventoried with none rather than with the one the slide relates to.
    (other,) = pictures(
        pptx(
            pic("2", body=blip(drawing="urn:other")),
            slide_rels=IMAGE_RELATION,
            members=IMAGE_MEMBER,
        )
    )
    assert other.structured_value == UNCLAIMED | NO_IMAGE


LOOSE_BLIP = f'<a:blip xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}" r:embed="rId2"/>'


def test_a_blip_off_the_displayed_image_route_names_no_image() -> None:
    """Only the picture's own direct ``blipFill`` and that fill's own direct
    ``a:blip`` declare the image shown. A blip reached anywhere else below the
    picture -- in its shape properties, in extension metadata, or nested inside
    the fill rather than written on it -- is not that image."""
    off_route = (
        '<p:pic><p:nvPicPr><p:cNvPr id="2"/><p:cNvPicPr/>'
        f"<p:extLst><p:ext>{LOOSE_BLIP}</p:ext></p:extLst></p:nvPicPr>"
        f"<p:blipFill><a:extLst xmlns:a='{DRAWING_NS}'>{LOOSE_BLIP}</a:extLst>"
        f"</p:blipFill><p:spPr>{LOOSE_BLIP}</p:spPr></p:pic>"
    )

    (record,) = pictures(
        pptx(off_route, slide_rels=IMAGE_RELATION, members=IMAGE_MEMBER)
    )
    assert record.structured_value == UNCLAIMED | NO_IMAGE


def test_two_direct_fills_or_blips_leave_the_displayed_image_ambiguous() -> None:
    """A second fill beside the picture's own, or a second blip written on one
    fill, is another declaration of the image shown and not a qualifier on the
    first -- so nothing here picks one."""
    doubled_fill = pic("2", body=BLIP_FILL + blip(embed="rId3"))
    doubled_blip = pic("2", body=f"<p:blipFill>{LOOSE_BLIP}{LOOSE_BLIP}</p:blipFill>")

    for tree in (doubled_fill, doubled_blip):
        with pytest.raises(
            OOXMLInvalid, match="declares more than one image reference"
        ):
            extract_pptx(
                pptx(
                    tree,
                    slide_rels=IMAGE_RELATION
                    + relation("rId3", target="../media/image1.png"),
                    members=IMAGE_MEMBER,
                ),
                capture_id=CAPTURE,
            )


def test_the_image_content_type_resolves_by_override_then_default() -> None:
    """An ``Override`` names the part outright and wins; a ``Default`` stands for
    every part carrying its extension that no ``Override`` names."""

    def declared(content_types: str) -> object:
        (record,) = pictures(
            pptx(
                pic("2", body=BLIP_FILL),
                slide_rels=IMAGE_RELATION,
                members=IMAGE_MEMBER,
                content_types=content_types,
            )
        )
        assert record.structured_value is not None
        return record.structured_value["imageContentType"]

    override = '<Override PartName="/ppt/media/image1.png" ContentType="image/jpeg"/>'
    assert declared(PNG_DEFAULT) == "image/png"
    assert declared(override) == "image/jpeg"
    assert declared(PNG_DEFAULT + override) == "image/jpeg"
    # An extension and a content type are matched case-insensitively, as OPC
    # declares them.
    assert declared('<Default Extension="PNG" ContentType="IMAGE/PNG"/>') == "image/png"

    # Two declarations of one extension are refused for the reason two of one
    # part are: nothing downstream reconciles them or picks one.
    with pytest.raises(OOXMLInvalid, match="declares an extension twice"):
        extract_pptx(
            pptx(pic("2"), content_types=PNG_DEFAULT + PNG_DEFAULT),
            capture_id=CAPTURE,
        )


def test_an_image_record_does_not_disturb_where_the_picture_sits() -> None:
    """The provenance of a grouped picture on a hidden slide is what it was
    before the image was inventoried at all."""
    content = pptx(
        group("5", pic("2", descr="A photograph", body=BLIP_FILL)),
        hidden=True,
        slide_rels=IMAGE_RELATION,
        members=IMAGE_MEMBER,
    )

    record, alt = pictures(content)
    assert dict(record.source_locator) == EXPECTED | {
        "slideHidden": True,
        "groupPath": ["5"],
        "zPath": [0, 0],
    }
    assert dict(alt.source_locator) == dict(record.source_locator) | {
        "altRole": "description"
    }
    assert (record.visibility, alt.visibility) == ("hidden", "hidden")
    assert record.structured_value is not None
    assert record.structured_value["imagePart"] == "ppt/media/image1.png"


def test_a_reference_that_names_no_one_image_is_refused() -> None:
    """Each of these leaves which image the picture shows, or whether it shows
    an image at all, unanswerable -- and nothing here guesses."""
    strict_embed = f' xmlns:s="{STRICT_REL_NS}" s:embed="rId2"'
    cases = (
        # An id the holding part relates to nothing names no image at all.
        (
            {"tree": pic("2", body=blip(embed="rId9")), "slide_rels": IMAGE_RELATION},
            "names a missing image relationship",
        ),
        # A relationship of another type names something that is not an image.
        (
            {
                "tree": pic("2", body=BLIP_FILL),
                "slide_rels": relation(kind=REL_NS + "/oleObject"),
            },
            "image relationship of another type",
        ),
        # Nor does a type reach an image by ending the same way as one.
        (
            {
                "tree": pic("2", body=BLIP_FILL),
                "slide_rels": relation(
                    kind="http://example.test/officeDocument/2006/relationships/image"
                ),
            },
            "image relationship of another type",
        ),
        # ``r:embed`` names a part of the package, so an external relationship
        # is not an embed and is not quietly recorded as a link.
        (
            {
                "tree": pic("2", body=BLIP_FILL),
                "slide_rels": relation(
                    target="https://example.test/logo.png", external=True
                ),
            },
            "embedded image relationship is external",
        ),
        # A picture shows one image, so a second blip below it is ambiguous.
        (
            {
                "tree": pic("2", body=BLIP_FILL + blip()),
                "slide_rels": IMAGE_RELATION,
            },
            "declares more than one image reference",
        ),
        # So is one attribute written in both relationships namespaces at once.
        (
            {
                "tree": pic("2", body=blip(extra=strict_embed)),
                "slide_rels": IMAGE_RELATION,
            },
            "names more than one image relationship",
        ),
        # An embedded part the package declares something other than an image,
        # or declares nothing at all, is not inventoried as an image.
        (
            {
                "tree": pic("2", body=BLIP_FILL),
                "slide_rels": IMAGE_RELATION,
                "content_types": '<Default Extension="png" ContentType="text/plain"/>',
            },
            "not declared an image",
        ),
        (
            {
                "tree": pic("2", body=BLIP_FILL),
                "slide_rels": IMAGE_RELATION,
                "content_types": "",
            },
            "not declared an image",
        ),
    )

    for arguments, message in cases:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_pptx(pptx(members=IMAGE_MEMBER, **arguments), capture_id=CAPTURE)


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
