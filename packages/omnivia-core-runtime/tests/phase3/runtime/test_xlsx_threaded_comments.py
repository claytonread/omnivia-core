"""XLSX threaded ("modern") worksheet comments over minimal packages.

The grammar is MS-XLSX's own extension: a workbook-wide ``personList`` every
comment cites, and one ``ThreadedComments`` list per worksheet whose entries
carry an id, a person, an optional parent and -- on a thread's root only -- the
anchor the whole thread sits at.

Each identity the grammar carries is a GUID, each anchor a cell or a range, and
each free-text claim rides in the locator under the shared metadata bound. The
practical bounds -- records per list, characters per comment -- are pinned at
their limit and one past it.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# The two relationship types MS-XLSX defines for the extension's own parts, and
# the one namespace both of those parts are written in.
PERSON_REL = "http://schemas.microsoft.com/office/2017/10/relationships/person"
THREADED_REL = (
    "http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"
)
TC = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
# The namespace the classic comment list is written in, used here only as a
# namespace the extension's own names can be spelled in without qualifying.
SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PERSON_TYPE = "application/vnd.ms-excel.person+xml"
THREADED_TYPE = "application/vnd.ms-excel.threadedcomments+xml"
CLASSIC_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"
)

SHEET_PART = "xl/worksheets/sheet1.xml"
PERSONS_PART = "xl/persons/person.xml"
THREADED_PART = "xl/threadedComments/threadedComment1.xml"
CLASSIC_PART = "xl/comments1.xml"
# The targets each extension part is related by, each relative to the rels part
# holding it: the person list to the workbook's, the threads to the sheet's.
PERSON_TARGET = "persons/person.xml"
THREADED_TARGET = "../threadedComments/threadedComment1.xml"
# One written cell, so the sheet has grid content of its own beside the
# annotations and a thread can be seen not to have materialised one.
CELL = '<row r="1"><c r="A1" t="inlineStr"><is><t>Region</t></is></c></row>'


def guid(tail: int, *, stem: str = "0000ABCD") -> str:
    """One ``ST_Guid`` in the braced, hyphenated form the schema writes.

    ``stem`` varies the only part of a GUID whose case can differ -- its hex
    letters -- so two spellings of one identity can be written out.
    """
    return f"{{{stem}-0000-0000-0000-{tail:012d}}}"


def guids(count: int) -> Iterator[str]:
    """``count`` distinct ids, so a bound is pinned without a literal of them."""
    return (guid(tail) for tail in range(1, count + 1))


ADA = guid(1)
GRACE = guid(2)
ROOT = guid(11)
REPLY = guid(12)


def person(
    ident: str | None,
    *,
    name: str | None = "Ada",
    attrs: str = "",
    children: str = "",
) -> str:
    """One ``person``; ``children`` writes content below an element with none.

    An ``ident`` of ``None`` writes no ``id`` at all, which is a different claim
    from writing one that is malformed.
    """
    claimed = "" if ident is None else f' id="{ident}"'
    display = "" if name is None else f' displayName="{name}"'
    head = f"<person{claimed}{display}{attrs}"
    return f"{head}/>" if not children else f"{head}>{children}</person>"


def person_list(*records: str) -> str:
    return f'<personList xmlns="{TC}">{"".join(records)}</personList>'


def rooted(name: str, *, namespace: str | None = TC, body: str = "") -> str:
    """One part root written out, so its own name and namespace can be varied.

    A ``namespace`` of ``None`` declares none at all, which leaves the root
    unqualified rather than qualified somewhere else.
    """
    declared = "" if namespace is None else f' xmlns="{namespace}"'
    return f"<{name}{declared}>{body}</{name}>"


# One extension point carrying contents nothing here reads: a lookalike of each
# of the extension's own element names, spelled in another namespace and in
# none, so a reader descending into the point rather than leaving it alone is
# refusing them instead of accepting the point.
EXT = (
    f'<extLst xmlns="{TC}"><ext uri="urn:omnivia:invalid">'
    f'<person xmlns="{SML}" id="borrowed"/><threadedComment xmlns=""/>'
    "</ext></extLst>"
)


def threaded(
    ident: str | None,
    *,
    ref: str | None = "A1",
    person_id: str | None = ADA,
    parent: str | None = None,
    body: str | None = "<text>Hi</text>",
    attrs: str = "",
) -> str:
    """One ``threadedComment``; ``body`` of ``None`` writes no ``text`` at all.

    An ``ident`` or ``person_id`` of ``None`` writes that attribute out of the
    element altogether, as ``ref`` and ``parent`` already do.
    """
    anchor = "" if ref is None else f' ref="{ref}"'
    cited = "" if person_id is None else f' personId="{person_id}"'
    parented = "" if parent is None else f' parentId="{parent}"'
    claimed = "" if ident is None else f' id="{ident}"'
    return (
        f"<threadedComment{claimed}{anchor}{cited}{parented}{attrs}>"
        f'{body or ""}</threadedComment>'
    )


def thread(*entries: str) -> str:
    return f'<ThreadedComments xmlns="{TC}">{"".join(entries)}</ThreadedComments>'


ONE_PERSON = person_list(person(ADA))
ONE_THREAD = thread(threaded(ROOT))

# One comment's text, and the two spans the mentions below it claim. Both sit
# after a multi-byte character, so an offset counted in bytes cuts somewhere
# else: ``@Ada`` is character 8 and byte 9, ``@Grace`` character 15 and byte 18.
MENTION_TEXT = "Dès 9h: @Ada ⇄ @Grace"
MENTION_A = guid(21)
MENTION_B = guid(22)


def mention(
    ident: str | None = MENTION_A,
    *,
    person_id: str | None = ADA,
    start: str | None = "8",
    length: str | None = "4",
    attrs: str = "",
) -> str:
    """One ``mention``, spanning ``@Ada`` in ``MENTION_TEXT`` by default.

    MS-XLSX spells the person citation ``mentionpersonId`` -- a lower-case ``p``
    where the rest of the grammar capitalises -- so it is written out here
    exactly as the schema declares it rather than as its neighbours read.

    The two offsets are written as strings because a value outside
    ``xsd:unsignedInt`` has to be authorable at all; a ``None`` writes that
    attribute out of the element altogether.
    """
    claimed = "".join(
        f' {name}="{value}"'
        for name, value in (
            ("mentionId", ident),
            ("mentionpersonId", person_id),
            ("startIndex", start),
            ("length", length),
        )
        if value is not None
    )
    return f"<mention{claimed}{attrs}/>"


def mentions(*items: str) -> str:
    """The one ``mentions`` list a comment may carry, holding ``items``."""
    return f'<mentions>{"".join(items)}</mentions>'


# Both people the mentions below cite, one carrying the further claims a person
# record may make about itself and one carrying none of them.
TWO_PERSONS = person_list(
    person(ADA, attrs=' userId="ada@example.invalid" providerId="AD"'),
    person(GRACE, name="Grace"),
)


def relationship(ident: str, target: str, kind: str, *, external: bool = False) -> str:
    """One ``Relationship`` element, as the rels part under test writes it."""
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{ident}" Target="{target}" Type="{kind}"{mode}/>'


PERSON_LINK = relationship("rIdP", PERSON_TARGET, PERSON_REL)
THREADED_LINK = relationship("rIdT", THREADED_TARGET, THREADED_REL)


def xlsx(
    part: str | None = ONE_THREAD,
    *,
    persons: str | None = ONE_PERSON,
    classic: str | None = None,
    state: str | None = None,
    person_link: str = PERSON_LINK,
    threaded_link: str = THREADED_LINK,
    person_type: str | None = PERSON_TYPE,
    threaded_type: str | None = THREADED_TYPE,
) -> bytes:
    """Build a one-sheet package carrying the threaded parts under test.

    Every relationship, content-type override and member is derived from the
    parts passed, so only the declarations under test vary. ``classic`` adds the
    SpreadsheetML comment list a real threaded workbook carries beside its
    threads as a placeholder for readers that do not know the extension. The
    two ``_link`` arguments carry each extension part's relationship as written,
    and a ``_type`` of ``None`` declares no content type for it at all, so a
    route can be varied without varying the part it reaches.
    """
    attrs = f' state="{state}"' if state is not None else ""
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{kind}"/>'
        for name, kind, member in (
            (PERSONS_PART, person_type, persons),
            (THREADED_PART, threaded_type, part),
            (CLASSIC_PART, CLASSIC_TYPE, classic),
        )
        if member is not None and kind is not None
    )
    workbook_rels = '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>' + (
        person_link if persons is not None else ""
    )
    sheet_rels = (threaded_link if part is not None else "") + (
        f'<Relationship Id="rIdC" Target="../comments1.xml" Type="{RELNS}/comments"/>'
        if classic is not None
        else ""
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{overrides}</Types>")
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns:r="{RELNS}"><sheets>'
            f'<sheet name="Budget" sheetId="7" r:id="rId1"{attrs}/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels", f"<Relationships>{workbook_rels}</Relationships>"
        )
        archive.writestr(
            SHEET_PART,
            f'<worksheet xmlns="http://schemas.openxmlformats.org/'
            f'spreadsheetml/2006/main" xmlns:r="{RELNS}">'
            f"<sheetData>{CELL}</sheetData></worksheet>",
        )
        if sheet_rels:
            archive.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                f"<Relationships>{sheet_rels}</Relationships>",
            )
        for name, member in (
            (PERSONS_PART, persons),
            (THREADED_PART, part),
            (CLASSIC_PART, classic),
        ):
            if member is not None:
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


def test_threads_are_reported_in_source_order_with_exact_provenance() -> None:
    """The part's own order, each entry's own claims, nothing resolved or repaired.

    Four entries between them cover the shapes one list comes in: a thread root
    carrying text and the anchor, a reply that inherits that anchor and writes
    an empty text, a root at a position the sheet never wrote a cell at whose
    ``text`` is absent altogether, and a root whose person is the second of two
    the workbook declares.

    ``done`` is a tri-state claim rather than a boolean: a thread the file never
    marked is not the same claim as one the file marked unresolved, so an absent
    attribute stays absent rather than folding into ``false``.
    """
    content = xlsx(
        thread(
            threaded(ROOT, ref="B2", body="<text>second</text>", attrs=' dT="2026-01-02T03:04:05.06"'),
            threaded(REPLY, ref=None, parent=ROOT, person_id=GRACE, body="<text/>", attrs=' done="0"'),
            threaded(guid(13), ref="Z99", body="<mentions/>", attrs=' done="1"'),
            threaded(guid(14), ref="A1", person_id=GRACE),
        ),
        persons=person_list(
            person(ADA, name="Ada", attrs=' userId="ada@example.invalid" providerId="AD"'),
            person(GRACE, name="Grace"),
        ),
    )
    extraction = extract_xlsx(content, capture_id=CAPTURE)

    assert annotations(content) == [
        ("B2", "second", None, "annotation"),
        # The reply inherits its thread's anchor, and an empty text is the claim
        # the file made, carried as a value because the segment contract refuses
        # empty text.
        ("B2", None, {"text": ""}, "annotation"),
        # A sparse anchor, and no ``text`` element: the schema makes the element
        # optional, so its absence is the same empty claim rather than a refusal.
        ("Z99", None, {"text": ""}, "annotation"),
        ("A1", "Hi", None, "annotation"),
    ]
    # A thread materialises no cell, and none of these anchors gained one.
    assert [
        item.source_locator["cell"]
        for item in extraction.segments
        if item.semantic_role != "comment"
    ] == ["A1"]
    assert extraction.expected == 1
    assert {
        item.extraction_method
        for item in extraction.segments
        if item.semantic_role == "comment"
    } == {"direct"}
    # Absent, false, true, absent -- three distinct claims, not two.
    assert [item["done"] for item in locators(content)] == [None, False, True, None]
    assert [item["mentionsDeclared"] for item in locators(content)] == [
        False,
        False,
        True,
        False,
    ]
    # ``text`` is optional, so whether one was written is a claim of its own: an
    # empty element and no element at all carry the same empty text, and only
    # the record that wrote none says nothing.
    assert [item["textDeclared"] for item in locators(content)] == [
        True,
        True,
        False,
        True,
    ]
    # The reply in full: it holds the anchor it inherited, states that it wrote
    # no ``ref`` of its own, and names the parent it cited.
    assert locators(content)[1] == {
        "captureId": CAPTURE,
        "part": THREADED_PART,
        "relationshipId": "rIdT",
        "commentForm": "threaded",
        "commentIndex": 1,
        "cell": "B2",
        "ref": None,
        "commentId": REPLY,
        "parentCommentId": ROOT,
        "personId": GRACE,
        "personPart": PERSONS_PART,
        "personRelationshipId": "rIdP",
        "personDisplayName": "Grace",
        "personUserId": None,
        "personProviderId": None,
        "authorAuthenticated": False,
        "timestamp": None,
        "done": False,
        "mentionsDeclared": False,
        # A comment that declared no list mentions nobody, which is the same
        # empty list a declared and empty one carries.
        "mentions": [],
        "textDeclared": True,
        "sheetPart": SHEET_PART,
        "sheetId": "7",
        "sheet": "Budget",
        "sheetState": "visible",
    }
    # Every person field is the package's own claim about itself, carried as
    # written and marked as unauthenticated so no consumer reads it as identity.
    root = locators(content)[0]
    assert (
        root["personDisplayName"],
        root["personUserId"],
        root["personProviderId"],
        root["authorAuthenticated"],
    ) == ("Ada", "ada@example.invalid", "AD", False)
    # ``dT`` stays the string the package wrote, never a moment anything happened.
    assert root["timestamp"] == "2026-01-02T03:04:05.06"
    assert [item["commentIndex"] for item in locators(content)] == [0, 1, 2, 3]

    # A thread sits outside the grid's flow wherever the grid sits, so a hidden
    # sheet does not make its comments hidden content; the state is in the
    # locator for a consumer that needs it.
    hidden = xlsx(state="veryHidden")
    assert annotations(hidden) == [("A1", "Hi", None, "annotation")]
    assert locators(hidden)[0]["sheetState"] == "veryHidden"


def test_a_reply_written_before_its_parent_still_resolves() -> None:
    """A parent is a comment the part declares, not one it has already written.

    The schema orders nothing: ``parentId`` names a comment anywhere in the
    list, so a reply written first inherits the anchor from a root written
    after it. What stays the part's order is the report: the reply is reported
    first because that is where the file put it, not re-sorted into thread order.
    """
    content = xlsx(
        thread(
            threaded(REPLY, ref=None, parent=ROOT, body="<text>reply</text>"),
            threaded(ROOT, ref="B2", body="<text>root</text>"),
        )
    )

    assert annotations(content) == [
        ("B2", "reply", None, "annotation"),
        ("B2", "root", None, "annotation"),
    ]
    assert [(item["commentId"], item["parentCommentId"]) for item in locators(content)] == [
        (REPLY, ROOT),
        (ROOT, None),
    ]


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        # A comment parented to itself anchors nowhere: the chain it would
        # inherit from never reaches a root.
        pytest.param([threaded(ROOT, ref=None, parent=ROOT)], "XLSX threaded comment", id="self-parent"),
        pytest.param(
            [
                threaded(ROOT, ref=None, parent=REPLY),
                threaded(REPLY, ref=None, parent=ROOT),
            ],
            "XLSX threaded comment",
            id="cycle",
        ),
        pytest.param(
            [threaded(REPLY, ref=None, parent=guid(99))],
            "reply names no parent comment",
            id="missing-parent",
        ),
        pytest.param(
            [threaded(ROOT, ref="B2"), threaded(REPLY, ref="C3", parent=ROOT)],
            "contradicts its thread anchor",
            id="conflicting-anchor",
        ),
        # The bounds are what is compared, so a reply restating its thread's
        # anchor as an equivalent range agrees with it rather than contradicting.
        pytest.param(
            [threaded(ROOT, ref="B2"), threaded(REPLY, ref="B2:B2", parent=ROOT)],
            "",
            id="equivalent-anchor",
        ),
    ],
)
def test_parent_claims_that_anchor_nothing_are_refused(
    entries: list[str], message: str
) -> None:
    """A reply sitting somewhere other than its thread is a claim nothing can place.

    The self-parent and cycle cases are pinned only to the format's own refusal:
    which message they earn depends on how the parent chain is walked, and both
    are the same defect -- a reply whose chain never reaches an anchor. An empty
    ``message`` is an accepted case.
    """
    part = thread(*entries)
    if not message:
        assert [item[0] for item in annotations(xlsx(part))] == ["B2", "B2"]
        return
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(xlsx(part), capture_id=CAPTURE)


def test_the_classic_placeholder_and_the_thread_are_both_reported() -> None:
    """Each form is its own part on its own route, and each says which it is.

    A workbook carrying threads normally carries a classic comment beside each
    of them so readers that do not know the extension still see something. That
    placeholder is the classic part's own claim: it is reported as itself rather
    than deduplicated against the thread or standing in for it. Neither form
    materialises a cell, so the workbook's populated count is unchanged.
    """
    content = xlsx(
        classic=(
            '<comments xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">'
            "<authors><author>Ada</author></authors>"
            '<commentList><comment ref="A1" authorId="0"><text><t>'
            "Comment:\n    Hi</t></text></comment></commentList></comments>"
        )
    )
    extraction = extract_xlsx(content, capture_id=CAPTURE)

    assert [(item["commentForm"], item["part"]) for item in locators(content)] == [
        ("classic", CLASSIC_PART),
        ("threaded", THREADED_PART),
    ]
    assert [item[1] for item in annotations(content)] == ["Comment:\n    Hi", "Hi"]
    # Two annotations at one anchor, and the anchor is still a cell neither form
    # wrote: the populated count is the sheet's own content.
    assert [item[0] for item in annotations(content)] == ["A1", "A1"]
    assert extraction.expected == 1
    assert extraction.unit_type == "workbook_cell"


@pytest.mark.parametrize(
    ("records", "entries", "message"),
    [
        # ``displayName`` is required on a person: a record without one declares
        # no person at all, so the table is refused rather than indexed under a
        # name nothing wrote.
        pytest.param(
            [person(ADA, name=None)],
            [threaded(ROOT)],
            "person declares no display name",
            id="no-display-name",
        ),
        # Only a GUID's hex letters vary in case, so two records differing in
        # nothing else claim one identity and leave every citation ambiguous.
        pytest.param(
            [person(ADA), person(guid(1, stem="0000abcd"), name="Grace")],
            [threaded(ROOT)],
            "person id is repeated",
            id="duplicate-person-id",
        ),
        pytest.param(
            [person(ADA)],
            [threaded(ROOT, ref="B2"), threaded(guid(11, stem="0000abcd"), ref="C3")],
            "comment id is repeated",
            id="duplicate-comment-id",
        ),
        pytest.param(
            [person(ADA)],
            [threaded(ROOT, person_id=guid(99))],
            "names an undeclared person",
            id="undeclared-person",
        ),
        # Resolution is case-insensitive for the same reason, and resolves to the
        # record cited rather than to whichever sorts or matches by name first.
        pytest.param(
            [person(guid(3), name="Grace"), person(ADA)],
            [threaded(ROOT, person_id=guid(1, stem="0000abcd"))],
            "",
            id="cited-in-another-case",
        ),
    ],
)
def test_person_claims_and_repeated_ids_are_refused(
    records: list[str], entries: list[str], message: str
) -> None:
    """A person record and a comment id are identities, so neither is repaired.

    An empty ``message`` is an accepted case: the citation resolves to the
    record it names and the id it wrote is kept exactly as written, never
    recased to whatever the person list spelled.
    """
    content = xlsx(thread(*entries), persons=person_list(*records))
    if not message:
        assert locators(content)[0]["personDisplayName"] == "Ada"
        assert locators(content)[0]["personId"] == guid(1, stem="0000abcd")
        return
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("route", "message", "anchors"),
    [
        # Only the scheme and the host fold, so the capital the extension writes
        # in the path is part of the route rather than a spelling of it.
        pytest.param(
            {
                "person_link": relationship(
                    "rIdP", PERSON_TARGET, PERSON_REL.replace("person", "Person")
                )
            },
            "names an undeclared person",
            [],
            id="person-rel-recased-path",
        ),
        pytest.param(
            {
                "threaded_link": relationship(
                    "rIdT",
                    THREADED_TARGET,
                    THREADED_REL.replace("threadedComment", "threadedcomment"),
                )
            },
            "",
            [],
            id="threaded-rel-recased-path",
        ),
        # The host does fold, and folding it reaches the same part.
        pytest.param(
            {
                "person_link": relationship(
                    "rIdP",
                    PERSON_TARGET,
                    PERSON_REL.replace("schemas.microsoft.com", "SCHEMAS.Microsoft.COM"),
                )
            },
            "",
            ["A1"],
            id="person-rel-recased-host",
        ),
        # The relationship qualifies and the part is there, but it is declared
        # as something else: the classic comment type reaches no person list,
        # and the person type reaches no thread list.
        pytest.param(
            {"person_type": CLASSIC_TYPE},
            "person relationship does not name a person part",
            [],
            id="person-type-foreign",
        ),
        pytest.param(
            {"threaded_type": PERSON_TYPE},
            "threaded comments relationship does not name a threaded comments part",
            [],
            id="threaded-type-foreign",
        ),
        # A part has to be named outright, so a package declaring nothing for it
        # qualifies it as nothing -- the default for ``.xml`` says only that it
        # is XML.
        pytest.param(
            {"person_type": None},
            "person relationship does not name a person part",
            [],
            id="person-type-absent",
        ),
        pytest.param(
            {"threaded_type": None},
            "threaded comments relationship does not name a threaded comments part",
            [],
            id="threaded-type-absent",
        ),
        # An external target is a reference out of the package. It is refused
        # rather than resolved against the member of that name that is in it.
        pytest.param(
            {"person_link": relationship("rIdP", PERSON_TARGET, PERSON_REL, external=True)},
            "XLSX person relationship is external",
            [],
            id="person-external",
        ),
        pytest.param(
            {
                "threaded_link": relationship(
                    "rIdT", THREADED_TARGET, THREADED_REL, external=True
                )
            },
            "XLSX threaded comments relationship is external",
            [],
            id="threaded-external",
        ),
        # Two qualifying relationships leave which part is meant ambiguous, even
        # where both name the same one.
        pytest.param(
            {
                "person_link": PERSON_LINK
                + relationship("rIdP2", PERSON_TARGET, PERSON_REL)
            },
            "XLSX workbook names more than one person relationship",
            [],
            id="person-duplicate",
        ),
        pytest.param(
            {
                "threaded_link": THREADED_LINK
                + relationship("rIdT2", THREADED_TARGET, THREADED_REL)
            },
            "XLSX worksheet names more than one threaded comments relationship",
            [],
            id="threaded-duplicate",
        ),
    ],
)
def test_each_extension_part_is_reached_only_by_its_own_route(
    route: dict[str, Any], message: str, anchors: list[str]
) -> None:
    """Each part is reached by its own relationship type from its own source.

    The person list hangs off the workbook and the threads off the worksheet,
    each by the one URI MS-XLSX defines for it and each resolving to a part the
    package declares with that type's own content type. A route that misses any
    of that reaches nothing: a thread list nothing reaches reports no comments
    at all, and a person list nothing reaches leaves every citation naming a
    person the workbook never declared. An empty ``message`` is an accepted
    case, and ``anchors`` the comment anchors it then reports.
    """
    content = xlsx(**route)
    if message:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    assert [item[0] for item in annotations(content)] == anchors


@pytest.mark.parametrize(
    ("package", "message"),
    [
        # Each part says what it is at its own root, in the one namespace the
        # extension defines. An unqualified root declares nothing, and a root
        # qualified elsewhere -- the namespace the classic list is written in
        # among them -- declares something else.
        pytest.param(
            {"persons": rooted("personList", namespace=None, body=person(ADA))},
            "part uses an unsupported namespace",
            id="person-root-unqualified",
        ),
        pytest.param(
            {"persons": rooted("personList", namespace=SML, body=person(ADA))},
            "part uses an unsupported namespace",
            id="person-root-foreign-namespace",
        ),
        pytest.param(
            {"part": rooted("ThreadedComments", namespace=None, body=threaded(ROOT))},
            "part uses an unsupported namespace",
            id="threaded-root-unqualified",
        ),
        pytest.param(
            {"part": rooted("ThreadedComments", namespace=SML, body=threaded(ROOT))},
            "part uses an unsupported namespace",
            id="threaded-root-foreign-namespace",
        ),
        # The root name is the name the schema writes, so its case is part of it
        # and the list's own capital is not a spelling of it.
        pytest.param(
            {"persons": rooted("personlist", body=person(ADA))},
            "is not rooted at its own list",
            id="person-root-recased",
        ),
        pytest.param(
            {"part": rooted("threadedComments", body=threaded(ROOT))},
            "is not rooted at its own list",
            id="threaded-root-recased",
        ),
        # Below each root only that root's own record is a record. The names are
        # held to the part's namespace, so a record left unqualified by the
        # declaration it carries, or qualified somewhere else, is not one.
        pytest.param(
            {"persons": person_list(person(ADA, attrs=' xmlns=""'))},
            "person list holds an unsupported element",
            id="person-unqualified",
        ),
        pytest.param(
            {"persons": person_list(person(ADA, attrs=f' xmlns="{SML}"'))},
            "person list holds an unsupported element",
            id="person-foreign-namespace",
        ),
        # Qualified, and the other part's record: each list holds its own.
        pytest.param(
            {"persons": person_list(person(ADA), threaded(REPLY))},
            "person list holds an unsupported element",
            id="person-list-holds-a-comment",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, attrs=' xmlns=""'))},
            "threaded comment list holds an unsupported element",
            id="threaded-comment-unqualified",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT), person(GRACE, name="Grace"))},
            "threaded comment list holds an unsupported element",
            id="threaded-list-holds-a-person",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT), f'<comment xmlns="{SML}" ref="A1"/>')},
            "threaded comment list holds an unsupported element",
            id="threaded-list-holds-a-foreign-element",
        ),
        # A person is its attributes and nothing else: the schema gives it no
        # content but an extension point, so anything below it is unsupported
        # and a person below a person declares no second person.
        pytest.param(
            {"persons": person_list(person(ADA, children=f'<text xmlns="{TC}">x</text>'))},
            "XLSX person holds an unsupported element",
            id="person-holds-a-text",
        ),
        pytest.param(
            {"persons": person_list(person(ADA, children=person(GRACE, name="Grace")))},
            "XLSX person holds an unsupported element",
            id="person-holds-a-person",
        ),
        # The extension point is qualified like everything else, so a lookalike
        # left unqualified is an unsupported element rather than a point.
        pytest.param(
            {"persons": person_list(person(ADA, children='<extLst xmlns=""/>'))},
            "XLSX person holds an unsupported element",
            id="person-holds-an-unqualified-extension-list",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, body='<text>Hi</text><extLst xmlns=""/>'))},
            "XLSX threaded comment holds an unsupported element",
            id="threaded-comment-holds-an-unqualified-extension-list",
        ),
        # One extension point per element. A second one leaves two sets of
        # extensions at one position and nothing here picks between them.
        pytest.param(
            {"persons": person_list(person(ADA), EXT, EXT)},
            "more than one extension list",
            id="person-list-holds-two-extension-lists",
        ),
        pytest.param(
            {"persons": person_list(person(ADA, children=EXT + EXT))},
            "more than one extension list",
            id="person-holds-two-extension-lists",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT), EXT, EXT)},
            "more than one extension list",
            id="threaded-list-holds-two-extension-lists",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, body=f"<text>Hi</text>{EXT}{EXT}"))},
            "more than one extension list",
            id="threaded-comment-holds-two-extension-lists",
        ),
        # Mentions are one list per comment for the same reason, and the one is
        # read only where the schema places it.
        pytest.param(
            {"part": thread(threaded(ROOT, body="<text>Hi</text><mentions/><mentions/>"))},
            "mention list",
            id="threaded-comment-holds-two-mention-lists",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, body='<text>Hi</text><mentions xmlns=""/>'))},
            "XLSX threaded comment holds an unsupported element",
            id="threaded-comment-holds-unqualified-mentions",
        ),
        # The accepted shape: one extension point at each position that has one,
        # carrying contents nothing here reads, and the comment still reported.
        pytest.param(
            {"persons": person_list(person(ADA, children=EXT), EXT)},
            "",
            id="one-extension-list-per-person-position",
        ),
        pytest.param(
            {
                "part": thread(
                    threaded(ROOT, body=f"<text>Hi</text><mentions/>{EXT}"), EXT
                )
            },
            "",
            id="one-extension-list-per-comment-position",
        ),
    ],
)
def test_the_extension_parts_hold_only_the_shape_their_schema_declares(
    package: dict[str, Any], message: str
) -> None:
    """Each part is its own vocabulary, held to its own namespace throughout.

    The declared content type says what a part is; the root is where the part
    says so itself, and every name below it is held to that same namespace. So
    the structural positions -- a record in either list, a comment's text,
    mentions and extension point, a person's extension point -- admit only the
    qualified names the schema puts there. A lookalike spelled in another
    namespace or in none declares nothing, and neither does the other part's
    record: nothing is read for whatever elements happen to be in it.

    The one extension point the schema gives each of those elements is accepted
    and left alone, which is what makes a second one a refusal rather than more
    of the same: two sets of extensions at one position are a claim nothing here
    picks between. An empty ``message`` is an accepted case, whose comment is
    still reported at its anchor.
    """
    content = xlsx(**package)
    if not message:
        assert [item[0] for item in annotations(content)] == ["A1"]
        return
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("package", "message"),
    [
        # Every identity the grammar carries is an ``ST_Guid``, and each is
        # required where the schema writes it: absent is one claim, written and
        # unreadable another. One reader validates the type at every position,
        # and each refusal names the attribute it was reading.
        pytest.param(
            {"persons": person_list(person(None))},
            "XLSX person declares no id",
            id="person-id-absent",
        ),
        pytest.param(
            {"persons": person_list(person(ADA.strip("{}")))},
            "XLSX person id is malformed",
            id="person-id-unbraced",
        ),
        pytest.param(
            {"part": thread(threaded(None))},
            "XLSX threaded comment declares no id",
            id="comment-id-absent",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT[:-1]))},
            "XLSX threaded comment id is malformed",
            id="comment-id-unterminated",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, person_id=None))},
            "XLSX threaded comment names no person",
            id="person-citation-absent",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, person_id="{not-a-guid}"))},
            "XLSX threaded comment person citation is malformed",
            id="person-citation-not-hex",
        ),
        # A comment writing no ``parentId`` is a thread root, and a root anchors
        # itself: with no parent to inherit from and no ``ref`` of its own it
        # says nowhere at all.
        pytest.param(
            {"part": thread(threaded(ROOT, ref=None))},
            "XLSX threaded comment declares no reference",
            id="parent-absent-and-no-reference",
        ),
        pytest.param(
            {"part": thread(threaded(REPLY, ref=None, parent=ROOT + "0"))},
            "XLSX threaded comment parent id is malformed",
            id="parent-overlong",
        ),
        # ``done`` is a closed type, so its lexical space is the one the schema
        # writes: a value outside it is refused rather than read as whichever of
        # the two flags it resembles, and an empty value is not an absent one.
        pytest.param(
            {"part": thread(threaded(ROOT, attrs=' done="TRUE"'))},
            "XLSX threaded comment resolution mark is malformed",
            id="done-recased",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, attrs=' done="yes"'))},
            "XLSX threaded comment resolution mark is malformed",
            id="done-foreign-word",
        ),
        # A root's anchor is a coordinate or a range, validated before it is
        # kept: a transposed coordinate names no cell, and a range whose end
        # precedes its start describes no rectangle.
        pytest.param(
            {"part": thread(threaded(ROOT, ref="1A"))},
            "XLSX cell coordinate is malformed",
            id="reference-transposed",
        ),
        pytest.param(
            {"part": thread(threaded(ROOT, ref="B2:A1"))},
            "XLSX cell range is malformed",
            id="reference-range-inverted",
        ),
    ],
)
def test_the_required_identities_marks_and_anchors_are_refused_unreadable(
    package: dict[str, Any], message: str
) -> None:
    """Each attribute the grammar requires is refused absent and unreadable.

    None of these is repaired: an absent id is not stood in for by position, an
    unreadable GUID is not renormalised into the one it resembles, a ``done``
    outside its type does not fold into a flag, and an anchor that names no
    rectangle is not reordered into the one it was probably meant to be.
    """
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(xlsx(**package), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("count", "message"),
    [
        pytest.param(10_000, "", id="at-the-bound"),
        pytest.param(10_001, "person list is too large", id="one-past-the-bound"),
    ],
)
def test_the_person_list_is_bounded_at_ten_thousand_records(
    count: int, message: str
) -> None:
    """The workbook's person table is bounded at the records it may declare.

    Each record is a distinct, valid person, so the bound is the only thing the
    accepted case sits against. The comment cites the last person the list
    declares rather than the first, so the accepted case is decided by that
    record having been indexed rather than by the table merely parsing.
    """
    records = [
        person(ident, name=f"P{index}") for index, ident in enumerate(guids(count))
    ]
    content = xlsx(
        thread(threaded(ROOT, person_id=guid(count))), persons=person_list(*records)
    )
    if not message:
        assert locators(content)[0]["personDisplayName"] == f"P{count - 1}"
        return
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("count", "message"),
    [
        pytest.param(10_000, "", id="at-the-bound"),
        pytest.param(10_001, "too many threaded comments", id="one-past-the-bound"),
    ],
)
def test_one_worksheets_thread_list_is_bounded_at_ten_thousand_comments(
    count: int, message: str
) -> None:
    """One worksheet's thread list is bounded at the comments it may declare.

    Every entry is a thread root carrying its own id and anchor and citing the
    person the workbook declares, so nothing but the count distinguishes the
    accepted case from the refused one. The accepted case is decided by the last
    entry: it is reported, at the position and under the id the part wrote.
    """
    content = xlsx(thread(*(threaded(ident) for ident in guids(count))))
    if message:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    records = locators(content)
    assert len(records) == count
    assert (
        records[-1]["commentId"],
        records[-1]["commentIndex"],
        records[-1]["cell"],
    ) == (guid(count), count - 1, "A1")


@pytest.mark.parametrize(
    ("length", "message"),
    [
        pytest.param(100_000, "", id="at-the-bound"),
        pytest.param(
            100_001, "text exceeds the accepted bound", id="one-past-the-bound"
        ),
    ],
)
def test_one_threaded_comments_text_is_bounded_at_a_hundred_thousand_characters(
    length: int, message: str
) -> None:
    """A threaded comment's text is bounded at the characters it may carry.

    The bound is one comment's own text, and the text is a plain string rather
    than the rich text a classic comment holds, so there are no runs under it to
    spread the length across. The accepted case carries its whole text: nothing
    is clipped on the way into the segment.
    """
    content = xlsx(thread(threaded(ROOT, body=f"<text>{'x' * length}</text>")))
    if message:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    assert annotations(content) == [("A1", "x" * length, None, "annotation")]


def test_a_comments_mentions_are_reported_in_source_order_with_their_spans() -> None:
    """A mention is a span of the comment's own text and a person it cites.

    It carries no text of its own: it names a position and a length in the
    ``text`` beside it, so the slice is the file's own claim about what was
    mentioned. The span is in characters rather than bytes, and both offsets
    here sit after a multi-byte character, so a reader counting bytes cuts
    somewhere else.

    The list is reported where the file wrote it: these two are written with the
    later span first, so an order recovered from the offsets would reverse them.
    The person each cites resolves through the workbook's own list and arrives
    carrying that record's claims about itself, marked unauthenticated exactly
    as a comment's own author claims are.

    ``mentionsDeclared`` stays the claim it was: a list declared empty carries
    no mentions, which is not the claim a comment declaring no list makes.
    """
    content = xlsx(
        thread(
            threaded(
                ROOT,
                body=f"<text>{MENTION_TEXT}</text>"
                + mentions(
                    mention(MENTION_B, person_id=GRACE, start="15", length="6"),
                    mention(MENTION_A, person_id=ADA, start="8", length="4"),
                ),
            ),
            threaded(REPLY, ref=None, parent=ROOT, body="<text>ok</text><mentions/>"),
        ),
        persons=TWO_PERSONS,
    )

    # A mention annotates the text rather than adding to it, so the segment is
    # the comment's own text either way.
    assert annotations(content) == [
        ("A1", MENTION_TEXT, None, "annotation"),
        ("A1", "ok", None, "annotation"),
    ]
    # The same offset read over the encoded text is neither of the slices below.
    assert (MENTION_TEXT[8:12], MENTION_TEXT.encode()[8:12]) == ("@Ada", b" @Ad")
    assert locators(content)[0]["mentions"] == [
        {
            "mentionId": MENTION_B,
            "mentionPersonId": GRACE,
            "startIndex": 15,
            "length": 6,
            "text": "@Grace",
            "personPart": PERSONS_PART,
            "personRelationshipId": "rIdP",
            "personDisplayName": "Grace",
            "personUserId": None,
            "personProviderId": None,
            "authorAuthenticated": False,
        },
        {
            "mentionId": MENTION_A,
            "mentionPersonId": ADA,
            "startIndex": 8,
            "length": 4,
            "text": "@Ada",
            "personPart": PERSONS_PART,
            "personRelationshipId": "rIdP",
            "personDisplayName": "Ada",
            "personUserId": "ada@example.invalid",
            "personProviderId": "AD",
            "authorAuthenticated": False,
        },
    ]
    assert [item["mentionsDeclared"] for item in locators(content)] == [True, True]
    assert locators(content)[1]["mentions"] == []


@pytest.mark.parametrize(
    ("holds", "message"),
    [
        pytest.param(
            mentions(mention(None)), r"mention declares no .*id", id="id-absent"
        ),
        pytest.param(
            mentions(mention(MENTION_A.strip("{}"))),
            r"mention id is malformed",
            id="id-unbraced",
        ),
        pytest.param(
            mentions(mention(person_id=None)),
            r"mention names no person",
            id="person-absent",
        ),
        pytest.param(
            mentions(mention(person_id="{not-a-guid}")),
            r"mention.*person.*malformed",
            id="person-not-hex",
        ),
        pytest.param(
            mentions(mention(person_id=guid(99))),
            r"undeclared person",
            id="person-undeclared",
        ),
        pytest.param(
            mentions(mention(start=None)),
            r"mention declares no start index",
            id="start-absent",
        ),
        # Outside the lexical space of the type, below its value space and past
        # the top of it: each is refused rather than read as the nearest offset
        # inside it.
        pytest.param(
            mentions(mention(start="eight")), r"mention start index", id="start-word"
        ),
        pytest.param(
            mentions(mention(start="-1")), r"mention start index", id="start-negative"
        ),
        pytest.param(
            mentions(mention(start=str(2**32))),
            r"mention start index",
            id="start-past-uint32",
        ),
        pytest.param(
            mentions(mention(length=None)),
            r"mention declares no length",
            id="length-absent",
        ),
        pytest.param(
            mentions(mention(length="four")), r"mention length", id="length-word"
        ),
        pytest.param(
            mentions(mention(length="-4")), r"mention length", id="length-negative"
        ),
        pytest.param(
            mentions(mention(length=str(2**32))),
            r"mention length",
            id="length-past-uint32",
        ),
        # The start is inside the text and the length is readable, and together
        # they run two characters past its end.
        pytest.param(
            mentions(mention(start="18", length="6")), r"mention", id="span-past-text"
        ),
        pytest.param(
            mentions(
                mention(MENTION_A, start="8", length="4"),
                mention(
                    guid(21, stem="0000abcd"), person_id=GRACE, start="15", length="6"
                ),
            ),
            r"mention id is repeated",
            id="duplicate-mention-id",
        ),
        # Below ``mentions`` only a qualified ``mention`` is a mention: the
        # part's own names spelled elsewhere in it declare nothing, and neither
        # does a lookalike left unqualified by the declaration it carries.
        pytest.param(
            mentions("<text>Ada</text>"),
            r"holds an unsupported element",
            id="mentions-holds-a-text",
        ),
        pytest.param(
            mentions(mention(attrs=' xmlns=""')),
            r"holds an unsupported element",
            id="mention-unqualified",
        ),
    ],
)
def test_a_mention_is_refused_unless_it_names_a_person_and_spans_the_text(
    holds: str, message: str
) -> None:
    """A mention is two identities and a span, and every one of them is required.

    Each identity is an ``ST_Guid`` -- absent is one claim, written and
    unreadable another -- and the person a mention cites has to be one the
    workbook declared, as a comment's own author does. Each offset is an
    ``xsd:unsignedInt``, so a value the type does not carry is refused rather
    than clamped into one it does, and a span running past the end of the text
    slices nothing rather than slicing what is left.

    A mention id is the identity it claims, so a repeat of one is refused for
    the reason the grammar's other ids are: only a GUID's hex letters vary in
    case, so two mentions differing in nothing else claim one identity.

    What each case pins is the refusal and which claim it is about, not the
    wording it is phrased in.
    """
    content = xlsx(
        thread(threaded(ROOT, body=f"<text>{MENTION_TEXT}</text>{holds}")),
        persons=TWO_PERSONS,
    )
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


def test_a_mentions_slice_is_reported_whole_rather_than_clipped() -> None:
    """A mention's slice is the span the file claimed, at the length it claimed.

    The slice is evidence of what was mentioned, so a span longer than the
    bound other free-text claims are clipped at is still reported whole: a
    shortened slice would be a different claim about the same offsets.
    """
    text = "." * 50 + "m" * 300 + "." * 50
    content = xlsx(
        thread(
            threaded(
                ROOT,
                body=f"<text>{text}</text>"
                + mentions(mention(start="50", length="300")),
            )
        )
    )

    reported = locators(content)[0]["mentions"]
    assert isinstance(reported, list)
    assert (reported[0]["length"], reported[0]["text"]) == (300, "m" * 300)


def test_overlapping_mention_spans_cannot_amplify_past_the_text_bound() -> None:
    """The slices one comment emits are bounded as its own text is.

    Each span is sliced out of the text again, so a list of them can emit far
    more than the comment holds; overlapping spans count independently because
    each one is its own output. These two are valid separately -- each sits
    inside a comment at the bound -- and together they would emit 120,000
    characters, which is past what one comment may carry.
    """
    content = xlsx(
        thread(
            threaded(
                ROOT,
                body=f"<text>{'x' * 100_000}</text>"
                + mentions(
                    mention(MENTION_A, start="0", length="60000"),
                    mention(MENTION_B, start="30000", length="60000"),
                ),
            )
        )
    )

    with pytest.raises(OOXMLInvalid, match="mention spans exceed the accepted bound"):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("count", "message"),
    [
        pytest.param(10_000, "", id="at-the-bound"),
        pytest.param(10_001, "too many mentions", id="one-past-the-bound"),
    ],
)
def test_one_comments_mention_list_is_bounded_at_ten_thousand_records(
    count: int, message: str
) -> None:
    """One comment's mention list is bounded at the records it may declare.

    Every mention carries its own id, cites the one person the workbook
    declares and spans nothing at the start of the text, so the slices sum to
    zero and the bound on what a list may emit cannot pre-empt the bound on how
    many records it may hold. The accepted case is decided by the last record:
    it is reported, under the id and at the offsets the part wrote.
    """
    content = xlsx(
        thread(
            threaded(
                ROOT,
                body="<text>Hi</text>"
                + mentions(
                    *(mention(ident, start="0", length="0") for ident in guids(count))
                ),
            )
        )
    )
    if message:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    records = locators(content)[0]["mentions"]
    assert len(records) == count
    assert (
        records[-1]["mentionId"],
        records[-1]["mentionPersonId"],
        records[-1]["startIndex"],
        records[-1]["length"],
        records[-1]["text"],
    ) == (guid(count), ADA, 0, 0, "")
