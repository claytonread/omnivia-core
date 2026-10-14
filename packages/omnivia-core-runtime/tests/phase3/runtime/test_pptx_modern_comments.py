"""Modern ("threaded") PPTX comments: qualification, provenance and refusals.

Only the modern form is read here -- the Microsoft comment and author parts a
slide reaches through the 2018/10 relationships, having first declared the
comment list in its own ``p:extLst``. The classic per-slide ``p:cmLst`` form
has its own readers and is covered in ``test_pptx_comments.py``; the two meet
only in the test that shows a slide carrying both reports both.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import _validated_segments
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The one namespace both modern parts are written in, and the DrawingML
# namespace a comment body's rich text is written in.
MODERN_NS = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
# The one relationship-type URI defined for each of the two parts.
COMMENTS_TYPE = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
AUTHORS_TYPE = "http://schemas.microsoft.com/office/2018/10/relationships/authors"
# The content types the two parts have to be declared with.
COMMENTS_CT = "application/vnd.ms-powerpoint.comments+xml"
AUTHORS_CT = "application/vnd.ms-powerpoint.authors+xml"
# The classic types, which reach none of the parts below.
CLASSIC_COMMENTS_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
CLASSIC_AUTHORS_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors"
)
CLASSIC_COMMENTS_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.comments+xml"
)
CLASSIC_AUTHORS_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.commentAuthors+xml"
)
CLASSIC_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
# The two OOXML relationships namespaces an ``r:id`` may be written in, and the
# slide extension PowerPoint writes to name its modern comment list. The
# extension is the only route to the part, so every fixture below either
# advertises it or has no modern comments to read.
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
EXTENSION = "{6950BFC3-D8DA-4A85-94F7-54DA5524770B}"
COMMENT_PART = "ppt/comments/modernComment1.xml"
AUTHOR_PART = "ppt/authors.xml"
AUTHOR_ID = "{B7D8A0A4-8E1C-4F2B-9C3D-1A2B3C4D5E6F}"
OTHER_AUTHOR_ID = "{C1D2E3F4-5566-7788-99AA-BBCCDDEEFF00}"
COMMENT_ID = "{11112222-3333-4444-5555-666677778888}"
REPLY_ID = "{99990000-AAAA-BBBB-CCCC-DDDDEEEEFFFF}"
SECOND_ID = "{0A0B0C0D-1E1F-2A2B-3C3D-4E4F5A5B5C5D}"
COMMENT_RELATION = (
    f'<Relationship Id="rId2" Type="{COMMENTS_TYPE}"'
    ' Target="../comments/modernComment1.xml"/>'
)
AUTHOR_RELATION = (
    f'<Relationship Id="rIdA" Type="{AUTHORS_TYPE}" Target="authors.xml"/>'
)
# A slide relationship under a type that qualifies as nothing read here.
LAYOUT_RELATION = (
    f'<Relationship Id="rId2" Type="{REL_NS}/slideLayout"'
    ' Target="../slideLayouts/slideLayout1.xml"/>'
)
AUTHOR_OVERRIDE = f'<Override PartName="/{AUTHOR_PART}" ContentType="{AUTHORS_CT}"/>'


def ext(
    ids: str = f' xmlns:r="{REL_NS}" r:id="rId2"',
    *,
    uri: str = EXTENSION,
    rels: int = 1,
    namespace: str = MODERN_NS,
    element_namespace: str | None = None,
) -> str:
    """One ``p:ext`` naming a modern comment list.

    ``ids`` is the attribute markup of the ``commentRel`` inside it, carrying
    its own namespace declaration so a test can write the id in another one;
    ``rels`` is how many ``commentRel`` children the extension holds.
    ``element_namespace`` writes the ``ext`` itself in that namespace instead
    of the slide's own PresentationML one.
    """
    opening, closing = (
        (f'<p:ext uri="{uri}">', "</p:ext>")
        if element_namespace is None
        else (f'<q:ext xmlns:q="{element_namespace}" uri="{uri}">', "</q:ext>")
    )
    return (
        opening
        + f'<p188:commentRel xmlns:p188="{namespace}"{ids}/>' * rels
        + closing
    )


def text_body(*paragraphs: str) -> str:
    """One ``p188:txBody``; each argument is a paragraph, ``|`` splitting runs."""
    written = "".join(
        "<a:p>"
        + "".join(f"<a:r><a:t>{piece}</a:t></a:r>" for piece in paragraph.split("|"))
        + "</a:p>"
        for paragraph in paragraphs
    )
    return f"<p188:txBody><a:bodyPr/><a:lstStyle/>{written}</p188:txBody>"


def comment(
    *paragraphs: str,
    tag: str = "cm",
    comment_id: str | None = COMMENT_ID,
    author_id: str | None = AUTHOR_ID,
    claims: str = ' created="2026-03-04T05:06:07.000" status="active"',
    pos: str | None = '<p188:pos x="1524000" y="-762000"/>',
    anchor: str = "",
    body: str | None = None,
    replies: str | None = None,
    extra: str = "",
) -> str:
    """One ``p188:cm`` or ``p188:reply``; ``body=""`` writes no body at all."""
    attributes = (
        "".join(
            f' {key}="{value}"'
            for key, value in (("id", comment_id), ("authorId", author_id))
            if value is not None
        )
        + claims
    )
    written = text_body(*(paragraphs or ("Tighten this claim",)))
    inner = (pos or "") + anchor + (written if body is None else body)
    if replies is not None:
        inner += f"<p188:replyLst>{replies}</p188:replyLst>"
    return f"<p188:{tag}{attributes}>{inner}{extra}</p188:{tag}>"


def reply(
    *paragraphs: str,
    comment_id: str = REPLY_ID,
    author_id: str = OTHER_AUTHOR_ID,
    claims: str = ' created="2026-03-05T06:07:08.000"',
    **extra: str,
) -> str:
    """One ``p188:reply``: a comment of the same shape, declaring no thread."""
    return comment(
        *paragraphs,
        tag="reply",
        comment_id=comment_id,
        author_id=author_id,
        claims=claims,
        pos=None,
        **extra,
    )


def author(
    author_id: str = AUTHOR_ID,
    *,
    name: str | None = "Dana Lee",
    initials: str | None = "DL",
    user: str | None = "dana@example.invalid",
    provider: str | None = "AD",
    extra: str = "",
) -> str:
    """One ``p188:author`` record; a None attribute is left out entirely."""
    attributes = "".join(
        f' {key}="{value}"'
        for key, value in (
            ("id", author_id),
            ("name", name),
            ("initials", initials),
            ("userId", user),
            ("providerId", provider),
        )
        if value is not None
    )
    return f"<p188:author{attributes}>{extra}</p188:author>"


def listed(*entries: str, root: str, namespace: str = MODERN_NS) -> str:
    """Wrap entries in one of the two modern list roots."""
    return (
        f'<p188:{root} xmlns:p188="{namespace}" xmlns:a="{DRAWING_NS}">'
        f'{"".join(entries)}</p188:{root}>'
    )


COMMENT_LIST = listed(comment(), root="cmLst")
AUTHOR_LIST = listed(author(), author(OTHER_AUTHOR_ID, name="Ola Ruiz", initials="OR"), root="authorLst")
# The provenance the default fixture's single comment carries.
EXPECTED: dict[str, object] = {
    "captureId": CAPTURE,
    "part": COMMENT_PART,
    "relationshipId": "rId2",
    "extensionRelationshipId": "rId2",
    "commentForm": "modern",
    "commentId": COMMENT_ID,
    "parentCommentId": None,
    "replyIndex": None,
    "authorId": AUTHOR_ID,
    "authorResolved": True,
    "authorPart": AUTHOR_PART,
    "authorName": "Dana Lee",
    "authorInitials": "DL",
    "authorUserId": "dana@example.invalid",
    "authorProviderId": "AD",
    "authorAuthenticated": False,
    "created": "2026-03-04T05:06:07.000",
    "status": "active",
    "title": None,
    "assignedTo": None,
    "startDate": None,
    "dueDate": None,
    "complete": None,
    "anchorKind": None,
    "x": 1524000,
    "y": -762000,
    "slidePart": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
}


def slide_xml(index: int, extension: str = "", *, nested: str = "") -> str:
    """One slide part, written in the real PresentationML namespace.

    ``extension`` is the markup of the slide's own ``p:extLst``, the one place
    a modern comment list is advertised; ``nested`` goes in an extension list
    hung off the shape instead, which no schema places there and which is not
    the slide's own advertisement.
    """
    return (
        f'<p:sld xmlns:p="{CLASSIC_NS}" xmlns:a="urn:a">'
        "<p:cSld><p:spTree>"
        '<p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:spPr/><p:txBody>'
        f"<a:p><a:r><a:t>slide {index} text</a:t></a:r></a:p>"
        "</p:txBody>"
        + (f"<p:extLst>{nested}</p:extLst>" if nested else "")
        + "</p:sp></p:spTree></p:cSld>"
        + (f"<p:extLst>{extension}</p:extLst>" if extension else "")
        + "</p:sld>"
    )


def pptx(
    *,
    comments: dict[str, str] | None = None,
    slide_rels: tuple[str | None, ...] = (COMMENT_RELATION,),
    presentation_rels: str = AUTHOR_RELATION,
    authors: str | None = AUTHOR_LIST,
    overrides: str | None = None,
    extension: str | tuple[str, ...] = ext(),
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a presentation whose slides carry the given comment relationships.

    ``comments`` maps part name to the list each holds; a part left out of it is
    simply not in the package, which is how a phantom filename-only part is
    built. ``overrides`` defaults to declaring exactly the parts that are there,
    and ``extension`` is the markup of each slide's ``p:extLst`` -- the route to
    the comment list -- either one string for every slide or one per slide.
    ``""`` advertises nothing.
    """
    parts = {COMMENT_PART: COMMENT_LIST} if comments is None else dict(comments)
    if overrides is None:
        overrides = "".join(
            f'<Override PartName="/{name}" ContentType="{COMMENTS_CT}"/>'
            for name in parts
        ) + (AUTHOR_OVERRIDE if authors is not None else "")
    slides = "".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index + 1}"/>'
        for index in range(len(slide_rels))
    )
    relations = "".join(
        f'<Relationship Id="rId{index + 1}" Target="slides/slide{index + 1}.xml"/>'
        for index in range(len(slide_rels))
    )
    if authors is not None:
        parts[AUTHOR_PART] = authors
    parts["[Content_Types].xml"] = f"<Types>{overrides}</Types>"
    parts["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="urn:p" xmlns:r="{REL_NS}">'
        f"<p:sldIdLst>{slides}</p:sldIdLst></p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        f"<Relationships>{relations}{presentation_rels}</Relationships>"
    )
    advertised = (
        extension
        if isinstance(extension, tuple)
        else (extension,) * len(slide_rels)
    )
    for index, slide in enumerate(slide_rels, start=1):
        parts[f"ppt/slides/slide{index}.xml"] = slide_xml(
            index, advertised[index - 1]
        )
        if slide is not None:
            parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = (
                f"<Relationships>{slide}</Relationships>"
            )
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def comments(content: bytes) -> list[tuple[str | None, dict[str, object]]]:
    return [
        (item.text, dict(item.source_locator))
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ]


def texts(content: bytes) -> list[str | None]:
    return [
        item.text
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "body"
    ]


def test_a_modern_comment_carries_its_whole_source_claim() -> None:
    """Text, identity, resolved author, timestamp, status and anchor come through."""
    content = pptx()

    ((text, locator),) = comments(content)
    assert text == "Tighten this claim"
    assert locator == EXPECTED
    assert [
        item.visibility
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ] == ["annotation"]
    # The slide's own text is still read, unchanged by the comments being there.
    assert texts(content) == ["slide 1 text"]


def test_a_thread_reports_each_reply_in_its_own_right() -> None:
    """A reply is a comment citing the one it answers and its place in the thread."""
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment(replies=reply("agreed") + reply("done", comment_id=SECOND_ID)),
                root="cmLst",
            )
        }
    )

    first, second, third = comments(content)
    # The comment itself is unchanged by holding a thread: it cites no parent.
    assert (first[0], first[1]) == ("Tighten this claim", EXPECTED)
    assert (second[0], third[0]) == ("agreed", "done")
    for index, (_text, locator) in enumerate((second, third)):
        assert locator["commentForm"] == "modern"
        assert locator["parentCommentId"] == COMMENT_ID
        assert locator["replyIndex"] == index
        # A reply carries its own identity, author and timestamp, not its
        # parent's, and declares no anchor of its own.
        assert locator["authorId"] == OTHER_AUTHOR_ID
        assert (locator["authorName"], locator["authorResolved"]) == ("Ola Ruiz", True)
        assert locator["created"] == "2026-03-05T06:07:08.000"
        assert (locator["x"], locator["y"], locator["anchorKind"]) == (None, None, None)
    assert (second[1]["commentId"], third[1]["commentId"]) == (REPLY_ID, SECOND_ID)
    # Every key a comment carries, a reply carries too.
    assert second[1].keys() == EXPECTED.keys()


def test_a_reply_holding_replies_of_its_own_is_refused() -> None:
    """A thread is one level deep, so a nested thread is not a shape read here."""
    nested = listed(
        comment(replies=reply("agreed", replies=reply("deeper", comment_id=SECOND_ID))),
        root="cmLst",
    )

    with pytest.raises(OOXMLInvalid, match="reply holds replies of its own"):
        extract_pptx(pptx(comments={COMMENT_PART: nested}), capture_id=CAPTURE)


def test_task_claims_are_recorded_where_present_and_never_interpreted() -> None:
    """Title, assignment, dates and completion are the claims the file wrote."""
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment(
                    claims=' created="2026-03-04T05:06:07.000" status="resolved"'
                    ' title="Tighten the claim" assignedTo="' + OTHER_AUTHOR_ID + '"'
                    ' startDate="2026-03-01T00:00:00.000"'
                    ' dueDate="2026-03-09T00:00:00.000" complete="50000"'
                ),
                root="cmLst",
            )
        }
    )

    ((_text, locator),) = comments(content)
    assert {key: locator[key] for key in ("status", "title", "complete")} == {
        "status": "resolved",
        "title": "Tighten the claim",
        "complete": "50000",
    }
    assert locator["assignedTo"] == OTHER_AUTHOR_ID
    assert (locator["startDate"], locator["dueDate"]) == (
        "2026-03-01T00:00:00.000",
        "2026-03-09T00:00:00.000",
    )


def test_each_anchor_kind_is_recorded_and_its_monikers_are_not_read() -> None:
    """The kind places the comment; nothing inside the anchor is resolved."""
    monikers = (
        '<p188:sldMkLst><pc:docMk xmlns:pc="urn:pc"/>'
        '<pc:sldMk xmlns:pc="urn:pc" cId="1" sldId="256"/></p188:sldMkLst>'
    )
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment(pos=None, anchor=monikers), root="cmLst"
            )
        }
    )

    ((_text, locator),) = comments(content)
    assert locator["anchorKind"] == "sldMkLst"
    assert (locator["x"], locator["y"]) == (None, None)

    for kind in ("unknownAnchor", "deMkLst", "txMkLst", "sldMkLst", "docMkLst"):
        anchored = pptx(
            comments={
                COMMENT_PART: listed(
                    comment(anchor=f"<p188:{kind}/>"), root="cmLst"
                )
            }
        )
        ((_text, where),) = comments(anchored)
        assert where["anchorKind"] == kind, kind
        # The ``pos`` anchor is independent of the moniker kind.
        assert (where["x"], where["y"]) == (1524000, -762000), kind


def test_an_unresolved_author_is_preserved_rather_than_invented() -> None:
    """A comment citing an undefined id keeps the id and says so.

    Unlike a classic comment, which indexes into a running table and is refused
    when the entry is not there, a modern comment cites an id directly: the
    citation is the file's own claim and survives the table not defining it.
    """
    # A table that is there, defining another id.
    other = pptx(authors=listed(author(OTHER_AUTHOR_ID), root="authorLst"))
    ((text, locator),) = comments(other)
    assert text == "Tighten this claim"
    assert locator["authorId"] == AUTHOR_ID
    assert locator["authorResolved"] is False
    assert {
        key: locator[key]
        for key in ("authorPart", "authorName", "authorInitials", "authorUserId")
    } == dict.fromkeys(
        ("authorPart", "authorName", "authorInitials", "authorUserId"), None
    )
    assert locator["authorAuthenticated"] is False
    # Every key a resolved comment carries is still there.
    assert locator.keys() == EXPECTED.keys()

    # No author table at all: the presentation relates to none.
    ((_text, bare),) = comments(pptx(authors=None, presentation_rels=""))
    assert (bare["authorId"], bare["authorResolved"]) == (AUTHOR_ID, False)


def test_an_author_id_differing_only_in_hex_case_resolves() -> None:
    """Only the hex digits of an ``ST_Guid`` vary in case, and ids stay verbatim."""
    folded = AUTHOR_ID.lower()
    content = pptx(
        comments={
            COMMENT_PART: listed(comment(author_id=folded), root="cmLst")
        }
    )

    ((_text, locator),) = comments(content)
    assert (locator["authorId"], locator["authorResolved"]) == (folded, True)
    assert locator["authorName"] == "Dana Lee"


def test_author_claims_are_clipped_and_never_authenticated() -> None:
    """Absent fields stay absent; present ones are clipped claims, not identity."""
    bare = pptx(
        authors=listed(
            author(name=None, initials=None, user=None, provider=None),
            root="authorLst",
        )
    )

    ((_text, locator),) = comments(bare)
    assert locator["authorResolved"] is True
    assert (
        locator["authorName"],
        locator["authorInitials"],
        locator["authorUserId"],
        locator["authorProviderId"],
        locator["authorAuthenticated"],
    ) == (None, None, None, None, False)

    long = pptx(
        comments={
            COMMENT_PART: listed(comment(claims=' created="' + "Z" * 400 + '"'), root="cmLst")
        },
        authors=listed(author(name="N" * 400), root="authorLst"),
    )
    ((_text, clipped),) = comments(long)
    assert clipped["authorName"] == "N" * 256
    assert clipped["created"] == "Z" * 256


def test_comments_are_emitted_in_source_order_across_slides() -> None:
    """Order is the order written: each comment, then its own replies."""
    second = listed(
        comment("second", comment_id=SECOND_ID, replies=reply("answer")),
        comment("first", comment_id=COMMENT_ID, pos=None),
        root="cmLst",
    )
    content = pptx(
        comments={
            COMMENT_PART: COMMENT_LIST,
            "ppt/comments/modernComment2.xml": second,
        },
        slide_rels=(
            COMMENT_RELATION,
            COMMENT_RELATION.replace("modernComment1", "modernComment2").replace(
                "rId2", "rId3"
            ),
        ),
        # Each slide names its own relationship, and slide 2 names another id.
        extension=(ext(), ext(f' xmlns:r="{REL_NS}" r:id="rId3"')),
    )

    assert [
        (text, locator["commentId"], locator["replyIndex"], locator["slide"])
        for text, locator in comments(content)
    ] == [
        ("Tighten this claim", COMMENT_ID, None, 1),
        ("second", SECOND_ID, None, 2),
        ("answer", REPLY_ID, 0, 2),
        ("first", COMMENT_ID, None, 2),
    ]
    assert texts(content) == ["slide 1 text", "slide 2 text"]


def test_paragraph_breaks_survive_and_runs_inside_one_are_joined() -> None:
    """A body is rich text, so it is read as the paragraphs and runs it holds."""
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment("first| and more", "second"), root="cmLst"
            )
        }
    )

    assert [text for text, _ in comments(content)] == ["first and more\nsecond"]


def test_both_comment_forms_on_one_slide_are_reported() -> None:
    """Neither form stands in for the other, and the locator says which is which."""
    classic_part = "ppt/comments/comment1.xml"
    classic_authors = "ppt/commentAuthors.xml"
    content = pptx(
        slide_rels=(
            COMMENT_RELATION
            + f'<Relationship Id="rId4" Type="{CLASSIC_COMMENTS_TYPE}"'
            ' Target="../comments/comment1.xml"/>',
        ),
        presentation_rels=AUTHOR_RELATION
        + f'<Relationship Id="rIdC" Type="{CLASSIC_AUTHORS_TYPE}"'
        ' Target="commentAuthors.xml"/>',
        overrides=f'<Override PartName="/{COMMENT_PART}" ContentType="{COMMENTS_CT}"/>'
        + AUTHOR_OVERRIDE
        + f'<Override PartName="/{classic_part}" ContentType="{CLASSIC_COMMENTS_CT}"/>'
        + f'<Override PartName="/{classic_authors}"'
        f' ContentType="{CLASSIC_AUTHORS_CT}"/>',
        members={
            classic_part: f'<p:cmLst xmlns:p="{CLASSIC_NS}">'
            '<p:cm authorId="0" idx="1"><p:text>classic claim</p:text></p:cm>'
            "</p:cmLst>",
            classic_authors: f'<p:cmAuthorLst xmlns:p="{CLASSIC_NS}">'
            '<p:cmAuthor id="0" name="Dana Lee"/></p:cmAuthorLst>',
        },
    )

    assert [
        (text, locator["commentForm"], locator["commentId"])
        for text, locator in comments(content)
    ] == [
        ("classic claim", "classic", "0:1"),
        ("Tighten this claim", "modern", COMMENT_ID),
    ]


def test_the_slide_extension_is_the_route_to_the_part() -> None:
    """A slide has modern comments only where its own XML says which list.

    The extension names the relationship the part is reached through, so a
    slide that advertises nothing has no modern comments however it relates --
    its own text and every other form are unaffected -- and the id may be
    written in either relationships namespace.
    """
    bare = pptx(extension="")
    assert comments(bare) == []
    assert texts(bare) == ["slide 1 text"]

    for namespace in (REL_NS, STRICT_REL_NS):
        content = pptx(
            extension=ext(f' xmlns:q="{namespace}" q:id="rId2"'),
        )
        ((text, locator),) = comments(content)
        assert (text, locator) == ("Tighten this claim", EXPECTED), namespace

    # The URI is a GUID, so only its hex digits vary in case.
    folded = pptx(extension=ext(uri=EXTENSION.lower()))
    assert [text for text, _ in comments(folded)] == ["Tighten this claim"]


def test_only_the_slide_s_own_top_level_extension_is_that_route() -> None:
    """The extension counts where it is defined to sit, not wherever it looks right.

    An extension list hung off a shape is that shape's, and an ``ext`` from
    another namespace is another vocabulary's element sharing a local name --
    neither advertises the slide's comment list, however the URI reads. The
    same package advertising it on the slide itself does.
    """
    # The one advertisement is nested in the shape tree rather than the slide.
    nested = pptx(
        extension="",
        members={"ppt/slides/slide1.xml": slide_xml(1, nested=ext())},
    )
    assert comments(nested) == []
    assert texts(nested) == ["slide 1 text"]

    # Top level, but an ``ext`` bound to something other than the slide's own
    # namespace -- including the namespace the extension's own content is in.
    for namespace in ("urn:q", MODERN_NS, CLASSIC_NS.replace("2006", "2206")):
        foreign = pptx(extension=ext(element_namespace=namespace))
        assert comments(foreign) == [], namespace
        assert texts(foreign) == ["slide 1 text"], namespace

    assert [text for text, _ in comments(pptx())] == ["Tighten this claim"]


def test_both_relationship_ids_are_recorded_and_the_locator_is_deterministic() -> None:
    """Every comment and reply carries the id it was reached by and the one named.

    The two are the same id, because a slide naming any other relationship is
    refused, and the locator is the same on every read of the same package.
    """
    content = pptx(
        comments={
            "ppt/comments/modernComment2.xml": listed(
                comment(replies=reply("agreed")), root="cmLst"
            )
        },
        slide_rels=(
            COMMENT_RELATION.replace('"rId2"', '"rId7"').replace(
                "modernComment1", "modernComment2"
            ),
        ),
        extension=ext(f' xmlns:r="{REL_NS}" r:id="rId7"'),
    )

    first, second = comments(content)
    for _text, locator in (first, second):
        assert locator["relationshipId"] == "rId7"
        assert locator["extensionRelationshipId"] == "rId7"
    assert list(first[1]) == list(EXPECTED), "locator keys are the same shape"
    assert comments(content) == comments(content)


def test_an_advertised_extension_naming_no_qualifying_relationship_is_refused() -> None:
    """Advertising the extension promises the relationship it names is there.

    A slide relating to nothing, to a part under another type, or to a part the
    extension did not name has advertised a list it cannot produce.
    """
    for slide in (None, "", LAYOUT_RELATION):
        with pytest.raises(OOXMLInvalid, match="extension names a missing relationship"):
            extract_pptx(
                pptx(
                    slide_rels=(slide,),
                    members={
                        "ppt/slideLayouts/slideLayout1.xml": '<p:sldLayout xmlns:p="urn:p"/>'
                    },
                ),
                capture_id=CAPTURE,
            )

    # The qualifying relationship is there, under another id than the one named.
    with pytest.raises(OOXMLInvalid, match="extension names another relationship"):
        extract_pptx(
            pptx(slide_rels=(COMMENT_RELATION.replace('"rId2"', '"rId9"'),)),
            capture_id=CAPTURE,
        )


def test_a_duplicate_or_incomplete_extension_is_refused() -> None:
    """One advertisement naming one relationship, or the slide means nothing."""
    with pytest.raises(OOXMLInvalid, match="advertises modern comments more than once"):
        extract_pptx(pptx(extension=ext() * 2), capture_id=CAPTURE)

    for rels in (0, 2):
        with pytest.raises(
            OOXMLInvalid, match="does not name exactly one relationship"
        ):
            extract_pptx(pptx(extension=ext(rels=rels)), capture_id=CAPTURE)

    # A ``commentRel`` from any other namespace is not the one element the
    # extension is defined to hold, so the extension holds none.
    for namespace in (CLASSIC_NS, MODERN_NS.replace("2018/8", "2018/9")):
        with pytest.raises(
            OOXMLInvalid, match="does not name exactly one relationship"
        ):
            extract_pptx(pptx(extension=ext(namespace=namespace)), capture_id=CAPTURE)


def test_an_extension_id_outside_the_relationships_namespaces_is_refused() -> None:
    """An ``r:id`` is an id only in the relationships namespace it is declared in."""
    for ids in (
        # No id at all.
        "",
        # Unprefixed: an attribute in no namespace, which names no relationship.
        ' id="rId2"',
        # Prefixed, but bound to something that is not the relationships
        # namespace -- including the modern namespace the element itself is in.
        ' xmlns:q="urn:r" q:id="rId2"',
        f' xmlns:q="{MODERN_NS}" q:id="rId2"',
        # A lookalike: the relationships namespace with something appended.
        f' xmlns:q="{REL_NS}/comments" q:id="rId2"',
    ):
        with pytest.raises(OOXMLInvalid, match="names no relationship id"):
            extract_pptx(pptx(extension=ext(ids)), capture_id=CAPTURE)

    # Two ids, each qualifying, leave which relationship is meant ambiguous.
    with pytest.raises(OOXMLInvalid, match="names more than one relationship id"):
        extract_pptx(
            pptx(
                extension=ext(
                    f' xmlns:r="{REL_NS}" xmlns:q="{STRICT_REL_NS}"'
                    ' r:id="rId2" q:id="rId2"'
                )
            ),
            capture_id=CAPTURE,
        )


def test_a_repeated_slide_relationship_id_is_refused() -> None:
    """The extension names one id, so one relationship may claim it."""
    with pytest.raises(OOXMLInvalid, match="OOXML relationship is malformed"):
        extract_pptx(
            pptx(slide_rels=(COMMENT_RELATION * 2,)),
            capture_id=CAPTURE,
        )


def test_an_absent_or_empty_comment_list_reports_no_comment() -> None:
    """A slide advertising nothing, relating to nothing, or an empty list."""
    assert comments(pptx(slide_rels=(None,), extension="")) == []
    assert comments(pptx(slide_rels=("",), extension="")) == []
    assert comments(pptx(comments={COMMENT_PART: listed(root="cmLst")})) == []


def test_an_empty_modern_comment_is_reported_as_an_empty_comment() -> None:
    """A body written with no text is a comment, not an absent one."""
    content = pptx(
        comments={COMMENT_PART: listed(comment(body=text_body("")), root="cmLst")}
    )

    (segment,) = [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ]
    assert (segment.text, segment.structured_value) == (None, {"text": ""})
    assert segment.visibility == "annotation"
    assert dict(segment.source_locator) == EXPECTED

    # The extraction document_intake would store is one it accepts.
    stored, _digest = _validated_segments(
        extract_pptx(content, capture_id=CAPTURE).segments,
        workspace_id="ws-pptx-modern",
        extraction_revision_id="exr-pptx-modern",
        capture_id=CAPTURE,
        fencing_generation=1,
    )
    (row,) = [item for item in stored if item.semantic_role == "comment"]
    assert (row.text, row.visibility) == (None, "annotation")
    assert row.structured_value_json is not None
    assert json.loads(row.structured_value_json) == {"text": ""}


def test_a_comments_looking_part_nothing_relates_to_is_never_read() -> None:
    """No comments path is derived from a slide's name, so an orphan is inert."""
    content = pptx(
        slide_rels=(LAYOUT_RELATION,),
        extension="",
        members={"ppt/slideLayouts/slideLayout1.xml": '<p:sldLayout xmlns:p="urn:p"/>'},
    )
    assert comments(content) == []
    assert texts(content) == ["slide 1 text"]


def test_only_the_defined_relationship_uris_qualify() -> None:
    """A lookalike, a suffix match and the classic type all reach nothing.

    Every target below is the real, qualified modern comments part, so the only
    thing keeping it unread is the relationship type failing to qualify. A
    slide that advertised the extension has named a list it cannot produce and
    is refused; one that advertised nothing simply has no modern comments.
    """
    unqualified = (
        # Same tail, different host.
        "http://comments.example.invalid/office/2018/10/relationships/comments",
        # The defined URI with something appended, and with something prepended.
        COMMENTS_TYPE + "Extra",
        "https://proxy.example.invalid/" + COMMENTS_TYPE,
        # The authors type: a slide's authors relationship names no comment
        # list, and the author table is the presentation's in any case.
        AUTHORS_TYPE,
        # The path is case-sensitive, so a folded path names another type.
        COMMENTS_TYPE.replace("relationships", "Relationships"),
    )

    for kind in unqualified:
        relation = COMMENT_RELATION.replace(COMMENTS_TYPE, kind)
        with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
            extract_pptx(pptx(slide_rels=(relation,)), capture_id=CAPTURE)

        content = pptx(slide_rels=(relation,), extension="")
        assert comments(content) == [], kind
        # Not refused either: an unrelated relationship leaves the slide readable.
        assert texts(content) == ["slide 1 text"], kind


def test_the_classic_relationship_does_not_reach_a_modern_part() -> None:
    """A classic relationship claims the part for the classic readers.

    It qualifies as a classic comments relationship, so the part has to be a
    classic comments part; it is not one, and nothing falls back to reading it
    as the modern list its content type declares.
    """
    classic = pptx(slide_rels=(COMMENT_RELATION.replace(COMMENTS_TYPE, CLASSIC_COMMENTS_TYPE),))

    with pytest.raises(OOXMLInvalid, match="comments relationship does not name"):
        extract_pptx(classic, capture_id=CAPTURE)


def test_a_defined_uri_differing_in_host_case_is_the_type() -> None:
    """Scheme and host are case-insensitive, so folding them still qualifies."""
    shouted = "HTTP://SCHEMAS.MICROSOFT.COM/office/2018/10/relationships/comments"

    content = pptx(slide_rels=(COMMENT_RELATION.replace(COMMENTS_TYPE, shouted),))
    assert [text for text, _ in comments(content)] == ["Tighten this claim"]


def test_an_undeclared_or_wrongly_declared_target_is_refused() -> None:
    """The target has to be declared a modern comments part, not merely named one."""
    classic = (
        f'<Override PartName="/{COMMENT_PART}" ContentType="{CLASSIC_COMMENTS_CT}"/>'
    )

    for overrides in ("", classic):
        with pytest.raises(OOXMLInvalid, match="does not name a modern comments part"):
            extract_pptx(
                pptx(overrides=overrides + AUTHOR_OVERRIDE), capture_id=CAPTURE
            )

    with pytest.raises(
        OOXMLInvalid, match="does not name a modern comment authors part"
    ):
        extract_pptx(
            pptx(
                overrides=f'<Override PartName="/{COMMENT_PART}"'
                f' ContentType="{COMMENTS_CT}"/>'
                + f'<Override PartName="/{AUTHOR_PART}"'
                f' ContentType="{CLASSIC_AUTHORS_CT}"/>'
            ),
            capture_id=CAPTURE,
        )


def test_an_external_comment_relationship_is_refused() -> None:
    """Both modern parts live in the package; nothing here resolves a URI."""
    external = (
        f'<Relationship Id="rId2" Type="{COMMENTS_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/modernComment1.xml"/>'
    )

    with pytest.raises(OOXMLInvalid, match="modern comments relationship is external"):
        extract_pptx(pptx(slide_rels=(external,)), capture_id=CAPTURE)

    with pytest.raises(
        OOXMLInvalid, match="modern comment authors relationship is external"
    ):
        extract_pptx(
            pptx(
                presentation_rels=f'<Relationship Id="rIdA" Type="{AUTHORS_TYPE}"'
                ' TargetMode="External" Target="https://example.invalid/a.xml"/>'
            ),
            capture_id=CAPTURE,
        )


def test_a_phantom_or_escaping_target_is_refused() -> None:
    """A declared part that is not a member, and a target that leaves the package."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(
            pptx(
                comments={},
                overrides=f'<Override PartName="/{COMMENT_PART}"'
                f' ContentType="{COMMENTS_CT}"/>' + AUTHOR_OVERRIDE,
            ),
            capture_id=CAPTURE,
        )

    traversal = COMMENT_RELATION.replace(
        "../comments/modernComment1.xml", "../../../etc/passwd"
    )
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(slide_rels=(traversal,)), capture_id=CAPTURE)


def test_two_qualifying_relationships_on_one_source_are_refused() -> None:
    """Which part is meant is ambiguous, so neither is read."""
    second = (
        f'<Relationship Id="rId3" Type="{COMMENTS_TYPE}"'
        ' Target="../comments/modernComment2.xml"/>'
    )
    with pytest.raises(
        OOXMLInvalid, match="more than one modern comments relationship"
    ):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: COMMENT_LIST,
                    "ppt/comments/modernComment2.xml": COMMENT_LIST,
                },
                slide_rels=(COMMENT_RELATION + second,),
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(
        OOXMLInvalid, match="presentation names more than one modern comment authors"
    ):
        extract_pptx(
            pptx(
                presentation_rels=AUTHOR_RELATION
                + f'<Relationship Id="rIdB" Type="{AUTHORS_TYPE}"'
                ' Target="authors.xml"/>'
            ),
            capture_id=CAPTURE,
        )


def test_a_modern_part_in_another_namespace_or_root_is_refused() -> None:
    """The declared type says what the part is; the root says so itself.

    The classic namespace is another namespace here, so a classic list declared
    as a modern part is refused rather than read by the classic readers.
    """
    for part in (
        listed(comment(), root="cmLst", namespace=CLASSIC_NS),
        listed(comment(), root="cmLst", namespace="urn:p"),
        listed('<x:cm xmlns:x="urn:x"/>', root="cmLst"),
        listed(comment(extra='<x:thing xmlns:x="urn:x"/>'), root="cmLst"),
    ):
        with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
            extract_pptx(pptx(comments={COMMENT_PART: part}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
        extract_pptx(
            pptx(authors=listed(author(), root="authorLst", namespace=CLASSIC_NS)),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="not rooted at its own list"):
        extract_pptx(
            pptx(comments={COMMENT_PART: listed(comment(), root="authorLst")}),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="not rooted at its own list"):
        extract_pptx(pptx(authors=listed(author(), root="cmLst")), capture_id=CAPTURE)


def test_an_element_the_modern_schema_does_not_place_here_is_refused() -> None:
    for part in (
        listed(author(), root="cmLst"),
        listed(comment(extra=f'<p188:author id="{AUTHOR_ID}"/>'), root="cmLst"),
        listed(comment(replies=author()), root="cmLst"),
    ):
        with pytest.raises(OOXMLInvalid, match="unsupported element"):
            extract_pptx(pptx(comments={COMMENT_PART: part}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="unsupported element"):
        extract_pptx(pptx(authors=listed(comment(), root="authorLst")), capture_id=CAPTURE)

    # ``extLst`` is a legal extension point on both lists and both entries, and
    # is never read.
    accepted = pptx(
        comments={
            COMMENT_PART: listed(
                comment(
                    extra='<p188:extLst><p188:ext uri="urn:x"/></p188:extLst>',
                    replies="<p188:extLst/>" + reply("agreed"),
                ),
                "<p188:extLst/>",
                root="cmLst",
            )
        },
        authors=listed(author(extra="<p188:extLst/>"), "<p188:extLst/>", root="authorLst"),
    )
    assert [
        (text, locator["replyIndex"]) for text, locator in comments(accepted)
    ] == [("Tighten this claim", None), ("agreed", 0)]


def test_malformed_modern_comment_xml_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(comments={COMMENT_PART: f'<p188:cmLst xmlns:p188="{MODERN_NS}">'}),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(authors=f'<p188:authorLst xmlns:p188="{MODERN_NS}"'),
            capture_id=CAPTURE,
        )


def test_a_missing_or_malformed_guid_is_refused() -> None:
    """Identity is an ``ST_Guid``, so an absent or unshaped one names nobody."""
    for entry in (comment(comment_id=None), comment(author_id=None), reply(comment_id=None)):
        holder = (
            listed(comment(replies=entry), root="cmLst")
            if "reply" in entry
            else listed(entry, root="cmLst")
        )
        with pytest.raises(OOXMLInvalid, match="modern comment id is missing"):
            extract_pptx(pptx(comments={COMMENT_PART: holder}), capture_id=CAPTURE)

    for bad in ("", "not-a-guid", COMMENT_ID.strip("{}"), COMMENT_ID + "x"):
        with pytest.raises(OOXMLInvalid, match="modern comment id is malformed"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(comment(comment_id=bad), root="cmLst")
                    }
                ),
                capture_id=CAPTURE,
            )

    with pytest.raises(OOXMLInvalid, match="modern comment id is malformed"):
        extract_pptx(
            pptx(authors=listed(author("0"), root="authorLst")), capture_id=CAPTURE
        )


def test_a_repeated_identity_is_refused() -> None:
    """One id, one entry: a comment and a reply share the part's id space."""
    with pytest.raises(OOXMLInvalid, match="comment id is repeated"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(comment("one"), comment("two"), root="cmLst")
                }
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="comment id is repeated"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(replies=reply("echo", comment_id=COMMENT_ID)),
                        root="cmLst",
                    )
                }
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="comment author id is repeated"):
        extract_pptx(
            pptx(
                authors=listed(
                    author(), author(AUTHOR_ID.lower(), name="Ola Ruiz"), root="authorLst"
                )
            ),
            capture_id=CAPTURE,
        )


def test_a_malformed_modern_body_or_anchor_is_refused() -> None:
    """No body, two bodies, two anchors, two threads, or an unreadable position."""
    for entry in (comment(body=""), comment(extra=text_body("again"))):
        with pytest.raises(OOXMLInvalid, match="modern comment text is malformed"):
            extract_pptx(
                pptx(comments={COMMENT_PART: listed(entry, root="cmLst")}),
                capture_id=CAPTURE,
            )

    with pytest.raises(OOXMLInvalid, match="more than one position"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(extra='<p188:pos x="1" y="2"/>'), root="cmLst"
                    )
                }
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="more than one anchor"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(anchor="<p188:sldMkLst/><p188:docMkLst/>"), root="cmLst"
                    )
                }
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="more than one thread"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(replies=reply("agreed"), extra="<p188:replyLst/>"),
                        root="cmLst",
                    )
                }
            ),
            capture_id=CAPTURE,
        )

    for anchor in ('<p188:pos x="1"/>', '<p188:pos x="1" y="z"/>'):
        with pytest.raises(OOXMLInvalid, match="comment position is malformed"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(comment(pos=anchor), root="cmLst")
                    }
                ),
                capture_id=CAPTURE,
            )


def test_markup_inside_modern_comment_text_stays_inert_text() -> None:
    """Escaped markup is text: nothing is parsed, resolved or executed from it."""
    payload = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;img src=x&gt;"
    content = pptx(comments={COMMENT_PART: listed(comment(payload), root="cmLst")})

    assert [text for text, _ in comments(content)] == [
        "<script>alert(1)</script> & <img src=x>"
    ]


def test_modern_comment_counts_and_text_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = listed(
        comment(replies=reply("one") + reply("two", comment_id=SECOND_ID)),
        root="cmLst",
    )

    # Replies count against the same bound: a thread is work the part asks for.
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENTS", 2)
    with pytest.raises(OOXMLInvalid, match="too many comments"):
        extract_pptx(pptx(comments={COMMENT_PART: thread}), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENTS", 10_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_AUTHORS", 1)
    with pytest.raises(OOXMLInvalid, match="author table is too large"):
        extract_pptx(pptx(), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_AUTHORS", 10_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_RUNS", 2)
    with pytest.raises(OOXMLInvalid, match="too many runs"):
        extract_pptx(pptx(comments={COMMENT_PART: thread}), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_RUNS", 20_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_CHARS", 4)
    with pytest.raises(OOXMLInvalid, match="comment text exceeds"):
        extract_pptx(
            pptx(comments={COMMENT_PART: listed(comment("abcde"), root="cmLst")}),
            capture_id=CAPTURE,
        )
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_CHARS", 100_000)

    # The slide's own text takes the one allowed segment, so the comment is one
    # too many however few comments the slide holds.
    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 1)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(pptx(), capture_id=CAPTURE)


def test_only_a_state_the_enumeration_defines_is_accepted() -> None:
    """``ST_CommentStatus`` is closed, so an undefined state is refused.

    The three defined states come through as written, and a comment claiming
    none keeps none -- nothing defaults an absent status to "active". Anything
    else is refused rather than carried: a reader asking whether a comment is
    still open would answer an unrecognised state with its own default.
    """
    for state in ("active", "resolved", "closed"):
        content = pptx(
            comments={
                COMMENT_PART: listed(
                    comment(claims=f' status="{state}"'), root="cmLst"
                )
            }
        )

        ((_text, locator),) = comments(content)
        assert (locator["status"], locator["created"]) == (state, None)

    content = pptx(comments={COMMENT_PART: listed(comment(claims=""), root="cmLst")})
    ((_text, locator),) = comments(content)
    assert locator["status"] is None

    # Case is not folded in: the enumeration is case-sensitive, so a state
    # differing only in case is a value the writer did not write.
    for state in ("", " ", "Active", "RESOLVED", "open", "done", "a" * 5000):
        with pytest.raises(OOXMLInvalid, match="status is not a defined state"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(
                            comment(claims=f' status="{state}"'), root="cmLst"
                        )
                    }
                ),
                capture_id=CAPTURE,
            )


def test_a_reply_state_is_checked_against_the_same_enumeration() -> None:
    """A reply may claim a state, and it is the same closed set."""
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment(replies=reply("agreed", claims=' status="closed"')),
                root="cmLst",
            )
        }
    )

    _first, (text, locator) = comments(content)
    assert (text, locator["status"], locator["created"]) == ("agreed", "closed", None)

    with pytest.raises(OOXMLInvalid, match="status is not a defined state"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(replies=reply("agreed", claims=' status="Closed"')),
                        root="cmLst",
                    )
                }
            ),
            capture_id=CAPTURE,
        )


def test_a_reply_holds_only_the_children_its_own_type_allows() -> None:
    """``CT_CommentReply`` admits a body and the extension point, nothing else.

    A position, an anchor and a nested thread are a comment's children, so a
    reply writing one is refused rather than read as a comment -- an empty
    ``replyLst`` included, which names a thread a reply may not hold however
    little is in it.
    """
    for illegal in (
        '<p188:pos x="1524000" y="-762000"/>',
        "<p188:sldMkLst/>",
        "<p188:unknownAnchor/>",
        "<p188:txMkLst/>",
        f'<p188:author id="{AUTHOR_ID}"/>',
    ):
        with pytest.raises(OOXMLInvalid, match="unsupported element"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(
                            comment(replies=reply("agreed", extra=illegal)),
                            root="cmLst",
                        )
                    }
                ),
                capture_id=CAPTURE,
            )

    for thread in (
        "<p188:replyLst/>",
        f"<p188:replyLst>{reply('deeper')}</p188:replyLst>",
    ):
        with pytest.raises(OOXMLInvalid, match="reply holds replies of its own"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(
                            comment(replies=reply("agreed", extra=thread)),
                            root="cmLst",
                        )
                    }
                ),
                capture_id=CAPTURE,
            )


def test_a_reply_carries_only_the_attributes_its_own_type_allows() -> None:
    """Identity, author, state and creation time; the task fields are not a reply's.

    Each refused attribute would otherwise be reported as a reply-only claim
    the schema never lets a reply make, so none of them is read -- and the keys
    they would have filled stay empty on every legal reply.
    """
    for claims in (
        ' title="Tighten the claim"',
        f' assignedTo="{OTHER_AUTHOR_ID}"',
        ' startDate="2026-03-01T00:00:00.000"',
        ' dueDate="2026-03-09T00:00:00.000"',
        ' complete="50000"',
        ' idx="1"',
        ' xmlns:x="urn:x" x:note="later"',
    ):
        with pytest.raises(OOXMLInvalid, match="reply holds an unsupported attribute"):
            extract_pptx(
                pptx(
                    comments={
                        COMMENT_PART: listed(
                            comment(
                                replies=reply(
                                    "agreed",
                                    claims=' created="2026-03-05T06:07:08.000"'
                                    + claims,
                                )
                            ),
                            root="cmLst",
                        )
                    }
                ),
                capture_id=CAPTURE,
            )

    # Every attribute a reply may carry, carried at once, with the comment-only
    # keys reported empty rather than filled from somewhere.
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment(
                    replies=reply(
                        "agreed",
                        claims=' created="2026-03-05T06:07:08.000" status="resolved"',
                    )
                ),
                root="cmLst",
            )
        }
    )

    _first, (_text, locator) = comments(content)
    assert (locator["commentId"], locator["authorId"]) == (REPLY_ID, OTHER_AUTHOR_ID)
    assert (locator["created"], locator["status"]) == (
        "2026-03-05T06:07:08.000",
        "resolved",
    )
    assert [
        locator[key]
        for key in ("title", "assignedTo", "startDate", "dueDate", "complete")
    ] == [None] * 5
