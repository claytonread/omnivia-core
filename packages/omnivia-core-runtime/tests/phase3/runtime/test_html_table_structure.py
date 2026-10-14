"""The place an HTML table cell keeps: which table, row, column and header.

Every case here is driven through the public ``extract_document`` entry point,
so what is asserted is the adapter's published locator and not a parser
internal. The grid cases are written as the smallest table that can tell a
right answer from a plausible wrong one: a cell under a ``rowspan`` is placed in
the column it really falls in rather than in its own position among its
siblings, and a table nested in a cell advances none of the enclosing table's
counters.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_extractors
from omnivia_core_runtime.service.document_extractors import (
    MAX_HTML_ELEMENTS,
    MAX_HTML_TABLE_GRID_SLOTS,
    MAX_HTML_TABLE_SPAN,
    DocumentExtractionInvalid,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome

CAPTURE: Final = "dcap-" + "c" * 64
#: A table with every row group, a caption, header and data cells: the fixture
#: the section, kind and caption cases all read.
GROUPED: Final = (
    b"<table><caption>Quarterly</caption>"
    b"<thead><tr><th>Metric</th><th>Q1</th></tr></thead>"
    b"<tbody><tr><th>Sales</th><td>5</td></tr></tbody>"
    b"<tfoot><tr><th>Total</th><td>5</td></tr></tfoot>"
    b"</table>"
)
#: A nested table inside a data cell, with cells in the outer table on both
#: sides of it so a leaked counter would move one of them.
NESTED: Final = (
    b"<table><tr><td>before</td>"
    b"<td><table><tr><td>inner-a</td><td>inner-b</td></tr>"
    b"<tr><td>inner-c</td></tr></table></td>"
    b"<td>after</td></tr>"
    b"<tr><td>next</td></tr></table>"
)


def html(source: bytes, *, media_type: str = "text/html") -> ExtractionOutcome:
    return extract_document(source, media_type=media_type, capture_id=CAPTURE)


def cells(result: ExtractionOutcome) -> list[dict[str, Any]]:
    """Every located cell, as its text and the place its locator keeps."""
    return [
        {
            "text": item.text,
            **{
                key: value
                for key, value in item.source_locator.items()
                if key
                in {
                    "path",
                    "table",
                    "row",
                    "column",
                    "section",
                    "cellKind",
                    "rowSpan",
                    "colSpan",
                    "headers",
                }
            },
        }
        for item in result.segments
        if item.semantic_role == "table_cell"
    ]


def grid(result: ExtractionOutcome) -> list[tuple[str | None, int, int, int]]:
    """Each cell's text against the table, row and column it was placed in."""
    return [
        (item["text"], item["table"], item["row"], item["column"])
        for item in cells(result)
    ]


def test_a_cell_keeps_its_dom_path_and_its_place_in_the_grid() -> None:
    result = html(b"<table><tr><td>only</td></tr></table>")

    # The DOM path still locates the element; the grid states what the path
    # cannot, which is the column the cell really falls in.
    assert cells(result) == [
        {
            "text": "only",
            "path": "/1:table/1:tr/1:td",
            "table": 1,
            "row": 1,
            "column": 1,
            "section": "direct",
            "cellKind": "td",
            "rowSpan": 1,
            "colSpan": 1,
            "headers": [],
        }
    ]


def test_rows_keep_one_ordinal_across_every_row_group() -> None:
    result = html(GROUPED)

    # The row ordinal runs over the whole table in source order and the group is
    # reported beside it, so neither reading has to be recovered from the other.
    assert [
        (item["text"], item["row"], item["section"], item["cellKind"])
        for item in cells(result)
    ] == [
        ("Metric", 1, "thead", "th"),
        ("Q1", 1, "thead", "th"),
        ("Sales", 2, "tbody", "th"),
        ("5", 2, "tbody", "td"),
        ("Total", 3, "tfoot", "th"),
        ("5", 3, "tfoot", "td"),
    ]


def test_a_row_outside_every_group_sits_directly_in_its_table() -> None:
    result = html(b"<table><tr><th>H</th><td>D</td></tr></table>")

    assert [(item["cellKind"], item["section"]) for item in cells(result)] == [
        ("th", "direct"),
        ("td", "direct"),
    ]


def test_a_caption_keeps_the_ordinal_of_the_table_it_captions() -> None:
    result = html(
        b"<table><caption>First</caption><tr><td>a</td></tr></table>"
        b"<table><caption>Second</caption><tr><td>b</td></tr></table>"
    )

    captions = [
        (item.text, item.source_locator["table"])
        for item in result.segments
        if item.source_locator.get("element") == "caption"
    ]
    assert captions == [("First", 1), ("Second", 2)]
    # The caption's own table is the one its cells report, so the association
    # survives a second table in the same source.
    assert grid(result) == [("a", 1, 1, 1), ("b", 2, 1, 1)]


def test_a_colspan_moves_the_next_cell_of_the_same_row() -> None:
    result = html(b'<table><tr><td colspan="2">wide</td><td>tail</td></tr></table>')

    assert [
        (item["text"], item["column"], item["colSpan"]) for item in cells(result)
    ] == [("wide", 1, 2), ("tail", 3, 1)]


def test_a_rowspan_holds_its_column_against_the_rows_below_it() -> None:
    result = html(
        b'<table><tr><td rowspan="3">held</td><td>r1</td></tr>'
        b"<tr><td>r2</td></tr><tr><td>r3</td></tr><tr><td>r4</td></tr></table>"
    )

    # Rows two and three have no cell of their own in column one, so their first
    # cell is placed in column two; row four is past the span and reclaims it.
    assert grid(result) == [
        ("held", 1, 1, 1),
        ("r1", 1, 1, 2),
        ("r2", 1, 2, 2),
        ("r3", 1, 3, 2),
        ("r4", 1, 4, 1),
    ]


def test_a_spanning_cell_claims_every_column_it_covers() -> None:
    result = html(
        b'<table><tr><td rowspan="2" colspan="2">block</td><td>a</td></tr>'
        b"<tr><td>b</td></tr></table>"
    )

    # The block holds columns one and two in both rows, so the second row's only
    # cell is placed in column three rather than in column one.
    assert grid(result) == [("block", 1, 1, 1), ("a", 1, 1, 3), ("b", 1, 2, 3)]


def test_a_data_cell_keeps_its_column_and_row_headers() -> None:
    result = html(GROUPED)

    assert [(item["text"], item["headers"]) for item in cells(result)] == [
        ("Metric", []),
        # The first `th` of a head row heads its column, not the cells beside it.
        ("Q1", []),
        ("Sales", ["/1:table/2:thead/1:tr/1:th"]),
        (
            "5",
            [
                "/1:table/2:thead/1:tr/2:th",
                "/1:table/3:tbody/1:tr/1:th",
            ],
        ),
        ("Total", ["/1:table/2:thead/1:tr/1:th"]),
        (
            "5",
            [
                "/1:table/2:thead/1:tr/2:th",
                "/1:table/4:tfoot/1:tr/1:th",
            ],
        ),
    ]


def test_the_first_header_of_a_column_heads_every_row_under_it() -> None:
    result = html(
        b"<table><tr><th>Name</th><th>Age</th></tr>"
        b"<tr><td>Alice</td><td>30</td></tr>"
        b"<tr><td>Bob</td><td>41</td></tr></table>"
    )

    # A table with no `thead` still has a header row; the header a column keeps
    # is the first one, so a later header row never displaces it.
    assert [(item["text"], item["headers"]) for item in cells(result)] == [
        ("Name", []),
        ("Age", []),
        ("Alice", ["/1:table/1:tr/1:th"]),
        ("30", ["/1:table/1:tr/2:th"]),
        ("Bob", ["/1:table/1:tr/1:th"]),
        ("41", ["/1:table/1:tr/2:th"]),
    ]


def test_a_row_header_spanning_rows_heads_each_row_it_covers() -> None:
    result = html(
        b'<table><tr><th rowspan="2">Region</th><td>north</td></tr>'
        b"<tr><td>south</td></tr><tr><td>loose</td></tr></table>"
    )

    assert [(item["text"], item["headers"]) for item in cells(result)] == [
        ("Region", []),
        ("north", ["/1:table/1:tr/1:th"]),
        ("south", ["/1:table/1:tr/1:th"]),
        # Past the span the header no longer reaches, and the cell reclaims the
        # column the header held, whose own first header is that same `th`.
        ("loose", ["/1:table/1:tr/1:th"]),
    ]


def test_cells_closed_only_by_the_next_cell_still_advance_the_row() -> None:
    result = html(b"<table><tr><td>a<td>b<td>c</tr><tr><td>d</tr></table>")

    # A cell is placed at its start tag, which is read after the implied end of
    # the cell before it, so a row written without end tags fills the same
    # columns as one written with them.
    assert grid(result) == [
        ("a", 1, 1, 1),
        ("b", 1, 1, 2),
        ("c", 1, 1, 3),
        ("d", 1, 2, 1),
    ]


def test_a_nested_table_keeps_its_own_identity_and_its_own_counters() -> None:
    result = html(NESTED)

    # The inner table is table two and counts its own rows and columns from one;
    # the outer cells on both sides of it keep the places they would have had
    # if it were not there at all.
    assert grid(result) == [
        ("before", 1, 1, 1),
        ("inner-a", 2, 1, 1),
        ("inner-b", 2, 1, 2),
        ("inner-c", 2, 2, 1),
        ("after", 1, 1, 3),
        ("next", 1, 2, 1),
    ]


def test_a_nested_table_leaks_no_header_into_the_table_around_it() -> None:
    result = html(
        b"<table><tr><th>Outer</th><td><table><tr><th>Inner</th>"
        b"<td>in</td></tr></table></td></tr>"
        b"<tr><td>below</td><td>beside</td></tr></table>"
    )

    assert [(item["text"], item["headers"]) for item in cells(result)] == [
        ("Outer", []),
        ("Inner", []),
        ("in", ["/1:table/1:tr/2:td/1:table/1:tr/1:th"]),
        ("below", ["/1:table/1:tr/1:th"]),
        ("beside", []),
    ]


def test_source_order_is_the_order_the_cells_are_published_in() -> None:
    result = html(NESTED)

    assert [item["text"] for item in cells(result)] == [
        "before",
        "inner-a",
        "inner-b",
        "inner-c",
        "after",
        "next",
    ]
    # The same bytes place the same cells on every run, so the digest the
    # revision is identified by does not move.
    repeated = html(NESTED)
    assert cells(repeated) == cells(result)
    assert repeated.output_digest == result.output_digest


@pytest.mark.parametrize("attribute", ["rowspan", "colspan"])
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"0", "outside the accepted bound"),
        (b"-1", "not a positive decimal"),
        (b"1.5", "not a positive decimal"),
        (b"abc", "not a positive decimal"),
        (b"", "not a positive decimal"),
        (b" 2 ", "not a positive decimal"),
        (b"+2", "not a positive decimal"),
        (b"0x2", "not a positive decimal"),
        # An Arabic-Indic two is a decimal digit that ``int`` would read as two,
        # so only ASCII digits are accepted and this is refused by syntax.
        ("٢".encode(), "not a positive decimal"),
        (b"1001", "outside the accepted bound"),
        (b"99999", "not a positive decimal"),
    ],
)
def test_a_malformed_span_refuses_the_source_rather_than_clamping_it(
    attribute: str, value: bytes, reason: str
) -> None:
    source = (
        b"<table><tr><td "
        + attribute.encode()
        + b'="'
        + value
        + b'">x</td></tr></table>'
    )

    with pytest.raises(DocumentExtractionInvalid, match=reason):
        html(source)

    # The same markup carrying a span at the bound is accepted, so the refusal
    # is the validation speaking and not a defect in the fixture.
    assert MAX_HTML_TABLE_SPAN == 1000
    accepted = source.replace(b'="' + value + b'"', b'="1000"')
    assert html(accepted).state == "succeeded"


def test_a_wide_cell_shifts_past_a_column_a_carried_span_still_occupies() -> None:
    result = html(
        b'<table><tr><td>a</td><td rowspan="2">held</td><td>c</td></tr>'
        b'<tr><td colspan="2">wide</td></tr></table>'
    )

    # Column one of the second row is free, but the column beside it is not, and
    # a cell covers contiguous columns only: the run has to start past the
    # carried span rather than straddle it, which leaves column one empty.
    assert [
        (item["text"], item["column"], item["colSpan"]) for item in cells(result)
    ] == [("a", 1, 1), ("held", 2, 1), ("c", 3, 1), ("wide", 3, 2)]


def test_a_shifted_cell_claims_its_own_columns_and_overwrites_no_other() -> None:
    result = html(
        b'<table><tr><td>a</td><td rowspan="3">held</td><td>c</td></tr>'
        b'<tr><td colspan="2" rowspan="2">wide</td></tr>'
        b"<tr><td>y</td><td>z</td></tr></table>"
    )

    # The shifted cell takes columns three and four for two rows, and the span it
    # stepped over still holds column two: the third row therefore finds columns
    # two, three and four all taken and places its second cell in column five.
    assert grid(result) == [
        ("a", 1, 1, 1),
        ("held", 1, 1, 2),
        ("c", 1, 1, 3),
        ("wide", 1, 2, 3),
        ("y", 1, 3, 1),
        ("z", 1, 3, 5),
    ]


def test_a_run_that_no_longer_fits_the_column_bound_refuses_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b'<table><tr><td>a</td><td rowspan="2">held</td></tr>'
        b'<tr><td colspan="3">wide</td></tr></table>'
    )
    # Three columns fit inside a four-column grid, but not once the run has to
    # start past the carried span in column two.
    monkeypatch.setattr(document_extractors, "MAX_HTML_TABLE_COLUMNS", 4)

    with pytest.raises(DocumentExtractionInvalid, match="too many columns"):
        html(source)

    monkeypatch.undo()
    assert [item["column"] for item in cells(html(source))] == [1, 2, 3]


def test_the_grid_budget_refuses_a_source_rather_than_truncating_its_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b'<table><tr><td colspan="4">a</td><td colspan="4">b</td></tr></table>'
    # Each cell charges the four columns its placement scans and the four it then
    # claims, so this source charges exactly sixteen slots and the first cell
    # exactly eight. A bound of eight therefore admits the first and refuses the
    # second, which is the aggregate speaking: neither cell breaches the column
    # or span bound on its own.
    monkeypatch.setattr(document_extractors, "MAX_HTML_TABLE_GRID_SLOTS", 8)

    with pytest.raises(DocumentExtractionInvalid, match="accepted work bound"):
        html(source)

    monkeypatch.setattr(document_extractors, "MAX_HTML_TABLE_GRID_SLOTS", 16)
    assert [item["text"] for item in cells(html(source))] == ["a", "b"]

    # The published bound is a hundredfold below the slots the cell and span
    # bounds would allow between them, which is the room the aggregate removes.
    monkeypatch.undo()
    assert MAX_HTML_TABLE_GRID_SLOTS * 100 == MAX_HTML_ELEMENTS * MAX_HTML_TABLE_SPAN
    assert html(source).state == "succeeded"


def test_the_column_bound_refuses_a_grid_rather_than_truncating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b'<table><tr><td colspan="3">wide</td></tr></table>'
    monkeypatch.setattr(document_extractors, "MAX_HTML_TABLE_COLUMNS", 2)

    with pytest.raises(DocumentExtractionInvalid, match="too many columns"):
        html(source)

    monkeypatch.undo()
    assert html(source).state == "succeeded"


def test_equivalent_html_and_xhtml_place_their_cells_identically() -> None:
    markup = (
        b"<table><caption>Quarterly</caption>"
        b"<thead><tr><th>Metric</th><th>Q1</th></tr></thead>"
        b'<tbody><tr><th rowspan="2">Sales</th><td colspan="2">5</td></tr>'
        b"<tr><td>6</td></tr></tbody></table>"
    )
    xhtml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        + markup
        + b"</body></html>"
    )

    def unwrap(item: dict[str, Any]) -> dict[str, Any]:
        """Drop the document the XHTML fixture must wrap its table in."""
        prefix = "/1:html/1:body"
        return {
            **item,
            "path": item["path"].removeprefix(prefix),
            "headers": [header.removeprefix(prefix) for header in item["headers"]],
        }

    plain = html(markup)
    strict = html(xhtml, media_type="application/xhtml+xml")

    # The admission parse differs between the two; nothing a cell keeps does.
    # Only the wrapping document separates the two paths, so once it is dropped
    # every place, span and header relationship is identical.
    assert cells(plain) == [unwrap(item) for item in cells(strict)]
    assert next(item["path"] for item in cells(strict)).startswith("/1:html/1:body/")


def test_a_cell_outside_any_table_keeps_no_place() -> None:
    result = html(b"<div><td>stray</td></div>")

    # There is no grid for a stray cell to have a place in, so none is invented
    # and the text is still located by its DOM path.
    assert cells(result) == [{"text": "stray", "path": "/1:div/1:td"}]


def test_a_cell_the_source_put_outside_a_row_still_falls_in_a_row() -> None:
    result = html(b"<table><td>loose</td></table>")

    assert grid(result) == [("loose", 1, 1, 1)]


def test_non_table_extraction_is_unchanged_by_the_grid() -> None:
    source = (
        b'<div><h1>Title</h1><p>body <a href="next.html">next</a></p>'
        b"<ul><li>one</li></ul>"
        b'<p><img src="map.png" alt="Map"></p></div>'
    )

    result = html(source)

    # No table, so nothing a cell keeps reaches any of these locators.
    assert [
        (item.semantic_role, item.text, dict(item.source_locator))
        for item in result.segments
        if item.text is not None
    ] == [
        (
            "heading",
            "Title",
            {
                "captureId": CAPTURE,
                "unit": "element",
                "element": "h1",
                "path": "/1:div/1:h1",
                "run": 1,
            },
        ),
        (
            "body",
            "body next",
            {
                "captureId": CAPTURE,
                "unit": "element",
                "element": "p",
                "path": "/1:div/2:p",
                "run": 1,
            },
        ),
        (
            "list_item",
            "one",
            {
                "captureId": CAPTURE,
                "unit": "element",
                "element": "li",
                "path": "/1:div/3:ul/1:li",
                "run": 1,
            },
        ),
        (
            "image_alt",
            "Map",
            {
                "captureId": CAPTURE,
                "unit": "element",
                "element": "img",
                "path": "/1:div/4:p/1:img",
            },
        ),
    ]


def test_a_link_inside_a_cell_stays_inert_inventory() -> None:
    result = html(
        b'<table><tr><td><a href="http://example.invalid/x">here</a></td></tr></table>'
    )

    inventory = [item for item in result.segments if item.structured_value is not None]
    assert [
        (
            item.semantic_role,
            item.extraction_method,
            item.visibility,
            item.structured_value["target"],
            item.structured_value["resolved"],
        )
        for item in inventory
    ] == [
        (
            "object_inventory",
            "inventory",
            "annotation",
            "http://example.invalid/x",
            False,
        )
    ]
    # The reference is located by its own element and keeps no cell place of its
    # own, so inventorying it never reads as a cell of the table.
    assert "column" not in inventory[0].source_locator
