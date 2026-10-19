"""XLSX extension coverage below the roots: every reached part reports its lists.

``test_xlsx_extension_coverage.py`` pins the worksheet and workbook roots. This
file pins the rest of the contract: a part the reader opens by a validated
route -- the shared strings, a table, a query table, the connections, comments,
threaded comments, persons, styles, pivot parts and an external link -- reports
the extension lists its own vocabulary allows, once per part, addressed by the
structural path of the owning element and never by anything the source wrote.
The nested owners of the workbook and of every sheet kind are pinned too.

The drawing and chart parts are DrawingML rather than SpreadsheetML, so they are
pinned next door in ``test_xlsx_drawing_extensions.py``. Not covered, and
deliberately not asserted as covered: the parts the reader never opens
(revision logs, the calculation chain, cell metadata, volatile types,
single-cell XML).
"""

from __future__ import annotations

import builtins
import io
import socket
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
ox = ooxml_extractors

SECRETS = (
    "hunter2",
    "Web.Contents",
    "SELECT * FROM dbo.Margins",
    "https://example.invalid/secret",
    "token=abc123",
    "=SUM(A1:A9)",
    "Quarterly forecast",
    "cached-value-42",
)
LEAKY = (
    '<x14:payload xmlns:x14="urn:x14" credential="hunter2">'
    "<x14:f>=SUM(A1:A9)</x14:f><x14:note>Quarterly forecast</x14:note>"
    '<extLst><ext uri="{DEADBEEF-0000-0000-0000-000000000000}"/></extLst>'
    "</x14:payload>"
    '<mashup>let Source = Web.Contents("https://example.invalid/secret") in Source'
    " SELECT * FROM dbo.Margins token=abc123</mashup><v>cached-value-42</v>"
)
PERSON = "{11111111-1111-1111-1111-111111111111}"
COMMENT = "{22222222-2222-2222-2222-222222222222}"


def guid(n: int) -> str:
    return f"{{{n:08X}-0000-0000-0000-000000000000}}"


def ext(uri: str | None, body: str = "") -> str:
    attribute = "" if uri is None else f' uri="{uri}"'
    return f"<ext{attribute}>{body}</ext>"


def lst(*uris: str | None, body: str = "") -> str:
    return "<extLst>" + "".join(ext(u, body) for u in uris) + "</extLst>"


def xlst(*uris: str | None, ns: str = MAIN, entry: str = "x:ext") -> str:
    """A threaded ``extLst`` (in the default namespace) with qualified entries."""
    items = "".join(
        f'<{entry} xmlns:x="{ns}"' + ("" if u is None else f' uri="{u}"') + "/>"
        for u in uris
    )
    return f"<extLst>{items}</extLst>"


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


# part kind -> (relationship type, content type)
KINDS: dict[str, tuple[str, str]] = {
    "table": (f"{RELNS}/table", ox._TABLE_CONTENT_TYPE),
    "queryTable": (f"{RELNS}/queryTable", ox._XLSX_QUERY_TABLE_CONTENT_TYPE),
    "comments": (f"{RELNS}/comments", ox._XLSX_COMMENTS_CONTENT_TYPE),
    "threaded": (
        "http://schemas.microsoft.com/office/2017/10/relationships/threadedComment",
        ox._XLSX_THREADED_CONTENT_TYPE,
    ),
    "person": (
        "http://schemas.microsoft.com/office/2017/10/relationships/person",
        ox._XLSX_PERSONS_CONTENT_TYPE,
    ),
    "connections": (f"{RELNS}/connections", ox._XLSX_CONNECTIONS_CONTENT_TYPE),
    "externalLink": (f"{RELNS}/externalLink", ox._XLSX_EXTERNAL_LINK_CONTENT_TYPE),
    "styles": (f"{RELNS}/styles", ox._XLSX_STYLES_CONTENT_TYPE),
    "pivotCache": (f"{RELNS}/pivotCacheDefinition", ox._XLSX_PIVOT_CACHE_CONTENT_TYPE),
    "pivotRecords": (f"{RELNS}/pivotCacheRecords", ox._XLSX_PIVOT_RECORDS_CONTENT_TYPE),
    "pivotTable": (f"{RELNS}/pivotTable", ox._XLSX_PIVOT_TABLE_CONTENT_TYPE),
    "sharedStrings": (f"{RELNS}/sharedStrings", ox._XLSX_SHARED_STRINGS_CONTENT_TYPE),
    "sharedStringsStrict": (
        "http://purl.oclc.org/ooxml/officeDocument/relationships/sharedStrings",
        ox._XLSX_SHARED_STRINGS_CONTENT_TYPE,
    ),
}


def build(
    parts: dict[str, tuple[str, str]],
    rels: dict[str, list[tuple[str, str, str]]],
    *,
    sheets: int = 1,
    states: tuple[str, ...] = (),
    workbook_tail: str = "",
    tails: dict[int, str] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """A workbook of ``sheets`` sheets plus ``parts`` (path -> (kind, xml)).

    ``rels`` maps a source part to its (id, kind, target) relationships, the
    target written relative to the source's own directory. Each part's content
    type is declared outright, as the validated routes require.
    """
    states = states + ("visible",) * (sheets - len(states))
    declared = "".join(
        f'<sheet name="S{n}" sheetId="{n}" state="{states[n - 1]}" r:id="rId{n}"/>'
        for n in range(1, sheets + 1)
    )
    members: dict[str, str] = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}">'
            f"<sheets>{declared}</sheets>{workbook_tail}</workbook>"
        ),
    }
    for n in range(1, sheets + 1):
        members[f"xl/worksheets/sheet{n}.xml"] = (
            f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}"><sheetData>'
            '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
            f"</sheetData>{(tails or {}).get(n, '')}</worksheet>"
        )
    overrides = ""
    for path, (kind, xml) in parts.items():
        members[path] = xml
        overrides += f'<Override PartName="/{path}" ContentType="{KINDS[kind][1]}"/>'
    members["[Content_Types].xml"] = f"<Types>{overrides}</Types>"
    every: dict[str, list[tuple[str, str, str]]] = {
        "xl/workbook.xml": [
            (f"rId{n}", f"{RELNS}/worksheet", f"worksheets/sheet{n}.xml")
            for n in range(1, sheets + 1)
        ]
    }
    for source, entries in rels.items():
        every.setdefault(source, []).extend(entries)
    for source, entries in every.items():
        folder, _, name = source.rpartition("/")
        members[f"{folder}/_rels/{name}.rels"] = (
            "<Relationships>"
            + "".join(
                f'<Relationship Id="{i}" Type="{t}" Target="{target}"'
                + (' TargetMode="External"' if target.startswith("http") else "")
                + "/>"
                for i, t, target in entries
            )
            + "</Relationships>"
        )
    return archive({**members, **(extra or {})})


def rel(kind: str, rid: str, target: str) -> tuple[str, str, str]:
    return (rid, KINDS[kind][0], target)


def records(content: bytes) -> list[tuple[dict[str, object], dict[str, object], str]]:
    return [
        (dict(item.source_locator), dict(item.structured_value or {}), item.visibility)
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]


def brief(content: bytes) -> list[tuple[object, ...]]:
    return [
        (r["owner"], r["ownerPath"], r["extensionIndex"], r["uri"], r["scope"])
        for _, r, _ in records(content)
    ]


def table_xml(columns: str = "", tail: str = "", head: str = "") -> str:
    return (
        f'<table xmlns="{MAIN}" id="1" name="T" displayName="T" ref="A1:B2">{head}'
        f'<tableColumns count="2"><tableColumn id="1" name="a"/>'
        f'<tableColumn id="2" name="b">{columns}</tableColumn></tableColumns>'
        f"{tail}</table>"
    )


TABLE_TAIL = '<tableParts count="1"><tablePart r:id="rIdT"/></tableParts>'


def with_table(xml: str, **more: object) -> bytes:
    return build(
        {"xl/tables/table1.xml": ("table", xml)},
        {"xl/worksheets/sheet1.xml": [rel("table", "rIdT", "../tables/table1.xml")]},
        tails={1: TABLE_TAIL},
        **more,  # type: ignore[arg-type]
    )


def full(on: bool, *, states: tuple[str, ...] = ()) -> bytes:
    """One package reaching every covered part kind; ``on`` writes the lists."""

    def L(n: int, body: str = "") -> str:
        return lst(guid(n), body=body) if on else ""

    def T(n: int) -> str:
        return xlst(guid(n)) if on else ""

    parts = {
        "xl/persons/person.xml": (
            "person",
            (
                f'<personList xmlns="{THREADED}"><person displayName="P" id="{PERSON}">'
                f"{T(1)}</person>{T(2)}</personList>"
            ),
        ),
        "xl/connections.xml": (
            "connections",
            (
                f'<connections xmlns="{MAIN}"><connection id="1" name="c">{L(3, LEAKY)}'
                f'</connection><connection id="2">{L(4)}</connection></connections>'
            ),
        ),
        "xl/externalLinks/externalLink1.xml": (
            "externalLink",
            f'<externalLink xmlns="{MAIN}"><externalBook/>{L(5)}</externalLink>',
        ),
        "xl/pivotCache/pivotCacheDefinition1.xml": (
            "pivotCache",
            (
                f'<pivotCacheDefinition xmlns="{MAIN}" xmlns:r="{RELNS}" r:id="rIdR">'
                '<cacheSource type="worksheet"><worksheetSource ref="A1:B2" sheet="S1"/>'
                f'{L(6)}</cacheSource><cacheFields count="1"><cacheField name="a">'
                f"{L(7)}</cacheField></cacheFields>{L(8)}</pivotCacheDefinition>"
            ),
        ),
        "xl/pivotCache/pivotCacheRecords1.xml": (
            "pivotRecords",
            f'<pivotCacheRecords xmlns="{MAIN}" count="0">{L(9)}</pivotCacheRecords>',
        ),
        "xl/styles.xml": (
            "styles",
            (
                f'<styleSheet xmlns="{MAIN}"><cellXfs count="1"><xf numFmtId="0">'
                f"{L(10)}</xf></cellXfs>{L(11)}</styleSheet>"
            ),
        ),
        "xl/tables/table1.xml": (
            "table",
            table_xml(L(12), L(13), f'<autoFilter ref="A1:B2">{L(14)}</autoFilter>'),
        ),
        "xl/pivotTables/pivotTable1.xml": (
            "pivotTable",
            (
                f'<pivotTableDefinition xmlns="{MAIN}" name="P" cacheId="1">'
                f'<location ref="A1:B2"/><pivotFields count="1"><pivotField>{L(15)}'
                f"</pivotField></pivotFields>{L(16)}</pivotTableDefinition>"
            ),
        ),
        "xl/queryTables/queryTable1.xml": (
            "queryTable",
            (
                f'<queryTable xmlns="{MAIN}" connectionId="1" name="Q">'
                '<queryTableRefresh><queryTableFields count="1">'
                f'<queryTableField id="1">{L(17)}</queryTableField></queryTableFields>'
                f"{L(18)}</queryTableRefresh>{L(19, LEAKY)}</queryTable>"
            ),
        ),
        "xl/comments1.xml": (
            "comments",
            (
                f'<comments xmlns="{MAIN}"><authors><author>A</author></authors>'
                '<commentList><comment ref="A1" authorId="0"><text><t>x</t></text>'
                f"</comment></commentList>{L(20)}</comments>"
            ),
        ),
        "xl/threadedComments/threadedComment1.xml": (
            "threaded",
            (
                f'<ThreadedComments xmlns="{THREADED}"><threadedComment ref="A1" '
                f'dT="2024" personId="{PERSON}" id="{COMMENT}"><text>hi</text>{T(21)}'
                f"</threadedComment>{T(22)}</ThreadedComments>"
            ),
        ),
    }
    rels = {
        "xl/workbook.xml": [
            rel("person", "rIdP", "persons/person.xml"),
            rel("connections", "rIdC", "connections.xml"),
            rel("externalLink", "rIdE", "externalLinks/externalLink1.xml"),
            rel("pivotCache", "rIdPC", "pivotCache/pivotCacheDefinition1.xml"),
            rel("styles", "rIdS", "styles.xml"),
        ],
        "xl/pivotCache/pivotCacheDefinition1.xml": [
            rel("pivotRecords", "rIdR", "pivotCacheRecords1.xml")
        ],
        "xl/worksheets/sheet1.xml": [
            rel("table", "rIdT", "../tables/table1.xml"),
            rel("pivotTable", "rIdPT", "../pivotTables/pivotTable1.xml"),
            rel("queryTable", "rIdQ", "../queryTables/queryTable1.xml"),
            rel("comments", "rIdM", "../comments1.xml"),
            rel("threaded", "rIdTC", "../threadedComments/threadedComment1.xml"),
        ],
    }
    return build(
        parts,
        rels,
        states=states,
        workbook_tail=(
            '<externalReferences><externalReference r:id="rIdE"/></externalReferences>'
            '<pivotCaches><pivotCache cacheId="1" r:id="rIdPC"/></pivotCaches>' + L(99)
        ),
        tails={
            1: TABLE_TAIL + '<pivotTableParts count="1"><pivotTablePart r:id="rIdPT"/>'
            "</pivotTableParts>" + L(98)
        },
    )


FULL_ORDER = [
    ("persons", "/person[0]", 0, guid(1), "workbook"),
    ("persons", "/", 0, guid(2), "workbook"),
    ("connections", "/connection[0]", 0, guid(3), "workbook"),
    ("connections", "/connection[1]", 0, guid(4), "workbook"),
    ("externalLink", "/", 0, guid(5), "workbook"),
    ("pivotCacheDefinition", "/cacheSource[0]", 0, guid(6), "workbook"),
    ("pivotCacheDefinition", "/cacheFields[0]/cacheField[0]", 0, guid(7), "workbook"),
    ("pivotCacheDefinition", "/", 0, guid(8), "workbook"),
    ("pivotCacheRecords", "/", 0, guid(9), "workbook"),
    ("styles", "/cellXfs[0]/xf[0]", 0, guid(10), "workbook"),
    ("styles", "/", 0, guid(11), "workbook"),
    # Document order: the fixture writes the autoFilter before the columns.
    ("table", "/autoFilter[0]", 0, guid(14), "sheet"),
    ("table", "/tableColumns[0]/tableColumn[1]", 0, guid(12), "sheet"),
    ("table", "/", 0, guid(13), "sheet"),
    ("pivotTable", "/pivotFields[0]/pivotField[0]", 0, guid(15), "sheet"),
    ("pivotTable", "/", 0, guid(16), "sheet"),
    (
        "queryTable",
        "/queryTableRefresh[0]/queryTableFields[0]/queryTableField[0]",
        0,
        guid(17),
        "sheet",
    ),
    ("queryTable", "/queryTableRefresh[0]", 0, guid(18), "sheet"),
    ("queryTable", "/", 0, guid(19), "sheet"),
    ("comments", "/", 0, guid(20), "sheet"),
    ("threadedComments", "/threadedComment[0]", 0, guid(21), "sheet"),
    ("threadedComments", "/", 0, guid(22), "sheet"),
    ("worksheet", "/", 0, guid(98), "sheet"),
    ("workbook", "/", 0, guid(99), "workbook"),
]


def test_every_reached_part_kind_reports_its_lists_in_one_fixed_order() -> None:
    """Exactly once each, nested owners before the owner's own trailing list."""
    content = full(True)

    found = brief(content)
    # The worksheet root is reported where the sheet is read, ahead of the
    # sheet-scoped parts it also relates; the order of the rest is the order the
    # reader first reached each part in.
    assert sorted(found, key=str) == sorted(FULL_ORDER, key=str)
    assert [f for f in found if f[0] not in ("worksheet", "workbook")] == [
        f for f in FULL_ORDER if f[0] not in ("worksheet", "workbook")
    ]
    assert found[-1] == FULL_ORDER[-1]
    assert brief(content) == found
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == len(
        FULL_ORDER
    )


def test_the_reached_parts_are_unchanged_by_the_coverage() -> None:
    """Strip the lists and every other segment, count and limit is the same."""
    plain = extract_xlsx(full(False), capture_id=CAPTURE)
    carrying = extract_xlsx(full(True), capture_id=CAPTURE)

    def same(item: object) -> tuple[object, ...]:
        # A connection already says whether it declared a list of its own; that
        # one flag is the only thing the lists change in what was reported.
        return (
            item.semantic_role,  # type: ignore[attr-defined]
            dict(item.source_locator),  # type: ignore[attr-defined]
            item.text,  # type: ignore[attr-defined]
            {
                k: v
                for k, v in (item.structured_value or {}).items()  # type: ignore[attr-defined]
                if k != "extLstDeclared"
            },
            item.visibility,  # type: ignore[attr-defined]
        )

    rest = [
        same(item)
        for item in carrying.segments
        if (item.structured_value or {}).get("kind") != "unsupportedExtension"
    ]
    assert rest == [same(item) for item in plain.segments]
    assert {k: v for k, v in carrying.limitations.items() if k != "extensions"} == {
        k: v for k, v in plain.limitations.items() if k != "extensions"
    }
    assert plain.limitations["extensions"] == 0
    assert carrying.unit_type == plain.unit_type == "workbook_cell"
    assert carrying.expected == plain.expected


def test_coverage_is_inert_inventory_and_never_a_cell() -> None:
    result = extract_xlsx(full(True), capture_id=CAPTURE)
    entries = [
        item
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]

    assert len(entries) == len(FULL_ORDER)
    for item in entries:
        assert item.semantic_role == "object_inventory"
        assert item.extraction_method == "inventory"
        assert item.text is None
        assert "cell" not in item.source_locator
        value = item.structured_value or {}
        assert value["payloadInterpreted"] is False
        assert value["refreshPerformed"] is False
        assert value["externalAccessPerformed"] is False
    assert [i.semantic_role for i in result.segments if i not in entries].count(
        "table_cell"
    ) == 1


def test_table_paths_are_ordinal_and_pin_the_locator() -> None:
    content = with_table(
        table_xml(
            columns=lst(guid(1), guid(2)),
            tail=lst(guid(3)),
        ).replace(
            '<tableColumn id="1" name="a"/>',
            f'<tableColumn id="1" name="a">{lst(guid(4))}</tableColumn>',
        )
    )
    found = records(content)

    assert [(w["ownerPath"], w["extensionIndex"]) for w, _, _ in found] == [
        ("/tableColumns[0]/tableColumn[0]", 0),
        ("/tableColumns[0]/tableColumn[1]", 0),
        ("/tableColumns[0]/tableColumn[1]", 1),
        ("/", 0),
    ]
    assert dict(found[1][0]) == {
        "captureId": CAPTURE,
        "part": "xl/tables/table1.xml",
        "ownerPath": "/tableColumns[0]/tableColumn[1]",
        "extensionIndex": 0,
        "sheetPart": "xl/worksheets/sheet1.xml",
        "sheetId": "1",
        "sheet": "S1",
        "sheetState": "visible",
    }
    assert found[1][1] == {
        "kind": "unsupportedExtension",
        "owner": "table",
        "ownerPart": "xl/tables/table1.xml",
        "ownerPath": "/tableColumns[0]/tableColumn[1]",
        "extensionIndex": 0,
        "uri": guid(1),
        "uriForm": "guid",
        "payloadInterpreted": False,
        "refreshPerformed": False,
        "externalAccessPerformed": False,
        "scope": "sheet",
    }
    # A column's name is source text: the path says where, not what.
    assert '"a"' not in repr(found) and "'a'" not in repr(found)


def test_table_columns_without_lists_still_count_toward_the_ordinal() -> None:
    [(where, _, _)] = records(with_table(table_xml(columns=lst(guid(1)))))
    assert where["ownerPath"] == "/tableColumns[0]/tableColumn[1]"


def test_a_repeated_uri_is_its_own_entry_in_every_nested_list() -> None:
    repeat = " " + guid(7).lower() + "\n"
    content = with_table(
        table_xml(columns=lst(guid(7), repeat, None, "urn:x", guid(7)))
    )
    found = records(content)

    assert [w["extensionIndex"] for w, _, _ in found] == [0, 1, 2, 3, 4]
    assert [r["uri"] for _, r, _ in found] == [
        guid(7),
        guid(7).lower(),
        None,
        None,
        guid(7),
    ]
    assert [r["uriForm"] for _, r, _ in found] == [
        "guid",
        "guid",
        None,
        "other",
        "guid",
    ]


def test_query_table_reports_the_root_the_refresh_and_each_field_list() -> None:
    content = build(
        {
            "xl/queryTables/queryTable1.xml": (
                "queryTable",
                (
                    f'<queryTable xmlns="{MAIN}" connectionId="3" name="Q">'
                    '<queryTableRefresh><queryTableFields count="2">'
                    f'<queryTableField id="1">{lst("urn:a", body=LEAKY)}</queryTableField>'
                    '<queryTableField id="2"/></queryTableFields>'
                    f"<sortState>{lst(guid(2))}</sortState>"
                    f"{lst(guid(3), guid(3))}</queryTableRefresh>{lst(guid(4))}"
                    "</queryTable>"
                ),
            )
        },
        {
            "xl/worksheets/sheet1.xml": [
                rel("queryTable", "rQ", "../queryTables/queryTable1.xml")
            ]
        },
    )
    result = extract_xlsx(content, capture_id=CAPTURE)

    assert result.limitations["queryTables"] == 1
    assert [
        (w["ownerPath"], w["extensionIndex"], r["uri"]) for w, r, _ in records(content)
    ] == [
        ("/queryTableRefresh[0]/queryTableFields[0]/queryTableField[0]", 0, None),
        ("/queryTableRefresh[0]/sortState[0]", 0, guid(2)),
        ("/queryTableRefresh[0]", 0, guid(3)),
        ("/queryTableRefresh[0]", 1, guid(3)),
        ("/", 0, guid(4)),
    ]
    for secret in SECRETS:
        assert secret not in repr(records(content))
    # The query table's own inventory is exactly what it was.
    [inventory] = [
        s.structured_value
        for s in result.segments
        if (s.structured_value or {}).get("kind") == "queryTable"
    ]
    assert inventory is not None
    assert inventory["refreshFieldCount"] == 2


def test_connections_report_each_connection_list_without_the_property_sets() -> None:
    content = build(
        {
            "xl/connections.xml": (
                "connections",
                (
                    f'<connections xmlns="{MAIN}"><connection id="1" name="c" '
                    f'type="5"><dbPr connection="Provider=hunter2" command="SELECT"/>'
                    f"{lst(guid(1), body=LEAKY)}</connection>"
                    f'<connection id="2"/><connection id="3">{lst(guid(2))}</connection>'
                    f"{lst(guid(9))}</connections>"
                ),
            )
        },
        {"xl/workbook.xml": [rel("connections", "rC", "connections.xml")]},
    )
    found = records(content)

    assert [(w["ownerPath"], r["uri"], v) for w, r, v in found] == [
        ("/connection[0]", guid(1), "visible"),
        ("/connection[2]", guid(2), "visible"),
    ]
    assert "hunter2" not in repr(found)
    [connection] = [
        s.structured_value
        for s in extract_xlsx(content, capture_id=CAPTURE).segments
        if (s.structured_value or {}).get("id") == 1
        and (s.structured_value or {}).get("kind") == "connection"
    ]
    assert connection is not None
    assert connection["extLstDeclared"] is True


def test_comments_threaded_comments_and_persons_report_their_own_lists() -> None:
    content = full(True)

    got = {
        (r["owner"], r["ownerPath"]): (w["part"], r["uri"])
        for w, r, _ in records(content)
        if r["owner"] in ("comments", "threadedComments", "persons")
    }
    assert got == {
        ("persons", "/person[0]"): ("xl/persons/person.xml", guid(1)),
        ("persons", "/"): ("xl/persons/person.xml", guid(2)),
        ("comments", "/"): ("xl/comments1.xml", guid(20)),
        ("threadedComments", "/threadedComment[0]"): (
            "xl/threadedComments/threadedComment1.xml",
            guid(21),
        ),
        ("threadedComments", "/"): (
            "xl/threadedComments/threadedComment1.xml",
            guid(22),
        ),
    }


THREADED_OWNERS = [
    pytest.param(
        "persons",
        "xl/persons/person.xml",
        lambda a, b: (
            f'<personList xmlns="{THREADED}"><person displayName="P" id="{PERSON}">'
            f"{a}</person>{b}</personList>"
        ),
        id="persons",
    ),
    pytest.param(
        "threadedComments",
        "xl/threadedComments/threadedComment1.xml",
        lambda a, b: (
            f'<ThreadedComments xmlns="{THREADED}"><threadedComment ref="A1" '
            f'dT="2024" personId="{PERSON}" id="{COMMENT}"><text>hi</text>{a}'
            f"</threadedComment>{b}</ThreadedComments>"
        ),
        id="threadedComments",
    ),
]


def threaded_uris(owner: str, part: str, make, inner: str, outer: str) -> list[str]:
    """Extension URIs one threaded part reports; a comment needs its person too."""
    parts = {part: ("person" if owner == "persons" else "threaded", make(inner, outer))}
    rels = {"xl/workbook.xml": [rel("person", "rIdP", "persons/person.xml")]}
    if owner == "persons":
        rels["xl/workbook.xml"][0] = rel("person", "rIdP", "persons/person.xml")
    else:
        parts["xl/persons/person.xml"] = (
            "person",
            (
                f'<personList xmlns="{THREADED}">'
                f'<person displayName="P" id="{PERSON}"/></personList>'
            ),
        )
        rels["xl/worksheets/sheet1.xml"] = [
            rel("threaded", "rIdX", "../threadedComments/threadedComment1.xml")
        ]
    content = build(parts, rels)
    return [r["uri"] for _, r, _ in records(content) if r["owner"] == owner]


@pytest.mark.parametrize(("owner", "part", "make"), THREADED_OWNERS)
def test_threaded_lists_take_spreadsheetml_qualified_entries(owner, part, make) -> None:
    got = threaded_uris(owner, part, make, xlst(guid(1), guid(2)), xlst(guid(3)))

    assert got == [guid(1), guid(2), guid(3)]


@pytest.mark.parametrize(("owner", "part", "make"), THREADED_OWNERS)
@pytest.mark.parametrize(
    "lookalike",
    [
        # The threaded namespace's own ``ext`` is not the schema's entry.
        f'<extLst><t:ext xmlns:t="{THREADED}" uri="{guid(9)}"/></extLst>',
        f'<extLst><ext uri="{guid(9)}"/></extLst>',
        f'<extLst><ext xmlns="" uri="{guid(9)}"/></extLst>',
        f'<extLst><f:ext xmlns:f="urn:foreign" uri="{guid(9)}"/></extLst>',
        # The Strict spelling is not what [MS-XLSX] binds ``x`` to.
        xlst(guid(9), ns="http://purl.oclc.org/ooxml/spreadsheetml/main"),
    ],
)
def test_threaded_lists_ignore_lookalike_entries(owner, part, make, lookalike) -> None:
    assert threaded_uris(owner, part, make, lookalike, lookalike) == []


@pytest.mark.parametrize(("owner", "part", "make"), THREADED_OWNERS)
@pytest.mark.parametrize(
    "point",
    [
        (
            f'<f:extLst xmlns:f="urn:foreign"><x:ext xmlns:x="{MAIN}" '
            f'uri="{guid(9)}"/></f:extLst>'
        ),
        f'<extLst xmlns=""><x:ext xmlns:x="{MAIN}" uri="{guid(9)}"/></extLst>',
    ],
)
def test_threaded_owners_refuse_a_foreign_extension_point(
    owner, part, make, point
) -> None:
    # The owner's parser admits only ``xltc:extLst``; the scanner never sees it.
    with pytest.raises(OOXMLInvalid, match="unsupported element"):
        threaded_uris(owner, part, make, point, "")


@pytest.mark.parametrize(("owner", "part", "make"), THREADED_OWNERS)
def test_threaded_entries_keep_exact_once_and_never_leak(owner, part, make) -> None:
    body = LEAKY.replace("<extLst>", "<q>").replace("</extLst>", "</q>")
    leaky = f'<extLst><x:ext xmlns:x="{MAIN}" uri="{guid(1)}">{body}</x:ext></extLst>'
    found = threaded_uris(owner, part, make, leaky, "")
    assert found == [guid(1)]
    for secret in SECRETS:
        assert secret not in repr(found)


def test_the_styles_part_reports_the_workbooks_lists_and_the_stored_xfs() -> None:
    content = build(
        {
            "xl/styles.xml": (
                "styles",
                (
                    f'<styleSheet xmlns="{MAIN}"><cellStyleXfs count="1"><xf>{lst(guid(1))}'
                    f'</xf></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/>'
                    f'<xf numFmtId="14">{lst(guid(2))}</xf></cellXfs><cellStyles count="1">'
                    f'<cellStyle name="Normal" xfId="0">{lst(guid(3))}</cellStyle>'
                    f'</cellStyles><dxfs count="1"><dxf>{lst(guid(4))}</dxf></dxfs>'
                    f"{lst(guid(5))}</styleSheet>"
                ),
            )
        },
        {"xl/workbook.xml": [rel("styles", "rS", "styles.xml")]},
    )

    assert [(w["ownerPath"], r["uri"]) for w, r, _ in records(content)] == [
        ("/cellStyleXfs[0]/xf[0]", guid(1)),
        ("/cellXfs[0]/xf[1]", guid(2)),
        ("/cellStyles[0]/cellStyle[0]", guid(3)),
        ("/dxfs[0]/dxf[0]", guid(4)),
        ("/", guid(5)),
    ]


# -- exact-once, shared and hidden provenance -------------------------------


def with_second_sheet_table(states: tuple[str, ...]) -> bytes:
    """Two sheets, each declaring and relating the one table part."""
    return build(
        {"xl/tables/table1.xml": ("table", table_xml(lst(guid(1))))},
        {
            "xl/worksheets/sheet1.xml": [rel("table", "rIdT", "../tables/table1.xml")],
            "xl/worksheets/sheet2.xml": [rel("table", "rIdT", "../tables/table1.xml")],
        },
        sheets=2,
        states=states,
        tails={1: TABLE_TAIL, 2: TABLE_TAIL},
    )


def test_a_part_two_sheets_reach_is_reported_once_and_shared() -> None:
    content = with_second_sheet_table(("visible", "visible"))
    [(where, record, visibility)] = records(content)

    assert record["scope"] == "shared"
    assert visibility == "visible"
    # Neither sheet is claimed: the part belongs to both.
    assert not {"sheetId", "sheet", "sheetPart", "sheetState"} & set(where)
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == 1
    # The table's own inventory is still reported once under each sheet.
    tables = [
        s
        for s in extract_xlsx(content, capture_id=CAPTURE).segments
        if (s.structured_value or {}).get("kind") == "table"
    ]
    assert len(tables) == 2


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (("hidden", "visible"), "visible"),
        (("visible", "veryHidden"), "visible"),
        (("hidden", "veryHidden"), "hidden"),
    ],
)
def test_a_shared_part_is_hidden_only_where_every_reaching_sheet_is(
    states: tuple[str, ...], expected: str
) -> None:
    [(_, record, visibility)] = records(with_second_sheet_table(states))
    assert (record["scope"], visibility) == ("shared", expected)


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
def test_a_sheet_scoped_part_takes_its_sheets_state(state: str) -> None:
    [(where, record, visibility)] = records(
        with_table(table_xml(lst(guid(1))), states=(state,))
    )
    assert (record["scope"], visibility) == ("sheet", "hidden")
    assert where["sheetState"] == state
    assert where["sheetPart"] == "xl/worksheets/sheet1.xml"


def test_a_workbook_part_is_never_hidden_by_hidden_sheets() -> None:
    hidden = full(True, states=("hidden",))
    for where, record, visibility in records(hidden):
        if record["scope"] == "workbook":
            assert visibility == "visible"
            assert "sheetId" not in where and "sheetState" not in where
        else:
            assert visibility == "hidden"
            assert where["sheetState"] == "hidden"
    assert {r["scope"] for _, r, _ in records(hidden)} == {"sheet", "workbook"}


# -- bounds, lookalikes, refusals, leakage and inertness ---------------------


def test_a_list_over_the_per_owner_bound_is_refused_before_any_record() -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a record was built before the bound was checked")

    content = with_table(table_xml(columns=lst(*[guid(1)] * 257)))
    original = ox._xlsx_extension_records
    ox._xlsx_extension_records = boom  # type: ignore[assignment]
    try:
        with pytest.raises(OOXMLInvalid, match="table declares too many extensions"):
            extract_xlsx(content, capture_id=CAPTURE)
    finally:
        ox._xlsx_extension_records = original  # type: ignore[assignment]
    assert len(records(with_table(table_xml(columns=lst(*[guid(1)] * 256))))) == 256


def test_lists_over_the_per_part_bound_are_refused_before_any_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    monkeypatch.setattr(ox, "_MAX_XLSX_PART_EXTENSIONS", 3)
    content = with_table(
        table_xml(columns=lst(guid(1), guid(2)), tail=lst(guid(3), guid(4)))
    )

    with pytest.raises(OOXMLInvalid, match="table declares too many extensions"):
        extract_xlsx(content, capture_id=CAPTURE)
    assert built == []


def test_the_global_segment_bound_is_checked_before_a_record_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 3)
    content = with_table(
        table_xml(columns=lst(guid(1), guid(2)), tail=lst(guid(3), guid(4)))
    )

    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_xlsx(content, capture_id=CAPTURE)
    assert built == []


@pytest.mark.parametrize(
    "columns",
    [
        # A foreign extension point, and one with no namespace at all.
        '<x:extLst xmlns:x="urn:foreign"><x:ext uri="{DEADBEEF-0000-0000-0000-000000000000}"/></x:extLst>',
        '<extLst xmlns=""><ext uri="{DEADBEEF-0000-0000-0000-000000000000}"/></extLst>',
        # Right list, foreign entry.
        '<extLst><f:ext xmlns:f="urn:foreign" uri="{DEADBEEF-0000-0000-0000-000000000000}"/></extLst>',
        # Below something that is not an owner.
        '<sub><extLst><ext uri="{DEADBEEF-0000-0000-0000-000000000000}"/></extLst></sub>',
    ],
)
def test_lookalikes_inside_a_reached_part_declare_nothing(columns: str) -> None:
    assert records(with_table(table_xml(columns=columns))) == []


def test_only_the_vocabularys_owners_are_read_in_a_reached_part() -> None:
    # tableColumns, tableStyleInfo and an unlisted holder are not owners.
    xml = table_xml(
        tail=f'<tableStyleInfo name="s">{lst(guid(1))}</tableStyleInfo>{lst(guid(2))}',
    ).replace("<tableColumns", "<tableColumns", 1)
    xml = xml.replace("</tableColumns>", f"{lst(guid(3))}</tableColumns>")
    assert [r["uri"] for _, r, _ in records(with_table(xml))] == [guid(2)]


def test_an_entrys_own_payload_is_never_entered() -> None:
    found = records(with_table(table_xml(columns=lst(guid(1), body=LEAKY))))

    assert [r["uri"] for _, r, _ in found] == [guid(1)]
    for secret in SECRETS:
        assert secret not in repr(found)
    assert "DEADBEEF" not in repr(found)


def test_a_second_list_on_one_owner_is_refused_but_two_owners_may_each_hold_one() -> (
    None
):
    with pytest.raises(OOXMLInvalid, match="more than one extension list"):
        extract_xlsx(
            with_table(table_xml(columns=lst(guid(1)) + lst(guid(2)))),
            capture_id=CAPTURE,
        )
    assert (
        len(records(with_table(table_xml(columns=lst(guid(1)), tail=lst(guid(2))))))
        == 2
    )


def test_an_existing_feature_refusal_is_not_replaced_by_the_new_one() -> None:
    content = build(
        {
            "xl/connections.xml": (
                "connections",
                (
                    f'<connections xmlns="{MAIN}"><connection id="1">{lst(guid(1))}'
                    f"{lst(guid(2))}</connection></connections>"
                ),
            )
        },
        {"xl/workbook.xml": [rel("connections", "rC", "connections.xml")]},
    )
    with pytest.raises(OOXMLInvalid, match="declares a property set twice"):
        extract_xlsx(content, capture_id=CAPTURE)


def test_unreferenced_members_and_name_lookalikes_are_never_scanned() -> None:
    stray = f'<table xmlns="{MAIN}">{lst(guid(1))}</table>'
    content = build(
        {},
        {},
        extra={
            "xl/tables/table9.xml": stray,
            "xl/queryTables/queryTable1.xml": f'<queryTable xmlns="{MAIN}">{lst(guid(2))}</queryTable>',
            "xl/comments1.xml": f'<comments xmlns="{MAIN}">{lst(guid(3))}</comments>',
            "xl/sharedStrings.xml": f'<sst xmlns="{MAIN}">{lst(guid(4))}</sst>',
            "xl/styles.xml": f'<styleSheet xmlns="{MAIN}">{lst(guid(5))}</styleSheet>',
        },
    )
    assert records(content) == []


def test_a_related_part_of_another_type_is_not_read_as_the_wrong_vocabulary() -> None:
    # Declared as a table relationship but typed as comments: refused by the
    # route, never reached, so nothing is reported for it.
    content = build(
        {"xl/tables/table1.xml": ("comments", table_xml(lst(guid(1))))},
        {"xl/worksheets/sheet1.xml": [rel("table", "rIdT", "../tables/table1.xml")]},
        tails={1: TABLE_TAIL},
    )
    with pytest.raises(OOXMLInvalid):
        extract_xlsx(content, capture_id=CAPTURE)


def test_no_package_filesystem_or_network_access_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = full(True)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external access attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert len(brief(content)) == len(FULL_ORDER)


def test_an_external_link_target_is_reported_by_the_link_and_never_followed() -> None:
    content = build(
        {
            "xl/externalLinks/externalLink1.xml": (
                "externalLink",
                (
                    f'<externalLink xmlns="{MAIN}" xmlns:r="{RELNS}">'
                    f'<externalBook r:id="rIdX"/>{lst(guid(1), body=LEAKY)}</externalLink>'
                ),
            )
        },
        {
            "xl/workbook.xml": [
                rel("externalLink", "rIdE", "externalLinks/externalLink1.xml")
            ],
            "xl/externalLinks/externalLink1.xml": [
                (
                    "rIdX",
                    f"{RELNS}/externalLinkPath",
                    "https://example.invalid/secret?token=abc123",
                )
            ],
        },
        workbook_tail='<externalReferences><externalReference r:id="rIdE"/></externalReferences>',
    )
    found = records(content)

    assert [
        (r["owner"], r["uri"], r["externalAccessPerformed"]) for _, r, _ in found
    ] == [("externalLink", guid(1), False)]
    assert "token=abc123" not in repr(found)


def test_the_pivot_definition_precedes_its_records_and_each_is_reported_once() -> None:
    owners = [
        (r["owner"], r["ownerPath"])
        for _, r, _ in records(full(True))
        if str(r["owner"]).startswith("pivot")
    ]
    assert owners == [
        ("pivotCacheDefinition", "/cacheSource[0]"),
        ("pivotCacheDefinition", "/cacheFields[0]/cacheField[0]"),
        ("pivotCacheDefinition", "/"),
        ("pivotCacheRecords", "/"),
        ("pivotTable", "/pivotFields[0]/pivotField[0]"),
        ("pivotTable", "/"),
    ]


# -- the remaining SpreadsheetML owners: shared strings and nested positions ----
#
# The owner matrix below is the Open XML SDK's SpreadsheetML schema model, checked
# against the parent inventory of Microsoft's ``Spreadsheet.ExtensionList`` page.
# Every matrix writes one list at each owner in document order, numbered by a
# running counter, so a record's URI says which owner it came from.

STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
SHARED_PART = "xl/sharedStrings.xml"
SHEET = "xl/worksheets/sheet1.xml"


class Seq:
    """A counter that writes the next ``extLst`` (or nothing, when ``on`` is off)."""

    def __init__(self, on: bool = True, start: int = 0) -> None:
        self.n = start
        self.on = on

    def __call__(self) -> str:
        self.n += 1
        return lst(guid(self.n)) if self.on else ""


def found(content: bytes, owner: str | None = None) -> list[tuple[object, object]]:
    return [
        (r["ownerPath"], r["uri"])
        for _, r, _ in records(content)
        if owner is None or r["owner"] == owner
    ]


def numbered(paths: list[str], first: int = 1) -> list[tuple[object, object]]:
    return [(path, guid(first + n)) for n, path in enumerate(paths)]


def sheet_xml(body: str, *, ns: str = MAIN, root: str = "worksheet") -> str:
    return f'<{root} xmlns="{ns}" xmlns:r="{RELNS}">{body}</{root}>'


GRID = '<sheetData><row r="1"><c r="A1" t="str"><v>Region</v></c></row></sheetData>'


def with_sheet(xml: str, **more: object) -> bytes:
    return build({}, {}, extra={SHEET: xml}, **more)  # type: ignore[arg-type]


SHEET_PATHS = [
    "/sheetViews[0]/sheetView[0]/pivotSelection[0]/pivotArea[0]/references[0]/reference[0]",
    "/sheetViews[0]/sheetView[0]/pivotSelection[0]/pivotArea[0]",
    "/sheetViews[0]/sheetView[0]",
    "/sheetViews[0]",
    "/sheetData[0]/row[0]/c[0]",
    "/sheetData[0]/row[0]",
    "/sheetData[0]/row[2]/c[1]",
    "/autoFilter[0]/filterColumn[0]",
    "/autoFilter[0]/sortState[0]",
    "/autoFilter[0]",
    "/sortState[0]",
    "/customSheetViews[0]/customSheetView[0]/autoFilter[0]/filterColumn[0]",
    "/customSheetViews[0]/customSheetView[0]/autoFilter[0]/sortState[0]",
    "/customSheetViews[0]/customSheetView[0]/autoFilter[0]",
    "/customSheetViews[0]/customSheetView[0]",
    "/conditionalFormatting[0]/cfRule[0]/colorScale[0]/cfvo[0]",
    "/conditionalFormatting[0]/cfRule[0]",
    "/conditionalFormatting[0]/cfRule[1]/dataBar[0]/cfvo[1]",
    "/conditionalFormatting[0]/cfRule[2]/iconSet[0]/cfvo[0]",
    "/conditionalFormatting[0]/cfRule[2]",
    "/conditionalFormatting[0]",
    "/ignoredErrors[0]",
    "/",
]


def sheet_matrix(L: Seq, ns: str = MAIN) -> str:
    """One list at every worksheet owner the schema places one at, in this order."""
    return sheet_xml(
        '<sheetViews><sheetView workbookViewId="0"><pivotSelection><pivotArea>'
        f'<references count="1"><reference field="0">{L()}</reference></references>'
        f"{L()}</pivotArea></pivotSelection>{L()}</sheetView>{L()}</sheetViews>"
        '<sheetData><row r="1"><c r="A1" t="str"><v>Region</v>'
        f'{L()}</c><c r="B1"/>{L()}</row><row r="2"/><row r="3"><c r="A3"/>'
        f'<c r="B3" t="str"><v>x</v>{L()}</c></row></sheetData>'
        f'<autoFilter ref="A1:B3"><filterColumn colId="0">{L()}</filterColumn>'
        f'<sortState ref="A2:B3">{L()}</sortState>{L()}</autoFilter>'
        f'<sortState ref="A2:B3">{L()}</sortState>'
        '<customSheetViews><customSheetView guid="{33333333-3333-3333-3333-333333333333}">'
        f'<autoFilter ref="A1:B3"><filterColumn colId="0">{L()}</filterColumn>'
        f'<sortState ref="A2:B3">{L()}</sortState>{L()}</autoFilter>{L()}'
        "</customSheetView></customSheetViews>"
        '<conditionalFormatting sqref="A1"><cfRule type="colorScale" priority="1">'
        f'<colorScale><cfvo type="min">{L()}</cfvo><cfvo type="max"/>'
        f'<color rgb="FFFF0000"/></colorScale>{L()}</cfRule>'
        '<cfRule type="dataBar" priority="2"><dataBar><cfvo type="min"/>'
        f'<cfvo type="max">{L()}</cfvo><color rgb="FF00FF00"/></dataBar></cfRule>'
        '<cfRule type="iconSet" priority="3"><iconSet>'
        f'<cfvo type="percent" val="0">{L()}</cfvo></iconSet>{L()}</cfRule>'
        f"{L()}</conditionalFormatting>"
        f'<ignoredErrors><ignoredError sqref="A1" numberStoredAsText="1"/>{L()}'
        f"</ignoredErrors>{L()}",
        ns=ns,
    )


def test_worksheet_nested_owners_report_in_authored_order_with_exact_paths() -> None:
    content = with_sheet(sheet_matrix(Seq()))

    assert found(content, "worksheet") == numbered(SHEET_PATHS)
    # The one worksheet part, exactly once, and nothing else was reported.
    assert found(content) == found(content, "worksheet")
    assert brief(content)[0] == ("worksheet", SHEET_PATHS[0], 0, guid(1), "sheet")
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == len(
        SHEET_PATHS
    )


def test_worksheet_nested_owners_carry_the_sheets_provenance_and_state() -> None:
    content = with_sheet(sheet_matrix(Seq()), states=("hidden",))

    for locator, record, visibility in records(content):
        assert locator["part"] == SHEET
        assert (locator["sheet"], locator["sheetId"]) == ("S1", "1")
        assert locator["sheetState"] == "hidden"
        assert record["ownerPart"] == SHEET and record["scope"] == "sheet"
        assert visibility == "hidden"


def test_a_strict_worksheet_reports_the_same_nested_owners() -> None:
    content = with_sheet(sheet_matrix(Seq(), ns=STRICT))

    assert found(content, "worksheet") == numbered(SHEET_PATHS)


def test_worksheet_nested_extraction_is_unchanged_by_the_lists() -> None:
    plain = extract_xlsx(with_sheet(sheet_matrix(Seq(False))), capture_id=CAPTURE)
    carrying = extract_xlsx(with_sheet(sheet_matrix(Seq())), capture_id=CAPTURE)

    def same(item: object) -> tuple[object, ...]:
        return (
            item.semantic_role,  # type: ignore[attr-defined]
            dict(item.source_locator),  # type: ignore[attr-defined]
            item.text,  # type: ignore[attr-defined]
            item.structured_value,  # type: ignore[attr-defined]
            item.visibility,  # type: ignore[attr-defined]
        )

    rest = [
        same(i)
        for i in carrying.segments
        if (i.structured_value or {}).get("kind") != "unsupportedExtension"
    ]
    assert rest == [same(i) for i in plain.segments]
    assert plain.limitations["extensions"] == 0
    assert (carrying.unit_type, carrying.expected) == (plain.unit_type, plain.expected)


@pytest.mark.parametrize(
    "holder",
    [
        # Neighbours of an owner that the schema gives no list.
        '<dimension ref="A1">{X}</dimension>',
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is>{X}</is></c></row>{X}</sheetData>',
        (
            '<sheetViews><sheetView><pane xSplit="1">{X}</pane><selection>{X}</selection>'
            "</sheetView></sheetViews>"
        ),
        '<cols><col min="1" max="1">{X}</col>{X}</cols>',
        '<mergeCells count="1"><mergeCell ref="A1:B1">{X}</mergeCell>{X}</mergeCells>',
        (
            '<conditionalFormatting sqref="A1"><cfRule type="expression" priority="1">'
            "<formula>1{X}</formula></cfRule></conditionalFormatting>"
        ),
        '<ignoredErrors><ignoredError sqref="A1">{X}</ignoredError></ignoredErrors>',
        '<controls><control shapeId="1">{X}</control>{X}</controls>',
        '<hyperlinks><hyperlink ref="A1" location="B1">{X}</hyperlink></hyperlinks>',
        '<tableParts count="0">{X}</tableParts>',
        (
            '<autoFilter ref="A1"><filterColumn colId="0"><filters>{X}</filters>'
            "</filterColumn></autoFilter>"
        ),
        # A real owner's name in the wrong place, in no namespace or a foreign one.
        '<grp><row r="1">{X}</row></grp>',
        '<sheetData><grp><row r="1">{X}</row></grp></sheetData>',
        '<sheetData><row xmlns="" r="1">{X}</row></sheetData>',
        '<f:row xmlns:f="urn:foreign" r="1"><f:extLst><f:ext uri="{U}"/></f:extLst></f:row>',
        '<sheetViews xmlns="urn:foreign"><sheetView>{X}</sheetView>{X}</sheetViews>',
        '<ignoredErrors xmlns="">{X}</ignoredErrors>',
        # Right owner and list, but the entries are not SpreadsheetML ``ext``.
        (
            '<ignoredErrors><extLst><f:ext xmlns:f="urn:foreign" uri="{U}"/></extLst>'
            "</ignoredErrors>"
        ),
    ],
)
def test_worksheet_lookalikes_declare_nothing(holder: str) -> None:
    body = holder.replace("{X}", lst(guid(1))).replace("{U}", guid(1))
    assert records(with_sheet(sheet_xml(GRID + body))) == []


def test_a_cell_list_below_a_row_below_sheetdata_is_the_only_cell_route() -> None:
    # ``c`` directly in ``sheetData`` or deeper than its row is not an owner.
    nested = (
        '<sheetData><row r="1"><c r="A1" t="str"><v>x</v></c>'
        f'<sub><c r="B1">{lst(guid(1))}</c></sub></row></sheetData>'
    )
    assert records(with_sheet(sheet_xml(nested))) == []


@pytest.mark.parametrize(
    "owner, body, paths",
    [
        (
            "chartsheet",
            '<sheetViews><sheetView zoomScale="100">{1}</sheetView>{2}</sheetViews>{3}',
            ["/sheetViews[0]/sheetView[0]", "/sheetViews[0]", "/"],
        ),
        (
            "dialogsheet",
            (
                '<sheetViews><sheetView><pivotSelection><pivotArea><references count="1">'
                '<reference field="0">{1}</reference></references>{2}</pivotArea>'
                "</pivotSelection>{3}</sheetView>{4}</sheetViews>"
                '<customSheetViews><customSheetView guid="{44444444-4444-4444-4444-444444444444}">'
                '<autoFilter ref="A1"><filterColumn colId="0">{5}</filterColumn>'
                '<sortState ref="A1">{6}</sortState>{7}</autoFilter>{8}</customSheetView>'
                "</customSheetViews>{9}"
            ),
            [
                "/sheetViews[0]/sheetView[0]/pivotSelection[0]/pivotArea[0]/references[0]/reference[0]",
                "/sheetViews[0]/sheetView[0]/pivotSelection[0]/pivotArea[0]",
                "/sheetViews[0]/sheetView[0]",
                "/sheetViews[0]",
                "/customSheetViews[0]/customSheetView[0]/autoFilter[0]/filterColumn[0]",
                "/customSheetViews[0]/customSheetView[0]/autoFilter[0]/sortState[0]",
                "/customSheetViews[0]/customSheetView[0]/autoFilter[0]",
                "/customSheetViews[0]/customSheetView[0]",
                "/",
            ],
        ),
    ],
)
def test_chart_and_dialog_sheets_are_sheets_with_their_own_owners(
    owner: str, body: str, paths: list[str]
) -> None:
    for n in range(1, len(paths) + 1):
        body = body.replace(f"{{{n}}}", lst(guid(n)))
    content = with_sheet(sheet_xml(body, root=owner))

    assert found(content) == numbered(paths)
    assert {r["owner"] for _, r, _ in records(content)} == {owner}


def test_a_sheet_part_with_another_root_declares_nothing() -> None:
    body = (
        f"<sheetViews><sheetView>{lst(guid(1))}</sheetView></sheetViews>{lst(guid(2))}"
    )
    for root in ("sheet", "macrosheet", "Worksheet"):
        assert records(with_sheet(sheet_xml(body, root=root))) == []
    foreign = sheet_xml(body).replace(MAIN, "urn:foreign")
    assert records(with_sheet(foreign)) == []


def test_workbook_nested_owners_report_in_authored_order() -> None:
    L = Seq()
    tail = (
        f'<bookViews><workbookView activeTab="0">{L()}</workbookView><workbookView/>'
        f"<workbookView>{L()}</workbookView></bookViews>"
        '<customWorkbookViews><customWorkbookView name="v" '
        f'guid="{{55555555-5555-5555-5555-555555555555}}" windowWidth="1">{L()}'
        f"</customWorkbookView></customWorkbookViews>{L()}"
    )
    content = build({}, {}, workbook_tail=tail)

    assert found(content, "workbook") == numbered(
        [
            "/bookViews[0]/workbookView[0]",
            "/bookViews[0]/workbookView[2]",
            "/customWorkbookViews[0]/customWorkbookView[0]",
            "/",
        ]
    )
    assert {r["scope"] for _, r, _ in records(content)} == {"workbook"}
    # Always in sight, even where every sheet is hidden.
    hidden = build({}, {}, workbook_tail=tail, states=("hidden",))
    assert {v for _, _, v in records(hidden)} == {"visible"}


@pytest.mark.parametrize(
    "tail",
    [
        f"<bookViews><sub><workbookView>{lst(guid(1))}</workbookView></sub></bookViews>",
        f"<workbookView>{lst(guid(1))}</workbookView>",
        f"<bookViews>{lst(guid(1))}</bookViews>",
        f"<customWorkbookViews>{lst(guid(1))}</customWorkbookViews>",
        (
            f'<bookViews><f:workbookView xmlns:f="urn:foreign">{lst(guid(1))}'
            "</f:workbookView></bookViews>"
        ),
        f'<bookViews><workbookView xmlns="">{lst(guid(1))}</workbookView></bookViews>',
    ],
)
def test_workbook_lookalikes_declare_nothing(tail: str) -> None:
    assert records(build({}, {}, workbook_tail=tail)) == []


def shared_package(
    xml: str, *, kind: str = "sharedStrings", target: str = "sharedStrings.xml"
) -> bytes:
    """A workbook whose sheet reads shared string 0, with the part declared."""
    return build(
        {SHARED_PART: (kind, xml)} if target == "sharedStrings.xml" else {},
        {"xl/workbook.xml": [rel(kind, "rIdSS", target)]},
        extra={
            SHEET: sheet_xml(
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
            ),
            **({} if target == "sharedStrings.xml" else {SHARED_PART: xml}),
        },
    )


def sst(*uris: str | None, ns: str = MAIN, root: str = "sst") -> str:
    return (
        f'<{root} xmlns="{ns}" count="1" uniqueCount="1"><si><t>Shared text</t></si>'
        f"{lst(*uris) if uris else ''}</{root}>"
    )


def texts(content: bytes) -> list[str]:
    return [
        s.structured_value["decoded"]  # type: ignore[index]
        for s in extract_xlsx(content, capture_id=CAPTURE).segments
        if (s.structured_value or {}).get("cellType") == "s"
    ]


@pytest.mark.parametrize(
    "kind, ns", [("sharedStrings", MAIN), ("sharedStringsStrict", STRICT)]
)
def test_a_consumed_shared_strings_part_reports_its_root_list(
    kind: str, ns: str
) -> None:
    content = shared_package(sst(guid(1), None, "urn:other", ns=ns), kind=kind)

    assert brief(content) == [
        ("sharedStrings", "/", 0, guid(1), "workbook"),
        ("sharedStrings", "/", 1, None, "workbook"),
        ("sharedStrings", "/", 2, None, "workbook"),
    ]
    assert [(r["ownerPart"], r["uriForm"]) for _, r, _ in records(content)] == [
        (SHARED_PART, "guid"),
        (SHARED_PART, None),
        (SHARED_PART, "other"),
    ]
    assert "Shared text" in texts(content)


def test_the_shared_strings_report_once_and_before_the_workbooks_other_parts() -> None:
    content = shared_package(sst(guid(1)))
    assert [(r["owner"], r["scope"]) for _, r, _ in records(content)] == [
        ("sharedStrings", "workbook")
    ]
    # Hidden sheets never hide the workbook's own string table.
    hidden = build(
        {SHARED_PART: ("sharedStrings", sst(guid(1)))},
        {"xl/workbook.xml": [rel("sharedStrings", "rIdSS", "sharedStrings.xml")]},
        states=("hidden",),
    )
    assert {v for _, _, v in records(hidden)} == {"visible"}


@pytest.mark.parametrize(
    "case",
    [
        "no-relationship",
        "no-content-type",
        "wrong-content-type",
        "relationship-to-another-part",
        "external-relationship",
        "unrelated-relationship-type",
        "foreign-namespace",
        "no-namespace",
        "other-root",
        "list-below-an-item",
    ],
)
def test_a_stray_shared_strings_lookalike_is_read_but_never_covered(case: str) -> None:
    xml = sst(guid(1))
    members: dict[str, str] = {
        SHEET: sheet_xml(
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
        )
    }
    parts: dict[str, tuple[str, str]] = {SHARED_PART: ("sharedStrings", xml)}
    rels: dict[str, list[tuple[str, str, str]]] = {
        "xl/workbook.xml": [rel("sharedStrings", "rIdSS", "sharedStrings.xml")]
    }
    if case == "no-relationship":
        rels = {}
    elif case == "no-content-type":
        parts = {}
        members[SHARED_PART] = xml
    elif case == "wrong-content-type":
        parts = {SHARED_PART: ("comments", xml)}
    elif case == "relationship-to-another-part":
        parts[SHARED_PART] = ("sharedStrings", xml)
        parts["xl/other.xml"] = ("sharedStrings", sst(guid(2)))
        rels = {"xl/workbook.xml": [rel("sharedStrings", "rIdSS", "other.xml")]}
    elif case == "external-relationship":
        rels = {
            "xl/workbook.xml": [
                rel(
                    "sharedStrings",
                    "rIdSS",
                    "https://example.invalid/sharedStrings.xml",
                )
            ]
        }
    elif case == "unrelated-relationship-type":
        rels = {"xl/workbook.xml": [("rIdSS", f"{RELNS}/theme", "sharedStrings.xml")]}
    elif case == "foreign-namespace":
        parts[SHARED_PART] = ("sharedStrings", sst(guid(1), ns="urn:foreign"))
    elif case == "no-namespace":
        parts[SHARED_PART] = ("sharedStrings", sst(guid(1), ns=""))
    elif case == "other-root":
        parts[SHARED_PART] = ("sharedStrings", sst(guid(1), root="sstx"))
    else:
        parts[SHARED_PART] = (
            "sharedStrings",
            f'<sst xmlns="{MAIN}"><si><t>Shared text</t>{lst(guid(1))}</si></sst>',
        )
    content = build(parts, rels, extra=members)

    assert records(content) == []
    # Extraction is exactly what it was: the name still supplies the strings.
    if case not in ("other-root", "foreign-namespace", "no-namespace"):
        assert "Shared text" in texts(content)


def test_an_unrelated_shared_strings_member_is_not_scanned() -> None:
    content = build({}, {}, extra={SHARED_PART: sst(guid(1))})
    assert records(content) == []


def test_a_second_shared_strings_list_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="sharedStrings holds more than one"):
        extract_xlsx(
            shared_package(sst(guid(1)).replace("</sst>", f"{lst(guid(2))}</sst>")),
            capture_id=CAPTURE,
        )


def test_the_shared_strings_list_is_bounded_before_any_record() -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    ox._xlsx_extension_records = lambda *a, **k: built.append(a) or original(*a, **k)  # type: ignore[assignment]
    try:
        with pytest.raises(OOXMLInvalid, match="sharedStrings declares too many"):
            extract_xlsx(shared_package(sst(*[guid(1)] * 257)), capture_id=CAPTURE)
    finally:
        ox._xlsx_extension_records = original  # type: ignore[assignment]
    assert built == []
    assert len(records(shared_package(sst(*[guid(1)] * 256)))) == 256


def table_matrix(L: Seq) -> str:
    return (
        f'<table xmlns="{MAIN}" id="1" name="T" displayName="T" ref="A1:B2">'
        f'<autoFilter ref="A1:B2"><filterColumn colId="0">{L()}</filterColumn>'
        f'<sortState ref="A2:B2">{L()}</sortState>{L()}</autoFilter>'
        f'<sortState ref="A2:B2">{L()}</sortState>'
        '<tableColumns count="2"><tableColumn id="1" name="a"/>'
        '<tableColumn id="2" name="b"><xmlColumnPr mapId="1" xpath="/r/b" '
        f'xmlDataType="string">{L()}</xmlColumnPr>{L()}</tableColumn></tableColumns>'
        f"{L()}</table>"
    )


TABLE_PATHS = [
    "/autoFilter[0]/filterColumn[0]",
    "/autoFilter[0]/sortState[0]",
    "/autoFilter[0]",
    "/sortState[0]",
    "/tableColumns[0]/tableColumn[1]/xmlColumnPr[0]",
    "/tableColumns[0]/tableColumn[1]",
    "/",
]


def test_the_remaining_table_owners_report_in_authored_order() -> None:
    content = with_table(table_matrix(Seq()))

    assert found(content, "table") == numbered(TABLE_PATHS)
    assert {r["scope"] for _, r, _ in records(content)} == {"sheet"}


@pytest.mark.parametrize(
    "xml",
    [
        # The autoFilter's own chain only exists directly below the table.
        '<sub><autoFilter ref="A1">{X}</autoFilter></sub>',
        (
            '<tableColumns count="1"><tableColumn id="1" name="a"><sub><xmlColumnPr '
            'mapId="1" xpath="/a">{X}</xmlColumnPr></sub></tableColumn></tableColumns>'
        ),
        (
            '<tableColumns count="1"><tableColumn id="1" name="a"><calculatedColumnFormula>'
            "1{X}</calculatedColumnFormula></tableColumn></tableColumns>"
        ),
        '<tableStyleInfo name="s">{X}</tableStyleInfo>',
        (
            '<autoFilter ref="A1"><filterColumn colId="0"><customFilters>{X}</customFilters>'
            "</filterColumn></autoFilter>"
        ),
    ],
)
def test_table_lookalikes_declare_nothing(xml: str) -> None:
    head = xml.replace("{X}", lst(guid(1)))
    columns = '<tableColumns count="1"><tableColumn id="1" name="a"/></tableColumns>'
    body = (
        f'<table xmlns="{MAIN}" id="1" name="T" displayName="T" ref="A1:A2">'
        + (head if "tableColumns" in head else head + columns)
        + "</table>"
    )
    assert records(with_table(body)) == []


def pivot_cache_matrix(L: Seq) -> str:
    return (
        f'<pivotCacheDefinition xmlns="{MAIN}" xmlns:r="{RELNS}" r:id="rIdR">'
        '<cacheSource type="worksheet"><worksheetSource ref="A1:B2" sheet="S1"/>'
        f'{L()}</cacheSource><cacheFields count="1"><cacheField name="a">{L()}'
        '</cacheField></cacheFields><cacheHierarchies count="1"><cacheHierarchy '
        'uniqueName="[a]" caption="a"><groupLevels count="1"><groupLevel '
        f'uniqueName="[g]" caption="g">{L()}</groupLevel></groupLevels>{L()}'
        f"</cacheHierarchy></cacheHierarchies><tupleCache>{L()}</tupleCache>"
        '<calculatedItems count="1"><calculatedItem field="0" formula="1"><pivotArea>'
        f'<references count="1"><reference field="0">{L()}</reference></references>'
        f"{L()}</pivotArea>{L()}</calculatedItem></calculatedItems>"
        f'<calculatedMembers count="1"><calculatedMember name="m" mdx="[m]">{L()}'
        f"</calculatedMember></calculatedMembers>{L()}</pivotCacheDefinition>"
    )


PIVOT_CACHE_PATHS = [
    "/cacheSource[0]",
    "/cacheFields[0]/cacheField[0]",
    "/cacheHierarchies[0]/cacheHierarchy[0]/groupLevels[0]/groupLevel[0]",
    "/cacheHierarchies[0]/cacheHierarchy[0]",
    "/tupleCache[0]",
    "/calculatedItems[0]/calculatedItem[0]/pivotArea[0]/references[0]/reference[0]",
    "/calculatedItems[0]/calculatedItem[0]/pivotArea[0]",
    "/calculatedItems[0]/calculatedItem[0]",
    "/calculatedMembers[0]/calculatedMember[0]",
    "/",
]


def pivot_table_matrix(L: Seq) -> str:
    def area(inner: str = "") -> str:
        return (
            '<pivotArea><references count="1"><reference field="0">'
            f"{L()}</reference></references>{L()}{inner}</pivotArea>"
        )

    return (
        f'<pivotTableDefinition xmlns="{MAIN}" name="P" cacheId="1">'
        '<location ref="A1:B2"/><pivotFields count="1"><pivotField>'
        f"<autoSortScope>{area()}</autoSortScope>{L()}</pivotField></pivotFields>"
        f'<pageFields count="1"><pageField fld="0">{L()}</pageField></pageFields>'
        f'<dataFields count="1"><dataField fld="0">{L()}</dataField></dataFields>'
        f'<formats count="1"><format>{area()}{L()}</format></formats>'
        '<conditionalFormats count="1"><conditionalFormat priority="1">'
        f'<pivotAreas count="1">{area()}</pivotAreas>{L()}</conditionalFormat>'
        '</conditionalFormats><chartFormats count="1"><chartFormat chart="0" '
        f'format="0">{area()}</chartFormat></chartFormats>'
        f'<pivotHierarchies count="1"><pivotHierarchy>{L()}</pivotHierarchy>'
        '</pivotHierarchies><filters count="1"><filter fld="0" type="count" id="1">'
        f'<autoFilter ref="A1"><filterColumn colId="0">{L()}</filterColumn>'
        f'<sortState ref="A1">{L()}</sortState>{L()}</autoFilter>{L()}</filter>'
        f"</filters>{L()}</pivotTableDefinition>"
    )


AREA = "pivotArea[0]/references[0]/reference[0]"
PIVOT_TABLE_PATHS = [
    f"/pivotFields[0]/pivotField[0]/autoSortScope[0]/{AREA}",
    "/pivotFields[0]/pivotField[0]/autoSortScope[0]/pivotArea[0]",
    "/pivotFields[0]/pivotField[0]",
    "/pageFields[0]/pageField[0]",
    "/dataFields[0]/dataField[0]",
    f"/formats[0]/format[0]/{AREA}",
    "/formats[0]/format[0]/pivotArea[0]",
    "/formats[0]/format[0]",
    f"/conditionalFormats[0]/conditionalFormat[0]/pivotAreas[0]/{AREA}",
    "/conditionalFormats[0]/conditionalFormat[0]/pivotAreas[0]/pivotArea[0]",
    "/conditionalFormats[0]/conditionalFormat[0]",
    f"/chartFormats[0]/chartFormat[0]/{AREA}",
    "/chartFormats[0]/chartFormat[0]/pivotArea[0]",
    "/pivotHierarchies[0]/pivotHierarchy[0]",
    "/filters[0]/filter[0]/autoFilter[0]/filterColumn[0]",
    "/filters[0]/filter[0]/autoFilter[0]/sortState[0]",
    "/filters[0]/filter[0]/autoFilter[0]",
    "/filters[0]/filter[0]",
    "/",
]


def pivot_package(cache: str, table: str) -> bytes:
    return build(
        {
            "xl/pivotCache/pivotCacheDefinition1.xml": ("pivotCache", cache),
            "xl/pivotCache/pivotCacheRecords1.xml": (
                "pivotRecords",
                f'<pivotCacheRecords xmlns="{MAIN}" count="0"/>',
            ),
            "xl/pivotTables/pivotTable1.xml": ("pivotTable", table),
        },
        {
            "xl/workbook.xml": [
                rel("pivotCache", "rIdPC", "pivotCache/pivotCacheDefinition1.xml")
            ],
            "xl/pivotCache/pivotCacheDefinition1.xml": [
                rel("pivotRecords", "rIdR", "pivotCacheRecords1.xml")
            ],
            "xl/worksheets/sheet1.xml": [
                rel("pivotTable", "rIdPT", "../pivotTables/pivotTable1.xml")
            ],
        },
        workbook_tail='<pivotCaches><pivotCache cacheId="1" r:id="rIdPC"/></pivotCaches>',
        tails={
            1: '<pivotTableParts count="1"><pivotTablePart r:id="rIdPT"/></pivotTableParts>'
        },
    )


def test_the_remaining_pivot_owners_report_in_authored_order() -> None:
    content = pivot_package(pivot_cache_matrix(Seq()), pivot_table_matrix(Seq()))

    assert found(content, "pivotCacheDefinition") == numbered(PIVOT_CACHE_PATHS)
    assert found(content, "pivotTable") == numbered(PIVOT_TABLE_PATHS)
    assert found(content, "pivotCacheRecords") == []


def test_pivot_extraction_is_unchanged_by_the_nested_lists() -> None:
    plain = extract_xlsx(
        pivot_package(pivot_cache_matrix(Seq(False)), pivot_table_matrix(Seq(False))),
        capture_id=CAPTURE,
    )
    carrying = extract_xlsx(
        pivot_package(pivot_cache_matrix(Seq()), pivot_table_matrix(Seq())),
        capture_id=CAPTURE,
    )
    rest = [
        (i.semantic_role, dict(i.source_locator), i.structured_value, i.visibility)
        for i in carrying.segments
        if (i.structured_value or {}).get("kind") != "unsupportedExtension"
    ]
    assert rest == [
        (i.semantic_role, dict(i.source_locator), i.structured_value, i.visibility)
        for i in plain.segments
    ]
    assert plain.limitations["extensions"] == 0


@pytest.mark.parametrize(
    "xml",
    [
        # A list below the holder of a pivot area, or a count, is not an owner.
        (
            '<pivotFields count="1"><pivotField><items count="1"><item x="0">{X}</item>'
            "</items></pivotField></pivotFields>"
        ),
        '<rowItems count="1"><i>{X}</i></rowItems>',
        '<pivotTableStyleInfo name="s">{X}</pivotTableStyleInfo>',
        (
            '<formats count="1"><format><pivotArea><references count="1">'
            '<reference field="0"><x v="0">{X}</x></reference></references></pivotArea>'
            "</format></formats>"
        ),
        (
            '<chartFormats count="1"><chartFormat chart="0" format="0">{X}<pivotArea/>'
            "</chartFormat></chartFormats>"
        ),
    ],
)
def test_pivot_lookalikes_declare_nothing(xml: str) -> None:
    table = (
        f'<pivotTableDefinition xmlns="{MAIN}" name="P" cacheId="1">'
        f'<location ref="A1:B2"/>{xml.replace("{X}", lst(guid(1)))}'
        "</pivotTableDefinition>"
    )
    cache = (
        f'<pivotCacheDefinition xmlns="{MAIN}" xmlns:r="{RELNS}" r:id="rIdR">'
        '<cacheSource type="worksheet"><worksheetSource ref="A1:B2" sheet="S1"/>'
        "</cacheSource></pivotCacheDefinition>"
    )
    assert found(pivot_package(cache, table), "pivotTable") == []


# Every owner takes one list: a second at the same owner is refused, whichever
# part, whichever depth.
DUPLICATES = [
    ("workbook", "<bookViews><workbookView>{XX}</workbookView></bookViews>"),
    (
        "workbook",
        (
            '<customWorkbookViews><customWorkbookView name="v" guid="{66666666-6666-6666-6666-666666666666}">'
            "{XX}</customWorkbookView></customWorkbookViews>"
        ),
    ),
    ("worksheet", "<sheetViews><sheetView>{XX}</sheetView></sheetViews>"),
    ("worksheet", "<sheetViews><sheetView/>{XX}</sheetViews>"),
    (
        "worksheet",
        (
            "<sheetViews><sheetView><pivotSelection><pivotArea>{XX}</pivotArea>"
            "</pivotSelection></sheetView></sheetViews>"
        ),
    ),
    (
        "worksheet",
        (
            '<sheetViews><sheetView><pivotSelection><pivotArea><references count="1">'
            '<reference field="0">{XX}</reference></references></pivotArea>'
            "</pivotSelection></sheetView></sheetViews>"
        ),
    ),
    ("worksheet", '<sheetData><row r="1">{XX}</row></sheetData>'),
    (
        "worksheet",
        '<sheetData><row r="1"><c r="A1" t="str"><v>x</v>{XX}</c></row></sheetData>',
    ),
    ("worksheet", '<autoFilter ref="A1">{XX}</autoFilter>'),
    (
        "worksheet",
        '<autoFilter ref="A1"><filterColumn colId="0">{XX}</filterColumn></autoFilter>',
    ),
    (
        "worksheet",
        '<autoFilter ref="A1"><sortState ref="A1">{XX}</sortState></autoFilter>',
    ),
    ("worksheet", '<sortState ref="A1">{XX}</sortState>'),
    (
        "worksheet",
        (
            '<customSheetViews><customSheetView guid="{77777777-7777-7777-7777-777777777777}">'
            "{XX}</customSheetView></customSheetViews>"
        ),
    ),
    (
        "worksheet",
        (
            '<customSheetViews><customSheetView guid="{77777777-7777-7777-7777-777777777777}">'
            '<autoFilter ref="A1">{XX}</autoFilter></customSheetView></customSheetViews>'
        ),
    ),
    ("worksheet", '<conditionalFormatting sqref="A1">{XX}</conditionalFormatting>'),
    (
        "worksheet",
        (
            '<conditionalFormatting sqref="A1"><cfRule type="expression" priority="1">'
            "{XX}</cfRule></conditionalFormatting>"
        ),
    ),
    (
        "worksheet",
        (
            '<conditionalFormatting sqref="A1"><cfRule type="colorScale" priority="1">'
            '<colorScale><cfvo type="min">{XX}</cfvo></colorScale></cfRule></conditionalFormatting>'
        ),
    ),
    ("worksheet", '<ignoredErrors><ignoredError sqref="A1"/>{XX}</ignoredErrors>'),
]


@pytest.mark.parametrize("owner, snippet", DUPLICATES)
def test_a_second_list_at_any_nested_owner_is_refused(owner: str, snippet: str) -> None:
    doubled = lst(guid(1)) + lst(guid(2))
    body = snippet.replace("{XX}", doubled)
    if owner == "workbook":
        content = build({}, {}, workbook_tail=body)
    else:
        content = with_sheet(sheet_xml(body if "<sheetData>" in body else GRID + body))
    with pytest.raises(OOXMLInvalid, match="more than one extension list"):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    "xml, owner",
    [
        (table_matrix(Seq(False)).replace("</table>", "{XX}</table>"), "table"),
        (
            table_matrix(Seq(False)).replace(
                'xmlDataType="string">', 'xmlDataType="string">{XX}'
            ),
            "table",
        ),
        (table_matrix(Seq(False)).replace("<sortState", "{XX}<sortState", 1), "table"),
    ],
)
def test_a_second_list_at_a_remaining_table_owner_is_refused(
    xml: str, owner: str
) -> None:
    doubled = lst(guid(1)) + lst(guid(2))
    with pytest.raises(OOXMLInvalid, match="more than one extension list"):
        extract_xlsx(with_table(xml.replace("{XX}", doubled)), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "closing",
    ["</tupleCache>", "</calculatedMember>", "</groupLevel>", "</calculatedItem>"],
)
def test_a_second_list_at_a_remaining_pivot_cache_owner_is_refused(
    closing: str,
) -> None:
    doubled = lst(guid(1)) + lst(guid(2))
    cache = pivot_cache_matrix(Seq(False)).replace(closing, doubled + closing)
    with pytest.raises(OOXMLInvalid, match="more than one extension list"):
        extract_xlsx(
            pivot_package(cache, pivot_table_matrix(Seq(False))), capture_id=CAPTURE
        )


def test_a_second_list_at_a_remaining_pivot_table_owner_is_refused() -> None:
    doubled = lst(guid(1)) + lst(guid(2))
    table = pivot_table_matrix(Seq(False)).replace(
        "</pivotHierarchy>", f"{doubled}</pivotHierarchy>"
    )
    cache = pivot_cache_matrix(Seq(False))
    with pytest.raises(OOXMLInvalid, match="more than one extension list"):
        extract_xlsx(pivot_package(cache, table), capture_id=CAPTURE)


# -- bounds across roots, nested owners and the shared strings -----------------


def spread() -> bytes:
    """The shared strings, a workbook view, the workbook, a row and the sheet root."""
    L = Seq()
    return build(
        {SHARED_PART: ("sharedStrings", sst(guid(100)))},
        {"xl/workbook.xml": [rel("sharedStrings", "rIdSS", "sharedStrings.xml")]},
        workbook_tail=(
            f"<bookViews><workbookView>{L()}</workbookView></bookViews>{L()}"
        ),
        extra={
            SHEET: sheet_xml(
                f'<sheetData><row r="1"><c r="A1" t="str"><v>x</v></c>{L()}</row>'
                f"</sheetData>{L()}"
            )
        },
    )


def test_the_global_bound_counts_roots_nested_owners_and_shared_strings_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    # Five lists: the strings, the view, the workbook, the row and the sheet.
    # Each part is far inside its own per-part bound; only the sum is over.
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 4)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_xlsx(spread(), capture_id=CAPTURE)
    assert built == []

    monkeypatch.setattr(ox, "MAX_SEGMENTS", 6)
    assert len(records(spread())) == 5


def test_the_per_part_bound_spans_every_owner_of_one_worksheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    monkeypatch.setattr(ox, "_MAX_XLSX_PART_EXTENSIONS", 3)
    body = (
        f'<sheetData><row r="1">{lst(guid(1), guid(2))}<c r="A1" t="str"><v>x</v>'
        f"{lst(guid(3))}</c></row></sheetData>{lst(guid(4))}"
    )
    with pytest.raises(OOXMLInvalid, match="worksheet declares too many extensions"):
        extract_xlsx(with_sheet(sheet_xml(body)), capture_id=CAPTURE)
    assert built == []


def test_a_nested_list_over_the_per_list_bound_is_refused_before_any_record() -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    ox._xlsx_extension_records = lambda *a, **k: built.append(a) or original(*a, **k)  # type: ignore[assignment]
    try:
        over = sheet_xml(
            f'<sheetData><row r="1">{lst(*[guid(1)] * 257)}</row></sheetData>'
        )
        with pytest.raises(OOXMLInvalid, match="worksheet declares too many"):
            extract_xlsx(with_sheet(over), capture_id=CAPTURE)
    finally:
        ox._xlsx_extension_records = original  # type: ignore[assignment]
    assert built == []


# -- payload, inertness and exact-once -----------------------------------------


def test_no_nested_or_shared_payload_ever_leaves_an_entry() -> None:
    body = (
        f"<sheetViews><sheetView>{lst(guid(1), body=LEAKY)}</sheetView></sheetViews>"
        f'<sheetData><row r="1">{lst(guid(2), body=LEAKY)}</row></sheetData>'
        '<conditionalFormatting sqref="A1"><cfRule type="colorScale" priority="1">'
        f'<colorScale><cfvo type="min">{lst(guid(3), body=LEAKY)}</cfvo></colorScale>'
        f"</cfRule></conditionalFormatting>{lst(guid(4), body=LEAKY)}"
    )
    content = build(
        {
            SHARED_PART: (
                "sharedStrings",
                sst().replace("</sst>", f"{lst(guid(5), body=LEAKY)}</sst>"),
            )
        },
        {"xl/workbook.xml": [rel("sharedStrings", "rIdSS", "sharedStrings.xml")]},
        workbook_tail=f"<bookViews><workbookView>{lst(guid(6), body=LEAKY)}</workbookView></bookViews>",
        extra={SHEET: sheet_xml(body)},
    )
    found_records = records(content)

    assert sorted(r["uri"] for _, r, _ in found_records) == [
        guid(n) for n in range(1, 7)
    ]
    for secret in SECRETS:
        assert secret not in repr(found_records)
    assert all(
        r["payloadInterpreted"] is False
        and r["refreshPerformed"] is False
        and r["externalAccessPerformed"] is False
        for _, r, _ in found_records
    )


def test_a_part_two_sheets_reach_through_nested_owners_is_still_reported_once() -> None:
    table = table_matrix(Seq())
    content = build(
        {"xl/tables/table1.xml": ("table", table)},
        {
            "xl/worksheets/sheet1.xml": [rel("table", "rIdT", "../tables/table1.xml")],
            "xl/worksheets/sheet2.xml": [rel("table", "rIdT", "../tables/table1.xml")],
        },
        sheets=2,
        tails={1: TABLE_TAIL, 2: TABLE_TAIL},
    )
    assert found(content, "table") == numbered(TABLE_PATHS)
    assert {r["scope"] for _, r, _ in records(content)} == {"shared"}


def test_the_nested_owner_matrix_is_deterministic() -> None:
    first = brief(with_sheet(sheet_matrix(Seq())))
    assert brief(with_sheet(sheet_matrix(Seq()))) == first


def test_the_global_bound_counts_every_sheet_before_any_record_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []
    original = ox._xlsx_extension_records
    monkeypatch.setattr(
        ox,
        "_xlsx_extension_records",
        lambda *a, **k: built.append(a) or original(*a, **k),
    )
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 2)
    # The first sheet is within the limit alone; the second takes the total over.
    content = build({}, {}, sheets=2, tails={1: lst(guid(1)), 2: lst(guid(2), guid(3))})

    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_xlsx(content, capture_id=CAPTURE)
    assert built == []
    # Two cells and three records fit exactly, and every record is then built.
    monkeypatch.setattr(ox, "MAX_SEGMENTS", 5)
    assert len(records(content)) == 3


def test_deferred_sheet_records_keep_their_place_among_the_sheets_segments() -> None:
    content = build(
        {"xl/tables/table1.xml": ("table", table_xml(columns=lst(guid(5))))},
        {"xl/worksheets/sheet1.xml": [rel("table", "rIdT", "../tables/table1.xml")]},
        sheets=2,
        tails={1: TABLE_TAIL + lst(guid(1)), 2: lst(guid(2))},
    )
    kinds = [
        (
            s.source_locator.get("sheet"),
            (s.structured_value or {}).get("kind"),
            s.semantic_role,
        )
        for s in extract_xlsx(content, capture_id=CAPTURE).segments
    ]
    # Each sheet's cells, then its inventories, then its own extension record;
    # the reached table's record comes after both sheets, as it always has.
    assert kinds == [
        ("S1", None, "table_cell"),
        ("S1", "table", "object_inventory"),
        ("S1", "unsupportedExtension", "object_inventory"),
        ("S2", None, "table_cell"),
        ("S2", "unsupportedExtension", "object_inventory"),
        ("S1", "unsupportedExtension", "object_inventory"),
    ]


PAYLOAD = (
    "<t>LEAK-T</t><v>LEAK-V</v>"
    '<f t="shared" si="0" ref="A1:A9">LEAK-F</f><f t="shared" si="7" ref="Z1"/>'
    '<row r="50"><c r="A50"><v>LEAK-ROW</v></c></row>'
    '<c r="B50" t="inlineStr"><is><t>LEAK-IS</t></is></c>'
    '<sheetData><row r="60"><c r="A60"><v>LEAK-GRID</v></c></row></sheetData>'
    '<mergeCells><mergeCell ref="A1:C3"/></mergeCells>'
    '<cols><col min="1" max="4" hidden="1"/></cols>'
    '<autoFilter ref="A1:B2"/><tableParts><tablePart r:id="rIdX"/></tableParts>'
    '<hyperlinks><hyperlink ref="A1" r:id="rIdMissing" location="LEAK-LINK"/></hyperlinks>'
    '<legacyDrawing r:id="rIdY"/><drawing r:id="rIdZ"/>'
    '<comments><commentList><comment ref="A1"><text><t>LEAK-C</t></text></comment>'
    "</commentList></comments>"
    '<sheets><sheet name="LEAK-SHEET" sheetId="9" r:id="rId1"/></sheets>'
)


def payload_sheet(body: str) -> str:
    """A sheet with real markup beside extension lists that each carry ``body``."""
    return sheet_xml(
        '<cols><col min="2" max="2" hidden="1"/></cols><sheetData>'
        f'<row r="1">{lst(guid(2), body=body)}'
        '<c r="A1" t="str"><v>Region</v></c>'
        f'<c r="B1" t="inlineStr"><is><t>Inline</t></is>{lst(guid(3), body=body)}</c>'
        "</row>"
        '<row r="2"><c r="A2"><f t="shared" si="0" ref="A2:A3">B2</f><v>2</v></c></row>'
        '<row r="3"><c r="A3"><f t="shared" si="0"/><v>3</v></c></row>'
        "</sheetData>"
        '<mergeCells count="1"><mergeCell ref="A4:B4"/></mergeCells>'
        '<hyperlinks><hyperlink ref="D1" location="S1!A1"/></hyperlinks>'
        f"{lst(guid(4), body=body)}"
    )


def payload_package(body: str) -> bytes:
    return with_sheet(payload_sheet(body), workbook_tail=lst(guid(5), body=body))


def test_a_cell_holding_only_an_extension_payload_is_no_cell() -> None:
    cell = (
        f'<c r="A1">{lst(guid(1), body="<t>LEAK-T</t><v>LEAK-V</v><f>LEAK-F</f>")}</c>'
    )
    content = with_sheet(sheet_xml(f'<sheetData><row r="1">{cell}</row></sheetData>'))

    segments = extract_xlsx(content, capture_id=CAPTURE).segments

    assert [
        (s.semantic_role, (s.structured_value or {}).get("kind")) for s in segments
    ] == [("object_inventory", "unsupportedExtension")]
    assert segments[0].source_locator["ownerPath"] == "/sheetData[0]/row[0]/c[0]"
    assert "LEAK" not in repr(segments)


@pytest.mark.parametrize("holder", ["row", "c", "sheet"])
def test_a_payload_master_cannot_satisfy_a_missing_shared_formula_master(
    holder: str,
) -> None:
    master = lst(guid(1), body='<f t="shared" si="0" ref="A1:A2">LEAK-F</f>')
    follower = '<c r="A1"><f t="shared" si="0"/><v>1</v>{c}</c>'
    row = '<row r="1">{r}{c}</row>'
    grid = (
        f"<sheetData>{row.format(r=master if holder == 'row' else '', c=follower.format(c=master if holder == 'c' else ''))}</sheetData>"
        f"{master if holder == 'sheet' else ''}"
    )
    with pytest.raises(OOXMLInvalid, match="shared formula"):
        extract_xlsx(with_sheet(sheet_xml(grid)), capture_id=CAPTURE)


def test_payload_masters_do_not_collide_with_the_sheets_own() -> None:
    # Two payload masters for one index would be a refusal if they were read.
    payload = '<f t="shared" si="0" ref="A1:A2"/><f t="shared" si="0" ref="A1:A2"/>'
    content = with_sheet(
        sheet_xml(
            "<sheetData>"
            f'<row r="1">{lst(guid(1), body=payload)}'
            '<c r="A1"><f t="shared" si="0" ref="A1:A2">B1</f><v>1</v></c></row>'
            "</sheetData>"
        )
    )
    assert [
        s.semantic_role for s in extract_xlsx(content, capture_id=CAPTURE).segments
    ][:2] == ["formula", "saved_value"]


def test_extension_payloads_change_nothing_outside_the_coverage_records() -> None:
    with_body = extract_xlsx(payload_package(PAYLOAD), capture_id=CAPTURE)
    without = extract_xlsx(payload_package(""), capture_id=CAPTURE)

    assert with_body.segments == without.segments
    assert with_body.limitations == without.limitations
    covered = [
        s
        for s in with_body.segments
        if (s.structured_value or {}).get("kind") == "unsupportedExtension"
    ]
    assert len(covered) == 4
    assert "LEAK" not in repr(with_body)
    # The real markup beside the payloads is still read.
    cells = {
        s.source_locator["cell"]: s.source_locator.get("columnHidden")
        for s in with_body.segments
        if s.semantic_role in ("table_cell", "formula") and "cell" in s.source_locator
    }
    assert cells == {"A1": False, "B1": True, "A2": False, "A3": False}
    assert [s.semantic_role for s in with_body.segments].count("object_inventory") == 5


def test_a_shared_strings_payload_is_no_string() -> None:
    xml = (
        f'<sst xmlns="{MAIN}"><si><t>Shared text</t></si>'
        f"{lst(guid(1), body='<si><t>LEAK-SI</t></si><t>LEAK-T</t>')}</sst>"
    )
    content = shared_package(xml)

    assert texts(content) == ["Shared text"]
    assert [r["owner"] for _, r, _ in records(content)] == ["sharedStrings"]
    assert "LEAK" not in repr(extract_xlsx(content, capture_id=CAPTURE).segments)


def test_a_workbook_payload_declares_no_sheet() -> None:
    body = '<sheets><sheet name="Ghost" sheetId="9" r:id="rId1"/></sheets>'
    content = build({}, {}, workbook_tail=lst(guid(1), body=body))

    names = {
        s.source_locator.get("sheet")
        for s in extract_xlsx(content, capture_id=CAPTURE).segments
        if s.semantic_role == "table_cell"
    }
    assert names == {"S1"}


def test_a_declared_shared_strings_part_without_workbook_relationships_is_read() -> (
    None
):
    members = {
        "[Content_Types].xml": (
            f'<Types><Override PartName="/{SHARED_PART}" '
            f'ContentType="{ox._XLSX_SHARED_STRINGS_CONTENT_TYPE}"/></Types>'
        ),
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}"><sheets/></workbook>',
        SHARED_PART: sst(guid(1)),
    }

    result = extract_xlsx(archive(members), capture_id=CAPTURE)

    assert result.segments == ()
    assert result.limitations["extensions"] == 0
