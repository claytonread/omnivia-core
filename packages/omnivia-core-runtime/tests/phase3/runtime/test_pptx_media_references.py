"""PPTX picture media: the audio and video references pictures declare.

A picture may reference one piece of media from its own non-visual application
properties. That reference is inventoried where the picture sits, in its own
``object_inventory`` record beside the picture's, told apart by the
``mediaRole`` its locator carries and by the ``kind`` in its value.

It is inventory and only inventory. Nothing here fetches, plays, decodes,
launches or inspects media: an internal part contributes its safe name, the
type the package declares for it and the byte length the package already
measured, and an external target is carried exactly as written beside the
scheme its own text names.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The PresentationML main namespaces the picture's own non-visual holders are
# qualified in, the DrawingML ones the schema writes the media elements in, and
# the relationships namespaces their ``r:link`` is. Each has a Transitional and
# a Strict spelling, and a package written to either profile reads the same.
PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_PML = "http://purl.oclc.org/ooxml/presentationml/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
AUDIO_TYPE = REL_NS + "/audio"
VIDEO_TYPE = REL_NS + "/video"
STRICT_AUDIO_TYPE = STRICT_REL_NS + "/audio"
STRICT_VIDEO_TYPE = STRICT_REL_NS + "/video"
IMAGE_TYPE = REL_NS + "/image"
# Bytes at the media targets that are not playable, decodable audio or video in
# any format. The proof that this slice never reaches the media is that these
# are never opened: only their length, which the package measured while
# validating the member, is ever reported.
VIDEO_BYTES = "not a video anything here plays"
AUDIO_BYTES = "not audio either"
MEDIA_MEMBERS = {
    "ppt/media/media1.mp4": VIDEO_BYTES,
    "ppt/media/media2.m4a": AUDIO_BYTES,
}
MEDIA_DEFAULTS = (
    '<Default Extension="mp4" ContentType="video/mp4"/>'
    '<Default Extension="m4a" ContentType="audio/mp4"/>'
)


def relation(
    relation_id: str = "rId2",
    *,
    target: str = "../media/media1.mp4",
    kind: str = VIDEO_TYPE,
    external: bool = False,
) -> str:
    """One ``Relationship`` of the slide's own relationship part."""
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{relation_id}" Target="{target}" Type="{kind}"{mode}/>'


VIDEO_RELATION = relation()
AUDIO_RELATION = relation("rId3", target="../media/media2.m4a", kind=AUDIO_TYPE)


def reference(
    element: str = "videoFile",
    *,
    link: str | None = "rId2",
    embed: str | None = None,
    namespace: str = DRAWING_NS,
    rel: str = REL_NS,
    prefix: str = "a",
    extra: str = "",
) -> str:
    """One media element, bound to ``namespace`` whatever prefix spells it.

    The prefix is immaterial -- a name is the namespace it is bound to and its
    local part -- so the DrawingML element qualifies whether a file spells it
    ``a:videoFile`` or binds some other prefix to the same namespace, and a
    PresentationML-bound ``p:videoFile`` is a different name entirely.
    """
    named = "".join(
        f' r:{attribute}="{value}"'
        for attribute, value in (("link", link), ("embed", embed))
        if value is not None
    )
    return (
        f"<{prefix}:{element} xmlns:{prefix}=\"{namespace}\" "
        f'xmlns:r="{rel}"{named}{extra}/>'
    )


VIDEO_REFERENCE = reference()
AUDIO_REFERENCE = reference("audioFile", link="rId3")


def pic(
    shape_id: str = "2",
    *,
    media: str = "",
    pml: str = PML,
    holder: str = "",
    extra: str = "",
) -> str:
    """One ``p:pic`` whose ``p:nvPr`` holds ``media``.

    ``holder`` goes beside that ``p:nvPr`` inside the same ``p:nvPicPr``, and
    ``extra`` goes beside the non-visual holder itself, so a lookalike can be
    placed at either depth without reaching the route the reference sits on.
    """
    return (
        f'<p:pic xmlns:p="{pml}"><p:nvPicPr><p:cNvPr id="{shape_id}"/>'
        f"<p:cNvPicPr/><p:nvPr>{media}</p:nvPr>{holder}</p:nvPicPr>"
        f"{extra}<p:spPr/></p:pic>"
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
    slide_rels: str = VIDEO_RELATION,
    members: dict[str, str] | None = None,
    content_types: str = MEDIA_DEFAULTS,
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
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{PML}"><p:cSld>'
        f"<p:spTree>{tree}</p:spTree></p:cSld></p:sld>",
        "ppt/slides/_rels/slide1.xml.rels": (
            f"<Relationships>{slide_rels}</Relationships>"
        ),
    }
    parts.update(MEDIA_MEMBERS if members is None else members)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(content: bytes) -> list[ExtractedSegment]:
    """Every media record the picture route produced, in emission order."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "pictureMedia"
    ]


def inventory(content: bytes) -> list[tuple[str, object]]:
    """The (kind, mediaRole) of every object record, in source order."""
    return [
        (
            str((item.structured_value or {}).get("kind")),
            item.source_locator.get("mediaRole"),
        )
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


# The provenance the default fixture's media record carries, which is the
# picture's own locator with the media role added to it.
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
    "readingOrder": 0,
    "readingPath": [0],
    "geometry": None,
    "placeholderType": None,
    "placeholderIndex": None,
    "mediaRole": "video",
}
# What the default fixture's record says about the video part it references.
VIDEO = {
    "kind": "pictureMedia",
    "media": "video",
    "mediaRelationshipId": "rId2",
    "mediaRelationshipType": VIDEO_TYPE,
    "mediaTarget": "ppt/media/media1.mp4",
    "mediaExternal": False,
    "mediaScheme": None,
    "mediaPart": "ppt/media/media1.mp4",
    "mediaContentType": "video/mp4",
    "mediaBytes": len(VIDEO_BYTES),
}
AUDIO = VIDEO | {
    "media": "audio",
    "mediaRelationshipId": "rId3",
    "mediaRelationshipType": AUDIO_TYPE,
    "mediaTarget": "ppt/media/media2.m4a",
    "mediaPart": "ppt/media/media2.m4a",
    "mediaContentType": "audio/mp4",
    "mediaBytes": len(AUDIO_BYTES),
}


def test_a_video_reference_is_inventoried_beside_the_picture() -> None:
    """One further record at the picture's own place, told apart by its role."""
    content = pptx(pic(media=VIDEO_REFERENCE))

    assert inventory(content) == [("picture", None), ("pictureMedia", "video")]
    record = records(content)[0]
    assert (record.extraction_method, record.visibility) == ("inventory", "visible")
    assert dict(record.source_locator) == EXPECTED
    assert record.structured_value == VIDEO


def test_an_audio_reference_is_told_apart_from_a_video_one() -> None:
    """The element names which of the two it is, and the record keeps them
    distinct in the role, the relationship type and the part."""
    content = pptx(
        pic("2", media=AUDIO_REFERENCE) + pic("3", media=VIDEO_REFERENCE),
        slide_rels=AUDIO_RELATION + VIDEO_RELATION,
    )

    first, second = records(content)
    assert first.source_locator["mediaRole"] == "audio"
    assert first.structured_value == AUDIO
    assert second.source_locator["mediaRole"] == "video"
    assert second.structured_value == VIDEO


def test_a_picture_referencing_no_media_gets_no_media_record() -> None:
    """The picture is still inventoried; nothing invents a reference for it."""
    content = pptx(pic())

    assert inventory(content) == [("picture", None)]


def test_the_strict_profile_is_read_the_same_way() -> None:
    """Strict DrawingML, Strict relationships and the Strict relationship type
    all qualify, and resolve to the record the Transitional pair does."""
    content = pptx(
        pic(
            pml=STRICT_PML,
            media=reference(
                namespace=STRICT_DRAWING_NS, rel=STRICT_REL_NS, prefix="s"
            ),
        ),
        slide_rels=relation(kind=STRICT_VIDEO_TYPE),
    )

    assert records(content)[0].structured_value == VIDEO | {
        "mediaRelationshipType": STRICT_VIDEO_TYPE
    }


def test_a_grouped_reference_on_a_hidden_slide_keeps_the_pictures_provenance()\
        -> None:
    """The record sits where the picture does -- group path, per-level index,
    z-order and the slide's own visibility -- and never anywhere else."""
    content = pptx(
        pic("2") + group("5", pic("8", media=VIDEO_REFERENCE)),
        hidden=True,
    )

    record = records(content)[0]
    assert record.visibility == "hidden"
    assert dict(record.source_locator) == EXPECTED | {
        "slideHidden": True,
        "shapeId": "8",
        "groupPath": ["5"],
        "zOrder": 0,
        "zPath": [1, 0],
        "readingOrder": 0,
        "readingPath": [1, 0],
    }


def test_malformed_media_bytes_are_measured_and_never_opened() -> None:
    """The member holds bytes no player would accept. Only the length the
    package already measured is reported, so nothing decodes or plays them."""
    content = pptx(
        pic(media=VIDEO_REFERENCE),
        members={"ppt/media/media1.mp4": "\x00\x00\x00\x00 not a container at all"},
    )

    assert records(content)[0].structured_value == VIDEO | {
        "mediaBytes": len("\x00\x00\x00\x00 not a container at all")
    }


def test_an_external_link_carries_its_target_and_scheme_verbatim() -> None:
    """The reference out of the package is inventory: nothing fetches, probes
    or normalises it, and no part name or byte length is invented for it."""
    content = pptx(
        pic(media=VIDEO_REFERENCE),
        slide_rels=relation(target="HTTPS://cdn.example/Clip.MP4", external=True),
    )

    assert records(content)[0].structured_value == VIDEO | {
        "mediaTarget": "HTTPS://cdn.example/Clip.MP4",
        "mediaExternal": True,
        "mediaScheme": "https",
        "mediaPart": None,
        "mediaContentType": None,
        "mediaBytes": None,
    }


def test_an_external_target_naming_no_scheme_reports_none() -> None:
    content = pptx(
        pic(media=VIDEO_REFERENCE),
        slide_rels=relation(target="clips/intro.mp4", external=True),
    )

    assert records(content)[0].structured_value["mediaScheme"] is None


@pytest.mark.parametrize(
    "markup",
    [
        # Deeper than the route: a reference inside a child of ``p:nvPr``.
        pic(media=f"<p:custDataLst>{VIDEO_REFERENCE}</p:custDataLst>"),
        # Beside ``p:nvPr`` rather than inside it, and outside the non-visual
        # holder altogether.
        pic(holder=VIDEO_REFERENCE),
        pic(extra=VIDEO_REFERENCE),
        # A group above the picture is not the picture.
        group("5", VIDEO_REFERENCE + pic("8")),
        # A local name shared with another vocabulary names no media, and the
        # holders being PresentationML does not make the element so: the
        # PresentationML-spelled lookalike is as foreign as any other.
        pic(media=reference(namespace="urn:someone-elses", prefix="x")),
        pic(media=reference(namespace=PML, prefix="p")),
        pic(media=reference(namespace=STRICT_PML, prefix="p")),
        pic(media=reference("audioFile", link="rId3", namespace=PML, prefix="p")),
        # Nor does one hung on a non-visual holder from another vocabulary.
        pic(media="", holder=f'<p:nvPr xmlns:p="urn:not-pml">{VIDEO_REFERENCE}</p:nvPr>'),
        # Other members of the media group are other declarations entirely.
        pic(media=reference("wavAudioFile")),
        pic(media=reference("audioCd")),
        pic(media=reference("quickTimeFile")),
    ],
)
def test_a_lookalike_declares_no_media(markup: str) -> None:
    assert [kind for kind, _role in inventory(pptx(markup))] == ["picture"]


@pytest.mark.parametrize(
    "media",
    [
        # Two of a kind, and one of each: a picture references one piece of
        # media or none, so nothing here chooses between two declarations.
        VIDEO_REFERENCE * 2,
        VIDEO_REFERENCE + AUDIO_REFERENCE,
        # The same element written in both namespaces that qualify it.
        VIDEO_REFERENCE + reference(namespace=STRICT_DRAWING_NS, prefix="s"),
    ],
)
def test_two_declarations_leave_the_reference_ambiguous(media: str) -> None:
    with pytest.raises(OOXMLInvalid, match="more than one media reference"):
        extract_pptx(
            pptx(pic(media=media), slide_rels=VIDEO_RELATION + AUDIO_RELATION),
            capture_id=CAPTURE,
        )


def test_one_element_naming_two_relationships_is_ambiguous() -> None:
    """``r:link`` in the Transitional and the Strict namespace at once names
    two relationships, and nothing here picks one."""
    markup = reference(extra=f' xmlns:s="{STRICT_REL_NS}" s:link="rId3"')

    with pytest.raises(OOXMLInvalid, match="more than one media relationship"):
        extract_pptx(
            pptx(pic(media=markup), slide_rels=VIDEO_RELATION + AUDIO_RELATION),
            capture_id=CAPTURE,
        )


def test_a_reference_naming_no_relationship_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="names no relationship"):
        extract_pptx(pptx(pic(media=reference(link=None))), capture_id=CAPTURE)


@pytest.mark.parametrize("link", ["rId2", None])
def test_an_embed_beside_the_link_contradicts_it(link: str | None) -> None:
    """``r:embed`` is not the attribute the schema puts the reference on, so it
    is a second, contradicting claim rather than a qualifier on the first."""
    with pytest.raises(OOXMLInvalid, match="also names an embedded part"):
        extract_pptx(
            pptx(pic(media=reference(link=link, embed="rId2"))), capture_id=CAPTURE
        )


def test_a_relationship_the_part_does_not_hold_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="missing media relationship"):
        extract_pptx(pptx(pic(media=reference(link="rId9"))), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("element", "kind"),
    [
        # The relationship has to agree with the kind the element declares.
        ("videoFile", AUDIO_TYPE),
        ("audioFile", VIDEO_TYPE),
        # Another type entirely reaches no media at all.
        ("videoFile", IMAGE_TYPE),
        # A type that merely ends the same way is not one OOXML defines.
        ("videoFile", "http://example.invalid/relationships/video"),
    ],
)
def test_a_relationship_of_another_type_names_no_media(
    element: str, kind: str
) -> None:
    with pytest.raises(OOXMLInvalid, match="media relationship of another type"):
        extract_pptx(
            pptx(pic(media=reference(element)), slide_rels=relation(kind=kind)),
            capture_id=CAPTURE,
        )


def test_an_internal_target_is_a_member_before_this_slice_sees_it() -> None:
    """Containment and existence are settled while the package is validated, so
    a reference can never reach a part the archive does not hold."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(
            pptx(
                pic(media=VIDEO_REFERENCE),
                slide_rels=relation(target="../media/absent.mp4"),
            ),
            capture_id=CAPTURE,
        )
