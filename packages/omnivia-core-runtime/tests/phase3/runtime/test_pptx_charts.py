"""PPTX chart parts: admission, stored-value extraction and refusals.

Only the chart references a slide's graphic frames already carry are followed,
and only what the chart part already stored is read -- a reference's cache, a
literal list written inline, and each level of a multi-level category axis. No
embedded workbook is opened, no absent cache value is inferred and nothing is
calculated, so every record is reported with a freshness of ``unknown`` and a
source formula is inert metadata beside the value.

The four record kinds the packet names -- ``chart_title``,
``chart_series_name``, ``chart_category`` and ``chart_value`` -- ride in the
locator's ``chartRole``, under the ``chart`` role the repository already
accepts. ``semantic_role`` is closed twice over: ``SEGMENT_ROLES`` in
``storage/document_intake.py``, and a ``CHECK (semantic_role IN (...))`` on both
segment tables in the already-applied ``0069_document_intake.sql``. The locator
is the open half of the contract -- validated for size only -- so it is where
the structural detail belongs, as ``mergedRange`` and ``rowSpan`` already do.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import _validated_segments
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The two DrawingML chart namespaces a chart part may be rooted in.
TRANSITIONAL_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
STRICT_NS = "http://purl.oclc.org/ooxml/drawingml/chart"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
# The two relationship-type URIs OOXML defines for a chart.
CHART_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
STRICT_CHART_TYPE = "http://purl.oclc.org/ooxml/officeDocument/relationships/chart"
CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CHART_PART = "ppt/charts/chart1.xml"
CHART_RELATION = (
    f'<Relationship Id="rId2" Target="../charts/chart1.xml" Type="{CHART_TYPE}"/>'
)


def points(*records: tuple[str, str | None]) -> str:
    """``c:pt`` records; a None value leaves the ``c:v`` out entirely."""
    return "".join(
        f'<c:pt idx="{index}">'
        + ("" if value is None else f"<c:v>{value}</c:v>")
        + "</c:pt>"
        for index, value in records
    )


def ref(kind: str, formula: str | None, body: str | None) -> str:
    """One ``c:strRef``/``c:numRef``; a None body leaves the cache out entirely."""
    inner = "" if formula is None else f"<c:f>{formula}</c:f>"
    if body is not None:
        inner += f"<c:{kind}Cache>{body}</c:{kind}Cache>"
    return f"<c:{kind}Ref>{inner}</c:{kind}Ref>"


def count(value: str | None) -> str:
    return "" if value is None else f'<c:ptCount val="{value}"/>'


def lit(kind: str, body: str) -> str:
    """One ``c:strLit``/``c:numLit``: the point list written into the chart."""
    return f"<c:{kind}Lit>{body}</c:{kind}Lit>"


def multi(formula: str, *levels: str, total: str | None = None) -> str:
    """One ``c:multiLvlStrRef``, its levels in the order they are written."""
    body = count(total) + "".join(f"<c:lvl>{level}</c:lvl>" for level in levels)
    return (
        f"<c:multiLvlStrRef><c:f>{formula}</c:f>"
        f"<c:multiLvlStrCache>{body}</c:multiLvlStrCache></c:multiLvlStrRef>"
    )


def series(
    *,
    idx: str | None = "0",
    order: str | None = "0",
    name: str | None = ref("str", "Sheet1!$B$1", count("1") + points(("0", "Revenue"))),
    cat: str | None = ref(
        "str", "Sheet1!$A$2:$A$3", count("2") + points(("0", "Q1"), ("1", "Q2"))
    ),
    val: str | None = ref(
        "num", "Sheet1!$B$2:$B$3", count("2") + points(("0", "12"), ("1", "15.5"))
    ),
) -> str:
    """One ``c:ser``; a None slot or index is left out entirely."""
    slots = "".join(
        f"<c:{tag}>{body}</c:{tag}>"
        for tag, body in (("tx", name), ("cat", cat), ("val", val))
        if body is not None
    )
    indices = "".join(
        f'<c:{tag} val="{value}"/>'
        for tag, value in (("idx", idx), ("order", order))
        if value is not None
    )
    return f"<c:ser>{indices}{slots}</c:ser>"


RICH_TITLE = (
    "<c:title><c:tx><c:rich>"
    "<a:p><a:r><a:t>Quarterly</a:t></a:r><a:r><a:t> revenue</a:t></a:r></a:p>"
    "</c:rich></c:tx></c:title>"
)


def chart(
    body: str | None = None,
    *,
    namespace: str = TRANSITIONAL_NS,
    root: str = "chartSpace",
) -> str:
    """One chart part holding the given ``c:chart`` body."""
    if body is None:
        body = RICH_TITLE + f"<c:plotArea><c:barChart>{series()}</c:barChart></c:plotArea>"
    return (
        f'<c:{root} xmlns:c="{namespace}" xmlns:a="{DRAWING_NS}">'
        f"<c:chart>{body}</c:chart></c:{root}>"
    )


def pptx(
    *,
    charts: dict[str, str] | None = None,
    slide_rels: str | None = CHART_RELATION,
    reference: str = '<c:chart xmlns:c="urn:c" xmlns:r="urn:r" r:id="rId2"/>',
    grouped: bool = False,
    overrides: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a one-slide presentation whose graphic frame refers to a chart.

    ``charts`` maps part name to the chart each holds; a part left out of it is
    simply not in the package, which is how a phantom declared-only part is
    built. ``overrides`` defaults to declaring exactly the parts that are there.
    """
    parts = {CHART_PART: chart()} if charts is None else dict(charts)
    if overrides is None:
        overrides = "".join(
            f'<Override PartName="/{name}" ContentType="{CHART_CT}"/>' for name in parts
        )
    frame = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3"/></p:nvGraphicFramePr>'
        f"<a:graphic><a:graphicData>{reference}</a:graphicData></a:graphic>"
        "</p:graphicFrame>"
    )
    if grouped:
        frame = (
            '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9"/></p:nvGrpSpPr><p:grpSpPr/>'
            f"{frame}</p:grpSp>"
        )
    parts["[Content_Types].xml"] = f"<Types>{overrides}</Types>"
    parts["ppt/presentation.xml"] = (
        '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
        '<p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/slides/slide1.xml"] = (
        '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree>'
        f"{frame}</p:spTree></p:cSld></p:sld>"
    )
    if slide_rels is not None:
        parts["ppt/slides/_rels/slide1.xml.rels"] = (
            f"<Relationships>{slide_rels}</Relationships>"
        )
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(content: bytes) -> list[tuple[str | None, dict[str, object]]]:
    return [
        (item.text, dict(item.source_locator))
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ]


def shape(content: bytes) -> list[tuple[str, str | None, str | None, str | None]]:
    """The (role, value, point index, formula) of each record, in source order."""
    return [
        (
            str(locator["chartRole"]),
            text,
            locator["pointIndex"],  # type: ignore[misc]
            locator["formula"],  # type: ignore[misc]
        )
        for text, locator in records(content)
    ]


def levelled(
    content: bytes,
) -> list[tuple[str, str | None, str | None, str | None, str | None]]:
    """The (role, value, level, point index, formula) of each record, in order."""
    return [
        (
            str(locator["chartRole"]),
            text,
            locator["categoryLevel"],  # type: ignore[misc]
            locator["pointIndex"],  # type: ignore[misc]
            locator["formula"],  # type: ignore[misc]
        )
        for text, locator in records(content)
    ]


# The provenance the default fixture's title record carries. Every other record
# differs from it only in ``chartRole`` and the place inside the chart.
EXPECTED = {
    "captureId": CAPTURE,
    "part": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
    "shapeId": "3",
    "shapeKind": "graphicFrame",
    "groupPath": [],
    "zOrder": 0,
    "zPath": [0],
    "readingOrder": 0,
    "readingPath": [0],
    "geometry": None,
    "placeholderType": None,
    "placeholderIndex": None,
    "chartPart": CHART_PART,
    "relationshipId": "rId2",
    "seriesIndex": None,
    "seriesOrder": None,
    "categoryLevel": None,
    "pointIndex": None,
    "formula": None,
    "chartRole": "chart_title",
    "freshness": "unknown",
}


def test_a_chart_reports_its_stored_title_and_every_cached_point() -> None:
    """Title, series name, categories and values, in the order written."""
    content = pptx()

    assert shape(content) == [
        ("chart_title", "Quarterly revenue", None, None),
        ("chart_series_name", "Revenue", "0", "Sheet1!$B$1"),
        ("chart_category", "Q1", "0", "Sheet1!$A$2:$A$3"),
        ("chart_category", "Q2", "1", "Sheet1!$A$2:$A$3"),
        ("chart_value", "12", "0", "Sheet1!$B$2:$B$3"),
        ("chart_value", "15.5", "1", "Sheet1!$B$2:$B$3"),
    ]
    (title, _name, _first, _second, _third, value) = records(content)
    assert title == ("Quarterly revenue", EXPECTED)
    # Numbers come through exactly as cached: nothing is parsed or recomputed.
    assert value == (
        "15.5",
        EXPECTED
        | {
            "chartRole": "chart_value",
            "seriesIndex": "0",
            "seriesOrder": "0",
            "pointIndex": "1",
            "formula": "Sheet1!$B$2:$B$3",
        },
    )
    assert [
        item.visibility
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ] == ["visible"] * 6


def test_the_strict_relationship_and_namespace_reach_the_same_chart() -> None:
    """Strict and Transitional each name the one type, and the one vocabulary."""
    strict = pptx(
        charts={CHART_PART: chart(namespace=STRICT_NS)},
        slide_rels=CHART_RELATION.replace(CHART_TYPE, STRICT_CHART_TYPE),
    )

    assert shape(strict) == shape(pptx())
    ((_text, locator),) = records(strict)[:1]
    assert locator == EXPECTED


def test_a_chart_inside_a_group_keeps_the_whole_path_to_its_frame() -> None:
    content = pptx(grouped=True)

    ((_text, locator),) = records(content)[:1]
    assert (locator["groupPath"], locator["zPath"], locator["shapeId"]) == (
        ["9"],
        [0, 0],
        "3",
    )


def test_a_referenced_title_carries_its_cache_and_formula() -> None:
    """A title taken from a cell is the stored copy, reported with its source."""
    titled = (
        "<c:title><c:tx>"
        + ref("str", "Sheet1!$A$1", count("1") + points(("0", "From a cell")))
        + "</c:tx></c:title>"
    )
    content = pptx(
        charts={CHART_PART: chart(titled + f"<c:plotArea>{series()}</c:plotArea>")}
    )

    assert shape(content)[0] == ("chart_title", "From a cell", "0", "Sheet1!$A$1")


def test_a_literal_series_name_is_read_without_a_cache() -> None:
    """``c:tx/c:v`` is the name written inline; it names no source at all."""
    content = pptx(
        charts={
            CHART_PART: chart(
                f"<c:plotArea>{series(name='<c:v>Inline</c:v>', cat=None, val=None)}"
                "</c:plotArea>"
            )
        }
    )

    assert shape(content) == [("chart_series_name", "Inline", None, None)]


def test_an_uncached_or_empty_reference_reports_nothing() -> None:
    """Nothing is fetched for a reference that stored no cache of its own."""
    content = _plotted(
        series(
            name=ref("str", "Sheet1!$B$1", None),
            cat="<c:multiLvlStrRef><c:f>Sheet1!$A$1:$B$2</c:f></c:multiLvlStrRef>",
            val=ref("num", "Sheet1!$B$2", count("0")),
        )
    )

    assert shape(content) == []
    # A slot holding no source at all stored nothing either.
    assert shape(_plotted(series(name="", cat="", val=""))) == []


def test_a_reference_reads_its_points_from_its_own_cache_and_nowhere_else() -> None:
    """A ``c:strRef`` keeps its stored copy in a ``c:strCache``, not another."""
    swapped = (
        "<c:strRef><c:f>Sheet1!$A$1</c:f><c:numCache>"
        + count("1")
        + points(("0", "12"))
        + "</c:numCache></c:strRef>"
    )

    assert shape(_plotted(series(name=None, cat=swapped, val=None))) == []


def test_inline_literal_lists_are_read_as_written() -> None:
    """A literal list is the values written into the chart; it names no source.

    The list is sparse like a cache -- index 1 below is simply not there -- and
    the indices it does claim are reported as the chart wrote them.
    """
    content = _plotted(
        series(
            name="<c:v>Inline</c:v>",
            cat=lit("str", count("3") + points(("0", "Q1"), ("2", "Q3"))),
            val=lit("num", count("3") + points(("0", "12"), ("2", "15.5"))),
        )
    )

    assert shape(content) == [
        ("chart_series_name", "Inline", None, None),
        ("chart_category", "Q1", "0", None),
        ("chart_category", "Q3", "2", None),
        ("chart_value", "12", "0", None),
        ("chart_value", "15.5", "2", None),
    ]
    ((_text, locator),) = records(content)[1:2]
    assert locator == EXPECTED | {
        "chartRole": "chart_category",
        "seriesIndex": "0",
        "seriesOrder": "0",
        "pointIndex": "0",
    }


def test_multilevel_categories_report_every_level_in_source_order() -> None:
    """Each level is a column of the category axis, numbered as it is written."""
    content = _plotted(
        series(
            name=None,
            cat=multi(
                "Sheet1!$A$2:$B$3",
                points(("0", "Q1"), ("1", "Q2")),
                points(("0", "2024"), ("1", "2025")),
                total="2",
            ),
            val=None,
        )
    )

    assert levelled(content) == [
        ("chart_category", "Q1", "0", "0", "Sheet1!$A$2:$B$3"),
        ("chart_category", "Q2", "0", "1", "Sheet1!$A$2:$B$3"),
        ("chart_category", "2024", "1", "0", "Sheet1!$A$2:$B$3"),
        ("chart_category", "2025", "1", "1", "Sheet1!$A$2:$B$3"),
    ]
    ((_text, locator),) = records(content)[2:3]
    assert locator == EXPECTED | {
        "chartRole": "chart_category",
        "seriesIndex": "0",
        "seriesOrder": "0",
        "categoryLevel": "1",
        "pointIndex": "0",
        "formula": "Sheet1!$A$2:$B$3",
    }


def test_one_index_in_two_levels_is_two_positions_not_a_repeat() -> None:
    """``ptCount`` sits on the cache, so it bounds every level's indices alike."""
    content = _plotted(
        series(
            name=None,
            cat=multi(
                "Sheet1!$A$1:$B$1",
                points(("0", "Q1")),
                points(("0", "2024")),
                total="1",
            ),
            val=None,
        )
    )

    assert levelled(content) == [
        ("chart_category", "Q1", "0", "0", "Sheet1!$A$1:$B$1"),
        ("chart_category", "2024", "1", "0", "Sheet1!$A$1:$B$1"),
    ]


def test_the_strict_namespace_reads_literals_and_levels_the_same() -> None:
    """Below the root every element is read by local name, in either namespace."""
    body = (
        "<c:plotArea>"
        + series(
            name="<c:v>Inline</c:v>",
            cat=multi("Sheet1!$A$1", points(("0", "Q1")), total="1"),
            val=lit("num", count("1") + points(("0", "12"))),
        )
        + "</c:plotArea>"
    )
    strict = pptx(
        charts={CHART_PART: chart(body, namespace=STRICT_NS)},
        slide_rels=CHART_RELATION.replace(CHART_TYPE, STRICT_CHART_TYPE),
    )

    assert levelled(strict) == [
        ("chart_series_name", "Inline", None, None, None),
        ("chart_category", "Q1", "0", "0", "Sheet1!$A$1"),
        ("chart_value", "12", None, "0", None),
    ]
    assert levelled(strict) == levelled(pptx(charts={CHART_PART: chart(body)}))


def test_an_axis_title_is_not_the_charts_own_title() -> None:
    """A ``c:title`` below an axis names that axis, not the chart it is on."""
    axis = (
        "<c:plotArea><c:valAx><c:title><c:tx><c:rich><a:p><a:r>"
        "<a:t>Pounds</a:t></a:r></a:p></c:rich></c:tx></c:title></c:valAx>"
        "</c:plotArea>"
    )

    assert shape(pptx(charts={CHART_PART: chart(axis)})) == []


def test_an_embedded_workbook_is_never_opened() -> None:
    """The chart's own package relationships are not followed to its data."""
    embedded = (
        '<Relationship Id="rIdX" Target="../embeddings/book.xlsx" Type="http://'
        'schemas.openxmlformats.org/officeDocument/2006/relationships/package"/>'
    )
    book = {
        "ppt/charts/_rels/chart1.xml.rels": f"<Relationships>{embedded}</Relationships>",
        "ppt/embeddings/book.xlsx": "not a workbook anything here reads",
    }

    assert shape(pptx(members=book)) == shape(pptx())

    # A formula naming the embedded book is metadata beside the stored value,
    # not a path to it: the one cached level is all that is reported.
    pointed = pptx(
        charts={
            CHART_PART: chart(
                "<c:plotArea>"
                + series(
                    name=None,
                    cat=multi("[1]Sheet1!$A$1:$A$9", points(("0", "Stored")), total="9"),
                    val=None,
                )
                + "</c:plotArea>"
            )
        },
        members=book,
    )

    assert levelled(pointed) == [
        ("chart_category", "Stored", "0", "0", "[1]Sheet1!$A$1:$A$9")
    ]


def test_an_empty_cached_value_is_reported_as_the_empty_claim_it_is() -> None:
    """A cache that stored an empty string claims a point and says it is blank."""
    content = pptx(
        charts={
            CHART_PART: chart(
                "<c:plotArea>"
                + series(cat=None, val=ref("num", "Sheet1!$B$2", count("1") + points(("0", ""))))
                + "</c:plotArea>"
            )
        }
    )

    (_name, blank) = [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ]
    assert (blank.text, blank.structured_value) == (None, {"value": ""})

    # The extraction document_intake would store is one it accepts.
    stored, _digest = _validated_segments(
        extract_pptx(content, capture_id=CAPTURE).segments,
        workspace_id="ws-pptx-charts",
        extraction_revision_id="exr-pptx-charts",
        capture_id=CAPTURE,
        fencing_generation=1,
    )
    (_first, row) = [item for item in stored if item.semantic_role == "chart"]
    assert row.structured_value_json is not None
    assert json.loads(row.structured_value_json) == {"value": ""}


def test_markup_inside_a_cached_value_stays_inert_text() -> None:
    payload = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; 2"
    content = pptx(
        charts={
            CHART_PART: chart(
                "<c:plotArea>"
                + series(
                    name=f"<c:v>{payload}</c:v>",
                    cat=None,
                    val=ref("num", None, count("1") + points(("0", payload))),
                )
                + "</c:plotArea>"
            )
        }
    )

    assert [text for text, _ in records(content)] == [
        "<script>alert(1)</script> & 2"
    ] * 2


def test_a_chart_part_nothing_relates_to_is_never_read() -> None:
    """No chart path is derived from the slide's name, so an orphan is inert."""
    content = pptx(
        reference='<a:pic xmlns:a="urn:a"/>',
        members={"ppt/charts/chart9.xml": chart()},
        charts={CHART_PART: chart(), "ppt/charts/chart9.xml": chart()},
    )

    assert records(content) == []


def test_a_relationship_of_another_type_is_refused() -> None:
    """The frame names this id as its chart, so a lookalike cannot stand in.

    Every target below is the real, qualified chart part; only the relationship
    type fails to qualify.
    """
    for kind in (
        # Same tail, different host.
        "http://charts.example.invalid/officeDocument/2006/relationships/chart",
        # The defined URI with something appended, and with something prepended.
        CHART_TYPE + "Extra",
        "https://proxy.example.invalid/" + CHART_TYPE,
        # The path is case-sensitive, so a folded path names another type.
        CHART_TYPE.replace("officeDocument", "officedocument"),
    ):
        with pytest.raises(OOXMLInvalid, match="relationship of another type"):
            extract_pptx(
                pptx(slide_rels=CHART_RELATION.replace(CHART_TYPE, kind)),
                capture_id=CAPTURE,
            )

    # Scheme and host are case-insensitive, so folding them still qualifies.
    shouted = "HTTP://SCHEMAS.OPENXMLFORMATS.ORG" + CHART_TYPE.partition(".org")[2]
    assert shape(pptx(slide_rels=CHART_RELATION.replace(CHART_TYPE, shouted))) == shape(
        pptx()
    )


def test_a_missing_external_or_ambiguous_reference_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        extract_pptx(pptx(slide_rels=None), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="names a missing relationship"):
        extract_pptx(
            pptx(slide_rels=CHART_RELATION.replace('Id="rId2"', 'Id="rId7"')),
            capture_id=CAPTURE,
        )

    external = (
        f'<Relationship Id="rId2" Type="{CHART_TYPE}" TargetMode="External"'
        ' Target="https://example.invalid/chart1.xml"/>'
    )
    with pytest.raises(OOXMLInvalid, match="chart relationship is external"):
        extract_pptx(pptx(slide_rels=external), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="names no relationship"):
        extract_pptx(pptx(reference='<c:chart xmlns:c="urn:c"/>'), capture_id=CAPTURE)

    doubled = '<c:chart xmlns:c="urn:c" xmlns:r="urn:r" r:id="rId2"/>'
    with pytest.raises(OOXMLInvalid, match="more than one chart reference"):
        extract_pptx(pptx(reference=doubled * 2), capture_id=CAPTURE)


def test_a_frame_claiming_both_a_table_and_a_chart_is_refused() -> None:
    table = (
        "<a:tbl><a:tblGrid><a:gridCol/></a:tblGrid>"
        "<a:tr><a:tc><a:txBody><a:p/></a:txBody></a:tc></a:tr></a:tbl>"
    )
    reference = '<c:chart xmlns:c="urn:c" xmlns:r="urn:r" r:id="rId2"/>'

    with pytest.raises(OOXMLInvalid, match="more than one graphic"):
        extract_pptx(pptx(reference=reference + table), capture_id=CAPTURE)


def test_an_undeclared_wrongly_declared_or_phantom_target_is_refused() -> None:
    """The target has to be declared a chart part, not merely be named one."""
    layout = (
        f'<Override PartName="/{CHART_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
    )
    for overrides in ("", layout):
        with pytest.raises(OOXMLInvalid, match="does not name a chart part"):
            extract_pptx(pptx(overrides=overrides), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(
            pptx(
                charts={},
                overrides=f'<Override PartName="/{CHART_PART}"'
                f' ContentType="{CHART_CT}"/>',
            ),
            capture_id=CAPTURE,
        )

    traversal = CHART_RELATION.replace("../charts/chart1.xml", "../../../etc/passwd")
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(pptx(slide_rels=traversal), capture_id=CAPTURE)


def test_a_chart_part_in_another_namespace_or_root_is_refused() -> None:
    """The declared type says what the part is; the root says so itself."""
    with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
        extract_pptx(pptx(charts={CHART_PART: chart(namespace="urn:c")}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="not rooted at its own chart space"):
        extract_pptx(pptx(charts={CHART_PART: chart(root="chart")}), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="OOXML XML is malformed"):
        extract_pptx(
            pptx(charts={CHART_PART: f'<c:chartSpace xmlns:c="{TRANSITIONAL_NS}">'}),
            capture_id=CAPTURE,
        )


def test_a_repeated_series_index_or_order_is_refused() -> None:
    for second in (series(order="1"), series(idx="1")):
        with pytest.raises(OOXMLInvalid, match="series index or order is repeated"):
            extract_pptx(
                pptx(
                    charts={
                        CHART_PART: chart(
                            f"<c:plotArea><c:barChart>{series()}{second}"
                            "</c:barChart></c:plotArea>"
                        )
                    }
                ),
                capture_id=CAPTURE,
            )


def test_a_malformed_series_index_or_order_is_refused() -> None:
    for one in (
        series(idx=None),
        series(order=None),
        series(idx="-1"),
        series(order="4294967296"),
        series(idx="1.0"),
    ):
        with pytest.raises(OOXMLInvalid, match="series index or order is malformed"):
            extract_pptx(
                pptx(charts={CHART_PART: chart(f"<c:plotArea>{one}</c:plotArea>")}),
                capture_id=CAPTURE,
            )


def test_a_malformed_cache_point_count_or_index_is_refused() -> None:
    for cache in (
        count("x") + points(("0", "12")),
        count("-1") + points(("0", "12")),
        count("4294967296") + points(("0", "12")),
        count("1") + points(("z", "12")),
        count("1") + points((" 0", "12")),
    ):
        with pytest.raises(OOXMLInvalid, match="point count is malformed"):
            extract_pptx(_valued(cache), capture_id=CAPTURE)

    # A point beyond what the cache says its source held names no position.
    with pytest.raises(OOXMLInvalid, match="index is outside its count"):
        extract_pptx(_valued(count("1") + points(("1", "12"))), capture_id=CAPTURE)

    with pytest.raises(OOXMLInvalid, match="point index is repeated"):
        extract_pptx(
            _valued(count("2") + points(("0", "12"), ("0", "15"))), capture_id=CAPTURE
        )


def test_a_point_that_stored_no_value_is_refused() -> None:
    """A point without a value stored none, which is not an empty one."""
    with pytest.raises(OOXMLInvalid, match="point value is malformed"):
        extract_pptx(_valued(count("1") + points(("0", None))), capture_id=CAPTURE)

    nested = count("1") + '<c:pt idx="0"><c:v>12<c:x/></c:v></c:pt>'
    with pytest.raises(OOXMLInvalid, match="point value is malformed"):
        extract_pptx(_valued(nested), capture_id=CAPTURE)


def test_a_malformed_literal_or_level_point_is_refused() -> None:
    """A literal list and a level are held to the same point rules as a cache."""
    for body, reason in (
        (lit("str", count("1") + points(("0", None))), "point value is malformed"),
        (lit("str", count("1") + points(("z", "Q1"))), "point count is malformed"),
        (lit("str", count("1") + points(("1", "Q1"))), "index is outside its count"),
        (
            lit("str", count("2") + points(("0", "Q1"), ("0", "Q2"))),
            "point index is repeated",
        ),
        (
            multi("Sheet1!$A$1", points(("0", None)), total="1"),
            "point value is malformed",
        ),
        (
            multi("Sheet1!$A$1", points(("1", "Q1")), total="1"),
            "index is outside its count",
        ),
        (
            multi("Sheet1!$A$1", points(("0", "Q1"), ("0", "Q2")), total="2"),
            "point index is repeated",
        ),
    ):
        with pytest.raises(OOXMLInvalid, match=reason):
            extract_pptx(
                _plotted(series(name=None, cat=body, val=None)), capture_id=CAPTURE
            )


def test_a_slot_with_two_sources_or_a_doubled_slot_is_refused() -> None:
    """A slot holds one source, and a series holds one of each slot."""
    for body in (
        ref("str", "Sheet1!$B$1", count("1") + points(("0", "a")))
        + lit("str", count("1") + points(("0", "b"))),
        "<c:v>a</c:v><c:v>b</c:v>",
        multi("Sheet1!$A$1", points(("0", "a")), total="1")
        + ref("str", "Sheet1!$A$1", count("1") + points(("0", "b"))),
    ):
        with pytest.raises(OOXMLInvalid, match="more than one data source"):
            extract_pptx(
                _plotted(series(name=body, cat=None, val=None)), capture_id=CAPTURE
            )

    doubled = series(cat=None, val=None).replace(
        "</c:ser>", "<c:tx><c:v>again</c:v></c:tx></c:ser>"
    )
    with pytest.raises(OOXMLInvalid, match="more than one of a slot"):
        extract_pptx(_plotted(doubled), capture_id=CAPTURE)


def test_a_chart_holding_two_titles_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="more than one title"):
        extract_pptx(
            pptx(charts={CHART_PART: chart(RICH_TITLE * 2 + "<c:plotArea/>")}),
            capture_id=CAPTURE,
        )


def test_an_empty_literal_or_level_value_is_the_empty_claim_it_is() -> None:
    """A stored empty string claims a point and says it is blank."""
    content = _plotted(
        series(
            name=None,
            cat=multi("Sheet1!$A$1", points(("0", "")), total="1"),
            val=lit("num", count("1") + points(("0", ""))),
        )
    )

    assert [
        (item.text, item.structured_value)
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "chart"
    ] == [(None, {"value": ""})] * 2


def test_literal_and_level_counts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    levels = multi("Sheet1!$A$1", *[points(("0", "Q1"))] * 4, total="1")
    levelled_chart = _plotted(series(name=None, cat=levels, val=None))

    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_CATEGORY_LEVELS", 3)
    with pytest.raises(OOXMLInvalid, match="too many category levels"):
        extract_pptx(levelled_chart, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_CATEGORY_LEVELS", 256)

    # The point bound counts every level, and a literal list, like any cache.
    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_POINTS", 3)
    with pytest.raises(OOXMLInvalid, match="too many cached points"):
        extract_pptx(levelled_chart, capture_id=CAPTURE)

    literal = lit(
        "num", count("4") + points(("0", "1"), ("1", "2"), ("2", "3"), ("3", "4"))
    )
    with pytest.raises(OOXMLInvalid, match="too many cached points"):
        extract_pptx(
            _plotted(series(name=None, cat=None, val=literal)), capture_id=CAPTURE
        )


def _plotted(body: str) -> bytes:
    """A package whose one chart's plot area holds ``body``."""
    return pptx(charts={CHART_PART: chart(f"<c:plotArea>{body}</c:plotArea>")})


def _valued(cache: str) -> bytes:
    """A package whose one series carries ``cache`` as its value cache."""
    return _plotted(series(cat=None, val=ref("num", "Sheet1!$B$2", cache)))


def test_chart_counts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    many = "".join(series(idx=str(index), order=str(index)) for index in range(4))
    plotted = pptx(charts={CHART_PART: chart(f"<c:plotArea>{many}</c:plotArea>")})

    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_SERIES", 3)
    with pytest.raises(OOXMLInvalid, match="too many series"):
        extract_pptx(plotted, capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_SERIES", 1_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_POINTS", 2)
    with pytest.raises(OOXMLInvalid, match="too many cached points"):
        extract_pptx(pptx(), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_POINTS", 100_000)

    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_TITLE_RUNS", 1)
    with pytest.raises(OOXMLInvalid, match="title has too many runs"):
        extract_pptx(pptx(), capture_id=CAPTURE)
    monkeypatch.setattr(ooxml_extractors, "_MAX_CHART_TITLE_RUNS", 20_000)

    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 2)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_pptx(pptx(), capture_id=CAPTURE)
