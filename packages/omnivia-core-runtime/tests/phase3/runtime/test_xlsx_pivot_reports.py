"""XLSX pivot reports: an inventory of the saved state, never a refresh.

A workbook keeps its pivot caches in ``pivotCaches``, each naming one
``pivotCacheDefinition`` part by relationship and claiming the ``cacheId`` a
report cites. A worksheet keeps its reports in ``pivotTableParts``, each naming
one ``pivotTableDefinition`` part by relationship, and each definition cites the
cache it was built from by that same id. This route reports both, and rebuilds
neither.

Both are reached on the schema's own authored route and no other. A ``.rels``
file resolves what a declaration named; it is never itself the declaration, so
a pivot relationship no element names is inert and its part is never opened.

So the contract here is deliberately narrow. A record names the parts and
relationship ids it was reached by, the cache or table identity, where the
cache says its data came from, the counts the file stored, and the authored
captions and version claims that make the saved state legible. It never reports
a cached field item, a shared item or a record row: those are the data itself,
and a reader that copied them out would be publishing the cache's contents
under the guise of describing it. Nothing is refreshed, no source worksheet is
re-read, no connection is opened, no external workbook is resolved and nothing
is recalculated -- every record says so by carrying ``freshness`` unknown and
``refreshPerformed`` and ``externalAccessPerformed`` false.
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
# Written here in OOXML's own camel case, as a package would; the reader folds
# what the package declared before comparing, so these reach it folded.
CACHE_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ".pivotCacheDefinition+xml"
)
RECORDS_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ".pivotCacheRecords+xml"
)
TABLE_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotTable+xml"
WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
SHEET_PART = "xl/worksheets/sheet1.xml"
SHEET_RELS = "xl/worksheets/_rels/sheet1.xml.rels"
CACHE_PART = "xl/pivotCache/pivotCacheDefinition1.xml"
CACHE_RELS = "xl/pivotCache/_rels/pivotCacheDefinition1.xml.rels"
RECORDS_PART = "xl/pivotCache/pivotCacheRecords1.xml"
TABLE_PART = "xl/pivotTables/pivotTable1.xml"
# The cache's own data: shared items, cached field values and the record rows
# under them. Every one of these strings is asserted absent from everything this
# route reports, in every form -- record, locator and segment text alike.
CACHED_FIELDS = (
    '<cacheFields count="3">'
    '<cacheField name="Region" numFmtId="0">'
    '<sharedItems count="2">'
    '<s v="Northern Reach"/><s v="Southern Reach"/>'
    "</sharedItems>"
    "</cacheField>"
    '<cacheField name="Amount" numFmtId="4">'
    '<sharedItems containsString="0" containsNumber="1"'
    ' minValue="118.25" maxValue="9412336.18"/>'
    "</cacheField>"
    '<cacheField name="Closed" numFmtId="14">'
    '<sharedItems containsDate="1"><d v="2026-03-31T00:00:00"/></sharedItems>'
    "</cacheField>"
    "</cacheFields>"
)
SECRETS = (
    "Northern Reach",
    "Southern Reach",
    "9412336.18",
    "118.25",
    "2026-03-31T00:00:00",
    "sharedItems",
)
WORKSHEET_SOURCE = (
    '<cacheSource type="worksheet">'
    '<worksheetSource ref="A1:C5" sheet="Sales"/>'
    "</cacheSource>"
)
CACHE_ATTRS = (
    'refreshedBy="A. Keeper" refreshedDate="45000.5" recordCount="4"'
    ' createdVersion="3" refreshedVersion="6" minRefreshableVersion="3"'
    ' invalid="0" saveData="1" refreshOnLoad="0" optimizeMemory="0"'
    ' enableRefresh="1" backgroundQuery="0" upgradeOnRefresh="0"'
    ' tupleCache="0" supportSubquery="0" supportAdvancedDrill="0"'
)
TABLE_ATTRS = (
    'name="Revenue by region" cacheId="0" dataCaption="Values"'
    ' rowHeaderCaption="Region" colHeaderCaption="Measure"'
    ' grandTotalCaption="All" errorCaption="#ERR" missingCaption="--"'
    ' createdVersion="3" updatedVersion="6" minRefreshableVersion="3"'
    ' applyNumberFormats="0" dataOnRows="0"'
)
# ``colFields`` is absent and ``pageFields`` is written empty, so one record
# shows the two apart: a list nothing declared reports None, a declared empty
# one reports 0.
TABLE_BODY = (
    '<location ref="E3:F9" firstHeaderRow="1" firstDataRow="2" firstDataCol="1"/>'
    '<pivotFields count="3">'
    '<pivotField axis="axisRow" showAll="0">'
    '<items count="2"><item x="0"/><item x="1"/></items>'
    "</pivotField>"
    '<pivotField dataField="1" showAll="0"/>'
    '<pivotField showAll="0"/>'
    "</pivotFields>"
    '<rowFields count="1"><field x="0"/></rowFields>'
    '<rowItems count="2"><i><x/></i><i t="grand"><x/></i></rowItems>'
    '<pageFields count="0"/>'
    '<dataFields count="1">'
    '<dataField name="Sum of Amount" fld="1" baseField="0" baseItem="0"/>'
    "</dataFields>"
)


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def definition(
    body: str = WORKSHEET_SOURCE + CACHED_FIELDS,
    *,
    main: str = MAIN,
    relns: str = RELNS,
    attrs: str = CACHE_ATTRS,
    records_id: str | None = "rIdRecords",
) -> str:
    """One pivot-cache definition part, relating its records part by default."""
    related = "" if records_id is None else f' r:id="{records_id}"'
    return (
        f'<pivotCacheDefinition xmlns="{main}" xmlns:r="{relns}"{related} {attrs}>'
        f"{body}</pivotCacheDefinition>"
    )


def table(
    body: str = TABLE_BODY,
    *,
    main: str = MAIN,
    relns: str = RELNS,
    attrs: str = TABLE_ATTRS,
) -> str:
    return (
        f'<pivotTableDefinition xmlns="{main}" xmlns:r="{relns}" {attrs}>'
        f"{body}</pivotTableDefinition>"
    )


def records(*, main: str = MAIN, count: str | None = "4") -> str:
    """One cache-records part. Its rows are data, and nothing reports them."""
    declared = "" if count is None else f' count="{count}"'
    return (
        f'<pivotCacheRecords xmlns="{main}"{declared}>'
        '<r><x v="0"/><n v="9412336.18"/><d v="2026-03-31T00:00:00"/></r>'
        '<r><x v="1"/><n v="118.25"/><d v="2026-03-31T00:00:00"/></r>'
        '<r><x v="0"/><n v="118.25"/><d v="2026-03-31T00:00:00"/></r>'
        '<r><x v="1"/><n v="9412336.18"/><d v="2026-03-31T00:00:00"/></r>'
        "</pivotCacheRecords>"
    )


def worksheet(body: str, *, main: str = MAIN, relns: str = RELNS) -> str:
    """One sheet with one written cell, and ``body`` where its declarations sit."""
    return (
        f'<worksheet xmlns="{main}" xmlns:r="{relns}"><sheetData>'
        '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
        f"</sheetData>{body}</worksheet>"
    )


def xlsx(
    *,
    caches: dict[str, str] | None = None,
    declarations: str | None = None,
    tables: dict[str, str] | None = None,
    sheet_parts: str | None = None,
    record_parts: dict[str, str] | None = None,
    main: str = MAIN,
    relns: str = RELNS,
    cache_type: str | None = None,
    records_type: str | None = None,
    table_type: str | None = None,
    cache_ct: str = CACHE_CT,
    records_ct: str = RECORDS_CT,
    table_ct: str = TABLE_CT,
    workbook_rels: str | None = None,
    sheet_rels: str | None = None,
    cache_rels: str | None = None,
    sheet_state: str = "visible",
    extra: dict[str, str] | None = None,
) -> bytes:
    """One sheet, one cache and one pivot table on it by default.

    ``caches`` maps each definition part to its XML and ``tables`` each pivot
    table part to its; ``declarations`` overrides the ``pivotCaches`` body the
    workbook writes, and ``caches`` of ``{}`` with no ``declarations`` writes no
    ``pivotCaches`` element at all, which is the absent case an empty list has
    to stay distinguishable from. ``sheet_parts`` does the same for the sheet:
    it replaces the whole ``pivotTableParts`` element, and ``tables`` of ``{}``
    with no ``sheet_parts`` writes none at all. The three ``*_rels`` arguments
    replace a whole relationship part.
    """
    cache_type = cache_type or f"{relns}/pivotCacheDefinition"
    records_type = records_type or f"{relns}/pivotCacheRecords"
    table_type = table_type or f"{relns}/pivotTable"
    cache_parts = {CACHE_PART: definition(main=main, relns=relns)} if caches is None else caches
    table_parts = {TABLE_PART: table(main=main, relns=relns)} if tables is None else tables
    stored = (
        {RECORDS_PART: records(main=main)} if record_parts is None else record_parts
    )
    declared = (
        declarations
        if declarations is not None
        else "".join(
            f'<pivotCache cacheId="{index}" r:id="rIdCache{index}"/>'
            for index in range(len(cache_parts))
        )
    )
    body = (
        ""
        if declarations is None and not cache_parts
        else f"<pivotCaches>{declared}</pivotCaches>"
    )
    if sheet_parts is not None:
        sheet_body = sheet_parts
    elif table_parts:
        sheet_body = (
            f'<pivotTableParts count="{len(table_parts)}">'
            + "".join(
                f'<pivotTablePart r:id="rIdPivot{index}"/>'
                for index in range(len(table_parts))
            )
            + "</pivotTableParts>"
        )
    else:
        sheet_body = ""
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{content}"/>'
        for group, content in (
            (cache_parts, cache_ct),
            (stored, records_ct),
            (table_parts, table_ct),
        )
        for name in group
    )
    members = {
        "[Content_Types].xml": f"<Types>{overrides}</Types>",
        WORKBOOK_PART: (
            f'<workbook xmlns="{main}" xmlns:r="{relns}"><sheets>'
            f'<sheet name="Sales" sheetId="7" state="{sheet_state}" r:id="rId1"/>'
            f"</sheets>{body}</workbook>"
        ),
        WORKBOOK_RELS: workbook_rels
        or (
            "<Relationships>"
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{relns}/worksheet"/>'
            + "".join(
                f'<Relationship Id="rIdCache{index}" Target="{name[len("xl/") :]}"'
                f' Type="{cache_type}"/>'
                for index, name in enumerate(cache_parts)
            )
            + "</Relationships>"
        ),
        SHEET_PART: worksheet(sheet_body, main=main, relns=relns),
        **(
            {SHEET_RELS: sheet_rels}
            if sheet_rels is not None
            else (
                {
                    SHEET_RELS: "<Relationships>"
                    + "".join(
                        f'<Relationship Id="rIdPivot{index}" Target="../{name[len("xl/") :]}"'
                        f' Type="{table_type}"/>'
                        for index, name in enumerate(table_parts)
                    )
                    + "</Relationships>"
                }
                if table_parts
                else {}
            )
        ),
        **(
            {CACHE_RELS: cache_rels}
            if cache_rels is not None
            else (
                {
                    CACHE_RELS: (
                        "<Relationships>"
                        f'<Relationship Id="rIdRecords" Target="{RECORDS_PART[len("xl/pivotCache/") :]}"'
                        f' Type="{records_type}"/>'
                        "</Relationships>"
                    )
                }
                if stored
                else {}
            )
        ),
        **cache_parts,
        **stored,
        **table_parts,
        **(extra or {}),
    }
    return archive(members)


def inventory(content: bytes, kind: str) -> list[tuple[dict, dict]]:
    """Every pivot record of one kind, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and (item.structured_value or {}).get("kind") == kind
    ]


def caches(content: bytes) -> list[tuple[dict, dict]]:
    return inventory(content, "pivotCache")


def tables(content: bytes) -> list[tuple[dict, dict]]:
    return inventory(content, "pivotTable")


CACHE_WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "part": WORKBOOK_PART,
    "pivotCachePart": CACHE_PART,
    "pivotCacheRelationshipId": "rIdCache0",
    "pivotCacheIndex": 0,
    "pivotCacheId": 0,
}
TABLE_WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "sheetPart": SHEET_PART,
    "sheetId": "7",
    "sheet": "Sales",
    "sheetState": "visible",
    "pivotTablePart": TABLE_PART,
    "pivotTableRelationshipId": "rIdPivot0",
    "pivotTableIndex": 0,
}
# Pinned by full equality so a field this route starts reporting -- or stops --
# fails here rather than passing unnoticed.
CACHE_RECORD: dict[str, object] = {
    "kind": "pivotCache",
    "cacheId": 0,
    "definitionPart": CACHE_PART,
    "freshness": "unknown",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "recordsPart": RECORDS_PART,
    "recordsRelationshipId": "rIdRecords",
    "recordsPartCount": 4,
    "sourceType": "worksheet",
    "sourceConnectionId": None,
    "sourceSheet": "Sales",
    "sourceName": None,
    "sourceRef": "A1:C5",
    "sourceRelationshipId": None,
    "fieldCount": 3,
    "refreshedBy": "A. Keeper",
    "refreshedDate": "45000.5",
    "invalid": False,
    "saveData": True,
    "refreshOnLoad": False,
    "optimizeMemory": False,
    "enableRefresh": True,
    "backgroundQuery": False,
    "upgradeOnRefresh": False,
    "tupleCache": False,
    "supportSubquery": False,
    "supportAdvancedDrill": False,
    "recordCount": 4,
    "createdVersion": 3,
    "refreshedVersion": 6,
    "minRefreshableVersion": 3,
}
TABLE_RECORD: dict[str, object] = {
    "kind": "pivotTable",
    "name": "Revenue by region",
    "cacheId": 0,
    "definitionPart": TABLE_PART,
    "cachePart": CACHE_PART,
    "cacheRelationshipId": "rIdCache0",
    "location": "E3:F9",
    "freshness": "unknown",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "pivotFieldCount": 3,
    "rowFieldCount": 1,
    "colFieldCount": None,
    "pageFieldCount": 0,
    "dataFieldCount": 1,
    "dataCaption": "Values",
    "rowHeaderCaption": "Region",
    "colHeaderCaption": "Measure",
    "grandTotalCaption": "All",
    "errorCaption": "#ERR",
    "missingCaption": "--",
    "createdVersion": 3,
    "updatedVersion": 6,
    "minRefreshableVersion": 3,
}


# --------------------------------------------------------------------------
# What an ordinary pivot report reports


def test_an_ordinary_cache_and_table_are_inventoried_exactly() -> None:
    """The whole surface of both records, pinned by full equality."""
    content = xlsx()
    assert caches(content) == [(CACHE_WHERE, CACHE_RECORD)]
    assert tables(content) == [(TABLE_WHERE, TABLE_RECORD)]


def test_pivot_records_are_inert_inventory_in_a_settled_order() -> None:
    """Table with its sheet, cache last with the workbook's own declarations."""
    segments = extract_xlsx(xlsx(), capture_id=CAPTURE).segments
    assert [item.semantic_role for item in segments] == [
        "table_cell",
        "object_inventory",
        "object_inventory",
    ]
    assert [(item.structured_value or {}).get("kind") for item in segments[1:]] == [
        "pivotTable",
        "pivotCache",
    ]
    for item in segments[1:]:
        assert item.extraction_method == "inventory"
        assert item.visibility == "visible"
        assert item.text is None


def test_the_pivot_counts_are_reported_in_the_extraction_metadata() -> None:
    """Two figures: a cache may serve several tables, or no table at all."""
    limitations = extract_xlsx(xlsx(), capture_id=CAPTURE).limitations
    assert limitations["pivotCaches"] == 1
    assert limitations["pivotTables"] == 1


def test_a_cache_no_table_cites_is_still_inventoried() -> None:
    """A workbook may keep a cache its last save left no report for."""
    content = xlsx(tables={})
    assert [record["cacheId"] for _where, record in caches(content)] == [0]
    assert tables(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["pivotTables"] == 0


def test_no_refresh_or_external_access_is_ever_claimed() -> None:
    """The three standing claims, on both records and under every source type."""
    for kind in ("worksheet", "external", "consolidation", "scenario"):
        source = (
            WORKSHEET_SOURCE
            if kind == "worksheet"
            else f'<cacheSource type="{kind}" connectionId="4"/>'
        )
        content = xlsx(
            caches={CACHE_PART: definition(source + CACHED_FIELDS)},
        )
        for _where, record in (*caches(content), *tables(content)):
            assert record["freshness"] == "unknown"
            assert record["refreshPerformed"] is False
            assert record["externalAccessPerformed"] is False


# --------------------------------------------------------------------------
# Transitional and Strict parity


def test_the_profile_fixtures_bind_r_id_to_their_own_relationship_namespace() -> None:
    """Guards every other parity test: a mis-bound ``r`` would prove nothing.

    Each profile's declarations -- the workbook's ``pivotCache`` and the
    sheet's ``pivotTablePart`` alike -- have to carry ``r:id`` in that
    profile's own office-document relationship namespace, or a reader keyed to
    the other one would pass here for the wrong reason.
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
        assert "<pivotTableParts" in package.read(SHEET_PART).decode()


def test_a_strict_workbook_reports_what_a_transitional_one_does() -> None:
    """Both profiles, same records: only the relationship URIs differ."""
    content = xlsx(main=STRICT_MAIN, relns=STRICT_RELNS)
    assert caches(content) == [(CACHE_WHERE, CACHE_RECORD)]
    assert tables(content) == [(TABLE_WHERE, TABLE_RECORD)]


def test_a_strict_package_may_relate_its_parts_by_the_transitional_uris() -> None:
    """The profile of the markup and of the relationship are independent."""
    content = xlsx(
        main=STRICT_MAIN,
        relns=STRICT_RELNS,
        cache_type=f"{RELNS}/pivotCacheDefinition",
        records_type=f"{RELNS}/pivotCacheRecords",
        table_type=f"{RELNS}/pivotTable",
    )
    assert caches(content) == [(CACHE_WHERE, CACHE_RECORD)]
    assert tables(content) == [(TABLE_WHERE, TABLE_RECORD)]


# --------------------------------------------------------------------------
# Linkage between a worksheet's table and the workbook's cache


def test_two_tables_citing_one_cache_both_name_it() -> None:
    """A cache serves as many reports as cite its id; the link is by id."""
    content = xlsx(
        tables={
            TABLE_PART: table(),
            "xl/pivotTables/pivotTable2.xml": table(
                attrs=TABLE_ATTRS.replace(
                    'name="Revenue by region"', 'name="Revenue by quarter"'
                )
            ),
        }
    )
    reported = tables(content)
    assert [record["name"] for _where, record in reported] == [
        "Revenue by region",
        "Revenue by quarter",
    ]
    for _where, record in reported:
        assert record["cachePart"] == CACHE_PART
        assert record["cacheRelationshipId"] == "rIdCache0"


def test_a_table_cites_the_cache_its_own_id_names_not_the_first_one() -> None:
    """Two caches, and the table's ``cacheId`` picks between them."""
    second = "xl/pivotCache/pivotCacheDefinition2.xml"
    content = xlsx(
        caches={
            CACHE_PART: definition(),
            second: definition(records_id=None),
        },
        record_parts={RECORDS_PART: records()},
        tables={TABLE_PART: table(attrs=TABLE_ATTRS.replace('cacheId="0"', 'cacheId="1"'))},
    )
    (_where, record), = tables(content)
    assert record["cacheId"] == 1
    assert record["cachePart"] == second
    assert record["cacheRelationshipId"] == "rIdCache1"


def test_a_table_citing_a_cache_the_workbook_never_declared_links_to_none() -> None:
    """The link is absent rather than invented, and the table is still reported.

    Nothing here derives a cache part from a name, falls back to the only cache
    in the package or picks the one that sorts first: a report citing an id the
    workbook declared nothing under names no cache, and says so.
    """
    content = xlsx(
        tables={TABLE_PART: table(attrs=TABLE_ATTRS.replace('cacheId="0"', 'cacheId="9"'))}
    )
    (_where, record), = tables(content)
    assert record["cacheId"] == 9
    assert record["cachePart"] is None
    assert record["cacheRelationshipId"] is None


def test_a_table_on_a_workbook_declaring_no_caches_links_to_none() -> None:
    """A sheet may relate a report whose cache the workbook dropped."""
    content = xlsx(caches={}, record_parts={})
    assert caches(content) == []
    (_where, record), = tables(content)
    assert (record["cachePart"], record["cacheRelationshipId"]) == (None, None)


# --------------------------------------------------------------------------
# Absent and empty states


def test_a_workbook_declaring_no_pivot_caches_reports_none() -> None:
    content = xlsx(caches={}, record_parts={}, tables={})
    assert caches(content) == []
    assert tables(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["pivotCaches"] == 0


def test_an_empty_pivot_cache_list_reports_none_and_is_not_a_refusal() -> None:
    """An authored empty list stays distinguishable from no list at all."""
    content = xlsx(caches={}, declarations="", record_parts={}, tables={})
    assert caches(content) == []


def test_a_cache_that_saved_no_records_part_reports_none_for_it() -> None:
    """``saveData`` off keeps no records part, and the relationship is optional."""
    content = xlsx(
        caches={CACHE_PART: definition(records_id=None)},
        record_parts={},
        cache_rels="<Relationships/>",
    )
    (_where, record), = caches(content)
    assert record["recordsPart"] is None
    assert record["recordsRelationshipId"] is None
    assert record["recordsPartCount"] is None
    # ``recordCount`` is the definition's own stored figure and is unaffected.
    assert record["recordCount"] == 4


def test_a_records_part_declaring_no_count_reports_none_for_it() -> None:
    """``count`` is optional, and nothing counts the rows to stand in for it."""
    content = xlsx(record_parts={RECORDS_PART: records(count=None)})
    (_where, record), = caches(content)
    assert record["recordsPartCount"] is None


def test_a_cache_declaring_no_fields_is_distinguishable_from_an_empty_list() -> None:
    content = xlsx(caches={CACHE_PART: definition(WORKSHEET_SOURCE)})
    (_where, record), = caches(content)
    assert record["fieldCount"] is None
    content = xlsx(
        caches={CACHE_PART: definition(WORKSHEET_SOURCE + '<cacheFields count="0"/>')}
    )
    (_where, record), = caches(content)
    assert record["fieldCount"] == 0


def test_a_cache_writing_no_optional_attribute_reports_none_not_a_default() -> None:
    """A default the schema would apply is not a claim the file made."""
    content = xlsx(caches={CACHE_PART: definition(attrs="", records_id=None)})
    (_where, record), = caches(content)
    for field in (
        "refreshedBy",
        "refreshedDate",
        "recordCount",
        "createdVersion",
        "refreshedVersion",
        "minRefreshableVersion",
        "saveData",
        "refreshOnLoad",
        "enableRefresh",
        "invalid",
    ):
        assert record[field] is None


def test_a_table_writing_no_optional_attribute_reports_none_not_a_default() -> None:
    content = xlsx(tables={TABLE_PART: table(attrs='name="Bare" cacheId="0"')})
    (_where, record), = tables(content)
    for field in (
        "dataCaption",
        "rowHeaderCaption",
        "colHeaderCaption",
        "grandTotalCaption",
        "errorCaption",
        "missingCaption",
        "createdVersion",
        "updatedVersion",
        "minRefreshableVersion",
    ):
        assert record[field] is None


def test_a_table_declaring_no_location_reports_none_for_it() -> None:
    content = xlsx(tables={TABLE_PART: table('<pivotFields count="0"/>')})
    (_where, record), = tables(content)
    assert record["location"] is None
    assert record["pivotFieldCount"] == 0
    assert record["rowFieldCount"] is None


def test_an_authored_empty_caption_stays_the_empty_claim_it_is() -> None:
    """``ST_Xstring`` written empty is a claim, not an absent attribute."""
    content = xlsx(
        tables={TABLE_PART: table(attrs='name="Report" cacheId="0" dataCaption=""')}
    )
    (_where, record), = tables(content)
    assert record["dataCaption"] == ""


def test_a_worksheet_source_naming_a_defined_name_reports_no_range() -> None:
    """``name`` and ``ref`` are alternatives, and neither is invented for the other."""
    content = xlsx(
        caches={
            CACHE_PART: definition(
                '<cacheSource type="worksheet">'
                '<worksheetSource name="LedgerRange"/>'
                "</cacheSource>"
            )
        }
    )
    (_where, record), = caches(content)
    assert record["sourceName"] == "LedgerRange"
    assert record["sourceRef"] is None
    assert record["sourceSheet"] is None


def test_a_source_that_is_not_a_worksheet_reports_only_its_type() -> None:
    content = xlsx(
        caches={CACHE_PART: definition('<cacheSource type="scenario"/>' + CACHED_FIELDS)}
    )
    (_where, record), = caches(content)
    assert record["sourceType"] == "scenario"
    assert record["sourceSheet"] is None
    assert record["sourceRef"] is None
    assert record["sourceConnectionId"] is None


# --------------------------------------------------------------------------
# Inert identifiers: a connection and an external source are named, not opened


def test_a_connection_id_is_preserved_as_an_inert_identifier() -> None:
    """The id the cache cites, kept as the number it is; no connection is opened.

    The package declares no connections part at all, so a reader that resolved
    the id would have failed outright. The record carries it and says no
    external access was performed.
    """
    content = xlsx(
        caches={
            CACHE_PART: definition(
                '<cacheSource type="external" connectionId="4"/>' + CACHED_FIELDS
            )
        }
    )
    (_where, record), = caches(content)
    assert record["sourceType"] == "external"
    assert record["sourceConnectionId"] == 4
    assert record["externalAccessPerformed"] is False
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["connections"] == 0


def test_a_worksheet_source_relationship_is_named_and_never_resolved() -> None:
    """A source in another workbook is an id in the record and nothing more.

    The sheet's own ``.rels`` holds no such relationship, so a reader that tried
    to resolve it would refuse the package; the id is reported as the inert text
    it is.
    """
    content = xlsx(
        caches={
            CACHE_PART: definition(
                '<cacheSource type="worksheet">'
                '<worksheetSource ref="A1:C5" sheet="Sales" r:id="rIdElsewhere"/>'
                "</cacheSource>" + CACHED_FIELDS
            )
        }
    )
    (_where, record), = caches(content)
    assert record["sourceRelationshipId"] == "rIdElsewhere"
    assert record["externalAccessPerformed"] is False


# --------------------------------------------------------------------------
# Cached values never escape


def test_no_cached_field_item_or_record_row_is_ever_reported() -> None:
    """The cache's own data, absent from every record, locator and segment."""
    extraction = extract_xlsx(xlsx(), capture_id=CAPTURE)
    rendered = repr(
        [
            (item.semantic_role, item.source_locator, item.structured_value, item.text)
            for item in extraction.segments
        ]
    )
    for secret in SECRETS:
        assert secret not in rendered
    assert repr(extraction.limitations).count("941") == 0


def test_a_cache_record_reports_counts_rather_than_the_values_behind_them() -> None:
    """Three fields and four rows, and not one of their values."""
    (_where, record), = caches(xlsx())
    assert (record["fieldCount"], record["recordCount"], record["recordsPartCount"]) == (
        3,
        4,
        4,
    )
    assert "Region" not in repr(record)


def test_a_table_record_reports_field_counts_rather_than_field_items() -> None:
    (_where, record), = tables(xlsx())
    assert record["pivotFieldCount"] == 3
    for leaked in ("item", "axisRow", "showAll", "fld"):
        assert leaked not in repr(record)


# --------------------------------------------------------------------------
# Visibility


def test_a_pivot_table_on_a_hidden_sheet_is_hidden_and_its_cache_is_not() -> None:
    """A table takes its sheet's state; a cache belongs to the workbook."""
    for state in ("hidden", "veryHidden"):
        segments = [
            item
            for item in extract_xlsx(
                xlsx(sheet_state=state), capture_id=CAPTURE
            ).segments
            if item.semantic_role == "object_inventory"
        ]
        reported = {
            (item.structured_value or {})["kind"]: item.visibility for item in segments
        }
        assert reported == {"pivotTable": "hidden", "pivotCache": "visible"}
        assert [
            item.source_locator["sheetState"]
            for item in segments
            if (item.structured_value or {})["kind"] == "pivotTable"
        ] == [state]


# --------------------------------------------------------------------------
# Ordering and determinism


def test_caches_are_reported_in_the_workbook_s_own_declaration_order() -> None:
    parts = {
        "xl/pivotCache/pivotCacheDefinition1.xml": definition(records_id=None),
        "xl/pivotCache/pivotCacheDefinition2.xml": definition(records_id=None),
        "xl/pivotCache/pivotCacheDefinition3.xml": definition(records_id=None),
    }
    content = xlsx(
        caches=parts,
        declarations="".join(
            f'<pivotCache cacheId="{cache_id}" r:id="rIdCache{index}"/>'
            for index, cache_id in enumerate((5, 1, 3))
        ),
        record_parts={},
        tables={},
    )
    reported = caches(content)
    assert [where["pivotCacheIndex"] for where, _record in reported] == [0, 1, 2]
    assert [where["pivotCacheId"] for where, _record in reported] == [5, 1, 3]
    assert [where["pivotCachePart"] for where, _record in reported] == list(parts)


def test_pivot_tables_are_reported_in_the_sheet_s_own_declaration_order() -> None:
    """The sheet writes the order, and no ``.rels`` line order displaces it."""
    parts = {
        TABLE_PART: table(attrs=TABLE_ATTRS.replace("region", "one")),
        "xl/pivotTables/pivotTable2.xml": table(
            attrs=TABLE_ATTRS.replace("region", "two")
        ),
    }
    relationships = [
        f'<Relationship Id="rIdPivot{index}" Target="../pivotTables/{name}"'
        f' Type="{RELNS}/pivotTable"/>'
        for index, name in enumerate(("pivotTable1.xml", "pivotTable2.xml"))
    ]
    # The sheet declares the second part first, and the ``.rels`` file is
    # written both ways round: the authored order is the one reported.
    declared = (
        '<pivotTableParts count="2">'
        '<pivotTablePart r:id="rIdPivot1"/><pivotTablePart r:id="rIdPivot0"/>'
        "</pivotTableParts>"
    )
    forwards = xlsx(
        tables=parts,
        sheet_parts=declared,
        sheet_rels=f"<Relationships>{''.join(relationships)}</Relationships>",
    )
    backwards = xlsx(
        tables=parts,
        sheet_parts=declared,
        sheet_rels=f"<Relationships>{''.join(reversed(relationships))}</Relationships>",
    )
    assert tables(forwards) == tables(backwards)
    assert [where["pivotTableIndex"] for where, _record in tables(forwards)] == [0, 1]
    assert [where["pivotTablePart"] for where, _record in tables(forwards)] == list(
        reversed(parts)
    )
    assert [record["name"] for _where, record in tables(forwards)] == [
        "Revenue by two",
        "Revenue by one",
    ]


def test_the_same_package_inventories_identically_every_time() -> None:
    content = xlsx()
    assert caches(content) == caches(content)
    assert tables(content) == tables(content)


# --------------------------------------------------------------------------
# Foreign and unqualified lookalikes declare nothing


@pytest.mark.parametrize(
    "declarations",
    [
        pytest.param(
            '<pivotCache xmlns="urn:other" cacheId="0" r:id="rIdCache0"/>',
            id="foreign-member",
        ),
        pytest.param(
            '<holder><pivotCache cacheId="0" r:id="rIdCache0"/></holder>',
            id="nested-deeper",
        ),
    ],
)
def test_a_lookalike_pivot_cache_declares_nothing(declarations: str) -> None:
    """Only the schema's own route declares a cache; everything else is transparent."""
    assert caches(xlsx(declarations=declarations, tables={})) == []


def workbook(body: str) -> str:
    """The default workbook with ``body`` where its ``pivotCaches`` would sit."""
    return (
        f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
        '<sheet name="Sales" sheetId="7" state="visible" r:id="rId1"/>'
        f"</sheets>{body}</workbook>"
    )


def test_a_pivot_cache_list_in_a_foreign_namespace_declares_nothing() -> None:
    """The holder itself written foreign: the workbook declares no list at all."""
    content = xlsx(
        tables={},
        extra={
            WORKBOOK_PART: workbook(
                '<pivotCaches xmlns="urn:other">'
                '<pivotCache cacheId="0" r:id="rIdCache0"/>'
                "</pivotCaches>"
            )
        },
    )
    assert caches(content) == []


def test_a_workbook_in_a_foreign_namespace_declares_no_pivot_caches() -> None:
    """The route is held to SpreadsheetML, as every other XLSX part route is."""
    assert caches(xlsx(main="urn:other", tables={})) == []


def test_a_foreign_field_list_or_source_child_declares_nothing() -> None:
    """A lookalike inside the definition supplies neither fields nor a source."""
    content = xlsx(
        caches={
            CACHE_PART: definition(
                WORKSHEET_SOURCE + '<cacheFields xmlns="urn:other" count="3"/>'
            )
        }
    )
    (_where, record), = caches(content)
    assert record["fieldCount"] is None


def test_a_foreign_field_list_in_a_table_declares_nothing() -> None:
    content = xlsx(
        tables={
            TABLE_PART: table(
                '<location ref="E3:F9"/>'
                '<pivotFields xmlns="urn:other" count="3"><pivotField/></pivotFields>'
            )
        }
    )
    (_where, record), = tables(content)
    assert record["pivotFieldCount"] is None


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(
            '<pivotTableParts xmlns="urn:other" count="1">'
            '<pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts>",
            id="foreign-holder",
        ),
        # No count on these two: the list that qualifies is empty, and the
        # count's own agreement is tested on its own below.
        pytest.param(
            "<pivotTableParts>"
            '<pivotTablePart xmlns="urn:other" r:id="rIdPivot0"/>'
            "</pivotTableParts>",
            id="foreign-member",
        ),
        pytest.param(
            '<holder><pivotTableParts count="1">'
            '<pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts></holder>",
            id="holder-nested-deeper",
        ),
        pytest.param(
            "<pivotTableParts>"
            '<holder><pivotTablePart r:id="rIdPivot0"/></holder>'
            "</pivotTableParts>",
            id="member-nested-deeper",
        ),
    ],
)
def test_a_lookalike_pivot_table_declaration_declares_nothing(declared: str) -> None:
    """Only the schema's own route declares a report; everything else is transparent.

    The sheet's ``.rels`` still holds the relationship and the package still
    holds the part, so a reader taking either for the route would report a
    table here. None is reported, and the part is never opened.
    """
    content = xlsx(sheet_parts=declared)
    assert tables(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["pivotTables"] == 0


def test_an_unqualified_pivot_table_declaration_declares_nothing() -> None:
    """The holder stripped of the sheet's own namespace declares no report."""
    content = xlsx(
        extra={
            SHEET_PART: (
                f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}">'
                '<sheetData><row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
                "</sheetData>"
                '<pivotTableParts xmlns="" count="1">'
                '<pivotTablePart r:id="rIdPivot0"/>'
                "</pivotTableParts></worksheet>"
            )
        }
    )
    assert tables(content) == []


def test_a_worksheet_in_a_foreign_namespace_declares_no_pivot_tables() -> None:
    """The route is held to SpreadsheetML, as the declared-table route is."""
    content = xlsx(
        extra={
            SHEET_PART: worksheet(
                '<pivotTableParts count="1"><pivotTablePart r:id="rIdPivot0"/>'
                "</pivotTableParts>",
                main="urn:other",
            )
        }
    )
    assert tables(content) == []


def test_a_pivot_table_relationship_the_sheet_never_declares_is_inert() -> None:
    """A ``.rels`` entry is how a declaration resolves, not a declaration itself.

    The sheet declares no ``pivotTableParts`` at all, and its ``.rels`` holds a
    qualifying pivot-table relationship to a part that would be refused outright
    if it were ever opened. Nothing opens it.
    """
    content = xlsx(
        sheet_parts="",
        tables={TABLE_PART: f'<somethingElse xmlns="{MAIN}"/>'},
        sheet_rels=(
            "<Relationships>"
            '<Relationship Id="rIdPivot0" Target="../pivotTables/pivotTable1.xml"'
            f' Type="{RELNS}/pivotTable"/>'
            "</Relationships>"
        ),
    )
    assert tables(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["pivotTables"] == 0


def test_an_undeclared_relationship_beside_a_declared_one_stays_inert() -> None:
    """One report declared, two related: only the declared one is reported."""
    content = xlsx(
        tables={
            TABLE_PART: table(),
            "xl/pivotTables/pivotTable2.xml": f'<somethingElse xmlns="{MAIN}"/>',
        },
        sheet_parts=(
            '<pivotTableParts count="1"><pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts>"
        ),
    )
    assert [where["pivotTablePart"] for where, _record in tables(content)] == [
        TABLE_PART
    ]


# --------------------------------------------------------------------------
# Malformed declarations are refused


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        pytest.param(
            '<pivotCache r:id="rIdCache0"/>', "id is missing", id="no-cache-id"
        ),
        pytest.param(
            '<pivotCache cacheId="-1" r:id="rIdCache0"/>',
            "id is missing or malformed",
            id="negative-cache-id",
        ),
        pytest.param(
            '<pivotCache cacheId="4294967296" r:id="rIdCache0"/>',
            "id is missing or malformed",
            id="oversized-cache-id",
        ),
        pytest.param(
            '<pivotCache cacheId="0"/>', "names no relationship", id="no-relationship"
        ),
        pytest.param(
            '<pivotCache cacheId="0" r:id="rIdCache0"/>'
            '<pivotCache cacheId="0" r:id="rIdCache0"/>',
            "id is repeated",
            id="repeated-cache-id",
        ),
        pytest.param(
            '<pivotCache cacheId="0" r:id="rIdCache0"/>'
            '<pivotCache cacheId="1" r:id="rIdCache0"/>',
            "relationship is repeated",
            id="repeated-relationship",
        ),
    ],
)
def test_a_malformed_pivot_cache_declaration_is_refused(
    declarations: str, message: str
) -> None:
    with pytest.raises(OOXMLInvalid, match=message):
        caches(xlsx(declarations=declarations, tables={}))


def test_two_relationships_resolving_to_one_cache_part_are_refused() -> None:
    """One cache declared twice; nothing counts it once or twice arbitrarily."""
    content = xlsx(
        declarations=(
            '<pivotCache cacheId="0" r:id="rIdCache0"/>'
            '<pivotCache cacheId="1" r:id="rIdCache1"/>'
        ),
        workbook_rels=(
            "<Relationships>"
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            f'<Relationship Id="rIdCache0" Target="pivotCache/pivotCacheDefinition1.xml"'
            f' Type="{RELNS}/pivotCacheDefinition"/>'
            f'<Relationship Id="rIdCache1" Target="pivotCache/pivotCacheDefinition1.xml"'
            f' Type="{RELNS}/pivotCacheDefinition"/>'
            "</Relationships>"
        ),
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="part is referenced twice"):
        caches(content)


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        pytest.param(
            '<pivotTableParts count="1"><pivotTablePart/></pivotTableParts>',
            "names no relationship",
            id="no-relationship",
        ),
        pytest.param(
            '<pivotTableParts count="2">'
            '<pivotTablePart r:id="rIdPivot0"/><pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts>",
            "part declaration is repeated",
            id="repeated-relationship",
        ),
        pytest.param(
            '<pivotTableParts count="2"><pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts>",
            "count does not match its part list",
            id="count-above-the-list",
        ),
        pytest.param(
            '<pivotTableParts count="0"><pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts>",
            "count does not match its part list",
            id="count-below-the-list",
        ),
    ],
)
def test_a_malformed_pivot_table_declaration_is_refused(
    declared: str, message: str
) -> None:
    with pytest.raises(OOXMLInvalid, match=message):
        tables(xlsx(sheet_parts=declared))


def test_more_than_one_pivot_table_part_list_is_refused() -> None:
    content = xlsx(
        sheet_parts=(
            '<pivotTableParts count="1"><pivotTablePart r:id="rIdPivot0"/>'
            "</pivotTableParts><pivotTableParts count='0'/>"
        )
    )
    with pytest.raises(OOXMLInvalid, match="more than one pivot-table-part list"):
        tables(content)


def test_an_empty_pivot_table_part_list_reports_none_and_is_not_a_refusal() -> None:
    """An authored empty list stays distinguishable from no list at all."""
    content = xlsx(sheet_parts='<pivotTableParts count="0"/>', tables={})
    assert tables(content) == []
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["pivotTables"] == 0


def test_a_pivot_table_part_list_writing_no_count_is_not_a_refusal() -> None:
    """``count`` is optional, so the list itself is the width the sheet wrote."""
    content = xlsx(
        sheet_parts='<pivotTableParts><pivotTablePart r:id="rIdPivot0"/></pivotTableParts>'
    )
    assert [where["pivotTablePart"] for where, _record in tables(content)] == [
        TABLE_PART
    ]


def test_more_than_one_pivot_cache_list_is_refused() -> None:
    content = xlsx(
        tables={},
        extra={
            WORKBOOK_PART: workbook(
                '<pivotCaches><pivotCache cacheId="0" r:id="rIdCache0"/></pivotCaches>'
                "<pivotCaches/>"
            )
        },
    )
    with pytest.raises(OOXMLInvalid, match="more than one pivot-cache list"):
        caches(content)


@pytest.mark.parametrize(
    ("body", "attrs", "message"),
    [
        pytest.param(
            CACHED_FIELDS, CACHE_ATTRS, "exactly one source", id="no-source"
        ),
        pytest.param(
            WORKSHEET_SOURCE + WORKSHEET_SOURCE,
            CACHE_ATTRS,
            "exactly one source",
            id="two-sources",
        ),
        pytest.param(
            '<cacheSource type="pivot"/>',
            CACHE_ATTRS,
            "source type is missing or undefined",
            id="undefined-source-type",
        ),
        pytest.param(
            "<cacheSource/>",
            CACHE_ATTRS,
            "source type is missing or undefined",
            id="no-source-type",
        ),
        pytest.param(
            '<cacheSource type="external">'
            '<worksheetSource ref="A1:C5" sheet="Sales"/>'
            "</cacheSource>",
            CACHE_ATTRS,
            "worksheet source under another source type",
            id="worksheet-source-under-external",
        ),
        pytest.param(
            '<cacheSource type="worksheet">'
            '<worksheetSource ref="A1"/><worksheetSource ref="B2"/>'
            "</cacheSource>",
            CACHE_ATTRS,
            "worksheet source twice",
            id="two-worksheet-sources",
        ),
        pytest.param(
            '<cacheSource type="worksheet">'
            '<worksheetSource ref="C5:A1" sheet="Sales"/>'
            "</cacheSource>",
            CACHE_ATTRS,
            "cell range is malformed",
            id="reversed-source-range",
        ),
        pytest.param(
            '<cacheSource type="worksheet" connectionId="-2"/>',
            CACHE_ATTRS,
            "numeric attribute is malformed",
            id="negative-connection-id",
        ),
        pytest.param(
            WORKSHEET_SOURCE + '<cacheFields count="2"><cacheField name="A"/></cacheFields>',
            CACHE_ATTRS,
            "field count does not match",
            id="field-count-mismatch",
        ),
        pytest.param(
            WORKSHEET_SOURCE + "<cacheFields/><cacheFields/>",
            CACHE_ATTRS,
            "field list twice",
            id="two-field-lists",
        ),
        pytest.param(
            WORKSHEET_SOURCE,
            'saveData="yes"',
            "pivot flag is malformed",
            id="open-boolean",
        ),
        pytest.param(
            WORKSHEET_SOURCE,
            'recordCount="-4"',
            "numeric attribute is malformed",
            id="negative-record-count",
        ),
        pytest.param(
            WORKSHEET_SOURCE,
            'refreshedVersion="256"',
            "numeric attribute is malformed",
            id="oversized-version",
        ),
    ],
)
def test_a_malformed_cache_definition_is_refused(
    body: str, attrs: str, message: str
) -> None:
    content = xlsx(
        caches={CACHE_PART: definition(body, attrs=attrs, records_id=None)},
        record_parts={},
        cache_rels="<Relationships/>",
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match=message):
        caches(content)


@pytest.mark.parametrize(
    ("body", "attrs", "message"),
    [
        pytest.param(
            TABLE_BODY, 'cacheId="0"', "declares no name", id="no-name"
        ),
        pytest.param(
            TABLE_BODY, 'name="Report"', "cache id is missing", id="no-cache-id"
        ),
        pytest.param(
            TABLE_BODY,
            'name="Report" cacheId="x"',
            "cache id is missing or malformed",
            id="malformed-cache-id",
        ),
        pytest.param(
            '<location ref="E3"/><location ref="F4"/>',
            'name="Report" cacheId="0"',
            "location twice",
            id="two-locations",
        ),
        pytest.param(
            '<location ref="F9:E3"/>',
            'name="Report" cacheId="0"',
            "cell range is malformed",
            id="reversed-location",
        ),
        pytest.param(
            '<rowFields count="1"/><rowFields count="1"/>',
            'name="Report" cacheId="0"',
            "field list twice",
            id="two-row-field-lists",
        ),
        pytest.param(
            TABLE_BODY,
            'name="Report" cacheId="0" updatedVersion="999"',
            "numeric attribute is malformed",
            id="oversized-version",
        ),
    ],
)
def test_a_malformed_table_definition_is_refused(
    body: str, attrs: str, message: str
) -> None:
    content = xlsx(tables={TABLE_PART: table(body, attrs=attrs)})
    with pytest.raises(OOXMLInvalid, match=message):
        tables(content)


# --------------------------------------------------------------------------
# Wrong roots, wrong content types, wrong relationships


@pytest.mark.parametrize(
    ("part", "replacement", "message"),
    [
        pytest.param(
            CACHE_PART,
            '<pivotCacheDefinition xmlns="urn:other"/>',
            "cache part uses an unsupported namespace",
            id="cache-foreign-namespace",
        ),
        pytest.param(
            CACHE_PART,
            f'<connections xmlns="{MAIN}"/>',
            "not rooted at its own definition",
            id="cache-wrong-root",
        ),
        pytest.param(
            TABLE_PART,
            '<pivotTableDefinition xmlns="urn:other"/>',
            "table part uses an unsupported namespace",
            id="table-foreign-namespace",
        ),
        pytest.param(
            TABLE_PART,
            f'<table xmlns="{MAIN}"/>',
            "not rooted at its own definition",
            id="table-wrong-root",
        ),
        pytest.param(
            RECORDS_PART,
            '<pivotCacheRecords xmlns="urn:other"/>',
            "records part uses an unsupported namespace",
            id="records-foreign-namespace",
        ),
        pytest.param(
            RECORDS_PART,
            f'<pivotCacheDefinition xmlns="{MAIN}"/>',
            "not rooted at its own records",
            id="records-wrong-root",
        ),
    ],
)
def test_a_part_that_is_not_what_it_was_declared_as_is_refused(
    part: str, replacement: str, message: str
) -> None:
    overrides = {CACHE_PART: definition(), TABLE_PART: table(), RECORDS_PART: records()}
    overrides[part] = replacement
    content = xlsx(
        caches={CACHE_PART: overrides[CACHE_PART]},
        tables={TABLE_PART: overrides[TABLE_PART]},
        record_parts={RECORDS_PART: overrides[RECORDS_PART]},
    )
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param(
            {"cache_ct": TABLE_CT},
            "does not name a pivot cache part",
            id="cache-declared-as-a-table",
        ),
        pytest.param(
            {"table_ct": CACHE_CT},
            "does not name a pivot table part",
            id="table-declared-as-a-cache",
        ),
        pytest.param(
            {"records_ct": CACHE_CT},
            "does not name a pivot cache records part",
            id="records-declared-as-a-cache",
        ),
        pytest.param(
            {"cache_type": f"{RELNS}/pivotTable"},
            "names a relationship of another type",
            id="cache-reached-by-a-pivot-table-relationship",
        ),
        pytest.param(
            {"records_type": f"{RELNS}/pivotCacheDefinition"},
            "names a relationship of another type",
            id="records-reached-by-a-cache-relationship",
        ),
    ],
)
def test_a_part_reached_by_the_wrong_declaration_is_refused(
    kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(xlsx(**kwargs), capture_id=CAPTURE)


def test_a_declaration_naming_a_relationship_of_another_type_is_refused() -> None:
    """The sheet declared a report, and what it named is not one."""
    with pytest.raises(OOXMLInvalid, match="names a relationship of another type"):
        tables(xlsx(table_type=f"{RELNS}/table"))


def test_a_missing_cache_relationship_is_refused() -> None:
    content = xlsx(
        workbook_rels=(
            "<Relationships>"
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            "</Relationships>"
        ),
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        caches(content)


def test_a_missing_records_relationship_is_refused() -> None:
    content = xlsx(cache_rels="<Relationships/>", record_parts={}, tables={})
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        caches(content)


def test_an_external_cache_relationship_is_refused() -> None:
    """The definition is a member of this package; an external one contradicts that."""
    content = xlsx(
        workbook_rels=(
            "<Relationships>"
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            '<Relationship Id="rIdCache0" TargetMode="External"'
            ' Target="https://example.invalid/cache.xml"'
            f' Type="{RELNS}/pivotCacheDefinition"/>'
            "</Relationships>"
        ),
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="relationship is external"):
        caches(content)


def test_an_external_records_relationship_is_refused() -> None:
    content = xlsx(
        cache_rels=(
            "<Relationships>"
            '<Relationship Id="rIdRecords" TargetMode="External"'
            ' Target="https://example.invalid/records.xml"'
            f' Type="{RELNS}/pivotCacheRecords"/>'
            "</Relationships>"
        ),
        record_parts={},
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="relationship is external"):
        caches(content)


def test_an_external_pivot_table_relationship_is_refused() -> None:
    content = xlsx(
        sheet_rels=(
            "<Relationships>"
            '<Relationship Id="rIdPivot0" TargetMode="External"'
            ' Target="https://example.invalid/pivotTable1.xml"'
            f' Type="{RELNS}/pivotTable"/>'
            "</Relationships>"
        )
    )
    with pytest.raises(OOXMLInvalid, match="relationship is external"):
        tables(content)


def test_two_declarations_resolving_to_one_table_part_are_refused() -> None:
    """Two distinct ids, one part: the report is declared twice."""
    content = xlsx(
        sheet_parts=(
            '<pivotTableParts count="2">'
            '<pivotTablePart r:id="rIdPivot0"/><pivotTablePart r:id="rIdPivot1"/>'
            "</pivotTableParts>"
        ),
        sheet_rels=(
            "<Relationships>"
            '<Relationship Id="rIdPivot0" Target="../pivotTables/pivotTable1.xml"'
            f' Type="{RELNS}/pivotTable"/>'
            '<Relationship Id="rIdPivot1" Target="../pivotTables/pivotTable1.xml"'
            f' Type="{RELNS}/pivotTable"/>'
            "</Relationships>"
        ),
    )
    with pytest.raises(OOXMLInvalid, match="pivot table part is referenced twice"):
        tables(content)


def test_a_relationship_escaping_the_package_is_refused() -> None:
    """Traversal is settled when the relationship is resolved, before any read."""
    content = xlsx(
        sheet_rels=(
            "<Relationships>"
            '<Relationship Id="rIdPivot0" Target="../../../etc/passwd"'
            f' Type="{RELNS}/pivotTable"/>'
            "</Relationships>"
        )
    )
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        tables(content)


def test_a_part_the_package_does_not_hold_is_refused() -> None:
    content = xlsx(
        sheet_rels=(
            "<Relationships>"
            '<Relationship Id="rIdPivot0" Target="../pivotTables/pivotTable9.xml"'
            f' Type="{RELNS}/pivotTable"/>'
            "</Relationships>"
        )
    )
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        tables(content)


def test_a_part_declared_by_no_override_is_refused() -> None:
    """A ``Default`` for ``.xml`` qualifies no part as a pivot part."""
    content = xlsx(
        caches={CACHE_PART: definition(records_id=None)},
        tables={},
        record_parts={},
        cache_rels="<Relationships/>",
        extra={
            "[Content_Types].xml": (
                '<Types><Default Extension="xml" ContentType="application/xml"/></Types>'
            )
        },
    )
    with pytest.raises(OOXMLInvalid, match="does not name a pivot cache part"):
        caches(content)


# --------------------------------------------------------------------------
# Bounds


def test_too_many_declared_pivot_caches_are_refused_before_any_part_is_opened() -> None:
    """Counted from the declarations, so none of the 257 parts is ever read."""
    content = xlsx(
        caches={},
        declarations="".join(
            f'<pivotCache cacheId="{index}" r:id="rIdCache{index}"/>'
            for index in range(257)
        ),
        record_parts={},
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="too many pivot caches"):
        caches(content)


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(
            '<pivotTableParts count="257">'
            + "".join(
                f'<pivotTablePart r:id="rIdPivot{index:03d}"/>' for index in range(257)
            )
            + "</pivotTableParts>",
            id="declared-count-and-list",
        ),
        # No count at all, so only the list itself carries the width.
        pytest.param(
            "<pivotTableParts>"
            + "".join(
                f'<pivotTablePart r:id="rIdPivot{index:03d}"/>' for index in range(257)
            )
            + "</pivotTableParts>",
            id="list-alone",
        ),
        # A width nothing wrote: the count is past the bound and the list is
        # never collected, let alone resolved.
        pytest.param(
            '<pivotTableParts count="4294967295"/>', id="declared-count-alone"
        ),
        pytest.param('<pivotTableParts count="many"/>', id="unreadable-count"),
    ],
)
def test_too_many_declared_pivot_tables_are_refused(declared: str) -> None:
    """Counted from the declarations, before the first definition is opened.

    Each id names nothing the sheet's ``.rels`` holds, which would itself be a
    refusal -- and the bound is taken first, so that is the one reported.
    """
    content = xlsx(sheet_parts=declared, sheet_rels="<Relationships/>")
    with pytest.raises(OOXMLInvalid, match="too many pivot tables or no count"):
        tables(content)


def test_a_declared_cache_field_count_past_the_bound_is_refused() -> None:
    """Bounded from the declared count, before the field list is collected."""
    content = xlsx(
        caches={
            CACHE_PART: definition(
                WORKSHEET_SOURCE + '<cacheFields count="16385"/>', records_id=None
            )
        },
        record_parts={},
        cache_rels="<Relationships/>",
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="too many fields or no count"):
        caches(content)


def test_an_unreadable_cache_field_count_is_refused() -> None:
    content = xlsx(
        caches={
            CACHE_PART: definition(
                WORKSHEET_SOURCE + '<cacheFields count="many"/>', records_id=None
            )
        },
        record_parts={},
        cache_rels="<Relationships/>",
        tables={},
    )
    with pytest.raises(OOXMLInvalid, match="too many fields or no count"):
        caches(content)


def test_an_oversized_stored_record_count_is_refused() -> None:
    content = xlsx(record_parts={RECORDS_PART: records(count="4294967296")}, tables={})
    with pytest.raises(OOXMLInvalid, match="numeric attribute is malformed"):
        caches(content)


# --------------------------------------------------------------------------
# What this route does not support, stated


def test_a_pivot_table_is_never_placed_against_the_cells_it_covers() -> None:
    """``location`` is an extent, like a table's range: no cell is materialised.

    The sheet writes one cell, at ``A1``, and the report covers ``E3:F9``. The
    extraction reports exactly the one cell the sheet wrote.
    """
    extraction = extract_xlsx(xlsx(), capture_id=CAPTURE)
    cells = [item for item in extraction.segments if item.semantic_role == "table_cell"]
    assert [item.source_locator["cell"] for item in cells] == ["A1"]
    assert extraction.expected == 1


def test_a_cache_s_stored_record_count_is_never_agreed_against_its_records() -> None:
    """Agreeing the two would mean reading the rows, so the file's figure stands.

    The definition claims four records and the records part stores two. Both are
    reported as stored and neither is corrected against the other.
    """
    content = xlsx(
        record_parts={
            RECORDS_PART: (
                f'<pivotCacheRecords xmlns="{MAIN}" count="2">'
                '<r><x v="0"/></r><r><x v="1"/></r>'
                "</pivotCacheRecords>"
            )
        },
        tables={},
    )
    (_where, record), = caches(content)
    assert (record["recordCount"], record["recordsPartCount"]) == (4, 2)
