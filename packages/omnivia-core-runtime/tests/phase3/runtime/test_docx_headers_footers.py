"""DOCX headers and footers: the sections that reach them, and the parts they are.

A header or a footer is never found by its name. Each one is reached through a
section of the document, the reference that section writes, the relationship
that reference names and the content type the package declares for the part it
resolves to, and the part has to be a header or a footer at its root. Every
test here leaves that route intact and changes exactly one step of it.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    _MAX_DOCX_SECTIONS,
    OOXMLInvalid,
    extract_docx,
)
from omnivia_core_runtime.storage.document_intake import SEGMENT_REVISIONS

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/wordprocessingml/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
OTHER = "urn:elsewhere"
CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
HEADER_CT = f"{CT}header+xml"
FOOTER_CT = f"{CT}footer+xml"
# A member that is named, declared and related like a header and is not parsable
# XML at all. Wherever one of these sits in a package below, reading it would
# raise, so a package that extracts cleanly proves the member was never read.
MALFORMED = "<w:hdr><w:p>"


def package(
    document_xml: str,
    relationships: str,
    content_types: str,
    parts: dict[str, str],
) -> bytes:
    """One package, with the main document's relationships and the parts given.

    Every package carries a default content type for ``.xml``: it says what each
    member is in general, which qualifies no part as a header or a footer.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f"{content_types}</Types>",
        )
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def rels(*items: tuple[str, str, str]) -> str:
    """Relationships as (id, target, type), the type a bare name or a full URI.

    A target carrying a URI scheme is written as the external reference it is.
    """
    return (
        "<Relationships>"
        + "".join(
            f'<Relationship Id="{rid}" '
            f'Type="{kind if "://" in kind else f"{RELS}/{kind}"}" '
            f'Target="{target}"'
            + (' TargetMode="External"' if "://" in target else "")
            + "/>"
            for rid, target, kind in items
        )
        + "</Relationships>"
    )


def override(part: str, content_type: str) -> str:
    return f'<Override PartName="/{part}" ContentType="{content_type}"/>'


def document(body: str, namespace: str = MAIN, relations: str = RELS) -> str:
    return (
        f'<w:document xmlns:w="{namespace}" xmlns:r="{relations}" '
        f'xmlns:w2="{STRICT_MAIN}" xmlns:r2="{STRICT_RELS}" xmlns:x="{OTHER}">'
        f"<w:body>{body}</w:body></w:document>"
    )


def story(root: str, text: str = "furniture", namespace: str | None = MAIN) -> str:
    opened = f'<{root} xmlns="{namespace}">' if namespace else f"<{root}>"
    return f"{opened}<p><r><t>{text}</t></r></p></{root}>"


def para(text: str = "prose", properties: str = "") -> str:
    return (
        f"<w:p>{f'<w:pPr>{properties}</w:pPr>' if properties else ''}"
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def sect(*references: str) -> str:
    return f"<w:sectPr>{''.join(references)}</w:sectPr>"


def reference(kind_of: str, kind: str = "default", rid: str = "rId1") -> str:
    return f'<w:{kind_of}Reference w:type="{kind}" r:id="{rid}"/>'


def record(section: int, kind: str, rid: str, inherited: bool) -> dict[str, object]:
    return {
        "section": section,
        "kind": kind,
        "relationshipId": rid,
        "inherited": inherited,
    }


def furniture(name: str, *records: dict[str, object]) -> dict[str, object]:
    return {"kind": "headerFooter", "story": name, "sections": records}


def read(content: bytes) -> list[tuple[str, str, str | None]]:
    return [
        (item.source_locator["part"], item.source_locator["story"], item.text)
        for item in extract_docx(content, capture_id=CAPTURE).segments
    ]


def applies(content: bytes) -> list[tuple[str, object]]:
    return [
        (item.source_locator["part"], item.structured_value)
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


HEADER_RELATION = ("rId1", "header1.xml", "header")
HEADER_OVERRIDE = override("word/header1.xml", HEADER_CT)
HEADER_SECTION = sect(reference("header"))


def furnished(
    body: str = HEADER_SECTION,
    relations: tuple[tuple[str, str, str], ...] = (HEADER_RELATION,),
    content_types: str = HEADER_OVERRIDE,
    parts: dict[str, str] | None = None,
) -> bytes:
    """The one default-header section, over the package given around it."""
    return package(
        document(body),
        rels(*relations),
        content_types,
        {"word/header1.xml": story("hdr", "top"), **(parts or {})},
    )


def test_one_section_reaches_its_own_default_header_and_footer() -> None:
    content = package(
        document(para() + sect(reference("header"), reference("footer", rid="rId2"))),
        rels(HEADER_RELATION, ("rId2", "footer1.xml", "footer")),
        HEADER_OVERRIDE + override("word/footer1.xml", FOOTER_CT),
        {
            "word/header1.xml": story("hdr", "top"),
            "word/footer1.xml": story("ftr", "end"),
        },
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert read(content) == [
        ("word/document.xml", "body", "prose"),
        # Each part's applicability precedes the prose it stands for.
        ("word/footer1.xml", "footer", None),
        ("word/footer1.xml", "footer", "end"),
        ("word/header1.xml", "header", None),
        ("word/header1.xml", "header", "top"),
    ]
    assert applies(content) == [
        ("word/footer1.xml", furniture("footer", record(0, "default", "rId2", False))),
        ("word/header1.xml", furniture("header", record(0, "default", "rId1", False))),
    ]
    assert result.limitations["documentSections"] == 1
    assert result.expected == 3


def test_an_applicability_record_stands_in_an_admitted_revision_state() -> None:
    """Where a part appears is not prose an author inserted or deleted.

    The record therefore stands unrevised, in one of the four states
    ``SEGMENT_REVISIONS`` closes publication over. A record standing in
    anything else is refused there, and every header and footer of the document
    goes with it.
    """
    result = extract_docx(furnished(), capture_id=CAPTURE)

    (applicability,) = [
        i for i in result.segments if i.semantic_role == "object_inventory"
    ]
    assert applicability.structured_value is not None
    assert applicability.structured_value["kind"] == "headerFooter"
    assert applicability.revision_state == "current"
    assert {i.revision_state for i in result.segments} <= SEGMENT_REVISIONS


def test_a_section_places_each_of_the_three_kinds_it_declares() -> None:
    """``ST_HdrFtr`` is exactly three kinds, and one section may place all of
    them: each is its own part, and none of them inherits anything."""
    content = package(
        document(
            para()
            + sect(
                reference("header"),
                reference("header", "first", "rId3"),
                reference("header", "even", "rId2"),
            )
        ),
        rels(
            HEADER_RELATION,
            ("rId2", "header2.xml", "header"),
            ("rId3", "header3.xml", "header"),
        ),
        "".join(
            override(f"word/header{index}.xml", HEADER_CT) for index in (1, 2, 3)
        ),
        {
            "word/header1.xml": story("hdr", "top"),
            "word/header2.xml": story("hdr", "even"),
            "word/header3.xml": story("hdr", "first"),
        },
    )

    assert applies(content) == [
        ("word/header1.xml", furniture("header", record(0, "default", "rId1", False))),
        ("word/header2.xml", furniture("header", record(0, "even", "rId2", False))),
        ("word/header3.xml", furniture("header", record(0, "first", "rId3", False))),
    ]


def test_a_later_section_inherits_what_it_declares_nothing_for() -> None:
    """The second section declares a header of its own and no footer, so the
    header it names replaces the first section's and the footer carries over as
    the same relationship to the same part, flagged as inherited."""
    content = package(
        document(
            para("a", sect(reference("header"), reference("footer", rid="rId3")))
            + para("b")
            + sect(reference("header", rid="rId2"))
        ),
        rels(
            HEADER_RELATION,
            ("rId2", "header2.xml", "header"),
            ("rId3", "footer1.xml", "footer"),
        ),
        HEADER_OVERRIDE
        + override("word/header2.xml", HEADER_CT)
        + override("word/footer1.xml", FOOTER_CT),
        {
            "word/header1.xml": story("hdr", "one"),
            "word/header2.xml": story("hdr", "two"),
            "word/footer1.xml": story("ftr", "both"),
        },
    )

    assert applies(content) == [
        (
            "word/footer1.xml",
            furniture(
                "footer",
                record(0, "default", "rId3", False),
                record(1, "default", "rId3", True),
            ),
        ),
        ("word/header1.xml", furniture("header", record(0, "default", "rId1", False))),
        ("word/header2.xml", furniture("header", record(1, "default", "rId2", False))),
    ]
    assert extract_docx(content, capture_id=CAPTURE).limitations["documentSections"] == 2


def test_a_part_several_sections_share_is_read_once_and_carries_them_all() -> None:
    """One part reached twice, as two kinds and through two relationships, is
    still one part: its prose appears once and its applicability says every
    place it stands for."""
    content = package(
        document(
            para("a", sect(reference("header")))
            + para("b")
            + sect(reference("header", "first", "rId2"))
        ),
        rels(HEADER_RELATION, ("rId2", "header1.xml", "header")),
        HEADER_OVERRIDE,
        {"word/header1.xml": story("hdr", "top")},
    )

    assert read(content) == [
        ("word/document.xml", "body", "a"),
        ("word/document.xml", "body", "b"),
        ("word/header1.xml", "header", None),
        ("word/header1.xml", "header", "top"),
    ]
    assert applies(content) == [
        (
            "word/header1.xml",
            furniture(
                "header",
                record(0, "default", "rId1", False),
                record(1, "default", "rId1", True),
                record(1, "first", "rId2", False),
            ),
        )
    ]


def test_sections_are_numbered_in_source_order_across_both_routes() -> None:
    """A paragraph's own ``sectPr`` ends the section it sits in and the body's
    trailing one describes the section after it, so the paragraph break is
    section 0 however the parts it reaches happen to be named."""
    content = package(
        document(
            para("a", sect(reference("header", rid="rId2")))
            + para("b")
            + sect(reference("header"))
        ),
        rels(HEADER_RELATION, ("rId2", "header2.xml", "header")),
        HEADER_OVERRIDE + override("word/header2.xml", HEADER_CT),
        {
            "word/header1.xml": story("hdr", "one"),
            "word/header2.xml": story("hdr", "two"),
        },
    )

    assert applies(content) == [
        ("word/header1.xml", furniture("header", record(1, "default", "rId1", False))),
        ("word/header2.xml", furniture("header", record(0, "default", "rId2", False))),
    ]


def test_a_member_no_section_reaches_is_never_parsed() -> None:
    """Three members named, declared and related exactly as headers and footers
    are, none of them referenced by any section. Each is malformed XML, so a
    package that reads cleanly is the proof that nothing opened them."""
    content = furnished(
        para() + HEADER_SECTION,
        (
            HEADER_RELATION,
            ("rId2", "header2.xml", "header"),
            ("rId3", "footer1.xml", "footer"),
        ),
        HEADER_OVERRIDE
        + override("word/header2.xml", HEADER_CT)
        + override("word/footer1.xml", FOOTER_CT),
        {
            "word/header2.xml": MALFORMED,
            "word/footer1.xml": MALFORMED,
            "word/header9.xml": MALFORMED,
        },
    )

    assert read(content) == [
        ("word/document.xml", "body", "prose"),
        ("word/header1.xml", "header", None),
        ("word/header1.xml", "header", "top"),
    ]


def test_a_strict_section_reaches_a_strict_header() -> None:
    """Every step written in the Strict profile -- the section, the reference,
    its ``type``, its relationship namespace, the relationship's own type URI
    and the part's root -- is the same route as the Transitional one."""
    content = package(
        document(
            para() + sect(reference("header", "first")), STRICT_MAIN, STRICT_RELS
        ),
        rels(("rId1", "header1.xml", f"{STRICT_RELS}/header")),
        HEADER_OVERRIDE,
        {"word/header1.xml": story("hdr", "top", STRICT_MAIN)},
    )

    assert applies(content) == [
        ("word/header1.xml", furniture("header", record(0, "first", "rId1", False)))
    ]


@pytest.mark.parametrize(
    ("body", "sections"),
    [
        # A reference in another vocabulary, or in none, references nothing.
        (sect('<x:headerReference w:type="default" r:id="rId1"/>'), 1),
        (sect('<headerReference type="default" id="rId1"/>'), 1),
        # A reference below a section rather than directly inside it is not the
        # section's own declaration.
        (sect(f"<w:footnotePr>{reference('header')}</w:footnotePr>"), 1),
        # A section break in another vocabulary, or in none, breaks nothing.
        (f"<x:sectPr>{reference('header')}</x:sectPr>", 0),
        (f"<sectPr>{reference('header')}</sectPr>", 0),
        # A section break below a foreign paragraph or foreign properties is on
        # neither of the two routes a section is declared on.
        (f"<x:p><w:pPr>{HEADER_SECTION}</w:pPr></x:p>", 0),
        (f"<w:p><x:pPr>{HEADER_SECTION}</x:pPr></w:p>", 0),
        # A section break inside a table cell declares no section of its own.
        (
            "<w:tbl><w:tr><w:tc>"
            + para("cell", HEADER_SECTION)
            + "</w:tc></w:tr></w:tbl>",
            0,
        ),
    ],
)
def test_a_lookalike_declares_no_section_and_reaches_no_part(
    body: str, sections: int
) -> None:
    content = furnished(
        para() + body, parts={"word/header1.xml": MALFORMED}
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert result.limitations["documentSections"] == sections
    assert {item.source_locator["part"] for item in result.segments} == {
        "word/document.xml"
    }


def test_a_foreign_body_declares_no_section() -> None:
    content = package(
        f'<w:document xmlns:w="{MAIN}" xmlns:r="{RELS}" xmlns:x="{OTHER}">'
        f"<x:body>{para() + HEADER_SECTION}</x:body></w:document>",
        rels(HEADER_RELATION),
        HEADER_OVERRIDE,
        {"word/header1.xml": MALFORMED},
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert result.limitations["documentSections"] == 0
    assert read(content) == [("word/document.xml", "body", "prose")]


def test_a_section_declaring_one_kind_twice_is_refused() -> None:
    """Two references of one kind in one section leave which part that kind
    places ambiguous, whichever relationships they name."""
    content = furnished(
        sect(reference("header"), reference("header", rid="rId2")),
        (HEADER_RELATION, ("rId2", "header2.xml", "header")),
        HEADER_OVERRIDE + override("word/header2.xml", HEADER_CT),
        {"word/header2.xml": story("hdr", "two")},
    )

    with pytest.raises(OOXMLInvalid, match="DOCX section references one kind twice"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("element", "reason"),
    [
        # ``r:id`` is required, and names exactly one relationship.
        ('<w:headerReference w:type="default"/>', "reference is malformed"),
        ('<w:headerReference w:type="default" id="rId1"/>', "reference is malformed"),
        ('<w:headerReference w:type="default" w:id="rId1"/>', "reference is malformed"),
        (
            '<w:headerReference w:type="default" r:id="rId1" r2:id="rId1"/>',
            "reference is malformed",
        ),
        (
            '<w2:headerReference w2:type="default" r:id="rId1" r2:id="rId1"/>',
            "reference is malformed",
        ),
        # The id is read only in the reference element's own profile, so a
        # Transitional reference carrying a Strict id -- or the reverse -- mixes
        # two profiles in one declaration and names no relationship.
        (
            '<w:headerReference w:type="default" r2:id="rId1"/>',
            "reference is malformed",
        ),
        (
            '<w2:headerReference w2:type="default" r:id="rId1"/>',
            "reference is malformed",
        ),
        # ``w:type`` is required, is a closed enumeration, and is read only in
        # the reference's own profile.
        ('<w:headerReference r:id="rId1"/>', "reference is malformed"),
        ('<w:headerReference w:type="" r:id="rId1"/>', "reference is malformed"),
        ('<w:headerReference w:type="Default" r:id="rId1"/>', "reference is malformed"),
        (
            '<w:headerReference w:type="evenPage" r:id="rId1"/>',
            "reference is malformed",
        ),
        ('<w:headerReference type="default" r:id="rId1"/>', "reference is malformed"),
        (
            '<w:headerReference w2:type="default" r:id="rId1"/>',
            "reference is malformed",
        ),
        (
            '<w:headerReference w:type="default" w2:type="first" r:id="rId1"/>',
            "reference declares two kinds",
        ),
        # An id naming no relationship of this part resolves to nothing.
        (
            '<w:headerReference w:type="default" r:id="rIdX"/>',
            "names a missing relationship",
        ),
    ],
)
def test_a_malformed_reference_places_nothing(element: str, reason: str) -> None:
    with pytest.raises(OOXMLInvalid, match=reason):
        extract_docx(furnished(sect(element)), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("relations", "content_types", "reason"),
    [
        # A header reference never resolves through a footer's relationship, or
        # any other, and a type that merely ends the same way is not one OOXML
        # defines.
        (
            (("rId1", "header1.xml", "footer"),),
            HEADER_OVERRIDE,
            "names a relationship of another type",
        ),
        (
            (("rId1", "header1.xml", "hyperlink"),),
            HEADER_OVERRIDE,
            "names a relationship of another type",
        ),
        (
            (
                (
                    "rId1",
                    "header1.xml",
                    "https://example.invalid/officeDocument/2006/relationships/header",
                ),
            ),
            HEADER_OVERRIDE,
            "names a relationship of another type",
        ),
        # The part has to be declared a header part outright: the package's
        # default for ``.xml`` and a footer's content type each qualify it as
        # nothing.
        ((HEADER_RELATION,), "", "does not name a header part"),
        (
            (HEADER_RELATION,),
            override("word/header1.xml", FOOTER_CT),
            "does not name a header part",
        ),
        # Nothing is fetched, and nothing is reached outside the package.
        (
            (("rId1", "https://example.invalid/header1.xml", "header"),),
            HEADER_OVERRIDE,
            "DOCX header relationship is external",
        ),
        (
            (("rId1", "nowhere.xml", "header"),),
            HEADER_OVERRIDE,
            "escapes or misses the package",
        ),
        (
            (("rId1", "../../outside.xml", "header"),),
            HEADER_OVERRIDE,
            "escapes or misses the package",
        ),
    ],
)
def test_a_reference_resolves_only_through_its_own_declarations(
    relations: tuple[tuple[str, str, str], ...], content_types: str, reason: str
) -> None:
    content = furnished(
        HEADER_SECTION, relations, content_types, {"word/header1.xml": MALFORMED}
    )

    with pytest.raises(OOXMLInvalid, match=reason):
        extract_docx(content, capture_id=CAPTURE)


def test_a_content_type_is_matched_case_folded() -> None:
    """The package folds the content types it declares, so a header part named
    in another case is still declared a header part."""
    content = furnished(content_types=override("word/header1.xml", HEADER_CT.upper()))

    assert applies(content) == [
        ("word/header1.xml", furniture("header", record(0, "default", "rId1", False)))
    ]


@pytest.mark.parametrize(
    "root",
    [
        # A footer where the section said header.
        story("ftr"),
        # The right local name in the wrong vocabulary, or in none.
        story("hdr", namespace=OTHER),
        story("hdr", namespace=None),
        # Arbitrary XML, including WordprocessingML that is not a story at all.
        f'<w:document xmlns:w="{MAIN}"><w:body>{para()}</w:body></w:document>',
        '<Relationships xmlns="urn:nothing"/>',
    ],
)
def test_a_resolved_part_has_to_be_the_story_it_was_reached_as(root: str) -> None:
    content = furnished(parts={"word/header1.xml": root})

    with pytest.raises(OOXMLInvalid, match="DOCX header part is not a header"):
        extract_docx(content, capture_id=CAPTURE)


def test_a_footer_reference_does_not_accept_a_header_part() -> None:
    content = package(
        document(sect(reference("footer"))),
        rels(("rId1", "footer1.xml", "footer")),
        override("word/footer1.xml", FOOTER_CT),
        {"word/footer1.xml": story("hdr", "top")},
    )

    with pytest.raises(OOXMLInvalid, match="DOCX footer part is not a footer"):
        extract_docx(content, capture_id=CAPTURE)


def test_sections_are_counted_to_the_bound_and_refused_beyond_it() -> None:
    """The bound is taken on the sections the document declares, before any
    reference is resolved: neither package below references anything at all."""
    at_bound = package(
        document("<w:sectPr/>" * _MAX_DOCX_SECTIONS), rels(), "", {}
    )
    beyond = package(
        document("<w:sectPr/>" * (_MAX_DOCX_SECTIONS + 1)), rels(), "", {}
    )

    assert (
        extract_docx(at_bound, capture_id=CAPTURE).limitations["documentSections"]
        == _MAX_DOCX_SECTIONS
    )
    with pytest.raises(OOXMLInvalid, match="DOCX declares too many sections"):
        extract_docx(beyond, capture_id=CAPTURE)


def test_a_document_without_sections_reports_none() -> None:
    content = package(document(para()), rels(), "", {})

    result = extract_docx(content, capture_id=CAPTURE)

    assert result.limitations["documentSections"] == 0
    assert read(content) == [("word/document.xml", "body", "prose")]
