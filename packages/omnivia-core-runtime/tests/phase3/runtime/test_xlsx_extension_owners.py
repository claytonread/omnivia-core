"""XLSX extension coverage below the roots: every reached part reports its lists.

``test_xlsx_extension_coverage.py`` pins the worksheet and workbook roots. This
file pins the rest of the contract: a part the reader reaches by a validated
relationship -- a table, a query table, the connections, comments, threaded
comments, persons, styles, pivot parts and an external link -- reports the
extension lists its own vocabulary allows, once per part, addressed by the
structural path of the owning element and never by anything the source wrote.

Not covered, and deliberately not asserted as covered: the nested owners of a
worksheet, the shared strings part (read by its fixed name rather than a
relationship) and drawing and chart parts (their lists are DrawingML).
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
