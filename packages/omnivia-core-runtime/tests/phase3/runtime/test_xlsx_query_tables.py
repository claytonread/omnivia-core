"""XLSX query tables: an inventory of the saved state, never a refresh.

A query table is where a workbook keeps what a data query last returned into a
range or into a declared table. Design §11.7 asks for the references to the
connections behind them and says the same thing about all of them: Power Query
and data connections are never refreshed. What the file stored is still state a
consumer has to be told about, so it is disclosed, and disclosed honestly --
every record says the freshness is unknown and that nothing was refreshed and
nothing outside the package was reached.

So the contract here is deliberately narrow. The route is the owner's own
qualifying relationship, because SpreadsheetML gives a query table no
declaration element in the worksheet's markup: the worksheet may relate one for
a query over a range of the sheet, and a declared table may relate one for a
table whose rows a query returns. A member that merely looks like a query table
is inert, and a relationship of another type reaches nothing.

What comes out is the saved declaration: the identity, the connection it cites,
the refresh and formatting policy it declared, and counts of the fields its
saved refresh listed. What never comes out is the query itself -- no connection
string, no command, no parameter, no credential, no external target, no cached
value, no formula, no field name, and nothing from an extension payload, which
is where a Power Query definition lives. Nothing is executed, opened, resolved
or recalculated, and ``refreshPerformed`` and ``externalAccessPerformed`` are
false on every record.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"

WORKBOOK_PART = "xl/workbook.xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
SHEET_RELS = "xl/worksheets/_rels/sheet1.xml.rels"
TABLE_PART = "xl/tables/table1.xml"
TABLE_RELS = "xl/tables/_rels/table1.xml.rels"
SHEET_QUERY_PART = "xl/queryTables/queryTable1.xml"
TABLE_QUERY_PART = "xl/queryTables/queryTable2.xml"
CONNECTIONS_PART = "xl/connections.xml"

# Written here in OOXML's own camel case, as a package would; the reader folds
# what the package declared before comparing, so these reach it folded.
QUERY_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.queryTable+xml"
)
TABLE_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
CONNECTIONS_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml"
)

# Everything a query table's record may never carry. The connection string, the
# command and the web URL live in the connections part the query table cites;
# the field names, the deleted field's name and the sort range live in its own
# saved refresh; and the M expression lives in the extension payload this
# increment does not interpret. None of it is reported anywhere.
SECRETS = (
    "Provider=SQLOLEDB;Data Source=finance-db;Password=hunter2",
    "SELECT * FROM dbo.Margins",
    "https://intranet.example.invalid/feed.csv",
    "Sales district",
    "Net amount",
    "Removed margin",
    "D2:E9",
    "Web.Contents",
)

# The whole attribute surface OOXML gives a query table, written out so the
# record has something to be wrong about for every one of them.
QUERY_ATTRIBUTES = (
    'name="Regional sales" connectionId="3" headers="1" rowNumbers="0"'
    ' disableRefresh="0" backgroundRefresh="1" firstBackgroundRefresh="0"'
    ' refreshOnLoad="1" fillFormulas="0" removeDataOnSave="1" disableEdit="1"'
    ' preserveFormatting="1" adjustColumnWidth="0" intermediate="0"'
    ' applyNumberFormats="1" applyBorderFormats="0" applyFontFormats="1"'
    ' applyPatternFormats="0" applyAlignmentFormats="1"'
    ' applyWidthHeightFormats="0" growShrinkType="insertClear"'
    ' autoFormatId="16"'
)
# One saved refresh, holding exactly what a record counts and nothing it reads.
REFRESH = (
    "<queryTableRefresh>"
    '<queryTableFields count="2">'
    '<queryTableField id="1" name="Sales district" tableColumnId="1"/>'
    '<queryTableField id="2" name="Net amount" tableColumnId="2"/>'
    "</queryTableFields>"
    '<queryTableDeletedFields count="1">'
    '<deletedField name="Removed margin"/>'
    "</queryTableDeletedFields>"
    '<sortState ref="D2:E9"><sortCondition ref="D2:D9"/></sortState>'
    "</queryTableRefresh>"
)
# The extension point, carrying the Power Query definition a real workbook keeps
# there. Admitted and left alone: this increment interprets no payload.
MASHUP = (
    '<extLst><ext uri="urn:test:mashup"><mashup>'
    'let Source = Web.Contents("https://example.invalid/secret") in Source'
    "</mashup></ext></extLst>"
)
# The second query table, the one a declared table relates: a different
# connection, no formatting policy and no saved refresh at all, so the default
# fixture pins both the declared and the undeclared shape of each group.
TABLE_QUERY_ATTRIBUTES = 'name="Order lines" connectionId="4" headers="0"'

# The sentinel the builder takes for "write the default part". ``None`` means
# relate no such part at all, which is the absent route the empty inventory has
# to stay distinguishable from, so the default cannot be spelled that way; and
# the default follows ``main``, so it cannot be a constant either.
DEFAULT = "#default"

CONNECTIONS = (
    '<connections xmlns="{main}">'
    '<connection id="3" name="Finance warehouse" type="1" refreshedVersion="6"'
    ' background="1">'
    '<dbPr connection="Provider=SQLOLEDB;Data Source=finance-db;Password=hunter2"'
    ' command="SELECT * FROM dbo.Margins"/>'
    "</connection>"
    '<connection id="4" name="Order feed" type="4" refreshedVersion="6">'
    '<webPr url="https://intranet.example.invalid/feed.csv"/>'
    "</connection>"
    "</connections>"
)


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def query_table(
    attributes: str = QUERY_ATTRIBUTES,
    body: str = REFRESH,
    *,
    main: str = MAIN,
    root: str = "queryTable",
) -> str:
    """One query-table part: ``attributes`` on its root and ``body`` inside it."""
    return f'<{root} xmlns="{main}" {attributes}>{body}</{root}>'


def table(main: str = MAIN) -> str:
    """The declared table the sheet relates, which relates a query table itself."""
    return (
        f'<table xmlns="{main}" id="1" name="OrderLines" displayName="OrderLines"'
        ' ref="A1:B2"><tableColumns count="2">'
        '<tableColumn id="1" name="District"/><tableColumn id="2" name="Amount"/>'
        "</tableColumns></table>"
    )


def worksheet(body: str, *, main: str = MAIN, relns: str = RELNS) -> str:
    """One sheet with one written cell, and ``body`` where its declarations sit."""
    return (
        f'<worksheet xmlns="{main}" xmlns:r="{relns}"><sheetData>'
        '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
        f"</sheetData>{body}</worksheet>"
    )


def relationships(
    declared: tuple[tuple[str, str, str], ...] = (),
    external: tuple[tuple[str, str, str], ...] = (),
) -> str:
    """A relationship part holding ``(id, target, type)`` in the order given.

    Anything in ``external`` is declared with ``TargetMode="External"``, which
    is the only way to name something outside the package: an absolute target
    written as an internal relationship escapes the package instead, and is
    refused long before any route resolves it.
    """
    return (
        "<Relationships>"
        + "".join(
            f'<Relationship Id="{identity}" Target="{target}" Type="{kind}"/>'
            for identity, target, kind in declared
        )
        + "".join(
            f'<Relationship Id="{identity}" Target="{target}" Type="{kind}"'
            ' TargetMode="External"/>'
            for identity, target, kind in external
        )
        + "</Relationships>"
    )


def xlsx(
    *,
    sheet_query: str | None = DEFAULT,
    table_query: str | None = DEFAULT,
    declared_table: str | None = DEFAULT,
    connections: str | None = DEFAULT,
    main: str = MAIN,
    relns: str = RELNS,
    sheet_query_type: str | None = None,
    table_query_type: str | None = None,
    sheet_query_ct: str = QUERY_CT,
    table_query_ct: str = QUERY_CT,
    sheet_rels: str | None = None,
    table_rels: str | None = None,
    sheet_state: str = "visible",
    extra_queries: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """One sheet relating a query table, and a declared table relating another.

    Both owner routes are written by default, so the ordinary case exercises the
    two of them at once. ``sheet_query`` or ``table_query`` of ``None`` drops
    that part and the relationship to it, which is the absent route the empty
    inventory has to stay distinguishable from; ``declared_table`` of ``None``
    drops the table, and the table's route with it.

    ``extra_queries`` writes further query-table parts, each declared with the
    query-table content type, for the authored ``sheet_rels`` or ``table_rels``
    of a package relating more than one of them to route to.
    """
    if sheet_query == DEFAULT:
        sheet_query = query_table(main=main)
    if table_query == DEFAULT:
        table_query = query_table(TABLE_QUERY_ATTRIBUTES, "", main=main)
    if declared_table == DEFAULT:
        declared_table = table(main)
    if connections == DEFAULT:
        connections = CONNECTIONS.format(main=main)
    overrides = {
        **{
            name: content_type
            for name, content_type, present in (
                (CONNECTIONS_PART, CONNECTIONS_CT, connections is not None),
                (TABLE_PART, TABLE_CT, declared_table is not None),
                (SHEET_QUERY_PART, sheet_query_ct, sheet_query is not None),
                (TABLE_QUERY_PART, table_query_ct, table_query is not None),
            )
            if present
        },
        **{name: QUERY_CT for name in extra_queries or {}},
    }
    members = {
        "[Content_Types].xml": "<Types>"
        + "".join(
            f'<Override PartName="/{name}" ContentType="{content_type}"/>'
            for name, content_type in overrides.items()
        )
        + "</Types>",
        WORKBOOK_PART: (
            f'<workbook xmlns="{main}" xmlns:r="{relns}"><sheets>'
            f'<sheet name="Sales" sheetId="7" state="{sheet_state}" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": relationships(
            (
                ("rId1", "worksheets/sheet1.xml", f"{relns}/worksheet"),
                *(
                    ()
                    if connections is None
                    else (("rIdConn", "connections.xml", f"{relns}/connections"),)
                ),
            )
        ),
        SHEET_PART: worksheet(
            ""
            if declared_table is None
            else '<tableParts count="1"><tablePart r:id="rIdTable0"/></tableParts>',
            main=main,
            relns=relns,
        ),
    }
    if declared_table is not None or sheet_query is not None:
        members[SHEET_RELS] = sheet_rels or relationships(
            (
                *(
                    ()
                    if declared_table is None
                    else (("rIdTable0", "../tables/table1.xml", f"{relns}/table"),)
                ),
                *(
                    ()
                    if sheet_query is None
                    else (
                        (
                            "rIdQueryZ",
                            "../queryTables/queryTable1.xml",
                            sheet_query_type or f"{relns}/queryTable",
                        ),
                    )
                ),
            )
        )
    if declared_table is not None:
        members[TABLE_PART] = declared_table
        members[TABLE_RELS] = table_rels or relationships(
            ()
            if table_query is None
            else (
                (
                    "rIdQueryA",
                    "../queryTables/queryTable2.xml",
                    table_query_type or f"{relns}/queryTable",
                ),
            )
        )
    if sheet_query is not None:
        members[SHEET_QUERY_PART] = sheet_query
    if table_query is not None:
        members[TABLE_QUERY_PART] = table_query
    if connections is not None:
        members[CONNECTIONS_PART] = connections
    return archive({**members, **(extra_queries or {}), **(extra or {})})


def owners(declared: tuple[tuple[str, str], ...], *, sheet_query: bool = True) -> bytes:
    """A sheet declaring several tables, each relating the query table it names.

    Each entry is ``(relationship id, query-table target)``. The sheet declares
    its tables in the order given while the ids run the other way, so an
    inventory that followed the ids rather than the declarations reports them
    reversed. The sheet's own query table, where it has one, is related under
    the last id of all.
    """
    parts = {
        f"xl/tables/table{index}.xml": (
            f'<table xmlns="{MAIN}" id="{index}" name="T{index}"'
            f' displayName="T{index}" ref="{chr(65 + index)}1:{chr(65 + index)}2">'
            '<tableColumns count="1">'
            f'<tableColumn id="1" name="C{index}"/></tableColumns></table>'
        )
        for index, _entry in enumerate(declared)
    }
    queries = {
        f"xl/queryTables/{target}": query_table(
            f'name="{target}" connectionId="3"', ""
        )
        for _identity, target in declared
    }
    if sheet_query:
        queries[f"xl/queryTables/{SHEET_QUERY_PART.rsplit('/', 1)[1]}"] = query_table(
            'name="Sheet own" connectionId="3"', ""
        )
    members = {
        "[Content_Types].xml": "<Types>"
        + "".join(
            f'<Override PartName="/{name}" ContentType="{TABLE_CT}"/>' for name in parts
        )
        + "".join(
            f'<Override PartName="/{name}" ContentType="{QUERY_CT}"/>' for name in queries
        )
        + "</Types>",
        WORKBOOK_PART: (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
            '<sheet name="Sales" sheetId="7" state="visible" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": relationships(
            (("rId1", "worksheets/sheet1.xml", f"{RELNS}/worksheet"),)
        ),
        SHEET_PART: worksheet(
            f'<tableParts count="{len(declared)}">'
            + "".join(
                f'<tablePart r:id="rIdTable{index}"/>'
                for index, _entry in enumerate(declared)
            )
            + "</tableParts>"
        ),
        SHEET_RELS: relationships(
            (
                *(
                    (f"rIdTable{index}", f"../tables/table{index}.xml", f"{RELNS}/table")
                    for index, _entry in enumerate(declared)
                ),
                *(
                    (
                        ("rIdQueryZ", "../queryTables/queryTable1.xml", f"{RELNS}/queryTable"),
                    )
                    if sheet_query
                    else ()
                ),
            )
        ),
        **parts,
        **queries,
        **{
            f"xl/tables/_rels/table{index}.xml.rels": relationships(
                ((identity, f"../queryTables/{target}", f"{RELNS}/queryTable"),)
            )
            for index, (identity, target) in enumerate(declared)
        },
    }
    return archive(members)


def inventory(content: bytes) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Every query-table inventory record, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and (item.structured_value or {}).get("kind") == "queryTable"
    ]


# Pinned by full equality so a field this route starts reporting -- or stops --
# fails here rather than passing unnoticed.
SHEET_WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "sheetPart": SHEET_PART,
    "sheetId": "7",
    "sheet": "Sales",
    "sheetState": "visible",
    "queryTablePart": SHEET_QUERY_PART,
    "queryTableRelationshipId": "rIdQueryZ",
    "queryTableIndex": 0,
}
# The table route names the table it was reached through beside the sheet. The
# absence of those two keys is what says a record came from the sheet's own
# relationship instead, so nothing invents a word for either case.
TABLE_WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "sheetPart": SHEET_PART,
    "sheetId": "7",
    "sheet": "Sales",
    "sheetState": "visible",
    "tablePart": TABLE_PART,
    "tableRelationshipId": "rIdTable0",
    "queryTablePart": TABLE_QUERY_PART,
    "queryTableRelationshipId": "rIdQueryA",
    "queryTableIndex": 1,
}
SHEET_RECORD: dict[str, object] = {
    "kind": "queryTable",
    "name": "Regional sales",
    "connectionId": 3,
    "connectionsPart": CONNECTIONS_PART,
    "connectionIndex": 0,
    "definitionPart": SHEET_QUERY_PART,
    "growShrinkType": "insertClear",
    "autoFormatId": 16,
    "freshness": "unknown",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "headers": True,
    "rowNumbers": False,
    "disableRefresh": False,
    "backgroundRefresh": True,
    "firstBackgroundRefresh": False,
    "refreshOnLoad": True,
    "fillFormulas": False,
    "removeDataOnSave": True,
    "disableEdit": True,
    "preserveFormatting": True,
    "adjustColumnWidth": False,
    "intermediate": False,
    "applyNumberFormats": True,
    "applyBorderFormats": False,
    "applyFontFormats": True,
    "applyPatternFormats": False,
    "applyAlignmentFormats": True,
    "applyWidthHeightFormats": False,
    "refreshDeclared": True,
    "refreshSortDeclared": True,
    "refreshFieldCount": 2,
    "refreshDeletedFieldCount": 1,
}
# The same shape with nothing declared: every flag the file left out is None
# rather than the schema's default, because a default is not a declaration.
TABLE_RECORD: dict[str, object] = {
    **{key: None for key in SHEET_RECORD},
    "kind": "queryTable",
    "name": "Order lines",
    "connectionId": 4,
    "connectionsPart": CONNECTIONS_PART,
    "connectionIndex": 1,
    "definitionPart": TABLE_QUERY_PART,
    "freshness": "unknown",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "headers": False,
    "refreshDeclared": False,
}


# --------------------------------------------------------------------------
# What an ordinary query table reports


def test_a_query_table_from_each_owner_is_inventoried_exactly() -> None:
    """The whole surface of both records, pinned by full equality."""
    assert inventory(xlsx()) == [
        (SHEET_WHERE, SHEET_RECORD),
        (TABLE_WHERE, TABLE_RECORD),
    ]


def test_the_query_table_count_is_reported_in_the_extraction_metadata() -> None:
    """Its own figure: a connection may serve several query tables, or none."""
    limitations = extract_xlsx(xlsx(), capture_id=CAPTURE).limitations

    assert limitations["queryTables"] == 2
    assert limitations["connections"] == 2


def test_query_records_are_inert_inventory_in_a_settled_order() -> None:
    """The sheet's content, its table, its query tables, then the workbook's."""
    segments = extract_xlsx(xlsx(), capture_id=CAPTURE).segments

    assert [item.semantic_role for item in segments] == [
        "table_cell",
        "object_inventory",
        "object_inventory",
        "object_inventory",
        "object_inventory",
        "object_inventory",
    ]
    assert [(item.structured_value or {}).get("kind") for item in segments[1:]] == [
        "table",
        "queryTable",
        "queryTable",
        "connection",
        "connection",
    ]
    for item in segments[2:4]:
        assert item.extraction_method == "inventory"
        assert item.visibility == "visible"
        assert item.text is None


def test_no_refresh_or_external_access_is_ever_claimed() -> None:
    """The three standing claims, on every record this route emits."""
    for _where, record in inventory(xlsx()):
        assert record["freshness"] == "unknown"
        assert record["refreshPerformed"] is False
        assert record["externalAccessPerformed"] is False


def test_nothing_outside_the_package_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof by refusal: every way out of the process is made to raise.

    A query table is a saved query over a database, a web feed or a Power Query
    source. Reading one has to stay an inert read of package members, so the
    inventory is taken with the filesystem, the network and the process launcher
    all unavailable.
    """
    import builtins
    import socket
    import subprocess
    import urllib.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the query table inventory reached outside the package")

    # The package is already a ``BytesIO``, so nothing legitimate opens a file.
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    assert len(inventory(xlsx())) == 2


def test_no_query_field_parameter_or_payload_is_ever_reported() -> None:
    """The query's own content, absent from every record, locator and segment."""
    extraction = extract_xlsx(
        xlsx(sheet_query=query_table(body=REFRESH + MASHUP)), capture_id=CAPTURE
    )
    rendered = repr(
        [
            (item.semantic_role, item.source_locator, item.structured_value, item.text)
            for item in extraction.segments
        ]
    )

    for secret in SECRETS:
        assert secret not in rendered
        assert secret not in repr(extraction.limitations)


def test_an_extension_payload_is_admitted_and_left_uninterpreted() -> None:
    """A Power Query definition under the extension point changes no record."""
    assert inventory(xlsx(sheet_query=query_table(body=MASHUP + REFRESH))) == [
        (SHEET_WHERE, SHEET_RECORD),
        (TABLE_WHERE, TABLE_RECORD),
    ]


# --------------------------------------------------------------------------
# Transitional and Strict parity


def test_a_strict_workbook_reports_what_a_transitional_one_does() -> None:
    """Both profiles, same records: only the namespaces and URIs differ."""
    assert inventory(xlsx(main=STRICT_MAIN, relns=STRICT_RELNS)) == [
        (SHEET_WHERE, SHEET_RECORD),
        (TABLE_WHERE, TABLE_RECORD),
    ]


def test_a_strict_package_may_relate_its_parts_by_the_transitional_uris() -> None:
    """The profile of the markup and of the relationship are independent."""
    content = xlsx(
        main=STRICT_MAIN,
        relns=STRICT_RELNS,
        sheet_query_type=f"{RELNS}/queryTable",
        table_query_type=f"{RELNS}/queryTable",
    )

    assert inventory(content) == [
        (SHEET_WHERE, SHEET_RECORD),
        (TABLE_WHERE, TABLE_RECORD),
    ]


def test_a_transitional_package_may_relate_its_parts_by_the_strict_uris() -> None:
    """And the other way round, for the same reason."""
    content = xlsx(
        sheet_query_type=f"{STRICT_RELNS}/queryTable",
        table_query_type=f"{STRICT_RELNS}/queryTable",
    )

    assert inventory(content) == [
        (SHEET_WHERE, SHEET_RECORD),
        (TABLE_WHERE, TABLE_RECORD),
    ]


def test_the_profile_fixtures_bind_r_id_to_their_own_relationship_namespace() -> None:
    """Guards the parity tests: a mis-bound ``r`` would prove nothing.

    Each profile's declarations -- the workbook's ``sheet`` and the sheet's
    ``tablePart``, which is how the table owner is reached at all -- have to
    carry ``r:id`` in that profile's own office-document relationship
    namespace, or a reader keyed to the other one would pass above for the wrong
    reason. The query-table parts themselves carry no ``r:id``, so their own
    main namespace is what is checked instead.
    """
    for main, relns, other in (
        (MAIN, RELNS, STRICT_RELNS),
        (STRICT_MAIN, STRICT_RELNS, RELNS),
    ):
        package = zipfile.ZipFile(io.BytesIO(xlsx(main=main, relns=relns)))
        for part in (WORKBOOK_PART, SHEET_PART):
            markup = package.read(part).decode()
            assert f'xmlns:r="{relns}"' in markup
            assert other not in markup
            assert 'r:id="' in markup
        assert "<tableParts" in package.read(SHEET_PART).decode()
        for part in (SHEET_QUERY_PART, TABLE_QUERY_PART):
            assert f'xmlns="{main}"' in package.read(part).decode()


# --------------------------------------------------------------------------
# The two owner routes, and neither of them derived


def test_a_query_table_the_sheet_alone_relates_is_inventoried() -> None:
    """No declared table at all, so the sheet's own route is the whole of it."""
    assert inventory(xlsx(declared_table=None)) == [(SHEET_WHERE, SHEET_RECORD)]


def test_a_query_table_a_declared_table_alone_relates_is_inventoried() -> None:
    """The sheet relates none, so the table route carries the only record."""
    assert inventory(xlsx(sheet_query=None)) == [
        ({**TABLE_WHERE, "queryTableIndex": 0}, TABLE_RECORD)
    ]


def test_a_sheet_that_relates_no_query_table_reports_none() -> None:
    """The absent route: a sheet and a table, neither relating a query table."""
    content = xlsx(sheet_query=None, table_query=None)

    assert inventory(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["queryTables"] == 0


def test_a_query_table_member_no_owner_relates_is_inert() -> None:
    """A part declared with the query-table type that nothing relates.

    It is in the package and the content types name it, so a reader that
    derived a path, listed members by type or asked a convenience API for the
    part would report it. The route is the owner's own relationship, so it
    reaches nothing at all.
    """
    content = xlsx(
        sheet_query=None,
        table_query=None,
        extra={
            "[Content_Types].xml": (
                "<Types>"
                f'<Override PartName="/{TABLE_PART}" ContentType="{TABLE_CT}"/>'
                f'<Override PartName="/{CONNECTIONS_PART}"'
                f' ContentType="{CONNECTIONS_CT}"/>'
                f'<Override PartName="/{SHEET_QUERY_PART}" ContentType="{QUERY_CT}"/>'
                "</Types>"
            ),
            SHEET_QUERY_PART: query_table(),
        },
    )

    assert inventory(content) == []


def test_a_table_part_the_sheet_never_declared_reaches_nothing() -> None:
    """A table definition is an owner only where the sheet declared it.

    The table part relates a query table and the package holds both, but the
    sheet writes no ``tableParts``, so the table was never reached and neither
    is anything hanging off it.
    """
    content = xlsx(
        sheet_query=None,
        extra={
            SHEET_PART: worksheet(""),
            SHEET_RELS: relationships(()),
        },
    )

    assert inventory(content) == []


def test_a_relationship_of_another_type_reaches_no_query_table() -> None:
    """A table, pivot-table or connections relationship qualifies as nothing."""
    for kind in ("table", "pivotTable", "connections", "externalLink", "queryTables"):
        assert inventory(xlsx(sheet_query_type=f"{RELNS}/{kind}", table_query=None)) == []


def test_a_lookalike_relationship_uri_reaches_no_query_table() -> None:
    """Only the two URIs OOXML defines qualify, not anything ending the same way."""
    for kind in (
        "http://example.invalid/officeDocument/relationships/queryTable",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/myQueryTable",
        f"{RELNS}/queryTable/",
        "queryTable",
    ):
        assert inventory(xlsx(sheet_query_type=kind, table_query=None)) == []


# --------------------------------------------------------------------------
# Order, and a part two owners reach


def test_the_sheets_own_route_is_reported_before_its_declared_tables() -> None:
    """Source order, not relationship-id order.

    The sheet declares ``table0`` before ``table1`` while their query
    relationships are ``rIdQueryB`` and ``rIdQueryA``, and the sheet's own is
    ``rIdQueryZ``. Sorting ids anywhere -- in the sheet's relationships or in
    the tables' -- reports these in another order than this.
    """
    content = owners((("rIdQueryB", "first.xml"), ("rIdQueryA", "second.xml")))

    assert [
        (where["queryTableIndex"], where["queryTablePart"], record["name"])
        for where, record in inventory(content)
    ] == [
        (0, "xl/queryTables/queryTable1.xml", "Sheet own"),
        (1, "xl/queryTables/first.xml", "first.xml"),
        (2, "xl/queryTables/second.xml", "second.xml"),
    ]


def several(
    sheet: tuple[tuple[str, str], ...] = (),
    table: tuple[tuple[str, str], ...] = (),
    *,
    root: str = "queryTable",
) -> bytes:
    """A sheet and its one declared table, each relating the routes it is given.

    Each entry is ``(relationship id, query-table member name)``, authored in
    the order given. The ids chosen in these tests sort the other way round, so
    an inventory that read the relationship mapping in sorted order rather than
    the order the part was written in reports them reversed.

    ``root`` renames the root element of every query-table part written, which
    is how a package whose routes all refuse on opening is built: what is
    reported then says whether the routes were bounded before any was opened.
    """
    return xlsx(
        sheet_query=None,
        table_query=None,
        extra_queries={
            f"xl/queryTables/{name}": query_table(
                f'name="{name}" connectionId="3"', "", root=root
            )
            for name in sorted({name for _identity, name in (*sheet, *table)})
        },
        sheet_rels=relationships(
            (
                ("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),
                *(
                    (identity, f"../queryTables/{name}", f"{RELNS}/queryTable")
                    for identity, name in sheet
                ),
            )
        ),
        table_rels=relationships(
            tuple(
                (identity, f"../queryTables/{name}", f"{RELNS}/queryTable")
                for identity, name in table
            )
        ),
    )


def reached(content: bytes) -> list[tuple[object, object, object]]:
    """Each record's position, the part it names and the owner it came through."""
    return [
        (
            where["queryTableIndex"],
            where["queryTablePart"],
            where.get("tableRelationshipId"),
        )
        for where, _record in inventory(content)
    ]


def test_several_query_tables_one_worksheet_relates_are_all_inventoried() -> None:
    """A worksheet owns a collection, not at most one.

    SpreadsheetML relates a worksheet one query table per range a query was
    written over, so two qualifying relationships are two query tables rather
    than an ambiguity. They come out in the order the relationship part
    authored them -- ``rIdQueryZ`` before ``rIdQueryA``, which is not the order
    the ids sort in -- and at consecutive positions.
    """
    content = several((("rIdQueryZ", "alpha.xml"), ("rIdQueryA", "beta.xml")))

    assert reached(content) == [
        (0, "xl/queryTables/alpha.xml", None),
        (1, "xl/queryTables/beta.xml", None),
    ]
    assert [
        where["queryTableRelationshipId"] for where, _record in inventory(content)
    ] == ["rIdQueryZ", "rIdQueryA"]


def test_several_query_tables_one_declared_table_relates_are_all_inventoried() -> None:
    """The table owner holds a collection on the same terms the worksheet does."""
    content = several(table=(("rIdQueryB", "gamma.xml"), ("rIdQueryA", "delta.xml")))

    assert reached(content) == [
        (0, "xl/queryTables/gamma.xml", "rIdTable0"),
        (1, "xl/queryTables/delta.xml", "rIdTable0"),
    ]
    assert [where["tablePart"] for where, _record in inventory(content)] == [
        TABLE_PART,
        TABLE_PART,
    ]


def test_every_owners_routes_are_reported_in_owner_then_authored_order() -> None:
    """Owner order outside, authored relationship order within each owner.

    The sheet's own routes come first, then its declared table's, and inside
    each the order that owner's relationship part wrote. Sorting ids at either
    level -- across the owners or within one -- reports these another way.
    """
    content = several(
        (("rIdQueryZ", "alpha.xml"), ("rIdQueryA", "beta.xml")),
        (("rIdQueryY", "gamma.xml"), ("rIdQueryB", "delta.xml")),
    )

    assert reached(content) == [
        (0, "xl/queryTables/alpha.xml", None),
        (1, "xl/queryTables/beta.xml", None),
        (2, "xl/queryTables/gamma.xml", "rIdTable0"),
        (3, "xl/queryTables/delta.xml", "rIdTable0"),
    ]


def test_the_routes_of_every_owner_are_bounded_before_any_part_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is the sheet's and its tables' routes together, counted first.

    Every part here refuses on opening, so the refusal says which happened: the
    bound where the four routes are over it, and the part's own refusal where
    they are not.
    """
    content = several(
        (("rIdQueryZ", "alpha.xml"), ("rIdQueryA", "beta.xml")),
        (("rIdQueryY", "gamma.xml"), ("rIdQueryB", "delta.xml")),
        root="table",
    )

    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_QUERY_TABLES", 3)
    with pytest.raises(OOXMLInvalid, match="reaches too many query tables"):
        inventory(content)

    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_QUERY_TABLES", 4)
    with pytest.raises(OOXMLInvalid, match="not rooted at its own query table"):
        inventory(content)


def test_two_routes_from_one_owner_that_name_one_part_are_refused() -> None:
    """Several routes are several query tables, and never one twice over.

    A repeated relationship id is refused where every relationship part is
    validated, so what is left for this check is two distinct ids resolving to
    one part, on either kind of owner.
    """
    for kwargs in (
        {"sheet": (("rIdQueryZ", "alpha.xml"), ("rIdQueryY", "alpha.xml"))},
        {"table": (("rIdQueryB", "gamma.xml"), ("rIdQueryA", "gamma.xml"))},
    ):
        with pytest.raises(OOXMLInvalid, match="query table part is referenced twice"):
            inventory(several(**kwargs))  # type: ignore[arg-type]


def test_a_relationship_of_another_type_beside_several_routes_stays_inert() -> None:
    """Two qualifying routes are read; the one of another type reaches nothing."""
    content = xlsx(
        sheet_query=None,
        table_query=None,
        extra_queries={
            "xl/queryTables/alpha.xml": query_table('name="alpha" connectionId="3"', ""),
            "xl/queryTables/beta.xml": query_table('name="beta" connectionId="3"', ""),
        },
        sheet_rels=relationships(
            (
                ("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),
                ("rIdQueryZ", "../queryTables/alpha.xml", f"{RELNS}/queryTable"),
                ("rIdOther", "../queryTables/beta.xml", f"{RELNS}/pivotTable"),
            )
        ),
        table_rels=relationships(()),
    )

    assert reached(content) == [(0, "xl/queryTables/alpha.xml", None)]


def test_a_package_of_several_routes_inventories_identically_every_time() -> None:
    """No set iteration and no sorted mapping stands in for the authored order."""
    content = several(
        (("rIdQueryZ", "alpha.xml"), ("rIdQueryA", "beta.xml")),
        (("rIdQueryY", "gamma.xml"), ("rIdQueryB", "delta.xml")),
    )
    first = inventory(content)

    assert all(inventory(content) == first for _ in range(3))


def test_a_query_table_two_owners_reach_is_refused() -> None:
    """One part is one query table, and the second route is refused.

    Two declared tables relating one part would otherwise be inventoried twice
    over, once under each owner. The check is the sheet's own, as a pivot
    table's is, and it is taken while the routes are collected: neither part is
    opened before the second route is found.
    """
    content = owners((("rIdQueryB", "shared.xml"), ("rIdQueryA", "shared.xml")))

    with pytest.raises(OOXMLInvalid, match="query table part is referenced twice"):
        inventory(content)


def test_a_query_table_the_sheet_and_its_table_both_reach_is_refused() -> None:
    """The same invariant across the two kinds of owner."""
    content = xlsx(
        table_rels=relationships(
            (("rIdQueryA", "../queryTables/queryTable1.xml", f"{RELNS}/queryTable"),)
        ),
        table_query=None,
    )

    with pytest.raises(OOXMLInvalid, match="query table part is referenced twice"):
        inventory(content)


def test_a_query_table_two_sheets_reach_is_reported_under_each() -> None:
    """The stated limit of the invariant: it is one sheet's, not the package's.

    A table definition belongs to the sheet that declared it, so the duplicate
    check is taken per sheet. Two sheets relating one part is a package that
    says the query table is in both places, and each record names the sheet it
    was reached through rather than one standing in for the other.
    """
    content = xlsx(
        declared_table=None,
        extra={
            WORKBOOK_PART: (
                f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
                '<sheet name="Sales" sheetId="7" state="visible" r:id="rId1"/>'
                '<sheet name="Costs" sheetId="8" state="visible" r:id="rId2"/>'
                "</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": relationships(
                (
                    ("rId1", "worksheets/sheet1.xml", f"{RELNS}/worksheet"),
                    ("rId2", "worksheets/sheet2.xml", f"{RELNS}/worksheet"),
                    ("rIdConn", "connections.xml", f"{RELNS}/connections"),
                )
            ),
            "xl/worksheets/sheet2.xml": worksheet(""),
            "xl/worksheets/_rels/sheet2.xml.rels": relationships(
                (
                    (
                        "rIdQueryZ",
                        "../queryTables/queryTable1.xml",
                        f"{RELNS}/queryTable",
                    ),
                )
            ),
        },
    )

    assert [where["sheet"] for where, _record in inventory(content)] == [
        "Sales",
        "Costs",
    ]


def test_the_same_package_inventories_identically_every_time() -> None:
    """No set iteration, no dictionary order and no path derivation anywhere."""
    content = xlsx()
    first = inventory(content)

    assert all(inventory(content) == first for _ in range(3))


# --------------------------------------------------------------------------
# The connection a query table cites is named, not opened


def test_a_cited_connection_is_linked_by_the_id_it_declares() -> None:
    """Each record names where the workbook declared the connection it cites."""
    assert [
        (record["connectionId"], record["connectionsPart"], record["connectionIndex"])
        for _where, record in inventory(xlsx())
    ] == [(3, CONNECTIONS_PART, 0), (4, CONNECTIONS_PART, 1)]


def test_an_undeclared_connection_id_stays_explicitly_unlinked() -> None:
    """The cited id is reported; the link is absent rather than invented.

    The workbook declares connections 3 and 4, so a query table citing 11 cites
    one that is not there. Nothing derives a connections part from the id,
    nothing picks a connection that happens to be in the package, and nothing
    reaches outside to resolve it.
    """
    content = xlsx(
        sheet_query=query_table('name="Regional sales" connectionId="11"', ""),
        table_query=None,
    )
    (_where, record), = inventory(content)

    assert record["connectionId"] == 11
    assert record["connectionsPart"] is None
    assert record["connectionIndex"] is None


def test_a_workbook_with_no_connections_part_leaves_every_citation_unlinked() -> None:
    """A query table is still the inventory it is with nothing to link to."""
    records = [record for _where, record in inventory(xlsx(connections=None))]

    assert [record["connectionId"] for record in records] == [3, 4]
    assert [record["connectionsPart"] for record in records] == [None, None]
    assert [record["connectionIndex"] for record in records] == [None, None]


# --------------------------------------------------------------------------
# Visibility


def test_a_query_table_on_a_hidden_sheet_is_hidden() -> None:
    """It takes the state of the sheet it was reached through, as written."""
    for state in ("hidden", "veryHidden"):
        records = [
            item
            for item in extract_xlsx(
                xlsx(sheet_state=state), capture_id=CAPTURE
            ).segments
            if (item.structured_value or {}).get("kind") == "queryTable"
        ]

        assert [item.visibility for item in records] == ["hidden", "hidden"]
        assert [item.source_locator["sheetState"] for item in records] == [state] * 2


# --------------------------------------------------------------------------
# Foreign and unqualified lookalikes declare nothing


@pytest.mark.parametrize(
    ("body", "declared", "sort", "fields", "deleted"),
    [
        pytest.param(REFRESH, True, True, 2, 1, id="qualified"),
        pytest.param("", False, None, None, None, id="nothing-declared"),
        pytest.param(
            '<queryTableRefresh xmlns=""><queryTableFields count="0"/>'
            "</queryTableRefresh>",
            False,
            None,
            None,
            None,
            id="unqualified-refresh",
        ),
        pytest.param(
            '<queryTableRefresh xmlns="urn:test"/>',
            False,
            None,
            None,
            None,
            id="foreign-refresh",
        ),
        pytest.param(
            f'<wrapper xmlns="{MAIN}"><queryTableRefresh/></wrapper>',
            False,
            None,
            None,
            None,
            id="nested-refresh",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}"/>',
            True,
            False,
            None,
            None,
            id="refresh-declaring-no-list",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableFields count="0"/>'
            '<queryTableFields xmlns="urn:test" count="9"/>'
            "</queryTableRefresh>",
            True,
            False,
            0,
            None,
            id="foreign-second-field-list",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<sortState xmlns="urn:test"/>'
            "</queryTableRefresh>",
            True,
            False,
            None,
            None,
            id="foreign-sort-state",
        ),
    ],
)
def test_only_a_qualified_refresh_declaration_is_read(
    body: str, declared: bool, sort: bool | None, fields: int | None, deleted: int | None
) -> None:
    """A lookalike written elsewhere declares no refresh state and no counts.

    The query table's own part carries no prefix, so an unqualified child is in
    no namespace at all rather than in the part's; nothing below the root is
    read by local name.
    """
    content = xlsx(sheet_query=query_table(body=body), table_query=None)
    (_where, record), = inventory(content)

    assert record["refreshDeclared"] is declared
    assert record["refreshSortDeclared"] is sort
    assert record["refreshFieldCount"] == fields
    assert record["refreshDeletedFieldCount"] == deleted


def test_a_foreign_field_member_occupies_no_position() -> None:
    """A list's count is what it declared in its own vocabulary, and no more."""
    content = xlsx(
        sheet_query=query_table(
            body=(
                f'<queryTableRefresh xmlns="{MAIN}"><queryTableFields count="1">'
                '<queryTableField id="1" name="Sales district"/>'
                '<queryTableField xmlns="urn:test" id="2" name="Net amount"/>'
                "</queryTableFields></queryTableRefresh>"
            )
        ),
        table_query=None,
    )
    (_where, record), = inventory(content)

    assert record["refreshFieldCount"] == 1


# --------------------------------------------------------------------------
# Malformed declarations are refused


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        pytest.param('name="Q"', "connection id is missing", id="no-connection-id"),
        pytest.param(
            'name="Q" connectionId=""',
            "connection id is missing or malformed",
            id="empty-connection-id",
        ),
        pytest.param(
            'name="Q" connectionId="-1"',
            "connection id is missing or malformed",
            id="negative-connection-id",
        ),
        pytest.param(
            'name="Q" connectionId="1.5"',
            "connection id is missing or malformed",
            id="fractional-connection-id",
        ),
        pytest.param(
            'name="Q" connectionId="4294967296"',
            "connection id is missing or malformed",
            id="oversized-connection-id",
        ),
        pytest.param(
            'connectionId="3" growShrinkType="insertOnly"',
            "grow-shrink type is not a defined value",
            id="undefined-grow-shrink",
        ),
        pytest.param(
            'connectionId="3" growShrinkType=""',
            "grow-shrink type is not a defined value",
            id="empty-grow-shrink",
        ),
        pytest.param(
            'connectionId="3" autoFormatId="-1"',
            "numeric attribute is malformed",
            id="negative-auto-format",
        ),
        pytest.param(
            'connectionId="3" autoFormatId="4294967296"',
            "numeric attribute is malformed",
            id="oversized-auto-format",
        ),
        pytest.param(
            'connectionId="3" refreshOnLoad="yes"',
            "query table flag is malformed",
            id="undefined-flag",
        ),
        pytest.param(
            'connectionId="3" applyFontFormats="2"',
            "query table flag is malformed",
            id="numeric-flag",
        ),
    ],
)
def test_a_malformed_query_table_declaration_is_refused(
    attributes: str, message: str
) -> None:
    """Each case names something the type does not have, so none is read as one."""
    content = xlsx(sheet_query=query_table(attributes, ""), table_query=None)

    with pytest.raises(OOXMLInvalid, match=message):
        inventory(content)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}"/><queryTableRefresh xmlns="{MAIN}"/>',
            "declares its refresh state twice",
            id="two-refresh-states",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableFields count="0"/><queryTableFields count="0"/>'
            "</queryTableRefresh>",
            "refresh declares a field list twice",
            id="two-field-lists",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            "<queryTableDeletedFields/><queryTableDeletedFields/>"
            "</queryTableRefresh>",
            "refresh declares a field list twice",
            id="two-deleted-field-lists",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<sortState ref="D2:E9"/><sortState ref="D2:E9"/>'
            "</queryTableRefresh>",
            "declares its sort state twice",
            id="two-sort-states",
        ),
        pytest.param(
            f'<extLst xmlns="{MAIN}"/><extLst xmlns="{MAIN}"/>',
            "query table holds more than one extension list",
            id="two-extension-lists",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            f'<extLst xmlns="{MAIN}"/><extLst xmlns="{MAIN}"/>'
            "</queryTableRefresh>",
            "query table refresh holds more than one extension list",
            id="two-refresh-extension-lists",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableFields count="3">'
            '<queryTableField id="1"/><queryTableField id="2"/>'
            "</queryTableFields></queryTableRefresh>",
            "field count does not match its field list",
            id="count-over-list",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableDeletedFields count="0"><deletedField/>'
            "</queryTableDeletedFields></queryTableRefresh>",
            "field count does not match its field list",
            id="count-under-list",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableFields count="-1"/></queryTableRefresh>',
            "too many fields or no count",
            id="negative-count",
        ),
        pytest.param(
            f'<queryTableRefresh xmlns="{MAIN}">'
            '<queryTableFields count="16385"/></queryTableRefresh>',
            "too many fields or no count",
            id="oversized-count",
        ),
    ],
)
def test_a_malformed_saved_refresh_is_refused(body: str, message: str) -> None:
    """Each case leaves two declarations at one position, or two field counts."""
    content = xlsx(
        sheet_query=query_table('connectionId="3"', body), table_query=None
    )

    with pytest.raises(OOXMLInvalid, match=message):
        inventory(content)


def test_an_absent_field_count_is_the_list_the_file_wrote() -> None:
    """``count`` is optional, so a list without one is counted as it stands."""
    content = xlsx(
        sheet_query=query_table(
            'connectionId="3"',
            f'<queryTableRefresh xmlns="{MAIN}"><queryTableFields>'
            '<queryTableField id="1"/><queryTableField id="2"/>'
            '<queryTableField id="3"/></queryTableFields></queryTableRefresh>',
        ),
        table_query=None,
    )
    (_where, record), = inventory(content)

    assert record["refreshFieldCount"] == 3


# --------------------------------------------------------------------------
# Wrong roots, wrong content types, wrong relationships


@pytest.mark.parametrize(
    ("part", "message"),
    [
        pytest.param(
            query_table(main="urn:test"),
            "query table part uses an unsupported namespace",
            id="foreign-namespace",
        ),
        pytest.param(
            f'<queryTable {QUERY_ATTRIBUTES}/>',
            "query table part uses an unsupported namespace",
            id="no-namespace",
        ),
        pytest.param(
            query_table(root="queryTableRefresh"),
            "not rooted at its own query table",
            id="wrong-root",
        ),
        pytest.param(
            query_table(root="table"),
            "not rooted at its own query table",
            id="rooted-at-a-table",
        ),
    ],
)
def test_a_query_table_part_that_is_not_one_is_refused(part: str, message: str) -> None:
    """The declared type is the claim; the root is where the part makes it."""
    with pytest.raises(OOXMLInvalid, match=message):
        inventory(xlsx(sheet_query=part, table_query=None))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param(
            {"sheet_query_ct": TABLE_CT},
            "query table relationship does not name a query table part",
            id="declared-as-a-table",
        ),
        pytest.param(
            {"sheet_query_ct": CONNECTIONS_CT},
            "query table relationship does not name a query table part",
            id="declared-as-connections",
        ),
        pytest.param(
            {
                "sheet_rels": relationships(
                    (("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),),
                    (
                        (
                            "rIdQueryZ",
                            "https://example.invalid/query.xml",
                            f"{RELNS}/queryTable",
                        ),
                    ),
                )
            },
            "query table relationship is external",
            id="external-route",
        ),
        pytest.param(
            {
                "sheet_rels": relationships(
                    (
                        ("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),
                        (
                            "rIdQueryZ",
                            "../queryTables/queryTable1.xml",
                            f"{RELNS}/queryTable",
                        ),
                        # A second qualifying route on the same owner, which is
                        # admitted now -- but this one names a part the package
                        # declares as a table, so it is not a query table and
                        # the sheet is refused rather than read past it.
                        (
                            "rIdQueryY",
                            "../tables/table1.xml",
                            f"{RELNS}/queryTable",
                        ),
                    )
                )
            },
            "query table relationship does not name a query table part",
            id="wrong-content-route-among-several",
        ),
        pytest.param(
            {
                "sheet_rels": relationships(
                    (
                        ("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),
                        (
                            "rIdQueryZ",
                            "../queryTables/queryTable1.xml",
                            f"{RELNS}/queryTable",
                        ),
                    ),
                    (
                        (
                            "rIdQueryY",
                            "https://example.invalid/query.xml",
                            f"{RELNS}/queryTable",
                        ),
                    ),
                )
            },
            "query table relationship is external",
            id="external-route-among-several",
        ),
    ],
)
def test_a_query_table_route_that_resolves_to_nothing_usable_is_refused(
    kwargs: dict[str, str], message: str
) -> None:
    """Each case leaves which part is meant, or whether it is one, unsettled."""
    with pytest.raises(OOXMLInvalid, match=message):
        inventory(xlsx(table_query=None, **kwargs))  # type: ignore[arg-type]


def test_a_route_that_leaves_the_package_is_refused_before_it_resolves() -> None:
    """Containment is settled where every relationship is, not on this route.

    The target names a path outside the package, so it is refused as the
    relationship it is rather than reaching a query table or the filesystem.
    """
    content = xlsx(
        table_query=None,
        sheet_rels=relationships(
            (
                ("rIdTable0", "../tables/table1.xml", f"{RELNS}/table"),
                ("rIdQueryZ", "../../../etc/passwd", f"{RELNS}/queryTable"),
            )
        ),
    )

    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        inventory(content)


def test_an_external_route_from_a_declared_table_is_refused() -> None:
    """Nothing outside the package is a query table, and none is fetched."""
    content = xlsx(
        sheet_query=None,
        table_rels=relationships(
            (),
            (
                (
                    "rIdQueryA",
                    "https://example.invalid/query.xml",
                    f"{RELNS}/queryTable",
                ),
            ),
        ),
    )

    with pytest.raises(OOXMLInvalid, match="query table relationship is external"):
        inventory(content)


# --------------------------------------------------------------------------
# Bounds


@pytest.mark.parametrize(
    ("constant", "bound", "message"),
    [
        pytest.param("_MAX_XLSX_QUERY_TABLES", 2, "", id="query-tables-at-bound"),
        pytest.param(
            "_MAX_XLSX_QUERY_TABLES",
            1,
            "reaches too many query tables",
            id="query-tables-over-bound",
        ),
        pytest.param(
            "_MAX_XLSX_QUERY_TABLE_FIELDS", 2, "", id="refresh-fields-at-bound"
        ),
        pytest.param(
            "_MAX_XLSX_QUERY_TABLE_FIELDS",
            1,
            "too many fields or no count",
            id="refresh-fields-over-bound",
        ),
    ],
)
def test_a_sheet_past_a_query_table_bound_is_refused(
    monkeypatch: pytest.MonkeyPatch, constant: str, bound: int, message: str
) -> None:
    """Each bound holds at its figure and refuses the one past it.

    The query-table bound is taken over the routes the sheet and its declared
    tables hold together, before any of their parts is opened; the field bound
    is taken on one saved refresh's own list.
    """
    monkeypatch.setattr(ooxml_extractors, constant, bound)

    if not message:
        assert len(inventory(xlsx())) == 2
        return
    with pytest.raises(OOXMLInvalid, match=message):
        inventory(xlsx())


def test_an_over_long_query_table_name_is_clipped_rather_than_refused() -> None:
    """A name is not an identity here, so nothing cites it and nothing collides."""
    content = xlsx(
        sheet_query=query_table(f'name="{"n" * 400}" connectionId="3"', ""),
        table_query=None,
    )
    (_where, record), = inventory(content)

    assert record["name"] == "n" * 256


def test_a_query_table_that_names_itself_nothing_keeps_the_claim_it_made() -> None:
    """An empty name is the empty claim the file wrote; an absent one is None."""
    for attributes, expected in (
        ('name="" connectionId="3"', ""),
        ('connectionId="3"', None),
    ):
        content = xlsx(sheet_query=query_table(attributes, ""), table_query=None)
        (_where, record), = inventory(content)

        assert record["name"] == expected
