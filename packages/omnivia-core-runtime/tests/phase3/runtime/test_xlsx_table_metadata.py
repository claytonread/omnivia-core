"""XLSX declared-table inventory and cell placement over minimal packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_xlsx

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
TABLE_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
WORKBOOK_RELS = (
    "<Relationships>"
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)
# Three declared columns over A:C, between them every optional column claim the
# reader reports: a totals-row label, a calculated-column formula, and a
# totals-row function beside its own formula text.
COLUMNS = (
    '<tableColumn id="1" name="Region" totalsRowLabel="Total"/>'
    '<tableColumn id="2" name="Units">'
    "<calculatedColumnFormula>Sales[Region]&amp;1</calculatedColumnFormula>"
    "</tableColumn>"
    '<tableColumn id="3" name="Total" totalsRowFunction="sum">'
    "<totalsRowFormula>SUBTOTAL(109,Sales[Units])</totalsRowFormula>"
    "</tableColumn>"
)
PLAIN = '<tableColumn id="1" name="Region"/><tableColumn id="2" name="Units"/>'


def table(
    ref: str,
    columns: str,
    *,
    identity: str = 'id="1" displayName="Sales"',
    attrs: str = "",
    main: str = MAIN,
    count: object = None,
) -> str:
    """One table part declaring ``columns`` over ``ref``.

    ``count`` replaces the declared column count, which otherwise follows the
    list, so a count that disagrees with the list or with the range can be
    declared on its own.
    """
    declared_count = columns.count("<tableColumn ") if count is None else count
    return (
        f'<table xmlns="{main}" {identity} ref="{ref}"{attrs}>'
        f'<tableColumns count="{declared_count}">{columns}'
        "</tableColumns></table>"
    )


def declared(*ids: str, holder: str = "tableParts", member: str = "tablePart") -> str:
    """The worksheet's own table-part list, naming each relationship id."""
    members = "".join(f'<{member} r:id="{rid}"/>' for rid in ids)
    return f'<{holder} count="{len(ids)}">{members}</{holder}>'


def xlsx(
    body: str,
    *,
    parts: dict[str, str] | None = None,
    rels: str | None = None,
    state: str | None = None,
    main: str = MAIN,
    relns: str = RELNS,
    content_type: str | None = TABLE_TYPE,
) -> bytes:
    """Build a one-sheet package whose table parts come from ``parts``.

    Each entry is a relationship id against the table XML it resolves to, and
    the content-type override, the sheet relationship and the member itself are
    all derived from that so only the declaration under test varies. ``rels``
    replaces the generated sheet relationships outright, and ``content_type``
    replaces the declared override for every table part -- ``None`` declares no
    override at all, leaving the part typed only by its ``.xml`` extension.
    """
    parts = parts or {}
    targets = {rid: f"tables/table{index}.xml" for index, rid in enumerate(parts, 1)}
    sheet_rels = (
        rels
        if rels is not None
        else "<Relationships>"
        + "".join(
            f'<Relationship Id="{rid}" Target="../{target}"'
            f' Type="{relns}/table"/>'
            for rid, target in targets.items()
        )
        + "</Relationships>"
    )
    attrs = f' state="{state}"' if state is not None else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            "<Types>"
            + "".join(
                f'<Override PartName="/xl/{target}" ContentType="{content_type}"/>'
                for target in targets.values()
                if content_type is not None
            )
            + "</Types>",
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns:r="{RELNS}"><sheets>'
            f'<sheet name="Budget" sheetId="7" r:id="rId1"{attrs}/>'
            "</sheets></workbook>",
        )
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}" xmlns:r="{relns}">{body}</worksheet>',
        )
        if parts or rels is not None:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
        for rid, target in targets.items():
            archive.writestr(f"xl/{target}", parts[rid])
    return buffer.getvalue()


def sheet(cells: str, *, after: str = "") -> str:
    return f"<sheetData>{cells}</sheetData>{after}"


def inventory(content: bytes) -> list[object]:
    return [
        item.structured_value
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


def placed(content: bytes) -> list[tuple[object, ...]]:
    """Each cell segment with the declared-table coordinates it carries."""
    return [
        (
            item.source_locator["cell"],
            item.semantic_role,
            item.source_locator.get("tableRow"),
            item.source_locator.get("tableColumn"),
            item.source_locator.get("tableColumnId"),
            item.source_locator.get("tableColumnName"),
            item.source_locator.get("tableRole"),
        )
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role != "object_inventory"
    ]


def test_declared_table_is_inventoried_and_places_the_cells_written_in_it() -> None:
    """The table is reported once; only written cells gain its coordinates.

    The range covers twelve positions and the sheet wrote four, so nothing in
    between is materialised, and a formula cell stays a formula beside its
    cached value rather than becoming table content.
    """
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Region</t></is></c></row>'
            '<row r="2"><c r="B2"><v>5</v></c></row>'
            '<row r="3"><c r="C3"><f>SUM(A3:B3)</f><v>7</v></c></row>'
            '<row r="4"><c r="A4" t="inlineStr"><is><t>Total</t></is></c></row>',
            after=declared("rIdT"),
        ),
        parts={"rIdT": table("A1:C4", COLUMNS, attrs=' totalsRowCount="1"')},
    )

    assert inventory(content) == [
        {
            "kind": "table",
            "relationshipId": "rIdT",
            "tablePart": "xl/tables/table1.xml",
            "id": "1",
            # Absent, so the schema's own default: the display name.
            "name": "Sales",
            "displayName": "Sales",
            "ref": "A1:C4",
            "headerRowCount": 1,
            "totalsRowCount": 1,
            "columnCount": 3,
            "columns": [
                {
                    "id": "1",
                    "name": "Region",
                    "totalsRowLabel": "Total",
                    "totalsRowFunction": None,
                    "calculatedColumnFormula": None,
                    "totalsRowFormula": None,
                },
                {
                    "id": "2",
                    "name": "Units",
                    "totalsRowLabel": None,
                    "totalsRowFunction": None,
                    "calculatedColumnFormula": "Sales[Region]&1",
                    "totalsRowFormula": None,
                },
                {
                    "id": "3",
                    "name": "Total",
                    "totalsRowLabel": None,
                    "totalsRowFunction": "sum",
                    "calculatedColumnFormula": None,
                    "totalsRowFormula": "SUBTOTAL(109,Sales[Units])",
                },
            ],
        }
    ]
    assert placed(content) == [
        ("A1", "table_cell", 1, 1, "1", "Region", "header"),
        ("B2", "table_cell", 2, 2, "2", "Units", "data"),
        ("C3", "formula", 3, 3, "3", "Total", "data"),
        ("C3", "saved_value", 3, 3, "3", "Total", "data"),
        ("A4", "table_cell", 4, 1, "1", "Region", "totals"),
    ]
    first = extract_xlsx(content, capture_id=CAPTURE).segments[0].source_locator
    assert first["tablePart"] == "xl/tables/table1.xml"
    assert first["tableId"] == "1"
    assert (first["tableName"], first["tableDisplayName"]) == ("Sales", "Sales")
    assert first["tableRange"] == "A1:C4"


def test_hidden_sheet_and_headerless_table_are_reported_as_declared() -> None:
    """The inventory follows the sheet's visibility; row 1 is data, not a header."""
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>'
            '<row r="2"><c r="A2"><v>2</v></c></row>',
            after=declared("rIdT"),
        ),
        parts={"rIdT": table("A1:B2", PLAIN, attrs=' headerRowCount="0"')},
        state="hidden",
    )
    record = extract_xlsx(content, capture_id=CAPTURE).segments[-1]

    assert record.semantic_role == "object_inventory"
    assert record.extraction_method == "inventory"
    assert record.text is None and record.visibility == "hidden"
    assert record.source_locator["cell"] == "A1:B2"
    assert record.source_locator["sheetState"] == "hidden"
    assert inventory(content)[0]["headerRowCount"] == 0  # type: ignore[index]
    assert [item[-1] for item in placed(content)] == ["data", "data"]


def test_strict_namespaces_and_relationship_type_resolve_the_same_table() -> None:
    """Strict is the other profile OOXML defines, not a foreign vocabulary."""
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>',
            after=declared("rIdT"),
        ),
        parts={"rIdT": table("A1:B1", PLAIN, main=STRICT_MAIN)},
        main=STRICT_MAIN,
        relns=STRICT_RELNS,
    )

    assert [(record["id"], record["ref"]) for record in inventory(content)] == [  # type: ignore[index]
        ("1", "A1:B1")
    ]
    assert placed(content) == [("A1", "table_cell", 1, 1, "1", "Region", "header")]


def test_table_part_lookalikes_declare_no_table() -> None:
    """Only the schema's own route declares a table: qualified, directly below.

    Each case below names a relationship that does resolve to a real table
    part, so anything reported came from the lookalike rather than from the
    package being short of one.
    """
    cases = {
        # Unqualified under a worksheet whose vocabulary is the main namespace.
        "unqualified": '<tableParts xmlns="" count="1"><tablePart r:id="rIdT"/>'
        "</tableParts>",
        # A foreign vocabulary that happens to spell the same two local names.
        "foreign": '<x:tableParts xmlns:x="urn:x" count="1">'
        '<x:tablePart r:id="rIdT"/></x:tableParts>',
        # Nested: the list below an extension, and the member below a wrapper.
        "nested": f"<extLst><ext>{declared('rIdT')}</ext></extLst>"
        + declared("rIdT", member="wrapped"),
    }

    def outcome(after: str) -> object:
        content = xlsx(
            sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=after),
            parts={"rIdT": table("A1:B1", PLAIN)},
        )
        try:
            return (inventory(content), [item[-1] for item in placed(content)])
        except OOXMLInvalid as error:  # pragma: no cover - reported as a failure
            return str(error)

    assert {name: outcome(after) for name, after in cases.items()} == {
        name: ([], [None]) for name in cases
    }


def test_overlapping_declared_tables_are_refused() -> None:
    """Two tables sharing B2 leave that cell in two tables, so the sheet is refused."""
    content = xlsx(
        sheet(
            '<row r="1"><c r="A1"><v>1</v></c></row>',
            after=declared("rIdA", "rIdB"),
        ),
        parts={
            "rIdA": table("A1:B2", PLAIN, identity='id="1" displayName="Alpha"'),
            "rIdB": table("B2:C3", PLAIN, identity='id="2" displayName="Beta"'),
        },
    )

    with pytest.raises(OOXMLInvalid, match="declared tables overlap"):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("rels", "message"),
    [
        pytest.param(
            "<Relationships>"
            '<Relationship Id="rIdT" Target="https://a.invalid/table1.xml"'
            f' Type="{RELNS}/table" TargetMode="External"/>'
            "</Relationships>",
            "table relationship is external",
            id="external",
        ),
        pytest.param("<Relationships/>", "missing relationship", id="missing"),
        pytest.param(
            "<Relationships>"
            '<Relationship Id="rIdT" Target="../tables/table1.xml"'
            f' Type="{RELNS}/hyperlink"/>'
            "</Relationships>",
            "relationship of another type",
            id="wrong-type",
        ),
    ],
)
def test_table_relationships_outside_the_profile_are_refused(
    rels: str, message: str
) -> None:
    content = xlsx(
        sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=declared("rIdT")),
        parts={"rIdT": table("A1:B1", PLAIN)},
        rels=rels,
    )

    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


# A table part's own column list, as a raw part so the holder itself can vary.
HOLDER = f'<tableColumns count="2">{PLAIN}</tableColumns>'
RAW = f'<table xmlns="{MAIN}" id="1" displayName="Sales" ref="A1:B1">{{}}</table>'
# A SpreadsheetML part that is not a table: the declared type promised one.
NOT_A_TABLE = (
    f'<queryTable xmlns="{MAIN}" id="1" displayName="Sales" ref="A1:B1">'
    f"{HOLDER}</queryTable>"
)
ONE = '<tableColumn id="1" name="Region"/>'


def refused(part: str, *, content_type: str | None = TABLE_TYPE) -> None:
    """Extract a one-sheet package declaring ``part`` as its only table."""
    extract_xlsx(
        xlsx(
            sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=declared("rIdT")),
            parts={"rIdT": part},
            content_type=content_type,
        ),
        capture_id=CAPTURE,
    )


@pytest.mark.parametrize(
    ("part", "content_type", "message"),
    [
        pytest.param(None, None, "does not name a table part", id="no-override"),
        pytest.param(
            None, "application/xml", "does not name a table part", id="plain-xml"
        ),
        pytest.param(
            None,
            TABLE_TYPE.replace("table+xml", "queryTable+xml"),
            "does not name a table part",
            id="query-table-type",
        ),
        pytest.param(None, TABLE_TYPE.upper(), "", id="uppercased-type"),
        pytest.param(
            table("A1:B1", PLAIN, main="urn:x"),
            TABLE_TYPE,
            "uses an unsupported namespace",
            id="foreign-root",
        ),
        pytest.param(
            table("A1:B1", PLAIN, main=""),
            TABLE_TYPE,
            "uses an unsupported namespace",
            id="unqualified-root",
        ),
        pytest.param(
            NOT_A_TABLE, TABLE_TYPE, "is not rooted at its own table", id="other-root"
        ),
    ],
)
def test_table_parts_outside_the_declared_profile_are_refused(
    part: str | None, content_type: str | None, message: str
) -> None:
    """A table part qualifies by its declared type and says so in its own root.

    The relationship has already been resolved by the time any of this is
    reached, so each case names a member that exists and is reachable: what is
    refused is the claim that the member is a table.

    An empty ``message`` is the one accepted case: OPC compares content types
    without regard to case, and the package reader folds both sides, so an
    uppercased override still declares a table part.
    """
    if not message:
        refused(part or table("A1:B1", PLAIN), content_type=content_type)
        return
    with pytest.raises(OOXMLInvalid, match=message):
        refused(part or table("A1:B1", PLAIN), content_type=content_type)


@pytest.mark.parametrize(
    ("ref", "identity", "attrs", "message"),
    [
        pytest.param("", 'id="1" displayName="Sales"', "", "declares no range", id="no-ref"),
        pytest.param(
            "B2:A1",
            'id="1" displayName="Sales"',
            "",
            "cell range is malformed",
            id="reversed-ref",
        ),
        pytest.param(
            "not-a-ref",
            'id="1" displayName="Sales"',
            "",
            "cell coordinate is malformed",
            id="unreadable-ref",
        ),
        pytest.param(
            "A1:B1", 'displayName="Sales"', "", "table id is malformed", id="no-id"
        ),
        pytest.param(
            "A1:B1",
            'id="x" displayName="Sales"',
            "",
            "table id is malformed",
            id="non-numeric-id",
        ),
        pytest.param(
            "A1:B1",
            'id="" displayName="Sales"',
            "",
            "table id is malformed",
            id="empty-id",
        ),
        pytest.param(
            "A1:B1",
            'id="12345678" displayName="Sales"',
            "",
            "table id is malformed",
            id="over-long-id",
        ),
        pytest.param("A1:B1", 'id="1"', "", "declares no display name", id="no-name"),
        pytest.param(
            "A1:B1",
            'id="1" displayName=""',
            "",
            "table name is empty or exceeds",
            id="empty-display-name",
        ),
        pytest.param(
            "A1:B1",
            'id="1" displayName="Sales" name=""',
            "",
            "table name is empty or exceeds",
            id="empty-name",
        ),
        pytest.param(
            "A1:B1",
            f'id="1" displayName="{"S" * 257}"',
            "",
            "exceeds the accepted bound",
            id="over-long-display-name",
        ),
        pytest.param(
            "A1:B1",
            'id="1" displayName="Sales"',
            ' headerRowCount="2"',
            "row count is not a defined value",
            id="two-header-rows",
        ),
        pytest.param(
            "A1:B1",
            'id="1" displayName="Sales"',
            ' totalsRowCount="x"',
            "row count is not a defined value",
            id="unreadable-totals-count",
        ),
        pytest.param(
            "A1:B1",
            'id="1" displayName="Sales"',
            ' totalsRowCount="-1"',
            "row count is not a defined value",
            id="negative-totals-count",
        ),
        pytest.param(
            "A1:B1",
            'id="1" displayName="Sales"',
            ' totalsRowCount="1"',
            "header and totals rows exceed its range",
            id="counts-over-range",
        ),
        pytest.param(
            "A1:B2",
            'id="1" displayName="Sales"',
            ' headerRowCount="1" totalsRowCount="1"',
            "",
            id="counts-filling-range",
        ),
    ],
)
def test_table_geometry_and_identity_claims_are_refused(
    ref: str, identity: str, attrs: str, message: str
) -> None:
    """A table's range, id and names are its own identity, so none is repaired.

    An empty ``message`` is the one case here that is accepted: two declared
    rows holding a header and a totals row fill the range exactly, which is the
    boundary the refusal above it sits one row past.
    """
    part = table(ref, PLAIN, identity=identity, attrs=attrs)
    if not message:
        refused(part)
        return
    with pytest.raises(OOXMLInvalid, match=message):
        refused(part)


@pytest.mark.parametrize(
    ("part", "message"),
    [
        pytest.param(RAW.format(""), "exactly one column list", id="no-holder"),
        pytest.param(RAW.format(HOLDER * 2), "exactly one column list", id="two-holders"),
        pytest.param(
            RAW.format(f'<x:tableColumns xmlns:x="urn:x" count="2">{PLAIN}</x:tableColumns>'),
            "exactly one column list",
            id="foreign-holder",
        ),
        pytest.param(
            table("A1:B1", PLAIN, count=""),
            "declares no readable count",
            id="empty-count",
        ),
        pytest.param(
            table("A1:B1", PLAIN, count="x"),
            "declares no readable count",
            id="unreadable-count",
        ),
        pytest.param(
            table("A1:B1", PLAIN, count=3),
            "count does not match its column list",
            id="count-over-list",
        ),
        pytest.param(
            table("A1:B1", PLAIN, count=1),
            "count does not match its column list",
            id="count-under-list",
        ),
        pytest.param(
            table("A1:C1", PLAIN), "does not match its range", id="list-under-range"
        ),
        pytest.param(table("A1:A1", PLAIN), "does not match its range", id="list-over-range"),
        pytest.param(
            table("A1:B1", f'<tableColumn name="Region"/>{ONE.replace("1", "2")}'),
            "column id is malformed",
            id="no-column-id",
        ),
        pytest.param(
            table("A1:B1", f'<tableColumn id="x" name="Region"/>{ONE.replace("1", "2")}'),
            "column id is malformed",
            id="unreadable-column-id",
        ),
        pytest.param(
            table("A1:B1", f'<tableColumn id="1"/>{ONE.replace("1", "2")}'),
            "column declares no name",
            id="no-column-name",
        ),
        pytest.param(
            table("A1:B1", f'<tableColumn id="1" name=""/>{ONE.replace("1", "2")}'),
            "table name is empty or exceeds",
            id="empty-column-name",
        ),
        pytest.param(
            table("A1:B1", f'{ONE}<tableColumn id="1" name="Units"/>'),
            "column id or name is repeated",
            id="repeated-column-id",
        ),
        pytest.param(
            table("A1:B1", f'{ONE}<tableColumn id="2" name="REGION"/>'),
            "column id or name is repeated",
            id="repeated-column-name",
        ),
        pytest.param(
            table(
                "A1:B1",
                '<tableColumn id="1" name="Region">'
                "<calculatedColumnFormula>A1</calculatedColumnFormula>"
                "<calculatedColumnFormula>B1</calculatedColumnFormula>"
                f'</tableColumn>{ONE.replace("1", "2").replace("Region", "Units")}',
            ),
            "declares a formula twice",
            id="repeated-calculated-formula",
        ),
        pytest.param(
            table(
                "A1:B1",
                '<tableColumn id="1" name="Region">'
                "<totalsRowFormula>A1</totalsRowFormula>"
                "<totalsRowFormula>B1</totalsRowFormula>"
                f'</tableColumn>{ONE.replace("1", "2").replace("Region", "Units")}',
            ),
            "declares a formula twice",
            id="repeated-totals-formula",
        ),
        pytest.param(
            table(
                "A1:B1",
                f'<x:tableColumn xmlns:x="urn:x" id="1" name="Region"/>'
                f'{ONE.replace("1", "2").replace("Region", "Units")}',
                count=2,
            ),
            "does not match its column list",
            id="foreign-column",
        ),
    ],
)
def test_table_column_list_claims_are_refused(part: str, message: str) -> None:
    """The column list is a table's own header, so a disagreeing one is refused.

    Three cases here probe the list's vocabulary rather than its contents: the
    part's root is held to SpreadsheetML, and the worksheet's own route to it is
    held to the sheet's namespace, so a foreign holder or member below a
    qualified root is the same kind of lookalike and should declare nothing.
    """
    with pytest.raises(OOXMLInvalid, match=message):
        refused(part)


@pytest.mark.parametrize(
    ("after", "parts", "message"),
    [
        pytest.param(
            declared("rIdT") * 2,
            {"rIdT": table("A1:B1", PLAIN)},
            "more than one table-part list",
            id="two-lists",
        ),
        pytest.param(
            declared("rIdT", "rIdT"),
            {"rIdT": table("A1:B1", PLAIN)},
            "declaration is repeated",
            id="repeated-relationship",
        ),
        pytest.param(
            '<tableParts count="1"><tablePart/></tableParts>',
            {"rIdT": table("A1:B1", PLAIN)},
            "names no relationship",
            id="no-relationship-id",
        ),
        pytest.param(
            declared("rIdA", "rIdB"),
            {
                "rIdA": table("A1:B1", PLAIN, identity='id="1" displayName="Alpha"'),
                "rIdB": table("A3:B3", PLAIN, identity='id="1" displayName="Beta"'),
            },
            "table identity is repeated",
            id="repeated-id",
        ),
        pytest.param(
            declared("rIdA", "rIdB"),
            {
                "rIdA": table("A1:B1", PLAIN, identity='id="1" displayName="Sales"'),
                "rIdB": table("A3:B3", PLAIN, identity='id="2" displayName="SALES"'),
            },
            "table identity is repeated",
            id="repeated-display-name",
        ),
        pytest.param(
            declared("rIdA", "rIdB"),
            {
                "rIdA": table(
                    "A1:B1", PLAIN, identity='id="1" displayName="Alpha" name="Sales"'
                ),
                "rIdB": table(
                    "A3:B3", PLAIN, identity='id="2" displayName="Beta" name="sales"'
                ),
            },
            "table identity is repeated",
            id="repeated-name",
        ),
        pytest.param(
            declared("rIdA", "rIdB"),
            {
                "rIdA": table("A1:B1", PLAIN, identity='id="1" displayName="Sales"'),
                "rIdB": table(
                    "A3:B3", PLAIN, identity='id="2" displayName="Beta" name="Sales"'
                ),
            },
            "table identity is repeated",
            id="name-collides-with-defaulted-name",
        ),
    ],
)
def test_repeated_table_declarations_are_refused(
    after: str, parts: dict[str, str], message: str
) -> None:
    """One sheet declares each table once and each identity once.

    Every pair here sits on disjoint ranges, so the overlap refusal cannot be
    what fires: what is refused is a cell citing a table that is two tables.
    """
    content = xlsx(
        sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=after), parts=parts
    )

    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("after", "part", "message"),
    [
        pytest.param(
            declared(*(f"rId{index}" for index in range(257))),
            None,
            "declares too many tables",
            id="too-many-tables",
        ),
        pytest.param(
            None,
            table("A1:B1", PLAIN, count=257),
            "too many columns",
            id="declared-column-count-over-bound",
        ),
        pytest.param(
            None,
            table(
                "A1:IW1",
                "".join(
                    f'<tableColumn id="{index}" name="c{index}"/>'
                    for index in range(1, 258)
                ),
            ),
            "too many columns",
            id="column-list-over-bound",
        ),
        pytest.param(
            None,
            table(
                "A1:A1",
                '<tableColumn id="1" name="R">'
                f'<calculatedColumnFormula>{"A" * 16_400}</calculatedColumnFormula>'
                "</tableColumn>",
            ),
            "metadata exceeds the accepted bound",
            id="metadata-over-bound",
        ),
        pytest.param(
            None,
            table(
                "A1:A1",
                '<tableColumn id="1" name="R" totalsRowLabel="T">'
                f'<calculatedColumnFormula>{"A" * 8_200}</calculatedColumnFormula>'
                f'<totalsRowFormula>{"B" * 8_200}</totalsRowFormula>'
                "</tableColumn>",
            ),
            "metadata exceeds the accepted bound",
            id="metadata-summed-over-bound",
        ),
    ],
)
def test_table_and_column_bounds_are_refused(
    after: str | None, part: str | None, message: str
) -> None:
    """The reader's own bounds, each taken before the work it would admit.

    The table count is taken before any part is opened and the column count
    before the list is read, so neither bound is reached by first doing the work
    it exists to refuse. The metadata bound is taken across the whole table,
    because one record carries all of it.
    """
    parts = {} if part is None else {"rIdT": part}
    content = xlsx(
        sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=after or declared("rIdT")),
        parts=parts,
    )

    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


def test_tables_and_metadata_at_the_bounds_are_accepted() -> None:
    """The bounds admit what sits exactly on them, so neither is off by one.

    256 columns is the whole of a legacy grid's width, and the metadata bound is
    taken as "exceeds", so a table whose declared metadata lands on it is read.
    """
    wide = xlsx(
        sheet(
            '<row r="1"><c r="IV1"><v>1</v></c></row>',
            after=declared("rIdT"),
        ),
        parts={
            "rIdT": table(
                "A1:IV1",
                "".join(
                    f'<tableColumn id="{index}" name="c{index}"/>'
                    for index in range(1, 257)
                ),
                attrs=' headerRowCount="0"',
            )
        },
    )
    assert inventory(wide)[0]["columnCount"] == 256  # type: ignore[index]
    assert placed(wide) == [("IV1", "table_cell", 1, 256, "256", "c256", "data")]

    # One id, one name and a formula filling the rest of the accepted metadata.
    exact = xlsx(
        sheet('<row r="1"><c r="A1"><v>1</v></c></row>', after=declared("rIdT")),
        parts={
            "rIdT": table(
                "A1:A1",
                '<tableColumn id="1" name="R">'
                f'<calculatedColumnFormula>{"A" * (16 * 1024 - 2)}</calculatedColumnFormula>'
                "</tableColumn>",
                attrs=' headerRowCount="0"',
            )
        },
    )
    assert len(inventory(exact)[0]["columns"][0]["calculatedColumnFormula"]) == 16_382  # type: ignore[index]
