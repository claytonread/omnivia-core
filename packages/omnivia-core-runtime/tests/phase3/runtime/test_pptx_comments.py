"""Classic PPTX slide comments: qualification, provenance and refusals.

Only the classic (per-slide ``p:cmLst``) comment form is read here. Threaded
"modern" comments carry a different relationship and content type, so they
reach nothing in this module and are covered only by the test that shows their
relationship type does not qualify.
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
# The two PresentationML main namespaces the comment parts may be written in.
TRANSITIONAL_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_NS = "http://purl.oclc.org/ooxml/presentationml/main"
# The two relationship-type URIs OOXML defines for each of the two parts.
COMMENTS_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
STRICT_COMMENTS_TYPE = "http://purl.oclc.org/ooxml/officeDocument/relationships/comments"
AUTHORS_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors"
)
STRICT_AUTHORS_TYPE = (
    "http://purl.oclc.org/ooxml/officeDocument/relationships/commentAuthors"
)
# The content types the two parts have to be declared with.
COMMENTS_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.comments+xml"
)
AUTHORS_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.commentAuthors+xml"
)
COMMENT_PART = "ppt/comments/comment1.xml"
AUTHOR_PART = "ppt/commentAuthors.xml"
COMMENT_RELATION = (
    f'<Relationship Id="rId2" Target="../comments/comment1.xml" Type="{COMMENTS_TYPE}"/>'
)
AUTHOR_RELATION = (
    f'<Relationship Id="rIdA" Target="commentAuthors.xml" Type="{AUTHORS_TYPE}"/>'
)
AUTHOR_OVERRIDE = f'<Override PartName="/{AUTHOR_PART}" ContentType="{AUTHORS_CT}"/>'


def comment(
    text: str | None = "Tighten this claim",
    *,
    author_id: str | None = "0",
    idx: str | None = "1",
    dt: str | None = "2026-03-04T05:06:07.000",
    pos: str | None = '<p:pos x="1524000" y="-762000"/>',
    extra: str = "",
) -> str:
    """One ``p:cm`` entry; a None attribute or body part is left out entirely."""
    attributes = "".join(
        f' {key}="{value}"'
        for key, value in (("authorId", author_id), ("idx", idx), ("dt", dt))
        if value is not None
    )
    body = (pos or "") + ("" if text is None else f"<p:text>{text}</p:text>")
    return f"<p:cm{attributes}>{body}{extra}</p:cm>"


def author(
    author_id: str = "0",
    *,
    name: str | None = "Dana Lee",
    initials: str | None = "DL",
    last: str | None = "2",
    colour: str | None = "0",
    extra: str = "",
) -> str:
    """One ``p:cmAuthor`` record; a None attribute is left out entirely."""
    attributes = "".join(
        f' {key}="{value}"'
        for key, value in (
            ("id", author_id),
            ("name", name),
            ("initials", initials),
            ("lastIdx", last),
            ("clrIdx", colour),
        )
        if value is not None
    )
    return f"<p:cmAuthor{attributes}>{extra}</p:cmAuthor>"


def listed(*entries: str, root: str, namespace: str = TRANSITIONAL_NS) -> str:
    """Wrap entries in one of the two comment list roots."""
    return f'<p:{root} xmlns:p="{namespace}">{"".join(entries)}</p:{root}>'


COMMENT_LIST = listed(comment(), root="cmLst")
AUTHOR_LIST = listed(author(), root="cmAuthorLst")
# The provenance the default fixture's single comment carries.
EXPECTED = {
    "captureId": CAPTURE,
    "part": COMMENT_PART,
    "relationshipId": "rId2",
    "commentId": "0:1",
    "commentIndex": "1",
    "authorId": "0",
    "authorPart": AUTHOR_PART,
    "authorName": "Dana Lee",
    "authorInitials": "DL",
    "authorLastIndex": "2",
    "authorColorIndex": "0",
    "authorAuthenticated": False,
    "timestamp": "2026-03-04T05:06:07.000",
    "x": 1524000,
    "y": -762000,
    "slidePart": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
}


def pptx(
    *,
    comments: dict[str, str] | None = None,
    slide_rels: tuple[str | None, ...] = (COMMENT_RELATION,),
    presentation_rels: str = AUTHOR_RELATION,
    authors: str | None = AUTHOR_LIST,
    overrides: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a presentation whose slides carry the given comment relationships.

    ``comments`` maps part name to the list each holds; a part left out of it is
    simply not in the package, which is how a phantom filename-only part is
    built. ``overrides`` defaults to declaring exactly the parts that are there.
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
        '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">'
        f"<p:sldIdLst>{slides}</p:sldIdLst></p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        f"<Relationships>{relations}{presentation_rels}</Relationships>"
    )
    for index, slide in enumerate(slide_rels, start=1):
        parts[f"ppt/slides/slide{index}.xml"] = (
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree>'
            f'<p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:spPr/><p:txBody>'
            f"<a:p><a:r><a:t>slide {index} text</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
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


def test_a_classic_comment_carries_its_whole_source_claim() -> None:
    """Text, identity, author record, timestamp and anchor all come through."""
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


def test_the_strict_relationship_and_namespace_reach_the_same_comments() -> None:
    """Strict and Transitional each name the one type, and the one vocabulary."""
    strict = pptx(
        comments={COMMENT_PART: listed(comment(), root="cmLst", namespace=STRICT_NS)},
        slide_rels=(COMMENT_RELATION.replace(COMMENTS_TYPE, STRICT_COMMENTS_TYPE),),
        presentation_rels=AUTHOR_RELATION.replace(AUTHORS_TYPE, STRICT_AUTHORS_TYPE),
        authors=listed(author(), root="cmAuthorLst", namespace=STRICT_NS),
    )

    ((text, locator),) = comments(strict)
    assert (text, locator) == ("Tighten this claim", EXPECTED)


def test_comments_are_emitted_in_source_order_across_slides() -> None:
    """Order is the order written, never a sort by id, author or timestamp."""
    second = listed(
        comment("second", author_id="1", idx="9", dt=None, pos=None),
        comment("first", author_id="0", idx="2", dt=None, pos=None),
        root="cmLst",
    )
    content = pptx(
        comments={COMMENT_PART: COMMENT_LIST, "ppt/comments/comment2.xml": second},
        slide_rels=(
            COMMENT_RELATION,
            COMMENT_RELATION.replace("comment1", "comment2").replace("rId2", "rId3"),
        ),
        authors=listed(author(), author("1", name="Ola Ruiz", initials="OR"), root="cmAuthorLst"),
    )

    assert [
        (text, locator["commentId"], locator["slide"], locator["slidePart"])
        for text, locator in comments(content)
    ] == [
        ("Tighten this claim", "0:1", 1, "ppt/slides/slide1.xml"),
        ("second", "1:9", 2, "ppt/slides/slide2.xml"),
        ("first", "0:2", 2, "ppt/slides/slide2.xml"),
    ]
    assert texts(content) == ["slide 1 text", "slide 2 text"]


def test_two_authors_may_each_hold_one_index() -> None:
    """``idx`` runs per author, so identity is the pair, not the index alone."""
    content = pptx(
        comments={
            COMMENT_PART: listed(
                comment("mine", author_id="0", idx="1"),
                comment("theirs", author_id="1", idx="1"),
                root="cmLst",
            )
        },
        authors=listed(author(), author("1", name="Ola Ruiz"), root="cmAuthorLst"),
    )

    assert [
        (text, locator["commentId"]) for text, locator in comments(content)
    ] == [("mine", "0:1"), ("theirs", "1:1")]


def test_author_metadata_is_recorded_where_present_and_never_authenticated() -> None:
    """Absent fields stay absent; present ones are clipped claims, not identity."""
    bare = pptx(
        authors=listed(
            author(name=None, initials=None, last=None, colour=None), root="cmAuthorLst"
        )
    )

    ((_text, locator),) = comments(bare)
    assert (
        locator["authorName"],
        locator["authorInitials"],
        locator["authorLastIndex"],
        locator["authorColorIndex"],
        locator["authorAuthenticated"],
    ) == (None, None, None, None, False)

    long = pptx(
        comments={COMMENT_PART: listed(comment(dt="Z" * 400), root="cmLst")},
        authors=listed(author(name="N" * 400), root="cmAuthorLst"),
    )
    ((_text, clipped),) = comments(long)
    assert clipped["authorName"] == "N" * 256
    assert clipped["timestamp"] == "Z" * 256


def test_an_absent_or_empty_comment_list_reports_no_comment() -> None:
    """A slide with no comments relationship, or a list holding nothing."""
    assert comments(pptx(slide_rels=(None,))) == []
    assert comments(pptx(slide_rels=("",))) == []
    assert comments(pptx(comments={COMMENT_PART: listed(root="cmLst")})) == []


def test_an_empty_comment_is_reported_as_an_empty_comment() -> None:
    """A comment written with no text is a comment, not an absent one.

    An empty list claims no comment at all; a ``p:cm`` whose ``p:text`` is empty
    claims one and says it holds nothing. The second is reported, with its whole
    provenance, carrying the empty claim as its value because ``document_intake``
    takes exactly one content representation and refuses empty text.
    """
    content = pptx(comments={COMMENT_PART: listed(comment(""), root="cmLst")})

    (segment,) = [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ]
    assert (segment.text, segment.structured_value) == (None, {"text": ""})
    assert segment.visibility == "annotation"
    assert dict(segment.source_locator) == EXPECTED
    # The slide's own text is still read, and the empty list still claims none.
    assert texts(content) == ["slide 1 text"]
    assert comments(pptx(comments={COMMENT_PART: listed(root="cmLst")})) == []

    # The extraction document_intake would store is one it accepts.
    stored, _digest = _validated_segments(
        extract_pptx(content, capture_id=CAPTURE).segments,
        workspace_id="ws-pptx-comments",
        extraction_revision_id="exr-pptx-comments",
        capture_id=CAPTURE,
        fencing_generation=1,
    )
    (row,) = [item for item in stored if item.semantic_role == "comment"]
    assert (row.text, row.visibility) == (None, "annotation")
    assert row.structured_value_json is not None
    assert json.loads(row.structured_value_json) == {"text": ""}


def test_a_comments_looking_part_nothing_relates_to_is_never_read() -> None:
    """No comments path is derived from a slide's name, so an orphan is inert.

    The package below holds the part at exactly the conventional name, declared
    with the comments content type, and the slide relates to something else.
    """
    unrelated = (
        '<Relationship Id="rId2" Type="urn:x/officeDocument/relationships/slideLayout"'
        ' Target="../slideLayouts/slideLayout1.xml"/>'
    )

    content = pptx(
        slide_rels=(unrelated,),
        members={"ppt/slideLayouts/slideLayout1.xml": '<p:sldLayout xmlns:p="urn:p"/>'},
    )
    assert comments(content) == []
    assert texts(content) == ["slide 1 text"]


def test_only_the_defined_relationship_uris_qualify() -> None:
    """A lookalike, a suffix match and the threaded-comment type all reach nothing.

    Every target below is the real, qualified comments part, so the only thing
    keeping it unread is the relationship type failing to qualify.
    """
    unqualified = (
        # Same tail, different host.
        "http://comments.example.invalid/officeDocument/2006/relationships/comments",
        # The defined URI with something appended, and with something prepended.
        COMMENTS_TYPE + "Extra",
        "https://proxy.example.invalid/" + COMMENTS_TYPE,
        # Threaded ("modern") comments: a different type, not implemented here.
        "http://schemas.microsoft.com/office/powerpoint/2018/4/relationships/comments",
        # The path is case-sensitive, so a folded path names another type.
        AUTHORS_TYPE.lower(),
    )

    for kind in unqualified:
        content = pptx(slide_rels=(COMMENT_RELATION.replace(COMMENTS_TYPE, kind),))
        assert comments(content) == [], kind
        # Not refused either: an unrelated relationship leaves the slide readable.
        assert texts(content) == ["slide 1 text"], kind


def test_a_defined_uri_differing_in_host_case_is_the_type() -> None:
    """Scheme and host are case-insensitive, so folding them still qualifies."""
    shouted = (
        "HTTP://SCHEMAS.OPENXMLFORMATS.ORG"
        "/officeDocument/2006/relationships/comments"
    )

    assert [
        text for text, _ in comments(pptx(slide_rels=(COMMENT_RELATION.replace(COMMENTS_TYPE, shouted),)))
    ] == ["Tighten this claim"]


def test_an_undeclared_or_wrongly_declared_target_is_refused() -> None:
    """The target has to be declared a comments part, not merely be named one."""
    layout = (
        f'<Override PartName="/{COMMENT_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
    )

    for overrides in ("", layout):
        with pytest.raises(OOXMLInvalid, match="does not name a comments part"):
            extract_pptx(
                pptx(overrides=overrides + AUTHOR_OVERRIDE), capture_id=CAPTURE
            )

    with pytest.raises(OOXMLInvalid, match="does not name a comment authors part"):
        extract_pptx(
            pptx(
                overrides=f'<Override PartName="/{COMMENT_PART}"'
                f' ContentType="{COMMENTS_CT}"/>'
            ),
            capture_id=CAPTURE,
        )


def test_an_external_comment_relationship_is_refused() -> None:
    """Both comment parts live in the package; nothing here resolves a URI."""
    external = (
        f'<Relationship Id="rId2" Type="{COMMENTS_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/comment1.xml"/>'
    )

    with pytest.raises(OOXMLInvalid, match="comments relationship is external"):
        extract_pptx(pptx(slide_rels=(external,)), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="comment authors relationship is external"):
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
        "../comments/comment1.xml", "../../../etc/passwd"
    )
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(slide_rels=(traversal,)), capture_id=CAPTURE)


def test_two_qualifying_relationships_on_one_source_are_refused() -> None:
    """Which part is meant is ambiguous, so neither is read.

    Each second relationship names the Strict URI against the first's
    Transitional one: both qualify, so the source carries two however they
    spell the one type.
    """
    second = (
        f'<Relationship Id="rId3" Type="{STRICT_COMMENTS_TYPE}"'
        ' Target="../comments/comment2.xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="more than one comments relationship"):
        extract_pptx(
            pptx(
                comments={COMMENT_PART: COMMENT_LIST, "ppt/comments/comment2.xml": COMMENT_LIST},
                slide_rels=(COMMENT_RELATION + second,),
            ),
            capture_id=CAPTURE,
        )

    with pytest.raises(
        OOXMLInvalid, match="presentation names more than one comment authors"
    ):
        extract_pptx(
            pptx(
                presentation_rels=AUTHOR_RELATION
                + f'<Relationship Id="rIdB" Type="{STRICT_AUTHORS_TYPE}"'
                ' Target="commentAuthors.xml"/>'
            ),
            capture_id=CAPTURE,
        )


def test_a_comment_part_in_another_namespace_or_root_is_refused() -> None:
    """The declared type says what the part is; the root says so itself."""
    for part in (
        listed(comment(), root="cmLst", namespace="urn:p"),
        listed('<x:cm xmlns:x="urn:x"/>', root="cmLst"),
        listed(comment(extra='<x:thing xmlns:x="urn:x"/>'), root="cmLst"),
    ):
        with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
            extract_pptx(pptx(comments={COMMENT_PART: part}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
        extract_pptx(
            pptx(authors=listed(author(), root="cmAuthorLst", namespace="urn:p")),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="not rooted at its own list"):
        extract_pptx(
            pptx(comments={COMMENT_PART: listed(comment(), root="cmAuthorLst")}),
            capture_id=CAPTURE,
        )
    with pytest.raises(OOXMLInvalid, match="not rooted at its own list"):
        extract_pptx(
            pptx(authors=listed(author(), root="cmLst")), capture_id=CAPTURE
        )


def test_an_element_the_comment_schema_does_not_place_here_is_refused() -> None:
    for part in (
        listed(author(), root="cmLst"),
        listed(comment(extra='<p:cmAuthor id="0"/>'), root="cmLst"),
    ):
        with pytest.raises(OOXMLInvalid, match="unsupported element"):
            extract_pptx(pptx(comments={COMMENT_PART: part}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="unsupported element"):
        extract_pptx(
            pptx(authors=listed(comment(), root="cmAuthorLst")), capture_id=CAPTURE
        )

    # ``extLst`` is a legal extension point on both entries and is never read.
    accepted = pptx(
        comments={
            COMMENT_PART: listed(
                comment(extra='<p:extLst><p:ext uri="urn:x"/></p:extLst>'), root="cmLst"
            )
        },
        authors=listed(
            author(), author("1", extra="<p:extLst/>"), root="cmAuthorLst"
        ),
    )
    assert [text for text, _ in comments(accepted)] == ["Tighten this claim"]


def test_malformed_comment_xml_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(comments={COMMENT_PART: f'<p:cmLst xmlns:p="{TRANSITIONAL_NS}">'}),
            capture_id=CAPTURE,
        )

    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(authors=f'<p:cmAuthorLst xmlns:p="{TRANSITIONAL_NS}"'),
            capture_id=CAPTURE,
        )


def test_a_repeated_comment_identity_is_refused() -> None:
    repeated = listed(comment("one"), comment("two"), root="cmLst")

    with pytest.raises(OOXMLInvalid, match="comment id is repeated"):
        extract_pptx(pptx(comments={COMMENT_PART: repeated}), capture_id=CAPTURE)


def test_a_repeated_author_id_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="comment author id is repeated"):
        extract_pptx(
            pptx(authors=listed(author(), author(name="Ola Ruiz"), root="cmAuthorLst")),
            capture_id=CAPTURE,
        )


def test_a_comment_whose_author_is_missing_or_dangling_is_refused() -> None:
    """A comment citing nobody is refused rather than reported without one."""
    # No author table at all: the presentation relates to none.
    with pytest.raises(OOXMLInvalid, match="comment names no author"):
        extract_pptx(
            pptx(authors=None, presentation_rels=""), capture_id=CAPTURE
        )

    # A table that is there but defines another id.
    with pytest.raises(OOXMLInvalid, match="comment names no author"):
        extract_pptx(
            pptx(authors=listed(author("7"), root="cmAuthorLst")), capture_id=CAPTURE
        )


def test_a_malformed_comment_index_is_refused() -> None:
    for entry in (
        comment(author_id=None),
        comment(idx=None),
    ):
        with pytest.raises(OOXMLInvalid, match="comment index is missing"):
            extract_pptx(
                pptx(comments={COMMENT_PART: listed(entry, root="cmLst")}),
                capture_id=CAPTURE,
            )

    for entry in (comment(idx="-1"), comment(idx="1.0"), comment(idx="4294967296")):
        with pytest.raises(OOXMLInvalid, match="comment index is malformed"):
            extract_pptx(
                pptx(comments={COMMENT_PART: listed(entry, root="cmLst")}),
                capture_id=CAPTURE,
            )

    with pytest.raises(OOXMLInvalid, match="comment index is malformed"):
        extract_pptx(
            pptx(authors=listed(author(last="x"), root="cmAuthorLst")),
            capture_id=CAPTURE,
        )


def test_a_malformed_comment_body_is_refused() -> None:
    """Two anchors, two bodies, no body, or markup inside the body."""
    with pytest.raises(OOXMLInvalid, match="more than one position"):
        extract_pptx(
            pptx(
                comments={
                    COMMENT_PART: listed(
                        comment(extra='<p:pos x="1" y="2"/>'), root="cmLst"
                    )
                }
            ),
            capture_id=CAPTURE,
        )

    for entry in (
        comment(extra="<p:text>again</p:text>"),
        comment(None),
        comment(extra="").replace(
            "<p:text>Tighten this claim</p:text>",
            "<p:text>plain<p:br/>more</p:text>",
        ),
    ):
        with pytest.raises(OOXMLInvalid, match="comment text is malformed"):
            extract_pptx(
                pptx(comments={COMMENT_PART: listed(entry, root="cmLst")}),
                capture_id=CAPTURE,
            )

    for anchor in ('<p:pos x="1"/>', '<p:pos x="1" y="z"/>'):
        with pytest.raises(OOXMLInvalid, match="comment position is malformed"):
            extract_pptx(
                pptx(comments={COMMENT_PART: listed(comment(pos=anchor), root="cmLst")}),
                capture_id=CAPTURE,
            )


def test_an_absent_anchor_leaves_the_position_absent() -> None:
    """Nothing is inferred from a layout, so an unanchored comment carries none."""
    content = pptx(comments={COMMENT_PART: listed(comment(pos=None), root="cmLst")})

    ((_text, locator),) = comments(content)
    assert (locator["x"], locator["y"]) == (None, None)


def test_markup_inside_comment_text_stays_inert_text() -> None:
    """Escaped markup is text: nothing is parsed, resolved or executed from it."""
    payload = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;img src=x&gt;"
    content = pptx(comments={COMMENT_PART: listed(comment(payload), root="cmLst")})

    assert [text for text, _ in comments(content)] == [
        "<script>alert(1)</script> & <img src=x>"
    ]


def test_comment_counts_and_text_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    many = listed(
        *(comment(f"c{index}", idx=str(index)) for index in range(4)), root="cmLst"
    )

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENTS", 3)
    with pytest.raises(OOXMLInvalid, match="too many comments"):
        extract_pptx(pptx(comments={COMMENT_PART: many}), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENTS", 10_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_AUTHORS", 1)
    with pytest.raises(OOXMLInvalid, match="author table is too large"):
        extract_pptx(
            pptx(authors=listed(author(), author("1"), root="cmAuthorLst")),
            capture_id=CAPTURE,
        )
    monkeypatch.setattr(ooxml_extractors, "_MAX_PPTX_COMMENT_AUTHORS", 10_000)

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
