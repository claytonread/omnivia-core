"""DOCX threaded comments: what Word's ``commentsExtended`` part claims.

The classic comments story says who commented, when and what; it says nothing
about threads. Word's own extended part is where a comment declares whether it
is a reply, which comment it replies to and whether the thread was marked done,
and it is reached only through the document's own relationship to it. The
claims land beside the classic ones in the single ``commentMetadata`` record,
so nothing downstream has to correlate two records per comment.

The classic story's own reading -- author, date, initials and comment prose --
is covered in ``test_docx_walker.py`` and is only re-checked here where the
extended claims sit beside it.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    _MAX_DOCX_COMMENT_EXTENSIONS,
    OOXMLInvalid,
    extract_docx,
)

CAPTURE = "dcap-" + "a" * 64
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
# The same story under the Strict profile. The extended vocabulary has no Strict
# spelling, so a Strict package's comments are annotated by the same records.
STRICT_W = 'xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"'
# The paragraph identity namespace is Word's 2010 extension and the extended
# comment vocabulary its 2012 one, neither of them a WordprocessingML profile.
W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
W15 = 'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
# The one relationship type and content type the part is reached by. The content
# type is written in Word's own casing here, which the package folds when it
# reads the content-types part.
EX_TYPE = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
EX_CT = "application/vnd.ms-word.commentsExtended+xml"
EX_PART = "word/commentsExtended.xml"
ROOT = "0000000A"
REPLY = "0000000B"
THIRD = "1A2B3C4D"
# The highest identity MS-DOCX leaves to a paragraph; the half above it is
# reserved, so this is the last value a record may join on.
HIGHEST = "7FFFFFFF"


def comment(note_id: str, para_id: str, text: str, attrs: str = "") -> str:
    """One classic comment, its single paragraph carrying one identity."""
    return (
        f'<w:comment w:id="{note_id}"{attrs}>'
        f'<w:p w14:paraId="{para_id}"><w:r><w:t>{text}</w:t></w:r>'
        "</w:p></w:comment>"
    )


def record(para_id: str, attrs: str = "", body: str = "") -> str:
    entry = f'<w15:commentEx w15:paraId="{para_id}"{attrs}'
    return f"{entry}/>" if not body else f"{entry}>{body}</w15:commentEx>"


def commented(
    comments: str,
    *records: str,
    relate: bool = True,
    relationship: str = EX_TYPE,
    content_type: str = EX_CT,
    extended: str | None = None,
    profile: str = W,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/{EX_PART}" ContentType="{content_type}"/></Types>',
        )
        archive.writestr(
            "word/document.xml",
            f"<w:document {profile}><w:body><w:p><w:r><w:t>body</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            "<Relationships>"
            + (
                f'<Relationship Id="rId1" Type="{relationship}"'
                ' Target="commentsExtended.xml"/>'
                if relate
                else ""
            )
            + "</Relationships>",
        )
        archive.writestr(
            "word/comments.xml", f"<w:comments {profile} {W14}>{comments}</w:comments>"
        )
        archive.writestr(
            EX_PART,
            extended
            if extended is not None
            else f"<w15:commentsEx {W15}>{''.join(records)}</w15:commentsEx>",
        )
    return buffer.getvalue()


def unidentified(count: int) -> str:
    """One classic comment claiming ``count`` identities and no id of its own."""
    paragraphs = "".join(
        f'<w:p w14:paraId="{index:08X}"/>' for index in range(1, count + 1)
    )
    return f"<w:comment>{paragraphs}</w:comment>"


def metadata(content: bytes) -> list[dict[str, object]]:
    return [
        item.structured_value
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.structured_value
        and item.structured_value["kind"] == "commentMetadata"
    ]


def test_reply_names_its_parent_comment_whatever_order_the_records_are_in() -> None:
    content = commented(
        comment("1", ROOT, "Root said") + comment("2", REPLY, "Reply said"),
        # The reply's record is written first: the list declares a join, not an
        # order, so neither the thread nor the comments follow it.
        record(REPLY, f' w15:paraIdParent="{ROOT}" w15:done="0"'),
        record(ROOT),
    )

    assert metadata(content) == [
        {
            "kind": "commentMetadata",
            "noteId": "1",
            "commentParaId": ROOT,
            "isReply": False,
            "parentParaId": None,
            "parentNoteId": None,
            "resolved": None,
        },
        {
            "kind": "commentMetadata",
            "noteId": "2",
            "commentParaId": REPLY,
            "isReply": True,
            "parentParaId": ROOT,
            "parentNoteId": "1",
            "resolved": False,
        },
    ]


def test_resolution_keeps_marked_unmarked_and_unstated_threads_apart() -> None:
    content = commented(
        comment("1", ROOT, "done")
        + comment("2", REPLY, "open")
        + comment("3", THIRD, "unstated"),
        record(ROOT, ' w15:done="1"'),
        record(REPLY, ' w15:done="false"'),
        record(THIRD),
    )

    assert [(item["noteId"], item["resolved"]) for item in metadata(content)] == [
        ("1", True),
        ("2", False),
        ("3", None),
    ]


def test_parent_identity_no_comment_claims_resolves_to_no_comment() -> None:
    content = commented(
        comment("1", REPLY, "Reply said"),
        # The comment this record replies to is gone from the story, so the
        # claim is kept and resolved to nobody rather than invented a root.
        record(REPLY, f' w15:paraIdParent="{ROOT}"'),
    )

    (item,) = metadata(content)

    assert (item["isReply"], item["parentParaId"], item["parentNoteId"]) == (
        True,
        ROOT,
        None,
    )


def test_classic_author_date_and_prose_survive_beside_the_extended_claims() -> None:
    content = commented(
        comment(
            "1",
            ROOT,
            "Comment one",
            ' w:author="Ada L." w:date="2026-01-02T03:04:05Z" w:initials="AL"',
        ),
        record(ROOT, ' w15:done="1"'),
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert metadata(content) == [
        {
            "kind": "commentMetadata",
            "noteId": "1",
            "author": "Ada L.",
            "date": "2026-01-02T03:04:05Z",
            "initials": "AL",
            "commentParaId": ROOT,
            "isReply": False,
            "parentParaId": None,
            "parentNoteId": None,
            "resolved": True,
        }
    ]
    assert [
        (item.semantic_role, item.text)
        for item in result.segments
        if item.text is not None
    ] == [("body", "body"), ("comment", "Comment one")]


def test_unrelated_extended_part_is_neither_read_nor_mistaken_for_comments() -> None:
    content = commented(
        comment("1", ROOT, "Comment one"),
        record(ROOT, ' w15:done="1"'),
        relate=False,
    )

    result = extract_docx(content, capture_id=CAPTURE)

    # Nothing relates to the part, so it contributes no thread metadata; and its
    # name is not what makes it the comments story, so it contributes no prose
    # and no paragraph count either.
    assert metadata(content) == []
    assert [item.text for item in result.segments if item.text is not None] == [
        "body",
        "Comment one",
    ]
    assert result.expected == 2


def test_two_records_for_one_comment_are_refused() -> None:
    comments = (
        f'<w:comment w:id="1"><w:p w14:paraId="{ROOT}"><w:r><w:t>one</w:t></w:r></w:p>'
        f'<w:p w14:paraId="{REPLY}"><w:r><w:t>two</w:t></w:r></w:p></w:comment>'
    )

    with pytest.raises(OOXMLInvalid, match="annotated by more than one record"):
        extract_docx(
            commented(comments, record(ROOT), record(REPLY)), capture_id=CAPTURE
        )


def test_relationship_of_another_type_reaches_no_extended_comments() -> None:
    content = commented(
        comment("1", ROOT, "Comment one"),
        record(ROOT, ' w15:done="1"'),
        # Word's sibling extension, written on the same part. Nothing names the
        # part by the one type that qualifies it, so -- exactly as when nothing
        # relates to it at all -- it is never reached and claims nothing.
        relationship=(
            "http://schemas.microsoft.com/office/2011/relationships/commentsIds"
        ),
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert metadata(content) == []
    assert [item.text for item in result.segments if item.text is not None] == [
        "body",
        "Comment one",
    ]
    assert result.expected == 2


def test_part_declared_as_another_content_type_is_not_the_extended_part() -> None:
    content = commented(
        comment("1", ROOT, "Comment one"),
        record(ROOT),
        content_type="application/vnd.ms-word.commentsIds+xml",
    )

    with pytest.raises(OOXMLInvalid, match="does not name a extended comments part"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    "extended",
    [
        (
            f'<w15:commentsExtended {W15}><w15:commentEx w15:paraId="{ROOT}"/>'
            "</w15:commentsExtended>"
        ),
        f'<w:commentsEx {W} {W15}><w15:commentEx w15:paraId="{ROOT}"/></w:commentsEx>',
    ],
    ids=["other-name", "other-namespace"],
)
def test_part_rooted_at_anything_else_is_refused(extended: str) -> None:
    content = commented(comment("1", ROOT, "Comment one"), extended=extended)

    with pytest.raises(OOXMLInvalid, match="not rooted at its own list"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ((record(ROOT, ' w15:paraIdAuthor="1"'),), "holds an unsupported attribute"),
        ((record(ROOT, ' done="1"'),), "holds an unsupported attribute"),
        ((record(ROOT, body="<w15:extLst/>"),), "holds unsupported content"),
        ((record(ROOT, body="claim"),), "holds unsupported content"),
        ((record(ROOT), "stray"), "holds unsupported content"),
        ((f'<w:commentEx {W} w15:paraId="{ROOT}"/>',), "holds an unsupported element"),
        (('<w15:commentEx w15:done="1"/>',), "names no comment paragraph"),
    ],
    ids=[
        "qualified-attribute-outside-the-grammar",
        "unqualified-done",
        "child-element",
        "text",
        "tail",
        "lookalike-element",
        "no-identity",
    ],
)
def test_record_outside_its_own_grammar_is_refused(
    records: tuple[str, ...], message: str
) -> None:
    content = commented(comment("1", ROOT, "Comment one"), *records)

    with pytest.raises(OOXMLInvalid, match=message):
        extract_docx(content, capture_id=CAPTURE)


def test_whitespace_a_pretty_printer_leaves_is_not_record_content() -> None:
    content = commented(
        comment("1", ROOT, "Root said") + comment("2", REPLY, "Reply said"),
        "\n  ",
        record(ROOT),
        "\n  ",
        record(REPLY, f' w15:paraIdParent="{ROOT}"'),
        "\n",
    )

    assert [(item["noteId"], item["isReply"]) for item in metadata(content)] == [
        ("1", False),
        ("2", True),
    ]


@pytest.mark.parametrize(
    ("attribute", "message"),
    [
        ("w15:paraId", "extended comment paragraph identity is malformed"),
        ("w15:paraIdParent", "parent paragraph identity is malformed"),
    ],
)
@pytest.mark.parametrize(
    "value",
    ["", "0", "0000000", "000000001", "00000000", "80000000", "FFFFFFFF", "0000000G"],
)
def test_record_identity_outside_the_hex_grammar_or_range_is_refused(
    attribute: str, message: str, value: str
) -> None:
    written = (
        record(value)
        if attribute == "w15:paraId"
        else record(ROOT, f' w15:paraIdParent="{value}"')
    )
    content = commented(comment("1", ROOT, "Comment one"), written)

    with pytest.raises(OOXMLInvalid, match=message):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize("value", ["", "yes", "2", "-1", "TRUE", "True", "On"])
def test_resolution_mark_outside_its_closed_set_is_refused(value: str) -> None:
    content = commented(
        comment("1", ROOT, "Comment one"), record(ROOT, f' w15:done="{value}"')
    )

    with pytest.raises(OOXMLInvalid, match="resolution mark is malformed"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize("repeat", [ROOT, ROOT.lower()], ids=["same", "other-case"])
def test_one_identity_claimed_by_two_comments_is_refused(repeat: str) -> None:
    # Every record naming the identity would be ambiguous between the two, and
    # the case of a hex digit is not part of the number, so both spellings are
    # the one claim made twice.
    content = commented(
        comment("1", ROOT, "Comment one") + comment("2", repeat, "Comment two"),
        record(ROOT),
    )

    with pytest.raises(OOXMLInvalid, match="DOCX comment paragraph identity is repeat"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    "comments",
    [comment("1", ROOT, "Comment one"), unidentified(1) + comment("1", ROOT, "one")],
    ids=["identity-absent", "identity-held-by-a-comment-without-an-id"],
)
def test_record_naming_no_identified_comment_is_refused(comments: str) -> None:
    content = commented(comments, record(HIGHEST))

    with pytest.raises(OOXMLInvalid, match="extended comment names no comment"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize("parent", [ROOT, ROOT.lower()], ids=["same", "other-case"])
def test_record_replying_to_itself_is_refused(parent: str) -> None:
    content = commented(
        comment("1", ROOT, "Comment one"),
        record(ROOT, f' w15:paraIdParent="{parent}"'),
    )

    with pytest.raises(OOXMLInvalid, match="replies to itself"):
        extract_docx(content, capture_id=CAPTURE)


def test_two_records_replying_to_each_other_are_refused() -> None:
    content = commented(
        comment("1", ROOT, "Root said") + comment("2", REPLY, "Reply said"),
        record(ROOT, f' w15:paraIdParent="{REPLY}"'),
        record(REPLY, f' w15:paraIdParent="{ROOT}"'),
    )

    with pytest.raises(OOXMLInvalid, match="replies form a cycle"):
        extract_docx(content, capture_id=CAPTURE)


def test_strict_classic_comments_carry_the_same_extended_claims() -> None:
    content = commented(
        comment("1", ROOT, "Root said") + comment("2", REPLY, "Reply said"),
        # A record may declare the extended namespace itself: a declaration is
        # not an attribute once the part is parsed, so it is no unsupported one.
        f'<w15:commentEx {W15} w15:paraId="{REPLY}"'
        f' w15:paraIdParent="{ROOT}" w15:done="1"/>',
        record(ROOT),
        profile=STRICT_W,
    )

    assert [
        (item["noteId"], item["isReply"], item["parentNoteId"], item["resolved"])
        for item in metadata(content)
    ] == [("1", False, None, None), ("2", True, "1", True)]


def test_repeat_extraction_of_one_package_reports_exactly_the_same_segments() -> None:
    content = commented(
        comment("1", ROOT, "Root said")
        + comment("2", REPLY, "Reply said")
        + comment("3", THIRD, "Reply replied"),
        # Written leaf first, so nothing downstream can be reading the list's
        # own order as the thread's.
        record(THIRD, f' w15:paraIdParent="{REPLY}"'),
        record(REPLY, f' w15:paraIdParent="{ROOT}" w15:done="0"'),
        record(ROOT, ' w15:done="1"'),
    )

    first = extract_docx(content, capture_id=CAPTURE)

    assert extract_docx(content, capture_id=CAPTURE).segments == first.segments
    assert [item["noteId"] for item in metadata(content)] == ["1", "2", "3"]


def test_identity_bound_counts_the_paragraphs_of_a_comment_without_an_id() -> None:
    # The id-less comment's identities are kept under no comment -- nothing can
    # report a comment the walker has no identity for -- but reading them is the
    # work the bound exists to cap, so an identified comment's own identity is
    # what tips a package already at the bound over it.
    at_bound = commented(
        unidentified(_MAX_DOCX_COMMENT_EXTENSIONS - 1) + comment("1", HIGHEST, "one"),
        record(HIGHEST),
    )
    past_it = commented(
        unidentified(_MAX_DOCX_COMMENT_EXTENSIONS) + comment("1", HIGHEST, "one"),
        record(HIGHEST),
    )

    assert [item["noteId"] for item in metadata(at_bound)] == ["1"]
    with pytest.raises(OOXMLInvalid, match="too many paragraph identities"):
        extract_docx(past_it, capture_id=CAPTURE)
