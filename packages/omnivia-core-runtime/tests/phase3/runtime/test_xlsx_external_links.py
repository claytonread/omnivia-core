"""XLSX external links: an inventory of what the workbook declared, inert.

A workbook may declare ``externalReferences``, and each reference relates one
external-link part. That part holds exactly one of the three forms
``EG_ExternalLinkChoice`` gives it: an ``externalBook`` linking to another
workbook, a ``ddeLink`` naming a DDE service and topic, or an ``oleLink``
naming an OLE server's document. This route reports that they exist and what
the author declared about each. It opens none of them.

So the contract here is deliberately narrow. A record names the form, the
relationship and part the workbook reached it by, the relationship's own target
text and the scheme that text names, and the inert metadata the form declares
beside it -- the service and topic a DDE link names, the ``ProgID`` an OLE link
names, and counts for the sheet names, defined names, cached sheet blocks and
item lists. The cached values under ``sheetDataSet`` are the *other* workbook's
content, so they are counted and never copied: a reader that reported them
would be reporting data as though the link had been followed. No linked
workbook is opened, no DDE service is contacted, no OLE server is launched, and
every record says so by carrying ``refreshPerformed`` and
``externalAccessPerformed`` as false.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
LINK_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"
)
WORKBOOK_PART = "xl/workbook.xml"
LINK_PART = "xl/externalLinks/externalLink1.xml"
LINK_RELS = "xl/externalLinks/_rels/externalLink1.xml.rels"
# Targets nothing in this module ever opens. The package holds no member by any
# of these names, so a reader that resolved one would fail outright, and every
# record naming one is asserted to carry ``externalAccessPerformed`` false.
BOOK_TARGET = "file:///Volumes/Finance/Q3%20Ledger.xlsx"
UNC_TARGET = r"\\reporting\warehouse\Forecast.xlsx"
OLE_TARGET = "file:///Volumes/Finance/Model.doc"
# The cached values the linked workbook stored. This route counts the blocks
# holding them and copies none of them out, so no test expects any of this in a
# record, a locator or a cell.
CACHED = (
    "<sheetDataSet>"
    '<sheetData sheetId="0">'
    '<row r="1"><cell r="A1" t="str"><v>9,412,336.18</v></cell>'
    '<cell r="B1"><v>0.0725</v></cell></row>'
    "</sheetData>"
    '<sheetData sheetId="1" refreshError="1"/>'
    "</sheetDataSet>"
)
SECRETS = ("9,412,336.18", "0.0725", "refreshError")
# A workbook link declaring every group this route counts, so one record shows
# the whole externalBook surface.
BOOK = (
    '<externalBook r:id="rIdTarget">'
    "<sheetNames>"
    '<sheetName val="Ledger"/><sheetName val="Rates"/><sheetName val="Notes"/>'
    "</sheetNames>"
    "<definedNames>"
    '<definedName name="Opening" refersTo="=Ledger!$A$1"/>'
    "</definedNames>"
    f"{CACHED}"
    "</externalBook>"
)
DDE = (
    '<ddeLink ddeService="QuoteFeed" ddeTopic="EURUSD">'
    "<ddeItems>"
    '<ddeItem name="bid"/><ddeItem name="ask"/>'
    "</ddeItems>"
    "</ddeLink>"
)
OLE = (
    '<oleLink r:id="rIdTarget" progId="Word.Document.12">'
    '<oleItems><oleItem name="Summary"/></oleItems>'
    "</oleLink>"
)


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def link(body: str = BOOK, *, main: str = MAIN, relns: str = RELNS) -> str:
    return f'<externalLink xmlns="{main}" xmlns:r="{relns}">{body}</externalLink>'


def target_rels(
    *,
    relationship: str = f"{RELNS}/externalLinkPath",
    target: str = BOOK_TARGET,
    mode: str = ' TargetMode="External"',
    relation_id: str = "rIdTarget",
) -> str:
    return (
        "<Relationships>"
        f'<Relationship Id="{relation_id}" Target="{target}"{mode}'
        f' Type="{relationship}"/>'
        "</Relationships>"
    )


def xlsx(
    *,
    links: dict[str, str] | None = None,
    references: str | None = None,
    content_type: str = LINK_CT,
    relationship: str = f"{RELNS}/externalLink",
    main: str = MAIN,
    relns: str = RELNS,
    rels: str | None = None,
    link_rels: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """One sheet, and a workbook declaring one external link by default.

    ``links`` maps each external-link part to its XML; ``references`` overrides
    the ``externalReferences`` body the workbook writes; ``link_rels`` maps each
    link part's ``.rels`` path to its XML. ``links`` of ``{}`` with no
    ``references`` writes no ``externalReferences`` element at all, which is the
    absent case an empty list has to stay distinguishable from.
    """
    parts = {LINK_PART: link()} if links is None else links
    declared = (
        references
        if references is not None
        else "".join(
            f'<externalReference r:id="rIdLink{index}"/>'
            for index in range(len(parts))
        )
    )
    body = (
        ""
        if references is None and not parts
        else f"<externalReferences>{declared}</externalReferences>"
    )
    related = "".join(
        f'<Relationship Id="rIdLink{index}" Target="{name[len("xl/") :]}"'
        f' Type="{relationship}"/>'
        for index, name in enumerate(parts)
    )
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{content_type}"/>' for name in parts
    )
    members = {
        "[Content_Types].xml": f"<Types>{overrides}</Types>",
        WORKBOOK_PART: (
            f'<workbook xmlns="{main}" xmlns:r="{relns}"><sheets>'
            '<sheet name="Sales" sheetId="7" r:id="rId1"/>'
            f"</sheets>{body}</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": rels
        or (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            f"{related}"
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{main}"><sheetData>'
            '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
            "</sheetData></worksheet>"
        ),
        **parts,
        **(
            {LINK_RELS: target_rels()}
            if link_rels is None and parts
            else (link_rels or {})
        ),
        **(extra or {}),
    }
    return archive(members)


def inventory(content: bytes) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Every external-link inventory record, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and (item.structured_value or {}).get("kind") == "externalLink"
    ]


# Where the single default record says it came from.
WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "part": WORKBOOK_PART,
    "externalLinkPart": LINK_PART,
    "externalLinkRelationshipId": "rIdLink0",
    "externalLinkIndex": 0,
}
# Every field a workbook link reports, pinned by full equality so a field this
# route starts reporting -- or stops -- fails here rather than passing
# unnoticed. The three DDE and two OLE fields are absent on it, as its three
# group counts are on the other two forms.
BOOK_RECORD: dict[str, object] = {
    "kind": "externalLink",
    "linkKind": "externalBook",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "targetRelationshipId": "rIdTarget",
    "targetRelationshipType": f"{RELNS}/externalLinkPath",
    "target": BOOK_TARGET,
    "targetExternal": True,
    "targetScheme": "file",
    "sheetNameCount": 3,
    "definedNameCount": 1,
    "cachedSheetCount": 2,
    "ddeService": None,
    "ddeTopic": None,
    "ddeItemCount": None,
    "progId": None,
    "oleItemCount": None,
}
DDE_RECORD: dict[str, object] = {
    "kind": "externalLink",
    "linkKind": "ddeLink",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "targetRelationshipId": None,
    "targetRelationshipType": None,
    "target": None,
    "targetExternal": None,
    "targetScheme": None,
    "sheetNameCount": None,
    "definedNameCount": None,
    "cachedSheetCount": None,
    "ddeService": "QuoteFeed",
    "ddeTopic": "EURUSD",
    "ddeItemCount": 2,
    "progId": None,
    "oleItemCount": None,
}
OLE_RECORD: dict[str, object] = {
    "kind": "externalLink",
    "linkKind": "oleLink",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "targetRelationshipId": "rIdTarget",
    "targetRelationshipType": f"{RELNS}/oleObject",
    "target": OLE_TARGET,
    "targetExternal": True,
    "targetScheme": "file",
    "sheetNameCount": None,
    "definedNameCount": None,
    "cachedSheetCount": None,
    "ddeService": None,
    "ddeTopic": None,
    "ddeItemCount": None,
    "progId": "Word.Document.12",
    "oleItemCount": 1,
}


def test_an_ordinary_workbook_link_is_inventoried_exactly() -> None:
    """The whole externalBook surface, pinned by full equality."""
    assert inventory(xlsx(links={LINK_PART: link()})) == [(WHERE, BOOK_RECORD)]


def test_external_links_are_inert_inventory_after_the_workbook_content() -> None:
    """Visible inventory, carrying no text, after the cells and connections."""
    segments = extract_xlsx(xlsx(links={LINK_PART: link()}), capture_id=CAPTURE).segments
    assert [item.semantic_role for item in segments] == [
        "table_cell",
        "object_inventory",
    ]
    assert segments[1].extraction_method == "inventory"
    assert segments[1].visibility == "visible"
    assert segments[1].text is None


def test_three_links_are_reported_in_declaration_order() -> None:
    """Each form once, indexed by the workbook's own declaration order."""
    parts = {
        "xl/externalLinks/externalLink1.xml": link(),
        "xl/externalLinks/externalLink2.xml": link(DDE),
        "xl/externalLinks/externalLink3.xml": link(OLE),
    }
    records = inventory(
        xlsx(
            links=parts,
            link_rels={
                "xl/externalLinks/_rels/externalLink1.xml.rels": target_rels(),
                "xl/externalLinks/_rels/externalLink3.xml.rels": target_rels(
                    relationship=f"{RELNS}/oleObject", target=OLE_TARGET
                ),
            },
        )
    )
    assert [locator["externalLinkIndex"] for locator, _record in records] == [0, 1, 2]
    assert [locator["externalLinkPart"] for locator, _record in records] == list(parts)
    assert [record for _locator, record in records] == [
        BOOK_RECORD,
        DDE_RECORD,
        OLE_RECORD,
    ]


def test_the_external_link_count_is_reported_in_the_extraction_metadata() -> None:
    """Counted beside ``connections``, and distinct from ``externalReferences``.

    ``externalLinks`` is what the workbook declared; ``externalReferences`` is
    every external relationship any part of the package holds. The default
    package declares one link whose own part relates one external path, so the
    two figures are one and one here and would diverge the moment any other
    part related anything external.
    """
    extraction = extract_xlsx(xlsx(links={LINK_PART: link()}), capture_id=CAPTURE)
    assert extraction.limitations["externalLinks"] == 1
    assert extraction.limitations["externalReferences"] == 1


def test_a_workbook_declaring_no_external_references_reports_none() -> None:
    """Absent is absent: no list, no records, and a count of zero."""
    extraction = extract_xlsx(xlsx(links={}), capture_id=CAPTURE)
    assert extraction.limitations["externalLinks"] == 0
    assert inventory(xlsx(links={})) == []


def test_an_empty_external_reference_list_reports_none() -> None:
    """An ``externalReferences`` holding nothing declares no link."""
    assert inventory(xlsx(links={}, references="")) == []


def test_the_strict_profile_resolves_and_reads_the_same_link() -> None:
    """Strict relationship types and Strict SpreadsheetML qualify alike.

    Every URI on the route is the Strict one -- the workbook's link
    relationship, the link part's path relationship -- and both namespaces are
    the Strict main. The record is identical but for the relationship type it
    reports, which is the one the package actually wrote.
    """
    records = inventory(
        xlsx(
            links={LINK_PART: link(main=STRICT_MAIN, relns=STRICT_RELNS)},
            relationship=f"{STRICT_RELNS}/externalLink",
            main=STRICT_MAIN,
            relns=STRICT_RELNS,
            link_rels={
                LINK_RELS: target_rels(
                    relationship=f"{STRICT_RELNS}/externalLinkPath"
                )
            },
        )
    )
    assert records == [
        (
            WHERE,
            {
                **BOOK_RECORD,
                "targetRelationshipType": f"{STRICT_RELNS}/externalLinkPath",
            },
        )
    ]


def test_a_dde_link_names_a_service_and_topic_and_reaches_neither() -> None:
    """Both required claims come out as text; no conversation is opened."""
    (_locator, record), = inventory(
        xlsx(links={LINK_PART: link(DDE)}, link_rels={})
    )
    assert record == DDE_RECORD


def test_an_ole_link_names_a_server_and_reaches_nothing() -> None:
    """The ``ProgID`` is the author's own text; nothing is registered or run."""
    (_locator, record), = inventory(
        xlsx(
            links={LINK_PART: link(OLE)},
            link_rels={
                LINK_RELS: target_rels(
                    relationship=f"{RELNS}/oleObject", target=OLE_TARGET
                )
            },
        )
    )
    assert record == OLE_RECORD


def test_an_authored_empty_required_claim_is_kept_as_written() -> None:
    """``ST_Xstring`` admits the empty string, so empty is a claim, not absence.

    The same split the person list makes: a claim never written is refused, one
    written blank is reported as the blank the file wrote.
    """
    (_locator, record), = inventory(
        xlsx(
            links={LINK_PART: link('<ddeLink ddeService="" ddeTopic=""/>')},
            link_rels={},
        )
    )
    assert (record["ddeService"], record["ddeTopic"]) == ("", "")
    assert record["ddeItemCount"] is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('<ddeLink ddeTopic="EURUSD"/>', id="no-service"),
        pytest.param('<ddeLink ddeService="QuoteFeed"/>', id="no-topic"),
        pytest.param("<ddeLink/>", id="neither"),
        pytest.param('<oleLink r:id="rIdTarget"/>', id="no-prog-id"),
    ],
)
def test_a_link_missing_a_required_claim_is_refused(body: str) -> None:
    """A claim the schema requires and the file never wrote names nothing."""
    with pytest.raises(OOXMLInvalid, match="missing a required claim"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link(body)},
                link_rels={
                    LINK_RELS: target_rels(
                        relationship=f"{RELNS}/oleObject", target=OLE_TARGET
                    )
                },
            ),
            capture_id=CAPTURE,
        )


def test_a_workbook_link_naming_no_path_is_a_cached_link_not_a_refusal() -> None:
    """``CT_ExternalBook`` leaves ``r:id`` optional, so absent stays absent.

    A workbook link that stored sheet names and cached values but no path is
    the inventory it is. The whole target group is reported absent rather than
    filled with a path nothing wrote.
    """
    (_locator, record), = inventory(
        xlsx(
            links={
                LINK_PART: link(
                    f'<externalBook><sheetNames><sheetName val="Ledger"/>'
                    f"</sheetNames>{CACHED}</externalBook>"
                )
            },
            link_rels={},
        )
    )
    assert record == {
        **BOOK_RECORD,
        "targetRelationshipId": None,
        "targetRelationshipType": None,
        "target": None,
        "targetExternal": None,
        "targetScheme": None,
        "sheetNameCount": 1,
        "definedNameCount": None,
        "cachedSheetCount": 2,
    }


def test_an_ole_link_naming_no_target_is_refused() -> None:
    """``CT_OleLink`` requires its ``r:id``, unlike ``CT_ExternalBook``."""
    with pytest.raises(OOXMLInvalid, match="names no target relationship"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link('<oleLink progId="Word.Document.12"/>')},
                link_rels={},
            ),
            capture_id=CAPTURE,
        )


def test_an_undeclared_group_is_absent_rather_than_counted_zero() -> None:
    """A group never written is None; one written empty counts zero."""
    (_locator, record), = inventory(
        xlsx(
            links={
                LINK_PART: link(
                    '<externalBook r:id="rIdTarget"><sheetNames/></externalBook>'
                )
            }
        )
    )
    assert record["sheetNameCount"] == 0
    assert record["definedNameCount"] is None
    assert record["cachedSheetCount"] is None


def test_every_record_states_that_nothing_was_refreshed_or_reached() -> None:
    """The two standing claims, on every form, whatever the link named."""
    records = inventory(
        xlsx(
            links={
                "xl/externalLinks/externalLink1.xml": link(),
                "xl/externalLinks/externalLink2.xml": link(DDE),
            },
            link_rels={"xl/externalLinks/_rels/externalLink1.xml.rels": target_rels()},
        )
    )
    assert len(records) == 2
    for _locator, record in records:
        assert record["refreshPerformed"] is False
        assert record["externalAccessPerformed"] is False


def test_no_cached_value_from_the_linked_workbook_reaches_the_extraction() -> None:
    """``sheetDataSet`` is the other workbook's content: counted, never copied.

    The cached blocks above hold a currency figure and a rate. Neither may
    appear anywhere in the extraction -- not in a record, not in a locator, not
    in a cell -- because reporting one would report the other workbook's data
    as though this link had been followed.
    """
    extraction = extract_xlsx(xlsx(links={LINK_PART: link()}), capture_id=CAPTURE)
    rendered = repr(
        [
            (item.source_locator, item.structured_value, item.text)
            for item in extraction.segments
        ]
    )
    for secret in SECRETS:
        assert secret not in rendered
    assert inventory(xlsx(links={LINK_PART: link()}))[0][1]["cachedSheetCount"] == 2


@pytest.mark.parametrize(
    ("target", "scheme"),
    [
        pytest.param(BOOK_TARGET, "file", id="file-url"),
        pytest.param("https://example.invalid/books/Q3.xlsx", "https", id="https"),
        pytest.param(UNC_TARGET, None, id="unc-path"),
        pytest.param("../sibling/Book2.xlsx", None, id="relative-path"),
        pytest.param("C:\\Finance\\Book2.xlsx", None, id="drive-letter"),
    ],
)
def test_the_target_is_reported_verbatim_beside_the_scheme_it_names(
    target: str, scheme: str | None
) -> None:
    """The relationship's own text, and the scheme read off it, reaching neither.

    A drive letter stays a path rather than becoming a ``c:`` scheme, and a
    relative target is never normalised, joined or resolved against anything.
    """
    (_locator, record), = inventory(
        xlsx(link_rels={LINK_RELS: target_rels(target=target)})
    )
    assert record["target"] == target
    assert record["targetScheme"] == scheme


def test_an_over_long_target_is_clipped_rather_than_refused() -> None:
    """Not an identity, so it is cut to the shared bound instead of refused."""
    long = "https://example.invalid/" + "p" * 4_000
    (_locator, record), = inventory(
        xlsx(link_rels={LINK_RELS: target_rels(target=long)})
    )
    assert record["target"] == long[:2048]
    assert record["targetScheme"] == "https"


def test_over_long_claims_are_clipped_rather_than_refused() -> None:
    """A service, topic and ``ProgID`` are text, so each is cut to the bound."""
    long = "q" * 400
    (_locator, record), = inventory(
        xlsx(
            links={
                LINK_PART: link(f'<ddeLink ddeService="{long}" ddeTopic="{long}"/>')
            },
            link_rels={},
        )
    )
    assert (record["ddeService"], record["ddeTopic"]) == ("q" * 256, "q" * 256)


def test_an_internal_target_relationship_is_refused() -> None:
    """A link's target is outside the package by definition.

    An internal one contradicts the declaration, so it is refused rather than
    opened as a package member -- which is exactly the read this route must
    never perform.
    """
    with pytest.raises(OOXMLInvalid, match="target relationship is not external"):
        extract_xlsx(
            xlsx(
                link_rels={
                    LINK_RELS: target_rels(
                        target="../worksheets/sheet1.xml", mode=""
                    )
                }
            ),
            capture_id=CAPTURE,
        )


def test_a_missing_target_relationship_is_refused() -> None:
    """The id names nothing in the link part's own relationships."""
    with pytest.raises(OOXMLInvalid, match="missing target relationship"):
        extract_xlsx(xlsx(link_rels={}), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "relationship",
    [
        pytest.param(f"{RELNS}/oleObject", id="other-defined-type"),
        pytest.param(f"{RELNS}/externalLink", id="the-reaching-type"),
        pytest.param(f"{RELNS}/externalLinkPaths", id="suffixed"),
        pytest.param("urn:fake/externalLinkPath", id="foreign"),
    ],
)
def test_a_target_relationship_of_another_type_is_refused(relationship: str) -> None:
    """Only the two URIs OOXML defines for the form's own target qualify."""
    with pytest.raises(OOXMLInvalid, match="target relationship of another type"):
        extract_xlsx(
            xlsx(link_rels={LINK_RELS: target_rels(relationship=relationship)}),
            capture_id=CAPTURE,
        )


def test_an_external_reference_naming_no_relationship_is_refused() -> None:
    """``CT_ExternalReference`` requires its ``r:id``."""
    with pytest.raises(OOXMLInvalid, match="names no relationship"):
        extract_xlsx(
            xlsx(links={LINK_PART: link()}, references="<externalReference/>"),
            capture_id=CAPTURE,
        )


def test_an_external_reference_naming_a_missing_relationship_is_refused() -> None:
    """The id resolves only through the workbook's own relationships."""
    with pytest.raises(
        OOXMLInvalid, match="external link reference names a missing relationship"
    ):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                references='<externalReference r:id="rIdGhost"/>',
            ),
            capture_id=CAPTURE,
        )


def test_a_reference_reaching_a_relationship_of_another_type_is_refused() -> None:
    """A connections or worksheet relationship reaches no external link."""
    with pytest.raises(
        OOXMLInvalid, match="external link reference names a relationship of another"
    ):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                references='<externalReference r:id="rId1"/>',
            ),
            capture_id=CAPTURE,
        )


def test_a_repeated_external_reference_is_refused() -> None:
    """Two references under one id leave one link declared twice."""
    with pytest.raises(OOXMLInvalid, match="external reference is repeated"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                references=(
                    '<externalReference r:id="rIdLink0"/>'
                    '<externalReference r:id="rIdLink0"/>'
                ),
            ),
            capture_id=CAPTURE,
        )


def test_two_relationships_resolving_to_one_part_are_refused() -> None:
    """Distinct ids reaching one part leave the link counted ambiguously."""
    with pytest.raises(OOXMLInvalid, match="external link part is referenced twice"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                references=(
                    '<externalReference r:id="rIdLink0"/>'
                    '<externalReference r:id="rIdLinkB"/>'
                ),
                rels=(
                    "<Relationships>"
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                    f' Type="{RELNS}/worksheet"/>'
                    '<Relationship Id="rIdLink0"'
                    ' Target="externalLinks/externalLink1.xml"'
                    f' Type="{RELNS}/externalLink"/>'
                    '<Relationship Id="rIdLinkB"'
                    ' Target="/xl/externalLinks/externalLink1.xml"'
                    f' Type="{STRICT_RELNS}/externalLink"/>'
                    "</Relationships>"
                ),
            ),
            capture_id=CAPTURE,
        )


def test_more_than_one_external_reference_list_is_refused() -> None:
    """``CT_Workbook`` gives it one list, so nothing picks between two."""
    with pytest.raises(
        OOXMLInvalid, match="more than one external-reference list"
    ):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                extra={
                    WORKBOOK_PART: (
                        f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
                        '<sheet name="Sales" sheetId="7" r:id="rId1"/></sheets>'
                        '<externalReferences><externalReference r:id="rIdLink0"/>'
                        "</externalReferences>"
                        '<externalReferences><externalReference r:id="rIdLink0"/>'
                        "</externalReferences></workbook>"
                    )
                },
            ),
            capture_id=CAPTURE,
        )


def test_a_part_declared_as_something_else_is_refused() -> None:
    """The declared content type is what qualifies the part the route reached."""
    with pytest.raises(
        OOXMLInvalid,
        match="external link relationship does not name a external link part",
    ):
        extract_xlsx(
            xlsx(links={LINK_PART: link()}, content_type="application/xml"),
            capture_id=CAPTURE,
        )


def test_a_part_declared_only_by_its_extension_is_refused() -> None:
    """A default for ``.xml`` says what every part is, which qualifies none."""
    package = archive(
        {
            "[Content_Types].xml": (
                '<Types><Default Extension="xml" ContentType="application/xml"/>'
                "</Types>"
            ),
            WORKBOOK_PART: (
                f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
                '<sheet name="Sales" sheetId="7" r:id="rId1"/></sheets>'
                '<externalReferences><externalReference r:id="rIdLink0"/>'
                "</externalReferences></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                "<Relationships>"
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                f' Type="{RELNS}/worksheet"/>'
                '<Relationship Id="rIdLink0"'
                ' Target="externalLinks/externalLink1.xml"'
                f' Type="{RELNS}/externalLink"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>'
            ),
            LINK_PART: link(),
            LINK_RELS: target_rels(),
        }
    )
    with pytest.raises(
        OOXMLInvalid,
        match="external link relationship does not name a external link part",
    ):
        extract_xlsx(package, capture_id=CAPTURE)


def test_an_external_link_relationship_is_refused() -> None:
    """The link part is a package member; the route out is the path inside it."""
    with pytest.raises(OOXMLInvalid, match="external link relationship is external"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link()},
                rels=(
                    "<Relationships>"
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                    f' Type="{RELNS}/worksheet"/>'
                    '<Relationship Id="rIdLink0" TargetMode="External"'
                    f' Target="{BOOK_TARGET}" Type="{RELNS}/externalLink"/>'
                    "</Relationships>"
                ),
            ),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    ("root", "message"),
    [
        pytest.param(
            '<externalLink xmlns="urn:fake"/>',
            "unsupported namespace",
            id="foreign-namespace",
        ),
        pytest.param("<externalLink/>", "unsupported namespace", id="unqualified"),
        pytest.param(
            f'<connections xmlns="{MAIN}"/>',
            "not rooted at its own link",
            id="wrong-root",
        ),
    ],
)
def test_a_part_whose_root_is_not_an_external_link_is_refused(
    root: str, message: str
) -> None:
    """The declared type promised a vocabulary; the root is where it is kept."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(
            xlsx(links={LINK_PART: root}, link_rels={}), capture_id=CAPTURE
        )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('<externalBook xmlns="urn:fake"/>', id="foreign-namespace"),
        pytest.param(
            '<x:externalBook xmlns:x="urn:fake"/>', id="foreign-prefix"
        ),
        pytest.param(
            "<extLst><externalBook/></extLst>", id="nested"
        ),
        pytest.param("<externalbook/>", id="miscased"),
        pytest.param("<externalWorkbook/>", id="misspelled"),
    ],
)
def test_a_lookalike_link_declares_nothing(body: str) -> None:
    """Off the schema's own route, the part declares no link and is refused.

    The nested case carries a well-formed qualified ``externalBook`` one level
    too deep, so a reader descending by local name would report it.
    """
    with pytest.raises(OOXMLInvalid, match="declares no link"):
        extract_xlsx(
            xlsx(links={LINK_PART: link(body)}, link_rels={}), capture_id=CAPTURE
        )


def test_a_part_declaring_two_links_is_refused() -> None:
    """``EG_ExternalLinkChoice`` gives the part one, so two are ambiguous."""
    with pytest.raises(OOXMLInvalid, match="declares more than one link"):
        extract_xlsx(
            xlsx(
                links={LINK_PART: link(f"{BOOK}{DDE}")},
            ),
            capture_id=CAPTURE,
        )


def test_a_repeated_group_inside_one_link_is_refused() -> None:
    """One group of a kind per link, so nothing picks between two."""
    with pytest.raises(OOXMLInvalid, match="declares a group twice"):
        extract_xlsx(
            xlsx(
                links={
                    LINK_PART: link(
                        '<externalBook r:id="rIdTarget">'
                        '<sheetNames><sheetName val="A"/></sheetNames>'
                        '<sheetNames><sheetName val="B"/></sheetNames>'
                        "</externalBook>"
                    )
                }
            ),
            capture_id=CAPTURE,
        )


def test_a_foreign_member_inside_a_group_is_not_counted() -> None:
    """A group counts only its own qualified members, nothing that spells alike."""
    (_locator, record), = inventory(
        xlsx(
            links={
                LINK_PART: link(
                    '<externalBook r:id="rIdTarget"><sheetNames>'
                    '<sheetName val="Ledger"/>'
                    '<x:sheetName xmlns:x="urn:fake" val="Ghost"/>'
                    '<sheetNames><sheetName val="Nested"/></sheetNames>'
                    "</sheetNames></externalBook>"
                )
            }
        )
    )
    assert record["sheetNameCount"] == 1


def test_a_lookalike_external_reference_declares_nothing() -> None:
    """Only a qualified direct ``externalReference`` counts, and it is indexed."""
    assert (
        inventory(
            xlsx(
                links={},
                references=(
                    '<x:externalReference xmlns:x="urn:fake" r:id="rIdLink0"/>'
                ),
            )
        )
        == []
    )


def test_a_workbook_that_wrote_no_namespace_declares_no_external_links() -> None:
    """A foreign workbook root is transparent rather than read by local name."""
    assert (
        inventory(
            xlsx(
                links={LINK_PART: link()},
                main="urn:fake",
                extra={
                    "xl/worksheets/sheet1.xml": (
                        '<worksheet xmlns="urn:fake"><sheetData/></worksheet>'
                    )
                },
            )
        )
        == []
    )


def test_a_list_at_the_bound_is_accepted_and_one_over_is_refused() -> None:
    """256 links resolve; the 257th is refused before a part is opened.

    The over-the-bound package deliberately relates and declares none of the
    parts, which proves the count is taken from the workbook's declarations
    rather than from anything resolved.
    """
    at_bound = {
        f"xl/externalLinks/externalLink{index}.xml": link(DDE)
        for index in range(256)
    }
    assert len(inventory(xlsx(links=at_bound, link_rels={}))) == 256
    over = "".join(
        f'<externalReference r:id="rIdLink{index}"/>' for index in range(257)
    )
    with pytest.raises(OOXMLInvalid, match="too many external links"):
        extract_xlsx(
            xlsx(links={}, references=over),
            capture_id=CAPTURE,
        )


def test_the_inventory_is_byte_for_byte_repeatable() -> None:
    """Same package, same records, in the same order, every time."""
    content = xlsx(
        links={
            "xl/externalLinks/externalLink1.xml": link(),
            "xl/externalLinks/externalLink2.xml": link(DDE),
            "xl/externalLinks/externalLink3.xml": link(OLE),
        },
        link_rels={
            "xl/externalLinks/_rels/externalLink1.xml.rels": target_rels(),
            "xl/externalLinks/_rels/externalLink3.xml.rels": target_rels(
                relationship=f"{RELNS}/oleObject", target=OLE_TARGET
            ),
        },
    )
    first = inventory(content)
    assert len(first) == 3
    assert all(inventory(content) == first for _ in range(3))


def test_the_package_metadata_counts_the_links_and_their_members() -> None:
    """``externalLinks`` and ``packageMembers`` both reflect the two links.

    ``externalReferences`` counts every external relationship in the package --
    here the one path the workbook link relates -- which is why it reads one
    while ``externalLinks`` reads two.
    """
    extraction = extract_xlsx(
        xlsx(
            links={
                "xl/externalLinks/externalLink1.xml": link(),
                "xl/externalLinks/externalLink2.xml": link(DDE),
            },
            link_rels={"xl/externalLinks/_rels/externalLink1.xml.rels": target_rels()},
        ),
        capture_id=CAPTURE,
    )
    assert extraction.limitations["externalLinks"] == 2
    assert extraction.limitations["externalReferences"] == 1
    assert extraction.limitations["packageMembers"] == 7
    assert extraction.limitations["connections"] == 0


def test_nothing_outside_the_package_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof by refusal: every way out of the process is made to raise.

    The three links above name a local file URL, a DDE quote feed and a Word
    OLE server. If any of that were opened, contacted or launched, one of these
    would fire. The extraction has to complete and still report all three.
    """
    import builtins
    import socket
    import subprocess
    import urllib.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the external-link inventory reached outside the package")

    # The package is already a ``BytesIO``, so nothing legitimate opens a file.
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    records = inventory(
        xlsx(
            links={
                "xl/externalLinks/externalLink1.xml": link(),
                "xl/externalLinks/externalLink2.xml": link(DDE),
                "xl/externalLinks/externalLink3.xml": link(OLE),
            },
            link_rels={
                "xl/externalLinks/_rels/externalLink1.xml.rels": target_rels(),
                "xl/externalLinks/_rels/externalLink3.xml.rels": target_rels(
                    relationship=f"{RELNS}/oleObject", target=OLE_TARGET
                ),
            },
        )
    )
    assert [record["linkKind"] for _locator, record in records] == [
        "externalBook",
        "ddeLink",
        "oleLink",
    ]
    assert records[0][1]["target"] == BOOK_TARGET
    assert records[1][1]["ddeService"] == "QuoteFeed"
    assert records[2][1]["progId"] == "Word.Document.12"
