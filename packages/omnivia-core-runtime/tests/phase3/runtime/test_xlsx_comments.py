"""XLSX classic worksheet comments ("notes") over minimal packages."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
COMMENTS_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"
)
WORKBOOK_RELS = (
    "<Relationships>"
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)
SHEET_PART = "xl/worksheets/sheet1.xml"
COMMENTS_PART = "xl/comments1.xml"
# One written cell, so a sheet always has grid content of its own beside the
# annotations and a comment can be seen not to have materialised one.
CELL = '<row r="1"><c r="A1" t="inlineStr"><is><t>Region</t></is></c></row>'


def authors(*names: str, tag: str = "author", holder: str = "authors") -> str:
    members = "".join(f"<{tag}>{name}</{tag}>" for name in names)
    return f"<{holder}>{members}</{holder}>"


def comment(
    body: str,
    *,
    ref: str | None = "A1",
    author_id: object = 0,
    attrs: str = "",
    tag: str = "comment",
) -> str:
    anchor = "" if ref is None else f' ref="{ref}"'
    cited = "" if author_id is None else f' authorId="{author_id}"'
    return f"<{tag}{anchor}{cited}{attrs}>{body}</{tag}>"


def listed(*entries: str, holder: str = "commentList") -> str:
    return f"<{holder}>{''.join(entries)}</{holder}>"


def comments(children: str, *, main: str = MAIN, root: str = "comments") -> str:
    """One comments part declaring ``children`` below its own root."""
    space = f' xmlns="{main}"' if main is not None else ""
    return f"<{root}{space}>{children}</{root}>"


# The ordinary part: one author, one comment at A1 carrying one stored string.
SIMPLE = comments(authors("Ada") + listed(comment("<text><t>Hi</t></text>")))


def xlsx(
    part: str | None,
    *,
    body: str = CELL,
    rels: str | None = None,
    state: str | None = None,
    main: str = MAIN,
    relns: str = RELNS,
    content_type: str | None = COMMENTS_TYPE,
    extra: dict[str, str] | None = None,
) -> bytes:
    """Build a one-sheet package whose comments part is ``part``.

    The sheet relationship to the part, the content-type override and the member
    itself are all derived from ``part``, so only the declaration under test
    varies. ``rels`` replaces the generated sheet relationships outright and
    ``content_type`` replaces the declared override -- ``None`` declares no
    override at all, leaving the part typed only by its ``.xml`` extension.
    ``extra`` adds further members verbatim.
    """
    sheet_rels = (
        rels
        if rels is not None
        else "<Relationships>"
        f'<Relationship Id="rIdC" Target="../comments1.xml" Type="{relns}/comments"/>'
        "</Relationships>"
    )
    attrs = f' state="{state}"' if state is not None else ""
    override = (
        f'<Override PartName="/{COMMENTS_PART}" ContentType="{content_type}"/>'
        if part is not None and content_type is not None
        else ""
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Default Extension="vml" ContentType="application/'
            f'vnd.openxmlformats-officedocument.vmlDrawing"/>{override}</Types>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns:r="{RELNS}"><sheets>'
            f'<sheet name="Budget" sheetId="7" r:id="rId1"{attrs}/>'
            "</sheets></workbook>",
        )
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr(
            SHEET_PART,
            f'<worksheet xmlns="{main}" xmlns:r="{relns}">'
            f"<sheetData>{body}</sheetData></worksheet>",
        )
        if part is not None or rels is not None:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
        if part is not None:
            archive.writestr(COMMENTS_PART, part)
        for name, member in (extra or {}).items():
            archive.writestr(name, member)
    return buffer.getvalue()


def annotations(content: bytes) -> list[tuple[object, ...]]:
    """Each comment segment as (anchor, text, structured value, visibility)."""
    return [
        (
            item.source_locator["cell"],
            item.text,
            item.structured_value,
            item.visibility,
        )
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ]


def locators(content: bytes) -> list[Any]:
    return [
        item.source_locator
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "comment"
    ]


def cells(content: bytes) -> list[object]:
    return [
        item.source_locator["cell"]
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role != "comment"
    ]


def refused(part: str, **kwargs: Any) -> None:
    extract_xlsx(xlsx(part, **kwargs), capture_id=CAPTURE)


def test_comments_are_reported_in_source_order_with_exact_provenance() -> None:
    """The list's own order, its stored runs concatenated, its anchors untouched.

    Four comments between them cover the shapes a body comes in: one stored
    string, two runs that are one text, a body storing nothing, and an anchor
    the sheet never wrote a cell at. The two authors spell the same name, which
    SpreadsheetML makes two authors rather than a duplicate, and the second
    comment cites the second of them.
    """
    content = xlsx(
        comments(
            authors("Ada", "Ada")
            + listed(
                comment("<text><t>second</t></text>", ref="B2"),
                comment(
                    "<text><r><t>rich </t></r><r><t>runs</t></r></text>",
                    ref="A1",
                    author_id=1,
                ),
                comment("<text/>", ref="C3"),
                comment("<text><t>sparse</t></text>", ref="Z99"),
            )
        )
    )
    extraction = extract_xlsx(content, capture_id=CAPTURE)

    assert annotations(content) == [
        ("B2", "second", None, "annotation"),
        ("A1", "rich runs", None, "annotation"),
        # An empty body is the claim the file made, carried as a value because
        # the segment contract refuses empty text.
        ("C3", None, {"text": ""}, "annotation"),
        ("Z99", "sparse", None, "annotation"),
    ]
    # A comment materialises no cell, and none of these anchors gained one.
    assert cells(content) == ["A1"]
    # Nor does one count as populated content: that stays the cell count.
    assert extraction.expected == 1
    assert extraction.unit_type == "workbook_cell"
    comment_segments = [
        item for item in extraction.segments if item.semantic_role == "comment"
    ]
    assert {item.extraction_method for item in comment_segments} == {"direct"}
    assert locators(content)[1] == {
        "captureId": CAPTURE,
        "part": COMMENTS_PART,
        "relationshipId": "rIdC",
        "commentForm": "classic",
        "commentIndex": 1,
        "cell": "A1",
        "authorId": 1,
        "author": "Ada",
        "authorAuthenticated": False,
        "sheetPart": SHEET_PART,
        "sheetId": "7",
        "sheet": "Budget",
        "sheetState": "visible",
    }
    # Two authors spelling one name stay two positions, so the first comment
    # resolves to the first of them rather than to whichever matched by name.
    assert [(item["authorId"], item["author"]) for item in locators(content)] == [
        (0, "Ada"),
        (1, "Ada"),
        (0, "Ada"),
        (0, "Ada"),
    ]


@pytest.mark.parametrize("state", ["visible", "hidden", "veryHidden"])
def test_a_comment_stays_an_annotation_whatever_the_sheet_hides(state: str) -> None:
    """Visibility is the comment's own place, so the sheet's state is reported beside it.

    A comment sits outside the grid's flow wherever the grid sits, so none of
    the three declared states folds into it; the state is in the locator for a
    consumer that needs it.
    """
    content = xlsx(SIMPLE, state=state)

    assert annotations(content) == [("A1", "Hi", None, "annotation")]
    assert locators(content)[0]["sheetState"] == state
    # The cell at the same position does follow the sheet.
    assert [
        item.visibility
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "table_cell"
    ] == ["visible" if state == "visible" else "hidden"]


def test_strict_profile_resolves_the_same_comment() -> None:
    """Strict is the other profile OOXML defines, not a foreign vocabulary."""
    content = xlsx(
        comments(
            authors("Ada") + listed(comment("<text><t>Hi</t></text>")),
            main=STRICT_MAIN,
        ),
        main=STRICT_MAIN,
        relns=STRICT_RELNS,
    )

    assert annotations(content) == [("A1", "Hi", None, "annotation")]
    assert locators(content)[0]["part"] == COMMENTS_PART


@pytest.mark.parametrize(
    ("rels", "content_type", "message"),
    [
        pytest.param(None, COMMENTS_TYPE, "", id="qualified"),
        # OPC compares content types without regard to case and the package
        # reader folds both sides, so an uppercased override still qualifies.
        pytest.param(None, COMMENTS_TYPE.upper(), "", id="uppercased-type"),
        pytest.param("<Relationships/>", COMMENTS_TYPE, None, id="no-relationship"),
        pytest.param(
            "<Relationships>"
            '<Relationship Id="rIdC" Target="../comments1.xml"'
            f' Type="{RELNS}/hyperlink"/>'
            "</Relationships>",
            COMMENTS_TYPE,
            None,
            id="another-relationship-type",
        ),
        pytest.param(
            "<Relationships>"
            '<Relationship Id="rIdC" Target="../comments1.xml"'
            f' Type="{RELNS}/commentsExtended"/>'
            "</Relationships>",
            COMMENTS_TYPE,
            None,
            id="threaded-relationship-type",
        ),
        pytest.param(
            "<Relationships>"
            '<Relationship Id="rIdC" Target="https://a.invalid/comments1.xml"'
            f' Type="{RELNS}/comments" TargetMode="External"/>'
            "</Relationships>",
            COMMENTS_TYPE,
            "XLSX comments relationship is external",
            id="external",
        ),
        pytest.param(
            "<Relationships>"
            f'<Relationship Id="rIdA" Target="../comments1.xml" Type="{RELNS}/comments"/>'
            f'<Relationship Id="rIdB" Target="../comments1.xml" Type="{STRICT_RELNS}/comments"/>'
            "</Relationships>",
            COMMENTS_TYPE,
            "XLSX worksheet names more than one comments relationship",
            id="two-qualifying-relationships",
        ),
        pytest.param(
            None, None, "does not name a comments part", id="no-override"
        ),
        pytest.param(
            None, "application/xml", "does not name a comments part", id="plain-xml"
        ),
        pytest.param(
            None,
            COMMENTS_TYPE.replace("spreadsheetml", "presentationml"),
            "does not name a comments part",
            id="slide-comments-type",
        ),
    ],
)
def test_comments_part_qualifies_by_relationship_and_content_type(
    rels: str | None, content_type: str | None, message: str | None
) -> None:
    """The part is reached on one route: an internal ``comments`` relationship
    from the worksheet, resolving to a part the package declares as
    SpreadsheetML's own comment list.

    Every case names a target that exists and holds a readable comment list, so
    what varies is only the claim that it is this sheet's comments part. A
    ``None`` message is a route that declares nothing and stays transparent --
    the sheet keeps its cell and reports no comment -- and an empty one is a
    route that resolves.
    """
    if message is None:
        content = xlsx(SIMPLE, rels=rels, content_type=content_type)
        assert annotations(content) == []
        assert cells(content) == ["A1"]
        return
    if not message:
        content = xlsx(SIMPLE, rels=rels, content_type=content_type)
        assert annotations(content) == [("A1", "Hi", None, "annotation")]
        return
    with pytest.raises(OOXMLInvalid, match=message):
        refused(SIMPLE, rels=rels, content_type=content_type)


ONE = comment("<text><t>Hi</t></text>")


@pytest.mark.parametrize(
    ("part", "message"),
    [
        # The part's root: SpreadsheetML, and rooted at its own list.
        pytest.param(
            comments(authors("Ada") + listed(ONE), main="urn:x"),
            "uses an unsupported namespace",
            id="foreign-root",
        ),
        pytest.param(
            comments(authors("Ada") + listed(ONE), main=None),
            "uses an unsupported namespace",
            id="unqualified-root",
        ),
        pytest.param(
            comments(authors("Ada") + listed(ONE), root="commentList"),
            "is not rooted at its own list",
            id="other-root",
        ),
        # Below the root, held to the root's own namespace. A lookalike spelling
        # one of these names in another vocabulary is not transparent here: it
        # is an element the part's own vocabulary does not define.
        pytest.param(
            comments(
                '<x:authors xmlns:x="urn:x"><x:author>Ada</x:author></x:authors>'
                + listed(ONE)
            ),
            "holds an unsupported element",
            id="foreign-authors",
        ),
        pytest.param(
            comments(
                '<authors xmlns=""><author>Ada</author></authors>' + listed(ONE)
            ),
            "holds an unsupported element",
            id="unqualified-authors",
        ),
        pytest.param(
            comments(
                authors("Ada") + f'<x:commentList xmlns:x="urn:x">{ONE}</x:commentList>'
            ),
            "holds an unsupported element",
            id="foreign-comment-list",
        ),
        pytest.param(
            comments(authors("Ada") + listed(ONE) + "<sheetData/>"),
            "holds an unsupported element",
            id="unrelated-element",
        ),
        # The lists themselves, each declared once.
        pytest.param(
            comments(authors("Ada") + authors("Grace") + listed(ONE)),
            "declares two author lists",
            id="two-author-lists",
        ),
        pytest.param(
            comments(authors("Ada") + listed(ONE) + listed(ONE)),
            "declares two comment lists",
            id="two-comment-lists",
        ),
        pytest.param(
            comments(listed(ONE)), "declares no author list", id="no-author-list"
        ),
        pytest.param(
            comments(authors("Ada")), "declares no comment list", id="no-comment-list"
        ),
        # An author is a bare string, reached directly below the list.
        pytest.param(
            comments(
                '<authors><x:author xmlns:x="urn:x">Ada</x:author></authors>'
                + listed(ONE)
            ),
            "author list holds an unsupported element",
            id="foreign-author",
        ),
        pytest.param(
            comments(
                "<authors><wrapped><author>Ada</author></wrapped></authors>"
                + listed(ONE)
            ),
            "author list holds an unsupported element",
            id="nested-author",
        ),
        pytest.param(
            comments("<authors><author><b>Ada</b></author></authors>" + listed(ONE)),
            "author list holds an unsupported element",
            id="structured-author",
        ),
        # A comment, and a comment's body, likewise.
        pytest.param(
            comments(
                authors("Ada")
                + listed(
                    '<x:comment xmlns:x="urn:x" ref="A1" authorId="0"><text/>'
                    "</x:comment>"
                )
            ),
            "comment list holds an unsupported element",
            id="foreign-comment",
        ),
        pytest.param(
            comments(authors("Ada") + listed(f"<wrapped>{ONE}</wrapped>")),
            "comment list holds an unsupported element",
            id="nested-comment",
        ),
        pytest.param(
            comments(
                authors("Ada")
                + listed(comment('<x:text xmlns:x="urn:x"><x:t>Hi</x:t></x:text>'))
            ),
            "comment holds an unsupported element",
            id="foreign-body",
        ),
        pytest.param(
            comments(authors("Ada") + listed(comment("<wrapped><text/></wrapped>"))),
            "comment holds an unsupported element",
            id="nested-body",
        ),
        pytest.param(
            comments(authors("Ada") + listed(comment(""))),
            "comment declares no body",
            id="no-body",
        ),
        pytest.param(
            comments(
                authors("Ada")
                + listed(comment("<text><t>a</t></text><text><t>b</t></text>"))
            ),
            "comment declares two bodies",
            id="two-bodies",
        ),
    ],
)
def test_comment_declarations_outside_the_schemas_own_route_are_refused(
    part: str, message: str
) -> None:
    """Each level of the part is reached directly, qualified in the root's namespace.

    The relationship and content type are already settled by the time any of
    this is read, so each case names a reachable member the package declares as
    a comment list: what is refused is what the member then declares about
    itself.
    """
    with pytest.raises(OOXMLInvalid, match=message):
        refused(part)


def test_extension_nesting_and_body_lookalikes_declare_nothing() -> None:
    """Below an extension and inside a body, a lookalike is inert rather than read.

    ``extLst`` is accepted and never descended into, so neither list is found
    below one -- and since each is required directly, nesting either is a part
    missing that list rather than a part declaring it. Inside a body only the
    two stored-text routes are concatenated,
    so a formatting wrapper, a phonetic reading and a foreign or unqualified
    ``t`` all contribute nothing -- and contribute no refusal either, because a
    body is rich text whose other children are formatting the reader does not
    claim to understand.
    """
    # A real author list, so what is missing is only the nested comment list.
    with pytest.raises(OOXMLInvalid, match="declares no comment list"):
        refused(comments(authors("Ada") + f"<extLst><ext>{listed(ONE)}</ext></extLst>"))

    # The same nesting applied to the author list leaves no author to resolve.
    with pytest.raises(OOXMLInvalid, match="declares no author list"):
        refused(comments(f"<extLst><ext>{authors('Ada')}</ext></extLst>" + listed(ONE)))

    body = (
        "<text>"
        "<rPr><t>formatting</t></rPr>"
        '<x:t xmlns:x="urn:x">foreign</x:t>'
        '<t xmlns="">unqualified</t>'
        "<t>kept</t>"
        "<r><rPr><t>run formatting</t></rPr><t> and run</t></r>"
        '<rPh sb="0" eb="4"><t>phonetic</t></rPh>'
        "</text>"
    )
    inert = xlsx(comments(authors("Ada") + listed(comment(body))))
    assert annotations(inert) == [("A1", "kept and run", None, "annotation")]

    # ``commentPr`` carries the on-screen box and is accepted without being read.
    boxed = xlsx(
        comments(
            authors("Ada")
            + listed(
                comment(
                    '<commentPr autoFill="0"><anchor><from><col>3</col></from>'
                    "</anchor></commentPr><text><t>Hi</t></text>"
                )
            )
        )
    )
    assert annotations(boxed) == [("A1", "Hi", None, "annotation")]
    assert locators(boxed)[0]["cell"] == "A1"


@pytest.mark.parametrize(
    ("entries", "author_names", "message"),
    [
        pytest.param([(None, 0)], ("Ada",), "declares no reference", id="no-ref"),
        pytest.param([("", 0)], ("Ada",), "declares no reference", id="empty-ref"),
        pytest.param(
            [("not-a-ref", 0)], ("Ada",), "cell coordinate is malformed", id="unreadable-ref"
        ),
        pytest.param(
            [("B2:A1", 0)], ("Ada",), "cell range is malformed", id="reversed-ref"
        ),
        pytest.param(
            [("A1", 0), ("A1", 0)], ("Ada",), "anchor is repeated", id="repeated-ref"
        ),
        # The bounds are what is compared, so these two name the one anchor.
        pytest.param(
            [("A1", 0), ("A1:A1", 0)],
            ("Ada",),
            "anchor is repeated",
            id="equivalent-ref",
        ),
        pytest.param(
            [("A1", None)], ("Ada",), "author id is malformed", id="no-author-id"
        ),
        pytest.param(
            [("A1", "")], ("Ada",), "author id is malformed", id="empty-author-id"
        ),
        pytest.param(
            [("A1", "x")], ("Ada",), "author id is malformed", id="unreadable-author-id"
        ),
        pytest.param(
            [("A1", "-1")], ("Ada",), "author id is malformed", id="negative-author-id"
        ),
        pytest.param(
            [("A1", "12345678901")],
            ("Ada",),
            "author id is malformed",
            id="over-long-author-id",
        ),
        pytest.param(
            [("A1", "4294967296")],
            ("Ada",),
            "author id is malformed",
            id="author-id-over-index-domain",
        ),
        pytest.param(
            [("A1", 1)], ("Ada",), "comment names no author", id="author-id-past-list"
        ),
        pytest.param([("A1", 0)], (), "comment names no author", id="empty-author-list"),
        # A leading zero still names a position, and the position resolves.
        pytest.param([("A1", "01")], ("Ada", "Grace"), "", id="padded-author-id"),
        # A multi-cell anchor is a range the schema allows, kept as written.
        pytest.param([("A1:B2", 0)], ("Ada",), "", id="range-anchor"),
    ],
)
def test_anchor_and_author_claims_are_refused(
    entries: list[tuple[str | None, object]], author_names: tuple[str, ...], message: str
) -> None:
    """A comment's anchor and its author are its own identity, so neither is repaired.

    An empty ``message`` is an accepted case: a zero-padded position still names
    the author it indexes, and a range anchor is kept exactly as the comment
    wrote it.
    """
    part = comments(
        authors(*author_names)
        + listed(
            *(
                comment("<text><t>Hi</t></text>", ref=ref, author_id=author_id)
                for ref, author_id in entries
            )
        )
    )
    if not message:
        content = xlsx(part)
        assert [item[0] for item in annotations(content)] == [
            entry[0] for entry in entries
        ]
        return
    with pytest.raises(OOXMLInvalid, match=message):
        refused(part)


def test_legacy_drawing_is_present_but_never_opened() -> None:
    """The VML beside a comment carries its box, never its text, so it is not read.

    The worksheet names the drawing and the package holds it, so the part is
    reachable: nothing here reaches it. The comment is placed by the ``ref`` it
    already declares, and the text the VML stores reaches no segment.
    """
    content = xlsx(
        SIMPLE,
        body=CELL,
        rels="<Relationships>"
        f'<Relationship Id="rIdC" Target="../comments1.xml" Type="{RELNS}/comments"/>'
        '<Relationship Id="rIdV" Target="../drawings/vmlDrawing1.vml"'
        f' Type="{RELNS}/vmlDrawing"/>'
        "</Relationships>",
        extra={
            "xl/drawings/vmlDrawing1.vml": (
                '<xml xmlns:v="urn:schemas-microsoft-com:vml">'
                '<v:shape type="#_x0000_t202"><v:textbox>'
                "<div>box text</div></v:textbox></v:shape></xml>"
            )
        },
    )
    extraction = extract_xlsx(content, capture_id=CAPTURE)

    assert annotations(content) == [("A1", "Hi", None, "annotation")]
    assert "xl/drawings/vmlDrawing1.vml" not in str(locators(content))
    assert all("box text" not in str(item) for item in extraction.segments)
    # Seven members, the drawing among them: it was in the package, not read.
    assert extraction.limitations["packageMembers"] == 7


def test_practical_bounds_hold_where_the_reader_takes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each bound refuses one past itself and admits what lands on it.

    The run bound is taken across the whole part, so two comments of one run
    each reach it together; the character bound is taken per comment, so two
    comments of four characters each do not. Author names are clipped rather
    than refused, because a name is metadata the record carries.
    """
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENTS", 1)
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_AUTHORS", 1)
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_RUNS", 2)
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_CHARS", 4)

    def body(runs: int) -> str:
        return "<text>" + "<r><t>ab</t></r>" * runs + "</text>"

    at_bound = xlsx(comments(authors("Ada") + listed(comment(body(2)))))
    assert annotations(at_bound) == [("A1", "abab", None, "annotation")]

    pairs = [
        (
            comments(
                authors("Ada")
                + listed(comment(body(1), ref="A1"), comment(body(1), ref="B2"))
            ),
            "too many comments",
        ),
        (comments(authors("Ada", "Grace") + listed(comment(body(1)))), "too large"),
        (
            comments(authors("Ada") + listed(comment(body(3)))),
            "runs exceed the accepted bound",
        ),
        (
            comments(authors("Ada") + listed(comment("<text><t>abcde</t></text>"))),
            "text exceeds the accepted bound",
        ),
    ]
    for part, message in pairs:
        with pytest.raises(OOXMLInvalid, match=message):
            refused(part)

    # Per comment, not per part: two bodies on the bound are both accepted,
    # because the character bound is deliberately taken per comment, as the
    # constant's own comment states.
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENTS", 2)
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_RUNS", 20_000)
    split = xlsx(
        comments(
            authors("Ada")
            + listed(
                comment("<text><t>abcd</t></text>", ref="A1"),
                comment("<text><t>abcd</t></text>", ref="B2"),
            )
        )
    )
    assert [item[1] for item in annotations(split)] == ["abcd", "abcd"]

    # An over-long author name is clipped to the metadata bound, not refused.
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_AUTHORS", 10_000)
    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_COMMENT_CHARS", 100_000)
    long_name = xlsx(
        comments(authors("A" * 300) + listed(comment("<text><t>Hi</t></text>")))
    )
    assert locators(long_name)[0]["author"] == "A" * 256
