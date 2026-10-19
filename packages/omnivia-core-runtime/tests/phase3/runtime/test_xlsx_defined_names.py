"""XLSX defined-name inventory over minimal qualified workbook packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
# (name, sheetId, state) per declared sheet, in workbook order. Ordinal 0 is
# visible and ordinal 1 is hidden, so a local scope's resolution and the
# visibility it takes from its sheet can both be read off one package.
SHEETS = (("Budget", "7", "visible"), ("Archive", "9", "hidden"))
# A third sheet the workbook keeps out of a reader's reach entirely.
DEEP = SHEETS + (("Draft", "11", "veryHidden"),)
# Looks like a reference into another workbook on someone else's disk. It is
# stored text and nothing more: nothing here opens it, resolves it or reports a
# cell for it.
EXTERNAL = r"'[C:\Ledger.xlsx]Sheet1'!$A$1"


def workbook(
    declarations: str,
    *,
    sheets: tuple[tuple[str, str, str], ...] = SHEETS,
    main: str | None = MAIN,
) -> str:
    """One workbook part declaring ``sheets`` then ``declarations`` verbatim.

    ``main`` is the root's default namespace, which is what the reader takes the
    qualified defined-name route from; ``None`` writes no default namespace at
    all, so the route cannot be reached however the declarations are spelled.
    """
    qualified = "" if main is None else f' xmlns="{main}"'
    declared = "".join(
        f'<sheet name="{name}" sheetId="{identity}" r:id="rId{index}" state="{state}"/>'
        for index, (name, identity, state) in enumerate(sheets, 1)
    )
    return (
        f'<workbook{qualified} xmlns:r="urn:r">'
        f"<sheets>{declared}</sheets>{declarations}</workbook>"
    )


def xlsx(
    declarations: str = "",
    *,
    sheets: tuple[tuple[str, str, str], ...] = SHEETS,
    main: str | None = MAIN,
) -> bytes:
    """A package whose only content is one written cell per declared sheet."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml", workbook(declarations, sheets=sheets, main=main)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            "<Relationships>"
            + "".join(
                f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, len(sheets) + 1)
            )
            + "</Relationships>",
        )
        for index in range(1, len(sheets) + 1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                '<worksheet><sheetData><row r="1">'
                f'<c r="A1"><v>{index}</v></c>'
                "</row></sheetData></worksheet>",
            )
    return buffer.getvalue()


def names(*declarations: str) -> str:
    """The workbook's own defined-name list, on the schema's route."""
    return f"<definedNames>{''.join(declarations)}</definedNames>"


def holder(declaration: str, namespace: str = "") -> str:
    """A list, and the member under it, in some namespace other than the root's.

    An empty namespace is the unqualified case, which is what a workbook that
    writes no default namespace leaves every one of its elements in.
    """
    return f'<definedNames xmlns="{namespace}">{declaration}</definedNames>'


def name(value: str, expression: str = "Budget!$A$1", attrs: str = "") -> str:
    return f'<definedName name="{value}"{attrs}>{expression}</definedName>'


def record(**overrides: object) -> dict[str, object]:
    """One expected inventory record, keyed in the order the reader writes."""
    return {
        "kind": "definedName",
        "name": "",
        "expression": "",
        "scope": "workbook",
        "localSheetId": None,
        "sheet": None,
        "sheetId": None,
        "sheetPart": None,
        "sheetState": None,
        "comment": None,
        "description": None,
        "hidden": False,
        "function": False,
        "vbProcedure": False,
        "xlm": False,
        "publishToServer": False,
        "workbookParameter": False,
        **overrides,
    }


def inventory(content: bytes) -> list[object]:
    return [
        item.structured_value
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


def placed(content: bytes) -> list[tuple[object, ...]]:
    """Every segment as (role, part, cell or name, visibility, method)."""
    return [
        (
            item.semantic_role,
            item.source_locator.get("part"),
            item.source_locator.get("cell") or item.source_locator.get("definedName"),
            item.visibility,
            item.extraction_method,
        )
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
    ]


def test_defined_names_follow_every_sheet_in_the_order_the_workbook_wrote() -> None:
    """Both scopes are reported once, after all sheet content, in source order.

    The local name is declared first to show the list is not re-sorted by
    scope, and it resolves its zero-based ordinal to that sheet's identity,
    part and state -- which is also where its ``hidden`` visibility comes from.
    The workbook-scoped name stores what looks like a reference into another
    workbook on disk: it is carried as the text it is, so the package yields no
    further segment and still reports no external reference.
    """
    content = xlsx(
        names(
            name(
                "_xlnm.Print_Area",
                "Archive!$A$1:$B$2",
                attrs=' localSheetId="1" comment="Rows to print"'
                ' description="Set on the sheet"',
            ),
            name("Rate", EXTERNAL),
        )
    )
    extraction = extract_xlsx(content, capture_id=CAPTURE)

    assert placed(content) == [
        ("table_cell", "xl/worksheets/sheet1.xml", "A1", "visible", "direct"),
        ("table_cell", "xl/worksheets/sheet2.xml", "A1", "hidden", "direct"),
        (
            "object_inventory",
            "xl/workbook.xml",
            "_xlnm.Print_Area",
            "hidden",
            "inventory",
        ),
        ("object_inventory", "xl/workbook.xml", "Rate", "visible", "inventory"),
    ]
    assert inventory(content) == [
        record(
            name="_xlnm.Print_Area",
            expression="Archive!$A$1:$B$2",
            scope="sheet",
            localSheetId=1,
            sheet="Archive",
            sheetId="9",
            sheetPart="xl/worksheets/sheet2.xml",
            sheetState="hidden",
            comment="Rows to print",
            description="Set on the sheet",
        ),
        record(name="Rate", expression=EXTERNAL),
    ]
    # The record's own key order, which dict equality above does not pin.
    assert list(inventory(content)[0]) == list(record())
    assert [
        dict(item.source_locator)
        for item in extraction.segments
        if item.semantic_role == "object_inventory"
    ] == [
        {
            "captureId": CAPTURE,
            "part": "xl/workbook.xml",
            "definedName": "_xlnm.Print_Area",
            "scope": "sheet",
        },
        {
            "captureId": CAPTURE,
            "part": "xl/workbook.xml",
            "definedName": "Rate",
            "scope": "workbook",
        },
    ]
    assert extraction.limitations["definedNames"] == 2
    assert extraction.limitations["externalReferences"] == 0
    assert extraction.expected == 2
    # The free text beside a name is neither an identity nor an expression, so
    # it is clipped to the metadata bound rather than refused with them.
    assert inventory(xlsx(names(name("Rate", attrs=f' comment="{"c" * 300}"')))) == [
        record(name="Rate", expression="Budget!$A$1", comment="c" * 256)
    ]


@pytest.mark.parametrize(
    ("attrs", "state", "visibility"),
    [
        ("", None, "visible"),
        (' hidden="1"', None, "hidden"),
        (' localSheetId="0"', "visible", "visible"),
        (' localSheetId="1"', "hidden", "hidden"),
        (' localSheetId="2"', "veryHidden", "hidden"),
        (' localSheetId="0" hidden="true"', "visible", "hidden"),
    ],
)
def test_a_name_is_out_of_sight_where_it_or_its_sheet_is(
    attrs: str, state: str | None, visibility: str
) -> None:
    """A name hides itself or inherits the sheet it scopes itself to."""
    content = xlsx(names(name("Rate", attrs=attrs)), sheets=DEEP)
    reported = inventory(content)

    assert [item["sheetState"] for item in reported] == [state]  # type: ignore[index]
    assert [
        item.visibility
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ] == [visibility]


def test_one_name_per_scope_however_it_is_cased() -> None:
    """Two scopes are two names; one scope names a name once, case-blind."""
    content = xlsx(
        names(
            name("Rate", attrs=' localSheetId="0"'),
            name("Rate", attrs=' localSheetId="1"'),
            name("Rate"),
        )
    )

    assert [
        (item["scope"], item["localSheetId"], item["sheet"])  # type: ignore[index]
        for item in inventory(content)
    ] == [("sheet", 0, "Budget"), ("sheet", 1, "Archive"), ("workbook", None, None)]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (name("Rate"), name("Rate")),
        (name("Rate"), name("RATE")),
        # Casefolding, not lowercasing: these are one name to a spreadsheet.
        (name("Stra\u00dfe"), name("STRASSE")),
        (
            name("Rate", attrs=' localSheetId="1"'),
            name("rate", attrs=' localSheetId="1"'),
        ),
    ],
)
def test_a_repeated_name_in_one_scope_is_refused(first: str, second: str) -> None:
    with pytest.raises(OOXMLInvalid, match="repeated in one scope"):
        extract_xlsx(xlsx(names(first, second)), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("declarations", "main", "reported"),
    [
        (names(name("Rate")), MAIN, 1),
        # The Strict profile's own namespace, read exactly as Transitional is.
        (names(name("Rate")), STRICT_MAIN, 1),
        # Unqualified: declares nothing, whatever it spells.
        (
            ('<definedNames xmlns=""><definedName name="Rate">A</definedName>'
            "</definedNames>"),
            MAIN,
            0,
        ),
        # Some other vocabulary that happens to spell the same local names.
        (
            ('<definedNames xmlns="urn:other"><definedName name="Rate">A</definedName>'
            "</definedNames>"),
            MAIN,
            0,
        ),
        # Strict holder under a Transitional root: not the root's own namespace.
        (
            (f'<definedNames xmlns="{STRICT_MAIN}">'
            '<definedName name="Rate">A</definedName></definedNames>'),
            MAIN,
            0,
        ),
        # Qualified holder, unqualified member.
        (
            ('<definedNames><definedName xmlns="" name="Rate">A</definedName>'
            "</definedNames>"),
            MAIN,
            0,
        ),
        # The holder one level deeper than the workbook.
        ("<extLst>" + names(name("Rate")) + "</extLst>", MAIN, 0),
        # The member one level deeper than the holder.
        ("<definedNames><group>" + name("Rate") + "</group></definedNames>", MAIN, 0),
        # A workbook that writes no namespace reaches no route at all.
        (names(name("Rate")), None, 0),
    ],
    ids=[
        "transitional",
        "strict",
        "unqualified-holder",
        "foreign-holder",
        "mismatched-profile",
        "unqualified-member",
        "nested-holder",
        "nested-member",
        "unqualified-workbook",
    ],
)
def test_only_the_qualified_direct_route_declares_a_name(
    declarations: str, main: str | None, reported: int
) -> None:
    content = xlsx(declarations, main=main)

    assert len(inventory(content)) == reported
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["definedNames"] == (
        reported
    )


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("<definedName>Budget!$A$1</definedName>", "missing, empty or over-long"),
        (
            '<definedName name="">Budget!$A$1</definedName>',
            "missing, empty or over-long",
        ),
        (name("n" * 257), "missing, empty or over-long"),
        (name("n" * 256), None),
        ('<definedName name="Rate"/>', "declares no expression"),
        ('<definedName name="Rate"></definedName>', "declares no expression"),
        (name("Rate", "A" * 8193), "expression exceeds the accepted bound"),
        (name("Rate", "A" * 8192), None),
    ],
    ids=[
        "no-name-attribute",
        "empty-name",
        "over-long-name",
        "longest-accepted-name",
        "no-expression",
        "empty-expression",
        "over-long-expression",
        "longest-accepted-expression",
    ],
)
def test_a_name_or_expression_outside_its_bound_is_refused(
    declaration: str, message: str | None
) -> None:
    """Neither identity nor expression is ever clipped down into acceptance."""
    content = xlsx(names(declaration))

    if message is not None:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    assert len(inventory(content)) == 1


@pytest.mark.parametrize(
    ("value", "ordinal"),
    [
        ("0", 0),
        ("1", 1),
        # Zero-padded, but still the same ordinal.
        ("01", 1),
        ("2", None),
        ("-1", None),
        ("", None),
        ("1.0", None),
        (" 0", None),
        ("0x1", None),
        ("+0", None),
        ("12345678", None),
        # Decimal, and ``int`` would take it; the reader's own digits do not.
        ("\u0661", None),
    ],
)
def test_a_local_scope_must_name_a_declared_sheet(
    value: str, ordinal: int | None
) -> None:
    content = xlsx(names(name("Rate", attrs=f' localSheetId="{value}"')))

    if ordinal is None:
        with pytest.raises(OOXMLInvalid, match="names no local sheet"):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    assert [
        (item["localSheetId"], item["sheet"])  # type: ignore[index]
        for item in inventory(content)
    ] == [(ordinal, SHEETS[ordinal][0])]


@pytest.mark.parametrize(
    ("value", "flag"),
    [
        ("1", True),
        ("true", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("off", False),
        ("True", None),
        ("TRUE", None),
        ("yes", None),
        ("2", None),
        ("", None),
    ],
)
def test_boolean_attributes_are_read_or_refused(value: str, flag: bool | None) -> None:
    """Only SpreadsheetML's own spellings; an absent one is a declared false."""
    content = xlsx(names(name("Rate", attrs=f' xlm="{value}"')))

    if flag is None:
        with pytest.raises(OOXMLInvalid, match="boolean attribute is malformed"):
            extract_xlsx(content, capture_id=CAPTURE)
        return
    assert inventory(content) == [
        record(name="Rate", expression="Budget!$A$1", xlm=flag)
    ]
    assert inventory(xlsx(names(name("Rate")))) == [
        record(name="Rate", expression="Budget!$A$1")
    ]


def test_a_second_name_list_and_the_count_bound_are_refused() -> None:
    """Two lists leave nothing to reconcile; the count precedes every read.

    The over-count declarations carry no name at all, so a refusal naming the
    count is the reader counting before it reads the first declaration.
    """
    with pytest.raises(OOXMLInvalid, match="more than one defined-name list"):
        extract_xlsx(
            xlsx(names(name("Rate")) + names(name("Cost"))), capture_id=CAPTURE
        )
    # Only the qualified list is a list, so an unqualified second one is not a
    # second list and the first is still read.
    assert (
        len(
            inventory(
                xlsx(
                    names(name("Rate"))
                    + '<definedNames xmlns=""><definedName name="Cost">A</definedName>'
                    "</definedNames>"
                )
            )
        )
        == 1
    )
    with pytest.raises(OOXMLInvalid, match="too many defined names"):
        extract_xlsx(
            xlsx(names("<definedName>A</definedName>" * 4097)), capture_id=CAPTURE
        )
    content = xlsx(names(*(name(f"Rate{index}") for index in range(4096))))

    assert len(inventory(content)) == 4096
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["definedNames"] == 4096
